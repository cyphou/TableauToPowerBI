#!/usr/bin/env python3
"""Migrate the whole example corpus and enforce release floors.

Static validation is the release gate, so the floors are the ones static
validation can actually prove: every artefact migrates, every generated project
passes the Power BI Desktop openability preflight, and nothing reports a
blocker. Warnings are advisory and never fail the gate — unrecognised
connectors and documented approximations are information, not defects.

Run locally exactly as CI does:

    python scripts/check_corpus_gate.py

Exits non-zero when a floor is breached, listing what broke.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Each batch is migrated separately so a failure names its corpus.
BATCHES = (
    ('tableau_samples', os.path.join('examples', 'tableau_samples')),
    ('real_world', os.path.join('examples', 'real_world')),
    ('prep_portfolio', os.path.join('examples', 'prep_portfolio')),
)


def _migrate(batch_dir: str, out_dir: str) -> tuple[int, str]:
    cmd = [sys.executable, os.path.join(_REPO_ROOT, 'migrate.py'),
           '--batch', os.path.join(_REPO_ROOT, batch_dir),
           '--output-dir', out_dir]
    proc = subprocess.run(cmd, cwd=_REPO_ROOT, capture_output=True, text=True,
                          errors='replace')
    return proc.returncode, (proc.stdout or '') + (proc.stderr or '')


def _load(path: str):
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _collect(out_dir: str) -> dict:
    """Read the evidence each migration left behind."""
    openable, not_openable, missing_gate = [], [], []
    for report in glob.glob(os.path.join(out_dir, '*', 'openability_report.json')):
        project = os.path.basename(os.path.dirname(report))
        verdict = _load(report)
        if verdict is None:
            missing_gate.append(project)
        elif verdict.get('openable'):
            openable.append(project)
        else:
            not_openable.append((project, verdict.get('blocking_issues') or []))

    blockers = []
    statuses = {}
    for report in glob.glob(os.path.join(out_dir, 'migration_quality_*.json')):
        data = _load(report) or {}
        name = os.path.basename(report)[len('migration_quality_'):-len('.json')]
        statuses[data.get('status', '?')] = statuses.get(data.get('status', '?'), 0) + 1
        for blocker in (data.get('blockers') or []):
            blockers.append((name, str(blocker)))

    # A .pbip project must ship the openability verdict; a prep flow has none.
    for pbip in glob.glob(os.path.join(out_dir, '*', '*.pbip')):
        project = os.path.basename(os.path.dirname(pbip))
        if project not in openable and project not in [p for p, _ in not_openable]:
            missing_gate.append(project)

    return {
        'openable': sorted(openable),
        'not_openable': not_openable,
        'missing_gate': sorted(set(missing_gate)),
        'blockers': blockers,
        'statuses': statuses,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', default=None,
                        help='Where to migrate (default: a temp directory)')
    parser.add_argument('--keep', action='store_true',
                        help='Keep the generated projects for inspection')
    args = parser.parse_args(argv)

    base = args.output_dir or tempfile.mkdtemp(prefix='corpus_gate_')
    os.makedirs(base, exist_ok=True)

    failures: list[str] = []
    totals = {'openable': 0, 'blockers': 0}
    statuses: dict = {}

    for label, batch_dir in BATCHES:
        source = os.path.join(_REPO_ROOT, batch_dir)
        if not os.path.isdir(source):
            print(f'  {label}: corpus not found, skipped')
            continue

        out_dir = os.path.join(base, label)
        code, output = _migrate(batch_dir, out_dir)
        if code != 0:
            failures.append(f'{label}: migration exited {code}')
            print(output[-4000:])

        evidence = _collect(out_dir)
        totals['openable'] += len(evidence['openable'])
        totals['blockers'] += len(evidence['blockers'])
        for status, count in evidence['statuses'].items():
            statuses[status] = statuses.get(status, 0) + count

        print(f'  {label:<18} openable={len(evidence["openable"]):<3} '
              f'blockers={len(evidence["blockers"])}')

        for project, issues in evidence['not_openable']:
            failures.append(f'{label}/{project}: will not open — '
                            f'{len(issues)} blocking issue(s): {issues[:3]}')
        for project in evidence['missing_gate']:
            failures.append(f'{label}/{project}: no openability verdict written')
        for name, blocker in evidence['blockers']:
            failures.append(f'{label}/{name}: blocker — {blocker}')

    print(f'\n  projects openable : {totals["openable"]}')
    print(f'  blockers          : {totals["blockers"]}')
    print(f'  quality verdicts  : {statuses or "none"}')

    if not args.keep and not args.output_dir:
        import shutil
        shutil.rmtree(base, ignore_errors=True)
    else:
        print(f'  output kept in    : {base}')

    if failures:
        print(f'\nCORPUS GATE FAILED ({len(failures)}):')
        for failure in failures:
            print(f'  - {failure}')
        return 1

    print('\nCorpus gate passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
