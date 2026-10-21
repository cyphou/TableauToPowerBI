"""
Tableau Prep Flow Parser (.tfl / .tflx)

Parses Tableau Prep flow files and converts them to Power Query M expressions
using the transform generators from m_query_builder.py.

Tableau Prep flows are JSON files (not XML like .twb) with:
- nodes: dict of step nodes (input, transform, output) forming a DAG
- connections: dict of data source connection definitions

Supported step types:
- Input: LoadCsv, LoadExcel, LoadSql, LoadJson, LoadHyper, LoadGoogle
- Clean (SuperTransform): RenameColumn, RemoveColumn, DuplicateColumn,
  ChangeColumnType, FilterOperation, FilterValues, FilterRange,
  ReplaceValues, SplitColumn, MergeColumns, AddColumn, CleanOperation,
  FillValues, ReplaceNulls, ConditionalColumn, GroupReplace, Remap (explicit text map)
- Aggregate: groupByFields + aggregateFields
- Join: inner, left, right, full, leftOnly, rightOnly
- Union: automatic or manual field mapping
- Pivot: columnsToRows (unpivot), rowsToColumns (pivot)
- Output: PublishExtract, SaveToFile, SaveToDatabase
"""

import logging
import os
import json

logger = logging.getLogger(__name__)
import zipfile
import re
from copy import deepcopy
from collections import OrderedDict
from urllib.parse import quote

# Import M query generators
import sys
sys.path.insert(0, os.path.dirname(__file__))
from m_query_builder import (
    _M_GENERATORS,
    _m_literal,
    generate_power_query_m,
    generate_m_from_hyper,
    inject_m_steps,
    m_transform_rename,
    m_transform_remove_columns,
    m_transform_select_columns,
    m_transform_duplicate_column,
    m_transform_split_by_delimiter,
    m_transform_merge_columns,
    m_transform_replace_value,
    m_transform_replace_nulls,
    m_transform_trim,
    m_transform_clean,
    m_transform_upper,
    m_transform_lower,
    m_transform_proper_case,
    m_transform_fill_down,
    m_transform_fill_up,
    m_transform_filter_values,
    m_transform_exclude_values,
    m_transform_filter_range,
    m_transform_filter_nulls,
    m_transform_filter_contains,
    m_transform_distinct,
    m_transform_top_n,
    m_transform_aggregate,
    m_transform_unpivot,
    m_transform_unpivot_other,
    m_transform_pivot,
    m_transform_join,
    m_transform_union,
    m_transform_sort,
    m_transform_add_column,
    m_transform_conditional_column,
    m_transform_add_index,
    m_transform_promote_headers,
)


# ═══════════════════════════════════════════════════════════════════════════════
#  TABLEAU PREP TYPE MAPPINGS
# ═══════════════════════════════════════════════════════════════════════════════

# Prep connection class → extraction/lineage connection type (legacy contract).
_PREP_CONNECTION_MAP = {
    'csv': 'textscan',
    'excel': 'excel-direct',
    'excel-direct': 'excel-direct',
    'sqlserver': 'sqlserver',
    'postgres': 'postgres',
    'mysql': 'mysql',
    'oracle': 'oracle',
    'bigquery': 'bigquery',
    'snowflake': 'snowflake',
    'redshift': 'redshift',
    'teradata': 'teradata',
    'saphana': 'saphana',
    'databricks': 'databricks',
    'spark': 'spark',
    'json': 'json',
    'hyper': 'hyper',
    'google-sheets': 'google-sheets',
    'salesforce': 'salesforce',
    'azure_sql_dw': 'azure_sql_dw',
    'odata': 'OData',
    'google-analytics': 'Google Analytics',
    'azure-blob': 'Azure Blob',
    'adls': 'ADLS',
    'wasbs': 'Azure Blob Storage',
}

# Builder dispatch uses display names, unlike the extraction/lineage contract.
_PREP_M_CONNECTION_MAP = {
    'csv': 'CSV',
    'textscan': 'CSV',
    'excel': 'Excel',
    'excel-direct': 'Excel',
    'sqlserver': 'SQL Server',
    'postgres': 'PostgreSQL',
    'mysql': 'MySQL',
    'oracle': 'Oracle',
    'bigquery': 'BigQuery',
    'snowflake': 'Snowflake',
    'redshift': 'Amazon Redshift',
    'teradata': 'Teradata',
    'saphana': 'SAP HANA',
    'databricks': 'Databricks',
    'spark': 'Spark SQL',
    'json': 'JSON',
    'hyper': 'hyper',
    'google-sheets': 'Google Sheets',
    'salesforce': 'Salesforce',
    'azure_sql_dw': 'Azure Synapse',
    'odata': 'OData',
    'google-analytics': 'Google Analytics',
    'azure-blob': 'Azure Blob',
    'adls': 'ADLS',
    'wasbs': 'Azure Blob Storage',
}


def _prep_m_connection_type(conn_type):
    """Resolve a real builder connector; never accept its empty-table fallback."""
    conn_class = (conn_type or '').strip().lower()
    supported_types = {name.lower(): name for name in _M_GENERATORS}
    resolved = _PREP_M_CONNECTION_MAP.get(conn_class, supported_types.get(conn_class))
    if resolved not in _M_GENERATORS:
        raise ValueError(f'unsupported connector {conn_type!r}')
    return resolved


# Prep data type → Power Query M type
_PREP_TYPE_MAP = {
    'string': 'text',
    'integer': 'number',
    'real': 'number',
    'date': 'date',
    'datetime': 'datetime',
    'boolean': 'logical',
}

# Prep aggregation → m_transform_aggregate aggregation key
_PREP_AGG_MAP = {
    'SUM': 'sum',
    'AVG': 'avg',
    'MEDIAN': 'median',
    'COUNT': 'count',
    'COUNTD': 'countd',
    'MIN': 'min',
    'MAX': 'max',
    'STDEV': 'stdev',
    'STDEVP': 'stdev',
    'VAR': 'var',
    'VARP': 'varp',
}

# Prep join type → m_transform_join join kind
_PREP_JOIN_MAP = {
    'inner': 'inner',
    'left': 'left',
    'right': 'right',
    'full': 'full',
    'leftOnly': 'leftanti',
    'rightOnly': 'rightanti',
    'notInner': 'leftanti',  # exclusive left (Tableau "not inner")
}


# ═══════════════════════════════════════════════════════════════════════════════
#  FLOW FILE READER
# ═══════════════════════════════════════════════════════════════════════════════

def _read_tflx_zip(filepath):
    """Extract and parse the flow JSON from inside a .tflx ZIP archive.

    Tableau Prep saves flow data in ZIP archives. The flow JSON can be
    stored as a file named ``*.tfl`` or simply ``flow`` (no extension).
    """
    with zipfile.ZipFile(filepath, 'r') as z:
        names = z.namelist()
        # Prefer a file ending in .tfl
        for name in names:
            if name.endswith('.tfl'):
                with z.open(name) as f:
                    return json.loads(f.read().decode('utf-8'))
        # Fallback: look for a file named 'flow' (Prep 2020.3+ format)
        if 'flow' in names:
            with z.open('flow') as f:
                return json.loads(f.read().decode('utf-8'))
    raise ValueError(f"No .tfl or 'flow' entry found inside {filepath}")


def read_prep_flow(filepath):
    """
    Read a Tableau Prep flow file (.tfl or .tflx) and return the parsed JSON.

    Auto-detects ZIP archives even when the file has a .tfl extension
    (some Prep exports save .tflx content with a .tfl extension).

    Args:
        filepath: Path to .tfl or .tflx file

    Returns:
        dict: Parsed flow JSON with 'nodes' and 'connections'
    """
    ext = os.path.splitext(filepath)[1].lower()

    if ext == '.tflx':
        return normalize_prep_flow(_read_tflx_zip(filepath))

    if ext == '.tfl':
        # Auto-detect ZIP archives saved with .tfl extension
        if zipfile.is_zipfile(filepath):
            return normalize_prep_flow(_read_tflx_zip(filepath))
        with open(filepath, 'r', encoding='utf-8') as f:
            return normalize_prep_flow(json.load(f))

    raise ValueError(f"Unsupported file extension: {ext} (expected .tfl or .tflx)")


# ═══════════════════════════════════════════════════════════════════════════════
#  FLOW GRAPH TRAVERSAL
# ═══════════════════════════════════════════════════════════════════════════════

def _get_node_type(node):
    """Extract the semantic node type from the versioned nodeType string.

    e.g. '.v2018_3_3.SuperTransform' → 'SuperTransform'
         '.v1.LoadCsv' → 'LoadCsv'
    """
    node = _as_dict(node)
    node_type = node.get('nodeType') or ''
    if not isinstance(node_type, str):
        raise ValueError("Prep nodeType must be a string")
    parts = node_type.rsplit('.', 1)
    return parts[-1] if parts else node_type


def get_prep_node_kind(node):
    """Return input/output/container/transform; explicit baseType takes priority.

    Only known input/output semantic suffixes are inferred, never arbitrary
    ``Load*`` or ``Publish*`` names. Unknown types remain transforms so conversion
    can report unsupported semantics rather than silently discard them.
    """
    node = _as_dict(node)
    base = node.get('baseType')
    if base in ('input', 'output', 'container', 'transform'):
        return base
    if base:
        raise ValueError(f"Unknown Prep baseType {base!r}")
    sem = _get_node_type(node)
    if sem in {
        'LoadCsv', 'LoadExcel', 'LoadSql', 'LoadJson', 'LoadHyper',
        'LoadGoogle', 'LoadGoogleSheets', 'LoadPublishedDataSource',
        'PublishedDataSource', 'Input',
    }:
        return 'input'
    if sem in {'PublishExtract', 'SaveToFile', 'SaveToDatabase', 'Output'}:
        return 'output'
    return 'container' if node.get('loomContainer') is not None else 'transform'


