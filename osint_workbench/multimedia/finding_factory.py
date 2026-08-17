"""FindingFactory for converting ExtractionResult objects into MultimediaFinding objects.

Bridges the multimedia extraction pipeline with the existing QualityPipeline by
producing MultimediaFinding instances and quality-compatible dicts.
"""

from typing import Optional, Tuple

from osint_workbench.multimedia.models import (
    ExtractionResult,
    MediaArtifact,
    MultimediaFinding,
)


class FindingFactory:
    """Converts extraction results to OSINT findings compatible with QualityPipeline."""

    def __init__(self, quality_pipeline: "QualityPipeline") -> None:  # noqa: F821
        """Initialize the FindingFactory.

        Args:
            quality_pipeline: The QualityPipeline instance used for scoring/filtering.
        """
        self.quality_pipeline = quality_pipeline

    def create_finding(
        self,
        extraction: ExtractionResult,
        artifact: MediaArtifact,
        target: str,
    ) -> MultimediaFinding:
        """Convert an ExtractionResult and MediaArtifact into a MultimediaFinding.

        Preserves text_content character-for-character, entities in original order,
        and extracts GPS coordinates from metadata. Computes relevance_score based
        on case-insensitive target occurrences in text_content.

        Args:
            extraction: The extraction result from a media extractor.
            artifact: The source media artifact.
            target: The investigation target string for relevance scoring.

        Returns:
            A MultimediaFinding populated with all extracted data and relevance score.
        """
        # Preserve text_content character-for-character (Req 10.1)
        text_content = extraction.text_content

        # Preserve entities in order, no additions or removals (Req 10.2)
        entities = extraction.entities

        # Extract GPS coordinates from metadata (Req 10.4)
        gps_coordinates = self._extract_gps(extraction.metadata)

        # Compute relevance score (Req 10.3)
        relevance_score = self._compute_relevance_score(text_content, target)

        # Derive name from artifact metadata or source URL
        name = artifact.metadata.get("name", "")
        if not name and artifact.source_url:
            # Derive name from the last path segment of the URL
            url_path = artifact.source_url.rstrip("/")
            name = url_path.split("/")[-1] if "/" in url_path else url_path

        return MultimediaFinding(
            artifact_id=artifact.artifact_id,
            url=artifact.source_url,
            name=name,
            media_type=artifact.media_type,
            text_content=text_content,
            metadata=extraction.metadata,
            entities=entities,
            confidence=extraction.confidence,
            gps_coordinates=gps_coordinates,
            relevance_score=relevance_score,
        )

    def to_quality_dict(self, finding: MultimediaFinding) -> dict:
        """Convert a MultimediaFinding to a dict accepted by QualityPipeline.filter_and_score.

        Returns a dict with keys: "name", "url", "snippet", "title", "status", "category".

        Args:
            finding: The MultimediaFinding to convert.

        Returns:
            A dict compatible with QualityPipeline.filter_and_score().
        """
        # Snippet is first 200 characters of text_content (Req 10.5)
        snippet = finding.text_content[:200]

        return {
            "name": finding.name,
            "url": finding.url or "",
            "snippet": snippet,
            "title": finding.name,
            "status": "Active/Accessible",
            "category": finding.media_type.value,
        }

    def _compute_relevance_score(self, text_content: str, target: str) -> float:
        """Compute relevance score from case-insensitive target occurrences.

        Uses the formula: count / max(count, 10) to normalize to [0.0, 1.0].
        - Empty text or empty target → 0.0
        - At least 1 mention → score > 0.0
        - 10+ mentions → score approaches or equals 1.0

        Args:
            text_content: The text to search for target occurrences.
            target: The target string to count (case-insensitive).

        Returns:
            A float in [0.0, 1.0].
        """
        if not text_content or not target:
            return 0.0

        count = text_content.lower().count(target.lower())

        if count == 0:
            return 0.0

        # Normalize: count / max(count, 10) ensures score in (0.0, 1.0]
        return count / max(count, 10)

    def _extract_gps(self, metadata: dict) -> Optional[Tuple[float, float]]:
        """Extract GPS coordinates from metadata.

        Expects metadata["gps"] to be a dict with "lat" and "lng" keys,
        or a tuple/list of (latitude, longitude).

        Args:
            metadata: The extraction metadata dict.

        Returns:
            A (latitude, longitude) tuple, or None if GPS data is absent.
        """
        gps_data = metadata.get("gps")
        if gps_data is None:
            return None

        if isinstance(gps_data, dict):
            lat = gps_data.get("lat")
            lng = gps_data.get("lng")
            if lat is not None and lng is not None:
                return (float(lat), float(lng))
        elif isinstance(gps_data, (list, tuple)) and len(gps_data) >= 2:
            return (float(gps_data[0]), float(gps_data[1]))

        return None
