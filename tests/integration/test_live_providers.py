"""Opt-in live provider smoke-test template (NOT real coverage).

These tests are placeholders: they are skipped by default and do not exercise
real providers. Enable with ALLSEARCH_RUN_LIVE_TESTS=1 AND inject real
credentials before use. They exist only as a starting point for an operator who
wants to add per-provider smoke checks against a live endpoint; the default
offline suite never touches the network and never reports these as coverage.
"""

from __future__ import annotations

import os

import pytest

RUN_LIVE = os.environ.get("ALLSEARCH_RUN_LIVE_TESTS", "0") == "1"


pytestmark = pytest.mark.live


@pytest.mark.skipif(not RUN_LIVE, reason="live tests disabled")
@pytest.mark.asyncio
async def test_live_placeholder():
    # Intentionally minimal: real credentials must be injected by the operator.
    # This is a template, not a live smoke — no provider is called and this test
    # must not be treated as evidence of live coverage. Expand per provider once
    # keys are available.
    pytest.skip("no live credentials configured in this environment")
