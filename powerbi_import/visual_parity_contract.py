"""Source-aware Power BI visual parity evidence."""

from __future__ import annotations

from typing import Any, Dict, Mapping

from powerbi_import.visual_mapping_matrix import find_visual_approximations_in_use


def build_visual_parity_contract(
    extracted: Mapping[str, Any],
    mapping_evidence: Mapping[str, Any],
    recovery_evidence: Mapping[str, Any],
    role_evidence: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Combine visual mapping coverage with generated PBIR recovery state."""
    summary = mapping_evidence.get("summary", {}) or {}
    status_counts = summary.get("status_counts", {}) or {}
    approximations = find_visual_approximations_in_use(dict(extracted))
    recovery_counts = recovery_evidence.get("counts", {}) or {}
    risk_count = int(recovery_counts.get("empty", 0) or 0) + int(
        recovery_counts.get("orphaned", 0) or 0
    )
    return {
        "schema_version": "2.0",
        "status": "static_evidence",
        "registry": {
            "native": int(status_counts.get("native", 0) or 0),
            "approximation": int(status_counts.get("approximation", 0) or 0),
            "custom_visual": int(status_counts.get("custom_visual", 0) or 0),
        },
        "source_used": {
            "approximation_count": len(approximations),
            "approximations": approximations,
        },
        "target_recovery": {
            "visual_count": int(recovery_evidence.get("visual_count", 0) or 0),
            "valid": int(recovery_counts.get("valid", 0) or 0),
            "empty": int(recovery_counts.get("empty", 0) or 0),
            "orphaned": int(recovery_counts.get("orphaned", 0) or 0),
            "risk_count": risk_count,
        },
        "runtime": "not_run",
        "role_contract": {
            "status": (role_evidence or {}).get("status", "not_run"),
            "visual_count": int((role_evidence or {}).get("visual_count", 0) or 0),
            "valid": int((role_evidence or {}).get("valid", 0) or 0),
            "needs_review": int((role_evidence or {}).get("needs_review", 0) or 0),
            "visuals": list((role_evidence or {}).get("visuals", []) or []),
        },
        "remediation_owner": "@visual" if len(approximations) or risk_count else None,
    }
