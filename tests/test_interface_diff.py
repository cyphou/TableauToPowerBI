"""Tests for powerbi_import/interface_diff.py.

Verifies the Tableau-vs-PowerBI interactivity fidelity comparison (filters,
action buttons, parameters, RLS roles).
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.interface_diff import (
    _expected_filter_count,
    compare_report_interface,
)


def _write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def _write_json(path, data):
    import json
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f)


def _dashboard(worksheet_names):
    return {'dashboards': [{
        'objects': [{'type': 'worksheetReference', 'worksheetName': n}
                   for n in worksheet_names],
    }]}


class TestExpectedFilterCount(unittest.TestCase):
    def test_counts_real_row_filters(self):
        extracted = {
            **_dashboard(['Sales']),
            'worksheets': [{'name': 'Sales', 'filters': [
                {'field': 'Region', 'datasource': 'federated.1', 'type': 'categorical',
                 'values': ['West']},
            ]}],
        }
        self.assertEqual(_expected_filter_count(extracted), 1)

    def test_excludes_off_dashboard_worksheet(self):
        extracted = {
            **_dashboard(['Sales']),
            'worksheets': [
                {'name': 'Sales', 'filters': []},
                {'name': 'Hidden', 'filters': [
                    {'field': 'Region', 'datasource': 'federated.1', 'type': 'categorical',
                     'values': ['West']},
                ]},
            ],
        }
        self.assertEqual(_expected_filter_count(extracted), 0)

    def test_excludes_parameter_driven_filter(self):
        extracted = {
            **_dashboard(['Sales']),
            'worksheets': [{'name': 'Sales', 'filters': [
                {'field': 'Business Unit Filter', 'datasource': 'Parameters', 'type': 'all'},
            ]}],
        }
        self.assertEqual(_expected_filter_count(extracted), 0)

    def test_excludes_dashboard_action_placeholder(self):
        extracted = {
            **_dashboard(['Sales']),
            'worksheets': [{'name': 'Sales', 'filters': [
                {'field': 'Action (Region)', 'datasource': 'federated.1', 'type': 'all'},
            ]}],
        }
        self.assertEqual(_expected_filter_count(extracted), 0)

    def test_excludes_measure_names_filter(self):
        extracted = {
            **_dashboard(['Sales']),
            'worksheets': [{'name': 'Sales', 'filters': [
                {'field': ':Measure Names', 'datasource': 'federated.1', 'type': 'categorical',
                 'values': ['"a"']},
            ]}],
        }
        self.assertEqual(_expected_filter_count(extracted), 0)

    def test_excludes_inert_all_values_placeholder(self):
        extracted = {
            **_dashboard(['Sales']),
            'worksheets': [{'name': 'Sales', 'filters': [
                {'field': 'Category', 'datasource': 'federated.1', 'type': 'all',
                 'values': [], 'min': None, 'max': None},
            ]}],
        }
        self.assertEqual(_expected_filter_count(extracted), 0)

    def test_restrictive_all_type_filter_still_counts(self):
        extracted = {
            **_dashboard(['Sales']),
            'worksheets': [{'name': 'Sales', 'filters': [
                {'field': 'Order Date', 'datasource': 'federated.1', 'type': 'all',
                 'values': [], 'min': '2020-01-01', 'max': None},
            ]}],
        }
        self.assertEqual(_expected_filter_count(extracted), 1)


class TestCompareReportInterface(unittest.TestCase):
    def test_covered_when_filter_config_present(self):
        extracted = {
            **_dashboard(['Sales']),
            'worksheets': [{'name': 'Sales', 'filters': [
                {'field': 'Region', 'datasource': 'federated.1', 'type': 'categorical',
                 'values': ['West']},
            ]}],
            'actions': [], 'parameters': [], 'user_filters': [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = os.path.join(tmp, 'Report')
            report_dir = os.path.join(project_dir, 'Report.Report')
            _write_json(os.path.join(report_dir, 'definition', 'report.json'), {
                'filterConfig': {'filters': [{'name': 'f1'}]},
            })
            result = compare_report_interface(extracted, project_dir, 'Report')

        self.assertTrue(result['filters']['covered'])
        self.assertEqual(result['filters']['expected'], 1)

    def test_slicer_counts_as_filter_coverage(self):
        extracted = {
            **_dashboard(['Sales']),
            'worksheets': [{'name': 'Sales', 'filters': [
                {'field': 'Region', 'datasource': 'federated.1', 'type': 'categorical',
                 'values': ['West']},
            ]}],
            'actions': [], 'parameters': [], 'user_filters': [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = os.path.join(tmp, 'Report')
            report_dir = os.path.join(project_dir, 'Report.Report')
            _write_json(os.path.join(report_dir, 'definition', 'report.json'), {})
            _write_json(os.path.join(
                report_dir, 'definition', 'pages', 'p1', 'visuals', 'v1', 'visual.json'), {
                'visual': {'visualType': 'slicer'},
            })
            result = compare_report_interface(extracted, project_dir, 'Report')

        self.assertEqual(result['filters']['generated_slicers'], 1)
        self.assertTrue(result['filters']['covered'])

    def test_parameter_covered_via_measure_fallback(self):
        extracted = {
            **_dashboard([]),
            'worksheets': [],
            'actions': [],
            'parameters': [{'caption': 'Top N', 'name': '[Parameters].[Top N]'}],
            'user_filters': [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = os.path.join(tmp, 'Report')
            report_dir = os.path.join(project_dir, 'Report.Report')
            semantic_model_dir = os.path.join(project_dir, 'Report.SemanticModel')
            _write_json(os.path.join(report_dir, 'definition', 'report.json'), {})
            tables_dir = os.path.join(semantic_model_dir, 'definition', 'tables')
            os.makedirs(tables_dir, exist_ok=True)
            _write_text(os.path.join(tables_dir, 'Orders.tmdl'),
                       "table Orders\n\tmeasure 'Top N' = 10\n")
            result = compare_report_interface(extracted, project_dir, 'Report')

        self.assertTrue(result['parameters']['covered'])
        self.assertEqual(result['parameters']['generated_coverage'], 1)

    def test_parameter_gap_when_no_table_or_measure(self):
        extracted = {
            **_dashboard([]),
            'worksheets': [],
            'actions': [],
            'parameters': [{'caption': 'Date Range Start', 'name': '[Parameters].[Date Range Start]'}],
            'user_filters': [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = os.path.join(tmp, 'Report')
            report_dir = os.path.join(project_dir, 'Report.Report')
            semantic_model_dir = os.path.join(project_dir, 'Report.SemanticModel')
            _write_json(os.path.join(report_dir, 'definition', 'report.json'), {})
            tables_dir = os.path.join(semantic_model_dir, 'definition', 'tables')
            os.makedirs(tables_dir, exist_ok=True)
            _write_text(os.path.join(tables_dir, 'Orders.tmdl'), "table Orders\n")
            result = compare_report_interface(extracted, project_dir, 'Report')

        self.assertFalse(result['parameters']['covered'])
        self.assertEqual(result['parameters']['generated_coverage'], 0)

    def test_rls_roles_counted(self):
        extracted = {
            **_dashboard([]),
            'worksheets': [], 'actions': [], 'parameters': [],
            'user_filters': [{'field': 'Region'}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = os.path.join(tmp, 'Report')
            report_dir = os.path.join(project_dir, 'Report.Report')
            semantic_model_dir = os.path.join(project_dir, 'Report.SemanticModel')
            _write_json(os.path.join(report_dir, 'definition', 'report.json'), {})
            _write_text(os.path.join(semantic_model_dir, 'definition', 'roles.tmdl'),
                       "role RegionRLS\n\tmodelPermission: read\n")
            result = compare_report_interface(extracted, project_dir, 'Report')

        self.assertTrue(result['rls']['covered'])
        self.assertEqual(result['rls']['generated_roles'], 1)

    def test_action_button_covered(self):
        extracted = {
            **_dashboard([]),
            'worksheets': [],
            'actions': [{'type': 'url'}],
            'parameters': [], 'user_filters': [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = os.path.join(tmp, 'Report')
            report_dir = os.path.join(project_dir, 'Report.Report')
            _write_json(os.path.join(report_dir, 'definition', 'report.json'), {})
            _write_json(os.path.join(
                report_dir, 'definition', 'pages', 'p1', 'visuals', 'v1', 'visual.json'), {
                'visual': {'visualType': 'actionButton'},
            })
            result = compare_report_interface(extracted, project_dir, 'Report')

        self.assertTrue(result['actions']['covered'])
        self.assertEqual(result['actions']['generated_buttons'], 1)


if __name__ == '__main__':
    unittest.main()
