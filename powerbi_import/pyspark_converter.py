"""Tableau calculation -> PySpark ``Column`` expression converter.

Used by the Fabric notebook generator to materialise row-level Tableau
calculations as Delta columns.  The converter parses the Tableau formula with
Tableau's operator precedence, tracks the type of every sub-expression and
emits a PySpark expression built only from ``pyspark.sql.functions`` calls,
``Column`` methods and Python operators.  Anything it cannot translate
faithfully is *refused with a reason* rather than emitted as code that fails
or silently computes something else; callers turn a refusal into a commented
TODO cell.

Supported constructs: arithmetic, comparisons, AND/OR/NOT, IN with a literal
list, IF/ELSEIF/ELSE, simple CASE, IIF, ISNULL, ZN, IFNULL, the numeric
functions ABS/SQRT/EXP/LN/LOG/POWER/SQUARE/PI/ROUND/FLOOR/CEILING/trigonometry,
INT/FLOAT/STR/DATE/DATETIME, UPPER/LOWER/TRIM/LTRIM/RTRIM/LEN/LEFT/RIGHT/MID/
CONTAINS/STARTSWITH/ENDSWITH/REPLACE/FIND/SPLIT (literal arguments where the
Spark function takes a literal), and TODAY/NOW/YEAR/MONTH/DAY/DATEPART/DATEDIFF/
DATETRUNC for the date parts whose Tableau semantics were checked.  Aggregates,
table calculations, LOD expressions, user functions, date arithmetic and
everything else are refused.

Nothing here was executed on Spark.  Semantics rest on the Tableau and PySpark
documentation: Spark's non-ANSI behaviour (NULL on division by zero) is
assumed, comparisons use Spark's binary collation, the result type is whatever
Spark infers, and parameter values are frozen at their migration-time default.
"""

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, NamedTuple

from .pyspark_gate import INT64_MAX, INT64_MIN, validate_statement

logger = logging.getLogger(__name__)

__all__ = [
    'Conversion', 'Ref', 'collect_field_refs', 'contains_lod',
    'convert_to_pyspark', 'literal_ref', 'normalize_datatype',
    'permissive_resolver', 'todo_statement', 'validate_statement',
    'with_column_statement',
    'T_BOOLEAN', 'T_DATE', 'T_DATETIME', 'T_INTEGER', 'T_REAL', 'T_STRING',
    'T_UNKNOWN',
]

T_STRING = 'string'
T_INTEGER = 'integer'
T_REAL = 'real'
T_BOOLEAN = 'boolean'
T_DATE = 'date'
T_DATETIME = 'datetime'
T_NULL = 'null'
T_UNKNOWN = 'unknown'

_NUMERIC = frozenset({T_INTEGER, T_REAL})
_TEMPORAL = frozenset({T_DATE, T_DATETIME})

_TYPE_ALIASES = {
    'string': T_STRING, 'text': T_STRING, 'str': T_STRING,
    'varchar': T_STRING, 'nvarchar': T_STRING, 'char': T_STRING,
    'integer': T_INTEGER, 'int': T_INTEGER, 'int32': T_INTEGER,
    'int64': T_INTEGER, 'long': T_INTEGER, 'bigint': T_INTEGER,
    'smallint': T_INTEGER, 'tinyint': T_INTEGER, 'short': T_INTEGER,
    'real': T_REAL, 'double': T_REAL, 'float': T_REAL, 'float64': T_REAL,
    'decimal': T_REAL, 'number': T_REAL, 'numeric': T_REAL,
    'currency': T_REAL, 'percentage': T_REAL,
    'boolean': T_BOOLEAN, 'bool': T_BOOLEAN,
    'date': T_DATE,
    'datetime': T_DATETIME, 'timestamp': T_DATETIME,
}


def normalize_datatype(value):
    """Map a Tableau/Spark type name to the converter's small type lattice."""
    if not isinstance(value, str):
        return T_UNKNOWN
    return _TYPE_ALIASES.get(value.strip().lower(), T_UNKNOWN)


# Python precedence levels of the generated expressions (higher binds tighter).
_ATOM = 100
_UNARY = 11
_MUL = 10
_ADD = 9
_AND = 7
_OR = 5
_CMP = 4

_MAX_DEPTH = 40

_KEYWORDS = frozenset({
    'IF', 'THEN', 'ELSEIF', 'ELSE', 'END', 'CASE', 'WHEN', 'AND', 'OR',
    'NOT', 'IN', 'TRUE', 'FALSE', 'NULL',
})
_IDENT_RE = re.compile(r'[^\W\d]\w*')
_NUM_RE = re.compile(r'(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?')
_DATE_RE = re.compile(
    r'#([0-9]{4})-([0-9]{2})-([0-9]{2})(?: ([0-9]{2}):([0-9]{2})(?::([0-9]{2}))?)?#')
_OPERATORS = ('<=', '>=', '<>', '!=', '==', '+', '-', '*', '/', '%', '^', '=', '<', '>')
_CMP_OPS = frozenset({'=', '==', '<>', '!=', '<', '<=', '>', '>='})
_PY_CMP = {'=': '==', '==': '==', '<>': '!=', '!=': '!=',
           '<': '<', '<=': '<=', '>': '>', '>=': '>='}

_DATE_PARTS = frozenset({
    'year', 'quarter', 'month', 'dayofyear', 'day', 'weekday', 'week',
    'hour', 'minute', 'second',
    'iso-year', 'iso-quarter', 'iso-week', 'iso-weekday',
})
_BARE_DATE_PARTS = frozenset(part for part in _DATE_PARTS if '-' not in part)
_DATE_PART_FUNCS = frozenset({'DATEPART', 'DATEDIFF', 'DATETRUNC', 'DATEADD', 'DATENAME'})

_AGGREGATES = frozenset({
    'SUM', 'AVG', 'COUNT', 'COUNTD', 'MEDIAN', 'ATTR', 'STDEV', 'STDEVP',
    'VAR', 'VARP', 'PERCENTILE', 'CORR', 'COVAR', 'COVARP', 'COLLECT',
})
_TABLE_CALCS = frozenset({
    'RUNNING_SUM', 'RUNNING_AVG', 'RUNNING_COUNT', 'RUNNING_MAX', 'RUNNING_MIN',
    'WINDOW_SUM', 'WINDOW_AVG', 'WINDOW_COUNT', 'WINDOW_MAX', 'WINDOW_MIN',
    'WINDOW_MEDIAN', 'WINDOW_STDEV', 'WINDOW_STDEVP', 'WINDOW_VAR',
    'WINDOW_VARP', 'WINDOW_PERCENTILE', 'WINDOW_CORR', 'WINDOW_COVAR',
    'WINDOW_COVARP', 'RANK', 'RANK_DENSE', 'RANK_MODIFIED', 'RANK_PERCENTILE',
    'RANK_UNIQUE', 'LOOKUP', 'PREVIOUS_VALUE', 'FIRST', 'LAST', 'INDEX',
    'SIZE', 'TOTAL',
})
_USER_FUNCTIONS = frozenset({'USERNAME', 'FULLNAME', 'USERDOMAIN', 'ISMEMBEROF', 'USER'})

