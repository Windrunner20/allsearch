"""Private on-disk store for full search and fetch responses.

Compact MCP tools return a bounded summary plus a result id. The full payload
stays here, with a TTL, an entry cap, a byte cap, and owner-only permissions.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

_ID_RE = re.compile(r"^[0-9a-f]{16}$")


def default_record_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "allsearch" / "records"


class RecordStore:
    def __init__(
        self,
        directory: str | Path | None,
        *,
        ttl_seconds: int = 3600,
        max_entries: int = 200,
        max_bytes: int = 33_554_432,
    ) -> None:
        self.directory = Path(directory) if directory else default_record_dir()
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)

    def put(self, kind: str, response: dict[str, Any], *, now: float | None = None) -> str:
        with self._lock:
            return self._put_locked(kind, response, now=now)

    def _put_locked(self, kind: str, response: dict[str, Any], *, now: float | None) -> str:
        created = time.time() if now is None else now
        self._cleanup_locked(now=created)
        result_id = secrets.token_hex(8)
        payload = {
            "result_id": result_id,
            "kind": kind,
            "created_at": created,
            "ttl_seconds": self.ttl_seconds,
            "response": response,
        }
        path = self._path(result_id)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(path, 0o600)
        self._cleanup_locked(now=created)
        return result_id

    def get(self, result_id: str, *, now: float | None = None) -> dict[str, Any] | None:
        with self._lock:
            return self._get_locked(result_id, now=now)

    def _get_locked(self, result_id: str, *, now: float | None) -> dict[str, Any] | None:
        if not _ID_RE.fullmatch(result_id or ""):
            return None
        path = self._path(result_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
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

    def _cleanup_locked(self, *, now: float | None) -> int:
        current = time.time() if now is None else now
        files = [path for path in self.directory.glob("*.json") if path.is_file()]
        kept: list[tuple[float, int, Path]] = []
        removed = 0
        for path in files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                created = float(payload.get("created_at") or path.stat().st_mtime)
                ttl = float(payload.get("ttl_seconds") or self.ttl_seconds)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                path.unlink(missing_ok=True)
                removed += 1
                continue
            if current - created > ttl:
                path.unlink(missing_ok=True)
                removed += 1
                continue
            kept.append((created, path.stat().st_size, path))
        kept.sort(key=lambda item: item[0])
        total = sum(size for _, size, _ in kept)
        while kept and (len(kept) > self.max_entries or total > self.max_bytes):
            _created, size, path = kept.pop(0)
            path.unlink(missing_ok=True)
            total -= size
            removed += 1
        return removed

    def _path(self, result_id: str) -> Path:
        if not _ID_RE.fullmatch(result_id):
            raise ValueError("invalid result id")
        return self.directory / f"{result_id}.json"
