"""DAX post-processing for the generated semantic model.

Everything `dax.agent.md` claims inside the TMDL generator, in one place: the
rewrites that make converted DAX loadable (unwrapping an aggregation around a
measure, aggregating a bare column reference), the RELATED -> LOOKUPVALUE
substitution for many-to-many pairs, and cross-table reference resolution.
Extracted from ``tmdl_generator``, which re-exports these names and calls into
them from `_build_table` and `_apply_semantic_enrichments` -- one direction,
no cycle.

Owned by **@dax**.
"""

import logging
import re

logger = logging.getLogger(__name__)


def resolve_table_for_column(column_name, datasource_name=None, dax_context=None):
    """Resolve which table a column belongs to, with optional datasource scoping.

    When a worksheet uses multiple datasources, ``datasource_name`` narrows
    the lookup to the tables that belong to that particular datasource.
    Falls back to the global ``column_table_map`` if no datasource-specific
    match is found.

    Args:
        column_name: Column name to resolve.
        datasource_name: Optional datasource name to scope the lookup.
        dax_context: DAX context dict containing ``column_table_map`` and
            ``ds_column_table_map``.

    Returns:
        str or None: Resolved table name, or *None* if unresolved.
    """
    if not dax_context:
        return None
    # Try datasource-specific lookup first
    if datasource_name:
        ds_map = dax_context.get('ds_column_table_map', {}).get(datasource_name, {})
        if column_name in ds_map:
            return ds_map[column_name]
    # Fallback to global map
    return dax_context.get('column_table_map', {}).get(column_name)


def resolve_table_for_formula(formula, datasource_name=None, dax_context=None):
    """Resolve the best target table for a DAX formula based on column references.

    Analyses ``[ColumnName]`` references in the formula and determines which
    table is referenced most frequently.  Useful for routing calculations that
    reference columns from multiple datasources.

    Args:
        formula: DAX formula string.
        datasource_name: Optional datasource name to scope the lookup.
        dax_context: DAX context dict.

    Returns:
        str or None: Best-fit table name, or *None* if unresolved.
    """
    if not formula or not dax_context:
        return None
    col_refs = re.findall(r'\[([^\]]+)\]', formula)
    if not col_refs:
        return None
    table_counts = {}
    for col in col_refs:
        tbl = resolve_table_for_column(col, datasource_name, dax_context)
        if tbl:
            table_counts[tbl] = table_counts.get(tbl, 0) + 1
    if not table_counts:
        return None
    return max(table_counts, key=lambda k: table_counts[k])


_BARE_COL_REF_RE = re.compile(r"^(?:'[^']*')?\[[^\]]+\]$")


def _wrap_bare_ref_expression(dax_formula, datatype, table_name):
    """Aggregate a measure whose whole expression is a bare column reference.

    Type-aware: MAXX over IF for Boolean (MAX needs a column ref), MAX for
    text and dates, SUM otherwise. Owned by @dax.
    """
    if not _BARE_COL_REF_RE.match(dax_formula.strip()):
        return dax_formula
    dt_lower = (datatype or '').lower()
    if dt_lower == 'boolean':
        tbl_esc = (table_name or '').replace("'", "''")
        return f"MAXX('{tbl_esc}', IF({dax_formula.strip()}, 1, 0))"
    if dt_lower in ('string', 'date', 'datetime'):
        return f"MAX({dax_formula.strip()})"
    return f"SUM({dax_formula.strip()})"


_BARE_REF_RE = re.compile(r"(?<!')\[([^\]]+)\]")


