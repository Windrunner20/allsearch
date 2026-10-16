from __future__ import annotations

import json
from pathlib import Path

from benchmarks.harness import (
    budget,
    current_stop_decision,
    load_cases,
    main,
    plan_case,
    score_observation,
)

CASES = Path(__file__).parents[2] / "benchmarks" / "cases.json"
REQUIRED_CHANNELS = {
    "zhihu",
    "tieba",
    "v2ex",
    "chiphell",
    "reddit",
    "hackernews",
    "x",
    "github",
    "news",
    "encyclopedia",
    "general_web",
}


def test_case_set_covers_channels_and_dimensions():
    cases = load_cases(CASES)
    assert len(cases) >= 30
    channels = {case["channel"] for case in cases}
    assert REQUIRED_CHANNELS <= channels
    for case in cases:
        assert case["target"]
        assert case["rubric"]
        assert case["dimensions"]
        assert set(case["dimensions"]) <= {
            "discovery",
            "reading",
            "comments",
            "cross_language",
            "synthesis",
            "freshness",
        }
        assert case["expected_access"] in {"public", "login", "paywall", "anti_bot", "mixed"}
        assert case["task_requires"]


def test_balanced_stops_supplement_for_forums_but_not_fresh_news(base_config):
    cases = {case["id"]: case for case in load_cases(CASES)}
    forum = plan_case(cases["v2ex-macos"], base_config, depth="balanced")
    fresh = plan_case(cases["news-fresh-query"], base_config, depth="balanced")
    verify = plan_case(cases["v2ex-macos"], base_config, depth="verify")
    assert forum["tavily_skipped_if_primary_sufficient"] is True
    assert fresh["tavily_skipped_if_primary_sufficient"] is False
    assert verify["tavily_skipped_if_primary_sufficient"] is False
    assert verify["scrape_pages"] == 2
    assert forum["scrape_pages"] == 0


def test_page_match_comment_and_paywall_are_separate():
    cases = {case["id"]: case for case in load_cases(CASES)}
    homepage = score_observation(
        cases["wiki-en-tcp"],
        {
            "answer": "The default port is 80.",
            "results": [{"url": "https://en.wikipedia.org/", "title": "Wikipedia", "snippet": "80"}],
        },
    )
    assert homepage["scores"]["discovery"]["status"] == "fail"
    found = score_observation(
        cases["wiki-en-tcp"],
        {
            "answer": "The default port is 80.",
            "results": [
                {
                    "url": "https://en.wikipedia.org/wiki/HTTP",
                    "title": "HTTP",
                    "snippet": "port 80",
                }
            ],
        },
    )
    assert found["scores"]["discovery"]["status"] == "pass"
    assert found["scores"]["synthesis"]["status"] == "pass"
    assert found["task"]["status"] == "pass"

    title_only = score_observation(
        cases["hn-comment-dissent"],
        {
            "answer": "The submission title mentions SQLite.",
            "results": [{"url": "https://news.ycombinator.com/item?id=23508923", "title": "SQLite"}],
        },
    )
    assert title_only["scores"]["comments"]["status"] == "not_attempted"
    assert title_only["task"]["status"] == "fail"
    assert title_only["algorithm_miss"] is True

    blocked = score_observation(
        cases["paywall-wsj"],
        {
            "answer": "",
            "results": [
                {
                    "url": "https://www.wsj.com/business/nobel",
                    "title": "Nobel",
                    "snippet": "Nobel prize",
                }
            ],
            "fetched": [
                {
                    "url": "https://www.wsj.com/business/nobel",
                    "content": "Subscribe to continue",
                }
            ],
        },
    )
    assert blocked["observed_failure"] == "paywall"
    assert blocked["expected_access"] == "paywall"
    assert blocked["task"]["status"] == "blocked"
    assert blocked["algorithm_miss"] is False

    unlabeled = score_observation(
        cases["weibo-login"],
        {"answer": "", "results": []},
    )
    assert unlabeled["observed_failure"] is None
    assert unlabeled["expected_access"] == "login"


