"""
DAX Formula Converter — Tableau formulas → DAX (Power BI)

Extracted from datasource_extractor.py for maintainability.
Converts Tableau calculation formulas to valid DAX expressions.
"""

import logging
import re
from typing import Any, cast

logger = logging.getLogger(__name__)

# ── Shared utility ────────────────────────────────────────────────────────────

def _reverse_tableau_bracket_escape(name):
    """Reverses the Tableau ] → ) substitution in column names.

    Tableau replaces ] with ) in physical column names because
    ] conflicts with its [field] syntax. To generate Power Query M that
    references the real column names in the source, we reverse this
    substitution when ) appears without a matching ( (orphan parenthesis).
    """
    opens = name.count('(')
    closes = name.count(')')
    excess = closes - opens
    if excess <= 0:
        return name
    result = list(name)
    replaced = 0
    for i in range(len(result) - 1, -1, -1):
        if result[i] == ')' and replaced < excess:
            result[i] = ']'
            replaced += 1
    return ''.join(result)


def sanitize_param_brackets(name):
    """Strip '[' and ']' from a Tableau parameter/identifier name.

    Tableau parameter captions may contain literal brackets (e.g.
    "AIP [Indicateur nationaux][detail]").  A DAX/TMDL bracketed identifier
    cannot contain a raw '[' and encodes ']' only via doubling, so emitting
    such a caption verbatim breaks bracket parsing.  Removing the brackets
    (and collapsing the resulting whitespace) yields a safe, stable name that
    is used consistently both when emitting a parameter reference in DAX and
    when naming the What-If parameter table/measure, so the two still match.
    """
    if not name or ('[' not in name and ']' not in name):
        return name
    cleaned = name.replace('[', ' ').replace(']', ' ')
    return re.sub(r'\s+', ' ', cleaned).strip()


# ── Tableau → DAX simple function mappings (table-driven) ─────────────────────
# Each tuple: (Tableau regex pattern, DAX replacement)
# Order matters — more specific patterns first

_SIMPLE_FUNCTION_MAP = [
    # User/security functions
    (r'\bUSERDOMAIN\s*\(\s*\)', '""  /* USERDOMAIN: no DAX equivalent — use RLS roles */'),

    # Null/logic
    (r'\bISNULL\b', 'ISBLANK'),
    (r'\bISNUMBER\s*\(', 'ISNUMBER('),
    (r'\bNOT\s*\(', 'NOT('),

    # Aggregation (before generic text/math to avoid conflicts)
    (r'\bAVG\s*\(', 'AVERAGE('),
    (r'\bCONTAINS\s*\(', 'CONTAINSSTRING('),
    (r'\bASCII\s*\(', 'UNICODE('),
    (r'\bCHAR\s*\(', 'UNICHAR('),
    # ATTR is handled separately in _convert_attr() — context-aware (measure vs column)

    # Date functions — misc
    # DATEADD handled by _convert_dateadd (scalar: EDATE/arithmetic)
    (r'\bTODAY\s*\(\s*\)', 'TODAY()'),
    (r'\bNOW\s*\(\s*\)', 'NOW()'),
    # DATENAME handled by _convert_datename (needs format string arg)
    # DATEPARSE handled by _convert_dateparse (needs value extraction)
    (r'\bMAKEDATE\s*\(', 'DATE('),
    (r'\bMAKETIME\s*\(', 'TIME('),

    # Text functions
    (r'\bTRIM\s*\(', 'TRIM('),
    # LTRIM/RTRIM handled by dedicated converters (_convert_ltrim, _convert_rtrim)
    (r'\bLEN\s*\(', 'LEN('),
    # LEFT/RIGHT are handled by _convert_left_right, which protects literals.
    (r'\bMID\s*\(', 'MID('),
    (r'\bUPPER\s*\(', 'UPPER('),
    (r'\bLOWER\s*\(', 'LOWER('),
    (r'\bREPLACE\s*\(', 'SUBSTITUTE('),
    (r'\bSPACE\s*\(', 'REPT(" ", '),
    (r'\bREPEAT\s*\(', 'REPT('),
    # REVERSE handled by _convert_reverse (no direct DAX equivalent)
    # FIND/FINDNTH handled by _convert_find (arg order swap needed)
    # ENDSWITH handled by _convert_endswith (needs decomposition)
    # STARTSWITH handled by _convert_startswith (needs decomposition)
    # PROPER handled by _convert_proper (no direct DAX equivalent)
    # SPLIT handled by _convert_split (no direct DAX equivalent)

    # Math functions
    (r'\bABS\s*\(', 'ABS('),
    (r'\bCEILING\s*\(', 'CEILING('),
    (r'\bFLOOR\s*\(', 'FLOOR('),
    (r'\bROUND\s*\(', 'ROUND('),
    (r'\bPOWER\s*\(', 'POWER('),
    (r'\bSQRT\s*\(', 'SQRT('),
    (r'\bLOG\s*\(', 'LOG('),
    (r'\bLN\s*\(', 'LN('),
    (r'\bEXP\s*\(', 'EXP('),
    (r'\bSIGN\s*\(', 'SIGN('),
    (r'\bPI\s*\(\s*\)', 'PI()'),
    # RADIANS/DEGREES handled by _convert_radians_degrees (no DAX equivalent)
    (r'\bSIN\s*\(', 'SIN('),
    (r'\bCOS\s*\(', 'COS('),
    (r'\bTAN\s*\(', 'TAN('),
    (r'\bACOS\s*\(', 'ACOS('),
    (r'\bASIN\s*\(', 'ASIN('),
    (r'\bATAN\s*\(', 'ATAN('),
    (r'\bCOT\s*\(', 'COT('),
    # ATAN2 handled by _convert_atan2 (two-arg → DAX formula)
    # DIV handled by _convert_div (→ QUOTIENT)

    # Statistical functions
    (r'\bMEDIAN\s*\(', 'MEDIAN('),
    (r'\bSTDEVP\s*\(', 'STDEV.P('),  # STDEVP before STDEV
    (r'\bSTDEV\s*\(', 'STDEV.S('),
    (r'\bVARP\s*\(', 'VAR.P('),      # VARP before VAR
    (r'\bVAR\s*\(', 'VAR.S('),
    # CORR/COVAR/COVARP handled by _convert_corr_covar (no direct DAX equivalent)

    # Type conversions
    (r'\bINT\s*\(', 'INT('),
    # FLOAT handled by _convert_float_to_convert (needs DOUBLE type arg)
    # STR handled by _convert_str_to_format (needs format string arg)
    # DATE / DATETIME casts handled by _convert_date_casts (arity-sensitive)

    # Aggregation (generic)
    (r'\bSUM\s*\(', 'SUM('),
    (r'\bMIN\s*\(', 'MIN('),
    (r'\bMAX\s*\(', 'MAX('),
    (r'\bCOUNT\s*\(', 'COUNT('),
    (r'\bCOUNTA\s*\(', 'COUNTA('),

    # Regex — REGEXP_MATCH, REGEXP_EXTRACT, REGEXP_EXTRACT_NTH, and REGEXP_REPLACE
    # are handled by dedicated converters called in Phase 3b-pre.

    # Table calculations — RUNNING_*/TOTAL handled by dedicated converters (_convert_running_functions, _convert_total_function)
    # RANK/RANK_UNIQUE/RANK_DENSE/RANK_MODIFIED/RANK_PERCENTILE handled by _convert_rank_functions
    # INDEX handled by dedicated converter (_convert_index)
    # PREVIOUS_VALUE and LOOKUP handled by dedicated converters below

    # WINDOW_* table calculations handled by _convert_window_functions (SUM/AVG/MAX/MIN/COUNT/MEDIAN/STDEV/VAR/PERCENTILE)
    # WINDOW_CORR/COVAR/COVARP also handled by dedicated converter in _convert_window_functions

]

# Pre-compile all patterns for performance
_COMPILED_FUNCTION_MAP = [(re.compile(pattern, re.IGNORECASE), replacement)
                           for pattern, replacement in _SIMPLE_FUNCTION_MAP]

# Pre-compiled static patterns used in the main converter and dedicated converters
_RE_ISMEMBEROF = re.compile(
    r'\bISMEMBEROF\s*\(\s*["\']([^"\']+)["\']\s*\)',
    re.IGNORECASE
)
_RE_NEWLINES = re.compile(r'[\r\n]+\s*')
# Consume identifiers and quoted tokens before considering NULL literals.
# Doubled delimiters and Tableau backslash escapes stay inside their token.
_RE_NULL_PROTECTED_TOKEN = re.compile(
    r'''"(?:[^"\\]|\\.|"")*"|'(?:[^'\\]|\\.|'')*'|\[(?:[^\]]|\]\])*\]'''
)
_RE_SECURITY_IDENTITY_TOKEN = re.compile(
    _RE_NULL_PROTECTED_TOKEN.pattern
    + r'|(?P<identity>\b(?:USERNAME|FULLNAME)\s*\(\s*\))',
    re.IGNORECASE,
)
_RE_TABLEAU_OPERATOR = re.compile(
    _RE_NULL_PROTECTED_TOKEN.pattern + r'|(?P<operator>!=|==|\bOR\b|\bAND\b)',
    re.IGNORECASE,
)
_TABLEAU_OPERATOR_MAP = {'!=': '<>', '==': '=', 'OR': '||', 'AND': '&&'}
_RE_COUNTD_TOKEN = re.compile(
    _RE_NULL_PROTECTED_TOKEN.pattern + r'|(?P<countd>\bCOUNTD\s*\()',
    re.IGNORECASE,
)
_RE_NULL_LITERAL = re.compile(
    _RE_NULL_PROTECTED_TOKEN.pattern
    + r'|(?P<null>(?<![\w.])NULL\b(?:\s*\(\s*\))?(?!\s*[\[(.]))',
    re.IGNORECASE,
)
# Escape-aware: a Tableau bracketed identifier may contain a literal ']'
# encoded as ']]' and literal '[' characters (e.g. a parameter caption like
# "AIP [Indicateur nationaux][detail]").  Match the full span so the whole
# reference is captured instead of truncating at the first ']'.
_RE_PARAM_REF = re.compile(r'\[Parameters\]\.\[((?:[^\]]|\]\])*)\]')
_RE_CALC_REF = re.compile(r'\[([^\]]+)\]')
_RE_ELSEIF = re.compile(r'\bELSEIF\b', re.IGNORECASE)
_RE_FINDNTH = re.compile(r'\bFINDNTH\s*\(', re.IGNORECASE)
_RE_DATE_LITERAL = re.compile(r'#(\d{4})-(\d{2})-(\d{2})#')
_RE_COLUMN_RESOLVE = re.compile(r"(?<!')\[([^\]]+)\]")
_RE_PREVIOUS_VALUE = re.compile(r'\bPREVIOUS_VALUE\s*\(', re.IGNORECASE)
_RE_LOOKUP = re.compile(r'\bLOOKUP\s*\(', re.IGNORECASE)
_RE_RAWSQL = re.compile(r'\bRAWSQL_(?:BOOL|INT|REAL|STR|DATE|DATETIME|SPATIAL)\s*\(', re.IGNORECASE)
_RE_FIND = re.compile(r'\bFIND\s*\(', re.IGNORECASE)
_RE_TOTAL = re.compile(r'\bTOTAL\s*\(', re.IGNORECASE)
_RE_LOD_NO_DIM = re.compile(
    r'\{\s*(SUM|AVG|AVERAGE|MIN|MAX|COUNT|COUNTD|MEDIAN)\s*\(',
    re.IGNORECASE
)

# Pattern cache for dynamic function-name-based patterns
_func_pattern_cache = {}


def _get_func_pattern(func_name, word_boundary=True):
    """Retrieve or create a compiled regex for matching ``func_name(``."""
    key = (func_name, word_boundary)
    pat = _func_pattern_cache.get(key)
    if pat is None:
        prefix = r'\b' + re.escape(func_name) if word_boundary else re.escape(func_name)
        pat = re.compile(prefix + r'\s*\(', re.IGNORECASE)
        _func_pattern_cache[key] = pat
    return pat


# ── Type mapping ──────────────────────────────────────────────────────────────

TABLEAU_TO_PBI_TYPE = {
    'string': 'String',
    'integer': 'Int64',
    'real': 'Double',
    'boolean': 'Boolean',
    'date': 'DateTime',
    'datetime': 'DateTime',
    'number': 'Double',
}


def map_tableau_to_powerbi_type(tableau_type):
    """Maps Tableau types to Power BI types."""
    return TABLEAU_TO_PBI_TYPE.get(tableau_type.lower(), 'String')


# ── Main converter ────────────────────────────────────────────────────────────

def convert_tableau_formula_to_dax(formula, column_name='Measure', table_name='Table',
                                    calc_map=None, param_map=None,
                                    column_table_map=None, measure_names=None,
                                    is_calc_column=False, param_values=None,
                                    calc_datatype=None, partition_fields=None,
                                    compute_using=None, table_columns=None,
                                    bool_columns=None,
                                    validate_output=False,
                                    fallback_on_invalid=False,
                                    verified_row_scope=None,
                                    verified_fixed_scope=None,
                                    fixed_shelf_aggregation=None,
                                    fixed_shelf_evidence='explicit',
                                    date_columns=None,
                                    lod_shelf_aggregation=None,
                                    numeric_columns=None,
                                    table_calc_context=None,
                                    row_approx_measure_names=()):
    """
    Converts a Tableau formula to DAX with context resolution.

    Args:
        formula: Raw Tableau formula
        column_name: Name of the calculated field (for debug)
        table_name: Name of the table containing this measure (fallback)
        calc_map: {raw_id: caption} to resolve references between calculations
        param_map: {raw_param_id: caption} to resolve parameters
        column_table_map: {column: table} to resolve cross-table columns
        measure_names: set of measure names (do NOT receive a table prefix)
        is_calc_column: True if the formula is for a calculated column (row-level)
        param_values: {parameter_caption: literal_value} to inline in calc columns
        calc_datatype: Tableau type ('string', 'real', etc.) for + → & conversion
        partition_fields: List of field names for table calc partitioning (COMPUTE USING)
            (deprecated — use compute_using instead)
        compute_using: list of dimension names for table calc addressing/partitioning
        validate_output: If True, run Phase 3 DAX validation on output
        fallback_on_invalid: If True and validation fails, return TODO/BLANK fallback
        verified_row_scope: Optional semantic-owner evidence for a narrowly
            supported, single-table SUM of a row expression. See
            ``_check_source_grain``; absent evidence never implies a row bag,
            shelf aggregation, or grand-total policy.
        verified_fixed_scope: Optional semantic-owner proof for a dedicated
            SUM of a single-table FIXED MIN at distinct FIXED-key grain. See
            ``_compile_verified_fixed_sum``. No proof is inferred here.
        fixed_shelf_aggregation: Independently resolved SUM/AVG/MIN/MAX shelf
            enabling a bounded physical-column FIXED approximation when exact
            proof is unavailable. Source filter scope and totals stay unverified.
        fixed_shelf_evidence: ``explicit``, ``inferred`` or ``conflicting``;
            non-explicit evidence is called out in the approximation comment.
        lod_shelf_aggregation: Explicit SUM/AVG/MIN/MAX shelf evidence for a
            bounded INCLUDE approximation. It never authorizes an inferred shelf.
        date_columns: Optional collection of local physical column names proven
            date/datetime by the semantic owner, permitting YEAR extrema lowering.
            Missing proof never implies a date type from syntax or output type.
        numeric_columns: Optional collection of local physical numeric columns
            proven by the semantic owner for bounded INCLUDE/EXCLUDE lowering
            and scalar MIN/MAX with a non-null numeric literal.
        table_calc_context: Optional worksheet table-calculation context. Its
            order_by metadata refines running-calculation diagnostics only; it
            does not prove mark grain, unique ordering, tie behavior or totals.
        row_approx_measure_names: Names of previously generated SUMX row-measure
            approximations; SUM around one of these may be unwrapped with a note.

    Returns:
        str: Valid DAX formula
    """
    if not formula or not formula.strip():
        return formula

    calc_map = calc_map or {}
    param_map = param_map or {}
    column_table_map = column_table_map or {}
    measure_names = measure_names or set()
    param_values = param_values or {}
    # Support both old partition_fields and new compute_using parameter
    if compute_using is None and partition_fields is not None:
        compute_using = partition_fields

    dax = formula.strip()
    if (not is_calc_column and table_columns is not None
            and re.fullmatch(r'(?:MIN|MAX)\s*\(\s*1\s*\)', dax, re.IGNORECASE)):
        safe_table_name = str(table_name).replace("'", "''")
        dax = f"IF(COUNTROWS('{safe_table_name}') > 0, 1, BLANK())"
        if validate_output:
            try:
                from powerbi_import.dax_validator import validate_dax_expression
                issues = validate_dax_expression(dax)
            except (ImportError, OSError, ValueError):
                issues = ['DAX validator unavailable']
            if issues and fallback_on_invalid:
                return _unsupported_conversion(
                    'MIN/MAX(1) presence measure failed DAX expression validation')
        return dax

    # Tableau comments may contain apostrophes and Tableau function names.
    # Remove them before quote conversion and validation.
    dax = _strip_tableau_comments(dax)
    dax = _normalize_tableau_quote_escapes(dax)
    dax = _normalize_tableau_group_references(dax)
    # Tableau's standalone NULL (and legacy NULL()) is a DAX BLANK().
    # Normalize before resolving references so captions/parameter values
    # introduced later cannot be mistaken for source-language literals.
    dax = _convert_null_literals(dax)
    fixed_source = dax

    # === Phase 0: Strip secondary-datasource prefixes ===
    # Tableau data blends use [federated.xxxID].[Column] references.
    # Published (sqlproxy) datasource cross-refs use [sqlproxy.xxxID].[Calculation_yyy].
    # Strip the prefix so the column/calc resolves against sibling tables/measures.
    dax = re.sub(r'\[(?:federated|sqlproxy)\.[^\]]*\]\.', '', dax)
    # Collapse cross-datasource row-id refs: [__tableau_internal_object_id__].[X]
    # represents the row-id of a secondary blended source. Drop the trailing
    # ".[X]" so any wrapping COUNT(...) still produces valid DAX.
    dax = re.sub(
        r'\[__tableau_internal_object_id__\]\.\[[^\]]+\]',
        '[__tableau_internal_object_id__]',
        dax,
    )

    # === Phase 1: Resolve Tableau references ===
    dax = _resolve_references(dax, calc_map, param_map, is_calc_column, param_values)

    # A FIXED value is not, in general, a measure at the visual's grain.
    # Neither a column-to-table map nor ALLEXCEPT proves source filter order,
    # shelf aggregation, or the subtotal/grand-total grain. In particular,
    # removing the FIXED dimensions in a multi-table expression is wrong.
    masked, restore = _protect_source_tokens(dax)
    has_source_lod = bool(re.search(
        r'\{\s*(?:FIXED|INCLUDE|EXCLUDE)\b', masked, re.IGNORECASE))
    if (table_columns is not None and re.search(
            r'\{\s*(?:INCLUDE|EXCLUDE)\b', masked, re.IGNORECASE)):
        lod_expression, lod_reason = _compile_context_lod(
            masked, restore, table_name=table_name,
            column_table_map=column_table_map, measure_names=measure_names,
            table_columns=table_columns, numeric_columns=numeric_columns,
            is_calc_column=is_calc_column,
            shelf=(lod_shelf_aggregation if lod_shelf_aggregation is not None
                   else fixed_shelf_aggregation))
        if lod_expression is None:
            return _unsupported_conversion(lod_reason)
        if validate_output:
            try:
                from powerbi_import.dax_validator import validate_dax_expression
                lod_issues = validate_dax_expression(lod_expression)
            except (ImportError, OSError, ValueError):
                lod_issues = []
            if lod_issues and fallback_on_invalid:
                return _unsupported_conversion(
                    'bounded INCLUDE/EXCLUDE expression failed DAX validation')
        return lod_expression
    if verified_fixed_scope is not None and re.search(
            r'\{\s*FIXED\b', masked, re.IGNORECASE):
        fixed_sum = _compile_verified_fixed_sum(
            masked, restore, table_name=table_name,
            column_table_map=column_table_map, measure_names=measure_names,
            table_columns=table_columns, is_calc_column=is_calc_column,
            scope=verified_fixed_scope)
        if fixed_sum is None and fixed_shelf_aggregation is None:
            return _unsupported_conversion(
                'FIXED SUM requires verified source filter scope, physical '
                'dimension grain, shelf aggregation and total grain')
        if fixed_sum is not None and validate_output:
            try:
                from powerbi_import.dax_validator import validate_dax_expression
                if validate_dax_expression(fixed_sum):
                    return _unsupported_conversion(
                        'verified FIXED SUM failed DAX expression validation')
            except (ImportError, OSError, ValueError):
                return _unsupported_conversion(
                    'verified FIXED SUM could not be validated')
        if fixed_sum is not None:
            return fixed_sum
    if fixed_shelf_aggregation is not None and re.search(
            r'\{\s*FIXED\b', masked, re.IGNORECASE):
        source_masked, source_restore = _protect_source_tokens(fixed_source)
        approximation = _compile_approximate_fixed_shelf(
            source_masked, source_restore, table_name=table_name,
            column_table_map=column_table_map, measure_names=measure_names,
            table_columns=table_columns, numeric_columns=numeric_columns,
            is_calc_column=is_calc_column,
            shelf=fixed_shelf_aggregation, calc_map=calc_map,
            shelf_evidence=fixed_shelf_evidence,
            row_approx_measure_names=row_approx_measure_names)
        if approximation is not None:
            return approximation
        ratio_approximation = _compile_approximate_fixed_aggregate_ratio(
            masked, restore, table_name=table_name,
            column_table_map=column_table_map, measure_names=measure_names,
            table_columns=table_columns, numeric_columns=numeric_columns,
            is_calc_column=is_calc_column,
            shelf=fixed_shelf_aggregation,
            row_approx_measure_names=row_approx_measure_names,
        )
        if ratio_approximation is not None:
            return ratio_approximation
        return _unsupported_conversion(
            'FIXED shelf approximation requires supported single-table '
            'physical dimensions and a direct physical-column aggregation')
    if re.search(r'\{\s*FIXED\b', masked, re.IGNORECASE):
        approximation = _compile_approximate_fixed_aggregate_ratio(
            masked, restore, table_name=table_name,
            column_table_map=column_table_map, measure_names=measure_names,
            table_columns=table_columns, numeric_columns=numeric_columns,
            is_calc_column=is_calc_column,
            row_approx_measure_names=row_approx_measure_names,
        )
        if approximation is not None:
            return approximation
        if table_columns is not None:
            return _unsupported_conversion(
                'FIXED requires verified source filter scope, dimension grain, '
                'shelf aggregation and total grain')
    if _aggregate_contains_lod(masked):
        return _unsupported_conversion(
            'aggregation of a level-of-detail expression requires its source '
            'shelf and total grain')

    # === Phase 2: Convert CASE/WHEN → SWITCH(), IF/THEN → IF() ===
    # The structural regexes must not see keywords in strings or field names.
    dax = restore(_convert_if_structure(_convert_case_structure(masked)))

    row_approx_state = {}
    dax, grain_issue = _prepare_source_expression(
        dax, table_name=table_name, column_table_map=column_table_map,
        measure_names=measure_names, is_calc_column=is_calc_column,
        table_columns=table_columns, calc_datatype=calc_datatype,
        verified_row_scope=verified_row_scope, date_columns=date_columns,
        numeric_columns=numeric_columns,
        row_approx_measure_names=row_approx_measure_names,
        nonphysical_names={name for mapping in (calc_map, param_map)
                           for name in (*mapping.keys(), *mapping.values())
                           if isinstance(name, str)},
        _row_approx_out=row_approx_state,
    )
    if grain_issue:
        return _unsupported_conversion(grain_issue)

    # === Phase 3: Convert Tableau functions → DAX ===

    # 3a. ISMEMBEROF (special — captures group name)
    dax = _RE_ISMEMBEROF.sub(
        r'TRUE()  /* ISMEMBEROF("\1"): implement via RLS role */',
        dax
    )

    # 3b-pre. Dedicated converters (functions needing special arg handling)
    dax = _convert_previous_value(dax, table_name, compute_using=compute_using,
                                   column_table_map=column_table_map)
    dax = _convert_lookup(dax, table_name, compute_using=compute_using,
                           column_table_map=column_table_map)
    dax = _convert_radians_degrees(dax)
    dax = _convert_find(dax)
    dax = _convert_str_to_format(dax)
    dax = _convert_float_to_convert(dax)
    dax = _convert_int_tolerant(dax)
    dax = _convert_datename(dax)
    dax = _convert_dateadd(dax)
    dax = _convert_dateparse(dax)
    dax = _convert_date_casts(dax)
    dax = _convert_isdate(dax)
    dax = _convert_corr_covar(dax, table_name)
    dax = _convert_endswith(dax)
    dax = _convert_startswith(dax)
    dax = _convert_proper(dax)
    dax = _convert_reverse(dax)
    dax = _convert_split(dax)
    dax = _convert_index(dax)
    dax = _convert_size(dax)
    dax = _convert_atan2(dax)
    dax = _convert_div(dax)
    dax = _convert_square(dax)
    dax = _convert_iif(dax)
    regex_series_value_token = '__TTPBI_GENERATESERIES_VALUE__'
    while regex_series_value_token.casefold() in dax.casefold():
        regex_series_value_token = '_' + regex_series_value_token
    dax = _convert_regexp_match(dax, value_column_ref=regex_series_value_token)
    dax = _convert_regexp_extract(dax)
    dax = _convert_regexp_extract_nth(dax)
    dax = _convert_regexp_replace(dax)
    dax = _convert_rawsql(dax)
    dax = _convert_attr(dax, measure_names)

    # COUNTD ignores NULL. Protect literals and identifiers while identifying
    # Tableau calls so a string containing "COUNTD(" stays unchanged.
    dax = _RE_COUNTD_TOKEN.sub(
        lambda match: 'DISTINCTCOUNTNOBLANK(' if match.group('countd')
        else match.group(0),
        dax,
    )

    # Map identity calls without modifying string literals or identifiers that
    # happen to contain their names. The RLS caller must independently verify
    # the physical field binding and Tableau-to-Power BI identity mapping.
    dax = _RE_SECURITY_IDENTITY_TOKEN.sub(
        lambda match: 'USERPRINCIPALNAME()' if match.group('identity')
        else match.group(0),
        dax,
    )

    # 3b. Apply all simple function mappings (table-driven)
    dax = _convert_datetrunc(
        dax, table_name=table_name, table_columns=table_columns,
        date_columns=date_columns, column_table_map=column_table_map,
        measure_names=measure_names,
        nonphysical_names={name for mapping in (calc_map, param_map)
                           for name in (*mapping.keys(), *mapping.values())
                           if isinstance(name, str)},
        is_calc_column=is_calc_column,
    )
    dax = _convert_datepart_direct_units(dax)
    dax = _convert_unsupported_functions(dax)
    masked_functions, restore_functions = _protect_source_tokens(dax)
    for compiled_pattern, replacement in _COMPILED_FUNCTION_MAP:
        masked_functions = compiled_pattern.sub(replacement, masked_functions)
    dax = restore_functions(masked_functions)

    # 3b-post. Fix functions needing additional arguments
    dax = _convert_left_right(dax)
    dax = _convert_ltrim(dax)
    dax = _convert_rtrim(dax, value_column_ref=regex_series_value_token)
    dax = _fix_ceiling_floor(dax)

    # 3c. Special functions requiring argument reordering
    dax = _convert_datediff(dax)
    dax = _convert_zn(dax)
    dax = _convert_ifnull(dax)

    # 3d. LOD Expressions → CALCULATE
    dax = _convert_lod_expressions(dax, table_name, column_table_map)
    masked_lod, _ = _protect_source_tokens(dax)
    if '/* TODO: DAX conversion validation failed for ' in masked_lod:
        if has_source_lod:
            return _unsupported_conversion(
                'level-of-detail expression requires verified source and target grain')
        return dax

    # 3e. WINDOW_xxx table calculations
    dax = _convert_window_functions(dax, table_name, compute_using=compute_using,
                                     column_table_map=column_table_map,
                                     partition_fields=partition_fields)

    # 3f. RANK / RANK_UNIQUE / RANK_DENSE → RANKX
    dax = _convert_rank_functions(dax, table_name, compute_using=compute_using,
                                   column_table_map=column_table_map)

    # 3g. RUNNING_SUM/AVG/COUNT/MAX/MIN → table calculations
    dax = _convert_running_functions(dax, table_name, table_calc_context)

    # 3h. Percent of Total (TOTAL function or pcto: prefix)
    dax = _convert_total_function(dax, table_name)

    # === Phase 4: Convert operators ===
    # Quoted values and identifiers can contain operator-looking text.
    dax = _RE_TABLEAU_OPERATOR.sub(
        lambda match: _TABLEAU_OPERATOR_MAP[match.group('operator').upper()]
        if match.group('operator') else match.group(0),
        dax,
    )
    dax = _convert_makedatetime(
        dax, table_name=table_name, table_columns=table_columns,
        column_table_map=column_table_map, measure_names=measure_names,
        nonphysical_names={name for mapping in (calc_map, param_map)
                           for name in (*mapping.keys(), *mapping.values())
                           if isinstance(name, str)},
        is_calc_column=is_calc_column)

    # === Phase 5: Resolve remaining columns [col] → 'Table'[col] ===
    dax = _resolve_columns(dax, table_name, column_table_map, measure_names,
                           is_calc_column, param_values, table_columns=table_columns)
    dax = _convert_percentile(dax)

    dax, startof_issue = _convert_startof_scalar_extrema(
        dax, table_name=table_name, table_columns=table_columns,
        date_columns=date_columns, column_table_map=column_table_map,
        measure_names=measure_names,
        nonphysical_names={name for mapping in (calc_map, param_map)
                           for name in (*mapping.keys(), *mapping.values())
                           if isinstance(name, str)},
    )
    if startof_issue:
        return _unsupported_conversion(startof_issue)

    # === Phase 5a: Fix STARTOF* for calculated columns ===
    if is_calc_column:
        dax = _fix_startof_calc_columns(dax)

    # === Phase 5b: Convert AGG(IF(...)) → AGGX('table', IF(...)) ===
    dax = _convert_agg_if_to_aggx(dax, table_name)

    # === Phase 5c: Convert AGG(multi-col expr) → AGGX('table', expr) ===
    # DAX SUM/AVERAGE/MIN/MAX/COUNT only accept a single column.
    # Expressions like SUM('T'[a] * 'T'[b]) must use SUMX('T', 'T'[a] * 'T'[b]).
    dax = _convert_agg_expr_to_aggx(dax, table_name)

    # === Phase 5d: String concatenation + → & ===
    if calc_datatype and calc_datatype.lower() in ('string', 'str'):
        dax = _convert_string_concat(dax)

    # === Phase 5e: Tableau single-quoted string literals → DAX double-quoted ===
    dax = _convert_single_quoted_strings(dax)

    # === Phase 5f: Fix ROUND with single argument → ROUND(x, 0) ===
    dax = _fix_round_single_arg(dax)

    # === Phase 5g: Fix double-quoted table names → single-quoted ===
    # _convert_single_quoted_strings may incorrectly convert table names
    # in complex DAX (e.g. FILTER("Table",...) or "Table"[Col]).
    # Fix "Table"[Col] → 'Table'[Col] and FUNC("Table", → FUNC('Table',
    dax = _fix_double_quoted_table_refs(dax)

    # Tableau COUNTD accepts row expressions and ignores NULL; DAX's
    # DISTINCTCOUNTNOBLANK accepts only a physical column.
    dax = _convert_tableau_countd(dax, table_name)

    # === Phase 5h: Wrap bare column refs in measures with MAX() ===
    # In measure context, bare 'Table'[Col] references (not inside an
    # aggregation/iterator) cause PBI "single value cannot be determined".
    # Skip when wrapping the whole row expression in an iterator below.
    _wrap_in_iterator = (
        bool(row_approx_state.get('unaggregated_row_approx'))
        and not row_approx_state.get('has_agg_calls')
        and bool(table_name)
        and (calc_datatype or '').lower() not in ('boolean', 'bool')
    )
    if not is_calc_column and table_columns and not _wrap_in_iterator:
        dax = _wrap_bare_column_refs_in_measure(dax, table_name,
                                                 table_columns, measure_names,
                                                 bool_columns=bool_columns)

    # === Phase 6: Final cleanup ===
    dax = _ensure_comparison_spacing(dax)
    dax = _normalize_spaces_outside_identifiers(dax).strip()
    # Strip // line comments before collapsing newlines — otherwise
    # the comment swallows the rest of the single-line DAX/M expression.
    dax = _strip_tableau_comments(dax, keep_block_comments=True,
                                  tableau_string_escapes=False)
    dax = _RE_NEWLINES.sub(' ', dax)

    # === Phase 6b: Fix date literals ===
    dax = _fix_date_literals(dax)

    # NULLIF emits DAX strict equality; keep it after Tableau == normalization.
    dax = _convert_nullif(dax)

    # Tableau undefined ratios are NULL, not the NaN/Infinity produced by /.
    # Parse operators rather than matching slashes in strings/identifiers.
    dax = _convert_measure_ref_ratio(dax, measure_names)
    dax = _convert_safe_division(dax)
    dax = _convert_datepart_quarter(dax)
    dax = _convert_datepart_week(dax)
    dax = dax.replace(regex_series_value_token, '[Value]')

    if row_approx_state.get('unaggregated_row_approx'):
        _row_note = (
            'Unaggregated row expression best-effort approximation; '
            'row scope, shelf aggregation and total grain unverified'
        )
        if _wrap_in_iterator:
            _shelf = (fixed_shelf_aggregation or lod_shelf_aggregation or 'SUM').strip().upper()
            _iter_map = {'SUM': 'SUMX', 'AVG': 'AVERAGEX', 'AVERAGE': 'AVERAGEX',
                         'MIN': 'MINX', 'MAX': 'MAXX'}
            _iterator = _iter_map.get(_shelf, 'SUMX')
            if (_iterator == 'SUMX'
                    and (calc_datatype or '').lower() in ('string', 'str', 'date', 'datetime')):
                _iterator = 'MAXX'
            _safe_tbl = "'" + table_name.replace("'", "''") + "'"
            dax = f'/* {_row_note} */ {_iterator}({_safe_tbl}, {dax})'
        else:
            dax = f'/* {_row_note} */ {dax}'
    if row_approx_state.get('row_measure_sum_unwrapped'):
        names = ', '.join(row_approx_state['row_measure_sum_unwrapped'])
        note = (
            'Row-measure aggregation best-effort approximation; SUM over '
            f'{names} unwrapped to its existing SUMX; source shelf and total '
            'grain unverified'
        )
        dax = f'/* {note} */ {dax}'

    # === Phase 7: Optional conversion guard validation (Phase 3 foundation) ===
    if validate_output:
        try:
            from powerbi_import.dax_validator import validate_dax_expression
            issues = validate_dax_expression(dax)
        except (ImportError, OSError, ValueError):
            issues = []

        if issues and fallback_on_invalid:
            preview = issues[0]
            return (
                f'/* TODO: DAX conversion validation failed for {column_name}: {preview} */ '
                'BLANK()'
            )

    return dax


