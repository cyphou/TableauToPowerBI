"""
Tests for standalone Tableau Prep flow (.tfl/.tflx) migration support.

Covers:
- run_standalone_prep() — single file extraction (legacy, still used by --prep flag)
- _migrate_single_prep_flow() — lineage + M + sources output (used in batch)
- TFL routing in _migrate_single_workbook (batch mode)
- TFL routing in _run_single_migration (single mode)
- File validation accepting .tfl/.tflx extensions
- Batch scanner including .tfl/.tflx files
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from tests.prep_flow_fixtures import clean_chain, operation, output, source, write_flow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tableau_export'))

import migrate
import prep_flow_parser  # noqa: F401 - ensure module is loaded so we can patch it

from migrate import (
    _migrate_single_prep_flow,
    _migrate_single_workbook,
    run_prep_flow,
    run_standalone_prep,
)


def _make_tfl_file(directory, filename='flow.tfl'):
    """Create a synthetic, valid Prep JSON flow."""
    return write_flow(directory, clean_chain(), filename)


def _make_tflx_file(directory, filename='flow.tflx'):
    """Create a minimal .tflx archive containing a .tfl file."""
    return write_flow(directory, clean_chain(), filename, member='flow.tfl')


def _hyper_flow():
    hyper = source('out', name='Extract')
    hyper['nodeType'] = '.v1.LoadHyper'
    hyper['connectionAttributes'] = {
        'class': 'hyper', 'filename': 'extract.hyper', 'table': 'Extract',
    }
    return {'nodes': {'hyper': hyper, 'out': output('PublishedSales')}}


def _script_prediction_flow():
    return {'nodes': {
        'input': source('script'),
        'script': operation(
            'RunScript', 'prediction', name='PythonStep', language='Python'),
        'prediction': operation('Prediction', 'out', name='ModelScore'),
        'out': output(),
    }}


# ── run_standalone_prep tests ────────────────────────────────────────────────

class TestRunStandalonePrep(unittest.TestCase):
    """Test the run_standalone_prep() function."""

    def setUp(self):
        self.extraction = tempfile.TemporaryDirectory()
        self.addCleanup(self.extraction.cleanup)
        patcher = patch('migrate._get_extract_dir', return_value=self.extraction.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_file_not_found(self):
        """Return False when the prep file doesn't exist."""
        result = run_standalone_prep('/nonexistent/flow.tfl')
        self.assertFalse(result)

    def test_successful_parse_writes_json(self):
        """Successful parse writes datasources.json and placeholder JSONs."""
        with tempfile.TemporaryDirectory() as td:
            tfl_path = _make_tfl_file(td)
            fake_ds = [{'name': 'Output',
                        'tables': [{'name': 'Output', 'm_query': 'let Source = 1 in Source'}],
                        'm_query_override': 'let Source = 1 in Source'}]

            with patch('prep_flow_parser.parse_prep_flow', return_value=fake_ds):
                result = run_standalone_prep(tfl_path)

            self.assertTrue(result)

            # datasources.json should contain our fake datasources
            json_dir = self.extraction.name
            ds_path = os.path.join(json_dir, 'datasources.json')
            self.assertTrue(os.path.exists(ds_path))
            with open(ds_path, encoding='utf-8') as f:
                ds = json.load(f)
            self.assertEqual(len(ds), 1)
            self.assertEqual(ds[0]['name'], 'Output')

    def test_explicit_hyper_source_map_is_used_for_single_flow_export(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_flow(td, _hyper_flow())
            source_map = {
                'hyper': {
                    'class': 'sqlserver',
                    'server': 'warehouse.example',
                    'database': 'Sales',
                    'table': 'Orders',
                },
            }
            self.assertTrue(run_standalone_prep(path, source_map=source_map))
            with open(os.path.join(self.extraction.name, 'datasources.json'),
                      encoding='utf-8') as handle:
                datasources = json.load(handle)
            output_ds = next(ds for ds in datasources if ds['prep_role'] == 'output')
            self.assertIn('Sql.Database(\"warehouse.example\", \"Sales\")',
                          output_ds['m_query_override'])
            self.assertNotIn('extract.hyper', output_ds['m_query_override'])

    def test_placeholder_files_created(self):
        """All 16 placeholder JSON files are created."""
        with tempfile.TemporaryDirectory() as td:
            tfl_path = _make_tfl_file(td)
            self.assertTrue(run_standalone_prep(tfl_path))

            json_dir = self.extraction.name
            expected_files = [
                'worksheets.json', 'calculations.json', 'parameters.json',
                'filters.json', 'stories.json', 'actions.json', 'sets.json',
                'groups.json', 'bins.json', 'hierarchies.json', 'sort_orders.json',
                'aliases.json', 'custom_sql.json', 'user_filters.json',
                'hyper_files.json', 'dashboards.json',
            ]
            for fname in expected_files:
                fpath = os.path.join(json_dir, fname)
                self.assertTrue(os.path.exists(fpath), f"Missing: {fname}")

    def test_parse_error_returns_false(self):
        """Return False when parse_prep_flow raises an exception."""
        with tempfile.TemporaryDirectory() as td:
            tfl_path = _make_tfl_file(td)
            with patch('prep_flow_parser.parse_prep_flow', side_effect=OSError("parse error")):
                result = run_standalone_prep(tfl_path)
            self.assertFalse(result)

    def test_empty_conversion_returns_false_without_completed_output(self):
        with tempfile.TemporaryDirectory() as td:
            path = _make_tfl_file(td)
            captured = io.StringIO()
            with patch('prep_flow_parser.parse_prep_flow', return_value=[]), \
                 redirect_stdout(captured):
                result = run_standalone_prep(path)
            self.assertFalse(result)
            self.assertFalse(os.path.exists(os.path.join(self.extraction.name, 'datasources.json')))
            self.assertNotIn('completed successfully', captured.getvalue())


# ── TFL routing in _migrate_single_workbook ──────────────────────────────────

class TestMigrateSingleWorkbookTFL(unittest.TestCase):
    """Test that _migrate_single_workbook routes .tfl/.tflx to _migrate_single_prep_flow."""

    def test_tfl_routes_to_prep_flow_pipeline(self):
        """A .tfl file should call _migrate_single_prep_flow, not run_extraction + run_generation."""
        with tempfile.TemporaryDirectory() as td:
            fake_result = {'success': True, 'report_name': 'my_flow', 'output_dir': td,
                           'prep_flow': True, 'm_query_count': 3, 'source_count': 2, 'grade': 'GREEN',
                           'stats': {'inputs': 2, 'outputs': 1, 'transforms': 3, 'm_queries': 3}}
            with patch('migrate._migrate_single_prep_flow', return_value=fake_result) as mock_prep, \
                 patch('migrate.run_extraction') as mock_extract, \
                 patch('migrate.run_generation') as mock_gen:
                result = _migrate_single_workbook(
                    tableau_file='my_flow.tfl',
                    basename='my_flow',
                    workbook_output_dir=td,
                    display_name='my_flow',
                    skip_extraction=False,
                    wb_prep=None,
                    wb_cal_start=None,
                    wb_cal_end=None,
                    wb_culture=None,
                )
                mock_prep.assert_called_once_with('my_flow.tfl', 'my_flow', td, 'my_flow')
                mock_extract.assert_not_called()
                mock_gen.assert_not_called()
                self.assertTrue(result['success'])
                self.assertTrue(result.get('prep_flow'))

    def test_tflx_routes_to_prep_flow_pipeline(self):
        """A .tflx file should call _migrate_single_prep_flow, not run_extraction + run_generation."""
        with tempfile.TemporaryDirectory() as td:
            fake_result = {'success': True, 'report_name': 'archive', 'output_dir': td,
                           'prep_flow': True, 'm_query_count': 2, 'source_count': 1, 'grade': 'GREEN',
                           'stats': {'inputs': 1, 'outputs': 1, 'transforms': 2, 'm_queries': 2}}
            with patch('migrate._migrate_single_prep_flow', return_value=fake_result) as mock_prep, \
                 patch('migrate.run_extraction') as mock_extract, \
                 patch('migrate.run_generation') as mock_gen:
                result = _migrate_single_workbook(
                    tableau_file='archive.tflx',
                    basename='archive',
                    workbook_output_dir=td,
                    display_name='archive',
                    skip_extraction=False,
                    wb_prep=None,
                    wb_cal_start=None,
                    wb_cal_end=None,
                    wb_culture=None,
                )
                mock_prep.assert_called_once_with('archive.tflx', 'archive', td, 'archive')
                mock_extract.assert_not_called()
                mock_gen.assert_not_called()
                self.assertTrue(result['success'])

    def test_twbx_still_routes_to_extraction(self):
        """A .twbx file should still use run_extraction, not _migrate_single_prep_flow."""
        with tempfile.TemporaryDirectory() as td:
            with patch('migrate._migrate_single_prep_flow') as mock_prep, \
                 patch('migrate.run_extraction', return_value=True) as mock_extract, \
                 patch('migrate.run_generation', return_value=True), \
                 patch('migrate.run_migration_report', return_value={'fidelity_score': 90}), \
                 patch('migrate._run_openability_gate', return_value=True), \
                 patch('powerbi_import.powerbi_internal_lineage.write_powerbi_lineage_artifacts'), \
                 patch('migrate._process_twbx_post_generation'):
                result = _migrate_single_workbook(
                    tableau_file='workbook.twbx',
                    basename='workbook',
                    workbook_output_dir=td,
                    display_name='workbook',
                    skip_extraction=False,
                    wb_prep=None,
                    wb_cal_start=None,
                    wb_cal_end=None,
                    wb_culture=None,
                )
                mock_extract.assert_called_once()
                mock_prep.assert_not_called()
                self.assertTrue(result['success'])

    def test_tfl_prep_flow_failure(self):
        """When _migrate_single_prep_flow fails for .tfl, result should indicate failure."""
        with tempfile.TemporaryDirectory() as td:
            fake_result = {'success': False, 'error': 'analysis', 'report_name': 'bad_flow',
                           'output_dir': td}
            with patch('migrate._migrate_single_prep_flow', return_value=fake_result):
                result = _migrate_single_workbook(
                    tableau_file='bad_flow.tfl',
                    basename='bad_flow',
                    workbook_output_dir=td,
                    display_name='bad_flow',
                    skip_extraction=False,
                    wb_prep=None,
                    wb_cal_start=None,
                    wb_cal_end=None,
                    wb_culture=None,
                )
                self.assertFalse(result['success'])

    def test_tfl_ignores_skip_extraction(self):
        """Standalone .tfl always routes to prep flow pipeline regardless of skip_extraction."""
        with tempfile.TemporaryDirectory() as td:
            fake_result = {'success': True, 'report_name': 'flow', 'output_dir': td,
                           'prep_flow': True, 'm_query_count': 1, 'source_count': 1, 'grade': 'GREEN',
                           'stats': {'inputs': 1, 'outputs': 1, 'transforms': 1, 'm_queries': 1}}
            with patch('migrate._migrate_single_prep_flow', return_value=fake_result) as mock_prep, \
                 patch('migrate.run_generation') as mock_gen:
                result = _migrate_single_workbook(
                    tableau_file='flow.tfl',
                    basename='flow',
                    workbook_output_dir=td,
                    display_name='flow',
                    skip_extraction=False,
                    wb_prep='some_other.tfl',
                    wb_cal_start=None,
                    wb_cal_end=None,
                    wb_culture=None,
                )
                mock_prep.assert_called_once()
                mock_gen.assert_not_called()
                self.assertTrue(result['success'])


# ── _migrate_single_prep_flow tests ──────────────────────────────────────────

class TestMigrateSinglePrepFlow(unittest.TestCase):
    """Test _migrate_single_prep_flow produces lineage + M + sources output."""

    def _make_fake_profile(self, name='TestFlow', num_inputs=2, num_outputs=1,
                           num_transforms=3, m_queries=None):
        """Create a fake FlowProfile-like object for testing."""
        inputs = []
        for i in range(num_inputs):
            inp = SimpleNamespace(
                name=f'Input_{i}', connection_type='csv', server='', database='',
                schema='', table_name=f'table_{i}', filename=f'data_{i}.csv',
                column_count=5, column_names=[f'col_{j}' for j in range(5)],
                fingerprint=f'fp_{i}',
            )
            inputs.append(inp)
        outputs = [SimpleNamespace(name=f'Output_{i}') for i in range(num_outputs)]
        transforms = [SimpleNamespace(name=f'Transform_{i}') for i in range(num_transforms)]
        if m_queries is None:
            m_queries = {f'Output_{i}': 'let\n  Source = Csv.Document(...)\nin\n  Source' for i in range(num_outputs)}
        return SimpleNamespace(
            name=name, inputs=inputs, outputs=outputs, transforms=transforms,
            m_queries=m_queries,
            assessment={'grade': 'GREEN', 'score': 95},
        )

    def test_produces_m_queries_and_sources(self):
        """Successful analysis should write M query files and source JSONs."""
        with tempfile.TemporaryDirectory() as td:
            tfl_path = _make_tfl_file(td)
            profile = self._make_fake_profile()
            with patch('prep_flow_analyzer.analyze_flow', return_value=profile):
                result = _migrate_single_prep_flow(tfl_path, 'flow', td, 'flow')

            self.assertTrue(result['success'])
            self.assertTrue(result.get('prep_flow'))
            self.assertEqual(result['m_query_count'], 1)
            self.assertEqual(result['source_count'], 2)
            self.assertEqual(result['grade'], 'GREEN')

            # Check output files exist
            pq_dir = os.path.join(td, 'flow', 'PowerQuery')
            self.assertTrue(os.path.isdir(pq_dir))
            pq_files = os.listdir(pq_dir)
            self.assertEqual(len(pq_files), 1)

            src_dir = os.path.join(td, 'flow', 'Sources')
            self.assertTrue(os.path.isdir(src_dir))
            src_files = os.listdir(src_dir)
            self.assertEqual(len(src_files), 2)

            # Check assessment.json
            assess_path = os.path.join(td, 'flow', 'assessment.json')
            self.assertTrue(os.path.exists(assess_path))
            with open(assess_path, encoding='utf-8') as f:
                assess = json.load(f)
            self.assertEqual(assess['grade'], 'GREEN')
            self.assertEqual(assess['inputs'], 2)
            self.assertEqual(assess['outputs'], 1)

    def test_no_pbip_generated(self):
        """Prep flow should NOT generate .pbip project files."""
        with tempfile.TemporaryDirectory() as td:
            tfl_path = _make_tfl_file(td)
            profile = self._make_fake_profile()
            with patch('prep_flow_analyzer.analyze_flow', return_value=profile):
                result = _migrate_single_prep_flow(tfl_path, 'flow', td, 'flow')

            self.assertTrue(result['success'])
            # Verify no .pbip file was created
            flow_dir = os.path.join(td, 'flow')
            for root, dirs, files in os.walk(flow_dir):
                for f in files:
                    self.assertFalse(f.endswith('.pbip'),
                                     f"Unexpected .pbip file: {f}")
                    self.assertFalse(f.endswith('.pbir'),
                                     f"Unexpected .pbir file: {f}")
                    self.assertFalse(f.endswith('.tmdl'),
                                     f"Unexpected .tmdl file: {f}")

    def test_analysis_failure(self):
        """Return failure dict when analyze_flow raises."""
        with tempfile.TemporaryDirectory() as td:
            tfl_path = _make_tfl_file(td)
            with patch('prep_flow_analyzer.analyze_flow', side_effect=ValueError("bad flow")):
                result = _migrate_single_prep_flow(tfl_path, 'bad_flow', td, 'bad_flow')

            self.assertFalse(result['success'])
            self.assertEqual(result['error'], 'analysis')

    def test_empty_m_queries_fails_without_completed_output(self):
        """An inventory-only profile cannot be reported as a completed export."""
        with tempfile.TemporaryDirectory() as td:
            tfl_path = _make_tfl_file(td)
            profile = self._make_fake_profile(m_queries={})
            captured = io.StringIO()
            with patch('prep_flow_analyzer.analyze_flow', return_value=profile), \
                 redirect_stdout(captured):
                result = _migrate_single_prep_flow(tfl_path, 'flow', td, 'flow')

            self.assertFalse(result['success'])
            self.assertNotIn('export completed', captured.getvalue())
            self.assertFalse(os.path.exists(os.path.join(td, 'flow', 'assessment.json')))
            self.assertFalse(os.path.exists(os.path.join(td, 'flow', 'PowerQuery')))

    def test_conversion_error_rejects_even_partial_queries(self):
        with tempfile.TemporaryDirectory() as td:
            profile = self._make_fake_profile()
            profile.assessment.update(
                conversion_error='ValueError: unsupported synthetic step',
                grade='RED', status='error',
            )
            captured = io.StringIO()
            with patch('prep_flow_analyzer.analyze_flow', return_value=profile), \
                 redirect_stdout(captured):
                result = _migrate_single_prep_flow(
                    _make_tfl_file(td), 'failed', td, 'failed')
            self.assertFalse(result['success'])
            self.assertNotIn('export completed', captured.getvalue())
            self.assertFalse(os.path.exists(os.path.join(td, 'failed', 'PowerQuery')))
            self.assertFalse(os.path.exists(os.path.join(td, 'failed', 'assessment.json')))

    def test_no_declared_outputs_fails_even_with_queries(self):
        with tempfile.TemporaryDirectory() as td:
            profile = self._make_fake_profile(
                num_outputs=0, m_queries={'Orphan': 'let Source = 1 in Source'})
            captured = io.StringIO()
            with patch('prep_flow_analyzer.analyze_flow', return_value=profile), \
                 redirect_stdout(captured):
                result = _migrate_single_prep_flow(
                    _make_tfl_file(td), 'failed', td, 'failed')
            self.assertFalse(result['success'])
            self.assertNotIn('export completed', captured.getvalue())
            self.assertFalse(os.path.exists(os.path.join(td, 'failed', 'assessment.json')))
            self.assertFalse(os.path.exists(os.path.join(td, 'failed', 'PowerQuery')))

    def test_real_unsupported_conversion_batch_route_cannot_succeed(self):
        with tempfile.TemporaryDirectory() as td:
            flow = {'nodes': {
                'input': source('bad'),
                'bad': operation('SyntheticUnsupported', 'out'),
                'out': output(),
            }}
            captured = io.StringIO()
            with redirect_stdout(captured):
                result = _migrate_single_workbook(
                    write_flow(td, flow), 'failed', td, 'failed',
                    False, None, None, None, None,
                )
            self.assertFalse(result['success'])
            self.assertNotIn('export completed', captured.getvalue())
            self.assertFalse(os.path.exists(os.path.join(td, 'failed', 'PowerQuery')))

    def test_real_container_exports_transformed_query(self):
        with tempfile.TemporaryDirectory() as td:
            result = _migrate_single_prep_flow(_make_tfl_file(td), 'flow', td, 'flow')
            self.assertTrue(result['success'])
            self.assertEqual(result['m_query_count'], 1)
            with open(os.path.join(td, 'flow', 'PowerQuery', 'PublishedSales.pq'),
                      encoding='utf-8') as stream:
                query = stream.read()
            for transform in ['RenameColumns', 'RemoveColumns', 'AddColumn',
                              'TransformColumnTypes', 'SelectRows']:
                self.assertIn('Table.' + transform, query)

    def test_hyper_source_map_exports_original_source_and_green_status(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_flow(td, _hyper_flow())
            mapping_path = os.path.join(td, 'source-map.json')
            with open(mapping_path, 'w', encoding='utf-8') as handle:
                json.dump({
                    'hyper': {
                        'class': 'sqlserver',
                        'server': 'warehouse.example',
                        'database': 'Sales',
                        'schema': 'dbo',
                        'table': 'Orders',
                    },
                }, handle)

            result = _migrate_single_prep_flow(
                path, 'flow', td, 'flow', source_map=mapping_path)
            self.assertTrue(result['success'])
            self.assertEqual(result['grade'], 'GREEN')
            with open(os.path.join(td, 'flow', 'PowerQuery',
                                   'PublishedSales.pq'), encoding='utf-8') as handle:
                query = handle.read()
            self.assertIn('Sql.Database("warehouse.example", "Sales")', query)
            self.assertNotIn('extract.hyper', query)
            with open(os.path.join(td, 'flow', 'Sources',
                                   'Extract.json'), encoding='utf-8') as handle:
                source_definition = json.load(handle)
            self.assertEqual(source_definition['connection_type'], 'sqlserver')

    def test_script_and_prediction_export_as_manual_not_complete(self):
        with tempfile.TemporaryDirectory() as td:
            path = write_flow(td, _script_prediction_flow())
            captured = io.StringIO()
            with redirect_stdout(captured):
                result = _migrate_single_prep_flow(path, 'flow', td, 'flow')
            self.assertTrue(result['success'])
            self.assertEqual(result['grade'], 'YELLOW')
            self.assertIn('manual migration required', captured.getvalue())
            self.assertIn('[OK] Prep flow export completed', captured.getvalue())
            with open(os.path.join(td, 'flow', 'assessment.json'),
                      encoding='utf-8') as handle:
                assessment = json.load(handle)
            self.assertEqual(assessment['grade'], 'YELLOW')
            self.assertEqual(assessment['assessment']['manual_step_count'], 2)

    def test_profile_returned_in_result(self):
        """The prep_profile should be included in the result for cross-flow lineage."""
        with tempfile.TemporaryDirectory() as td:
            tfl_path = _make_tfl_file(td)
            profile = self._make_fake_profile()
            with patch('prep_flow_analyzer.analyze_flow', return_value=profile):
                result = _migrate_single_prep_flow(tfl_path, 'flow', td, 'flow')

            self.assertIs(result['prep_profile'], profile)


class TestRunPrepFlowSourceMap(unittest.TestCase):
    def test_prep_source_map_is_used_for_workbook_integration(self):
        with tempfile.TemporaryDirectory() as td:
            prep_path = write_flow(td, _hyper_flow())
            source_map = {
                'hyper': {
                    'class': 'sqlserver',
                    'server': 'warehouse.example',
                    'database': 'Sales',
                    'table': 'Orders',
                },
            }
            datasource_path = os.path.join(td, 'datasources.json')
            with open(datasource_path, 'w', encoding='utf-8') as handle:
                json.dump([{'name': 'workbook', 'tables': [{'name': 'Sales'}]}], handle)
            captured = {}

            def merge(prep_datasources, twb_datasources, *, return_report=False):
                captured['datasources'] = prep_datasources
                return ([{'name': 'Merged', 'tables': []}], {
                    'matched': [], 'unused_outputs': [], 'unmatched_local': [],
                })

            with patch('prep_flow_parser.merge_prep_with_workbook', side_effect=merge):
                self.assertTrue(run_prep_flow(
                    prep_path, datasource_path, source_map=source_map))

            query = captured['datasources'][0]['m_query_override']
            self.assertIn('Sql.Database("warehouse.example", "Sales")', query)
            self.assertNotIn('extract.hyper', query)

    def test_integrated_script_prediction_remain_visible_manual_placeholders(self):
        with tempfile.TemporaryDirectory() as td:
            prep_path = write_flow(td, _script_prediction_flow())
            datasource_path = os.path.join(td, 'datasources.json')
            with open(datasource_path, 'w', encoding='utf-8') as handle:
                json.dump([{'name': 'workbook', 'tables': [{'name': 'Sales'}]}], handle)
            captured = {}

            def merge(prep_datasources, twb_datasources, *, return_report=False):
                captured['datasources'] = prep_datasources
                return ([{'name': 'Merged', 'tables': []}], {
                    'matched': [], 'unused_outputs': [], 'unmatched_local': [],
                })

            output = io.StringIO()
            with patch('prep_flow_parser.merge_prep_with_workbook', side_effect=merge), \
                 redirect_stdout(output):
                self.assertTrue(run_prep_flow(prep_path, datasource_path))

            query = captured['datasources'][0]['m_query_override']
            self.assertIn('__script_warning', query)
            self.assertIn('__prediction_warning', query)
            self.assertEqual(
                len(captured['datasources'][0]['prep_manual_steps']), 2)
            self.assertIn('manual migration required', output.getvalue())
            self.assertIn('[OK] Prep flow parsing completed successfully',
                          output.getvalue())


class TestPrepSourceMapBatchCli(unittest.TestCase):
    def test_batch_cli_accepts_source_map_option(self):
        with tempfile.TemporaryDirectory() as td:
            mapping_path = os.path.join(td, 'source-map.json')
            with open(mapping_path, 'w', encoding='utf-8') as handle:
                json.dump({}, handle)
            with patch('sys.argv', [
                    'migrate.py', 'batch', td, '--prep-source-map', mapping_path]):
                args = migrate._parse_cli_args(migrate._build_argument_parser())
        self.assertEqual(args.prep_source_map, mapping_path)

    def test_batch_standalone_flow_receives_source_map(self):
        with tempfile.TemporaryDirectory() as td:
            batch_dir = os.path.join(td, 'flows')
            output_dir = os.path.join(td, 'output')
            os.makedirs(batch_dir)
            path = write_flow(batch_dir, _hyper_flow())
            mapping_path = os.path.join(td, 'source-map.json')
            with open(mapping_path, 'w', encoding='utf-8') as handle:
                json.dump({
                    'hyper': {
                        'class': 'sqlserver',
                        'server': 'warehouse.example',
                        'database': 'Sales',
                        'table': 'Orders',
                    },
                }, handle)

            result = migrate.run_batch_migration(
                batch_dir, output_dir=output_dir, prep_source_map=mapping_path,
                quiet=True)

            self.assertEqual(result, migrate.ExitCode.SUCCESS)
            with open(os.path.join(output_dir, 'synthetic', 'assessment.json'),
                      encoding='utf-8') as handle:
                assessment = json.load(handle)
            self.assertEqual(assessment['grade'], 'GREEN')
            with open(os.path.join(output_dir, 'synthetic', 'PowerQuery',
                                   'PublishedSales.pq'), encoding='utf-8') as handle:
                self.assertIn('Sql.Database("warehouse.example", "Sales")',
                              handle.read())


# ── File validation tests ────────────────────────────────────────────────────

class TestFileValidation(unittest.TestCase):
    """Test that .tfl/.tflx are accepted by file validation."""

    def test_tfl_accepted(self):
        """Verify .tfl extension is in the accepted set."""
        # The validation is inline — we verify by checking that the
        # accepted extensions include .tfl/.tflx
        # This is a structural test, the actual validation is in main()
        import migrate
        from migrate import _build_argument_parser  # noqa: F401
        src = open(migrate.__file__, encoding='utf-8').read()
        self.assertIn('.tfl', src)
        self.assertIn('.tflx', src)


# ── Batch scanner tests ─────────────────────────────────────────────────────

class TestBatchScannerIncludesTFL(unittest.TestCase):
    """Test that batch mode scanner picks up .tfl/.tflx files."""

    def test_batch_finds_tfl_files(self):
        """Batch directory scan should include .tfl files."""
        with tempfile.TemporaryDirectory() as td:
            # Create mixed files
            _make_tfl_file(td, 'flow1.tfl')
            _make_tflx_file(td, 'flow2.tflx')
            with open(os.path.join(td, 'workbook.twbx'), 'w') as f:
                f.write('<workbook/>')
            with open(os.path.join(td, 'unrelated.txt'), 'w') as f:
                f.write('skip')

            # Simulate the batch scanner logic from migrate.py
            tableau_files = []
            for root, dirs, files in os.walk(td):
                for fname in sorted(files):
                    if fname.lower().endswith(('.twb', '.twbx', '.tds', '.tdsx', '.tfl', '.tflx')):
                        tableau_files.append(os.path.join(root, fname))

            basenames = [os.path.basename(f) for f in tableau_files]
            self.assertIn('flow1.tfl', basenames)
            self.assertIn('flow2.tflx', basenames)
            self.assertIn('workbook.twbx', basenames)
            self.assertNotIn('unrelated.txt', basenames)
            self.assertEqual(len(tableau_files), 3)


if __name__ == '__main__':
    unittest.main()
