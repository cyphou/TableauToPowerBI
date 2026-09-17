"""Tests for Sprint 167 - migration_planner module."""

import unittest
from powerbi_import.migration_planner import (
    estimate_effort,
    assign_waves,
    generate_workspace_mapping,
    generate_permission_mapping,
    generate_migration_plan,
    generate_plan_html,
)


class TestEstimateEffort(unittest.TestCase):
    def test_simple_workbook(self):
        stats = {'visuals': 3, 'measures': 5, 'connectors': 1, 'rls_roles': 0}
        result = estimate_effort(stats)
        self.assertEqual(result['category'], 'Simple')
        self.assertLess(result['score'], 30)

    def test_complex_workbook(self):
        stats = {'visuals': 45, 'measures': 70, 'connectors': 4, 'rls_roles': 3}
        result = estimate_effort(stats)
        self.assertIn(result['category'], ('Complex', 'Very Complex'))
        self.assertGreater(result['score'], 50)

    def test_empty_workbook(self):
        result = estimate_effort({})
        self.assertEqual(result['category'], 'Simple')

    def test_medium_workbook(self):
        stats = {'visuals': 15, 'measures': 20, 'connectors': 2, 'rls_roles': 0}
        result = estimate_effort(stats)
        self.assertIn(result['category'], ('Simple', 'Medium', 'Complex'))


class TestAssignWaves(unittest.TestCase):
    def test_single_workbook_one_wave(self):
        items = [{'id': 'wb1', 'name': 'WB1', 'stats': {'visuals': 3}, 'datasource_ids': ['ds1']}]
        waves = assign_waves(items)
        self.assertEqual(len(waves), 1)
        self.assertEqual(len(waves[0]['workbooks']), 1)

    def test_multiple_waves(self):
        items = [
            {'id': f'wb{i}', 'name': f'WB{i}', 'stats': {'visuals': i * 5},
             'datasource_ids': [f'ds{i}']}
            for i in range(15)
        ]
        waves = assign_waves(items, max_per_wave=5)
        self.assertGreater(len(waves), 1)
        self.assertTrue(all(len(w['workbooks']) <= 5 for w in waves))

    def test_shared_datasource_clusters(self):
        items = [
            {'id': 'wb1', 'name': 'WB1', 'stats': {}, 'datasource_ids': ['shared']},
            {'id': 'wb2', 'name': 'WB2', 'stats': {}, 'datasource_ids': ['shared']},
            {'id': 'wb3', 'name': 'WB3', 'stats': {}, 'datasource_ids': ['other']},
        ]
        dep_graph = {'shared': ['wb1', 'wb2']}
        waves = assign_waves(items, dependency_graph=dep_graph, max_per_wave=10)
        wave_0_names = [w['name'] for w in waves[0]['workbooks']]
        if 'WB1' in wave_0_names:
            self.assertIn('WB2', wave_0_names)


class TestGenerateWorkspaceMapping(unittest.TestCase):
    def test_by_project_strategy(self):
        items = [{'name': 'WB1', 'project': 'Sales'}, {'name': 'WB2', 'project': 'Marketing'}]
        mapping = generate_workspace_mapping(items, strategy='by_project')
        self.assertEqual(len(mapping), 2)
        self.assertTrue(any('Sales' in k for k in mapping.keys()))

    def test_flat_strategy(self):
        items = [{'name': 'WB1', 'project': 'A'}, {'name': 'WB2', 'project': 'B'}]
        mapping = generate_workspace_mapping(items, strategy='flat')
        self.assertEqual(len(mapping), 1)

    def test_consolidated_strategy(self):
        items = (
            [{'name': f'WB{i}', 'project': 'Big'} for i in range(4)]
            + [{'name': 'WBx', 'project': 'Tiny'}]
        )
        mapping = generate_workspace_mapping(items, strategy='consolidated')
        self.assertTrue(any('Big' in k for k in mapping.keys()))


class TestGeneratePermissionMapping(unittest.TestCase):
    def test_creator_maps_to_admin(self):
        users = [{'name': 'admin', 'siteRole': 'Creator', 'email': 'a@c.com'}]
        result = generate_permission_mapping(users, [], {'WS1': []})
        entry = next(p for p in result if p['identity'] == 'a@c.com')
        self.assertEqual(entry['pbi_role'], 'Admin')

    def test_viewer_maps_to_viewer(self):
        users = [{'name': 'v', 'siteRole': 'Viewer', 'email': 'v@c.com'}]
        result = generate_permission_mapping(users, [], {'WS1': []})
        entry = next(p for p in result if p['identity'] == 'v@c.com')
        self.assertEqual(entry['pbi_role'], 'Viewer')

    def test_explorer_maps_to_member(self):
        users = [{'name': 'e', 'siteRole': 'Explorer', 'email': 'e@c.com'}]
        result = generate_permission_mapping(users, [], {'WS1': []})
        entry = next(p for p in result if p['identity'] == 'e@c.com')
        self.assertEqual(entry['pbi_role'], 'Member')


