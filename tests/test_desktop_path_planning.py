"""Regression tests for pre-write path planning and fatal filesystem errors."""
import builtins
import json
import os
from pathlib import Path
from unittest import mock

import pytest

import migrate
from powerbi_import.desktop_paths import (
    DesktopPathError, plan_desktop_project_name, validate_desktop_project,
    validate_desktop_tree, path_units,
)
from powerbi_import.openability import _check_path_length
from powerbi_import.pbip_generator import PowerBIProjectGenerator
from powerbi_import.tmdl_writers import _write_tmdl_files, _safe_filename


def _root_at(base, length):
    path = str(base)
    while len(path) < length:
        path = os.path.join(path, 'd' * min(18, length - len(path) - 1))
    return path


def _objects():
    # The raw semantic table path is also >260; the physical filename must
    # shrink without changing the logical identity or dropping the table.
    return {
        'datasources': [{
            'name': 'Sample',
            'connection': {'type': 'sqlserver', 'details': {
                'server': 'localhost', 'database': 'Test'}},
            'tables': [{'name': 'Data_' + 'x' * 180, 'columns': [
                {'name': 'Category', 'datatype': 'string'},
                {'name': 'Sales', 'datatype': 'real'},
            ]}],
            'calculations': [], 'relationships': [],
        }],
        'worksheets': [{
            'name': 'Sheet1', 'chart_type': 'clusteredBarChart',
            'fields': [{'name': 'Category', 'shelf': 'rows'},
                       {'name': 'Sales', 'shelf': 'columns'}],
            'formatting': {}, 'mark_encoding': {}, 'filters': [],
        }],
        'dashboards': [], 'calculations': [], 'parameters': [],
    }


def test_name_plan_counts_double_basename_and_is_stable(tmp_path):
    root = _root_at(tmp_path, 120)
    source_name = 'Workbook_' + 'a' * 85
    assert len(os.path.join(root, source_name, source_name + '.Report',
                            'definition', 'pages', 'ReportSection',
                            'visuals', '12345678', 'visual.json')) > 260
    name = migrate._desktop_project_name(source_name, root)
    assert name.startswith('P_')
    assert name == plan_desktop_project_name(root, source_name)
    assert plan_desktop_project_name(root, name) == name
    assert not os.path.exists(root)  # planning never writes (also dry-run safe)
    assert plan_desktop_project_name(tmp_path, 'Orders') == 'Orders'
    assert name != plan_desktop_project_name(root, source_name + 'b')


def test_windows_budget_counts_utf16_units(tmp_path):
    root = _root_at(tmp_path, 120)
    name = 'Orders_' + '\U0001f600' * 30
    assert plan_desktop_project_name(root, name).startswith('P_')
    directory = os.path.join(root, 'Orders.SemanticModel', 'definition', 'tables')
    stem = _safe_filename('\U0001f600' * 100, directory, '.tmdl')
    assert path_units(os.path.join(directory, stem + '.tmdl')) < 260


def test_impossible_root_fails_before_any_generation_write(tmp_path):
    root = _root_at(tmp_path, 210)
    with pytest.raises(DesktopPathError, match='shorter --output-dir'):
        plan_desktop_project_name(root, 'Orders')
    with pytest.raises(DesktopPathError, match='Desktop'):
        PowerBIProjectGenerator(output_dir=root).generate_project('Orders', _objects())
    assert not os.path.exists(root)


@pytest.mark.parametrize('root', [
    '\\\\?\\C:\\output', '\\\\?\\UNC\\server\\share', '\\\\.\\C:\\output',
    '//?/C:/output', 'out\x00put',
])
def test_device_paths_not_used_to_bypass_budget_or_confinement(root):
    with pytest.raises(DesktopPathError):
        plan_desktop_project_name(root, 'Orders')
    with mock.patch('os.makedirs') as mkdir:
        with pytest.raises(DesktopPathError):
            PowerBIProjectGenerator(output_dir=root)
        mkdir.assert_not_called()


