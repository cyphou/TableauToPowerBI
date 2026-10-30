#!/usr/bin/env python3
"""Migrate the whole example corpus and enforce release floors.

Static validation is the release gate, so the floors are the ones static
validation can actually prove: every artefact migrates, every generated project
passes the Power BI Desktop openability preflight, and nothing reports a
blocker. Warnings are advisory and never fail the gate — unrecognised
connectors and documented approximations are information, not defects.

Each artefact is accounted for from the batch state that ``migrate.py --batch``
records, so a failure names its artefact, error category and details instead of
a bare exit code.

A few artefacts are known not to migrate. ``examples/corpus_known_limitations.json``
lists them, each with the reason. The list is an exact match in both
directions: an unlisted failure or blocker fails the gate, and so does a listed
artefact that now migrates, fails differently or reports other blockers. It can
only shrink towards empty and is never a place to park a regression.
Retracted outputs of exact known failures remain diagnostics, not published
projects; their failure records must agree with the batch state and no PBIP
entry point may remain.

Hyper extracts are exported through the Hyper API, so ``tableauhyperapi`` must
be installed. Without it every packaged extract would read as a corpus failure
that belongs to the environment, so the gate refuses to run.

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
from collections import Counter
from typing import NamedTuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Each batch is migrated separately so a failure names its corpus.
BATCHES = (
    ('tableau_samples', os.path.join('examples', 'tableau_samples')),
    ('real_world', os.path.join('examples', 'real_world')),
    ('prep_portfolio', os.path.join('examples', 'prep_portfolio')),
)

#: Source suffixes ``migrate.py --batch`` picks up; it also skips names starting with '~'.
ARTEFACT_SUFFIXES = ('.twb', '.twbx', '.tds', '.tdsx', '.tfl', '.tflx')

#: ``migrate.ExitCode.BATCH_PARTIAL_FAIL``: some artefacts failed, the rest migrated.
EXIT_BATCH_PARTIAL_FAIL = 7

STATE_FILE = '.migration_batch_state.json'
BASELINE_RELPATH = 'examples/corpus_known_limitations.json'
BASELINE_PATH = os.path.join(_REPO_ROOT, *BASELINE_RELPATH.split('/'))


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


class BaselineError(ValueError):
    """The known-limitations baseline cannot be trusted."""


def _strings(value, where: str, minimum: int) -> list[str]:
    if (not isinstance(value, list) or len(value) < minimum
            or not all(isinstance(item, str) and item.strip() for item in value)):
        raise BaselineError(
            f'{where} must be a list of at least {minimum} non-empty string(s)')
    return list(value)


def _reject_duplicate_keys(pairs):
    keys = [key for key, _ in pairs]
    duplicated = sorted({key for key in keys if keys.count(key) > 1})
    if duplicated:
        raise ValueError(f'duplicate key(s) {duplicated}')
    return dict(pairs)


def load_baseline(path: str = BASELINE_PATH) -> dict[str, dict]:
    """Load the known limitations as ``{'<batch>/<artefact>': entry}``.

    The file is reviewed data, so anything unexpected is an error rather than
    something to guess around: a typo must never quietly widen what passes.
    """
    try:
        with open(path, encoding='utf-8') as handle:
            data = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    except (OSError, ValueError) as exc:
        raise BaselineError(f'cannot read {path}: {exc}') from exc

    top_level = {'_comment', 'version', 'known_limitations'}
    if not isinstance(data, dict) or set(data) - top_level:
        raise BaselineError(f'{path}: expected an object with only {sorted(top_level)}')
    if data.get('version') != 1:
        raise BaselineError(f'{path}: unsupported version {data.get("version")!r}')
    entries = data.get('known_limitations')
    if not isinstance(entries, dict):
        raise BaselineError(f'{path}: "known_limitations" must be an object')

    labels = {label for label, _ in BATCHES}
    fields = {'category', 'details', 'blockers', 'reason'}
    baseline: dict[str, dict] = {}
    for key, entry in entries.items():
        batch, _, artefact = key.partition('/')
        if batch not in labels or not artefact.lower().endswith(ARTEFACT_SUFFIXES):
            raise BaselineError(
                f'{key!r}: keys read "<batch>/<artefact>" with a batch among {sorted(labels)}')
        if not isinstance(entry, dict) or set(entry) != fields:
            raise BaselineError(f'{key}: an entry has exactly the fields {sorted(fields)}')
        for field in ('category', 'reason'):
            if not isinstance(entry[field], str) or not entry[field].strip():
                raise BaselineError(f'{key}: {field} must be a non-empty string')
        baseline[key] = {
            'category': entry['category'],
            'details': _strings(entry['details'], f'{key}: details', 1),
            'blockers': _strings(entry['blockers'], f'{key}: blockers', 0),
            'reason': entry['reason'],
        }
    return baseline


def _hyper_reader_problem() -> str | None:
    """Why the Hyper reader cannot be used, or ``None`` when it can."""
    try:
        import tableauhyperapi  # noqa: F401
    except Exception as exc:  # ImportError, or OSError when the native library will not load
        return (f'the Hyper reader (tableauhyperapi) cannot be imported ({exc}). '
                'Install it with: pip install tableauhyperapi')
    return None


def _artefacts(source_dir: str) -> list[str]:
    """Source artefacts of a batch, discovered the way ``migrate.py --batch`` does."""
    found = []
    for root, _dirs, files in os.walk(source_dir):
        for name in files:
            if name.lower().endswith(ARTEFACT_SUFFIXES) and not name.startswith('~'):
                found.append(os.path.relpath(os.path.join(root, name), source_dir)
                             .replace(os.sep, '/'))
    return sorted(found)


def _read_state(out_dir: str) -> tuple[dict, str]:
    """The outcome ``migrate.py --batch`` recorded per artefact, or why there is none."""
    path = os.path.join(out_dir, STATE_FILE)
    state = _load(path)
    if not isinstance(state, dict) or not state:
        return {}, f'no readable batch state at {path}'
    return ({key.replace('\\', '/'): entry for key, entry in state.items()
             if isinstance(entry, dict)}, '')


def _result_of(entry: dict) -> dict:
    result = entry.get('result')
    return result if isinstance(result, dict) else {}


def _failure_of(entry: dict) -> tuple[str, str]:
    """Error category and details of a failed artefact."""
    result = _result_of(entry)
    return (str(result.get('error') or entry.get('error') or 'unknown'),
            str(result.get('error_details') or ''))


def _clip(text: str, limit: int = 300) -> str:
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 3] + '...'


def _mismatches(limitation: dict, category: str, details: str,
                observed: Counter) -> list[str]:
    """How a failed artefact departs from the failure that was reviewed."""
    problems = []
    if category != limitation['category']:
        problems.append(
            f'error category is {category!r}, recorded {limitation["category"]!r}')
    for fragment in limitation['details']:
        if fragment not in details:
            problems.append(f'error details no longer contain {fragment!r}')
    recorded = Counter(limitation['blockers'])
    for text in sorted((observed - recorded).elements()):
        problems.append(f'unrecorded blocker {text!r}')
    for text in sorted((recorded - observed).elements()):
        problems.append(f'recorded blocker no longer reported {text!r}')
    return problems


def _is_retracted_failure(out_dir: str, name: str, result: dict) -> bool:
    project_dir = os.path.join(out_dir, name)
    if glob.glob(os.path.join(project_dir, '*.pbip')):
        return False
    failure = result.get('failure')
    record = _load(os.path.join(project_dir, 'migration_failure.json'))
    verdict = _load(os.path.join(project_dir, 'openability_report.json'))
    diagnostics = verdict.get('diagnostics') if isinstance(verdict, dict) else None
    return (
        isinstance(failure, dict) and bool(failure)
        and failure.get('message') == result.get('error_details')
        and isinstance(record, dict) and record.get('schema_version') == '1.0'
        and record.get('status') == 'failed' and record.get('report_name') == name
        and record.get('completion_marker') == 'withheld'
        and record.get('failure') == failure
        and verdict.get('openable') is False
        and isinstance(diagnostics, dict)
        and diagnostics.get('pipeline_failure') == failure
    )


class Verdict(NamedTuple):
    failures: list[str]
    acknowledged: list[str]
    artefacts: int
    migrated: int
    blockers: int


def _account(label: str, source_dir: str, out_dir: str, exit_code: int,
             evidence: dict, baseline: dict[str, dict]) -> Verdict:
    """Judge one migrated batch against the floors and the known limitations.

    ``baseline`` maps ``'<batch>/<artefact>'`` to its reviewed limitation.
    """
    failures: list[str] = []
    acknowledged: list[str] = []

    openability_failures = {
        project: (f'{label}/{project}: will not open — '
                  f'{len(issues)} blocking issue(s): {issues[:3]}')
        for project, issues in evidence['not_openable']
    }
    for project in evidence['missing_gate']:
        failures.append(f'{label}/{project}: no openability verdict written')

    expected = _artefacts(source_dir)
    if not expected:
        failures.append(f'{label}: the corpus holds no artefacts to migrate')

    state, problem = _read_state(out_dir)
    if problem:
        # Without the state nothing is attributable to an artefact, so nothing is excused.
        failures.append(f'{label}: {problem}')
        if exit_code != 0:
            failures.append(f'{label}: migration exited {exit_code}')
        failures.extend(f'{label}/{name}: blocker — {text}'
                        for name, text in evidence['blockers'])
        return Verdict(list(openability_failures.values()) + failures,
                       acknowledged, 0, 0, len(evidence['blockers']))

    for artefact in expected:
        if artefact not in state:
            failures.append(f'{label}/{artefact}: the batch recorded no outcome')
    for artefact in sorted(set(state) - set(expected)):
        failures.append(f'{label}/{artefact}: outcome recorded for an artefact the '
                        'corpus does not contain (stale batch state?)')

    known = {key.split('/', 1)[1]: entry for key, entry in baseline.items()
             if key.split('/', 1)[0] == label}
    for artefact in sorted(set(known) - set(expected)):
        failures.append(f'{label}/{artefact}: listed as a known limitation but the '
                        'corpus has no such artefact — remove the entry')

    # A quality report is named after the artefact's report, and only blockers
    # that belong to a known limitation can be excused with it.
    owner = {}
    for artefact, entry in state.items():
        report = os.path.basename(str(_result_of(entry).get('quality_json') or ''))
        if report.startswith('migration_quality_') and report.endswith('.json'):
            owner[report[len('migration_quality_'):-len('.json')]] = artefact
    observed: dict[str, Counter] = {}
    for name, text in evidence['blockers']:
        if name in owner:
            observed.setdefault(owner[name], Counter())[text] += 1

    judged = set()
    accepted = set()
    unsuccessful = 0
    for artefact in sorted(state):
        entry = state[artefact]
        status = entry.get('status')
        subject = f'{label}/{artefact}'
        limitation = known.get(artefact)
        if status == 'success':
            if limitation:
                failures.append(f'{subject}: listed as a known limitation but now '
                                'migrates — remove the entry')
            continue
        unsuccessful += 1
        if status != 'failed':
            failures.append(f'{subject}: never finished (status {status!r})')
            continue
        category, details = _failure_of(entry)
        if limitation is None:
            failures.append(f'{subject}: failed ({category}) — {_clip(details)}')
            continue
        judged.add(artefact)
        problems = _mismatches(limitation, category, details,
                               observed.get(artefact, Counter()))
        if problems:
            failures.append(f'{subject}: no longer matches its known limitation — '
                            + '; '.join(problems))
        else:
            accepted.add(artefact)
            acknowledged.append(f'{subject}: {category} — {limitation["reason"]}')

    # Only matching, reviewed failures with no entry point are diagnostic output.
    # A published PBIP must always pass the independent openability floor.
    for project in list(openability_failures):
        artefact = owner.get(project)
        if (artefact in accepted
                and _is_retracted_failure(out_dir, project, _result_of(state[artefact]))):
            del openability_failures[project]

    for project, artefact in owner.items():
        if (state[artefact].get('status') == 'failed'
                and project not in openability_failures
                and glob.glob(os.path.join(out_dir, project, '*.pbip'))):
            failures.append(f'{label}/{artefact}: failed artefact published a PBIP entry point')

    blockers = 0
    for name, text in evidence['blockers']:
        if owner.get(name) not in judged:
            blockers += 1
            failures.append(f'{label}/{name}: blocker — {text}')

    expected_code = EXIT_BATCH_PARTIAL_FAIL if unsuccessful else 0
    if exit_code != expected_code:
        failures.append(f'{label}: migration exited {exit_code}, expected {expected_code} '
                        f'for {unsuccessful} unsuccessful artefact(s)')

    migrated = sum(1 for entry in state.values() if entry.get('status') == 'success')
    return Verdict(list(openability_failures.values()) + failures,
                   acknowledged, len(state), migrated, blockers)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output-dir', default=None,
                        help='Where to migrate (default: a temp directory)')
    parser.add_argument('--keep', action='store_true',
                        help='Keep the generated projects for inspection')
    args = parser.parse_args(argv)

    problem = _hyper_reader_problem()
    if problem:
        print(f'CORPUS GATE CANNOT RUN: {problem}')
        return 1
    try:
        baseline = load_baseline()
    except BaselineError as exc:
        print(f'CORPUS GATE CANNOT RUN: {exc}')
        return 1

    base = args.output_dir or tempfile.mkdtemp(prefix='corpus_gate_')
    os.makedirs(base, exist_ok=True)

    failures: list[str] = []
    acknowledged: list[str] = []
    totals = {'artefacts': 0, 'migrated': 0, 'openable': 0, 'blockers': 0}
    statuses: dict = {}

    for label, batch_dir in BATCHES:
        source = os.path.join(_REPO_ROOT, batch_dir)
        if not os.path.isdir(source):
            failures.append(f'{label}: corpus directory {batch_dir} not found')
            continue

        out_dir = os.path.join(base, label)
        code, output = _migrate(batch_dir, out_dir)
        evidence = _collect(out_dir)
        verdict = _account(label, source, out_dir, code, evidence, baseline)
        if any(failure.startswith(f'{label}: ') for failure in verdict.failures):
            # The batch as a whole misbehaved, so its own log is the evidence.
            print(output[-4000:])

        totals['artefacts'] += verdict.artefacts
        totals['migrated'] += verdict.migrated
        totals['openable'] += len(evidence['openable'])
        totals['blockers'] += verdict.blockers
        for status, count in evidence['statuses'].items():
            statuses[status] = statuses.get(status, 0) + count
        failures.extend(verdict.failures)
        acknowledged.extend(verdict.acknowledged)

        print(f'  {label:<18} artefacts={verdict.artefacts:<3} '
              f'migrated={verdict.migrated:<3} '
              f'known limitations={len(verdict.acknowledged):<2} '
              f'openable={len(evidence["openable"]):<3} '
              f'blockers={verdict.blockers}')

    print(f'\n  artefacts migrated : {totals["migrated"]} of {totals["artefacts"]}')
    print(f'  known limitations  : {len(acknowledged)}')
    print(f'  projects openable  : {totals["openable"]}')
    print(f'  blockers           : {totals["blockers"]}')
    print(f'  quality verdicts   : {statuses or "none"}')

    if not args.keep and not args.output_dir:
        import shutil
        shutil.rmtree(base, ignore_errors=True)
    else:
        print(f'  output kept in     : {base}')

    if acknowledged:
        print(f'\nKnown limitations, reviewed in {BASELINE_RELPATH}:')
        for line in acknowledged:
            print(f'  - {line}')

    if failures:
        print(f'\nCORPUS GATE FAILED ({len(failures)}):')
        for failure in failures:
            print(f'  - {failure}')
        print(f'\nFix the regression. Only a deliberate, reviewed limitation belongs '
              f'in {BASELINE_RELPATH}.')
        return 1

    print('\nCorpus gate passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
