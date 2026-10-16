"""Firecrawl known-URL scrape adapter."""

from __future__ import annotations

import time
from collections import deque
from typing import Any
from urllib.parse import urlparse

from allsearch.config import FirecrawlConfig
from allsearch.errors import AllSearchError, AuthError, ProviderUnavailableError, TargetError
from allsearch.health import HealthRegistry
from allsearch.models import ProviderFetchResult
from allsearch.transport import HttpTransport


class FirecrawlProvider:
    name = "firecrawl"

    def __init__(
        self,
        config: FirecrawlConfig,
        transport: HttpTransport,
        health: HealthRegistry | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.health = health
        # Hosts of consecutive per-URL failures. Only failures spread over
        # several unrelated sites say something about Firecrawl itself.
        self._target_failures: deque[str] = deque(maxlen=3)

    def configured(self) -> bool:
        return self.config.configured()

    def build_payload(self, url: str) -> dict[str, Any]:
        return {
            "url": url,
            "formats": ["markdown"],
            "onlyMainContent": self.config.only_main_content,
        }

    @staticmethod
    def _target_error(exc: AllSearchError) -> TargetError | None:
        """Per-URL failures reported by Firecrawl, as opposed to provider failures."""
        status = getattr(exc, "status_code", None)
        message = str(exc.message or "").lower()
        if status == 403 and "not support" in message:
            return TargetError("Firecrawl does not support this site", code="site_not_supported", provider="firecrawl")
        if status == 408 or (status == 500 and ("scrap" in message or "timed out" in message or "engine" in message)):
            return TargetError(f"Firecrawl could not read this URL (HTTP {status})", code="target_unreadable", provider="firecrawl")
        return None

    async def scrape(
        self,
        url: str,
        *,
        deadline_seconds: float | None = None,
        **_: Any,
    ) -> ProviderFetchResult:
        if not self.configured():
            raise ProviderUnavailableError(
                "Firecrawl API key not configured",
                provider=self.name,
                code="fetch_unavailable",
            )
        if self.health:
            self.health.ensure_closed_or_raise(self.name)

        payload = self.build_payload(url)
        endpoint = f"{self.config.base_url.rstrip('/')}{self.config.scrape_path}"
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        started = time.perf_counter()
        try:
            try:
                response = await self.transport.request_json(
                    "POST",
                    endpoint,
                    headers=headers,
                    json=payload,
                    deadline_seconds=deadline_seconds,
                    provider=self.name,
                    # A failed scrape of one URL fails again on retry; do not
                    # spend the budget (or credits) on it.
                    retries=0,
                )
            except AllSearchError as exc:
                target = self._target_error(exc)
                if target is None:
                    raise
                host = (urlparse(url).hostname or "").lower()
                self._target_failures.append(host)
                if self.health and len(self._target_failures) == 3 and len(set(self._target_failures)) == 3:
                    self.health.record_failure(self.name, target.code, retryable=True)
                raise target from exc
            self._target_failures.clear()
            latency_ms = int((time.perf_counter() - started) * 1000)
            data = response.get("data") or {}
            if not isinstance(data, dict):
                data = {}
            content = str(data.get("markdown") or "")
            if not content and data.get("json") is not None:
                import json

                content = json.dumps(data["json"], ensure_ascii=False, indent=2)
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            final_url = (
                str(metadata.get("sourceURL") or metadata.get("url") or data.get("url") or url)
            )
            title = str(metadata.get("title") or data.get("title") or "")
            if self.health:
                self.health.record_success(self.name, latency_ms)
            return ProviderFetchResult(
                provider=self.name,
                url=url,
                final_url=final_url,
                title=title,
                content=content,
                content_type="text/markdown",
                metadata=metadata,
                latency_ms=latency_ms,
            )
        except TargetError:
            raise
        except AuthError:
            if self.health:
                self.health.record_failure(self.name, "auth_error", retryable=False)
            raise
        except Exception as exc:
            if self.health:
                code = getattr(exc, "code", "error")
                retryable = bool(getattr(exc, "retryable", True))
                self.health.record_failure(self.name, str(code), retryable=retryable)
            raise
