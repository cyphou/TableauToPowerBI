"""A partition that renames its columns breaks the assumption that a model
column name equals the name the partition produces. The model must ask for
the produced name, and the self-healer must not undo that.
"""
import unittest

from powerbi_import.self_healing_v3 import _heal_source_column_missing
from powerbi_import.tmdl_m_conversion import m_rename_map


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
