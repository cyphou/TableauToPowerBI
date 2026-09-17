"""Regression tests for fidelity v2 preprocessing components."""

from powerbi_import.dual_axis_and_reference_lines import DualAxisBuilder, ReferenceLineBuilder
from powerbi_import.relationship_inference_v2 import RelationshipInferenceEngine
from powerbi_import.equivalence_tester_v2 import run_full_equivalence_suite


def test_dual_axis_detection_with_axes_flag():
    worksheet = {
        "axes": {"dual_axis": True, "dual_axis_sync": False},
        "fields": [],
    }
    assert DualAxisBuilder.detect_dual_axis_worksheet(worksheet) is True


def test_reference_line_normalization():
    worksheet = {
        "reference_lines": [
            {
                "name": "Target",
                "type": "constant",
                "value": 100,
                "color": "#ff0000",
                "lineStyle": "dashed",
            }
        ]
    }
    refs = ReferenceLineBuilder.extract_reference_lines(worksheet)
    assert len(refs) == 1
    cfg = ReferenceLineBuilder.build_pbi_reference_line_config(refs[0])
    assert cfg["lineStyle"] == "dashed"
    assert cfg["value"] == 100


def test_relationship_inference_detects_simple_id_join():
    tables = [
        {
            "name": "FactSales",
            "columns": [
                {"name": "order_id", "datatype": "int"},
                {"name": "customer_id", "datatype": "int"},
            ],
        },
        {
            "name": "DimCustomer",
            "columns": [
                {"name": "customer_id", "datatype": "int", "is_primary_key": True},
                {"name": "customer_name", "datatype": "string"},
            ],
        },
    ]

    inferred = RelationshipInferenceEngine(verbose=False).infer_relationships(tables)
    assert inferred
    assert any(
        rel["fromColumn"] == "customer_id" and rel["toColumn"] == "customer_id"
        for rel in inferred
    )


def test_equivalence_suite_handles_dict_fields_without_type_error():
    tableau_export = {
        "datasources": [{"name": "Sales", "row_count": 10}],
        "calculations": [{"name": "Revenue", "formula": "SUM([Amount])"}],
        "worksheets": [
            {
                "name": "Overview",
                "fields": [{"name": "Region"}, {"name": "Revenue"}],
            }
        ],
    }

    report = run_full_equivalence_suite(tableau_export, {"project_dir": "tmp"}, verbose=False)
    assert report["total"] >= 3
    assert report["failed"] == 0
