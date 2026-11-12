"""Check static migration evidence for the pinned public Prep corpus."""

import argparse
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from powerbi_import.m_validator import validate_m_query
from scripts.check_corpus_gate import EXIT_BATCH_PARTIAL_FAIL
from scripts.fetch_prep_public_corpus import MANIFEST, fetch_corpus


def compare_report(report, baseline):
    """Return discrepancies without treating known blockers as successful flows."""
    if not isinstance(report, dict) or not isinstance(report.get('flows'), list):
        return ['Consolidated Prep report is missing or malformed']
    if not isinstance(baseline, dict) or not baseline:
        return ['Prep baseline is missing or malformed']
    flows = report['flows']
    if any(not isinstance(flow, dict) or not isinstance(flow.get('flow'), str)
           for flow in flows):
        return ['Consolidated Prep flow inventory is malformed']
    by_name = {flow['flow']: flow for flow in flows}
    if len(by_name) != len(flows):
        return ['Consolidated Prep flow names are duplicated']
    problems = []
    if set(by_name) != set(baseline):
        problems.append('Consolidated Prep flow inventory does not match the baseline')
    for name in sorted(set(by_name) & set(baseline)):
        flow = by_name[name]
        expected = baseline[name]
        if not isinstance(expected, dict) or expected.get('status') not in ('generated', 'blocked'):
            problems.append(f'{name}: baseline entry is malformed')
            continue
        status = flow.get('status')
        queries = flow.get('queries')
        blockers = flow.get('blockers')
        if (status not in ('generated', 'blocked') or not isinstance(queries, list)
                or not queries or not isinstance(blockers, list)
                or any(not isinstance(query, dict) for query in queries)
                or any(not isinstance(blocker, dict) or not isinstance(blocker.get('code'), str)
                       for blocker in blockers)):
            problems.append(f'{name}: query evidence is missing or malformed')
            continue
        summary = flow.get('summary')
        outputs = [query for query in queries if query.get('query_role') == 'output']
        output_ids = [query.get('output_node_id') for query in outputs]
        counts = {
            'output_count': len(outputs),
            'query_count': len(queries),
            'generated_queries': sum(query.get('status') == 'generated' for query in queries),
            'blocked_queries': sum(query.get('status') == 'blocked' for query in queries),
        }
        if (not outputs or any(not isinstance(node_id, str) or not node_id for node_id in output_ids)
                or len(set(output_ids)) != len(output_ids)
                or not isinstance(summary, dict)
                or any(type(summary.get(key)) is not int or summary[key] != value
                       for key, value in counts.items())):
            problems.append(f'{name}: output inventory or query counts are inconsistent')
            continue
        if status == 'generated':
            if blockers or any(query.get('status') != 'generated' or not query.get('m_file')
                               for query in queries):
                problems.append(f'{name}: generated flow has incomplete query evidence')
            required = expected.get('required_queries', [])
            if (not isinstance(required, list)
                    or not all(isinstance(item, str) and item for item in required)
                    or not set(required).issubset({query.get('name') for query in queries})):
                problems.append(f'{name}: baseline queries are missing')
        elif expected['status'] == 'generated':
            problems.append(f'{name}: previously generated flow is blocked')
        else:
            allowed = expected.get('blocker_codes')
            observed = {blocker['code'] for blocker in blockers}
            if (not isinstance(allowed, list) or not allowed
                    or not all(isinstance(item, str) and item for item in allowed)
                    or not observed
                    or not observed.issubset(set(allowed))):
                problems.append(f'{name}: blocker evidence differs from baseline')
            if any(query.get('status') != 'blocked' or query.get('m_file')
                   for query in queries):
                problems.append(f'{name}: blocked flow reports a generated query')
    return problems


def check_generated_files(report, output_dir):
    """Check current generated M files, without claiming native execution."""
    root = Path(output_dir).resolve()
    problems = []
    for flow in report['flows']:
        for query in flow['queries']:
            if query.get('status') != 'generated':
                continue
            path = query.get('m_file')
            if not isinstance(path, str) or not path or '\\' in path:
                problems.append(f"{flow['flow']}: invalid M file path")
                continue
            relative = PurePosixPath(path)
            if (relative.is_absolute() or any(part in ('..', '.') for part in relative.parts)
                    or relative.suffix.lower() != '.pq'):
                problems.append(f"{flow['flow']}: invalid M file path")
                continue
            target = root.joinpath(*relative.parts)
            try:
                if target.is_symlink() or not target.resolve().is_relative_to(root):
                    raise ValueError('M file is outside the output directory')
                text = target.read_text(encoding='utf-8')
                validation = query.get('static_m_validation')
                if (not text.strip() or not isinstance(validation, dict)
                        or validation.get('status') != 'pass' or validate_m_query(text)):
                    raise ValueError('M validation is missing or failed')
            except (OSError, UnicodeError, ValueError):
                problems.append(f"{flow['flow']}: generated M evidence is unavailable or invalid")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input_dir', help='Previously downloaded public Prep corpus')
    parser.add_argument('output_dir', help='New output directory for this batch')
    args = parser.parse_args(argv)
    try:
        with MANIFEST.open(encoding='utf-8') as stream:
            manifest = json.load(stream)
        entries = manifest['inputs']
        baseline = manifest['baseline']
        fetch_corpus(entries, args.input_dir, verify_only=True)
        input_dir = Path(args.input_dir).resolve()
        output_dir = Path(args.output_dir).resolve()
        if output_dir.exists() or output_dir.is_relative_to(input_dir):
            raise ValueError('Output directory must be new and outside the input directory')
        expected_names = {Path(entry['filename']).stem for entry in entries}
        if not isinstance(baseline, dict) or set(baseline) != expected_names:
            raise ValueError('Baseline does not match the corpus manifest')
        actual_paths = {path.relative_to(input_dir).as_posix()
                        for path in input_dir.rglob('*')
                        if path.is_file() and path.suffix.lower() in ('.tfl', '.tflx')}
        if actual_paths != {entry['filename'] for entry in entries}:
            raise ValueError('Input directory has missing or extra Prep files')
        process = subprocess.run(
            [sys.executable, str(ROOT / 'migrate.py'), 'batch', str(input_dir),
             '--output-dir', str(output_dir), '--quiet'],
            cwd=ROOT, capture_output=True, text=True, errors='replace', check=False,
        )
        with (output_dir / 'powerquery_migration_report.json').open(encoding='utf-8') as stream:
            report = json.load(stream)
        problems = compare_report(report, baseline)
        if not problems:
            problems.extend(check_generated_files(report, output_dir))
        expected_exit = EXIT_BATCH_PARTIAL_FAIL if any(
            flow.get('status') == 'blocked' for flow in report.get('flows', [])) else 0
        if process.returncode != expected_exit:
            problems.append(f'Batch exit code {process.returncode} did not match its flow statuses')
        if problems:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 1
        print(f"Static corpus check passed: {len(report['flows'])} flows; "
              f"{sum(flow['status'] == 'blocked' for flow in report['flows'])} remain blocked.")
        print('Native Power Query execution: not_run')
        print(f'Report: {output_dir / "powerquery_migration_report.html"}')
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f'Public Prep corpus check failed: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
