"""Tests for the compact unified evidence summary."""

import unittest

from powerbi_import.evidence_summary import build_evidence_summary


class TestEvidenceSummary(unittest.TestCase):
    def test_summarizes_all_next_track_contracts(self):
        summary = build_evidence_summary({
            "status": "WARN",
            "handoff_status": "WARN",
            "blockers": [],
            "warnings": ["review"],
            "priorities": [{"priority": "P2"}],
            "source_inventory": {"object_count": 10, "orphan_count": 1},
            "recovery": {"status": "pending", "counts": {"pending_validation": 4}},
            "validation_contract": {
                "release_ready": False,
                "semantic": {"execution_status": "not_run"},
                "m": {"status": "warning"},
            },
            "visual_parity": {
                "source_used": {"approximation_count": 2},
                "target_recovery": {"risk_count": 1},
            },
            "visual_recovery": {"status": "scanned"},
            "roundtrip_validation": {
                "static": {"status": "static_fail"},
                "desktop": {"status": "not_run"},
                "visual": {"screenshot_comparison": "not_run"},
            },
        })

        self.assertEqual(summary["inventory"]["object_count"], 10)
        self.assertEqual(summary["recovery"]["pending_validation"], 4)
        self.assertEqual(summary["validation"]["semantic_execution"], "not_run")
        self.assertEqual(summary["visual"]["target_risk_count"], 1)
        self.assertEqual(summary["roundtrip"]["static_status"], "static_fail")

    def test_missing_contracts_are_safe_and_runtime_stays_not_run(self):
        summary = build_evidence_summary({})

        self.assertEqual(summary["status"], "UNVERIFIED")
        self.assertFalse(summary["validation"]["release_ready"])
        self.assertEqual(summary["roundtrip"]["desktop_status"], "not_run")
        self.assertEqual(summary["roundtrip"]["screenshot_comparison"], "not_run")


if __name__ == "__main__":
    unittest.main()
