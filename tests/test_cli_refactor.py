"""Tests for Sprint 45 — CLI Refactoring & Function Decomposition.

Validates that the refactored helper functions extracted from large
monolithic functions (main, _build_argument_parser, run_batch_migration,
import_shared_model, _build_visual_query) behave correctly.
"""

import argparse
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'powerbi_import'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))


# ── _build_argument_parser helper tests ──────────────────────────────────────

class TestArgumentParserHelpers(unittest.TestCase):
    """Tests for the 9 _add_*_args helpers and the dispatcher."""

    def _get_parser(self):
        import migrate
        return migrate._build_argument_parser()

    def test_parser_returns_argparse_parser(self):
        parser = self._get_parser()
        self.assertIsInstance(parser, argparse.ArgumentParser)

    def test_source_args_present(self):
        parser = self._get_parser()
        args = parser.parse_args(['test.twbx'])
        self.assertEqual(args.tableau_file, 'test.twbx')

    def test_output_args_present(self):
        parser = self._get_parser()
        args = parser.parse_args(['test.twbx', '--output-dir', '/tmp/out', '--verbose'])
        self.assertEqual(args.output_dir, '/tmp/out')
        self.assertTrue(args.verbose)

    def test_batch_args_present(self):
        parser = self._get_parser()
        args = parser.parse_args(['--batch', '/tmp/dir'])
        self.assertEqual(args.batch, '/tmp/dir')

    def test_migration_args_present(self):
        parser = self._get_parser()
        args = parser.parse_args(['test.twbx', '--dry-run', '--culture', 'fr-FR'])
        self.assertTrue(args.dry_run)
        self.assertEqual(args.culture, 'fr-FR')

    def test_calendar_args(self):
        parser = self._get_parser()
        args = parser.parse_args(['test.twbx', '--calendar-start', '2018',
                                  '--calendar-end', '2028'])
        self.assertEqual(args.calendar_start, 2018)
        self.assertEqual(args.calendar_end, 2028)

    def test_report_args_present(self):
        parser = self._get_parser()
        args = parser.parse_args(['test.twbx', '--compare', '--dashboard'])
        self.assertTrue(args.compare)
        self.assertTrue(args.dashboard)

    def test_static_openability_is_enabled_by_default(self):
        parser = self._get_parser()
        args = parser.parse_args(['test.twbx'])
        self.assertTrue(args.verify_open)
        self.assertFalse(args.desktop_probe)

    def test_static_openability_can_be_disabled_explicitly(self):
        parser = self._get_parser()
        args = parser.parse_args(['test.twbx', '--no-verify-open'])
        self.assertFalse(args.verify_open)
        self.assertFalse(args.desktop_probe)

    def test_desktop_probe_remains_explicit(self):
        parser = self._get_parser()
        args = parser.parse_args(['test.twbx', '--desktop-probe'])
        self.assertTrue(args.verify_open)
        self.assertTrue(args.desktop_probe)

    def test_deploy_args_present(self):
        parser = self._get_parser()
        args = parser.parse_args(['test.twbx', '--deploy', 'WS123'])
        self.assertEqual(args.deploy, 'WS123')

    def test_server_args_present(self):
        parser = self._get_parser()
        args = parser.parse_args(['--server', 'https://tab.example.com',
                                  '--workbook', 'Sales'])
        self.assertEqual(args.server, 'https://tab.example.com')
        self.assertEqual(args.workbook, 'Sales')

    def test_enterprise_args_present(self):
        parser = self._get_parser()
        args = parser.parse_args(['test.twbx', '--parallel', '4', '--resume'])
        self.assertEqual(args.parallel, 4)
        self.assertTrue(args.resume)

    def test_shared_model_args_present(self):
        parser = self._get_parser()
        args = parser.parse_args(['--shared-model', 'a.twbx', 'b.twbx'])
        self.assertEqual(args.shared_model, ['a.twbx', 'b.twbx'])

    def test_all_groups_registered(self):
        """Every expected argument should parse without error."""
        parser = self._get_parser()
        args = parser.parse_args([
            'test.twbx', '--prep', 'flow.tfl', '--output-dir', '/tmp',
            '--verbose', '--dry-run', '--culture', 'en-US',
            '--calendar-start', '2020', '--calendar-end', '2030',
            '--compare', '--deploy', 'WS1', '--parallel', '2',
        ])
        self.assertEqual(args.tableau_file, 'test.twbx')
        self.assertEqual(args.prep, 'flow.tfl')
        self.assertEqual(args.deploy, 'WS1')
        self.assertEqual(args.parallel, 2)

    def test_simple_commands_are_limited_to_eight(self):
        import migrate
        self.assertEqual(len(migrate._SIMPLE_COMMANDS), 8)

    def test_simple_migrate_command(self):
        import migrate
        args = migrate._parse_cli_args(
            self._get_parser(), ['migrate', 'sales.twbx', '--verbose'])
        self.assertEqual(args.tableau_file, 'sales.twbx')
        self.assertTrue(args.verbose)

    def test_simple_assess_batch_fabric_and_qa_commands(self):
        import migrate
        parser = self._get_parser()

        assess = migrate._parse_cli_args(parser, ['assess', 'sales.twbx'])
        batch = migrate._parse_cli_args(parser, ['batch', 'portfolio'])
        fabric = migrate._parse_cli_args(parser, ['fabric', 'sales.twbx'])
        qa = migrate._parse_cli_args(parser, ['qa', 'sales.twbx'])

        self.assertTrue(assess.assess)
        self.assertEqual(batch.batch, 'portfolio')
        self.assertEqual(fabric.output_format, 'fabric')
        self.assertTrue(qa.qa)

    def test_simple_server_merge_and_deploy_commands(self):
        import migrate
        parser = self._get_parser()

        server = migrate._parse_cli_args(
            parser, ['server', 'https://tableau.example', 'Sales'])
        merge = migrate._parse_cli_args(
            parser, ['merge', 'a.twbx', 'b.twbx', '--model-name', 'Sales'])
        deploy = migrate._parse_cli_args(
            parser, ['deploy', 'sales.twbx', 'workspace-1'])

        self.assertEqual(server.workbook, 'Sales')
        self.assertEqual(merge.shared_model, ['a.twbx', 'b.twbx'])
        self.assertEqual(merge.model_name, 'Sales')
        self.assertEqual(deploy.output_format, 'fabric')
        self.assertEqual(deploy.deploy, 'workspace-1')
        self.assertTrue(deploy.deploy_refresh)

    def test_legacy_flag_cli_remains_compatible(self):
        import migrate
        args = migrate._parse_cli_args(
            self._get_parser(), ['sales.twbx', '--assess'])
        self.assertEqual(args.tableau_file, 'sales.twbx')
        self.assertTrue(args.assess)

    def test_default_help_lists_only_simple_commands(self):
        import migrate
        with patch('sys.stdout', new_callable=StringIO) as output, \
                self.assertRaises(SystemExit) as exit_context:
            migrate._parse_cli_args(self._get_parser(), ['--help'])

        self.assertEqual(exit_context.exception.code, 0)
        self.assertIn('8 simple commands', output.getvalue())
        self.assertIn('deploy WORKBOOK WORKSPACE_ID', output.getvalue())
        self.assertNotIn('--server-preserve-folders', output.getvalue())

    def test_simple_command_help_does_not_require_arguments(self):
        import migrate
        with patch('sys.stdout', new_callable=StringIO) as output, \
                self.assertRaises(SystemExit) as exit_context:
            migrate._parse_cli_args(self._get_parser(), ['deploy', '--help'])

        self.assertEqual(exit_context.exception.code, 0)
        self.assertIn('deploy WORKBOOK WORKSPACE_ID', output.getvalue())


