"""Synthetic regressions for Prep superNode, field-map, and sparse-shape variants."""

import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tableau_export'))

from prep_flow_analyzer import analyze_flow
from prep_flow_parser import (
    _convert_action_to_m_step,
    get_prep_node_kind,
    normalize_prep_flow,
    parse_prep_flow,
    read_prep_flow,
)

from tests.prep_flow_fixtures import edge, output, source, write_flow


def _action_node(kind, name, node_id, **payload):
    """Build the inner action object with identity distinct from wrapper edges."""
    return {
        'nodeType': f'.v2018_2_3.{kind}',
        'name': name,
        'id': node_id,
        'baseType': 'transform',
        'nextNodes': [],
        **payload,
    }


def _super_node(kind, name, node_id, action_node, next_id=None):
    """Build the documented superNode envelope using only synthetic identifiers."""
    return {
        'nodeType': f'.v2018_2_3.{kind}',
        'name': name,
        'id': node_id,
        'baseType': 'superNode',
        'nextNodes': [edge(next_id)] if next_id else [],
        'serialize': True,
        'description': 'synthetic superNode wrapper',
        'beforeActionAnnotations': [],
        'afterActionAnnotations': [],
        'actionNode': action_node,
    }


def _synthetic_source(target, name='SyntheticSource', fields=None):
    node = source(target, name)
    if fields is not None:
        node['fields'] = fields
    return node


def _output_datasource(datasources):
    return next(ds for ds in datasources if ds.get('prep_role') == 'output')


def _parse_tmp(flow):
    with tempfile.TemporaryDirectory() as directory:
        path = write_flow(directory, flow)
        return parse_prep_flow(path)


def _clean_supernode_flow():
    name = 'SyntheticRename'
    node_id = 'synthetic-rename-id'
    rename = _action_node(
        'RenameColumn', name, node_id,
        columnName='Raw', newColumnName='Amount',
    )
    return {
        'nodes': {
            'input': _synthetic_source('clean'),
            'clean': _super_node('SuperTransform', name, node_id, rename, 'out'),
            'out': output(),
        },
        'connections': {},
    }


def test_supertransform_actionnode_rename_is_applied_and_wrapper_edges_survive():
    flow = _clean_supernode_flow()
    flow['nodes']['clean']['fields'] = None
    flow['nodes']['clean']['actionNode']['fields'] = {
        'Amount': {'type': 'integer'},
        'Discard': {'name': 'Discard', 'type': 'string'},
    }
    normalized = normalize_prep_flow(flow)
    clean = normalized['nodes']['clean']

    assert get_prep_node_kind({
        'baseType': 'superNode',
        'nodeType': '.v2018_2_3.SuperTransform',
        'actionNode': {},
    }) == 'transform'
    assert clean['baseType'] == 'transform'
    assert clean['nodeType'] == '.v2018_2_3.SuperTransform'
    assert clean['name'] == 'SyntheticRename'
    assert clean['id'] == 'synthetic-rename-id'
    assert clean['nextNodes'] == [edge('out')]
    assert 'actionNode' not in clean
    assert clean['actionGroup']['actions'][0]['actionType'] == 'RenameColumn'

    datasource = _output_datasource(_parse_tmp(flow))
    columns = [column['name'] for column in datasource['tables'][0]['columns']]
    assert columns == ['Amount', 'Discard']
    query = datasource['m_query_override']
    assert 'RenameColumns' in query
    assert '"Raw"' in query and '"Amount"' in query


def test_supertransform_orders_before_direct_action_and_after_group():
    name = 'OrderedRename'
    node_id = 'ordered-rename-id'
    rename = _action_node(
        'RenameColumn', name, node_id,
        columnName='Intermediate', newColumnName='Amount',
        beforeActionGroup={'actions': [{
            'actionType': '.v1.RenameColumn',
            'columnName': 'Raw',
            'newColumnName': 'Intermediate',
        }]},
        afterActionGroup={'actions': [{
            'actionType': '.v1.RemoveColumn',
            'columnName': 'Discard',
        }]},
    )
    flow = {
        'nodes': {
            'input': _synthetic_source('clean'),
            'clean': _super_node('SuperTransform', name, node_id, rename, 'out'),
            'out': output(),
        },
        'connections': {},
    }

    datasource = _output_datasource(_parse_tmp(flow))
    query = datasource['m_query_override']
    first_rename = query.index('"Raw"')
    second_rename = query.index('"Intermediate"')
    remove = query.index('RemoveColumns')
    assert first_rename < second_rename < remove
    assert [column['name'] for column in datasource['tables'][0]['columns']] == ['Amount']


