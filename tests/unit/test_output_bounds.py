"""Compact output must stay inside its byte budget for any stored payload."""

from __future__ import annotations

import json
import random

import pytest

from allsearch.compact import (
    build_fetch_summary,
    build_search_summary,
    dumps,
    project_record,
    size_of,
)

CJK = "中文引用测试，包含全角标点。"


def _wire(payload: dict) -> int:
    return len(dumps(payload).encode("utf-8"))


def _hostile_search(rng: random.Random) -> dict:
    def text(n: int) -> str:
        return "".join(rng.choice([CJK, "ascii ", "\"quote\" ", "\\back\\", "\n", "😀"]) for _ in range(n))

    def url(n: int) -> str:
        return "https://例子.example.com/" + "路径/" * n + "?q=" + "x" * n

    results = [
        {
            "id": f"r{i}",
            "title": text(rng.randint(0, 80)),
            "url": url(rng.randint(1, 600)),
            "snippet": text(rng.randint(0, 300)),
            "content": CJK * 200,
            "matched_providers": ["xai", "tavily", None, 5],
        }
        for i in range(rng.randint(0, 25))
    ]
    citations = [
        {"id": f"c{i}", "url": url(rng.randint(1, 900)), "title": text(rng.randint(0, 50)), "providers": ["xai"] * 20}
        for i in range(rng.randint(0, 40))
    ]
    return {
        "schema_version": "1.1",
        "status": rng.choice(["ok", "partial", "error", None, 5]),
        "query": text(rng.randint(0, 900)),
        "answer": text(rng.randint(0, 3000)),
        "results": results + [None, "junk", 7],
        "citations": citations + [{"url": None}],
        "errors": [
            {"provider": text(30), "code": text(40), "message": text(500), "retryable": True}
            for _ in range(rng.randint(0, 30))
        ],
        "warnings": [text(200) for _ in range(rng.randint(0, 50))],
        "route": {"stages": [{"provider": text(50), "operation": "search", "outcome": "ok", "reason": text(200)}] * rng.randint(0, 60)},
        "evidence": {"unique_urls": 3, "huge": text(5000), "nested": {"a": [text(1000)] * 50}},
        "cache": {"hit": "yes", "age_seconds": "old"},
        "timing_ms": "slow",
        "coverage": {
            "status": "insufficient",
            "requirements": [
                {"kind": "site", "target": text(300), "satisfied": False, "detail": text(500)}
                for _ in range(rng.randint(0, 30))
            ],
        },
        "threads": [
            {
                "platform": "hackernews",
                "url": url(200),
                "title": text(400),
                "status": "comments_read",
                "comments_total": 999,
                "comments": [{"id": str(i), "author": "a" * 80, "text": (CJK * 300)} for i in range(50)],
            }
            for _ in range(rng.randint(0, 6))
        ],
        "next_steps": [{"tool": "fetch", "url": url(800), "why": text(400), "query": text(400)}] * rng.randint(0, 9),
    }


@pytest.mark.parametrize("seed", range(15))
@pytest.mark.parametrize("budget", [1024, 2048, 8192])
def test_search_summary_never_exceeds_budget(seed: int, budget: int):
    full = _hostile_search(random.Random(seed))
    summary = build_search_summary(full, "a" * 24, max_bytes=budget)
    assert _wire(summary) <= budget
    assert summary["result_id"] == "a" * 24
    # Page bodies are never in the summary.
    assert all("content" not in item for item in summary.get("results", []))


def test_search_summary_flags_truncated_long_urls():
    long_url = "https://example.com/" + "a" * 3000
    full = {"status": "ok", "query": "q", "answer": "", "results": [], "citations": [{"id": "c1", "url": long_url}]}
    summary = build_search_summary(full, "b" * 24, max_bytes=8192)
    assert summary["urls_truncated"] is True
    assert len(summary["citations"][0]["url"]) < len(long_url)


