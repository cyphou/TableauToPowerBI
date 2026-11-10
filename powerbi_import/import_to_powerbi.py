"""
Import orchestrator for Power BI project generation.

Loads extracted Tableau JSON files and drives the .pbip project
generation pipeline (BIM model + TMDL + PBIR report).
"""

import json
import logging
import os
import sys
import uuid
from copy import deepcopy

# The generator modules also support legacy top-level imports. Make them
# available when this importer is loaded through its public package path.
_module_dir = os.path.dirname(os.path.abspath(__file__))
if _module_dir not in sys.path:
    sys.path.insert(0, _module_dir)

from pbip_generator import PowerBIProjectGenerator
from telemetry import PhaseTimingCollector

logger = logging.getLogger(__name__)


def _write_json_file(path, data):
    """Write a JSON file with consistent formatting."""
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _preflight_fabric_datasources(datasources):
    """Check the active source trees before publishing any Fabric artifacts."""
    from powerbi_import.fabric_sources import preflight_fabric_ingestion
    from powerbi_import.tmdl_relationships import preflight_physical_joins

    # The join check also looks across datasources, so it needs a re-iterable
    # collection even when a caller supplies a generator.
    datasources = tuple(datasources or ())
    preflight_physical_joins(datasources, provenance_retained=True)
    preflight_fabric_ingestion(datasources)


def _shared_native_hyper_provenance(all_converted_objects):
    """Remember native bindings whose historical identities passed preflight."""
    from powerbi_import.fabric_sources import table_connection

    provenance = []
    for objects in all_converted_objects:
        for datasource in objects.get('datasources', []):
            for table in datasource.get('tables', []):
                if table.get('hyper_identity') is not None:
                    provenance.append(deepcopy({
                        'name': table.get('name'),
                        'source_table': table.get('source_table'),
                        'connection': table_connection(datasource, table),
                        'hyper_identity': table['hyper_identity'],
                        'source_selection': datasource.get('source_selection'),
                    }))
    return provenance


def _retain_shared_native_bindings(merged, provenance):
    """Keep inactive Hyper history without treating it as the active source."""
    from powerbi_import.fabric_sources import table_connection

    for datasource in merged.get('datasources', []):
        for table in datasource.get('tables', []):
            identity = table.get('hyper_identity')
            if identity is None:
                continue
            matches = [
                original for original in provenance
                if original['hyper_identity'] == identity
                and original['name'] == table.get('name')
                and original['source_table'] == table.get('source_table')
                and original['connection'] == table_connection(datasource, table)
            ]
            # An unmatched or ambiguous projection remains subject to the
            # normal merged-source preflight, which refuses active Hyper.
            if not matches or any(match != matches[0] for match in matches[1:]):
                continue
            if ('_fabric_historical_hyper_identity' in table
                    or '_fabric_native_source_selection' in table):
                continue
            table['_fabric_historical_hyper_identity'] = table.pop('hyper_identity')
            table['_fabric_native_source_selection'] = matches[0]['source_selection']


def _fabric_project_path(results, project_name, *, include_report=True):
    """Require a complete, locally validated Fabric project on disk."""
    if not isinstance(results, dict):
        raise RuntimeError("Fabric generation did not return a project result")
    project_path = results.get('project_path')
    validation = results.get('validation')
    if (not project_path or not os.path.isdir(project_path)
            or not isinstance(validation, dict)
            or validation.get('valid') is not True):
        raise RuntimeError("Fabric generation did not return a locally validated project")
    from powerbi_import.fabric_validator import FabricProjectValidator

    actual_validation = FabricProjectValidator.validate(
        project_path, project_name, include_report=include_report)
    if not isinstance(actual_validation, dict) or actual_validation.get('valid') is not True:
        raise RuntimeError("Fabric generation did not produce a valid project on disk")
    return project_path


