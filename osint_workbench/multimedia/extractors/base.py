"""Abstract base class for all media extractors.

Defines the interface that ImageExtractor, VideoExtractor, AudioExtractor,
and DocumentExtractor must implement.
"""

from abc import ABC, abstractmethod
from typing import List

from osint_workbench.multimedia.models import ExtractionResult, MediaArtifact


class BaseExtractor(ABC):
    """Abstract base for all media extractors.

    Subclasses must implement extract(), supports(), and max_file_size_bytes().
    The extract() method must never raise exceptions to the caller — all errors
    should be captured in the ExtractionResult.error field.
    """

    @abstractmethod
    def extract(self, artifact: MediaArtifact) -> ExtractionResult:
        """Extract intelligence content from a multimedia artifact.

        Args:
            artifact: The media artifact to process.

        Returns:
            ExtractionResult with extracted content, metadata, and confidence.
            Never raises — errors are captured in the result's error field.
        """
        ...

    @abstractmethod
    def supports(self) -> List[str]:
        """Return the list of MIME types this extractor supports.

        Returns:
            List of MIME type strings (e.g., ["image/jpeg", "image/png"]).
        """
        ...

    @abstractmethod
    def max_file_size_bytes(self) -> int:
        """Return the maximum file size in bytes this extractor will process.

        Returns:
            Maximum file size in bytes.
        """
        ...
