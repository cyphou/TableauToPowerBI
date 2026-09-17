"""Compact reviewer summary for portable migration evidence."""

from __future__ import annotations

from typing import Any, Mapping


def build_evidence_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build a concise status view while preserving detailed evidence elsewhere."""
    inventory = payload.get("source_inventory", {}) or {}
    recovery = payload.get("recovery", {}) or {}
    validation = payload.get("validation_contract", {}) or {}
    visual = payload.get("visual_parity", {}) or {}
    roundtrip = payload.get("roundtrip_validation", {}) or {}
    visual_recovery = payload.get("visual_recovery", {}) or {}
    blockers = payload.get("blockers", []) or []
    warnings = payload.get("warnings", []) or []
    priorities = payload.get("priorities", []) or []
    return {
        "status": payload.get("status", "UNVERIFIED"),
        "handoff_status": payload.get("handoff_status", "UNVERIFIED"),
        "blocker_count": len(blockers),
        "warning_count": len(warnings),
        "priority_count": len(priorities),
        "inventory": {
            "object_count": int(inventory.get("object_count", 0) or 0),
            "orphan_count": int(inventory.get("orphan_count", 0) or 0),
        },
        "recovery": {
            "status": recovery.get("status", "not_available"),
            "pending_validation": int(
                (recovery.get("counts", {}) or {}).get("pending_validation", 0) or 0
            ),
        },
        "validation": {
            "release_ready": bool(validation.get("release_ready", False)),
            "semantic_execution": (
                (validation.get("semantic", {}) or {}).get("execution_status", "not_run")
            ),
            "m_status": (validation.get("m", {}) or {}).get("status", "not_available"),
        },
        "visual": {
            "source_approximation_count": int(
                (visual.get("source_used", {}) or {}).get("approximation_count", 0) or 0
            ),
            "target_risk_count": int(
                (visual.get("target_recovery", {}) or {}).get("risk_count", 0) or 0
            ),
            "recovery_status": visual_recovery.get("status", "not_available"),
        },
        "roundtrip": {
            "static_status": (roundtrip.get("static", {}) or {}).get(
                "status", "not_available"
            ),
            "desktop_status": (roundtrip.get("desktop", {}) or {}).get(
                "status", "not_run"
            ),
            "screenshot_comparison": (roundtrip.get("visual", {}) or {}).get(
                "screenshot_comparison", "not_run"
            ),
        },
    }
