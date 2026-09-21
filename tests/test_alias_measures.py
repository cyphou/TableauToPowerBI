"""Tableau field aliases must survive as named measures.

Tableau aggregates implicitly, so an author who renames ``sum:F: GDP (curr $)``
to "GDP (US $'s)" has no named object to carry that caption. The migration used
to drop it: measured across the example corpus, 0 of 17 measure-name aliases
reached the model. An explicit measure is added instead of renaming anything,
so no existing reference can break.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.parity_registry import (  # noqa: E402
    HEALED,
    scan_workbook,
)
from powerbi_import.tmdl_generator import (  # noqa: E402
    _inject_alias_measures,
    _parse_alias_field_ref,
)


def _model():
    return {'model': {'tables': [{
        'name': 'Extract',
        'columns': [
            {'name': 'F: GDP (curr $)', 'dataType': 'Double'},
            {'name': 'H: Life exp (years)', 'dataType': 'Double'},
            {'name': 'Region', 'dataType': 'String'},
        ],
        'measures': [],
    }]}}


_COLUMN_TABLE_MAP = {
    'F: GDP (curr $)': 'Extract',
    'H: Life exp (years)': 'Extract',
    'Region': 'Extract',
}


def _measures(model):
    return {m['name']: m['expression']
            for t in model['model']['tables'] for m in t.get('measures', [])}


class TestParseAliasFieldRef(unittest.TestCase):
    """Field names contain colons, so naive splitting loses them."""

    def test_aggregation_and_field_are_separated(self):
        self.assertEqual(
            _parse_alias_field_ref('"[DS].[sum:F: GDP (curr $):qk]"'),
            (['sum'], 'F: GDP (curr $)'))

    def test_colon_inside_the_field_name_is_kept(self):
        _, field = _parse_alias_field_ref('"[DS].[avg:H: Life exp (years):qk]"')
        self.assertEqual(field, 'H: Life exp (years)')

    def test_stacked_prefixes_are_all_consumed(self):
        self.assertEqual(
            _parse_alias_field_ref('"[DS].[rank:sum:P: Population (count):qk]"'),
            (['rank', 'sum'], 'P: Population (count)'))

    def test_reference_without_datasource_prefix(self):
        self.assertEqual(_parse_alias_field_ref('[ctd:customer_id:qk]'),
                         (['ctd'], 'customer_id'))

    def test_empty_reference_is_safe(self):
        self.assertEqual(_parse_alias_field_ref(''), ([], ''))
        self.assertEqual(_parse_alias_field_ref(None), ([], ''))


class TestInjectAliasMeasures(unittest.TestCase):
    def test_aggregation_alias_becomes_a_named_measure(self):
        model = _model()
        _inject_alias_measures(model, {':Measure Names': {
            '"[DS].[sum:F: GDP (curr $):qk]"': "GDP (US $'s)",
        }}, _COLUMN_TABLE_MAP)
        self.assertEqual(_measures(model)["GDP (US $'s)"],
                         "SUM('Extract'[F: GDP (curr $)])")

    def test_average_alias_uses_average(self):
        model = _model()
        _inject_alias_measures(model, {':Measure Names': {
            '"[DS].[avg:H: Life exp (years):qk]"': 'Life Expectancy',
        }}, _COLUMN_TABLE_MAP)
        self.assertEqual(_measures(model)['Life Expectancy'],
                         "AVERAGE('Extract'[H: Life exp (years)])")

    def test_rank_alias_wraps_the_aggregation(self):
        model = _model()
        _inject_alias_measures(model, {':Measure Names': {
            '"[DS].[rank:sum:F: GDP (curr $):qk]"': 'Rank GDP',
        }}, _COLUMN_TABLE_MAP)
        self.assertEqual(_measures(model)['Rank GDP'],
                         "RANKX(ALL('Extract'), SUM('Extract'[F: GDP (curr $)]))")

    def test_nothing_is_renamed(self):
        """Adding, never renaming, is what keeps existing references valid."""
        model = _model()
        model['model']['tables'][0]['measures'].append(
            {'name': 'Existing', 'expression': "SUM('Extract'[F: GDP (curr $)])"})
        _inject_alias_measures(model, {':Measure Names': {
            '"[DS].[sum:F: GDP (curr $):qk]"': 'Total GDP',
        }}, _COLUMN_TABLE_MAP)
        measures = _measures(model)
        self.assertIn('Existing', measures)
        self.assertIn('Total GDP', measures)

    def test_existing_name_is_never_overwritten(self):
        model = _model()
        model['model']['tables'][0]['measures'].append(
            {'name': 'Total GDP', 'expression': 'BLANK()'})
        _inject_alias_measures(model, {':Measure Names': {
            '"[DS].[sum:F: GDP (curr $):qk]"': 'Total GDP',
        }}, _COLUMN_TABLE_MAP)
        self.assertEqual(_measures(model)['Total GDP'], 'BLANK()')

    def test_column_name_collision_is_skipped(self):
        model = _model()
        _inject_alias_measures(model, {':Measure Names': {
            '"[DS].[sum:F: GDP (curr $):qk]"': 'Region',
        }}, _COLUMN_TABLE_MAP)
        self.assertNotIn('Region', _measures(model))

    def test_unknown_column_produces_nothing(self):
        model = _model()
        _inject_alias_measures(model, {':Measure Names': {
            '"[DS].[sum:Not A Column:qk]"': 'Ghost',
        }}, _COLUMN_TABLE_MAP)
        self.assertEqual(_measures(model), {})

    def test_unaggregated_alias_produces_nothing(self):
        """`none:` names a field, it does not aggregate it."""
        model = _model()
        _inject_alias_measures(model, {':Measure Names': {
            '"[DS].[none:Region:nk]"': 'Sales Region',
        }}, _COLUMN_TABLE_MAP)
        self.assertEqual(_measures(model), {})

    def test_value_aliases_are_ignored(self):
        model = _model()
        _inject_alias_measures(model, {'Region': {'%null%': ' '}},
                               _COLUMN_TABLE_MAP)
        self.assertEqual(_measures(model), {})

    def test_value_aliases_generate_a_switch_column(self):
        model = _model()
        _inject_alias_measures(model, {'Region': {'%null%': ' '}},
                               _COLUMN_TABLE_MAP)
        from powerbi_import.tmdl_generator import _inject_value_alias_columns
        _inject_value_alias_columns(model, {'Region': {'North': 'North (N)', 'South': 'South (S)'}},
                                   _COLUMN_TABLE_MAP)
        columns = {c['name']: c.get('expression', '') for t in model['model']['tables'] for c in t.get('columns', [])}
        self.assertIn('Region (Display)', columns)
        self.assertIn('SWITCH', columns['Region (Display)'])
        self.assertIn('North (N)', columns['Region (Display)'])

    def test_absent_aliases_are_safe(self):
        model = _model()
        _inject_alias_measures(model, {}, _COLUMN_TABLE_MAP)
        _inject_alias_measures(model, None, _COLUMN_TABLE_MAP)
        self.assertEqual(_measures(model), {})

    def test_apostrophe_in_the_table_name_is_escaped(self):
        model = _model()
        model['model']['tables'][0]['name'] = "Bob's Data"
        _inject_alias_measures(model, {':Measure Names': {
            '"[DS].[sum:F: GDP (curr $):qk]"': 'Total',
        }}, {'F: GDP (curr $)': "Bob's Data"})
        self.assertEqual(_measures(model)['Total'],
                         "SUM('Bob''s Data'[F: GDP (curr $)])")


class TestAliasParityIsSplitByCapability(unittest.TestCase):
    """The two alias kinds do not migrate with the same fidelity."""

    def test_measure_name_aliases_are_healed(self):
        scan = scan_workbook({'aliases': {
            ':Measure Names': {'"[DS].[sum:Sales:qk]"': 'Revenue'}}})
        usage = {u.key: u for u in scan.usages}['alias_measure_name']
        self.assertEqual(usage.status, HEALED)
        self.assertEqual(usage.count, 1)

    def test_value_aliases_are_healed(self):
        scan = scan_workbook({'aliases': {'Region': {'%null%': ' '}}})
        usage = {u.key: u for u in scan.usages}['alias_value']
        self.assertEqual(usage.status, HEALED)

    def test_parameter_value_aliases_are_not_counted(self):
        """Parameter value labels migrate via the parameter table, not a column."""
        scan = scan_workbook({
            'parameters': [{'name': 'Last x Days', 'caption': 'Last x Days'}],
            'aliases': {'Last x Days': {'"30"': 'Last 30 days'}},
        })
        self.assertNotIn('alias_value', {u.key for u in scan.usages})

    def test_measure_aliases_alone_score_full_parity(self):
        scan = scan_workbook({'aliases': {
            ':Measure Names': {'"[DS].[sum:Sales:qk]"': 'Revenue'}}})
        self.assertEqual(scan.parity_score, 100.0)

    def test_aliases_stay_tracked_not_untracked(self):
        scan = scan_workbook({'aliases': {'Region': {'a': 'b'}}})
        self.assertNotIn('aliases', scan.untracked_features)


if __name__ == '__main__':
    unittest.main()
