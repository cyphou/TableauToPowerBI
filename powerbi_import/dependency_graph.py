"""
Site topology discovery and dependency graph engine.

Sprint 139 — Builds a complete topology of a Tableau Server site
(workbooks, datasources, prep flows, users, groups, schedules) and
constructs a dependency-ordered DAG for migration planning.

Usage::

    from powerbi_import.dependency_graph import (
        build_site_topology, build_dependency_graph, classify_usage,
        audit_certifications, generate_topology_report,
    )

    topology = build_site_topology(client)
    graph = build_dependency_graph(topology)
    usage = classify_usage(topology)
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  Site Topology Builder (Sprint 139.1)
# ═══════════════════════════════════════════════════════════════════════

def build_site_topology(client) -> Dict[str, Any]:
    """Build a complete site topology from a Tableau Server client.

    Orchestrates list_workbooks, list_datasources, list_prep_flows,
    list_users, list_groups, list_schedules, and get_workbook_connections
    to build a unified site map with adjacency information.

    Args:
        client: An authenticated TableauServerClient instance.

    Returns:
        dict: Site topology with keys:
            - site_info: Site metadata
            - workbooks: List of workbook dicts (enriched with connections)
            - datasources: List of published datasource dicts
            - prep_flows: List of Prep flow dicts
            - users: List of user dicts
            - groups: List of group dicts
            - schedules: List of schedule dicts
            - projects: List of project dicts
            - workbook_connections: {wb_id: [connection_dicts]}
            - ds_to_workbooks: {ds_id: [wb_ids]} (reverse index)
            - build_timestamp: ISO timestamp
    """
    logger.info("Building site topology...")

    site_info = _safe_call(client.get_site_info, default={})
    workbooks = _safe_call(client.list_workbooks, default=[])
    datasources = _safe_call(client.list_datasources, default=[])
    prep_flows = _safe_call(client.list_prep_flows, default=[])
    users = _safe_call(client.list_users, default=[])
    groups = _safe_call(client.list_groups, default=[])
    schedules = _safe_call(client.list_schedules, default=[])
    projects = _safe_call(client.list_projects, default=[])

    # Build workbook → connections mapping
    workbook_connections = {}
    ds_to_workbooks = defaultdict(list)

    for wb in workbooks:
        wb_id = wb.get('id', '')
        if not wb_id:
            continue
        conns = _safe_call(
            lambda wid=wb_id: client.get_workbook_connections(wid),
            default=[],
        )
        workbook_connections[wb_id] = conns

        # Build reverse index: datasource → workbooks
        for conn in conns:
            ds_id = conn.get('datasource', {}).get('id', '')
            if ds_id:
                ds_to_workbooks[ds_id].append(wb_id)

    # Enrich workbooks with usage stats (best-effort)
    for wb in workbooks:
        wb_id = wb.get('id', '')
        if wb_id:
            stats = _safe_call(
                lambda wid=wb_id: client.get_usage_stats(wid),
                default={},
            )
            wb['_usage'] = stats

    topology = {
        'site_info': site_info,
        'workbooks': workbooks,
        'datasources': datasources,
        'prep_flows': prep_flows,
        'users': users,
        'groups': groups,
        'schedules': schedules,
        'projects': projects,
        'workbook_connections': workbook_connections,
        'ds_to_workbooks': dict(ds_to_workbooks),
        'build_timestamp': datetime.now(timezone.utc).isoformat(),
    }

    logger.info(
        "Topology built: %d workbooks, %d datasources, %d users, %d projects",
        len(workbooks), len(datasources), len(users), len(projects),
    )
    return topology


# ═══════════════════════════════════════════════════════════════════════
#  Dependency Graph Engine (Sprint 139.2)
# ═══════════════════════════════════════════════════════════════════════

def build_dependency_graph(topology: Dict[str, Any]) -> Dict[str, Any]:
    """Build a directed dependency graph from a site topology.

    Nodes: workbooks + published datasources.
    Edges: datasource → workbook (datasource must migrate first).

    Args:
        topology: Site topology dict from build_site_topology().

    Returns:
        dict with keys:
            - nodes: [{id, name, type, project}]
            - edges: [(from_id, to_id)]  (from must migrate before to)
            - adjacency: {node_id: [dependent_ids]}
            - reverse_adj: {node_id: [dependency_ids]}
            - topological_order: [node_ids] in migration order
            - cycles: [[node_ids]] if cycles detected
            - shared_datasources: [{ds_id, ds_name, workbook_count, workbook_names}]
    """
    nodes = {}
    edges = []
    adjacency = defaultdict(list)
    reverse_adj = defaultdict(list)

    # Add datasource nodes
    for ds in topology.get('datasources', []):
        ds_id = ds.get('id', '')
        if ds_id:
            nodes[ds_id] = {
                'id': ds_id,
                'name': ds.get('name', 'Unknown DS'),
                'type': 'datasource',
                'project': ds.get('project', {}).get('name', ''),
            }

    # Add workbook nodes
    for wb in topology.get('workbooks', []):
        wb_id = wb.get('id', '')
        if wb_id:
            nodes[wb_id] = {
                'id': wb_id,
                'name': wb.get('name', 'Unknown WB'),
                'type': 'workbook',
                'project': wb.get('project', {}).get('name', ''),
            }

    # Build edges: datasource → workbook
    ds_to_wb = topology.get('ds_to_workbooks', {})
    for ds_id, wb_ids in ds_to_wb.items():
        for wb_id in wb_ids:
            if ds_id in nodes and wb_id in nodes:
                edges.append((ds_id, wb_id))
                adjacency[ds_id].append(wb_id)
                reverse_adj[wb_id].append(ds_id)

    # Detect shared datasources
    shared_datasources = []
    for ds_id, wb_ids in ds_to_wb.items():
        unique_wbs = list(set(wb_ids))
        if len(unique_wbs) > 1:
            ds_node = nodes.get(ds_id, {})
            shared_datasources.append({
                'ds_id': ds_id,
                'ds_name': ds_node.get('name', ''),
                'workbook_count': len(unique_wbs),
                'workbook_names': [
                    nodes.get(wid, {}).get('name', wid) for wid in unique_wbs
                ],
            })

    # Topological sort with cycle detection
    topo_order, cycles = _topological_sort(nodes, adjacency)

    return {
        'nodes': list(nodes.values()),
        'edges': edges,
        'adjacency': dict(adjacency),
        'reverse_adj': dict(reverse_adj),
        'topological_order': topo_order,
        'cycles': cycles,
        'shared_datasources': shared_datasources,
    }


def _topological_sort(
    nodes: Dict[str, Dict],
    adjacency: Dict[str, List[str]],
) -> Tuple[List[str], List[List[str]]]:
    """Kahn's algorithm for topological sort with cycle detection.

    Returns:
        (ordered_ids, cycles): Topological order and any detected cycles.
    """
    in_degree = defaultdict(int)
    for nid in nodes:
        in_degree[nid] = 0
    for nid, deps in adjacency.items():
        for dep in deps:
            in_degree[dep] += 1

    queue = deque(nid for nid, deg in in_degree.items() if deg == 0)
    result = []

    while queue:
        node = queue.popleft()
        result.append(node)
        for dep in adjacency.get(node, []):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)

    # Detect cycles: remaining nodes with in_degree > 0
    cycles = []
    remaining = {nid for nid in nodes if nid not in set(result)}
    if remaining:
        # Find cycle members via DFS
        visited = set()
        for start in remaining:
            if start in visited:
                continue
            cycle = []
            stack = [start]
            while stack:
                n = stack.pop()
                if n in visited:
                    continue
                visited.add(n)
                if n in remaining:
                    cycle.append(n)
                    for dep in adjacency.get(n, []):
                        if dep in remaining:
                            stack.append(dep)
            if cycle:
                cycles.append(cycle)
        # Append cycle nodes to end of result so they're not lost
        result.extend(remaining)

    return result, cycles


# ═══════════════════════════════════════════════════════════════════════
#  Usage Classification (Sprint 139.3)
# ═══════════════════════════════════════════════════════════════════════

def classify_usage(
    topology: Dict[str, Any],
    active_days: int = 30,
    stale_days: int = 180,
) -> Dict[str, Any]:
    """Classify workbooks by usage recency.

    Args:
        topology: Site topology dict.
        active_days: Days threshold for 'active' classification.
        stale_days: Days threshold for 'stale' vs 'dormant'.

    Returns:
        dict with keys:
            - active: [workbook_dicts] (accessed within active_days)
            - stale: [workbook_dicts] (between active_days and stale_days)
            - dormant: [workbook_dicts] (not accessed within stale_days)
            - unknown: [workbook_dicts] (no usage data)
            - summary: {active_count, stale_count, dormant_count, unknown_count}
    """
    now = datetime.now(timezone.utc)
    active, stale, dormant, unknown = [], [], [], []

    for wb in topology.get('workbooks', []):
        usage = wb.get('_usage', {})
        last_accessed = usage.get('lastAccessed', '')
        updated = wb.get('updatedAt', '')
        date_str = last_accessed or updated

        if not date_str:
            unknown.append(wb)
            continue

        try:
            # Parse ISO date (handle various formats)
            dt_str = date_str.replace('Z', '+00:00')
            if '+' not in dt_str and 'T' in dt_str:
                dt_str += '+00:00'
            dt = datetime.fromisoformat(dt_str)
            days_ago = (now - dt).days
        except (ValueError, TypeError):
            unknown.append(wb)
            continue

        wb['_days_since_access'] = days_ago

        if days_ago <= active_days:
            active.append(wb)
        elif days_ago <= stale_days:
            stale.append(wb)
        else:
            dormant.append(wb)

    return {
        'active': active,
        'stale': stale,
        'dormant': dormant,
        'unknown': unknown,
        'summary': {
            'active_count': len(active),
            'stale_count': len(stale),
            'dormant_count': len(dormant),
            'unknown_count': len(unknown),
        },
    }


# ═══════════════════════════════════════════════════════════════════════
#  Content Certification Audit (Sprint 139.4)
# ═══════════════════════════════════════════════════════════════════════

def audit_certifications(topology: Dict[str, Any], client=None) -> Dict[str, Any]:
    """Audit content certification and quality warnings.

    Args:
        topology: Site topology dict.
        client: Optional TableauServerClient for fetching quality warnings.

    Returns:
        dict with keys:
            - certified: [content_dicts] (certified, must migrate)
            - warned: [content_dicts] (quality warnings, needs review)
            - uncertified: [content_dicts] (no certification, lower priority)
            - warnings: [{content_id, content_name, warning_type, message}]
    """
    certified, warned, uncertified = [], [], []
    all_warnings = []

    all_content = []
    for wb in topology.get('workbooks', []):
        all_content.append({**wb, '_content_type': 'workbook'})
    for ds in topology.get('datasources', []):
        all_content.append({**ds, '_content_type': 'datasource'})

    for item in all_content:
        item_id = item.get('id', '')
        content_type = item.get('_content_type', 'workbook')

        # Check tags for certification
        tags = item.get('tags', {}).get('tag', [])
        if isinstance(tags, dict):
            tags = [tags]
        tag_labels = [t.get('label', '').lower() for t in tags]
        is_certified = 'certified' in tag_labels or item.get('isCertified', False)

        # Fetch quality warnings if client available
        warnings = []
        if client and item_id:
            warnings = _safe_call(
                lambda ctype=content_type, cid=item_id: client.get_quality_warnings(
                    content_type=ctype, content_id=cid
                ),
                default=[],
            )

        for w in warnings:
            all_warnings.append({
                'content_id': item_id,
                'content_name': item.get('name', ''),
                'content_type': content_type,
                'warning_type': w.get('type', ''),
                'message': w.get('message', ''),
            })

        if is_certified:
            certified.append(item)
        elif warnings:
            warned.append(item)
        else:
            uncertified.append(item)

    return {
        'certified': certified,
        'warned': warned,
        'uncertified': uncertified,
        'warnings': all_warnings,
        'summary': {
            'certified_count': len(certified),
            'warned_count': len(warned),
            'uncertified_count': len(uncertified),
            'warning_count': len(all_warnings),
        },
    }


# ═══════════════════════════════════════════════════════════════════════
#  Lineage Enrichment (Sprint 139.5)
# ═══════════════════════════════════════════════════════════════════════

def enrich_with_lineage(topology: Dict[str, Any], client) -> Dict[str, Any]:
    """Enrich topology with Metadata API lineage (Server 2019.3+).

    Falls back gracefully if Metadata API is unavailable.

    Args:
        topology: Site topology dict.
        client: Authenticated TableauServerClient.

    Returns:
        topology dict enriched with '_lineage' on each workbook.
    """
    for wb in topology.get('workbooks', []):
        wb_id = wb.get('id', '')
        if not wb_id:
            continue
        lineage = _safe_call(
            lambda wid=wb_id: client.get_lineage_upstream(wid),
            default={},
        )
        wb['_lineage'] = lineage

    return topology


# ═══════════════════════════════════════════════════════════════════════
#  Topology HTML Report (Sprint 139.6)
# ═══════════════════════════════════════════════════════════════════════

def generate_topology_report(
    topology: Dict[str, Any],
    graph: Dict[str, Any],
    usage: Dict[str, Any],
    certification: Dict[str, Any],
    output_path: str,
) -> str:
    """Generate an interactive HTML report for site topology.

    Args:
        topology: Site topology dict.
        graph: Dependency graph dict.
        usage: Usage classification dict.
        certification: Certification audit dict.
        output_path: File path for the HTML report.

    Returns:
        str: Path to the generated HTML file.
    """
    try:
        from powerbi_import.html_template import (
            html_open, html_close, stat_card, stat_grid,
            section_open, section_close, badge, data_table,
            donut_chart, esc,
        )
    except ImportError:
        from html_template import (
            html_open, html_close, stat_card, stat_grid,
            section_open, section_close, badge, data_table,
            donut_chart, esc,
        )

    parts = [html_open("Tableau Server — Site Topology Report")]

    # ── Executive Summary ──
    parts.append(stat_grid([
        stat_card(str(len(topology.get('workbooks', []))), "Workbooks", accent="blue"),
        stat_card(str(len(topology.get('datasources', []))), "Datasources", accent="purple"),
        stat_card(str(len(topology.get('prep_flows', []))), "Prep Flows", accent="teal"),
        stat_card(str(len(topology.get('users', []))), "Users"),
        stat_card(str(len(topology.get('projects', []))), "Projects"),
        stat_card(str(len(graph.get('shared_datasources', []))), "Shared DS", accent="warn"),
    ]))

    # ── Usage Classification ──
    parts.append(section_open("usage", "Usage Classification"))
    summary = usage.get('summary', {})
    parts.append(donut_chart([
        ("Active", summary.get('active_count', 0), "#107c10"),
        ("Stale", summary.get('stale_count', 0), "#ffb900"),
        ("Dormant", summary.get('dormant_count', 0), "#d13438"),
        ("Unknown", summary.get('unknown_count', 0), "#a19f9d"),
    ], center_text="Usage"))
    parts.append(section_close())

    # ── Certification Breakdown ──
    parts.append(section_open("cert", "Content Certification"))
    cert_summary = certification.get('summary', {})
    parts.append(donut_chart([
        ("Certified", cert_summary.get('certified_count', 0), "#107c10"),
        ("Warned", cert_summary.get('warned_count', 0), "#ffb900"),
        ("Uncertified", cert_summary.get('uncertified_count', 0), "#a19f9d"),
    ], center_text="Cert"))
    parts.append(section_close())

    # ── Dependency Graph (Mermaid) ──
    parts.append(section_open("depgraph", "Dependency Graph"))
    mermaid_lines = ["graph TD"]
    node_map = {n['id']: n for n in graph.get('nodes', [])}
    for edge in graph.get('edges', [])[:100]:  # Cap at 100 edges for readability
        src = node_map.get(edge[0], {})
        dst = node_map.get(edge[1], {})
        src_label = esc(src.get('name', edge[0])[:30])
        dst_label = esc(dst.get('name', edge[1])[:30])
        safe_src = edge[0].replace('-', '_')
        safe_dst = edge[1].replace('-', '_')
        mermaid_lines.append(f'    {safe_src}["{src_label}"] --> {safe_dst}["{dst_label}"]')
    if len(graph.get('edges', [])) > 100:
        mermaid_lines.append(f'    note["... and {len(graph["edges"]) - 100} more edges"]')
    parts.append(f'<div class="mermaid">{"<br>".join(mermaid_lines)}</div>')
    parts.append(section_close())

    # ── Shared Datasources Table ──
    if graph.get('shared_datasources'):
        parts.append(section_open("shared_ds", "Shared Datasources"))
        rows = []
        for sd in graph['shared_datasources']:
            rows.append([
                esc(sd['ds_name']),
                str(sd['workbook_count']),
                esc(', '.join(sd['workbook_names'][:5])),
            ])
        parts.append(data_table(
            headers=["Datasource", "# Workbooks", "Used By"],
            rows=rows,
        ))
        parts.append(section_close())

    # ── Workbook Inventory ──
    parts.append(section_open("wb_inv", "Workbook Inventory"))
    wb_rows = []
    for wb in topology.get('workbooks', []):
        usage_data = wb.get('_usage', {})
        days = wb.get('_days_since_access', '?')
        project = wb.get('project', {}).get('name', '')
        owner = wb.get('owner', {}).get('name', '')
        views = usage_data.get('totalViews', 0)
        wb_rows.append([
            esc(wb.get('name', '')),
            esc(project),
            esc(owner),
            str(views),
            str(days),
        ])
    parts.append(data_table(
        headers=["Workbook", "Project", "Owner", "Views", "Days Since Access"],
        rows=wb_rows[:200],  # Cap for performance
    ))
    parts.append(section_close())

    # ── Cycles Warning ──
    if graph.get('cycles'):
        parts.append(section_open("cycles", "⚠️ Circular Dependencies Detected"))
        for i, cycle in enumerate(graph['cycles']):
            names = [node_map.get(nid, {}).get('name', nid) for nid in cycle]
            parts.append(f'<p>Cycle {i + 1}: {" → ".join(esc(n) for n in names)}</p>')
        parts.append(section_close())

    # ── Topological Order ──
    parts.append(section_open("topo", "Migration Order (Topological)"))
    topo_rows = []
    for i, nid in enumerate(graph.get('topological_order', [])):
        node = node_map.get(nid, {})
        topo_rows.append([
            str(i + 1),
            badge(node.get('type', ''), 'blue' if node.get('type') == 'datasource' else 'green'),
            esc(node.get('name', nid)),
            esc(node.get('project', '')),
        ])
    parts.append(data_table(
        headers=["#", "Type", "Name", "Project"],
        rows=topo_rows[:200],
    ))
    parts.append(section_close())

    parts.append(html_close())

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(parts))

    logger.info("Topology report written to %s", output_path)
    return output_path


# ═══════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════

def _safe_call(func, default=None):
    """Call a function and return default on any exception."""
    try:
        result = func()
        return result if result is not None else default
    except Exception as e:
        logger.debug("Safe call failed: %s", e)
        return default


def save_topology(topology: Dict[str, Any], output_path: str) -> str:
    """Save site topology to a JSON file.

    Args:
        topology: Site topology dict.
        output_path: File path for the JSON output.

    Returns:
        str: Path to the saved file.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(topology, f, indent=2, default=str)
    logger.info("Topology saved to %s", output_path)
    return output_path


def load_topology(input_path: str) -> Dict[str, Any]:
    """Load site topology from a JSON file.

    Args:
        input_path: File path to load.

    Returns:
        dict: Site topology.
    """
    with open(input_path, 'r', encoding='utf-8') as f:
        return json.load(f)
