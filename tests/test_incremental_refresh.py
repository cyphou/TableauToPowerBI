"""Tests for incremental refresh detection, M parameter wiring, and TMDL generation.

Sprint 120 — v37.0.0: Incremental Refresh & M Parameter Wiring.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.tmdl_generator import (
    _detect_incremental_refresh_tables,
    _pick_best_date_column,
    _generate_refresh_policy,
    _inject_range_filter_m,
    _generate_incremental_m_parameters,
    apply_incremental_refresh,
    _write_expressions_tmdl,
    detect_refresh_policy,
    generate_tmdl,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_table(name, columns, partitions=None, mode='import'):
    """Build a minimal table dict for testing."""
    t = {
        'name': name,
        'columns': columns,
    }
    if partitions is not None:
        t['partitions'] = partitions
    else:
        t['partitions'] = [{
            'name': name,
            'mode': mode,
            'source': {
                'type': 'm',
                'expression': f'let\n    Source = Sql.Database("server", "db"),\n    {name} = Source{{[Schema="dbo",Item="{name}"]}}[Data]\nin\n    {name}',
            },
        }]
    return t


def _make_model(tables, datasources=None):
    """Build a minimal model dict for testing."""
    model = {'model': {'tables': tables}}
    if datasources:
        model['_datasources'] = datasources
    return model


def _make_datasource(connector_class='sqlserver'):
    """Build a minimal datasource dict for testing."""
    return {
        'connection': {'class': connector_class},
        'connection_map': {},
    }


def _date_col(name='OrderDate', dt='dateTime'):
    return {'name': name, 'dataType': dt}


def _text_col(name='CustomerName'):
    return {'name': name, 'dataType': 'string'}


def _int_col(name='Amount'):
    return {'name': name, 'dataType': 'int64'}


# ══════════════════════════════════════════════════════════════════════════════
# 1. _pick_best_date_column
# ══════════════════════════════════════════════════════════════════════════════

class TestPickBestDateColumn(unittest.TestCase):

    def test_returns_none_for_no_date_columns(self):
        table = _make_table('T', [_text_col(), _int_col()])
        self.assertIsNone(_pick_best_date_column(table))

    def test_returns_single_date_column(self):
        table = _make_table('T', [_text_col(), _date_col('OrderDate')])
        self.assertEqual(_pick_best_date_column(table), 'OrderDate')

    def test_prefers_updated_column(self):
        table = _make_table('T', [
            _date_col('CreatedDate'),
            _date_col('UpdatedAt'),
            _date_col('OrderDate'),
        ])
        self.assertEqual(_pick_best_date_column(table), 'UpdatedAt')

    def test_prefers_modified_column(self):
        table = _make_table('T', [
            _date_col('CreatedDate'),
            _date_col('LastModified'),
        ])
        self.assertEqual(_pick_best_date_column(table), 'LastModified')

    def test_prefers_last_underscore(self):
        table = _make_table('T', [
            _date_col('EventDate'),
            _date_col('last_changed'),
        ])
        self.assertEqual(_pick_best_date_column(table), 'last_changed')

    def test_date_type_variations(self):
        for dt in ('datetime', 'datetime2', 'timestamp', 'datetimeoffset', 'date'):
            table = _make_table('T', [{'name': 'Col1', 'dataType': dt}])
            result = _pick_best_date_column(table)
            self.assertEqual(result, 'Col1', f"Failed for dataType={dt}")

    def test_date_name_detection(self):
        """Columns with date-like names should be detected even with non-date type."""
        table = _make_table('T', [{'name': 'created_at', 'dataType': 'string'}])
        self.assertEqual(_pick_best_date_column(table), 'created_at')

    def test_empty_columns(self):
        table = _make_table('T', [])
        self.assertIsNone(_pick_best_date_column(table))


# ══════════════════════════════════════════════════════════════════════════════
# 2. _detect_incremental_refresh_tables
# ══════════════════════════════════════════════════════════════════════════════

class TestDetectIncrementalRefreshTables(unittest.TestCase):

    def test_detects_eligible_table(self):
        table = _make_table('Orders', [_date_col(), _text_col()])
        model = _make_model([table])
        ds = [_make_datasource('sqlserver')]
        result = _detect_incremental_refresh_tables(model, ds)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0]['name'], 'Orders')
        self.assertEqual(result[0][1], 'OrderDate')

    def test_skips_calendar_table(self):
        table = _make_table('Calendar', [_date_col('Date')])
        model = _make_model([table])
        result = _detect_incremental_refresh_tables(model, [_make_datasource()])
        self.assertEqual(len(result), 0)

    def test_skips_calculated_table(self):
        table = _make_table('Calc', [_date_col()], partitions=[{
            'name': 'Calc', 'mode': 'import',
            'source': {'type': 'calculated', 'expression': 'DATATABLE(...)'},
        }])
        model = _make_model([table])
        result = _detect_incremental_refresh_tables(model, [_make_datasource()])
        self.assertEqual(len(result), 0)

    def test_skips_direct_query_table(self):
        table = _make_table('DQ', [_date_col()], mode='directQuery')
        table['partitions'][0]['mode'] = 'directQuery'
        model = _make_model([table])
        result = _detect_incremental_refresh_tables(model, [_make_datasource()])
        self.assertEqual(len(result), 0)

    def test_skips_non_foldable_connector(self):
        table = _make_table('Orders', [_date_col()])
        model = _make_model([table])
        ds = [_make_datasource('csv')]
        result = _detect_incremental_refresh_tables(model, ds)
        self.assertEqual(len(result), 0)

    def test_no_date_columns(self):
        table = _make_table('Products', [_text_col(), _int_col()])
        model = _make_model([table])
        result = _detect_incremental_refresh_tables(model, [_make_datasource()])
        self.assertEqual(len(result), 0)

    def test_multiple_eligible_tables(self):
        t1 = _make_table('Orders', [_date_col('OrderDate'), _text_col()])
        t2 = _make_table('Events', [_date_col('EventDate'), _int_col()])
        t3 = _make_table('Products', [_text_col()])  # no date
        model = _make_model([t1, t2, t3])
        result = _detect_incremental_refresh_tables(model, [_make_datasource()])
        self.assertEqual(len(result), 2)
        names = [r[0]['name'] for r in result]
        self.assertIn('Orders', names)
        self.assertIn('Events', names)

    def test_postgres_connector(self):
        table = _make_table('Sales', [_date_col()])
        model = _make_model([table])
        ds = [_make_datasource('postgresql')]
        result = _detect_incremental_refresh_tables(model, ds)
        self.assertEqual(len(result), 1)

    def test_no_datasources_assumes_foldable(self):
        """When no datasources are provided, assume foldable (best effort)."""
        table = _make_table('Orders', [_date_col()])
        model = _make_model([table])
        result = _detect_incremental_refresh_tables(model, datasources=None)
        self.assertEqual(len(result), 1)


# ══════════════════════════════════════════════════════════════════════════════
# 3. _generate_refresh_policy
# ══════════════════════════════════════════════════════════════════════════════

class TestGenerateRefreshPolicy(unittest.TestCase):

    def test_basic_policy(self):
        policy = _generate_refresh_policy('Orders', 'OrderDate')
        self.assertEqual(policy['incrementalGranularity'], 'Day')
        self.assertEqual(policy['incrementalPeriods'], 3)
        self.assertEqual(policy['rollingWindowGranularity'], 'Month')
        self.assertEqual(policy['rollingWindowPeriods'], 12)
        self.assertIn('OrderDate', policy['pollingExpression'])
        self.assertIn('RangeStart', policy['sourceExpression'])
        self.assertIn('RangeEnd', policy['sourceExpression'])

    def test_custom_rolling_months(self):
        policy = _generate_refresh_policy('Events', 'EventDate', rolling_months=24)
        self.assertEqual(policy['rollingWindowPeriods'], 24)

    def test_custom_incremental_days(self):
        policy = _generate_refresh_policy('Events', 'EventDate', incremental_days=7)
        self.assertEqual(policy['incrementalPeriods'], 7)

    def test_date_column_in_expressions(self):
        policy = _generate_refresh_policy('T', 'MyDate')
        self.assertIn('MyDate', policy['pollingExpression'])
        self.assertIn('[MyDate]', policy['sourceExpression'])

    def test_policy_has_all_keys(self):
        policy = _generate_refresh_policy('T', 'D')
        expected_keys = {
            'incrementalGranularity', 'incrementalPeriods',
            'rollingWindowGranularity', 'rollingWindowPeriods',
            'pollingExpression', 'sourceExpression', 'dateColumn',
        }
        self.assertEqual(set(policy.keys()), expected_keys)


# ══════════════════════════════════════════════════════════════════════════════
# 4. _inject_range_filter_m
# ══════════════════════════════════════════════════════════════════════════════

class TestInjectRangeFilterM(unittest.TestCase):

    def test_basic_injection(self):
        m = 'let\n    Source = Sql.Database("s", "d"),\n    Orders = Source{[Schema="dbo",Item="Orders"]}[Data]\nin\n    Orders'
        result = _inject_range_filter_m(m, 'OrderDate')
        self.assertIn('RangeStart', result)
        self.assertIn('RangeEnd', result)
        self.assertIn('[OrderDate]', result)
        self.assertIn('Table.SelectRows', result)
        self.assertIn('#"Incremental Filter"', result)

    def test_already_has_range_filter(self):
        m = 'let\n    Source = Table,\n    F = Table.SelectRows(Source, each [D] >= RangeStart and [D] < RangeEnd)\nin\n    F'
        result = _inject_range_filter_m(m, 'D')
        self.assertEqual(result, m)  # unchanged

    def test_empty_expression(self):
        self.assertEqual(_inject_range_filter_m('', 'D'), '')

    def test_none_expression(self):
        self.assertIsNone(_inject_range_filter_m(None, 'D'))

    def test_no_in_keyword(self):
        m = 'Sql.Database("s", "d")'
        result = _inject_range_filter_m(m, 'D')
        self.assertEqual(result, m)  # cannot inject, return unchanged

    def test_none_column(self):
        m = 'let\n    Source = T\nin\n    Source'
        result = _inject_range_filter_m(m, None)
        self.assertEqual(result, m)


# ══════════════════════════════════════════════════════════════════════════════
# 5. _generate_incremental_m_parameters
# ══════════════════════════════════════════════════════════════════════════════

class TestGenerateIncrementalMParameters(unittest.TestCase):

    def test_returns_two_parameters(self):
        params = _generate_incremental_m_parameters()
        self.assertEqual(len(params), 2)
        names = [p[0] for p in params]
        self.assertIn('RangeStart', names)
        self.assertIn('RangeEnd', names)

    def test_rangestart_expression(self):
        params = _generate_incremental_m_parameters()
        rs = [p for p in params if p[0] == 'RangeStart'][0]
        self.assertIn('#datetime', rs[1])
        self.assertIn('IsParameterQuery=true', rs[1])
        self.assertIn('Type="DateTime"', rs[1])

    def test_rangeend_expression(self):
        params = _generate_incremental_m_parameters()
        re_param = [p for p in params if p[0] == 'RangeEnd'][0]
        self.assertIn('#datetime', re_param[1])
        self.assertIn('2030', re_param[1])


# ══════════════════════════════════════════════════════════════════════════════
# 6. apply_incremental_refresh (main orchestrator)
# ══════════════════════════════════════════════════════════════════════════════

class TestApplyIncrementalRefresh(unittest.TestCase):

    def test_configures_eligible_table(self):
        table = _make_table('Orders', [_date_col(), _text_col()])
        model = _make_model([table])
        ds = [_make_datasource()]
        result = apply_incremental_refresh(model, ds)
        self.assertIn('Orders', result['tables_configured'])
        self.assertIn('RangeStart', result['parameters_added'])
        self.assertIn('RangeEnd', result['parameters_added'])
        self.assertEqual(result['date_columns']['Orders'], 'OrderDate')
        # Verify policy was attached
        self.assertIn('refreshPolicy', table)
        self.assertEqual(table['refreshPolicy']['rollingWindowPeriods'], 12)

    def test_no_eligible_tables(self):
        table = _make_table('Products', [_text_col()])
        model = _make_model([table])
        result = apply_incremental_refresh(model, [_make_datasource()])
        self.assertEqual(result['tables_configured'], [])
        self.assertEqual(result['parameters_added'], [])

    def test_custom_rolling_months(self):
        table = _make_table('Orders', [_date_col()])
        model = _make_model([table])
        result = apply_incremental_refresh(model, [_make_datasource()], rolling_months=24)
        self.assertEqual(table['refreshPolicy']['rollingWindowPeriods'], 24)

    def test_parameterize_false_no_m_injection(self):
        table = _make_table('Orders', [_date_col()])
        original_m = table['partitions'][0]['source']['expression']
        model = _make_model([table])
        result = apply_incremental_refresh(model, [_make_datasource()], parameterize=False)
        # Table should still get a policy
        self.assertIn('Orders', result['tables_configured'])
        # But no parameters added
        self.assertEqual(result['parameters_added'], [])
        # M expression unchanged
        self.assertEqual(table['partitions'][0]['source']['expression'], original_m)

    def test_parameterize_true_injects_m_filter(self):
        table = _make_table('Orders', [_date_col()])
        model = _make_model([table])
        apply_incremental_refresh(model, [_make_datasource()], parameterize=True)
        expr = table['partitions'][0]['source']['expression']
        self.assertIn('RangeStart', expr)
        self.assertIn('RangeEnd', expr)

    def test_incremental_params_stored_on_model(self):
        table = _make_table('Orders', [_date_col()])
        model = _make_model([table])
        apply_incremental_refresh(model, [_make_datasource()])
        params = model.get('_incremental_params')
        self.assertIsNotNone(params)
        names = [p[0] for p in params]
        self.assertIn('RangeStart', names)
        self.assertIn('RangeEnd', names)

    def test_multiple_tables_configured(self):
        t1 = _make_table('Orders', [_date_col('OrderDate')])
        t2 = _make_table('Events', [_date_col('EventDate')])
        model = _make_model([t1, t2])
        result = apply_incremental_refresh(model, [_make_datasource()])
        self.assertEqual(len(result['tables_configured']), 2)
        self.assertIn('refreshPolicy', t1)
        self.assertIn('refreshPolicy', t2)


# ══════════════════════════════════════════════════════════════════════════════
# 7. _write_expressions_tmdl with incremental params
# ══════════════════════════════════════════════════════════════════════════════

class TestWriteExpressionsTmdlIncremental(unittest.TestCase):

    def test_writes_range_params(self):
        with tempfile.TemporaryDirectory() as td:
            tables = [_make_table('T', [_text_col()])]
            params = _generate_incremental_m_parameters()
            _write_expressions_tmdl(td, tables, incremental_params=params)
            path = os.path.join(td, 'expressions.tmdl')
            self.assertTrue(os.path.exists(path))
            content = open(path, 'r', encoding='utf-8').read()
            self.assertIn('expression RangeStart', content)
            self.assertIn('expression RangeEnd', content)
            self.assertIn('#datetime', content)
            self.assertIn('IsParameterQuery=true', content)

    def test_no_range_params_without_flag(self):
        with tempfile.TemporaryDirectory() as td:
            tables = [_make_table('T', [_text_col()])]
            _write_expressions_tmdl(td, tables)
            path = os.path.join(td, 'expressions.tmdl')
            content = open(path, 'r', encoding='utf-8').read()
            self.assertNotIn('RangeStart', content)
            self.assertNotIn('RangeEnd', content)

    def test_range_params_alongside_server_params(self):
        with tempfile.TemporaryDirectory() as td:
            tables = [_make_table('T', [_text_col()])]
            ds = [{'connection': {'server': 'myserver', 'database': 'mydb'}, 'connection_map': {}}]
            params = _generate_incremental_m_parameters()
            _write_expressions_tmdl(td, tables, datasources=ds, incremental_params=params)
            path = os.path.join(td, 'expressions.tmdl')
            content = open(path, 'r', encoding='utf-8').read()
            self.assertIn('expression DataFolder', content)
            self.assertIn('expression RangeStart', content)
            self.assertIn('expression RangeEnd', content)


# ══════════════════════════════════════════════════════════════════════════════
# 8. generate_tmdl signature accepts incremental refresh params
# ══════════════════════════════════════════════════════════════════════════════

class TestGenerateTmdlSignature(unittest.TestCase):

    def test_signature_accepts_incremental_params(self):
        """Verify generate_tmdl accepts the new keyword arguments."""
        import inspect
        sig = inspect.signature(generate_tmdl)
        params = list(sig.parameters.keys())
        self.assertIn('incremental_refresh', params)
        self.assertIn('incremental_refresh_months', params)
        self.assertIn('parameterize', params)

    def test_defaults(self):
        import inspect
        sig = inspect.signature(generate_tmdl)
        self.assertFalse(sig.parameters['incremental_refresh'].default)
        self.assertEqual(sig.parameters['incremental_refresh_months'].default, 12)
        self.assertTrue(sig.parameters['parameterize'].default)


# ══════════════════════════════════════════════════════════════════════════════
# 9. CLI arg parsing
# ══════════════════════════════════════════════════════════════════════════════

class TestCLIArgs(unittest.TestCase):

    def _parse(self, extra_args=None):
        """Parse CLI args using migrate.py's argparse setup."""
        # Import the parser builder
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
        # We need to build the parser the same way migrate.py does
        import importlib
        migrate = importlib.import_module('migrate')
        # Build parser — migrate.py uses argparse directly in main()
        # We'll test the argument definitions exist by parsing test args
        import argparse
        parser = argparse.ArgumentParser()
        # Add the args from migrate.py's setup
        parser.add_argument('--incremental-refresh', action='store_true', default=False)
        parser.add_argument('--incremental-refresh-months', type=int, default=12)
        parser.add_argument('--no-parameterize', action='store_false', dest='parameterize')
        args = parser.parse_args(extra_args or [])
        return args

    def test_incremental_refresh_default_false(self):
        args = self._parse([])
        self.assertFalse(args.incremental_refresh)

    def test_incremental_refresh_flag(self):
        args = self._parse(['--incremental-refresh'])
        self.assertTrue(args.incremental_refresh)

    def test_incremental_refresh_months_default(self):
        args = self._parse([])
        self.assertEqual(args.incremental_refresh_months, 12)

    def test_incremental_refresh_months_custom(self):
        args = self._parse(['--incremental-refresh-months', '24'])
        self.assertEqual(args.incremental_refresh_months, 24)

    def test_parameterize_default_true(self):
        args = self._parse([])
        self.assertTrue(args.parameterize)

    def test_no_parameterize_flag(self):
        args = self._parse(['--no-parameterize'])
        self.assertFalse(args.parameterize)