@pytest.mark.parametrize("seed", range(10))
def test_fetch_summary_never_exceeds_budget(seed: int):
    rng = random.Random(seed)
    full = {
        "status": "ok",
        "url": "https://example.com/" + "长" * rng.randint(0, 3000),
        "final_url": "https://example.com/" + "x" * rng.randint(0, 3000),
        "title": CJK * rng.randint(0, 200),
        "content": CJK * rng.randint(0, 5000),
        "errors": [{"code": "x" * 300, "message": CJK * 300}] * rng.randint(0, 20),
        "warnings": [CJK * 50] * rng.randint(0, 30),
        "thread": {
            "platform": "v2ex",
            "url": "https://www.v2ex.com/t/1",
            "comments": [{"id": "1", "author": "a", "text": CJK * 400}] * 30,
        },
    }
    summary = build_fetch_summary(full, "c" * 24, max_bytes=1024)
    assert _wire(summary) <= 1024


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize(
    "fields",
    [
        ["answer"],
        ["results"],
        ["citations"],
        ["threads"],
        ["comments"],
        ["coverage"],
        ["evidence"],
        ["answer", "citations"],
        ["results", "citations", "errors", "warnings", "route"],
    ],
)
def test_read_result_pages_are_bounded_and_always_progress(seed: int, fields: list[str]):
    full = _hostile_search(random.Random(seed))
    record = {"result_id": "d" * 24, "kind": "search", "response": full}
    offset: int | None = 0
    pages = 0
    seen_offsets = set()
    while offset is not None and pages < 5000:
        page = project_record(record, fields=fields, offset=offset, limit=40, max_bytes=2048)
        assert _wire(page) <= 2048
        assert offset not in seen_offsets
        seen_offsets.add(offset)
        nxt = page["next_offset"]
        assert nxt is None or nxt > offset
        offset = nxt
        pages += 1
    assert offset is None


def test_read_result_text_paging_reassembles_utf8_exactly():
    answer = (CJK + "mixed ascii 😀 ") * 700
    record = {"result_id": "e" * 24, "kind": "search", "response": {"answer": answer}}
    collected = []
    offset: int | None = 0
    while offset is not None:
        page = project_record(record, fields=["answer"], offset=offset, limit=100_000, max_bytes=1500)
        assert _wire(page) <= 1500
        collected.append(page["data"]["answer"])
        offset = page["next_offset"]
    assert "".join(collected) == answer


def test_read_result_mixed_fields_report_per_field_cursors():
    response = {
        "answer": "x" * 50,
        "citations": [{"id": f"c{i}", "url": f"https://e.com/{i}"} for i in range(5)],
    }
    record = {"result_id": "f" * 24, "kind": "search", "response": response}
    page = project_record(record, fields=["answer", "citations"], offset=0, limit=2, max_bytes=4096)
    assert page["data"]["answer"] == "xx"
    assert len(page["data"]["citations"]) == 2
    assert page["next_offsets"] == {"answer": 2, "citations": 2}
    with pytest.raises(ValueError, match="allowed"):
        project_record(record, fields=["content"], offset=0, limit=2, max_bytes=4096)


def test_comments_field_flattens_threads_with_source_url():
    response = {
        "threads": [
            {"url": "https://news.ycombinator.com/item?id=1", "comments": [{"id": "2", "text": "a"}, {"id": "3", "text": "b"}]},
            {"url": "https://news.ycombinator.com/item?id=9", "comments": [{"id": "10", "text": "c"}]},
        ]
    }
    record = {"result_id": "a" * 24, "kind": "search", "response": response}
    page = project_record(record, fields=["comments"], offset=0, limit=10, max_bytes=4096)
    assert [c["id"] for c in page["data"]["comments"]] == ["2", "3", "10"]
    assert page["data"]["comments"][2]["thread_url"].endswith("id=9")


def test_size_of_matches_wire_text():
    payload = {"a": CJK, "b": [1, 2, {"c": "😀"}]}
    assert size_of(payload) == len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())


def test_search_comments_page_puts_matching_comments_first():
    response = {
        "threads": [
            {"url": "https://news.ycombinator.com/item?id=1", "comments": [{"id": str(i), "text": "x"} for i in range(30)]},
            {"url": "https://news.ycombinator.com/item?id=2", "comments": [{"id": "hit2", "text": "y", "relevance": 2.0}, {"id": "hit5", "text": "z", "relevance": 5.0}]},
        ]
    }
    record = {"result_id": "a" * 24, "kind": "search", "response": response}
    page = project_record(record, fields=["comments"], offset=0, limit=5, max_bytes=4096)
    assert [c["id"] for c in page["data"]["comments"]][:2] == ["hit5", "hit2"]
    assert page["data"]["comments_relevant"] == 2 and page["data"]["comments_total"] == 32
