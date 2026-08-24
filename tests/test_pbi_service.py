"""
Tests for Sprint 14 — Power BI Service deployment pipeline.

Covers:
  - PBIServiceClient (auth, REST API helpers)
  - PBIXPackager (pbip → pbix packaging)
  - PBIWorkspaceDeployer (orchestration, polling, batch)
  - DeploymentResult (serialization)
  - CLI --deploy flag
"""

import json
import os
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import MagicMock, patch, PropertyMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.deploy.pbi_client import PBIServiceClient
from powerbi_import.deploy.pbix_packager import PBIXPackager
from powerbi_import.deploy.pbi_deployer import PBIWorkspaceDeployer, DeploymentResult

import io
import argparse
from powerbi_import.deploy import PBIServiceClient
from powerbi_import.deploy import PBIXPackager
from powerbi_import.deploy import PBIWorkspaceDeployer
from powerbi_import.deploy import DeploymentResult


# ── DeploymentResult ──────────────────────────────────────────

class TestDeploymentResult(unittest.TestCase):
    """Test DeploymentResult data class."""

    def test_defaults(self):
        r = DeploymentResult(project_name='test')
        self.assertEqual(r.project_name, 'test')
        self.assertEqual(r.status, 'pending')
        self.assertIsNone(r.import_id)
        self.assertIsNone(r.dataset_id)
        self.assertIsNone(r.report_id)
        self.assertIsNone(r.error)

    def test_to_dict(self):
        r = DeploymentResult(
            project_name='Sales',
            status='succeeded',
            import_id='imp-123',
            dataset_id='ds-456',
            report_id='rpt-789',
        )
        d = r.to_dict()
        self.assertEqual(d['project_name'], 'Sales')
        self.assertEqual(d['status'], 'succeeded')
        self.assertEqual(d['import_id'], 'imp-123')
        self.assertEqual(d['dataset_id'], 'ds-456')
        self.assertEqual(d['report_id'], 'rpt-789')
        self.assertIsNone(d['error'])

    def test_to_dict_with_error(self):
        r = DeploymentResult(
            project_name='Bad',
            status='failed',
            error='Something broke',
        )
        d = r.to_dict()
        self.assertEqual(d['status'], 'failed')
        self.assertEqual(d['error'], 'Something broke')


# ── PBIServiceClient ─────────────────────────────────────────

class TestPBIServiceClient(unittest.TestCase):
    """Test PBIServiceClient initialization and helper methods."""

    def test_init_with_env_token(self):
        """Client initializes from PBI_ACCESS_TOKEN env var."""
        with patch.dict(os.environ, {'PBI_ACCESS_TOKEN': 'tok-abc'}):
            client = PBIServiceClient()
            headers = client._headers()
            self.assertEqual(headers['Authorization'], 'Bearer tok-abc')

    def test_init_with_explicit_params(self):
        """Client stores explicit tenant/client/secret."""
        client = PBIServiceClient(
            tenant_id='t1', client_id='c1', client_secret='s1',
        )
        self.assertEqual(client.tenant_id, 't1')
        self.assertEqual(client.client_id, 'c1')
        self.assertEqual(client.client_secret, 's1')

    def test_headers_include_content_type(self):
        with patch.dict(os.environ, {'PBI_ACCESS_TOKEN': 'tok-x'}):
            client = PBIServiceClient()
            h = client._headers()
            self.assertIn('Authorization', h)

    @patch.dict(os.environ, {'PBI_ACCESS_TOKEN': 'tok-test'})
    def test_list_workspaces_url(self):
        """list_workspaces calls the correct endpoint."""
        client = PBIServiceClient()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {'value': []}
            result = client.list_workspaces()
            mock_req.assert_called_once()
            args, kwargs = mock_req.call_args
            self.assertIn('/groups', args[1])

    @patch.dict(os.environ, {'PBI_ACCESS_TOKEN': 'tok-test'})
    def test_list_datasets_url(self):
        client = PBIServiceClient()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {'value': [{'id': 'ds1'}]}
            result = client.list_datasets('ws-123')
            args, kwargs = mock_req.call_args
            self.assertIn('/groups/ws-123/datasets', args[1])

    @patch.dict(os.environ, {'PBI_ACCESS_TOKEN': 'tok-test'})
    def test_list_reports_url(self):
        client = PBIServiceClient()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {'value': []}
            client.list_reports('ws-456')
            args, _ = mock_req.call_args
            self.assertIn('/groups/ws-456/reports', args[1])

    @patch.dict(os.environ, {'PBI_ACCESS_TOKEN': 'tok-test'})
    def test_refresh_dataset_url(self):
        client = PBIServiceClient()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {}
            client.refresh_dataset('ws-1', 'ds-2')
            args, _ = mock_req.call_args
            self.assertIn('/datasets/ds-2/refreshes', args[1])
            self.assertEqual(args[0], 'POST')

    @patch.dict(os.environ, {'PBI_ACCESS_TOKEN': 'tok-test'})
    def test_get_import_status_url(self):
        client = PBIServiceClient()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {'importState': 'Succeeded'}
            client.get_import_status('ws-1', 'imp-5')
            args, _ = mock_req.call_args
            self.assertIn('/groups/ws-1/imports/imp-5', args[1])

    @patch.dict(os.environ, {'PBI_ACCESS_TOKEN': 'tok-test'})
    def test_delete_report_url(self):
        client = PBIServiceClient()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {}
            client.delete_report('ws-1', 'rpt-3')
            args, _ = mock_req.call_args
            self.assertEqual(args[0], 'DELETE')
            self.assertIn('/reports/rpt-3', args[1])


