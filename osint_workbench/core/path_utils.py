"""Path normalization utilities for cross-platform path handling.

Provides functions to normalize path separators from configuration files
to the host operating system's native separator. Handles Windows backslashes
on Unix systems and forward slashes on Windows systems.

Requirements: 8.2, 8.3, 8.5
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def normalize_path(path_str: str, base_dir: Path | None = None) -> Path:
    """Normalize a path string from a config file to the host OS native format.

    Converts all path separators (both forward slashes and backslashes) to
    the host operating system's native separator using pathlib. Logs a warning
    if the resolved path does not exist on the file system.

    Args:
        path_str: A path string potentially containing mixed separators
            (e.g., "templates\\report.md" or "reports/output").
        base_dir: Optional base directory to resolve relative paths against.
            If provided and the path is relative, it will be resolved against
            this directory for the existence check. The returned Path is still
            the normalized (possibly relative) path.

    Returns:
        A Path object with separators normalized to the host OS native format.

    Examples:
        On Unix:
            normalize_path("templates\\\\report.md") -> Path("templates/report.md")
            normalize_path("reports\\\\2024\\\\output") -> Path("reports/2024/output")

        On Windows:
            normalize_path("templates/report.md") -> Path("templates\\\\report.md")
            normalize_path("reports/2024/output") -> Path("reports\\\\2024\\\\output")
    """
    if not path_str:
        logger.warning("Empty path string provided for normalization.")
        return Path("")

    # Replace both separator types with the OS-native separator.
    # First normalize all separators to forward slash, then let Path handle it.
    # This approach handles mixed separators like "templates\\sub/file.md"
    unified = path_str.replace("\\", "/").replace("/", os.sep)

    # Use Path to get proper normalization (removes redundant separators, etc.)
    normalized = Path(unified)

    # Check existence and log warning if path doesn't resolve to an existing location
    if base_dir is not None:
        resolved = base_dir / normalized if not normalized.is_absolute() else normalized
    else:
        resolved = normalized

    if not resolved.exists():
        logger.warning(
            "Normalized path does not exist: '%s' (resolved to '%s')",
            path_str,
            resolved,
        )

    return normalized
