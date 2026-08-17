"""RAG context ingestion: turns user-provided text/doc/image context into
trust-tiered SteeringIndex 'hint' rows the research loop reads back via
`OSINTEngine._build_replan_prompt` / `_build_burst_search_prompt`.

Three provenance tiers (TRUST_TIER_WEIGHTS in steering_index.py):
- typed_snippet (1.0): raw text pasted by the user directly -- no
  extraction step, highest trust.
- document (0.8): text pulled from an uploaded text/markdown/pdf/docx file.
- ocr_image (0.6): text OCR'd from an uploaded image -- lowest trust since
  OCR itself is error-prone.

All hints are written to SteeringIndex.GLOBAL_SCOPE rather than a specific
investigation_id. This is deliberate, not an oversight: routes.py mints its
own investigation_id for `_state["investigation_id"]` at run start, but
`OSINTEngine.run_investigation` mints a SEPARATE one internally for the
actual InvestigationState/InvestigationPlan -- the two never match while a
run is in flight. Scoping hints to "the active investigation_id" would
silently write them under an id `_build_replan_prompt` never queries.
GLOBAL_SCOPE sidesteps that mismatch entirely (same reasoning already
applied to `source_category` rows) at the cost of hints persisting beyond
one investigation, which is an acceptable, even useful, tradeoff for
user-supplied background context.

A thinker-tier LLM call turns the raw extracted text into a small set of
typed "slots" (concrete facts/leads an investigator would act on) instead
of dumping the whole raw blob into steering_index -- keeps hint rows short,
fingerprint-dedupable, and directly interpolatable into prompts.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

from osint_workbench.core.llm_client import LLMClient, LLMClientError
from osint_workbench.core.steering_index import SteeringIndex
from osint_workbench.multimedia.extractors.document import DocumentExtractor
from osint_workbench.multimedia.extractors.image import ImageExtractor
from osint_workbench.multimedia.models import MediaArtifact, MediaType

logger = logging.getLogger(__name__)

# Cap on raw extracted text handed to the thinker tagging call -- keeps a
# large upload from blowing the token budget on one ingest.
MAX_INGEST_CHARS = 8000

_IMAGE_EXTRACTOR = ImageExtractor()
_DOCUMENT_EXTRACTOR = DocumentExtractor()


@dataclass
class IngestResult:
    """Outcome of one ingest_context() call."""

    slot_count: int
    slots: list = field(default_factory=list)
    error: Optional[str] = None


def extract_upload_text(
    path: Path, mime_type: str, media_type: MediaType, investigation_id: str
) -> Tuple[str, str, Optional[str]]:
    """Extract raw text from an uploaded file and pick its trust tier.

    Args:
        path: Local path to the uploaded file (caller owns cleanup).
        mime_type: The file's MIME type, already validated against
            MultimediaConfig's supported-types allowlist by the caller.
        media_type: MediaType.IMAGE or MediaType.DOCUMENT.
        investigation_id: Used only to satisfy MediaArtifact's validation;
            not the scope hints get written under (see module docstring).

    Returns:
        (text_content, trust_tier, error). Never raises -- extractor
        failures are already captured in ExtractionResult.error, and any
        MediaArtifact validation failure here yields empty text with the
        error set rather than propagating.
    """
    if media_type not in (MediaType.IMAGE, MediaType.DOCUMENT):
        return "", "document", f"Unsupported media type for context ingest: {media_type.value}"

    try:
        artifact = MediaArtifact(
            artifact_id=str(uuid.uuid4()),
            source_url=None,
            local_path=path,
            media_type=media_type,
            mime_type=mime_type,
            file_size_bytes=max(1, path.stat().st_size),
            investigation_id=investigation_id,
        )
    except ValueError as exc:
        return "", "document", str(exc)

    extractor = _IMAGE_EXTRACTOR if media_type == MediaType.IMAGE else _DOCUMENT_EXTRACTOR
    result = extractor.extract(artifact)
    trust_tier = "ocr_image" if media_type == MediaType.IMAGE else "document"
    return result.text_content, trust_tier, result.error


def _build_slot_tagging_prompt(text: str, target: str) -> str:
    excerpt = text[:MAX_INGEST_CHARS]
    return (
        f"You are tagging user-supplied background context for an OSINT "
        f"investigation of target '{target}'.\n\n"
        f"--- CONTEXT TEXT ---\n{excerpt}\n--- END CONTEXT ---\n\n"
        f"Extract a short list of distinct, concrete facts or leads worth "
        f"steering future research toward (e.g. a name, alias, employer, "
        f"location, handle, email, phone number, or claim). Skip filler "
        f"and anything not concrete.\n\n"
        f"For each fact provide:\n"
        f"- 'slot_type': a short category label (e.g. 'alias', 'employer', 'location')\n"
        f"- 'value': the concrete fact, as short as possible\n\n"
        f"Return ONLY a JSON object: {{'slots': [array of objects with "
        f"'slot_type' and 'value']}}. If nothing concrete is present, "
        f"return {{'slots': []}}."
    )


def ingest_context(
    text: str,
    *,
    target: str,
    trust_tier: str,
    llm_client: LLMClient,
    steering_index: SteeringIndex,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
) -> IngestResult:
    """Thinker-tag raw context text into hints and write them into
    steering_index as GLOBAL_SCOPE entry_type='hint' rows.

    Never raises: LLM/parse failures are captured in IngestResult.error
    with zero slots written, so a bad upload can't take down the caller
    (typically a background thread off the upload request).
    """
    stripped = text.strip()
    if not stripped:
        return IngestResult(slot_count=0, error="No text to tag")

    prompt = _build_slot_tagging_prompt(stripped, target)
    try:
        response = llm_client.ask_json(
            prompt,
            system_prompt="You output strictly valid JSON.",
            model=model,
            temperature=temperature,
        )
    except LLMClientError as exc:
        logger.error("RAG ingest slot-tagging failed: %s", exc)
        return IngestResult(slot_count=0, error=str(exc))

    raw_slots = response.get("slots", [])
    if not isinstance(raw_slots, list):
        return IngestResult(slot_count=0, error="Malformed slot response: 'slots' is not a list")

    written = []
    for slot in raw_slots:
        if not isinstance(slot, dict):
            continue
        slot_type = str(slot.get("slot_type", "")).strip()
        value = str(slot.get("value", "")).strip()
        if not slot_type or not value:
            continue
        steering_index.add(
            SteeringIndex.GLOBAL_SCOPE, "hint",
            fingerprint=f"{slot_type}:{value.lower()}",
            payload=f"{slot_type}: {value}",
            trust_tier=trust_tier,
        )
        written.append({"slot_type": slot_type, "value": value})

    return IngestResult(slot_count=len(written), slots=written)
