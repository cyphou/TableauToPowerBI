"""Production Remap regressions: static M contracts, not native M execution.

Observed payloads retain the attribution in prep_dialect_samples/remap_evidence.json.
All flow connections below are invented public placeholders.
"""

import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy

from tests.prep_flow_fixtures import operation, output, source, write_flow

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tableau_export'))

from prep_flow_parser import (
    _process_transform_node, merge_prep_with_workbook, parse_prep_flow,
)
from powerbi_import.tmdl_generator import generate_tmdl


FIXTURE = Path(__file__).parent / 'prep_dialect_samples' / 'remap_evidence.json'
PATHS = ('standalone', 'beforeActionGroup', 'actionGroup', 'afterActionGroup')


def remap(values=None, column='Label'):
    return {
        'nodeType': '.v2018_3_3.Remap',
        'columnName': column,
        'values': {'"B"': ['"A"']} if values is None else values,
        'groupMethodProps': None,
    }


def transform_node(payload, path):
    if path == 'standalone':
        return {**deepcopy(payload), 'name': 'Mapped', 'nextNodes': []}
    return operation('SuperTransform', **{
        path: {'actions': [deepcopy(payload)]},
    })


def fields_for(column):
    return [
        {'name': 'ID', 'type': 'integer'},
        {'name': column, 'type': 'string'},
        {'name': 'Amount', 'type': 'real'},
    ]


