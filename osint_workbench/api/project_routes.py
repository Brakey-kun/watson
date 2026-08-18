"""Project CRUD HTTP surface.

Split out into its own narrow Blueprint so it can be registered by BOTH
Flask hosts this project ships: osint_workbench/app.py's create_app() and
gui.py (the legacy dashboard). Matches create_rag_blueprint's convention
of reading storage from `current_app.config` at request time instead of
taking it as a constructor arg, so either host can populate
app.config["PROJECT_STORE"] however suits it.
"""

import dataclasses
import logging
from typing import Callable, Optional

from flask import Blueprint, current_app, jsonify, request

logger = logging.getLogger(__name__)


def create_project_blueprint(
    get_active_project_id: Optional[Callable[[], Optional[str]]] = None,
) -> Blueprint:
    """Create the Project CRUD Blueprint.

    Reads `current_app.config["PROJECT_STORE"]` (a
    `osint_workbench.core.project_store.ProjectStore`) at request time in
    every handler; returns 500 if that key is unset/None so callers get a
    clear "not configured" error instead of an AttributeError.

    Args:
        get_active_project_id: Optional callback returning the project_id
            of the investigation CURRENTLY running (None if none is), so
            delete_project can refuse to pull a project/steering scope
            out from under a live run instead of leaving it writing into
            now-deleted state. A host with no is_running tracking of its
            own may omit this (delete is then always allowed).
    """
    project_bp = Blueprint("project", __name__)

    def _get_store():
        return current_app.config.get("PROJECT_STORE")

    def _no_store_response():
        return jsonify({"success": False, "error": "Project store not available"}), 500

    # --- GET /api/strategies ---
    @project_bp.route("/api/strategies", methods=["GET"])
    def list_strategies():
        """List available investigation strategies with plain-language
        descriptions, sourced live from engine.py's STRATEGIES registry
        (the authoritative definition -- see its module-level docstring)
        so the dashboard's strategy selector and docs modal can never
        drift from what run_investigation() actually dispatches on.
        Local import mirrors gui.py's _build_engine_from_config() pattern:
        avoids pulling engine.py's heavier dependency chain into either
        Flask host's module-level import graph."""
        from osint_workbench.core.engine import DEFAULT_STRATEGY, STRATEGIES

        return jsonify({
            "success": True,
            "strategies": STRATEGIES,
            "default": DEFAULT_STRATEGY,
        }), 200

    # --- GET /api/projects ---
    @project_bp.route("/api/projects", methods=["GET"])
    def list_projects():
        project_store = _get_store()
        if project_store is None:
            return _no_store_response()
        include_archived = request.args.get("include_archived", "").strip().lower() in ("true", "1")
        projects = project_store.list(include_archived=include_archived)
        return jsonify({
            "success": True,
            "projects": [dataclasses.asdict(p) for p in projects],
        }), 200

    # --- POST /api/projects ---
    @project_bp.route("/api/projects", methods=["POST"])
    def create_project():
        project_store = _get_store()
        if project_store is None:
            return _no_store_response()
        data = request.get_json(force=True, silent=True) or {}
        try:
            project = project_store.create(
                data.get("name", ""),
                data.get("description", ""),
            )
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        return jsonify({"success": True, "project": dataclasses.asdict(project)}), 200

    # --- GET /api/projects/<project_id> ---
    @project_bp.route("/api/projects/<project_id>", methods=["GET"])
    def get_project(project_id):
        project_store = _get_store()
        if project_store is None:
            return _no_store_response()
        project = project_store.get(project_id)
        if project is None:
            return jsonify({"success": False, "error": f"Project '{project_id}' not found"}), 404
        return jsonify({"success": True, "project": dataclasses.asdict(project)}), 200

    # --- PATCH /api/projects/<project_id> ---
    @project_bp.route("/api/projects/<project_id>", methods=["PATCH"])
    def update_project(project_id):
        project_store = _get_store()
        if project_store is None:
            return _no_store_response()
        data = request.get_json(force=True, silent=True) or {}
        project = project_store.update(
            project_id,
            name=data.get("name"),
            description=data.get("description"),
        )
        if project is None:
            return jsonify({"success": False, "error": f"Project '{project_id}' not found"}), 404
        return jsonify({"success": True, "project": dataclasses.asdict(project)}), 200

    # --- POST /api/projects/<project_id>/archive ---
    @project_bp.route("/api/projects/<project_id>/archive", methods=["POST"])
    def archive_project(project_id):
        project_store = _get_store()
        if project_store is None:
            return _no_store_response()
        data = request.get_json(force=True, silent=True) or {}
        if "archived" not in data:
            return jsonify({"success": False, "error": "Missing required field: archived"}), 400
        project = project_store.set_archived(project_id, bool(data["archived"]))
        if project is None:
            return jsonify({"success": False, "error": f"Project '{project_id}' not found"}), 404
        return jsonify({"success": True, "project": dataclasses.asdict(project)}), 200

    # --- DELETE /api/projects/<project_id> ---
    @project_bp.route("/api/projects/<project_id>", methods=["DELETE"])
    def delete_project(project_id):
        project_store = _get_store()
        if project_store is None:
            return _no_store_response()
        if get_active_project_id is not None and get_active_project_id() == project_id:
            return (
                jsonify({
                    "success": False,
                    "error": "Cannot clear data for the investigation that is currently running. Stop it first.",
                }),
                409,
            )
        deleted = project_store.delete(project_id)
        if not deleted:
            return jsonify({"success": False, "error": f"Project '{project_id}' not found"}), 404
        return jsonify({"success": True, "deleted": True}), 200

    return project_bp
