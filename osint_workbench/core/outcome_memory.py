"""Outcome-scored memory: contradiction detection over claims, and a
staging/promotion pipeline that turns "what worked" into reusable knowledge.

Two concerns share this module because they're the same shape of problem --
turning noisy, per-round signal into a small set of things worth trusting:

- `ContradictionDetector` extracts lightweight (subject, predicate, value)
  claim candidates from findings via cheap regex heuristics (no LLM, no
  NLP pipeline) and diffs them against known claims, so a round that
  surfaces "born 1990" against an existing "born 1985" claim gets flagged
  before it silently pollutes the report.
- `OutcomeMemory` persists claims and a `kb_lessons` table of staged
  approach/outcome pairs. A lesson only starts influencing planning once
  its measured cumulative_delta (round-efficiency improvement across reuse)
  crosses a promotion threshold -- unvalidated advice never reaches the plan.

Both are backed by the shared sqlite3 connection in `db.py`, matching
SteeringIndex and PlanStore's self-contained table ownership.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional

from . import db, paths
from .models import ClaimStatus, Finding

_CREATE_CLAIMS_SQL = """
CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    investigation_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value TEXT NOT NULL,
    source_urls TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('flagged', 'verifying', 'confirmed', 'unresolved')),
    confidence REAL NOT NULL DEFAULT 0.0,
    verify_attempts INTEGER NOT NULL DEFAULT 0,
    created_round INTEGER NOT NULL,
    resolved_round INTEGER
)
"""
_CREATE_CLAIMS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_claims_investigation "
    "ON claims(investigation_id, subject, predicate)"
)

_CREATE_KB_LESSONS_SQL = """
CREATE TABLE IF NOT EXISTS kb_lessons (
    id TEXT PRIMARY KEY,
    subject_type TEXT NOT NULL,
    action_taken TEXT NOT NULL,
    outcome_quality REAL NOT NULL,
    round_cost INTEGER NOT NULL,
    lesson TEXT NOT NULL,
    stage TEXT NOT NULL CHECK(stage IN ('staged', 'active', 'demoted')) DEFAULT 'staged',
    uses_since_promotion INTEGER NOT NULL DEFAULT 0,
    cumulative_delta REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL,
    promoted_at REAL
)
"""

# A staged lesson is promoted to 'active' (allowed to influence future
# planning) once it has been reused at least this many times AND its
# cumulative round-efficiency delta crosses this threshold -- both gates
# must pass so a single lucky reuse can't promote unvalidated advice.
PROMOTION_MIN_USES = 3
PROMOTION_MIN_CUMULATIVE_DELTA = 1.5

# A promoted lesson whose cumulative_delta falls back below zero (it turned
# out not to generalize) is demoted rather than left active indefinitely.
DEMOTION_CUMULATIVE_DELTA = -1.0


# ---------------------------------------------------------------------------
# Contradiction detection
# ---------------------------------------------------------------------------

# "Key: Value" / "Key - Value" style attribute segments commonly seen in
# OSINT source titles/snippets (profile cards, whois records, people-search
# sites). Matched against one delimiter-split segment at a time (see
# extract_candidates), anchored to the whole segment, so a leading word from
# an unrelated title/sentence can't get swallowed into the key or a later
# "Key: Value" pair on the same line can't get swallowed into the value.
_ATTR_LINE_RE = re.compile(
    r"^(?P<key>[A-Za-z][A-Za-z ]{1,20}?)\s*[:\-]\s*(?P<value>.+)$"
)
# "Key is/was Value" prose pattern, same whole-segment anchoring.
_ATTR_PROSE_RE = re.compile(
    r"^(?P<key>[A-Za-z][A-Za-z ]{1,20}?)\s+(?:is|was)\s+(?P<value>.+)$"
)
# Delimiters that separate distinct attribute segments within one title or
# snippet string (profile-card text is usually comma/pipe/semicolon/newline
# separated key:value pairs, e.g. "Age: 34, Location: New York"). A bare
# comma is NOT always a segment boundary -- date-ish values routinely
# contain one ("Born: January 5, 1990"), so only split on a comma that is
# actually followed by what looks like the start of the *next* key
# (letters then a colon/dash/is/was), never on every comma in the text.
_SEGMENT_SPLIT_RE = re.compile(
    r"[;|\n]|,(?=\s*[A-Za-z][A-Za-z ]{0,20}?\s*(?::|-|\bis\b|\bwas\b))"
)

# Predicates worth tracking -- a narrow allowlist keeps this a precision
# tool (a handful of trustworthy, comparable attributes) rather than a noisy
# catch-all that flags every incidental "color: dark" style match.
_TRACKED_PREDICATES = {
    "age", "born", "birthday", "birthdate", "dob", "location", "city",
    "state", "country", "employer", "company", "occupation", "email",
    "phone", "school", "university",
}

# The subject of every extracted claim is the investigation's own target --
# cross-entity extraction (claims about *other* people mentioned in a
# finding) is out of scope for this heuristic pass.
TARGET_SUBJECT = "target"


def _normalize_predicate(key: str) -> Optional[str]:
    norm = re.sub(r"\s+", "_", key.strip().lower())
    if norm in _TRACKED_PREDICATES:
        return norm
    return None


def _normalize_value(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower()).rstrip(".,;")


# Predicates whose values are dates, so a naive string comparison would
# manufacture false contradictions between two correct-but-differently-
# formatted sources ("January 5, 1990" vs "1990-01-05" is the normal case
# for OSINT DOB data, not an edge case).
DATE_PREDICATES = {"born", "birthday", "birthdate", "dob"}

_DATE_FORMATS = (
    "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
    "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%d %B %Y", "%d %b %Y",
)
_YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")


def _try_parse_date(value: str) -> Optional[date]:
    cleaned = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()  # noqa: DTZ007 -- date-only, tz is meaningless for a birthdate
        except ValueError:
            continue
    return None


def _extract_year(value: str) -> Optional[int]:
    m = _YEAR_RE.search(value)
    return int(m.group(1)) if m else None


def _values_conflict(a: str, b: str, predicate: Optional[str] = None) -> bool:
    """True when two normalized values are meaningfully different rather
    than just differently formatted. Treats one being a substring of the
    other as compatible ("new york" vs "new york, ny"), so formatting
    variance doesn't manufacture false contradictions.

    For DATE_PREDICATES, tries actual date parsing first (several common
    formats) and compares date objects rather than strings; if only a
    4-digit year is extractable from both sides, compares years instead.
    Falls back to the generic string logic when parsing fails on either
    side, since a heuristic without a real date parser is inherently
    imperfect and should stay conservative (flag rather than silently
    accept) when it can't confidently normalize both values.
    """
    if predicate in DATE_PREDICATES:
        da, db_ = _try_parse_date(a), _try_parse_date(b)
        if da is not None and db_ is not None:
            return da != db_
        ya, yb = _extract_year(a), _extract_year(b)
        if ya is not None and yb is not None:
            return ya != yb
    na, nb = _normalize_value(a), _normalize_value(b)
    return na != nb and na not in nb and nb not in na


def _extract_from_segment(segment: str) -> Optional[tuple[str, str]]:
    """Match one delimiter-split segment against the key/value patterns.
    Returns (predicate, value) if it's a tracked attribute, else None."""
    segment = segment.strip()
    if not segment:
        return None
    for pattern in (_ATTR_LINE_RE, _ATTR_PROSE_RE):
        m = pattern.match(segment)
        if not m:
            continue
        predicate = _normalize_predicate(m.group("key"))
        if predicate is None:
            continue
        value = m.group("value").strip()
        if value:
            return predicate, value
    return None


