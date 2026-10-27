"""Bug-bash regressions: planned identities, thin layouts, final checkpoints."""
import builtins
import hashlib
import json
import os
import threading
from contextlib import ExitStack
from pathlib import Path
from unittest import TestCase, mock

import pytest

import migrate
from powerbi_import.desktop_paths import (
    DesktopPathError,
    path_units,
    plan_desktop_project_name,
    validate_desktop_project,
    validate_desktop_tree,
)
from powerbi_import.pbip_generator import PowerBIProjectGenerator
from powerbi_import.thin_report_generator import ThinReportGenerator


def _root_at(base, units):
    root = str(base)
    while path_units(root) < units:
        root = os.path.join(root, 'd' * min(18, units - path_units(root) - 1))
    assert path_units(root) == units
    return root


def _publish(report_name, output_dir, output_format='pbip', **kwargs):
    project = Path(output_dir, report_name)
    project.mkdir(parents=True, exist_ok=True)
    if output_format == 'fabric':
        definition = project / f'{report_name}.SemanticModel' / 'definition'
        definition.mkdir(parents=True, exist_ok=True)
        (definition / 'model.tmdl').write_text(
            'model Model\n\tculture: en-US\n', encoding='utf-8')
    else:
        (project / f'{report_name}.pbip').write_text('{}', encoding='utf-8')
    return True


@pytest.mark.parametrize('output_format', ['pbip', 'fabric'])
@pytest.mark.parametrize('with_checkpoint', [True, False])
def test_batch_resume_uses_generated_identity_without_extract_or_generate(
        tmp_path, monkeypatch, output_format, with_checkpoint):
    # Exercise the real worker and checkpoint, not just a fabricated skip.
    source_dir = tmp_path / 'sources'
    (source_dir / 'nested').mkdir(parents=True)
    name = 'Workbook_' + 'a' * 85 if output_format == 'pbip' else 'Orders'
    (source_dir / 'nested' / f'{name}.twb').write_text('<workbook/>')
    output = _root_at(tmp_path / 'output', 120)
    extract = mock.Mock(return_value=True)
    generate = mock.Mock(side_effect=_publish)
    monkeypatch.setattr(migrate, 'run_extraction', extract)
    monkeypatch.setattr(migrate, 'run_generation', generate)
    monkeypatch.setattr(migrate, 'run_migration_report', mock.Mock(return_value={}))
    monkeypatch.setattr(migrate, 'run_batch_html_dashboard', mock.Mock())
    for hook in (
            '_stage_declared_local_sources', '_fix_twb_data_folder',
            '_collect_local_data_files', '_convert_hyper_to_csv_in_data',
            '_route_spatial_partitions', '_reconcile_csv_partitions',
            '_wire_shapefile_map_coordinates', '_restrict_file_partitions_to_model',
            '_sync_final_calendar_source_expressions', '_export_power_query_files'):
        monkeypatch.setattr(migrate, hook, mock.Mock())
    # No extracted quality report is needed for the resume identity contract.
    from import_to_powerbi import PowerBIImporter
    monkeypatch.setattr(PowerBIImporter, '_load_converted_objects', lambda self: {})
    options = dict(batch_dir=str(source_dir), output_dir=output,
                   output_format=output_format, verify_open=False)
    assert migrate.run_batch_migration(**options) == migrate.ExitCode.SUCCESS
    identity = generate.call_args.kwargs['report_name']
    if output_format == 'pbip':
        assert identity == plan_desktop_project_name(
            os.path.join(output, 'nested'), name)
        assert identity.startswith('P_')
    if not with_checkpoint:
        Path(output, '.migration_batch_state.json').unlink()
    extract.reset_mock()
    generate.reset_mock()
    assert migrate.run_batch_migration(**options, resume=True) == migrate.ExitCode.SUCCESS
    extract.assert_not_called()
    generate.assert_not_called()


@pytest.mark.parametrize('with_checkpoint', [True, False])
def test_batch_resume_retains_safe_legacy_identity(
        tmp_path, monkeypatch, with_checkpoint):
    source_dir = tmp_path / 'sources'
    source_dir.mkdir()
    name = 'Legacy_' + 'a' * 35
    (source_dir / f'{name}.twb').write_text('<workbook/>')
    output = _root_at(tmp_path / 'output', 120)
    planned = plan_desktop_project_name(output, name)
    assert planned != name

    def publish_legacy(**kwargs):
        _publish(name, kwargs['workbook_output_dir'])
        return {'success': True}

    with mock.patch.object(migrate, '_migrate_single_workbook',
                           side_effect=publish_legacy), \
            mock.patch.object(migrate, 'run_batch_html_dashboard'):
        assert migrate.run_batch_migration(
            str(source_dir), output_dir=output) == migrate.ExitCode.SUCCESS
    if not with_checkpoint:
        Path(output, '.migration_batch_state.json').unlink()
    with mock.patch.object(migrate, 'run_extraction') as extract, \
            mock.patch.object(migrate, 'run_generation') as generate:
        assert migrate.run_batch_migration(
            str(source_dir), output_dir=output, resume=True) == migrate.ExitCode.SUCCESS
    extract.assert_not_called()
    generate.assert_not_called()


@pytest.mark.parametrize('live_connection', [None, 'workspace/Shared'])
def test_real_thin_report_at_181_utf16_root(tmp_path, live_connection):
    # Both byPath (shared model) and byConnection (Fabric) reuse the PBIP
    # content generator, without a nested <name>/<name>.Report layout.
    root = _root_at(tmp_path / '\U0001f600', 181)
    with pytest.raises(DesktopPathError):
        validate_desktop_project(root, 'P')
    PowerBIProjectGenerator(root)  # Root alone is valid; no write needed.
    assert not os.path.exists(root)
    objects = {
        'datasources': [], 'dashboards': [],
        'worksheets': [{'name': 'Sheet1', 'chart_type': 'clusteredBarChart',
                        'fields': [], 'formatting': {}, 'mark_encoding': {},
                        'filters': []}],
    }
    report = ThinReportGenerator(
        'Shared', root, live_connection=live_connection).generate_thin_report(
            'Thin', objects)
    validate_desktop_tree(report)
    paths = [p for p in Path(report).rglob('*') if p.is_file()]
    assert any(p.name == 'visual.json' for p in paths)
    assert 250 <= max(path_units(str(p)) for p in paths) < 260
    assert Path(root, 'Thin.pbip').is_file()
    pbir = json.loads(Path(report, 'definition.pbir').read_text(encoding='utf-8'))
    if live_connection:
        assert 'byConnection' in pbir['datasetReference']
    else:
        assert pbir['datasetReference']['byPath']['path'] == '../Shared.SemanticModel'


def test_thin_report_content_failure_does_not_publish_pbip(tmp_path):
    marker = tmp_path / 'Thin.pbip'
    marker.write_text('{}')
    generator = ThinReportGenerator('Shared', str(tmp_path))
    with mock.patch.object(generator, '_generate_report_content',
                           side_effect=DesktopPathError('injected content failure')):
        with pytest.raises(DesktopPathError):
            generator.generate_thin_report('Thin', {})
    assert not marker.exists()


def test_thin_report_final_path_failure_does_not_publish_pbip(tmp_path):
    generator = ThinReportGenerator('Shared', str(tmp_path))
    with mock.patch('powerbi_import.desktop_paths.validate_desktop_tree',
                    side_effect=DesktopPathError('injected final path failure')):
        with pytest.raises(DesktopPathError):
            generator.generate_thin_report('Thin', {})
    assert not (tmp_path / 'Thin.pbip').exists()


@pytest.mark.parametrize('route', ['fabric', 'shared_pbip', 'shared_fabric'])
def test_real_shared_and_fabric_generation_with_deep_thin_report_root(tmp_path, route):
    objects = {
        'datasources': [{
            'name': 'Sales',
            'connection': {'type': 'sqlserver', 'details': {
                'server': 'localhost', 'database': 'Test'}},
            'tables': [{'name': 'Orders', 'columns': [
                {'name': 'Amount', 'datatype': 'real'}]}],
            'calculations': [], 'relationships': [],
        }],
        'worksheets': [{'name': 'Sheet1', 'chart_type': 'clusteredBarChart',
                        'fields': [{'name': 'Amount', 'shelf': 'columns'}],
                        'formatting': {}, 'mark_encoding': {}, 'filters': []}],
        'dashboards': [], 'calculations': [], 'parameters': [],
    }
    output = _root_at(tmp_path, 179)
    if route == 'fabric':
        from powerbi_import.fabric_project_generator import FabricProjectGenerator
        result = FabricProjectGenerator(output).generate_project('F', objects)
        report = result['artifacts']['report']['path']
    else:
        from powerbi_import.import_to_powerbi import PowerBIImporter
        result = PowerBIImporter().import_shared_model(
            model_name='F', all_converted_objects=[objects],
            workbook_names=['R'], output_dir=output, force_merge=True,
            output_format='fabric' if route == 'shared_fabric' else 'pbip')
        report = result['report_paths'][0]
    assert path_units(os.path.dirname(report)) == 181
    assert Path(report).is_dir()
    assert list(Path(report).rglob('visual.json'))
    validate_desktop_tree(report)
    assert Path(report).with_suffix('.pbip').is_file()


