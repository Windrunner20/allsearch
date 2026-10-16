"""Authentication, rate limiting, and request limits for Streamable HTTP.

stdio needs none of this: the client launched the process and owns its pipes.
Over HTTP every request must carry ``Authorization: Bearer <token>`` unless the
operator explicitly allows anonymous access on a loopback address.

Tokens are compared as SHA-256 digests in constant time. The tokens file holds
one ``principal sha256:<hex>`` pair per line, so the file never contains a
usable credential. ``allsearch hash-token`` prints a fresh token and its line.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from allsearch.errors import ConfigError

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

ANONYMOUS = "anonymous"
PRINCIPAL_STATE_KEY = "allsearch_principal"
_PRINCIPAL_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-@")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token_line(principal: str) -> tuple[str, str]:
    """Return (token, tokens-file line) for a new principal."""
    _check_principal(principal)
    token = "as_" + secrets.token_urlsafe(32)
    return token, f"{principal} sha256:{hash_token(token)}"


def _check_principal(principal: str) -> None:
    if not principal or len(principal) > 64 or not set(principal) <= _PRINCIPAL_CHARS:
        raise ConfigError(f"invalid principal name {principal!r}: use 1-64 of [A-Za-z0-9._-@]")


def parse_tokens_file(text: str) -> dict[str, str]:
    """Map sha256 digest -> principal."""
    digests: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or not parts[1].startswith("sha256:"):
            raise ConfigError(f"tokens file line {number}: expected '<principal> sha256:<hex>'")
        principal, digest = parts[0], parts[1][len("sha256:") :].lower()
        _check_principal(principal)
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ConfigError(f"tokens file line {number}: digest must be 64 hex characters")
        if digest in digests:
            raise ConfigError(f"tokens file line {number}: duplicate token")
        digests[digest] = principal
    return digests


def is_loopback_host(host: str) -> bool:
    value = (host or "").strip().strip("[]").lower()
    if value in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


@dataclass(slots=True)
class HttpSecurityConfig:
    token_digests: dict[str, str] = field(default_factory=dict)
    allow_anonymous: bool = False
    rate_per_minute: int = 120
    burst: int = 30
    max_body_bytes: int = 1_048_576
    allowed_hosts: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=list)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.token_digests)


def load_http_security(
    *,
    host: str,
    token: str,
    tokens_file: str,
    allow_anonymous: bool,
    rate_per_minute: int,
    burst: int,
    max_body_bytes: int,
    allowed_hosts: list[str],
    allowed_origins: list[str],
) -> HttpSecurityConfig:
    digests: dict[str, str] = {}
    if tokens_file:
        path = Path(tokens_file).expanduser()
        try:
            digests.update(parse_tokens_file(path.read_text(encoding="utf-8")))
        except OSError as exc:
            raise ConfigError(f"cannot read ALLSEARCH_HTTP_TOKENS_FILE: {exc.strerror}") from exc
    if token:
        if len(token) < 24:
            raise ConfigError("ALLSEARCH_HTTP_TOKEN must be at least 24 characters")
        digests.setdefault(hash_token(token), "default")
    if not digests:
        if not allow_anonymous:
            raise ConfigError(
                "Streamable HTTP needs ALLSEARCH_HTTP_TOKEN or ALLSEARCH_HTTP_TOKENS_FILE. "
                "For a loopback-only development server set ALLSEARCH_HTTP_ALLOW_ANONYMOUS=true."
            )
        if not is_loopback_host(host):
            raise ConfigError(
                f"refusing anonymous HTTP on non-loopback address {host!r}; configure tokens"
            )
    return HttpSecurityConfig(
        token_digests=digests,
        allow_anonymous=allow_anonymous and not digests,
        rate_per_minute=rate_per_minute,
        burst=burst,
        max_body_bytes=max_body_bytes,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


class TokenBucket:
    """Per-principal token bucket. Not shared across worker processes."""

    def __init__(self, rate_per_minute: int, burst: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.rate = max(1, rate_per_minute) / 60.0
        self.burst = max(1, burst)
        self.clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}

    def take(self, key: str) -> float:
        """Return 0 when allowed, otherwise seconds until the next token."""
        now = self.clock()
        tokens, stamp = self._buckets.get(key, (float(self.burst), now))
        tokens = min(float(self.burst), tokens + (now - stamp) * self.rate)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return 0.0
        self._buckets[key] = (tokens, now)
        if len(self._buckets) > 10_000:
            self._buckets = dict(list(self._buckets.items())[-5_000:])
        return (1.0 - tokens) / self.rate


async def _respond(send: Send, status: int, body: dict[str, Any], headers: list[tuple[bytes, bytes]] | None = None) -> None:
    data = json.dumps(body).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(data)).encode()),
                *(headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": data})


class SecurityMiddleware:
    """ASGI wrapper around the FastMCP Streamable HTTP app."""

    def __init__(self, app: ASGIApp, config: HttpSecurityConfig, *, health_path: str = "/healthz") -> None:
        self.app = app
        self.config = config
        self.health_path = health_path
        self.limiter = TokenBucket(config.rate_per_minute, config.burst)

    def authenticate(self, scope: Scope) -> str | None:
        if not self.config.auth_enabled:
            return ANONYMOUS if self.config.allow_anonymous else None
        header = b""
        for name, value in scope.get("headers") or []:
            if name.lower() == b"authorization":
                header = value
                break
        text = header.decode("latin-1").strip()
        if not text.lower().startswith("bearer "):
            return None
        digest = hash_token(text[7:].strip())
        principal = None
        for known, name in self.config.token_digests.items():
            if hmac.compare_digest(known, digest):
                principal = name
        return principal

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") == self.health_path and scope.get("method") == "GET":
            await _respond(send, 200, {"status": "ok"})
            return
        principal = self.authenticate(scope)
        if principal is None:
            await _respond(
                send,
                401,
                {"error": "unauthorized"},
                [(b"www-authenticate", b'Bearer realm="allsearch"')],
            )
            return
        wait = self.limiter.take(principal)
        if wait > 0:
            await _respond(
                send,
                429,
                {"error": "rate_limited", "retry_after_seconds": round(wait, 1)},
                [(b"retry-after", str(max(1, int(wait + 0.999))).encode())],
            )
            return
        for name, value in scope.get("headers") or []:
            if name.lower() == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared < 0 or declared > self.config.max_body_bytes:
                    await _respond(send, 413, {"error": "request_too_large"})
                    return
        scope.setdefault("state", {})[PRINCIPAL_STATE_KEY] = principal

        limit = self.config.max_body_bytes
        seen = 0

        async def limited_receive() -> dict[str, Any]:
            nonlocal seen
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body") or b"")
                if seen > limit:
                    # Truncate: the JSON-RPC parser rejects the partial body.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        await self.app(scope, limited_receive, send)
