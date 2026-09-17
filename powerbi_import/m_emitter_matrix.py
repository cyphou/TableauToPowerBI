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

_CONNECTOR_FIXTURES = [
    {
        "name": "sql_server_schema_and_quotes",
        "connection": {"type": "SQL Server", "details": {
            "server": 'fixture-host"quoted', "database": "fixture_db", "schema": "analytics",
        }},
        "table": {"name": "Orders", "columns": _SAMPLE_TABLE["columns"]},
    },
    {
        "name": "csv_file_and_delimiter",
        "connection": {"type": "CSV", "details": {
            "filename": "fixture-data.csv", "delimiter": ";",
        }},
        "table": {"name": "FixtureData", "columns": _SAMPLE_TABLE["columns"]},
    },
    {
        "name": "excel_source_table_navigation",
        "connection": {"type": "Excel", "details": {
            "filename": "fixture.xlsx",
        }},
        "table": {"name": "Sheet1", "source_table": "[extract].[Sheet1$]", "columns": []},
    },
    {
        "name": "custom_sql_parameter_safe",
        "connection": {"type": "Custom SQL", "details": {
            "server": "fixture-host", "database": "fixture_db",
            "custom_sql": "SELECT id, label FROM analytics.orders WHERE id > 0",
        }},
        "table": {"name": "OrdersQuery", "columns": []},
    },
    {
        "name": "tableau_server_published_datasource",
        "connection": {"type": "Tableau Server", "details": {
            "server": "tableau-fixture", "dbname": "published_orders",
        }},
        "table": {"name": "PublishedOrders", "columns": []},
    },
]


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
    fixture_rows = []
    for fixture in _CONNECTOR_FIXTURES:
        try:
            query = m_query_builder.generate_power_query_m(
                fixture["connection"], fixture["table"]
            )
            issues = validate_m_query(query)
            fixture_rows.append({
                "fixture": fixture["name"],
                "connector": fixture["connection"]["type"],
                "status": "invalid" if issues else "generated",
                "issues": list(issues),
            })
        except Exception as exc:
            fixture_rows.append({
                "fixture": fixture["name"],
                "connector": fixture["connection"]["type"],
                "status": "error",
                "issues": [repr(exc)],
            })
    rows.extend({"connector_fixture": row} for row in fixture_rows)
    return rows


def summarize_m_emitter_matrix(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Return release-friendly counts and failed connector names."""
    counts: Dict[str, int] = {}
    alias_rows = [row for row in rows if "connector" in row]
    fixture_rows = [row["connector_fixture"] for row in rows if "connector_fixture" in row]
    for row in alias_rows:
        status = str(row.get("status", "error"))
        counts[status] = counts.get(status, 0) + 1
    return {
        "aliases": len(alias_rows),
        "status_counts": counts,
        "invalid_connectors": [
            row["connector"] for row in alias_rows
            if row.get("status") in {"invalid", "error"}
        ],
        "fallback_connectors": [
            row["connector"] for row in alias_rows if row.get("status") == "fallback"
        ],
        "remediation": {
            row["connector"]: row["remediation"]
            for row in alias_rows if row.get("status") in {"fallback", "invalid", "error"}
        },
        "fixtures": {
            "count": len(fixture_rows),
            "status_counts": {
                status: sum(1 for row in fixture_rows if row.get("status") == status)
                for status in {row.get("status") for row in fixture_rows}
            },
            "invalid_fixtures": [
                row["fixture"] for row in fixture_rows
                if row.get("status") in {"invalid", "error"}
            ],
        },
    }