_NOTE_CASE = ('string matching is case-sensitive in Spark; Tableau may ignore '
              'case depending on the data source')
_NOTE_NAN = ("Spark returns NaN outside the function's domain; the Tableau "
             'result there was not verified')
_NOTE_ANSI = 'division by zero yields NULL only while spark.sql.ansi.enabled is false'
_NOTE_ROUND = ('ROUND uses Spark HALF_UP rounding (ties away from zero); '
               'the Tableau tie-breaking rule is not documented')
_NOTE_SUBSTR = ('position or length taken from a column: zero or negative values '
                'follow Spark substr() (Tableau behaviour not verified)')
_NOTE_LOGIC = ('AND/OR follow Spark three-valued logic (NULL AND FALSE is FALSE); the '
               'Tableau documentation says AND returns NULL when either operand is NULL, '
               'which was not verified against the source')
_NOTE_STRCMP = 'string comparison follows Spark (case-sensitive, binary collation)'


def _a(typ):
    """Return ``typ`` with its indefinite article, for refusal messages."""
    return ('an ' if typ == T_INTEGER else 'a ') + typ


class _Unsupported(Exception):
    """Raised for any construct the converter refuses to translate."""


class _ModelOnly(_Unsupported):
    """Raised for a construct that is not row-level by nature (it stays in the model)."""


@dataclass(frozen=True)
class Ref:
    """How the caller resolves a field reference found in a formula.

    kind ``column``: emit ``F.col(name)`` of type ``datatype``.
    kind ``literal``: inline ``value`` (int, float, bool or str) of type ``datatype``.
    kind ``missing``: refuse the formula, ``reason`` says why.
    """

    kind: str
    name: str = ''
    datatype: str = T_UNKNOWN
    value: Any = None
    reason: str = ''
    note: str = ''


def permissive_resolver(qualifier, name):
    """Resolver that accepts every unqualified field as a column of unknown type."""
    if qualifier is not None:
        return Ref('missing', reason=f'qualified field reference [{qualifier}].[{name}]')
    return Ref('column', name=name)


@dataclass
class Conversion:
    """Outcome of :func:`convert_to_pyspark`."""

    expression: str | None = None
    reason: str | None = None
    datatype: str = T_UNKNOWN
    columns: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    model_only: bool = False

    @property
    def ok(self):
        return self.expression is not None


@dataclass
class _E:
    code: str
    kind: str
    typ: str
    prec: int = _ATOM
    value: Any = None
    parts: list | None = None


class _Tok(NamedTuple):
    kind: str
    text: str
    value: Any
    pos: int


_EOF = _Tok('EOF', '', None, -1)


def _bad(pos, reason):
    return _Tok('BAD', '', reason, pos)


# ── Tokenizer ──────────────────────────────────────────────────────

def _scan_string(text, i, toks):
    quote = text[i]
    n = len(text)
    j = i + 1
    buf = []
    backslash = False
    while j < n:
        c = text[j]
        if c == quote:
            if j + 1 < n and text[j + 1] == quote:
                buf.append(quote)
                j += 2
                continue
            if backslash:
                toks.append(_bad(i, 'backslash in string literal'))
            else:
                toks.append(_Tok('STR', text[i:j + 1], ''.join(buf), i))
            return j + 1
        if c == '\\':
            backslash = True
        buf.append(c)
        j += 1
    toks.append(_bad(i, 'unterminated string literal'))
    return n


def _scan_bracket(text, i):
    """Return ``(name, end)`` for the ``[...]`` at ``i``; ``]]`` is an escaped bracket."""
    n = len(text)
    j = i + 1
    buf = []
    while j < n:
        c = text[j]
        if c == ']':
            if j + 1 < n and text[j + 1] == ']':
                buf.append(']')
                j += 2
                continue
            return ''.join(buf), j + 1
        buf.append(c)
        j += 1
    return None, n


def _scan_ref(text, i, toks):
    n = len(text)
    name, j = _scan_bracket(text, i)
    if name is None:
        toks.append(_bad(i, 'unterminated field reference'))
        return n
    qualifier = None
    if text.startswith('.[', j):
        second, k = _scan_bracket(text, j + 1)
        if second is None:
            toks.append(_bad(i, 'unterminated field reference'))
            return n
        qualifier, name, j = name, second, k
        if text.startswith('.[', j):
            while text.startswith('.[', j):
                extra, k = _scan_bracket(text, j + 1)
                j = k
                if extra is None:
                    break
            toks.append(_bad(i, 'multi-level field reference'))
            return j
    if not name:
        toks.append(_bad(i, 'empty field reference'))
    else:
        toks.append(_Tok('REF', text[i:j], (qualifier, name), i))
    return j


def _scan_date(text, i, toks):
    match = _DATE_RE.match(text, i)
    if match:
        year, month, day, hour, minute, second = match.groups()
        try:
            if hour is None:
                value = date(int(year), int(month), int(day)).isoformat()
                toks.append(_Tok('DATE', match.group(0), value, i))
            else:
                stamp = datetime(int(year), int(month), int(day),
                                 int(hour), int(minute), int(second or 0))
                toks.append(_Tok('DATETIME', match.group(0),
                                 stamp.strftime('%Y-%m-%d %H:%M:%S'), i))
        except ValueError:
            toks.append(_bad(i, 'invalid date literal'))
        return match.end()
    end = text.find('#', i + 1)
    toks.append(_bad(i, 'unsupported date literal format'))
    return len(text) if end < 0 else end + 1


def _scan_number(match, text, i, toks):
    raw = match.group(0)
    j = match.end()
    n = len(text)
    if j < n and (text[j].isalnum() or text[j] == '_'):
        while j < n and (text[j].isalnum() or text[j] == '_'):
            j += 1
        toks.append(_bad(i, 'malformed number'))
        return j
    try:
        if any(c in raw for c in '.eE'):
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError(raw)
        else:
            value = int(raw)
            if value > INT64_MAX:
                raise ValueError(raw)
    except ValueError:
        toks.append(_bad(i, 'numeric literal out of range'))
        return j
    toks.append(_Tok('NUM', raw, value, i))
    return j


def _tokenize(text):
    """Split ``text`` into tokens.  Never raises; problems become BAD tokens."""
    toks = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if text.startswith('//', i):
            end = text.find('\n', i)
            i = n if end < 0 else end
            continue
        if text.startswith('/*', i):
            end = text.find('*/', i + 2)
            if end < 0:
                toks.append(_bad(i, 'unterminated comment'))
                break
            i = end + 2
            continue
        if ch in '\'"':
            i = _scan_string(text, i, toks)
            continue
        if ch == '[':
            i = _scan_ref(text, i, toks)
            continue
        if ch == '#':
            i = _scan_date(text, i, toks)
            continue
        match = _NUM_RE.match(text, i)
        if match:
            i = _scan_number(match, text, i, toks)
            continue
        match = _IDENT_RE.match(text, i)
        if match:
            word = match.group(0)
            upper = word.upper() if word.isascii() else word
            if upper in _KEYWORDS:
                toks.append(_Tok('KW', upper, upper, i))
            else:
                toks.append(_Tok('IDENT', word, word, i))
            i = match.end()
            continue
        op = next((o for o in _OPERATORS if text.startswith(o, i)), None)
        if op:
            toks.append(_Tok('OP', op, op, i))
            i += len(op)
            continue
        if ch == '(':
            toks.append(_Tok('LP', ch, ch, i))
        elif ch == ')':
            toks.append(_Tok('RP', ch, ch, i))
        elif ch == ',':
            toks.append(_Tok('COMMA', ch, ch, i))
        elif ch in '{}':
            toks.append(_bad(i, 'LOD expression'))
        else:
            toks.append(_bad(i, f'unexpected character {ch!r}'))
        i += 1
    return toks


