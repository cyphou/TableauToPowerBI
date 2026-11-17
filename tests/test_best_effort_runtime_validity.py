"""Synthetic runtime-validity regressions for best-effort model repairs."""

import unittest

from powerbi_import.tmdl_dax_postprocess import _iterate_row_expressions_over_joins
from powerbi_import.tmdl_self_heal import (
    _is_numeric_use,
    _placeholder_data_type,
    _widen_numeric_placeholders,
)


def _placeholder(name, data_type='string'):
    return {
        'name': name,
        'dataType': data_type,
        'expression': 'BLANK()',
        'isCalculated': True,
        'isHidden': True,
        'annotations': [{
            'name': 'MigrationNote',
            'value': 'Self-heal: placeholder column created to satisfy a missing reference.',
        }],
    }


class TestPlaceholderTypedFromUse(unittest.TestCase):
    def test_numeric_aggregate_types_first_placeholder(self):
        consumer = {'name': 'Total', 'expression': "SUM('Sales'[Missing])"}
        numeric_use = _is_numeric_use(
            consumer, 'Missing', placeholder_table='Sales', consumer_table='Sales')
        self.assertTrue(numeric_use)
        self.assertEqual(_placeholder_data_type(consumer, numeric_use=numeric_use), 'double')

    def test_numeric_arithmetic_types_placeholder(self):
        consumer = {
            'name': 'Adjusted', 'datatype': 'real',
            'expression': "('Sales'[Missing] * 2) + 'Sales'[Amount]",
        }
        self.assertTrue(_is_numeric_use(
            consumer, 'Missing', placeholder_table='Sales', consumer_table='Sales'))
        self.assertEqual(_placeholder_data_type(consumer, numeric_use=True), 'double')

    def test_string_comparison_does_not_type_placeholder_numeric(self):
        consumer = {
            'name': 'Label Match', 'datatype': 'string',
            'expression': "'Sales'[Missing] = \"North\"",
        }
        self.assertFalse(_is_numeric_use(
            consumer, 'Missing', placeholder_table='Sales', consumer_table='Sales'))
        self.assertEqual(_placeholder_data_type(consumer), 'string')

    def test_arithmetic_on_string_output_does_not_type_placeholder_numeric(self):
        consumer = {
            'name': 'Joined Label', 'datatype': 'string',
            'expression': "'Sales'[Missing] + \" label\"",
        }
        self.assertFalse(_is_numeric_use(
            consumer, 'Missing', placeholder_table='Sales', consumer_table='Sales'))

    def test_qualified_reference_to_another_table_is_not_a_local_consumer(self):
        consumer = {'name': 'Total', 'expression': "SUM('Other'[Missing])"}
        self.assertFalse(_is_numeric_use(
            consumer, 'Missing', placeholder_table='Sales', consumer_table='Sales'))

    def test_later_numeric_consumer_widens_existing_placeholder(self):
        missing = _placeholder('Missing')
        model = {'model': {'tables': [
            {'name': 'Sales', 'columns': [missing], 'measures': [
                {'name': 'Display', 'expression': "'Sales'[Missing] = \"N/A\""},
                {'name': 'Total', 'expression': "SUM('Sales'[Missing])"},
            ]},
        ]}}
        self.assertEqual(_widen_numeric_placeholders(model), 1)
        self.assertEqual(missing['dataType'], 'double')

    def test_nonnumeric_placeholder_remains_text(self):
        missing = _placeholder('Missing')
        model = {'model': {'tables': [
            {'name': 'Sales', 'columns': [missing], 'measures': [
                {'name': 'Display', 'expression': "'Sales'[Missing] = \"N/A\""},
            ]},
        ]}}
        self.assertEqual(_widen_numeric_placeholders(model), 0)
        self.assertEqual(missing['dataType'], 'string')

    def test_real_calculated_column_is_never_retyped(self):
        real_column = {
            'name': 'Amount', 'dataType': 'string', 'expression': '"n/a"',
            'isCalculated': True,
        }
        model = {'model': {'tables': [
            {'name': 'Sales', 'columns': [real_column], 'measures': [
                {'name': 'Total', 'expression': "SUM('Sales'[Amount])"},
            ]},
        ]}}
        self.assertEqual(_widen_numeric_placeholders(model), 0)
        self.assertEqual(real_column['dataType'], 'string')


