"""Token Budget Manager for context window management.

Prevents context window overflow by tracking cumulative token usage,
truncating findings to fit within model limits, and signaling when
research should stop due to budget exhaustion.
"""

from typing import List


class TokenBudgetManager:
    """Manages token budget for LLM context window.

    Tracks cumulative token usage across research rounds, truncates
    findings to fit within available budget, and signals when context
    is approaching capacity.

    Attributes:
        context_window: Total context window size in tokens.
        system_reserve: Tokens reserved for system prompt.
        response_reserve: Tokens reserved for LLM response output.
        used_tokens: Cumulative tokens consumed so far.
    """

    # Minimum tokens needed for one compressed finding (name + url + status + category + short snippet)
    MIN_COMPRESSED_FINDING_TOKENS = 50

    def __init__(
        self,
        context_window: int = 8192,
        system_reserve: int = 500,
        response_reserve: int = 2000,
    ):
        """Initialize the token budget manager.

        Args:
            context_window: Total context window size in tokens (default 8192).
            system_reserve: Tokens reserved for system prompt (default 500).
            response_reserve: Tokens reserved for LLM response (default 2000).
        """
        self.context_window = context_window
        self.system_reserve = system_reserve
        self.response_reserve = response_reserve
        self.used_tokens = 0

    def estimate_tokens(self, text: str) -> int:
        """Estimate token count for a text string.

        Uses a character-based approximation (len(text) / 4) consistent
        with the LLMClient estimation approach.

        Args:
            text: The text to estimate tokens for.

        Returns:
            Estimated token count (minimum 1 for non-empty text, 0 for empty).
        """
        if not text:
            return 0
        return max(1, len(text) // 4)

    def get_available_tokens(self) -> int:
        """Return how many tokens are available for findings content.

        Calculates: context_window - system_reserve - response_reserve - used_tokens.
        Returns 0 if budget is exhausted.

        Returns:
            Number of available tokens (non-negative).
        """
        return max(
            0,
            self.context_window
            - self.system_reserve
            - self.response_reserve
            - self.used_tokens,
        )

    def add_used_tokens(self, count: int) -> None:
        """Increment the cumulative used tokens tracker.

        Args:
            count: Number of tokens to add to the usage counter.
        """
        self.used_tokens += count

    def truncate_findings(
        self, findings: List[dict], available_tokens: int
    ) -> List[dict]:
        """Truncate findings list to fit within available token budget.

        Prioritizes findings by:
        1. Status: "Active/Accessible" findings ranked first.
        2. Insertion order: most recently added first (reverse order).

        Iterates through sorted findings, including each if it fits.
        If a finding doesn't fit entirely but can be compressed, includes
        the compressed version. Stops when budget is exhausted.

        Args:
            findings: List of finding dicts to truncate.
            available_tokens: Maximum tokens available for findings.

        Returns:
            List of finding dicts that fit within the token budget.
        """
        if not findings or available_tokens <= 0:
            return []

        # Sort findings: Active/Accessible first, then reverse insertion order
        # We use enumerate to track original insertion order
        indexed_findings = list(enumerate(findings))

        def sort_key(item):
            idx, finding = item
            # Primary: Active/Accessible status gets priority (0 = high, 1 = low)
            status = finding.get("status", "")
            status_priority = 0 if status == "Active/Accessible" else 1
            # Secondary: most recently added first (higher index = more recent)
            # Negate index so higher indices sort first
            return (status_priority, -idx)

        indexed_findings.sort(key=sort_key)

        result = []
        remaining_tokens = available_tokens

        for _idx, finding in indexed_findings:
            if remaining_tokens <= 0:
                break

            # Estimate tokens for the full finding
            finding_tokens = self._estimate_finding_tokens(finding)

            if finding_tokens <= remaining_tokens:
                # Finding fits entirely
                result.append(finding)
                remaining_tokens -= finding_tokens
            elif remaining_tokens >= self.MIN_COMPRESSED_FINDING_TOKENS:
                # Try to compress and fit
                compressed = self.compress_finding(finding, remaining_tokens)
                compressed_tokens = self._estimate_finding_tokens(compressed)
                if compressed_tokens <= remaining_tokens:
                    result.append(compressed)
                    remaining_tokens -= compressed_tokens
            # else: skip this finding, budget too small

        return result

    def compress_finding(self, finding: dict, max_tokens: int) -> dict:
        """Compress a single finding to fit within a token limit.

        Retains name, url, status, and category fields. Truncates the
        snippet to a maximum of 80 tokens (~320 characters).

        Args:
            finding: The finding dict to compress.
            max_tokens: Maximum tokens allowed for this finding.

        Returns:
            Compressed finding dict with essential fields retained.
        """
        snippet = finding.get("snippet", "") or ""

        # Truncate snippet to max 80 tokens (~320 characters)
        max_snippet_chars = 80 * 4  # 320 chars ≈ 80 tokens
        if len(snippet) > max_snippet_chars:
            snippet = snippet[:max_snippet_chars]

        return {
            "name": finding.get("name", ""),
            "url": finding.get("url", ""),
            "status": finding.get("status", ""),
            "category": finding.get("category", ""),
            "snippet": snippet,
        }

    def should_stop_research(self) -> bool:
        """Determine whether research should stop due to budget limits.

        Returns True when:
        - Cumulative used tokens >= 90% of total available budget
          (context_window - system_reserve - response_reserve)
        - Available tokens are too small to fit even one compressed finding

        Returns:
            True if research should stop, False otherwise.
        """
        total_budget = (
            self.context_window - self.system_reserve - self.response_reserve
        )

        # Stop if used >= 90% of total budget
        if total_budget > 0 and self.used_tokens >= 0.9 * total_budget:
            return True

        # Stop if we can't fit even one compressed finding
        available = self.get_available_tokens()
        if available < self.MIN_COMPRESSED_FINDING_TOKENS:
            return True

        return False

    def _estimate_finding_tokens(self, finding: dict) -> int:
        """Estimate total tokens for a finding dict.

        Sums the estimated tokens of all string values in the finding.

        Args:
            finding: The finding dict to estimate.

        Returns:
            Estimated total token count for the finding.
        """
        total = 0
        for value in finding.values():
            if isinstance(value, str):
                total += self.estimate_tokens(value)
        return total
