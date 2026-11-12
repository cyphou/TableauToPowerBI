"""Producer-backed QuickCalcColumn TrimSpaces conversion tests."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tableau_export'))

from prep_flow_parser import (
    _unicode_letter_ranges,
    _expression_type,
    _process_transform_node,
    normalize_prep_flow,
)
from prep_flow_analyzer import _extract_clean_operations, analyze_flow
from powerbi_import.m_validator import validate_m_query
from tests.prep_flow_fixtures import operation, output, source, write_flow


FIXTURE = Path(__file__).parent / 'prep_dialect_samples' / 'quick_calc_evidence.json'


class TestPrepQuickCalcConversion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evidence = json.loads(FIXTURE.read_text(encoding='utf-8'))

    def test_numeric_type_inference_ignores_full_line_comments(self):
        fields = [
            {'name': 'Quantity', 'type': 'integer'},
            {'name': 'Unit Price', 'type': 'integer'},
        ]
        expression = '// Total line value\n[Quantity]*[Unit Price]'
        self.assertEqual(_expression_type(expression, fields), 'integer')

    def convert(self, payload, field_type='string'):
        node = {**deepcopy(payload), 'name': 'Quick Calc', 'id': 'quick-calc',
                'nextNodes': []}
        upstream = {
            'name': 'Input',
            'fields': [{'name': payload['columnName'], 'type': field_type}],
            'm_query': 'let\n    Source = #table({}, {})\nin\n    Source',
        }
        results = {'input': upstream}
        before = deepcopy((node, upstream))
        with redirect_stdout(io.StringIO()):
            _process_transform_node(
                'quick-calc', node, {'quick-calc': node}, ['input'],
                'QuickCalcColumn', results, set(), 'Quick Calc',
            )
        self.assertEqual((node, upstream), before)
        return results['quick-calc']

    def test_observed_trim_spaces_generates_static_valid_m(self):
        payload = self.evidence['trim_spaces']['payload']
        normalized = normalize_prep_flow({
            'nodes': {'quick-calc': {
                **deepcopy(payload), 'id': 'quick-calc',
                'baseType': 'transform', 'nextNodes': [],
            }},
            'connections': {},
        })
        self.assertEqual(
            normalized['nodes']['quick-calc']['calcExpressionType'], 'TrimSpaces'
        )
        result = self.convert(payload)
        self.assertIn(
            'Table.TransformColumns(Source, {{"Calculation formula", Text.Trim}})',
            result['m_query'],
        )
        self.assertEqual(validate_m_query(result['m_query']), [])
        self.assertEqual(
            result['fields'], [{'name': 'Calculation formula', 'type': 'string'}]
        )
        operations = _extract_clean_operations([payload], {})
        self.assertEqual(operations[0]['type'], 'trim')
        self.assertEqual(operations[0]['column'], 'Calculation formula')

    def test_unverified_quick_calc_types_and_malformed_expressions_stay_blocked(self):
        payload = deepcopy(self.evidence['trim_spaces']['payload'])
        payload['calcExpressionType'] = 'RemoveSymbols'
        with self.assertRaisesRegex(ValueError, 'subtype has no verified conversion'):
            self.convert(payload)

        evidence_path = Path(__file__).parent / 'prep_dialect_samples' / 'range_filter_evidence.json'
        remove_letters = json.loads(evidence_path.read_text(encoding='utf-8'))[
            'quick_calc_remove_letters']['payload']
        payload = deepcopy(remove_letters)
        payload['expression'] = "REGEXP_REPLACE([Sales], '[[:alpha:]]', '')"
        with self.assertRaisesRegex(ValueError, 'does not match the observed producer form'):
            self.convert(payload)

    def test_requires_existing_string_column(self):
        payload = self.evidence['trim_spaces']['payload']
        with self.assertRaisesRegex(ValueError, 'requires a string field'):
            self.convert(payload, field_type='real')

    def test_remove_letters_uses_pinned_unicode_m_with_runtime_warning(self):
        evidence_path = Path(__file__).parent / 'prep_dialect_samples' / 'range_filter_evidence.json'
        payload = json.loads(evidence_path.read_text(encoding='utf-8'))[
            'quick_calc_remove_letters']['payload']
        input_node = source('quick-calc', name='PublicInput')
        input_node['fields'] = [{'name': 'Sales', 'type': 'string'}]
        flow = {
            'nodes': {
                'input': input_node,
                'quick-calc': operation('QuickCalcColumn', 'result', **payload),
                'result': output('CleanSales', base_type=True),
            },
            'connections': {},
        }
        with tempfile.TemporaryDirectory() as directory:
            profile = analyze_flow(
                write_flow(directory, flow), include_m_queries=True)

        self.assertEqual([item.name for item in profile.outputs], ['CleanSales'])
        query = profile.m_queries['CleanSales']
        self.assertIn('Unicode 16.0 General_Category=Letter', query)
        self.assertIn('Character.ToNumber', query)
        self.assertIn('Number.IntegerDivide', query)
        self.assertLess(len(query), 30000)
        self.assertEqual(validate_m_query(query), [])
        warning = next(item for item in profile.diagnostics
                       if item['code'] == 'quick_calc_unicode_version_pinned')
        self.assertEqual(warning['severity'], 'warning')
        self.assertFalse(warning['blocks_conversion'])
        self.assertNotIn('conversion_error', profile.assessment)

    def test_remove_letters_fails_closed_for_another_unicode_database(self):
        _unicode_letter_ranges.cache_clear()
        try:
            with patch('prep_flow_parser.unicodedata.unidata_version', '15.0.0'):
                with self.assertRaisesRegex(ValueError, 'requires Unicode data 16.0.0'):
                    _unicode_letter_ranges()
        finally:
            _unicode_letter_ranges.cache_clear()

if __name__ == '__main__':
    unittest.main()
