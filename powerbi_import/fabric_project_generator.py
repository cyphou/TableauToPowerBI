"""
Fabric-native artifact orchestrator.

Coordinates the generation of all Fabric artifacts from extracted
Tableau data:
  1. Lakehouse (table schemas, DDL)
  2. Dataflow Gen2 (Power Query M ingestion)
  3. PySpark Notebook (ETL pipeline)
  4. Semantic Model (DirectLake TMDL)
  5. Pipeline (orchestration)

This module is invoked when ``--output-format fabric`` is specified
on the CLI.
"""

import json
import os
from datetime import datetime

from .dataflow_generator import DataflowGenerator
from .fabric_item import build_item_registry, logical_id
from .fabric_semantic_model_generator import FabricSemanticModelGenerator
from .fabric_validator import FabricProjectValidator
from .lakehouse_generator import LakehouseGenerator
from .notebook_generator import NotebookGenerator
from .pipeline_generator import PipelineGenerator
from .telemetry import PhaseTimingCollector
from .thin_report_generator import ThinReportGenerator


def _json_default(obj):
    """JSON serializer for objects not serializable by default json code."""
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _sanitize_for_json(value):
    """Recursively coerce ``value`` so ``json.dump`` accepts it.

    Handles tuple dict keys (rewritten as ``"a::b"``), sets, tuples, and
    nested dicts produced by the TMDL generator (e.g. ``actual_bim_column_types``
    is keyed by ``(table, column)`` tuples).
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, tuple):
                k = "::".join(str(p) for p in k)
            elif not isinstance(k, (str, int, float, bool)) and k is not None:
                k = str(k)
            out[k] = _sanitize_for_json(v)
        return out
    if isinstance(value, (set, tuple)):
        return [_sanitize_for_json(x) for x in value]
    if isinstance(value, list):
        return [_sanitize_for_json(x) for x in value]
    return value


class FabricProjectGenerator:
    """Orchestrates generation of a complete Fabric project from Tableau data."""

    def __init__(self, output_dir=None):
        self.output_dir = output_dir or os.path.join('artifacts', 'fabric_projects', 'migrated')
        self.last_phase_timings = None

    def generate_project(self, project_name, extracted_data,
                         calendar_start=None, calendar_end=None,
                         culture=None, languages=None,
                         include_report=True):
        """Generate all Fabric artifacts for a migrated Tableau workbook.

        Args:
            project_name: Name for the Fabric project (used in all artifact names).
            extracted_data: Dict with all extracted Tableau objects
                            (datasources, worksheets, calculations, etc.).
            calendar_start: Start year for Calendar table.
            calendar_end: End year for Calendar table.
            culture: Override culture/locale.
            languages: Comma-separated additional locales.
            include_report: Generate a report for the supplied workbook content.
                Shared-model callers disable this and generate workbook-specific
                thin reports separately.

        Returns:
            dict with project_path and per-artifact stats.
        """
        project_dir = os.path.join(self.output_dir, project_name)
        phase_timings = PhaseTimingCollector().start()
        os.makedirs(project_dir, exist_ok=True)
        item_registry = build_item_registry(project_name)
        workspace_id = logical_id(project_name, 'Workspace')
        artifact_count = 6 if include_report else 5

        results = {
            'project_path': project_dir,
            'project_name': project_name,
            'generated_at': datetime.now().isoformat(),
            'artifacts': {},
        }

        # 1. Lakehouse
        print(f"  [1/{artifact_count}] Generating Lakehouse...")
        with phase_timings.phase('lakehouse'):
            lh_gen = LakehouseGenerator(
                project_dir, project_name, item_id=item_registry['Lakehouse'])
            lh_stats = lh_gen.generate(extracted_data)
        results['artifacts']['lakehouse'] = lh_stats
        print(f"         Tables: {lh_stats['tables']}, Columns: {lh_stats['columns']}, "
              f"Calc columns: {lh_stats['calc_columns']}")

        # 2. Dataflow Gen2
        print(f"  [2/{artifact_count}] Generating Dataflow Gen2...")
        with phase_timings.phase('dataflow'):
            df_gen = DataflowGenerator(
                project_dir,
                project_name,
                item_id=item_registry['Dataflow'],
                lakehouse_id=item_registry['Lakehouse'],
                workspace_id=workspace_id,
            )
            df_stats = df_gen.generate(extracted_data)
        results['artifacts']['dataflow'] = df_stats
        print(f"         Queries: {df_stats['queries']}, Calc columns: {df_stats['calc_columns']}")

        # 3. PySpark Notebook
        print(f"  [3/{artifact_count}] Generating PySpark Notebooks...")
        with phase_timings.phase('notebook'):
            nb_gen = NotebookGenerator(
                project_dir,
                project_name,
                item_id=item_registry['Notebook'],
                lakehouse_id=item_registry['Lakehouse'],
                workspace_id=workspace_id,
            )
            nb_stats = nb_gen.generate(extracted_data)
        results['artifacts']['notebook'] = nb_stats
        print(f"         Notebooks: {nb_stats['notebooks']}, Cells: {nb_stats['cells']}")

        # 4. Semantic Model (DirectLake)
        print(f"  [4/{artifact_count}] Generating DirectLake Semantic Model...")
        with phase_timings.phase('semantic_model'):
            sm_gen = FabricSemanticModelGenerator(
                project_dir, project_name,
                lakehouse_name=f'{project_name}_Lakehouse',
                item_id=item_registry['SemanticModel'],
                lakehouse_id=item_registry['Lakehouse'],
                workspace_id=workspace_id,
            )
            sm_stats = sm_gen.generate(
                extracted_data,
                calendar_start=calendar_start,
                calendar_end=calendar_end,
                culture=culture,
                languages=languages,
            )
        results['artifacts']['semantic_model'] = sm_stats
        print(f"         Tables: {sm_stats.get('tables', 0)}, "
              f"Measures: {sm_stats.get('measures', 0)}, "
              f"Relationships: {sm_stats.get('relationships', 0)}")

        if include_report:
            # 5. Power BI report bound to the local Direct Lake semantic model
            print(f"  [5/{artifact_count}] Generating Power BI Report...")
            with phase_timings.phase('report'):
                report_gen = ThinReportGenerator(
                    project_name,
                    project_dir,
                    item_id=item_registry['Report'],
                )
                report_dir = report_gen.generate_thin_report(
                    project_name,
                    extracted_data,
                )
                report_stats = {
                    'path': report_dir,
                    'worksheets': len(extracted_data.get('worksheets', [])),
                    'dashboards': len(extracted_data.get('dashboards', [])),
                }
            results['artifacts']['report'] = report_stats
            print(f"         Worksheets: {report_stats['worksheets']}, "
                  f"Dashboards: {report_stats['dashboards']}")

        # Final artifact: orchestration pipeline
        print(f"  [{artifact_count}/{artifact_count}] Generating Data Pipeline...")
        with phase_timings.phase('pipeline'):
            pipe_gen = PipelineGenerator(
                project_dir, project_name,
                lakehouse_name=f'{project_name}_Lakehouse',
                item_id=item_registry['DataPipeline'],
                item_registry=item_registry,
                workspace_id=workspace_id,
            )
            pipe_stats = pipe_gen.generate(extracted_data)
        results['artifacts']['pipeline'] = pipe_stats
        print(f"         Activities: {pipe_stats['activities']}, Stages: {pipe_stats['stages']}")

        with phase_timings.phase('validation'):
            validation = FabricProjectValidator.validate(
                project_dir, project_name, include_report=include_report)
        results['validation'] = validation
        if not validation['valid']:
            details = '; '.join(validation['errors'])
            raise ValueError(f'Fabric project validation failed: {details}')

        # Write project metadata
        meta_path = os.path.join(project_dir, 'fabric_project_metadata.json')
        with phase_timings.phase('metadata'):
            with open(meta_path, 'w', encoding='utf-8') as f:
                json.dump(_sanitize_for_json(results), f, indent=2, default=_json_default)
        self.last_phase_timings = phase_timings.finish()
        results['phase_timings'] = self.last_phase_timings

        print(f"\n  [OK] Fabric project created: {project_dir}")
        return results