def collect_field_refs(formula):
    """Return the ordered, unique ``(qualifier, name)`` field references of a formula."""
    seen = []
    if not isinstance(formula, str):
        return seen
    for tok in _tokenize(formula):
        if tok.kind == 'REF' and tok.value not in seen:
            seen.append(tok.value)
    return seen


def contains_lod(formula):
    """Return True when the formula holds a level-of-detail ``{...}`` expression."""
    if not isinstance(formula, str):
        return False
    return any(tok.kind == 'BAD' and tok.value == 'LOD expression'
               for tok in _tokenize(formula))


def literal_ref(formula):
    """Return a ``literal`` :class:`Ref` when ``formula`` is one number, string or boolean.

    Anything else, including expressions over several tokens, returns ``None``.
    """
    if not isinstance(formula, str):
        return None
    toks = _tokenize(formula)
    negate = len(toks) == 2 and toks[0].kind == 'OP' and toks[0].text == '-'
    if negate:
        toks = toks[1:]
    if len(toks) != 1:
        return None
    tok = toks[0]
    if tok.kind == 'NUM':
        typ = T_INTEGER if isinstance(tok.value, int) else T_REAL
        return Ref('literal', datatype=typ, value=-tok.value if negate else tok.value)
    if negate:
        return None
    if tok.kind == 'STR':
        return Ref('literal', datatype=T_STRING, value=tok.value)
    if tok.kind == 'KW' and tok.text in ('TRUE', 'FALSE'):
        return Ref('literal', datatype=T_BOOLEAN, value=tok.text == 'TRUE')
    return None


# ── Code generation helpers ────────────────────────────────────────

def _wrap(code, prec, need, strict=False):
    if prec < need or (strict and prec == need):
        return f'({code})'
    return code


def _prec(e):
    return _ATOM if e.kind == 'lit' else e.prec


def _py_literal(text):
    """Return ``text`` as a double-quoted Python string literal.

    Characters that are not printable (controls, format characters such as the
    bidirectional overrides, line and paragraph separators, lone surrogates) are
    written as escapes, so text taken from a workbook can neither split the
    statement nor change how it is displayed.
    """
    return ''.join(ch if ch.isprintable() else _escape_char(ch)
                   for ch in json.dumps(str(text), ensure_ascii=False))


def _escape_char(ch):
    code = ord(ch)
    return f'\\u{code:04x}' if code < 0x10000 else f'\\U{code:08x}'


def _col_code(name):
    quoted = name
    if '.' in name or '`' in name or name == '*':
        quoted = '`' + name.replace('`', '``') + '`'
    return f'F.col({_py_literal(quoted)})'


def _one_line(text):
    """Collapse every run of whitespace and non-printable characters to one space."""
    return ' '.join(''.join(ch if ch.isprintable() else ' ' for ch in str(text)).split())


def with_column_statement(col_name, expression):
    """Return the ``df = df.withColumn(...)`` statement for ``expression``."""
    return f'df = df.withColumn({_py_literal(col_name)}, {expression})'


def todo_statement(col_name, reason, formula):
    """Return a comment-only (valid Python) block for a column that was not converted."""
    return (f'# TODO: {_one_line(col_name)} not converted: {_one_line(reason)}\n'
            f'# formula: {_one_line(formula)[:200]}')


_JAVA_REGEX_META = re.compile(r'([\\.\[\]{}()*+?^$|])')


def _java_escape(text):
    return _JAVA_REGEX_META.sub(r'\\\1', text)


def _java_replacement(text):
    return text.replace('\\', '\\\\').replace('$', '\\$')


def _unify(types):
    present = [t for t in types if t != T_NULL]
    if not present:
        return T_NULL
    known = {t for t in present if t != T_UNKNOWN}
    if len(known) > 1:
        if known <= _NUMERIC:
            merged = T_REAL
        elif known <= _TEMPORAL:
            merged = T_DATETIME
        else:
            raise _Unsupported(
                'branches have incompatible types (' + ', '.join(sorted(known)) + ')')
    elif known:
        merged = next(iter(known))
    else:
        return T_UNKNOWN
    return T_UNKNOWN if T_UNKNOWN in present else merged


def _comparable(a, b):
    if T_UNKNOWN in (a, b) or a == b:
        return True
    return (a in _NUMERIC and b in _NUMERIC) or (a in _TEMPORAL and b in _TEMPORAL)


def _arith_type(a, b):
    if T_REAL in (a, b):
        return T_REAL
    if a == T_INTEGER and b == T_INTEGER:
        return T_INTEGER
    return T_UNKNOWN


def _int_literal(name, e, what, minimum=None):
    if e.kind != 'lit' or isinstance(e.value, bool) or not isinstance(e.value, int):
        raise _Unsupported(f'{name} needs an integer literal for the {what}')
    if minimum is not None and e.value < minimum:
        raise _Unsupported(f'{name} {what} must be at least {minimum}')
    return e.value


def _str_literal(name, e, what, allow_empty=False):
    if e.kind != 'lit' or not isinstance(e.value, str):
        raise _Unsupported(f'{name} needs a string literal for the {what}')
    if not allow_empty and e.value == '':
        raise _Unsupported(f'{name} {what} must not be empty')
    return e.value


def _part_literal(name, e):
    if e.kind != 'lit' or not isinstance(e.value, str):
        raise _Unsupported(f'{name} needs a literal date part')
    part = e.value.strip().lower()
    if part not in _DATE_PARTS:
        raise _Unsupported(f"{name}: unsupported date part '{e.value}'")
    return part


def _function_refusal(name):
    """Return the exception refusing a function that has no registered handler."""
    if name in _AGGREGATES:
        return _ModelOnly(f'aggregate function {name} (kept as a DAX measure)')
    if name in _TABLE_CALCS:
        return _ModelOnly(f'table calculation {name} (no row-level equivalent)')
    if name in _USER_FUNCTIONS:
        return _ModelOnly(f'user function {name} (handled by row-level security)')
    return _Unsupported(f'unsupported function {name}')


# ── Parser / code generator ────────────────────────────────────────

