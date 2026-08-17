"""Resolves Watson's external, per-user data directory.

Watson ships as source in a public repo (and as a portable zip). Anything
that holds a user's own data - API keys, investigation history, generated
reports - must never default to a location inside that source tree, so it
can't end up committed, zipped into a release, or otherwise shipped
alongside the app by accident.

This module is the single place that decides where that data actually
lives. Everything else (config.py, db.py, engine_factory.py, app.py, gui.py,
file_guardian.py, ...) asks this module instead of hardcoding a relative
literal like "config.json" or "reports".

Precedence:
    1. WATSON_DATA_DIR env var, if set - lets tests and portable installs
       point the whole app back at an explicit directory (e.g. the portable
       app's own folder, to opt back into fully self-contained behavior).
    2. The OS-conventional per-user data directory:
       - Windows: %LOCALAPPDATA%\\Watson
       - macOS:   ~/Library/Application Support/Watson
       - Linux/other: $XDG_DATA_HOME/watson or ~/.local/share/watson

App assets that ship with the source tree (templates/, sources.json,
system-prompt.md, requirements.txt, .venv/) are NOT covered here - those
stay resolved against Portable_Root (see integrity.py:resolve_portable_root)
because they're part of the app, not user data.
"""

import os
import shutil
import sys
from pathlib import Path

_APP_NAME = "Watson"


def data_dir() -> Path:
    """Return the external, per-user directory for Watson's own runtime data.

    Created on disk (including parents) if it doesn't already exist.
    """
    override = os.environ.get("WATSON_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / _APP_NAME
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / _APP_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        path = Path(base) / _APP_NAME.lower()

    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def config_path() -> Path:
    """Path to config.json (local LLM backend config, API keys)."""
    return data_dir() / "config.json"


def db_path() -> Path:
    """Path to investigations.db (SQLite: history, steering index, plans, KB)."""
    return data_dir() / "investigations.db"


def reports_dir() -> Path:
    """Directory generated HTML/Markdown investigation reports are written to."""
    path = data_dir() / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def migrate_legacy_data(portable_root: Path) -> list[str]:
    """One-time move of pre-existing user data from portable_root into data_dir().

    Older Watson versions read/wrote config.json, investigations.db, and
    reports/ directly inside the app's own source tree. Now that those
    default to the external data_dir() instead, anything still sitting at
    the old portable_root location would otherwise look "missing" on first
    launch under the new default - silently dropping the user's configured
    LLM backend and investigation history into a fresh setup wizard.

    This moves each legacy file/entry over, once. Idempotent and
    non-destructive: a file is only moved when the destination doesn't
    already exist, and this always moves (never copies-then-deletes, so
    there's never a window with two live copies to fall out of sync).

    Must only be called explicitly by a real entry point (gui.py, main.py's
    `__main__` block) - never at import time and never from paths.py itself
    - so it can't fire during test collection against a sandboxed/patched
    portable_root.

    Args:
        portable_root: The app's own source-tree root to look for legacy
            data in (see integrity.py:resolve_portable_root).

    Returns:
        Human-readable "what moved where" strings, for startup logging.
        Empty when there was nothing to migrate.
    """
    moved: list[str] = []
    dest_dir = data_dir()

    for name in ("config.json", "investigations.db", "investigations.db-wal", "investigations.db-shm"):
        legacy = portable_root / name
        dest = dest_dir / name
        if legacy.is_file() and not dest.exists():
            shutil.move(str(legacy), str(dest))
            moved.append(f"{name} -> {dest}")

    legacy_reports = portable_root / "reports"
    if legacy_reports.is_dir():
        dest_reports = reports_dir()
        for entry in legacy_reports.iterdir():
            dest_entry = dest_reports / entry.name
            if not dest_entry.exists():
                shutil.move(str(entry), str(dest_entry))
                moved.append(f"reports/{entry.name} -> {dest_entry}")

    return moved
