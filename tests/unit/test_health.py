from __future__ import annotations

import pytest

from allsearch.errors import CircuitOpenError
from allsearch.health import HealthRegistry
from tests.conftest import FakeClock


def test_circuit_opens_and_half_open():
    clock = FakeClock(0.0)
    reg = HealthRegistry(failure_threshold=2, open_seconds=10, clock=clock)
    reg.register("xai", True)
    reg.record_failure("xai", "timeout", retryable=True)
    reg.record_failure("xai", "timeout", retryable=True)
    with pytest.raises(CircuitOpenError):
        reg.ensure_closed_or_raise("xai")
    clock.advance(11)
    reg.ensure_closed_or_raise("xai")  # half-open allowed
    assert reg.get("xai").state == "half_open"
    reg.record_success("xai", 12)
    assert reg.get("xai").state == "healthy"


@pytest.mark.asyncio
async def test_probe_collapse_and_cache():
    reg = HealthRegistry(probe_ttl_seconds=60)
    calls = {"n": 0}

    async def probe():
        calls["n"] += 1
        return {"ok": True}

    a = await reg.probe_once("tavily", probe)
    b = await reg.probe_once("tavily", probe)
    assert a == b == {"ok": True}
    assert calls["n"] == 1
