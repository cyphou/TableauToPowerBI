"""Tableau sets, groups and bins as Power BI calculated columns.

A set becomes a boolean IN column, a group a SWITCH column and a bin a FLOOR
column; each is emitted as a Power Query M step where possible and falls back to
DAX for cross-table references. Extracted from ``tmdl_generator``, which
re-exports these names.
"""

import hashlib
import json
import logging
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))
from extract_primitives import (  # noqa: E402
    _RE_DERIVATION_PREFIX,
    _RE_TABLE_CALC_PREFIX,
    _clean_field_ref,
)
from m_query_builder import _m_header_literal, m_transform_add_column  # noqa: E402

from powerbi_import.tmdl_m_conversion import (
    _dax_to_m_expression,
    _inject_m_steps_into_partition,
)

logger = logging.getLogger(__name__)

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


def _combined_group_identity(group):
    identity = {
        key: group.get(key) for key in ('name', 'raw_name', 'datasource', 'source_fields')
    }
    if 'source_field_refs' in group:
        identity['source_field_refs'] = group['source_field_refs']
    encoded = json.dumps(identity, sort_keys=True, ensure_ascii=True).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


_COMBINED_GROUP_CONSUMER_KEYS = frozenset({
    '_datasources', 'datasources', '_worksheets', 'worksheets', 'calculations',
    'filters', 'actions', 'dashboards', 'user_filters', 'datasource_filters',
    'sort_orders', 'sets', 'bins', 'parameters', 'aliases', 'data_blending',
    'custom_sql', 'table_extensions', 'published_datasources', 'stories',
    'hierarchies', 'custom_geocoding',
})


def _iter_nested_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _iter_nested_strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _iter_nested_strings(item)


def _is_non_restrictive_filter(filter_data):
    return (
        isinstance(filter_data, dict)
        and filter_data.get('type') in ('all', '', None)
        and not filter_data.get('values')
        and filter_data.get('min') is None
        and filter_data.get('max') is None
    )


def _filter_field_name(filter_data):
    field = filter_data.get('field', '') if isinstance(filter_data, dict) else ''
    if not isinstance(field, str):
        return ''
    bracketed = re.findall(r'\[([^\]]+)\]', field)
    return (bracketed[-1] if bracketed else field.strip('[]')).casefold()


def _combined_group_reference_status(group, extra_objects):
    aliases = {
        str(value).strip().casefold()
        for value in (group.get('name'), group.get('raw_name'))
        if isinstance(value, str) and value.strip()
    }
    if not aliases:
        return None

    identifier_aliases = set()
    for alias in aliases:
        bracketed = re.findall(r'\[([^\]]+)\]', alias)
        identifier_aliases.add(bracketed[-1] if bracketed else alias.strip('[]'))

    consumer_context_available = False
    for key in _COMBINED_GROUP_CONSUMER_KEYS:
        if key not in extra_objects:
            continue
        consumer_context_available = True
        consumer_data = extra_objects[key]
        if key in ('filters', 'datasource_filters') and isinstance(consumer_data, list):
            consumer_data = [
                filter_data for filter_data in consumer_data
                if not _is_non_restrictive_filter(filter_data)
            ]
        elif key in ('worksheets', '_worksheets') and isinstance(consumer_data, list):
            worksheets = []
            for worksheet in consumer_data:
                if not isinstance(worksheet, dict):
                    worksheets.append(worksheet)
                    continue
                worksheet_copy = dict(worksheet)
                worksheet_filters = worksheet.get('filters', [])
                inert_names = {
                    _filter_field_name(filter_data)
                    for filter_data in worksheet_filters
                    if _is_non_restrictive_filter(filter_data)
                }
                active_names = {
                    _filter_field_name(filter_data)
                    for filter_data in worksheet_filters
                    if not _is_non_restrictive_filter(filter_data)
                }
                inert_names.difference_update(active_names)
                for field_key in ('fields', 'slices'):
                    fields = worksheet.get(field_key)
                    if isinstance(fields, list):
                        worksheet_copy[field_key] = [
                            field for field in fields
                            if not (
                                isinstance(field, dict)
                                and field.get('shelf') == 'filter'
                                and isinstance(field.get('name'), str)
                                and field['name'].strip('[]').casefold() in inert_names
                            )
                        ]
                if isinstance(worksheet_filters, list):
                    worksheet_copy['filters'] = [
                        filter_data for filter_data in worksheet_filters
                        if not _is_non_restrictive_filter(filter_data)
                    ]
                worksheets.append(worksheet_copy)
            consumer_data = worksheets
        for text in _iter_nested_strings(consumer_data):
            normalized = text.casefold()
            for alias in identifier_aliases:
                bracketed_ref = r'\[' + re.escape(alias) + r'\]'
                bare_ref = (r'(?<![\w])' + re.escape(alias)
                            + r'(?![\w]|\s+[\w])')
                if (re.search(bracketed_ref, normalized)
                        or normalized.strip() == alias
                        or re.search(bare_ref, normalized)):
                    return True
    return False if consumer_context_available else None