def _promote_measure_dependent_calc_columns(result_table):
    """Turn a calculated column that calls a measure into a measure.

    A calculated column cannot call a measure: the measure's filter context
    is unknown at row level, so the engine makes the column depend on its
    whole table, detects a circular dependency and refuses to load the
    model. Tableau's own formula was already aggregate-valued, so a measure
    is the faithful target. Owned by @dax.

    Runs before :func:`_unwrap_aggregations_of_measures` so that call sites
    written as ``MIN('T'[col])`` are rewritten to ``[col]`` by that pass, and
    before :func:`_wrap_bare_cross_table_refs` so the promoted expression's
    bare column refs get an aggregation.
    """
    measure_names = {m.get("name", "") for m in result_table.get("measures", [])}
    if not measure_names:
        return []

    promoted = []
    # A promotion can make another column measure-dependent, so iterate.
    for _ in range(len(result_table.get("columns", [])) + 1):
        kept, changed = [], False
        for col in result_table.get("columns", []):
            expr = col.get("expression", "")
            name = col.get("name", "")
            if not (col.get("isCalculated") and expr and name):
                kept.append(col)
                continue
            called = sorted({r for r in _BARE_REF_RE.findall(expr)
                             if r in measure_names})
            if not called or name in measure_names:
                kept.append(col)
                continue
            measure = {"name": name, "expression": expr}
            for key in ("formatString", "isHidden", "description",
                        "displayFolder"):
                if key in col:
                    measure[key] = col[key]
            result_table["measures"].append(measure)
            measure_names.add(name)
            promoted.append((name, called))
            changed = True
            logger.warning(
                "Calculated column '%s' calls measure(s) %s; promoted to a "
                "measure to avoid a circular dependency",
                name, ", ".join(called),
            )
        result_table["columns"] = kept
        if not changed:
            break
    return promoted


def _unwrap_aggregations_of_measures(result_table):
    """Strip SUM/AVERAGE/COUNT/MIN/MAX wrappers from measure references.

    DAX aggregations take a column; a measure already aggregates, so the
    wrapper has to go. Owned by @dax.
    """
    # ── Post-processing: fix SUM/AVG/COUNT/MIN/MAX of measure references ──
    # In DAX, SUM([X]) only accepts a column reference.  If [X] resolves to
    # a measure name, the aggregation wrapper must be removed because the
    # measure already aggregates internally.
    _all_measure_names = {m["name"] for m in result_table["measures"]}
    _qualified_measure_ref = re.compile(
        r"'([^']+(?:''[^']*)*)'\[([^\]]+)\]"
    )
    for meas in result_table["measures"]:
        expr = meas.get("expression", "")
        if not expr:
            continue
        normalized = _qualified_measure_ref.sub(
            lambda match: f"[{match.group(2)}]"
            if match.group(2) in _all_measure_names else match.group(0),
            expr,
        )
        meas["expression"] = normalized
    _AGG_OF_MEASURE_RE = re.compile(
        r'\b(SUM|AVERAGE|COUNT|MIN|MAX)\(\s*\[([^\]]+)\]\s*\)',
        re.IGNORECASE,
    )
    for meas in result_table["measures"]:
        expr = meas.get("expression", "")
        if not expr:
            continue
        new_expr = expr
        for m_agg in _AGG_OF_MEASURE_RE.finditer(expr):
            agg_fn = m_agg.group(1)
            ref_name = m_agg.group(2)
            if ref_name in _all_measure_names:
                # Replace SUM([measure]) with just [measure]
                new_expr = new_expr.replace(m_agg.group(0), f'[{ref_name}]')
                logger.debug(
                    "Unwrapped %s([%s]) → [%s] (measure reference, not column)",
                    agg_fn, ref_name, ref_name,
                )
        if new_expr != expr:
            meas["expression"] = new_expr



