from __future__ import annotations

from tests.conftest import FakeClock
from allsearch.cache import MemoryCache


def test_ttl_expiry_and_deepcopy():
    clock = FakeClock(0.0)
    cache = MemoryCache(max_entries=10, clock=clock)
    key = cache.make_key("search", {"q": "a"})
    value = {"results": [1]}
    cache.set(key, value, ttl_seconds=5)
    value["results"].append(2)
    got = cache.get(key)
    assert got == {"results": [1]}
    got["results"].append(3)
    assert cache.get(key) == {"results": [1]}
    clock.advance(6)
    assert cache.get(key) is None
    assert cache.stats.misses >= 1


def test_eviction_and_deterministic_keys():
    cache = MemoryCache(max_entries=2)
    k1 = cache.make_key("ns", {"a": 1})
    k2 = cache.make_key("ns", {"a": 1})
    assert k1 == k2
    cache.set("a", 1, 60)
    cache.set("b", 2, 60)
    cache.set("c", 3, 60)
    assert cache.stats.evictions >= 1
    assert cache.stats.entries <= 2


def test_fresh_bypass_semantics_zero_ttl():
    cache = MemoryCache()
    cache.set("k", {"x": 1}, ttl_seconds=0)
    assert cache.get("k") is None
