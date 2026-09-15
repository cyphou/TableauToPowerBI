"""Round-trip and visual validation evidence boundary."""

from __future__ import annotations

from typing import Any, Mapping


def build_roundtrip_validation(
    openability: Mapping[str, Any],
    visual_recovery: Mapping[str, Any],
    visual_parity: Mapping[str, Any],
) -> dict[str, Any]:
    """Compose static validation evidence without inferring Desktop behavior."""
    openable = bool(openability.get("openable", False))
    issues = list(openability.get("blocking_issues", []) or [])
    visual_counts = visual_recovery.get("counts", {}) or {}
    visual_risks = int(visual_counts.get("empty", 0) or 0) + int(
        visual_counts.get("orphaned", 0) or 0
    )
    static_status = "static_pass" if openable and not visual_risks else "static_fail"
    return {
        "schema_version": "1.0",
        "status": "static_evidence",
        "static": {
            "status": static_status,
            "openability": "passed" if openable else "failed",
            "blocking_issue_count": len(issues),
            "visual_risk_count": visual_risks,
            "issues": issues,
        },
        "desktop": {
            "status": "not_run",
            "reopen": "not_run",
            "required_for": "DESKTOP_REOPEN_PASS",
        },
        "visual": {
            "runtime": "not_run",
            "parity_runtime": visual_parity.get("runtime", "not_run"),
            "screenshot_comparison": "not_run",
        },
        "post_repair_revalidation": "required" if visual_risks else "not_required",
    }