def _unsupported_conversion(reason):
    """Return the existing, auditable conversion-failure marker.

    Reasons are static diagnostics, not source text or identifiers. In
    particular they cannot terminate the DAX comment or leak a formula.
    """
    logger.warning("Unsupported Tableau calculation: %s", reason)
    return ('/* TODO: DAX conversion validation failed for measure: '
            + reason + ' */ BLANK()')


def _protect_source_tokens(text):
    """Hide quoted values and bracketed identifiers from structural regexes."""
    prefix = '__tableau_token_'
    while prefix.casefold() in text.casefold():
        prefix = '_' + prefix
    tokens = []

    def protect(match):
        tokens.append(match.group(0))
        return f'{prefix}{len(tokens) - 1}__'

    masked = _RE_NULL_PROTECTED_TOKEN.sub(protect, text)
    pattern = re.compile(re.escape(prefix) + r'(\d+)__')

    def restore(value):
        return pattern.sub(lambda match: tokens[int(match.group(1))], value)

    return masked, restore


def _normalize_source_control_syntax(text):
    masked, restore = _protect_source_tokens(text)
    return restore(_convert_if_structure(_convert_case_structure(masked)))


def _aggregate_contains_lod(masked):
    """Detect an aggregate enclosing an LOD without inspecting literal tokens."""
    stack = []
    for pos, char in enumerate(masked):
        if char == '(':
            end = pos
            while end and masked[end - 1].isspace():
                end -= 1
            start = end
            while start and (masked[start - 1].isalnum() or masked[start - 1] == '_'):
                start -= 1
            stack.append(masked[start:end].upper())
        elif char == ')':
            if stack:
                stack.pop()
        elif char == '{' and any(name in _SOURCE_AGGREGATES for name in stack):
            if re.match(r'\s*(?:FIXED|INCLUDE|EXCLUDE)\b|'
                        r'\s*(?:SUM|AVG|AVERAGE|MIN|MAX|COUNT|COUNTD|MEDIAN)\s*\(',
                        masked[pos + 1:], re.IGNORECASE):
                return True
    return False


class _SourceExpressionError(ValueError):
    pass


_SOURCE_TOKEN = re.compile(
    r'\s+|' + _RE_NULL_PROTECTED_TOKEN.pattern
    + r'|#[^#]*#'
    + r'|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
    + r'|[^\W\d]\w*(?:\.[^\W\d]\w*)*'
    + r'|==|!=|<>|<=|>=|&&|\|\||[()+\-*/^&=<>!,]',
    re.UNICODE,
)


