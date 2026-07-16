from __future__ import annotations

from dataclasses import replace

import pytest

from allsearch.errors import ProviderUnavailableError
from allsearch.models import ProviderSearchResult, SearchRequest
from tests.conftest import FakeAnySearch, FakeFirecrawl, FakeTavily, FakeXAI, make_orchestrator


@pytest.mark.asyncio
async def test_balanced_skips_tavily_when_primary_sufficient(base_config):
    xai = FakeXAI()
    tavily = FakeTavily()
    orch = make_orchestrator(base_config, xai=xai, tavily=tavily)
    resp = await orch.search(SearchRequest(query="stable query about cats", depth="balanced"))
    assert resp.status in {"ok", "partial"}
    assert xai.calls
    # primary has 3 citations / multi domain => skip tavily
    assert not tavily.calls
    assert any(s.outcome == "skipped" and s.provider == "tavily" for s in resp.route.stages)


@pytest.mark.asyncio
async def test_balanced_runs_tavily_when_primary_weak(base_config):
    weak = ProviderSearchResult(
        provider="xai",
        query="q",
        answer="thin",
        results=[],
        citations=[{"title": "only", "url": "https://only.example/x"}],
    )
    xai = FakeXAI(weak)
    tavily = FakeTavily()
    orch = make_orchestrator(base_config, xai=xai, tavily=tavily)
    resp = await orch.search(SearchRequest(query="need more sources", depth="balanced"))
    assert tavily.calls
    assert resp.results
    assert any(s.provider == "tavily" and s.outcome == "ok" for s in resp.route.stages)


@pytest.mark.asyncio
async def test_verify_runs_tavily_after_primary(base_config):
    # Weak primary (single citation) so verify supplements with Tavily.
    weak = ProviderSearchResult(
        provider="xai",
        query="verify this claim",
        answer="thin",
        results=[],
        citations=[{"title": "only", "url": "https://only.example/x"}],
    )
    xai = FakeXAI(weak)
    tavily = FakeTavily()
    orch = make_orchestrator(base_config, xai=xai, tavily=tavily)
    resp = await orch.search(SearchRequest(query="verify this claim", depth="verify"))
    assert xai.calls and tavily.calls
    assert resp.route.depth == "verify"
    assert any(s.provider == "tavily" and s.outcome == "ok" for s in resp.route.stages)


@pytest.mark.asyncio
async def test_primary_unavailable_strict(base_config):
    cfg = replace(base_config, allow_degraded_search=False)
    # mark xai unconfigured via config
    from allsearch.config import XAIConfig

    cfg = replace(
        cfg,
        xai=XAIConfig(
            api_key=None,
            base_url=cfg.xai.base_url,
            responses_path=cfg.xai.responses_path,
            model=cfg.xai.model,
            fallback_models=cfg.xai.fallback_models,
            reasoning_effort=cfg.xai.reasoning_effort,
            allowed_models=cfg.xai.allowed_models,
            max_tool_calls=cfg.xai.max_tool_calls,
        ),
    )
    orch = make_orchestrator(cfg, xai=FakeXAI(ProviderUnavailableError("no", provider="xai")))
    # health/config says unconfigured so plan skips xai
    resp = await orch.search(SearchRequest(query="anything", depth="balanced"))
    assert resp.status == "error"
    assert any(e.code == "primary_unavailable" for e in resp.errors)


@pytest.mark.asyncio
async def test_degraded_mode_uses_tavily_answer(base_config):
    from allsearch.config import XAIConfig

    cfg = replace(
        base_config,
        allow_degraded_search=True,
        xai=XAIConfig(
            api_key=None,
            base_url=base_config.xai.base_url,
            responses_path=base_config.xai.responses_path,
            model=base_config.xai.model,
            fallback_models=base_config.xai.fallback_models,
            reasoning_effort=base_config.xai.reasoning_effort,
            allowed_models=base_config.xai.allowed_models,
            max_tool_calls=base_config.xai.max_tool_calls,
        ),
    )
    tavily = FakeTavily()
    orch = make_orchestrator(cfg, tavily=tavily)
    resp = await orch.search(SearchRequest(query="fallback please", depth="balanced"))
    # with xai unavailable, tavily is group1 conditional and primary_ok false => should run
    assert resp.route.degraded is True
    assert resp.answer == "Tavily answer" or resp.results


