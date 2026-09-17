import unittest

from powerbi_import.migration_ledger import build_migration_ledger


class TestMigrationLedger(unittest.TestCase):
    def test_normalizes_recovery_dispositions(self):
        inventory = {
            "objects": [
                {"stable_id": "b", "object_type": "calculations", "name": "Calc B"},
                {"stable_id": "a", "object_type": "worksheets", "name": "Sheet A"},
            ]
        }
        recovery = {
            "objects": [
                {"stable_id": "a", "disposition": "generated", "owner": "@visual"},
                {"stable_id": "b", "disposition": "approximated", "owner": "@dax"},
            ]
        }

        ledger = build_migration_ledger(inventory, recovery)

        self.assertEqual([row["stable_id"] for row in ledger["objects"]], ["b", "a"])
        self.assertEqual(ledger["objects"][0]["target_disposition"], "approximated")
        self.assertEqual(ledger["objects"][1]["target_disposition"], "exact")
        self.assertEqual(ledger["target_dispositions"]["exact"], 1)

    def test_pending_and_blocked_are_validation_states(self):
        inventory = {"objects": [
            {"stable_id": "pending", "object_type": "worksheets", "name": "Pending"},
            {"stable_id": "blocked", "object_type": "worksheets", "name": "Blocked"},
        ]}
        recovery = {"objects": [
            {"stable_id": "pending", "disposition": "pending_validation"},
            {"stable_id": "blocked", "disposition": "blocked"},
        ]}

        ledger = build_migration_ledger(inventory, recovery)

        self.assertEqual(ledger["status"], "pending")
        self.assertEqual(ledger["validation"], {"pending": 1, "validated": 0, "blocked": 1})
        self.assertEqual(ledger["unresolved_count"], 1)
        self.assertIsNone(ledger["objects"][1]["target_disposition"])


if __name__ == "__main__":
    unittest.main()