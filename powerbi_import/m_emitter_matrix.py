"""Coverage matrix for Tableau-to-Power Query M connector emitters."""

from __future__ import annotations

from typing import Any, Dict, List

from powerbi_import.m_validator import validate_m_query
from tableau_export import m_query_builder


_SAMPLE_TABLE = {
    "name": "SampleTable",
    "columns": [
        {"name": "id", "datatype": "integer"},
        {"name": "label", "datatype": "string"},
    ],
}

_FALLBACK_REMEDIATION = {
    "owner": "@wiring",
    "action": "Add a connector-specific M generator and a focused validator test before production use.",
}


def build_m_emitter_matrix() -> List[Dict[str, Any]]:
    """Exercise every registered connector alias and validate its M output.

    The matrix is deterministic and uses placeholder-only connection details.
    It is intended for CI coverage and release evidence, not live connectivity.
    """
    fallback = getattr(m_query_builder, "_gen_m_fallback", None)
    rows: List[Dict[str, Any]] = []
    for connector in sorted(m_query_builder._M_GENERATORS, key=str):
        generator = m_query_builder._M_GENERATORS[connector]
        try:
            query = m_query_builder.generate_power_query_m(
                {"type": connector, "details": {}}, _SAMPLE_TABLE
            )
            issues = validate_m_query(query)
            status = "fallback" if fallback is not None and generator is fallback else (
                "invalid" if issues else "generated"
            )
            rows.append({
                "connector": connector,
                "generator": getattr(generator, "__name__", str(generator)),
                "status": status,
                "issues": list(issues),
                "remediation": _FALLBACK_REMEDIATION if status == "fallback" else {},
            })
        except Exception as exc:  # matrix reports emitter defects as evidence
            rows.append({
                "connector": connector,
                "generator": getattr(generator, "__name__", str(generator)),
                "status": "error",
                "issues": [repr(exc)],
                "remediation": {
                    "owner": "@wiring",
                    "action": "Investigate the emitter exception and add a regression fixture.",
                },
            })
    fallback_query = m_query_builder.generate_power_query_m(
        {"type": "UnknownConnector", "details": {}}, _SAMPLE_TABLE
    )
    rows.append({
        "connector": "UnknownConnector",
        "generator": "_gen_m_fallback",
        "status": "fallback" if not validate_m_query(fallback_query) else "invalid",
        "issues": validate_m_query(fallback_query),
        "remediation": dict(_FALLBACK_REMEDIATION),
    })
    return rows


def summarize_m_emitter_matrix(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return release-friendly counts and failed connector names."""
    counts: Dict[str, int] = {}
    for row in rows:
        status = str(row.get("status", "error"))
        counts[status] = counts.get(status, 0) + 1
    return {
        "aliases": len(rows),
        "status_counts": counts,
        "invalid_connectors": [
            row["connector"] for row in rows
            if row.get("status") in {"invalid", "error"}
        ],
        "fallback_connectors": [
            row["connector"] for row in rows if row.get("status") == "fallback"
        ],
        "remediation": {
            row["connector"]: row["remediation"]
            for row in rows if row.get("status") in {"fallback", "invalid", "error"}
        },
    }
