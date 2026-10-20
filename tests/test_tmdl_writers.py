"""Physical sourceColumn bindings match Desktop's raw string serialization."""

import os
import base64
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.tmdl_writers import _write_table_tmdl


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
                'Customer\'s "Order Date"', ' Order Date ',
                "'Order Date'", 'Date_123', 'Événement Date'):
            with self.subTest(source=source):
                text, value = self._emit(source)
                self.assertEqual(value, source)
                self.assertIn("\tcolumn 'Display Name'\n", text)

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
                   'Customer\'s "Order Date"', 'Date_123', 'Événement Date']
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
            quote = lambda value: "'" + value.replace("'", "''") + "'"
            script = "$ErrorActionPreference = 'Stop'; "
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
