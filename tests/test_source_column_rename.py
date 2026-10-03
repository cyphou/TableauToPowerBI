"""A partition that renames its columns breaks the assumption that a model
column name equals the name the partition produces. The model must ask for
the produced name, and the self-healer must not undo that.
"""
import tempfile
import unittest

from powerbi_import.pbip_generator import PowerBIProjectGenerator
from powerbi_import.self_healing_v3 import _heal_source_column_missing
from powerbi_import.tmdl_dax_postprocess import retarget_dax_column_refs
from powerbi_import.tmdl_generator import _build_table, generate_tmdl
from powerbi_import.tmdl_m_conversion import m_rename_map, rename_m_column_refs

CSV_CONNECTION = {'type': 'csv', 'filename': 'sales.csv'}


def _columns(*names):
    return [{'name': n} for n in names]


def _meta(**pairs):
    return {name: {'caption': caption} for name, caption in pairs.items()}


class TestRenameMap(unittest.TestCase):

    def test_a_caption_becomes_a_rename(self):
        m = m_rename_map(_columns('full_date'), _meta(full_date='Full Date'))
        self.assertEqual(m, {'full_date': 'Full Date'})

    def test_a_case_only_caption_still_renames(self):
        # This is the pair the healer used to undo.
        m = m_rename_map(_columns('year'), _meta(year='Year'))
        self.assertEqual(m, {'year': 'Year'})

    def test_an_identical_caption_is_not_a_rename(self):
        self.assertEqual(m_rename_map(_columns('year'), _meta(year='year')), {})

    def test_no_caption_is_not_a_rename(self):
        self.assertEqual(m_rename_map(_columns('year'), {}), {})

    def test_bracketed_names_are_cleaned(self):
        m = m_rename_map([{'name': '[year]'}], {'[year]': {'caption': 'Year'}})
        self.assertEqual(m, {'year': 'Year'})


class TestRenameMapCollisions(unittest.TestCase):
    """A rename onto a name the table already answers to is refused.

    Two columns of the same name is a shape the partition cannot produce and
    the model cannot load, so the caption loses rather than the table.
    """

    def test_a_caption_taken_by_another_column_is_skipped(self):
        m = m_rename_map(_columns('pct', 'Total'), _meta(pct='Total'))
        self.assertEqual(m, {})

    def test_the_other_columns_still_rename_around_the_collision(self):
        m = m_rename_map(_columns('pct', 'Total', 'amt'),
                         _meta(pct='Total', amt='Montant'))
        self.assertEqual(m, {'amt': 'Montant'})

    def test_two_columns_claiming_one_caption_only_let_the_first_through(self):
        m = m_rename_map(_columns('a', 'b'), _meta(a='X', b='X'))
        self.assertEqual(m, {'a': 'X'})

    def test_a_vacated_name_is_free_for_the_next_column(self):
        # 'pct' renames away to 'Taux', which releases 'pct' for 'raw'.
        m = m_rename_map(_columns('pct', 'raw'), _meta(pct='Taux', raw='pct'))
        self.assertEqual(m, {'pct': 'Taux', 'raw': 'pct'})


class TestRenameMColumnRefs(unittest.TestCase):
    """Calculated M steps are injected after Table.RenameColumns, so a
    reference to the Tableau name resolves to nothing at that point."""

    RENAMES = {'pct': 'Taux %'}

    def test_a_bare_reference_is_retargeted(self):
        self.assertEqual(rename_m_column_refs('[pct] * 2', self.RENAMES),
                         '[#"Taux %"] * 2')

    def test_an_already_quoted_reference_is_retargeted(self):
        self.assertEqual(rename_m_column_refs('[#"pct"] * 2', self.RENAMES),
                         '[#"Taux %"] * 2')

    def test_a_target_needing_quotes_gets_them(self):
        # '%' cannot appear in a bare M generalized identifier.
        self.assertEqual(rename_m_column_refs('[pct]', self.RENAMES),
                         '[#"Taux %"]')

    def test_a_target_with_one_interior_space_stays_bare(self):
        # A single interior space is legal, so quoting it would be noise.
        self.assertEqual(rename_m_column_refs('[lib]', {'lib': 'Ofp Libelle'}),
                         '[Ofp Libelle]')

    def test_a_quoted_source_name_is_matched_unquoted(self):
        self.assertEqual(rename_m_column_refs('[#"raw pct "]',
                                              {'raw pct ': 'Taux'}),
                         '[Taux]')

    def test_a_string_literal_of_the_same_name_is_left_alone(self):
        # Table.RenameColumns carries the old name as a literal; rewriting it
        # would make the step rename a column that no longer exists.
        self.assertEqual(
            rename_m_column_refs('Text.From("pct") & [pct]', self.RENAMES),
            'Text.From("pct") & [#"Taux %"]')

    def test_an_empty_rename_map_is_a_no_op(self):
        self.assertEqual(rename_m_column_refs('[pct] * 2', {}), '[pct] * 2')

    def test_an_empty_expression_is_a_no_op(self):
        self.assertEqual(rename_m_column_refs('', self.RENAMES), '')

    def test_an_unrenamed_reference_is_untouched(self):
        self.assertEqual(rename_m_column_refs('[other]', self.RENAMES),
                         '[other]')

    def test_every_renamed_reference_in_one_expression_is_retargeted(self):
        self.assertEqual(
            rename_m_column_refs('[pct] + [amt]',
                                 {'pct': 'Taux %', 'amt': 'Montant'}),
            '[#"Taux %"] + [Montant]')


