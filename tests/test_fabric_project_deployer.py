"""Contract tests for deploying a generated Fabric-native project."""

import base64
import json
import os
import tempfile
import unittest
import uuid

from powerbi_import.deploy.deployer import FabricDeployer


ARTIFACTS = (
    ('Lakehouse', 'Lakehouse'),
    ('Dataflow', 'Dataflow'),
    ('Notebook', 'Notebook'),
    ('SemanticModel', 'SemanticModel'),
    ('Report', 'Report'),
    ('Pipeline', 'DataPipeline'),
)

EXPECTED_PARTS = {
    'Lakehouse': {'.platform', 'lakehouse.metadata.json'},
    'Dataflow': {'.platform', 'mashup.pq', 'queryMetadata.json'},
    'Notebook': {'.platform', 'artifact.content.ipynb'},
    'SemanticModel': {
        '.platform', 'definition.pbism', 'definition/expressions.tmdl'},
    'Report': {'.platform', 'definition.pbir', 'definition/report.json'},
    'DataPipeline': {'.platform', 'pipeline-content.json'},
}


class RecordingFabricClient:
    """Fabric client fake that records item discovery and API writes."""

    def __init__(self, existing=None):
        self.existing = existing or {}
        self.list_calls = []
        self.post_calls = []
        self.created_ids = {
            item_type: f'physical-{item_type.lower()}'
            for _, item_type in ARTIFACTS
        }
        self.connection_id = 'lakehouse-connection'
        self.cluster_id = 'lakehouse-cluster'
        self.upload_calls = []

    def list_items(self, workspace_id, item_type=None):
        self.list_calls.append((workspace_id, item_type))
        return {
            'value': [
                {
                    'id': item_id,
                    'displayName': 'RetailMigration',
                    'type': item_type,
                }
                for item_id in [self.existing.get(item_type)]
                if item_id
            ]
        }

    def get_workspace(self, workspace_id):
        return {
            'id': workspace_id,
            'displayName': 'Migration Workspace',
        }

    def post(self, path, data=None):
        self.post_calls.append((path, data))
        if '/jobs/instances?jobType=Pipeline' in path:
            return {'id': 'pipeline-job', 'status': 'Completed'}
        if path.endswith('/items'):
            return {'id': self.created_ids[data['type']]}
        item_id = path.split('/items/', 1)[1].split('/', 1)[0]
        return {'id': item_id}

    def get(self, path, params=None):
        if path != '/connections':
            raise AssertionError(f'Unexpected GET: {path}')
        return {
            'value': [{
                'id': self.connection_id,
                'connectionDetails': {
                    'type': 'Lakehouse',
                    'parameters': [
                        {
                            'name': 'workspaceId',
                            'value': TestFabricProjectDeployer.workspace_id,
                        },
                        {
                            'name': 'lakehouseId',
                            'value': self.existing.get(
                                'Lakehouse', self.created_ids['Lakehouse']),
                        },
                    ],
                },
            }],
        }

    def get_absolute(self, url):
        if url != ('https://api.powerbi.com/v2.0/myorg/me/'
                   'gatewayClusterDatasources'):
            raise AssertionError(f'Unexpected absolute GET: {url}')
        return {
            'value': [{
                'id': self.connection_id,
                'clusterId': self.cluster_id,
            }],
        }

    def upload_onelake_file(self, workspace_id, lakehouse_id, path, content):
        self.upload_calls.append((workspace_id, lakehouse_id, path, content))
        return {'path': path, 'size': len(content)}


class CreatingConnectionFabricClient(RecordingFabricClient):
    """Client fake that creates a missing Lakehouse connection."""

    def __init__(self):
        super().__init__()
        self.connection_payload = None

    def get(self, path, params=None):
        if path == '/connections':
            return {'value': []}
        if path == '/connections/supportedConnectionTypes':
            return {
                'value': [{
                    'type': 'Lakehouse',
                    'supportedCredentialTypes': ['WorkspaceIdentity'],
                    'creationMethods': [{
                        'name': 'Lakehouse',
                        'parameters': [
                            {'name': 'workspaceId', 'dataType': 'Text'},
                            {'name': 'lakehouseId', 'dataType': 'Text'},
                        ],
                    }],
                }],
            }
        raise AssertionError(f'Unexpected GET: {path}')

    def post(self, path, data=None):
        if path == '/connections':
            self.connection_payload = data
            return {'id': self.connection_id}
        return super().post(path, data)


class InaccessibleWorkspaceFabricClient(RecordingFabricClient):
    """Client fake that rejects the read-only workspace preflight."""

    def get_workspace(self, workspace_id):
        raise PermissionError(f'No access to workspace {workspace_id}')


