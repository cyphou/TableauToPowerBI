"""Hyper metadata and legacy diagnostic M generation tests.

Sample rows are diagnostic only and cannot satisfy a full-data Import binding.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'powerbi_import'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))


class TestHyperFilesLoadedInPipeline(unittest.TestCase):
    """hyper_files.json is loaded by _load_converted_objects()."""

    def test_hyper_files_key_present(self):
        import tempfile

        from import_to_powerbi import PowerBIImporter
        with tempfile.TemporaryDirectory() as tmpdir:
            imp = PowerBIImporter(tmpdir)
            data = imp._load_converted_objects()
            self.assertIn('hyper_files', data)
            self.assertEqual(data['hyper_files'], [])

    def test_hyper_files_loaded_from_json(self):
        import tempfile

        from import_to_powerbi import PowerBIImporter
        sample = [{'path': 'Data/Extract.hyper', 'filename': 'Extract.hyper',
                    'tables': [{'table': 'Extract', 'columns': []}]}]
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, 'hyper_files.json'), 'w') as f:
                json.dump(sample, f)
            imp = PowerBIImporter(tmpdir)
            data = imp._load_converted_objects()
            self.assertEqual(len(data['hyper_files']), 1)
            self.assertEqual(data['hyper_files'][0]['filename'], 'Extract.hyper')


class TestHyperImportBindingInTMDL(unittest.TestCase):
    """Unbound or sample-only extracts cannot produce a TMDL model."""

    def _make_datasource(self, table_name='Extract', conn_type='hyper'):
        return {
            'name': 'TestDS',
            'connection': {'type': conn_type, 'details': {}},
            'connection_map': {},
            'tables': [{
                'name': table_name,
                'columns': [
                    {'name': 'ID', 'datatype': 'integer'},
                    {'name': 'Name', 'datatype': 'string'},
                ],
            }],
            'calculations': [],
            'relationships': [],
            'columns': [],
        }

    def _make_hyper_files(self, table_name='Extract'):
        return [{
            'path': 'Data/Extract.hyper',
            'filename': 'Extract.hyper',
            'hyper_reader_tables': [{
                'table': table_name,
                'columns': [
                    {'name': 'ID', 'hyper_type': 'integer'},
                    {'name': 'Name', 'hyper_type': 'text'},
                ],
                'sample_rows': [
                    {'ID': 1, 'Name': 'Alice'},
                    {'ID': 2, 'Name': 'Bob'},
                    {'ID': 3, 'Name': 'Charlie'},
                ],
                'row_count': 3,
            }],
        }]

    def _read_all_tmdl(self, tmpdir):
        table_dir = os.path.join(tmpdir, 'definition', 'tables')
        content = ''
        if os.path.isdir(table_dir):
            for tf in os.listdir(table_dir):
                if tf.endswith('.tmdl'):
                    with open(os.path.join(table_dir, tf), 'r', encoding='utf-8') as f:
                        content += f.read()
        return content

    def _assert_unbound_hyper_rejected(self, ds, hyper_files):
        import tempfile

        from tmdl_generator import generate_tmdl

        from powerbi_import.errors import GenerationError

        extra = {
            'hierarchies': [], 'sets': [], 'groups': [], 'bins': [],
            'aliases': {}, 'parameters': [], 'user_filters': [],
            '_datasources': [ds], '_worksheets': [],
            'hyper_files': hyper_files,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(GenerationError, 'exact physical identity'):
                generate_tmdl(
                    datasources=[ds],
                    report_name='TestReport',
                    extra_objects=extra,
                    output_dir=tmpdir,
                )
            self.assertEqual(self._read_all_tmdl(tmpdir), '')

    def test_sample_hyper_rows_are_not_full_import_data(self):
        ds = self._make_datasource()
        self._assert_unbound_hyper_rejected(ds, self._make_hyper_files())

    def test_missing_hyper_files_are_rejected_instead_of_empty_table(self):
        self._assert_unbound_hyper_rejected(self._make_datasource(), [])

    def test_hyper_inlining_skipped_when_prep_override_exists(self):
        import tempfile

        from tmdl_generator import generate_tmdl

        ds = self._make_datasource()
        ds['m_query_overrides'] = {
            'Extract': 'let\n    Source = Sql.Database("server", "db")\nin\n    Source'
        }
        extra = {
            'hierarchies': [], 'sets': [], 'groups': [], 'bins': [],
            'aliases': {}, 'parameters': [], 'user_filters': [],
            '_datasources': [ds], '_worksheets': [],
            'hyper_files': self._make_hyper_files(),
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            generate_tmdl(
                datasources=[ds],
                report_name='TestReport',
                extra_objects=extra,
                output_dir=tmpdir,
            )
            content = self._read_all_tmdl(tmpdir)
            self.assertIn('Sql.Database', content)
            self.assertNotIn('Alice', content)

    def test_unbound_extract_connection_is_rejected(self):
        ds = self._make_datasource(conn_type='extract')
        self._assert_unbound_hyper_rejected(ds, self._make_hyper_files())

    def test_unbound_dataengine_connection_is_rejected(self):
        ds = self._make_datasource(conn_type='dataengine')
        self._assert_unbound_hyper_rejected(ds, self._make_hyper_files())

    def test_non_hyper_connection_untouched(self):
        import tempfile

        from tmdl_generator import generate_tmdl

        ds = self._make_datasource(conn_type='sqlserver')
        ds['connection'] = {
            'type': 'sqlserver',
            'details': {'server': 'localhost', 'database': 'testdb'},
        }
        extra = {
            'hierarchies': [], 'sets': [], 'groups': [], 'bins': [],
            'aliases': {}, 'parameters': [], 'user_filters': [],
            '_datasources': [ds], '_worksheets': [],
            'hyper_files': self._make_hyper_files(),
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            generate_tmdl(
                datasources=[ds],
                report_name='TestReport',
                extra_objects=extra,
                output_dir=tmpdir,
            )
            content = self._read_all_tmdl(tmpdir)
            # SQL Server connection should NOT have inline hyper data
            self.assertNotIn('Alice', content)

    def test_case_insensitive_table_name_does_not_replace_physical_identity(self):
        ds = self._make_datasource(table_name='extract')  # lowercase
        hyper_files = self._make_hyper_files(table_name='Extract')  # PascalCase
        self._assert_unbound_hyper_rejected(ds, hyper_files)

    def test_multiple_sample_tables_are_not_a_full_import(self):
        ds = {
            'name': 'MultiDS',
            'connection': {'type': 'hyper', 'details': {}},
            'connection_map': {},
            'tables': [
                {'name': 'Orders', 'columns': [{'name': 'OrderID', 'datatype': 'integer'}]},
                {'name': 'Products', 'columns': [{'name': 'ProdName', 'datatype': 'string'}]},
            ],
            'calculations': [],
            'relationships': [],
            'columns': [],
        }
        hyper_files = [{
            'path': 'Data/Extract.hyper',
            'filename': 'Extract.hyper',
            'hyper_reader_tables': [
                {
                    'table': 'Orders',
                    'columns': [{'name': 'OrderID', 'hyper_type': 'integer'}],
                    'sample_rows': [{'OrderID': 100}, {'OrderID': 200}],
                    'row_count': 2,
                },
                {
                    'table': 'Products',
                    'columns': [{'name': 'ProdName', 'hyper_type': 'text'}],
                    'sample_rows': [{'ProdName': 'Widget'}],
                    'row_count': 1,
                },
            ],
        }]
        self._assert_unbound_hyper_rejected(ds, hyper_files)


class TestGenerateMFromHyper(unittest.TestCase):
    """Unit tests for generate_m_from_hyper()."""

    def test_returns_m_with_inline_data(self):
        from m_query_builder import generate_m_from_hyper
        tables = [{
            'table': 'Orders',
            'columns': [
                {'name': 'OrderID', 'hyper_type': 'integer'},
                {'name': 'Product', 'hyper_type': 'text'},
            ],
            'sample_rows': [
                {'OrderID': 1, 'Product': 'Widget'},
                {'OrderID': 2, 'Product': 'Gadget'},
            ],
            'row_count': 2,
        }]
        result = generate_m_from_hyper(tables, table_name='Orders')
        assert result is not None
        self.assertIn('Widget', result)
        self.assertIn('Gadget', result)

    def test_returns_none_for_empty(self):
        from m_query_builder import generate_m_from_hyper
        self.assertIsNone(generate_m_from_hyper([]))
        self.assertIsNone(generate_m_from_hyper(None))

    def test_matches_by_table_name(self):
        from m_query_builder import generate_m_from_hyper
        tables = [
            {'table': 'A', 'columns': [{'name': 'x', 'hyper_type': 'text'}],
             'sample_rows': [{'x': 'aaa'}], 'row_count': 1},
            {'table': 'B', 'columns': [{'name': 'y', 'hyper_type': 'text'}],
             'sample_rows': [{'y': 'bbb'}], 'row_count': 1},
        ]
        result = generate_m_from_hyper(tables, table_name='B')
        assert result is not None
        self.assertIn('bbb', result)

    def test_uses_first_table_when_no_match(self):
        from m_query_builder import generate_m_from_hyper
        tables = [
            {'table': 'Only', 'columns': [{'name': 'z', 'hyper_type': 'text'}],
             'sample_rows': [{'z': 'zzz'}], 'row_count': 1},
        ]
        result = generate_m_from_hyper(tables, table_name='NonExistent')
        assert result is not None
        self.assertIn('zzz', result)

    def test_no_sample_rows_produces_empty_table(self):
        from m_query_builder import generate_m_from_hyper
        tables = [{
            'table': 'Empty',
            'columns': [{'name': 'col1', 'hyper_type': 'text'}],
            'sample_rows': [],
            'row_count': 0,
        }]
        result = generate_m_from_hyper(tables)
        assert result is not None
        self.assertIn('#table', result)


if __name__ == '__main__':
    unittest.main()
