"""
Fabric artifact deployment module.

Deploys generated Power BI projects to a Microsoft Fabric workspace
via the Fabric REST API.

Requires:
    - azure-identity (pip install azure-identity)
    - requests (pip install requests) — optional, falls back to urllib

Usage:
    from deployer import FabricDeployer
    deployer = FabricDeployer()
    deployer.deploy_dataset(workspace_id, 'MyDataset', config)
"""

import base64
import os
import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


class ArtifactType:
    """Supported Fabric artifact types."""
    DATASET = 'Dataset'
    DATAFLOW = 'Dataflow'
    REPORT = 'Report'
    NOTEBOOK = 'Notebook'
    LAKEHOUSE = 'Lakehouse'
    WAREHOUSE = 'Warehouse'
    PIPELINE = 'Pipeline'
    SEMANTIC_MODEL = 'SemanticModel'


class FabricDeployer:
    """Deploy Fabric artifacts to a workspace."""

    _PBI_CLUSTER_DATASOURCES_URL = (
        'https://api.powerbi.com/v2.0/myorg/me/gatewayClusterDatasources')

    _FABRIC_PROJECT_ARTIFACTS = (
        ('Lakehouse', 'Lakehouse'),
        ('Dataflow', 'Dataflow'),
        ('Notebook', 'Notebook'),
        ('SemanticModel', 'SemanticModel'),
        ('Report', 'Report'),
        ('Pipeline', 'DataPipeline'),
    )

    def __init__(self, client=None):
        """
        Initialize Fabric Deployer.

        Args:
            client: FabricClient instance (creates default if None)
        """
        if client is None:
            from .client import FabricClient
            client = FabricClient()
        self.client = client

    def deploy_dataset(self, workspace_id, dataset_name, dataset_config,
                       overwrite=True):
        """
        Deploy a dataset / semantic model to a workspace.

        Args:
            workspace_id: Target workspace ID
            dataset_name: Name of the dataset
            dataset_config: Dataset configuration dict
            overwrite: Overwrite if exists

        Returns:
            Deployment result dict
        """
        logger.info(f'Deploying dataset: {dataset_name}')

        existing = self._find_item(workspace_id, dataset_name,
                                    ArtifactType.DATASET)

        if existing and overwrite:
            logger.info(f'Overwriting existing dataset: {existing["id"]}')
            result = self.client.put(
                f'/workspaces/{workspace_id}/items/{existing["id"]}',
                data=dataset_config,
            )
        else:
            result = self.client.post(
                f'/workspaces/{workspace_id}/items',
                data={
                    'displayName': dataset_name,
                    'type': ArtifactType.DATASET,
                    'definition': dataset_config,
                },
            )

        logger.info(f'Dataset deployed: {result.get("id", "?")}')
        return result

    def deploy_report(self, workspace_id, report_name, report_config,
                      overwrite=True):
        """
        Deploy a report to a workspace.

        Args:
            workspace_id: Target workspace ID
            report_name: Name of the report
            report_config: Report configuration dict
            overwrite: Overwrite if exists

        Returns:
            Deployment result dict
        """
        logger.info(f'Deploying report: {report_name}')

        existing = self._find_item(workspace_id, report_name,
                                    ArtifactType.REPORT)

        if existing and overwrite:
            logger.info(f'Overwriting existing report: {existing["id"]}')
            result = self.client.put(
                f'/workspaces/{workspace_id}/items/{existing["id"]}',
                data=report_config,
            )
        else:
            result = self.client.post(
                f'/workspaces/{workspace_id}/items',
                data={
                    'displayName': report_name,
                    'type': ArtifactType.REPORT,
                    'definition': report_config,
                },
            )

        logger.info(f'Report deployed: {result.get("id", "?")}')
        return result

    def deploy_from_file(self, workspace_id, artifact_path, artifact_type,
                         overwrite=True):
        """
        Deploy an artifact from a JSON file.

        Args:
            workspace_id: Target workspace ID
            artifact_path: Path to artifact JSON file
            artifact_type: Type (Dataset, Report, etc.)
            overwrite: Overwrite if exists

        Returns:
            Deployment result dict
        """
        artifact_path = Path(artifact_path)
        logger.info(f'Loading artifact from: {artifact_path}')

        with open(artifact_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        artifact_name = config.get('displayName') or artifact_path.stem

        if artifact_type == ArtifactType.DATASET:
            return self.deploy_dataset(workspace_id, artifact_name, config,
                                        overwrite)
        elif artifact_type == ArtifactType.REPORT:
            return self.deploy_report(workspace_id, artifact_name, config,
                                       overwrite)
        else:
            raise ValueError(f'Unsupported artifact type: {artifact_type}')

    def deploy_artifacts_batch(self, workspace_id, artifacts_dir,
                               overwrite=True):
        """
        Deploy all artifacts from a directory.

        Args:
            workspace_id: Target workspace ID
            artifacts_dir: Directory containing artifact JSON files
            overwrite: Overwrite existing artifacts

        Returns:
            List of deployment results
        """
        artifacts_dir = Path(artifacts_dir)
        results = []

        for artifact_file in sorted(artifacts_dir.glob('*.json')):
            try:
                logger.info(f'Processing: {artifact_file.name}')
                with open(artifact_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)

                artifact_type = config.get('type', ArtifactType.DATASET)
                result = self.deploy_from_file(
                    workspace_id, artifact_file, artifact_type, overwrite,
                )
                results.append({'file': str(artifact_file), 'result': result})
            except Exception as e:
                logger.error(f'Failed to deploy {artifact_file.name}: {e}')
                results.append({'file': str(artifact_file), 'error': str(e)})

        return results

    def deploy_fabric_project(self, workspace_id, project_dir, project_name,
                              overwrite=True):
        """Deploy a generated six-artifact Fabric-native project.

        Logical IDs embedded by the generators are resolved to the physical IDs
        returned by Fabric as each dependency is created or discovered.

        Args:
            workspace_id: Target Fabric workspace ID.
            project_dir: Directory containing the generated artifact folders.
            project_name: Generated project name and artifact folder prefix.
            overwrite: Update same-name items when they already exist.

        Returns:
            Dict containing deployment status and physical item IDs by type.

        Raises:
            FileNotFoundError: If an artifact directory or manifest is missing.
            ValueError: If a manifest is invalid or overwrite is disabled for an
                existing item.
        """
        project_path = Path(project_dir)
        artifacts = self._discover_fabric_project(project_path, project_name)
        logical_workspace_ids = self._logical_workspace_ids(artifacts)
        replacements = {
            logical_id: workspace_id for logical_id in logical_workspace_ids
        }
        item_ids = {}
        statuses = []
        data_files = self._project_data_files(project_path)
        self._validate_notebook_file_references(
            artifacts, {path.name.casefold() for path in data_files})
        preflight = self.preflight_fabric_workspace(workspace_id)
        uploaded_files = []

        for artifact in artifacts:
            item_type = artifact['type']
            display_name = artifact['display_name']
            existing = self._find_item(
                workspace_id, display_name, item_type)
            if existing and not overwrite:
                raise ValueError(
                    f"Fabric item '{display_name}' ({item_type}) already "
                    'exists and overwrite is disabled')

            dataflow_connections = None
            if (item_type == 'Dataflow'
                    and self._dataflow_has_queries(artifact)):
                dataflow_connections = [self._ensure_lakehouse_connection(
                    workspace_id, item_ids.get('Lakehouse'))]
            parts = self._package_fabric_parts(
                artifact, replacements, workspace_id, item_ids,
                dataflow_connections=dataflow_connections)
            if existing:
                physical_id = existing.get('id')
                if not physical_id:
                    raise ValueError(
                        f"Existing Fabric item '{display_name}' has no ID")
                response = self.client.post(
                    f'/workspaces/{workspace_id}/items/{physical_id}'
                    '/updateDefinition?updateMetadata=true',
                    data={'definition': {'parts': parts}},
                )
                status = 'updated'
            else:
                response = self.client.post(
                    f'/workspaces/{workspace_id}/items',
                    data={
                        'displayName': display_name,
                        'type': item_type,
                        'definition': {'parts': parts},
                    },
                )
                physical_id = response.get('id')
                if not physical_id:
                    raise ValueError(
                        f"Fabric did not return an ID for '{display_name}' "
                        f'({item_type})')
                status = 'created'

            item_ids[item_type] = physical_id
            replacements[artifact['logical_id']] = physical_id
            if item_type == 'Lakehouse':
                uploaded_files = self._upload_project_data(
                    workspace_id, physical_id, project_path, data_files)
            statuses.append({
                'type': item_type,
                'display_name': display_name,
                'logical_id': artifact['logical_id'],
                'item_id': physical_id,
                'status': status,
                'parts': [part['path'] for part in parts],
                'response': response,
            })

        return {
            'success': True,
            'workspace_id': workspace_id,
            'project_name': project_name,
            'preflight': preflight,
            'item_ids': item_ids,
            'uploaded_files': uploaded_files,
            'artifacts': statuses,
        }

    def preflight_fabric_workspace(self, workspace_id):
        """Verify read access to the target workspace before remote writes."""
        if not str(workspace_id or '').strip():
            raise ValueError('A target Fabric workspace ID is required')

        workspace = self.client.get_workspace(workspace_id)
        if not isinstance(workspace, dict) or not workspace.get('id'):
            raise RuntimeError(
                'Fabric workspace preflight returned no workspace ID')
        items = self.client.list_items(workspace_id)
        if not isinstance(items, dict) or not isinstance(
                items.get('value', []), list):
            raise RuntimeError(
                'Fabric workspace preflight returned an invalid item listing')
        return {
            'workspace_id': workspace['id'],
            'workspace_name': workspace.get('displayName', ''),
            'accessible_items': len(items.get('value', [])),
        }

    @staticmethod
    def _dataflow_has_queries(artifact):
        """Return whether a generated Dataflow contains source queries."""
        definition_path = artifact['directory'] / 'dataflow_definition.json'
        try:
            definition = json.loads(definition_path.read_text(encoding='utf-8'))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f'Invalid Dataflow definition: {definition_path}') from exc
        return bool(definition.get('queries'))

    def run_fabric_pipeline(self, workspace_id, pipeline_id):
        """Run a deployed Data Pipeline and wait for its terminal job state."""
        if not pipeline_id:
            raise ValueError('A deployed DataPipeline item ID is required')
        result = self.client.post(
            f'/workspaces/{workspace_id}/items/{pipeline_id}'
            '/jobs/instances?jobType=Pipeline',
            data={},
        )
        status = str(result.get('status', '')).lower()
        if status != 'completed':
            raise RuntimeError(
                'Fabric Data Pipeline did not complete successfully: '
                f'{result.get("status", "unknown")}')
        return result

    def _discover_fabric_project(self, project_dir, project_name):
        """Read and validate the ordered Fabric artifact manifests."""
        artifacts = []
        for suffix, expected_type in self._FABRIC_PROJECT_ARTIFACTS:
            artifact_dir = project_dir / f'{project_name}.{suffix}'
            if not artifact_dir.is_dir():
                raise FileNotFoundError(
                    f'Missing Fabric artifact directory: {artifact_dir}')

            platform_path = artifact_dir / '.platform'
            if not platform_path.is_file():
                raise FileNotFoundError(
                    f'Missing Fabric artifact manifest: {platform_path}')
            try:
                platform = json.loads(platform_path.read_text(encoding='utf-8'))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f'Invalid Fabric artifact manifest: {platform_path}') from exc

            metadata = platform.get('metadata', {})
            item_type = metadata.get('type')
            logical_id = platform.get('config', {}).get('logicalId')
            if item_type != expected_type:
                raise ValueError(
                    f'{platform_path} has type {item_type!r}; '
                    f'expected {expected_type!r}')
            if not isinstance(logical_id, str) or not logical_id:
                raise ValueError(f'{platform_path} has no logicalId')

            display_name = metadata.get('displayName') or project_name
            if not isinstance(display_name, str) or not display_name:
                raise ValueError(f'{platform_path} has no displayName')
            artifacts.append({
                'suffix': suffix,
                'type': item_type,
                'display_name': display_name,
                'logical_id': logical_id,
                'directory': artifact_dir,
            })
        return artifacts

    @staticmethod
    def _logical_workspace_ids(artifacts):
        """Infer generated logical workspace IDs from text definitions."""
        workspace_ids = set()
        patterns = (
            r'"workspaceId"\s*:\s*"([^"]+)"',
            r'"default_lakehouse_workspace_id"\s*:\s*"([^"]+)"',
            r'workspaceId\s*=\s*"([^"]+)"',
        )
        for artifact in artifacts:
            for path in artifact['directory'].rglob('*'):
                if not path.is_file():
                    continue
                try:
                    text = path.read_text(encoding='utf-8')
                except (OSError, UnicodeDecodeError):
                    continue
                for pattern in patterns:
                    workspace_ids.update(re.findall(pattern, text))
        return workspace_ids

    @staticmethod
    def _project_data_files(project_dir):
        """Return canonical staged files that belong in Lakehouse Files."""
        data_dir = project_dir / 'Data'
        if not data_dir.is_dir():
            return []
        return sorted(
            path for path in data_dir.rglob('*.csv') if path.is_file())

    @staticmethod
    def _validate_notebook_file_references(artifacts, available_names):
        """Fail before deployment when Notebook Files references are missing."""
        notebook = next(
            artifact for artifact in artifacts if artifact['type'] == 'Notebook')
        path = notebook['directory'] / 'artifact.content.ipynb'
        try:
            content = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f'Invalid Notebook definition: {path}') from exc
        referenced = {
            Path(value.replace('\\', '/')).name.casefold()
            for value in re.findall(r'Files/([^"\s]+)', content)
        }
        missing = sorted(referenced - available_names)
        if missing:
            raise FileNotFoundError(
                'Notebook references unstaged Lakehouse file(s): '
                + ', '.join(missing))

    def _upload_project_data(self, workspace_id, lakehouse_id, project_dir,
                             data_files):
        """Upload canonical local files to the deployed Lakehouse Files area."""
        data_dir = project_dir / 'Data'
        uploaded = []
        for path in data_files:
            relative_path = path.relative_to(data_dir).as_posix()
            result = self.client.upload_onelake_file(
                workspace_id,
                lakehouse_id,
                f'Files/{relative_path}',
                path.read_bytes(),
            )
            uploaded.append(result)
        return uploaded

    def _package_fabric_parts(self, artifact, replacements, workspace_id,
                              item_ids, dataflow_connections=None):
        """Build InlineBase64 definition parts for one Fabric item."""
        files = self._fabric_part_files(artifact)
        parts = []
        for path in files:
            relative_path = path.relative_to(
                artifact['directory']).as_posix()
            content = path.read_bytes()
            try:
                text = content.decode('utf-8')
            except UnicodeDecodeError:
                pass
            else:
                for logical_id, physical_id in replacements.items():
                    text = text.replace(logical_id, physical_id)
                if (artifact['type'] == 'Report'
                        and relative_path == 'definition.pbir'):
                    text = self._bind_report_to_semantic_model(
                        text, workspace_id, item_ids)
                if (artifact['type'] == 'Dataflow'
                        and relative_path == 'queryMetadata.json'):
                    text = self._bind_dataflow_connections(
                        text, dataflow_connections or [])
                content = text.encode('utf-8')

            parts.append({
                'path': relative_path,
                'payload': base64.b64encode(content).decode('ascii'),
                'payloadType': 'InlineBase64',
            })
        return parts

    def _ensure_lakehouse_connection(self, workspace_id, lakehouse_id):
        """Return a Dataflow binding for the deployed Lakehouse destination."""
        if not lakehouse_id:
            raise ValueError(
                'Dataflow deployment requires a deployed Lakehouse')

        connection = self._find_lakehouse_connection(
            workspace_id, lakehouse_id)
        if connection is None:
            connection = self._create_lakehouse_connection(
                workspace_id, lakehouse_id)

        datasource_id = connection.get('id')
        if not datasource_id:
            raise ValueError('Fabric Lakehouse connection has no ID')
        cluster_id = self._resolve_connection_cluster_id(datasource_id)
        return {
            'connectionId': json.dumps({
                'ClusterId': cluster_id,
                'DatasourceId': datasource_id,
            }, separators=(',', ':')),
            'kind': 'Lakehouse',
            'path': 'Lakehouse',
        }

    def _find_lakehouse_connection(self, workspace_id, lakehouse_id):
        """Find a visible Lakehouse connection targeting the deployed item."""
        response = self.client.get('/connections')
        for connection in response.get('value', []):
            details = connection.get('connectionDetails', {})
            if str(details.get('type', '')).lower() != 'lakehouse':
                continue
            parameters = {
                str(param.get('name', '')).lower(): str(param.get('value', ''))
                for param in details.get('parameters', [])
            }
            if (parameters.get('workspaceid') == str(workspace_id)
                    and parameters.get('lakehouseid') == str(lakehouse_id)):
                return connection
        return None

    def _create_lakehouse_connection(self, workspace_id, lakehouse_id):
        """Create a Workspace Identity Lakehouse connection when supported."""
        response = self.client.get('/connections/supportedConnectionTypes')
        lakehouse_type = next((
            item for item in response.get('value', [])
            if str(item.get('type', '')).lower() == 'lakehouse'
        ), None)
        if lakehouse_type is None:
            raise RuntimeError(
                'No Lakehouse connection targets the deployed item and the '
                'tenant does not advertise Lakehouse connection creation')

        credentials = {
            str(value).lower()
            for value in lakehouse_type.get('supportedCredentialTypes', [])
        }
        if 'workspaceidentity' not in credentials:
            raise RuntimeError(
                'No Lakehouse connection targets the deployed item and '
                'WorkspaceIdentity is not supported by this tenant')

        methods = lakehouse_type.get('creationMethods', [])
        creation = next((
            method for method in methods
            if str(method.get('name', method.get('creationMethod', ''))).lower()
            == 'lakehouse'
        ), methods[0] if methods else None)
        if creation is None:
            raise RuntimeError(
                'Lakehouse connection creation metadata has no creation method')

        parameter_types = {
            str(param.get('name', '')).lower(): param.get('dataType', 'Text')
            for param in creation.get('parameters', [])
        }
        connection = self.client.post('/connections', data={
            'connectivityType': 'ShareableCloud',
            'displayName': f'Lakehouse-{lakehouse_id}',
            'connectionDetails': {
                'type': 'Lakehouse',
                'creationMethod': creation.get(
                    'name', creation.get('creationMethod', 'Lakehouse')),
                'parameters': [
                    {
                        'dataType': parameter_types.get('workspaceid', 'Text'),
                        'name': 'workspaceId',
                        'value': workspace_id,
                    },
                    {
                        'dataType': parameter_types.get('lakehouseid', 'Text'),
                        'name': 'lakehouseId',
                        'value': lakehouse_id,
                    },
                ],
            },
            'privacyLevel': 'Organizational',
            'credentialDetails': {
                'singleSignOnType': 'None',
                'connectionEncryption': 'Encrypted',
                'skipTestConnection': False,
                'credentials': {'credentialType': 'WorkspaceIdentity'},
            },
        })
        if not connection.get('id'):
            raise RuntimeError(
                'Fabric did not return an ID for the Lakehouse connection')
        return connection

    def _resolve_connection_cluster_id(self, datasource_id):
        """Resolve the Power BI cluster ID required by Dataflow metadata."""
        response = self.client.get_absolute(
            self._PBI_CLUSTER_DATASOURCES_URL)
        for datasource in response.get('value', []):
            if str(datasource.get('id')) == str(datasource_id):
                cluster_id = datasource.get('clusterId')
                if cluster_id:
                    return cluster_id
        raise RuntimeError(
            'Lakehouse connection is not visible in '
            f'gatewayClusterDatasources: {datasource_id}')

    @staticmethod
    def _bind_dataflow_connections(text, connections):
        """Inject resolved connection bindings into queryMetadata.json."""
        try:
            metadata = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError('Invalid Dataflow queryMetadata.json') from exc
        metadata['connections'] = connections
        return json.dumps(metadata, indent=2, ensure_ascii=False)

    @staticmethod
    def _fabric_part_files(artifact):
        """Return official deployable parts, excluding generator auxiliaries."""
        artifact_dir = artifact['directory']
        item_type = artifact['type']
        platform = artifact_dir / '.platform'
        if item_type == 'Lakehouse':
            files = [platform, artifact_dir / 'lakehouse.metadata.json']
        elif item_type == 'Dataflow':
            files = [platform, artifact_dir / 'mashup.pq',
                     artifact_dir / 'queryMetadata.json']
        elif item_type == 'Notebook':
            files = [platform, artifact_dir / 'artifact.content.ipynb']
        elif item_type == 'SemanticModel':
            files = [platform, artifact_dir / 'definition.pbism']
            definition_dir = artifact_dir / 'definition'
            if definition_dir.is_dir():
                files.extend(sorted(
                    path for path in definition_dir.rglob('*')
                    if path.is_file()))
        elif item_type == 'Report':
            files = [platform, artifact_dir / 'definition.pbir']
            definition_dir = artifact_dir / 'definition'
            if definition_dir.is_dir():
                files.extend(sorted(
                    path for path in definition_dir.rglob('*')
                    if path.is_file()))
        elif item_type == 'DataPipeline':
            files = [platform, artifact_dir / 'pipeline-content.json']
        else:
            raise ValueError(f'Unsupported Fabric project item: {item_type}')

        missing = [str(path) for path in files if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                'Missing deployable Fabric definition part(s): '
                + ', '.join(missing))
        return files

    @staticmethod
    def _bind_report_to_semantic_model(text, workspace_id, item_ids):
        """Convert a local PBIR byPath reference to a deployed connection."""
        try:
            definition = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError('Invalid Report definition.pbir') from exc

        dataset_reference = definition.get('datasetReference', {})
        if 'byPath' not in dataset_reference:
            return text
        model_id = item_ids.get('SemanticModel')
        if not model_id:
            raise ValueError(
                'Report deployment requires a deployed SemanticModel')

        definition['datasetReference'] = {
            'byConnection': {
                'connectionString': (
                    'Data Source=powerbi://api.powerbi.com/v1.0/myorg/'
                    f'{workspace_id};Initial Catalog={model_id}'
                ),
                'pbiServiceModelId': model_id,
                'pbiModelVirtualServerName': 'sobe_wowvirtualserver',
                'pbiModelDatabaseName': model_id,
            },
        }
        return json.dumps(definition, indent=2, ensure_ascii=False)

    def _find_item(self, workspace_id, item_name, item_type):
        """
        Find an item by name and type in a workspace.

        Args:
            workspace_id: Workspace ID
            item_name: Item name
            item_type: Item type

        Returns:
            Item dict if found, None otherwise
        """
        try:
            import unicodedata as _ud
            def _norm(s):
                return ''.join(c for c in _ud.normalize('NFKD', s)
                               if not _ud.combining(c)).lower()
            items = self.client.list_items(workspace_id, item_type)
            norm_target = _norm(item_name)
            exact = None
            fuzzy = None
            for item in items.get('value', []):
                dn = item.get('displayName', '')
                if dn == item_name:
                    exact = item
                    break
                if not fuzzy and _norm(dn) == norm_target:
                    fuzzy = item
            return exact or fuzzy
        except Exception as e:
            logger.warning(f'Failed to search for item: {e}')
            return None

    def get_deployment_status(self, workspace_id, item_id):
        """
        Get deployment or item status.

        Args:
            workspace_id: Workspace ID
            item_id: Item ID

        Returns:
            Status dict
        """
        return self.client.get(
            f'/workspaces/{workspace_id}/items/{item_id}'
        )

    def deploy_shared_model(self, workspace_id, project_dir,
                             model_name, report_names,
                             overwrite=True):
        """Deploy a shared semantic model + thin reports atomically.

        Deploys the semantic model first, then each thin report.
        If any report fails, logs the error and continues with remaining.

        Args:
            workspace_id: Target workspace ID.
            project_dir: Root project directory containing model + reports.
            model_name: Name of the shared semantic model.
            report_names: List of thin report names to deploy.
            overwrite: Overwrite existing artifacts.

        Returns:
            DeploymentResult dict with status per artifact.
        """
        from pathlib import Path

        result = {
            'model_name': model_name,
            'workspace_id': workspace_id,
            'model_status': 'pending',
            'model_id': None,
            'reports': [],
            'success': True,
        }

        project_path = Path(project_dir)

        # Step 1: Deploy the semantic model first
        logger.info(
            "Deploying shared semantic model '%s' to workspace %s",
            model_name, workspace_id,
        )
        sm_dir = project_path / f"{model_name}.SemanticModel"
        if sm_dir.is_dir():
            try:
                sm_config = self._read_artifact_config(sm_dir)
                sm_result = self.deploy_dataset(
                    workspace_id, model_name, sm_config, overwrite,
                )
                result['model_status'] = 'deployed'
                result['model_id'] = sm_result.get('id')
                logger.info("Semantic model deployed: %s", result['model_id'])
            except Exception as e:
                logger.error("Failed to deploy semantic model: %s", e)
                result['model_status'] = 'failed'
                result['model_error'] = str(e)
                result['success'] = False
                return result  # Can't deploy reports without model
        else:
            logger.warning("SemanticModel directory not found: %s", sm_dir)
            result['model_status'] = 'not_found'
            result['success'] = False
            return result

        # Step 2: Deploy each thin report
        for report_name in report_names:
            report_result = {
                'name': report_name,
                'status': 'pending',
                'id': None,
            }

            report_dir = project_path / f"{report_name}.Report"
            if not report_dir.is_dir():
                report_result['status'] = 'not_found'
                result['reports'].append(report_result)
                logger.warning("Report directory not found: %s", report_dir)
                continue

            try:
                rpt_config = self._read_artifact_config(report_dir)
                rpt_result = self.deploy_report(
                    workspace_id, report_name, rpt_config, overwrite,
                )
                report_result['status'] = 'deployed'
                report_result['id'] = rpt_result.get('id')
                logger.info("Report deployed: %s (%s)",
                            report_name, report_result['id'])
            except Exception as e:
                logger.error(
                    "Failed to deploy report '%s': %s", report_name, e,
                )
                report_result['status'] = 'failed'
                report_result['error'] = str(e)
                result['success'] = False

            result['reports'].append(report_result)

        deployed_count = sum(
            1 for r in result['reports'] if r['status'] == 'deployed'
        )
        logger.info(
            "Shared model deployment: model=%s, reports=%d/%d",
            result['model_status'], deployed_count, len(report_names),
        )
        return result

    def _read_artifact_config(self, artifact_dir):
        """Read artifact configuration from a directory.

        Looks for definition files (*.json, *.pbir, *.pbism) in the directory.

        Args:
            artifact_dir: Path to the artifact directory.

        Returns:
            dict: Configuration suitable for deployment API.
        """
        from pathlib import Path

        artifact_dir = Path(artifact_dir)
        config = {'displayName': artifact_dir.stem.replace('.SemanticModel', '')
                                               .replace('.Report', '')}

        # Read definition files
        definition_dir = artifact_dir / 'definition'
        if definition_dir.is_dir():
            parts = {}
            for f in sorted(definition_dir.rglob('*')):
                if f.is_file():
                    rel = str(f.relative_to(definition_dir)).replace('\\', '/')
                    try:
                        parts[rel] = f.read_text(encoding='utf-8')
                    except (UnicodeDecodeError, ValueError):
                        logger.debug('Binary file, hex-encoding: %s', f)
                        parts[rel] = f.read_bytes().hex()
            config['definition'] = parts

        return config

    # ── Sprint 100: Endorsement & Certification ────────────────────────────

    def endorse_item(self, workspace_id, item_id, endorsement='promoted'):
        """Set endorsement status on a Fabric item (dataset/report).

        Endorsement values:
          - 'none': Remove endorsement
          - 'promoted': Mark as Promoted
          - 'certified': Mark as Certified (requires admin permission)

        Args:
            workspace_id: Workspace containing the item.
            item_id: Item ID to endorse.
            endorsement: 'none', 'promoted', or 'certified'.

        Returns:
            dict: API response or error info.
        """
        valid = ('none', 'promoted', 'certified')
        if endorsement not in valid:
            raise ValueError(
                f"endorsement must be one of {valid}, got '{endorsement}'"
            )

        payload = {
            'endorsementDetails': {
                'endorsement': endorsement,
            },
        }

        try:
            endpoint = f'/workspaces/{workspace_id}/items/{item_id}'
            result = self.client.patch(endpoint, data=payload)
            logger.info(
                "Endorsement '%s' applied to item %s in workspace %s",
                endorsement, item_id, workspace_id,
            )
            return {'status': 'succeeded', 'endorsement': endorsement,
                    'item_id': item_id}
        except Exception as e:
            logger.error("Failed to endorse item %s: %s", item_id, e)
            return {'status': 'failed', 'error': str(e),
                    'item_id': item_id}
