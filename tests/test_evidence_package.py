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

    def test_zip_includes_related_quality_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            quality_json = os.path.join(tmp, "migration_quality_Demo.json")
            quality_html = os.path.join(tmp, "migration_quality_Demo.html")
            openability_json = os.path.join(tmp, "openability_report.json")
            for file_path, contents in {
                quality_json: '{"kind": "quality"}',
                quality_html: '<html>demo</html>',
                openability_json: '{"openable": true}',
            }.items():
                with open(file_path, "w", encoding="utf-8") as handle:
                    handle.write(contents)

            path = write_evidence_package(
                _Report(),
                os.path.join(tmp, "evidence.zip"),
                extra_files=[quality_json, quality_html, openability_json],
            )

            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                self.assertIn("evidence_package.json", names)
                self.assertIn("README.txt", names)
                self.assertIn("quality/migration_quality_Demo.json", names)
                self.assertIn("quality/migration_quality_Demo.html", names)
                self.assertIn("quality/openability_report.json", names)

    def test_payload_contains_operator_summary_counts(self):
        payload = build_evidence_package(_Report())
        self.assertIn("summary", payload)
        self.assertEqual(payload["summary"]["status"], "WARN")
        self.assertEqual(payload["summary"]["blocker_count"], 0)
        self.assertEqual(payload["summary"]["warning_count"], 1)
        self.assertEqual(payload["summary"]["priority_count"], 0)


if __name__ == "__main__":
    unittest.main()
