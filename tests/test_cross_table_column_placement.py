"""A calculated column cannot read a bare column from another table.

Power BI rejects the model with "A single value for column X in table Y cannot
be determined". Two generators produced that shape: a Tableau group or bin was
always added to the main table even when its source column lived elsewhere,
and a self-heal placeholder was declared as a physical column bound to source
data that no partition produces.
"""
import unittest

from powerbi_import.tmdl_self_heal import _self_heal_model
from powerbi_import.tmdl_sets import _process_sets_groups_bins


def _model(*tables):
    return {'model': {'tables': list(tables), 'relationships': []}}


def _table(name, columns=(), measures=()):
    return {
        'name': name,
        'columns': [dict(c) for c in columns],
        'measures': [dict(m) for m in measures],
        'partitions': [{
            'name': name,
            'mode': 'import',
            'source': {
                'type': 'm',
                'expression': (
                    'let\n'
                    '    Source = Csv.Document(File.Contents("d.csv")),\n'
                    '    #"Promoted Headers" = Table.PromoteHeaders(Source)\n'
                    'in\n'
                    '    #"Promoted Headers"'
                ),
            },
        }],
    }


def _columns(table):
    return {c['name']: c for c in table['columns']}


class TestGroupsLandOnTheOwningTable(unittest.TestCase):

    def _run(self, extra, column_table_map):
        main = _table('Facts', [{'name': 'Amount'}])
        other = _table('Invoices', [{'name': 'Label'}, {'name': 'Total'}])
        model = _model(main, other)
        _process_sets_groups_bins(model, extra, 'Facts', column_table_map)
        return main, other

    def test_group_is_added_to_the_table_owning_its_source(self):
        extra = {'groups': [{
            'name': 'Label (group)', 'group_type': 'values',
            'source_field': 'Label',
            'members': {'A': ['x'], 'B': ['y']},
        }]}
        main, other = self._run(extra, {'Label': 'Invoices'})
        self.assertIn('Label (group)', _columns(other))
        self.assertNotIn('Label (group)', _columns(main))

    def test_group_on_a_main_table_column_stays_put(self):
        extra = {'groups': [{
            'name': 'Amount (group)', 'group_type': 'values',
            'source_field': 'Amount',
            'members': {'A': ['1'], 'B': ['2']},
        }]}
        main, other = self._run(extra, {'Amount': 'Facts'})
        self.assertIn('Amount (group)', _columns(main))
        self.assertNotIn('Amount (group)', _columns(other))

    def test_group_never_reaches_across_tables(self):
        extra = {'groups': [{
            'name': 'Label (group)', 'group_type': 'values',
            'source_field': 'Label',
            'members': {'A': ['x']},
        }]}
        _main, other = self._run(extra, {'Label': 'Invoices'})
        col = _columns(other)['Label (group)']
        # The recode lives either in DAX or in the partition M, but whichever
        # it is, it must not name another table.
        recode = col.get('expression') or \
            other['partitions'][0]['source']['expression']
        self.assertIn('Label (group)', recode)
        self.assertNotIn("'Facts'", recode)

    def test_bin_is_added_to_the_table_owning_its_source(self):
        extra = {'bins': [{'name': 'Total (bin)', 'source_field': 'Total',
                           'size': '10'}]}
        main, other = self._run(extra, {'Total': 'Invoices'})
        self.assertIn('Total (bin)', _columns(other))
        self.assertNotIn('Total (bin)', _columns(main))

    def test_unknown_source_falls_back_to_the_main_table(self):
        extra = {'bins': [{'name': 'Ghost (bin)', 'source_field': 'Ghost',
                           'size': '5'}]}
        main, other = self._run(extra, {})
        self.assertIn('Ghost (bin)', _columns(main))
        self.assertNotIn('Ghost (bin)', _columns(other))

    def test_m_steps_reach_the_owning_partition(self):
        extra = {'groups': [{
            'name': 'Label (group)', 'group_type': 'values',
            'source_field': 'Label',
            'members': {'A': ['x']},
        }]}
        main, other = self._run(extra, {'Label': 'Invoices'})
        other_m = other['partitions'][0]['source']['expression']
        main_m = main['partitions'][0]['source']['expression']
        self.assertIn('Label (group)', other_m)
        self.assertNotIn('Label (group)', main_m)


class TestPlaceholderColumnsClaimNoSourceData(unittest.TestCase):

    def _heal(self):
        table = _table(
            'Sales',
            [{'name': 'Net', 'dataType': 'double'}],
        )
        table['columns'].append({
            'name': 'Negated', 'dataType': 'double',
            'expression': "-'Sales'[Gross]", 'isCalculated': True,
        })
        model = _model(table)
        _self_heal_model(model)
        return table

    def test_placeholder_is_created(self):
        cols = _columns(self._heal())
        self.assertIn('Gross', cols)

    def test_placeholder_is_calculated_not_physical(self):
        # A physical column whose partition never produces it makes Power BI
        # report the field as broken.
        col = _columns(self._heal())['Gross']
        self.assertTrue(col.get('isCalculated'))
        self.assertNotIn('sourceColumn', col)

    def test_placeholder_returns_blank(self):
        col = _columns(self._heal())['Gross']
        self.assertEqual(col.get('expression'), 'BLANK()')

    def test_placeholder_stays_hidden_and_annotated(self):
        col = _columns(self._heal())['Gross']
        self.assertTrue(col.get('isHidden'))
        notes = [a for a in col.get('annotations', [])
                 if a.get('name') == 'MigrationNote']
        self.assertTrue(notes)

    def test_real_columns_keep_their_source(self):
        # Negative control: healing must not strip genuine physical columns.
        col = _columns(self._heal())['Net']
        self.assertFalse(col.get('isCalculated'))


if __name__ == '__main__':
    unittest.main()
