"""Deterministic report repairs driven by Power BI Desktop evidence.

This module consumes the aggregate evidence emitted by ``probe_projects.py``.
It never invents a field, image, or row: a repair may only copy a projection
already present in the same visual into a supported Power BI data role.
"""

from __future__ import annotations

import os
from copy import deepcopy
from typing import Any, Dict, Iterable

from powerbi_import.cross_validator import scan_visual_role_contract, validate_visual_data_roles
from powerbi_import.self_healing_report import load_report, write_report


def _projections(query_state: Dict[str, Any]) -> list[Dict[str, Any]]:
    result = []
    for role in query_state.values():
        if isinstance(role, dict):
            result.extend(p for p in role.get("projections", []) if isinstance(p, dict))
    return result


def _kind(projection: Dict[str, Any]) -> str:
    field = projection.get("field", {})
    if not isinstance(field, dict):
        return ""
    if "Column" in field:
        return "column"
    if "Measure" in field or "Aggregation" in field:
        return "measure"
    return ""


def _first(projections: Iterable[Dict[str, Any]], kind: str) -> Dict[str, Any] | None:
    return next((p for p in projections if _kind(p) == kind), None)


def _copy_role(query_state: Dict[str, Any], role: str, projection: Dict[str, Any] | None) -> bool:
    if role in query_state or projection is None:
        return False
    query_state[role] = {"projections": [deepcopy(projection)]}
    return True


def _heal_missing_roles(state: Dict[str, Any], evidence: Dict[str, Any], recovery=None) -> int:
    """Fill required PBIR roles only from fields already bound to a visual."""
    from powerbi_import.visual_generator import VISUAL_DATA_ROLES

    reported = {
        row.get("path"): set(row.get("missing_roles", []))
        for row in (evidence.get("visual_contract") or {}).get("visuals", [])
        if isinstance(row, dict) and isinstance(row.get("path"), str)
    }
    repaired = 0
    for page in state.get("pages", []):
        for visual in page.get("visuals", []):
            path = os.path.join(visual["dir"], "visual.json")
            relative = os.path.relpath(path, os.path.dirname(state["def_dir"])).replace(os.sep, "/")
            # The probe reports paths relative to the project, including the .Report directory.
            relative = os.path.basename(os.path.dirname(state["def_dir"])) + "/" + relative
            if not reported.get(relative):
                continue
            payload = visual.get("json", {})
            block = payload.get("visual", {}) if isinstance(payload, dict) else {}
            query = block.get("query", {}) if isinstance(block, dict) else {}
            query_state = query.get("queryState", {}) if isinstance(query, dict) else {}
            if not isinstance(query_state, dict):
                continue
            finding = validate_visual_data_roles(payload)
            vtype = finding["visual_type"]
            allowed_dimensions, allowed_measures = VISUAL_DATA_ROLES.get(vtype, ([], []))
            roles = _projections(query_state)
            changed = []
            for role in finding["missing_roles"]:
                if role not in reported[relative]:
                    continue
                kind = "column" if role in allowed_dimensions else "measure" if role in allowed_measures else ""
                candidate = _first(roles, kind) if kind else None
                if _copy_role(query_state, role, candidate):
                    changed.append(role)
            if (vtype == "scatterChart" and changed
                    and "Category" not in query_state and "Series" not in query_state
                    and not validate_visual_data_roles(payload)["missing_roles"]):
                candidate = next(
                    (projection for projection in roles
                     if _kind(projection) == "column"
                     and isinstance(projection["field"]["Column"], dict)
                     and isinstance(projection["field"]["Column"].get("Property"), str)
                     and projection["field"]["Column"]["Property"].strip()),
                    None,
                )
                if _copy_role(query_state, "Category", candidate):
                    changed.append("Category")
            if not changed:
                continue
            payload.setdefault("annotations", []).append({
                "name": "MigrationNote",
                "value": "Desktop feedback healed missing PBIR roles: " + ", ".join(changed),
            })
            state["_dirty_files"].add(path)
            if recovery is not None:
                recovery.record("visual", "desktop_missing_role", item_name=visual["name"],
                                description="Desktop evidence found a visual missing required roles",
                                action="Copied existing visual projections into: " + ", ".join(changed),
                                severity="warning", follow_up="Re-run Desktop validation")
            repaired += 1
    return repaired


def _record_desktop_evidence(evidence: Dict[str, Any], recovery=None) -> None:
    if recovery is None:
        return
    project_name = os.path.basename(evidence.get("pbip_path", ""))
    screenshot = evidence.get("screenshot")
    if not screenshot or not os.path.isfile(screenshot):
        recovery.record("visual", "desktop_screenshot_missing", item_name=project_name,
                        description="Desktop validation did not produce a readable screenshot",
                        action="No visual was removed or replaced",
                        severity="error", follow_up="Re-run the Desktop probe and inspect the report window")
    elif evidence.get("screenshot_blank"):
        recovery.record("visual", "desktop_screenshot_blank", item_name=project_name,
                        description="Desktop screenshot was detected as blank",
                        action="No visual was removed or replaced",
                        severity="error", follow_up="Inspect rendering, model load, and visual errors in Power BI Desktop")
    data = evidence.get("data_load", {}) if isinstance(evidence, dict) else {}
    refresh = evidence.get("refresh", {}) if isinstance(evidence, dict) else {}
    if data.get("status") in {"empty", "partial", "unavailable", "query_failed"}:
        recovery.record("m_query", "desktop_data_status", item_name=project_name,
                        description="Desktop data verification returned " + str(data.get("status")),
                        action="No synthetic rows or source substitutions were applied",
                        severity="error", follow_up="Repair the source query or provide the missing source files")
    if refresh.get("status") == "pending_changes":
        recovery.record("m_query", "desktop_pending_changes", item_name=project_name,
                        description="Desktop left query changes pending after schema refresh",
                        action="No report content was removed",
                        severity="warning", follow_up="Apply changes in Power BI Desktop and re-run validation")
    for dialog in evidence.get("dialogs", []) or []:
        recovery.record("visual", "desktop_dialog", item_name=project_name,
                        description="Desktop dialog: " + str(dialog), action="Captured as validation evidence",
                        severity="error", follow_up="Review the captured dialog and repair its reported source issue")


def heal_from_desktop_evidence(project_dir: str, evidence: Dict[str, Any], recovery=None) -> Dict[str, Any]:
    """Apply deterministic PBIR repairs inferred from one Desktop probe record."""
    report_dirs = [os.path.join(project_dir, name) for name in os.listdir(project_dir)
                   if name.endswith(".Report") and os.path.isdir(os.path.join(project_dir, name))]
    if len(report_dirs) != 1:
        return {"repairs": 0, "remaining_invalid_visuals": 0}
    state = load_report(report_dirs[0])
    if not state:
        return {"repairs": 0, "remaining_invalid_visuals": 0}
    _record_desktop_evidence(evidence, recovery)
    screenshot = evidence.get("screenshot")
    can_repair = bool(screenshot and os.path.isfile(screenshot) and
                      evidence.get("screenshot_blank") is not True)
    repairs = _heal_missing_roles(state, evidence, recovery) if can_repair else 0
    if repairs:
        write_report(state)
    contract = scan_visual_role_contract(project_dir)
    return {"repairs": repairs, "remaining_invalid_visuals": contract.get("needs_review", 0)}
