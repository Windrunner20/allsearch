"""Tests for Tavily multi-key rotation pool."""

from __future__ import annotations

import time

import pytest

from allsearch.config import TavilyConfig
from allsearch.errors import AuthError, TransportError
from allsearch.providers.tavily import TavilyProvider, _KeyPool
from allsearch.transport import HttpTransport


def _cfg(keys: tuple[str, ...]) -> TavilyConfig:
    primary = keys[0] if keys else None
    extra = keys[1:] if len(keys) > 1 else ()
    return TavilyConfig(
        api_key=primary,
        extra_api_keys=extra,
        base_url="https://api.tavily.com",
        search_path="/search",
        default_depth="basic",
        max_results=8,
    )


# ---------------------------------------------------------------------------
# _KeyPool
# ---------------------------------------------------------------------------


def test_pool_round_robin():
    pool = _KeyPool(("a", "b", "c"))
    picks = [pool.pick() for _ in range(6)]
    assert picks == ["a", "b", "c", "a", "b", "c"]


def test_pool_pick_none_when_empty():
    assert _KeyPool(()).pick() is None


def test_pool_park_advances_index_and_skips_key():
    pool = _KeyPool(("a", "b", "c"))
    assert pool.pick() == "a"
    pool.park("a")
    # "a" parked -> next picks should skip it
    assert pool.pick() == "b"
    assert pool.pick() == "c"
    assert pool.pick() == "b"  # a still parked


def test_pool_reset_clears_parking():
    pool = _KeyPool(("a", "b"))
    pool.park("a")
    assert "a" not in pool.available_keys()
    pool.reset("a")
    assert "a" in pool.available_keys()


def test_pool_available_keys_filters_parked():
    pool = _KeyPool(("a", "b", "c"))
    assert set(pool.available_keys()) == {"a", "b", "c"}
    pool.park("b")
    assert set(pool.available_keys()) == {"a", "c"}


# ---------------------------------------------------------------------------
# TavilyProvider rotation behavior
# ---------------------------------------------------------------------------


class _FakeTransport(HttpTransport):
    """Records the api_key used per call and returns canned responses or raises."""

    def __init__(self, responses):
        super().__init__(timeout_seconds=5, max_retries=0)
        self.responses = responses  # list of dict | Exception
        self.used_keys: list[str] = []

    async def request_json(self, method, url, *, headers=None, json=None, **kw):  # type: ignore[override]
        self.used_keys.append(json["api_key"])
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.mark.asyncio
async def test_provider_rotates_on_429_to_next_key():
    cfg = _cfg(("k1", "k2", "k3"))
    transport = _FakeTransport(
        [
            TransportError("provider HTTP 429", retryable=True, status_code=429),
            {"query": "q", "answer": "ok", "results": []},
        ]
    )
    provider = TavilyProvider(cfg, transport)
    result = await provider.search("q")
    assert result.answer == "ok"
    assert transport.used_keys == ["k1", "k2"]


@pytest.mark.asyncio
async def test_provider_rotates_on_quota_message_then_exhausts():
    cfg = _cfg(("k1", "k2"))
    transport = _FakeTransport(
        [
            TransportError("rate limit exceeded", retryable=True, status_code=429),
            TransportError("quota exceeded", retryable=True, status_code=429),
        ]
    )
    provider = TavilyProvider(cfg, transport)
    with pytest.raises(TransportError):
        await provider.search("q")
    assert transport.used_keys == ["k1", "k2"]


@pytest.mark.asyncio
async def test_provider_single_key_no_rotation_on_429():
    cfg = _cfg(("only",))
    transport = _FakeTransport(
        [TransportError("provider HTTP 429", retryable=True, status_code=429)]
    )
    provider = TavilyProvider(cfg, transport)
    with pytest.raises(TransportError):
        await provider.search("q")
    assert transport.used_keys == ["only"]


@pytest.mark.asyncio
async def test_provider_non_quota_error_does_not_rotate():
    cfg = _cfg(("k1", "k2"))
    # 500-style persistent transport error is not a quota signal and should raise immediately.
    transport = _FakeTransport(
        [TransportError("provider HTTP 503", retryable=True, status_code=503)]
    )
    provider = TavilyProvider(cfg, transport)
    with pytest.raises(TransportError):
        await provider.search("q")
    assert transport.used_keys == ["k1"]


@pytest.mark.asyncio
async def test_provider_auth_error_rotates_when_pool_has_multiple_keys():
    cfg = _cfg(("bad", "good"))
    transport = _FakeTransport(
        [
            AuthError("authentication failed (401)", provider="tavily"),
            {"query": "q", "answer": "ok", "results": []},
        ]
    )
    provider = TavilyProvider(cfg, transport)
    result = await provider.search("q")
    assert result.answer == "ok"
    assert transport.used_keys == ["bad", "good"]


@pytest.mark.asyncio
async def test_provider_unconfigured_raises():
    cfg = TavilyConfig(
        api_key=None,
        extra_api_keys=(),
        base_url="https://api.tavily.com",
        search_path="/search",
        default_depth="basic",
        max_results=8,
    )
    provider = TavilyProvider(cfg, _FakeTransport([]))
    from allsearch.errors import ProviderUnavailableError

    with pytest.raises(ProviderUnavailableError):
        await provider.search("q")


def test_config_pool_dedup_and_order():
    cfg = TavilyConfig(
        api_key="primary",
        extra_api_keys=("dup", "primary", "extra", "dup"),
        base_url="https://api.tavily.com",
        search_path="/search",
        default_depth="basic",
        max_results=8,
    )
    assert cfg.all_keys() == ("primary", "dup", "extra")


@pytest.mark.asyncio
async def test_provider_uses_round_robin_across_calls():
    cfg = _cfg(("k1", "k2", "k3"))
    transport = _FakeTransport(
        [
            {"query": "q", "answer": "a1", "results": []},
            {"query": "q", "answer": "a2", "results": []},
            {"query": "q", "answer": "a3", "results": []},
        ]
    )
    provider = TavilyProvider(cfg, transport)
    await provider.search("q1")
    await provider.search("q2")
    await provider.search("q3")
    # round-robin should distribute across all three keys
    assert transport.used_keys == ["k1", "k2", "k3"]
