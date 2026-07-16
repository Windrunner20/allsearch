"""Opt-in live provider smoke tests. Disabled unless ALLSEARCH_RUN_LIVE_TESTS=1."""

from __future__ import annotations

import os

import pytest

RUN_LIVE = os.environ.get("ALLSEARCH_RUN_LIVE_TESTS", "0") == "1"


pytestmark = pytest.mark.live


@pytest.mark.skipif(not RUN_LIVE, reason="live tests disabled")
@pytest.mark.asyncio
async def test_live_placeholder():
    # Intentionally minimal: real credentials must be injected by the operator.
    # Individual provider smokes can be expanded once keys are available.
    pytest.skip("no live credentials configured in this environment")
