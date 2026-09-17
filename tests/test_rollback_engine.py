"""Tests for Phase 9 — Auto-Rollback + Recovery Engine."""

import json
import os
import shutil
import tempfile
import unittest
import zipfile

from powerbi_import.rollback_engine import RollbackEngine, Severity, Verdict


class TestSeverity(unittest.TestCase):
    """Severity ordering and comparison."""

    def test_worse_returns_higher(self):
        self.assertEqual(Severity.worse(Severity.INFO, Severity.WARNING), Severity.WARNING)
        self.assertEqual(Severity.worse(Severity.ERROR, Severity.WARNING), Severity.ERROR)
        self.assertEqual(Severity.worse(Severity.CRITICAL, Severity.ERROR), Severity.CRITICAL)

    def test_worse_same(self):
        self.assertEqual(Severity.worse(Severity.WARNING, Severity.WARNING), Severity.WARNING)

    def test_level_ordering(self):
        self.assertLess(Severity.level(Severity.INFO), Severity.level(Severity.WARNING))
        self.assertLess(Severity.level(Severity.WARNING), Severity.level(Severity.ERROR))
        self.assertLess(Severity.level(Severity.ERROR), Severity.level(Severity.CRITICAL))


class TestVerdict(unittest.TestCase):
    """Verdict creation and properties."""

    def test_clean_verdict(self):
        v = Verdict(Severity.INFO, [])
        self.assertTrue(v.should_ship)
        self.assertFalse(v.should_quarantine)
        self.assertFalse(v.should_rollback)
        self.assertEqual(v.exit_code, 0)

    def test_warning_verdict(self):
        v = Verdict(Severity.WARNING, [(Severity.WARNING, 'test', 'something')])
        self.assertTrue(v.should_ship)
        self.assertFalse(v.should_quarantine)
        self.assertEqual(v.exit_code, 1)

    def test_error_verdict(self):
        v = Verdict(Severity.ERROR, [(Severity.ERROR, 'test', 'broken')])
        self.assertFalse(v.should_ship)
        self.assertTrue(v.should_quarantine)
        self.assertFalse(v.should_rollback)
        self.assertEqual(v.exit_code, 2)

    def test_critical_verdict(self):
        v = Verdict(Severity.CRITICAL, [(Severity.CRITICAL, 'test', 'fatal')])
        self.assertFalse(v.should_ship)
        self.assertFalse(v.should_quarantine)
        self.assertTrue(v.should_rollback)
        self.assertEqual(v.exit_code, 3)

    def test_to_dict(self):
        v = Verdict(Severity.WARNING, [(Severity.WARNING, 'src', 'msg')])
        d = v.to_dict()
        self.assertEqual(d['severity'], 'warning')
        self.assertEqual(d['exit_code'], 1)
        self.assertEqual(d['issue_count'], 1)
        self.assertEqual(len(d['issues']), 1)
        self.assertIn('timestamp', d)

    def test_auto_message(self):
        v = Verdict(Severity.ERROR, [
            (Severity.ERROR, 's', 'm1'),
            (Severity.WARNING, 's', 'm2'),
        ])
        self.assertIn('1 error', v.message)
        self.assertIn('1 warning', v.message)