def _combined_source_field_refs(group):
    fields = group.get('source_fields')
    if not isinstance(fields, list) or not fields:
        raise ValueError(f"Combined group {group.get('name')!r}: source fields are missing")
    if 'source_field_refs' not in group:
        return fields, False
    refs = group['source_field_refs']
    if (not isinstance(refs, list) or len(refs) != len(fields)
            or any(not isinstance(field, str) or not isinstance(ref, str)
                   or not field or not ref or _clean_field_ref(ref) != field
                   for field, ref in zip(fields, refs))):
        raise ValueError(
            f"Combined group {group.get('name')!r}: source field references "
            "are inconsistent with normalized source fields")
    return refs, True


def _combined_datasource_table_scope(model, extra_objects, group, *, allow_unresolved=False):
    datasource_name = group.get('datasource', '')
    if not datasource_name:
        return None
    datasources = extra_objects.get('_datasources') or model.get('_datasources', [])
    sources = [ds for ds in datasources if ds.get('name') == datasource_name]
    if len(sources) != 1:
        if allow_unresolved:
            return set()
        problem = 'missing' if not sources else 'ambiguous'
        raise ValueError(
            f"Combined group {group.get('name')!r}: declared datasource "
            f"{datasource_name!r} is {problem}")
    renames = model.get('_table_rename_map', {})
    return {
        renames.get((datasource_name, table.get('name')), table.get('name'))
        for table in sources[0].get('tables', [])
    }


