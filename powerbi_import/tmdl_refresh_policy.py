"""Incremental refresh policy detection and generation.

Decides which tables can carry a Power BI incremental refresh policy, picks
the date column to partition on, and emits the RangeStart/RangeEnd M
parameters and the filter step that uses them. Extracted from tmdl_generator
so the refresh surface is one concern in one place; tmdl_generator re-exports
these names for backward compatibility.
"""

import logging
import re

logger = logging.getLogger(__name__)


def detect_refresh_policy(table, datasources=None):
    """Auto-detect an incremental refresh policy for a table.

    If the table has a DateTime column and comes from a relational data source,
    generate default policy settings. Users should refine these.

    Args:
        table: Table dict with 'columns' list.
        datasources: Optional list of datasource dicts for connection type detection.

    Returns:
        dict with policy settings, or None if not applicable.
    """
    date_cols = []
    for col in table.get('columns', []):
        dt = (col.get('dataType') or col.get('type') or '').lower()
        name = (col.get('name') or '').lower()
        if 'date' in dt or 'datetime' in dt or 'timestamp' in dt:
            date_cols.append(col)
        elif any(kw in name for kw in ('date', 'datetime', 'timestamp', 'created_at', 'updated_at')):
            date_cols.append(col)

    if not date_cols:
        return None

    # Pick the best candidate date column
    best = date_cols[0]
    for c in date_cols:
        cn = (c.get('name') or '').lower()
        if any(kw in cn for kw in ('updated', 'modified', 'last_')):
            best = c
            break

    col_name = best.get('name', 'Date')

    # Build M polling expression
    polling = f'let\n    currentDate = DateTime.LocalNow(),\n    #"MaxDate" = Sql.Database("server", "db"){{[Schema="dbo",Item="{table.get("name", "Table")}"]}}[{col_name}],\n    maxVal = List.Max(#"MaxDate")\nin\n    maxVal'

    return {
        'incrementalGranularity': 'Day',
        'incrementalPeriods': 3,
        'rollingWindowGranularity': 'Month',
        'rollingWindowPeriods': 12,
        'pollingExpression': polling,
        'sourceExpression': '',
        'dateColumn': col_name,
    }


# ── Incremental Refresh Detection & Wiring (Sprint 120) ──────────────────────

_INCREMENTAL_CONNECTORS = frozenset({
    'sqlserver', 'postgres', 'postgresql', 'oracle', 'mysql', 'snowflake',
    'redshift', 'bigquery', 'databricks', 'azure_sql_dw', 'synapse',
    'teradata', 'sap_hana', 'vertica', 'db2', 'netezza',
})

_DATE_TYPE_KEYWORDS = frozenset({
    'date', 'datetime', 'datetime2', 'datetimeoffset', 'timestamp',
    'smalldatetime', 'timestamptz', 'timestamp_ntz', 'timestamp_ltz',
})

_DATE_COL_KEYWORDS = (
    'date', 'datetime', 'timestamp', 'created', 'modified', 'updated',
    'created_at', 'updated_at', 'modified_at', 'last_modified',
    'order_date', 'transaction_date', 'event_date', 'load_date',
)