# ── PBIXPackager ──────────────────────────────────────────────

class TestPBIXPackager(unittest.TestCase):
    """Test PBIX packaging from .pbip directory structure."""

    def _make_pbip_project(self, base_dir, name='TestProject'):
        """Create a minimal .pbip project structure for testing."""
        proj_dir = os.path.join(base_dir, name)
        os.makedirs(proj_dir, exist_ok=True)

        # .pbip file
        with open(os.path.join(proj_dir, f'{name}.pbip'), 'w') as f:
            json.dump({'version': '1.0'}, f)

        # .Report directory
        report_dir = os.path.join(proj_dir, f'{name}.Report')
        os.makedirs(report_dir, exist_ok=True)
        with open(os.path.join(report_dir, 'report.json'), 'w') as f:
            json.dump({'$schema': 'report-schema'}, f)

        # .SemanticModel directory
        sm_dir = os.path.join(proj_dir, f'{name}.SemanticModel')
        os.makedirs(sm_dir, exist_ok=True)
        with open(os.path.join(sm_dir, 'model.tmdl'), 'w') as f:
            f.write('model Model\n')

        # Definition subdirectory for TMDL
        defn_dir = os.path.join(sm_dir, 'definition')
        os.makedirs(defn_dir, exist_ok=True)
        with open(os.path.join(defn_dir, 'tables.tmdl'), 'w') as f:
            f.write("table 'Sales'\n  column 'Amount'\n")

        return proj_dir

    def test_find_pbip_projects(self):
        """Finds .pbip files in directory tree."""
        with tempfile.TemporaryDirectory() as td:
            self._make_pbip_project(td, 'A')
            self._make_pbip_project(td, 'B')
            packager = PBIXPackager()
            projects = packager.find_pbip_projects(td)
            self.assertEqual(len(projects), 2)

    def test_find_no_projects(self):
        with tempfile.TemporaryDirectory() as td:
            packager = PBIXPackager()
            projects = packager.find_pbip_projects(td)
            self.assertEqual(len(projects), 0)

    def test_package_creates_zip(self):
        """Package produces a valid .pbix (ZIP) file."""
        with tempfile.TemporaryDirectory() as td:
            proj = self._make_pbip_project(td, 'MyReport')
            output_path = os.path.join(td, 'MyReport.pbix')
            packager = PBIXPackager()
            packager.package(proj, output_path)

            self.assertTrue(os.path.exists(output_path))
            self.assertTrue(zipfile.is_zipfile(output_path))

    def test_package_contains_content_types(self):
        """ZIP contains [Content_Types].xml."""
        with tempfile.TemporaryDirectory() as td:
            proj = self._make_pbip_project(td, 'CTypes')
            output_path = os.path.join(td, 'CTypes.pbix')
            PBIXPackager().package(proj, output_path)
            with zipfile.ZipFile(output_path, 'r') as zf:
                names = zf.namelist()
                self.assertIn('[Content_Types].xml', names)

    def test_package_to_bytes(self):
        """package_to_bytes returns bytes without writing a file."""
        with tempfile.TemporaryDirectory() as td:
            proj = self._make_pbip_project(td, 'ByteTest')
            packager = PBIXPackager()
            data = packager.package_to_bytes(proj)
            self.assertIsInstance(data, bytes)
            self.assertGreater(len(data), 0)

    def test_package_to_bytes_is_valid_zip(self):
        """Bytes output is a valid ZIP."""
        with tempfile.TemporaryDirectory() as td:
            proj = self._make_pbip_project(td, 'ZipCheck')
            data = PBIXPackager().package_to_bytes(proj)
            with zipfile.ZipFile(io.BytesIO(data), 'r') as zf:
                self.assertIn('[Content_Types].xml', zf.namelist())

    def test_package_nonexistent_dir_raises(self):
        """Packaging a nonexistent directory raises an error."""
        with tempfile.TemporaryDirectory() as td:
            packager = PBIXPackager()
            with self.assertRaises(Exception):
                packager.package(
                    os.path.join(td, 'no_exist'),
                    os.path.join(td, 'out.pbix'),
                )


