"""Data models and enums for the Multimedia Intelligence Pipeline.

Defines MediaType enum, MediaArtifact, ExtractionResult, MultimediaFinding,
and MultimediaConfig dataclasses with validation logic.
"""

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse


class MediaType(Enum):
    """Supported media type categories."""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    UNKNOWN = "unknown"


# UUID4 pattern: 8-4-4-4-12 hex with version nibble = 4
_UUID4_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# Default supported MIME types by media category
DEFAULT_SUPPORTED_IMAGE_TYPES = [
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/tiff"
]
DEFAULT_SUPPORTED_VIDEO_TYPES = [
    "video/mp4", "video/avi", "video/mkv", "video/webm", "video/mov"
]
DEFAULT_SUPPORTED_AUDIO_TYPES = [
    "audio/mpeg", "audio/wav", "audio/ogg", "audio/flac", "audio/mp4"
]
DEFAULT_SUPPORTED_DOCUMENT_TYPES = [
    "application/pdf", "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain", "text/markdown",
]


def _get_all_supported_types() -> List[str]:
    """Return the full list of default supported MIME types across all categories."""
    return (
        DEFAULT_SUPPORTED_IMAGE_TYPES
        + DEFAULT_SUPPORTED_VIDEO_TYPES
        + DEFAULT_SUPPORTED_AUDIO_TYPES
        + DEFAULT_SUPPORTED_DOCUMENT_TYPES
    )


@dataclass
class MultimediaConfig:
    """Configuration for the multimedia pipeline."""

    max_image_size_mb: float = 50.0
    max_video_size_mb: float = 500.0
    max_audio_size_mb: float = 200.0
    max_document_size_mb: float = 100.0
    enable_ocr: bool = True
    enable_transcription: bool = True
    enable_llm_analysis: bool = True
    keyframe_interval_seconds: float = 5.0
    max_concurrent_extractions: int = 4
    supported_image_types: List[str] = field(
        default_factory=lambda: list(DEFAULT_SUPPORTED_IMAGE_TYPES)
    )
    supported_video_types: List[str] = field(
        default_factory=lambda: list(DEFAULT_SUPPORTED_VIDEO_TYPES)
    )
    supported_audio_types: List[str] = field(
        default_factory=lambda: list(DEFAULT_SUPPORTED_AUDIO_TYPES)
    )
    supported_document_types: List[str] = field(
        default_factory=lambda: list(DEFAULT_SUPPORTED_DOCUMENT_TYPES)
    )

    def get_all_supported_types(self) -> List[str]:
        """Return the full list of supported MIME types across all categories."""
        return (
            self.supported_image_types
            + self.supported_video_types
            + self.supported_audio_types
            + self.supported_document_types
        )

    def get_supported_types_for(self, media_type: "MediaType") -> List[str]:
        """Return supported MIME types for a specific media type category."""
        type_map = {
            MediaType.IMAGE: self.supported_image_types,
            MediaType.VIDEO: self.supported_video_types,
            MediaType.AUDIO: self.supported_audio_types,
            MediaType.DOCUMENT: self.supported_document_types,
        }
        return type_map.get(media_type, [])


