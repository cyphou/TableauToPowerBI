"""What-If parameter tables, calculation groups and field parameters.

Tableau parameters become three different Power BI shapes: a What-If table with
a SELECTEDVALUE measure, a calculation group when the parameter swaps measures,
and a field parameter when it swaps dimensions. Extracted from
``tmdl_generator``, which re-exports these names; the cluster calls nothing that
stayed behind, so the dependency runs one way.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))
from datasource_extractor import sanitize_param_brackets  # noqa: E402


def _create_parameter_tables(model, parameters, main_table_name):
    """Create What-If parameter tables for Tableau parameters.

    - Range parameters (integer/real): GENERATESERIES(min, max, step) table
    - List parameters (string/boolean): DATATABLE with domain values
    - Any parameters (no domain): measure with default value on main table
    """
    if not parameters:
        return

    type_map = {
        'integer': ('int64', 'INTEGER'),
        'real': ('double', 'DOUBLE'),
        'date': ('dateTime', 'DATETIME'),
        'datetime': ('dateTime', 'DATETIME'),
        'boolean': ('boolean', 'BOOLEAN'),
        'string': ('string', 'STRING'),
    }

    for param in parameters:
        caption = param.get('caption', '')
        if not caption:
            continue

        # Parameter captions may contain literal brackets (e.g.
        # "AIP [Indicateur nationaux][detail]") which break DAX/TMDL bracketed
        # identifiers.  Use a bracket-free name for the table/measure so it
        # matches the reference emitted by convert_tableau_formula_to_dax.
        safe_caption = sanitize_param_brackets(caption)

        datatype = param.get('datatype', 'string')
        default_value = param.get('value', '').strip('"')
        domain_type = param.get('domain_type', 'any')
        allowable_values = param.get('allowable_values', [])

        pbi_type, dax_type = type_map.get(datatype, ('string', 'STRING'))

        if datatype == 'string':
            default_expr = f'"{default_value}"'
        elif datatype == 'boolean':
            default_expr = default_value.upper() if default_value else 'TRUE'
        elif datatype in ('date', 'datetime'):
            # Convert Tableau #YYYY-MM-DD# date literal to DAX DATE()
            date_m = re.match(r'#(\d{4})-(\d{2})-(\d{2})#', default_value)
            if date_m:
                default_expr = f'DATE({int(date_m.group(1))}, {int(date_m.group(2))}, {int(date_m.group(3))})'
            else:
                default_expr = default_value if default_value else 'DATE(2024, 1, 1)'
        else:
            default_expr = default_value if default_value else '0'

        if domain_type == 'database':
            # Dynamic parameter — database-query-driven (Tableau 2024.3+)
            # Generate M table using Value.NativeQuery() for database refresh
            query_sql = param.get('query', '')
            conn_class = param.get('query_connection', '')
            dbname = param.get('query_dbname', '')

            # Build M expression referencing native query
            if query_sql:
                escaped_sql = query_sql.replace('"', '""')
                m_source = f'Value.NativeQuery(#"Source", "{escaped_sql}", null, [EnableFolding=true])'
            else:
                # Fallback — no query available, produce DAX table
                m_source = None

            col_name = "Value"
            param_table = {
                "name": safe_caption,
                "columns": [{
                    "name": col_name,
                    "dataType": pbi_type,
                    "sourceColumn": col_name,
                    "annotations": [
                        {"name": "displayFolder", "value": "Parameters"}
                    ]
                }],
                "measures": [{
                    "name": safe_caption,
                    "expression": f"SELECTEDVALUE('{safe_caption.replace(chr(39), chr(39)*2)}'[{col_name}], {default_expr})",
                    "annotations": [
                        {"name": "displayFolder", "value": "Parameters"},
                        {"name": "MigrationNote",
                         "value": f"Dynamic parameter from Tableau — source query: {query_sql[:200]}"}
                    ]
                }],
                "partitions": [{
                    "name": safe_caption,
                    "mode": "import",
                    "source": {
                        "type": "m",
                        "expression": m_source or f'#table({{"{col_name}"}}, {{{{"{default_value}"}}}})'
                    }
                }],
                "annotations": [
                    {"name": "MigrationNote",
                     "value": "Tableau dynamic parameter — configure Power Query source connection"}
                ]
            }
            if param.get('refresh_on_open'):
                param_table['refreshPolicy'] = {
                    'type': 'automatic'
                }
            model["model"]["tables"].append(param_table)
            continue

        if domain_type == 'any' or not allowable_values:
            # Determine the constant-value DAX expression for the parameter.
            # Tableau may escape embedded quotes either by doubling them ("")
            # or with a backslash (\"). Both are normalized and re-escaped to
            # the DAX doubled-quote convention so that constant-string values
            # (e.g. an "Average of IF [Won Flag]=\"Y\" THEN [Amount] END" KPI
            # description) are emitted verbatim as a constant-string measure
            # rather than being dropped. Simple quoted values like "CHAMPIONS"
            # keep the re-wrapped default_expr so DAX formulas referencing
            # [ParamCaption] still resolve.
            _measure_expr = default_expr
            _raw_val = param.get('value', '').strip()
            if (_raw_val.startswith('"') and _raw_val.endswith('"')
                    and len(_raw_val) > 2):
                _inner = _raw_val[1:-1]
                if '"' in _inner or '\\' in _inner:
                    _norm = _inner.replace('\\"', '"').replace('""', '"')
                    _measure_expr = '"' + _norm.replace('"', '""') + '"'
            for table in model["model"]["tables"]:
                if table.get("name") == main_table_name:
                    if "measures" not in table:
                        table["measures"] = []
                    table["measures"].append({
                        "name": safe_caption,
                        "expression": _measure_expr,
                        "annotations": [
                            {"name": "displayFolder", "value": "Parameters"}
                        ]
                    })
                    break
            continue

        table_expr = None
        col_name = safe_caption
        has_aliases = False

        if domain_type == 'range':
            range_info = next((v for v in allowable_values if v.get('type') == 'range'), None)
            if range_info:
                min_val = range_info.get('min', '') or '0'
                max_val = range_info.get('max', '') or '100'
                step = range_info.get('step', '') or '1'
                table_expr = f"GENERATESERIES({min_val}, {max_val}, {step})"
                col_name = "Value"

        elif domain_type == 'list':
            list_values = [v for v in allowable_values if v.get('type') != 'range']
            if list_values:
                # Check if aliases exist and differ from values (display names)
                has_aliases = any(
                    v.get('alias') and str(v.get('alias')) != str(v.get('value', ''))
                    for v in list_values
                )
                if datatype == 'string':
                    def _clean_str_val(v):
                        val = v.get('value', '')
                        # Strip one layer of surrounding quotes (Tableau wraps strings)
                        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                            val = val[1:-1]
                        # Escape internal quotes for DAX string literals
                        return val.replace('"', '""')
                    if has_aliases:
                        # String list with aliases → include both Value and Name columns
                        def _clean_str_alias(v):
                            alias = v.get('alias', v.get('value', ''))
                            if len(alias) >= 2 and alias[0] == '"' and alias[-1] == '"':
                                alias = alias[1:-1]
                            return alias.replace('"', '""')
                        rows = ', '.join(
                            '{{"{}","{}"}}'.format(_clean_str_val(v), _clean_str_alias(v))
                            for v in list_values
                        )
                    else:
                        rows = ', '.join(
                            '{{"{}"}}'.format(_clean_str_val(v))
                            for v in list_values
                        )
                elif datatype == 'boolean':
                    rows = ', '.join(f'{{{v.get("value", "TRUE").upper()}}}' for v in list_values)
                elif has_aliases:
                    # Numeric list with aliases → include Name column
                    rows = ', '.join(
                        '{{{}, "{}"}}'.format(v.get("value", "0"), v.get("alias", v.get("value", "")).replace('"', '""'))
                        for v in list_values
                    )
                else:
                    rows = ', '.join(f'{{{v.get("value", "0")}}}' for v in list_values)
                col_name = "Value"
                if has_aliases and datatype not in ('boolean',):
                    table_expr = f'DATATABLE("Value", {dax_type}, "Name", STRING, {{{rows}}})'
                else:
                    table_expr = f'DATATABLE("Value", {dax_type}, {{{rows}}})'

        if not table_expr:
            continue

        # Escape apostrophes in caption for DAX table references
        dax_caption = safe_caption.replace("'", "''")

        param_table = {
            "name": safe_caption,
            "columns": [{
                "name": col_name,
                "dataType": pbi_type,
                "sourceColumn": col_name,
                "annotations": [
                    {"name": "displayFolder", "value": "Parameters"}
                ]
            }],
            "measures": [{
                "name": safe_caption,
                "expression": f"SELECTEDVALUE('{dax_caption}'[{col_name}], {default_expr})",
                "annotations": [
                    {"name": "displayFolder", "value": "Parameters"}
                ]
            }],
            "partitions": [{
                "name": safe_caption,
                "mode": "import",
                "source": {
                    "type": "calculated",
                    "expression": table_expr
                }
            }]
        }

        # Add Name column when DATATABLE includes aliases (numeric or string with aliases)
        if has_aliases and domain_type == 'list' and datatype not in ('boolean',):
            param_table["columns"].append({
                "name": "Name",
                "dataType": "string",
                "sourceColumn": "Name",
                "annotations": [
                    {"name": "displayFolder", "value": "Parameters"}
                ]
            })

        # Mark as parameter table so Phase 10 skips it during relationship inference
        if "annotations" not in param_table:
            param_table["annotations"] = []
        param_table["annotations"].append({"name": "ParameterTable", "value": "true"})

        model["model"]["tables"].append(param_table)

    # Deduplicate: remove parameter measures from other tables
    param_table_names = set()
    for param in parameters:
        caption = param.get('caption', '')
        domain_type = param.get('domain_type', 'any')
        if caption and domain_type in ('range', 'list') and param.get('allowable_values'):
            param_table_names.add(sanitize_param_brackets(caption))

    if param_table_names:
        for table in model["model"]["tables"]:
            table_name = table.get("name", "")
            if table_name in param_table_names:
                continue
            if "measures" in table:
                table["measures"] = [
                    m for m in table["measures"]
                    if m.get("name", "") not in param_table_names
                ]


def _create_calculation_groups(model, parameters, main_table_name):
    """Create calculation group tables from parameters that switch between measures.

    Two detection paths:
    1. **String list parameters** whose allowable values match existing measure
       names → each measure becomes a ``CALCULATE(SELECTEDMEASURE())`` item.
    2. **Numeric list parameters with aliases** where a SWITCH measure maps
       numeric values to aggregation expressions → each alias becomes a
       calculation item with the branch expression.  The SWITCH measure and
       What-If table are kept alongside for backward compatibility.
    """
    if not parameters:
        return

    existing_tables = {t.get('name', '') for t in model['model']['tables']}

    # Collect all measure names across the model
    measure_names = set()
    for table in model['model']['tables']:
        for m in table.get('measures', []):
            measure_names.add(m.get('name', ''))

    for param in parameters:
        caption = param.get('caption', '')
        domain_type = param.get('domain_type', '')
        datatype = param.get('datatype', 'string')
        allowable_values = param.get('allowable_values', [])

        if domain_type != 'list' or not allowable_values:
            continue

        # ── Path 1: String list parameters matching measure names ──
        if datatype == 'string':
            matching_values = [
                v for v in allowable_values
                if v.get('type') != 'range' and v.get('value', '') in measure_names
            ]
            if len(matching_values) < 2:
                continue

            cg_name = f"{caption} CalcGroup"
            if cg_name in existing_tables:
                continue

            calc_items = []
            for idx, val in enumerate(matching_values):
                measure_ref = val.get('value', '')
                calc_items.append({
                    "name": measure_ref,
                    "expression": "CALCULATE(SELECTEDMEASURE())",
                    "ordinal": idx,
                })

            cg_table = {
                "name": cg_name,
                "calculationGroup": {
                    "columns": [{"name": caption, "dataType": "string",
                                 "sourceColumn": "Name"}],
                    "calculationItems": calc_items,
                },
                "columns": [{"name": caption, "dataType": "string",
                             "sourceColumn": "Name"}],
                "partitions": [{
                    "name": cg_name,
                    "mode": "import",
                    "source": {"type": "calculationGroup"},
                }],
                "annotations": [
                    {"name": "displayFolder", "value": "Calculation Groups"},
                ],
            }
            model['model']['tables'].append(cg_table)
            existing_tables.add(cg_name)
            continue

        # ── Path 2: Numeric list parameters with aliases → SWITCH measure ──
        if datatype not in ('real', 'integer'):
            continue

        list_values = [v for v in allowable_values if v.get('type') != 'range']
        if len(list_values) < 2:
            continue
        # All values must have aliases for meaningful item names
        if not all(v.get('alias') for v in list_values):
            continue

        # Build value→alias map (normalise "1.0" → "1")
        val_alias = {}
        for v in list_values:
            raw = v.get('value', '')
            try:
                num = float(raw)
                normalised = str(int(num)) if num == int(num) else str(num)
            except (ValueError, OverflowError):
                normalised = raw
            val_alias[normalised] = v.get('alias', '')

        # Find SWITCH measures that reference this parameter
        switch_pat = re.compile(
            r'^SWITCH\s*\(\s*\[' + re.escape(caption) + r'\]\s*,(.+)\)$',
            re.IGNORECASE | re.DOTALL,
        )

        found_branches = None
        for table in model['model']['tables']:
            for m in table.get('measures', []):
                expr = m.get('expression', '').strip()
                sm = switch_pat.match(expr)
                if not sm:
                    continue
                branches = _parse_switch_branches(sm.group(1).strip())
                if branches and all(bk in val_alias for bk, _ in branches):
                    found_branches = branches
                    break
            if found_branches:
                break

        if not found_branches:
            continue

        cg_name = f"{caption} CalcGroup"
        if cg_name in existing_tables:
            continue

        calc_items = []
        for idx, (bval, bexpr) in enumerate(found_branches):
            item_name = val_alias.get(bval, bval)
            calc_items.append({
                "name": item_name,
                "expression": f"CALCULATE({bexpr})",
                "ordinal": idx,
            })

        cg_table = {
            "name": cg_name,
            "calculationGroup": {
                "columns": [{"name": caption, "dataType": "string",
                             "sourceColumn": "Name"}],
                "calculationItems": calc_items,
            },
            "columns": [{"name": caption, "dataType": "string",
                         "sourceColumn": "Name"}],
            "partitions": [{
                "name": cg_name,
                "mode": "import",
                "source": {"type": "calculationGroup"},
            }],
            "annotations": [
                {"name": "displayFolder", "value": "Calculation Groups"},
            ],
        }
        model['model']['tables'].append(cg_table)
        existing_tables.add(cg_name)


def _parse_switch_branches(args_str):
    """Parse the arguments of a SWITCH() after the switch expression.

    Returns a list of ``(value, expression)`` tuples, or *None* if parsing fails.
    The trailing default value (odd argument) is ignored.
    """
    parts = []
    depth = 0
    current = []
    for ch in args_str:
        if ch in '(':
            depth += 1
            current.append(ch)
        elif ch in ')':
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append(''.join(current).strip())

    branches = []
    i = 0
    while i + 1 < len(parts):
        val = parts[i].strip()
        expr = parts[i + 1].strip()
        try:
            num = float(val)
            val = str(int(num)) if num == int(num) else str(num)
        except (ValueError, OverflowError):
            pass
        branches.append((val, expr))
        i += 2
    return branches if branches else None


def _create_field_parameters(model, parameters, main_table_name, column_table_map):
    """Create field parameter tables from parameters that switch between columns.

    Field parameters in Power BI allow users to dynamically choose which column
    appears on a visual axis or slicer. This converts Tableau parameters whose
    allowable values match existing column names into PBI field parameter tables
    with ``NAMEOF()`` references.
    """
    if not parameters:
        return

    existing_tables = {t.get('name', '') for t in model['model']['tables']}

    # Collect all known column names and measure names
    all_columns = set()
    measure_names = set()
    for table in model['model']['tables']:
        for col in table.get('columns', []):
            all_columns.add(col.get('name', ''))
        for m in table.get('measures', []):
            measure_names.add(m.get('name', ''))

    for param in parameters:
        caption = param.get('caption', '')
        domain_type = param.get('domain_type', '')
        datatype = param.get('datatype', 'string')
        allowable_values = param.get('allowable_values', [])

        # Only string list parameters with column-like values
        if datatype != 'string' or domain_type != 'list' or not allowable_values:
            continue

        matching_cols = [
            v for v in allowable_values
            if v.get('type') != 'range' and v.get('value', '') in all_columns
        ]

        if len(matching_cols) < 2:
            continue
        # Skip if all values are measures (those become calc groups instead)
        if all(v.get('value', '') in measure_names for v in matching_cols):
            continue

        fp_name = f"{caption} FieldParam"
        if fp_name in existing_tables:
            continue

        # Build NAMEOF references for the field parameter DAX expression
        rows = []
        for idx, val in enumerate(matching_cols):
            col_name = val.get('value', '')
            col_table = column_table_map.get(col_name, main_table_name)
            rows.append(
                f"(NAMEOF('{col_table}'[{col_name}]), {idx}, \"{col_name}\")"
            )

        fp_expr = "{\n" + ",\n".join(rows) + "\n}"

        fp_table = {
            "name": fp_name,
            "columns": [
                {"name": caption, "dataType": "string",
                 "sourceColumn": caption,
                 "annotations": [{"name": "displayFolder",
                                  "value": "Field Parameters"}]},
                {"name": f"{caption}_Order", "dataType": "int64",
                 "sourceColumn": f"{caption}_Order", "isHidden": True},
                {"name": f"{caption}_Fields", "dataType": "string",
                 "sourceColumn": f"{caption}_Fields", "isHidden": True},
            ],
            "partitions": [{
                "name": fp_name,
                "mode": "import",
                "source": {
                    "type": "calculated",
                    "expression": fp_expr,
                },
            }],
            "annotations": [
                {"name": "displayFolder", "value": "Field Parameters"},
                {"name": "PBI_NavigationStepName", "value": "Navigation"},
                {"name": "ParameterMetadata",
                 "value": json.dumps({"version": 3, "kind": 2})},
            ],
        }
        model['model']['tables'].append(fp_table)
        existing_tables.add(fp_name)