def test_supernode_conflicting_inner_identity_or_edges_fail_explicitly():
    flow = _clean_supernode_flow()
    flow['nodes']['clean']['actionNode']['name'] = 'DifferentInnerName'
    with pytest.raises(ValueError, match='conflicting wrapper/actionNode name'):
        normalize_prep_flow(flow)

    flow = _clean_supernode_flow()
    flow['nodes']['clean']['actionNode']['nextNodes'] = [edge('input')]
    with pytest.raises(ValueError, match='actionNode.nextNodes conflict'):
        normalize_prep_flow(flow)

    flow = {
        'nodes': {
            'input': _synthetic_source('join', 'LeftSource'),
            'join': _super_node(
                'SuperJoin', 'JoinStep', 'join-id',
                _action_node(
                    'Aggregate', 'JoinStep', 'join-id',
                    joinType='left',
                    leftNodeId='input',
                    rightNodeId='input',
                    joinConditions=[{'leftColumn': 'Raw', 'rightColumn': 'Raw'}],
                ),
                'out',
            ),
            'out': output(),
        },
        'connections': {},
    }
    with pytest.raises(ValueError, match='conflicts with wrapper'):
        normalize_prep_flow(flow)

    with pytest.raises(ValueError, match='Unsupported Prep superNode nodeType'):
        get_prep_node_kind({
            'baseType': 'superNode',
            'nodeType': '.v99.FutureSuperNode',
            'actionNode': {},
        })


def test_unsupported_actionnode_fields_stay_conversion_blocking_after_renormalize():
    flow = _clean_supernode_flow()
    flow['nodes']['clean']['actionNode']['renameMode'] = 'fuzzy'
    normalized = normalize_prep_flow(flow)
    renormalized = normalize_prep_flow(normalized)
    diagnostics = renormalized['_prep_diagnostics']
    assert any(item['code'] == 'unsupported_action_node_field'
               for item in diagnostics)

    with tempfile.TemporaryDirectory() as directory:
        path = write_flow(directory, flow)
        with pytest.raises(ValueError, match='unsupported_action_node_field'):
            parse_prep_flow(path)


def test_superjoin_and_superaggregate_dispatch_inner_actionnode_semantics():
    join_node = _super_node(
        'SuperJoin', 'MatchedRows', 'join-id',
        _action_node(
            'Join', 'MatchedRows', 'join-id',
            joinType='left',
            leftNodeId='left',
            rightNodeId='right',
            joinConditions=[{'leftColumn': 'ID', 'rightColumn': 'ID'}],
        ),
        'out',
    )
    join_flow = {
        'nodes': {
            'left': _synthetic_source('join', 'LeftSource', [
                {'name': 'ID', 'type': 'integer'},
                {'name': 'LeftValue', 'type': 'string'},
            ]),
            'right': _synthetic_source('join', 'RightSource', [
                {'name': 'ID', 'type': 'integer'},
                {'name': 'RightValue', 'type': 'string'},
            ]),
            'join': join_node,
            'out': output('JoinedRows'),
        },
        'connections': {},
    }
    joined = _output_datasource(_parse_tmp(join_flow))
    assert 'JoinKind.LeftOuter' in joined['m_query_override']
    assert [column['name'] for column in joined['tables'][0]['columns']] == [
        'ID', 'LeftValue', 'RightValue',
    ]

    aggregate = _super_node(
        'SuperAggregate', 'Totals', 'aggregate-id',
        _action_node(
            'Aggregate', 'Totals', 'aggregate-id',
            groupByFields=[{'name': 'Category'}],
            aggregateFields=[{
                'name': 'Amount',
                'newColumnName': 'Total',
                'aggregation': 'SUM',
            }],
        ),
        'out',
    )
    aggregate_flow = {
        'nodes': {
            'input': _synthetic_source('aggregate', fields=[
                {'name': 'Category', 'type': 'string'},
                {'name': 'Amount', 'type': 'real'},
            ]),
            'aggregate': aggregate,
            'out': output('TotalsOutput'),
        },
        'connections': {},
    }
    totals = _output_datasource(_parse_tmp(aggregate_flow))
    assert 'Table.Group' in totals['m_query_override']
    assert [column['name'] for column in totals['tables'][0]['columns']] == [
        'Category', 'Total',
    ]


