"""
Integration tests for Tableau Server Enterprise Migration pipeline.
Tests the end-to-end flow: discover → plan → permissions → subscriptions → cutover.
"""

import json
import os
import sys
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.dependency_graph import (
    build_site_topology,
    build_dependency_graph,
    classify_usage,
    save_topology,
    load_topology,
)
from powerbi_import.migration_planner import (
    generate_migration_plan,
    generate_migration_plan_from_topology,
    generate_timeline,
)
from powerbi_import.permission_mapper import (
    map_site_roles,
    reconcile_rls_principals,
)
from powerbi_import.subscription_generator import (
    generate_pbi_subscriptions,
    generate_power_automate_flows,
    detect_schedule_conflicts,
)
from powerbi_import.cutover_manager import (
    generate_cutover_plan,
    execute_cutover,
    parallel_run_check,
)


# ── Mock Client ─────────────────────────────────────────────────────

class _MockServerClient:
    """Simulates a Tableau Server client with a realistic site topology."""

    def list_workbooks(self):
        return [
            {'id': 'wb1', 'name': 'Sales Dashboard', 'project': {'name': 'Sales'},
             'sheetCount': 5, 'owner': {'id': 'u1'}},
            {'id': 'wb2', 'name': 'Revenue Report', 'project': {'name': 'Sales'},
             'sheetCount': 3, 'owner': {'id': 'u2'}},
            {'id': 'wb3', 'name': 'Finance Overview', 'project': {'name': 'Finance'},
             'sheetCount': 8, 'owner': {'id': 'u1'}},
        ]

    def list_published_datasources(self):
        return [
            {'id': 'ds1', 'name': 'Sales DB', 'type': 'sqlserver',
             'project': {'name': 'Sales'}, 'owner': {'id': 'u1'}},
        ]

    def get_workbook_connections(self, wb_id):
        if wb_id in ('wb1', 'wb2'):
            return [{'datasource': {'id': 'ds1', 'name': 'Sales DB'}}]
        return []

    def list_users(self):
        return [
            {'id': 'u1', 'name': 'alice', 'siteRole': 'Creator',
             'email': 'alice@contoso.com'},
            {'id': 'u2', 'name': 'bob', 'siteRole': 'Viewer',
             'email': 'bob@contoso.com'},
        ]

    def list_groups(self):
        return [
            {'name': 'Sales Team', 'users': [
                {'name': 'alice', 'email': 'alice@contoso.com'},
                {'name': 'bob', 'email': 'bob@contoso.com'},
            ]},
        ]

    def list_users_with_groups(self):
        return [
            {'id': 'u1', 'name': 'alice', 'siteRole': 'Creator',
             'email': 'alice@contoso.com', 'groups': ['Sales Team']},
            {'id': 'u2', 'name': 'bob', 'siteRole': 'Viewer',
             'email': 'bob@contoso.com', 'groups': ['Sales Team']},
        ]

    def get_all_subscriptions(self):
        return [
            {
                'id': 'sub1', 'subject': 'Daily Sales',
                'schedule': {'frequency': 'Daily', 'time': '08:00:00'},
                'content': {'type': 'Workbook', 'id': 'wb1', 'name': 'Sales Dashboard'},
                'user': {'name': 'alice', 'email': 'alice@contoso.com'},
            },
        ]

    def list_data_alerts(self):
        return []

    def get_site_topology(self):
        return {
            'workbooks': self.list_workbooks(),
            'datasources': self.list_published_datasources(),
            'users': self.list_users_with_groups(),
            'groups': self.list_groups(),
        }


# ── End-to-End Pipeline Tests ───────────────────────────────────────

class TestDiscoverToPlan(unittest.TestCase):
    """Test Phase 1→2: discover topology → generate migration plan."""

    def test_topology_to_plan(self):
        client = _MockServerClient()
        topology = client.get_site_topology()
        # Build dependency graph
        dep_graph = build_dependency_graph(topology)
        self.assertIsInstance(dep_graph, dict)

        # Generate plan
        plan = generate_migration_plan_from_topology(topology, team_size=2)
        self.assertIn('summary', plan)
        self.assertIn('waves', plan)
        self.assertIn('timeline', plan)
        self.assertEqual(plan['summary']['total_workbooks'], 3)


class TestPlanToPermissions(unittest.TestCase):
    """Test Phase 2→4: plan → permission mapping."""

    def test_plan_permissions(self):
        client = _MockServerClient()
        users = client.list_users_with_groups()
        assignments = map_site_roles(users)
        self.assertEqual(len(assignments), 2)

        # Creator → Admin, Viewer → Viewer
        alice = [a for a in assignments if a['identity'] == 'alice@contoso.com'][0]
        bob = [a for a in assignments if a['identity'] == 'bob@contoso.com'][0]
        self.assertEqual(alice['pbi_role'], 'Admin')
        self.assertEqual(bob['pbi_role'], 'Viewer')


class TestPermissionsToRls(unittest.TestCase):
    """Test RLS principal reconciliation."""

    def test_rls_reconciliation(self):
        users = [
            {'name': 'alice', 'email': 'alice@contoso.com'},
            {'name': 'bob', 'email': 'bob@contoso.com'},
        ]
        roles = [
            {'name': 'EastRole', 'members': ['alice']},
            {'name': 'WestRole', 'members': ['carol']},  # unresolvable
        ]
        result = reconcile_rls_principals(roles, users)
        self.assertEqual(len(result['assignments']), 1)
        self.assertEqual(result['assignments'][0]['azure_ad_upn'], 'alice@contoso.com')
        self.assertEqual(len(result['unresolved']), 1)


class TestSubscriptionMigration(unittest.TestCase):
    """Test Phase 5: subscription conversion."""

    def test_subscription_pipeline(self):
        client = _MockServerClient()
        subs = client.get_all_subscriptions()
        pbi_subs = generate_pbi_subscriptions(subs)
        self.assertGreaterEqual(len(pbi_subs), 1)
        conflicts = detect_schedule_conflicts(pbi_subs)
        self.assertIsInstance(conflicts, list)


class TestCutoverPipeline(unittest.TestCase):
    """Test Phase 6: cutover plan generation and dry-run."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cutover_dry_run(self):
        # Build a plan, then cutover
        topology = _MockServerClient().get_site_topology()
        plan = generate_migration_plan_from_topology(topology)

        cutover = generate_cutover_plan(plan, cutover_date='2025-07-01')
        result = execute_cutover(cutover, self.tmpdir, dry_run=True)
        self.assertIn('status', result)

    def test_parallel_run(self):
        tab = {'Revenue': 100000, 'Count': 42}
        pbi = {'Revenue': 100010, 'Count': 42}
        result = parallel_run_check(tab, pbi, tolerance=0.001)
        self.assertEqual(result['status'], 'pass')


class TestFullPipelineSaveLoad(unittest.TestCase):
    """Test topology save/load roundtrip."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_load_roundtrip(self):
        client = _MockServerClient()
        topology = client.get_site_topology()
        path = save_topology(topology, os.path.join(self.tmpdir, 'topology.json'))
        self.assertTrue(os.path.isfile(path))

        loaded = load_topology(path)
        self.assertEqual(len(loaded['workbooks']), 3)
        self.assertEqual(len(loaded['users']), 2)


if __name__ == '__main__':
    unittest.main()