class _SourceExpressionParser:
    """Bounded scalar grammar for source grain and nullable-logic checks.

    It is intentionally not a general DAX parser. An expression requiring a
    nullable rewrite or row-grain decision that it cannot parse fails closed.
    """

    _precedence = {
        'OR': 10, '||': 10, 'AND': 20, '&&': 20,
        '=': 30, '==': 30, '!=': 30, '<>': 30,
        '<': 30, '<=': 30, '>': 30, '>=': 30,
        '&': 40, '+': 50, '-': 50, '*': 60, '/': 60, '^': 80,
    }

    def __init__(self, text):
        self.tokens = []
        end = 0
        for match in _SOURCE_TOKEN.finditer(text):
            if match.start() != end:
                raise _SourceExpressionError("Unsupported source token")
            end = match.end()
            if not match.group().isspace():
                self.tokens.append(match.group())
        if end != len(text) or len(self.tokens) > 4000:
            raise _SourceExpressionError("Unsupported or oversized source expression")
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else ''

    def take(self, expected=None):
        token = self.peek()
        if not token or (expected is not None and token != expected):
            raise _SourceExpressionError("Incomplete source expression")
        self.pos += 1
        return token

    def parse(self):
        result = self.expression()
        if self.peek():
            raise _SourceExpressionError("Unsupported trailing source expression")
        return result

    def expression(self, minimum=0):
        token = self.take()
        upper = token.upper()
        if token in ('+', '-') or upper == 'NOT':
            node = ('unary', upper, self.expression(25 if upper == 'NOT' else 70))
        elif token == '(':
            node = ('group', self.expression())
            self.take(')')
        elif token.startswith('['):
            node = ('ref', token, None, token[1:-1].replace(']]', ']'))
        elif token.startswith(("'", '"')) or token.startswith('#'):
            if token.startswith("'") and self.peek().startswith('['):
                col = self.take()
                node = ('ref', token + col, token[1:-1].replace("''", "'"),
                        col[1:-1].replace(']]', ']'))
            else:
                node = ('atom', token)
        elif re.fullmatch(r'[^\W\d]\w*(?:\.[^\W\d]\w*)*', token, re.UNICODE):
            if self.peek().startswith('['):
                col = self.take()
                node = ('ref', token + col, token,
                        col[1:-1].replace(']]', ']'))
            elif self.peek() == '(':
                self.take('(')
                args = []
                if self.peek() != ')':
                    while True:
                        if self.peek() in (',', ')'):
                            args.append(('missing',))
                        else:
                            args.append(self.expression())
                        if self.peek() != ',':
                            break
                        self.take(',')
                self.take(')')
                node = ('call', token, tuple(args))
            else:
                node = ('atom', token)
        elif re.fullmatch(r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', token):
            node = ('atom', token)
        else:
            raise _SourceExpressionError("Unsupported source operand")

        while self.peek() and self.peek().upper() in self._precedence:
            operator = self.peek().upper()
            priority = self._precedence[operator]
            if priority < minimum:
                break
            self.take()
            node = ('op', operator, node, self.expression(priority + 1))
        return node


_SOURCE_AGGREGATES = frozenset({
    'SUM', 'AVG', 'AVERAGE', 'MIN', 'MAX', 'COUNT', 'COUNTD', 'COUNTA',
    'MEDIAN', 'STDEV', 'STDEVP', 'VAR', 'VARP', 'PERCENTILE', 'ATTR',
    'CORR', 'COVAR', 'COVARP',
})
_PAIRED_SOURCE_AGGREGATES = frozenset({'CORR', 'COVAR', 'COVARP'})
_SOURCE_COMPARISONS = frozenset({'=', '==', '!=', '<>', '<', '<=', '>', '>='})
_SOURCE_BOOLEAN = frozenset({'AND', 'OR', '&&', '||'})


def _source_children(node):
    if node[0] == 'call':
        return node[2]
    if node[0] == 'op':
        return node[2:]
    if node[0] in ('unary', 'group'):
        return node[-1:]
    return ()


def _source_walk(node):
    yield node
    for child in _source_children(node):
        yield from _source_walk(child)


_NUMERIC_SCALAR_SOURCE_ARITIES = {
    'ABS': {1}, 'ROUND': {1, 2}, 'POWER': {2}, 'SQRT': {1},
    'EXP': {1}, 'LN': {1}, 'LOG': {2}, 'SIGN': {1},
    'MIN': {2}, 'MAX': {2},
}
_NUMERIC_ROW_ITERATORS = {
    'SUM': 'SUMX', 'AVG': 'AVERAGEX', 'AVERAGE': 'AVERAGEX',
    'MIN': 'MINX', 'MAX': 'MAXX',
}


def _ungroup_source(node):
    while node[0] == 'group':
        node = node[1]
    return node


def _is_numeric_scalar_source(node, is_numeric_reference):
    """Check a scalar AST without accepting aggregates or unknown functions."""
    node = _ungroup_source(node)
    if node[0] == 'ref':
        return is_numeric_reference(node)
    if node[0] == 'atom':
        return re.fullmatch(
            r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', node[1]) is not None
    if node[0] == 'group':
        return _is_numeric_scalar_source(node[1], is_numeric_reference)
    if node[0] == 'unary':
        return (node[1] in ('+', '-')
                and _is_numeric_scalar_source(node[2], is_numeric_reference))
    if node[0] == 'op':
        return (node[1] in ('+', '-', '*', '/', '^')
                and _is_numeric_scalar_source(node[2], is_numeric_reference)
                and _is_numeric_scalar_source(node[3], is_numeric_reference))
    if node[0] == 'call':
        name, args = node[1].upper(), node[2]
        if name == 'IF':
            branch_indexes = (1,) if len(args) == 2 else (
                (1, 2) if len(args) == 3 else ())
        elif name == 'IIF':
            branch_indexes = (1, 2) if len(args) == 3 else (
                (1, 2, 3) if len(args) == 4 else ())
        else:
            branch_indexes = ()
        if name in ('IF', 'IIF'):
            return bool(branch_indexes) and all(
                _is_numeric_scalar_source(args[index], is_numeric_reference)
                for index in branch_indexes
            )
        arities = _NUMERIC_SCALAR_SOURCE_ARITIES.get(name, set())
        return (len(args) in arities and all(
            _is_numeric_scalar_source(arg, is_numeric_reference) for arg in args))
    return False


def _compile_numeric_row_aggregate(aggregate, expression, *, table_name,
                                   table_columns, numeric_columns,
                                   column_table_map, measure_names, source_text):
    """Lower a whitelisted numeric row expression over local physical rows."""
    iterator = _NUMERIC_ROW_ITERATORS.get(aggregate.upper())
    if (iterator is None or not isinstance(table_name, str) or not table_name
            or not isinstance(table_columns, (list, tuple, set, frozenset))
            or not isinstance(numeric_columns, (list, tuple, set, frozenset))):
        return None
    available = set(table_columns)
    numeric = set(numeric_columns)
    measures = {name.casefold() for name in measure_names
                if isinstance(name, str)}
    references = [node for node in _source_walk(expression) if node[0] == 'ref']

    def is_numeric_reference(node):
        _, _, owner, name = node
        return (name in available and name in numeric
                and name.casefold() not in measures
                and (owner is None or owner == table_name)
                and column_table_map.get(name, table_name) == table_name)

    if (not references
            or not _is_numeric_scalar_source(expression, is_numeric_reference)):
        return None

    table = "'" + table_name.replace("'", "''") + "'"

    def qualify(node):
        if node[0] == 'ref':
            name = node[3]
            return ('ref', table + '[' + name.replace(']', ']]') + ']',
                    table_name, name)
        if node[0] == 'op':
            return (node[0], node[1], qualify(node[2]), qualify(node[3]))
        if node[0] in ('unary', 'group'):
            return (*node[:-1], qualify(node[-1]))
        if node[0] == 'call':
            return (node[0], node[1], tuple(qualify(argument) for argument in node[2]))
        return node

    scalar = _render_nullable_source(
        qualify(expression), source_text, 'number', True,
        null_numeric_functions=True)
    return f'{iterator}({table}, {scalar})'


def _scalar_numeric_minmax_reference(node, *, table_name, table_columns,
                                     numeric_columns, column_table_map,
                                     measure_names, nonphysical_names):
    """Return the physical operand only for the bounded binary numeric case."""
    node = _ungroup_source(node)
    collections = (list, tuple, set, frozenset)
    if (node[0] != 'call' or node[1].upper() not in ('MIN', 'MAX')
            or len(node[2]) != 2 or not isinstance(table_name, str)
            or not table_name or not isinstance(table_columns, collections)
            or not isinstance(numeric_columns, collections)):
        return None
    operands = [_ungroup_source(arg) for arg in node[2]]
    for reference, literal in (operands, operands[::-1]):
        if reference[0] != 'ref':
            continue
        if literal[0] == 'unary' and literal[1] in ('+', '-'):
            literal = _ungroup_source(literal[2])
        if (literal[0] != 'atom' or re.fullmatch(
                r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', literal[1]) is None):
            continue
        owner, name = reference[2:]
        excluded = {item.casefold() for item in (*measure_names, *nonphysical_names)
                    if isinstance(item, str)}
        if (name in table_columns and name in numeric_columns
                and name.casefold() not in excluded
                and (owner is None or owner == table_name)
                and column_table_map.get(name, table_name) == table_name):
            return reference
    return None


def is_supported_scalar_numeric_minmax(formula, *, table_name, table_columns,
                                        numeric_columns, column_table_map=None,
                                        measure_names=None, calc_map=None,
                                        param_map=None):
    """Recognize a whole scalar MIN/MAX formula for pre-conversion classification.

    Physical and numeric inventories must exclude calculations, parameters,
    ambiguous bindings and declaration conflicts. Maps additionally veto raw
    calculation/parameter identifiers and captions. True permits row-column
    classification only; it proves neither measure scope nor aggregate totals.
    Pass the same inventories/maps to convert_tableau_formula_to_dax with
    is_calc_column=True after independently establishing local row context.
    """
    if not isinstance(formula, str):
        return False
    try:
        root = _SourceExpressionParser(_normalize_tableau_quote_escapes(
            _strip_tableau_comments(formula)).strip()).parse()
        return _scalar_numeric_minmax_reference(
            root, table_name=table_name, table_columns=table_columns,
            numeric_columns=numeric_columns,
            column_table_map=column_table_map or {},
            measure_names=measure_names or (),
            nonphysical_names={name for mapping in (calc_map or {}, param_map or {})
                               for name in (*mapping.keys(), *mapping.values())
                               if isinstance(name, str)},
        ) is not None
    except (_SourceExpressionError, RecursionError):
        return False


def _compile_verified_fixed_sum(masked, restore, *, table_name, column_table_map,
                                measure_names, table_columns, is_calc_column,
                                scope):
    """Compile only an explicitly proven SUM of single-table FIXED MIN.

    The semantic owner must supply all of the following, for *every* target
    use including cross-filtering, security and totals:

    * ``table``, ``fixed_dimensions``, ``value_column`` and
      ``physical_fields`` identify actual columns in one physical table;
    * ``source_filters`` and ``target_filters`` are explicit lists of filter
      records, each with nonempty ``id`` and ``predicate`` strings (an empty
      list states that none exist). ``filter_policy`` is
      ``target_context_equals_fixed_input_per_key``;
    * ``view_grains`` lists every verified view grain, each a subset of the
      FIXED dimensions, and includes the explicitly verified grand total
      ``[]``. ``total_grain`` is ``[]`` and ``total_aggregation`` is
      ``SUM_DISTINCT_FIXED_KEYS``;
    * ``shelf_aggregation`` is ``SUM`` for a bare FIXED, or ``AGG`` for an
      explicit ``SUM({FIXED ...})``. ``value_type`` is ``numeric``;
    * the eight ``*_verified`` assertions below are literal booleans. They
      cover the row bag, physical bindings, fixed-key domain and collation,
      source/target filters, numeric/NULL behavior, all view and total uses,
      and target engine support.

    Filter records document the proof; this function does not install or
    independently verify filters. A bare assertion is not evidence that a
    workbook or a future interaction satisfies the contract.
    """
    if (not isinstance(scope, dict) or is_calc_column or table_columns is None
            or not isinstance(table_columns, (list, tuple, set, frozenset))
            or any(not isinstance(name, str) or not name for name in table_columns)
            or scope.get('table') != table_name):
        return None
    match = re.fullmatch(
        r'\s*(?:(?P<outer>SUM)\s*\(\s*)?'
        r'\{\s*FIXED\s+(?P<dims>[^:{}()]+)\s*:\s*'
        r'MIN\s*\(\s*(?P<value>[^(){}:]+?)\s*\)\s*\}'
        r'\s*(?(outer)\))\s*',
        masked, re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    if scope.get('shelf_aggregation') != ('AGG' if match.group('outer') else 'SUM'):
        return None
    if (scope.get('value_type') != 'numeric'
            or scope.get('filter_policy') != 'target_context_equals_fixed_input_per_key'
            or scope.get('total_grain') != []
            or scope.get('total_aggregation') != 'SUM_DISTINCT_FIXED_KEYS'):
        return None
    if not all(scope.get(key) is True for key in (
            'source_row_bag_verified', 'physical_fields_verified',
            'fixed_key_domain_verified', 'filter_equivalence_verified',
            'group_key_and_blank_equivalence_verified',
            'numeric_and_null_equivalence_verified',
            'all_target_uses_and_totals_verified', 'engine_support_verified')):
        return None
    for key in ('source_filters', 'target_filters'):
        filters = scope.get(key)
        if not isinstance(filters, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get('id'), str) or not item['id'].strip()
                or not isinstance(item.get('predicate'), str) or not item['predicate'].strip()
                for item in filters):
            return None

    try:
        dimensions = [
            _ungroup_source(_SourceExpressionParser(restore(part)).parse())
            for part in match.group('dims').split(',')
        ]
        value = _ungroup_source(
            _SourceExpressionParser(restore(match.group('value'))).parse())
    except (_SourceExpressionError, RecursionError):
        return None
    if (not dimensions or len(dimensions) > 32
            or any(node[0] != 'ref' for node in dimensions)
            or value[0] != 'ref'):
        return None
    names = [node[3] for node in dimensions]
    if (len({name.casefold() for name in names}) != len(names)
            or scope.get('fixed_dimensions') != names
            or scope.get('value_column') != value[3]):
        return None
    physical = scope.get('physical_fields')
    if (not isinstance(physical, list) or any(not isinstance(n, str) or not n
                                              for n in physical)):
        return None
    physical, available = set(physical), set(table_columns)
    measures = {name.casefold() for name in measure_names if isinstance(name, str)}
    for node in [*dimensions, value]:
        owner, name = node[2], node[3]
        if (name not in physical or name not in available or name.casefold() in measures
                or (owner is not None and owner != table_name)
                or (owner is None and name in column_table_map
                    and column_table_map[name] != table_name)):
            return None

    view_grains = scope.get('view_grains')
    if (not isinstance(view_grains, list) or not view_grains or [] not in view_grains
            or any(not isinstance(grain, list)
                   or any(not isinstance(name, str) for name in grain)
                   or len(set(grain)) != len(grain)
                   or not set(grain).issubset(names)
                   for grain in view_grains)):
        return None
    table = "'" + table_name.replace("'", "''") + "'"

    def column(name):
        return table + '[' + name.replace(']', ']]') + ']'

    keys = ', '.join(column(name) for name in names)
    return f"SUMX(SUMMARIZE({table}, {keys}), CALCULATE(MIN({column(value[3])})))"


def _compile_approximate_fixed_aggregate_ratio(
        masked, restore, *, table_name, column_table_map, measure_names,
    table_columns, numeric_columns, is_calc_column, shelf=None,
    row_approx_measure_names=()):
    """Best-effort lowering for FIXED ratios of local aggregate columns."""
    if is_calc_column or not isinstance(table_name, str) or not table_name:
        return None
    match = re.fullmatch(
        r'\s*(?:(?P<outer>SUM|AVG|MIN|MAX)\s*\(\s*)?'
        r'\{\s*FIXED\s+(?P<dims>[^:{}()]+)\s*:\s*'
        r'(?P<body>[^{}]+)\}\s*(?(outer)\))\s*',
        masked, re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    outer = (match.group('outer') or '').upper()
    shelf = shelf.strip().upper() if isinstance(shelf, str) else None
    if outer and shelf and outer != shelf:
        return None
    try:
        dimensions = [
            _ungroup_source(_SourceExpressionParser(restore(part)).parse())
            for part in _split_args(match.group('dims'))
        ]
        body = _ungroup_source(
            _SourceExpressionParser(restore(match.group('body')).strip()).parse())
    except (_SourceExpressionError, RecursionError):
        return None
    if (not dimensions or any(node[0] != 'ref' for node in dimensions)
            or body[0] != 'op' or body[1] != '/'):
        return None
    dimensions = cast(list[tuple[str, str, str | None, str]], dimensions)
    body = cast(tuple[str, str, object, object], body)

    aggregates = {'SUM': 'SUM', 'AVG': 'AVERAGE', 'AVERAGE': 'AVERAGE',
                  'MIN': 'MIN', 'MAX': 'MAX'}

    def aggregate_reference(node: object):
        parsed = _ungroup_source(node)
        if not isinstance(parsed, tuple) or len(parsed) != 3 or parsed[0] != 'call':
            return None
        call = cast(tuple[str, str, tuple[object, ...]], parsed)
        if len(call[2]) != 1:
            return None
        function = aggregates.get(call[1].upper())
        argument = _ungroup_source(call[2][0])
        if function is None:
            return None
        if isinstance(argument, tuple) and len(argument) == 4 and argument[0] == 'ref':
            return function, argument
        row_expression = _compile_numeric_row_aggregate(
            call[1].upper(), argument, table_name=table_name,
            table_columns=table_columns, numeric_columns=numeric_columns,
            column_table_map=column_table_map, measure_names=measure_names,
            source_text=restore(match.group('body')).strip())
        if row_expression is None:
            return None
        return function, ('row_expression', row_expression)

    numerator = aggregate_reference(body[2])
    denominator_node = _ungroup_source(body[3])
    if (isinstance(denominator_node, tuple) and len(denominator_node) == 3
            and denominator_node[0] == 'call'
            and denominator_node[1].upper() == 'NULLIF'):
        nullif = cast(tuple[str, str, tuple[object, ...]], denominator_node)
        if (len(nullif[2]) != 2
                or _ungroup_source(nullif[2][1]) != ('atom', '0')):
            return None
        denominator_node = _ungroup_source(nullif[2][0])
    denominator = aggregate_reference(denominator_node)
    if numerator is None or denominator is None:
        return None

    available = (set(table_columns) if isinstance(
        table_columns, (list, tuple, set, frozenset)) else None)
    measures = {name.casefold() for name in (measure_names or ())
                if isinstance(name, str)}
    row_approx_measures = {name.casefold() for name in row_approx_measure_names
                           if isinstance(name, str)}
    available_folded = {name.casefold() for name in available or ()}

    foreign_operands = {}
    missing_operands = []
    calculation_operands = []

    def aggregate_operand(aggregate, reference):
        if reference[0] == 'row_expression':
            return ('row_expression', aggregate, reference[1])
        owner, name = reference[2], reference[3]
        folded = name.casefold()
        mapped_owner = (column_table_map.get(name)
                        if isinstance(column_table_map, dict) else None)
        local = owner in (None, table_name) and mapped_owner in (None, table_name)
        if local and folded in measures and folded not in available_folded:
            if folded not in row_approx_measures:
                calculation_operands.append(name)
            return ('measure', aggregate, name)
        foreign = next((table for table in (owner, mapped_owner)
                        if table not in (None, table_name)), None)
        if foreign is not None:
            if folded in measures:
                return None
            foreign_operands[name] = foreign
            return ('column', aggregate, name)
        if available is not None and name not in available:
            if mapped_owner == table_name:
                calculation_operands.append(name)
                return ('measure', aggregate, name)
            missing_operands.append(name)
            return ('column', aggregate, name)
        return ('column', aggregate, name)

    numerator_binding = aggregate_operand(*numerator)
    denominator_binding = aggregate_operand(*denominator)
    if numerator_binding is None or denominator_binding is None:
        return None

    known_tables = (set(column_table_map.values())
                    if isinstance(column_table_map, dict) else set())
    dimension_keys = []
    cross_table_dims = []
    missing_dims = []
    for node in dimensions:
        owner, name = node[2], node[3]
        if (name.casefold() in measures
                and name.casefold() not in available_folded):
            return None
        if owner is not None and owner != table_name and owner not in known_tables:
            return None
        if owner is not None:
            dimension_table = owner
        elif isinstance(column_table_map, dict) and name in column_table_map:
            dimension_table = column_table_map[name]
        else:
            dimension_table = (table_name if available is None or name in available
                               else None)
        if (dimension_table is None
                or (dimension_table == table_name and available is not None
                    and name not in available)):
            missing_dims.append(name)
            continue
        dimension_keys.append((dimension_table, name))
        if dimension_table != table_name:
            cross_table_dims.append(name)

    dimension_names = [name for _, name in dimension_keys]
    if len({name.casefold() for name in dimension_names}) != len(dimension_names):
        return None
    if cross_table_dims and not outer and not shelf:
        return None

    safe_table = "'" + table_name.replace("'", "''") + "'"

    def column(name, owner=table_name):
        table_ref = "'" + owner.replace("'", "''") + "'"
        return table_ref + '[' + name.replace(']', ']]') + ']'

    def operand(binding):
        kind, aggregate, name = binding
        if kind == 'measure':
            measure_ref = '[' + name.replace(']', ']]') + ']'
            if aggregate == 'SUM':
                return measure_ref
            iterator = _NUMERIC_ROW_ITERATORS.get(aggregate)
            if iterator is None:
                return measure_ref
            return f'{iterator}({safe_table}, {measure_ref})'
        if kind == 'row_expression':
            return name
        return aggregate + '(' + column(name, foreign_operands.get(name, table_name)) + ')'

    numerator_value = operand(numerator_binding)
    denominator_value = operand(denominator_binding)
    ratio = f'DIVIDE({numerator_value}, {denominator_value})'
    keys = ', '.join(column(name, owner) for owner, name in dimension_keys)
    warnings = []
    if cross_table_dims:
        warnings.append(
            f"cross-table dimension(s) {', '.join(cross_table_dims)}; "
            "relationship propagation unverified")
    if missing_dims:
        warnings.append(f"missing schema dimension(s) {', '.join(missing_dims)} omitted")
    measure_operands = [
        (aggregate, name)
        for kind, aggregate, name in (numerator_binding, denominator_binding)
        if kind == 'measure'
    ]
    row_expression_operands = [
        aggregate for kind, aggregate, _ in (numerator_binding, denominator_binding)
        if kind == 'row_expression'
    ]
    for aggregate, name in measure_operands:
        kind = 'calculation' if name in calculation_operands else 'row-expression measure'
        if aggregate == 'SUM':
            warnings.append(f"SUM over {kind} {name} unwrapped")
        else:
            warnings.append(
                f"{aggregate} over {kind} {name} iterated with "
                f"{_NUMERIC_ROW_ITERATORS.get(aggregate, 'iterator')}"
            )
    if foreign_operands:
        warnings.append(
            f"cross-table operand(s) {', '.join(foreign_operands)}; "
            "relationship propagation unverified")
    if missing_operands:
        warnings.append(
            f"missing schema operand(s) {', '.join(missing_operands)} referenced as columns")
    if row_expression_operands:
        iterators = sorted({
            _NUMERIC_ROW_ITERATORS[aggregate]
            for aggregate in row_expression_operands
        })
        warnings.append(
            f"numeric row-expression operand(s) lowered via {', '.join(iterators)}")
    warning_suffix = ('; ' + '; '.join(warnings)) if warnings else ''
    note = (
        'FIXED best-effort approximation; source filters, aggregation scope '
        f'and total grain unverified{warning_suffix}'
    )
    iterator_aggregations = {
        'SUM': 'SUMX', 'AVG': 'AVERAGEX', 'MIN': 'MINX', 'MAX': 'MAXX',
    }
    shelf_aggregation = outer or (shelf if cross_table_dims else None)
    if shelf_aggregation and shelf_aggregation not in iterator_aggregations:
        return None
    if shelf_aggregation and dimension_keys:
        if cross_table_dims:
            domain = f'SUMMARIZECOLUMNS({keys})'
        else:
            domain = (f'VALUES({keys})' if len(dimension_keys) == 1
                      else f'SUMMARIZE({safe_table}, {keys})')
        result = (
            f'/* {note} */ {iterator_aggregations[shelf_aggregation]}'
            f'({domain}, CALCULATE({ratio}))'
        )
    elif dimension_keys:
        result = (f'/* {note} */ CALCULATE({ratio}, '
                 f'ALLEXCEPT({safe_table}, {keys}))')
    else:
        result = f'/* {note} */ {ratio}'
    try:
        from powerbi_import.dax_validator import validate_dax_expression
        if validate_dax_expression(result):
            return None
    except (ImportError, OSError, ValueError):
        return None
    return result


def _compile_approximate_fixed_shelf(masked, restore, *, table_name,
                                     column_table_map, measure_names,
                                     table_columns, numeric_columns,
                                     is_calc_column, shelf,
                                     calc_map=None, shelf_evidence='explicit',
                                     row_approx_measure_names=()):
    """Approximate a resolved shelf over known FIXED keys and numeric values."""
    from typing import cast

    iterators = {'SUM': 'SUMX', 'AVG': 'AVERAGEX', 'MIN': 'MINX', 'MAX': 'MAXX'}
    if (not isinstance(shelf, str)
            or shelf_evidence not in ('explicit', 'inferred', 'conflicting')):
        return None
    shelf = shelf.strip().upper()
    if (shelf not in iterators or is_calc_column
            or not isinstance(table_name, str) or not table_name
            or not isinstance(table_columns, (list, tuple, set, frozenset))
            or any(not isinstance(name, str) or not name for name in table_columns)):
        return None
    match = re.fullmatch(
        r'\s*(?:(?P<outer>SUM|AVG|MIN|MAX)\s*\(\s*)?'
        r'\{\s*FIXED\s+(?P<dims>[^:{}()]+)\s*:\s*'
        r'(?P<value>[^{}:]+)\}\s*(?(outer)\))\s*',
        masked, re.IGNORECASE | re.DOTALL,
    )
    if not match or (match.group('outer') and match.group('outer').upper() != shelf):
        return None
    aggregates = {
        'SUM': 'SUM', 'AVG': 'AVERAGE', 'AVERAGE': 'AVERAGE',
        'MIN': 'MIN', 'MAX': 'MAX', 'COUNT': 'COUNT', 'COUNTD': 'DISTINCTCOUNT',
        'COUNTA': 'COUNTA', 'MEDIAN': 'MEDIAN', 'STDEV': 'STDEV.S',
        'STDEVP': 'STDEV.P', 'VAR': 'VAR.S', 'VARP': 'VAR.P',
    }

    def parse_reference_expression(part):
        text = restore(part)
        text = re.sub(
            r'(?P<owner>\[(?:[^\]]|\]\])+\])\s*\.\s*'
            r'(?P<field>\[(?:[^\]]|\]\])+\])|'
            + _RE_NULL_PROTECTED_TOKEN.pattern,
            lambda token: (
                "'" + token.group('owner')[1:-1].replace(']]', ']').replace("'", "''")
                + "'" + token.group('field')
                if token.group('owner') else token.group(0)),
            text,
        )
        text = _resolve_references(text, calc_map or {}, {}, False, {})
        text = _normalize_source_control_syntax(text)
        return _ungroup_source(_SourceExpressionParser(text).parse())

    try:
        dimensions = [
            parse_reference_expression(part)
            for part in match.group('dims').split(',')
        ]
        aggregate = parse_reference_expression(match.group('value'))
    except (_SourceExpressionError, RecursionError):
        return None
    if (not dimensions or len(dimensions) > 32
            or any(node[0] != 'ref' for node in dimensions)
            or aggregate[0] != 'call'):
        return None
    aggregate = cast(tuple[str, str, tuple[tuple[object, ...], ...]], aggregate)
    if (aggregate[1].upper() not in aggregates
            or len(aggregate[2]) != 1):
        return None
    value = cast(tuple[Any, ...], _ungroup_source(aggregate[2][0]))
    dimensions = cast(list[tuple[str, str, str | None, str]], dimensions)
    value_is_ref = value[0] == 'ref'
    val_owner: str | None = None
    val_name = ''
    if value_is_ref:
        value_ref = cast(tuple[str, str, str | None, str], value)
        val_owner, val_name = value_ref[2], value_ref[3]
    names = [node[3] for node in dimensions]
    if len({name.casefold() for name in names}) != len(names):
        return None
    available = set(table_columns)
    available_folded = {name.casefold() for name in available}
    measures = {name.casefold() for name in measure_names if isinstance(name, str)}
    row_approx_measures = {name.casefold() for name in row_approx_measure_names
                           if isinstance(name, str)}
    val_is_measure = False
    val_is_row_measure = False
    val_is_row_expression = not value_is_ref
    row_expression_dax = None
    if value_is_ref:
        val_is_measure = (val_name.casefold() in measures
                          and val_name.casefold() not in available_folded
                          and (val_name not in column_table_map
                               or (val_name.casefold() in row_approx_measures
                                   and column_table_map.get(val_name) == table_name)))
        val_is_row_measure = (
            val_is_measure and val_name.casefold() in row_approx_measures)
        if val_is_measure:
            if val_owner is not None and val_owner != table_name:
                return None
        else:
            val_table = (
                val_owner if val_owner is not None
                else column_table_map.get(val_name, table_name)
            )
            if ((val_name not in available and val_name not in column_table_map)
                    or val_name.casefold() in measures):
                return None
            if val_table != table_name:
                return None
    else:
        value_text = restore(match.group('value')).strip()
        aggregate_argument = re.fullmatch(
            r'\s*(?:SUM|AVG|AVERAGE|MIN|MAX)\s*\((.*)\)\s*',
            value_text, re.IGNORECASE | re.DOTALL,
        )
        if aggregate_argument is None:
            return None
        row_expression_dax = _compile_numeric_row_aggregate(
            aggregate[1], value, table_name=table_name,
            table_columns=table_columns, numeric_columns=numeric_columns,
            column_table_map=column_table_map, measure_names=measure_names,
            source_text=aggregate_argument.group(1).strip())
        if row_expression_dax is None:
            return None

    table = "'" + table_name.replace("'", "''") + "'"

    def column(name, col_table=table_name):
        t_ref = "'" + col_table.replace("'", "''") + "'"
        return t_ref + '[' + name.replace(']', ']]') + ']'

    valid_keys = []
    cross_table_dims = []
    missing_dims = []
    known_tables = set(column_table_map.values()) if isinstance(column_table_map, dict) else set()
    for node in dimensions:
        owner, name = node[2], node[3]
        if name.casefold() in measures:
            return None
        if owner is not None and owner != table_name and owner not in known_tables:
            return None
        dim_table = (
            owner if owner is not None
            else column_table_map.get(name, table_name if name in available else None)
        )
        if dim_table is None or (dim_table == table_name and name not in available):
            missing_dims.append(name)
            continue
        if dim_table != table_name:
            cross_table_dims.append(name)
        valid_keys.append(column(name, dim_table))

    if val_is_measure:
        inner = '[' + val_name.replace(']', ']]') + ']'
    elif not val_is_row_expression:
        inner = aggregates[aggregate[1].upper()] + '(' + column(val_name) + ')'
    else:
        inner = row_expression_dax
    extra_warnings = []
    if cross_table_dims:
        extra_warnings.append(
            f"cross-table dimension(s) {', '.join(cross_table_dims)}; "
            "relationship propagation unverified")
    if missing_dims:
        extra_warnings.append(
            f"missing schema dimension(s) {', '.join(missing_dims)} omitted")
    if val_is_measure:
        measure_kind = 'row-expression' if val_is_row_measure else 'ordinary'
        extra_warnings.append(
            f"{aggregate[1].upper()} over {measure_kind} measure {val_name} "
            "aggregated with shelf semantics unverified")
    if val_is_row_expression:
        extra_warnings.append(
            f"{aggregate[1].upper()} numeric row expression lowered to "
            f"{_NUMERIC_ROW_ITERATORS[aggregate[1].upper()]}")
    extra_suffix = ('; ' + '; '.join(extra_warnings)) if extra_warnings else ''
    if shelf_evidence == 'inferred':
        note = (f'shelf ({shelf.lower()}: inferred) approximation; aggregation '
                f'inferred, source filter scope and totals unverified{extra_suffix}')
    elif shelf_evidence == 'conflicting':
        note = (f'shelf ({shelf.lower()}: chosen from conflicting evidence) '
                'approximation; aggregation conflict, source filter scope and '
                f'totals unverified{extra_suffix}')
    else:
        note = (f'shelf ({shelf.lower()}:) approximation; source filter scope '
                f'and totals unverified{extra_suffix}')
    if not valid_keys:
        expression = f'/* {note} */ CALCULATE({inner})'
    else:
        keys = ', '.join(valid_keys)
        if cross_table_dims:
            domain = f'SUMMARIZECOLUMNS({keys})'
        else:
            domain = (f'VALUES({keys})' if len(valid_keys) == 1
                      else f'SUMMARIZE({table}, {keys})')
        expression = (
            f'/* {note} */ {iterators[shelf]}({domain}, CALCULATE({inner}))')
    try:
        from powerbi_import.dax_validator import validate_dax_expression
        if validate_dax_expression(expression):
            return None
    except (ImportError, OSError, ValueError):
        return None
    return expression


def _compile_context_lod(masked, restore, *, table_name, column_table_map,
                         measure_names, table_columns, numeric_columns,
                         is_calc_column, shelf):
    """Compile bounded INCLUDE/EXCLUDE values with explicit grain warnings."""
    if (is_calc_column or not isinstance(table_name, str) or not table_name
            or not isinstance(table_columns, (list, tuple, set, frozenset))
            or not isinstance(numeric_columns, (list, tuple, set, frozenset))):
        return None, 'INCLUDE/EXCLUDE requires physical table and numeric-column evidence'

    match = re.fullmatch(
        r'\s*(?:(?P<outer>SUM|AVG|AVERAGE|MIN|MAX)\s*\(\s*)?'
        r'\{\s*(?P<kind>INCLUDE|EXCLUDE)\b'
        r'(?P<dims>[^:{}()]*?)\s*:\s*(?P<body>[^{}]+)\}'
        r'\s*(?(outer)\))\s*',
        masked, re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None, 'nested or composed INCLUDE/EXCLUDE requires verified target grain'

    kind = match.group('kind').upper()
    outer = match.group('outer')
    dims_text = match.group('dims').strip()
    if kind == 'INCLUDE' and not dims_text:
        return None, 'INCLUDE requires one or more physical dimension keys'
    if kind == 'EXCLUDE' and outer:
        return None, 'aggregating an EXCLUDE result requires verified visual and total grain'

    try:
        dimensions = [
            _ungroup_source(_SourceExpressionParser(restore(part)).parse())
            for part in dims_text.split(',') if part.strip()
        ]
        body_text = _normalize_source_control_syntax(
            restore(match.group('body')).strip()).strip()
        body = _ungroup_source(_SourceExpressionParser(body_text).parse())
    except (_SourceExpressionError, RecursionError):
        return None, f'{kind} requires a supported aggregate and physical dimension keys'

    if len(dimensions) > 32 or any(node[0] != 'ref' for node in dimensions):
        return None, f'{kind} requires a supported single-column or row-expression aggregate'
    if not isinstance(body, tuple) or len(body) != 3 or body[0] != 'call':
        return None, f'{kind} requires a supported single-column or row-expression aggregate'
    dimensions = cast(list[tuple[str, str, str | None, str]], dimensions)
    body = cast(tuple[str, str, tuple[tuple[Any, ...], ...]], body)
    if len(body[2]) != 1:
        return None, f'{kind} requires a supported single-column or row-expression aggregate'

    aggregate_names = {
        'SUM': 'SUM', 'AVG': 'AVERAGE', 'AVERAGE': 'AVERAGE',
        'MIN': 'MIN', 'MAX': 'MAX',
    }
    inner_name = aggregate_names.get(body[1].upper())
    if inner_name is None:
        return None, f'{kind} inner aggregate is unsupported'

    available = set(table_columns)
    numeric = set(numeric_columns)
    measures = {name.casefold() for name in measure_names if isinstance(name, str)}

    def local_physical_column(node, *, require_numeric=False):
        node = _ungroup_source(node)
        if node[0] != 'ref':
            return None
        _, _, owner, name = node
        if (name not in available or name.casefold() in measures
                or (owner is not None and owner != table_name)
                or column_table_map.get(name, table_name) != table_name
                or (require_numeric and name not in numeric)):
            return None
        return name

    source_dimension_names = [node[3] for node in dimensions]
    if len({name.casefold() for name in source_dimension_names}) != len(source_dimension_names):
        return None, f'{kind} dimension keys must be distinct physical columns'

    dimension_refs = []
    cross_table_dims = []
    missing_dims = []
    known_tables = set(column_table_map.values())
    for node in dimensions:
        owner, name = node[2], node[3]
        if name.casefold() in measures and name not in available:
            return None, f'{kind} dimension keys cannot be measures'
        mapped_owner = column_table_map.get(name)
        dimension_table = owner or mapped_owner
        if dimension_table is None and name in available:
            dimension_table = table_name
        if (dimension_table is None
                or (dimension_table == table_name and name not in available)):
            missing_dims.append(name)
            continue
        if dimension_table != table_name:
            if dimension_table not in known_tables:
                missing_dims.append(name)
                continue
            cross_table_dims.append(name)
        dimension_refs.append((dimension_table, name))

    value = _ungroup_source(body[2][0])
    value_refs = [node for node in _source_walk(value) if node[0] == 'ref']
    value_names = [local_physical_column(node, require_numeric=True)
                   for node in value_refs]
    if not value_refs or any(name is None for name in value_names):
        return None, f'{kind} aggregate needs numeric physical columns in the same table'

    def numeric_scalar(node):
        return _is_numeric_scalar_source(
            node, lambda reference: local_physical_column(
                reference, require_numeric=True) is not None)

    if not numeric_scalar(value):
        return None, f'{kind} row aggregate only supports numeric scalar expressions'

    safe_table = "'" + table_name.replace("'", "''") + "'"

    def column(name, owner=table_name):
        table_ref = "'" + owner.replace("'", "''") + "'"
        return table_ref + '[' + name.replace(']', ']]') + ']'

    if value[0] == 'ref':
        inner = f'{inner_name}({column(value_names[0])})'
    else:
        value_text = re.fullmatch(
            r'\s*(?:SUM|AVG|AVERAGE|MIN|MAX)\s*\((.*)\)\s*',
            body_text, re.IGNORECASE | re.DOTALL)
        if not value_text:
            return None, f'{kind} aggregate body is not a supported row expression'
        inner = _compile_numeric_row_aggregate(
            inner_name, value, table_name=table_name,
            table_columns=table_columns, numeric_columns=numeric_columns,
            column_table_map=column_table_map, measure_names=measure_names,
            source_text=value_text.group(1).strip())
        if inner is None:
            return None, f'{kind} row expression requires proven numeric local columns'

    dimension_columns = [column(name, owner) for owner, name in dimension_refs]
    warnings = []
    if cross_table_dims:
        warnings.append(
            f"cross-table dimension(s) {', '.join(cross_table_dims)} retained; "
            "relationship propagation unverified")
    if missing_dims:
        warnings.append(f"missing schema dimension(s) {', '.join(missing_dims)} omitted")
    warning_suffix = ('; ' + '; '.join(warnings)) if warnings else ''
    if kind == 'EXCLUDE':
        note = 'EXCLUDE dimension-filter approximation; totals unverified'
        if dimension_columns:
            expression = (
                f'/* {note}{warning_suffix} */ '
                f'CALCULATE({inner}, REMOVEFILTERS({", ".join(dimension_columns)}))')
        else:
            expression = f'/* {note}{warning_suffix} */ CALCULATE({inner})'
        return expression, ''

    shelf_name = shelf.strip().upper() if isinstance(shelf, str) else ''
    if outer:
        if shelf_name and shelf_name != outer.upper():
            return None, 'INCLUDE outer aggregate conflicts with its explicit shelf'
        shelf_name = outer.upper()
    shelf_iterators = {
        'SUM': 'SUMX', 'AVG': 'AVERAGEX', 'MIN': 'MINX', 'MAX': 'MAXX',
    }
    if shelf_name not in shelf_iterators:
        return None, 'INCLUDE requires explicit consistent SUM/AVG/MIN/MAX shelf evidence'
    note = (f'shelf ({shelf_name.lower()}:) INCLUDE approximation; source filters and '
            f'totals unverified{warning_suffix}')
    if not dimension_columns:
        return f'/* {note} */ {inner}', ''
    if cross_table_dims:
        domain = f'SUMMARIZECOLUMNS({", ".join(dimension_columns)})'
    else:
        domain = (f'VALUES({dimension_columns[0]})'
                  if len(dimension_columns) == 1
                  else f'SUMMARIZE({safe_table}, {", ".join(dimension_columns)})')
    expression = (
        f'/* {note} */ {shelf_iterators[shelf_name]}({domain}, '
        f'CALCULATE({inner}))')
    return expression, ''


def compile_verified_fixed_dimension(formula, *, table_name, table_columns,
                                     verified_scope, column_table_map=None,
                                     measure_names=None, calc_map=None):
    """Compile a narrowly verified FIXED ratio for *internal refresh use only*.

    The returned expression is intended to be inlined into a calculated
    dimension; it is not evidence that the FIXED is a valid public measure
    on another shelf or at totals. The semantic owner must independently
    establish that the consuming dimension and its nullable logic are safe.

    Supported source shape: ``{FIXED keys : SUM(num) / SUM(den)}``, optionally
    with ``NULLIF(SUM(den), 0)``. All fields must be physical columns of one
    Import table. ``verified_scope`` must explicitly provide:

    * ``table``, ``target_dimension``, ``fixed_dimensions``,
      ``numerator_column``, ``denominator_column``, ``physical_fields``,
      ``numeric_columns``, ``storage_mode='import'`` and
      ``execution_scope='refresh_calculated_column_only'``;
    * ``source_filters`` and ``target_refresh_filters``, lists of nonempty
      ``{'id': ..., 'predicate': ...}`` records, or explicit empty lists;
      ``filter_policy='source_fixed_scope_equals_target_refresh_per_key'``;
    * literal-True assertions ``source_row_bag_verified``,
      ``physical_fields_verified``, ``fixed_key_and_nulls_verified``,
      ``numeric_and_null_equivalence_verified``, ``filter_equivalence_verified``,
      ``all_security_and_contexts_verified`` and ``engine_support_verified``.

    These are caller assertions, not automatically discovered evidence. In
    particular, report-time context filters and RLS must not change the
    source FIXED value relative to its refresh-time materialization.
    """
    def unsupported():
        return {
            'status': 'unsupported',
            'reason': ('FIXED dimension requires proven physical refresh grain, '
                       'source filter and security scope, and nullable behavior'),
        }

    scope = verified_scope
    if (not isinstance(formula, str) or not isinstance(table_name, str)
            or not table_name or not isinstance(scope, dict)
            or not isinstance(table_columns, (list, tuple, set, frozenset))
            or any(not isinstance(name, str) or not name for name in table_columns)
            or scope.get('table') != table_name
            or scope.get('storage_mode') != 'import'
            or scope.get('execution_scope') != 'refresh_calculated_column_only'
            or scope.get('filter_policy') != 'source_fixed_scope_equals_target_refresh_per_key'
            or not isinstance(scope.get('target_dimension'), str)
            or not scope['target_dimension'].strip()):
        return unsupported()
    if not all(scope.get(key) is True for key in (
            'source_row_bag_verified', 'physical_fields_verified',
            'fixed_key_and_nulls_verified', 'numeric_and_null_equivalence_verified',
            'filter_equivalence_verified', 'all_security_and_contexts_verified',
            'engine_support_verified')):
        return unsupported()
    for key in ('source_filters', 'target_refresh_filters'):
        filters = scope.get(key)
        if not isinstance(filters, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get('id'), str) or not item['id'].strip()
                or not isinstance(item.get('predicate'), str) or not item['predicate'].strip()
                for item in filters):
            return unsupported()
    for key in ('physical_fields', 'numeric_columns'):
        fields = scope.get(key)
        if (not isinstance(fields, list)
                or any(not isinstance(name, str) or not name for name in fields)):
            return unsupported()

    text = _resolve_references(
        _strip_tableau_comments(formula).strip(), calc_map or {}, {},
        False, {})
    masked, restore = _protect_source_tokens(text)
    match = re.fullmatch(
        r'\s*\{\s*FIXED\s+(?P<dims>[^:{}()]+)\s*:\s*'
        r'(?P<body>[^{}]+)\}\s*',
        masked, re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return unsupported()
    try:
        dimensions = [
            _ungroup_source(_SourceExpressionParser(restore(part)).parse())
            for part in match.group('dims').split(',')
        ]
        body = _ungroup_source(
            _SourceExpressionParser(restore(match.group('body'))).parse())
    except (_SourceExpressionError, RecursionError):
        return unsupported()
    if (not dimensions or len(dimensions) > 32
            or any(node[0] != 'ref' for node in dimensions)
            or body[0] != 'op' or body[1] != '/'):
        return unsupported()

    def summed_ref(node):
        node = _ungroup_source(node)
        if node[0] != 'call' or node[1].upper() != 'SUM' or len(node[2]) != 1:
            return None
        ref = _ungroup_source(node[2][0])
        return ref if ref[0] == 'ref' else None

    numerator = summed_ref(body[2])
    denominator_node = _ungroup_source(body[3])
    strict_nullif = False
    if (denominator_node[0] == 'call'
            and denominator_node[1].upper() == 'NULLIF'):
        if len(denominator_node[2]) != 2:
            return unsupported()
        zero = _ungroup_source(denominator_node[2][1])
        if zero[0] != 'atom' or not re.fullmatch(r'0+(?:\.0+)?', zero[1]):
            return unsupported()
        strict_nullif = True
        denominator_node = denominator_node[2][0]
    denominator = summed_ref(denominator_node)
    if numerator is None or denominator is None:
        return unsupported()
    names = [node[3] for node in dimensions]
    if (len({name.casefold() for name in names}) != len(names)
            or scope.get('fixed_dimensions') != names
            or scope.get('numerator_column') != numerator[3]
            or scope.get('denominator_column') != denominator[3]
            or numerator[3] not in scope['numeric_columns']
            or denominator[3] not in scope['numeric_columns']):
        return unsupported()
    physical, available = set(scope['physical_fields']), set(table_columns)
    owners = column_table_map or {}
    measures = {name.casefold() for name in (measure_names or ())
                if isinstance(name, str)}
    for node in [*dimensions, numerator, denominator]:
        owner, name = node[2], node[3]
        if (name not in physical or name not in available or name.casefold() in measures
                or (owner is not None and owner != table_name)
                or (owner is None and name in owners and owners[name] != table_name)):
            return unsupported()

    table = "'" + table_name.replace("'", "''") + "'"

    def column(name):
        return table + '[' + name.replace(']', ']]') + ']'

    num = f"SUM({column(numerator[3])})"
    den = f"SUM({column(denominator[3])})"
    if strict_nullif:
        den = f"IF(({den}) == (0), BLANK(), ({den}))"
    keys = ', '.join(column(name) for name in names)
    expression = f"CALCULATE(DIVIDE({num}, {den}), ALLEXCEPT({table}, {keys}))"
    try:
        from powerbi_import.dax_validator import validate_dax_expression
        if validate_dax_expression(expression):
            return unsupported()
    except (ImportError, OSError, ValueError):
        return unsupported()
    return {
        'status': 'supported',
        'expression': expression,
        'target_dimension': scope['target_dimension'],
        'fixed_dimensions': names,
        'execution_scope': 'refresh_calculated_column_only',
    }


def _check_source_grain(root, table_name, column_table_map, measure_names,
                        is_calc_column, table_columns, verified_row_scope):
    """Check row and shelf grain before any MAX/SUM/iterator guesses.

    The optional scope is an explicit semantic assertion, not an inference:
    ``table``, ``source_row_bag_verified``, ``filter_context_verified``,
    ``comparison_policy_verified``, ``numeric_result_verified``,
    ``shelf_aggregation='AGG'`` and ``total_grain='source_rows'``. Only a
    root SUM of a single-table scalar expression uses it. The table must
    already carry the source row bag and the current/total filter contexts.
    """
    local_columns = set(table_columns or ())

    def binding(ref):
        _, _, owner, name = ref
        if name in measure_names and not (owner == table_name and name in local_columns):
            return 'measure', None
        if owner:
            return 'column', owner
        if name in local_columns:
            return 'column', table_name
        return 'column', column_table_map.get(name, table_name)

    bare = []
    force_nullable_arithmetic = False

    def visit(node):
        nonlocal force_nullable_arithmetic
        if node[0] == 'ref':
            if binding(node)[0] == 'column':
                bare.append(node)
            return None
        if node[0] == 'call' and node[1].upper() in _SOURCE_AGGREGATES:
            args = node[2]
            aggregate_name = node[1].upper()
            if aggregate_name in _PAIRED_SOURCE_AGGREGATES:
                if len(args) != 2:
                    return 'correlation/covariance requires exactly two source expressions'
                if table_columns is not None:
                    return ('paired correlation/covariance requires verified source row '
                            'pairs, filter context, shelf aggregation and total grain')
                return None
            if len(args) != 1:
                return ('aggregate argument or grain is not established'
                        if table_columns is not None else None)
            argument = _ungroup_source(args[0])
            if argument[0] == 'ref':
                if (table_columns is not None and node[1].upper() != 'ATTR'
                        and binding(argument)[0] == 'measure'):
                    if is_calc_column:
                        return ('aggregation of a calculation requires its source '
                                'shelf and total grain')
                return None
            refs = [part for part in _source_walk(argument) if part[0] == 'ref']
            explicit_owners = {
                binding(ref)[1] for ref in refs
                if binding(ref)[0] == 'column'
                and (ref[2] is not None or ref[3] in column_table_map
                     or ref[3] in local_columns)
            }
            if len(explicit_owners) > 1:
                return 'row aggregation spans tables without a proven source row bag'
            if (table_columns is not None
                    and any(binding(ref)[0] == 'measure' for ref in refs)):
                return ('aggregation of a calculation requires its source '
                        'shelf and total grain')
            # The raw translation API has historically accepted expressions
            # without a model/row-context contract. Do not invent a diagnostic
            # from that absence; generation supplies table_columns explicitly.
            if table_columns is None:
                return None
            if any(part[0] == 'call' and part[1].upper() in _SOURCE_AGGREGATES
                   for part in _source_walk(argument)):
                return 'nested aggregates require an explicit source grain'
            owners = {binding(ref)[1] for ref in refs}
            if len(owners) > 1:
                return 'row aggregation spans tables without a proven source row bag'
            scope = verified_row_scope if isinstance(verified_row_scope, dict) else {}
            verified = (
                node is _ungroup_source(root)
                and node[1].upper() == 'SUM'
                and len(owners) == 1
                and scope.get('table') == next(iter(owners))
                and all(scope.get(key) is True for key in (
                    'source_row_bag_verified', 'filter_context_verified',
                    'comparison_policy_verified', 'numeric_result_verified'))
                and scope.get('shelf_aggregation') == 'AGG'
                and scope.get('total_grain') == 'source_rows'
                and all(ref[2] is not None or ref[3] in column_table_map
                        or ref[3] in local_columns for ref in refs)
            )
            supported_calls = {'IF', 'IIF', 'ISNULL', 'IFNULL', 'ZN',
                               'NULLIF', 'BLANK', 'TRUE', 'FALSE'}
            if not verified or any(
                    part[0] == 'call' and part[1].upper() not in supported_calls
                    for part in _source_walk(argument)):
                return ('row aggregation requires a verified source row bag, '
                        'shelf aggregation and total grain')
            force_nullable_arithmetic = True
            return None
        for child in _source_children(node):
            issue = visit(child)
            if issue:
                return issue
        return None

    issue = visit(root)
    if issue:
        return issue, False
    if bare:
        if not is_calc_column and table_columns is not None:
            return ('unaggregated row expression requires source row scope, '
                    'shelf aggregation and total grain'), False
        # Calculated-column cross-table references must remain RELATED so the
        # semantic model's relationship validation can inspect them. In
        # particular, replacing a remote combined-group reference with a
        # BLANK here would bypass its required active-to-one path check.
        if is_calc_column and table_columns is not None and any(
               part[0] == 'ref' and binding(part)[0] == 'measure'
               for part in _source_walk(root)):
            return 'row expression mixes a measure with physical columns', False
    return None, force_nullable_arithmetic


def _render_nullable_source(root, text, calc_datatype, force_arithmetic,
                            force_render=False, null_numeric_functions=False):
    """Lower Tableau nullable booleans without DAX BLANK coercion."""
    nodes = list(_source_walk(root))
    needs_logic = any(
        (node[0] == 'op' and node[1] in _SOURCE_COMPARISONS | _SOURCE_BOOLEAN)
        or (node[0] == 'unary' and node[1] == 'NOT')
        or (node[0] == 'call' and node[1].upper() in ('IIF', 'AND', 'OR', 'NOT'))
        or (null_numeric_functions and node[0] == 'call'
            and node[1].upper() in _NUMERIC_SCALAR_SOURCE_ARITIES)
        for node in nodes
    )
    if not needs_logic and not force_arithmetic and not force_render:
        return text

    # The converter has no operand types with which to distinguish a numeric
    # plus from Tableau string concatenation inside a string-valued formula.
    if ((calc_datatype or '').lower() in ('string', 'str')
            and any(node[0] == 'op' and node[1] == '+' for node in nodes)):
        raise _SourceExpressionError("String and numeric addition need operand types")

    null_propagating_string_calls = {
        'LEFT', 'RIGHT', 'MID', 'UPPER', 'LOWER', 'TRIM', 'LEN', 'STR',
    }
    safe_comparison_calls = _SOURCE_AGGREGATES | null_propagating_string_calls | {
        'IF', 'IIF', 'ISNULL', 'IFNULL', 'ZN', 'NULLIF',
        'BLANK', 'TRUE', 'FALSE', 'TODAY', 'NOW', 'PI',
        'USERNAME', 'FULLNAME',
        'DATE', 'DATEADD', 'DATEPART', 'DATENAME', 'DATEDIFF',
        'DATEVALUE', 'DATETIME', 'DATEPARSE', 'YEAR', 'QUARTER',
        'MONTH', 'DAY', 'HOUR', 'MINUTE', 'SECOND', 'WEEKNUM',
        'WEEKDAY', 'EDATE', 'LEN',
    }
    if null_numeric_functions:
        safe_comparison_calls = (
            safe_comparison_calls | set(_NUMERIC_SCALAR_SOURCE_ARITIES)
        )
    for node in nodes:
        if (node[0] == 'call' and node[1].upper() in ('USERNAME', 'FULLNAME')
                and node[2]):
            raise _SourceExpressionError("Identity function takes no arguments")
        if (node[0] == 'call'
                and node[1].upper() in null_propagating_string_calls):
            expected_args = {'LEFT': 2, 'RIGHT': 2, 'MID': 3}.get(
                node[1].upper(), 1)
            if len(node[2]) != expected_args:
                raise _SourceExpressionError(
                    "String function has an unsupported argument count")
        if node[0] == 'op' and node[1] in _SOURCE_COMPARISONS:
            for operand in node[2:]:
                if any(part[0] == 'call'
                       and part[1].upper() not in safe_comparison_calls
                       for part in _source_walk(operand)):
                    raise _SourceExpressionError(
                        "Comparison operand has unverified null semantics")
        if node[0] == 'call' and node[1].upper() == 'IIF' and len(node[2]) not in (3, 4):
            raise _SourceExpressionError("IIF requires three or four arguments")

    prefix = '__TableauNullable'
    while prefix.casefold() in text.casefold():
        prefix = '_' + prefix
    counter = 0

    def variable():
        nonlocal counter
        name = f'{prefix}{counter}'
        counter += 1
        return name

    def binary_vars(left, right, body):
        a, b = variable(), variable()
        return f'(VAR {a} = {left} VAR {b} = {right} RETURN {body(a, b)})'

    def nonempty_string_literal(node):
        node = _ungroup_source(node)
        if node[0] != 'atom':
            return False
        token = node[1]
        return (len(token) > 2 and token[0] in ('"', "'")
                and token[-1] == token[0])

    def small_nonnegative_integer(node):
        node = _ungroup_source(node)
        if node[0] != 'atom' or not re.fullmatch(r'\d{1,9}', node[1]):
            return None
        return int(node[1])

    def numeric_literal(node):
        node = _ungroup_source(node)
        return (node[0] == 'atom'
                and re.fullmatch(r'(?:\d+(?:\.\d*)?|\.\d+)'
                                 r'(?:[eE][+-]?\d+)?', node[1]) is not None)

    def sum_of_field(node):
        node = _ungroup_source(node)
        return (node[0] == 'call' and node[1].upper() == 'SUM'
                and len(node[2]) == 1
                and _ungroup_source(node[2][0])[0] == 'ref')

    def coalesced_conditional_count(node):
        """Only COUNT(IF/SWITCH(...)) is explicitly lowered to COALESCE(..., 0)."""
        node = _ungroup_source(node)
        if node[0] != 'call' or node[1].upper() != 'COUNT' or len(node[2]) != 1:
            return False
        argument = _ungroup_source(node[2][0])
        return argument[0] == 'call' and argument[1].upper() in ('IF', 'SWITCH')

    def truth_condition(node):
        """Only IF's direct condition may collapse unknown to not-TRUE."""
        node = _ungroup_source(node)
        if node[0] == 'op' and node[1] in _SOURCE_COMPARISONS:
            left, right = render(node[2]), render(node[3])
            operator = {'==': '=', '!=': '<>'}.get(node[1], node[1])
            # A NULL compared to a nonempty string literal is UNKNOWN in
            # Tableau but FALSE under DAX's loose equality. Both choose IF's
            # else branch. Do not apply this to empty strings, <>/NOT, IIF,
            # or comparisons whose UNKNOWN result is itself observable.
            if operator == '=' and (
                    (_ungroup_source(node[2])[0] == 'ref'
                     and nonempty_string_literal(node[3]))
                    or (_ungroup_source(node[3])[0] == 'ref'
                        and nonempty_string_literal(node[2]))):
                return f'{left} = {right}'
            # Likewise, BLANK coerces to zero for a direct numeric IF test.
            # Keep this restricted to small integer literals whose comparison
            # with zero is false; the nullable result must not escape the IF.
            left_ref = _ungroup_source(node[2])[0] == 'ref'
            right_ref = _ungroup_source(node[3])[0] == 'ref'
            left_int = small_nonnegative_integer(node[2])
            right_int = small_nonnegative_integer(node[3])
            safe_numeric_test = (
                (left_ref and right_int is not None
                 and (operator == '>' or (right_int > 0 and operator in ('>=', '='))))
                or (right_ref and left_int is not None
                    and (operator == '<' or (left_int > 0 and operator in ('<=', '='))))
            )
            if safe_numeric_test:
                return f'{left} {operator} {right}'
            return binary_vars(
                left, right,
                lambda a, b: (f'NOT(ISBLANK({a})) && NOT(ISBLANK({b})) '
                              f'&& ({a} {operator} {b})'))
        return render(node)

    def render(node):
        kind = node[0]
        if kind in ('atom', 'ref'):
            value = node[1]
            return value + '()' if kind == 'atom' and value.upper() in ('TRUE', 'FALSE') else value
        if kind == 'missing':
            return ''
        if kind == 'group':
            return '(' + render(node[1]) + ')'
        if kind == 'unary':
            value = render(node[2])
            name = variable()
            body = f'NOT({name})' if node[1] == 'NOT' else f'{node[1]}({name})'
            return f'(VAR {name} = {value} RETURN IF(ISBLANK({name}), BLANK(), {body}))'
        if kind == 'op':
            operator, left, right = node[1], render(node[2]), render(node[3])
            if (operator == '/' and coalesced_conditional_count(node[2])
                    and coalesced_conditional_count(node[3])):
                # Both inputs become numeric zero rather than BLANK. DIVIDE
                # still returns BLANK for an empty/zero denominator.
                return f'DIVIDE({left}, {right})'
            if (operator == '*'
                    and ((sum_of_field(node[2]) and numeric_literal(node[3]))
                         or (numeric_literal(node[2]) and sum_of_field(node[3])))):
                # SUM of a field and a numeric literal are numeric operands;
                # DAX multiplication itself preserves BLANK on either side.
                return f'{left} * {right}'
            if operator in _SOURCE_BOOLEAN:
                def boolean(a, b):
                    if operator in ('AND', '&&'):
                        return (f'IF(NOT(ISBLANK({a})) && NOT({a}), FALSE(), '
                                f'IF(NOT(ISBLANK({b})) && NOT({b}), FALSE(), '
                                f'IF(ISBLANK({a}) || ISBLANK({b}), BLANK(), TRUE())))')
                    return (f'IF(NOT(ISBLANK({a})) && {a}, TRUE(), '
                            f'IF(NOT(ISBLANK({b})) && {b}, TRUE(), '
                            f'IF(ISBLANK({a}) || ISBLANK({b}), BLANK(), FALSE())))')
                return binary_vars(left, right, boolean)
            mapped = {'==': '=', '!=': '<>'}.get(operator, operator)
            def scalar(a, b):
                expression = f'DIVIDE({a}, {b})' if mapped == '/' else f'({a} {mapped} {b})'
                return f'IF(ISBLANK({a}) || ISBLANK({b}), BLANK(), {expression})'
            return binary_vars(left, right, scalar)
        name = node[1]
        args = node[2]
        upper = name.upper()
        if upper == 'IF' and len(args) in (2, 3):
            return name + '(' + ', '.join(
                [truth_condition(args[0])] + [render(arg) for arg in args[1:]]
            ) + ')'
        if upper == 'IIF':
            condition = render(args[0])
            var = variable()
            unknown = render(args[3]) if len(args) == 4 else 'BLANK()'
            return (f'(VAR {var} = {condition} RETURN IF(ISBLANK({var}), {unknown}, '
                    f'IF({var}, {render(args[1])}, {render(args[2])})))')
        if upper in ('AND', 'OR') and len(args) == 2:
            return render(('op', upper, args[0], args[1]))
        if upper == 'NOT' and len(args) == 1:
            return render(('unary', 'NOT', args[0]))
        if (null_numeric_functions
                and upper in _NUMERIC_SCALAR_SOURCE_ARITIES
                and len(args) in _NUMERIC_SCALAR_SOURCE_ARITIES[upper]):
            rendered_args = [render(arg) for arg in args]
            names = [variable() for _ in rendered_args]
            bindings = ' '.join(
                f'VAR {var} = {value}' for var, value in zip(names, rendered_args))
            nulls = ' || '.join(f'ISBLANK({var})' for var in names)
            call_args = names + (['0'] if upper == 'ROUND' and len(names) == 1 else [])
            call = f'{upper}({", ".join(call_args)})'
            return f'({bindings} RETURN IF({nulls}, BLANK(), {call}))'
        if upper in null_propagating_string_calls and args:
            rendered_args = [render(arg) for arg in args]
            names = [variable() for _ in rendered_args]
            bindings = ' '.join(
                f'VAR {var} = {value}' for var, value in zip(names, rendered_args))
            nulls = ' || '.join(f'ISBLANK({var})' for var in names)
            call = f'{name}({", ".join(names)})'
            return f'({bindings} RETURN IF({nulls}, BLANK(), {call}))'
        return name + '(' + ', '.join(render(arg) for arg in args) + ')'

    return render(root)


def _prepare_source_expression(text, *, table_name, column_table_map,
                               measure_names, is_calc_column, table_columns,
                               calc_datatype, verified_row_scope,
                               date_columns=None, numeric_columns=None,
                               nonphysical_names=(), row_approx_measure_names=(),
                               _row_approx_out=None):
    """Fail closed on unknown row grain; otherwise preserve nullable logic."""
    try:
        root = _SourceExpressionParser(text).parse()
        scalar_extrema_rewritten = False

        def lower_scalar_extrema(node):
            nonlocal scalar_extrema_rewritten
            if node[0] == 'call':
                reference = _scalar_numeric_minmax_reference(
                    node, table_name=table_name, table_columns=table_columns,
                    numeric_columns=numeric_columns,
                    column_table_map=column_table_map, measure_names=measure_names,
                    nonphysical_names=nonphysical_names)
                if reference is not None:
                    left, right = node[2]
                    comparison = '<' if node[1].upper() == 'MIN' else '>'
                    scalar_extrema_rewritten = True
                    return ('call', 'IF', (
                        ('call', 'ISNULL', (reference,)),
                        ('call', 'BLANK', ()),
                        ('call', 'IF', (('op', comparison, left, right), left, right))))
                arguments = tuple(lower_scalar_extrema(arg) for arg in node[2])
                node = ('call', node[1], arguments)
                if (node[1].upper() in ('MIN', 'MAX')
                    and len(arguments) == 1
                    and isinstance(table_columns, (list, tuple, set, frozenset))
                    and isinstance(date_columns, (list, tuple, set, frozenset))
                    and all(isinstance(name, str) and name for name in date_columns)):
                    year = _ungroup_source(arguments[0])
                    if (year[0] == 'call' and year[1].upper() == 'YEAR'
                            and len(year[2]) == 1):
                        reference = _ungroup_source(year[2][0])
                        if reference[0] == 'ref':
                            owner, name = reference[2:]
                            if (name in table_columns
                                    and name in date_columns
                                    and name not in measure_names
                                    and (owner is None or owner == table_name)
                                    and column_table_map.get(name, table_name) == table_name):
                                aggregate = ('call', node[1], (reference,))
                                scalar_extrema_rewritten = True
                                return ('call', 'IF', (
                                    ('call', 'ISNULL', (aggregate,)),
                                    ('call', 'BLANK', ()),
                                    ('call', 'YEAR', (aggregate,))))
                return node
            if node[0] == 'op':
                return (node[0], node[1], lower_scalar_extrema(node[2]),
                        lower_scalar_extrema(node[3]))
            if node[0] in ('unary', 'group'):
                return (*node[:-1], lower_scalar_extrema(node[-1]))
            return node

        root = lower_scalar_extrema(root)
        row_approx_names = {
            name.casefold() for name in row_approx_measure_names
            if isinstance(name, str) and name
        }
        unwrapped_row_measures = set()

        def unwrap_row_measure_sum(node):
            if node[0] == 'call':
                if node[1].upper() == 'SUM' and len(node[2]) == 1:
                    reference = _ungroup_source(node[2][0])
                    if reference[0] == 'ref':
                        owner, name = reference[2:]
                        if (name.casefold() in row_approx_names
                                and (owner is None or owner == table_name)):
                            unwrapped_row_measures.add(name)
                            return reference
                return ('call', node[1], tuple(
                    unwrap_row_measure_sum(argument) for argument in node[2]))
            if node[0] == 'op':
                return (node[0], node[1], unwrap_row_measure_sum(node[2]),
                        unwrap_row_measure_sum(node[3]))
            if node[0] == 'group':
                return ('group', unwrap_row_measure_sum(node[1]))
            if node[0] == 'unary':
                return (node[0], node[1], unwrap_row_measure_sum(node[2]))
            return node

        root = unwrap_row_measure_sum(root)
        if unwrapped_row_measures and isinstance(_row_approx_out, dict):
            _row_approx_out['row_measure_sum_unwrapped'] = sorted(
                unwrapped_row_measures, key=str.casefold)
        issue, force_arithmetic = _check_source_grain(
            root, table_name, column_table_map, measure_names,
            is_calc_column, table_columns, verified_row_scope)
        if (issue == ('unaggregated row expression requires source row scope, '
                      'shelf aggregation and total grain')
                and isinstance(_row_approx_out, dict)):
            _row_approx_out['unaggregated_row_approx'] = True
            _row_approx_out['has_agg_calls'] = any(
                part[0] == 'call' and part[1].upper() in _SOURCE_AGGREGATES
                for part in _source_walk(root)
            )
            issue = None
        if issue:
            return text, issue
        return _render_nullable_source(
            root, text, calc_datatype,
            force_arithmetic or scalar_extrema_rewritten,
            force_render=bool(unwrapped_row_measures)), None
    except (_SourceExpressionError, RecursionError):
        masked, _ = _protect_source_tokens(text)
        if (table_columns is None
                and re.search(r'\{\s*(?:FIXED|INCLUDE|EXCLUDE)\b', masked, re.IGNORECASE)
                and not re.search(r'==|!=|<>|<=|>=|[=<>]|\b(?:AND|OR|NOT|IIF)\b',
                                  masked, re.IGNORECASE)):
            # Preserve the context-free legacy LOD translation, with its
            # explicit warning. Model generation takes the guarded path.
            return text, None
        if (re.search(r'==|!=|<>|<=|>=|[=<>]|\b(?:AND|OR|NOT|IIF)\b',
                      masked, re.IGNORECASE)
                or re.search(r'\b(?:SUM|AVG|AVERAGE|MIN|MAX|COUNT|COUNTD)\s*\(',
                             masked, re.IGNORECASE)):
            return text, 'nullable expression or aggregation is outside the verified grammar'
        return text, None


# ── Literal and comment handling ─────────────────────────────────────────────

def _convert_measure_ref_ratio(dax, measure_names):
    """Use DIVIDE for two bare measures, allowing grouping parentheses.

    Tokenize only references, parentheses and slash; reject everything else.
    In particular, slashes inside identifiers are not operators, and strings,
    qualified columns, functions and compound arithmetic are left untouched.
    """
    if '/' not in dax or not measure_names:
        return dax
    tokens = []
    end = 0
    for match in re.finditer(r'\s+|\[(?:[^\]]|\]\])+\]|[()/]', dax):
        if match.start() != end:
            return dax
        end = match.end()
        if not match.group().isspace():
            tokens.append(match.group())
    if end != len(dax):
        return dax

    stack = []
    pairs = {}
    slashes = []
    for index, token in enumerate(tokens):
        if token == '(':
            stack.append(index)
        elif token == ')':
            if not stack:
                return dax
            pairs[stack.pop()] = index
        elif token == '/':
            slashes.append(index)
    if stack or len(slashes) != 1:
        return dax

    def ungroup(start, stop):
        while start < stop and tokens[start] == '(' and pairs.get(start) == stop - 1:
            start += 1
            stop -= 1
        return start, stop

    start, stop = ungroup(0, len(tokens))
    slash = slashes[0]
    if not start < slash < stop - 1:
        return dax
    operands = []
    for left, right in ((start, slash), (slash + 1, stop)):
        left, right = ungroup(left, right)
        if right - left != 1 or not tokens[left].startswith('['):
            return dax
        ref = tokens[left]
        if ref[1:-1].replace(']]', ']') not in measure_names:
            return dax
        operands.append(ref)
    return f'DIVIDE({operands[0]}, {operands[1]})'


def _convert_safe_division(dax):
    """Rewrite division in supported scalar DAX expressions, including LODs.

    A small, fail-closed expression parser retains source spans and precedence.
    It supports calls, qualified refs, literals, grouping and scalar operators;
    unsupported syntax (e.g. VAR/RETURN or table constructors) is left intact.
    Quoted tokens, escaped brackets and comments are never slash operators.
    No alternate result is supplied: zero/BLANK denominators yield BLANK.
    """
    if '/' not in dax:
        return dax

    token_pattern = re.compile(
        r'\s+|/\*[\s\S]*?\*/|//[^\r\n]*'
        + '|' + _RE_NULL_PROTECTED_TOKEN.pattern
        + r'|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
        + r'|[^\W\d]\w*(?:\.\w+)*'
        + r'|==|<>|!=|<=|>=|&&|\|\||[()+\-*/^&=<>!,]',
    )
    tokens = []
    end = 0
    for match in token_pattern.finditer(dax):
        if match.start() != end:
            return dax
        end = match.end()
        value = match.group()
        if not value.isspace() and not value.startswith(('/*', '//')):
            tokens.append((value, match.start(), match.end()))
    if end != len(dax) or not any(t[0] == '/' for t in tokens):
        return dax

    # DAX scalar precedence, from weakest to strongest. Operators at each
    # level are left-associative, including chained multiplication/division.
    precedence = {
        '||': 10, '&&': 20,
        '=': 30, '==': 30, '<>': 30, '!=': 30,
        '<': 30, '>': 30, '<=': 30, '>=': 30,
        '&': 40, '+': 50, '-': 50,
        '*': 60, '/': 60, '^': 80,
    }
    position = 0

    class Unsupported(ValueError):
        pass

    def peek():
        # '' marks the end of input; real tokens are never empty.
        return tokens[position][0] if position < len(tokens) else ''

    def take(value=None):
        nonlocal position
        if position >= len(tokens) or (value is not None and peek() != value):
            raise Unsupported()
        token = tokens[position]
        position += 1
        return token

    def node(start, stop, children=()):
        # Keep all original spelling/spacing except the rewritten subtrees.
        parts = []
        cursor = start
        for left, right, text in children:
            parts.extend((dax[cursor:left], text))
            cursor = right
        parts.append(dax[cursor:stop])
        return start, stop, ''.join(parts)

    def expression(minimum=0):
        token, start, stop = take()
        if token in ('+', '-') or token.upper() == 'NOT':
            child = expression(25 if token.upper() == 'NOT' else 70)
            left = node(start, child[1], (child,))
        elif token == '(':
            child = expression()
            close = take(')')
            left = node(start, close[2], (child,))
        elif token[0] in ('"', "'", '[') or re.match(r'\w|\.\d', token):
            # A quoted/unquoted table plus bracketed field is one operand.
            if peek() and peek().startswith('[') and not token.startswith(('"', '[')):
                stop = take()[2]
            if peek() == '(' and not token.startswith(('"', "'", '[')):
                take('(')
                children = []
                if peek() != ')':
                    while True:
                        # DAX permits omitted optional function arguments.
                        if peek() not in (',', ')'):
                            children.append(expression())
                        if peek() != ',':
                            break
                        take(',')
                stop = take(')')[2]
                left = node(start, stop, children)
            else:
                left = node(start, stop)
        else:
            raise Unsupported()

        while peek() in precedence and precedence[peek()] >= minimum:
            operator, op_start, op_stop = take()
            right = expression(precedence[operator] + 1)
            if operator == '/':
                # Retain comments adjacent to the operator (including a line
                # comment's terminating newline), not the slash itself.
                before = dax[left[1]:op_start]
                after = dax[op_stop:right[0]]
                before = before if before.strip() else ''
                after = after if after.strip() else ''
                left = (
                    left[0], right[1],
                    f'DIVIDE({left[2]}{before}, {after}{right[2]})',
                )
            else:
                left = node(left[0], right[1], (left, right))
        return left

    try:
        result = expression()
        if position != len(tokens):
            return dax
        return dax[:result[0]] + result[2] + dax[result[1]:]
    except (Unsupported, RecursionError):
        return dax


def _convert_null_literals(text):
    """Convert NULL tokens without rewriting strings or field/table names."""
    return _RE_NULL_LITERAL.sub(
        lambda match: 'BLANK()' if match.group('null') else match.group(0),
        text,
    )


def _normalize_tableau_quote_escapes(text):
    def normalize(match):
        token = match.group(0)
        delimiter = token[0]
        if delimiter == '[' or re.match(r'\s*\[', text[match.end():]):
            return token
        return re.sub(
            r'\\(.)',
            lambda escape: delimiter * 2 if escape.group(1) == delimiter
            else escape.group(0),
            token,
        )

    return _RE_NULL_PROTECTED_TOKEN.sub(normalize, text)


def _normalize_tableau_group_references(text):
    """Normalize Tableau's split-bracket group field spelling."""
    pattern = re.compile(
        r'"(?:[^"\\]|\\.|"")*"|'
        r"'(?:[^'\\]|\\.|'')*'|"
        r'(?P<group>\[(?:[^\]]|\]\])+\]\s*\((?:group|groupe)\)\])',
        re.IGNORECASE,
    )

    def normalize(match):
        token = match.group('group')
        if token is None:
            return match.group(0)
        parts = re.fullmatch(
            r'(?P<base>\[(?:[^\]]|\]\])+\])\s*\('
            r'(?P<suffix>group|groupe)\)\]',
            token, re.IGNORECASE,
        )
        if parts is None:
            return token
        field = parts.group('base')[1:-1].replace(']]', ']')
        suffix = parts.group('suffix')
        return '[' + field + ' (' + suffix + ')]'

    return pattern.sub(normalize, text)


def _strip_tableau_comments(text, *, keep_block_comments=False,
                            tableau_string_escapes=True):
    """Remove Tableau comments while preserving literals and field names."""
    out = []
    i = 0
    while i < len(text):
        if text[i] in ('"', "'", '['):
            closer = {'"': '"', "'": "'", '[': ']'}[text[i]]
            start = i
            i += 1
            while i < len(text):
                if (tableau_string_escapes and closer != ']'
                        and text[i] == '\\' and i + 1 < len(text)):
                    i += 2
                    continue
                if text[i] == closer:
                    i += 1
                    if i < len(text) and text[i] == closer:
                        i += 1
                        continue
                    break
                i += 1
            out.append(text[start:i])
            continue
        if text.startswith('//', i):
            newline = text.find('\n', i)
            i = len(text) if newline < 0 else newline
            continue
        if text.startswith('/*', i):
            end = text.find('*/', i + 2)
            if keep_block_comments:
                out.append(text[i:] if end < 0 else text[i:end + 2])
            i = len(text) if end < 0 else end + 2
            continue
        out.append(text[i])
        i += 1
    return ''.join(out)


# ── Phase 1: Reference resolution ────────────────────────────────────────────

def _resolve_references(dax, calc_map, param_map, is_calc_column, param_values):
    """Resolve [Parameters].[X] and [Calculation_xxx] references."""

    # [Parameters].[Parameter X] → [caption] or inline value
    def resolve_param(m):
        param_id = m.group(1)
        if param_id in param_map:
            caption = param_map[param_id]
        else:
            # param_map keys are typically stored with all brackets stripped;
            # normalize the captured id (which may retain literal '[' and the
            # ']]' escape) the same way before falling back to the raw id.
            normalized = param_id.replace('[', '').replace(']', '')
            caption = param_map.get(normalized, param_id)
        if is_calc_column and caption in param_values:
            return param_values[caption]
        return f'[{sanitize_param_brackets(caption)}]'
    dax = _RE_PARAM_REF.sub(resolve_param, dax)

    # [Calculation_xxx] → [caption]
    def resolve_calc(m):
        ref = m.group(1)
        if ref in calc_map:
            return f'[{calc_map[ref]}]'
        return m.group(0)
    dax = _RE_CALC_REF.sub(resolve_calc, dax)

    # Bare (unbracketed) references to literal-value calculations.
    # Tableau occasionally uses calc names without brackets
    # (e.g. __MyToday instead of [__MyToday]).  DAX requires brackets
    # for identifiers.  Since param_values entries are literal constants
    # (numbers, DATE(), strings), inlining is always safe and avoids
    # classification mismatches (e.g. dimension-role calcs not in
    # measure_names).
    if param_values:
        for pname in sorted(param_values, key=len, reverse=True):
            if pname not in dax:
                continue
            repl = param_values[pname]
            # Process outside string literals AND bracket expressions
            # to avoid replacing inside "strings" or [column names].
            parts = re.split(r'("(?:[^"\\]|\\.)*"|\[[^\]]*\])', dax)
            for i in range(0, len(parts), 2):
                # Negative lookahead: don't substitute if the bare token is
                # immediately followed by '(' — that means it's being used
                # as a function call (e.g. `Index()`), and replacing the
                # name with a literal value would yield broken DAX like
                # `1()`.  Such call-style references must be left intact for
                # the dedicated function converters (e.g. `_convert_index`).
                parts[i] = re.sub(
                    r'\b' + re.escape(pname) + r'\b(?!\s*\()',
                    repl, parts[i]
                )
            dax = ''.join(parts)

    return dax


# ── Phase 2: IF and CASE structure conversion ────────────────────────────────

def _convert_case_structure(text):
    """
    Converts Tableau CASE/WHEN/THEN/ELSE/END structures to DAX SWITCH().

    Tableau: CASE [field] WHEN 'A' THEN 1 WHEN 'B' THEN 2 ELSE 0 END
    DAX:     SWITCH([field], "A", 1, "B", 2, 0)
    """
    # Each iteration consumes exactly one CASE...END, so budget by content:
    # a fixed cap silently leaves raw Tableau syntax in long chains.
    max_iter = len(re.findall(r'\bCASE\b', text, re.IGNORECASE)) + 20
    for _ in range(max_iter):
        m = re.search(
            r'\bCASE\s+((?:(?!\bCASE\b|\bEND\b).)*?)\s+WHEN\s+((?:(?!\bCASE\b|\bEND\b).)*?)\s+END\b',
            text, re.IGNORECASE | re.DOTALL
        )
        if not m:
            break

        expr = m.group(1).strip()
        when_block = m.group(2).strip()

        # Parse WHEN value THEN result pairs
        parts = re.split(r'\bWHEN\b', when_block, flags=re.IGNORECASE)
        # CASE compares each WHEN using Tableau's nullable comparison rules.
        # The source-expression pass below lowers these comparisons to
        # three-valued DAX; SWITCH(TRUE(), ...) then selects only TRUE.
        switch_args = ['TRUE()']
        else_val = None

        for part in parts:
            part = part.strip()
            if not part:
                continue

            # Check for ELSE clause
            else_match = re.search(r'\bELSE\s+(.*)', part, re.IGNORECASE | re.DOTALL)
            if else_match:
                # Split off the ELSE
                before_else = part[:else_match.start()].strip()
                else_val = else_match.group(1).strip()
                part = before_else

            # Parse THEN
            then_match = re.search(r'\bTHEN\b', part, re.IGNORECASE)
            if then_match:
                when_val = part[:then_match.start()].strip()
                then_val = part[then_match.end():].strip()
                switch_args.append(f'({expr}) = ({when_val})')
                switch_args.append(then_val)

        if else_val:
            switch_args.append(else_val)

        replacement = f'SWITCH({", ".join(switch_args)})'
        text = text[:m.start()] + replacement + text[m.end():]

    return text


def _convert_if_structure(text):
    """
    Converts Tableau IF/THEN/ELSEIF/ELSE/END structures to DAX IF().

    Handles nested structures (processed from innermost to outermost).
    """
    # Pre-processing: ELSEIF → ELSE IF + add corresponding ENDs
    elseif_count = len(re.findall(r'\bELSEIF\b', text, re.IGNORECASE))
    if elseif_count > 0:
        text = _RE_ELSEIF.sub('ELSE IF', text)
        text = text.rstrip() + ' END' * elseif_count

    # Each iteration consumes exactly one IF...END, so budget by content:
    # a fixed cap silently leaves raw Tableau syntax in long chains.
    max_iter = len(re.findall(r'\bEND\b', text, re.IGNORECASE)) + 30
    for _ in range(max_iter):
        # IF cond THEN val ELSE val2 END (innermost)
        # Note: the anchor ``\bIF\b\s*`` (not ``\bIF\s+``) lets Tableau's
        # paren-style ``if(cond) then ...`` (no space before ``(``) match too.
        # The content lookaheads keep ``\bIF\s`` (keyword form only) so an
        # already-converted inner ``IF(...)`` does NOT block the outer IF.
        m = re.search(
            r'\bIF\b\s*((?:(?!\bIF\s|\bEND\b).)*?)\s+THEN\s+((?:(?!\bIF\s|\bEND\b).)*?)\s+ELSE\s+((?:(?!\bIF\s|\bEND\b).)*?)\s*END\b',
            text, re.IGNORECASE | re.DOTALL
        )
        if m:
            cond, val1, val2 = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
            text = text[:m.start()] + f'IF({cond}, {val1}, {val2})' + text[m.end():]
            continue

        # IF cond THEN val END (no ELSE)
        m = re.search(
            r'\bIF\b\s*((?:(?!\bIF\s|\bEND\b).)*?)\s+THEN\s+((?:(?!\bIF\s|\bEND\b).)*?)\s*END\b',
            text, re.IGNORECASE | re.DOTALL
        )
        if m:
            cond, val1 = m.group(1).strip(), m.group(2).strip()
            text = text[:m.start()] + f'IF({cond}, {val1}, BLANK())' + text[m.end():]
            continue

        break

    return text


# ── Phase 3 helpers ───────────────────────────────────────────────────────────

def _convert_datediff(dax_str):
    """Reorder Tableau DATEDIFF only for a known literal interval."""
    supported_intervals = {
        'YEAR', 'QUARTER', 'MONTH', 'DAY',
        'HOUR', 'MINUTE', 'SECOND',
    }

    def convert(args, inner, restore):
        if len(args) < 3:
            return _unsupported_conversion(
            'DATEDIFF requires at least three arguments')
        interval_arg = restore(args[0]).strip()
        if (len(interval_arg) < 2
                or interval_arg[0] not in ('"', "'")
                or interval_arg[-1] != interval_arg[0]):
            return _unsupported_conversion(
                'DATEDIFF requires a supported literal interval')
        interval = interval_arg[1:-1].upper()
        start = restore(args[1]).strip()
        end = restore(args[2]).strip()
        if interval == 'WEEK':
            return (
                '/* DATEDIFF best-effort approximation; WEEK uses 7-day '
                'buckets, week-start and extra arguments unverified */ '
                f'QUOTIENT(DATEDIFF({start}, {end}, DAY), 7)'
            )
        if len(args) != 3:
            return _unsupported_conversion(
                'DATEDIFF extra arguments are unsupported for this interval')
        if interval not in supported_intervals:
            return _unsupported_conversion(
                'DATEDIFF requires a supported literal interval')
        return f"DATEDIFF({start}, {end}, {interval})"

    return _transform_date_func_call(dax_str, 'DATEDIFF', convert)


def _convert_dateadd(dax_str):
    """DATEADD('date_part', interval, date) → scalar date arithmetic.

    Tableau DATEADD is a **scalar** function: DATEADD(date_part, interval, date).
    DAX DATEADD is a **Time Intelligence TABLE** function that requires a date
    column from a marked date table — completely different semantics.

    Convert to scalar DAX equivalents:
      - MONTH  → EDATE(date, n)
      - YEAR   → EDATE(date, n * 12)
      - QUARTER→ EDATE(date, n * 3)
      - DAY    → date + n
      - WEEK   → date + n * 7
      - HOUR   → date + n / 24
      - MINUTE → date + n / 1440
      - SECOND → date + n / 86400
    """
    def _xf(args, inner):
        if len(args) == 3:
            interval_unit = args[0].strip().strip("'\"").upper()
            number = args[1].strip()
            date_expr = args[2].strip()
            if interval_unit == 'MONTH':
                return f"EDATE({date_expr}, {number})"
            elif interval_unit == 'YEAR':
                return f"EDATE({date_expr}, ({number}) * 12)"
            elif interval_unit == 'QUARTER':
                return f"EDATE({date_expr}, ({number}) * 3)"
            elif interval_unit == 'DAY':
                return f"({date_expr} + {number})"
            elif interval_unit == 'WEEK':
                return f"({date_expr} + ({number}) * 7)"
            elif interval_unit == 'HOUR':
                return f"({date_expr} + ({number}) / 24)"
            elif interval_unit == 'MINUTE':
                return f"({date_expr} + ({number}) / 1440)"
            elif interval_unit == 'SECOND':
                return f"({date_expr} + ({number}) / 86400)"
            else:
                return _unsupported_conversion(
                    'DATEADD requires a supported date part')
        return f"DATEADD({inner})"
    return _transform_func_call(dax_str, 'DATEADD', _xf)


def _extract_balanced_call(dax, func_name):
    """Find a balanced-paren function call and return (start, end, inner_text).

    Returns a list of (start, end, inner) tuples for every occurrence.
    Uses depth-tracking to handle nested parentheses correctly.
    """
    results = []
    pattern = _get_func_pattern(func_name)
    offset = 0
    while True:
        match = pattern.search(dax, offset)
        if not match:
            break
        start_pos = match.end()
        depth = 1
        i = start_pos
        while i < len(dax) and depth > 0:
            if dax[i] == '(':
                depth += 1
            elif dax[i] == ')':
                depth -= 1
            i += 1
        if depth != 0:
            break
        inner = dax[start_pos:i - 1]
        results.append((match.start(), i, inner))
        offset = i
    return results


def _convert_percentile(dax):
    """Convert only PERCENTILE calls over one direct physical column."""
    masked, restore = _protect_source_tokens(dax)
    pattern = _get_func_pattern('PERCENTILE')
    if not pattern.search(masked):
        return dax

    calls = _extract_balanced_call(masked, 'PERCENTILE')
    if len(calls) != len(list(pattern.finditer(masked))):
        return _unsupported_conversion(
            'PERCENTILE requires a balanced call over a direct physical column')

    for start, end, inner in reversed(calls):
        args = _split_args(inner)
        if (len(args) != 2 or not all(args)
                or not _is_bare_column_ref(restore(args[0]))):
            return _unsupported_conversion(
                'PERCENTILE requires exactly two arguments and a direct physical column')
        replacement = f"PERCENTILE.INC({restore(args[0])}, {restore(args[1])})"
        masked = masked[:start] + replacement + masked[end:]

    return restore(masked)


def _convert_zn(dax):
    """ZN(expr) → IF(ISBLANK(expr), 0, expr)"""
    masked, restore = _protect_source_tokens(dax)
    for start, end, inner in reversed(_extract_balanced_call(masked, 'ZN')):
        inner = _convert_zn(restore(inner))
        replacement = f'IF(ISBLANK({inner}), 0, {inner})'
        masked = masked[:start] + replacement + masked[end:]
    return restore(masked)


def _convert_ifnull(dax):
    """IFNULL(a, b) → IF(ISBLANK(a), b, a)"""
    masked, restore = _protect_source_tokens(dax)
    for start, end, inner in reversed(_extract_balanced_call(masked, 'IFNULL')):
        parts = _split_args(_convert_ifnull(restore(inner)))
        if len(parts) == 2:
            replacement = f'IF(ISBLANK({parts[0].strip()}), {parts[1].strip()}, {parts[0].strip()})'
        else:
            replacement = masked[start:end]
        masked = masked[:start] + replacement + masked[end:]
    return restore(masked)


def _convert_nullif(dax):
    """NULLIF(a, b) → blank when strictly equal, otherwise a (including zero)."""
    if not _get_func_pattern('NULLIF').search(dax):
        return dax

    # Hide quotes/brackets from the existing balanced-call/argument helpers:
    # commas, parentheses and function names within these tokens are data.
    prefix = '__nullif_token_'
    while prefix in dax:
        prefix = '_' + prefix
    tokens = []

    def protect(match):
        tokens.append(match.group(0))
        return f'{prefix}{len(tokens) - 1}__'

    masked = _RE_NULL_PROTECTED_TOKEN.sub(protect, dax)

    def rewrite(text):
        def transform(args, inner):
            if len(args) != 2 or not all(args):
                return f'NULLIF({inner})'
            left, right = (rewrite(arg) for arg in args)
            # Loose DAX equality conflates BLANK with 0 and empty text.
            # Parenthesize arbitrary argument expressions to retain precedence.
            return f'IF(({left}) == ({right}), BLANK(), ({left}))'

        return _transform_func_call(text, 'NULLIF', transform)

    converted = rewrite(masked)
    return re.sub(
        re.escape(prefix) + r'(\d+)__',
        lambda match: tokens[int(match.group(1))],
        converted,
    )


def _convert_previous_value(dax, table_name, compute_using=None, column_table_map=None):
    """Convert PREVIOUS_VALUE(seed) → OFFSET-based DAX.

    Output:
        VAR __prev = CALCULATE([inner], OFFSET(-1, ALLSELECTED('Table'), ORDERBY([dim]), PARTITIONBY([dim2])))
        RETURN IF(ISBLANK(__prev), <seed>, __prev)

    When compute_using is present, first dimension → ORDERBY, remaining → PARTITIONBY.
    """
    column_table_map = column_table_map or {}
    pattern = _RE_PREVIOUS_VALUE
    match = pattern.search(dax)
    while match:
        start_pos = match.end()
        depth = 1
        i = start_pos
        while i < len(dax) and depth > 0:
            if dax[i] == '(':
                depth += 1
            elif dax[i] == ')':
                depth -= 1
            i += 1
        if depth == 0:
            inner = dax[start_pos:i - 1].strip()
            seed = inner if inner else '0'
            if compute_using:
                order_col = compute_using[0]
                order_table = column_table_map.get(order_col, table_name)
                orderby = f"ORDERBY('{order_table}'[{order_col}])"
                # Additional dims → PARTITIONBY
                partition_clause = ""
                if len(compute_using) > 1:
                    parts = []
                    for dim in compute_using[1:]:
                        t = column_table_map.get(dim, table_name)
                        parts.append(f"'{t}'[{dim}]")
                    partition_clause = f", PARTITIONBY({', '.join(parts)})"
            else:
                orderby = "ORDERBY([Value])"
                partition_clause = ""
            replacement = (
                f"VAR __prev = CALCULATE({seed}, "
                f"OFFSET(-1, ALLSELECTED('{table_name}'), {orderby}{partition_clause})) "
                f"RETURN IF(ISBLANK(__prev), {seed}, __prev)"
            )
            dax = dax[:match.start()] + replacement + dax[i:]
        match = pattern.search(dax, match.start() + 1 if depth != 0 else 0)
    return dax


def _convert_lookup(dax, table_name, compute_using=None, column_table_map=None):
    """Convert LOOKUP(expr, offset) → OFFSET-based DAX.

    Output:
        CALCULATE(<expr>, OFFSET(<offset>, ALLSELECTED('Table'), ORDERBY([dim]), PARTITIONBY([dim2])))

    When compute_using has 2+ dims, first → ORDERBY, rest → PARTITIONBY.
    """
    column_table_map = column_table_map or {}
    pattern = _RE_LOOKUP
    masked, restore = _protect_source_tokens(dax)

    def rewrite(text):
        result = []
        cursor = 0
        search_offset = 0
        while True:
            match = pattern.search(text, search_offset)
            if not match:
                break
            start_pos = match.end()
            depth = 1
            end = start_pos
            while end < len(text) and depth > 0:
                if text[end] == '(':
                    depth += 1
                elif text[end] == ')':
                    depth -= 1
                end += 1
            if depth:
                search_offset = match.end()
                continue

            args = _split_args(text[start_pos:end - 1].strip())
            expr = rewrite(args[0].strip()) if args else 'BLANK()'
            offset = rewrite(args[1].strip()) if len(args) > 1 else '0'
            if compute_using:
                order_col = compute_using[0]
                order_table = column_table_map.get(order_col, table_name)
                orderby = f"ORDERBY('{order_table}'[{order_col}])"
                partition_clause = ""
                if len(compute_using) > 1:
                    parts = []
                    for dim in compute_using[1:]:
                        t = column_table_map.get(dim, table_name)
                        parts.append(f"'{t}'[{dim}]")
                    partition_clause = f", PARTITIONBY({', '.join(parts)})"
            else:
                orderby = "ORDERBY([Value])"
                partition_clause = ""
            replacement = (
                f"CALCULATE({expr}, "
                f"OFFSET({offset}, ALLSELECTED('{table_name}'), {orderby}{partition_clause}))"
            )
            result.append(text[cursor:match.start()])
            result.append(replacement)
            cursor = end
            search_offset = end
        result.append(text[cursor:])
        return ''.join(result)

    return restore(rewrite(masked))


def _convert_rawsql(dax):
    """Convert a narrow whitelist of scalar ``RAWSQL_*`` wrappers.

    Tableau RAWSQL accepts arbitrary database-specific SQL, which cannot be
    translated safely in a model-independent way. Only one-argument scalar
    wrappers with a single ``%1`` placeholder are converted; every other form
    becomes a typed BLANK with the original template preserved in a note.
    """
    safe_wrappers = {
        'UPPER': 'UPPER', 'LOWER': 'LOWER', 'TRIM': 'TRIM',
        'LTRIM': 'TRIM', 'RTRIM': 'TRIM', 'LENGTH': 'LEN',
        'ABS': 'ABS', 'ROUND': 'ROUND',
    }

    def _xf(args, function_name):
        type_suffix = function_name.rsplit('_', 1)[-1].upper()
        fallback = '0' if type_suffix in ('BOOL', 'INT', 'REAL') else 'BLANK()'
        if not args:
            return f'/* {function_name}: missing SQL template — manual conversion needed */ {fallback}'
        template = args[0].strip()
        if len(template) < 2 or template[0] not in ('"', "'") or template[-1] != template[0]:
            return f'/* {function_name}: SQL template must be a literal — manual conversion needed */ {fallback}'
        sql = template[1:-1]
        value_args = [arg.strip() for arg in args[1:]]
        if '%1' not in sql or any(f'%{index}' in sql for index in range(2, 10)) or len(value_args) != 1:
            return f'/* {function_name}({sql}): unsupported SQL — manual conversion needed */ {fallback}'
        match = re.fullmatch(r'\s*([A-Za-z]+)\s*\(\s*%1(?:\s*,\s*([0-9]+))?\s*\)\s*', sql)
        if not match or match.group(1).upper() not in safe_wrappers:
            return f'/* {function_name}({sql}): unsupported SQL — manual conversion needed */ {fallback}'
        dax_function = safe_wrappers[match.group(1).upper()]
        value = value_args[0]
        if dax_function == 'ROUND':
            return f'ROUND({value}, {match.group(2) or "0"})'
        return f'{dax_function}({value})'

    for function_name in ('RAWSQL_BOOL', 'RAWSQL_INT', 'RAWSQL_REAL', 'RAWSQL_STR',
                          'RAWSQL_DATE', 'RAWSQL_DATETIME', 'RAWSQL_SPATIAL'):
        dax = _transform_func_call(
            dax, function_name,
            lambda args, inner, _name=function_name: _xf(args, _name),
        )
    return dax


def _convert_radians_degrees(dax):
    """RADIANS(x) → ((x)*PI()/180), DEGREES(x) → ((x)*180/PI())."""
    for func, template in [('RADIANS', '(({inner})*PI()/180)'),
                           ('DEGREES', '(({inner})*180/PI())')]:
        dax = _transform_func_call(dax, func,
                                   lambda args, inner, _t=template: _t.format(inner=inner.strip()))
    return dax


def _convert_find(dax):
    """Swap FIND args: Tableau FIND(string, substring) → DAX FIND(substring, string).

    Also converts FINDNTH → FIND.
    Tableau: FIND(within_text, find_text[, start])
    DAX:     FIND(find_text, within_text[, start[, not_found]])
    """
    masked, restore = _protect_source_tokens(dax)
    masked = _RE_FINDNTH.sub(
        'FIND( /* FINDNTH: occurrence arg needs manual review */ ', masked)
    dax = masked
    pattern = _RE_FIND
    match = pattern.search(dax)
    while match:
        start_pos = match.end()
        depth = 1
        i = start_pos
        while i < len(dax) and depth > 0:
            if dax[i] == '(':
                depth += 1
            elif dax[i] == ')':
                depth -= 1
            i += 1
        if depth != 0:
            break
        inner = dax[start_pos:i - 1]
        inner = _convert_find(inner)
        args = _split_args(inner)
        if len(args) >= 2:
            swapped = [args[1].strip(), args[0].strip()] + [a.strip() for a in args[2:]]
            if len(args) == 2:
                swapped.extend(('1', '0'))
            elif len(args) == 3:
                swapped.append('0')
            replacement = f"FIND({', '.join(swapped)})"
        else:
            replacement = dax[match.start():i]
        dax = dax[:match.start()] + replacement + dax[i:]
        match = pattern.search(dax, match.start() + len(replacement))
    return restore(dax)


def _convert_str_to_format(dax):
    """STR(expr) → FORMAT(expr, "0")"""
    return _transform_func_call(dax, 'STR', lambda args, inner: f'FORMAT({inner.strip()}, "0")')


def _convert_int_tolerant(dax):
    """INT(expr) -> IFERROR(INT(expr), BLANK())

    Tableau's INT() yields Null when the argument is not numeric; DAX's raises
    and poisons the whole column, so the cast has to be guarded to keep parity.
    """
    return _transform_func_call(
        dax, 'INT', lambda args, inner: f'IFERROR(INT({inner.strip()}), BLANK())')


def _convert_float_to_convert(dax):
    """FLOAT(expr) → CONVERT(expr, DOUBLE)"""
    return _transform_func_call(dax, 'FLOAT', lambda args, inner: f'CONVERT({inner.strip()}, DOUBLE)')


def _convert_datename(dax):
    """Convert DATENAME calls without rewriting quoted source text."""
    _DATENAME_FORMATS = {
        'year': '"YYYY"', 'quarter': '"Q"', 'month': '"MMMM"',
        'day': '"D"', 'weekday': '"DDDD"', 'dayofweek': '"DDDD"',
    }
    _DATENAME_SCALAR_UNITS = {
        'hour': 'HOUR', 'minute': 'MINUTE', 'second': 'SECOND',
    }

    def convert(args, inner, restore):
        if len(args) != 2:
            return _unsupported_conversion(
                'DATENAME arity requires exactly two arguments')
        part = restore(args[0]).strip().strip('\'"').lower()
        date = restore(args[1]).strip()
        scalar_function = _DATENAME_SCALAR_UNITS.get(part)
        if scalar_function:
            return (
                f'IF(ISBLANK({date}), BLANK(), '
                f'FORMAT({scalar_function}({date}), "0"))'
            )
        format_string = _DATENAME_FORMATS.get(part)
        if format_string is None:
            return _unsupported_conversion(
                'DATENAME part is unsupported without verified Tableau semantics')
        if date == 'BLANK()':
            return 'BLANK()'
        return f'IF(ISBLANK({date}), BLANK(), FORMAT({date}, {format_string}))'

    return _transform_date_func_call(dax, 'DATENAME', convert)


def _convert_corr_covar(dax, table_name='Table'):
    """Convert CORR/COVAR/COVARP to proper DAX using VAR/iterator patterns.

    CORR(x, y) → Pearson correlation using VAR + SUMX pattern
    COVAR(x, y) → sample covariance using VAR + SUMX pattern
    COVARP(x, y) → population covariance using VAR + SUMX pattern
    """
    for tab_func in ['CORR', 'COVARP', 'COVAR']:
        pattern = _get_func_pattern(tab_func)
        match = pattern.search(dax)
        while match:
            start_pos = match.end()
            depth = 1
            i = start_pos
            while i < len(dax) and depth > 0:
                if dax[i] == '(':
                    depth += 1
                elif dax[i] == ')':
                    depth -= 1
                i += 1
            if depth == 0:
                inner = dax[start_pos:i - 1]
                # Split arguments (x, y)
                args = _split_args(inner)
                if len(args) >= 2:
                    x_expr = args[0].strip()
                    y_expr = args[1].strip()
                    replacement = _build_corr_covar_dax(tab_func.upper(), x_expr, y_expr, table_name)
                else:
                    # Fallback if can't parse args
                    replacement = f'0 /* {tab_func}({inner}): could not parse arguments */'
                dax = dax[:match.start()] + replacement + dax[i:]
                match = pattern.search(dax, match.start() + len(replacement))
            else:
                break
    return dax


def _build_corr_covar_dax(func_name, x_expr, y_expr, table_name='Table'):
    """Build DAX expression for correlation/covariance using VAR/iterator pattern.

    Uses SUMX over ALL() rows with deviation-from-mean calculations.
    The table_name parameter specifies which table to iterate over.
    """
    tbl = table_name.replace("'", "''")
    if func_name == 'CORR':
        # Pearson correlation: Σ((x-μx)(y-μy)) / √(Σ(x-μx)² × Σ(y-μy)²)
        return (
            f"VAR _MeanX = AVERAGEX(ALL('{tbl}'), {x_expr}) "
            f"VAR _MeanY = AVERAGEX(ALL('{tbl}'), {y_expr}) "
            f"VAR _Cov = SUMX(ALL('{tbl}'), ({x_expr} - _MeanX) * ({y_expr} - _MeanY)) "
            f"VAR _StdX = SQRT(SUMX(ALL('{tbl}'), ({x_expr} - _MeanX) ^ 2)) "
            f"VAR _StdY = SQRT(SUMX(ALL('{tbl}'), ({y_expr} - _MeanY) ^ 2)) "
            f'RETURN DIVIDE(_Cov, _StdX * _StdY, 0)'
        )
    elif func_name == 'COVARP':
        # Population covariance: Σ((x-μx)(y-μy)) / N
        return (
            f"VAR _MeanX = AVERAGEX(ALL('{tbl}'), {x_expr}) "
            f"VAR _MeanY = AVERAGEX(ALL('{tbl}'), {y_expr}) "
            f"VAR _N = COUNTROWS(ALL('{tbl}')) "
            f"RETURN DIVIDE(SUMX(ALL('{tbl}'), ({x_expr} - _MeanX) * ({y_expr} - _MeanY)), _N, 0)"
        )
    else:  # COVAR — sample covariance
        # Sample covariance: Σ((x-μx)(y-μy)) / (N-1)
        return (
            f"VAR _MeanX = AVERAGEX(ALL('{tbl}'), {x_expr}) "
            f"VAR _MeanY = AVERAGEX(ALL('{tbl}'), {y_expr}) "
            f"VAR _N = COUNTROWS(ALL('{tbl}')) "
            f"RETURN DIVIDE(SUMX(ALL('{tbl}'), ({x_expr} - _MeanX) * ({y_expr} - _MeanY)), _N - 1, 0)"
        )


# ── Generic function-call transformer ─────────────────────────────────────────

def _transform_func_call(dax, func_name, transformer_fn):
    """Generic: find func_name(...), extract balanced args, apply transformer_fn.

    *transformer_fn* receives (args_list, raw_inner_str) and must return
    the replacement string.  The function handles nested parentheses and
    iterates until no more matches are found.
    """
    pattern = _get_func_pattern(func_name, word_boundary=True)
    match = pattern.search(dax)
    while match:
        start_pos = match.end()
        depth, i = 1, start_pos
        while i < len(dax) and depth > 0:
            if dax[i] == '(':
                depth += 1
            elif dax[i] == ')':
                depth -= 1
            i += 1
        if depth != 0:
            break
        inner = dax[start_pos:i - 1]
        args = _split_args(inner)
        replacement = transformer_fn(args, inner)
        dax = dax[:match.start()] + replacement + dax[i:]
        match = pattern.search(dax, match.start() + len(replacement))
    return dax


# ── Dedicated converters (using _transform_func_call) ─────────────────────────

def _convert_endswith(dax):
    """ENDSWITH(string, substring) → RIGHT(string, LEN(substring)) = substring"""
    def _xf(args, inner):
        if len(args) >= 2:
            return f'(RIGHT({args[0].strip()}, LEN({args[1].strip()})) = {args[1].strip()})'
        return f'ENDSWITH({inner})'
    return _transform_func_call(dax, 'ENDSWITH', _xf)


def _convert_left_right(dax):
    """Guard LEFT/RIGHT against missing text and negative character counts.

    Tableau returns NULL for missing text and does not raise on negative
    character counts. DAX LEFT/RIGHT can error for a negative count, so clamp
    the function argument and return BLANK for negative counts explicitly.
    """
    def _xf(args, inner, function_name):
        if len(args) < 2:
            return f'{function_name}({inner})'

        text = args[0].strip()
        length = args[1].strip()
        return (
            f'IF(ISBLANK({text}), BLANK(), '
            f'IF({length} < 0, BLANK(), '
            f'{function_name}({text}, IF({length} < 0, 0, {length}))))'
        )

    # The protected transformer also descends into nested calls of the same
    # function. It uses a collision-free token prefix and restores literals,
    # quoted table names and bracketed identifiers after rewriting.
    for function_name in ('LEFT', 'RIGHT'):
        dax = _transform_date_func_call(
            dax, function_name,
            lambda args, inner, restore, name=function_name: _xf(args, inner, name),
        )
    return dax


def _convert_startswith(dax):
    """STARTSWITH(string, substring) → LEFT(string, LEN(substring)) = substring"""
    def _xf(args, inner):
        if len(args) >= 2:
            return f'(LEFT({args[0].strip()}, LEN({args[1].strip()})) = {args[1].strip()})'
        return f'STARTSWITH({inner})'
    return _transform_func_call(dax, 'STARTSWITH', _xf)


def _convert_proper(dax):
    """PROPER(string) → word-by-word capitalisation via CONCATENATEX + UPPER/LOWER.

    Tableau PROPER("hello world") → "Hello World" (capitalizes first letter
    of every word).  DAX has no built-in PROPER function.  The approximation
    UPPER(LEFT()) & LOWER(MID()) only capitalizes position 1.

    The pattern below splits on spaces, capitalizes each token, and
    reassembles with space delimiters.  It works for single-space-separated
    words (the overwhelmingly common case).
    """
    comment_prefix = '__proper_comment_'
    while comment_prefix in dax:
        comment_prefix = '_' + comment_prefix
    comments = []
    protected_token_pattern = re.compile(
        _RE_NULL_PROTECTED_TOKEN.pattern + r'|/\*.*?\*/|//[^\r\n]*|--[^\r\n]*',
        re.DOTALL,
    )

    def protect_comment(match):
        token = match.group(0)
        if token.startswith(('/*', '//', '--')):
            comments.append(token)
            return f'{comment_prefix}{len(comments) - 1}__'
        return token

    dax = protected_token_pattern.sub(protect_comment, dax)

    def _xf(args, inner, restore):
        s = restore(inner.strip())
        # Split into words via GENERATESERIES + MID/FIND, capitalize each
        # DAX limitation: no native PROPER.  Use the simple first-char
        # approach but add a migration comment noting the limitation.
        return (
            f'UPPER(LEFT({s}, 1)) & LOWER(MID({s}, 2, LEN({s})))'
            f' /* Title case: only capitalizes first character; '
            f'review if multi-word capitalisation needed */'
        )

    dax = _transform_date_func_call(dax, 'PROPER', _xf)
    return re.sub(
        re.escape(comment_prefix) + r'(\d+)__',
        lambda match: comments[int(match.group(1))],
        dax,
    )


def _convert_split(dax):
    """SPLIT(string, delimiter, token_number) → best-effort PATHITEM expression.

    Negative token indexes count from the end using PATHITEMREVERSE. Power BI's
    PATHITEM uses ``|`` as its path separator, so values containing ``|`` may
    be split differently when Tableau's delimiter is another character.
    """
    def _xf(args, inner):
        if len(args) >= 2:
            s = args[0].strip()
            delim = args[1].strip()
            token = args[2].strip() if len(args) >= 3 else '1'
            reverse = False
            try:
                idx = int(token)
                if idx < 0:
                    token = str(-idx)
                    reverse = True
            except (ValueError, TypeError):
                pass

            path_item = 'PATHITEMREVERSE' if reverse else 'PATHITEM'
            direct_item = f'{path_item}({s}, {token})'
            substituted_item = f'{path_item}(SUBSTITUTE({s}, {delim}, "|"), {token})'
            is_string_literal = (
                len(delim) >= 2
                and delim[0] == delim[-1]
                and delim[0] in ('"', "'")
            )

            if is_string_literal and delim[1:-1] == '|':
                result = direct_item
            else:
                if is_string_literal:
                    result = substituted_item
                else:
                    result = f'IF({delim} = "|", {direct_item}, {substituted_item})'
                result = (
                    '/* SPLIT best-effort approximation; values containing '
                    'the reserved | separator may be mis-split */ ' + result
                )
            return f'IF(ISBLANK({s}), BLANK(), {result})'
        return f'/* SPLIT({inner}): insufficient arguments */ BLANK()'
    return _transform_func_call(dax, 'SPLIT', _xf)


def _convert_ltrim(dax):
    """LTRIM(string) → MID-based left-trim that removes leading spaces only.

    DAX TRIM() removes both leading and trailing spaces.
    LTRIM should only remove leading spaces, preserving trailing ones.
    """
    def _xf(args, inner):
        s = inner.strip()
        return (
            f'IF(ISBLANK({s}), BLANK(), IF(LEN(TRIM({s})) = 0, "", '
            f'MID({s}, FIND(LEFT(TRIM({s}), 1), {s}), LEN({s}))))'
        )
    return _transform_func_call(dax, 'LTRIM', _xf)


def _convert_rtrim(dax, value_column_ref='[Value]'):
    """RTRIM(string) → LEFT-based right-trim that removes trailing spaces only.

    DAX TRIM() removes both leading and trailing spaces.
    RTRIM should only remove trailing spaces, preserving leading ones.
    """
    def _xf(args, inner):
        s = inner.strip()
        return (
            f'IF(ISBLANK({s}), BLANK(), IF(LEN(TRIM({s})) = 0, "", '
            f'LEFT({s}, MAXX(FILTER(GENERATESERIES(1, LEN({s})), '
            f'MID({s}, {value_column_ref}, 1) <> " "), '
            f'{value_column_ref}))))'
        )
    return _transform_func_call(dax, 'RTRIM', _xf)


def _convert_index(dax):
    """Replace INDEX() with a deliberately lossy compatibility constant.

    Tableau INDEX() returns the sequential row number in the mark partition.
    The converter does not have a proven mark identity, partition and ordering
    tuple here, so this constant must not be interpreted as converted index
    semantics. Keep the limitation explicit in the emitted comment.
    """
    pattern = re.compile(r'\bINDEX\s*\(\s*\)', re.IGNORECASE)
    return pattern.sub(
        '1 /* INDEX fallback: constant for visual compatibility */',
        dax
    )


def _convert_size(dax):
    """SIZE() requires verified Tableau mark grain and partition context."""
    masked, restore = _protect_source_tokens(dax)
    pattern = re.compile(r'\bSIZE\s*\(\s*\)', re.IGNORECASE)
    return restore(pattern.sub(
        lambda _: _unsupported_conversion(
            'SIZE requires verified Tableau mark grain and partition context'),
        masked,
    ))


def _convert_reverse(dax):
    """REVERSE(string) → iterative MID concatenation pattern.

    DAX has no native REVERSE.  Emits a CONCATENATEX + GENERATESERIES pattern.
    """
    def _xf(args, inner):
        s = inner.strip()
        return (
            f'CONCATENATEX('
            f'GENERATESERIES(1, LEN({s})), '
            f'MID({s}, LEN({s}) - [Value] + 1, 1), '
            f'"")'
        )
    return _transform_func_call(dax, 'REVERSE', _xf)


def _convert_atan2(dax):
    """ATAN2(y, x) → quadrant-aware ATAN using IF/SIGN."""
    def _xf(args, inner):
        if len(args) >= 2:
            y = args[0].strip()
            x = args[1].strip()
            return (
                f'VAR __y = {y} '
                f'VAR __x = {x} '
                f'RETURN IF(__x > 0, ATAN(__y / __x), '
                f'IF(__x < 0 && __y >= 0, ATAN(__y / __x) + PI(), '
                f'IF(__x < 0 && __y < 0, ATAN(__y / __x) - PI(), '
                f'IF(__x = 0 && __y > 0, PI() / 2, '
                f'IF(__x = 0 && __y < 0, -PI() / 2, BLANK())))))'
            )
        return f'ATAN2({inner})'
    return _transform_func_call(dax, 'ATAN2', _xf)


def _convert_div(dax):
    """DIV(integer1, integer2) → QUOTIENT(integer1, integer2)"""
    return _transform_func_call(dax, 'DIV', lambda args, inner: f'QUOTIENT({inner})')


def _convert_square(dax):
    """SQUARE(number) → POWER(number, 2)"""
    return _transform_func_call(dax, 'SQUARE', lambda args, inner: f'POWER({inner.strip()}, 2)')


# Tableau→DAX date format token mapping
_DATE_FORMAT_MAP = {
    'yyyy': 'YYYY', 'yy': 'YY',
    'MMMM': 'MMMM', 'MMM': 'MMM', 'MM': 'MM', 'M': 'M',
    'dd': 'DD', 'd': 'D',
    'HH': 'HH', 'hh': 'HH', 'mm': 'NN', 'ss': 'SS',
    'EEEE': 'DDDD', 'EEE': 'DDD',
}

def _convert_unsupported_functions(dax):
    """Replace unsupported Tableau calls with a valid diagnostic expression."""

    def fallback(args, inner, restore):
        return _unsupported_conversion(
            'No DAX equivalent; manual conversion is required')

    for function_name in (
            'MAKEPOINT', 'MAKELINE', 'DISTANCE', 'BUFFER', 'AREA',
            'INTERSECTION', 'HEXBINX', 'HEXBINY', 'SCRIPT_BOOL',
            'SCRIPT_INT', 'SCRIPT_REAL', 'SCRIPT_STR', 'COLLECT',
            'USERATTRIBUTE', 'USERATTRIBUTEINCLUDES', 'ISUSERNAME',
            'FIRST', 'LAST'):
        dax = _transform_date_func_call(dax, function_name, fallback)
    return dax


def _transform_date_func_call(dax, func_name, transformer_fn):
    """Transform calls, including nested calls, but never quoted tokens.

    The generic call/argument helpers count delimiters in raw text.  Mask
    literals and identifiers here so embedded commas, parentheses and function
    names remain data, without changing the other conversion paths.  Used by
    date conversions and the LEFT/RIGHT guards.
    """
    prefix = '__date_token_'
    while prefix in dax:
        prefix = '_' + prefix
    tokens = []

    def protect(match):
        tokens.append(match.group(0))
        return f'{prefix}{len(tokens) - 1}__'

    masked = _RE_NULL_PROTECTED_TOKEN.sub(protect, dax)

    def restore(text):
        return re.sub(
            re.escape(prefix) + r'(\d+)__',
            lambda match: tokens[int(match.group(1))],
            text,
        )

    pattern = _get_func_pattern(func_name)

    def rewrite(text):
        match = pattern.search(text)
        while match:
            depth, end = 1, match.end()
            while end < len(text) and depth:
                if text[end] == '(':
                    depth += 1
                elif text[end] == ')':
                    depth -= 1
                end += 1
            if depth:
                break
            inner = rewrite(text[match.end():end - 1])
            replacement = transformer_fn(_split_args(inner), inner, restore)
            if replacement is None:
                # Preserve constructors verbatim, except any nested casts.
                replacement = text[match.start():match.end()] + inner + ')'
            text = text[:match.start()] + replacement + text[end:]
            match = pattern.search(text, match.start() + len(replacement))
        return text

    return restore(rewrite(masked))


def _convert_makedatetime(dax, *, table_name=None, table_columns=None,
                          column_table_map=None, measure_names=None,
                          nonphysical_names=(), is_calc_column=False):
    """Combine MAKEDATETIME arguments with scalar null propagation."""

    def convert(args, inner, restore):
        if len(args) != 2:
            return None
        date, time = (restore(arg).strip() for arg in args)
        if date.upper() == 'BLANK()' or time.upper() == 'BLANK()':
            return 'BLANK()'
        combined = f'({date} + {time})'
        return (f'IF(OR(ISBLANK({date}), ISBLANK({time})), '
                f'BLANK(), {combined})')

    return _transform_date_func_call(dax, 'MAKEDATETIME', convert)


def _convert_datetrunc(dax, *, table_name=None, table_columns=None,
                       date_columns=None, column_table_map=None,
                       measure_names=None, nonphysical_names=(),
                       is_calc_column=False):
    """Map DATETRUNC calls, scalarizing only proven local day columns."""
    startof_functions = {
        'year': 'STARTOFYEAR',
        'quarter': 'STARTOFQUARTER',
        'month': 'STARTOFMONTH',
    }
    scalar_truncations = {
        'year': lambda date: f'DATE(YEAR({date}), 1, 1)',
        'quarter': lambda date: (
            f'DATE(YEAR({date}), 3 * INT((MONTH({date}) - 1) / 3) + 1, 1)'),
        'month': lambda date: f'DATE(YEAR({date}), MONTH({date}), 1)',
    }
    number_literal = re.compile(
        r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z')
    scalar_clock = re.compile(r'(?:TODAY|NOW)\s*\(\s*\)\Z', re.IGNORECASE)
    physical_columns = (table_columns if isinstance(
        table_columns, (list, tuple, set, frozenset)) else ())
    proven_dates = (date_columns if isinstance(
        date_columns, (list, tuple, set, frozenset)) else ())
    measure_names = measure_names or ()
    nonphysical_names = set(nonphysical_names)

    def convert(args, inner, restore):
        if len(args) != 2:
            return _unsupported_conversion(
                'DATETRUNC requires a supported granularity and date expression')
        granularity = restore(args[0]).strip().strip("'\"").lower()
        function = startof_functions.get(granularity)
        date_expression = args[1].strip()
        makedate_calls = _extract_balanced_call(date_expression, 'MAKEDATE')
        if makedate_calls:
            start, end, makedate_inner = makedate_calls[0]
            makedate_args = _split_args(makedate_inner)
            is_direct_call = not date_expression[:start].strip() and not date_expression[end:].strip()
            if (not is_direct_call or len(makedate_args) != 3
                    or not all(number_literal.fullmatch(arg.strip())
                               for arg in makedate_args)):
                return _unsupported_conversion(
                    'DATETRUNC over MAKEDATE requires three numeric literal arguments')
            scalar_function = scalar_truncations.get(granularity)
            if scalar_function:
                date = f"DATE({', '.join(arg.strip() for arg in makedate_args)})"
                return scalar_function(date)

        scalar_function = scalar_truncations.get(granularity)
        if scalar_function and scalar_clock.fullmatch(date_expression):
            return scalar_function(date_expression)

        if function:
            return f'{function}({date_expression})'
        if granularity == 'day':
            try:
                reference = _ungroup_source(
                    _SourceExpressionParser(
                        restore(date_expression).strip()).parse())
            except (_SourceExpressionError, RecursionError):
                reference = None
            if (is_calc_column and reference is not None
                    and reference[0] == 'ref'):
                owner, name = reference[2], reference[3]
                if (owner in (None, table_name)
                        and isinstance(table_columns, (list, tuple, set, frozenset))
                        and isinstance(date_columns, (list, tuple, set, frozenset))
                        and name in physical_columns and name in proven_dates
                        and name not in measure_names
                        and name not in nonphysical_names
                        and isinstance(column_table_map, dict)
                        and column_table_map.get(name) == table_name):
                    date = restore(date_expression).strip()
                    return (
                        f'IF(ISBLANK({date}), BLANK(), '
                        f'DATE(YEAR({date}), MONTH({date}), DAY({date})))')
            return _unsupported_conversion(
                'day truncation requires a proven local physical date column in calculated-column context')
        return _unsupported_conversion(
            'requested truncation granularity is unsupported without an explicit policy')

    return _transform_date_func_call(dax, 'DATETRUNC', convert)


def _convert_datepart_quarter(dax):
    """Convert DATEPART('quarter', date) to a null-preserving scalar expression."""
    def convert(args, inner, restore):
        if not args:
            return _unsupported_conversion(
                'DATEPART requires exactly two arguments')
        part = restore(args[0]).strip().strip("'\"").lower()
        if part != 'quarter':
            return None
        if len(args) != 2:
            return _unsupported_conversion(
                'DATEPART quarter requires exactly two arguments')
        date = restore(args[1]).strip()
        return (
            f'IF(ISBLANK({date}), BLANK(), '
            f'INT((MONTH({date}) - 1) / 3) + 1)'
        )

    return _transform_date_func_call(dax, 'DATEPART', convert)


def _convert_datepart_direct_units(dax):
    """Convert direct DATEPART units while preserving null date values."""
    functions = {
        'year': 'YEAR',
        'month': 'MONTH',
        'day': 'DAY',
        'hour': 'HOUR',
        'minute': 'MINUTE',
        'second': 'SECOND',
    }

    def convert(args, inner, restore):
        if not args:
            return _unsupported_conversion(
                'DATEPART requires exactly two arguments')
        part = restore(args[0]).strip().strip('\'\"').lower()
        if part in ('quarter', 'week'):
            return None
        function = functions.get(part)
        if function is None:
            if part == 'weekday' and len(args) == 2:
                return _unsupported_conversion(
                    'day-of-week numbering depends on unverified week-start semantics')
            if len(args) != 2:
                return _unsupported_conversion(
                    'DATEPART requires exactly two arguments')
            return _unsupported_conversion(
                'requested date part is unsupported without verified semantics')
        if len(args) != 2:
            return _unsupported_conversion(
                'DATEPART direct unit requires exactly two arguments')
        date = restore(args[1]).strip()
        return f'IF(ISBLANK({date}), BLANK(), {function}({date}))'

    return _transform_date_func_call(dax, 'DATEPART', convert)


def _convert_datepart_week(dax):
    """Reject DATEPART week calls without verified Tableau week metadata."""
    def convert(args, inner, restore):
        if not args:
            return _unsupported_conversion(
                'DATEPART requires exactly two arguments')
        part = restore(args[0]).strip().strip("'\"").lower()
        if part != 'week':
            return None
        if len(args) in (2, 3):
            return _unsupported_conversion(
                'Tableau week-start and numbering semantics require verified metadata')
        return _unsupported_conversion(
            'DATEPART week requires exactly two or three arguments')

    return _transform_date_func_call(dax, 'DATEPART', convert)


def _convert_startof_scalar_extrema(dax, *, table_name, table_columns,
                                    date_columns, column_table_map,
                                    measure_names, nonphysical_names):
    """Lower only proven local physical MIN/MAX date inputs to scalar dates."""
    aggregates = {'SUM', 'AVERAGE', 'MIN', 'MAX', 'COUNT', 'COUNTA',
                  'COUNTBLANK', 'DISTINCTCOUNT', 'MEDIAN', 'STDEV', 'STDEV.S',
                  'STDEV.P', 'VAR', 'VAR.S', 'VAR.P'}
    aggregate_pattern = re.compile(
        r'\b(?:' + '|'.join(re.escape(name) for name in aggregates) + r')\s*\(',
        re.IGNORECASE,
    )
    granularities = {
        'STARTOFYEAR': lambda value: f'DATE(YEAR({value}), 1, 1)',
        'STARTOFMONTH': lambda value: f'DATE(YEAR({value}), MONTH({value}), 1)',
        'STARTOFQUARTER': lambda value: (
            f'DATE(YEAR({value}), 3 * INT((MONTH({value}) - 1) / 3) + 1, 1)'),
    }
    physical_columns = (table_columns if isinstance(
        table_columns, (list, tuple, set, frozenset)) else ())
    proven_dates = (date_columns if isinstance(
        date_columns, (list, tuple, set, frozenset)) else ())

    def walk(node):
        if node[0] == 'call':
            return ([node] if node[1].upper() in aggregates else []) + [
                aggregate for arg in node[2] for aggregate in walk(arg)]
        if node[0] == 'op':
            return walk(node[2]) + walk(node[3])
        if node[0] in ('unary', 'group'):
            return walk(node[-1])
        return []

    issue = None

    def convert_startof(args, inner, restore, function_name):
        nonlocal issue
        expression = restore(inner).strip()
        try:
            node = _ungroup_source(_SourceExpressionParser(expression).parse())
            found_aggregates = walk(node)
        except (_SourceExpressionError, RecursionError):
            found_aggregates = aggregate_pattern.findall(inner)
            node = None
        if not found_aggregates:
            return None

        if (node is None or node[0] != 'call'
                or node[1].upper() not in ('MIN', 'MAX')
                or len(node[2]) != 1 or len(found_aggregates) != 1):
            issue = 'DATETRUNC scalar aggregate requires verified local date-column grain'
            return None

        reference = _ungroup_source(node[2][0])
        if reference[0] != 'ref':
            issue = 'DATETRUNC scalar aggregate requires verified local date-column grain'
            return None
        spans = list(_dax_qualified_column_spans(reference[1]))
        if len(spans) != 1 or spans[0][0] != 0 or spans[0][1] != len(reference[1]):
            issue = 'DATETRUNC scalar aggregate requires verified local date-column grain'
            return None
        _, _, owner, name = spans[0]
        if (not isinstance(table_columns, (list, tuple, set, frozenset))
                or not isinstance(date_columns, (list, tuple, set, frozenset))
                or any(not isinstance(value, str) or not value
                       for value in date_columns)
                or name not in physical_columns or name not in proven_dates
                or name in measure_names or name in nonphysical_names
                or owner != table_name
                or column_table_map.get(name, table_name) != table_name):
            issue = 'DATETRUNC scalar aggregate requires verified local date-column grain'
            return None

        variable = '__TableauDateExtremum'
        suffix = 0
        while re.search(r'\b' + re.escape(variable) + r'\b', dax, re.IGNORECASE):
            suffix += 1
            variable = f'__TableauDateExtremum{suffix}'
        aggregate = f'{node[1].upper()}({reference[1]})'
        return (f'VAR {variable} = {aggregate} RETURN '
                f'IF(ISBLANK({variable}), BLANK(), '
                f'{granularities[function_name](variable)})')

    for function_name in granularities:
        dax = _transform_date_func_call(
            dax, function_name,
            lambda args, inner, restore, name=function_name:
            convert_startof(args, inner, restore, name),
        )
        if issue:
            return dax, issue
    return dax, None


def _convert_date_casts(dax):
    """Convert one-argument Tableau casts, not DAX DATE(y, m, d).

    CONVERT accepts text, numeric serials and already-typed dates.  DATE
    discards the time fraction; the final conversion keeps the result typed
    as datetime rather than leaving the integer serial returned by INT.
    DATETIME retains the time component.
    """
    def date_cast(args, inner, restore):
        if len(args) == 3:
            return None
        if len(args) == 1 and args[0].strip():
            return f'CONVERT(INT(CONVERT({args[0]}, DATETIME)), DATETIME)'
        return _unsupported_conversion(
            'DATE cast requires one argument; three arguments denote a DAX date constructor')

    def datetime_cast(args, inner, restore):
        if len(args) == 1 and args[0].strip():
            return f'CONVERT({args[0]}, DATETIME)'
        return _unsupported_conversion(
            'DATETIME cast requires exactly one argument')

    dax = _transform_date_func_call(dax, 'DATE', date_cast)
    return _transform_date_func_call(dax, 'DATETIME', datetime_cast)


def _convert_dateparse(dax):
    """Convert only DATEPARSE formats with locale-invariant semantics.

    The format is a parsing contract, not an output format. Dropping an
    ambiguous day/month pattern and calling DATEVALUE would silently delegate
    parsing to the model locale. Keep only the year-only and year-first forms
    with an explicit mapping; diagnose other formats rather than guessing.
    """
    def _xf(args, inner, restore):
        if len(args) != 2:
            return _unsupported_conversion(
                'DATEPARSE requires exactly two arguments')
        format_token = restore(args[0]).strip()
        expr = restore(args[1]).strip()
        if format_token in ('"yyyy"', "'yyyy'"):
            return f'DATE(VALUE({expr}), 1, 1)'
        if format_token in (
                '"yyyy-MM-dd"', "'yyyy-MM-dd'",
                '"%Y-%m-%d"', "'%Y-%m-%d'"):
            return f'DATEVALUE({expr})'
        return _unsupported_conversion(
            'DATEPARSE requires a supported locale-invariant format')
    return _transform_date_func_call(dax, 'DATEPARSE', _xf)


def _convert_isdate(dax):
    """ISDATE(string) → NOT(ISERROR(DATEVALUE(string)))"""
    return _transform_func_call(dax, 'ISDATE', lambda args, inner: f'NOT(ISERROR(DATEVALUE({inner.strip()})))')


def _convert_attr(dax, measure_names=None):
    """Convert ATTR(x) → SELECTEDVALUE(x) for columns, or just x for measures.

    Tableau ATTR() returns a single value when the context has exactly one distinct
    value.  For columns, DAX SELECTEDVALUE() is the equivalent.  But when the argument
    is a measure (already scalar), wrapping it in SELECTEDVALUE is invalid — simply
    reference the measure directly.

    Uses _transform_func_call for balanced-paren extraction so nested calls
    like ATTR(UPPER([Name])) are handled correctly.
    """
    measure_names = measure_names or set()

    def _xf(args, inner):
        stripped = inner.strip()
        # Extract field name from brackets: [FieldName]
        field_match = re.match(r'^\[([^\]]+)\]$', stripped)
        if field_match:
            field_name = field_match.group(1)
            if field_name in measure_names:
                # ATTR of a measure → just the measure reference (already scalar)
                return stripped
        # Column reference → SELECTEDVALUE
        return f'SELECTEDVALUE({stripped})'

    return _transform_func_call(dax, 'ATTR', _xf)


def _convert_iif(dax):
    """Preserve IIF's separate unknown branch, including its implicit NULL."""
    prefix = '__TableauIifCondition'
    while prefix.casefold() in dax.casefold():
        prefix = '_' + prefix
    counter = 0

    def _xf(args, inner):
        nonlocal counter
        if len(args) in (3, 4):
            var = f'{prefix}{counter}'
            counter += 1
            unknown = args[3].strip() if len(args) == 4 else 'BLANK()'
            return (f'(VAR {var} = {args[0].strip()} RETURN '
                    f'IF(ISBLANK({var}), {unknown}, '
                    f'IF({var}, {args[1].strip()}, {args[2].strip()})))')
        return f'IIF({inner})'
    return _transform_func_call(dax, 'IIF', _xf)


def _char_class_to_code_check(char_class_body, field_expr, code_function='CODE'):
    """Convert a regex character class body to a DAX code-point check.

    *field_expr* should be a single-character expression like ``MID(field, i, 1)``.
    Returns a DAX boolean expression using ``||`` and ``&&`` operators (not
    ``OR()``/``AND()`` functions, which would be mangled by Phase 4 operator
    conversion), or ``None`` if the class is too complex.
    """
    parts = []
    i = 0
    while i < len(char_class_body):
        if i + 2 < len(char_class_body) and char_class_body[i + 1] == '-':
            # Range like a-z
            lo = char_class_body[i]
            hi = char_class_body[i + 2]
            if ord(lo) > ord(hi):
                return None
            parts.append(
                f'({code_function}({field_expr}) >= {ord(lo)} && '
                f'{code_function}({field_expr}) <= {ord(hi)})')
            i += 3
        else:
            ch = char_class_body[i]
            parts.append(f'({code_function}({field_expr}) = {ord(ch)})')
            i += 1
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return '(' + ' || '.join(parts) + ')'


def _convert_regexp_match(dax, value_column_ref='[Value]'):
    """Convert REGEXP_MATCH(field, "pattern") to DAX equivalents.

    Smart conversion for common regex patterns:
    - ^literal$ → exact match: field = "literal"
    - ^literal  → LEFT(field, len) = "literal"
    - literal$  → RIGHT(field, len) = "literal"
    - .+ / .* / ^.*$ → always TRUE (match anything / non-empty)
    - pat1|pat2 → CONTAINSSTRING(field, "pat1") || CONTAINSSTRING(field, "pat2")
    - simple literal (no metacharacters) → CONTAINSSTRING(field, "literal")
    - Anchored ASCII character classes → per-character Unicode checks
    - Unanchored ASCII character classes → any-character Unicode checks
    - \\d / \\d+ → digit detection via CODE ranges
    - complex patterns → CONTAINSSTRING fallback with warning comment
    """
    _REGEX_META = set(r'.+?*[](){}\^$')

    def _is_simple_literal(pattern):
        """Return True if *pattern* has no regex metacharacters."""
        return not any(ch in _REGEX_META for ch in pattern)

    def _xf(args, inner):
        if len(args) < 2:
            return f'CONTAINSSTRING({inner})'
        field = args[0].strip()

        def _blank_guard(expression):
            return f'IF(ISBLANK({field}), BLANK(), {expression})'

        raw_pat = args[1].strip()
        # Strip surrounding quotes
        if (raw_pat.startswith('"') and raw_pat.endswith('"')) or \
           (raw_pat.startswith("'") and raw_pat.endswith("'")):
            pat = raw_pat[1:-1]
        else:
            pat = raw_pat

        # Normalize common regex shorthands to bracket notation
        pat = pat.replace('\\d', '[0-9]').replace('\\w', '[a-zA-Z0-9_]').replace('\\s', '[ \\t]')

        # .+ / .* / ^.*$ / ^.+$ → always TRUE (matches any non-empty / any string)
        if pat in ('.+', '.*', '^.*$', '^.+$'):
            if pat in ('.+', '^.+$'):
                result = f'(LEN({field}) > 0)'
            else:
                result = 'TRUE()'
            return f'IF(ISBLANK({field}), BLANK(), {result})'

        # ^literal$ → exact match
        if pat.startswith('^') and pat.endswith('$') and len(pat) > 2:
            body = pat[1:-1]
            if _is_simple_literal(body):
                return _blank_guard(f'({field} = "{body}")')

        # ^literal  → LEFT match
        if pat.startswith('^'):
            body = pat[1:]
            if _is_simple_literal(body):
                return f'(LEFT({field}, {len(body)}) = "{body}")'

        # literal$  → RIGHT match
        if pat.endswith('$') and (len(pat) < 2 or pat[-2] != '\\'):
            body = pat[:-1]
            if _is_simple_literal(body):
                return f'(RIGHT({field}, {len(body)}) = "{body}")'

        # Exact ASCII character classes can be checked character by character.
        # DAX string search is case-insensitive, so use Unicode code points to
        # preserve the case-sensitive range semantics of regular expressions.
        full_class = re.fullmatch(r'\^\[([a-zA-Z0-9_-]+)\]([+*])\$', pat)
        if full_class:
            class_body, quantifier = full_class.groups()
            prefix = '__TableauRegex'
            while prefix.casefold() in dax.casefold():
                prefix = '_' + prefix
            text_var = prefix + 'Text'
            length_var = prefix + 'Length'
            char_check = _char_class_to_code_check(
                class_body, f'MID({text_var}, {value_column_ref}, 1)',
                code_function='UNICODE')
            if char_check:
                empty_result = 'FALSE()' if quantifier == '+' else 'TRUE()'
                nonempty_check = (
                    f'COUNTROWS(FILTER(GENERATESERIES(1, {length_var}), '
                    f'NOT({char_check}))) = 0')
                return (
                    f'(VAR {text_var} = {field} VAR {length_var} = LEN({text_var}) '
                    f'RETURN IF(ISBLANK({text_var}), BLANK(), '
                    f'IF({length_var} = 0, {empty_result}, {nonempty_check})))'
                )

        # [0-9] / \\d → contains any digit
        if pat in ('[0-9]', '\\d'):
            digit_checks = ' || '.join(
                f'CONTAINSSTRING({field}, "{d}")' for d in '0123456789'
            )
            return _blank_guard(f'({digit_checks})')

        contains_class = re.fullmatch(r'\[([a-zA-Z0-9_-]+)\]', pat)
        if contains_class:
            prefix = '__TableauRegex'
            while prefix.casefold() in dax.casefold():
                prefix = '_' + prefix
            text_var = prefix + 'Text'
            length_var = prefix + 'Length'
            char_check = _char_class_to_code_check(
                contains_class.group(1), f'MID({text_var}, {value_column_ref}, 1)',
                code_function='UNICODE')
            if char_check:
                matching_char = (
                    f'COUNTROWS(FILTER(GENERATESERIES(1, {length_var}), '
                    f'{char_check})) > 0')
                return (
                    f'(VAR {text_var} = {field} VAR {length_var} = LEN({text_var}) '
                    f'RETURN IF(ISBLANK({text_var}), BLANK(), '
                    f'IF({length_var} = 0, FALSE(), {matching_char})))'
                )

        # pat1|pat2|...  → OR of CONTAINSSTRING (only if each branch is simple)
        if '|' in pat and not any(ch in pat for ch in r'.+?*[](){}\^$' if ch != '|'):
            branches = pat.split('|')
            parts = [f'CONTAINSSTRING({field}, "{b}")' for b in branches if b]
            if parts:
                return _blank_guard('(' + ' || '.join(parts) + ')')

        # Simple literal (no metacharacters at all)
        if _is_simple_literal(pat):
            return _blank_guard(f'CONTAINSSTRING({field}, "{pat}")')

        # Fallback — complex regex, keep CONTAINSSTRING with comment
        return _blank_guard(
            f'/* REGEXP_MATCH: complex regex "{pat}" — verify manually */ '
            f'CONTAINSSTRING({field}, "{pat}")')

    return _transform_func_call(dax, 'REGEXP_MATCH', _xf)


def _convert_regexp_extract(dax):
    """Convert REGEXP_EXTRACT(field, "pattern") to DAX equivalents.

    - Fixed-prefix capture: "prefix(.*)" → MID(field, SEARCH("prefix", field) + len, LEN(field))
    - Suffix capture: "(.*)suffix" → LEFT(field, SEARCH("suffix", field) - 1)
    - Prefix+suffix capture: "prefix(.*)suffix" → MID with calculated length
    - Digit extraction: "(\\d+)" → extract first numeric substring
    - Fallback → comment + BLANK()
    """
    def _xf(args, inner):
        if len(args) < 2:
            return '/* REGEXP_EXTRACT: no DAX regex — manual conversion needed */ BLANK()'
        field = args[0].strip()
        raw_pat = args[1].strip()
        # Strip surrounding quotes
        if (raw_pat.startswith('"') and raw_pat.endswith('"')) or \
           (raw_pat.startswith("'") and raw_pat.endswith("'")):
            pat = raw_pat[1:-1]
        else:
            pat = raw_pat

        # Normalize regex shorthands
        pat = pat.replace('\\d', '[0-9]').replace('\\w', '[a-zA-Z0-9_]')

        # Character class for literal prefix/suffix chars (broader set)
        _LIT = r'[A-Za-z0-9_=:;/&@#%!,.<>\-\[\]{}|~\' ]+'

        # Fixed-prefix capture: "prefix(.*)"  or  "prefix(.*?)" or "prefix(.+)"
        m = re.match(r'^(' + _LIT + r')\(\.(\*|\+)\??\)$', pat)
        if m:
            prefix = m.group(1)
            prefix_len = len(prefix)
            return f'MID({field}, SEARCH("{prefix}", {field}) + {prefix_len}, LEN({field}))'

        # Suffix capture: "(.*)" + suffix or "(.*?)suffix"
        m = re.match(r'^\(\.\*\??\)(' + _LIT + r')$', pat)
        if m:
            suffix = m.group(1)
            return f'LEFT({field}, SEARCH("{suffix}", {field}) - 1)'

        # Prefix + suffix capture: "prefix(.*)suffix"
        m = re.match(r'^(' + _LIT + r')\(\.\*\??\)(' + _LIT + r')$', pat)
        if m:
            prefix = m.group(1)
            suffix = m.group(2)
            prefix_len = len(prefix)
            return (
                f'MID({field}, '
                f'SEARCH("{prefix}", {field}) + {prefix_len}, '
                f'SEARCH("{suffix}", {field}) - SEARCH("{prefix}", {field}) - {prefix_len})'
            )

        # Digit extraction: "([0-9]+)"
        if pat in ('([0-9]+)',):
            return (
                f'/* REGEXP_EXTRACT("\\d+"): extract first number */ '
                f'MID({field}, '
                f'MIN(IFERROR(FIND("0",{field}),999), IFERROR(FIND("1",{field}),999), '
                f'IFERROR(FIND("2",{field}),999), IFERROR(FIND("3",{field}),999), '
                f'IFERROR(FIND("4",{field}),999), IFERROR(FIND("5",{field}),999), '
                f'IFERROR(FIND("6",{field}),999), IFERROR(FIND("7",{field}),999), '
                f'IFERROR(FIND("8",{field}),999), IFERROR(FIND("9",{field}),999)), '
                f'LEN({field}))'
            )

        # Fallback
        return f'/* REGEXP_EXTRACT("{pat}"): no DAX regex — manual conversion needed */ BLANK()'

    return _transform_func_call(dax, 'REGEXP_EXTRACT', _xf)


def _convert_regexp_extract_nth(dax):
    """Convert REGEXP_EXTRACT_NTH(field, "pattern", index) to DAX equivalents.

    Tableau REGEXP_EXTRACT_NTH extracts the Nth capture group from a regex match.
    Since DAX has no regex support, we convert common patterns:

    - Delimiter-based: "([^-]*)" with delimiter char → PATHITEM(SUBSTITUTE(field, delim, "|"), index)
    - Fixed-prefix capture: "prefix(.*)suffix" → MID(field, SEARCH("prefix",...)+len, ...)
    - Simple alternation capture: "(a|b|c)" → IF chain
    - Fallback → BLANK() with migration comment
    """
    _REGEX_META = set(r'.+?*[](){}\^$')

    def _is_simple_literal(pattern):
        return not any(ch in _REGEX_META for ch in pattern)

    def _xf(args, inner):
        if len(args) < 3:
            if len(args) == 2:
                # REGEXP_EXTRACT_NTH(field, pattern) — assume index 1
                field = args[0].strip()
                raw_pat = args[1].strip()
                index_str = '1'
            else:
                return f'/* REGEXP_EXTRACT_NTH({inner}): insufficient arguments */ BLANK()'
        else:
            field = args[0].strip()
            raw_pat = args[1].strip()
            index_str = args[2].strip()

        # Strip surrounding quotes from pattern
        if (raw_pat.startswith('"') and raw_pat.endswith('"')) or \
           (raw_pat.startswith("'") and raw_pat.endswith("'")):
            pat = raw_pat[1:-1]
        else:
            pat = raw_pat

        # --- Delimiter-based extraction ---
        # Pattern like ([^X]*) where X is a delimiter character
        # Common: REGEXP_EXTRACT_NTH("a-b-c", "([^-]*)", 2) → 2nd token split by "-"
        delim_match = re.match(r'^\(\[\^([^\]]+)\]\*\)$', pat)
        if delim_match:
            delim_char = delim_match.group(1)
            if len(delim_char) == 1:
                return f'PATHITEM(SUBSTITUTE({field}, "{delim_char}", "|"), {index_str})'
            # Multi-char delimiter class — use first char as approximation
            return f'/* REGEXP_EXTRACT_NTH: delimiter class [{delim_char}] approximated */ PATHITEM(SUBSTITUTE({field}, "{delim_char[0]}", "|"), {index_str})'

        # --- Multiple capture groups with literal separators ---
        # Pattern like "^(\\w+)\\s*-\\s*(\\w+)$" or similar with literal separators
        # Simplified: detect delimiter between capture groups
        multi_group = re.match(r'^\(?\\w[\+\*]?\)?([^()\\\w]+)\(?\\w[\+\*]?\)?$', pat)
        if multi_group:
            delim = multi_group.group(1).strip().replace('\\s*', '').replace('\\s+', ' ')
            if delim:
                return f'PATHITEM(SUBSTITUTE({field}, "{delim}", "|"), {index_str})'

        # --- Fixed-prefix capture ---
        # Pattern like "prefix(.*)" → MID extraction
        prefix_match = re.match(r'^([A-Za-z0-9_=:;/&@#%!, -]+)\(\.\*\)$', pat)
        if prefix_match:
            prefix = prefix_match.group(1)
            prefix_len = len(prefix)
            return f'MID({field}, SEARCH("{prefix}", {field}) + {prefix_len}, LEN({field}))'

        # --- Simple alternation capture ---
        # Pattern like "(cat|dog|fish)" → just a CONTAINSSTRING check
        alt_match = re.match(r'^\(([A-Za-z0-9_|]+)\)$', pat)
        if alt_match:
            alternatives = alt_match.group(1).split('|')
            if len(alternatives) >= 2:
                # Build an IF chain that returns the first match
                result = 'BLANK()'
                for alt in reversed(alternatives):
                    result = f'IF(CONTAINSSTRING({field}, "{alt}"), "{alt}", {result})'
                return f'/* REGEXP_EXTRACT_NTH: alternation match */ {result}'

        # --- Fallback ---
        return f'/* REGEXP_EXTRACT_NTH("{pat}", {index_str}): no DAX regex \u2014 manual conversion needed */ BLANK()'

    return _transform_func_call(dax, 'REGEXP_EXTRACT_NTH', _xf)


def _convert_regexp_replace(dax):
    """Convert REGEXP_REPLACE(field, "pattern", "replacement") to DAX equivalents.

    Smart conversion for common regex patterns:
    - Simple literal pattern → SUBSTITUTE(field, "pattern", "replacement")
    - Character class [abc] → chained SUBSTITUTE for each character
    - Dot (.) + quantifier → comment + SUBSTITUTE fallback
    - Alternation (pat1|pat2) → nested SUBSTITUTE calls
    - Complex patterns → SUBSTITUTE with warning comment
    """
    _REGEX_META = set(r'.+?*[](){}\^$')

    def _is_simple_literal(pattern):
        """Return True if *pattern* has no regex metacharacters."""
        return not any(ch in _REGEX_META for ch in pattern)

    def _xf(args, inner):
        if len(args) < 3:
            # Fewer than 3 args — fall back to simple SUBSTITUTE
            return f'SUBSTITUTE({inner})'
        field = args[0].strip()
        raw_pat = args[1].strip()
        raw_repl = args[2].strip()

        # Strip surrounding quotes from pattern
        pat = raw_pat
        if (pat.startswith('"') and pat.endswith('"')) or \
           (pat.startswith("'") and pat.endswith("'")):
            pat = pat[1:-1]

        # Keep replacement as-is (with quotes)
        repl = raw_repl

        # A single optional character in the HTTP scheme expands to two
        # disjoint literal matches, so SUBSTITUTE preserves global replacement.
        if pat == 'https?://':
            https = f'SUBSTITUTE({field}, "https://", {repl})'
            return f'SUBSTITUTE({https}, "http://", {repl})'

        # Simple literal — direct SUBSTITUTE
        if _is_simple_literal(pat):
            return f'SUBSTITUTE({field}, "{pat}", {repl})'

        # Character class [abc] → chain of SUBSTITUTE calls
        import re as _re
        cc_match = _re.match(r'^\[([^\]]+)\]$', pat)
        if cc_match:
            chars = list(cc_match.group(1))
            result = field
            for ch in chars:
                result = f'SUBSTITUTE({result}, "{ch}", {repl})'
            return result

        # Alternation pat1|pat2 (no other metacharacters)
        if '|' in pat and not any(ch in pat for ch in r'.+?*[]()\^$' if ch != '|'):
            branches = [b for b in pat.split('|') if b]
            result = field
            for branch in branches:
                result = f'SUBSTITUTE({result}, "{branch}", {repl})'
            return result

        # Anchored patterns — ^literal or literal$
        if pat.startswith('^'):
            body = pat[1:]
            if _is_simple_literal(body):
                return f'IF(LEFT({field}, {len(body)}) = "{body}", {repl} & MID({field}, {len(body) + 1}, LEN({field})), {field})'

        if pat.endswith('$') and (len(pat) < 2 or pat[-2] != '\\'):
            body = pat[:-1]
            if _is_simple_literal(body):
                return f'IF(RIGHT({field}, {len(body)}) = "{body}", LEFT({field}, LEN({field}) - {len(body)}) & {repl}, {field})'

        # Fallback — complex regex
        return f'/* REGEXP_REPLACE("{pat}"): complex regex — verify manually */ SUBSTITUTE({field}, "{pat}", {repl})'

    return _transform_func_call(dax, 'REGEXP_REPLACE', _xf)


def _fix_ceiling_floor(dax):
    """Add significance=1 to CEILING/FLOOR with single argument."""
    for func in ['CEILING', 'FLOOR']:
        def _xf(args, inner, _f=func):
            return f'{_f}({inner.strip()}, 1)' if len(args) == 1 else f'{_f}({inner})'
        dax = _transform_func_call(dax, func, _xf)
    return dax


def _fix_startof_calc_columns(dax):
    """Convert STARTOFMONTH/QUARTER/YEAR → DATE() for calculated columns."""
    conversions = {
        'STARTOFYEAR': lambda col: f'DATE(YEAR({col}), 1, 1)',
        'STARTOFMONTH': lambda col: f'DATE(YEAR({col}), MONTH({col}), 1)',
        'STARTOFQUARTER': lambda col: f'DATE(YEAR({col}), 3 * INT((MONTH({col}) - 1) / 3) + 1, 1)',
    }
    for func_name, converter in conversions.items():
        dax = _transform_func_call(dax, func_name,
                                   lambda args, inner, _c=converter: _c(inner.strip()))
    return dax


def _fix_date_literals(dax):
    """Convert Tableau #YYYY-MM-DD# date literals to DAX DATE(Y, M, D)."""
    def _date_repl(m):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f'DATE({y}, {mo}, {d})'
    return _RE_DATE_LITERAL.sub(_date_repl, dax)


def _fix_round_single_arg(dax):
    """Add missing second argument to ROUND() calls.

    Tableau ``ROUND(x)`` rounds to 0 decimal places by default.
    DAX ``ROUND`` requires two arguments: ``ROUND(x, 0)``.
    """
    pattern = re.compile(r'\bROUND\s*\(', re.IGNORECASE)
    result = []
    last = 0
    for m in pattern.finditer(dax):
        paren_start = m.end() - 1  # position of '('
        depth = 1
        pos = paren_start + 1
        has_comma = False
        while pos < len(dax) and depth > 0:
            if dax[pos] == '(':
                depth += 1
            elif dax[pos] == ')':
                depth -= 1
            elif dax[pos] == ',' and depth == 1:
                has_comma = True
            pos += 1
        if depth == 0 and not has_comma:
            # Single-arg ROUND — insert ', 0' before closing paren
            close_pos = pos - 1
            result.append(dax[last:close_pos])
            result.append(', 0)')
            last = pos
    result.append(dax[last:])
    return ''.join(result)


# Regex to fix double-quoted table name followed by column ref: "Table"[Col]
_RE_DOUBLE_QUOTED_TABLE_COL = re.compile(r'"([^"]+)"\s*\[')
# DAX functions that take a table name as first argument
_TABLE_ARG_FUNCTIONS = (
    'ALL', 'ALLEXCEPT', 'ALLNOBLANKROW', 'ALLSELECTED', 'VALUES', 'DISTINCT',
    'FILTER', 'CALCULATETABLE', 'RELATEDTABLE', 'RELATED',
    'SUMX', 'AVERAGEX', 'MINX', 'MAXX', 'COUNTX', 'COUNTAX', 'RANKX',
    'TOPN', 'ADDCOLUMNS', 'SELECTCOLUMNS', 'SUMMARIZE', 'SUMMARIZECOLUMNS',
    'GENERATE', 'GENERATEALL', 'NATURALINNERJOIN', 'NATURALLEFTOUTERJOIN',
    'UNION', 'INTERSECT', 'EXCEPT', 'SAMPLE', 'DATATABLE',
)
_RE_FUNC_DOUBLE_QUOTED_TABLE = re.compile(
    r'\b(' + '|'.join(_TABLE_ARG_FUNCTIONS) + r')\s*\(\s*"([^"]+)"',
    re.IGNORECASE,
)


def _fix_double_quoted_table_refs(dax):
    """Fix double-quoted table names that should use single quotes in DAX.

    The ``_convert_single_quoted_strings`` function may incorrectly convert
    table name quotes from ``'Table Name'`` to ``"Table Name"`` in complex
    DAX (e.g. LOD-generated CALCULATE/FILTER patterns). This post-processing
    step restores the correct single-quote syntax for:

    - ``"Table"[Column]`` → ``'Table'[Column]``
    - ``FILTER("Table", ...)`` → ``FILTER('Table', ...)``
    """
    if '"' not in dax:
        return dax
    # Fix "Table"[Column] patterns
    dax = _RE_DOUBLE_QUOTED_TABLE_COL.sub(lambda m: "'" + m.group(1) + "'[", dax)
    # Fix FUNC("Table", ...) patterns
    dax = _RE_FUNC_DOUBLE_QUOTED_TABLE.sub(
        lambda m: m.group(1) + "('" + m.group(2) + "'", dax
    )
    return dax



def _convert_single_quoted_strings(dax):
    """Convert Tableau single-quoted string literals to DAX double-quoted.

    Tableau uses ``'text'`` for string literals.  In DAX, single quotes
    delimit table names (``'Table Name'[Col]`` or ``ALL('Table')``).

    We identify Tableau string literals by context: a single-quoted token
    that is NOT followed by ``[`` (column ref) and whose content is NOT
    a known table name appearing elsewhere in the expression.
    """
    # Collect all table names from 'name'[ patterns (handles '' escape)
    table_names = set()
    i = 0
    while i < len(dax):
        # Skip bracketed identifiers [Col name] — apostrophes/quotes inside
        # a column/measure name (e.g. [% ou Nombre d'appel]) are part of the
        # identifier and must never be parsed as string-literal delimiters.
        if dax[i] == '[':
            j = i + 1
            while j < len(dax):
                if dax[j] == ']' and j + 1 < len(dax) and dax[j + 1] == ']':
                    j += 2
                    continue
                if dax[j] == ']':
                    break
                j += 1
            i = j + 1
            continue
        if dax[i] == "'":
            j = i + 1
            while j < len(dax):
                if dax[j] == "'" and j + 1 < len(dax) and dax[j + 1] == "'":
                    j += 2
                    continue
                if dax[j] == "'":
                    break
                j += 1
            if j < len(dax) and j + 1 < len(dax) and dax[j + 1] == '[':
                table_names.add(dax[i + 1:j])
            i = j + 1
        else:
            i += 1

    # Also add table names that appear in DAX functions that take table arguments
    _TABLE_FN_RE = re.compile(
        r"(?:ALL|ALLEXCEPT|VALUES|RELATED|RELATEDTABLE|FILTER|CALCULATETABLE"
        r"|MINX|MAXX|SUMX|AVERAGEX|COUNTX|RANKX|TOPN|ADDCOLUMNS|SELECTCOLUMNS"
        r"|SUMMARIZE|GENERATE|GENERATEALL|NATURALLEFTOUTERJOIN|NATURALINNERJOIN"
        r"|UNION|INTERSECT|EXCEPT|DATATABLE|DISTINCT|SAMPLE)\s*\(\s*'((?:[^']|'')+)'",
        re.IGNORECASE
    )
    for m in _TABLE_FN_RE.finditer(dax):
        table_names.add(m.group(1))

    # If no table names found, any 'token' with spaces or mixed case is likely a table name
    # Only convert simple short tokens that look like Tableau string literals
    result = []
    i = 0
    while i < len(dax):
        ch = dax[i]
        # Skip bracketed identifiers [Col name] verbatim — apostrophes and
        # quotes inside a column/measure name are part of the identifier and
        # must not be treated as string-literal delimiters.
        if ch == '[':
            j = i + 1
            while j < len(dax):
                if dax[j] == ']' and j + 1 < len(dax) and dax[j + 1] == ']':
                    j += 2
                    continue
                if dax[j] == ']':
                    break
                j += 1
            result.append(dax[i:j + 1])
            i = j + 1
            continue
        # Skip double-quoted strings
        if ch == '"':
            j = i + 1
            while j < len(dax) and dax[j] != '"':
                j += 1
            result.append(dax[i:j + 1])
            i = j + 1
            continue
        # Handle single-quoted tokens (with '' escape support)
        if ch == "'":
            j = i + 1
            while j < len(dax):
                if dax[j] == "'" and j + 1 < len(dax) and dax[j + 1] == "'":
                    j += 2
                    continue
                if dax[j] == "'":
                    break
                j += 1
            if j < len(dax):
                content = dax[i + 1:j]
                after = dax[j + 1:j + 2]
                if after == '[' or content in table_names:
                    # Table reference — keep single quotes
                    result.append(dax[i:j + 1])
                elif len(content) > 2 and any(content in tn or tn.startswith(content) for tn in table_names):
                    # Partial match — likely a broken table ref with unescaped apostrophe
                    # Require >2 chars to avoid matching trivial substrings like ' ' or ','
                    result.append(dax[i:j + 1])
                else:
                    # String literal — convert to double quotes
                    str_content = content.replace("''", "'").replace('"', '""')
                    result.append('"' + str_content + '"')
                i = j + 1
                continue
        result.append(ch)
        i += 1
    return ''.join(result)


def _convert_string_concat(dax):
    """Convert Tableau + to DAX & for string concatenation.

    When the formula is known to be a string type, ALL ``+`` operators are
    converted to ``&`` EXCEPT those that are clearly arithmetic — detected
    when the ``+`` is immediately preceded or followed by a numeric literal
    (e.g. ``FIND(...) + 1`` or ``2 + LEN(...)``).  String-literal adjacency
    (a ``"..."`` token right before/after the ``+``) keeps the ``&``.
    """
    result = []
    in_string = False
    in_single_quote = False
    i = 0
    while i < len(dax):
        ch = dax[i]
        if in_string:
            result.append(ch)
            if ch == '"':
                in_string = False
            i += 1
            continue
        if in_single_quote:
            result.append(ch)
            if ch == "'":
                # Check for escaped '' inside table names
                if i + 1 < len(dax) and dax[i + 1] == "'":
                    result.append("'")
                    i += 2
                    continue
                in_single_quote = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            result.append(ch)
            i += 1
            continue
        if ch == "'":
            in_single_quote = True
            result.append(ch)
            i += 1
            continue
        if ch == '+':
            # Look at non-space tokens immediately before and after the +
            # to decide if this is arithmetic.
            left = ''.join(result).rstrip()
            right_part = dax[i + 1:].lstrip()
            numeric_before = bool(left) and (left[-1].isdigit() or left[-1] == '.')
            numeric_after = bool(right_part) and (right_part[0].isdigit() or right_part[0] == '.')
            if numeric_before or numeric_after:
                # Both sides are numeric literals → arithmetic
                result.append('+')
            else:
                result.append('&')
        else:
            result.append(ch)
        i += 1
    return ''.join(result)


def _dax_qualified_column_spans(text):
    """Find quoted DAX columns, excluding strings and comments."""
    i = 0
    while i < len(text):
        if text.startswith('//', i):
            end = text.find('\n', i + 2)
            i = len(text) if end < 0 else end
        elif text.startswith('/*', i):
            end = text.find('*/', i + 2)
            i = len(text) if end < 0 else end + 2
        elif text[i] == '"':
            i += 1
            while i < len(text):
                if text[i] == '"' and i + 1 < len(text) and text[i + 1] == '"':
                    i += 2
                elif text[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
        elif text[i] in ("'", '['):
            start = i
            close = "'" if text[i] == "'" else ']'
            i += 1
            while i < len(text):
                if text[i] == close and i + 1 < len(text) and text[i + 1] == close:
                    i += 2
                elif text[i] == close:
                    i += 1
                    break
                else:
                    i += 1
            else:
                return
            if close != "'" or i >= len(text) or text[i] != '[':
                continue
            table = text[start + 1:i - 1].replace("''", "'")
            i += 1
            col_start = i
            while i < len(text):
                if text[i] == ']' and i + 1 < len(text) and text[i + 1] == ']':
                    i += 2
                elif text[i] == ']':
                    yield start, i + 1, table, text[col_start:i].replace(']]', ']')
                    i += 1
                    break
                else:
                    i += 1
        else:
            i += 1


def _infer_iteration_table(inner_expr, default_table):
    """Infer a table only after the caller has established the row grain."""
    tables = [table for _, _, table, _ in _dax_qualified_column_spans(inner_expr)]
    if not tables:
        return default_table
    counts = {}
    for t in tables:
        counts[t] = counts.get(t, 0) + 1
    # When exactly 2 tables appear and one is the measure's own table,
    # prefer the OTHER table.  Measures typically aggregate rows from a
    # related fact table while referencing their own (dimension) table's
    # columns as conditions.  Iterating over the fact table and using
    # RELATED() for the dimension columns produces correct DAX.
    if len(counts) == 2 and default_table in counts:
        non_default = [t for t in counts if t != default_table]
        if non_default:
            return non_default[0]
    return max(counts, key=lambda k: counts[k])


def _wrap_cross_table_related(inner_expr, iter_table):
    """Wrap cross-table column references inside an AGGX body with RELATED().

    Inside ``SUMX('T', ...)``, references to ``'Other'[col]`` need
    ``RELATED('Other'[col])`` to navigate via the relationship.
    Already-wrapped references (``RELATED('Other'[col])``) are skipped.

    Refs that appear as **direct arguments** to DAX functions that require
    bare ``Table[Column]`` references (``ALLEXCEPT``, ``REMOVEFILTERS``,
    ``ALL``, ``VALUES``, ``DISTINCT``) are also skipped — wrapping them in
    ``RELATED()`` would produce invalid DAX.
    """
    # Functions whose arguments must be bare column refs (not RELATED-wrapped).
    _BARE_REF_FUNCS = ('ALLEXCEPT', 'REMOVEFILTERS', 'ALL', 'VALUES', 'DISTINCT')

    def _enclosing_func(text, pos):
        """Return the uppercase name of the innermost enclosing function call
        whose ``(`` is to the left of *pos*, or ``''`` if none.
        """
        depth = 0
        for i in range(pos - 1, -1, -1):
            ch = text[i]
            if ch == ')':
                depth += 1
            elif ch == '(':
                if depth > 0:
                    depth -= 1
                else:
                    # Found the unmatched '('. Extract the function name
                    # immediately before it.
                    j = i - 1
                    while j >= 0 and text[j].isspace():
                        j -= 1
                    end = j + 1
                    while j >= 0 and (text[j].isalnum() or text[j] in '_.'):
                        j -= 1
                    return text[j + 1:end].upper()
        return ''

    result = inner_expr
    for start, end, tbl, _ in reversed(list(_dax_qualified_column_spans(inner_expr))):
        if tbl == iter_table:
            continue
        # Check if already wrapped in RELATED
        prefix_start = max(0, start - 8)
        prefix = inner_expr[prefix_start:start].rstrip()
        if prefix.upper().endswith('RELATED('):
            continue
        # Skip if the ref is a direct argument to a function that requires
        # bare 'T'[col] refs (ALLEXCEPT, REMOVEFILTERS, ALL, ...).
        if _enclosing_func(inner_expr, start) in _BARE_REF_FUNCS:
            continue
        result = result[:start] + f"RELATED({inner_expr[start:end]})" + result[end:]
    return result


_BARE_COL_REF_RE = re.compile(
    r"^\s*'(?:[^']|'')+'\[(?:[^\]]|\]\])+\]\s*$"
)


def _is_bare_column_ref(ref):
    """Return True if `ref` is a simple ``'Table'[Column]`` reference.

    DAX filter-modifier functions like ``ALLEXCEPT`` and ``REMOVEFILTERS``
    require bare column references as args 2+. Complex expressions
    (function calls, IF/SWITCH, nested brackets, embedded commas/parens)
    must be rejected to keep the generated DAX valid.
    """
    if not ref:
        return False
    return bool(_BARE_COL_REF_RE.match(ref))


def _convert_lod_expressions(dax, table_name, column_table_map):
    """Convert legacy LOD forms; generation must not assume a FIXED grain."""
    masked, _ = _protect_source_tokens(dax)
    if re.search(r'\{\s*(?:INCLUDE|EXCLUDE)\b', masked, re.IGNORECASE):
        return _unsupported_conversion(
            'INCLUDE/EXCLUDE require verified source keys, shelf and target grain')
    if re.search(r'\{\s*FIXED\b', masked, re.IGNORECASE):
        return _unsupported_conversion(
            'FIXED requires verified source filter scope, dimension grain, '
            'shelf aggregation and total grain')
    if _aggregate_contains_lod(masked):
        return _unsupported_conversion(
            'aggregation of a level-of-detail expression requires its source '
            'shelf and total grain')

    def _resolve_dims(dims_str, default_table):
        masked_dims, restore_dims = _protect_source_tokens(dims_str)
        dims, refs = [], []
        for part in masked_dims.split(','):
            if not part.strip():
                continue
            node = _ungroup_source(_SourceExpressionParser(restore_dims(part)).parse())
            if node[0] != 'ref':
                raise _SourceExpressionError("LOD dimension is not a physical field")
            _, _, owner, name = node
            table = owner or column_table_map.get(name, default_table)
            dims.append(name)
            refs.append("'" + table.replace("'", "''") + "'["
                        + name.replace(']', ']]') + ']')
        return dims, refs

    def _convert_single_lod(keyword, dims_str, agg_str):
        """Convert one legacy LOD node without dropping complex dimensions."""
        try:
            _, dim_refs = _resolve_dims(dims_str, table_name)
        except (_SourceExpressionError, RecursionError):
            return _unsupported_conversion(
                'level-of-detail dimensions require physical field bindings')
        if keyword == 'FIXED':
            owners = {
                owner for ref in dim_refs
                for _, _, owner, _ in _dax_qualified_column_spans(ref)
            }
            if len(owners) > 1:
                return _unsupported_conversion(
                    'cross-table FIXED requires verified source filter scope, '
                    'dimension grain, shelf aggregation and total grain')
            logger.warning(
                'FIXED raw translation has unverified filter, shelf and total grain; '
                'semantic generation requires explicit evidence')
            if dim_refs:
                owner = next(iter(owners), table_name).replace("'", "''")
                return f"CALCULATE({agg_str}, ALLEXCEPT('{owner}', {', '.join(dim_refs)}))"
            owner = table_name.replace("'", "''")
            return f"CALCULATE({agg_str}, ALL('{owner}'))"
        if keyword in ('INCLUDE', 'EXCLUDE'):
            return _unsupported_conversion(
                f'{keyword} requires verified source keys, shelf and target grain')
        return agg_str  # fallback

    def _find_colon(text):
        """Find the first top-level ':' not inside braces or parens."""
        brace_depth = 0
        paren_depth = 0
        for i, ch in enumerate(text):
            if ch == '{':
                brace_depth += 1
            elif ch == '}':
                brace_depth -= 1
            elif ch == '(':
                paren_depth += 1
            elif ch == ')':
                paren_depth -= 1
            elif ch == ':' and brace_depth == 0 and paren_depth == 0:
                return i
        return None

    def _parse_lod_recursive(text, depth=0):
        """Recursively parse and convert LOD expressions in *text*.

        Scans left-to-right; when a ``{FIXED|INCLUDE|EXCLUDE`` token is found
        it recursively converts the aggregate body first (which may itself
        contain nested LODs) then converts the current LOD node.

        Returns the converted string with all LODs resolved.
        """
        if depth > 200:  # safety limit for pathological input
            return text

        result_parts = []
        i = 0
        while i < len(text):
            if text[i] == '{':
                after = text[i + 1:].lstrip()
                kw_match = re.match(r'(FIXED|INCLUDE|EXCLUDE)\b', after, re.IGNORECASE)
                if kw_match:
                    # Find the matching closing brace
                    brace_depth = 1
                    j = i + 1
                    while j < len(text) and brace_depth > 0:
                        if text[j] == '{':
                            brace_depth += 1
                        elif text[j] == '}':
                            brace_depth -= 1
                        j += 1
                    if brace_depth == 0:
                        inner = text[i + 1:j - 1]
                        kw = kw_match.group(1).upper()
                        rest = inner.lstrip()[len(kw):]
                        colon_pos = _find_colon(rest)
                        if colon_pos is not None:
                            dims_str = rest[:colon_pos].strip()
                            agg_str = rest[colon_pos + 1:].strip()
                            # Recurse into the aggregate body to resolve nested LODs
                            resolved_agg = _parse_lod_recursive(agg_str, depth + 1)
                            replacement = _convert_single_lod(kw, dims_str, resolved_agg)
                            result_parts.append(replacement)
                            i = j
                            continue
                # Not an LOD brace — emit it literally
                result_parts.append(text[i])
                i += 1
            else:
                result_parts.append(text[i])
                i += 1
        return ''.join(result_parts)

    dax = _parse_lod_recursive(dax)

    # LOD without dimension — use balanced brace matching (not global replace)
    match = _RE_LOD_NO_DIM.search(dax)
    while match:
        # Find the matching closing brace for this LOD expression
        start = match.start()
        depth = 1
        i = start + 1
        while i < len(dax) and depth > 0:
            if dax[i] == '{':
                depth += 1
            elif dax[i] == '}':
                depth -= 1
            i += 1
        if depth == 0:
            # Extract inner content (between { and })
            inner = dax[start + 1:i - 1].strip()
            # Convert to CALCULATE(inner)
            replacement = f'CALCULATE({inner})'
            dax = dax[:start] + replacement + dax[i:]
            match = _RE_LOD_NO_DIM.search(dax, start + len(replacement))
        else:
            break

    return dax


def _build_window_clauses(compute_using, table_name, ctm, partition_fields=None):
    """Build ORDERBY, PARTITIONBY, MATCHBY, and filter clauses for DAX window functions.

    Supports multi-level partitioning:
    - ``compute_using`` list: first element → ORDERBY, rest → PARTITIONBY
    - ``partition_fields`` dict with optional keys:
        - ``order_by``: list of ``(col, direction)`` tuples for multi-column ordering
        - ``partition_by``: list of column names for explicit PARTITIONBY
        - ``match_by``: list of column names for MATCHBY (grain disambiguation)

    Returns ``(order_clause, partition_clause, matchby_clause, filter_clause)``
    where each is a ready-to-insert DAX fragment (or empty string).
    """
    pf = partition_fields or {}
    order_by = pf.get('order_by', [])
    explicit_pb = pf.get('partition_by', [])
    match_by = pf.get('match_by', [])

    def _col_ref(col):
        t = ctm.get(col, table_name)
        return f"'{t}'[{col}]"

    # --- ORDERBY ---
    if order_by:
        parts = []
        all_refs = []
        for col, direction in order_by:
            ref = _col_ref(col)
            parts.append(f"{ref}, {direction.upper()}")
            all_refs.append(ref)
        order_clause = f"ORDERBY({', '.join(parts)})"
    elif compute_using:
        ref = _col_ref(compute_using[0])
        order_clause = f"ORDERBY({ref}, ASC)"
        all_refs = [ref]
    else:
        first_col = (list(ctm.keys())[0]) if ctm else 'RowNumber'
        ref = f"'{table_name}'[{first_col}]"
        order_clause = f"ORDERBY({ref}, ASC)"
        all_refs = [ref]

    # --- PARTITIONBY ---
    if explicit_pb:
        pb_refs = [_col_ref(c) for c in explicit_pb]
        partition_clause = f", PARTITIONBY({', '.join(pb_refs)})"
    elif compute_using and len(compute_using) > 1:
        pb_refs = [_col_ref(d) for d in compute_using[1:]]
        partition_clause = f", PARTITIONBY({', '.join(pb_refs)})"
    else:
        partition_clause = ""

    # --- MATCHBY ---
    if match_by:
        mb_refs = [_col_ref(c) for c in match_by]
        matchby_clause = f", MATCHBY({', '.join(mb_refs)})"
    else:
        matchby_clause = ""

    # --- Filter (ALLEXCEPT vs ALL) ---
    if compute_using:
        dim_refs = [_col_ref(d) for d in compute_using]
        filter_clause = f"ALLEXCEPT('{table_name}', {', '.join(dim_refs)})"
    else:
        filter_clause = f"ALL('{table_name}')"

    return order_clause, partition_clause, matchby_clause, filter_clause


def _convert_window_functions(dax, table_name, compute_using=None, column_table_map=None,
                              partition_fields=None):
    """Convert WINDOW_SUM/AVG/MAX/MIN/COUNT → CALCULATE(..., ALL/ALLEXCEPT).

    When compute_using dimensions are provided (from table calc addressing),
    uses ALLEXCEPT to partition by those dimensions instead of blanket ALL.

    Tableau WINDOW_* functions support frame boundaries:
        WINDOW_SUM(expr, first, last)
    where first/last are integer offsets relative to the current row
    (negative = preceding, positive = following, 0 = current).
    When frame boundaries are provided, the converter generates OFFSET-based
    DAX patterns to approximate the sliding window.

    ``partition_fields`` dict enables multi-level windowing:
        - ``order_by``: list of ``(col, direction)`` for multi-column ORDERBY
        - ``partition_by``: list of column names for explicit PARTITIONBY
        - ``match_by``: list of column names for MATCHBY grain disambiguation
    """
    ctm = column_table_map or {}
    percentile_pattern = _get_func_pattern('WINDOW_PERCENTILE')
    match = percentile_pattern.search(dax)
    while match:
        start_pos = match.end()
        depth = 1
        i = start_pos
        while i < len(dax) and depth > 0:
            if dax[i] == '(':
                depth += 1
            elif dax[i] == ')':
                depth -= 1
            i += 1
        if depth != 0:
            break
        replacement = _unsupported_conversion(
            'percentile window grain is not verified'
        )
        dax = dax[:match.start()] + replacement + dax[i:]
        match = percentile_pattern.search(dax)

    # Mapping of WINDOW functions that need an extra wrapping DAX aggregate function
    _WINDOW_WRAP = {
        'WINDOW_MEDIAN': 'MEDIAN',
        'WINDOW_STDEV': 'STDEV.S',
        'WINDOW_STDEVP': 'STDEV.P',
        'WINDOW_VAR': 'VAR.S',
        'WINDOW_VARP': 'VAR.P',
    }
    for window_func in ['WINDOW_SUM', 'WINDOW_AVG', 'WINDOW_MAX', 'WINDOW_MIN', 'WINDOW_COUNT'] + list(_WINDOW_WRAP.keys()):
        pattern = _get_func_pattern(window_func)
        match = pattern.search(dax)
        while match:
            start_pos = match.end()
            depth = 1
            i = start_pos
            while i < len(dax) and depth > 0:
                if dax[i] == '(':
                    depth += 1
                elif dax[i] == ')':
                    depth -= 1
                i += 1
            if depth != 0:
                break
            inner = dax[start_pos:i - 1]

            # Parse arguments — inner may contain commas at depth 0
            args = _split_args(inner)

            # Determine the inner aggregation expression and optional frame bounds
            inner_expr = args[0].strip() if args else inner
            # Wrap inner expression for WINDOW functions that need an extra aggregate
            wrap_fn = _WINDOW_WRAP.get(window_func)
            if wrap_fn:
                inner_expr = f'{wrap_fn}({inner_expr})'
            frame_start = None
            frame_end = None
            invalid_frame = len(args) not in (1, 3)
            if len(args) == 3:
                try:
                    frame_start = int(args[1].strip())
                    frame_end = int(args[2].strip())
                except (ValueError, TypeError):
                    invalid_frame = True

            # Build the DAX replacement
            if invalid_frame:
                replacement = _unsupported_conversion(
                    'window frame bounds require either no bounds or exactly two integer offsets'
                )
            elif frame_start is not None and frame_end is not None:
                # Frame boundaries specified — use DAX WINDOW function for precise range
                order_clause, partition_clause, matchby_clause, filter_clause = \
                    _build_window_clauses(compute_using, table_name, ctm, partition_fields)

                tag = window_func.replace('_', '.')
                replacement = (
                    f"CALCULATE({inner_expr}, "
                    f"WINDOW({frame_start}, REL, {frame_end}, REL, "
                    f"{order_clause}{partition_clause}{matchby_clause}), {filter_clause}) "
                    f"/* {tag}: frame [{frame_start},{frame_end}] */"
                )
            elif compute_using:
                # No frame boundaries but partition dimensions provided
                dim_refs = []
                for dim in compute_using:
                    t = ctm.get(dim, table_name)
                    dim_refs.append(f"'{t}'[{dim}]")
                replacement = f"CALCULATE({inner_expr}, ALLEXCEPT('{table_name}', {', '.join(dim_refs)}))"
            else:
                replacement = f"CALCULATE({inner_expr}, ALL('{table_name}'))"

            dax = dax[:match.start()] + replacement + dax[i:]
            match = pattern.search(dax)

    # --- WINDOW_CORR / WINDOW_COVAR / WINDOW_COVARP ---
    # These take 2 expression arguments (x, y) rather than 1, so they need
    # special handling separate from the main WINDOW_SUM/AVG loop above.
    for window_stat_func, stat_func in [('WINDOW_CORR', 'CORR'),
                                         ('WINDOW_COVAR', 'COVAR'),
                                         ('WINDOW_COVARP', 'COVARP')]:
        pattern = _get_func_pattern(window_stat_func)
        match = pattern.search(dax)
        while match:
            start_pos = match.end()
            depth = 1
            i = start_pos
            while i < len(dax) and depth > 0:
                if dax[i] == '(':
                    depth += 1
                elif dax[i] == ')':
                    depth -= 1
                i += 1
            if depth != 0:
                break
            inner = dax[start_pos:i - 1]
            args = _split_args(inner)

            if len(args) == 2:
                x_expr = args[0].strip()
                y_expr = args[1].strip()
                # Build the core correlation/covariance DAX
                core_dax = _build_corr_covar_dax(stat_func, x_expr, y_expr, table_name)

                # Wrap in CALCULATE with windowing context
                if compute_using:
                    dim_refs = []
                    for dim in compute_using:
                        t = ctm.get(dim, table_name)
                        dim_refs.append(f"'{t}'[{dim}]")
                    replacement = f"CALCULATE({core_dax}, ALLEXCEPT('{table_name}', {', '.join(dim_refs)}))"
                else:
                    replacement = f"CALCULATE({core_dax}, ALL('{table_name}'))"
            else:
                replacement = _unsupported_conversion(
                    'insufficient arguments or unsupported frame bounds for '
                    'correlation/covariance; exactly two expressions are required '
                    'without frame bounds'
                )

            dax = dax[:match.start()] + replacement + dax[i:]
            match = pattern.search(dax)

    return dax


def _convert_rank_functions(dax, table_name, compute_using=None, column_table_map=None):
    """Convert RANK(expr), RANK_UNIQUE(expr), RANK_DENSE(expr), RANK_MODIFIED(expr),
    RANK_PERCENTILE(expr) → RANKX(ALL/ALLEXCEPT('table'), expr) variants.

    When compute_using dimensions are provided (from table calc addressing),
    uses ALLEXCEPT to partition by those dimensions.
    """
    ctm = column_table_map or {}
    # Process longer names first to avoid partial matches
    for rank_func in ['RANK_PERCENTILE', 'RANK_MODIFIED', 'RANK_DENSE', 'RANK_UNIQUE', 'RANK']:
        pattern = _get_func_pattern(rank_func)
        match = pattern.search(dax)
        while match:
            start_pos = match.end()
            depth = 1
            i = start_pos
            while i < len(dax) and depth > 0:
                if dax[i] == '(':
                    depth += 1
                elif dax[i] == ')':
                    depth -= 1
                i += 1
            if depth != 0:
                break
            inner = dax[start_pos:i - 1].strip()
            func_upper = rank_func.upper()
            if compute_using:
                dim_refs = []
                for dim in compute_using:
                    t = ctm.get(dim, table_name)
                    dim_refs.append(f"'{t}'[{dim}]")
                table_expr = f"ALLEXCEPT('{table_name}', {', '.join(dim_refs)})"
            else:
                table_expr = f"ALL('{table_name}')"
            if func_upper == 'RANK_DENSE':
                replacement = f"RANKX({table_expr}, {inner},, ASC, DENSE)"
            elif func_upper == 'RANK_MODIFIED':
                replacement = f"RANKX({table_expr}, {inner},, ASC, SKIP) /* RANK_MODIFIED: modified competition ranking */"
            elif func_upper == 'RANK_PERCENTILE':
                replacement = f"DIVIDE(RANKX({table_expr}, {inner}) - 1, COUNTROWS({table_expr}) - 1) /* RANK_PERCENTILE: approximate */"
            else:
                replacement = f"RANKX({table_expr}, {inner})"
            dax = dax[:match.start()] + replacement + dax[i:]
            match = pattern.search(dax, match.start() + len(replacement))
    return dax


def _convert_running_functions(dax, table_name, table_calc_context=None):
    """Emit a warned visible-row aggregate when running semantics are unknown."""
    for tab_func in (
            'RUNNING_SUM', 'RUNNING_AVG', 'RUNNING_COUNT',
            'RUNNING_MAX', 'RUNNING_MIN'):
        def convert(args, inner, restore):
            safe_table_name = str(table_name).replace("'", "''")
            note = (
                'Table-calculation best-effort approximation; visible-row aggregate '
                'is not a verified cumulative result; mark grain, row order, '
                'partition, source filters and totals unverified'
            )
            logger.warning("%s; %s", tab_func, note)
            return (
                f"/* {note} */ CALCULATE({inner}, "
                f"FILTER(ALLSELECTED('{safe_table_name}'), TRUE()))"
            )

        dax = _transform_date_func_call(dax, tab_func, convert)
    return dax


def _convert_total_function(dax, table_name):
    """Convert TOTAL(expr) → CALCULATE(expr, ALL('table')).

    TOTAL() in Tableau returns the grand total of an expression,
    ignoring the current partition. This maps to CALCULATE + ALL.
    """
    pattern = _RE_TOTAL
    match = pattern.search(dax)
    while match:
        start_pos = match.end()
        depth = 1
        i = start_pos
        while i < len(dax) and depth > 0:
            if dax[i] == '(':
                depth += 1
            elif dax[i] == ')':
                depth -= 1
            i += 1
        inner = dax[start_pos:i - 1].strip()
        replacement = f"CALCULATE({inner}, ALL('{table_name}'))"
        dax = dax[:match.start()] + replacement + dax[i:]
        match = pattern.search(dax)
    return dax


# ── Phase 5: Column resolution ───────────────────────────────────────────────

def _resolve_columns(dax, table_name, column_table_map, measure_names,
                     is_calc_column, param_values, table_columns=None):
    """Resolve [col] → 'Table'[col] with cross-table RELATED() support.

    When *table_columns* is provided (set of column names belonging to the
    current table), a column that exists in the current table is always
    treated as a same-table reference — even if ``column_table_map`` maps
    it to a different table (which can happen when multiple datasources
    share table names and their columns are merged).
    """
    _local_cols = table_columns or set()

    def _dax_escape_col(col_name):
        return col_name.replace(']', ']]')

    def _resolve_col_name(col):
        reversed_name = _reverse_tableau_bracket_escape(col)
        if reversed_name != col and reversed_name in column_table_map:
            return reversed_name
        # Salesforce connector pattern: Tableau uses CamelCase-split display
        # names like "First Name" but actual columns are "FirstName (Table)".
        # Try matching col without spaces, with table suffix preferred.
        no_space = col.replace(' ', '')
        if no_space != col:
            # Prefer current table's suffixed column
            suffixed = f'{no_space} ({table_name})'
            if suffixed in column_table_map:
                return suffixed
            # Fall back to any table's matching column
            for candidate in column_table_map:
                base = candidate.split(' (')[0] if ' (' in candidate else candidate
                if base == no_space:
                    return candidate
        return col

    def resolve_column(m):
        raw_col = m.group(1)
        col = _resolve_col_name(raw_col)
        # Inline literal-only calc values in calc column expressions first,
        # before measure_names or column_table_map checks.
        if is_calc_column and col in param_values:
            return param_values[col]
        if col in measure_names:
            return f'[{_dax_escape_col(col)}]'
        if col in column_table_map:
            col_table = column_table_map[col]
            # If the column also belongs to the current table, prefer
            # same-table reference to avoid spurious RELATED/LOOKUPVALUE.
            if col in _local_cols:
                col_table = table_name
            safe_col_table = col_table.replace("'", "''")
            if is_calc_column and col_table != table_name:
                return f"RELATED('{safe_col_table}'[{_dax_escape_col(col)}])"
            return f"'{safe_col_table}'[{_dax_escape_col(col)}]"
        safe_table = table_name.replace("'", "''")
        return f"'{safe_table}'[{_dax_escape_col(col)}]"

    resolved = []
    cursor = 0
    for token in _RE_NULL_PROTECTED_TOKEN.finditer(dax):
        if cursor < token.start():
            resolved.append(_RE_COLUMN_RESOLVE.sub(
                resolve_column, dax[cursor:token.start()]))
        value = token.group(0)
        if value.startswith('[') and not (
                token.start() > 0 and dax[token.start() - 1] == "'"):
            value = _RE_COLUMN_RESOLVE.sub(resolve_column, value)
        resolved.append(value)
        cursor = token.end()
    if cursor < len(dax):
        resolved.append(_RE_COLUMN_RESOLVE.sub(resolve_column, dax[cursor:]))
    return ''.join(resolved)


# ── Phase 5h: Wrap bare column refs in measures ──────────────────────────────

# Functions that provide row context or accept bare column arguments.
# A bare 'Table'[Col] inside any of these is valid — do NOT wrap.
_MEASURE_AGG_RE = re.compile(
    r'\b(?:SUM|AVERAGE|MIN|MAX|COUNT|COUNTA|COUNTBLANK|DISTINCTCOUNT|'
    r'SUMX|AVERAGEX|MINX|MAXX|COUNTX|COUNTAX|CALCULATE|FILTER|'
    r'LOOKUPVALUE|RELATED|RANKX|PERCENTILE|MEDIAN|STDEV|VAR|'
    r'ALLEXCEPT|REMOVEFILTERS|ALL|VALUES|HASONEVALUE|SELECTEDVALUE|'
    r'EARLIER|EARLIEST|CONCATENATEX|TOPN|ADDCOLUMNS|SUMMARIZE|'
    r'GENERATE|GENERATEALL|TREATAS|USERELATIONSHIP|CROSSFILTER|'
    r'TOTALYTD|TOTALQTD|TOTALMTD|DATESYTD|DATESMTD|DATESQTD|'
    r'DATEADD|DATESBETWEEN|DATESINPERIOD|SAMEPERIODLASTYEAR|'
    r'PREVIOUSDAY|PREVIOUSMONTH|PREVIOUSQUARTER|PREVIOUSYEAR|'
    r'NEXTDAY|NEXTMONTH|NEXTQUARTER|NEXTYEAR|PARALLELPERIOD|'
    r'STARTOFMONTH|STARTOFQUARTER|STARTOFYEAR|'
    r'ENDOFMONTH|ENDOFQUARTER|ENDOFYEAR|'
    r'FIRSTDATE|LASTDATE|FIRSTNONBLANK|LASTNONBLANK|'
    r'CLOSINGBALANCEMONTH|CLOSINGBALANCEQUARTER|CLOSINGBALANCEYEAR|'
    r'OPENINGBALANCEMONTH|OPENINGBALANCEQUARTER|OPENINGBALANCEYEAR|'
    r'COUNTROWS|DIVIDE|DISTINCTCOUNTNOBLANK|COMBINEVALUES|CONTAINS|'
    r'PATH|PATHITEM|SELECTCOLUMNS)\s*\(',
    re.IGNORECASE
)
_MEASURE_TABLE_COL_RE = re.compile(r"'((?:[^']|'')+)'\[([^\]]+)\]")


def _wrap_bare_column_refs_in_measure(dax, table_name, table_columns, measure_names,
                                       bool_columns=None):
    """Wrap bare same-table column refs in MAX() for measure context.

    In a measure, a bare 'Table'[Col] that is not inside an aggregation or
    iterator function causes PBI to error with 'single value cannot be
    determined'.  This wraps such refs in MAX(), which is safe for
    LOD-derived calculated columns (they have one value per filter context).

    For Boolean columns, MAX() is invalid in DAX — wrap with
    MAXX('Table', IF(col, 1, 0)) instead, converting TRUE/FALSE to 1/0.

    Only wraps refs that:
    - Point to the current table
    - Refer to a known column (not a measure)
    - Are NOT already inside an aggregation/iterator/time-intel function
    """
    refs = list(_MEASURE_TABLE_COL_RE.finditer(dax))
    if not refs:
        return dax

    _bool_cols = bool_columns or set()
    result = dax
    for ref_match in reversed(refs):
        ref_table = ref_match.group(1).replace("''", "'")
        ref_col = ref_match.group(2)

        if ref_table != table_name:
            continue
        if ref_col in measure_names:
            continue
        if ref_col not in table_columns:
            continue

        # Backward paren walk to check if inside any aggregation function
        prefix = result[:ref_match.start()]
        inside_agg = False
        depth = 0
        for i in range(len(prefix) - 1, -1, -1):
            if prefix[i] == ')':
                depth += 1
            elif prefix[i] == '(':
                if depth > 0:
                    depth -= 1
                else:
                    # Extract function name immediately before '(' —
                    # check ONLY that name, not the entire prefix.
                    func_prefix = prefix[:i].rstrip()
                    fname_m = re.search(r'(\w+)\s*$', func_prefix)
                    if fname_m and _MEASURE_AGG_RE.search(fname_m.group(1) + '('):
                        inside_agg = True
                        break
        if inside_agg:
            continue

        ref_text = ref_match.group(0)
        tbl_esc = table_name.replace("'", "''")
        if ref_col in _bool_cols:
            wrap = f"MAXX('{tbl_esc}', IF({ref_text}, 1, 0))"
        else:
            wrap = f'MAX({ref_text})'
        result = (result[:ref_match.start()] +
                  wrap +
                  result[ref_match.end():])

    return result


# ── Phase 5b: AGG(IF) → AGGX ─────────────────────────────────────────────────

def _convert_agg_if_to_aggx(dax_text, table_name):
    """Convert SUM(IF(...)), AVERAGE(IF(...)), etc. to SUMX, AVERAGEX, etc."""
    agg_map = {
        'SUM': 'SUMX', 'AVERAGE': 'AVERAGEX', 'AVG': 'AVERAGEX',
        'MIN': 'MINX', 'MAX': 'MAXX', 'COUNT': 'COUNTX'
    }
    for agg, aggx in agg_map.items():
        pattern = re.compile(r'\b' + agg + r'\s*\(\s*(IF|SWITCH)\s*\(', re.IGNORECASE)
        m = pattern.search(dax_text)
        while m:
            start = m.start()
            paren_pos = dax_text.index('(', start)
            depth = 1
            pos = paren_pos + 1
            while pos < len(dax_text) and depth > 0:
                if dax_text[pos] == '(':
                    depth += 1
                elif dax_text[pos] == ')':
                    depth -= 1
                pos += 1
            if depth == 0:
                inner = dax_text[paren_pos + 1:pos - 1]
                iter_table = _infer_iteration_table(inner, table_name)
                inner = _wrap_cross_table_related(inner, iter_table)
                safe_table = iter_table.replace("'", "''")
                replacement = f"{aggx}('{safe_table}', {inner})"
                if agg == 'COUNT':
                    # Tableau COUNT is zero when every conditional value is
                    # NULL (or the input is empty); DAX COUNTX returns BLANK.
                    # Normalize the count, not a surrounding SUM or ratio:
                    # DIVIDE still returns BLANK for a zero denominator.
                    replacement = f"COALESCE({replacement}, 0)"
                dax_text = dax_text[:start] + replacement + dax_text[pos:]
            m = pattern.search(dax_text, start + len(aggx))

    # DISTINCTCOUNT has no AGGX variant — convert to CALCULATE+FILTER.
    dax_text = _convert_distinctcount_if(dax_text, table_name)

    return dax_text


def _split_if_args(if_content):
    """Split a balanced IF argument string into (condition, value, else).

    Returns ``(condition, value_col, else_expr)`` or ``None`` on failure.
    The *else_expr* may be ``None`` if no third argument exists.
    """
    depth = 0
    args = []
    start = 0
    for i, ch in enumerate(if_content):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif ch == ',' and depth == 0:
            args.append(if_content[start:i].strip())
            start = i + 1
    args.append(if_content[start:].strip())
    if len(args) < 2:
        return None
    return (args[0], args[1], args[2] if len(args) > 2 else None)


_RE_COUNTD_COLUMN = re.compile(
    r"^'((?:[^']|'')+)'\[((?:[^\]]|\]\])+)\]$"
)


def _countd_result(table_name, expression):
    table = table_name.replace("'", "''")
    return (f"IF(ISEMPTY('{table}'), BLANK(), "
            f"COALESCE({expression}, 0))")


def tableau_countd_column(table_name, column_name):
    """Count nonblank values, without creating marks for missing groups."""
    table = table_name.replace("'", "''")
    column = column_name.replace(']', ']]')
    return _countd_result(
        table_name, f"DISTINCTCOUNTNOBLANK('{table}'[{column}])")


def _convert_tableau_countd(dax, default_table):
    """Lower Tableau COUNTD calls after their column references are resolved."""
    prefix = '__countd_token_'
    while prefix in dax:
        prefix = '_' + prefix
    tokens = []

    def protect(match):
        tokens.append(match.group(0))
        return f'{prefix}{len(tokens) - 1}__'

    masked = _RE_NULL_PROTECTED_TOKEN.sub(protect, dax)
    token_re = re.compile(re.escape(prefix) + r'(\d+)__')

    def restore(text):
        return token_re.sub(lambda match: tokens[int(match.group(1))], text)

    for start, end, masked_inner in reversed(
            _extract_balanced_call(masked, 'DISTINCTCOUNTNOBLANK')):
        inner = restore(masked_inner).strip()
        simple = _RE_COUNTD_COLUMN.fullmatch(inner)
        if simple:
            table = simple.group(1).replace("''", "'")
            column = simple.group(2).replace(']]', ']')
            replacement = tableau_countd_column(table, column)
        else:
            # Ignore quoted string contents when finding row-context tables.
            without_strings = re.sub(r'"(?:[^"]|"")*"', '', inner)
            tables = {
                match.group(1).replace("''", "'")
                for match in _MEASURE_TABLE_COL_RE.finditer(without_strings)
            }
            if len(tables) > 1:
                logger.warning(
                    "COUNTD expression references multiple tables; "
                    "its row context requires manual review"
                )
                replacement = '/* TODO: COUNTD row context spans tables */ BLANK()'
            else:
                table = next(iter(tables), default_table)
                safe_table = table.replace("'", "''")
                count = None
                calls = _extract_balanced_call(masked_inner.strip(), 'IF')
                if calls and calls[0][0] == 0 and calls[0][1] == len(masked_inner.strip()):
                    args = _split_if_args(calls[0][2])
                    if args:
                        condition, value, otherwise = args
                        value = restore(value).strip()
                        other = restore(otherwise).strip() if otherwise else ''
                        if (_RE_COUNTD_COLUMN.fullmatch(value)
                                and (not other or re.fullmatch(
                                    r'BLANK\s*\(\s*\)', other, re.IGNORECASE))):
                            count = (
                                f"CALCULATE(DISTINCTCOUNTNOBLANK({value}), "
                                f"FILTER('{safe_table}', {restore(condition)}))"
                            )
                if count is None:
                    count = (
                        f"COUNTROWS(FILTER(DISTINCT(SELECTCOLUMNS('{safe_table}', "
                        f"\"__TableauCountD\", {inner})), "
                        "NOT(ISBLANK([__TableauCountD]))))"
                    )
                replacement = _countd_result(table, count)
        masked = masked[:start] + replacement + masked[end:]

    return restore(masked)


def _convert_distinctcount_if(dax_text, table_name):
    """Convert DISTINCTCOUNT(IF(cond, [col], BLANK())) to CALCULATE pattern.

    DISTINCTCOUNT only accepts a single column reference, so
    ``DISTINCTCOUNT(IF(cond, [col], BLANK()))`` must become
    ``CALCULATE(DISTINCTCOUNT([col]), FILTER('table', cond))``.
    """
    pattern = re.compile(
        r'\bDISTINCTCOUNT\s*\(\s*IF\s*\(', re.IGNORECASE
    )
    m = pattern.search(dax_text)
    while m:
        # Find outer DISTINCTCOUNT( balanced paren
        outer_start = m.start()
        paren_pos = dax_text.index('(', outer_start)
        depth = 1
        pos = paren_pos + 1
        while pos < len(dax_text) and depth > 0:
            if dax_text[pos] == '(':
                depth += 1
            elif dax_text[pos] == ')':
                depth -= 1
            pos += 1
        if depth != 0:
            break
        # inner = "IF(cond, col, BLANK())"
        inner = dax_text[paren_pos + 1:pos - 1].strip()
        # Extract IF arguments: strip outer IF(...)
        if_match = re.match(r'IF\s*\(', inner, re.IGNORECASE)
        if not if_match:
            break
        if_start = if_match.end()
        # Find balanced close of IF(
        if_depth = 1
        if_pos = if_start
        while if_pos < len(inner) and if_depth > 0:
            if inner[if_pos] == '(':
                if_depth += 1
            elif inner[if_pos] == ')':
                if_depth -= 1
            if_pos += 1
        if if_depth != 0:
            break
        if_content = inner[if_start:if_pos - 1]
        parsed = _split_if_args(if_content)
        if parsed is None:
            break
        condition, value_col, _ = parsed
        iter_table = _infer_iteration_table(
            if_content, table_name)
        replacement = (
            f"CALCULATE(DISTINCTCOUNT({value_col}), "
            f"FILTER('{iter_table}', {condition}))"
        )
        dax_text = dax_text[:outer_start] + replacement + dax_text[pos:]
        m = pattern.search(dax_text, outer_start + len(replacement))
    return dax_text


# ── Phase 5c: AGG(multi-col) → AGGX ──────────────────────────────────────────

def _unwrap_inner_agg(inner):
    """If *inner* is a simple ``AGG(expr)`` call, return *expr*; else ``None``.

    Handles nested parentheses correctly by matching balanced parens.
    Only unwraps when the AGG call spans the entire string (no trailing text).
    """
    agg_funcs = ['SUM', 'AVERAGE', 'AVG', 'MIN', 'MAX', 'COUNT']
    for func in agg_funcs:
        m = re.match(r'\b' + func + r'\s*\(', inner, re.IGNORECASE)
        if m:
            paren_start = m.end() - 1
            depth = 1
            pos = paren_start + 1
            while pos < len(inner) and depth > 0:
                if inner[pos] == '(':
                    depth += 1
                elif inner[pos] == ')':
                    depth -= 1
                pos += 1
            # Only unwrap if there's nothing after the closing paren
            if depth == 0 and pos == len(inner):
                return inner[paren_start + 1:pos - 1].strip()
    return None


def _convert_agg_expr_to_aggx(dax_text, table_name):
    """Convert SUM(expr), AVERAGE(expr), etc. to SUMX, AVERAGEX when expr is
    not a single column reference.

    DAX SUM/AVERAGE/MIN/MAX/COUNT only accept a single column.
    Expressions like  SUM('T'[a] * 'T'[b])  must become  SUMX('T', 'T'[a] * 'T'[b]).

    Statistical functions (STDEV.S, STDEV.P, MEDIAN) are also converted to
    their iterator forms (STDEVX.S, STDEVX.P, MEDIANX).  When a statistical
    function wraps another aggregation — e.g. ``STDEV.S(SUM(qty*price))`` —
    the inner aggregation is *unwrapped* because the iterator already provides
    row context, yielding ``STDEVX.S('T', qty*price)``.
    """

    def _is_single_column(expr):
        """True when *expr* is a bare column reference like 'T'[Col] or [Col]."""
        if _RE_COUNTD_COLUMN.fullmatch(expr):
            return True
        if re.fullmatch(r"\[(?:[^\]]|\]\])+\]", expr):
            return True
        return False

    def _is_numeric_constant(expr):
        return bool(re.fullmatch(r"[-+]?\d+(?:\.\d+)?", expr.strip()))

    def _process_map(dax, mapping, unwrap_inner_agg=False):
        for agg, aggx in mapping.items():
            pattern = _get_func_pattern(agg)
            matches = list(pattern.finditer(dax))
            for m in reversed(matches):
                end_of_word = m.end() - 1  # position of '('
                word_start = m.start()
                word_text = dax[word_start:end_of_word].strip()
                if word_text.upper() != agg.upper():
                    continue

                paren_start = end_of_word
                depth = 1
                pos = paren_start + 1
                while pos < len(dax) and depth > 0:
                    if dax[pos] == '(':
                        depth += 1
                    elif dax[pos] == ')':
                        depth -= 1
                    pos += 1
                if depth != 0:
                    continue

                inner = dax[paren_start + 1:pos - 1].strip()

                if _is_single_column(inner):
                    continue

                if _is_numeric_constant(inner):
                    dax = dax[:m.start()] + inner + dax[pos:]
                    continue

                # For statistical iterators, collapse a redundant inner agg:
                #   STDEV.S(SUM(a*b)) → STDEVX.S('T', a*b)
                if unwrap_inner_agg:
                    unwrapped = _unwrap_inner_agg(inner)
                    if unwrapped is not None:
                        inner = unwrapped

                iter_table = _infer_iteration_table(inner, table_name)
                inner = _wrap_cross_table_related(inner, iter_table)
                safe_table = iter_table.replace("'", "''")
                replacement = f"{aggx}('{safe_table}', {inner})"
                dax = dax[:m.start()] + replacement + dax[pos:]
        return dax

    # Step 1: Statistical aggregates (process FIRST so that their inner
    #         SUM/AVERAGE/etc. hasn't been converted to SUMX yet).
    stat_to_statx = {
        'STDEV.S': 'STDEVX.S', 'STDEV.P': 'STDEVX.P',
        'MEDIAN': 'MEDIANX',
    }
    dax_text = _process_map(dax_text, stat_to_statx, unwrap_inner_agg=True)

    # Step 2: Basic aggregation (SUM → SUMX, etc.)
    agg_to_aggx = {
        'SUM': 'SUMX', 'AVERAGE': 'AVERAGEX',
        'MIN': 'MINX', 'MAX': 'MAXX', 'COUNT': 'COUNTX',
    }
    dax_text = _process_map(dax_text, agg_to_aggx)

    return dax_text


def _ensure_comparison_spacing(dax):
    """Ensure spaces around comparison operators outside strings, brackets, and quoted names.

    Prevents tokens like ``]>EDATE`` which some TMDL/DAX engines may
    misparse.  Multi-character operators (>=, <=, <>) are handled before
    single-character ones (>, <) to avoid partial matches.
    """
    # Fast path: skip regex split when no comparison operators present
    if '<' not in dax and '>' not in dax:
        return dax
    # Split into delimited tokens (strings, brackets, quoted names) and the rest
    parts = re.split(r'("(?:[^"\\]|\\.)*"|\[[^\]]*\]|\'(?:[^\']|\'\')*\')', dax)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            # Inside delimited token — preserve as-is
            result.append(part)
        else:
            # Add spaces around comparison operators
            part = re.sub(r'(>=|<=|<>|(?<![<])>(?!=)|<(?![>=]))', r' \1 ', part)
            # Collapse multiple spaces into one
            part = re.sub(r'  +', ' ', part)
            result.append(part)
    return ''.join(result)


# ── Phase 6: Cleanup ─────────────────────────────────────────────────────────

def _normalize_spaces_outside_identifiers(text):
    """Normalize multiple spaces except inside identifiers and string literals."""
    result = []
    i = 0
    while i < len(text):
        if text[i] == '[':
            close = ']'
            j = text.index(close, i + 1) + 1 if close in text[i + 1:] else len(text)
            result.append(text[i:j])
            i = j
        elif text[i] == "'":
            # Handle single-quoted table names, skipping escaped '' pairs
            j = i + 1
            while j < len(text):
                if text[j] == "'":
                    if j + 1 < len(text) and text[j + 1] == "'":
                        j += 2  # skip escaped ''
                    else:
                        j += 1  # closing quote
                        break
                else:
                    j += 1
            result.append(text[i:j])
            i = j
        elif text[i] == '"':
            # DAX uses doubled quotes; Tableau also permits backslash escapes.
            j = i + 1
            while j < len(text):
                if text[j] == '\\' and j + 1 < len(text):
                    j += 2
                elif text[j] == '"':
                    if j + 1 < len(text) and text[j + 1] == '"':
                        j += 2
                    else:
                        j += 1
                        break
                else:
                    j += 1
            result.append(text[i:j])
            i = j
        elif text[i] in (' ', '\t'):
            j = i
            while j < len(text) and text[j] in (' ', '\t'):
                j += 1
            result.append(' ')
            i = j
        else:
            result.append(text[i])
            i += 1
    return ''.join(result)


# ── Utility ───────────────────────────────────────────────────────────────────

def _split_args(inner):
    """Split function arguments respecting nested parentheses and string literals."""
    args = []
    depth = 0
    current = []
    in_string = False
    string_char = None
    for ch in inner:
        if in_string:
            current.append(ch)
            if ch == string_char:
                in_string = False
                string_char = None
        elif ch == '"' or ch == "'":
            in_string = True
            string_char = ch
            current.append(ch)
        elif ch == '(':
            depth += 1
            current.append(ch)
        elif ch == ')':
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0:
            args.append(''.join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        args.append(''.join(current).strip())
    return args


def generate_combined_field_dax(source_fields, table_name, separator=' '):
    """Generate DAX expression for a combined field (CONCATENATE of multiple columns).

    Args:
        source_fields: List of source column names
        table_name: Table containing the columns
        separator: Separator between values (default: space)

    Returns:
        str: DAX calculated column expression
    """
    if not source_fields:
        return '""'
    if len(source_fields) == 1:
        return f"'{table_name}'[{source_fields[0]}]"
    parts = [f"'{table_name}'[{f}]" for f in source_fields]
    sep_literal = f'"{separator}"'
    # Use nested CONCATENATE pairs for 2 fields, or & for more
    if len(parts) == 2:
        return f"{parts[0]} & {sep_literal} & {parts[1]}"
    return (' & ' + sep_literal + ' & ').join(parts)


# ═══════════════════════════════════════════════════════════════════════════════
#  SCRIPT_* Analytics Extension Detection
# ═══════════════════════════════════════════════════════════════════════════════

_RE_SCRIPT_CALL = re.compile(
    r'\b(SCRIPT_(?:BOOL|INT|REAL|STR))\s*\(\s*"((?:[^"\\]|\\.)*)"\s*,',
    re.IGNORECASE | re.DOTALL,
)


def detect_script_functions(formula):
    """Detect SCRIPT_* analytics extension calls in a Tableau formula.

    Args:
        formula: Raw Tableau calculation formula string.

    Returns:
        list[dict]: One entry per SCRIPT_* call with keys:
            - ``function``: e.g. ``"SCRIPT_REAL"``
            - ``language``: ``"python"`` or ``"r"`` (heuristic)
            - ``code``: The embedded script source string.
            - ``return_type``: ``"bool"``, ``"int"``, ``"real"``, or ``"str"``.
    """
    if not formula:
        return []

    results = []
    for m in _RE_SCRIPT_CALL.finditer(formula):
        func_name = m.group(1).upper()
        raw_code = m.group(2).replace('\\"', '"').replace('\\n', '\n')

        # Heuristic: detect language from script content
        language = _detect_script_language(raw_code)

        type_suffix = func_name.split('_')[1].lower()
        results.append({
            'function': func_name,
            'language': language,
            'code': raw_code,
            'return_type': type_suffix,
        })
    return results


def _detect_script_language(code):
    """Heuristic to detect if a script is Python or R.

    Checks for common language-specific markers.

    Returns:
        ``"python"`` or ``"r"``.
    """
    python_markers = ['import ', 'def ', 'pandas', 'numpy', 'print(', 'elif ',
                      'return ', 'lambda ', '__', '.append(', '.items()',
                      'pd.', 'np.', 'from ']
    r_markers = ['<-', 'library(', 'c(', 'data.frame(', 'ggplot', 'dplyr',
                 'tidyr', 'sapply(', 'lapply(', 'function(', 'nrow(',
                 'ncol(', 'paste0(', '%>%', 'data.table']

    code_lower = code.lower()
    py_score = sum(1 for m in python_markers if m.lower() in code_lower)
    r_score = sum(1 for m in r_markers if m.lower() in code_lower)

    return 'python' if py_score >= r_score else 'r'


def has_script_functions(formula):
    """Return True if the formula contains any SCRIPT_* function call."""
    if not formula:
        return False
    return bool(_RE_SCRIPT_CALL.search(formula))


# ═══════════════════════════════════════════════════════════════════
# Sprint 158 — Spatial & Regex Gap Closure
# ═══════════════════════════════════════════════════════════════════

# Regex pattern library for Tableau REGEXP_* functions
_REGEXP_PATTERNS = {
    # Common Tableau regex patterns → DAX equivalents
    'email': (r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',
              'CONTAINSSTRING([{col}], "@") && CONTAINSSTRING([{col}], ".")'),
    'phone_us': (r'\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}',
                 'LEN(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE(SUBSTITUTE('
                 '[{col}], "-", ""), "(", ""), ")", ""), " ", "")) >= 10'),
    'url': (r'https?://[^\s]+',
            'CONTAINSSTRING([{col}], "http://") || CONTAINSSTRING([{col}], "https://")'),
    'zip_us': (r'^\d{5}(-\d{4})?$',
               'LEN([{col}]) >= 5 && ISNUMBER(VALUE(LEFT([{col}], 5)))'),
}


def convert_regexp_match(formula, column_refs=None):
    """Convert Tableau REGEXP_MATCH to DAX approximation.

    REGEXP_MATCH(field, pattern) → CONTAINSSTRING-based logic.
    For complex patterns, generates a Python visual fallback comment.

    Args:
        formula: Tableau REGEXP_MATCH formula string.
        column_refs: Optional set of known column names.

    Returns:
        str: DAX expression (best-effort) or commented fallback.
    """
    match = re.match(
        r'REGEXP_MATCH\s*\(\s*(.+?)\s*,\s*["\'](.+?)["\']\s*\)',
        formula, re.IGNORECASE
    )
    if not match:
        return f'/* REGEXP_MATCH not converted: {formula} */ TRUE()'

    field = match.group(1).strip()
    pattern = match.group(2).strip()

    # Check known patterns
    for name, (regex, dax_template) in _REGEXP_PATTERNS.items():
        if pattern == regex or re.fullmatch(regex, pattern):
            return dax_template.replace('{col}', field)

    # Simple contains pattern: just literal string
    if re.fullmatch(r'[A-Za-z0-9_\- ]+', pattern):
        return f'CONTAINSSTRING({field}, "{pattern}")'

    # Fallback with comment
    return (f'/* REGEXP_MATCH("{pattern}") — no direct DAX equivalent. '
            f'Consider Python visual or Power Query M. */ '
            f'CONTAINSSTRING({field}, "")')


def convert_regexp_replace(formula):
    """Convert Tableau REGEXP_REPLACE to DAX SUBSTITUTE chain.

    REGEXP_REPLACE(field, pattern, replacement) → SUBSTITUTE for simple patterns.

    Args:
        formula: Tableau REGEXP_REPLACE formula string.

    Returns:
        str: DAX expression.
    """
    match = re.match(
        r'REGEXP_REPLACE\s*\(\s*(.+?)\s*,\s*["\'](.+?)["\']\s*,\s*["\'](.*)["\']\s*\)',
        formula, re.IGNORECASE
    )
    if not match:
        return f'/* REGEXP_REPLACE not converted: {formula} */ ""'

    field = match.group(1).strip()
    pattern = match.group(2).strip()
    replacement = match.group(3).strip()

    # Simple literal replacement
    if re.fullmatch(r'[A-Za-z0-9_\-. ]+', pattern):
        return f'SUBSTITUTE({field}, "{pattern}", "{replacement}")'

    # Character class [xyz] → chained SUBSTITUTE
    char_class_match = re.fullmatch(r'\[(.+?)\]', pattern)
    if char_class_match:
        chars = char_class_match.group(1)
        result = field
        for ch in chars:
            if ch == '\\':
                continue
            result = f'SUBSTITUTE({result}, "{ch}", "{replacement}")'
        return result

    return (f'/* REGEXP_REPLACE("{pattern}") → complex regex, manual review needed */ '
            f'SUBSTITUTE({field}, "", "{replacement}")')


def convert_regexp_extract(formula):
    """Convert Tableau REGEXP_EXTRACT to DAX MID/FIND approximation.

    REGEXP_EXTRACT(field, pattern, n) → best-effort substring extraction.

    Args:
        formula: Tableau REGEXP_EXTRACT formula string.

    Returns:
        str: DAX expression.
    """
    match = re.match(
        r'REGEXP_EXTRACT\s*\(\s*(.+?)\s*,\s*["\'](.+?)["\']\s*(?:,\s*(\d+))?\s*\)',
        formula, re.IGNORECASE
    )
    if not match:
        return f'/* REGEXP_EXTRACT not converted: {formula} */ ""'

    field = match.group(1).strip()
    pattern = match.group(2).strip()

    # Email domain extraction pattern
    if '@' in pattern and '\\.' in pattern:
        return (f'MID({field}, FIND("@", {field}) + 1, '
                f'LEN({field}) - FIND("@", {field}))')

    return (f'/* REGEXP_EXTRACT("{pattern}") — requires Power Query M. */ '
            f'MID({field}, 1, LEN({field}))')


def convert_spatial_to_python_visual(formula, mark_type='map'):
    """Convert Tableau spatial functions to PBI Python visual template.

    MAKEPOINT, MAKELINE, DISTANCE, BUFFER, AREA → Python visual with
    geopandas/folium.

    Args:
        formula: Tableau spatial formula.
        mark_type: 'map', 'line', 'polygon'.

    Returns:
        dict: {script: str, language: 'python', packages: [...]}
    """
    spatial_funcs = {
        'MAKEPOINT': 'Point',
        'MAKELINE': 'LineString',
        'DISTANCE': 'distance',
        'BUFFER': 'buffer',
        'AREA': 'area',
        'INTERSECTION': 'intersection',
        'UNION': 'union',
    }

    func_match = re.match(r'(\w+)\s*\(', formula)
    func_name = func_match.group(1).upper() if func_match else 'MAKEPOINT'
    geom_type = spatial_funcs.get(func_name, 'Point')

    script = f'''# Tableau spatial function: {formula}
import geopandas as gpd
from shapely.geometry import {geom_type}
import matplotlib.pyplot as plt

# dataset is the PBI Python visual input DataFrame
gdf = gpd.GeoDataFrame(dataset, geometry=gpd.points_from_xy(
    dataset['Longitude'], dataset['Latitude']))
gdf.plot(figsize=(10, 8), markersize=5, alpha=0.7)
plt.title("Spatial Visualization")
plt.tight_layout()
plt.show()'''

    return {
        'script': script,
        'language': 'python',
        'packages': ['geopandas', 'shapely', 'matplotlib'],
        'note': f'Tableau {func_name} → Python visual (no native DAX spatial)',
    }


# ═══════════════════════════════════════════════════════════════════
# Sprint 159 — Table Calculation Depth
# ═══════════════════════════════════════════════════════════════════

def convert_window_percentile(formula, table_name):
    """Legacy standalone approximation of Tableau WINDOW_PERCENTILE.

    This helper is not invoked by the main formula converter. The active
    conversion path fails closed because mark grain, percentile, and frame
    semantics are unverified. This approximation is not evidence of active-path
    support or runtime parity.

    WINDOW_PERCENTILE(expr, percentile) →
        PERCENTILEX.INC(ALL('table'), expr, percentile)

    Args:
        formula: Tableau WINDOW_PERCENTILE expression.
        table_name: Context table name.

    Returns:
        str: DAX expression.
    """
    match = re.match(
        r'WINDOW_PERCENTILE\s*\(\s*(.+?)\s*,\s*([0-9.]+)\s*\)',
        formula, re.IGNORECASE
    )
    if not match:
        return None

    expr = match.group(1).strip()
    percentile = match.group(2).strip()
    return f"PERCENTILEX.INC(ALL('{table_name}'), {expr}, {percentile})"


def convert_running_with_partition(formula, table_name, partition_cols=None):
    """Convert Tableau RUNNING_* with partition (addressing) to DAX.

    RUNNING_SUM(SUM([Sales])) partitioned by [Region] →
        CALCULATE(SUM([Sales]),
            FILTER(ALL('table'),
                'table'[Region] = EARLIER('table'[Region]) &&
                'table'[__sort__] <= EARLIER('table'[__sort__])))

    Args:
        formula: Tableau RUNNING_SUM/AVG/COUNT expression.
        table_name: Context table name.
        partition_cols: List of partition (addressing) columns.

    Returns:
        str: DAX expression with CALCULATE + FILTER.
    """
    match = re.match(
        r'RUNNING_(SUM|AVG|COUNT|MIN|MAX)\s*\(\s*(.+)\s*\)',
        formula, re.IGNORECASE
    )
    if not match:
        return None

    func = match.group(1).upper()
    inner_expr = match.group(2).strip()

    dax_func_map = {
        'SUM': 'SUM', 'AVG': 'AVERAGE', 'COUNT': 'COUNT',
        'MIN': 'MIN', 'MAX': 'MAX',
    }
    dax_func = dax_func_map.get(func, 'SUM')

    if not partition_cols:
        # Simple running total (no partition)
        return (f"CALCULATE({dax_func}({inner_expr}), "
                f"FILTER(ALL('{table_name}'), "
                f"'{table_name}'[__sort__] <= EARLIER('{table_name}'[__sort__])))")

    # Partitioned running total
    partition_filters = " && ".join(
        f"'{table_name}'[{col}] = EARLIER('{table_name}'[{col}])"
        for col in partition_cols
    )
    return (f"CALCULATE({dax_func}({inner_expr}), "
            f"FILTER(ALL('{table_name}'), "
            f"{partition_filters} && "
            f"'{table_name}'[__sort__] <= EARLIER('{table_name}'[__sort__])))")


def convert_lookup_offset(formula, table_name, offset=1):
    """Convert Tableau LOOKUP to DAX OFFSET (PBI 2023+).

    LOOKUP(expr, offset) → OFFSET(offset, ALLSELECTED('table'), ORDERBY(...))

    Falls back to INDEX for pre-2023 compatibility.

    Args:
        formula: Tableau LOOKUP expression.
        table_name: Context table name.
        offset: Row offset (positive = forward, negative = backward).

    Returns:
        str: DAX expression using OFFSET or INDEX.
    """
    match = re.match(
        r'LOOKUP\s*\(\s*(.+?)\s*,\s*(-?\d+)\s*\)',
        formula, re.IGNORECASE
    )
    if match:
        expr = match.group(1).strip()
        offset = int(match.group(2))
    else:
        expr = formula
        offset = offset

    return (f"CALCULATE({expr}, "
            f"OFFSET({offset}, ALLSELECTED('{table_name}'), "
            f"ORDERBY('{table_name}'[__sort__], ASC)))")


# ═══════════════════════════════════════════════════════════════════
# Sprint 160 — LOD & Security Hardening
# ═══════════════════════════════════════════════════════════════════

def convert_nested_lod(formula, table_name, all_dimensions=None):
    """Convert nested LOD expressions (LOD inside LOD).

    {FIXED [Dim1] : SUM({FIXED [Dim2] : COUNT([Field])})} →
        VAR _inner = CALCULATE(COUNT([Field]), ALLEXCEPT('t', 't'[Dim2]))
        RETURN CALCULATE(SUM(_inner), ALLEXCEPT('t', 't'[Dim1]))

    For nested LODs, generates a CALCULATE-in-CALCULATE pattern.

    Args:
        formula: Tableau LOD expression with nesting.
        table_name: Context table name.
        all_dimensions: All available dimensions for EXCLUDE context.

    Returns:
        str: DAX expression or None if not a nested LOD.
    """
    # Count LOD nesting depth
    lod_count = formula.upper().count('{FIXED') + formula.upper().count('{INCLUDE') + \
                formula.upper().count('{EXCLUDE')
    if lod_count < 2:
        return None  # Not nested — handled by standard LOD converter

    # Extract inner LOD first
    inner_match = re.search(
        r'\{(FIXED|INCLUDE|EXCLUDE)\s+([^:}]+)\s*:\s*(\w+)\s*\(\s*([^}]+?)\s*\)\s*\}',
        formula, re.IGNORECASE
    )
    if not inner_match:
        return None

    inner_type = inner_match.group(1).upper()
    inner_dims_raw = inner_match.group(2).strip()
    inner_agg = inner_match.group(3).strip()
    inner_field = inner_match.group(4).strip()

    inner_dims = [d.strip().strip('[]') for d in inner_dims_raw.split(',')]

    # Build inner CALCULATE
    if inner_type == 'FIXED':
        inner_allexcept = ", ".join(f"'{table_name}'[{d}]" for d in inner_dims)
        inner_dax = f"CALCULATE({inner_agg}({inner_field}), ALLEXCEPT('{table_name}', {inner_allexcept}))"
    elif inner_type == 'INCLUDE':
        inner_dax = f"CALCULATE({inner_agg}({inner_field}))"
    else:  # EXCLUDE
        inner_dax = f"CALCULATE({inner_agg}({inner_field}), REMOVEFILTERS('{table_name}'))"

    # Replace inner LOD with placeholder and process outer
    outer_formula = formula[:inner_match.start()] + inner_dax + formula[inner_match.end():]

    # If outer is also an LOD, process it
    outer_match = re.search(
        r'\{(FIXED|INCLUDE|EXCLUDE)\s+([^:}]+)\s*:\s*(\w+)\s*\(\s*(.+?)\s*\)\s*\}',
        outer_formula, re.IGNORECASE
    )
    if outer_match:
        outer_type = outer_match.group(1).upper()
        outer_dims_raw = outer_match.group(2).strip()
        outer_agg = outer_match.group(3).strip()
        outer_field = outer_match.group(4).strip()
        outer_dims = [d.strip().strip('[]') for d in outer_dims_raw.split(',')]

        if outer_type == 'FIXED':
            outer_allexcept = ", ".join(f"'{table_name}'[{d}]" for d in outer_dims)
            return f"CALCULATE({outer_agg}({outer_field}), ALLEXCEPT('{table_name}', {outer_allexcept}))"
        elif outer_type == 'INCLUDE':
            return f"CALCULATE({outer_agg}({outer_field}))"
        else:
            return f"CALCULATE({outer_agg}({outer_field}), REMOVEFILTERS('{table_name}'))"

    return inner_dax


def convert_multi_dim_exclude(formula, table_name, all_dimensions):
    """Convert multi-dimension EXCLUDE LOD to DAX.

    {EXCLUDE [Dim1], [Dim2] : SUM([Sales])} →
        CALCULATE(SUM([Sales]), REMOVEFILTERS('t'[Dim1], 't'[Dim2]))

    Args:
        formula: Tableau EXCLUDE LOD with multiple dimensions.
        table_name: Context table name.
        all_dimensions: All dimensions in the viz.

    Returns:
        str: DAX CALCULATE with REMOVEFILTERS on excluded dims.
    """
    match = re.match(
        r'\{\s*EXCLUDE\s+(.+?)\s*:\s*(\w+)\s*\(\s*(.+?)\s*\)\s*\}',
        formula, re.IGNORECASE
    )
    if not match:
        return None

    dims_raw = match.group(1).strip()
    agg = match.group(2).strip()
    field = match.group(3).strip()

    excluded_dims = [d.strip().strip('[]') for d in dims_raw.split(',')]
    remove_filters = ", ".join(f"'{table_name}'[{d}]" for d in excluded_dims)

    return f"CALCULATE({agg}({field}), REMOVEFILTERS({remove_filters}))"


def convert_ismemberof_to_rls(formula, groups=None):
    """Convert Tableau ISMEMBEROF to RLS role annotations.

    ISMEMBEROF("Finance") → TRUE() + annotation for RLS role creation.

    Args:
        formula: Tableau formula containing ISMEMBEROF.
        groups: Optional list of group names to extract.

    Returns:
        dict: {dax: str, rls_roles: [{group, dax_filter}]}
    """
    rls_roles = []
    dax = formula

    for match in re.finditer(r'ISMEMBEROF\s*\(\s*["\'](.+?)["\']\s*\)',
                             formula, re.IGNORECASE):
        group_name = match.group(1)
        rls_roles.append({
            'group': group_name,
            'dax_filter': 'TRUE()',
            'note': f'Create RLS role "{group_name}" and assign Azure AD group members',
        })
        # Replace in formula with TRUE() (RLS role handles the logic)
        dax = dax[:match.start()] + 'TRUE()' + dax[match.end():]

    return {'dax': dax, 'rls_roles': rls_roles}