def extract_candidates(findings: list[Finding]) -> list[tuple[str, str, str, str]]:
    """Extract (subject, predicate, value, source_url) candidate claim
    tuples from a batch of findings using cheap regex heuristics.

    Deliberately not NLP: this trades recall for zero LLM cost and full
    determinism. Title and snippet are each split into comma/semicolon/
    pipe/newline-delimited segments and matched independently -- keeps
    "Age: 34, Location: New York" from bleeding into one giant blob, and
    keeps an unrelated title ("Profile") from being swallowed into the
    next field's key. Only recognizes a narrow allowlist of predicates
    (_TRACKED_PREDICATES), and attributes every extracted claim to the
    investigation's own target.
    """
    candidates: list[tuple[str, str, str, str]] = []
    for finding in findings:
        for text in (finding.title, finding.snippet):
            if not text:
                continue
            for segment in _SEGMENT_SPLIT_RE.split(text):
                extracted = _extract_from_segment(segment)
                if extracted is None:
                    continue
                predicate, value = extracted
                candidates.append((TARGET_SUBJECT, predicate, value, finding.url))
    return candidates


@dataclass
class Claim:
    """A single tracked (subject, predicate, value) assertion, corroborated
    or contradicted across one or more source URLs."""

    claim_id: str
    investigation_id: str
    subject: str
    predicate: str
    value: str
    source_urls: list[str]
    status: ClaimStatus = ClaimStatus.FLAGGED
    confidence: float = 0.3
    verify_attempts: int = 0
    created_round: int = 1
    resolved_round: Optional[int] = None


