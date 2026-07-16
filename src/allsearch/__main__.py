"""CLI and MCP transport entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from typing import Literal

from allsearch import __version__
from allsearch.config import load_config
from allsearch.server import build_mcp

TransportName = Literal["stdio", "sse", "streamable-http"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="allsearch",
        description="AllSearch MCP server (Grok-primary multi-provider search)",
    )
    parser.add_argument("--version", action="version", version=f"allsearch {__version__}")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=None,
        help="MCP transport (default: ALLSEARCH_TRANSPORT or stdio)",
    )
    parser.add_argument("--host", default=None, help="HTTP bind host override")
    parser.add_argument("--port", type=int, default=None, help="HTTP bind port override")
    parser.add_argument("--path", default=None, help="Streamable HTTP path override")
    parser.add_argument(
        "--log-level",
        default=None,
        help="Logging level (default: ALLSEARCH_LOG_LEVEL or INFO)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Load config after argv parse so --help works without credentials.
    config = load_config()
    replacements: dict = {}
    if args.transport:
        replacements["transport"] = args.transport
    if args.host:
        replacements["mcp_host"] = args.host
    if args.port is not None:
        replacements["mcp_port"] = args.port
    if args.path:
        path = args.path if args.path.startswith("/") else f"/{args.path}"
        replacements["mcp_path"] = path
    if replacements:
        config = replace(config, **replacements)

    log_level = (args.log_level or config.log_level).upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    _, server = build_mcp(config)
    transport: TransportName = config.transport  # type: ignore[assignment]
    if transport == "stdio":
        server.run(transport="stdio")
    elif transport == "sse":
        server.run(transport="sse")
    else:
        server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
