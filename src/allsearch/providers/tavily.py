"""Tavily search adapter."""

from __future__ import annotations

import time
from typing import Any, Literal

from allsearch.config import TavilyConfig
from allsearch.errors import AuthError, ProviderUnavailableError
from allsearch.health import HealthRegistry
from allsearch.models import ProviderResultItem, ProviderSearchResult
from allsearch.transport import HttpTransport


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

    def configured(self) -> bool:
        return self.config.configured()

    def build_payload(
        self,
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
            "api_key": self.config.api_key,
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

        payload = self.build_payload(
            query,
            max_results=max_results,
            search_depth=search_depth,
            topic=topic,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
        )
        url = f"{self.config.base_url.rstrip('/')}{self.config.search_path}"
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