class _Parser:
    def __init__(self, tokens, resolve):
        self.toks = tokens
        self.i = 0
        self.resolve = resolve
        self.notes = []
        self.columns = []
        self.depth = 0

    # token access
    def peek(self, offset=0):
        index = self.i + offset
        return self.toks[index] if index < len(self.toks) else _EOF

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def at_kw(self, *words):
        tok = self.peek()
        return tok.kind == 'KW' and tok.text in words

    def at_op(self, *ops):
        tok = self.peek()
        return tok.kind == 'OP' and tok.text in ops

    @staticmethod
    def describe(tok):
        return 'end of formula' if tok.kind == 'EOF' else f"'{tok.text}'"

    def expect_kw(self, word):
        if not self.at_kw(word):
            raise _Unsupported(f'expected {word}, found {self.describe(self.peek())}')
        self.take()

    def expect_kind(self, kind, what):
        if self.peek().kind != kind:
            raise _Unsupported(f'expected {what}, found {self.describe(self.peek())}')
        return self.take()

    def enter(self):
        self.depth += 1
        if self.depth > _MAX_DEPTH:
            raise _Unsupported('formula is nested too deeply')

    def leave(self):
        self.depth -= 1

    def note(self, text):
        if text and text not in self.notes:
            self.notes.append(text)

    # helpers used by the function handlers
    def arity(self, name, args, lo, hi=None):
        hi = lo if hi is None else hi
        if lo <= len(args) <= hi:
            return
        wanted = str(lo) if lo == hi else f'{lo} to {hi}'
        raise _Unsupported(f'{name} expects {wanted} argument(s), got {len(args)}')

    @staticmethod
    def expect(name, e, kinds, what):
        if e.typ == T_UNKNOWN or e.typ in kinds:
            return
        raise _Unsupported(f'{name} expects {what}, got {_a(e.typ)} value')

    @staticmethod
    def col(e):
        return f'F.lit({e.code})' if e.kind == 'lit' else e.code

    def recv(self, e):
        code = self.col(e)
        return code if _prec(e) >= _ATOM else f'({code})'

    def val_parts(self, e):
        """Code and precedence of ``e`` where PySpark also accepts a bare number/string."""
        if e.kind == 'lit' and not isinstance(e.value, bool) and e.value is not None:
            return e.code, e.prec
        return self.col(e), _prec(e)

    def val(self, e):
        return self.val_parts(e)[0]

    def cond(self, e, what='condition'):
        if e.kind == 'lit':
            if isinstance(e.value, bool):
                return f'F.lit({e.code})', _ATOM
            raise _Unsupported(f'{what} is not a boolean expression')
        if e.typ not in (T_BOOLEAN, T_UNKNOWN):
            raise _Unsupported(f'{what} is {_a(e.typ)} value, not a boolean')
        return e.code, e.prec

    # literals and references
    @staticmethod
    def lit(value, typ):
        if isinstance(value, bool) or value is None or isinstance(value, str):
            return _E(repr(value), 'lit', typ, _ATOM, value)
        if isinstance(value, int):
            if not INT64_MIN <= value <= INT64_MAX:
                raise _Unsupported('integer literal out of 64-bit range')
        elif not math.isfinite(value):
            raise _Unsupported('non-finite number literal')
        return _E(repr(value), 'lit', typ, _UNARY if value < 0 else _ATOM, value)

    def reference(self, qualifier, name):
        ref = self.resolve(qualifier, name)
        if ref.kind == 'missing':
            raise _Unsupported(ref.reason or f'unresolved field [{name}]')
        self.note(ref.note)
        typ = normalize_datatype(ref.datatype)
        if ref.kind == 'literal':
            return self._parameter_literal(ref, typ)
        if ref.kind != 'column' or not ref.name:
            raise _Unsupported(f'unresolved field [{name}]')
        if ref.name not in self.columns:
            self.columns.append(ref.name)
        if typ == T_UNKNOWN:
            self.note(f'the type of [{ref.name}] is unknown; the expression assumes '
                      'it fits the way it is used')
        return _E(_col_code(ref.name), 'col', typ, _ATOM)

    def _parameter_literal(self, ref, typ):
        value = ref.value
        if typ == T_INTEGER and isinstance(value, int) and not isinstance(value, bool):
            return self.lit(value, T_INTEGER)
        if typ == T_REAL and isinstance(value, (int, float)) and not isinstance(value, bool):
            return self.lit(float(value), T_REAL)
        if typ == T_BOOLEAN and isinstance(value, bool):
            return self.lit(value, T_BOOLEAN)
        if typ == T_STRING and isinstance(value, str):
            return self.lit(value, T_STRING)
        raise _Unsupported(f'parameter value of type {ref.datatype!r} is not inlined')

    # grammar
    def parse(self):
        result = self.expr()
        tok = self.peek()
        if tok.kind != 'EOF':
            raise _Unsupported(f'unexpected {self.describe(tok)}')
        return result

    def expr(self):
        self.enter()
        try:
            left = self.and_expr()
            while self.at_kw('OR'):
                self.take()
                left = self.logic('|', _OR, left, self.and_expr())
            return left
        finally:
            self.leave()

    def and_expr(self):
        left = self.not_expr()
        while self.at_kw('AND'):
            self.take()
            left = self.logic('&', _AND, left, self.not_expr())
        return left

    def logic(self, op, prec, a, b):
        code_a, prec_a = self.cond(a, f'{"AND" if op == "&" else "OR"} operand')
        code_b, prec_b = self.cond(b, f'{"AND" if op == "&" else "OR"} operand')
        code = f'{_wrap(code_a, prec_a, prec)} {op} {_wrap(code_b, prec_b, prec, strict=True)}'
        self.note(_NOTE_LOGIC)
        return _E(code, 'col', T_BOOLEAN, prec)

    def not_expr(self):
        if not self.at_kw('NOT'):
            return self.cmp_expr()
        self.enter()
        try:
            self.take()
            code, prec = self.cond(self.not_expr(), 'NOT operand')
            return _E(f'~{_wrap(code, prec, _UNARY + 1)}', 'col', T_BOOLEAN, _UNARY)
        finally:
            self.leave()

    def cmp_expr(self):
        left = self.add_expr()
        if self.at_kw('IN'):
            self.take()
            result = self.in_list(left)
        elif self.peek().kind == 'OP' and self.peek().text in _CMP_OPS:
            op = self.take().text
            result = self.compare(op, left, self.add_expr())
        else:
            return left
        nxt = self.peek()
        if (nxt.kind == 'OP' and nxt.text in _CMP_OPS) or self.at_kw('IN'):
            raise _Unsupported('chained comparison')
        return result

    def compare(self, op, a, b):
        if T_NULL in (a.typ, b.typ):
            raise _Unsupported('comparison with NULL (use ISNULL)')
        if not _comparable(a.typ, b.typ):
            raise _Unsupported(f'cannot compare {_a(a.typ)} value with {_a(b.typ)} value')
        if T_STRING in (a.typ, b.typ):
            self.note(_NOTE_STRCMP)
        left = _wrap(self.col(a), _prec(a), _ADD)
        right_code, right_prec = self.val_parts(b)
        right = _wrap(right_code, right_prec, _ADD)
        return _E(f'{left} {_PY_CMP[op]} {right}', 'col', T_BOOLEAN, _CMP)

    def in_list(self, left):
        if self.peek().kind != 'LP':
            raise _Unsupported('IN with a set or a field is not converted (literal list only)')
        self.take()
        items = [self.expr()]
        while self.peek().kind == 'COMMA':
            self.take()
            items.append(self.expr())
        self.expect_kind('RP', "')' after the IN list")
        for item in items:
            if item.kind != 'lit' or item.value is None or isinstance(item.value, bool):
                raise _Unsupported('IN list items must be number or string literals')
        typ = _unify([item.typ for item in items])
        if not _comparable(left.typ, typ):
            raise _Unsupported(f'cannot compare {_a(left.typ)} value with {_a(typ)} list')
        if T_STRING in (left.typ, typ):
            self.note(_NOTE_STRCMP)
        args = ', '.join(item.code for item in items)
        return _E(f'{self.recv(left)}.isin({args})', 'col', T_BOOLEAN, _ATOM)

    def add_expr(self):
        left = self.mul_expr()
        while self.at_op('+', '-'):
            op = self.take().text
            right = self.mul_expr()
            left = self.plus(left, right) if op == '+' else self.binary('-', _ADD, left, right)
        return left

    def mul_expr(self):
        left = self.pow_expr()
        while self.at_op('*', '/', '%'):
            op = self.take().text
            right = self.pow_expr()
            if op == '%':
                raise _Unsupported('modulo (%) is not converted: negative-operand semantics are not verified')
            if op == '/':
                self.note(_NOTE_ANSI)
                left = self.binary('/', _MUL, left, right, T_REAL)
            else:
                left = self.binary('*', _MUL, left, right)
        return left

    def pow_expr(self):
        left = self.unary()
        if not self.at_op('^'):
            return left
        self.take()
        right = self.unary()
        if self.at_op('^'):
            raise _Unsupported('chained power operator')
        self.arith_check('^', left, right)
        return _E(f'F.pow({self.col(left)}, {self.col(right)})', 'col', T_REAL, _ATOM)

    def unary(self):
        if self.at_op('+'):
            raise _Unsupported('unary plus')
        if not self.at_op('-'):
            return self.primary()
        self.enter()
        try:
            self.take()
            operand = self.unary()
        finally:
            self.leave()
        if operand.typ not in _NUMERIC and operand.typ != T_UNKNOWN:
            raise _Unsupported(f'unary minus on {_a(operand.typ)} value')
        if operand.kind == 'lit':
            return self.lit(-operand.value, operand.typ)
        return _E(f'-{_wrap(operand.code, operand.prec, _UNARY + 1)}', 'col', operand.typ, _UNARY)

    def arith_check(self, op, a, b):
        for e in (a, b):
            if e.typ in _TEMPORAL:
                raise _Unsupported('date arithmetic is not converted (use DATEDIFF/DATEADD)')
            if e.typ not in _NUMERIC and e.typ != T_UNKNOWN:
                raise _Unsupported(f"operator '{op}' on {_a(e.typ)} value")

    def binary(self, op, prec, a, b, typ=None):
        self.arith_check(op, a, b)
        left = _wrap(self.col(a), _prec(a), prec)
        right_code, right_prec = self.val_parts(b)
        right = _wrap(right_code, right_prec, prec, strict=True)
        result_type = typ or _arith_type(a.typ, b.typ)
        return _E(f'{left} {op} {right}', 'col', result_type, prec)

    def plus(self, a, b):
        if T_STRING in (a.typ, b.typ):
            for e in (a, b):
                if e.typ not in (T_STRING, T_UNKNOWN):
                    raise _Unsupported(f"'+' mixes a string with {_a(e.typ)} value")
            parts = (a.parts or [a]) + (b.parts or [b])
            code = 'F.concat(' + ', '.join(self.col(part) for part in parts) + ')'
            return _E(code, 'col', T_STRING, _ATOM, parts=parts)
        if a.typ in _TEMPORAL or b.typ in _TEMPORAL:
            raise _Unsupported('date arithmetic is not converted (use DATEDIFF/DATEADD)')
        if a.typ in _NUMERIC or b.typ in _NUMERIC:
            return self.binary('+', _ADD, a, b)
        raise _Unsupported("cannot tell numeric '+' from string concatenation: operand types are unknown")

    def primary(self):
        tok = self.peek()
        kind = tok.kind
        if kind == 'NUM':
            self.take()
            return self.lit(tok.value, T_INTEGER if isinstance(tok.value, int) else T_REAL)
        if kind == 'STR':
            self.take()
            return self.lit(tok.value, T_STRING)
        if kind == 'REF':
            self.take()
            return self.reference(*tok.value)
        if kind == 'DATE':
            self.take()
            return _E(f'F.to_date(F.lit({tok.value!r}))', 'col', T_DATE, _ATOM)
        if kind == 'DATETIME':
            self.take()
            return _E(f'F.to_timestamp(F.lit({tok.value!r}))', 'col', T_DATETIME, _ATOM)
        if kind == 'LP':
            self.take()
            inner = self.expr()
            self.expect_kind('RP', "')'")
            return inner
        if kind == 'KW':
            return self.keyword_primary(tok)
        if kind == 'IDENT':
            if self.peek(1).kind == 'LP':
                return self.call()
            raise _Unsupported(f'unknown identifier {tok.text}')
        raise _Unsupported(f'unexpected {self.describe(tok)}')

    def keyword_primary(self, tok):
        word = tok.text
        if word in ('TRUE', 'FALSE'):
            self.take()
            return self.lit(word == 'TRUE', T_BOOLEAN)
        if word == 'NULL':
            self.take()
            return self.lit(None, T_NULL)
        if word == 'IF':
            return self.if_expr()
        if word == 'CASE':
            return self.case_expr()
        raise _Unsupported(f'unexpected keyword {word}')

    def if_expr(self):
        self.take()
        branches = []
        while True:
            condition = self.expr()
            self.expect_kw('THEN')
            branches.append((condition, self.expr()))
            if self.at_kw('ELSEIF'):
                self.take()
                continue
            break
        otherwise = None
        if self.at_kw('ELSE'):
            self.take()
            otherwise = self.expr()
        self.expect_kw('END')
        pairs = [(self.cond(c, 'IF condition')[0], v) for c, v in branches]
        return self.when_chain(pairs, otherwise)

    def case_expr(self):
        self.take()
        if self.at_kw('WHEN'):
            raise _Unsupported('CASE without an expression (searched CASE) is not converted')
        subject = self.expr()
        branches = []
        while self.at_kw('WHEN'):
            self.take()
            value = self.expr()
            if self.peek().kind == 'COMMA':
                raise _Unsupported('CASE WHEN with several values is not converted')
            self.expect_kw('THEN')
            branches.append((value, self.expr()))
        if not branches:
            raise _Unsupported('CASE needs at least one WHEN')
        otherwise = None
        if self.at_kw('ELSE'):
            self.take()
            otherwise = self.expr()
        self.expect_kw('END')
        pairs = [(self.compare('=', subject, value).code, result) for value, result in branches]
        return self.when_chain(pairs, otherwise)

    def when_chain(self, pairs, otherwise):
        types = [value.typ for _, value in pairs]
        if otherwise is not None:
            types.append(otherwise.typ)
        typ = _unify(types)
        code = ''.join(
            f'{"F.when" if index == 0 else ".when"}({condition}, {self.val(value)})'
            for index, (condition, value) in enumerate(pairs))
        if otherwise is not None:
            code += f'.otherwise({self.val(otherwise)})'
        return _E(code, 'col', typ, _ATOM)

    def call(self):
        name_tok = self.take()
        self.take()
        name = name_tok.text.upper()
        args = []
        if self.peek().kind != 'RP':
            while True:
                if not args and name in _DATE_PART_FUNCS and self.at_bare_date_part():
                    args.append(self.lit(self.take().text.lower(), T_STRING))
                else:
                    args.append(self.expr())
                if self.peek().kind == 'COMMA':
                    self.take()
                    continue
                break
        if self.peek().kind != 'RP':
            raise _Unsupported(f"expected ')' after the arguments of {name}")
        self.take()
        handler = _HANDLERS.get(name)
        if handler is None:
            raise _function_refusal(name)
        return handler(self, name, args)

    def at_bare_date_part(self):
        tok = self.peek()
        return (tok.kind == 'IDENT' and tok.text.lower() in _BARE_DATE_PARTS
                and self.peek(1).kind in ('COMMA', 'RP'))


