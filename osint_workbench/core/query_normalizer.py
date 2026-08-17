"""Query normalization for deduplication.

Provides a deterministic canonical form for search query strings,
enabling detection of semantically identical queries with different formatting.
"""

import re
from typing import List

# Regex pattern matching search operators and their values
_OPERATOR_PATTERN = re.compile(
    r'(site:|inurl:|intitle:|intext:|filetype:|ext:)(\S+)',
    re.IGNORECASE
)


def normalize_query(query: str) -> str:
    """Normalize a search query string to canonical form.

    Transformations applied:
    1. Strip leading/trailing whitespace
    2. Collapse consecutive whitespace to single space
    3. Convert to lowercase
    4. Extract operator:value pairs as atomic tokens
    5. Sort all tokens (words + operator pairs) alphabetically
    6. Rejoin with single space

    Args:
        query: The raw query string.

    Returns:
        Canonical normalized string suitable for equality comparison.
        Returns empty string for whitespace-only input.
    """
    if not query or not query.strip():
        return ""

    # Step 1-2: Strip and collapse whitespace
    collapsed = re.sub(r'\s+', ' ', query.strip())

    # Step 3: Lowercase
    lowered = collapsed.lower()

    # Step 4: Extract operators as atomic tokens
    operators: List[str] = []
    remaining = lowered

    for match in _OPERATOR_PATTERN.finditer(lowered):
        operators.append(match.group(0))  # e.g., "site:example.com"

    # Remove operators from remaining text
    remaining = _OPERATOR_PATTERN.sub('', remaining).strip()
    remaining = re.sub(r'\s+', ' ', remaining).strip()

    # Step 5: Split remaining into words, combine with operators, sort
    words = remaining.split() if remaining else []
    all_tokens = sorted(words + operators)

    # Step 6: Join
    return ' '.join(all_tokens)
