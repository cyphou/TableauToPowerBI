"""Normalized local evidence for a generated Fabric artifact bundle."""

from __future__ import annotations

import json
import ntpath
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .fabric_sources import HYPER_SOURCE_SELECTION_UNVERIFIED

_ARTIFACT_DEPENDENCIES = {
    "Lakehouse": [],
    "Dataflow": ["Lakehouse"],
    "Notebook": ["Lakehouse"],
    "SemanticModel": ["Lakehouse", "Dataflow", "Notebook"],
    "Report": ["SemanticModel"],
    "Pipeline": ["Dataflow", "Notebook", "SemanticModel"],
}
_MODEL_ONLY_ARTIFACTS = ("Lakehouse", "Dataflow", "Notebook", "SemanticModel", "Pipeline")
_MODEL_ONLY_METADATA_KEYS = {
    "lakehouse", "dataflow", "notebook", "semantic_model", "pipeline",
}


def _model_only_layout(project_dir: str, project_name: str) -> bool:
    """Require one named model, five local items and no PBIP/PBIR artifacts."""
    if (not isinstance(project_name, str) or not project_name
            or project_name in {".", ".."}
            or ntpath.basename(project_name) != project_name
            or any(ord(char) < 32 or char in "/\\:" for char in project_name)):
        return False
    root = Path(project_dir)
    try:
        models = list(root.glob("*.SemanticModel"))
        if (len(models) != 1 or models[0].name != f"{project_name}.SemanticModel"
                or any(root.rglob("*.Report"))
                or any(root.rglob("definition.pbir"))
                or any(root.rglob("*.pbip"))
                or any(root.rglob("report.json"))
                or any(root.rglob("page.json"))
                or any(root.rglob("visual.json"))):
            return False
        root_resolved = root.resolve()
        return all(
            (root / f"{project_name}.{suffix}").is_dir()
            and (root / f"{project_name}.{suffix}").resolve().is_relative_to(root_resolved)
            for suffix in _MODEL_ONLY_ARTIFACTS
        )
    except (OSError, ValueError):
        return False


def _model_only_metadata(project_dir: str, project_name: str) -> Optional[bool]:
    """True for corroboration, None for absent metadata, False for a conflict."""
    root = Path(project_dir)
    path = root / "fabric_project_metadata.json"
    try:
        if not path.exists() and not path.is_symlink():
            return None
        if not path.resolve().is_relative_to(root.resolve()):
            return False
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or metadata.get("project_name") != project_name:
            return False
        artifacts = metadata.get("artifacts")
        validation = metadata.get("validation")
        quality = metadata.get("quality")
        return bool(
            isinstance(artifacts, dict)
            and set(artifacts) == _MODEL_ONLY_METADATA_KEYS
            and all(isinstance(value, dict) for value in artifacts.values())
            and isinstance(validation, dict)
            and validation.get("valid") is True
            and validation.get("artifacts_checked") == 5
            and validation.get("errors") == []
            and isinstance(quality, dict)
            and quality.get("validation") == "locally_valid"
            and quality.get("confidence") == "FABRIC_STATIC_PASS"
        )
    except (OSError, UnicodeError, ValueError, TypeError):
        return False


def is_reportless_fabric_bundle(project_dir: str, project_name: str) -> bool:
    """Corroborate scope from generated metadata; this does not validate items."""
    return (_model_only_layout(project_dir, project_name)
            and _model_only_metadata(project_dir, project_name) is True)


def _validate_fabric(project_dir: str, project_name: str,
                     include_report: bool) -> Dict[str, Any]:
    try:
        from powerbi_import.fabric_validator import FabricProjectValidator

        result = FabricProjectValidator.validate(
            project_dir, project_name, include_report=include_report)
        if not isinstance(result, dict) or type(result.get("valid")) is not bool:
            raise ValueError("Fabric validator did not return a validity result")
        return dict(result)
    except Exception as exc:  # noqa: BLE001 - unavailable validation is not a pass
        return {"valid": None, "errors": [], "warnings": [str(exc)],
                "artifacts_checked": None, "status": "not_run"}


