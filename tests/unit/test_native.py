from __future__ import annotations

import httpx
import pytest
import respx

from allsearch.cache import MemoryCache
from allsearch.health import HealthRegistry
from allsearch.models import Comment
from allsearch.native import (
    NativeSources,
    canonical_thread_url,
    html_to_text,
    rank_comments,
    render_thread,
    thread_ref,
)
from allsearch.transport import HttpTransport

HN_ITEM = {
    "id": 100,
    "type": "story",
    "title": "SQLite as an Application File Format",
    "author": "op",
    "created_at": "2020-06-13T12:28:53.000Z",
    "url": "https://sqlite.org/appfileformat.html",
    "text": None,
    "children": [
        {
            "id": 101,
            "parent_id": 100,
            "author": "a",
            "created_at": "2020-06-13T13:00:00.000Z",
            "text": "<p>If the user interface depends on synchronous IO something is very wrong.</p>",
            "children": [
                {"id": 103, "parent_id": 101, "author": "c", "created_at": "2020-06-13T14:00:00.000Z", "text": "Agreed &amp; more", "children": []}
            ],
        },
        {"id": 102, "parent_id": 100, "author": "b", "created_at": "2020-06-13T13:30:00.000Z", "text": "Zip files are fine.", "children": []},
        {"id": 104, "parent_id": 100, "author": None, "text": None, "children": []},
    ],
}


@pytest.fixture
async def native():
    transport = HttpTransport(timeout_seconds=5, max_retries=0)
    sources = NativeSources(transport, HealthRegistry(), MemoryCache())
    yield sources
    await transport.aclose()


def test_thread_refs_and_canonical_urls():
    assert thread_ref("https://news.ycombinator.com/item?id=23508923") == ("hackernews", "23508923")
    assert thread_ref("https://hk.v2ex.com/t/910789?p=2#reply3") == ("v2ex", "910789")
    assert thread_ref("https://stackoverflow.com/questions/927358/how-do-i") == ("stackexchange", "stackoverflow:927358")
    assert thread_ref("https://www.reddit.com/r/rust/comments/abc12/title/") == ("reddit", "abc12")
    assert thread_ref("https://www.v2ex.com/member/x") is None
    assert canonical_thread_url("https://cn.v2ex.com/t/5?p=1") == "https://www.v2ex.com/t/5"


def test_html_to_text():
    raw = '<p>One &amp; two</p><p>See <a href="https://x.y/z">docs</a><br>line</p>'
    assert html_to_text(raw) == "One & two\n\nSee docs\nline"


def test_rank_comments_prefers_matching_terms_and_keeps_order_without_focus():
    comments = [Comment(id=str(i), text=t) for i, t in enumerate(["zip files", "ui waits on synchronous disk io", "the ui is fine"])]
    ranked = rank_comments(comments, "interface wait disk", limit=2)
    assert ranked[0].id == "1" and ranked[0].relevance > 0
    assert [c.id for c in rank_comments(comments, "", limit=2)] == ["0", "1"]


@respx.mock
async def test_hn_thread_read_returns_comment_ids_authors_and_parents(native):
    respx.get("https://hn.algolia.com/api/v1/items/100").mock(return_value=httpx.Response(200, json=HN_ITEM))
    thread = await native.read_thread("https://news.ycombinator.com/item?id=100", focus="user interface synchronous", deadline=5)
    assert thread.status == "comments_read"
    assert thread.comments_total == 3
    first = thread.comments[0]
    assert (first.id, first.author, first.parent_id) == ("101", "a", "100")
    assert first.text.startswith("If the user interface")
    assert first.url == "https://news.ycombinator.com/item?id=101"
    reply = next(c for c in thread.comments if c.id == "103")
    assert reply.depth == 1 and reply.parent_id == "101" and reply.text == "Agreed & more"
    assert "## Comments" in render_thread(thread)