class TestCalculatedColumnSourceFormula(unittest.TestCase):
    def test_dax_calculated_column_keeps_original_tableau_formula(self):
        from powerbi_import.tmdl_generator import _build_table

        source_formula = '[Amount] * [Units]'
        result = _build_table(
            {'name': 'Facts', 'columns': [
                {'name': 'Amount', 'datatype': 'real'},
                {'name': 'Units', 'datatype': 'real'},
            ]},
            {'type': 'sqlserver', 'server': 'synthetic.invalid',
             'database': 'synthetic'},
            [{'name': '[LineTotal]', 'caption': 'LineTotal',
              'formula': source_formula, 'role': 'dimension', 'datatype': 'real'}],
            [],
            dax_context={
                'calc_map': {}, 'param_map': {},
                'column_table_map': {'Amount': 'Facts', 'Units': 'Facts'},
                'measure_names': set(), 'param_values': {},
            },
            col_metadata_map={},
        )
        column = next(col for col in result['columns'] if col['name'] == 'LineTotal')
        self.assertEqual(column['_original_formula'], source_formula)
        self.assertTrue(column.get('isCalculated') or column.get('sourceColumn'))


def _joined_row_model(join_type='left', relations=None):
    return {'model': {'tables': [
        {'name': 'A', 'columns': [
            {'name': 'Id'}, {'name': 'Amount'}], 'measures': [{
                'name': 'Total',
                'expression': (
                    '/* Unaggregated row expression best-effort approximation; '
                    'row scope, shelf aggregation and total grain unverified */ '
                    "SUMX('A', 'A'[Amount] * 'B'[Rate])"),
            }]},
        {'name': 'B', 'columns': [{'name': 'AId'}, {'name': 'Rate'}], 'measures': []},
    ], 'relationships': relations if relations is not None else [{
        'name': 'Relationship-1', 'fromTable': 'A', 'fromColumn': 'Id',
        'toTable': 'B', 'toColumn': 'AId', 'joinType': join_type,
        'isActive': True,
    }]}}


class TestRowExpressionOverJoinedRows(unittest.TestCase):
    def test_left_join_expands_row_domain_from_explicit_keys(self):
        model = _joined_row_model()
        changed = _iterate_row_expressions_over_joins(model)
        expression = model['model']['tables'][0]['measures'][0]['expression']
        self.assertEqual(changed, 1)
        self.assertIn(
            "SUMX(GENERATEALL('A', FILTER('B', 'B'[AId] = 'A'[Id]))",
            expression,
        )
        self.assertIn('with B; left;', expression)
        self.assertTrue(any(
            annotation.get('name') == 'MigrationNote'
            for annotation in model['model']['tables'][0]['measures'][0]['annotations']
        ))

    def test_inner_join_uses_generate(self):
        model = _joined_row_model('inner')
        self.assertEqual(_iterate_row_expressions_over_joins(model), 1)
        expression = model['model']['tables'][0]['measures'][0]['expression']
        self.assertIn("SUMX(GENERATE('A', FILTER('B'", expression)

    def test_unmatched_relationship_leaves_row_iterator_unchanged(self):
        model = _joined_row_model(relations=[])
        original = model['model']['tables'][0]['measures'][0]['expression']
        self.assertEqual(_iterate_row_expressions_over_joins(model), 0)
        self.assertEqual(model['model']['tables'][0]['measures'][0]['expression'], original)

    def test_ambiguous_relationships_leave_row_iterator_unchanged(self):
        relations = [
            {'name': 'Relationship-1', 'fromTable': 'A', 'fromColumn': 'Id',
             'toTable': 'B', 'toColumn': 'AId', 'joinType': 'left', 'isActive': True},
            {'name': 'Relationship-2', 'fromTable': 'A', 'fromColumn': 'OtherId',
             'toTable': 'B', 'toColumn': 'OtherAId', 'joinType': 'left', 'isActive': True},
        ]
        model = _joined_row_model(relations=relations)
        original = model['model']['tables'][0]['measures'][0]['expression']
        self.assertEqual(_iterate_row_expressions_over_joins(model), 0)
        self.assertEqual(model['model']['tables'][0]['measures'][0]['expression'], original)

    def test_aggregation_consumed_foreign_reference_does_not_trigger_join(self):
        model = _joined_row_model()
        measure = model['model']['tables'][0]['measures'][0]
        measure['expression'] = (
            "SUMX('A', 'A'[Amount] * SUM('B'[Rate]))")
        self.assertEqual(_iterate_row_expressions_over_joins(model), 0)
        self.assertEqual(measure['expression'], "SUMX('A', 'A'[Amount] * SUM('B'[Rate]))")

    def test_pass_is_idempotent(self):
        model = _joined_row_model()
        self.assertEqual(_iterate_row_expressions_over_joins(model), 1)
        expression = model['model']['tables'][0]['measures'][0]['expression']
        self.assertEqual(_iterate_row_expressions_over_joins(model), 0)
        self.assertEqual(model['model']['tables'][0]['measures'][0]['expression'], expression)


if __name__ == '__main__':
    unittest.main()