@pytest.fixture
def single_case(tmp_path, monkeypatch):
    source = tmp_path / 'Orders.twb'
    source.write_text('<workbook/>')
    output = tmp_path / 'out'
    extract_dir = tmp_path / 'extract'
    extract_dir.mkdir()
    (extract_dir / 'worksheets.json').write_text('[]')
    monkeypatch.setenv('TTPBI_EXTRACT_DIR', str(extract_dir))
    args = migrate._build_argument_parser().parse_args([
        str(source), '--output-dir', str(output), '--resume',
        '--no-verify-open', '--quiet'])
    extract = mock.Mock(return_value=True)
    generate = mock.Mock(side_effect=_publish)
    postprocess = mock.Mock()
    monkeypatch.setattr(migrate, 'run_extraction', extract)
    monkeypatch.setattr(migrate, 'run_generation', generate)
    monkeypatch.setattr(migrate, '_extract_twbx_data_files', postprocess)
    for hook in ('_stage_declared_local_sources', 'run_html_dashboard',
                 '_run_post_generation_reports', '_record_zero_touch'):
        monkeypatch.setattr(migrate, hook, mock.Mock())
    monkeypatch.setattr(migrate, 'run_migration_report', mock.Mock(return_value={}))
    monkeypatch.setattr(migrate, '_run_rollback_gate', mock.Mock(return_value=None))
    verify = mock.Mock(return_value=True)
    monkeypatch.setattr(migrate, '_run_verify_open', verify)
    return (args, output, extract, generate, postprocess, verify)


def _state(output):
    return json.loads((output / '.Orders.migration_checkpoint.json').read_text())


def test_single_failed_postprocessing_is_retried_not_resumed(single_case):
    args, output, extract, generate, postprocess, verify = single_case

    def fail_after_generation(*unused):
        assert _state(output)['stages']['generation']['status'] == 'running'
        assert (output / 'Orders' / 'Orders.pbip').is_file()
        raise OSError('injected postprocessing failure')

    postprocess.side_effect = fail_after_generation
    assert migrate._run_single_migration(args) == migrate.ExitCode.GENERAL_ERROR
    assert _state(output)['stages']['generation']['status'] == 'failed'
    assert not (output / 'Orders' / 'Orders.pbip').exists()
    extract.reset_mock()
    generate.reset_mock()
    postprocess.side_effect = None
    assert migrate._run_single_migration(args) == migrate.ExitCode.SUCCESS
    extract.assert_not_called()  # Extraction snapshot remains useful.
    generate.assert_called_once()
    assert postprocess.call_count == 2
    assert _state(output)['stages']['generation']['postprocessed'] is True
    verify.assert_not_called()
    # A genuinely complete run can now resume both stages.
    generate.reset_mock()
    postprocess.reset_mock()
    assert migrate._run_single_migration(args) == migrate.ExitCode.SUCCESS
    extract.assert_not_called()
    generate.assert_not_called()
    postprocess.assert_not_called()


def test_single_final_path_failure_invalidates_checkpoint_and_marker(
        single_case):
    args, output, extract, generate, postprocess, verify = single_case
    with mock.patch.object(migrate, '_shorten_report_artifact_path',
                           side_effect=DesktopPathError('injected final path failure')):
        assert migrate._run_single_migration(args) == migrate.ExitCode.GENERAL_ERROR
    assert _state(output)['stages']['generation']['status'] == 'failed'
    assert not (output / 'Orders' / 'Orders.pbip').exists()
    generate.reset_mock()
    assert migrate._run_single_migration(args) == migrate.ExitCode.SUCCESS
    generate.assert_called_once()
    verify.assert_not_called()


def test_single_unsafe_resumed_tree_cannot_be_successful(single_case):
    args, output, extract, generate, postprocess, verify = single_case
    assert migrate._run_single_migration(args) == migrate.ExitCode.SUCCESS
    generate.reset_mock()
    with mock.patch('powerbi_import.desktop_paths.validate_desktop_tree',
                    side_effect=DesktopPathError('persistent unsafe output')):
        assert migrate._run_single_migration(args) == migrate.ExitCode.GENERAL_ERROR
    generate.assert_called_once()
    assert _state(output)['stages']['generation']['status'] == 'failed'
    assert not (output / 'Orders' / 'Orders.pbip').exists()
    verify.assert_not_called()


@pytest.mark.parametrize('invalid_output', ['old_checkpoint', 'missing_marker', 'unsafe_tree'])
def test_single_revalidates_completed_output_even_without_verify_open(
        single_case, invalid_output):
    args, output, extract, generate, postprocess, verify = single_case
    assert migrate._run_single_migration(args) == migrate.ExitCode.SUCCESS
    if invalid_output == 'old_checkpoint':
        state = _state(output)
        del state['stages']['generation']['postprocessed']
        (output / '.Orders.migration_checkpoint.json').write_text(json.dumps(state))
    elif invalid_output == 'missing_marker':
        (output / 'Orders' / 'Orders.pbip').unlink()
    extract.reset_mock()
    generate.reset_mock()
    postprocess.reset_mock()
    with mock.patch('powerbi_import.desktop_paths.validate_desktop_tree',
                    side_effect=([DesktopPathError('unsafe resumed output'), None]
                                 if invalid_output == 'unsafe_tree' else None)) as check:
        assert migrate._run_single_migration(args) == migrate.ExitCode.SUCCESS
        if invalid_output == 'unsafe_tree':
            assert check.call_count == 2  # Before reuse AND final check.
    extract.assert_not_called()
    generate.assert_called_once()
    postprocess.assert_called_once()
    verify.assert_not_called()


class TestHyperSingleCheckpointOptions(TestCase):
    """Option changes must not silently reuse a differently exported project."""

    @pytest.fixture(autouse=True)
    def _case(self, single_case):
        (self.args, self.output, self.extract, self.generate,
         self.postprocess, self.verify) = single_case

    def _reset(self):
        for hook in (self.extract, self.generate, self.postprocess, self.verify):
            hook.reset_mock()

    def _run(self):
        self.assertEqual(migrate._run_single_migration(self.args), migrate.ExitCode.SUCCESS)
        state = _state(self.output)
        self.assertEqual(state["stages"]["generation"]["status"], "completed")
        self.assertIs(state["stages"]["generation"]["postprocessed"], True)
        return state["config_signature"]

    def _skip(self):
        self._reset()
        signature = self._run()
        self.extract.assert_not_called()
        self.generate.assert_not_called()
        self.postprocess.assert_not_called()
        return signature

    def test_export_format_cap_zip_limit_and_mode_are_in_checkpoint_signature(self):
        cases = (
            ("hyper_export_format", "auto", "csv"),
            ("hyper_export_max_rows", None, 3),
            ("max_zip_entry_mb", 2, 4),
            ("mode", "auto", "import"),
        )
        for key, before, after in cases:
            with self.subTest(option=key):
                setattr(self.args, key, before)
                old_signature = self._run()
                self.assertEqual(self._skip(), old_signature)
                setattr(self.args, key, after)
                self._reset()
                new_signature = self._run()
                self.assertNotEqual(new_signature, old_signature)
                self.generate.assert_called_once()
                self.postprocess.assert_called_once()
                self.assertEqual(self._skip(), new_signature)

    def test_metadata_sample_limit_is_not_export_cap_and_invalidates_extraction(self):
        self.args.hyper_rows = 2
        self.args.hyper_export_max_rows = 7
        old_signature = self._run()
        self.assertEqual(self.extract.call_args.kwargs["hyper_max_rows"], 2)
        self.assertEqual(self.args.hyper_export_max_rows, 7)
        self._skip()
        self.args.hyper_rows = 3
        self._reset()
        self.assertNotEqual(self._run(), old_signature)
        self.extract.assert_called_once()
        self.assertEqual(self.extract.call_args.kwargs["hyper_max_rows"], 3)
        self.assertEqual(self.args.hyper_export_max_rows, 7)


# ── N3 resilience regressions ────────────────────────────────────────────────

_BATCH_HOOKS = (
    '_stage_declared_local_sources', '_fix_twb_data_folder',
    '_collect_local_data_files', '_convert_hyper_to_csv_in_data',
    '_route_spatial_partitions', '_reconcile_csv_partitions',
    '_wire_shapefile_map_coordinates', '_restrict_file_partitions_to_model',
    '_sync_final_calendar_source_expressions', '_export_power_query_files')


@pytest.fixture
def batch_case(tmp_path, monkeypatch):
    """Real batch worker/checkpoint; only extraction/generation are faked."""
    source_dir = tmp_path / 'sources'
    source_dir.mkdir()
    name = 'Workbook_' + 'a' * 85  # long enough for a planned short identity
    (source_dir / f'{name}.twb').write_text('<workbook/>')
    output = _root_at(tmp_path / 'output', 120)
    extract = mock.Mock(return_value=True)
    generate = mock.Mock(side_effect=_publish)
    monkeypatch.setattr(migrate, 'run_extraction', extract)
    monkeypatch.setattr(migrate, 'run_generation', generate)
    monkeypatch.setattr(migrate, 'run_migration_report', mock.Mock(return_value={}))
    monkeypatch.setattr(migrate, 'run_batch_html_dashboard', mock.Mock())
    for hook in _BATCH_HOOKS:
        monkeypatch.setattr(migrate, hook, mock.Mock())
    from import_to_powerbi import PowerBIImporter
    monkeypatch.setattr(PowerBIImporter, '_load_converted_objects', lambda self: {})
    options = dict(batch_dir=str(source_dir), output_dir=output, verify_open=False)
    planned = plan_desktop_project_name(output, name)
    assert planned != name and planned.startswith('P_')
    marker = Path(output, planned, f'{planned}.pbip')
    state = Path(output, '.migration_batch_state.json')
    return options, extract, generate, marker, state, name


