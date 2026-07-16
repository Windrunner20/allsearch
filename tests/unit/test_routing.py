from __future__ import annotations

from allsearch.models import SearchRequest
from allsearch.routing import build_execution_plan, classify_query, tavily_required_after_primary


def test_classify_cve_vertical(base_config):
    req = SearchRequest(query="details for CVE-2024-1234")
    signals = classify_query(req, base_config.anysearch.verticals)
    assert "security" in signals.verticals


def test_fast_plan_conditional_tavily(base_config):
    req = SearchRequest(query="hello world", depth="fast")
    plan = build_execution_plan(req, base_config)
    providers = [(s.provider, s.parallel_group) for s in plan.stages]
    assert ("xai", 0) in providers
    assert ("tavily", 1) in providers
    # Grok is the only group-0 stage
    assert all(grp == 0 for prov, grp in providers if prov == "xai")
    assert all(grp != 0 for prov, grp in providers if prov != "xai")
    assert plan.scrape_after_merge is False


def test_verify_plan_tavily_in_group1_after_primary(base_config):
    req = SearchRequest(query="latest news about AI chips", depth="verify", mode="news")
    plan = build_execution_plan(req, base_config)
    xai = [s for s in plan.stages if s.provider == "xai"]
    tavily = [s for s in plan.stages if s.provider == "tavily"]
    # Grok is the ONLY group-0 stage
    assert xai and xai[0].parallel_group == 0
    assert all(s.parallel_group == 1 for s in plan.stages if s.provider != "xai")
    # Tavily is planned as a post-primary supplement
    assert tavily and tavily[0].parallel_group == 1
    assert plan.scrape_after_merge is True


def test_deep_scrape_budget(base_config):
    req = SearchRequest(query="research quantum computing", depth="deep")
    plan = build_execution_plan(req, base_config)
    assert plan.scrape_budget >= 1


def test_tavily_required_matrix():
    from allsearch.routing import QuerySignals

    signals = QuerySignals(recency=False)
    assert tavily_required_after_primary(
        depth="balanced",
        primary_ok=True,
        primary_sufficient=True,
        signals=signals,
        tavily_planned=True,
    ) is False
    assert tavily_required_after_primary(
        depth="balanced",
        primary_ok=True,
        primary_sufficient=False,
        signals=signals,
        tavily_planned=True,
    ) is True
    assert tavily_required_after_primary(
        depth="verify",
        primary_ok=True,
        primary_sufficient=True,
        signals=signals,
        tavily_planned=True,
    ) is True  # verify always cross-checks with Tavily
    assert tavily_required_after_primary(
        depth="deep",
        primary_ok=True,
        primary_sufficient=True,
        signals=signals,
        tavily_planned=True,
    ) is True  # deep always gathers supplemental web evidence
    assert tavily_required_after_primary(
        depth="verify",
        primary_ok=False,
        primary_sufficient=False,
        signals=signals,
        tavily_planned=True,
    ) is True  # group-1 supplement path: run when primary failed


def test_anysearch_on_explicit_vertical(base_config):
    req = SearchRequest(query="market cap of AAPL", depth="balanced", vertical="finance")
    plan = build_execution_plan(req, base_config)
    assert any(s.provider == "anysearch" for s in plan.stages)
