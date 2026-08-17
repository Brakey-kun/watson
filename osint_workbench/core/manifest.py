"""Manifest module for the self-healing integrity engine.

Defines data structures for manifest entries, repair actions, package requirements,
and health reports. Embeds the application manifest as a Python data structure.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ManifestEntry:
    """Defines a critical file or directory tracked by the integrity engine."""

    relative_path: str
    file_type: str  # "json" | "markdown" | "directory"
    has_user_data: bool
    default_content: Optional[str] = None
    root: str = "app"  # "app" (Portable_Root) | "data" (external per-user data dir)


@dataclass
class RepairAction:
    """Records a repair action taken by the integrity engine."""

    file_path: str
    repair_type: str  # "created", "restored-from-default", "restored-from-backup"
    success: bool
    error: Optional[str] = None


@dataclass
class PackageRequirement:
    """A Python package requirement parsed from requirements.txt."""

    name: str
    version_spec: str


@dataclass
class PackageStatus:
    """Status of a single package in the virtual environment."""

    name: str
    installed: bool
    installed_version: Optional[str] = None
    satisfies_constraint: bool = False


@dataclass
class HealthReport:
    """Summary of all integrity checks performed at startup."""

    repairs: list[RepairAction] = field(default_factory=list)
    failures: list[RepairAction] = field(default_factory=list)
    needs_user_attention: bool = False
    all_healthy: bool = True

    def to_log_string(self) -> str:
        """Format as human-readable log output.

        Produces structured, human-readable logging output covering:
        - Requirement 11.1: Each repair action with file path and repair type
        - Requirement 11.2: Single confirmation message when all healthy
        - Requirement 11.3: Notice directing user to setup wizard when needed
        - Requirement 11.4: Failed repairs with failure reasons
        """
        if self.all_healthy:
            return (
                "All integrity checks passed \u2014 application is healthy."
            )

        lines: list[str] = []

        # Requirement 11.1: Log each repair action with file path and repair type
        if self.repairs:
            repair_details = ", ".join(
                f"{action.file_path} ({action.repair_type})"
                for action in self.repairs
            )
            lines.append(
                f"Integrity repairs performed: {repair_details}"
            )

        # Requirement 11.4: Log failed repairs with failure reasons
        if self.failures:
            for action in self.failures:
                error_msg = action.error or "Unknown error"
                lines.append(
                    f"FAILED repairs: {action.file_path} \u2014 {error_msg}"
                )

        # Requirement 11.3: Notice directing user to setup wizard
        if self.needs_user_attention:
            lines.append(
                "Action required: Please complete the setup wizard "
                "to configure user data fields."
            )

        return "\n".join(lines)


# --- Default content constants ---

DEFAULT_CONFIG_JSON = """{
    "setup_completed": false,
    "llm": {
        "host": "127.0.0.1",
        "port": 1234,
        "model": "",
        "temperature": 0.7,
        "max_context_tokens": 8192,
        "max_retries": 3
    },
    "fetcher": {
        "max_workers": 20,
        "timeout_seconds": 10,
        "max_retries": 2,
        "rate_limit_per_second": 5.0
    },
    "search": {
        "engines": ["google"],
        "rate_limit_per_engine": 2.0,
        "jitter_min": 0.5,
        "jitter_max": 2.0
    },
    "quality": {
        "min_relevance_score": 0.3,
        "enable_content_dedup": true,
        "noise_patterns": []
    },
    "reporting": {
        "output_dir": "reports"
    }
}"""

DEFAULT_SOURCES_JSON = """{
    "person": [
        {"name": "LinkedIn", "url": "https://www.linkedin.com/in/{query}"},
        {"name": "Twitter/X", "url": "https://x.com/{query}"}
    ],
    "organization": [
        {"name": "Crunchbase", "url": "https://www.crunchbase.com/organization/{query}"},
        {"name": "OpenCorporates", "url": "https://opencorporates.com/companies?q={query}"}
    ],
    "domain": [
        {"name": "Whois", "url": "https://who.is/whois/{query}"},
        {"name": "SecurityTrails", "url": "https://securitytrails.com/domain/{query}"}
    ],
    "ip_address": [
        {"name": "Shodan", "url": "https://www.shodan.io/host/{query}"},
        {"name": "AbuseIPDB", "url": "https://www.abuseipdb.com/check/{query}"}
    ],
    "email": [
        {"name": "Hunter.io", "url": "https://hunter.io/email-verifier/{query}"},
        {"name": "Have I Been Pwned", "url": "https://haveibeenpwned.com/account/{query}"}
    ]
}"""

DEFAULT_SYSTEM_PROMPT = "You are an expert OSINT intelligence analyst."

DEFAULT_REPORT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>OSINT Report</title>
</head>
<body>
    <h1>OSINT Intelligence Report</h1>
    <p>Report content will be generated here.</p>
</body>
</html>"""

DEFAULT_REPORT_MD = """# OSINT Intelligence Report

## Summary

Report content will be generated here.
"""

# --- Supported file types ---

SUPPORTED_FILE_TYPES = {"json", "markdown", "directory"}


def validate_manifest_entry(entry: ManifestEntry) -> bool:
    """Validate a manifest entry has all required fields with valid values.

    Returns True if the entry is valid, False otherwise.
    """
    if not entry.relative_path or not entry.relative_path.strip():
        return False
    if entry.file_type not in SUPPORTED_FILE_TYPES:
        return False
    if not isinstance(entry.has_user_data, bool):
        return False
    if entry.root not in ("app", "data"):
        return False
    return True


# --- Embedded Manifest ---

MANIFEST: list[ManifestEntry] = [
    ManifestEntry("config.json", "json", True, DEFAULT_CONFIG_JSON, root="data"),
    ManifestEntry("sources.json", "json", False, DEFAULT_SOURCES_JSON),
    ManifestEntry("system-prompt.md", "markdown", False, DEFAULT_SYSTEM_PROMPT),
    ManifestEntry("templates/report.html", "markdown", False, DEFAULT_REPORT_HTML),
    ManifestEntry("templates/report.md", "markdown", False, DEFAULT_REPORT_MD),
    ManifestEntry("templates", "directory", False, None),
    ManifestEntry("reports", "directory", False, None, root="data"),
]
