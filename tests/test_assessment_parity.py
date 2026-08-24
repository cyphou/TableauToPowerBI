"""Tests for the Functionality Parity assessment category (Sprint 209.2)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))

from powerbi_import.assessment import (  # noqa: E402
    run_assessment, _check_functionality_parity, PASS, INFO, WARN, FAIL,
)


def _extracted_with_gaps():
    return {
        "datasources": [{"name": "DS", "tables": []}],
        "calculations": [{"name": "c", "formula": "[A]-[B]", "role": "measure"}],
        "data_blending": [{"primary": "A", "secondary": "B"}],  # approximated
        "worksheets": [{"forecasting": [{"model": "auto"}]}],   # unsupported
    }


def _clean_extracted():
    return {
        "datasources": [{"name": "DS", "tables": []}],
        "filters": [{"field": "x"}],
        "worksheets": [{"forecasting": [], "clustering": [], "trend_lines": []}],
    }


class TestParityCategory(unittest.TestCase):
    def test_category_present_in_full_assessment(self):
        report = run_assessment(_clean_extracted(), workbook_name="WB")
        names = [c.name for c in report.categories]
        self.assertIn("Functionality Parity", names)

    def test_has_parity_score_check(self):
        cat = _check_functionality_parity(_clean_extracted())
        summary = [c for c in cat.checks if c.name == "Parity score"]
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0].severity, PASS)
        self.assertIn("%", summary[0].detail)

    def test_gaps_are_info_severity(self):
        cat = _check_functionality_parity(_extracted_with_gaps())
        gap_checks = [c for c in cat.checks if c.name != "Parity score"]
        self.assertTrue(gap_checks)
        # Parity findings must be INFO-only so they never change overall score.
        for c in gap_checks:
            self.assertEqual(c.severity, INFO)

    def test_category_never_warns_or_fails(self):
        cat = _check_functionality_parity(_extracted_with_gaps())
        self.assertEqual(cat.warn_count, 0)
        self.assertEqual(cat.fail_count, 0)

    def test_no_gaps_reports_clean(self):
        cat = _check_functionality_parity(_clean_extracted())
        clean = [c for c in cat.checks if c.name == "No parity gaps"]
        self.assertEqual(len(clean), 1)

    def test_does_not_change_overall_score(self):
        # A workbook with only approximated/unsupported parity gaps but no other
        # warn/fail should still assess GREEN (parity findings are INFO).
        report = run_assessment({
            "datasources": [{"name": "DS", "tables": []}],
            "worksheets": [{"forecasting": [{"m": 1}]}],
        }, workbook_name="WB")
        parity = [c for c in report.categories if c.name == "Functionality Parity"][0]
        self.assertEqual(parity.worst_severity, INFO)


if __name__ == "__main__":
    unittest.main()
