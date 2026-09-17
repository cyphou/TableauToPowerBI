"""Tests for the repeatable performance baseline runner."""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_perf_baseline


class TestPerformanceStatistics(unittest.TestCase):
    def test_percentile_interpolates_ordered_values(self):
        self.assertEqual(run_perf_baseline._percentile([4.0], 0.95), 4.0)
        self.assertAlmostEqual(
            run_perf_baseline._percentile([1.0, 2.0, 3.0], 0.95),
            2.9,
        )

    def test_summary_includes_variation(self):
        summary = run_perf_baseline._summarize([1.0, 2.0, 3.0])
        self.assertEqual(summary['min'], 1.0)
        self.assertEqual(summary['median'], 2.0)
        self.assertEqual(summary['max'], 3.0)
        self.assertEqual(summary['variation_percent'], 100.0)

    def test_summary_rejects_empty_values(self):
        with self.assertRaisesRegex(ValueError, 'empty measurement'):
            run_perf_baseline._summarize([])


class TestRunBaseline(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workbook = os.path.join(self.temp_dir.name, 'fixture.twb')
        with open(self.workbook, 'w', encoding='utf-8') as handle:
            handle.write('<workbook/>')

    def tearDown(self):
        self.temp_dir.cleanup()

    @patch('scripts.run_perf_baseline.run_once')
    def test_warmup_is_excluded_from_measured_summary(self, run_once):
        warmup = {
            'extraction_seconds': 9.0,
            'generation_seconds': 9.0,
            'total_seconds': 18.0,
            'generation_phase_timings': None,
        }
        measured = {
            'extraction_seconds': 1.0,
            'generation_seconds': 2.0,
            'total_seconds': 3.0,
            'generation_phase_timings': {
                'phases': {'semantic_model': 1.0, 'report': 0.9},
            },
        }
        memory = {
            'extraction_seconds': 10.0,
            'generation_seconds': 20.0,
            'total_seconds': 30.0,
            'generation_phase_timings': {
                'phases': {'semantic_model': 10.0},
            },
            'extraction_peak_mb': 4.0,
            'generation_peak_mb': 5.0,
            'peak_mb': 5.0,
        }
        run_once.side_effect = [warmup, measured, measured, measured, memory]

        baseline = run_perf_baseline.run_baseline(
            self.workbook, runs=3, warmup=1,
        )

        self.assertEqual(run_once.call_count, 5)
        run_once.assert_called_with(
            self.workbook, output_format='pbip', verbose=False,
            measure_memory=True,
        )
        self.assertEqual(baseline['summary']['total_seconds']['median'], 3.0)
        self.assertEqual(baseline['summary']['peak_mb']['max'], 5.0)
        self.assertEqual(
            baseline['summary']['generation_phases']['semantic_model']['median'],
            1.0,
        )
        self.assertEqual(
            baseline['summary']['generation_phase_coverage_percent']['median'],
            95.0,
        )
        self.assertEqual(baseline['schema_version'], 3)
        self.assertEqual(baseline['memory_measurement']['peak_mb'], 5.0)
        self.assertEqual(len(baseline['measurements']), 3)
        self.assertTrue(baseline['stable'])
        self.assertFalse(baseline['timing_traced'])
        self.assertEqual(baseline['runs'], 3)
        self.assertEqual(baseline['warmup_runs'], 1)
        self.assertEqual(baseline['memory_runs'], 1)
        self.assertEqual(baseline['output_format'], 'pbip')

    @patch('scripts.run_perf_baseline.tracemalloc')
    def test_measure_only_traces_memory_when_requested(self, traced):
        _, _, untraced_peak = run_perf_baseline._measure(lambda: 'ok')
        self.assertIsNone(untraced_peak)
        traced.start.assert_not_called()

        traced.get_traced_memory.return_value = (128, 1024 * 1024)
        _, _, traced_peak = run_perf_baseline._measure(
            lambda: 'ok', trace_memory=True,
        )

        self.assertEqual(traced_peak, 1.0)
        traced.start.assert_called_once_with()
        traced.stop.assert_called_once_with()

    def test_rejects_invalid_run_counts(self):
        with self.assertRaisesRegex(ValueError, 'at least 1'):
            run_perf_baseline.run_baseline(self.workbook, runs=0)
        with self.assertRaisesRegex(ValueError, 'cannot be negative'):
            run_perf_baseline.run_baseline(self.workbook, warmup=-1)

    def test_rejects_missing_workbook(self):
        with self.assertRaises(FileNotFoundError):
            run_perf_baseline.run_baseline(
                os.path.join(self.temp_dir.name, 'missing.twb'),
            )

    def test_rejects_unknown_output_format(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported output format'):
            run_perf_baseline.run_baseline(
                self.workbook, output_format='unknown',
            )


class TestFabricBaselineGeneration(unittest.TestCase):
    def _fabric_result(self):
        return {
            'artifacts': {
                'lakehouse': {},
                'dataflow': {},
                'notebook': {},
                'semantic_model': {},
                'report': {},
                'pipeline': {},
            },
            'validation': {
                'valid': True,
                'artifacts_checked': 6,
            },
        }

    @patch('powerbi_import.fabric_project_generator.FabricProjectGenerator')
    @patch('powerbi_import.import_to_powerbi.PowerBIImporter')
    def test_accepts_valid_six_artifact_bundle(self, importer, generator):
        importer.return_value._load_converted_objects.return_value = {
            'datasources': [{}],
        }
        generator.return_value.generate_project.return_value = (
            self._fabric_result()
        )

        timings = run_perf_baseline._generate_fabric(
            'extract', 'output', 'Report',
        )

        generator.assert_called_once_with(output_dir='output')
        generator.return_value.generate_project.assert_called_once_with(
            'Report', {'datasources': [{}]},
        )
        self.assertIn('input_loading', timings['phases'])
        self.assertIn('bundle_validation', timings['phases'])
        self.assertIn('fabric_orchestration', timings['phases'])

    @patch('powerbi_import.fabric_project_generator.FabricProjectGenerator')
    @patch('powerbi_import.import_to_powerbi.PowerBIImporter')
    def test_rejects_incomplete_bundle(self, importer, generator):
        importer.return_value._load_converted_objects.return_value = {}
        result = self._fabric_result()
        result['artifacts'].pop('report')
        result['validation']['artifacts_checked'] = 5
        generator.return_value.generate_project.return_value = result

        with self.assertRaisesRegex(RuntimeError, 'six-artifact bundle'):
            run_perf_baseline._generate_fabric(
                'extract', 'output', 'Report',
            )


class TestBaselineCommand(unittest.TestCase):
    @patch('scripts.run_perf_baseline.run_baseline')
    def test_main_writes_json_baseline(self, run_baseline):
        run_baseline.return_value = {
            'summary': {
                'total_seconds': {'median': 1.25, 'p95': 1.5},
                'peak_mb': {'max': 12.0},
            },
            'stable': True,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, 'baseline.json')
            exit_code = run_perf_baseline.main([
                'fixture.twb', '--runs', '2', '--warmup', '0',
                '--format', 'fabric',
                '--output', output_path,
            ])

            self.assertEqual(exit_code, 0)
            with open(output_path, encoding='utf-8') as handle:
                saved = json.load(handle)

        self.assertEqual(saved, run_baseline.return_value)
        run_baseline.assert_called_once_with(
            'fixture.twb', runs=2, warmup=0, output_format='fabric',
            verbose=False,
        )


if __name__ == '__main__':
    unittest.main()
