"""Normalize assessment findings into an explainable evidence contract."""

from __future__ import annotations

from typing import Any, Dict


_OWNER_BY_SCOPE = {
    "datasource": "@wiring",
    "calculation": "@dax",
    "visual": "@visual",
    "filter": "@visual",
    "interactiv": "@visual",
    "semantic": "@semantic",
    "extract": "@extractor",
    "scope": "@assessor",
}


def build_assessment_evidence(report: Any) -> Dict[str, Any]:
    """Build deterministic, operator-facing evidence from an assessment report."""
    findings = []
    for category in getattr(report, "categories", []):
        category_name = str(category.name)
        owner = _owner_for(category_name)
        for index, check in enumerate(category.checks):
            severity = str(check.severity)
            state = "not_run" if severity == "not_run" else (
                "pass" if severity in {"pass", "info"} else severity
            )
            recommendation = str(check.recommendation or "")
            findings.append({
                "evidence_id": f"assessment.{_slug(category_name)}.{index}",
                "category": category_name,
                "name": str(check.name),
                "state": state,
                "source_scope": category_name,
                "target_scope": _target_scope(category_name),
                "owner": owner,
                "detail": str(check.detail),
                "next_action": recommendation or _default_action(state),
            })

    counts = {state: sum(item["state"] == state for item in findings)
              for state in ("pass", "warn", "fail", "not_run")}
    return {
        "schema_version": "1.0",
        "status": "static_evidence",
        "overall_score": getattr(report, "overall_score", "UNVERIFIED"),
        "counts": counts,
        "findings": findings,
    }


def _owner_for(category: str) -> str:
    normalized = category.lower()
    return next((owner for token, owner in _OWNER_BY_SCOPE.items() if token in normalized), "@assessor")


def _target_scope(category: str) -> str:
    normalized = category.lower()
    if "visual" in normalized or "interactiv" in normalized:
        return "PBIR"
    if "calculation" in normalized or "semantic" in normalized:
        return "TMDL/DAX"
    if "datasource" in normalized or "extract" in normalized:
        return "Power Query M"
    return "migration"


def _default_action(state: str) -> str:
    return {
        "pass": "Retain evidence and continue migration.",
        "warn": "Review the finding before release.",
        "fail": "Resolve the finding before release.",
        "not_run": "Run the authorized validation stage.",
    }.get(state, "Review the finding.")


def _slug(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "_" for char in value).strip("_")
