"""COUNTD bindings must exclude nulls and retain qualified source identity."""

import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'powerbi_import'))
sys.path.insert(0, os.path.join(ROOT, 'tableau_export'))
sys.path.insert(0, ROOT)

from pbip_generator import PowerBIProjectGenerator

from powerbi_import.tmdl_generator import (
    _build_semantic_model,
    _create_countd_measures,
    _create_quick_table_calc_measures,
    _parse_countd_reference,
)


def _model():
    return {
        '_column_rename_map': {('Orders', 'Id'): 'Customer Id'},
        'model': {'tables': [
            {
                'name': 'Orders',
                'columns': [
                    {'name': 'Customer Id', 'sourceColumn': 'Id', 'dataType': 'string'},
                    {'name': 'Category', 'dataType': 'string'},
                ],
                'measures': [{'name': 'COUNTD Customer Id (Orders)', 'expression': '1'}],
            },
            {
                'name': 'Orders (Source B)',
                'columns': [{'name': 'Id', 'dataType': 'string'}],
                'measures': [],
            },
        ]},
    }


class TestCountdModelBindings(unittest.TestCase):
    def setUp(self):
        self.model = _model()
        self.worksheets = [{
            'fields': [{'name': 'Id', 'datasource': 'source.a', 'aggregation': 'ctd'}],
            'sort_orders': [{'field': 'Category', 'sort_by': 'source.b.countd:Id:qk'}],
        }]
        _create_countd_measures(self.model, self.worksheets)

    def test_pill_parser_preserves_dotted_datasource_and_field(self):
        self.assertEqual(
            _parse_countd_reference('[federated.1].[ctd:Customer.Code:qk]'),
            ('federated.1', 'Customer.Code'),
        )

    def test_helpers_are_unique_hidden_and_qualified(self):
        bindings = self.model['_countd_measure_bindings']
        self.assertEqual(set(bindings), {
            ('Orders', 'Customer Id'), ('Orders (Source B)', 'Id'),
        })
        self.assertEqual(len(set(bindings.values())), 2)
        self.assertNotEqual(bindings[('Orders', 'Customer Id')],
                            'COUNTD Customer Id (Orders)')
        for table in self.model['model']['tables']:
            pair = next(pair for pair in bindings if pair[0] == table['name'])
            measure = next(m for m in table['measures'] if m['name'] == bindings[pair])
            self.assertTrue(measure['isHidden'])
            self.assertIn(f"ISEMPTY('{pair[0]}')", measure['expression'])
            self.assertIn(f"DISTINCTCOUNTNOBLANK('{pair[0]}'[{pair[1]}])",
                          measure['expression'])

    def test_missing_column_is_logged_and_not_bound(self):
        with self.assertLogs('powerbi_import.tmdl_generator', level='WARNING'):
            _create_countd_measures(
                self.model, [{'fields': [{'name': 'Missing', 'aggregation': 'countd'}]}])
        self.assertNotIn(('Orders', 'Missing'), self.model['_countd_measure_bindings'])

    def _generator(self, output):
        generator = PowerBIProjectGenerator(output_dir=output)
        bindings = self.model['_countd_measure_bindings']
        generator._countd_measure_bindings = bindings
        generator._column_rename_map = self.model['_column_rename_map']
        generator._table_rename_map = {('source.b', 'Orders'): 'Orders (Source B)'}
        generator._datasources_ref = [
            {'name': 'source.a', 'tables': [{'name': 'Orders'}]},
            {'name': 'source.b', 'tables': [{'name': 'Orders'}]},
        ]
        generator._main_table = 'Orders'
        generator._field_map = {
            'Id': ('Orders', 'Customer Id'),
            'Category': ('Orders', 'Category'),
        }
        generator._actual_bim_symbols = {
            (table['name'], item['name'])
            for table in self.model['model']['tables']
            for item in table['columns'] + table['measures']
        }
        generator._actual_bim_measure_symbols = {
            (table['name'], item['name'])
            for table in self.model['model']['tables'] for item in table['measures']
        }
        generator._actual_bim_column_types = {
            (table['name'], column['name']): column['dataType']
            for table in self.model['model']['tables'] for column in table['columns']
        }
        generator._actual_bim_measure_names = {
            name for _, name in generator._actual_bim_measure_symbols
        }
        generator._bim_measure_names = set(generator._actual_bim_measure_names)
        generator._measure_names = set()
        return generator

    def test_scatter_and_ordinary_projection_bind_the_correct_measure(self):
        with tempfile.TemporaryDirectory() as output:
            generator = self._generator(output)
            for pair in self.model['_countd_measure_bindings']:
                field = {'name': 'Id', 'aggregation': 'countd', '_resolved_pair': pair}
                for project in (generator._make_projection_entry,
                                generator._make_scatter_axis_entry):
                    with self.subTest(pair=pair, project=project.__name__):
                        entry = project(field)
                        self.assertEqual(entry['field'], {
                            'Measure': {
                                'Expression': {'SourceRef': {'Entity': pair[0]}},
                                'Property': self.model['_countd_measure_bindings'][pair],
                            },
                        })
                        self.assertEqual(entry['nativeQueryRef'], pair[1])

    def test_computed_sort_uses_the_qualified_nonblank_measure(self):
        with tempfile.TemporaryDirectory() as output:
            generator = self._generator(output)
            worksheet = {
                'name': 'Count by category',
                'chart_type': 'clusteredBarChart',
                'fields': [
                    {'name': 'Category', 'shelf': 'columns', 'datasource': 'source.a'},
                    {'name': 'Id', 'shelf': 'rows', 'datasource': 'source.a',
                     'aggregation': 'ctd'},
                ],
                'sort_orders': [{
                    'field': 'Category', 'direction': 'DESC',
                    'sort_by': 'source.b.countd:Id:qk',
                }],
            }
            generator._create_visual_worksheet(
                output, worksheet, {'worksheetName': worksheet['name'], 'position': {}},
                1, 1, 0, [worksheet], {},
            )
            path = next(os.path.join(root, 'visual.json')
                        for root, _, files in os.walk(output) if 'visual.json' in files)
            with open(path, encoding='utf-8') as stream:
                visual = json.load(stream)
            sort = visual['visual']['query']['sortDefinition']['sort'][0]
            self.assertEqual(sort['field']['Measure'], {
                'Expression': {'SourceRef': {'Entity': 'Orders (Source B)'}},
                'Property': self.model['_countd_measure_bindings'][('Orders (Source B)', 'Id')],
            })

    def test_standalone_sort_uses_the_qualified_nonblank_measure(self):
        with tempfile.TemporaryDirectory() as output:
            generator = self._generator(output)
            worksheet = {
                'name': 'Count by category',
                'chart_type': 'clusteredBarChart',
                'fields': [
                    {'name': 'Category', 'shelf': 'rows', 'datasource': 'source.a'},
                    {'name': 'Id', 'shelf': 'columns', 'datasource': 'source.a',
                     'aggregation': 'ctd'},
                ],
                'sort_orders': [{
                    'field': 'Category', 'direction': 'DESC',
                    'sort_by': 'source.b.countd:Id:qk',
                }],
            }
            generator._create_fallback_page(output, [worksheet], {})
            path = next(os.path.join(root, 'visual.json')
                        for root, _, files in os.walk(output) if 'visual.json' in files)
            with open(path, encoding='utf-8') as stream:
                visual = json.load(stream)
            sort = visual['visual']['query']['sortDefinition']['sort'][0]
            self.assertEqual(sort, {
                'direction': 'Descending',
                'field': {'Measure': {
                    'Expression': {'SourceRef': {'Entity': 'Orders (Source B)'}},
                    'Property': self.model['_countd_measure_bindings'][('Orders (Source B)', 'Id')],
                }},
            })

    def test_computed_sort_preserves_dotted_source_and_column_names(self):
        with tempfile.TemporaryDirectory() as output:
            generator = self._generator(output)
            generator._field_map['Amount.Value'] = ('Orders', 'Amount.Value')
            for table in ('Orders', 'Orders (Source B)'):
                generator._actual_bim_symbols.add((table, 'Amount.Value'))
                generator._actual_bim_column_types[(table, 'Amount.Value')] = 'double'
            worksheet = {
                'name': 'Qualified sort', 'chart_type': 'clusteredBarChart',
                'fields': [
                    {'name': 'Category', 'shelf': 'rows', 'datasource': 'source.a'},
                    {'name': 'Amount.Value', 'shelf': 'columns',
                     'datasource': 'source.b', 'aggregation': 'sum'},
                ],
                'sort_orders': [{
                    'field': 'Category', 'direction': 'DESC',
                    'sort_by': '[source.b].[sum:Amount.Value:qk]',
                }],
            }
            generator._create_visual_worksheet(
                output, worksheet, {'worksheetName': worksheet['name'], 'position': {}},
                1, 1, 0, [worksheet], {},
            )
            path = next(os.path.join(root, 'visual.json')
                        for root, _, files in os.walk(output) if 'visual.json' in files)
            with open(path, encoding='utf-8') as stream:
                visual = json.load(stream)
            sort = visual['visual']['query']['sortDefinition']['sort'][0]
            self.assertEqual(sort, {
                'direction': 'Descending',
                'field': {'Aggregation': {
                    'Expression': {'Column': {
                        'Expression': {'SourceRef': {'Entity': 'Orders (Source B)'}},
                        'Property': 'Amount.Value'}},
                    'Function': 0,
                }},
            })

    def test_pie_wedge_size_countd_survives_xml_extraction_and_pbir_generation(self):
        from extract_tableau_data import TableauExtractor

        with tempfile.TemporaryDirectory() as output:
            extractor = TableauExtractor(os.path.join(output, 'fixture.twb'), output_dir=output)
            extractor.extract_worksheets(ET.fromstring("""
                <workbook><worksheets><worksheet name="Distinct pie"><table>
                    <panes><pane><mark class="Pie"/><encodings>
                        <color column="[source.a].[none:Category:nk]"/>
                        <wedge-size column="[source.a].[ctd:Id:qk]"/>
                    </encodings></pane></panes>
                </table></worksheet></worksheets></workbook>
            """))
            worksheet = extractor.workbook_data['worksheets'][0]
            wedge = next(field for field in worksheet['fields']
                         if field['shelf'] == 'wedge-size')
            self.assertEqual(wedge['name'], 'Id')
            self.assertEqual(wedge['aggregation'], 'ctd')

            generator = self._generator(output)
            generator._create_visual_worksheet(
                output, worksheet, {'worksheetName': worksheet['name'], 'position': {}},
                1, 1, 0, [worksheet], {},
            )
            path = next(os.path.join(root, 'visual.json')
                        for root, _, files in os.walk(output) if 'visual.json' in files)
            with open(path, encoding='utf-8') as stream:
                visual = json.load(stream)['visual']
            self.assertEqual(visual['visualType'], 'pieChart')
            state = visual['query']['queryState']
            self.assertEqual(state['Category']['projections'][0]['field'], {
                'Column': {
                    'Expression': {'SourceRef': {'Entity': 'Orders'}},
                    'Property': 'Category',
                },
            })
            self.assertEqual(state['Y']['projections'][0]['field'], {
                'Measure': {
                    'Expression': {'SourceRef': {'Entity': 'Orders'}},
                    'Property': self.model['_countd_measure_bindings'][('Orders', 'Customer Id')],
                },
            })

    def test_conditional_calculation_id_binds_to_nonblank_measure_in_pbip(self):
        calculation = {
            'name': '[Calculation_1]', 'caption': 'Ouverts',
            'formula': 'IF [Etat] = "ouvert" THEN [Id] END',
            'datatype': 'integer', 'role': 'measure', 'datasource_name': 'source.a',
        }
        source = {
            'name': 'source.a',
            'connection': {'type': 'CSV', 'details': {'filename': 'synthetic.csv'}},
            'tables': [{'name': 'Facts', 'source_table': '[synthetic#csv]', 'columns': [
                {'name': 'Id', 'datatype': 'integer'},
                {'name': 'Etat', 'datatype': 'string'},
            ]}],
            'calculations': [calculation],
        }
        worksheet = {
            'name': 'Conditional count', 'chart_type': 'card',
            'fields': [{'name': 'Calculation_1', 'datasource': 'source.a',
                        'aggregation': 'ctd', 'shelf': 'text'}],
        }
        converted = {'datasources': [source], 'calculations': [calculation],
                     'worksheets': [worksheet], 'dashboards': []}
        with tempfile.TemporaryDirectory() as output:
            generator = PowerBIProjectGenerator(output_dir=output)
            generator.generate_project('Conditional', converted)
            binding = generator._countd_measure_bindings[('Facts', 'Ouverts')]
            table_file = next(
                os.path.join(root, 'Facts.tmdl') for root, _, files in os.walk(output)
                if 'Facts.tmdl' in files)
            with open(table_file, encoding='utf-8') as stream:
                table = stream.read()
            visual_file = next(
                os.path.join(root, 'visual.json') for root, _, files in os.walk(output)
                if 'visual.json' in files)
            with open(visual_file, encoding='utf-8') as stream:
                visual = json.load(stream)
        self.assertIn("DISTINCTCOUNTNOBLANK('Facts'[Ouverts])", table)
        projections = visual['visual']['query']['queryState']['Values']['projections']
        self.assertEqual(projections[0]['field']['Measure']['Property'], binding)


