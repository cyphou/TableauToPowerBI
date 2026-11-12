"""Producer-backed Prep RangeFilter conversion; no native runtime claim."""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tableau_export'))

from prep_flow_parser import _process_transform_node
from powerbi_import.m_validator import validate_m_query


FIXTURE = Path(__file__).parent / 'prep_dialect_samples' / 'range_filter_evidence.json'


class TestPrepRangeFilterConversion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evidence = json.loads(FIXTURE.read_text(encoding='utf-8'))

    def convert(self, payload, field_type='real'):
        node = {**deepcopy(payload), 'name': 'Range', 'id': 'range', 'nextNodes': []}
        upstream = {
            'name': 'Input',
            'fields': [{'name': next(iter(payload['ranges'])), 'type': field_type}],
            'm_query': 'let\n    Source = #table({}, {})\nin\n    Source',
        }
        results = {'input': upstream}
        before = deepcopy((node, upstream))
        with redirect_stdout(io.StringIO()):
            _process_transform_node(
                'range', node, {'range': node}, ['input'], 'RangeFilter',
                results, set(), 'Range',
            )
        self.assertEqual((node, upstream), before)
        return results['range']

    def test_pinned_numeric_exclusion_converts_with_boundary_and_null_semantics(self):
        payload = self.evidence['numeric_exclusion']['payload']
        result = self.convert(payload)
        self.assertIn(
            'Table.SelectRows(Source, each if [Discount] = null then true else '
            'not ([Discount] >= 17.0 and [Discount] < 18.0))',
            result['m_query'],
        )
        self.assertEqual(validate_m_query(result['m_query']), [])
        self.assertEqual(result['fields'], [{'name': 'Discount', 'type': 'real'}])

    def test_observed_null_range_exclusion_filters_out_nulls(self):
        payload = self.evidence['null_exclusion']['payload']
        result = self.convert(payload, field_type='string')
        self.assertIn(
            'Table.SelectRows(Source, each if [#"Order ID"] = null then false else true)',
            result['m_query'],
        )
        self.assertEqual(validate_m_query(result['m_query']), [])
        self.assertEqual(result['fields'], [{'name': 'Order ID', 'type': 'string'}])

    def test_observed_remove_letters_quick_calc_generates_pinned_unicode_m(self):
        payload = self.evidence['quick_calc_remove_letters']['payload']
        node = {**deepcopy(payload), 'name': 'Quick Calc', 'id': 'quick-calc',
                'nextNodes': []}
        upstream = {
            'name': 'Input',
            'fields': [{'name': 'Sales', 'type': 'string'}],
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
        query = results['quick-calc']['m_query']
        self.assertIn('Unicode 16.0 General_Category=Letter', query)
        self.assertIn('Text.ToList', query)
        self.assertEqual(validate_m_query(query), [])

    def test_rejects_non_numeric_fields_and_multiple_intervals(self):
        payload = deepcopy(self.evidence['numeric_exclusion']['payload'])
        with self.assertRaisesRegex(ValueError, 'numeric fields only'):
            self.convert(payload, field_type='string')

        payload['exclude'] = False
        with self.assertRaisesRegex(ValueError, 'exclude=true semantics only'):
            self.convert(payload)

        payload = deepcopy(self.evidence['numeric_exclusion']['payload'])
        payload['ranges']['Discount'].append(
            deepcopy(payload['ranges']['Discount'][0])
        )
        with self.assertRaisesRegex(ValueError, 'exactly one interval'):
            self.convert(payload)


if __name__ == '__main__':
    unittest.main()
