"""OSINT Engine orchestrator.

Central orchestrator that manages the multi-round research loop, coordinates
all sub-components, maintains investigation state, and enforces lifecycle rules.
"""

import json
import logging
import threading
import time
import urllib.parse
import uuid
from typing import Optional

from osint_workbench.core import plan_object
from osint_workbench.core.events import Event, EventBus, EventType
from osint_workbench.core.fetcher import ConcurrentFetcher
from osint_workbench.core.llm_client import LLMClient, LLMClientError
from osint_workbench.core.models import (
    AppConfig,
    ClaimStatus,
    Finding,
    InvestigationConfig,
    InvestigationState,
    InvestigationStatus,
    QueryReason,
)
from osint_workbench.core.outcome_memory import (
    ContradictionDetector,
    DoubtBudget,
    OutcomeMemory,
)
from osint_workbench.core.plan_object import (
    Hypothesis,
    InvestigationPlan,
    PlanState,
    PlanStore,
    QueuedQuery,
)
from osint_workbench.core.quality import QualityPipeline
from osint_workbench.core.query_normalizer import normalize_query
from osint_workbench.core.search_engines import MultiEngineSearch
from osint_workbench.core.state import StateManager
from osint_workbench.core.steering_index import SteeringIndex
from osint_workbench.core.token_budget import TokenBudgetManager
from osint_workbench.reporting.generator import ReportGenerator

logger = logging.getLogger(__name__)


# Valid categories for classification
VALID_CATEGORIES = frozenset({
    "Username", "Domain", "Email", "Telephone", "Location", "Business"
})

# Valid state transitions (from_status -> set of allowed to_statuses)
VALID_TRANSITIONS = {
    InvestigationStatus.QUEUED: {InvestigationStatus.RUNNING},
    InvestigationStatus.RUNNING: {
        InvestigationStatus.COMPLETED,
        InvestigationStatus.FAILED,
        InvestigationStatus.PAUSED,
        InvestigationStatus.CANCELLED,
    },
    InvestigationStatus.PAUSED: {
        InvestigationStatus.RUNNING,
        InvestigationStatus.CANCELLED,
    },
}


# System prompt for category classification
_CLASSIFY_SYSTEM_PROMPT = "You output strictly one category name."

_CLASSIFY_PROMPT_TEMPLATE = (
    "Classify the following OSINT target input: '{target}'.\n"
    "Select the most appropriate category from this list:\n"
    "- 'Username' (if it looks like a handle, alias, or nickname)\n"
    "- 'Domain' (if it looks like a website, host, domain name, or URL)\n"
    "- 'Email' (if it looks like an email address)\n"
    "- 'Telephone' (if it looks like a phone number)\n"
    "- 'Location' (if it looks like a street address, city, state, zip, or coordinates)\n"
    "- 'Business' (if it looks like a company name, legal entity, or personal full name)\n\n"
    "Return ONLY the category name exactly as listed. "
    "Do not include markdown formatting or extra words."
)

_GEOCODE_SYSTEM_PROMPT = "You output strictly valid JSON."

_GEOCODE_PROMPT_TEMPLATE = (
    "You are an address parser. Parse the following address/location input: '{target}'.\n"
    "Extract coordinates (latitude/longitude) if present, and split address components.\n"
    "Return ONLY a JSON object containing keys: 'lat', 'lng', 'street', 'city', 'state', 'zip'. "
    "Do not include markdown blocks."
)


