"""
Tests for Sprint 79 — Conditional Formatting & Theme Depth.

Covers: diverging 3-stop gradient, sequential 2-stop gradient, stepped
color from thresholds, categorical color assignment, theme font mapping,
theme background/border/foreground, assessment formatting coverage metric.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'powerbi_import'))

from powerbi_import import visual_generator as vg
from powerbi_import.pbip_generator import PowerBIProjectGenerator
from powerbi_import.tmdl_generator import generate_theme_json
from powerbi_import.assessment import _check_visuals


# ── Diverging 3-Stop Gradient ─────────────────────────────────────

class TestDivergingGradient(unittest.TestCase):
    """Tests for 3-stop linearGradient3 from quantitative color encoding."""

    def _decorate(self, color_encoding):
        ws = {
            'name': 'S1',
            'referenceLines': [],
            'reference_lines': [],
            'mark_encoding': {'color': color_encoding},
            'axes': {},
        }
        visual_obj = {}
        vg._apply_visual_decorations(ws, 'bar', 'clusteredBarChart', 'S1', {}, visual_obj)
        return visual_obj

    def test_three_stop_gradient(self):
        obj = self._decorate({
            'type': 'quantitative',
            'palette_colors': ['#FF0000', '#FFFFFF', '#0000FF'],
        })
        dp = obj.get('objects', {}).get('dataPoint', [{}])
        fill = dp[0].get('properties', {}).get('fillRule', {})
        self.assertIn('linearGradient3', fill)
        grad = fill['linearGradient3']
        self.assertEqual(grad['min']['color'], '#FF0000')
        self.assertEqual(grad['mid']['color'], '#FFFFFF')
        self.assertEqual(grad['max']['color'], '#0000FF')

    def test_four_colors_uses_first_mid_last(self):
        obj = self._decorate({
            'type': 'quantitative',
            'palette_colors': ['#A', '#B', '#C', '#D'],
        })
        dp = obj.get('objects', {}).get('dataPoint', [{}])
        fill = dp[0].get('properties', {}).get('fillRule', {})
        self.assertIn('linearGradient3', fill)
        grad = fill['linearGradient3']
        self.assertEqual(grad['min']['color'], '#A')
        self.assertEqual(grad['max']['color'], '#D')


# ── Sequential 2-Stop Gradient ────────────────────────────────────

class TestSequentialGradient(unittest.TestCase):
    """Tests for 2-stop linearGradient2."""

    def _decorate(self, color_encoding):
        ws = {
            'name': 'S1',
            'referenceLines': [],
            'reference_lines': [],
            'mark_encoding': {'color': color_encoding},
            'axes': {},
        }
        visual_obj = {}
        vg._apply_visual_decorations(ws, 'bar', 'clusteredBarChart', 'S1', {}, visual_obj)
        return visual_obj

    def test_two_stop_gradient(self):
        obj = self._decorate({
            'type': 'quantitative',
            'palette_colors': ['#FFFFFF', '#0000FF'],
        })
        dp = obj.get('objects', {}).get('dataPoint', [{}])
        fill = dp[0].get('properties', {}).get('fillRule', {})
        self.assertIn('linearGradient2', fill)
        grad = fill['linearGradient2']
        self.assertEqual(grad['min']['color'], '#FFFFFF')
        self.assertEqual(grad['max']['color'], '#0000FF')

    def test_single_color_no_gradient(self):
        obj = self._decorate({
            'type': 'quantitative',
            'palette_colors': ['#FF0000'],
        })
        dp = obj.get('objects', {}).get('dataPoint', [{}])
        props = dp[0].get('properties', {}) if dp else {}
        fill = props.get('fillRule', {})
        self.assertNotIn('linearGradient2', fill)
        self.assertNotIn('linearGradient3', fill)


# ── Stepped Color from Thresholds ─────────────────────────────────

class TestSteppedColor(unittest.TestCase):
    """Tests for steppedColor from threshold-based encoding."""

    def _decorate(self, color_encoding):
        ws = {
            'name': 'S1',
            'referenceLines': [],
            'reference_lines': [],
            'mark_encoding': {'color': color_encoding},
            'axes': {},
        }
        visual_obj = {}
        vg._apply_visual_decorations(ws, 'bar', 'clusteredBarChart', 'S1', {}, visual_obj)
        return visual_obj

    def test_thresholds_produce_steps(self):
        obj = self._decorate({
            'type': 'quantitative',
            'thresholds': [
                {'value': 0, 'color': '#FF0000'},
                {'value': 50, 'color': '#FFFF00'},
                {'value': 100, 'color': '#00FF00'},
            ],
        })
        dp = obj.get('objects', {}).get('dataPoint', [{}])
        fill = dp[0].get('properties', {}).get('fillRule', {})
        self.assertIn('steppedColor', fill)
        self.assertEqual(len(fill['steppedColor']['steps']), 3)

    def test_threshold_values(self):
        obj = self._decorate({
            'type': 'quantitative',
            'thresholds': [
                {'value': 10, 'color': '#A'},
                {'value': 90, 'color': '#B'},
            ],
        })
        dp = obj.get('objects', {}).get('dataPoint', [{}])
        fill = dp[0].get('properties', {}).get('fillRule', {})
        steps = fill['steppedColor']['steps']
        self.assertEqual(steps[0]['inputValue'], 10)
        self.assertEqual(steps[0]['color'], '#A')
        self.assertEqual(steps[1]['inputValue'], 90)


# ── Categorical Color Assignment ──────────────────────────────────

class TestCategoricalColor(unittest.TestCase):
    """Tests for per-category sentimentColors from categorical encoding."""

    def _decorate(self, color_encoding):
        ws = {
            'name': 'S1',
            'referenceLines': [],
            'reference_lines': [],
            'mark_encoding': {'color': color_encoding},
            'axes': {},
        }
        visual_obj = {}
        vg._apply_visual_decorations(ws, 'bar', 'clusteredBarChart', 'S1', {}, visual_obj)
        return visual_obj

    def test_categorical_colors(self):
        obj = self._decorate({
            'type': 'categorical',
            'palette_colors': ['#FF0000', '#0000FF'],
        })
        sent = obj.get('objects', {}).get('sentimentColors', [])
        self.assertTrue(len(sent) > 0)

    def test_empty_categorical_no_sentiment(self):
        obj = self._decorate({
            'type': 'categorical',
            'palette_colors': [],
        })
        self.assertNotIn('sentimentColors', obj.get('objects', {}))


# ── Theme Font Mapping ─────────────────────────────────────────────

class TestThemeFontMapping(unittest.TestCase):
    """Tests for Tableau → web-safe font mapping in generate_theme_json()."""

    def test_tableau_book_to_segoe_ui(self):
        theme = generate_theme_json({'font_family': 'Tableau Book'})
        self.assertIn('Segoe UI', json.dumps(theme))

    def test_tableau_light_to_segoe_ui_light(self):
        theme = generate_theme_json({'font_family': 'Tableau Light'})
        self.assertIn('Segoe UI Light', json.dumps(theme))

    def test_tableau_semibold_to_segoe_ui_semibold(self):
        theme = generate_theme_json({'font_family': 'Tableau Semibold'})
        self.assertIn('Segoe UI Semibold', json.dumps(theme))

    def test_tableau_bold_to_segoe_ui_bold(self):
        theme = generate_theme_json({'font_family': 'Tableau Bold'})
        self.assertIn('Segoe UI Bold', json.dumps(theme))

    def test_unknown_font_passthrough(self):
        theme = generate_theme_json({'font_family': 'Comic Sans MS'})
        self.assertIn('Comic Sans MS', json.dumps(theme))

    def test_benton_sans_to_segoe_ui(self):
        theme = generate_theme_json({'font_family': 'Benton Sans'})
        self.assertIn('Segoe UI', json.dumps(theme))

    def test_no_font(self):
        theme = generate_theme_json({})
        self.assertIn('Segoe UI', json.dumps(theme))


# ── Theme Background / Border ─────────────────────────────────────

class TestThemeBackgroundBorder(unittest.TestCase):
    """Tests for theme background color, border color/width."""

    def test_background_color_in_theme(self):
        theme = generate_theme_json({'background_color': '#F0F0F0'})
        self.assertEqual(theme['background'], '#F0F0F0')

    def test_foreground_color_in_theme(self):
        theme = generate_theme_json({'foreground_color': '#333333'})
        self.assertEqual(theme['foreground'], '#333333')

    def test_border_color_in_theme(self):
        theme = generate_theme_json({
            'border_color': '#CCCCCC',
            'border_width': 2,
        })
        border = theme['visualStyles']['*']['*'].get('border', [])
        self.assertTrue(len(border) > 0)
        self.assertEqual(border[0]['color'], '#CCCCCC')
        self.assertEqual(border[0]['width'], 2)

    def test_no_extras_when_none(self):
        theme = generate_theme_json()
        self.assertIn('name', theme)
        self.assertEqual(theme['background'], '#FFFFFF')

    def test_invalid_color_ignored(self):
        theme = generate_theme_json({'background_color': 'not-a-color'})
        self.assertEqual(theme['background'], '#FFFFFF')


# ── Assessment Formatting Coverage ─────────────────────────────────

class TestAssessmentFormattingCoverage(unittest.TestCase):
    """Tests for formatting coverage sub-metric in _check_visuals()."""

    def _assess(self, worksheets, dashboards=None):
        extracted = {
            'worksheets': worksheets,
            'dashboards': dashboards or [],
        }
        result = _check_visuals(extracted)
        return result.checks

    def test_color_encoded_counted(self):
        checks = self._assess([{
            'name': 'S1',
            'mark_type': 'bar',
            'chart_type': 'bar',
            'fields': ['Category'],
            'mark_encoding': {'color': {'field': 'Region', 'type': 'categorical'}},
        }])
        fmt_checks = [c for c in checks
                       if 'format' in (c.detail or '').lower()
                       or 'color' in (c.detail or '').lower()]
        self.assertTrue(len(fmt_checks) >= 1)

    def test_no_encoding_no_crash(self):
        checks = self._assess([{
            'name': 'S1',
            'mark_type': 'text',
            'chart_type': 'text',
            'fields': ['Value'],
            'mark_encoding': {},
        }])
        self.assertIsInstance(checks, list)

    def test_conditional_formatting_counted(self):
        checks = self._assess([{
            'name': 'S1',
            'mark_type': 'bar',
            'chart_type': 'bar',
            'fields': ['X'],
            'mark_encoding': {},
            'conditionalFormatting': [{'field': 'Sales', 'mode': 'gradient'}],
        }])
        fmt_checks = [c for c in checks
                       if 'format' in (c.detail or '').lower()]
        self.assertTrue(len(fmt_checks) >= 1)


# ── Quantitative Gradient FillRule (PBIR dataPoint) ───────────────

class TestQuantitativeFillRule(unittest.TestCase):
    """A continuous Tableau colour must survive as a PBIR FillRule driven by
    the same measure. Without it the visual keeps a flat fill and the colour
    meaning is lost.
    """

    TABLE = 'Sales'
    MEASURE = 'Profit Ratio'

    def _generator(self, symbols=None):
        gen = PowerBIProjectGenerator.__new__(PowerBIProjectGenerator)
        gen._field_map = {}
        gen._main_table = self.TABLE
        gen._measure_names = {self.MEASURE}
        gen._bim_measure_names = {self.MEASURE}
        gen._actual_bim_measure_names = {self.MEASURE}
        gen._actual_bim_symbols = (set(symbols) if symbols is not None
                                   else {(self.TABLE, self.MEASURE)})
        gen._datasources_ref = []
        gen._collision_tables = set()
        gen._unavailable_parameter_names = set()
        return gen

    def _objects(self, color_encoding, symbols=None,
                 visual_type='clusteredBarChart'):
        objects = {}
        ws = {'name': 'S1', 'fields': [], 'totals': {}, 'padding': {}}
        mark_encoding = ({'color': color_encoding} if color_encoding is not None
                         else {})
        self._generator(symbols)._build_color_encoding_objects(
            objects, ws, visual_type, mark_encoding)
        return objects

    def _measure_ref(self):
        return {'Measure': {
            'Expression': {'SourceRef': {'Entity': self.TABLE}},
            'Property': self.MEASURE,
        }}

    def _gradient(self, low, high):
        return {'linearGradient2': {
            'min': {'color': {'Literal': {'Value': "'%s'" % low}}},
            'max': {'color': {'Literal': {'Value': "'%s'" % high}}},
        }}

    def _expected_fill(self, low, high):
        return {'solid': {'color': {'expr': {'FillRule': {
            'Input': self._measure_ref(),
            'FillRule': self._gradient(low, high),
        }}}}}

    def test_default_gradient_structure_and_literals(self):
        objects = self._objects({'type': 'quantitative', 'field': self.MEASURE})
        self.assertEqual(objects['dataPoint'],
                         [{'properties': {'fill': self._expected_fill(
                             '#F2F2F2', '#4472C4')}}])

    def test_gradient_input_is_the_colour_measure(self):
        objects = self._objects({'type': 'quantitative', 'field': self.MEASURE})
        rule = objects['dataPoint'][0]['properties']['fill']['solid']['color']['expr']['FillRule']
        self.assertEqual(rule['Input'], self._measure_ref())

    def test_palette_uses_first_and_last_colour(self):
        objects = self._objects({
            'type': 'quantitative',
            'field': self.MEASURE,
            'palette_colors': ['#FFF5EB', '#FDD0A2', '#7F3B08'],
        })
        self.assertEqual(objects['dataPoint'],
                         [{'properties': {'fill': self._expected_fill(
                             '#FFF5EB', '#7F3B08')}}])

    def test_single_palette_colour_keeps_default_high_stop(self):
        objects = self._objects({
            'type': 'quantitative',
            'field': self.MEASURE,
            'palette_colors': ['#FFF5EB'],
        })
        self.assertEqual(objects['dataPoint'],
                         [{'properties': {'fill': self._expected_fill(
                             '#FFF5EB', '#4472C4')}}])

    def _fill_rules(self, objects):
        return [rule for rule in objects.get('dataPoint', [])
                if 'FillRule' in json.dumps(
                    rule.get('properties', {}).get('fill', {}))]

    def test_no_colour_encoding_emits_no_data_point(self):
        self.assertEqual(self._objects(None), {})

    def test_empty_colour_encoding_emits_no_data_point(self):
        self.assertNotIn('dataPoint', self._objects({}))

    def test_quantitative_without_field_emits_no_fill_rule(self):
        objects = self._objects({'type': 'quantitative'})
        self.assertEqual(self._fill_rules(objects), [])

    def test_unresolved_colour_field_emits_no_fill_rule(self):
        # The measure is absent from the model, so no projection can be
        # built; emitting a FillRule would reference a deleted column.
        objects = self._objects({'type': 'quantitative', 'field': 'Ghost Ratio'},
                                symbols={(self.TABLE, 'Other Measure')})
        self.assertEqual(self._fill_rules(objects), [])
        self.assertNotIn('dataPoint', objects)

    def test_unresolved_colour_field_with_palette_falls_back_to_flat_fill(self):
        objects = self._objects(
            {'type': 'quantitative', 'field': 'Ghost Ratio',
             'palette_colors': ['#FFF5EB', '#7F3B08']},
            symbols={(self.TABLE, 'Other Measure')})
        self.assertEqual(self._fill_rules(objects), [])
        self.assertEqual(
            objects['dataPoint'][0]['properties']['fill']['solid']['color'],
            {'expr': {'Literal': {'Value': "'#FFF5EB'"}}})


if __name__ == '__main__':
    unittest.main()
