"""
Regression test for boolean-valued *column* filter conversion.

Tableau may emit a categorical filter on a boolean column (e.g.
``Date Signature Surveillant PAR`` derived in Power Query as
``type logical``) with values ``"true"`` / ``"false"``.  Power BI
Desktop rejects every JSON form that places a boolean literal directly
into a column filter:

* ``Categorical In(boolean)`` triggers ``visitIn`` ("a.accept is not a
  function") in the SQExprValidationVisitor.
* ``Advanced Comparison(== "true")`` triggers ``visitCompare``
  ("a.accept is not a function") and prevents the report from
  rendering.

The fix in :mod:`pbip_generator._create_visual_filters` is to **drop**
boolean column filters entirely with a logger note, since the
underlying Tableau semantic ("show rows where the boolean column is
TRUE") is typically already encoded inside the column's M expression.
This test asserts that no Advanced/Comparison and no In filter with
boolean literals leaks into the output.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'powerbi_import'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from pbip_generator import PowerBIProjectGenerator


class TestBooleanColumnFilter(unittest.TestCase):
    """Boolean column filters must be skipped, not emitted."""

    def setUp(self):
        self.gen = PowerBIProjectGenerator(output_dir=tempfile.mkdtemp())
        self.gen._main_table = 'FactTable'
        self.gen._field_map = {
            'Flag Col': ('FactTable', 'Flag Col'),
            'Other Col': ('FactTable', 'Other Col'),
        }
        # Both columns exist in the BIM
        self.gen._actual_bim_symbols = {
            ('FactTable', 'Flag Col'),
            ('FactTable', 'Other Col'),
        }
        # No measures
        self.gen._bim_measure_names = set()
        # Flag Col is boolean, Other Col is string
        self.gen._actual_bim_column_types = {
            ('FactTable', 'Flag Col'): 'boolean',
            ('FactTable', 'Other Col'): 'string',
        }

    def _run(self, filters):
        return self.gen._create_visual_filters(filters)

    def test_true_value_filter_is_dropped(self):
        """boolean column == TRUE → filter omitted entirely."""
        out = self._run([{
            'field': 'Flag Col',
            'datasource': '',
            'type': 'categorical',
            'values': ['true'],
        }])
        self.assertEqual(out, [], "Boolean column filter must be dropped")

    def test_false_value_filter_is_dropped(self):
        out = self._run([{
            'field': 'Flag Col',
            'datasource': '',
            'type': 'categorical',
            'values': ['false'],
        }])
        self.assertEqual(out, [])

    def test_quoted_boolean_values_are_dropped(self):
        """Tableau wraps values in double quotes (e.g. ``"true"``)."""
        out = self._run([{
            'field': 'Flag Col',
            'datasource': '',
            'type': 'categorical',
            'values': ['"true"', '"false"'],
        }])
        self.assertEqual(out, [])

    def test_no_boolean_literal_emitted_anywhere(self):
        """Regression: scan emitted filters for any boolean literal leak."""
        out = self._run([{
            'field': 'Flag Col',
            'datasource': '',
            'type': 'categorical',
            'values': ['true', 'false'],
        }])
        # Verify nothing was emitted; defensively scan if anything is.
        for flt in out:
            for w in flt.get('filter', {}).get('Where', []):
                cond = w.get('Condition', {})
                # Check Comparison.Right.Literal
                comp = cond.get('Comparison')
                if comp is not None:
                    val = comp.get('Right', {}).get('Literal', {}).get('Value')
                    self.assertNotIn(val, ('true', 'false'),
                                     "Comparison form leaked boolean literal")
                # Check In.Values literals
                in_node = cond.get('In')
                if in_node is not None:
                    for value_group in in_node.get('Values', []):
                        for lit in value_group:
                            v = lit.get('Literal', {}).get('Value')
                            self.assertNotIn(v, ('true', 'false'),
                                             "In form leaked boolean literal")

    def test_non_boolean_column_filter_preserved(self):
        """Non-boolean column filter alongside boolean filter must survive."""
        out = self._run([
            {
                'field': 'Flag Col',
                'datasource': '',
                'type': 'categorical',
                'values': ['true'],
            },
            {
                'field': 'Other Col',
                'datasource': '',
                'type': 'categorical',
                'values': ['Foo', 'Bar'],
            },
        ])
        self.assertEqual(len(out), 1, "Only the non-boolean filter should remain")
        flt = out[0]
        self.assertEqual(flt['type'], 'Categorical')
        # Field must reference Other Col
        self.assertEqual(flt['field']['Column']['Property'], 'Other Col')


if __name__ == '__main__':
    unittest.main()
