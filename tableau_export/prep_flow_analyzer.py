"""
Prep Flow Analyzer — Per-flow metadata extraction for lineage analysis.

Parses individual .tfl/.tflx files and extracts structured metadata:
inputs (sources), outputs (destinations), transforms, DAG statistics,
and complexity signals. Produces FlowProfile objects used by the
cross-flow lineage engine (prep_lineage.py).

Usage::

    from prep_flow_analyzer import analyze_flow, analyze_flows_bulk

    profile = analyze_flow("path/to/flow.tfl")
    profiles = analyze_flows_bulk("path/to/flows/")
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from prep_flow_parser import (
    _PREP_CONNECTION_MAP,
    _get_node_type,
    _topological_sort,
    get_prep_node_kind,
    read_prep_flow,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

def _fingerprint(*parts: str) -> str:
    """SHA-256 fingerprint from normalized parts."""
    raw = '\x00'.join(str(p or '').lower().strip() for p in parts)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


@dataclass
class FlowInput:
    """A source feeding into a Prep flow."""
    node_id: str
    name: str
    connection_type: str
    server: str = ''
    database: str = ''
    schema: str = ''
    table_name: str = ''
    filename: str = ''
    column_count: int = 0
    column_names: List[str] = field(default_factory=list)
    fingerprint: str = ''

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class FlowOutput:
    """A destination produced by a Prep flow."""
    node_id: str
    name: str
    output_type: str = ''
    target_server: str = ''
    target_database: str = ''
    target_table: str = ''
    target_filename: str = ''
    column_count: int = 0
    column_names: List[str] = field(default_factory=list)
    fingerprint: str = ''

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class FlowTransform:
    """A transformation step inside a Prep flow."""
    node_id: str
    name: str
    transform_type: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class FlowProfile:
    """Complete metadata for one Tableau Prep flow."""
    name: str
    file_path: str
    inputs: List[FlowInput] = field(default_factory=list)
    outputs: List[FlowOutput] = field(default_factory=list)
    transforms: List[FlowTransform] = field(default_factory=list)
    node_count: int = 0
    edge_count: int = 0
    dag_depth: int = 0
    join_count: int = 0
    union_count: int = 0
    script_count: int = 0
    calc_count: int = 0
    node_graph: Dict[str, List[str]] = field(default_factory=dict)
    m_queries: Dict[str, str] = field(default_factory=dict)
    assessment: Dict[str, Any] = field(default_factory=dict)

    @property
    def complexity_score(self) -> int:
        """Weighted complexity: joins×3 + unions×2 + scripts×5 + calcs×1 + nodes×0.5."""
        return int(
            self.join_count * 3
            + self.union_count * 2
            + self.script_count * 5
            + self.calc_count * 1
            + self.node_count * 0.5
        )

    @property
    def complexity_label(self) -> str:
        s = self.complexity_score
        if s <= 10:
            return 'Low'
        if s <= 30:
            return 'Medium'
        return 'High'

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'file_path': self.file_path,
            'inputs': [i.to_dict() for i in self.inputs],
            'outputs': [o.to_dict() for o in self.outputs],
            'transforms': [t.to_dict() for t in self.transforms],
            'node_count': self.node_count,
            'edge_count': self.edge_count,
            'dag_depth': self.dag_depth,
            'join_count': self.join_count,
            'union_count': self.union_count,
            'script_count': self.script_count,
            'calc_count': self.calc_count,
            'complexity_score': self.complexity_score,
            'complexity_label': self.complexity_label,
            'node_graph': self.node_graph,
            'm_queries': self.m_queries,
            'assessment': self.assessment,
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  EXTRACTION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_inputs(nodes: dict, connections: dict) -> List[FlowInput]:
    """Collect all input nodes from the flow."""
    inputs = []
    for nid, node in (nodes or {}).items():
        if get_prep_node_kind(node) != 'input':
            continue

        conn_id = node.get('connectionId', '')
        conn_def = (connections or {}).get(conn_id) or {}
        conn_attrs = {**(conn_def.get('connectionAttributes') or {}),
                      **(node.get('connectionAttributes') or {})}

        conn_class = str(conn_attrs.get('class') or '').lower()
        conn_type = _PREP_CONNECTION_MAP.get(conn_class, conn_class)

        server = conn_attrs.get('server', '')
        database = conn_attrs.get('dbname', conn_attrs.get('database', ''))
        schema = conn_attrs.get('schema', '')
        table_name = conn_attrs.get('table', node.get('name', ''))
        filename = conn_attrs.get('filename', '')

        col_names = _field_names(node)

        fp = _fingerprint(conn_type, server, database, schema, table_name or filename)

        inputs.append(FlowInput(
            node_id=nid,
            name=node.get('name', nid[:8]),
            connection_type=conn_type,
            server=server,
            database=database,
            schema=schema,
            table_name=table_name,
            filename=filename,
            column_count=len(col_names),
            column_names=col_names,
            fingerprint=fp,
        ))
    return inputs


def _extract_outputs(nodes: dict) -> List[FlowOutput]:
    """Collect all output nodes (and leaf nodes as fallback)."""
    outputs = []
    has_output_base = False

    for nid, node in (nodes or {}).items():
        if get_prep_node_kind(node) == 'output':
            has_output_base = True
            sem = _get_node_type(node)
            attrs = node.get('connectionAttributes') or {}
            col_names = _field_names(node)
            target_server = attrs.get('server', '')
            target_db = attrs.get('dbname', attrs.get('database', ''))
            target_table = attrs.get('table', node.get('name', ''))
            target_file = attrs.get('filename', '')

            fp = _fingerprint(sem, target_server, target_db, '', target_table or target_file)

            outputs.append(FlowOutput(
                node_id=nid,
                name=node.get('name', nid[:8]),
                output_type=sem,
                target_server=target_server,
                target_database=target_db,
                target_table=target_table,
                target_filename=target_file,
                column_count=len(col_names),
                column_names=col_names,
                fingerprint=fp,
            ))

    # Fallback: leaf nodes (no outgoing edges)
    if not has_output_base:
        for nid, node in (nodes or {}).items():
            if not node.get('nextNodes'):
                col_names = _field_names(node)
                name = node.get('name', nid[:8])
                fp = _fingerprint('leaf', '', '', '', name)
                outputs.append(FlowOutput(
                    node_id=nid,
                    name=name,
                    output_type='LeafNode',
                    column_count=len(col_names),
                    column_names=col_names,
                    fingerprint=fp,
                ))
    return outputs


_TRANSFORM_TYPES = {
    'SuperTransform': 'Clean',
    'SuperAggregate': 'Aggregate',
    'Aggregate': 'Aggregate',
    'SuperJoin': 'Join',
    'Join': 'Join',
    'SuperUnion': 'Union',
    'Union': 'Union',
    'Pivot': 'Pivot',
    'Script': 'Script',
    'RunScript': 'Script',
    'RunCommand': 'Script',
    'Prediction': 'Prediction',
    'TabPy': 'Script',
    'Einstein': 'Prediction',
    'CrossJoin': 'Join',
    'PublishedDataSource': 'PublishedDS',
    'LoadPublishedDataSource': 'PublishedDS',
}

# Standalone clean operations accepted by the parser's transform dispatcher.
# Keep unknown semantics as Other so analysis-only inventory remains possible.
_DIRECT_CLEAN_TYPES = {
    'AddColumn', 'RemoveColumn', 'RemoveColumns', 'RenameColumn',
    'ValueFilter', 'ChangeColumnType', 'FilterOperation', 'DuplicateColumn',
    'RichRangeFilter', 'RangeFilter', 'Remap', 'RichDiscreteValueFilter',
    'QuickCalcColumn', 'RichWildcardFilter', 'RichNullFilter',
    'FilterValues', 'FilterRange', 'ReplaceValues', 'ReplaceNulls',
    'SplitColumn', 'MergeColumns', 'CleanOperation', 'FillValues',
    'GroupReplace', 'ConditionalColumn', 'ExtractValues', 'CustomCalculation',
}


def _field_names(node: dict) -> List[str]:
    """Read optional fields, including when helpers are called without normalization."""
    return [f.get('name') or '' for f in (node.get('fields') or [])
            if isinstance(f, dict)]


def _clean_actions(node: dict) -> List[dict]:
    """Inventory grouped and legacy actions, or a standalone clean operation."""
    if _get_node_type(node) in _DIRECT_CLEAN_TYPES:
        return [node]
    actions = []
    for key in ('beforeActionGroup', 'actionGroup', 'afterActionGroup'):
        actions.extend((node.get(key) or {}).get('actions') or [])
    actions.extend(node.get('actions') or [])
    return [action for action in actions if isinstance(action, dict)]


def _extract_transforms(sorted_ids: list, nodes: dict) -> List[FlowTransform]:
    """Classify each transform step and extract action-level detail."""
    transforms = []
    for nid in sorted_ids:
        node = nodes.get(nid, {})
        if get_prep_node_kind(node) != 'transform':
            continue
        sem = _get_node_type(node)
        ttype = 'Clean' if sem in _DIRECT_CLEAN_TYPES else _TRANSFORM_TYPES.get(sem, 'Other')
        details: Dict[str, Any] = {}

        if ttype == 'Clean':
            actions = _clean_actions(node)
            details['action_count'] = len(actions)
            details['action_types'] = sorted({
                _clean_action_type(a) for a in actions})
            # Extract individual operations for retro-documentation
            operations = _extract_clean_operations(actions, node)
            if operations:
                details['operations'] = operations
        elif sem in ('SuperJoin', 'Join', 'CrossJoin'):
            details['join_type'] = node.get('joinType', 'inner')
            conditions = node.get('joinConditions') or []
            details['key_count'] = len(conditions)
            # Extract join key columns
            keys = []
            for cond in conditions:
                left = cond.get('leftField', cond.get('left', ''))
                right = cond.get('rightField', cond.get('right', ''))
                if left or right:
                    keys.append({'left': left, 'right': right})
            if keys:
                details['join_keys'] = keys
        elif sem in ('Aggregate', 'SuperAggregate'):
            details['group_fields'] = len(node.get('groupByFields') or [])
            details['agg_fields'] = len(node.get('aggregateFields') or [])
            # Extract group-by and aggregate column names
            gnames = [f.get('name', '') for f in (node.get('groupByFields') or []) if f.get('name')]
            anames = []
            for af in node.get('aggregateFields') or []:
                agg = af.get('aggregation', af.get('type', ''))
                col = af.get('name', af.get('field', ''))
                anames.append(f'{agg}({col})' if agg else col)
            if gnames:
                details['group_by_columns'] = gnames
            if anames:
                details['aggregate_columns'] = anames
        elif sem == 'Pivot':
            details['pivot_type'] = node.get('pivotType', '')
        elif sem in ('Script', 'RunScript', 'TabPy', 'RunCommand'):
            details['script_type'] = node.get('scriptType', node.get('language', 'unknown'))

        # Capture input/output column names when present
        fields = _field_names(node)
        if fields:
            details['output_columns'] = [name for name in fields if name]

        transforms.append(FlowTransform(
            node_id=nid,
            name=node.get('name', nid[:8]),
            transform_type=ttype,
            details=details,
        ))
    return transforms


# ── Clean step operation extractor ──────────────────────────────────────────

# Map nodeType suffixes to human-readable operation categories
_ACTION_TYPE_MAP = {
    'RemoveColumns': 'remove_columns',
    'RenameColumn': 'rename_column',
    'RenameColumns': 'rename_columns',
    'ChangeDataType': 'change_type',
    'Filter': 'filter',
    'FilterValues': 'filter',
    'FilterRange': 'filter',
    'FilterCalculation': 'filter',
    'AddColumn': 'add_column',
    'ConditionalColumn': 'conditional_column',
    'calc': 'calculated_field',
    'GroupReplace': 'group_replace',
    'SplitValues': 'split',
    'MergeValues': 'merge',
    'CleanText': 'clean_text',
    'Replace': 'replace_value',
    'ReplaceValues': 'replace_value',
    'SortValues': 'sort',
    'RemoveColumn': 'remove_columns',
    'ChangeColumnType': 'change_type',
    'CustomCalculation': 'calculated_field',
    'QuickCalcColumn': 'calculated_field',
    'SplitColumn': 'split',
    'MergeColumns': 'merge',
    'ReplaceNulls': 'replace_value',
}


def _clean_action_type(action: dict) -> str:
    raw_type = (action.get('type') or action.get('actionType')
                or action.get('nodeType') or '')
    return raw_type.rsplit('.', 1)[-1]


def _extract_clean_operations(actions: list, node: dict) -> List[Dict[str, Any]]:
    """Extract individual operations from a SuperTransform's action list.

    Each operation is returned as a dict with at minimum 'type' and 'description'.
    """
    ops: List[Dict[str, Any]] = []
    for action in actions or []:
        if not isinstance(action, dict):
            continue
        raw_type = _clean_action_type(action)

        op_type = _ACTION_TYPE_MAP.get(raw_type, raw_type or 'unknown')
        name = action.get('name', '')
        column = action.get('column', action.get('columnName', ''))
        expression = action.get('expression', action.get('formula', ''))

        op: Dict[str, Any] = {'type': op_type}
        if name:
            op['name'] = name
        if column:
            op['column'] = column
        if expression:
            op['expression'] = str(expression)[:200]

        # Extract columns affected from the action name (e.g., "Remove Right_Row ID + 19 more")
        if not column and name:
            op['description'] = name
        elif column and expression:
            op['description'] = f'{column} = {str(expression)[:100]}'
        elif column:
            op['description'] = f'{op_type}: {column}'
        else:
            op['description'] = name or op_type

        ops.append(op)
    return ops


def _build_node_graph(nodes: dict) -> Tuple[Dict[str, List[str]], int]:
    """Build adjacency list and count edges."""
    graph: Dict[str, List[str]] = {}
    edge_count = 0
    for nid, node in nodes.items():
        nexts = []
        for edge in node.get('nextNodes') or []:
            next_id = edge.get('nextNodeId', '')
            if next_id:
                nexts.append(next_id)
                edge_count += 1
        graph[nid] = nexts
    return graph, edge_count


def _compute_dag_depth(nodes: dict) -> int:
    """Longest path from any input to any output."""
    graph: Dict[str, List[str]] = {}
    for nid, node in nodes.items():
        graph[nid] = [e.get('nextNodeId', '') for e in (node.get('nextNodes') or []) if e.get('nextNodeId')]

    sorted_ids = _topological_sort(nodes)
    depth: Dict[str, int] = {nid: 0 for nid in sorted_ids}

    for nid in sorted_ids:
        for next_id in graph.get(nid, []):
            if next_id in depth:
                depth[next_id] = max(depth[next_id], depth[nid] + 1)

    return max(depth.values()) if depth else 0


def _count_calcs(nodes: dict) -> int:
    """Count calculated fields across Clean steps."""
    count = 0
    for node in nodes.values():
        if get_prep_node_kind(node) == 'transform':
            for action in _clean_actions(node):
                atype = _clean_action_type(action)
                if atype in ('AddColumn', 'ConditionalColumn', 'calc',
                             'CustomCalculation', 'QuickCalcColumn'):
                    count += 1
    return count


def _assess_flow(inputs, outputs, transforms, join_count, union_count,
                 script_count, calc_count, dag_depth, node_count) -> Dict[str, Any]:
    """Produce a readiness assessment for one prep flow.

    Returns dict with grade (GREEN/YELLOW/RED), items list, and summary.
    """
    items = []  # list of {category, status, detail}
    # Datasource readiness
    unsupported_conns = [i for i in inputs if i.connection_type in ('unknown', '')]
    if unsupported_conns:
        items.append({'category': 'datasource', 'status': 'warn',
                      'detail': f'{len(unsupported_conns)} input(s) with unknown connector'})
    else:
        items.append({'category': 'datasource', 'status': 'pass',
                      'detail': f'{len(inputs)} input(s) — all connectors supported'})

    # Transform complexity
    if script_count > 0:
        items.append({'category': 'script', 'status': 'warn',
                      'detail': f'{script_count} Script node(s) — manual review needed'})
    else:
        items.append({'category': 'script', 'status': 'pass',
                      'detail': 'No Script nodes'})

    # Join complexity
    if join_count > 3:
        items.append({'category': 'join', 'status': 'warn',
                      'detail': f'{join_count} joins — consider simplifying'})
    elif join_count > 0:
        items.append({'category': 'join', 'status': 'pass',
                      'detail': f'{join_count} join(s) → Power Query Table.NestedJoin'})
    else:
        items.append({'category': 'join', 'status': 'pass', 'detail': 'No joins'})

    # Calcs
    if calc_count > 0:
        items.append({'category': 'calculation', 'status': 'pass',
                      'detail': f'{calc_count} calculated field(s) → Table.AddColumn'})

    # Aggregates
    agg_count = sum(1 for t in transforms if t.transform_type == 'Aggregate')
    if agg_count > 0:
        items.append({'category': 'aggregate', 'status': 'pass',
                      'detail': f'{agg_count} aggregate(s) → Table.Group'})

    # Unions
    if union_count > 0:
        items.append({'category': 'union', 'status': 'pass',
                      'detail': f'{union_count} union(s) → Table.Combine'})

    # Output count
    if len(outputs) == 0:
        items.append({'category': 'output', 'status': 'fail',
                      'detail': 'No outputs detected — flow may be incomplete'})
    else:
        items.append({'category': 'output', 'status': 'pass',
                      'detail': f'{len(outputs)} output(s)'})

    # Grade
    fail_count = sum(1 for it in items if it['status'] == 'fail')
    warn_count = sum(1 for it in items if it['status'] == 'warn')
    if fail_count > 0:
        grade = 'RED'
    elif warn_count > 0:
        grade = 'YELLOW'
    else:
        grade = 'GREEN'

    return {
        'grade': grade,
        'items': items,
        'pass_count': sum(1 for it in items if it['status'] == 'pass'),
        'warn_count': warn_count,
        'fail_count': fail_count,
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════════

def analyze_flow(filepath: str, include_m_queries: bool = False) -> FlowProfile:
    """Parse one TFL/TFLX and extract full metadata.

    Args:
        filepath: Path to .tfl or .tflx file
        include_m_queries: If True, also run parse_prep_flow to get Power Query M.
            Failures set assessment['conversion_error'] to a nonempty message,
            assessment['grade'] to RED, and assessment['status'] to error.
            Analysis-only profiles omit conversion_error.

    Returns:
        FlowProfile with inputs, outputs, transforms, DAG stats
    """
    flow = read_prep_flow(filepath)
    nodes = flow.get('nodes', {})
    connections = flow.get('connections', {})

    sorted_ids = _topological_sort(nodes)
    inputs = _extract_inputs(nodes, connections)
    outputs = _extract_outputs(nodes)
    transforms = _extract_transforms(sorted_ids, nodes)
    node_graph, edge_count = _build_node_graph(nodes)
    dag_depth = _compute_dag_depth(nodes)

    join_count = sum(1 for t in transforms if t.transform_type == 'Join')
    union_count = sum(1 for t in transforms if t.transform_type == 'Union')
    script_count = sum(1 for t in transforms if t.transform_type == 'Script')
    calc_count = _count_calcs(nodes)

    name = os.path.splitext(os.path.basename(filepath))[0]

    # Power Query M conversion
    m_queries: Dict[str, str] = {}
    conversion_error = ''
    if include_m_queries:
        try:
            from prep_flow_parser import parse_prep_flow
            datasources = parse_prep_flow(filepath)
            if not datasources:
                raise ValueError('Prep conversion produced zero outputs')
            for ds in datasources:
                if not isinstance(ds, dict):
                    raise ValueError('Prep conversion datasource must be a dictionary')
                tables = ds.get('tables', [])
                if not isinstance(tables, list):
                    raise ValueError('Prep conversion datasource tables must be a list')
                # M query can be at table level or datasource level (m_query_override)
                ds_mq = ds.get('m_query_override', '')
                for tbl in tables:
                    if not isinstance(tbl, dict):
                        raise ValueError('Prep conversion table must be a dictionary')
                    tbl_name = tbl.get('name', '')
                    mq = tbl.get('m_query', '') or ds_mq
                    if tbl_name and mq:
                        m_queries[tbl_name] = mq
            if not m_queries:
                raise ValueError('Prep conversion produced no output M queries')
        except (ImportError, OSError, ValueError, KeyError) as exc:
            # Conversion is optional: preserve inventory, but never report a
            # successful assessment or retain partial queries on failure.
            conversion_error = f'{type(exc).__name__}: {exc}'
            m_queries.clear()
            logger.warning("Could not extract M queries from %s: %s", filepath, exc)

    # Assessment
    assessment = _assess_flow(inputs, outputs, transforms,
                              join_count, union_count, script_count,
                              calc_count, dag_depth, len(nodes))
    if conversion_error:
        assessment['conversion_error'] = conversion_error
        assessment['grade'] = 'RED'
        assessment['status'] = 'error'
        assessment['items'].append({
            'category': 'conversion', 'status': 'fail',
            'detail': conversion_error,
        })
        assessment['fail_count'] += 1

    return FlowProfile(
        name=name,
        file_path=os.path.abspath(filepath),
        inputs=inputs,
        outputs=outputs,
        transforms=transforms,
        node_count=len(nodes),
        edge_count=edge_count,
        dag_depth=dag_depth,
        join_count=join_count,
        union_count=union_count,
        script_count=script_count,
        calc_count=calc_count,
        node_graph=node_graph,
        m_queries=m_queries,
        assessment=assessment,
    )


def analyze_flows_bulk(directory: str) -> List[FlowProfile]:
    """Scan a directory for .tfl/.tflx files and analyze each.

    Args:
        directory: Path to folder containing Prep flows

    Returns:
        List of FlowProfile objects sorted by name
    """
    profiles = []
    for root, _dirs, files in os.walk(directory):
        for fname in sorted(files):
            if fname.lower().endswith(('.tfl', '.tflx')):
                fpath = os.path.join(root, fname)
                try:
                    profile = analyze_flow(fpath)
                    profiles.append(profile)
                except (ValueError, OSError, KeyError) as exc:
                    logger.warning("Failed to analyze %s: %s", fpath, exc)
    return profiles
