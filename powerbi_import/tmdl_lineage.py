"""Tableau → Power BI lineage mapping for the generated semantic model.

Extracted from ``tmdl_generator``, which re-exports these names. The block is
self-contained: it reads the finished model and the extracted datasources and
returns a lineage document, so it calls nothing back in the generator.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))
from datasource_extractor import sanitize_param_brackets  # noqa: E402


def _normalize_column(value):
    """Fold a column name to letters and digits for tolerant matching."""
    return re.sub(r'[^a-z0-9]', '', str(value).lower())


def _generated_table_names(tables, extra):
    """Tables the generator creates itself, which have no Tableau source table.

    Derived from what actually produced them rather than from their names: a
    What-If table is named after its parameter (``Base Salary``), so a
    name-prefix test reports every one of them as an orphan.
    """
    names = {'Calendar'}
    for param in extra.get('parameters', []) or []:
        caption = param.get('caption', '')
        if caption:
            names.add(sanitize_param_brackets(caption))
            names.add(f'{caption} FieldParam')
    for table in tables or []:
        for partition in table.get('partitions', []) or []:
            source = partition.get('source', {}) or {}
            if source.get('type') == 'calculationGroup':
                names.add(table.get('name', ''))
    return {name for name in names if name}


def _calculation_names(extra):
    """Lower-cased Tableau calculation names, for resolving calculated columns."""
    names = set()
    for calc in extra.get('calculations', []) or []:
        for key in (calc.get('caption', ''), calc.get('name', '')):
            clean = str(key).replace('[', '').replace(']', '').strip()
            if clean:
                names.add(clean.lower())
    return names


def _build_lineage_map(tables, relationships, extra_objects, datasources):
    """Build a lineage map tracking Tableau source → PBI target for every object.

    Returns:
        dict with 'tables', 'calculations', 'relationships', 'worksheets' lineage entries.
    """
    extra = extra_objects or {}
    lineage = {
        'contract': {
            'version': '1.1',
            'source': 'tableau_extraction',
            'target': 'powerbi_tmdl',
            'status': 'not_run',
            'coverage': {},
            'unresolved': [],
        },
        'tables': [],
        'columns': [],
        'calculations': [],
        'relationships': [],
        'worksheets': [],
        'filters': [],
        'parameters': [],
        'actions': [],
    }

    # Table lineage: Tableau datasource.table → PBI table. Keep the source
    # table index separate from the target names because collision handling can
    # rename a target table to ``Table (Datasource)``.
    source_tables = []
    ds_names = {}
    for ds in (datasources or []):
        ds_name = ds.get('name', '')
        for tbl in ds.get('tables', []):
            tname = tbl.get('name', '')
            if tname:
                ds_names[tname] = ds_name
                source_tables.append({
                    'datasource': ds_name,
                    'caption': ds.get('caption', ds_name),
                    'name': tname,
                    'columns': [
                        c.get('name', '') for c in tbl.get('columns', [])
                        if c.get('name')
                    ],
                })

    def _source_table_for(target_name):
        exact = [s for s in source_tables if s['name'] == target_name]
        if exact:
            return exact[0]
        renamed = [
            s for s in source_tables
            if target_name.startswith(s['name'] + ' (')
        ]
        return renamed[0] if len(renamed) == 1 else None

    def _source_column_for(source_table, target_column, declared_source=''):
        """Resolve a target column to its Tableau source column.

        Returns ``(column, status, owning_table)``. A join merges columns from
        several Tableau tables into one target table, so a column missing from
        its own source table is looked up across the others before being
        called unresolved.
        """
        if declared_source:
            return declared_source, 'declared', None
        if source_table:
            source_columns = source_table['columns']
            exact = next((c for c in source_columns if c == target_column), None)
            if exact:
                return exact, 'exact', None
            folded = next((c for c in source_columns
                           if c.lower() == target_column.lower()), None)
            if folded:
                return folded, 'inferred', None
            normalized = _normalize_column(target_column)
            normalized_match = next(
                (c for c in source_columns
                 if _normalize_column(c) == normalized), None)
            if normalized_match:
                return normalized_match, 'inferred', None

        for candidate in source_tables:
            if source_table and candidate is source_table:
                continue
            match = next((c for c in candidate['columns']
                          if c == target_column), None)
            if match:
                return match, 'inferred', candidate['name']

        return target_column, 'unresolved', None

    target_columns = {
        t.get('name', ''): {
            c.get('name', ''): c for c in t.get('columns', []) if c.get('name')
        }
        for t in (tables or []) if t.get('name')
    }

    generated_names = _generated_table_names(tables, extra)
    calculation_names = _calculation_names(extra)

    for t in (tables or []):
        pbi_name = t.get('name', '')
        if pbi_name:
            source_table = _source_table_for(pbi_name)
            source_name = source_table['name'] if source_table else pbi_name
            generated_table = pbi_name in generated_names
            lineage['tables'].append({
                'tableau_datasource': source_table['datasource'] if source_table else ds_names.get(pbi_name, ''),
                'tableau_table': source_name,
                'pbi_table': pbi_name,
                'source_status': 'exact' if source_table else ('generated' if generated_table else 'unresolved'),
            })
            for col in t.get('columns', []):
                column_name = col.get('name', '')
                if column_name:
                    source_column, source_status, owning_table = _source_column_for(
                        source_table, column_name, col.get('sourceColumn', ''))
                    if not source_table and generated_table:
                        source_status = 'generated'
                    elif (source_status == 'unresolved'
                            and column_name.lower() in calculation_names):
                        # A calculated column's source is a calculation, not a
                        # column of the source table.
                        source_status = 'calculated'
                    lineage['columns'].append({
                        'tableau_table': owning_table or source_name,
                        'tableau_column': source_column,
                        'pbi_table': pbi_name,
                        'pbi_column': column_name,
                        'source_status': source_status,
                        'source_kind': 'calculated' if col.get('type') == 'calculated' else 'physical',
                    })

    # Calculation lineage: Tableau calc → PBI measure or calculated column
    calcs = extra.get('calculations', [])
    calc_by_name = {}
    for calc in calcs:
        for key in (calc.get('caption', ''), calc.get('name', '')):
            clean = key.replace('[', '').replace(']', '')
            if clean:
                calc_by_name[clean.lower()] = calc
    for t in (tables or []):
        pbi_table = t.get('name', '')
        for m in t.get('measures', []):
            mname = m.get('name', '')
            source = calc_by_name.get(mname.lower())
            lineage['calculations'].append({
                'tableau_calculation': (source or {}).get('caption', (source or {}).get('name', mname)),
                'source_formula': (source or {}).get('formula', ''),
                'pbi_table': pbi_table,
                'pbi_object': mname,
                'pbi_type': 'measure',
                'source_status': 'exact' if source else 'generated',
            })
        for col in t.get('columns', []):
            if col.get('type') == 'calculated':
                cname = col.get('name', '')
                lineage['calculations'].append({
                    'tableau_calculation': cname,
                    'pbi_table': pbi_table,
                    'pbi_object': cname,
                    'pbi_type': 'calculatedColumn',
                    'source_status': 'exact',
                })

    # Relationship lineage
    for rel in (relationships or []):
        from_source = _source_table_for(rel.get('fromTable', ''))
        to_source = _source_table_for(rel.get('toTable', ''))
        from_target_col = target_columns.get(rel.get('fromTable', ''), {}).get(rel.get('fromColumn', {}), {})
        to_target_col = target_columns.get(rel.get('toTable', ''), {}).get(rel.get('toColumn', {}), {})
        from_column, from_status, _ = _source_column_for(
            from_source, rel.get('fromColumn', ''), from_target_col.get('sourceColumn', ''))
        to_column, to_status, _ = _source_column_for(
            to_source, rel.get('toColumn', ''), to_target_col.get('sourceColumn', ''))
        lineage['relationships'].append({
            'from': f"{rel.get('fromTable', '')}[{rel.get('fromColumn', '')}]",
            'to': f"{rel.get('toTable', '')}[{rel.get('toColumn', '')}]",
            'cardinality': rel.get('crossFilteringBehavior', rel.get('cardinality', '')),
            'tableau_from': f"{from_source['name'] if from_source else rel.get('fromTable', '')}[{from_column}]",
            'tableau_to': f"{to_source['name'] if to_source else rel.get('toTable', '')}[{to_column}]",
            'source_status': 'exact' if from_status != 'unresolved' and to_status != 'unresolved' else 'unresolved',
        })

    # Worksheet lineage (from extra_objects — key is '_worksheets' with
    # underscore prefix as set in pbip_generator.create_tmdl_model)
    worksheets = extra.get('_worksheets') or extra.get('worksheets') or []
    for ws in worksheets:
        ws_name = ws.get('name', '')
        if ws_name:
            lineage['worksheets'].append({
                'tableau_worksheet': ws_name,
                'pbi_page': ws_name,
            })

    # Report-behavior lineage: retain the source object identity even when
    # the generated report represents it through a different Power BI object.
    for key in ('filters', 'parameters', 'actions'):
        for index, obj in enumerate(extra.get(key, []) or []):
            name = obj.get('name') or obj.get('caption') or obj.get('field') or f'{key}_{index + 1}'
            identity = {'name': name}
            for field_name in ('field', 'type', 'worksheet'):
                if obj.get(field_name):
                    identity[field_name] = obj[field_name]
            lineage[key].append({
                'tableau_object': name,
                'object_type': key[:-1],
                'source_identity': identity,
                'source_status': 'exact',
            })

    # "Where did this come from?" is answered by a source, by the generator
    # having created it, or by a Tableau calculation — all three are resolved.
    _RESOLVED = ('exact', 'declared', 'inferred', 'generated', 'calculated')
    tracked = ('tables', 'columns', 'calculations', 'relationships')
    for key in tracked:
        records = lineage[key]
        resolved = sum(r.get('source_status') in _RESOLVED for r in records)
        lineage['contract']['coverage'][key] = {
            'target_count': len(records),
            'resolved_count': resolved,
            'percent': round(100 * resolved / len(records), 2) if records else 100.0,
        }
        lineage['contract']['unresolved'].extend(
            {'type': key, 'target': r.get('pbi_object') or r.get('pbi_column') or r.get('pbi_table') or r.get('from', '')}
            for r in records if r.get('source_status') == 'unresolved'
        )
    lineage['contract']['status'] = 'complete' if not lineage['contract']['unresolved'] else 'partial'

    return lineage
