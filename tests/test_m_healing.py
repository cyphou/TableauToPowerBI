"""Tests for confidence-scored Power Query M self-healing (Sprint 210.4)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'powerbi_import'))

from powerbi_import.m_healing import (  # noqa: E402
    heal_m, heal_quote_identifiers, heal_balance_parens, heal_trailing_comma,
    _m_spans, _paren_profile,
)
from powerbi_import.dax_healing import HIGH, MEDIUM  # noqa: E402


class TestMSpans(unittest.TestCase):
    def test_string_span(self):
        net, _ = _paren_profile('Text.Combine({"a)", "b("})')
        self.assertEqual(net, 0)

    def test_field_access_span(self):
        # paren inside [Rev (Net)] must not count
        net, _ = _paren_profile('Table.AddColumn(p, "c", each [Rev (Net)])')
        self.assertEqual(net, 0)

    def test_line_comment_span(self):
        net, _ = _paren_profile('Source // trailing ( comment\n')
        self.assertEqual(net, 0)

    def test_block_comment_span(self):
        net, _ = _paren_profile('Source /* ( ( */ ')
        self.assertEqual(net, 0)


class TestQuoteIdentifiers(unittest.TestCase):
    def test_quotes_special_char_field(self):
        healed, action = heal_quote_identifiers('each [Rev/Cost]')
        self.assertIn('[#"Rev/Cost"]', healed)
        self.assertEqual(action.confidence, HIGH)

    def test_leaves_simple_field(self):
        healed, action = heal_quote_identifiers('each [Sales]')
        self.assertIsNone(action)

    def test_idempotent(self):
        once, _ = heal_quote_identifiers('each [Rev/Cost]')
        twice, action = heal_quote_identifiers(once)
        self.assertIsNone(action)


class TestBalanceParens(unittest.TestCase):
    def test_appends_missing(self):
        healed, action = heal_balance_parens('Table.SelectRows(Source, each true')
        self.assertEqual(healed, 'Table.SelectRows(Source, each true)')
        self.assertEqual(action.confidence, HIGH)

    def test_balanced_untouched(self):
        healed, action = heal_balance_parens('Table.FirstN(Source, 10)')
        self.assertIsNone(action)

    def test_close_before_open_not_fixed(self):
        healed, action = heal_balance_parens('a) b (')
        self.assertIsNone(action)

    def test_paren_in_field_not_counted(self):
        healed, action = heal_balance_parens('each [Rev (Net)]')
        self.assertIsNone(action)


class TestTrailingComma(unittest.TestCase):
    def test_before_in(self):
        healed, action = heal_trailing_comma("let a = 1, in a")
        self.assertNotIn(", in", healed)
        self.assertEqual(action.confidence, HIGH)

    def test_before_close_paren(self):
        healed, action = heal_trailing_comma("Table.Combine({a, b, })")
        self.assertNotIn(", )", healed)

    def test_at_end(self):
        healed, action = heal_trailing_comma("Source,")
        self.assertEqual(healed, "Source")

    def test_valid_comma_untouched(self):
        healed, action = heal_trailing_comma("Table.FirstN(Source, 10)")
        self.assertIsNone(action)

    def test_comma_in_string_untouched(self):
        healed, action = heal_trailing_comma('Text.From("a, ")')
        self.assertIsNone(action)


class TestHealMOrchestrator(unittest.TestCase):
    def test_combined(self):
        report = heal_m('Table.SelectRows(Source, each [Rev/Cost] > 0, )')
        self.assertTrue(report.changed)
        self.assertIn('[#"Rev/Cost"]', report.healed)
        self.assertNotIn(', )', report.healed)
        self.assertTrue(report.healed.endswith(')'))

    def test_idempotent(self):
        r1 = heal_m('Table.SelectRows(Source, each [A/B] > 0,')
        r2 = heal_m(r1.healed)
        self.assertFalse(r2.changed, f"not idempotent: {r2.healed!r}")

    def test_valid_m_unchanged(self):
        valid = ('let Source = Table.FromRows({{1}}, {"A"}), '
                 'Filtered = Table.SelectRows(Source, each [A] > 0) in Filtered')
        report = heal_m(valid)
        self.assertFalse(report.changed)
        self.assertEqual(report.actions, [])

    def test_to_dict(self):
        report = heal_m('Table.FirstN(Source, 10')
        d = report.to_dict()
        self.assertIn("healed", d)
        self.assertTrue(d["changed"])


if __name__ == "__main__":
    unittest.main()
