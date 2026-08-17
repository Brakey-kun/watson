"""Media extractors for the Multimedia Intelligence Pipeline.

Provides specialized extractors for image, video, audio, and document media types.
"""

from osint_workbench.multimedia.extractors.base import BaseExtractor
from osint_workbench.multimedia.extractors.document import DocumentExtractor
from osint_workbench.multimedia.extractors.image import ImageExtractor

__all__ = ["BaseExtractor", "DocumentExtractor", "ImageExtractor"]
