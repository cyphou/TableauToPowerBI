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

#: ``'Sales'[Amount]``, ``Sales[Amount]`` or a bare ``[Amount]``.
_DAX_COLUMN_REF = re.compile(
    r"""(?P<owner>'(?:[^']|'')+'|\b[A-Za-z_]\w*)?\[(?P<col>[^\[\]]+)\]"""
)


def _split_dax_strings(expr):
    """Yield ``(text, is_literal)`` so rewrites never reach inside a string."""
    parts, buf, in_string, i = [], [], False, 0
    while i < len(expr):
        ch = expr[i]
        if ch == '"':
            if in_string and i + 1 < len(expr) and expr[i + 1] == '"':
                buf.append('""')
                i += 2
                continue
            buf.append(ch)
            parts.append((''.join(buf), in_string))
            buf, in_string = [], not in_string
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        parts.append((''.join(buf), in_string))
    return parts


def retarget_dax_column_refs(model):
    """Point DAX column references at the names the model actually carries.

    A captioned column is renamed by its partition, so the model names it
    after what the partition produces.  Expressions written against the
    Tableau name have to follow, or they resolve to nothing.

    Returns the number of expressions rewritten.
    """
    renames = model.get('_column_rename_map') or {}
    if not renames:
        return 0

    by_table = {}
    for (table_name, raw), produced in renames.items():
        by_table.setdefault(table_name, {})[raw] = produced

    def rewrite(expr, home_table):
        def _swap(match):
            owner = match.group('owner')
            column = match.group('col')
            if owner:
                key = owner[1:-1].replace("''", "'") if owner.startswith("'") else owner
            else:
                key = home_table
            target = by_table.get(key, {}).get(column)
            if not target:
                return match.group(0)
            return f'{owner or ""}[{target}]'

        return ''.join(
            chunk if literal else _DAX_COLUMN_REF.sub(_swap, chunk)
            for chunk, literal in _split_dax_strings(expr)
        )

    rewritten = 0
    for table in model.get('model', {}).get('tables', []):
        home = table.get('name', '')
        for holder in (table.get('columns', []), table.get('measures', [])):
            for item in holder:
                expression = item.get('expression')
                if not expression or '[' not in expression:
                    continue
                updated = rewrite(expression, home)
                if updated != expression:
                    item['expression'] = updated
                    rewritten += 1
    if rewritten:
        logger.debug("Retargeted %d DAX expression(s) onto renamed columns",
                     rewritten)
    return rewritten


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
        from powerbi_import.dax_validator import (
            validate_dax_expression as _validate_dax,
        )
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


# Scoped logical-relationship compiler. This deliberately does not use the
# permissive raw translator or mutate model relationships/partitions. Only the
# requested aggregate dependency closure is evaluated at relationship-pair grain.

class _Unsupported(ValueError):
    pass


def _table(name):
    return "'" + name.replace("'", "''") + "'"


def _column(table, column):
    return _table(table) + "[" + column.replace("]", "]]") + "]"


def _identity(name):
    """Decode one source identifier, not arbitrary embedded brackets."""
    if name.startswith("[") and name.endswith("]"):
        return name[1:-1].replace("]]", "]")
    return name


def _tokens(text):
    result, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise _Unsupported("Unclosed source comment")
            i = end + 2
        elif ch in "\"'[":
            close = "]" if ch == "[" else ch
            value, i = [], i + 1
            while i < len(text):
                if text[i] == close:
                    if i + 1 < len(text) and text[i + 1] == close:
                        value.append(close)
                        i += 2
                        continue
                    i += 1
                    break
                if ch != "[" and text[i] == "\\":
                    i += 1
                    if i >= len(text):
                        raise _Unsupported("Unclosed source string escape")
                    escapes = {"n": "\n", "r": "\r", "t": "\t",
                               "\\": "\\", "'": "'", '"': '"'}
                    if text[i] not in escapes:
                        raise _Unsupported("Unsupported source string escape")
                    value.append(escapes[text[i]])
                else:
                    value.append(text[i])
                i += 1
            else:
                raise _Unsupported("Unclosed source identifier or string")
            result.append(("REF" if ch == "[" else "STR", "".join(value)))
        else:
            match = re.match(r"\d+(?:\.\d+)?|[^\W\d]\w*|>=|<=|<>|!=|==|[=<>(),.:{}]",
                             text[i:], re.UNICODE)
            if not match:
                raise _Unsupported("Unsupported source token at offset %d" % i)
            value = match[0]
            kind = "NUM" if value[0].isdigit() else value.upper()
            result.append((kind, value))
            i += len(value)
    return result + [("EOF", "")]


