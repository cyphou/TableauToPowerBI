"""Benchmark tests for Fabric generation performance at scale.

PowerShell:
    $env:RUN_BENCHMARKS = "1"
    ./.venv/Scripts/python.exe -m pytest tests/test_fabric_performance.py -v

Not run in CI by default; requires RUN_BENCHMARKS=1.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Skip the module unless benchmarks are explicitly enabled.
_RUN_BENCHMARKS = os.environ.get('RUN_BENCHMARKS') == '1'


def _workbook(name='Complex_Enterprise.twb'):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, 'examples', 'tableau_samples', name)


@unittest.skipUnless(_RUN_BENCHMARKS, 'Benchmark tests require RUN_BENCHMARKS=1')
class TestSingleFabricPerformance(unittest.TestCase):
    """Single workbook Fabric generation performance checks."""

    def test_single_fabric_generation_budget_and_artifacts(self):
        from scripts.run_perf_baseline import run_once

        wb = _workbook('Complex_Enterprise.twb')
        if not os.path.isfile(wb):
            self.skipTest(f'Fixture not found: {wb}')

        started = time.perf_counter()
        measurement = run_once(
            wb,
            output_format='fabric',
            verbose=False,
            measure_memory=False,
        )
        elapsed = time.perf_counter() - started

        phase = measurement.get('generation_phase_timings') or {}
        phase_names = set((phase.get('phases') or {}).keys())

        self.assertGreater(measurement['generation_seconds'], 0.0)
        self.assertLess(
            measurement['generation_seconds'],
            8.0,
            f"Single Fabric generation took {measurement['generation_seconds']:.2f}s (>8s)",
        )
        self.assertLess(
            elapsed,
            12.0,
            f"End-to-end run_once took {elapsed:.2f}s (>12s)",
        )

        required_phases = {
            'input_loading',
            'lakehouse',
            'dataflow',
            'notebook',
            'semantic_model',
            'report',
            'pipeline',
            'validation',
            'metadata',
            'bundle_validation',
        }
        self.assertTrue(
            required_phases.issubset(phase_names),
            f'Missing expected Fabric phases: {sorted(required_phases - phase_names)}',
        )


@unittest.skipUnless(_RUN_BENCHMARKS, 'Benchmark tests require RUN_BENCHMARKS=1')
class TestSharedFabricPerformance(unittest.TestCase):
    """Shared-model Fabric generation performance and integrity checks."""

    def _extract(self, workbook_path, output_dir):
        from tableau_export.extract_tableau_data import TableauExtractor

        ext = TableauExtractor(workbook_path, output_dir=output_dir)
        ok = ext.extract_all()
        self.assertTrue(ok, f'Extraction failed for {workbook_path}')

    def test_shared_fabric_generation_budget(self):
        from powerbi_import.import_to_powerbi import PowerBIImporter

        wb1 = _workbook('Enterprise_Sales.twb')
        wb2 = _workbook('Complex_Enterprise.twb')
        if not os.path.isfile(wb1) or not os.path.isfile(wb2):
            self.skipTest('Shared Fabric fixtures not found')

        run_dir = tempfile.mkdtemp(prefix='fabric_shared_perf_')
        extract1 = os.path.join(run_dir, 'extract1')
        extract2 = os.path.join(run_dir, 'extract2')
        out_dir = os.path.join(run_dir, 'out')
        os.makedirs(extract1)
        os.makedirs(extract2)
        os.makedirs(out_dir)

        try:
            self._extract(wb1, extract1)
            self._extract(wb2, extract2)

            importer1 = PowerBIImporter(source_dir=extract1)
            importer2 = PowerBIImporter(source_dir=extract2)
            obj1 = importer1._load_converted_objects()
            obj2 = importer2._load_converted_objects()

            started = time.perf_counter()
            result = importer1.import_shared_model(
                model_name='PerfSharedFabric',
                all_converted_objects=[obj1, obj2],
                workbook_names=['Enterprise_Sales', 'Complex_Enterprise'],
                output_dir=out_dir,
                output_format='fabric',
                force_merge=True,
            )
            elapsed = time.perf_counter() - started

            self.assertIsNotNone(result)
            self.assertIn('model_path', result)
            self.assertLess(elapsed, 25.0, f'Shared Fabric merge took {elapsed:.2f}s (>25s)')

            model_path = result['model_path']
            self.assertTrue(model_path and os.path.isdir(model_path), 'Shared model path missing')

            # Shared model intentionally emits no embedded report in include_report=False path.
            # Verify key Fabric artifacts exist.
            project_dir = os.path.dirname(model_path)
            expected_dirs = [
                f for f in os.listdir(project_dir)
                if f.endswith('.Lakehouse') or f.endswith('.Dataflow')
                or f.endswith('.Notebook') or f.endswith('.DataPipeline')
            ]
            self.assertGreaterEqual(
                len(expected_dirs),
                4,
                'Expected Fabric shared project artifacts were not generated',
            )
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)


class TestFabricBundleIntegrity(unittest.TestCase):
    """Integrity guard: fast-but-incomplete Fabric bundle must fail."""

    @patch('powerbi_import.fabric_project_generator.FabricProjectGenerator')
    @patch('powerbi_import.import_to_powerbi.PowerBIImporter')
    def test_fast_incomplete_bundle_fails(self, importer, generator):
        from scripts.run_perf_baseline import _generate_fabric

        importer.return_value._load_converted_objects.return_value = {}
        generator.return_value.generate_project.return_value = {
            'artifacts': {
                'lakehouse': {},
                'dataflow': {},
                'notebook': {},
                'semantic_model': {},
                # report intentionally missing
                'pipeline': {},
            },
            'validation': {
                'valid': False,
                'artifacts_checked': 5,
            },
        }

        with self.assertRaisesRegex(RuntimeError, 'six-artifact bundle'):
            _generate_fabric('extract', 'output', 'Report')


if __name__ == '__main__':
    unittest.main()