class TestEngineIngest(unittest.TestCase):
    """Ingestion of various result types."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project_dir = os.path.join(self.tmp, 'TestProject')
        os.makedirs(self.project_dir)
        self.engine = RollbackEngine(self.project_dir, 'TestProject')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ingest_validation_errors(self):
        val = {'errors': ['missing file', 'bad json'], 'warnings': ['minor']}
        self.engine.ingest_validation(val)
        self.assertEqual(len(self.engine.issues), 3)
        sevs = [s for s, _, _ in self.engine.issues]
        self.assertEqual(sevs.count(Severity.ERROR), 2)
        self.assertEqual(sevs.count(Severity.WARNING), 1)

    def test_ingest_validation_none(self):
        self.engine.ingest_validation(None)
        self.assertEqual(len(self.engine.issues), 0)

    def test_ingest_schema_dict(self):
        results = [
            {'issues': [
                {'severity': 'error', 'message': 'bad schema', 'path': 'v.json', 'repaired': False},
                {'severity': 'warning', 'message': 'old version', 'path': 'p.json', 'repaired': False},
                {'severity': 'error', 'message': 'type coerced', 'path': 'v2.json', 'repaired': True},
            ]}
        ]
        self.engine.ingest_schema_result(results)
        self.assertEqual(len(self.engine.issues), 3)
        # Repaired issue → INFO
        self.assertEqual(self.engine.issues[2][0], Severity.INFO)

    def test_ingest_cross_result_dict(self):
        cross = {'issues': [
            {'severity': 'error', 'message': 'missing table ref'},
            {'severity': 'warning', 'message': 'orphan visual'},
        ]}
        self.engine.ingest_cross_result(cross)
        self.assertEqual(len(self.engine.issues), 2)

    def test_ingest_repairs(self):
        recovery = {'repairs': [
            {'severity': 'error', 'description': 'broken DAX', 'category': 'tmdl'},
            {'severity': 'warning', 'description': 'visual fallback', 'category': 'visual'},
        ]}
        self.engine.ingest_repairs(recovery)
        self.assertEqual(len(self.engine.issues), 2)
        # Error repair → downgraded to WARNING
        self.assertEqual(self.engine.issues[0][0], Severity.WARNING)
        # Warning repair → downgraded to INFO
        self.assertEqual(self.engine.issues[1][0], Severity.INFO)

    def test_ingest_qa_report_file(self):
        qa = {
            'validation': {
                'valid': False,
                'errors': 2,
                'warnings': 5,
                'error_details': ['err1', 'err2'],
            },
            'auto_fix': {'total_repairs': 3},
        }
        qa_path = os.path.join(self.project_dir, 'qa_report.json')
        with open(qa_path, 'w') as f:
            json.dump(qa, f)

        self.engine.ingest_qa_report(qa_path)
        # 2 errors + 1 warning aggregate + 1 autofix info
        self.assertEqual(len(self.engine.issues), 4)

    def test_ingest_qa_missing_file(self):
        self.engine.ingest_qa_report('/nonexistent/qa.json')
        self.assertEqual(len(self.engine.issues), 0)


class TestEngineEvaluate(unittest.TestCase):
    """Verdict evaluation logic."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project_dir = os.path.join(self.tmp, 'TestProject')
        os.makedirs(self.project_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clean_evaluation(self):
        engine = RollbackEngine(self.project_dir, 'TestProject')
        verdict = engine.evaluate()
        self.assertEqual(verdict.severity, Severity.INFO)
        self.assertTrue(verdict.should_ship)

    def test_warning_evaluation(self):
        engine = RollbackEngine(self.project_dir, 'TestProject')
        engine.issues.append((Severity.WARNING, 'test', 'minor'))
        verdict = engine.evaluate()
        self.assertEqual(verdict.severity, Severity.WARNING)
        self.assertTrue(verdict.should_ship)

    def test_error_evaluation(self):
        engine = RollbackEngine(self.project_dir, 'TestProject')
        engine.issues.append((Severity.ERROR, 'test', 'broken'))
        verdict = engine.evaluate()
        self.assertEqual(verdict.severity, Severity.ERROR)
        self.assertTrue(verdict.should_quarantine)

    def test_critical_escalation(self):
        engine = RollbackEngine(self.project_dir, 'TestProject')
        # Add >20 errors to trigger CRITICAL escalation
        for i in range(25):
            engine.issues.append((Severity.ERROR, 'test', f'error {i}'))
        verdict = engine.evaluate()
        self.assertEqual(verdict.severity, Severity.CRITICAL)
        self.assertTrue(verdict.should_rollback)

    def test_mixed_uses_worst(self):
        engine = RollbackEngine(self.project_dir, 'TestProject')
        engine.issues.append((Severity.INFO, 'a', 'fine'))
        engine.issues.append((Severity.WARNING, 'b', 'hmm'))
        engine.issues.append((Severity.ERROR, 'c', 'bad'))
        verdict = engine.evaluate()
        self.assertEqual(verdict.severity, Severity.ERROR)


class TestEngineExecute(unittest.TestCase):
    """Execute actions: ship, quarantine, rollback."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project_dir = os.path.join(self.tmp, 'TestProject')
        os.makedirs(self.project_dir)
        # Create a dummy file in the project
        with open(os.path.join(self.project_dir, 'test.json'), 'w') as f:
            json.dump({'test': True}, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ship_action(self):
        engine = RollbackEngine(self.project_dir, 'TestProject')
        verdict = Verdict(Severity.INFO, [])
        result = engine.execute(verdict)
        self.assertEqual(result['action'], 'ship')
        self.assertIsNone(result['triage_path'])
        # Project still exists
        self.assertTrue(os.path.isdir(self.project_dir))

    def test_quarantine_moves_to_failed(self):
        engine = RollbackEngine(self.project_dir, 'TestProject')
        verdict = Verdict(Severity.ERROR, [(Severity.ERROR, 'v', 'bad schema')])
        result = engine.execute(verdict)
        self.assertEqual(result['action'], 'quarantine')
        # Project moved to _FAILED/
        failed_dir = os.path.join(self.tmp, '_FAILED', 'TestProject')
        self.assertTrue(os.path.isdir(failed_dir))
        self.assertFalse(os.path.isdir(self.project_dir))
        # Triage HTML exists
        self.assertTrue(result['triage_path'].endswith('.html'))

    def test_rollback_removes_and_restores(self):
        # Create a backup
        backup_dir = os.path.join(self.tmp, 'TestProject.backup_20250101')
        os.makedirs(backup_dir)
        with open(os.path.join(backup_dir, 'original.json'), 'w') as f:
            json.dump({'original': True}, f)

        engine = RollbackEngine(self.project_dir, 'TestProject')
        verdict = Verdict(Severity.CRITICAL, [(Severity.CRITICAL, 'v', 'fatal')])
        result = engine.execute(verdict, backup_dir=backup_dir)
        self.assertEqual(result['action'], 'rollback')
        # Triage package ZIP exists
        self.assertTrue(result['triage_path'].endswith('.zip'))
        self.assertTrue(os.path.isfile(result['triage_path']))
        # Project restored from backup
        self.assertTrue(os.path.isdir(self.project_dir))
        self.assertTrue(os.path.isfile(os.path.join(self.project_dir, 'original.json')))

    def test_rollback_without_backup(self):
        engine = RollbackEngine(self.project_dir, 'TestProject')
        verdict = Verdict(Severity.CRITICAL, [(Severity.CRITICAL, 'v', 'fatal')])
        result = engine.execute(verdict, backup_dir=None)
        self.assertEqual(result['action'], 'rollback')
        # Project removed, no restore
        self.assertFalse(os.path.isdir(self.project_dir))

    def test_strict_exit_codes(self):
        engine = RollbackEngine(self.project_dir, 'TestProject')
        verdict = Verdict(Severity.WARNING, [(Severity.WARNING, 'a', 'b')])
        result = engine.execute(verdict, strict=True)
        self.assertEqual(result['exit_code'], 1)

    def test_non_strict_exit_zero(self):
        engine = RollbackEngine(self.project_dir, 'TestProject')
        verdict = Verdict(Severity.WARNING, [(Severity.WARNING, 'a', 'b')])
        result = engine.execute(verdict, strict=False)
        self.assertEqual(result['exit_code'], 0)


class TestTriagePackage(unittest.TestCase):
    """Triage package contents."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project_dir = os.path.join(self.tmp, 'TestProject')
        os.makedirs(self.project_dir)
        # Create a migration metadata file
        with open(os.path.join(self.project_dir, 'migration_metadata.json'), 'w') as f:
            json.dump({'version': '31.5.0'}, f)
        # Create extract dir with a JSON
        self.extract_dir = os.path.join(self.tmp, 'extract')
        os.makedirs(self.extract_dir)
        with open(os.path.join(self.extract_dir, 'worksheets.json'), 'w') as f:
            json.dump([{'name': 'Sheet1'}], f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_triage_zip_contents(self):
        engine = RollbackEngine(self.project_dir, 'TestProject',
                                extract_dir=self.extract_dir)
        verdict = Verdict(Severity.CRITICAL, [(Severity.CRITICAL, 'v', 'fatal')])
        result = engine.execute(verdict)

        zip_path = result['triage_path']
        self.assertTrue(os.path.isfile(zip_path))

        with zipfile.ZipFile(zip_path, 'r') as zf:
            names = zf.namelist()
            # Should contain verdict JSON
            self.assertIn('triage/verdict.json', names)
            # Should contain extraction JSON
            self.assertIn('triage/extraction/worksheets.json', names)
            # Should contain output metadata
            self.assertIn('triage/output/migration_metadata.json', names)
            # Should contain triage HTML
            self.assertIn('triage/triage.html', names)

    def test_triage_zip_with_source_info(self):
        # Create a dummy source file
        source = os.path.join(self.tmp, 'test.twbx')
        with open(source, 'w') as f:
            f.write('dummy')

        engine = RollbackEngine(self.project_dir, 'TestProject',
                                extract_dir=self.extract_dir)
        verdict = Verdict(Severity.CRITICAL, [(Severity.CRITICAL, 'v', 'fatal')])
        result = engine.execute(verdict, source_file=source)

        with zipfile.ZipFile(result['triage_path'], 'r') as zf:
            self.assertIn('triage/source_info.json', zf.namelist())
            info = json.loads(zf.read('triage/source_info.json'))
            self.assertEqual(info['source_file'], 'test.twbx')


class TestTriageHTML(unittest.TestCase):
    """Triage HTML rendering."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project_dir = os.path.join(self.tmp, 'TestProject')
        os.makedirs(self.project_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_quarantine_creates_triage_html(self):
        engine = RollbackEngine(self.project_dir, 'TestProject')
        verdict = Verdict(Severity.ERROR, [
            (Severity.ERROR, 'validator', 'missing report.json'),
            (Severity.WARNING, 'schema', 'old page version'),
        ])
        result = engine.execute(verdict)
        triage_path = result['triage_path']
        self.assertTrue(os.path.isfile(triage_path))

        with open(triage_path, 'r', encoding='utf-8') as f:
            html = f.read()
        self.assertIn('Migration Triage Report', html)
        self.assertIn('TestProject', html)
        self.assertIn('missing report.json', html)
        self.assertIn('ERROR', html)

    def test_triage_html_escapes_xss(self):
        engine = RollbackEngine(self.project_dir, 'TestProject')
        verdict = Verdict(Severity.ERROR, [
            (Severity.ERROR, 'test', '<script>alert("xss")</script>'),
        ])
        result = engine.execute(verdict)

        failed_dir = os.path.join(self.tmp, '_FAILED')
        triage_html = os.path.join(failed_dir, 'TestProject_triage.html')
        with open(triage_html, 'r', encoding='utf-8') as f:
            html = f.read()
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)


class TestEngineIntegration(unittest.TestCase):
    """End-to-end integration scenarios."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project_dir = os.path.join(self.tmp, 'TestProject')
        os.makedirs(self.project_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_clean_pipeline(self):
        """No issues → ship."""
        engine = RollbackEngine(self.project_dir, 'TestProject')
        engine.ingest_validation({'errors': [], 'warnings': []})
        engine.ingest_schema_result([])
        engine.ingest_cross_result({'issues': []})
        verdict = engine.evaluate()
        result = engine.execute(verdict)
        self.assertEqual(result['action'], 'ship')
        self.assertTrue(os.path.isdir(self.project_dir))

    def test_warnings_only_still_ships(self):
        """Warnings only → ship with warnings."""
        engine = RollbackEngine(self.project_dir, 'TestProject')
        engine.ingest_validation({'errors': [], 'warnings': ['minor issue']})
        verdict = engine.evaluate()
        self.assertTrue(verdict.should_ship)
        result = engine.execute(verdict)
        self.assertEqual(result['action'], 'ship')

    def test_single_error_quarantines(self):
        """One validation error → quarantine."""
        engine = RollbackEngine(self.project_dir, 'TestProject')
        engine.ingest_validation({'errors': ['critical: missing model.tmdl'], 'warnings': []})
        verdict = engine.evaluate()
        self.assertTrue(verdict.should_quarantine)
        result = engine.execute(verdict)
        self.assertEqual(result['action'], 'quarantine')

    def test_massive_errors_rollback(self):
        """Many errors → escalate to CRITICAL → rollback."""
        engine = RollbackEngine(self.project_dir, 'TestProject')
        engine.ingest_validation({
            'errors': [f'error_{i}' for i in range(25)],
            'warnings': [],
        })
        verdict = engine.evaluate()
        self.assertTrue(verdict.should_rollback)
        result = engine.execute(verdict)
        self.assertEqual(result['action'], 'rollback')
        self.assertFalse(os.path.isdir(self.project_dir))


if __name__ == '__main__':
    unittest.main()
