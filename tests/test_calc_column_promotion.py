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

    def test_expression_is_carried_over_unchanged(self):
        expr = "[Total] * 'Sales'[Amount]"
        t = _table(
            [{"name": "Weighted", "expression": expr, "isCalculated": True}],
            [{"name": "Total", "expression": "SUM('Sales'[Amount])"}],
        )
        _promote_measure_dependent_calc_columns(t)
        moved = [m for m in t["measures"] if m["name"] == "Weighted"][0]
        self.assertEqual(moved["expression"], expr)

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

    def test_call_sites_aggregating_the_promoted_name_are_repaired(self):
        # Another measure read the column as MIN('Sales'[Weighted]); once the
        # column is a measure that aggregation is invalid, and the existing
        # unwrap pass has to strip it.
        t = _table(
            [{"name": "Weighted", "expression": "[Total] * 'Sales'[Amount]",
              "isCalculated": True}],
            [{"name": "Total", "expression": "SUM('Sales'[Amount])"},
             {"name": "Grand", "expression": "CALCULATE(MIN('Sales'[Weighted]), ALL('Sales'))"}],
        )
        _promote_measure_dependent_calc_columns(t)
        _unwrap_aggregations_of_measures(t)
        grand = [m for m in t["measures"] if m["name"] == "Grand"][0]
        self.assertNotIn("MIN(", grand["expression"])
        self.assertIn("[Weighted]", grand["expression"])


if __name__ == "__main__":
    unittest.main()
