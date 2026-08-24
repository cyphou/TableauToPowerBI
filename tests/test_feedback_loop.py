"""Tests for Phase 10 — Continuous Feedback Loop."""

import json
import os
import shutil
import tempfile
import unittest
import zipfile

from powerbi_import.feedback_loop import (
    IssueCollector,
    RegressionFixtureGenerator,
    ZeroTouchTracker,
    redact_text,
)


class TestRedactText(unittest.TestCase):
    """Credential redaction."""

    def test_redacts_password(self):
        text = 'password=SuperSecret123'
        result = redact_text(text)
        self.assertNotIn('SuperSecret123', result)
        self.assertIn('REDACTED', result)

    def test_redacts_server(self):
        text = 'Server=mydb.database.windows.net'
        result = redact_text(text)
        self.assertNotIn('mydb.database.windows.net', result)

    def test_redacts_json_secret(self):
        text = '{"apiKey": "abc123def456"}'
        result = redact_text(text)
        self.assertNotIn('abc123def456', result)
        self.assertIn('REDACTED', result)

    def test_preserves_non_sensitive(self):
        text = 'SELECT * FROM Sales WHERE Year = 2024'
        result = redact_text(text)
        self.assertEqual(text, result)

    def test_multiple_redactions(self):
        text = 'Server=host1;User Id=admin;Password=pass123'
        result = redact_text(text)
        self.assertNotIn('host1', result)
        self.assertNotIn('admin', result)
        self.assertNotIn('pass123', result)