class OSINTEngine:
    """Central orchestrator for OSINT investigations.

    Manages the multi-round research loop, coordinates sub-components,
    maintains investigation state, enforces lifecycle rules, and emits events.
    """

    def __init__(
        self,
        config: AppConfig,
        sources_path: str = "sources.json",
        event_bus: Optional[EventBus] = None,
        state_manager: Optional[StateManager] = None,
        llm_client: Optional[LLMClient] = None,
        fetcher: Optional[ConcurrentFetcher] = None,
        quality_pipeline: Optional[QualityPipeline] = None,
        token_budget_manager: Optional[TokenBudgetManager] = None,
        search_engine: Optional[MultiEngineSearch] = None,
        report_generator: Optional[ReportGenerator] = None,
        steering_index: Optional[SteeringIndex] = None,
        plan_store: Optional[PlanStore] = None,
        outcome_memory: Optional[OutcomeMemory] = None,
    ):
        """Initialize the OSINT Engine with dependency injection.

        Args:
            config: Application configuration.
            sources_path: Path to sources.json file.
            event_bus: EventBus for emitting lifecycle events.
            state_manager: StateManager for checkpoint persistence.
            llm_client: LLMClient for LLM communication.
            fetcher: ConcurrentFetcher for HTTP fetching.
            quality_pipeline: QualityPipeline for scoring/filtering.
            token_budget_manager: TokenBudgetManager for context window.
            search_engine: MultiEngineSearch for adaptive queries.
            report_generator: ReportGenerator for output generation.
            steering_index: SteeringIndex for fuzzy query dedup and plan
                reconciliation. Without one, the adaptive loop still runs
                (single replan, drain queue, stop) but loses fuzzy dedup,
                decay, and the periodic staleness re-grounding floor.
            plan_store: PlanStore for persisting/resuming InvestigationPlan
                across pause/resume. Without one, a resumed investigation
                starts its plan fresh instead of picking up mid-plan.
            outcome_memory: OutcomeMemory for claim tracking and contradiction
                detection. Without one, the round loop skips claim
                extraction entirely and doubt-search verification queries
                are never dispatched.
        """
        self._config = config
        self._sources_path = sources_path
        self._event_bus = event_bus or EventBus()
        self._state_manager = state_manager
        self._llm_client = llm_client
        self._fetcher = fetcher
        self._quality_pipeline = quality_pipeline or QualityPipeline()
        self._token_budget = token_budget_manager
        self._search_engine = search_engine
        self._report_generator = report_generator
        self._steering_index = steering_index
        self._plan_store = plan_store
        self._outcome_memory = outcome_memory

        # State tracking
        self._current_state: Optional[InvestigationState] = None
        self._is_running = False
        self._pause_event = threading.Event()
        self._pause_event.set()  # Not paused initially (set = not paused)
        self._stop_event = threading.Event()
        # _stop_event starts cleared (not stopped)
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        """Whether an investigation is currently running."""
        return self._is_running

    @property
    def current_state(self) -> Optional[InvestigationState]:
        """The current investigation state, if any."""
        return self._current_state

    def _validate_transition(
        self, from_status: InvestigationStatus, to_status: InvestigationStatus
    ) -> bool:
        """Check if a status transition is valid per the state machine.

        Args:
            from_status: Current status.
            to_status: Desired new status.

        Returns:
            True if the transition is valid, False otherwise.
        """
        allowed = VALID_TRANSITIONS.get(from_status, set())
        return to_status in allowed

    def _set_status(self, state: InvestigationState, new_status: InvestigationStatus) -> None:
        """Set investigation status with transition validation.

        Args:
            state: The investigation state to update.
            new_status: The target status.

        Raises:
            ValueError: If the transition is invalid.
        """
        if not self._validate_transition(state.status, new_status):
            raise ValueError(
                f"Invalid status transition: {state.status.value} -> {new_status.value}"
            )
        state.status = new_status

    def _load_sources(self, category: str) -> list:
        """Load sources from sources.json filtered by category.

        Args:
            category: The investigation category to filter by.

        Returns:
            List of source dicts with 'name' and 'url' keys.
        """
        try:
            with open(self._sources_path, "r", encoding="utf-8") as f:
                all_sources = json.load(f)
            return all_sources.get(category, [])
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error("Failed to load sources from %s: %s", self._sources_path, e)
            return []

    def _auto_detect_category(self, target: str) -> str:
        """Auto-detect the investigation category using the LLM.

        Falls back to 'Username' if the LLM returns an unrecognized value.

        Args:
            target: The investigation target string.

        Returns:
            A valid category string.
        """
        if self._llm_client is None:
            logger.warning("No LLM client configured, defaulting to 'Username'")
            return "Username"

        try:
            prompt = _CLASSIFY_PROMPT_TEMPLATE.format(target=target)
            model, temperature = self._resolve_tier("small")
            response = self._llm_client.ask(
                prompt, system_prompt=_CLASSIFY_SYSTEM_PROMPT, model=model, temperature=temperature
            )
            detected = response.content.strip().replace("'", "").replace('"', "").strip()

            # Try to extract a valid category from the response
            for category in VALID_CATEGORIES:
                if category in detected:
                    logger.info("Auto-detected category: %s", category)
                    return category

            # Unrecognized value — default to Username
            logger.warning(
                "Unrecognized auto-detect result '%s', defaulting to 'Username'",
                detected,
            )
            return "Username"

        except LLMClientError as e:
            logger.error("LLM category detection failed: %s. Defaulting to 'Username'", e)
            return "Username"

    def _parse_format_params(self, category: str, target: str) -> dict:
        """Parse format parameters based on the category.

        Handles telephone digit extraction, location geocoding, and
        default target parameter for other categories.

        Args:
            category: The resolved investigation category.
            target: The raw target string.

        Returns:
            Dict of format parameters for URL templates.
        """
        format_params = {"target": target}

        if category == "Telephone":
            digits = "".join(c for c in target if c.isdigit())
            if len(digits) == 10:
                format_params["target_area"] = digits[0:3]
                format_params["target_prefix"] = digits[3:6]
                format_params["target_line"] = digits[6:10]
            elif len(digits) == 11 and digits.startswith("1"):
                format_params["target_area"] = digits[1:4]
                format_params["target_prefix"] = digits[4:7]
                format_params["target_line"] = digits[7:11]
            else:
                parts = target.split("-")
                if len(parts) == 3:
                    format_params["target_area"] = parts[0]
                    format_params["target_prefix"] = parts[1]
                    format_params["target_line"] = parts[2]
                else:
                    format_params["target_area"] = target
                    format_params["target_prefix"] = target
                    format_params["target_line"] = target

        elif category == "Location":
            format_params.update(self._parse_location_params(target))

        return format_params

    def _parse_location_params(self, target: str) -> dict:
        """Parse location-specific format parameters using LLM geocoding.

        Args:
            target: The location target string.

        Returns:
            Dict with lat, lng, street, city, state, zip keys.
        """
        defaults = {
            "target_lat": "",
            "target_lng": "",
            "target_street": "",
            "target_city": "",
            "target_state": "",
            "target_zip": "",
        }

        if self._llm_client is None:
            return defaults

        try:
            prompt = _GEOCODE_PROMPT_TEMPLATE.format(target=target)
            model, temperature = self._resolve_tier("small")
            geo_data = self._llm_client.ask_json(
                prompt, system_prompt=_GEOCODE_SYSTEM_PROMPT, model=model, temperature=temperature
            )
            return {
                "target_lat": str(geo_data.get("lat", "")),
                "target_lng": str(geo_data.get("lng", "")),
                "target_street": str(geo_data.get("street", "")),
                "target_city": str(geo_data.get("city", "")),
                "target_state": str(geo_data.get("state", "")),
                "target_zip": str(geo_data.get("zip", "")),
            }
        except LLMClientError as e:
            logger.error("Location geocoding failed: %s", e)
            return defaults

    def _resolve_max_rounds(self, max_rounds) -> int:
        """Resolve the max_rounds config value to an integer.

        Args:
            max_rounds: Either "Auto" or an integer value.

        Returns:
            Integer max rounds, capped between 1 and 50.
        """
        if max_rounds == "Auto" or max_rounds == "auto":
            return 50
        try:
            rounds = int(max_rounds)
            return max(1, min(50, rounds))
        except (ValueError, TypeError):
            return 50

    def _format_hint_context(self, investigation_id: str, k: int = 6) -> str:
        """Format user-provided RAG hints (steering_index entry_type='hint')
        as a prompt fragment, or "" if none exist.

        rag_ingest.ingest_context() writes hints to GLOBAL_SCOPE (see its
        module docstring for why investigation-scoped writes are unsafe
        today), so GLOBAL_SCOPE is read unconditionally here.
        investigation_id's own scope is also read for forward
        compatibility, in case a future caller ever writes hints there
        directly.
        """
        if not self._steering_index:
            return ""
        hints = list(self._steering_index.top(SteeringIndex.GLOBAL_SCOPE, "hint", k=k))
        if investigation_id != SteeringIndex.GLOBAL_SCOPE:
            hints += self._steering_index.top(investigation_id, "hint", k=k)
        if not hints:
            return ""
        hints.sort(key=lambda e: e.pheromone, reverse=True)
        lines = "\n".join(f"- {e.payload} (trust: {e.pheromone:.2f})" for e in hints[:k])
        return (
            f"\nUser-provided context hints (from uploaded documents/images/"
            f"snippets -- weigh by trust, higher = more reliable):\n{lines}\n"
        )

    def _build_replan_prompt(
        self,
        plan: InvestigationPlan,
        findings_for_llm: list,
        config: InvestigationConfig,
        current_round: int,
    ) -> str:
        """Build the thinker-tier prompt that re-derives `plan`: an updated
        subject profile, any new hypotheses worth tracking, and a fresh
        batch of queued queries with an auditable reason each.

        Only called when plan.needs_replan() -- most rounds drain the
        existing queue at zero LLM cost instead (see _run_adaptive_loop).
        """
        urgency_instruction = ""
        if config.urgency in [
            "missing person search",
            "critical data search",
            "legal leads and evidence search",
            "potential criminal search and identification",
        ]:
            urgency_instruction = (
                f"\nCRITICAL URGENCY INSTRUCTION: The urgency mode is '{config.urgency}'. "
                f"Aggressively generate targeted search dorks and try alternative sources."
            )

        reason_options = ", ".join(
            f"'{r.value}'" for r in QueryReason
            if r not in (QueryReason.BURST_SEED, QueryReason.DOUBT_VERIFICATION)
        )

        return (
            f"You are an expert OSINT planner. Analyze findings for target "
            f"'{config.target}' (type: {config.category}).\n"
            f"Urgency Mode: {config.urgency}{urgency_instruction}\n"
            f"{self._format_hint_context(plan.investigation_id)}"
            f"Plan state at round {current_round}: {plan.state.value} "
            f"(this is why a fresh plan is needed now).\n"
            f"Existing hypotheses: "
            f"{json.dumps([h.statement for h in plan.hypotheses], indent=2)}\n"
            f"Current findings:\n"
            f"{json.dumps(findings_for_llm[:20], indent=2)}\n\n"
            f"Task:\n"
            f"1. Update the subject profile with anything newly learned.\n"
            f"2. Propose any new hypotheses worth tracking (short statements).\n"
            f"3. Generate up to 3-5 additional high-impact search queries, each "
            f"tagged with why it's worth running.\n"
            f"4. Decide if research should continue at all.\n\n"
            f"Return ONLY a JSON object with keys:\n"
            f"- 'subject_profile' (object of freeform key/value facts)\n"
            f"- 'hypotheses' (array of short statement strings)\n"
            f"- 'continue_research' (boolean)\n"
            f"- 'queries' (array of objects with 'query' (string), "
            f"'category' (string), 'reason' (one of {reason_options}), "
            f"'priority' (number 0-1, higher = more valuable))\n"
        )

    def _build_synthesis_prompt(
        self, all_findings_map: dict, config: InvestigationConfig
    ) -> str:
        """Build the final report synthesis prompt.

        Args:
            all_findings_map: All collected findings keyed by URL.
            config: The investigation configuration.

        Returns:
            The synthesis prompt string.
        """
        compressed = []
        for f in all_findings_map.values():
            if isinstance(f, dict):
                compressed.append(f)
            else:
                compressed.append({"url": f.url, "name": f.name, "status": f.status})

        return (
            f"You are a Senior OSINT Intelligence Analyst writing a final report.\n"
            f"Target: '{config.target}' (Category: {config.category})\n\n"
            f"Write a comprehensive OSINT research report with sections:\n"
            f"## Key Profile Details\n"
            f"## Executive Summary\n"
            f"## Detailed OSINT Analysis\n"
            f"## Conclusion\n\n"
            f"Evidence:\n{json.dumps(compressed[:50], indent=2)}\n\n"
            f"Write ONLY the Markdown report."
        )

    def run_investigation(self, config: InvestigationConfig) -> InvestigationState:
        """Execute a full OSINT investigation.

        Main entry point. Orchestrates category detection, concurrent fetching,
        quality filtering, adaptive research loop, and report generation.

        Args:
            config: The investigation configuration.

        Returns:
            Final InvestigationState with status COMPLETED or FAILED.

        Raises:
            RuntimeError: If an investigation is already running.
        """
        # Check if already running (reject with error)
        with self._lock:
            if self._is_running:
                raise RuntimeError("An investigation is already in progress.")
            self._is_running = True

        # Reset pause and stop events
        self._pause_event.set()
        self._stop_event.clear()  # Reset stop signal for new run

        # Initialize state
        investigation_id = str(uuid.uuid4())
        state = InvestigationState(
            investigation_id=investigation_id,
            config=config,
            status=InvestigationStatus.QUEUED,
            current_round=0,
            findings={},
            round_plans=[],
            elapsed_seconds=0.0,
        )
        self._current_state = state
        start_time = time.time()

        try:
            # Transition QUEUED -> RUNNING
            self._set_status(state, InvestigationStatus.RUNNING)

            # Emit investigation_started event
            self._event_bus.emit(Event(
                type=EventType.INVESTIGATION_STARTED,
                investigation_id=investigation_id,
                data={"target": config.target, "category": config.category},
            ))

            # Auto-detect category if needed
            category = config.category
            if category == "Auto-Detect":
                category = self._auto_detect_category(config.target)
                config.category = category

            # Parse format parameters based on category
            format_params = self._parse_format_params(category, config.target)

            # Load sources filtered by category
            sources = self._load_sources(category)

            # Round 1: Concurrent base sweep
            state.current_round = 1
            self._event_bus.emit(Event(
                type=EventType.ROUND_STARTED,
                investigation_id=investigation_id,
                data={"round": 1},
            ))

            all_findings_map: dict = {}

            if self._fetcher and sources:
                results = self._fetcher.fetch_batch(sources, format_params)

                # Convert FetchResult objects to dicts for quality pipeline
                findings_dicts = []
                for r in results:
                    finding_dict = {
                        "url": r.url,
                        "name": r.name,
                        "status": r.status,
                        "title": r.title or "",
                        "snippet": r.snippet or "",
                        "category": r.category,
                    }
                    findings_dicts.append(finding_dict)

                # Quality filter results
                scored_results = self._quality_pipeline.filter_and_score(
                    findings_dicts, config.target
                )

                # Build all_findings_map from scored results
                for sf in scored_results:
                    if not sf.is_noise:
                        all_findings_map[sf.url] = {
                            "url": sf.url,
                            "name": sf.name,
                            "status": "Active/Accessible",
                            "title": sf.title,
                            "snippet": sf.snippet,
                            "category": sf.category,
                            "relevance_score": sf.relevance_score,
                        }

            self._event_bus.emit(Event(
                type=EventType.ROUND_COMPLETE,
                investigation_id=investigation_id,
                data={"round": 1, "findings_count": len(all_findings_map)},
            ))

            # Checkpoint after round 1
            state.findings = all_findings_map
            state.elapsed_seconds = time.time() - start_time
            if self._state_manager:
                self._state_manager.save_checkpoint(state)

            # Adaptive research loop (rounds 2..N), driven by a persistent
            # InvestigationPlan instead of a fresh LLM prompt every round.
            # Burst search (if enabled) seeds the plan with the highest-
            # scoring of several divergent candidate angles instead of the
            # single deterministic fresh plan _load_or_init_plan builds.
            burst_plan = self._run_burst_search(investigation_id, config)
            burst_categories = (
                {q.category for q in burst_plan.queued_queries} if burst_plan else None
            )
            plan = burst_plan or self._load_or_init_plan(investigation_id, config)
            target_rounds = self._resolve_max_rounds(config.max_rounds)

            all_findings_map, plan, paused = self._run_adaptive_loop(
                state, config, all_findings_map, plan, format_params,
                start_time, 2, target_rounds,
            )
            if paused:
                return state

            return self._finalize_investigation(
                state, all_findings_map, config, start_time,
                burst_categories=burst_categories,
            )

        except Exception as e:
            return self._handle_investigation_exception(
                state, e, start_time, "Investigation failed with exception"
            )

        finally:
            with self._lock:
                self._is_running = False

    def _load_or_init_plan(
        self, investigation_id: str, config: InvestigationConfig
    ) -> InvestigationPlan:
        """Load a persisted plan for `investigation_id` (resume path), or
        construct a fresh one that immediately needs a thinker-tier replan.

        A fresh plan starts in QUEUE_EXHAUSTED (not the dataclass default
        EXPLORING) precisely so the first round-2 iteration of
        _run_adaptive_loop calls _replan() instead of silently popping
        from an empty queue and wasting a round.
        """
        if self._plan_store is not None:
            existing = self._plan_store.load_latest(investigation_id)
            if existing is not None:
                return existing
        return InvestigationPlan(
            investigation_id=investigation_id,
            subject_profile={"target": config.target, "category": config.category},
            state=PlanState.QUEUE_EXHAUSTED,
        )

    def _resolve_tier(self, tier_name: str) -> tuple:
        """Resolve the (model, temperature) override for a steering tier
        ('thinker', 'default', or 'small') from AppConfig.tiers.

        An empty tier model means "use the active backend's configured
        model" (TierModelConfig's documented default) -- returns None
        rather than "" so LLMClient.ask()/ask_json() fall back to the
        client's own configured model/temperature instead of overriding
        with an empty string.
        """
        tier = getattr(self._config.tiers, tier_name, None)
        if tier is None:
            return None, None
        return (tier.model or None), tier.temperature

    def _build_burst_search_prompt(
        self, investigation_id: str, config: InvestigationConfig, probe_count: int
    ) -> str:
        """Build the thinker-tier prompt that proposes `probe_count`
        divergent first-pass research angles for burst search."""
        return (
            f"You are an expert OSINT investigator planning a fresh investigation.\n"
            f"Target: '{config.target}' (Category: {config.category})\n"
            f"{self._format_hint_context(investigation_id)}\n"
            f"Propose {probe_count} DIFFERENT, genuinely divergent research angles "
            f"for this investigation -- each should pursue a distinct kind of "
            f"evidence (e.g. one might chase social presence, another public "
            f"records, another professional history) rather than variations on "
            f"the same idea.\n\n"
            f"For each angle, provide:\n"
            f"- 'hypothesis': a short statement of what this angle is trying to confirm\n"
            f"- 'queries': 2-3 objects with 'query' (search string) and 'category' "
            f"(a short label for the kind of source/evidence this query targets)\n\n"
            f"Return ONLY a JSON object: {{'candidates': [array of "
            f"{probe_count} objects with 'hypothesis' and 'queries']}}"
        )

    def _category_yield(self, subject_type: str, categories: set) -> float:
        """Average global source_category pheromone across `categories`,
        defaulting to 1.0 (neutral) for any category with no prior history
        -- an angle never seen before is neither penalized nor favored on
        its first run."""
        if not categories or not self._steering_index:
            return 1.0
        weights = []
        for category in categories:
            entry = self._steering_index.get_by_fingerprint(
                SteeringIndex.GLOBAL_SCOPE, "source_category",
                fingerprint=f"{subject_type}:{category}",
            )
            weights.append(entry.pheromone if entry else 1.0)
        return sum(weights) / len(weights)

    def _run_burst_search(
        self, investigation_id: str, config: InvestigationConfig
    ) -> Optional[InvestigationPlan]:
        """Probe-free divergent seeding: ask the thinker tier for
        burst_search.probe_count candidate research angles, score each by
        (distinct query categories) x (steering index's learned yield for
        those categories), and seed the investigation's plan with ONLY the
        winner. The other candidates are discarded without ever being
        dispatched -- this costs exactly one LLM call and zero extra
        fetches beyond the normal round-1 base sweep.

        Returns None (the caller falls back to _load_or_init_plan's normal
        fresh-plan path) when burst_search is disabled, no LLM is
        configured, or the response can't be parsed into any usable
        candidate.
        """
        if not self._config.burst_search.enabled or self._llm_client is None:
            return None

        probe_count = max(1, self._config.burst_search.probe_count)
        model, temperature = self._resolve_tier("thinker")
        prompt = self._build_burst_search_prompt(investigation_id, config, probe_count)
        try:
            response = self._llm_client.ask_json(
                prompt,
                system_prompt="You output strictly valid JSON.",
                model=model,
                temperature=temperature,
            )
        except LLMClientError as e:
            logger.error("Burst search LLM call failed: %s", e)
            return None

        if self._token_budget:
            tokens_used = self._llm_client.estimate_tokens(prompt) + 200
            self._token_budget.add_used_tokens(tokens_used)

        candidates = []
        for c in (response.get("candidates") or [])[:probe_count]:
            if not isinstance(c, dict):
                continue
            hypothesis = str(c.get("hypothesis", "")).strip()
            queries = []
            for q in (c.get("queries") or []):
                if not isinstance(q, dict):
                    continue
                query_text = str(q.get("query", "")).strip()
                if not query_text:
                    continue
                queries.append(QueuedQuery(
                    query=query_text,
                    category=str(q.get("category") or config.category),
                    reason=QueryReason.BURST_SEED,
                    priority=0.7,
                ))
            if hypothesis and queries:
                candidates.append((hypothesis, queries))

        if not candidates:
            return None

        def score(queries: list) -> float:
            categories = {q.category for q in queries}
            return len(categories) * self._category_yield(config.category, categories)

        winner_hypothesis, winner_queries = max(candidates, key=lambda c: score(c[1]))

        plan = InvestigationPlan(
            investigation_id=investigation_id,
            subject_profile={"target": config.target, "category": config.category},
            state=PlanState.EXPLORING,
        )
        plan.new_hypothesis(winner_hypothesis, current_round=1)
        plan.enqueue(winner_queries)

        # Mark the winning categories as "actually chosen this run" in the
        # global source_category ledger, so a later reinforce() call at
        # investigation completion (see _finalize_investigation) has a row
        # to update, and so a FUTURE investigation's burst search sees this
        # angle existed even before it has been reinforced by real yield.
        if self._steering_index:
            for category in {q.category for q in winner_queries}:
                self._steering_index.add(
                    SteeringIndex.GLOBAL_SCOPE, "source_category",
                    fingerprint=f"{config.category}:{category}",
                    payload=category,
                )
        return plan

    def _replan(
        self,
        plan: InvestigationPlan,
        findings_for_llm: list,
        config: InvestigationConfig,
        current_round: int,
    ) -> InvestigationPlan:
        """Ask the thinker tier to re-derive `plan`: apply its response via
        plan_object.replan() and return the result.

        Only called when plan.needs_replan() -- see _run_adaptive_loop.
        On any LLM/parse failure, returns `plan` still QUEUE_EXHAUSTED so
        the adaptive loop's stall detection (QUEUE_EXHAUSTED + zero
        queries dispatched this round) stops the investigation instead of
        retrying a broken LLM every round forever.
        """
        if self._llm_client is None:
            plan.state = PlanState.QUEUE_EXHAUSTED
            return plan

        model, temperature = self._resolve_tier("thinker")
        try:
            prompt = self._build_replan_prompt(plan, findings_for_llm, config, current_round)
            response = self._llm_client.ask_json(
                prompt,
                system_prompt="You output strictly valid JSON.",
                model=model,
                temperature=temperature,
            )

            if self._token_budget:
                tokens_used = self._llm_client.estimate_tokens(prompt) + 200
                self._token_budget.add_used_tokens(tokens_used)

            subject_profile = response.get("subject_profile", {})
            if not isinstance(subject_profile, dict):
                subject_profile = plan.subject_profile

            new_hypotheses = [
                Hypothesis(
                    id=uuid.uuid4().hex[:12],
                    statement=statement,
                    created_round=current_round,
                )
                for statement in (response.get("hypotheses") or [])
                if isinstance(statement, str) and statement.strip()
            ]

            new_queries = []
            for q in (response.get("queries") or []):
                if not isinstance(q, dict):
                    continue
                query_text = str(q.get("query", "")).strip()
                if not query_text:
                    continue
                try:
                    reason = QueryReason(q.get("reason"))
                except ValueError:
                    reason = QueryReason.FILL_GAP
                try:
                    priority = float(q.get("priority", 0.5))
                except (TypeError, ValueError):
                    priority = 0.5
                new_queries.append(QueuedQuery(
                    query=query_text,
                    category=str(q.get("category") or config.category),
                    reason=reason,
                    priority=max(0.0, min(1.0, priority)),
                ))

            if not response.get("continue_research", True):
                # LLM says stop even though it may have proposed queries --
                # respect it by not enqueuing anything.
                new_queries = []

            return plan_object.replan(plan, subject_profile, new_hypotheses, new_queries)

        except LLMClientError as e:
            logger.error("Failed to get replan: %s", e)
            plan.state = PlanState.QUEUE_EXHAUSTED
            return plan

    def _run_doubt_search(
        self,
        investigation_id: str,
        plan: InvestigationPlan,
        scored_new: list,
        findings_count: int,
        current_round: int,
        config: InvestigationConfig,
    ) -> None:
        """Extract claims from this round's new findings, detect
        contradictions against prior claims, and -- budget permitting --
        enqueue a bounded, programmatic (LLM-free) verification query for
        the highest-value unresolved claim.

        Mutates `plan` (may enqueue one query) and persists claim state via
        self._outcome_memory. No-op without an OutcomeMemory configured or
        when config.doubt_search.enabled is False.
        """
        if not self._outcome_memory or not self._config.doubt_search.enabled:
            return

        round_findings = [
            Finding(url=sf.url, name=sf.name, status="Active/Accessible",
                    title=sf.title, snippet=sf.snippet, category=sf.category)
            for sf in scored_new if not sf.is_noise
        ]
        if not round_findings:
            return

        existing_claims = self._outcome_memory.get_claims(investigation_id)
        scan_result = ContradictionDetector.scan(
            round_findings, existing_claims, investigation_id, current_round
        )
        for claim in scan_result.new_claims:
            self._outcome_memory.save_claim(claim)

        claims_by_id = {c.claim_id: c for c in existing_claims}
        for claim_id in set(scan_result.corroborated_claim_ids):
            self._outcome_memory.save_claim(claims_by_id[claim_id])

        for flag in scan_result.contradictions:
            claim = claims_by_id.get(flag.claim_id) or self._outcome_memory.get_claim(flag.claim_id)
            if claim is not None and claim.status not in (ClaimStatus.FLAGGED, ClaimStatus.UNRESOLVED):
                # A previously-corroborated or in-verification claim now
                # conflicts with a fresh source -- reopen it as FLAGGED so
                # pick_target() reconsiders it. UNRESOLVED is terminal.
                claim.status = ClaimStatus.FLAGGED
                self._outcome_memory.save_claim(claim)

        all_claims = self._outcome_memory.get_claims(investigation_id)
        doubt_budget = DoubtBudget(
            max_free_attempts=self._config.doubt_search.max_free_attempts,
            max_total_attempts=self._config.doubt_search.max_total_attempts,
        )
        for claim in all_claims:
            for _ in range(claim.verify_attempts):
                doubt_budget.record_attempt()

        target_claim = DoubtBudget.pick_target(all_claims)
        if target_claim is None:
            return

        if target_claim.verify_attempts >= doubt_budget.max_total_attempts:
            # Exhausted its budget without ever resolving -- terminal,
            # never retried again (surfaced to the report as-is).
            target_claim.status = ClaimStatus.UNRESOLVED
            target_claim.resolved_round = current_round
            self._outcome_memory.save_claim(target_claim)
            return

        efficiency = findings_count / max(current_round, 1)
        if not doubt_budget.can_spend(efficiency):
            return

        verify_query = QueuedQuery(
            query=(
                f'"{config.target}" {target_claim.predicate.replace("_", " ")} '
                f'"{target_claim.value}"'
            ),
            category=config.category,
            reason=QueryReason.DOUBT_VERIFICATION,
            priority=1.0,
        )
        if plan.enqueue([verify_query]):
            target_claim.verify_attempts += 1
            target_claim.status = ClaimStatus.VERIFYING
            self._outcome_memory.save_claim(target_claim)

    # Queries drained from the plan's queue per round when no replan is
    # needed -- caps per-round fetch volume similar to the old prompt's
    # "3-5 queries" instruction, now amortized across however many rounds
    # the queue lasts instead of re-asked of the LLM every round.
    QUERIES_PER_ROUND = 5

    def _run_adaptive_loop(
        self,
        state: InvestigationState,
        config: InvestigationConfig,
        all_findings_map: dict,
        plan: InvestigationPlan,
        format_params: dict,
        start_time: float,
        current_round: int,
        target_rounds: int,
    ) -> tuple:
        """Run rounds `current_round`..`target_rounds`, draining `plan`'s
        query queue and only spending a thinker-tier LLM call when
        `plan.needs_replan()` -- the deterministic-by-default loop that
        replaces the old "ask the LLM for a fresh plan every round" design
        (see plan_object.py's module docstring).

        Shared by run_investigation (after round 1's base sweep) and
        resume_investigation (from the checkpointed round), so both entry
        points run the exact same plan_object/steering_index wiring.

        Returns:
            (all_findings_map, plan, paused) -- paused=True means the
            caller must return `state` immediately (already transitioned
            to PAUSED with is_running cleared); paused=False means the
            caller should proceed to report generation.
        """
        investigation_id = state.investigation_id

        while current_round <= target_rounds:
            # Check stop signal FIRST (before pause check)
            if self._stop_event.is_set():
                self._event_bus.emit(Event(
                    type=EventType.STOP_REQUESTED,
                    investigation_id=investigation_id,
                    data={"round": current_round},
                ))
                state.findings = all_findings_map
                state.elapsed_seconds = time.time() - start_time
                if self._state_manager:
                    self._state_manager.save_checkpoint(state)
                break  # Exit loop -> fall through to report generation

            # Check pause flag
            if not self._pause_event.is_set():
                state.elapsed_seconds = time.time() - start_time
                state.current_round = current_round - 1
                state.findings = all_findings_map
                self._set_status(state, InvestigationStatus.PAUSED)
                if self._state_manager:
                    self._state_manager.save_checkpoint(state)
                self._event_bus.emit(Event(
                    type=EventType.INVESTIGATION_PAUSED,
                    investigation_id=investigation_id,
                    data={"last_round": current_round - 1},
                ))
                with self._lock:
                    self._is_running = False
                return all_findings_map, plan, True

            # Check token budget
            if self._token_budget and self._token_budget.should_stop_research():
                logger.info("Token budget exhausted, stopping research.")
                break

            self._event_bus.emit(Event(
                type=EventType.ROUND_STARTED,
                investigation_id=investigation_id,
                data={"round": current_round},
            ))

            # Thinker-tier replan only when the deterministic path ran out
            # of runway (queue empty, contradiction, or staleness floor) --
            # this is the load-bearing cost saving over the old design.
            if plan.needs_replan():
                findings_for_llm = list(all_findings_map.values())
                if self._token_budget:
                    available = self._token_budget.get_available_tokens()
                    findings_for_llm = self._token_budget.truncate_findings(
                        findings_for_llm, available
                    )
                plan = self._replan(plan, findings_for_llm, config, current_round)
                if self._plan_store:
                    self._plan_store.save(plan)
                self._event_bus.emit(Event(
                    type=EventType.PLAN_UPDATED,
                    investigation_id=investigation_id,
                    data={
                        "round": current_round,
                        "epoch": plan.epoch,
                        "state": plan.state.value,
                    },
                ))

            # Drain up to QUERIES_PER_ROUND queued queries deterministically
            # -- zero LLM cost, the common case for most rounds.
            queries_this_round = []
            for _ in range(min(self.QUERIES_PER_ROUND, len(plan.queued_queries))):
                q = plan.pop_next_query()
                if q is not None:
                    queries_this_round.append(q)

            state.round_plans.append({
                "round": current_round,
                "epoch": plan.epoch,
                "queries": [q.to_dict() for q in queries_this_round],
            })

            if queries_this_round and self._fetcher:
                query_sources = []
                dispatched_queries = []
                for q in queries_this_round:
                    # Fuzzy, weighted dedup against everything already
                    # tried this investigation -- replaces QueryRegistry's
                    # exact-match check. Checked and registered one query
                    # at a time (not batched) so two near-duplicates queued
                    # in the SAME round still catch each other.
                    blocking = (
                        self._steering_index.check(investigation_id, "query", q.query)
                        if self._steering_index else None
                    )
                    if blocking is not None:
                        logger.info("Skipping near-duplicate query: %s", q.query)
                        self._event_bus.emit(Event(
                            type=EventType.QUERY_SKIPPED,
                            investigation_id=investigation_id,
                            data={"query": q.query, "reason": "duplicate"},
                        ))
                        plan.mark_tried(q.query)
                        continue

                    url = f"https://www.google.com/search?q={urllib.parse.quote(q.query)}"
                    if url in all_findings_map:
                        continue

                    query_sources.append({"name": q.category or "Custom Query", "url": url})
                    dispatched_queries.append(q)
                    if self._steering_index:
                        self._steering_index.add(
                            investigation_id, "query",
                            fingerprint=normalize_query(q.query) or q.query.strip().lower(),
                            payload=q.query,
                        )

                if query_sources:
                    new_results = self._fetcher.fetch_batch(query_sources, format_params)
                    new_dicts = [{
                        "url": r.url,
                        "name": r.name,
                        "status": r.status,
                        "title": r.title or "",
                        "snippet": r.snippet or "",
                        "category": "Google Dorking Query",
                    } for r in new_results]

                    scored_new = self._quality_pipeline.filter_and_score(
                        new_dicts, config.target
                    )
                    for sf in scored_new:
                        if not sf.is_noise and sf.url not in all_findings_map:
                            all_findings_map[sf.url] = {
                                "url": sf.url,
                                "name": sf.name,
                                "status": "Active/Accessible",
                                "title": sf.title,
                                "snippet": sf.snippet,
                                "category": sf.category,
                                "relevance_score": sf.relevance_score,
                            }

                    for q in dispatched_queries:
                        plan.mark_tried(q.query)

                    # Claim extraction + contradiction detection + bounded
                    # doubt-search verification query, run BEFORE reconcile()
                    # so a freshly-enqueued verification query is already
                    # counted when reconcile() decides EXPLORING vs
                    # QUEUE_EXHAUSTED for the next round.
                    self._run_doubt_search(
                        investigation_id, plan, scored_new,
                        len(all_findings_map), current_round, config,
                    )

            # Deterministically advance the plan for the next round --
            # prunes now-hot-duplicate queued queries via the steering
            # index and decides whether EXPLORING can continue.
            if self._steering_index:
                plan = plan_object.reconcile(plan, self._steering_index)
                self._steering_index.decay(investigation_id, "query", rounds_elapsed=1.0)
                # RAG hints live in GLOBAL_SCOPE (see rag_ingest module
                # docstring) and clear_scope() no-ops there by design, so
                # decay is the only thing that ever retires a stale one --
                # without it an old OCR mistake would steer every future
                # investigation forever, only ever reinforced, never fading.
                # Half-life is deliberately much longer than the query
                # default (30 vs. 6 rounds): a long single run (up to 50
                # rounds, see _resolve_max_rounds) must not mute the
                # user's own uploaded context partway through, and since
                # these rows are global they'd otherwise carry decayed
                # state into the *next* investigation too.
                self._steering_index.decay(
                    SteeringIndex.GLOBAL_SCOPE, "hint", rounds_elapsed=1.0, half_life_rounds=30.0
                )
            elif not plan.queued_queries:
                # No steering index configured, so reconcile() (which
                # requires one to fuzzy-dedup the queue) can't run. Mirror
                # its "empty queue" outcome directly so needs_replan() and
                # the stall check below still behave correctly without one.
                plan.state = PlanState.QUEUE_EXHAUSTED
            if self._plan_store:
                self._plan_store.save(plan)

            # Update state and checkpoint
            state.current_round = current_round
            state.findings = all_findings_map
            state.elapsed_seconds = time.time() - start_time
            if self._state_manager:
                self._state_manager.save_checkpoint(state)

            self._event_bus.emit(Event(
                type=EventType.ROUND_COMPLETE,
                investigation_id=investigation_id,
                data={"round": current_round, "findings_count": len(all_findings_map)},
            ))

            # Stop once a round produced nothing to run AND nothing is
            # queued for next round either -- checked directly on the
            # queue rather than plan.state so a missing steering_index
            # (reconcile() didn't run) can't turn this into an infinite
            # empty-round loop.
            if not queries_this_round and not plan.queued_queries:
                logger.info(
                    "Plan exhausted with no queries at round %d; stopping.", current_round
                )
                break

            current_round += 1

        return all_findings_map, plan, False

    def _write_back_kb_lesson(
        self, state: InvestigationState, config: InvestigationConfig
    ) -> None:
        """Stage one KB lesson summarizing this completed investigation's
        dominant query strategy and measured round-efficiency.

        Runs on a background thread (see _finalize_investigation) so KB
        bookkeeping never adds latency to investigation completion. Staged
        lessons don't influence planning until record_reuse() promotes
        them via repeated, measurably-positive reuse -- this call only
        ever creates the STAGED starting point.
        """
        if not self._outcome_memory:
            return
        try:
            reason_counts: dict = {}
            for round_plan in state.round_plans:
                for q in round_plan.get("queries", []):
                    reason = q.get("reason")
                    if reason:
                        reason_counts[reason] = reason_counts.get(reason, 0) + 1
            if not reason_counts:
                return
            dominant_reason = max(reason_counts, key=reason_counts.get)

            rounds_run = max(state.current_round, 1)
            efficiency = len(state.findings) / rounds_run

            self._outcome_memory.stage_lesson(
                subject_type=config.category,
                action_taken=dominant_reason,
                outcome_quality=efficiency,
                round_cost=state.current_round,
                lesson=(
                    f"For {config.category} targets, a research strategy dominated by "
                    f"'{dominant_reason}' queries yielded {efficiency:.2f} findings/round "
                    f"over {state.current_round} rounds."
                ),
            )
        except Exception:
            logger.exception(
                "Failed to write back KB lesson for investigation %s", state.investigation_id
            )

    def _finalize_investigation(
        self,
        state: InvestigationState,
        all_findings_map: dict,
        config: InvestigationConfig,
        start_time: float,
        burst_categories: Optional[set] = None,
    ) -> InvestigationState:
        """Generate the synthesis report, transition to COMPLETED, persist,
        and emit investigation_complete. Shared tail for run_investigation
        and resume_investigation once the adaptive loop exits normally.

        burst_categories: source categories the winning burst-search
        candidate targeted (run_investigation only -- always None on the
        resume path). If set, their global source_category pheromone gets
        reinforced by this run's measured findings-per-round efficiency,
        closing the loop so future burst searches learn from it.
        """
        investigation_id = state.investigation_id

        report_output = None
        if self._llm_client and self._report_generator:
            try:
                synthesis_prompt = self._build_synthesis_prompt(all_findings_map, config)
                model, temperature = self._resolve_tier("default")
                report_response = self._llm_client.ask(
                    synthesis_prompt, model=model, temperature=temperature
                )
                report_md = report_response.content

                if self._token_budget:
                    self._token_budget.add_used_tokens(report_response.total_tokens)

                report_output = self._report_generator.generate(
                    state, report_md, enable_pdf=config.enable_pdf
                )
            except (LLMClientError, Exception) as e:
                logger.error("Report generation failed: %s", e)

        state.elapsed_seconds = time.time() - start_time
        state.findings = all_findings_map
        self._set_status(state, InvestigationStatus.COMPLETED)

        if self._state_manager:
            self._state_manager.save_checkpoint(state)

        if self._outcome_memory:
            threading.Thread(
                target=self._write_back_kb_lesson,
                args=(state, config),
                daemon=True,
            ).start()

        if burst_categories and self._steering_index:
            rounds_run = max(state.current_round, 1)
            yield_score = len(state.findings) / rounds_run
            for category in burst_categories:
                entry = self._steering_index.get_by_fingerprint(
                    SteeringIndex.GLOBAL_SCOPE, "source_category",
                    fingerprint=f"{config.category}:{category}",
                )
                if entry:
                    self._steering_index.reinforce(entry.id, yield_score)

        event_data = {}
        if report_output:
            event_data["markdown_path"] = report_output.markdown_path
            event_data["html_path"] = report_output.html_path
            if report_output.pdf_path:
                event_data["pdf_path"] = report_output.pdf_path
            if report_output.raw_data_path:
                event_data["raw_data_path"] = report_output.raw_data_path
            if report_output.organized_data_path:
                event_data["organized_data_path"] = report_output.organized_data_path

        self._event_bus.emit(Event(
            type=EventType.INVESTIGATION_COMPLETE,
            investigation_id=investigation_id,
            data=event_data,
        ))

        return state

    def _handle_investigation_exception(
        self,
        state: InvestigationState,
        exc: Exception,
        start_time: float,
        log_prefix: str,
    ) -> InvestigationState:
        """Transition to FAILED, persist partial findings, and emit
        investigation_failed. Shared exception handler for
        run_investigation and resume_investigation."""
        logger.error("%s: %s", log_prefix, exc)
        state.error = f"{type(exc).__name__}: {exc}"
        state.elapsed_seconds = time.time() - start_time

        try:
            if self._validate_transition(state.status, InvestigationStatus.FAILED):
                state.status = InvestigationStatus.FAILED
        except Exception:
            state.status = InvestigationStatus.FAILED

        if self._state_manager:
            try:
                self._state_manager.save_checkpoint(state)
            except Exception as save_err:
                logger.error("Failed to save partial state: %s", save_err)

        self._event_bus.emit(Event(
            type=EventType.INVESTIGATION_FAILED,
            investigation_id=state.investigation_id,
            data={
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
        ))

        return state

    def pause_investigation(self, investigation_id: str) -> None:
        """Gracefully pause a running investigation.

        Sets the pause flag which is checked in the research loop.
        Waits up to 10s for the current batch to complete, then
        persists state and emits the paused event.

        Args:
            investigation_id: The UUID of the investigation to pause.

        Raises:
            ValueError: If no matching running investigation exists.
        """
        if self._current_state is None:
            raise ValueError(f"No active investigation found: {investigation_id}")
        if self._current_state.investigation_id != investigation_id:
            raise ValueError(f"Investigation ID mismatch: {investigation_id}")
        if self._current_state.status != InvestigationStatus.RUNNING:
            raise ValueError(
                f"Cannot pause investigation in status: {self._current_state.status.value}"
            )

        # Signal the pause (clear the event so the loop sees it's not set)
        self._pause_event.clear()

        # Wait up to 10s for the investigation loop to detect the pause
        # The loop will handle saving state and emitting the event
        timeout = 10.0
        start = time.time()
        while self._is_running and (time.time() - start) < timeout:
            time.sleep(0.1)

        # If still running after 10s, force save state
        if self._is_running and self._current_state.status == InvestigationStatus.RUNNING:
            state = self._current_state
            if self._validate_transition(state.status, InvestigationStatus.PAUSED):
                state.status = InvestigationStatus.PAUSED
            if self._state_manager:
                self._state_manager.save_checkpoint(state)
            self._event_bus.emit(Event(
                type=EventType.INVESTIGATION_PAUSED,
                investigation_id=investigation_id,
                data={"last_round": state.current_round},
            ))
            with self._lock:
                self._is_running = False

    def stop_investigation(self, investigation_id: str) -> None:
        """Request graceful stop of the running investigation.

        Sets _stop_event so the research loop exits at the next round boundary.
        In-flight fetches in the current batch are allowed to complete.

        Args:
            investigation_id: UUID of the investigation to stop.

        Raises:
            ValueError: If no matching running investigation exists.
        """
        if self._current_state is None:
            raise ValueError(f"No active investigation found: {investigation_id}")
        if self._current_state.investigation_id != investigation_id:
            raise ValueError(f"Investigation ID mismatch: {investigation_id}")
        if self._current_state.status not in (
            InvestigationStatus.RUNNING, InvestigationStatus.PAUSED
        ):
            raise ValueError(
                f"Cannot stop investigation in status: {self._current_state.status.value}"
            )
        self._stop_event.set()
        self._event_bus.emit(Event(
            type=EventType.STOP_REQUESTED,
            investigation_id=investigation_id,
            data={"round": self._current_state.current_round},
        ))

    def resume_investigation(self, investigation_id: str) -> InvestigationState:
        """Resume a paused investigation from its last checkpoint.

        Loads the checkpoint from the state manager, verifies the status
        is PAUSED, sets RUNNING, emits resumed event, and continues
        the research loop from the next round.

        Args:
            investigation_id: The UUID of the investigation to resume.

        Returns:
            The final InvestigationState after completion.

        Raises:
            ValueError: If investigation not found or not in PAUSED status.
            RuntimeError: If another investigation is already running.
        """
        # Check if already running
        with self._lock:
            if self._is_running:
                raise RuntimeError("An investigation is already in progress.")

        # Load checkpoint
        if self._state_manager is None:
            raise ValueError("No state manager configured for resume.")

        state = self._state_manager.load_checkpoint(investigation_id)
        if state is None:
            raise ValueError(f"No checkpoint found for investigation: {investigation_id}")

        if state.status != InvestigationStatus.PAUSED:
            raise ValueError(
                f"Cannot resume investigation in status: {state.status.value}. "
                f"Only PAUSED investigations can be resumed."
            )

        # Set running
        with self._lock:
            self._is_running = True

        self._pause_event.set()  # Reset pause flag
        self._current_state = state
        start_time = time.time() - state.elapsed_seconds  # Account for prior elapsed

        try:
            # Transition PAUSED -> RUNNING
            self._set_status(state, InvestigationStatus.RUNNING)

            # Emit investigation_resumed
            self._event_bus.emit(Event(
                type=EventType.INVESTIGATION_RESUMED,
                investigation_id=investigation_id,
                data={"resume_from_round": state.current_round + 1},
            ))

            # Restore findings and plan, continue from next round
            all_findings_map = state.findings if state.findings else {}
            config = state.config
            format_params = self._parse_format_params(config.category, config.target)
            target_rounds = self._resolve_max_rounds(config.max_rounds)
            current_round = state.current_round + 1

            plan = self._load_or_init_plan(investigation_id, config)

            all_findings_map, plan, paused = self._run_adaptive_loop(
                state, config, all_findings_map, plan, format_params,
                start_time, current_round, target_rounds,
            )
            if paused:
                return state

            return self._finalize_investigation(state, all_findings_map, config, start_time)

        except Exception as e:
            return self._handle_investigation_exception(
                state, e, start_time, "Resumed investigation failed"
            )

        finally:
            with self._lock:
                self._is_running = False
