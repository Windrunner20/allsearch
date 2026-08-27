"""Shared fixtures and fakes for AllSearch tests."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from typing import Any

import pytest

from allsearch.cache import MemoryCache
from allsearch.config import AllSearchConfig, load_config
from allsearch.health import HealthRegistry
from allsearch.models import ProviderFetchResult, ProviderResultItem, ProviderSearchResult
from allsearch.orchestrator import Orchestrator
from allsearch.transport import HttpTransport


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch):
    """Isolate config-related env vars."""
    keys = [k for k in os.environ if k.startswith("ALLSEARCH_")]
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    yield monkeypatch


@pytest.fixture
def base_config(clean_env) -> AllSearchConfig:
    clean_env.setenv("ALLSEARCH_XAI_API_KEY", "test-xai-key")
    clean_env.setenv("ALLSEARCH_TAVILY_API_KEY", "test-tavily-key")
    clean_env.setenv("ALLSEARCH_ANYSEARCH_API_KEY", "test-anysearch-key")
    clean_env.setenv("ALLSEARCH_FIRECRAWL_API_KEY", "test-firecrawl-key")
    clean_env.setenv("ALLSEARCH_XAI_MODEL", "grok-4.20")
    clean_env.setenv("ALLSEARCH_ALLOW_DEGRADED_SEARCH", "false")
    return load_config()


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
async def loop_clock(monkeypatch):
    """Patch the running loop's monotonic clock so timeout_at + Deadline share
    a controllable timeline. Returns the LoopClock instance."""
    clock = LoopClock()
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "time", clock)
    return clock


class LoopClock:
    """Controllable monotonic clock; assignable to loop.time."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def no_dns(monkeypatch):
    """Neutralize DNS resolution in orchestrator URL validation while keeping
    literal-IP/localhost/scheme checks. Records validation calls so tests can
    assert original + final URLs were both validated with resolve_dns=True."""
    import allsearch.orchestrator as orch_mod
    from allsearch.security import validate_public_http_url as real_validate

    calls: list[tuple[str, bool]] = []

    def fake_validate(url: str, *, resolve_dns: bool = True) -> str:
        calls.append((url, resolve_dns))
        return real_validate(url, resolve_dns=False)

    monkeypatch.setattr(orch_mod, "validate_public_http_url", fake_validate)
    return calls


class FakeXAI:
    name = "xai"

    def __init__(self, result: ProviderSearchResult | Exception | None = None) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def configured(self) -> bool:
        return True

    async def search(self, query: str, **kwargs: Any) -> ProviderSearchResult:
        self.calls.append({"query": query, **kwargs})
        if isinstance(self.result, Exception):
            raise self.result
        if self.result is None:
            return ProviderSearchResult(
                provider="xai",
                query=query,
                answer="Grok answer",
                results=[
                    ProviderResultItem(
                        title="A",
                        url="https://example.com/a",
                        snippet="sa",
                        provider="xai",
                    ),
                    ProviderResultItem(
                        title="B",
                        url="https://example.org/b",
                        snippet="sb",
                        provider="xai",
                    ),
                    ProviderResultItem(
                        title="C",
                        url="https://docs.example.com/c",
                        snippet="sc",
                        provider="xai",
                    ),
                ],
                citations=[
                    {"title": "A", "url": "https://example.com/a"},
                    {"title": "B", "url": "https://example.org/b"},
                    {"title": "C", "url": "https://docs.example.com/c"},
                ],
            )
        return self.result


class FakeTavily:
    name = "tavily"

    def __init__(self, result: ProviderSearchResult | Exception | None = None) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def configured(self) -> bool:
        return True

    async def search(self, query: str, **kwargs: Any) -> ProviderSearchResult:
        self.calls.append({"query": query, **kwargs})
        if isinstance(self.result, Exception):
            raise self.result
        if self.result is None:
            return ProviderSearchResult(
                provider="tavily",
                query=query,
                answer="Tavily answer",
                results=[
                    ProviderResultItem(
                        title="T1",
                        url="https://example.com/a",
                        snippet="tavily richer snippet for a",
                        provider="tavily",
                    ),
                    ProviderResultItem(
                        title="T2",
                        url="https://news.example.net/n",
                        snippet="news",
                        provider="tavily",
                    ),
                ],
                citations=[
                    {"title": "T1", "url": "https://example.com/a"},
                    {"title": "T2", "url": "https://news.example.net/n"},
                ],
            )
        return self.result


class FakeAnySearch:
    name = "anysearch"

    def __init__(self, result: ProviderSearchResult | Exception | None = None) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def configured(self) -> bool:
        return True

    async def search(self, query: str, **kwargs: Any) -> ProviderSearchResult:
        self.calls.append({"query": query, **kwargs})
        if isinstance(self.result, Exception):
            raise self.result
        if self.result is None:
            return ProviderSearchResult(
                provider="anysearch",
                query=query,
                results=[
                    ProviderResultItem(
                        title="CVE item",
                        url="https://nvd.nist.gov/vuln/detail/CVE-2024-1234",
                        snippet="vertical",
                        provider="anysearch",
                        source_type="vertical",
                        vertical=kwargs.get("domain"),
                    )
                ],
                citations=[
                    {
                        "title": "CVE item",
                        "url": "https://nvd.nist.gov/vuln/detail/CVE-2024-1234",
                    }
                ],
            )
        return self.result


class FakeFirecrawl:
    name = "firecrawl"

    def __init__(self, result: ProviderFetchResult | Exception | None = None) -> None:
        self.result = result
        self.calls: list[str] = []

    def configured(self) -> bool:
        return True

    async def scrape(self, url: str, **kwargs: Any) -> ProviderFetchResult:
        self.calls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        if self.result is None:
            return ProviderFetchResult(
                provider="firecrawl",
                url=url,
                final_url=url,
                title="Fetched",
                content="# hello\n\nbody content large enough",
            )
        return self.result


def make_orchestrator(
    config: AllSearchConfig,
    *,
    xai: Any = None,
    tavily: Any = None,
    anysearch: Any = None,
    firecrawl: Any = None,
    health: HealthRegistry | None = None,
    cache: MemoryCache | None = None,
) -> Orchestrator:
    cache = cache or MemoryCache(max_entries=config.cache.max_entries)
    health = health or HealthRegistry(cache=cache)
    transport = HttpTransport(timeout_seconds=5, max_retries=0)
    return Orchestrator(
        config,
        transport=transport,
        cache=cache,
        health=health,
        xai=xai or FakeXAI(),
        tavily=tavily or FakeTavily(),
        anysearch=anysearch or FakeAnySearch(),
        firecrawl=firecrawl or FakeFirecrawl(),
    )
