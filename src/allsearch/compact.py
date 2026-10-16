"""Bounded projections of stored search and fetch responses.

The text content and the structuredContent of a compact tool call are the same
object, serialized once. Every builder here measures the UTF-8 bytes of that
exact serialization, so the bound holds for long URLs, CJK text, and malformed
fields alike, and a client that ignores truncation flags still cannot receive
the stored payload.
"""

from __future__ import annotations

import json
from typing import Any

READ_DEFAULT_BYTES = 8192
READ_MAX_BYTES = 16384
SUMMARY_FLOOR_BYTES = 1024
URL_MAX_CHARS = 400

_RESULT_KEYS = (
    "id",
    "title",
    "url",
    "snippet",
    "provider",
    "matched_providers",
    "source_type",
    "published_at",
    "score",
)
_TEXT_FIELDS = {"answer", "content"}
_SEARCH_FIELDS = {
    "answer",
    "results",
    "citations",
    "errors",
    "warnings",
    "route",
    "evidence",
    "cache",
    "coverage",
    "threads",
    "comments",
}
_FETCH_FIELDS = {"content", "title", "errors", "warnings", "metadata", "route", "comments", "thread"}
_LIST_FIELDS = {"results", "citations", "errors", "warnings", "route", "threads", "comments"}


def dumps(payload: Any) -> str:
    """The one serialization used for both the size check and the wire."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def size_of(payload: Any) -> int:
    return len(dumps(payload).encode("utf-8"))


def clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1] + "…"


def clip_url(value: Any, limit: int = URL_MAX_CHARS) -> tuple[str, bool]:
    url = value if isinstance(value, str) else ""
    if len(url) <= limit:
        return url, False
    return url[:limit], True


def shrink(value: Any, *, max_str: int, max_items: int, depth: int = 4) -> Any:
    """Recursively bound an arbitrary JSON-like value."""
    if isinstance(value, str):
        return clip(value, max_str)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if depth <= 0:
        return clip(dumps(value), max_str)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items * 2:
                out["…"] = f"{len(value) - index} more keys"
                break
            out[clip(str(key), 64)] = shrink(item, max_str=max_str, max_items=max_items, depth=depth - 1)
        return out
    if isinstance(value, (list, tuple)):
        items = [shrink(item, max_str=max_str, max_items=max_items, depth=depth - 1) for item in value[:max_items]]
        if len(value) > max_items:
            items.append(f"… {len(value) - max_items} more")
        return items
    return clip(str(value), max_str)


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _errors(items: Any, *, limit: int = 6, message_len: int = 200) -> list[dict[str, Any]]:
    return [
        {
            "provider": clip(item.get("provider"), 40) or None,
            "code": clip(item.get("code"), 60),
            "message": clip(item.get("message"), message_len),
            "retryable": bool(item.get("retryable")),
        }
        for item in _dicts(items)[:limit]
    ]


def _stages(route: Any, *, limit: int = 16) -> list[dict[str, Any]]:
    stages = route.get("stages") if isinstance(route, dict) else None
    return [
        {
            "provider": clip(stage.get("provider"), 40),
            "operation": clip(stage.get("operation"), 40),
            "outcome": clip(stage.get("outcome"), 20),
            "reason": clip(stage.get("reason"), 80),
        }
        for stage in _dicts(stages)[:limit]
    ]


def _evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key, item in value.items()
        if isinstance(item, (int, float, bool)) and not isinstance(item, str)
    }


def _coverage(value: Any, *, limit: int) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    requirements = []
    for item in _dicts(value.get("requirements"))[:limit]:
        requirements.append(
            {
                "kind": clip(item.get("kind"), 30),
                "target": clip(item.get("target"), 80),
                "satisfied": bool(item.get("satisfied")),
                "detail": clip(item.get("detail"), 120),
            }
        )
    return {
        "status": clip(value.get("status"), 20),
        "requirements": requirements,
        "requirements_total": len(_dicts(value.get("requirements"))),
    }


def _thread_brief(value: Any, *, limit: int, text_len: int) -> list[dict[str, Any]]:
    out = []
    for thread in _dicts(value)[:limit]:
        url, _ = clip_url(thread.get("url"))
        comments = _dicts(thread.get("comments"))
        out.append(
            {
                "platform": clip(thread.get("platform"), 30),
                "url": url,
                "title": clip(thread.get("title"), 120),
                "status": clip(thread.get("status"), 30),
                "comments_total": thread.get("comments_total") if isinstance(thread.get("comments_total"), int) else None,
                "comments_read": len(comments),
                "top_comments": [
                    {
                        "id": clip(comment.get("id"), 40),
                        "author": clip(comment.get("author"), 40),
                        "text": clip(comment.get("text"), text_len),
                    }
                    for comment in comments[: 2 if text_len else 0]
                ],
            }
        )
    return out


def _next_steps(value: Any, *, limit: int) -> list[dict[str, Any]]:
    out = []
    for step in _dicts(value)[:limit]:
        url, _ = clip_url(step.get("url"))
        item = {"tool": clip(step.get("tool"), 30), "why": clip(step.get("why"), 120)}
        if url:
            item["url"] = url
        if step.get("query"):
            item["query"] = clip(step.get("query"), 200)
        for key in ("include_domains", "fields"):
            if isinstance(step.get(key), list):
                item[key] = [clip(value, 80) for value in step[key][:5]]
        out.append(item)
    return out


def build_search_summary(full: dict[str, Any], result_id: str, *, max_bytes: int) -> dict[str, Any]:
    """Summary that keeps every citation URL until the byte budget is exhausted.

    Snippets and titles are dropped before any URL. URLs that still do not fit
    remain stored and are returned by ``read_result``.
    """
    max_bytes = max(SUMMARY_FLOOR_BYTES, int(max_bytes))
    results = _dicts(full.get("results"))
    citations = _dicts(full.get("citations"))
    answer = full.get("answer") if isinstance(full.get("answer"), str) else ""
    warnings = [str(item) for item in full.get("warnings") or [] if item is not None] if isinstance(full.get("warnings"), list) else []
    cache = full.get("cache") if isinstance(full.get("cache"), dict) else {}
    threads = _dicts(full.get("threads"))

    def pack(level: int, citation_count: int | None, result_count: int | None) -> dict[str, Any]:
        snippet_len, title_len, answer_len, comment_len = (
            (160, 120, 800, 240),
            (0, 80, 600, 160),
            (0, 0, 400, 0),
            (0, 0, 200, 0),
        )[level]
        shown_citations = citations if citation_count is None else citations[:citation_count]
        shown_results = results if result_count is None else results[:result_count]
        truncated_urls = False

        def url_of(item: dict[str, Any]) -> str:
            nonlocal truncated_urls
            url, cut = clip_url(item.get("url"))
            truncated_urls = truncated_urls or cut
            return url

        payload: dict[str, Any] = {
            "schema_version": clip(full.get("schema_version"), 10),
            "result_id": result_id,
            "kind": "search",
            "status": clip(full.get("status"), 20),
            "query": clip(full.get("query"), 400 if level < 2 else 160),
            "answer": clip(answer, answer_len),
            "answer_chars": len(answer),
            "evidence": _evidence(full.get("evidence")),
            "cache": {"hit": bool(cache.get("hit")), "age_seconds": cache.get("age_seconds", 0) if isinstance(cache.get("age_seconds", 0), (int, float)) else 0},
            "timing_ms": full.get("timing_ms", 0) if isinstance(full.get("timing_ms", 0), int) else 0,
            "warning_count": len(warnings),
            "warnings": [clip(item, 160) for item in warnings[: 8 if level < 2 else 3]],
            "errors": _errors(full.get("errors"), limit=6 if level < 2 else 3),
            "route": _stages(full.get("route"), limit=16 if level < 2 else 6),
            "result_count": len(results),
            "citation_count": len(citations),
            "results": [
                {
                    "id": clip(item.get("id"), 16),
                    "title": clip(item.get("title"), title_len),
                    "url": url_of(item),
                    "snippet": clip(item.get("snippet"), snippet_len),
                    "providers": [clip(p, 30) for p in (item.get("matched_providers") or [item.get("provider")])[:6] if p],
                }
                for item in shown_results
            ],
            "citations": [
                {
                    "id": clip(item.get("id"), 16),
                    "url": url_of(item),
                    "title": clip(item.get("title"), title_len),
                    "result_item_id": clip(item.get("result_id"), 16) or None,
                    "providers": [clip(p, 30) for p in (item.get("providers") or [])[:6] if p],
                }
                for item in shown_citations
            ],
            "read": {"tool": "read_result", "result_id": result_id},
        }
        coverage = _coverage(full.get("coverage"), limit=8 if level < 2 else 4)
        if coverage is not None:
            payload["coverage"] = coverage
        if threads:
            payload["threads"] = _thread_brief(threads, limit=4 if level < 2 else 2, text_len=comment_len)
        steps = _next_steps(full.get("next_steps"), limit=4 if level < 2 else 2)
        if steps:
            payload["next_steps"] = steps
        if isinstance(full.get("session"), dict):
            payload["session"] = shrink(full["session"], max_str=120, max_items=8, depth=2)
        if result_count is not None and result_count < len(results):
            payload["results_truncated"] = True
        if citation_count is not None and citation_count < len(citations):
            payload["citations_truncated"] = True
        if truncated_urls:
            payload["urls_truncated"] = True
        return payload

    for level in range(4):
        payload = pack(level, None, None)
        if size_of(payload) <= max_bytes:
            return payload
    # Drop results before citations: every result URL also appears as a citation.
    for count in range(len(results), -1, -1):
        payload = pack(3, None, count)
        if size_of(payload) <= max_bytes:
            return payload
    for count in range(len(citations), -1, -1):
        payload = pack(3, count, 0)
        if size_of(payload) <= max_bytes:
            return payload
    return _minimal(full, result_id, "search", max_bytes)


def build_fetch_summary(full: dict[str, Any], result_id: str, *, max_bytes: int) -> dict[str, Any]:
    max_bytes = max(SUMMARY_FLOOR_BYTES, int(max_bytes))
    content = full.get("content") if isinstance(full.get("content"), str) else ""
    thread = full.get("thread") if isinstance(full.get("thread"), dict) else None
    url, url_cut = clip_url(full.get("url"))
    final_url, final_cut = clip_url(full.get("final_url"))
    warnings = full.get("warnings") if isinstance(full.get("warnings"), list) else []
    preview_len = 1000
    while True:
        payload: dict[str, Any] = {
            "schema_version": clip(full.get("schema_version"), 10),
            "result_id": result_id,
            "kind": "fetch",
            "status": clip(full.get("status"), 20),
            "url": url,
            "final_url": final_url,
            "title": clip(full.get("title"), 300 if preview_len >= 200 else 80),
            "provider": clip(full.get("provider"), 30) or None,
            "content_preview": content[:preview_len],
            "content_chars": len(content),
            "truncated": len(content) > preview_len or bool(full.get("truncated")),
            "errors": _errors(full.get("errors"), limit=4),
            "warning_count": len(warnings),
            "warnings": [clip(item, 120) for item in warnings[:4]],
            "read": {"tool": "read_result", "result_id": result_id, "fields": ["content"]},
        }
        if thread is not None:
            brief = _thread_brief([thread], limit=1, text_len=240 if preview_len >= 400 else 0)
            payload["thread"] = brief[0] if brief else None
            payload["read"]["comment_fields"] = ["comments"]
        if url_cut or final_cut:
            payload["urls_truncated"] = True
        if size_of(payload) <= max_bytes:
            return payload
        if preview_len == 0:
            return _minimal(full, result_id, "fetch", max_bytes)
        preview_len = preview_len // 2 if preview_len > 100 else 0


def _minimal(full: dict[str, Any], result_id: str, kind: str, max_bytes: int) -> dict[str, Any]:
    """Last resort. Every field is fixed-width, so this is far below the floor."""
    return {
        "result_id": result_id,
        "kind": kind,
        "status": clip(full.get("status"), 20),
        "truncated": True,
        "note": "summary did not fit max_bytes; use read_result",
        "read": {"tool": "read_result", "result_id": result_id},
    }


def _text_page(text: str, offset: int, limit: int, budget: int) -> tuple[str, int]:
    """Largest slice text[offset:offset+n] (n <= limit) whose UTF-8 size fits budget."""
    chunk = text[offset : offset + limit]
    if len(chunk.encode("utf-8")) + len(chunk) // 8 <= budget:
        # JSON escaping can grow control characters and quotes; leave headroom
        # and let the caller's final size check catch the rest.
        return chunk, len(chunk)
    low, high = 0, len(chunk)
    while low < high:
        mid = (low + high + 1) // 2
        if size_of(chunk[:mid]) <= budget:
            low = mid
        else:
            high = mid - 1
    return chunk[:low], low


def _render_item(field: str, item: Any, text_len: int) -> Any:
    if field == "results" and isinstance(item, dict):
        rendered = {key: item.get(key) for key in _RESULT_KEYS}
        rendered["title"] = clip(rendered.get("title"), text_len)
        rendered["snippet"] = clip(rendered.get("snippet"), text_len)
        return shrink(rendered, max_str=max(text_len, URL_MAX_CHARS), max_items=8, depth=2)
    if field == "threads" and isinstance(item, dict):
        rendered = {key: value for key, value in item.items() if key != "comments"}
        rendered["comments_read"] = len(_dicts(item.get("comments")))
        rendered["body"] = clip(item.get("body"), text_len)
        return shrink(rendered, max_str=max(text_len, URL_MAX_CHARS), max_items=8, depth=2)
    if field == "comments" and isinstance(item, dict):
        rendered = dict(item)
        rendered["text"] = clip(item.get("text"), max(text_len, 200))
        return shrink(rendered, max_str=max(text_len, URL_MAX_CHARS), max_items=8, depth=2)
    return shrink(item, max_str=max(text_len, URL_MAX_CHARS) if text_len else 200, max_items=8, depth=3)


def _list_source(response: dict[str, Any], field: str) -> list[Any]:
    if field == "route":
        return _stages(response.get("route"), limit=10_000)
    if field == "comments":
        direct = response.get("comments")
        if isinstance(direct, list):
            return direct
        thread = response.get("thread")
        if isinstance(thread, dict) and isinstance(thread.get("comments"), list):
            return thread["comments"]
        # Search records: comments that matched the request (relevance > 0),
        # across all threads and best first, then the rest in thread order.
        # Agents otherwise page through hundreds of comments to find them.
        matched: list[Any] = []
        rest: list[Any] = []
        for thread in _dicts(response.get("threads")):
            for comment in _dicts(thread.get("comments")):
                item = {**comment, "thread_url": thread.get("url")}
                score = comment.get("relevance")
                (matched if isinstance(score, (int, float)) and score > 0 else rest).append(item)
        matched.sort(key=lambda c: -float(c.get("relevance") or 0))
        return matched + rest
    value = response.get(field)
    return value if isinstance(value, list) else []


def project_record(
    record: dict[str, Any],
    *,
    fields: list[str] | None,
    offset: int,
    limit: int,
    max_bytes: int,
) -> dict[str, Any]:
    """One page of a stored record.

    ``offset`` and ``limit`` count characters for text fields (``answer``,
    ``content``) and items for list fields. When fields of both kinds are
    requested together, each field reports its own ``next_offsets`` entry and
    ``next_offset`` is the smallest of them.
    """
    kind = str(record.get("kind") or "")
    response = record.get("response") if isinstance(record.get("response"), dict) else {}
    allowed = _FETCH_FIELDS if kind == "fetch" else _SEARCH_FIELDS
    selected = list(dict.fromkeys(fields or (["content"] if kind == "fetch" else ["results", "citations"])))
    unknown = [field for field in selected if field not in allowed]
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(unknown)}; allowed: {', '.join(sorted(allowed))}")
    offset = max(0, int(offset))
    ceiling = max(512, min(int(max_bytes), READ_MAX_BYTES))
    text_fields = [field for field in selected if field in _TEXT_FIELDS]
    list_fields = [field for field in selected if field in _LIST_FIELDS]
    other_fields = [field for field in selected if field not in _TEXT_FIELDS and field not in _LIST_FIELDS]
    text_limit = max(1, min(int(limit), 200_000)) if text_fields else 0
    item_limit = max(1, min(int(limit), 50)) if list_fields else 0
    if text_fields and list_fields and int(limit) > 50:
        # A character-sized limit is meaningless for list fields.
        item_limit = 10

    def render(item_cap: int, item_text_len: int, text_budget: int) -> tuple[dict[str, Any], dict[str, int | None]]:
        data: dict[str, Any] = {}
        next_offsets: dict[str, int | None] = {}
        per_text_budget = max(64, text_budget // max(1, len(text_fields))) if text_fields else 0
        for field in text_fields:
            text = response.get(field) if isinstance(response.get(field), str) else ""
            page, used = _text_page(text, offset, text_limit, per_text_budget)
            data[field] = page
            data[f"{field}_chars"] = len(text)
            next_offsets[field] = offset + used if offset + used < len(text) else None
        for field in list_fields:
            items = _list_source(response, field)
            page = items[offset : offset + item_cap]
            data[field] = [_render_item(field, item, item_text_len) for item in page]
            data[f"{field}_total"] = len(items)
            if field == "comments":
                data["comments_relevant"] = sum(
                    1 for c in items if isinstance(c, dict) and isinstance(c.get("relevance"), (int, float)) and c["relevance"] > 0
                )
            next_offsets[field] = offset + len(page) if offset + len(page) < len(items) else None
        for field in other_fields:
            data[field] = shrink(response.get(field), max_str=item_text_len or 120, max_items=16, depth=3)
        return data, next_offsets

    def envelope(data: dict[str, Any], next_offsets: dict[str, int | None]) -> dict[str, Any]:
        pending = [value for value in next_offsets.values() if value is not None]
        return {
            "result_id": record.get("result_id"),
            "kind": kind,
            "fields": selected,
            "offset": offset,
            "next_offset": min(pending) if pending else None,
            "next_offsets": next_offsets,
            "truncated": bool(pending),
            "data": data,
        }

    item_cap = item_limit
    item_text_len = 600
    text_budget = ceiling - 600
    while True:
        data, next_offsets = render(item_cap, item_text_len, text_budget)
        payload = envelope(data, next_offsets)
        if size_of(payload) <= ceiling:
            break
        if text_fields and text_budget > 256:
            text_budget = max(256, text_budget * 2 // 3)
            continue
        if list_fields and item_cap > 1:
            item_cap = max(1, item_cap // 2)
            continue
        if item_text_len > 80:
            item_text_len = max(80, item_text_len // 2)
            continue
        if text_fields and text_budget > 64:
            text_budget = 64
            continue
        # Unreachable for well-formed records. Still advance the cursor so a
        # client paging in a loop cannot spin on the same offset forever.
        skipped = {field: offset + 1 for field in list_fields}
        skipped.update({field: offset + 64 for field in text_fields})
        payload = envelope(
            {"note": "page exceeded the output ceiling and was skipped; request one field at a time"},
            skipped,
        )
        break
    return payload
