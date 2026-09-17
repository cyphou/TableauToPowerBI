"""Tests for powerbi_import/powerquery_diff.py.

Verifies the Tableau-vs-PowerBI table/column data-fidelity comparison.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.powerquery_diff import (
    _find_generated_match,
    _normalize_table_name,
    compare_report_tables,
)


def _write_tmdl_table(semantic_model_dir, filename, table_name, columns):
    tables_dir = os.path.join(semantic_model_dir, 'definition', 'tables')
    os.makedirs(tables_dir, exist_ok=True)
    lines = [f"table {table_name}"]
    for col in columns:
        lines.append(f"\tcolumn '{col}'")
        lines.append("\t\tdataType: string")
    lines.append("\tpartition 'p' = m")
    lines.append("\t\tmode: import")
    lines.append("\t\tsource = ```")
    lines.append("\t\t\tlet Source = \"x\" in Source")
    lines.append("\t\t```")
    with open(os.path.join(tables_dir, filename), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


class TestNormalizeTableName(unittest.TestCase):
    def test_strips_brackets_and_lowercases(self):
        self.assertEqual(_normalize_table_name('[Orders]'), 'orders')

    def test_strips_qualified_bracket_path(self):
        self.assertEqual(_normalize_table_name('[federated.abc123].[Orders]'), 'orders')


class TestFindGeneratedMatch(unittest.TestCase):
    def test_exact_match(self):
        generated = {'orders': {'name': 'Orders', 'columns': []}}
        match = _find_generated_match('orders', generated)
        self.assertIsNotNone(match)
        self.assertEqual(match['name'], 'Orders')

    def test_no_match_returns_none(self):
        generated = {'customers': {'name': 'Customers', 'columns': []}}
        self.assertIsNone(_find_generated_match('orders', generated))


class TestCompareReportTables(unittest.TestCase):
    def test_full_coverage(self):
        extracted = {
            'datasources': [{
                'name': 'ds1',
                'tables': [{
                    'name': 'Orders',
                    'columns': [{'name': 'OrderID'}, {'name': 'Amount'}],
                }],
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = os.path.join(tmp, 'Report')
            semantic_model_dir = os.path.join(project_dir, 'Report.SemanticModel')
            _write_tmdl_table(semantic_model_dir, 'Orders.tmdl', 'Orders',
                              ['OrderID', 'Amount'])
            result = compare_report_tables(extracted, project_dir, 'Report')

        self.assertEqual(result['summary']['source_tables'], 1)
        self.assertEqual(result['summary']['tables_found'], 1)
        self.assertEqual(result['summary']['avg_column_coverage_percent'], 100.0)

    def test_missing_table_flagged(self):
        extracted = {
            'datasources': [{
                'name': 'ds1',
                'tables': [{
                    'name': 'Orders',
                    'columns': [{'name': 'OrderID'}],
                }],
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = os.path.join(tmp, 'Report')
            semantic_model_dir = os.path.join(project_dir, 'Report.SemanticModel')
            _write_tmdl_table(semantic_model_dir, 'Customers.tmdl', 'Customers',
                              ['CustomerID'])
            result = compare_report_tables(extracted, project_dir, 'Report')

        self.assertEqual(result['summary']['tables_found'], 0)
        self.assertEqual(result['tables'][0]['table'], 'Orders')
        self.assertFalse(result['tables'][0]['found'])

    def test_partial_column_coverage(self):
        extracted = {
            'datasources': [{
                'name': 'ds1',
                'tables': [{
                    'name': 'Orders',
                    'columns': [{'name': 'OrderID'}, {'name': 'Amount'},
                               {'name': 'Discount'}],
                }],
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = os.path.join(tmp, 'Report')
            semantic_model_dir = os.path.join(project_dir, 'Report.SemanticModel')
            _write_tmdl_table(semantic_model_dir, 'Orders.tmdl', 'Orders',
                              ['OrderID', 'Amount'])
            result = compare_report_tables(extracted, project_dir, 'Report')

        entry = result['tables'][0]
        self.assertTrue(entry['found'])
        self.assertIn('Discount', entry['missing_columns'])
        self.assertAlmostEqual(entry['column_coverage_percent'], 200 / 3, places=1)


if __name__ == '__main__':
    unittest.main()
