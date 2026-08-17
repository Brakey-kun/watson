import glob
import json
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------------------
# Self-Healing Integrity Engine — Startup Hook
# Runs BEFORE any application imports that could fail (e.g., main.py which
# imports third-party packages). Resolves Portable_Root, runs integrity checks,
# logs health report, and sets up runtime import failure recovery.
# Requirements: 1.1, 2.1, 2.4, 5.4, 8.4, 11.1, 11.2, 11.3
# ---------------------------------------------------------------------------

_logger = logging.getLogger("osint_workbench.startup")

# Resolve Portable_Root as the parent directory of this entry point file
_PORTABLE_ROOT = Path(__file__).resolve().parent

# Flag indicating whether setup wizard should be shown instead of dashboard
_needs_setup_wizard = False

try:
    from osint_workbench.core import paths
    from osint_workbench.core.dependency_manager import DependencyManager
    from osint_workbench.core.integrity import IntegrityEngine, resolve_portable_root
    from osint_workbench.core.setup_router import SetupRouter

    # Use resolve_portable_root() which derives from integrity.py's location
    _PORTABLE_ROOT = resolve_portable_root()

    # One-time move of pre-existing config.json/investigations.db/reports/
    # from the app's own source tree into the external per-user data
    # directory, before anything below looks for them at the new location
    # and "repairs" them as missing (see paths.migrate_legacy_data).
    _migrated = paths.migrate_legacy_data(_PORTABLE_ROOT)
    if _migrated:
        _logger.info(
            "Migrated legacy user data to the external data directory: %s",
            ", ".join(_migrated),
        )

    # Run integrity checks at startup
    _integrity_engine = IntegrityEngine(_PORTABLE_ROOT, paths.data_dir())
    _health_report = _integrity_engine.run_checks()

    # Health report is already emitted to logs by IntegrityEngine._emit_health_report()
    # Check if user needs to visit setup wizard
    if _health_report.needs_user_attention:
        _needs_setup_wizard = True
        _logger.info(
            "Setup wizard required: user data fields need configuration."
        )
    else:
        # Also check setup_completed directly via SetupRouter
        _config_path = paths.config_path()
        _setup_router = SetupRouter(_config_path)
        if _setup_router.needs_setup():
            _needs_setup_wizard = True
            _logger.info(
                "Setup wizard required: setup_completed is not true."
            )
        else:
            _logger.info("Setup complete — proceeding to main dashboard.")

except Exception as _startup_err:
    _logger.error(
        "Integrity engine startup failed: %s. Continuing with degraded mode.",
        _startup_err,
    )


# ---------------------------------------------------------------------------
# Runtime Import Failure Hook
# Catches ImportError/ModuleNotFoundError during execution and attempts
# automatic recovery via DependencyManager.handle_import_failure().
# Requirements: 5.4
# ---------------------------------------------------------------------------

_original_excepthook = sys.excepthook


def _import_failure_excepthook(exc_type, exc_value, exc_tb):
    """Custom excepthook that intercepts ImportError/ModuleNotFoundError.

    Attempts to recover by force-reinstalling the failed package via
    DependencyManager before falling through to the original excepthook.
    """
    if exc_type in (ImportError, ModuleNotFoundError):
        # Extract the module name from the exception
        module_name = getattr(exc_value, "name", None)
        if module_name:
            _logger.warning(
                "Runtime import failure detected for '%s'. "
                "Attempting automatic recovery...",
                module_name,
            )
            try:
                venv_path = _PORTABLE_ROOT / ".venv"
                requirements_path = _PORTABLE_ROOT / "requirements.txt"
                dep_manager = DependencyManager(venv_path, requirements_path)
                recovered = dep_manager.handle_import_failure(module_name)
                if recovered:
                    _logger.info(
                        "Successfully recovered import for '%s'.", module_name
                    )
                    return  # Swallow the exception — recovery succeeded
                else:
                    _logger.error(
                        "Could not recover import for '%s'.", module_name
                    )
            except Exception as recovery_err:
                _logger.error(
                    "Error during import failure recovery: %s", recovery_err
                )

    # Fall through to original handler
    _original_excepthook(exc_type, exc_value, exc_tb)