class TestPrepRemapConversion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evidence = json.loads(FIXTURE.read_text(encoding='utf-8'))

    def convert(self, payload, path, fields=None):
        """Exercise the actual standalone/grouped dispatcher and injected M."""
        node = transform_node(payload, path)
        upstream = {
            'name': 'PublicInput', 'connection': {}, 'table': {},
            'm_query': 'let\n    Source = #table({}, {})\nin\n    Source',
            'fields': fields_for(payload['columnName']) if fields is None else fields,
        }
        nodes = {'mapped': node}
        results = {'input': upstream}
        before = deepcopy((payload, nodes, upstream))
        try:
            with redirect_stdout(io.StringIO()):
                _process_transform_node(
                    'mapped', node, nodes, ['input'],
                    'Remap' if path == 'standalone' else 'SuperTransform',
                    results, set(), 'Mapped',
                )
            return results['mapped']
        finally:
            self.assertEqual((payload, nodes, upstream), before)

    def assert_transform(self, query, column_literal, sources, targets):
        # Check production output itself: one lookup on the original cell, not
        # successive ReplaceValue steps or an independent test-side evaluator.
        expected = (
            'Table.TransformColumns(Source, {{' + column_literal +
            ', each if _ = null then null else let position = '
            'List.PositionOf({' + sources +
            '}, _, Occurrence.First, Comparer.Ordinal) '
            'in if position = -1 then _ else {' + targets +
            '}{position}, type text}})'
        )
        self.assertIn(expected, query)
        self.assertEqual(query.count('Table.TransformColumns('), 1)
        self.assertEqual(query.count('List.PositionOf('), 1)
        self.assertNotIn('Table.ReplaceValue', query)
        self.assertNotIn('Comparer.OrdinalIgnoreCase', query)
        self.assertNotIn('Text.Lower', query)
        self.assertNotIn('Text.Upper', query)
        self.assertNotIn('Fuzzy', query)
        self.assertNotIn('{prev}', query)
        self.assertRegex(query, r'Result = #"Remapped Values"\s+in\s+Result\s*$')

    def test_both_observed_payloads_use_one_exact_text_transform(self):
        expected = {
            'discounts_remap': ('"0", "None"', '"0", "0"'),
            'generic_multigroup_remap': (
                '"label.1", "Label 1x", "Label_1", "Label_2", "label-2"',
                '"Label_1", "Label_1", "Label_1", "Label_2", "Label_2"',
            ),
        }
        for key, (sources, targets) in expected.items():
            payload = self.evidence[key]['payload']
            for path in PATHS:
                with self.subTest(fixture=key, path=path):
                    result = self.convert(payload, path)
                    self.assertEqual(result['fields'], fields_for(payload['columnName']))
                    self.assert_transform(
                        result['m_query'], '"' + payload['columnName'] + '"',
                        sources, targets,
                    )

    def test_chained_targets_are_simultaneous_and_case_sensitive(self):
        payload = remap({'"B"': ['"A"'], '"C"': ['"B"'], '"lower"': ['"a"']})
        for path in PATHS:
            with self.subTest(path=path):
                result = self.convert(payload, path)
                self.assert_transform(
                    result['m_query'], '"Label"', '"A", "B", "a"',
                    '"B", "C", "lower"',
                )
                self.assertEqual(result['fields'], fields_for('Label'))

    def test_quotes_controls_escape_sequences_and_step_sentinel_are_safe(self):
        column = 'Label"\n#(lf){prev}'
        src = 'a"\r\n\t#(lf){prev}'
        target = 'b"\n#(cr){prev}'
        payload = remap({json.dumps(target): [json.dumps(src)]}, column)
        for path in PATHS:
            with self.subTest(path=path):
                result = self.convert(payload, path)
                self.assert_transform(
                    result['m_query'],
                    '"Label""#(lf)#(#)(lf)#(007B)prev}"',
                    '"a""#(cr)#(lf)#(tab)#(#)(lf)#(007B)prev}"',
                    '"b""#(lf)#(#)(cr)#(007B)prev}"',
                )
                self.assertEqual(result['fields'], fields_for(column))

    def test_idempotent_duplicate_source_is_not_ambiguous(self):
        for path in PATHS:
            with self.subTest(path=path):
                result = self.convert(remap({'"B"': ['"A"', '"A"']}), path)
                self.assert_transform(result['m_query'], '"Label"', '"A"', '"B"')

    def test_missing_field_and_nonstring_upstream_types_reject(self):
        cases = [
            ([], "Prep Remap: column 'Label' is not in the upstream schema"),
            *[([{'name': 'Label', 'type': dtype}],
               "Prep Remap column 'Label' requires upstream string type")
              for dtype in ('integer', 'real', 'boolean', 'date', 'datetime', 'unknown')],
        ]
        for path in PATHS:
            for fields, message in cases:
                with self.subTest(path=path, fields=fields):
                    with self.assertRaisesRegex(ValueError, re.escape(message)):
                        self.convert(remap(), path, fields)

    def test_invalid_encoded_maps_reject_without_mutation(self):
        cases = [
            ({}, 'values must be a nonempty object'),
            ([], 'values must be a nonempty object'),
            (None, 'values must be a nonempty object'),
            ({'"B"': []}, 'sources must be nonempty lists'),
            ({'"B"': '"A"'}, 'sources must be nonempty lists'),
            ({'"B"': None}, 'sources must be nonempty lists'),
            ({'bad': ['"A"']}, 'invalid JSON-encoded string'),
            ({'"B"': ['bad']}, 'invalid JSON-encoded string'),
            ({1: ['"A"']}, 'values must be JSON-encoded strings'),
            ({'"B"': [1]}, 'values must be JSON-encoded strings'),
            ({'"B"': [None]}, 'values must be JSON-encoded strings'),
            ({'"B"': ['"A"'], '"C"': ['"A"']},
             "conflicting mappings for source 'A'"),
        ]
        for encoded in ('1', 'null', 'true', '[]', '{}'):
            cases.extend([
                ({encoded: ['"A"']}, 'supports only decoded string values'),
                ({'"B"': [encoded]}, 'supports only decoded string values'),
            ])
        for path in PATHS:
            for values, message in cases:
                with self.subTest(path=path, values=values):
                    payload = remap()
                    payload['values'] = values
                    with self.assertRaisesRegex(ValueError, re.escape('Prep Remap ' + message)):
                        self.convert(payload, path)

    def test_column_name_is_required(self):
        for path in PATHS:
            for column in ('', None, 42):
                with self.subTest(path=path, column=column):
                    with self.assertRaisesRegex(ValueError, 'Prep Remap requires a nonempty columnName'):
                        self.convert(remap(column=column), path, fields_for('Label'))

    def test_remaining_seven_dialects_still_reject(self):
        for dialect in (
            'ValueFilter', 'RichRangeFilter', 'RangeFilter',
            'RichDiscreteValueFilter', 'QuickCalcColumn',
            'RichWildcardFilter', 'RichNullFilter',
        ):
            for path in PATHS:
                with self.subTest(dialect=dialect, path=path):
                    payload = remap()
                    payload['nodeType'] = '.v2018_3_3.' + dialect
                    node = transform_node(payload, path)
                    upstream = {
                        'fields': fields_for('Label'),
                        'm_query': 'let Source = #table({}, {}) in Source',
                    }
                    results = {'input': upstream}
                    before = deepcopy((node, upstream))
                    with self.assertRaisesRegex(
                        ValueError, re.escape("unsupported or incomplete clean action '" + dialect + "'"),
                    ), redirect_stdout(io.StringIO()):
                        _process_transform_node(
                            'mapped', node, {'mapped': node}, ['input'],
                            dialect if path == 'standalone' else 'SuperTransform',
                            results, set(), 'Mapped',
                        )
                    self.assertEqual((node, upstream), before)
                    self.assertNotIn('mapped', results)


