"""Quality Pipeline for filtering, deduplicating, and scoring OSINT findings."""

import hashlib
import logging
from typing import List, Optional

from osint_workbench.core.models import ScoredFinding

logger = logging.getLogger(__name__)

# Default noise indicator patterns (case-insensitive matching)
DEFAULT_NOISE_PATTERNS = [
    "captcha",
    "domain for sale",
    "access denied",
    "page not found",
]


class QualityPipeline:
    """Filters, deduplicates, and scores findings before LLM context window consumption."""

    def __init__(
        self,
        noise_patterns: Optional[List[str]] = None,
        min_relevance_score: float = 0.3,
    ):
        """Initialize the quality pipeline.

        Args:
            noise_patterns: List of case-insensitive strings that indicate noise content.
            min_relevance_score: Minimum relevance score for LLM submission (default 0.3).
        """
        self.noise_patterns = noise_patterns if noise_patterns is not None else DEFAULT_NOISE_PATTERNS
        self.min_relevance_score = min_relevance_score

    def compute_relevance(self, finding: dict, target: str) -> float:
        """Score how relevant a finding is to the target.

        Scoring rubric:
            - Target in title (case-insensitive): +0.3
            - Target in snippet (case-insensitive): +0.2
            - Snippet word count > 20: +0.2
            - Status is "Active/Accessible": +0.2
            - Multiple mentions of target in snippet (count > 1): +0.1

        Args:
            finding: Dict with keys: name, url, status, title, snippet, category.
            target: The investigation target string.

        Returns:
            Relevance score capped at 1.0.
        """
        score = 0.0
        target_lower = target.lower()

        title = (finding.get("title") or "").lower()
        snippet = (finding.get("snippet") or "").lower()
        status = finding.get("status") or ""

        # Target in title: +0.3
        if target_lower in title:
            score += 0.3

        # Target in snippet: +0.2
        if target_lower in snippet:
            score += 0.2

        # Snippet word count > 20: +0.2
        snippet_text = finding.get("snippet") or ""
        word_count = len(snippet_text.split())
        if word_count > 20:
            score += 0.2

        # Accessible status: +0.2
        if status == "Active/Accessible":
            score += 0.2

        # Multiple mentions of target in snippet (count > 1): +0.1
        mention_count = snippet.count(target_lower)
        if mention_count > 1:
            score += 0.1

        return min(round(score, 8), 1.0)

    def is_noise(self, finding: dict) -> bool:
        """Detect noise findings: HTTP errors, CAPTCHA, parking pages, sparse content.

        Noise indicators:
            - Status starts with "HTTP Status 4" or "HTTP Status 5"
            - Snippet or title contains noise patterns (case-insensitive)
            - Snippet has fewer than 3 words

        If noise is detected, the finding's relevance_score is set to 0.0.

        Args:
            finding: Dict with keys: name, url, status, title, snippet, category.

        Returns:
            True if the finding is noise, False otherwise.
        """
        status = finding.get("status") or ""
        title = (finding.get("title") or "").lower()
        snippet_text = finding.get("snippet") or ""
        snippet_lower = snippet_text.lower()

        # HTTP 4xx/5xx status
        if status.startswith("HTTP Status 4") or status.startswith("HTTP Status 5"):
            return True

        # Check noise patterns in title or snippet
        for pattern in self.noise_patterns:
            pattern_lower = pattern.lower()
            if pattern_lower in title or pattern_lower in snippet_lower:
                return True

        # Snippet has fewer than 3 words
        word_count = len(snippet_text.split())
        if word_count < 3:
            return True

        return False

    def deduplicate_content(self, findings: List[ScoredFinding]) -> List[ScoredFinding]:
        """Remove findings with duplicate content using SHA-256 hash of snippet.

        Retains the first occurrence of each unique content hash.

        Args:
            findings: List of ScoredFinding objects with content_hash populated.

        Returns:
            Deduplicated list retaining first occurrence of each hash.
        """
        seen_hashes: set = set()
        deduplicated: List[ScoredFinding] = []

        for finding in findings:
            if finding.content_hash not in seen_hashes:
                seen_hashes.add(finding.content_hash)
                deduplicated.append(finding)

        return deduplicated

    def filter_and_score(self, findings: List[dict], target: str) -> List[ScoredFinding]:
        """Score findings by relevance, filter noise, deduplicate by content.

        Pipeline:
            1. For each finding: detect noise, compute relevance (0.0 if noise)
            2. Create ScoredFinding objects with content_hash
            3. Deduplicate by content hash
            4. Sort: non-noise by relevance descending, then noise at bottom
            5. Exclude findings with score < 0.3 from LLM submission (mark is_noise=True)
            6. Cap non-noise findings at 50 for LLM processing
            7. Log warning if all findings are noise

        Args:
            findings: List of dicts with keys: name, url, status, title, snippet, category.
            target: The investigation target string.

        Returns:
            List of ScoredFinding objects sorted by relevance with noise at bottom.
        """
        scored_findings: List[ScoredFinding] = []

        for finding in findings:
            # Detect noise
            noise = self.is_noise(finding)

            # Compute relevance (0.0 if noise)
            if noise:
                relevance = 0.0
            else:
                relevance = self.compute_relevance(finding, target)

            # Compute content hash from snippet
            snippet_text = finding.get("snippet") or ""
            content_hash = hashlib.sha256(snippet_text.encode("utf-8")).hexdigest()

            # Create ScoredFinding
            scored = ScoredFinding(
                url=finding.get("url", ""),
                name=finding.get("name", ""),
                title=finding.get("title") or "",
                snippet=snippet_text,
                relevance_score=relevance,
                is_noise=noise,
                category=finding.get("category", ""),
                content_hash=content_hash,
            )

            scored_findings.append(scored)

        # Deduplicate by content hash
        scored_findings = self.deduplicate_content(scored_findings)

        # Mark findings below threshold as noise (exclude from LLM submission)
        for finding in scored_findings:
            if not finding.is_noise and finding.relevance_score < self.min_relevance_score:
                finding.is_noise = True

        # Separate non-noise and noise findings
        non_noise = [f for f in scored_findings if not f.is_noise]
        noise_findings = [f for f in scored_findings if f.is_noise]

        # Sort non-noise by relevance descending
        non_noise.sort(key=lambda f: f.relevance_score, reverse=True)

        # Sort noise by relevance descending (for consistent ordering)
        noise_findings.sort(key=lambda f: f.relevance_score, reverse=True)

        # Cap non-noise findings at 50 for LLM processing
        non_noise = non_noise[:50]

        # Check if all findings are noise
        if not non_noise:
            logger.warning(
                "No relevant findings identified for target '%s'. "
                "All %d findings were classified as noise.",
                target,
                len(scored_findings),
            )

        # Combine: non-noise first, then noise at bottom
        result = non_noise + noise_findings

        return result