# ── Function handlers ──────────────────────────────────────────────

def _fn_isnull(p, name, args):
    p.arity(name, args, 1)
    return _E(f'{p.recv(args[0])}.isNull()', 'col', T_BOOLEAN)


def _fn_zn(p, name, args):
    p.arity(name, args, 1)
    x = args[0]
    p.expect(name, x, _NUMERIC, 'a numeric argument')
    return _E(f'F.coalesce({p.col(x)}, F.lit(0))', 'col', x.typ)


def _fn_ifnull(p, name, args):
    p.arity(name, args, 2)
    typ = _unify([args[0].typ, args[1].typ])
    return _E(f'F.coalesce({p.col(args[0])}, {p.col(args[1])})', 'col', typ)


def _fn_iif(p, name, args):
    p.arity(name, args, 3, 4)
    test, prec = p.cond(args[0], 'IIF test')
    negated = f'~{_wrap(test, prec, _UNARY + 1)}'
    typ = _unify([e.typ for e in args[1:]])
    code = f'F.when({test}, {p.val(args[1])}).when({negated}, {p.val(args[2])})'
    if len(args) == 4:
        code += f'.otherwise({p.val(args[3])})'
    return _E(code, 'col', typ)


def _numeric1(spark_fn, result=T_REAL, keep_type=False, note=None):
    def handler(p, name, args):
        p.arity(name, args, 1)
        x = args[0]
        p.expect(name, x, _NUMERIC, 'a numeric argument')
        if note:
            p.note(note)
        typ = (x.typ if x.typ in _NUMERIC else T_UNKNOWN) if keep_type else result
        return _E(f'F.{spark_fn}({p.col(x)})', 'col', typ)
    return handler


