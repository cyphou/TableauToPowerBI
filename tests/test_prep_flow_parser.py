"""
Unit tests for Tableau Prep flow parser (prep_flow_parser.py).

Tests flow reading, node type detection, topological sort, step conversion,
expression conversion, join/union/pivot/aggregate parsing, and the merge logic.
"""

import io
import json
import os
import re
import sys
import tempfile
import unittest
import zipfile
from copy import deepcopy

from tests.prep_flow_fixtures import (
    clean_chain,
    container,
    edge,
    join_flow,
    operation,
    output,
    source,
    write_flow,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tableau_export'))

from prep_flow_parser import (
    _PREP_AGG_MAP,
    _PREP_CONNECTION_MAP,
    _PREP_JOIN_MAP,
    _PREP_TYPE_MAP,
    _clean_m_table_ref,
    _collect_prep_datasources,
    _convert_action_to_m_step,
    _convert_prep_expression_to_m,
    _get_node_type,
    _parse_aggregate_node,
    _parse_clean_actions,
    _parse_input_node,
    _parse_join_node,
    _parse_pivot_node,
    _parse_union_node,
    _process_prep_node,
    _process_transform_node,
    _topological_sort,
    get_prep_node_kind,
    merge_prep_with_workbook,
    normalize_prep_flow,
    parse_prep_flow,
    read_prep_flow,
)

# ═══════════════════════════════════════════════════════════════════
# Helper — build minimal flow JSON
# ═══════════════════════════════════════════════════════════════════

def _make_flow(nodes, connections=None):
    """Create a minimal Prep flow dict."""
    return {
        'nodes': nodes,
        'connections': connections or {},
    }


def _input_node(name='Table1', conn_id='conn1', fields=None, next_ids=None):
    """Create an input node."""
    node = {
        'baseType': 'input',
        'nodeType': '.v1.LoadCsv',
        'name': name,
        'connectionId': conn_id,
        'connectionAttributes': {'class': 'csv', 'filename': f'{name}.csv'},
        'fields': fields or [
            {'name': 'ID', 'type': 'integer'},
            {'name': 'Name', 'type': 'string'},
            {'name': 'Amount', 'type': 'real'},
        ],
        'nextNodes': [{'nextNodeId': nid} for nid in (next_ids or [])],
    }
    return node


def _clean_node(name='Clean', actions=None, next_ids=None):
    """Create a SuperTransform (clean) node."""
    return {
        'baseType': 'transform',
        'nodeType': '.v2018_3_3.SuperTransform',
        'name': name,
        'beforeActionGroup': {'actions': actions or []},
        'nextNodes': [{'nextNodeId': nid} for nid in (next_ids or [])],
    }


def _output_node(name='Output'):
    """Create an output node."""
    return {
        'baseType': 'output',
        'nodeType': '.v1.PublishExtract',
        'name': name,
        'nextNodes': [],
    }


# ═══════════════════════════════════════════════════════════════════
# Flow reading
# ═══════════════════════════════════════════════════════════════════

class TestReadPrepFlow(unittest.TestCase):
    """Test read_prep_flow for .tfl files."""

    def test_read_tfl_file(self):
        flow_data = _make_flow({'n1': _input_node()})
        with tempfile.NamedTemporaryFile(mode='w', suffix='.tfl', delete=False,
                                         encoding='utf-8') as f:
            json.dump(flow_data, f)
            path = f.name
        try:
            result = read_prep_flow(path)
            self.assertIn('nodes', result)
            self.assertIn('n1', result['nodes'])
        finally:
            os.unlink(path)

    def test_unsupported_extension_raises(self):
        with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as f:
            path = f.name
        try:
            with self.assertRaises(ValueError):
                read_prep_flow(path)
        finally:
            os.unlink(path)

    def test_read_tflx_zip(self):
        """read_prep_flow handles .tflx ZIP archives with a .tfl entry."""
        flow_data = _make_flow({'n1': _input_node()})
        with tempfile.NamedTemporaryFile(suffix='.tflx', delete=False) as tmp:
            path = tmp.name
        try:
            with zipfile.ZipFile(path, 'w') as z:
                z.writestr('flow.tfl', json.dumps(flow_data))
            result = read_prep_flow(path)
            self.assertIn('nodes', result)
            self.assertIn('n1', result['nodes'])
        finally:
            os.unlink(path)

    def test_read_tfl_zip_autodetect(self):
        """read_prep_flow auto-detects ZIP archives with a .tfl extension."""
        flow_data = _make_flow({'n1': _input_node()})
        with tempfile.NamedTemporaryFile(suffix='.tfl', delete=False) as tmp:
            path = tmp.name
        try:
            with zipfile.ZipFile(path, 'w') as z:
                z.writestr('flow', json.dumps(flow_data))
            result = read_prep_flow(path)
            self.assertIn('nodes', result)
        finally:
            os.unlink(path)

    def test_read_tflx_flow_entry_fallback(self):
        """read_prep_flow finds a 'flow' entry when no .tfl entry exists."""
        flow_data = _make_flow({'n2': _input_node('T2')})
        with tempfile.NamedTemporaryFile(suffix='.tflx', delete=False) as tmp:
            path = tmp.name
        try:
            with zipfile.ZipFile(path, 'w') as z:
                z.writestr('flow', json.dumps(flow_data))
                z.writestr('maestroMetadata', '{}')
            result = read_prep_flow(path)
            self.assertIn('n2', result['nodes'])
        finally:
            os.unlink(path)


# ═══════════════════════════════════════════════════════════════════
# Node type detection
# ═══════════════════════════════════════════════════════════════════

class TestGetNodeType(unittest.TestCase):
    """Test _get_node_type extraction."""

    def test_versioned_node_type(self):
        node = {'nodeType': '.v2018_3_3.SuperTransform'}
        self.assertEqual(_get_node_type(node), 'SuperTransform')

    def test_simple_node_type(self):
        node = {'nodeType': '.v1.LoadCsv'}
        self.assertEqual(_get_node_type(node), 'LoadCsv')

    def test_empty_node_type(self):
        node = {'nodeType': ''}
        self.assertEqual(_get_node_type(node), '')

    def test_none_node_is_safe(self):
        self.assertEqual(_get_node_type(None), '')


# ═══════════════════════════════════════════════════════════════════
# Topological sort
# ═══════════════════════════════════════════════════════════════════

class TestTopologicalSort(unittest.TestCase):
    """Test _topological_sort for DAG traversal."""

    def test_linear_chain(self):
        nodes = {
            'a': {'nextNodes': [{'nextNodeId': 'b'}]},
            'b': {'nextNodes': [{'nextNodeId': 'c'}]},
            'c': {'nextNodes': []},
        }
        result = _topological_sort(nodes)
        self.assertEqual(result, ['a', 'b', 'c'])

    def test_diamond_graph(self):
        nodes = {
            'a': {'nextNodes': [{'nextNodeId': 'b'}, {'nextNodeId': 'c'}]},
            'b': {'nextNodes': [{'nextNodeId': 'd'}]},
            'c': {'nextNodes': [{'nextNodeId': 'd'}]},
            'd': {'nextNodes': []},
        }
        result = _topological_sort(nodes)
        self.assertEqual(result[0], 'a')
        self.assertEqual(result[-1], 'd')
        self.assertEqual(len(result), 4)

    def test_single_node(self):
        nodes = {'a': {'nextNodes': []}}
        result = _topological_sort(nodes)
        self.assertEqual(result, ['a'])

    def test_empty_graph(self):
        result = _topological_sort({})
        self.assertEqual(result, [])

    def test_malformed_nodes_are_ignored(self):
        nodes = {
            'a': None,
            'b': {'nextNodes': [{'nextNodeId': 'c'}]},
            'c': {'nextNodes': []},
        }
        result = _topological_sort(nodes)
        self.assertIn('b', result)


# ═══════════════════════════════════════════════════════════════════
# Expression conversion
# ═══════════════════════════════════════════════════════════════════

class TestConvertPrepExpression(unittest.TestCase):
    """Test _convert_prep_expression_to_m."""

    def test_if_then_else(self):
        result = _convert_prep_expression_to_m('IF [X] > 10 THEN "High" ELSE "Low" END')
        self.assertIn('if', result)
        self.assertIn('then', result)
        self.assertIn('else', result)
        self.assertNotIn('END', result)

    def test_elseif_conversion(self):
        result = _convert_prep_expression_to_m('IF [X] > 10 THEN "A" ELSEIF [X] > 5 THEN "B" ELSE "C" END')
        self.assertIn('else if', result)

    def test_logical_operators(self):
        result = _convert_prep_expression_to_m('[A] > 1 AND [B] < 2 OR NOT [C]')
        self.assertIn('and', result)
        self.assertIn('or', result)
        self.assertIn('not', result)

    def test_comparison_operators(self):
        result = _convert_prep_expression_to_m('[A] != [B] AND [C] == [D]')
        self.assertIn('<>', result)
        self.assertNotIn('!=', result)
        # == becomes = in M
        self.assertIn('=', result)

    def test_string_functions(self):
        result = _convert_prep_expression_to_m('CONTAINS([Name], "test")')
        self.assertIn('Text.Contains', result)

    def test_len_function(self):
        result = _convert_prep_expression_to_m('LEN([Name])')
        self.assertIn('Text.Length', result)

    def test_upper_lower(self):
        self.assertIn('Text.Upper', _convert_prep_expression_to_m('UPPER([X])'))
        self.assertIn('Text.Lower', _convert_prep_expression_to_m('LOWER([X])'))

    def test_empty_returns_empty_string_literal(self):
        result = _convert_prep_expression_to_m('')
        self.assertEqual(result, '""')

    def test_none_returns_empty_string_literal(self):
        result = _convert_prep_expression_to_m(None)
        self.assertEqual(result, '""')

    def test_isnull_conversion(self):
        result = _convert_prep_expression_to_m('ISNULL([X])')
        self.assertIn('null', result)


# ═══════════════════════════════════════════════════════════════════
# Clean action conversion
# ═══════════════════════════════════════════════════════════════════

class TestCleanActions(unittest.TestCase):
    """Test _parse_clean_actions and _convert_action_to_m_step."""

    def test_rename_column(self):
        node = _clean_node(actions=[
            {'actionType': '.v1.RenameColumn', 'columnName': 'OldName', 'newColumnName': 'NewName'},
        ])
        steps = _parse_clean_actions(node)
        self.assertEqual(len(steps), 1)
        step_name, step_expr = steps[0]
        self.assertIn('RenameColumns', step_expr)
        self.assertIn('OldName', step_expr)
        self.assertIn('NewName', step_expr)

    def test_batched_renames(self):
        node = _clean_node(actions=[
            {'actionType': '.v1.RenameColumn', 'columnName': 'A', 'newColumnName': 'X'},
            {'actionType': '.v1.RenameColumn', 'columnName': 'B', 'newColumnName': 'Y'},
        ])
        steps = _parse_clean_actions(node)
        # Both renames should be batched into one step
        self.assertEqual(len(steps), 1)
        _, expr = steps[0]
        self.assertIn('A', expr)
        self.assertIn('X', expr)
        self.assertIn('B', expr)
        self.assertIn('Y', expr)

    def test_remove_column(self):
        node = _clean_node(actions=[
            {'actionType': '.v1.RemoveColumn', 'columnName': 'DropMe'},
        ])
        steps = _parse_clean_actions(node)
        self.assertEqual(len(steps), 1)
        _, expr = steps[0]
        self.assertIn('RemoveColumns', expr)
        self.assertIn('DropMe', expr)

    def test_duplicate_column(self):
        node = _clean_node(actions=[
            {'actionType': '.v1.DuplicateColumn', 'columnName': 'Col', 'newColumnName': 'Col_copy'},
        ])
        steps = _parse_clean_actions(node)
        self.assertEqual(len(steps), 1)
        _, expr = steps[0]
        self.assertIn('DuplicateColumn', expr)

    def test_change_column_type(self):
        node = _clean_node(actions=[
            {'actionType': '.v1.ChangeColumnType', 'columnName': 'Amount', 'newType': 'integer'},
        ])
        steps = _parse_clean_actions(node)
        self.assertEqual(len(steps), 1)
        _, expr = steps[0]
        self.assertIn('TransformColumnTypes', expr)
        self.assertIn('number', expr)

    def test_filter_values_keep(self):
        node = _clean_node(actions=[
            {'actionType': '.v1.FilterValues', 'columnName': 'Status',
             'values': ['Active', 'Pending'], 'filterType': 'keep'},
        ])
        steps = _parse_clean_actions(node)
        self.assertEqual(len(steps), 1)

    def test_filter_values_remove(self):
        node = _clean_node(actions=[
            {'actionType': '.v1.FilterValues', 'columnName': 'Status',
             'values': ['Deleted'], 'filterType': 'remove'},
        ])
        steps = _parse_clean_actions(node)
        self.assertEqual(len(steps), 1)

    def test_replace_values(self):
        node = _clean_node(actions=[
            {'actionType': '.v1.ReplaceValues', 'columnName': 'Region',
             'oldValue': 'NA', 'newValue': 'North America'},
        ])
        steps = _parse_clean_actions(node)
        self.assertEqual(len(steps), 1)
        _, expr = steps[0]
        self.assertIn('ReplaceValue', expr)

    def test_split_column(self):
        node = _clean_node(actions=[
            {'actionType': '.v1.SplitColumn', 'columnName': 'FullName', 'delimiter': ' '},
        ])
        steps = _parse_clean_actions(node)
        self.assertEqual(len(steps), 1)
        _, expr = steps[0]
        self.assertIn('SplitColumn', expr)

    def test_add_column(self):
        node = _clean_node(actions=[
            {'actionType': '.v1.AddColumn', 'columnName': 'Total',
             'expression': '[Amount] * 1.1'},
        ])
        steps = _parse_clean_actions(node)
        self.assertEqual(len(steps), 1)
        _, expr = steps[0]
        self.assertIn('AddColumn', expr)

    def test_clean_operation_trim(self):
        node = _clean_node(actions=[
            {'actionType': '.v1.CleanOperation', 'columnName': 'Name', 'operation': 'trim'},
        ])
        steps = _parse_clean_actions(node)
        self.assertEqual(len(steps), 1)
        _, expr = steps[0]
        self.assertIn('Trim', expr)

    def test_fill_down(self):
        node = _clean_node(actions=[
            {'actionType': '.v1.FillValues', 'columnName': 'Region', 'direction': 'down'},
        ])
        steps = _parse_clean_actions(node)
        self.assertEqual(len(steps), 1)
        _, expr = steps[0]
        self.assertIn('FillDown', expr)

    def test_fill_up(self):
        node = _clean_node(actions=[
            {'actionType': '.v1.FillValues', 'columnName': 'Region', 'direction': 'up'},
        ])
        steps = _parse_clean_actions(node)
        self.assertEqual(len(steps), 1)
        _, expr = steps[0]
        self.assertIn('FillUp', expr)

    def test_group_replace(self):
        node = _clean_node(actions=[
            {'actionType': '.v1.GroupReplace', 'columnName': 'Category',
             'groupings': [
                 {'from': 'Cat A', 'to': 'Category A'},
                 {'from': 'Cat B', 'to': 'Category B'},
             ]},
        ])
        steps = _parse_clean_actions(node)
        self.assertEqual(len(steps), 2)  # One replace step per grouping

    def test_unknown_action_raises(self):
        node = _clean_node(actions=[
            {'actionType': '.v1.FutureAction', 'columnName': 'X'},
        ])
        with self.assertRaisesRegex(ValueError, 'unsupported.*FutureAction'):
            _parse_clean_actions(node)

    def test_rename_flush_before_other_action(self):
        """Renames should flush before a non-rename action."""
        node = _clean_node(actions=[
            {'actionType': '.v1.RenameColumn', 'columnName': 'A', 'newColumnName': 'X'},
            {'actionType': '.v1.RemoveColumn', 'columnName': 'B'},
        ])
        steps = _parse_clean_actions(node)
        self.assertEqual(len(steps), 2)
        # First step should be rename, second should be remove
        self.assertIn('RenameColumns', steps[0][1])
        self.assertIn('RemoveColumns', steps[1][1])


# ═══════════════════════════════════════════════════════════════════
# Aggregate step
# ═══════════════════════════════════════════════════════════════════

class TestAggregateNode(unittest.TestCase):
    """Test _parse_aggregate_node."""

    def test_basic_aggregation(self):
        node = {
            'groupByFields': [{'name': 'Region'}],
            'aggregateFields': [
                {'name': 'Sales', 'aggregation': 'SUM', 'newColumnName': 'Total Sales'},
            ],
        }
        result = _parse_aggregate_node(node)
        self.assertIsNotNone(result)
        assert result is not None
        step_name, step_expr = result
        self.assertIn('Table.Group', step_expr)

    def test_empty_fields_returns_none(self):
        node = {'groupByFields': [], 'aggregateFields': []}
        result = _parse_aggregate_node(node)
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════
# Join step
# ═══════════════════════════════════════════════════════════════════

class TestJoinNode(unittest.TestCase):
    """Test _parse_join_node."""

    def test_inner_join(self):
        node = {
            'joinType': 'inner',
            'joinConditions': [
                {'leftColumn': 'ID', 'rightColumn': 'CustID'},
            ],
        }
        right_fields = [
            {'name': 'CustID'}, {'name': 'CustName'},
        ]
        result = _parse_join_node(node, 'Customers', right_fields)
        self.assertIsNotNone(result)
        assert result is not None
        # Should return list of steps (join + expand)
        self.assertIsInstance(result, list)
        self.assertTrue(len(result) >= 1)

    def test_no_conditions_returns_none(self):
        node = {'joinType': 'inner', 'joinConditions': []}
        result = _parse_join_node(node, 'Table', [])
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════
# Union step
# ═══════════════════════════════════════════════════════════════════

class TestUnionNode(unittest.TestCase):
    """Test _parse_union_node."""

    def test_union_two_tables(self):
        node = {}
        result = _parse_union_node(node, ['TableA', 'TableB'])
        self.assertIsNotNone(result)
        assert result is not None
        step_name, step_expr = result
        self.assertIn('Table.Combine', step_expr)

    def test_empty_tables_returns_none(self):
        result = _parse_union_node({}, [])
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════
# Pivot step
# ═══════════════════════════════════════════════════════════════════

class TestPivotNode(unittest.TestCase):
    """Test _parse_pivot_node."""

    def test_unpivot(self):
        node = {
            'pivotType': 'columnsToRows',
            'pivotFields': [{'name': 'Q1'}, {'name': 'Q2'}],
            'pivotValuesName': 'Value',
            'pivotNamesName': 'Quarter',
        }
        result = _parse_pivot_node(node)
        self.assertIsNotNone(result)
        assert result is not None
        _, expr = result
        self.assertIn('Unpivot', expr)

    def test_pivot(self):
        node = {
            'pivotType': 'rowsToColumns',
            'pivotKeyField': {'name': 'Category'},
            'pivotValueField': {'name': 'Sales'},
            'aggregation': 'SUM',
        }
        result = _parse_pivot_node(node)
        self.assertIsNotNone(result)
        assert result is not None
        _, expr = result
        self.assertIn('Pivot', expr)

    def test_unknown_pivot_type_returns_none(self):
        result = _parse_pivot_node({'pivotType': 'somethingElse'})
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════
# Input node parsing
# ═══════════════════════════════════════════════════════════════════

class TestParseInputNode(unittest.TestCase):
    """Test _parse_input_node."""

    def test_csv_input(self):
        node = _input_node('sales', 'c1')
        connections = {
            'c1': {'connectionAttributes': {'class': 'csv', 'filename': 'sales.csv'}},
        }
        conn, table = _parse_input_node(node, connections)
        self.assertEqual(conn['type'], 'textscan')
        self.assertEqual(table['name'], 'sales')
        self.assertEqual(len(table['columns']), 3)

    def test_postgres_input(self):
        node = {
            'baseType': 'input',
            'nodeType': '.v1.LoadSql',
            'name': 'Orders',
            'connectionId': 'c1',
            'connectionAttributes': {'table': 'public.orders'},
            'fields': [{'name': 'id', 'type': 'integer'}],
            'nextNodes': [],
        }
        connections = {
            'c1': {'connectionAttributes': {
                'class': 'postgres',
                'server': 'localhost',
                'dbname': 'mydb',
            }},
        }
        conn, table = _parse_input_node(node, connections)
        self.assertEqual(conn['type'], 'postgres')
        self.assertEqual(conn['details']['server'], 'localhost')
        self.assertEqual(conn['details']['database'], 'mydb')


# ═══════════════════════════════════════════════════════════════════
# M table ref cleaning
# ═══════════════════════════════════════════════════════════════════

class TestCleanMTableRef(unittest.TestCase):
    """Test _clean_m_table_ref."""

    def test_strips_csv_extension(self):
        self.assertEqual(_clean_m_table_ref('sales.csv'), 'sales')

    def test_strips_xlsx_extension(self):
        self.assertEqual(_clean_m_table_ref('data.xlsx'), 'data')

    def test_replaces_spaces(self):
        self.assertEqual(_clean_m_table_ref('my table'), 'my_table')

    def test_no_extension(self):
        self.assertEqual(_clean_m_table_ref('RawTable'), 'RawTable')


# ═══════════════════════════════════════════════════════════════════
# Merge prep with workbook
# ═══════════════════════════════════════════════════════════════════

class TestMergePrepWithWorkbook(unittest.TestCase):
    """Test merge_prep_with_workbook."""

    def _run_merge(self, prep, twb):
        """Run merge_prep_with_workbook with stdout redirected to avoid
        Unicode encoding errors on Windows cp1252 consoles."""
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            return merge_prep_with_workbook(prep, twb)
        finally:
            sys.stdout = old_stdout

    def test_no_prep_data_returns_combined(self):
        prep = [{'name': 'prep.Out', 'tables': [{'name': 'Out'}],
                 'm_query_override': ''}]
        twb = [{'name': 'ds1', 'tables': [{'name': 'T1'}]}]
        original_prep, original_twb = deepcopy(prep), deepcopy(twb)
        with self.assertRaisesRegex(ValueError, 'nonempty string M query'):
            self._run_merge(prep, twb)
        self.assertEqual(prep, original_prep)
        self.assertEqual(twb, original_twb)

    def test_matching_table_merges_m_query(self):
        prep = [{'name': 'prep.Orders', 'caption': 'Orders',
                 'tables': [{'name': 'Orders'}],
                 'm_query_override': 'let Source = Csv.Document() in Source'}]
        twb = [{'name': 'ds1', 'tables': [{'name': 'Orders'}]}]
        result = self._run_merge(prep, twb)
        # TWB datasource should have the override
        ds = result[0]
        self.assertIn('m_query_overrides', ds)
        self.assertIn('Orders', ds['m_query_overrides'])

    def test_unmatched_prep_added_standalone(self):
        prep = [{'name': 'prep.NewTable', 'caption': 'NewTable',
                 'tables': [{'name': 'NewTable'}],
                 'm_query_override': 'let Source = #table({}) in Source'}]
        twb = [{'name': 'ds1', 'tables': [{'name': 'OtherTable'}]}]
        original_prep, original_twb = deepcopy(prep), deepcopy(twb)
        with self.assertRaisesRegex(ValueError, 'No exact Prep bindings'):
            self._run_merge(prep, twb)
        self.assertEqual(prep, original_prep)
        self.assertEqual(twb, original_twb)

    def test_invalid_empty_binding_shapes_are_rejected_without_mutation(self):
        cases = [
            (
                [{'name': 'prep.Empty', 'tables': [],
                  'm_query_override': 'let Source = 1 in Source'}],
                [{'name': 'ds1', 'tables': [{'name': 'Empty'}]}],
                'one table per parsed datasource',
            ),
            (
                [{'name': 'prep.Out', 'tables': [{'name': 'Out'}],
                  'm_query_override': 'let Source = 1 in Source'}],
                [{'name': 'ds1', 'tables': []}],
                'No exact Prep bindings',
            ),
        ]
        for prep, twb, message in cases:
            with self.subTest(message=message):
                original_prep, original_twb = deepcopy(prep), deepcopy(twb)
                with self.assertRaisesRegex(ValueError, message):
                    self._run_merge(prep, twb)
                self.assertEqual(prep, original_prep)
                self.assertEqual(twb, original_twb)

    def test_ambiguous_prep_alias_is_rejected_without_mutation(self):
        prep = [
            {'name': 'prep.First', 'caption': 'Shared',
             'tables': [{'name': 'First'}], 'm_query_override': 'let A = 1 in A'},
            {'name': 'prep.Second', 'caption': 'Shared',
             'tables': [{'name': 'Second'}], 'm_query_override': 'let B = 1 in B'},
        ]
        twb = [{'name': 'ds1', 'tables': [{'name': 'Shared'}]}]
        original_prep, original_twb = deepcopy(prep), deepcopy(twb)
        with self.assertRaisesRegex(ValueError, 'Ambiguous Prep merge alias'):
            self._run_merge(prep, twb)
        self.assertEqual(prep, original_prep)
        self.assertEqual(twb, original_twb)

    def test_incomplete_published_binding_is_rejected_without_mutation(self):
        prep = [{'name': 'prep.Orders', 'caption': 'Orders',
                 'tables': [{'name': 'Orders'}],
                 'm_query_override': 'let Source = 1 in Source'}]
        twb = [
            {'name': 'published-orders', 'connection': {'type': 'published'},
             'tables': [{'name': 'Orders'}]},
            {'name': 'published-customers', 'connection': {'type': 'published'},
             'tables': [{'name': 'Customers'}]},
        ]
        original_prep, original_twb = deepcopy(prep), deepcopy(twb)
        with self.assertRaisesRegex(ValueError, 'Incomplete Prep binding'):
            self._run_merge(prep, twb)
        self.assertEqual(prep, original_prep)
        self.assertEqual(twb, original_twb)


# ═══════════════════════════════════════════════════════════════════
# End-to-end flow parsing (minimal flow)
# ═══════════════════════════════════════════════════════════════════

class TestParseFlowEndToEnd(unittest.TestCase):
    """Test parse_prep_flow with a minimal synthetic flow."""

    def test_simple_input_to_output(self):

        flow = {
            'nodes': {
                'n1': {
                    'baseType': 'input',
                    'nodeType': '.v1.LoadCsv',
                    'name': 'Sales',
                    'connectionId': 'c1',
                    'connectionAttributes': {'class': 'csv', 'filename': 'sales.csv'},
                    'fields': [
                        {'name': 'Product', 'type': 'string'},
                        {'name': 'Amount', 'type': 'real'},
                    ],
                    'nextNodes': [{'nextNodeId': 'n2'}],
                },
                'n2': {
                    'baseType': 'output',
                    'nodeType': '.v1.PublishExtract',
                    'name': 'SalesOut',
                    'nextNodes': [],
                },
            },
            'connections': {
                'c1': {'connectionAttributes': {'class': 'csv', 'filename': 'sales.csv'}},
            },
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.tfl', delete=False,
                                         encoding='utf-8') as f:
            json.dump(flow, f)
            path = f.name

        try:
            # Suppress print output
            _old = sys.stdout
            sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
            try:
                result = parse_prep_flow(path)
            finally:
                sys.stdout = _old

            self.assertIsInstance(result, list)
            self.assertGreater(len(result), 0)
            ds = result[0]
            self.assertIn('m_query_override', ds)
            self.assertTrue(ds['m_query_override'])  # Should have M query
        finally:
            os.unlink(path)

    def test_malformed_flow_structure_raises(self):
        flow = {
            'nodes': {
                'bad': None,
                'n1': _input_node(next_ids=['n2']),
                'n2': _output_node(),
            },
            'connections': None,
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.tfl', delete=False,
                                         encoding='utf-8') as f:
            json.dump(flow, f)
            path = f.name

        try:
            _old = sys.stdout
            sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
            try:
                with self.assertRaisesRegex(ValueError, "bad.*expected object"):
                    parse_prep_flow(path)
            finally:
                sys.stdout = _old

        finally:
            os.unlink(path)


# ═══════════════════════════════════════════════════════════════════
# Type mapping
# ═══════════════════════════════════════════════════════════════════

class TestPrepTypeMaps(unittest.TestCase):
    """Test mapping dictionaries."""

    def test_connection_map_coverage(self):
        # Core connectors should be mapped
        for key in ['csv', 'excel', 'sqlserver', 'postgres', 'mysql', 'bigquery']:
            self.assertIn(key, _PREP_CONNECTION_MAP, f'Missing connector: {key}')

    def test_type_map_coverage(self):
        for key in ['string', 'integer', 'real', 'date', 'datetime', 'boolean']:
            self.assertIn(key, _PREP_TYPE_MAP, f'Missing type: {key}')

    def test_agg_map_coverage(self):
        for key in ['SUM', 'AVG', 'COUNT', 'COUNTD', 'MIN', 'MAX']:
            self.assertIn(key, _PREP_AGG_MAP, f'Missing agg: {key}')

    def test_join_map_coverage(self):
        for key in ['inner', 'left', 'right', 'full', 'leftOnly', 'rightOnly']:
            self.assertIn(key, _PREP_JOIN_MAP, f'Missing join type: {key}')


# ═══════════════════════════════════════════════════════════════════
# Additional clean action coverage (uncovered action types)
# ═══════════════════════════════════════════════════════════════════

class TestCleanActionsExtended(unittest.TestCase):
    """Cover _convert_action_to_m_step paths not tested by TestCleanActions."""

    def _step(self, action_type, action_dict):
        """Call _convert_action_to_m_step directly."""
        return _convert_action_to_m_step(action_type, action_dict, {})

    # --- FilterOperation ---
    def test_filter_operation_keep(self):
        step = self._step('FilterOperation', {
            'filterExpression': '[Amount] > 100', 'filterType': 'keep'})
        self.assertIsNotNone(step)
        _, expr = step
        self.assertIn('Table.SelectRows', expr)
        self.assertNotIn('not', expr)

    def test_filter_operation_remove(self):
        step = self._step('FilterOperation', {
            'filterExpression': '[Status] = "Deleted"', 'filterType': 'remove'})
        self.assertIsNotNone(step)
        _, expr = step
        self.assertIn('not', expr)

    def test_filter_operation_counter_increments(self):
        counter = {}
        s1 = _convert_action_to_m_step('FilterOperation',
                                        {'filterExpression': '[X]>1'}, counter)
        s2 = _convert_action_to_m_step('FilterOperation',
                                        {'filterExpression': '[Y]>2'}, counter)
        self.assertIsNotNone(s1)
        self.assertIsNotNone(s2)
        self.assertNotEqual(s1[0], s2[0])  # distinct step names

    # --- FilterRange ---
    def test_filter_range(self):
        step = self._step('FilterRange', {
            'columnName': 'Price', 'min': 10, 'max': 100})
        self.assertIsNotNone(step)

    def test_filter_range_no_column(self):
        step = self._step('FilterRange', {'min': 0, 'max': 10})
        self.assertIsNone(step)

    # --- ReplaceNulls ---
    def test_replace_nulls(self):
        step = self._step('ReplaceNulls', {'columnName': 'Region', 'replacement': 'Unknown'})
        self.assertIsNotNone(step)

    def test_replace_nulls_no_column(self):
        step = self._step('ReplaceNulls', {'replacement': 'X'})
        self.assertIsNone(step)

    # --- MergeColumns ---
    def test_merge_columns(self):
        step = self._step('MergeColumns', {
            'columns': ['First', 'Last'], 'separator': ' ', 'newColumnName': 'FullName'})
        self.assertIsNotNone(step)

    def test_merge_columns_no_columns(self):
        step = self._step('MergeColumns', {'columns': [], 'separator': ','})
        self.assertIsNone(step)

    # --- CleanOperation variants ---
    def test_clean_operation_upper(self):
        step = self._step('CleanOperation', {'columnName': 'Name', 'operation': 'upper'})
        self.assertIsNotNone(step)

    def test_clean_operation_lower(self):
        step = self._step('CleanOperation', {'columnName': 'Name', 'operation': 'lower'})
        self.assertIsNotNone(step)

    def test_clean_operation_proper(self):
        step = self._step('CleanOperation', {'columnName': 'Name', 'operation': 'proper'})
        self.assertIsNotNone(step)

    def test_clean_operation_removeletters(self):
        step = self._step('CleanOperation', {'columnName': 'Code', 'operation': 'removeletters'})
        self.assertIsNotNone(step)

    def test_clean_operation_no_column(self):
        step = self._step('CleanOperation', {'operation': 'trim'})
        self.assertIsNone(step)

    # --- FillValues up ---
    def test_fill_values_up(self):
        node = _clean_node(actions=[
            {'actionType': '.v1.FillValues', 'columnName': 'X', 'direction': 'up'},
        ])
        steps = _parse_clean_actions(node)
        self.assertEqual(len(steps), 1)
        _, expr = steps[0]
        self.assertIn('FillUp', expr)

    # --- ConditionalColumn ---
    def test_conditional_column(self):
        step = self._step('ConditionalColumn', {
            'newColumnName': 'Tier',
            'rules': [
                {'condition': '[Amount] > 1000', 'value': 'High'},
                {'condition': '[Amount] > 500', 'value': 'Medium'},
            ],
            'defaultValue': 'Low',
        })
        self.assertIsNotNone(step)
        # The generated M must NOT have double 'each' (each if each ...)
        name, expr = step
        self.assertNotIn('each if each', expr)
        # Must have proper if...then...else
        self.assertIn('if', expr)
        self.assertIn('then', expr)
        self.assertIn('else', expr)

    def test_conditional_column_no_rules(self):
        step = self._step('ConditionalColumn', {'newColumnName': 'X', 'rules': []})
        self.assertIsNone(step)

    # --- ExtractValues ---
    def test_extract_values(self):
        step = self._step('ExtractValues', {
            'columnName': 'Email', 'pattern': '@.*$', 'newColumnName': 'Domain'})
        self.assertIsNotNone(step)
        name, expr = step
        self.assertIn('AddColumn', expr)

    def test_extract_values_no_column(self):
        step = self._step('ExtractValues', {'pattern': '.*'})
        self.assertIsNone(step)

    def test_extract_values_counter(self):
        counter = {}
        s1 = _convert_action_to_m_step('ExtractValues',
                                        {'columnName': 'A', 'pattern': '.'}, counter)
        s2 = _convert_action_to_m_step('ExtractValues',
                                        {'columnName': 'B', 'pattern': '.'}, counter)
        self.assertNotEqual(s1[0], s2[0])

    # --- CustomCalculation ---
    def test_custom_calculation(self):
        step = self._step('CustomCalculation', {
            'columnName': 'Profit', 'expression': '[Sales] - [Cost]'})
        self.assertIsNotNone(step)

    def test_custom_calculation_default_name(self):
        step = self._step('CustomCalculation', {'expression': '42'})
        self.assertIsNotNone(step)

    # --- afterActionGroup ---
    def test_after_action_group_actions(self):
        """afterActionGroup actions should be included."""
        node = {
            'baseType': 'transform',
            'nodeType': '.v2018_3_3.SuperTransform',
            'name': 'Clean',
            'beforeActionGroup': {'actions': [
                {'actionType': '.v1.RemoveColumn', 'columnName': 'Drop1'},
            ]},
            'afterActionGroup': {'actions': [
                {'actionType': '.v1.RemoveColumn', 'columnName': 'Drop2'},
            ]},
            'nextNodes': [],
        }
        steps = _parse_clean_actions(node)
        self.assertEqual(len(steps), 2)


# ═══════════════════════════════════════════════════════════════════
# Transform node dispatch (process_transform_node branches)
# ═══════════════════════════════════════════════════════════════════

class TestProcessTransformNode(unittest.TestCase):
    """Test _process_transform_node and _process_prep_node dispatch branches."""

    def _suppress(self):
        """Redirect stdout to suppress print output."""
        import io as _io
        self._old_stdout = sys.stdout
        sys.stdout = _io.StringIO()

    def _restore(self):
        sys.stdout = self._old_stdout

    def setUp(self):
        self._suppress()

    def tearDown(self):
        self._restore()

    def _base_input_result(self, name='Input1'):
        """Return a basic node_results entry simulating an input node."""
        return {
            'connection': {'type': 'textscan', 'details': {}},
            'table': {'name': name, 'columns': []},
            'name': name,
            'm_query': 'let\n    Source = Csv.Document(File.Contents("data.csv"))\nin\n    Source',
            'fields': [{'name': 'ID', 'type': 'integer'}, {'name': 'Val', 'type': 'real'}],
        }

    # --- Aggregate ---
    def test_aggregate_transform(self):
        nid = 'agg1'
        node = {
            'baseType': 'transform', 'nodeType': '.v1.Aggregate', 'name': 'GroupBy',
            'groupByFields': [{'name': 'Region'}],
            'aggregateFields': [{'name': 'Sales', 'aggregation': 'SUM', 'newColumnName': 'TotalSales'}],
            'nextNodes': [],
        }
        nodes = {'inp': _input_node(next_ids=[nid]), nid: node}
        node_results = {'inp': {**self._base_input_result(),
                                'fields': [{'name': 'Region', 'type': 'string'},
                                           {'name': 'Sales', 'type': 'real'}]}}
        secondary = set()
        _process_transform_node(nid, node, nodes, ['inp'], 'Aggregate',
                                node_results, secondary, 'GroupBy')
        self.assertIn(nid, node_results)
        self.assertIn('Table.Group', node_results[nid]['m_query'])

    def test_aggregate_rejects_missing_group_and_measure_columns(self):
        for field in ('Region', 'Sales'):
            with self.subTest(missing=field):
                nid = 'agg_missing'
                node = {
                    'baseType': 'transform', 'nodeType': '.v1.Aggregate',
                    'name': 'GroupBy',
                    'groupByFields': [{'name': 'Region'}],
                    'aggregateFields': [
                        {'name': 'Sales', 'aggregation': 'SUM', 'newColumnName': 'TotalSales'},
                    ],
                    'nextNodes': [],
                }
                fields = [{'name': 'Sales', 'type': 'real'}] if field == 'Region' else [
                    {'name': 'Region', 'type': 'string'}]
                upstream = {**self._base_input_result(), 'fields': fields}
                node_results = {'inp': upstream}
                original = deepcopy(node_results)
                with self.assertRaisesRegex(ValueError, 'not in the upstream schema'):
                    _process_transform_node(
                        nid, node, {'inp': _input_node(next_ids=[nid]), nid: node},
                        ['inp'], 'Aggregate', node_results, set(), 'GroupBy')
                self.assertEqual(node_results, original)
                self.assertNotIn(nid, node_results)

    def test_aggregate_rejects_text_measure_for_sum(self):
        nid = 'agg_text'
        node = {
            'baseType': 'transform', 'nodeType': '.v1.Aggregate', 'name': 'GroupBy',
            'groupByFields': [{'name': 'Region'}],
            'aggregateFields': [{'name': 'Sales', 'aggregation': 'SUM'}],
            'nextNodes': [],
        }
        node_results = {'inp': {**self._base_input_result(), 'fields': [
            {'name': 'Region', 'type': 'string'}, {'name': 'Sales', 'type': 'string'}]}}
        original = deepcopy(node_results)
        with self.assertRaisesRegex(ValueError, 'requires numeric input.*Sales'):
            _process_transform_node(
                nid, node, {'inp': _input_node(next_ids=[nid]), nid: node},
                ['inp'], 'Aggregate', node_results, set(), 'GroupBy')
        self.assertEqual(node_results, original)
        self.assertNotIn(nid, node_results)

    # --- Join with leftNodeId / rightNodeId ---
    def test_join_transform_with_explicit_ids(self):
        nid = 'join1'
        node = {
            'baseType': 'transform', 'nodeType': '.v1.Join', 'name': 'JoinStep',
            'leftNodeId': 'left', 'rightNodeId': 'right',
            'joinType': 'inner',
            'joinConditions': [{'leftColumn': 'ID', 'rightColumn': 'CID'}],
            'nextNodes': [],
        }
        nodes = {'left': _input_node('Left'), 'right': _input_node('Right'), nid: node}
        node_results = {
            'left': self._base_input_result('Left'),
            'right': {**self._base_input_result('Right'),
                      'fields': [{'name': 'CID'}, {'name': 'Name'}]},
        }
        secondary = set()
        _process_transform_node(nid, node, nodes, ['left', 'right'], 'Join',
                                node_results, secondary, 'JoinStep')
        self.assertIn(nid, node_results)
        self.assertIn('right', secondary)  # right branch marked secondary

    def test_join_transform_single_upstream_raises(self):
        """A one-input join must not silently become a passthrough."""
        nid = 'join1'
        node = {
            'baseType': 'transform', 'nodeType': '.v1.Join', 'name': 'JoinStep',
            'joinType': 'left',
            'joinConditions': [{'leftColumn': 'ID', 'rightColumn': 'CID'}],
            'nextNodes': [],
        }
        nodes = {'inp': _input_node(next_ids=[nid]), nid: node}
        node_results = {'inp': self._base_input_result()}
        secondary = set()
        with self.assertRaisesRegex(ValueError, 'Prep join.*provenance required'):
            _process_transform_node(nid, node, nodes, ['inp'], 'Join',
                                    node_results, secondary, 'JoinStep')
        self.assertNotIn(nid, node_results)
        self.assertEqual(secondary, set())

    # --- Union ---
    def test_union_transform(self):
        nid = 'union1'
        node = {'baseType': 'transform', 'nodeType': '.v1.Union', 'name': 'UnionStep',
                'nextNodes': []}
        nodes = {'a': _input_node('A'), 'b': _input_node('B'), nid: node}
        node_results = {
            'a': self._base_input_result('A'),
            'b': self._base_input_result('B'),
        }
        secondary = set()
        _process_transform_node(nid, node, nodes, ['a', 'b'], 'Union',
                                node_results, secondary, 'UnionStep')
        self.assertIn(nid, node_results)
        self.assertIn('Table.Combine', node_results[nid]['m_query'])
        # Union marks all upstream as secondary
        self.assertIn('a', secondary)
        self.assertIn('b', secondary)

    # --- Pivot ---
    def test_pivot_transform_unpivot(self):
        nid = 'piv1'
        node = {
            'baseType': 'transform', 'nodeType': '.v1.Pivot', 'name': 'Unpivot',
            'pivotType': 'columnsToRows',
            'pivotFields': [{'name': 'Q1'}, {'name': 'Q2'}],
            'pivotValuesName': 'Value', 'pivotNamesName': 'Quarter',
            'nextNodes': [],
        }
        nodes = {'inp': _input_node(next_ids=[nid]), nid: node}
        node_results = {'inp': {**self._base_input_result(), 'fields': [
            {'name': 'Q1', 'type': 'real'}, {'name': 'Q2', 'type': 'real'}]}}
        secondary = set()
        _process_transform_node(nid, node, nodes, ['inp'], 'Pivot',
                                node_results, secondary, 'Unpivot')
        self.assertIn(nid, node_results)
        self.assertIn('Unpivot', node_results[nid]['m_query'])

    def test_unpivot_rejects_missing_pivot_column(self):
        nid = 'piv_missing'
        node = {
            'baseType': 'transform', 'nodeType': '.v1.Pivot', 'name': 'Unpivot',
            'pivotType': 'columnsToRows',
            'pivotFields': [{'name': 'Q1'}, {'name': 'Q2'}],
            'pivotValuesName': 'Value', 'pivotNamesName': 'Quarter',
            'nextNodes': [],
        }
        node_results = {'inp': {**self._base_input_result(), 'fields': [
            {'name': 'Q1', 'type': 'real'}]}}
        original = deepcopy(node_results)
        with self.assertRaisesRegex(ValueError, 'not in the upstream schema'):
            _process_transform_node(
                nid, node, {'inp': _input_node(next_ids=[nid]), nid: node},
                ['inp'], 'Pivot', node_results, set(), 'Unpivot')
        self.assertEqual(node_results, original)
        self.assertNotIn(nid, node_results)

    # --- Script ---
    def test_script_transform(self):
        nid = 'scr1'
        node = {
            'baseType': 'transform', 'nodeType': '.v1.RunScript', 'name': 'PyScript',
            'scriptLanguage': 'Python', 'nextNodes': [],
        }
        nodes = {'inp': _input_node(next_ids=[nid]), nid: node}
        node_results = {'inp': self._base_input_result()}
        secondary = set()
        _process_transform_node(nid, node, nodes, ['inp'], 'Script',
                                node_results, secondary, 'PyScript')
        self.assertIn(nid, node_results)
        self.assertIn('script_warning', node_results[nid]['m_query'])

    def test_run_script_variant(self):
        nid = 'scr2'
        node = {'baseType': 'transform', 'nodeType': '.v1.RunCommand', 'name': 'RCmd',
                'language': 'R', 'nextNodes': []}
        nodes = {'inp': _input_node(next_ids=[nid]), nid: node}
        node_results = {'inp': self._base_input_result()}
        _process_transform_node(nid, node, nodes, ['inp'], 'RunCommand',
                                node_results, set(), 'RCmd')
        self.assertIn(nid, node_results)

    # --- Prediction ---
    def test_prediction_transform(self):
        nid = 'pred1'
        node = {'baseType': 'transform', 'nodeType': '.v1.Prediction', 'name': 'ML',
                'nextNodes': []}
        nodes = {'inp': _input_node(next_ids=[nid]), nid: node}
        node_results = {'inp': self._base_input_result()}
        _process_transform_node(nid, node, nodes, ['inp'], 'Prediction',
                                node_results, set(), 'ML')
        self.assertIn(nid, node_results)
        self.assertIn('prediction_warning', node_results[nid]['m_query'])

    def test_tabpy_variant(self):
        nid = 'tp1'
        node = {'baseType': 'transform', 'nodeType': '.v1.TabPy', 'name': 'TabPy',
                'nextNodes': []}
        nodes = {'inp': _input_node(next_ids=[nid]), nid: node}
        node_results = {'inp': self._base_input_result()}
        _process_transform_node(nid, node, nodes, ['inp'], 'TabPy',
                                node_results, set(), 'TabPy')
        self.assertIn(nid, node_results)

    # --- CrossJoin ---
    def test_cross_join_transform(self):
        nid = 'cj1'
        node = {'baseType': 'transform', 'nodeType': '.v1.CrossJoin', 'name': 'Cross',
                'nextNodes': []}
        nodes = {'a': _input_node('A'), 'b': _input_node('B'), nid: node}
        node_results = {
            'a': {**self._base_input_result('A'), 'fields': [
                {'name': 'ID', 'type': 'integer'}, {'name': 'Val', 'type': 'real'}]},
            'b': {**self._base_input_result('B'), 'fields': [
                {'name': 'CustID', 'type': 'integer'}, {'name': 'CustName', 'type': 'string'}]},
        }
        secondary = set()
        _process_transform_node(nid, node, nodes, ['a', 'b'], 'CrossJoin',
                                node_results, secondary, 'Cross')
        self.assertIn(nid, node_results)
        self.assertIn('Table.Join', node_results[nid]['m_query'])
        self.assertIn('B', node_results[nid]['m_query'])
        self.assertEqual(
            [field['name'] for field in node_results[nid]['fields']],
            ['ID', 'Val', 'CustID', 'CustName'])
        self.assertIn('b', secondary)

    def test_cross_join_rejects_colliding_fields_instead_of_mismatching_m_schema(self):
        nid = 'cj_collision'
        node = {'baseType': 'transform', 'nodeType': '.v1.CrossJoin', 'name': 'Cross',
                'nextNodes': []}
        node_results = {
            'a': {**self._base_input_result('A'), 'fields': [
                {'name': 'ID', 'type': 'integer'}, {'name': 'Shared', 'type': 'string'}]},
            'b': {**self._base_input_result('B'), 'fields': [
                {'name': 'CustID', 'type': 'integer'}, {'name': 'Shared', 'type': 'string'}]},
        }
        original = deepcopy(node_results)
        with self.assertRaisesRegex(ValueError, 'duplicate/colliding field'):
            _process_transform_node(
                nid, node, {'a': _input_node('A'), 'b': _input_node('B'), nid: node},
                ['a', 'b'], 'CrossJoin', node_results, set(), 'Cross')
        self.assertEqual(node_results, original)
        self.assertNotIn(nid, node_results)

    # --- PublishedDataSource ---
    def test_published_datasource_transform(self):
        nid = 'pub1'
        node = {'baseType': 'transform', 'nodeType': '.v1.LoadPublishedDataSource',
                'name': 'PubDS', 'publishedDatasourceName': 'SharedSales',
                'fields': [{'name': 'Revenue', 'type': 'real'}], 'nextNodes': []}
        nodes = {nid: node}
        node_results = {}
        _process_transform_node(nid, node, nodes, [], 'PublishedDataSource',
                                node_results, set(), 'PubDS')
        self.assertIn(nid, node_results)
        self.assertIn('SharedSales', node_results[nid]['m_query'])
        self.assertEqual(node_results[nid]['connection']['type'], 'published')

    def test_load_published_datasource_variant(self):
        nid = 'pub2'
        node = {'baseType': 'transform', 'nodeType': '.v1.LoadPublishedDataSource',
                'name': 'PDS', 'datasourceName': 'MyDS', 'nextNodes': []}
        nodes = {nid: node}
        node_results = {}
        _process_transform_node(nid, node, nodes, [], 'LoadPublishedDataSource',
                                node_results, set(), 'PDS')
        self.assertIn(nid, node_results)
        self.assertIn('MyDS', node_results[nid]['m_query'])

    # --- Unknown transforms must fail explicitly ---
    def test_unknown_transform_raises(self):
        nid = 'unk1'
        node = {'baseType': 'transform', 'nodeType': '.v1.FutureStep', 'name': 'Future',
                'nextNodes': []}
        nodes = {'inp': _input_node(next_ids=[nid]), nid: node}
        node_results = {'inp': self._base_input_result()}
        with self.assertRaisesRegex(ValueError, 'unsupported transform.*FutureStep'):
            _process_transform_node(nid, node, nodes, ['inp'], 'FutureStep',
                                    node_results, set(), 'Future')
        self.assertNotIn(nid, node_results)


# ═══════════════════════════════════════════════════════════════════
# Output node handling (_process_prep_node)
# ═══════════════════════════════════════════════════════════════════

class TestProcessPrepNode(unittest.TestCase):
    """Test _process_prep_node for output nodes."""

    def setUp(self):
        self._old = sys.stdout
        sys.stdout = io.StringIO()

    def tearDown(self):
        sys.stdout = self._old

    def test_output_node_copies_upstream(self):
        nodes = {
            'inp': _input_node('Data', next_ids=['out']),
            'out': _output_node('FinalOutput'),
        }
        connections = {'conn1': {'connectionAttributes': {'class': 'csv', 'filename': 'x.csv'}}}
        node_results = {}
        secondary = set()
        # Process input first
        _process_prep_node('inp', nodes, connections, node_results, secondary)
        self.assertIn('inp', node_results)
        # Then output
        _process_prep_node('out', nodes, connections, node_results, secondary)
        self.assertIn('out', node_results)
        self.assertTrue(node_results['out'].get('is_output'))
        self.assertEqual(node_results['out']['name'], 'FinalOutput')

    def test_output_node_no_upstream_raises(self):
        """Disconnected explicit outputs are conversion failures."""
        nodes = {'out': _output_node('Orphan')}
        node_results = {}
        with self.assertRaisesRegex(ValueError, 'output.*expected one resolved upstream'):
            _process_prep_node('out', nodes, {}, node_results, set())
        self.assertNotIn('out', node_results)


# ═══════════════════════════════════════════════════════════════════
# _collect_prep_datasources
# ═══════════════════════════════════════════════════════════════════

class TestCollectPrepDatasources(unittest.TestCase):
    """Test _collect_prep_datasources secondary branches, output nodes, and leaf fallback."""

    def setUp(self):
        self._old = sys.stdout
        sys.stdout = io.StringIO()

    def tearDown(self):
        sys.stdout = self._old

    def _base_result(self, name, is_output=False, fields=None):
        return {
            'connection': {'type': 'textscan', 'details': {}},
            'table': {'name': name, 'columns': []},
            'name': name,
            'm_query': f'let Source = #"{name}" in Source',
            'fields': fields or [{'name': 'Col1', 'type': 'string'}],
            'is_output': is_output,
        }

    def test_secondary_branch_emitted(self):
        """Secondary branch nodes (right side of joins) are emitted first."""
        sorted_ids = ['left', 'right', 'join', 'out']
        nodes = {
            'left': _input_node('Left'),
            'right': _input_node('Right'),
            'join': {'baseType': 'transform', 'nextNodes': [{'nextNodeId': 'out'}]},
            'out': _output_node('Result'),
        }
        node_results = {
            'left': self._base_result('Left'),
            'right': self._base_result('Right'),
            'join': self._base_result('Join'),
            'out': self._base_result('Result', is_output=True),
        }
        secondary = {'right'}
        ds = _collect_prep_datasources(sorted_ids, nodes, node_results, secondary)
        # Should have secondary (Right) + output (Result)
        self.assertEqual(len(ds), 2)
        names = [d['name'] for d in ds]
        self.assertTrue(any('Right' in n for n in names))

    def test_output_nodes_collected(self):
        sorted_ids = ['inp', 'out']
        nodes = {'inp': _input_node('Data'), 'out': _output_node('Out')}
        node_results = {
            'inp': self._base_result('Data'),
            'out': self._base_result('Out', is_output=True,
                                     fields=[{'name': 'F1', 'type': 'integer'}]),
        }
        ds = _collect_prep_datasources(sorted_ids, nodes, node_results, set())
        self.assertEqual(len(ds), 1)
        self.assertEqual(ds[0]['tables'][0]['columns'][0]['name'], 'F1')

    def test_leaf_fallback_when_no_outputs(self):
        """When no output nodes exist, leaf nodes (no nextNodes) are used."""
        sorted_ids = ['inp', 'clean']
        nodes = {
            'inp': _input_node('Data', next_ids=['clean']),
            'clean': {'baseType': 'transform', 'nextNodes': [],
                      'nodeType': '.v1.SuperTransform', 'name': 'Clean'},
        }
        node_results = {
            'inp': self._base_result('Data'),
            'clean': self._base_result('Cleaned'),
        }
        ds = _collect_prep_datasources(sorted_ids, nodes, node_results, set())
        # clean has no nextNodes and no is_output → leaf fallback
        self.assertGreater(len(ds), 0)

    def test_empty_results_raises(self):
        with self.assertRaisesRegex(ValueError, 'zero outputs'):
            _collect_prep_datasources(['n1'], {'n1': _input_node()}, {}, set())


# ═══════════════════════════════════════════════════════════════════
# Merge prep with workbook — extended coverage
# ═══════════════════════════════════════════════════════════════════

class TestMergePrepExtended(unittest.TestCase):
    """Extended merge_prep_with_workbook coverage."""

    def _run(self, prep, twb):
        old = sys.stdout
        sys.stdout = io.StringIO()
        try:
            return merge_prep_with_workbook(prep, twb)
        finally:
            sys.stdout = old

    def test_caption_based_matching(self):
        """Prep table matched via caption (not just table name)."""
        prep = [{'name': 'prep.Orders', 'caption': 'Sales Orders',
                 'tables': [{'name': 'Orders'}],
                 'm_query_override': 'let S = 1 in S'}]
        twb = [{'name': 'ds1', 'tables': [{'name': 'Sales_Orders'}]}]
        result = self._run(prep, twb)
        # caption converted with replace(' ', '_') → 'Sales_Orders' matches
        ds = result[0]
        self.assertIn('m_query_overrides', ds)
        self.assertIn('Sales_Orders', ds['m_query_overrides'])

    def test_empty_prep_returns_twb(self):
        twb = [{'name': 'ds1', 'tables': [{'name': 'T'}]}]
        result = self._run([], twb)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['name'], 'ds1')

    def test_empty_workbook_is_valid_standalone_prep(self):
        prep = [{'name': 'prep.Standalone', 'caption': 'Standalone',
                 'tables': [{'name': 'Standalone'}],
                 'm_query_override': 'let Source = 1 in Source'}]
        original = deepcopy(prep)
        old = sys.stdout
        sys.stdout = io.StringIO()
        try:
            result, coverage = merge_prep_with_workbook(prep, [], return_report=True)
        finally:
            sys.stdout = old
        self.assertEqual(coverage['status'], 'standalone')
        self.assertEqual(result, prep)
        self.assertEqual(prep, original)

    def test_partial_local_and_unused_output_coverage_is_reported(self):
        prep = [
            {'name': 'prep.Orders', 'caption': 'Orders',
             'tables': [{'name': 'Orders'}],
             'm_query_override': 'let Source = 1 in Source'},
            {'name': 'prep.Auxiliary', 'caption': 'Auxiliary',
             'tables': [{'name': 'Auxiliary'}],
             'm_query_override': 'let Source = 2 in Source'},
        ]
        twb = [
            {'name': 'main', 'tables': [{'name': 'Orders'}, {'name': 'LocalOnly'}]},
            {'name': 'another-local', 'tables': [{'name': 'AlsoLocal'}]},
        ]
        original_prep, original_twb = deepcopy(prep), deepcopy(twb)
        old = sys.stdout
        sys.stdout = io.StringIO()
        try:
            result, coverage = merge_prep_with_workbook(prep, twb, return_report=True)
        finally:
            sys.stdout = old
        self.assertEqual(coverage['status'], 'complete')
        self.assertEqual(len(coverage['matched']), 1)
        self.assertEqual(len(coverage['unmatched_local']), 2)
        self.assertEqual(coverage['unused_outputs'], ['Auxiliary'])
        self.assertEqual([ds['name'] for ds in result], ['main', 'another-local'])
        self.assertEqual(prep, original_prep)
        self.assertEqual(twb, original_twb)

    def test_multiple_matches(self):
        """Multiple TWB tables matching different Prep tables."""
        prep = [
            {'name': 'p1', 'caption': 'Orders', 'tables': [{'name': 'Orders'}],
             'm_query_override': 'let O = 1 in O'},
            {'name': 'p2', 'caption': 'Customers', 'tables': [{'name': 'Customers'}],
             'm_query_override': 'let C = 1 in C'},
        ]
        twb = [
            {'name': 'ds1', 'tables': [{'name': 'Orders'}]},
            {'name': 'ds2', 'tables': [{'name': 'Customers'}]},
        ]
        result = self._run(prep, twb)
        self.assertEqual(len(result), 2)
        self.assertIn('m_query_overrides', result[0])
        self.assertIn('m_query_overrides', result[1])


