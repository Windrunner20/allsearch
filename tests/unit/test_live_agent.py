"""Offline checks for the agent-round observation builder (no Pi, no providers)."""

from __future__ import annotations

import json

from benchmarks.harness import load_cases, score_observation
from benchmarks.live_agent import _parse, efficiency, observation_from_calls


def _events(*events: dict) -> str:
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\nnot json\n"


def _call(cid: str, tool: str, args: dict, structured: dict | None, *, text: str | None = None) -> list[dict]:
    result: dict = {"content": [{"type": "text", "text": text if text is not None else json.dumps(structured)}]}
    if structured is not None and text is None:
        result["structuredContent"] = structured
    return [
        {"type": "tool_execution_start", "toolCallId": cid, "toolName": f"mcp__allsearch__{tool}", "args": args},
        {"type": "tool_execution_end", "toolCallId": cid, "toolName": f"mcp__allsearch__{tool}", "result": result, "isError": False, "durationMs": 5},
    ]


HN = "https://news.ycombinator.com/item?id=23508923"
SEARCH = {
    "result_id": "a" * 24,
    "results": [{"url": HN, "title": "SQLite as an Application File Format", "snippet": "302 comments"}],
    "citations": [{"url": "https://sqlite.org/appfileformat.html", "title": ""}],
    "threads": [{"url": HN, "top_comments": [{"id": "23552225", "author": "lillecarl", "text": "If the user interface depends on synchronous IO something is very wrong."}]}],
    "session": {"calls_without_new_evidence": 0},
}
FETCH = {
    "result_id": "b" * 24,
    "url": HN,
    "final_url": HN,
    "content_preview": "# SQLite as an Application File Format",
    "thread": {"top_comments": [{"id": "23546691", "author": "int_19h", "text": "Windows has snapshot-based transactions on NTFS"}]},
    "session": {"calls_without_new_evidence": 2},
}
READ = {"result_id": "b" * 24, "data": {"content": " more page text", "comments": [{"id": "9", "text": "no id link"}, {"text": "missing id"}]}}


def test_parse_collects_calls_in_order_and_final_answer():
    stdout = _events(
        *_call("1", "search", {"query": "q"}, SEARCH),
        *_call("2", "fetch", {"url": HN}, None, text=json.dumps(FETCH)),  # text-only result
        *_call("3", "fetch", {"url": HN}, FETCH),
        {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "Answer with "}, {"type": "text", "text": HN}]}},
    )
    calls, answer = _parse(stdout)
    assert [c["tool"] for c in calls] == ["mcp__allsearch__search", "mcp__allsearch__fetch", "mcp__allsearch__fetch"]
    assert calls[1]["result"]["result_id"] == "b" * 24
    assert answer == "Answer with " + HN
    eff = efficiency(calls)
    assert eff["tool_calls"] == {"search": 1, "fetch": 2}
    assert eff["duplicate_calls"] == 1
    assert eff["max_calls_without_new_evidence"] == 2


def test_observation_uses_tool_results_not_the_answer():
    calls, _ = _parse(_events(*_call("1", "search", {"query": "q"}, SEARCH), *_call("2", "fetch", {"url": HN}, FETCH), *_call("3", "read_result", {"result_id": "b" * 24}, READ)))
    answer_only_url = "https://news.ycombinator.com/item?id=99999999"
    obs = observation_from_calls("hn-two-comments", calls, f"see {answer_only_url}")
    urls = {r["url"] for r in obs["results"]}
    assert HN in urls and answer_only_url not in urls
    assert obs["fetched"][0]["content"] == "# SQLite as an Application File Format more page text"
    ids = [c["id"] for c in obs["comments"]]
    assert ids == ["23552225", "23546691", "9"]  # the record without an id is dropped
    assert all(c["source"] == "api" for c in obs["comments"])
    case = next(c for c in load_cases() if c["id"] == "hn-two-comments")
    assert score_observation(case, obs)["scores"]["comments"]["status"] == "pass"


def test_answer_urls_alone_do_not_pass_discovery():
    case = next(c for c in load_cases() if c["id"] == "x-public-post")
    obs = observation_from_calls(case["id"], [], "OpenAI: https://x.com/OpenAI/status/1635687373060317185 GPT-4")
    assert score_observation(case, obs)["task"]["status"] == "fail"


def test_model_failure_is_detected():
    stdout = _events({"type": "message_end", "message": {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "503 unavailable"}})
    calls, answer = _parse(stdout)
    assert calls == [] and answer == ""
    assert _parse.model_error.startswith("503")


def test_pi_nested_call_tool_result_is_unwrapped():
    """Pi puts the whole MCP CallToolResult into result.structuredContent."""
    nested = {"content": [{"type": "text", "text": json.dumps(SEARCH)}], "structuredContent": SEARCH, "isError": False}
    stdout = _events(
        {"type": "tool_execution_start", "toolCallId": "x|y", "toolName": "mcp__allsearch__search", "args": {}},
        {"type": "tool_execution_end", "toolCallId": "x|y", "toolName": "mcp__allsearch__search",
         "result": {"content": nested["content"], "details": {"server": "allsearch"}, "structuredContent": nested}},
    )
    calls, _ = _parse(stdout)
    assert calls[0]["result"]["result_id"] == "a" * 24
    obs = observation_from_calls("x", calls, "")
    assert obs["results"][0]["url"] == HN and obs["comments"][0]["id"] == "23552225"
