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
async def test_verify_runs_tavily_after_primary(base_config, no_dns):
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
async def test_deep_firecrawl_post_discovery(base_config, no_dns):
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


# ---------------------------------------------------------------------------
# v0.2.0: strict gate, concurrent group 1, hard deadline, auto-scrape, health
# ---------------------------------------------------------------------------


def _unconfigured_xai_cfg(base_config):
    from dataclasses import replace

    from allsearch.config import XAIConfig

    return replace(
        base_config,
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


@pytest.mark.asyncio
async def test_strict_primary_missing_does_not_call_group1(base_config):
    from allsearch.models import SearchRequest

    cfg = _unconfigured_xai_cfg(base_config)
    tavily = FakeTavily()
    anysearch = FakeAnySearch()
    orch = make_orchestrator(cfg, xai=FakeXAI(), tavily=tavily, anysearch=anysearch)
    resp = await orch.search(
        SearchRequest(query="CVE-2024-1234 details", depth="verify", vertical="security")
    )
    assert resp.status == "error"
    # Tavily / AnySearch must never be called.
    assert not tavily.calls
    assert not anysearch.calls
    # Every planned group-1 stage is recorded as skipped/primary_unavailable.
    group1 = [s for s in resp.route.stages if s.provider in {"tavily", "anysearch"}]
    assert group1
    assert all(s.outcome == "skipped" and s.reason == "primary_unavailable" for s in group1)
    assert any(e.code == "primary_unavailable" for e in resp.errors)


@pytest.mark.asyncio
async def test_group1_concurrent_and_deterministic_order(base_config):
    from allsearch.models import SearchRequest
    from allsearch.routing import build_execution_plan

    xai = FakeXAI()
    tavily = FakeTavily()
    anysearch = FakeAnySearch()
    orch = make_orchestrator(base_config, xai=xai, tavily=tavily, anysearch=anysearch)
    req = SearchRequest(query="CVE-2024-1234 details", depth="verify", vertical="security")
    resp = await orch.search(req)
    # Both group-1 supplements were actually invoked.
    assert tavily.calls
    assert anysearch.calls
    # Route stage order matches the plan's group-1 order (deterministic).
    plan = build_execution_plan(req, base_config)
    expected = [s.provider for s in plan.stages if s.parallel_group == 1]
    actual = [
        s.provider for s in resp.route.stages if s.provider in {"tavily", "anysearch"}
    ]
    assert actual == expected
    assert expected == ["tavily", "anysearch"]


@pytest.mark.asyncio
async def test_one_group1_failure_does_not_block_other(base_config):
    from allsearch.errors import TransportError
    from allsearch.models import SearchRequest

    xai = FakeXAI()
    tavily = FakeTavily(TransportError("tavily down", retryable=True))
    anysearch = FakeAnySearch()
    orch = make_orchestrator(base_config, xai=xai, tavily=tavily, anysearch=anysearch)
    resp = await orch.search(
        SearchRequest(query="CVE-2024-1234 details", depth="verify", vertical="security")
    )
    assert anysearch.calls  # not blocked by tavily failure
    assert any(s.provider == "tavily" and s.outcome == "error" for s in resp.route.stages)
    assert any(s.provider == "anysearch" and s.outcome == "ok" for s in resp.route.stages)
    assert resp.status in {"ok", "partial"}


@pytest.mark.asyncio
async def test_hard_deadline_phase_no_hang_partial_no_cache_no_dup(base_config, loop_clock):
    import asyncio

    from allsearch.health import HealthRegistry
    from allsearch.models import SearchRequest

    health = HealthRegistry(failure_threshold=3, open_seconds=60)
    xai = FakeXAI()  # completes instantly (primary done -> partial)
    clock = loop_clock

    class HangingTavily:
        name = "tavily"

        def __init__(self, health) -> None:
            self.health = health
            self.calls: list[str] = []

        def configured(self) -> bool:
            return True

        async def search(self, query: str, **kwargs) -> None:
            self.calls.append(query)
            clock.advance(1_000.0)  # blow past the shared deadline
            try:
                await asyncio.sleep(60)  # would hang forever in real time
            except asyncio.CancelledError:
                raise
            except Exception:
                self.health.record_failure("tavily", "boom", retryable=True)
                raise

    tavily = HangingTavily(health)
    orch = make_orchestrator(base_config, health=health, xai=xai, tavily=tavily)
    resp = await orch.search(SearchRequest(query="deadline test", depth="verify"))
    # verify always needs tavily; it hangs -> phase 1 hard-times-out.
    assert resp.status == "partial"
    assert xai.calls and tavily.calls
    # Exactly one structured deadline_exceeded error (no duplicates).
    dl = [e for e in resp.errors if e.code == "deadline_exceeded"]
    assert len(dl) == 1
    # Cancelled task did not record a provider circuit failure.
    assert health.get("tavily").consecutive_failures == 0
    # Timed-out responses are not cached.
    second = await orch.search(SearchRequest(query="deadline test", depth="verify"))
    assert second.cache.hit is False


@pytest.mark.asyncio
async def test_deadline_exhausted_between_phases_is_reported_and_not_cached(
    base_config, monkeypatch
):
    """If the shared budget is exhausted after primary completion but before
    group 1 starts, the skipped phase must still produce one deadline error and
    the incomplete response must not be cached."""
    from allsearch.models import SearchRequest

    class _NoopTimeout:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class BoundaryDeadline:
        def __init__(self, total_seconds: float) -> None:
            self.expired_checks = 0

        @property
        def remaining(self) -> float:
            return 1.0

        @property
        def expired(self) -> bool:
            self.expired_checks += 1
            return self.expired_checks >= 2

        @property
        def timeout(self):
            return _NoopTimeout()

    monkeypatch.setattr("allsearch.orchestrator.Deadline", BoundaryDeadline)
    tavily = FakeTavily()
    orch = make_orchestrator(base_config, xai=FakeXAI(), tavily=tavily)
    req = SearchRequest(query="phase boundary deadline", depth="verify")
    resp = await orch.search(req)

    assert resp.status == "partial"
    assert not tavily.calls
    assert any(
        s.provider == "tavily"
        and s.outcome == "skipped"
        and s.reason == "deadline_exceeded"
        for s in resp.route.stages
    )
    dl = [e for e in resp.errors if e.code == "deadline_exceeded"]
    assert len(dl) == 1 and dl[0].provider == "allsearch"
    assert (await orch.search(req)).cache.hit is False


@pytest.mark.asyncio
async def test_hard_deadline_strict_primary_incomplete_error(base_config, loop_clock):
    import asyncio

    from allsearch.models import SearchRequest

    clock = loop_clock

    class HangingXAI:
        name = "xai"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def configured(self) -> bool:
            return True

        async def search(self, query: str, **kwargs) -> None:
            self.calls.append(query)
            clock.advance(1_000.0)
            await asyncio.sleep(60)

    xai = HangingXAI()
    tavily = FakeTavily()
    orch = make_orchestrator(base_config, xai=xai, tavily=tavily)
    resp = await orch.search(SearchRequest(query="deadline strict", depth="verify"))
    assert resp.status == "error"
    dl = [e for e in resp.errors if e.code == "deadline_exceeded"]
    assert len(dl) == 1
    # Group 1 must never have started after the phase-0 timeout.
    assert not tavily.calls
    assert resp.cache.hit is False


@pytest.mark.asyncio
async def test_auto_scrape_validates_original_and_final_dns(base_config, no_dns):
    from allsearch.models import ProviderFetchResult, SearchRequest

    class RedirectingFirecrawl:
        name = "firecrawl"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def configured(self) -> bool:
            return True

        async def scrape(self, url: str, **kwargs):
            self.calls.append(url)
            return ProviderFetchResult(
                provider="firecrawl",
                url=url,
                final_url="https://redirected.example/x",
                title="T",
                content="real body content " * 20,
            )

    fc = RedirectingFirecrawl()
    orch = make_orchestrator(base_config, xai=FakeXAI(), firecrawl=fc)
    resp = await orch.search(SearchRequest(query="deep research topic", depth="deep"))
    assert fc.calls
    assert resp.status in {"ok", "partial"}
    origins = [u for u, _ in no_dns]
    # Both the original scraped URL and the final_url were validated.
    assert any(u.startswith("https://example.com") for u in origins)
    assert "https://redirected.example/x" in origins
    # All validations requested DNS resolution.
    assert all(resolve for _, resolve in no_dns)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,reason",
    [
        ("", "empty_content"),
        ("tiny", "content_too_short"),
        ("please complete the captcha challenge to continue browsing now", "anti_bot_shell"),
    ],
)
async def test_auto_scrape_rejects_low_quality(base_config, no_dns, content, reason):
    from allsearch.models import ProviderFetchResult, SearchRequest

    class BadFirecrawl:
        name = "firecrawl"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def configured(self) -> bool:
            return True

        async def scrape(self, url: str, **kwargs):
            self.calls.append(url)
            return ProviderFetchResult(
                provider="firecrawl", url=url, final_url=url, title="T", content=content
            )

    fc = BadFirecrawl()
    orch = make_orchestrator(base_config, xai=FakeXAI(), firecrawl=fc)
    resp = await orch.search(SearchRequest(query="deep research topic", depth="deep"))
    assert fc.calls
    # Rejected: not attached, no pages_fetched, stage partial with the reason.
    assert resp.evidence.pages_fetched == 0
    assert all(r.content is None for r in resp.results)
    assert any(
        s.provider == "firecrawl" and s.outcome == "partial" and s.detail == reason
        for s in resp.route.stages
    )
    assert any(w == f"auto_scrape_rejected:{reason}" for w in resp.warnings)


