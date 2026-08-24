"""Tests for Sprint 174 — Rich Tooltip Preservation.

Covers: tooltip field extraction, data role generation, formatting
metadata, tooltip size estimation, and edge cases.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.visual_generator import (
    build_rich_tooltip_config,
    build_tooltip_data_roles,
    build_tooltip_formatting,
    estimate_tooltip_size,
    TOOLTIP_PAGE_WIDTH,
    TOOLTIP_PAGE_HEIGHT,
    TOOLTIP_MIN_HEIGHT,
    TOOLTIP_MAX_HEIGHT,
)


class TestBuildRichTooltipConfig(unittest.TestCase):
    """Tests for build_rich_tooltip_config()."""

    def test_none_tooltips(self):
        result = build_rich_tooltip_config(None)
        self.assertEqual(result['fields'], [])
        self.assertFalse(result['has_custom_text'])

    def test_empty_list(self):
        result = build_rich_tooltip_config([])
        self.assertEqual(result['fields'], [])
        self.assertFalse(result['has_custom_text'])

    def test_non_text_type_ignored(self):
        tips = [{'type': 'viz_in_tooltip', 'worksheet': 'Sheet2'}]
        result = build_rich_tooltip_config(tips)
        self.assertEqual(result['fields'], [])
        self.assertFalse(result['has_custom_text'])

    def test_text_without_runs(self):
        tips = [{'type': 'text', 'content': 'Hello'}]
        result = build_rich_tooltip_config(tips)
        self.assertEqual(result['fields'], [])
        self.assertFalse(result['has_custom_text'])

    def test_field_ref_extracted(self):
        tips = [{'type': 'text', 'content': '[Sales]', 'runs': [
            {'text': '[Sales]', 'field_ref': 'Sales'}
        ]}]
        result = build_rich_tooltip_config(tips, table_name='FactSales')
        self.assertEqual(len(result['fields']), 1)
        self.assertEqual(result['fields'][0]['field'], 'Sales')
        self.assertEqual(result['fields'][0]['table'], 'FactSales')
        self.assertTrue(result['has_custom_text'])

    def test_multiple_field_refs(self):
        tips = [{'type': 'text', 'runs': [
            {'text': '[Sales]', 'field_ref': 'Sales'},
            {'text': ': '},
            {'text': '[Profit]', 'field_ref': 'Profit'},
        ]}]
        result = build_rich_tooltip_config(tips)
        self.assertEqual(len(result['fields']), 2)
        fields = [f['field'] for f in result['fields']]
        self.assertIn('Sales', fields)
        self.assertIn('Profit', fields)

    def test_duplicate_field_refs_deduplicated(self):
        tips = [{'type': 'text', 'runs': [
            {'text': '[Sales]', 'field_ref': 'Sales'},
            {'text': '[Sales]', 'field_ref': 'Sales'},
        ]}]
        result = build_rich_tooltip_config(tips)
        self.assertEqual(len(result['fields']), 1)

    def test_formatting_preserved(self):
        tips = [{'type': 'text', 'runs': [
            {'text': '[Revenue]', 'field_ref': 'Revenue', 'bold': True,
             'color': '#FF0000', 'font_size': '14'}
        ]}]
        result = build_rich_tooltip_config(tips)
        f = result['fields'][0]
        self.assertTrue(f['bold'])
        self.assertEqual(f['color'], '#FF0000')
        self.assertEqual(f['font_size'], '14')

    def test_non_dict_items_skipped(self):
        tips = [None, 'text', 42, {'type': 'text', 'runs': [
            {'text': '[A]', 'field_ref': 'A'}
        ]}]
        result = build_rich_tooltip_config(tips)
        self.assertEqual(len(result['fields']), 1)


class TestBuildTooltipDataRoles(unittest.TestCase):
    """Tests for build_tooltip_data_roles()."""

    def test_empty_config(self):
        self.assertEqual(build_tooltip_data_roles({}), [])

    def test_single_field(self):
        config = {'fields': [{'field': 'Sales', 'table': 'Fact'}]}
        roles = build_tooltip_data_roles(config)
        self.assertEqual(len(roles), 1)
        self.assertEqual(roles[0]['role'], 'Tooltips')
        self.assertEqual(roles[0]['column'], 'Sales')
        self.assertEqual(roles[0]['table'], 'Fact')

    def test_no_table(self):
        config = {'fields': [{'field': 'Profit', 'table': ''}]}
        roles = build_tooltip_data_roles(config)
        self.assertEqual(len(roles), 1)
        self.assertNotIn('table', roles[0])

    def test_empty_field_name_skipped(self):
        config = {'fields': [{'field': '', 'table': 'T'}]}
        roles = build_tooltip_data_roles(config)
        self.assertEqual(len(roles), 0)

    def test_multiple_fields(self):
        config = {'fields': [
            {'field': 'A', 'table': 'T1'},
            {'field': 'B', 'table': 'T2'},
        ]}
        roles = build_tooltip_data_roles(config)
        self.assertEqual(len(roles), 2)


class TestBuildTooltipFormatting(unittest.TestCase):
    """Tests for build_tooltip_formatting()."""

    def test_none_returns_empty(self):
        self.assertEqual(build_tooltip_formatting(None), [])

    def test_empty_returns_empty(self):
        self.assertEqual(build_tooltip_formatting([]), [])

    def test_basic_formatting(self):
        tips = [{'type': 'text', 'runs': [
            {'text': 'Revenue: ', 'bold': True},
            {'text': '[Revenue]', 'field_ref': 'Revenue'},
        ]}]
        fmt = build_tooltip_formatting(tips)
        self.assertEqual(len(fmt), 2)
        self.assertTrue(fmt[0]['bold'])
        self.assertFalse(fmt[0]['is_field'])
        self.assertTrue(fmt[1]['is_field'])

    def test_color_and_size(self):
        tips = [{'type': 'text', 'runs': [
            {'text': 'Total', 'color': '#00FF00', 'font_size': '16'}
        ]}]
        fmt = build_tooltip_formatting(tips)
        self.assertEqual(fmt[0]['color'], '#00FF00')
        self.assertEqual(fmt[0]['font_size'], '16')

    def test_non_text_skipped(self):
        tips = [{'type': 'viz_in_tooltip', 'worksheet': 'S'}]
        fmt = build_tooltip_formatting(tips)
        self.assertEqual(len(fmt), 0)


class TestEstimateTooltipSize(unittest.TestCase):
    """Tests for estimate_tooltip_size()."""

    def test_none_returns_defaults(self):
        w, h = estimate_tooltip_size(None)
        self.assertEqual(w, TOOLTIP_PAGE_WIDTH)
        self.assertEqual(h, TOOLTIP_PAGE_HEIGHT)

    def test_empty_returns_defaults(self):
        w, h = estimate_tooltip_size([])
        self.assertEqual(w, TOOLTIP_PAGE_WIDTH)
        self.assertEqual(h, TOOLTIP_PAGE_HEIGHT)

    def test_single_line(self):
        tips = [{'type': 'text', 'runs': [{'text': 'Hello'}]}]
        w, h = estimate_tooltip_size(tips)
        self.assertEqual(w, TOOLTIP_PAGE_WIDTH)
        self.assertGreaterEqual(h, TOOLTIP_MIN_HEIGHT)

    def test_many_lines_increases_height(self):
        runs = [{'text': f'Line {i}\n'} for i in range(15)]
        tips = [{'type': 'text', 'runs': runs}]
        w, h = estimate_tooltip_size(tips)
        self.assertGreater(h, TOOLTIP_MIN_HEIGHT)

    def test_max_height_capped(self):
        runs = [{'text': f'Line {i}\n'} for i in range(100)]
        tips = [{'type': 'text', 'runs': runs}]
        w, h = estimate_tooltip_size(tips)
        self.assertLessEqual(h, TOOLTIP_MAX_HEIGHT)

    def test_viz_in_tooltip_uses_standard_height(self):
        tips = [{'type': 'viz_in_tooltip', 'worksheet': 'Detail'}]
        w, h = estimate_tooltip_size(tips)
        self.assertGreaterEqual(h, TOOLTIP_PAGE_HEIGHT)

    def test_mixed_text_and_viz(self):
        tips = [
            {'type': 'text', 'runs': [{'text': 'Info'}]},
            {'type': 'viz_in_tooltip', 'worksheet': 'Chart'},
        ]
        w, h = estimate_tooltip_size(tips)
        self.assertGreaterEqual(h, TOOLTIP_PAGE_HEIGHT)

    def test_width_always_base_width(self):
        tips = [{'type': 'text', 'runs': [{'text': 'X' * 200}]}]
        w, h = estimate_tooltip_size(tips, base_width=600)
        self.assertEqual(w, 600)


if __name__ == '__main__':
    unittest.main()
