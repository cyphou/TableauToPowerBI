"""Production-path regressions for evidence-backed Prep manual unions.

The only supported manual-union dialect exercised here is the observed
``SuperUnion`` / ``actionNode: SimpleUnion`` payload with
``namespaceFieldMappings``. The producer shape is checked against the
CC-BY-4.0 Tableau Prep book example; synthetic values and connection paths
below are invented.

Evidence scope: these tests exercise flow parsing, schema contracts, generated
M text, workbook binding, and TMDL serialization. They do not execute Power
Query M. Append/null-fill/duplicate and row-order runtime behavior is therefore
``not_run``; the M assertions pin the generated append/no-dedup contract only.
"""

import io
import json
import os
import re
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

from prep_flow_parser import merge_prep_with_workbook, parse_prep_flow

from migrate import run_standalone_prep
from powerbi_import.tmdl_generator import generate_tmdl
from tests.prep_flow_fixtures import write_flow

REAL_WORLD_FLOW = ROOT / 'examples' / 'real_world' / 'tableau_prep_book.tfl'
REAL_WORLD_SOURCES = ROOT / 'examples' / 'real_world' / 'SOURCES.md'

NS_WEST = 'Union-Namespace-synthetic-west'
NS_EAST = 'Union-Namespace-synthetic-east'
NS_THIRD = 'Union-Namespace-synthetic-third'


def _input_node(name, namespace, fields):
    """Make an input whose edge carries Tableau's union namespace identity."""
    return {
        'baseType': 'input',
        'nodeType': '.v1.LoadCsv',
        'name': name,
        'connectionAttributes': {
            'class': 'csv',
            'filename': f'{name}.csv',
        },
        'fields': deepcopy(fields),
        'nextNodes': [{
            'namespace': 'Default',
            'nextNodeId': 'union',
            'nextNamespace': namespace,
        }],
    }


def _simple_union_node(namespace_mappings, *, fields=None, updates=None):
    """Build the licensed example's SuperUnion/SimpleUnion envelope shape."""
    node = {
        'nodeType': '.v2018_2_3.SuperUnion',
        'name': 'Synthetic Union',
        'id': 'synthetic-union-wrapper',
        'baseType': 'superNode',
        'nextNodes': [{
            'namespace': 'Default',
            'nextNodeId': 'out',
            'nextNamespace': 'Left',
        }],
        'serialize': False,
        'description': None,
        'beforeActionAnnotations': [],
        'afterActionAnnotations': [],
        'actionNode': {
            'nodeType': '.v1.SimpleUnion',
            'name': 'Synthetic Union',
            'id': 'synthetic-simple-union',
            'baseType': 'transform',
            'nextNodes': [],
            'serialize': False,
            'description': None,
            'namespaceFieldMappings': deepcopy(namespace_mappings),
        },
    }
    if fields is not None:
        node['fields'] = deepcopy(fields)
    if updates:
        node.update(deepcopy(updates))
    return node


def _manual_union_flow(
    branches,
    mappings,
    *,
    mapping_order=None,
    union_fields=None,
    output_fields=None,
    union_updates=None,
    output_name='UnionOutput',
):
    """Create a synthetic flow, intentionally decoupling node/map list order."""
    namespaces = [branch['namespace'] for branch in branches]
    order = list(mapping_order if mapping_order is not None else namespaces)
    namespace_mappings = [
        {
            'namespaceName': namespace,
            'fieldMappings': deepcopy(mappings.get(namespace, {})),
        }
        for namespace in order
    ]

    nodes = {}
    # Reverse node insertion order so namespace identity, not incidental
    # dictionary position, must associate each input with its field mappings.
    for branch in reversed(branches):
        nodes[branch['id']] = _input_node(
            branch['name'], branch['namespace'], branch['fields'])

    nodes['union'] = _simple_union_node(
        namespace_mappings, fields=union_fields, updates=union_updates)
    output = {
        'baseType': 'output',
        'nodeType': '.v1.PublishExtract',
        'name': output_name,
        'nextNodes': [],
    }
    if output_fields is not None:
        output['fields'] = deepcopy(output_fields)
    nodes['out'] = output
    return {'nodes': nodes, 'connections': {}}


def _parse_flow(flow):
    with tempfile.TemporaryDirectory() as directory:
        path = write_flow(directory, flow)
        with redirect_stdout(io.StringIO()):
            return parse_prep_flow(path)


def _output_datasource(datasources):
    return next(ds for ds in datasources if ds.get('prep_role') == 'output')


def _datasource_for_table(datasources, table_name):
    return next(
        ds for ds in datasources
        if ds.get('tables') and ds['tables'][0].get('name') == table_name
    )


