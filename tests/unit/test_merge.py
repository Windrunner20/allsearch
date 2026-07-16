from __future__ import annotations

from allsearch.merge import merge_provider_results
from allsearch.models import ProviderResultItem, ProviderSearchResult


def test_merge_dedupes_and_keeps_provenance():
    xai = ProviderSearchResult(
        provider="xai",
        query="q",
        answer="ans",
        results=[
            ProviderResultItem(title="A", url="https://example.com/a?utm_source=x", snippet="s", provider="xai"),
            ProviderResultItem(title="B", url="https://example.org/b", snippet="s", provider="xai"),
        ],
        citations=[
            {"title": "A", "url": "https://example.com/a?utm_source=x"},
            {"title": "B", "url": "https://example.org/b"},
        ],
    )
    tavily = ProviderSearchResult(
        provider="tavily",
        query="q",
        results=[
            ProviderResultItem(
                title="A better",
                url="https://example.com/a",
                snippet="richer snippet content",
                provider="tavily",
            ),
            ProviderResultItem(title="C", url="https://news.example.net/c", snippet="n", provider="tavily"),
        ],
        citations=[
            {"title": "A better", "url": "https://example.com/a"},
            {"title": "C", "url": "https://news.example.net/c"},
        ],
    )
    results, citations, evidence = merge_provider_results([xai, tavily], max_results=5, primary_provider="xai")
    urls = [r.url for r in results]
    assert "https://example.com/a" in urls or any("example.com/a" in u for u in urls)
    matched = next(r for r in results if "example.com/a" in r.url)
    assert set(matched.matched_providers) >= {"xai", "tavily"}
    assert matched.snippet == "richer snippet content"
    assert evidence.cross_provider_matches >= 1
    assert evidence.pages_fetched == 0
    assert citations


def test_provider_content_is_not_counted_as_firecrawl_fetch():
    anysearch = ProviderSearchResult(
        provider="anysearch",
        query="q",
        results=[
            ProviderResultItem(
                title="Vertical result",
                url="https://example.com/vertical",
                snippet="snippet",
                content="provider supplied content",
                provider="anysearch",
                source_type="vertical",
            )
        ],
    )
    _, _, evidence = merge_provider_results([anysearch], max_results=5)
    assert evidence.pages_fetched == 0
