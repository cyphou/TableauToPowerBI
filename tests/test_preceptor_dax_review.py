"""Regression tests for preceptor DAX review accuracy.

DAX has its own DATEADD(<dates>, <n>, <interval>) time-intelligence function and
the converter emits it on purpose, so only Tableau's scalar form — which takes a
quoted date-part as its first argument — is a genuine leak.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.dax_validator import (
    TABLEAU_LEAK_FUNCTIONS,
    validate_dax_expression,
)
from powerbi_import.preceptor import _TABLEAU_LEAK_RE


def _leaks(expr):
    return [p.pattern for p in _TABLEAU_LEAK_RE if p.search(expr)]


class TestDateAddLeakDetection(unittest.TestCase):
    def test_tableau_scalar_dateadd_is_flagged(self):
        self.assertTrue(_leaks("DATEADD('month', 1, [OrderDate])"))

    def test_tableau_scalar_dateadd_double_quoted_is_flagged(self):
        self.assertTrue(_leaks('DATEADD("month", 1, [OrderDate])'))

    def test_dax_time_intelligence_dateadd_is_not_flagged(self):
        self.assertEqual(_leaks("DATEADD(Calendar[Date], -1, YEAR)"), [])

    def test_dax_dateadd_nested_in_calculate_is_not_flagged(self):
        self.assertEqual(
            _leaks("CALCULATE([Sales], DATEADD(Calendar[Date], -1, YEAR))"), [])

    def test_two_dax_dateadds_are_not_flagged(self):
        """The previous lookahead made detection depend on later occurrences."""
        expr = ("DATEADD(Calendar[Date], -1, YEAR) + "
                "DATEADD(Calendar[Date], -2, YEAR)")
        self.assertEqual(_leaks(expr), [])

    def test_two_tableau_dateadds_are_flagged(self):
        expr = "DATEADD('month', 1, [a]) + DATEADD('month', 2, [b])"
        self.assertTrue(_leaks(expr))


class TestOtherLeaksStillDetected(unittest.TestCase):
    def test_countd_flagged(self):
        self.assertTrue(_leaks("COUNTD([Customer])"))

    def test_zn_flagged(self):
        self.assertTrue(_leaks("ZN([Sales])"))

    def test_clean_dax_not_flagged(self):
        self.assertEqual(_leaks("DISTINCTCOUNT('Orders'[Customer])"), [])


class TestLeakRegistryIsSingleSourceOfTruth(unittest.TestCase):
    """The preceptor must not approve DAX that dax_validator would reject.

    Both detectors derive from TABLEAU_LEAK_FUNCTIONS, so every name in the
    registry has to be caught on both sides. Before consolidation the preceptor
    silently passed RUNNING_*, WINDOW_*, RANK_*, LOOKUP and PREVIOUS_VALUE.
    """

    def test_every_registered_function_is_flagged_by_preceptor(self):
        for name in TABLEAU_LEAK_FUNCTIONS:
            with self.subTest(function=name):
                self.assertTrue(_leaks(f'{name}([Field])'))

    def test_every_registered_function_is_flagged_by_dax_validator(self):
        for name in TABLEAU_LEAK_FUNCTIONS:
            with self.subTest(function=name):
                self.assertTrue(validate_dax_expression(f'{name}([Field])'))

    def test_table_calc_leaks_reach_the_preceptor(self):
        for expr in ('RUNNING_SUM(SUM([Sales]))', 'WINDOW_AVG([X])',
                     'RANK_DENSE([Y])', 'LOOKUP([A], -1)',
                     'PREVIOUS_VALUE(0)'):
            with self.subTest(expression=expr):
                self.assertTrue(_leaks(expr))

    def test_lookupvalue_is_not_mistaken_for_tableau_lookup(self):
        expr = "LOOKUPVALUE('Dim'[Name], 'Dim'[Id], 'Fact'[Id])"
        self.assertEqual(_leaks(expr), [])
        self.assertEqual(validate_dax_expression(expr), [])


class TestParenBalanceIsSpanAware(unittest.TestCase):
    """The review must not count parens inside strings or bracketed names."""

    def setUp(self):
        from powerbi_import.dax_validator import _check_balanced
        self.check = _check_balanced

    def test_paren_inside_string_literal_is_not_grouping(self):
        self.assertEqual(self.check('IF([A] = "open (x", 1, 0)'), [])

    def test_paren_inside_bracketed_identifier_is_not_grouping(self):
        self.assertEqual(self.check("SUM('T'[Show % (Nat)])"), [])

    def test_genuinely_unbalanced_is_reported(self):
        self.assertNotEqual(self.check('SUM(T[Amount]'), [])


if __name__ == '__main__':
    unittest.main()
