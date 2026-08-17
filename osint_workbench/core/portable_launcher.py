"""Portable launcher utilities for relocation detection and marker management.

Provides the PortableLauncher class that handles detecting when the application
has been moved to a new location by comparing the current path against a stored
marker file.
"""

from pathlib import Path


class PortableLauncher:
    """Handles portable application relocation detection and environment setup."""

    MARKER_FILE = ".venv/portable_root_marker"

    @staticmethod
    def detect_relocation(portable_root: Path) -> bool:
        """Compare current path against stored marker to detect relocation.

        Returns True if:
        - The marker file does not exist (new install scenario)
        - The stored path differs from the current portable_root

        Returns False if the stored path matches the current portable_root.
        """
        marker_path = portable_root / PortableLauncher.MARKER_FILE
        if not marker_path.exists():
            return True

        try:
            stored_path = marker_path.read_text(encoding="utf-8").strip()
        except (OSError, IOError):
            return True

        return stored_path != str(portable_root)

    @staticmethod
    def write_marker(portable_root: Path) -> None:
        """Write current absolute path to marker file for future relocation detection."""
        marker_path = portable_root / PortableLauncher.MARKER_FILE
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(str(portable_root), encoding="utf-8")

    @staticmethod
    def get_python_binary_path(portable_root: Path, platform_name: str) -> Path:
        """Select the correct Python binary path based on the operating system.

        Args:
            portable_root: The root directory containing the .venv.
            platform_name: The OS identifier as returned by platform.system()
                           (e.g., "Windows", "Linux", "Darwin").

        Returns:
            Path to the Python binary within the virtual environment.
            On Windows: .venv/Scripts/python.exe
            On Unix-like (Linux, macOS/Darwin): .venv/bin/python
        """
        if platform_name == "Windows":
            return portable_root / ".venv" / "Scripts" / "python.exe"
        else:
            # Linux, Darwin (macOS), or any other Unix-like system
            return portable_root / ".venv" / "bin" / "python"
