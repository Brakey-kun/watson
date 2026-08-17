"""Integrity Engine module for the self-healing portable application.

Top-level orchestrator that runs at startup to verify all critical files,
dependencies, and configuration. Delegates file checking to FileGuardian,
dependency checking to DependencyManager, and routing logic to SetupRouter.
Produces a HealthReport summarizing all repair actions.

Requirements: 1.1, 1.5, 1.6, 1.7, 8.1, 8.4, 11.1, 11.2, 11.3, 11.4, 12.1
"""

import logging
from pathlib import Path
from typing import Optional

from osint_workbench.core.dependency_manager import (
    DependencyManager,
    RequirementsFileError,
)
from osint_workbench.core.file_guardian import FileGuardian
from osint_workbench.core.manifest import (
    MANIFEST,
    HealthReport,
    ManifestEntry,
    RepairAction,
    validate_manifest_entry,
)
from osint_workbench.core.path_utils import normalize_path
from osint_workbench.core.setup_router import SetupRouter

logger = logging.getLogger(__name__)


def resolve_portable_root() -> Path:
    """Resolve the Portable_Root as the parent directory of main.py.

    Uses the location of the top-level main.py file (two levels up from
    this module's location in osint_workbench/core/) rather than the
    current working directory.

    Returns:
        The resolved Portable_Root path.
    """
    # This module is at osint_workbench/core/integrity.py
    # Portable_Root is the project root containing main.py
    # i.e., parent of osint_workbench/ which is parent of core/
    return Path(__file__).resolve().parent.parent.parent


