"""Shared absolute search deadline with per-phase hard timeouts.

A single :class:`Deadline` instance is created once per ``search`` call and
every phase (group 0 primary, group 1 supplements, post-merge scrape) runs
under ``asyncio.timeout_at(expires_at)`` so the whole search is bounded by one
absolute budget. Providers keep their existing ``deadline_seconds`` parameter
(remaining seconds) so their internal retry budgets stay consistent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


def _loop_time() -> float:
    return asyncio.get_running_loop().time()


@dataclass(slots=True)
class Deadline:
    """A monotonic absolute deadline shared across all search phases.

    ``clock`` is injectable for deterministic tests; by default it reads the
    running event loop's monotonic clock (the same one ``asyncio.timeout_at``
    uses, so injected loop clocks remain consistent).
    """

    total_seconds: float
    clock: Callable[[], float] | None = None
    _clock: Callable[[], float] = field(init=False, default=_loop_time)
    started_at: float = field(init=False, default=0.0)
    expires_at: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self._clock = self.clock or _loop_time
        self.started_at = float(self._clock())
        self.expires_at = self.started_at + self.total_seconds

    @property
    def remaining(self) -> float:
        return max(0.0, self.expires_at - float(self._clock()))

    @property
    def expired(self) -> bool:
        return float(self._clock()) >= self.expires_at

    @property
    def timeout(self) -> Any:
        """Async context manager that hard-times-out at the shared deadline."""
        return asyncio.timeout_at(self.expires_at)
