"""Tests for xAI endpoint-level fallback and OpenAI chat-completions parsing."""

from __future__ import annotations

import pytest

from allsearch.config import XAIConfig, XAIFallbackEndpoint
from allsearch.errors import AuthError, ProviderContractError, TransportError
from allsearch.providers.xai import XAIProvider, _is_fallbackable
from allsearch.transport import HttpTransport


def _fb() -> XAIFallbackEndpoint:
    return XAIFallbackEndpoint(
        api_key="fb-key",
        base_url="https://fallback.example/v1",
        chat_path="/chat/completions",
        model="grok-4.3-fast",
        protocol="openai",
        reasoning_effort=None,
    )


def _cfg(*, with_fallback: bool = True) -> XAIConfig:
    return XAIConfig(
        api_key="primary-key",
        base_url="https://primary.example/v1",
        responses_path="/responses",
        model="grok-4.5",
        fallback_models=("grok-4.3",),
        reasoning_effort="low",
        allowed_models=("grok-4.5", "grok-4.3", "grok-4.3-fast"),
        max_tool_calls=4,
        fallback_endpoint=_fb() if with_fallback else None,
    )


class _FakeTransport(HttpTransport):
    """Returns scripted responses/exceptions keyed by URL substring.

    Scripts fire once. To make a URL fail repeatedly (e.g. across model-level
    retries), pass an Exception script; it is registered as a *sticky* error
    that fires every time that URL substring is hit.
    """

    def __init__(self, scripts):
        super().__init__(timeout_seconds=5, max_retries=0)
        raw = list(scripts)
        self.sticky_errors: list[tuple[str, Exception]] = []
        self.scripts: list[tuple[str, object]] = []
        for substr, resp in raw:
            if isinstance(resp, Exception):
                self.sticky_errors.append((substr, resp))
            else:
                self.scripts.append((substr, resp))
        self.calls: list[tuple[str, dict]] = []

    async def request_json(self, method, url, *, headers=None, json=None, **kw):  # type: ignore[override]
        self.calls.append((url, json or {}))
        # Sticky errors take precedence and never get consumed.
        for substr, err in self.sticky_errors:
            if substr in url:
                raise err
        for i, (substr, resp) in enumerate(self.scripts):
            if substr in url:
                self.scripts.pop(i)
                return resp  # type: ignore[return-value]
        raise AssertionError(f"no script matched url={url}")


# ---------------------------------------------------------------------------
# _is_fallbackable
# ---------------------------------------------------------------------------


def test_fallbackable_recognizes_402_balance_exhausted():
    err = TransportError("provider HTTP 402", retryable=False, status_code=402)
    assert _is_fallbackable(err) is True


def test_fallbackable_recognizes_429():
    err = TransportError("provider HTTP 429", retryable=True, status_code=429)
    assert _is_fallbackable(err) is True


def test_fallbackable_recognizes_auth_error():
    assert _is_fallbackable(AuthError("401", provider="xai")) is True


def test_fallbackable_skips_permanent_4xx():
    err = TransportError("provider HTTP 404", retryable=False, status_code=404)
    assert _is_fallbackable(err) is False


def test_fallbackable_recognizes_provider_contract_error():
    assert _is_fallbackable(ProviderContractError("bad shape", provider="xai")) is True


# ---------------------------------------------------------------------------
# Endpoint failover
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_primary_402_triggers_fallback_to_chat_endpoint():
    transport = _FakeTransport(
        [
            ("primary.example", TransportError("HTTP 402", retryable=False, status_code=402)),
            (
                "fallback.example",
                {
                    "choices": [
                        {
                            "message": {
                                "content": "Paris is the capital.",
                                "annotations": [
                                    {"url_citation": {"url": "https://wikipedia.org/paris", "title": "Paris"}}
                                ],
                            }
                        }
                    ],
                    "usage": {"completion_tokens": 5},
                },
            ),
        ]
    )
    prov = XAIProvider(_cfg(), transport)
    result = await prov.search("capital of France")
    assert result.tool_usage["endpoint"] == "fallback"
    assert result.tool_usage["model"] == "grok-4.3-fast"
    assert result.tool_usage["fallback_used"] is True
    assert result.answer == "Paris is the capital."
    assert any(c["url"] == "https://wikipedia.org/paris" for c in result.citations)
    assert transport.calls[0][0].endswith("/responses")
    assert transport.calls[1][0].endswith("/chat/completions")