def normalize_prep_flow(flow):
    """Copy, validate and flatten a Prep DAG, without inferring execution order.

    Top-level IDs are unchanged. Children receive escaped, collision-free scoped
    IDs; edge, join-side and connection references are rewritten. Connections
    remain datasource definitions, not graph edges. Missing/null optional
    collections become empty collections; invalid shapes raise ValueError.

    Container boundaries may be declared on the container or loomContainer via
    entry/exitNodeId(s), input/outputNodeId(s), entry/exitNodes, input/outputNodes,
    or initial/terminalNodeId(s). A reference is a string or {nodeId: string}.
    Edges from a container to its descendant (or back) also declare boundaries.
    Otherwise exactly one internal root/sink is required when routing through
    that boundary. Multi-entry joins must have edges to the actual child inputs;
    they are never wired by predecessor order or by broadcasting.
    """
    flow = deepcopy(_as_dict(flow))
    try:
        raw_nodes = _as_dict(flow.get('nodes'))
        connections = _as_dict(flow.get('connections'))
    except ValueError as exc:
        raise ValueError(f'Prep flow nodes/connections: {exc}') from exc
    if any(not isinstance(cid, str) or not cid for cid in connections):
        raise ValueError('Prep flow: connection IDs must be nonempty strings')
    flow['connections'] = connections
    prefix = '__prep_scope__/'
    while any(str(nid).startswith(prefix) for nid in raw_nodes):
        prefix = '_' + prefix
    records, scopes, parents, children = {}, {}, {}, {}
    scope_connections = {}
    list_keys = {
        'nextNodes', 'fields', 'actions', 'groupByFields', 'aggregateFields',
        'joinConditions', 'pivotFields', 'columns', 'values', 'groupings', 'rules',
    }
    dict_keys = {
        'connectionAttributes', 'beforeActionGroup', 'actionGroup',
        'afterActionGroup', 'pivotKeyField', 'pivotValueField',
    }
    object_lists = {
        'nextNodes', 'fields', 'actions', 'groupByFields', 'aggregateFields',
        'joinConditions', 'pivotFields', 'groupings', 'rules',
    }
    string_keys = {
        'name', 'nodeType', 'actionType', 'baseType', 'connectionId', 'class',
        'columnName', 'newColumnName', 'expression', 'filterExpression',
        'filterType', 'newType', 'operation', 'direction', 'aggregation',
        'joinType', 'pivotType', 'leftNodeId', 'rightNodeId',
        'delimiter', 'separator', 'pattern', 'pivotValuesName', 'pivotNamesName',
        'scriptLanguage', 'language', 'publishedDatasourceName', 'datasourceName',
    }

    def optional_shapes(obj, context):
        kind = obj.get('actionType') or obj.get('nodeType') or ''
        is_remap = isinstance(kind, str) and kind.rsplit('.', 1)[-1] == 'Remap'
        if is_remap:
            _parse_remap(obj)
        for key, value in list(obj.items()):
            here = f'{context}.{key}'
            if key == 'values' and is_remap:
                # Remap uses an encoded target -> encoded sources object,
                # unlike the value lists used by other clean operations.
                continue
            elif key in list_keys:
                try:
                    value = _as_list(value)
                except ValueError as exc:
                    raise ValueError(f'{here}: {exc}') from exc
                obj[key] = value
                for item in value:
                    if key in object_lists and not isinstance(item, dict):
                        raise ValueError(f'{here}: expected object elements')
                    if isinstance(item, dict):
                        optional_shapes(item, here)
            elif key in dict_keys:
                try:
                    obj[key] = _as_dict(value)
                except ValueError as exc:
                    raise ValueError(f'{here}: {exc}') from exc
                if key == 'connectionAttributes':
                    for attr, attr_value in list(obj[key].items()):
                        if attr_value is None:
                            obj[key].pop(attr)
                        elif not isinstance(attr_value, (str, int, float, bool)):
                            raise ValueError(f'{here}.{attr}: expected scalar connection attribute')
                optional_shapes(obj[key], here)
            elif key in string_keys:
                if value is None:
                    obj.pop(key)
                elif not isinstance(value, str):
                    raise ValueError(f'{here}: expected string')

    def add_scope(node_map, scope, path, parent, local_connections):
        node_map = _as_dict(node_map)
        ids = {}
        for local_id in node_map:
            if not isinstance(local_id, str) or not local_id:
                raise ValueError(f'Prep scope {path}: node IDs must be nonempty strings')
            nid = local_id if not path else prefix + '/'.join(
                quote(part, safe='') for part in (*path, local_id))
            if nid in records:
                raise ValueError(f'Prep duplicate scoped node ID {nid!r}')
            ids[local_id] = nid
            if not isinstance(node_map[local_id], dict):
                raise ValueError(f'Prep node {nid!r}: expected object')
            records[nid] = node_map[local_id]
            parents[nid] = parent
        scopes[scope] = ids
        scope_connections[scope] = local_connections
        children[parent] = list(ids.values())
        for local_id, nid in ids.items():
            node = records[nid]
            node.setdefault('_prep_declared_fields',
                            'fields' in node and node['fields'] is not None)
            optional_shapes(node, f'Prep node {nid!r}')
            node.setdefault('nextNodes', [])
            node.setdefault('fields', [])
            node.setdefault('connectionAttributes', {})
            node['baseType'] = get_prep_node_kind(node)
            node['_prep_scope'] = scope
            if node['baseType'] == 'container':
                if (node.get('actions') or any(
                    _as_dict(node.get(key)).get('actions') for key in
                    ('beforeActionGroup', 'actionGroup', 'afterActionGroup')
                )):
                    raise ValueError(f'Prep container {nid!r}: outer actions cannot be '
                                     'ordered reliably against child graph')
                if any(node.get(key) for key in (
                    'groupByFields', 'aggregateFields', 'joinConditions', 'pivotType',
                )):
                    raise ValueError(f'Prep container {nid!r}: outer transformation '
                                     'semantics cannot be discarded while flattening')
                loom = _as_dict(node.get('loomContainer'))
                if not loom.get('nodes'):
                    raise ValueError(f'Prep container {nid!r}: missing/empty loomContainer.nodes')
                local = dict(local_connections)
                for cid, definition in _as_dict(loom.get('connections')).items():
                    if not isinstance(cid, str) or not cid:
                        raise ValueError(f'Prep container {nid!r}: invalid connection ID {cid!r}')
                    scoped_cid = prefix + 'connection/' + quote(nid, safe='') + '/' + quote(cid, safe='')
                    if scoped_cid in connections:
                        raise ValueError(f'Prep duplicate connection {scoped_cid!r}')
                    connections[scoped_cid] = definition
                    local[cid] = scoped_cid
                add_scope(loom['nodes'], nid, (*path, local_id), nid, local)

    add_scope(raw_nodes, None, (), None, {cid: cid for cid in connections})
    containers = {nid for nid, node in records.items() if node['baseType'] == 'container'}

    def is_inside(nid, container):
        parent = parents[nid]
        while parent is not None:
            if parent == container:
                return True
            parent = parents[parent]
        return False

    def resolve(ref, scope, context):
        if isinstance(ref, dict):
            ref = ref.get('nodeId', ref.get('nextNodeId', ref.get('id')))
        if not isinstance(ref, str) or not ref:
            raise ValueError(f'{context}: expected nonempty node reference, got {ref!r}')
        cursor = scope
        while True:
            if ref in scopes[cursor]:
                return scopes[cursor][ref]
            if cursor is None:
                break
            cursor = parents[cursor]
        if ref in records:
            return ref
        # Outer edges may name a child directly, but never choose between
        # identically named children in different containers.
        matches = [ids[ref] for key, ids in scopes.items() if ref in ids
                   and (scope is None or key == scope or
                        (key is not None and is_inside(key, scope)))]
        if len(matches) != 1:
            raise ValueError(f'{context}: dangling/ambiguous reference {ref!r} ({matches})')
        return matches[0]

    edges = []
    declared = {nid: {'entry': [], 'exit': []} for nid in containers}
    for nid, node in records.items():
        scope = node['_prep_scope']
        for edge in node['nextNodes']:
            target = resolve(edge.get('nextNodeId'), scope, f'Prep edge from {nid!r}')
            if nid in containers and is_inside(target, nid):
                declared[nid]['entry'].append(target)
            elif target in containers and is_inside(nid, target):
                declared[target]['exit'].append(nid)
            else:
                edges.append((nid, target, edge))
        if node.get('nextNodeId') is not None:
            target = resolve(node['nextNodeId'], scope, f'Prep edge from {nid!r}')
            if nid in containers and is_inside(target, nid):
                declared[nid]['entry'].append(target)
            elif target in containers and is_inside(nid, target):
                declared[target]['exit'].append(nid)
            else:
                edges.append((nid, target, {'nextNodeId': target}))
            node.pop('nextNodeId')
        cid = node.get('connectionId')
        if cid is not None:
            if not isinstance(cid, str):
                raise ValueError(f'Prep node {nid!r}: connectionId must be a string')
            node['connectionId'] = scope_connections[scope].get(cid, cid)
        for direction, stems in (
            ('entry', ('entry', 'input', 'initial')),
            ('exit', ('exit', 'output', 'terminal')),
        ):
            if nid not in containers:
                continue
            for owner in (node, _as_dict(node.get('loomContainer'))):
                for stem in stems:
                    for key in (stem + 'NodeId', stem + 'NodeIds', stem + 'Node', stem + 'Nodes'):
                        if owner.get(key) is None:
                            continue
                        refs = owner[key] if isinstance(owner[key], list) else [owner[key]]
                        for ref in refs:
                            target = resolve(ref, nid, f'Prep container {nid!r}.{key}')
                            if not is_inside(target, nid):
                                raise ValueError(f'Prep container {nid!r}.{key}: not a child: {target!r}')
                            declared[nid][direction].append(target)

    boundary_cache = {}

    def boundary(nid, direction, active=()):
        if nid not in containers:
            return nid
        key = (nid, direction)
        if key in boundary_cache:
            return boundary_cache[key]
        if key in active:
            raise ValueError(f'Prep cyclic container boundary {active + (key,)!r}')
        candidates = set(declared[nid][direction])
        if not candidates:
            candidates = set(children[nid])
            # Project real internal edges to direct children. No dictionary
            # ordering is used to choose an entry, an exit, or join sides.
            for source, target, _ in edges:
                if not (is_inside(source, nid) and is_inside(target, nid)):
                    continue
                a, b = source, target
                while parents[a] != nid:
                    a = parents[a]
                while parents[b] != nid:
                    b = parents[b]
                if a != b:
                    candidates.discard(b if direction == 'entry' else a)
        if len(candidates) != 1:
            raise ValueError(f'Prep container {nid!r}: ambiguous {direction} boundary '
                             f'{sorted(candidates)!r}; explicit child routing required')
        result = boundary(next(iter(candidates)), direction, active + (key,))
        boundary_cache[key] = result
        return result

    flat = {nid: node for nid, node in records.items() if nid not in containers}
    for node in flat.values():
        node['nextNodes'] = []
    for source, target, edge in edges:
        source = boundary(source, 'exit')
        target = boundary(target, 'entry')
        rewritten = {**edge, 'nextNodeId': target}
        if rewritten not in flat[source]['nextNodes']:
            flat[source]['nextNodes'].append(rewritten)
    for nid, node in flat.items():
        for key in ('leftNodeId', 'rightNodeId'):
            if node.get(key) is not None:
                node[key] = boundary(resolve(node[key], node['_prep_scope'],
                                             f'Prep node {nid!r}.{key}'), 'exit')
        node.pop('_prep_scope', None)
        kind = get_prep_node_kind(node)
        if kind == 'input' and _find_upstream_nodes(flat, nid):
            raise ValueError(f'Prep input {nid!r}: incoming edges would be discarded')
        if kind == 'output' and node['nextNodes']:
            raise ValueError(f'Prep output {nid!r}: unexpected outgoing edges')
    # An explicitly selected exit must not strand another internal branch.
    # Retaining that branch in JSON while omitting it from generated output
    # would still be silent data loss. Internal inputs/outputs are real graph
    # terminals; other child roots/sinks must have actual routed edges, except
    # for terminal leaves eligible for the legacy no-explicit-output fallback.
    has_explicit_outputs = any(get_prep_node_kind(node) == 'output'
                               for node in flat.values())
    routed_containers = {source for source, _, _ in edges if source in containers}
    for nid, node in flat.items():
        if parents[nid] is None:
            continue
        upstream = _find_upstream_nodes(flat, nid)
        if not upstream and get_prep_node_kind(node) != 'input':
            raise ValueError(f'Prep container child {nid!r}: unrouted entry/root')
        if not node['nextNodes'] and get_prep_node_kind(node) != 'output':
            parent = parents[nid]
            routed = False
            while parent is not None:
                routed = routed or parent in routed_containers
                parent = parents[parent]
            if routed or has_explicit_outputs:
                raise ValueError(f'Prep container child {nid!r}: branch stranded at exit')
    for cid, definition in connections.items():
        definition = _as_dict(definition)
        optional_shapes(definition, f'Prep connection {cid!r}')
        connections[cid] = definition
    flow['nodes'] = flat
    _topological_sort(flat)
    return flow


