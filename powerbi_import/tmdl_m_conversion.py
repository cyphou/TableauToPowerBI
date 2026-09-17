"""DAX to Power Query M expression conversion.

Extracted from tmdl_generator so the M-conversion surface has a single
owner (@wiring) instead of being co-owned inside the semantic-model module.
tmdl_generator re-exports these names for backward compatibility.
"""

import logging
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))
from m_query_builder import inject_m_steps  # noqa: E402

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════
#  DAX → POWER QUERY M EXPRESSION CONVERTER
#  Eliminates DAX calculated columns in favour of M Table.AddColumn
# ════════════════════════════════════════════════════════════════════

# M type strings matching DAX/BIM dataType values
_DAX_TO_M_TYPE = {
    'Boolean': 'type logical', 'boolean': 'type logical',
    'String': 'type text', 'string': 'type text',
    'Double': 'type number', 'double': 'type number',
    'Decimal': 'type number', 'decimal': 'type number',
    'Int64': 'Int64.Type', 'int64': 'Int64.Type',
    'DateTime': 'type datetime', 'dateTime': 'type datetime',
    'datetime': 'type datetime',
    'Date': 'type date', 'date': 'type date',
    'Time': 'type time', 'time': 'type time',
}


def _split_dax_args(s):
    """Split a string at top-level commas, respecting parentheses and quotes."""
    parts, depth, current, in_str = [], 0, [], False
    for ch in s:
        if in_str:
            current.append(ch)
            if ch == '"':
                in_str = False
        elif ch == '"':
            current.append(ch)
            in_str = True
        elif ch == '(':
            depth += 1
            current.append(ch)
        elif ch == ')':
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
    parts.append(''.join(current).strip())
    return parts


def _extract_function_body(expr, func_name):
    """Extract the content between balanced parens for a named DAX function.

    Only matches if the function call spans the entire expression.
    Returns the inner content string, or None.
    """
    pattern = re.compile(r'^' + re.escape(func_name) + r'\s*\(', re.IGNORECASE)
    m = pattern.match(expr)
    if not m:
        return None
    start = m.end() - 1  # opening '('
    depth, in_str = 0, False
    for i in range(start, len(expr)):
        ch = expr[i]
        if in_str:
            if ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                if expr[i + 1:].strip() == '':
                    return expr[start + 1:i]
                return None  # function doesn't span full expression
    return None


# Characters that are NOT valid in M generalized identifiers inside [...].
# M generalized-identifier quoting is implemented once in
# powerbi_import.calc_column_utils._quote_m_ids; re-exported here under
# the historical name for backward compatibility (Sprint 129.3 dedup).
from powerbi_import.calc_column_utils import (
    _M_SPECIAL as _M_SPECIAL_CHARS,
    _quote_m_ids as _quote_m_identifiers,
)


def _split_top_level_binop(expr, ops):
    """Find the *rightmost* top-level occurrence of any operator in ``ops``.

    Returns ``(left, op, right)`` on first match (rightmost match position so
    splits are left-associative), or ``None`` if no top-level match exists.
    Operators are checked longest-first at each position so multi-char ops
    like ``>=`` win over ``>``.  Respects parens, brackets, and string
    literals.
    """
    if not expr or not ops:
        return None
    sorted_ops = sorted(ops, key=len, reverse=True)
    n = len(expr)
    depth = 0
    bracket_depth = 0
    in_str = False
    matches = []  # list of (start_pos, op) at depth 0
    i = 0
    while i < n:
        ch = expr[i]
        if in_str:
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch == '(':
            depth += 1
            i += 1
            continue
        if ch == ')':
            depth -= 1
            i += 1
            continue
        if ch == '[':
            bracket_depth += 1
            i += 1
            continue
        if ch == ']':
            bracket_depth -= 1
            i += 1
            continue
        if depth == 0 and bracket_depth == 0:
            matched = None
            for op in sorted_ops:
                if expr[i:i + len(op)] == op:
                    matched = op
                    break
            if matched:
                matches.append((i, matched))
                i += len(matched)
                continue
        i += 1
    if not matches:
        return None
    pos, op = matches[-1]  # rightmost → left-associative outermost split
    return (expr[:pos].strip(), op, expr[pos + len(op):].strip())


