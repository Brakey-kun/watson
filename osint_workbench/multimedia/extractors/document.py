"""Document extractor for the Multimedia Intelligence Pipeline.

Extracts text from uploaded text/markdown files natively, and from PDF/DOCX
files via optional pypdf/python-docx imports -- mirroring ImageExtractor's
run_ocr(): a missing optional dependency degrades to an empty result with
the error field set, rather than failing extraction outright.
"""

import time
from pathlib import Path
from typing import List, Optional, Tuple

from osint_workbench.multimedia.extractors.base import BaseExtractor
from osint_workbench.multimedia.models import (
    ExtractionResult,
    MediaArtifact,
    MultimediaConfig,
)

_TEXT_MIME_TYPES = {"text/plain", "text/markdown"}
_PDF_MIME_TYPE = "application/pdf"
_DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_LEGACY_DOC_MIME_TYPE = "application/msword"


class DocumentExtractor(BaseExtractor):
    """Extracts text content from text/markdown/PDF/DOCX document files.

    Text and Markdown are read natively (stdlib only, always available).
    PDF (pypdf) and DOCX (python-docx) support is optional: if the library
    isn't installed, extraction returns empty text_content with a
    descriptive error instead of raising, matching
    ImageExtractor.run_ocr()'s graceful-degradation contract for optional
    dependencies. Legacy binary .doc is explicitly unsupported (python-docx
    only reads the modern XML-based format).
    """

    def __init__(self, config: Optional[MultimediaConfig] = None) -> None:
        """Initialize DocumentExtractor with pipeline configuration.

        Args:
            config: Optional MultimediaConfig; defaults to MultimediaConfig().
        """
        self._config = config or MultimediaConfig()

    def supports(self) -> List[str]:
        """Return the list of MIME types this extractor supports."""
        return list(self._config.supported_document_types)

    def max_file_size_bytes(self) -> int:
        """Return the maximum file size in bytes this extractor will process."""
        return int(self._config.max_document_size_mb * 1024 * 1024)

    def extract(self, artifact: MediaArtifact) -> ExtractionResult:
        """Extract text content from a document artifact.

        Args:
            artifact: The media artifact to process.

        Returns:
            ExtractionResult with extracted text and confidence. Never
            raises -- extraction failures are captured in the error field.
        """
        start = time.time()
        path = artifact.local_path
        error: Optional[str] = None
        text = ""

        if path is None:
            error = "No local file path to extract from"
        elif artifact.mime_type in _TEXT_MIME_TYPES:
            text, error = self._extract_text(Path(path))
        elif artifact.mime_type == _PDF_MIME_TYPE:
            text, error = self._extract_pdf(Path(path))
        elif artifact.mime_type == _DOCX_MIME_TYPE:
            text, error = self._extract_docx(Path(path))
        elif artifact.mime_type == _LEGACY_DOC_MIME_TYPE:
            error = "Legacy .doc binary format is not supported; convert to .docx or PDF"
        else:
            error = f"Unsupported document MIME type: {artifact.mime_type}"

        return ExtractionResult(
            artifact_id=artifact.artifact_id,
            media_type=artifact.media_type,
            text_content=text,
            metadata={},
            entities=[],
            confidence=1.0 if text and not error else 0.0,
            processing_time_ms=(time.time() - start) * 1000,
            error=error,
        )

    def _extract_text(self, path: Path) -> Tuple[str, Optional[str]]:
        """Read a plain text/markdown file, replacing undecodable bytes."""
        try:
            return path.read_text(encoding="utf-8", errors="replace"), None
        except OSError as exc:
            return "", str(exc)

    def _extract_pdf(self, path: Path) -> Tuple[str, Optional[str]]:
        """Extract text from a PDF via pypdf, if installed."""
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            pages = [page.extract_text() or "" for page in reader.pages]
            return "\n".join(pages).strip(), None
        except ImportError:
            return "", "PDF extraction requires the 'pypdf' package (not installed)"
        except Exception as exc:
            return "", f"Failed to extract PDF text: {exc}"

    def _extract_docx(self, path: Path) -> Tuple[str, Optional[str]]:
        """Extract text from a DOCX via python-docx, if installed."""
        try:
            import docx

            document = docx.Document(str(path))
            paragraphs = [p.text for p in document.paragraphs]
            return "\n".join(paragraphs).strip(), None
        except ImportError:
            return "", "DOCX extraction requires the 'python-docx' package (not installed)"
        except Exception as exc:
            return "", f"Failed to extract DOCX text: {exc}"
