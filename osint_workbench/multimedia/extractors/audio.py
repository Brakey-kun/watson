"""Audio extractor for the Multimedia Intelligence Pipeline.

Extracts audio metadata (duration, sample rate, channels, codec) via pydub,
transcribes audio content via whisper, detects spoken language, and performs
entity extraction on transcripts.
"""

import re
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

# Graceful imports for optional dependencies
try:
    from pydub import AudioSegment
    from pydub.utils import mediainfo

    _PYDUB_AVAILABLE = True
except ImportError:
    _PYDUB_AVAILABLE = False

try:
    import whisper

    _WHISPER_AVAILABLE = True
except ImportError:
    _WHISPER_AVAILABLE = False


# Supported audio MIME types
SUPPORTED_AUDIO_TYPES: List[str] = [
    "audio/mpeg",
    "audio/wav",
    "audio/ogg",
    "audio/flac",
    "audio/mp4",
]

# Entity extraction patterns
_EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
_URL_PATTERN = re.compile(
    r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%\-]+"
)
_PHONE_PATTERN = re.compile(
    r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}\b"
)
_CAPITALIZED_PHRASE_PATTERN = re.compile(
    r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b"
)


def extract_entities_from_text(text: str) -> List[str]:
    """Extract entities from text using simple regex-based NER.

    Extracts:
    - Capitalized multi-word phrases (potential names, organizations)
    - Email addresses
    - URLs
    - Phone numbers

    Args:
        text: The text to extract entities from.

    Returns:
        Deduplicated list of extracted entity strings.
    """
    if not text or not text.strip():
        return []

    entities: List[str] = []

    # Extract capitalized phrases (potential names/organizations)
    for match in _CAPITALIZED_PHRASE_PATTERN.finditer(text):
        entities.append(match.group())

    # Extract email addresses
    for match in _EMAIL_PATTERN.finditer(text):
        entities.append(match.group())

    # Extract URLs
    for match in _URL_PATTERN.finditer(text):
        entities.append(match.group())

    # Extract phone numbers
    for match in _PHONE_PATTERN.finditer(text):
        entities.append(match.group())

    # Deduplicate while preserving order
    seen = set()
    deduplicated = []
    for entity in entities:
        if entity not in seen:
            seen.add(entity)
            deduplicated.append(entity)

    return deduplicated


