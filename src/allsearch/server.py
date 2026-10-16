"""Thin FastMCP bindings for search, fetch, and health."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from allsearch.compact import build_fetch_summary, build_search_summary, project_record
from allsearch.config import AllSearchConfig, load_config
from allsearch.models import FetchRequest, SearchRequest
from allsearch.orchestrator import Orchestrator
from allsearch.records import RecordStore

SearchMode = Literal["auto", "web", "news", "docs", "research", "vertical"]
SearchDepth = Literal["fast", "balanced", "verify", "deep"]


def _ensure_list(value: str | list[str] | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value)


def build_mcp(config: AllSearchConfig | None = None) -> tuple[Orchestrator, FastMCP]:
    cfg = config or load_config()
    orchestrator = Orchestrator(cfg)
    records = (
        RecordStore(
            cfg.record_dir or None,
            ttl_seconds=cfg.record_ttl_seconds,
            max_entries=cfg.record_max_entries,
            max_bytes=cfg.record_max_bytes,
        )
        if cfg.compact_output
        else None
    )
    instructions = None
    if records is not None:
        instructions = (
            "search and fetch return a bounded summary and a result_id. "
            "Citation URLs and error codes are in that summary, or on a later "
            "read_result page if citations_truncated is true. Full answers, "
            "snippets, and page text are only available through read_result."
        )
    mcp = FastMCP(
        cfg.server_name,
        instructions=instructions,
        json_response=True,
        host=cfg.mcp_host,
        port=cfg.mcp_port,
        streamable_http_path=cfg.mcp_path,
        stateless_http=cfg.mcp_stateless_http,
    )

    def _search_payload(result: Any) -> dict[str, Any]:
        full = result.model_dump()
        if records is None:
            return full
        result_id = records.put("search", full)
        return build_search_summary(full, result_id, max_bytes=cfg.compact_max_bytes)

    def _fetch_payload(result: Any) -> dict[str, Any]:
        full = result.model_dump()
        if records is None:
            return full
        result_id = records.put("fetch", full)
        return build_fetch_summary(full, result_id, max_bytes=cfg.compact_max_bytes)

    @mcp.tool()
    async def search(
        query: str,
        mode: SearchMode = "auto",
        depth: SearchDepth = "balanced",
        max_results: int = 8,
        include_domains: str | list[str] | None = None,
        exclude_domains: str | list[str] | None = None,
        vertical: str | None = None,
        fresh: bool = False,
    ) -> dict[str, Any]:
        """Orchestrated web search: Grok primary, Tavily/AnySearch/Firecrawl as supplements."""
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
        result = await orchestrator.search(req)
        return _search_payload(result)

    @mcp.tool()
    async def fetch(
        url: str,
        focus: str | None = None,
        max_chars: int = 30_000,
        fresh: bool = False,
    ) -> dict[str, Any]:
        """Fetch full content for a known HTTP(S) URL via Firecrawl (post-discovery assist)."""
        req = FetchRequest(
            url=url,
            focus=focus,
            max_chars=max(100, min(int(max_chars), 200_000)),
            fresh=fresh,
        )
        result = await orchestrator.fetch(req)
        return _fetch_payload(result)

    @mcp.tool()
    async def health(probe: bool = False) -> dict[str, Any]:
        """Provider configuration and circuit health. probe=true runs bounded cached probes."""
        result = await orchestrator.health(probe=probe)
        return result.model_dump()

    if records is not None:

        @mcp.tool()
        async def read_result(
            result_id: str,
            fields: str | list[str] | None = None,
            offset: int = 0,
            limit: int = 4,
            max_bytes: int = 4096,
        ) -> dict[str, Any]:
            """Read one stored search or fetch result by id.

            offset and limit page list fields and slice answer/content.
            The returned object is size-capped. Page content is omitted from
            result items unless fields includes a text field on a fetch record.
            """
            record = records.get(result_id)
            if record is None:
                raise ToolError("unknown or expired result_id")
            selected = _ensure_list(fields)
            try:
                return project_record(
                    record,
                    fields=selected,
                    offset=max(0, int(offset)),
                    limit=max(1, int(limit)),
                    max_bytes=max(256, min(int(max_bytes), 8192)),
                )
            except ValueError as exc:
                raise ToolError(str(exc)) from exc

    return orchestrator, mcp