def _topological_sort(nodes):
    """
    Topological sort of flow nodes (DAG traversal).

    Returns:
        list of node IDs in execution order (inputs first, outputs last)
    """
    nodes = _as_dict(nodes)
    # Build adjacency: node_id → [next_node_ids]
    graph = {}
    in_degree = {}

    for nid, node in nodes.items():
        node = _as_dict(node)
        if nid not in graph:
            graph[nid] = []
        if nid not in in_degree:
            in_degree[nid] = 0

        for edge in _as_list(node.get('nextNodes', [])):
            edge = _as_dict(edge)
            next_id = edge.get('nextNodeId', '')
            if next_id:
                if next_id not in nodes:
                    raise ValueError(f'Prep node {nid!r}: dangling edge to {next_id!r}')
                graph[nid].append(next_id)
                if next_id not in graph:
                    graph[next_id] = []
                in_degree[next_id] = in_degree.get(next_id, 0) + 1

    # Kahn's algorithm
    queue = [nid for nid, deg in in_degree.items() if deg == 0]
    sorted_ids = []

    while queue:
        nid = queue.pop(0)
        sorted_ids.append(nid)
        for next_id in graph.get(nid, []):
            in_degree[next_id] -= 1
            if in_degree[next_id] == 0:
                queue.append(next_id)

    if len(sorted_ids) != len(nodes):
        blocked = [nid for nid in nodes if in_degree[nid] > 0]
        raise ValueError(f'Prep graph contains a cycle; blocked nodes: {blocked!r}')
    return sorted_ids


def _find_upstream_nodes(nodes, node_id):
    """Find all node IDs that have nextNodes pointing to node_id."""
    nodes = _as_dict(nodes)
    upstream = []
    for nid, node in nodes.items():
        node = _as_dict(node)
        for edge in _as_list(node.get('nextNodes', [])):
            edge = _as_dict(edge)
            if edge.get('nextNodeId') == node_id:
                upstream.append(nid)
    return upstream


def _as_dict(value):
    """Normalize an optional object; reject malformed non-null shapes."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f'Prep expected object, got {type(value).__name__}')
    return value


def _as_list(value):
    """Normalize an optional collection; reject malformed non-null shapes."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f'Prep expected list, got {type(value).__name__}')
    return value


# ═══════════════════════════════════════════════════════════════════════════════
#  INPUT NODE → CONNECTION + TABLE
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_input_node(node, connections):
    """
    Parse an input node into a connection dict and table dict
    compatible with generate_power_query_m().

    Returns:
        (connection_dict, table_dict)
    """
    conn_id = node.get('connectionId', '')
    conn_def = _as_dict(connections.get(conn_id))
    conn_attrs = _as_dict(conn_def.get('connectionAttributes'))
    node_conn_attrs = _as_dict(node.get('connectionAttributes'))

    # Merge connection-level and node-level attributes
    all_attrs = {**conn_attrs, **node_conn_attrs}

    # Map Prep connection class to m_query_builder type
    conn_class = (all_attrs.get('class') or '').strip().lower()
    conn_type = _PREP_CONNECTION_MAP.get(conn_class, conn_class)
    try:
        _prep_m_connection_type(conn_type)
    except ValueError as exc:
        raise ValueError(f"Prep input {node.get('name', '<unnamed>')!r}: {exc}") from exc

    # Build table name
    table_name = node.get('name', 'Table')
    db_table = all_attrs.get('table', table_name)

    # Build columns from fields
    columns = []
    for field in _as_list(node.get('fields')):
        field = _as_dict(field)
        columns.append({
            'name': field.get('name', ''),
            'datatype': field.get('type', 'string'),
        })

    # Build connection dict for m_query_builder
    connection = {
        'type': conn_type,
        'details': {
            'server': all_attrs.get('server', ''),
            'port': all_attrs.get('port', ''),
            'database': all_attrs.get('dbname', all_attrs.get('database', '')),
            'schema': all_attrs.get('schema', ''),
            'filename': all_attrs.get('filename', ''),
            'directory': all_attrs.get('directory', ''),
            'warehouse': all_attrs.get('warehouse', ''),
            'project': all_attrs.get('project', ''),
            'dataset': all_attrs.get('dataset', ''),
        }
    }

    table = {
        'name': db_table,
        'columns': columns,
    }

    return connection, table


# ═══════════════════════════════════════════════════════════════════════════════
#  CLEAN STEP (SuperTransform) → M STEPS
# ═══════════════════════════════════════════════════════════════════════════════

# Pre-compiled patterns for Prep expression → M conversion
_RE_IF = re.compile(r'\bIF\b', re.IGNORECASE)
_RE_THEN = re.compile(r'\bTHEN\b', re.IGNORECASE)
_RE_ELSE = re.compile(r'\bELSE\b', re.IGNORECASE)
_RE_ELSEIF = re.compile(r'\bELSEIF\b', re.IGNORECASE)
_RE_END = re.compile(r'(?<!\[)\bEND\b(?!\s*\w*\])', re.IGNORECASE)
_RE_AND = re.compile(r'\bAND\b', re.IGNORECASE)
_RE_OR = re.compile(r'\bOR\b', re.IGNORECASE)
_RE_NOT = re.compile(r'\bNOT\b', re.IGNORECASE)
_RE_ISNULL = re.compile(r'\bISNULL\s*\(', re.IGNORECASE)
_RE_CONTAINS = re.compile(r'\bCONTAINS\s*\(', re.IGNORECASE)
_RE_LEN = re.compile(r'\bLEN\s*\(', re.IGNORECASE)
_RE_UPPER = re.compile(r'\bUPPER\s*\(', re.IGNORECASE)
_RE_LOWER = re.compile(r'\bLOWER\s*\(', re.IGNORECASE)
_RE_TRIM = re.compile(r'\bTRIM\s*\(', re.IGNORECASE)
_RE_LEFT = re.compile(r'\bLEFT\s*\(', re.IGNORECASE)
_RE_RIGHT = re.compile(r'\bRIGHT\s*\(', re.IGNORECASE)


def _convert_prep_expression_to_m(expression):
    """
    Convert a Tableau Prep calculation expression to a Power Query M expression.

    Simple conversions for common patterns. Complex formulas may need manual review.
    """
    if not expression:
        return '""'

    expr = expression.strip()

    # Basic column references: [Column] → [Column] (same in M for row context)
    # Tableau Prep IF/THEN/ELSE → M if/then/else (pre-compiled patterns)
    expr = _RE_ELSEIF.sub('else if', expr)  # ELSEIF before ELSE/IF
    expr = _RE_IF.sub('if', expr)
    expr = _RE_THEN.sub('then', expr)
    expr = _RE_ELSE.sub('else', expr)
    expr = _RE_END.sub('', expr)
    expr = _RE_AND.sub('and', expr)
    expr = _RE_OR.sub('or', expr)
    expr = _RE_NOT.sub('not', expr)

    # Comparison operators
    expr = expr.replace('!=', '<>')
    expr = expr.replace('==', '=')

    # NULL handling
    expr = _RE_ISNULL.sub('(null = ', expr)

    # String functions
    expr = _RE_CONTAINS.sub('Text.Contains(', expr)
    expr = _RE_LEN.sub('Text.Length(', expr)
    expr = _RE_UPPER.sub('Text.Upper(', expr)
    expr = _RE_LOWER.sub('Text.Lower(', expr)
    expr = _RE_TRIM.sub('Text.Trim(', expr)
    expr = _RE_LEFT.sub('Text.Start(', expr)
    expr = _RE_RIGHT.sub('Text.End(', expr)

    return expr


def _parse_remap(action, fields=None):
    """Decode only materialized JSON-string maps; never rerun grouping methods."""
    column = action.get('columnName')
    if not isinstance(column, str) or not column:
        raise ValueError('Prep Remap requires a nonempty columnName')
    values = action.get('values')
    if not isinstance(values, dict) or not values:
        raise ValueError('Prep Remap values must be a nonempty object')

    def decode(encoded):
        if not isinstance(encoded, str):
            raise ValueError('Prep Remap values must be JSON-encoded strings')
        try:
            value = json.loads(encoded)
        except ValueError as exc:
            raise ValueError(f'Prep Remap invalid JSON-encoded string {encoded!r}') from exc
        if not isinstance(value, str):
            raise ValueError('Prep Remap supports only decoded string values')
        return value

    mapping = {}
    for encoded_target, encoded_sources in values.items():
        target = decode(encoded_target)
        if not isinstance(encoded_sources, list) or not encoded_sources:
            raise ValueError('Prep Remap sources must be nonempty lists')
        for encoded_source in encoded_sources:
            source = decode(encoded_source)
            if source in mapping and mapping[source] != target:
                raise ValueError(f'Prep Remap conflicting mappings for source {source!r}')
            mapping[source] = target

    if fields is not None:
        field = _require_field(fields, column, 'Remap')
        if field.get('type') != 'string':
            raise ValueError(f'Prep Remap column {column!r} requires upstream string type')
    # groupMethodProps describes how the producer obtained this explicit map;
    # it is provenance, not an instruction to execute fuzzy matching.
    return column, mapping