sys.excepthook = _import_failure_excepthook

# ---------------------------------------------------------------------------
# End of self-healing startup hook
# ---------------------------------------------------------------------------

from flask import Flask, jsonify, render_template, request, send_from_directory
from flask_socketio import SocketIO

from osint_workbench.api.rag_routes import create_rag_blueprint

# Engine components used by /api/run - see _build_engine_from_config().
# Heavier construction-only imports (OSINTEngine, ConcurrentFetcher, etc.)
# are imported locally inside that function to keep this module's import
# footprint narrow for routes that don't need them.
from osint_workbench.core.events import Event, EventBus, EventType
from osint_workbench.core.models import InvestigationConfig
from osint_workbench.core.outcome_memory import OutcomeMemory
from osint_workbench.core.plan_object import PlanStore
from osint_workbench.core.steering_index import SteeringIndex

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Persistent EventBus + steering/plan/outcome-memory singletons, shared
# across every /api/run call (unlike LLMClient/engine, which are rebuilt
# fresh per run so config.json edits apply immediately -- see
# _build_engine_from_config()). These are cheap SQLite-table wrappers with
# no backend/model dependency, and the RAG ingest UI (drag-drop box,
# investigation insights panel) needs to reach them even when no
# investigation is currently running.
_gui_event_bus = EventBus()
_steering_index = SteeringIndex()
_plan_store = PlanStore()
_outcome_memory = OutcomeMemory()
app.config["STEERING_INDEX"] = _steering_index
app.config["PLAN_STORE"] = _plan_store
app.config["OUTCOME_MEMORY"] = _outcome_memory


def _get_current_investigation_id() -> str:
    """Best-effort "what investigation is running right now" for the RAG
    blueprint -- mirrors api_status()'s own engine.current_state read."""
    engine = _current_engine
    state = engine.current_state if engine is not None else None
    return state.investigation_id if state is not None else ""


_rag_blueprint = create_rag_blueprint(
    event_bus=_gui_event_bus,
    get_investigation_id=_get_current_investigation_id,
)


@_rag_blueprint.before_request
def _refresh_rag_config():
    """Rebuild APP_CONFIG/LLM_CLIENT fresh from config.json before every
    RAG-blueprint request, matching this module's "config changes apply on
    the very next call, no restart" philosophy (see
    _build_engine_from_config())."""
    from osint_workbench.core.config import ConfigLoader
    from osint_workbench.core.llm_client import LLMClient
    from osint_workbench.engine_factory import resolve_backend_params

    config = ConfigLoader().load()
    base_url, model, temperature, api_key = resolve_backend_params(config)
    app.config["APP_CONFIG"] = config
    app.config["LLM_CLIENT"] = LLMClient(
        base_url=base_url, model=model, temperature=temperature,
        max_retries=config.llm.max_retries, api_key=api_key,
    )


app.register_blueprint(_rag_blueprint)

# Ensure reports directory exists
paths.reports_dir()  # Ensure the external reports directory exists


def _load_active_backend_display() -> dict:
    """Read config.json fresh and return display info for the active LLM
    backend. Replaces the old `import main; main.llm_cfg/main.backend_info`
    pattern, which was built once at server-startup import time and never
    reflected settings saved later via the setup wizard without a restart.
    """
    defaults = {
        "model": "your-model-name-here", "host": "127.0.0.1", "port": 1234,
        "backend_name": "lm_studio", "endpoint": "http://127.0.0.1:1234/v1",
    }
    try:
        config_path = paths.config_path()
        if not config_path.exists():
            return defaults
        config_data = json.loads(config_path.read_text(encoding="utf-8"))
        llm = config_data.get("llm", {})
        backend_name = llm.get("backend", defaults["backend_name"])
        backend = config_data.get("backends", {}).get(backend_name, {})
        host = llm.get("host", defaults["host"])
        port = llm.get("port", defaults["port"])
        return {
            "model": llm.get("model", defaults["model"]),
            "host": host,
            "port": port,
            "backend_name": backend_name,
            "endpoint": backend.get("endpoint", f"http://{host}:{port}/v1"),
        }
    except Exception:
        _logger.exception("Failed to load active backend display info")
        return defaults


