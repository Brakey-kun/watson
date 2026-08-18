"""Destructive admin HTTP surface: full data reset ("Settings > Reset,
start from scratch").

Split into its own narrow Blueprint, same convention as project_routes.py/
model_routes.py/skills_routes.py, so both Flask hosts this project ships
can register it. Unlike its siblings, this route is guarded by an explicit
same-origin check regardless of host: wiping every investigation, project,
RAG hint, claim, plan, and learned skill a user owns -- irreversibly, no
backup -- is a large enough blast radius that "matches the other
blueprints' lack of CSRF defense" is the wrong bar to clear here.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from flask import Blueprint, jsonify

from osint_workbench.core.reset import delete_all_reports, reset_all_tables

logger = logging.getLogger(__name__)


def create_admin_blueprint(
    run_lock: threading.Lock,
    get_is_running: Callable[[], bool],
    check_origin: Callable[[], bool],
    on_reset: Optional[Callable[[], None]] = None,
) -> Blueprint:
    """Create the admin Blueprint.

    Args:
        run_lock: The SAME threading.Lock the host already holds while
            flipping is_running (routes.py's `_run_lock` / gui.py's
            `_run_lock`) -- reused here rather than a second lock, so the
            is_running check and the actual truncate happen atomically.
            Without sharing the lock, a run could start in the gap
            between this route's check and the wipe, silently orphaning
            its own writes into tables that vanish out from under it.
        get_is_running: Reads the host's current is_running flag.
        check_origin: Same-origin / CSRF check, called before anything
            else runs. gui.py passes its existing `_check_same_origin`;
            a host with no such mechanism yet may pass `lambda: True`
            (that's still no worse than that host's pre-existing routes,
            just not better -- see module docstring for why this route
            specifically gets one regardless).
        on_reset: Optional host-specific cleanup invoked (still holding
            run_lock) right after a successful wipe -- clears any
            in-memory "last run" fields the host tracks outside the DB
            (e.g. gui.py's last_run_project_id/last_report_filename, or
            routes.py's `_state`) so a stale reference to now-deleted
            data can't keep leaking back out through /api/status.
    """
    admin_bp = Blueprint("admin", __name__)

    @admin_bp.route("/api/reset-all-data", methods=["POST"])
    def reset_all_data_route():
        """Hard-wipe every investigation/project/hint/claim/plan/skill
        this app has stored, plus every generated report file.

        Deliberately leaves config.json (LLM backend + tuning settings)
        untouched -- "start from scratch" means data, not credentials;
        the frontend confirm dialog says so explicitly.
        """
        if not check_origin():
            return jsonify({"success": False, "error": "Cross-origin request rejected"}), 403

        with run_lock:
            if get_is_running():
                return (
                    jsonify({
                        "success": False,
                        "error": "Cannot reset while an investigation is running. Stop it first.",
                    }),
                    409,
                )

            cleared = reset_all_tables()
            reports_deleted, reports_failed = delete_all_reports()

            if on_reset is not None:
                on_reset()

        logger.info(
            "Full data reset completed: %d table(s) cleared, %d report file(s) deleted, %d failed",
            len(cleared), reports_deleted, len(reports_failed),
        )
        return jsonify({
            "success": True,
            "cleared": cleared,
            "reports_deleted": reports_deleted,
            "reports_failed": reports_failed,
        }), 200

    return admin_bp
