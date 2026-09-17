"""
Full Lineage — Cross-content lineage connecting Prep flows to Reports.

Bridges the gap between:
- prep_lineage.py (flow→flow connections)
- global_assessment.py (workbook↔workbook redundancy)

This module links Prep flow outputs to workbook datasource inputs by matching:
- Table names (exact and fuzzy)
- Connection fingerprints (server+database+table)
- Published datasource names
- Column overlap (Jaccard similarity)

Usage::

    from powerbi_import.full_lineage import build_full_lineage

    full = build_full_lineage(flow_profiles, workbook_extractions)
    # full.flow_to_report_edges → which flows feed which reports
    # full.orphan_flows → flows whose outputs feed no report
    # full.report_external_sources → reports that skip prep entirely
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

try:
    from prep_flow_analyzer import FlowProfile, FlowOutput
except ImportError:
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))
    from prep_flow_analyzer import FlowProfile, FlowOutput

try:
    from powerbi_import.prep_lineage import PrepLineageGraph, build_lineage_graph
except ImportError:
    try:
        from prep_lineage import PrepLineageGraph, build_lineage_graph
    except ImportError:
        PrepLineageGraph = None
        build_lineage_graph = None


# ═══════════════════════════════════════════════════════════════════════════════
#  DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class FlowToReportEdge:
    """A flow output that feeds a workbook datasource."""
    flow_name: str
    flow_output: str
    workbook_name: str
    datasource_name: str
    table_name: str
    match_type: str  # 'exact_table', 'connection_fp', 'published_ds', 'column_overlap', 'fuzzy_name'
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class WorkbookDatasourceInfo:
    """Summarized datasource metadata from a workbook extraction."""
    workbook_name: str
    datasource_name: str
    connection_type: str = ''
    server: str = ''
    database: str = ''
    tables: List[Dict[str, Any]] = field(default_factory=list)
    table_names: List[str] = field(default_factory=list)
    column_sets: Dict[str, Set[str]] = field(default_factory=dict)  # table → set of columns

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d['column_sets'] = {k: sorted(v) for k, v in self.column_sets.items()}
        return d


@dataclass
class FullLineageGraph:
    """Complete lineage: flows → flows → reports."""
    flow_graph: Optional[Any] = None  # PrepLineageGraph (flow→flow)
    flow_to_report_edges: List[FlowToReportEdge] = field(default_factory=list)
    orphan_flows: List[str] = field(default_factory=list)  # flows feeding no report
    report_external_sources: List[Dict[str, str]] = field(default_factory=list)  # reports skipping prep
    workbook_datasources: List[WorkbookDatasourceInfo] = field(default_factory=list)
    redundancy_clusters: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def total_flow_to_report_links(self) -> int:
        return len(self.flow_to_report_edges)

    @property
    def total_orphan_flows(self) -> int:
        return len(self.orphan_flows)

    def to_dict(self) -> dict:
        return {
            'flow_graph': self.flow_graph.to_dict() if self.flow_graph else None,
            'flow_to_report_edges': [e.to_dict() for e in self.flow_to_report_edges],
            'orphan_flows': self.orphan_flows,
            'report_external_sources': self.report_external_sources,
            'redundancy_clusters': self.redundancy_clusters,
            'summary': {
                'total_flow_to_report_links': self.total_flow_to_report_links,
                'total_orphan_flows': self.total_orphan_flows,
                'total_workbook_datasources': len(self.workbook_datasources),
            },
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  MATCHING HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _normalize(name: str) -> str:
    """Normalize for comparison: lowercase, strip, replace separators."""
    return name.lower().strip().replace(' ', '_').replace('-', '_').replace('.', '_')


def _connection_fingerprint(server: str, database: str, table: str) -> str:
    """SHA-256 fingerprint from connection coordinates."""
    raw = '\x00'.join(_normalize(p) for p in (server, database, table))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def _jaccard_columns(set_a: Set[str], set_b: Set[str]) -> float:
    """Column overlap score (0.0–1.0)."""
    if not set_a or not set_b:
        return 0.0
    na = {_normalize(c) for c in set_a}
    nb = {_normalize(c) for c in set_b}
    intersection = na & nb
    union = na | nb
    return len(intersection) / len(union) if union else 0.0


def _fuzzy_name_match(a: str, b: str) -> float:
    """Name similarity using substring and character overlap."""
    na, nb = _normalize(a), _normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.85
    # Character Jaccard
    sa, sb = set(na), set(nb)
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union) if union else 0.0


# ═══════════════════════════════════════════════════════════════════════════════
#  WORKBOOK DATASOURCE EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════════

def extract_workbook_datasource_info(
    workbook_name: str,
    datasources: List[Dict[str, Any]],
) -> List[WorkbookDatasourceInfo]:
    """Extract matching-relevant info from workbook datasources.json.

    Args:
        workbook_name: Name of the workbook.
        datasources: Parsed datasources.json list from extraction.

    Returns:
        List of WorkbookDatasourceInfo objects.
    """
    results = []
    for ds in datasources:
        conn = ds.get('connection', {})
        details = conn.get('details', {})
        tables = ds.get('tables', [])

        table_names = [t.get('name', '') for t in tables if t.get('name')]
        column_sets = {}
        for t in tables:
            tname = t.get('name', '')
            if tname:
                cols = {c.get('name', '') for c in t.get('columns', []) if c.get('name')}
                column_sets[tname] = cols

        info = WorkbookDatasourceInfo(
            workbook_name=workbook_name,
            datasource_name=ds.get('caption', ds.get('name', 'Unknown')),
            connection_type=conn.get('type', ''),
            server=details.get('server', ''),
            database=details.get('dbname', details.get('database', '')),
            tables=tables,
            table_names=table_names,
            column_sets=column_sets,
        )
        results.append(info)
    return results


# ═══════════════════════════════════════════════════════════════════════════════
#  FLOW→REPORT MATCHING
# ═══════════════════════════════════════════════════════════════════════════════

def match_flows_to_reports(
    profiles: List[FlowProfile],
    wb_datasources: List[WorkbookDatasourceInfo],
) -> List[FlowToReportEdge]:
    """Match flow outputs to workbook datasource tables.

    Uses a multi-pass strategy (exact → fingerprint → column overlap → fuzzy).

    Args:
        profiles: FlowProfile objects from prep_flow_analyzer.
        wb_datasources: WorkbookDatasourceInfo from workbook extractions.

    Returns:
        List of FlowToReportEdge connections.
    """
    edges: List[FlowToReportEdge] = []
    matched: Set[Tuple[str, str, str, str]] = set()  # (flow, output, wb, table)

    # Build index of flow outputs
    flow_outputs: List[Tuple[FlowProfile, FlowOutput]] = []
    for prof in profiles:
        for out in prof.outputs:
            flow_outputs.append((prof, out))

    # Pass 1: Exact table name match
    for prof, out in flow_outputs:
        out_table = _normalize(out.target_table or out.name)
        if not out_table:
            continue
        for wds in wb_datasources:
            for tname in wds.table_names:
                if _normalize(tname) == out_table:
                    key = (prof.name, out.name, wds.workbook_name, tname)
                    if key not in matched:
                        matched.add(key)
                        edges.append(FlowToReportEdge(
                            flow_name=prof.name,
                            flow_output=out.name,
                            workbook_name=wds.workbook_name,
                            datasource_name=wds.datasource_name,
                            table_name=tname,
                            match_type='exact_table',
                            confidence=1.0,
                        ))

    # Pass 2: Connection fingerprint match (same server+db+table)
    for prof, out in flow_outputs:
        fp_out = _connection_fingerprint(
            out.target_server or '',
            out.target_database or '',
            out.target_table or out.name,
        )
        for wds in wb_datasources:
            for tname in wds.table_names:
                fp_wb = _connection_fingerprint(wds.server, wds.database, tname)
                if fp_out == fp_wb:
                    key = (prof.name, out.name, wds.workbook_name, tname)
                    if key not in matched:
                        matched.add(key)
                        edges.append(FlowToReportEdge(
                            flow_name=prof.name,
                            flow_output=out.name,
                            workbook_name=wds.workbook_name,
                            datasource_name=wds.datasource_name,
                            table_name=tname,
                            match_type='connection_fp',
                            confidence=0.95,
                        ))

    # Pass 3: Column overlap (high Jaccard → same underlying table)
    for prof, out in flow_outputs:
        if not out.column_names:
            continue
        out_cols = set(out.column_names)
        for wds in wb_datasources:
            for tname, wb_cols in wds.column_sets.items():
                key = (prof.name, out.name, wds.workbook_name, tname)
                if key in matched:
                    continue
                score = _jaccard_columns(out_cols, wb_cols)
                if score >= 0.6:
                    matched.add(key)
                    edges.append(FlowToReportEdge(
                        flow_name=prof.name,
                        flow_output=out.name,
                        workbook_name=wds.workbook_name,
                        datasource_name=wds.datasource_name,
                        table_name=tname,
                        match_type='column_overlap',
                        confidence=round(score, 2),
                    ))

    # Pass 4: Fuzzy name match (last resort)
    for prof, out in flow_outputs:
        out_name = out.target_table or out.name
        if not out_name:
            continue
        for wds in wb_datasources:
            for tname in wds.table_names:
                key = (prof.name, out.name, wds.workbook_name, tname)
                if key in matched:
                    continue
                score = _fuzzy_name_match(out_name, tname)
                if score >= 0.75:
                    matched.add(key)
                    edges.append(FlowToReportEdge(
                        flow_name=prof.name,
                        flow_output=out.name,
                        workbook_name=wds.workbook_name,
                        datasource_name=wds.datasource_name,
                        table_name=tname,
                        match_type='fuzzy_name',
                        confidence=round(score, 2),
                    ))

    return edges


# ═══════════════════════════════════════════════════════════════════════════════
#  REDUNDANCY DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

def detect_redundant_sources(
    profiles: List[FlowProfile],
    wb_datasources: List[WorkbookDatasourceInfo],
) -> List[Dict[str, Any]]:
    """Find tables/sources that appear in multiple flows AND multiple reports.

    These are candidates for consolidation into a shared model.

    Returns:
        List of redundancy clusters: {table, flows: [...], workbooks: [...], type}
    """
    # Index: normalized table → set of (content_type, content_name)
    table_usage: Dict[str, Dict[str, Set[str]]] = {}  # table → {flows: set, reports: set}

    for prof in profiles:
        for inp in prof.inputs:
            tname = _normalize(inp.table_name or inp.name)
            if not tname:
                continue
            entry = table_usage.setdefault(tname, {'flows': set(), 'reports': set()})
            entry['flows'].add(prof.name)

        for out in prof.outputs:
            tname = _normalize(out.target_table or out.name)
            if not tname:
                continue
            entry = table_usage.setdefault(tname, {'flows': set(), 'reports': set()})
            entry['flows'].add(prof.name)

    for wds in wb_datasources:
        for tname in wds.table_names:
            norm = _normalize(tname)
            entry = table_usage.setdefault(norm, {'flows': set(), 'reports': set()})
            entry['reports'].add(wds.workbook_name)

    # Redundant = appears in 2+ flows OR 2+ reports
    clusters = []
    for table, usage in sorted(table_usage.items()):
        flow_count = len(usage['flows'])
        report_count = len(usage['reports'])
        if flow_count >= 2 or report_count >= 2 or (flow_count >= 1 and report_count >= 1):
            rtype = 'shared_across_all'
            if flow_count >= 2 and report_count >= 2:
                rtype = 'shared_across_all'
            elif flow_count >= 2:
                rtype = 'shared_across_flows'
            elif report_count >= 2:
                rtype = 'shared_across_reports'
            else:
                rtype = 'flow_to_report_link'

            clusters.append({
                'table': table,
                'flows': sorted(usage['flows']),
                'workbooks': sorted(usage['reports']),
                'flow_count': flow_count,
                'report_count': report_count,
                'type': rtype,
            })

    return clusters


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════════

def build_full_lineage(
    profiles: List[FlowProfile],
    workbook_extractions: Dict[str, List[Dict[str, Any]]],
) -> FullLineageGraph:
    """Build the complete lineage graph: flows→flows→reports.

    Args:
        profiles: List of FlowProfile objects from analyze_flow().
        workbook_extractions: {workbook_name: datasources_list} from extraction JSONs.

    Returns:
        FullLineageGraph with flow-to-flow, flow-to-report, redundancy info.
    """
    # Step 1: Build flow→flow lineage
    flow_graph = None
    if build_lineage_graph and len(profiles) >= 2:
        flow_graph = build_lineage_graph(profiles)

    # Step 2: Extract workbook datasource info
    all_wb_ds: List[WorkbookDatasourceInfo] = []
    for wb_name, datasources in workbook_extractions.items():
        all_wb_ds.extend(extract_workbook_datasource_info(wb_name, datasources))

    # Step 3: Match flows to reports
    flow_to_report_edges = match_flows_to_reports(profiles, all_wb_ds)

    # Step 4: Find orphan flows (outputs not linked to any report)
    linked_flows = {e.flow_name for e in flow_to_report_edges}
    orphan_flows = sorted(p.name for p in profiles if p.name not in linked_flows)

    # Step 5: Find reports with no prep flow source (direct DB connections)
    fed_tables: Set[Tuple[str, str]] = set()  # (workbook, table)
    for e in flow_to_report_edges:
        fed_tables.add((e.workbook_name, e.table_name))

    external_sources = []
    for wds in all_wb_ds:
        for tname in wds.table_names:
            if (wds.workbook_name, tname) not in fed_tables:
                external_sources.append({
                    'workbook': wds.workbook_name,
                    'datasource': wds.datasource_name,
                    'table': tname,
                    'connection_type': wds.connection_type,
                    'server': wds.server,
                })

    # Step 6: Detect redundancy
    redundancy = detect_redundant_sources(profiles, all_wb_ds)

    return FullLineageGraph(
        flow_graph=flow_graph,
        flow_to_report_edges=flow_to_report_edges,
        orphan_flows=orphan_flows,
        report_external_sources=external_sources,
        workbook_datasources=all_wb_ds,
        redundancy_clusters=redundancy,
    )


def print_full_lineage_summary(lineage: FullLineageGraph):
    """Print a console summary of the full lineage."""
    print()
    print("  ═══ FULL LINEAGE SUMMARY ═══")
    print()

    if lineage.flow_graph:
        fg = lineage.flow_graph
        print(f"  Prep Flows:              {fg.total_flows}")
        print(f"  Cross-flow edges:        {fg.total_cross_flow_edges}")
        print(f"  Flow chains:             {len(fg.chains)}")
        print(f"  Isolated flows:          {len(fg.isolated_flows)}")
        print()

    print(f"  Flow→Report links:       {lineage.total_flow_to_report_links}")
    print(f"  Orphan flows (no report): {lineage.total_orphan_flows}")
    print(f"  Report external sources:  {len(lineage.report_external_sources)}")
    print(f"  Redundancy clusters:      {len(lineage.redundancy_clusters)}")
    print()

    if lineage.flow_to_report_edges:
        print("  Flow → Report connections:")
        for e in lineage.flow_to_report_edges:
            conf = f" ({e.confidence:.0%})" if e.confidence < 1.0 else ""
            print(f"    {e.flow_name} → {e.workbook_name} [{e.table_name}]{conf}")
        print()

    if lineage.orphan_flows:
        print("  Orphan flows (output feeds no report):")
        for f in lineage.orphan_flows:
            print(f"    ⚠ {f}")
        print()

    if lineage.redundancy_clusters:
        shared = [c for c in lineage.redundancy_clusters if c['type'] == 'shared_across_all']
        if shared:
            print(f"  Consolidation candidates ({len(shared)} tables shared across flows + reports):")
            for c in shared[:10]:
                print(f"    • {c['table']} → {c['flow_count']} flows, {c['report_count']} reports")
            print()
