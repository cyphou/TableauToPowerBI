"""Object-level recovery dispositions for migration evidence."""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, Mapping


DISPOSITIONS = (
    "generated",
    "approximated",
    "unsupported",
    "intentionally_omitted",
    "blocked",
    "pending_validation",
)

_OWNER_BY_TYPE = {
    "worksheets": "@visual",
    "dashboards": "@visual",
    "calculations": "@dax",
    "datasources": "@wiring",
    "parameters": "@semantic",
    "filters": "@visual",
    "actions": "@visual",
    "relationships": "@semantic",
    "stories": "@visual",
    "sets": "@semantic",
    "groups": "@semantic",
    "bins": "@semantic",
    "hierarchies": "@semantic",
    "prep_flows": "@extractor",
}


def build_recovery_registry(
    inventory: Mapping[str, Any],
    dispositions: Mapping[str, str] | None = None,
) -> Dict[str, Any]:
    """Build a conservative recovery registry from a source inventory.

    ``dispositions`` is keyed by inventory ``stable_id`` and is intended to be
    populated only after target generation or an explicit review decision.
    Unknown IDs and invalid dispositions are rejected rather than hidden.
    """
    overrides = dict(dispositions or {})
    objects = list(inventory.get("objects", []))
    known_ids = {row.get("stable_id") for row in objects}
    unknown_ids = sorted(set(overrides) - known_ids)
    if unknown_ids:
        raise ValueError(f"Unknown recovery object ID(s): {', '.join(unknown_ids)}")

    rows = []
    for row in objects:
        stable_id = row["stable_id"]
        disposition = overrides.get(stable_id, "pending_validation")
        if disposition not in DISPOSITIONS:
            raise ValueError(f"Invalid recovery disposition: {disposition}")
        object_type = row["object_type"]
        rows.append({
            "stable_id": stable_id,
            "object_type": object_type,
            "name": row["name"],
            "disposition": disposition,
            "owner": _OWNER_BY_TYPE.get(object_type, "@assessor"),
            "remediation": _remediation(disposition, object_type),
        })

    counts = Counter(row["disposition"] for row in rows)
    return {
        "schema_version": "1.0",
        "status": "complete" if not counts.get("pending_validation") else "pending",
        "object_count": len(rows),
        "counts": {key: counts.get(key, 0) for key in DISPOSITIONS},
        "objects": rows,
    }


def _remediation(disposition: str, object_type: str) -> str:
    if disposition == "pending_validation":
        return "Validate the generated target artifact before release."
    if disposition == "generated":
        return "No remediation required; retain target evidence."
    if disposition == "approximated":
        return f"Review the {object_type} approximation and confirm acceptable parity."
    if disposition == "unsupported":
        return f"Provide a manual target design or supported replacement for {object_type}."
    if disposition == "intentionally_omitted":
        return "Record the omission rationale and confirm stakeholder acceptance."
    return "Resolve the blocking dependency before release."
