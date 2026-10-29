"""
Utility functions for classifying and converting Tableau calculated columns.

Determines which calculations should be materialised as physical columns
in the Lakehouse (loaded by Dataflows or Notebooks) versus kept as
DAX measures in the Semantic Model.

Architecture:
    Calculated columns → physical columns in Lakehouse Delta tables
    Measures           → DAX expressions in Semantic Model
"""

import re

from .fabric_constants import AGG_PATTERN as _AGG_PATTERN
from .fabric_constants import SPARK_TYPE_MAP
from .pyspark_converter import convert_to_pyspark, todo_statement, with_column_statement

_CALC_SPARK_TYPE = {k: v for k, v in SPARK_TYPE_MAP.items()
                    if k in ('string', 'integer', 'int64', 'real',
                             'double', 'number', 'boolean', 'date', 'datetime')}


def calc_spark_type(datatype):
    """Spark type documented for a materialised calculated column."""
    return _CALC_SPARK_TYPE.get(datatype, 'STRING')


def is_calc_column_candidate(calc):
    """Return True when a calculation is a row-level expression.

    A calculated column needs a formula that refers to at least one field and
    either is a dimension or contains no aggregation function.
    """
    formula = (calc.get('formula') or '').strip()
    if not formula:
        return False
    is_literal = '[' not in formula
    has_aggregation = bool(_AGG_PATTERN.search(formula))
    return (not is_literal) and (
        calc.get('role', 'measure') == 'dimension' or not has_aggregation
    )


def classify_calculations(calculations):
    """Split Tableau calculations into *calculated columns* vs *measures*.

    Calculated columns are row-level expressions without aggregation and
    are materialised in the Lakehouse.  Measures use aggregation functions
    and remain as DAX in the Semantic Model.

    Returns:
        ``(calc_columns, measures)`` — two lists.
        Each ``calc_column`` dict carries an extra ``spark_type`` key.
    """
    calc_columns = []
    measures = []

    for calc in calculations:
        if not (calc.get('formula') or '').strip():
            continue

        if is_calc_column_candidate(calc):
            cc = dict(calc)
            cc['spark_type'] = calc_spark_type(calc.get('datatype', 'string'))
            calc_columns.append(cc)
        else:
            measures.append(calc)

    return calc_columns, measures


def sanitize_calc_col_name(name):
    """Sanitize a calculated-column name for Delta Lake / Spark."""
    name = name.replace('[', '').replace(']', '')
    name = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    name = re.sub(r'^[0-9]+', '_', name)
    name = re.sub(r'_+', '_', name).strip('_')
    return name.lower() or 'calc_col'


def tableau_formula_to_m(formula):
    """Best-effort conversion of a Tableau formula to a Power Query M
    row expression (for use inside ``each …``).
    """
    if not formula:
        return ""
    m = formula.strip()

    # Pre-process ELSEIF → else if (before keyword lowering)
    m = re.sub(r'\bELSEIF\b', 'ELSE IF', m, flags=re.IGNORECASE)

    m = re.sub(r'\bIF\b', 'if', m, flags=re.IGNORECASE)
    m = re.sub(r'\bTHEN\b', 'then', m, flags=re.IGNORECASE)
    m = re.sub(r'\bELSE\b', 'else', m, flags=re.IGNORECASE)
    m = re.sub(r'\bEND\b', '', m, flags=re.IGNORECASE)

    # Ensure every M 'if...then' has a matching 'else' (Power Query M requires it).
    # Count if/then/else outside string literals.
    stripped = re.sub(r'"([^"]|"")*"', '""', m)
    if_count = len(re.findall(r'\bif\b', stripped))
    else_count = len(re.findall(r'\belse\b', stripped))
    if if_count > else_count:
        m = m.rstrip() + ' else null' * (if_count - else_count)

    m = re.sub(r'\bAND\b', 'and', m, flags=re.IGNORECASE)
    m = re.sub(r'\bOR\b', 'or', m, flags=re.IGNORECASE)
    m = re.sub(r'\bNOT\b', 'not', m, flags=re.IGNORECASE)

    m = re.sub(r'\bLEFT\s*\(', 'Text.Start(', m, flags=re.IGNORECASE)
    m = re.sub(r'\bRIGHT\s*\(', 'Text.End(', m, flags=re.IGNORECASE)
    m = re.sub(r'\bUPPER\s*\(', 'Text.Upper(', m, flags=re.IGNORECASE)
    m = re.sub(r'\bLOWER\s*\(', 'Text.Lower(', m, flags=re.IGNORECASE)
    m = re.sub(r'\bLEN\s*\(', 'Text.Length(', m, flags=re.IGNORECASE)
    m = re.sub(r'\bTRIM\s*\(', 'Text.Trim(', m, flags=re.IGNORECASE)
    m = re.sub(r'\bROUND\s*\(', 'Number.Round(', m, flags=re.IGNORECASE)
    m = re.sub(r'\bABS\s*\(', 'Number.Abs(', m, flags=re.IGNORECASE)
    m = re.sub(r'\bINT\s*\(', 'Int64.From(', m, flags=re.IGNORECASE)

    return normalize_m_boolean_literals(normalize_m_numeric_literals(m.strip()))