# ═══════════════════════════════════════════════════════════════════
# parse_prep_flow — extended end-to-end flows
# ═══════════════════════════════════════════════════════════════════

class TestParseFlowExtended(unittest.TestCase):
    """End-to-end parse_prep_flow with more complex topologies."""

    def _write_flow(self, flow_data):
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.tfl', delete=False,
                                        encoding='utf-8')
        json.dump(flow_data, f)
        f.close()
        return f.name

    def _parse(self, path):
        old = sys.stdout
        sys.stdout = io.StringIO()
        try:
            return parse_prep_flow(path)
        finally:
            sys.stdout = old

    def test_empty_flow_raises(self):
        path = self._write_flow({'nodes': {}, 'connections': {}})
        try:
            with self.assertRaisesRegex(ValueError, 'no nodes.*no outputs'):
                self._parse(path)
        finally:
            os.unlink(path)

    def test_input_clean_output_chain(self):
        flow = {
            'nodes': {
                'n1': {
                    'baseType': 'input', 'nodeType': '.v1.LoadCsv',
                    'name': 'Data', 'connectionId': 'c1',
                    'connectionAttributes': {'class': 'csv', 'filename': 'data.csv'},
                    'fields': [{'name': 'A', 'type': 'string'}],
                    'nextNodes': [{'nextNodeId': 'n2'}],
                },
                'n2': {
                    'baseType': 'transform', 'nodeType': '.v2018_3_3.SuperTransform',
                    'name': 'CleanStep',
                    'beforeActionGroup': {'actions': [
                        {'actionType': '.v1.RenameColumn', 'columnName': 'A', 'newColumnName': 'B'},
                    ]},
                    'nextNodes': [{'nextNodeId': 'n3'}],
                },
                'n3': {
                    'baseType': 'output', 'nodeType': '.v1.PublishExtract',
                    'name': 'Output', 'nextNodes': [],
                },
            },
            'connections': {
                'c1': {'connectionAttributes': {'class': 'csv', 'filename': 'data.csv'}},
            },
        }
        path = self._write_flow(flow)
        try:
            result = self._parse(path)
            self.assertGreater(len(result), 0)
            # Output should have M query with rename
            self.assertIn('RenameColumns', result[-1].get('m_query_override', ''))
        finally:
            os.unlink(path)

    def test_join_flow_emits_secondary_table(self):
        flow = {
            'nodes': {
                'left': {
                    'baseType': 'input', 'nodeType': '.v1.LoadCsv',
                    'name': 'Orders', 'connectionId': 'c1',
                    'connectionAttributes': {'class': 'csv', 'filename': 'orders.csv'},
                    'fields': [{'name': 'ID', 'type': 'integer'}, {'name': 'CustID', 'type': 'integer'}],
                    'nextNodes': [{'nextNodeId': 'join'}],
                },
                'right': {
                    'baseType': 'input', 'nodeType': '.v1.LoadCsv',
                    'name': 'Customers', 'connectionId': 'c2',
                    'connectionAttributes': {'class': 'csv', 'filename': 'customers.csv'},
                    'fields': [{'name': 'CustID', 'type': 'integer'}, {'name': 'Name', 'type': 'string'}],
                    'nextNodes': [{'nextNodeId': 'join'}],
                },
                'join': {
                    'baseType': 'transform', 'nodeType': '.v1.Join', 'name': 'JoinStep',
                    'leftNodeId': 'left', 'rightNodeId': 'right',
                    'joinType': 'inner',
                    'joinConditions': [{'leftColumn': 'CustID', 'rightColumn': 'CustID'}],
                    'nextNodes': [{'nextNodeId': 'out'}],
                },
                'out': {
                    'baseType': 'output', 'nodeType': '.v1.PublishExtract',
                    'name': 'JoinedOut', 'nextNodes': [],
                },
            },
            'connections': {
                'c1': {'connectionAttributes': {'class': 'csv', 'filename': 'orders.csv'}},
                'c2': {'connectionAttributes': {'class': 'csv', 'filename': 'customers.csv'}},
            },
        }
        path = self._write_flow(flow)
        try:
            result = self._parse(path)
            # Should have secondary (Customers) + output (JoinedOut)
            self.assertGreaterEqual(len(result), 2)
        finally:
            os.unlink(path)