def _wrap_bare_cross_table_refs(result_table):
    """Wrap bare column references in a measure with an aggregation.

    Refs already inside an aggregation or iterator are left alone -- those
    supply the context a bare ref needs. Owned by @dax.
    """
    _all_measure_names = {m["name"] for m in result_table["measures"]}
    # ── Post-processing: wrap bare cross-table column refs in SUM ──
    # A DAX measure cannot reference a column from another table without
    # aggregation.  Pattern: 'Table'[Column] where Column is NOT a measure.
    # Only skip wrapping when the ref is inside an aggregation function
    # (SUM, MAX, etc. — already aggregated) or an iterator function
    # (SUMX, FILTER, etc. — provides row context).  Scalar functions
    # like IF, CONVERT, NOT, SWITCH do NOT provide aggregation or row
    # context, so column refs inside them still need wrapping.
    _XTABLE_COL_RE = re.compile(
        r"'([^']+(?:''[^']*)*)'(\[[^\]]+\])"
    )
    # Functions that provide column context (aggregation, iteration, or
    # column-reference semantics).  Column refs inside these do NOT need
    # SUM wrapping.
    _COLUMN_CONTEXT_FUNCS = frozenset({
        # Aggregation functions (column is their direct input)
        'SUM', 'AVERAGE', 'COUNT', 'COUNTA', 'COUNTBLANK', 'COUNTROWS',
        'MIN', 'MAX', 'DISTINCTCOUNT', 'DISTINCTCOUNTNOBLANK',
        'MEDIAN', 'PERCENTILE',
        # Iterator functions (provide row context in their body)
        'SUMX', 'AVERAGEX', 'COUNTX', 'MINX', 'MAXX', 'PRODUCTX',
        'CONCATENATEX', 'MEDIANX', 'PERCENTILEX',
        'FILTER', 'ADDCOLUMNS', 'SELECTCOLUMNS',
        'GENERATE', 'GENERATEALL', 'RANKX', 'TOPN',
        'SUMMARIZE', 'SUMMARIZECOLUMNS', 'GROUPBY',
        # Lookup / column-reference functions
        'RELATED', 'RELATEDTABLE', 'LOOKUPVALUE', 'TREATAS',
        'VALUES', 'DISTINCT', 'ALL', 'ALLEXCEPT', 'ALLNOBLANKROW',
        'ALLSELECTED', 'REMOVEFILTERS',
        'EARLIER', 'EARLIEST', 'SELECTEDVALUE',
        'HASONEVALUE', 'HASONEFILTER', 'ISINSCOPE',
        'USERELATIONSHIP', 'CROSSFILTER', 'CALCULATETABLE',
    })
    # Build set of boolean columns in the current table — SUM/MAX don't
    # support Boolean type, so these need special wrapping (IF(col,1,0)).
    _bool_cols_for_xtable = {
        c.get('name', '') for c in result_table.get("columns", [])
        if (c.get('dataType', '') or '').lower() == 'boolean'
        and c.get('name')
    }
    # String/DateTime columns should use MAX, not SUM (SUM is invalid for text/dates).
    _text_date_cols_for_xtable = {
        c.get('name', '') for c in result_table.get("columns", [])
        if (c.get('dataType', '') or '').lower() in ('string', 'datetime')
        and c.get('name')
    }
    for meas in result_table["measures"]:
        expr = meas.get("expression", "")
        if not expr:
            continue
        new_expr = expr
        for m_col in reversed(list(_XTABLE_COL_RE.finditer(expr))):
            col_name = m_col.group(2).strip('[]')
            if col_name in _all_measure_names:
                continue  # measure ref — leave as-is
            # Walk expression up to match position tracking whether we
            # are inside an aggregation/iterator function.  Only those
            # functions provide contexts where bare column refs are valid.
            # Scalar functions (IF, CONVERT, NOT, SWITCH …) do NOT
            # provide aggregation or row context.
            agg_depth = 0
            func_stack = []   # True/False per paren level
            text_before = expr[:m_col.start()]
            i = 0
            while i < len(text_before):
                ch = text_before[i]
                if ch == '"':
                    # Skip DAX string literal ("" = escaped double-quote)
                    i += 1
                    while i < len(text_before):
                        if text_before[i] == '"':
                            if (i + 1 < len(text_before)
                                    and text_before[i + 1] == '"'):
                                i += 2  # escaped double-quote
                            else:
                                break
                        i += 1
                    i += 1  # skip closing quote
                elif ch == '(':
                    # Look backwards past spaces to find the function name
                    j = i - 1
                    while j >= 0 and text_before[j] == ' ':
                        j -= 1
                    func_end = j + 1
                    while j >= 0 and (text_before[j].isalnum()
                                      or text_before[j] == '_'):
                        j -= 1
                    func_name = text_before[j + 1:func_end].upper()
                    is_ctx = func_name in _COLUMN_CONTEXT_FUNCS
                    func_stack.append(is_ctx)
                    if is_ctx:
                        agg_depth += 1
                    i += 1
                elif ch == ')':
                    if func_stack:
                        if func_stack.pop():
                            agg_depth -= 1
                    i += 1
                else:
                    i += 1
            if agg_depth > 0:
                continue  # Inside aggregation/iterator — row-level ref
            # Bare column ref not inside any aggregation → wrap.
            # Type-aware: SUM for numeric, MAX for string/date,
            # MAXX('Table', IF(col, 1, 0)) for Boolean.
            old_ref = m_col.group(0)
            tbl_name = m_col.group(1).replace("''", "'")
            is_same_table = tbl_name == result_table.get("name", "")
            if is_same_table and col_name in _bool_cols_for_xtable:
                tbl_esc = tbl_name.replace("'", "''")
                new_ref = f"MAXX('{tbl_esc}', IF({old_ref}, 1, 0))"
            elif is_same_table and col_name in _text_date_cols_for_xtable:
                new_ref = f"MAX({old_ref})"
            else:
                new_ref = f"SUM({old_ref})"
            new_expr = new_expr[:m_col.start()] + new_ref + new_expr[m_col.end():]
            logger.debug(
                "Wrapped bare column ref %s → SUM(%s) in measure '%s'",
                old_ref, old_ref, meas.get("name", ""),
            )
        if new_expr != expr:
            meas["expression"] = new_expr



