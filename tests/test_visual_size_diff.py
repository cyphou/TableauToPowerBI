"""Tests for powerbi_import/visual_size_diff.py.

Verifies the Tableau-vs-PowerBI visual size comparison used to audit
migrated reports (see scripts/compare_visual_sizes.py).
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.visual_size_diff import (
    compare_dashboard_visual_sizes,
    compare_report_visual_sizes,
)


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f)


def _write_page(report_dir, page_folder, display_name, width, height, visuals):
    page_dir = os.path.join(report_dir, 'definition', 'pages', page_folder)
    _write_json(os.path.join(page_dir, 'page.json'), {
        'displayName': display_name, 'width': width, 'height': height,
    })
    for vid, (w, h, vtype) in visuals.items():
        _write_json(os.path.join(page_dir, 'visuals', vid, 'visual.json'), {
            'name': vid,
            'position': {'x': 0, 'y': 0, 'width': w, 'height': h},
            'visual': {'visualType': vtype},
        })


class TestCompareDashboardVisualSizes(unittest.TestCase):
    def test_matches_scaled_worksheet_size(self):
        dashboard = {
            'name': 'Page 1',
            'size': {'width': 1280, 'height': 720},
            'objects': [
                {'type': 'worksheetReference', 'worksheetName': 'Sales',
                 'position': {'x': 0, 'y': 0, 'w': 1280, 'h': 720}},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = os.path.join(tmp, 'Report.Report')
            _write_page(report_dir, 'ReportSection', 'Page 1', 1280, 720,
                       {'v1': (1280, 720, 'clusteredBarChart')})
            result = compare_dashboard_visual_sizes(dashboard, report_dir)

        self.assertEqual(len(result['matched']), 1)
        self.assertEqual(result['mismatched'], [])
        self.assertTrue(result['canvas']['matched'])

    def test_flags_size_mismatch(self):
        dashboard = {
            'name': 'Page 1',
            'size': {'width': 1280, 'height': 720},
            'objects': [
                {'type': 'worksheetReference', 'worksheetName': 'Sales',
                 'position': {'x': 0, 'y': 0, 'w': 1280, 'h': 720}},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = os.path.join(tmp, 'Report.Report')
            # Actual visual is incorrectly shrunk (e.g. wrong canvas clamp).
            _write_page(report_dir, 'ReportSection', 'Page 1', 1280, 720,
                       {'v1': (900, 500, 'clusteredBarChart')})
            result = compare_dashboard_visual_sizes(dashboard, report_dir)

        self.assertEqual(result['matched'], [])
        self.assertEqual(len(result['mismatched']), 1)
        self.assertEqual(result['mismatched'][0]['reason'], 'no matching actual size')

    def test_page_not_found(self):
        dashboard = {
            'name': 'Missing Page',
            'size': {'width': 1280, 'height': 720},
            'objects': [
                {'type': 'text', 'name': 'Title',
                 'position': {'x': 0, 'y': 0, 'w': 400, 'h': 60}},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = os.path.join(tmp, 'Report.Report')
            result = compare_dashboard_visual_sizes(dashboard, report_dir)

        self.assertFalse(result['page_found'])
        self.assertEqual(len(result['mismatched']), 1)
        self.assertEqual(result['mismatched'][0]['reason'], 'page not found')

    def test_grid_layout_map_used_when_zone_hierarchy_present(self):
        """A 2x2 zone_hierarchy grid must yield evenly split expected sizes."""
        dashboard = {
            'name': 'Grid Page',
            'size': {'width': 1400, 'height': 900},
            'objects': [
                {'type': 'worksheetReference', 'worksheetName': 'A',
                 'position': {'x': 0, 'y': 0, 'w': 50000, 'h': 50000}},
                {'type': 'worksheetReference', 'worksheetName': 'B',
                 'position': {'x': 50000, 'y': 0, 'w': 50000, 'h': 50000}},
            ],
            'zone_hierarchy': {
                'id': '1', 'name': '', 'zone_type': 'layout-basic',
                'orientation': '', 'position': {'x': 0, 'y': 0, 'w': 100000, 'h': 100000},
                'is_floating': False, 'children': [
                    {'id': '2', 'name': 'A', 'zone_type': 'worksheet', 'orientation': '',
                     'position': {'x': 0, 'y': 0, 'w': 50000, 'h': 50000},
                     'is_floating': False, 'children': []},
                    {'id': '3', 'name': 'B', 'zone_type': 'worksheet', 'orientation': '',
                     'position': {'x': 50000, 'y': 0, 'w': 50000, 'h': 50000},
                     'is_floating': False, 'children': []},
                ],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            report_dir = os.path.join(tmp, 'Report.Report')
            _write_page(report_dir, 'ReportSection', 'Grid Page', 1400, 900, {
                'v1': (700, 900, 'clusteredBarChart'),
                'v2': (700, 900, 'lineChart'),
            })
            result = compare_dashboard_visual_sizes(dashboard, report_dir)

        self.assertEqual(len(result['matched']), 2)
        self.assertEqual(result['mismatched'], [])


class TestCompareReportVisualSizes(unittest.TestCase):
    def test_aggregates_summary_across_pages(self):
        extracted = {
            'dashboards': [
                {
                    'name': 'Page 1',
                    'size': {'width': 1280, 'height': 720},
                    'objects': [
                        {'type': 'worksheetReference', 'worksheetName': 'Sales',
                         'position': {'x': 0, 'y': 0, 'w': 1280, 'h': 720}},
                    ],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = tmp
            report_dir = os.path.join(project_dir, 'MyReport.Report')
            _write_page(report_dir, 'ReportSection', 'Page 1', 1280, 720,
                       {'v1': (1280, 720, 'clusteredBarChart')})
            result = compare_report_visual_sizes(extracted, project_dir, 'MyReport')

        self.assertEqual(result['summary']['expected_visuals'], 1)
        self.assertEqual(result['summary']['matched'], 1)
        self.assertEqual(result['summary']['mismatched'], 0)
        self.assertEqual(result['summary']['match_rate_percent'], 100.0)

    def test_empty_dashboards_gives_full_match_rate(self):
        result = compare_report_visual_sizes({'dashboards': []}, '/no/such/dir', 'Empty')
        self.assertEqual(result['summary']['expected_visuals'], 0)
        self.assertEqual(result['summary']['match_rate_percent'], 100.0)


if __name__ == '__main__':
    unittest.main()
