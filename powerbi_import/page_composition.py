"""Page blueprint and composition parity for Tableau dashboards."""

from __future__ import annotations

import glob
import hashlib
import json
import os
from typing import Any, Dict, List, Optional


_CONTENT_TYPES = {"worksheetReference", "text", "image", "filter_control", "parameter_control"}


def build_page_blueprint(dashboard: Dict[str, Any]) -> Dict[str, Any]:
    """Build a deterministic source blueprint without depending on generator layout code."""
    dashboard_name = str(dashboard.get("name", "Dashboard"))
    page_id = _stable_id("page", dashboard_name)
    objects = []
    for index, source in enumerate(dashboard.get("objects", []) or []):
        if not isinstance(source, dict) or source.get("type") not in _CONTENT_TYPES:
            continue
        name = str(
            source.get("worksheetName")
            or source.get("name")
            or source.get("param_name")
            or source.get("type")
        )
        position = source.get("position", {}) or {}
        objects.append({
            "stable_id": _stable_id("object", page_id, index, source.get("type"), name),
            "source_index": index,
            "name": name,
            "type": source.get("type"),
            "position": _source_position(position),
            "visible": source.get("visible", source.get("is_visible", True)) is not False,
        })
    return {
        "schema_version": "1.0",
        "page_id": page_id,
        "name": dashboard_name,
        "canvas": _source_position(dashboard.get("size", {}) or {}),
        "composition_policy": "preserve",
        "zones": _flatten_zones(dashboard.get("zone_hierarchy", {}) or {}, page_id),
        "objects": objects,
    }


def compare_page_composition(
    dashboard: Dict[str, Any],
    report_dir: str,
    tolerance: int = 2,
) -> Dict[str, Any]:
    """Compare a source page blueprint with PBIR geometry independently."""
    blueprint = build_page_blueprint(dashboard)
    page_dir = _find_page(report_dir, blueprint["name"])
    result = {
        "page": blueprint["name"],
        "page_id": blueprint["page_id"],
        "blueprint": blueprint,
        "match_strategy": "source_order",
        "page_found": page_dir is not None,
        "canvas": {"matched": False, "tableau": blueprint["canvas"], "powerbi": None},
        "matched": [],
        "mismatched": [],
    }
    if page_dir is None:
        result["mismatched"] = [
            {"stable_id": item["stable_id"], "reason": "page not found"}
            for item in blueprint["objects"]
        ]
        return result

    page = _load_json(os.path.join(page_dir, "page.json")) or {}
    actual_canvas = _source_position(page)
    result["canvas"]["powerbi"] = actual_canvas
    result["canvas"]["matched"] = _same_position(blueprint["canvas"], actual_canvas, tolerance)
    actual = _actual_visuals(page_dir)
    for source, target in zip(blueprint["objects"], actual):
        differences = _position_differences(source["position"], target["position"], tolerance)
        expected_z = source["source_index"]
        actual_z = target.get("z")
        if actual_z is not None and abs(actual_z - expected_z) > tolerance:
            differences.append("z")
        row = {
            "stable_id": source["stable_id"],
            "source_name": source["name"],
            "target_name": target.get("name"),
            "source_position": source["position"],
            "target_position": target["position"],
            "differences": differences,
        }
        (result["mismatched"] if differences else result["matched"]).append(row)
    if len(actual) != len(blueprint["objects"]):
        result["mismatched"].append({
            "reason": "object_count",
            "source_count": len(blueprint["objects"]),
            "target_count": len(actual),
        })
    result["status"] = "pass" if not result["mismatched"] and result["canvas"]["matched"] else "needs_review"
    return result


def build_page_composition_contract(
    extracted: Dict[str, Any], project_dir: str, report_name: str
) -> Dict[str, Any]:
    """Compare all extracted dashboards against one generated report."""
    dashboards = extracted.get("dashboards", []) or []
    if isinstance(dashboards, dict):
        dashboards = dashboards.get("dashboards", [])
    report_dir = os.path.join(project_dir, f"{report_name}.Report")
    pages = [compare_page_composition(dashboard, report_dir) for dashboard in dashboards]
    return {
        "schema_version": "1.0",
        "status": "pass" if pages and all(page.get("status") == "pass" for page in pages) else "needs_review",
        "pages": pages,
        "summary": {
            "source_pages": len(pages),
            "matched_pages": sum(page.get("status") == "pass" for page in pages),
            "matched_objects": sum(len(page["matched"]) for page in pages),
            "mismatched_objects": sum(len(page["mismatched"]) for page in pages),
        },
    }


def _stable_id(kind: str, *parts: Any) -> str:
    raw = "|".join(str(part) for part in parts)
    return f"{kind}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _source_position(position: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "x": position.get("x", 0),
        "y": position.get("y", 0),
        "width": position.get("width", position.get("w", 0)),
        "height": position.get("height", position.get("h", 0)),
    }


def _flatten_zones(zone: Dict[str, Any], page_id: str, path: str = "0") -> List[Dict[str, Any]]:
    if not isinstance(zone, dict):
        return []
    key = zone.get("name") or zone.get("id") or path
    rows = [{
        "stable_id": _stable_id("zone", page_id, path, key),
        "path": path,
        "name": key,
        "zone_type": zone.get("zone_type", ""),
        "orientation": zone.get("orientation", ""),
        "padding": zone.get("padding", 0),
        "position": _source_position(zone.get("position", {}) or {}),
    }]
    for index, child in enumerate(zone.get("children", []) or []):
        rows.extend(_flatten_zones(child, page_id, f"{path}.{index}"))
    return rows


def _find_page(report_dir: str, display_name: str) -> Optional[str]:
    pages_root = os.path.join(report_dir, "definition", "pages")
    for page_dir in sorted(glob.glob(os.path.join(pages_root, "*"))):
        page = _load_json(os.path.join(page_dir, "page.json")) or {}
        if page.get("displayName") == display_name:
            return page_dir
    return None


def _actual_visuals(page_dir: str) -> List[Dict[str, Any]]:
    rows = []
    for path in sorted(glob.glob(os.path.join(page_dir, "visuals", "*", "visual.json"))):
        payload = _load_json(path) or {}
        position = payload.get("position", {}) or {}
        rows.append({
            "name": payload.get("name"),
            "position": _source_position(position),
            "z": position.get("z", position.get("tabOrder")),
        })
    return sorted(rows, key=lambda row: (row.get("z") is None, row.get("z", 0), row.get("name") or ""))


def _position_differences(source: Dict[str, Any], target: Dict[str, Any], tolerance: int) -> List[str]:
    return [key for key in ("x", "y", "width", "height") if abs(source[key] - target[key]) > tolerance]


def _same_position(source: Dict[str, Any], target: Dict[str, Any], tolerance: int) -> bool:
    return not _position_differences(source, target, tolerance)


def _load_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None