def test_budget_command_does_not_require_live_calls(base_config, capsys):
    assert main(["budget", "--depth", "balanced"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cases"] >= 30
    assert payload["xai_min"] >= 30
    assert payload["skipped_supplement_if_primary_looks_full"] > 0
    plans = [plan_case(case, base_config, depth="balanced") for case in load_cases(CASES)]
    assert budget(plans)["tavily_max"] >= budget(plans)["tavily_min"]


def test_live_command_refuses_without_running():
    assert main(["live"]) == 2


def test_current_policy_stops_before_the_requested_evidence(base_config):
    named_forum = current_stop_decision(
        base_config,
        "V2EX 上关于 MacBook 续航突然变差的具体主题帖",
        [
            "https://zhuanlan.zhihu.com/p/1",
            "https://sspai.com/post/2",
            "https://www.wikipedia.org/wiki/MacBook",
        ],
    )
    assert named_forum["sufficient"] is True
    assert named_forum["supplement_skipped"] is True

    one_doc = current_stop_decision(
        base_config,
        "对比 React 官方的 managing state 和 Vue 官方的 reactivity fundamentals",
        [
            "https://react.dev/learn/managing-state",
            "https://react.dev/learn/state-a-components-memory",
            "https://blog.example.com/react-vue",
        ],
    )
    assert one_doc["sufficient"] is True
    assert one_doc["supplement_skipped"] is True

    assert current_stop_decision(
        base_config,
        "Hacker News discussion of SQLite as an application file format",
        ["https://news.ycombinator.com/item?id=23508923"],
    )["recency"] is True
    assert current_stop_decision(
        base_config,
        "HN item 23508923 SQLite application file format",
        ["https://news.ycombinator.com/item?id=23508923"],
    )["recency"] is False


def test_stale_news_fails_the_freshness_task():
    case = next(item for item in load_cases(CASES) if item["id"] == "news-fresh-query")
    scored = score_observation(
        case,
        {
            "executed_at": "2026-04-15T00:00:00Z",
            "window_has_story": True,
            "answer": "旧闻来自 Example News，日期是 2020-01-01。",
            "results": [
                {
                    "url": "https://example.com/news/semiconductor",
                    "title": "出口管制",
                    "snippet": "半导体",
                    "published_at": "2020-01-01T00:00:00Z",
                }
            ],
        },
    )
    assert scored["scores"]["freshness"]["status"] == "fail"
    assert scored["task"]["status"] == "fail"


def test_open_comment_uses_text_and_source_not_a_placeholder_id():
    case = next(item for item in load_cases(CASES) if item["id"] == "v2ex-macos")
    scored = score_observation(
        case,
        {
            "answer": "",
            "results": [
                {
                    "url": "https://www.v2ex.com/t/123456",
                    "title": "MacBook 续航",
                    "snippet": "续航突然变差",
                }
            ],
            "comments": [
                {
                    "id": "r1",
                    "source": "api",
                    "url": "https://www.v2ex.com/t/123456#reply",
                    "text": "我的续航从 10 小时掉到 4 小时",
                }
            ],
        },
    )
    assert scored["scores"]["comments"]["status"] == "pass"
    assert scored["scores"]["discovery"]["status"] == "pass"


def test_page_body_is_not_a_comment():
    """Grader v1 counted a fetched page body as a comment; v2 does not."""
    case = next(item for item in load_cases(CASES) if item["id"] == "v2ex-macos")
    page = {"url": "https://www.v2ex.com/t/123456", "text": "主题正文：续航突然变差"}
    for comment in (page, {**page, "id": "x", "source": "page_body"}):
        scored = score_observation(case, {"answer": "", "results": [], "comments": [comment]})
        assert scored["scores"]["comments"]["status"] == "fail"


def test_snippet_mentioning_captcha_is_not_an_access_failure():
    case = next(item for item in load_cases(CASES) if item["id"] == "antibot-shell")
    observation = {
        "results": [{"url": "https://github.com/x/chaptcha", "title": "captcha solver", "snippet": "captcha"}],
        "fetched": [],
    }
    assert score_observation(case, observation)["observed_failure"] is None


def test_timeout_is_its_own_outcome():
    case = next(item for item in load_cases(CASES) if item["id"] == "hn-comment-dissent")
    assert score_observation(case, {"timed_out": True, "answer": ""})["task"]["status"] == "timeout"
    assert score_observation(case, {"skipped": "xai_auth_stopped"})["task"]["status"] == "not_attempted"


def test_percent_encoded_zh_wikipedia_title_matches():
    case = next(item for item in load_cases(CASES) if item["id"] == "wiki-zh-tcp")
    url = "https://zh.wikipedia.org/zh-hans/%E8%B6%85%E6%96%87%E6%9C%AC%E4%BC%A0%E8%BE%93%E5%8D%8F%E8%AE%AE"
    scored = score_observation(case, {"answer": "默认端口 80", "results": [{"url": url}]})
    assert scored["task"]["status"] == "pass"


def test_wsj_chemistry_article_no_longer_satisfies_physics_case():
    case = next(item for item in load_cases(CASES) if item["id"] == "paywall-wsj")
    url = "https://www.wsj.com/science/nobel-prize-in-chemistry-awarded-to-trio-62295a7d"
    scored = score_observation(case, {"results": [{"url": url, "title": "Nobel"}], "fetched": [{"url": url, "content": "Nobel"}]})
    assert scored["scores"]["discovery"]["status"] == "fail"


def test_answer_keys_are_not_in_the_provider_query():
    from benchmarks.harness import provider_query

    for case in load_cases(CASES):
        query = provider_query(case)
        for target in case.get("targets") or []:
            url = str(target.get("url") or "")
            path = str(target.get("path_contains") or "")
            if url:
                assert url not in query
            if any(character.isdigit() for character in path):
                assert path not in query
        for comment in case.get("comment_targets") or []:
            comment_id = str(comment.get("comment_id") or "")
            if comment_id.isdigit():
                assert comment_id not in query
        for secret in ("23508923", "23552225", "23546691"):
            assert secret not in query


def test_missing_news_in_window_is_not_a_search_failure():
    case = next(item for item in load_cases(CASES) if item["id"] == "news-fresh-query")
    scored = score_observation(
        case,
        {
            "executed_at": "2026-04-15T00:00:00Z",
            "window_has_story": False,
            "answer": "这个 48 小时窗口内没有查到相关新闻。",
            "results": [],
        },
    )
    assert scored["scores"]["freshness"]["status"] == "not_answerable"
    assert scored["task"]["status"] == "not_answerable"
    assert scored["algorithm_miss"] is False


def test_two_comments_are_not_complete_when_only_one_is_read():
    case = next(item for item in load_cases(CASES) if item["id"] == "hn-two-comments")
    scored = score_observation(
        case,
        {
            "comments": [
                {"comment_id": "23552225", "text": "synchronous IO is the problem"}
            ]
        },
    )
    assert scored["scores"]["comments"]["status"] == "partial"
    assert scored["task"]["status"] == "fail"
