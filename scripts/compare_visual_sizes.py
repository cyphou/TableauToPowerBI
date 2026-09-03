"""Compare Tableau vs Power BI visual sizes across migrated reports.

For each Tableau workbook, runs a fresh isolated extraction + PBIP generation,
then compares every dashboard's expected visual sizes (recomputed from the
extracted zone geometry) against the actual sizes written into the generated
``visual.json`` files. Only width/height are compared — see
``powerbi_import/visual_size_diff.py`` for the rationale.

Usage:
    python scripts/compare_visual_sizes.py                       # demo corpus
    python scripts/compare_visual_sizes.py workbook1.twb wb2.twbx
    python scripts/compare_visual_sizes.py --output-json report.json
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
    """Run a fresh extraction + generation for one workbook and compare sizes."""
    from powerbi_import.import_to_powerbi import PowerBIImporter
    from powerbi_import.visual_size_diff import compare_report_visual_sizes
    from tableau_export.extract_tableau_data import TableauExtractor

    report_name = os.path.splitext(os.path.basename(workbook_path))[0]
    run_dir = tempfile.mkdtemp(prefix='ttpbi_size_diff_')
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
            if not extracted.get('dashboards'):
                return {'report_name': report_name, 'skipped': 'no dashboards'}

            importer.import_all(generate_pbip=True, report_name=report_name,
                                output_dir=output_dir)
        project_dir = os.path.join(output_dir, report_name)
        return compare_report_visual_sizes(extracted, project_dir, report_name)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def _build_parser():
    parser = argparse.ArgumentParser(
        description='Compare Tableau vs Power BI visual sizes across reports.')
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
    total_expected = total_matched = total_mismatched = 0
    for workbook in workbooks:
        result = compare_workbook(workbook)
        results.append(result)
        summary = result.get('summary')
        if not summary:
            note = result.get('error') or result.get('skipped') or 'no summary'
            print(f"  {os.path.basename(workbook):40s}  {note}")
            continue
        total_expected += summary['expected_visuals']
        total_matched += summary['matched']
        total_mismatched += summary['mismatched']
        status = 'OK' if summary['mismatched'] == 0 else 'MISMATCH'
        print(f"  {os.path.basename(workbook):40s}  "
              f"{summary['matched']}/{summary['expected_visuals']} matched "
              f"({summary['match_rate_percent']:.1f}%)  [{status}]")

    overall_rate = (round(total_matched / total_expected * 100, 1)
                   if total_expected else 100.0)
    print(f"\nOverall: {total_matched}/{total_expected} visuals matched "
          f"({overall_rate:.1f}%), {total_mismatched} mismatch(es)")

    if args.output_json:
        output_path = os.path.abspath(args.output_json)
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as handle:
            json.dump({
                'reports': results,
                'summary': {
                    'expected_visuals': total_expected,
                    'matched': total_matched,
                    'mismatched': total_mismatched,
                    'match_rate_percent': overall_rate,
                },
            }, handle, indent=2)
            handle.write('\n')
        print(f'JSON report written to: {output_path}')

    return 0 if total_mismatched == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
