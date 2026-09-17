"""The DAX healer must repair Tableau function leaks, not just syntax.

heal_dax previously fixed parens, commas and `==` but left COUNTD, DATEPART and
friends untouched, so the autoheal loop could report success on an expression
Power BI Desktop would still refuse to load — and which the validator and the
preceptor both flag.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.dax_healing import heal_dax, heal_tableau_leaks
from powerbi_import.dax_validator import (
    TABLEAU_LEAK_REPLACEMENTS,
    validate_dax_expression,
)
from powerbi_import.healing_core import HIGH, LOW, MEDIUM
from powerbi_import.validator import ArtifactValidator


class TestLeakHealing(unittest.TestCase):
    def test_countd_becomes_distinctcount(self):
        healed, action = heal_tableau_leaks('COUNTD(T[Customer])')
        self.assertEqual(healed, 'DISTINCTCOUNT(T[Customer])')
        self.assertIsNotNone(action)

    def test_datepart_year_becomes_year(self):
        healed, _ = heal_tableau_leaks("DATEPART('year', T[Date])")
        self.assertEqual(healed, 'YEAR(T[Date])')

    def test_healed_output_passes_the_validator(self):
        """The healer must not declare success on DAX the validator rejects."""
        for expr in ('COUNTD(T[A])', "DATEPART('month', T[D])",
                     "DATEPART('quarter', T[D])"):
            with self.subTest(expression=expr):
                healed = heal_dax(expr).healed
                self.assertEqual(validate_dax_expression(healed), [])

    def test_leak_and_syntax_are_fixed_together(self):
        report = heal_dax('COUNTD(T[A]')
        self.assertEqual(report.healed, 'DISTINCTCOUNT(T[A])')
        self.assertEqual(validate_dax_expression(report.healed), [])

    def test_heal_dax_records_the_leak_action(self):
        report = heal_dax('COUNTD(T[A])')
        self.assertIn('tableau_leaks', [a.healer for a in report.actions])


class TestHealerIsConservative(unittest.TestCase):
    def test_valid_dax_is_untouched(self):
        self.assertIsNone(heal_tableau_leaks('DISTINCTCOUNT(T[A])')[1])

    def test_idempotent(self):
        once, _ = heal_tableau_leaks('COUNTD(T[A])')
        self.assertIsNone(heal_tableau_leaks(once)[1])

    def test_string_literal_is_not_rewritten(self):
        expr = 'IF(T[Name] = "COUNTD(", 1, 0)'
        self.assertIsNone(heal_tableau_leaks(expr)[1])

    def test_bracketed_identifier_is_not_rewritten(self):
        expr = "SUM('T'[COUNTD(x)])"
        self.assertIsNone(heal_tableau_leaks(expr)[1])

    def test_only_high_confidence_rules_are_auto_applied(self):
        """Meaning-changing rewrites stay opt-in via the validator's auto-fix."""
        self.assertIsNone(heal_tableau_leaks('ATTR(T[A])')[1])
        self.assertIsNone(heal_tableau_leaks('IF(a) ELSEIF(b)')[1])

    def test_structural_leaks_are_never_guessed_at(self):
        for expr in ('{FIXED [R] : SUM([S])}', 'MAKEPOINT(T[La], T[Lo])',
                     'RUNNING_SUM(SUM(T[S]))'):
            with self.subTest(expression=expr):
                self.assertIsNone(heal_tableau_leaks(expr)[1])


class TestCanonicalRepairTable(unittest.TestCase):
    def test_confidence_vocabulary_matches_healing_core(self):
        """A mismatched vocabulary silently emptied the healer's rule set."""
        allowed = {HIGH, MEDIUM, LOW}
        for pattern, _replacement, confidence in TABLEAU_LEAK_REPLACEMENTS:
            with self.subTest(pattern=pattern):
                self.assertIn(confidence, allowed)

    def test_validator_derives_every_rule_in_order(self):
        self.assertEqual(
            [p.pattern for p, _ in ArtifactValidator._AUTO_FIX_RULES],
            [p for p, _r, _c in TABLEAU_LEAK_REPLACEMENTS],
        )

    def test_validator_auto_fix_still_repairs_countd(self):
        fixed, repairs = ArtifactValidator.auto_fix_dax_leaks('COUNTD(T[A])')
        self.assertEqual(fixed, 'DISTINCTCOUNT(T[A])')
        self.assertTrue(repairs)


if __name__ == '__main__':
    unittest.main()