# ── _run_single_migration helper tests ───────────────────────────────────────

class TestSingleMigrationHelpers(unittest.TestCase):
    """Tests for helpers extracted from main()'s single-file pipeline."""

    def test_fabric_twbx_data_is_extracted_into_fabric_project(self):
        import migrate
        args = argparse.Namespace(
            tableau_file='Retail.twbx',
            output_dir=None,
            output_format='fabric',
        )

        with patch.object(migrate, '_process_twbx_post_generation') as process:
            migrate._extract_twbx_data_files(args, 'Retail')

        process.assert_called_once_with(
            'Retail.twbx',
            os.path.join('artifacts', 'fabric_projects', 'migrated', 'Retail'),
            'Retail',
        )

    def test_fabric_generation_uses_package_imports(self):
        import migrate
        extracted = {'datasources': [{'name': 'Orders'}], 'dashboards': []}
        generated = {
            'project_path': os.path.join('out', 'FabricReport'),
            'artifacts': {'semantic_model': {}},
        }

        with patch(
                'powerbi_import.import_to_powerbi.PowerBIImporter._load_converted_objects',
                return_value=extracted), patch(
                'powerbi_import.fabric_project_generator.FabricProjectGenerator.generate_project',
                return_value=generated) as generate, patch('os.path.exists', return_value=True):
            success = migrate._run_fabric_generation(
                report_name='FabricReport', output_dir='out'
            )

        self.assertTrue(success)
        generate.assert_called_once_with(
            project_name='FabricReport',
            extracted_data=extracted,
            calendar_start=None,
            calendar_end=None,
            culture=None,
            languages=None,
        )

    def test_fabric_output_routes_deployment_to_fabric(self):
        import migrate
        args = argparse.Namespace(output_format='fabric')

        with patch.object(migrate, '_run_deploy_to_fabric',
                          return_value={'success': True}) as fabric_deploy, \
                patch.object(migrate, '_run_deploy_to_pbi_service') as pbi_deploy:
            result = migrate._run_generated_project_deployment(args, 'Sales')

        self.assertEqual(result, {'success': True})
        fabric_deploy.assert_called_once_with(args, 'Sales')
        pbi_deploy.assert_not_called()

    def test_pbip_output_keeps_power_bi_deployment(self):
        import migrate
        args = argparse.Namespace(output_format='pbip')

        with patch.object(migrate, '_run_deploy_to_fabric') as fabric_deploy, \
                patch.object(migrate, '_run_deploy_to_pbi_service',
                             return_value='deployed') as pbi_deploy:
            result = migrate._run_generated_project_deployment(args, 'Sales')

        self.assertEqual(result, 'deployed')
        pbi_deploy.assert_called_once_with(args, 'Sales')
        fabric_deploy.assert_not_called()

    def test_fabric_deploy_refresh_runs_physical_data_pipeline(self):
        import migrate
        args = argparse.Namespace(
            deploy='workspace-1',
            deploy_refresh=True,
            output_dir='fabric-output',
        )
        deployer = MagicMock()
        deployer.deploy_fabric_project.return_value = {
            'success': True,
            'preflight': {
                'workspace_id': 'workspace-1',
                'workspace_name': 'Migration Workspace',
                'accessible_items': 3,
            },
            'artifacts': [],
            'item_ids': {'DataPipeline': 'pipeline-physical'},
        }
        deployer.run_fabric_pipeline.return_value = {
            'id': 'job-1',
            'status': 'Completed',
        }

        with patch(
                'powerbi_import.deploy.deployer.FabricDeployer',
                return_value=deployer):
            result = migrate._run_deploy_to_fabric(args, 'Sales')

        deployer.deploy_fabric_project.assert_called_once_with(
            workspace_id='workspace-1',
            project_dir=os.path.join('fabric-output', 'Sales'),
            project_name='Sales',
        )
        deployer.run_fabric_pipeline.assert_called_once_with(
            'workspace-1', 'pipeline-physical')
        self.assertEqual(result['pipeline_job']['status'], 'Completed')

    def test_fabric_deployment_failure_fails_migration_summary(self):
        import migrate

        results = {
            'extraction': True,
            'generation': True,
            'deployment': False,
        }
        with patch('sys.stdout', new_callable=StringIO):
            success = migrate._print_migration_summary(
                results, {'fidelity_score': 100}, migrate.datetime.now())

        self.assertFalse(success)

    def test_batch_workbook_fails_when_default_openability_gate_fails(self):
        import migrate
        with tempfile.TemporaryDirectory() as output_dir, \
                patch('migrate.run_extraction', return_value=True), \
                patch('migrate.run_generation', return_value=True), \
                patch('migrate.run_migration_report', return_value={}), \
                patch('migrate._fix_twb_data_folder'), \
                patch('migrate._run_openability_gate', return_value=False) as gate:
            result = migrate._migrate_single_workbook(
                tableau_file='test.twb',
                basename='test',
                workbook_output_dir=output_dir,
                display_name='test',
                skip_extraction=False,
                wb_prep=None,
                wb_cal_start=None,
                wb_cal_end=None,
                wb_culture=None,
            )

        self.assertFalse(result['success'])
        gate.assert_called_once_with(os.path.join(output_dir, 'test'))

    def test_batch_workbook_can_explicitly_skip_openability_gate(self):
        import migrate
        with tempfile.TemporaryDirectory() as output_dir, \
                patch('migrate.run_extraction', return_value=True), \
                patch('migrate.run_generation', return_value=True), \
                patch('migrate.run_migration_report', return_value={}), \
                patch('migrate._fix_twb_data_folder'), \
                patch('migrate._run_openability_gate') as gate:
            result = migrate._migrate_single_workbook(
                tableau_file='test.twb',
                basename='test',
                workbook_output_dir=output_dir,
                display_name='test',
                skip_extraction=False,
                wb_prep=None,
                wb_cal_start=None,
                wb_cal_end=None,
                wb_culture=None,
                verify_open=False,
            )

        self.assertTrue(result['success'])
        gate.assert_not_called()

    def test_print_single_migration_header(self):
        import migrate
        args = argparse.Namespace(
            tableau_file='test.twbx', prep=None, output_dir=None,
            dry_run=False, calendar_start=None, calendar_end=None,
            culture=None, mode='import', output_format='pbip',
            rollback=False, telemetry=False,
        )
        with patch('sys.stdout', new_callable=StringIO) as mock_out:
            migrate._print_single_migration_header(args)
            output = mock_out.getvalue()
        self.assertIn('TABLEAU TO POWER BI MIGRATION', output)
        self.assertIn('test.twbx', output)

    def test_print_header_with_options(self):
        import migrate
        args = argparse.Namespace(
            tableau_file='wb.twbx', prep='flow.tfl', output_dir='/tmp/out',
            dry_run=True, calendar_start=2018, calendar_end=2028,
            culture='fr-FR', mode='directquery', output_format='pbix',
            rollback=True, telemetry=True,
        )
        with patch('sys.stdout', new_callable=StringIO) as mock_out:
            migrate._print_single_migration_header(args)
            output = mock_out.getvalue()
        self.assertIn('wb.twbx', output)
        self.assertIn('flow.tfl', output)
        self.assertIn('/tmp/out', output)
        self.assertIn('DRY RUN', output)
        self.assertIn('2018', output)
        self.assertIn('fr-FR', output)
        self.assertIn('directquery', output)
        self.assertIn('Rollback', output)
        self.assertIn('Telemetry', output)

    def test_init_telemetry_disabled(self):
        import migrate
        args = argparse.Namespace(telemetry=False)
        result = migrate._init_telemetry(args)
        self.assertIsNone(result)

    def test_finalize_telemetry_none(self):
        """Finalize with None telemetry should not raise."""
        import migrate
        migrate._finalize_telemetry(None, True, {})  # should not raise