def _parse_clean_actions(node):
    """
    Parse SuperTransform actions into M transformation steps.

    Consecutive RenameColumn actions are batched into a single
    Table.RenameColumns step for cleaner M output.

    Returns:
        list of (step_name, step_expression) tuples
    """
    steps = []
    actions = []
    for key in ('beforeActionGroup', 'actionGroup', 'afterActionGroup'):
        action_group = _as_dict(node.get(key))
        actions.extend(_as_list(action_group.get('actions')))
    if node.get('actions'):
        raise ValueError('Prep SuperTransform has unscoped actions; action-group order required')

    # Track step counter for unique step names
    step_counter = {}

    # Batch consecutive renames
    pending_renames = {}

    def flush_renames():
        """Emit a batched rename step if any are pending."""
        if pending_renames:
            steps.append(m_transform_rename(dict(pending_renames)))
            pending_renames.clear()

    for action in actions:
        action = _as_dict(action)
        action_type = action.get('actionType') or action.get('nodeType') or ''
        if not isinstance(action_type, str):
            raise ValueError('Prep clean actionType must be a string')
        # Extract semantic type: '.v1.RenameColumn' → 'RenameColumn'
        sem_type = action_type.rsplit('.', 1)[-1] if '.' in action_type else action_type

        if sem_type == 'RenameColumn':
            old_name = action.get('columnName', '')
            new_name = action.get('newColumnName', '')
            if old_name and new_name:
                # A->B followed by B->C is sequential, not a simultaneous map.
                if old_name in pending_renames or old_name in pending_renames.values():
                    flush_renames()
                pending_renames[old_name] = new_name
            else:
                raise ValueError(f'Prep RenameColumn requires columnName/newColumnName: {action!r}')
            continue

        # Flush any pending renames before processing a non-rename action
        flush_renames()

        step = _convert_action_to_m_step(sem_type, action, step_counter)
        if step:
            if isinstance(step, list):
                steps.extend(step)
            else:
                steps.append(step)
        else:
            raise ValueError(f'Prep unsupported or incomplete clean action {sem_type!r}: {action!r}')

    # Flush any trailing renames
    flush_renames()

    return steps


def _convert_action_to_m_step(action_type, action, counter):
    """
    Convert a single Prep action to an M transformation step.

    Returns:
        (step_name, step_expression) tuple, list of tuples, or None
    """

    if action_type == 'RenameColumn':
        old_name = action.get('columnName', '')
        new_name = action.get('newColumnName', '')
        if old_name and new_name:
            return m_transform_rename({old_name: new_name})

    elif action_type == 'RemoveColumns':
        columns = _as_list(action.get('columnNames', action.get('columns')))
        if columns and all(isinstance(col, str) and col for col in columns):
            return m_transform_remove_columns(columns)

    elif action_type == 'RemoveColumn':
        col = action.get('columnName', '')
        if col:
            return m_transform_remove_columns([col])

    elif action_type == 'DuplicateColumn':
        col = action.get('columnName', '')
        new_col = action.get('newColumnName', f'{col}_copy')
        if col:
            return m_transform_duplicate_column(col, new_col)

    elif action_type == 'ChangeColumnType':
        col = action.get('columnName', '')
        new_type = action.get('newType', 'string')
        if not col or new_type not in _PREP_TYPE_MAP:
            raise ValueError('Prep ChangeColumnType requires a column and supported newType')
        m_type = _PREP_TYPE_MAP.get(new_type, 'text')
        n = counter.get('type', 0)
        counter['type'] = n + 1
        step_name = f'#"Changed Type {n}"' if n > 0 else '#"Changed Type"'
        col = col.replace('"', '""')
        return (step_name,
                f'Table.TransformColumnTypes({{prev}}, {{{{"{col}", type {m_type}}}}})')

    elif action_type == 'FilterOperation':
        expr = action.get('filterExpression', '')
        if not expr:
            raise ValueError('Prep FilterOperation requires filterExpression')
        filter_type = action.get('filterType', 'keep')
        m_expr = _convert_prep_expression_to_m(expr)
        n = counter.get('filter', 0)
        counter['filter'] = n + 1
        step_name = f'#"Filtered Rows {n}"' if n > 0 else '#"Filtered Rows"'
        if filter_type == 'remove':
            return (step_name,
                    f'Table.SelectRows({{prev}}, each not ({m_expr}))')
        return (step_name,
                f'Table.SelectRows({{prev}}, each {m_expr})')

    elif action_type == 'FilterValues':
        col = action.get('columnName', '')
        values = action.get('values', [])
        filter_type = action.get('filterType', 'keep')
        if col and values:
            if filter_type == 'remove':
                return m_transform_exclude_values(col, values)
            return m_transform_filter_values(col, values)

    elif action_type == 'FilterRange':
        col = action.get('columnName', '')
        min_val = action.get('min')
        max_val = action.get('max')
        if col:
            return m_transform_filter_range(col, min_val, max_val)

    elif action_type == 'ReplaceValues':
        col = action.get('columnName', '')
        old_val = action.get('oldValue', '')
        new_val = action.get('newValue', '')
        if col:
            return m_transform_replace_value(col, old_val, new_val)

    elif action_type == 'Remap':
        column, mapping = _parse_remap(action)

        def literal(value):
            # The shared helper quotes text. Also protect M escape sequences,
            # control characters and inject_m_steps' literal {prev} sentinel.
            return _m_literal(value.replace('#', '#(#)')
                              .replace('\r', '#(cr)').replace('\n', '#(lf)')
                              .replace('\t', '#(tab)')
                              .replace('{prev}', '#(007B)prev}'))

        sources = '{' + ', '.join(literal(source) for source in mapping) + '}'
        targets = '{' + ', '.join(literal(target) for target in mapping.values()) + '}'
        n = counter.get('remap', 0)
        counter['remap'] = n + 1
        step_name = f'#"Remapped Values {n}"' if n > 0 else '#"Remapped Values"'
        return (step_name,
                f'Table.TransformColumns({{prev}}, {{{{{literal(column)}, '
                f'each if _ = null then null else let position = '
                f'List.PositionOf({sources}, _, Occurrence.First, Comparer.Ordinal) '
                f'in if position = -1 then _ else {targets}{{position}}, type text}}}})')

    elif action_type == 'ReplaceNulls':
        col = action.get('columnName', '')
        replacement = action.get('replacement', '')
        if col:
            return m_transform_replace_nulls(col, replacement)

    elif action_type == 'SplitColumn':
        col = action.get('columnName', '')
        delimiter = action.get('delimiter', ',')
        if col:
            parts = action.get('numParts')
            if parts is not None and (
                    not isinstance(parts, int) or isinstance(parts, bool) or parts < 1):
                raise ValueError('Prep SplitColumn requires explicit positive numParts '
                                 'to determine its output schema')
            return m_transform_split_by_delimiter(col, delimiter, parts)

    elif action_type == 'MergeColumns':
        columns = action.get('columns', [])
        separator = action.get('separator', ' ')
        new_col = action.get('newColumnName', 'Merged')
        if columns:
            return m_transform_merge_columns(columns, new_col, separator)

    elif action_type == 'AddColumn':
        col_name = action.get('columnName', 'NewColumn')
        expression = action.get('expression', '')
        if not expression:
            raise ValueError('Prep AddColumn requires expression')
        m_expr = _convert_prep_expression_to_m(expression)
        # M Table.AddColumn requires 'each' prefix for row-context expressions
        if not m_expr.strip().startswith('each'):
            m_expr = f'each {m_expr}'
        dtype = action.get('newType', action.get('type'))
        if dtype is not None and dtype not in _PREP_TYPE_MAP:
            raise ValueError(f'Prep AddColumn unsupported type {dtype!r}')
        return m_transform_add_column(
            col_name, m_expr, f'type {_PREP_TYPE_MAP[dtype]}' if dtype else None)

    elif action_type == 'CleanOperation':
        col = action.get('columnName', '')
        operation = action.get('operation', '').lower()
        if col:
            if operation == 'trim':
                return m_transform_trim([col])
            elif operation == 'upper':
                return m_transform_upper([col])
            elif operation == 'lower':
                return m_transform_lower([col])
            elif operation == 'proper':
                return m_transform_proper_case([col])
            elif operation in ('removeletters', 'removenumbers', 'removepunctuation'):
                return m_transform_clean([col])

    elif action_type == 'FillValues':
        col = action.get('columnName', '')
        direction = action.get('direction', 'down').lower()
        if col:
            if direction == 'up':
                return m_transform_fill_up([col])
            return m_transform_fill_down([col])

    elif action_type == 'GroupReplace':
        col = action.get('columnName', '')
        groupings = action.get('groupings', [])
        if col and groupings:
            # Convert groupings to replace steps
            result_steps = []
            for g in groupings:
                old_val = g.get('from', '')
                new_val = g.get('to', '')
                if old_val and new_val:
                    result_steps.append(m_transform_replace_value(col, old_val, new_val))
            return result_steps if result_steps else None

    elif action_type == 'ConditionalColumn':
        new_col = action.get('newColumnName', 'Conditional')
        rules = action.get('rules', [])
        default = action.get('defaultValue', 'null')
        if rules:
            conditions = []
            for rule in rules:
                condition = _convert_prep_expression_to_m(rule.get('condition', ''))
                value = rule.get('value', '""')
                if isinstance(value, str) and not value.startswith('"'):
                    value = f'"{value}"'
                conditions.append((condition, str(value)))
            return m_transform_conditional_column(new_col, conditions, str(default))

    elif action_type == 'ExtractValues':
        # Regex or pattern extraction → Table.TransformColumns with Text.Select / regex
        col = action.get('columnName', '')
        pattern = action.get('pattern', '')
        new_col = action.get('newColumnName', f'{col}_extracted')
        if col:
            m_pattern = pattern.replace('\\', '\\\\') if pattern else '.*'
            n = counter.get('extract', 0)
            counter['extract'] = n + 1
            step_name = f'#"Extracted {n}"' if n > 0 else '#"Extracted Values"'
            # Use Table.AddColumn with a regex-like Text approach
            return (step_name,
                    f'Table.AddColumn({{prev}}, "{new_col}", '
                    f'each Text.Select([{col}], {{"a".."z", "A".."Z", "0".."9"}}), type text)')

    elif action_type == 'CustomCalculation':
        # Full custom calculation expression in Prep
        col_name = action.get('columnName', action.get('newColumnName', 'Calc'))
        expression = action.get('expression', '')
        m_expr = _convert_prep_expression_to_m(expression)
        if not m_expr.strip().startswith('each'):
            m_expr = f'each {m_expr}'
        return m_transform_add_column(col_name, m_expr)

    return None


def _schema(fields, context):
    """Validate known fields without synthesizing missing source columns."""
    if not isinstance(fields, list):
        raise ValueError(f'Prep {context}: fields must be a list')
    result = deepcopy(fields)
    seen = set()
    for field in result:
        if not isinstance(field, dict) or not isinstance(field.get('name'), str) or not field['name']:
            raise ValueError(f'Prep {context}: each field requires a nonempty name')
        key = field['name'].casefold()
        if key in seen:
            raise ValueError(f'Prep {context}: duplicate/colliding field {field["name"]!r}')
        seen.add(key)
        dtype = field.get('type', 'string')
        if dtype not in _PREP_TYPE_MAP and dtype != 'unknown':
            raise ValueError(f'Prep {context}: unsupported field type {dtype!r}')
        field['type'] = dtype
    return result


def _require_field(fields, name, context):
    for field in fields:
        if field['name'] == name:
            return field
    raise ValueError(f'Prep {context}: column {name!r} is not in the upstream schema')


