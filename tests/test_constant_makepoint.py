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
    FAIL,
    INFO,
    PASS,
    WARN,
    _check_calculations,
    _is_constant_point_only,
)


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

    def test_row_dependent_condition_is_not_constant(self):
        self.assertFalse(_is_constant_point_only(
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

    def test_quoted_table_calc_lod_and_script_text_is_ignored(self):
        checks = _checks(
            '"WINDOW_SUM(1) {INCLUDE [Region] : SUM([Sales])} SCRIPT_REAL(1)"')
        for check_name in (
            'Table calculations',
            'LOD expressions',
            'SCRIPT_* analytics extensions',
        ):
            with self.subTest(check=check_name):
                self.assertEqual(checks[check_name].severity, PASS)
                self.assertNotIn('Calc 0', checks[check_name].detail)

    def test_real_table_calculation_is_detected(self):
        checks = _checks('WINDOW_SUM(1)')
        self.assertEqual(checks['Table calculations'].severity, INFO)
        self.assertIn('Calc 0', checks['Table calculations'].detail)

    def test_real_lod_expression_is_detected(self):
        checks = _checks('{INCLUDE [Region] : SUM([Sales])}')
        self.assertEqual(checks['LOD expressions'].severity, INFO)
        self.assertIn('Calc 0', checks['LOD expressions'].detail)

    def test_real_script_extension_is_detected(self):
        checks = _checks('SCRIPT_REAL(1)')
        self.assertEqual(checks['SCRIPT_* analytics extensions'].severity, WARN)
        self.assertIn('Calc 0', checks['SCRIPT_* analytics extensions'].detail)

    def test_a_constant_point_warns_instead_of_failing(self):
        checks = _checks('MAKEPOINT(48.85, 2.35)')
        self.assertEqual(checks['Unsupported functions'].severity, PASS)
        self.assertEqual(checks['Constant MAKEPOINT'].severity, WARN)

    def test_scientific_notation_constant_point_warns_not_fails(self):
        formula = 'MAKEPOINT(1e2, 2)'
        self.assertTrue(_is_constant_point_only(formula))

        checks = _checks(formula)
        self.assertEqual(checks['Unsupported functions'].severity, PASS)
        self.assertEqual(checks['Constant MAKEPOINT'].severity, WARN)

    def test_a_point_from_columns_still_fails(self):
        checks = _checks('MAKEPOINT([Lat], [Lon])')
        self.assertEqual(checks['Unsupported functions'].severity, FAIL)
        self.assertNotIn('Constant MAKEPOINT', checks)

    def test_constant_point_under_row_condition_fails(self):
        checks = _checks('IF [Flag] THEN MAKEPOINT(1, 2) END')
        self.assertEqual(checks['Unsupported functions'].severity, FAIL)
        self.assertNotIn('Constant MAKEPOINT', checks)

    def test_quoted_column_call_shaped_text_is_ignored(self):
        checks = _checks('"MAKEPOINT([Lat], [Lon])"')
        self.assertEqual(checks['Unsupported functions'].severity, PASS)
        self.assertNotIn('Constant MAKEPOINT', checks)

    def test_quoted_constant_call_shaped_text_does_not_warn(self):
        checks = _checks('"MAKEPOINT(1, 2)"')
        self.assertEqual(checks['Unsupported functions'].severity, PASS)
        self.assertNotIn('Constant MAKEPOINT', checks)

    def test_real_call_is_detected_alongside_quoted_call_shaped_text(self):
        checks = _checks(
            'IF "MAKEPOINT([Lat], [Lon])" = "x" '
            'THEN MAKEPOINT([Lat], [Lon]) END')
        self.assertEqual(checks['Unsupported functions'].severity, FAIL)
        self.assertNotIn('Constant MAKEPOINT', checks)

    def test_call_shaped_text_in_a_comment_is_ignored(self):
        checks = _checks('SUM([Sales]) // MAKEPOINT(1, 2)')
        self.assertEqual(checks['Unsupported functions'].severity, PASS)
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
