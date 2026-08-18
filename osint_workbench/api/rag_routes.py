"""RAG context ingest + investigation-insights HTTP surface.

Split out of routes.py's create_api_blueprint() into its own narrow
Blueprint so it can be registered by BOTH Flask hosts this project ships:
osint_workbench/app.py's create_app() (the modern, SocketIO-backed path)
and gui.py (the legacy dashboard actually launched by run.bat/run.sh).
Folding the whole api_blueprint into gui.py isn't an option -- both hosts
already define their own /api/run, /api/status, /api/list-models, etc.,
and registering two blueprints with the same route would collide.

Both routes read their storage handles (SteeringIndex/OutcomeMemory/
PlanStore/LLMClient/AppConfig) from `current_app.config` at request time,
matching create_api_blueprint's existing convention -- each host is
responsible for populating those keys (see gui.py and app.py's create_app
for the two different ways they do it: app.py builds everything once at
startup; gui.py refreshes APP_CONFIG/LLM_CLIENT fresh before each request
via this blueprint's before_request hook, consistent with its "config.json
changes apply on the very next call, no restart" design).
"""

import logging
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request
from werkzeug.utils import secure_filename

from osint_workbench.core.events import Event, EventBus, EventType
from osint_workbench.core.models import ClaimStatus
from osint_workbench.core import project_store
from osint_workbench.core.rag_ingest import extract_upload_text, ingest_context
from osint_workbench.core.steering_index import SteeringIndex
from osint_workbench.multimedia.models import MediaType
from osint_workbench.multimedia.router import EXTENSION_MAP, EXTENSION_TO_MIME

logger = logging.getLogger(__name__)


