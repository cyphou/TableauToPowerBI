"""Cross-artifact semantic and Power Query validation contract."""

from __future__ import annotations

from typing import Any, Dict, Mapping


def build_validation_contract(
    semantic_context: Mapping[str, Any],
    m_emitters: Mapping[str, Any],
    recovery: Mapping[str, Any],
) -> Dict[str, Any]:
    """Summarize existing validation evidence without changing policy outcomes."""
    semantic_execution = semantic_context.get("execution", {}) or {}
    semantic_status = _status_from_execution(semantic_execution)
    static_issues = sum(int(semantic_context.get(key, 0) or 0) for key in ("issue_count",))
    static_issues += int((semantic_context.get("measure_context", {}) or {}).get("issue_count", 0) or 0)
    static_issues += int((semantic_context.get("filter_context", {}) or {}).get("issue_count", 0) or 0)
    emitter_summary = m_emitters.get("summary", {}) or {}
    fallback_count = len(m_emitters.get("fallback_in_use", []) or [])
    error_count = int(emitter_summary.get("errors", 0) or 0)
    pending_count = int((recovery.get("counts", {}) or {}).get("pending_validation", 0) or 0)
    return {
        "schema_version": "1.0",
        "status": "static_evidence",
        "semantic": {
            "static_status": "failed" if static_issues else "passed",
            "static_issue_count": static_issues,
            "execution_status": semantic_status,
            "runtime": "not_run" if semantic_status == "not_run" else "evidence_recorded",
        },
        "m": {
            "status": "failed" if error_count else ("warning" if fallback_count else "passed"),
            "fallback_in_use": fallback_count,
            "emitter_errors": error_count,
        },
        "recovery": {
            "status": "pending" if pending_count else "complete",
            "pending_validation": pending_count,
        },
        "release_ready": (
            not static_issues
            and semantic_status == "passed"
            and not error_count
            and not pending_count
        ),
    }


def _status_from_execution(execution: Mapping[str, Any]) -> str:
    status = str(execution.get("status", "not_run"))
    if status in {"passed", "failed", "not_run"}:
        return status
    return "failed" if execution.get("failed", 0) else "not_run"