def _expression_type(expression, fields):
    """Conservative inference for literals, references and numeric arithmetic."""
    expr = (expression or '').strip()
    if re.fullmatch(r'(?:"[^"]*"|\'[^\']*\')', expr):
        return 'string'
    # Brackets inside literals are not field references.
    unquoted = re.sub(r'"(?:[^"]|"")*"|\'(?:[^\']|\'\')*\'', '""', expr)
    refs = re.findall(r'\[([^\]]+)\]', unquoted)
    types = [_require_field(fields, ref, 'calculation')['type'] for ref in refs]
    if re.fullmatch(r'\[[^\]]+\]', expr):
        return types[0]
    if expr.lower() in ('true', 'false'):
        return 'boolean'
    arithmetic = re.sub(r'\[[^\]]+\]', '1', expr)
    if re.fullmatch(r'[\d\s.+*/()%-]+', arithmetic) and all(
            t in ('integer', 'real') for t in types):
        return 'real' if '/' in expr or '.' in arithmetic or 'real' in types else 'integer'
    return 'unknown'


def _clean_schema(node, fields):
    fields = _schema(fields, 'clean input')
    actions = []
    for key in ('beforeActionGroup', 'actionGroup', 'afterActionGroup'):
        actions.extend(_as_list(_as_dict(node.get(key)).get('actions')))
    for action in actions:
        kind = (action.get('actionType') or action.get('nodeType') or '').rsplit('.', 1)[-1]
        name = action.get('columnName', '')
        if kind in ('RemoveColumn', 'RemoveColumns'):
            removed = ([name] if kind == 'RemoveColumn' else
                       _as_list(action.get('columnNames', action.get('columns'))))
            for col in removed:
                _require_field(fields, col, kind)
            fields = [f for f in fields if f['name'] not in removed]
        elif kind == 'RenameColumn':
            _require_field(fields, name, kind)['name'] = action['newColumnName']
        elif kind == 'ChangeColumnType':
            _require_field(fields, name, kind)['type'] = action['newType']
        elif kind == 'DuplicateColumn':
            field = deepcopy(_require_field(fields, name, kind))
            field['name'] = action.get('newColumnName', f'{name}_copy')
            fields.append(field)
        elif kind in ('AddColumn', 'CustomCalculation'):
            new_name = action.get('columnName', action.get(
                'newColumnName', 'NewColumn' if kind == 'AddColumn' else 'Calc'))
            dtype = action.get('newType', action.get('type')) or _expression_type(
                action.get('expression'), fields)
            fields.append({'name': new_name, 'type': dtype})
        elif kind == 'ConditionalColumn':
            for rule in action.get('rules', []):
                _expression_type(rule.get('condition'), fields)
            # The existing generator quotes string rule values; do not infer a
            # numeric type from a numeric-looking string.
            values = [rule.get('value', '') for rule in action.get('rules', [])]
            default = action.get('defaultValue', 'null')
            types = {('string' if isinstance(v, str) else
                      'boolean' if isinstance(v, bool) else 'real')
                     for v in values if v is not None}
            if default != 'null':
                types.add(_expression_type(str(default), fields))
            fields.append({'name': action.get('newColumnName', 'Conditional'),
                           'type': next(iter(types)) if len(types) == 1 else 'unknown'})
        elif kind == 'ExtractValues':
            _require_field(fields, name, kind)
            fields.append({'name': action.get('newColumnName', f'{name}_extracted'),
                           'type': 'string'})
        elif kind == 'SplitColumn':
            _require_field(fields, name, kind)
            if not isinstance(action.get('numParts'), int) or action['numParts'] < 1:
                raise ValueError('Prep SplitColumn requires explicit positive numParts '
                                 'to determine its output schema')
            pos = next(i for i, f in enumerate(fields) if f['name'] == name)
            fields[pos:pos + 1] = [
                {'name': f'{name}.{i + 1}', 'type': 'string'}
                for i in range(action['numParts'])]
        elif kind == 'MergeColumns':
            columns = action.get('columns', [])
            for col in columns:
                _require_field(fields, col, kind)
            pos = min(i for i, f in enumerate(fields) if f['name'] in columns)
            fields = [f for f in fields if f['name'] not in columns]
            fields.insert(pos, {'name': action.get('newColumnName', 'Merged'), 'type': 'string'})
        elif kind == 'FilterOperation':
            _expression_type(action.get('filterExpression'), fields)
        elif kind == 'Remap':
            _parse_remap(action, fields)
        elif name:
            _require_field(fields, name, kind)
        fields = _schema(fields, kind)
    return fields


def _declared_schema(node, result, context, project=False):
    """Honor explicit schemas as a checked contract, never as invented columns.

    Output fields may select/reorder an existing schema. Transform fields must
    describe exactly the computed names. Known incompatible types are errors;
    a declared type can resolve an otherwise unknown calculation's type.
    """
    fields = _schema(result.get('fields', []), context)
    if node.get('_prep_declared_fields', 'fields' in node):
        declared = _schema(node['fields'], context + ' declared')
        if not project and {f['name'] for f in fields} != {f['name'] for f in declared}:
            raise ValueError(f'Prep {context}: declared fields disagree with computed schema')
        for raw, field in zip(node['fields'], declared):
            actual = _require_field(fields, field['name'], context)
            if 'type' not in raw:
                field['type'] = actual['type']
            if field['type'] == 'unknown':
                raise ValueError(f'Prep {context}: declared fields require concrete types')
            if actual['type'] != 'unknown' and actual['type'] != field['type']:
                raise ValueError(f'Prep {context}: declared type for {field["name"]!r} '
                                 f'({field["type"]}) disagrees with computed {actual["type"]}')
        if project or [f['name'] for f in declared] != [f['name'] for f in fields]:
            result['m_query'] = inject_m_steps(result['m_query'], [
                m_transform_select_columns([f['name'] for f in declared])])
        # Resolve unknown calculation types explicitly in M as well as JSON.
        conversions = [(f['name'], f['type']) for f in declared
                       if _require_field(fields, f['name'], context)['type'] == 'unknown']
        if conversions:
            pairs = ', '.join(
                '{"' + name.replace('"', '""') + '", type ' + _PREP_TYPE_MAP[dtype] + '}'
                for name, dtype in conversions)
            result['m_query'] = inject_m_steps(result['m_query'], [
                ('#"Declared Types"', f'Table.TransformColumnTypes({{prev}}, {{{pairs}}})')])
        fields = declared
    result['fields'] = fields
    result['table'] = {**result.get('table', {}), 'columns': [
        {'name': f['name'], 'datatype': f['type']} for f in fields]}
    return result


def _aggregate_schema(node, fields):
    fields = _schema(fields, 'aggregate input')
    result = []
    for group in _as_list(node.get('groupByFields')):
        name = group.get('name')
        if not name:
            continue
        result.append(_require_field(fields, name, 'aggregate group'))
    for agg in _as_list(node.get('aggregateFields')):
        source_name = agg.get('name')
        if not source_name:
            continue
        source = _require_field(fields, source_name, 'aggregate')
        operation = agg.get('aggregation', 'SUM').upper()
        if operation not in _PREP_AGG_MAP:
            raise ValueError(f'Prep unsupported aggregation {operation!r}')
        if operation not in ('COUNT', 'COUNTD') and source['type'] not in ('integer', 'real'):
            raise ValueError(f'Prep aggregate: {operation} requires numeric input '
                             f'for column {source_name!r}')
        result.append({'name': agg.get('newColumnName', agg.get('name')),
                       'type': 'integer' if operation in ('COUNT', 'COUNTD') else 'real'})
    return _schema(result, 'aggregate output')


def _union_schema(results, node):
    if (node.get('fieldMappings') or node.get('columnMappings') or node.get('fieldMapping')
            or node.get('unionType', '').lower() == 'manual'):
        raise ValueError('Prep union manual field mapping requires an explicit supported dialect')
    fields = OrderedDict()
    for result in results:
        for field in _schema(result.get('fields', []), 'union input'):
            name, dtype = field['name'], field['type']
            if name in fields and fields[name]['type'] != dtype:
                previous = fields[name]['type']
                if {previous, dtype} <= {'integer', 'real'}:
                    dtype = 'real'
                else:
                    raise ValueError(f'Prep union incompatible types for {name!r}: '
                                     f'{previous!r}, {dtype!r}')
            fields[name] = {'name': name, 'type': dtype}
    return _schema(list(fields.values()), 'union output')


# ═══════════════════════════════════════════════════════════════════════════════
#  AGGREGATE STEP → M STEP
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_aggregate_node(node):
    """Convert an Aggregate node to an m_transform_aggregate step."""
    node = _as_dict(node)
    group_fields = [f.get('name', '') for f in _as_list(node.get('groupByFields', []))
                    if isinstance(f, dict)]
    agg_fields = []
    for af in _as_list(node.get('aggregateFields', [])):
        if not isinstance(af, dict):
            continue
        agg = af.get('aggregation', 'SUM').upper()
        agg_fields.append({
            'name': af.get('newColumnName', af.get('name', '')),
            'column': af.get('name', ''),
            'agg': _PREP_AGG_MAP.get(agg, 'sum'),
        })

    if group_fields or agg_fields:
        return m_transform_aggregate(group_fields, agg_fields)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  JOIN STEP → M STEPS
# ═══════════════════════════════════════════════════════════════════════════════

def _clean_m_table_ref(name):
    """Clean a node name into a valid Power Query M table reference.

    Strips file extension, replaces spaces, and wraps in #"..." if needed.
    """
    # Strip common file extensions
    for ext in ('.csv', '.xlsx', '.xls', '.json', '.hyper', '.tde'):
        if name.lower().endswith(ext):
            name = name[:-len(ext)]
            break
    # Replace spaces with underscores for the table name
    clean = name.replace(' ', '_')
    return clean


def _parse_join_node(node, right_table_name, right_fields):
    """
    Convert a Join node to m_transform_join steps.

    Returns:
        list of (step_name, step_expression) tuples (join + expand)
    """
    node = _as_dict(node)
    join_type = _PREP_JOIN_MAP.get(node.get('joinType', 'inner'), 'inner')
    conditions = _as_list(node.get('joinConditions', []))

    left_keys = [c.get('leftColumn', '') for c in conditions if isinstance(c, dict)]
    right_keys = [c.get('rightColumn', '') for c in conditions if isinstance(c, dict)]

    # Fields to expand from right table
    expand_fields = [f.get('name', '') for f in _as_list(right_fields)
                     if isinstance(f, dict) and f.get('name', '') not in right_keys]

    if left_keys and right_keys:
        # Use cleaned M table reference
        m_ref = _clean_m_table_ref(right_table_name)
        return m_transform_join(
            m_ref, left_keys, right_keys,
            join_type, expand_fields
        )
    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  UNION STEP → M STEP
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_union_node(node, upstream_table_names):
    """Convert a Union node to an m_transform_union step."""
    if upstream_table_names:
        return m_transform_union(upstream_table_names)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  PIVOT STEP → M STEP
# ═══════════════════════════════════════════════════════════════════════════════

