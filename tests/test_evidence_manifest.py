import hashlib
import json
import os
import tempfile
import unittest

from powerbi_import.evidence_manifest import (
    MANIFEST_VERSION,
    build_evidence_manifest,
    save_evidence_manifest,
)


class TestEvidenceManifest(unittest.TestCase):
    def test_manifest_records_source_hash_and_not_run_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "sales.twb")
            with open(source, "wb") as handle:
                handle.write(b"source")
            manifest = build_evidence_manifest(source, target_path=tmp)
        self.assertEqual(manifest["manifest_version"], MANIFEST_VERSION)
        self.assertEqual(manifest["source"]["sha256"], hashlib.sha256(b"source").hexdigest())
        self.assertEqual(manifest["environment"]["deployment"], "not_run")

    def test_save_manifest_is_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = save_evidence_manifest(build_evidence_manifest(), os.path.join(tmp, "manifest.json"))
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
        self.assertEqual(payload["manifest_version"], MANIFEST_VERSION)


if __name__ == "__main__":
    unittest.main()
