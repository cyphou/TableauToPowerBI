"""Tests for the portable migration evidence package."""

import json
import os
import tempfile
import unittest
import zipfile

from powerbi_import.evidence_package import build_evidence_package, write_evidence_package


class _Report:
    def to_dict(self):
        return {
            "report_name": "Demo",
            "status": "WARN",
            "handoff_status": "WARN",
            "blockers": [],
            "warnings": ["review approximation"],
            "visual_mappings": {"summary": {"status_counts": {"approximation": 1}}},
            "m_emitters": {"summary": {"aliases": 98}},
            "fabric_evidence": {"status": "not_present"},
            "openability_confidence": {"semantic_execution": "not_run"},
        }


class TestEvidencePackage(unittest.TestCase):
    def test_payload_keeps_static_runtime_boundary(self):
        payload = build_evidence_package(_Report())
        self.assertEqual(payload["status"], "WARN")
        self.assertEqual(payload["runtime_boundary"]["semantic_execution"], "not_run")
        self.assertEqual(payload["m_emitters"]["summary"]["aliases"], 98)

    def test_zip_contains_json_and_readme(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence_package(_Report(), os.path.join(tmp, "evidence.zip"))
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(set(archive.namelist()), {"evidence_package.json", "README.txt"})
                payload = json.loads(archive.read("evidence_package.json"))
        self.assertEqual(payload["report_name"], "Demo")


if __name__ == "__main__":
    unittest.main()
