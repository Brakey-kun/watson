"""Flask + SocketIO application factory for Watson.

Creates and configures the Flask app with all components wired together.
Uses the app factory pattern for testability with different configs.

Requirements: 1.1, 7.4
"""

import atexit
import logging
import os
from typing import Tuple

from flask import Flask, jsonify
from flask_socketio import SocketIO

from osint_workbench.api.admin_routes import create_admin_blueprint
from osint_workbench.api.model_routes import create_model_blueprint
from osint_workbench.api.project_routes import create_project_blueprint
from osint_workbench.api.rag_routes import create_rag_blueprint
from osint_workbench.api.routes import create_api_blueprint
from osint_workbench.api.skills_routes import create_skills_blueprint
from osint_workbench.core import paths
from osint_workbench.core.config import ConfigLoader, ConfigurationError
from osint_workbench.core.engine import OSINTEngine
from osint_workbench.core.events import EventBus
from osint_workbench.core.fetcher import ConcurrentFetcher
from osint_workbench.core.llm_client import LLMClient
from osint_workbench.core.models import AppConfig
from osint_workbench.core.outcome_memory import OutcomeMemory
from osint_workbench.core.plan_object import PlanStore
from osint_workbench.core.project_store import ProjectStore
from osint_workbench.core.quality import QualityPipeline
from osint_workbench.core.search_engines import MultiEngineSearch
from osint_workbench.core.state import StateManager
from osint_workbench.core.steering_index import SteeringIndex
from osint_workbench.core.token_budget import TokenBudgetManager
from osint_workbench.engine_factory import resolve_backend_params
from osint_workbench.reporting.generator import ReportGenerator

logger = logging.getLogger(__name__)