@pytest.mark.asyncio
async def test_auto_scrape_truncates_to_30000(base_config, no_dns):
    from allsearch.models import ProviderFetchResult, SearchRequest
    from allsearch.orchestrator import AUTO_SCRAPE_MAX_CHARS

    big = "x" * (AUTO_SCRAPE_MAX_CHARS + 10_000)

    class BigFirecrawl:
        name = "firecrawl"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def configured(self) -> bool:
            return True

        async def scrape(self, url: str, **kwargs):
            self.calls.append(url)
            return ProviderFetchResult(
                provider="firecrawl", url=url, final_url=url, title="T", content=big
            )

    fc = BigFirecrawl()
    orch = make_orchestrator(base_config, xai=FakeXAI(), firecrawl=fc)
    resp = await orch.search(SearchRequest(query="deep research topic", depth="deep"))
    assert fc.calls
    attached = [r.content for r in resp.results if r.content]
    assert attached
    assert all(len(c) == AUTO_SCRAPE_MAX_CHARS for c in attached)
    assert resp.evidence.pages_fetched == len(fc.calls)
    assert "auto_scrape_truncated" in resp.warnings


@pytest.mark.asyncio
async def test_explicit_fetch_validates_final_url_and_returns_short(base_config, no_dns):
    from allsearch.models import FetchRequest, ProviderFetchResult

    class RedirectingFirecrawl:
        name = "firecrawl"

        def configured(self) -> bool:
            return True

        async def scrape(self, url: str, **kwargs):
            return ProviderFetchResult(
                provider="firecrawl",
                url=url,
                final_url="https://redirected.example/x",
                title="T",
                content="short body",  # short content is still returned explicitly
            )

    orch = make_orchestrator(base_config, firecrawl=RedirectingFirecrawl())
    resp = await orch.fetch(FetchRequest(url="https://example.com/page"))
    assert resp.status == "ok"
    assert resp.content == "short body"
    assert any(u == "https://example.com/page" for u, _ in no_dns)
    assert any(u == "https://redirected.example/x" for u, _ in no_dns)
    assert all(resolve for _, resolve in no_dns)


