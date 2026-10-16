from __future__ import annotations

from allsearch.session import SessionLimits, SessionRegistry, advice, fetch_key, search_key


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_search_key_ignores_case_and_whitespace_but_not_parameters():
    a = search_key({"query": "  SQLite   file format ", "depth": "balanced"})
    b = search_key({"query": "sqlite file format", "depth": "balanced"})
    c = search_key({"query": "sqlite file format", "depth": "deep"})
    assert a == b != c


def test_fetch_key_canonicalizes_tracking_parameters():
    assert fetch_key("https://e.com/a?utm_source=x", None, 100) == fetch_key("https://e.com/a", None, 100)


def test_stale_counter_and_advice():
    clock = Clock()
    reg = SessionRegistry(SessionLimits(), clock=clock)
    state = reg.get("s")
    first = reg.observe(state, "search", urls=["https://a.com/1", "https://a.com/2"])
    assert first["new_urls"] == 2 and first["calls_without_new_evidence"] == 0
    for _ in range(3):
        block = reg.observe(state, "search", urls=["https://a.com/1"])
    assert block["calls_without_new_evidence"] == 3
    assert "no new" in advice(block)
    reset = reg.observe(state, "fetch", urls=["https://a.com/1"], content_urls=["https://a.com/1"])
    assert reset["calls_without_new_evidence"] == 0
    assert reset["pages_read"] == 1


def test_rolling_budget_recovers_after_window():
    clock = Clock()
    reg = SessionRegistry(SessionLimits(window_seconds=60, max_searches=2), clock=clock)
    state = reg.get("s")
    for _ in range(2):
        assert reg.over_budget(state, "search") is None
        reg.charge(state, "search")
    assert reg.over_budget(state, "search") == 2
    clock.t += 61
    assert reg.over_budget(state, "search") is None


def test_repeat_window_expires():
    clock = Clock()
    reg = SessionRegistry(SessionLimits(repeat_window_seconds=30), clock=clock)
    state = reg.get("s")
    reg.remember(state, "searches", "k", "a" * 24)
    assert reg.previous(state, "searches", "k").result_id == "a" * 24
    clock.t += 31
    assert reg.previous(state, "searches", "k") is None


def test_sessions_are_isolated_and_bounded():
    clock = Clock()
    reg = SessionRegistry(SessionLimits(max_sessions=2), clock=clock)
    a = reg.get("a")
    reg.observe(a, "search", urls=["https://x.com/"])
    b = reg.get("b")
    assert b.urls == set()
    reg.get("c")
    assert "a" not in reg._sessions
