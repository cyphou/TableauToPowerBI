"""Tests for the round-trip validation evidence boundary."""

import unittest

from powerbi_import.roundtrip_validation import build_roundtrip_validation


class TestRoundtripValidation(unittest.TestCase):
    def test_static_pass_keeps_desktop_and_screenshot_states_not_run(self):
        result = build_roundtrip_validation(
            {"openable": True, "blocking_issues": []},
            {"counts": {"valid": 3, "empty": 0, "orphaned": 0}},
            {"runtime": "not_run"},
        )

        self.assertEqual(result["static"]["status"], "static_pass")
        self.assertEqual(result["desktop"]["status"], "not_run")
        self.assertEqual(result["visual"]["screenshot_comparison"], "not_run")
        self.assertEqual(result["post_repair_revalidation"], "not_required")

    def test_visual_risk_fails_static_roundtrip_and_requires_revalidation(self):
        result = build_roundtrip_validation(
            {"openable": True, "blocking_issues": []},
            {"counts": {"valid": 1, "empty": 1, "orphaned": 1}},
            {"runtime": "not_run"},
        )

        self.assertEqual(result["static"]["status"], "static_fail")
        self.assertEqual(result["static"]["visual_risk_count"], 2)
        self.assertEqual(result["post_repair_revalidation"], "required")

    def test_openability_failure_is_preserved(self):
        result = build_roundtrip_validation(
            {"openable": False, "blocking_issues": ["missing model"]},
            {"counts": {}},
            {"runtime": "not_run"},
        )

        self.assertEqual(result["static"]["status"], "static_fail")
        self.assertEqual(result["static"]["blocking_issue_count"], 1)


if __name__ == "__main__":
    unittest.main()