def _columns(datasource):
    return [
        (column['name'], column['datatype'])
        for column in datasource['tables'][0].get('columns', [])
    ]


def _m_literal(value):
    """Expected M text literal for static code-generation assertions."""
    return '"' + value.replace('"', '""') + '"'


class TestSimpleUnionProducerEvidence(unittest.TestCase):
    """Pin the exact public producer shape before exercising synthetic flows."""

    @classmethod
    def setUpClass(cls):
        cls.flow = json.loads(REAL_WORLD_FLOW.read_text(encoding='utf-8'))
        cls.source_notes = REAL_WORLD_SOURCES.read_text(encoding='utf-8')
        matches = [
            node for node in cls.flow['nodes'].values()
            if node.get('nodeType') == '.v2018_2_3.SuperUnion'
        ]
        if len(matches) != 1:
            raise AssertionError(
                f'Expected one observed SuperUnion, found {len(matches)}')
        cls.union = matches[0]

    def test_public_attribution_and_superunion_simpleunion_shape(self):
        self.assertIn('tableau_prep_book.tfl', self.source_notes)
        self.assertIn('aloth/tableau-book-resources', self.source_notes)
        self.assertIn('CC-BY-4.0', self.source_notes)
        self.assertIn('Alexander Loth', self.source_notes)
        self.assertIn('Wiley', self.source_notes)

        union = self.union
        self.assertEqual(union['baseType'], 'superNode')
        self.assertEqual(union['nodeType'], '.v2018_2_3.SuperUnion')
        self.assertIsInstance(union['nextNodes'], list)
        self.assertEqual(len(union['nextNodes']), 1)

        action = union['actionNode']
        self.assertEqual(action['baseType'], 'transform')
        self.assertEqual(action['nodeType'], '.v1.SimpleUnion')
        self.assertEqual(action['nextNodes'], [])
        self.assertNotIn('namespaceFieldMappings', union)
        self.assertEqual(
            set(action['namespaceFieldMappings'][0]),
            {'namespaceName', 'fieldMappings'},
        )
        self.assertTrue(all(
            isinstance(entry['namespaceName'], str) and entry['namespaceName']
            and isinstance(entry['fieldMappings'], dict)
            for entry in action['namespaceFieldMappings']
        ))

    def test_input_nextnamespace_values_key_the_observed_field_maps(self):
        union_id = self.union['id']
        incoming = []
        for node in self.flow['nodes'].values():
            for edge in node.get('nextNodes', []):
                if edge.get('nextNodeId') == union_id:
                    incoming.append(edge)

        entries = self.union['actionNode']['namespaceFieldMappings']
        names = [entry['namespaceName'] for entry in entries]
        self.assertEqual(len(names), 3)
        self.assertEqual(len(set(names)), len(names))
        self.assertEqual(len(incoming), 3)
        self.assertTrue(all(edge.get('namespace') == 'Default'
                            for edge in incoming))
        self.assertEqual(
            {edge.get('nextNamespace') for edge in incoming},
            set(names),
        )
        mapped = [
            entry['fieldMappings'] for entry in entries
            if entry['fieldMappings']
        ]
        self.assertEqual(mapped, [{'[Discounts]': 'Discount'}])

    def test_public_simplejoin_inside_superjoin_remains_fail_closed(self):
        joins = [
            node for node in self.flow['nodes'].values()
            if node.get('nodeType') == '.v2018_2_3.SuperJoin'
        ]
        self.assertEqual(len(joins), 1)
        public_join = deepcopy(joins[0])
        self.assertEqual(public_join['actionNode']['nodeType'], '.v1.SimpleJoin')
        self.assertEqual(public_join['actionNode']['joinType'], 'left')
        self.assertEqual(public_join['actionNode']['conditions'], [{
            'leftExpression': '[Order ID]',
            'rightExpression': '[Order ID]',
            'comparator': '==',
        }])

        incoming_namespaces = [
            edge['nextNamespace']
            for node in self.flow['nodes'].values()
            for edge in node.get('nextNodes', [])
            if edge.get('nextNodeId') == public_join['id']
        ]
        self.assertCountEqual(incoming_namespaces, ['Left', 'Right'])

        # Isolate the exact producer action payload and edge namespaces. Using
        # a neutral display name ensures rejection is semantic, not name-based.
        public_join['name'] = 'Synthetic Join'
        public_join['actionNode']['name'] = 'Synthetic Join'
        public_join['nextNodes'] = [{
            'namespace': 'Default',
            'nextNodeId': 'out',
            'nextNamespace': 'Default',
        }]
        fields = [{'name': 'Order ID', 'type': 'string'}]
        left = _input_node('SyntheticLeft', 'Left', fields)
        right = _input_node('SyntheticRight', 'Right', fields)
        left['nextNodes'][0]['nextNodeId'] = 'join'
        right['nextNodes'][0]['nextNodeId'] = 'join'
        flow = {
            'nodes': {
                'left': left,
                'right': right,
                'join': public_join,
                'out': {
                    'baseType': 'output',
                    'nodeType': '.v1.PublishExtract',
                    'name': 'JoinOutput',
                    'nextNodes': [],
                },
            },
            'connections': {},
        }

        with self.assertRaisesRegex(
            ValueError,
            r"actionNode\.nodeType 'SimpleJoin' conflicts with wrapper 'SuperJoin'",
        ):
            _parse_flow(flow)

    def test_public_discount_union_mapping_rejects_string_to_real_without_cast(self):
        nodes = self.flow['nodes']

        def find_node(name):
            return next(node for node in nodes.values()
                        if node.get('name') == name)

        def public_field(node, name):
            matches = [field for field in node.get('fields', [])
                       if field.get('name') == name]
            self.assertEqual(len(matches), 1, (node.get('name'), name))
            return deepcopy(matches[0])

        west = find_node('Orders_West')
        south = find_node('orders_south_2015')
        central = find_node('Orders_Central')
        central_container = find_node('Dates & Fields')
        self.assertEqual(public_field(west, 'Discount')['type'], 'real')
        self.assertEqual(public_field(south, 'Discount')['type'], 'real')
        self.assertEqual(public_field(central, 'Discounts')['type'], 'string')

        def edge_namespace(node, target_id):
            matches = [edge for edge in node.get('nextNodes', [])
                       if edge.get('nextNodeId') == target_id]
            self.assertEqual(len(matches), 1, node.get('name'))
            return matches[0]['nextNamespace']

        central_namespace = edge_namespace(
            central_container, self.union['id'])
        branches = [
            {
                'id': 'west',
                'name': 'Public West',
                'namespace': edge_namespace(west, self.union['id']),
                'fields': [public_field(west, 'Discount')],
            },
            {
                'id': 'south',
                'name': 'Public South',
                'namespace': edge_namespace(south, self.union['id']),
                'fields': [public_field(south, 'Discount')],
            },
            {
                'id': 'central',
                'name': 'Public Central',
                'namespace': central_namespace,
                'fields': [public_field(central, 'Discounts')],
            },
        ]
        # This reduced graph preserves the actual SimpleUnion mapping record,
        # its three incoming namespace identities, and the public input field
        # types. It introduces no cast or declared union/output schema; the
        # original flow's value remap is not a type cast. It does not claim the
        # complete public flow succeeds (its SimpleJoin remains unsupported).
        public_records = deepcopy(
            self.union['actionNode']['namespaceFieldMappings'])
        mapping_by_namespace = {
            record['namespaceName']: record['fieldMappings']
            for record in public_records
        }
        flow = _manual_union_flow(branches, mapping_by_namespace)
        flow['nodes']['union'] = deepcopy(self.union)
        flow['nodes']['union']['nextNodes'] = [{
            'namespace': 'Default',
            'nextNodeId': 'out',
            'nextNamespace': 'Left',
        }]
        self.assertEqual(
            flow['nodes']['union']['actionNode']['namespaceFieldMappings'],
            public_records,
        )
        self.assertNotIn('fields', flow['nodes']['union'])
        self.assertNotIn('fields', flow['nodes']['out'])

        with self.assertRaisesRegex(
            ValueError, r"Prep union incompatible types for 'Discount'"
        ):
            _parse_flow(flow)


