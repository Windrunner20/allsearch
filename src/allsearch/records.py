"""Private on-disk store for full search and fetch responses.

Compact MCP tools return a bounded summary plus a result id. The full payload
stays here, with a TTL, an entry cap, a byte cap, and owner-only permissions.

Every record belongs to an owner (the authenticated principal, or ``local``
for stdio). The owner is hashed into the file name, so a ``result_id`` that
leaks to another principal resolves to a different path and reads as unknown.
Writes go through a temporary file and an atomic rename, so concurrent
processes sharing the directory never observe a partial record.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from allsearch.paths import ensure_private_dir, state_dir

# 0.2 issued 16 hex characters; 0.3 issues 24. Both shapes are accepted.
_ID_RE = re.compile(r"^[0-9a-f]{16,32}$")
_FILE_RE = re.compile(r"^[0-9a-f]{16}_[0-9a-f]{16,32}\.json$")
LOCAL_OWNER = "local"


def default_record_dir() -> Path:
    return state_dir() / "records"


def owner_key(owner: str) -> str:
    return hashlib.sha256((owner or LOCAL_OWNER).encode("utf-8")).hexdigest()[:16]


class RecordStore:
    def __init__(
        self,
        directory: str | Path | None,
        *,
        ttl_seconds: int = 3600,
        max_entries: int = 200,
        max_bytes: int = 33_554_432,
        max_entries_per_owner: int | None = None,
    ) -> None:
        self.directory = Path(directory) if directory else default_record_dir()
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_entries_per_owner = max_entries_per_owner or max_entries
        self._lock = threading.RLock()
        ensure_private_dir(self.directory)

    def put(
        self,
        kind: str,
        response: dict[str, Any],
        *,
        owner: str = LOCAL_OWNER,
        now: float | None = None,
    ) -> str:
        with self._lock:
            created = time.time() if now is None else now
            result_id = secrets.token_hex(12)
            payload = {
                "result_id": result_id,
                "kind": kind,
                "owner": owner_key(owner),
                "created_at": created,
                "ttl_seconds": self.ttl_seconds,
                "response": response,
            }
            self._write_atomic(self._path(owner, result_id), payload, created)
            self._cleanup_locked(now=created)
            return result_id

    def get(
        self,
        result_id: str,
        *,
        owner: str = LOCAL_OWNER,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(result_id, str) or not _ID_RE.fullmatch(result_id):
            return None
        path = self._path(owner, result_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("owner") != owner_key(owner):
            return None
        created = float(payload.get("created_at") or 0)
        ttl = float(payload.get("ttl_seconds") or self.ttl_seconds)
        current = time.time() if now is None else now
        if current - created > ttl:
            path.unlink(missing_ok=True)
            return None
        return payload

    def cleanup(self, *, now: float | None = None) -> int:
        with self._lock:
            return self._cleanup_locked(now=now)

    def _write_atomic(self, path: Path, payload: dict[str, Any], created: float) -> None:
        tmp = path.with_name(f".tmp-{secrets.token_hex(6)}-{path.name}")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            # mtime carries the creation time so cleanup never parses JSON.
            os.utime(tmp, (created, created))
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _cleanup_locked(self, *, now: float | None) -> int:
        current = time.time() if now is None else now
        kept: list[tuple[float, int, str, Path]] = []
        removed = 0
        for path in self.directory.iterdir():
            name = path.name
            try:
                stat = path.stat()
            except OSError:
                continue
            if name.startswith(".tmp-"):
                # Orphans from a crashed writer. Live temp files are seconds old.
                if current - stat.st_mtime > 300:
                    path.unlink(missing_ok=True)
                continue
            if not name.endswith(".json"):
                continue
            if not _FILE_RE.fullmatch(name) or current - stat.st_mtime > self.ttl_seconds:
                # Expired, or a 0.2-era record that no owner can address.
                path.unlink(missing_ok=True)
                removed += 1
                continue
            kept.append((stat.st_mtime, stat.st_size, name.split("_", 1)[0], path))
        kept.sort(key=lambda item: item[0])

        per_owner: dict[str, int] = {}
        for _, _, owner, _ in kept:
            per_owner[owner] = per_owner.get(owner, 0) + 1
        survivors: list[tuple[float, int, str, Path]] = []
        for item in kept:
            owner = item[2]
            if per_owner[owner] > self.max_entries_per_owner:
                item[3].unlink(missing_ok=True)
                per_owner[owner] -= 1
                removed += 1
            else:
                survivors.append(item)

        total = sum(size for _, size, _, _ in survivors)
        while survivors and (len(survivors) > self.max_entries or total > self.max_bytes):
            _, size, _, path = survivors.pop(0)
            path.unlink(missing_ok=True)
            total -= size
            removed += 1
        return removed

    def _path(self, owner: str, result_id: str) -> Path:
        if not _ID_RE.fullmatch(result_id):
            raise ValueError("invalid result id")
        return self.directory / f"{owner_key(owner)}_{result_id}.json"
