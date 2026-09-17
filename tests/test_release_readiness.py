"""Tests for the static release-readiness scorecard."""

import unittest

from powerbi_import.release_readiness import build_release_readiness


class TestReleaseReadiness(unittest.TestCase):
    def test_static_release_requires_clean_corpus_and_keeps_runtime_unresolved(self):
        scorecard = build_release_readiness(
            {
                "release_claim": "static_corpus_only",
                "corpus_count": 2,
                "counts": {"certified_static": 2, "needs_review": 0, "blocked": 0},
            },
            compatibility={"tableau": ">=2023.1", "power_bi": "PBIR v4.0"},
            known_limitations=["Desktop rendering not verified"],
        )

        self.assertEqual(scorecard["status"], "ready_for_static_release")
        self.assertEqual(scorecard["gates"]["runtime_desktop"]["status"], "not_run")
        self.assertEqual(scorecard["compatibility"]["power_bi"], "PBIR v4.0")
        self.assertEqual(scorecard["known_limitations"], ["Desktop rendering not verified"])

    def test_review_or_blocked_corpus_is_not_release_ready(self):
        scorecard = build_release_readiness({
            "release_claim": "static_corpus_only",
            "corpus_count": 2,
            "counts": {"certified_static": 0, "needs_review": 1, "blocked": 1},
        })

        self.assertEqual(scorecard["status"], "needs_review")
        self.assertEqual(scorecard["gates"]["no_blocked_workbooks"]["status"], "failed")
        self.assertEqual(scorecard["gates"]["no_review_workbooks"]["status"], "warning")

    def test_empty_or_wrong_claim_fails_static_gate(self):
        scorecard = build_release_readiness({
            "release_claim": "production",
            "corpus_count": 0,
            "counts": {},
        })

        self.assertEqual(scorecard["status"], "needs_review")
        self.assertEqual(scorecard["gates"]["corpus_present"]["status"], "failed")
        self.assertEqual(scorecard["gates"]["static_only_claim"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
