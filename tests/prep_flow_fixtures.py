"""Synthetic, public-only Prep fixtures for reports #81–83.

All paths, fields and node IDs are invented; no Tableau/private exports needed.
"""

import json
import re
import zipfile
from pathlib import Path


def split_generated_prep_metadata(query, flow_name):
    """Remove only our bounded Prep block, retaining and validating its origin."""
    pattern = (r'\n    // Tableau Prep provenance \(metadata only\)\n'
               r'(?:    // [^\r\n]*\n)*?'
               r'    // End Tableau Prep provenance\n')
    matches = list(re.finditer(pattern, query))
    if len(matches) != 1:
        raise AssertionError(f'Expected exactly one Prep metadata block: {query}')
    block = matches[0].group()
    flow_line = '    // Flow: ' + flow_name + '\n'
    if block.count(flow_line) != 1:
        raise AssertionError(f'Expected flow {flow_name!r} in {block!r}')
    normalized_block = block.replace(flow_line, '    // Flow: <flow>\n', 1)
    return query[:matches[0].start()] + query[matches[0].end():], normalized_block


def edge(target):
    return {'nextNodeId': target}


def operation(kind, target=None, **attrs):
    return {
        'nodeType': '.v2024_1.' + kind,
        'name': kind,
        'nextNodes': [edge(target)] if target else [],
        **attrs,
    }


def source(target=None, name='SyntheticSales'):
    return operation(
        'LoadCsv', target, name=name,
        connectionAttributes={'class': 'csv', 'filename': 'synthetic_sales.csv'},
        fields=[
            {'name': 'Raw', 'type': 'integer'},
            {'name': 'Discard', 'type': 'string'},
        ],
    )


def output(name='PublishedSales', base_type=False):
    node = operation('PublishExtract', name=name)
    if base_type:
        node['baseType'] = 'output'
    return node


def container(nodes, target=None, **attrs):
    return {
        'baseType': 'container',
        'name': 'SyntheticContainer',
        'loomContainer': {'nodes': nodes},
        'nextNodes': [edge(target)] if target else [],
        **attrs,
    }


def clean_chain(nested=False):
    """CSV -> container(rename/remove/add/type/filter) -> PublishExtract."""
    # Deliberately not in execution order.
    children = {
        'filter': operation('FilterOperation', filterExpression='[Twice] > 10',
                            filterType='keep'),
        'add': operation('AddColumn', 'type', columnName='Twice',
                         expression='[Amount] * 2'),
        'rename': operation('RenameColumn', 'remove', columnName='Raw',
                            newColumnName='Amount'),
        'type': operation('ChangeColumnType', 'filter', columnName='Twice',
                          newType='integer'),
        'remove': operation('RemoveColumn', 'add', columnName='Discard'),
    }
    if nested:
        children = {'inner': container(children)}
    return {
        'nodes': {
            'out': output(),
            'box': container(children, 'out'),
            'input': source('box'),
        },
        'connections': {},
    }


def join_flow(explicit=True):
    """Two container exits feed explicitly sided, noncommutative left join."""
    join = operation(
        'Join', 'out', name='MatchedSales', joinType='left',
        joinConditions=[{'leftColumn': 'Raw', 'rightColumn': 'Raw'}],
    )
    if explicit:
        join.update(leftNodeId='left_box', rightNodeId='right_box')
    return {
        'nodes': {
            # Right branch first to catch iteration-order side inference.
            'right_box': container({
                'same/id': operation('RemoveColumn', columnName='Discard',
                                     name='RightClean'),
            }, 'join'),
            'right_input': source('right_box', 'RightSource'),
            'join': join,
            'left_box': container({
                'same/id': operation('AddColumn', columnName='LeftMarker',
                                     expression='1', name='LeftClean'),
            }, 'join'),
            'left_input': source('left_box', 'LeftSource'),
            'out': output('JoinedSales'),
        },
        'connections': {},
    }


def write_flow(directory, flow, filename='synthetic.tfl', member=None):
    path = Path(directory) / filename
    if member is None:
        path.write_text(json.dumps(flow), encoding='utf-8')
    else:
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr(member, json.dumps(flow))
    return str(path)