def create_rag_blueprint(
    event_bus: EventBus,
    get_investigation_id: Callable[[], str],
) -> Blueprint:
    """Create the RAG ingest / investigation-insights Blueprint.

    Args:
        event_bus: EventBus to emit EXTRACTION_COMPLETE/EXTRACTION_FAILED
            on. Must have at least one live subscriber (a SocketIO
            forwarder) for the frontend to see the outcome of a background
            ingest -- see each host's registration site for how it's wired.
        get_investigation_id: Returns the currently tracked investigation
            id, or "" / None if none is active. Used only as an event-
            routing label and as investigation-insights' default when the
            `investigation_id` query param is omitted.

    Returns:
        Configured Flask Blueprint.
    """
    rag = Blueprint("rag", __name__)

    # --- POST /api/upload-context ---
    @rag.route("/api/upload-context", methods=["POST"])
    def upload_context():
        """Ingest user-provided context into the steering index as
        trust-tiered RAG hints the research loop reads back every replan
        and at burst-search round-1 seeding.

        Accepts either a multipart file upload (`file` field --
        text/markdown/pdf/docx/image, plus optional `target` form field)
        or a JSON typed-text snippet (`text` field, highest trust, plus
        optional `target`). `target` lets the frontend pass whatever's
        currently in the investigation-target input even before an
        investigation has started.

        Extraction runs synchronously (fast: file read or OCR); the
        thinker-tier slot-tagging LLM call is slow, so it's dispatched to
        a background thread and this endpoint returns immediately rather
        than blocking the SocketIO worker.
        """
        steering_index = current_app.config.get("STEERING_INDEX")
        llm_client = current_app.config.get("LLM_CLIENT")
        app_config = current_app.config.get("APP_CONFIG")
        if steering_index is None or llm_client is None or app_config is None:
            return jsonify({"success": False, "error": "RAG ingest is not available"}), 500

        tier_model = app_config.tiers.thinker.model or None
        tier_temperature = app_config.tiers.thinker.temperature

        if "file" in request.files:
            upload = request.files["file"]
            if not upload.filename:
                return jsonify({"success": False, "error": "No file selected"}), 400

            # Only the extension is trusted, and only to pick an
            # extractor/MIME type -- never joined into a filesystem path.
            ext = Path(upload.filename).suffix.lower()
            display_filename = secure_filename(upload.filename) or ext
            media_type = EXTENSION_MAP.get(ext)
            if media_type not in (MediaType.IMAGE, MediaType.DOCUMENT):
                return (
                    jsonify({
                        "success": False,
                        "error": f"Unsupported file type '{ext}'. Supported: "
                                 f"text, markdown, pdf, docx, and common image formats.",
                    }),
                    400,
                )
            mime_type = EXTENSION_TO_MIME.get(ext, "application/octet-stream")
            target = request.form.get("target", "").strip() or "the current investigation subject"
            project_id = request.form.get("project_id", "").strip() or None

            tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext)
            os.close(tmp_fd)
            try:
                upload.save(tmp_path)
            except Exception as exc:
                os.unlink(tmp_path)
                logger.error("Failed to save uploaded context file: %s", exc)
                return jsonify({"success": False, "error": "Failed to save uploaded file"}), 500

            def _ingest_file():
                event_investigation_id = get_investigation_id() or ""
                try:
                    text, trust_tier, extract_error = extract_upload_text(
                        Path(tmp_path), mime_type, media_type, investigation_id="rag-context-upload",
                    )
                    if extract_error:
                        logger.warning("RAG upload extraction issue for '%s': %s", display_filename, extract_error)
                        event_bus.emit(Event(
                            type=EventType.EXTRACTION_FAILED,
                            investigation_id=event_investigation_id,
                            data={"source": "upload", "filename": display_filename, "error": extract_error},
                        ))
                        return
                    result = ingest_context(
                        text, target=target, trust_tier=trust_tier,
                        llm_client=llm_client, steering_index=steering_index,
                        project_id=project_id,
                        model=tier_model, temperature=tier_temperature,
                    )
                    logger.info(
                        "RAG ingest wrote %d hint(s) from '%s' upload (trust_tier=%s)",
                        result.slot_count, display_filename, trust_tier,
                    )
                    event_bus.emit(Event(
                        type=EventType.EXTRACTION_COMPLETE,
                        investigation_id=event_investigation_id,
                        data={
                            "source": "upload", "filename": display_filename, "trust_tier": trust_tier,
                            "slot_count": result.slot_count, "error": result.error,
                        },
                    ))
                except Exception as exc:
                    # Never let an unexpected failure vanish silently to a
                    # daemon thread's stderr with zero user-visible trace.
                    logger.error("RAG upload ingest thread failed: %s", exc)
                    event_bus.emit(Event(
                        type=EventType.EXTRACTION_FAILED,
                        investigation_id=event_investigation_id,
                        data={"source": "upload", "filename": display_filename, "error": str(exc)},
                    ))
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

            threading.Thread(target=_ingest_file, daemon=True).start()
            return jsonify({"success": True, "status": "processing"}), 200

        data = request.get_json(force=True, silent=True) or {}
        text = str(data.get("text", "")).strip()
        if not text:
            return jsonify({"success": False, "error": "No file or text provided"}), 400
        target = str(data.get("target", "")).strip() or "the current investigation subject"
        project_id = str(data.get("project_id") or "").strip() or None

        def _ingest_snippet():
            event_investigation_id = get_investigation_id() or ""
            try:
                result = ingest_context(
                    text, target=target, trust_tier="typed_snippet",
                    llm_client=llm_client, steering_index=steering_index,
                    project_id=project_id,
                    model=tier_model, temperature=tier_temperature,
                )
                logger.info("RAG ingest wrote %d hint(s) from typed snippet", result.slot_count)
                event_bus.emit(Event(
                    type=EventType.EXTRACTION_COMPLETE,
                    investigation_id=event_investigation_id,
                    data={
                        "source": "snippet", "trust_tier": "typed_snippet",
                        "slot_count": result.slot_count, "error": result.error,
                    },
                ))
            except Exception as exc:
                logger.error("RAG snippet ingest thread failed: %s", exc)
                event_bus.emit(Event(
                    type=EventType.EXTRACTION_FAILED,
                    investigation_id=event_investigation_id,
                    data={"source": "snippet", "error": str(exc)},
                ))

        threading.Thread(target=_ingest_snippet, daemon=True).start()
        return jsonify({"success": True, "status": "processing"}), 200

    # --- GET /api/investigation-insights ---
    @rag.route("/api/investigation-insights", methods=["GET"])
    def investigation_insights():
        """Read-only snapshot for the dashboard's visibility panel:
        pheromone-weighted steering-index entries (RAG hints + learned
        source-category yields), unresolved claims, and the current plan
        timeline for one investigation.

        Query param `investigation_id` selects the investigation (defaults
        to the currently tracked one, if any). Query param `project_id`
        selects which investigation's independent scope to read hints
        from -- when present, hints are read ONLY from that project's own
        steering scope (see project_store.steering_scope), mirroring
        engine.py's _format_hint_context exclusive-read contract, so a
        different investigation's hints can never leak into this panel.
        With no project_id, falls back to SteeringIndex.GLOBAL_SCOPE plus
        the given investigation_id's own scope (pre-Projects behavior, for
        callers that never had a project_id to begin with).
        """
        steering_index = current_app.config.get("STEERING_INDEX")
        outcome_memory = current_app.config.get("OUTCOME_MEMORY")
        plan_store = current_app.config.get("PLAN_STORE")

        investigation_id = request.args.get("investigation_id") or get_investigation_id()
        project_id = request.args.get("project_id") or None

        hints = []
        source_categories = []
        if steering_index is not None:
            if project_id:
                hint_entries = list(
                    steering_index.top(project_store.steering_scope(project_id), "hint", k=20)
                )
            else:
                hint_entries = list(steering_index.top(SteeringIndex.GLOBAL_SCOPE, "hint", k=20))
                if investigation_id:
                    hint_entries += steering_index.top(investigation_id, "hint", k=20)
            hints = [
                {
                    "payload": e.payload,
                    "pheromone": round(e.pheromone, 3),
                    "trust_tier": e.trust_tier,
                    "reinforce_count": e.reinforce_count,
                }
                for e in hint_entries
            ]
            source_categories = [
                {"category": e.payload, "pheromone": round(e.pheromone, 3)}
                for e in steering_index.top(SteeringIndex.GLOBAL_SCOPE, "source_category", k=20)
            ]

        claims = []
        if outcome_memory is not None and investigation_id:
            claims = [
                {
                    "subject": c.subject,
                    "predicate": c.predicate,
                    "value": c.value,
                    "status": c.status.value,
                    "confidence": round(c.confidence, 3),
                    "verify_attempts": c.verify_attempts,
                }
                for c in outcome_memory.get_claims(investigation_id)
                if c.status != ClaimStatus.CONFIRMED
            ]

        plan = None
        if plan_store is not None and investigation_id:
            loaded_plan = plan_store.load_latest(investigation_id)
            if loaded_plan is not None:
                plan = {
                    "epoch": loaded_plan.epoch,
                    "state": loaded_plan.state.value,
                    "subject_profile": loaded_plan.subject_profile,
                    "hypotheses": [
                        {"statement": h.statement, "status": h.status.value}
                        for h in loaded_plan.hypotheses
                    ],
                    "queued_queries": len(loaded_plan.queued_queries),
                    "rounds_since_replan": loaded_plan.rounds_since_replan,
                }

        return jsonify({
            "success": True,
            "investigation_id": investigation_id,
            "project_id": project_id,
            "hints": hints,
            "source_categories": source_categories,
            "claims": claims,
            "plan": plan,
        }), 200

    return rag
