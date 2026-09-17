"""Tests for the canonical extracted-object inventory."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from powerbi_import.source_inventory import OBJECT_TYPES, build_source_inventory


class TestSourceInventory(unittest.TestCase):
    def test_inventory_covers_all_object_families(self):
        extracted = {object_type: [{"name": object_type}] for object_type in OBJECT_TYPES}

        inventory = build_source_inventory(extracted)

        self.assertEqual(inventory["object_count"], len(OBJECT_TYPES))
        self.assertEqual(inventory["counts"], {object_type: 1 for object_type in OBJECT_TYPES})
        self.assertEqual({row["object_type"] for row in inventory["objects"]}, set(OBJECT_TYPES))
        self.assertTrue(all(row["disposition"] == "extracted" for row in inventory["objects"]))

    def test_ids_are_stable_and_duplicates_are_reported(self):
        extracted = {
            "worksheets": [
                {"name": "Sales", "source_location": "dashboard[0]/worksheet[0]"},
                {"name": "Sales", "source_location": "dashboard[1]/worksheet[0]"},
            ]
        }

        first = build_source_inventory(extracted)
        second = build_source_inventory(extracted)

        self.assertEqual(first, second)
        self.assertEqual(len(first["duplicate_names"]), 1)
        self.assertEqual(first["duplicate_names"][0]["name"], "Sales")
        self.assertEqual(first["orphan_count"], 0)

    def test_relationship_without_parent_is_an_orphan(self):
        inventory = build_source_inventory({"relationships": [{"name": "Orders-Customers"}]})

        self.assertEqual(inventory["orphan_count"], 1)
        self.assertTrue(inventory["objects"][0]["orphan"])


if __name__ == "__main__":
    unittest.main()