class _Parser:
    """Small grammar, deliberately independent of the permissive converter."""

    def __init__(self, text):
        self.tokens, self.pos = _tokens(text), 0

    def take(self, kind):
        if self.tokens[self.pos][0] == kind:
            token = self.tokens[self.pos]
            self.pos += 1
            return token
        return None

    def need(self, kind):
        token = self.take(kind)
        if token is None:
            raise _Unsupported("Expected %s, found %s" %
                               (kind, self.tokens[self.pos][0]))
        return token

    def parse(self):
        node = self.expr()
        self.need("EOF")
        return node

    def expr(self, level=0):
        if level == 2 and self.take("NOT"):
            return ("call", "NOT", (self.expr(2),))
        if level < 3:
            node = self.expr(level + 1)
            operators = (("OR",), ("AND",),
                         ("=", "==", "<>", "!=", "<", "<=", ">", ">="))[level]
            while self.tokens[self.pos][0] in operators:
                op = self.tokens[self.pos][0]
                self.pos += 1
                node = ("op", op, node, self.expr(level + 1))
            return node
        if self.take("("):
            node = self.expr()
            self.need(")")
            return node
        if self.take("IF"):
            return self.if_tail()
        if self.take("{"):
            self.need("FIXED")
            dims = [self.reference()]
            while self.take(","):
                dims.append(self.reference())
            self.need(":")
            body = self.expr()
            self.need("}")
            return ("fixed", tuple(dims), body)
        if self.tokens[self.pos][0] == "REF":
            return self.reference()
        for kind in ("STR", "NUM"):
            token = self.take(kind)
            if token:
                return ("literal", kind, token[1])
        if self.take("NULL"):
            if self.take("("):
                self.need(")")
            return ("null",)
        if self.tokens[self.pos][0] in ("TRUE", "FALSE"):
            token = self.tokens[self.pos][0]
            self.pos += 1
            return ("bool", token)
        name = self.tokens[self.pos][0]
        if re.fullmatch(r"[^\W\d]\w*", name, re.UNICODE):
            self.pos += 1
            self.need("(")
            args = []
            if not self.take(")"):
                args.append(self.expr())
                while self.take(","):
                    args.append(self.expr())
                self.need(")")
            return ("call", name, tuple(args))
        raise _Unsupported("Unsupported source expression: %s" % name)

    def reference(self):
        parts = [self.need("REF")[1]]
        if self.take("."):
            parts.append(self.need("REF")[1])
        return ("ref", tuple(parts))

    def if_tail(self):
        condition = self.expr()
        self.need("THEN")
        yes = self.expr()
        if self.take("ELSEIF"):
            no = self.if_tail()
        else:
            no = self.expr() if self.take("ELSE") else ("null",)
            self.need("END")
        return ("if", condition, yes, no)


def _compare(operator, left, right, exact_text=False):
    """SQL/Tableau null comparison, not DAX's blank-to-zero coercion."""
    operator = {"=": "==", "!=": "<>"}.get(operator, operator)
    comparison = f"({left}) {operator} ({right})"
    if exact_text:
        comparison = f"EXACT({left}, {right})"
        if operator == "<>":
            comparison = f"NOT({comparison})"
    return f"IF(ISBLANK({left}) || ISBLANK({right}), BLANK(), {comparison})"


def _boolean(operator, left, right):
    # Retain three-valued logic, including when a boolean is itself returned.
    if operator == "AND":
        return (f"IF(({left}) == FALSE() || ({right}) == FALSE(), FALSE(), "
                f"IF(ISBLANK({left}) || ISBLANK({right}), BLANK(), TRUE()))")
    return (f"IF(({left}) == TRUE() || ({right}) == TRUE(), TRUE(), "
            f"IF(ISBLANK({left}) || ISBLANK({right}), BLANK(), FALSE()))")


