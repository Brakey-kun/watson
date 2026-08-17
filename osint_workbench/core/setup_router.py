"""Setup_Router module for the self-healing integrity engine.

Manages routing between the setup wizard and main dashboard based on
the `setup_completed` flag in config.json. Validates user data fields
and persists configuration when setup is completed.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class SetupRouter:
    """Routes users to setup wizard or main dashboard based on config state.

    Reads and writes the `setup_completed` flag and user data fields in
    config.json to determine whether the setup wizard should be shown.
    """

    def __init__(self, config_path: Path) -> None:
        """Initialize SetupRouter with the path to config.json.

        Args:
            config_path: Path to the config.json file.
        """
        self.config_path = config_path

    def _read_config(self) -> dict:
        """Read and parse config.json, returning empty dict on failure."""
        try:
            content = self.config_path.read_text(encoding="utf-8")
            data = json.loads(content)
            if isinstance(data, dict):
                return data
            logger.warning(
                "config.json does not contain a JSON object, treating as empty config"
            )
            return {}
        except (OSError, json.JSONDecodeError) as e:
            logger.error("Failed to read config.json at %s: %s", self.config_path, e)
            return {}

    def _write_config(self, config: dict) -> bool:
        """Write config dict to config.json.

        Returns True on success, False on failure.
        """
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(
                json.dumps(config, indent=4), encoding="utf-8"
            )
            return True
        except OSError as e:
            logger.error(
                "Failed to write config.json at %s: %s", self.config_path, e
            )
            return False

    def needs_setup(self) -> bool:
        """Check if the setup wizard should be shown.

        Returns True (needs setup) if `setup_completed` is False, absent,
        or config.json cannot be read.
        """
        config = self._read_config()
        return config.get("setup_completed") is not True

    def mark_setup_incomplete(self) -> None:
        """Set setup_completed to false in config.json."""
        config = self._read_config()
        config["setup_completed"] = False
        self._write_config(config)

    def complete_setup(self, user_data: dict[str, str]) -> bool:
        """Validate all fields are non-empty, persist values, and mark complete.

        Validates that all values in user_data are non-empty strings after
        stripping whitespace. If any field is empty, returns False without
        persisting anything. If all fields are valid, merges user_data into
        config.json and sets setup_completed to True.

        Args:
            user_data: Dictionary of field names to string values provided
                       by the user in the setup wizard.

        Returns:
            True if all fields are valid and config was persisted successfully.
            False if any field is empty or persistence fails.
        """
        if not user_data:
            return False

        # Validate all fields are non-empty strings after stripping
        for key, value in user_data.items():
            if not isinstance(value, str) or not value.strip():
                return False

        # Read current config, merge user data, and mark complete
        config = self._read_config()
        config.update(user_data)
        config["setup_completed"] = True

        if self._write_config(config):
            return True

        return False
