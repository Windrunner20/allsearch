"""CLI and MCP transport entry point.

    allsearch                          stdio (default)
    allsearch --transport streamable-http --host 0.0.0.0 --port 8000
    allsearch hash-token <principal>   print a new HTTP token and its tokens-file line
    allsearch check                    print resolved configuration (no secrets)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from dataclasses import replace
from typing import Literal

from allsearch import __version__
from allsearch.config import load_config
from allsearch.errors import ConfigError

TransportName = Literal["stdio", "sse", "streamable-http"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="allsearch",
        description="AllSearch MCP search server",
    )
    parser.add_argument("--version", action="version", version=f"allsearch {__version__}")
    parser.add_argument(
        "command",
        nargs="?",
        choices=["serve", "hash-token", "check"],
        default="serve",
        help="serve (default), hash-token PRINCIPAL, or check",
    )
    parser.add_argument("principal", nargs="?", help="principal name for hash-token")
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


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if level > logging.DEBUG:
        # One INFO line per JSON-RPC request is noise for stdio hosts.
        logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)
        logging.getLogger("mcp.server.streamable_http_manager").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "hash-token":
        from allsearch.http_security import new_token_line

        if not args.principal:
            parser.error("hash-token needs a principal name")
        try:
            token, line = new_token_line(args.principal)
        except ConfigError as exc:
            parser.error(str(exc))
        print(f"token (give to the client):      {token}")
        print(f"line (append to the tokens file): {line}")
        return

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"allsearch: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
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

    if args.command == "check":
        from allsearch.config import dotenv_candidates

        public = config.public_dict()
        public["dotenv_candidates"] = [str(path) for path in dotenv_candidates()]
        print(json.dumps(public, indent=2, ensure_ascii=False, default=str))
        return

    _configure_logging(args.log_level or config.log_level)

    # mcp 1.26 + pydantic-settings on Python 3.14 warn about FastMCP's own
    # lifespan annotation at import; it is not actionable here.
    warnings.filterwarnings("ignore", message=r"Field 'lifespan' has an incomplete definition")
    from allsearch.server import build_http_app, build_mcp

    _, server = build_mcp(config)
    transport: TransportName = config.transport  # type: ignore[assignment]
    if transport == "stdio":
        server.run(transport="stdio")
        return

    import anyio
    import uvicorn

    if transport == "sse":
        from allsearch.http_security import SecurityMiddleware, load_http_security

        security = _security(config, load_http_security)
        app = SecurityMiddleware(server.sse_app(), security)
    else:
        try:
            app, security = build_http_app(config, server)
        except ConfigError as exc:
            print(f"allsearch: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
    mode = "bearer-token" if security.auth_enabled else "anonymous (loopback only)"
    logging.getLogger("allsearch").warning(
        "serving %s on http://%s:%s%s with %s auth",
        transport,
        config.mcp_host,
        config.mcp_port,
        config.mcp_path if transport == "streamable-http" else "/sse",
        mode,
    )
    uv_config = uvicorn.Config(
        app,
        host=config.mcp_host,
        port=config.mcp_port,
        log_level=(args.log_level or config.log_level).lower(),
        proxy_headers=False,
        server_header=False,
    )
    anyio.run(uvicorn.Server(uv_config).serve)


def _security(config, loader):  # type: ignore[no-untyped-def]
    try:
        return loader(
            host=config.mcp_host,
            token=config.http_token,
            tokens_file=config.http_tokens_file,
            allow_anonymous=config.http_allow_anonymous,
            rate_per_minute=config.http_rate_per_minute,
            burst=config.http_burst,
            max_body_bytes=config.http_max_body_bytes,
            allowed_hosts=list(config.http_allowed_hosts),
            allowed_origins=list(config.http_allowed_origins),
        )
    except ConfigError as exc:
        print(f"allsearch: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