def _fn_log(p, name, args):
    p.arity(name, args, 1, 2)
    x = args[0]
    p.expect(name, x, _NUMERIC, 'a numeric argument')
    if len(args) == 1:
        return _E(f'F.log10({p.col(x)})', 'col', T_REAL)
    base = args[1]
    if (base.kind != 'lit' or isinstance(base.value, bool)
            or not isinstance(base.value, (int, float)) or base.value <= 0 or base.value == 1):
        raise _Unsupported('LOG base must be a numeric literal > 0 and != 1')
    return _E(f'F.log({float(base.value)!r}, {p.col(x)})', 'col', T_REAL)


def _fn_power(p, name, args):
    p.arity(name, args, 2)
    p.arith_check('^', args[0], args[1])
    return _E(f'F.pow({p.col(args[0])}, {p.col(args[1])})', 'col', T_REAL)


def _fn_square(p, name, args):
    p.arity(name, args, 1)
    p.expect(name, args[0], _NUMERIC, 'a numeric argument')
    return _E(f'F.pow({p.col(args[0])}, F.lit(2))', 'col', T_REAL)


def _fn_pi(p, name, args):
    p.arity(name, args, 0)
    return p.lit(math.pi, T_REAL)


def _fn_round(p, name, args):
    p.arity(name, args, 1, 2)
    x = args[0]
    p.expect(name, x, _NUMERIC, 'a numeric argument')
    p.note(_NOTE_ROUND)
    typ = x.typ if x.typ in _NUMERIC else T_UNKNOWN
    if len(args) == 1:
        return _E(f'F.round({p.col(x)})', 'col', typ)
    scale = _int_literal(name, args[1], 'precision')
    return _E(f'F.round({p.col(x)}, {scale})', 'col', typ)


def _fn_int(p, name, args):
    p.arity(name, args, 1)
    x = args[0]
    if x.typ == T_STRING:
        raise _Unsupported('INT of a string is not converted (Tableau parses the text; the Spark cast differs)')
    p.expect(name, x, _NUMERIC, 'a numeric argument')
    if x.typ == T_UNKNOWN:
        p.note('INT is converted as a numeric cast; Tableau INT also parses strings')
    return _E(f"{p.recv(x)}.cast('long')", 'col', T_INTEGER, _ATOM)


def _fn_float(p, name, args):
    p.arity(name, args, 1)
    x = args[0]
    if x.typ == T_STRING:
        raise _Unsupported('FLOAT of a string is not converted (Tableau parses the text; the Spark cast differs)')
    p.expect(name, x, _NUMERIC, 'a numeric argument')
    if x.typ == T_UNKNOWN:
        p.note('FLOAT is converted as a numeric cast; Tableau FLOAT also parses strings')
    return _E(f"{p.recv(x)}.cast('double')", 'col', T_REAL, _ATOM)


def _fn_str(p, name, args):
    p.arity(name, args, 1)
    x = args[0]
    if x.typ not in (T_INTEGER, T_STRING):
        raise _Unsupported(f'STR of {_a(x.typ)} value is not converted (text formatting differs)')
    return _E(f"{p.recv(x)}.cast('string')", 'col', T_STRING, _ATOM)


def _temporal_cast(spark_fn, result):
    def handler(p, name, args):
        p.arity(name, args, 1)
        if args[0].typ not in _TEMPORAL:
            raise _Unsupported(f'{name} is only converted for date or datetime arguments')
        return _E(f'F.{spark_fn}({p.col(args[0])})', 'col', result)
    return handler


def _string1(spark_fn, result=T_STRING):
    def handler(p, name, args):
        p.arity(name, args, 1)
        p.expect(name, args[0], {T_STRING}, 'a string argument')
        return _E(f'F.{spark_fn}({p.col(args[0])})', 'col', result)
    return handler


def _fn_left(p, name, args):
    p.arity(name, args, 2)
    s, n = args
    p.expect(name, s, {T_STRING}, 'a string argument')
    if n.kind == 'lit':
        count = _int_literal(name, n, 'length', minimum=0)
        return _E(f'{p.recv(s)}.substr(1, {count})', 'col', T_STRING)
    p.expect(name, n, {T_INTEGER}, 'an integer length')
    p.note(_NOTE_SUBSTR)
    return _E(f'{p.recv(s)}.substr(F.lit(1), {p.col(n)})', 'col', T_STRING)


