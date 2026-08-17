"""Stigmergic steering index: one weighted, decaying/reinforcing table that
serves query dedup, RAG-hint retrieval, and cross-investigation source-
category weighting through the same read/decay/reinforce operations.

Mirrors ant-colony pheromone trails: a candidate query/hint/category gets
auto-rejected (or auto-preferred) by matching against existing entries
weighted by their current pheromone, without ever spending an LLM call to
make that judgment. Productive entries get reinforced (pheromone rises,
capped) after a round proves they yielded findings; unproductive ones decay
exponentially and become eligible again later -- muted, never deleted, so a
played-out direction is reversible rather than a permanent blocklist.

No embeddings anywhere: similarity is plain Jaccard token-overlap on
normalized text, since a local LM Studio setup may have zero
embeddings-capable model loaded. This is the intentional, cheaper baseline
the architecture decision settled on over a vector store.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Optional

from . import db, paths

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS steering_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    entry_type TEXT NOT NULL CHECK(entry_type IN ('query', 'hint', 'source_category')),
    fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    pheromone REAL NOT NULL DEFAULT 1.0,
    trust_tier TEXT NOT NULL DEFAULT 'auto_extracted',
    kind TEXT NOT NULL DEFAULT 'normal',
    created_at REAL NOT NULL,
    last_touched_at REAL NOT NULL,
    reinforce_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(scope, entry_type, fingerprint)
)
"""
_CREATE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_steering_scope_type "
    "ON steering_index(scope, entry_type)"
)

# Pheromone below this floor is treated as "muted" -- excluded from check()
# blocking and from top() results, but the row is kept (never deleted) so
# decay is reversible.
MUTE_FLOOR = 0.05

# decay()'s floor: comfortably below MUTE_FLOOR (so top()/check() muting is
# unaffected) but strictly positive, so repeated multiplicative decay of a
# long-lived, never-reinforced GLOBAL_SCOPE row can never underflow to an
# exact float 0.0 -- see decay()'s docstring for why that would matter.
_DECAY_FLOOR = 1e-6

DEFAULT_HALF_LIFE_ROUNDS = 6.0
# Doubt-search "verification" queries decay much faster than exploratory
# ones -- the verification trail itself shouldn't dominate future planning.
VERIFICATION_HALF_LIFE_ROUNDS = 2.0