def _dax_to_m_expression(dax_expr, table_name=''):
    """Convert a DAX calculated-column expression to Power Query M.

    Handles IF, SWITCH, FLOOR, ISBLANK, IN {}, string/date/math functions,
    simple arithmetic, column references, and boolean operators.

    Returns the M expression string on success, or *None* if the expression
    contains cross-table references or DAX constructs with no M equivalent
    (RELATED, LOOKUPVALUE, CALCULATE, etc.).
    """
    if not dax_expr:
        return dax_expr
    expr = dax_expr.strip()
    if not expr:
        return expr

    # ── Reject unconvertible patterns ───────────────────────────────
    upper = expr.upper()
    if 'RELATED(' in upper or 'LOOKUPVALUE(' in upper:
        return None

    # Remove self-table qualifications: 'TableName'[Col] → [Col]
    if table_name:
        expr = re.sub(r"'" + re.escape(table_name) + r"'\[", '[', expr)
    # Any remaining cross-table refs → bail
    if re.search(r"'[^']+'\[", expr):
        return None

    # ── IF(cond, true_val [, false_val]) ────────────────────────────
    body = _extract_function_body(expr, 'IF')
    if body is not None:
        args = _split_dax_args(body)
        if len(args) >= 2:
            cond = _dax_to_m_expression(args[0], table_name)
            true_v = _dax_to_m_expression(args[1], table_name)
            false_v = _dax_to_m_expression(args[2], table_name) if len(args) >= 3 else 'null'
            if cond is not None and true_v is not None and false_v is not None:
                return f'if {cond} then {true_v} else {false_v}'
        return None

    # ── SWITCH(expr, v1, r1, …, default) ────────────────────────────
    body = _extract_function_body(expr, 'SWITCH')
    if body is not None:
        args = _split_dax_args(body)
        if len(args) >= 3:
            sw = _dax_to_m_expression(args[0], table_name)
            if sw is None:
                return None
            parts = []
            i = 1
            while i + 1 < len(args):
                v = _dax_to_m_expression(args[i], table_name)
                r = _dax_to_m_expression(args[i + 1], table_name)
                if v is None or r is None:
                    return None
                parts.append(f'if {sw} = {v} then {r}')
                i += 2
            default_v = (_dax_to_m_expression(args[-1], table_name)
                         if len(args) % 2 == 0 else '"Other"')
            if default_v is None:
                return None
            return ' else '.join(parts) + f' else {default_v}'
        return None

    # ── FLOOR(x, n) → Number.RoundDown(x / n) * n ──────────────────
    body = _extract_function_body(expr, 'FLOOR')
    if body is not None:
        args = _split_dax_args(body)
        if len(args) == 2:
            x = _dax_to_m_expression(args[0], table_name)
            if x is None:
                return None
            n = args[1].strip()
            return f'Number.RoundDown({x} / {n}) * {n}'
        return None

    # ── ISBLANK(x) → (x = null) ────────────────────────────────────
    body = _extract_function_body(expr, 'ISBLANK')
    if body is not None:
        inner = _dax_to_m_expression(body, table_name)
        return f'({inner} = null)' if inner is not None else None

    # ── NOT(x) → not x ─────────────────────────────────────────────
    body = _extract_function_body(expr, 'NOT')
    if body is not None:
        inner = _dax_to_m_expression(body, table_name)
        return f'not ({inner})' if inner is not None else None

    # ── Single-argument DAX → M function map ────────────────────────
    _SINGLE = [
        ('UPPER', 'Text.Upper'), ('LOWER', 'Text.Lower'),
        ('TRIM', 'Text.Trim'), ('LEN', 'Text.Length'),
        ('YEAR', 'Date.Year'), ('MONTH', 'Date.Month'),
        ('DAY', 'Date.Day'), ('QUARTER', 'Date.QuarterOfYear'),
        ('ABS', 'Number.Abs'), ('INT', 'Number.RoundDown'),
        ('SQRT', 'Number.Sqrt'),
    ]
    for dax_fn, m_fn in _SINGLE:
        body = _extract_function_body(expr, dax_fn)
        if body is not None:
            inner = _dax_to_m_expression(body, table_name)
            return f'{m_fn}({inner})' if inner is not None else None

    # ── Multi-argument DAX → M function map ─────────────────────────
    _MULTI = [
        ('LEFT', 'Text.Start'), ('RIGHT', 'Text.End'),
        ('ROUND', 'Number.Round'),
        ('CONTAINSSTRING', 'Text.Contains'),
    ]
    for dax_fn, m_fn in _MULTI:
        body = _extract_function_body(expr, dax_fn)
        if body is not None:
            args = _split_dax_args(body)
            converted = [_dax_to_m_expression(a, table_name) for a in args]
            if any(c is None for c in converted):
                return None
            return f'{m_fn}({", ".join(converted)})'

    # MID → Text.Middle with 1-based to 0-based start position adjustment
    body = _extract_function_body(expr, 'MID')
    if body is not None:
        args = _split_dax_args(body)
        if len(args) >= 3:
            converted = [_dax_to_m_expression(a, table_name) for a in args]
            if any(c is None for c in converted):
                return None
            return f'Text.Middle({converted[0]}, {converted[1]} - 1, {converted[2]})'

    # SUBSTITUTE → Text.Replace (ignore optional 4th arg: instance_num)
    body = _extract_function_body(expr, 'SUBSTITUTE')
    if body is not None:
        args = _split_dax_args(body)
        if len(args) >= 3:
            converted = [_dax_to_m_expression(a, table_name) for a in args[:3]]
            if any(c is None for c in converted):
                return None
            return f'Text.Replace({", ".join(converted)})'

    # ── TODAY() / NOW() → Date.From(DateTime.LocalNow()) / DateTime.LocalNow()
    if re.match(r'^TODAY\s*\(\s*\)$', expr, re.IGNORECASE):
        return 'Date.From(DateTime.LocalNow())'
    if re.match(r'^NOW\s*\(\s*\)$', expr, re.IGNORECASE):
        return 'DateTime.LocalNow()'

    # ── DATEDIFF(start, end, interval) → Duration.Days/Months/Years ──
    body = _extract_function_body(expr, 'DATEDIFF')
    if body is not None:
        args = _split_dax_args(body)
        if len(args) == 3:
            start_m = _dax_to_m_expression(args[0], table_name)
            end_m = _dax_to_m_expression(args[1], table_name)
            interval = args[2].strip().upper()
            if start_m is not None and end_m is not None:
                # Wrap column refs in Date.From() for type safety
                # (CSV sources return text; date arithmetic needs typed dates)
                start_d = f'Date.From({start_m})' if start_m.strip().startswith('[') else start_m
                end_d = f'Date.From({end_m})' if end_m.strip().startswith('[') else end_m
                if interval == 'DAY':
                    return f'Duration.Days({end_d} - {start_d})'
                elif interval == 'MONTH':
                    return f'(Date.Year({end_d})*12 + Date.Month({end_d})) - (Date.Year({start_d})*12 + Date.Month({start_d}))'
                elif interval == 'YEAR':
                    return f'Date.Year({end_d}) - Date.Year({start_d})'
                elif interval == 'QUARTER':
                    return f'(Date.Year({end_d})*4 + Date.QuarterOfYear({end_d})) - (Date.Year({start_d})*4 + Date.QuarterOfYear({start_d}))'
                elif interval in ('HOUR', 'MINUTE', 'SECOND'):
                    return f'Duration.TotalSeconds({end_d} - {start_d})'
                # Unsupported interval
                return None
        return None

    # ── DATE(y, m, d) → #date(y, m, d)  |  DATE(col) → Date.From(col) ──
    body = _extract_function_body(expr, 'DATE')
    if body is not None:
        args = _split_dax_args(body)
        if len(args) == 3:
            y = _dax_to_m_expression(args[0], table_name)
            mo = _dax_to_m_expression(args[1], table_name)
            d = _dax_to_m_expression(args[2], table_name)
            if y is not None and mo is not None and d is not None:
                return f'#date({y}, {mo}, {d})'
        elif len(args) == 1:
            inner = _dax_to_m_expression(args[0], table_name)
            if inner is not None:
                return f'Date.From({inner})'
        return None

    # ── DATEVALUE(text) → Date.From(text) ──────────────────────────
    body = _extract_function_body(expr, 'DATEVALUE')
    if body is not None:
        args = _split_dax_args(body)
        if len(args) == 1:
            inner = _dax_to_m_expression(args[0], table_name)
            if inner is not None:
                return f'Date.From({inner})'
        return None

    # ── [expr] IN {val1, val2, …} → List.Contains({…}, expr) ───────
    in_match = re.match(r'^(.+?)\s+IN\s+(\{.+\})\s*$', expr, re.IGNORECASE)
    if in_match:
        col_m = _dax_to_m_expression(in_match.group(1), table_name)
        if col_m is not None:
            # M uses double-quoted strings only — convert any DAX/Tableau
            # single-quoted literals like {'High', 'Low'} to {"High", "Low"}
            set_expr = re.sub(r"'([^']*)'", r'"\1"', in_match.group(2))
            return f'List.Contains({set_expr}, {col_m})'
        return None

    # ── Top-level boolean operators (lowest precedence first) ──────
    # Try ||/OR before &&/AND so OR forms the outermost split.
    # Recursing on each side lets embedded function calls (DATE(), IF(), …)
    # be re-matched by the function-body handlers above.
    for op_set, m_join in (
        (['||'], ' or '),
        (['&&'], ' and '),
    ):
        split = _split_top_level_binop(expr, op_set)
        if split:
            left, _op, right = split
            l_m = _dax_to_m_expression(left, table_name)
            r_m = _dax_to_m_expression(right, table_name)
            if l_m is not None and r_m is not None:
                return f'{l_m}{m_join}{r_m}'
            return None

    # ── Top-level comparison operators ─────────────────────────────
    # DAX and M share the same comparison syntax (=, <>, <, >, <=, >=).
    cmp_ops = ['<=', '>=', '<>', '=', '<', '>']
    split = _split_top_level_binop(expr, cmp_ops)
    if split:
        left, op, right = split
        l_m = _dax_to_m_expression(left, table_name)
        r_m = _dax_to_m_expression(right, table_name)
        if l_m is not None and r_m is not None:
            return f'{l_m} {op} {r_m}'
        return None

    # ── Leaf expression (literals, column refs, operators) ──────────
    result = expr
    result = result.replace('&&', ' and ').replace('||', ' or ')
    result = re.sub(r'\bTRUE\s*\(\s*\)', 'true', result, flags=re.IGNORECASE)
    result = re.sub(r'\bFALSE\s*\(\s*\)', 'false', result, flags=re.IGNORECASE)
    result = re.sub(r'\bBLANK\s*\(\s*\)', 'null', result, flags=re.IGNORECASE)

    # Remaining DAX function calls → not convertible
    if re.search(r'\b[A-Z_]{2,}\s*\(', result):
        return None
    return _quote_m_identifiers(result)


