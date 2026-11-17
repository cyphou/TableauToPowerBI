"""A calculated column may not call a measure.

The measure's filter context is unknown at row level, so the engine makes the
column depend on its whole table, detects a circular dependency and refuses to
load the model.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from powerbi_import.tmdl_dax_postprocess import (
    _promote_measure_dependent_calc_columns,
    _unwrap_aggregations_of_measures,
)


def _table(columns, measures):
    return {"name": "Sales", "columns": columns, "measures": measures}


class TestPromoteMeasureDependentCalcColumns(unittest.TestCase):

    def test_calc_column_calling_a_measure_becomes_a_measure(self):
        t = _table(
            [{"name": "Weighted", "expression": "[Total] * 'Sales'[Amount]",
              "isCalculated": True}],
            [{"name": "Total", "expression": "SUM('Sales'[Amount])"}],
        )
        promoted = _promote_measure_dependent_calc_columns(t)
        self.assertEqual([n for n, _ in promoted], ["Weighted"])
        self.assertEqual(t["columns"], [])
        self.assertIn("Weighted", {m["name"] for m in t["measures"]})

    def test_escaped_closing_bracket_measure_reference_is_detected(self):
        t = _table(
            [{"name": "Weighted", "expression": "[Net]] Total] * 'Sales'[Amount]",
              "isCalculated": True}],
            [{"name": "Net] Total", "expression": "SUM('Sales'[Amount])"}],
        )
        promoted = _promote_measure_dependent_calc_columns(t)
        self.assertEqual(promoted, [("Weighted", ["Net] Total"])])
        self.assertEqual(t["columns"], [])

    def test_measure_name_in_string_literal_does_not_promote_column(self):
        expr = 'IF(\'Sales\'[Category] = "[Total]", 1, 0)'
        t = _table(
            [{"name": "Flag", "expression": expr, "isCalculated": True}],
            [{"name": "Total", "expression": "SUM('Sales'[Amount])"}],
        )
        promoted = _promote_measure_dependent_calc_columns(t)
        self.assertEqual(promoted, [])
        self.assertEqual([column["name"] for column in t["columns"]], ["Flag"])

    def test_promoted_numeric_row_expression_gets_iterator_and_note(self):
        expr = "[Total] * 'Sales'[Amount]"
        t = _table(
            [{"name": "Weighted", "expression": expr, "dataType": "Double",
              "isCalculated": True, "_original_formula": "[Total] * [Amount]",
              "_tableau_calculation_identity": {
                                    "name": "[Weighted]", "caption": "Weighted"}},
                         {"name": "Amount", "sourceColumn": "Amount"}],
            [{"name": "Total", "expression": "SUM('Sales'[Amount])"}],
        )
        _promote_measure_dependent_calc_columns(t)
        moved = [m for m in t["measures"] if m["name"] == "Weighted"][0]
        self.assertIn("Unaggregated row expression best-effort approximation", moved["expression"])
        self.assertIn("SUMX('Sales',", moved["expression"])
        self.assertTrue(moved["expression"].endswith(expr + ")"))
        self.assertEqual(moved["_original_formula"], "[Total] * [Amount]")
        self.assertEqual(moved["_tableau_calculation_identity"]["name"], "[Weighted]")
        self.assertTrue(any(
            annotation.get("name") == "MigrationNote"
            for annotation in moved.get("annotations", [])
        ))

    def test_qualified_home_table_measure_reference_promotes(self):
        t = _table(
            [{"name": "Weighted", "expression": "'Sales'[Total] * 'Sales'[Amount]",
                            "dataType": "Double", "isCalculated": True},
                         {"name": "Amount", "sourceColumn": "Amount"}],
            [{"name": "Total", "expression": "SUM('Sales'[Amount])"}],
        )
        promoted = _promote_measure_dependent_calc_columns(t)
        self.assertEqual(promoted, [("Weighted", ["Total"])])
        moved = next(m for m in t["measures"] if m["name"] == "Weighted")
        self.assertIn("SUMX('Sales',", moved["expression"])

    def test_calc_column_over_plain_columns_is_left_alone(self):
        t = _table(
            [{"name": "Line Total",
              "expression": "'Sales'[Amount] * 'Sales'[Qty]",
              "isCalculated": True}],
            [{"name": "Total", "expression": "SUM('Sales'[Amount])"}],
        )
        promoted = _promote_measure_dependent_calc_columns(t)
        self.assertEqual(promoted, [])
        self.assertEqual(len(t["columns"]), 1)
        self.assertEqual(len(t["measures"]), 1)

    def test_source_column_is_never_promoted(self):
        t = _table(
            [{"name": "Amount", "sourceColumn": "Amount"}],
            [{"name": "Total", "expression": "SUM('Sales'[Amount])"}],
        )
        _promote_measure_dependent_calc_columns(t)
        self.assertEqual(len(t["columns"]), 1)

    def test_promotion_is_transitive(self):
        # B depends on A, which itself becomes a measure.
        t = _table(
            [{"name": "A", "expression": "[Total] * 2", "isCalculated": True},
             {"name": "B", "expression": "[A] + 1", "isCalculated": True}],
            [{"name": "Total", "expression": "SUM('Sales'[Amount])"}],
        )
        promoted = _promote_measure_dependent_calc_columns(t)
        self.assertEqual({n for n, _ in promoted}, {"A", "B"})
        self.assertEqual(t["columns"], [])

    def test_name_already_taken_by_a_measure_is_left_alone(self):
        t = _table(
            [{"name": "Total", "expression": "[Other] * 2",
              "isCalculated": True}],
            [{"name": "Total", "expression": "SUM('Sales'[Amount])"},
             {"name": "Other", "expression": "SUM('Sales'[Qty])"}],
        )
        _promote_measure_dependent_calc_columns(t)
        self.assertEqual(len(t["columns"]), 1)
        self.assertEqual(len([m for m in t["measures"]
                              if m["name"] == "Total"]), 1)

    def test_no_measures_at_all_is_a_no_op(self):
        t = _table([{"name": "X", "expression": "[Y] * 2",
                     "isCalculated": True}], [])
        self.assertEqual(_promote_measure_dependent_calc_columns(t), [])
        self.assertEqual(len(t["columns"]), 1)

    def test_aggregation_of_promoted_measure_is_lowered_with_a_visible_warning(self):
        t = _table(
            [{"name": "Weighted", "expression": "[Total] * 'Sales'[Amount]",
              "isCalculated": True}],
            [{"name": "Total", "expression": "SUM('Sales'[Amount])"},
             {"name": "Grand", "expression": "CALCULATE(MIN('Sales'[Weighted]), ALL('Sales'))"}],
        )
        promoted = _promote_measure_dependent_calc_columns(t)
        self.assertEqual([name for name, _ in promoted], ["Weighted"])
        self.assertNotIn("Weighted", {column["name"] for column in t["columns"]})
        self.assertIn("Weighted", {measure["name"] for measure in t["measures"]})
        _unwrap_aggregations_of_measures(t)
        grand = [m for m in t["measures"] if m["name"] == "Grand"][0]
        self.assertIn("Measure aggregation best-effort approximation", grand["expression"])
        self.assertIn("MINX('Sales', [Weighted])", grand["expression"])
        self.assertNotIn("TODO: DAX conversion validation failed", grand["expression"])
        self.assertTrue(any(
            annotation.get("name") == "MigrationNote"
            and "MIN over measure Weighted lowered to MINX"
            in annotation.get("value", "")
            for annotation in grand.get("annotations", [])
        ))


if __name__ == "__main__":
    unittest.main()
