from __future__ import annotations

import os
import sys
import time

import pytest

from allsearch.records import RecordStore, owner_key


def test_result_id_is_scoped_to_its_owner(tmp_path):
    store = RecordStore(tmp_path, ttl_seconds=60, max_entries=10)
    rid = store.put("search", {"answer": "alice's"}, owner="alice")
    assert store.get(rid, owner="alice")["response"]["answer"] == "alice's"
    assert store.get(rid, owner="bob") is None
    assert store.get(rid) is None  # local stdio owner is a different principal


def test_forged_owner_field_inside_file_is_rejected(tmp_path):
    store = RecordStore(tmp_path, ttl_seconds=60, max_entries=10)
    rid = store.put("search", {"answer": "x"}, owner="alice")
    [path] = list(tmp_path.glob("*.json"))
    # A file placed under bob's prefix but carrying alice's owner hash is not bob's.
    forged = tmp_path / f"{owner_key('bob')}_{rid}.json"
    forged.write_bytes(path.read_bytes())
    assert store.get(rid, owner="bob") is None


def test_per_owner_cap_does_not_evict_other_owners(tmp_path):
    store = RecordStore(tmp_path, ttl_seconds=600, max_entries=50, max_entries_per_owner=3)
    base = time.time()
    keep = store.put("search", {"n": "bob"}, owner="bob", now=base)
    ids = [store.put("search", {"n": i}, owner="alice", now=base + 1 + i) for i in range(10)]
    assert store.get(keep, owner="bob", now=base + 20) is not None
    alive = [rid for rid in ids if store.get(rid, owner="alice", now=base + 20) is not None]
    assert alive == ids[-3:]


def test_legacy_and_orphan_files_are_cleaned(tmp_path):
    store = RecordStore(tmp_path, ttl_seconds=600, max_entries=50)
    legacy = tmp_path / ("a" * 16 + ".json")
    legacy.write_text("{}", encoding="utf-8")
    orphan = tmp_path / ".tmp-abc-x.json"
    orphan.write_text("partial", encoding="utf-8")
    old = time.time() - 3600
    os.utime(orphan, (old, old))
    store.put("search", {"n": 1})
    assert not legacy.exists()
    assert not orphan.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_new_files_are_never_world_readable(tmp_path):
    old = os.umask(0)
    try:
        store = RecordStore(tmp_path / "r", ttl_seconds=60, max_entries=10)
        store.put("fetch", {"content": "secret page"})
    finally:
        os.umask(old)
    assert ((tmp_path / "r").stat().st_mode & 0o777) == 0o700
    for path in (tmp_path / "r").iterdir():
        assert (path.stat().st_mode & 0o777) == 0o600


def test_unknown_and_malformed_ids(tmp_path):
    store = RecordStore(tmp_path, ttl_seconds=60, max_entries=10)
    for raw in ("", "zz" * 12, "a" * 15, "a" * 33, None, 123):
        assert store.get(raw) is None  # type: ignore[arg-type]
