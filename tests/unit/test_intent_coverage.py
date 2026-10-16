from __future__ import annotations

from datetime import datetime, timezone

import pytest

from allsearch.coverage import evaluate
from allsearch.intent import analyze, keywords, language_of
from allsearch.models import Comment, ResultItem, ThreadEvidence
from allsearch.sites import mentioned_sites, used_as_place


@pytest.mark.parametrize(
    "query,site,strict",
    [
        ("V2EX 上关于 MacBook 续航的主题帖", "v2ex", True),
        ("Hacker News discussion of SQLite", "hackernews", True),
        ("Reddit thread in r/LocalLLaMA", "reddit", True),
        ("中文维基百科 HTTP 条目", "wikipedia", True),
        ("Node.js 22 GitHub release page", "github", True),
        ("Find the public X status from OpenAI", "x", True),
        ("How to fix GitHub Actions caching error", "github", False),
        ("How do I use the Reddit API to fetch comments", "reddit", False),
        ("GitHub 上传失败怎么办", "github", False),
        ("Telegram bot API rate limits", "telegram", False),
    ],
)
def test_site_used_as_place(query: str, site: str, strict: bool):
    found = {s.key: s for _, s in mentioned_sites(query)}
    assert site in found
    assert used_as_place(query, found[site]) is strict


def test_no_false_site_matches():
    for query in ("what is in my box.com folder", "NGA says the satellite", "the next weekly report"):
        assert analyze(query).sites == []


def test_intent_requirements():
    dual = analyze("分别找 V2EX 主题帖和 Chiphell 讨论帖，内容都要提到客制化键盘")
    assert [r.site.key for r in dual.sites] == ["v2ex", "chiphell"]
    assert dual.all_sites is True
    assert dual.topic == "客制化键盘"

    hn = analyze(
        "In the Hacker News thread about SQLite as an application file format, "
        "find a comment that says the user interface should not wait on disk before responding"
    )
    assert hn.comments and hn.topic == "SQLite as application file format"
    assert "disk" in hn.comment_focus and "SQLite" not in hn.comment_focus
    assert hn.recency_hours is None

    cross = analyze("用中文找 Hacker News 上把 SQLite 当作应用程序文件格式来讨论的英文帖子")
    assert cross.target_language == "en" and cross.target_language_explicit
    assert cross.needs_rewrite

    assert analyze("过去 48 小时内关于半导体出口管制的一条新闻").recency_hours == 48
    assert analyze("AI chip export news from the past 3 days").recency_hours == 72
    assert analyze("今天的新闻").recency_hours == 24

    compare = analyze("对比 React 官方的 managing state 和 Vue 官方的 reactivity fundamentals")
    assert compare.entities == ["React managing state", "Vue reactivity fundamentals"]

    wiki = analyze("English Wikipedia HTTP article: the default TCP port")
    assert wiki.sites[0].domains == ("en.wikipedia.org",)


def test_include_domains_overrides_site_detection():
    assert analyze("V2EX 上的帖子", include_domains=["example.com"]).sites == []


def test_keywords_keep_topic_words():
    assert keywords("LWN.net article explaining RCU in the Linux kernel") == "explaining RCU Linux kernel"
    assert keywords("How to fix GitHub Actions caching error") == "fix GitHub Actions caching error"
    assert language_of("V2EX 上关于 MacBook 续航") == "zh"
    assert language_of("Hacker News discussion") == "en"


def _r(url: str, title: str = "", published_at: str | None = None) -> ResultItem:
    return ResultItem(id="r", url=url, title=title or url, published_at=published_at)


def test_site_requirement_needs_a_content_page_on_that_site():
    intent = analyze("V2EX 上关于 MacBook 续航突然变差的具体主题帖")
    off_site = evaluate(intent, [_r("https://zhuanlan.zhihu.com/p/1"), _r("https://sspai.com/post/2")], [], [])
    assert off_site.status == "insufficient"
    home = evaluate(intent, [_r("https://www.v2ex.com/"), _r("https://www.v2ex.com/member/x")], [], [])
    assert home.status == "insufficient"
    assert "non-content" in home.requirements[0].detail
    topic = evaluate(intent, [_r("https://hk.v2ex.com/t/910789")], [], [])
    assert topic.status == "sufficient"


def test_all_named_sources_must_be_covered():
    intent = analyze("分别找 V2EX 主题帖和 Chiphell 讨论帖，内容都要提到客制化键盘")
    one = evaluate(intent, [_r("https://www.v2ex.com/t/1")], [], [])
    assert one.status == "insufficient"
    both = evaluate(intent, [_r("https://www.v2ex.com/t/1"), _r("https://www.chiphell.com/thread-2-1-1.html")], [], [])
    assert both.status == "sufficient"


def test_comments_requirement_needs_api_comments_matching_the_focus():
    intent = analyze("In the Hacker News thread about SQLite, find a comment that says the UI should not wait on disk")
    url = "https://news.ycombinator.com/item?id=1"
    found_only = ThreadEvidence(platform="hackernews", url=url, status="found")
    assert evaluate(intent, [_r(url)], [], [found_only]).status == "insufficient"
    unrelated = ThreadEvidence(
        platform="hackernews", url=url, status="comments_read", comments=[Comment(id="2", text="nice", relevance=None)]
    )
    assert evaluate(intent, [_r(url)], [], [unrelated]).status == "insufficient"
    relevant = ThreadEvidence(
        platform="hackernews", url=url, status="comments_read", comments=[Comment(id="3", text="UI must not block on disk", relevance=2.0)]
    )
    assert evaluate(intent, [_r(url)], [], [relevant]).status == "sufficient"


def test_recency_window_uses_published_dates():
    intent = analyze("过去 48 小时内关于半导体出口管制的一条新闻")
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    stale = evaluate(intent, [_r("https://e.com/a", published_at="2026-10-01T00:00:00Z")], [], [], now=now)
    assert stale.status == "insufficient"
    fresh = evaluate(intent, [_r("https://e.com/a", published_at="2026-10-15T08:00:00Z")], [], [], now=now)
    assert fresh.status == "sufficient"


def test_comparison_needs_both_sides():
    intent = analyze("对比 React 官方的 managing state 和 Vue 官方的 reactivity fundamentals")
    one = evaluate(intent, [_r("https://react.dev/learn/managing-state", "Managing State – React")], [], [])
    assert [r.target for r in one.requirements if not r.satisfied] == ["Vue reactivity fundamentals"]
    both = evaluate(
        intent,
        [
            _r("https://react.dev/learn/managing-state", "Managing State – React"),
            _r("https://vuejs.org/guide/essentials/reactivity-fundamentals.html", "Reactivity Fundamentals | Vue.js"),
        ],
        [],
        [],
    )
    assert both.status == "sufficient"


def test_queries_without_requirements_are_not_applicable():
    assert evaluate(analyze("python venv documentation"), [_r("https://docs.python.org/3/library/venv.html")], [], []).status == "not_applicable"