# ── _print_batch_summary tests ──────────────────────────────────────────────

class TestPrintBatchSummary(unittest.TestCase):
    """Tests for _print_batch_summary extracted from run_batch_migration."""

    def test_basic_summary(self):
        import migrate
        results = {
            'Workbook1': {'success': True, 'fidelity': 85,
                          'stats': {'tmdl_tables': 3, 'visuals_generated': 5}},
            'Workbook2': {'success': False, 'fidelity': None,
                          'stats': {}},
        }
        duration = timedelta(seconds=42)
        with patch('sys.stdout', new_callable=StringIO) as mock_out:
            with patch.object(migrate, 'run_batch_html_dashboard', return_value=None):
                succeeded, failed = migrate._print_batch_summary(results, duration, '/tmp/out')
        output = mock_out.getvalue()
        self.assertEqual(succeeded, 1)
        self.assertEqual(failed, 1)
        self.assertIn('Workbook1', output)
        self.assertIn('Workbook2', output)
        self.assertIn('OK', output)
        self.assertIn('FAIL', output)

    def test_all_succeeded(self):
        import migrate
        results = {
            'A': {'success': True, 'fidelity': 90, 'stats': {}},
            'B': {'success': True, 'fidelity': 80, 'stats': {}},
        }
        with patch('sys.stdout', new_callable=StringIO):
            with patch.object(migrate, 'run_batch_html_dashboard', return_value=None):
                s, f = migrate._print_batch_summary(results, timedelta(seconds=10), '/tmp')
        self.assertEqual(s, 2)
        self.assertEqual(f, 0)

    def test_aggregate_fidelity(self):
        import migrate
        results = {
            'A': {'success': True, 'fidelity': 60, 'stats': {}},
            'B': {'success': True, 'fidelity': 100, 'stats': {}},
        }
        with patch('sys.stdout', new_callable=StringIO) as mock_out:
            with patch.object(migrate, 'run_batch_html_dashboard', return_value=None):
                migrate._print_batch_summary(results, timedelta(seconds=5), '/tmp')
        output = mock_out.getvalue()
        self.assertIn('80.0%', output)  # avg of 60 and 100
        self.assertIn('60%', output)    # min
        self.assertIn('100%', output)   # max