@pytest.mark.asyncio
async def test_primary_circuit_open_emits_structured_error(base_config):
    """A configured Grok whose circuit is open must still yield a structured error."""
    cfg = base_config  # xai configured
    orch = make_orchestrator(cfg, xai=FakeXAI(ProviderUnavailableError("down", provider="xai")))
    # Force the circuit into open state so the plan omits xai via availability.
    for _ in range(cfg.reliability.circuit_failure_threshold):
        orch.health_registry.record_failure("xai", "error", retryable=True)
    resp = await orch.search(SearchRequest(query="anything", depth="balanced"))
    assert resp.status == "error"
    assert any(e.code in {"primary_unavailable", "circuit_open"} and e.provider == "xai" for e in resp.errors)


@pytest.mark.asyncio
async def test_anysearch_vertical_cve(base_config):
    anysearch = FakeAnySearch()
    orch = make_orchestrator(base_config, anysearch=anysearch)
    resp = await orch.search(
        SearchRequest(query="Explain CVE-2024-1234 impact", depth="balanced", vertical="security")
    )
    assert anysearch.calls
    assert any(r.provider == "anysearch" or "anysearch" in r.matched_providers for r in resp.results) or resp.results


@pytest.mark.asyncio
async def test_deep_firecrawl_post_discovery(base_config):
    firecrawl = FakeFirecrawl()
    # weak snippets to encourage scrape selection
    xai = FakeXAI(
        ProviderSearchResult(
            provider="xai",
            query="q",
            answer="deep answer",
            results=[
                __import__("allsearch.models", fromlist=["ProviderResultItem"]).ProviderResultItem(
                    title="A", url="https://example.com/a", snippet="", provider="xai"
                ),
                __import__("allsearch.models", fromlist=["ProviderResultItem"]).ProviderResultItem(
                    title="B", url="https://example.org/b", snippet="", provider="xai"
                ),
                __import__("allsearch.models", fromlist=["ProviderResultItem"]).ProviderResultItem(
                    title="C", url="https://docs.example.com/c", snippet="", provider="xai"
                ),
            ],
            citations=[
                {"title": "A", "url": "https://example.com/a"},
                {"title": "B", "url": "https://example.org/b"},
                {"title": "C", "url": "https://docs.example.com/c"},
            ],
        )
    )
    orch = make_orchestrator(base_config, xai=xai, firecrawl=firecrawl)
    resp = await orch.search(SearchRequest(query="deep research topic", depth="deep"))
    assert firecrawl.calls
    assert any(s.provider == "firecrawl" for s in resp.route.stages)


@pytest.mark.asyncio
async def test_fetch_ssrf_blocked(base_config):
    orch = make_orchestrator(base_config)
    resp = await orch.fetch(__import__("allsearch.models", fromlist=["FetchRequest"]).FetchRequest(url="http://127.0.0.1/secret"))
    assert resp.status == "error"
    assert resp.errors


@pytest.mark.asyncio
async def test_health_passive(base_config):
    orch = make_orchestrator(base_config)
    health = await orch.health(probe=False)
    assert health.status in {"ok", "degraded", "unavailable"}
    assert health.model == "grok-4.20"
    names = {p.name for p in health.providers}
    assert {"xai", "tavily", "anysearch", "firecrawl"} <= names
    # no secrets
    blob = health.model_dump()
    assert "test-xai-key" not in str(blob)


@pytest.mark.asyncio
async def test_search_cache_hit(base_config):
    orch = make_orchestrator(base_config)
    req = SearchRequest(query="cache me", depth="balanced")
    first = await orch.search(req)
    second = await orch.search(req)
    assert first.cache.hit is False
    assert second.cache.hit is True
