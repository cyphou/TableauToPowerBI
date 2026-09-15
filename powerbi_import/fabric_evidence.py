"""Normalized local evidence for a generated Fabric artifact bundle."""

from __future__ import annotations

from typing import Any, Dict
import os


_ARTIFACT_DEPENDENCIES = {
    "Lakehouse": [],
    "Dataflow": ["Lakehouse"],
    "Notebook": ["Lakehouse"],
    "SemanticModel": ["Lakehouse", "Dataflow", "Notebook"],
    "Report": ["SemanticModel"],
    "Pipeline": ["Dataflow", "Notebook", "SemanticModel"],
}


def build_fabric_evidence(project_dir: str, project_name: str) -> Dict[str, Any]:
    """Validate a local Fabric bundle and expose artifact dependencies/statuses."""
    from powerbi_import.fabric_validator import FabricProjectValidator

    has_bundle = any(
        os.path.isdir(os.path.join(project_dir, f"{project_name}.{artifact}"))
        for artifact in ("Lakehouse", "Dataflow", "Notebook", "Pipeline")
    )
    if not has_bundle:
        return {
            "status": "not_present",
            "confidence": "UNVERIFIED",
            "validation": {"valid": True, "errors": [], "warnings": [], "artifacts_checked": 0},
            "artifacts": {},
            "runtime": {"deployment": "not_run", "refresh": "not_run",
                         "semantic_execution": "not_run", "post_deploy": "not_run"},
        }

    validation = FabricProjectValidator.validate(
        project_dir, project_name, include_report=True
    )
    artifacts = {}
    for artifact, dependencies in _ARTIFACT_DEPENDENCIES.items():
        path = f"{project_name}.{artifact}"
        present = os.path.isdir(os.path.join(project_dir, path))
        artifacts[artifact] = {
            "status": "locally_valid" if present and validation["valid"] else (
                "present" if present else "missing"
            ),
            "path": path if present else None,
            "depends_on": list(dependencies),
            "deployment": "not_run",
            "refresh": "not_run",
        }
    return {
        "status": "locally_valid" if validation["valid"] else "invalid",
        "confidence": "FABRIC_STATIC_PASS" if validation["valid"] else "UNVERIFIED",
        "validation": validation,
        "artifacts": artifacts,
        "runtime": {
            "deployment": "not_run",
            "refresh": "not_run",
            "semantic_execution": "not_run",
            "post_deploy": "not_run",
        },
    }