def _fn_right(p, name, args):
    p.arity(name, args, 2)
    s, n = args
    p.expect(name, s, {T_STRING}, 'a string argument')
    if n.kind == 'lit':
        count = _int_literal(name, n, 'length', minimum=1)
        return _E(f'{p.recv(s)}.substr(-{count}, {count})', 'col', T_STRING)
    p.expect(name, n, {T_INTEGER}, 'an integer length')
    p.note(_NOTE_SUBSTR)
    negated = f'-{_wrap(n.code, n.prec, _UNARY + 1)}'
    return _E(f'{p.recv(s)}.substr({negated}, {p.col(n)})', 'col', T_STRING)


def _fn_mid(p, name, args):
    p.arity(name, args, 2, 3)
    s, start = args[0], args[1]
    length = args[2] if len(args) == 3 else None
    p.expect(name, s, {T_STRING}, 'a string argument')
    if start.kind == 'lit':
        begin = _int_literal(name, start, 'start', minimum=1)
        if length is None:
            tail = f'F.length({p.col(s)})'
            return _E(f'{p.recv(s)}.substr(F.lit({begin}), {tail})', 'col', T_STRING)
        if length.kind == 'lit':
            count = _int_literal(name, length, 'length', minimum=0)
            return _E(f'{p.recv(s)}.substr({begin}, {count})', 'col', T_STRING)
        p.expect(name, length, {T_INTEGER}, 'an integer length')
        p.note(_NOTE_SUBSTR)
        return _E(f'{p.recv(s)}.substr(F.lit({begin}), {p.col(length)})', 'col', T_STRING)
    p.expect(name, start, {T_INTEGER}, 'an integer start')
    p.note(_NOTE_SUBSTR)
    if length is None:
        tail = f'F.length({p.col(s)})'
    elif length.kind == 'lit':
        tail = f'F.lit({_int_literal(name, length, "length", minimum=0)})'
    else:
        p.expect(name, length, {T_INTEGER}, 'an integer length')
        tail = p.col(length)
    return _E(f'{p.recv(s)}.substr({p.col(start)}, {tail})', 'col', T_STRING)


def _string_arg(p, name, e):
    p.expect(name, e, {T_STRING}, 'a string argument')
    if e.kind == 'lit':
        return e.code
    return p.col(e)


def _fn_contains(p, name, args):
    p.arity(name, args, 2)
    p.expect(name, args[0], {T_STRING}, 'a string argument')
    sub = _string_arg(p, name, args[1])
    p.note(_NOTE_CASE)
    return _E(f'{p.recv(args[0])}.contains({sub})', 'col', T_BOOLEAN)


def _fn_startswith(p, name, args):
    p.arity(name, args, 2)
    p.expect(name, args[0], {T_STRING}, 'a string argument')
    sub = _string_arg(p, name, args[1])
    p.note(_NOTE_CASE)
    p.note('STARTSWITH ignores leading spaces of the string (Tableau); Spark ltrim() removes spaces only')
    return _E(f'F.ltrim({p.col(args[0])}).startswith({sub})', 'col', T_BOOLEAN)


def _fn_endswith(p, name, args):
    p.arity(name, args, 2)
    p.expect(name, args[0], {T_STRING}, 'a string argument')
    sub = _string_arg(p, name, args[1])
    p.note(_NOTE_CASE)
    p.note('ENDSWITH ignores trailing spaces of the string (Tableau); Spark rtrim() removes spaces only')
    return _E(f'F.rtrim({p.col(args[0])}).endswith({sub})', 'col', T_BOOLEAN)


def _fn_replace(p, name, args):
    p.arity(name, args, 3)
    p.expect(name, args[0], {T_STRING}, 'a string argument')
    search = _str_literal(name, args[1], 'search string')
    replacement = _str_literal(name, args[2], 'replacement', allow_empty=True)
    code = (f'F.regexp_replace({p.col(args[0])}, {_java_escape(search)!r}, '
            f'{_java_replacement(replacement)!r})')
    return _E(code, 'col', T_STRING)


def _fn_find(p, name, args):
    p.arity(name, args, 2, 3)
    p.expect(name, args[0], {T_STRING}, 'a string argument')
    sub = _str_literal(name, args[1], 'substring')
    p.note(_NOTE_CASE)
    if len(args) == 3:
        begin = _int_literal(name, args[2], 'start', minimum=1)
        return _E(f'F.locate({sub!r}, {p.col(args[0])}, {begin})', 'col', T_INTEGER)
    return _E(f'F.locate({sub!r}, {p.col(args[0])})', 'col', T_INTEGER)


def _fn_split(p, name, args):
    p.arity(name, args, 3)
    p.expect(name, args[0], {T_STRING}, 'a string argument')
    delimiter = _str_literal(name, args[1], 'delimiter')
    token = _int_literal(name, args[2], 'token number')
    if token < 1:
        raise _Unsupported('SPLIT with a negative or zero token number is not converted')
    p.note('SPLIT: empty and out-of-range tokens follow Spark split() semantics '
           '(not verified against Tableau)')
    code = f'F.split({p.col(args[0])}, {_java_escape(delimiter)!r}).getItem({token - 1})'
    return _E(code, 'col', T_STRING)


def _fn_today(p, name, args):
    p.arity(name, args, 0)
    p.note('TODAY() is evaluated when the notebook runs; the column keeps that snapshot')
    return _E('F.current_date()', 'col', T_DATE)


def _fn_now(p, name, args):
    p.arity(name, args, 0)
    p.note('NOW() is evaluated when the notebook runs; the column keeps that snapshot')
    return _E('F.current_timestamp()', 'col', T_DATETIME)


def _date_part1(spark_fn):
    def handler(p, name, args):
        p.arity(name, args, 1)
        p.expect(name, args[0], _TEMPORAL, 'a date argument')
        return _E(f'F.{spark_fn}({p.col(args[0])})', 'col', T_INTEGER)
    return handler


_DATEPART_DATE = {'year': 'year', 'quarter': 'quarter', 'month': 'month',
                  'dayofyear': 'dayofyear', 'day': 'dayofmonth'}
_DATEPART_TIME = {'hour': 'hour', 'minute': 'minute', 'second': 'second'}


def _fn_datepart(p, name, args):
    p.arity(name, args, 2, 3)
    if len(args) == 3:
        raise _Unsupported('DATEPART with a start_of_week argument is not converted')
    part = _part_literal(name, args[0])
    x = args[1]
    p.expect(name, x, _TEMPORAL, 'a date argument')
    if part in _DATEPART_DATE:
        return _E(f'F.{_DATEPART_DATE[part]}({p.col(x)})', 'col', T_INTEGER)
    if part in _DATEPART_TIME:
        if x.typ != T_DATETIME:
            raise _Unsupported(f"DATEPART('{part}') needs a datetime argument")
        return _E(f'F.{_DATEPART_TIME[part]}({p.col(x)})', 'col', T_INTEGER)
    raise _Unsupported(f"DATEPART('{part}') is not converted")