@dataclass
class ContradictionFlag:
    """A newly-detected conflict between an existing claim and a fresh
    candidate value for the same (subject, predicate)."""

    claim_id: str
    subject: str
    predicate: str
    old_value: str
    new_value: str
    new_source_url: str


@dataclass
class ScanResult:
    """Output of one ContradictionDetector.scan() call."""

    new_claims: list[Claim] = field(default_factory=list)
    corroborated_claim_ids: list[str] = field(default_factory=list)
    contradictions: list[ContradictionFlag] = field(default_factory=list)


# A claim is promoted from FLAGGED to CONFIRMED once it has this many
# independent corroborating source URLs with no conflicting value seen.
CORROBORATION_THRESHOLD = 2


class ContradictionDetector:
    """Pure, stateless diff logic between extracted candidates and known
    claims. Holds no I/O -- callers own persistence via OutcomeMemory."""

    @staticmethod
    def scan(
        findings: list[Finding],
        existing_claims: list[Claim],
        investigation_id: str,
        current_round: int,
    ) -> ScanResult:
        """Extract candidates from `findings` and diff them against
        `existing_claims` for the same investigation.

        For each (subject, predicate) candidate:
          - No existing claim -> queued as a new FLAGGED Claim.
          - Existing claim, compatible value, new source URL -> corroborated
            (source_urls grows; promoted to CONFIRMED at CORROBORATION_THRESHOLD).
          - Existing claim, conflicting value -> a ContradictionFlag against
            that claim_id (the existing claim's status/value is NOT mutated
            here; the caller decides how to resolve it, e.g. via doubt search).

        Returns a ScanResult the caller applies via OutcomeMemory.save_claim()
        for new_claims and corroborated claims; contradictions are surfaced
        to plan_object.reconcile() as contradicted_hypothesis_ids input.
        """
        result = ScanResult()
        existing_by_key: dict[tuple[str, str], list[Claim]] = {}
        for c in existing_claims:
            existing_by_key.setdefault((c.subject, c.predicate), []).append(c)

        for subject, predicate, value, source_url in extract_candidates(findings):
            key = (subject, predicate)
            matches = existing_by_key.get(key, [])
            if not matches:
                claim = Claim(
                    claim_id=uuid.uuid4().hex[:12],
                    investigation_id=investigation_id,
                    subject=subject,
                    predicate=predicate,
                    value=value,
                    source_urls=[source_url],
                    created_round=current_round,
                )
                result.new_claims.append(claim)
                existing_by_key.setdefault(key, []).append(claim)
                continue

            conflict_found = False
            for claim in matches:
                if _values_conflict(claim.value, value, predicate):
                    result.contradictions.append(
                        ContradictionFlag(
                            claim_id=claim.claim_id,
                            subject=subject,
                            predicate=predicate,
                            old_value=claim.value,
                            new_value=value,
                            new_source_url=source_url,
                        )
                    )
                    conflict_found = True
                elif source_url not in claim.source_urls:
                    claim.source_urls.append(source_url)
                    if (
                        len(claim.source_urls) >= CORROBORATION_THRESHOLD
                        and claim.status in (ClaimStatus.FLAGGED, ClaimStatus.VERIFYING)
                    ):
                        claim.status = ClaimStatus.CONFIRMED
                        claim.resolved_round = current_round
                    claim.confidence = min(1.0, claim.confidence + 0.25)
                    result.corroborated_claim_ids.append(claim.claim_id)
            if not conflict_found and not matches:
                pass  # unreachable: matches is non-empty in this branch

        return result


# ---------------------------------------------------------------------------
# Doubt budget
# ---------------------------------------------------------------------------


