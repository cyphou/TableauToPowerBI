"""The names a consumer reads must be names the extractor writes.

Every defect found while hardening the quality surface was the same shape: a
producer emitted one key and a consumer looked for another, silently. The
extractor emits `source_worksheets` for actions while the inventory searched for
`worksheet`, so 216 perfectly attributed objects were reported as orphans.

These tests derive the real vocabulary by extracting genuine workbooks, then
assert the constants consumers depend on intersect it. They fail when either
side is renamed without the other.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))

from extract_tableau_data import TableauExtractor
from powerbi_import.source_inventory import (
    ORPHANABLE_TYPES,
    _NAME_KEYS,
    _PARENT_KEYS,
    build_source_inventory,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Genuine Tableau files, not hand-written fixtures: the hand-written ones do not
# always reproduce Tableau's own schema.
WORKBOOKS = (
    os.path.join(ROOT, 'examples', 'real_world', 'superstore_sales_dashboard.twbx'),
    os.path.join(ROOT, 'examples', 'real_world', 'World Indicators.twbx'),
)


def _vocabulary():
    """Map each extracted object type to the field names actually emitted."""
    vocab = {}
    payloads = {}
    for workbook in WORKBOOKS:
        if not os.path.isfile(workbook):
            continue
        out = tempfile.mkdtemp()
        try:
            TableauExtractor(workbook, output_dir=out).extract_all()
            for filename in os.listdir(out):
                if not filename.endswith('.json'):
                    continue
                kind = filename[:-len('.json')]
                try:
                    with open(os.path.join(out, filename), encoding='utf-8') as fh:
                        data = json.load(fh)
                except (OSError, json.JSONDecodeError):
                    continue
                items = data if isinstance(data, list) else [data]
                records = [i for i in items if isinstance(i, dict)]
                if not records:
                    continue
                payloads.setdefault(kind, []).extend(records)
                keys = vocab.setdefault(kind, set())
                for record in records:
                    keys.update(record.keys())
        finally:
            shutil.rmtree(out, ignore_errors=True)
    return vocab, payloads


class _ContractBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vocab, cls.payloads = _vocabulary()
        if not cls.vocab:
            raise unittest.SkipTest('no sample workbooks available')


class TestInventoryReadsWhatTheExtractorWrites(_ContractBase):
    def test_orphanable_types_expose_a_parent_key(self):
        """An object the inventory can call an orphan must be attributable."""
        for object_type in sorted(ORPHANABLE_TYPES):
            keys = self.vocab.get(object_type)
            if not keys:
                continue  # the corpus produced none of this type
            with self.subTest(object_type=object_type):
                self.assertTrue(
                    set(_PARENT_KEYS) & keys,
                    f"{object_type} is scored for orphanhood but emits none of "
                    f"{_PARENT_KEYS}; it emits {sorted(keys)}")

    def test_extracted_objects_expose_a_name_key(self):
        for object_type, keys in sorted(self.vocab.items()):
            if object_type in ('linguistic_schema', 'aliases'):
                continue  # keyed by field name, not records with a name
            with self.subTest(object_type=object_type):
                self.assertTrue(
                    set(_NAME_KEYS) & keys,
                    f"{object_type} emits no name-like key; it emits {sorted(keys)}")

    def test_actions_are_attributed_on_a_real_workbook(self):
        """The regression itself: actions carry source_worksheets, not worksheet."""
        actions = self.payloads.get('actions') or []
        if not actions:
            self.skipTest('corpus produced no actions')
        inventory = build_source_inventory({'actions': actions})
        attributed = [r for r in inventory['objects'] if not r['orphan']]
        self.assertTrue(
            attributed,
            'every action on a real workbook was reported as an orphan')


class TestInterfaceDiffReadsWhatTheExtractorWrites(_ContractBase):
    """Filter coverage is computed from worksheet-level filters.

    Not from the flat `filters.json` index: that one aggregates every `<filter>`
    in the document, including datasource-level nodes, and carries a different
    shape. Reading the wrong one is how a coverage gap gets misdiagnosed.
    """

    FILTER_FIELDS = ('field', 'type', 'values', 'min', 'max', 'datasource')

    def _worksheet_filter_keys(self):
        keys = set()
        for worksheet in self.payloads.get('worksheets') or []:
            for f in worksheet.get('filters') or []:
                keys.update(f.keys())
        return keys

    def test_worksheet_filters_expose_the_fields_coverage_depends_on(self):
        keys = self._worksheet_filter_keys()
        if not keys:
            self.skipTest('corpus produced no worksheet filters')
        for field in self.FILTER_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, keys)

    def test_flat_filter_index_carries_its_owning_worksheet(self):
        keys = self.vocab.get('filters')
        if not keys:
            self.skipTest('corpus produced no filters')
        self.assertIn('worksheet', keys)


if __name__ == '__main__':
    unittest.main()