@pytest.mark.asyncio
async def test_health_open_degrades_with_warning(base_config):
    orch = make_orchestrator(base_config)
    for _ in range(base_config.reliability.circuit_failure_threshold):
        orch.health_registry.record_failure("xai", "timeout", retryable=True)
    health = await orch.health(probe=False)
    assert health.status == "degraded"
    assert "primary_circuit_open" in health.warnings


@pytest.mark.asyncio
async def test_health_idle_is_ok(base_config):
    orch = make_orchestrator(base_config)
    health = await orch.health(probe=False)
    assert health.status == "ok"


@pytest.mark.asyncio
async def test_domain_filter_constrains_final_results_and_scrape(base_config, no_dns):
    from allsearch.models import ProviderResultItem, ProviderSearchResult, SearchRequest

    xai = FakeXAI(
        ProviderSearchResult(
            provider="xai",
            query="q",
            answer="ans",
            results=[
                ProviderResultItem(title="A", url="https://example.com/a", provider="xai"),
                ProviderResultItem(title="B", url="https://other.org/b", provider="xai"),
            ],
            citations=[
                {"title": "A", "url": "https://example.com/a"},
                {"title": "B", "url": "https://other.org/b"},
            ],
        )
    )
    firecrawl = FakeFirecrawl()
    orch = make_orchestrator(base_config, xai=xai, firecrawl=firecrawl)
    resp = await orch.search(
        SearchRequest(query="deep topic", depth="deep", include_domains=["example.com"])
    )
    # Final structured results + citations constrained to the include filter.
    assert all("example.com" in r.url for r in resp.results)
    assert all("example.com" in c.url for c in resp.citations)
    # Auto-scrape only targets filtered (allowed) URLs.
    assert firecrawl.calls
    assert all(u.startswith("https://example.com") for u in firecrawl.calls)
    assert resp.evidence.unique_domains == 1