def _entry(state):
    entries = json.loads(state.read_text(encoding='utf-8'))
    assert len(entries) == 1
    return next(iter(entries.values()))


def _publish_structural(report_name, output_dir, output_format='pbip', **kwargs):
    """Small structural baseline, not just a completion marker."""
    _publish(report_name, output_dir, output_format)
    project = Path(output_dir, report_name)
    files = {
        f'{report_name}.SemanticModel/definition/model.tmdl':
            'model Model\n\tculture: en-US\n',
    }
    if output_format == 'fabric':
        files[f'{report_name}.SemanticModel/.platform'] = json.dumps({
            'metadata': {'type': 'SemanticModel', 'displayName': report_name},
        })
    else:
        files.update({
            f'{report_name}.Report/definition/report.json': '{"theme": "Light"}',
            f'{report_name}.Report/definition.pbir': json.dumps({
                'version': '4.0',
                'datasetReference': {
                    'byPath': {'path': f'../{report_name}.SemanticModel'}},
            }),
            'PowerQuery/Orders.pq': 'let Source = 1 in Source\n',
        })
    for relative, content in files.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    return True


class TestBatchArtifactInventory(TestCase):
    """Real batch worker: generated structure, not stat metadata, governs reuse."""

    @pytest.fixture(autouse=True)
    def _case(self, batch_case):
        (self.options, self.extract, self.generate, self.marker,
         self.state, self.name) = batch_case
        self.generate.side_effect = _publish_structural

    def _run(self, resume=False):
        self.assertEqual(
            migrate.run_batch_migration(**self.options, resume=resume),
            migrate.ExitCode.SUCCESS)
        self.assertEqual(_entry(self.state)['status'], 'success')

    def _skip(self):
        before = self.state.read_bytes()
        self.extract.reset_mock()
        self.generate.reset_mock()
        self._run(resume=True)
        self.extract.assert_not_called()
        self.generate.assert_not_called()
        self.assertEqual(self.state.read_bytes(), before)

    def _rerun(self):
        self.extract.reset_mock()
        self.generate.reset_mock()
        self._run(resume=True)
        self.extract.assert_called_once()
        self.generate.assert_called_once()

    def _assert_inventory(self):
        inventory = _entry(self.state)['artifact_inventory']
        self.assertEqual(inventory['project'], self.marker.parent.name)
        self.assertEqual(inventory['marker'], self.marker.name)
        expected = {
            path.relative_to(self.marker.parent).as_posix():
                hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.marker.parent.rglob('*') if path.is_file()
        }
        self.assertEqual(inventory['files'], expected)
        self.assertTrue(expected)
        return inventory

    def _mutate(self, relative, operation):
        self._run()
        before = self._assert_inventory()
        self._skip()  # An unchanged structural baseline must be reusable.
        path = self.marker.parent / relative
        if operation == 'delete':
            self.assertIn(relative, before['files'])
            path.unlink()
        elif operation == 'edit':
            original = path.stat()
            content = path.read_bytes()
            self.assertIn(b'en-US', content)
            path.write_bytes(content.replace(b'en-US', b'fr-FR'))
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
            self.assertEqual(path.stat().st_size, original.st_size)
            self.assertEqual(path.stat().st_mtime_ns, original.st_mtime_ns)
        else:
            self.assertEqual(operation, 'add')
            self.assertNotIn(relative, before['files'])
            path.write_text('let Added = 2 in Added\n', encoding='utf-8')
        self._rerun()
        self._assert_inventory()
        self._skip()

    def test_deleted_report_json_invalidates(self):
        self._mutate(
            f'{self.marker.parent.name}.Report/definition/report.json', 'delete')

    def test_deleted_definition_pbir_invalidates(self):
        self._mutate(
            f'{self.marker.parent.name}.Report/definition.pbir', 'delete')

    def test_same_size_tmdl_edit_with_restored_mtime_invalidates(self):
        self._mutate(
            f'{self.marker.parent.name}.SemanticModel/definition/model.tmdl', 'edit')

    def test_deleted_power_query_invalidates(self):
        self._mutate('PowerQuery/Orders.pq', 'delete')

    def test_added_structural_query_invalidates(self):
        self._mutate('PowerQuery/Added.pq', 'add')

    def _fabric(self, operation):
        self.options['output_format'] = 'fabric'
        self.marker = (
            self.state.parent / self.name / f'{self.name}.SemanticModel')
        self._mutate(f'{self.name}.SemanticModel/definition/model.tmdl', operation)

    def test_fabric_deleted_model_invalidates(self):
        self._fabric('delete')

    def test_fabric_same_size_edit_with_restored_mtime_invalidates(self):
        self._fabric('edit')

    def test_actual_legacy_identity_is_inventoried(self):
        source = Path(self.options['batch_dir'], self.name + '.twb')
        self.name = 'Legacy_' + 'a' * 35
        source.rename(source.with_name(self.name + '.twb'))
        planned = plan_desktop_project_name(self.options['output_dir'], self.name)
        self.assertNotEqual(planned, self.name)

        def publish_legacy(**kwargs):
            kwargs['report_name'] = self.name
            return _publish_structural(**kwargs)

        self.generate.side_effect = publish_legacy
        self.marker = self.state.parent / self.name / f'{self.name}.pbip'
        self._run()
        self.assertEqual(self.generate.call_args.kwargs['report_name'], planned)
        self._assert_inventory()
        self.assertFalse((self.state.parent / planned).exists())
        self._skip()
        model = self.marker.parent / f'{self.name}.SemanticModel/definition/model.tmdl'
        model.unlink()
        self._rerun()
        self._assert_inventory()
        self._skip()

    def test_old_success_without_inventory_regenerates_once(self):
        self._run()
        self._assert_inventory()
        state = json.loads(self.state.read_text(encoding='utf-8'))
        del state[self.name + '.twb']['artifact_inventory']
        self.state.write_text(json.dumps(state), encoding='utf-8')
        self._rerun()
        self._assert_inventory()
        self._skip()

    def test_hash_read_failure_warns_and_never_reuses_success(self):
        self._run()
        self._assert_inventory()
        target = self.marker.parent / (
            f'{self.marker.parent.name}.SemanticModel/definition/model.tmdl')
        real_open = open
        failed_reads = []

        def artifact_open(path, mode='r', *args, **kwargs):
            if Path(path) == target and mode == 'rb':
                failed_reads.append(path)
                raise PermissionError('injected structural artifact read failure')
            return real_open(path, mode, *args, **kwargs)

        # Inject at the hashing module's IO boundary, not the inventory result.
        with mock.patch('powerbi_import.evidence_manifest.open',
                        side_effect=artifact_open, create=True):
            for attempt in range(2):
                with self.subTest(attempt=attempt), \
                        self.assertLogs(migrate.logger, level='WARNING') as logs:
                    self._rerun()
                self.assertTrue(any(
                    'Could not inventory generated artifacts' in line
                    and 'forcing regeneration' in line for line in logs.output))
                self.assertIsNone(_entry(self.state)['artifact_inventory'])
                self.assertTrue(self.marker.is_file())
        self.assertGreaterEqual(len(failed_reads), 3)
        self._rerun()  # A success with no readable inventory is not reusable.
        self._assert_inventory()
        self._skip()

    def test_customer_data_and_desktop_runtime_metadata_do_not_invalidate(self):
        def publish_with_local_state(**kwargs):
            result = _publish_structural(**kwargs)
            project = Path(kwargs['output_dir'], kwargs['report_name'])
            for relative in (
                    'Data/customer.json', 'Data/customer.csv',
                    f'{project.name}.Report/.pbi/localSettings.json',
                    f'{project.name}.SemanticModel/.pbi/cache.abf'):
                path = project / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('original', encoding='utf-8')
            return result

        self.generate.side_effect = publish_with_local_state
        self._run()
        before = _entry(self.state)['artifact_inventory']
        self.assertNotIn('Data/customer.json', before['files'])
        self.assertNotIn('Data/customer.csv', before['files'])
        self.assertFalse(any('/.pbi/' in key for key in before['files']))
        self._skip()
        for path in self.marker.parent.rglob('*'):
            if path.is_file() and (
                    'Data' in path.parts or '.pbi' in path.parts):
                path.write_text('modified', encoding='utf-8')
        (self.marker.parent / 'Data/extra.json').write_text('{}', encoding='utf-8')
        runtime = self.marker.parent / f'{self.marker.parent.name}.Report/.pbi'
        (runtime / 'added.json').write_text('{}', encoding='utf-8')
        self._skip()
        (self.marker.parent / 'Data/customer.csv').unlink()
        (runtime / 'localSettings.json').unlink()
        self._skip()
        self.assertEqual(_entry(self.state)['artifact_inventory'], before)

    def test_standalone_prep_stray_pbip_cannot_be_skipped(self):
        from tests.prep_flow_fixtures import clean_chain, write_flow
        Path(self.options['batch_dir'], self.name + '.twb').unlink()
        source = Path(write_flow(self.options['batch_dir'], clean_chain(), 'Flow.tfl'))
        stray = self.state.parent / source.stem / f'{source.stem}.pbip'
        stray.parent.mkdir(parents=True)
        stray.write_text('{}', encoding='utf-8')
        with mock.patch.object(
                migrate, '_migrate_single_prep_flow',
                wraps=migrate._migrate_single_prep_flow) as prep:
            # No checkpoint: even legacy marker-only resume must not skip Prep.
            self._run(resume=True)
            prep.assert_called_once()
            self.assertTrue(_entry(self.state)['result']['prep_flow'])
            self.assertIsNone(_entry(self.state)['artifact_inventory'])
            prep.reset_mock()
            self._run(resume=True)
            prep.assert_called_once()
            self.extract.assert_not_called()
            self.generate.assert_not_called()
        self.assertTrue(stray.is_file())