class TestPrepUnionMappingConversion(unittest.TestCase):
    """Exercise namespace-aware SimpleUnion conversion through parse_prep_flow."""

    def test_mapping_is_selected_by_each_inputs_namespace(self):
        branches = [
            {
                'id': 'west',
                'name': 'WestRows',
                'namespace': NS_WEST,
                'fields': [
                    {'name': 'West Legacy', 'type': 'string'},
                    {'name': 'WestOnly', 'type': 'integer'},
                ],
            },
            {
                'id': 'east',
                'name': 'EastRows',
                'namespace': NS_EAST,
                'fields': [
                    {'name': 'East Legacy', 'type': 'string'},
                    {'name': 'EastOnly', 'type': 'boolean'},
                ],
            },
        ]
        flow = _manual_union_flow(
            branches,
            {
                NS_WEST: {'[West Legacy]': 'Unified Label'},
                NS_EAST: {'[East Legacy]': 'Unified Label'},
            },
            # Reverse mapping order as well as node insertion order.
            mapping_order=[NS_EAST, NS_WEST],
        )
        datasources = _parse_flow(flow)
        result = _output_datasource(datasources)

        self.assertCountEqual(
            _columns(result),
            [
                ('Unified Label', 'string'),
                ('WestOnly', 'integer'),
                ('EastOnly', 'boolean'),
            ],
        )
        query = result['m_query_override']
        self.assertIn('Table.Combine(', query)
        self.assertIn(_m_literal('West Legacy'), query)
        self.assertIn(_m_literal('East Legacy'), query)
        self.assertIn(_m_literal('Unified Label'), query)

    def test_swap_renames_are_simultaneous_across_occupied_destinations(self):
        branches = [
            {
                'id': 'swap',
                'name': 'SwapRows',
                'namespace': NS_WEST,
                'fields': [
                    {'name': 'Alpha', 'type': 'integer'},
                    {'name': 'Beta', 'type': 'string'},
                ],
            },
            {
                'id': 'control',
                'name': 'ControlRows',
                'namespace': NS_EAST,
                'fields': [
                    {'name': 'Alpha', 'type': 'string'},
                    {'name': 'Beta', 'type': 'integer'},
                ],
            },
        ]
        flow = _manual_union_flow(
            branches,
            {NS_WEST: {'[Alpha]': 'Beta', '[Beta]': 'Alpha'}, NS_EAST: {}},
        )
        result = _output_datasource(_parse_flow(flow))
        # The schema types follow each original source column through the swap;
        # destinations are not overwritten or cascaded in-place.
        self.assertCountEqual(
            _columns(result),
            [('Alpha', 'string'), ('Beta', 'integer')],
        )
        query = result['m_query_override']
        for value in ('Alpha', 'Beta'):
            self.assertIn(_m_literal(value), query)
        self.assertIn('Table.Combine(', query)

    def test_rename_chain_into_an_occupied_name_does_not_cascade(self):
        branches = [
            {
                'id': 'chain',
                'name': 'ChainRows',
                'namespace': NS_WEST,
                'fields': [
                    {'name': 'Old', 'type': 'integer'},
                    {'name': 'Occupied', 'type': 'string'},
                ],
            },
            {
                'id': 'control',
                'name': 'ControlRows',
                'namespace': NS_EAST,
                'fields': [
                    {'name': 'Occupied', 'type': 'integer'},
                    {'name': 'New', 'type': 'string'},
                ],
            },
        ]
        flow = _manual_union_flow(
            branches,
            {NS_WEST: {'[Old]': 'Occupied', '[Occupied]': 'New'}, NS_EAST: {}},
        )
        result = _output_datasource(_parse_flow(flow))

        self.assertCountEqual(
            _columns(result),
            [('Occupied', 'integer'), ('New', 'string')],
        )
        query = result['m_query_override']
        for value in ('Old', 'Occupied', 'New'):
            self.assertIn(_m_literal(value), query)

    def test_unmapped_fields_schema_union_type_widening_and_append_contract(self):
        branches = [
            {
                'id': 'west',
                'name': 'WestSales',
                'namespace': NS_WEST,
                'fields': [
                    {'name': 'Legacy Amount', 'type': 'integer'},
                    {'name': 'WestOnly', 'type': 'string'},
                ],
            },
            {
                'id': 'east',
                'name': 'EastSales',
                'namespace': NS_EAST,
                'fields': [
                    {'name': 'Amount', 'type': 'real'},
                    {'name': 'EastOnly', 'type': 'string'},
                ],
            },
        ]
        flow = _manual_union_flow(
            branches,
            {NS_WEST: {'[Legacy Amount]': 'Amount'}, NS_EAST: {}},
        )
        datasources = _parse_flow(flow)
        result = _output_datasource(datasources)

        self.assertCountEqual(
            _columns(result),
            [
                ('Amount', 'real'),
                ('WestOnly', 'string'),
                ('EastOnly', 'string'),
            ],
        )
        west = _datasource_for_table(datasources, 'WestSales')
        east = _datasource_for_table(datasources, 'EastSales')
        self.assertNotIn('EastOnly', {name for name, _ in _columns(west)})
        self.assertNotIn('WestOnly', {name for name, _ in _columns(east)})

        query = result['m_query_override']
        self.assertEqual(query.count('Table.Combine('), 1)
        # Source-header validation uses List.Distinct on column names; only a
        # row-level table distinct would change the union's duplicate contract.
        self.assertNotRegex(query, r'\bTable\.Distinct\s*\(')
        self.assertIn('WestSales', query)
        self.assertIn('EastSales', query)
        # This pins the generated M append/no-dedup contract and schema union.
        # It does not execute M or claim row order/null values at runtime.

    def test_unicode_and_quotes_are_escaped_in_generated_m_field_identifiers(self):
        source_name = 'Legacy "café"'
        target_name = 'Canonical "été"'
        branches = [
            {
                'id': 'quoted',
                'name': 'QuotedRows',
                'namespace': NS_WEST,
                'fields': [{'name': source_name, 'type': 'string'}],
            },
            {
                'id': 'canonical',
                'name': 'CanonicalRows',
                'namespace': NS_EAST,
                'fields': [{'name': target_name, 'type': 'string'}],
            },
        ]
        flow = _manual_union_flow(
            branches,
            {NS_WEST: {f'[{source_name}]': target_name}, NS_EAST: {}},
        )
        result = _output_datasource(_parse_flow(flow))
        self.assertCountEqual(_columns(result), [(target_name, 'string')])

        query = result['m_query_override']
        self.assertIn(_m_literal(source_name), query)
        self.assertIn(_m_literal(target_name), query)
        self.assertNotIn('"Legacy "café""', query)

    def test_declared_schema_survives_standalone_binding_and_tmdl_export(self):
        declared = [
            {'name': 'Region', 'type': 'string'},
            {'name': 'Revenue', 'type': 'real'},
            {'name': 'EastOnly', 'type': 'string'},
        ]
        branches = [
            {
                'id': 'west',
                'name': 'WestRows',
                'namespace': NS_WEST,
                'fields': [
                    {'name': 'Legacy Revenue', 'type': 'integer'},
                    {'name': 'Region', 'type': 'string'},
                ],
            },
            {
                'id': 'east',
                'name': 'EastRows',
                'namespace': NS_EAST,
                'fields': [
                    {'name': 'Revenue', 'type': 'real'},
                    {'name': 'Region', 'type': 'string'},
                    {'name': 'EastOnly', 'type': 'string'},
                ],
            },
        ]
        flow = _manual_union_flow(
            branches,
            {NS_WEST: {'[Legacy Revenue]': 'Revenue'}, NS_EAST: {}},
            union_fields=declared,
            output_fields=declared,
        )
        parsed = _parse_flow(flow)
        query = _output_datasource(parsed)['m_query_override']
        self.assertEqual(_columns(_output_datasource(parsed)), [
            ('Region', 'string'),
            ('Revenue', 'real'),
            ('EastOnly', 'string'),
        ])

        standalone, standalone_report = merge_prep_with_workbook(
            parsed, [], return_report=True)
        self.assertEqual(standalone_report['status'], 'standalone')
        self.assertEqual(
            _columns(_output_datasource(standalone)),
            _columns(_output_datasource(parsed)),
        )
        self.assertEqual(
            _output_datasource(standalone)['m_query_override'], query)

        workbook = [{
            'name': 'SyntheticWorkbook',
            'connection': {'type': 'published'},
            'tables': [{
                'name': 'UnionOutput',
                'columns': [
                    {'name': name, 'datatype': dtype}
                    for name, dtype in _columns(_output_datasource(parsed))
                ],
            }],
            'calculations': [],
            'relationships': [],
        }]
        originals = deepcopy((parsed, workbook))
        merged, report = merge_prep_with_workbook(
            parsed, workbook, return_report=True)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual((parsed, workbook), originals)
        self.assertEqual(
            merged[0]['m_query_overrides']['UnionOutput'], query)

        with tempfile.TemporaryDirectory() as directory:
            extra = {key: [] for key in (
                'hierarchies', 'sets', 'groups', 'bins', 'aliases',
                'parameters', 'user_filters',
            )}
            extra['_datasources'] = merged
            model_dir = os.path.join(directory, 'UnionModel.SemanticModel')
            with redirect_stdout(io.StringIO()):
                generate_tmdl(merged, 'UnionModel', extra, model_dir)

            table_file = Path(
                model_dir, 'definition', 'tables', 'UnionOutput.tmdl')
            self.assertTrue(table_file.is_file())
            text = table_file.read_text(encoding='utf-8')
            self.assertEqual(len(re.findall(r'(?m)^\s*column ', text)), 3)
            for name, datatype in (
                ('Region', 'string'),
                ('Revenue', 'double'),
                ('EastOnly', 'string'),
            ):
                self.assertRegex(
                    text,
                    r"(?m)^\s*column '?" + re.escape(name) +
                    r"'?\s*\n\s*dataType: " + datatype + r'\s*$',
                )
            # The partition carries the same generated M; this remains a
            # static serialization check, not Power Query runtime validation.
            partition = text.split('source =', 1)[1]
            self.assertEqual(
                [line.strip() for line in partition.strip().splitlines()]
                [:len(query.splitlines())],
                [line.strip() for line in query.splitlines()],
            )