@app.route("/")
def index():
    # Re-check config.json at request time instead of using the stale startup flag.
    # This ensures that once the user completes/closes the wizard (which sets
    # setup_completed=true), refreshing the page won't re-open it.
    global _needs_setup_wizard
    if _needs_setup_wizard:
        # Re-read config to see if setup has been completed since startup
        try:
            _config_path = paths.config_path()
            _setup_router = SetupRouter(_config_path)
            if not _setup_router.needs_setup():
                _needs_setup_wizard = False
        except Exception:
            _logger.exception("Failed to re-check setup-wizard status")

    show_setup_wizard = _needs_setup_wizard

    # Load dynamically from sources.json
    try:
        with open("sources.json", "r", encoding="utf-8") as f:
            sources_data = json.load(f)
        categories = sorted(list(sources_data.keys()))
    except Exception:
        _logger.exception("Failed to load sources.json; falling back to default categories")
        categories = ["Username / Person", "Domain", "Email", "Telephone", "Location", "Business"]

    backend_display = _load_active_backend_display()
    return render_template(
        "dashboard.html",
        model=backend_display["model"],
        host=backend_display["host"],
        port=backend_display["port"],
        backend_name=backend_display["backend_name"],
        endpoint=backend_display["endpoint"],
        categories=categories,
        show_setup_wizard=show_setup_wizard
    )

# ---------------------------------------------------------------------------
# Setup Wizard API routes (get-config, test-connection, save-config)
# These are needed by the setup wizard frontend JS but were previously only
# available via the full app factory (osint_workbench/app.py).
# ---------------------------------------------------------------------------


def _mask_api_key(key: str) -> str:
    """Mask an API key for safe display (first 4 chars + '***', or '***' if <=4 chars)."""
    if not key:
        return key
    if len(key) > 4:
        return key[:4] + "***"
    return "***"


_ALLOWED_ORIGINS = {"http://127.0.0.1:5000", "http://localhost:5000"}


def _check_same_origin() -> bool:
    """Reject cross-origin state-changing requests (CSRF defense-in-depth).

    Only enforced when the browser sends an Origin/Referer header; local
    non-browser clients that omit both are allowed, matching this app's
    localhost-only threat model.
    """
    origin = request.headers.get("Origin")
    if origin is not None:
        return origin in _ALLOWED_ORIGINS
    referer = request.headers.get("Referer")
    if referer is not None:
        return any(referer == a or referer.startswith(a + "/") for a in _ALLOWED_ORIGINS)
    return True


@app.route("/api/get-config", methods=["GET"])
def get_config():
    """Return current config.json contents as JSON, with API keys masked."""
    config_path = paths.config_path()
    try:
        if config_path.exists():
            config_data = json.loads(config_path.read_text(encoding="utf-8"))
        else:
            config_data = {}
        backends = config_data.get("backends")
        if isinstance(backends, dict):
            for backend in backends.values():
                if isinstance(backend, dict) and backend.get("api_key"):
                    backend["api_key"] = _mask_api_key(backend["api_key"])
        return jsonify(config_data), 200
    except (json.JSONDecodeError, OSError) as exc:
        return jsonify({"error": f"Failed to read config: {exc}"}), 500


def _resolve_backend(config_data: dict, backend_name: str | None = None):
    """Resolve (name, backend_dict) from raw config.json, defaulting to
    the active backend named in config['llm']['backend']. Returns
    (name, None) if that backend isn't present in config['backends'].
    """
    name = backend_name or config_data.get("llm", {}).get("backend", "lm_studio")
    backends = config_data.get("backends", {})
    return name, backends.get(name)


def _load_raw_config() -> dict:
    config_path = paths.config_path()
    if config_path.exists():
        return json.loads(config_path.read_text(encoding="utf-8"))
    return {}


