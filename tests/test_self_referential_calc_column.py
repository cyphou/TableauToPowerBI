"""A calculated column may not reference a column of its own name.

Column names are unique within a table, so such a reference can only be the
column itself and Power BI refuses to load the model with a circular
dependency.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from powerbi_import.tmdl_self_heal import _self_heal_model
from powerbi_import.tmdl_writers import _quote_name


def _model(columns):
    return {'model': {'tables': [{
        'name': 'Sales',
        'columns': columns,
        'measures': [],
        'partitions': [],
    }]}}


class TestSelfReferentialCalcColumn(unittest.TestCase):

    def _cols(self, model):
        return model['model']['tables'][0]['columns']

    def test_qualified_self_reference_is_blanked(self):
        model = _model([
            {'name': 'Amount', 'dataType': 'double',
             'expression': "-'Sales'[Amount]", 'isCalculated': True},
        ])
        _self_heal_model(model)
        col = [c for c in self._cols(model) if c.get('expression')][0]
        self.assertEqual(col['expression'], 'BLANK()')
        self.assertTrue(col.get('isHidden'))

    def test_bare_self_reference_is_blanked(self):
        model = _model([
            {'name': 'Amount', 'dataType': 'double',
             'expression': '-[Amount]', 'isCalculated': True},
        ])
        _self_heal_model(model)
        col = [c for c in self._cols(model) if c.get('expression')][0]
        self.assertEqual(col['expression'], 'BLANK()')

    def test_no_placeholder_is_created_for_the_columns_own_name(self):
        model = _model([
            {'name': 'Amount', 'dataType': 'double',
             'expression': "-'Sales'[Amount]", 'isCalculated': True},
        ])
        _self_heal_model(model)
        self.assertEqual([c['name'] for c in self._cols(model)], ['Amount'])

    def test_reference_to_a_different_column_is_untouched(self):
        model = _model([
            {'name': 'Net', 'dataType': 'double',
             'expression': "-'Sales'[Gross]", 'isCalculated': True},
        ])
        _self_heal_model(model)
        col = [c for c in self._cols(model) if c.get('name') == 'Net'][0]
        self.assertEqual(col['expression'], "-'Sales'[Gross]")
        # the missing target still gets its placeholder
        self.assertIn('Gross', [c['name'] for c in self._cols(model)])


class TestNameWhitespaceIsIdentity(unittest.TestCase):
    """Tableau allows 'X ' and 'X' to coexist; trimming merged them and made
    a calculated column reference itself."""

    def test_trailing_space_is_preserved(self):
        self.assertEqual(_quote_name('Montant Articles '),
                         "'Montant Articles '")

    def test_trimmed_and_untrimmed_names_stay_distinct(self):
        self.assertNotEqual(_quote_name('Montant Articles '),
                            _quote_name('Montant Articles'))

    def test_newlines_are_still_normalised(self):
        self.assertNotIn('\n', _quote_name('Bad\nName'))

    def test_plain_identifier_is_unquoted(self):
        self.assertEqual(_quote_name('Amount'), 'Amount')


if __name__ == '__main__':
    unittest.main()