@pytest.mark.parametrize('name', ['../escape', '..\\escape', 'C:\\escape', '..'])
def test_name_cannot_escape_output_root(tmp_path, name):
    with pytest.raises(DesktopPathError):
        plan_desktop_project_name(tmp_path, name)


def _assert_real_project(root, name, long_table=True):
    project = os.path.join(root, name)
    manifest = json.loads(Path(project, name + '.pbip').read_text(encoding='utf-8'))
    report = manifest['artifacts'][0]['report']['path']
    assert report == name + '.Report'
    pbir = json.loads(Path(project, report, 'definition.pbir').read_text(encoding='utf-8'))
    model_ref = pbir['datasetReference']['byPath']['path']
    assert model_ref == '../' + name + '.SemanticModel'
    assert Path(project, report, model_ref, 'definition', 'model.tmdl').is_file()
    assert list(Path(project, report).rglob('visual.json'))
    tables = list(Path(project, name + '.SemanticModel', 'definition', 'tables').glob('*.tmdl'))
    assert tables
    if long_table:
        assert any('Data_' + 'x' * 180 in p.read_text(encoding='utf-8') for p in tables)
    for directory, dirs, files in os.walk(project):
        assert all(len(os.path.join(directory, d)) < 248 for d in dirs)
        assert all(len(os.path.join(directory, f)) < 260 for f in files)
    assert _check_path_length(project).ok
    validate_desktop_tree(project)
    return project


def test_real_deep_report_and_semantic_generation(tmp_path):
    root = _root_at(tmp_path, 120)
    name = plan_desktop_project_name(root, 'Workbook_' + 'a' * 85)
    PowerBIProjectGenerator(output_dir=root).generate_project(name, _objects())
    _assert_real_project(root, name)


def test_batch_worker_uses_planned_name_through_real_postprocessing(tmp_path):
    from import_to_powerbi import PowerBIImporter
    root = _root_at(tmp_path, 120)
    source_name = 'Workbook_' + 'a' * 85
    source = tmp_path / (source_name + '.twb')
    source.write_text('<workbook/>', encoding='utf-8')
    # Keep generation, data-folder repair, partition reconciliation, image and
    # coordinate wiring, calendar sync, and M/DAX exports real. Isolate only
    # extraction metadata / source discovery from the repository's own files.
    with mock.patch.object(PowerBIImporter, '_load_converted_objects', return_value=_objects()), \
            mock.patch.object(migrate, '_stage_declared_local_sources'), \
            mock.patch.object(migrate, '_collect_local_data_files'), \
            mock.patch.object(migrate, 'run_migration_report', return_value={}) as report:
        result = migrate._migrate_single_workbook(
            str(source), source_name, root, source_name, True,
            None, None, None, None, verify_open=False)
    assert result['success'], result
    name = plan_desktop_project_name(root, source_name)
    assert result['report_name'] == name
    assert result['metadata_path'] == os.path.join(root, name, 'migration_metadata.json')
    report.assert_called_once_with(report_name=name, output_dir=root)
    project = _assert_real_project(root, name)
    assert list(Path(project, 'PowerQuery').glob('*.pq'))
    assert not os.path.exists(os.path.join(root, source_name))


