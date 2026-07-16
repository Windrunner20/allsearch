from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from allsearch.config import XAIConfig
from allsearch.providers.xai import XAIProvider
from allsearch.transport import HttpTransport

FIXTURE = Path(__file__).parent / "fixtures" / "xai_response.json"
ALT_FIXTURE = Path(__file__).parent / "fixtures" / "xai_response_alt_citations.json"


@pytest.mark.asyncio
@respx.mock
async def test_xai_payload_and_normalization():
    cfg = XAIConfig(
        api_key="test-key",
        base_url="https://api.x.ai/v1",
        responses_path="/responses",
        model="grok-4.20",
        fallback_models=("grok-4.3",),
        reasoning_effort="low",
        allowed_models=("grok-4.20", "grok-4.3"),
        max_tool_calls=4,
    )
    route = respx.post("https://api.x.ai/v1/responses").mock(
        return_value=httpx.Response(200, json=json.loads(FIXTURE.read_text()))
    )
    transport = HttpTransport(timeout_seconds=5, max_retries=0)
    provider = XAIProvider(cfg, transport)
    result = await provider.search(
        "quantum computing",
        max_results=5,
        include_domains=["example.com"],
    )
    assert route.called
    sent = json.loads(route.calls.last.request.content.decode())
    assert sent["model"] == "grok-4.20"
    assert sent["store"] is False
    assert sent["reasoning"] == {"effort": "low"}
    assert sent["max_tool_calls"] == 4
    assert sent["tools"][0]["type"] == "web_search"
    assert sent["tools"][0]["filters"]["allowed_domains"] == ["example.com"]
    assert "Bearer test-key" in route.calls.last.request.headers.get("Authorization", "")
    assert result.answer.startswith("Grok summary")
    assert len(result.citations) >= 2
    await transport.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_xai_falls_back_to_secondary_model_on_retryable_primary_failure():
    cfg = XAIConfig(
        api_key="test-key",
        base_url="https://gateway.example/v1",
        responses_path="/responses",
        model="grok-4.20-fast",
        fallback_models=("grok-4.3",),
        reasoning_effort="low",
        allowed_models=("grok-4.20-fast", "grok-4.3"),
        max_tool_calls=4,
    )
    seen_models: list[str] = []
    seen_bodies: list[dict] = []

    def side_effect(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen_bodies.append(body)
        seen_models.append(body["model"])
        if body["model"] == "grok-4.20-fast":
            return httpx.Response(500, json={"error": {"message": "upstream unavailable"}})
        return httpx.Response(200, json=json.loads(FIXTURE.read_text()))

    respx.post("https://gateway.example/v1/responses").mock(side_effect=side_effect)
    transport = HttpTransport(timeout_seconds=5, max_retries=0)
    provider = XAIProvider(cfg, transport)
    result = await provider.search("latest Python release", max_results=5)
    assert seen_models == ["grok-4.20-fast", "grok-4.3"]
    assert seen_bodies[0]["reasoning"] == {"effort": "low"}
    assert "reasoning" not in seen_bodies[1]
    assert all(body["max_tool_calls"] == 4 for body in seen_bodies)
    assert result.tool_usage["model"] == "grok-4.3"
    assert result.tool_usage["fallback_used"] is True
    assert result.tool_usage["attempted_models"] == ["grok-4.20-fast", "grok-4.3"]
    await transport.aclose()


def test_extract_annotations_fallback():
    payload = {
        "output": [
            {
                "content": [
                    {
                        "text": "body",
                        "annotations": [{"title": "Ann", "url": "https://ann.example/x"}],
                    }
                ]
            }
        ]
    }
    cites = XAIProvider.extract_citations(payload)
    assert cites[0]["url"] == "https://ann.example/x"


def test_extract_alternate_citation_fields():
    payload = json.loads(ALT_FIXTURE.read_text())
    cites = XAIProvider.extract_citations(payload)
    urls = {c["url"] for c in cites}
    assert "https://example.com/target" in urls
    assert "https://example.org/source" in urls
