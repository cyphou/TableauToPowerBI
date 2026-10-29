import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from powerbi_import.calc_column_plan import plan_calculated_columns
from powerbi_import.lakehouse_generator import LakehouseGenerator
from powerbi_import.notebook_generator import NotebookGenerator
from powerbi_import.pyspark_gate import validate_statement


@pytest.fixture
def sample_plan_data():
    return {
        'datasources': [
            {
                'name': 'Orders DS',
                'tables': [
                    {
                        'name': 'Orders',
                        'columns': [
                            {'name': 'OrderID', 'datatype': 'integer'},
                            {'name': 'Amount', 'datatype': 'real'},
                            {'name': 'Sales', 'datatype': 'real'},
                            {'name': 'Region', 'datatype': 'string'},
                        ],
                    },
                    {
                        'name': 'Customers',
                        'columns': [
                            {'name': 'CustomerID', 'datatype': 'integer'},
                            {'name': 'Name', 'datatype': 'string'},
                        ],
                    },
                ],
            },
            {
                'name': 'Other DS',
                'tables': [
                    {
                        'name': 'Extra',
                        'columns': [
                            {'name': 'Amount', 'datatype': 'real'},
                        ],
                    },
                ],
            },
        ],
        'parameters': [
            {'name': '[Threshold]', 'caption': 'Threshold', 'datatype': 'integer', 'value': '10'},
            {'name': '[Label]', 'caption': 'Label', 'datatype': 'string', 'value': '"abc"'},
        ],
        'calculations': [
            {'name': '[A]', 'caption': 'A', 'formula': '[Amount] + 1', 'datatype': 'real'},
            {'name': '[B]', 'caption': 'B', 'formula': '[A] * 2', 'datatype': 'real'},
            {'name': '[Profit Ratio]', 'caption': 'Profit Ratio', 'formula': '[Amount] / [Sales]', 'datatype': 'real'},
            {'name': '[Total]', 'caption': 'Total', 'formula': 'SUM([Sales])', 'datatype': 'real', 'role': 'measure'},
            {'name': '[Badge]', 'caption': 'Badge', 'formula': '[Threshold] + [Amount]', 'datatype': 'real'},
            {'name': '[Broken]', 'caption': 'Broken', 'formula': '[Missing] + 1', 'datatype': 'real'},
            {'name': '[Cycle 1]', 'caption': 'Cycle 1', 'formula': '[Cycle 2] + 1', 'datatype': 'real'},
            {'name': '[Cycle 2]', 'caption': 'Cycle 2', 'formula': '[Cycle 1] + 1', 'datatype': 'real'},
        ],
    }


def test_plan_calculated_columns_handles_non_dict_and_empty_input():
    assert not plan_calculated_columns({}).columns
    assert not plan_calculated_columns([]).columns
    assert not plan_calculated_columns([1, 2, 3]).columns


def test_plan_calculated_columns_orders_dependencies_and_parameters(sample_plan_data):
    plan = plan_calculated_columns(sample_plan_data)
    ordered = [col.caption for col in plan.columns if col.materialised]
    assert 'A' in ordered and 'B' in ordered
    assert ordered.index('A') < ordered.index('B')
    assert any(col.caption == 'Profit Ratio' and col.delta_table == 'orders' for col in plan.columns)
    assert any(col.caption == 'Badge' and col.statement is not None for col in plan.columns)


def test_plan_calculated_columns_keeps_aggregates_and_marks_pending_items(sample_plan_data):
    plan = plan_calculated_columns(sample_plan_data)
    reasons = {measure.caption: measure.reason for measure in plan.measures}
    assert 'Total' in reasons
    assert 'aggregate expression (kept as a DAX measure)' in reasons['Total']
    pending = {col.caption: col.reason for col in plan.columns if not col.materialised}
    assert 'Broken' in pending
    assert 'field [Missing]' in pending['Broken']
    assert 'Cycle 1' in pending
    assert 'circular' in pending['Cycle 1']


def test_plan_calculated_columns_scope_and_multi_table_resolution(sample_plan_data):
    data = {
        'datasources': [
            {'name': 'Orders DS', 'tables': [{'name': 'Orders', 'columns': [{'name': 'Amount', 'datatype': 'real'}]}]},
            {'name': 'Other DS', 'tables': [{'name': 'Extra', 'columns': [{'name': 'Amount', 'datatype': 'real'}]}]},
        ],
        'calculations': [
            {'name': '[Scoped]', 'caption': 'Scoped', 'formula': '[Amount] + 1', 'datatype': 'real', 'datasource_name': 'Other DS'},
            {'name': '[Cross]', 'caption': 'Cross', 'formula': '[Amount] + [CustomerID]', 'datatype': 'real'},
        ],
    }
    plan = plan_calculated_columns(data)
    pending = {col.caption: col.reason for col in plan.columns if not col.materialised}
    assert 'Cross' in pending
    assert 'field [CustomerID]' in pending['Cross']
    assert 'Scoped' in pending or any(col.caption == 'Scoped' for col in plan.columns)


def test_lakehouse_and_notebook_integration(tmp_path, sample_plan_data):
    project_dir = tmp_path / 'fabric_out'
    project_dir.mkdir()
    lakehouse = LakehouseGenerator(str(project_dir), 'TestProject')
    lakehouse_stats = lakehouse.generate(sample_plan_data)
    assert lakehouse_stats['calc_columns'] >= 3
    meta = json.loads((project_dir / 'TestProject.Lakehouse' / 'table_metadata.json').read_text(encoding='utf-8'))
    assert any('pending_calc_columns' in table for table in meta['tables'])

    notebook = NotebookGenerator(str(project_dir), 'TestProject')
    notebook_stats = notebook.generate(sample_plan_data)
    assert notebook_stats['cells'] > 0
    transform_path = project_dir / 'TestProject.Notebook' / 'transformations.ipynb'
    assert transform_path.exists()
    payload = json.loads(transform_path.read_text(encoding='utf-8'))
    assert 'cells' in payload and payload['cells']
    for cell in payload['cells']:
        if cell.get('cell_type') == 'code':
            source = ''.join(cell.get('source', []))
            if 'withColumn' in source:
                assert 'df = df.withColumn(' in source
                reason = validate_statement(source, ['Amount', 'Sales', 'Region', 'A', 'a', 'b'])
                assert reason is None, reason


def test_plan_error_path_is_handled_gracefully(monkeypatch, sample_plan_data):
    import powerbi_import.calc_column_plan as plan_mod

    def boom(*args, **kwargs):
        raise RuntimeError('boom')

    monkeypatch.setattr(plan_mod, 'convert_to_pyspark', boom)
    plan = plan_mod.plan_calculated_columns(sample_plan_data)
    assert plan.columns
    assert any(col.reason and 'internal planner error' in col.reason for col in plan.columns if not col.materialised)