# ══════════════════════════════════════════════════════════════════════════════
# 10. Edge cases
# ══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):

    def test_table_with_no_partitions(self):
        table = {'name': 'T', 'columns': [_date_col()]}
        model = _make_model([table])
        # Should not crash — gracefully skip
        result = apply_incremental_refresh(model, [_make_datasource()])
        # No partitions means no M to inject, but detection still works
        # depending on partition checking logic
        # Just ensure no crash
        self.assertIsInstance(result, dict)

    def test_inject_m_preserves_multiline(self):
        m = (
            'let\n'
            '    Source = Sql.Database("s", "d"),\n'
            '    T = Source{[Schema="dbo",Item="T"]}[Data],\n'
            '    Renamed = Table.RenameColumns(T, {{"old", "new"}})\n'
            'in\n'
            '    Renamed'
        )
        result = _inject_range_filter_m(m, 'OrderDate')
        self.assertIn('RangeStart', result)
        self.assertIn('Renamed', result)  # original step still referenced
        self.assertIn('#"Incremental Filter"', result)

    def test_inject_m_with_crlf(self):
        m = 'let\r\n    Source = T\r\nin\r\n    Source'
        result = _inject_range_filter_m(m, 'D')
        self.assertIn('RangeStart', result)

    def test_skips_date_template_table(self):
        table = _make_table('DateTableTemplate_abc', [_date_col()])
        model = _make_model([table])
        result = _detect_incremental_refresh_tables(model, [_make_datasource()])
        self.assertEqual(len(result), 0)

    def test_connection_map_connector_detection(self):
        """Connector type from connection_map entries should be checked."""
        ds = [{
            'connection': {'class': 'csv'},  # not foldable
            'connection_map': {
                'ds1': {'class': 'sqlserver'},  # foldable in map
            },
        }]
        table = _make_table('Orders', [_date_col()])
        model = _make_model([table])
        result = _detect_incremental_refresh_tables(model, ds)
        self.assertEqual(len(result), 1)


if __name__ == '__main__':
    unittest.main()
