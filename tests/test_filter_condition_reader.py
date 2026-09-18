"""Filter members live in <groupfilter member="...">, never in <value>.

The workbook-level reader looked only for ``<value>`` children — a shape
Tableau does not write — so all 167 filters in the example corpus reported no
values at all, and the generator correctly refused to emit a categorical filter
with an empty condition. The worksheet-level reader already read the real
shape, so the two disagreed about the same XML; they now share one function.
"""

import os
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))

from tableau_export.extract_tableau_data import (  # noqa: E402
    TableauExtractor, _read_filter_condition)


def _filter(inner='', attrs='class="categorical" column="[ds].[Category]"'):
    return ET.fromstring(f'<filter {attrs}>{inner}</filter>')


def _workbook(filter_xml):
    return ET.fromstring(
        f'<workbook><worksheets><worksheet name="Sheet">'
        f'<table>{filter_xml}</table></worksheet></worksheets></workbook>')


def _global_filters(filter_xml):
    extractor = TableauExtractor.__new__(TableauExtractor)
    extractor.workbook_data = {}
    extractor._xml_node_cache = {}
    extractor.extract_filters(_workbook(filter_xml))
    return extractor.workbook_data['filters']


class TestConditionReader(unittest.TestCase):

    def test_a_single_member_is_the_value(self):
        kind, values, lo, hi, exclude = _read_filter_condition(_filter(
            '<groupfilter function="member" member="&quot;Technology&quot;"/>'))
        self.assertEqual('categorical', kind)
        self.assertEqual(['"Technology"'], values)
        self.assertFalse(exclude)

    def test_a_union_collects_every_member(self):
        kind, values, _, _, _ = _read_filter_condition(_filter(
            '<groupfilter function="union">'
            '<groupfilter function="member" member="A"/>'
            '<groupfilter function="member" member="B"/>'
            '</groupfilter>'))
        self.assertEqual('categorical', kind)
        self.assertEqual(['A', 'B'], values)

    def test_an_except_keeps_its_members_and_flags_exclusion(self):
        kind, values, _, _, exclude = _read_filter_condition(_filter(
            '<groupfilter function="except">'
            '<groupfilter function="level-members" level="[x]"/>'
            '<groupfilter function="member" member="&quot;Cancelled&quot;"/>'
            '</groupfilter>'))
        self.assertEqual('categorical', kind)
        self.assertEqual(['"Cancelled"'], values)
        self.assertTrue(exclude)

    def test_a_numeric_range_becomes_a_range(self):
        kind, _, lo, hi, _ = _read_filter_condition(_filter(
            '<groupfilter function="range" from="10" to="90"/>'))
        self.assertEqual('range', kind)
        self.assertEqual(('10', '90'), (lo, hi))

    def test_plain_value_children_are_still_read(self):
        _, values, _, _, _ = _read_filter_condition(
            _filter('<value>North</value><value>South</value>'))
        self.assertEqual(['North', 'South'], values)


class TestNothingIsInvented(unittest.TestCase):
    """Reading members from the wrong place is how this went wrong once."""

    def test_level_members_means_everything_not_a_value_list(self):
        kind, values, _, _, _ = _read_filter_condition(_filter(
            '<groupfilter function="level-members" level="[Category]"/>'))
        self.assertEqual('all', kind)
        self.assertEqual([], values)

    def test_a_text_range_means_everything(self):
        kind, _, lo, hi, _ = _read_filter_condition(_filter(
            '<groupfilter function="range" from="A" to="Z"/>'))
        self.assertEqual('all', kind)
        self.assertIsNone(lo)
        self.assertIsNone(hi)

    def test_a_crossjoin_action_filter_means_everything(self):
        kind, values, _, _, _ = _read_filter_condition(_filter(
            '<groupfilter function="crossjoin"/>'))
        self.assertEqual('all', kind)
        self.assertEqual([], values)

    def test_an_empty_filter_yields_nothing(self):
        kind, values, lo, hi, exclude = _read_filter_condition(_filter())
        self.assertEqual(('', [], None, None, False),
                         (kind, values, lo, hi, exclude))


class TestBothReadersAgree(unittest.TestCase):
    """They did not, which is why the workbook-level list was always empty."""

    SHAPES = (
        '<filter class="categorical" column="[ds].[Category]">'
        '<groupfilter function="member" member="&quot;Technology&quot;"/></filter>',
        '<filter class="categorical" column="[ds].[Region]">'
        '<groupfilter function="union">'
        '<groupfilter function="member" member="North"/>'
        '<groupfilter function="member" member="South"/></groupfilter></filter>',
        '<filter class="categorical" column="[ds].[Status]">'
        '<groupfilter function="except">'
        '<groupfilter function="member" member="Cancelled"/></groupfilter></filter>',
    )

    def test_the_same_xml_yields_the_same_values_at_both_levels(self):
        for shape in self.SHAPES:
            with self.subTest(shape=shape[:60]):
                worksheet = ET.fromstring(f'<worksheet>{shape}</worksheet>')
                per_sheet = TableauExtractor.extract_worksheet_filters(
                    TableauExtractor.__new__(TableauExtractor), worksheet)
                per_book = _global_filters(shape)
                self.assertEqual(per_sheet[0]['values'], per_book[0]['values'])
                self.assertEqual(per_sheet[0]['exclude'], per_book[0]['exclude'])

    def test_the_workbook_level_list_now_carries_values(self):
        filters = _global_filters(self.SHAPES[1])
        self.assertEqual(['North', 'South'], filters[0]['values'])


class TestTopN(unittest.TestCase):
    """A top-N cut-off is a rank, not a bound on the measure."""

    XML = ('<filter class="topn" column="[ds].[sum:amount:qk]" '
           'direction="top" max="10"/>')

    def test_a_top_n_filter_is_not_read_as_a_range(self):
        found = _global_filters(self.XML)[0]
        self.assertEqual('top-n', found['filter_mode'])
        self.assertEqual(10, found['top_n_count'])
        self.assertEqual('top', found['top_n_direction'])

    def test_a_top_n_filter_exposes_no_range_bound(self):
        found = _global_filters(self.XML)[0]
        self.assertIsNone(found.get('min'))
        self.assertIsNone(found.get('max'))


if __name__ == '__main__':
    unittest.main()