# Provenance-weighted starting trust for RAG hints (Requirement: hints must
# be structurally trust-tiered, not mixed untagged into free prompt text).
TRUST_TIER_WEIGHTS = {
    "typed_snippet": 1.0,  # user-typed text snippet -- highest trust
    "document": 0.8,  # uploaded document
    "ocr_image": 0.6,  # OCR'd image caption/text
    "auto_extracted": 0.4,  # engine-derived from a prior round's findings
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    """Lowercase, alnum-token split. No stemming/stopwords -- kept dumb and
    fast since this runs on every candidate query/hint check."""
    return set(_WORD_RE.findall((text or "").lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    """Token-overlap similarity in [0, 1]. Empty sets never match (0.0),
    not undefined -- an empty candidate can't be "too similar" to anything."""
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


@dataclass
class SteeringEntry:
    """A single steering_index row."""

    id: int
    scope: str
    entry_type: str
    fingerprint: str
    payload: str
    pheromone: float
    trust_tier: str
    kind: str
    created_at: float
    last_touched_at: float
    reinforce_count: int


_SELECT_COLUMNS = (
    "id, scope, entry_type, fingerprint, payload, pheromone, trust_tier, "
    "kind, created_at, last_touched_at, reinforce_count"
)


class SteeringIndex:
    """Weighted, decaying/reinforcing index shared by query dedup, RAG
    hints, and cross-investigation source-category weighting.

    `scope` is the investigation_id for 'query'/'hint' rows (wiped when an
    investigation starts, mirroring QueryRegistry.clear()) or the constant
    `GLOBAL_SCOPE` for 'source_category' rows, which persist across
    investigations keyed by fingerprint `f"{subject_type}:{category}"`.
    """

    GLOBAL_SCOPE = "global"

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path if db_path is not None else str(paths.db_path())
        conn = db.get_connection(self._db_path)
        with db.write_lock(self._db_path):
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.commit()

    @property
    def _conn(self):
        # Never cache the connection object on self: db.get_connection() is
        # a cheap dict lookup, and re-resolving it here means a caller that
        # closes/reopens the shared connection (StateManager.close() in
        # tests) never leaves this instance holding a stale, closed handle.
        return db.get_connection(self._db_path)

    def check(
        self,
        scope: str,
        entry_type: str,
        candidate_text: str,
        threshold: float = 0.6,
    ) -> Optional[SteeringEntry]:
        """Return the best-matching hot entry blocking `candidate_text`, or
        None if the candidate is novel enough to proceed.

        A candidate is rejected (this returns the matched entry) when
        `similarity * pheromone` against some existing same-scope/entry_type
        row exceeds `threshold`. This is a deterministic table scan -- it
        never calls the LLM, which is what lets "don't repeat this search"
        be enforced before a small model ever sees the candidate.

        Args:
            scope: Investigation id, or GLOBAL_SCOPE for source_category rows.
            entry_type: 'query', 'hint', or 'source_category'.
            candidate_text: The free text to check for a hot near-duplicate.
            threshold: Rejection cutoff on similarity * pheromone, in [0, 1].

        Returns:
            The blocking SteeringEntry, or None if the candidate may proceed.
        """
        candidate_tokens = _tokenize(candidate_text)
        if not candidate_tokens:
            return None
        rows = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM steering_index "
            "WHERE scope = ? AND entry_type = ?",
            (scope, entry_type),
        ).fetchall()
        best: Optional[SteeringEntry] = None
        best_score = 0.0
        for row in rows:
            entry = SteeringEntry(*row)
            if entry.pheromone < MUTE_FLOOR:
                continue
            # Cap pheromone's contribution here at 1.0: reinforce() exists to
            # reward ranking in top(), not to make dedup MORE aggressive.
            # Decay pulling pheromone below 1.0 correctly weakens blocking
            # (a cooled-off entry becomes eligible again); reinforcement
            # pushing it above 1.0 must NOT then over-block similar-but-novel
            # candidates just because an unrelated past query yielded well.
            score = _jaccard(candidate_tokens, _tokenize(entry.payload)) * min(1.0, entry.pheromone)
            if score > threshold and score > best_score:
                best, best_score = entry, score
        return best

    def add(
        self,
        scope: str,
        entry_type: str,
        fingerprint: str,
        payload: str,
        trust_tier: str = "auto_extracted",
        kind: str = "normal",
        initial_pheromone: Optional[float] = None,
    ) -> int:
        """Insert an entry, or reactivate/refresh it if (scope, entry_type,
        fingerprint) already exists.

        Args:
            scope: Investigation id, or GLOBAL_SCOPE for source_category rows.
            entry_type: 'query', 'hint', or 'source_category'.
            fingerprint: Stable dedup key (normalized query string, or
                `f"{slot_type}:{normalized_value}"` for hints, or
                `f"{subject_type}:{category}"` for source_category rows).
            payload: The free text used for similarity matching in check().
            trust_tier: Provenance tier; determines default starting pheromone
                for hints via TRUST_TIER_WEIGHTS.
            kind: Free-form sub-classification (e.g. "normal" vs
                "verification" for doubt-search queries, which decay faster).
            initial_pheromone: Explicit starting weight; overrides the
                trust_tier-derived default.

        Returns:
            The row id of the inserted/updated entry.
        """
        now = time.time()
        if initial_pheromone is not None:
            pheromone = initial_pheromone
        elif entry_type == "hint":
            # Trust-tier weighting (typed_snippet > document > ocr_image >
            # auto_extracted) is a RAG-provenance concept -- it only applies
            # to hint rows. A 'query'/'source_category' row without an
            # explicit initial_pheromone starts fully trusted (1.0): a
            # query that was actually tried, or a category that was
            # actually searched, is a fact, not a provenance-graded guess.
            pheromone = TRUST_TIER_WEIGHTS.get(trust_tier, 1.0)
        else:
            pheromone = 1.0
        conn = self._conn
        with db.write_lock(self._db_path):
            conn.execute(
                "INSERT INTO steering_index "
                "(scope, entry_type, fingerprint, payload, pheromone, trust_tier, "
                "kind, created_at, last_touched_at, reinforce_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0) "
                "ON CONFLICT(scope, entry_type, fingerprint) DO UPDATE SET "
                "payload=excluded.payload, trust_tier=excluded.trust_tier, "
                "kind=excluded.kind, pheromone=excluded.pheromone, "
                "last_touched_at=excluded.last_touched_at",
                (scope, entry_type, fingerprint, payload, pheromone, trust_tier, kind, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT id FROM steering_index WHERE scope=? AND entry_type=? AND fingerprint=?",
                (scope, entry_type, fingerprint),
            ).fetchone()
        return row[0] if row else -1

    def reinforce(self, entry_id: int, yield_score: float) -> None:
        """Multiply an entry's pheromone by a yield-derived factor.

        Clamped to [0.5, 2.0] so one noisy round can't instantly zero out or
        explode an entry's weight -- the load-bearing risk of this whole
        mechanism is bad yield attribution, and a hard clamp bounds the
        damage a single mis-attributed round can do.

        Args:
            entry_id: The steering_index row id to reinforce (from add() or
                check()/top()'s returned SteeringEntry.id).
            yield_score: Findings-per-cost for the round this entry produced,
                normalized so 1.0 = the investigation's rolling average
                (>1 rewards above-average yield, <1 penalizes below-average).
        """
        factor = max(0.5, min(2.0, yield_score))
        conn = self._conn
        with db.write_lock(self._db_path):
            conn.execute(
                "UPDATE steering_index SET pheromone = pheromone * ?, "
                "last_touched_at = ?, reinforce_count = reinforce_count + 1 "
                "WHERE id = ?",
                (factor, time.time(), entry_id),
            )
            conn.commit()

    def decay(
        self,
        scope: str,
        entry_type: Optional[str] = None,
        rounds_elapsed: float = 1.0,
        half_life_rounds: float = DEFAULT_HALF_LIFE_ROUNDS,
    ) -> None:
        """Apply exponential decay to every entry in `scope` (optionally
        restricted to one entry_type). Call once per research round so
        unproductive queries/hints/categories cool down and become eligible
        again without ever being deleted.

        Args:
            scope: Investigation id, or GLOBAL_SCOPE for source_category rows.
            entry_type: Restrict decay to this entry_type, or None for all.
            rounds_elapsed: How many rounds' worth of decay to apply.
            half_life_rounds: Rounds for pheromone to halve if untouched.
        """
        # Floor decay at a small positive epsilon, well below MUTE_FLOOR (so
        # top()/check() muting behavior is unaffected) but never exact 0.0.
        # GLOBAL_SCOPE rows (e.g. RAG hints) persist across investigations
        # and are decayed every round forever -- without a floor, repeated
        # multiplication eventually underflows to a hard float 0.0, and
        # reinforce()'s <=2.0x-per-call clamp can NEVER recover from an
        # actual zero. That would silently break the invariant documented
        # above (MUTE_FLOOR docstring): rows are kept "so decay is
        # reversible" -- reversible requires staying strictly positive.
        factor = 0.5 ** (rounds_elapsed / half_life_rounds) if half_life_rounds > 0 else 1.0
        conn = self._conn
        with db.write_lock(self._db_path):
            if entry_type:
                conn.execute(
                    "UPDATE steering_index SET pheromone = MAX(pheromone * ?, ?) "
                    "WHERE scope = ? AND entry_type = ?",
                    (factor, _DECAY_FLOOR, scope, entry_type),
                )
            else:
                conn.execute(
                    "UPDATE steering_index SET pheromone = MAX(pheromone * ?, ?) WHERE scope = ?",
                    (factor, _DECAY_FLOOR, scope),
                )
            conn.commit()

    def top(
        self,
        scope: str,
        entry_type: str,
        k: int = 5,
        fingerprint_prefix: Optional[str] = None,
    ) -> list[SteeringEntry]:
        """Return the k highest-pheromone non-muted entries for (scope, entry_type).

        Gives the plan object a read-only "what's currently hot" view
        without needing to know about similarity math or decay internals --
        the plan object owns sequencing, this index owns what's worth
        pursuing. `fingerprint_prefix` lets a caller scope source_category
        rows to one subject_type (fingerprints are `f"{subject_type}:{category}"`).

        Args:
            scope: Investigation id, or GLOBAL_SCOPE for source_category rows.
            entry_type: 'query', 'hint', or 'source_category'.
            k: Maximum number of entries to return.
            fingerprint_prefix: Optional SQL LIKE prefix filter on fingerprint.

        Returns:
            Up to k SteeringEntry rows, highest pheromone first.
        """
        conn = self._conn
        if fingerprint_prefix:
            rows = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM steering_index "
                "WHERE scope = ? AND entry_type = ? AND pheromone >= ? "
                "AND fingerprint LIKE ? ESCAPE '\\' "
                "ORDER BY pheromone DESC LIMIT ?",
                (
                    scope,
                    entry_type,
                    MUTE_FLOOR,
                    fingerprint_prefix.replace("%", r"\%").replace("_", r"\_") + "%",
                    k,
                ),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM steering_index "
                "WHERE scope = ? AND entry_type = ? AND pheromone >= ? "
                "ORDER BY pheromone DESC LIMIT ?",
                (scope, entry_type, MUTE_FLOOR, k),
            ).fetchall()
        return [SteeringEntry(*row) for row in rows]

    def get(self, entry_id: int) -> Optional[SteeringEntry]:
        """Fetch a single entry by id, or None if it doesn't exist."""
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM steering_index WHERE id = ?",
            (entry_id,),
        ).fetchone()
        return SteeringEntry(*row) if row else None

    def get_by_fingerprint(
        self, scope: str, entry_type: str, fingerprint: str
    ) -> Optional[SteeringEntry]:
        """Exact-match lookup by (scope, entry_type, fingerprint).

        Unlike check() (near-duplicate similarity match) or top()'s
        fingerprint_prefix (a LIKE-prefix scan that can over-match a
        shorter fingerprint against longer ones sharing the same prefix),
        this is a precise point lookup -- used to read back a specific
        known entry (e.g. a source_category row for one exact
        subject_type:category pair) without similarity ambiguity.
        """
        row = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM steering_index "
            "WHERE scope = ? AND entry_type = ? AND fingerprint = ?",
            (scope, entry_type, fingerprint),
        ).fetchone()
        return SteeringEntry(*row) if row else None

    def clear_scope(self, scope: str) -> None:
        """Remove all rows for a scope. Call when a new investigation starts
        (mirroring QueryRegistry.clear()). No-ops on GLOBAL_SCOPE, which is
        the whole point of that scope: it must survive across investigations.
        """
        if scope == self.GLOBAL_SCOPE:
            return
        conn = self._conn
        with db.write_lock(self._db_path):
            conn.execute("DELETE FROM steering_index WHERE scope = ?", (scope,))
            conn.commit()
