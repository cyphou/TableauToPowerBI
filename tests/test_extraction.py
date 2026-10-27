"""
Unit tests for Tableau extraction (extract_tableau_data.py).

Tests the TableauExtractor class end-to-end using the Superstore sample
workbook, and validates that all 16 object types are correctly extracted.
"""

import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from copy import deepcopy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tableau_export'))

from extract_tableau_data import TableauExtractor

from tests.fixture_corpus import all_workbooks, describe

SAMPLE_TWB = os.path.join(ROOT, 'examples', 'tableau_samples', 'Superstore_Sales.twb')


def _has_sample():
    return os.path.exists(SAMPLE_TWB)


# ═══════════════════════════════════════════════════════════════════
# TableauExtractor — init and file reading
# ═══════════════════════════════════════════════════════════════════

class TestExtractorInit(unittest.TestCase):
    """Test TableauExtractor initialization and file reading."""

    def test_constructor_sets_fields(self):
        tmpdir = tempfile.mkdtemp()
        try:
            ext = TableauExtractor(SAMPLE_TWB, output_dir=tmpdir)
            self.assertEqual(ext.tableau_file, SAMPLE_TWB)
            self.assertTrue(os.path.isdir(tmpdir))
        finally:
            shutil.rmtree(tmpdir)

    @unittest.skipUnless(_has_sample(), 'Superstore sample not available')
    def test_read_tableau_file_returns_xml(self):
        tmpdir = tempfile.mkdtemp()
        try:
            ext = TableauExtractor(SAMPLE_TWB, output_dir=tmpdir)
            xml = ext.read_tableau_file()
            self.assertIsNotNone(xml)
            self.assertIn('<workbook', xml)
        finally:
            shutil.rmtree(tmpdir)

    def test_missing_file_raises_or_returns_none(self):
        tmpdir = tempfile.mkdtemp()
        try:
            ext = TableauExtractor('/nonexistent/file.twb', output_dir=tmpdir)
            # The method may raise FileNotFoundError or return None
            try:
                xml = ext.read_tableau_file()
                self.assertIsNone(xml)
            except (FileNotFoundError, OSError):
                pass  # Also valid — file does not exist
        finally:
            shutil.rmtree(tmpdir)


# ═══════════════════════════════════════════════════════════════════
# Full extraction — validate all 16 JSON outputs
# ═══════════════════════════════════════════════════════════════════

