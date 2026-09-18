"""An action button belongs on the page the action came from.

Buttons are built per page from the whole workbook action list. The call site
filtered on ``source_worksheet`` — singular, a key the extractor never emits —
so every action passed and one action became one button per page: three
Salesforce actions produced twenty-four buttons.

Tableau names the endpoint on ``<source>``/``<target>`` as ``dashboard``, and
only sometimes as ``worksheet``: all six ``<target>`` elements in the example
corpus carry ``dashboard`` alone.
"""

import os
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))

from powerbi_import.pbip_generator import _action_belongs_to_page  # noqa: E402
from tableau_export.extract_tableau_data import TableauExtractor  # noqa: E402


def _extract(action_xml):
    extractor = TableauExtractor.__new__(TableauExtractor)
    extractor.workbook_data = {}
    extractor._xml_node_cache = {}
    extractor.extract_workbook_actions(
        ET.fromstring(f'<workbook><actions>{action_xml}</actions></workbook>'))
    return extractor.workbook_data['actions'][0]


class TestEndpointsAreCaptured(unittest.TestCase):

    def test_a_source_names_its_dashboard(self):
        action = _extract(
            '<action name="a"><source type="dashboard" dashboard="Exec" '
            'worksheet="Revenue"/><command command="tsc:brush"/></action>')
        self.assertEqual(['Exec'], action['source_dashboards'])
        self.assertEqual(['Revenue'], action['source_worksheets'])

    def test_a_target_names_its_dashboard(self):
        action = _extract(
            '<action name="a"><target type="dashboard" dashboard="Detail"/>'
            '<command command="tsc:tsl-filter"/></action>')
        self.assertEqual(['Detail'], action['target_dashboards'])
        self.assertEqual([], action['target_worksheets'])

    def test_a_dashboard_is_recorded_once(self):
        action = _extract(
            '<action name="a">'
            '<source type="dashboard" dashboard="Exec" worksheet="A"/>'
            '<source type="dashboard" dashboard="Exec" worksheet="B"/>'
            '<command command="tsc:brush"/></action>')
        self.assertEqual(['Exec'], action['source_dashboards'])
        self.assertEqual(['A', 'B'], action['source_worksheets'])

    def test_an_endpointless_action_records_nothing(self):
        action = _extract('<action name="a"><command command="tsc:brush"/></action>')
        self.assertEqual([], action['source_dashboards'])
        self.assertEqual([], action['target_dashboards'])


class TestPageOwnership(unittest.TestCase):

    def test_an_action_belongs_to_the_dashboard_it_names(self):
        action = {'source_dashboards': ['Executive Summary']}
        self.assertTrue(_action_belongs_to_page(action, 'Executive Summary'))
        self.assertFalse(_action_belongs_to_page(action, 'Performance Review'))

    def test_a_worksheet_source_matches_the_page_holding_it(self):
        action = {'source_worksheets': ['Revenue by Region']}
        self.assertTrue(_action_belongs_to_page(
            action, 'Exec', {'Revenue by Region', 'Profit Trend'}))
        self.assertFalse(_action_belongs_to_page(action, 'Other', {'Profit Trend'}))

    def test_a_dashboard_source_wins_over_worksheets(self):
        action = {'source_dashboards': ['Exec'],
                  'source_worksheets': ['Revenue by Region']}
        self.assertFalse(_action_belongs_to_page(
            action, 'Other', {'Revenue by Region'}))

    def test_an_action_naming_nowhere_stays_on_every_page(self):
        self.assertTrue(_action_belongs_to_page({}, 'Any page'))
        self.assertTrue(_action_belongs_to_page(
            {'source_dashboards': [], 'source_worksheets': []}, 'Any page'))

    def test_the_singular_key_is_no_longer_consulted(self):
        # The old filter keyed on source_worksheet, which nothing emits.
        action = {'source_worksheet': 'Revenue by Region',
                  'source_dashboards': ['Executive Summary']}
        self.assertFalse(_action_belongs_to_page(action, 'Performance Review'))


if __name__ == '__main__':
    unittest.main()
