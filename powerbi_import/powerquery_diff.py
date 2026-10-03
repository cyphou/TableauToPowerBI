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
import re
from collections import Counter
from typing import Dict, List, Optional

from powerbi_import.artifact_diff import _parse_tmdl_table
from tableau_export.dax_converter import TABLEAU_TO_PBI_TYPE

_EMPTY_HASH = hashlib.sha256(''.encode('utf-8')).hexdigest()[:16]


def _extracted_tables(extracted: Dict) -> Dict[tuple, set]:
    """Keep source tables separate across Tableau datasources."""
    tables: Dict[tuple, set] = {}
    datasources = extracted.get('datasources', [])
    if isinstance(datasources, dict):
        datasources = datasources.get('datasources', [])
    for ds in datasources:
        for table in ds.get('tables', []):
            name = table.get('name', '')
            if not name:
                continue
            cols = {c.get('name', '') for c in table.get('columns', []) if c.get('name')}
            tables.setdefault((ds.get('name', ''), name), set()).update(cols)
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
    occurrences = Counter(_normalize_table_name(name) for _ds, name in extracted_tables)
    claimed_originals = set()
    datasources = {ds.get('name', ''): ds for ds in extracted.get('datasources', [])}

    results: List[Dict] = []
    for (ds_name, src_name), src_cols in extracted_tables.items():
        norm_name = _normalize_table_name(src_name)
        if occurrences[norm_name] > 1:
            datasource = datasources.get(ds_name, {})
            label = datasource.get('caption') or ds_name
            label = label.replace('[', '').replace(']', '')
            if label == ds_name and label.startswith('federated.'):
                label = label.replace('federated.', '', 1)[:8]
            renamed = generated_by_norm.get(_normalize_table_name(f'{src_name} ({label})'))
            if renamed is not None:
                match = renamed
            elif norm_name not in claimed_originals:
                claimed_originals.add(norm_name)
                match = generated_by_norm.get(norm_name)
            else:
                match = None
        else:
            match = _find_generated_match(norm_name, generated_by_norm)
        if match is None:
            results.append({
                'table': src_name, 'datasource': ds_name, 'found': False,
                'column_coverage_percent': 0.0,
                'missing_columns': sorted(src_cols),
                'has_source': False,
            })
            continue

        generated_names = {name for col in match['columns']
                   for name in (col['name'], col.get('sourceColumn')) if name}
        generated_folded = {name.casefold() for name in generated_names}
        source_counts = Counter(name.casefold() for name in src_cols)
        missing = sorted(c for c in src_cols
                 if c not in generated_names and
                 (source_counts[c.casefold()] > 1 or
                  c.casefold() not in generated_folded))
        covered = len(src_cols) - len(missing)
        coverage = round(covered / len(src_cols) * 100, 1) if src_cols else 100.0
        # A calculated/Direct-Lake entity partition is a valid data source too —
        # only flag as sourceless when the table has NO partition at all.
        has_source = bool(match.get('partitions'))
        results.append({
            'table': src_name, 'datasource': ds_name, 'found': True,
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


def compare_report_counts(extracted: Dict, project_dir: str, report_name: str,
                          table_comparison: Optional[Dict] = None,
                          calculation_comparison: Optional[Dict] = None) -> Dict:
    """Count physical fields and declarations separately from generated model objects."""
    source_tables = _extracted_tables(extracted)
    model = _load_generated_tables(os.path.join(project_dir, f'{report_name}.SemanticModel'))
    table_comparison = table_comparison or compare_report_tables(extracted, project_dir, report_name)
    calculation_comparison = calculation_comparison or compare_calculation_metadata(
        extracted, project_dir, report_name)
    source_measure_records = [record for record in calculation_comparison['calculations']
                              if record['source_role'] == 'measure']
    return {
        'tableau': {
            'tables': len(source_tables),
            'physical_columns': sum(len(columns) for columns in source_tables.values()),
            'measure_calculations': len(source_measure_records),
        },
        'powerbi': {
            'tables': len(model),
            'physical_columns': sum(bool(col.get('sourceColumn'))
                                    for table in model.values() for col in table['columns']),
            'calculated_columns': sum(not col.get('sourceColumn')
                                      for table in model.values() for col in table['columns']),
            'measures': sum(len(table['measures']) for table in model.values()),
        },
        'source_to_target': {
            'tables_found': table_comparison['summary']['tables_found'],
            'physical_columns_found': sum(
                len(source_tables.get((row['datasource'], row['table']), ())) -
                len(row['missing_columns']) for row in table_comparison['tables']),
            'measure_calculations_as_measures': sum(
                record['status'] == 'measure_declared' for record in source_measure_records),
            'measure_calculations_as_columns': sum(
                record['status'] == 'column_declared' for record in source_measure_records),
            'measure_calculations_unresolved': sum(
                record['status'] not in ('measure_declared', 'column_declared')
                for record in source_measure_records),
        },
    }


def compare_report_metadata(extracted: Dict, project_dir: str, report_name: str) -> Dict:
    """Compare declared physical-column types with exact model/sourceColumn links."""
    model_dir = os.path.join(project_dir, f'{report_name}.SemanticModel')
    generated = _load_generated_tables(model_dir)
    by_name = {_normalize_table_name(name): data for name, data in generated.items()}
    table_occurrences = {}
    for datasource in extracted.get('datasources', []):
        for table in datasource.get('tables', []):
            norm = _normalize_table_name(table.get('name', ''))
            table_occurrences[norm] = table_occurrences.get(norm, 0) + 1
    relationship_keys = set()
    relations = os.path.join(model_dir, 'definition', 'relationships.tmdl')
    if os.path.isfile(relations):
        with open(relations, encoding='utf-8') as stream:
            for table, column in re.findall(
                r'(?m)^\s*(?:fromColumn|toColumn):\s*'
                r"('(?:[^']|'')+'|[^.'\s]+)\.('(?:[^']|'')+'|[^'\s]+)\s*$",
                stream.read(),
            ):
                relationship_keys.add((_normalize_table_name(table.replace("''", "'")),
                                       column.strip("'").replace("''", "'").casefold()))
    records = []
    seen = set()
    claimed_originals = set()
    for datasource in extracted.get('datasources', []):
        for table in datasource.get('tables', []):
            source_table = table.get('name', '')
            norm_table = _normalize_table_name(source_table)
            target = by_name.get(norm_table)
            source_name_counts = Counter(col.get('name', '').strip('[]').casefold()
                                         for col in table.get('columns', [])
                                         if col.get('name') and not col.get('calculation'))
            if table_occurrences.get(norm_table, 0) > 1:
                label = datasource.get('caption') or datasource.get('name', '')
                label = label.replace('[', '').replace(']', '')
                if label == datasource.get('name') and label.startswith('federated.'):
                    label = label.replace('federated.', '', 1)[:8]
                renamed = by_name.get(_normalize_table_name(f'{source_table} ({label})'))
                if renamed is not None:
                    target = renamed
                elif target is not None and norm_table not in claimed_originals:
                    claimed_originals.add(norm_table)
                else:
                    target = None
            for column in table.get('columns', []):
                if column.get('calculation') or not column.get('name'):
                    continue
                source_name = column['name'].strip('[]')
                datatype = str(column.get('datatype') or '').lower()
                identity = (source_table.casefold(), source_name, datatype)
                if identity in seen:
                    continue
                seen.add(identity)
                status = 'unverified_table'
                actual = None
                if target is not None:
                    matches = [item for item in target['columns']
                               if (item.get('sourceColumn') or item['name']) == source_name]
                    if not matches and source_name_counts[source_name.casefold()] == 1:
                        matches = [item for item in target['columns']
                                   if (item.get('sourceColumn') or item['name']).casefold()
                                   == source_name.casefold()]
                    if not matches and source_name_counts[source_name.casefold()] == 1:
                        matches = [item for item in target['columns']
                                   if item['name'].casefold() == source_name.casefold()]
                    if not matches:
                        base = re.sub(r'\s*\([^()]*\)$', '', source_name)
                        if base != source_name:
                            matches = [item for item in target['columns']
                                       if (item.get('sourceColumn') or item['name']).casefold()
                                       == base.casefold()]
                    if len(matches) == 1:
                        actual = matches[0].get('dataType')
                        expected = TABLEAU_TO_PBI_TYPE.get(datatype)
                        key = (_normalize_table_name(target['name']),
                               (matches[0].get('sourceColumn') or matches[0]['name']).casefold())
                        if actual and expected and actual.casefold() == expected.casefold():
                            status = 'type_match'
                        elif (datatype in ('integer', 'real', 'number') and actual
                            and actual.casefold() == 'string'
                              and key in relationship_keys):
                            status = 'relationship_key_type_review'
                        elif actual and expected:
                            status = 'type_mismatch'
                        else:
                            status = 'datatype_not_checked'
                    else:
                        status = 'unmatched_column' if not matches else 'ambiguous_column'
                records.append({'table': source_table, 'column': source_name,
                                'source_datatype': datatype or None,
                                'target_datatype': actual, 'status': status})
    counts = {status: sum(row['status'] == status for row in records)
              for status in ('type_match', 'type_mismatch', 'relationship_key_type_review',
                             'datatype_not_checked',
                             'unverified_table', 'unmatched_column', 'ambiguous_column')}
    return {'summary': counts, 'columns': records}


def compare_calculation_metadata(extracted: Dict, project_dir: str, report_name: str) -> Dict:
    """Identify where Tableau calculations are declared, not whether DAX/M is equivalent."""
    model_dir = os.path.join(project_dir, f'{report_name}.SemanticModel')
    model = _load_generated_tables(model_dir)
    records = []
    for calc in extracted.get('calculations', []):
        if not isinstance(calc, dict):
            continue
        name = calc.get('caption') or str(calc.get('name', '')).strip('[]')
        if not name:
            continue
        matches = [(table_name, kind, item) for table_name, table in model.items()
                   for kind in ('columns', 'measures') for item in table[kind]
                   if item['name'] == name]
        if not matches:
            matches = [(table_name, kind, item) for table_name, table in model.items()
                       for kind in ('columns', 'measures') for item in table[kind]
                       if item['name'].casefold() == name.casefold()]
        datatype = str(calc.get('datatype') or '').lower()
        expected = TABLEAU_TO_PBI_TYPE.get(datatype)
        if not matches:
            status = 'missing_symbol'
        elif len(matches) > 1:
            status = 'ambiguous_symbol'
        elif matches[0][1] == 'columns' and expected and matches[0][2].get('dataType') and (
                expected.casefold() != matches[0][2]['dataType'].casefold()):
            status = 'type_mismatch'
        else:
            status = 'column_declared' if matches[0][1] == 'columns' else 'measure_declared'
        records.append({'calculation': name, 'source_role': calc.get('role'),
                        'source_datatype': datatype or None,
                        'target_table': matches[0][0] if len(matches) == 1 else None,
                        'target_kind': matches[0][1] if len(matches) == 1 else None,
                        'status': status, 'expression_equivalence': 'not_verified'})
    counts = {status: sum(row['status'] == status for row in records)
              for status in ('column_declared', 'measure_declared', 'missing_symbol',
                             'ambiguous_symbol', 'type_mismatch')}
    return {'summary': counts, 'calculations': records}


def compare_caption_metadata(extracted: Dict, project_dir: str, report_name: str) -> Dict:
    """Check whether explicit Tableau captions survive in model symbols."""
    model_dir = os.path.join(project_dir, f'{report_name}.SemanticModel')
    tables = _load_generated_tables(model_dir)
    records = []
    seen = set()
    for datasource in extracted.get('datasources', []):
        for column in datasource.get('columns', []):
            caption = column.get('caption')
            if (not caption or not column.get('name') or column.get('calculation')
                    or column['name'].startswith('[:')):
                continue
            identity = (datasource.get('name'), column['name'], caption)
            if identity in seen:
                continue
            seen.add(identity)
            source_name = column['name'].strip('[]')
            owners = {_normalize_table_name(table.get('name', ''))
                      for table in datasource.get('tables', [])
                      if any(col.get('name', '').strip('[]') == source_name
                             for col in table.get('columns', []))}
            candidates = (item for name, table in tables.items()
                          if any(_normalize_table_name(name) == owner or
                                 _normalize_table_name(name).startswith(owner + ' (')
                                 for owner in owners)
                          for item in table['columns'])
            retained = any(item['name'].casefold() == caption.casefold() and
                           (item.get('sourceColumn') or item['name']) in (source_name, caption)
                           for item in candidates)
            records.append({'datasource': datasource.get('name'),
                            'source_column': column['name'], 'caption': caption,
                            'status': ('retained_in_model' if retained
                                       else 'not_found_in_model')})
    return {'summary': {
        'retained_in_model': sum(r['status'] == 'retained_in_model' for r in records),
        'not_found_in_model': sum(r['status'] == 'not_found_in_model' for r in records),
    }, 'captions': records}


def compare_semantic_metadata(extracted: Dict, project_dir: str, report_name: str) -> Dict:
    """Check only Tableau-declared semantic roles and hidden flags."""
    from powerbi_import.tmdl_generator import _map_semantic_role_to_category

    model_dir = os.path.join(project_dir, f'{report_name}.SemanticModel')
    tables = _load_generated_tables(model_dir)
    all_columns = [(table_name, column) for table_name, table in tables.items()
                   for column in table['columns']]
    records = []
    seen = set()
    for datasource in extracted.get('datasources', []):
        physical = {column.get('name', '').strip('[]').casefold()
                    for table in datasource.get('tables', [])
                    for column in table.get('columns', []) if column.get('name')}
        for source in datasource.get('columns', []):
            role = source.get('semantic_role')
            hidden = True if str(source.get('hidden', '')).lower() == 'true' else None
            if not role and hidden is None:
                continue
            name = source.get('name', '').strip('[]')
            if (not name or name.casefold() not in physical or source.get('calculation')
                    or source['name'].startswith('[:')):
                continue
            identity = (datasource.get('name'), name, role, hidden)
            if identity in seen:
                continue
            seen.add(identity)
            owners = [table.get('name') for table in datasource.get('tables', [])
                      if any(col.get('name', '').strip('[]') == name
                             for col in table.get('columns', []))]
            if not owners:
                owners = [table.get('name') for table in datasource.get('tables', [])
                          if any(col.get('name', '').strip('[]').casefold() == name.casefold()
                                 for col in table.get('columns', []))]
            matches = [(table, col) for table, col in all_columns
                       if _normalize_table_name(table) in {
                           _normalize_table_name(owner) for owner in owners}
                       if name.casefold() in (col['name'].casefold(),
                                              (col.get('sourceColumn') or '').casefold())]
            physical_matches = [(table, col) for table, col in matches
                                if col.get('sourceColumn') == name]
            if physical_matches:
                matches = physical_matches
            expected_category = (_map_semantic_role_to_category(role, name,
                                  source.get('datatype')) if role else None)
            categories = sorted({col.get('dataCategory') for _table, col in matches
                                 if col.get('dataCategory')})
            if role:
                category_status = ('unmapped_role' if not expected_category
                                   else 'column_not_found' if not matches
                                   else 'category_match' if all(
                                       col.get('dataCategory') == expected_category
                                       for _table, col in matches)
                                   else 'category_mismatch')
            else:
                category_status = 'not_declared'
            if hidden is not None:
                target_hidden = str(hidden).lower() == 'true'
                hidden_status = ('column_not_found' if not matches else
                                 'hidden_match' if all(bool(col.get('isHidden')) == target_hidden
                                                       for _table, col in matches)
                                 else 'hidden_mismatch')
            else:
                hidden_status = 'not_declared'
            records.append({'column': name, 'source_role': role,
                            'expected_category': expected_category,
                            'target_categories': categories,
                            'category_status': category_status,
                            'hidden_status': hidden_status})
    return {'summary': {
        status: sum(row['category_status'] == status or row['hidden_status'] == status
                    for row in records)
        for status in ('category_match', 'category_mismatch', 'unmapped_role',
                       'column_not_found', 'hidden_match', 'hidden_mismatch')
    }, 'columns': records}
