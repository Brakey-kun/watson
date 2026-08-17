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

from osint_workbench.core import paths
from osint_workbench.core.config import ConfigLoader
from osint_workbench.core.engine import OSINTEngine
from osint_workbench.core.events import EventBus
from osint_workbench.core.fetcher import ConcurrentFetcher
from osint_workbench.core.llm_client import LLMClient
from osint_workbench.core.models import AppConfig
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
