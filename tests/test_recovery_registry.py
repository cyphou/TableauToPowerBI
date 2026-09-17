"""Tests for object-level recovery dispositions."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from powerbi_import.recovery_registry import build_recovery_registry
from powerbi_import.source_inventory import build_source_inventory


class TestRecoveryRegistry(unittest.TestCase):
    def test_every_object_gets_conservative_disposition_and_owner(self):
        inventory = build_source_inventory({
            "worksheets": [{"name": "Sales"}],
            "calculations": [{"name": "Revenue"}],
        })

        registry = build_recovery_registry(inventory)

        self.assertEqual(registry["status"], "pending")
        self.assertEqual(registry["counts"]["pending_validation"], 2)
        self.assertEqual({row["owner"] for row in registry["objects"]}, {"@visual", "@dax"})
        self.assertTrue(all(row["remediation"] for row in registry["objects"]))

    def test_explicit_dispositions_complete_registry(self):
        inventory = build_source_inventory({"worksheets": [{"name": "Sales"}]})
        stable_id = inventory["objects"][0]["stable_id"]

        registry = build_recovery_registry(inventory, {stable_id: "generated"})

        self.assertEqual(registry["status"], "complete")
        self.assertEqual(registry["counts"]["generated"], 1)

    def test_invalid_disposition_and_unknown_id_fail_closed(self):
        inventory = build_source_inventory({"worksheets": [{"name": "Sales"}]})
        stable_id = inventory["objects"][0]["stable_id"]

        with self.assertRaises(ValueError):
            build_recovery_registry(inventory, {stable_id: "guessed"})
        with self.assertRaises(ValueError):
            build_recovery_registry(inventory, {"src-missing": "generated"})


if __name__ == "__main__":
    unittest.main()