class TestIssueCollector(unittest.TestCase):
    """Issue package creation."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project_dir = os.path.join(self.tmp, 'TestProject')
        os.makedirs(self.project_dir)
        self.extract_dir = os.path.join(self.tmp, 'extract')
        os.makedirs(self.extract_dir)
        # Create a sample extraction JSON
        with open(os.path.join(self.extract_dir, 'worksheets.json'), 'w') as f:
            json.dump([{'name': 'Sheet1'}], f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_collect_creates_zip(self):
        collector = IssueCollector(self.project_dir, 'TestProject',
                                   extract_dir=self.extract_dir)
        path = collector.collect(output_dir=self.tmp)
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(path.endswith('.zip'))

    def test_zip_contains_metadata(self):
        collector = IssueCollector(self.project_dir, 'TestProject',
                                   extract_dir=self.extract_dir)
        path = collector.collect(output_dir=self.tmp)
        with zipfile.ZipFile(path, 'r') as zf:
            self.assertIn('issue/metadata.json', zf.namelist())
            meta = json.loads(zf.read('issue/metadata.json'))
            self.assertEqual(meta['source_basename'], 'TestProject')

    def test_zip_contains_extraction(self):
        collector = IssueCollector(self.project_dir, 'TestProject',
                                   extract_dir=self.extract_dir)
        path = collector.collect(output_dir=self.tmp)
        with zipfile.ZipFile(path, 'r') as zf:
            self.assertIn('issue/extraction/worksheets.json', zf.namelist())

    def test_zip_contains_fixture_hint(self):
        collector = IssueCollector(self.project_dir, 'TestProject',
                                   extract_dir=self.extract_dir)
        path = collector.collect(output_dir=self.tmp)
        with zipfile.ZipFile(path, 'r') as zf:
            self.assertIn('issue/fixture_hint.json', zf.namelist())
            hint = json.loads(zf.read('issue/fixture_hint.json'))
            self.assertEqual(hint['source_basename'], 'TestProject')

    def test_collect_with_verdict_dict(self):
        verdict = {
            'severity': 'error',
            'issues': [
                {'severity': 'error', 'source': 'validator', 'message': 'missing file'},
            ],
        }
        collector = IssueCollector(self.project_dir, 'TestProject',
                                   extract_dir=self.extract_dir)
        path = collector.collect(verdict=verdict, output_dir=self.tmp)
        with zipfile.ZipFile(path, 'r') as zf:
            meta = json.loads(zf.read('issue/metadata.json'))
            self.assertEqual(meta['verdict']['severity'], 'error')
            hint = json.loads(zf.read('issue/fixture_hint.json'))
            self.assertEqual(len(hint['failure_modes']), 1)

    def test_collect_with_source_file(self):
        source = os.path.join(self.tmp, 'test.twbx')
        with open(source, 'w') as f:
            f.write('dummy')
        collector = IssueCollector(self.project_dir, 'TestProject',
                                   extract_dir=self.extract_dir)
        path = collector.collect(source_file=source, output_dir=self.tmp)
        with zipfile.ZipFile(path, 'r') as zf:
            meta = json.loads(zf.read('issue/metadata.json'))
            self.assertEqual(meta['source_file_name'], 'test.twbx')
            self.assertEqual(meta['source_file_size'], 5)

    def test_redacts_extraction_content(self):
        # Write extraction JSON with credentials
        with open(os.path.join(self.extract_dir, 'datasources.json'), 'w') as f:
            json.dump([{'connection': 'Server=secret.db;Password=abc123'}], f)
        collector = IssueCollector(self.project_dir, 'TestProject',
                                   extract_dir=self.extract_dir)
        path = collector.collect(output_dir=self.tmp)
        with zipfile.ZipFile(path, 'r') as zf:
            content = zf.read('issue/extraction/datasources.json').decode('utf-8')
            self.assertNotIn('secret.db', content)
            self.assertNotIn('abc123', content)

    def test_collect_includes_qa_report(self):
        qa = {'validation': {'valid': True, 'errors': 0}}
        with open(os.path.join(self.project_dir, 'qa_report.json'), 'w') as f:
            json.dump(qa, f)
        collector = IssueCollector(self.project_dir, 'TestProject',
                                   extract_dir=self.extract_dir)
        path = collector.collect(output_dir=self.tmp)
        with zipfile.ZipFile(path, 'r') as zf:
            self.assertIn('issue/qa_report.json', zf.namelist())


class TestRegressionFixtureGenerator(unittest.TestCase):
    """Regression fixture generation from issue packages."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Create a minimal issue package
        self.pkg_path = os.path.join(self.tmp, 'test_issue.zip')
        with zipfile.ZipFile(self.pkg_path, 'w') as zf:
            zf.writestr('issue/metadata.json', json.dumps({
                'source_basename': 'TestWb',
                'timestamp': '2025-01-01T00:00:00',
            }))
            zf.writestr('issue/fixture_hint.json', json.dumps({
                'source_basename': 'TestWb',
                'failure_modes': ['missing_table_ref'],
                'affected_areas': ['validator', 'cross_validator'],
            }))
            zf.writestr('issue/extraction/worksheets.json',
                         json.dumps([{'name': 'Sheet1'}]))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_generates_fixture_dir(self):
        gen = RegressionFixtureGenerator(self.pkg_path)
        out_dir = os.path.join(self.tmp, 'fixtures')
        fixture_dir = gen.generate(output_dir=out_dir)
        self.assertIsNotNone(fixture_dir)
        self.assertTrue(os.path.isdir(fixture_dir))

    def test_fixture_has_metadata(self):
        gen = RegressionFixtureGenerator(self.pkg_path)
        out_dir = os.path.join(self.tmp, 'fixtures')
        fixture_dir = gen.generate(output_dir=out_dir)
        fixture_json = os.path.join(fixture_dir, 'fixture.json')
        self.assertTrue(os.path.isfile(fixture_json))
        with open(fixture_json) as f:
            data = json.load(f)
        self.assertEqual(data['source'], 'TestWb')
        self.assertIn('missing_table_ref', data['failure_modes'])

    def test_fixture_has_extraction_jsons(self):
        gen = RegressionFixtureGenerator(self.pkg_path)
        out_dir = os.path.join(self.tmp, 'fixtures')
        fixture_dir = gen.generate(output_dir=out_dir)
        ws = os.path.join(fixture_dir, 'worksheets.json')
        self.assertTrue(os.path.isfile(ws))

    def test_bad_zip_returns_none(self):
        bad_path = os.path.join(self.tmp, 'bad.zip')
        with open(bad_path, 'w') as f:
            f.write('not a zip')
        gen = RegressionFixtureGenerator(bad_path)
        result = gen.generate(output_dir=self.tmp)
        self.assertIsNone(result)


