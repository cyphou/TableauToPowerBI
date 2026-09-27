"""A point built from constants is not a migration blocker.

MAKEPOINT has no Power BI equivalent, so the assessment called every use of
it blocking and told the user to rewrite or remove it. On a real workbook the
only uses were MAKEPOINT(<number>, <number>) as a FIXED dimension: grouping
by one fixed location is the same as not grouping, so the migrated DAX gives
the same numbers. The workbook opened and computed correctly while its report
said FAIL.

A point built from columns is different: it is a real per-row grain the model
cannot hold, and that must stay blocking.
"""
import unittest

from powerbi_import.assessment import (
    FAIL, PASS, WARN, _check_calculations, _is_constant_point_only)


def _checks(*formulas):
    calcs = [{'name': 'c%d' % i, 'caption': 'Calc %d' % i, 'formula': f}
             for i, f in enumerate(formulas)]
    result = _check_calculations({'calculations': calcs})
    return {c.name: c for c in result.checks}


class TestConstantPointIsRecognised(unittest.TestCase):

    def test_two_numeric_literals(self):
        self.assertTrue(_is_constant_point_only('MAKEPOINT(48.85, 2.35)'))

    def test_negative_and_signed_values(self):
        self.assertTrue(_is_constant_point_only('MAKEPOINT(-33.9, +18.4)'))

    def test_integers(self):
        self.assertTrue(_is_constant_point_only('MAKEPOINT(0, 0)'))

    def test_the_srid_form(self):
        self.assertTrue(_is_constant_point_only('MAKEPOINT(652000, 6862000, 2154)'))

    def test_inside_a_larger_expression(self):
        self.assertTrue(_is_constant_point_only(
            'IF [x] > 0 THEN MAKEPOINT(1.5, 2.5) END'))


class TestAnythingElseIsNotConstant(unittest.TestCase):

    def test_a_point_from_columns(self):
        self.assertFalse(_is_constant_point_only('MAKEPOINT([Lat], [Lon])'))

    def test_one_column_one_constant(self):
        self.assertFalse(_is_constant_point_only('MAKEPOINT([Lat], 2.35)'))

    def test_a_computed_argument(self):
        self.assertFalse(_is_constant_point_only('MAKEPOINT(1 + 1, 2)'))

    def test_a_nested_call(self):
        self.assertFalse(_is_constant_point_only('MAKEPOINT(ABS(-1), 2)'))

    def test_another_unsupported_function(self):
        self.assertFalse(_is_constant_point_only('BUFFER(MAKEPOINT(1, 2), 5, "km")'))

    def test_a_constant_point_beside_a_real_one(self):
        self.assertFalse(_is_constant_point_only(
            'IF [x] THEN MAKEPOINT(1, 2) ELSE MAKEPOINT([Lat], [Lon]) END'))

    def test_the_wrong_arity(self):
        self.assertFalse(_is_constant_point_only('MAKEPOINT(1)'))

    def test_an_unclosed_call(self):
        self.assertFalse(_is_constant_point_only('MAKEPOINT(1, 2'))

    def test_no_unsupported_function_at_all(self):
        self.assertFalse(_is_constant_point_only('SUM([Sales])'))


class TestTheVerdict(unittest.TestCase):

    def test_a_constant_point_warns_instead_of_failing(self):
        checks = _checks('MAKEPOINT(48.85, 2.35)')
        self.assertEqual(checks['Unsupported functions'].severity, PASS)
        self.assertEqual(checks['Constant MAKEPOINT'].severity, WARN)

    def test_a_point_from_columns_still_fails(self):
        checks = _checks('MAKEPOINT([Lat], [Lon])')
        self.assertEqual(checks['Unsupported functions'].severity, FAIL)
        self.assertNotIn('Constant MAKEPOINT', checks)

    def test_both_kinds_are_reported_separately(self):
        checks = _checks('MAKEPOINT(1, 2)', 'MAKEPOINT([Lat], [Lon])')
        self.assertEqual(checks['Unsupported functions'].severity, FAIL)
        self.assertIn('Calc 1', checks['Unsupported functions'].detail)
        self.assertNotIn('Calc 0', checks['Unsupported functions'].detail)
        self.assertIn('Calc 0', checks['Constant MAKEPOINT'].detail)

    def test_other_unsupported_functions_still_fail(self):
        checks = _checks('COLLECT([Geometry])')
        self.assertEqual(checks['Unsupported functions'].severity, FAIL)


if __name__ == '__main__':
    unittest.main()