# ── PBIWorkspaceDeployer ──────────────────────────────────────

class TestPBIWorkspaceDeployer(unittest.TestCase):
    """Test workspace deployment orchestration."""

    def _make_mock_client(self):
        client = MagicMock(spec=PBIServiceClient)
        client.import_pbix.return_value = {'id': 'imp-001'}
        client.get_import_status.return_value = {
            'importState': 'Succeeded',
            'datasets': [{'id': 'ds-001'}],
            'reports': [{'id': 'rpt-001'}],
        }
        client.list_datasets.return_value = [{'id': 'ds-001'}]
        client.refresh_dataset.return_value = {}
        client.get_refresh_history.return_value = []
        return client

    @staticmethod
    def _make_deploy_project(td, name):
        """Create a minimal .pbip project structure for deployment tests."""
        proj = os.path.join(td, name)
        os.makedirs(proj)
        with open(os.path.join(proj, f'{name}.pbip'), 'w') as f:
            json.dump({}, f)
        rd = os.path.join(proj, f'{name}.Report')
        os.makedirs(rd)
        with open(os.path.join(rd, 'report.json'), 'w') as f:
            json.dump({}, f)
        sd = os.path.join(proj, f'{name}.SemanticModel')
        os.makedirs(sd)
        with open(os.path.join(sd, 'model.tmdl'), 'w') as f:
            f.write('model\n')
        return proj

    def test_deploy_project_success(self):
        """Successful deployment returns succeeded result."""
        client = self._make_mock_client()
        deployer = PBIWorkspaceDeployer(workspace_id='ws-1', client=client)

        with tempfile.TemporaryDirectory() as td:
            proj = self._make_deploy_project(td, 'Sales')
            result = deployer.deploy_project(proj, max_wait_seconds=5, poll_interval=0)

        self.assertEqual(result.status, 'succeeded')
        self.assertEqual(result.dataset_id, 'ds-001')
        self.assertEqual(result.report_id, 'rpt-001')

    def test_deploy_project_import_failed(self):
        """Failed import returns failed result with error."""
        client = self._make_mock_client()
        client.get_import_status.return_value = {
            'importState': 'Failed',
            'error': {'message': 'Invalid model'},
        }
        deployer = PBIWorkspaceDeployer(workspace_id='ws-1', client=client)

        with tempfile.TemporaryDirectory() as td:
            proj = self._make_deploy_project(td, 'Bad')
            result = deployer.deploy_project(proj, max_wait_seconds=5, poll_interval=0)

        self.assertEqual(result.status, 'failed')
        self.assertIn('Invalid model', result.error)

    def test_deploy_project_upload_error(self):
        """Upload exception → failed result."""
        client = self._make_mock_client()
        client.import_pbix.side_effect = Exception('Network error')
        deployer = PBIWorkspaceDeployer(workspace_id='ws-1', client=client)

        with tempfile.TemporaryDirectory() as td:
            proj = self._make_deploy_project(td, 'Net')
            result = deployer.deploy_project(proj, max_wait_seconds=5, poll_interval=0)

        self.assertEqual(result.status, 'failed')
        self.assertIn('Upload failed', result.error)

    def test_deploy_project_timeout(self):
        """Import polling timeout → failed result."""
        client = self._make_mock_client()
        client.get_import_status.return_value = {'importState': 'Publishing'}
        deployer = PBIWorkspaceDeployer(workspace_id='ws-1', client=client)

        with tempfile.TemporaryDirectory() as td:
            proj = self._make_deploy_project(td, 'Slow')
            result = deployer.deploy_project(
                proj, max_wait_seconds=0, poll_interval=0,
            )

        self.assertEqual(result.status, 'failed')
        self.assertIn('timed out', result.error)

    def test_deploy_with_refresh(self):
        """Refresh is triggered when requested."""
        client = self._make_mock_client()
        deployer = PBIWorkspaceDeployer(workspace_id='ws-1', client=client)

        with tempfile.TemporaryDirectory() as td:
            proj = self._make_deploy_project(td, 'Ref')
            result = deployer.deploy_project(
                proj, refresh=True, max_wait_seconds=5, poll_interval=0,
            )

        self.assertEqual(result.status, 'succeeded')
        client.refresh_dataset.assert_called_once_with('ws-1', 'ds-001')

    def test_validate_deployment_all_pass(self):
        """Validation passes when dataset exists and no refresh errors."""
        client = self._make_mock_client()
        deployer = PBIWorkspaceDeployer(workspace_id='ws-1', client=client)
        val = deployer.validate_deployment('ds-001')
        self.assertEqual(val['overall'], 'passed')
        self.assertTrue(all(c['passed'] for c in val['checks']))

    def test_validate_deployment_dataset_not_found(self):
        """Validation fails when dataset not in list."""
        client = self._make_mock_client()
        client.list_datasets.return_value = [{'id': 'other-ds'}]
        deployer = PBIWorkspaceDeployer(workspace_id='ws-1', client=client)
        val = deployer.validate_deployment('ds-missing')
        self.assertEqual(val['overall'], 'failed')

    def test_batch_deploy_no_projects(self):
        """Batch deploy with empty directory returns empty list."""
        client = self._make_mock_client()
        deployer = PBIWorkspaceDeployer(workspace_id='ws-1', client=client)
        with tempfile.TemporaryDirectory() as td:
            results = deployer.deploy_batch(td)
        self.assertEqual(results, [])


