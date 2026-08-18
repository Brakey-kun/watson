"""Full data wipe: hard-deletes every row this app owns from the shared
SQLite connection, plus every generated report file. Backs the destructive
"Reset" action in Settings (`POST /api/reset-all-data` in admin_routes.py).

Deliberately does NOT touch config.json (LLM backend credentials/tuning) --
"start from scratch" means data, not the setup wizard's work; the
frontend's confirm dialog says so explicitly so that's a stated choice,
not an oversight.

Enumerates tables from sqlite_master at call time rather than a hardcoded
list, so this stays exhaustive if a table is ever added later without
this module needing a matching update -- unlike SteeringIndex.clear_scope(),
which explicitly no-ops on GLOBAL_SCOPE (by design, for query/hint decay)
and would silently leave that whole pile behind if reused here. This is a
different operation: an unconditional wipe, not a scoped decay-preserving
clear.

Callers MUST already hold the same threading.Lock their host guards
is_running with before calling reset_all_tables() -- see admin_routes.py's
create_admin_blueprint docstring for why sharing that lock (not a second
one) is what actually closes the race where a run starts mid-wipe.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from . import db, paths

logger = logging.getLogger(__name__)


def reset_all_tables(db_path: Optional[str] = None) -> dict[str, int]:
    """Hard-delete every row from every table in the shared SQLite db.

    Returns {table_name: rows_deleted}. Runs under db.write_lock() like
    every other writer in this app (StateManager, SteeringIndex, etc. all
    share the same connection for this db_path -- see db.py), so this
    can't race a concurrent write from one of them either.
    """
    conn = db.get_connection(db_path)
    with db.write_lock(db_path):
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        cleared: dict[str, int] = {}
        for table in tables:
            cursor = conn.execute(f'DELETE FROM "{table}"')
            cleared[table] = max(cursor.rowcount, 0)
        conn.commit()
        try:
            # Flushes + truncates the -wal/-shm side files through the
            # live connection so disk usage actually shrinks. Never fatal
            # to the reset itself if this fails (e.g. a read cursor from
            # another thread still open) -- the data is already gone.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError as exc:
            logger.warning("wal_checkpoint after reset failed (non-fatal): %s", exc)
    logger.info("Reset all data: cleared %s", cleared)
    return cleared


def delete_all_reports() -> tuple[int, list[str]]:
    """Delete every file in reports_dir() (generated HTML/Markdown
    investigation reports -- the directory holds nothing else, see
    paths.reports_dir()).

    Returns (deleted_count, [filenames that failed to delete, e.g.
    because one is open in another process]). Never raises: a single
    locked file shouldn't abort the rest of the wipe.
    """
    reports_dir = paths.reports_dir()
    deleted = 0
    failed: list[str] = []
    if not reports_dir.is_dir():
        return deleted, failed
    for entry in reports_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            entry.unlink()
            deleted += 1
        except OSError as exc:
            logger.warning("Failed to delete report file '%s': %s", entry.name, exc)
            failed.append(entry.name)
    return deleted, failed