class TestEffectiveWorkbookCheckpointConfig(TestCase):
    """Exercise config invalidation through the real, identity-planning worker."""

    @pytest.fixture(autouse=True)
    def _case(self, batch_case, monkeypatch):
        (self.options, self.extract, self.generate, self.marker,
         self.state, self.name) = batch_case
        self.worker = mock.Mock(wraps=migrate._migrate_single_workbook)
        monkeypatch.setattr(migrate, '_migrate_single_workbook', self.worker)
        self.prep = mock.Mock(return_value=True)
        monkeypatch.setattr(migrate, 'run_prep_flow', self.prep)
        # Batch uses this boundary, not _run_verify_open or a Desktop launch.
        self.verify = mock.Mock(return_value=True)
        monkeypatch.setattr(migrate, '_run_openability_gate', self.verify)

    def _run(self, resume=False):
        self.assertEqual(
            migrate.run_batch_migration(**self.options, resume=resume),
            migrate.ExitCode.SUCCESS)
        self.assertTrue(self.marker.is_file())

    def _reset(self):
        for hook in (self.extract, self.generate, self.worker, self.prep, self.verify):
            hook.reset_mock()

    def _rerun(self):
        self._reset()
        self._run(resume=True)
        self.extract.assert_called_once()
        self.generate.assert_called_once()
        self.worker.assert_called_once()
        self.assertEqual(self.generate.call_args.kwargs['report_name'],
                         self.marker.parent.name)
        self.assertEqual(_entry(self.state)['status'], 'success')

    def _skip(self):
        self._reset()
        self._run(resume=True)
        for hook in (self.extract, self.generate, self.worker, self.prep, self.verify):
            hook.assert_not_called()

    def _manifest_change(self, key, before, after, worker_key):
        entry = {'file': self.name + '.twb', key: before}
        self.options['manifest'] = [entry]
        self._run()
        old_signature = _entry(self.state)['config_signature']
        entry[key] = after
        self._rerun()
        self.assertNotEqual(_entry(self.state)['config_signature'], old_signature)
        self.assertEqual(self.worker.call_args.kwargs[worker_key], after)
        self.assertEqual(self.generate.call_args.kwargs[key], after)
        self._skip()

    def test_manifest_effective_culture_change_invalidates(self):
        self._manifest_change('culture', 'en-US', 'fr-FR', 'wb_culture')

    def test_manifest_effective_calendar_start_change_invalidates(self):
        self._manifest_change('calendar_start', 2020, 2021, 'wb_cal_start')

    def test_manifest_effective_calendar_end_change_invalidates(self):
        self._manifest_change('calendar_end', 2025, 2026, 'wb_cal_end')

    def test_hyper_options_and_automatic_mode_invalidate_batch_resume(self):
        cases = (
            ("hyper_export_format", "auto", "csv"),
            ("hyper_export_max_rows", None, 7),
            ("max_zip_entry_mb", 2, 4),
            ("model_mode", "auto", "import"),
        )
        for key, before, after in cases:
            with self.subTest(option=key):
                self.options[key] = before
                self._run()
                signature = _entry(self.state)["config_signature"]
                self._skip()
                self.options[key] = after
                self._rerun()
                self.assertNotEqual(_entry(self.state)["config_signature"], signature)
                self.assertEqual(self.worker.call_args.kwargs[key], after)
                self._skip()

    def test_manifest_hyper_cap_wins_over_global_and_only_effective_changes_invalidate(self):
        entry = {"file": self.name + ".twb", "hyper_export_max_rows": 3}
        self.options.update(hyper_export_max_rows=20, manifest=[entry])
        self._run()
        signature = _entry(self.state)["config_signature"]
        self.assertEqual(self.worker.call_args.kwargs["hyper_export_max_rows"], 3)
        self.assertEqual(
            migrate._convert_hyper_to_csv_in_data.call_args.kwargs["hyper_export_max_rows"], 3,
        )
        self.options["hyper_export_max_rows"] = 50
        self._skip()
        self.assertEqual(_entry(self.state)["config_signature"], signature)
        entry["hyper_export_max_rows"] = 4
        self._rerun()
        self.assertNotEqual(_entry(self.state)["config_signature"], signature)
        self.assertEqual(self.worker.call_args.kwargs["hyper_export_max_rows"], 4)
        self.assertEqual(
            migrate._convert_hyper_to_csv_in_data.call_args.kwargs["hyper_export_max_rows"], 4,
        )
        self._skip()

    def test_parallel_worker_forwards_hyper_options_to_extraction_and_postprocessing(self):
        self.options.update(
            parallel=2, model_mode="import", max_zip_entry_mb=4,
            hyper_export_format="csv", hyper_export_max_rows=7,
        )
        self._run()
        self.worker.assert_called_once()
        self.assertEqual(self.worker.call_args.kwargs["max_zip_entry_mb"], 4)
        self.assertEqual(self.extract.call_args.kwargs["max_zip_entry_mb"], 4)
        self.assertEqual(self.generate.call_args.kwargs["model_mode"], "import")
        post = migrate._convert_hyper_to_csv_in_data.call_args.kwargs
        self.assertEqual(post["hyper_export_format"], "csv")
        self.assertEqual(post["hyper_export_max_rows"], 7)
        signature = _entry(self.state)["config_signature"]
        self.options["parallel"] = None
        self._skip()
        self.assertEqual(_entry(self.state)["config_signature"], signature)

    def test_unchanged_effective_overrides_ignore_changed_globals(self):
        prep = Path(self.options['batch_dir']).parent / 'effective.tfl'
        prep.write_text('{"flow": "effective"}', encoding='utf-8')
        entry = {'file': self.name + '.twb', 'culture': 'fr-FR',
                 'calendar_start': 2020, 'calendar_end': 2025, 'prep': str(prep)}
        self.options.update(
            manifest=[entry], culture='en-US', calendar_start=2000,
            calendar_end=2030, prep_file=str(prep.with_name('unused-old.tfl')))
        self._run()
        self.prep.assert_called_once_with(str(prep))
        for key in ('culture', 'calendar_start', 'calendar_end'):
            self.assertEqual(self.generate.call_args.kwargs[key], entry[key])
        signature = _entry(self.state)['config_signature']
        self.options.update(
            culture='de-DE', calendar_start=1990, calendar_end=2040,
            prep_file=str(prep.with_name('unused-new.tfl')))
        self._skip()
        self.assertEqual(_entry(self.state)['config_signature'], signature)

    def test_unrelated_manifest_change_leaves_other_source_completed(self):
        other = Path(self.options['batch_dir'], 'Other.twb')
        other.write_text('<workbook/>', encoding='utf-8')
        entry = {'file': other.name, 'culture': 'en-US'}
        self.options['manifest'] = [entry]
        self._run()
        self.assertEqual(self.worker.call_count, 2)
        previous = json.loads(self.state.read_text(encoding='utf-8'))
        entry['culture'] = 'fr-FR'
        self._reset()
        self._run(resume=True)
        self.worker.assert_called_once()
        self.extract.assert_called_once()
        self.generate.assert_called_once()
        self.assertEqual(self.worker.call_args.kwargs['tableau_file'], str(other))
        self.assertEqual(self.worker.call_args.kwargs['wb_culture'], 'fr-FR')
        self.assertEqual(self.generate.call_args.kwargs['culture'], 'fr-FR')
        current = json.loads(self.state.read_text(encoding='utf-8'))
        self.assertEqual(current[self.name + '.twb'], previous[self.name + '.twb'])
        self.assertNotEqual(current[other.name]['config_signature'],
                            previous[other.name]['config_signature'])
        self._skip()

    def test_same_path_prep_contents_edit_invalidates(self):
        prep = Path(self.options['batch_dir']).parent / 'flow.tfl'
        prep.write_text('{"version": 1}', encoding='utf-8')
        self.options['manifest'] = [{'file': self.name + '.twb', 'prep': str(prep)}]
        self._run()
        self.prep.assert_called_once_with(str(prep))
        signature = _entry(self.state)['config_signature']
        original_stat = prep.stat()
        # Same path, size and mtime: the content signature must detect the edit.
        prep.write_text('{"version": 2}', encoding='utf-8')
        os.utime(prep, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        self.assertEqual(prep.stat().st_size, original_stat.st_size)
        self._rerun()
        self.prep.assert_called_once_with(str(prep))
        self.assertEqual(self.worker.call_args.kwargs['wb_prep'], str(prep))
        self.assertNotEqual(_entry(self.state)['config_signature'], signature)
        self._skip()

    def test_missing_requested_prep_never_matches_prior_checkpoint(self):
        prep = Path(self.options['batch_dir']).parent / 'missing-later.tfl'
        prep.write_text('{}', encoding='utf-8')
        self.options['prep_file'] = str(prep)
        self._run()
        self.assertIsNotNone(_entry(self.state)['config_signature'])
        prep.unlink()
        # Preflight must reject the missing input before entering the worker,
        # even if the mocked Prep boundary would otherwise return success.
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                self._reset()
                self.assertEqual(
                    migrate.run_batch_migration(**self.options, resume=True),
                    migrate.ExitCode.BATCH_PARTIAL_FAIL)
                for hook in (self.worker, self.extract, self.generate, self.prep):
                    hook.assert_not_called()
                entry = _entry(self.state)
                self.assertEqual(entry['status'], 'failed')
                self.assertIs(entry['result']['success'], False)
                self.assertEqual(entry['result']['error_category'], 'prep_unavailable')
                self.assertIsNone(entry['config_signature'])
                self.assertIsNone(entry['artifact_inventory'])
                self.assertFalse(self.marker.exists())
        prep.write_text('{}', encoding='utf-8')
        self._rerun()
        self.prep.assert_called_once_with(str(prep))
        self._skip()

    def test_quality_policy_change_invalidates_and_reaches_quality_builder(self):
        from import_to_powerbi import PowerBIImporter
        objects = {'datasources': [{'name': 'Orders'}]}
        report = mock.Mock(status='passed')
        with mock.patch.object(PowerBIImporter, '_load_converted_objects',
                               return_value=objects), \
                mock.patch('powerbi_import.migration_quality.build_quality_report',
                           return_value=report) as quality, \
                mock.patch('powerbi_import.evidence_package.write_evidence_package'):
            self.options['quality_policy'] = 'report'
            self._run()
            self.assertEqual(quality.call_args.kwargs['quality_policy'], 'report')
            signature = _entry(self.state)['config_signature']
            quality.reset_mock()
            self.options['quality_policy'] = 'production'
            self._rerun()
            self.assertEqual(self.worker.call_args.kwargs['quality_policy'], 'production')
            quality.assert_called_once_with(
                objects, str(self.marker.parent), self.marker.parent.name,
                source_path=str(Path(self.options['batch_dir'], self.name + '.twb')),
                prep_flow=False, quality_policy='production')
            self.assertNotEqual(_entry(self.state)['config_signature'], signature)
            quality.reset_mock()
            self._skip()
            quality.assert_not_called()

    def test_verify_open_changes_invalidate_and_reach_batch_validation_boundary(self):
        self._run()
        self.verify.assert_not_called()
        initial_signature = _entry(self.state)['config_signature']
        self.options['verify_open'] = True
        self._rerun()
        self.assertIs(self.worker.call_args.kwargs['verify_open'], True)
        self.verify.assert_called_once_with(str(self.marker.parent))
        self.assertEqual(_entry(self.state)['stages']['validation'], 'completed')
        self.assertNotEqual(_entry(self.state)['config_signature'], initial_signature)
        self._skip()
        self.options['verify_open'] = False
        self._rerun()
        self.assertIs(self.worker.call_args.kwargs['verify_open'], False)
        self.verify.assert_not_called()
        self.assertEqual(_entry(self.state)['config_signature'], initial_signature)
        self._skip()

    def test_obsolete_global_config_signature_regenerates_once(self):
        self._run()
        current_signature = _entry(self.state)['config_signature']
        # Exact pre-effective-config payload, with globals matching this worker.
        obsolete = {
            'output_format': 'pbip', 'calendar_start': None, 'calendar_end': None,
            'culture': None, 'model_mode': 'auto', 'skip_extraction': False,
            'prep_file': None,
        }
        obsolete_signature = hashlib.sha256(
            json.dumps(obsolete, sort_keys=True, default=str).encode('utf-8')
        ).hexdigest()
        self.assertNotEqual(obsolete_signature, current_signature)
        state = json.loads(self.state.read_text(encoding='utf-8'))
        state[self.name + '.twb']['config_signature'] = obsolete_signature
        self.state.write_text(json.dumps(state), encoding='utf-8')
        self._rerun()
        self.assertEqual(_entry(self.state)['config_signature'], current_signature)
        self._skip()


_BAD_CHECKPOINTS = {
    'empty': b'',
    'truncated': b'{"Workbook_',
    'truncated_after_key': b'{"a.twb": ',
    'list': b'[]',
    'string': b'"done"',
    'null': b'null',
    'number': b'7',
    'garbage': b'\x00\x01not json',
    'invalid_utf8': b'\xff\xfe\xfa{"a": 1}',
    'empty_object': b'{}',
}


@pytest.fixture
def completed_batch_pair(batch_case):
    """Two real successful workers with reusable structural inventories."""
    options, extract, generate, marker, state, name = batch_case
    Path(options['batch_dir'], 'Healthy.twb').write_text('<workbook/>')
    generate.side_effect = _publish_structural
    assert migrate.run_batch_migration(**options) == migrate.ExitCode.SUCCESS
    baseline = json.loads(state.read_bytes())
    assert set(baseline) == {name + '.twb', 'Healthy.twb'}
    for entry in baseline.values():
        assert entry['status'] == 'success'
        inventory = entry['artifact_inventory']
        project = state.parent / inventory['project']
        assert inventory['files'] == {
            path.relative_to(project).as_posix():
                hashlib.sha256(path.read_bytes()).hexdigest()
            for path in project.rglob('*') if path.is_file()
        }
        assert inventory['files']
    before = state.read_bytes()
    extract.reset_mock()
    generate.reset_mock()
    assert migrate.run_batch_migration(**options, resume=True) == migrate.ExitCode.SUCCESS
    extract.assert_not_called()
    generate.assert_not_called()
    assert state.read_bytes() == before
    return batch_case, baseline


_PRIVATE_CHECKPOINT_PAYLOAD = 'sensitive-checkpoint-payload-DO-NOT-LOG'


@pytest.mark.parametrize('field,value,reason', [
    ('entry', None, 'workbook entry must be an object'),
    ('entry', [_PRIVATE_CHECKPOINT_PAYLOAD], 'workbook entry must be an object'),
    ('entry', _PRIVATE_CHECKPOINT_PAYLOAD, 'workbook entry must be an object'),
    ('entry', 17, 'workbook entry must be an object'),
    ('entry', True, 'workbook entry must be an object'),
    ('result', None, 'result must be an object'),
    ('result', [], 'result must be an object'),
    ('result', [_PRIVATE_CHECKPOINT_PAYLOAD], 'result must be an object'),
    ('result', _PRIVATE_CHECKPOINT_PAYLOAD, 'result must be an object'),
    ('result', 17, 'result must be an object'),
    ('result', True, 'result must be an object'),
    ('report_name', 17, 'report_name must be a nonempty string or null'),
    ('report_name', '', 'report_name must be a nonempty string or null'),
    ('report_name', '../' + _PRIVATE_CHECKPOINT_PAYLOAD,
     'report_name is not a planned or source-basename identity'),
    ('absolute_report_name', None,
     'report_name is not a planned or source-basename identity'),
    ('report_name', _PRIVATE_CHECKPOINT_PAYLOAD,
     'report_name is not a planned or source-basename identity'),
], ids=[
    'entry-null', 'entry-list', 'entry-string', 'entry-number', 'entry-bool',
    'result-null', 'result-empty-list', 'result-truthy-list', 'result-string',
    'result-number', 'result-bool', 'identity-number', 'identity-empty',
    'identity-traversal', 'identity-absolute', 'identity-unexpected',
])
def test_batch_resume_malformed_entry_isolated(
        completed_batch_pair, caplog, field, value, reason):
    case, baseline = completed_batch_pair
    options, extract, generate, marker, state, name = case
    key = name + '.twb'
    entries = json.loads(state.read_bytes())
    if field == 'entry':
        entries[key] = value
    elif field == 'result':
        entries[key]['result'] = value
    else:
        if field == 'absolute_report_name':
            value = str(state.parent.parent / _PRIVATE_CHECKPOINT_PAYLOAD)
            assert os.path.isabs(value)
        entries[key]['result']['report_name'] = value
    state.write_text(json.dumps(entries), encoding='utf-8')
    caplog.clear()
    with caplog.at_level('WARNING'):
        assert migrate.run_batch_migration(**options, resume=True) == migrate.ExitCode.SUCCESS
    extract.assert_called_once()
    generate.assert_called_once()
    assert generate.call_args.kwargs['report_name'] == marker.parent.name
    warnings = [record.getMessage() for record in caplog.records
                if record.levelname == 'WARNING']
    assert any(key in message and reason in message for message in warnings)
    assert _PRIVATE_CHECKPOINT_PAYLOAD not in '\n'.join(warnings)
    repaired = json.loads(state.read_bytes())
    assert repaired['Healthy.twb'] == baseline['Healthy.twb']
    assert repaired[key]['status'] == 'success'
    assert repaired[key]['artifact_inventory'] == baseline[key]['artifact_inventory']

    # The repaired entry and the untouched healthy entry now both skip, without
    # rewriting even checkpoint timestamps/durations.
    before = state.read_bytes()
    extract.reset_mock()
    generate.reset_mock()
    assert migrate.run_batch_migration(**options, resume=True) == migrate.ExitCode.SUCCESS
    extract.assert_not_called()
    generate.assert_not_called()
    assert state.read_bytes() == before


@pytest.mark.parametrize('control', [
    'missing_result', 'missing_report_name', 'null_report_name',
])
def test_batch_resume_optional_identity_preserves_inventory_skip(
        completed_batch_pair, caplog, control):
    case, baseline = completed_batch_pair
    options, extract, generate, marker, state, name = case
    entries = json.loads(state.read_bytes())
    entry = entries[name + '.twb']
    if control == 'missing_result':
        del entry['result']
    elif control == 'missing_report_name':
        del entry['result']['report_name']
    else:
        entry['result']['report_name'] = None
    state.write_text(json.dumps(entries), encoding='utf-8')
    before = state.read_bytes()
    caplog.clear()
    with caplog.at_level('WARNING'):
        assert migrate.run_batch_migration(**options, resume=True) == migrate.ExitCode.SUCCESS
    extract.assert_not_called()
    generate.assert_not_called()
    assert state.read_bytes() == before
    assert json.loads(before)['Healthy.twb'] == baseline['Healthy.twb']
    assert not any('Invalid batch checkpoint' in record.getMessage()
                   for record in caplog.records)


def test_batch_completed_project_never_uses_arbitrary_preferred_directory(
        batch_case, tmp_path):
    options, extract, generate, marker, state, name = batch_case
    external = tmp_path / 'External'
    external.mkdir()
    # Cover both a normal marker inside the decoy and the absolute-identity
    # join result that the unguarded implementation would probe.
    decoy = external / 'External.pbip'
    decoy.write_text('external decoy must not be read', encoding='utf-8')
    absolute_marker = Path(str(external) + '.pbip')
    absolute_marker.write_text('external absolute marker', encoding='utf-8')
    worker = migrate._migrate_single_workbook

    def unexpected_identity(**kwargs):
        result = worker(**kwargs)
        assert result['success']
        return {**result, 'report_name': str(external)}

    from powerbi_import.evidence_manifest import file_sha256
    with mock.patch.object(migrate, '_migrate_single_workbook',
                           side_effect=unexpected_identity), \
            mock.patch.object(migrate.os.path, 'isfile',
                              wraps=os.path.isfile) as isfile, \
            mock.patch.object(migrate.os, 'walk', wraps=os.walk) as walk, \
            mock.patch('powerbi_import.evidence_manifest.file_sha256',
                       wraps=file_sha256) as digest:
        assert migrate.run_batch_migration(**options) == migrate.ExitCode.SUCCESS
    for probe in (isfile, walk, digest):
        assert probe.called
        assert not any(
            os.path.normcase(os.path.abspath(call.args[0])).startswith(
                os.path.normcase(str(external)))
            for call in probe.call_args_list)
    inventory = _entry(state)['artifact_inventory']
    assert inventory['project'] == marker.parent.name
    assert inventory['marker'] == marker.name
    assert inventory['files'] == {
        marker.name: hashlib.sha256(marker.read_bytes()).hexdigest()}
    assert decoy.read_text(encoding='utf-8') == 'external decoy must not be read'
    assert absolute_marker.read_text(encoding='utf-8') == 'external absolute marker'


@pytest.mark.parametrize('kind', sorted(_BAD_CHECKPOINTS))
def test_batch_resume_unusable_checkpoint_with_marker_reruns_worker(
        batch_case, kind):
    options, extract, generate, marker, state, name = batch_case
    assert migrate.run_batch_migration(**options) == migrate.ExitCode.SUCCESS
    assert marker.is_file()
    state.write_bytes(_BAD_CHECKPOINTS[kind])
    extract.reset_mock()
    generate.reset_mock()
    assert migrate.run_batch_migration(**options, resume=True) == migrate.ExitCode.SUCCESS
    # A damaged checkpoint cannot prove completion despite the leftover marker.
    generate.assert_called_once()
    assert _entry(state)['status'] == 'success'


def test_batch_resume_absent_checkpoint_keeps_legacy_marker_skip(batch_case):
    options, extract, generate, marker, state, name = batch_case
    assert migrate.run_batch_migration(**options) == migrate.ExitCode.SUCCESS
    state.unlink()
    extract.reset_mock()
    generate.reset_mock()
    assert migrate.run_batch_migration(**options, resume=True) == migrate.ExitCode.SUCCESS
    extract.assert_not_called()
    generate.assert_not_called()


@pytest.mark.parametrize('failure', [OSError, KeyboardInterrupt])
def test_batch_worker_postprocessing_failure_retracts_planned_pbip_and_retries(
        batch_case, monkeypatch, failure):
    options, extract, generate, marker, state, name = batch_case
    seen = {}

    def fail_after_publish(project_dir, basename):
        # The marker exists at the failure point, under the shortened identity.
        seen['marker_existed'] = marker.is_file()
        seen['basename'] = basename
        raise failure('injected postprocessing failure')

    monkeypatch.setattr(migrate, '_export_power_query_files', fail_after_publish)
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            migrate.run_batch_migration(**options)
    else:
        assert migrate.run_batch_migration(**options) == migrate.ExitCode.BATCH_PARTIAL_FAIL
    assert seen == {'marker_existed': True, 'basename': marker.parent.name}
    assert marker.parent.name != name  # Not the long source-derived name.
    assert not marker.exists()
    assert not Path(options['output_dir'], name, f'{name}.pbip').exists()
    assert _entry(state)['status'] != 'success'

    # Retrying the same identity regenerates it (not skipped as completed).
    monkeypatch.setattr(migrate, '_export_power_query_files', mock.Mock())
    generate.reset_mock()
    assert migrate.run_batch_migration(**options, resume=True) == migrate.ExitCode.SUCCESS
    generate.assert_called_once()
    assert generate.call_args.kwargs['report_name'] == marker.parent.name
    assert marker.is_file()
    assert _entry(state)['status'] == 'success'

    # Only now is the completed output resumable.
    generate.reset_mock()
    assert migrate.run_batch_migration(**options, resume=True) == migrate.ExitCode.SUCCESS
    generate.assert_not_called()


class _CheckpointSaveFault:
    """Intercept only handles/renames belonging to this batch checkpoint."""

    def __init__(self, state, marker, source_name, mode):
        self.state = state
        self.marker = marker
        self.key = source_name + '.twb'
        self.mode = mode
        self.temps = []
        self.handles = set()
        self.fds = set()
        self.before = None
        self.replace_failures = 0
        self.dump_failures = 0

    def __enter__(self):
        self.stack = ExitStack()
        mkstemp = migrate.tempfile.mkstemp
        fdopen = migrate.os.fdopen
        dump = migrate.json.dump
        replace = migrate.os.replace

        def checkpoint_temp(*args, **kwargs):
            fd, path = mkstemp(*args, **kwargs)
            if (kwargs.get('prefix') == '.migration_batch_state_'
                    and Path(path).parent == self.state.parent):
                self.fds.add(fd)
                self.temps.append(Path(path))
            return fd, path

        def checkpoint_handle(fd, *args, **kwargs):
            handle = fdopen(fd, *args, **kwargs)
            if fd in self.fds:
                self.fds.remove(fd)
                self.handles.add(handle)
            return handle

        def checkpoint_dump(value, handle, *args, **kwargs):
            if handle in self.handles:
                if (self.before is None
                        and value.get(self.key, {}).get('status') == 'success'):
                    # Arm only after the real worker published its PBIP, never
                    # on the initial "running" checkpoint.
                    assert self.marker.is_file()
                    self.before = self.state.read_bytes()
                    assert json.loads(self.before)[self.key]['status'] == 'running'
                if self.before is not None and (
                        self.mode == 'partial_dump'
                        or (self.mode == 'interrupt' and self.dump_failures == 0)):
                    handle.write('{"partially_written":')
                    handle.flush()
                    assert self.temps[-1].is_file()
                    assert self.state.read_bytes() == self.before
                    self.dump_failures += 1
                    error = KeyboardInterrupt if self.mode == 'interrupt' else OSError
                    raise error('injected checkpoint dump failure')
            return dump(value, handle, *args, **kwargs)

        def checkpoint_replace(src, dst, *args, **kwargs):
            if (Path(dst) == self.state and Path(src) in self.temps
                    and self.before is not None):
                if (self.mode == 'persistent_replace'
                        or (self.mode == 'transient_replace'
                            and self.replace_failures == 0)):
                    self.replace_failures += 1
                    assert Path(src).is_file()
                    assert self.state.read_bytes() == self.before
                    raise PermissionError('injected checkpoint replace failure')
            return replace(src, dst, *args, **kwargs)

        self.stack.enter_context(mock.patch.object(
            migrate.tempfile, 'mkstemp', side_effect=checkpoint_temp))
        self.stack.enter_context(mock.patch.object(
            migrate.os, 'fdopen', side_effect=checkpoint_handle))
        self.stack.enter_context(mock.patch.object(
            migrate.json, 'dump', side_effect=checkpoint_dump))
        self.stack.enter_context(mock.patch.object(
            migrate.os, 'replace', side_effect=checkpoint_replace))
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)


