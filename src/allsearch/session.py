"""Per-session call ledger that lets any MCP client see when it is looping.

The server cannot read the agent's plan, but it can see the calls. For each
session (stdio process, or HTTP principal plus ``Mcp-Session-Id``) it records
which searches, fetches, and result pages were already served, which URLs
have been seen, and how many consecutive calls produced nothing new. That
state goes back to the client in a small ``session`` block, and identical
calls inside the window are answered from the stored record instead of
spending provider budget again.

Budgets are rolling windows, not lifetime caps, because one stdio process may
serve many unrelated tasks over a day.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from allsearch.security import canonicalize_url

STALE_ADVICE_AFTER = 3


@dataclass(slots=True)
class _Seen:
    result_id: str
    at: float
    count: int = 1


@dataclass(slots=True)
class SessionState:
    key: str
    last_seen: float
    searches: dict[str, _Seen] = field(default_factory=dict)
    fetches: dict[str, _Seen] = field(default_factory=dict)
    reads: dict[str, int] = field(default_factory=dict)
    urls: set[str] = field(default_factory=set)
    read_content: set[str] = field(default_factory=set)
    search_times: deque[float] = field(default_factory=deque)
    fetch_times: deque[float] = field(default_factory=deque)
    totals: dict[str, int] = field(default_factory=lambda: {"search": 0, "fetch": 0, "read_result": 0})
    stale: int = 0


@dataclass(slots=True)
class SessionLimits:
    window_seconds: int = 900
    max_searches: int = 40
    max_fetches: int = 80
    repeat_window_seconds: int = 900
    idle_seconds: int = 3600
    max_sessions: int = 2048


def search_key(arguments: dict[str, Any]) -> str:
    query = " ".join(str(arguments.get("query") or "").split()).casefold()
    parts = [
        query,
        str(arguments.get("mode") or "auto"),
        str(arguments.get("depth") or "balanced"),
        str(arguments.get("max_results") or 8),
        ",".join(sorted(d.casefold() for d in arguments.get("include_domains") or [])),
        ",".join(sorted(d.casefold() for d in arguments.get("exclude_domains") or [])),
        str(arguments.get("vertical") or ""),
    ]
    return "\x1f".join(parts)


def fetch_key(url: str, focus: str | None, max_chars: int) -> str:
    return "\x1f".join([canonicalize_url(url) or url, (focus or "").casefold(), str(max_chars)])


class SessionRegistry:
    def __init__(self, limits: SessionLimits | None = None, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits or SessionLimits()
        self.clock = clock
        self._sessions: OrderedDict[str, SessionState] = OrderedDict()

    def get(self, key: str) -> SessionState:
        now = self.clock()
        state = self._sessions.get(key)
        if state is None or now - state.last_seen > self.limits.idle_seconds:
            state = SessionState(key=key, last_seen=now)
            self._sessions[key] = state
        state.last_seen = now
        self._sessions.move_to_end(key)
        while len(self._sessions) > self.limits.max_sessions:
            self._sessions.popitem(last=False)
        return state

    # -- repeat detection ---------------------------------------------------
    def previous(self, state: SessionState, table: str, key: str) -> _Seen | None:
        entry = getattr(state, table).get(key)
        if entry is None or self.clock() - entry.at > self.limits.repeat_window_seconds:
            return None
        return entry

    def remember(self, state: SessionState, table: str, key: str, result_id: str) -> None:
        entries: dict[str, _Seen] = getattr(state, table)
        old = entries.get(key)
        entries[key] = _Seen(result_id=result_id, at=self.clock(), count=(old.count + 1) if old else 1)
        if len(entries) > 512:
            for stale_key in list(entries)[:128]:
                entries.pop(stale_key, None)

    # -- budgets ------------------------------------------------------------
    def _trim(self, times: deque[float]) -> None:
        cutoff = self.clock() - self.limits.window_seconds
        while times and times[0] < cutoff:
            times.popleft()

    def over_budget(self, state: SessionState, kind: str) -> int | None:
        """Return the window limit when one more provider call would exceed it."""
        times = state.search_times if kind == "search" else state.fetch_times
        limit = self.limits.max_searches if kind == "search" else self.limits.max_fetches
        if limit <= 0:
            return None
        self._trim(times)
        return limit if len(times) >= limit else None

    def charge(self, state: SessionState, kind: str) -> None:
        times = state.search_times if kind == "search" else state.fetch_times
        times.append(self.clock())

    # -- evidence accounting ------------------------------------------------
    def observe(
        self,
        state: SessionState,
        tool: str,
        *,
        urls: Iterable[str] = (),
        content_urls: Iterable[str] = (),
        repeat: bool = False,
    ) -> dict[str, Any]:
        state.totals[tool] = state.totals.get(tool, 0) + 1
        new_urls = 0
        for url in urls:
            canon = canonicalize_url(url) or url
            if canon and canon not in state.urls:
                state.urls.add(canon)
                new_urls += 1
        new_content = 0
        for url in content_urls:
            canon = canonicalize_url(url) or url
            if canon and canon not in state.read_content:
                state.read_content.add(canon)
                new_content += 1
        progressed = (new_urls + new_content) > 0 and not repeat
        state.stale = 0 if progressed else state.stale + 1
        block: dict[str, Any] = {
            "calls": dict(state.totals),
            "new_urls": new_urls,
            "new_pages_read": new_content,
            "seen_urls": len(state.urls),
            "pages_read": len(state.read_content),
            "calls_without_new_evidence": state.stale,
        }
        if repeat:
            block["repeat"] = True
        return block

    def note_read(self, state: SessionState, page_key: str) -> int:
        count = state.reads.get(page_key, 0) + 1
        state.reads[page_key] = count
        if len(state.reads) > 2048:
            for stale_key in list(state.reads)[:512]:
                state.reads.pop(stale_key, None)
        return count


def advice(block: dict[str, Any], *, repeat_of: str | None = None) -> str | None:
    if repeat_of:
        return (
            f"Identical call already answered in this session as result_id {repeat_of}; "
            "nothing new was fetched. Use read_result on it, or change the query."
        )
    if int(block.get("calls_without_new_evidence") or 0) >= STALE_ADVICE_AFTER:
        return (
            "The last calls found no new URLs or pages. Answer with the evidence already "
            "found and say what is missing, or change the site, language, or wording."
        )
    return None
