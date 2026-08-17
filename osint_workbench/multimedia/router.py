"""MediaRouter for media type detection and artifact routing.

Detects media types from URLs and file paths using a three-strategy priority:
1. Magic bytes (first 8192 bytes via python-magic)
2. File extension mapping
3. HTTP Content-Type header

Routes MediaArtifacts to the appropriate extractor based on detected type.
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse
from uuid import uuid4

from osint_workbench.core.events import Event, EventBus, EventType
from osint_workbench.multimedia.extractors.base import BaseExtractor
from osint_workbench.multimedia.models import (
    ExtractionResult,
    MediaArtifact,
    MediaType,
    MultimediaConfig,
)

logger = logging.getLogger(__name__)

# Try to import python-magic for magic byte detection
try:
    import magic

    _MAGIC_AVAILABLE = True
except ImportError:
    _MAGIC_AVAILABLE = False


# Extension to MediaType mapping
EXTENSION_MAP: Dict[str, MediaType] = {
    ".jpg": MediaType.IMAGE,
    ".jpeg": MediaType.IMAGE,
    ".png": MediaType.IMAGE,
    ".gif": MediaType.IMAGE,
    ".webp": MediaType.IMAGE,
    ".tiff": MediaType.IMAGE,
    ".mp4": MediaType.VIDEO,
    ".avi": MediaType.VIDEO,
    ".mkv": MediaType.VIDEO,
    ".webm": MediaType.VIDEO,
    ".mov": MediaType.VIDEO,
    ".mp3": MediaType.AUDIO,
    ".wav": MediaType.AUDIO,
    ".ogg": MediaType.AUDIO,
    ".flac": MediaType.AUDIO,
    ".m4a": MediaType.AUDIO,
    ".pdf": MediaType.DOCUMENT,
    ".doc": MediaType.DOCUMENT,
    ".docx": MediaType.DOCUMENT,
    ".txt": MediaType.DOCUMENT,
    ".md": MediaType.DOCUMENT,
}

# MIME type prefix to MediaType mapping (for Content-Type and magic byte results)
_MIME_PREFIX_MAP: Dict[str, MediaType] = {
    "image/": MediaType.IMAGE,
    "video/": MediaType.VIDEO,
    "audio/": MediaType.AUDIO,
    "application/pdf": MediaType.DOCUMENT,
    "application/msword": MediaType.DOCUMENT,
    "application/vnd.openxmlformats-officedocument.wordprocessingml": MediaType.DOCUMENT,
}


def _mime_to_media_type(mime_type: str) -> MediaType:
    """Convert a MIME type string to a MediaType enum value.

    Args:
        mime_type: The MIME type string (e.g., "image/jpeg").

    Returns:
        The corresponding MediaType, or MediaType.UNKNOWN if unmapped.
    """
    if not mime_type:
        return MediaType.UNKNOWN

    mime_lower = mime_type.lower().strip()

    # Check exact matches first
    for prefix, media_type in _MIME_PREFIX_MAP.items():
        if mime_lower.startswith(prefix):
            return media_type

    return MediaType.UNKNOWN


def _extract_path_from_url(url: str) -> Optional[Path]:
    """Extract the path component from a URL for extension-based detection.

    Args:
        url: The URL string to parse.

    Returns:
        A Path object representing the URL path, or None if parsing fails.
    """
    try:
        parsed = urlparse(url)
        if parsed.path:
            return Path(parsed.path)
    except Exception:
        pass
    return None


def _detect_from_extension(suffix: str) -> MediaType:
    """Detect media type from a file extension.

    Args:
        suffix: The file extension including the dot (e.g., ".jpg").

    Returns:
        The corresponding MediaType, or MediaType.UNKNOWN if unmapped.
    """
    return EXTENSION_MAP.get(suffix.lower(), MediaType.UNKNOWN)


def _detect_from_magic_bytes(path: Path) -> MediaType:
    """Detect media type using magic bytes (first 8192 bytes).

    Args:
        path: Path to the file to analyze.

    Returns:
        The detected MediaType, or MediaType.UNKNOWN if detection fails.
    """
    if not _MAGIC_AVAILABLE:
        return MediaType.UNKNOWN

    try:
        mime_type = magic.from_file(str(path), mime=True)
        if mime_type:
            return _mime_to_media_type(mime_type)
    except Exception:
        pass

    return MediaType.UNKNOWN


def _fetch_content_type(url: str) -> Optional[str]:
    """Fetch the Content-Type header from a URL via HEAD request.

    Uses a 10-second timeout. Returns None on any error (network error,
    timeout, non-2xx response).

    Args:
        url: The URL to query.

    Returns:
        The Content-Type header value, or None if unavailable.
    """
    try:
        import requests

        response = requests.head(url, timeout=10, allow_redirects=True)
        if response.status_code < 200 or response.status_code >= 300:
            return None
        content_type = response.headers.get("Content-Type", "")
        # Strip parameters (e.g., "text/html; charset=utf-8" → "text/html")
        if ";" in content_type:
            content_type = content_type.split(";")[0].strip()
        return content_type if content_type else None
    except Exception:
        return None


# Default MIME type for artifacts with known media type but no specific mime
_DEFAULT_MIME_FOR_TYPE: Dict[MediaType, str] = {
    MediaType.IMAGE: "image/jpeg",
    MediaType.VIDEO: "video/mp4",
    MediaType.AUDIO: "audio/mpeg",
    MediaType.DOCUMENT: "application/pdf",
}

# Extension to MIME type mapping. Public: also used directly by
# osint_workbench.core.rag_ingest to derive a valid MediaArtifact.mime_type
# from an uploaded file's extension.
EXTENSION_TO_MIME: Dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".tiff": "image/tiff",
    ".mp4": "video/mp4",
    ".avi": "video/avi",
    ".mkv": "video/mkv",
    ".webm": "video/webm",
    ".mov": "video/mov",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
}


class MediaRouter:
    """Detects media types and routes artifacts to appropriate extractors.

    Uses a three-strategy priority for type detection:
    1. Magic bytes (most reliable, requires local file)
    2. File extension mapping
    3. HTTP Content-Type header (for URLs)

    Routes artifacts to registered extractors and handles the extraction
    lifecycle including error handling and event emission.
    """

    def __init__(
        self,
        config: MultimediaConfig,
        event_bus: Optional[EventBus] = None,
        extractors: Optional[Dict[MediaType, BaseExtractor]] = None,
    ) -> None:
        """Initialize the MediaRouter.

        Args:
            config: Multimedia pipeline configuration.
            event_bus: EventBus instance for emitting events. If None, events
                are not emitted.
            extractors: Optional mapping of MediaType to extractor instances.
                If None, process_artifact() will fail with "no extractor registered".
        """
        self._config = config
        self._event_bus = event_bus
        self._extractors: Dict[MediaType, BaseExtractor] = extractors or {}

    def detect_media_type(
        self, url: Optional[str] = None, path: Optional[Path] = None
    ) -> MediaType:
        """Detect media type from a URL and/or file path.

        Detection follows a priority order:
        1. Magic bytes (if local file exists and is readable)
        2. File extension (from path or URL)
        3. HTTP Content-Type header (for URLs only)

        If all strategies fail, returns MediaType.UNKNOWN (never raises).

        Args:
            url: Optional URL string for the media resource.
            path: Optional local file path.

        Returns:
            The detected MediaType enum value. Deterministic: same input
            always yields same result.
        """
        # Strategy 1: Magic bytes (if local file available and readable)
        if path is not None:
            try:
                resolved = Path(path)
                if resolved.exists() and resolved.is_file():
                    magic_type = _detect_from_magic_bytes(resolved)
                    if magic_type != MediaType.UNKNOWN:
                        return magic_type
            except Exception:
                # File doesn't exist or not readable — skip to next strategy
                pass

        # Strategy 2: File extension
        # Try from local path first, then from URL
        target_path: Optional[Path] = None
        if path is not None:
            target_path = Path(path)
        elif url is not None:
            target_path = _extract_path_from_url(url)

        if target_path is not None:
            suffix = target_path.suffix.lower()
            if suffix:
                ext_type = _detect_from_extension(suffix)
                if ext_type != MediaType.UNKNOWN:
                    return ext_type

        # Strategy 3: HTTP Content-Type (HEAD request, only for URLs)
        if url is not None:
            content_type = _fetch_content_type(url)
            if content_type:
                ct_type = _mime_to_media_type(content_type)
                if ct_type != MediaType.UNKNOWN:
                    return ct_type

        return MediaType.UNKNOWN

    def route_results(
        self, fetch_results: List[dict], investigation_id: str = ""
    ) -> List[MediaArtifact]:
        """Convert fetch results into MediaArtifact objects.

        Detects media type from the URL in each fetch result, creates
        MediaArtifact objects with uuid4 IDs, and emits MEDIA_DISCOVERED events.

        Args:
            fetch_results: List of dicts with keys "url", "name", "status".
                Only results with status containing "Active" or "Accessible"
                are processed.
            investigation_id: The investigation ID to associate with artifacts.

        Returns:
            List of MediaArtifact objects for successfully processed results.
        """
        artifacts: List[MediaArtifact] = []

        for result in fetch_results:
            url = result.get("url", "")
            if not url:
                continue

            # Detect media type from URL (extension-based, no local file)
            media_type = self.detect_media_type(url=url)

            # Determine MIME type from URL extension
            url_path = _extract_path_from_url(url)
            mime_type = _DEFAULT_MIME_FOR_TYPE.get(media_type, "application/octet-stream")
            if url_path and url_path.suffix.lower() in EXTENSION_TO_MIME:
                mime_type = EXTENSION_TO_MIME[url_path.suffix.lower()]

            # Skip UNKNOWN types that have no valid MIME — we can't create
            # a valid MediaArtifact without a supported MIME type
            if media_type == MediaType.UNKNOWN:
                logger.info(
                    "Skipping URL with unknown media type: %s", url
                )
                continue

            # Check if mime_type is in the supported list
            supported_types = self._config.get_all_supported_types()
            if mime_type not in supported_types:
                logger.info(
                    "Skipping URL with unsupported MIME type %s: %s",
                    mime_type,
                    url,
                )
                continue

            artifact_id = str(uuid4())

            try:
                artifact = MediaArtifact(
                    artifact_id=artifact_id,
                    source_url=url,
                    local_path=None,
                    media_type=media_type,
                    mime_type=mime_type,
                    file_size_bytes=1,  # Placeholder — actual size determined on download
                    investigation_id=investigation_id,
                    metadata={"name": result.get("name", ""), "status": result.get("status", "")},
                    _config=self._config,
                )
            except ValueError as e:
                logger.warning("Failed to create MediaArtifact for %s: %s", url, e)
                continue

            artifacts.append(artifact)

            # Emit MEDIA_DISCOVERED event
            if self._event_bus is not None:
                self._event_bus.emit(
                    Event(
                        type=EventType.MEDIA_DISCOVERED,
                        investigation_id=investigation_id,
                        data={
                            "artifact_id": artifact_id,
                            "media_type": media_type.value,
                            "url": url,
                        },
                    )
                )

        return artifacts

    def process_artifact(self, artifact: MediaArtifact) -> ExtractionResult:
        """Select extractor, run extraction, and return result.

        Handles all failure cases:
        - UNKNOWN media type → emit EXTRACTION_FAILED, return error result
        - Unsupported MIME type → emit EXTRACTION_FAILED, return error result
        - Extractor exception → emit EXTRACTION_FAILED, return error result
        - Success → return ExtractionResult from extractor

        Args:
            artifact: The MediaArtifact to process.

        Returns:
            ExtractionResult with extraction data or error details.
            Never raises exceptions.
        """
        # Case 1: UNKNOWN media type — skip extraction
        if artifact.media_type == MediaType.UNKNOWN:
            logger.info(
                "Skipping extraction for artifact %s with UNKNOWN media type",
                artifact.artifact_id,
            )
            self._emit_extraction_failed(
                artifact, reason="unsupported_media_type"
            )
            return ExtractionResult(
                artifact_id=artifact.artifact_id,
                media_type=artifact.media_type,
                text_content="",
                metadata={},
                entities=[],
                confidence=0.0,
                error="unsupported_media_type",
            )

        # Case 2: No extractor registered for this media type
        extractor = self._extractors.get(artifact.media_type)
        if extractor is None:
            logger.warning(
                "No extractor registered for media type %s (artifact %s)",
                artifact.media_type.value,
                artifact.artifact_id,
            )
            self._emit_extraction_failed(
                artifact, reason="no_extractor_registered"
            )
            return ExtractionResult(
                artifact_id=artifact.artifact_id,
                media_type=artifact.media_type,
                text_content="",
                metadata={},
                entities=[],
                confidence=0.0,
                error="no_extractor_registered",
            )

        # Case 3: Verify MIME type is supported by the extractor
        supported_mimes = extractor.supports()
        if artifact.mime_type not in supported_mimes:
            logger.info(
                "MIME type %s not supported by %s extractor (artifact %s)",
                artifact.mime_type,
                artifact.media_type.value,
                artifact.artifact_id,
            )
            self._emit_extraction_failed(
                artifact, reason="unsupported_mime_type"
            )
            return ExtractionResult(
                artifact_id=artifact.artifact_id,
                media_type=artifact.media_type,
                text_content="",
                metadata={},
                entities=[],
                confidence=0.0,
                error="unsupported_mime_type",
            )

        # Case 4: Run extraction (catch any exceptions)
        try:
            result = extractor.extract(artifact)
            return result
        except Exception as exc:
            logger.error(
                "Extractor raised exception for artifact %s: %s",
                artifact.artifact_id,
                exc,
            )
            self._emit_extraction_failed(
                artifact, reason="extractor_error"
            )
            return ExtractionResult(
                artifact_id=artifact.artifact_id,
                media_type=artifact.media_type,
                text_content="",
                metadata={},
                entities=[],
                confidence=0.0,
                error=f"extractor_error: {exc}",
            )

    def _emit_extraction_failed(
        self, artifact: MediaArtifact, reason: str
    ) -> None:
        """Emit an EXTRACTION_FAILED event on the EventBus.

        Args:
            artifact: The artifact that failed extraction.
            reason: The failure reason string.
        """
        if self._event_bus is None:
            return

        self._event_bus.emit(
            Event(
                type=EventType.EXTRACTION_FAILED,
                investigation_id=artifact.investigation_id,
                data={
                    "artifact_id": artifact.artifact_id,
                    "media_type": artifact.media_type.value,
                    "reason": reason,
                    "confidence": 0.0,
                },
            )
        )
