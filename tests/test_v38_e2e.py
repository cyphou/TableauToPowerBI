"""Sprint 179 integration tests for v38 hardening.

End-to-end flow coverage (module boundaries):
1. report package generation
2. API batch progress + OpenAPI endpoints
3. artifact diff + baseline helpers from migrate.py
4. post-generation orchestration wiring in migrate.py
"""

import json
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Ensure project root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import migrate
from powerbi_import import api_server
from powerbi_import import report_packager
from powerbi_import.artifact_diff import BASELINE_MANIFEST


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2)


def _build_project(base_dir, name='TestProject', table_tmdl=''):
    """Create a minimal .pbip-style project for diff/baseline tests."""
    project_dir = os.path.join(base_dir, name)

    sem_def = os.path.join(project_dir, f'{name}.SemanticModel', 'definition')
    rep_def = os.path.join(project_dir, f'{name}.Report', 'definition')
    os.makedirs(os.path.join(sem_def, 'tables'), exist_ok=True)
    os.makedirs(os.path.join(rep_def, 'pages', 'p1', 'visuals', 'v1'), exist_ok=True)

    _write(os.path.join(sem_def, 'model.tmdl'), 'model Model\n')
    _write(os.path.join(sem_def, 'database.tmdl'), 'database\n')
    _write(os.path.join(sem_def, 'tables', 'Orders.tmdl'), table_tmdl)
    _write(os.path.join(sem_def, 'relationships.tmdl'), '')
    _write(os.path.join(sem_def, 'roles.tmdl'), '')

    _write_json(os.path.join(rep_def, 'report.json'), {'filterConfig': {'filters': []}})
    _write_json(os.path.join(rep_def, 'pages', 'p1', 'page.json'), {'displayName': 'Page 1'})
    _write_json(
        os.path.join(rep_def, 'pages', 'p1', 'visuals', 'v1', 'visual.json'),
        {'visual': {'visualType': 'table'}},
    )

    return project_dir


SAMPLE_TABLE_V1 = """table 'Orders'

\tcolumn 'Amount'
\t\tdataType: decimal

\tmeasure 'Total Sales' =
\t\texpression = SUM('Orders'[Amount])

\tpartition 'Orders-partition' = m
\t\tmode: import
\t\tsource = let Source = Sql.Database("server", "db") in Source
"""


SAMPLE_TABLE_V2 = """table 'Orders'

\tcolumn 'Amount'
\t\tdataType: decimal

\tmeasure 'Total Sales' =
\t\texpression = CALCULATE(SUM('Orders'[Amount]))

\tpartition 'Orders-partition' = m
\t\tmode: import
\t\tsource = let Source = Sql.Database("server2", "db") in Source
"""


class _AssessmentStub:
    def __init__(self):
        self._data = {
            'workbook_name': 'SalesWorkbook',
            'overall_score': 'GREEN',
            'timestamp': '2026-05-26T00:00:00Z',
            'totals': {'checks': 2, 'pass': 1, 'warn': 1, 'fail': 0},
            'categories': [
                {
                    'name': 'datasource',
                    'checks': [
                        {
                            'name': 'Connector supported',
                            'severity': 'pass',
                            'detail': 'SQL Server',
                            'recommendation': '',
                        },
                        {
                            'name': 'Custom SQL detected',
                            'severity': 'warn',
                            'detail': 'One query',
                            'recommendation': 'Review query folding',
                        },
                    ],
                }
            ],
        }

    def to_dict(self):
        return self._data


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix='v38_e2e_')
    yield d
    shutil.rmtree(d, ignore_errors=True)