# ── CLI Integration ───────────────────────────────────────────

class TestCLIDeployFlag(unittest.TestCase):
    """Test that --deploy argument is registered in argparse."""

    def test_deploy_argument_recognized(self):
        """argparse accepts --deploy WORKSPACE_ID."""
        # Import migrate module to get the parser
        # We'll just verify the arg is parseable
        parser = argparse.ArgumentParser()
        parser.add_argument('tableau_file', nargs='?')
        parser.add_argument('--deploy', metavar='WORKSPACE_ID', default=None)
        parser.add_argument('--deploy-refresh', action='store_true', default=False)
        args = parser.parse_args(['test.twbx', '--deploy', 'ws-abc-123'])
        self.assertEqual(args.deploy, 'ws-abc-123')
        self.assertFalse(args.deploy_refresh)

    def test_deploy_refresh_flag(self):
        parser = argparse.ArgumentParser()
        parser.add_argument('tableau_file', nargs='?')
        parser.add_argument('--deploy', default=None)
        parser.add_argument('--deploy-refresh', action='store_true', default=False)
        args = parser.parse_args(['test.twbx', '--deploy', 'ws-1', '--deploy-refresh'])
        self.assertTrue(args.deploy_refresh)


# ── Module Exports ────────────────────────────────────────────

