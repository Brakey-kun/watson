"""Investigation state persistence manager.

Provides SQLite-backed checkpoint storage for investigation pause/resume,
history tracking, crash recovery, and diff computation between runs.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from . import db, paths
from .events import Event, EventBus, EventType
from .models import (
    InvestigationConfig,
    InvestigationState,
    InvestigationStatus,
)

logger = logging.getLogger(__name__)

# SQL for creating the investigations table
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS investigations (
    id TEXT PRIMARY KEY,
    target TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    config TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    elapsed_seconds REAL NOT NULL DEFAULT 0.0,
    rounds_completed INTEGER NOT NULL DEFAULT 0,
    findings_count INTEGER NOT NULL DEFAULT 0,
    report_md_path TEXT,
    report_html_path TEXT,
    report_pdf_path TEXT
)
"""

_UPSERT_SQL = """
INSERT OR REPLACE INTO investigations (
    id, target, category, status, config, state,
    created_at, updated_at, elapsed_seconds, rounds_completed,
    findings_count, report_md_path, report_html_path, report_pdf_path
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_BY_ID_SQL = """
SELECT id, target, category, status, config, state,
       created_at, updated_at, elapsed_seconds, rounds_completed,
       findings_count, report_md_path, report_html_path, report_pdf_path
FROM investigations WHERE id = ?
"""

_LIST_SQL = """
SELECT id, target, category, status, created_at, updated_at,
       elapsed_seconds, rounds_completed, findings_count,
       report_md_path, report_html_path, report_pdf_path