def create_app(config_path: str | None = None) -> Tuple[Flask, SocketIO]:
    """Create and configure the Flask + SocketIO application.

    Initializes all OSINT Workbench components in dependency order,
    registers API routes, and sets up shutdown hooks.

    Args:
        config_path: Path to the configuration JSON file. Defaults to the
            external per-user data directory when omitted.

    Returns:
        Tuple of (Flask app, SocketIO instance).
    """
    # --- Load configuration ---
    config_path = config_path if config_path is not None else str(paths.config_path())
    config_loader = ConfigLoader(config_path=config_path)
    try:
        config = config_loader.load()
    except (FileNotFoundError, ConfigurationError) as e:
        logger.warning(
            "Failed to load config from '%s': %s. Using defaults.", config_path, e
        )
        config = AppConfig()

    # --- Create Flask app ---
    static_folder = os.path.join(os.path.dirname(__file__), "..", "static")
    template_folder = os.path.join(os.path.dirname(__file__), "..", "templates")

    app = Flask(
        __name__,
        static_folder=static_folder if os.path.isdir(static_folder) else None,
        template_folder=template_folder if os.path.isdir(template_folder) else None,
    )

    # Store the ConfigLoader instance and app config for use by API endpoints
    app.config["CONFIG_LOADER"] = config_loader
    app.config["APP_CONFIG"] = config
    # Caps request body size (chiefly /api/upload-context file uploads) so
    # a large upload can't stall the eventlet/threading worker or exhaust
    # memory. Flask returns a bare-HTML 413 by default, which dashboard.html's
    # unconditional `.then(r => r.json())` fetch handling can't parse --
    # the JSON handler below keeps the failure visible to the user instead
    # of a silent, uncaught parse error.
    app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB

    @app.errorhandler(413)
    def _upload_too_large(_exc):
        return jsonify({"success": False, "error": "File exceeds the 25 MB upload limit"}), 413

    # --- Create SocketIO instance ---
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

    # --- Initialize components in dependency order ---

    # 1. EventBus (no dependencies)
    event_bus = EventBus()

    # 2. StateManager (depends on EventBus)
    state_manager = StateManager(event_bus=event_bus)
    steering_index = SteeringIndex()
    plan_store = PlanStore()
    outcome_memory = OutcomeMemory()
    project_store = ProjectStore()

    # Store on app for access by API endpoints (RAG ingest + visibility panel)
    app.config["STEERING_INDEX"] = steering_index
    app.config["PLAN_STORE"] = plan_store
    app.config["OUTCOME_MEMORY"] = outcome_memory
    app.config["PROJECT_STORE"] = project_store

    # 3. LLMClient (depends on config)
    llm_base_url, llm_model, llm_temperature, llm_api_key = resolve_backend_params(config)
    llm_client = LLMClient(
        base_url=llm_base_url,
        model=llm_model,
        temperature=llm_temperature,
        api_key=llm_api_key,
    )

    # Store LLMClient on app for access by API endpoints (e.g., switch-backend)
    app.config["LLM_CLIENT"] = llm_client

    # 4. ConcurrentFetcher (depends on config)
    fetcher = ConcurrentFetcher(
        max_workers=config.fetcher.max_workers,
        timeout=config.fetcher.timeout_seconds,
        rate_limit_per_second=config.fetcher.rate_limit_per_second,
    )

    # 5. QualityPipeline (depends on config)
    quality_pipeline = QualityPipeline(
        min_relevance_score=config.quality.min_relevance_score,
    )

    # 6. TokenBudgetManager (depends on config)
    token_budget = TokenBudgetManager(
        context_window=config.llm.max_context_tokens,
    )

    # 7. MultiEngineSearch (depends on config)
    search_engine = MultiEngineSearch(
        rate_limit_per_engine=config.search.rate_limit_per_engine,
        jitter_range=(config.search.jitter_min, config.search.jitter_max),
    )

    # 8. ReportGenerator (standalone)
    report_generator = ReportGenerator()

    # 9. OSINTEngine (depends on all of the above)
    engine = OSINTEngine(
        config=config,
        event_bus=event_bus,
        state_manager=state_manager,
        llm_client=llm_client,
        fetcher=fetcher,
        quality_pipeline=quality_pipeline,
        token_budget_manager=token_budget,
        search_engine=search_engine,
        report_generator=report_generator,
        steering_index=steering_index,
        plan_store=plan_store,
        outcome_memory=outcome_memory,
    )

    # Store engine on app config for access by CLI signal handler
    app.config["ENGINE"] = engine

    # --- Register API blueprint ---
    api_blueprint = create_api_blueprint(
        engine=engine,
        state_manager=state_manager,
        event_bus=event_bus,
        socketio=socketio,
    )
    app.register_blueprint(api_blueprint)

    # --- Register RAG ingest / investigation-insights blueprint ---
    # Separate from api_blueprint so gui.py (the legacy dashboard) can
    # register it too without route collisions -- see rag_routes.py.
    rag_blueprint = create_rag_blueprint(
        event_bus=event_bus,
        get_investigation_id=api_blueprint.get_current_investigation_id,
    )
    app.register_blueprint(rag_blueprint)

    # --- Register model-selector / project CRUD / skills / admin blueprints ---
    # Same narrow-Blueprint convention as rag_blueprint: registered by both
    # Flask hosts (this factory and the legacy gui.py dashboard), reading
    # their dependencies from current_app.config at request time.
    app.register_blueprint(create_model_blueprint())
    app.register_blueprint(create_project_blueprint(get_active_project_id=api_blueprint.get_active_project_id))
    app.register_blueprint(create_skills_blueprint())
    # This host has no same-origin/CSRF mechanism of its own yet (see
    # admin_routes.py's docstring) -- reset is still guarded by the SAME
    # is_running lock every other run-guarded route here shares, just not
    # by an Origin check, matching this host's existing routes' lack of
    # one rather than introducing a one-off exception.
    app.register_blueprint(create_admin_blueprint(
        run_lock=api_blueprint.run_lock,
        get_is_running=api_blueprint.get_is_running,
        check_origin=lambda: True,
        on_reset=api_blueprint.reset_state,
    ))

    # --- Set up shutdown hook ---
    atexit.register(fetcher.close)

    logger.info("Watson OSINT Workbench application initialized successfully.")

    return app, socketio
