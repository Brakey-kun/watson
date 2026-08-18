"""SQLite-backed persistence for Projects (Requirement: investigations,
RAG hints, and steering data are scoped per project/case, not globally).

A project is a long-lived case: the user creates it once, then runs many
investigations into it over time. RAG hints ingested while a project is
active are written to that project's own SteeringIndex scope (see
rag_ingest.py) so re-investigating the same subject later reuses hints
learned earlier in the case, without leaking into an unrelated case.

Follows the same connection/locking convention as OutcomeMemory and
SteeringIndex: one shared WAL-mode connection per db_path (db.py), all
writes serialized under db.write_lock(db_path).
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from . import db, paths

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
)
"""

_SELECT_COLUMNS = "id, name, description, created_at, updated_at, archived"


@dataclass
class Project:
    """A single projects-table row."""

    id: str
    name: str
    description: str
    created_at: float
    updated_at: float
    archived: bool


def _row_to_project(row) -> Project:
    return Project(
        id=row[0],
        name=row[1],
        description=row[2],
        created_at=row[3],
        updated_at=row[4],
        archived=bool(row[5]),
    )


def steering_scope(project_id: str) -> str:
    """The SteeringIndex `scope` string a project's RAG hints/steering
    data live under -- a distinct namespace from SteeringIndex.GLOBAL_SCOPE
    and from any investigation_id, so project-scoped rows can never
    collide with either. Shared helper so rag_ingest.py, engine.py, and
    this module all derive the same string from a project_id.
    """
    return f"project:{project_id}"


class ProjectStore:
    """CRUD persistence for projects."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path if db_path is not None else str(paths.db_path())
        conn = db.get_connection(self._db_path)
        with db.write_lock(self._db_path):
            conn.execute(_CREATE_TABLE_SQL)
            conn.commit()

    @property
    def _conn(self):
        return db.get_connection(self._db_path)

    def create(self, name: str, description: str = "") -> Project:
        """Create a new project. Raises ValueError if name is blank."""
        name = name.strip()
        if not name:
            raise ValueError("Project name is required.")
        now = time.time()
        project = Project(
            id=str(uuid.uuid4()),
            name=name,
            description=description.strip(),
            created_at=now,
            updated_at=now,
            archived=False,
        )
        with db.write_lock(self._db_path):
            self._conn.execute(
                "INSERT INTO projects (id, name, description, created_at, "
                "updated_at, archived) VALUES (?, ?, ?, ?, ?, ?)",
                (project.id, project.name, project.description,
                 project.created_at, project.updated_at, int(project.archived)),
            )
            self._conn.commit()
        return project

    def get(self, project_id: str) -> Optional[Project]:
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return _row_to_project(row) if row else None

    def list(self, include_archived: bool = False) -> list[Project]:
        """List projects, most recently updated first."""
        if include_archived:
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM projects ORDER BY updated_at DESC"
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM projects WHERE archived = 0 "
                "ORDER BY updated_at DESC"
            ).fetchall()
        return [_row_to_project(row) for row in rows]

    def update(
        self, project_id: str, name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Optional[Project]:
        """Rename/redescribe a project. Returns the updated Project, or
        None if project_id doesn't exist. No-ops on blank name (keeps the
        existing one) rather than raising, since this is a partial update."""
        project = self.get(project_id)
        if project is None:
            return None
        new_name = name.strip() if name and name.strip() else project.name
        new_description = description if description is not None else project.description
        now = time.time()
        with db.write_lock(self._db_path):
            self._conn.execute(
                "UPDATE projects SET name = ?, description = ?, updated_at = ? "
                "WHERE id = ?",
                (new_name, new_description, now, project_id),
            )
            self._conn.commit()
        return self.get(project_id)

    def set_archived(self, project_id: str, archived: bool) -> Optional[Project]:
        """Archive/unarchive a project (soft delete -- keeps its history
        and steering data intact, just hides it from the default list)."""
        if self.get(project_id) is None:
            return None
        with db.write_lock(self._db_path):
            self._conn.execute(
                "UPDATE projects SET archived = ?, updated_at = ? WHERE id = ?",
                (int(archived), time.time(), project_id),
            )
            self._conn.commit()
        return self.get(project_id)

    def delete(self, project_id: str) -> bool:
        """Hard-delete a project and its steering-scoped RAG hints/source
        weighting rows. Returns False if project_id didn't exist."""
        if self.get(project_id) is None:
            return False
        with db.write_lock(self._db_path):
            self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            try:
                self._conn.execute(
                    "DELETE FROM steering_index WHERE scope = ?",
                    (steering_scope(project_id),),
                )
            except sqlite3.OperationalError:
                # steering_index table doesn't exist yet on this db_path
                # (no SteeringIndex has ever been constructed against it) --
                # nothing to clean up in that case.
                pass
            self._conn.commit()
        return True
