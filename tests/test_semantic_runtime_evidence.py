"""Tests for redacted semantic executor evidence."""

import unittest

from powerbi_import.semantic_runtime import describe_executor, validate_semantic_execution


class _Executor:
    provider = "Fabric"
    version = "2026.09"
    endpoint = "https://fabric.example.test"
    metadata = {
        "capabilities": ["dax", "rows"],
        "token": "do-not-export",
    }

    def execute(self, query):
        return {"rows": [{"Value": 1}]}


class TestSemanticRuntimeEvidence(unittest.TestCase):
    def test_missing_executor_is_explicitly_not_run(self):
        evidence = validate_semantic_execution([], None)

        self.assertEqual(evidence["status"], "not_run")
        self.assertEqual(evidence["executor"]["status"], "not_run")

    def test_executor_metadata_is_redacted_and_attached(self):
        executor = _Executor()

        descriptor = describe_executor(executor)
        evidence = validate_semantic_execution(
            [{"name": "smoke", "dax": "EVALUATE ROW(\"Value\", 1)"}], executor
        )

        self.assertEqual(descriptor["provider"], "Fabric")
        self.assertEqual(descriptor["capabilities"], ["dax", "rows"])
        self.assertEqual(descriptor["endpoint_present"], True)
        self.assertEqual(evidence["executor"]["version"], "2026.09")
        self.assertNotIn("do-not-export", str(evidence))
        self.assertEqual(evidence["status"], "passed")


if __name__ == "__main__":
    unittest.main()