def _parse_pivot_node(node):
    """Convert a Pivot node to an M step (unpivot or pivot)."""
    node = _as_dict(node)
    pivot_type = node.get('pivotType', '')

    if pivot_type == 'columnsToRows':
        # Unpivot
        pivot_fields = [f.get('name', '') for f in _as_list(node.get('pivotFields', []))
                        if isinstance(f, dict)]
        values_name = node.get('pivotValuesName', 'Value')
        names_name = node.get('pivotNamesName', 'Attribute')
        if pivot_fields:
            return m_transform_unpivot(pivot_fields, names_name, values_name)

    elif pivot_type == 'rowsToColumns':
        # Pivot
        key_field = _as_dict(node.get('pivotKeyField', {})).get('name', '')
        value_field = _as_dict(node.get('pivotValueField', {})).get('name', '')
        agg = node.get('aggregation', 'SUM').lower()
        if key_field and value_field:
            return m_transform_pivot(key_field, value_field, agg)

    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN FLOW PARSER — CONVERTS ENTIRE FLOW TO DATASOURCES
# ═══════════════════════════════════════════════════════════════════════════════

def parse_prep_flow(filepath):
    """
    Parse a Tableau Prep flow file and convert to datasource definitions
    compatible with the Power BI generation pipeline.

    Each output node in the flow produces one datasource/table with a
    complete M query that chains all upstream transformations.

    Args:
        filepath: Path to .tfl or .tflx file

    Returns:
        list of datasource dicts, each with:
        - name: datasource name
        - tables: [{name, columns, m_query}]
        - connections: [connection_dict]
        - columns_metadata: []
        - calculations: []
    """
    flow = normalize_prep_flow(read_prep_flow(filepath))
    nodes = _as_dict(flow.get('nodes', {}))
    connections = _as_dict(flow.get('connections', {}))

    if not nodes:
        raise ValueError(f'Prep flow {filepath!s}: no nodes; no outputs can be produced')

    # Topological sort to process in order
    sorted_ids = _topological_sort(nodes)

    # Track per-node results: node_id → {connection, table, m_query, fields}
    node_results = {}
    # Track secondary branch nodes that need to be emitted as separate queries
    # (e.g. right side of joins, extra union inputs)
    secondary_branch_ids = set()

    for nid in sorted_ids:
        _process_prep_node(nid, nodes, connections, node_results, secondary_branch_ids)

    # Collect output nodes into datasources
    return _collect_prep_datasources(sorted_ids, nodes, node_results, secondary_branch_ids)


def _process_prep_node(nid, nodes, connections, node_results, secondary_branch_ids):
    """Process a single Prep flow node (input/transform/output) and store result in node_results."""
    nodes = _as_dict(nodes)
    connections = _as_dict(connections)
    node = _as_dict(nodes.get(nid, {}))
    base_type = get_prep_node_kind(node)
    sem_type = _get_node_type(node)
    node_name = node.get('name', nid[:8])

    if base_type == 'input':
        _process_input_node(nid, node, connections, node_results, node_name)

    elif base_type == 'transform':
        upstream_ids = _find_upstream_nodes(nodes, nid)
        _process_transform_node(nid, node, nodes, upstream_ids, sem_type,
                                node_results, secondary_branch_ids, node_name)
        _declared_schema(node, node_results[nid], f'transform {nid!r}')

    elif base_type == 'output':
        upstream_ids = _find_upstream_nodes(nodes, nid)
        if len(upstream_ids) != 1 or upstream_ids[0] not in node_results:
            raise ValueError(f'Prep output {nid!r} ({node_name}): expected one resolved '
                             f'upstream, got {upstream_ids!r}')
        node_results[nid] = {
            **node_results[upstream_ids[0]],
            'name': node_name,
            'is_output': True,
            'prep_output_type': sem_type,
        }
        _declared_schema(node, node_results[nid], f'output {nid!r}', project=True)
        print(f"    ✓ Output: {node_name}")
    else:
        raise ValueError(f'Prep node {nid!r}: unnormalized/unsupported kind {base_type!r}')


def _process_input_node(nid, node, connections, node_results, node_name):
    """Process an input node: parse connection and generate base M query.

    For Hyper connections, attempts to read actual schema/data via
    ``hyper_reader`` to produce a richer M expression.
    """
    node = _as_dict(node)
    connections = _as_dict(connections)
    if _get_node_type(node) in ('PublishedDataSource', 'LoadPublishedDataSource'):
        _process_transform_node(nid, node, {}, [], _get_node_type(node),
                                node_results, set(), node_name)
        return
    connection, table = _parse_input_node(node, connections)
    conn_type = connection.get('type', '')

    m_query = None
    # For hyper sources, try to read actual data first
    if conn_type.lower() in ('hyper', 'extract'):
        filename = connection.get('details', {}).get('filename', '')
        if filename:
            try:
                from hyper_reader import read_hyper
                result = _as_dict(read_hyper(filename, max_rows=20))
                hyper_tables = _as_list(result.get('tables', []))
                if hyper_tables:
                    m_query = generate_m_from_hyper(
                        hyper_tables, table.get('name'))
            except (ImportError, OSError, KeyError, ValueError) as exc:
                logger.debug('Hyper read failed for %s: %s', node_name, exc)

    if m_query is None:
        # Keep lineage metadata stable while dispatching to an actual builder
        # connector (CSV instead of textscan, PostgreSQL instead of postgres).
        m_connection = {**connection, 'type': _prep_m_connection_type(conn_type)}
        m_query = generate_power_query_m(m_connection, table)

    node_results[nid] = {
        'connection': connection,
        'table': table,
        'name': node_name,
        'm_query': m_query,
        'fields': _schema(_as_list(node.get('fields', [])), f'input {nid!r}'),
        'prep_dependencies': [],
    }
    print(f"    ✓ Input: {node_name} ({connection.get('type', '?')})")


