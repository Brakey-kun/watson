"""Shared OSINTEngine construction, wired from config.json on disk.

Used by both gui.py's /api/run (the Flask dashboard) and main.py (the
standalone one-shot CLI), so both entry points run the SAME tested engine
instead of gui.py's route handler duplicating its own copy of this wiring.
Building fresh per-call (rather than once at process-startup, like the
osint_workbench/app.py Flask factory or the old main.py module-level
`client`/`llm_client` globals) means settings saved via the setup wizard
take effect on the very next run - no server restart or process restart
needed.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

from osint_workbench.core import paths
from osint_workbench.core.config import ConfigLoader
from osint_workbench.core.engine import DEFAULT_STRATEGY, OSINTEngine, STRATEGIES
from osint_workbench.core.events import EventBus
from osint_workbench.core.fetcher import ConcurrentFetcher
from osint_workbench.core.llm_client import LLMClient
from osint_workbench.core.models import AppConfig, InvestigationConfig
from osint_workbench.core.outcome_memory import OutcomeMemory
from osint_workbench.core.plan_object import PlanStore
from osint_workbench.core.quality import QualityPipeline
from osint_workbench.core.search_engines import MultiEngineSearch
from osint_workbench.core.state import StateManager
from osint_workbench.core.steering_index import SteeringIndex
from osint_workbench.core.token_budget import TokenBudgetManager
from osint_workbench.reporting.visual_adapter import VisualReportGenerator

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = "You are an expert OSINT intelligence analyst."


def _load_system_prompt(system_prompt_path: str = "system-prompt.md") -> str:
    """Load a custom system prompt from disk, same convention the legacy
    main.py used, so migrating engines doesn't silently revert the analyst
    persona to the LLMClient's bare default."""
    path = Path(system_prompt_path)
    if path.exists():
        try:
            prompt = path.read_text(encoding="utf-8").strip()
            if prompt:
                return prompt
        except Exception:
            logger.exception("Failed to read %s", system_prompt_path)
    return DEFAULT_SYSTEM_PROMPT


def resolve_backend_params(config: AppConfig) -> tuple[str, str, float, str]:
    """Resolve the (base_url, model, temperature, api_key) an LLMClient
    should be constructed with for `config`'s CURRENTLY ACTIVE backend.

    Shared by build_engine_from_config (below) and osint_workbench/app.py's
    Flask factory, so both entry points resolve backend settings identically
    instead of maintaining two copies that can drift out of sync. This is
    the same resolution /api/switch-backend applies to the live LLMClient
    at runtime -- important because switch-backend only persists
    `llm.backend` to disk, never `llm.model`/`llm.temperature`, so those
    legacy top-level fields go stale the moment a non-default backend is
    selected and must never be trusted over the active backend's own
    values.

    Falls back to the legacy `config.llm.*` fields (and the "lm-studio"
    api_key convention) when `config.llm.backend` doesn't resolve to a
    configured backend, or when the active backend's `model` is an empty
    string (e.g. a half-filled setup-wizard save).
    """
    active_backend = config.backends.get(config.llm.backend)
    base_url = (
        active_backend.endpoint if active_backend is not None
        else f"http://{config.llm.host}:{config.llm.port}/v1"
    )
    model = (active_backend.model if active_backend is not None else None) or config.llm.model
    temperature = (
        active_backend.temperature if active_backend is not None else config.llm.temperature
    )
    api_key = active_backend.api_key if active_backend is not None else "lm-studio"
    return base_url, model, temperature, api_key


class ModelNotConfiguredError(RuntimeError):
    """Raised when an investigation can't start because no LLM model is
    selected for the active backend and none could be safely
    auto-detected from what's currently loaded there."""


def ensure_active_model(base_url: str, model: str, api_key: str) -> Tuple[str, Optional[str]]:
    """Resolve the model an LLMClient for `base_url` should actually use.

    `model` already set -- returned unchanged, no network call. Watson
    never re-validates a configured id against list_models(): some
    OpenAI-compatible backends (hosted APIs in particular) don't
    exhaustively list every servable model there, so treating "not in
    the list" as an error would break working setups.

    `model` empty -- Watson no longer ships a hardcoded guess here, so
    this probes `base_url` for what's currently loaded and auto-selects
    it when there's exactly one candidate. Zero or multiple candidates
    (or an unreachable endpoint) can't be resolved safely, so an error
    naming what IS available is returned instead of guessing wrong.

    Returns (resolved_model, error). When `error` is set, resolved_model
    is "" and MUST NOT be used to construct or run an LLMClient.
    """
    if model:
        return model, None
    probe = LLMClient(base_url=base_url, api_key=api_key or "lm-studio").list_models()
    if probe["error"]:
        return "", (
            "No model selected, and the backend couldn't be reached to "
            f"auto-detect one ({probe['error']}). Start your backend, then "
            "assign one in Settings \u2192 Assign Model to Backend."
        )
    models = probe["models"]
    if len(models) == 1:
        return models[0], None
    if not models:
        return "", (
            "No model selected, and none are currently loaded at the "
            "backend. Load a model, then assign it in Settings \u2192 Assign Model to Backend."
        )
    return "", (
        "No model selected, and multiple are loaded at the backend "
        f"({', '.join(models)}). Assign one in Settings \u2192 Assign Model to Backend."
    )


