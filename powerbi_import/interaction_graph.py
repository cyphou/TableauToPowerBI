"""Static interaction graph evidence for generated PBIR reports."""

from __future__ import annotations

import glob
import json
import os
from typing import Any, Dict, List


def build_interaction_graph(
    extracted: Dict[str, Any], project_dir: str, report_name: str
) -> Dict[str, Any]:
    """Collect interaction edges without claiming runtime behavior."""
    report_dir = os.path.join(project_dir, f"{report_name}.Report")
    edges: List[Dict[str, Any]] = []
    visual_count = 0
    for path in sorted(glob.glob(os.path.join(
            report_dir, "definition", "pages", "*", "visuals", "*", "visual.json"))):
        payload = _load_json(path)
        if not payload:
            continue
        visual_count += 1
        visual = payload.get("visual", {}) or {}
        source = payload.get("name") or os.path.basename(os.path.dirname(path))
        objects = visual.get("objects", {}) or {}
        for action in objects.get("action", []) or []:
            properties = action.get("properties", {}) if isinstance(action, dict) else {}
            action_type = _literal(properties.get("type"))
            destination = _literal(properties.get("destination"))
            edges.append({
                "kind": "action",
                "source": source,
                "action_type": action_type,
                "destination": destination,
                "path": _relative(path, project_dir),
            })
        sync_group = payload.get("syncGroup")
        if isinstance(sync_group, dict) and sync_group.get("groupName"):
            edges.append({
                "kind": "sync_group",
                "source": source,
                "destination": sync_group["groupName"],
                "path": _relative(path, project_dir),
            })
        filter_config = payload.get("filterConfig") or {}
        if filter_config.get("disabled"):
            edges.append({
                "kind": "disabled_cross_filter",
                "source": source,
                "destination": None,
                "path": _relative(path, project_dir),
            })
        for _filter in filter_config.get("filters", []) or []:
            edges.append({
                "kind": "visual_filter",
                "source": source,
                "destination": _filter.get("field", _filter.get("expression")),
                "path": _relative(path, project_dir),
            })

    bookmarks = []
    for path in sorted(glob.glob(os.path.join(
            report_dir, "definition", "bookmarks", "*", "bookmark.json"))):
        payload = _load_json(path) or {}
        bookmarks.append({
            "name": payload.get("displayName") or os.path.basename(os.path.dirname(path)),
            "path": _relative(path, project_dir),
        })
    source_actions = extracted.get("actions", []) or []
    if isinstance(source_actions, dict):
        source_actions = source_actions.get("actions", [])
    return {
        "schema_version": "1.0",
        "status": "scanned" if visual_count or bookmarks else "not_available",
        "runtime": "not_run",
        "source_action_count": len(source_actions),
        "target_visual_count": visual_count,
        "edges": edges,
        "bookmarks": bookmarks,
        "summary": {
            "edge_count": len(edges),
            "action_edges": sum(edge["kind"] == "action" for edge in edges),
            "filter_edges": sum(edge["kind"] == "visual_filter" for edge in edges),
            "disabled_edges": sum(edge["kind"] == "disabled_cross_filter" for edge in edges),
            "sync_edges": sum(edge["kind"] == "sync_group" for edge in edges),
            "bookmark_count": len(bookmarks),
        },
    }


def _load_json(path: str) -> Dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _literal(value: Any) -> Any:
    if isinstance(value, dict):
        literal = value.get("expr", {}).get("Literal")
        if isinstance(literal, dict):
            raw = literal.get("Value")
            if isinstance(raw, str) and len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
                return raw[1:-1].replace("''", "'")
            return raw
    return value


def _relative(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")