"""Synthetic runtime-validity regressions for best-effort model repairs."""

import unittest
import xml.etree.ElementTree as ET

from powerbi_import.tmdl_dax_postprocess import (
    _iterate_row_expressions_over_joins,
    _unwrap_aggregations_of_measures,
)
from powerbi_import.tmdl_relationships import DeclaredRowJoin, declared_row_joins
from powerbi_import.tmdl_self_heal import (
    _is_numeric_use,
    _placeholder_data_type,
    _widen_numeric_placeholders,
)
from tableau_export.dax_converter import convert_tableau_formula_to_dax
from tests.test_reported_runtime_acceptance import _source_xml as acceptance_source_xml
from tests.test_reported_runtime_acceptance import extract_datasource


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

    def test_a_relationship_declared_from_the_other_table_is_used_in_reverse(self):
        relations = [{'name': 'Relationship-1', 'fromTable': 'B', 'fromColumn': 'AId',
                      'toTable': 'A', 'toColumn': 'Id', 'isActive': True}]
        model = _joined_row_model(relations=relations)

        self.assertEqual(_iterate_row_expressions_over_joins(model), 1)

        self.assertIn("SUMX(GENERATEALL('A', FILTER('B', 'B'[AId] = 'A'[Id]))",
                      model['model']['tables'][0]['measures'][0]['expression'])

    def test_an_inferred_relationship_is_the_warned_last_resort(self):
        relations = [{'name': 'inferred_A_B', 'fromTable': 'A', 'fromColumn': 'Id',
                      'toTable': 'B', 'toColumn': 'AId'}]
        model = _joined_row_model(relations=relations)

        self.assertEqual(_iterate_row_expressions_over_joins(model), 1)

        measure = model['model']['tables'][0]['measures'][0]
        self.assertIn("GENERATEALL('A', FILTER('B', 'B'[AId] = 'A'[Id]))",
                      measure['expression'])
        self.assertIn('inferred from column names; join keys and join type unverified',
                      measure['_migration_note'])


class TestRowExpressionOverDeclaredJoin(unittest.TestCase):
    """The Tableau physical join is the source row; a relationship is a fallback."""

    def _declared(self, join_type='left',
                  condition="'B'[AId] = 'A'[Id] && 'A'[Tag] = 'B'[Tag]", omitted=0):
        join = DeclaredRowJoin('A', 'B', join_type, condition, omitted)
        return {('A', 'B'): join, ('B', 'A'): join}

    def test_the_declared_join_wins_over_a_relationship(self):
        model = _joined_row_model()

        self.assertEqual(_iterate_row_expressions_over_joins(model, self._declared()), 1)

        measure = model['model']['tables'][0]['measures'][0]
        self.assertIn(
            "GENERATEALL('A', FILTER('B', 'B'[AId] = 'A'[Id] && 'A'[Tag] = 'B'[Tag]))",
            measure['expression'])
        self.assertIn('on the declared Tableau left join', measure['_migration_note'])

    def test_untranslated_join_conditions_are_said(self):
        model = _joined_row_model(relations=[])

        _iterate_row_expressions_over_joins(model, self._declared(omitted=1))

        self.assertIn('1 join condition(s) not translated, omitted',
                      model['model']['tables'][0]['measures'][0]['_migration_note'])

    def test_iterating_the_right_side_of_a_left_join_keeps_matched_rows_only(self):
        model = _joined_row_model(relations=[])
        model['model']['tables'][0]['name'], model['model']['tables'][1]['name'] = 'B', 'A'
        measure = model['model']['tables'][0]['measures'][0]
        measure['expression'] = "SUMX('B', 'B'[Rate] * 'A'[Amount])"
        model['model']['tables'][0]['columns'] = [{'name': 'AId'}, {'name': 'Rate'}]
        model['model']['tables'][1]['columns'] = [{'name': 'Id'}, {'name': 'Amount'}]

        self.assertEqual(_iterate_row_expressions_over_joins(model, self._declared()), 1)

        self.assertIn("SUMX(GENERATE('B', FILTER('A', ", measure['expression'])


