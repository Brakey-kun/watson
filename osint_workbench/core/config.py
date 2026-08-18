"""Configuration loading and validation for Watson.

This module provides the ConfigLoader class for loading, validating, and
managing application configuration. It handles auto-generation of missing
config files, recovery from corrupt JSON, merging of defaults for missing
fields, and legacy config migration.
"""

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from osint_workbench.core import paths

from osint_workbench.core.models import (
    AppConfig,
    BackendConfig,
    BurstSearchConfig,
    DoubtSearchConfig,
    FetcherConfig,
    LLMConfig,
    ModelTiers,
    QualityConfig,
    SearchConfig,
    TierModelConfig,
    ValidationError,
)

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """Raised when a configuration value fails validation.

    Attributes:
        field_name: The name of the invalid configuration field.
        provided_value: The value that was provided.
        acceptable_range: A description of the acceptable range.
    """

    def __init__(self, field_name: str, provided_value: Any, acceptable_range: str) -> None:
        self.field_name = field_name
        self.provided_value = provided_value
        self.acceptable_range = acceptable_range
        message = (
            f"Invalid configuration for '{field_name}': "
            f"got {provided_value!r}, expected {acceptable_range}"
        )
        super().__init__(message)


def _validate_range(
    field_name: str,
    value: Any,
    min_val: float,
    max_val: float,
    *,
    exclusive_min: bool = False,
) -> None:
    """Validate that a numeric value falls within the specified range.

    Args:
        field_name: Name of the config field (for error reporting).
        value: The value to validate.
        min_val: Minimum allowed value.
        max_val: Maximum allowed value.
        exclusive_min: If True, the minimum bound is exclusive (value > min_val).

    Raises:
        ConfigurationError: If the value is outside the acceptable range.
    """
    if exclusive_min:
        if value <= min_val:
            acceptable = f"a value > {min_val} and <= {max_val}"
            raise ConfigurationError(field_name, value, acceptable)
    else:
        if value < min_val:
            acceptable = f"a value between {min_val} and {max_val} inclusive"
            raise ConfigurationError(field_name, value, acceptable)

    if value > max_val:
        if exclusive_min:
            acceptable = f"a value > {min_val} and <= {max_val}"
        else:
            acceptable = f"a value between {min_val} and {max_val} inclusive"
        raise ConfigurationError(field_name, value, acceptable)


def _validate_positive(field_name: str, value: Any) -> None:
    """Validate that a numeric value is strictly greater than zero.

    Args:
        field_name: Name of the config field (for error reporting).
        value: The value to validate.

    Raises:
        ConfigurationError: If the value is not positive.
    """
    if value <= 0:
        raise ConfigurationError(field_name, value, "a positive value greater than 0.0")


def _get_default_config() -> dict:
    """Return the default configuration dictionary."""
    return {
        "llm": {
            "backend": "lm_studio",
            "host": "127.0.0.1",
            "port": 1234,
            "model": "",
            "temperature": 0.7,
            "max_context_tokens": 32768,
            "max_retries": 3,
        },
        "backends": {
            "lm_studio": {
                "endpoint": "http://127.0.0.1:1234/v1",
                "api_key": "lm-studio",
                "model": "",
                "temperature": 0.7,
                "last_tested": None,
            }
        },
        "fetcher": {
            "max_workers": 20,
            "timeout_seconds": 10,
            "max_retries": 2,
            "rate_limit_per_second": 5.0,
        },
        "search": {
            "engines": ["google", "bing", "duckduckgo"],
            "rate_limit_per_engine": 2.0,
            "jitter_min": 0.5,
            "jitter_max": 2.0,
        },
        "quality": {
            "min_relevance_score": 0.2,
            "enable_content_dedup": True,
            "noise_patterns": [],
        },
        "reporting": {
            "output_dir": "reports",
            "template_md": "templates/report.md",
            "template_html": "templates/report.html",
        },
        "tiers": {
            "thinker": {"model": "", "temperature": None},
            "default": {"model": "", "temperature": None},
            "small": {"model": "", "temperature": None},
        },
        "burst_search": {
            "enabled": False,
            "probe_count": 3,
        },
        "doubt_search": {
            "enabled": True,
            "max_free_attempts": 1,
            "max_total_attempts": 3,
        },
        "setup_completed": False,
    }


