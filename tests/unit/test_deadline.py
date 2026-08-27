"""Deadline unit tests: injected clocks and loop-clock-driven timeouts."""

from __future__ import annotations

import asyncio

import pytest

from allsearch.deadline import Deadline
from tests.conftest import FakeClock


def test_deadline_remaining_and_expired_with_injected_clock():
    clock = FakeClock(100.0)
    d = Deadline(10.0, clock=clock)
    assert d.expires_at == 110.0
    assert d.remaining == 10.0
    assert not d.expired
    clock.advance(5.0)
    assert d.remaining == pytest.approx(5.0)
    clock.advance(5.2)
    assert d.remaining == 0.0
    assert d.expired


def test_deadline_remaining_never_negative():
    clock = FakeClock(0.0)
    d = Deadline(3.0, clock=clock)
    clock.advance(100.0)
    assert d.remaining == 0.0
    assert d.expired


@pytest.mark.asyncio
async def test_deadline_timeout_fires_after_loop_clock_passes(loop_clock):
    d = Deadline(1.0)  # expires_at = clock(0) + 1
    loop_clock.advance(2.0)  # clock already past the deadline
    with pytest.raises(asyncio.TimeoutError):
        async with d.timeout:
            await asyncio.sleep(60)  # cancelled immediately, no real wait


@pytest.mark.asyncio
async def test_deadline_timeout_does_not_fire_before_deadline(loop_clock):
    d = Deadline(10.0)  # expires_at = 10
    completed = False
    async with d.timeout:
        await asyncio.sleep(0)  # yields; clock still at 0 < 10
        completed = True
    assert completed