class TestDeclaredRowJoinsFromTheExtractor(unittest.TestCase):
    """``declared_row_joins`` reads the predicate the extractor keeps."""

    @classmethod
    def setUpClass(cls):
        cls.joins = declared_row_joins(
            [extract_datasource(acceptance_source_xml())], ['Housing', 'Households'])

    def test_every_equality_of_the_predicate_becomes_a_condition(self):
        join = self.joins[('Housing', 'Households')]

        self.assertEqual((join.left_table, join.right_table, join.join_type),
                         ('Housing', 'Households', 'left'))
        self.assertEqual(join.omitted_conditions, 0)
        self.assertEqual(join.condition.count(' && '), 2)
        self.assertIn("'Housing'[Zone] = 'Households'[ZoneCode]", join.condition)

    def test_a_calculated_side_saved_as_if_without_space_is_translated(self):
        condition = self.joins[('Housing', 'Households')].condition

        self.assertIn("'Housing'[Status] = (IF('Households'[Heating] = \"district\"", condition)
        self.assertNotIn('TODO', condition)

    def test_a_condition_mixing_both_tables_on_one_side_is_counted_not_dropped(self):
        source = acceptance_source_xml()
        conjunction = source.find('.//clause/expression')
        equality = ET.SubElement(conjunction, 'expression', op='=')
        ET.SubElement(equality, 'expression', op='[Housing].[Sector]')
        ET.SubElement(equality, 'expression',
                      op='[Households].[Heating] + [Housing].[Zone]')

        join = declared_row_joins([extract_datasource(source)],
                                  ['Housing', 'Households'])[('Housing', 'Households')]

        self.assertEqual(join.omitted_conditions, 1)
        self.assertEqual(join.condition.count(' && '), 2)


class TestAggregateOverDiagnosticMeasure(unittest.TestCase):
    def test_an_aggregate_over_a_blank_diagnostic_measure_is_still_lowered(self):
        table = {'name': 'Housing', 'columns': [{'name': 'Homes'}], 'measures': [
            {'name': 'Unsure', 'expression': '/* TODO: DAX conversion validation failed for '
                                             'measure: FIXED requires evidence */ BLANK()'},
            {'name': 'Scaled', 'expression': "MIN([Unsure]) * MIN('Housing'[Homes])"},
        ]}

        _unwrap_aggregations_of_measures(table)

        scaled = table['measures'][1]['expression']
        self.assertIn("MINX('Housing', [Unsure]) * MIN('Housing'[Homes])", scaled)
        self.assertNotRegex(scaled, r'\bMIN\(\s*\[Unsure\]\s*\)')


class TestFixedRatioOperands(unittest.TestCase):
    """The ratio keeps operands it used to refuse, and names them in the warning."""

    def _ratio(self, formula, **context):
        return convert_tableau_formula_to_dax(
            formula, table_name='Housing', table_columns=['Sector', 'Usage'],
            fixed_shelf_aggregation='SUM', **context)

    def test_an_operand_of_another_table_is_qualified_with_that_table(self):
        dax = self._ratio('SUM({ FIXED [Sector] : SUM([Usage]) / SUM([Weight]) })',
                          column_table_map={'Sector': 'Housing', 'Usage': 'Housing',
                                            'Weight': 'Households'})

        self.assertIn("DIVIDE(SUM('Housing'[Usage]), SUM('Households'[Weight]))", dax)
        self.assertIn('cross-table operand(s) Weight; relationship propagation unverified', dax)

    def test_an_operand_absent_from_the_schema_is_kept_as_a_column(self):
        dax = self._ratio('SUM({ FIXED [Sector] : SUM([Usage]) / SUM([Ghost]) })',
                          column_table_map={'Sector': 'Housing', 'Usage': 'Housing'})

        self.assertIn("DIVIDE(SUM('Housing'[Usage]), SUM('Housing'[Ghost]))", dax)
        self.assertIn('missing schema operand(s) Ghost referenced as columns', dax)

    def test_a_calculation_of_this_table_not_generated_yet_is_aggregated_as_a_measure(self):
        dax = self._ratio('SUM({ FIXED [Sector] : AVG([Later]) / SUM([Usage]) })',
                          column_table_map={'Sector': 'Housing', 'Usage': 'Housing',
                                            'Later': 'Housing'})

        self.assertIn("DIVIDE(AVERAGEX('Housing', [Later]), SUM('Housing'[Usage]))", dax)
        self.assertIn('AVERAGE over calculation Later iterated with AVERAGEX', dax)
        self.assertNotIn('TODO', dax)


if __name__ == '__main__':
    unittest.main()