def _deep_merge(defaults: dict, overrides: dict) -> dict:
    """Recursively merge overrides into defaults, preserving existing values.

    For each key in defaults:
      - If the key is present in overrides and both values are dicts, recurse.
      - If the key is present in overrides, use the override value.
      - If the key is absent from overrides, use the default value.

    Keys in overrides not present in defaults are preserved as-is.
    """
    result = defaults.copy()
    for key, value in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class ConfigLoader:
    """Loads, validates, and manages the application configuration file."""

    def __init__(self, config_path: str | None = None) -> None:
        """Initialize the ConfigLoader.

        Args:
            config_path: Path to config.json. Defaults to the external
                per-user data directory (see osint_workbench.core.paths)
                when omitted.
        """
        self.config_path = Path(config_path) if config_path is not None else paths.config_path()

    def load(self) -> AppConfig:
        """Load and validate config. Auto-generates if missing/invalid.

        Handles the following cases:
        - Config file missing: creates with defaults, logs warning
        - Config file invalid JSON: renames to .bak, creates new with defaults, logs warning
        - Valid JSON with missing fields: merges with defaults preserving existing values
        - Legacy config (llm section but no backends): migrates to backends format

        Returns:
            A fully populated and validated AppConfig instance.
        """
        defaults = _get_default_config()
        raw = self._read_or_create(defaults)

        # Legacy migration: if config has llm but no backends, synthesize from flat llm fields
        raw = self._migrate_legacy(raw)

        # Merge with defaults to fill in any missing fields
        merged = _deep_merge(defaults, raw)

        # Normalize unrecognized llm.backend to lm_studio
        recognized_backends = {"lm_studio", "antigravity_api"}
        llm_data = merged.get("llm", {})
        backend_value = llm_data.get("backend", "lm_studio")
        if backend_value not in recognized_backends:
            logger.warning(
                "Unrecognized llm.backend value '%s', falling back to 'lm_studio'.",
                backend_value,
            )
            merged["llm"]["backend"] = "lm_studio"

        # Store raw backends data for use by get_valid_backends
        self._raw_backends = merged.get("backends", {})

        # Build AppConfig from merged data
        config = self._build_app_config(merged)

        # Validate numeric ranges
        self._validate_config(config)

        # Store the loaded config for use by get_valid_backends
        self._config = config

        return config

    def save(self, config_data: dict) -> None:
        """Persist config data to disk atomically.

        Writes data to a temporary file in the same directory as config_path,
        then renames it to the actual config path. This prevents partial writes
        if the process crashes mid-write.

        Args:
            config_data: The configuration dictionary to persist.
        """
        import os
        import tempfile

        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(config_data, indent=2) + "\n"

        # Write to a temp file in the same directory, then rename for atomicity
        dir_path = self.config_path.parent
        fd, tmp_path = tempfile.mkstemp(
            suffix=".tmp", prefix="config_", dir=str(dir_path)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                tmp_file.write(content)
            # On Windows, os.replace handles overwriting the target atomically
            os.replace(tmp_path, str(self.config_path))
        except Exception:
            # Clean up the temp file if rename failed
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def validate_backend(self, name: str, backend: dict) -> list[ValidationError]:
        """Validate a single backend entry. Returns list of errors (empty = valid).

        Checks that:
        - endpoint: is a non-empty string
        - api_key: is a string (may be empty)
        - model: is a non-empty string with max 128 characters
        - temperature: is a float/int in [0.0, 2.0]

        Args:
            name: The backend name (used in error reporting).
            backend: A dictionary with backend configuration fields.

        Returns:
            A list of ValidationError objects. Empty list means the backend is valid.
        """
        errors: list[ValidationError] = []

        # Validate endpoint: required, non-empty string
        if "endpoint" not in backend:
            errors.append(ValidationError(
                backend_name=name,
                field_name="endpoint",
                issue="missing",
                detail="Field 'endpoint' is required but not present.",
            ))
        elif not isinstance(backend["endpoint"], str):
            errors.append(ValidationError(
                backend_name=name,
                field_name="endpoint",
                issue="wrong_type",
                detail=f"Field 'endpoint' must be a string, got {type(backend['endpoint']).__name__}.",
            ))
        elif not backend["endpoint"]:
            errors.append(ValidationError(
                backend_name=name,
                field_name="endpoint",
                issue="missing",
                detail="Field 'endpoint' must be a non-empty string.",
            ))

        # Validate api_key: required, must be a string (may be empty)
        if "api_key" not in backend:
            errors.append(ValidationError(
                backend_name=name,
                field_name="api_key",
                issue="missing",
                detail="Field 'api_key' is required but not present.",
            ))
        elif not isinstance(backend["api_key"], str):
            errors.append(ValidationError(
                backend_name=name,
                field_name="api_key",
                issue="wrong_type",
                detail=f"Field 'api_key' must be a string, got {type(backend['api_key']).__name__}.",
            ))

        # Validate model: required, non-empty string, max 128 characters
        if "model" not in backend:
            errors.append(ValidationError(
                backend_name=name,
                field_name="model",
                issue="missing",
                detail="Field 'model' is required but not present.",
            ))
        elif not isinstance(backend["model"], str):
            errors.append(ValidationError(
                backend_name=name,
                field_name="model",
                issue="wrong_type",
                detail=f"Field 'model' must be a string, got {type(backend['model']).__name__}.",
            ))
        elif not backend["model"]:
            errors.append(ValidationError(
                backend_name=name,
                field_name="model",
                issue="missing",
                detail="Field 'model' must be a non-empty string.",
            ))
        elif len(backend["model"]) > 128:
            errors.append(ValidationError(
                backend_name=name,
                field_name="model",
                issue="out_of_range",
                detail=f"Field 'model' must be at most 128 characters, got {len(backend['model'])}.",
            ))

        # Validate temperature: required, numeric (int or float), in [0.0, 2.0]
        if "temperature" not in backend:
            errors.append(ValidationError(
                backend_name=name,
                field_name="temperature",
                issue="missing",
                detail="Field 'temperature' is required but not present.",
            ))
        elif not isinstance(backend["temperature"], (int, float)) or isinstance(backend["temperature"], bool):
            errors.append(ValidationError(
                backend_name=name,
                field_name="temperature",
                issue="wrong_type",
                detail=f"Field 'temperature' must be a number, got {type(backend['temperature']).__name__}.",
            ))
        elif not (0.0 <= float(backend["temperature"]) <= 2.0):
            errors.append(ValidationError(
                backend_name=name,
                field_name="temperature",
                issue="out_of_range",
                detail=f"Field 'temperature' must be between 0.0 and 2.0, got {backend['temperature']}.",
            ))

        return errors

    def get_valid_backends(self) -> dict[str, BackendConfig]:
        """Return only backends that pass validation.

        Uses the raw backends data stored during load() to validate each backend.
        Invalid backends are excluded and a warning is logged for each.

        Returns:
            A dictionary mapping backend names to BackendConfig objects,
            containing only those backends that pass all validation checks.
        """
        raw_backends = getattr(self, "_raw_backends", {})
        valid_backends: dict[str, BackendConfig] = {}

        for name, backend_data in raw_backends.items():
            if not isinstance(backend_data, dict):
                logger.warning(
                    "Backend '%s' is not a valid dictionary, skipping.",
                    name,
                )
                continue

            errors = self.validate_backend(name, backend_data)
            if errors:
                for error in errors:
                    logger.warning(
                        "Backend '%s' failed validation: field '%s' - %s (%s)",
                        error.backend_name,
                        error.field_name,
                        error.issue,
                        error.detail,
                    )
            else:
                valid_backends[name] = BackendConfig(
                    endpoint=backend_data.get("endpoint", "http://127.0.0.1:1234/v1"),
                    api_key=backend_data.get("api_key", "lm-studio"),
                    model=backend_data.get("model", ""),
                    temperature=float(backend_data.get("temperature", 0.7)),
                    last_tested=backend_data.get("last_tested", None),
                )

        return valid_backends


    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _read_or_create(self, defaults: dict) -> dict:
        """Read config from disk, or create with defaults if missing/invalid.

        Args:
            defaults: The default configuration dictionary to write if needed.

        Returns:
            The parsed JSON dictionary from the config file.
        """
        if not self.config_path.exists():
            logger.warning(
                "Config file '%s' not found. Creating with default values.",
                self.config_path,
            )
            self._write_defaults(defaults)
            return {}

        # File exists, try to read and parse
        try:
            content = self.config_path.read_text(encoding="utf-8")
            raw = json.loads(content)
            if not isinstance(raw, dict):
                raise ValueError("Config file root must be a JSON object")
            return raw
        except (json.JSONDecodeError, ValueError) as e:
            # Invalid JSON: rename to .bak and create new with defaults
            bak_path = self.config_path.with_suffix(
                self.config_path.suffix + ".bak"
            )
            logger.warning(
                "Config file '%s' contains invalid JSON (%s). "
                "Renaming to '%s' and creating new file with defaults.",
                self.config_path,
                e,
                bak_path,
            )
            shutil.move(str(self.config_path), str(bak_path))
            self._write_defaults(defaults)
            return {}

    def _write_defaults(self, defaults: dict) -> None:
        """Write the defaults dictionary to the config file."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(defaults, indent=2) + "\n",
            encoding="utf-8",
        )

    def _migrate_legacy(self, raw: dict) -> dict:
        """Migrate legacy flat llm config to backends format.

        If the config has an 'llm' section but no 'backends' section,
        synthesize a 'backends.lm_studio' entry from the flat llm fields.
        """
        if "llm" in raw and "backends" not in raw:
            llm_data = raw["llm"]
            host = llm_data.get("host", "127.0.0.1")
            port = llm_data.get("port", 1234)
            model = llm_data.get("model", "")
            temperature = llm_data.get("temperature", 0.7)

            raw["backends"] = {
                "lm_studio": {
                    "endpoint": f"http://{host}:{port}/v1",
                    "api_key": "lm-studio",
                    "model": model,
                    "temperature": temperature,
                    "last_tested": None,
                }
            }
            logger.info(
                "Migrated legacy llm config to backends format "
                "(synthesized backends.lm_studio from llm.host=%s, llm.port=%s).",
                host,
                port,
            )
        return raw

    def _build_app_config(self, merged: dict) -> AppConfig:
        """Build an AppConfig instance from a merged config dictionary."""
        llm_data = merged.get("llm", {})
        fetcher_data = merged.get("fetcher", {})
        search_data = merged.get("search", {})
        quality_data = merged.get("quality", {})
        backends_data = merged.get("backends", {})
        tiers_data = merged.get("tiers", {})
        burst_data = merged.get("burst_search", {})
        doubt_data = merged.get("doubt_search", {})

        # Build LLMConfig
        llm_config = LLMConfig(
            backend=llm_data.get("backend", "lm_studio"),
            host=llm_data.get("host", "127.0.0.1"),
            port=llm_data.get("port", 1234),
            model=llm_data.get("model", ""),
            temperature=llm_data.get("temperature", 0.7),
            max_context_tokens=llm_data.get("max_context_tokens", 32768),
            max_retries=llm_data.get("max_retries", 3),
        )

        # Build FetcherConfig
        fetcher_config = FetcherConfig(
            max_workers=fetcher_data.get("max_workers", 20),
            timeout_seconds=fetcher_data.get("timeout_seconds", 10),
            max_retries=fetcher_data.get("max_retries", 2),
            rate_limit_per_second=fetcher_data.get("rate_limit_per_second", 5.0),
        )

        # Build SearchConfig
        search_config = SearchConfig(
            engines=search_data.get("engines", ["google", "bing", "duckduckgo"]),
            rate_limit_per_engine=search_data.get("rate_limit_per_engine", 2.0),
            jitter_min=search_data.get("jitter_min", 0.5),
            jitter_max=search_data.get("jitter_max", 2.0),
        )

        # Build QualityConfig
        quality_config = QualityConfig(
            min_relevance_score=quality_data.get("min_relevance_score", 0.2),
            enable_content_dedup=quality_data.get("enable_content_dedup", True),
            noise_patterns=quality_data.get("noise_patterns", []),
        )

        # Build BackendConfig dict
        backends = {}
        for name, backend_data in backends_data.items():
            if isinstance(backend_data, dict):
                backends[name] = BackendConfig(
                    endpoint=backend_data.get("endpoint", "http://127.0.0.1:1234/v1"),
                    api_key=backend_data.get("api_key", "lm-studio"),
                    model=backend_data.get("model", ""),
                    temperature=backend_data.get("temperature", 0.7),
                    last_tested=backend_data.get("last_tested", None),
                )

        # Build ModelTiers (empty model = "use active backend's model")
        def _tier(name: str) -> TierModelConfig:
            t = tiers_data.get(name, {}) if isinstance(tiers_data, dict) else {}
            if not isinstance(t, dict):
                t = {}
            return TierModelConfig(model=t.get("model", ""), temperature=t.get("temperature", None))

        tiers_config = ModelTiers(thinker=_tier("thinker"), default=_tier("default"), small=_tier("small"))

        burst_search_config = BurstSearchConfig(
            enabled=burst_data.get("enabled", False) if isinstance(burst_data, dict) else False,
            probe_count=burst_data.get("probe_count", 3) if isinstance(burst_data, dict) else 3,
        )

        doubt_search_config = DoubtSearchConfig(
            enabled=doubt_data.get("enabled", True) if isinstance(doubt_data, dict) else True,
            max_free_attempts=doubt_data.get("max_free_attempts", 1) if isinstance(doubt_data, dict) else 1,
            max_total_attempts=doubt_data.get("max_total_attempts", 3) if isinstance(doubt_data, dict) else 3,
        )

        return AppConfig(
            llm=llm_config,
            backends=backends,
            fetcher=fetcher_config,
            search=search_config,
            quality=quality_config,
            tiers=tiers_config,
            burst_search=burst_search_config,
            doubt_search=doubt_search_config,
            setup_completed=merged.get("setup_completed", False),
        )

    def _validate_config(self, config: AppConfig) -> None:
        """Validate numeric ranges on the loaded config.

        Raises:
            ConfigurationError: If any value is out of range.
        """
        _validate_range("llm.port", config.llm.port, 1, 65535)
        _validate_range("llm.temperature", config.llm.temperature, 0.0, 2.0)
        _validate_range("fetcher.max_workers", config.fetcher.max_workers, 1, 100)
        _validate_range("fetcher.timeout_seconds", config.fetcher.timeout_seconds, 1, 60)
        _validate_positive("fetcher.rate_limit_per_second", config.fetcher.rate_limit_per_second)
        _validate_range(
            "quality.min_relevance_score", config.quality.min_relevance_score, 0.0, 1.0
        )


# ---------------------------------------------------------------------------
# Backward-compatible wrapper
# ---------------------------------------------------------------------------


def load_config(path: str | None = None) -> AppConfig:
    """Load and validate application configuration from a JSON file.

    This is a backward-compatible wrapper around ConfigLoader.load().
    Existing code that calls load_config(path) will continue to work.

    Args:
        path: File path to the config.json file. Defaults to the external
            per-user data directory when omitted.

    Returns:
        A fully populated and validated AppConfig instance.

    Raises:
        ConfigurationError: If any configuration value fails validation.
    """
    loader = ConfigLoader(config_path=path)
    return loader.load()
