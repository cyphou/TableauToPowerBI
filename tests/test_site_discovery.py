"""
Tests for Site Topology Discovery (powerbi_import.dependency_graph).
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
    audit_certifications,
    enrich_with_lineage,
    generate_topology_report,
    save_topology,
    load_topology,
)


# ── Fixtures ────────────────────────────────────────────────────────

class _MockClient:
    """Minimal mock of TableauServerClient for topology discovery."""

    def get_site_info(self):
        return {'name': 'TestSite', 'contentUrl': 'test'}

    def list_workbooks(self, **_kw):
        return [
            {
                'id': 'wb1', 'name': 'Sales Dashboard',
                'project': {'name': 'Marketing'},
                'owner': {'name': 'alice'},
                'updatedAt': '2025-01-15',
                'usage': {'viewCount': '150'},
            },
            {
                'id': 'wb2', 'name': 'Finance Report',
                'project': {'name': 'Finance'},
                'owner': {'name': 'bob'},
                'updatedAt': '2024-06-01',
                'usage': {'viewCount': '5'},
            },
        ]

    def list_datasources(self):
        return [
            {
                'id': 'ds1', 'name': 'SalesDB',
                'type': 'sqlserver',
                'project': {'name': 'Marketing'},
                'isCertified': 'true',
            },
        ]

    def list_users(self):
        return [
            {'id': 'u1', 'name': 'alice', 'siteRole': 'Creator', 'email': 'alice@co.com'},
            {'id': 'u2', 'name': 'bob', 'siteRole': 'Viewer', 'email': 'bob@co.com'},
        ]

    def list_groups(self):
        return [{'id': 'g1', 'name': 'Analysts'}]

    def list_schedules(self):
        return [{'id': 's1', 'name': 'Daily'}]

    def list_views(self):
        return [{'id': 'v1', 'name': 'Sheet1', 'viewUrlName': 'sheet1'}]

    def list_projects(self):
        return [
            {'id': 'p1', 'name': 'Marketing'},
            {'id': 'p2', 'name': 'Finance'},
        ]

    def list_prep_flows(self):
        return []

    def get_workbook_connections(self, wb_id):
        if wb_id == 'wb1':
            return [{'datasource': {'id': 'ds1', 'name': 'SalesDB'}, 'type': 'sqlserver'}]
        return []

    def get_server_summary(self):
        return {'workbook_count': 2, 'datasource_count': 1}


class TestBuildSiteTopology(unittest.TestCase):

    def test_builds_topology_from_mock(self):
        client = _MockClient()
        topo = build_site_topology(client)
        self.assertIn('workbooks', topo)
        self.assertIn('datasources', topo)
        self.assertIn('users', topo)
        self.assertEqual(len(topo['workbooks']), 2)
        self.assertEqual(len(topo['datasources']), 1)
        self.assertEqual(len(topo['users']), 2)

    def test_topology_has_site_info(self):
        client = _MockClient()
        topo = build_site_topology(client)
        self.assertIn('site_info', topo)


class TestBuildDependencyGraph(unittest.TestCase):

    def test_builds_graph(self):
        topo = {
            'workbooks': [
                {'id': 'wb1', 'name': 'WB1', 'datasource_ids': ['ds1']},
                {'id': 'wb2', 'name': 'WB2', 'datasource_ids': ['ds1', 'ds2']},
            ],
            'datasources': [
                {'id': 'ds1', 'name': 'DS1'},
                {'id': 'ds2', 'name': 'DS2'},
            ],
        }
        graph = build_dependency_graph(topo)
        self.assertIsInstance(graph, dict)

    def test_empty_topology(self):
        topo = {'workbooks': [], 'datasources': []}
        graph = build_dependency_graph(topo)
        self.assertIsInstance(graph, dict)


class TestClassifyUsage(unittest.TestCase):

    def test_classifies_workbooks(self):
        topology = {
            'workbooks': [
                {'id': 'wb1', 'name': 'Active', 'usage': {'viewCount': '100'},
                 'updatedAt': '2025-01-01'},
                {'id': 'wb2', 'name': 'Stale', 'usage': {'viewCount': '0'},
                 'updatedAt': '2020-01-01'},
            ],
            'datasources': [],
        }
        result = classify_usage(topology)
        self.assertIn('active', result)
        self.assertIn('stale', result)


class TestAuditCertifications(unittest.TestCase):

    def test_audit(self):
        topology = {
            'workbooks': [],
            'datasources': [
                {'id': 'ds1', 'name': 'DS1', 'isCertified': True},
                {'id': 'ds2', 'name': 'DS2', 'isCertified': False},
            ],
        }
        result = audit_certifications(topology)
        self.assertIn('certified', result)
        self.assertEqual(len(result['certified']), 1)


class TestSaveLoadTopology(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_save_and_load(self):
        topo = {'workbooks': [{'id': 'wb1', 'name': 'Test'}], 'datasources': []}
        path = save_topology(topo, os.path.join(self.tmpdir, 'topology.json'))
        self.assertTrue(os.path.isfile(path))
        loaded = load_topology(path)
        self.assertEqual(loaded['workbooks'][0]['name'], 'Test')


class TestGenerateTopologyReport(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_generates_html(self):
        topo = {
            'site_info': {'name': 'Test'},
            'workbooks': [{'id': 'wb1', 'name': 'WB1', 'project': {'name': 'P'}}],
            'datasources': [{'id': 'ds1', 'name': 'DS1'}],
            'users': [{'name': 'alice', 'siteRole': 'Creator'}],
            'groups': [],
            'projects': [{'name': 'P'}],
        }
        graph = {'datasource_dependents': {}}
        usage = {'active': [], 'stale': [], 'dormant': [], 'unknown': [],
                 'summary': {'active_count': 0, 'stale_count': 0,
                             'dormant_count': 0, 'unknown_count': 0}}
        cert = {'certified': [], 'warned': [], 'uncertified': [],
                'warnings': [],
                'summary': {'certified_count': 0, 'warned_count': 0,
                            'uncertified_count': 0, 'warning_count': 0}}
        out = os.path.join(self.tmpdir, 'report.html')
        result = generate_topology_report(topo, graph, usage, cert, out)
        self.assertTrue(os.path.isfile(result))
        with open(result, encoding='utf-8') as f:
            html = f.read()
        self.assertIn('html', html.lower())


if __name__ == '__main__':
    unittest.main()
