"""Investigation plan as a persistent, mutable object -- not a prompt.

Replaces the old "reconstruct the whole plan from scratch every round via a
one-shot JSON mega-prompt" design. A 7B-class model asked to reconstruct
classification + query invention + stop/continue judgment cold, every round,
from the raw findings list, is exactly the shape of task small local models
fail at (self-consistency across rounds, position-biased "pick a winner").

Instead, `InvestigationPlan` is state the engine mutates deterministically
(`reconcile()`) every round for near-zero cost, and only asks a thinker-tier
LLM to re-derive when `PlanState` says the deterministic path ran out of
runway (queue empty, a contradiction surfaced, or the plan has gone stale).
This is the load-bearing simplification: replanning becomes the exception,
not the per-round default.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from . import db, paths
from .models import QueryReason
from .steering_index import SteeringIndex

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS plan_objects (
    investigation_id TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (investigation_id, epoch)
)
"""

# How many rounds a plan may run on deterministic reconcile() alone before
# forcing a thinker-tier re-grounding call even if the queue isn't empty --
# guards against slowly drifting off the actual findings for a hard cap on
# per-round LLM cost.
DEFAULT_REPLAN_STALENESS_ROUNDS = 4


class HypothesisStatus(Enum):
    """Lifecycle of a single hypothesis the plan is tracking."""

    UNEXPLORED = "unexplored"
    FOLLOWING = "following"
    CONFIRMED = "confirmed"
    CONTRADICTED = "contradicted"


class PlanState(Enum):
    """Why the plan currently does/doesn't need a thinker-tier re-derivation.

    EXPLORING is the steady state: pop_next_query() drains the queue at
    zero LLM cost. Any other value is the *reason* the engine must pay for
    a thinker call before the next round can proceed.
    """

    EXPLORING = "exploring"
    QUEUE_EXHAUSTED = "queue_exhausted"
    CONTRADICTION_DETECTED = "contradiction_detected"
    EPOCH_STALE = "epoch_stale"


@dataclass
class Hypothesis:
    """A single claim about the subject the plan is actively tracking."""

    id: str
    statement: str
    status: HypothesisStatus = HypothesisStatus.UNEXPLORED
    supporting_finding_ids: list[str] = field(default_factory=list)
    created_round: int = 1

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "statement": self.statement,
            "status": self.status.value,
            "supporting_finding_ids": list(self.supporting_finding_ids),
            "created_round": self.created_round,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Hypothesis:
        return cls(
            id=data["id"],
            statement=data["statement"],
            status=HypothesisStatus(data.get("status", "unexplored")),
            supporting_finding_ids=list(data.get("supporting_finding_ids", [])),
            created_round=data.get("created_round", 1),
        )


@dataclass
class QueuedQuery:
    """One query the plan intends to run, with an auditable reason.

    `reason` is a closed enum (not free-text rationale) specifically so the
    dashboard timeline (EventType.PLAN_UPDATED) can render a trustworthy,
    diffable audit trail without spending an LLM call to summarize itself.
    """

    query: str
    category: str
    reason: QueryReason
    priority: float = 0.5
    source_hypothesis_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "category": self.category,
            "reason": self.reason.value,
            "priority": self.priority,
            "source_hypothesis_id": self.source_hypothesis_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> QueuedQuery:
        return cls(
            query=data["query"],
            category=data["category"],
            reason=QueryReason(data["reason"]),
            priority=data.get("priority", 0.5),
            source_hypothesis_id=data.get("source_hypothesis_id"),
        )


