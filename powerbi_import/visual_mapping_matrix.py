"""Machine-readable coverage inventory for Tableau visual mappings."""

from __future__ import annotations

from typing import Any, Dict, List

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
        })
    for source, info in sorted(visual_generator.CUSTOM_VISUAL_GUIDS.items()):
        rows.append({
            "source": source,
            "target": info.get("visualType", source) if isinstance(info, dict) else source,
            "status": "custom_visual",
            "guid": info.get("guid") if isinstance(info, dict) else None,
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