def _mark_shared_fabric_evidence_incomplete(project_dir, fabric_results, workbook_names):
    """Keep model-only evidence distinct from the unchecked shared report set."""
    from powerbi_import.atomic_file import atomic_write_text

    metadata_path = os.path.join(project_dir, 'fabric_project_metadata.json')
    evidence_path = os.path.join(project_dir, 'fabric_evidence.json')
    with open(metadata_path, encoding='utf-8') as stream:
        metadata = json.load(stream)
    original = metadata.get('fabric_evidence')
    if not isinstance(original, dict):
        raise RuntimeError("Fabric generation did not publish its model evidence")

    reason = (
        "Shared Fabric output contains workbook-specific thin reports; "
        "the single-report Fabric validator does not validate that report set."
    )
    evidence = deepcopy(original)
    original_validation = original.get('validation') or {}
    validation = {
        'valid': None,
        'status': 'not_run',
        'errors': [],
        'warnings': [*(original_validation.get('warnings') or []), reason],
        'artifacts_checked': 0,
    }
    if 'hyper_source_selection' in original_validation:
        validation['hyper_source_selection'] = deepcopy(
            original_validation['hyper_source_selection'])
    evidence.update({
        'status': 'not_run',
        'confidence': 'UNVERIFIED',
        'scope': 'unknown',
        'requested_scope': 'fabric_shared_model_and_thin_reports',
        'scope_diagnostic': reason,
        'validation': validation,
        'report_validation': 'not_run',
        'model_generation_evidence': deepcopy(original),
        'thin_reports': {
            'validation': 'not_run',
            'expected_workbooks': list(workbook_names),
        },
    })
    evidence.pop('generation_evidence', None)
    for artifact in (evidence.get('artifacts') or {}).values():
        if isinstance(artifact, dict):
            artifact['status'] = 'not_run'
            artifact['dependency_status'] = 'not_run'

    # The generator's five-item validation is still useful, but it does not
    # cover the subsequently generated thin reports.
    for item in (metadata.get('validation'), metadata.get('quality')):
        if isinstance(item, dict):
            item['scope'] = 'fabric_model_only'
            item['report_validation'] = 'not_run'
    manifest = metadata.get('evidence_manifest')
    if isinstance(manifest, dict):
        for key in ('validation', 'environment'):
            item = manifest.get(key)
            if isinstance(item, dict):
                item['scope'] = 'fabric_model_only'
                item['report_validation'] = 'not_run'
    metadata['fabric_evidence'] = evidence
    metadata['shared_bundle_validation'] = {
        'scope': 'fabric_shared_model_and_thin_reports',
        'status': 'not_run',
        'valid': None,
        'reason': reason,
    }

    # Publish both conservative records before creating any thin report.
    # If publication fails, no reports have yet been generated.
    atomic_write_text(
        metadata_path, json.dumps(metadata, indent=2, ensure_ascii=False))
    atomic_write_text(
        evidence_path, json.dumps(evidence, indent=2, ensure_ascii=False))
    fabric_results['fabric_evidence'] = evidence
    return evidence