class IntegrityEngine:
    """Orchestrator for all startup health checks.

    Runs the embedded manifest against the file system via FileGuardian,
    verifies dependencies via DependencyManager, and routes to setup
    wizard via SetupRouter when user data needs filling.
    """

    def __init__(self, portable_root: Path, data_root: Optional[Path] = None) -> None:
        """Initialize the IntegrityEngine.

        Args:
            portable_root: The root directory of the portable application.
                App-asset manifest entries (root="app") resolve relative to
                this directory.
            data_root: The external per-user data directory. User-data
                manifest entries (root="data", e.g. config.json, reports/)
                resolve here instead. Defaults to portable_root when omitted,
                so single-root callers (most existing tests) are unaffected.
        """
        self.portable_root = portable_root
        self.data_root = data_root if data_root is not None else portable_root
        self.manifest = MANIFEST
        self.repairs: list[RepairAction] = []
        self.errors: list[str] = []

    def run_checks(self) -> HealthReport:
        """Run all integrity checks and return a consolidated health report.

        Orchestrates the following sequence:
          1. Validate manifest entries (skip malformed ones with logging)
          2. Iterate all valid manifest entries calling FileGuardian.check_and_repair()
          3. Call DependencyManager.verify_all()
          4. Call FileGuardian.ensure_reports_directory()
          5. Build and return HealthReport

        If any repaired file has has_user_data=True, sets setup_completed=false
        via SetupRouter.

        Returns:
            A HealthReport summarizing all repairs and failures.
        """
        file_guardian = FileGuardian(self.portable_root, self.data_root)
        config_path = self.data_root / "config.json"

        # --- Step 1 & 2: Validate manifest entries and check/repair files ---
        valid_entries = self._get_valid_entries()

        for entry in valid_entries:
            self._check_file(file_guardian, entry)

        # --- Step 3: Verify dependencies ---
        self._verify_dependencies()

        # --- Step 4: Ensure reports directory ---
        self._ensure_reports_directory(file_guardian, config_path)

        # --- Step 5: Build health report ---
        health_report = self._build_health_report(valid_entries)

        # --- If any repaired file has user data, mark setup incomplete ---
        if self._has_repaired_user_data_file(valid_entries):
            self._mark_setup_incomplete(config_path)
            health_report.needs_user_attention = True

        # --- Emit health report to application logs ---
        self._emit_health_report(health_report)

        return health_report

    def _get_valid_entries(self) -> list[ManifestEntry]:
        """Validate all manifest entries and return only valid ones.

        Malformed entries are logged as errors and skipped.

        Returns:
            List of valid ManifestEntry objects.
        """
        valid_entries: list[ManifestEntry] = []

        for entry in self.manifest:
            if validate_manifest_entry(entry):
                valid_entries.append(entry)
            else:
                error_msg = (
                    f"Malformed manifest entry skipped: "
                    f"relative_path={entry.relative_path!r}, "
                    f"file_type={entry.file_type!r}, "
                    f"has_user_data={entry.has_user_data!r}"
                )
                logger.error(error_msg)
                self.errors.append(error_msg)

        return valid_entries

    def _check_file(
        self, file_guardian: FileGuardian, entry: ManifestEntry
    ) -> None:
        """Check a single manifest entry and record repair if needed.

        Wraps FileGuardian.check_and_repair() with error handling to ensure
        failures don't halt remaining checks (fail-soft, Requirement 1.6).

        Args:
            file_guardian: The FileGuardian instance to delegate to.
            entry: The manifest entry to check.
        """
        try:
            repair_action = file_guardian.check_and_repair(entry)
            if repair_action is not None:
                self.repairs.append(repair_action)
        except Exception as e:
            # Fail-soft: log the error and continue with remaining files
            error_msg = (
                f"Unexpected error checking {entry.relative_path}: {e}"
            )
            logger.error(error_msg)
            self.errors.append(error_msg)
            self.repairs.append(
                RepairAction(
                    file_path=entry.relative_path,
                    repair_type="created",
                    success=False,
                    error=error_msg,
                )
            )

    def _verify_dependencies(self) -> None:
        """Verify all Python dependencies via DependencyManager.

        If requirements.txt is missing or unparseable, logs a critical error.
        Individual package failures are recorded as repair actions.
        """
        venv_path = self.portable_root / ".venv"
        requirements_path = self.portable_root / "requirements.txt"

        dependency_manager = DependencyManager(venv_path, requirements_path)

        try:
            dep_repairs = dependency_manager.verify_all()
            self.repairs.extend(dep_repairs)
        except RequirementsFileError as e:
            error_msg = f"Critical dependency error: {e}"
            logger.critical(error_msg)
            self.errors.append(error_msg)
            self.repairs.append(
                RepairAction(
                    file_path="requirements.txt",
                    repair_type="created",
                    success=False,
                    error=error_msg,
                )
            )

    def _ensure_reports_directory(
        self, file_guardian: FileGuardian, config_path: Path
    ) -> None:
        """Ensure the reports output directory exists.

        Delegates to FileGuardian.ensure_reports_directory() with fallback logic.

        Args:
            file_guardian: The FileGuardian instance to delegate to.
            config_path: Path to the config.json file.
        """
        try:
            repair_action = file_guardian.ensure_reports_directory(
                config_path, self.data_root
            )
            if repair_action is not None:
                self.repairs.append(repair_action)
        except Exception as e:
            error_msg = f"Failed to ensure reports directory: {e}"
            logger.error(error_msg)
            self.errors.append(error_msg)
            self.repairs.append(
                RepairAction(
                    file_path="reports",
                    repair_type="created",
                    success=False,
                    error=error_msg,
                )
            )

    def _build_health_report(
        self, valid_entries: list[ManifestEntry]
    ) -> HealthReport:
        """Build a consolidated HealthReport from all collected repair actions.

        Args:
            valid_entries: The valid manifest entries that were checked.

        Returns:
            A HealthReport with repairs, failures, and health status.
        """
        successful_repairs = [r for r in self.repairs if r.success]
        failed_repairs = [r for r in self.repairs if not r.success]

        all_healthy = len(self.repairs) == 0 and len(self.errors) == 0

        return HealthReport(
            repairs=successful_repairs,
            failures=failed_repairs,
            needs_user_attention=False,  # Set later if user data files were repaired
            all_healthy=all_healthy,
        )

    def _has_repaired_user_data_file(
        self, valid_entries: list[ManifestEntry]
    ) -> bool:
        """Check if any successfully repaired file has has_user_data=True.

        Args:
            valid_entries: The valid manifest entries that were checked.

        Returns:
            True if any repaired file contains user data fields.
        """
        # Build a set of relative paths that have user data
        user_data_paths = {
            entry.relative_path
            for entry in valid_entries
            if entry.has_user_data
        }

        # Check if any successful repair is for a user data file
        for repair in self.repairs:
            if repair.success and repair.file_path in user_data_paths:
                return True

        return False

    def _mark_setup_incomplete(self, config_path: Path) -> None:
        """Mark setup as incomplete via SetupRouter.

        Called when a file containing User_Data_Fields has been repaired,
        requiring the user to revisit the setup wizard.

        Args:
            config_path: Path to the config.json file.
        """
        try:
            setup_router = SetupRouter(config_path)
            setup_router.mark_setup_incomplete()
            logger.info(
                "Setup marked incomplete: repaired file(s) contain "
                "user data fields requiring configuration."
            )
        except Exception as e:
            error_msg = f"Failed to mark setup as incomplete: {e}"
            logger.error(error_msg)
            self.errors.append(error_msg)

    def _emit_health_report(self, health_report: HealthReport) -> None:
        """Emit the health report to application logs.

        Handles different cases with appropriate log levels:
        - Requirement 11.2: INFO when all healthy (single confirmation)
        - Requirement 11.1: WARNING when repairs were performed
        - Requirement 11.3: WARNING notice for setup wizard
        - Requirement 11.4: ERROR for failed repairs

        Args:
            health_report: The completed health report to log.
        """
        if health_report.all_healthy:
            logger.info(health_report.to_log_string())
            return

        # Log each line of the report at the appropriate level
        log_output = health_report.to_log_string()
        for line in log_output.splitlines():
            if not line.strip():
                continue
            if line.startswith("FAILED"):
                logger.error(line)
            else:
                logger.warning(line)

    def normalize_config_path(self, path_str: str) -> Path:
        """Normalize a path string read from a configuration file.

        Converts all path separators to the host OS native separator before
        using the path in file system operations. Logs a warning if the
        resolved path does not exist.

        This method should be called on any path value read from config.json
        before performing file system operations with it.

        Args:
            path_str: A path string from a config file, potentially using
                non-native separators (e.g., backslashes on Unix or forward
                slashes on Windows).

        Returns:
            A Path object with separators normalized to the host OS format,
            resolved relative to the portable_root for existence checking.

        Requirements: 8.2, 8.3, 8.5
        """
        return normalize_path(path_str, base_dir=self.portable_root)