class TestV38ReportPackage:
    def test_generate_report_package_creates_zip(self, tmp_dir):
        out = os.path.join(tmp_dir, 'package.zip')
        assessment = _AssessmentStub()

        with patch('powerbi_import.pdf_renderer.render_print_html', return_value='<html>print</html>'):
            with patch('powerbi_import.pptx_report.generate_pptx_report') as mock_pptx:
                def _fake_pptx(_data, output_path, migration_stats=None):
                    with open(output_path, 'wb') as f:
                        f.write(b'PPTX-STUB')

                mock_pptx.side_effect = _fake_pptx
                zip_path = report_packager.generate_report_package(
                    assessment,
                    '<html>interactive</html>',
                    out,
                    migration_stats={'tables': 5},
                )

        assert os.path.isfile(zip_path)

    def test_package_contains_expected_members(self, tmp_dir):
        import zipfile

        out = os.path.join(tmp_dir, 'package.zip')
        assessment = _AssessmentStub()

        with patch('powerbi_import.pdf_renderer.render_print_html', return_value='<html>print</html>'):
            with patch('powerbi_import.pptx_report.generate_pptx_report') as mock_pptx:
                mock_pptx.side_effect = lambda _d, p, migration_stats=None: open(p, 'wb').write(b'PPTX')
                report_packager.generate_report_package(assessment, '<html>x</html>', out)

        with zipfile.ZipFile(out, 'r') as zf:
            names = set(zf.namelist())

        expected = {
            'assessment_report.html',
            'assessment_report.pdf.html',
            'executive_summary.pptx',
            'assessment_data.json',
            'fidelity_checks.csv',
            'README.txt',
        }
        assert expected.issubset(names)

    def test_package_readme_mentions_workbook(self, tmp_dir):
        import zipfile

        out = os.path.join(tmp_dir, 'package.zip')
        assessment = _AssessmentStub()

        with patch('powerbi_import.pdf_renderer.render_print_html', return_value='<html>print</html>'):
            with patch('powerbi_import.pptx_report.generate_pptx_report') as mock_pptx:
                mock_pptx.side_effect = lambda _d, p, migration_stats=None: open(p, 'wb').write(b'PPTX')
                report_packager.generate_report_package(assessment, '<html>x</html>', out)

        with zipfile.ZipFile(out, 'r') as zf:
            readme = zf.read('README.txt').decode('utf-8')
        assert 'SalesWorkbook' in readme
        assert 'GREEN' in readme

    def test_package_csv_contains_check_rows(self, tmp_dir):
        import zipfile

        out = os.path.join(tmp_dir, 'package.zip')
        assessment = _AssessmentStub()

        with patch('powerbi_import.pdf_renderer.render_print_html', return_value='<html>print</html>'):
            with patch('powerbi_import.pptx_report.generate_pptx_report') as mock_pptx:
                mock_pptx.side_effect = lambda _d, p, migration_stats=None: open(p, 'wb').write(b'PPTX')
                report_packager.generate_report_package(assessment, '<html>x</html>', out)

        with zipfile.ZipFile(out, 'r') as zf:
            csv_data = zf.read('fidelity_checks.csv').decode('utf-8')
        assert 'Category,Check Name,Severity,Detail,Recommendation' in csv_data
        assert 'Connector supported' in csv_data

    def test_package_json_is_parseable(self, tmp_dir):
        import zipfile

        out = os.path.join(tmp_dir, 'package.zip')
        assessment = _AssessmentStub()

        with patch('powerbi_import.pdf_renderer.render_print_html', return_value='<html>print</html>'):
            with patch('powerbi_import.pptx_report.generate_pptx_report') as mock_pptx:
                mock_pptx.side_effect = lambda _d, p, migration_stats=None: open(p, 'wb').write(b'PPTX')
                report_packager.generate_report_package(assessment, '<html>x</html>', out)

        with zipfile.ZipFile(out, 'r') as zf:
            data = json.loads(zf.read('assessment_data.json').decode('utf-8'))
        assert data['workbook_name'] == 'SalesWorkbook'


