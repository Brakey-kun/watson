"""Image extractor for the Multimedia Intelligence Pipeline.

Extracts EXIF metadata, GPS coordinates, and OCR text from image files
using Pillow and pytesseract.
"""

import time
from pathlib import Path
from typing import List, Optional

from osint_workbench.multimedia.extractors.base import BaseExtractor
from osint_workbench.multimedia.models import (
    ExtractionResult,
    MediaArtifact,
    MediaType,
    MultimediaConfig,
)


class ImageExtractor(BaseExtractor):
    """Extracts EXIF metadata, GPS coordinates, OCR text from image files.

    Uses Pillow for image properties and EXIF extraction, and pytesseract
    for optical character recognition. All extraction is wrapped in
    try/except — this class never raises exceptions to the caller.
    """

    def __init__(self, config: Optional[MultimediaConfig] = None) -> None:
        """Initialize ImageExtractor with pipeline configuration.

        Args:
            config: Pipeline configuration. Uses defaults if None.
        """
        self._config = config or MultimediaConfig()

    def supports(self) -> List[str]:
        """Return the list of MIME types this extractor supports.

        Returns:
            List of supported image MIME type strings.
        """
        return list(self._config.supported_image_types)

    def max_file_size_bytes(self) -> int:
        """Return the maximum file size in bytes this extractor will process.

        Returns:
            Maximum image file size in bytes derived from config.
        """
        return int(self._config.max_image_size_mb * 1024 * 1024)

    def extract(self, artifact: MediaArtifact) -> ExtractionResult:
        """Extract intelligence content from an image artifact.

        Algorithm:
        1. Check file size against max_image_size_mb
        2. Check mime_type against supported types
        3. Open image with Pillow → extract width, height, format, mode
        4. Extract EXIF data
        5. Extract GPS from EXIF
        6. Run OCR if enabled
        7. Clamp confidence to max 1.0
        8. On any exception → confidence 0.0, error message

        Args:
            artifact: The image media artifact to process.

        Returns:
            ExtractionResult with extracted content. Never raises.
        """
        start_time = time.time()

        # Step 1: Check file size (Req 3.7)
        if artifact.file_size_bytes > self.max_file_size_bytes():
            processing_time = (time.time() - start_time) * 1000
            return ExtractionResult(
                artifact_id=artifact.artifact_id,
                media_type=MediaType.IMAGE,
                text_content="",
                metadata={},
                entities=[],
                confidence=0.0,
                error=(
                    f"File size {artifact.file_size_bytes} bytes exceeds "
                    f"maximum allowed {self.max_file_size_bytes()} bytes"
                ),
                processing_time_ms=processing_time,
            )

        # Step 2: Check MIME type (Req 3.5, 3.6)
        if artifact.mime_type not in self.supports():
            processing_time = (time.time() - start_time) * 1000
            return ExtractionResult(
                artifact_id=artifact.artifact_id,
                media_type=MediaType.IMAGE,
                text_content="",
                metadata={},
                entities=[],
                confidence=0.0,
                error=f"Unsupported MIME type: {artifact.mime_type}",
                processing_time_ms=processing_time,
            )

        metadata: dict = {}
        text_content = ""
        entities: List[str] = []
        confidence = 0.0
        error: Optional[str] = None

        try:
            # Step 3: Open image and extract basic properties (Req 3.1)
            try:
                from PIL import Image
            except ImportError:
                processing_time = (time.time() - start_time) * 1000
                return ExtractionResult(
                    artifact_id=artifact.artifact_id,
                    media_type=MediaType.IMAGE,
                    text_content="",
                    metadata={},
                    entities=[],
                    confidence=0.0,
                    error="Pillow (PIL) is not installed",
                    processing_time_ms=processing_time,
                )

            with Image.open(artifact.local_path) as img:
                img.load()  # Force full load to detect corrupt images
                metadata["width"] = img.width
                metadata["height"] = img.height
                metadata["format"] = img.format
                metadata["mode"] = img.mode
                confidence += 0.3

            # Step 4: EXIF extraction (Req 3.2)
            exif_data = self.extract_exif(artifact.local_path)
            if exif_data:
                metadata["exif"] = exif_data
                confidence += 0.2

                # Step 5: GPS extraction from EXIF (Req 3.2)
                gps = self.extract_gps(exif_data)
                if gps:
                    metadata["gps"] = {"lat": gps[0], "lng": gps[1]}
                    confidence += 0.2

            # Step 6: OCR if enabled (Req 3.3)
            if self._config.enable_ocr:
                text_content = self.run_ocr(artifact.local_path)
                if text_content.strip():
                    confidence += 0.3

        except Exception as e:
            # Step 8: On any exception → confidence 0.0 (Req 3.4)
            error = f"Image extraction error: {str(e)}"
            confidence = 0.0
            metadata = {}

        # Step 7: Clamp confidence
        confidence = min(confidence, 1.0)

        processing_time = (time.time() - start_time) * 1000

        return ExtractionResult(
            artifact_id=artifact.artifact_id,
            media_type=MediaType.IMAGE,
            text_content=text_content,
            metadata=metadata,
            entities=entities,
            confidence=confidence,
            error=error,
            processing_time_ms=processing_time,
        )

    def extract_exif(self, path: Path) -> dict:
        """Extract EXIF data from an image file.

        Args:
            path: Path to the image file.

        Returns:
            Dictionary of EXIF fields (camera make, model, datetime, GPS info).
            Returns empty dict if no EXIF data or on error.
        """
        try:
            from PIL import Image
            from PIL.ExifTags import TAGS

            exif_data: dict = {}
            with Image.open(path) as img:
                raw_exif = img.getexif()
                if not raw_exif:
                    return {}

                # Map common EXIF tag IDs to human-readable names
                for tag_id, value in raw_exif.items():
                    tag_name = TAGS.get(tag_id, str(tag_id))
                    # Only include relevant fields
                    if tag_name in ("Make", "Model", "DateTime", "DateTimeOriginal"):
                        exif_data[tag_name] = str(value)

                # Extract GPS IFD data
                gps_ifd = raw_exif.get_ifd(0x8825)  # GPSInfo IFD
                if gps_ifd:
                    exif_data["GPSInfo"] = {
                        str(k): v for k, v in gps_ifd.items()
                    }

            return exif_data

        except Exception:
            return {}

    def extract_gps(self, exif_data: dict) -> Optional[tuple]:
        """Extract GPS coordinates from EXIF data.

        Args:
            exif_data: Dictionary of EXIF data (as returned by extract_exif).

        Returns:
            Tuple of (latitude, longitude) as floats, or None if not available.
        """
        try:
            gps_info = exif_data.get("GPSInfo")
            if not gps_info:
                return None

            # GPS tag IDs: 1=LatRef, 2=Lat, 3=LngRef, 4=Lng
            # When read from IFD, keys are integers
            gps_lat = gps_info.get("2") or gps_info.get(2)
            gps_lat_ref = gps_info.get("1") or gps_info.get(1)
            gps_lng = gps_info.get("4") or gps_info.get(4)
            gps_lng_ref = gps_info.get("3") or gps_info.get(3)

            if not all([gps_lat, gps_lat_ref, gps_lng, gps_lng_ref]):
                return None

            lat = self._convert_to_degrees(gps_lat)
            lng = self._convert_to_degrees(gps_lng)

            if gps_lat_ref in ("S", b"S"):
                lat = -lat
            if gps_lng_ref in ("W", b"W"):
                lng = -lng

            return (lat, lng)

        except Exception:
            return None

    def run_ocr(self, path: Path) -> str:
        """Run OCR on an image to extract text.

        Args:
            path: Path to the image file.

        Returns:
            Recognized text string, or empty string if no text found or on
            error -- including a stuck tesseract process. Without the
            timeout, a hung OCR call blocks the caller (e.g. the RAG
            ingest background thread) forever with no user-visible
            failure; `pytesseract.image_to_string` raises
            `RuntimeError` on expiry, which the blanket except below
            converts into the same graceful empty-string result as any
            other OCR failure.
        """
        try:
            import pytesseract
            from PIL import Image

            with Image.open(path) as img:
                text = pytesseract.image_to_string(img, timeout=30)
                return text if text else ""

        except ImportError:
            # pytesseract not installed — return empty string
            return ""
        except Exception:
            return ""

    @staticmethod
    def _convert_to_degrees(value) -> float:
        """Convert GPS coordinate from degrees/minutes/seconds to decimal degrees.

        Args:
            value: GPS coordinate as tuple of (degrees, minutes, seconds)
                   where each element may be a float or IFDRational.

        Returns:
            Decimal degrees as float.
        """
        try:
            d = float(value[0])
            m = float(value[1])
            s = float(value[2])
            return d + (m / 60.0) + (s / 3600.0)
        except (TypeError, IndexError, ValueError, ZeroDivisionError):
            return 0.0
