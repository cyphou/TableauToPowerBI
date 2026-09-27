"""Power BI hierarchies: Tableau drill-paths and generated date hierarchies.

``_apply_hierarchies`` carries over the drill-paths the workbook declares.
``_auto_date_hierarchies`` adds Year > Quarter > Month > Day over any date
column not already in a declared hierarchy -- it operates on every table, not
just the Calendar, which is why it does not live with the date table.
Extracted from ``tmdl_generator``, which re-exports these names.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))
from m_query_builder import m_transform_add_column  # noqa: E402

from powerbi_import.calc_column_utils import _m_identifier_needs_quoting
from powerbi_import.tmdl_m_conversion import _inject_m_steps_into_partition


def _apply_hierarchies(model, hierarchies, column_table_map):
    """Apply Tableau hierarchies (drill-paths) to the model."""
    if not hierarchies:
        return

    for h in hierarchies:
        h_name = h.get('name', '')
        levels = h.get('levels', [])
        if not h_name or not levels:
            continue

        first_level = levels[0]
        target_table_name = column_table_map.get(first_level, '')
        if not target_table_name:
            continue

        for table in model["model"]["tables"]:
            if table.get("name") == target_table_name:
                table_col_names = {col.get("name", "") for col in table.get("columns", [])}
                valid_levels = [l for l in levels if l in table_col_names]

                if valid_levels:
                    if "hierarchies" not in table:
                        table["hierarchies"] = []

                    hierarchy = {
                        "name": h_name,
                        "levels": [
                            {"name": lvl, "ordinal": idx, "column": lvl}
                            for idx, lvl in enumerate(valid_levels)
                        ]
                    }
                    table["hierarchies"].append(hierarchy)
                break


def _auto_date_hierarchies(model):
    """Auto-generate Year > Quarter > Month > Day hierarchies for date columns.

    For every date/dateTime column that does not already belong to a
    user-defined hierarchy, we create Power Query M columns
    (Date.Year, Date.QuarterOfYear, Date.Month, Date.Day)
    and a hierarchy definition on the same table.
    """
    DATE_TYPES = {'dateTime', 'date'}
    # (label, M function, BIM dataType, ordinal)
    PARTS = [
        ('Year', 'Date.Year', 'int64', 0),
        ('Quarter', 'Date.QuarterOfYear', 'int64', 1),
        ('Month', 'Date.Month', 'int64', 2),
        ('Day', 'Date.Day', 'int64', 3),
    ]

    for table in model.get('model', {}).get('tables', []):
        columns = table.get('columns', [])
        existing_hierarchies = table.get('hierarchies', [])

        # Collect columns already used in a hierarchy
        hier_cols = set()
        for h in existing_hierarchies:
            for lvl in h.get('levels', []):
                hier_cols.add(lvl.get('column', ''))

        existing_col_names = {c.get('name', '') for c in columns}

        m_steps = []  # M steps for this table

        for col in list(columns):  # iterate copy — we may append
            col_type = col.get('dataType', '')
            col_name = col.get('name', '')
            if col_type not in DATE_TYPES:
                continue
            if col_name in hier_cols:
                continue  # already in a user-defined hierarchy

            # Build hierarchy name scoped to the column
            hier_name = f"{col_name} Hierarchy"

            # Skip if we already auto-generated this one (idempotency)
            if any(h.get('name') == hier_name for h in existing_hierarchies):
                continue

            # Add M-based columns for the parts (skip if name clashes)
            calc_col_names = []
            for part_label, m_fn, dt, _ in PARTS:
                calc_name = f"{col_name} {part_label}"
                if calc_name in existing_col_names:
                    calc_col_names.append(calc_name)
                    continue  # already exists (e.g. from Tableau extraction)

                col_ref = (f'[#"{col_name}"]'
                           if _m_identifier_needs_quoting(col_name)
                           else f'[{col_name}]')
                m_steps.append(m_transform_add_column(
                    calc_name,
                    f'each {m_fn}({col_ref})',
                    'Int64.Type'
                ))
                columns.append({
                    'name': calc_name,
                    'dataType': dt,
                    'sourceColumn': calc_name,
                    'isHidden': True,
                })
                existing_col_names.add(calc_name)
                calc_col_names.append(calc_name)

            # Create the hierarchy
            hierarchy = {
                'name': hier_name,
                'levels': [
                    {'name': PARTS[i][0], 'ordinal': i, 'column': calc_col_names[i]}
                    for i in range(len(calc_col_names))
                ],
            }
            if 'hierarchies' not in table:
                table['hierarchies'] = []
            table['hierarchies'].append(hierarchy)

        # Inject accumulated M steps into the table's partition
        if m_steps:
            _inject_m_steps_into_partition(table, m_steps)