class PowerBIImporter:
    """Power BI object importer"""

    def __init__(self, source_dir=None):
        self.source_dir = source_dir or os.environ.get('TTPBI_EXTRACT_DIR', 'tableau_export/')
        self.last_generation_timings = None

    def import_all(self, generate_pbip=True, report_name=None, output_dir=None,
                   calendar_start=None, calendar_end=None, culture=None,
                   model_mode='auto', output_format='pbip', languages=None,
                   composite_threshold=None, agg_tables='none',
                   incremental_refresh=False, incremental_refresh_months=12,
                   parameterize=True, optimize_dax=False, time_intelligence='none',
                   strict_generation=False):
        """
        Import all extracted objects and generate Power BI project

        Args:
            generate_pbip: If True, generates the requested output format.
            report_name: Override report name (defaults to dashboard name or 'Report')
            output_dir: Custom output directory for generated projects.
            output_format: 'pbip' (default) or 'fabric'.
            calendar_start: Start year for Calendar table (default: 2020)
            calendar_end: End year for Calendar table (default: 2030)
            culture: Override culture/locale for semantic model
        """

        import_timings = PhaseTimingCollector().start()
        print("=" * 80)
        print("IMPORT POWER BI")
        print("=" * 80)
        print()

        # Load converted objects directly from tableau_export/
        with import_timings.phase('input_loading'):
            converted_objects = self._load_converted_objects()

        if not converted_objects.get('datasources'):
            self.last_generation_timings = import_timings.finish()
            print(f"  [ERROR] No datasources found in {os.path.join(self.source_dir, 'datasources.json')}")
            print("     Run extraction first: python migrate.py <file>")
            raise ValueError("Cannot generate a project without datasources")

        # Determine report name
        if not report_name:
            dashboards = converted_objects.get('dashboards', [])
            if dashboards:
                report_name = dashboards[0].get('name', 'Report')
            else:
                report_name = 'Report'

        print(f"  Report: {report_name}")
        print(f"  Datasources: {len(converted_objects.get('datasources', []))}")
        print(f"  Worksheets: {len(converted_objects.get('worksheets', []))}")
        print(f"  Calculations: {len(converted_objects.get('calculations', []))}")

        # Generate Power BI Project (.pbip) directly from converted objects
        if generate_pbip:
            self.generate_powerbi_project(report_name, converted_objects, output_dir=output_dir,
                                          calendar_start=calendar_start, calendar_end=calendar_end,
                                          culture=culture, model_mode=model_mode,
                                          output_format=output_format, languages=languages,
                                          composite_threshold=composite_threshold, agg_tables=agg_tables,
                                          incremental_refresh=incremental_refresh,
                                          incremental_refresh_months=incremental_refresh_months,
                                          parameterize=parameterize,
                                          optimize_dax=optimize_dax,
                                          time_intelligence=time_intelligence,
                                          strict_generation=strict_generation)

        print()
        print("=" * 80)
        print("IMPORT COMPLETE")
        print("=" * 80)
        print()
        if generate_pbip:
            if output_format == 'fabric':
                print("[OK] Fabric project generated with local static validation")
                print("   Fabric refresh and deployment were not run")
            else:
                print("[OK] Power BI Projects (.pbip) generated automatically")
                print("   Open the .pbip files in Power BI Desktop")
            print()

        importer_report = import_timings.finish()
        generator_report = self.last_generation_timings or {}
        generator_phases = generator_report.get('phases', {})
        input_loading = importer_report['phases'].get('input_loading', 0.0)
        orchestration = max(
            0.0,
            importer_report['total_seconds']
            - input_loading
            - generator_report.get('total_seconds', 0.0),
        )
        phases = {'input_loading': input_loading, **generator_phases}
        phases['import_orchestration'] = orchestration
        measured = sum(phases.values())
        self.last_generation_timings = {
            'total_seconds': importer_report['total_seconds'],
            'measured_seconds': measured,
            'coverage_percent': (
                0.0 if importer_report['total_seconds'] == 0
                else measured / importer_report['total_seconds'] * 100.0
            ),
            'phases': phases,
        }

    def _load_converted_objects(self):
        """Load all extracted JSON files from the source directory."""
        data = {}
        src = self.source_dir
        files_map = {
            'datasources': os.path.join(src, 'datasources.json'),
            'worksheets': os.path.join(src, 'worksheets.json'),
            'dashboards': os.path.join(src, 'dashboards.json'),
            'calculations': os.path.join(src, 'calculations.json'),
            'parameters': os.path.join(src, 'parameters.json'),
            'filters': os.path.join(src, 'filters.json'),
            'stories': os.path.join(src, 'stories.json'),
            'actions': os.path.join(src, 'actions.json'),
            'sets': os.path.join(src, 'sets.json'),
            'groups': os.path.join(src, 'groups.json'),
            'bins': os.path.join(src, 'bins.json'),
            'hierarchies': os.path.join(src, 'hierarchies.json'),
            'sort_orders': os.path.join(src, 'sort_orders.json'),
            'aliases': os.path.join(src, 'aliases.json'),
            'custom_sql': os.path.join(src, 'custom_sql.json'),
            'user_filters': os.path.join(src, 'user_filters.json'),
            'hyper_files': os.path.join(src, 'hyper_files.json'),
        }

        for key, filepath in files_map.items():
            try:
                if os.path.exists(filepath):
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data[key] = json.load(f)
                else:
                    data[key] = [] if key != 'aliases' else {}
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.warning("Failed to load %s (%s): %s — using empty fallback", key, filepath, exc)
                data[key] = [] if key != 'aliases' else {}

        return data

    def generate_powerbi_project(self, report_name, converted_objects, output_dir=None,
                                 calendar_start=None, calendar_end=None, culture=None,
                                 model_mode='auto', output_format='pbip', paginated=False,
                                 languages=None, composite_threshold=None, agg_tables='none',
                                 incremental_refresh=False, incremental_refresh_months=12,
                                 parameterize=True, optimize_dax=False,
                                 time_intelligence='none', strict_generation=False):
        """Generate a Power BI project or a Fabric-native project.

        Args:
            report_name: Name of the report
            converted_objects: Dict of extracted Tableau objects
            output_dir: Custom output directory for .pbip project
            calendar_start: Start year for Calendar table
            calendar_end: End year for Calendar table
            culture: Override culture/locale
        """

        if output_format == 'fabric':
            unsupported = [
                name for name, requested in (
                    ('model_mode', model_mode not in (None, 'auto')),
                    ('paginated', paginated),
                    ('composite_threshold', composite_threshold is not None),
                    ('agg_tables', agg_tables != 'none'),
                    ('incremental_refresh', incremental_refresh),
                    ('incremental_refresh_months', incremental_refresh_months != 12),
                    ('parameterize', parameterize is not True),
                    ('optimize_dax', optimize_dax),
                    ('time_intelligence', time_intelligence != 'none'),
                    ('strict_generation', strict_generation),
                ) if requested
            ]
            if unsupported:
                raise ValueError(
                    "Fabric generation does not support these options: "
                    + ', '.join(unsupported))

            print("\n  Generating Fabric project...")
            try:
                from powerbi_import.fabric_project_generator import FabricProjectGenerator

                _preflight_fabric_datasources(converted_objects.get('datasources', []))
                projects_dir = os.path.abspath(
                    output_dir or os.path.join('artifacts', 'fabric_projects', 'migrated'))
                generator = FabricProjectGenerator(output_dir=projects_dir)
                results = generator.generate_project(
                    project_name=report_name,
                    extracted_data=converted_objects,
                    calendar_start=calendar_start,
                    calendar_end=calendar_end,
                    culture=culture,
                    languages=languages,
                )
                project_path = _fabric_project_path(results, report_name)
                self.last_generation_timings = generator.last_phase_timings
                print(f"  [OK] Fabric project created: {project_path}")
            except Exception as e:
                print(f"  [ERROR] Error generating Fabric project: {str(e)}")
                raise
            return

        if output_format != 'pbip':
            raise ValueError(f"Unsupported output format: {output_format}")

        print("\n  Generating Power BI Project (.pbip)...")

        try:
            # Determine absolute path to powerbi_projects
            if output_dir:
                projects_dir = os.path.abspath(output_dir)
            else:
                artifacts_dir = os.path.abspath('artifacts')
                projects_dir = os.path.join(artifacts_dir, 'powerbi_projects', 'migrated')

            artifacts_dir = os.path.abspath('artifacts')
            generator = PowerBIProjectGenerator(
                output_dir=projects_dir
            )

            project_path = generator.generate_project(report_name, converted_objects,
                                                       calendar_start=calendar_start,
                                                       calendar_end=calendar_end,
                                                       culture=culture,
                                                       model_mode=model_mode,
                                                       output_format=output_format,
                                                       paginated=paginated,
                                                       languages=languages,
                                                       composite_threshold=composite_threshold,
                                                       agg_tables=agg_tables,
                                                       incremental_refresh=incremental_refresh,
                                                       incremental_refresh_months=incremental_refresh_months,
                                                       parameterize=parameterize,
                                                       optimize_dax=optimize_dax,
                                                       time_intelligence=time_intelligence,
                                                       strict_generation=strict_generation)
            self.last_generation_timings = generator.last_phase_timings
            print(f"  [OK] Power BI Project created: {project_path}")

        except Exception as e:
            print(f"  [ERROR] Error generating Power BI Project: {str(e)}")
            # A partial directory is not a generated project. In particular,
            # WinError 206 must reach run_generation's failure result.
            raise

    def import_shared_model(self, model_name, all_converted_objects,
                            workbook_names, output_dir=None,
                            calendar_start=None, calendar_end=None,
                            culture=None, model_mode='auto',
                            languages=None, force_merge=False,
                            merge_config_path=None, save_config=False,
                            strict_merge=False, workbook_paths=None,
                            output_format='pbip',
                            strict_thin_report=False,
                            thin_report_max_orphans=0,
                            live_connection=None):
        """Generate a shared semantic model + thin reports.

        Args:
            model_name: Name for the shared semantic model.
            all_converted_objects: List of converted_objects dicts (one per workbook).
            workbook_names: List of workbook names (parallel with all_converted_objects).
            output_dir: Custom output directory.
            calendar_start: Start year for Calendar table.
            calendar_end: End year for Calendar table.
            culture: Override culture/locale.
            model_mode: Semantic model mode (auto/import/directquery/composite).
            languages: Additional locale strings.
            force_merge: Force merge even with low score.
            merge_config_path: Path to merge config JSON (load saved decisions).
            save_config: Save merge decisions to config file.
            output_format: Output format — 'pbip' (default) or 'fabric'
                (Lakehouse + Dataflow Gen2 + Notebook + DirectLake SemanticModel + Pipeline).
            strict_thin_report: Block generation if thin-report orphaned fields exceed
                the configured threshold.
            thin_report_max_orphans: Maximum allowed orphaned field references across
                all generated thin reports when strict_thin_report is enabled.
            live_connection: Optional ``WORKSPACE_ID/MODEL_NAME``. Wires thin reports
                to a deployed Fabric model via byConnection instead of byPath.

        Returns:
            dict with 'assessment', 'model_path', 'report_paths', plus new fields.
        """
        from powerbi_import.merge_assessment import print_merge_summary
        from powerbi_import.shared_model import (
            analyze_measure_risk,
            assess_merge,
            build_column_lineage,
            build_cross_report_navigation,
            build_field_mapping,
            build_merge_manifest,
            consolidate_rls_roles,
            generate_lineage_annotations,
            generate_merge_validation_report,
            merge_rls_roles,
            merge_semantic_models,
            validate_thin_report_fields,
        )
        from powerbi_import.thin_report_generator import ThinReportGenerator

        premerge_unverified_hyper_sources = None
        native_hyper_provenance = []
        if output_format == 'fabric':
            from powerbi_import.fabric_sources import preflight_fabric_ingestion

            # Merging can deduplicate a file and an active Hyper table and lose
            # the latter's identity. Check every original source, not the
            # merged projection, regardless of workbook order.
            premerge_unverified_hyper_sources = preflight_fabric_ingestion(
                ds for objects in all_converted_objects
                for ds in objects.get('datasources', [])
            )
            # Any table identity surviving that check is historical under a
            # proven native selection, not an active Hyper binding.
            native_hyper_provenance = _shared_native_hyper_provenance(
                all_converted_objects)

        print("=" * 64)
        print("  SHARED SEMANTIC MODEL MIGRATION")
        print("=" * 64)

        # 1. Assess merge feasibility
        print("\n  Step 1: Analyzing merge candidates...")
        assessment = assess_merge(all_converted_objects, workbook_names)

        # 1b. Load merge config if provided
        if merge_config_path:
            from powerbi_import.merge_config import (
                apply_merge_config,
                load_merge_config,
            )
            print(f"  Loading merge config: {merge_config_path}")
            config = load_merge_config(merge_config_path)
            assessment = apply_merge_config(assessment, config)

        print_merge_summary(assessment)

        if assessment.recommendation == 'separate' and not force_merge:
            print("  Merge not recommended (score too low). Use --force-merge to override.")
            return {'assessment': assessment, 'model_path': None, 'report_paths': []}

        # 1c. Report isolated tables (excluded from shared model)
        if assessment.isolated_tables:
            total_isolated = sum(len(v) for v in assessment.isolated_tables.values())
            print(f"\n  Step 1c: {total_isolated} isolated table(s) excluded from shared model")
            for wb, tables in assessment.isolated_tables.items():
                for t in tables:
                    print(f"    [skip] {t} — no link to other workbooks (stays in {wb} report only)")

        # 1d. Analyze measure risks
        risk_analysis = []
        if assessment.measure_conflicts:
            print("\n  Step 1d: Analyzing measure conflict risks...")
            risk_analysis = analyze_measure_risk(assessment.measure_conflicts)
            for ra in risk_analysis:
                icon = {"low": "✓", "medium": "⚠", "high": "✗"}.get(ra.risk_level, "?")
                print(f"    [{icon}] {ra.measure_name}: {ra.risk_level} — {ra.reason}")

        # 1e. Consolidate RLS roles
        rls_consolidations = consolidate_rls_roles(all_converted_objects, workbook_names)
        if rls_consolidations:
            print(f"\n  Step 1e: RLS role consolidation ({len(rls_consolidations)} roles)...")
            for cons in rls_consolidations:
                print(f"    [{cons.action}] {cons.role_name} from {', '.join(cons.source_workbooks)}")

        # 2. Merge into unified dataset
        print("\n  Step 2: Merging semantic models...")
        merged = merge_semantic_models(all_converted_objects, assessment, model_name)

        # 2a. Post-merge safety validation (Sprint 55)
        print("\n  Step 2a: Running post-merge safety checks...")
        validation = generate_merge_validation_report(merged)
        vc = validation['counts']
        icons = {
            'cycles': '✗' if vc['cycles'] else '✓',
            'type_errors': '✗' if vc['type_errors'] else '✓',
            'type_warnings': '⚠' if vc['type_warnings'] else '✓',
            'dax_errors': '✗' if vc['dax_errors'] else '✓',
            'cardinality': '⚠' if vc['cardinality_mismatches'] else '✓',
        }
        print(f"    [{icons['cycles']}] Relationship cycles: {vc['cycles']}")
        print(f"    [{icons['type_errors']}] Column type errors: {vc['type_errors']}")
        print(f"    [{icons['type_warnings']}] Column type warnings: {vc['type_warnings']}")
        print(f"    [{icons['dax_errors']}] Unresolved DAX references: {vc['dax_errors']}")
        print(f"    [{icons['cardinality']}] Cardinality mismatches: {vc['cardinality_mismatches']}")
        print(f"    Validation score: {validation['score']}/100")

        if not validation['passed'] and strict_merge:
            print("\n  ✗ Strict merge validation FAILED — generation blocked.")
            print("    Remove --strict-merge to proceed with warnings.")
            for cycle in validation['cycles']:
                print(f"    Cycle: {' → '.join(cycle)}")
            for tw in validation['type_warnings']:
                if tw['level'] == 'error':
                    print(f"    Type error: {tw['table']}.{tw['column']} — {tw['types']}")
            return {
                'assessment': assessment,
                'model_path': None,
                'report_paths': [],
                'validation': validation,
            }

        # 2b. Apply RLS consolidation
        merged_rls = merge_rls_roles(all_converted_objects, workbook_names)
        if merged_rls:
            merged['user_filters'] = merged_rls

        # 2c. Build column lineage
        lineage = build_column_lineage(all_converted_objects, workbook_names, assessment)
        lineage_annotations = generate_lineage_annotations(lineage)

        # Store lineage annotations in merged data for TMDL generator
        if lineage_annotations:
            merged['_lineage_annotations'] = lineage_annotations

        if output_format == 'fabric':
            _retain_shared_native_bindings(merged, native_hyper_provenance)
            # The Fabric generator checks the merged sources as well. Perform
            # the same checks here before this orchestrator creates directories.
            _preflight_fabric_datasources(merged.get('datasources', []))

        # 3. Determine output directory
        if output_dir:
            projects_dir = os.path.abspath(output_dir)
        else:
            default_subdir = 'fabric_projects' if output_format == 'fabric' else 'powerbi_projects'
            projects_dir = os.path.abspath(
                os.path.join('artifacts', default_subdir, 'shared')
            )
        os.makedirs(projects_dir, exist_ok=True)

        # Create the project directory
        project_dir = os.path.join(projects_dir, model_name)
        os.makedirs(project_dir, exist_ok=True)

        # 4. Generate the shared semantic model (or full Fabric project)
        fabric_results: dict = {}
        shared_fabric_evidence = None
        # Name index of the generated model; thin reports resolve their fields
        # against it so that visuals match the model's own names.
        model_context = None
        if output_format == 'fabric':
            print(f"\n  Step 3: Generating Fabric project '{model_name}' "
                  f"(Lakehouse + Dataflow + Notebook + SemanticModel + Pipeline)...")
            try:
                from powerbi_import.fabric_project_generator import (
                    FabricProjectGenerator,
                )
            except ImportError:
                from fabric_project_generator import FabricProjectGenerator

            fabric_gen = FabricProjectGenerator(output_dir=projects_dir)
            fabric_results = fabric_gen.generate_project(
                project_name=model_name,
                extracted_data=merged,
                calendar_start=calendar_start,
                calendar_end=calendar_end,
                culture=culture,
                languages=languages,
                include_report=False,
                premerge_unverified_hyper_sources=premerge_unverified_hyper_sources,
            )
            sm_dir = os.path.join(
                project_dir,
                f"{model_name}.SemanticModel",
            )
            # The Fabric generator places all artifacts inside project_dir
            fabric_project_path = _fabric_project_path(
                fabric_results, model_name, include_report=False)
            if workbook_names:
                shared_fabric_evidence = _mark_shared_fabric_evidence_incomplete(
                    fabric_project_path, fabric_results, workbook_names)
            model_context = fabric_results.get('artifacts', {}).get('semantic_model')
            # Also set sm_dir to wherever the SemanticModel was generated
            for dirpath, dirnames, _files in os.walk(fabric_project_path):
                for d in dirnames:
                    if d.endswith('.SemanticModel'):
                        sm_dir = os.path.join(dirpath, d)
                        break
            print(f"  [OK] Fabric project created: {fabric_project_path}")
        else:
            print(f"\n  Step 3: Generating shared semantic model '{model_name}'...")
            generator = PowerBIProjectGenerator(output_dir=projects_dir)

            # Store generation options
            generator._calendar_start = calendar_start
            generator._calendar_end = calendar_end
            generator._culture = culture
            generator._model_mode = model_mode or 'auto'
            generator._languages = languages

            # Generate SemanticModel
            sm_dir = generator.create_semantic_model_structure(
                project_dir, model_name, merged
            )
            model_context = generator.export_model_context()
        print(f"  [OK] Shared SemanticModel created: {sm_dir}")

        # Create a model-explorer report so the model can be opened in PBI Desktop
        # (only for PBIP format — Fabric projects don't need .pbip wrapper)
        if output_format != 'fabric':
            self._create_model_explorer_report(project_dir, model_name)

        # 5. Generate thin reports for each workbook
        # For Fabric: thin reports are generated inside the Fabric project dir
        # and reference the DirectLake SemanticModel via byPath
        thin_report_dir = project_dir
        if output_format == 'fabric':
            # Place thin reports inside the Fabric project dir from fabric_gen
            thin_report_dir = fabric_results.get('project_path', project_dir)

        print(f"\n  Step 4: Generating {len(workbook_names)} thin reports...")
        report_paths = []
        validation_issues = []
        orphaned_issue_count = 0

        thin_gen = ThinReportGenerator(model_name, thin_report_dir,
                                       live_connection=live_connection,
                                       model_context=model_context)

        # Build cross-report navigation
        nav_configs = build_cross_report_navigation(workbook_names, model_name)

        for wb_name, wb_data in zip(workbook_names, all_converted_objects):
            field_mapping = build_field_mapping(assessment, wb_name)

            # Validate fields before generating
            issues = validate_thin_report_fields(wb_data, merged, field_mapping)
            if issues:
                validation_issues.extend(issues)
                orphaned_issue_count += len(issues)
                for issue in issues[:3]:
                    print(f"    [WARN] {wb_name}: orphaned field '{issue['field']}' in {issue['location']}")

            report_path = thin_gen.generate_thin_report(
                wb_name, wb_data, field_mapping=field_mapping
            )
            report_paths.append(report_path)
            print(f"    [OK] Thin report: {wb_name}")

        if strict_thin_report and orphaned_issue_count > thin_report_max_orphans:
            print("\n  ✗ Strict thin-report validation FAILED.")
            print(
                f"    Orphaned references: {orphaned_issue_count} "
                f"(max allowed: {thin_report_max_orphans})"
            )
            print("    Remove --strict-thin-report or raise --thin-report-max-orphans.")
            return {
                'assessment': assessment,
                'model_path': None,
                'report_paths': [],
                'validation_issues': validation_issues,
                'validation': validation,
                'risk_analysis': risk_analysis,
                'rls_consolidations': rls_consolidations,
                'lineage': lineage,
                'navigation': nav_configs,
                'strict_thin_report_failed': True,
                'orphaned_issue_count': orphaned_issue_count,
                'thin_report_max_orphans': thin_report_max_orphans,
                **({'fabric_evidence': shared_fabric_evidence}
                   if shared_fabric_evidence is not None else {}),
            }

        # 6. Save artifacts (assessment, config, lineage, HTML report)
        self._save_shared_model_artifacts(
            project_dir, assessment, lineage, save_config,
            workbook_names, all_converted_objects, merged, model_name,
        )

        # 7. Write merge manifest for incremental add/remove
        manifest = build_merge_manifest(
            model_name=model_name,
            all_extracted=all_converted_objects,
            workbook_names=workbook_names,
            workbook_paths=workbook_paths,
            merged=merged,
            assessment=assessment,
            validation_score=validation.get('score', 0) if validation else 0,
            merge_config=None,
        )
        manifest_path = manifest.save(project_dir)
        print(f"  [OK] Merge manifest: {manifest_path}")

        print("\n  Shared Semantic Model migration complete!")
        print(f"  Output: {project_dir}")

        return {
            'assessment': assessment,
            'model_path': sm_dir,
            'report_paths': report_paths,
            'validation_issues': validation_issues,
            'validation': validation,
            'risk_analysis': risk_analysis,
            'rls_consolidations': rls_consolidations,
            'lineage': lineage,
            'navigation': nav_configs,
            'manifest': manifest,
            'orphaned_issue_count': orphaned_issue_count,
            'thin_report_max_orphans': thin_report_max_orphans,
            **({'fabric_evidence': shared_fabric_evidence}
               if shared_fabric_evidence is not None else {}),
        }

    def _create_model_explorer_report(self, project_dir, model_name):
        """Create a model-explorer report so the model can be opened in PBI Desktop.

        PBI Desktop requires a .Report artifact to open a .pbip file.
        """
        model_report_name = f"{model_name}_Model"
        model_report_dir = os.path.join(project_dir, f"{model_report_name}.Report")
        os.makedirs(os.path.join(model_report_dir, 'definition'), exist_ok=True)

        # .platform
        _write_json_file(os.path.join(model_report_dir, '.platform'), {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
            "metadata": {"type": "Report", "displayName": model_report_name},
            "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
        })

        # definition.pbir -> byPath to the shared model
        _write_json_file(os.path.join(model_report_dir, 'definition.pbir'), {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
            "version": "4.0",
            "datasetReference": {
                "byPath": {"path": f"../{model_name}.SemanticModel"},
            },
        })

        # Minimal report.json (empty report -- user opens Model view)
        _write_json_file(os.path.join(model_report_dir, 'definition', 'report.json'), {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/2.0.0/schema.json",
            "themeCollection": {
                "baseTheme": {
                    "name": "CY24SU06",
                    "reportVersionAtImport": "5.55",
                    "type": "SharedResources"
                }
            },
        })

        # version.json
        _write_json_file(os.path.join(model_report_dir, 'definition', 'version.json'), {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json",
            "dataFormatVersion": "2.0",
        })

        # A PBIR report without pages/pages.json is incomplete and will not open.
        page_name = 'ReportSection'
        page_dir = os.path.join(model_report_dir, 'definition', 'pages', page_name)
        os.makedirs(page_dir, exist_ok=True)
        _write_json_file(os.path.join(model_report_dir, 'definition', 'pages', 'pages.json'), {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json",
            "pageOrder": [page_name],
            "activePageName": page_name,
        })
        _write_json_file(os.path.join(page_dir, 'page.json'), {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json",
            "name": page_name,
            "displayName": "Model",
            "displayOption": "FitToPage",
            "height": 720,
            "width": 1280,
        })

        # .pbip pointing to the model-explorer report
        _write_json_file(os.path.join(project_dir, f"{model_name}.pbip"), {
            "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
            "version": "1.0",
            "artifacts": [{"report": {"path": f"{model_report_name}.Report"}}],
            "settings": {"enableAutoRecovery": True},
        })
        print(f"  [OK] Model explorer .pbip: {model_name}.pbip → {model_report_name}.Report")

    def _save_shared_model_artifacts(self, project_dir, assessment, lineage,
                                      save_config, workbook_names,
                                      all_converted_objects, merged, model_name):
        """Save merge assessment, config, lineage, and HTML report artifacts."""
        from powerbi_import.merge_assessment import generate_merge_report

        # Write merge assessment report
        assess_path = os.path.join(project_dir, 'merge_assessment.json')
        generate_merge_report(assessment, output_path=assess_path)
        print(f"\n  [OK] Merge assessment saved: {assess_path}")

        # Save merge config if requested
        if save_config:
            from powerbi_import.merge_config import save_merge_config
            config_path = os.path.join(project_dir, 'merge_config.json')
            save_merge_config(assessment, workbook_names, config_path, merged)
            print(f"  [OK] Merge config saved: {config_path}")

        # Save lineage report
        if lineage:
            lineage_path = os.path.join(project_dir, 'column_lineage.json')
            with open(lineage_path, 'w', encoding='utf-8') as f:
                json.dump(lineage, f, indent=2, ensure_ascii=False)
            print(f"  [OK] Column lineage saved: {lineage_path}")

        # Generate HTML merge report
        try:
            from powerbi_import.merge_report_html import generate_merge_html_report
        except ImportError:
            from merge_report_html import generate_merge_html_report

        html_path = os.path.join(project_dir, 'MERGE_REPORT.html')
        generate_merge_html_report(
            assessment=assessment,
            all_extracted=all_converted_objects,
            workbook_names=workbook_names,
            merged=merged,
            model_name=model_name,
            output_path=html_path,
        )
        print(f"  [OK] HTML merge report: {html_path}")


def main():
    """Main entry point"""

    import sys

    # Option to disable .pbip generation
    generate_pbip = '--no-pbip' not in sys.argv

    importer = PowerBIImporter()
    importer.import_all(generate_pbip=generate_pbip)


if __name__ == '__main__':
    main()