_DATE_DATATYPES = frozenset({
    'date', 'datetime', 'dateTime', 'Date', 'DateTime',
})

_RE_COL_SUBTRACTION = re.compile(
    r'^\s*(\[#?"?[^\]"]+\"?\])\s*-\s*(\[#?"?[^\]"]+\"?\])\s*$'
)


def _wrap_date_subtraction_in_duration_days(m_expr, columns, col_metadata_map):
    """Wrap bare date-column subtractions in Duration.Days() for M.

    In Power Query M, subtracting two date/datetime values produces a
    ``duration``, not an integer.  When the target column type is integer
    or number, the result must be wrapped in ``Duration.Days()`` so that
    Power BI can store it as a numeric value.
    """
    m = _RE_COL_SUBTRACTION.match(m_expr)
    if not m:
        return m_expr

    col_type_map = {}
    for c in (columns or []):
        cn = c.get('name', '')
        if cn:
            col_type_map[cn] = c.get('datatype', '')
    for cn, meta in (col_metadata_map or {}).items():
        dt = meta.get('datatype', '')
        if dt:
            col_type_map[cn] = dt

    def _extract_col_name(bracket_ref):
        s = bracket_ref.strip().lstrip('[').rstrip(']')
        s = s.lstrip('#').strip('"')
        return s

    left_name = _extract_col_name(m.group(1))
    right_name = _extract_col_name(m.group(2))

    left_dt = col_type_map.get(left_name, '')
    right_dt = col_type_map.get(right_name, '')

    if left_dt in _DATE_DATATYPES and right_dt in _DATE_DATATYPES:
        return f'Duration.Days({m_expr.strip()})'
    return m_expr