class TestBuiltTableNamesTheProducedColumn(unittest.TestCase):
    """Desktop resolves a column by the name the partition emits, so a model
    name that differs from it leaves every report reference dangling."""

    def _build(self, columns, metadata, table_name='Sales'):
        return _build_table({'name': table_name, 'columns': columns},
                            CSV_CONNECTION, [], [], {}, metadata, {})

    def test_a_captioned_column_is_named_after_its_source_column(self):
        table = self._build([{'name': 'pct', 'datatype': 'real'}],
                            {'pct': {'caption': 'Taux %'}})
        column = table['columns'][0]
        self.assertEqual(column['name'], 'Taux %')
        self.assertEqual(column['name'], column['sourceColumn'])

    def test_an_uncaptioned_column_keeps_its_tableau_name(self):
        table = self._build([{'name': 'region', 'datatype': 'string'}], {})
        column = table['columns'][0]
        self.assertEqual(column['name'], 'region')
        self.assertEqual(column['sourceColumn'], 'region')

    def test_the_partition_renames_onto_that_same_name(self):
        table = self._build([{'name': 'pct', 'datatype': 'real'}],
                            {'pct': {'caption': 'Taux %'}})
        expression = table['partitions'][0]['source']['expression']
        self.assertIn('Table.RenameColumns', expression)
        self.assertIn('{"pct", "Taux %"}', expression)

    def test_the_rename_is_recorded_on_the_table(self):
        table = self._build([{'name': 'pct', 'datatype': 'real'}],
                            {'pct': {'caption': 'Taux %'}})
        self.assertEqual(table['_column_renames'], {'pct': 'Taux %'})

    def test_a_table_with_no_rename_records_nothing(self):
        table = self._build([{'name': 'region', 'datatype': 'string'}], {})
        self.assertNotIn('_column_renames', table)

    def test_metadata_is_still_keyed_on_the_tableau_name(self):
        table = self._build(
            [{'name': 'pct', 'datatype': 'real'}],
            {'pct': {'caption': 'Taux %', 'hidden': True,
                     'description': 'Part du total'}})
        column = table['columns'][0]
        self.assertEqual(column['name'], 'Taux %')
        self.assertTrue(column['isHidden'])
        self.assertEqual(column['description'], 'Part du total')

    def test_a_collided_caption_leaves_both_columns_as_they_were(self):
        table = self._build([{'name': 'pct', 'datatype': 'real'},
                             {'name': 'Total', 'datatype': 'real'}],
                            {'pct': {'caption': 'Total'}})
        self.assertEqual([c['name'] for c in table['columns']],
                         ['pct', 'Total'])
        self.assertNotIn('_column_renames', table)

    def test_duplicate_produced_names_are_suffixed(self):
        table = self._build([{'name': 'a', 'datatype': 'string'},
                             {'name': 'a', 'datatype': 'string'}],
                            {'a': {'caption': 'X'}})
        self.assertEqual([c['name'] for c in table['columns']], ['X', 'X_1'])
        self.assertEqual([c['sourceColumn'] for c in table['columns']],
                         ['X', 'X'])