def test_real_batch_cli_extraction_generation_and_references(tmp_path):
    import shutil
    import subprocess
    import sys
    repository = Path(__file__).resolve().parents[1]
    sources = tmp_path / 'sources'
    sources.mkdir()
    source_name = 'Workbook_' + 'a' * 85
    # SQL/cloud fixture needs no local data or network connection to generate.
    shutil.copyfile(repository / 'examples' / 'tableau_samples' / 'BigQuery_Analytics.twb',
                    sources / (source_name + '.twb'))
    root = _root_at(tmp_path, 120)
    result = subprocess.run(
        [sys.executable, str(repository / 'migrate.py'), '--batch', str(sources),
         '--output-dir', root, '--no-verify-open'],
        cwd=tmp_path, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
        capture_output=True, text=True, encoding='utf-8', timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    name = plan_desktop_project_name(root, source_name)
    _assert_real_project(root, name, long_table=False)
    assert Path(root, '.migration_extract', name + '.twb', 'datasources.json').is_file()
    assert not Path(root, source_name).exists()


@pytest.mark.skipif(os.name != 'nt', reason='Real Windows filesystem regression')
def test_windows_real_over_260_then_native_desktop_safe_generation(tmp_path):
    root = _root_at(tmp_path, 120)
    source_name = 'Workbook_' + 'a' * 85
    raw_report = os.path.join(root, source_name, source_name + '.Report',
                             'definition', 'pages', 'ReportSection',
                             'visuals', '12345678', 'visual.json')
    raw_model = os.path.join(root, source_name, source_name + '.SemanticModel',
                            'definition', 'tables', 'Data_' + 'x' * 180 + '.tmdl')
    # Establish actual >260 filesystem paths, not just mocked lengths.
    # Prefix is ONLY a fixture: production uses ordinary Desktop-safe paths.
    for raw in (raw_report, raw_model):
        assert len(raw) > 260
        extended = '\\\\?\\' + os.path.abspath(raw)
        os.makedirs(os.path.dirname(extended), exist_ok=True)
        with open(extended, 'w', encoding='utf-8') as stream:
            stream.write('{}')
        assert os.path.isfile(extended)
    import shutil
    shutil.rmtree('\\\\?\\' + os.path.join(root, source_name))
    name = migrate._desktop_project_name(source_name, root)
    PowerBIProjectGenerator(output_dir=root).generate_project(name, _objects())
    _assert_real_project(root, name)


def _winerror206():
    exc = OSError(206, 'The filename or extension is too long')
    exc.winerror = 206
    return exc


@pytest.mark.parametrize('failure_target', ['visual.json', '.tmdl'])
def test_injected_winerror206_reaches_cli_no_partial_success(tmp_path, capsys, failure_target):
    # Go through the REAL importer and generator. Mock only extracted input
    # and the failing filesystem write, not the layer that swallowed errors.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'powerbi_import'))
    from import_to_powerbi import PowerBIImporter
    real_open = builtins.open

    def failing_open(file, mode='r', *args, **kwargs):
        path = os.fspath(file)
        if 'w' in mode and (
                (failure_target == 'visual.json' and path.endswith('visual.json'))
                or (failure_target == '.tmdl' and path.endswith('.tmdl')
                    and os.path.basename(os.path.dirname(path)) == 'tables')):
            raise _winerror206()
        return real_open(file, mode, *args, **kwargs)

    # Also ensure a stale entry point from a prior generation is invalidated.
    project = tmp_path / 'Orders'
    project.mkdir()
    (project / 'Orders.pbip').write_text('{}', encoding='utf-8')
    with mock.patch.object(PowerBIImporter, '_load_converted_objects', return_value=_objects()), \
            mock.patch('builtins.open', side_effect=failing_open):
        assert migrate.run_generation('Orders', str(tmp_path)) is False
    assert not (project / 'Orders.pbip').exists()
    text = capsys.readouterr().out
    assert 'generated successfully' not in text
    assert 'IMPORT COMPLETE' not in text
    assert 'Error during generation' in text


def test_standalone_tmdl_filesystem_failure_is_not_an_omitted_table(tmp_path):
    with mock.patch('powerbi_import.tmdl_writers._write_table_tmdl',
                    side_effect=_winerror206()):
        with pytest.raises(OSError) as failure:
            _write_tmdl_files({'model': {'tables': [{'name': 'Orders', 'columns': []}]}},
                              str(tmp_path / 'Orders.SemanticModel'))
    assert failure.value.winerror == 206


def test_postprocessing_path_hook_no_longer_renames_report(tmp_path):
    project = tmp_path / 'Orders'
    (project / 'Orders.Report').mkdir(parents=True)
    assert migrate._shorten_report_artifact_path(str(project), 'Orders') is False
    assert (project / 'Orders.Report').is_dir()
    assert not (project / 'R.Report').exists()