# ── _classify_shelf_fields tests ─────────────────────────────────────────────

class TestClassifyShelfFields(unittest.TestCase):
    """Tests for _classify_shelf_fields extracted from _build_visual_query."""

    def _get_generator(self):
        from pbip_generator import PowerBIProjectGenerator
        gen = PowerBIProjectGenerator.__new__(PowerBIProjectGenerator)
        gen._measure_names = set()
        gen._field_map = {}
        return gen

    def test_rows_dims(self):
        gen = self._get_generator()
        fields = [{'name': 'Region', 'shelf': 'rows'}]
        result = gen._classify_shelf_fields(fields)
        self.assertEqual(len(result['rows_dims']), 1)
        self.assertEqual(result['rows_dims'][0]['name'], 'Region')

    def test_measure_value_shelf(self):
        gen = self._get_generator()
        gen._measure_names = {'Sales'}
        fields = [{'name': 'Sales', 'shelf': 'measure_value'}]
        result = gen._classify_shelf_fields(fields)
        self.assertEqual(len(result['expanded_meas']), 1)

    def test_color_dim_vs_meas(self):
        gen = self._get_generator()
        gen._measure_names = {'Profit'}
        fields = [
            {'name': 'Category', 'shelf': 'color'},
            {'name': 'Profit', 'shelf': 'color'},
        ]
        result = gen._classify_shelf_fields(fields)
        self.assertEqual(len(result['color_dims']), 1)
        self.assertEqual(len(result['color_meas']), 1)

    def test_tooltip_and_size(self):
        gen = self._get_generator()
        fields = [
            {'name': 'Info', 'shelf': 'tooltip'},
            {'name': 'Amount', 'shelf': 'size'},
        ]
        result = gen._classify_shelf_fields(fields)
        self.assertEqual(len(result['tooltip_fields']), 1)
        self.assertEqual(len(result['size_fields']), 1)

    def test_no_shelf_fallback(self):
        gen = self._get_generator()
        gen._measure_names = {'Revenue'}
        fields = [
            {'name': 'City', 'shelf': ''},
            {'name': 'Revenue', 'shelf': ''},
        ]
        result = gen._classify_shelf_fields(fields)
        self.assertEqual(len(result['other_dims']), 1)
        self.assertEqual(len(result['other_meas']), 1)

    def test_text_shelf(self):
        gen = self._get_generator()
        fields = [{'name': 'Label', 'shelf': 'text'}]
        result = gen._classify_shelf_fields(fields)
        self.assertEqual(len(result['text_fields']), 1)

    def test_columns_classification(self):
        gen = self._get_generator()
        gen._measure_names = {'Total'}
        fields = [
            {'name': 'Date', 'shelf': 'columns'},
            {'name': 'Total', 'shelf': 'columns'},
        ]
        result = gen._classify_shelf_fields(fields)
        self.assertEqual(len(result['cols_dims']), 1)
        self.assertEqual(len(result['cols_meas']), 1)

    def test_empty_fields(self):
        gen = self._get_generator()
        result = gen._classify_shelf_fields([])
        for key in result:
            self.assertEqual(len(result[key]), 0)

    def test_dim_buckets_strip_aggregation(self):
        """Fields routed to dimension buckets must lose any shelf aggregation.

        Tableau encodes pills like ``sum:Ps Id`` even on the Color shelf;
        Power BI dimension wells (Series/Legend, Category, Group, Rows,
        Columns) reject aggregations and would render ``Sum of Ps Id``.
        """
        gen = self._get_generator()
        # Identifier-style fields with a shelf-side aggregation
        fields = [
            {'name': 'Ps Id', 'shelf': 'color', 'aggregation': 'sum'},
            {'name': 'Region', 'shelf': 'rows', 'aggregation': 'cnt'},
            {'name': 'Code', 'shelf': 'columns', 'aggregation': 'cntd'},
            {'name': 'Bucket', 'shelf': '', 'aggregation': 'sum'},
        ]
        result = gen._classify_shelf_fields(fields)
        self.assertEqual(len(result['color_dims']), 1)
        self.assertEqual(len(result['rows_dims']), 1)
        self.assertEqual(len(result['cols_dims']), 1)
        self.assertEqual(len(result['other_dims']), 1)
        # Aggregation must have been stripped on every dimension bucket
        for bucket in ('color_dims', 'rows_dims', 'cols_dims', 'other_dims'):
            for f in result[bucket]:
                self.assertNotIn(
                    'aggregation', f,
                    f'aggregation must be stripped from {bucket}, got {f}'
                )
        # The original input dicts must not be mutated
        self.assertEqual(fields[0].get('aggregation'), 'sum')
        self.assertEqual(fields[1].get('aggregation'), 'cnt')

    def test_measure_buckets_keep_aggregation(self):
        """Fields routed to measure buckets must retain their aggregation."""
        gen = self._get_generator()
        gen._measure_names = {'Sales', 'Profit'}
        fields = [
            {'name': 'Sales', 'shelf': 'color', 'aggregation': 'sum'},
            {'name': 'Profit', 'shelf': 'rows', 'aggregation': 'avg'},
        ]
        result = gen._classify_shelf_fields(fields)
        self.assertEqual(len(result['color_meas']), 1)
        self.assertEqual(len(result['rows_meas']), 1)
        self.assertEqual(result['color_meas'][0].get('aggregation'), 'sum')
        self.assertEqual(result['rows_meas'][0].get('aggregation'), 'avg')


