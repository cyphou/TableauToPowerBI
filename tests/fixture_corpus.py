"""Name-free addressing for the committed test fixtures.

Workbook names are only permitted under ``examples/``, so fixture directories
and snapshots are keyed by a derived id instead. The id is a hash of the
workbook stem rather than an index, so adding or removing a workbook never
renumbers the others and invalidates their fixtures.

Failure messages should call :func:`describe` so a broken fixture still points
at a real file on disk.
"""

import glob
import hashlib
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES_DIR = os.path.join(ROOT, 'examples', 'tableau_samples')
MANIFEST = os.path.join(SAMPLES_DIR, 'fixture_corpus.json')

GOLDEN_DIR = os.path.join(ROOT, 'tests', 'golden')
BASELINES_DIR = os.path.join(ROOT, 'tests', 'baselines')


def fixture_id(workbook):
    """Return the stable, name-free fixture id for a workbook path or stem."""
    stem = os.path.splitext(os.path.basename(workbook))[0]
    digest = hashlib.sha256(stem.encode('utf-8')).hexdigest()[:8]
    return f'wb_{digest}'


def _manifest():
    with open(MANIFEST, encoding='utf-8') as fh:
        return json.load(fh)


def golden_workbooks():
    """Curated golden corpus as ``[(fixture_id, absolute_path), ...]``."""
    entries = []
    for name in _manifest()['golden']:
        path = os.path.join(SAMPLES_DIR, name)
        entries.append((fixture_id(name), path))
    return sorted(entries)


def all_workbooks():
    """Every sample workbook on disk, as ``[(fixture_id, absolute_path), ...]``."""
    paths = []
    for ext in ('*.twb', '*.twbx'):
        paths.extend(glob.glob(os.path.join(SAMPLES_DIR, ext)))
    paths = [p for p in paths if not os.path.basename(p).startswith('~')]
    return sorted((fixture_id(p), p) for p in paths)


def golden_path(fid):
    return os.path.join(GOLDEN_DIR, fid, 'visuals.json')


def baseline_path(fid):
    return os.path.join(BASELINES_DIR, f'{fid}.snapshot.json')


def describe(fid):
    """Resolve an id back to its workbook filename for readable assertions."""
    for known, path in all_workbooks():
        if known == fid:
            return os.path.basename(path)
    return fid
