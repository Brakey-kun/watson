"""Dependency Manager module for the self-healing integrity engine.

Verifies and repairs Python package dependencies by parsing requirements.txt,
checking installed versions against constraints, and installing/reinstalling
packages via pip subprocess calls with retry logic.
"""

import importlib
import importlib.metadata
import logging
import re
import subprocess
import sys
from pathlib import Path

from packaging.version import Version

from osint_workbench.core.manifest import PackageRequirement, PackageStatus, RepairAction

logger = logging.getLogger(__name__)


class RequirementsFileError(Exception):
    """Raised when requirements.txt is missing or cannot be parsed.

    This signals that the application startup should halt.
    """

    pass


class DependencyManager:
    """Verifies and repairs Python package dependencies.

    Parses requirements.txt, checks installed packages against version
    constraints, and installs/reinstalls packages using pip with retry logic.
    """

    def __init__(self, venv_path: Path, requirements_path: Path) -> None:
        self.venv_path = venv_path
        self.requirements_path = requirements_path
        self.max_retries = 3
        self.install_timeout = 120  # seconds per package

    def parse_requirements(self) -> list[PackageRequirement]:
        """Parse requirements.txt into structured requirements.

        Handles comments (# lines), blank lines, and -r includes.
        Raises RequirementsFileError if the file is missing or unparseable.
        """
        if not self.requirements_path.exists():
            msg = (
                f"requirements.txt not found at {self.requirements_path}. "
                "Cannot verify dependencies. Please restore or recreate the file."
            )
            logger.critical(msg)
            raise RequirementsFileError(msg)

        try:
            content = self.requirements_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            msg = (
                f"Cannot read requirements.txt at {self.requirements_path}: {e}. "
                "Please restore or recreate the file."
            )
            logger.critical(msg)
            raise RequirementsFileError(msg) from e

        requirements: list[PackageRequirement] = []
        # Pattern to match package lines like: package==1.0, package>=1.0, package~=1.0
        # Also handles bare package names without version constraints
        req_pattern = re.compile(
            r"^([A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)"
            r"(([><=!~]=?[^,;\s]+)(,[><=!~]=?[^,;\s]+)*)?\s*$"
        )

        for line_num, raw_line in enumerate(content.splitlines(), start=1):
            line = raw_line.strip()

            # Skip blank lines and comments
            if not line or line.startswith("#"):
                continue

            # Skip -r includes (recursive requirements files)
            if line.startswith("-r ") or line.startswith("--requirement"):
                continue

            # Skip options like --index-url, -i, --extra-index-url, etc.
            if line.startswith("-"):
                continue

            match = req_pattern.match(line)
            if match:
                name = match.group(1)
                version_spec = match.group(3) or ""
                requirements.append(PackageRequirement(name=name, version_spec=version_spec))
            else:
                logger.warning(
                    f"Skipping unparseable requirement on line {line_num}: {raw_line!r}"
                )

        return requirements

    def check_package(self, req: PackageRequirement) -> PackageStatus:
        """Check if a single package is installed at the correct version.

        Uses importlib.metadata to check installed version and packaging.version
        for version comparison against the constraint.
        """
        try:
            installed_version = importlib.metadata.version(req.name)
        except importlib.metadata.PackageNotFoundError:
            return PackageStatus(
                name=req.name,
                installed=False,
                installed_version=None,
                satisfies_constraint=False,
            )

        if not req.version_spec:
            # No version constraint specified — any installed version is acceptable
            return PackageStatus(
                name=req.name,
                installed=True,
                installed_version=installed_version,
                satisfies_constraint=True,
            )

        satisfies = self._version_satisfies(installed_version, req.version_spec)
        return PackageStatus(
            name=req.name,
            installed=True,
            installed_version=installed_version,
            satisfies_constraint=satisfies,
        )

    def install_package(self, req: PackageRequirement, force: bool = False) -> bool:
        """Install or reinstall a package using pip.

        Args:
            req: The package requirement to install.
            force: If True, force-reinstall the package.

        Returns:
            True if the installation succeeded, False otherwise.
        """
        pip_executable = self._get_pip_path()
        cmd = [str(pip_executable), "install"]

        if force:
            cmd.append("--force-reinstall")

        # Build the package specifier
        package_spec = req.name + req.version_spec if req.version_spec else req.name
        cmd.append(package_spec)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.install_timeout,
            )
            if result.returncode == 0:
                logger.info(f"Successfully installed {package_spec}")
                return True
            else:
                logger.error(
                    f"pip install failed for {package_spec}: {result.stderr.strip()}"
                )
                return False
        except subprocess.TimeoutExpired:
            logger.error(
                f"pip install timed out for {package_spec} "
                f"(timeout={self.install_timeout}s)"
            )
            return False
        except OSError as e:
            logger.error(f"Failed to run pip for {package_spec}: {e}")
            return False

    def verify_all(self) -> list[RepairAction]:
        """Verify all packages and install/upgrade as needed.

        Iterates all requirements from requirements.txt, checks each package,
        and attempts installation with retry logic for any that are missing
        or have version mismatches.

        Returns:
            A list of RepairAction entries for each package that needed attention.
        """
        try:
            requirements = self.parse_requirements()
        except RequirementsFileError:
            # Re-raise to signal startup halt — the caller must handle this
            raise

        repair_actions: list[RepairAction] = []

        for req in requirements:
            status = self.check_package(req)

            if status.installed and status.satisfies_constraint:
                continue

            # Package needs installation or upgrade
            repair_type = "installed" if not status.installed else "upgraded"
            package_spec = req.name + req.version_spec if req.version_spec else req.name

            success = False
            last_error: str | None = None

            # Use force when the package is installed but at wrong version
            use_force = status.installed and not status.satisfies_constraint

            for attempt in range(1, self.max_retries + 1):
                logger.info(
                    f"Attempting to install {package_spec} "
                    f"(attempt {attempt}/{self.max_retries})"
                )
                if self.install_package(req, force=use_force):
                    success = True
                    break
                else:
                    last_error = (
                        f"Installation attempt {attempt}/{self.max_retries} "
                        f"failed for {package_spec}"
                    )
                    logger.warning(last_error)

            if success:
                repair_actions.append(
                    RepairAction(
                        file_path=req.name,
                        repair_type=repair_type,
                        success=True,
                    )
                )
            else:
                manual_cmd = f"pip install {package_spec}"
                error_msg = (
                    f"Failed to install {package_spec} after {self.max_retries} attempts. "
                    f"Last error: {last_error}. "
                    f"Suggested manual command: {manual_cmd}"
                )
                logger.error(error_msg)
                repair_actions.append(
                    RepairAction(
                        file_path=req.name,
                        repair_type=repair_type,
                        success=False,
                        error=error_msg,
                    )
                )

        return repair_actions

    def handle_import_failure(self, package_name: str) -> bool:
        """Force-reinstall and retry import for corrupted packages.

        Called at runtime when an ImportError or ModuleNotFoundError is caught.
        Looks up the requirement for the package, force-reinstalls it, then
        retries the import once.

        Args:
            package_name: The name of the package that failed to import.

        Returns:
            True if the package was successfully reinstalled and imported,
            False otherwise.
        """
        logger.info(f"Handling import failure for package: {package_name}")

        # Find the requirement for this package
        req = self._find_requirement(package_name)
        if req is None:
            # Not found in requirements — create a bare requirement
            req = PackageRequirement(name=package_name, version_spec="")

        # Force-reinstall the package
        success = self.install_package(req, force=True)
        if not success:
            manual_cmd = f"pip install --force-reinstall {package_name}"
            logger.error(
                f"Failed to reinstall {package_name}. "
                f"Suggested manual command: {manual_cmd}"
            )
            return False

        # Retry the import
        try:
            # Invalidate import caches before retrying
            importlib.invalidate_caches()
            if package_name in sys.modules:
                del sys.modules[package_name]
            importlib.import_module(package_name)
            logger.info(f"Successfully re-imported {package_name} after reinstall")
            return True
        except (ImportError, ModuleNotFoundError) as e:
            manual_cmd = f"pip install --force-reinstall {package_name}"
            logger.error(
                f"Import of {package_name} still fails after reinstall: {e}. "
                f"Suggested manual command: {manual_cmd}"
            )
            return False

    def _find_requirement(self, package_name: str) -> PackageRequirement | None:
        """Find a PackageRequirement by package name from requirements.txt."""
        try:
            requirements = self.parse_requirements()
        except RequirementsFileError:
            return None

        # Normalize for comparison (pip treats - and _ as equivalent)
        normalized = package_name.lower().replace("-", "_")
        for req in requirements:
            if req.name.lower().replace("-", "_") == normalized:
                return req
        return None

    def _get_pip_path(self) -> Path:
        """Get the path to the pip executable in the virtual environment."""
        if sys.platform == "win32":
            return self.venv_path / "Scripts" / "pip.exe"
        else:
            return self.venv_path / "bin" / "pip"

    @staticmethod
    def _version_satisfies(installed_version: str, version_spec: str) -> bool:
        """Check if an installed version satisfies a version constraint string.

        Supports operators: ==, >=, <=, !=, ~=, >, <
        Supports multiple comma-separated constraints (e.g., ">=1.0,<2.0").
        """
        try:
            installed = Version(installed_version)
        except Exception:
            return False

        # Split on commas for multiple constraints
        constraints = [c.strip() for c in version_spec.split(",") if c.strip()]

        for constraint in constraints:
            # Parse operator and version from constraint
            match = re.match(r"^([><=!~]+)(.+)$", constraint)
            if not match:
                # If no operator, treat as exact match
                try:
                    return installed == Version(constraint)
                except Exception:
                    return False

            operator = match.group(1)
            version_str = match.group(2).strip()

            try:
                required = Version(version_str)
            except Exception:
                return False

            if operator == "==":
                if not (installed == required):
                    return False
            elif operator == ">=":
                if not (installed >= required):
                    return False
            elif operator == "<=":
                if not (installed <= required):
                    return False
            elif operator == "!=":
                if not (installed != required):
                    return False
            elif operator == ">":
                if not (installed > required):
                    return False
            elif operator == "<":
                if not (installed < required):
                    return False
            elif operator == "~=":
                # Compatible release: ~=X.Y means >=X.Y, <(X+1).0
                # ~=X.Y.Z means >=X.Y.Z, <X.(Y+1).0
                if installed < required:
                    return False
                # Calculate upper bound: increment the second-to-last version component
                release = list(required.release)
                if len(release) >= 2:
                    release[-2] += 1
                    release = release[:-1]
                    upper = Version(".".join(str(r) for r in release))
                    if not (installed < upper):
                        return False
                else:
                    # Single-component version: ~=X means >=X (no upper bound meaningful)
                    pass
            else:
                # Unknown operator — cannot validate
                return False

        return True
