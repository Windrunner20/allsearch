"""Evidence-driven search: what runs, and why, for queries that state requirements."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from allsearch.models import (
    Comment,
    FetchRequest,
    ProviderResultItem,
    ProviderSearchResult,
    SearchRequest,
    ThreadEvidence,
)
from tests.conftest import FakeTavily, FakeXAI, make_orchestrator


def _primary(urls: list[str], answer: str = "answer") -> ProviderSearchResult:
    return ProviderSearchResult(
        provider="xai",
        query="q",
        answer=answer,
        results=[ProviderResultItem(title=u, url=u, provider="xai") for u in urls],
        citations=[{"title": u, "url": u} for u in urls],
    )


class FakeNative:
    """Stands in for NativeSources; records calls."""

    def __init__(self, threads: dict[str, ThreadEvidence] | None = None, search: dict[str, list[ProviderResultItem]] | None = None) -> None:
        self.threads = threads or {}
        self.results = search or {}
        self.searches: list[tuple[str, str]] = []
        self.reads: list[tuple[str, str]] = []

    def can_search(self, key: str) -> bool:
        return key in self.results

    def can_read(self, platform: str) -> bool:
        return platform in {"hackernews", "v2ex", "stackexchange"}

    async def search(self, key: str, query: str, **_: Any) -> list[ProviderResultItem]:
        self.searches.append((key, query))
        return self.results.get(key, [])

    async def read_thread(self, url: str, *, focus: str, **_: Any) -> ThreadEvidence:
        self.reads.append((url, focus))
        return self.threads.get(url, ThreadEvidence(platform="hackernews", url=url, status="failed", error="not_found"))


async def test_named_site_restricts_primary_and_targets_missing_site(base_config):
    xai = FakeXAI(_primary(["https://zhuanlan.zhihu.com/p/1", "https://sspai.com/post/2", "https://example.org/x"]))
    tavily = FakeTavily(
        ProviderSearchResult(
            provider="tavily",
            query="q",
            results=[ProviderResultItem(title="MacBook 续航", url="https://www.v2ex.com/t/42", snippet="掉电", provider="tavily")],
        )
    )
    orch = make_orchestrator(base_config, xai=xai, tavily=tavily)
    resp = await orch.search(SearchRequest(query="V2EX 上关于 MacBook 续航突然变差的具体主题帖", fresh=True))
    assert xai.calls[0]["include_domains"] == ["v2ex.com", "v2ex.co"]
    # One targeted Tavily call restricted to the site, not the general supplement.
    assert len(tavily.calls) == 1
    assert tavily.calls[0]["include_domains"] == ["v2ex.com", "v2ex.co"]
    assert tavily.calls[0]["query"] == "MacBook 续航突然变差"
    assert resp.results[0].url == "https://www.v2ex.com/t/42"
    assert resp.coverage.status == "sufficient"
    assert any(s.reason == "site:v2ex" for s in resp.route.stages)


async def test_site_covered_by_primary_needs_no_supplement_even_with_one_domain(base_config):
    xai = FakeXAI(_primary(["https://www.v2ex.com/t/1", "https://www.v2ex.com/t/2"]))
    tavily = FakeTavily()
    orch = make_orchestrator(base_config, xai=xai, tavily=tavily)
    resp = await orch.search(SearchRequest(query="V2EX 上关于 MacBook 续航突然变差的具体主题帖", fresh=True))
    assert tavily.calls == []
    assert resp.coverage.status == "sufficient"
    assert not any(w.startswith("primary_insufficient") for w in resp.warnings)


async def test_hacker_news_is_not_a_recency_signal(base_config):
    xai = FakeXAI(_primary(["https://news.ycombinator.com/item?id=1", "https://news.ycombinator.com/item?id=2", "https://news.ycombinator.com/item?id=3"]))
    tavily = FakeTavily()
    orch = make_orchestrator(base_config, xai=xai, tavily=tavily)
    resp = await orch.search(SearchRequest(query="Hacker News discussion of SQLite as an application file format", fresh=True))
    assert tavily.calls == []
    assert "recency_signal" not in resp.route.reasons


async def test_comment_query_reads_threads_and_returns_api_comments(base_config):
    url = "https://news.ycombinator.com/item?id=100"
    thread = ThreadEvidence(
        platform="hackernews",
        url=url,
        thread_id="100",
        title="SQLite as an Application File Format",
        status="comments_read",
        comments_total=2,
        comments=[Comment(id="101", author="a", text="UI must not wait on disk", relevance=3.0)],
    )
    native = FakeNative(
        threads={url: thread},
        search={"hackernews": [ProviderResultItem(title="SQLite as an Application File Format", url=url, provider="hackernews", metadata={"num_comments": 300})]},
    )
    xai = FakeXAI(_primary(["https://sqlite.org/appfileformat.html"]))
    orch = make_orchestrator(base_config, xai=xai, tavily=FakeTavily())
    orch.native = native
    resp = await orch.search(
        SearchRequest(
            query="In the Hacker News thread about SQLite as an application file format, find a comment that says the user interface should not wait on disk",
            fresh=True,
        )
    )
    assert native.searches == [("hackernews", "SQLite as application file format")]
    assert native.reads and native.reads[0][0] == url
    assert "disk" in native.reads[0][1]
    assert resp.threads[0].comments[0].id == "101"
    assert resp.evidence.comments_read == 1
    assert resp.coverage.status == "sufficient"
    assert resp.results[0].title == "SQLite as an Application File Format"
    assert {"tool": "read_result", "fields": ["comments"]}.items() <= resp.next_steps[0].items()


async def test_unreadable_forum_threads_are_reported_as_found_not_read(base_config):
    xai = FakeXAI(_primary(["https://www.zhihu.com/question/123", "https://www.zhihu.com/question/456"]))
    orch = make_orchestrator(base_config, xai=xai, tavily=FakeTavily())
    orch.native = FakeNative()
    resp = await orch.search(SearchRequest(query="知乎上关于 Python GIL 的高赞回答", fresh=True))
    assert [t.status for t in resp.threads] == ["found", "found"]
    assert all("no_public_comment_api" in (t.error or "") for t in resp.threads)
    comments = next(r for r in resp.coverage.requirements if r.kind == "comments")
    assert comments.satisfied is False
    assert any(step["tool"] == "fetch" for step in resp.next_steps)


async def test_answer_links_are_added_after_citations(base_config):
    answer = "The post is https://x.com/OpenAI/status/1635687373060317185 and [docs](https://openai.com/gpt-4)."
    xai = FakeXAI(_primary(["https://x.com/someone/status/1"], answer=answer))
    orch = make_orchestrator(base_config, xai=xai, tavily=FakeTavily())
    resp = await orch.search(SearchRequest(query="Find the public X status from OpenAI that announces GPT-4", fresh=True))
    urls = [r.url for r in resp.results]
    assert "https://x.com/OpenAI/status/1635687373060317185" in urls
    added = next(r for r in resp.results if r.url.endswith("1635687373060317185"))
    assert added.metadata.get("from_answer") is True


async def test_slow_primary_is_capped_and_supplements_still_run(base_config, monkeypatch):
    monkeypatch.setattr("allsearch.orchestrator.MIN_BUDGET_FOR_PRIMARY_CAP", 0.1)
    monkeypatch.setattr("allsearch.orchestrator.PRIMARY_SHARE", 0.3)

    class SlowXAI(FakeXAI):
        async def search(self, query: str, **kwargs: Any) -> ProviderSearchResult:
            self.calls.append(kwargs)
            await asyncio.sleep(5)
            raise AssertionError("not cancelled")

    cfg = replace(base_config, reliability=replace(base_config.reliability, total_budget_seconds=1.5))
    tavily = FakeTavily()
    orch = make_orchestrator(cfg, xai=SlowXAI(), tavily=tavily)
    resp = await orch.search(SearchRequest(query="slow primary", fresh=True))
    assert resp.status == "partial"
    assert any(e.code == "primary_timeout" for e in resp.errors)
    assert any(s.provider == "xai" and s.reason == "primary_time_cap" for s in resp.route.stages)
    assert len(tavily.calls) == 1
    assert resp.results


async def test_fetch_reads_forum_threads_through_the_api(base_config):
    url = "https://news.ycombinator.com/item?id=100"
    thread = ThreadEvidence(platform="hackernews", url=url, title="T", status="comments_read", comments_total=1,
                            comments=[Comment(id="101", author="a", text="hello")], source_api="hn")
    orch = make_orchestrator(base_config)
    orch.native = FakeNative(threads={url: thread})
    resp = await orch.fetch(FetchRequest(url=url, fresh=True))
    assert resp.status == "ok" and resp.provider == "hackernews"
    assert resp.thread.comments[0].id == "101"
    assert "[101] a" in resp.content
    assert orch.firecrawl.calls == []


async def test_fetch_reports_login_walls(base_config, no_dns):
    from allsearch.models import ProviderFetchResult
    from tests.conftest import FakeFirecrawl

    wall = ProviderFetchResult(provider="firecrawl", url="https://e.com/a", final_url="https://e.com/a", content="请登录后查看完整内容")
    orch = make_orchestrator(base_config, firecrawl=FakeFirecrawl(wall))
    resp = await orch.fetch(FetchRequest(url="https://e.com/a", fresh=True))
    assert resp.status == "partial"
    assert resp.errors[0].code == "login_required"


async def test_exact_identifiers_rank_first(base_config):
    urls = [
        "https://www.rfc-editor.org/info/rfc8999",
        "https://datatracker.ietf.org/doc/html/rfc2529.txt",
        "https://www.rfc-editor.org/rfc/rfc791.txt",
    ]
    orch = make_orchestrator(base_config, xai=FakeXAI(_primary(urls)), tavily=FakeTavily())
    resp = await orch.search(SearchRequest(query="RFC 791 text for the Internet Protocol Version field", fresh=True))
    assert resp.results[0].url == "https://www.rfc-editor.org/rfc/rfc791.txt"
    assert resp.citations[0].url == resp.results[0].url and resp.citations[0].result_id == "r1"


async def test_comment_task_on_readable_site_sends_grok_the_thread_topic(base_config):
    url = "https://news.ycombinator.com/item?id=100"
    thread = ThreadEvidence(
        platform="hackernews", url=url, title="SQLite essay", status="comments_read",
        comments=[Comment(id="101", author="lillecarl", url="https://news.ycombinator.com/item?id=101",
                          text="If the user interface depends on synchronous IO something is very wrong.", relevance=4.0)],
    )
    native = FakeNative(threads={url: thread}, search={"hackernews": [ProviderResultItem(title="SQLite as an Application File Format", url=url, provider="hackernews")]})
    xai = FakeXAI(_primary([url], answer="Threads: item 100."))
    orch = make_orchestrator(base_config, xai=xai, tavily=FakeTavily())
    orch.native = native
    resp = await orch.search(SearchRequest(
        query="In the Hacker News thread about SQLite as an application file format, find a comment that says the user interface should not wait on disk",
        fresh=True,
    ))
    assert xai.calls[0]["query"] == "Hacker News threads about: SQLite as application file format"
    assert resp.answer.startswith("Threads: item 100.")
    assert "synchronous IO" in resp.answer and "lillecarl" in resp.answer
    assert "answer_includes_api_comments" in resp.warnings


async def test_bare_citations_get_title_and_snippet_from_the_answer(base_config):
    answer = (
        "Relevant videos:\n\n"
        "1. **[Python GIL 详解](https://www.bilibili.com/video/BV1abc)** 讲解全局解释器锁为什么限制多核。\n"
        "2. Another one https://www.bilibili.com/video/BV2xyz about threads.\n"
        "3. Marker only [1](https://www.bilibili.com/video/BV3num) about the GIL.\n"
    )
    urls = ["https://www.bilibili.com/video/BV1abc", "https://www.bilibili.com/video/BV2xyz", "https://www.bilibili.com/video/BV3num"]
    orch = make_orchestrator(base_config, xai=FakeXAI(_primary(urls, answer=answer)), tavily=FakeTavily())
    resp = await orch.search(SearchRequest(query="B站上介绍 Python GIL 的具体视频", fresh=True))
    first = next(r for r in resp.results if r.url.endswith("BV1abc"))
    assert first.title == "Python GIL 详解"
    assert "全局解释器锁" in first.snippet
    assert first.metadata["snippet_source"] == "answer_context"
    second = next(r for r in resp.results if r.url.endswith("BV2xyz"))
    assert second.title.startswith("https://") and "threads" in second.snippet
    third = next(r for r in resp.results if r.url.endswith("BV3num"))
    assert third.title.startswith("https://") and "GIL" in third.snippet


async def test_capped_primary_falls_back_to_tavily_answer_and_clean_wikipedia_query(base_config, monkeypatch):
    """Regression for wiki-en-tcp in live A/B round 2: Grok stalled past its cap,
    the answer came back empty, and the Wikipedia query kept the word "English"."""
    monkeypatch.setattr("allsearch.orchestrator.MIN_BUDGET_FOR_PRIMARY_CAP", 0.1)
    monkeypatch.setattr("allsearch.orchestrator.PRIMARY_SHARE", 0.3)

    class StalledXAI(FakeXAI):
        async def search(self, query: str, **kwargs: Any) -> ProviderSearchResult:
            self.calls.append(kwargs)
            await asyncio.sleep(5)
            raise AssertionError("not cancelled")

    native = FakeNative(search={"wikipedia": [ProviderResultItem(title="HTTP", url="https://en.wikipedia.org/wiki/HTTP", provider="wikipedia")]})
    tavily = FakeTavily(ProviderSearchResult(provider="tavily", query="q", answer="HTTP uses TCP port 80 by default.",
                                             results=[ProviderResultItem(title="HTTP", url="https://en.wikipedia.org/wiki/HTTP", provider="tavily")]))
    cfg = replace(base_config, reliability=replace(base_config.reliability, total_budget_seconds=1.5))
    orch = make_orchestrator(cfg, xai=StalledXAI(), tavily=tavily)
    orch.native = native
    resp = await orch.search(SearchRequest(query="English Wikipedia HTTP article: the default TCP port", fresh=True))
    assert native.searches == [("wikipedia", "HTTP default TCP port")]
    assert resp.answer == "HTTP uses TCP port 80 by default."
    assert "answer_from_tavily_primary_timeout" in resp.warnings
    assert any(r.url == "https://en.wikipedia.org/wiki/HTTP" for r in resp.results)


async def test_include_domains_on_known_site_uses_native_search(base_config):
    url = "https://news.ycombinator.com/item?id=100"
    native = FakeNative(search={"hackernews": [ProviderResultItem(title="SQLite as an Application File Format", url=url, provider="hackernews")]})
    orch = make_orchestrator(base_config, xai=FakeXAI(_primary([url])), tavily=FakeTavily())
    orch.native = native
    await orch.search(SearchRequest(query="SQLite application file format", include_domains=["news.ycombinator.com"], fresh=True))
    assert native.searches == [("hackernews", "SQLite application file format")]