@app.route("/api/list-models", methods=["GET"])
def list_models():
    """List model identifiers currently available at a backend's endpoint.

    Query param `backend` selects which configured backend to query
    (defaults to the active backend). Mirrors
    osint_workbench/api/routes.py's /api/list-models -- see that module's
    docstring for why the model selector UI needs this at all.
    """
    config_data = _load_raw_config()
    backend_name, backend = _resolve_backend(config_data, request.args.get("backend"))
    if backend is None:
        return jsonify({
            "success": False,
            "error": f"Backend '{backend_name}' not found in configuration",
        }), 400

    from osint_workbench.core.llm_client import LLMClient

    client = LLMClient(base_url=backend.get("endpoint", ""), api_key=backend.get("api_key") or "lm-studio")
    result = client.list_models()
    if result["error"]:
        return jsonify({"success": False, "error": result["error"]}), 502

    return jsonify({"success": True, "backend": backend_name, "models": result["models"]}), 200


@app.route("/v1/models", methods=["GET"])
def openai_models_passthrough():
    """OpenAI-compatible /v1/models passthrough to the active backend."""
    config_data = _load_raw_config()
    backend_name, backend = _resolve_backend(config_data)
    if backend is None:
        return jsonify({"error": "Active backend not found in configuration"}), 500

    from osint_workbench.core.llm_client import LLMClient

    client = LLMClient(base_url=backend.get("endpoint", ""), api_key=backend.get("api_key") or "lm-studio")
    result = client.list_models()
    if result["error"]:
        return jsonify({"error": result["error"]}), 502

    return jsonify({
        "object": "list",
        "data": [{"id": m, "object": "model", "owned_by": backend_name} for m in result["models"]],
    }), 200


