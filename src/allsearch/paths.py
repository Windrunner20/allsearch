"""Per-platform state and config directories without extra dependencies.

Linux and other POSIX systems follow XDG. macOS uses ``~/Library/Application
Support``. Windows uses ``%LOCALAPPDATA%`` for state and ``%APPDATA%`` for config.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP = "allsearch"


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else None


def state_dir() -> Path:
    if sys.platform == "win32":
        base = _env_path("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return base / APP
    if sys.platform == "darwin" and _env_path("XDG_STATE_HOME") is None:
        return Path.home() / "Library" / "Application Support" / APP
    base = _env_path("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return base / APP


def config_dir() -> Path:
    if sys.platform == "win32":
        base = _env_path("APPDATA") or Path.home() / "AppData" / "Roaming"
        return base / APP
    if sys.platform == "darwin" and _env_path("XDG_CONFIG_HOME") is None:
        return Path.home() / "Library" / "Application Support" / APP
    base = _env_path("XDG_CONFIG_HOME") or Path.home() / ".config"
    return base / APP


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` and restrict it to the owner where the OS supports modes.

    On Windows ``chmod`` only toggles the read-only bit, so the directory keeps
    the ACL inherited from the user profile, which is already per-user.
    """
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)
    return path