class TestPrepRemapTMDLRoundTrip(unittest.TestCase):
    def test_public_placeholder_flow_schema_and_override_survive_to_tmdl(self):
        evidence = json.loads(FIXTURE.read_text(encoding='utf-8'))
        for key in ('discounts_remap', 'generic_multigroup_remap'):
            payload = evidence[key]['payload']
            for path in ('standalone', 'actionGroup'):
                with self.subTest(fixture=key, path=path), tempfile.TemporaryDirectory() as directory:
                    mapped = transform_node(payload, path)
                    mapped['nextNodes'] = [{'nextNodeId': 'out'}]
                    input_node = source('mapped', name='PublicInput')
                    fields = fields_for(payload['columnName'])
                    input_node['fields'] = fields
                    flow = {'nodes': {
                        'input': input_node, 'mapped': mapped,
                        'out': output('PublicOutput'),
                    }, 'connections': {}}
                    before = deepcopy(flow)
                    with redirect_stdout(io.StringIO()):
                        parsed = parse_prep_flow(write_flow(directory, flow))
                    self.assertEqual(flow, before)
                    self.assertEqual(len(parsed), 1)
                    columns = [{'name': f['name'], 'datatype': f['type']} for f in fields]
                    self.assertEqual(parsed[0]['tables'][0]['columns'], columns)
                    query = parsed[0]['m_query_override']
                    self.assertEqual(query.count('Table.TransformColumns('), 1)
                    workbook = [{
                        'name': 'PublicWorkbook',
                        'connection': {'type': 'published'},
                        'tables': [{'name': 'PublicOutput', 'columns': [
                            {'name': 'ID', 'datatype': 'integer'},
                        ]}],
                        'calculations': [], 'relationships': [],
                    }]
                    originals = deepcopy((parsed, workbook))
                    with redirect_stdout(io.StringIO()):
                        merged, coverage = merge_prep_with_workbook(
                            parsed, workbook, return_report=True,
                        )
                    self.assertEqual((parsed, workbook), originals)
                    self.assertEqual(coverage['status'], 'complete')
                    self.assertEqual(merged[0]['tables'][0]['columns'], columns)
                    self.assertEqual(merged[0]['m_query_overrides']['PublicOutput'], query)
                    model_dir = os.path.join(directory, 'PublicModel.SemanticModel')
                    extra = {key: [] for key in (
                        'hierarchies', 'sets', 'groups', 'bins', 'aliases',
                        'parameters', 'user_filters',
                    )}
                    extra['_datasources'] = merged
                    with redirect_stdout(io.StringIO()):
                        generate_tmdl(merged, 'PublicModel', extra, model_dir)
                    table = Path(model_dir, 'definition', 'tables', 'PublicOutput.tmdl')
                    self.assertTrue(table.is_file())
                    text = table.read_text(encoding='utf-8')
                    self.assertEqual(len(re.findall(r'(?m)^\s*column ', text)), 3)
                    for name, datatype in (
                        ('ID', 'int64'), (payload['columnName'], 'string'), ('Amount', 'double'),
                    ):
                        self.assertRegex(
                            text, r"(?m)^\s*column '?" + re.escape(name) +
                            r"'?\s*\n\s*dataType: " + datatype + r'\s*$',
                        )
                    # Normalize TMDL indentation only, preserving every M line.
                    partition = text.split('source =', 1)[1]
                    self.assertEqual(
                        [line.strip() for line in partition.strip().splitlines()][:len(query.splitlines())],
                        [line.strip() for line in query.splitlines()],
                    )


if __name__ == '__main__':
    unittest.main()