@pytest.mark.asyncio
async def test_primary_success_does_not_use_fallback():
    transport = _FakeTransport(
        [
            (
                "primary.example",
                {
                    "output_text": "from primary",
                    "citations": [{"url": "https://primary.example/x", "title": "X"}],
                    "usage": {},
                },
            )
        ]
    )
    prov = XAIProvider(_cfg(), transport)
    result = await prov.search("q")
    assert result.tool_usage["endpoint"] == "primary"
    assert transport.calls[0][0].endswith("/responses")


@pytest.mark.asyncio
async def test_no_fallback_when_not_configured():
    transport = _FakeTransport(
        [("primary.example", TransportError("HTTP 500", retryable=True, status_code=500))]
    )
    prov = XAIProvider(_cfg(with_fallback=False), transport)
    with pytest.raises(TransportError):
        await prov.search("q")
    # only primary was hit
    assert all("fallback" not in url for url, _ in transport.calls)


# ---------------------------------------------------------------------------
# Chat citation extraction (including content-text fallback parser)
# ---------------------------------------------------------------------------


def test_extract_chat_citations_from_search_sources():
    payload = {
        "search_sources": [
            {"url": "https://a.example", "title": "A"},
            {"url": "https://b.example", "title": "B"},
        ]
    }
    cites = XAIProvider.extract_chat_citations(payload)
    assert [c["url"] for c in cites] == ["https://a.example", "https://b.example"]


def test_extract_chat_citations_from_annotations():
    payload = {
        "choices": [
            {
                "message": {
                    "annotations": [
                        {"url_citation": {"url": "https://ann.example", "title": "Ann"}}
                    ]
                }
            }
        ]
    }
    cites = XAIProvider.extract_chat_citations(payload)
    assert cites == [{"title": "Ann", "url": "https://ann.example"}]


def test_extract_chat_citations_falls_back_to_content_markdown_links():
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Paris is the capital. See "
                        "[Britannica](https://britannica.com/place/Paris) "
                        "and https://raw.example/page."
                    )
                }
            }
        ]
    }
    cites = XAIProvider.extract_chat_citations(payload)
    urls = [c["url"] for c in cites]
    assert "https://britannica.com/place/Paris" in urls
    assert "https://raw.example/page" in urls


def test_extract_chat_citations_dedupes():
    payload = {
        "search_sources": [{"url": "https://dup.example", "title": "1"}],
        "choices": [
            {"message": {"annotations": [{"url_citation": {"url": "https://dup.example", "title": "2"}}]}}
        ],
    }
    cites = XAIProvider.extract_chat_citations(payload)
    assert len(cites) == 1


def test_extract_chat_text_picks_first_nonempty_content():
    payload = {
        "choices": [
            {"message": {"content": ""}},
            {"message": {"content": "real answer"}},
        ]
    }
    assert XAIProvider.extract_chat_text(payload) == "real answer"


# ---------------------------------------------------------------------------
# Fallback payload uses endpoint reasoning_effort, not the primary's
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_payload_uses_endpoint_reasoning_effort():
    transport = _FakeTransport(
        [
            ("primary.example", TransportError("HTTP 402", retryable=False, status_code=402)),
            ("fallback.example", {"choices": [{"message": {"content": "ok"}}], "usage": {}}),
        ]
    )
    cfg = _cfg()  # primary reasoning_effort=low, fallback endpoint reasoning=None
    prov = XAIProvider(cfg, transport)
    await prov.search("q")
    # primary payload should carry reasoning effort
    primary_payload = transport.calls[0][1]
    assert primary_payload.get("reasoning") == {"effort": "low"}
    # fallback (chat) payload should NOT carry reasoning_effort (endpoint default None)
    fallback_payload = transport.calls[1][1]
    assert "reasoning_effort" not in fallback_payload