def _process_transform_node(nid, node, nodes, upstream_ids, sem_type,
                            node_results, secondary_branch_ids, node_name):
    """Dispatch a transform node to its specific handler based on semantic type."""
    node = _as_dict(node)
    nodes = _as_dict(nodes)
    upstream_ids = _as_list(upstream_ids)

    source_types = ('PublishedDataSource', 'LoadPublishedDataSource')
    if sem_type not in source_types:
        if not upstream_ids or any(uid not in node_results for uid in upstream_ids):
            raise ValueError(f'Prep transform {nid!r} ({sem_type}): unresolved upstream '
                             f'{upstream_ids!r}')
        if sem_type not in ('Join', 'CrossJoin', 'Union') and len(upstream_ids) != 1:
            raise ValueError(f'Prep transform {nid!r} ({sem_type}): expected one upstream, '
                             f'got {upstream_ids!r}')

    action_types = {
        'AddColumn', 'RemoveColumn', 'RemoveColumns', 'RenameColumn',
        'ValueFilter', 'ChangeColumnType', 'FilterOperation', 'DuplicateColumn',
        'RichRangeFilter', 'RangeFilter', 'Remap', 'RichDiscreteValueFilter',
        'QuickCalcColumn', 'RichWildcardFilter', 'RichNullFilter',
        'FilterValues', 'FilterRange', 'ReplaceValues', 'ReplaceNulls',
        'SplitColumn', 'MergeColumns', 'CleanOperation', 'FillValues',
        'GroupReplace', 'ConditionalColumn', 'ExtractValues', 'CustomCalculation',
    }
    if sem_type in action_types:
        if any(_as_dict(node.get(key)).get('actions') for key in
               ('beforeActionGroup', 'actionGroup', 'afterActionGroup')):
            raise ValueError(f'Prep operation {nid!r}: mixed standalone/grouped actions')
        action = {**node, 'actionType': sem_type}
        try:
            steps = _parse_clean_actions({'actionGroup': {'actions': [action]}})
        except ValueError as exc:
            raise ValueError(f'Prep operation {nid!r} ({sem_type}): {exc}') from exc
        upstream = node_results[upstream_ids[0]]
        node_results[nid] = {
            **upstream, 'name': node_name,
            'm_query': inject_m_steps(upstream['m_query'], steps),
            'fields': _clean_schema({'actionGroup': {'actions': [action]}},
                                    upstream.get('fields', [])),
        }
        return

    if sem_type in ('SuperTransform',):
        # Clean step — chain onto upstream M query
        if upstream_ids and upstream_ids[0] in node_results:
            upstream = node_results[upstream_ids[0]]
            steps = _parse_clean_actions(node)
            m_query = upstream['m_query']
            if steps:
                m_query = inject_m_steps(m_query, steps)
            node_results[nid] = {
                **upstream,
                'name': node_name,
                'm_query': m_query,
                'fields': _clean_schema(node, upstream.get('fields', [])),
            }
            print(f"    ✓ Clean: {node_name} ({len(steps)} actions)")

    elif sem_type in ('Aggregate', 'SuperAggregate'):
        if upstream_ids and upstream_ids[0] in node_results:
            upstream = node_results[upstream_ids[0]]
            step = _parse_aggregate_node(node)
            if not step:
                raise ValueError(f'Prep aggregate {nid!r}: missing grouping/aggregation fields')
            m_query = upstream['m_query']
            if step:
                m_query = inject_m_steps(m_query, [step])
            node_results[nid] = {
                **upstream,
                'name': node_name,
                'm_query': m_query,
                'fields': _aggregate_schema(node, upstream.get('fields', [])),
            }
            print(f"    ✓ Aggregate: {node_name}")

    elif sem_type == 'Join':
        # Join needs two upstream sources
        left_id = node.get('leftNodeId')
        right_id = node.get('rightNodeId')

        # A side may be inferred only as the unique complement of an explicitly
        # identified side. Dictionary/topological order is not left/right
        # provenance, even for an otherwise flat, legacy flow.
        if len(upstream_ids) == 2:
            if left_id in upstream_ids and not right_id:
                right_id = next(uid for uid in upstream_ids if uid != left_id)
            elif right_id in upstream_ids and not left_id:
                left_id = next(uid for uid in upstream_ids if uid != right_id)
        if not left_id or not right_id:
            raise ValueError(f'Prep join {nid!r}: explicit leftNodeId/rightNodeId '
                             'provenance required; upstream iteration order is ambiguous')

        if (len(upstream_ids) != 2 or left_id == right_id
                or {left_id, right_id} != set(upstream_ids)):
            raise ValueError(f'Prep join {nid!r}: requires two resolved left/right inputs; '
                             f'got {upstream_ids!r}')
        if left_id and left_id in node_results:
            left = node_results[left_id]
            right_name = 'RightTable'
            right_fields = []

            if right_id and right_id in node_results:
                right = node_results[right_id]
                right_name = right.get('name', 'RightTable')
                right_fields = right.get('fields', [])
                # Mark the right branch for emission as a separate query
                secondary_branch_ids.add(right_id)

            if node.get('joinType', 'inner') not in _PREP_JOIN_MAP:
                raise ValueError(f'Prep join {nid!r}: unsupported joinType')
            left_fields = _schema(left.get('fields', []), 'join left')
            right_fields = _schema(right_fields, 'join right')
            conditions = _as_list(node.get('joinConditions'))
            for condition in conditions:
                _require_field(left_fields, condition.get('leftColumn'), 'join left key')
                _require_field(right_fields, condition.get('rightColumn'), 'join right key')
            right_keys = {c.get('rightColumn') for c in conditions}
            expanded = [f for f in right_fields if f['name'] not in right_keys]
            # NestedJoin introduces Joined; expansion removes it only if there
            # are fields to expand. Remove the empty nested column explicitly.
            if any(f['name'].casefold() == 'joined' for f in left_fields):
                raise ValueError(f'Prep join {nid!r}: reserved nested column Joined collides')
            joined_fields = _schema(left_fields + expanded, 'join output')
            join_steps = _parse_join_node(node, right_name, right_fields)
            if not join_steps:
                raise ValueError(f'Prep join {nid!r}: missing supported join conditions')
            if not expanded:
                join_steps.append(m_transform_remove_columns(['Joined']))
            m_query = left['m_query']
            if join_steps:
                m_query = inject_m_steps(m_query, join_steps)

            node_results[nid] = {
                **left,
                'name': node_name,
                'm_query': m_query,
                'fields': joined_fields,
                'prep_dependencies': list(dict.fromkeys(
                    left.get('prep_dependencies', []) + [_clean_m_table_ref(right_name)])),
            }
            print(f"    ✓ Join: {node_name} ({node.get('joinType', 'inner')})")

    elif sem_type == 'Union':
        upstream_names = []
        for uid in upstream_ids:
            if uid in node_results:
                raw_name = node_results[uid].get('name', uid[:8])
                upstream_names.append(_clean_m_table_ref(raw_name))
                # Mark all union inputs as secondary branches
                secondary_branch_ids.add(uid)

        # For union, take first upstream as base
        if upstream_ids and upstream_ids[0] in node_results:
            upstream = node_results[upstream_ids[0]]
            step = _parse_union_node(node, upstream_names)
            m_query = upstream['m_query']
            if step:
                m_query = inject_m_steps(m_query, [step])
            node_results[nid] = {
                **upstream,
                'name': node_name,
                'm_query': m_query,
                'fields': _union_schema([node_results[uid] for uid in upstream_ids], node),
                'prep_dependencies': upstream_names,
            }
            print(f"    ✓ Union: {node_name} ({len(upstream_names)} inputs)")

    elif sem_type == 'Pivot':
        if upstream_ids and upstream_ids[0] in node_results:
            upstream = node_results[upstream_ids[0]]
            step = _parse_pivot_node(node)
            if not step:
                raise ValueError(f'Prep pivot {nid!r}: unsupported/incomplete pivot configuration')
            if node.get('pivotType') != 'columnsToRows':
                raise ValueError(f'Prep pivot {nid!r}: data-dependent rowsToColumns schema; '
                                 'static output columns cannot be proven')
            pivot_names = [f.get('name') for f in _as_list(node.get('pivotFields'))]
            pivot_fields = [_require_field(upstream.get('fields', []), name, 'unpivot')
                            for name in pivot_names if name]
            types = {f['type'] for f in pivot_fields}
            if len(types) > 1 and not types <= {'integer', 'real'}:
                raise ValueError(f'Prep pivot {nid!r}: incompatible value types {types!r}')
            fields = [deepcopy(f) for f in upstream.get('fields', [])
                      if f['name'] not in pivot_names]
            fields.extend([
                {'name': node.get('pivotNamesName', 'Attribute'), 'type': 'string'},
                {'name': node.get('pivotValuesName', 'Value'),
                 'type': 'real' if 'real' in types else next(iter(types))},
            ])
            m_query = upstream['m_query']
            if step:
                m_query = inject_m_steps(m_query, [step])
            node_results[nid] = {
                **upstream,
                'name': node_name,
                'm_query': m_query,
                'fields': _schema(fields, 'unpivot output'),
            }
            print(f"    ✓ Pivot: {node_name} ({node.get('pivotType', '?')})")

    elif sem_type in ('Script', 'RunScript', 'RunCommand'):
        # Script step (Python/R) — not directly convertible; pass through with comment
        if upstream_ids and upstream_ids[0] in node_results:
            upstream = node_results[upstream_ids[0]]
            script_lang = node.get('scriptLanguage', node.get('language', 'Python'))
            comment_step = (
                '#"Script Warning"',
                f'Table.AddColumn({{prev}}, "__script_warning", '
                f'each "/* {script_lang} script step not auto-converted — '
                f'manual migration required */", type text)'
            )
            m_query = inject_m_steps(upstream['m_query'], [comment_step])
            node_results[nid] = {
                **upstream,
                'name': node_name,
                'm_query': m_query,
                'fields': _schema(upstream.get('fields', []) + [
                    {'name': '__script_warning', 'type': 'string'}], 'script warning'),
            }
            print(f"    ⚠ Script step ({script_lang}): {node_name} — manual migration required")

    elif sem_type in ('Prediction', 'TabPy', 'Einstein'):
        # Prediction / ML step — not convertible; pass through with warning
        if upstream_ids and upstream_ids[0] in node_results:
            upstream = node_results[upstream_ids[0]]
            comment_step = (
                '#"Prediction Warning"',
                f'Table.AddColumn({{prev}}, "__prediction_warning", '
                f'each "/* ML/prediction step not auto-converted — '
                f'use Power BI AutoML or Python visual */", type text)'
            )
            m_query = inject_m_steps(upstream['m_query'], [comment_step])
            node_results[nid] = {
                **upstream,
                'name': node_name,
                'm_query': m_query,
                'fields': _schema(upstream.get('fields', []) + [
                    {'name': '__prediction_warning', 'type': 'string'}], 'prediction warning'),
            }
            print(f"    ⚠ Prediction step: {node_name} — manual migration required")

    elif sem_type == 'CrossJoin':
        # Cross join (no key condition) → Table.Join with no key
        if len(upstream_ids) != 2:
            raise ValueError(f'Prep cross join {nid!r}: expected two inputs, got {upstream_ids!r}')
        if upstream_ids and len(upstream_ids) >= 2:
            left_id = upstream_ids[0]
            right_id = upstream_ids[1]
            if left_id in node_results and right_id in node_results:
                left = node_results[left_id]
                right = node_results[right_id]
                right_name = _clean_m_table_ref(right.get('name', 'RightTable'))
                secondary_branch_ids.add(right_id)
                cross_step = (
                    '#"Cross Join"',
                    f'Table.Join({{prev}}, {{}}, {right_name}, {{}}, JoinKind.FullOuter)'
                )
                m_query = inject_m_steps(left['m_query'], [cross_step])
                node_results[nid] = {
                    **left,
                    'name': node_name,
                    'm_query': m_query,
                    'fields': _schema(left.get('fields', []) + right.get('fields', []),
                                      'cross join output'),
                    'prep_dependencies': list(dict.fromkeys(
                        left.get('prep_dependencies', []) + [right_name])),
                }
                print(f"    ✓ Cross Join: {node_name}")

    elif sem_type in ('PublishedDataSource', 'LoadPublishedDataSource'):
        # Published data source input — reference as external table
        ds_name = node.get('publishedDatasourceName',
                           node.get('datasourceName', node_name))
        table_ref = _clean_m_table_ref(ds_name)
        m_query = (
            f'let\n'
            f'    // Published Data Source: {ds_name}\n'
            f'    // TODO: Replace with actual Power BI dataset reference\n'
            f'    Source = #"{table_ref}"\n'
            f'in\n    Source'
        )
        node_results[nid] = {
            'connection': {'type': 'published', 'details': {}},
            'table': {'name': table_ref, 'columns': []},
            'name': node_name,
            'm_query': m_query,
            'fields': node.get('fields', []),
            'prep_dependencies': [],
        }
        print(f"    ✓ Published DS Input: {node_name} → {ds_name}")

    else:
        raise ValueError(f'Prep unsupported transform {nid!r} ({node_name}): {sem_type!r}; '
                         'manual migration required')


def _collect_prep_datasources(sorted_ids, nodes, node_results, secondary_branch_ids):
    """Collect processed node results into datasource definitions for the PBI pipeline."""
    nodes = _as_dict(nodes)
    node_results = _as_dict(node_results)
    secondary_branch_ids = set(secondary_branch_ids or [])
    datasources = []
    output_count = 0
    explicit_outputs = [nid for nid, node in nodes.items()
                        if get_prep_node_kind(node) == 'output']
    unresolved = [nid for nid in explicit_outputs
                  if not node_results.get(nid, {}).get('is_output')]
    if unresolved:
        raise ValueError(f'Prep explicit outputs have no resolved result: {unresolved!r}')

    emitted_names = {}

    def reserve_table_name(table_name, nid):
        # Power BI query/table names are case-insensitive. Check the emitted
        # name, not just the caption: "Sales Data" and "Sales_Data" collide too.
        key = table_name.casefold()
        if key in emitted_names:
            raise ValueError(f'Duplicate Prep output/table name {table_name!r}: '
                             f'nodes {emitted_names[key]!r} and {nid!r}')
        emitted_names[key] = nid
        unknown = [f['name'] for f in node_results[nid].get('fields', [])
                   if f.get('type') == 'unknown']
        if unknown:
            raise ValueError(f'Prep emitted table {table_name!r}: unresolved field types '
                             f'{unknown!r}; supply validated output fields or ChangeColumnType')

    # First, emit secondary branch nodes (join right-tables, union extras)
    # so their queries exist in the model for the main query to reference
    for sec_id in secondary_branch_ids:
        if sec_id in node_results:
            result = node_results[sec_id]
            raw_name = result.get('name', 'SecondaryTable')
            table_name = _clean_m_table_ref(raw_name)
            reserve_table_name(table_name, sec_id)
            columns = []
            for f in _as_list(result.get('fields', [])):
                if not isinstance(f, dict):
                    continue
                columns.append({
                    'name': f.get('name', ''),
                    'datatype': f.get('type', 'string'),
                })

            datasource = {
                'name': f'prep.{table_name}',
                'caption': raw_name,
                'tables': [{
                    'name': table_name,
                    'columns': columns,
                }],
                'connection': result.get('connection', {}),
                'connection_map': {},
                'connections': [result.get('connection', {})],
                'columns_metadata': [],
                'calculations': [],
                'relationships': [],
                'm_query_override': result.get('m_query', ''),
                'is_prep_source': True,
                'prep_role': 'secondary',
                'prep_dependencies': result.get('prep_dependencies', []),
            }
            datasources.append(datasource)
            print(f"    + Secondary table emitted: {table_name}")

    # Then collect output nodes
    for nid, result in node_results.items():
        if result.get('is_output', False):
            ds_name = result.get('name', 'PrepOutput')
            table_name = ds_name.replace(' ', '_')
            reserve_table_name(table_name, nid)
            columns = []
            for f in _as_list(result.get('fields', [])):
                if not isinstance(f, dict):
                    continue
                columns.append({
                    'name': f.get('name', ''),
                    'datatype': f.get('type', 'string'),
                })

            datasource = {
                'name': f'prep.{table_name}',
                'caption': ds_name,
                'tables': [{
                    'name': table_name,
                    'columns': columns,
                }],
                'connection': result.get('connection', {}),
                'connection_map': {},
                'connections': [result.get('connection', {})],
                'columns_metadata': [],
                'calculations': [],
                'relationships': [],
                'm_query_override': result.get('m_query', ''),
                'is_prep_source': True,
                'prep_role': 'output',
                'prep_dependencies': result.get('prep_dependencies', []),
                'prep_output_type': result.get('prep_output_type', ''),
            }
            datasources.append(datasource)
            output_count += 1

    # If no output nodes, use all leaf nodes (nodes with no outgoing edges)
    if not explicit_outputs:
        for nid in sorted_ids:
            node = _as_dict(nodes.get(nid, {}))
            if not node.get('nextNodes') and nid in node_results:
                result = node_results[nid]
                ds_name = result.get('name', 'PrepOutput')
                table_name = ds_name.replace(' ', '_')
                reserve_table_name(table_name, nid)
                datasource = {
                    'name': f'prep.{table_name}',
                    'caption': ds_name,
                    'tables': [{
                        'name': table_name,
                        'columns': [{'name': f.get('name', ''), 'datatype': f.get('type', 'string')}
                                    for f in _as_list(result.get('fields', []))
                                    if isinstance(f, dict)],
                    }],
                    'connection': result.get('connection', {}),
                    'connection_map': {},
                    'connections': [result.get('connection', {})],
                    'columns_metadata': [],
                    'calculations': [],
                    'relationships': [],
                    'm_query_override': result.get('m_query', ''),
                    'is_prep_source': True,
                    'prep_role': 'output',
                    'prep_dependencies': result.get('prep_dependencies', []),
                }
                datasources.append(datasource)
                output_count += 1

    if not output_count:
        raise ValueError('Prep flow produced zero outputs (no resolved explicit output or leaf)')
    return datasources


