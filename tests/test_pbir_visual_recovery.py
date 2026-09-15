"""Tests for generated PBIR visual recovery evidence."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from powerbi_import.pbir_visual_recovery import scan_pbir_visual_recovery


class TestPbirVisualRecovery(unittest.TestCase):
    def test_classifies_valid_empty_and_orphaned_visuals(self):
        with tempfile.TemporaryDirectory() as root:
            visual_root = os.path.join(root, "definition", "pages", "page1", "visuals")
            for visual_id in ("valid", "empty", "orphan"):
                os.makedirs(os.path.join(visual_root, visual_id))
            payloads = {
                "valid": {"visual": {"visualType": "table", "query": {"Column": "Sales"}}},
                "empty": {"visual": {}},
                "orphan": {"visual": {"visualType": "table", "query": {}}},
            }
            for visual_id, payload in payloads.items():
                with open(os.path.join(visual_root, visual_id, "visual.json"), "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)

            evidence = scan_pbir_visual_recovery(root)

        self.assertEqual(evidence["counts"], {"valid": 1, "empty": 1, "orphaned": 1, "unvalidated": 0})
        self.assertEqual(evidence["visual_count"], 3)
        states = {row["visual_id"]: row["state"] for row in evidence["visuals"]}
        self.assertEqual(states, {"empty": "empty", "orphan": "orphaned", "valid": "valid"})

    def test_missing_project_is_not_available(self):
        evidence = scan_pbir_visual_recovery("missing-project")
        self.assertEqual(evidence["status"], "not_available")
        self.assertEqual(evidence["visual_count"], 0)


if __name__ == "__main__":
    unittest.main()