@app.route("/api/switch-model", methods=["POST"])
def switch_model():
    """Assign a model (and optional temperature) to one steering tier.

    Body: {"tier": "thinker"|"default"|"small", "model": "<id>",
    "temperature": <float, optional>}. Tiers are opt-in overrides: model=""
    explicitly clears a tier back to "use the active backend's model".
    """
    if not _check_same_origin():
        return jsonify({"success": False, "error": "Cross-origin request rejected"}), 403

    data = request.get_json(silent=True) or {}
    tier = data.get("tier")
    if tier not in ("thinker", "default", "small"):
        return jsonify({
            "success": False,
            "error": "tier must be one of: thinker, default, small",
        }), 400
    if "model" not in data:
        return jsonify({"success": False, "error": "Missing required field: model"}), 400

    try:
        new_temperature = None
        if "temperature" in data:
            temp = data.get("temperature")
            new_temperature = float(temp) if temp is not None else None
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "temperature must be a number"}), 400

    config_path = paths.config_path()
    try:
        current_config = _load_raw_config()
        tiers = current_config.setdefault("tiers", {})
        tier_entry = tiers.setdefault(tier, {})
        tier_entry["model"] = str(data.get("model") or "")
        if "temperature" in data:
            tier_entry["temperature"] = new_temperature

        config_path.write_text(
            json.dumps(current_config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return jsonify({
            "success": True,
            "tier": tier,
            "model": tier_entry["model"],
            "temperature": tier_entry.get("temperature"),
        }), 200
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/test-connection", methods=["POST"])
def test_connection_endpoint():
    """Test connectivity to an LLM API endpoint."""
    if not _check_same_origin():
        return jsonify({"success": False, "error": "Cross-origin request rejected"}), 403

    from osint_workbench.core.connection_tester import test_connection

    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint")
    api_key = data.get("api_key")

    missing_fields = []
    if not endpoint:
        missing_fields.append("endpoint")
    if not api_key:
        missing_fields.append("api_key")

    if missing_fields:
        return jsonify({
            "success": False,
            "error": f"Missing required fields: {', '.join(missing_fields)}",
        }), 400

    # If the client echoed back a masked key from /api/get-config unchanged
    # (matched by endpoint + mask, since this endpoint isn't told a backend
    # name), resolve it to the real stored key before testing.
    try:
        config_path = paths.config_path()
        if config_path.exists():
            stored_config = json.loads(config_path.read_text(encoding="utf-8"))
            for backend in stored_config.get("backends", {}).values():
                if not isinstance(backend, dict):
                    continue
                stored_key = backend.get("api_key", "")
                if (
                    stored_key
                    and backend.get("endpoint") == endpoint
                    and api_key == _mask_api_key(stored_key)
                ):
                    api_key = stored_key
                    break
    except (json.JSONDecodeError, OSError):
        pass  # best-effort resolution only; fall through and test with the value as given

    result = test_connection(endpoint, api_key)

    return jsonify({
        "success": result.success,
        "status_code": result.status_code,
        "error_category": result.error_category,
        "error_detail": result.error_detail,
        "models_available": result.models_available,
        "response_time_ms": result.response_time_ms,
    }), 200


@app.route("/api/save-config", methods=["POST"])
def save_config():
    """Save configuration from the setup wizard."""
    if not _check_same_origin():
        return jsonify({"success": False, "error": "Cross-origin request rejected"}), 403

    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"success": False, "error": "Invalid or missing JSON body"}), 400

    config_path = paths.config_path()
    try:
        if config_path.exists():
            current_config = json.loads(config_path.read_text(encoding="utf-8"))
        else:
            current_config = {}

        # Preserve real API keys when the client echoes back a masked
        # placeholder unchanged (e.g. reopening the settings wizard without
        # editing the key field), so /api/get-config's masked display value
        # never clobbers the stored credential.
        incoming_backends = data.get("backends")
        if isinstance(incoming_backends, dict):
            current_backends = current_config.get("backends", {})
            for name, backend in incoming_backends.items():
                if not isinstance(backend, dict) or not backend.get("api_key"):
                    continue
                incoming_key = backend["api_key"]
                stored_key = current_backends.get(name, {}).get("api_key", "")
                if stored_key and incoming_key == _mask_api_key(stored_key):
                    # Client echoed back its own masked placeholder unchanged - restore the real key.
                    backend["api_key"] = stored_key
                elif incoming_key.endswith("***"):
                    # A masked placeholder that doesn't match this backend's own
                    # stored key (e.g. leaked across backends via the wizard's
                    # prefill) must never be persisted as a literal credential.
                    return jsonify({
                        "success": False,
                        "error": f"Refusing to save a masked placeholder as the API key for '{name}'.",
                    }), 400

        # Deep merge: submitted data overrides current config
        def _deep_merge(base, override):
            merged = base.copy()
            for key, value in override.items():
                if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                    merged[key] = _deep_merge(merged[key], value)
                else:
                    merged[key] = value
            return merged

        merged_config = _deep_merge(current_config, data)

        if "setup_completed" in data:
            merged_config["setup_completed"] = data["setup_completed"]

        config_path.write_text(
            json.dumps(merged_config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        return jsonify({"success": True}), 200

    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


# Global state tracking
is_running = False
last_run_target = ""
last_run_category = ""
last_report_filename = ""
_run_lock = threading.Lock()

_engine_logs = []
_engine_logs_lock = threading.Lock()
_current_engine = None


def _append_engine_log(message: str) -> None:
    with _engine_logs_lock:
        _engine_logs.append(message)


def _build_engine_from_config():
    """Build a fresh OSINTEngine wired from the CURRENT config.json.

    Thin wrapper around the shared osint_workbench.engine_factory (also
    used by main.py's CLI), built per-run rather than once at import time
    so settings saved via the setup wizard take effect on the very next
    run, no server restart needed.

    Returns (engine, event_bus, fetcher) - the caller is responsible for
    closing `fetcher` when the run finishes.
    """
    from osint_workbench.engine_factory import build_engine_from_config
    return build_engine_from_config(event_bus=_gui_event_bus)


def _handle_engine_event(event: Event) -> None:
    """Translate OSINTEngine's coarse lifecycle events into the gui.py
    dashboard's log panel. The engine only emits round/investigation-level
    events (no per-source progress hook exists yet), so the full per-source
    listing is surfaced once, at INVESTIGATION_COMPLETE, where
    state.findings is guaranteed populated - not live-streamed the way the
    legacy main.py's line-per-fetch logging was. See PLAN CAREFULLY report
    for the tracked follow-up (an optional progress_callback on
    ConcurrentFetcher.fetch_batch) if live per-source lines are wanted.
    """
    global last_report_filename
    if event.type == EventType.INVESTIGATION_STARTED:
        _append_engine_log(
            f"[*] Investigation started: {event.data.get('target')} ({event.data.get('category')})"
        )
    elif event.type == EventType.ROUND_STARTED:
        _append_engine_log(f"=== Round {event.data.get('round')} ===")
    elif event.type == EventType.ROUND_COMPLETE:
        _append_engine_log(
            f"[+] Round {event.data.get('round')} complete: {event.data.get('findings_count')} findings"
        )
    elif event.type == EventType.QUERY_SKIPPED:
        _append_engine_log(f"[*] Skipping duplicate query: {event.data.get('query')}")
    elif event.type == EventType.STOP_REQUESTED:
        _append_engine_log("[*] Stop requested. Wrapping up current round...")
    elif event.type == EventType.INVESTIGATION_COMPLETE:
        engine = _current_engine
        state = engine.current_state if engine is not None else None
        if state is not None and state.findings:
            for finding in state.findings.values():
                if isinstance(finding, dict):
                    name = finding.get("name", "")
                    status = finding.get("status", "")
                else:
                    name = getattr(finding, "name", "")
                    status = getattr(finding, "status", "")
                _append_engine_log(f"[*] Fetched: {name} -> {status}")
        html_path = event.data.get("html_path")
        if html_path:
            last_report_filename = os.path.basename(html_path)
        _append_engine_log("[+] Investigation completed successfully.")
    elif event.type == EventType.INVESTIGATION_FAILED:
        _append_engine_log(f"[-] Investigation failed: {event.data}")
    else:
        _append_engine_log(f"[{event.type.value}] {event.data}")


def _forward_event_to_socket(event: Event) -> None:
    """Push an event to all connected WebSocket clients, mirroring
    osint_workbench/api/routes.py's _on_event -- lets the dashboard's
    socket.on('plan_updated'/'extraction_complete'/etc.) handlers (the RAG
    ingest status list, investigation insights panel) actually fire under
    gui.py, not just under the modern create_app() path."""
    socketio.emit(event.type.value, {
        "type": event.type.value,
        "investigation_id": event.investigation_id,
        "data": event.data,
    })


# Subscribed ONCE at module load, not per-run: _gui_event_bus is a
# persistent, process-lifetime bus shared by every /api/run call plus the
# RAG blueprint's background ingest threads (which can fire before any
# investigation has ever run).
for _event_type in EventType:
    _gui_event_bus.subscribe(_event_type, _handle_engine_event)
    _gui_event_bus.subscribe(_event_type, _forward_event_to_socket)

@app.route("/api/run", methods=["POST"])
def api_run():
    global is_running, last_run_target, last_run_category, last_report_filename, _current_engine
    try:
        if not _check_same_origin():
            return jsonify({"success": False, "error": "Cross-origin request rejected"}), 403

        data = request.json or {}
        category = data.get("category", "")
        target = data.get("target", "")
        max_rounds = data.get("max_rounds", "Auto")
        urgency = data.get("urgency", "normal OSINT search")

        if not category or not target:
            return jsonify({"success": False, "error": "Missing target or category"})

        with _run_lock:
            if is_running:
                return jsonify({"success": False, "error": "An investigation is already in progress"})
            is_running = True
            last_run_target = target
            last_run_category = category
            last_report_filename = ""

        with _engine_logs_lock:
            _engine_logs.clear()

        try:
            engine, _unused_event_bus, fetcher = _build_engine_from_config()
            _current_engine = engine
        except Exception:
            with _run_lock:
                is_running = False
            raise

        def run_thread():
            global is_running, _current_engine
            try:
                config = InvestigationConfig(
                    target=target, category=category, max_rounds=max_rounds, urgency=urgency,
                )
                engine.run_investigation(config)
            except Exception as e:
                _append_engine_log(f"[-] Thread execution error: {str(e)}")
            finally:
                try:
                    fetcher.close()
                except Exception:
                    _logger.exception("Failed to close fetcher after run")
                _current_engine = None
                with _run_lock:
                    is_running = False

        # Non-daemon, matching the legacy thread: lets an in-flight
        # investigation finish (and still write its report) if the
        # server process is asked to exit, rather than being killed
        # mid-run with nothing to show for it.
        t = threading.Thread(target=run_thread)
        t.start()
        return jsonify({"success": True, "status": "started"})
    except Exception as e:
        print(f"[-] Error in api_run request handler: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """Gracefully stop the currently running investigation.

    The dashboard JS already calls this route (fetch('/api/stop', ...) in
    stopInvestigation()) but it never existed - clicking "Stop Investigation"
    silently 404'd. OSINTEngine.stop_investigation() makes this a real
    feature: it sets a stop signal the round loop checks at the next round
    boundary, then proceeds straight to report generation on what was found
    so far, instead of leaving no report at all.
    """
    if not _check_same_origin():
        return jsonify({"success": False, "error": "Cross-origin request rejected"}), 403

    engine = _current_engine
    state = engine.current_state if engine is not None else None
    if engine is None or state is None or not engine.is_running:
        return jsonify({"success": False, "error": "No active investigation"}), 400

    try:
        engine.stop_investigation(state.investigation_id)
        return jsonify({"success": True, "message": "Stop signal sent"}), 200
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/status")
def api_status():
    global is_running, last_run_target, last_run_category, last_report_filename
    exists = bool(last_report_filename) and os.path.exists(
        os.path.join(str(paths.reports_dir()), last_report_filename)
    )
    return jsonify({
        "is_running": is_running,
        "report_filename": last_report_filename,
        "exists": exists,
        "target": last_run_target,
        "category": last_run_category
    })

@app.route("/api/logs")
def api_logs():
    with _engine_logs_lock:
        return jsonify(list(_engine_logs))

def extract_category(base_filename):
    md_filename = base_filename.replace(".html", ".md")
    md_path = os.path.join(str(paths.reports_dir()), md_filename)
    if os.path.exists(md_path):
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                lines = [f.readline() for _ in range(10)]
            for line in lines:
                if "**Target Type:**" in line:
                    return line.split("**Target Type:**")[1].strip()
                if "Target Type:" in line:
                    return line.split("Target Type:")[1].strip()
                if "**Subject:**" in line:
                    return "Username"
        except Exception:
            _logger.exception("Failed to extract category from report %s", md_path)
    return "OSINT"

@app.route("/api/history")
def api_history():
    files = glob.glob(str(paths.reports_dir() / "*.html"))
    # Sort files by modification date
    files.sort(key=os.path.getmtime, reverse=True)
    
    history = []
    for f in files:
        base = os.path.basename(f)
        parts = base.replace("report_", "").replace(".html", "").split("_")
        if len(parts) >= 2:
            target = "_".join(parts[:-1])
            date = parts[-1]
        else:
            target = base
            date = ""
            
        category = extract_category(base)
            
        history.append({
            "filename": base,
            "target": target,
            "date": date,
            "category": category
        })
    return jsonify(history)
@app.route("/report/<filename>")
def serve_report(filename):
    # Sanitize filename to prevent directory traversal
    filename = os.path.basename(filename)
    reports_dir = str(paths.reports_dir())
    response = send_from_directory(reports_dir, filename)
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response

if __name__ == "__main__":
    # Start the server on port 5000
    print("[*] Launching OSINT Workbench on http://127.0.0.1:5000 ...")
    
    # Auto open in browser
    def open_browser():
        webbrowser.open("http://127.0.0.1:5000")
        
    threading.Timer(1.5, open_browser).start()
    socketio.run(app, host="127.0.0.1", port=5000, debug=False)
