"""Bug-bash regressions: planned identities, thin layouts, final checkpoints."""
import json
import os
from pathlib import Path
from unittest import mock

import pytest

import migrate
from powerbi_import.desktop_paths import (
    DesktopPathError, path_units, plan_desktop_project_name,
    validate_desktop_project, validate_desktop_tree,
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
        (project / f'{report_name}.SemanticModel').mkdir(exist_ok=True)
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
