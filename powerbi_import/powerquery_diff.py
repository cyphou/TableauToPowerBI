"""Power Query / semantic-model table coverage — Tableau source tables and
columns vs the generated TMDL tables and their Power Query M (or Direct Lake
entity) partitions.

Complements ``visual_size_diff.py`` (visual geometry) with the data side of
migration fidelity: does every extracted table exist in the generated
semantic model, does every extracted column exist in that table, and does
the table actually have a data source (M partition or entity/calculated
partition) rather than being empty.

Public API:
    compare_report_tables(extracted, project_dir, report_name) -> dict
"""

from __future__ import annotations

import glob
import hashlib
import os
from typing import Dict, List, Optional

from powerbi_import.artifact_diff import _parse_tmdl_table

_EMPTY_HASH = hashlib.sha256(''.encode('utf-8')).hexdigest()[:16]


def _extracted_tables(extracted: Dict) -> Dict[str, set]:
    """Flatten every (table_name -> {column_name, ...}) across all datasources."""
    tables: Dict[str, set] = {}
    datasources = extracted.get('datasources', [])
    if isinstance(datasources, dict):
        datasources = datasources.get('datasources', [])
    for ds in datasources:
        for table in ds.get('tables', []):
            name = table.get('name', '')
            if not name:
                continue
            cols = {c.get('name', '') for c in table.get('columns', []) if c.get('name')}
            tables.setdefault(name, set()).update(cols)
    return tables


def _normalize_table_name(name: str) -> str:
    """Reduce a qualified/bracketed Tableau table name to a bare lowercase key."""
    return name.strip('[]').split('].[')[-1].strip("'\" ").lower()


def _load_generated_tables(semantic_model_dir: str) -> Dict[str, Dict]:
    tables = {}
    pattern = os.path.join(semantic_model_dir, 'definition', 'tables', '*.tmdl')
    for path in sorted(glob.glob(pattern)):
        parsed = _parse_tmdl_table(path)
        if parsed:
            tables[parsed['name']] = parsed
    return tables


def _find_generated_match(norm_name: str, generated_by_norm: Dict[str, Dict]) -> Optional[Dict]:
    match = generated_by_norm.get(norm_name)
    if match is not None:
        return match
    if not norm_name:
        return None
    for gnorm, gdata in generated_by_norm.items():
        if norm_name in gnorm or gnorm in norm_name:
            return gdata
    return None


def compare_report_tables(extracted: Dict, project_dir: str, report_name: str) -> Dict:
    """Compare extracted Tableau tables/columns to the generated TMDL tables."""
    semantic_model_dir = os.path.join(project_dir, f'{report_name}.SemanticModel')
    extracted_tables = _extracted_tables(extracted)
    generated_tables = _load_generated_tables(semantic_model_dir)
    generated_by_norm = {_normalize_table_name(name): data
                         for name, data in generated_tables.items()}

    results: List[Dict] = []
    for src_name, src_cols in extracted_tables.items():
        match = _find_generated_match(_normalize_table_name(src_name), generated_by_norm)
        if match is None:
            results.append({
                'table': src_name, 'found': False,
                'column_coverage_percent': 0.0,
                'missing_columns': sorted(src_cols),
                'has_source': False,
            })
            continue

        gen_col_names = {c['name'].lower() for c in match['columns']}
        missing = sorted(c for c in src_cols if c.lower() not in gen_col_names)
        covered = len(src_cols) - len(missing)
        coverage = round(covered / len(src_cols) * 100, 1) if src_cols else 100.0
        # A calculated/Direct-Lake entity partition is a valid data source too —
        # only flag as sourceless when the table has NO partition at all.
        has_source = bool(match.get('partitions'))
        results.append({
            'table': src_name, 'found': True,
            'column_coverage_percent': coverage,
            'missing_columns': missing,
            'has_source': has_source,
        })

    total = len(results)
    found = sum(1 for r in results if r['found'])
    sourceless = [r['table'] for r in results if r['found'] and not r['has_source']]
    avg_coverage = (round(sum(r['column_coverage_percent'] for r in results) / total, 1)
                   if total else 100.0)
    return {
        'report_name': report_name,
        'tables': results,
        'summary': {
            'source_tables': total,
            'tables_found': found,
            'table_match_percent': round(found / total * 100, 1) if total else 100.0,
            'avg_column_coverage_percent': avg_coverage,
            'sourceless_tables': sourceless,
        },
    }
