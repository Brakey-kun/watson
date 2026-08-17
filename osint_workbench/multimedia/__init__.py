"""Multimedia Intelligence Pipeline for the Watson OSINT Workbench.

Provides media type detection, specialized extractors (image, video, audio, document),
artifact storage with deduplication, finding conversion, and event integration.
"""

from osint_workbench.multimedia.models import (
    MediaType,
    MediaArtifact,
    ExtractionResult,
    MultimediaFinding,
    MultimediaConfig,
)

__all__ = [
    "MediaType",
    "MediaArtifact",
    "ExtractionResult",
    "MultimediaFinding",
    "MultimediaConfig",
]