def _assert_batch_retry_then_skip(options, extract, generate, marker, state):
    extract.reset_mock()
    generate.reset_mock()
    assert migrate.run_batch_migration(**options, resume=True) == migrate.ExitCode.SUCCESS
    extract.assert_called_once()
    generate.assert_called_once()
    assert marker.is_file()
    assert _entry(state)['status'] == 'success'
    extract.reset_mock()
    generate.reset_mock()
    assert migrate.run_batch_migration(**options, resume=True) == migrate.ExitCode.SUCCESS
    extract.assert_not_called()
    generate.assert_not_called()


@pytest.mark.parametrize('mode', [
    'partial_dump', 'persistent_replace', 'transient_replace',
])
def test_batch_success_checkpoint_io_failure_is_atomic_and_resumable(batch_case, mode):
    options, extract, generate, marker, state, name = batch_case
    with _CheckpointSaveFault(state, marker, name, mode) as fault:
        result = migrate.run_batch_migration(**options)
    assert fault.before is not None
    assert fault.temps and not any(path.exists() for path in fault.temps)
    assert not list(state.parent.glob('.migration_batch_state_*.tmp'))
    generate.assert_called_once()
    if mode == 'transient_replace':
        assert fault.replace_failures == 1
        assert result == migrate.ExitCode.SUCCESS
        assert marker.is_file()
        assert _entry(state)['status'] == 'success'
        generate.reset_mock()
        extract.reset_mock()
        assert migrate.run_batch_migration(**options, resume=True) == migrate.ExitCode.SUCCESS
        generate.assert_not_called()
        extract.assert_not_called()
    else:
        assert result == migrate.ExitCode.BATCH_PARTIAL_FAIL
        assert not marker.exists()
        assert state.read_bytes() == fault.before
        assert _entry(state)['status'] == 'running'
        if mode == 'persistent_replace':
            # Three attempts for success, three more for failure recording.
            assert fault.replace_failures == 6
        else:
            assert fault.dump_failures == 2
        _assert_batch_retry_then_skip(options, extract, generate, marker, state)