FROM investigations ORDER BY created_at DESC LIMIT ?
"""

_CLEANUP_SQL = """
DELETE FROM investigations
WHERE status IN ('completed', 'failed')
AND created_at < ?
"""


class StateManager:
    """Persists investigation state to SQLite for pause/resume, history, and recovery.

    Attributes:
        db_path: Path to the SQLite database file.
        event_bus: Optional EventBus for emitting error events.
    """

    def __init__(self, db_path: Optional[str] = None, event_bus: Optional[EventBus] = None) -> None:
        """Initialize the state manager, creating the database and schema if needed.

        Args:
            db_path: Path to the SQLite database file. Created if it does not exist.
            event_bus: Optional EventBus instance for emitting error events on corruption.
        """
        self.db_path = db_path if db_path is not None else str(paths.db_path())
        self.event_bus = event_bus
        self._conn = db.get_connection(self.db_path)
        with db.write_lock(self.db_path):
            self._conn.execute(_CREATE_TABLE_SQL)
            self._conn.commit()

    def save_checkpoint(self, state: InvestigationState) -> None:
        """Save current investigation state to the database.

        Serializes config and full state (including findings dict) to JSON and
        performs an UPSERT (INSERT OR REPLACE) on the record.

        Args:
            state: The full InvestigationState to persist.
        """
        now = datetime.now(timezone.utc).isoformat()

        # Serialize config to JSON
        config_json = json.dumps({
            "target": state.config.target,
            "category": state.config.category,
            "max_rounds": state.config.max_rounds,
            "urgency": state.config.urgency,
            "lm_studio_url": state.config.lm_studio_url,
            "enable_multi_engine": state.config.enable_multi_engine,
            "enable_pdf": state.config.enable_pdf,
        })

        # Serialize state to JSON (findings, round_plans, error)
        state_json = json.dumps({
            "findings": self._serialize_findings(state.findings),
            "round_plans": state.round_plans,
            "error": state.error,
        })

        # Check if record already exists to preserve created_at
        existing = self._conn.execute(
            "SELECT created_at FROM investigations WHERE id = ?",
            (state.investigation_id,)
        ).fetchone()
        created_at = existing[0] if existing else now

        with db.write_lock(self.db_path):
            self._conn.execute(_UPSERT_SQL, (
                state.investigation_id,
                state.config.target,
                state.config.category,
                state.status.value,
                config_json,
                state_json,
                created_at,
                now,
                state.elapsed_seconds,
                state.current_round,
                len(state.findings),
                None,  # report_md_path - set externally if needed
                None,  # report_html_path
                None,  # report_pdf_path
            ))
            self._conn.commit()

    def load_checkpoint(self, investigation_id: str) -> Optional[InvestigationState]:
        """Load a saved investigation state from the database.

        If deserialization fails (corrupt JSON), sets the investigation status to
        FAILED and emits an error event via the event_bus if one is provided.

        Args:
            investigation_id: The UUID of the investigation to load.

        Returns:
            The restored InvestigationState, or None if not found.
        """
        row = self._conn.execute(_SELECT_BY_ID_SQL, (investigation_id,)).fetchone()
        if row is None:
            return None

        (
            inv_id, target, category, status, config_str, state_str,
            created_at, updated_at, elapsed_seconds, rounds_completed,
            findings_count, report_md_path, report_html_path, report_pdf_path
        ) = row

        try:
            config_data = json.loads(config_str)
            state_data = json.loads(state_str)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.error(
                "Corrupt checkpoint for investigation %s: %s", investigation_id, exc
            )
            # Set status to FAILED in the database
            with db.write_lock(self.db_path):
                self._conn.execute(
                    "UPDATE investigations SET status = ?, updated_at = ? WHERE id = ?",
                    (InvestigationStatus.FAILED.value, datetime.now(timezone.utc).isoformat(), investigation_id)
                )
                self._conn.commit()

            # Emit error event if event_bus is available
            if self.event_bus is not None:
                self.event_bus.emit(Event(
                    type=EventType.INVESTIGATION_FAILED,
                    investigation_id=investigation_id,
                    data={"error": f"Checkpoint corrupted: {exc}"},
                ))
            return None

        # Reconstruct InvestigationConfig
        config = InvestigationConfig(
            target=config_data.get("target", target),
            category=config_data.get("category", category),
            max_rounds=config_data.get("max_rounds", "Auto"),
            urgency=config_data.get("urgency", "normal"),
            lm_studio_url=config_data.get("lm_studio_url"),
            enable_multi_engine=config_data.get("enable_multi_engine", True),
            enable_pdf=config_data.get("enable_pdf", False),
        )

        # Reconstruct findings dict
        findings = self._deserialize_findings(state_data.get("findings", {}))

        # Reconstruct InvestigationState
        investigation_state = InvestigationState(
            investigation_id=inv_id,
            config=config,
            status=InvestigationStatus(status),
            current_round=rounds_completed,
            findings=findings,
            round_plans=state_data.get("round_plans", []),
            elapsed_seconds=elapsed_seconds,
            error=state_data.get("error"),
        )

        return investigation_state

    def list_investigations(self, limit: int = 50) -> List[dict]:
        """List investigations ordered by created_at descending.

        Args:
            limit: Maximum number of records to return. Clamped to range [1, 200].

        Returns:
            List of dicts with summary fields (not full state).
        """
        # Clamp limit to 1-200
        limit = max(1, min(200, limit))

        rows = self._conn.execute(_LIST_SQL, (limit,)).fetchall()
        results = []
        for row in rows:
            (
                inv_id, target, category, status, created_at, updated_at,
                elapsed_seconds, rounds_completed, findings_count,
                report_md_path, report_html_path, report_pdf_path
            ) = row
            results.append({
                "id": inv_id,
                "target": target,
                "category": category,
                "status": status,
                "created_at": created_at,
                "updated_at": updated_at,
                "elapsed_seconds": elapsed_seconds,
                "rounds_completed": rounds_completed,
                "findings_count": findings_count,
                "report_md_path": report_md_path,
                "report_html_path": report_html_path,
                "report_pdf_path": report_pdf_path,
            })
        return results

    def get_diff(self, investigation_id: str, previous_id: str) -> dict:
        """Compare finding URLs between two investigations.

        Args:
            investigation_id: The current (newer) investigation ID.
            previous_id: The previous (older) investigation ID.

        Returns:
            Dict with keys "new", "removed", "unchanged" containing lists of URLs,
            or a dict with an "error" key if targets don't match or IDs not found.
        """
        current_state = self.load_checkpoint(investigation_id)
        previous_state = self.load_checkpoint(previous_id)

        if current_state is None:
            return {"error": f"Investigation '{investigation_id}' not found"}
        if previous_state is None:
            return {"error": f"Investigation '{previous_id}' not found"}

        # Check targets match
        if current_state.config.target != previous_state.config.target:
            return {
                "error": (
                    f"Targets do not match: '{current_state.config.target}' "
                    f"vs '{previous_state.config.target}'"
                )
            }

        current_urls = set(current_state.findings.keys())
        previous_urls = set(previous_state.findings.keys())

        return {
            "new": sorted(current_urls - previous_urls),
            "removed": sorted(previous_urls - current_urls),
            "unchanged": sorted(current_urls & previous_urls),
        }

    def cleanup_old(self, days: int = 30) -> int:
        """Remove completed/failed investigation records older than N days.

        Args:
            days: Number of days. Records older than this are deleted. Minimum 1.

        Returns:
            Number of rows deleted.
        """
        # Clamp days to minimum 1
        days = max(1, days)

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_iso = cutoff.isoformat()

        with db.write_lock(self.db_path):
            cursor = self._conn.execute(_CLEANUP_SQL, (cutoff_iso,))
            self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        """Close the database connection."""
        db.close_connection(self.db_path)

    def _serialize_findings(self, findings: dict) -> dict:
        """Serialize findings dict to a JSON-compatible format.

        Findings values can be Finding dataclass instances or plain dicts.
        """
        serialized = {}
        for url, finding in findings.items():
            if hasattr(finding, "__dataclass_fields__"):
                # Convert dataclass to dict
                serialized[url] = {
                    "url": finding.url,
                    "name": finding.name,
                    "status": finding.status,
                    "title": finding.title,
                    "snippet": finding.snippet,
                    "category": finding.category,
                    "relevance_score": finding.relevance_score,
                    "content_hash": finding.content_hash,
                    "fetched_at": finding.fetched_at,
                    "response_time_ms": finding.response_time_ms,
                    "round_discovered": finding.round_discovered,
                    "confidence": finding.confidence,
                }
            else:
                # Already a dict
                serialized[url] = finding
        return serialized

    def _deserialize_findings(self, findings_data: dict) -> dict:
        """Deserialize findings from JSON-compatible format back to a dict.

        Returns findings as plain dicts keyed by URL.
        """
        # Return as-is since InvestigationState.findings is typed as dict
        return findings_data