def _validate_measures_after_rewrites(result_table):
    """Re-validate every measure once the DAX rewrites have run."""
    # ── Phase 3: Post-processing DAX validation sweep ──
    # After all rewrites (SUM-of-measure unwrap, bare column wrapping),
    # validate every measure expression one more time.
    try:
        from powerbi_import.dax_validator import validate_dax_expression as _validate_dax
        for meas in result_table["measures"]:
            m_expr = meas.get("expression", "")
            if not m_expr or 'TODO: DAX conversion validation failed' in m_expr:
                continue
            issues = _validate_dax(m_expr)
            if issues:
                logger.warning(
                    "Post-processing DAX validation issue in measure '%s': %s",
                    meas.get("name", ""), issues[0],
                )
    except Exception:
        pass  # validator must never block generation


def _fix_related_for_many_to_many(model):
    """
    Replace RELATED('table'[col]) with LOOKUPVALUE() for manyToMany relationships.
    """
    # Build lookup by full relationship identity. Keep the legacy table-pair
    # form as a fallback for callers/tests that provide synthetic mappings.
    m2m_pairs = {}
    for rel in model['model']['relationships']:
        if rel.get('fromCardinality') == 'many' and rel.get('toCardinality') == 'many':
            to_table = rel.get('toTable', '')
            to_col = rel.get('toColumn', '')
            from_table = rel.get('fromTable', '')
            from_col = rel.get('fromColumn', '')
            # From from_table context, RELATED('to_table'[x]) uses to_col ↔ from_col
            m2m_pairs.setdefault((from_table, to_table), (to_col, from_col))
            # From to_table context, RELATED('from_table'[x]) uses from_col ↔ to_col
            m2m_pairs.setdefault((to_table, from_table), (from_col, to_col))

    if not m2m_pairs:
        return

    for table in model['model']['tables']:
        current_table = table.get('name', '')
        for col in table.get('columns', []):
            expr = col.get('expression', '')
            if expr and 'RELATED(' in expr:
                # Bug #21 fix: Use LOOKUPVALUE() for calc columns, not CALCULATE(SELECTEDVALUE())
                # CALCULATE() doesn't work in row context (calc column context).
                # LOOKUPVALUE() works in both row and filter context and supports all types.
                col['expression'] = _replace_related_with_lookupvalue(
                    expr, m2m_pairs, current_table,
                    use_calculate_selectedvalue=False)
        for measure in table.get('measures', []):
            expr = measure.get('expression', '')
            if expr and 'RELATED(' in expr:
                # First pass: replace RELATED using the measure's own table
                expr = _replace_related_with_lookupvalue(
                    expr, m2m_pairs, current_table)
                # Second pass: replace RELATED inside SUMX/AVERAGEX/etc.
                # where the iteration table is the context, not the measure table
                expr = _replace_related_in_aggx_context(expr, m2m_pairs)
                measure['expression'] = expr


