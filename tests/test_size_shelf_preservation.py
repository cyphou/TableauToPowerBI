"""A Tableau size-shelf measure must not vanish when the visual cannot size by it.

Only scatter and map expose a Size role. Tableau happily sizes a pie by one
measure while its slices show another, and that second measure used to be
dropped on the floor. Measured over the real portfolio nothing is lost today
(17 land in a native Size role, 8 are already bound elsewhere), so these cases
are built to exercise the shape the corpus does not contain.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'tableau_export'))

from powerbi_import.pbip_generator import (PowerBIProjectGenerator,
                                           _TOOLTIP_CAPABLE_VISUALS)


def _generator():
    gen = PowerBIProjectGenerator.__new__(PowerBIProjectGenerator)
    gen._field_map = {'Angle': ('Sales', 'Angle'),
                      'Weight': ('Sales', 'Weight')}
    gen._main_table = 'Sales'
    gen._measure_names = {'Angle', 'Weight'}
    gen._bim_measure_names = set()
    gen._actual_bim_measure_names = set()
    gen._actual_bim_symbols = {}
    gen._actual_bim_column_types = {}
    gen._datasources_ref = []
    gen._collision_tables = {}
    gen._unavailable_parameter_names = set()
    return gen


def _measure(name):
    return {'name': name, 'shelf': 'size', 'aggregation': 'sum'}


def _bound_names(query_state):
    names = []
    for role in query_state.values():
        for projection in role.get('projections', []):
            field = projection['field']
            holder = field.get('Aggregation', {}).get('Expression', field)
            for wrapper in ('Column', 'Measure'):
                if wrapper in holder:
                    names.append(holder[wrapper]['Property'])
    return names


class TestSizeMeasurePreserved(unittest.TestCase):

    def test_pie_keeps_a_size_measure_its_slices_do_not_show(self):
        gen = _generator()
        state = {'Y': {'projections': [gen._make_projection_entry(
            {'name': 'Angle', 'aggregation': 'sum'})]}}
        gen._preserve_size_measures(state, 'pieChart', [_measure('Weight')])
        self.assertIn('Tooltips', state)
        self.assertIn('Weight', _bound_names(state))
        self.assertIn('Angle', _bound_names(state))

    def test_a_size_measure_already_shown_is_not_duplicated(self):
        gen = _generator()
        state = {'Y': {'projections': [gen._make_projection_entry(
            {'name': 'Weight', 'aggregation': 'sum'})]}}
        gen._preserve_size_measures(state, 'pieChart', [_measure('Weight')])
        self.assertNotIn('Tooltips', state)
        self.assertEqual(_bound_names(state).count('Weight'), 1)

    def test_a_visual_with_its_own_size_role_is_left_alone(self):
        gen = _generator()
        state = {'Size': {'projections': [gen._make_projection_entry(
            {'name': 'Weight', 'aggregation': 'sum'})]}}
        gen._preserve_size_measures(state, 'scatterChart', [_measure('Weight')])
        self.assertNotIn('Tooltips', state)

    def test_a_visual_without_a_tooltips_role_gains_nothing(self):
        gen = _generator()
        for visual_type in ('tableEx', 'card', 'matrix', 'multiRowCard'):
            with self.subTest(visual_type=visual_type):
                self.assertNotIn(visual_type, _TOOLTIP_CAPABLE_VISUALS)
                state = {'Values': {'projections': [gen._make_projection_entry(
                    {'name': 'Angle', 'aggregation': 'sum'})]}}
                gen._preserve_size_measures(state, visual_type,
                                            [_measure('Weight')])
                self.assertNotIn('Tooltips', state)

    def test_nothing_is_invented_when_there_is_no_size_measure(self):
        gen = _generator()
        state = {'Y': {'projections': [gen._make_projection_entry(
            {'name': 'Angle', 'aggregation': 'sum'})]}}
        gen._preserve_size_measures(state, 'pieChart', [])
        self.assertNotIn('Tooltips', state)

    def test_an_empty_visual_is_not_given_a_lone_tooltip(self):
        gen = _generator()
        state = {}
        gen._preserve_size_measures(state, 'pieChart', [_measure('Weight')])
        self.assertEqual(state, {})


if __name__ == '__main__':
    unittest.main()