class TestPrepContainerReports81To83(unittest.TestCase):
    """Regression contracts for recursive containers and strict conversion."""

    def parse(self, flow):
        with tempfile.TemporaryDirectory() as directory:
            return parse_prep_flow(write_flow(directory, flow))

    def assert_actual_chain(self, query):
        expected = [
            ('Table.RenameColumns', '"Raw", "Amount"'),
            ('Table.RemoveColumns', '"Discard"'),
            ('Table.AddColumn', 'each [Amount] * 2'),
            ('Table.TransformColumnTypes', '"Twice", type number'),
            ('Table.SelectRows', 'each [Twice] > 10'),
        ]
        previous = None
        positions = []
        for function, payload in expected:
            lines = [line.strip().rstrip(',') for line in query.splitlines()
                     if function + '(' in line and payload in line]
            self.assertEqual(len(lines), 1, (function, query))
            name, expression = lines[0].split(' = ', 1)
            if previous is not None:
                self.assertTrue(expression.startswith(function + '(' + previous + ','),
                                (previous, expression))
            previous = name
            positions.append(query.index(lines[0]))
        self.assertEqual(positions, sorted(positions))
        self.assertRegex(query, r'Result\s*=\s*' + re.escape(previous) + r'\s*\nin\s+Result\s*$')
        self.assertNotIn('{prev}', query)

    def test_container_executes_all_operations_in_graph_order(self):
        datasources = self.parse(clean_chain())
        self.assertEqual([ds['name'] for ds in datasources], ['prep.PublishedSales'])
        self.assert_actual_chain(datasources[0]['m_query_override'])

    def test_recursive_container_preserves_operations(self):
        datasources = self.parse(clean_chain(nested=True))
        self.assertEqual(len(datasources), 1)
        self.assert_actual_chain(datasources[0]['m_query_override'])

    def test_flat_standalone_operations_dispatch(self):
        flow = clean_chain()
        children = flow['nodes'].pop('box')['loomContainer']['nodes']
        children['filter']['nextNodes'] = [edge('out')]
        flow['nodes'].update(children)
        flow['nodes']['input']['nextNodes'] = [edge('rename')]
        self.assert_actual_chain(self.parse(flow)[0]['m_query_override'])

    def test_normalization_is_deterministic_idempotent_and_nonmutating(self):
        flow = clean_chain(nested=True)
        before = deepcopy(flow)
        normalized = normalize_prep_flow(flow)
        self.assertEqual(flow, before)
        self.assertEqual(normalized, normalize_prep_flow(flow))
        self.assertEqual(normalized, normalize_prep_flow(normalized))
        self.assertEqual(len(normalized['nodes']), 7)
        self.assertNotIn('box', normalized['nodes'])
        self.assertTrue(all(get_prep_node_kind(n) != 'container'
                            for n in normalized['nodes'].values()))
        normalized['nodes']['input']['fields'][0]['name'] = 'ChangedOnlyInCopy'
        self.assertEqual(flow, before)

    def test_scoped_duplicate_child_ids_and_explicit_join_sides(self):
        flow = join_flow()
        normalized = normalize_prep_flow(flow)
        join = normalized['nodes']['join']
        left, right = join['leftNodeId'], join['rightNodeId']
        self.assertNotEqual(left, right)
        self.assertEqual(normalized['nodes'][left]['name'], 'LeftClean')
        self.assertEqual(normalized['nodes'][right]['name'], 'RightClean')
        self.assertEqual(normalized['nodes']['left_input']['nextNodes'], [edge(left)])
        self.assertEqual(normalized['nodes']['right_input']['nextNodes'], [edge(right)])
        datasources = self.parse(flow)
        self.assertEqual({ds['name'] for ds in datasources},
                         {'prep.RightClean', 'prep.JoinedSales'})
        query = next(ds['m_query_override'] for ds in datasources
                     if ds['name'] == 'prep.JoinedSales')
        self.assertIn('Table.AddColumn', query)
        self.assertIn('RightClean', query)
        self.assertIn('JoinKind.LeftOuter', query)
        right_query = next(ds['m_query_override'] for ds in datasources
                           if ds['name'] == 'prep.RightClean')
        self.assertIn('Table.RemoveColumns', right_query)
        self.assertNotIn('LeftMarker', right_query)

    def test_join_side_is_independent_of_node_dictionary_order(self):
        flow = join_flow()
        expected = {ds['name']: ds['m_query_override'] for ds in self.parse(flow)}
        flow['nodes'] = dict(reversed(list(flow['nodes'].items())))
        actual = {ds['name']: ds['m_query_override'] for ds in self.parse(flow)}
        self.assertEqual(actual, expected)

    def test_ambiguous_join_sides_rejected(self):
        with self.assertRaisesRegex(ValueError, 'provenance required.*ambiguous'):
            self.parse(join_flow(explicit=False))

    def test_explicit_single_side_unique_complement(self):
        flow = join_flow()
        del flow['nodes']['join']['rightNodeId']
        query = next(ds['m_query_override'] for ds in self.parse(flow)
                     if ds['name'] == 'prep.JoinedSales')
        self.assertIn('RightClean', query)
        self.assertIn('JoinKind.LeftOuter', query)

    def test_one_input_join_with_explicit_sides_rejected(self):
        flow = join_flow()
        del flow['nodes']['right_box']
        del flow['nodes']['right_input']
        flow['nodes']['join']['rightNodeId'] = 'left_box'
        with self.assertRaisesRegex(ValueError, 'requires two resolved left/right inputs'):
            self.parse(flow)

    def test_multiple_container_entries_require_direct_child_routing(self):
        flow = join_flow()
        nodes = flow['nodes']
        left = nodes.pop('left_box')['loomContainer']['nodes']['same/id']
        right = nodes.pop('right_box')['loomContainer']['nodes']['same/id']
        left['nextNodes'], right['nextNodes'] = [edge('join')], [edge('join')]
        join = nodes.pop('join')
        join.update(leftNodeId='left', rightNodeId='right')
        nodes['box'] = container({'left': left, 'right': right, 'join': join}, 'out')
        join['nextNodes'] = []
        nodes['left_input']['nextNodes'] = [edge('left')]
        nodes['right_input']['nextNodes'] = [edge('right')]
        self.assertIn('JoinKind.LeftOuter', self.parse(flow)[-1]['m_query_override'])
        nodes['left_input']['nextNodes'] = [edge('box')]
        with self.assertRaisesRegex(ValueError, 'ambiguous entry boundary'):
            self.parse(flow)

    def test_branched_container_union_keeps_both_transformations(self):
        flow = {
            'nodes': {
                'input': source('box'),
                'box': container({
                    'root': operation('SuperTransform'),
                    'left': operation('AddColumn', 'union', name='LeftBranch',
                                      columnName='L', expression='1'),
                    'right': operation('AddColumn', 'union', name='RightBranch',
                                       columnName='R', expression='2'),
                    'union': operation('Union'),
                }, 'out'),
                'out': output(),
            },
        }
        flow['nodes']['box']['loomContainer']['nodes']['root']['nextNodes'] = [
            edge('left'), edge('right'),
        ]
        datasources = self.parse(flow)
        self.assertEqual({ds['name'] for ds in datasources},
                         {'prep.LeftBranch', 'prep.RightBranch', 'prep.PublishedSales'})
        query = next(ds['m_query_override'] for ds in datasources
                     if ds['name'] == 'prep.PublishedSales')
        self.assertIn('Table.Combine', query)
        self.assertIn('LeftBranch', query)
        self.assertIn('RightBranch', query)
        for name, column in [('LeftBranch', 'L'), ('RightBranch', 'R')]:
            branch = next(ds for ds in datasources if ds['name'] == 'prep.' + name)
            self.assertIn('"' + column + '"', branch['m_query_override'])

    def test_container_ambiguous_exit_and_stranded_branch_rejected(self):
        flow = {'nodes': {
            'input': source('box'),
            'box': container({
                'root': operation('SuperTransform'),
                'a': operation('RemoveColumn', columnName='Discard'),
                'b': operation('RenameColumn', columnName='Raw', newColumnName='Kept'),
            }, 'out'),
            'out': output(),
        }}
        box = flow['nodes']['box']
        box['loomContainer']['nodes']['root']['nextNodes'] = [edge('a'), edge('b')]
        with self.assertRaisesRegex(ValueError, 'ambiguous exit boundary'):
            normalize_prep_flow(flow)
        box['exitNodeId'] = 'a'
        with self.assertRaisesRegex(ValueError, 'branch stranded at exit'):
            normalize_prep_flow(flow)

    def test_explicit_boundary_aliases_route_without_reordering(self):
        for entry_key, exit_key in [
            ('entryNodeId', 'exitNodeId'),
            ('inputNodes', 'outputNodes'),
            ('initialNodeIds', 'terminalNodeIds'),
        ]:
            with self.subTest(entry=entry_key):
                flow = clean_chain()
                loom = flow['nodes']['box']['loomContainer']
                loom[entry_key] = {'nodeId': 'rename'}
                loom[exit_key] = {'nodeId': 'filter'}
                self.assert_actual_chain(self.parse(flow)[0]['m_query_override'])

    def test_outer_container_actions_cannot_be_silently_discarded(self):
        flow = clean_chain()
        flow['nodes']['box']['actions'] = [
            {'actionType': 'RemoveColumn', 'columnName': 'Raw'},
        ]
        with self.assertRaisesRegex(ValueError, 'outer actions'):
            normalize_prep_flow(flow)

    def test_known_output_versions_with_and_without_base_type(self):
        for kind in ['PublishExtract', 'SaveToFile', 'SaveToDatabase']:
            for base_type in [False, True]:
                with self.subTest(kind=kind, base_type=base_type):
                    node = output(base_type=base_type)
                    node['nodeType'] = '.v2030_2.' + kind
                    self.assertEqual(get_prep_node_kind(node), 'output')
                    result = self.parse({'nodes': {'in': source('out'), 'out': node}})
                    self.assertEqual([ds['name'] for ds in result], ['prep.PublishedSales'])

    def test_kind_never_guesses_unknown_load_or_publish_suffix(self):
        for kind in ['LoadFuture', 'PublishFuture', 'FutureStep']:
            with self.subTest(kind=kind):
                self.assertEqual(get_prep_node_kind(operation(kind)), 'transform')
        self.assertEqual(get_prep_node_kind({
            'baseType': 'output', 'nodeType': '.v99.CustomSink',
        }), 'output')
        with self.assertRaisesRegex(ValueError, 'Unknown Prep baseType'):
            get_prep_node_kind({'baseType': 'future'})

    def test_nullable_optional_collections_convert(self):
        flow = {'nodes': {
            'input': source('clean'),
            'clean': operation('SuperTransform', 'out', fields=None,
                               connectionAttributes=None, actions=None,
                               beforeActionGroup=None,
                               actionGroup={'actions': None}, afterActionGroup=None),
            'out': {**output(), 'fields': None, 'nextNodes': None,
                    'connectionAttributes': None},
        }, 'connections': None}
        flow['nodes']['input']['fields'] = None
        flow['nodes']['input']['connectionAttributes']['server'] = None
        before = deepcopy(flow)
        normalized = normalize_prep_flow(flow)
        self.assertEqual(normalized['connections'], {})
        self.assertEqual(normalized['nodes']['clean']['fields'], [])
        self.assertEqual(normalized['nodes']['clean']['actionGroup'], {'actions': []})
        self.assertNotIn('server', normalized['nodes']['input']['connectionAttributes'])
        self.assertTrue(self.parse(flow)[0]['m_query_override'].strip())
        self.assertEqual(flow, before)

    def test_container_local_connection_is_scoped(self):
        flow = {'connections': {'c': {'connectionAttributes': {
            'class': 'csv', 'filename': 'outer.csv',
        }}}, 'nodes': {
            'box': container({
                'in': operation('LoadCsv', 'out', name='LocalInput', connectionId='c'),
                'out': output(),
            }),
        }}
        flow['nodes']['box']['loomContainer']['connections'] = {
            'c': {'connectionAttributes': {'class': 'csv', 'filename': 'inner.csv'}},
        }
        normalized = normalize_prep_flow(flow)
        inner = next(n for n in normalized['nodes'].values() if n['name'] == 'LocalInput')
        self.assertNotEqual(inner['connectionId'], 'c')
        self.assertEqual(normalized['connections'][inner['connectionId']]
                         ['connectionAttributes']['filename'], 'inner.csv')
        self.assertEqual(normalized['connections']['c']['connectionAttributes']
                         ['filename'], 'outer.csv')
        query = self.parse(flow)[0]['m_query_override']
        self.assertIn('inner.csv', query)
        self.assertNotIn('outer.csv', query)

    def test_nullable_connection_definitions_and_container_collections(self):
        flow = clean_chain()
        flow['connections'] = {'unused': None, 'optional': {'connectionAttributes': None}}
        loom = flow['nodes']['box']['loomContainer']
        loom['connections'] = None
        flow['nodes']['box']['fields'] = None
        flow['nodes']['box']['actions'] = None
        before = deepcopy(flow)
        normalized = normalize_prep_flow(flow)
        self.assertEqual(normalized['connections'], {
            'unused': {}, 'optional': {'connectionAttributes': {}},
        })
        self.assert_actual_chain(self.parse(flow)[0]['m_query_override'])
        self.assertEqual(flow, before)

    def test_scoped_ids_cannot_collide_with_existing_top_level_ids(self):
        flow = clean_chain()
        reserved = '__prep_scope__/box/rename'
        flow['nodes'][reserved] = source('out', name='ExtraInput')
        # This additional branch would make the output invalid, so only test the
        # normalizer's ID allocation; parsing validates output cardinality.
        before = deepcopy(flow)
        normalized = normalize_prep_flow(flow)
        self.assertIn(reserved, normalized['nodes'])
        rename_ids = [nid for nid, n in normalized['nodes'].items()
                      if n['nodeType'].endswith('.RenameColumn')]
        self.assertEqual(len(rename_ids), 1)
        self.assertNotEqual(rename_ids[0], reserved)
        self.assertEqual(len(normalized['nodes']), 8)
        self.assertEqual(normalized, normalize_prep_flow(flow))
        self.assertEqual(flow, before)

    def test_unresolved_explicit_output_blocks_leaf_fallback(self):
        nodes = {'input': source(), 'out': output()}
        results = {'input': {
            'name': 'SyntheticSales', 'm_query': 'let Source = 1 in Source',
            'fields': [], 'table': {}, 'connection': {},
        }}
        with self.assertRaisesRegex(ValueError, 'explicit outputs have no resolved result'):
            _collect_prep_datasources(['input', 'out'], nodes, results, set())

    def test_malformed_shapes_fail_instead_of_passthrough(self):
        cases = [
            ('fields', {}), ('connectionAttributes', []), ('nextNodes', 'out'),
            ('beforeActionGroup', []), ('actions', {}),
            ('fields', [None]), ('nodeType', 17),
        ]
        for key, bad_value in cases:
            with self.subTest(key=key, value=bad_value):
                flow = clean_chain()
                flow['nodes']['input'][key] = bad_value
                with self.assertRaisesRegex(ValueError, 'Prep node'):
                    normalize_prep_flow(flow)
        for connections in [[], 'bad']:
            with self.subTest(connections=connections):
                with self.assertRaisesRegex(ValueError, 'nodes/connections'):
                    normalize_prep_flow({'nodes': {}, 'connections': connections})

    def test_unknown_or_incomplete_operations_fail(self):
        for node in [
            operation('FutureTransform'),
            operation('RenameColumn', columnName='Raw'),
            operation('ChangeColumnType', columnName='Raw', newType='future'),
            operation('AddColumn', columnName='MissingExpression'),
            operation('FilterOperation'),
            operation('SuperTransform', actions=[{'actionType': 'FutureAction'}]),
        ]:
            with self.subTest(kind=node['nodeType']):
                node['nextNodes'] = [edge('out')]
                with self.assertRaises(ValueError):
                    self.parse({'nodes': {
                        'input': source('bad'), 'bad': node, 'out': output(),
                    }})

    def test_disconnected_unresolved_and_multi_upstream_outputs_fail(self):
        flows = [
            {'nodes': {'input': source(), 'out': output()}},
            {'nodes': {'input': source('out'), 'other': source('out'), 'out': output()}},
        ]
        for flow in flows:
            with self.subTest(flow=flow):
                with self.assertRaisesRegex(ValueError, 'expected one resolved upstream'):
                    self.parse(flow)
        nodes = {'input': source('out'), 'out': output()}
        with self.assertRaisesRegex(ValueError, 'expected one resolved upstream'):
            _process_prep_node('out', nodes, {}, {}, set())

    def test_cycle_and_dangling_edges_rejected(self):
        for flow in [
            {'nodes': {'input': source('missing')}},
            {'nodes': {'a': operation('SuperTransform', 'b'),
                       'b': operation('SuperTransform', 'a')}},
        ]:
            with self.subTest(flow=flow):
                with self.assertRaisesRegex(ValueError, 'dangling|cycl'):
                    self.parse(flow)

    def test_json_and_zip_archives_have_identical_normalized_graph_and_m(self):
        flow = clean_chain(nested=True)
        with tempfile.TemporaryDirectory() as directory:
            expected_graph = normalize_prep_flow(flow)
            expected_queries = None
            for filename, member in [
                ('plain.tfl', None), ('packed.tfl', 'flow'),
                ('packed.tflx', 'flow'), ('nested.tflx', 'Flow/synthetic.tfl'),
                ('nested.tfl', 'Flow/synthetic.tfl'),
            ]:
                with self.subTest(filename=filename, member=member):
                    path = write_flow(directory, flow, filename, member)
                    self.assertEqual(read_prep_flow(path), expected_graph)
                    queries = [ds['m_query_override'] for ds in parse_prep_flow(path)]
                    if expected_queries is None:
                        expected_queries = queries
                    self.assertEqual(queries, expected_queries)
                    self.assert_actual_chain(queries[0])


if __name__ == '__main__':
    unittest.main()