class TestFabricProjectDeployer(unittest.TestCase):
    """Define the deployment contract for a six-artifact Fabric project."""

    workspace_id = 'physical-workspace'
    project_name = 'RetailMigration'

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.logical_ids = {
            suffix: str(uuid.uuid5(uuid.NAMESPACE_DNS, f'test-{suffix}'))
            for suffix, _ in ARTIFACTS
        }
        self.logical_workspace_id = str(
            uuid.uuid5(uuid.NAMESPACE_DNS, 'test-Workspace')
        )
        self.project_dir = os.path.join(self.temp_dir.name, self.project_name)
        os.makedirs(self.project_dir)
        self._write_project()

    def _write_json(self, path, value):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2)

    def _write_text(self, path, value):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as stream:
            stream.write(value)

    def _artifact_dir(self, suffix):
        return os.path.join(
            self.project_dir, f'{self.project_name}.{suffix}')

    def _write_platform(self, suffix, item_type):
        self._write_json(
            os.path.join(self._artifact_dir(suffix), '.platform'),
            {
                'metadata': {
                    'type': item_type,
                    'displayName': self.project_name,
                },
                'config': {'logicalId': self.logical_ids[suffix]},
            },
        )

    def _write_project(self):
        for suffix, item_type in ARTIFACTS:
            self._write_platform(suffix, item_type)

        self._write_json(
            os.path.join(
                self._artifact_dir('Lakehouse'), 'lakehouse_definition.json'),
            {'tables': [{'name': 'Orders'}]},
        )
        self._write_json(
            os.path.join(
                self._artifact_dir('Lakehouse'), 'lakehouse.metadata.json'),
            {'defaultSchema': 'dbo'},
        )
        self._write_text(
            os.path.join(self._artifact_dir('Dataflow'), 'mashup.pq'),
            'section Section1;\n'
            'shared Orders = Lakehouse.Contents(){'
            f'[workspaceId="{self.logical_workspace_id}"]}}[Data]{{'
            f'[lakehouseId="{self.logical_ids["Lakehouse"]}"]}}[Data];\n',
        )
        self._write_json(
            os.path.join(
                self._artifact_dir('Dataflow'), 'queryMetadata.json'),
            {'queriesMetadata': {'Orders': {'queryName': 'Orders'}}},
        )
        self._write_json(
            os.path.join(
                self._artifact_dir('Dataflow'), 'dataflow_definition.json'),
            {'queries': [{'name': 'Orders'}]},
        )
        self._write_json(
            os.path.join(
                self._artifact_dir('Notebook'), 'artifact.content.ipynb'),
            {
                'cells': [{
                    'cell_type': 'code',
                    'source': ['df = spark.read.load("Files/orders.csv")'],
                }],
                'metadata': {
                    'lakehouse_id': self.logical_ids['Lakehouse'],
                    'workspace_id': self.logical_workspace_id,
                    'dependencies': {'lakehouse': {
                        'default_lakehouse': self.logical_ids['Lakehouse'],
                        'default_lakehouse_workspace_id': (
                            self.logical_workspace_id),
                    }},
                },
                'nbformat': 4,
                'nbformat_minor': 5,
            },
        )
        data_dir = os.path.join(self.project_dir, 'Data')
        os.makedirs(data_dir, exist_ok=True)
        with open(os.path.join(data_dir, 'orders.csv'), 'wb') as stream:
            stream.write(b'OrderID\n1\n')
        self._write_text(
            os.path.join(
                self._artifact_dir('SemanticModel'),
                'definition', 'expressions.tmdl'),
            'expression DirectLake = AnalysisServices.Database('
            f'"{self.logical_workspace_id}", '
            f'"{self.logical_ids["Lakehouse"]}")\n',
        )
        self._write_json(
            os.path.join(
                self._artifact_dir('SemanticModel'), 'definition.pbism'),
            {'version': '4.2'},
        )
        self._write_json(
            os.path.join(self._artifact_dir('Report'), 'definition.pbir'),
            {
                'version': '4.0',
                'datasetReference': {
                    'byConnection': {
                        'pbiServiceModelId': self.logical_ids['SemanticModel'],
                        'connectionString': (
                            'Data Source=powerbi://api.powerbi.com/v1.0/myorg/'
                            f'{self.logical_workspace_id};'
                            f'Initial Catalog={self.logical_ids["SemanticModel"]}'
                        ),
                    }
                },
            },
        )
        self._write_json(
            os.path.join(
                self._artifact_dir('Report'), 'definition', 'report.json'),
            {'version': '4.0'},
        )
        pipeline = {
            'properties': {
                'activities': [
                    {
                        'type': 'RefreshDataflow',
                        'typeProperties': {
                            'workspaceId': self.logical_workspace_id,
                            'dataflowId': self.logical_ids['Dataflow'],
                        },
                    },
                    {
                        'type': 'TridentNotebook',
                        'typeProperties': {
                            'workspaceId': self.logical_workspace_id,
                            'notebookId': self.logical_ids['Notebook'],
                        },
                    },
                    {
                        'type': 'TridentDatasetRefresh',
                        'typeProperties': {
                            'workspaceId': self.logical_workspace_id,
                            'datasetId': self.logical_ids['SemanticModel'],
                        },
                    },
                ]
            }
        }
        self._write_json(
            os.path.join(
                self._artifact_dir('Pipeline'), 'pipeline-content.json'),
            pipeline,
        )
        self._write_json(
            os.path.join(
                self._artifact_dir('Pipeline'), 'pipeline_definition.json'),
            pipeline,
        )

    @staticmethod
    def _decode_parts(payload):
        decoded = {}
        for part in payload['definition']['parts']:
            if part['payloadType'] != 'InlineBase64':
                raise AssertionError('Fabric definition parts must use InlineBase64')
            decoded[part['path']] = base64.b64decode(
                part['payload']
            ).decode('utf-8')
        return decoded

    def test_deploys_six_artifacts_in_dependency_order_and_resolves_ids(self):
        client = RecordingFabricClient()
        deployer = FabricDeployer(client=client)

        result = deployer.deploy_fabric_project(
            self.workspace_id,
            self.project_dir,
            self.project_name,
        )

        create_calls = [
            (path, payload)
            for path, payload in client.post_calls
            if path.endswith('/items')
        ]
        self.assertEqual(
            [payload['type'] for _, payload in create_calls],
            [item_type for _, item_type in ARTIFACTS],
        )
        self.assertTrue(result['success'])
        self.assertEqual(result['workspace_id'], self.workspace_id)
        self.assertEqual(result['preflight'], {
            'workspace_id': self.workspace_id,
            'workspace_name': 'Migration Workspace',
            'accessible_items': 0,
        })
        self.assertEqual(
            result['item_ids'],
            client.created_ids,
        )
        self.assertEqual(client.upload_calls, [(
            self.workspace_id,
            client.created_ids['Lakehouse'],
            'Files/orders.csv',
            b'OrderID\n1\n',
        )])
        self.assertEqual(result['uploaded_files'], [
            {'path': 'Files/orders.csv', 'size': 10},
        ])

        payloads = {payload['type']: payload for _, payload in create_calls}
        for _, payload in create_calls:
            self.assertEqual(payload['displayName'], self.project_name)
            self.assertTrue(payload['definition']['parts'])
            decoded_parts = self._decode_parts(payload)
            self.assertTrue(
                EXPECTED_PARTS[payload['type']].issubset(decoded_parts),
                f'Missing official parts for {payload["type"]}',
            )

        dataflow_parts = self._decode_parts(payloads['Dataflow'])
        mashup = dataflow_parts['mashup.pq']
        self.assertIn(self.workspace_id, mashup)
        self.assertIn(client.created_ids['Lakehouse'], mashup)
        self.assertNotIn(self.logical_workspace_id, mashup)
        self.assertNotIn(self.logical_ids['Lakehouse'], mashup)
        query_metadata = json.loads(dataflow_parts['queryMetadata.json'])
        self.assertEqual(query_metadata['connections'], [{
            'connectionId': json.dumps({
                'ClusterId': client.cluster_id,
                'DatasourceId': client.connection_id,
            }, separators=(',', ':')),
            'kind': 'Lakehouse',
            'path': 'Lakehouse',
        }])

        semantic_parts = self._decode_parts(payloads['SemanticModel'])
        expressions = semantic_parts['definition/expressions.tmdl']
        self.assertIn(self.workspace_id, expressions)
        self.assertIn(client.created_ids['Lakehouse'], expressions)
        self.assertNotIn(self.logical_workspace_id, expressions)
        self.assertNotIn(self.logical_ids['Lakehouse'], expressions)

        notebook_parts = self._decode_parts(payloads['Notebook'])
        notebook = notebook_parts['artifact.content.ipynb']
        self.assertIn(client.created_ids['Lakehouse'], notebook)
        self.assertIn(self.workspace_id, notebook)
        self.assertNotIn(self.logical_ids['Lakehouse'], notebook)
        self.assertNotIn(self.logical_workspace_id, notebook)

        report_parts = self._decode_parts(payloads['Report'])
        report_binding = json.loads(report_parts['definition.pbir'])
        binding_text = json.dumps(report_binding)
        self.assertIn(client.created_ids['SemanticModel'], binding_text)
        self.assertNotIn(self.logical_ids['SemanticModel'], binding_text)

        pipeline_parts = self._decode_parts(payloads['DataPipeline'])
        pipeline = json.loads(pipeline_parts['pipeline-content.json'])
        self.assertNotIn('pipeline_definition.json', pipeline_parts)
        pipeline_text = json.dumps(pipeline)
        for expected_id in (
            self.workspace_id,
            client.created_ids['Dataflow'],
            client.created_ids['Notebook'],
            client.created_ids['SemanticModel'],
        ):
            self.assertIn(expected_id, pipeline_text)
        for logical_id in self.logical_ids.values():
            self.assertNotIn(logical_id, pipeline_text)
        self.assertNotIn(self.logical_workspace_id, pipeline_text)

    def test_overwrite_updates_existing_items_with_inline_definitions(self):
        existing = {
            item_type: f'existing-{item_type.lower()}'
            for _, item_type in ARTIFACTS
        }
        client = RecordingFabricClient(existing=existing)
        deployer = FabricDeployer(client=client)

        result = deployer.deploy_fabric_project(
            self.workspace_id,
            self.project_dir,
            self.project_name,
            overwrite=True,
        )

        self.assertFalse(any(
            path.endswith('/items') for path, _ in client.post_calls
        ))
        update_calls = client.post_calls
        self.assertEqual(len(update_calls), len(ARTIFACTS))
        self.assertEqual(
            [
                path.split('/items/', 1)[1].split('/', 1)[0]
                for path, _ in update_calls
            ],
            [existing[item_type] for _, item_type in ARTIFACTS],
        )
        for path, payload in update_calls:
            self.assertTrue(
                path.endswith('/updateDefinition?updateMetadata=true'))
            self.assertTrue(payload['definition']['parts'])
            self._decode_parts(payload)
        self.assertTrue(result['success'])
        self.assertEqual(result['item_ids'], existing)

    def test_creates_workspace_identity_lakehouse_connection_when_missing(self):
        client = CreatingConnectionFabricClient()
        deployer = FabricDeployer(client=client)

        deployer.deploy_fabric_project(
            self.workspace_id,
            self.project_dir,
            self.project_name,
        )

        payload = client.connection_payload
        self.assertIsNotNone(payload)
        self.assertEqual(payload['connectivityType'], 'ShareableCloud')
        self.assertEqual(
            payload['credentialDetails']['credentials']['credentialType'],
            'WorkspaceIdentity',
        )
        parameters = {
            item['name']: item['value']
            for item in payload['connectionDetails']['parameters']
        }
        self.assertEqual(parameters, {
            'workspaceId': self.workspace_id,
            'lakehouseId': client.created_ids['Lakehouse'],
        })

    def test_runs_deployed_data_pipeline_and_returns_job_status(self):
        client = RecordingFabricClient()
        deployer = FabricDeployer(client=client)

        result = deployer.run_fabric_pipeline(
            self.workspace_id,
            client.created_ids['DataPipeline'],
        )

        self.assertEqual(result['status'], 'Completed')
        path, payload = client.post_calls[-1]
        self.assertEqual(
            path,
            '/workspaces/physical-workspace/items/'
            'physical-datapipeline/jobs/instances?jobType=Pipeline',
        )
        self.assertEqual(payload, {})

    def test_rejects_missing_notebook_file_before_creating_items(self):
        os.remove(os.path.join(self.project_dir, 'Data', 'orders.csv'))
        client = RecordingFabricClient()
        deployer = FabricDeployer(client=client)

        with self.assertRaisesRegex(
                FileNotFoundError, 'unstaged Lakehouse file.*orders.csv'):
            deployer.deploy_fabric_project(
                self.workspace_id, self.project_dir, self.project_name)

        self.assertEqual(client.post_calls, [])

    def test_rejects_inaccessible_workspace_before_creating_items(self):
        client = InaccessibleWorkspaceFabricClient()
        deployer = FabricDeployer(client=client)

        with self.assertRaisesRegex(PermissionError, 'No access to workspace'):
            deployer.deploy_fabric_project(
                self.workspace_id, self.project_dir, self.project_name)

        self.assertEqual(client.post_calls, [])
        self.assertEqual(client.upload_calls, [])


if __name__ == '__main__':
    unittest.main()