class _Compiler:
    def __init__(self, *, calculations, root_calculation, relation_metadata,
                 field_bindings, field_types, reference_bindings, row_scopes,
                 unmatched_left, contains_case_sensitive,
                 comparison_case_sensitive, relationship_index):
        if unmatched_left not in ("exclude", "null_extend"):
            raise _Unsupported("Specify unmatched_left='exclude' or 'null_extend'")
        if not isinstance(contains_case_sensitive, bool):
            raise _Unsupported("Specify the source CONTAINS case-sensitivity")
        if not isinstance(comparison_case_sensitive, bool):
            raise _Unsupported("Specify the source field-comparison case-sensitivity")
        self.unmatched = unmatched_left
        self.case_sensitive = contains_case_sensitive
        self.exact_comparison = comparison_case_sensitive
        if relation_metadata.get("version") != 1:
            raise _Unsupported("Version 1 relation provenance is required")
        relationships = relation_metadata.get("logical_relationships", [])
        if relationship_index is None:
            if len(relationships) != 1:
                raise _Unsupported("Select one explicit logical relationship")
            relationship_index = 0
        if (not isinstance(relationship_index, int) or relationship_index < 0
                or relationship_index >= len(relationships)):
            raise _Unsupported("Invalid logical relationship index")
        rel = relationships[relationship_index]
        self.aliases = []
        for side in ("left", "right"):
            endpoint = rel.get(side, {})
            aliases = endpoint.get("tables", [])
            objects = [obj for obj in relation_metadata.get("logical_tables", [])
                       if obj.get("object_id") == endpoint.get("object_id")]
            if (not isinstance(endpoint.get("object_id"), str) or not endpoint["object_id"]
                    or len(aliases) != 1 or not isinstance(aliases[0], str)
                    or not aliases[0] or len(objects) != 1):
                raise _Unsupported("Each endpoint must resolve to one proven logical alias")
            roots = objects[0].get("relations", [])
            if (len(roots) != 1 or roots[0].get("type") not in ("table", "text")
                    or roots[0].get("name") != aliases[0]
                    or not roots[0].get("connection")):
                raise _Unsupported("Joined/opaque logical objects are outside this compiler")
            self.aliases.append(aliases[0])
        if self.aliases[0] == self.aliases[1] or rel.get("join_type") is not None:
            raise _Unsupported("Expected two distinct logical aliases, not a physical join")
        self.left, self.right = self.aliases
        self.bindings = {}
        self.tables = {}
        for key, target in field_bindings.items():
            if (not isinstance(key, tuple) or len(key) != 2
                    or key[0] not in self.aliases
                    or not isinstance(target, (tuple, list)) or len(target) != 2
                    or not all(isinstance(v, str) and v for v in (*key, *target))):
                raise _Unsupported("Bindings must be (alias, local column): (model table, column)")
            self.bindings[key] = tuple(target)
            if key[0] in self.tables and self.tables[key[0]] != target[0]:
                raise _Unsupported("One logical alias cannot map to multiple model tables")
            self.tables[key[0]] = target[0]
        if set(self.tables) != set(self.aliases):
            raise _Unsupported("Both logical aliases need explicit model bindings")
        types = {
            "string": "text", "text": "text",
            "integer": "number", "int64": "number", "real": "number",
            "double": "number", "decimal": "number", "number": "number",
            "date": "date", "datetime": "date", "boolean": "boolean",
        }
        self.types = {}
        for key in self.bindings:
            source_type = field_types.get(key)
            if not isinstance(source_type, str) or source_type.lower() not in types:
                raise _Unsupported("Explicit supported field type required for %s.%s" % key)
            self.types[key] = types[source_type.lower()]
        self.refs = {}
        for key, value in reference_bindings.items():
            if (not isinstance(key, str) or not isinstance(value, (tuple, list))
                    or len(value) != 2 or not all(isinstance(v, str) for v in value)):
                raise _Unsupported("Invalid reference binding")
            self.refs[key] = tuple(value)
        if any(value not in self.bindings for value in self.refs.values()):
            raise _Unsupported("Reference binding points to an unknown alias field")
        if set(row_scopes) != {"left", "right", "fixed_left", "fixed_right"} or any(
                not isinstance(value, str) or not value.strip() for value in row_scopes.values()):
            raise _Unsupported("Four explicit pair/FIXED row scopes are required")
        self.scopes = row_scopes
        self.calcs, self.calc_aliases = {}, {}
        for calc in calculations:
            raw = _identity(calc.get("name", ""))
            if not raw or raw in self.calcs:
                raise _Unsupported("Calculation identities must be nonempty and unique")
            self.calcs[raw] = calc.get("formula", "")
            for key in {raw, calc.get("caption") or raw}:
                self.calc_aliases.setdefault(key, set()).add(raw)
        self.root = self.calc_id(_identity(root_calculation))
        self.parsed, self.used, self.active, self.lods = {}, set(), [], []
        self.fields = set()
        self.predicate = self.predicate_node(rel.get("predicate", {}))

    def calc_id(self, name):
        ids = self.calc_aliases.get(name, set())
        if len(ids) != 1:
            raise _Unsupported("Unresolved or ambiguous calculation: %s" % name)
        return next(iter(ids))

    def field(self, parts) -> tuple:
        if not parts or not all(isinstance(part, str) and part for part in parts):
            raise _Unsupported("Unresolved relationship field operand")
        key = tuple(parts) if len(parts) == 2 else self.refs.get(parts[0])
        if key not in self.bindings:
            raise _Unsupported("Unresolved alias field: %s" % ".".join(parts))
        self.fields.add(key)
        return ("field", key)

    def calculation(self, name):
        if name in self.active:
            raise _Unsupported("Cyclic calculation dependency")
        if name in self.parsed:
            return self.parsed[name]
        self.active.append(name)
        self.used.add(name)
        node = self.expand(_Parser(self.calcs[name]).parse())
        self.active.pop()
        self.parsed[name] = node
        return node

    def expand(self, node) -> tuple:
        kind = node[0]
        if kind == "ref":
            parts = node[1]
            if len(parts) == 1 and parts[0] in self.calc_aliases:
                if parts[0] in self.refs:
                    raise _Unsupported("Field/calculation identity is ambiguous")
                return self.calculation(self.calc_id(parts[0]))
            return self.field(parts)
        if kind == "fixed":
            dims = tuple(self.field(dim[1])[1] for dim in node[1])
            if any(key[0] != self.left for key in dims):
                raise _Unsupported("FIXED dimensions must belong to the left alias")
            body = self.expand(node[2])
            if body[0:2] != ("call", "MAX") or len(body[2]) != 1:
                raise _Unsupported("Only source-declared FIXED MAX is supported")
            self.check_scalar(body[2][0], allow_lod=False)
            lod = (dims, body[2][0])
            if lod not in self.lods:
                self.lods.append(lod)
            return ("lod", self.lods.index(lod))
        if kind == "call":
            return (kind, node[1], tuple(self.expand(arg) for arg in node[2]))
        if kind == "if":
            return (kind, *(self.expand(arg) for arg in node[1:]))
        if kind == "op":
            return (kind, node[1], self.expand(node[2]), self.expand(node[3]))
        return node

    def predicate_node(self, node):
        op = node.get("operator", "").upper()
        if op == "AND" and len(node.get("arguments", [])) >= 2:
            args = [self.predicate_node(arg) for arg in node["arguments"]]
            result = args[0]
            for arg in args[1:]:
                result = ("op", "AND", result, arg)
            return result
        if op not in ("=", "==", "<>", "!=", "<", "<=", ">", ">="):
            raise _Unsupported("Only explicit conjunctive field comparisons are supported")
        operands = []
        for side in ("left", "right"):
            operand = node.get(side, {})
            operands.append(self.field((operand.get("table"), operand.get("column"))))
        if {operand[1][0] for operand in operands} != set(self.aliases):
            raise _Unsupported("Each relationship comparison must connect the two aliases")
        return ("op", op, *operands)

    def check_scalar(self, node, allow_lod=True):
        kind = node[0]
        if kind in ("literal", "null", "bool", "field"):
            return
        if kind == "lod" and allow_lod:
            return
        if kind == "op":
            args = node[2:]
        elif kind == "if":
            args = node[1:]
        elif kind == "call" and node[1] in ("IIF", "CONTAINS", "ISNULL", "NOT"):
            counts = {"IIF": (3,), "CONTAINS": (2,), "ISNULL": (1,), "NOT": (1,)}
            if len(node[2]) not in counts[node[1]]:
                raise _Unsupported("Unsupported function arity: %s" % node[1])
            if node[1] == "CONTAINS" and node[2][1][0:2] != ("literal", "STR"):
                raise _Unsupported("CONTAINS currently requires a literal substring")
            args = node[2]
        else:
            raise _Unsupported("Unsupported scalar/aggregate expression: %s" %
                               (node[1] if kind == "call" else kind))
        for arg in args:
            self.check_scalar(arg, allow_lod)

    def type_of(self, node):
        kind = node[0]
        if kind == "field":
            return self.types[node[1]]
        if kind == "lod":
            return self.type_of(self.lods[node[1]][1])
        if kind == "null":
            return "null"
        if kind == "bool":
            return "boolean"
        if kind == "literal":
            return "text" if node[1] == "STR" else "number"
        if kind == "op":
            left, right = self.type_of(node[2]), self.type_of(node[3])
            if node[1] in ("AND", "OR"):
                if left not in ("boolean", "null") or right not in ("boolean", "null"):
                    raise _Unsupported("Boolean operators require boolean inputs")
            elif left != right and "null" not in (left, right):
                raise _Unsupported("Comparison input types differ")
            elif (self.exact_comparison and left == right == "text"
                  and node[1] not in ("=", "==", "!=", "<>")):
                raise _Unsupported("Case-sensitive text ordering is outside this compiler")
            return "boolean"
        if kind == "if" or (kind == "call" and node[1] == "IIF"):
            args = node[1:] if kind == "if" else node[2]
            if self.type_of(args[0]) not in ("boolean", "null"):
                raise _Unsupported("IF requires a boolean condition")
            yes, no = self.type_of(args[1]), self.type_of(args[2])
            if yes == no:
                return yes
            if "null" in (yes, no):
                return no if yes == "null" else yes
            raise _Unsupported("Mixed-type IF branches are outside this compiler")
        if kind == "call" and node[1] == "ISNULL":
            self.type_of(node[2][0])
            return "boolean"
        if kind == "call" and node[1] == "NOT":
            if self.type_of(node[2][0]) not in ("boolean", "null"):
                raise _Unsupported("NOT requires a boolean input")
            return "boolean"
        if kind == "call" and node[1] == "CONTAINS":
            if self.type_of(node[2][0]) not in ("text", "null"):
                raise _Unsupported("CONTAINS requires a text input")
            return "boolean"
        raise _Unsupported("Cannot establish scalar result type")

    def scalar(self, node, left_vars, null_right=False):
        node = self.simplify(node, null_right)
        kind = node[0]
        if kind == "field":
            key = node[1]
            if key[0] == self.left:
                return left_vars[key]
            return "BLANK()" if null_right else _column(*self.bindings[key])
        if kind == "lod":
            return "__lrLod%d" % node[1]
        if kind == "null":
            return "BLANK()"
        if kind == "bool":
            return node[1] + "()"
        if kind == "literal":
            return ('"' + node[2].replace('"', '""') + '"'
                    if node[1] == "STR" else node[2])
        if kind == "op":
            a = self.scalar(node[2], left_vars, null_right)
            b = self.scalar(node[3], left_vars, null_right)
            return (_boolean(node[1], a, b) if node[1] in ("AND", "OR")
                    else _compare(node[1], a, b, self.exact_comparison
                                  and self.type_of(node[2]) == "text"))
        if kind == "if" or (kind == "call" and node[1] == "IIF"):
            args = node[1:] if kind == "if" else node[2]
            return "IF(%s, %s, %s)" % (
                self.truth(args[0], left_vars, null_right),
                self.scalar(args[1], left_vars, null_right),
                self.scalar(args[2], left_vars, null_right))
        if kind == "call" and node[1] == "ISNULL":
            return "ISBLANK(%s)" % self.scalar(node[2][0], left_vars, null_right)
        if kind == "call" and node[1] == "NOT":
            value = self.scalar(node[2][0], left_vars, null_right)
            return f"IF(ISBLANK({value}), BLANK(), NOT({value}))"
        if kind == "call" and node[1] == "CONTAINS":
            text = self.scalar(node[2][0], left_vars, null_right)
            needle = node[2][1][2]
            # SEARCH accepts wildcards, Tableau CONTAINS does not.
            if not self.case_sensitive:
                needle = needle.replace("~", "~~").replace("*", "~*").replace("?", "~?")
            needle = '"' + needle.replace('"', '""') + '"'
            function = "FIND" if self.case_sensitive else "SEARCH"
            return (f"IF(ISBLANK({text}), BLANK(), "
                    f"{function}({needle}, {text}, 1, 0) > 0)")
        raise _Unsupported("Unsupported scalar emission")

    def simplify(self, node, null_right=False):
        """Fold only identities valid with Tableau's three-valued null logic.

        In particular, a null-extended row cannot compare equal to a FIXED
        result, even if that result is null too. Removing that dependency is
        important: an unmatched pair must not trigger the FIXED scans.
        """
        kind = node[0]
        if kind == "field" and null_right and node[1][0] == self.right:
            return ("null",)
        if kind == "op":
            a, b = (self.simplify(n, null_right) for n in node[2:])
            if node[1] in ("AND", "OR"):
                decisive = ("bool", "FALSE" if node[1] == "AND" else "TRUE")
                identity = ("bool", "TRUE" if node[1] == "AND" else "FALSE")
                if a == decisive or b == decisive:
                    return decisive
                if a == identity:
                    return b
                if b == identity:
                    return a
                if a == b == ("null",):
                    return a
            elif a == ("null",) or b == ("null",):
                return ("null",)
            return (kind, node[1], a, b)
        if kind == "if" or (kind == "call" and node[1] == "IIF"):
            args = node[1:] if kind == "if" else node[2]
            condition, yes, no = (self.simplify(n, null_right) for n in args)
            condition = self.condition(condition)
            if condition == ("bool", "TRUE"):
                return yes
            if condition in (("bool", "FALSE"), ("null",)):
                return no
            if yes == no:
                return yes
            return ("if", condition, yes, no)
        if kind == "call":
            args = tuple(self.simplify(n, null_right) for n in node[2])
            if node[1] == "CONTAINS" and args[0] == ("null",):
                return ("null",)
            if node[1] == "ISNULL" and args[0][0] in ("null", "literal", "bool"):
                return ("bool", "TRUE" if args[0][0] == "null" else "FALSE")
            if node[1] == "NOT":
                if args[0] == ("null",):
                    return ("null",)
                if args[0][0] == "bool":
                    return ("bool", "FALSE" if args[0][1] == "TRUE" else "TRUE")
            return (kind, node[1], args)
        return node

    def condition(self, node) -> tuple:
        """Fold in a truth-only context without changing nullable scalars."""
        node = self.simplify(node)
        if node[0] == "null":
            return ("bool", "FALSE")
        if node[0] == "op" and node[1] in ("AND", "OR"):
            return self.simplify(("op", node[1], self.condition(node[2]),
                                  self.condition(node[3])))
        return node

    def truth(self, node, left_vars, null_right=False):
        """Emit a condition (only TRUE passes), not a nullable scalar.

        AND/OR in IF and FILTER do not need the exponentially repeated
        three-valued scalar construction. Their truth sets are intersection/
        union even when either input is unknown.
        """
        node = self.condition(self.simplify(node, null_right))
        if node[0] == "op" and node[1] in ("AND", "OR"):
            return "(%s) %s (%s)" % (
                self.truth(node[2], left_vars, null_right),
                "&&" if node[1] == "AND" else "||",
                self.truth(node[3], left_vars, null_right))
        return self.scalar(node, left_vars, null_right)

    @staticmethod
    def dependencies(node, kind):
        """Collect field/LOD dependencies from the expanded scalar AST."""
        if node[0] == kind:
            return {node[1]}
        if node[0] == "op":
            children = node[2:]
        elif node[0] == "if":
            children = node[1:]
        elif node[0] == "call":
            children = node[2]
        else:
            children = ()
        return set().union(*(_Compiler.dependencies(n, kind) for n in children))

    def necessary_left_truth(self, node, passes=True):
        """Overapproximate TRUE (or not-TRUE) using only current left fields.

        Right/LOD atoms are unknown, not false. In particular NOT(unknown)
        must remain possible, rather than negating an abstract TRUE to FALSE.
        AND/OR truth sets compose by intersection/union; their not-TRUE sets
        compose by the dual operations, including nullable conditions.
        """
        fields = self.dependencies(node, "field")
        if (not self.dependencies(node, "lod")
                and all(key[0] == self.left for key in fields)):
            return node if passes else (
                "if", node, ("bool", "FALSE"), ("bool", "TRUE"))
        if node[0] == "op" and node[1] in ("AND", "OR"):
            operator = node[1]
            if not passes:
                operator = "OR" if operator == "AND" else "AND"
            return self.simplify((
                "op", operator,
                self.necessary_left_truth(node[2], passes),
                self.necessary_left_truth(node[3], passes)))
        return ("bool", "TRUE")

    def necessary_left_nonzero(self, node):
        """Prove a necessary left condition for a literal-numeric IF tree.

        Fail closed on *any* other output leaf, especially NULL/implicit ELSE.
        When this condition fails, every reachable output is literal zero for
        all right rows, null extension and FIXED values. This is not a claim
        about empty strings being null, nor about the number of matching rows.
        """
        if node[0:2] == ("literal", "NUM"):
            return ("bool", "TRUE" if node[2].strip("0.") else "FALSE")
        if node[0] == "if" or node[0:2] == ("call", "IIF"):
            condition, yes, no = node[1:] if node[0] == "if" else node[2]
            yes_needed = self.necessary_left_nonzero(yes)
            no_needed = self.necessary_left_nonzero(no)
            if yes_needed is None or no_needed is None:
                return None
            return self.simplify((
                "op", "OR",
                ("op", "AND", self.necessary_left_truth(condition), yes_needed),
                ("op", "AND", self.necessary_left_truth(condition, False), no_needed)))
        return None

    def right_rows(self, scope_name, left_vars):
        """Push conjunctive keys into a proven base-table scope, never a VAR.

        Arbitrary scoped expressions may freeze filters or change row
        multiplicity when reevaluated. Only the declared table / ALL(table)
        forms are safe here; all other expressions keep the captured path.
        """
        fallback = {"right": "__lrRightRows", "fixed_right": "__lrFixedRight"}[scope_name]
        source = self.scopes[scope_name].strip()
        table = self.tables[self.right]
        quoted = _table(table)

        def is_table(expression):
            return expression == quoted or (
                expression == table and re.fullmatch(r"[^\W\d]\w*", table) is not None)

        all_scope = re.fullmatch(r"(?i:ALL)\s*\(\s*(.*?)\s*\)", source)
        if not is_table(source) and not (all_scope and is_table(all_scope[1])):
            return fallback

        def terms(node):
            if node[0:2] == ("op", "AND"):
                return terms(node[2]) + terms(node[3])
            return [node]

        filters = []
        for node in terms(self.predicate):
            if node[1] not in ("=", "=="):
                continue
            left, right = node[2][1], node[3][1]
            if left[0] == self.right:
                left, right = right, left
            key_filter = "KEEPFILTERS(TREATAS({%s}, %s))" % (
                left_vars[left], _column(*self.bindings[right]))
            if key_filter not in filters:
                filters.append(key_filter)
        if not filters:
            return fallback
        if all_scope:
            # ALL(table) as the first argument would erase the pushed filter.
            # Normalize its scope reset to a modifier, applied before the
            # explicit key filters. This preserves ALL's original row domain.
            source = quoted
            filters.insert(0, "REMOVEFILTERS(%s)" % quoted)
        return "CALCULATETABLE(%s, %s)" % (source, ", ".join(filters))

    def pair_rows(self, scope, left_vars):
        """Filter cheaply before the case-sensitive residual, preserving bags.

        The explicit non-null stage also makes the plain numeric/date
        comparisons safe outside DAX (the synthetic evaluator, for example).
        The optional storage-engine key prefilter is only a superset: retain
        every null/range/equality/EXACT check here and never deduplicate rows.
        """
        def terms(node):
            if node[0:2] == ("op", "AND"):
                return terms(node[2]) + terms(node[3])
            return [node]

        guards, comparisons, residuals = [], [], []
        for node in terms(self.predicate):
            a, b = (self.scalar(n, left_vars) for n in node[2:])
            for value in (a, b):
                guard = "NOT(ISBLANK(%s))" % value
                if guard not in guards:
                    guards.append(guard)
            op = {"=": "==", "!=": "<>"}.get(node[1], node[1])
            exact = self.exact_comparison and self.type_of(node[2]) == "text"
            # Exact equality implies collation equality, but exact inequality
            # does NOT imply collation inequality (e.g. 'a' versus 'A').
            if not exact or op == "==":
                comparisons.append("(%s) %s (%s)" % (a, op, b))
            if exact:
                check = "EXACT(%s, %s)" % (a, b)
                residuals.append("NOT(%s)" % check if op == "<>" else check)
        result = "FILTER(%s, %s)" % (scope, " && ".join(guards))
        if comparisons:
            result = "FILTER(%s, %s)" % (result, " && ".join(comparisons))
        if residuals:
            result = "FILTER(%s, %s)" % (result, " && ".join(residuals))
        return result

    def compile(self):
        root = self.calculation(self.root)
        if root[0:2] != ("call", "SUM") or len(root[2]) != 1:
            raise _Unsupported("Root must be a single SUM of a row expression")
        body = root[2][0]
        self.check_scalar(body)
        if not self.lods:
            raise _Unsupported("No supported FIXED dependency in the requested SUM")
        self.type_of(self.predicate)
        for _, lod_body in self.lods:
            if self.type_of(lod_body) not in ("number", "date"):
                raise _Unsupported("This compiler supports only numeric/date FIXED MAX")
        if self.type_of(body) != "number":
            raise _Unsupported("SUM requires a numeric row expression")
        # Capture left values BEFORE the candidate scan. Key pushdown uses
        # these scalars, not EARLIER, a measure or a key-to-scalar lookup.
        fields = sorted(key for key in self.fields if key[0] == self.left)
        current = {key: "__lrLeft%d" % i for i, key in enumerate(fields)}
        group = {key: "__lrGroup%d" % i for i, key in enumerate(fields)}
        # Reuse exactly the outer zero proof for per-row pruning. Under null
        # extension every omitted left row has at least one output, and all
        # its outputs are literal numeric zero, even without any candidates.
        # Exclusion / nullable or other leaves cannot use this identity.
        necessary = (self.necessary_left_nonzero(body)
                     if self.unmatched == "null_extend" else None)
        prune_left = necessary is not None and necessary != ("bool", "TRUE")
        guard = ""
        if prune_left:
            left_columns = {key: _column(*self.bindings[key]) for key in fields}
            guard = self.truth(necessary, left_columns)
        right_rows = self.right_rows("right", current)
        fixed_right = self.right_rows("fixed_right", current)
        # Keep the pair/FIXED work inside the nonempty-domain IF branch.
        # A per-left-row test alone still exposes the nested scans to planning.
        lines = []
        if right_rows == "__lrRightRows":
            lines.append("VAR __lrRightRows = " + self.scopes["right"])
        fixed_scope_lines = ["VAR __lrFixedLeft = " + self.scopes["fixed_left"]]
        if fixed_right == "__lrFixedRight":
            fixed_scope_lines.append("VAR __lrFixedRight = " + self.scopes["fixed_right"])
        lines += fixed_scope_lines
        if prune_left:
            # Do not replace the original scope or the independent FIXED
            # scans. The baseline adds only literal zero per ORIGINAL row:
            # empty + empty stays BLANK; nonempty + empty stays numeric 0.
            # Retained rows still run their full pair bag, including ties.
            lines.append("RETURN SUMX(__lrLeftRows, 0) + SUMX("
                         "FILTER(__lrLeftRows, " + guard + "),")
        else:
            lines.append("RETURN SUMX(__lrLeftRows,")
        lines += ["    VAR %s = %s" % (current[key], _column(*self.bindings[key]))
                  for key in fields]
        lod_lines = []
        for i, (dims, lod_body) in enumerate(self.lods):
            group_tests = []
            for key in dims:
                column, value = _column(*self.bindings[key]), current[key]
                equality = (f"EXACT({column}, {value})" if self.exact_comparison
                            and self.types[key] == "text" else f"({column} == {value})")
                # GROUP BY equates two nulls; comparisons in predicates do not.
                group_tests.append(
                    f"IF(ISBLANK({column}) || ISBLANK({value}), "
                    f"ISBLANK({column}) && ISBLANK({value}), {equality})")
            grouping = " && ".join(group_tests)
            # MAX is idempotent over duplicate group peers. Collapse the left
            # iteration ONLY if every left input to the predicate and the
            # value is fixed by the grouping dimensions. A narrower FIXED
            # left scope still requires a group-existence check.
            inputs = self.dependencies(self.predicate, "field")
            inputs |= self.dependencies(lod_body, "field")
            left_inputs = {key for key in inputs if key[0] == self.left}
            if left_inputs <= set(dims):
                value = ("MAXX(" + self.pair_rows(fixed_right, current)
                         + ", " + self.scalar(lod_body, current) + ")")
                guaranteed_peer = self.scopes["fixed_left"].strip() in (
                    self.scopes["left"].strip(),
                    "ALL(%s)" % _table(self.tables[self.left]))
                if not guaranteed_peer:
                    value = ("IF(ISEMPTY(FILTER(__lrFixedLeft, " + grouping
                             + ")), BLANK(), " + value + ")")
                lod_lines += ["    VAR __lrLod%d = %s" % (i, value)]
                continue
            lod_lines += [
                "    VAR __lrLod%d =" % i,
                "        MAXX(FILTER(__lrFixedLeft, " + grouping + "),",
            ]
            lod_lines += ["            VAR %s = %s" % (group[key], _column(*self.bindings[key]))
                          for key in fields]
            group_right = self.right_rows("fixed_right", group)
            group_pairs = (
                "FILTER(__lrFixedRight, " + self.truth(self.predicate, group) + ")"
                if group_right == "__lrFixedRight"
                else self.pair_rows(group_right, group))
            lod_lines += [
                "            RETURN MAXX(",
                "                " + group_pairs + ",",
                "                " + self.scalar(lod_body, group),
                "            )",
                "        )",
            ]
        lines += [
            "    VAR __lrPairs = " + self.pair_rows(right_rows, current),
        ]
        pair_sum = "SUMX(__lrPairs, " + self.scalar(body, current) + ")"
        # Variables live INSIDE the selected IF branch. Do not rely on && or
        # a variable declared before IF to defer a potentially quadratic LOD.
        matched = "\n".join(lod_lines + ["    RETURN " + pair_sum])
        null_body, unmatched = (), ""
        if self.unmatched == "null_extend":
            null_body = self.simplify(body, True)
            unmatched = self.scalar(null_body, current, True)
            if self.dependencies(null_body, "lod"):
                unmatched = "\n".join(lod_lines + ["    RETURN " + unmatched])
            pair_sum = ("IF(ISEMPTY(__lrPairs), " + unmatched
                        + ",\n" + matched + ")")
        else:
            pair_sum = "IF(ISEMPTY(__lrPairs), BLANK(),\n" + matched + ")"
        lines += ["    RETURN " + pair_sum, ")"]
        # Every supported relationship term is a conjunctive cross-alias
        # comparison, and pair_rows rejects NULL on either operand. Therefore
        # an empty non-null key domain on EITHER side proves there are no
        # pairs. This is only a sufficient emptiness check: nonempty domains
        # still require the full predicate, including range and EXACT checks.
        # Use the pair scopes, never the independent FIXED scopes.
        predicate_fields = self.dependencies(self.predicate, "field")
        empty_domains = []
        for alias, scope in ((self.left, "__lrLeftRows"),
                             (self.right, self.scopes["right"])):
            guards = [
                "NOT(ISBLANK(%s))" % _column(*self.bindings[key])
                for key in sorted(predicate_fields) if key[0] == alias
            ]
            empty_domains.append("ISEMPTY(FILTER(%s, %s))" %
                                 (scope, " && ".join(guards)))
        global_unmatched = "BLANK()"
        if self.unmatched == "null_extend":
            needs_lod = bool(self.dependencies(null_body, "lod"))
            null_fields = (set(fields) if needs_lod
                           else self.dependencies(null_body, "field"))
            captures = [
                "    VAR %s = %s" % (current[key], _column(*self.bindings[key]))
                for key in fields if key in null_fields
            ]
            # Reuse the compiled null-right branch, not a constant fallback.
            # SUMX retains duplicate left rows and BLANK for an empty left bag.
            null_lines = (["SUMX(__lrLeftRows,"] + captures
                          + [("    RETURN " if captures else "    ") + unmatched, ")"])
            global_unmatched = "\n".join(null_lines)
            if needs_lod:
                global_unmatched = "\n".join(
                    fixed_scope_lines + ["RETURN " + global_unmatched])
        lines = [
            "VAR __lrLeftRows = " + self.scopes["left"],
            "RETURN IF(" + " || ".join(empty_domains) + ",",
            global_unmatched + ",",
            *lines,
            ")",
        ]
        # This outer branch must precede ALL pair/FIXED declarations: even
        # unused nested LOD work can otherwise expose a Cartesian eager plan.
        # Exclusion is ineligible: removing all pairs may yield BLANK, not 0.
        if prune_left:
            lines = [
                lines[0],
                "RETURN IF(ISEMPTY(FILTER(__lrLeftRows, " + guard + ")),",
                "SUMX(__lrLeftRows, 0),",
                lines[1][len("RETURN "):],
                *lines[2:],
                ")",
            ]
        return {
            "status": "supported",
            "expression": "\n".join(lines),
            "home_table": self.tables[self.left],
            "root_calculation": self.root,
            "consumed_calculations": sorted(self.used),
            "grain": "logical_relationship_pairs",
        }