class TestPrepUnionMappingRejections(unittest.TestCase):
    """Malformed or unproven manual-union metadata must fail closed."""

    def assert_rejected(self, flow, message=None):
        if message is None:
            with self.assertRaises(ValueError):
                _parse_flow(flow)
        else:
            with self.assertRaisesRegex(ValueError, message):
                _parse_flow(flow)

    def _two_branch_flow(self, mappings=None):
        branches = [
            {
                'id': 'left',
                'name': 'LeftRows',
                'namespace': NS_WEST,
                'fields': [
                    {'name': 'Alpha', 'type': 'integer'},
                    {'name': 'Beta', 'type': 'string'},
                ],
            },
            {
                'id': 'right',
                'name': 'RightRows',
                'namespace': NS_EAST,
                'fields': [
                    {'name': 'Alpha', 'type': 'integer'},
                    {'name': 'Beta', 'type': 'string'},
                ],
            },
        ]
        return _manual_union_flow(
            branches,
            mappings or {NS_WEST: {}, NS_EAST: {}},
        )

    def test_unknown_mapped_source_field_is_rejected(self):
        flow = self._two_branch_flow({
            NS_WEST: {'[Not In Schema]': 'Alpha'},
            NS_EAST: {},
        })
        self.assert_rejected(flow)

    def test_empty_and_malformed_namespace_mapping_records_are_rejected(self):
        cases = []

        empty_records = self._two_branch_flow()
        empty_records['nodes']['union']['actionNode'][
            'namespaceFieldMappings'] = []
        cases.append((
            'empty list', empty_records,
            'namespaceFieldMappings must be a nonempty list',
        ))

        non_list_records = self._two_branch_flow()
        non_list_records['nodes']['union']['actionNode'][
            'namespaceFieldMappings'] = {}
        cases.append((
            'non-list records', non_list_records,
            'namespaceFieldMappings must be a nonempty list',
        ))

        malformed_records = [
            ('non-object record', None, r'namespaceFieldMappings\[0\]: expected object'),
            ('missing namespaceName', {'fieldMappings': {}}, 'expected exactly'),
            ('missing fieldMappings', {'namespaceName': NS_WEST}, 'expected exactly'),
            ('extra record property', {
                'namespaceName': NS_WEST,
                'fieldMappings': {},
                'mode': 'positional',
            }, 'expected exactly'),
            ('empty namespaceName', {
                'namespaceName': ' ',
                'fieldMappings': {},
            }, 'namespaceName: expected nonempty string'),
            ('non-string namespaceName', {
                'namespaceName': 7,
                'fieldMappings': {},
            }, 'namespaceName: expected nonempty string'),
            ('non-object fieldMappings', {
                'namespaceName': NS_WEST,
                'fieldMappings': [],
            }, 'fieldMappings: expected object'),
        ]
        for label, record, message in malformed_records:
            flow = self._two_branch_flow()
            flow['nodes']['union']['actionNode'][
                'namespaceFieldMappings'][0] = record
            cases.append((label, flow, message))

        for label, flow, message in cases:
            with self.subTest(case=label):
                self.assert_rejected(flow, message)

    def test_namespace_mapping_records_must_match_input_edge_namespaces(self):
        missing_record = self._two_branch_flow()
        records = missing_record['nodes']['union']['actionNode'][
            'namespaceFieldMappings']
        records[:] = [
            record for record in records
            if record['namespaceName'] != NS_WEST
        ]

        extra_record = self._two_branch_flow()
        extra_record['nodes']['union']['actionNode'][
            'namespaceFieldMappings'].append({
                'namespaceName': NS_THIRD,
                'fieldMappings': {},
            })

        for label, flow in (
            ('missing input record', missing_record),
            ('extra unconnected record', extra_record),
        ):
            with self.subTest(case=label):
                self.assert_rejected(flow, 'namespaceFieldMappings must match')

    def test_duplicate_incoming_edge_for_one_input_is_rejected(self):
        flow = self._two_branch_flow()
        input_edges = flow['nodes']['left']['nextNodes']
        duplicate = deepcopy(input_edges[0])
        # Keep the target namespace identical while preserving two distinct
        # serialized edge records through flow normalization.
        duplicate['namespace'] = 'SyntheticDuplicateSourceNamespace'
        input_edges.append(duplicate)

        self.assert_rejected(flow, 'duplicate upstream input identities')

    def test_mapping_requires_observed_brackets_and_string_targets(self):
        malformed_maps = [
            ('unbracketed source', {'Alpha': 'Unified Alpha'},
             r'source must use the observed \[Field\] reference form'),
            ('missing closing bracket', {'[Alpha': 'Unified Alpha'},
             r'source must use the observed \[Field\] reference form'),
            ('missing opening bracket', {'Alpha]': 'Unified Alpha'},
             r'source must use the observed \[Field\] reference form'),
            ('nested brackets', {'[A[lpha]]': 'Unified Alpha'},
             r'source must use the observed \[Field\] reference form'),
            ('empty source reference', {'[]': 'Unified Alpha'},
             r'source must use the observed \[Field\] reference form'),
            ('integer target', {'[Alpha]': 7},
             'target must be a nonempty field name'),
            ('null target', {'[Alpha]': None},
             'target must be a nonempty field name'),
            ('blank target', {'[Alpha]': ''},
             'target must be a nonempty field name'),
            ('whitespace target', {'[Alpha]': '  '},
             'target must be a nonempty field name'),
            ('list target', {'[Alpha]': ['Unified Alpha']},
             'target must be a nonempty field name'),
            ('object target', {'[Alpha]': {'name': 'Unified Alpha'}},
             'target must be a nonempty field name'),
        ]
        for label, mapping, message in malformed_maps:
            with self.subTest(case=label):
                flow = self._two_branch_flow({
                    NS_WEST: mapping,
                    NS_EAST: {},
                })
                self.assert_rejected(flow, message)

    def test_two_sources_in_one_namespace_cannot_conflict_on_target(self):
        flow = self._two_branch_flow({
            NS_WEST: {'[Alpha]': 'Shared', '[Beta]': 'Shared'},
            NS_EAST: {},
        })
        self.assert_rejected(flow)

    def test_case_insensitive_target_collisions_are_rejected(self):
        flow = self._two_branch_flow({
            NS_WEST: {'[Alpha]': 'Shared', '[Beta]': 'shared'},
            NS_EAST: {},
        })
        self.assert_rejected(flow)

    def test_unknown_and_duplicate_namespace_identities_are_rejected(self):
        cases = []

        unknown_edge = self._two_branch_flow()
        unknown_edge['nodes']['left']['nextNodes'][0]['nextNamespace'] = (
            'Union-Namespace-not-listed')
        cases.append(unknown_edge)

        missing_edge_namespace = self._two_branch_flow()
        del missing_edge_namespace['nodes']['left']['nextNodes'][0][
            'nextNamespace']
        cases.append(missing_edge_namespace)

        duplicate_input = self._two_branch_flow()
        duplicate_input['nodes']['right']['nextNodes'][0]['nextNamespace'] = NS_WEST
        cases.append(duplicate_input)

        duplicate_map = self._two_branch_flow()
        entries = duplicate_map['nodes']['union']['actionNode'][
            'namespaceFieldMappings']
        entries.append(deepcopy(entries[0]))
        cases.append(duplicate_map)

        for index, flow in enumerate(cases):
            with self.subTest(case=index):
                self.assert_rejected(flow)

    def test_unproven_union_dialects_and_metadata_are_rejected(self):
        cases = []

        wrong_action_node = self._two_branch_flow()
        wrong_action_node['nodes']['union']['actionNode']['nodeType'] = (
            '.v1.OtherUnion')
        cases.append(wrong_action_node)

        unknown_action_metadata = self._two_branch_flow()
        unknown_action_metadata['nodes']['union']['actionNode'][
            'mappingMode'] = 'by-position'
        cases.append(unknown_action_metadata)

        legacy_mapping_shape = self._two_branch_flow()
        legacy_mapping_shape['nodes']['union']['fieldMappings'] = {
            '[Alpha]': 'Unified Alpha',
        }
        cases.append(legacy_mapping_shape)

        conflicting_union_mode = self._two_branch_flow()
        conflicting_union_mode['nodes']['union']['unionType'] = 'automatic'
        cases.append(conflicting_union_mode)

        for index, flow in enumerate(cases):
            with self.subTest(case=index):
                self.assert_rejected(flow)

    def test_incompatible_types_after_mapping_are_rejected(self):
        branches = [
            {
                'id': 'left',
                'name': 'LeftRows',
                'namespace': NS_WEST,
                'fields': [{'name': 'Left Amount', 'type': 'string'}],
            },
            {
                'id': 'right',
                'name': 'RightRows',
                'namespace': NS_EAST,
                'fields': [{'name': 'Amount', 'type': 'real'}],
            },
        ]
        flow = _manual_union_flow(
            branches,
            {NS_WEST: {'[Left Amount]': 'Amount'}, NS_EAST: {}},
        )
        self.assert_rejected(flow, 'Prep union incompatible types')

    def test_mapped_target_cannot_collide_with_an_unmapped_field(self):
        branches = [
            {
                'id': 'left',
                'name': 'LeftRows',
                'namespace': NS_WEST,
                'fields': [
                    {'name': 'Legacy Alpha', 'type': 'integer'},
                    {'name': 'Alpha', 'type': 'string'},
                ],
            },
            {
                'id': 'right',
                'name': 'RightRows',
                'namespace': NS_EAST,
                'fields': [{'name': 'Alpha', 'type': 'integer'}],
            },
        ]
        flow = _manual_union_flow(
            branches,
            {NS_WEST: {'[Legacy Alpha]': 'Alpha'}, NS_EAST: {}},
        )
        self.assert_rejected(flow, 'duplicate/colliding field')

    def test_declared_union_schema_must_agree_with_computed_union(self):
        branches = [
            {
                'id': 'left',
                'name': 'LeftRows',
                'namespace': NS_WEST,
                'fields': [{'name': 'Amount', 'type': 'integer'}],
            },
            {
                'id': 'right',
                'name': 'RightRows',
                'namespace': NS_EAST,
                'fields': [{'name': 'Amount', 'type': 'real'}],
            },
        ]
        flow = _manual_union_flow(
            branches,
            {NS_WEST: {}, NS_EAST: {}},
            union_fields=[{'name': 'Amount', 'type': 'string'}],
        )
        self.assert_rejected(flow)

    def test_rejected_manual_union_writes_no_success_shaped_standalone_export(self):
        flow = self._two_branch_flow({
            NS_WEST: {'[Missing Column]': 'Alpha'},
            NS_EAST: {},
        })
        with tempfile.TemporaryDirectory() as directory:
            flow_path = write_flow(directory, flow)
            extraction = Path(directory, 'extract')
            captured = io.StringIO()
            with patch('migrate._get_extract_dir', return_value=str(extraction)), \
                    redirect_stdout(captured):
                result = run_standalone_prep(flow_path)

            self.assertFalse(result)
            self.assertFalse(Path(extraction, 'datasources.json').exists())
            self.assertEqual(list(extraction.glob('*.json')), [])
            self.assertNotIn('completed', captured.getvalue().lower())


