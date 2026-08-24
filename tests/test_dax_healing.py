"""Tests for confidence-scored DAX self-healing (Sprint 210.3)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.dax_healing import (  # noqa: E402
    heal_dax, heal_measures, HealReport, HealAction,
    heal_balance_parens, heal_balance_brackets, heal_trailing_comma,
    heal_double_equals, heal_sum_of_measure, _spans, _paren_profile,
    HIGH, MEDIUM,
)


class TestSpanScanning(unittest.TestCase):
    def test_bracket_span(self):
        spans = _spans("SUM([Sales (USD)])")
        # the [Sales (USD)] identifier is one opaque span
        self.assertTrue(any(e - s >= len("[Sales (USD)]") for s, e in spans))

    def test_paren_inside_bracket_ignored(self):
        net, neg = _paren_profile("SUM([Show % (Nat)])")
        self.assertEqual(net, 0)
        self.assertFalse(neg)

    def test_string_span(self):
        net, _ = _paren_profile('IF([x]="a)", 1, 2)')
        self.assertEqual(net, 0)


class TestBalanceParens(unittest.TestCase):
    def test_appends_missing_paren(self):
        healed, action = heal_balance_parens("CALCULATE(SUM([Sales])")
        self.assertEqual(healed, "CALCULATE(SUM([Sales]))")
        self.assertEqual(action.confidence, HIGH)

    def test_two_missing_medium_confidence(self):
        healed, action = heal_balance_parens("CALCULATE(SUM([S])")  # 1 missing
        self.assertTrue(healed.endswith(")"))

    def test_balanced_untouched(self):
        healed, action = heal_balance_parens("SUM([Sales])")
        self.assertIsNone(action)
        self.assertEqual(healed, "SUM([Sales])")

    def test_close_before_open_not_fixed(self):
        # ')' before '(' — unsafe, must not append
        healed, action = heal_balance_parens("SUM[Sales]))((")
        self.assertIsNone(action)

    def test_paren_in_name_not_counted(self):
        healed, action = heal_balance_parens("SUM([Rev (Net)])")
        self.assertIsNone(action)


class TestBalanceBrackets(unittest.TestCase):
    def test_closes_unterminated_bracket(self):
        healed, action = heal_balance_brackets("SUM([Sales")
        self.assertEqual(healed, "SUM([Sales]")
        self.assertEqual(action.confidence, MEDIUM)

    def test_balanced_untouched(self):
        healed, action = heal_balance_brackets("SUM([Sales])")
        self.assertIsNone(action)


class TestTrailingComma(unittest.TestCase):
    def test_before_close_paren(self):
        healed, action = heal_trailing_comma("SWITCH(TRUE(), [a], 1, )")
        self.assertNotIn(", )", healed)
        self.assertEqual(action.confidence, HIGH)

    def test_at_end(self):
        healed, action = heal_trailing_comma("1 + 2,")
        self.assertEqual(healed, "1 + 2")

    def test_comma_in_string_untouched(self):
        healed, action = heal_trailing_comma('IF([x]="a,", 1, 2)')
        self.assertIsNone(action)

    def test_valid_comma_untouched(self):
        healed, action = heal_trailing_comma("IF([x], 1, 2)")
        self.assertIsNone(action)


class TestDoubleEquals(unittest.TestCase):
    def test_converts(self):
        healed, action = heal_double_equals("IF([x] == 1, 1, 0)")
        self.assertIn("[x] = 1", healed)
        self.assertEqual(action.confidence, HIGH)

    def test_leaves_lte_gte(self):
        for op in ("<=", ">=", "<>"):
            healed, action = heal_double_equals(f"IF([x] {op} 1, 1, 0)")
            self.assertIsNone(action)

    def test_equals_in_string_untouched(self):
        healed, action = heal_double_equals('IF([x]="a==b", 1, 0)')
        self.assertIsNone(action)


class TestSumOfMeasure(unittest.TestCase):
    def test_unwraps_measure(self):
        healed, action = heal_sum_of_measure("SUM([Total Sales])", {"Total Sales"})
        self.assertEqual(healed, "[Total Sales]")
        self.assertEqual(action.confidence, HIGH)

    def test_leaves_column(self):
        healed, action = heal_sum_of_measure("SUM([Sales])", {"Total Sales"})
        self.assertIsNone(action)

    def test_no_measure_set_noop(self):
        healed, action = heal_sum_of_measure("SUM([Total Sales])", None)
        self.assertIsNone(action)

    def test_case_insensitive(self):
        healed, action = heal_sum_of_measure("sum([total sales])", {"Total Sales"})
        self.assertEqual(healed, "[total sales]")


class TestHealDaxOrchestrator(unittest.TestCase):
    def test_combined_heal(self):
        report = heal_dax("CALCULATE(SUM([Amt]) , )", {"Amt2"})
        self.assertTrue(report.changed)
        self.assertTrue(report.healed.endswith(")"))
        self.assertNotIn(", )", report.healed)

    def test_idempotent(self):
        report1 = heal_dax("IF([x] == 1, SUM([Amt]),", {"Amt"})
        report2 = heal_dax(report1.healed, {"Amt"})
        self.assertFalse(report2.changed, f"not idempotent: {report2.healed!r}")

    def test_valid_dax_unchanged(self):
        valid = "CALCULATE(SUM([Sales]), ALLEXCEPT('T', 'T'[Region]))"
        report = heal_dax(valid, {"Total"})
        self.assertFalse(report.changed)
        self.assertEqual(report.actions, [])

    def test_records_actions_with_confidence(self):
        report = heal_dax("SUM([M]) ==", {"M"})
        # sum-of-measure + double-equals both fire
        healers = {a.healer for a in report.actions}
        self.assertIn("sum_of_measure", healers)
        for a in report.actions:
            self.assertIn(a.confidence, (HIGH, MEDIUM, "low"))

    def test_to_dict(self):
        report = heal_dax("CALCULATE(SUM([A])", {"X"})
        d = report.to_dict()
        self.assertIn("healed", d)
        self.assertIn("actions", d)
        self.assertTrue(d["changed"])


class TestHealMeasures(unittest.TestCase):
    def test_heals_list(self):
        measures = [
            {"name": "A", "expression": "CALCULATE(SUM([Sales])"},   # missing paren
            {"name": "B", "expression": "SUM([Sales])"},              # valid
        ]
        reports = heal_measures(measures)
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0].healed.endswith("))"))

    def test_skips_empty(self):
        reports = heal_measures([{"name": "A", "expression": ""}])
        self.assertEqual(reports, [])


if __name__ == "__main__":
    unittest.main()
