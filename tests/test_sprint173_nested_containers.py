"""Tests for Sprint 173 — Nested Container Solver.

Covers: recursive layout constraint solving for 4+ level nesting,
overflow detection, z-order preservation, padding/margin inheritance,
nesting depth calculation.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.visual_generator import (
    solve_nested_layout,
    get_nesting_depth,
    _fix_overflow,
    MIN_VISUAL_DIM,
    DEFAULT_CONTAINER_PADDING,
)


class TestNestingDepth(unittest.TestCase):
    """Tests for get_nesting_depth()."""

    def test_none_returns_zero(self):
        self.assertEqual(get_nesting_depth(None), 0)

    def test_empty_returns_zero(self):
        self.assertEqual(get_nesting_depth({}), 0)

    def test_leaf_returns_zero(self):
        self.assertEqual(get_nesting_depth({'name': 'A'}), 0)

    def test_one_level(self):
        z = {'name': 'root', 'children': [{'name': 'A'}, {'name': 'B'}]}
        self.assertEqual(get_nesting_depth(z), 1)

    def test_two_levels(self):
        z = {'name': 'root', 'children': [
            {'name': 'A', 'children': [{'name': 'A1'}]},
        ]}
        self.assertEqual(get_nesting_depth(z), 2)

    def test_four_levels(self):
        z = {'name': 'L0', 'children': [
            {'name': 'L1', 'children': [
                {'name': 'L2', 'children': [
                    {'name': 'L3', 'children': [
                        {'name': 'L4'}
                    ]}
                ]}
            ]}
        ]}
        self.assertEqual(get_nesting_depth(z), 4)

    def test_asymmetric_tree(self):
        z = {'name': 'root', 'children': [
            {'name': 'shallow'},
            {'name': 'deep', 'children': [
                {'name': 'd1', 'children': [{'name': 'd2'}]}
            ]},
        ]}
        self.assertEqual(get_nesting_depth(z), 3)


class TestSolveNestedLayoutBasic(unittest.TestCase):
    """Basic tests for solve_nested_layout()."""

    def test_empty_hierarchy(self):
        self.assertEqual(solve_nested_layout(None), {})
        self.assertEqual(solve_nested_layout({}), {})

    def test_single_leaf(self):
        z = {'name': 'Sheet1'}
        layout = solve_nested_layout(z, 1280, 720)
        self.assertIn('Sheet1', layout)
        self.assertGreater(layout['Sheet1']['w'], 0)
        self.assertGreater(layout['Sheet1']['h'], 0)

    def test_horizontal_split(self):
        z = {
            'name': 'root', 'orientation': 'horz',
            'children': [
                {'name': 'Left', 'position': {'w': 1}},
                {'name': 'Right', 'position': {'w': 1}},
            ]
        }
        layout = solve_nested_layout(z, 1000, 500)
        self.assertIn('Left', layout)
        self.assertIn('Right', layout)
        # Left should be on the left side
        self.assertLess(layout['Left']['x'], layout['Right']['x'])

    def test_vertical_split(self):
        z = {
            'name': 'root', 'orientation': 'vert',
            'children': [
                {'name': 'Top', 'position': {'h': 1}},
                {'name': 'Bottom', 'position': {'h': 1}},
            ]
        }
        layout = solve_nested_layout(z, 1000, 500)
        self.assertIn('Top', layout)
        self.assertIn('Bottom', layout)
        self.assertLess(layout['Top']['y'], layout['Bottom']['y'])

    def test_three_level_nesting(self):
        z = {
            'name': 'root', 'orientation': 'horz',
            'children': [
                {'name': 'col1', 'position': {'w': 1}, 'orientation': 'vert',
                 'children': [
                     {'name': 'A', 'position': {'h': 1}},
                     {'name': 'B', 'position': {'h': 1}},
                 ]},
                {'name': 'col2', 'position': {'w': 1}},
            ]
        }
        layout = solve_nested_layout(z, 1000, 500)
        self.assertIn('A', layout)
        self.assertIn('B', layout)
        self.assertIn('col2', layout)


class TestDeepNesting(unittest.TestCase):
    """Tests for 4+ level deep nesting."""

    def _make_deep_tree(self, depth, orientation='horz'):
        """Create a zone hierarchy with given depth."""
        if depth <= 0:
            return {'name': f'Leaf_d{depth}'}
        child = self._make_deep_tree(depth - 1, 'vert' if orientation == 'horz' else 'horz')
        child['position'] = {'w': 1, 'h': 1}
        return {
            'name': f'Level_{depth}',
            'orientation': orientation,
            'children': [child, {'name': f'Sibling_{depth}', 'position': {'w': 1, 'h': 1}}],
        }

    def test_four_level_nesting(self):
        z = self._make_deep_tree(4)
        layout = solve_nested_layout(z, 1280, 720)
        self.assertGreater(len(layout), 4)
        # All visuals should have positive dimensions
        for key, rect in layout.items():
            self.assertGreaterEqual(rect['w'], MIN_VISUAL_DIM, f"{key} width too small")
            self.assertGreaterEqual(rect['h'], MIN_VISUAL_DIM, f"{key} height too small")

    def test_five_level_nesting(self):
        z = self._make_deep_tree(5)
        layout = solve_nested_layout(z, 1280, 720)
        self.assertGreater(len(layout), 5)

    def test_six_level_nesting(self):
        z = self._make_deep_tree(6)
        layout = solve_nested_layout(z, 1280, 720)
        self.assertGreater(len(layout), 6)
        # Even at 6 levels, no visual should overflow the page
        for key, rect in layout.items():
            self.assertLessEqual(rect['x'] + rect['w'], 1280 + 1,
                                 f"{key} overflows right edge")
            self.assertLessEqual(rect['y'] + rect['h'], 720 + 1,
                                 f"{key} overflows bottom edge")

    def test_max_depth_safety(self):
        """Layout should not crash with very deep nesting."""
        z = self._make_deep_tree(15)
        layout = solve_nested_layout(z, 1280, 720, max_depth=10)
        # Should still produce some layout (depth-limited)
        self.assertGreater(len(layout), 0)


class TestZOrderPreservation(unittest.TestCase):
    """Tests for z-order assignment."""

    def test_z_order_increases(self):
        z = {
            'name': 'root', 'orientation': 'horz',
            'children': [
                {'name': 'A', 'position': {'w': 1}},
                {'name': 'B', 'position': {'w': 1}},
                {'name': 'C', 'position': {'w': 1}},
            ]
        }
        layout = solve_nested_layout(z, 1000, 500)
        z_values = [layout[k]['z'] for k in ['A', 'B', 'C']]
        self.assertEqual(z_values, sorted(z_values))

    def test_z_order_unique(self):
        z = {
            'name': 'root', 'orientation': 'vert',
            'children': [
                {'name': 'X', 'position': {'h': 1}},
                {'name': 'Y', 'position': {'h': 1}},
            ]
        }
        layout = solve_nested_layout(z, 1000, 500)
        z_x = layout['X']['z']
        z_y = layout['Y']['z']
        self.assertNotEqual(z_x, z_y)

    def test_depth_recorded(self):
        z = {
            'name': 'root', 'orientation': 'horz',
            'children': [
                {'name': 'child', 'position': {'w': 1}, 'orientation': 'vert',
                 'children': [{'name': 'grandchild', 'position': {'h': 1}}]},
            ]
        }
        layout = solve_nested_layout(z, 1000, 500)
        self.assertEqual(layout['grandchild']['depth'], 2)


class TestPaddingInheritance(unittest.TestCase):
    """Tests for padding/margin inheritance."""

    def test_default_padding_applied(self):
        z = {'name': 'Sheet1', 'padding': None}
        layout = solve_nested_layout(z, 100, 100)
        rect = layout['Sheet1']
        # With default padding (None triggers DEFAULT_CONTAINER_PADDING), the visual should be inset
        self.assertGreater(rect['x'], 0)
        self.assertGreater(rect['y'], 0)

    def test_custom_padding(self):
        z = {'name': 'Sheet1', 'padding': 10}
        layout = solve_nested_layout(z, 200, 200)
        rect = layout['Sheet1']
        self.assertEqual(rect['x'], 10)
        self.assertEqual(rect['y'], 10)

    def test_zero_padding(self):
        z = {'name': 'Sheet1', 'padding': 0}
        layout = solve_nested_layout(z, 200, 200)
        rect = layout['Sheet1']
        self.assertEqual(rect['x'], 0)
        self.assertEqual(rect['y'], 0)

    def test_margin_between_children(self):
        z = {
            'name': 'root', 'orientation': 'horz', 'padding': 0, 'margin': 10,
            'children': [
                {'name': 'A', 'position': {'w': 1}, 'padding': 0},
                {'name': 'B', 'position': {'w': 1}, 'padding': 0},
            ]
        }
        layout = solve_nested_layout(z, 1000, 500)
        # B should start after A + margin gap
        gap = layout['B']['x'] - (layout['A']['x'] + layout['A']['w'])
        self.assertGreaterEqual(gap, 9)  # Allow rounding


class TestOverflowDetection(unittest.TestCase):
    """Tests for overflow detection and auto-resize."""

    def test_fix_right_overflow(self):
        layout = {'A': {'x': 1200, 'y': 0, 'w': 200, 'h': 100, 'z': 1, 'depth': 0}}
        _fix_overflow(layout, 1280, 720)
        self.assertLessEqual(layout['A']['x'] + layout['A']['w'], 1280)

    def test_fix_bottom_overflow(self):
        layout = {'A': {'x': 0, 'y': 650, 'w': 100, 'h': 200, 'z': 1, 'depth': 0}}
        _fix_overflow(layout, 1280, 720)
        self.assertLessEqual(layout['A']['y'] + layout['A']['h'], 720)

    def test_fix_negative_position(self):
        layout = {'A': {'x': -10, 'y': -20, 'w': 100, 'h': 100, 'z': 1, 'depth': 0}}
        _fix_overflow(layout, 1280, 720)
        self.assertEqual(layout['A']['x'], 0)
        self.assertEqual(layout['A']['y'], 0)

    def test_minimum_dimension_enforced(self):
        layout = {'A': {'x': 1270, 'y': 0, 'w': 200, 'h': 100, 'z': 1, 'depth': 0}}
        _fix_overflow(layout, 1280, 720)
        self.assertGreaterEqual(layout['A']['w'], MIN_VISUAL_DIM)


class TestFloatingChildren(unittest.TestCase):
    """Tests for floating (absolute positioned) children."""

    def test_floating_child_positioned(self):
        z = {
            'name': 'root', 'orientation': 'horz', 'padding': 0,
            'children': [
                {'name': 'tiled', 'position': {'w': 1}},
                {'name': 'floating', 'is_floating': True,
                 'position': {'x': 100, 'y': 50, 'w': 200, 'h': 150}},
            ]
        }
        layout = solve_nested_layout(z, 1000, 500)
        self.assertIn('floating', layout)
        self.assertIn('tiled', layout)

    def test_floating_and_tiled_coexist(self):
        z = {
            'name': 'root', 'padding': 0,
            'children': [
                {'name': 'A', 'position': {'w': 1, 'h': 1}},
                {'name': 'F', 'is_floating': True,
                 'position': {'x': 0, 'y': 0, 'w': 100, 'h': 100}},
            ]
        }
        layout = solve_nested_layout(z, 500, 500)
        self.assertIn('A', layout)
        self.assertIn('F', layout)


class TestProportionalLayout(unittest.TestCase):
    """Tests for 2D proportional layout (no explicit orientation)."""

    def test_two_d_grid(self):
        z = {
            'name': 'root', 'padding': 0,
            'children': [
                {'name': 'TL', 'position': {'x': 0, 'y': 0, 'w': 50, 'h': 50}},
                {'name': 'TR', 'position': {'x': 50, 'y': 0, 'w': 50, 'h': 50}},
                {'name': 'BL', 'position': {'x': 0, 'y': 50, 'w': 50, 'h': 50}},
                {'name': 'BR', 'position': {'x': 50, 'y': 50, 'w': 50, 'h': 50}},
            ]
        }
        layout = solve_nested_layout(z, 1000, 1000)
        # Top-right should be to the right of top-left
        self.assertGreater(layout['TR']['x'], layout['TL']['x'])
        # Bottom-left should be below top-left
        self.assertGreater(layout['BL']['y'], layout['TL']['y'])


class TestFilterZoneTypes(unittest.TestCase):
    """Tests that filter/paramctrl/color/title zones are excluded."""

    def test_filter_zone_excluded(self):
        z = {
            'name': 'root', 'orientation': 'horz',
            'children': [
                {'name': 'Sheet1', 'position': {'w': 1}},
                {'name': 'FilterZone', 'zone_type': 'filter', 'position': {'w': 1}},
            ]
        }
        layout = solve_nested_layout(z, 1000, 500)
        self.assertIn('Sheet1', layout)
        self.assertNotIn('FilterZone', layout)

    def test_paramctrl_zone_excluded(self):
        z = {'name': 'Param', 'zone_type': 'paramctrl'}
        layout = solve_nested_layout(z, 1000, 500)
        self.assertNotIn('Param', layout)


if __name__ == '__main__':
    unittest.main()
