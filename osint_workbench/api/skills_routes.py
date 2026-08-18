"""Skills (kb_lessons) management HTTP surface.

Wraps osint_workbench.core.outcome_memory.OutcomeMemory's kb_lessons table
as the "Skills" UI panel: list every lesson (staged/active/demoted), toggle
a lesson's user-facing active flag, and delete a lesson outright.

Reads its OutcomeMemory handle from `current_app.config["OUTCOME_MEMORY"]`
at request time, matching create_rag_blueprint's convention -- each host
(osint_workbench/app.py's create_app() and the legacy gui.py dashboard) is
responsible for populating that key before registering this blueprint.
"""

import dataclasses

from flask import Blueprint, current_app, jsonify, request


def _lesson_to_dict(lesson) -> dict:
    return {**dataclasses.asdict(lesson), "stage": lesson.stage.value}


def create_skills_blueprint() -> Blueprint:
    """Create the Skills (kb_lessons) management Blueprint."""
    skills = Blueprint("skills", __name__)

    # --- GET /api/skills ---
    @skills.route("/api/skills", methods=["GET"])
    def list_skills():
        outcome_memory = current_app.config.get("OUTCOME_MEMORY")
        if outcome_memory is None:
            return jsonify({"success": False, "error": "Outcome memory not available"}), 500

        subject_type = request.args.get("subject_type") or None
        lessons = outcome_memory.list_all_lessons(subject_type=subject_type)
        return jsonify({"success": True, "skills": [_lesson_to_dict(lesson) for lesson in lessons]}), 200

    # --- POST /api/skills/<lesson_id>/toggle ---
    @skills.route("/api/skills/<lesson_id>/toggle", methods=["POST"])
    def toggle_skill(lesson_id):
        outcome_memory = current_app.config.get("OUTCOME_MEMORY")
        if outcome_memory is None:
            return jsonify({"success": False, "error": "Outcome memory not available"}), 500

        data = request.get_json(force=True, silent=True) or {}
        if "active" not in data:
            return jsonify({"success": False, "error": "Missing required field: active"}), 400

        lesson = outcome_memory.set_lesson_active(lesson_id, bool(data["active"]))
        if lesson is None:
            return jsonify({"success": False, "error": f"Lesson '{lesson_id}' not found"}), 404

        return jsonify({"success": True, "skill": _lesson_to_dict(lesson)}), 200

    # --- DELETE /api/skills/<lesson_id> ---
    @skills.route("/api/skills/<lesson_id>", methods=["DELETE"])
    def delete_skill(lesson_id):
        outcome_memory = current_app.config.get("OUTCOME_MEMORY")
        if outcome_memory is None:
            return jsonify({"success": False, "error": "Outcome memory not available"}), 500

        deleted = outcome_memory.delete_lesson(lesson_id)
        if not deleted:
            return jsonify({"success": False, "error": f"Lesson '{lesson_id}' not found"}), 404

        return jsonify({"success": True, "deleted": True}), 200

    return skills
