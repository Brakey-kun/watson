"""Shared data models and enums for the Watson OSINT engine."""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class InvestigationStatus(Enum):
    """Investigation lifecycle states."""

    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class BackendConfig:
    """Configuration for a single LLM backend."""

    endpoint: str = "http://127.0.0.1:1234/v1"
    api_key: str = "lm-studio"
    model: str = "gemma-4-12b-it-uncensored"
    temperature: float = 0.7
    last_tested: Optional[str] = None  # ISO timestamp or None


@dataclass
class ValidationError:
    """Describes a validation failure for a backend config field."""

    backend_name: str
    field_name: str
    issue: str  # "missing", "wrong_type", "out_of_range"
    detail: str


@dataclass
class LLMConfig:
    """Configuration for the active LLM connection."""

    backend: str = "lm_studio"
    host: str = "127.0.0.1"
    port: int = 1234
    model: str = "gemma-4-12b-it-uncensored"
    temperature: float = 0.7
    max_context_tokens: int = 32768
    max_retries: int = 3


@dataclass
class FetcherConfig:
    """Configuration for the concurrent HTTP fetcher."""

    max_workers: int = 20
    timeout_seconds: int = 10
    max_retries: int = 2
    rate_limit_per_second: float = 5.0


@dataclass
class SearchConfig:
    """Configuration for multi-engine search."""

    engines: List[str] = field(default_factory=lambda: ["google", "bing", "duckduckgo"])
    rate_limit_per_engine: float = 2.0
    jitter_min: float = 0.5
    jitter_max: float = 2.0


@dataclass
class QualityConfig:
    """Configuration for the quality filtering pipeline."""

    min_relevance_score: float = 0.2
    enable_content_dedup: bool = True
    noise_patterns: List[str] = field(default_factory=list)


@dataclass
class AppConfig:
    """Top-level application configuration combining all sub-configs."""

    llm: LLMConfig = field(default_factory=LLMConfig)
    backends: dict[str, BackendConfig] = field(default_factory=dict)
    fetcher: FetcherConfig = field(default_factory=FetcherConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    tiers: "ModelTiers" = field(default_factory=lambda: ModelTiers())
    burst_search: "BurstSearchConfig" = field(default_factory=lambda: BurstSearchConfig())
    doubt_search: "DoubtSearchConfig" = field(default_factory=lambda: DoubtSearchConfig())
    setup_completed: bool = False


@dataclass
class TierModelConfig:
    """Model assignment for one steering role (thinker/default/small).

    An empty `model` means "fall back to the active backend's configured
    model" -- tiers are opt-in overrides, not a second required setup step.
    """

    model: str = ""
    temperature: Optional[float] = None


@dataclass
class ModelTiers:
    """Model assignments for the three roles the research loop dispatches to.

    - thinker: planning, idea aggregation, burst-search scoring, plan re-derivation.
    - default: the main bounded-diff reconciliation and extraction/execution path.
    - small: cheap agentic burst-search probes and doubt-search verification.
    """

    thinker: TierModelConfig = field(default_factory=TierModelConfig)
    default: TierModelConfig = field(default_factory=TierModelConfig)
    small: TierModelConfig = field(default_factory=TierModelConfig)


@dataclass
class BurstSearchConfig:
    """Configuration for the optional divergent first-pass burst search."""

    enabled: bool = False
    probe_count: int = 3


@dataclass
class DoubtSearchConfig:
    """Configuration for bounded mid-investigation doubt/verification search."""

    enabled: bool = True
    max_free_attempts: int = 1
    max_total_attempts: int = 3


class QueryReason(Enum):
    """Closed set of justifications a query can be queued under.

    Cheap for a small local model to emit reliably (classification, not
    generation) and gives the dashboard a glanceable audit trail instead of
    free-text rationale that can't be diffed or trusted.
    """

    FOLLOW_ENTITY = "follow_entity"
    FILL_GAP = "fill_gap"
    CONTRADICTION_CHECK = "contradiction_check"
    SOURCE_CATEGORY_UNEXPLORED = "source_category_unexplored"
    BURST_SEED = "burst_seed"
    DOUBT_VERIFICATION = "doubt_verification"


class ClaimStatus(Enum):
    """Lifecycle of a doubt-search claim: FLAGGED -> VERIFYING -> CONFIRMED|UNRESOLVED.

    UNRESOLVED is terminal -- an irreducibly uncertain claim never retries
    itself, it becomes a labeled artifact surfaced in the final report.
    """

    FLAGGED = "flagged"
    VERIFYING = "verifying"
    CONFIRMED = "confirmed"
    UNRESOLVED = "unresolved"

@dataclass
class InvestigationConfig:
    """Configuration for a single investigation run."""

    target: str
    category: str
    max_rounds: int | str = "Auto"
    urgency: str = "normal"
    lm_studio_url: Optional[str] = None
    enable_multi_engine: bool = True
    enable_pdf: bool = False


@dataclass
class FetchResult:
    """Result from fetching a single URL."""

    name: str
    url: str
    status: str
    title: Optional[str] = None
    snippet: Optional[str] = None
    status_code: Optional[int] = None
    response_time_ms: Optional[float] = None
    category: str = "Direct Lookup"
    error: Optional[str] = None


@dataclass
class Finding:
    """A single OSINT finding from a source URL."""

    url: str
    name: str
    status: str
    title: Optional[str] = None
    snippet: Optional[str] = None
    category: str = "Direct Lookup"
    relevance_score: float = 0.0
    content_hash: str = ""
    fetched_at: str = ""
    response_time_ms: float = 0.0
    round_discovered: int = 1
    confidence: float = 0.0
    unverified: bool = False
    claim_id: Optional[str] = None


@dataclass
class ScoredFinding:
    """A finding scored by the quality pipeline."""

    url: str
    name: str
    title: str
    snippet: str
    relevance_score: float
    is_noise: bool
    category: str
    content_hash: str


@dataclass
class LLMResponse:
    """Response from an LLM request."""

    content: str
    tokens_used: int
    model: str
    success: bool
    error: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class TokenBudget:
    """Token budget tracking for context window management."""

    max_context_tokens: int = 8192
    reserved_for_system: int = 500
    reserved_for_response: int = 2000
    used_tokens: int = 0


@dataclass
class InvestigationState:
    """Full state of a running or completed investigation."""

    investigation_id: str
    config: InvestigationConfig
    status: InvestigationStatus
    current_round: int = 0
    findings: dict = field(default_factory=dict)
    round_plans: list = field(default_factory=list)
    elapsed_seconds: float = 0.0
    error: Optional[str] = None
