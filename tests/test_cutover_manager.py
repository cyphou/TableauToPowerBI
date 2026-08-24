"""
Tests for Cutover Manager (powerbi_import.cutover_manager).
"""

import json
import os
import sys
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.cutover_manager import (
    generate_cutover_plan,
    execute_cutover,
    rollback,
    list_snapshots,
    parallel_run_check,
    generate_cutover_dashboard,
    save_cutover_plan,
)


# ── Fixtures ────────────────────────────────────────────────────────

def _migration_plan():
    return {
        'waves': [
            {
                'wave': 1,
                'effort_hours': 10,
                'workbooks': [
                    {'id': 'wb1', 'name': 'Sales Dashboard',
                     'effort': {'score': 15, 'hours': 4, 'category': 'Simple'}},
                    {'id': 'wb2', 'name': 'Revenue Report',
                     'effort': {'score': 25, 'hours': 6, 'category': 'Medium'}},
                ],
            },
            {
                'wave': 2,
                'effort_hours': 20,
                'workbooks': [
                    {'id': 'wb3', 'name': 'Finance Complex',
                     'effort': {'score': 60, 'hours': 20, 'category': 'Complex'}},
                ],
            },
        ],
        'workspace_mapping': {'PBI - Sales': ['Sales Dashboard', 'Revenue Report']},
    }


class TestGenerateCutoverPlan(unittest.TestCase):

    def test_generates_plan(self):
        plan = generate_cutover_plan(_migration_plan(), waves_to_cut=[1])
        self.assertIn('cutover_date', plan)
        self.assertIn('workbooks', plan)
        self.assertIn('stages', plan)
        self.assertIn('pre_checks', plan)

    def test_plan_only_mode(self):
        plan = generate_cutover_plan(_migration_plan(), plan_only=True)
        self.assertTrue(plan.get('plan_only'))

    def test_all_waves(self):
        plan = generate_cutover_plan(_migration_plan())
        self.assertGreaterEqual(plan['total_workbooks'], 1)

    def test_cutover_date(self):
        plan = generate_cutover_plan(_migration_plan(),
                                     cutover_date='2025-06-15')
        self.assertIn('2025-06-15', plan['cutover_date'])


class TestExecuteCutover(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.artifacts_dir = os.path.join(self.tmpdir, 'artifacts')
        os.makedirs(self.artifacts_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dry_run(self):
        plan = generate_cutover_plan(_migration_plan(), waves_to_cut=[1])
        result = execute_cutover(plan, self.artifacts_dir, dry_run=True)
        self.assertIn('status', result)
        self.assertIn('stages', result)


class TestRollback(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_rollback_nonexistent_snapshot(self):
        fake_snapshot = os.path.join(self.tmpdir, 'no_such_snapshot')
        result = rollback(fake_snapshot, self.tmpdir)
        self.assertEqual(result.get('status'), 'failed')

    def test_rollback_valid_snapshot(self):
        snap_dir = os.path.join(self.tmpdir, 'snapshots', 'snapshot_20250601_120000')
        os.makedirs(snap_dir)
        meta = {'cutover_date': '2025-06-01T12:00:00', 'files': []}
        with open(os.path.join(snap_dir, 'snapshot_meta.json'), 'w') as f:
            json.dump(meta, f)
        target = os.path.join(self.tmpdir, 'target')
        os.makedirs(target)
        result = rollback(snap_dir, target)
        self.assertIsNotNone(result)


class TestListSnapshots(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_dir(self):
        snapshots = list_snapshots(self.tmpdir)
        self.assertEqual(snapshots, [])

    def test_finds_snapshots(self):
        snap_dir = os.path.join(self.tmpdir, '.snapshots', 'snapshot_20250601_120000')
        os.makedirs(snap_dir)
        meta = {'cutover_date': '2025-06-01T12:00:00'}
        with open(os.path.join(snap_dir, 'snapshot_meta.json'), 'w') as f:
            json.dump(meta, f)
        snapshots = list_snapshots(self.tmpdir)
        self.assertEqual(len(snapshots), 1)


class TestParallelRunCheck(unittest.TestCase):

    def test_identical_data(self):
        data = {'Revenue': 100000, 'Count': 42}
        result = parallel_run_check(data, data)
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['mismatches'], 0)

    def test_within_tolerance(self):
        tab_data = {'Revenue': 100000}
        pbi_data = {'Revenue': 100001}
        result = parallel_run_check(tab_data, pbi_data, tolerance=0.001)
        self.assertEqual(result['status'], 'pass')

    def test_exceeds_tolerance(self):
        tab_data = {'Revenue': 100000}
        pbi_data = {'Revenue': 120000}
        result = parallel_run_check(tab_data, pbi_data, tolerance=0.01)
        self.assertEqual(result['status'], 'fail')
        self.assertGreater(result['mismatches'], 0)

    def test_missing_keys(self):
        tab_data = {'Revenue': 100, 'Count': 10}
        pbi_data = {'Revenue': 100}
        result = parallel_run_check(tab_data, pbi_data)
        self.assertGreater(len(result.get('missing_in_pbi', [])), 0)


class TestGenerateCutoverDashboard(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_produces_html(self):
        plan = generate_cutover_plan(_migration_plan(), waves_to_cut=[1])
        out = os.path.join(self.tmpdir, 'dashboard.html')
        result = generate_cutover_dashboard(plan, output_path=out)
        self.assertTrue(os.path.isfile(result))
        with open(result, encoding='utf-8') as f:
            html = f.read()
        self.assertIn('html', html.lower())


class TestSaveCutoverPlan(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_saves_json(self):
        plan = generate_cutover_plan(_migration_plan())
        path = os.path.join(self.tmpdir, 'cutover_plan.json')
        result = save_cutover_plan(plan, path)
        self.assertTrue(os.path.isfile(result))
        with open(result) as f:
            loaded = json.load(f)
        self.assertIn('workbooks', loaded)


if __name__ == '__main__':
    unittest.main()
