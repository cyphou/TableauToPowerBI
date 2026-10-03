"""Compare Tableau sources with already generated Power BI projects.

    python scripts/compare_source_fidelity.py SOURCE_ROOT OPENING_REPORT OUTPUT_JSON

All paths and field-level evidence stay in OUTPUT_JSON outside the repository.
No migrated project is modified.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from powerbi_import.interface_diff import compare_report_interface
from powerbi_import.artifact_diff import _parse_tmdl_table
from powerbi_import.powerquery_diff import (
    compare_calculation_metadata, compare_caption_metadata,
    compare_report_counts, compare_report_metadata, compare_report_tables,
    compare_semantic_metadata,
)
from powerbi_import.visual_diff import generate_visual_diff_json
from powerbi_import.visual_size_diff import compare_report_visual_sizes
from tableau_export.extract_tableau_data import TableauExtractor


_EXTRACTED = ('worksheets', 'dashboards', 'datasources', 'calculations',
              'parameters', 'filters', 'actions', 'user_filters')


def _discover_projects(project_roots):
    projects = {}
    for root in project_roots:
        in_root = {}
        for path in glob.glob(os.path.join(root, '**', '*.pbip'), recursive=True):
            key = os.path.splitext(os.path.basename(path))[0].casefold()
            current = in_root.get(key)
            if current is None or len(path) < len(current):
                in_root[key] = path
        projects.update(in_root)
    return projects


def compare_existing(source_root, opening_report, project_roots=()):
    with open(opening_report, encoding='utf-8') as stream:
        evidence = json.load(stream)
    if not isinstance(evidence, list):
        raise ValueError('Opening report must contain a list of Desktop records')
    sources = {}
    for base, dirs, files in os.walk(source_root):
        dirs[:] = [folder for folder in dirs if folder.lower() != 'migration_output']
        for filename in files:
            if filename.lower().endswith(('.twb', '.twbx')):
                sources.setdefault(os.path.splitext(filename)[0].casefold(), []).append(
                    os.path.join(base, filename))
    projects = _discover_projects(project_roots)

    results = []
    for index, item in enumerate(evidence, 1):
        original_pbip = item.get('pbip_path', '')
        stem = os.path.splitext(os.path.basename(original_pbip))[0]
        pbip = projects.get(stem.casefold()) if project_roots else original_pbip
        candidates = sources.get(stem.casefold(), [])
        if len(candidates) != 1 or not os.path.isfile(pbip):
            results.append({'report': stem, 'status': 'unmatched_source_or_project',
                            'source_candidates': len(candidates)})
            continue
        project = os.path.dirname(pbip)
        source = candidates[0]
        with tempfile.TemporaryDirectory(prefix='ttpbi_source_audit_') as output:
            with contextlib.redirect_stdout(io.StringIO()):
                extractor = TableauExtractor(source, output_dir=output)
                succeeded = extractor.extract_all()
            if not succeeded:
                results.append({'report': stem, 'status': 'extraction_failed'})
                continue
            extracted = {}
            for name in _EXTRACTED:
                path = os.path.join(output, name + '.json')
                if os.path.isfile(path):
                    with open(path, encoding='utf-8') as stream:
                        extracted[name] = json.load(stream)
        visuals = generate_visual_diff_json(extracted, project)
        model_symbols = set()
        for table_file in glob.glob(os.path.join(project, '*.SemanticModel', 'definition',
                                                  'tables', '*.tmdl')):
            model_table = _parse_tmdl_table(table_file)
            if model_table:
                model_symbols.update(item['name'].casefold()
                                     for key in ('columns', 'measures')
                                     for item in model_table[key])
        for visual in visuals['visuals']:
            visual['missing_tooltip_details'] = [
                {'field': field,
                 'model_status': ('exact_model_symbol' if field.casefold() in model_symbols
                                  else 'no_exact_model_symbol')}
                for field in visual['missing_tooltips']
            ]
        tables = compare_report_tables(extracted, project, stem)
        metadata = compare_report_metadata(extracted, project, stem)
        calculations = compare_calculation_metadata(extracted, project, stem)
        counts = compare_report_counts(extracted, project, stem, tables, calculations)
        captions = compare_caption_metadata(extracted, project, stem)
        semantics = compare_semantic_metadata(extracted, project, stem)
        interface = compare_report_interface(extracted, project, stem)
        sizes = (compare_report_visual_sizes(extracted, project, stem)
                 if extracted.get('dashboards') else None)
        gaps = sum(len(visual['missing_tooltips']) for visual in visuals['visuals'])
        table_summary = tables['summary']
        interface_gaps = [name for name, result in interface.items()
                          if isinstance(result, dict) and result.get('covered') is False]
        desktop_data = (item.get('data_load') or {}).get('status', 'not_verified')
        if os.path.normcase(os.path.abspath(pbip)) != os.path.normcase(
                os.path.abspath(original_pbip)):
            desktop_data = 'not_verified'
        complete = (not gaps and not visuals['unmapped']
                    and table_summary['table_match_percent'] == 100
                    and table_summary['avg_column_coverage_percent'] == 100
                    and not table_summary['sourceless_tables']
                    and not any(count for status, count in metadata['summary'].items()
                                if status != 'type_match')
                    and not any(calculations['summary'][status]
                                for status in ('missing_symbol', 'ambiguous_symbol',
                                               'type_mismatch'))
                    and not captions['summary']['not_found_in_model']
                    and not any(semantics['summary'][status]
                                for status in ('category_mismatch', 'unmapped_role',
                                               'column_not_found', 'hidden_mismatch'))
                    and not interface_gaps and desktop_data == 'verified')
        results.append({
            'report': stem, 'status': 'compared' if complete else 'needs_review',
            'source': source, 'project': project,
            'desktop_data': desktop_data, 'interface_gaps': interface_gaps,
            'visuals': visuals, 'tables': tables, 'counts': counts, 'metadata': metadata,
            'calculations': calculations,
            'captions': captions,
            'semantic_metadata': semantics,
            'interface': interface,
            'visual_sizes': sizes,
        })
        print(f'{index}/{len(evidence)} compared; missing tooltip roles={gaps}; '
              f'unmapped worksheets={visuals["unmapped"]}')
    return {'total': len(results), 'reports': results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source_root')
    parser.add_argument('opening_report')
    parser.add_argument('output_json')
    parser.add_argument('--projects-root', action='append', default=[],
                        help='one or more new PBIP output roots; every source must be found')
    args = parser.parse_args(argv)
    report = compare_existing(args.source_root, args.opening_report,
                              args.projects_root)
    with open(args.output_json, 'w', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False)
    print(f'Compared {report["total"]} reports; '
          f'{sum(row["status"] != "compared" for row in report["reports"])} need review')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())