# ---------------------------------------------------------------------------
# v0.2.0 regression: partial completion under deadline must stay consistent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group1_partial_completion_on_deadline(base_config, loop_clock):
    """One group-1 provider completes before the deadline, another hangs: the
    completed result/route/warning must be preserved, the hung one skipped, and
    exactly one deadline error with provider='allsearch' emitted."""
    import asyncio

    from allsearch.models import ProviderResultItem, ProviderSearchResult, SearchRequest

    clock = loop_clock

    class HangingTavily:
        name = "tavily"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def configured(self) -> bool:
            return True

        async def search(self, query: str, **kwargs) -> None:
            self.calls.append(query)
            clock.advance(1_000.0)
            await asyncio.sleep(60)

    class InstantAnySearch:
        name = "anysearch"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def configured(self) -> bool:
            return True

        async def search(self, query: str, **kwargs):
            self.calls.append(query)
            return ProviderSearchResult(
                provider="anysearch",
                query=query,
                results=[
                    ProviderResultItem(
                        title="v",
                        url="https://nvd.nist.gov/vuln/detail/CVE-2024-1234",
                        snippet="s",
                        provider="anysearch",
                        source_type="vertical",
                        vertical=kwargs.get("domain"),
                    )
                ],
                citations=[
                    {"title": "v", "url": "https://nvd.nist.gov/vuln/detail/CVE-2024-1234"}
                ],
                raw_warnings=["anysearch_partial_warning"],
            )

    orch = make_orchestrator(
        base_config,
        xai=FakeXAI(),
        tavily=HangingTavily(),
        anysearch=InstantAnySearch(),
    )
    resp = await orch.search(
        SearchRequest(query="CVE-2024-1234 details", depth="verify", vertical="security")
    )
    # Completed supplement is preserved end-to-end: route ok + result + warning.
    assert any(s.provider == "anysearch" and s.outcome == "ok" for s in resp.route.stages)
    assert any("nvd.nist.gov" in r.url for r in resp.results)
    assert "anysearch_partial_warning" in resp.warnings
    # Partial timeout still preserves the execution-plan order.
    assert [s.provider for s in resp.route.stages if s.provider in {"tavily", "anysearch"}] == [
        "tavily",
        "anysearch",
    ]
    # Hung supplement is marked skipped, not error.
    assert any(
        s.provider == "tavily"
        and s.outcome == "skipped"
        and s.reason == "deadline_exceeded"
        for s in resp.route.stages
    )
    # Exactly one structured deadline error with provider 'allsearch'.
    dl = [e for e in resp.errors if e.code == "deadline_exceeded"]
    assert len(dl) == 1
    assert dl[0].provider == "allsearch"
    assert resp.status == "partial"


