"""Model-selector HTTP surface: GPU VRAM detection + active-model switch.

Split into its own narrow Blueprint following rag_routes.py's convention so
it can be registered by BOTH Flask hosts this project ships. Reads its
storage/config dependencies (CONFIG_LOADER, APP_CONFIG, LLM_CLIENT) from
`current_app.config` at request time -- each host is responsible for
populating those keys before registering this blueprint.
"""

import dataclasses
import logging

from flask import Blueprint, current_app, jsonify, request

from osint_workbench.core import vram_detect
from osint_workbench.engine_factory import resolve_backend_params

logger = logging.getLogger(__name__)


def create_model_blueprint() -> Blueprint:
    """Create the model-selector Blueprint (GET /api/detect-vram,
    POST /api/set-active-model)."""
    model_bp = Blueprint("model", __name__)

    # --- GET /api/detect-vram ---
    @model_bp.route("/api/detect-vram", methods=["GET"])
    def detect_vram():
        """Best-effort GPU VRAM probe + a proposed context-length starting
        point for the model selector UI.

        Never surfaces a 500 -- this is a best-effort hardware probe, and
        "not detected" is a normal, expected outcome the UI falls back to a
        manual override prompt for, not an error.
        """
        try:
            config_loader = current_app.config.get("CONFIG_LOADER")
            app_config = current_app.config.get("APP_CONFIG")
            if config_loader is None or app_config is None:
                return jsonify({"success": False, "error": "Configuration not available"}), 500

            model_id = request.args.get("model")
            if not model_id:
                model_id = resolve_backend_params(app_config)[1]

            max_context_length = request.args.get("max_context_length")
            if max_context_length is not None:
                max_context_length = int(max_context_length)
            else:
                max_context_length = app_config.llm.max_context_tokens

            result = vram_detect.detect_vram_gb()
            if not result.detected:
                return jsonify({
                    "success": True,
                    "vram_gb": None,
                    "source": result.source,
                    "detected": False,
                    "proposed_context_length": None,
                }), 200

            proposed_context_length = vram_detect.propose_context_length(
                result.vram_gb, model_id, max_context_length,
            )
            return jsonify({
                "success": True,
                "vram_gb": result.vram_gb,
                "source": result.source,
                "detected": True,
                "proposed_context_length": proposed_context_length,
            }), 200
        except Exception:
            logger.exception("VRAM detection failed unexpectedly")
            return jsonify({
                "success": True,
                "vram_gb": None,
                "source": "none",
                "detected": False,
                "proposed_context_length": None,
            }), 200

    # --- POST /api/set-active-model ---
    @model_bp.route("/api/set-active-model", methods=["POST"])
    def set_active_model():
        """Assign a model (and optional context length) to a backend and
        persist it, mirroring switch_backend's revert-on-failure contract.
        """
        data = request.get_json(force=True, silent=True) or {}

        model = data.get("model")
        if not model or not str(model).strip():
            return jsonify({"success": False, "error": "Missing required field: model"}), 400
        model = str(model)

        config_loader = current_app.config.get("CONFIG_LOADER")
        app_config = current_app.config.get("APP_CONFIG")
        if config_loader is None or app_config is None:
            return jsonify({"success": False, "error": "Configuration not available"}), 500

        backend_name = data.get("backend") or app_config.llm.backend
        if backend_name not in app_config.backends:
            return (
                jsonify({
                    "success": False,
                    "error": f"Backend '{backend_name}' not found in configuration",
                }),
                400,
            )

        # Validate the proposed backend state (same rigor switch_backend
        # applies) -- catches e.g. a model id over the 128-char limit that
        # the bare non-blank check above doesn't.
        backend_obj = app_config.backends[backend_name]
        proposed_backend_dict = {
            "endpoint": backend_obj.endpoint,
            "api_key": backend_obj.api_key,
            "model": model,
            "temperature": backend_obj.temperature,
            "last_tested": backend_obj.last_tested,
        }
        validation_errors = config_loader.validate_backend(backend_name, proposed_backend_dict)
        if validation_errors:
            error_details = "; ".join(f"{e.field_name}: {e.detail}" for e in validation_errors)
            return (
                jsonify({
                    "success": False,
                    "error": f"Backend '{backend_name}' failed validation: {error_details}",
                }),
                400,
            )

        clamped_context_length = None
        if "context_length" in data and data.get("context_length") is not None:
            try:
                context_length = int(data.get("context_length"))
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": "context_length must be an integer"}), 400
            clamped_context_length = max(
                vram_detect.MIN_PROPOSED_CONTEXT,
                min(vram_detect.MAX_PROPOSED_CONTEXT, context_length),
            )

        old_model = backend_obj.model
        old_max_context_tokens = app_config.llm.max_context_tokens

        backend_obj.model = model
        if clamped_context_length is not None:
            app_config.llm.max_context_tokens = clamped_context_length

        try:
            config_loader.save(dataclasses.asdict(app_config))
        except Exception as exc:
            backend_obj.model = old_model
            app_config.llm.max_context_tokens = old_max_context_tokens
            logger.error("Failed to persist active model switch to disk: %s", exc)
            return jsonify({"success": False, "error": f"Failed to save configuration: {exc}"}), 500

        if backend_name == app_config.llm.backend:
            llm_client = current_app.config.get("LLM_CLIENT")
            if llm_client is not None:
                llm_client.model = model
                llm_client.model_autodetected = False

        return jsonify({
            "success": True,
            "backend": backend_name,
            "model": model,
            "max_context_tokens": app_config.llm.max_context_tokens,
        }), 200

    return model_bp
