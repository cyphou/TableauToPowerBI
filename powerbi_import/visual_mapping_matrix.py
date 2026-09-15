"""Machine-readable coverage inventory for Tableau visual mappings."""

from __future__ import annotations

from typing import Any, Dict, List
import re

from powerbi_import import visual_generator


def build_visual_mapping_matrix() -> List[Dict[str, Any]]:
    """Return native, approximate, and custom visual mapping records."""
    rows: List[Dict[str, Any]] = []
    for source, target in sorted(visual_generator.VISUAL_TYPE_MAP.items()):
        rows.append({"source": source, "target": target, "status": "native"})
    for source, entry in sorted(visual_generator.APPROXIMATION_MAP.items()):
        target = entry[0] if isinstance(entry, (tuple, list)) else entry
        note = entry[1] if isinstance(entry, (tuple, list)) and len(entry) > 1 else ""
        rows.append({
            "source": source,
            "target": target,
            "status": "approximation",
            "note": note,
            "owner": "@visual",
            "remediation": "Review target behavior and accept, improve, or replace the approximation.",
        })
    for source, info in sorted(visual_generator.CUSTOM_VISUAL_GUIDS.items()):
        rows.append({
            "source": source,
            "target": info.get("visualType", source) if isinstance(info, dict) else source,
            "status": "custom_visual",
            "guid": info.get("guid") if isinstance(info, dict) else None,
            "owner": "@visual",
            "remediation": "Verify AppSource/custom visual availability in the target tenant.",
        })
    return rows


def summarize_visual_mapping_matrix(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    for row in rows:
        status = row.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {
        "mappings": len(rows),
        "status_counts": counts,
        "sources": sorted({row["source"] for row in rows}),
        "targets": sorted({row["target"] for row in rows}),
    }


def find_visual_approximations_in_use(extracted: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Match approximation mappings against extracted worksheet mark metadata."""
    rows = build_visual_mapping_matrix()
    normalize = lambda value: re.sub(r"[^a-z0-9]+", "", str(value).lower())
    approximations = {
        normalize(row["source"]): row
        for row in rows if row.get("status") == "approximation"
    }
    matches: List[Dict[str, Any]] = []
    for worksheet in extracted.get("worksheets", []) or []:
        candidates = {
            normalize(worksheet.get("original_mark_class", "")),
            normalize(worksheet.get("chart_type", "")),
        }
        for candidate in candidates - {""}:
            if candidate in approximations:
                match = dict(approximations[candidate])
                match["worksheet"] = worksheet.get("name", "")
                matches.append(match)
    return matches