def _strip_m_inline_comments(m_expr):
    """Strip ``//`` single-line comments from an M expression.

    Tableau calculated fields may contain inline comments like ``//New v1.6``
    which break M parsing when the expression is written on a single line.
    This function removes ``//``-style comments while preserving content
    inside string literals (``"..."``).

    Also fixes the ``#"each if ..."`` / ``#"else if ..."`` corruption pattern
    where M keywords get incorrectly quoted as identifier references.
    """
    if not m_expr:
        return m_expr

    # Fix corrupted patterns: #"each if X" → each if X, #"else if X" → else if X
    m_expr = re.sub(r'#"(each if[^"]*)"', r'\1', m_expr)
    m_expr = re.sub(r'#"(else if[^"]*)"', r'\1', m_expr)
    m_expr = re.sub(r'#"(else null[^"]*)"', r'\1', m_expr)

    # Strip // comments outside string literals
    if '//' not in m_expr:
        return m_expr

    result = []
    i = 0
    n = len(m_expr)
    while i < n:
        if m_expr[i] == '"':
            # Inside a string literal — skip to closing quote
            result.append(m_expr[i])
            i += 1
            while i < n:
                if m_expr[i] == '"':
                    result.append(m_expr[i])
                    i += 1
                    if i < n and m_expr[i] == '"':
                        # Escaped quote ""
                        result.append(m_expr[i])
                        i += 1
                    else:
                        break
                else:
                    result.append(m_expr[i])
                    i += 1
        elif m_expr[i:i+2] == '//':
            # Found a // comment — determine how much to strip.
            # First check if there's a column ref [bracket] nearby after //
            # indicating this is a short Tableau annotation (e.g. //New v1.6)
            # followed by code that should be preserved.
            j = i + 2
            while j < n and m_expr[j] != '\n':
                j += 1
            # Text between // and end-of-line (or end-of-string)
            comment_text = m_expr[i+2:j]
            bracket_pos = comment_text.find('[')
            if bracket_pos >= 0 and bracket_pos < 50:
                # Short annotation followed by column ref — keep the code
                # Remove just the comment text (up to the bracket)
                i = i + 2 + bracket_pos
            elif j < n:
                # Multi-line: no bracket nearby, strip comment to newline
                i = j
            else:
                # Single line: true trailing comment — strip everything
                break
        else:
            result.append(m_expr[i])
            i += 1

    return ''.join(result)


def _inject_m_steps_into_partition(table, steps):
    """Inject M transformation steps into a table's M partition.

    Phase 3: validates the resulting M expression after injection.
    Issues are logged but do not block generation.
    """
    if not steps:
        return False
    # Sanitize step expressions: strip // comments and fix corrupted patterns
    sanitized_steps = []
    for step_name, step_expr in steps:
        sanitized_steps.append((step_name, _strip_m_inline_comments(step_expr)))
    for partition in table.get('partitions', []):
        source = partition.get('source', {})
        if source.get('type') == 'm' and source.get('expression'):
            # Also strip // comments from the existing M expression before injection
            source['expression'] = _strip_m_inline_comments(source['expression'])
            source['expression'] = inject_m_steps(source['expression'], sanitized_steps)
            # Phase 3: inline M validation after step injection
            try:
                from powerbi_import.m_validator import validate_m_query
                m_issues = validate_m_query(source['expression'])
                if m_issues:
                    tname = table.get('name', '<unknown>')
                    logger.warning(
                        "M validation issue after step injection on '%s': %s",
                        tname, m_issues[0],
                    )
            except Exception:
                pass  # validator must never block generation
            return True
    return False
