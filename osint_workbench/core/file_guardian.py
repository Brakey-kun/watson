"""File Guardian module for the self-healing integrity engine.

Handles file-level detection and repair operations for critical application files.
All file operations use pathlib for platform-agnostic path handling.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from osint_workbench.core.manifest import (
    DEFAULT_SOURCES_JSON,
    ManifestEntry,
    RepairAction,
)

logger = logging.getLogger(__name__)


class FileGuardian:
    """Monitors and restores critical application files.

    Detects missing, corrupted, or invalid files and applies appropriate
    repair actions (recreate from defaults, backup and recreate).
    """

    # Expected target type categories in sources.json
    SOURCES_CATEGORIES = ("person", "organization", "domain", "ip_address", "email")

    def __init__(self, portable_root: Path, data_root: Optional[Path] = None) -> None:
        """Initialize FileGuardian with the portable root and data root paths.

        Args:
            portable_root: The root directory of the portable application.
                App assets (has_user_data=False manifest entries) resolve here.
            data_root: The external per-user data directory. User-data manifest
                entries (has_user_data=True, e.g. config.json, reports/) resolve
                here instead. Defaults to portable_root when omitted, preserving
                the pre-split behavior for callers that don't care about the
                distinction (e.g. tests exercising a single tmp_path).
        """
        self.portable_root = portable_root
        self.data_root = data_root if data_root is not None else portable_root
        self._default_sources: Optional[dict[str, Any]] = None

    def _base_for(self, entry: ManifestEntry) -> Path:
        """Return the root a manifest entry's relative_path resolves against."""
        return self.data_root if entry.root == "data" else self.portable_root

    @property
    def default_sources(self) -> dict[str, Any]:
        """Lazily parse and cache the default sources JSON."""
        if self._default_sources is None:
            self._default_sources = json.loads(DEFAULT_SOURCES_JSON)
        return self._default_sources

    @staticmethod
    def is_valid_source_entry(entry: Any) -> bool:
        """Check if a source entry is structurally valid.

        A source entry is valid iff it is a dict containing both a "name" field
        with a non-empty string value AND a "url" field with a non-empty string value.

        Args:
            entry: The value to validate as a source entry.

        Returns:
            True if valid, False otherwise.
        """
        if not isinstance(entry, dict):
            return False
        name = entry.get("name")
        url = entry.get("url")
        return (
            isinstance(name, str)
            and len(name.strip()) > 0
            and isinstance(url, str)
            and len(url.strip()) > 0
        )

    def repair_sources_json(self, path: Path, entry: ManifestEntry) -> RepairAction:
        """Repair sources.json by merging missing categories and fixing invalid values.

        This method handles the following repair scenarios:
        - Missing target type categories: adds them with default entries while
          preserving all existing categories and their entries.
        - Category value is not a JSON array: replaces with default entries for
          that category.

        Args:
            path: Path to the sources.json file.
            entry: The manifest entry for sources.json.

        Returns:
            RepairAction indicating the result of the repair operation.
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            # If we can't read/parse it, fall back to backup and recreate
            error_msg = f"Failed to read sources.json for repair: {e}"
            logger.error(error_msg)
            return self.backup_and_recreate(path, entry)

        if not isinstance(data, dict):
            # Top-level is not a dict, backup and recreate entirely
            return self.backup_and_recreate(path, entry)

        modified = False
        defaults = self.default_sources

        for category in self.SOURCES_CATEGORIES:
            if category not in data:
                # Missing category: add with default entries
                data[category] = defaults.get(category, [])
                modified = True
            elif not isinstance(data[category], list):
                # Category value is not a JSON array: replace with defaults
                data[category] = defaults.get(category, [])
                modified = True

        if not modified:
            # Nothing to repair
            return RepairAction(
                file_path=entry.relative_path,
                repair_type="restored-from-default",
                success=True,
            )

        # Write the merged data back
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            logger.info(
                "Repaired sources.json: added missing categories or fixed "
                "non-array values: %s",
                entry.relative_path,
            )
            return RepairAction(
                file_path=entry.relative_path,
                repair_type="restored-from-default",
                success=True,
            )
        except OSError as e:
            error_msg = f"Failed to write repaired sources.json: {e}"
            logger.error(error_msg)
            return RepairAction(
                file_path=entry.relative_path,
                repair_type="restored-from-default",
                success=False,
                error=error_msg,
            )

    def validate_json(self, path: Path) -> bool:
        """Check if file contains valid JSON.

        Args:
            path: Path to the JSON file to validate.

        Returns:
            True if the file contains parseable JSON, False otherwise.
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)
            return True
        except (json.JSONDecodeError, ValueError):
            return False
        except OSError:
            return False

    def validate_markdown(self, path: Path) -> bool:
        """Check if markdown file has non-zero size with non-whitespace content.

        Args:
            path: Path to the markdown file to validate.

        Returns:
            True if the file has non-zero size and contains non-whitespace content.
        """
        try:
            if not path.exists():
                return False
            if path.stat().st_size == 0:
                return False
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            return len(content.strip()) > 0
        except OSError:
            return False

    def validate_directory(self, path: Path) -> bool:
        """Check if path exists as a directory.

        Args:
            path: Path to validate as a directory.

        Returns:
            True if the path exists and is a directory.
        """
        try:
            return path.is_dir()
        except OSError:
            return False

    def recreate_from_default(self, entry: ManifestEntry) -> RepairAction:
        """Recreate file with embedded default content.

        Creates parent directories if needed, then writes the default content
        from the manifest entry to the file path.

        Args:
            entry: The manifest entry containing the file path and default content.

        Returns:
            RepairAction indicating success or failure of the recreation.
        """
        full_path = self._base_for(entry) / entry.relative_path

        try:
            # Create parent directories if they don't exist
            full_path.parent.mkdir(parents=True, exist_ok=True)

            if entry.file_type == "directory":
                full_path.mkdir(parents=True, exist_ok=True)
            else:
                content = entry.default_content or ""
                full_path.write_text(content, encoding="utf-8")

            logger.info(
                "Recreated file from defaults: %s", entry.relative_path
            )
            return RepairAction(
                file_path=entry.relative_path,
                repair_type="created",
                success=True,
            )
        except OSError as e:
            error_msg = f"Failed to recreate {entry.relative_path}: {e}"
            logger.error(error_msg)
            return RepairAction(
                file_path=entry.relative_path,
                repair_type="created",
                success=False,
                error=error_msg,
            )

    def backup_and_recreate(self, path: Path, entry: ManifestEntry) -> RepairAction:
        """Rename corrupted file to .bak and create new with defaults.

        If a .bak file already exists, appends a timestamp suffix to avoid
        overwriting existing backups.

        Args:
            path: The path to the corrupted file.
            entry: The manifest entry with default content for recreation.

        Returns:
            RepairAction indicating success or failure.
        """
        try:
            # Determine backup path
            backup_path = path.with_suffix(path.suffix + ".bak")
            if backup_path.exists():
                # Append timestamp suffix to avoid overwriting existing backup
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = path.with_suffix(
                    path.suffix + f".bak.{timestamp}"
                )

            # Rename corrupted file to backup
            path.rename(backup_path)
            logger.info("Backed up corrupted file: %s -> %s", path, backup_path)

            # Create parent directories if needed
            path.parent.mkdir(parents=True, exist_ok=True)

            # Write default content
            content = entry.default_content or ""
            path.write_text(content, encoding="utf-8")

            logger.info(
                "Restored file from defaults after backup: %s",
                entry.relative_path,
            )
            return RepairAction(
                file_path=entry.relative_path,
                repair_type="restored-from-backup",
                success=True,
            )
        except OSError as e:
            error_msg = (
                f"Failed to backup and recreate {entry.relative_path}: {e}"
            )
            logger.error(error_msg)
            return RepairAction(
                file_path=entry.relative_path,
                repair_type="restored-from-backup",
                success=False,
                error=error_msg,
            )

    def _is_system_prompt(self, entry: ManifestEntry) -> bool:
        """Check if the manifest entry corresponds to system-prompt.md.

        Args:
            entry: The manifest entry to check.

        Returns:
            True if the entry is for system-prompt.md.
        """
        return entry.relative_path == "system-prompt.md"

    def _check_system_prompt_readable(self, path: Path) -> tuple[bool, Optional[str]]:
        """Attempt to read system-prompt.md and detect I/O or encoding errors.

        Args:
            path: Path to the system-prompt.md file.

        Returns:
            A tuple of (is_readable, content). If the file cannot be read,
            returns (False, None). If readable, returns (True, content_string).
        """
        try:
            content = path.read_text(encoding="utf-8")
            return (True, content)
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(
                "system-prompt.md is unreadable due to %s: %s",
                type(e).__name__,
                e,
            )
            return (False, None)

    def _check_sources_json(self, path: Path, entry: ManifestEntry) -> Optional[RepairAction]:
        """Check sources.json for missing categories or non-array values.

        Called after JSON is confirmed valid. Checks whether any expected
        target type categories are missing or have non-array values, and
        triggers repair if needed.

        Args:
            path: Path to the sources.json file.
            entry: The manifest entry for sources.json.

        Returns:
            RepairAction if repair was needed, None if sources.json is healthy.
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Should not happen since validate_json passed, but handle gracefully
            return self.backup_and_recreate(path, entry)

        if not isinstance(data, dict):
            return self.backup_and_recreate(path, entry)

        # Check if any category is missing or has a non-array value
        needs_repair = False
        for category in self.SOURCES_CATEGORIES:
            if category not in data or not isinstance(data[category], list):
                needs_repair = True
                break

        if needs_repair:
            logger.info(
                "sources.json has missing or invalid categories, repairing: %s",
                entry.relative_path,
            )
            return self.repair_sources_json(path, entry)

        return None

    def check_and_repair(self, entry: ManifestEntry) -> Optional[RepairAction]:
        """Orchestrate detection and repair for a single manifest entry.

        Checks file existence and structural validity, applying the appropriate
        repair action when issues are detected:
        - Missing file/directory: recreate from defaults
        - Invalid JSON: backup and recreate
        - Zero-byte or whitespace-only markdown: recreate from defaults
        - system-prompt.md unreadable (I/O or encoding error): backup and recreate
        - Valid file: no action needed (returns None)

        Args:
            entry: The manifest entry to check and potentially repair.

        Returns:
            A RepairAction if repair was needed, or None if the file is healthy.
        """
        full_path = self._base_for(entry) / entry.relative_path

        try:
            # Check existence
            if not full_path.exists():
                logger.info(
                    "File missing, recreating: %s", entry.relative_path
                )
                return self.recreate_from_default(entry)

            # Special handling for system-prompt.md (Requirement 10.3)
            if self._is_system_prompt(entry):
                readable, content = self._check_system_prompt_readable(full_path)
                if not readable:
                    # File exists but cannot be read - backup and recreate
                    logger.error(
                        "system-prompt.md is unreadable, backing up and "
                        "recreating: %s",
                        entry.relative_path,
                    )
                    return self.backup_and_recreate(full_path, entry)
                # File is readable - check for zero-byte or whitespace-only
                if content is not None and len(content.strip()) == 0:
                    logger.info(
                        "system-prompt.md has no meaningful content "
                        "(empty or whitespace-only), recreating: %s",
                        entry.relative_path,
                    )
                    return self.recreate_from_default(entry)
                # system-prompt.md is healthy
                return None

            # Validate based on file type
            if entry.file_type == "directory":
                if not self.validate_directory(full_path):
                    logger.info(
                        "Path is not a directory, recreating: %s",
                        entry.relative_path,
                    )
                    return self.recreate_from_default(entry)

            elif entry.file_type == "json":
                if not self.validate_json(full_path):
                    logger.info(
                        "Invalid JSON detected, backing up and recreating: %s",
                        entry.relative_path,
                    )
                    return self.backup_and_recreate(full_path, entry)

                # For sources.json, perform additional structural checks
                if entry.relative_path == "sources.json":
                    return self._check_sources_json(full_path, entry)

            elif entry.file_type == "markdown":
                if not self.validate_markdown(full_path):
                    logger.info(
                        "Invalid markdown (empty or whitespace-only), "
                        "recreating: %s",
                        entry.relative_path,
                    )
                    return self.recreate_from_default(entry)

        except OSError as e:
            error_msg = (
                f"Error checking {entry.relative_path}: {e}"
            )
            logger.error(error_msg)
            return RepairAction(
                file_path=entry.relative_path,
                repair_type="created",
                success=False,
                error=error_msg,
            )

        # File is healthy
        return None

    def ensure_reports_directory(
        self, config_path: Path, data_root: Path
    ) -> Optional[RepairAction]:
        """Ensure the reports output directory exists, with fallback.

        Reads the reporting.output_dir value from config.json, resolves it
        relative to data_root, and creates the full directory path including
        intermediates. If creation fails (empty value, illegal characters,
        permissions), falls back to creating a 'reports' directory in data_root.

        Args:
            config_path: Path to the config.json file.
            data_root: The external per-user data directory reports live under.

        Returns:
            A RepairAction if a directory was created (or fallback used),
            or None if the directory already existed.
        """
        output_dir_value = "reports"  # default fallback value

        # Step 1: Read config.json and extract reporting.output_dir
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            raw_value = config.get("reporting", {}).get("output_dir", "")
            if raw_value and isinstance(raw_value, str) and raw_value.strip():
                output_dir_value = raw_value.strip()
        except (json.JSONDecodeError, OSError, TypeError):
            # If config can't be read, use default
            pass

        # Step 2: Resolve relative to data_root
        target_path = data_root / output_dir_value

        # Step 3: Attempt to create the directory
        try:
            if target_path.exists() and target_path.is_dir():
                # Directory already exists, no action needed
                return None

            target_path.mkdir(parents=True, exist_ok=True)
            logger.info(
                "Created reports directory: %s", target_path
            )
            return RepairAction(
                file_path=str(target_path.relative_to(data_root)),
                repair_type="created",
                success=True,
            )
        except (OSError, ValueError) as e:
            # Step 4: Fallback to 'reports' directory in data_root
            fallback_path = data_root / "reports"
            logger.warning(
                "Failed to create configured reports directory '%s' (%s). "
                "Falling back to '%s'.",
                output_dir_value,
                e,
                fallback_path,
            )

            try:
                if fallback_path.exists() and fallback_path.is_dir():
                    # Fallback already exists — still report a repair action
                    # since we had to fall back from the configured path
                    return RepairAction(
                        file_path="reports",
                        repair_type="created",
                        success=True,
                        error=f"Fallback used: configured path '{output_dir_value}' failed ({e})",
                    )

                fallback_path.mkdir(parents=True, exist_ok=True)
                return RepairAction(
                    file_path="reports",
                    repair_type="created",
                    success=True,
                    error=f"Fallback used: configured path '{output_dir_value}' failed ({e})",
                )
            except OSError as fallback_error:
                error_msg = (
                    f"Failed to create both configured reports directory "
                    f"'{output_dir_value}' and fallback 'reports': {fallback_error}"
                )
                logger.error(error_msg)
                return RepairAction(
                    file_path="reports",
                    repair_type="created",
                    success=False,
                    error=error_msg,
                )
