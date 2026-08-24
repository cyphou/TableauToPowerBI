"""Sprint 182 — Custom SQL & Native Query Depth.

Lightweight, regex-based analyzer for the custom SQL / native queries that
Tableau embeds in ``custom_sql`` datasources. It is *not* a full SQL parser:
it extracts enough structure (select list + aliases, source tables, join
clauses, WHERE / GROUP BY / ORDER BY fragments, parameter placeholders and
SQL dialect) to:

* drive a parameterised ``Value.NativeQuery`` M step,
* surface complexity signals in the assessment, and
* infer column names/types so downstream TMDL generation has metadata.

No external dependencies — pure ``re`` + stdlib.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "SqlColumn",
    "SqlAnalysis",
    "analyze_sql",
    "detect_dialect",
    "extract_parameters",
    "to_native_query_m",
]

# ── Dialect detection ────────────────────────────────────────────────────────

_DIALECT_SIGNALS = {
    "tsql": [r"\bTOP\s+\d+", r"\bISNULL\s*\(", r"\bGETDATE\s*\(", r"\[\w+\]", r"\bNVARCHAR\b"],
    "postgres": [r"::\w+", r"\bILIKE\b", r"\bNOW\s*\(\)", r"\bSERIAL\b", r'"\w+"::'],
    "mysql": [r"`\w+`", r"\bLIMIT\s+\d+", r"\bIFNULL\s*\(", r"\bGROUP_CONCAT\s*\("],
    "oracle": [r"\bROWNUM\b", r"\bNVL\s*\(", r"\bSYSDATE\b", r"\bDUAL\b", r"\bCONNECT\s+BY\b"],
    "snowflake": [r"\bIFF\s*\(", r"\bLATERAL\s+FLATTEN", r"\bQUALIFY\b", r"::VARIANT"],
    "bigquery": [r"`[\w.-]+\.[\w.-]+`", r"\bSAFE_CAST\s*\(", r"\bUNNEST\s*\(", r"\bARRAY_AGG\s*\("],
}

# ── SQL type → Power BI/TMDL data type ──────────────────────────────────────

_TYPE_MAP = {
    "int": "int64", "integer": "int64", "bigint": "int64", "smallint": "int64",
    "tinyint": "int64", "serial": "int64", "bigserial": "int64",
    "decimal": "decimal", "numeric": "decimal", "money": "decimal", "number": "decimal",
    "float": "double", "double": "double", "real": "double", "double precision": "double",
    "bit": "boolean", "boolean": "boolean", "bool": "boolean",
    "date": "dateTime", "datetime": "dateTime", "datetime2": "dateTime",
    "timestamp": "dateTime", "time": "dateTime", "smalldatetime": "dateTime",
    "varchar": "string", "nvarchar": "string", "char": "string", "nchar": "string",
    "text": "string", "ntext": "string", "string": "string", "uuid": "string",
}

# Aggregation functions → likely numeric output type
_NUMERIC_AGGS = {"sum", "count", "avg", "min", "max", "count_big", "stddev", "variance"}


@dataclass
class SqlColumn:
    """A single projected column in a SELECT list."""
    expression: str
    alias: Optional[str] = None
    source_column: Optional[str] = None
    inferred_type: str = "string"
    is_aggregate: bool = False

    @property
    def name(self) -> str:
        return self.alias or self.source_column or self.expression


@dataclass
class SqlAnalysis:
    """Structured result of analyzing a custom SQL query."""
    raw_sql: str
    dialect: str = "ansi"
    columns: list = field(default_factory=list)        # list[SqlColumn]
    tables: list = field(default_factory=list)         # list[str]
    joins: list = field(default_factory=list)          # list[dict]
    where: Optional[str] = None
    group_by: list = field(default_factory=list)       # list[str]
    order_by: list = field(default_factory=list)       # list[str]
    parameters: list = field(default_factory=list)     # list[str]
    has_subquery: bool = False
    is_select_star: bool = False
    grade: str = "GREEN"

    def to_dict(self) -> dict:
        return {
            "dialect": self.dialect,
            "columns": [
                {
                    "name": c.name,
                    "expression": c.expression,
                    "alias": c.alias,
                    "type": c.inferred_type,
                    "is_aggregate": c.is_aggregate,
                }
                for c in self.columns
            ],
            "tables": self.tables,
            "joins": self.joins,
            "where": self.where,
            "group_by": self.group_by,
            "order_by": self.order_by,
            "parameters": self.parameters,
            "has_subquery": self.has_subquery,
            "is_select_star": self.is_select_star,
            "grade": self.grade,
        }


# ── Helpers ──────────────────────────────────────────────────────────────────

def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return sql


def _normalize_ws(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def _split_top_level_commas(text: str) -> list:
    """Split on commas not enclosed in parentheses."""
    parts, depth, buf = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return [p for p in parts if p]


def detect_dialect(sql: str) -> str:
    """Return the most likely SQL dialect for *sql* (ansi if no signal)."""
    best, best_score = "ansi", 0
    for dialect, patterns in _DIALECT_SIGNALS.items():
        score = sum(1 for p in patterns if re.search(p, sql, re.IGNORECASE))
        if score > best_score:
            best, best_score = dialect, score
    return best


def extract_parameters(sql: str) -> list:
    """Extract distinct parameter placeholders from *sql*.

    Recognises Tableau ``<Parameters.Name>``, ``@name``, ``:name`` and
    ``${name}`` styles. Returns a de-duplicated, order-preserving list.
    """
    found: list = []
    seen = set()
    patterns = [
        r"<Parameters\.([\w ]+)>",
        r"\[Parameters\]\.\[([\w ]+)\]",
        r"@(\w+)",
        r"(?<![:\w]):(\w+)",       # :name but not ::cast
        r"\$\{(\w+)\}",
    ]
    for pat in patterns:
        for m in re.finditer(pat, sql):
            name = m.group(1).strip()
            key = name.lower()
            if key and key not in seen:
                seen.add(key)
                found.append(name)
    return found


def _infer_type(expr: str) -> tuple:
    """Return (inferred_type, is_aggregate) for a select expression."""
    low = expr.lower().strip()
    # CAST/CONVERT to explicit type
    cast = re.search(r"cast\s*\(.+?\bas\s+(\w+)", low)
    if cast:
        return _TYPE_MAP.get(cast.group(1), "string"), False
    conv = re.search(r"::\s*(\w+)", low)
    if conv:
        return _TYPE_MAP.get(conv.group(1), "string"), False
    # Aggregate function
    agg = re.match(r"(\w+)\s*\(", low)
    if agg and agg.group(1) in _NUMERIC_AGGS:
        return "int64" if agg.group(1) in {"count", "count_big"} else "double", True
    # Numeric / date literal heuristics
    if re.match(r"^-?\d+$", low):
        return "int64", False
    if re.match(r"^-?\d+\.\d+$", low):
        return "double", False
    return "string", False


def _parse_select_list(select_clause: str) -> tuple:
    """Return (columns, is_select_star)."""
    select_clause = re.sub(r"^\s*(distinct|top\s+\d+|all)\s+", "", select_clause, flags=re.IGNORECASE)
    if select_clause.strip() == "*" or re.match(r"^\w+\.\*$", select_clause.strip()):
        return [], True
    columns = []
    for part in _split_top_level_commas(select_clause):
        if part.strip() == "*":
            return [], True
        alias = None
        source_col = None
        # explicit AS alias
        m = re.search(r"\s+as\s+([\"\[`]?[\w ]+[\"\]`]?)\s*$", part, re.IGNORECASE)
        if m:
            alias = m.group(1).strip("\"[]`")
            expr = part[: m.start()].strip()
        else:
            # implicit "expr alias" (no AS) — only when last token isn't part of a func
            m2 = re.search(r"^(.*\S)\s+([\"\[`]?\w+[\"\]`]?)$", part)
            if m2 and not part.rstrip().endswith(")") and "." not in m2.group(2) \
                    and m2.group(2).strip("\"[]`").lower() not in {"end", "asc", "desc"}:
                expr = m2.group(1).strip()
                # only treat as alias if expr has structure (function/operator)
                if re.search(r"[()+\-*/]|\bcase\b", expr, re.IGNORECASE):
                    alias = m2.group(2).strip("\"[]`")
                else:
                    expr = part.strip()
            else:
                expr = part.strip()
        # source column from dotted ref or bare identifier
        dm = re.search(r"([\w]+)\.([\"\[`]?\w+[\"\]`]?)\s*$", expr)
        if dm:
            source_col = dm.group(2).strip("\"[]`")
        elif re.match(r"^[\"\[`]?\w+[\"\]`]?$", expr):
            source_col = expr.strip("\"[]`")
        inferred, is_agg = _infer_type(expr)
        columns.append(SqlColumn(
            expression=expr,
            alias=alias,
            source_column=source_col,
            inferred_type=inferred,
            is_aggregate=is_agg,
        ))
    return columns, False


def _parse_from_joins(from_clause: str) -> tuple:
    """Return (tables, joins) from a FROM clause."""
    tables, joins = [], []
    # Split on JOIN keywords, keeping the join type
    join_re = re.compile(
        r"\b((?:left|right|full|inner|cross|outer)\s+(?:outer\s+)?)?join\b",
        re.IGNORECASE,
    )
    tokens = join_re.split(from_clause)
    # First token is the base table(s)
    base = tokens[0].strip() if tokens else from_clause.strip()
    for tbl in _split_top_level_commas(base):
        name = _table_name(tbl)
        if name:
            tables.append(name)
    # Remaining come in pairs: (join_type, table+on)
    i = 1
    while i < len(tokens):
        jtype = (tokens[i] or "inner").strip().lower() if i < len(tokens) else "inner"
        rest = tokens[i + 1] if i + 1 < len(tokens) else ""
        on_m = re.search(r"\bon\b(.+)$", rest, re.IGNORECASE | re.DOTALL)
        on_clause = on_m.group(1).strip() if on_m else None
        tbl_part = rest[: on_m.start()].strip() if on_m else rest.strip()
        name = _table_name(tbl_part)
        if name:
            tables.append(name)
            joins.append({
                "type": re.sub(r"\s+", " ", jtype).replace(" outer", "").strip() or "inner",
                "table": name,
                "on": _normalize_ws(on_clause) if on_clause else None,
            })
        i += 2
    # de-dup tables, preserve order
    seen, uniq = set(), []
    for t in tables:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    return uniq, joins


def _table_name(token: str) -> Optional[str]:
    token = token.strip()
    if not token or token.startswith("("):
        return None
    # strip alias: "schema.table t" or "table AS t"
    token = re.sub(r"\s+as\s+\w+$", "", token, flags=re.IGNORECASE)
    parts = token.split()
    name = parts[0] if parts else token
    return name.strip("\"[]`") or None


# ── Public entry point ───────────────────────────────────────────────────────

def analyze_sql(sql: str, dialect: Optional[str] = None) -> SqlAnalysis:
    """Analyze a custom SQL string and return a :class:`SqlAnalysis`."""
    raw = sql or ""
    clean = _normalize_ws(_strip_comments(raw))
    analysis = SqlAnalysis(raw_sql=raw)
    analysis.dialect = dialect or detect_dialect(clean)
    analysis.parameters = extract_parameters(raw)
    analysis.has_subquery = bool(re.search(r"\(\s*select\b", clean, re.IGNORECASE))

    sel_m = re.search(
        r"\bselect\b(.*?)\bfrom\b(.*?)$",
        clean, re.IGNORECASE | re.DOTALL,
    )
    if not sel_m:
        analysis.grade = "RED"
        return analysis

    select_clause = sel_m.group(1).strip()
    remainder = sel_m.group(2).strip()

    # Carve the FROM clause up to the next top-level keyword.
    kw = re.search(
        r"\b(where|group\s+by|having|order\s+by|limit|qualify)\b",
        remainder, re.IGNORECASE,
    )
    from_clause = remainder[: kw.start()].strip() if kw else remainder
    tail = remainder[kw.start():] if kw else ""

    analysis.columns, analysis.is_select_star = _parse_select_list(select_clause)
    analysis.tables, analysis.joins = _parse_from_joins(from_clause)

    where_m = re.search(r"\bwhere\b(.*?)(?:\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b|\bqualify\b|$)",
                        tail, re.IGNORECASE | re.DOTALL)
    if where_m and where_m.group(1).strip():
        analysis.where = _normalize_ws(where_m.group(1))

    gb_m = re.search(r"\bgroup\s+by\b(.*?)(?:\border\s+by\b|\bhaving\b|\blimit\b|\bqualify\b|$)",
                     tail, re.IGNORECASE | re.DOTALL)
    if gb_m and gb_m.group(1).strip():
        analysis.group_by = _split_top_level_commas(_normalize_ws(gb_m.group(1)))

    ob_m = re.search(r"\border\s+by\b(.*?)(?:\blimit\b|\bqualify\b|$)",
                     tail, re.IGNORECASE | re.DOTALL)
    if ob_m and ob_m.group(1).strip():
        analysis.order_by = _split_top_level_commas(_normalize_ws(ob_m.group(1)))

    analysis.grade = _grade(analysis)
    return analysis


def _grade(a: SqlAnalysis) -> str:
    """Migration complexity grade for the SQL."""
    if a.has_subquery or len(a.joins) >= 3:
        return "RED"
    if a.is_select_star or len(a.joins) >= 1 or a.group_by or a.parameters:
        return "YELLOW"
    return "GREEN"


# ── Native query M emission ──────────────────────────────────────────────────

def to_native_query_m(sql: str, server: str, database: str, *,
                      table_name: str = "Query",
                      source_func: str = "Sql.Database",
                      params: Optional[dict] = None,
                      enable_folding: bool = True,
                      dialect: Optional[str] = None) -> str:
    """Build a parameterised ``Value.NativeQuery`` M step for *sql*.

    *params* maps parameter names to default values; when the SQL contains
    Tableau-style ``<Parameters.X>`` placeholders they are rewritten to M
    ``@X`` bind markers and exposed via the optional record argument.
    """
    analysis = analyze_sql(sql, dialect=dialect)
    rewritten, bind_names = _rewrite_placeholders(sql, analysis.parameters)
    params = params or {}

    srv = _m_escape(server)
    db = _m_escape(database)
    sql_lit = rewritten.replace('"', '""')

    lines = ["let", f"    // Native query ({analysis.dialect})"]
    src = f'{source_func}("{srv}", "{db}")'
    if bind_names:
        record_items = ", ".join(
            f'{_safe_param(n)}="{_m_escape(str(params.get(n, "")))}"' for n in bind_names
        )
        opts = "[EnableFolding=true]" if enable_folding else "[]"
        lines.append(
            f'    Source = Value.NativeQuery({src}, "{sql_lit}", [{record_items}], {opts}),'
        )
    else:
        opts = "[EnableFolding=true]" if enable_folding else "[]"
        lines.append(
            f'    Source = Value.NativeQuery({src}, "{sql_lit}", null, {opts}),'
        )
    lines.append("    Result = Source")
    lines.append("in")
    lines.append("    Result")
    return "\n".join(lines)


def _rewrite_placeholders(sql: str, params: list) -> tuple:
    """Rewrite Tableau/colon placeholders to M ``@name`` bind markers.

    Returns (rewritten_sql, ordered_bind_names).
    """
    rewritten = sql
    binds: list = []
    seen = set()

    def _add(name):
        key = name.lower()
        if key not in seen:
            seen.add(key)
            binds.append(name)

    # <Parameters.Name> and [Parameters].[Name]
    def repl_tableau(m):
        name = m.group(1).strip()
        _add(name)
        return "@" + _safe_param(name)

    rewritten = re.sub(r"<Parameters\.([\w ]+)>", repl_tableau, rewritten)
    rewritten = re.sub(r"\[Parameters\]\.\[([\w ]+)\]", repl_tableau, rewritten)

    # :name → @name
    def repl_colon(m):
        name = m.group(1)
        _add(name)
        return "@" + _safe_param(name)

    rewritten = re.sub(r"(?<![:\w]):(\w+)", repl_colon, rewritten)

    # already @name markers
    for m in re.finditer(r"@(\w+)", rewritten):
        _add(m.group(1))

    return rewritten, binds


def _safe_param(name: str) -> str:
    return re.sub(r"\W+", "_", name).strip("_") or "p"


def _m_escape(value: str) -> str:
    """Escape a value for a Power Query M double-quoted string literal.

    In M, the only special character inside a text literal is the double
    quote, escaped by doubling. Backslashes are literal — so a SQL Server
    named instance like ``SERVER\\INSTANCE`` must be left untouched.
    """
    return str(value).replace('"', '""')
