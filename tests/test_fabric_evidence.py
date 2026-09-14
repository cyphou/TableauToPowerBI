"""Tests for normalized local Fabric evidence."""

import json
import os
import tempfile
import unittest

from powerbi_import.fabric_evidence import build_fabric_evidence


class TestFabricEvidence(unittest.TestCase):
    def test_non_fabric_project_is_not_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = build_fabric_evidence(tmp, "Demo")
        self.assertEqual(evidence["status"], "not_present")
        self.assertEqual(evidence["runtime"]["deployment"], "not_run")

    def test_invalid_bundle_exposes_artifact_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "Demo.Lakehouse"))
            evidence = build_fabric_evidence(tmp, "Demo")
        self.assertEqual(evidence["status"], "invalid")
        self.assertEqual(evidence["artifacts"]["Lakehouse"]["status"], "present")
        self.assertEqual(evidence["artifacts"]["Dataflow"]["status"], "missing")
        self.assertEqual(evidence["runtime"]["refresh"], "not_run")


if __name__ == "__main__":
    unittest.main()
