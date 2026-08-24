"""Tests for Sprint 172 — Motion Chart Workaround.

Covers: motion chart detection, bookmark sequence generation, action
button creation, assessment integration, migration note annotation.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.visual_generator import (
    _build_motion_chart_bookmarks,
    _build_motion_chart_action_button,
    has_motion_chart,
)


class TestMotionChartDetection(unittest.TestCase):
    """Tests for has_motion_chart() detection."""

    def test_detects_pages_shelf_with_field(self):
        ws = {'pages_shelf': {'field': 'Year', 'datasource': 'ds1'}}
        self.assertTrue(has_motion_chart(ws))

    def test_no_pages_shelf(self):
        ws = {'name': 'Sheet1', 'fields': []}
        self.assertFalse(has_motion_chart(ws))

    def test_empty_pages_shelf(self):
        ws = {'pages_shelf': {}}
        self.assertFalse(has_motion_chart(ws))

    def test_pages_shelf_no_field(self):
        ws = {'pages_shelf': {'datasource': 'ds1'}}
        self.assertFalse(has_motion_chart(ws))

    def test_none_worksheet(self):
        self.assertFalse(has_motion_chart(None))

    def test_pages_shelf_not_dict(self):
        ws = {'pages_shelf': 'Year'}
        self.assertFalse(has_motion_chart(ws))


class TestMotionChartBookmarks(unittest.TestCase):
    """Tests for _build_motion_chart_bookmarks()."""

    def test_basic_bookmark_generation(self):
        bms = _build_motion_chart_bookmarks('Year', [2020, 2021, 2022], 'Page1')
        self.assertEqual(len(bms), 3)

    def test_bookmark_display_names(self):
        bms = _build_motion_chart_bookmarks('Year', [2020, 2021], 'Page1', 'Sales')
        self.assertIn('Year = 2020', bms[0]['displayName'])
        self.assertIn('Sales', bms[0]['displayName'])

    def test_bookmark_has_name_id(self):
        bms = _build_motion_chart_bookmarks('Year', [2020], 'Page1')
        self.assertTrue(bms[0]['name'].startswith('Motion_'))

    def test_bookmark_exploration_state(self):
        bms = _build_motion_chart_bookmarks('Year', [2020], 'MyPage')
        self.assertEqual(bms[0]['explorationState']['activeSection'], 'MyPage')

    def test_bookmark_filter_values(self):
        bms = _build_motion_chart_bookmarks('Year', [2020], 'Page1')
        filters = bms[0]['explorationState']['filters']
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0]['field'], 'Year')
        self.assertEqual(filters[0]['values'], [2020])

    def test_bookmark_filter_type_categorical(self):
        bms = _build_motion_chart_bookmarks('Year', [2020], 'Page1')
        self.assertEqual(bms[0]['explorationState']['filters'][0]['type'], 'Categorical')

    def test_bookmark_options_motion_flag(self):
        bms = _build_motion_chart_bookmarks('Year', [2020, 2021, 2022], 'Page1')
        self.assertTrue(bms[0]['options']['motionChart'])
        self.assertEqual(bms[0]['options']['frameIndex'], 0)
        self.assertEqual(bms[0]['options']['frameCount'], 3)

    def test_bookmark_frame_indices(self):
        bms = _build_motion_chart_bookmarks('Year', ['A', 'B', 'C', 'D'], 'Page1')
        indices = [bm['options']['frameIndex'] for bm in bms]
        self.assertEqual(indices, [0, 1, 2, 3])

    def test_empty_values_returns_empty(self):
        bms = _build_motion_chart_bookmarks('Year', [], 'Page1')
        self.assertEqual(len(bms), 0)

    def test_single_value(self):
        bms = _build_motion_chart_bookmarks('Year', [2020], 'Page1')
        self.assertEqual(len(bms), 1)
        self.assertEqual(bms[0]['options']['frameCount'], 1)

    def test_string_values(self):
        bms = _build_motion_chart_bookmarks('Region', ['East', 'West', 'North'], 'Page1')
        self.assertEqual(len(bms), 3)
        self.assertIn('Region = East', bms[0]['displayName'])

    def test_default_worksheet_name(self):
        bms = _build_motion_chart_bookmarks('Year', [2020], 'Page1')
        self.assertIn('Motion', bms[0]['displayName'])


class TestMotionChartActionButton(unittest.TestCase):
    """Tests for _build_motion_chart_action_button()."""

    def test_basic_action_button(self):
        btn = _build_motion_chart_action_button(['bm1', 'bm2'], 'Page1')
        self.assertIn('visual', btn)
        self.assertEqual(btn['visual']['visualType'], 'actionButton')

    def test_action_button_position(self):
        btn = _build_motion_chart_action_button(['bm1'], 'Page1', x=50, y=100)
        self.assertEqual(btn['position']['x'], 50)
        self.assertEqual(btn['position']['y'], 100)

    def test_action_button_size(self):
        btn = _build_motion_chart_action_button(['bm1'], 'Page1', width=200, height=50)
        self.assertEqual(btn['position']['width'], 200)
        self.assertEqual(btn['position']['height'], 50)

    def test_action_button_has_play_icon(self):
        btn = _build_motion_chart_action_button(['bm1'], 'Page1')
        icon = btn['visual']['objects']['icon']
        self.assertEqual(len(icon), 1)

    def test_action_button_text(self):
        btn = _build_motion_chart_action_button(['bm1'], 'Page1')
        text_props = btn['visual']['objects']['text'][0]['properties']
        self.assertIn('text', text_props)

    def test_action_button_bookmark_reference(self):
        btn = _build_motion_chart_action_button(['bm1', 'bm2', 'bm3'], 'Page1')
        self.assertEqual(btn['_motionBookmarks'], ['bm1', 'bm2', 'bm3'])
        self.assertEqual(btn['_motionPageName'], 'Page1')

    def test_empty_bookmark_list(self):
        btn = _build_motion_chart_action_button([], 'Page1')
        self.assertEqual(btn['_motionBookmarks'], [])

    def test_has_schema(self):
        btn = _build_motion_chart_action_button(['bm1'], 'Page1')
        self.assertIn('$schema', btn)

    def test_unique_visual_id(self):
        btn1 = _build_motion_chart_action_button(['bm1'], 'Page1')
        btn2 = _build_motion_chart_action_button(['bm1'], 'Page1')
        self.assertNotEqual(btn1['name'], btn2['name'])


class TestAssessmentMotionChart(unittest.TestCase):
    """Tests for motion chart assessment warnings."""

    def test_assessment_pages_shelf_warning(self):
        from powerbi_import.assessment import _check_interactivity
        extracted = {
            'worksheets': [{'name': 'Sheet1', 'pages_shelf': {'field': 'Year'}}],
            'dashboards': [],
            'actions': [],
            'stories': [],
        }
        cat = _check_interactivity(extracted)
        warn_checks = [c for c in cat.checks if 'Pages Shelf' in c.name]
        self.assertEqual(len(warn_checks), 1)
        self.assertIn('bookmark sequence', warn_checks[0].recommendation.lower())

    def test_assessment_no_pages_shelf(self):
        from powerbi_import.assessment import _check_interactivity
        extracted = {
            'worksheets': [{'name': 'Sheet1', 'pages_shelf': {}}],
            'dashboards': [],
            'actions': [],
            'stories': [],
        }
        cat = _check_interactivity(extracted)
        warn_checks = [c for c in cat.checks if 'Pages Shelf' in c.name]
        self.assertEqual(len(warn_checks), 0)


if __name__ == '__main__':
    unittest.main()