@pytest.mark.asyncio
async def test_auto_scrape_partial_completion_on_deadline(base_config, no_dns, loop_clock):
    """One scrape completes before the deadline, another hangs: the completed
    page is attached + counted + staged ok, the hung URL is staged skipped, and
    exactly one deadline error with provider='allsearch' is emitted."""
    import asyncio

    from allsearch.models import ProviderFetchResult, SearchRequest

    clock = loop_clock
    HANG_URL = "https://example.org/b"

    class PartialFirecrawl:
        name = "firecrawl"

        def __init__(self) -> None:
            self.calls: list[str] = []

        def configured(self) -> bool:
            return True

        async def scrape(self, url: str, **kwargs):
            self.calls.append(url)
            if url == HANG_URL:
                clock.advance(1_000.0)
                await asyncio.sleep(60)
            return ProviderFetchResult(
                provider="firecrawl",
                url=url,
                final_url=url,
                title="T",
                content="good body content " * 10,
            )

    fc = PartialFirecrawl()
    orch = make_orchestrator(base_config, xai=FakeXAI(), firecrawl=fc)
    resp = await orch.search(SearchRequest(query="deep research topic", depth="deep"))
    assert fc.calls
    # Completed scrapes attached + counted; hung URL not attached.
    assert resp.evidence.pages_fetched == 2
    attached = [r.url for r in resp.results if r.content]
    assert "https://example.com/a" in attached
    assert "https://docs.example.com/c" in attached
    assert HANG_URL not in attached
    # Route: two ok firecrawl stages + one skipped/deadline_exceeded.
    oks = [
        s for s in resp.route.stages if s.provider == "firecrawl" and s.outcome == "ok"
    ]
    skips = [
        s
        for s in resp.route.stages
        if s.provider == "firecrawl"
        and s.outcome == "skipped"
        and s.reason == "deadline_exceeded"
    ]
    assert len(oks) == 2
    assert len(skips) == 1
    # Exactly one deadline error with provider 'allsearch'.
    dl = [e for e in resp.errors if e.code == "deadline_exceeded"]
    assert len(dl) == 1
    assert dl[0].provider == "allsearch"


@pytest.mark.asyncio
async def test_group1_overlap_proven_with_active_counter(base_config):
    """Prove group-1 concurrency: both providers must be simultaneously in
    flight (active count == 2), not merely both eventually called."""
    import asyncio

    from allsearch.models import ProviderResultItem, ProviderSearchResult, SearchRequest

    go = asyncio.Event()
    active = [0]
    max_active = [0]

    class OverlapProvider:
        def __init__(self, name: str, url: str) -> None:
            self.name = name
            self.url = url
            self.calls: list[str] = []

        def configured(self) -> bool:
            return True

        async def search(self, query: str, **kwargs):
            self.calls.append(query)
            active[0] += 1
            max_active[0] = max(max_active[0], active[0])
            await go.wait()
            active[0] -= 1
            return ProviderSearchResult(
                provider=self.name,
                query=query,
                results=[
                    ProviderResultItem(
                        title=self.name, url=self.url, snippet="s", provider=self.name
                    )
                ],
                citations=[{"title": self.name, "url": self.url}],
            )

    tavily = OverlapProvider("tavily", "https://example.com/a")
    anysearch = OverlapProvider("anysearch", "https://nvd.nist.gov/x")
    orch = make_orchestrator(
        base_config, xai=FakeXAI(), tavily=tavily, anysearch=anysearch
    )
    search_task = asyncio.ensure_future(
        orch.search(
            SearchRequest(
                query="CVE-2024-1234 details", depth="verify", vertical="security"
            )
        )
    )
    try:
        # Wait until both group-1 providers are concurrently in flight.
        for _ in range(500):
            if active[0] == 2:
                break
            await asyncio.sleep(0)
        assert active[0] == 2, f"group-1 providers did not overlap (active={active[0]})"
        assert max_active[0] == 2
    finally:
        go.set()
    resp = await asyncio.wait_for(search_task, timeout=5)
    assert resp.status in {"ok", "partial"}
    assert tavily.calls and anysearch.calls