@dataclass
class InvestigationPlan:
    """The living plan for one investigation.

    `epoch` increments only when a thinker-tier call re-derives the plan --
    it is the checkpoint granularity for resume_investigation(), finer than
    the whole-investigation state: a paused run whose PlanState was still
    EXPLORING at pause time resumes without spending a thinker call at all.
    """

    investigation_id: str
    epoch: int = 0
    subject_profile: dict = field(default_factory=dict)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    tried_fingerprints: set[str] = field(default_factory=set)
    queued_queries: list[QueuedQuery] = field(default_factory=list)
    state: PlanState = PlanState.EXPLORING
    rounds_since_replan: int = 0
    yield_delta: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def needs_replan(self) -> bool:
        """True when the deterministic path has run out of runway and a
        thinker-tier call is required before the next round can proceed."""
        return self.state != PlanState.EXPLORING

    def pop_next_query(self) -> Optional[QueuedQuery]:
        """Remove and return the highest-priority queued query, or None if
        the queue is empty (queued_queries is kept priority-sorted by
        enqueue(), so this is a plain pop(0))."""
        if not self.queued_queries:
            return None
        return self.queued_queries.pop(0)

    def enqueue(self, queries: list[QueuedQuery]) -> int:
        """Add new queries, skipping any whose normalized text is already in
        tried_fingerprints. Returns the count actually added. Keeps the
        queue sorted highest-priority-first so pop_next_query() stays O(1).
        """
        added = 0
        for q in queries:
            fingerprint = q.query.strip().lower()
            if fingerprint in self.tried_fingerprints:
                continue
            self.queued_queries.append(q)
            added += 1
        self.queued_queries.sort(key=lambda q: q.priority, reverse=True)
        self.updated_at = time.time()
        return added

    def mark_tried(self, query_text: str) -> None:
        """Record a query as tried so future enqueue()/reconcile() calls
        never re-queue it, independent of the steering index."""
        self.tried_fingerprints.add(query_text.strip().lower())

    def get_hypothesis(self, hypothesis_id: str) -> Optional[Hypothesis]:
        return next((h for h in self.hypotheses if h.id == hypothesis_id), None)

    def new_hypothesis(
        self, statement: str, current_round: int = 1, hypothesis_id: Optional[str] = None
    ) -> Hypothesis:
        """Create, register, and return a new Hypothesis on this plan."""
        h = Hypothesis(
            id=hypothesis_id or uuid.uuid4().hex[:12],
            statement=statement,
            created_round=current_round,
        )
        self.hypotheses.append(h)
        return h

    def to_dict(self) -> dict:
        return {
            "investigation_id": self.investigation_id,
            "epoch": self.epoch,
            "subject_profile": self.subject_profile,
            "hypotheses": [h.to_dict() for h in self.hypotheses],
            "tried_fingerprints": sorted(self.tried_fingerprints),
            "queued_queries": [q.to_dict() for q in self.queued_queries],
            "state": self.state.value,
            "rounds_since_replan": self.rounds_since_replan,
            "yield_delta": self.yield_delta,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> InvestigationPlan:
        return cls(
            investigation_id=data["investigation_id"],
            epoch=data.get("epoch", 0),
            subject_profile=data.get("subject_profile", {}),
            hypotheses=[Hypothesis.from_dict(h) for h in data.get("hypotheses", [])],
            tried_fingerprints=set(data.get("tried_fingerprints", [])),
            queued_queries=[QueuedQuery.from_dict(q) for q in data.get("queued_queries", [])],
            state=PlanState(data.get("state", "exploring")),
            rounds_since_replan=data.get("rounds_since_replan", 0),
            yield_delta=data.get("yield_delta", 0.0),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
        )


def reconcile(
    plan: InvestigationPlan,
    steering_index: SteeringIndex,
    contradicted_hypothesis_ids: Optional[list[str]] = None,
    replan_staleness_rounds: int = DEFAULT_REPLAN_STALENESS_ROUNDS,
) -> InvestigationPlan:
    """Deterministically advance the plan by one round. No LLM calls.

    Call this once per round, after that round's findings have been scored
    and outcome_memory's ContradictionDetector has run. It:

    1. Drops queued queries that the steering index now considers a hot
       near-duplicate of something already tried this investigation (via
       SteeringIndex.check on entry_type='query') -- this is what replaces
       QueryRegistry's exact-match dedup with fuzzy, weighted dedup.
    2. Demotes any hypothesis named in `contradicted_hypothesis_ids` to
       CONTRADICTED and flips the plan into CONTRADICTION_DETECTED, since a
       demoted hypothesis's still-queued follow-up queries are no longer
       trustworthy to run blind.
    3. Sets QUEUE_EXHAUSTED if no queued queries survive step 1 and no
       contradiction fired.
    4. Sets EPOCH_STALE if the plan has run `replan_staleness_rounds` or
       more rounds since its last thinker-tier replan, even with a
       non-empty queue -- a periodic re-grounding floor.
    5. Otherwise leaves the plan in EXPLORING (or restores it there), so the
       engine can keep draining the queue at zero LLM cost.

    Mutates and returns `plan`.
    """
    contradicted_ids = set(contradicted_hypothesis_ids or [])

    surviving: list[QueuedQuery] = []
    for q in plan.queued_queries:
        if steering_index.check(plan.investigation_id, "query", q.query) is not None:
            continue
        surviving.append(q)
    plan.queued_queries = surviving

    contradiction_fired = False
    for hypothesis_id in contradicted_ids:
        h = plan.get_hypothesis(hypothesis_id)
        if h is not None and h.status != HypothesisStatus.CONTRADICTED:
            h.status = HypothesisStatus.CONTRADICTED
            contradiction_fired = True

    plan.rounds_since_replan += 1

    if contradiction_fired:
        plan.state = PlanState.CONTRADICTION_DETECTED
    elif not plan.queued_queries:
        plan.state = PlanState.QUEUE_EXHAUSTED
    elif plan.rounds_since_replan >= replan_staleness_rounds:
        plan.state = PlanState.EPOCH_STALE
    else:
        plan.state = PlanState.EXPLORING

    plan.updated_at = time.time()
    return plan


def replan(
    plan: InvestigationPlan,
    subject_profile: dict,
    new_hypotheses: list[Hypothesis],
    new_queries: list[QueuedQuery],
) -> InvestigationPlan:
    """Apply a thinker-tier replan result to the plan: bumps the epoch,
    resets the staleness counter, merges in fresh hypotheses/queries, and
    returns the plan to EXPLORING. This is the only place `plan.epoch`
    changes -- epoch is a count of thinker calls, not of rounds.
    """
    plan.epoch += 1
    plan.rounds_since_replan = 0
    plan.subject_profile = subject_profile
    plan.hypotheses.extend(new_hypotheses)
    plan.enqueue(new_queries)
    plan.state = PlanState.EXPLORING
    plan.updated_at = time.time()
    return plan


class PlanStore:
    """SQLite-backed persistence for InvestigationPlan, keyed by
    (investigation_id, epoch). Owns its own table via the shared db module,
    mirroring SteeringIndex -- StateManager stays responsible for whole-
    investigation state only, not plan internals.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path if db_path is not None else str(paths.db_path())
        conn = db.get_connection(self._db_path)
        with db.write_lock(self._db_path):
            conn.execute(_CREATE_TABLE_SQL)
            conn.commit()

    @property
    def _conn(self):
        return db.get_connection(self._db_path)

    def save(self, plan: InvestigationPlan) -> None:
        """Persist `plan` at its current epoch (upsert)."""
        conn = self._conn
        with db.write_lock(self._db_path):
            conn.execute(
                "INSERT INTO plan_objects (investigation_id, epoch, plan_json, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(investigation_id, epoch) DO UPDATE SET "
                "plan_json=excluded.plan_json",
                (plan.investigation_id, plan.epoch, json.dumps(plan.to_dict()), time.time()),
            )
            conn.commit()

    def load_latest(self, investigation_id: str) -> Optional[InvestigationPlan]:
        """Load the highest-epoch plan for an investigation, or None if no
        plan has been saved yet (a fresh investigation starts without one)."""
        row = self._conn.execute(
            "SELECT plan_json FROM plan_objects WHERE investigation_id = ? "
            "ORDER BY epoch DESC LIMIT 1",
            (investigation_id,),
        ).fetchone()
        if row is None:
            return None
        return InvestigationPlan.from_dict(json.loads(row[0]))

    def clear(self, investigation_id: str) -> None:
        """Remove all saved epochs for an investigation (call on fresh
        investigation start, mirroring SteeringIndex.clear_scope())."""
        conn = self._conn
        with db.write_lock(self._db_path):
            conn.execute("DELETE FROM plan_objects WHERE investigation_id = ?", (investigation_id,))
            conn.commit()
