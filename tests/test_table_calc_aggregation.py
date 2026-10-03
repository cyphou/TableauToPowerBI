"""A Tableau quick table calc must aggregate the way its column allows.

A "% of Total" over a text column produced DIVIDE(SUM([siret]), ...). SUM is
invalid on text in DAX, so the measure never evaluated and Power BI Desktop
showed the visual as "This might be caused by a capacity or license issue" --
a message that names neither the measure nor the column.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'tableau_export'))

from powerbi_import.tmdl_generator import (_agg_for_column,
                                           _create_quick_table_calc_measures)


def _model(data_type):
    return {'model': {'tables': [{
        'name': 'Sales',
        'columns': [{'name': 'siret', 'dataType': data_type},
                    {'name': 'Amount', 'dataType': 'double'}],
        'measures': [],
    }]}}


def _worksheets(field_name, agg='sum'):
    return [{'fields': [{'name': field_name, 'table_calc': 'pcto',
                         'table_calc_agg': agg}]}]


def _measure_named(model, prefix):
    for table in model['model']['tables']:
        for measure in table.get('measures', []):
            if measure['name'].startswith(prefix):
                return measure
    return None


class TestAggregationMatchesColumnType(unittest.TestCase):

    def test_sum_over_text_becomes_a_count(self):
        self.assertEqual(_agg_for_column(_model('string'), 'Sales', 'siret', 'SUM'),
                         'COUNTA')

    def test_average_over_text_becomes_a_count(self):
        self.assertEqual(_agg_for_column(_model('string'), 'Sales', 'siret', 'AVERAGE'),
                         'COUNTA')

    def test_sum_over_a_number_is_left_alone(self):
        self.assertEqual(_agg_for_column(_model('double'), 'Sales', 'Amount', 'SUM'),
                         'SUM')

    def test_min_and_max_are_legal_on_text_and_stay(self):
        for agg in ('MIN', 'MAX', 'DISTINCTCOUNT', 'COUNT'):
            with self.subTest(agg=agg):
                self.assertEqual(
                    _agg_for_column(_model('string'), 'Sales', 'siret', agg), agg)

    def test_an_unknown_column_keeps_the_requested_aggregation(self):
        self.assertEqual(_agg_for_column(_model('string'), 'Sales', 'absent', 'SUM'),
                         'SUM')

    def test_percent_of_total_over_text_never_emits_sum(self):
        model = _model('string')
        _create_quick_table_calc_measures(model, _worksheets('siret'), 'Sales',
                                          {'siret': 'Sales'})
        measure = _measure_named(model, '% of Total siret')
        self.assertIsNotNone(measure)
        self.assertNotIn('SUM(', measure['expression'])
        self.assertIn("COUNTA('Sales'[siret])", measure['expression'])

    def test_percent_of_total_over_a_number_still_sums(self):
        model = _model('double')
        _create_quick_table_calc_measures(model, _worksheets('Amount'), 'Sales',
                                          {'Amount': 'Sales'})
        measure = _measure_named(model, '% of Total Amount')
        self.assertIsNotNone(measure)
        self.assertIn("SUM('Sales'[Amount])", measure['expression'])


if __name__ == '__main__':
    unittest.main()
