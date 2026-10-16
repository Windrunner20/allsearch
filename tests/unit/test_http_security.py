from __future__ import annotations

from typing import Any

import pytest

from allsearch.errors import ConfigError
from allsearch.http_security import (
    PRINCIPAL_STATE_KEY,
    SecurityMiddleware,
    TokenBucket,
    hash_token,
    load_http_security,
    new_token_line,
    parse_tokens_file,
)


def _load(**overrides: Any):
    args = dict(
        host="127.0.0.1",
        token="",
        tokens_file="",
        allow_anonymous=False,
        rate_per_minute=60,
        burst=2,
        max_body_bytes=100,
        allowed_hosts=[],
        allowed_origins=[],
    )
    args.update(overrides)
    return load_http_security(**args)


def test_http_requires_tokens_unless_anonymous_loopback():
    with pytest.raises(ConfigError, match="ALLSEARCH_HTTP_TOKEN"):
        _load()
    assert _load(allow_anonymous=True).allow_anonymous is True
    with pytest.raises(ConfigError, match="refusing anonymous"):
        _load(host="0.0.0.0", allow_anonymous=True)
    with pytest.raises(ConfigError, match="24"):
        _load(token="short")


def test_tokens_file_format(tmp_path):
    token, line = new_token_line("alice")
    assert parse_tokens_file(f"# c\n\n{line}\n") == {hash_token(token): "alice"}
    for bad in ["alice", "alice plain:abc", "al ice sha256:" + "0" * 64, "a/b sha256:" + "0" * 64, "a sha256:xyz"]:
        with pytest.raises(ConfigError):
            parse_tokens_file(bad)
    with pytest.raises(ConfigError, match="duplicate"):
        parse_tokens_file(f"{line}\n{line.replace('alice', 'bob')}")
    path = tmp_path / "t"
    path.write_text(line, encoding="utf-8")
    assert _load(tokens_file=str(path)).token_digests == {hash_token(token): "alice"}


def test_token_bucket():
    now = [0.0]
    bucket = TokenBucket(60, 2, clock=lambda: now[0])
    assert bucket.take("a") == 0 and bucket.take("a") == 0
    assert bucket.take("a") > 0
    assert bucket.take("b") == 0
    now[0] += 1.0
    assert bucket.take("a") == 0


class _Recorder:
    def __init__(self) -> None:
        self.scopes: list[dict[str, Any]] = []

    async def __call__(self, scope, receive, send):
        self.scopes.append(scope)
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": str(len(body)).encode()})


async def _call(app, headers: list[tuple[bytes, bytes]], chunks: list[bytes], path: str = "/mcp", method: str = "POST"):
    sent: list[dict[str, Any]] = []
    queue = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "path": path, "method": method, "headers": headers}, receive, send)
    return sent[0]["status"], sent[1].get("body", b"")


@pytest.mark.asyncio
async def test_middleware_auth_rate_and_body_limits():
    inner = _Recorder()
    token = "x" * 30
    app = SecurityMiddleware(inner, _load(token=token))
    auth = [(b"authorization", b"Bearer " + token.encode())]
    assert (await _call(app, [], [b"{}"]))[0] == 401
    assert (await _call(app, [(b"authorization", b"Bearer wrong")], [b"{}"]))[0] == 401
    status, _ = await _call(app, auth, [b"{}"])
    assert status == 200
    assert inner.scopes[-1]["state"][PRINCIPAL_STATE_KEY] == "default"
    status, _ = await _call(app, auth + [(b"content-length", b"1000")], [b"{}"])
    assert status == 413  # declared too large (also consumes the second burst token)
    status, _ = await _call(app, auth, [b"{}"])
    assert status == 429
    assert (await _call(app, [], [], path="/healthz", method="GET"))[0] == 200


@pytest.mark.asyncio
async def test_streamed_body_over_limit_is_truncated():
    inner = _Recorder()
    app = SecurityMiddleware(inner, _load(allow_anonymous=True, burst=10))
    status, body = await _call(app, [], [b"a" * 60, b"b" * 60])
    assert status == 200
    assert int(body) <= 100
