"""Physical sourceColumn bindings roundtrip through Desktop's string syntax."""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.tmdl_writers import (
    _parse_source_column,
    _serialize_source_column,
    _write_table_tmdl,
)


class TestSourceColumnStringEscaping(unittest.TestCase):
    def _emit(self, source, name='Display Name'):
        column = {'name': name, 'dataType': 'dateTime', 'summarizeBy': 'none'}
        if source is not None:
            column['sourceColumn'] = source
        table = {
            'name': 'Orders', 'columns': [column], 'measures': [],
            'partitions': [{
                'name': 'Orders', 'mode': 'import',
                'source': {'type': 'm', 'expression': 'let Source = null in Source'},
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            _write_table_tmdl(directory, table)
            with open(os.path.join(directory, 'Orders.tmdl'), encoding='utf-8') as stream:
                text = stream.read()
        values = re.findall(r'^\t\tsourceColumn: (.*)$', text, re.MULTILINE)
        self.assertEqual(len(values), 1)
        return text, values[0]

    def test_physical_names_preserve_raw_binding_without_added_quotes(self):
        for source in (
                'Order Date', "Customer's Date", 'Order "Date"',
                'Customer\'s "Order Date"',
                "'Order Date'", 'Date_123', 'Événement Date'):
            with self.subTest(source=source):
                text, value = self._emit(source)
                self.assertEqual(value, source)
                self.assertIn("\tcolumn 'Display Name'\n", text)

    def test_peripheral_whitespace_and_outer_quotes_are_escaped_exactly(self):
        for source in (' Order Date ', 'Order Date ', ' Order Date', '\tDate\t',
                       ' A"B ', '"Quoted"', '"', '\u00a0Date\u00a0'):
            with self.subTest(source=source):
                _, value = self._emit(source)
                self.assertEqual(value, '"' + source.replace('"', '""') + '"')
                self.assertEqual(_parse_source_column(value), source)

    def test_single_quotes_are_literal_binding_characters(self):
        self.assertEqual(_parse_source_column("'Order Date'"), "'Order Date'")
        self.assertEqual(_parse_source_column("Customer''s Date"), "Customer''s Date")

    def test_invalid_multiline_bindings_are_not_written_as_tmdl_properties(self):
        for source in (None, 42, 'Line\nColumn', 'Line\rColumn'):
            with self.subTest(source=source):
                with self.assertRaisesRegex(ValueError, 'single-line string'):
                    _serialize_source_column(source)

    def test_all_binding_readers_preserve_distinct_physical_names(self):
        import migrate
        from powerbi_import.artifact_diff import _parse_tmdl_table as parse_artifact
        from powerbi_import.shared_model import _parse_tmdl_table as parse_shared

        sources = ['Order Date', 'Order Date ', ' Order Date', '"Quoted"',
                   "'Order Date'", ' A"B ']
        with tempfile.TemporaryDirectory() as directory:
            _write_table_tmdl(directory, {
                'name': 'Orders',
                'columns': [{'name': f'C{index}', 'sourceColumn': source,
                             'dataType': 'int64'} for index, source in enumerate(sources)],
                'partitions': [], 'measures': [],
            })
            path = os.path.join(directory, 'Orders.tmdl')
            with open(path, encoding='utf-8') as stream:
                content = stream.read()
            self.assertEqual([col['sourceColumn'] for col in
                              parse_artifact(path)['columns']], sources)
            self.assertEqual([col['sourceColumn'] for col in
                              parse_shared(content)['columns']], sources)
            expected = [
                '{"' + source.replace('"', '""') + '", Int64.Type}'
                for source in sources
            ]
            self.assertEqual(migrate._hyper_csv_type_changes(content, sources, {}),
                             expected)

    def test_missing_source_column_defaults_to_exact_column_name(self):
        _, value = self._emit(None, name="Order's Date")
        self.assertEqual(value, "Order's Date")

    def test_display_identifier_and_physical_source_are_independent(self):
        text, value = self._emit('Order Date', name="User's Date")
        self.assertIn("\tcolumn 'User''s Date'\n", text)
        self.assertEqual(value, 'Order Date')
        self.assertNotIn("sourceColumn: 'Order Date'", text)
        self.assertNotIn('sourceColumn: "Order Date"', text)

    def test_installed_tom_parser_roundtrips_physical_source_bindings(self):
        desktop = os.environ.get(
            'TTPBI_TOM_BIN', r'C:\Program Files\Microsoft Power BI Desktop\bin')
        shell = shutil.which('pwsh')
        assemblies = (
            'Microsoft.AnalysisServices.Server.Core.dll',
            'Microsoft.AnalysisServices.Server.Tabular.dll',
            'Microsoft.PowerBI.Tabular.dll',
        )
        if not shell or not all(os.path.isfile(os.path.join(desktop, name))
                                for name in assemblies):
            self.skipTest('PowerShell Core and Desktop TOM parser not installed')
        sources = ['Order Date', "Customer's Date", 'Order "Date"',
                   'Customer\'s "Order Date"', 'Date_123', 'Événement Date',
                   'Order Date ', ' Order Date', ' Order Date ', ' A"B ',
                   '"Quoted"', '"Leading', 'Trailing"', "'Order Date'",
                   '\tDate\t', '\u00a0Date\u00a0']
        with tempfile.TemporaryDirectory() as directory:
            tables = os.path.join(directory, 'tables')
            os.makedirs(tables)
            _write_table_tmdl(tables, {
                'name': 'Orders',
                'columns': [{'name': f'C{index}', 'dataType': 'string',
                             'sourceColumn': source}
                            for index, source in enumerate(sources)],
                'measures': [], 'partitions': [{
                    'name': 'Orders', 'mode': 'import',
                    'source': {'type': 'm', 'expression': 'let Source = null in Source'},
                }],
            })
            with open(os.path.join(directory, 'model.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write('model Model\n\tculture: en-US\n\nref table Orders\n')
            def quote(value):
                return "'" + value.replace("'", "''") + "'"

            script = (
                "$ErrorActionPreference = 'Stop'; "
                '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); ')
            for name in assemblies:
                script += (
                    f'[System.Reflection.Assembly]::LoadFrom('
                    f'{quote(os.path.join(desktop, name))}) | Out-Null; ')
            script += (
                '$model = [Microsoft.AnalysisServices.Tabular.TmdlSerializer]'
                f'::DeserializeModelFromFolder({quote(directory)}); '
                "$values = @($model.Tables['Orders'].Columns | "
                'ForEach-Object { $_.SourceColumn }); '
                'ConvertTo-Json -InputObject $values -Compress')
            encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
            result = subprocess.run(
                [shell, '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                capture_output=True, text=True, encoding='utf-8', timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout.strip()), sources)
