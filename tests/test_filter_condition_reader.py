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

from tableau_export.extract_tableau_data import TableauExtractor, _read_filter_condition  # noqa: E402


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

    def test_relevant_inclusive_union_is_preserved_when_all_is_unproven(self):
        with self.assertLogs('extract_primitives', level='WARNING'):
            kind, values, _, _, _ = _read_filter_condition(_filter(
                '<groupfilter function="union" ui-domain="relevant" '
                'ui-enumeration="inclusive" ui-marker="enumerate">'
                '<groupfilter function="member" member="A"/>'
                '<groupfilter function="member" member="B"/>'
                '</groupfilter>'))
        self.assertEqual('categorical', kind)
        self.assertEqual(['A', 'B'], values)

    def test_explicit_all_does_not_materialize_a_member_snapshot(self):
        kind, values, _, _, _ = _read_filter_condition(_filter(
            '<groupfilter function="union" ui-domain="relevant" '
            'ui-enumeration="inclusive" ui-marker="enumerate">'
            '<groupfilter function="member" member="A"/>'
            '</groupfilter>',
            attrs='class="categorical" column="[ds].[Category]" included-values="all"'))
        self.assertEqual(('all', []), (kind, values))

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

    def test_worksheet_date_range_children_are_preserved(self):
        xml = (
            '<filter class="quantitative" column="[ds].[Jour]" '
            'included-values="in-range"><min>#2020-01-01#</min>'
            '<max>#2022-12-31#</max></filter>')
        per_sheet = TableauExtractor.extract_worksheet_filters(
            TableauExtractor.__new__(TableauExtractor),
            ET.fromstring(f'<worksheet>{xml}</worksheet>'))[0]
        self.assertEqual('range', per_sheet['type'])
        self.assertEqual(('#2020-01-01#', '#2022-12-31#'),
                         (per_sheet['min'], per_sheet['max']))

    def test_date_groupfilter_range_is_not_treated_as_all(self):
        kind, _, lo, hi, _ = _read_filter_condition(_filter(
            '<groupfilter function="range" from="#2020-01-01#" '
            'to="#2022-12-31#"/>'))
        self.assertEqual(('range', '#2020-01-01#', '#2022-12-31#'),
                         (kind, lo, hi))

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


class TestSharedViewFilterScope(unittest.TestCase):

    def test_shared_view_filter_is_attached_only_to_using_worksheets(self):
        root = ET.fromstring(
            '<workbook><shared-views><shared-view name="source-a">'
            '<filter class="categorical" '
            'column="[source-a].[none:Status:nk]">'
            '<groupfilter function="union">'
            '<groupfilter function="member" member="Open"/>'
            '</groupfilter></filter></shared-view></shared-views></workbook>')
        worksheets = [
            {'name': 'Using A',
             'fields': [{'name': 'Status', 'datasource': 'source-a'}],
             'filters': []},
            {'name': 'Using B',
             'fields': [{'name': 'Region', 'datasource': 'source-b'}],
             'filters': []},
        ]
        extractor = TableauExtractor.__new__(TableauExtractor)

        extractor._attach_shared_view_filters(root, worksheets)

        self.assertEqual(len(worksheets[0]['filters']), 1)
        self.assertEqual(worksheets[0]['filters'][0]['datasource'], 'source-a')
        self.assertEqual(worksheets[0]['filters'][0]['values'], ['Open'])
        self.assertEqual(worksheets[1]['filters'], [])

    def test_datasource_dependency_counts_even_without_a_projected_source_field(self):
        root = ET.fromstring(
            '<workbook><worksheets><worksheet name="Using A"><table><view>'
            '<datasource-dependencies datasource="source-a"/>'
            '</view></table></worksheet>'
            '<worksheet name="Using B"><table><view>'
            '<datasource-dependencies datasource="source-b"/>'
            '</view></table></worksheet></worksheets>'
            '<shared-views><shared-view name="source-a">'
            '<filter class="categorical" column="[source-a].[none:Status:nk]">'
            '<groupfilter function="member" member="Open"/>'
            '</filter></shared-view></shared-views></workbook>')
        worksheets = [{'name': 'Using A', 'fields': [], 'filters': []},
                      {'name': 'Using B', 'fields': [], 'filters': []}]
        TableauExtractor.__new__(TableauExtractor)._attach_shared_view_filters(
            root, worksheets)
        self.assertEqual(worksheets[0]['filters'][0]['values'], ['Open'])
        self.assertEqual(worksheets[1]['filters'], [])

    def test_conflicting_shared_view_source_is_not_applied_to_another_source(self):
        root = ET.fromstring(
            '<workbook><shared-views><shared-view name="source-a">'
            '<filter class="categorical" column="[source-b].[none:Status:nk]">'
            '<groupfilter function="member" member="Open"/>'
            '</filter></shared-view></shared-views></workbook>')
        worksheets = [
            {'name': 'Using A', 'fields': [{'name': 'Status', 'datasource': 'source-a'}]},
            {'name': 'Using B', 'fields': [{'name': 'Status', 'datasource': 'source-b'}]},
        ]
        with self.assertLogs('tableau_export.extract_tableau_data', level='WARNING'):
            TableauExtractor.__new__(TableauExtractor)._attach_shared_view_filters(
                root, worksheets)
        self.assertTrue(all(not ws.get('filters') for ws in worksheets))


if __name__ == '__main__':
    unittest.main()
