"""Lineage must resolve targets against the right source category.

Measured on the example corpus: 120 records across 12 of 26 workbooks were
reported unresolved. None was a missing source. The resolver compared every
target against source *tables* and *columns* only, so:

- generated What-If tables, named after their parameter (``Base Salary``),
  matched no source table and were reported as orphans;
- calculated columns, whose source is a calculation, matched no source column;
- columns a join merged in from another Tableau table were looked up only in
  the table that shares the target's name.

These tests pin each category so the count cannot silently inflate again.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.tmdl_generator import (  # noqa: E402
    _build_lineage_map,
    _calculation_names,
    _generated_table_names,
)

_DATASOURCES = [{
    'name': 'ds1',
    'caption': 'Sales data',
    'tables': [
        {'name': 'Orders', 'columns': [{'name': 'order_id'}, {'name': 'amount'}]},
        {'name': 'Customers', 'columns': [{'name': 'city'}, {'name': 'segment'}]},
    ],
}]


def _table(name, columns, partitions=None):
    return {
        'name': name,
        'columns': [{'name': c} for c in columns],
        'measures': [],
        'partitions': partitions or [],
    }


def _status(lineage, section, target_key, target):
    for record in lineage[section]:
        if record.get(target_key) == target:
            return record.get('source_status')
    return None


class TestGeneratedTableNames(unittest.TestCase):
    def test_calendar_is_generated(self):
        self.assertIn('Calendar', _generated_table_names([], {}))

    def test_parameter_table_is_named_after_its_parameter(self):
        """`Base Salary` is a What-If table, not an orphan."""
        names = _generated_table_names([], {'parameters': [
            {'caption': 'Base Salary'}]})
        self.assertIn('Base Salary', names)

    def test_field_parameter_table_is_recognised(self):
        names = _generated_table_names([], {'parameters': [
            {'caption': 'Select Dimension'}]})
        self.assertIn('Select Dimension FieldParam', names)

    def test_calculation_group_is_recognised_structurally(self):
        tables = [_table('Time Intelligence', [], partitions=[
            {'source': {'type': 'calculationGroup'}}])]
        self.assertIn('Time Intelligence', _generated_table_names(tables, {}))

    def test_a_source_table_is_not_generated(self):
        self.assertNotIn('Orders', _generated_table_names([], {'parameters': []}))


class TestCalculationNames(unittest.TestCase):
    def test_caption_and_name_are_both_indexed(self):
        names = _calculation_names({'calculations': [
            {'caption': 'Profit Ratio', 'name': '[Calculation_1]'}]})
        self.assertIn('profit ratio', names)
        self.assertIn('calculation_1', names)


class TestLineageResolution(unittest.TestCase):
    def test_parameter_table_is_generated_not_unresolved(self):
        tables = [_table('Base Salary', ['Value'])]
        lineage = _build_lineage_map(
            tables, [], {'parameters': [{'caption': 'Base Salary'}]},
            _DATASOURCES)
        self.assertEqual(_status(lineage, 'tables', 'pbi_table', 'Base Salary'),
                         'generated')
        self.assertEqual(lineage['contract']['unresolved'], [])

    def test_calculated_column_resolves_to_its_calculation(self):
        tables = [_table('Orders', ['order_id', 'Days Since First Order'])]
        lineage = _build_lineage_map(
            tables, [], {'calculations': [{'caption': 'Days Since First Order'}]},
            _DATASOURCES)
        self.assertEqual(
            _status(lineage, 'columns', 'pbi_column', 'Days Since First Order'),
            'calculated')

    def test_joined_column_resolves_to_its_owning_table(self):
        """A join merges columns from several Tableau tables into one target."""
        tables = [_table('Orders', ['order_id', 'city'])]
        lineage = _build_lineage_map(tables, [], {}, _DATASOURCES)
        record = next(r for r in lineage['columns']
                      if r.get('pbi_column') == 'city')
        self.assertEqual(record['source_status'], 'inferred')
        self.assertEqual(record['tableau_table'], 'Customers')

    def test_a_column_from_nowhere_stays_unresolved(self):
        """The resolver must still be able to report a genuine orphan."""
        tables = [_table('Orders', ['order_id', 'invented_column'])]
        lineage = _build_lineage_map(tables, [], {}, _DATASOURCES)
        self.assertEqual(
            _status(lineage, 'columns', 'pbi_column', 'invented_column'),
            'unresolved')
        self.assertTrue(lineage['contract']['unresolved'])

    def test_an_exact_source_column_is_still_exact(self):
        tables = [_table('Orders', ['order_id'])]
        lineage = _build_lineage_map(tables, [], {}, _DATASOURCES)
        self.assertEqual(
            _status(lineage, 'columns', 'pbi_column', 'order_id'), 'exact')


class TestCoverageCountsKnownOrigins(unittest.TestCase):
    """"Where did this come from?" is answered by a source, by the generator
    having made it, or by a calculation."""

    def test_generated_tables_count_as_resolved(self):
        tables = [_table('Base Salary', ['Value'])]
        lineage = _build_lineage_map(
            tables, [], {'parameters': [{'caption': 'Base Salary'}]},
            _DATASOURCES)
        coverage = lineage['contract']['coverage']['tables']
        self.assertEqual(coverage['resolved_count'], coverage['target_count'])
        self.assertEqual(coverage['percent'], 100.0)

    def test_calculated_columns_count_as_resolved(self):
        tables = [_table('Orders', ['Days Since First Order'])]
        lineage = _build_lineage_map(
            tables, [], {'calculations': [{'caption': 'Days Since First Order'}]},
            _DATASOURCES)
        coverage = lineage['contract']['coverage']['columns']
        self.assertEqual(coverage['percent'], 100.0)

    def test_an_orphan_still_lowers_coverage(self):
        tables = [_table('Orders', ['order_id', 'invented_column'])]
        lineage = _build_lineage_map(tables, [], {}, _DATASOURCES)
        self.assertLess(lineage['contract']['coverage']['columns']['percent'],
                        100.0)
        self.assertEqual(lineage['contract']['status'], 'partial')


if __name__ == '__main__':
    unittest.main()