class TestV38ApiBatchFlow:
    def setup_method(self):
        self._jobs_backup = api_server._jobs.copy()
        self._batches_backup = api_server._batches.copy()
        api_server._jobs.clear()
        api_server._batches.clear()

    def teardown_method(self):
        api_server._jobs.clear()
        api_server._jobs.update(self._jobs_backup)
        api_server._batches.clear()
        api_server._batches.update(self._batches_backup)

    def test_openapi_contains_batch_endpoints(self):
        spec = api_server._build_openapi_spec()
        assert '/migrate/batch' in spec['paths']
        assert '/batch/{id}' in spec['paths']

    def test_update_batch_progress_completed(self):
        api_server._jobs['job1'] = {'status': 'completed', 'created': 0}
        api_server._batches['b1'] = {
            'status': 'running',
            'created': 0,
            'job_ids': ['job1'],
            'completed': 0,
            'failed': 0,
            'total': 1,
        }

        api_server._update_batch_progress('b1', 'job1')

        assert api_server._batches['b1']['completed'] == 1
        assert api_server._batches['b1']['status'] == 'completed'

    def test_update_batch_progress_failed(self):
        api_server._jobs['job2'] = {'status': 'failed', 'created': 0}
        api_server._batches['b2'] = {
            'status': 'running',
            'created': 0,
            'job_ids': ['job2'],
            'completed': 0,
            'failed': 0,
            'total': 1,
        }

        api_server._update_batch_progress('b2', 'job2')

        assert api_server._batches['b2']['failed'] == 1
        assert api_server._batches['b2']['status'] == 'completed'

    def test_update_batch_progress_partial_stays_running(self):
        api_server._jobs['jobA'] = {'status': 'completed', 'created': 0}
        api_server._batches['b3'] = {
            'status': 'running',
            'created': 0,
            'job_ids': ['jobA', 'jobB'],
            'completed': 0,
            'failed': 0,
            'total': 2,
        }

        api_server._update_batch_progress('b3', 'jobA')

        assert api_server._batches['b3']['completed'] == 1
        assert api_server._batches['b3']['status'] == 'running'

    def test_update_batch_progress_missing_batch_is_noop(self):
        api_server._jobs['jobX'] = {'status': 'completed', 'created': 0}
        api_server._update_batch_progress('missing', 'jobX')
        assert 'missing' not in api_server._batches

    def test_update_batch_progress_missing_job_is_noop(self):
        api_server._batches['b4'] = {
            'status': 'running',
            'created': 0,
            'job_ids': ['jobZ'],
            'completed': 0,
            'failed': 0,
            'total': 1,
        }
        api_server._update_batch_progress('b4', 'jobZ')
        assert api_server._batches['b4']['completed'] == 0
        assert api_server._batches['b4']['failed'] == 0


class TestV38ArtifactDiffBaselineFlow:
    def test_run_artifact_diff_generates_json_and_html(self, tmp_dir):
        old_dir = _build_project(tmp_dir, name='Old', table_tmdl=SAMPLE_TABLE_V1)
        new_dir = _build_project(tmp_dir, name='New', table_tmdl=SAMPLE_TABLE_V2)

        report = migrate._run_artifact_diff(new_dir, old_dir, tmp_dir)

        assert report.has_changes
        assert os.path.isfile(os.path.join(tmp_dir, 'artifact_diff.json'))
        assert os.path.isfile(os.path.join(tmp_dir, 'artifact_diff_report.html'))

    def test_run_artifact_diff_identical_projects_has_no_changes(self, tmp_dir):
        old_dir = _build_project(tmp_dir, name='Old2', table_tmdl=SAMPLE_TABLE_V1)
        new_dir = _build_project(tmp_dir, name='New2', table_tmdl=SAMPLE_TABLE_V1)

        report = migrate._run_artifact_diff(new_dir, old_dir, tmp_dir)

        assert report.has_changes is False

    def test_run_save_baseline_creates_manifest(self, tmp_dir):
        project_dir = _build_project(tmp_dir, name='Current', table_tmdl=SAMPLE_TABLE_V1)
        baseline_dir = os.path.join(tmp_dir, 'baseline')

        migrate._run_save_baseline(project_dir, baseline_dir)

        assert os.path.isdir(baseline_dir)
        assert os.path.isfile(os.path.join(baseline_dir, BASELINE_MANIFEST))

    def test_run_check_baseline_passes_without_changes(self, tmp_dir):
        project_dir = _build_project(tmp_dir, name='CurrentPass', table_tmdl=SAMPLE_TABLE_V1)
        baseline_dir = os.path.join(tmp_dir, 'baseline_pass')
        migrate._run_save_baseline(project_dir, baseline_dir)

        passed = migrate._run_check_baseline(project_dir, baseline_dir, tmp_dir)
        assert passed is True

    def test_run_check_baseline_fails_with_changes_and_writes_reports(self, tmp_dir):
        project_dir = _build_project(tmp_dir, name='CurrentFail', table_tmdl=SAMPLE_TABLE_V2)
        baseline_src = _build_project(tmp_dir, name='BaseFail', table_tmdl=SAMPLE_TABLE_V1)
        baseline_dir = os.path.join(tmp_dir, 'baseline_fail')
        migrate._run_save_baseline(baseline_src, baseline_dir)

        passed = migrate._run_check_baseline(project_dir, baseline_dir, tmp_dir)
        assert passed is False
        assert os.path.isfile(os.path.join(tmp_dir, 'baseline_diff.json'))
        assert os.path.isfile(os.path.join(tmp_dir, 'baseline_diff_report.html'))

    def test_run_check_baseline_missing_baseline_returns_false(self, tmp_dir):
        project_dir = _build_project(tmp_dir, name='CurrentMissing', table_tmdl=SAMPLE_TABLE_V1)
        baseline_dir = os.path.join(tmp_dir, 'missing_baseline')

        passed = migrate._run_check_baseline(project_dir, baseline_dir, tmp_dir)
        assert passed is False


