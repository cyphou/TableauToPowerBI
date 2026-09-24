"""Row-Level Security roles from Tableau user filters.

Tableau expresses per-user visibility as user filters, USERNAME()/FULLNAME()
calculations and ISMEMBEROF() group tests; each becomes a Power BI RLS role with
a DAX table permission. Extracted from ``tmdl_generator``, which re-exports
these names; the cluster calls nothing that stayed behind.
"""

import logging
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))
from datasource_extractor import convert_tableau_formula_to_dax  # noqa: E402

logger = logging.getLogger(__name__)


def _create_rls_roles(model, user_filters, main_table_name, column_table_map):
    """Create Row-Level Security (RLS) roles from Tableau user filters.

    Converts Tableau security patterns to Power BI RLS roles:
    - User filter (explicit user->row mappings) -> RLS role with USERPRINCIPALNAME()
    - Calculated security (USERNAME/FULLNAME formulas) -> RLS role with DAX filter
    - ISMEMBEROF group patterns -> separate RLS role per group
    """
    if not user_filters:
        return

    if not main_table_name:
        tables = model.get('model', {}).get('tables', [])
        if tables:
            main_table_name = tables[0].get('name', 'Table')
        else:
            main_table_name = 'Table'

    roles = []
    role_names = set()

    model_tables = model.get('model', {}).get('tables', [])
    table_name_map = {
        (t.get('name') or '').lower(): (t.get('name') or '')
        for t in model_tables
        if t.get('name')
    }

    def _build_table_column_index():
        idx = {}
        for t in model_tables:
            tname = t.get('name') or ''
            if not tname:
                continue
            cols = set()
            for c in t.get('columns', []) or []:
                cname = (c.get('name') or '').strip()
                if cname:
                    cols.add(cname.lower())
                src = (c.get('sourceColumn') or '').strip()
                if src:
                    cols.add(src.lower())
            idx[tname] = cols
        return idx

    table_cols_index = _build_table_column_index()

    def _missing_refs_in_expr(expr, default_table):
        missing = []
        pattern = re.compile(r"(?:'((?:[^']|'')+)')?\[([^\]\r\n]+)\]")
        for m in pattern.finditer(expr or ''):
            raw_table = m.group(1)
            ref_col = (m.group(2) or '').strip().replace(']]', ']')
            if not ref_col:
                continue

            if raw_table:
                ref_table = raw_table.replace("''", "'")
                ref_table = table_name_map.get(ref_table.lower(), ref_table)
            else:
                ref_table = default_table

            known_cols = table_cols_index.get(ref_table)
            if known_cols is None or ref_col.lower() not in known_cols:
                missing.append((ref_table, ref_col))
        return missing

    def _table_has_column(table_name, column_name):
        known_cols = table_cols_index.get(table_name, set())
        return (column_name or '').strip().lower() in known_cols

    for uf in user_filters:
        uf_type = uf.get('type', '')

        if uf_type == 'user_filter':
            filter_name = uf.get('name', 'UserFilter')
            column = uf.get('column', '')
            user_mappings = uf.get('user_mappings', [])

            table_name = column_table_map.get(column, main_table_name)

            col_clean = column
            if ':' in col_clean:
                col_clean = col_clean.split(':')[-1]

            if user_mappings:
                user_values = {}
                for mapping in user_mappings:
                    user = mapping.get('user', '')
                    val = mapping.get('value', '')
                    if user and val:
                        user_values.setdefault(user, []).append(val)

                or_clauses = []
                for user_email, values in user_values.items():
                    if len(values) == 1:
                        val_expr = f'[{col_clean}] = "{values[0]}"'
                    else:
                        val_list = ', '.join(f'"{v}"' for v in values)
                        val_expr = f'[{col_clean}] IN {{{val_list}}}'
                    or_clauses.append(
                        f'(USERPRINCIPALNAME() = "{user_email}" && {val_expr})'
                    )

                if or_clauses:
                    filter_dax = ' || '.join(or_clauses)
                else:
                    filter_dax = 'FALSE()'

                fallback_note = ''
                if not _table_has_column(table_name, col_clean):
                    filter_dax = 'TRUE()'
                    fallback_note = (
                        f" RLS expression referenced missing column '{col_clean}' "
                        f"on table '{table_name}'; filter was downgraded to TRUE()."
                    )

                role_name = _unique_role_name(filter_name, role_names)
                role_names.add(role_name)

                roles.append({
                    "name": role_name,
                    "modelPermission": "read",
                    "tablePermissions": [
                        {
                            "name": table_name,
                            "filterExpression": filter_dax
                        }
                    ],
                    "_migration_note": (
                        f"Migrated from Tableau user filter '{filter_name}'. "
                        f"Each user is mapped to their allowed {col_clean} values inline. "
                        f"Consider creating a security table for dynamic RLS."
                        f"{fallback_note}"
                    ),
                    "_user_mappings": user_mappings
                })

            elif column:
                filter_dax = f"[{col_clean}] = USERPRINCIPALNAME()"
                fallback_note = ''
                if not _table_has_column(table_name, col_clean):
                    filter_dax = 'TRUE()'
                    fallback_note = (
                        f" RLS expression referenced missing column '{col_clean}' "
                        f"on table '{table_name}'; filter was downgraded to TRUE()."
                    )
                role_name = _unique_role_name(filter_name, role_names)
                role_names.add(role_name)

                roles.append({
                    "name": role_name,
                    "modelPermission": "read",
                    "tablePermissions": [
                        {
                            "name": table_name,
                            "filterExpression": filter_dax
                        }
                    ],
                    "_migration_note": (
                        f"Migrated from Tableau user filter '{filter_name}' "
                        f"without explicit user mappings.{fallback_note}"
                    )
                })

        elif uf_type == 'calculated_security':
            calc_name = uf.get('name', 'SecurityCalc')
            formula = uf.get('formula', '')
            functions_used = uf.get('functions_used', [])
            ismemberof_groups = uf.get('ismemberof_groups', [])

            if ismemberof_groups:
                for group in ismemberof_groups:
                    role_name = _unique_role_name(group, role_names)
                    role_names.add(role_name)

                    filter_dax = f"TRUE()  /* Members of role '{group}' have access */"

                    roles.append({
                        "name": role_name,
                        "modelPermission": "read",
                        "tablePermissions": [
                            {
                                "name": main_table_name,
                                "filterExpression": filter_dax
                            }
                        ],
                        "_migration_note": (
                            f"Migrated from Tableau ISMEMBEROF(\"{group}\"). "
                            f"Assign Azure AD group members to this RLS role."
                        )
                    })

            elif 'USERNAME' in functions_used or 'FULLNAME' in functions_used:
                dax_filter = convert_tableau_formula_to_dax(
                    formula,
                    table_name=main_table_name,
                    column_table_map=column_table_map,
                    validate_output=True,
                    fallback_on_invalid=True,
                )
                if dax_filter and 'TODO: DAX conversion validation failed' in dax_filter:
                    logger.warning(
                        "RLS DAX conversion guard triggered for '%s'",
                        calc_name,
                    )

                role_name = _unique_role_name(calc_name, role_names)
                role_names.add(role_name)

                # Determine which table the filter applies to
                cross_ref = re.search(r"'([^']+)'\[", dax_filter)
                perm_table = main_table_name
                if cross_ref:
                    ref_table = cross_ref.group(1)
                    model_table_names = {t.get("name", "") for t in model["model"]["tables"]}
                    if ref_table in model_table_names and ref_table != main_table_name:
                        perm_table = ref_table
                        dax_filter = dax_filter.replace(f"'{ref_table}'[", "[")

                fallback_note = ''
                missing_refs = _missing_refs_in_expr(dax_filter, perm_table)
                if missing_refs:
                    dax_filter = 'TRUE()'
                    missing_desc = ', '.join(
                        f"{tbl}[{col}]" for tbl, col in missing_refs[:6]
                    )
                    fallback_note = (
                        " Converted RLS filter referenced missing columns "
                        f"({missing_desc}); filter was downgraded to TRUE()."
                    )

                roles.append({
                    "name": role_name,
                    "modelPermission": "read",
                    "tablePermissions": [
                        {
                            "name": perm_table,
                            "filterExpression": dax_filter
                        }
                    ],
                    "_migration_note": (
                        f"Migrated from Tableau calculated security '{calc_name}'. "
                        f"Original formula: {formula}"
                        f"{fallback_note}"
                    )
                })

    if roles:
        model["model"]["roles"] = roles
        print(f"    \u2713 {len(roles)} RLS role(s) created")


def _unique_role_name(base_name, existing_names):
    """Generate a unique role name, appending _N if needed."""
    clean = re.sub(r'[^\w\s-]', '', base_name).strip()
    if not clean:
        clean = 'Role'

    if clean not in existing_names:
        return clean

    counter = 2
    while f"{clean}_{counter}" in existing_names:
        counter += 1
    return f"{clean}_{counter}"