class TestGeneratedModelReportsItsRenames(unittest.TestCase):

    def test_the_stats_carry_the_rename_ledger(self):
        datasources = [{
            'name': 'ds',
            'connection': dict(CSV_CONNECTION),
            'tables': [{'name': 'Sales', 'columns': [
                {'name': 'pct', 'datatype': 'real', 'caption': 'Taux %'},
                {'name': 'region', 'datatype': 'string'},
            ]}],
            'columns': [{'name': 'pct', 'caption': 'Taux %',
                         'datatype': 'real'}],
            'calculations': [],
            'relationships': [],
        }]
        with tempfile.TemporaryDirectory() as output_dir:
            stats = generate_tmdl(datasources, 'R', {}, output_dir)
        self.assertEqual(stats['column_rename_map'],
                         {('Sales', 'pct'): 'Taux %'})


def _model(tables, renames):
    return {'_column_rename_map': renames, 'model': {'tables': tables}}


class TestRetargetDaxColumnRefs(unittest.TestCase):
    """DAX written against the Tableau name resolves to nothing once the
    model carries the name its partition produces."""

    def test_a_bare_reference_resolves_against_its_own_table(self):
        model = _model(
            [{'name': 'Sales',
              'columns': [{'name': 'flag', 'expression': 'IF([pct] > 0, 1, 0)'}],
              'measures': []}],
            {('Sales', 'pct'): 'Taux %'})
        self.assertEqual(retarget_dax_column_refs(model), 1)
        self.assertEqual(model['model']['tables'][0]['columns'][0]['expression'],
                         'IF([Taux %] > 0, 1, 0)')

    def test_a_quoted_qualified_reference_resolves_against_the_named_table(self):
        model = _model(
            [{'name': 'Sales', 'columns': [],
              'measures': [{'name': 'M', 'expression': "SUM('Dim'[lib])"}]}],
            {('Dim', 'lib'): 'Libelle'})
        self.assertEqual(retarget_dax_column_refs(model), 1)
        self.assertEqual(model['model']['tables'][0]['measures'][0]['expression'],
                         "SUM('Dim'[Libelle])")

    def test_an_unquoted_qualified_reference_resolves_too(self):
        model = _model(
            [{'name': 'Sales', 'columns': [],
              'measures': [{'name': 'M', 'expression': 'SUM(Dim[lib])'}]}],
            {('Dim', 'lib'): 'Libelle'})
        self.assertEqual(retarget_dax_column_refs(model), 1)
        self.assertEqual(model['model']['tables'][0]['measures'][0]['expression'],
                         'SUM(Dim[Libelle])')

    def test_a_bare_reference_is_not_resolved_against_a_foreign_table(self):
        model = _model(
            [{'name': 'Sales',
              'columns': [{'name': 'flag', 'expression': 'IF([lib] = "x", 1, 0)'}],
              'measures': []}],
            {('Dim', 'lib'): 'Libelle'})
        self.assertEqual(retarget_dax_column_refs(model), 0)
        self.assertEqual(model['model']['tables'][0]['columns'][0]['expression'],
                         'IF([lib] = "x", 1, 0)')

    def test_a_string_literal_is_never_rewritten(self):
        model = _model(
            [{'name': 'Sales',
              'columns': [{'name': 'flag',
                           'expression': 'IF([pct] > 0, "pct", "no pct")'}],
              'measures': []}],
            {('Sales', 'pct'): 'Taux %'})
        self.assertEqual(retarget_dax_column_refs(model), 1)
        self.assertEqual(model['model']['tables'][0]['columns'][0]['expression'],
                         'IF([Taux %] > 0, "pct", "no pct")')

    def test_columns_and_measures_are_both_counted(self):
        model = _model(
            [{'name': 'Sales',
              'columns': [{'name': 'flag', 'expression': '[pct] * 2'}],
              'measures': [{'name': 'M', 'expression': 'SUM([pct])'}]}],
            {('Sales', 'pct'): 'Taux %'})
        self.assertEqual(retarget_dax_column_refs(model), 2)

    def test_an_unrenamed_reference_leaves_the_expression_alone(self):
        model = _model(
            [{'name': 'Sales', 'columns': [],
              'measures': [{'name': 'M', 'expression': 'SUM([region])'}]}],
            {('Sales', 'pct'): 'Taux %'})
        self.assertEqual(retarget_dax_column_refs(model), 0)
        self.assertEqual(model['model']['tables'][0]['measures'][0]['expression'],
                         'SUM([region])')

    def test_an_absent_rename_map_rewrites_nothing(self):
        model = {'model': {'tables': [
            {'name': 'Sales', 'columns': [],
             'measures': [{'name': 'M', 'expression': 'SUM([pct])'}]}]}}
        self.assertEqual(retarget_dax_column_refs(model), 0)
        self.assertEqual(model['model']['tables'][0]['measures'][0]['expression'],
                         'SUM([pct])')

    def test_an_empty_rename_map_rewrites_nothing(self):
        self.assertEqual(retarget_dax_column_refs(_model([], {})), 0)