class DoubtBudget:
    """Enforces DoubtSearchConfig's per-investigation verification caps and
    picks which FLAGGED/contradicted claim is worth spending a doubt-search
    round on next.

    `max_free_attempts` verifications are "free" (always allowed if there's
    a flagged claim); beyond that, up to `max_total_attempts` are allowed
    only while the investigation's measured round-efficiency (findings per
    round) stays above `min_efficiency_to_spend`, so doubt search doesn't
    crowd out discovery once a run is already running dry.
    """

    def __init__(
        self,
        max_free_attempts: int = 1,
        max_total_attempts: int = 3,
        min_efficiency_to_spend: float = 0.5,
    ) -> None:
        self.max_free_attempts = max_free_attempts
        self.max_total_attempts = max_total_attempts
        self.min_efficiency_to_spend = min_efficiency_to_spend
        self._attempts_used = 0

    @property
    def attempts_used(self) -> int:
        return self._attempts_used

    def can_spend(self, current_round_efficiency: float) -> bool:
        """Whether another doubt-search attempt is allowed right now."""
        if self._attempts_used >= self.max_total_attempts:
            return False
        if self._attempts_used < self.max_free_attempts:
            return True
        return current_round_efficiency >= self.min_efficiency_to_spend

    def record_attempt(self) -> None:
        self._attempts_used += 1

    @staticmethod
    def pick_target(claims: list[Claim]) -> Optional[Claim]:
        """Choose the highest-value FLAGGED claim to verify next: fewest
        prior verify_attempts first (spread budget across claims rather
        than hammering one), then oldest (created_round) as a tiebreak."""
        candidates = [c for c in claims if c.status == ClaimStatus.FLAGGED]
        if not candidates:
            return None
        return min(candidates, key=lambda c: (c.verify_attempts, c.created_round))


# ---------------------------------------------------------------------------
# KB lessons: staging -> promotion -> demotion
# ---------------------------------------------------------------------------


class LessonStage(Enum):
    STAGED = "staged"
    ACTIVE = "active"
    DEMOTED = "demoted"


@dataclass
class KBLesson:
    id: str
    subject_type: str
    action_taken: str
    outcome_quality: float
    round_cost: int
    lesson: str
    stage: LessonStage = LessonStage.STAGED
    uses_since_promotion: int = 0
    cumulative_delta: float = 0.0
    created_at: float = field(default_factory=time.time)
    promoted_at: Optional[float] = None
    # "Skills" UI toggle (Requirement: user can activate/deactivate a
    # skill without losing its staged/active/demoted promotion history).
    # A deactivated lesson is excluded from active_lessons() regardless
    # of stage, but stays in the table -- toggling back on restores it
    # with its promotion history intact, only delete_lesson() is
    # permanent.
    active: bool = True