def test_batch_interrupt_during_success_checkpoint_cleans_temp_and_retries(batch_case):
    options, extract, generate, marker, state, name = batch_case
    with _CheckpointSaveFault(state, marker, name, 'interrupt') as fault:
        with pytest.raises(KeyboardInterrupt, match='injected checkpoint dump failure'):
            migrate.run_batch_migration(**options)
    assert fault.before is not None
    assert fault.dump_failures == 1
    assert state.read_bytes() == fault.before
    assert _entry(state)['status'] == 'running'
    assert fault.temps and not any(path.exists() for path in fault.temps)
    assert not list(state.parent.glob('.migration_batch_state_*.tmp'))
    assert not marker.exists()
    _assert_batch_retry_then_skip(options, extract, generate, marker, state)


def test_parallel_checkpoint_interrupt_cannot_leak_uncommitted_success(
        batch_case, monkeypatch):
    options, extract, generate, marker, state, name = batch_case
    Path(options['batch_dir'], 'Healthy.twb').write_text('<workbook/>')
    healthy_identity = plan_desktop_project_name(options['output_dir'], 'Healthy')
    healthy_marker = state.parent / healthy_identity / f'{healthy_identity}.pbip'
    generated = threading.Barrier(2, timeout=10)
    retracted = threading.Event()
    remove = os.remove

    def publish_together(**kwargs):
        result = _publish(**kwargs)
        generated.wait()
        if kwargs['report_name'] == healthy_identity:
            assert retracted.wait(timeout=10), 'failed worker did not retract marker'
        return result

    def signal_retraction(path, *args, **kwargs):
        result = remove(path, *args, **kwargs)
        if Path(path) == marker:
            retracted.set()
        return result

    generate.side_effect = publish_together
    with monkeypatch.context() as patch:
        patch.setattr(migrate.os, 'remove', signal_retraction)
        with _CheckpointSaveFault(state, marker, name, 'interrupt') as fault:
            with pytest.raises(KeyboardInterrupt, match='injected checkpoint dump failure'):
                migrate.run_batch_migration(**options, parallel=2)

    assert retracted.is_set()
    assert not marker.exists()
    assert healthy_marker.is_file()
    assert fault.dump_failures == 1
    assert fault.temps and not any(path.exists() for path in fault.temps)
    assert not list(state.parent.glob('.migration_batch_state_*.tmp'))
    persisted = json.loads(state.read_bytes())
    assert persisted['Healthy.twb']['status'] == 'success'
    # The healthy worker must not commit the interrupted worker's speculative
    # in-memory success along with its own update.
    interrupted_status = persisted[name + '.twb']['status']

    generate.side_effect = _publish
    extract.reset_mock()
    generate.reset_mock()
    assert migrate.run_batch_migration(**options, resume=True, parallel=2) == migrate.ExitCode.SUCCESS
    extract.assert_called_once()
    generate.assert_called_once()
    assert generate.call_args.kwargs['report_name'] == marker.parent.name
    assert marker.is_file()
    generate.reset_mock()
    extract.reset_mock()
    assert migrate.run_batch_migration(**options, resume=True, parallel=2) == migrate.ExitCode.SUCCESS
    extract.assert_not_called()
    generate.assert_not_called()
    assert interrupted_status != 'success', (
        'healthy checkpoint serialized interrupted worker success after its save failed')


