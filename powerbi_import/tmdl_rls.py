"""Row-Level Security roles from Tableau user filters.

Tableau expresses per-user visibility as user filters, USERNAME()/FULLNAME()
calculations and ISMEMBEROF() group tests; each becomes a Power BI RLS role with
a DAX table permission. Extracted from ``tmdl_generator``, which re-exports
these names; the cluster calls nothing that stayed behind.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))
from datasource_extractor import convert_tableau_formula_to_dax  # noqa: E402

def _rls_error(user_filter, table_name, reason):
    """Stop generation rather than emitting a permissive or placeholder role."""
    from powerbi_import.errors import PartialGenerationError

    name = user_filter.get('name') or 'UserFilter'
    return PartialGenerationError(
        f"Cannot safely generate RLS filter {name!r}: {reason} No permissive "
        "fallback or unverified DAX placeholder was emitted.",
        item=name,
        source=user_filter.get('datasource') or table_name,
        recovery_kind='rls_filter_semantics',
        required_evidence=[
            'Complete source security predicate and exact table/field bindings',
            'Verified user and group identity mapping',
            'Verified source filter, calculation grain and null behavior',
        ])


def _simple_identity_comparison(formula):
    """Recognize only a physical-field equality to USERNAME or FULLNAME."""
    text = (formula or '').strip()
    wrapper = re.fullmatch(
        r'IF\s+(.+?)\s+THEN\s+TRUE\s+ELSE\s+FALSE\s+END',
        text, re.IGNORECASE | re.DOTALL)
    if wrapper:
        text = wrapper.group(1).strip()
        if text.startswith('(') and text.endswith(')'):
            text = text[1:-1].strip()
    field = r'\[(?P<field>[^\[\]]+)\]'
    identity = r'(?P<identity>USERNAME|FULLNAME)\s*\(\s*\)'
    match = (re.fullmatch(rf'{field}\s*(?:==|=)\s*{identity}', text, re.IGNORECASE)
             or re.fullmatch(rf'{identity}\s*(?:==|=)\s*{field}', text, re.IGNORECASE))
    return (match.group('field'), match.group('identity').upper()) if match else None


def _dax_string(value):
    return '"' + str(value).replace('"', '""') + '"'


def _validate_rls_bindings(model):
    """Verify generated RLS roles still bind the same physical security field."""
    tables = model.get('model', {}).get('tables', [])
    roles = model.get('model', {}).get('roles', [])
    for requirement in model.get('_rls_required_roles', []):
        source = {'name': requirement['name'], 'datasource': requirement['source']}
        table_name = requirement['table']
        matching_roles = [
            role for role in roles
            if isinstance(role.get('name'), str)
            and role['name'].casefold() == requirement['name'].casefold()
        ]
        if (len(matching_roles) != 1
                or matching_roles[0]['name'] != requirement['name']):
            raise _rls_error(source, table_name,
                             "the generated security role is missing or ambiguous.")
        permissions = matching_roles[0].get('tablePermissions') or []
        if (len(permissions) != 1
                or permissions[0].get('name') != table_name
                or permissions[0].get('filterExpression') != requirement['expression']):
            raise _rls_error(
                source, table_name,
                "the verified table permission was removed or changed after conversion.")
        matches = [
            table for table in tables
            if isinstance(table.get('name'), str)
            and table['name'].casefold() == table_name.casefold()
        ]
        if len(matches) != 1 or matches[0]['name'] != table_name:
            raise _rls_error(source, table_name,
                             "the permission table is missing or ambiguous.")
        field = requirement.get('field')
        if field is None:
            continue
        columns = [
            column for column in matches[0].get('columns', []) or []
            if isinstance(column.get('name'), str)
            and column['name'].casefold() == field.casefold()
        ]
        if (len(columns) != 1 or columns[0]['name'] != field
                or columns[0].get('sourceColumn') != requirement.get('source_column')
                or columns[0].get('isCalculated') or columns[0].get('expression')):
            raise _rls_error(
                source, table_name,
                f"security field {field!r} no longer binds the original "
                "physical column after model transformations.")


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
    required_roles = []

    model_tables = model.get('model', {}).get('tables', [])
    table_names = {table.get('name') for table in model_tables if table.get('name')}

    def _security_column(field, user_filter):
        if not isinstance(field, str) or not field:
            raise _rls_error(user_filter, main_table_name,
                             "the source security column is missing.")
        matches = []
        for table in model_tables:
            table_name = table.get('name') or ''
            for column in table.get('columns', []) or []:
                aliases = {
                    value for value in (column.get('name'), column.get('sourceColumn'))
                    if isinstance(value, str) and value
                }
                if any(alias.casefold() == field.casefold() for alias in aliases):
                    matches.append((table_name, column, aliases))
        if len(matches) != 1 or field not in matches[0][2]:
            raise _rls_error(
                user_filter, main_table_name,
                f"security field {field!r} is missing, case-only, or ambiguous "
                "among the active model columns.")
        table_name, column, _ = matches[0]
        if column.get('isCalculated') or column.get('expression'):
            raise _rls_error(
                user_filter, table_name,
                f"security field {field!r} is calculated and its complete "
                "security/filter scope has not been independently proven.")
        return table_name, column

    def _record_role(role, user_filter, column=None):
        permission = role['tablePermissions'][0]
        required_roles.append({
            'name': role['name'],
            'source': user_filter.get('datasource') or permission['name'],
            'table': permission['name'],
            'expression': permission['filterExpression'],
            'field': column.get('name') if column is not None else None,
            'source_column': column.get('sourceColumn') if column is not None else None,
        })
        roles.append(role)

    for uf in user_filters:
        if not isinstance(uf, dict):
            raise _rls_error({}, main_table_name, "the source security object is invalid.")
        uf_type = uf.get('type', '')
        if not uf_type and uf.get('formula'):
            uf_type = 'calculated_security'
        elif not uf_type and (uf.get('column') or uf.get('field')):
            uf_type = 'user_filter'

        if uf_type == 'user_filter':
            filter_name = uf.get('name', 'UserFilter')
            column = uf.get('column') or uf.get('field') or ''
            user_mappings = uf.get('user_mappings', [])
            col_clean = column
            if ':' in col_clean:
                col_clean = col_clean.split(':')[-1]
            table_name, security_column = _security_column(col_clean, uf)
            col_clean = security_column['name']
            if uf.get('group_filter'):
                raise _rls_error(
                    uf, table_name,
                    "the source group-filter semantics were not mapped to an "
                    "independently verified security role.")
            if not user_mappings and isinstance(uf.get('users'), dict):
                user_mappings = [
                    {'user': user, 'value': value}
                    for user, values in uf['users'].items()
                    for value in (values if isinstance(values, list) else [values])
                ]
            escaped_column = col_clean.replace(']', ']]')

            if user_mappings:
                user_values = {}
                for mapping in user_mappings:
                    if not isinstance(mapping, dict):
                        raise _rls_error(uf, table_name,
                                         "a source user mapping is malformed.")
                    user = mapping.get('user', '')
                    val = mapping.get('value')
                    if not isinstance(user, str) or not user or val is None:
                        raise _rls_error(
                            uf, table_name,
                            "a source user mapping is missing its user or value.")
                    user_values.setdefault(user, []).append(val)

                or_clauses = []
                for user_email, values in user_values.items():
                    if len(values) == 1:
                        val_expr = f'[{escaped_column}] == {_dax_string(values[0])}'
                    else:
                        val_list = ', '.join(_dax_string(v) for v in values)
                        val_expr = f'[{escaped_column}] IN {{{val_list}}}'
                    or_clauses.append(
                        f'(USERPRINCIPALNAME() == {_dax_string(user_email)} && {val_expr})'
                    )

                if or_clauses:
                    filter_dax = ' || '.join(or_clauses)
                else:
                    filter_dax = 'FALSE()'

                role_name = _unique_role_name(filter_name, role_names)
                role_names.add(role_name)

                _record_role({
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
                    ),
                    "_user_mappings": user_mappings
                }, uf, security_column)

            elif column:
                filter_dax = (
                    "NOT(ISBLANK(USERPRINCIPALNAME())) && "
                    "LEN(USERPRINCIPALNAME()) > 0 && "
                    f"NOT(ISBLANK([{escaped_column}])) && "
                    f"[{escaped_column}] == USERPRINCIPALNAME()"
                )
                role_name = _unique_role_name(filter_name, role_names)
                role_names.add(role_name)

                _record_role({
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
                        "without explicit user mappings."
                    )
                }, uf, security_column)

        elif uf_type == 'calculated_security':
            calc_name = uf.get('name', 'SecurityCalc')
            formula = uf.get('formula', '')
            ismemberof_groups = uf.get('ismemberof_groups', [])
            if not isinstance(formula, str) or not formula.strip():
                raise _rls_error(uf, main_table_name, "the source predicate is missing.")
            group_match = re.fullmatch(
                r"""ISMEMBEROF\s*\(\s*(?P<quote>["'])(?P<group>[^"']+)"""
                r"""(?P=quote)\s*\)""", formula.strip(), re.IGNORECASE)
            if ismemberof_groups or re.search(r'\bISMEMBEROF\s*\(', formula, re.I):
                if (not group_match or
                        (ismemberof_groups and ismemberof_groups != [group_match['group']])):
                    raise _rls_error(
                        uf, main_table_name,
                        "group membership is combined with another predicate or "
                        "its source group identity is ambiguous.")
                if main_table_name not in table_names:
                    raise _rls_error(uf, main_table_name,
                                     "the group role's permission table is missing.")
                group = group_match['group']
                role_name = _unique_role_name(group, role_names)
                role_names.add(role_name)
                _record_role({
                    "name": role_name,
                    "modelPermission": "read",
                    "tablePermissions": [{
                        "name": main_table_name,
                        # This is an intentional group-only role, not a
                        # conversion fallback; membership must be assigned.
                        "filterExpression": "TRUE()",
                    }],
                    "_migration_note": (
                        f"Migrated from Tableau ISMEMBEROF(\"{group}\"). "
                        "Assign the verified Azure AD group members to this RLS role."
                    ),
                }, uf)
                continue

            simple = _simple_identity_comparison(formula)
            if not simple:
                # Supply a real table-column context even for rejected forms:
                # omitting it bypasses the converter's unverified FIXED guard.
                local_columns = {
                    column.get('name')
                    for table in model_tables if table.get('name') == main_table_name
                    for column in table.get('columns', []) or []
                    if column.get('name')
                }
                try:
                    guarded = convert_tableau_formula_to_dax(
                        formula, table_name=main_table_name,
                        column_table_map=column_table_map,
                        table_columns=local_columns, is_calc_column=True,
                        validate_output=True, fallback_on_invalid=True)
                except Exception as exc:
                    raise _rls_error(
                        uf, main_table_name,
                        "the complete source predicate could not be validated.") from exc
                reason = (
                    "the source predicate requires unverified calculation, "
                    "FIXED grain or filter-scope semantics."
                    if not guarded or 'TODO: DAX conversion validation failed' in guarded
                    else "the complete source predicate is outside the verified "
                         "single-field identity comparison."
                )
                raise _rls_error(uf, main_table_name, reason)

            field, identity_function = simple
            perm_table, security_column = _security_column(field, uf)
            field_name = security_column['name']
            if '[' in field_name or ']' in field_name:
                raise _rls_error(
                    uf, perm_table,
                    "the emitted security field has escaped brackets whose "
                    "row-level binding was not verified.")
            local_columns = {
                column.get('name')
                for table in model_tables if table.get('name') == perm_table
                for column in table.get('columns', []) or []
                if column.get('name')
            }
            sentinel = '__TTPBI_RLS_IDENTITY__'
            expression = f'[{field_name}] = "{sentinel}"'
            owners = dict(column_table_map or {})
            owners[field_name] = perm_table
            try:
                converted = convert_tableau_formula_to_dax(
                    expression, column_name=calc_name, table_name=perm_table,
                    column_table_map=owners, table_columns=local_columns,
                    is_calc_column=True, validate_output=True,
                    fallback_on_invalid=True)
            except Exception as exc:
                raise _rls_error(
                    uf, perm_table, "the identity comparison could not be validated.") from exc
            if (not converted or 'TODO: DAX conversion validation failed' in converted
                    or f'"{sentinel}"' not in converted):
                raise _rls_error(
                    uf, perm_table,
                    "the identity comparison could not be converted with a "
                    "verified row-level field binding.")
            converted = converted.replace(f'"{sentinel}"', 'USERPRINCIPALNAME()')
            dax_filter = (
                "NOT(ISBLANK(USERPRINCIPALNAME())) && "
                "LEN(USERPRINCIPALNAME()) > 0 && "
                f"COALESCE({converted}, FALSE())"
            )
            from powerbi_import.dax_validator import validate_dax_expression
            if validate_dax_expression(dax_filter):
                raise _rls_error(
                    uf, perm_table, "the resulting RLS expression failed DAX validation.")
            role_name = _unique_role_name(calc_name, role_names)
            role_names.add(role_name)
            _record_role({
                "name": role_name,
                "modelPermission": "read",
                "tablePermissions": [{
                    "name": perm_table,
                    "filterExpression": dax_filter,
                }],
                "_migration_note": (
                    f"Migrated from Tableau calculated security '{calc_name}'. "
                    f"{identity_function} uses USERPRINCIPALNAME(); verify the "
                    "source-to-Power BI identity mapping."
                ),
            }, uf, security_column)
        else:
            raise _rls_error(uf, main_table_name,
                             "the source security filter type is missing or unsupported.")

    if roles:
        model["model"]["roles"] = roles
        model['_rls_required_roles'] = required_roles
        _validate_rls_bindings(model)
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