class TestPrepPivotFixedDomainRejection(unittest.TestCase):
    """A declared schema or aggregation cannot invent a fixed pivot domain."""

    def _flow(self, aggregation_marker):
        input_node = {
            'baseType': 'input',
            'nodeType': '.v1.LoadCsv',
            'name': 'PivotInput',
            'connectionAttributes': {
                'class': 'csv',
                'filename': 'pivot_input.csv',
            },
            'fields': [
                {'name': 'ID', 'type': 'integer'},
                {'name': 'Quarter', 'type': 'string'},
                {'name': 'Value', 'type': 'real'},
            ],
            'nextNodes': [{'nextNodeId': 'pivot'}],
        }
        pivot = {
            'baseType': 'transform',
            'nodeType': '.v1.Pivot',
            'name': 'WidePivot',
            'pivotType': 'rowsToColumns',
            'pivotKeyField': {'name': 'Quarter'},
            'pivotValueField': {'name': 'Value'},
            'nextNodes': [{'nextNodeId': 'out'}],
            'fields': [
                {'name': 'ID', 'type': 'integer'},
                {'name': 'Q1', 'type': 'real'},
                {'name': 'Q2', 'type': 'real'},
            ],
        }
        if aggregation_marker is not ...:
            pivot['aggregation'] = aggregation_marker
        output = {
            'baseType': 'output',
            'nodeType': '.v1.PublishExtract',
            'name': 'PivotOutput',
            'fields': deepcopy(pivot['fields']),
            'nextNodes': [],
        }
        return {
            'nodes': {'input': input_node, 'pivot': pivot, 'out': output},
            'connections': {},
        }

    def test_rows_to_columns_stays_blocked_with_declared_fields(self):
        cases = [
            ('aggregation absent', ...),
            ('aggregation null', None),
            ('aggregation invalid', 'NOT_A_SUPPORTED_AGGREGATION'),
            ('aggregation valid but domain still absent', 'SUM'),
        ]
        for label, aggregation in cases:
            with self.subTest(case=label):
                flow = self._flow(aggregation)
                with self.assertRaisesRegex(
                    ValueError, 'data-dependent rowsToColumns schema'
                ):
                    _parse_flow(flow)


if __name__ == '__main__':
    unittest.main()
