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
from collections import deque

logger = logging.getLogger(__name__)

def _mask_dax_noncode(expr):
    """Mask strings/comments without changing offsets or identifier spelling."""
    masked = list(expr)
    i = 0
    while i < len(expr):
        if expr[i] == '[':
            i += 1
            while i < len(expr):
                if expr[i] == ']' and i + 1 < len(expr) and expr[i + 1] == ']':
                    i += 2
                elif expr[i] == ']':
                    i += 1
                    break
                else:
                    i += 1
            continue
        if expr[i] == "'":
            i += 1
            while i < len(expr):
                if expr[i] == "'" and i + 1 < len(expr) and expr[i + 1] == "'":
                    i += 2
                elif expr[i] == "'":
                    i += 1
                    break
                else:
                    i += 1
            continue
        start = i
        if expr[i] == '"':
            i += 1
            while i < len(expr):
                if expr[i] == '"' and i + 1 < len(expr) and expr[i + 1] == '"':
                    i += 2
                elif expr[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
        elif expr.startswith('/*', i):
            depth = 1
            i += 2
            while i < len(expr) and depth:
                if expr.startswith('/*', i):
                    depth += 1
                    i += 2
                elif expr.startswith('*/', i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
        elif expr.startswith(('//', '--'), i):
            end = expr.find('\n', i + 2)
            i = len(expr) if end < 0 else end
        else:
            i += 1
            continue
        masked[start:i] = ' ' * (i - start)
    return ''.join(masked)


def _unsupported_measure_expression(reason):
    """Use the generation path's existing, quality-reportable TODO marker."""
    logger.warning("Unsupported measure rewrite: %s", reason)
    return ('/* TODO: DAX conversion validation failed for measure: '
            + reason + ' */ BLANK()')


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

    by_identity, ambiguous_renames = {}, set()
    for identity, produced in renames.items():
        if (not isinstance(identity, tuple) or len(identity) != 2
                or not all(isinstance(part, str) for part in identity)
                or not isinstance(produced, str) or not produced):
            continue
        key = (identity[0].casefold(), identity[1].casefold())
        if key in by_identity and by_identity[key] != produced:
            ambiguous_renames.add(key)
        else:
            by_identity[key] = produced

    measures_by_name, measures_by_owner = set(), set()
    for table in model.get('model', {}).get('tables', []):
        table_name = table.get('name', '')
        if not isinstance(table_name, str):
            continue
        for measure in table.get('measures', []):
            name = measure.get('name', '')
            if isinstance(name, str) and name:
                measures_by_name.add(name.casefold())
                measures_by_owner.add((table_name.casefold(), name.casefold()))

    def rewrite(expr, home_table):
        refs, _ = _dax_dependency_tokens(expr, include_spans=True)
        result = expr
        for owner, column, column_only, start, bracket, end in reversed(refs):
            table_name = owner if owner is not None else home_table
            if not isinstance(table_name, str):
                continue
            key = (table_name.casefold(), column.casefold())
            target = by_identity.get(key)
            if not target:
                continue
            if key in ambiguous_renames:
                logger.warning(
                    "Ambiguous column rename for '%s'[%s]; reference left unchanged",
                    table_name, column)
                continue
            # A bare field can be a model-wide measure. A qualified field can
            # be a measure of that table. Only a proven column-only argument
            # may be renamed when a same-named measure also exists.
            if not column_only and (
                    (owner is None and column.casefold() in measures_by_name)
                    or (owner is not None and key in measures_by_owner)):
                logger.warning(
                    "Ambiguous column/measure reference '%s'[%s]; left unchanged",
                    table_name, column)
                continue
            result = (result[:bracket + 1] + target.replace(']', ']]')
                      + result[end - 1:])
        return result

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


_BARE_COL_REF_RE = re.compile(
    r"^(?P<owner>'(?:[^']|'')+'|[^\W\d]\w*)?\[(?:[^\]]|\]\])+\]$",
    re.UNICODE,
)


def _wrap_bare_ref_expression(dax_formula, datatype, table_name):
    """Do not invent a shelf aggregation for a measure's bare column."""
    match = _BARE_COL_REF_RE.match(dax_formula.strip())
    if not match or not match.group('owner'):
        # A bare [Name] can be a measure; the caller lacks global metadata.
        return dax_formula
    return _unsupported_measure_expression(
        'bare column requires source shelf aggregation and total grain')


_BARE_REF_RE = re.compile(r"(?<!')\[([^\]]+)\]")


def _promote_measure_dependent_calc_columns(result_table):
    """Turn a calculated column that calls a measure into a measure.

    Referencing a measure from a calculated column can introduce context
    transitions and circular dependencies. Promotion avoids that structural
    problem but does not prove equivalence to the source calculation.

    Subsequent aggregation and bare-column guards emit a diagnostic TODO
    when the source row scope, shelf aggregation or total grain is unknown;
    they do not automatically remove MIN or invent an aggregation.
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
    """Reject aggregation of a measure when its source grain is unknown.

    Removing SUM/MIN/etc. fixes DAX syntax but can change the Tableau shelf
    and total result, especially when the measure represents a FIXED value.
    Only the semantic owner can establish that the outer aggregation is
    redundant or compile it over the correct source grain.
    """
    _all_measure_names = {m["name"] for m in result_table["measures"]}
    _column_names = {c.get("name", "") for c in result_table.get("columns", [])}
    home_table = result_table.get("name", "")
    _qualified_measure_ref = re.compile(
        r"(?P<owner>'(?:[^']|'')+'|[^\W\d]\w*)"
        r"\[(?P<col>(?:[^\]]|\]\])+)\]",
        re.UNICODE,
    )
    for meas in result_table["measures"]:
        expr = meas.get("expression", "")
        if not expr:
            continue
        masked = _mask_dax_noncode(expr)
        for match in reversed(list(_qualified_measure_ref.finditer(masked))):
            owner = match.group('owner')
            owner = owner[1:-1].replace("''", "'") if owner.startswith("'") else owner
            name = match.group('col').replace(']]', ']')
            if owner == home_table and name in _all_measure_names and name not in _column_names:
                expr = expr[:match.start()] + '[' + match.group('col') + ']' + expr[match.end():]
        meas["expression"] = expr
    _AGG_OF_MEASURE_RE = re.compile(
        r'\b(SUM|AVERAGE|COUNT|MIN|MAX)\s*\(\s*\[((?:[^\]]|\]\])+)\]\s*\)',
        re.IGNORECASE,
    )
    for meas in result_table["measures"]:
        expr = meas.get("expression", "")
        if not expr:
            continue
        for match in _AGG_OF_MEASURE_RE.finditer(_mask_dax_noncode(expr)):
            name = match.group(2).replace(']]', ']')
            if name in _all_measure_names and name not in _column_names:
                meas["expression"] = _unsupported_measure_expression(
                    'aggregation of a calculation requires its source shelf '
                    'and total grain')
                break



def _wrap_bare_cross_table_refs(result_table):
    """Reject bare measure columns instead of guessing their aggregation."""
    _all_measure_names = {m["name"] for m in result_table["measures"]}
    _column_names = {c.get("name", "") for c in result_table.get("columns", [])}
    home_table = result_table.get("name", "")
    _XTABLE_COL_RE = re.compile(
        r"(?<![\w'])(?P<owner>'(?:[^']|'')+'|[^\W\d]\w*)"
        r"\[(?P<col>(?:[^\]]|\]\])+)\]",
        re.UNICODE,
    )
    _COLUMN_CONTEXT_FUNCS = frozenset({
        'SUM', 'AVERAGE', 'COUNT', 'COUNTA', 'COUNTBLANK', 'COUNTROWS',
        'MIN', 'MAX', 'DISTINCTCOUNT', 'DISTINCTCOUNTNOBLANK',
        'MEDIAN', 'PERCENTILE',
        'SUMX', 'AVERAGEX', 'COUNTX', 'MINX', 'MAXX', 'PRODUCTX',
        'CONCATENATEX', 'MEDIANX', 'PERCENTILEX',
        'FILTER', 'ADDCOLUMNS', 'SELECTCOLUMNS',
        'GENERATE', 'GENERATEALL', 'RANKX', 'TOPN',
        'SUMMARIZE', 'SUMMARIZECOLUMNS', 'GROUPBY',
        'RELATED', 'RELATEDTABLE', 'LOOKUPVALUE', 'TREATAS',
        'VALUES', 'DISTINCT', 'ALL', 'ALLEXCEPT', 'ALLNOBLANKROW',
        'ALLSELECTED', 'REMOVEFILTERS',
        'EARLIER', 'EARLIEST', 'SELECTEDVALUE',
        'HASONEVALUE', 'HASONEFILTER', 'ISINSCOPE',
        'USERELATIONSHIP', 'CROSSFILTER',
    })
    for meas in result_table["measures"]:
        expr = meas.get("expression", "")
        if not expr:
            continue
        masked = _mask_dax_noncode(expr)
        for match in _XTABLE_COL_RE.finditer(masked):
            owner = match.group('owner')
            owner = owner[1:-1].replace("''", "'") if owner.startswith("'") else owner
            col_name = match.group('col').replace(']]', ']')
            if (owner == home_table and col_name in _all_measure_names
                    and col_name not in _column_names):
                continue
            # Strings and comments are masked; quoted table names and
            # bracketed identifiers must also be skipped while counting.
            func_stack = []  # [function name, current argument index]
            text_before = masked[:match.start()]
            i = 0
            while i < len(text_before):
                ch = text_before[i]
                if ch in ("'", '['):
                    close = ']' if ch == '[' else "'"
                    i += 1
                    while i < len(text_before):
                        if text_before[i] == close:
                            if i + 1 < len(text_before) and text_before[i + 1] == close:
                                i += 2
                            else:
                                i += 1
                                break
                        else:
                            i += 1
                elif ch == '(':
                    j = i - 1
                    while j >= 0 and text_before[j].isspace():
                        j -= 1
                    func_end = j + 1
                    while j >= 0 and (text_before[j].isalnum()
                                      or text_before[j] in '_.'):
                        j -= 1
                    func_stack.append([text_before[j + 1:func_end].upper(), 0])
                    i += 1
                elif ch == ')':
                    if func_stack:
                        func_stack.pop()
                    i += 1
                elif ch == ',' and func_stack:
                    func_stack[-1][1] += 1
                    i += 1
                else:
                    i += 1
            if any(name in _COLUMN_CONTEXT_FUNCS
                   or (name in ('CALCULATE', 'CALCULATETABLE') and index > 0)
                   for name, index in func_stack):
                continue
            meas["expression"] = _unsupported_measure_expression(
                'bare column requires source row scope, shelf aggregation '
                'and total grain')
            break



def _dax_dependency_tokens(expression, *, include_spans=False):
    """Return real DAX field refs and conversion markers, excluding literals.

    Quoted owners, escaped delimiters, Unicode names and line/block comments
    are consumed as tokens rather than searched with a bracket-only regex.
    A direct argument to a column-only function is identified separately so
    a same-named measure cannot be mistaken for that physical column.
    """
    refs = []
    unsupported = False
    length = len(expression)
    code = _mask_dax_noncode(expression)
    column_function = re.compile(
        r'(?<![\w.])(?P<name>SUM|AVERAGE|COUNT|COUNTA|COUNTBLANK|'
        r'DISTINCTCOUNT|DISTINCTCOUNTNOBLANK|MEDIAN|MIN|MAX|VALUES|'
        r'HASONEVALUE|ISFILTERED|ISINSCOPE|SELECTEDVALUE|'
        r'STDEV\.[SP]|VAR\.[SP])\s*\(\s*$',
        re.IGNORECASE,
    )

    def add_ref(owner, name, start, bracket_start, end):
        match = column_function.search(code[:start])
        column_only = False
        if match:
            tail = code[end:]
            # SELECTEDVALUE permits an optional alternate scalar result;
            # the other supported signatures here take one column argument.
            close = r'\s*(?:\)|,)' if match.group('name').upper() == 'SELECTEDVALUE' else r'\s*\)'
            column_only = bool(re.match(close, tail))
        if include_spans:
            refs.append((owner, name, column_only, start, bracket_start, end))
        else:
            refs.append((owner, name, column_only))

    def quoted(start, delimiter):
        chars, pos = [], start + 1
        while pos < length:
            if expression[pos] == delimiter:
                if pos + 1 < length and expression[pos + 1] == delimiter:
                    chars.append(delimiter)
                    pos += 2
                    continue
                return ''.join(chars), pos + 1
            chars.append(expression[pos])
            pos += 1
        return None, length

    def bracket(start):
        return quoted(start, ']')

    i = 0
    while i < length:
        if expression.startswith(('//', '--'), i):
            newline = expression.find('\n', i + 2)
            i = length if newline < 0 else newline + 1
        elif expression.startswith('/*', i):
            start = i + 2
            depth, end = 1, start
            while end < length and depth:
                if expression.startswith('/*', end):
                    depth += 1
                    end += 2
                elif expression.startswith('*/', end):
                    depth -= 1
                    if depth:
                        end += 2
                else:
                    end += 1
            if re.match(r'\s*TODO:\s*DAX conversion validation failed\b',
                        expression[start:end], re.IGNORECASE):
                unsupported = True
            i = min(end + 2, length)
        elif expression[i] == '"':
            _, i = quoted(i, '"')
        elif expression[i] == "'":
            start = i
            owner, end = quoted(i, "'")
            pos = end
            while pos < length and expression[pos].isspace():
                pos += 1
            if owner is not None and pos < length and expression[pos] == '[':
                name, i = bracket(pos)
                if name is not None:
                    add_ref(owner, name, start, pos, i)
            else:
                i = end
        elif expression[i] == '[':
            start = i
            name, i = bracket(i)
            if name is not None:
                add_ref(None, name, start, start, i)
        elif expression[i] == '_' or expression[i].isalpha():
            start = i
            end = i + 1
            while end < length and (expression[end].isalnum()
                                    or expression[end] == '_'):
                end += 1
            owner = expression[i:end]
            pos = end
            while pos < length and expression[pos].isspace():
                pos += 1
            if pos < length and expression[pos] == '[':
                name, i = bracket(pos)
                if name is not None:
                    add_ref(owner, name, start, pos, i)
            else:
                i = end
        else:
            i += 1
    return refs, unsupported


def _tableau_source_identifier(value):
    """Decode one explicitly recorded Tableau calculation identifier."""
    if not isinstance(value, str) or not value:
        return None
    if not value.startswith('['):
        return value
    refs, _ = _dax_dependency_tokens(value, include_spans=True)
    if (len(refs) == 1 and refs[0][0] is None
            and refs[0][3] == 0 and refs[0][5] == len(value)):
        return refs[0][1]
    return None


def _single_source_reference(item):
    """Return one unqualified source ref, without guessing token alignment."""
    formula = item.get('_original_formula')
    if not isinstance(formula, str) or not formula:
        return None
    refs, _ = _dax_dependency_tokens(formula, include_spans=True)
    if not refs:
        return None
    names = set()
    for owner, name, column_only, start, _, end in refs:
        before = formula[:start].rstrip()
        after = formula[end:].lstrip()
        if (owner is not None or column_only
                or before.endswith('.') or after.startswith('.')):
            return None
        names.add(name.casefold())
    return refs[0][1] if len(names) == 1 else None


def _identical_source_declarations(left, right):
    """Compare complete source metadata without coercion or mutation."""
    remaining = 10000

    def equal(a, b, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 64 or type(a) is not type(b):
            return False
        if type(a) is dict:
            if (not all(type(key) is str for key in a)
                    or not all(type(key) is str for key in b)
                    or a.keys() != b.keys()):
                return False
            return all(equal(a[key], b[key], depth + 1) for key in a)
        if type(a) in (list, tuple):
            return (len(a) == len(b)
                    and all(equal(x, y, depth + 1) for x, y in zip(a, b)))
        if type(a) is float:
            return a == b and a.hex() == b.hex()
        if type(a) in (str, int, bool, type(None)):
            return a == b
        return False

    return equal(left, right, 0)


def _source_calculation_inventory(model):
    """Index original source declarations while retaining their namespaces."""
    sources = model.get('_datasources')
    if not isinstance(sources, list) or not sources:
        return None
    names = [source.get('name') if isinstance(source, dict) else None
             for source in sources]
    namespace_counts = {}
    for name in names:
        if isinstance(name, str) and name.strip():
            namespace_counts[name.casefold()] = namespace_counts.get(name.casefold(), 0) + 1
    aliases = {}
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get('calculations'), list):
            return None
        namespace = source.get('name')
        if (not isinstance(namespace, str) or not namespace.strip()
                or namespace_counts.get(namespace.casefold()) != 1):
            namespace = None
        for calc in source['calculations']:
            if not isinstance(calc, dict):
                return None
            raw = _tableau_source_identifier(calc.get('name'))
            if not raw:
                return None
            caption = calc.get('caption') or raw
            if not isinstance(caption, str):
                return None
            declared_namespace = calc.get('datasource_name')
            calc_namespace = namespace
            if ('datasource_name' in calc and declared_namespace != namespace):
                calc_namespace = None
            # Keep the original declaration, including every metadata field.
            # Do not collapse records until both target identities are proven.
            record = (raw, caption, calc.get('formula'), calc_namespace, calc)
            for alias in {raw.casefold(), caption.casefold()}:
                aliases.setdefault(alias, []).append(record)
    return aliases


def propagate_unsupported_model_dependencies(
        model, *, diagnose_ambiguous=True, defer_source_binding=False):
    """Fail closed on transitive dependencies on unsupported model fields.

    Invoke on the complete, final symbol table after semantic renames and all
    measure/column creation. Qualified refs bind only to their named table;
    bare refs are resolved against all matching model symbols. If a ref has
    multiple possible identities, it is diagnosed as ambiguous rather than
    silently attributed to an unsupported namesake. The returned diagnostics
    contain field identities, not formula text. Recorded measure/column rename
    maps are used to diagnose stale pre-rename references; this pass never
    changes reference text or rewrites a same-named physical column, except
    when explicit source identities and model symbols (or a unique recorded
    rename) prove the materialized target.

    An earlier model pass may set ``diagnose_ambiguous=False`` to defer those
    identities until the semantic owner has finished renaming symbols. This
    function does not inspect Power Query expressions or report visual calcs.
    ``defer_source_binding`` is for incomplete pre-rename/local passes; a
    final model-wide call must diagnose missing or ambiguous targets.
    """
    objects, by_name, by_owner_name, table_names = [], {}, {}, set()
    for table in model.get('model', {}).get('tables', []):
        owner = table.get('name', '')
        if not isinstance(owner, str):
            continue
        table_names.add(owner.casefold())
        for kind in ('measures', 'columns'):
            for item in table.get(kind, []):
                name = item.get('name', '')
                if not isinstance(name, str) or not name:
                    continue
                index = len(objects)
                objects.append((owner, kind, item))
                by_name.setdefault(name.casefold(), []).append(index)
                by_owner_name.setdefault((owner.casefold(), name.casefold()), []).append(index)

    renamed_by_name, renamed_by_owner_name = {}, {}
    for map_name, kind in (('_measure_rename_map', 'measures'),
                           ('_column_rename_map', 'columns')):
        for identity, new_name in (model.get(map_name) or {}).items():
            if (not isinstance(identity, tuple) or len(identity) != 2
                    or not all(isinstance(part, str) for part in identity)
                    or not isinstance(new_name, str) or not new_name):
                continue
            owner, old_name = identity
            renamed = [index for index in by_owner_name.get(
                (owner.casefold(), new_name.casefold()), [])
                       if objects[index][1] == kind]
            if renamed:
                renamed_by_name.setdefault(old_name.casefold(), []).extend(renamed)
                renamed_by_owner_name.setdefault(
                    (owner.casefold(), old_name.casefold()), []).extend(renamed)

    source_inventory = _source_calculation_inventory(model)
    targets_by_source, physical_names, physical_by_source, caption_targets = {}, set(), {}, {}
    for index, (_, kind, item) in enumerate(objects):
        identity = item.get('_tableau_calculation_identity')
        raw = (_tableau_source_identifier(identity.get('name'))
               if isinstance(identity, dict) else None)
        if raw:
            targets_by_source.setdefault(raw.casefold(), []).append(index)
            caption = identity.get('caption')
            if kind == 'measures' and isinstance(caption, str):
                caption_targets.setdefault(caption.casefold(), set()).add(
                    item['name'].casefold())
        elif kind == 'columns':
            for name in (item.get('name'), item.get('sourceColumn')):
                if isinstance(name, str) and name:
                    physical_names.add(name.casefold())
                    physical_by_source.setdefault(name.casefold(), []).append(index)

    def matches_source_record(item, record, namespace):
        identity = item.get('_tableau_calculation_identity')
        declaration = record[4]
        return (
            isinstance(identity, dict)
            and isinstance(namespace, str) and bool(namespace.strip())
            and record[3] == namespace
            and identity.get('datasource_name') == namespace
            and identity.get('name') == declaration.get('name')
            and identity.get('caption', '') == declaration.get('caption', '')
            and isinstance(item.get('_original_formula'), str)
            and item['_original_formula'] == declaration.get('formula')
        )

    def collapse_identical_records(records, namespace):
        """Collapse only full, exact declarations in one proven namespace."""
        if len(records) < 2:
            return records
        first = records[0]
        if (first[3] != namespace
                or any(record[3] != namespace
                       or record[4].get('name') != first[4].get('name')
                       or not _identical_source_declarations(first[4], record[4])
                       for record in records[1:])):
            return records
        return records[:1]

    def emitted_target_reference(target, refs):
        """Prove an emitted DAX ref identifies this target, without pairing by position."""
        target_owner, kind, target_item = objects[target]
        rewrites = []
        found = False
        for owner, name, column_only, _, bracket, end in refs:
            exact = (by_name.get(name.casefold(), []) if owner is None
                     else by_owner_name.get((owner.casefold(), name.casefold()), []))
            renamed = (renamed_by_name.get(name.casefold(), []) if owner is None
                       else renamed_by_owner_name.get((owner.casefold(), name.casefold()), []))
            candidates = list(dict.fromkeys([*exact, *renamed]))
            if column_only:
                candidates = [candidate for candidate in candidates
                              if objects[candidate][1] == 'columns']
                exact = [candidate for candidate in exact
                         if objects[candidate][1] == 'columns']
            if target not in candidates:
                continue
            if candidates != [target]:
                return 'target_ambiguous', []
            found = True
            if target not in exact:
                # Only a recorded, unique rename can repair a stale symbol.
                new_name = target_item['name']
                current = (by_name.get(new_name.casefold(), []) if owner is None
                           else by_owner_name.get(
                               (target_owner.casefold(), new_name.casefold()), []))
                if current != [target] or (kind == 'measures'
                                           and by_name.get(new_name.casefold()) != [target]):
                    return 'target_ambiguous', []
                rewrites.append((bracket, end, new_name))
        return ('resolved', rewrites) if found else ('unresolved', [])

    def multiple_source_bindings(index, refs):
        """Verify each distinct source dependency before using the DAX graph."""
        item = objects[index][2]
        identity = item.get('_tableau_calculation_identity')
        consumer_id = (_tableau_source_identifier(identity.get('name'))
                       if isinstance(identity, dict) else None)
        formula = item.get('_original_formula')
        if not isinstance(formula, str) or not formula:
            if consumer_id:
                output_names = {(owner.casefold() if owner else None, name.casefold())
                                for owner, name, _, _, _, _ in refs}
                if source_inventory is None:
                    return ('unresolved', consumer_id, []) if len(output_names) > 1 else None
                source_formulas_found = False
                for record in source_inventory.get(consumer_id.casefold(), []):
                    source_formula = record[2]
                    if (record[0].casefold() != consumer_id.casefold()
                            or not isinstance(source_formula, str)):
                        continue
                    source_formulas_found = True
                    source_refs, _ = _dax_dependency_tokens(source_formula)
                    if len({(owner.casefold() if owner else None, name.casefold())
                            for owner, name, _ in source_refs}) > 1:
                        return 'unproven', consumer_id, []
                if not source_formulas_found and len(output_names) > 1:
                    return 'unproven', consumer_id, []
            return None
        source_refs, _ = _dax_dependency_tokens(formula, include_spans=True)
        distinct = {(owner.casefold() if owner else None, name.casefold())
                    for owner, name, _, _, _, _ in source_refs}
        if len(distinct) < 2:
            return None

        namespace = identity.get('datasource_name') if isinstance(identity, dict) else None
        inventory = source_inventory or {}
        calculation_refs = False
        for _, name, _, _, _, _ in source_refs:
            records = inventory.get(name.casefold(), [])
            physical = physical_by_source.get(name.casefold(), [])
            if (targets_by_source.get(name.casefold())
                    or any(record[0].casefold() == name.casefold() for record in records)
                    or (records and not physical)):
                calculation_refs = True
                break
        consumer_records = (inventory.get(consumer_id.casefold(), [])
                            if consumer_id else [])
        if calculation_refs or consumer_records:
            if source_inventory is None or not consumer_id:
                return 'unresolved', source_refs[0][1], []
            if not consumer_records:
                return 'unresolved', consumer_id, []
            if targets_by_source.get(consumer_id.casefold(), []) != [index]:
                return 'ambiguous', consumer_id, []
            if len(consumer_records) > 1:
                if (not matches_source_record(item, consumer_records[0], namespace)
                        or len(collapse_identical_records(consumer_records, namespace)) != 1):
                    return 'ambiguous', consumer_id, []
                consumer_records = consumer_records[:1]
            if (consumer_records[0][0].casefold() != consumer_id.casefold()
                    or not matches_source_record(item, consumer_records[0], namespace)):
                return 'unproven', consumer_id, []

        problems, rewrites, seen = [], [], set()
        for owner, name, _, start, _, end in source_refs:
            key = (owner.casefold() if owner else None, name.casefold())
            if key in seen:
                continue
            seen.add(key)
            before, after = formula[:start].rstrip(), formula[end:].lstrip()
            if owner is not None or before.endswith('.') or after.startswith('.'):
                problems.append(('unresolved', name))
                continue
            records = inventory.get(name.casefold(), [])
            physical = list(dict.fromkeys(physical_by_source.get(name.casefold(), [])))
            raw_match = any(record[0].casefold() == name.casefold() for record in records)

            # A caption is not a calculation ID. A uniquely identified
            # physical field may still have the same text as that caption.
            if not records or (not raw_match and physical):
                if not records and targets_by_source.get(name.casefold()):
                    problems.append(('unproven', name))
                    continue
                if len(physical) != 1:
                    problems.append(('ambiguous' if physical else 'unresolved', name))
                    continue
                status, changes = emitted_target_reference(physical[0], refs)
                if status != 'resolved':
                    problems.append((status, name))
                else:
                    rewrites.extend(changes)
                continue
            if physical or not raw_match:
                problems.append(('ambiguous', name))
                continue

            record = records[0]
            targets = targets_by_source.get(record[0].casefold(), [])
            if len(records) > 1:
                if (not matches_source_record(item, consumer_records[0], namespace)
                        or record[3] != namespace
                        or len(collapse_identical_records(records, namespace)) != 1):
                    problems.append(('ambiguous', name))
                    continue
            if len(targets) != 1:
                problems.append(('ambiguous' if targets else 'unresolved', name))
                continue
            target = targets[0]
            target_item = objects[target][2]
            if target == index or not matches_source_record(target_item, record, namespace):
                problems.append(('unproven', name))
                continue
            status, changes = emitted_target_reference(target, refs)
            if status != 'resolved':
                problems.append((status, name))
            else:
                rewrites.extend(changes)

        if problems:
            priority = {'ambiguous': 0, 'unproven': 1,
                        'target_ambiguous': 2, 'unresolved': 3}
            status, name = min(problems, key=lambda problem: priority[problem[0]])
            return status, name, []
        return 'resolved', None, list(dict.fromkeys(rewrites))

    def source_binding(index, refs):
        """Resolve only a single source ID to one explicitly identified measure."""
        _, _, item = objects[index]
        identity = item.get('_tableau_calculation_identity')
        if not isinstance(identity, dict) or not _tableau_source_identifier(identity.get('name')):
            return None
        source_ref = _single_source_reference(item)
        if source_ref is None or not refs:
            return None
        signatures = {(owner.casefold() if owner else None, name.casefold(), column_only)
                      for owner, name, column_only, _, _, _ in refs}
        if len(signatures) != 1 or refs[0][2]:
            return None
        owner, dax_name = refs[0][:2]
        exact = (by_name.get(dax_name.casefold(), []) if owner is None
                 else by_owner_name.get((owner.casefold(), dax_name.casefold()), []))
        # A qualified, uniquely identified physical column is not a measure.
        if owner is not None and len(exact) == 1 and objects[exact[0]][1] == 'columns':
            return None

        if source_inventory is None:
            # A local table lacks the complete source inventory. In
            # particular, a same-caption target might not have been built yet.
            renamed = (renamed_by_name.get(dax_name.casefold(), []) if owner is None
                       else renamed_by_owner_name.get(
                           (owner.casefold(), dax_name.casefold()), []))
            identities = []
            for candidate in dict.fromkeys([*exact, *renamed]):
                candidate_identity = objects[candidate][2].get('_tableau_calculation_identity')
                if isinstance(candidate_identity, dict):
                    raw = _tableau_source_identifier(candidate_identity.get('name'))
                    caption = candidate_identity.get('caption')
                    if raw and isinstance(caption, str):
                        identities.append((raw.casefold(), caption.casefold()))
            source_key = source_ref.casefold()
            if (len(identities) > 1
                    and any(raw == source_key for raw, _ in identities)
                    and all(caption != source_key for raw, caption in identities
                            if raw != source_key)):
                return 'unresolved', None, source_ref
            if (identities and all(source_ref.casefold() not in pair for pair in identities)
                    and any(caption == dax_name.casefold() for _, caption in identities)):
                return 'unresolved', None, source_ref
            return None

        records = source_inventory.get(source_ref.casefold(), [])
        if not records:
            return None
        consumer_id = _tableau_source_identifier(identity.get('name'))
        consumer_records = source_inventory.get(consumer_id.casefold(), [])
        if (records and consumer_records
                and (len(records) > 1 or len(consumer_records) > 1)):
            consumer_targets = targets_by_source.get(consumer_id.casefold(), [])
            source_targets = targets_by_source.get(records[0][0].casefold(), [])
            if consumer_targets == [index]:
                namespace = identity.get('datasource_name')
                if matches_source_record(item, consumer_records[0], namespace):
                    collapsed_consumer = collapse_identical_records(
                        consumer_records, namespace)
                    if (len(records) > 1 and not source_targets
                            and defer_source_binding
                            and records[0][3] == namespace
                            and len(collapsed_consumer) == 1
                            and len(collapse_identical_records(records, namespace)) == 1):
                        # The complete model must still prove the remote target.
                        return 'unresolved', None, source_ref
                    if len(source_targets) == 1:
                        target_item = objects[source_targets[0]][2]
                        if matches_source_record(target_item, records[0], namespace):
                            consumer_records = collapsed_consumer
                            records = collapse_identical_records(records, namespace)
        if len(consumer_records) > 1:
            return 'ambiguous', None, source_ref
        if (len(consumer_records) != 1
                or consumer_records[0][0].casefold() != consumer_id.casefold()
                or consumer_records[0][2] != item.get('_original_formula')):
            return 'unresolved', None, source_ref
        if len(records) != 1 or source_ref.casefold() in physical_names:
            return 'ambiguous', None, source_ref
        raw, caption = records[0][:2]
        # A caption alone is not proof of the underlying source ID.
        if raw.casefold() != source_ref.casefold():
            return None
        targets = targets_by_source.get(raw.casefold(), [])
        if len(targets) != 1:
            return ('ambiguous' if targets else 'unresolved'), None, source_ref
        target = targets[0]
        target_owner, kind, target_item = objects[target]
        target_identity = target_item.get('_tableau_calculation_identity') or {}
        target_caption = target_identity.get('caption') or raw
        if (kind != 'measures' or not isinstance(target_caption, str)
                or target_caption.casefold() != caption.casefold()):
            return 'unresolved', None, source_ref

        # Retarget only references known to originate from this caption,
        # including a previously namespaced namesake. Never alter literals.
        aliases = {raw.casefold(), caption.casefold(), target_item['name'].casefold()}
        aliases.update(caption_targets.get(caption.casefold(), ()))
        if (dax_name.casefold() not in aliases
                or (owner is not None and owner.casefold() != target_owner.casefold())):
            return 'unresolved', None, source_ref
        target_expr = target_item.get('expression', '')
        if (isinstance(target_expr, str) and target_expr
                and _dax_dependency_tokens(target_expr)[1]):
            # The consumer must fail irrespective of whether the target's
            # eventual DAX name collides with a column or another measure.
            return 'unsupported_target', target, source_ref
        if by_name.get(target_item['name'].casefold(), []) != [target]:
            return 'target_ambiguous', target, source_ref
        return 'resolved', target, source_ref

    def replace_with_diagnostic(index, reason, issue, ref_owner, ref_name, source_ref):
        owner, kind, item = objects[index]
        expression = item.get('expression', '')
        provenance = re.match(
            r'\s*/\*\s*Tableau calculation; migrated to DAX\b[\s\S]*?\*/\s*',
            expression, re.IGNORECASE)
        prefix = provenance.group(0) if provenance else ''
        item['expression'] = prefix + _unsupported_measure_expression(reason)
        diagnostics.append({
            'table': owner, 'kind': kind, 'field': item['name'],
            'reference_table': ref_owner, 'reference': ref_name,
            'source_reference': source_ref, 'issue': issue,
        })

    reverse = [[] for _ in objects]
    unsupported = set()
    diagnostics = []
    for index, (_, _, item) in enumerate(objects):
        expression = item.get('expression', '')
        if not isinstance(expression, str) or not expression:
            continue
        refs, marker = _dax_dependency_tokens(expression, include_spans=True)
        if marker:
            unsupported.add(index)
            continue
        multi_binding = multiple_source_bindings(index, refs)
        if multi_binding:
            status, source_ref, changes = multi_binding
            ref_owner, ref_name = refs[0][:2] if refs else (None, None)
            if status in ('unresolved', 'target_ambiguous') and defer_source_binding:
                diagnostics.append({
                    'table': objects[index][0], 'kind': objects[index][1],
                    'field': item['name'], 'reference_table': ref_owner,
                    'reference': ref_name, 'source_reference': source_ref,
                    'issue': 'source calculation binding requires final resolution',
                })
                continue
            if status != 'resolved':
                reason = ('ambiguous source calculation identity'
                          if status == 'ambiguous' else
                          'ambiguous target for source calculation identity'
                          if status == 'target_ambiguous' else
                          'unresolved source calculation identity or target')
                issue = ('unresolved source identity' if status == 'unproven'
                         else status + ' source identity')
                replace_with_diagnostic(index, reason, issue,
                                        ref_owner, ref_name, source_ref)
                unsupported.add(index)
                continue
            for bracket, end, target_name in sorted(changes, reverse=True):
                expression = (expression[:bracket] + '['
                              + target_name.replace(']', ']]') + ']'
                              + expression[end:])
            if changes:
                item['expression'] = expression
                refs, _ = _dax_dependency_tokens(expression, include_spans=True)
        binding = source_binding(index, refs)
        selected = None
        if binding:
            status, selected, source_ref = binding
            ref_owner, ref_name = refs[0][:2]
            if status in ('unresolved', 'target_ambiguous') and defer_source_binding:
                diagnostics.append({
                    'table': objects[index][0], 'kind': objects[index][1],
                    'field': item['name'], 'reference_table': ref_owner,
                    'reference': ref_name, 'source_reference': source_ref,
                    'issue': 'source calculation binding requires final resolution',
                })
                if selected is None:
                    continue
            elif status not in ('resolved', 'unsupported_target'):
                reason = ('ambiguous source calculation identity'
                          if status == 'ambiguous' else
                          'ambiguous target for source calculation identity'
                          if status == 'target_ambiguous' else
                          'unresolved source calculation identity or target')
                replace_with_diagnostic(index, reason, status + ' source identity',
                                        ref_owner, ref_name, source_ref)
                unsupported.add(index)
                continue
            if status == 'resolved':
                target_name = objects[selected][2]['name']
                for _, _, _, _, bracket, end in reversed(refs):
                    expression = (expression[:bracket] + '['
                                  + target_name.replace(']', ']]') + ']'
                                  + expression[end:])
                item['expression'] = expression
                refs, _ = _dax_dependency_tokens(expression, include_spans=True)
        for owner, name, column_only, _, _, _ in refs:
            exact = (by_name.get(name.casefold(), []) if owner is None
                     else by_owner_name.get((owner.casefold(), name.casefold()), []))
            renamed = (renamed_by_name.get(name.casefold(), []) if owner is None
                       else renamed_by_owner_name.get((owner.casefold(), name.casefold()), []))
            candidates = ([selected] if selected is not None else
                          list(dict.fromkeys([*exact, *renamed])))
            if column_only:
                columns = [candidate for candidate in candidates
                           if objects[candidate][1] == 'columns']
                if columns:
                    candidates = columns
                    renamed = [candidate for candidate in renamed if candidate in columns]
            resolution = ('unique' if selected is not None else
                          'ambiguous' if len(candidates) > 1 else
                          'unresolved' if renamed and not exact else 'unique')
            if owner is not None and not candidates and owner.casefold() in table_names:
                # Do not silently bind a wrongly qualified reference to a
                # namesake measure in another table. If that measure becomes
                # unsupported, report the unresolved identity explicitly.
                candidates = [candidate for candidate in by_name.get(name.casefold(), [])
                              if objects[candidate][1] == 'measures']
                if candidates:
                    resolution = 'unresolved'
            for dependency in candidates:
                # Physical columns without a DAX expression cannot acquire
                # a DAX TODO in this pass. Keep them for identity ambiguity
                # checks, but do not build unnecessary dependency edges.
                dependency_expr = objects[dependency][2].get('expression', '')
                if (dependency != index and isinstance(dependency_expr, str)
                        and dependency_expr):
                    reverse[dependency].append(
                        (index, resolution, owner, name))

    queue = deque(unsupported)
    deferred = set()
    while queue:
        dependency = queue.popleft()
        for index, resolution, ref_owner, ref_name in reverse[dependency]:
            if index in unsupported:
                continue
            owner, kind, item = objects[index]
            identity = (index, ref_owner, ref_name)
            if resolution == 'ambiguous' and not diagnose_ambiguous:
                if identity not in deferred:
                    deferred.add(identity)
                    diagnostics.append({
                        'table': owner, 'kind': kind, 'field': item['name'],
                        'reference_table': ref_owner, 'reference': ref_name,
                        'issue': 'ambiguous unsupported dependency requires final resolution',
                    })
                continue
            reason = {
                'ambiguous': 'ambiguous field identity may reference an unsupported calculation',
                'unresolved': 'unresolved qualified field identity may reference an unsupported calculation',
                'unique': 'depends on a calculation with unsupported source grain',
            }[resolution]
            # Keep any leading generated-provenance comment intact. Strings
            # and comments elsewhere are never treated as field references.
            expression = item.get('expression', '')
            provenance = re.match(
                r'\s*/\*\s*Tableau calculation; migrated to DAX\b[\s\S]*?\*/\s*',
                expression, re.IGNORECASE)
            prefix = provenance.group(0) if provenance else ''
            item['expression'] = prefix + _unsupported_measure_expression(reason)
            diagnostics.append({
                'table': owner, 'kind': kind, 'field': item['name'],
                'reference_table': ref_owner, 'reference': ref_name,
                'issue': ('unsupported dependency' if resolution == 'unique'
                          else f'{resolution} unsupported dependency'),
            })
            unsupported.add(index)
            queue.append(index)
    return diagnostics


def _validate_measures_after_rewrites(
        result_table, *, source_calculations=None, source_datasources=None):
    """Propagate local unsupported dependencies, then re-validate measures.

    ``source_datasources`` is the complete, original datasource inventory,
    retaining each datasource's name and full calculation declarations. It
    takes precedence over the legacy, flattened ``source_calculations``.
    A flattened inventory cannot prove that repeated declarations share a
    namespace. Bindings needing targets outside this table remain deferred to
    the final model-wide pass.
    """
    measures = result_table.get("measures", [])
    model = {'model': {'tables': [result_table]}}
    if source_datasources is not None:
        model['_datasources'] = source_datasources
    elif source_calculations is not None:
        model['_datasources'] = [{'calculations': source_calculations}]
    propagate_unsupported_model_dependencies(
        model, defer_source_binding=True)
    # ── Phase 3: Post-processing DAX validation sweep ──
    # After all rewrites (SUM-of-measure unwrap, bare column wrapping),
    # validate every measure expression one more time.
    try:
        from powerbi_import.dax_validator import (
            validate_dax_expression as _validate_dax,
        )
        for meas in measures:
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

    The model is available at this stage, so also propagate unsupported
    dependencies across its current tables. Ambiguous identities are
    diagnosed, not silently bound to a namesake. The semantic owner must still
    run a final model-wide sweep after later creation and renaming of fields.
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
        propagate_unsupported_model_dependencies(model, defer_source_binding=True)
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

    propagate_unsupported_model_dependencies(model, defer_source_binding=True)


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
            counts = {"IIF": (3, 4), "CONTAINS": (2,), "ISNULL": (1,), "NOT": (1,)}
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
            branch_types = {self.type_of(arg) for arg in args[1:]} - {"null"}
            if len(branch_types) > 1:
                raise _Unsupported("Mixed-type IF branches are outside this compiler")
            return next(iter(branch_types), "null")
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
        if kind == "if":
            args = node[1:]
            return "IF(%s, %s, %s)" % (
                self.truth(args[0], left_vars, null_right),
                self.scalar(args[1], left_vars, null_right),
                self.scalar(args[2], left_vars, null_right))
        if kind == "call" and node[1] == "IIF":
            args = node[2]
            condition = self.scalar(args[0], left_vars, null_right)
            unknown = (self.scalar(args[3], left_vars, null_right)
                       if len(args) == 4 else "BLANK()")
            return "IF(ISBLANK(%s), %s, IF(%s, %s, %s))" % (
                condition, unknown, condition,
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
        if kind == "if":
            condition, yes, no = (self.simplify(n, null_right) for n in node[1:])
            condition = self.condition(condition)
            if condition == ("bool", "TRUE"):
                return yes
            if condition in (("bool", "FALSE"), ("null",)):
                return no
            if yes == no:
                return yes
            return ("if", condition, yes, no)
        if kind == "call" and node[1] == "IIF":
            args = tuple(self.simplify(n, null_right) for n in node[2])
            condition, yes, no = args[:3]
            unknown = args[3] if len(args) == 4 else ("null",)
            if condition == ("bool", "TRUE"):
                return yes
            if condition == ("bool", "FALSE"):
                return no
            if condition == ("null",):
                return unknown
            if yes == no == unknown:
                return yes
            return ("call", "IIF", args)
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
        if node[0:2] == ("call", "IIF"):
            # Its NULL-test branch is not the ELSE branch. Do not prove a
            # zero-row pruning condition from only the TRUE/FALSE leaves.
            return None
        if node[0] == "if":
            condition, yes, no = node[1:]
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
    source numeric/date MAX, IF/ELSEIF/IIF (including its unknown branch),
    literal CONTAINS, ISNULL and scalar
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