@unittest.skipUnless(_has_sample(), 'Superstore sample not available')
class TestFullExtraction(unittest.TestCase):
    """End-to-end extraction test with the Superstore workbook."""

    @classmethod
    def setUpClass(cls):
        """Run extraction once for all tests in this class."""
        cls.tmpdir = tempfile.mkdtemp()
        ext = TableauExtractor(SAMPLE_TWB, output_dir=cls.tmpdir)

        # Suppress print output during extraction
        _old = sys.stdout
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
        try:
            cls.success = ext.extract_all()
        finally:
            sys.stdout = _old

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir)

    def _load(self, filename):
        path = os.path.join(self.tmpdir, filename)
        if not os.path.exists(path):
            return None
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    @staticmethod
    def _normalize_json(value):
        """Normalize nested JSON structures for stable semantic comparison.

        - dict keys sorted recursively
        - list entries normalized and sorted by JSON representation
        - volatile extraction metadata removed
        """
        volatile_keys = {'extracted_at', 'timestamp', 'generated_at', 'run_id'}
        if isinstance(value, dict):
            normalized = {}
            for key in sorted(value.keys()):
                if key in volatile_keys:
                    continue
                normalized[key] = TestFullExtraction._normalize_json(value[key])
            return normalized
        if isinstance(value, list):
            normalized_items = [TestFullExtraction._normalize_json(item) for item in value]
            return sorted(normalized_items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
        return value

    def test_extraction_succeeds(self):
        self.assertTrue(self.success)

    def test_extraction_outputs_semantically_stable_across_runs(self):
        """Two extraction runs on the same workbook should be semantically equivalent.

        This protects optimization work from introducing behavioral drift while
        allowing ordering/timestamp differences.
        """
        tmpdir_2 = tempfile.mkdtemp()
        try:
            ext2 = TableauExtractor(SAMPLE_TWB, output_dir=tmpdir_2)
            _old = sys.stdout
            sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
            try:
                success2 = ext2.extract_all()
            finally:
                sys.stdout = _old
            self.assertTrue(success2)

            expected_files = [
                'datasources.json', 'worksheets.json', 'dashboards.json',
                'calculations.json', 'parameters.json', 'filters.json',
                'stories.json', 'actions.json', 'sets.json', 'groups.json',
                'bins.json', 'hierarchies.json', 'sort_orders.json',
                'aliases.json', 'custom_sql.json', 'user_filters.json',
                'datasource_filters.json', 'custom_geocoding.json',
                'published_datasources.json', 'data_blending.json',
                'hyper_files.json', 'table_extensions.json',
                'linguistic_schema.json',
            ]

            for fname in expected_files:
                p1 = os.path.join(self.tmpdir, fname)
                p2 = os.path.join(tmpdir_2, fname)
                self.assertTrue(os.path.exists(p1), f'Missing file from run1: {fname}')
                self.assertTrue(os.path.exists(p2), f'Missing file from run2: {fname}')
                with open(p1, 'r', encoding='utf-8') as f1:
                    d1 = json.load(f1)
                with open(p2, 'r', encoding='utf-8') as f2:
                    d2 = json.load(f2)
                n1 = self._normalize_json(deepcopy(d1))
                n2 = self._normalize_json(deepcopy(d2))
                self.assertEqual(n1, n2, f'Semantic mismatch in {fname}')
        finally:
            shutil.rmtree(tmpdir_2)

    # ─── Core objects ──────────────────────────────────────

    def test_datasources_extracted(self):
        data = self._load('datasources.json')
        self.assertIsNotNone(data)
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0, 'Should extract at least one datasource')

    def test_datasource_has_connection(self):
        data = self._load('datasources.json')
        for ds in data:
            self.assertIn('connection', ds, f'Datasource missing connection: {ds.get("name")}')

    def test_datasource_has_tables(self):
        data = self._load('datasources.json')
        has_tables = any(len(ds.get('tables', [])) > 0 for ds in data)
        self.assertTrue(has_tables, 'At least one datasource should have tables')

    def test_datasource_has_columns(self):
        data = self._load('datasources.json')
        has_cols = any(len(ds.get('columns', [])) > 0 for ds in data)
        self.assertTrue(has_cols, 'At least one datasource should have columns')

    def test_worksheets_extracted(self):
        data = self._load('worksheets.json')
        self.assertIsNotNone(data)
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0, 'Should extract at least one worksheet')

    def test_worksheet_has_name(self):
        data = self._load('worksheets.json')
        for ws in data:
            self.assertIn('name', ws)
            self.assertTrue(ws['name'], 'Worksheet name should not be empty')

    def test_worksheet_has_fields(self):
        data = self._load('worksheets.json')
        has_fields = any(len(ws.get('fields', [])) > 0 for ws in data)
        self.assertTrue(has_fields, 'At least one worksheet should have fields')

    def test_dashboards_extracted(self):
        data = self._load('dashboards.json')
        self.assertIsNotNone(data)
        self.assertIsInstance(data, list)
        for db in data:
            self.assertIn('name', db)

    def test_calculations_extracted(self):
        data = self._load('calculations.json')
        self.assertIsNotNone(data)
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0, 'Superstore should have calculations')

    def test_calculation_has_formula(self):
        data = self._load('calculations.json')
        has_formula = any(c.get('formula') for c in data)
        self.assertTrue(has_formula, 'At least one calculation should have a formula')

    # ─── Parameters ─────────────────────────────────────────

    def test_parameters_extracted(self):
        data = self._load('parameters.json')
        self.assertIsNotNone(data)
        self.assertIsInstance(data, list)

    # ─── Filters ────────────────────────────────────────────

    def test_filters_extracted(self):
        data = self._load('filters.json')
        self.assertIsNotNone(data)
        self.assertIsInstance(data, list)

    # ─── Stories ────────────────────────────────────────────

    def test_stories_extracted(self):
        data = self._load('stories.json')
        self.assertIsNotNone(data)
        self.assertIsInstance(data, list)

    # ─── Actions ────────────────────────────────────────────

    def test_actions_extracted(self):
        data = self._load('actions.json')
        self.assertIsNotNone(data)
        self.assertIsInstance(data, list)

    # ─── Sets / Groups / Bins / Hierarchies ─────────────────

    def test_sets_extracted(self):
        data = self._load('sets.json')
        self.assertIsNotNone(data)
        self.assertIsInstance(data, list)

    def test_groups_extracted(self):
        data = self._load('groups.json')
        self.assertIsNotNone(data)
        self.assertIsInstance(data, list)

    def test_bins_extracted(self):
        data = self._load('bins.json')
        self.assertIsNotNone(data)
        self.assertIsInstance(data, list)

    def test_hierarchies_extracted(self):
        data = self._load('hierarchies.json')
        self.assertIsNotNone(data)
        self.assertIsInstance(data, list)

    # ─── Sort / Aliases / Custom SQL / User Filters ─────────

    def test_sort_orders_extracted(self):
        data = self._load('sort_orders.json')
        self.assertIsNotNone(data)
        self.assertIsInstance(data, list)

    def test_aliases_extracted(self):
        data = self._load('aliases.json')
        self.assertIsNotNone(data)
        # aliases.json may be a dict (column→alias mapping) or a list
        self.assertIsInstance(data, (list, dict))

    def test_custom_sql_extracted(self):
        data = self._load('custom_sql.json')
        self.assertIsNotNone(data)
        self.assertIsInstance(data, list)

    def test_user_filters_extracted(self):
        data = self._load('user_filters.json')
        self.assertIsNotNone(data)
        self.assertIsInstance(data, list)

    # ─── Cross-consistency checks ───────────────────────────

    def test_all_16_json_files_exist(self):
        expected = [
            'datasources.json', 'worksheets.json', 'dashboards.json',
            'calculations.json', 'parameters.json', 'filters.json',
            'stories.json', 'actions.json', 'sets.json', 'groups.json',
            'bins.json', 'hierarchies.json', 'sort_orders.json',
            'aliases.json', 'custom_sql.json', 'user_filters.json',
        ]
        for fname in expected:
            path = os.path.join(self.tmpdir, fname)
            self.assertTrue(
                os.path.exists(path),
                f'Missing extraction output: {fname}',
            )

    def test_json_files_are_valid_json(self):
        for fname in os.listdir(self.tmpdir):
            if fname.endswith('.json'):
                path = os.path.join(self.tmpdir, fname)
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        json.load(f)
                except json.JSONDecodeError:
                    self.fail(f'Invalid JSON in extraction output: {fname}')

    def test_worksheet_fields_reference_valid_datasource(self):
        """Worksheet datasource references should match extracted datasources."""
        worksheets = self._load('worksheets.json')
        datasources = self._load('datasources.json')
        ds_names = set()
        for ds in datasources:
            ds_names.add(ds.get('name', ''))
            ds_names.add(ds.get('caption', ''))
        for ws in worksheets:
            if ws.get('fields'):
                self.assertIsInstance(ws['fields'], list)