class AudioExtractor(BaseExtractor):
    """Extracts audio metadata, transcription, language, and entities.

    Uses pydub for audio metadata extraction and whisper for transcription.
    Performs regex-based entity extraction on transcribed text.
    Never raises exceptions to the caller — all errors are captured in
    ExtractionResult.error field.
    """

    def __init__(self, config: Optional[MultimediaConfig] = None) -> None:
        """Initialize the AudioExtractor.

        Args:
            config: Pipeline configuration. Uses defaults if not provided.
        """
        self._config = config or MultimediaConfig()
        self._whisper_model = None

    def supports(self) -> List[str]:
        """Return the list of supported audio MIME types.

        Returns:
            List of supported audio MIME type strings.
        """
        return list(SUPPORTED_AUDIO_TYPES)

    def max_file_size_bytes(self) -> int:
        """Return the maximum audio file size in bytes.

        Returns:
            Maximum file size derived from config's max_audio_size_mb.
        """
        return int(self._config.max_audio_size_mb * 1024 * 1024)

    def extract(self, artifact: MediaArtifact) -> ExtractionResult:
        """Extract intelligence content from an audio artifact.

        Performs metadata extraction, transcription (if enabled), language
        detection, and entity extraction. Never raises — all errors are
        captured in the result's error field.

        Args:
            artifact: The audio media artifact to process.

        Returns:
            ExtractionResult with extracted content, metadata, and confidence.
        """
        start_time = time.time()
        metadata: dict = {}
        text_content: str = ""
        entities: List[str] = []
        confidence: float = 0.0
        language: Optional[str] = None
        error: Optional[str] = None

        try:
            # Req 5.5: Check MIME type support
            if artifact.mime_type not in SUPPORTED_AUDIO_TYPES:
                processing_time = (time.time() - start_time) * 1000
                return ExtractionResult(
                    artifact_id=artifact.artifact_id,
                    media_type=MediaType.AUDIO,
                    text_content="",
                    metadata={},
                    entities=[],
                    confidence=0.0,
                    language=None,
                    error=f"Unsupported MIME type: {artifact.mime_type}",
                    processing_time_ms=processing_time,
                )

            # Req 5.6: Check file size limit
            if artifact.file_size_bytes > self.max_file_size_bytes():
                processing_time = (time.time() - start_time) * 1000
                return ExtractionResult(
                    artifact_id=artifact.artifact_id,
                    media_type=MediaType.AUDIO,
                    text_content="",
                    metadata={},
                    entities=[],
                    confidence=0.0,
                    language=None,
                    error=(
                        f"File size {artifact.file_size_bytes} bytes exceeds "
                        f"maximum allowed {self.max_file_size_bytes()} bytes"
                    ),
                    processing_time_ms=(time.time() - start_time) * 1000,
                )

            # Req 5.1: Extract audio metadata
            metadata = self._extract_metadata(artifact)
            if metadata:
                confidence += 0.3

            # Req 5.2: Transcription (if enabled)
            if self._config.enable_transcription:
                transcript_result = self._transcribe(artifact)
                if transcript_result is not None:
                    text_content = transcript_result.get("text", "")
                    language = transcript_result.get("language")
                    if language:
                        metadata["language"] = language
                    if text_content:
                        confidence += 0.5

                        # Req 5.3: Entity extraction on transcript
                        entities = extract_entities_from_text(text_content)
                        if entities:
                            confidence += 0.2

        except Exception as e:
            # Req 5.4: Capture errors, preserve any successful metadata
            error = f"Audio extraction error: {str(e)}"
            confidence = 0.0

        processing_time = (time.time() - start_time) * 1000

        return ExtractionResult(
            artifact_id=artifact.artifact_id,
            media_type=MediaType.AUDIO,
            text_content=text_content,
            metadata=metadata,
            entities=entities,
            confidence=min(confidence, 1.0),
            language=language,
            error=error,
            processing_time_ms=processing_time,
        )

    def _extract_metadata(self, artifact: MediaArtifact) -> dict:
        """Extract audio metadata using pydub.

        Args:
            artifact: The audio artifact to extract metadata from.

        Returns:
            Dictionary with duration_seconds, sample_rate, channels, codec.
        """
        if not _PYDUB_AVAILABLE:
            return {}

        if artifact.local_path is None or not Path(artifact.local_path).exists():
            return {}

        metadata: dict = {}
        try:
            audio = AudioSegment.from_file(str(artifact.local_path))
            metadata["duration_seconds"] = len(audio) / 1000.0
            metadata["sample_rate"] = audio.frame_rate
            metadata["channels"] = audio.channels
        except Exception:
            # Partial metadata extraction failure is not fatal
            pass

        # Try to get codec info via mediainfo
        try:
            info = mediainfo(str(artifact.local_path))
            if info and "codec_name" in info:
                metadata["codec"] = info["codec_name"]
            elif info and "format_name" in info:
                metadata["codec"] = info["format_name"]
        except Exception:
            pass

        return metadata

    def _transcribe(self, artifact: MediaArtifact) -> Optional[dict]:
        """Transcribe audio content using whisper.

        Args:
            artifact: The audio artifact to transcribe.

        Returns:
            Dictionary with 'text' and 'language' keys, or None on failure.
        """
        if not _WHISPER_AVAILABLE:
            return None

        if artifact.local_path is None or not Path(artifact.local_path).exists():
            return None

        try:
            if self._whisper_model is None:
                self._whisper_model = whisper.load_model("base")

            result = self._whisper_model.transcribe(str(artifact.local_path))
            return {
                "text": result.get("text", ""),
                "language": result.get("language"),
            }
        except Exception:
            return None
