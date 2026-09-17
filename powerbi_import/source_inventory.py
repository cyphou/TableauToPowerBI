"""Canonical inventory of objects extracted from a Tableau workbook."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Dict, Iterable, List


OBJECT_TYPES = (
    "worksheets",
    "dashboards",
    "datasources",
    "calculations",
    "parameters",
    "filters",
    "stories",
    "actions",
    "sets",
    "groups",
    "bins",
    "hierarchies",
    "relationships",
    "sort_orders",
    "aliases",
    "custom_sql",
    "user_filters",
    "hyper_files",
    "datasource_filters",
    "custom_geocoding",
    "published_datasources",
    "data_blending",
    "table_extensions",
    "linguistic_schema",
)


_NAME_KEYS = ("name", "caption", "title", "id", "key")
#: Extractors name the owning object differently per type — actions carry
#: `source_worksheets`, filters carry `worksheet`. Reading only the generic
#: names reported every action in the corpus as an orphan.
_PARENT_KEYS = ("parent", "parent_id", "dashboard", "worksheet", "datasource",
                "source_worksheets", "source_worksheet", "sheet")


def _parent_value(item_dict: Dict[str, Any]) -> Any:
    """First non-empty parent reference, unwrapping single-owner lists."""
    for key in _PARENT_KEYS:
        value = item_dict.get(key)
        if isinstance(value, (list, tuple)):
            value = next((v for v in value if v not in (None, "")), None)
        if value not in (None, ""):
            return str(value)
    return None


def build_source_inventory(extracted: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deterministic, redaction-safe inventory for extracted objects.

    The inventory deliberately stores metadata rather than full source payloads.
    It is suitable for lineage, assessment, recovery, and evidence packages.
    """
    rows: List[Dict[str, Any]] = []
    for object_type in OBJECT_TYPES:
        value = extracted.get(object_type, [])
        for index, item in enumerate(_iter_items(value)):
            rows.append(_inventory_row(object_type, item, index))

    rows.sort(key=lambda row: (row["object_type"], row["stable_id"]))
    counts = Counter(row["object_type"] for row in rows)
    return {
        "schema_version": "1.0",
        "object_types": list(OBJECT_TYPES),
        "object_count": len(rows),
        "counts": {key: counts.get(key, 0) for key in OBJECT_TYPES},
        "duplicate_names": _duplicate_names(rows),
        "orphan_count": sum(row["orphan"] for row in rows),
        "objects": rows,
    }


def _iter_items(value: Any) -> Iterable[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return ({"key": key, **item} if isinstance(item, dict) else {"key": key, "value": item}
                for key, item in value.items())
    if value in (None, ""):
        return ()
    return (value,)


def _inventory_row(object_type: str, item: Any, index: int) -> Dict[str, Any]:
    item_dict = item if isinstance(item, dict) else {"value": item}
    name = next((str(item_dict[key]) for key in _NAME_KEYS if item_dict.get(key) not in (None, "")), f"item-{index}")
    parent = _parent_value(item_dict)
    source_location = _source_location(item_dict)
    stable_input = json.dumps(
        {"type": object_type, "name": name, "parent": parent, "source": source_location},
        sort_keys=True,
        ensure_ascii=True,
        default=str,
    )
    stable_id = f"src-{hashlib.sha256(stable_input.encode('utf-8')).hexdigest()[:16]}"
    return {
        "stable_id": stable_id,
        "object_type": object_type,
        "name": name,
        "source_location": source_location,
        "parent": parent,
        "orphan": int(parent is None and object_type in {"actions", "filters", "relationships", "sort_orders"}),
        "disposition": "extracted",
    }


def _source_location(item: Dict[str, Any]) -> str:
    for key in ("source_location", "path", "xml_path", "location"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return "extracted"


def _duplicate_names(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[tuple[str, str], List[str]] = {}
    for row in rows:
        grouped.setdefault((row["object_type"], row["name"]), []).append(row["stable_id"])
    return [
        {"object_type": object_type, "name": name, "stable_ids": stable_ids}
        for (object_type, name), stable_ids in sorted(grouped.items())
        if len(stable_ids) > 1
    ]