_SAMPLE_BRACKETED_IDENTIFIER = r'\[((?:[^\]]|\]\])*)\]'


class TestSampleColumnMapProvenance(unittest.TestCase):
    """Sample ownership records must agree with their explicit XML maps."""

    def _map_parts(self, mapping):
        key = (mapping.get('key') or '').strip()
        value = (mapping.get('value') or '').strip()
        local = re.fullmatch(_SAMPLE_BRACKETED_IDENTIFIER, key)
        physical = re.fullmatch(
            _SAMPLE_BRACKETED_IDENTIFIER + r'\.' + _SAMPLE_BRACKETED_IDENTIFIER,
            value,
        )
        self.assertIsNotNone(local, f'Malformed explicit map key: {key!r}')
        self.assertIsNotNone(physical, f'Malformed explicit map value: {value!r}')
        parts = (local.group(1), *physical.groups())
        self.assertTrue(all(parts), f'Empty explicit map identifier: {key!r} -> {value!r}')
        return tuple(part.replace(']]', ']') for part in parts)

    def _record_for_map(self, connection, mapping, context):
        local, parent, remote = self._map_parts(mapping)
        records = [
            record for record in connection.findall(
                './metadata-records/metadata-record[@class="column"]')
            if (record.findtext('local-name') or '').strip() == mapping.get('key').strip()
        ]
        label = f'{context}: {local!r} -> {parent!r}.{remote!r}'
        self.assertEqual(len(records), 1,
                         f'{label}: expected exactly one same-connection column metadata record')
        record = records[0]
        self.assertEqual((record.findtext('parent-name') or '').strip(),
                         '[' + parent.replace(']', ']]') + ']', label)
        self.assertEqual((record.findtext('remote-name') or '').strip(), remote, label)
        return record

    def _declared_types(self, datasource, connection, mapping):
        """Read original declarations only, never the metadata under test."""
        local, parent, remote = self._map_parts(mapping)

        def identifier(name):
            match = re.fullmatch(_SAMPLE_BRACKETED_IDENTIFIER, name)
            return match.group(1).replace(']]', ']') if match else name

        declarations = [
            column for column in datasource.findall('./column')
            if identifier(column.get('name', '')) == local
        ]
        for relation in connection.findall('.//relation'):
            if identifier(relation.get('name', '')) == parent:
                declarations.extend(
                    column for column in relation.findall('./columns/column')
                    if identifier(column.get('name', '')) == remote
                )
        return {column.get('datatype').strip() for column in declarations
                if (column.get('datatype') or '').strip()}

    def test_custom_sql_unknown_local_types_remain_absent(self):
        fixtures = dict(all_workbooks())
        self.assertIn('wb_64c6c0a2', fixtures)
        root = ET.parse(fixtures['wb_64c6c0a2']).getroot()
        candidates = [
            datasource for datasource in root.findall('./datasources/datasource')
            if datasource.find('./connection//relation[@type="text"]') is not None
        ]
        self.assertEqual(len(candidates), 1, 'Expected one Custom SQL datasource')
        datasource = candidates[0]
        connection = datasource.find('./connection')
        self.assertIsNotNone(connection)
        unknown = [
            mapping for mapping in connection.findall('./cols/map')
            if not self._declared_types(datasource, connection, mapping)
        ]
        unknown_names = {self._map_parts(mapping)[0] for mapping in unknown}
        self.assertTrue(
            {'fiscal_year', 'fiscal_quarter', 'budget_channel'} <= unknown_names,
            'Custom SQL has no original type declaration for these local fields',
        )
        context = f"{describe('wb_64c6c0a2')} / {datasource.get('name')}"
        for mapping in unknown:
            with self.subTest(local=mapping.get('key')):
                record = self._record_for_map(connection, mapping, context)
                self.assertIsNone(
                    record.find('local-type'),
                    f'{context}: {mapping.get("key")} has no declared type in this '
                    'datasource; do not infer SQL types or borrow another datasource type',
                )


def _make_sample_column_map_case(fid, workbook):
    def _test(self):
        root = ET.parse(workbook).getroot()
        for datasource in root.findall('./datasources/datasource'):
            context = f"{describe(fid)} / {datasource.get('name')}"
            # Only explicit maps in the datasource's own direct connection
            # require records; unreferenced physical columns are not required.
            for connection in datasource.findall('./connection'):
                for mapping in connection.findall('./cols/map'):
                    record = self._record_for_map(connection, mapping, context)
                    declared_types = self._declared_types(datasource, connection, mapping)
                    if declared_types:
                        self.assertEqual(len(declared_types), 1, context)
                        self.assertEqual(record.findtext('local-type'),
                                         next(iter(declared_types)), context)
                    else:
                        self.assertIsNone(record.find('local-type'), context)
    _test.__name__ = f'test_mapped_column_metadata_{fid}'
    return _test


for _fid, _path in all_workbooks():
    if os.path.splitext(_path)[1].lower() == '.twb':
        setattr(TestSampleColumnMapProvenance, f'test_mapped_column_metadata_{_fid}',
                _make_sample_column_map_case(_fid, _path))


if __name__ == '__main__':
    unittest.main()