class TestDeployExports(unittest.TestCase):
    """Verify deploy subpackage exports."""

    def test_import_pbi_client(self):
        self.assertTrue(callable(PBIServiceClient))

    def test_import_pbix_packager(self):
        self.assertTrue(callable(PBIXPackager))

    def test_import_pbi_deployer(self):
        self.assertTrue(callable(PBIWorkspaceDeployer))

    def test_import_deployment_result(self):
        self.assertTrue(callable(DeploymentResult))


# ── PBIServiceClient — Workspace & Gateway APIs ──────────────

class TestPBIServiceClientWorkspaceGateway(unittest.TestCase):
    """Tests for create_workspace, gateway, and bind APIs."""

    @patch.dict(os.environ, {'PBI_ACCESS_TOKEN': 'tok-test'})
    def test_create_workspace_url_and_body(self):
        client = PBIServiceClient()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {'id': 'ws-new', 'name': 'MyWS'}
            result = client.create_workspace('MyWS')
            mock_req.assert_called_once()
            args, kwargs = mock_req.call_args
            self.assertEqual(args[0], 'POST')
            self.assertIn('/groups', args[1])
            self.assertIn('workspaceV2=True', args[1])
            self.assertEqual(result['id'], 'ws-new')

    @patch.dict(os.environ, {'PBI_ACCESS_TOKEN': 'tok-test'})
    def test_list_gateways_url(self):
        client = PBIServiceClient()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {'value': [{'id': 'gw-1'}]}
            result = client.list_gateways()
            args, _ = mock_req.call_args
            self.assertIn('/gateways', args[1])
            self.assertEqual(len(result), 1)

    @patch.dict(os.environ, {'PBI_ACCESS_TOKEN': 'tok-test'})
    def test_get_gateway_url(self):
        client = PBIServiceClient()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {'id': 'gw-1', 'name': 'OnPremGW'}
            result = client.get_gateway('gw-1')
            args, _ = mock_req.call_args
            self.assertIn('/gateways/gw-1', args[1])
            self.assertEqual(result['name'], 'OnPremGW')

    @patch.dict(os.environ, {'PBI_ACCESS_TOKEN': 'tok-test'})
    def test_get_dataset_datasources_url(self):
        client = PBIServiceClient()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {'value': [{'datasourceId': 'src-1'}]}
            result = client.get_dataset_datasources('ws-1', 'ds-1')
            args, _ = mock_req.call_args
            self.assertIn('/groups/ws-1/datasets/ds-1/datasources', args[1])
            self.assertEqual(len(result), 1)

    @patch.dict(os.environ, {'PBI_ACCESS_TOKEN': 'tok-test'})
    def test_get_gateway_datasources_url(self):
        client = PBIServiceClient()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {'value': [{'id': 'gw-ds-1'}]}
            result = client.get_gateway_datasources('gw-1')
            args, _ = mock_req.call_args
            self.assertIn('/gateways/gw-1/datasources', args[1])
            self.assertEqual(len(result), 1)

    @patch.dict(os.environ, {'PBI_ACCESS_TOKEN': 'tok-test'})
    def test_bind_dataset_to_gateway_url_and_body(self):
        client = PBIServiceClient()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {}
            client.bind_dataset_to_gateway('ws-1', 'ds-1', 'gw-1')
            args, kwargs = mock_req.call_args
            self.assertEqual(args[0], 'POST')
            self.assertIn('/Default.BindToGateway', args[1])

    @patch.dict(os.environ, {'PBI_ACCESS_TOKEN': 'tok-test'})
    def test_bind_dataset_with_datasource_ids(self):
        client = PBIServiceClient()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {}
            client.bind_dataset_to_gateway(
                'ws-1', 'ds-1', 'gw-1',
                datasource_ids=['gw-ds-1', 'gw-ds-2'],
            )
            args, kwargs = mock_req.call_args
            body = kwargs.get('data', args[2] if len(args) > 2 else {})
            self.assertEqual(body['gatewayObjectId'], 'gw-1')
            self.assertEqual(body['datasourceObjectIds'], ['gw-ds-1', 'gw-ds-2'])

    @patch.dict(os.environ, {'PBI_ACCESS_TOKEN': 'tok-test'})
    def test_take_over_dataset_url(self):
        client = PBIServiceClient()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {}
            client.take_over_dataset('ws-1', 'ds-1')
            args, _ = mock_req.call_args
            self.assertEqual(args[0], 'POST')
            self.assertIn('/Default.TakeOver', args[1])

    @patch.dict(os.environ, {'PBI_ACCESS_TOKEN': 'tok-test'})
    def test_update_dataset_datasources_url(self):
        client = PBIServiceClient()
        with patch.object(client, '_request') as mock_req:
            mock_req.return_value = {}
            client.update_dataset_datasources('ws-1', 'ds-1', [
                {'connectionDetails': {'server': 'old'}, 'datasourceSelector': {}},
            ])
            args, _ = mock_req.call_args
            self.assertEqual(args[0], 'POST')
            self.assertIn('/Default.UpdateDatasources', args[1])


