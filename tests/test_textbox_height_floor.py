"""A textbox has to be tall enough for its own font.

The PBIR validator reports PBIR_TEXTBOX_HEIGHT_BELOW_FLOOR when a textbox is
shorter than its text needs (18pt wants 45px, 16pt wants 41px, both including
Power BI's 8+8px padding); below that Power BI draws a scrollbar over the text.
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'tableau_export'))

from powerbi_import.pbip_generator import (PowerBIProjectGenerator,
                                           _TEXTBOX_VERTICAL_PADDING)


def _generator():
    return PowerBIProjectGenerator.__new__(PowerBIProjectGenerator)


def _paragraphs(*sizes):
    runs = []
    for size in sizes:
        run = {'value': 'x'}
        if size is not None:
            run['textStyle'] = {'fontSize': '%spt' % size}
        runs.append(run)
    return [{'textRuns': runs}]


class TestTextboxHeightFloor(unittest.TestCase):

    def test_floor_clears_the_validator_minimum(self):
        # The validator's own numbers, quoted in its diagnostics.
        for points, validator_minimum in ((18, 45), (16, 41)):
            with self.subTest(points=points):
                floor = _generator()._textbox_height_floor(_paragraphs(points))
                self.assertGreaterEqual(floor, validator_minimum)

    def test_the_largest_run_sets_the_floor(self):
        gen = _generator()
        self.assertEqual(gen._textbox_height_floor(_paragraphs(9, 18, 11)),
                         gen._textbox_height_floor(_paragraphs(18)))

    def test_a_textbox_without_a_declared_size_is_not_resized(self):
        self.assertEqual(_generator()._textbox_height_floor(_paragraphs(None)), 0)
        self.assertEqual(_generator()._textbox_height_floor([]), 0)

    def test_an_unparsable_size_is_ignored_rather_than_crashing(self):
        paragraphs = [{'textRuns': [{'value': 'x',
                                     'textStyle': {'fontSize': 'large'}}]}]
        self.assertEqual(_generator()._textbox_height_floor(paragraphs), 0)

    def test_floor_is_padding_plus_the_rendered_line(self):
        floor = _generator()._textbox_height_floor(_paragraphs(12))
        expected = int(math.ceil(12 * 4 / 3 * 1.2)) + _TEXTBOX_VERTICAL_PADDING
        self.assertEqual(floor, expected)


if __name__ == '__main__':
    unittest.main()
