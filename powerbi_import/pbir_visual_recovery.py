"""Object-level recovery evidence for generated PBIR visuals."""

from __future__ import annotations

import glob
import json
import os
from typing import Any, Dict, List


STATES = ("valid", "empty", "orphaned", "unvalidated")


def scan_pbir_visual_recovery(project_dir: str) -> Dict[str, Any]:
    """Scan generated PBIR visuals without changing the project on disk."""
    patterns = (
        os.path.join(project_dir, "definition", "pages", "*", "visuals", "*", "visual.json"),
        os.path.join(project_dir, "*.Report", "definition", "pages", "*", "visuals", "*", "visual.json"),
    )
    rows: List[Dict[str, Any]] = []
    paths = {path for pattern in patterns for path in glob.glob(pattern)}
    for path in sorted(paths):
        rows.append(_scan_visual(project_dir, path))
    counts = {state: sum(row["state"] == state for row in rows) for state in STATES}
    return {
        "schema_version": "1.0",
        "status": "scanned" if rows else "not_available",
        "visual_count": len(rows),
        "counts": counts,
        "visuals": rows,
    }


def _scan_visual(project_dir: str, path: str) -> Dict[str, Any]:
    relative = os.path.relpath(path, project_dir).replace(os.sep, "/")
    row = {
        "path": relative,
        "page": _path_part(relative, "pages"),
        "visual_id": os.path.basename(os.path.dirname(path)),
        "state": "unvalidated",
        "visual_type": None,
        "reasons": [],
    }
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        row["state"] = "empty"
        row["reasons"].append(f"invalid_json: {exc}")
        return row

    visual = payload.get("visual") if isinstance(payload, dict) else None
    if not isinstance(visual, dict):
        row["state"] = "empty"
        row["reasons"].append("missing_visual_definition")
        return row
    row["visual_type"] = visual.get("visualType") or payload.get("type")
    if not row["visual_type"]:
        row["state"] = "empty"
        row["reasons"].append("missing_visual_type")
        return row

    query = visual.get("query")
    bindings = _binding_count(query)
    if query is None and row["visual_type"] not in {"textbox", "image", "actionButton", "shape"}:
        row["state"] = "orphaned"
        row["reasons"].append("missing_query")
        return row
    if query is not None and bindings == 0 and row["visual_type"] not in {"textbox", "image", "actionButton", "shape"}:
        row["state"] = "orphaned"
        row["reasons"].append("query_has_no_field_bindings")
        return row

    row["state"] = "valid"
    return row


def _binding_count(value: Any) -> int:
    if isinstance(value, dict):
        key_bindings = sum(
            1 for key in value
            if any(token in str(key) for token in ("Column", "Measure", "Entity"))
        )
        return key_bindings + sum(_binding_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_binding_count(item) for item in value)
    if isinstance(value, str):
        return int("Column" in value or "Measure" in value or "Entity" in value)
    return 0


def _path_part(path: str, marker: str) -> str:
    parts = path.split("/")
    try:
        return parts[parts.index(marker) + 1]
    except (ValueError, IndexError):
        return ""