@dataclass
class MediaArtifact:
    """Represents a discovered or ingested multimedia item.

    Validates all fields in __post_init__ per Requirements 13.1-13.7.
    Raises ValueError with a descriptive message if any field fails validation.
    """

    artifact_id: str
    source_url: Optional[str]
    local_path: Optional[Path]
    media_type: MediaType
    mime_type: str
    file_size_bytes: int
    investigation_id: str
    metadata: dict = field(default_factory=dict)
    _config: Optional[MultimediaConfig] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Validate all fields upon creation."""
        self._validate_artifact_id()
        self._validate_source_location()
        self._validate_file_size()
        self._validate_mime_type()
        self._validate_media_type()
        self._validate_source_url_format()

    def _validate_artifact_id(self) -> None:
        """Validate artifact_id is a valid UUID4 (Req 13.1)."""
        if not isinstance(self.artifact_id, str) or not _UUID4_PATTERN.match(self.artifact_id):
            raise ValueError(
                f"artifact_id: Must be a valid UUID4 in 8-4-4-4-12 hex format with "
                f"version nibble 4, got '{self.artifact_id}'"
            )

    def _validate_source_location(self) -> None:
        """Validate at least one of source_url or local_path is set (Req 13.2)."""
        has_url = (
            self.source_url is not None
            and isinstance(self.source_url, str)
            and self.source_url.strip() != ""
        )
        has_path = (
            self.local_path is not None
            and str(self.local_path).strip() != ""
        )
        if not has_url and not has_path:
            raise ValueError(
                "source_url/local_path: At least one of source_url or local_path "
                "must be set to a non-None, non-empty, non-whitespace value"
            )

    def _validate_file_size(self) -> None:
        """Validate file_size_bytes is an integer > 0 (Req 13.3)."""
        if not isinstance(self.file_size_bytes, int) or self.file_size_bytes <= 0:
            raise ValueError(
                f"file_size_bytes: Must be an integer greater than 0, "
                f"got {self.file_size_bytes!r}"
            )

    def _validate_mime_type(self) -> None:
        """Validate mime_type is non-empty and in supported types list (Req 13.4)."""
        if not isinstance(self.mime_type, str) or self.mime_type.strip() == "":
            raise ValueError(
                f"mime_type: Must be a non-empty string, got {self.mime_type!r}"
            )

        # Check against configured supported types or default list
        if self._config is not None:
            supported = self._config.get_all_supported_types()
        else:
            supported = _get_all_supported_types()

        if self.mime_type not in supported:
            raise ValueError(
                f"mime_type: '{self.mime_type}' is not in the supported types list"
            )

    def _validate_media_type(self) -> None:
        """Validate media_type is a valid MediaType enum value (Req 13.5)."""
        if not isinstance(self.media_type, MediaType):
            raise ValueError(
                f"media_type: Must be a valid MediaType enum value, "
                f"got {self.media_type!r}"
            )

    def _validate_source_url_format(self) -> None:
        """Validate source_url contains scheme and host if provided (Req 13.7)."""
        if self.source_url is None:
            return
        if not isinstance(self.source_url, str) or self.source_url.strip() == "":
            return  # Already handled by _validate_source_location if local_path also absent

        parsed = urlparse(self.source_url)
        if not parsed.scheme:
            raise ValueError(
                f"source_url: Must contain a scheme component (e.g., 'http' or 'https'), "
                f"got '{self.source_url}'"
            )
        if not parsed.hostname:
            raise ValueError(
                f"source_url: Must contain a host component, "
                f"got '{self.source_url}'"
            )


@dataclass
class ExtractionResult:
    """Output of a media extraction operation.

    Validates confidence is clamped to [0.0, 1.0] and processing_time_ms >= 0
    per Requirements 7.1, 7.2.
    """

    artifact_id: str
    media_type: MediaType
    text_content: str
    metadata: dict
    entities: List[str]
    confidence: float
    language: Optional[str] = None
    error: Optional[str] = None
    processing_time_ms: float = 0.0

    def __post_init__(self) -> None:
        """Validate and clamp fields upon creation."""
        # Req 7.1: Clamp confidence to [0.0, 1.0]
        if not isinstance(self.confidence, (int, float)):
            self.confidence = 0.0
        self.confidence = max(0.0, min(1.0, float(self.confidence)))

        # Req 7.2: processing_time_ms must be non-negative
        if not isinstance(self.processing_time_ms, (int, float)):
            self.processing_time_ms = 0.0
        if self.processing_time_ms < 0:
            self.processing_time_ms = 0.0


@dataclass
class MultimediaFinding:
    """A finding generated from multimedia analysis.

    Compatible with the existing QualityPipeline for scoring and filtering.
    """

    artifact_id: str
    url: Optional[str]
    name: str
    media_type: MediaType
    text_content: str
    metadata: dict
    entities: List[str]
    confidence: float
    gps_coordinates: Optional[Tuple[float, float]]
    relevance_score: float = 0.0
