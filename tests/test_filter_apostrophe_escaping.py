"""Regression tests for apostrophe escaping in visual/slicer filter literals
and the TMDL per-table cascade guard.

Covers two backlog items:
  * #38 — apostrophe escaping in visual filters (broken PBIR JSON when a
    categorical/range filter value contains a single quote, e.g. ``O'Brien``).
  * #52 — TMDL cascade guard (a single failing table must not cascade into a
    model that references an omitted table).
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.pbip_generator import _filter_literal
from powerbi_import.visual_generator import (
    _build_visual_filters,
    create_filters_config,
)


class TestFilterLiteralApostrophe(unittest.TestCase):
    """_filter_literal (range/date filter min/max) must double apostrophes."""

    def test_string_value_apostrophe_escaped(self):
        self.assertEqual(_filter_literal("O'Brien"), "'O''Brien'")

    def test_string_value_multiple_apostrophes(self):
        self.assertEqual(_filter_literal("d'a'b'c"), "'d''a''b''c'")

    def test_plain_string_unchanged(self):
        self.assertEqual(_filter_literal("Alpha"), "'Alpha'")

    def test_numeric_still_unquoted(self):
        self.assertEqual(_filter_literal("42"), "42L")
        self.assertEqual(_filter_literal("3.5"), "3.5D")


class TestBuildVisualFiltersApostrophe(unittest.TestCase):
    """_build_visual_filters categorical values must double apostrophes."""

    def test_categorical_value_apostrophe_escaped(self):
        filters = [{
            'field': 'Manager',
            'type': 'categorical',
            'values': ["O'Brien", "Smith"],
        }]
        result = _build_visual_filters(filters, {'Manager': 'People'})
        self.assertEqual(len(result), 1)
        vals = result[0]['values']
        literals = [cell[0]['Literal']['Value'] for cell in vals]
        self.assertIn("'O''Brien'", literals)
        self.assertIn("'Smith'", literals)

    def test_plain_categorical_value_unchanged(self):
        filters = [{
            'field': 'Region',
            'type': 'categorical',
            'values': ['East'],
        }]
        result = _build_visual_filters(filters, {'Region': 'Geo'})
        self.assertEqual(
            result[0]['values'][0][0]['Literal']['Value'], "'East'"
        )


class TestCreateFiltersConfigApostrophe(unittest.TestCase):
    """create_filters_config In-filter values must double apostrophes."""

    def test_in_filter_value_apostrophe_escaped(self):
        filters = [{'field': 'Owner', 'values': ["O'Brien", "N/A"]}]
        result = create_filters_config(filters, table_name='Accounts')
        self.assertEqual(len(result), 1)
        where_vals = (result[0]['filter']['Where'][0]['Condition']
                      ['In']['Values'])
        literals = [cell[0]['Literal']['Value'] for cell in where_vals]
        self.assertIn("'O''Brien'", literals)
        self.assertIn("'N/A'", literals)


class TestTmdlCascadeGuard(unittest.TestCase):
    """#52 — a failing table write must not leave dangling references."""

    def test_failed_table_pruned_from_model_and_relationships(self):
        import powerbi_import.tmdl_generator as tg

        datasources = [{
            'name': 'DS',
            'connection': {'type': 'SQL Server',
                           'details': {'server': 's', 'database': 'd'}},
            'connection_map': {},
            'tables': [
                {'name': 'Orders',
                 'columns': [{'name': 'CustomerID', 'datatype': 'integer'}]},
                {'name': 'Customers',
                 'columns': [{'name': 'CustomerID', 'datatype': 'integer'}]},
            ],
            'relationships': [{
                'fromTable': 'Orders', 'fromColumn': 'CustomerID',
                'toTable': 'Customers', 'toColumn': 'CustomerID',
                'joinType': 'inner',
            }],
            'calculations': [],
        }]

        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            real_write = tg._write_table_tmdl

            def _selective_write(tables_dir, table):
                if table.get('name') == 'Customers':
                    raise ValueError('simulated table write failure')
                return real_write(tables_dir, table)

            sm_dir = os.path.join(tmp, 'Test.SemanticModel')
            with patch.object(tg, '_write_table_tmdl',
                              side_effect=_selective_write):
                tg.generate_tmdl(datasources, 'Test', {}, sm_dir)

            tables_dir = os.path.join(sm_dir, 'definition', 'tables')
            written = set(os.listdir(tables_dir)) if os.path.isdir(tables_dir) else set()
            self.assertNotIn('Customers.tmdl', written)

            # relationships.tmdl must NOT reference the omitted table.
            rel_path = os.path.join(sm_dir, 'definition', 'relationships.tmdl')
            if os.path.isfile(rel_path):
                with open(rel_path, encoding='utf-8') as f:
                    rel_text = f.read()
                self.assertNotIn('Customers', rel_text)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
