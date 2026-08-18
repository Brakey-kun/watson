"""Shared SQLite connection for all Watson persistence needs.

`StateManager` (investigations table) and the newer steering/plan/memory
modules (steering_index, plan_objects, claims, kb_lessons tables) all write
to the same on-disk database from different threads: query-time pheromone
reinforcement happens inline in the research loop thread, doubt-search claim
transitions happen inline too, and knowledge-base write-back happens from a
background thread after an investigation completes. Independent
`sqlite3.connect()` calls to the same file from multiple threads is how you
get intermittent "database is locked" errors even under WAL mode.

This module hands out ONE connection per db_path (WAL mode, `timeout=30` as
a backstop) plus one `threading.Lock` per db_path that every writer MUST
hold for the duration of an INSERT/UPDATE/DELETE (concurrent reads are fine
under WAL and need no lock). Each owning module is responsible for its own
`CREATE TABLE IF NOT EXISTS` DDL, run once under the write lock the first
time that module opens the connection — DDL is idempotent and the tables
have no foreign-key relationships to each other, so creation order across
modules does not matter.
"""

from __future__ import annotations

import sqlite3
import threading

from osint_workbench.core import paths

_connections: dict[str, sqlite3.Connection] = {}
_connections_lock = threading.Lock()
_write_locks: dict[str, threading.Lock] = {}


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Return the shared WAL-mode connection for db_path, opening it on first use.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        A `sqlite3.Connection` shared by every caller that passes the same
        db_path, safe to use from multiple threads (`check_same_thread=False`)
        as long as writers hold `write_lock(db_path)`.
    """
    if db_path is None:
        db_path = str(paths.db_path())
    with _connections_lock:
        conn = _connections.get(db_path)
        if conn is None:
            conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()
            _connections[db_path] = conn
            _write_locks[db_path] = threading.Lock()
        return conn


def write_lock(db_path: str | None = None) -> threading.Lock:
    """Return the write lock guarding db_path, opening the connection first if needed.

    Every INSERT/UPDATE/DELETE/DDL statement against db_path MUST run inside
    `with write_lock(db_path): ...` to serialize writers across threads.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        The `threading.Lock` instance associated with db_path.
    """
    if db_path is None:
        db_path = str(paths.db_path())
    get_connection(db_path)
    return _write_locks[db_path]


def close_connection(db_path: str | None = None) -> None:
    """Close and forget the shared connection for db_path, if one is open.

    Actually closes the underlying OS file handle (required on Windows,
    where an open sqlite3 connection blocks deleting the file/its -wal/-shm
    sidecars) and removes db_path from the registry so a later
    `get_connection(db_path)` reopens cleanly rather than reusing a closed
    connection object.

    Args:
        db_path: Path to the SQLite database file.
    """
    if db_path is None:
        db_path = str(paths.db_path())
    with _connections_lock:
        conn = _connections.pop(db_path, None)
        _write_locks.pop(db_path, None)
        if conn is not None:
            conn.close()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Add `column` to `table` if it doesn't already exist (idempotent).

    Every table in this codebase is created via `CREATE TABLE IF NOT
    EXISTS`, which is idempotent for brand-new columns on a *new* table but
    silently does nothing for a column added to the schema after some
    users already have a populated on-disk database -- the old file simply
    lacks the column, and the first query that references it raises
    "no such column" at runtime. Call this once (under the table's own
    write_lock) right after that table's `CREATE TABLE IF NOT EXISTS`, for
    every column added post-release, to bring existing databases forward.

    Args:
        conn: An open connection (caller holds write_lock(db_path)).
        table: Table name (trusted, not user input -- interpolated directly
            since sqlite3 parameter binding doesn't support identifiers).
        column: Column name to check/add (same trust assumption).
        decl: The column type + constraints to append after its name in
            `ALTER TABLE ... ADD COLUMN`, e.g. "INTEGER NOT NULL DEFAULT 1".
            SQLite requires ADD COLUMN defaults to be a constant, not an
            expression, so keep `decl` to simple literal defaults.
    """
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        conn.commit()
