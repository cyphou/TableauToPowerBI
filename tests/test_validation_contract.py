"""Tests for the cross-artifact validation contract."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from powerbi_import.validation_contract import build_validation_contract


class TestValidationContract(unittest.TestCase):
    def test_not_run_semantic_and_pending_recovery_are_explicit(self):
        contract = build_validation_contract(
            {"issue_count": 0, "execution": {"status": "not_run"}},
            {"summary": {"errors": 0}, "fallback_in_use": []},
            {"counts": {"pending_validation": 2}},
        )

        self.assertEqual(contract["semantic"]["execution_status"], "not_run")
        self.assertEqual(contract["semantic"]["runtime"], "not_run")
        self.assertEqual(contract["recovery"]["status"], "pending")
        self.assertFalse(contract["release_ready"])

    def test_failures_and_fallbacks_are_classified(self):
        contract = build_validation_contract(
            {
                "issue_count": 1,
                "execution": {"status": "failed", "failed": 1},
                "measure_context": {"issue_count": 2},
            },
            {"summary": {"errors": 1}, "fallback_in_use": [{"connector": "Unknown"}]},
            {"counts": {"pending_validation": 0}},
        )

        self.assertEqual(contract["semantic"]["static_issue_count"], 3)
        self.assertEqual(contract["m"]["status"], "failed")
        self.assertEqual(contract["semantic"]["execution_status"], "failed")
        self.assertFalse(contract["release_ready"])

    def test_clean_contract_is_release_ready(self):
        contract = build_validation_contract(
            {"issue_count": 0, "execution": {"status": "passed"}},
            {"summary": {"errors": 0}, "fallback_in_use": []},
            {"counts": {"pending_validation": 0}},
        )

        self.assertTrue(contract["release_ready"])
        self.assertEqual(contract["m"]["status"], "passed")


if __name__ == "__main__":
    unittest.main()