# ═══════════════════════════════════════════════════════════════════════════════
#  MERGE PREP DATASOURCES WITH TWB DATASOURCES
# ═══════════════════════════════════════════════════════════════════════════════

class PrepBindingError(ValueError):
    """A rejected binding plan, carrying coverage without mutating its inputs."""

    def __init__(self, message, coverage):
        self.coverage = coverage
        super().__init__(message + '; coverage=' + json.dumps(coverage, ensure_ascii=False))


def _published_evidence(ds):
    connections = [ds.get('connection', {})]
    connections.extend(_as_dict(ds.get('connection_map')).values())
    connections.extend(_as_list(ds.get('connections')))
    connections.extend(t.get('connection_details', {}) for t in ds.get('tables', []))
    published = bool(ds.get('published_datasource') or ds.get('_published_unresolved')
                     or ds.get('_published_resolved'))
    names = set()
    for connection in connections:
        if connection.get('type', '').lower() in ('tableau server', 'sqlproxy', 'published'):
            published = True
            name = _as_dict(connection.get('details')).get('server_ds_name')
            if name:
                names.add(name)
    metadata = _as_dict(ds.get('published_datasource'))
    if metadata.get('name'):
        names.add(metadata['name'])
    return published, names


def merge_prep_with_workbook(prep_datasources, twb_datasources, *, return_report=False):
    """Plan, validate, then apply a workbook binding on deep copies.

    Contract: every published workbook table must be reconnected, and at least
    one output must bind. Unrelated local tables and auxiliary flow outputs do
    not need to match. Unused outputs are reported, NOT appended as a substitute
    for reconnecting workbook sources. Secondary queries are included only when
    referenced by a bound query. Empty workbook input remains standalone mode.

    Match by exact table name / output caption (including its deterministic
    space-to-underscore form). The sole reserved convention is
    F_<digits>_<subject> -> S_F<digits>_<subject>, requiring a unique output and
    published metadata explicitly naming that S_F datasource. No fuzzy match,
    table-order matching or synthetic connection is permitted.
    """
    coverage = {'status': 'rejected', 'matched': [], 'unmatched_published': [],
                'unmatched_local': [], 'unused_outputs': [], 'secondary_queries': []}

    def reject(message):
        raise PrepBindingError(message + '. Supply exact output names/captions or '
                               'published datasource identity metadata; no fuzzy binding '
                               'or invented connection is performed', coverage)

    for label, datasources in (('Prep', prep_datasources), ('workbook', twb_datasources)):
        if not isinstance(datasources, list):
            reject(f'{label} datasources must be a list')
        for ds in datasources:
            if not isinstance(ds, dict) or not isinstance(ds.get('tables', []), list):
                reject(f'{label} datasource requires an object with a tables list')
            if any(not isinstance(t, dict) or not isinstance(t.get('name'), str)
                   or not t['name'] for t in ds.get('tables', [])):
                reject(f'{label} tables require nonempty string names')
            if label == 'Prep':
                query = ds.get('m_query_override')
                if not isinstance(query, str) or not query.strip():
                    reject('Prep datasource requires a nonempty string M query')

    # Empty workbook input remains a standalone flow; explicit workbook tables are
    # not required for the merge to succeed when no workbook data is present.
    if not twb_datasources:
        coverage['status'] = 'standalone'
        result = deepcopy(prep_datasources)
        return (result, coverage) if return_report else result
    if not prep_datasources:
        coverage['status'] = 'no_outputs'
        result = deepcopy(twb_datasources)
        return (result, coverage) if return_report else result

    aliases, outputs, secondary = {}, {}, {}
    for index, ds in enumerate(prep_datasources):
        if len(ds.get('tables', [])) != 1:
            reject('Prep binding requires one table per parsed datasource')
        table = ds['tables'][0]
        name = table.get('name')
        if not name or not isinstance(name, str):
            reject('Prep output requires a nonempty string table name')
        if ds.get('prep_role') == 'secondary':
            if name in secondary:
                reject(f'Ambiguous secondary query {name!r}')
            secondary[name] = index
            continue
        outputs[index] = name
        caption = ds.get('caption') or ''
        if not isinstance(caption, str):
            reject('Prep output caption must be a string')
        for alias in {name, caption, caption.replace(' ', '_')} - {''}:
            if alias in aliases and aliases[alias] != index:
                reject(f'Ambiguous Prep merge alias {alias!r}')
            aliases[alias] = index

    plan = []
    table_names = {}
    used = set()
    for ds_index, ds in enumerate(twb_datasources):
        published, evidence = _published_evidence(ds)
        tables = ds.get('tables', [])
        if published and not tables:
            coverage['unmatched_published'].append({'datasource': ds.get('name'), 'table': None})
        for table_index, table in enumerate(tables):
            name = table.get('name', '')
            identity = {'datasource': ds.get('name'), 'table': name}
            table_names.setdefault(name.casefold(), []).append(identity)
            candidate = aliases.get(name) if name != 'sqlproxy' else None
            method = 'exact_name_or_caption'
            # sqlproxy is a protocol token, never an output identity.
            if published and len(tables) == 1:
                exact = {aliases[alias] for alias in
                         ({ds.get('caption', '')} | evidence)
                         if alias != 'sqlproxy' and alias in aliases}
                if candidate is not None:
                    exact.add(candidate)
                if len(exact) > 1:
                    reject(f'Ambiguous published datasource/table identity {name!r}')
                if exact:
                    candidate = next(iter(exact))
                # Check the reserved convention even if an exact alias exists,
                # so exact-vs-prefix collisions cannot silently choose a winner.
                reserved = set()
                for alias, index in aliases.items():
                    match = re.fullmatch(r'F_(\d+)_(.+)', alias)
                    target = f'S_F{match[1]}_{match[2]}' if match else None
                    if target and target in evidence and target in (name, ds.get('caption')):
                        reserved.add(index)
                if candidate is not None:
                    reserved.add(candidate)
                if len(reserved) > 1:
                    reject(f'Ambiguous reserved-prefix binding for {name!r}')
                if candidate is None and reserved:
                    candidate = next(iter(reserved))
                    method = 'reserved_prefix_with_published_identity'
            if candidate is None:
                coverage['unmatched_published' if published else 'unmatched_local'].append(identity)
                continue
            prep_table = prep_datasources[candidate]['tables'][0]
            if 'columns' in prep_table:
                columns = prep_table['columns']
                prep_names = {c['name'] for c in columns}
                missing = [c['name'] for c in table.get('columns', [])
                           if c['name'] not in prep_names]
                if missing:
                    reject(f'Workbook table {name!r} references absent Prep columns {missing!r}')
            plan.append((ds_index, table_index, candidate))
            used.add(candidate)
            coverage['matched'].append({**identity, 'output': outputs[candidate], 'method': method})

    coverage['unused_outputs'] = [name for index, name in outputs.items() if index not in used]
    if coverage['unmatched_published']:
        reject('Incomplete Prep binding: published workbook sources remain unreconnected')
    if not plan:
        reject('No exact Prep bindings found for workbook sources')
    for ds_index, table_index, _ in plan:
        name = twb_datasources[ds_index]['tables'][table_index]['name']
        if len(table_names[name.casefold()]) != 1:
            reject(f'Ambiguous workbook table name {name!r}; per-table override would collide')

    # Resolve only real secondary dependencies, recursively. Never add an
    # unmatched output and call it a successful workbook reconnection.
    needed = set()
    queries = [prep_datasources[i] for i in used]
    while queries:
        current = queries.pop()
        query = current['m_query_override']
        dependencies = current.get('prep_dependencies')
        if dependencies is not None:
            missing = set(dependencies) - set(secondary)
            if missing:
                reject(f'Unresolved secondary queries {sorted(missing)!r}')
        for name, index in secondary.items():
            quoted = '#"' + name.replace('"', '""') + '"'
            referenced = (name in dependencies if dependencies is not None else
                          quoted in query or re.search(
                              r'(?<![\w"])' + re.escape(name) + r'(?![\w"])', query))
            if index not in needed and referenced:
                if name.casefold() in table_names:
                    reject(f'Secondary query {name!r} collides with a workbook table')
                needed.add(index)
                queries.append(prep_datasources[index])
    merged = deepcopy(twb_datasources)
    for ds_index, table_index, index in plan:
        ds = merged[ds_index]
        table = ds['tables'][table_index]
        prep = prep_datasources[index]
        overrides = _as_dict(ds.get('m_query_overrides'))
        ds['m_query_overrides'] = overrides
        # TMDL collects the datasource-wide override after per-table overrides.
        # Preserve its intent for untouched tables, but do not let it mask the
        # binding just installed here.
        previous = ds.pop('m_query_override', '')
        if previous:
            for other in ds['tables']:
                overrides[other['name']] = previous
        overrides[table['name']] = prep['m_query_override']
        if 'columns' in prep['tables'][0]:
            table['columns'] = deepcopy(prep['tables'][0]['columns'])
    for index in sorted(needed):
        merged.append(deepcopy(prep_datasources[index]))
        coverage['secondary_queries'].append(prep_datasources[index]['tables'][0]['name'])
    coverage['status'] = 'complete'
    print('    Prep binding coverage: ' + json.dumps(coverage, ensure_ascii=False))
    return (merged, coverage) if return_report else merged
