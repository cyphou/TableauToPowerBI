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


class TestParentAttribution(unittest.TestCase):
    """Extractors name the owner per type; reading only the generic names
    reported every action and filter in the corpus as an orphan."""

    def test_action_source_worksheets_counts_as_a_parent(self):
        inventory = build_source_inventory({
            "actions": [{"name": "Region Filter", "source_worksheets": ["Revenue by Region"]}]
        })

        self.assertEqual(inventory["orphan_count"], 0)
        self.assertEqual(inventory["objects"][0]["parent"], "Revenue by Region")

    def test_action_without_any_source_stays_an_orphan(self):
        inventory = build_source_inventory({
            "actions": [{"name": "Company Website", "source_worksheets": []}]
        })

        self.assertEqual(inventory["orphan_count"], 1)

    def test_filter_worksheet_counts_as_a_parent(self):
        inventory = build_source_inventory({
            "filters": [{"field": "Region", "worksheet": "Sales Overview"}]
        })

        self.assertEqual(inventory["orphan_count"], 0)
        self.assertEqual(inventory["objects"][0]["parent"], "Sales Overview")

    def test_empty_list_is_not_mistaken_for_a_parent(self):
        inventory = build_source_inventory({
            "actions": [{"name": "A", "source_worksheets": [], "target_worksheets": [""]}]
        })

        self.assertIsNone(inventory["objects"][0]["parent"])

    def test_first_named_owner_wins_for_multi_source_actions(self):
        inventory = build_source_inventory({
            "actions": [{"name": "A", "source_worksheets": ["", "Sheet B"]}]
        })

        self.assertEqual(inventory["objects"][0]["parent"], "Sheet B")


if __name__ == "__main__":
    unittest.main()