class TestReportFieldsResolveToTheProducedName(unittest.TestCase):
    """The report has to ask the model for the name the model carries."""

    def _field_map(self, column_renames):
        generator = PowerBIProjectGenerator(output_dir=tempfile.mkdtemp())
        generator._column_rename_map = column_renames
        generator._build_field_mapping({'datasources': [{
            'name': 'ds',
            'tables': [{'name': 'Sales', 'columns': [
                {'name': 'pct', 'caption': 'Taux %'},
                {'name': 'region'},
            ]}],
            'columns': [],
            'calculations': [],
        }]})
        return generator._field_map

    def test_the_tableau_name_resolves_to_the_produced_name(self):
        self.assertEqual(self._field_map({('Sales', 'pct'): 'Taux %'})['pct'],
                         ('Sales', 'Taux %'))

    def test_the_caption_resolves_to_the_produced_name(self):
        field_map = self._field_map({('Sales', 'pct'): 'Taux %'})
        self.assertEqual(field_map['Taux %'], ('Sales', 'Taux %'))

    def test_an_unrenamed_column_resolves_to_itself(self):
        field_map = self._field_map({('Sales', 'pct'): 'Taux %'})
        self.assertEqual(field_map['region'], ('Sales', 'region'))

    def test_without_a_rename_the_tableau_name_is_kept(self):
        self.assertEqual(self._field_map({})['pct'], ('Sales', 'pct'))


def _table(m_expression, columns):
    return {
        'name': 'T',
        'columns': columns,
        'partitions': [{'name': 'T', 'mode': 'import',
                        'source': {'type': 'm', 'expression': m_expression}}],
    }


class TestHealerRespectsARenamingPartition(unittest.TestCase):

    RENAMING = ('let S = Source,\n'
                '  #"Renamed Columns" = Table.RenameColumns(S, '
                '{{"year", "Year"}, {"full_date", "Full Date"}})\n'
                'in #"Renamed Columns"')

    def _heal(self, table):
        model = {'model': {'tables': [table]}}
        repairs = _heal_source_column_missing(model)
        return repairs, {c['name']: c.get('sourceColumn')
                         for c in table['columns']}

    def test_a_produced_name_is_left_alone(self):
        # Previously "Year" was realigned to "year" because a column named
        # year exists, silently breaking the binding the rename created.
        table = _table(self.RENAMING,
                       [{'name': 'year', 'sourceColumn': 'Year'}])
        repairs, cols = self._heal(table)
        self.assertEqual(cols['year'], 'Year')
        self.assertEqual(repairs, 0)

    def test_every_renamed_column_is_left_alone(self):
        table = _table(self.RENAMING, [
            {'name': 'year', 'sourceColumn': 'Year'},
            {'name': 'full_date', 'sourceColumn': 'Full Date'},
        ])
        _repairs, cols = self._heal(table)
        self.assertEqual(cols, {'year': 'Year', 'full_date': 'Full Date'})

    def test_a_genuine_case_mismatch_is_still_healed(self):
        # Negative control: without a rename producing it, the old repair
        # is still the right one.
        table = _table('let S = Source in S',
                       [{'name': 'year', 'sourceColumn': 'YEAR'}])
        repairs, cols = self._heal(table)
        self.assertEqual(cols['year'], 'year')
        self.assertEqual(repairs, 1)

    def test_a_table_without_a_partition_is_still_healed(self):
        table = {'name': 'T',
                 'columns': [{'name': 'year', 'sourceColumn': 'YEAR'}]}
        model = {'model': {'tables': [table]}}
        self.assertEqual(_heal_source_column_missing(model), 1)

    def test_a_matching_source_column_is_untouched(self):
        table = _table('let S = Source in S',
                       [{'name': 'year', 'sourceColumn': 'year'}])
        repairs, cols = self._heal(table)
        self.assertEqual(cols['year'], 'year')
        self.assertEqual(repairs, 0)


if __name__ == '__main__':
    unittest.main()