class TestV38PostGenerationOrchestration:
    @staticmethod
    def _args(**overrides):
        base = dict(
            compare=False,
            dashboard=False,
            fidelity=False,
            autoplay=False,
            autoplay_open=False,
            verbose=False,
            dry_run=False,
            output_dir=None,
            diff=None,
            save_baseline=None,
            check_baseline=None,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_calls_artifact_diff_when_enabled(self):
        args = self._args(diff='previous_dir')
        results = {'generation': True}

        with patch.object(migrate, '_run_artifact_diff') as mock_diff:
            migrate._run_post_generation_reports(args, 'BookA', results)

        mock_diff.assert_called_once()

    def test_skips_artifact_diff_on_dry_run(self):
        args = self._args(diff='previous_dir', dry_run=True)
        results = {'generation': True}

        with patch.object(migrate, '_run_artifact_diff') as mock_diff:
            migrate._run_post_generation_reports(args, 'BookB', results)

        mock_diff.assert_not_called()

    def test_calls_save_baseline_when_enabled(self):
        args = self._args(save_baseline='baseline_dir')
        results = {'generation': True}

        with patch.object(migrate, '_run_save_baseline') as mock_save:
            migrate._run_post_generation_reports(args, 'BookC', results)

        mock_save.assert_called_once()

    def test_calls_check_baseline_when_enabled(self):
        args = self._args(check_baseline='baseline_dir')
        results = {'generation': True}

        with patch.object(migrate, '_run_check_baseline', return_value=True) as mock_check:
            migrate._run_post_generation_reports(args, 'BookD', results)

        mock_check.assert_called_once()

    def test_sets_baseline_failed_when_check_returns_false(self):
        args = self._args(check_baseline='baseline_dir')
        results = {'generation': True}

        with patch.object(migrate, '_run_check_baseline', return_value=False):
            migrate._run_post_generation_reports(args, 'BookE', results)

        assert results.get('baseline_failed') is True

    def test_no_baseline_failed_when_check_passes(self):
        args = self._args(check_baseline='baseline_dir')
        results = {'generation': True}

        with patch.object(migrate, '_run_check_baseline', return_value=True):
            migrate._run_post_generation_reports(args, 'BookF', results)

        assert 'baseline_failed' not in results

    def test_skips_post_reports_when_generation_false(self):
        args = self._args(diff='x', save_baseline='y', check_baseline='z')
        results = {'generation': False}

        with patch.object(migrate, '_run_artifact_diff') as mock_diff:
            with patch.object(migrate, '_run_save_baseline') as mock_save:
                with patch.object(migrate, '_run_check_baseline') as mock_check:
                    migrate._run_post_generation_reports(args, 'BookG', results)

        mock_diff.assert_not_called()
        mock_save.assert_not_called()
        mock_check.assert_not_called()

    def test_logs_warning_when_artifact_diff_raises(self):
        args = self._args(diff='previous_dir')
        results = {'generation': True}

        with patch.object(migrate, '_run_artifact_diff', side_effect=ValueError('boom')):
            with patch.object(migrate.logger, 'warning') as mock_warn:
                migrate._run_post_generation_reports(args, 'BookH', results)

        assert mock_warn.called


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