# ── import_shared_model refactoring tests ────────────────────────────────────

class TestSharedModelHelpers(unittest.TestCase):
    """Tests for helpers extracted from import_shared_model."""

    def test_create_model_explorer_report_creates_files(self):
        import tempfile
        import json
        from import_to_powerbi import PowerBIImporter

        gen = PowerBIImporter.__new__(PowerBIImporter)

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('sys.stdout', new_callable=StringIO):
                gen._create_model_explorer_report(tmpdir, 'TestModel')

            # Check .pbip file
            pbip_path = os.path.join(tmpdir, 'TestModel.pbip')
            self.assertTrue(os.path.exists(pbip_path))
            with open(pbip_path, 'r', encoding='utf-8') as f:
                pbip = json.load(f)
            self.assertIn('artifacts', pbip)

            # Check report directory
            report_dir = os.path.join(tmpdir, 'TestModel_Model.Report')
            self.assertTrue(os.path.isdir(report_dir))

            # Check .platform
            platform = os.path.join(report_dir, '.platform')
            self.assertTrue(os.path.exists(platform))
            with open(platform, 'r', encoding='utf-8') as f:
                plat = json.load(f)
            self.assertEqual(plat['metadata']['type'], 'Report')

            # Check definition.pbir
            pbir = os.path.join(report_dir, 'definition.pbir')
            self.assertTrue(os.path.exists(pbir))
            with open(pbir, 'r', encoding='utf-8') as f:
                pbir_data = json.load(f)
            self.assertIn('byPath', pbir_data['datasetReference'])

            # Check report.json
            report_json = os.path.join(report_dir, 'definition', 'report.json')
            self.assertTrue(os.path.exists(report_json))

    def test_save_shared_model_artifacts(self):
        import tempfile
        import json
        from import_to_powerbi import PowerBIImporter

        gen = PowerBIImporter.__new__(PowerBIImporter)

        # Create minimal assessment mock
        assessment = MagicMock()
        assessment.overall_score = 80
        assessment.recommendation = 'merge'
        assessment.table_overlaps = []
        assessment.column_overlaps = []
        assessment.measure_conflicts = []
        assessment.relationship_conflicts = []
        assessment.workbook_scores = {}
        assessment.isolated_tables = {}
        assessment.to_dict.return_value = {
            'overall_score': 80, 'recommendation': 'merge',
            'table_overlaps': [], 'column_overlaps': [],
            'measure_conflicts': [], 'relationship_conflicts': [],
            'workbook_scores': {}, 'isolated_tables': {},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch('sys.stdout', new_callable=StringIO):
                with patch('powerbi_import.merge_report_html.generate_merge_html_report'):
                    with patch.dict('sys.modules', {'merge_report_html': MagicMock()}):
                        gen._save_shared_model_artifacts(
                            tmpdir, assessment, lineage=None,
                            save_config=False, workbook_names=['wb1'],
                            all_converted_objects=[{}], merged={},
                            model_name='TestModel',
                        )

            # Merge assessment JSON should exist
            assess_path = os.path.join(tmpdir, 'merge_assessment.json')
            self.assertTrue(os.path.exists(assess_path))


# ── _run_single_migration integration test ───────────────────────────────────

class TestRunSingleMigrationExists(unittest.TestCase):
    """Verify _run_single_migration is callable."""

    def test_function_exists(self):
        import migrate
        self.assertTrue(callable(migrate._run_single_migration))

    def test_function_signature(self):
        import inspect
        import migrate
        sig = inspect.signature(migrate._run_single_migration)
        self.assertIn('args', sig.parameters)


if __name__ == '__main__':
    unittest.main()