class OutcomeMemory:
    """SQLite-backed persistence for claims and kb_lessons, owning both
    tables via the shared db module (same pattern as SteeringIndex/PlanStore)."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path if db_path is not None else str(paths.db_path())
        conn = db.get_connection(self._db_path)
        with db.write_lock(self._db_path):
            conn.execute(_CREATE_CLAIMS_SQL)
            conn.execute(_CREATE_CLAIMS_INDEX_SQL)
            conn.execute(_CREATE_KB_LESSONS_SQL)
            # "Skills" UI (Requirement: user-toggleable global lessons):
            # kb_lessons predates the active flag, so existing on-disk
            # databases need it added post-hoc -- see db.ensure_column.
            db.ensure_column(conn, "kb_lessons", "active", "INTEGER NOT NULL DEFAULT 1")
            conn.commit()

    @property
    def _conn(self):
        return db.get_connection(self._db_path)

    # -- Claims --------------------------------------------------------

    def get_claims(self, investigation_id: str) -> list[Claim]:
        rows = self._conn.execute(
            "SELECT claim_id, investigation_id, subject, predicate, value, source_urls, "
            "status, confidence, verify_attempts, created_round, resolved_round "
            "FROM claims WHERE investigation_id = ?",
            (investigation_id,),
        ).fetchall()
        return [self._row_to_claim(row) for row in rows]

    def get_claim(self, claim_id: str) -> Optional[Claim]:
        row = self._conn.execute(
            "SELECT claim_id, investigation_id, subject, predicate, value, source_urls, "
            "status, confidence, verify_attempts, created_round, resolved_round "
            "FROM claims WHERE claim_id = ?",
            (claim_id,),
        ).fetchone()
        return self._row_to_claim(row) if row else None

    @staticmethod
    def _row_to_claim(row) -> Claim:
        return Claim(
            claim_id=row[0],
            investigation_id=row[1],
            subject=row[2],
            predicate=row[3],
            value=row[4],
            source_urls=json.loads(row[5]),
            status=ClaimStatus(row[6]),
            confidence=row[7],
            verify_attempts=row[8],
            created_round=row[9],
            resolved_round=row[10],
        )

    def save_claim(self, claim: Claim) -> None:
        """Upsert a claim (used for both newly-extracted and mutated
        (corroborated/verified) claims)."""
        conn = self._conn
        with db.write_lock(self._db_path):
            conn.execute(
                "INSERT INTO claims (claim_id, investigation_id, subject, predicate, value, "
                "source_urls, status, confidence, verify_attempts, created_round, resolved_round) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(claim_id) DO UPDATE SET "
                "value=excluded.value, source_urls=excluded.source_urls, status=excluded.status, "
                "confidence=excluded.confidence, verify_attempts=excluded.verify_attempts, "
                "resolved_round=excluded.resolved_round",
                (
                    claim.claim_id,
                    claim.investigation_id,
                    claim.subject,
                    claim.predicate,
                    claim.value,
                    json.dumps(claim.source_urls),
                    claim.status.value,
                    claim.confidence,
                    claim.verify_attempts,
                    claim.created_round,
                    claim.resolved_round,
                ),
            )
            conn.commit()

    def clear(self, investigation_id: str) -> None:
        conn = self._conn
        with db.write_lock(self._db_path):
            conn.execute("DELETE FROM claims WHERE investigation_id = ?", (investigation_id,))
            conn.commit()

    # -- KB lessons ------------------------------------------------------

    def stage_lesson(
        self,
        subject_type: str,
        action_taken: str,
        outcome_quality: float,
        round_cost: int,
        lesson: str,
    ) -> str:
        """Record a new staged (not yet trusted) lesson. Returns its id."""
        entry = KBLesson(
            id=uuid.uuid4().hex[:12],
            subject_type=subject_type,
            action_taken=action_taken,
            outcome_quality=outcome_quality,
            round_cost=round_cost,
            lesson=lesson,
        )
        conn = self._conn
        with db.write_lock(self._db_path):
            conn.execute(
                "INSERT INTO kb_lessons (id, subject_type, action_taken, outcome_quality, "
                "round_cost, lesson, stage, uses_since_promotion, cumulative_delta, created_at, promoted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0.0, ?, NULL)",
                (
                    entry.id,
                    entry.subject_type,
                    entry.action_taken,
                    entry.outcome_quality,
                    entry.round_cost,
                    entry.lesson,
                    entry.stage.value,
                    entry.created_at,
                ),
            )
            conn.commit()
        return entry.id

    def record_reuse(self, lesson_id: str, efficiency_delta: float) -> LessonStage:
        """Record one reuse of a lesson and its measured round-efficiency
        delta. Auto-promotes STAGED -> ACTIVE once both PROMOTION_MIN_USES
        and PROMOTION_MIN_CUMULATIVE_DELTA are crossed; auto-demotes an
        ACTIVE lesson whose cumulative_delta falls back below
        DEMOTION_CUMULATIVE_DELTA. Returns the lesson's stage after update.
        """
        conn = self._conn
        row = conn.execute(
            "SELECT stage, uses_since_promotion, cumulative_delta FROM kb_lessons WHERE id = ?",
            (lesson_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"No kb_lesson with id {lesson_id!r}")
        stage, uses, cumulative = LessonStage(row[0]), row[1] + 1, row[2] + efficiency_delta

        new_stage = stage
        promoted_at_val = None
        if (
            stage == LessonStage.STAGED
            and uses >= PROMOTION_MIN_USES
            and cumulative >= PROMOTION_MIN_CUMULATIVE_DELTA
        ):
            new_stage = LessonStage.ACTIVE
            promoted_at_val = time.time()
        elif stage == LessonStage.ACTIVE and cumulative <= DEMOTION_CUMULATIVE_DELTA:
            new_stage = LessonStage.DEMOTED

        with db.write_lock(self._db_path):
            if promoted_at_val is not None:
                conn.execute(
                    "UPDATE kb_lessons SET stage=?, uses_since_promotion=?, cumulative_delta=?, "
                    "promoted_at=? WHERE id=?",
                    (new_stage.value, uses, cumulative, promoted_at_val, lesson_id),
                )
            else:
                conn.execute(
                    "UPDATE kb_lessons SET stage=?, uses_since_promotion=?, cumulative_delta=? "
                    "WHERE id=?",
                    (new_stage.value, uses, cumulative, lesson_id),
                )
            conn.commit()
        return new_stage

    def active_lessons(self, subject_type: str, k: int = 5) -> list[KBLesson]:
        """Return up to k ACTIVE (promoted) lessons for a subject_type,
        highest cumulative_delta first -- the only lessons safe to surface
        to a thinker-tier replan prompt. Excludes lessons the user has
        deactivated via the Skills UI even if their stage is 'active'."""
        rows = self._conn.execute(
            "SELECT id, subject_type, action_taken, outcome_quality, round_cost, lesson, "
            "stage, uses_since_promotion, cumulative_delta, created_at, promoted_at, active "
            "FROM kb_lessons WHERE subject_type = ? AND stage = 'active' AND active = 1 "
            "ORDER BY cumulative_delta DESC LIMIT ?",
            (subject_type, k),
        ).fetchall()
        return [self._row_to_lesson(r) for r in rows]

    @staticmethod
    def _row_to_lesson(r) -> KBLesson:
        return KBLesson(
            id=r[0], subject_type=r[1], action_taken=r[2], outcome_quality=r[3],
            round_cost=r[4], lesson=r[5], stage=LessonStage(r[6]),
            uses_since_promotion=r[7], cumulative_delta=r[8], created_at=r[9],
            promoted_at=r[10], active=bool(r[11]),
        )

    # -- Skills (kb_lessons surfaced as user-manageable entries) --------

    def list_all_lessons(self, subject_type: Optional[str] = None) -> list[KBLesson]:
        """List every lesson regardless of stage/active flag -- feeds the
        Skills panel, which shows staged/active/demoted and lets the user
        see + toggle + delete any of them, not just the promoted subset
        active_lessons() hands to the planner."""
        if subject_type:
            rows = self._conn.execute(
                "SELECT id, subject_type, action_taken, outcome_quality, round_cost, lesson, "
                "stage, uses_since_promotion, cumulative_delta, created_at, promoted_at, active "
                "FROM kb_lessons WHERE subject_type = ? "
                "ORDER BY cumulative_delta DESC",
                (subject_type,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, subject_type, action_taken, outcome_quality, round_cost, lesson, "
                "stage, uses_since_promotion, cumulative_delta, created_at, promoted_at, active "
                "FROM kb_lessons ORDER BY cumulative_delta DESC"
            ).fetchall()
        return [self._row_to_lesson(r) for r in rows]

    def set_lesson_active(self, lesson_id: str, active: bool) -> Optional[KBLesson]:
        """Toggle a lesson's active flag. Returns the updated lesson, or
        None if lesson_id doesn't exist. Deactivating does NOT touch
        stage/cumulative_delta -- re-activating restores it exactly where
        its promotion history left off."""
        conn = self._conn
        with db.write_lock(self._db_path):
            cursor = conn.execute(
                "UPDATE kb_lessons SET active = ? WHERE id = ?", (int(active), lesson_id),
            )
            conn.commit()
            if cursor.rowcount == 0:
                return None
        row = conn.execute(
            "SELECT id, subject_type, action_taken, outcome_quality, round_cost, lesson, "
            "stage, uses_since_promotion, cumulative_delta, created_at, promoted_at, active "
            "FROM kb_lessons WHERE id = ?",
            (lesson_id,),
        ).fetchone()
        return self._row_to_lesson(row) if row else None

    def delete_lesson(self, lesson_id: str) -> bool:
        """Permanently remove a lesson (Requirement: user can delete a
        skill they've found unhelpful, not just deactivate it). Returns
        False if lesson_id didn't exist."""
        conn = self._conn
        with db.write_lock(self._db_path):
            cursor = conn.execute("DELETE FROM kb_lessons WHERE id = ?", (lesson_id,))
            conn.commit()
            return cursor.rowcount > 0