# ── Batch input stability gate ───────────────────────────────────────────────

class _BatchInputGate:
    """Keep the real batch worker/checkpoint; fault only the selected input."""

    def __init__(self, batch_case, monkeypatch, input_kind):
        (self.options, self.extract, self.generate, self.marker,
         self.state, self.name) = batch_case
        self.source = Path(self.options['batch_dir'], self.name + '.twb')
        self.prep_path = self.source.parent.parent / 'requested.tfl'
        self.prep_path.write_bytes(b'{"version": 1}')
        self.options['manifest'] = [
            {'file': self.source.name, 'prep': str(self.prep_path)}]
        self.target = self.source if input_kind == 'source' else self.prep_path
        self.original = self.target.read_bytes()
        self.source_digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.input_kind = input_kind
        self.unreadable = False
        self.denied_reads = []
        self.prep = mock.Mock(return_value=True)
        self.worker = mock.Mock(wraps=migrate._migrate_single_workbook)
        monkeypatch.setattr(migrate, 'run_prep_flow', self.prep)
        monkeypatch.setattr(migrate, '_migrate_single_workbook', self.worker)
        # Do not fail checkpoint, inventory or output reads, or depend on
        # platform-specific chmod behavior. Only migrate's input hash opens fail.
        monkeypatch.setattr(migrate, 'open', self._open, raising=False)

    def _open(self, path, mode='r', *args, **kwargs):
        if (self.unreadable and mode == 'rb'
                and os.path.normcase(os.path.abspath(path))
                == os.path.normcase(str(self.target.resolve()))):
            self.denied_reads.append(Path(path))
            raise PermissionError('injected input hash read failure')
        return builtins.open(path, mode, *args, **kwargs)

    def entry(self):
        return json.loads(self.state.read_bytes())[self.source.name]

    def reset(self):
        for hook in (self.extract, self.generate, self.prep, self.worker):
            hook.reset_mock()

    def run(self, resume=False):
        return migrate.run_batch_migration(**self.options, resume=resume)

    def mutate(self, operation):
        if operation == 'delete':
            self.target.unlink()
        elif operation == 'unreadable':
            self.unreadable = True
        else:
            assert operation == 'change'
            stat = self.target.stat()
            changed = (b'<WORKBOOK/>' if self.input_kind == 'source'
                       else b'{"version": 2}')
            assert len(changed) == len(self.original)
            self.target.write_bytes(changed)
            os.utime(self.target, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    def assert_failed(self, operation, during):
        entry = self.entry()
        category = (
            ('source_changed' if self.input_kind == 'source' else 'config_changed')
            if operation == 'change' else self.input_kind + '_unavailable')
        assert entry['status'] == 'failed'
        assert entry['result']['success'] is False
        assert entry['result']['error_category'] == category
        assert entry['error'] == entry['result']['error']
        assert ('during migration' if during else 'before migration') in entry['error']
        assert entry['artifact_inventory'] is None
        assert not self.marker.exists()
        if during:
            assert entry['stages']['input_validation'] == 'failed'
            assert entry['result']['stages']['input_validation'] == 'failed'
            # Generation itself completed: it is the real final input gate
            # that rejects publication, not an earlier mocked worker failure.
            assert entry['stages']['generation'] == 'completed'
        if operation == 'unreadable':
            assert self.denied_reads
            assert all(path == self.target for path in self.denied_reads)

    def retry_then_skip(self, operation):
        self.unreadable = False
        if operation == 'delete':
            self.target.write_bytes(self.original)
        # Keep changed bytes: the retry must migrate the new, now-stable input.
        self.reset()
        assert self.run(resume=True) == migrate.ExitCode.SUCCESS
        for hook in (self.worker, self.extract, self.generate, self.prep):
            hook.assert_called_once()
        assert self.marker.is_file()
        assert self.entry()['status'] == 'success'
        assert self.entry()['result']['success'] is True
        assert self.entry()['artifact_inventory']['marker'] == self.marker.name
        assert self.entry()['source_signature'] == hashlib.sha256(
            self.source.read_bytes()).hexdigest()
        before = self.state.read_bytes()
        self.reset()
        assert self.run(resume=True) == migrate.ExitCode.SUCCESS
        for hook in (self.worker, self.extract, self.generate, self.prep):
            hook.assert_not_called()
        assert self.state.read_bytes() == before


@pytest.fixture
def input_gate_case(batch_case, monkeypatch, request):
    return _BatchInputGate(batch_case, monkeypatch, request.param)


@pytest.mark.parametrize('input_gate_case,operation,phase', [
    ('source', 'change', 'generation'),
    ('prep', 'change', 'postprocess'),
    ('source', 'delete', 'postprocess'),
    ('prep', 'delete', 'generation'),
    ('source', 'unreadable', 'generation'),
    ('prep', 'unreadable', 'postprocess'),
], indirect=['input_gate_case'])
def test_batch_input_mutation_retracts_publication_and_keeps_captured_signatures(
        input_gate_case, monkeypatch, operation, phase):
    case = input_gate_case
    assert case.run() == migrate.ExitCode.SUCCESS
    baseline = case.entry()
    case.reset()
    captured = []

    def mutate_after_publish(*args, **kwargs):
        if phase == 'generation':
            assert _publish(*args, **kwargs)
        assert case.marker.is_file()
        running = case.entry()
        assert running['status'] == 'running'
        captured.append(running)
        case.mutate(operation)
        return True

    with monkeypatch.context() as patch:
        if phase == 'generation':
            case.generate.side_effect = mutate_after_publish
        else:
            patch.setattr(migrate, '_export_power_query_files', mutate_after_publish)
        assert case.run() == migrate.ExitCode.BATCH_PARTIAL_FAIL
    case.generate.side_effect = _publish
    for hook in (case.worker, case.extract, case.generate, case.prep):
        hook.assert_called_once()
    assert len(captured) == 1
    case.assert_failed(operation, during=True)
    failed = case.entry()
    for key in ('source_signature', 'config_signature'):
        assert failed[key] == captured[0][key] == baseline[key]
    assert failed['source_signature'] == case.source_digest
    if operation == 'change' and case.input_kind == 'source':
        assert failed['source_signature'] != hashlib.sha256(
            case.source.read_bytes()).hexdigest()
    case.retry_then_skip(operation)
    if operation == 'change':
        key = 'source_signature' if case.input_kind == 'source' else 'config_signature'
        assert case.entry()[key] != failed[key]


@pytest.mark.parametrize('input_gate_case,operation', [
    ('source', 'delete'), ('source', 'unreadable'),
    ('prep', 'delete'), ('prep', 'unreadable'),
], indirect=['input_gate_case'])
def test_batch_input_unavailable_at_start_never_enters_worker(
        input_gate_case, operation):
    case = input_gate_case
    assert case.run() == migrate.ExitCode.SUCCESS
    # Discovery must still include a source that disappears before its worker
    # starts. Mutate it from the preceding real sequential worker, not a sleep
    # or a fabricated task result. Prep overrides apply only to the target.
    preceding = case.source.with_name('000_First.twb')
    preceding.write_bytes(b'<workbook/>')
    case.reset()

    def publish_preceding(**kwargs):
        assert kwargs['report_name'] != case.marker.parent.name
        result = _publish(**kwargs)
        case.mutate(operation)
        return result

    case.generate.side_effect = publish_preceding
    assert case.run() == migrate.ExitCode.BATCH_PARTIAL_FAIL
    case.assert_failed(operation, during=False)
    case.worker.assert_called_once()
    assert case.worker.call_args.kwargs['tableau_file'] == str(preceding)
    case.extract.assert_called_once_with(
        str(preceding), output_dir=case.generate.call_args.kwargs['source_dir'],
        max_zip_entry_mb=1024)
    case.generate.assert_called_once()
    case.prep.assert_not_called()
    failed = case.entry()
    if case.input_kind == 'source':
        assert failed['source_signature'] is None
    else:
        assert failed['source_signature'] == case.source_digest
        assert failed['config_signature'] is None
    case.generate.side_effect = _publish
    case.retry_then_skip(operation)


@pytest.mark.parametrize('input_gate_case', ['prep'], indirect=True)
def test_batch_queued_prep_edit_refreshes_signature_before_worker_start(input_gate_case):
    case = input_gate_case
    assert case.run() == migrate.ExitCode.SUCCESS
    old_signature = case.entry()['config_signature']
    preceding = case.source.with_name('000_First.twb')
    preceding.write_bytes(b'<workbook/>')
    case.reset()
    observed = []

    def publish_in_order(**kwargs):
        if not observed:
            assert kwargs['report_name'] != case.marker.parent.name
            case.mutate('change')
        else:
            assert kwargs['report_name'] == case.marker.parent.name
            assert case.entry()['status'] == 'running'
            assert case.entry()['config_signature'] != old_signature
        observed.append(kwargs['report_name'])
        return _publish(**kwargs)

    case.generate.side_effect = publish_in_order
    assert case.run() == migrate.ExitCode.SUCCESS
    assert len(observed) == 2
    assert case.worker.call_count == case.extract.call_count == case.generate.call_count == 2
    case.prep.assert_called_once_with(str(case.prep_path))
    refreshed = case.entry()['config_signature']
    assert refreshed != old_signature
    assert case.entry()['status'] == 'success'
    assert case.entry()['source_signature'] == case.source_digest
    before = case.state.read_bytes()
    case.reset()
    assert case.run(resume=True) == migrate.ExitCode.SUCCESS
    for hook in (case.worker, case.extract, case.generate, case.prep):
        hook.assert_not_called()
    assert case.state.read_bytes() == before


# ── Atomic Prep merge write ──────────────────────────────────────────────────

@pytest.fixture
def prep_case(tmp_path):
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(migrate.__file__), 'tableau_export'))
    import prep_flow_parser  # noqa: F401 - patched below by dotted name
    prep = tmp_path / 'flow.tfl'
    prep.write_text('{}')
    data_dir = tmp_path / 'extract'
    data_dir.mkdir()
    target = data_dir / 'datasources.json'
    original = b'{"keep": "\xc3\xa9 original"}\n'
    # Valid JSON list is required by the loader; keep bytes distinctive.
    original = json.dumps([{'name': 'Orig', 'note': 'é'}],
                          ensure_ascii=False).encode('utf-8')
    target.write_bytes(original)
    merged = [{'name': 'Merged', 'tables': [{'name': 'T' * 5000}]}]
    patches = (
        mock.patch('prep_flow_parser.parse_prep_flow',
                   return_value=[{'name': 'O', 'm_query_override': 'let x = 1 in x'}]),
        mock.patch('prep_flow_parser.merge_prep_with_workbook',
                   return_value=(merged, {'matched': [], 'unused_outputs': [],
                                          'unmatched_local': []})),
    )
    return prep, target, original, patches