def _fn_datediff(p, name, args):
    p.arity(name, args, 3, 4)
    if len(args) == 4:
        raise _Unsupported('DATEDIFF with a start_of_week argument is not converted')
    part = _part_literal(name, args[0])
    a, b = args[1], args[2]
    p.expect(name, a, _TEMPORAL, 'date arguments')
    p.expect(name, b, _TEMPORAL, 'date arguments')
    start, end = p.col(a), p.col(b)
    if part == 'day':
        return _E(f'F.datediff({end}, {start})', 'col', T_INTEGER)
    if part == 'year':
        return _E(f'F.year({end}) - F.year({start})', 'col', T_INTEGER, _ADD)
    if part == 'month':
        code = (f'(F.year({end}) - F.year({start})) * 12 + '
                f'(F.month({end}) - F.month({start}))')
        return _E(code, 'col', T_INTEGER, _ADD)
    if part == 'quarter':
        code = (f'(F.year({end}) - F.year({start})) * 4 + '
                f'(F.quarter({end}) - F.quarter({start}))')
        return _E(code, 'col', T_INTEGER, _ADD)
    raise _Unsupported(f"DATEDIFF('{part}') is not converted")


def _fn_datetrunc(p, name, args):
    p.arity(name, args, 2, 3)
    if len(args) == 3:
        raise _Unsupported('DATETRUNC with a start_of_week argument is not converted')
    part = _part_literal(name, args[0])
    x = args[1]
    p.expect(name, x, _TEMPORAL, 'a date argument')
    if x.typ == T_DATE:
        if part in ('year', 'quarter', 'month'):
            return _E(f'F.trunc({p.col(x)}, {part!r})', 'col', T_DATE)
        if part == 'day':
            return x
    elif x.typ == T_DATETIME:
        if part in ('year', 'quarter', 'month', 'day', 'hour', 'minute', 'second'):
            return _E(f'F.date_trunc({part!r}, {p.col(x)})', 'col', T_DATETIME)
    else:
        raise _Unsupported('DATETRUNC needs to know whether the argument is a date or a datetime')
    raise _Unsupported(f"DATETRUNC('{part}') is not converted")


def _fn_minmax(p, name, args):
    if len(args) == 1:
        raise _ModelOnly(f'aggregate function {name} (kept as a DAX measure)')
    raise _Unsupported(f'scalar {name} of two values is not converted (NULL handling differs)')


def _unsupported_handler(reason):
    def handler(p, name, args):
        raise _Unsupported(reason)
    return handler


_HANDLERS: dict[str, Callable] = {
    'ISNULL': _fn_isnull,
    'ZN': _fn_zn,
    'IFNULL': _fn_ifnull,
    'IIF': _fn_iif,
    'ABS': _numeric1('abs', keep_type=True),
    'SQRT': _numeric1('sqrt', note=_NOTE_NAN),
    'EXP': _numeric1('exp'),
    'SIN': _numeric1('sin'),
    'COS': _numeric1('cos'),
    'TAN': _numeric1('tan'),
    'ASIN': _numeric1('asin', note=_NOTE_NAN),
    'ACOS': _numeric1('acos', note=_NOTE_NAN),
    'ATAN': _numeric1('atan'),
    'DEGREES': _numeric1('degrees'),
    'RADIANS': _numeric1('radians'),
    'FLOOR': _numeric1('floor', result=T_INTEGER),
    'CEILING': _numeric1('ceil', result=T_INTEGER),
    'LN': _numeric1('log'),
    'LOG': _fn_log,
    'POWER': _fn_power,
    'SQUARE': _fn_square,
    'PI': _fn_pi,
    'ROUND': _fn_round,
    'INT': _fn_int,
    'FLOAT': _fn_float,
    'STR': _fn_str,
    'DATE': _temporal_cast('to_date', T_DATE),
    'DATETIME': _temporal_cast('to_timestamp', T_DATETIME),
    'UPPER': _string1('upper'),
    'LOWER': _string1('lower'),
    'TRIM': _string1('trim'),
    'LTRIM': _string1('ltrim'),
    'RTRIM': _string1('rtrim'),
    'LEN': _string1('length', T_INTEGER),
    'LEFT': _fn_left,
    'RIGHT': _fn_right,
    'MID': _fn_mid,
    'CONTAINS': _fn_contains,
    'STARTSWITH': _fn_startswith,
    'ENDSWITH': _fn_endswith,
    'REPLACE': _fn_replace,
    'FIND': _fn_find,
    'SPLIT': _fn_split,
    'TODAY': _fn_today,
    'NOW': _fn_now,
    'YEAR': _date_part1('year'),
    'MONTH': _date_part1('month'),
    'DAY': _date_part1('dayofmonth'),
    'DATEPART': _fn_datepart,
    'DATEDIFF': _fn_datediff,
    'DATETRUNC': _fn_datetrunc,
    'MIN': _fn_minmax,
    'MAX': _fn_minmax,
    'DATEADD': _unsupported_handler('DATEADD is not converted (month-end clamping and part semantics not verified)'),
    'DIV': _unsupported_handler('DIV is not converted (negative-operand semantics not verified)'),
    'SIGN': _unsupported_handler('SIGN is not converted'),
    'PROPER': _unsupported_handler('PROPER is not converted (Spark initcap differs after hyphens)'),
}


# ── Public entry point ─────────────────────────────────────────────

def _convert(formula, resolve):
    tokens = _tokenize(formula)
    for tok in tokens:
        if tok.kind == 'BAD':
            raise _Unsupported(tok.value)
    parser = _Parser(tokens, resolve)
    result = parser.parse()
    if result.typ == T_NULL:
        raise _Unsupported('the formula is always NULL')
    expression = _Parser.col(result)
    rejected = validate_statement(with_column_statement('_check', expression))
    if rejected:
        logger.warning('PySpark converter output rejected by the gate: %s', rejected)
        raise _Unsupported(f'generated code rejected by validation: {rejected}')
    return Conversion(expression, None, result.typ, list(parser.columns), list(parser.notes))


def convert_to_pyspark(formula, resolve=None):
    """Convert a Tableau calculation to a PySpark ``Column`` expression.

    ``resolve(qualifier, name)`` returns a :class:`Ref`; the default accepts any
    unqualified field as a column of unknown type.  The result is either an
    expression (``Conversion.ok``) or a refusal reason, never an exception.
    """
    if not isinstance(formula, str) or not formula.strip():
        return Conversion(reason='empty formula')
    try:
        return _convert(formula, resolve or permissive_resolver)
    except _ModelOnly as exc:
        return Conversion(reason=str(exc), model_only=True)
    except _Unsupported as exc:
        return Conversion(reason=str(exc))
    except RecursionError:
        return Conversion(reason='formula is too deeply nested')
    except Exception as exc:  # a converter bug must not abort the migration
        logger.warning('PySpark converter failed: %s: %s', type(exc).__name__, exc)
        return Conversion(reason=f'internal converter error: {type(exc).__name__}: {exc}')