def build_engine_from_config(
    config_path: str | None = None,
    reports_dir: str | None = None,
    db_path: str | None = None,
    system_prompt_path: str = "system-prompt.md",
    event_bus: EventBus | None = None,
) -> tuple[OSINTEngine, EventBus, ConcurrentFetcher]:
    """Build a fresh OSINTEngine wired from the CURRENT config.json.

    Returns (engine, event_bus, fetcher). The caller owns `fetcher` and is
    responsible for calling `fetcher.close()` once the run finishes.

    Args:
        event_bus: Reuse this bus instead of constructing a fresh one --
            lets a caller keep persistent subscribers (e.g. gui.py's
            SocketIO forwarder) attached across multiple runs instead of
            resubscribing to a new throwaway bus every call.
    """
    config_path = config_path if config_path is not None else str(paths.config_path())
    reports_dir = reports_dir if reports_dir is not None else str(paths.reports_dir())
    db_path = db_path if db_path is not None else str(paths.db_path())
    config_loader = ConfigLoader(config_path=config_path)
    config = config_loader.load()

    system_prompt = _load_system_prompt(system_prompt_path)

    llm_base_url, llm_model, llm_temperature, llm_api_key = resolve_backend_params(config)
    llm_model, model_error = ensure_active_model(llm_base_url, llm_model, llm_api_key)
    if model_error:
        raise ModelNotConfiguredError(model_error)
    llm_client = LLMClient(
        base_url=llm_base_url,
        model=llm_model,
        temperature=llm_temperature,
        max_retries=config.llm.max_retries,
        system_prompt=system_prompt,
        api_key=llm_api_key,
    )

    fetcher = ConcurrentFetcher(
        max_workers=config.fetcher.max_workers,
        timeout=config.fetcher.timeout_seconds,
        max_retries=config.fetcher.max_retries,
        rate_limit_per_second=config.fetcher.rate_limit_per_second,
    )
    quality_pipeline = QualityPipeline(
        noise_patterns=config.quality.noise_patterns or None,
        min_relevance_score=config.quality.min_relevance_score,
    )
    token_budget = TokenBudgetManager(context_window=config.llm.max_context_tokens)
    search_engine = MultiEngineSearch(
        rate_limit_per_engine=config.search.rate_limit_per_engine,
        jitter_range=(config.search.jitter_min, config.search.jitter_max),
    )
    report_generator = VisualReportGenerator(output_dir=reports_dir)
    event_bus = event_bus if event_bus is not None else EventBus()
    state_manager = StateManager(db_path=db_path, event_bus=event_bus)
    steering_index = SteeringIndex(db_path=db_path)
    plan_store = PlanStore(db_path=db_path)
    outcome_memory = OutcomeMemory(db_path=db_path)

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
    return engine, event_bus, fetcher


def build_investigation_config(
    data: dict,
    *,
    require_category: bool = False,
    default_urgency: str = "normal",
) -> Tuple[Optional[InvestigationConfig], Optional[str]]:
    """Parse a POST /api/run request body into an InvestigationConfig, or
    return (None, error_message) if a required field is missing.

    Shared by gui.py's and osint_workbench/api/routes.py's /api/run
    handlers -- the two Flask hosts this project ships (see rag_routes.py's
    docstring for why they can't just share one Blueprint here: both
    already define their own /api/run and registering a second one would
    collide). Centralizing target/category/project_id/strategy parsing
    here means the Projects/Strategy fields can't drift between the two
    handlers, and a test exercising this function directly is guaranteed
    to cover exactly what gui.py (the launchers' actual entry point) runs
    at request-parse time, not just routes.py's separately-tested copy.

    `require_category`/`default_urgency` let each caller keep its own
    pre-existing validation contract instead of silently changing either
    handler's user-facing behavior:
    - gui.py requires an explicit category (no auto-detect fallback) and
      defaults urgency to "normal OSINT search" -- pass
      require_category=True, default_urgency="normal OSINT search".
    - routes.py defaults category to "Auto-Detect" and urgency to
      "normal" when absent -- pass require_category=False (the default).

    Args:
        data: The parsed JSON request body.
        require_category: If True, a missing/blank category is treated as
            a validation error (matching "Missing target or category").
            If False, a missing/blank category defaults to "Auto-Detect".
        default_urgency: Urgency value used when the request omits one.

    Returns:
        (config, None) on success, or (None, error_message) if target (or,
        when require_category=True, category) is missing/blank.
    """
    target = str(data.get("target", "")).strip()
    category = str(data.get("category", "")).strip()

    if require_category:
        if not target or not category:
            return None, "Missing target or category"
    else:
        if not target:
            return None, "Target is required"
        if not category:
            category = "Auto-Detect"

    # Projects feature: optional at this layer (a missing/None project_id
    # is valid -- the dashboard UI no longer prompts the user to pick one;
    # it auto-mints a fresh, independent scope per investigation via
    # ensureInvestigationScope() before /api/run is ever called, so a
    # caller here without one is the CLI/tests/any non-dashboard caller,
    # which keeps working via the GLOBAL_SCOPE fallback). `data.get(...)
    # or ""` guards against an explicit JSON `null` (str(None) == "None",
    # which is truthy and would silently scope hints under the literal,
    # unreadable project "None").
    project_id = str(data.get("project_id") or "").strip() or None
    strategy = str(data.get("strategy") or "").strip() or DEFAULT_STRATEGY
    if strategy not in STRATEGIES:
        strategy = DEFAULT_STRATEGY

    config = InvestigationConfig(
        target=target,
        category=category,
        max_rounds=data.get("max_rounds", "Auto"),
        urgency=data.get("urgency", default_urgency),
        lm_studio_url=data.get("lm_studio_url"),
        enable_pdf=bool(data.get("enable_pdf", False)),
        project_id=project_id,
        strategy=strategy,
    )
    return config, None
