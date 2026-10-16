"""MCP bindings: search, fetch, read_result, health.

Nothing here knows about a particular client. Each tool returns standard MCP
content: one compact JSON text block plus the same object as
``structuredContent``. Failures that the model should see and recover from are
returned with ``isError: true``; malformed arguments are rejected by the
JSON-Schema validation FastMCP derives from the signatures below.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from allsearch.compact import (
    READ_DEFAULT_BYTES,
    READ_MAX_BYTES,
    build_fetch_summary,
    build_search_summary,
    dumps,
    project_record,
)
from allsearch.config import AllSearchConfig, load_config
from allsearch.http_security import PRINCIPAL_STATE_KEY, is_loopback_host
from allsearch.models import FetchRequest, SearchRequest
from allsearch.orchestrator import Orchestrator
from allsearch.records import LOCAL_OWNER, RecordStore
from allsearch.session import SessionLimits, SessionRegistry, advice, fetch_key, search_key

logger = logging.getLogger("allsearch.server")

SearchMode = Literal["auto", "web", "news", "docs", "research", "vertical"]
SearchDepth = Literal["fast", "balanced", "verify", "deep"]

INSTRUCTIONS = (
    "AllSearch searches the public web and reads pages and forum threads. "
    "search returns a bounded summary with citation URLs, a coverage block that "
    "says which parts of the request were or were not satisfied, and a result_id. "
    "fetch reads one known URL; Hacker News, V2EX and Stack Exchange threads come "
    "back with real comment ids, authors and timestamps. read_result pages any "
    "stored result without calling a provider again. The session block counts "
    "calls without new evidence; when it says nothing new was found, answer with "
    "what you have and state what is missing instead of repeating the call."
)


def _ensure_list(value: str | list[str] | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value)


def _caller(ctx: Context | None) -> tuple[str, str]:
    """(record owner, session key) for the current request."""
    request = None
    if ctx is not None:
        try:
            request = ctx.request_context.request
        except (ValueError, AttributeError):
            request = None
    if request is None:
        return LOCAL_OWNER, LOCAL_OWNER
    state = getattr(request, "state", None)
    principal = getattr(state, PRINCIPAL_STATE_KEY, None) or "anonymous"
    session_id = ""
    headers = getattr(request, "headers", None)
    if headers is not None:
        session_id = headers.get("mcp-session-id") or ""
    return principal, f"{principal}\x1f{session_id}"


def _result(payload: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=dumps(payload))],
        structuredContent=payload,
        isError=is_error,
    )


def _urls_of(full: dict[str, Any]) -> list[str]:
    urls = [str(item.get("url") or "") for item in full.get("results") or [] if isinstance(item, dict)]
    urls += [str(item.get("url") or "") for item in full.get("citations") or [] if isinstance(item, dict)]
    return [url for url in urls if url]


def _read_urls(full: dict[str, Any]) -> list[str]:
    urls = [
        str(item.get("url") or "")
        for item in full.get("results") or []
        if isinstance(item, dict) and item.get("content")
    ]
    urls += [
        str(thread.get("url") or "")
        for thread in full.get("threads") or []
        if isinstance(thread, dict) and thread.get("status") in {"read", "comments_read"}
    ]
    return [url for url in urls if url]


_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True, idempotentHint=True)
_LOCAL_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False, idempotentHint=True)

Output = Annotated[CallToolResult, dict[str, Any]]


def build_mcp(config: AllSearchConfig | None = None) -> tuple[Orchestrator, FastMCP]:
    cfg = config or load_config()
    orchestrator = Orchestrator(cfg)
    compact = cfg.compact_output
    records = (
        RecordStore(
            cfg.record_dir or None,
            ttl_seconds=cfg.record_ttl_seconds,
            max_entries=cfg.record_max_entries,
            max_bytes=cfg.record_max_bytes,
            max_entries_per_owner=cfg.record_max_entries_per_owner,
        )
        if compact
        else None
    )
    sessions = SessionRegistry(
        SessionLimits(
            window_seconds=cfg.session_window_seconds,
            max_searches=cfg.session_max_searches,
            max_fetches=cfg.session_max_fetches,
            repeat_window_seconds=min(cfg.session_window_seconds, cfg.record_ttl_seconds),
        )
    )

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await orchestrator.aclose()

    transport_security = None
    if cfg.http_allowed_hosts or cfg.http_allowed_origins:
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(cfg.http_allowed_hosts),
            allowed_origins=list(cfg.http_allowed_origins),
        )
    elif not is_loopback_host(cfg.mcp_host):
        # Host-header checks need the public hostname, which only the operator
        # knows. Bearer auth (required off-loopback) is the control here.
        transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    mcp = FastMCP(
        cfg.server_name,
        instructions=INSTRUCTIONS if compact else None,
        json_response=True,
        host=cfg.mcp_host,
        port=cfg.mcp_port,
        streamable_http_path=cfg.mcp_path,
        stateless_http=cfg.mcp_stateless_http,
        lifespan=lifespan,
        transport_security=transport_security,
        log_level=cfg.log_level if cfg.log_level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else "INFO",  # type: ignore[arg-type]
    )

    def budget_error(kind: str, limit: int) -> CallToolResult:
        return _result(
            {
                "kind": kind,
                "status": "error",
                "errors": [
                    {
                        "provider": "allsearch",
                        "code": "session_budget_exhausted",
                        "message": (
                            f"{limit} {kind} calls in the last {cfg.session_window_seconds}s for this "
                            "session. Answer with the evidence already found."
                        ),
                        "retryable": True,
                    }
                ],
            },
            is_error=True,
        )

    @mcp.tool(title="Web search", annotations=_READ_ONLY)
    async def search(
        query: Annotated[str, Field(min_length=1, max_length=2000, description="What to find. Name a site (e.g. V2EX, Hacker News) to restrict to it.")],
        mode: SearchMode = "auto",
        depth: Annotated[SearchDepth, Field(description="fast < balanced < verify < deep in cost and coverage")] = "balanced",
        max_results: Annotated[int, Field(ge=1, le=20)] = 8,
        include_domains: str | list[str] | None = None,
        exclude_domains: str | list[str] | None = None,
        vertical: str | None = None,
        fresh: Annotated[bool, Field(description="Bypass caches and repeat detection")] = False,
        ctx: Context | None = None,
    ) -> Output:
        """Search the public web. Returns citations, a coverage check against what the query asked for (site, comments, recency, cross-language), forum threads read through their APIs, and a result_id for read_result."""
        owner, session_key = _caller(ctx)
        state = sessions.get(session_key)
        req = SearchRequest(
            query=query,
            mode=mode,
            depth=depth,
            max_results=max(1, min(int(max_results), 20)),
            include_domains=_ensure_list(include_domains),
            exclude_domains=_ensure_list(exclude_domains),
            vertical=vertical,
            fresh=fresh,
        )
        key = search_key(req.model_dump())
        if records is not None and not fresh:
            previous = sessions.previous(state, "searches", key)
            stored = records.get(previous.result_id, owner=owner) if previous else None
            if previous is not None and stored is not None:
                full = stored["response"]
                block = sessions.observe(state, "search", urls=_urls_of(full), repeat=True)
                block["advice"] = advice(block, repeat_of=previous.result_id)
                sessions.remember(state, "searches", key, previous.result_id)
                full = {**full, "session": block}
                return _result(build_search_summary(full, previous.result_id, max_bytes=cfg.compact_max_bytes))
        limit = sessions.over_budget(state, "search")
        if limit is not None:
            return budget_error("search", limit)
        sessions.charge(state, "search")
        result = await orchestrator.search(req)
        full = result.model_dump()
        if records is None:
            # 0.2 full mode: payload unchanged, status carried in the body only.
            return _result(full)
        result_id = records.put("search", full, owner=owner)
        sessions.remember(state, "searches", key, result_id)
        block = sessions.observe(state, "search", urls=_urls_of(full), content_urls=_read_urls(full))
        hint = advice(block)
        if hint:
            block["advice"] = hint
        summary = build_search_summary({**full, "session": block}, result_id, max_bytes=cfg.compact_max_bytes)
        return _result(summary, is_error=full.get("status") == "error")

    @mcp.tool(title="Read a URL", annotations=_READ_ONLY)
    async def fetch(
        url: Annotated[str, Field(min_length=1, max_length=4096, description="Absolute http(s) URL")],
        focus: Annotated[str | None, Field(description="Optional words used to rank forum comments")] = None,
        max_chars: Annotated[int, Field(ge=100, le=200_000)] = 30_000,
        fresh: bool = False,
        ctx: Context | None = None,
    ) -> Output:
        """Read one known URL. Hacker News, V2EX and Stack Exchange threads are read through their public APIs with comment ids, authors, timestamps and replies; other pages through Firecrawl. Login walls, captchas and paywalls are reported, never bypassed."""
        owner, session_key = _caller(ctx)
        state = sessions.get(session_key)
        req = FetchRequest(
            url=url,
            focus=focus,
            max_chars=max(100, min(int(max_chars), 200_000)),
            fresh=fresh,
        )
        key = fetch_key(req.url, req.focus, req.max_chars)
        if records is not None and not fresh:
            previous = sessions.previous(state, "fetches", key)
            stored = records.get(previous.result_id, owner=owner) if previous else None
            if previous is not None and stored is not None:
                full = stored["response"]
                block = sessions.observe(state, "fetch", repeat=True)
                block["advice"] = advice(block, repeat_of=previous.result_id)
                sessions.remember(state, "fetches", key, previous.result_id)
                summary = build_fetch_summary(full, previous.result_id, max_bytes=cfg.compact_max_bytes)
                summary["session"] = block
                return _result(summary)
        limit = sessions.over_budget(state, "fetch")
        if limit is not None:
            return budget_error("fetch", limit)
        sessions.charge(state, "fetch")
        result = await orchestrator.fetch(req)
        full = result.model_dump()
        if records is None:
            return _result(full)
        result_id = records.put("fetch", full, owner=owner)
        sessions.remember(state, "fetches", key, result_id)
        read_ok = bool(full.get("content")) or bool((full.get("thread") or {}).get("comments"))
        block = sessions.observe(
            state,
            "fetch",
            urls=[full.get("final_url") or req.url],
            content_urls=[full.get("final_url") or req.url] if read_ok else [],
        )
        hint = advice(block)
        if hint:
            block["advice"] = hint
        summary = build_fetch_summary(full, result_id, max_bytes=cfg.compact_max_bytes - 400)
        summary["session"] = block
        return _result(summary, is_error=full.get("status") == "error")

    @mcp.tool(title="Provider health", annotations=_LOCAL_READ)
    async def health(probe: bool = False) -> Output:
        """Provider configuration, circuit-breaker state and cache counters. probe=true runs bounded cached probes."""
        result = await orchestrator.health(probe=probe)
        return _result(result.model_dump())

    if records is not None:

        @mcp.tool(title="Read a stored result", annotations=_LOCAL_READ)
        async def read_result(
            result_id: Annotated[str, Field(min_length=16, max_length=32, pattern="^[0-9a-f]+$")],
            fields: str | list[str] | None = None,
            offset: Annotated[int, Field(ge=0)] = 0,
            limit: Annotated[int, Field(ge=1, le=200_000, description="Characters for answer/content, items for lists")] = 20,
            max_bytes: Annotated[int, Field(ge=512, le=READ_MAX_BYTES)] = READ_DEFAULT_BYTES,
            ctx: Context | None = None,
        ) -> Output:
            """Page through a stored search or fetch result without calling any provider.

            Search fields: answer, results, citations, coverage, threads, comments, errors, warnings, route, evidence, cache.
            Fetch fields: content, comments, thread, title, metadata, errors, warnings, route.
            offset/limit count characters for answer/content and items for lists; follow next_offset.
            """
            owner, session_key = _caller(ctx)
            record = records.get(result_id, owner=owner)
            if record is None:
                raise ToolError("unknown or expired result_id")
            selected = _ensure_list(fields)
            try:
                page = project_record(
                    record,
                    fields=selected,
                    offset=max(0, int(offset)),
                    limit=max(1, int(limit)),
                    max_bytes=max(512, min(int(max_bytes), READ_MAX_BYTES)) - 200,
                )
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            state = sessions.get(session_key)
            seen = sessions.note_read(state, dumps([result_id, page["fields"], page["offset"], page.get("next_offset")]))
            block = sessions.observe(state, "read_result", repeat=seen > 1)
            if seen > 1:
                block["advice"] = f"This page was already returned {seen - 1} time(s) in this session."
            page["session"] = block
            return _result(page)

    return orchestrator, mcp


def build_http_app(cfg: AllSearchConfig, mcp: FastMCP) -> Any:
    """Streamable HTTP ASGI app wrapped in auth, rate limiting and body limits."""
    from allsearch.http_security import SecurityMiddleware, load_http_security

    security = load_http_security(
        host=cfg.mcp_host,
        token=cfg.http_token,
        tokens_file=cfg.http_tokens_file,
        allow_anonymous=cfg.http_allow_anonymous,
        rate_per_minute=cfg.http_rate_per_minute,
        burst=cfg.http_burst,
        max_body_bytes=cfg.http_max_body_bytes,
        allowed_hosts=list(cfg.http_allowed_hosts),
        allowed_origins=list(cfg.http_allowed_origins),
    )
    return SecurityMiddleware(mcp.streamable_http_app(), security), security
