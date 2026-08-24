"""
Regression test for boolean-valued measure filter handling.

A Tableau worksheet may filter on a measure that returns a boolean
expression (e.g. ``CALCULATE(COUNT(...) > 0)`` or ``[col] = [param]``)
and set the value to TRUE/FALSE.

History:

* Cycle 1 (initial fix) converted the boolean comparison into
  ``measure > 0`` (true) / ``measure <= 0`` (false), reasoning that
  the measure returned 0/1 numerically.
* Cycle 4 (current behaviour) discovered that PBI Desktop's
  ``SQExprValidationVisitor.visitCompare`` crashes with
  ``a.accept is not a function`` when the measure's actual return type
  is boolean or string — both of which Tableau measures often produce
  (e.g. ``CALCULATE(... > 0, ...)`` returns boolean,
  ``IF(..., "true", "false")`` returns string).  Numeric ``> 0``
  comparisons against non-numeric measures crash filter validation at
  report-open time.

The current fix in :mod:`pbip_generator._create_visual_filters` drops
boolean-valued measure filters universally with an info-level log.
The Tableau filter semantic ("where measure-condition holds") is
typically encoded in the measure's own DAX expression, so dropping the
filter is a no-op for the visual's data shape.

This test asserts that boolean-valued measure filters produce no
``visual_filters`` entries — neither ``Comparison`` nor ``In`` form —
regardless of the measure's declared return type.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'powerbi_import'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from pbip_generator import PowerBIProjectGenerator


class TestBooleanMeasureFilter(unittest.TestCase):
    """Boolean-valued measure filters must be dropped — never emitted."""

    def setUp(self):
        self.gen = PowerBIProjectGenerator(output_dir=tempfile.mkdtemp())
        # Minimal generator state required by _create_visual_filters
        self.gen._main_table = 'FactTable'
        self.gen._field_map = {
            'A_10_5 - Ps non AIP': ('FactTable', 'A_10_5 - Ps non AIP'),
        }
        self.gen._actual_bim_symbols = {
            ('FactTable', 'A_10_5 - Ps non AIP'),
        }
        self.gen._bim_measure_names = {'A_10_5 - Ps non AIP'}
        self.gen._actual_bim_measure_types = {
            ('FactTable', 'A_10_5 - Ps non AIP'): 'boolean',
        }

    def _run(self, values):
        filters = [{
            'field': 'A_10_5 - Ps non AIP',
            'datasource': '',
            'type': 'categorical',
            'values': values,
        }]
        return self.gen._create_visual_filters(filters)

    def test_true_value_is_dropped(self):
        """Filter ``measure == TRUE`` must be dropped (empty result)."""
        out = self._run(['true'])
        self.assertEqual(out, [],
                         "Boolean-valued measure filter must be dropped, "
                         "not emitted as Comparison")

    def test_false_value_is_dropped(self):
        """Filter ``measure == FALSE`` must be dropped (empty result)."""
        out = self._run(['false'])
        self.assertEqual(out, [],
                         "Boolean-valued measure filter must be dropped")

    def test_mixed_true_false_is_dropped(self):
        """Filter ``measure IN (TRUE, FALSE)`` must be dropped."""
        out = self._run(['true', 'false'])
        self.assertEqual(out, [],
                         "Mixed boolean-valued measure filter must be dropped")

    def test_quoted_tableau_boolean_values_are_dropped(self):
        """Tableau wraps values in double quotes (e.g. ``"true"``).

        After stripping, the value is recognised as boolean and dropped
        like any other boolean-valued measure filter.
        """
        out = self._run(['"true"'])
        self.assertEqual(out, [],
                         "Quoted boolean value must still be dropped")

    def test_no_compare_or_in_emitted_for_boolean_values(self):
        """Regression: no Comparison or In condition with boolean
        literals (or numeric ``0`` Right-side) is emitted for any
        boolean-valued measure filter."""
        for vals in (['true'], ['false'], ['true', 'false'], ['"true"']):
            out = self._run(vals)
            self.assertEqual(out, [], f"Filter not dropped for input {vals}")

    def test_non_boolean_value_is_preserved(self):
        """Sanity check: a measure filter with non-boolean string values
        is NOT affected by the boolean-drop logic — it is still emitted
        as a Comparison(Equal) per the existing measure-categorical
        branch."""
        # Use a measure with string return-type so a string equality
        # comparison is well-formed.
        self.gen._actual_bim_measure_types = {
            ('FactTable', 'A_10_5 - Ps non AIP'): 'string',
        }
        out = self._run(['Active'])
        self.assertEqual(len(out), 1,
                         "Non-boolean-valued measure filter must still be "
                         "emitted")
        flt = out[0]
        self.assertEqual(flt['type'], 'Advanced')
        self.assertIn('Measure', flt['field'])


if __name__ == '__main__':
    unittest.main()
