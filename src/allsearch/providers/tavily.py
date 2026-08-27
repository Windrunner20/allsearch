"""Tavily search adapter with multi-key rotation pool."""

from __future__ import annotations

import time
from typing import Any, Literal

from allsearch.config import TavilyConfig
from allsearch.errors import AuthError, ProviderUnavailableError, TransportError
from allsearch.health import HealthRegistry
from allsearch.models import ProviderResultItem, ProviderSearchResult
from allsearch.transport import HttpTransport

# How long (seconds) a key is parked after a rate-limit / quota-style failure.
_KEY_COOLDOWN_SECONDS = 60.0


class _KeyPool:
    """Round-robin key selector that parks keys temporarily on quota failures."""

    def __init__(self, keys: tuple[str, ...], *, cooldown_seconds: float = _KEY_COOLDOWN_SECONDS) -> None:
        self._keys = keys
        self._cooldown_seconds = cooldown_seconds
        self._index = 0
        # key -> monotonic time until which the key is parked (0 = available)
        self._parked_until: dict[str, float] = {}
        self._lock_tick = 0  # increments on each park; drives index advancement

    def __len__(self) -> int:
        return len(self._keys)

    def available_keys(self) -> tuple[str, ...]:
        now = time.monotonic()
        return tuple(k for k in self._keys if self._parked_until.get(k, 0.0) <= now)

    def pick(self) -> str | None:
        """Return the next non-parked key in round-robin order, or None if all parked."""
        now = time.monotonic()
        n = len(self._keys)
        if n == 0:
            return None
        for offset in range(n):
            idx = (self._index + offset) % n
            key = self._keys[idx]
            if self._parked_until.get(key, 0.0) <= now:
                self._index = (idx + 1) % n
                return key
        return None

    def park(self, key: str) -> None:
        """Temporarily disable a key after a rate-limit / quota failure."""
        self._parked_until[key] = time.monotonic() + self._cooldown_seconds
        # advance so the next pick() tries a different key first
        try:
            base = self._keys.index(key)
        except ValueError:
            return
        self._index = (base + 1) % len(self._keys)

    def reset(self, key: str) -> None:
        self._parked_until.pop(key, None)


class TavilyProvider:
    name = "tavily"

    def __init__(
        self,
        config: TavilyConfig,
        transport: HttpTransport,
        health: HealthRegistry | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.health = health
        self.pool = _KeyPool(config.all_keys())

    def configured(self) -> bool:
        return len(self.pool) > 0

    def build_payload(
        self,
        api_key: str,
        query: str,
        *,
        max_results: int = 8,
        search_depth: Literal["basic", "advanced"] | None = None,
        topic: Literal["general", "news"] = "general",
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        clamped = max(1, min(int(max_results), self.config.max_results, 20))
        payload: dict[str, Any] = {
            "api_key": api_key,
            "query": query,
            "max_results": clamped,
            "search_depth": search_depth or self.config.default_depth,
            "topic": topic,
            "include_answer": True,
            "include_raw_content": False,
        }
        if include_domains:
            payload["include_domains"] = include_domains
        if exclude_domains:
            payload["exclude_domains"] = exclude_domains
        return payload

    def _is_quota_signal(self, exc: BaseException) -> bool:
        """Heuristic: does this error suggest the current key hit its quota / rate limit?"""
        # 429 / 5xx transport errors and HTTP-level rate limiting -> rotate to next key.
        status = getattr(exc, "status_code", None)
        if status == 429:
            return True
        msg = str(getattr(exc, "message", "") or exc).lower()
        quota_markers = (
            "rate limit",
            "rate_limit",
            "ratelimit",
            "quota",
            "exceeded",
            "too many requests",
            "credit",
            "usage limit",
            "insufficient",
            "429",
        )
        return any(marker in msg for marker in quota_markers)

    async def search(
        self,
        query: str,
        *,
        max_results: int = 8,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        search_depth: Literal["basic", "advanced"] | None = None,
        topic: Literal["general", "news"] = "general",
        deadline_seconds: float | None = None,
        **_: Any,
    ) -> ProviderSearchResult:
        if not self.configured():
            raise ProviderUnavailableError("Tavily API key not configured", provider=self.name)
        if self.health:
            self.health.ensure_closed_or_raise(self.name)

        url = f"{self.config.base_url.rstrip('/')}{self.config.search_path}"
        last_exc: BaseException | None = None
        attempts = 0
        # Try at most every available key once.
        max_attempts = len(self.pool.available_keys())

        while attempts < max_attempts:
            attempts += 1
            key = self.pool.pick()
            if key is None:
                # All keys parked — last attempt to surface whatever error we saw.
                break

            payload = self.build_payload(
                key,
                query,
                max_results=max_results,
                search_depth=search_depth,
                topic=topic,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
            )
            started = time.perf_counter()
            try:
                response = await self.transport.request_json(
                    "POST",
                    url,
                    headers={"Content-Type": "application/json"},
                    json=payload,
                    deadline_seconds=deadline_seconds,
                    provider=self.name,
                )
                latency_ms = int((time.perf_counter() - started) * 1000)
                results: list[ProviderResultItem] = []
                citations: list[dict[str, str]] = []
                for item in response.get("results") or []:
                    if not isinstance(item, dict):
                        continue
                    item_url = str(item.get("url") or "")
                    title = str(item.get("title") or "")
                    snippet = str(item.get("content") or item.get("snippet") or "")
                    results.append(
                        ProviderResultItem(
                            title=title,
                            url=item_url,
                            snippet=snippet,
                            content=None,
                            published_at=item.get("published_date") or item.get("published_at"),
                            source_type="news" if topic == "news" else "web",
                            provider=self.name,
                            score=item.get("score") if isinstance(item.get("score"), (int, float)) else None,
                        )
                    )
                    if item_url:
                        citations.append({"title": title, "url": item_url})
                # success: this key is healthy
                self.pool.reset(key)
                if self.health:
                    self.health.record_success(self.name, latency_ms)
                return ProviderSearchResult(
                    provider=self.name,
                    query=str(response.get("query") or query),
                    answer=str(response.get("answer") or ""),
                    results=results,
                    citations=citations,
                    latency_ms=latency_ms,
                )
            except AuthError as exc:
                # 401/403 on this key — park it (invalid/revoked) and try the next.
                last_exc = exc
                if len(self.pool) > 1:
                    self.pool.park(key)
                    continue
                if self.health:
                    self.health.record_failure(self.name, "auth_error", retryable=False)
                raise
            except (TransportError, Exception) as exc:  # noqa: BLE001 - re-raised below
                last_exc = exc
                retryable = bool(getattr(exc, "retryable", True))
                code = getattr(exc, "code", "error")
                if self._is_quota_signal(exc) and len(self.pool) > 1 and self.pool.available_keys():
                    # Quota / rate-limit on this key: park it and rotate to the next.
                    self.pool.park(key)
                    continue
                if self.health:
                    self.health.record_failure(self.name, str(code), retryable=retryable)
                raise

        # Exhausted all keys via rotation.
        if last_exc is not None:
            if self.health:
                code = getattr(last_exc, "code", "error")
                retryable = bool(getattr(last_exc, "retryable", True))
                self.health.record_failure(self.name, str(code), retryable=retryable)
            raise last_exc
        raise ProviderUnavailableError(
            "All Tavily keys are rate-limited or unavailable", provider=self.name
        )