def _entries(directory):
    return sorted(p.name for p in Path(directory).iterdir())


def test_prep_partial_json_write_preserves_existing_datasources(prep_case):
    prep, target, original, (parse, merge) = prep_case
    real_dump = json.dump

    def partial_dump(obj, stream, **kwargs):
        stream.write(json.dumps(obj, **kwargs)[:100])
        stream.flush()
        raise OSError(28, 'No space left on device')

    with parse, merge, mock.patch('json.dump', partial_dump):
        assert migrate.run_prep_flow(str(prep), str(target)) is False
    assert target.read_bytes() == original
    assert _entries(target.parent) == ['datasources.json']
    assert json.dump is real_dump


def test_prep_replace_failure_preserves_existing_datasources(prep_case):
    prep, target, original, (parse, merge) = prep_case
    real_replace = os.replace
    attempts = []

    def failing_replace(src, dst, *args, **kwargs):
        if os.path.abspath(dst) == os.path.abspath(target):
            attempts.append(src)
            # The staged file is complete here; the swap itself fails.
            assert json.loads(Path(src).read_text(encoding='utf-8'))[0]['name'] == 'Merged'
            raise PermissionError(13, 'datasource file is locked')
        return real_replace(src, dst, *args, **kwargs)

    with parse, merge, mock.patch('os.replace', failing_replace):
        assert migrate.run_prep_flow(str(prep), str(target)) is False
    assert len(attempts) == 1
    assert target.read_bytes() == original
    assert _entries(target.parent) == ['datasources.json']


def test_prep_successful_write_replaces_datasources_without_leftover(prep_case):
    prep, target, original, (parse, merge) = prep_case
    with parse, merge:
        assert migrate.run_prep_flow(str(prep), str(target)) is True
    assert json.loads(target.read_text(encoding='utf-8'))[0]['name'] == 'Merged'
    assert _entries(target.parent) == ['datasources.json']
