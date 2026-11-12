"""Producer-backed Prep ValueFilter conversion and lineage metadata tests."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tableau_export'))

from prep_flow_analyzer import _extract_clean_operations, analyze_flow
from prep_flow_parser import (
    _process_transform_node,
    normalize_prep_flow,
    parse_prep_flow,
)
from powerbi_import.m_validator import validate_m_query
from tests.prep_flow_fixtures import operation, output, source, write_flow


FIXTURE = Path(__file__).parent / 'prep_dialect_samples' / 'value_filter_evidence.json'


class TestPrepValueFilterConversion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evidence = json.loads(FIXTURE.read_text(encoding='utf-8'))

    def convert(self, payload):
        node = {**deepcopy(payload), 'id': 'value-filter', 'nextNodes': []}
        upstream = {
            'name': 'Input',
            'fields': [{'name': next(iter(payload['values'])), 'type': 'string'}],
            'm_query': 'let\n    Source = #table({}, {})\nin\n    Source',
        }
        results = {'input': upstream}
        before = deepcopy((node, upstream))
        with redirect_stdout(io.StringIO()):
            _process_transform_node(
                'value-filter', node, {'value-filter': node}, ['input'],
                'ValueFilter', results, set(), 'Value Filter',
            )
        self.assertEqual((node, upstream), before)
        return results['value-filter']

    def test_observed_exclusion_values_normalize_and_convert_to_valid_m(self):
        payload = self.evidence['exclude_text_value']['payload']
        normalized = normalize_prep_flow({
            'nodes': {'value-filter': {
                **deepcopy(payload), 'id': 'value-filter',
                'baseType': 'transform', 'nextNodes': [],
            }},
            'connections': {},
        })
        self.assertEqual(
            normalized['nodes']['value-filter']['values'], payload['values']
        )
        result = self.convert(payload)
        self.assertIn(
            'if [#"Workbook project"] = null then true else '
            'not List.Contains({"Samples"}, [#"Workbook project"])',
            result['m_query'],
        )
        self.assertEqual(validate_m_query(result['m_query']), [])
        self.assertEqual(
            result['fields'], [{'name': 'Workbook project', 'type': 'string'}]
        )

    def test_empty_string_value_is_preserved(self):
        payload = self.evidence['exclude_empty_string']['payload']
        result = self.convert(payload)
        self.assertIn('List.Contains({""}, [#"Calculation id"])', result['m_query'])

    def test_filter_values_are_escaped_as_data_before_step_injection(self):
        payload = deepcopy(self.evidence['exclude_text_value']['payload'])
        payload['values']['Workbook project'] = [
            json.dumps('A#(lf){prev}\nB')
        ]
        result = self.convert(payload)
        self.assertIn('"A#(#)(lf)#(007B)prev}#(lf)B"', result['m_query'])
        self.assertNotIn('{prev}', result['m_query'])
        self.assertEqual(validate_m_query(result['m_query']), [])

    def test_lineage_records_filter_column_without_copying_filter_values(self):
        payload = self.evidence['exclude_text_value']['payload']
        operations = _extract_clean_operations([payload], {})
        self.assertEqual(operations[0]['type'], 'filter')
        self.assertEqual(operations[0]['column'], 'Workbook project')
        self.assertNotIn('Samples', json.dumps(operations))

    def test_end_to_end_export_keeps_lineage_and_redacts_filter_values(self):
        payload = self.evidence['exclude_text_value']['payload']
        input_node = source('value-filter', name='PublicInput')
        input_node['fields'] = [{'name': 'Workbook project', 'type': 'string'}]
        value_filter = operation('ValueFilter', 'filtered-output', **payload)
        flow = {
            'nodes': {
                'input': input_node,
                'value-filter': value_filter,
                'filtered-output': output('PublicOutput', base_type=True),
            },
            'connections': {},
        }
        with tempfile.TemporaryDirectory() as directory:
            parsed = parse_prep_flow(write_flow(directory, flow))
            profile = analyze_flow(write_flow(directory, flow))

        datasource = parsed[0]
        query = datasource['m_query_override']
        self.assertIn('List.Contains({"Samples"}', query)
        self.assertEqual(validate_m_query(query), [])
        provenance = datasource['prep_provenance']
        self.assertIn('value-filter', {
            node_id for node_id in (
                item['id'] for item in provenance['nodes'])
        })
        self.assertNotIn('Samples', json.dumps(provenance))
        filter_transform = next(
            transform for transform in profile.transforms
            if transform.node_id == 'value-filter'
        )
        self.assertEqual(filter_transform.transform_type, 'Clean')
        self.assertEqual(filter_transform.name, 'ValueFilter on Workbook project')
        self.assertNotIn('Samples', json.dumps(filter_transform.to_dict()))

    def test_unsupported_value_filter_shapes_are_rejected(self):
        payload = deepcopy(self.evidence['exclude_text_value']['payload'])
        payload['exclude'] = False
        with self.assertRaisesRegex(ValueError, 'exclude=true semantics only'):
            self.convert(payload)

        payload = deepcopy(self.evidence['exclude_text_value']['payload'])
        payload['values']['Another field'] = ['"Other"']
        with self.assertRaisesRegex(ValueError, 'exactly one field value map'):
            self.convert(payload)

        payload = deepcopy(self.evidence['exclude_text_value']['payload'])
        with self.assertRaisesRegex(ValueError, 'string fields only'):
            node = {**payload, 'id': 'value-filter', 'nextNodes': []}
            upstream = {
                'fields': [{'name': 'Workbook project', 'type': 'real'}],
                'm_query': 'let\n    Source = #table({}, {})\nin\n    Source',
            }
            with redirect_stdout(io.StringIO()):
                _process_transform_node(
                    'value-filter', node, {'value-filter': node}, ['input'],
                    'ValueFilter', {'input': upstream}, set(), 'Value Filter',
                )


if __name__ == '__main__':
    unittest.main()