def test_superunion_and_superunpivot_extract_actionnode_payloads():
    union = _super_node(
        'SuperUnion', 'UnionedRows', 'union-id',
        _action_node('Union', 'UnionedRows', 'union-id', unionType='automatic'),
        'out',
    )
    union_flow = {
        'nodes': {
            'left': _synthetic_source('union', 'LeftRows'),
            'right': _synthetic_source('union', 'RightRows'),
            'union': union,
            'out': output('UnionOutput'),
        },
        'connections': {},
    }
    union_output = _output_datasource(_parse_tmp(union_flow))
    assert 'Table.Combine' in union_output['m_query_override']
    assert [column['name'] for column in union_output['tables'][0]['columns']] == [
        'Raw', 'Discard',
    ]

    unpivot = _super_node(
        'SuperUnpivotExtended', 'LongRows', 'unpivot-id',
        _action_node(
            'UnpivotExtended', 'LongRows', 'unpivot-id',
            pivotFields=[{'name': 'Q1'}, {'name': 'Q2'}],
            pivotNamesName='Quarter',
            pivotValuesName='Value',
        ),
        'out',
    )
    unpivot_flow = {
        'nodes': {
            'input': _synthetic_source('unpivot', fields=[
                {'name': 'ID', 'type': 'integer'},
                {'name': 'Q1', 'type': 'real'},
                {'name': 'Q2', 'type': 'real'},
            ]),
            'unpivot': unpivot,
            'out': output('LongOutput'),
        },
        'connections': {},
    }
    long_output = _output_datasource(_parse_tmp(unpivot_flow))
    assert 'Unpivot' in long_output['m_query_override']
    assert [column['name'] for column in long_output['tables'][0]['columns']] == [
        'ID', 'Quarter', 'Value',
    ]


def test_superpivot_payload_is_not_silently_discarded_when_conversion_is_unsupported():
    pivot = _super_node(
        'SuperPivot', 'WideRows', 'pivot-id',
        _action_node(
            'Pivot', 'WideRows', 'pivot-id',
            pivotType='rowsToColumns',
            pivotKeyField={'name': 'Quarter'},
            pivotValueField={'name': 'Value'},
            aggregation='SUM',
        ),
        'out',
    )
    flow = {
        'nodes': {
            'input': _synthetic_source('pivot', fields=[
                {'name': 'ID', 'type': 'integer'},
                {'name': 'Quarter', 'type': 'string'},
                {'name': 'Value', 'type': 'real'},
            ]),
            'pivot': pivot,
            'out': output(),
        },
        'connections': {},
    }
    normalized = normalize_prep_flow(flow)
    assert normalized['nodes']['pivot']['pivotKeyField'] == {'name': 'Quarter'}
    assert normalized['nodes']['pivot']['pivotValueField'] == {'name': 'Value'}
    with tempfile.TemporaryDirectory() as directory:
        path = write_flow(directory, flow)
        with pytest.raises(ValueError, match='data-dependent rowsToColumns schema'):
            parse_prep_flow(path)


@pytest.mark.parametrize('fields', [
    {
        'Raw': {'type': 'integer'},
        'Label': {'name': 'Label', 'type': 'string'},
    },
    [
        {'name': 'Raw', 'type': 'integer'},
        {'name': 'Label', 'type': 'string'},
    ],
])
def test_field_list_and_column_keyed_map_normalize_to_same_schema(fields):
    flow = {
        'nodes': {
            'input': _synthetic_source('out', fields=fields),
            'out': output(),
        },
        'connections': {},
    }
    normalized = normalize_prep_flow(flow)
    normalized_fields = normalized['nodes']['input']['fields']
    assert [field['name'] for field in normalized_fields] == ['Raw', 'Label']
    assert [column['name'] for column in _output_datasource(_parse_tmp(flow))['tables'][0]['columns']] == [
        'Raw', 'Label',
    ]


def test_field_map_name_conflicts_and_case_collisions_are_rejected():
    flow = {
        'nodes': {
            'input': _synthetic_source('out', fields={
                'Raw': {'name': 'Other', 'type': 'integer'},
            }),
            'out': output(),
        },
        'connections': {},
    }
    with pytest.raises(ValueError, match='conflicts with its field-map key'):
        normalize_prep_flow(flow)

    flow['nodes']['input']['fields'] = {
        'Name': {'type': 'string'},
        'name': {'type': 'string'},
    }
    with pytest.raises(ValueError, match='field-map keys.*collide'):
        normalize_prep_flow(flow)


def _filter_flow(filter_type_marker='keep', include_filter_type=True):
    action = {
        'actionType': '.v1.FilterOperation',
        'filterExpression': '[Raw] > 0',
    }
    if include_filter_type:
        action['filterType'] = filter_type_marker
    return {
        'nodes': {
            'input': _synthetic_source('clean'),
            'clean': {
                'baseType': 'transform',
                'nodeType': '.v2018_3_3.SuperTransform',
                'name': 'FilterStep',
                'beforeActionGroup': {'actions': [action]},
                'nextNodes': [edge('out')],
            },
            'out': output(),
        },
        'connections': {},
    }


