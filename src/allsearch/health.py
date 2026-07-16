"""Passive provider metrics, probes, and circuit breakers."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from allsearch.cache import MemoryCache
from allsearch.errors import CircuitOpenError


@dataclass(slots=True)
class ProviderState:
    name: str
    configured: bool
    state: str = "idle"  # idle|healthy|degraded|open|half_open|unconfigured
    last_success_at: float | None = None
    last_failure_at: float | None = None
    last_error_code: str | None = None
    consecutive_failures: int = 0
    latency_ms_ewma: float | None = None
    probe_at: float | None = None
    detail: str | None = None
    circuit_opened_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "configured": self.configured,
            "state": self.state if self.configured else "unconfigured",
            "last_success_at": self.last_success_at,
            "last_error_code": self.last_error_code,
            "consecutive_failures": self.consecutive_failures,
            "latency_ms_ewma": self.latency_ms_ewma,
            "probe_at": self.probe_at,
            "detail": self.detail,
        }


@dataclass
class HealthRegistry:
    failure_threshold: int = 3
    open_seconds: int = 60
    probe_ttl_seconds: int = 300
    cache: MemoryCache | None = None
    clock: Any = field(default=time.time)

    def __post_init__(self) -> None:
        self._providers: dict[str, ProviderState] = {}
        self._probe_locks: dict[str, asyncio.Lock] = {}
        self._inflight_probes: dict[str, asyncio.Future[Any]] = {}
        if self.cache is None:
            self.cache = MemoryCache()

    def _now(self) -> float:
        return float(self.clock())

    def register(self, name: str, configured: bool) -> None:
        state = self._providers.get(name)
        if state is None:
            self._providers[name] = ProviderState(
                name=name,
                configured=configured,
                state="idle" if configured else "unconfigured",
            )
        else:
            state.configured = configured
            if not configured:
                state.state = "unconfigured"

    def get(self, name: str) -> ProviderState | None:
        return self._providers.get(name)

    def all_states(self) -> list[ProviderState]:
        return list(self._providers.values())

    def record_success(self, name: str, latency_ms: float | None = None) -> None:
        state = self._providers.setdefault(
            name, ProviderState(name=name, configured=True, state="healthy")
        )
        state.configured = True
        state.state = "healthy"
        state.last_success_at = self._now()
        state.consecutive_failures = 0
        state.last_error_code = None
        state.circuit_opened_at = None
        if latency_ms is not None:
            if state.latency_ms_ewma is None:
                state.latency_ms_ewma = float(latency_ms)
            else:
                state.latency_ms_ewma = (0.7 * state.latency_ms_ewma) + (0.3 * float(latency_ms))

    def record_failure(self, name: str, code: str, *, retryable: bool = True) -> None:
        state = self._providers.setdefault(
            name, ProviderState(name=name, configured=True, state="degraded")
        )
        state.last_failure_at = self._now()
        state.last_error_code = code
        if retryable:
            state.consecutive_failures += 1
            if state.consecutive_failures >= self.failure_threshold:
                state.state = "open"
                state.circuit_opened_at = self._now()
            else:
                state.state = "degraded"
        else:
            # auth/config failures do not thrash the circuit open forever, but mark degraded
            state.state = "degraded"

    def ensure_closed_or_raise(self, name: str) -> None:
        state = self._providers.get(name)
        if state is None:
            return
        if state.state != "open":
            return
        opened = state.circuit_opened_at or 0.0
        if self._now() - opened >= self.open_seconds:
            state.state = "half_open"
            return
        raise CircuitOpenError(f"circuit open for provider {name}", provider=name)

    def is_available(self, name: str) -> bool:
        state = self._providers.get(name)
        if state is None:
            return True
        if not state.configured:
            return False
        if state.state != "open":
            return True
        opened = state.circuit_opened_at or 0.0
        if self._now() - opened >= self.open_seconds:
            state.state = "half_open"
            return True
        return False

    async def probe_once(
        self,
        name: str,
        probe_fn: Callable[[], Awaitable[Any]],
        *,
        force: bool = False,
    ) -> Any:
        """Run a probe with TTL cache and in-flight collapse."""
        assert self.cache is not None
        cache_key = self.cache.make_key("probe", {"provider": name})
        if not force:
            cached = self.cache.get(cache_key)
            if cached is not None:
                return cached

        if name in self._inflight_probes:
            return await self._inflight_probes[name]

        loop = asyncio.get_event_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._inflight_probes[name] = fut
        try:
            result = await probe_fn()
            self.cache.set(cache_key, result, self.probe_ttl_seconds)
            state = self._providers.get(name)
            if state:
                state.probe_at = self._now()
            fut.set_result(result)
            return result
        except Exception as exc:
            fut.set_exception(exc)
            raise
        finally:
            self._inflight_probes.pop(name, None)
