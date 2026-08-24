"""Cross-artifact validation for generated Microsoft Fabric projects."""

from __future__ import annotations

import json
import os
import re


class FabricProjectValidator:
    """Validate the deployable parts and references in a Fabric bundle."""

    _ARTIFACTS = {
        'Lakehouse': ('Lakehouse', ('lakehouse.metadata.json',)),
        'Dataflow': ('Dataflow', ('mashup.pq', 'queryMetadata.json')),
        'Notebook': ('Notebook', ('artifact.content.ipynb',)),
        'SemanticModel': (
            'SemanticModel',
            ('definition.pbism', 'definition/model.tmdl'),
        ),
        'Pipeline': ('DataPipeline', ('pipeline-content.json',)),
    }

    @classmethod
    def validate(cls, project_dir, project_name, include_report=True):
        """Return validation errors and warnings for a generated bundle."""
        errors = []
        warnings = []
        logical_ids = set()
        artifacts = dict(cls._ARTIFACTS)
        if include_report:
            artifacts['Report'] = (
                'Report', ('definition.pbir', 'definition/report.json'))

        for suffix, (item_type, required_parts) in artifacts.items():
            item_dir = os.path.join(project_dir, f'{project_name}.{suffix}')
            if not os.path.isdir(item_dir):
                errors.append(f'Missing {suffix} artifact directory')
                continue

            platform = cls._read_json(
                os.path.join(item_dir, '.platform'), errors)
            if platform:
                actual_type = platform.get('metadata', {}).get('type')
                if actual_type != item_type:
                    errors.append(
                        f'{suffix} manifest type is {actual_type!r}, '
                        f'expected {item_type!r}')
                logical_id = platform.get('config', {}).get('logicalId')
                if not logical_id:
                    errors.append(f'{suffix} manifest has no logicalId')
                elif logical_id in logical_ids:
                    errors.append(f'Duplicate logicalId: {logical_id}')
                else:
                    logical_ids.add(logical_id)

            for part in required_parts:
                if not os.path.isfile(os.path.join(item_dir, *part.split('/'))):
                    errors.append(f'{suffix} is missing required part {part}')

        cls._validate_table_contract(project_dir, project_name, errors)
        cls._validate_direct_lake(project_dir, project_name, errors)
        cls._validate_pipeline(project_dir, project_name, errors)
        if include_report:
            cls._validate_report_binding(project_dir, project_name, errors)

        return {
            'valid': not errors,
            'errors': errors,
            'warnings': warnings,
            'artifacts_checked': len(artifacts),
        }

    @classmethod
    def _validate_table_contract(cls, project_dir, project_name, errors):
        lakehouse = cls._read_json(os.path.join(
            project_dir, f'{project_name}.Lakehouse',
            'lakehouse_definition.json'), errors)
        dataflow = cls._read_json(os.path.join(
            project_dir, f'{project_name}.Dataflow',
            'dataflow_definition.json'), errors)
        query_metadata = cls._read_json(os.path.join(
            project_dir, f'{project_name}.Dataflow',
            'queryMetadata.json'), errors)
        if not lakehouse or not dataflow or not query_metadata:
            return

        lakehouse_tables = {
            table.get('name') for table in lakehouse.get('tables', [])
            if table.get('name')
        }
        destination_tables = {
            query.get('destination', {}).get('tableName')
            for query in dataflow.get('queries', [])
            if query.get('destination', {}).get('tableName')
        }
        if not destination_tables.issubset(lakehouse_tables):
            errors.append(
            'Dataflow destination tables are missing from the Lakehouse')

        metadata = query_metadata.get('queriesMetadata', {})
        for query in dataflow.get('queries', []):
            destination_name = f'{query.get("name", "")}_DataDestination'
            destination = metadata.get(destination_name, {})
            if not destination.get('isHidden'):
                errors.append(
                    f'Dataflow destination query is not hidden: '
                    f'{destination_name}')
            if destination.get('loadEnabled') is not False:
                errors.append(
                    f'Dataflow destination query must disable load: '
                    f'{destination_name}')

        mashup_path = os.path.join(
            project_dir, f'{project_name}.Dataflow', 'mashup.pq')
        mashup = cls._read_text(mashup_path, errors)
        if mashup and mashup.count('[DataDestinations = {') != len(
                dataflow.get('queries', [])):
            errors.append(
                'Dataflow mashup destination annotations do not match queries')

    @classmethod
    def _validate_direct_lake(cls, project_dir, project_name, errors):
        definition_dir = os.path.join(
            project_dir, f'{project_name}.SemanticModel', 'definition')
        tables_dir = os.path.join(definition_dir, 'tables')
        if not os.path.isdir(tables_dir):
            errors.append('SemanticModel has no tables directory')
            return

        table_files = [
            os.path.join(tables_dir, name)
            for name in os.listdir(tables_dir) if name.endswith('.tmdl')
        ]
        for table_file in table_files:
            content = cls._read_text(table_file, errors)
            if '= entity' not in content or 'mode: directLake' not in content:
                errors.append(
                    f'Non-Direct Lake partition in {os.path.basename(table_file)}')
            if re.search(r'^\s*partition\s+.+?=\s+m\b', content, re.MULTILINE):
                errors.append(
                    f'Import M partition in {os.path.basename(table_file)}')
            if 'mode: import' in content:
                errors.append(
                    f'Import mode in {os.path.basename(table_file)}')

        model = cls._read_text(
            os.path.join(definition_dir, 'model.tmdl'), errors)
        expressions = cls._read_text(
            os.path.join(definition_dir, 'expressions.tmdl'), errors)
        if model and 'defaultMode: directLake' not in model:
            errors.append('SemanticModel defaultMode is not directLake')
        if expressions and 'expression DatabaseQuery' not in expressions:
            errors.append('SemanticModel has no Direct Lake DatabaseQuery')

    @classmethod
    def _validate_pipeline(cls, project_dir, project_name, errors):
        pipeline = cls._read_json(os.path.join(
            project_dir, f'{project_name}.Pipeline',
            'pipeline-content.json'), errors)
        if not pipeline:
            return
        serialized = json.dumps(pipeline)
        if re.search(r'\{\{[^{}]+\}\}', serialized):
            errors.append('Pipeline contains unresolved placeholders')

        activities = pipeline.get('properties', {}).get('activities', [])
        activity_names = {
            activity.get('name') for activity in activities
            if activity.get('name')
        }
        dependencies = {
            dependency.get('activity')
            for activity in activities
            for dependency in activity.get('dependsOn', [])
            if dependency.get('activity')
        }
        unknown = dependencies - activity_names
        if unknown:
            errors.append(
                'Pipeline has unknown dependencies: '
                + ', '.join(sorted(unknown)))

        refreshes = [
            activity for activity in activities
            if activity.get('type') == 'RefreshDataflow'
        ]
        if len(refreshes) > 1:
            errors.append(
                'Pipeline must not refresh the generated Dataflow more than once')

    @classmethod
    def _validate_report_binding(cls, project_dir, project_name, errors):
        report_dir = os.path.join(project_dir, f'{project_name}.Report')
        definition = cls._read_json(
            os.path.join(report_dir, 'definition.pbir'), errors)
        if not definition:
            return
        model_path = definition.get(
            'datasetReference', {}).get('byPath', {}).get('path')
        if not model_path:
            errors.append('Report has no byPath SemanticModel reference')
            return
        resolved = os.path.normpath(os.path.join(report_dir, model_path))
        expected = os.path.normpath(os.path.join(
            project_dir, f'{project_name}.SemanticModel'))
        if resolved != expected:
            errors.append('Report byPath does not resolve to the SemanticModel')

    @staticmethod
    def _read_json(path, errors):
        try:
            with open(path, encoding='utf-8') as stream:
                return json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f'Cannot read JSON {path}: {exc}')
            return None

    @staticmethod
    def _read_text(path, errors):
        try:
            with open(path, encoding='utf-8') as stream:
                return stream.read()
        except OSError as exc:
            errors.append(f'Cannot read file {path}: {exc}')
            return ''