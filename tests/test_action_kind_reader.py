"""A Tableau action names its kind with a child, not with an attribute.

Every action in the real example workbooks is untyped: 29 of the 45 `<action>`
elements carry no `type`, and the 16 that do all come from hand-written sample
files. Reading only `@type` saw none of the real ones, so no cross-filter,
highlight or URL drill-back from a genuine workbook was migrated.
"""

import os
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))

from powerbi_import.pbip_generator import _is_static_url  # noqa: E402
from powerbi_import.tmdl_generator import _inject_url_action_categories  # noqa: E402
from tableau_export.extract_tableau_data import (  # noqa: E402
    TableauExtractor, _read_action_kind)


def _action(inner='', attrs='name="[Action1]"'):
    return ET.fromstring(f'<action {attrs}>{inner}</action>')


def _extract(action_xml):
    extractor = TableauExtractor.__new__(TableauExtractor)
    extractor.workbook_data = {}
    extractor._xml_node_cache = {}
    extractor.extract_workbook_actions(
        ET.fromstring(f'<workbook><actions>{action_xml}</actions></workbook>'))
    return extractor.workbook_data['actions']


class TestKindFromChildren(unittest.TestCase):

    def test_a_brush_command_is_a_highlight(self):
        self.assertEqual('highlight', _read_action_kind(
            _action('<command command="tsc:brush"/>')))

    def test_a_tsl_filter_command_is_a_filter(self):
        self.assertEqual('filter', _read_action_kind(
            _action('<command command="tsc:tsl-filter"/>')))

    def test_a_link_to_a_field_is_a_url_action(self):
        self.assertEqual('url', _read_action_kind(
            _action('<link expression="&lt;[ds].[Drill-back]&gt;"/>')))

    def test_an_internal_tsl_link_is_a_filter_not_a_url(self):
        self.assertEqual('filter', _read_action_kind(
            _action('<link expression="tsl:Dashboard?%5Bds%5D~s0=x"/>')))

    def test_an_explicit_type_attribute_still_wins(self):
        actions = _extract('<action type="highlight" name="a">'
                           '<command command="tsc:tsl-filter"/></action>')
        self.assertEqual('highlight', actions[0]['type'])


class TestNothingIsGuessed(unittest.TestCase):

    def test_an_action_with_no_link_and_no_command_stays_unnamed(self):
        self.assertEqual('', _read_action_kind(
            _action('<source worksheet="Sheet 1"/>')))

    def test_an_unknown_command_stays_unnamed(self):
        self.assertEqual('', _read_action_kind(
            _action('<command command="tsc:something-new"/>')))

    def test_an_empty_action_stays_unnamed(self):
        self.assertEqual('', _read_action_kind(_action()))


class TestUrlTarget(unittest.TestCase):

    def test_the_url_is_read_from_the_element_text(self):
        actions = _extract('<action type="url" name="Site">'
                           '<url>https://example.com/page</url></action>')
        self.assertEqual('https://example.com/page', actions[0]['url'])

    def test_the_url_is_read_from_a_link_expression(self):
        actions = _extract('<action name="Drill">'
                           '<link expression="&lt;[ds].[Link]&gt;"/></action>')
        self.assertEqual('url', actions[0]['type'])
        self.assertEqual('<[ds].[Link]>', actions[0]['url'])


class TestOnlyAStaticUrlBecomesAButton(unittest.TestCase):
    """A per-row link is not a button target; writing it ships a dead link."""

    def test_a_plain_url_is_static(self):
        self.assertTrue(_is_static_url('https://example.com/page'))
        self.assertTrue(_is_static_url('mailto:someone@example.com'))

    def test_a_field_reference_is_not_static(self):
        self.assertFalse(_is_static_url('<[ds].[URL Drill-back]>'))

    def test_an_interpolated_url_is_not_static(self):
        self.assertFalse(
            _is_static_url('https://crm.company.com/customer/<customer_id>'))

    def test_an_empty_target_is_not_static(self):
        self.assertFalse(_is_static_url(''))
        self.assertFalse(_is_static_url(None))

    def test_a_non_http_scheme_is_not_static(self):
        self.assertFalse(_is_static_url('tsl:Dashboard?x=1'))
        self.assertFalse(_is_static_url('Open the CRM'))


class TestDynamicUrlFieldMetadata(unittest.TestCase):

    def test_direct_url_field_becomes_a_web_url_column(self):
        model = {'model': {'tables': [{
            'name': 'Sales',
            'columns': [{'name': 'Opportunity URL', 'dataType': 'String'}],
        }]}}
        _inject_url_action_categories(
            model,
            [{'type': 'url', 'url': '<[ds].[Opportunity URL]>'}],
            {'Opportunity URL': 'Sales'},
        )
        self.assertEqual('WebUrl',
                         model['model']['tables'][0]['columns'][0]['dataCategory'])

    def test_composed_url_does_not_mark_a_partial_field_as_web_url(self):
        model = {'model': {'tables': [{
            'name': 'Sales',
            'columns': [{'name': 'Opportunity ID', 'dataType': 'String'}],
        }]}}
        _inject_url_action_categories(
            model,
            [{'type': 'url', 'url': 'https://crm/opportunity/<Opportunity ID>'}],
            {'Opportunity ID': 'Sales'},
        )
        self.assertNotIn('dataCategory',
                         model['model']['tables'][0]['columns'][0])

    def test_calculated_url_field_uses_its_generated_caption(self):
        model = {'model': {'tables': [{
            'name': 'Sales',
            'columns': [{'name': 'Opportunity Link', 'dataType': 'String'}],
        }]}}
        _inject_url_action_categories(
            model,
            [{'type': 'url', 'url': '<[ds].[Calculation_123]>'}],
            {'Opportunity Link': 'Sales'},
            [{'name': '[Calculation_123]', 'caption': 'Opportunity Link'}],
        )
        self.assertEqual('WebUrl',
                         model['model']['tables'][0]['columns'][0]['dataCategory'])


if __name__ == '__main__':
    unittest.main()
