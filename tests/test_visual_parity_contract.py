"""Tests for source-aware Power BI visual parity evidence."""

import unittest

from powerbi_import.visual_parity_contract import build_visual_parity_contract


class TestVisualParityContract(unittest.TestCase):
    def test_separates_registry_coverage_source_usage_and_target_risk(self):
        contract = build_visual_parity_contract(
            {"worksheets": [{"name": "Gantt", "chart_type": "Gantt Bar"}]},
            {"summary": {"status_counts": {
                "native": 129, "approximation": 16, "custom_visual": 19,
            }}},
            {"visual_count": 4, "counts": {
                "valid": 3, "empty": 0, "orphaned": 1, "unvalidated": 0,
            }},
        )

        self.assertEqual(contract["registry"]["approximation"], 16)
        self.assertEqual(contract["source_used"]["approximation_count"], 1)
        self.assertEqual(contract["target_recovery"]["risk_count"], 1)
        self.assertEqual(contract["remediation_owner"], "@visual")
        self.assertEqual(contract["runtime"], "not_run")

    def test_clean_target_and_source_have_no_remediation_owner(self):
        contract = build_visual_parity_contract(
            {"worksheets": [{"name": "Sales", "chart_type": "bar"}]},
            {"summary": {"status_counts": {
                "native": 1, "approximation": 0, "custom_visual": 0,
            }}},
            {"visual_count": 1, "counts": {
                "valid": 1, "empty": 0, "orphaned": 0, "unvalidated": 0,
            }},
        )

        self.assertEqual(contract["source_used"]["approximation_count"], 0)
        self.assertIsNone(contract["remediation_owner"])


if __name__ == "__main__":
    unittest.main()
