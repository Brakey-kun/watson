"""REST API endpoints and WebSocket server for Watson.

Provides Flask Blueprint with endpoints for investigation management,
real-time event delivery via Flask-SocketIO, and report file serving.
Security headers are applied to all responses.

Requirements: 7.4, 7.5, 16.1, 16.2, 16.3, 16.4, 16.5, 17.2, 17.5, 17.7
"""

import dataclasses
import logging
import os
import threading
import uuid

from flask import Blueprint, current_app, jsonify, request, send_from_directory

from osint_workbench.core import paths
from osint_workbench.core.connection_tester import test_connection
from osint_workbench.core.engine import OSINTEngine
from osint_workbench.core.events import Event, EventBus, EventType
from osint_workbench.core.llm_client import LLMClient
from osint_workbench.core.state import StateManager
from osint_workbench.engine_factory import build_investigation_config, ensure_active_model

logger = logging.getLogger(__name__)


def _mask_api_key(key: str) -> str:
    """Mask an API key for safe display.

    If key length > 4, show first 4 chars + "***".
    If key length <= 4, show "***" entirely.
    """
    if len(key) > 4:
        return key[:4] + "***"
    return "***"


def create_api_blueprint(
    engine: OSINTEngine,
    state_manager: StateManager,
    event_bus: EventBus,
    socketio,
    reports_dir: str | None = None,
) -> Blueprint:
    """Create and configure the API Blueprint with all endpoints.

    Args:
        engine: The OSINT Engine instance for running investigations.
        state_manager: StateManager for investigation history.
        event_bus: EventBus for subscribing to real-time events.
        socketio: Flask-SocketIO instance for WebSocket event push.
        reports_dir: Directory path where report files are stored. Defaults
            to the external per-user data directory when omitted.

    Returns:
        Configured Flask Blueprint.
    """
    reports_dir = reports_dir if reports_dir is not None else str(paths.reports_dir())
    api = Blueprint("api", __name__)

    # --- Concurrency lock for is_running flag ---
    # Requirement 16.1: threading.Lock guards all reads/writes of is_running
    _run_lock = threading.Lock()
    _state = {
        "is_running": False,
        "investigation_id": None,
        "current_round": 0,
        "project_id": None,
    }

    # --- Log buffer ---
    _log_buffer = []
    _log_lock = threading.Lock()

    def _append_log(message: str) -> None:
        """Append a log message to the buffer (thread-safe)."""
        with _log_lock:
            _log_buffer.append(message)

    # --- Subscribe to EventBus for WebSocket push and log collection ---
    def _on_event(event: Event) -> None:
        """Handle events from the EventBus: push via SocketIO and buffer logs.

        Requirement 7.4: Push events via Flask-SocketIO within 500ms latency.
        """
        try:
            # Emit event to all connected WebSocket clients
            socketio.emit(
                event.type.value,
                {
                    "type": event.type.value,
                    "investigation_id": event.investigation_id,
                    "data": event.data,
                },
            )
        except Exception as exc:
            logger.error("Failed to emit SocketIO event: %s", exc)

        # Update internal tracking state
        if event.type == EventType.ROUND_STARTED:
            _state["current_round"] = event.data.get("round", 0)
        elif event.type == EventType.ROUND_COMPLETE:
            _state["current_round"] = event.data.get("round", 0)

        # Buffer log messages
        if event.type == EventType.LOG_MESSAGE:
            _append_log(event.data.get("message", str(event.data)))
        else:
            _append_log(f"[{event.type.value}] {event.data}")

    # Subscribe the SocketIO handler to all event types
    for event_type in EventType:
        event_bus.subscribe(event_type, _on_event)

    # --- Security headers middleware ---
    # Requirements 17.2, 17.5: X-Frame-Options and Content-Security-Policy
    @api.after_request
    def _add_security_headers(response):
        """Add security headers to all responses from this blueprint."""
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Content-Security-Policy"] = (
            "script-src 'self' 'unsafe-inline'; object-src 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    # --- POST /api/run ---
    @api.route("/api/run", methods=["POST"])
    def run_investigation():
        """Start a new OSINT investigation.

        Requirement 16.2: Atomic check-and-set of is_running with Lock.
        Requirement 16.3: Return HTTP 409 if already running.
        Requirement 16.5: Lock held only for reading/writing the flag.
        """
        data = request.get_json(force=True, silent=True) or {}
        config, error = build_investigation_config(data)
        if error:
            return jsonify({"success": False, "error": error}), 400

        # No model configured (or the last value was itself a guess) --
        # re-probe the endpoint now rather than let the run fail deep in
        # the LLM retry loop with an opaque "model not found". Re-checking
        # every time the active model is a guess (not just when it's
        # blank) catches the loaded model changing mid-session too --
        # e.g. LM Studio's JIT loading will silently start serving a
        # different id instead of erroring.
        llm_client = current_app.config.get("LLM_CLIENT")
        if llm_client is not None and (not llm_client.model or llm_client.model_autodetected):
            resolved_model, model_error = ensure_active_model(
                llm_client.base_url, "", llm_client.api_key
            )
            if model_error:
                return jsonify({"success": False, "error": model_error}), 400
            llm_client.model = resolved_model
            llm_client.model_autodetected = True

        # Atomic check-and-set with lock (Requirement 16.1, 16.2)
        with _run_lock:
            if _state["is_running"]:
                # Requirement 16.3: Return 409 if already running
                return (
                    jsonify({
                        "success": False,
                        "error": "Investigation already in progress",
                    }),
                    409,
                )
            _state["is_running"] = True

        # Generate investigation ID
        investigation_id = str(uuid.uuid4())
        _state["investigation_id"] = investigation_id
        _state["current_round"] = 0
        _state["project_id"] = config.project_id

        # Clear log buffer for new investigation
        with _log_lock:
            _log_buffer.clear()

        # Spawn investigation in a new thread
        def _run_thread():
            """Execute the investigation and release lock on completion.

            Requirement 16.4: Lock acquired and is_running set False in
            finally block.
            """
            try:
                _append_log(
                    f"Investigation started: {config.target} ({config.category})"
                )
                engine.run_investigation(config)
                _append_log("Investigation completed successfully.")
            except Exception as exc:
                logger.error("Investigation thread failed: %s", exc)
                _append_log(f"Investigation failed: {exc}")
            finally:
                # Requirement 16.4: Acquire lock, set is_running = False
                with _run_lock:
                    _state["is_running"] = False

        thread = threading.Thread(target=_run_thread, daemon=True)
        thread.start()

        return jsonify({
            "success": True,
            "investigation_id": investigation_id,
        }), 200

    # --- GET /api/status ---
    @api.route("/api/status", methods=["GET"])
    def get_status():
        """Return current investigation status.

        Returns is_running, investigation_id, and current_round.
        """
        return jsonify({
            "is_running": _state["is_running"],
            "investigation_id": _state["investigation_id"],
            "current_round": _state["current_round"],
            "project_id": _state.get("project_id"),
        }), 200

    # --- GET /api/logs ---
    @api.route("/api/logs", methods=["GET"])
    def get_logs():
        """Return buffered log messages."""
        with _log_lock:
            logs_copy = list(_log_buffer)
        return jsonify({"logs": logs_copy}), 200

    # --- GET /api/history ---
    @api.route("/api/history", methods=["GET"])
    def get_history():
        """Return investigation history from StateManager."""
        try:
            investigations = state_manager.list_investigations()
            return jsonify({"investigations": investigations}), 200
        except Exception as exc:
            logger.error("Failed to list investigations: %s", exc)
            return (
                jsonify({"investigations": [], "error": str(exc)}),
                500,
            )

    # --- POST /api/pause ---
    @api.route("/api/pause", methods=["POST"])
    def pause_investigation():
        """Pause the currently running investigation."""
        investigation_id = _state.get("investigation_id")
        if not investigation_id:
            return (
                jsonify({"success": False, "error": "No active investigation"}),
                400,
            )

        try:
            engine.pause_investigation(investigation_id)
            _append_log("Investigation paused.")
            return jsonify({"success": True}), 200
        except (ValueError, RuntimeError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    # --- POST /api/resume ---
    @api.route("/api/resume", methods=["POST"])
    def resume_investigation():
        """Resume a paused investigation in a new thread."""
        investigation_id = _state.get("investigation_id")
        if not investigation_id:
            return (
                jsonify({
                    "success": False,
                    "error": "No investigation to resume",
                }),
                400,
            )

        # Check we aren't already running
        with _run_lock:
            if _state["is_running"]:
                return (
                    jsonify({
                        "success": False,
                        "error": "Investigation already in progress",
                    }),
                    409,
                )
            _state["is_running"] = True

        def _resume_thread():
            """Resume investigation and release lock on completion."""
            try:
                _append_log("Investigation resumed.")
                engine.resume_investigation(investigation_id)
                _append_log("Resumed investigation completed.")
            except Exception as exc:
                logger.error("Resume thread failed: %s", exc)
                _append_log(f"Resume failed: {exc}")
            finally:
                with _run_lock:
                    _state["is_running"] = False

        thread = threading.Thread(target=_resume_thread, daemon=True)
        thread.start()

        return jsonify({"success": True}), 200

    # --- POST /api/stop ---
    @api.route("/api/stop", methods=["POST"])
    def stop_investigation():
        """Gracefully stop the currently running investigation.

        Sets the engine stop signal so the research loop exits at the
        next round boundary and proceeds to report generation.

        Returns:
            200 with success=True if stop signal was set.
            400 if no investigation is active.

        Requirements: 3.2, 3.3, 3.4, 8.4, 8.5
        """
        with _run_lock:
            if not _state["is_running"]:
                return (
                    jsonify({"success": False, "error": "No active investigation"}),
                    400,
                )

        investigation_id = _state.get("investigation_id")
        if not investigation_id:
            return (
                jsonify({"success": False, "error": "No active investigation"}),
                400,
            )

        try:
            engine.stop_investigation(investigation_id)
            _append_log("Stop requested. Wrapping up current round...")
            return jsonify({"success": True, "message": "Stop signal sent"}), 200
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

    # --- GET /reports/<filename> ---
    @api.route("/reports/<path:filename>", methods=["GET"])
    def serve_report(filename: str):
        """Serve a report file with basename-only resolution.

        Requirement 17.7: Strip path traversal by resolving to basename.
        """
        # Strip path traversal: only use the basename
        safe_filename = os.path.basename(filename)

        if not safe_filename:
            return jsonify({"error": "Invalid filename"}), 400

        # Resolve absolute reports directory
        abs_reports_dir = os.path.abspath(reports_dir)

        # Check file exists
        file_path = os.path.join(abs_reports_dir, safe_filename)
        if not os.path.isfile(file_path):
            return jsonify({"error": "File not found"}), 404

        return send_from_directory(abs_reports_dir, safe_filename)

    # --- POST /api/test-connection ---
    @api.route("/api/test-connection", methods=["POST"])
    def test_connection_endpoint():
        """Test connectivity to an LLM API endpoint.

        Accepts JSON body with `endpoint` and `api_key` fields.
        Returns structured ConnectionTestResult as JSON.

        Requirement 4.3: Connection test with categorized error responses.
        """
        data = request.get_json(force=True, silent=True) or {}

        endpoint = data.get("endpoint")
        api_key = data.get("api_key")

        # Validate required fields
        missing_fields = []
        if not endpoint:
            missing_fields.append("endpoint")
        if not api_key:
            missing_fields.append("api_key")

        if missing_fields:
            return (
                jsonify({
                    "success": False,
                    "error": f"Missing required fields: {', '.join(missing_fields)}",
                }),
                400,
            )

        # Run connection test
        result = test_connection(endpoint, api_key)

        return jsonify({
            "success": result.success,
            "status_code": result.status_code,
            "error_category": result.error_category,
            "error_detail": result.error_detail,
            "models_available": result.models_available,
            "response_time_ms": result.response_time_ms,
        }), 200

    # --- POST /api/save-config ---
    @api.route("/api/save-config", methods=["POST"])
    def save_config():
        """Save full or partial configuration to disk.

        Accepts a JSON body with config data, merges it with the current
        config (supporting partial updates), and persists via ConfigLoader.save().
        Sets the setup_completed flag when the wizard completes.

        Requirements: 2.5, 4.5
        """
        data = request.get_json(force=True, silent=True)
        if data is None:
            return jsonify({"success": False, "error": "Invalid or missing JSON body"}), 400

        config_loader = current_app.config.get("CONFIG_LOADER")
        if config_loader is None:
            return jsonify({"success": False, "error": "ConfigLoader not available"}), 500

        try:
            # Load the current config from disk to get the base state
            import json
            config_path = config_loader.config_path
            if config_path.exists():
                current_config = json.loads(config_path.read_text(encoding="utf-8"))
            else:
                current_config = {}

            # Deep merge: submitted data overrides current config (partial update support)
            from osint_workbench.core.config import _deep_merge
            merged_config = _deep_merge(current_config, data)

            # If the submitted data explicitly sets setup_completed, ensure it's in the merged config
            if "setup_completed" in data:
                merged_config["setup_completed"] = data["setup_completed"]

            # Persist to disk
            config_loader.save(merged_config)

            return jsonify({"success": True}), 200

        except Exception as exc:
            logger.error("Failed to save config: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    # --- GET /api/get-config ---
    @api.route("/api/get-config", methods=["GET"])
    def get_config():
        """Return current configuration with API keys masked.

        Returns the active LLM config, all backends with masked API keys,
        the setup_completed flag, and the list of valid backend names.

        Masking rule: if key length > 4, show first 4 chars + "***";
        if key length <= 4, show "***" entirely.

        Requirement 6.1: Config file schema with llm, backends, setup_completed.
        """
        config_loader = current_app.config.get("CONFIG_LOADER")
        if config_loader is None:
            return jsonify({"success": False, "error": "ConfigLoader not available"}), 500

        try:
            # Reload config from disk to get the latest state
            config = config_loader.load()

            # Build the LLM section
            llm_data = {
                "backend": config.llm.backend,
                "host": config.llm.host,
                "port": config.llm.port,
                "model": config.llm.model,
                "temperature": config.llm.temperature,
            }

            # Build backends section with masked API keys
            backends_data = {}
            for name, backend in config.backends.items():
                masked_key = _mask_api_key(backend.api_key)
                backends_data[name] = {
                    "endpoint": backend.endpoint,
                    "api_key": masked_key,
                    "model": backend.model,
                    "temperature": backend.temperature,
                    "last_tested": backend.last_tested,
                }

            # Get valid backends list
            valid_backends = list(config_loader.get_valid_backends().keys())

            # Build the tiers section (thinker/default/small model overrides)
            tiers_data = {
                "thinker": {"model": config.tiers.thinker.model, "temperature": config.tiers.thinker.temperature},
                "default": {"model": config.tiers.default.model, "temperature": config.tiers.default.temperature},
                "small": {"model": config.tiers.small.model, "temperature": config.tiers.small.temperature},
            }

            return jsonify({
                "llm": llm_data,
                "backends": backends_data,
                "tiers": tiers_data,
                "setup_completed": config.setup_completed,
                "valid_backends": valid_backends,
            }), 200

        except Exception as exc:
            logger.error("Failed to get config: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    # --- POST /api/switch-backend ---
    @api.route("/api/switch-backend", methods=["POST"])
    def switch_backend():
        """Switch the active LLM backend.

        Accepts JSON body with `backend_name`. Validates the backend exists
        and passes validation, updates in-memory config, persists to disk,
        and re-initializes the LLM client with new backend settings.

        On disk write failure: reverts in-memory state and returns error.

        Requirements: 3.5, 5.3, 5.4
        """
        data = request.get_json(force=True, silent=True) or {}

        backend_name = data.get("backend_name")
        if not backend_name:
            return (
                jsonify({
                    "success": False,
                    "error": "Missing required field: backend_name",
                }),
                400,
            )

        config_loader = current_app.config.get("CONFIG_LOADER")
        app_config = current_app.config.get("APP_CONFIG")
        if config_loader is None or app_config is None:
            return (
                jsonify({
                    "success": False,
                    "error": "Configuration not available",
                }),
                500,
            )

        # Validate backend exists in config's backends dict
        if backend_name not in app_config.backends:
            return (
                jsonify({
                    "success": False,
                    "error": f"Backend '{backend_name}' not found in configuration",
                }),
                400,
            )

        # Validate the backend passes validation
        backend_obj = app_config.backends[backend_name]
        backend_dict = {
            "endpoint": backend_obj.endpoint,
            "api_key": backend_obj.api_key,
            "model": backend_obj.model,
            "temperature": backend_obj.temperature,
            "last_tested": backend_obj.last_tested,
        }
        validation_errors = config_loader.validate_backend(backend_name, backend_dict)
        if validation_errors:
            error_details = "; ".join(
                f"{e.field_name}: {e.detail}" for e in validation_errors
            )
            return (
                jsonify({
                    "success": False,
                    "error": f"Backend '{backend_name}' failed validation: {error_details}",
                }),
                400,
            )

        # Store old backend name for potential revert
        old_backend_name = app_config.llm.backend

        # Update in-memory config
        app_config.llm.backend = backend_name

        # Build config dict for persistence
        config_data = _app_config_to_dict(app_config)

        # Persist to disk
        try:
            config_loader.save(config_data)
        except Exception as exc:
            # Revert in-memory state on disk write failure
            app_config.llm.backend = old_backend_name
            logger.error("Failed to persist backend switch to disk: %s", exc)
            return (
                jsonify({
                    "success": False,
                    "error": f"Failed to save configuration: {exc}",
                }),
                500,
            )

        # Re-initialize LLM client with new backend settings
        llm_client = current_app.config.get("LLM_CLIENT")
        if llm_client is not None:
            from openai import OpenAI as _OpenAI

            llm_client.base_url = backend_obj.endpoint
            llm_client.model = backend_obj.model
            llm_client.model_autodetected = False
            llm_client.temperature = backend_obj.temperature
            llm_client.api_key = backend_obj.api_key
            llm_client._client = _OpenAI(
                base_url=backend_obj.endpoint,
                api_key=backend_obj.api_key,
            )

        return jsonify({
            "success": True,
            "active_backend": backend_name,
        }), 200

    # --- GET /api/list-models ---
    @api.route("/api/list-models", methods=["GET"])
    def list_models():
        """List model identifiers currently available at a backend's endpoint.

        Query param `backend` selects which configured backend to query
        (defaults to the active backend). Feeds the tier model-selector UI's
        dropdowns so the user picks from what's actually loaded/servable
        rather than typing a model id blind.
        """
        app_config = current_app.config.get("APP_CONFIG")
        if app_config is None:
            return jsonify({"success": False, "error": "Configuration not available"}), 500

        backend_name = request.args.get("backend") or app_config.llm.backend
        backend_obj = app_config.backends.get(backend_name)
        if backend_obj is None:
            return (
                jsonify({
                    "success": False,
                    "error": f"Backend '{backend_name}' not found in configuration",
                }),
                400,
            )

        client = LLMClient(base_url=backend_obj.endpoint, api_key=backend_obj.api_key or "lm-studio")
        result = client.list_models()
        if result["error"]:
            logger.error("Failed to list models for backend %s: %s", backend_name, result["error"])
            return jsonify({"success": False, "error": result["error"]}), 502

        return jsonify({"success": True, "backend": backend_name, "models": result["models"]}), 200

    # --- GET /v1/models ---
    @api.route("/v1/models", methods=["GET"])
    def openai_models_passthrough():
        """OpenAI-compatible /v1/models passthrough to the active backend.

        Lets any OpenAI-API-shaped client (including the dashboard's own JS)
        query Watson's own origin instead of hardcoding the backend's
        endpoint directly, so a backend switch doesn't require a frontend
        config change too.
        """
        app_config = current_app.config.get("APP_CONFIG")
        if app_config is None:
            return jsonify({"error": "Configuration not available"}), 500

        backend_obj = app_config.backends.get(app_config.llm.backend)
        if backend_obj is None:
            return jsonify({"error": "Active backend not found in configuration"}), 500

        client = LLMClient(base_url=backend_obj.endpoint, api_key=backend_obj.api_key or "lm-studio")
        result = client.list_models()
        if result["error"]:
            logger.error("Failed to reach backend for /v1/models: %s", result["error"])
            return jsonify({"error": result["error"]}), 502

        return jsonify({
            "object": "list",
            "data": [
                {"id": m, "object": "model", "owned_by": app_config.llm.backend}
                for m in result["models"]
            ],
        }), 200

    # --- POST /api/switch-model ---
    @api.route("/api/switch-model", methods=["POST"])
    def switch_model():
        """Assign a model (and optional temperature) to one steering tier.

        Body: {"tier": "thinker"|"default"|"small", "model": "<id>",
        "temperature": <float, optional>}. Tiers are opt-in overrides
        (TierModelConfig): model="" explicitly clears a tier back to
        "fall back to the active backend's configured model".

        On disk write failure: reverts in-memory state and returns error,
        mirroring switch_backend's revert-on-failure contract.
        """
        data = request.get_json(force=True, silent=True) or {}

        tier = data.get("tier")
        if tier not in ("thinker", "default", "small"):
            return (
                jsonify({
                    "success": False,
                    "error": "tier must be one of: thinker, default, small",
                }),
                400,
            )
        if "model" not in data:
            return jsonify({"success": False, "error": "Missing required field: model"}), 400

        config_loader = current_app.config.get("CONFIG_LOADER")
        app_config = current_app.config.get("APP_CONFIG")
        if config_loader is None or app_config is None:
            return jsonify({"success": False, "error": "Configuration not available"}), 500

        tier_config = getattr(app_config.tiers, tier)
        old_model, old_temperature = tier_config.model, tier_config.temperature

        try:
            new_model = str(data.get("model") or "")
            new_temperature = old_temperature
            if "temperature" in data:
                temp = data.get("temperature")
                new_temperature = float(temp) if temp is not None else None
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "temperature must be a number"}), 400

        tier_config.model = new_model
        tier_config.temperature = new_temperature

        try:
            config_loader.save(_app_config_to_dict(app_config))
        except Exception as exc:
            tier_config.model, tier_config.temperature = old_model, old_temperature
            logger.error("Failed to persist model switch to disk: %s", exc)
            return jsonify({"success": False, "error": f"Failed to save configuration: {exc}"}), 500

        return jsonify({
            "success": True,
            "tier": tier,
            "model": tier_config.model,
            "temperature": tier_config.temperature,
        }), 200

    # Exposed so sibling blueprints registered by the SAME host (rag
    # blueprint's get_investigation_id, project blueprint's is_running
    # guard on delete, admin blueprint's reset guard) can read/reuse this
    # closure's otherwise-private run-tracking state instead of
    # duplicating _run_lock/_state -- the concurrency contract in
    # Requirement 16.1/16.2 only holds if there's ever exactly one lock.
    api.get_current_investigation_id = lambda: _state.get("investigation_id")
    api.run_lock = _run_lock
    api.get_is_running = lambda: _state["is_running"]
    api.get_active_project_id = lambda: (_state.get("project_id") if _state["is_running"] else None)
    api.reset_state = lambda: _state.update({"investigation_id": None, "current_round": 0, "project_id": None})
    return api


def _app_config_to_dict(app_config) -> dict:
    """Convert an AppConfig instance to a JSON-serializable dictionary.

    Uses `dataclasses.asdict()` rather than a hand-maintained field-by-field
    mapping. AppConfig's whole tree (llm/backends/fetcher/search/quality/
    tiers/burst_search/doubt_search) is plain dataclasses, dicts, and
    scalars -- no Enum members anywhere in it -- so asdict recurses
    correctly with zero custom encoding. A hand-written mapping silently
    drops any field added to AppConfig later without a matching edit here;
    this is exactly the bug that let `tiers`/`burst_search`/`doubt_search`
    get wiped from disk on every /api/switch-backend call before this fix.

    Args:
        app_config: The AppConfig dataclass instance.

    Returns:
        A dictionary suitable for JSON serialization and persistence.
    """
    return dataclasses.asdict(app_config)
