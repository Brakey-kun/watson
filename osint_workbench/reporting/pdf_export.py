"""PDF export module for Watson.

Provides HTML-to-PDF conversion using WeasyPrint. If WeasyPrint is not
installed, the module gracefully degrades by raising ImportError with a
clear message when generate_pdf is called.
"""

import logging

logger = logging.getLogger(__name__)

try:
    from weasyprint import HTML as _WeasyHTML

    _WEASYPRINT_AVAILABLE = True
except ImportError:
    _WEASYPRINT_AVAILABLE = False


def generate_pdf(html_path: str, output_path: str) -> None:
    """Convert an HTML file to PDF using WeasyPrint.

    Args:
        html_path: Path to the source HTML file.
        output_path: Path where the PDF file will be written.

    Raises:
        ImportError: If WeasyPrint is not installed.
        FileNotFoundError: If the HTML file does not exist.
        RuntimeError: If PDF rendering fails.
    """
    if not _WEASYPRINT_AVAILABLE:
        raise ImportError(
            "WeasyPrint is not installed. Install it with: "
            "pip install weasyprint"
        )

    import os

    if not os.path.isfile(html_path):
        raise FileNotFoundError(
            f"HTML file not found: {html_path}"
        )

    try:
        html_doc = _WeasyHTML(filename=html_path)
        html_doc.write_pdf(output_path)
        logger.info("PDF generated: %s", output_path)
    except Exception as e:
        raise RuntimeError(
            f"PDF rendering failed: {e}"
        ) from e
