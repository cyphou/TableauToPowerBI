"""Worksheet filters must not leak to other pages or standalone worksheets."""

import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

from powerbi_import.pbip_generator import PowerBIProjectGenerator
from tableau_export.extract_tableau_data import TableauExtractor


class TestWorksheetFilterScope(unittest.TestCase):
    def setUp(self):
        self.worksheets = [
            {
                'name': 'Filtered', 'chart_type': 'card',
                'fields': [{'name': 'Id', 'shelf': 'text', 'aggregation': 'count',
                            'datasource': 'source.a'}],
                'filters': [{'field': 'State', 'datasource': 'source.a',
                             'type': 'categorical', 'values': ['Open'],
                             'is_context': True}],
            },
            {
                'name': 'Independent', 'chart_type': 'card',
                'fields': [{'name': 'Id', 'shelf': 'text', 'aggregation': 'count',
                            'datasource': 'source.b'}],
                'filters': [],
            },
        ]

    def _generator(self, output):
        generator = PowerBIProjectGenerator(output_dir=output)
        generator._main_table = 'FactsA'
        generator._field_map = {'Id': ('FactsA', 'Id'), 'State': ('FactsA', 'State')}
        generator._datasources_ref = [
            {'name': 'source.a', 'caption': 'Source A', 'tables': [{'name': 'FactsA'}]},
            {'name': 'source.b', 'caption': 'Source B', 'tables': [{'name': 'FactsB'}]},
        ]
        generator._actual_bim_symbols = {
            (table, field) for table in ('FactsA', 'FactsB')
            for field in ('Id', 'State', 'Jour')
        }
        generator._actual_bim_column_types = {
            (table, field): 'string' for table, field in generator._actual_bim_symbols
        }
        generator._actual_bim_measure_symbols = set()
        generator._measure_names = set()
        generator._bim_measure_names = set()
        return generator

    @staticmethod
    def _read_pages(pages_dir):
        pages = {}
        for root, _, files in os.walk(pages_dir):
            if 'page.json' not in files:
                continue
            with open(os.path.join(root, 'page.json'), encoding='utf-8') as stream:
                page = json.load(stream)
            visuals = []
            for visual_root, _, visual_files in os.walk(os.path.join(root, 'visuals')):
                if 'visual.json' in visual_files:
                    with open(os.path.join(visual_root, 'visual.json'), encoding='utf-8') as stream:
                        visuals.append(json.load(stream))
            pages[page['displayName']] = (page, visuals)
        return pages

    def test_context_filter_only_applies_to_its_worksheet_across_dashboards(self):
        def obj(name, x):
            return {'type': 'worksheetReference', 'name': name, 'worksheetName': name,
                    'position': {'x': x, 'y': 0, 'w': 300, 'h': 200}}

        dashboards = [
            {'name': 'First', 'size': {'width': 600, 'height': 200},
             'objects': [obj('Filtered', 0), obj('Independent', 300)]},
            {'name': 'Second', 'size': {'width': 300, 'height': 200},
             'objects': [obj('Independent', 0)],
             'filters': [{'field': 'State', 'datasource': 'source.b',
                          'type': 'categorical', 'values': ['Closed']}]},
        ]
        with tempfile.TemporaryDirectory() as output:
            generator = self._generator(output)
            pages_dir = os.path.join(output, 'pages')
            os.makedirs(pages_dir)
            generator._create_dashboard_pages(
                pages_dir, dashboards, self.worksheets, {}, {})
            pages = self._read_pages(pages_dir)
        first_page, first_visuals = pages['First']
        second_page, second_visuals = pages['Second']
        self.assertNotIn('filterConfig', first_page)
        self.assertIn("'Closed'", json.dumps(second_page['filterConfig']))
        self.assertNotIn("'Open'", json.dumps(second_page))
        filtered = [visual for visual in first_visuals if 'filterConfig' in visual]
        self.assertEqual(len(filtered), 1)
        self.assertIn("'Open'", json.dumps(filtered[0]['filterConfig']))
        self.assertIn('"Entity": "FactsA"', json.dumps(filtered[0]['filterConfig']))
        self.assertFalse(any('filterConfig' in visual for visual in second_visuals))

    def test_standalone_page_keeps_worksheet_filter(self):
        with tempfile.TemporaryDirectory() as output:
            generator = self._generator(output)
            pages_dir = os.path.join(output, 'pages')
            os.makedirs(pages_dir)
            generator._create_fallback_page(pages_dir, self.worksheets, {})
            pages = self._read_pages(pages_dir)
        filtered_page, filtered_visuals = pages['Filtered']
        _, independent_visuals = pages['Independent']
        self.assertNotIn('filterConfig', filtered_page)
        self.assertIn("'Open'", json.dumps(filtered_visuals[0]['filterConfig']))
        self.assertNotIn('filterConfig', independent_visuals[0])

    def test_tableau_date_range_is_emitted_only_on_its_visual(self):
        xml = ET.fromstring(
            '<worksheet><filter class="quantitative" '
            'column="[source.a].[Jour]" included-values="in-range">'
            '<min>#2020-01-01#</min><max>#2022-12-31#</max>'
            '</filter></worksheet>')
        self.worksheets[0]['filters'] = TableauExtractor.extract_worksheet_filters(
            TableauExtractor.__new__(TableauExtractor), xml)
        with tempfile.TemporaryDirectory() as output:
            generator = self._generator(output)
            pages_dir = os.path.join(output, 'pages')
            os.makedirs(pages_dir)
            generator._create_fallback_page(pages_dir, self.worksheets, {})
            pages = self._read_pages(pages_dir)
        visual = pages['Filtered'][1][0]
        encoded = json.dumps(visual['filterConfig'])
        self.assertIn("datetime'2020-01-01T00:00:00'", encoded)
        self.assertIn("datetime'2022-12-31T00:00:00'", encoded)
        self.assertNotIn('filterConfig', pages['Independent'][1][0])

    def test_datasource_filter_record_targets_its_qualified_source(self):
        with tempfile.TemporaryDirectory() as output:
            generator = self._generator(output)
            records = generator._normalize_datasource_filters([{
                'datasource': 'Source B', 'column': 'source.b.none:State:nk',
                'filter_class': 'categorical', 'filter_type': '',
                'values': ['Closed'], 'range_min': None, 'range_max': None,
            }])
            filters = generator._create_visual_filters(records)
            qualified = generator._create_visual_filters([{
                'field': '[source.b].[none:State:nk]', 'values': ['Closed'],
            }])
        for filters_to_check in (filters, qualified):
            self.assertEqual(len(filters_to_check), 1)
            self.assertEqual(
                filters_to_check[0]['field']['Column']['Expression']['SourceRef']['Entity'],
                'FactsB')

    def test_ambiguous_datasource_filter_is_reported_instead_of_misbound(self):
        with tempfile.TemporaryDirectory() as output:
            generator = self._generator(output)
            generator._datasources_ref[1]['caption'] = 'Source A'
            with self.assertLogs('powerbi_import.pbip_generator', level='WARNING'):
                records = generator._normalize_datasource_filters([{
                    'datasource': 'Source A', 'column': 'State', 'values': ['Open'],
                }])
            self.assertEqual(records, [])

    def test_conflicting_or_missing_datasource_never_uses_an_unrelated_table(self):
        with tempfile.TemporaryDirectory() as output:
            generator = self._generator(output)
            for record in (
                    {'field': '[source.b].[none:State:nk]', 'datasource': 'source.a',
                     'type': 'categorical', 'values': ['Open']},
                    {'field': 'State', 'type': 'categorical', 'values': ['Open']}):
                with self.subTest(record=record), \
                        self.assertLogs('powerbi_import.pbip_generator', level='WARNING'):
                    self.assertEqual(generator._create_visual_filters([record]), [])

    def test_filter_control_resolves_homonymous_column_from_its_datasource(self):
        with tempfile.TemporaryDirectory() as output:
            generator = self._generator(output)
            control = {
                'name': 'State', 'field': 'State', 'calc_column_id': 'State',
                'param': '[source.b].[none:State:nk]',
                'position': {'x': 0, 'y': 0, 'w': 100, 'h': 80},
            }
            generator._create_visual_filter_control(output, control, 1, 1, 0, {}, {})
            path = next(os.path.join(root, 'visual.json')
                        for root, _, files in os.walk(output) if 'visual.json' in files)
            with open(path, encoding='utf-8') as stream:
                visual = json.load(stream)
            column = visual['visual']['query']['queryState']['Values']['projections'][0]['field']['Column']
            self.assertEqual(column, {
                'Expression': {'SourceRef': {'Entity': 'FactsB'}},
                'Property': 'State',
            })
            self.assertTrue(any(
                'slicer interactions' in annotation.get('value', '')
                for annotation in visual.get('annotations', [])))

    def test_ambiguous_or_unknown_filter_controls_are_reported_and_omitted(self):
        for param in ('', '[unknown.source].[none:State:nk]'):
            with self.subTest(param=param), tempfile.TemporaryDirectory() as output:
                generator = self._generator(output)
                control = {
                    'field': 'State', 'param': param,
                    'position': {'x': 0, 'y': 0, 'w': 100, 'h': 80},
                }
                with self.assertLogs('powerbi_import.pbip_generator', level='WARNING'):
                    generator._create_visual_filter_control(output, control, 1, 1, 0, {}, {})
                self.assertFalse(any('visual.json' in files for _, _, files in os.walk(output)))

    def test_datasource_filter_is_written_in_generated_report(self):
        source = {
            'name': 'source.a', 'caption': 'Source A',
            'connection': {'type': 'CSV', 'details': {'filename': 'synthetic.csv'}},
            'tables': [{'name': 'FactsA', 'source_table': '[synthetic#csv]', 'columns': [
                {'name': 'Id', 'datatype': 'integer'},
                {'name': 'State', 'datatype': 'string'},
            ]}],
        }
        converted = {
            'datasources': [source], 'worksheets': [self.worksheets[0]],
            'dashboards': [], 'datasource_filters': [{
                'datasource': 'Source A', 'column': 'State',
                'filter_class': 'categorical', 'filter_type': '',
                'values': ['Open'], 'range_min': None, 'range_max': None,
            }],
        }
        with tempfile.TemporaryDirectory() as output:
            generator = PowerBIProjectGenerator(output_dir=output)
            generator.generate_project('Filter scope', converted)
            report_file = next(
                os.path.join(root, 'report.json') for root, _, files in os.walk(output)
                if 'report.json' in files)
            with open(report_file, encoding='utf-8') as stream:
                report = json.load(stream)
        filters = report['filterConfig']['filters']
        self.assertEqual(len(filters), 1)
        self.assertEqual(
            filters[0]['field']['Column']['Expression']['SourceRef']['Entity'], 'FactsA')
        self.assertIn("'Open'", json.dumps(filters[0]))


if __name__ == '__main__':
    unittest.main()