def _detect_incremental_refresh_tables(model, datasources=None):
    """Detect tables eligible for incremental refresh.

    A table qualifies when:
      1. It has at least one DateTime/Date column.
      2. Its datasource connection uses a query-foldable connector
         (SQL Server, PostgreSQL, Oracle, etc.).
      3. It is **not** a calculated table, Calendar, or parameter table.

    Args:
        model: Semantic model dict (from ``_build_semantic_model``).
        datasources: Raw datasource list for connector-type detection.

    Returns:
        list of (table_dict, date_column_name) tuples for eligible tables.
    """
    # Build connector lookup from datasources
    connector_types = set()
    if datasources:
        for ds in (datasources if isinstance(datasources, list) else [datasources]):
            conn = ds.get('connection', {})
            ctype = (conn.get('class', '') or conn.get('type', '')).lower().replace(' ', '_')
            connector_types.add(ctype)
            # Also check connection_map entries
            for cmap_val in ds.get('connection_map', {}).values():
                if isinstance(cmap_val, dict):
                    cm_class = (cmap_val.get('class', '') or '').lower().replace(' ', '_')
                    if cm_class:
                        connector_types.add(cm_class)

    has_foldable = bool(connector_types & _INCREMENTAL_CONNECTORS) if connector_types else True

    skip_tables = {'Calendar', 'DateTableTemplate_'}
    results = []

    for table in model.get('model', {}).get('tables', []):
        tname = table.get('name', '')
        # Skip calculated tables, Calendar, parameter tables
        if tname in skip_tables or tname.startswith('DateTableTemplate_'):
            continue
        partitions = table.get('partitions', [])
        if partitions:
            src_type = partitions[0].get('source', {}).get('type', 'm')
            if src_type in ('calculated', 'calculationGroup'):
                continue

        # Check partition mode — only import mode tables
        if partitions and partitions[0].get('mode', 'import') != 'import':
            continue

        if not has_foldable:
            continue

        # Find best date column
        best_date_col = _pick_best_date_column(table)
        if best_date_col:
            results.append((table, best_date_col))

    return results


def _pick_best_date_column(table):
    """Pick the best date column for incremental refresh from a table.

    Preference order:
      1. Columns with 'updated'/'modified'/'last_' in the name
      2. Columns with DateTime/Date data type
      3. Columns with date-like names (order_date, created_at, etc.)

    Returns:
        str: Column name, or None if no suitable column found.
    """
    date_cols = []
    for col in table.get('columns', []):
        dt = (col.get('dataType') or col.get('type') or '').lower()
        name = (col.get('name') or '')
        name_lower = name.lower()

        is_date_type = any(kw in dt for kw in _DATE_TYPE_KEYWORDS)
        is_date_name = any(kw in name_lower for kw in _DATE_COL_KEYWORDS)

        if is_date_type or is_date_name:
            date_cols.append(col)

    if not date_cols:
        return None

    # Prefer updated/modified columns
    for c in date_cols:
        cn = (c.get('name') or '').lower()
        if any(kw in cn for kw in ('updated', 'modified', 'last_')):
            return c.get('name', '')

    return date_cols[0].get('name', '')


def _generate_refresh_policy(table_name, date_column, rolling_months=12,
                              incremental_days=3):
    """Generate an incremental refresh policy dict for a table.

    Args:
        table_name: Name of the target table.
        date_column: Name of the DateTime column to filter on.
        rolling_months: Total rolling window in months (default: 12).
        incremental_days: Days to incrementally refresh (default: 3).

    Returns:
        dict: refreshPolicy ready for ``_write_refresh_policy``.
    """
    # Build M polling expression to detect new data
    polling_m = (
        f'let\n'
        f'    Source = #"{table_name}",\n'
        f'    MaxDate = List.Max(Source[{date_column}])\n'
        f'in\n'
        f'    MaxDate'
    )

    # Build source expression with RangeStart/RangeEnd filtering
    source_m = (
        f'let\n'
        f'    Source = #"{table_name}",\n'
        f'    #"Filtered Rows" = Table.SelectRows(Source, each [{date_column}] >= RangeStart and [{date_column}] < RangeEnd)\n'
        f'in\n'
        f'    #"Filtered Rows"'
    )

    return {
        'incrementalGranularity': 'Day',
        'incrementalPeriods': incremental_days,
        'rollingWindowGranularity': 'Month',
        'rollingWindowPeriods': rolling_months,
        'pollingExpression': polling_m,
        'sourceExpression': source_m,
        'dateColumn': date_column,
    }


