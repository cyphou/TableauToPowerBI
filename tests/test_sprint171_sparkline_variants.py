"""Tests for Sprint 171 — Sparkline Variants.

Covers: area sparkline, bar/column sparkline, win/loss sparkline,
conditional formatting color rules, axis range propagation, subtype
detection, VISUAL_TYPE_MAP entries.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.visual_generator import (
    _build_sparkline_config,
    detect_sparkline_subtype,
    VISUAL_TYPE_MAP,
    SPARKLINE_LINE,
    SPARKLINE_COLUMN,
    SPARKLINE_AREA,
    SPARKLINE_WINLOSS,
    _SPARKLINE_SUBTYPE_MAP,
)


class TestSparklineSubtypeDetection(unittest.TestCase):
    """Tests for detect_sparkline_subtype()."""

    def test_line_sparkline(self):
        self.assertEqual(detect_sparkline_subtype('sparkline'), SPARKLINE_LINE)

    def test_area_sparkline(self):
        self.assertEqual(detect_sparkline_subtype('areasparkline'), SPARKLINE_AREA)
        self.assertEqual(detect_sparkline_subtype('area-sparkline'), SPARKLINE_AREA)

    def test_bar_sparkline(self):
        self.assertEqual(detect_sparkline_subtype('barsparkline'), SPARKLINE_COLUMN)
        self.assertEqual(detect_sparkline_subtype('bar-sparkline'), SPARKLINE_COLUMN)

    def test_column_sparkline(self):
        self.assertEqual(detect_sparkline_subtype('columnsparkline'), SPARKLINE_COLUMN)
        self.assertEqual(detect_sparkline_subtype('column-sparkline'), SPARKLINE_COLUMN)

    def test_winloss_sparkline(self):
        self.assertEqual(detect_sparkline_subtype('winlosssparkline'), SPARKLINE_WINLOSS)
        self.assertEqual(detect_sparkline_subtype('winloss-sparkline'), SPARKLINE_WINLOSS)
        self.assertEqual(detect_sparkline_subtype('winloss'), SPARKLINE_WINLOSS)

    def test_none_for_non_sparkline(self):
        self.assertIsNone(detect_sparkline_subtype('bar'))
        self.assertIsNone(detect_sparkline_subtype('line'))
        self.assertIsNone(detect_sparkline_subtype(None))
        self.assertIsNone(detect_sparkline_subtype(''))

    def test_case_insensitive(self):
        self.assertEqual(detect_sparkline_subtype('AreaSparkline'), SPARKLINE_AREA)
        self.assertEqual(detect_sparkline_subtype('WINLOSS'), SPARKLINE_WINLOSS)

    def test_underscore_normalization(self):
        self.assertEqual(detect_sparkline_subtype('area_sparkline'), SPARKLINE_AREA)
        self.assertEqual(detect_sparkline_subtype('win_loss_sparkline'), SPARKLINE_WINLOSS)


class TestVisualTypeMapSparklineEntries(unittest.TestCase):
    """Tests for VISUAL_TYPE_MAP sparkline entries."""

    def test_line_sparkline_maps_to_line_chart(self):
        self.assertEqual(VISUAL_TYPE_MAP['sparkline'], 'lineChart')

    def test_area_sparkline_maps_to_area_chart(self):
        self.assertEqual(VISUAL_TYPE_MAP['areasparkline'], 'areaChart')
        self.assertEqual(VISUAL_TYPE_MAP['area-sparkline'], 'areaChart')

    def test_bar_sparkline_maps_to_column_chart(self):
        self.assertEqual(VISUAL_TYPE_MAP['barsparkline'], 'clusteredColumnChart')
        self.assertEqual(VISUAL_TYPE_MAP['bar-sparkline'], 'clusteredColumnChart')

    def test_column_sparkline_maps_to_column_chart(self):
        self.assertEqual(VISUAL_TYPE_MAP['columnsparkline'], 'clusteredColumnChart')
        self.assertEqual(VISUAL_TYPE_MAP['column-sparkline'], 'clusteredColumnChart')

    def test_winloss_sparkline_maps_to_column_chart(self):
        self.assertEqual(VISUAL_TYPE_MAP['winlosssparkline'], 'clusteredColumnChart')
        self.assertEqual(VISUAL_TYPE_MAP['winloss-sparkline'], 'clusteredColumnChart')
        self.assertEqual(VISUAL_TYPE_MAP['winloss'], 'clusteredColumnChart')


class TestAreaSparklineConfig(unittest.TestCase):
    """Tests for area sparkline configuration."""

    def test_area_sparkline_type_normalized(self):
        config = _build_sparkline_config('Sales', 'Orders', sparkline_type='area')
        self.assertEqual(config['sparklineType'], 'line')

    def test_area_sparkline_has_fill_color(self):
        config = _build_sparkline_config('Sales', 'Orders', sparkline_type='area',
                                         color='#00FF00')
        self.assertIn('fillColor', config)
        self.assertEqual(config['fillColor']['solid']['color'], '#00FF00')

    def test_area_sparkline_has_fill_opacity(self):
        config = _build_sparkline_config('Sales', 'Orders', sparkline_type='area')
        self.assertEqual(config['fillOpacity'], 30)

    def test_area_sparkline_preserves_points(self):
        config = _build_sparkline_config('Sales', 'Orders', sparkline_type='area')
        self.assertTrue(config['showHighPoint'])
        self.assertTrue(config['showLowPoint'])


class TestColumnSparklineConfig(unittest.TestCase):
    """Tests for bar/column sparkline configuration."""

    def test_column_sparkline_type(self):
        config = _build_sparkline_config('Revenue', 'Sales', sparkline_type='column')
        self.assertEqual(config['sparklineType'], 'column')

    def test_column_sparkline_no_fill(self):
        config = _build_sparkline_config('Revenue', 'Sales', sparkline_type='column')
        self.assertNotIn('fillColor', config)
        self.assertNotIn('fillOpacity', config)

    def test_column_sparkline_no_winloss_mode(self):
        config = _build_sparkline_config('Revenue', 'Sales', sparkline_type='column')
        self.assertNotIn('winLossMode', config)


class TestWinLossSparklineConfig(unittest.TestCase):
    """Tests for win/loss sparkline configuration."""

    def test_winloss_sparkline_type_normalized(self):
        config = _build_sparkline_config('Score', 'Games', sparkline_type='winloss')
        self.assertEqual(config['sparklineType'], 'column')

    def test_winloss_mode_enabled(self):
        config = _build_sparkline_config('Score', 'Games', sparkline_type='winloss')
        self.assertTrue(config['winLossMode'])

    def test_winloss_points_disabled(self):
        config = _build_sparkline_config('Score', 'Games', sparkline_type='winloss')
        self.assertFalse(config['showHighPoint'])
        self.assertFalse(config['showLowPoint'])

    def test_winloss_negative_color_default(self):
        config = _build_sparkline_config('Score', 'Games', sparkline_type='winloss')
        self.assertIn('negativeColor', config)
        self.assertEqual(config['negativeColor']['solid']['color'], '#D64550')

    def test_winloss_custom_colors_via_rules(self):
        rules = [
            {'threshold': 0, 'color': '#00FF00'},
            {'threshold': -1, 'color': '#FF0000'},
        ]
        config = _build_sparkline_config('Score', 'Games', sparkline_type='winloss',
                                         color_rules=rules)
        self.assertEqual(config['lineColor']['solid']['color'], '#00FF00')
        self.assertEqual(config['negativeColor']['solid']['color'], '#FF0000')

    def test_winloss_no_color_rules_array(self):
        """Win/loss should not produce colorRules (uses negativeColor instead)."""
        rules = [{'threshold': 0, 'color': '#00FF00'}]
        config = _build_sparkline_config('Score', 'Games', sparkline_type='winloss',
                                         color_rules=rules)
        self.assertNotIn('colorRules', config)


class TestSparklineConditionalFormatting(unittest.TestCase):
    """Tests for sparkline conditional formatting color rules."""

    def test_color_rules_on_line_sparkline(self):
        rules = [
            {'threshold': 50, 'color': '#FFA500'},
            {'threshold': 100, 'color': '#008000'},
        ]
        config = _build_sparkline_config('Sales', 'Orders', sparkline_type='line',
                                         color_rules=rules)
        self.assertIn('colorRules', config)
        self.assertEqual(len(config['colorRules']), 2)
        self.assertEqual(config['colorRules'][0]['value'], 50)
        self.assertEqual(config['colorRules'][0]['color']['solid']['color'], '#FFA500')

    def test_color_rules_on_area_sparkline(self):
        rules = [{'threshold': 0, 'color': '#FF0000'}]
        config = _build_sparkline_config('Sales', 'Orders', sparkline_type='area',
                                         color_rules=rules)
        self.assertIn('colorRules', config)
        self.assertEqual(len(config['colorRules']), 1)

    def test_color_rules_on_column_sparkline(self):
        rules = [
            {'threshold': 10, 'color': '#AAA'},
            {'threshold': 20, 'color': '#BBB'},
            {'threshold': 30, 'color': '#CCC'},
        ]
        config = _build_sparkline_config('Revenue', 'Sales', sparkline_type='column',
                                         color_rules=rules)
        self.assertIn('colorRules', config)
        self.assertEqual(len(config['colorRules']), 3)

    def test_no_color_rules_when_none(self):
        config = _build_sparkline_config('Sales', 'Orders')
        self.assertNotIn('colorRules', config)

    def test_empty_color_rules_ignored(self):
        config = _build_sparkline_config('Sales', 'Orders', color_rules=[])
        self.assertNotIn('colorRules', config)


class TestSparklineAxisRange(unittest.TestCase):
    """Tests for sparkline axis range propagation."""

    def test_axis_min_set(self):
        config = _build_sparkline_config('Sales', 'Orders', axis_min=0)
        self.assertEqual(config['axisMin'], 0)

    def test_axis_max_set(self):
        config = _build_sparkline_config('Sales', 'Orders', axis_max=1000)
        self.assertEqual(config['axisMax'], 1000)

    def test_both_axis_limits(self):
        config = _build_sparkline_config('Sales', 'Orders', axis_min=-100, axis_max=500)
        self.assertEqual(config['axisMin'], -100)
        self.assertEqual(config['axisMax'], 500)

    def test_no_axis_limits_by_default(self):
        config = _build_sparkline_config('Sales', 'Orders')
        self.assertNotIn('axisMin', config)
        self.assertNotIn('axisMax', config)

    def test_axis_zero_is_valid(self):
        config = _build_sparkline_config('Sales', 'Orders', axis_min=0, axis_max=0)
        self.assertEqual(config['axisMin'], 0)
        self.assertEqual(config['axisMax'], 0)


class TestSparklineBackwardsCompatibility(unittest.TestCase):
    """Ensure existing line/column sparkline API still works unchanged."""

    def test_line_default(self):
        config = _build_sparkline_config('Sales', 'Orders', 'OrderDate')
        self.assertEqual(config['type'], 'sparkline')
        self.assertEqual(config['sparklineType'], 'line')
        self.assertIn('field', config)
        self.assertIn('dateAxis', config)

    def test_column_type(self):
        config = _build_sparkline_config('Revenue', 'Sales', sparkline_type='column')
        self.assertEqual(config['sparklineType'], 'column')

    def test_custom_color(self):
        config = _build_sparkline_config('Profit', 'Orders', color='#FF0000')
        self.assertEqual(config['lineColor']['solid']['color'], '#FF0000')

    def test_show_points(self):
        config = _build_sparkline_config('X', 'T')
        self.assertTrue(config['showHighPoint'])
        self.assertTrue(config['showLowPoint'])
        self.assertFalse(config['showLastPoint'])

    def test_id_format(self):
        config = _build_sparkline_config('Revenue', 'SalesTable')
        self.assertEqual(config['id'], 'sparkline_Revenue')

    def test_line_width(self):
        config = _build_sparkline_config('X', 'T')
        self.assertEqual(config['lineWidth'], 2)

    def test_field_entity(self):
        config = _build_sparkline_config('Sales', 'MyTable')
        self.assertEqual(config['field']['Column']['Expression']['SourceRef']['Entity'], 'MyTable')

    def test_date_axis_entity(self):
        config = _build_sparkline_config('Sales', 'MyTable', 'MyDate')
        self.assertEqual(config['dateAxis']['Column']['Property'], 'MyDate')


class TestSparklineSubtypeMap(unittest.TestCase):
    """Tests for _SPARKLINE_SUBTYPE_MAP completeness."""

    def test_all_visual_type_map_sparklines_have_subtype(self):
        """Every sparkline entry in VISUAL_TYPE_MAP should have a subtype mapping."""
        sparkline_keys = [k for k in VISUAL_TYPE_MAP if 'sparkline' in k or k == 'winloss']
        for key in sparkline_keys:
            self.assertIn(key, _SPARKLINE_SUBTYPE_MAP,
                          f"Missing subtype mapping for '{key}'")

    def test_subtype_map_values_are_valid(self):
        valid = {SPARKLINE_LINE, SPARKLINE_COLUMN, SPARKLINE_AREA, SPARKLINE_WINLOSS}
        for key, subtype in _SPARKLINE_SUBTYPE_MAP.items():
            self.assertIn(subtype, valid, f"Invalid subtype '{subtype}' for '{key}'")


if __name__ == '__main__':
    unittest.main()
