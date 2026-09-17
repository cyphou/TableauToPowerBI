"""Canonical source-to-target migration ledger.

The existing source inventory and recovery registry intentionally keep their
legacy schemas for compatibility. This adapter provides one release-facing
view with stable source IDs, canonical target dispositions, owners, and an
explicit validation state.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping


TARGET_DISPOSITIONS = ("exact", "healed", "approximated", "unsupported")
VALIDATION_STATES = ("pending", "validated", "blocked")

_DISPOSITION_MAP = {
    "generated": "exact",
    "approximated": "approximated",
    "unsupported": "unsupported",
    "intentionally_omitted": "unsupported",
    "blocked": "unsupported",
}


def build_migration_ledger(
    inventory: Mapping[str, Any],
    recovery: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic release-facing ledger from existing evidence."""
    recovery_by_id = {
        row.get("stable_id"): row
        for row in (recovery or {}).get("objects", [])
        if isinstance(row, Mapping)
    }
    rows = []
    for source in inventory.get("objects", []) or []:
        if not isinstance(source, Mapping):
            continue
        stable_id = source.get("stable_id")
        evidence = recovery_by_id.get(stable_id, {})
        legacy_disposition = evidence.get("disposition", "pending_validation")
        target_disposition = _DISPOSITION_MAP.get(legacy_disposition)
        validation_status = "pending"
        if legacy_disposition == "blocked":
            validation_status = "blocked"
        elif target_disposition:
            validation_status = "validated" if legacy_disposition != "generated" else "pending"
        rows.append({
            "stable_id": stable_id,
            "object_type": source.get("object_type", ""),
            "name": source.get("name", ""),
            "source_location": source.get("source_location", ""),
            "owner": evidence.get("owner", "@assessor"),
            "target_disposition": target_disposition,
            "validation_status": validation_status,
            "remediation": evidence.get(
                "remediation", "Validate the generated target artifact before release."
            ),
        })
    rows.sort(key=lambda row: (str(row["object_type"]), str(row["stable_id"])))
    disposition_counts = Counter(row["target_disposition"] or "unresolved" for row in rows)
    validation_counts = Counter(row["validation_status"] for row in rows)
    return {
        "schema_version": "1.0",
        "status": "complete" if not validation_counts["pending"] and not validation_counts["blocked"] else "pending",
        "object_count": len(rows),
        "target_dispositions": {
            key: disposition_counts.get(key, 0) for key in TARGET_DISPOSITIONS
        },
        "unresolved_count": disposition_counts.get("unresolved", 0),
        "validation": {
            key: validation_counts.get(key, 0) for key in VALIDATION_STATES
        },
        "objects": rows,
    }