def _inject_range_filter_m(m_expression, date_column):
    """Inject RangeStart/RangeEnd filter into an existing M partition expression.

    Adds a ``Table.SelectRows`` step that filters the date column between
    the ``RangeStart`` and ``RangeEnd`` Power Query parameters.

    Args:
        m_expression: Original M query string.
        date_column: Column name to filter on.

    Returns:
        str: Modified M expression with range filtering.
    """
    if not m_expression or not date_column:
        return m_expression

    # Already has range filter
    if 'RangeStart' in m_expression and 'RangeEnd' in m_expression:
        return m_expression

    # Find the final step name (the identifier after 'in')
    in_match = re.search(r'\bin\s*\r?\n\s*(\S+)\s*$', m_expression, re.DOTALL)
    if not in_match:
        return m_expression

    final_step = in_match.group(1).strip()
    col_ref = f'[{date_column}]'

    filter_step_name = '#"Incremental Filter"'
    filter_step = (
        f'    {filter_step_name} = Table.SelectRows({final_step}, '
        f'each {col_ref} >= RangeStart and {col_ref} < RangeEnd)'
    )

    # Insert filter step before 'in' and update final reference
    parts = m_expression.rsplit('\nin', 1)
    if len(parts) == 2:
        new_m = f'{parts[0]},\n{filter_step}\nin\n    {filter_step_name}'
    else:
        parts = m_expression.rsplit('\r\nin', 1)
        if len(parts) == 2:
            new_m = f'{parts[0]},\n{filter_step}\nin\n    {filter_step_name}'
        else:
            new_m = m_expression

    return new_m


def _generate_incremental_m_parameters():
    """Generate RangeStart and RangeEnd M parameter expressions.

    These are the standard Power BI parameters that define the incremental
    refresh window boundaries.

    Returns:
        list of (name, m_expression) tuples.
    """
    range_start = (
        '#datetime(2020, 1, 1, 0, 0, 0) '
        'meta [IsParameterQuery=true, Type="DateTime", '
        'IsParameterQueryRequired=true]'
    )
    range_end = (
        '#datetime(2030, 12, 31, 23, 59, 59) '
        'meta [IsParameterQuery=true, Type="DateTime", '
        'IsParameterQueryRequired=true]'
    )
    return [
        ('RangeStart', range_start),
        ('RangeEnd', range_end),
    ]


def apply_incremental_refresh(model, datasources=None, rolling_months=12,
                               incremental_days=3, parameterize=True):
    """Apply incremental refresh to eligible tables in the semantic model.

    This is the main orchestrator for Sprint 120 incremental refresh wiring.
    It detects eligible tables, generates refresh policies, injects M range
    filters, and prepares RangeStart/RangeEnd parameters.

    Args:
        model: Semantic model dict (from ``_build_semantic_model``).
        datasources: Raw datasource list for connector detection.
        rolling_months: Rolling window size in months (default: 12).
        incremental_days: Incremental refresh period in days (default: 3).
        parameterize: If True, inject RangeStart/RangeEnd M parameters and
                      modify partition M expressions with range filters.

    Returns:
        dict with keys:
          - 'tables_configured': list of table names with refresh policies
          - 'parameters_added': list of parameter names added
          - 'date_columns': dict of table_name -> date_column used
    """
    eligible = _detect_incremental_refresh_tables(model, datasources)
    if not eligible:
        logger.info("No tables eligible for incremental refresh")
        return {'tables_configured': [], 'parameters_added': [], 'date_columns': {}}

    tables_configured = []
    date_columns = {}

    for table, date_col in eligible:
        tname = table.get('name', '')

        # Generate and attach refresh policy
        policy = _generate_refresh_policy(
            tname, date_col,
            rolling_months=rolling_months,
            incremental_days=incremental_days,
        )
        table['refreshPolicy'] = policy
        tables_configured.append(tname)
        date_columns[tname] = date_col

        # Inject RangeStart/RangeEnd filter into partition M expression
        if parameterize:
            for partition in table.get('partitions', []):
                source = partition.get('source', {})
                if isinstance(source, dict):
                    expr = source.get('expression', '')
                    if expr:
                        source['expression'] = _inject_range_filter_m(expr, date_col)

        logger.info("Incremental refresh configured for table '%s' on column '%s'",
                     tname, date_col)

    # Track parameters to add
    params_added = []
    if parameterize and tables_configured:
        params_added = ['RangeStart', 'RangeEnd']
        # Store on model so _write_expressions_tmdl can emit them
        model.setdefault('_incremental_params', _generate_incremental_m_parameters())

    return {
        'tables_configured': tables_configured,
        'parameters_added': params_added,
        'date_columns': date_columns,
    }
