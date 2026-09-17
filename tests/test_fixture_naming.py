"""Fixtures must stay addressable without naming a workbook.

Report and workbook names are only permitted under ``examples/``. Fixture
directories and snapshots previously carried those names, which put them in the
tracked tree under ``tests/``.
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tests.fixture_corpus import (
    BASELINES_DIR,
    GOLDEN_DIR,
    MANIFEST,
    SAMPLES_DIR,
    all_workbooks,
    describe,
    fixture_id,
    golden_path,
    golden_workbooks,
)

_ID_RE = re.compile(r'^wb_[0-9a-f]{8}$')


def _sample_stems():
    return {os.path.splitext(f)[0] for f in os.listdir(SAMPLES_DIR)
            if f.endswith(('.twb', '.twbx'))}


class TestFixtureNaming(unittest.TestCase):
    def test_golden_directories_are_ids(self):
        for entry in os.listdir(GOLDEN_DIR):
            if os.path.isfile(os.path.join(GOLDEN_DIR, entry)):
                continue
            with self.subTest(entry=entry):
                self.assertRegex(entry, _ID_RE)

    def test_baseline_files_are_ids(self):
        for entry in os.listdir(BASELINES_DIR):
            if not entry.endswith('.snapshot.json'):
                continue
            with self.subTest(entry=entry):
                self.assertRegex(entry[:-len('.snapshot.json')], _ID_RE)

    def test_no_fixture_is_named_after_a_workbook(self):
        stems = _sample_stems()
        for root in (GOLDEN_DIR, BASELINES_DIR):
            for entry in os.listdir(root):
                stem = entry.split('.')[0]
                with self.subTest(entry=entry):
                    self.assertNotIn(stem, stems)


class TestFixtureIds(unittest.TestCase):
    def test_ids_are_unique(self):
        ids = [fid for fid, _ in all_workbooks()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_id_is_stable_across_path_and_stem(self):
        for fid, path in all_workbooks():
            with self.subTest(path=path):
                self.assertEqual(fixture_id(os.path.basename(path)), fid)
                self.assertEqual(fixture_id(path), fid)

    def test_describe_resolves_back_to_a_filename(self):
        for fid, path in all_workbooks():
            with self.subTest(fid=fid):
                self.assertEqual(describe(fid), os.path.basename(path))


class TestCuratedCorpus(unittest.TestCase):
    def test_manifest_lists_existing_workbooks(self):
        for fid, path in golden_workbooks():
            with self.subTest(fid=fid):
                self.assertTrue(os.path.isfile(path), path)

    def test_every_curated_workbook_has_a_fixture(self):
        for fid, _path in golden_workbooks():
            with self.subTest(fid=fid):
                self.assertTrue(os.path.isfile(golden_path(fid)))

    def test_excluded_workbooks_carry_a_reason(self):
        with open(MANIFEST, encoding='utf-8') as fh:
            manifest = json.load(fh)
        for name, reason in manifest.get('golden_excluded', {}).items():
            with self.subTest(workbook=name):
                self.assertTrue(reason.strip())

    def test_curated_and_excluded_do_not_overlap(self):
        with open(MANIFEST, encoding='utf-8') as fh:
            manifest = json.load(fh)
        overlap = set(manifest['golden']) & set(manifest.get('golden_excluded', {}))
        self.assertEqual(overlap, set())


if __name__ == '__main__':
    unittest.main()
