"""Tests for scripts.compare_perf_baselines."""

import json
import os
import tempfile
import unittest

from scripts import compare_perf_baselines


def _baseline(workbook_name, output_format='pbip', total=1.0, generation=0.8,
              extraction=0.2, peak=5.0, stable=True):
    return {
        'schema_version': 3,
        'workbook': f'/fixtures/{workbook_name}',
        'output_format': output_format,
        'stable': stable,
        'summary': {
            'total_seconds': {'median': total, 'p95': total * 1.1},
            'generation_seconds': {'median': generation},
            'extraction_seconds': {'median': extraction},
            'peak_mb': {'max': peak},
        },
    }


class TestComparePerfBaselines(unittest.TestCase):
    def test_detects_regressions_over_threshold(self):
        with tempfile.TemporaryDirectory() as current_dir, tempfile.TemporaryDirectory() as previous_dir:
            current = _baseline('Complex_Enterprise.twb', total=1.2, generation=0.95)
            previous = _baseline('Complex_Enterprise.twb', total=1.0, generation=0.8)

            with open(os.path.join(current_dir, 'complex.json'), 'w', encoding='utf-8') as handle:
                json.dump(current, handle)
            with open(os.path.join(previous_dir, 'complex.json'), 'w', encoding='utf-8') as handle:
                json.dump(previous, handle)

            report = compare_perf_baselines.compare_baselines(
                current_dir=current_dir,
                previous_dir=previous_dir,
                threshold_percent=10.0,
            )

            fixture = report['fixtures']['Complex_Enterprise.twb:pbip']
            self.assertEqual(report['summary']['status'], 'regression')
            self.assertGreaterEqual(len(fixture['regressions']), 1)

    def test_handles_missing_previous_baselines(self):
        with tempfile.TemporaryDirectory() as current_dir:
            current = _baseline('Enterprise_Sales.twb', output_format='fabric')
            with open(os.path.join(current_dir, 'enterprise.json'), 'w', encoding='utf-8') as handle:
                json.dump(current, handle)

            report = compare_perf_baselines.compare_baselines(
                current_dir=current_dir,
                previous_dir=None,
                threshold_percent=10.0,
            )

            fixture = report['fixtures']['Enterprise_Sales.twb:fabric']
            self.assertIsNone(fixture['previous'])
            self.assertEqual(report['summary']['status'], 'ok')
            self.assertEqual(report['summary']['fixtures_with_previous'], 0)

    def test_main_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as root:
            current_dir = os.path.join(root, 'current')
            previous_dir = os.path.join(root, 'previous')
            os.makedirs(current_dir)
            os.makedirs(previous_dir)

            with open(os.path.join(current_dir, 'enterprise.json'), 'w', encoding='utf-8') as handle:
                json.dump(_baseline('Enterprise_Sales.twb', total=1.0), handle)
            with open(os.path.join(previous_dir, 'enterprise.json'), 'w', encoding='utf-8') as handle:
                json.dump(_baseline('Enterprise_Sales.twb', total=1.0), handle)

            output_json = os.path.join(root, 'reports', 'trend.json')
            output_md = os.path.join(root, 'reports', 'trend.md')

            exit_code = compare_perf_baselines.main([
                '--current-dir', current_dir,
                '--previous-dir', previous_dir,
                '--threshold-percent', '10',
                '--output-json', output_json,
                '--output-md', output_md,
            ])

            self.assertEqual(exit_code, 0)
            self.assertTrue(os.path.isfile(output_json))
            self.assertTrue(os.path.isfile(output_md))

            with open(output_json, encoding='utf-8') as handle:
                saved = json.load(handle)
            with open(output_md, encoding='utf-8') as handle:
                md = handle.read()

            self.assertIn('fixtures', saved)
            self.assertIn('# Performance Trend Diff', md)


if __name__ == '__main__':
    unittest.main()
