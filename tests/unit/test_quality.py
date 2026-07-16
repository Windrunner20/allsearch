from __future__ import annotations

from allsearch.models import ProviderResultItem, ProviderSearchResult, ResultItem
from allsearch.quality import content_quality_issue, evaluate_primary_sufficiency, select_urls_for_scrape


def test_insufficient_without_citations(base_config):
    primary = ProviderSearchResult(provider="xai", query="q", answer="long " * 100, results=[], citations=[])
    result = evaluate_primary_sufficiency(primary, base_config, depth="balanced")
    assert result.sufficient is False
    assert any("citations" in r or "long_answer" in r for r in result.reasons)


def test_sufficient_with_domains(base_config):
    primary = ProviderSearchResult(
        provider="xai",
        query="q",
        answer="ok",
        results=[
            ProviderResultItem(title="a", url="https://a.com/1", provider="xai"),
            ProviderResultItem(title="b", url="https://b.com/2", provider="xai"),
            ProviderResultItem(title="c", url="https://c.com/3", provider="xai"),
        ],
        citations=[
            {"title": "a", "url": "https://a.com/1"},
            {"title": "b", "url": "https://b.com/2"},
            {"title": "c", "url": "https://c.com/3"},
        ],
    )
    result = evaluate_primary_sufficiency(primary, base_config, depth="balanced")
    assert result.sufficient is True


def test_select_urls_prefers_empty_snippet():
    results = [
        ResultItem(id="r1", title="1", url="https://example.com/1", snippet="has", provider="xai"),
        ResultItem(id="r2", title="2", url="https://example.com/2", snippet="", provider="xai"),
    ]
    urls = select_urls_for_scrape(results, budget=1)
    assert urls == ["https://example.com/2"]


def test_content_quality_markers():
    assert content_quality_issue("") == "empty_content"
    assert content_quality_issue("please complete the captcha challenge to continue browsing this site now") == "anti_bot_shell"