@pytest.mark.parametrize('bad_filter_type', [1, {'mode': 'remove'}, None, 'exclude'])
def test_unsupported_filter_type_is_inventory_diagnostic_and_blocks_conversion(
        bad_filter_type):
    with tempfile.TemporaryDirectory() as directory:
        path = write_flow(directory, _filter_flow(bad_filter_type))
        flow = read_prep_flow(path)
        diagnostics = flow['_prep_diagnostics']
        assert diagnostics[0]['code'] == 'unsupported_filter_type'
        assert diagnostics[0]['blocks_conversion'] is True
        assert diagnostics[0]['node_id'].endswith('clean')

        profile = analyze_flow(path)
        assert profile.diagnostics == diagnostics
        assert profile.assessment['grade'] == 'RED'
        assert profile.assessment['unsupported_diagnostics'] == diagnostics
        with pytest.raises(ValueError, match='unsupported_filter_type'):
            parse_prep_flow(path)

        profile_with_conversion = analyze_flow(path, include_m_queries=True)
        assert profile_with_conversion.assessment['status'] == 'error'
        assert profile_with_conversion.m_queries == {}
        assert profile_with_conversion.diagnostics == diagnostics


def test_missing_filter_direction_is_not_defaulted_to_keep():
    with tempfile.TemporaryDirectory() as directory:
        path = write_flow(directory, _filter_flow(include_filter_type=False))
        flow = read_prep_flow(path)
        assert flow['_prep_diagnostics'][0]['code'] == 'missing_filter_type'
        with pytest.raises(ValueError, match='missing_filter_type'):
            parse_prep_flow(path)

    with pytest.raises(ValueError, match='requires explicit filterType'):
        _convert_action_to_m_step(
            'FilterOperation', {'filterExpression': '[Raw] > 0'}, {},
        )


def test_known_filter_directions_are_canonicalized_and_converted():
    flow = _filter_flow(' Remove ')
    normalized = normalize_prep_flow(flow)
    action = normalized['nodes']['clean']['beforeActionGroup']['actions'][0]
    assert action['filterType'] == 'remove'
    assert not normalized.get('_prep_diagnostics')
    query = _output_datasource(_parse_tmp(flow))['m_query_override']
    assert 'Table.SelectRows' in query
    assert 'not (' in query


def _empty_container(next_id, **attrs):
    return {
        'baseType': 'container',
        'nodeType': '.v1.Container',
        'name': 'EmptyIdentity',
        'id': 'empty-container-id',
        'nextNodes': [edge(next_id)] if next_id else [],
        'loomContainer': {'nodes': {}},
        **attrs,
    }


def test_empty_semantics_free_container_collapses_only_with_unique_routing():
    flow = {
        'nodes': {
            'input': _synthetic_source('empty'),
            'empty': _empty_container('out'),
            'out': output(),
        },
        'connections': {},
    }
    normalized = normalize_prep_flow(flow)
    assert 'empty' not in normalized['nodes']
    assert normalized['nodes']['input']['nextNodes'] == [edge('out')]
    assert _output_datasource(_parse_tmp(flow))['tables'][0]['columns'][0]['name'] == 'Raw'

    missing_nodes = {
        'nodes': {
            'input': _synthetic_source('empty'),
            'empty': _empty_container('out'),
            'out': output(),
        },
        'connections': {},
    }
    del missing_nodes['nodes']['empty']['loomContainer']['nodes']
    normalized_missing = normalize_prep_flow(missing_nodes)
    assert 'empty' not in normalized_missing['nodes']
    assert normalized_missing['nodes']['input']['nextNodes'] == [edge('out')]

    ambiguous = {
        'nodes': {
            'input': _synthetic_source('empty'),
            'empty': _empty_container(None),
            'out_a': output('OutputA'),
            'out_b': output('OutputB'),
        },
        'connections': {},
    }
    ambiguous['nodes']['empty']['nextNodes'] = [edge('out_a'), edge('out_b')]
    with pytest.raises(ValueError, match='ambiguous entry routing'):
        normalize_prep_flow(ambiguous)


def test_empty_container_with_schema_or_edge_semantics_is_not_identity():
    flow = {
        'nodes': {
            'input': _synthetic_source('empty'),
            'empty': _empty_container('out', fields=[
                {'name': 'Invented', 'type': 'string'},
            ]),
            'out': output(),
        },
        'connections': {},
    }
    with pytest.raises(ValueError, match='declares a schema'):
        normalize_prep_flow(flow)

    flow = {
        'nodes': {
            'input': _synthetic_source('empty'),
            'empty': _empty_container('out'),
            'out': output(),
        },
        'connections': {},
    }
    flow['nodes']['input']['nextNodes'] = [{
        'nextNodeId': 'empty',
        'routingMode': 'special',
    }]
    with pytest.raises(ValueError, match='edge metadata cannot be preserved'):
        normalize_prep_flow(flow)


def test_empty_container_cycles_remain_rejected():
    flow = {
        'nodes': {
            'a': _empty_container('b'),
            'b': _empty_container('a'),
        },
        'connections': {},
    }
    with pytest.raises(ValueError, match='cyclic container boundary'):
        normalize_prep_flow(flow)
