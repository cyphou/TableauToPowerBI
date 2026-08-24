"""
Tests for Permission Mapping (powerbi_import.permission_mapper) — Sprint 167 extensions.
"""

import json
import os
import sys
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.permission_mapper import (
    generate_rls_powershell,
    generate_credential_template,
    map_site_roles,
    reconcile_rls_principals,
    generate_azure_ad_scripts,
    generate_permission_report,
)


# ── Existing function tests ─────────────────────────────────────────

class TestGenerateRlsPowershell(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_generates_ps1(self):
        roles = [
            {'name': 'EastRole', 'filter': "'Region'[Region] = \"East\"",
             'members': ['alice@co.com']},
        ]
        path = os.path.join(self.tmpdir, 'rls.ps1')
        result = generate_rls_powershell(roles, path, 'SalesModel')
        self.assertIsNotNone(result)
        with open(result, encoding='utf-8') as f:
            content = f.read()
        self.assertIn('SalesModel', content)

    def test_empty_roles(self):
        path = os.path.join(self.tmpdir, 'rls.ps1')
        result = generate_rls_powershell([], path, 'Model')
        # Should still generate or return None
        self.assertTrue(result is None or os.path.isfile(result))


# ── Sprint 167 — Site Role Mapping ──────────────────────────────────

class TestMapSiteRoles(unittest.TestCase):

    def test_maps_creator_to_admin(self):
        users = [{'name': 'alice', 'siteRole': 'Creator', 'email': 'alice@co.com'}]
        assignments = map_site_roles(users)
        self.assertEqual(len(assignments), 1)
        self.assertEqual(assignments[0]['pbi_role'], 'Admin')

    def test_maps_viewer(self):
        users = [{'name': 'bob', 'siteRole': 'Viewer', 'email': 'bob@co.com'}]
        assignments = map_site_roles(users)
        self.assertEqual(assignments[0]['pbi_role'], 'Viewer')

    def test_skips_unlicensed(self):
        users = [{'name': 'ghost', 'siteRole': 'Unlicensed'}]
        assignments = map_site_roles(users)
        self.assertEqual(len(assignments), 0)

    def test_explorer_can_publish(self):
        users = [{'name': 'carol', 'siteRole': 'ExplorerCanPublish', 'email': 'c@co.com'}]
        assignments = map_site_roles(users)
        self.assertEqual(assignments[0]['pbi_role'], 'Contributor')

    def test_with_workspace_mapping(self):
        users = [{'name': 'alice', 'siteRole': 'Creator', 'email': 'a@co.com'}]
        ws = {'Sales': 'PBI - Sales'}
        assignments = map_site_roles(users, ws)
        self.assertEqual(assignments[0]['workspaces'], ['PBI - Sales'])

    def test_empty_users(self):
        assignments = map_site_roles([])
        self.assertEqual(assignments, [])


class TestReconcileRlsPrincipals(unittest.TestCase):

    def test_resolves_by_email(self):
        roles = [{'name': 'EastRole', 'members': ['alice']}]
        users = [{'name': 'alice', 'email': 'alice@contoso.com'}]
        result = reconcile_rls_principals(roles, users)
        self.assertEqual(len(result['assignments']), 1)
        self.assertEqual(result['assignments'][0]['azure_ad_upn'], 'alice@contoso.com')
        self.assertEqual(len(result['unresolved']), 0)

    def test_flags_unresolved(self):
        roles = [{'name': 'EastRole', 'members': ['unknown_user']}]
        users = [{'name': 'alice', 'email': 'alice@co.com'}]
        result = reconcile_rls_principals(roles, users)
        self.assertEqual(len(result['unresolved']), 1)

    def test_empty_roles(self):
        result = reconcile_rls_principals([], [])
        self.assertEqual(result['assignments'], [])
        self.assertEqual(result['unresolved'], [])


class TestGenerateAzureAdScripts(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_generates_script(self):
        groups = [
            {'name': 'Analysts', 'users': [
                {'name': 'alice@co.com'}, {'name': 'bob@co.com'}
            ]},
        ]
        path = os.path.join(self.tmpdir, 'ad_groups.ps1')
        result = generate_azure_ad_scripts(groups, path)
        self.assertIsNotNone(result)
        with open(result, encoding='utf-8') as f:
            content = f.read()
        self.assertIn('Analysts', content)

    def test_no_groups(self):
        path = os.path.join(self.tmpdir, 'ad_groups.ps1')
        result = generate_azure_ad_scripts([], path)
        self.assertIsNone(result)


class TestGeneratePermissionReport(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_generates_html(self):
        assignments = [
            {'identity': 'alice@co.com', 'source_role': 'Creator',
             'pbi_role': 'Admin', 'groups': []},
        ]
        path = os.path.join(self.tmpdir, 'report.html')
        result = generate_permission_report(assignments, output_path=path)
        self.assertIsNotNone(result)
        with open(result, encoding='utf-8') as f:
            content = f.read()
        self.assertIn('alice@co.com', content)

    def test_with_rls_reconciliation(self):
        assignments = [
            {'identity': 'alice@co.com', 'source_role': 'Creator',
             'pbi_role': 'Admin', 'groups': []},
        ]
        rls = {
            'assignments': [{'role': 'R1', 'tableau_name': 'alice',
                             'azure_ad_upn': 'alice@co.com', 'principal_type': 'user'}],
            'unresolved': [{'role': 'R2', 'tableau_name': 'ghost',
                           'reason': 'No email'}],
        }
        path = os.path.join(self.tmpdir, 'report.html')
        result = generate_permission_report(assignments, rls, path)
        self.assertIsNotNone(result)
        with open(result, encoding='utf-8') as f:
            content = f.read()
        self.assertIn('html', content.lower())


if __name__ == '__main__':
    unittest.main()