def compile_logical_relationship_sum(
        *, root_calculation, calculations, relation_metadata, field_bindings,
        field_types, reference_bindings, row_scopes, unmatched_left,
        contains_case_sensitive, comparison_case_sensitive,
        relationship_index=None):
    """Compile SUM(row expression) with left-grouped FIXED MAX dependencies.

    Integration contract (all names refer to ONE datasource):

    * ``calculations``: extracted ``{name, caption, formula}`` records.
    * ``relation_metadata``: extractor version-1 provenance, not inferred links.
    * ``field_bindings``: ``{(source_alias, local_column):
      (emitted_model_table, emitted_model_column)}``. Use actual emitted names,
      after partition renames. No caption/remote-column/RowID guessing occurs.
    * ``field_types``: ``{(source_alias, local_column): datatype}``, taken from
      source metadata (string, integer/real, date/datetime, boolean; corresponding
      model String/Int64/Double spellings are accepted). No type is guessed.
    * ``reference_bindings``: ``{bare_source_field: (source_alias, local_column)}``.
      Qualified ``[alias].[local_column]`` refs resolve directly.
    * ``row_scopes``: four lineage-preserving, bag-valued DAX table expressions:
      ``left``, ``right`` for the row-pair sum; ``fixed_left``, ``fixed_right``
      for FIXED evaluation. Semantic must supply the source filter-order policy
      (FIXED ignores ordinary dimension filters, not context/data-source filters).
      Opaque scopes are captured outside all iterators. A right-side table or
      ALL(table) scope may be reevaluated with a coarse storage-engine key
      filter before the full predicate; ALL's reset is retained as REMOVEFILTERS.
      Never supply DISTINCT/SUMMARIZE or a flattened physical join. Table refs
      in each scope must retain the column lineage specified by that alias's
      bindings.
    * ``unmatched_left``: explicitly ``exclude`` (matched-pair domain) or
      ``null_extend`` (one null candidate for an unmatched left row). A logical
      relationship's null ``join_type`` alone does NOT establish this policy.
    * ``contains_case_sensitive``: explicit source string-comparison policy.
    * ``comparison_case_sensitive``: use EXACT for text equality (including
      FIXED grouping) when true; false asserts that model collation matches
      the source. Case-sensitive text ordering is unsupported. These policies
      cannot recover case distinctions already lost by model string encoding;
      Semantic must reject such a model rather than silently conflate keys.

    Returns ``{status: 'supported', expression, home_table, root_calculation,
    consumed_calculations, grain}`` or ``{status: 'unsupported', reason}``.
    Unsupported results deliberately have NO DAX fallback.

    Install the supported expression as the root MEASURE before generic
    classification/conversion. Intermediate dependencies are scoped row/pair
    expressions, NOT calculated columns or scalar measures: skip generic
    emission for this closure. If another public field or visual directly uses
    an intermediate, handle that field separately or report it unsupported;
    do not substitute this SUM or a scalar lookup for it. Recompile dependent
    aggregate measures against the root measure as usual. Do not install an
    inferred model relationship between these aliases.

    Only conjunctive cross-alias comparisons, direct left FIXED dimensions,
    source numeric/date MAX, IF/ELSEIF/IIF, literal CONTAINS, ISNULL and scalar
    comparisons/boolean expressions are supported. Nested aggregates/LODs,
    INCLUDE/EXCLUDE, multi-table logical endpoints, ambiguous fields and cycles
    fail closed. No model mutations or physical materialization occur here.
    """
    try:
        return _Compiler(
            root_calculation=root_calculation, calculations=calculations,
            relation_metadata=relation_metadata, field_bindings=field_bindings,
            field_types=field_types, reference_bindings=reference_bindings,
            row_scopes=row_scopes, unmatched_left=unmatched_left,
            contains_case_sensitive=contains_case_sensitive,
            comparison_case_sensitive=comparison_case_sensitive,
            relationship_index=relationship_index,
        ).compile()
    except (_Unsupported, RecursionError) as exc:
        return {"status": "unsupported", "reason": str(exc) or "Expression nesting limit exceeded"}
    except (TypeError, KeyError, AttributeError, IndexError) as exc:
        return {"status": "unsupported",
                "reason": "Malformed compiler input: %s" % exc}
