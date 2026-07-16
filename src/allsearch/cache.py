"""Bounded in-memory TTL cache with deep-copy returns."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any


def stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class CacheStats:
    hits: int = 0
    misses: int = 0
    sets: int = 0
    evictions: int = 0
    entries: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "sets": self.sets,
            "evictions": self.evictions,
            "entries": self.entries,
        }


@dataclass(slots=True)
class _Entry:
    value: Any
    expires_at: float
    created_at: float


@dataclass
class MemoryCache:
    max_entries: int = 512
    clock: Any = field(default=time.monotonic)

    def __post_init__(self) -> None:
        self._data: dict[str, _Entry] = {}
        self.stats = CacheStats()

    def _now(self) -> float:
        return float(self.clock())

    def _purge_expired(self) -> None:
        now = self._now()
        expired = [k for k, e in self._data.items() if e.expires_at <= now]
        for key in expired:
            del self._data[key]
            self.stats.evictions += 1
        self.stats.entries = len(self._data)

    def _evict_if_needed(self) -> None:
        while len(self._data) >= self.max_entries and self._data:
            # Evict oldest by created_at
            oldest = min(self._data.items(), key=lambda kv: kv[1].created_at)
            del self._data[oldest[0]]
            self.stats.evictions += 1
        self.stats.entries = len(self._data)

    def make_key(self, namespace: str, payload: Any) -> str:
        return f"{namespace}:{stable_hash(payload)}"

    def get(self, key: str) -> Any | None:
        self._purge_expired()
        entry = self._data.get(key)
        if entry is None:
            self.stats.misses += 1
            return None
        if entry.expires_at <= self._now():
            del self._data[key]
            self.stats.misses += 1
            self.stats.evictions += 1
            self.stats.entries = len(self._data)
            return None
        self.stats.hits += 1
        return copy.deepcopy(entry.value)

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        self._purge_expired()
        self._evict_if_needed()
        now = self._now()
        self._data[key] = _Entry(
            value=copy.deepcopy(value),
            expires_at=now + float(ttl_seconds),
            created_at=now,
        )
        self.stats.sets += 1
        self.stats.entries = len(self._data)

    def delete(self, key: str) -> None:
        if key in self._data:
            del self._data[key]
            self.stats.entries = len(self._data)

    def clear_namespace(self, namespace: str) -> int:
        prefix = f"{namespace}:"
        keys = [k for k in self._data if k.startswith(prefix)]
        for key in keys:
            del self._data[key]
        self.stats.entries = len(self._data)
        return len(keys)

    def age_seconds(self, key: str) -> float | None:
        entry = self._data.get(key)
        if not entry:
            return None
        return max(0.0, self._now() - entry.created_at)

    def snapshot(self) -> dict[str, Any]:
        self._purge_expired()
        return self.stats.to_dict()
