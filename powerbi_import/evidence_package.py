"""Portable evidence package for a unified migration quality report."""

from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict


def build_evidence_package(report: Any) -> Dict[str, Any]:
    """Build a redaction-friendly package payload from a quality report."""
    payload = report.to_dict() if hasattr(report, "to_dict") else dict(report)
    blockers = payload.get("blockers", [])
    warnings = payload.get("warnings", [])
    priorities = payload.get("priorities", [])
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_name": payload.get("report_name"),
        "status": payload.get("status", "UNVERIFIED"),
        "handoff_status": payload.get("handoff_status", "UNVERIFIED"),
        "summary": {
            "status": payload.get("status", "UNVERIFIED"),
            "handoff_status": payload.get("handoff_status", "UNVERIFIED"),
            "blocker_count": len(blockers),
            "warning_count": len(warnings),
            "priority_count": len(priorities),
        },
        "blockers": blockers,
        "warnings": warnings,
        "priorities": priorities,
        "openability_confidence": payload.get("openability_confidence", {}),
        "assessment": payload.get("assessment", {}),
        "parity": payload.get("parity", {}),
        "lineage": payload.get("lineage", {}),
        "m_emitters": payload.get("m_emitters", {}),
        "visual_mappings": payload.get("visual_mappings", {}),
        "fabric_evidence": payload.get("fabric_evidence", {}),
        "semantic_context": payload.get("semantic_context", {}),
        "evidence_manifest": payload.get("evidence_manifest", {}),
        "runtime_boundary": {
            "desktop": payload.get("desktop", {}).get("status", "not_run"),
            "semantic_execution": payload.get("openability_confidence", {}).get(
                "semantic_execution", "not_run"
            ),
            "refresh": payload.get("openability_confidence", {}).get("refresh", "not_run"),
            "deployment": payload.get("openability_confidence", {}).get("deployment", "not_run"),
        },
    }


def write_evidence_package(report: Any, output_path: str, extra_files: list[str] | None = None) -> str:
    """Write a JSON + README ZIP package for an already-built report.

    ``extra_files`` can include the generated quality JSON/HTML and openability
    artifacts produced alongside the migration. They are bundled under a
    ``quality/`` folder so the zip is a portable operator handoff package.
    """
    payload = build_evidence_package(report)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    readme = (
        "Tableau to Power BI Evidence Package\n"
        "=====================================\n\n"
        f"Report: {payload['report_name']}\n"
        f"Status: {payload['status']}\n"
        f"Handoff: {payload['handoff_status']}\n\n"
        "Static evidence and runtime evidence are intentionally separated.\n"
        "A not_run runtime state is not a deployment or refresh success.\n"
        "This archive also includes the generated quality artifacts for direct review.\n"
    )
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("evidence_package.json", json.dumps(payload, indent=2, ensure_ascii=False))
        archive.writestr("README.txt", readme)
        for file_path in extra_files or []:
            if not file_path or not os.path.isfile(file_path):
                continue
            arcname = os.path.join("quality", os.path.basename(file_path))
            with open(file_path, "rb") as handle:
                archive.writestr(arcname, handle.read())
    return os.path.abspath(output_path)
