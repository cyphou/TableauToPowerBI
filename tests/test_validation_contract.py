"""Tests for the cross-artifact validation contract."""

import os
import sys
import unittest
from copy import deepcopy

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from powerbi_import.errors import ValidationError
from powerbi_import.semantic_runtime import validate_semantic_execution
from powerbi_import.validation_contract import build_validation_contract


class _OfflineSuccessfulExecutor:
    """Local deterministic fixture; it never opens a runtime or source connection."""

    def execute(self, query):
        return {"rows": [{"Value": 1}]}


class TestValidationContract(unittest.TestCase):
    @staticmethod
    def _successful_execution():
        return validate_semantic_execution(
            [
                {
                    "name": "offline-smoke",
                    "dax": 'EVALUATE ROW("Value", 1)',
                    "expected_rows": [{"Value": 1}],
                }
            ],
            _OfflineSuccessfulExecutor(),
        )

    @staticmethod
    def _clean_contract(execution):
        return build_validation_contract(
            {"issue_count": 0, "execution": execution},
            {"summary": {"errors": 0}, "fallback_in_use": []},
            {"counts": {"pending_validation": 0}},
        )

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
        execution = self._successful_execution()

        self.assertEqual(execution["status"], "passed")
        self.assertEqual(execution["queries_requested"], 1)
        self.assertEqual(execution["queries_run"], 1)
        self.assertEqual(execution["passed"], 1)
        self.assertEqual(execution["failed"], 0)
        self.assertEqual(execution["results"][0]["status"], "passed")
        contract = self._clean_contract(execution)

        self.assertTrue(contract["release_ready"])
        self.assertEqual(contract["m"]["status"], "passed")

    def test_bare_passed_status_is_not_release_ready(self):
        contract = self._clean_contract({"status": "passed"})

        self.assertFalse(contract["release_ready"])

    def test_readiness_requires_nonempty_complete_consistent_execution_evidence(self):
        complete = self._successful_execution()

        zero_query_suite = deepcopy(complete)
        zero_query_suite.update(
            queries_requested=0,
            queries_run=0,
            passed=0,
            failed=0,
            results=[],
        )
        partial_coverage = deepcopy(complete)
        partial_coverage["queries_requested"] = 2
        missing_results = deepcopy(complete)
        del missing_results["results"]
        empty_results = deepcopy(complete)
        empty_results["results"] = []
        missing_executor_descriptor = deepcopy(complete)
        del missing_executor_descriptor["executor"]
        unavailable_executor = deepcopy(complete)
        unavailable_executor["executor"]["status"] = "not_run"
        count_mismatch = deepcopy(complete)
        count_mismatch["queries_run"] = 0
        contradictory_pass_count = deepcopy(complete)
        contradictory_pass_count["passed"] = 0
        contradictory_failure_count = deepcopy(complete)
        contradictory_failure_count["failed"] = 1
        contradictory_result = deepcopy(complete)
        contradictory_result["results"][0]["status"] = "failed"

        cases = [
            ("zero-query suite", zero_query_suite),
            ("partial coverage", partial_coverage),
            ("missing results", missing_results),
            ("empty results", empty_results),
            ("missing executor descriptor", missing_executor_descriptor),
            ("executor did not run", unavailable_executor),
            ("run count contradicts results", count_mismatch),
            ("pass count contradicts results", contradictory_pass_count),
            ("failure count contradicts status", contradictory_failure_count),
            ("result contradicts summary", contradictory_result),
        ]
        for field in ("queries_requested", "queries_run", "passed", "failed"):
            missing_count = deepcopy(complete)
            del missing_count[field]
            cases.append((f"missing {field} count", missing_count))

        for field, boolean_value in (
            ("queries_requested", True),
            ("queries_run", True),
            ("passed", True),
            ("failed", False),
        ):
            boolean_count = deepcopy(complete)
            boolean_count[field] = boolean_value
            cases.append((f"boolean {field} count", boolean_count))

        for label, execution in cases:
            with self.subTest(case=label):
                contract = self._clean_contract(execution)
                self.assertFalse(contract["release_ready"])

    def test_explicit_semantic_failure_never_releases_an_otherwise_clean_contract(self):
        execution = self._successful_execution()
        execution["status"] = "failed"

        contract = self._clean_contract(execution)

        self.assertEqual(contract["semantic"]["execution_status"], "failed")
        self.assertFalse(contract["release_ready"])

    def test_invalid_top_level_evidence_mappings_never_create_success_contracts(self):
        semantic_context = {
            "issue_count": 0,
            "execution": self._successful_execution(),
        }
        valid_m_emitters = {"summary": {"errors": 0}, "fallback_in_use": []}
        valid_recovery = {"counts": {"pending_validation": 0}}
        cases = (
            ("m_emitters", [], valid_recovery, "m", "passed"),
            ("recovery", valid_m_emitters, [], "recovery", "complete"),
        )

        for label, m_emitters, recovery, section, success_status in cases:
            with self.subTest(evidence=label):
                try:
                    contract = build_validation_contract(
                        semantic_context,
                        m_emitters,
                        recovery,
                    )
                except ValidationError:
                    # Invalid top-level envelopes may be rejected explicitly;
                    # they must not be normalized into a success-shaped report.
                    continue

                self.assertIsInstance(contract, dict)
                self.assertFalse(contract.get("release_ready", False))
                self.assertNotEqual(contract.get("status"), "passed")
                section_evidence = contract.get(section)
                if isinstance(section_evidence, dict):
                    self.assertNotEqual(
                        section_evidence.get("status"), success_status
                    )


if __name__ == "__main__":
    unittest.main()
