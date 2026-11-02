"""Reproduce a Tableau total card covered by its nested breakdown cards."""

import json
import os
import tempfile
import unittest

from powerbi_import.pbip_generator import PowerBIProjectGenerator


class TestOverlappingCards(unittest.TestCase):
    def _render(self, output, children, hierarchy=None):
        generator = PowerBIProjectGenerator(output_dir=output)
        generator._main_table = 'Facts'
        generator._field_map = {'Id': ('Facts', 'Id'), 'State': ('Facts', 'State')}
        generator._datasources_ref = [{'name': 'source', 'tables': [{'name': 'Facts'}]}]
        generator._actual_bim_symbols = {('Facts', 'Id'), ('Facts', 'State')}
        generator._actual_bim_measure_symbols = set()
        generator._actual_bim_column_types = {
            ('Facts', 'Id'): 'int64', ('Facts', 'State'): 'string',
        }
        generator._measure_names = set()
        generator._bim_measure_names = set()
        worksheets = []
        objects = []
        for name, position, state in [('Total', {'x': 0, 'y': 0, 'w': 200, 'h': 342}, None)] + children:
            worksheet = {
                'name': name, 'chart_type': 'card',
                'fields': [{'name': 'Id', 'shelf': 'text', 'datasource': 'source',
                            'aggregation': 'count'}],
            }
            if state:
                worksheet['filters'] = [{
                    'field': 'State', 'datasource': 'source',
                    'type': 'categorical', 'values': [state],
                }]
            worksheets.append(worksheet)
            objects.append({
                'type': 'worksheetReference', 'name': name,
                'worksheetName': name, 'position': position,
            })
        dashboard = {
            'name': 'Dashboard', 'size': {'width': 200, 'height': 342},
            'objects': objects, 'zone_hierarchy': hierarchy or {},
        }
        pages_dir = os.path.join(output, 'pages')
        os.makedirs(pages_dir)
        generator._create_dashboard_pages(pages_dir, [dashboard], worksheets, {}, {})
        visuals = []
        for root, _, files in os.walk(pages_dir):
            if 'visual.json' in files:
                with open(os.path.join(root, 'visual.json'), encoding='utf-8') as stream:
                    visuals.append(json.load(stream))
        return sorted(visuals, key=lambda visual: visual['position']['z'])

    def test_total_is_restricted_to_free_band_and_filters_remain_distinct(self):
        children = [
            ('Open', {'x': 0, 'y': 74, 'w': 200, 'h': 134}, 'open'),
            ('Closed', {'x': 0, 'y': 208, 'w': 200, 'h': 134}, 'closed'),
        ]
        hierarchy = {
            'name': 'Total', 'zone_type': 'worksheet',
            'position': {'x': 0, 'y': 0, 'w': 200, 'h': 342},
            'children': [
                {'name': name, 'zone_type': 'worksheet', 'position': position,
                 'children': []}
                for name, position, _ in children
            ],
        }
        with tempfile.TemporaryDirectory() as output:
            total, opened, closed = self._render(output, children, hierarchy)
        self.assertEqual(total['position']['height'], 74)
        self.assertEqual(opened['position']['y'], 74)
        self.assertEqual(closed['position']['y'], 208)
        self.assertIn('expose its value', total['annotations'][-1]['value'])
        self.assertNotIn('filterConfig', total)
        self.assertIn("'open'", json.dumps(opened['filterConfig']))
        self.assertIn("'closed'", json.dumps(closed['filterConfig']))
        for visual in (total, opened, closed):
            self.assertEqual(visual['visual']['visualType'], 'card')
            query = json.dumps(visual['visual']['query']['queryState'])
            self.assertIn('"Function": 5', query)

    def test_non_occluding_and_non_contained_cards_keep_their_geometry(self):
        for position in (
            {'x': 0, 'y': 210, 'w': 200, 'h': 100},
            {'x': 150, 'y': 100, 'w': 100, 'h': 150},
        ):
            with self.subTest(position=position), tempfile.TemporaryDirectory() as output:
                total, _ = self._render(output, [('Detail', position, None)])
            self.assertEqual(total['position']['height'], 342)
            self.assertNotIn('annotations', total)

    def test_no_free_band_is_reported_without_destroying_the_card(self):
        children = [('Detail', {'x': 0, 'y': 20, 'w': 200, 'h': 250}, None)]
        with tempfile.TemporaryDirectory() as output:
            with self.assertLogs('powerbi_import.pbip_generator', level='WARNING'):
                total, _ = self._render(output, children)
        self.assertEqual(total['position']['height'], 342)
        self.assertIn('may be obscured', total['annotations'][-1]['value'])


if __name__ == '__main__':
    unittest.main()