@respx.mock
async def test_v2ex_and_stackexchange_reads(native):
    respx.get("https://www.v2ex.com/api/topics/show.json?id=5").mock(
        return_value=httpx.Response(200, json=[{"id": 5, "title": "续航", "content": "正文", "created": 1700000000, "replies": 1, "member": {"username": "op"}}])
    )
    respx.get("https://www.v2ex.com/api/replies/show.json?topic_id=5").mock(
        return_value=httpx.Response(200, json=[{"id": 9, "content": "换电池", "created": 1700000100, "member": {"username": "r"}}])
    )
    thread = await native.read_thread("https://cn.v2ex.com/t/5", focus="", deadline=5)
    assert thread.status == "comments_read"
    assert thread.url == "https://www.v2ex.com/t/5"
    assert thread.comments[0].url == "https://www.v2ex.com/t/5#r_9"
    assert thread.comments[0].created_at == "2023-11-14T22:15:00Z"

    respx.get(url__regex=r"https://api\.stackexchange\.com/2\.3/questions/7\?.*").mock(
        return_value=httpx.Response(200, json={"items": [{"question_id": 7, "title": "Undo &quot;commit&quot;", "link": "https://stackoverflow.com/questions/7/undo", "body": "<p>How?</p>", "answer_count": 1}]})
    )
    respx.get(url__regex=r"https://api\.stackexchange\.com/2\.3/questions/7/answers\?.*").mock(
        return_value=httpx.Response(200, json={"items": [{"answer_id": 8, "is_accepted": True, "score": 10, "body": "<pre>git reset HEAD~</pre>", "owner": {"display_name": "Esko"}}]})
    )
    se = await native.read_thread("https://stackoverflow.com/questions/7/undo", focus="", deadline=5)
    assert se.title == 'Undo "commit"'
    assert se.comments[0].author == "Esko [accepted]" and "git reset" in se.comments[0].text


@respx.mock
async def test_blocked_and_unconfigured_reads_are_explicit(native):
    respx.get("https://www.v2ex.com/api/topics/show.json?id=6").mock(return_value=httpx.Response(403))
    blocked = await native.read_thread("https://www.v2ex.com/t/6", focus="", deadline=5)
    assert blocked.status == "blocked" and blocked.error == "auth_error"
    reddit = await native.read_thread("https://www.reddit.com/r/rust/comments/abc/x", focus="", deadline=5)
    assert reddit.status == "found" and reddit.error == "reddit_oauth_not_configured"
    assert not native.can_search("reddit")


@respx.mock
async def test_native_search_results_have_titles_and_dates(native):
    respx.get(url__regex=r"https://hn\.algolia\.com/api/v1/search\?.*").mock(
        return_value=httpx.Response(200, json={"hits": [{"objectID": "100", "title": "SQLite as an Application File Format", "points": 5, "num_comments": 3, "author": "op", "created_at": "2020-06-13T12:28:53Z", "url": "https://sqlite.org/x"}]})
    )
    respx.get(url__regex=r"https://www\.sov2ex\.com/api/search\?.*").mock(
        return_value=httpx.Response(200, json={"hits": [{"_source": {"id": 9, "title": "OrbStack 体验", "content": "替代 Docker Desktop", "created": "2023-01-02T03:04:05"}}]})
    )
    respx.get(url__regex=r"https://zh\.wikipedia\.org/w/rest\.php/v1/search/page\?.*").mock(
        return_value=httpx.Response(200, json={"pages": [{"key": "超文本传输协议", "title": "超文本传输协议", "excerpt": "<span>HTTP</span> 默认端口 80"}]})
    )
    hn = await native.search("hackernews", "sqlite file format", limit=3, deadline=5)
    assert hn[0].url == "https://news.ycombinator.com/item?id=100" and hn[0].published_at.startswith("2020")
    v2 = await native.search("v2ex", "OrbStack", limit=3, deadline=5)
    assert v2[0].url == "https://www.v2ex.com/t/9" and v2[0].title == "OrbStack 体验"
    wiki = await native.search("wikipedia", "HTTP", limit=3, deadline=5, lang="zh")
    assert wiki[0].url.startswith("https://zh.wikipedia.org/wiki/%E8%B6%85") and "80" in wiki[0].snippet