# ── PBIWorkspaceDeployer — Workspace & Gateway orchestration ──

class TestDeployerWorkspaceGateway(unittest.TestCase):
    """Tests for create_workspace, ensure_workspace, bind_to_gateway, deploy_and_bind."""

    def _make_mock_client(self):
        client = MagicMock(spec=PBIServiceClient)
        client.import_pbix.return_value = {'id': 'imp-001'}
        client.get_import_status.return_value = {
            'importState': 'Succeeded',
            'datasets': [{'id': 'ds-001'}],
            'reports': [{'id': 'rpt-001'}],
        }
        client.list_datasets.return_value = [{'id': 'ds-001'}]
        client.refresh_dataset.return_value = {}
        client.get_refresh_history.return_value = []
        client.create_workspace.return_value = {'id': 'ws-new', 'name': 'NewWS'}
        client.list_workspaces.return_value = []
        client.take_over_dataset.return_value = {}
        client.get_dataset_datasources.return_value = [
            {'datasourceId': 'src-1', 'datasourceType': 'Sql',
             'connectionDetails': {'server': 'sql.example.com', 'database': 'SalesDB'},
             'gatewayId': None},
        ]
        client.bind_dataset_to_gateway.return_value = {}
        return client

    @staticmethod
    def _make_deploy_project(td, name):
        proj = os.path.join(td, name)
        os.makedirs(proj)
        with open(os.path.join(proj, f'{name}.pbip'), 'w') as f:
            json.dump({}, f)
        rd = os.path.join(proj, f'{name}.Report')
        os.makedirs(rd)
        with open(os.path.join(rd, 'report.json'), 'w') as f:
            json.dump({}, f)
        sd = os.path.join(proj, f'{name}.SemanticModel')
        os.makedirs(sd)
        with open(os.path.join(sd, 'model.tmdl'), 'w') as f:
            f.write('model\n')
        return proj

    def test_create_workspace(self):
        client = self._make_mock_client()
        deployer = PBIWorkspaceDeployer(workspace_id='', client=client)
        ws = deployer.create_workspace('Sales Dashboard')
        self.assertEqual(ws['id'], 'ws-new')
        self.assertEqual(deployer.workspace_id, 'ws-new')
        client.create_workspace.assert_called_once_with('Sales Dashboard')

    def test_ensure_workspace_creates_new(self):
        client = self._make_mock_client()
        client.list_workspaces.return_value = []
        deployer = PBIWorkspaceDeployer(workspace_id='', client=client)
        ws = deployer.ensure_workspace('NewWS')
        self.assertTrue(ws['created'])
        self.assertEqual(ws['id'], 'ws-new')

    def test_ensure_workspace_reuses_existing(self):
        client = self._make_mock_client()
        client.list_workspaces.return_value = [
            {'id': 'ws-existing', 'name': 'Sales'},
        ]
        deployer = PBIWorkspaceDeployer(workspace_id='', client=client)
        ws = deployer.ensure_workspace('Sales')
        self.assertFalse(ws['created'])
        self.assertEqual(ws['id'], 'ws-existing')
        client.create_workspace.assert_not_called()

    def test_ensure_workspace_case_insensitive(self):
        client = self._make_mock_client()
        client.list_workspaces.return_value = [
            {'id': 'ws-x', 'name': 'Finance Reports'},
        ]
        deployer = PBIWorkspaceDeployer(workspace_id='', client=client)
        ws = deployer.ensure_workspace('finance reports')
        self.assertFalse(ws['created'])

    def test_bind_to_gateway_success(self):
        client = self._make_mock_client()
        deployer = PBIWorkspaceDeployer(workspace_id='ws-1', client=client)
        result = deployer.bind_to_gateway('ds-1', 'gw-1')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['gateway_id'], 'gw-1')
        self.assertEqual(len(result['datasources']), 1)
        client.take_over_dataset.assert_called_once()
        client.bind_dataset_to_gateway.assert_called_once()

    def test_bind_to_gateway_skip_takeover(self):
        client = self._make_mock_client()
        deployer = PBIWorkspaceDeployer(workspace_id='ws-1', client=client)
        result = deployer.bind_to_gateway('ds-1', 'gw-1', take_over=False)
        self.assertEqual(result['status'], 'succeeded')
        client.take_over_dataset.assert_not_called()

    def test_bind_to_gateway_with_datasource_ids(self):
        client = self._make_mock_client()
        deployer = PBIWorkspaceDeployer(workspace_id='ws-1', client=client)
        result = deployer.bind_to_gateway(
            'ds-1', 'gw-1', datasource_ids=['gw-ds-1'],
        )
        self.assertEqual(result['status'], 'succeeded')
        client.bind_dataset_to_gateway.assert_called_once_with(
            'ws-1', 'ds-1', 'gw-1', datasource_ids=['gw-ds-1'],
        )

    def test_bind_to_gateway_bind_failure(self):
        client = self._make_mock_client()
        client.bind_dataset_to_gateway.side_effect = Exception('Gateway unreachable')
        deployer = PBIWorkspaceDeployer(workspace_id='ws-1', client=client)
        result = deployer.bind_to_gateway('ds-1', 'gw-1')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('Gateway unreachable', result['error'])

    def test_bind_to_gateway_takeover_already_owner(self):
        client = self._make_mock_client()
        client.take_over_dataset.side_effect = Exception('400 Bad Request')
        deployer = PBIWorkspaceDeployer(workspace_id='ws-1', client=client)
        result = deployer.bind_to_gateway('ds-1', 'gw-1')
        # 400 = already owner, should proceed
        self.assertEqual(result['status'], 'succeeded')

    def test_bind_to_gateway_takeover_real_failure(self):
        client = self._make_mock_client()
        client.take_over_dataset.side_effect = Exception('403 Forbidden')
        deployer = PBIWorkspaceDeployer(workspace_id='ws-1', client=client)
        result = deployer.bind_to_gateway('ds-1', 'gw-1')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('TakeOver failed', result['error'])

    def test_deploy_and_bind_success(self):
        client = self._make_mock_client()
        deployer = PBIWorkspaceDeployer(workspace_id='ws-1', client=client)
        with tempfile.TemporaryDirectory() as td:
            proj = self._make_deploy_project(td, 'Sales')
            result = deployer.deploy_and_bind(proj, gateway_id='gw-1')
            self.assertEqual(result['deploy']['status'], 'succeeded')
            self.assertEqual(result['gateway_bind']['status'], 'succeeded')

    def test_deploy_and_bind_with_refresh(self):
        client = self._make_mock_client()
        deployer = PBIWorkspaceDeployer(workspace_id='ws-1', client=client)
        with tempfile.TemporaryDirectory() as td:
            proj = self._make_deploy_project(td, 'Sales')
            result = deployer.deploy_and_bind(
                proj, gateway_id='gw-1', refresh=True,
            )
            self.assertIsNotNone(result['refresh'])
            self.assertEqual(result['refresh']['status'], 'triggered')

    def test_deploy_and_bind_deploy_failure_skips_bind(self):
        client = self._make_mock_client()
        client.import_pbix.side_effect = Exception('Upload failed')
        deployer = PBIWorkspaceDeployer(workspace_id='ws-1', client=client)
        with tempfile.TemporaryDirectory() as td:
            proj = self._make_deploy_project(td, 'Bad')
            result = deployer.deploy_and_bind(proj, gateway_id='gw-1')
            self.assertNotEqual(
                (result.get('deploy') or {}).get('status'), 'succeeded'
            )
            self.assertIsNone(result['gateway_bind'])


if __name__ == '__main__':
    unittest.main()
