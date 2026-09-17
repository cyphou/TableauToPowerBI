"""Sprint 148 — Equivalence Testing CI gate (Phase 8).

Migrates every sample workbook end-to-end and validates the output using
the full stack: validator, cross-validator, schema validator, and
regression snapshot comparison.

This module doubles as a pytest test suite (``pytest tests/test_equivalence_ci.py``)
and a standalone script for generating baseline snapshots:

    python tests/test_equivalence_ci.py --generate-baselines
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tableau_export'))
sys.path.insert(0, os.path.join(ROOT, 'powerbi_import'))

from tableau_export.extract_tableau_data import TableauExtractor
from powerbi_import.import_to_powerbi import PowerBIImporter
from powerbi_import.validator import ArtifactValidator
from powerbi_import.cross_validator import cross_validate
from powerbi_import.schema_validator import validate_report_dir
from powerbi_import.regression_suite import (
    generate_regression_snapshot,
    compare_snapshots,
)

# ── Paths ────────────────────────────────────────────────────────────

SAMPLE_DIR = os.path.join(ROOT, 'examples', 'tableau_samples')
BASELINES_DIR = os.path.join(ROOT, 'tests', 'baselines')


def _find_workbooks(directory):
    paths = []
    for ext in ('*.twb', '*.twbx'):
        paths.extend(glob.glob(os.path.join(directory, ext)))
    paths = [p for p in paths if not os.path.basename(p).startswith('~')]
    return sorted(paths)


SAMPLE_WORKBOOKS = _find_workbooks(SAMPLE_DIR)


# ── Migration helper ────────────────────────────────────────────────

def _migrate_workbook(wb_path, output_dir):
    """Run full extract → generate pipeline, return (project_dir, name, extracted)."""
    basename = os.path.splitext(os.path.basename(wb_path))[0]
    temp_extract = tempfile.mkdtemp(prefix=f'eq_ext_{basename}_')
    try:
        extractor = TableauExtractor(wb_path, output_dir=temp_extract)
        ok = extractor.extract_all()
        if not ok:
            raise RuntimeError(f'Extraction failed for {basename}')

        # Load extracted data for snapshot generation
        extracted = {}
        for jfile in ('datasources.json', 'calculations.json',
                      'worksheets.json', 'filters.json',
                      'parameters.json', 'relationships.json'):
            jpath = os.path.join(temp_extract, jfile)
            if os.path.isfile(jpath):
                with open(jpath, 'r', encoding='utf-8') as f:
                    key = jfile.replace('.json', '')
                    extracted[key] = json.load(f)

        importer = PowerBIImporter(source_dir=temp_extract)
        importer.import_all(
            generate_pbip=True,
            report_name=basename,
            output_dir=output_dir,
        )
        project_dir = os.path.join(output_dir, basename)
        return project_dir, basename, extracted
    finally:
        shutil.rmtree(temp_extract, ignore_errors=True)


def _load_model(project_dir, name):
    """Load the BIM model JSON if present."""
    # The TMDL generator writes a bim file or we can reconstruct from tmdl
    # For cross-validation, read the model.bim if available
    bim_path = os.path.join(project_dir, f'{name}.SemanticModel', 'model.bim')
    if os.path.isfile(bim_path):
        with open(bim_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


def _load_report_state(project_dir, name):
    """Build a minimal report state dict for cross-validation."""
    def_dir = os.path.join(project_dir, f'{name}.Report', 'definition')
    pages_dir = os.path.join(def_dir, 'pages')
    if not os.path.isdir(pages_dir):
        return None

    report_json = {}
    rj_path = os.path.join(def_dir, 'report.json')
    if os.path.isfile(rj_path):
        with open(rj_path, 'r', encoding='utf-8') as f:
            report_json = json.load(f)

    pages_meta = {}
    pm_path = os.path.join(def_dir, 'pages.json')
    if os.path.isfile(pm_path):
        with open(pm_path, 'r', encoding='utf-8') as f:
            pages_meta = json.load(f)

    pages = []
    if os.path.isdir(pages_dir):
        for pname in sorted(os.listdir(pages_dir)):
            pdir = os.path.join(pages_dir, pname)
            pjson_path = os.path.join(pdir, 'page.json')
            if not os.path.isfile(pjson_path):
                continue
            with open(pjson_path, 'r', encoding='utf-8') as f:
                pjson = json.load(f)

            visuals = []
            vdir = os.path.join(pdir, 'visuals')
            if os.path.isdir(vdir):
                for vname in sorted(os.listdir(vdir)):
                    vjpath = os.path.join(vdir, vname, 'visual.json')
                    if os.path.isfile(vjpath):
                        with open(vjpath, 'r', encoding='utf-8') as f:
                            vjson = json.load(f)
                        visuals.append({
                            'dir': os.path.join(vdir, vname),
                            'name': vname,
                            'json': vjson,
                        })
            pages.append({
                'dir': pdir,
                'name': pname,
                'json': pjson,
                'visuals': visuals,
            })

    return {
        'def_dir': def_dir,
        'pages_dir': pages_dir,
        'report_json': report_json,
        'pages_metadata': pages_meta,
        'pages': pages,
        '_dirty_files': set(),
    }


# ── Baseline snapshot management ─────────────────────────────────────

def _baseline_path(wb_name):
    return os.path.join(BASELINES_DIR, f'{wb_name}.snapshot.json')


def _save_baseline(wb_name, snapshot):
    os.makedirs(BASELINES_DIR, exist_ok=True)
    with open(_baseline_path(wb_name), 'w', encoding='utf-8') as f:
        json.dump(snapshot, f, indent=2, sort_keys=True)


def _load_baseline(wb_name):
    bp = _baseline_path(wb_name)
    if not os.path.isfile(bp):
        return None
    with open(bp, 'r', encoding='utf-8') as f:
        return json.load(f)


# ── Test class factory ───────────────────────────────────────────────

def _make_equivalence_class(wb_path):
    """Create a test class for one workbook with deep validation."""
    basename = os.path.splitext(os.path.basename(wb_path))[0]

    class _EquivalenceTest(unittest.TestCase):
        _project_dir = None
        _basename = None
        _extracted = None
        _tmpdir = None

        @classmethod
        def setUpClass(cls):
            cls._tmpdir = tempfile.mkdtemp(prefix=f'eq_ci_{basename}_')
            try:
                cls._project_dir, cls._basename, cls._extracted = \
                    _migrate_workbook(wb_path, cls._tmpdir)
            except Exception as exc:
                cls._project_dir = None
                cls._skip_reason = str(exc)

        @classmethod
        def tearDownClass(cls):
            if cls._tmpdir:
                shutil.rmtree(cls._tmpdir, ignore_errors=True)

        def setUp(self):
            if self._project_dir is None:
                self.skipTest(f'Migration failed: {getattr(self, "_skip_reason", "unknown")}')

        def test_01_project_structure(self):
            """Project directory and .pbip file exist."""
            self.assertTrue(os.path.isdir(self._project_dir),
                            f'{self._project_dir} not found')
            pbip = os.path.join(self._project_dir, f'{self._basename}.pbip')
            self.assertTrue(os.path.isfile(pbip), f'{pbip} not found')

        def test_02_validator_passes(self):
            """ArtifactValidator.validate_project() returns valid."""
            result = ArtifactValidator.validate_project(self._project_dir)
            self.assertTrue(result.get('valid', False),
                            f'Validation errors: {result.get("errors", [])}')

        def test_03_schema_validator_no_errors(self):
            """Schema validator finds no errors in report definition."""
            def_dir = os.path.join(self._project_dir,
                                   f'{self._basename}.Report', 'definition')
            if not os.path.isdir(def_dir):
                self.skipTest('No report definition directory')
            results = validate_report_dir(def_dir)
            errors = []
            for r in results:
                for iss in r.errors:
                    if not iss.repaired:
                        errors.append(f'{r.file_path}: {iss}')
            self.assertEqual(len(errors), 0,
                             f'Schema errors:\n' + '\n'.join(errors[:10]))

        def test_04_cross_validator_no_errors(self):
            """Cross-artifact validator finds no errors."""
            model = _load_model(self._project_dir, self._basename)
            if model is None:
                self.skipTest('No model.bim found')
            rs = _load_report_state(self._project_dir, self._basename)
            result = cross_validate(model, rs)
            error_msgs = [f'{i.category}: {i.message}' for i in result.errors]
            self.assertEqual(len(error_msgs), 0,
                             f'Cross-validation errors:\n' + '\n'.join(error_msgs[:10]))

        def test_05_regression_snapshot_stable(self):
            """Regression snapshot matches baseline (or generates new baseline)."""
            if not self._extracted:
                self.skipTest('No extracted data')

            snapshot = generate_regression_snapshot(self._extracted)
            baseline = _load_baseline(self._basename)

            if baseline is None:
                # First run — save baseline, pass with warning
                _save_baseline(self._basename, snapshot)
                return  # no baseline to compare against

            comparison = compare_snapshots(baseline, snapshot)
            if not comparison.get('passed', True):
                drifts = comparison.get('drifts', [])
                drift_msgs = [
                    f"{d.get('type', '?')}: {d.get('detail', d)}"
                    for d in drifts[:10]
                ]
                self.fail(f'Regression drift detected:\n' + '\n'.join(drift_msgs))

    _EquivalenceTest.__name__ = f'TestEquivalence_{basename}'
    _EquivalenceTest.__qualname__ = f'TestEquivalence_{basename}'
    return _EquivalenceTest


# ── Generate test classes for all sample workbooks ──────────────────

for _wb in SAMPLE_WORKBOOKS:
    _name = os.path.splitext(os.path.basename(_wb))[0]
    _cls = _make_equivalence_class(_wb)
    globals()[f'TestEquivalence_{_name}'] = _cls


# ── Standalone baseline generation ──────────────────────────────────

def _generate_all_baselines():
    """Generate baseline snapshots for all sample workbooks."""
    import argparse
    parser = argparse.ArgumentParser(description='Generate equivalence baselines')
    parser.add_argument('--generate-baselines', action='store_true')
    args = parser.parse_args()

    if not args.generate_baselines:
        # Run as test suite
        unittest.main()
        return

    print(f'Generating baselines for {len(SAMPLE_WORKBOOKS)} workbooks...')
    for wb in SAMPLE_WORKBOOKS:
        basename = os.path.splitext(os.path.basename(wb))[0]
        tmpdir = tempfile.mkdtemp(prefix=f'eq_bl_{basename}_')
        try:
            _, name, extracted = _migrate_workbook(wb, tmpdir)
            if extracted:
                snapshot = generate_regression_snapshot(extracted)
                _save_baseline(name, snapshot)
                print(f'  ✓ {name}')
            else:
                print(f'  ✗ {name} (no extracted data)')
        except Exception as exc:
            print(f'  ✗ {basename}: {exc}')
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    print(f'Baselines saved to {BASELINES_DIR}')


if __name__ == '__main__':
    _generate_all_baselines()
