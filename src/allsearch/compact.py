"""Bounded projections of stored search and fetch responses.

Both the text content and structuredContent of a compact tool are this object,
so a client that ignores text truncation still cannot see the stored payload.
"""

from __future__ import annotations

import json
from typing import Any

READ_MAX_BYTES = 8192
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
_SEARCH_FIELDS = {
    "answer",
    "results",
    "citations",
    "errors",
    "warnings",
    "route",
    "evidence",
    "cache",
}
_FETCH_FIELDS = {"content", "title", "errors", "warnings", "metadata", "route"}


def _clip(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    if limit <= 0 or len(text) <= limit:
        return text if limit > 0 else ""
    if limit == 1:
        return "…"
    return text[: limit - 1] + "…"


def _dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _errors(items: Any) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return rendered
    for item in items:
        if not isinstance(item, dict):
            continue
        rendered.append(
            {
                "provider": item.get("provider"),
                "code": item.get("code"),
                "message": _clip(item.get("message"), 240),
                "retryable": bool(item.get("retryable")),
            }
        )
    return rendered


def _stages(route: Any) -> list[dict[str, Any]]:
    stages = route.get("stages") if isinstance(route, dict) else None
    if not isinstance(stages, list):
        return []
    rendered = []
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        rendered.append(
            {
                "provider": stage.get("provider"),
                "operation": stage.get("operation"),
                "outcome": stage.get("outcome"),
                "reason": stage.get("reason"),
            }
        )
    return rendered


def build_search_summary(full: dict[str, Any], result_id: str, *, max_bytes: int) -> dict[str, Any]:
    """Summary that keeps every citation URL until the byte budget is exhausted.

    Snippets and titles are dropped before any URL. URLs that still do not fit
    remain stored and are returned by ``read_result``.
    """
    results = [item for item in full.get("results") or [] if isinstance(item, dict)]
    citations = [item for item in full.get("citations") or [] if isinstance(item, dict)]
    answer = full.get("answer") if isinstance(full.get("answer"), str) else ""
    warnings = [str(item) for item in full.get("warnings") or []]
    cache = full.get("cache") if isinstance(full.get("cache"), dict) else {}

    def pack(snippet_len: int, title_len: int, citation_count: int | None) -> dict[str, Any]:
        shown = citations if citation_count is None else citations[:citation_count]
        return {
            "schema_version": full.get("schema_version"),
            "result_id": result_id,
            "kind": "search",
            "status": full.get("status"),
            "query": _clip(full.get("query"), 400),
            "answer": _clip(answer, 800),
            "answer_chars": len(answer),
            "evidence": full.get("evidence") or {},
            "cache": {"hit": bool(cache.get("hit")), "age_seconds": cache.get("age_seconds", 0)},
            "timing_ms": full.get("timing_ms", 0),
            "warning_count": len(warnings),
            "warnings": [_clip(item, 160) for item in warnings[:8]],
            "errors": _errors(full.get("errors")),
            "route": _stages(full.get("route")),
            "result_count": len(results),
            "citation_count": len(citations),
            "results": [
                {
                    "id": item.get("id"),
                    "title": _clip(item.get("title"), title_len),
                    "url": item.get("url") or "",
                    "snippet": _clip(item.get("snippet"), snippet_len),
                    "providers": item.get("matched_providers") or [item.get("provider")],
                }
                for item in results
            ],
            "citations": [
                {
                    "id": item.get("id"),
                    "url": item.get("url") or "",
                    "title": _clip(item.get("title"), title_len),
                    "result_item_id": item.get("result_id"),
                    "providers": item.get("providers") or [],
                }
                for item in shown
            ],
            "read": {"tool": "read_result", "result_id": result_id},
        }

    for snippet_len, title_len in ((160, 120), (0, 80), (0, 0)):
        payload = pack(snippet_len, title_len, None)
        if len(_dumps(payload)) <= max_bytes:
            return payload
    for count in range(len(citations), -1, -1):
        payload = pack(0, 0, count)
        payload["citations_truncated"] = count < len(citations)
        if len(_dumps(payload)) <= max_bytes:
            return payload
    payload = pack(0, 0, 0)
    payload["citations_truncated"] = bool(citations)
    return payload


def build_fetch_summary(full: dict[str, Any], result_id: str, *, max_bytes: int) -> dict[str, Any]:
    content = full.get("content") if isinstance(full.get("content"), str) else ""
    preview_len = 1000
    while True:
        payload = {
            "schema_version": full.get("schema_version"),
            "result_id": result_id,
            "kind": "fetch",
            "status": full.get("status"),
            "url": full.get("url") or "",
            "final_url": full.get("final_url") or "",
            "title": _clip(full.get("title"), 300),
            "provider": full.get("provider"),
            "content_preview": content[:preview_len],
            "content_chars": len(content),
            "truncated": len(content) > preview_len or bool(full.get("truncated")),
            "errors": _errors(full.get("errors")),
            "warning_count": len(full.get("warnings") or []),
            "read": {"tool": "read_result", "result_id": result_id, "fields": ["content"]},
        }
        if len(_dumps(payload)) <= max_bytes or preview_len <= 200:
            return payload
        preview_len = max(200, preview_len // 2)


def project_record(
    record: dict[str, Any],
    *,
    fields: list[str] | None,
    offset: int,
    limit: int,
    max_bytes: int,
) -> dict[str, Any]:
    kind = str(record.get("kind") or "")
    response = record.get("response") if isinstance(record.get("response"), dict) else {}
    allowed = _FETCH_FIELDS if kind == "fetch" else _SEARCH_FIELDS
    selected = fields or (["content"] if kind == "fetch" else ["results", "citations"])
    unknown = [field for field in selected if field not in allowed]
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(unknown)}")
    offset = max(0, offset)
    limit = max(1, min(limit, 20 if "content" not in selected and "answer" not in selected else 6000))
    ceiling = max(256, min(max_bytes, READ_MAX_BYTES))

    def render(item_limit: int, text_limit: int) -> dict[str, Any]:
        data: dict[str, Any] = {}
        more = False
        for field in selected:
            if field == "answer":
                text = response.get("answer") if isinstance(response.get("answer"), str) else ""
                data["answer"] = text[offset : offset + text_limit]
                data["answer_chars"] = len(text)
                more = more or offset + text_limit < len(text)
            elif field == "content":
                text = response.get("content") if isinstance(response.get("content"), str) else ""
                data["content"] = text[offset : offset + text_limit]
                data["content_chars"] = len(text)
                more = more or offset + text_limit < len(text)
            elif field == "results":
                items = [item for item in response.get("results") or [] if isinstance(item, dict)]
                page = items[offset : offset + item_limit]
                data["results"] = [{key: item.get(key) for key in _RESULT_KEYS} for item in page]
                data["results_total"] = len(items)
                more = more or offset + item_limit < len(items)
            elif field == "citations":
                items = [item for item in response.get("citations") or [] if isinstance(item, dict)]
                data["citations"] = items[offset : offset + item_limit]
                data["citations_total"] = len(items)
                more = more or offset + item_limit < len(items)
            elif field in {"errors", "warnings"}:
                items = response.get(field) or []
                if not isinstance(items, list):
                    items = []
                data[field] = items[offset : offset + item_limit]
                data[f"{field}_total"] = len(items)
                more = more or offset + item_limit < len(items)
            elif field == "route":
                stages = _stages(response.get("route"))
                data["route"] = stages[offset : offset + item_limit]
                data["route_total"] = len(stages)
                more = more or offset + item_limit < len(stages)
            else:
                data[field] = response.get(field)
        return {
            "result_id": record.get("result_id"),
            "kind": kind,
            "offset": offset,
            "returned": text_limit if selected == ["content"] or selected == ["answer"] else item_limit,
            "next_offset": (offset + (text_limit if _text_only(selected) else item_limit)) if more else None,
            "data": data,
        }

    text_only = _text_only(selected)
    item_limit = 1 if text_only else limit
    text_limit = limit if text_only else 0
    payload = render(item_limit, text_limit if text_only else limit)
    while len(_dumps(payload)) > ceiling and (item_limit > 1 or text_limit > 200):
        if text_only and text_limit > 200:
            text_limit = max(200, text_limit // 2)
        elif item_limit > 1:
            item_limit -= 1
        else:
            break
        payload = render(item_limit, text_limit if text_only else item_limit)
    payload["truncated"] = len(_dumps(payload)) > ceiling or payload["next_offset"] is not None
    if len(_dumps(payload)) > ceiling:
        payload["data"] = {"note": "page exceeded the output ceiling; request fewer fields"}
        payload["truncated"] = True
    return payload


def _text_only(fields: list[str]) -> bool:
    return set(fields) <= {"answer", "content", "title"}