def _unverified_hyper_sources_from_metadata(project_dir: str, project_name: str) -> int:
    """Recover only a generation diagnostic, never a cached validation result."""
    root = Path(project_dir)
    path = root / "fabric_project_metadata.json"
    try:
        if not path.resolve().is_relative_to(root.resolve()):
            return 0
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or metadata.get("project_name") != project_name:
            return 0
        ingestion = metadata.get("fabric_ingestion")
        selection = ingestion.get("hyper_source_selection") if isinstance(ingestion, dict) else None
        if not isinstance(selection, dict) or selection.get("status") != "not_run":
            return 0
        count = selection.get("unverified_datasources")
        return count if type(count) is int and count > 0 else 0
    except (OSError, UnicodeError, ValueError, TypeError):
        return 0


def build_fabric_evidence(project_dir: str, project_name: str, *,
                          include_report: Optional[bool] = None,
                          unverified_hyper_sources: Optional[int] = None) -> Dict[str, Any]:
    """Expose current Fabric checks; an absent report alone does not establish intent.

    The generator can pass its explicit ``include_report`` choice before
    metadata exists. With no choice, only corroborating generation metadata
    selects the model-only contract. This function never writes artifacts.
    """
    if include_report is not None and type(include_report) is not bool:
        raise ValueError("include_report must be a bool or None")
    if (unverified_hyper_sources is not None
            and (type(unverified_hyper_sources) is not int or unverified_hyper_sources < 0)):
        raise ValueError("unverified_hyper_sources must be a nonnegative integer or None")

    has_bundle = any(
        os.path.isdir(os.path.join(project_dir, f"{project_name}.{artifact}"))
        for artifact in ("Lakehouse", "Dataflow", "Notebook", "Pipeline")
    )
    if not has_bundle:
        # A recorded five-item Fabric generation remains evidence of intent
        # even if all four Fabric-specific directories were subsequently lost.
        has_bundle = _model_only_metadata(project_dir, project_name) is True
    if not has_bundle:
        return {
            "status": "not_present",
            "confidence": "UNVERIFIED",
            "validation": {"valid": None, "status": "not_run", "errors": [],
                           "warnings": [], "artifacts_checked": 0},
            "artifacts": {},
            "pbip_openability": "not_run", "report_validation": "not_run",
            "visual_coverage": "not_run", "visual_parity": "not_run",
            "runtime": {"deployment": "not_run", "refresh": "not_run",
                         "semantic_execution": "not_run", "post_deploy": "not_run"},
        }

    layout = _model_only_layout(project_dir, project_name)
    metadata = _model_only_metadata(project_dir, project_name) if layout else False
    model_only = layout and (
        metadata is True if include_report is None
        else include_report is False and metadata is not False
    )
    validation = _validate_fabric(project_dir, project_name, not model_only)
    scope = "fabric_model_only" if model_only else "fabric_report_required"
    scope_conflict = include_report is False and not model_only
    if scope_conflict and validation.get("valid") is True:
        # A valid report-required bundle does not prove that the caller's
        # contradictory reportless request can be honored.
        validation = {
            "valid": None, "errors": [], "artifacts_checked": None,
            "warnings": ["Requested model-only scope conflicts with current artifacts or metadata."],
            "candidates": {"report_required": validation},
        }
        scope = "unknown"

    # If a caller omits include_report before metadata is written, both
    # contracts can be inspected but a missing report does not establish intent.
    if include_report is None and layout and metadata is None:
        report_required = validation
        candidate = _validate_fabric(project_dir, project_name, False)
        scope = "unknown"
        if report_required.get("valid") is False and candidate.get("valid") is False:
            # Both possible static contracts fail. Keep both sets of errors.
            validation = dict(report_required)
            validation["warnings"] = list(validation.get("warnings", [])) + [
                "Report scope is unavailable; both candidate contracts failed."]
            validation["candidates"] = {
                "report_required": report_required, "model_only": candidate,
            }
        else:
            validation = {
                "valid": None, "errors": [],
                "warnings": ["Report scope is unavailable; five-artifact validity "
                             "does not establish whether a report was required."],
                "artifacts_checked": None,
                "candidates": {
                    "report_required": report_required,
                    "model_only": candidate,
                },
            }

    valid = validation.get("valid")
    status = "locally_valid" if valid is True else "invalid" if valid is False else "not_run"
    artifacts = {}
    present_names = {
        artifact for artifact in _ARTIFACT_DEPENDENCIES
        if os.path.isdir(os.path.join(project_dir, f"{project_name}.{artifact}"))
    }
    for artifact, dependencies in _ARTIFACT_DEPENDENCIES.items():
        path = f"{project_name}.{artifact}"
        present = os.path.isdir(os.path.join(project_dir, path))
        artifacts[artifact] = {
            "status": ("not_run" if artifact == "Report" and scope in {
                "fabric_model_only", "unknown"} else
                "locally_valid" if present and valid is True else
                "present" if present else "missing"),
            "path": path if present else None,
            "depends_on": list(dependencies),
            "deployment": "not_run",
            "refresh": "not_run",
            "dependency_status": "not_run" if artifact == "Report" and scope in {
                "fabric_model_only", "unknown"} else "valid" if all(
                dependency in present_names for dependency in dependencies
            ) else "missing",
        }
    dependency_errors = [
        f"{artifact} depends on missing {dependency}"
        for artifact, details in artifacts.items()
        if not (artifact == "Report" and scope in {"fabric_model_only", "unknown"})
        for dependency in details["depends_on"]
        if dependency not in present_names
    ]
    validation = dict(validation)
    validation["dependency_errors"] = dependency_errors
    if unverified_hyper_sources is None:
        unverified_hyper_sources = _unverified_hyper_sources_from_metadata(
            project_dir, project_name)
    if unverified_hyper_sources:
        validation["warnings"] = [
            *(validation.get("warnings") or []), HYPER_SOURCE_SELECTION_UNVERIFIED,
        ]
        validation["hyper_source_selection"] = {
            "status": "not_run",
            "unverified_datasources": unverified_hyper_sources,
        }
    result = {
        "status": status,
        "confidence": "FABRIC_STATIC_PASS" if valid is True else "UNVERIFIED",
        "scope": scope,
        "validation": validation,
        "artifacts": artifacts,
        "pbip_openability": "not_run",
        "report_validation": (
            "not_run" if scope in {"fabric_model_only", "unknown"} or valid is None
            else "static_evidence"),
        "visual_coverage": "not_run",
        "visual_parity": "not_run",
        "runtime": {
            "deployment": "not_run",
            "refresh": "not_run",
            "semantic_execution": "not_run",
            "post_deploy": "not_run",
        },
    }
    if model_only:
        result["generation_evidence"] = {
            "path": "fabric_project_metadata.json" if metadata is True else None,
            "scope_source": "generation_metadata" if metadata is True else "explicit_caller",
            "limitation": "Scope evidence does not establish current validity; "
                          "the five artifacts were checked again.",
        }
    elif scope == "unknown":
        result["scope_diagnostic"] = (
            "The requested model-only scope conflicts with current artifacts or metadata; "
            "the report-required candidate is retained."
            if scope_conflict else
            "No explicit report choice or generation metadata was available. "
            "Both candidate validations are retained; neither scope is assumed.")
    elif scope_conflict:
        result["scope_diagnostic"] = (
            "The requested model-only scope conflicts with current artifacts or metadata; "
            "the report-required contract was not relaxed.")
    return result
