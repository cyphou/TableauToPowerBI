"""Full fidelity comparison — Tableau source vs Power BI target across
visual size, Power Query table/column coverage, and interactivity (filters,
actions, parameters, RLS).

For each Tableau workbook, runs a fresh isolated extraction + PBIP
generation, then runs all three comparisons:
    powerbi_import/visual_size_diff.py    — per-visual width/height
    powerbi_import/powerquery_diff.py     — table/column data coverage
    powerbi_import/interface_diff.py      — filters/actions/parameters/RLS

Usage:
    python scripts/compare_full_fidelity.py                       # demo corpus
    python scripts/compare_full_fidelity.py workbook1.twb wb2.twbx
    python scripts/compare_full_fidelity.py --output-json report.json
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
for source_dir in ('tableau_export', 'powerbi_import'):
    source_path = os.path.join(ROOT, source_dir)
    if source_path not in sys.path:
        sys.path.insert(0, source_path)

_DEFAULT_GLOBS = (
    os.path.join(ROOT, 'examples', 'tableau_samples', '*.twb'),
    os.path.join(ROOT, 'examples', 'tableau_samples', '*.twbx'),
    os.path.join(ROOT, 'examples', 'real_world', '*.twb'),
    os.path.join(ROOT, 'examples', 'real_world', '*.twbx'),
)


def _default_workbooks():
    paths = []
    for pattern in _DEFAULT_GLOBS:
        paths.extend(glob.glob(pattern))
    return sorted(set(paths))


def compare_workbook(workbook_path):
    """Run a fresh extraction + generation for one workbook, then compare."""
    from powerbi_import.import_to_powerbi import PowerBIImporter
    from powerbi_import.interface_diff import compare_report_interface
    from powerbi_import.powerquery_diff import compare_report_tables
    from powerbi_import.visual_size_diff import compare_report_visual_sizes
    from tableau_export.extract_tableau_data import TableauExtractor

    report_name = os.path.splitext(os.path.basename(workbook_path))[0]
    run_dir = tempfile.mkdtemp(prefix='ttpbi_fidelity_')
    extract_dir = os.path.join(run_dir, 'extract')
    output_dir = os.path.join(run_dir, 'output')
    os.makedirs(extract_dir)
    os.makedirs(output_dir)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            extractor = TableauExtractor(workbook_path, output_dir=extract_dir)
            if not extractor.extract_all():
                return {'report_name': report_name, 'error': 'extraction failed'}

            importer = PowerBIImporter(source_dir=extract_dir)
            extracted = importer._load_converted_objects()
            importer.import_all(generate_pbip=True, report_name=report_name,
                                output_dir=output_dir)
        project_dir = os.path.join(output_dir, report_name)

        result = {'report_name': report_name}
        if extracted.get('dashboards'):
            result['visual_sizes'] = compare_report_visual_sizes(
                extracted, project_dir, report_name)
        result['tables'] = compare_report_tables(extracted, project_dir, report_name)
        result['interface'] = compare_report_interface(extracted, project_dir, report_name)
        return result
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def _print_workbook_summary(workbook, result):
    name = os.path.basename(workbook)
    if 'error' in result:
        print(f"  {name:35s}  ERROR: {result['error']}")
        return

    parts = []
    vsize = result.get('visual_sizes', {}).get('summary')
    if vsize:
        parts.append(f"visuals {vsize['matched']}/{vsize['expected_visuals']}"
                     f" ({vsize['match_rate_percent']:.0f}%)")
    tables = result.get('tables', {}).get('summary')
    if tables:
        parts.append(f"tables {tables['tables_found']}/{tables['source_tables']}"
                     f" ({tables['table_match_percent']:.0f}%)"
                     f", cols {tables['avg_column_coverage_percent']:.0f}%")
    iface = result.get('interface', {})
    flags = []
    for key in ('filters', 'actions', 'parameters', 'rls'):
        if key in iface and not iface[key].get('covered', True):
            flags.append(key)
    iface_text = 'iface OK' if not flags else f"iface GAP({','.join(flags)})"
    parts.append(iface_text)
    print(f"  {name:35s}  " + '  '.join(parts))


def _build_parser():
    parser = argparse.ArgumentParser(
        description='Compare Tableau source vs Power BI target across '
                    'visual size, table/column data, and interactivity.')
    parser.add_argument('workbooks', nargs='*',
                        help='Workbook paths (default: full demo corpus).')
    parser.add_argument('--output-json', default=None,
                        help='Optional path to write the consolidated JSON report.')
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    workbooks = args.workbooks or _default_workbooks()
    if not workbooks:
        print('No workbooks found to compare.')
        return 1

    results = []
    any_gap = False
    for workbook in workbooks:
        result = compare_workbook(workbook)
        results.append({'workbook': workbook, 'result': result})
        _print_workbook_summary(workbook, result)
        if 'error' in result:
            any_gap = True
            continue
        vsize = result.get('visual_sizes', {}).get('summary')
        if vsize and vsize['mismatched']:
            any_gap = True
        tables = result.get('tables', {}).get('summary')
        if tables and tables['tables_found'] < tables['source_tables']:
            any_gap = True
        iface = result.get('interface', {})
        if any(not iface.get(k, {}).get('covered', True)
               for k in ('filters', 'actions', 'parameters', 'rls')):
            any_gap = True

    if args.output_json:
        output_path = os.path.abspath(args.output_json)
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as handle:
            json.dump(results, handle, indent=2)
            handle.write('\n')
        print(f'JSON report written to: {output_path}')

    return 1 if any_gap else 0


if __name__ == '__main__':
    raise SystemExit(main())
