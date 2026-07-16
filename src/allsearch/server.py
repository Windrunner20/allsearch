"""Thin FastMCP bindings for search, fetch, and health."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from allsearch.config import AllSearchConfig, load_config
from allsearch.models import FetchRequest, SearchRequest
from allsearch.orchestrator import Orchestrator

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
    mcp = FastMCP(
        cfg.server_name,
        json_response=True,
        host=cfg.mcp_host,
        port=cfg.mcp_port,
        streamable_http_path=cfg.mcp_path,
        stateless_http=cfg.mcp_stateless_http,
    )

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
        return result.model_dump()

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
        return result.model_dump()

    @mcp.tool()
    async def health(probe: bool = False) -> dict[str, Any]:
        """Provider configuration and circuit health. probe=true runs bounded cached probes."""
        result = await orchestrator.health(probe=probe)
        return result.model_dump()

    return orchestrator, mcp