def _replace_related_with_lookupvalue(expr, m2m_pairs, current_table='',
                                      use_calculate_selectedvalue=False):
    """Replace RELATED('table'[col]) with LOOKUPVALUE() for m2m tables.

    When *use_calculate_selectedvalue* is True (calculated columns),
    generates ``CALCULATE(SELECTEDVALUE('table'[col]))`` instead of
    ``LOOKUPVALUE()``.  SELECTEDVALUE works for **all** data types
    including Boolean (unlike MIN/MAX which fail on Boolean).  It returns
    the column value when filter context yields a single distinct value,
    or BLANK() when there are zero or multiple distinct values.
    """
    pattern = r"RELATED\(('([^']+)'|([A-Za-z0-9_][A-Za-z0-9_ .-]*))\[([^\]]*(?:\]\][^\]]*)*)\]\)"

    def replacer(match):
        table_name = match.group(2) if match.group(2) else match.group(3)
        col_name = match.group(4)

        pair_key = (current_table, table_name)
        if pair_key not in m2m_pairs:
            return match.group(0)

        ref_join_col, current_join_col = m2m_pairs[pair_key]

        # Escape apostrophes in TMDL table names ('O''Reilly')
        t_esc = table_name.replace("'", "''")
        ct_esc = current_table.replace("'", "''")
        t_ref = f"'{t_esc}'" if not table_name.isidentifier() else table_name
        ct_ref = f"'{ct_esc}'" if not current_table.isidentifier() else current_table

        if use_calculate_selectedvalue:
            # SELECTEDVALUE works for all types (Boolean, String, Date,
            # Numeric).  Returns the value when filter context narrows to
            # one distinct value, BLANK() otherwise.
            return f"CALCULATE(SELECTEDVALUE({t_ref}[{col_name}]))"

        return f"LOOKUPVALUE({t_ref}[{col_name}], {t_ref}[{ref_join_col}], {ct_ref}[{current_join_col}])"

    return re.sub(pattern, replacer, expr)


def _replace_related_in_aggx_context(expr, m2m_pairs):
    """Replace RELATED() inside SUMX/AVERAGEX/etc. using the iteration table.

    Inside ``SUMX('Opportunities', ...RELATED('Created By'[col])...)``,
    the RELATED navigates from ``Opportunities`` (iteration context) to
    ``Created By``.  The standard ``_replace_related_with_lookupvalue``
    uses the measure's own table, which is wrong for iterator context.
    """
    if 'RELATED(' not in expr:
        return expr

    aggx_pattern = re.compile(
        r'\b(SUMX|AVERAGEX|MINX|MAXX|COUNTX|STDEVX\.S|STDEVX\.P|MEDIANX)\s*\(\s*'
        r"'([^']+)'\s*,\s*",
        re.IGNORECASE)

    result = expr
    for m in reversed(list(aggx_pattern.finditer(expr))):
        iter_table = m.group(2)
        body_start = m.end()
        # Find the matching closing paren for the AGGX call
        depth = 1
        pos = body_start
        while pos < len(result) and depth > 0:
            if result[pos] == '(':
                depth += 1
            elif result[pos] == ')':
                depth -= 1
            pos += 1
        if depth != 0:
            continue
        body = result[body_start:pos - 1]
        if 'RELATED(' not in body:
            continue
        new_body = _replace_related_with_lookupvalue(
            body, m2m_pairs, iter_table)
        if new_body != body:
            result = result[:body_start] + new_body + result[pos - 1:]

    return result
