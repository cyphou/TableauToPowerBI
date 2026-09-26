"""Tableau sets, groups and bins as Power BI calculated columns.

A set becomes a boolean IN column, a group a SWITCH column and a bin a FLOOR
column; each is emitted as a Power Query M step where possible and falls back to
DAX for cross-table references. Extracted from ``tmdl_generator``, which
re-exports these names.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))
from m_query_builder import m_transform_add_column  # noqa: E402

from powerbi_import.tmdl_m_conversion import (
    _dax_to_m_expression,
    _inject_m_steps_into_partition,
)


# ════════════════════════════════════════════════════════════════════
#  TABLEAU DERIVATION PREFIX CLEANING
#  Secondary defense against Tableau internal field names leaking
# ════════════════════════════════════════════════════════════════════

_RE_TMDL_DERIVATION_PREFIX = re.compile(
    r'^(none|sum|avg|count|min|max|usr|yr|mn|dy|qr|wk|attr|md|mdy|hms|hr|mt|sc|thr|trunc|tyr|tqr|tmn|tdy|twk):'
)
_RE_TMDL_TYPE_SUFFIX = re.compile(r':(nk|qk|ok|fn|tn)$')


def _clean_tableau_field_ref(raw):
    """Strip Tableau derivation prefixes and type suffixes from a field name.

    Defensive secondary filter applied in the TMDL generator to catch any
    Tableau internal names that leaked through extraction.
    """
    clean = _RE_TMDL_DERIVATION_PREFIX.sub('', raw)
    return _RE_TMDL_TYPE_SUFFIX.sub('', clean)


def _process_sets_groups_bins(model, extra_objects, main_table_name, column_table_map):
    """Add sets, groups and bins as Power Query M columns (fallback: DAX calc cols)."""
    if not main_table_name:
        return

    main_table = None
    for table in model["model"]["tables"]:
        if table.get("name") == main_table_name:
            main_table = table
            break
    if not main_table:
        return

    existing_cols = {col.get("name", "") for col in main_table.get("columns", [])}
    m_steps_by_table = {}   # table name -> accumulated M steps

    def _steps_for(table_name):
        return m_steps_by_table.setdefault(table_name, [])

    def _owner_of(source_field):
        """Table that owns *source_field*.

        A group or bin is a row-level recode of its source column, so it has
        to live in that column's own table: a calculated column cannot read a
        bare column from another table, and Power BI rejects the model with
        "a single value for column X cannot be determined".
        """
        name = column_table_map.get(source_field, main_table_name)
        if name != main_table_name:
            for table in model["model"]["tables"]:
                if table.get("name") == name:
                    return table, name
        return main_table, main_table_name

    m_steps = _steps_for(main_table_name)

    # Sets -> boolean column
    for s in extra_objects.get('sets', []):
        set_name = s.get('name', '')
        if not set_name or set_name in existing_cols:
            continue

        members = s.get('members', [])
        formula = s.get('formula', '')

        if formula:
            dax_expr = formula
        elif members:
            escaped = [f'"{m.replace(chr(34), chr(34)+chr(34))}"' for m in members[:50]]
            dax_expr = f"'{main_table_name}'[{set_name}] IN {{{', '.join(escaped)}}}"
        else:
            dax_expr = 'TRUE()'

        m_expr = _dax_to_m_expression(dax_expr, main_table_name)
        if m_expr is not None:
            m_steps.append(m_transform_add_column(set_name, f'each {m_expr}', 'type logical'))
            main_table["columns"].append({
                "name": set_name,
                "dataType": "Boolean",
                "sourceColumn": set_name,
                "summarizeBy": "none",
                "displayFolder": "Sets"
            })
        else:
            main_table["columns"].append({
                "name": set_name,
                "dataType": "Boolean",
                "expression": dax_expr,
                "summarizeBy": "none",
                "isCalculated": True,
                "displayFolder": "Sets"
            })
        existing_cols.add(set_name)

    # Groups -> SWITCH / concatenation column
    for g in extra_objects.get('groups', []):
        group_name = g.get('name', '')
        if not group_name or group_name in existing_cols:
            continue

        group_type = g.get('group_type', 'values')
        members = g.get('members', {})
        source_field = g.get('source_field', '').replace('[', '').replace(']', '')
        source_fields = g.get('source_fields', [])

        if group_type == 'combined' and source_fields:
            calc_map_lookup = {}
            # Also build internal-name → caption mapping from datasource columns
            col_caption_map = {}
            for ds in extra_objects.get('_datasources', []):
                for calc in ds.get('calculations', []):
                    raw = calc.get('name', '').replace('[', '').replace(']', '')
                    cap = (calc.get('caption', '') or raw).replace('[', '').replace(']', '')
                    calc_map_lookup[raw] = cap
                for tbl in ds.get('tables', []):
                    for col_info in tbl.get('columns', []):
                        col_name = col_info.get('name', '').replace('[', '').replace(']', '')
                        col_cap = col_info.get('caption', '')
                        if col_cap and col_name != col_cap:
                            col_caption_map[col_name] = col_cap
            # Also resolve from existing_cols (columns already in the BIM table)
            for table_obj in model.get('model', {}).get('tables', []):
                for col in table_obj.get('columns', []):
                    col_name = col.get('name', '')
                    src_col = col.get('sourceColumn', '')
                    if src_col and src_col != col_name:
                        col_caption_map[src_col] = col_name
            for table_obj in model.get('model', {}).get('tables', []):
                for col in table_obj.get('columns', []):
                    if col.get('isCalculated'):
                        col_name = col.get('name', '')
                        if col_name and col_name not in column_table_map:
                            column_table_map[col_name] = table_obj.get('name', main_table_name)
                for meas in table_obj.get('measures', []):
                    meas_name = meas.get('name', '')
                    if meas_name and meas_name not in column_table_map:
                        column_table_map[meas_name] = table_obj.get('name', main_table_name)

            # Date-part derivation prefix → M date function mapping
            _DATE_PART_M_FUNC = {
                'yr': 'Date.Year', 'tyr': 'Date.Year',
                'mn': 'Date.Month', 'tmn': 'Date.Month',
                'dy': 'Date.Day', 'tdy': 'Date.Day',
                'qr': 'Date.QuarterOfYear', 'tqr': 'Date.QuarterOfYear',
                'wk': 'Date.WeekOfYear', 'twk': 'Date.WeekOfYear',
                'hr': 'Time.Hour', 'mt': 'Time.Minute', 'sc': 'Time.Second',
            }
            # Also map function names from group name (e.g. YEAR, MONTH)
            _FUNC_NAME_M = {
                'YEAR': 'Date.Year', 'MONTH': 'Date.Month', 'DAY': 'Date.Day',
                'QUARTER': 'Date.QuarterOfYear', 'WEEK': 'Date.WeekOfYear',
                'HOUR': 'Time.Hour', 'MINUTE': 'Time.Minute', 'SECOND': 'Time.Second',
            }
            # Parse group name to extract function wrappers per position
            # e.g. "Action (Category,YEAR(Order Date),MONTH(Order Date))"
            #  → [None, 'Date.Year', 'Date.Month']
            name_func_map = []
            _gn_match = re.match(r'^.*?\((.+)\)\s*$', group_name)
            if _gn_match:
                _gn_inner = _gn_match.group(1)
                _gn_parts, _gn_depth, _gn_cur = [], 0, []
                for _ch in _gn_inner:
                    if _ch == '(':
                        _gn_depth += 1
                        _gn_cur.append(_ch)
                    elif _ch == ')':
                        _gn_depth -= 1
                        _gn_cur.append(_ch)
                    elif _ch == ',' and _gn_depth == 0:
                        _gn_parts.append(''.join(_gn_cur).strip())
                        _gn_cur = []
                    else:
                        _gn_cur.append(_ch)
                _gn_parts.append(''.join(_gn_cur).strip())
                for _gp in _gn_parts:
                    _fm = re.match(r'^(YEAR|MONTH|DAY|QUARTER|WEEK|HOUR|MINUTE|SECOND)\(', _gp, re.IGNORECASE)
                    name_func_map.append(_FUNC_NAME_M.get(_fm.group(1).upper()) if _fm else None)

            m_parts = []
            dax_parts = []
            for idx, sf_raw in enumerate(source_fields):
                # 1. Extract derivation prefix before cleaning
                prefix_match = _RE_TMDL_DERIVATION_PREFIX.match(sf_raw)
                date_prefix = prefix_match.group(1) if prefix_match else None
                # 2. Clean and resolve field name
                sf = _clean_tableau_field_ref(sf_raw)
                resolved = calc_map_lookup.get(sf, sf)
                resolved = _clean_tableau_field_ref(resolved)
                # Resolve internal Tableau field name to caption (e.g. "Postal Code" → "Code postal")
                resolved = col_caption_map.get(resolved, resolved)
                # Also check if the resolved name exists in existing columns
                if resolved not in existing_cols and sf in col_caption_map:
                    resolved = col_caption_map[sf]
                # Validate: skip fields that don't exist in any known column set
                if resolved not in existing_cols and resolved not in column_table_map:
                    print(f"  ⚠ Group '{group_name}': skipping unknown source field '{sf_raw}' (resolved='{resolved}')")
                    continue
                # 3. Build M column reference
                escaped_m = resolved.replace('"', '""')
                m_ref = f'[#"{escaped_m}"]'
                # 4. Apply date-part function: first from derivation prefix, then from group name
                m_func = None
                if date_prefix and date_prefix in _DATE_PART_M_FUNC:
                    m_func = _DATE_PART_M_FUNC[date_prefix]
                elif idx < len(name_func_map) and name_func_map[idx]:
                    m_func = name_func_map[idx]
                if m_func:
                    m_ref = f'{m_func}({m_ref})'
                # 5. Wrap in Text.From() for safe text concatenation
                m_ref = f'Text.From({m_ref})'
                m_parts.append(m_ref)
                # Also build DAX parts for fallback
                table_ref = column_table_map.get(resolved, column_table_map.get(sf, main_table_name))
                escaped_col = resolved.replace(']', ']]')
                ref = f"'{table_ref}'[{escaped_col}]"
                if table_ref != main_table_name:
                    ref = f"RELATED({ref})"
                dax_parts.append(ref)

            if not m_parts:
                # All source fields were unknown — skip this group entirely
                print(f"  ⚠ Group '{group_name}': no valid source fields found, skipping")
                continue
            # Build M expression directly (type-safe concatenation)
            if len(m_parts) == 1:
                m_concat_expr = m_parts[0]
            else:
                m_concat_expr = ' & " | " & '.join(m_parts)
            m_steps.append(m_transform_add_column(group_name, f'each {m_concat_expr}', 'type text'))
            main_table["columns"].append({
                "name": group_name,
                "dataType": "String",
                "sourceColumn": group_name,
                "summarizeBy": "none",
                "displayFolder": "Groups"
            })
            existing_cols.add(group_name)
            continue

        elif members and source_field:
            owner_table, owner_name = _owner_of(source_field)
            if group_name in {c.get("name", "") for c in owner_table.get("columns", [])}:
                continue
            total_values = sum(len(v) for v in members.values())
            # Large groups: use M table-join lookup (avoids M engine complexity limit)
            if total_values > 100:
                escaped_src = source_field.replace('"', '""')
                escaped_grp = group_name.replace('"', '""')
                rows = []
                for label, values in members.items():
                    el = label.replace('"', '""')
                    for val in values:
                        ev = val.replace('"', '""')
                        rows.append(f'{{"{ev}", "{el}"}}')
                map_expr = (
                    f'#table(type table [key = text, grp = text], '
                    f'{{{", ".join(rows)}}})'
                )
                safe_tag = re.sub(r'[^A-Za-z0-9_]', '_', group_name)
                owner_steps = _steps_for(owner_name)
                owner_steps.append((
                    f'#"Join_{safe_tag}"',
                    f'Table.NestedJoin({{prev}}, {{"{escaped_src}"}}, {map_expr}, {{"key"}}, "_lkp_{safe_tag}", JoinKind.LeftOuter)'
                ))
                owner_steps.append((
                    f'#"Expand_{safe_tag}"',
                    f'Table.ExpandTableColumn({{prev}}, "_lkp_{safe_tag}", {{"grp"}}, {{"{escaped_grp}"}})'
                ))
                owner_steps.append((
                    f'#"Fill_{safe_tag}"',
                    f'Table.ReplaceValue({{prev}}, null, "Other", Replacer.ReplaceValue, {{"{escaped_grp}"}})'
                ))
                owner_table["columns"].append({
                    "name": group_name,
                    "dataType": "String",
                    "sourceColumn": group_name,
                    "summarizeBy": "none",
                    "displayFolder": "Groups"
                })
                existing_cols.add(group_name)
                continue

            table_ref = owner_name
            cases = []
            for label, values in members.items():
                escaped_label = label.replace('"', '""')
                for val in values:
                    escaped_val = val.replace('"', '""')
                    cases.append(f'"{escaped_val}", "{escaped_label}"')

            if cases:
                dax_expr = f"SWITCH('{table_ref}'[{source_field}], {', '.join(cases)}, \"Other\")"
            else:
                dax_expr = f"'{table_ref}'[{source_field}]"
            group_target, group_target_name = owner_table, owner_name
        else:
            dax_expr = '""'
            group_target, group_target_name = main_table, main_table_name

        m_expr = _dax_to_m_expression(dax_expr, group_target_name)
        if m_expr is not None:
            _steps_for(group_target_name).append(
                m_transform_add_column(group_name, f'each {m_expr}', 'type text'))
            group_target["columns"].append({
                "name": group_name,
                "dataType": "String",
                "sourceColumn": group_name,
                "summarizeBy": "none",
                "displayFolder": "Groups"
            })
        else:
            group_target["columns"].append({
                "name": group_name,
                "dataType": "String",
                "expression": dax_expr,
                "summarizeBy": "none",
                "isCalculated": True,
                "displayFolder": "Groups"
            })
        existing_cols.add(group_name)

    # Bins -> FLOOR column
    for b in extra_objects.get('bins', []):
        bin_name = b.get('name', '')
        if not bin_name or bin_name in existing_cols:
            continue

        source_field = b.get('source_field', '').replace('[', '').replace(']', '')
        bin_size = b.get('size', '10')

        if source_field:
            bin_target, table_ref = _owner_of(source_field)
            dax_expr = f"FLOOR('{table_ref}'[{source_field}], {bin_size})"
        else:
            bin_target, table_ref = main_table, main_table_name
            dax_expr = '0'
        if bin_name in {c.get("name", "") for c in bin_target.get("columns", [])}:
            continue

        m_expr = _dax_to_m_expression(dax_expr, table_ref)
        if m_expr is not None:
            _steps_for(table_ref).append(
                m_transform_add_column(bin_name, f'each {m_expr}', 'type number'))
            bin_target["columns"].append({
                "name": bin_name,
                "dataType": "Double",
                "sourceColumn": bin_name,
                "summarizeBy": "none",
                "displayFolder": "Bins"
            })
        else:
            bin_target["columns"].append({
                "name": bin_name,
                "dataType": "Double",
                "expression": dax_expr,
                "summarizeBy": "none",
                "isCalculated": True,
                "displayFolder": "Bins"
            })
        existing_cols.add(bin_name)

    # Inject accumulated M steps into each table's own partition
    for table_name, steps in m_steps_by_table.items():
        if not steps:
            continue
        target = main_table
        if table_name != main_table_name:
            for table in model["model"]["tables"]:
                if table.get("name") == table_name:
                    target = table
                    break
        _inject_m_steps_into_partition(target, steps)
