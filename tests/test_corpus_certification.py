"""Tests for corpus certification and release boundaries."""

import unittest
import json
import os
import tempfile

from powerbi_import.corpus_certification import certify_corpus, certify_quality_files


class TestCorpusCertification(unittest.TestCase):
    def test_classifies_static_ready_review_and_blocked_items(self):
        result = certify_corpus([
            {
                "report_name": "Ready",
                "status": "PASS",
                "blockers": [],
                "summary": {
                    "validation": {"release_ready": True, "semantic_execution": "passed"},
                    "recovery": {"status": "complete"},
                    "roundtrip": {"static_status": "static_pass"},
                },
            },
            {
                "report_name": "Review",
                "status": "WARN",
                "blockers": [],
                "summary": {
                    "validation": {"release_ready": False, "semantic_execution": "not_run"},
                    "recovery": {"status": "pending"},
                    "roundtrip": {"static_status": "static_pass"},
                },
            },
            {"report_name": "Blocked", "status": "FAIL", "blockers": ["bad model"]},
        ])

        self.assertEqual(result["counts"], {
            "certified_static": 1, "needs_review": 1, "blocked": 1,
        })
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["release_claim"], "static_corpus_only")
        self.assertEqual(result["runtime_boundary"]["desktop"], "not_run")
        self.assertIn("semantic execution is not_run", result["reports"][1]["reasons"])

    def test_empty_corpus_needs_review(self):
        result = certify_corpus([])
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["corpus_count"], 0)

    def test_current_quality_schema_is_classified_and_not_promoted(self):
        result = certify_corpus([{
            "report_name": "Current",
            "status": "WARN",
            "recovery": {"status": "complete"},
            "validation_contract": {
                "release_ready": False,
                "semantic": {"execution_status": "not_run"},
            },
            "roundtrip_validation": {
                "static": {"status": "static_pass"},
            },
        }])

        self.assertEqual(result["counts"]["needs_review"], 1)
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("semantic execution is not_run", result["reports"][0]["reasons"])

    def test_quality_files_aggregate_and_record_load_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            quality_path = os.path.join(tmp, "quality.json")
            with open(quality_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "report_name": "Demo",
                    "status": "FAIL",
                    "blockers": ["invalid model"],
                }, handle)

            result = certify_quality_files([quality_path, os.path.join(tmp, "missing.json")])

        self.assertEqual(result["quality_files"], 1)
        self.assertEqual(len(result["load_errors"]), 1)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["counts"]["blocked"], 1)


if __name__ == "__main__":
    unittest.main()