def _combined_field_owners(model, extra_objects, group, column_table_map,
                           *, allow_unresolved=False, register_aliases=True):
    """Resolve each combined-field input to a uniquely evidenced table."""
    from powerbi_import.errors import PartialGenerationError

    _, has_source_refs = _combined_source_field_refs(group)
    owners = {}
    table_fields = {}
    datasources = extra_objects.get('_datasources') or model.get('_datasources', [])
    allowed_tables = _combined_datasource_table_scope(
        model, extra_objects, group, allow_unresolved=allow_unresolved)

    def remember(field, table_name):
        if isinstance(field, str) and field:
            owners.setdefault(field.casefold(), set()).add(table_name)

    for table in model['model']['tables']:
        table_name = table.get('name', '')
        table_fields[table_name] = set()
        for field in table.get('columns', []) + table.get('measures', []):
            for alias in (field.get('name'), field.get('sourceColumn'), field.get('caption')):
                if isinstance(alias, str) and alias:
                    table_fields[table_name].add(alias.casefold())
                    remember(alias, table_name)
    for (table_name, raw), produced in model.get('_column_rename_map', {}).items():
        if (isinstance(produced, str)
                and produced.casefold() in table_fields.get(table_name, set())):
            remember(raw, table_name)
    for datasource in datasources:
        aliases = list(datasource.get('calculations', [])) + list(datasource.get('columns', []))
        for table in datasource.get('tables', []):
            aliases.extend(table.get('columns', []))
        for alias in aliases:
            raw = str(alias.get('name', '')).replace('[', '').replace(']', '')
            caption = str(alias.get('caption', '')).replace('[', '').replace(']', '')
            if raw and caption:
                raw_owners = owners.get(raw.casefold(), set()).copy()
                caption_owners = owners.get(caption.casefold(), set()).copy()
                if raw_owners:
                    for table_name in raw_owners:
                        remember(caption, table_name)
                else:
                    for table_name in caption_owners:
                        remember(raw, table_name)

    resolved = {}
    for raw in group['source_fields']:
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"Combined group {group.get('name')!r}: "
                             "source fields must be nonempty strings")
        field = raw if has_source_refs else _clean_tableau_field_ref(raw)
        candidates = owners.get(field.casefold(), set()).copy()
        if allowed_tables is not None:
            candidates.intersection_update(allowed_tables)
        if len(candidates) != 1:
            if allow_unresolved:
                return None
            if candidates:
                raise ValueError(
                    f"Combined group {group.get('name')!r}: source field "
                    f"{raw!r} has ambiguous ownership in {sorted(candidates)!r}")
            raise PartialGenerationError(
                f"Combined group {group.get('name')!r}: source field "
                f"{raw!r} is unresolved in the model",
                item=group.get('raw_name') or group.get('name'),
                source=group.get('datasource'), recovery_kind='unresolved_combined_group',
                required_evidence=[f'Explicit source-column binding for {raw!r}'])
        owner = next(iter(candidates))
        mapped = column_table_map.get(field)
        if (mapped and mapped != owner
                and (allowed_tables is None or mapped in allowed_tables)):
            if allow_unresolved:
                return None
            raise ValueError(
                f"Combined group {group.get('name')!r}: source field "
                f"{raw!r} has conflicting ownership metadata")
        if register_aliases:
            column_table_map.setdefault(field, owner)
        resolved[field] = owner
    return resolved


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
        is_combined = g.get('group_type') == 'combined'
        if group_name and is_combined and not g.get('source_fields'):
            raise ValueError(f"Combined group {group_name!r}: source fields are missing")
        if not group_name or (not is_combined and group_name in existing_cols):
            continue

        if is_combined:
            source_refs, has_source_refs = _combined_source_field_refs(g)
            try:
                field_owners = _combined_field_owners(
                    model, extra_objects, g, column_table_map)
            except ValueError as exc:
                if _combined_group_reference_status(g, extra_objects) is False:
                    logger.warning(
                        "Skipping unresolved unreferenced combined group %r: %s",
                        group_name, exc)
                    continue
                raise
            if field_owners is None:
                raise ValueError(
                    f"Combined group {group_name!r}: source field ownership is unresolved")
            hosts = set(field_owners.values())
            if None not in hosts and len(hosts) == 1:
                host = next(iter(hosts))
                if host != main_table_name and any(
                        table.get('name') == host
                        for table in model['model']['tables']):
                    _process_sets_groups_bins(
                        model, dict(extra_objects, sets=[], groups=[g], bins=[]),
                        host, column_table_map)
                    continue
            else:
                materialized = {
                    frozenset((rel.get('fromTable'), rel.get('toTable')))
                    for rel in model.get('_materialized_composite_relationships', [])
                }
                if any(frozenset(pair).issubset(hosts)
                       and frozenset(pair) not in materialized
                       for pair in model.get('_logical_predicate_pairs', [])):
                    raise ValueError(
                        f"Combined group {group_name!r} spans a logical predicate: "
                        "no verified scalar relationship; cannot substitute RELATED or MAX")
            if '\0' in group_name:
                raise ValueError(
                    f"Combined group {group_name!r}: name contains a null character")
            identity = _combined_group_identity(g)
            existing = [
                field for field in main_table.get('columns', []) + main_table.get('measures', [])
                if any(isinstance(alias, str) and alias.casefold() == group_name.casefold()
                       for alias in (field.get('name'), field.get('sourceColumn')))
            ]
            same_group = len(existing) == 1 and any(
                    annotation.get('name') == 'Migration_CombinedGroupIdentity'
                    and annotation.get('value') == identity
                    for annotation in existing[0].get('annotations', []))
            if existing and not same_group:
                raise ValueError(
                    f"Combined group {group_name!r}: a column, measure or source binding "
                    f"with that name already exists in table {main_table_name!r}")
            raw_name = g.get('raw_name', '')
            aliases = {group_name}
            if isinstance(raw_name, str) and raw_name:
                aliases.add(raw_name)
            scope = _combined_datasource_table_scope(model, extra_objects, g)
            for table in model['model']['tables']:
                if scope is not None and table.get('name') not in scope:
                    continue
                fields = table.get('columns', []) + table.get('measures', [])
                for field in fields:
                    if same_group and field is existing[0]:
                        continue
                    for field_name in (field.get('name'), field.get('sourceColumn')):
                        if not isinstance(field_name, str):
                            continue
                        for alias in aliases:
                            if field_name.casefold() == alias.casefold():
                                raise ValueError(
                                    f"Combined group {group_name!r}: identifier {alias!r} "
                                    f"already exists in table {table.get('name')!r}")
            if same_group:
                continue

            source_fields = g['source_fields']
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
            for (host, raw), produced in model.get('_column_rename_map', {}).items():
                if host == main_table_name:
                    col_caption_map[raw] = produced
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
                'yr': 'Date.Year', 'tyr': 'Date.StartOfYear',
                'mn': 'Date.Month', 'tmn': 'Date.StartOfMonth',
                'dy': 'Date.Day',
                'qr': 'Date.QuarterOfYear', 'tqr': 'Date.StartOfQuarter',
                'wk': 'Date.WeekOfYear',
                'hr': 'Time.Hour', 'mt': 'Time.Minute', 'sc': 'Time.Second',
            }
            # These derivations are date/time operations, but do not have a
            # supported equivalent in the mapping above. Failing closed is
            # preferable to serializing the untouched timestamp as text.
            _UNSUPPORTED_DATE_PART_PREFIXES = {'md', 'mdy', 'hms', 'thr', 'trunc', 'tdy', 'twk'}
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
            # Model columns include DAX-only columns, and column_table_map also
            # includes measures. Neither exists in the Power Query row. Rebuild
            # this map for each group so earlier M additions are available, but
            # earlier DAX fallbacks are not.
            m_row_columns = {}
            for col in main_table.get('columns', []):
                if not col.get('expression') and not col.get('isCalculated'):
                    source = col.get('sourceColumn') or col.get('name', '')
                    m_row_columns[col.get('name', '')] = source
                    m_row_columns[source] = source
            measure_refs = {
                (table.get('name'), measure.get('name'))
                for table in model['model']['tables']
                for measure in table.get('measures', [])
            }
            date_dax_funcs = {
                'Date.Year': 'YEAR', 'Date.Month': 'MONTH',
                'Date.Day': 'DAY', 'Date.QuarterOfYear': 'QUARTER',
                'Date.WeekOfYear': 'WEEKNUM', 'Time.Hour': 'HOUR',
                'Time.Minute': 'MINUTE', 'Time.Second': 'SECOND',
            }
            all_m_available = True
            for idx, sf_raw in enumerate(source_refs):
                # 1. Extract derivation prefix before cleaning
                if _RE_TABLE_CALC_PREFIX.match(sf_raw):
                    raise ValueError(
                        f"Combined group {group_name!r} uses unsupported "
                        f"table-calculation derivation {sf_raw!r}")
                prefix_match = _RE_DERIVATION_PREFIX.match(sf_raw)
                date_prefix = prefix_match.group(1) if prefix_match else None
                if date_prefix in _UNSUPPORTED_DATE_PART_PREFIXES:
                    raise ValueError(
                        f"Combined group '{group_name}' uses unsupported "
                        f"date-part derivation prefix '{date_prefix}'")
                if date_prefix and date_prefix not in _DATE_PART_M_FUNC and date_prefix != 'none':
                    raise ValueError(
                        f"Combined group {group_name!r} uses unsupported "
                        f"component derivation prefix {date_prefix!r}")
                # 2. Clean and resolve field name
                sf = source_fields[idx]
                if not has_source_refs:
                    sf = _clean_tableau_field_ref(sf)
                resolved = calc_map_lookup.get(sf, sf)
                if not has_source_refs:
                    resolved = _clean_tableau_field_ref(resolved)
                # Resolve internal Tableau field name to caption (e.g. "Postal Code" → "Code postal")
                owner = field_owners.get(sf)
                if owner is None:
                    raise ValueError(
                        f"Combined group {group_name!r}: source field {sf!r} "
                        "has no resolved owner")
                resolved = model.get('_column_rename_map', {}).get(
                    (owner, sf), col_caption_map.get(resolved, resolved))
                owner_table = next(table for table in model['model']['tables']
                                   if table.get('name') == owner)
                owner_names = {
                    field.get('name') for field in
                    owner_table.get('columns', []) + owner_table.get('measures', [])
                }
                if resolved not in owner_names:
                    matching = [name for name in owner_names
                                if isinstance(name, str)
                                and name.casefold() == resolved.casefold()]
                    if len(matching) == 1:
                        resolved = matching[0]
                if resolved not in owner_names:
                    raise ValueError(
                        f"Combined group {group_name!r}: source field {sf_raw!r} "
                        f"is unresolved (resolved name {resolved!r})")
                # 3. Build M column reference
                if owner != main_table_name or resolved not in m_row_columns:
                    all_m_available = False
                source_name = m_row_columns.get(resolved, resolved)
                if '\0' in source_name:
                    raise ValueError(
                        f"Combined group {group_name!r}: source field contains a null character")
                m_ref = '[#' + _m_header_literal(source_name) + ']'
                # 4. Apply date-part function: first from derivation prefix, then from group name
                m_func = None
                if date_prefix and date_prefix in _DATE_PART_M_FUNC:
                    m_func = _DATE_PART_M_FUNC[date_prefix]
                elif (not has_source_refs and not date_prefix
                      and idx < len(name_func_map) and name_func_map[idx]):
                    m_func = name_func_map[idx]
                if m_func:
                    m_ref = f'{m_func}({m_ref})'
                # 5. Wrap in Text.From() for safe text concatenation
                m_ref = f'Text.From({m_ref})'
                m_parts.append(m_ref)
                # Also build DAX parts for fallback
                table_ref = owner
                escaped_table = table_ref.replace("'", "''")
                escaped_col = resolved.replace(']', ']]')
                ref = f"'{escaped_table}'[{escaped_col}]"
                if table_ref != main_table_name and (table_ref, resolved) not in measure_refs:
                    ref = f"RELATED({ref})"
                if m_func == 'Date.StartOfYear':
                    ref = f'IF(ISBLANK({ref}), BLANK(), DATE(YEAR({ref}), 1, 1))'
                elif m_func == 'Date.StartOfQuarter':
                    ref = (f'IF(ISBLANK({ref}), BLANK(), '
                           f'DATE(YEAR({ref}), 3 * INT((MONTH({ref}) - 1) / 3) + 1, 1))')
                elif m_func == 'Date.StartOfMonth':
                    ref = f'IF(ISBLANK({ref}), BLANK(), DATE(YEAR({ref}), MONTH({ref}), 1))'
                elif m_func:
                    ref = f'{date_dax_funcs[m_func]}({ref})'
                dax_parts.append(ref)

            approximation_note = (
                'Combined field concatenation is approximate: delimiter collisions, '
                'nulls, culture and Tableau labels require source-to-target validation.'
            )
            logger.warning("Combined group %r: %s", group_name, approximation_note)
            annotations = [
                {'name': 'Migration_CombinedGroup', 'value': 'approximated'},
                {'name': 'Migration_CombinedGroupIdentity', 'value': identity},
                {'name': 'MigrationNote', 'value': approximation_note},
            ]
            if all_m_available:
                # Every reference is present in this partition's row.
                m_concat_expr = ' & " | " & '.join(m_parts)
                m_steps.append(m_transform_add_column(group_name, f'each {m_concat_expr}', 'type text'))
                main_table["columns"].append({
                    "name": group_name,
                    "dataType": "String",
                    "sourceColumn": group_name,
                    "summarizeBy": "none",
                    "displayFolder": "Groups",
                    "annotations": annotations,
                })
            else:
                # Do not pass this through DAX-to-M conversion: its dependencies
                # were explicitly found to be unavailable during Power Query.
                main_table["columns"].append({
                    "name": group_name,
                    "dataType": "String",
                    "expression": ' & " | " & '.join(dax_parts),
                    "summarizeBy": "none",
                    "isCalculated": True,
                    "displayFolder": "Groups",
                    "annotations": annotations,
                })
            existing_cols.add(group_name)
            continue

        members = g.get('members', {})
        source_field = g.get('source_field', '').replace('[', '').replace(']', '')
        if members and source_field:
            owner_table, owner_name = _owner_of(source_field)
            if group_name in {c.get("name", "") for c in owner_table.get("columns", [])}:
                continue
            # Tableau value groups default unmatched values to "Other" unless
            # the calculation declares an explicit default. Keep explicit
            # empty-string and null defaults distinct from an absent default.
            calculation_metadata = g.get('calculation')
            nested_default = (
                isinstance(calculation_metadata, dict)
                and 'default' in calculation_metadata
            )
            if 'default' in g:
                group_default = g['default']
            elif 'calculation_default' in g:
                group_default = g['calculation_default']
            elif nested_default:
                group_default = calculation_metadata['default']
            else:
                group_default = 'Other'
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
                if group_default is not None:
                    escaped_default = str(group_default).replace('"', '""')
                    owner_steps.append((
                        f'#"Fill_{safe_tag}"',
                        f'Table.ReplaceValue({{prev}}, null, "{escaped_default}", Replacer.ReplaceValue, {{"{escaped_grp}"}})'
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
                if group_default is None:
                    default_arg = ', BLANK()'
                else:
                    escaped_default = str(group_default).replace('"', '""')
                    default_arg = f', "{escaped_default}"'
                dax_expr = (
                    f"SWITCH('{table_ref}'[{source_field}], "
                    f"{', '.join(cases)}{default_arg})"
                )
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