class TestGenerateMigrationPlan(unittest.TestCase):
    def test_plan_structure(self):
        inventory = {
            'workbooks': [
                {'id': 'wb1', 'name': 'WB1', 'project': 'Sales',
                 'stats': {'visuals': 5}, 'datasource_ids': ['ds1']},
            ],
            'users': [{'name': 'u1', 'siteRole': 'Creator', 'email': 'u@c.com'}],
            'groups': [],
            'datasources': [{'id': 'ds1', 'name': 'DS1', 'type': 'postgres'}],
        }
        plan = generate_migration_plan(inventory)
        self.assertIn('waves', plan)
        self.assertIn('workspace_mapping', plan)
        self.assertIn('summary', plan)
        self.assertEqual(plan['summary']['total_workbooks'], 1)

    def test_empty_plan(self):
        plan = generate_migration_plan({'workbooks': [], 'users': [], 'groups': []})
        self.assertEqual(plan['summary']['total_workbooks'], 0)


class TestGeneratePlanHtml(unittest.TestCase):
    def test_html_output(self):
        plan = {
            'summary': {
                'total_workbooks': 1, 'total_waves': 1,
                'total_effort_hours': 2.0, 'workspace_count': 1,
                'user_count': 1, 'group_count': 0,
            },
            'waves': [{
                'wave': 1,
                'workbooks': [
                    {'id': 'wb1', 'name': 'WB1',
                     'effort': {'score': 20, 'hours': 2.0, 'category': 'Simple'}},
                ],
                'effort_hours': 2.0,
            }],
            'workspace_mapping': {'PBI - Sales': ['WB1']},
            'permissions': [{
                'type': 'user', 'identity': 'u1', 'pbi_role': 'Admin',
                'source_role': 'Creator', 'workspaces': ['PBI - Sales'],
            }],
        }
        html = generate_plan_html(plan)
        self.assertIn('<html', html)
        self.assertIn('WB1', html)


# ── Sprint 167 — Timeline & Topology Integration Tests ─────────────

class TestGenerateTimeline(unittest.TestCase):

    def test_generates_dates(self):
        from powerbi_import.migration_planner import generate_timeline
        waves = [
            {'wave': 1, 'effort_hours': 10, 'workbooks': []},
            {'wave': 2, 'effort_hours': 20, 'workbooks': []},
        ]
        timeline = generate_timeline(waves, team_size=2, start_date='2025-03-01')
        self.assertEqual(len(timeline), 2)
        self.assertEqual(timeline[0]['start_date'], '2025-03-01')
        self.assertGreater(timeline[1]['start_date'], timeline[0]['end_date'])

    def test_single_engineer(self):
        from powerbi_import.migration_planner import generate_timeline
        waves = [{'wave': 1, 'effort_hours': 6, 'workbooks': []}]
        timeline = generate_timeline(waves, team_size=1, hours_per_day=6)
        self.assertEqual(timeline[0]['duration_days'], 1)

    def test_empty_waves(self):
        from powerbi_import.migration_planner import generate_timeline
        timeline = generate_timeline([], team_size=1)
        self.assertEqual(timeline, [])


class TestGenerateMigrationPlanFromTopology(unittest.TestCase):

    def test_from_topology(self):
        from powerbi_import.migration_planner import generate_migration_plan_from_topology
        topology = {
            'workbooks': [
                {'id': 'wb1', 'name': 'WB1', 'project': {'name': 'Sales'},
                 'sheetCount': 5, 'datasource_ids': []},
            ],
            'users': [{'name': 'alice', 'siteRole': 'Creator', 'email': 'a@co.com'}],
            'groups': [],
            'datasources': [],
        }
        plan = generate_migration_plan_from_topology(topology, team_size=2)
        self.assertIn('timeline', plan)
        self.assertIn('summary', plan)

    def test_with_string_project(self):
        from powerbi_import.migration_planner import generate_migration_plan_from_topology
        topology = {
            'workbooks': [
                {'id': 'wb1', 'name': 'WB1', 'project': 'Sales',
                 'sheetCount': 3, 'datasource_ids': []},
            ],
            'users': [],
            'groups': [],
            'datasources': [],
        }
        plan = generate_migration_plan_from_topology(topology)
        self.assertEqual(plan['summary']['total_workbooks'], 1)


if __name__ == '__main__':
    unittest.main()