class TestQuickCountdBindings(unittest.TestCase):
    def setUp(self):
        self.model = _model()
        self.model['_datasources'] = [
            {'name': 'source.a', 'tables': [{'name': 'Orders'}]},
            {'name': 'source.b', 'tables': [{'name': 'Orders'}]},
        ]
        self.model['_table_rename_map'] = {('source.b', 'Orders'): 'Orders (Source B)'}
        self.worksheets = [{
            'name': 'Qualified COUNTD',
            'chart_type': 'clusteredBarChart',
            'fields': [
                {'name': 'Category', 'shelf': 'columns', 'datasource': 'source.a'},
                {'name': 'Id', 'shelf': 'rows', 'datasource': 'source.a',
                 'table_calc': 'pcto', 'table_calc_agg': 'countd'},
                {'name': 'Id', 'shelf': 'tooltip', 'datasource': 'source.b',
                 'table_calc': 'pcto', 'table_calc_agg': 'ctd'},
            ],
        }]

    def _generate(self):
        _create_quick_table_calc_measures(
            self.model, self.worksheets, 'Orders', {'Id': 'Orders'})

    def _generator(self, output):
        fixture = TestCountdModelBindings()
        fixture.model = self.model
        self.model.setdefault('_countd_measure_bindings', {})
        generator = fixture._generator(output)
        generator._quick_table_calc_bindings = self.model['_quick_table_calc_bindings']
        return generator

    def test_duplicate_fields_get_distinct_qualified_measures(self):
        self._generate()
        bindings = self.model['_quick_table_calc_bindings']
        first = bindings[('Orders', 'Customer Id', 'pcto', 'countd')]
        second = bindings[('Orders (Source B)', 'Id', 'pcto', 'countd')]
        self.assertNotEqual(first, second)
        measures = {
            (table['name'], measure['name']): measure
            for table in self.model['model']['tables']
            for measure in table['measures']
        }
        self.assertIn("DISTINCTCOUNTNOBLANK('Orders'[Customer Id])",
                      measures[first]['expression'])
        self.assertIn("DISTINCTCOUNTNOBLANK('Orders (Source B)'[Id])",
                      measures[second]['expression'])
        self.assertIn('mark partitioning', measures[first]['annotations'][0]['value'])

        with tempfile.TemporaryDirectory() as output:
            generator = self._generator(output)
            query = generator._build_visual_query(self.worksheets[0])
        projections = [
            projection['field']['Measure']
            for role in query['queryState'].values()
            for projection in role['projections']
            if 'Measure' in projection['field']
        ]
        actual = {
            (item['Expression']['SourceRef']['Entity'], item['Property'])
            for item in projections
        }
        self.assertIn(first, actual)
        self.assertIn(second, actual)
        self.assertIn('scope may differ', self.worksheets[0]['_visual_mapping_note'])

    def test_unknown_or_ambiguous_source_is_omitted_and_reported(self):
        self.worksheets[0]['fields'][1]['datasource'] = 'unknown-source'
        with self.assertLogs('powerbi_import.tmdl_generator', level='WARNING'):
            self._generate()
        self.assertNotIn(
            ('Orders', 'Customer Id', 'pcto', 'countd'),
            self.model['_quick_table_calc_bindings'])
        with tempfile.TemporaryDirectory() as output:
            generator = self._generator(output)
            with self.assertLogs('pbip_generator', level='WARNING'):
                generator._build_visual_query(self.worksheets[0])
        self.assertIn('Id', self.worksheets[0]['_quick_countd_unresolved'])

        self.model['_datasources'][0]['tables'].append({'name': 'Orders (Source B)'})
        self.worksheets[0]['fields'][1]['datasource'] = 'source.a'
        with self.assertLogs('powerbi_import.tmdl_generator', level='WARNING'):
            self._generate()
        self.assertNotIn(
            ('Orders', 'Customer Id', 'pcto', 'countd'),
            self.model['_quick_table_calc_bindings'])

    def test_running_countd_is_not_mistaken_for_a_distinct_cumulative_total(self):
        self.worksheets[0]['fields'][1]['table_calc'] = 'running_sum'
        with self.assertLogs('powerbi_import.tmdl_generator', level='WARNING') as captured:
            self._generate()
        self.assertTrue(any('requires Tableau' in message for message in captured.output))
        self.assertNotIn(
            ('Orders', 'Customer Id', 'running_sum', 'countd'),
            self.model['_quick_table_calc_bindings'])

    def test_standalone_visual_reports_omitted_quick_countd(self):
        self.worksheets[0]['fields'][1]['table_calc'] = 'running_sum'
        with self.assertLogs('powerbi_import.tmdl_generator', level='WARNING'):
            self._generate()
        with tempfile.TemporaryDirectory() as output:
            generator = self._generator(output)
            pages = os.path.join(output, 'pages')
            os.makedirs(pages)
            with self.assertLogs('pbip_generator', level='WARNING'):
                generator._create_fallback_page(pages, self.worksheets, {})
            path = next(os.path.join(root, 'visual.json')
                        for root, _, files in os.walk(pages) if 'visual.json' in files)
            with open(path, encoding='utf-8') as stream:
                visual = json.load(stream)
        notes = [item['value'] for item in visual.get('annotations', [])]
        self.assertTrue(any('Quick COUNTD omitted' in note for note in notes))

    def test_directlake_never_generates_a_reference_to_missing_calendar(self):
        datasources = [{
            'name': 'source.a',
            'tables': [{'name': 'Orders', 'columns': [
                {'name': 'Id', 'datatype': 'string'},
                {'name': 'Amount', 'datatype': 'real'},
            ]}],
        }]
        worksheets = [{'fields': [
            {'name': 'Amount', 'datasource': 'source.a', 'table_calc': 'running_sum'},
            {'name': 'Amount', 'datasource': 'source.a', 'table_calc': 'pctd'},
            {'name': 'Id', 'datasource': 'source.a', 'table_calc': 'pcto',
             'table_calc_agg': 'countd'},
        ]}]
        with self.assertLogs('powerbi_import.tmdl_generator', level='WARNING'):
            model = _build_semantic_model(
                datasources, extra_objects={'_worksheets': worksheets}, model_mode='directLake')
        measures = [measure for table in model['model']['tables']
                    for measure in table.get('measures', [])]
        self.assertTrue(any('DISTINCTCOUNTNOBLANK' in m['expression'] for m in measures))
        self.assertFalse(any("'Calendar'[Date]" in m['expression'] for m in measures))
        self.assertEqual(len(model['_unsupported_quick_table_calcs']), 2)

    def test_fabric_writer_persists_qualified_countd_without_missing_calendar(self):
        from powerbi_import.fabric_semantic_model_generator import (
            FabricSemanticModelGenerator,
        )

        extracted = {
            'datasources': [{
                'name': 'source.a',
                'tables': [{'name': 'Orders', 'columns': [
                    {'name': 'Id', 'datatype': 'string'},
                    {'name': 'Amount', 'datatype': 'real'},
                ]}],
            }],
            'worksheets': [{'name': 'Counts', 'fields': [
                {'name': 'Id', 'datasource': 'source.a', 'aggregation': 'countd'},
                {'name': 'Id', 'datasource': 'source.a', 'table_calc': 'pcto',
                 'table_calc_agg': 'countd'},
                {'name': 'Amount', 'datasource': 'source.a', 'table_calc': 'running_sum'},
            ]}],
        }
        with tempfile.TemporaryDirectory() as output:
            generator = FabricSemanticModelGenerator(output, 'Synthetic')
            with self.assertLogs('powerbi_import.tmdl_generator', level='WARNING'):
                stats = generator.generate(extracted)
            table_file = os.path.join(generator.sm_dir, 'definition', 'tables', 'Orders.tmdl')
            with open(table_file, encoding='utf-8') as stream:
                table = stream.read()
        self.assertIn(('Orders', 'Id'), stats['countd_measure_bindings'])
        self.assertIn(('Orders', 'Id', 'pcto', 'countd'),
                      stats['quick_table_calc_bindings'])
        self.assertIn("DISTINCTCOUNTNOBLANK('Orders'[Id])", table)
        self.assertNotIn("'Calendar'[Date]", table)

    def test_dense_rank_uses_the_fifth_rankx_argument(self):
        self.worksheets[0]['fields'][1]['table_calc'] = 'rank_dense'
        self._generate()
        binding = self.model['_quick_table_calc_bindings'][
            ('Orders', 'Customer Id', 'rank_dense', 'countd')]
        measure = next(
            item for table in self.model['model']['tables']
            if table['name'] == binding[0] for item in table['measures']
            if item['name'] == binding[1])
        self.assertIn(', , DESC, DENSE)', measure['expression'])
        self.assertIn("DISTINCTCOUNTNOBLANK('Orders'[Customer Id])",
                      measure['expression'])

    def test_quick_countd_accepts_calculation_id_and_caption(self):
        source = self.model['_datasources'][0]
        source['calculations'] = [{'name': '[Calculation_1]', 'caption': 'Opened'}]
        self.model['model']['tables'][0]['columns'].append(
            {'name': 'Opened', 'dataType': 'string'})
        self.worksheets[0]['fields'][1]['name'] = 'Calculation_1'
        self._generate()
        self.assertIn(
            ('Orders', 'Opened', 'pcto', 'countd'),
            self.model['_quick_table_calc_bindings'])


if __name__ == '__main__':
    unittest.main()