class TestZeroTouchTracker(unittest.TestCase):
    """Zero-Touch Open Rate tracking."""

    def test_empty_rate(self):
        tracker = ZeroTouchTracker()
        self.assertEqual(tracker.zero_touch_rate, 0.0)
        self.assertEqual(tracker.total_count, 0)

    def test_all_success(self):
        tracker = ZeroTouchTracker()
        tracker.record('wb1', success=True)
        tracker.record('wb2', success=True)
        self.assertEqual(tracker.zero_touch_rate, 1.0)
        self.assertEqual(tracker.success_count, 2)

    def test_mixed_results(self):
        tracker = ZeroTouchTracker()
        tracker.record('wb1', success=True)
        tracker.record('wb2', success=False, failure_mode='missing_table')
        self.assertEqual(tracker.zero_touch_rate, 0.5)
        self.assertEqual(tracker.failure_count, 1)

    def test_top_failure_modes(self):
        tracker = ZeroTouchTracker()
        tracker.record('a', success=False, failure_mode='bad_dax')
        tracker.record('b', success=False, failure_mode='bad_dax')
        tracker.record('c', success=False, failure_mode='missing_col')
        modes = tracker.top_failure_modes()
        self.assertEqual(modes[0], ('bad_dax', 2))
        self.assertEqual(modes[1], ('missing_col', 1))

    def test_get_summary(self):
        tracker = ZeroTouchTracker()
        tracker.record('wb1', success=True)
        tracker.record('wb2', success=False, failure_mode='error')
        summary = tracker.get_summary()
        self.assertEqual(summary['total'], 2)
        self.assertEqual(summary['success'], 1)
        self.assertEqual(summary['zero_touch_rate'], 50.0)

    def test_save_and_load(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'history.json')
            tracker = ZeroTouchTracker(history_path=path)
            tracker.record('wb1', success=True)
            tracker.record('wb2', success=False, failure_mode='err')
            saved = tracker.save()
            self.assertIsNotNone(saved)
            self.assertTrue(os.path.isfile(path))

            # Load
            tracker2 = ZeroTouchTracker(history_path=path)
            self.assertEqual(tracker2.total_count, 2)
            self.assertEqual(tracker2.zero_touch_rate, 0.5)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_save_creates_parent_dirs(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, 'nested', 'deep', 'history.json')
            tracker = ZeroTouchTracker(history_path=path)
            tracker.record('wb1', success=True)
            result = tracker.save()
            self.assertIsNotNone(result)
            self.assertTrue(os.path.isfile(path))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestZeroTouchDashboard(unittest.TestCase):
    """Dashboard HTML rendering."""

    def test_render_html(self):
        tracker = ZeroTouchTracker()
        tracker.record('wb1', success=True)
        tracker.record('wb2', success=False, failure_mode='bad_dax')
        tracker.record('wb3', success=True)
        html = tracker.render_dashboard_html()
        self.assertIn('Zero-Touch Open Rate', html)
        self.assertIn('66.7%', html)
        self.assertIn('bad_dax', html)
        self.assertIn('wb1', html)

    def test_save_dashboard(self):
        tmp = tempfile.mkdtemp()
        try:
            tracker = ZeroTouchTracker()
            tracker.record('wb1', success=True)
            path = os.path.join(tmp, 'dashboard.html')
            result = tracker.save_dashboard(path)
            self.assertIsNotNone(result)
            self.assertTrue(os.path.isfile(path))
            with open(path, 'r') as f:
                html = f.read()
            self.assertIn('Zero-Touch Open Rate', html)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_xss_escaped(self):
        tracker = ZeroTouchTracker()
        tracker.record('<script>alert(1)</script>', success=False,
                       failure_mode='<img onerror=alert(1)>')
        html = tracker.render_dashboard_html()
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_empty_dashboard(self):
        tracker = ZeroTouchTracker()
        html = tracker.render_dashboard_html()
        self.assertIn('0.0%', html)
        self.assertIn('No records', html)


class TestFixtureHintFromVerdict(unittest.TestCase):
    """Fixture hint builds correctly from verdict data."""

    def test_hint_from_verdict_with_tuples(self):
        from powerbi_import.rollback_engine import Severity, Verdict
        verdict = Verdict(Severity.ERROR, [
            (Severity.ERROR, 'validator', 'missing model.tmdl'),
            (Severity.WARNING, 'schema', 'old version'),
        ])
        collector = IssueCollector.__new__(IssueCollector)
        collector.source_basename = 'Test'
        hint = collector._build_fixture_hint(verdict)
        self.assertEqual(len(hint['failure_modes']), 1)
        self.assertIn('missing model.tmdl', hint['failure_modes'])
        self.assertIn('validator', hint['affected_areas'])

    def test_hint_from_verdict_dict(self):
        verdict = {
            'issues': [
                {'severity': 'error', 'source': 'cross', 'message': 'orphan ref'},
                {'severity': 'critical', 'source': 'validator', 'message': 'fatal'},
            ]
        }
        collector = IssueCollector.__new__(IssueCollector)
        collector.source_basename = 'Test'
        hint = collector._build_fixture_hint(verdict)
        self.assertEqual(len(hint['failure_modes']), 2)


if __name__ == '__main__':
    unittest.main()
