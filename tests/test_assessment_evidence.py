"""Tests for normalized assessment evidence."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from powerbi_import.assessment import AssessmentReport, CategoryResult, CheckItem
from powerbi_import.assessment_evidence import build_assessment_evidence


class TestAssessmentEvidence(unittest.TestCase):
    def test_normalizes_findings_with_scope_owner_and_action(self):
        report = AssessmentReport(
            workbook_name="Demo",
            timestamp="2026-09-15T00:00:00Z",
            categories=[
                CategoryResult(name="Visual Fidelity", checks=[
                    CheckItem("Visual Fidelity", "Chart mapping", "warn", "Approximation used"),
                ]),
                CategoryResult(name="Calculations", checks=[
                    CheckItem("Calculations", "DAX", "fail", "Unsupported formula", "Replace formula"),
                ]),
            ],
        )

        evidence = build_assessment_evidence(report)

        self.assertEqual(evidence["schema_version"], "1.0")
        self.assertEqual(evidence["counts"], {"pass": 0, "warn": 1, "fail": 1, "not_run": 0})
        self.assertEqual(evidence["findings"][0]["target_scope"], "PBIR")
        self.assertEqual(evidence["findings"][0]["owner"], "@visual")
        self.assertEqual(evidence["findings"][1]["next_action"], "Replace formula")
        self.assertTrue(evidence["findings"][0]["evidence_id"].startswith("assessment."))

    def test_info_is_non_blocking_pass_and_unknown_scope_defaults_to_assessor(self):
        report = AssessmentReport(
            workbook_name="Demo",
            timestamp="2026-09-15T00:00:00Z",
            categories=[CategoryResult(name="General", checks=[
                CheckItem("General", "Note", "info", "Informational"),
            ])],
        )

        finding = build_assessment_evidence(report)["findings"][0]

        self.assertEqual(finding["state"], "pass")
        self.assertEqual(finding["owner"], "@assessor")
        self.assertEqual(finding["target_scope"], "migration")


if __name__ == "__main__":
    unittest.main()