def normalize_m_boolean_literals(expr):
    """Lower boolean literals, leaving strings and field/table names intact."""
    parts = re.split(
        r'("(?:[^"]|"")*"|\'(?:[^\']|\'\')*\'|\[(?:[^\]]|\]\])*\])',
        expr,
    )
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(
            r'\b(TRUE|FALSE)\b(?:\s*\(\s*\))?',
            lambda match: match.group(1).lower(),
            parts[i], flags=re.IGNORECASE,
        )
    return ''.join(parts)


# Characters invalid in M generalized identifiers (must use [#"name"] quoting).
_M_SPECIAL = set('./()\'"+@#$%^&*!~`<>?;:{}|\\,-')

# Tableau and DAX accept a trailing decimal point (`40.`); M does not, and
# inside parentheses the engine reports "Token ')' expected". The guards keep
# `Int64.Type` and `1.5` intact.
_M_TRAILING_DOT_NUMBER = re.compile(r'(?<![\w.])(\d+)\.(?![\w.])')


def normalize_m_numeric_literals(m_expr):
    """Rewrite `40.` as `40` so the M parser accepts the literal."""
    if not m_expr:
        return m_expr
    return _M_TRAILING_DOT_NUMBER.sub(r'\1', m_expr)


def _m_identifier_needs_quoting(name):
    """True when ``name`` cannot appear bare inside ``[...]`` in M.

    A generalized identifier is a run of parts joined by *single* spaces, so
    Tableau names that keep an edge space (``'Montant Articles '``), a double
    space, or a tab are rejected by the M engine as "Invalid identifier" even
    though they contain no special character.

    The test is a whitelist on purpose. A blacklist of forbidden characters
    has twice let one through -- whitespace, then the curly apostrophe U+2019
    -- and each time Power BI refused to load the whole model.
    """
    if not name:
        return True
    if name != name.strip() or '  ' in name:
        return True
    return not all(ch.isalpha() or ch.isdigit() or ch in '_ ' for ch in name)


def _quote_m_ids(m_expr):
    """Quote [field] refs containing chars invalid in M generalized identifiers."""
    if not m_expr:
        return m_expr
    def _repl(match):
        name = match.group(1)
        if name.startswith('#"') or '=' in name:
            return match.group(0)
        if _m_identifier_needs_quoting(name):
            return f'[#"{name}"]'
        return match.group(0)
    return re.sub(r'\[([^\]]+)\]', _repl, m_expr)


def make_m_add_column_step(formula, col_name, prev_step):
    """Return a Power Query M ``Table.AddColumn`` step string.

    Returns:
        ``(m_line, step_name)`` tuple.
    """
    safe_col = col_name.replace('"', '""')
    m_expr = _quote_m_ids(tableau_formula_to_m(formula))
    step_name = f'CalcCol_{sanitize_calc_col_name(col_name)}'
    line = f'    {step_name} = Table.AddColumn({prev_step}, "{safe_col}", each {m_expr})'
    return line, step_name


def tableau_formula_to_pyspark(formula, col_name):
    """Return the PySpark ``.withColumn()`` statement for a Tableau formula.

    Fields are treated as columns of unknown type, so ``+`` between two
    untyped fields is refused; the planner in ``calc_column_plan`` supplies
    datatypes.  A formula that cannot be converted faithfully yields a
    comment-only ``# TODO`` block, which is valid Python, rather than code
    that fails or computes something else.
    """
    conversion = convert_to_pyspark(formula)
    if conversion.ok:
        return with_column_statement(col_name, conversion.expression)
    return todo_statement(col_name, conversion.reason, formula)
