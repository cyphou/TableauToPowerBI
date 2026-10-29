"""Default-deny validation of the PySpark statements generated for calculated columns.

Every ``df = df.withColumn("<name>", <expression>)`` statement that the Fabric
notebook generator writes for a Tableau calculated column goes through
:func:`validate_statement` first.  The check is an allow-list over the Python
AST: only the node types, ``pyspark.sql.functions`` calls and ``Column`` methods
listed below are accepted, every argument must be the kind PySpark expects, and
anything else is refused with a reason instead of being written into a notebook
cell that would fail when it runs.

What the gate establishes: the statement is one ``withColumn`` assignment, its
expression is built only from allow-listed calls and operators, bare Python
literals never stand where PySpark would read them as a column name, every
``Column`` operator has a ``Column`` operand (so Python never evaluates it
eagerly), and, when a column list is given, every ``F.col`` names a known
column.  What it does not establish: Spark semantics.  Nothing here executes
Spark.
"""

import ast
import math

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

_MAX_DEPTH = 120
_MAX_ARGS = 255

_ARITHMETIC = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod)
_COMPARISONS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)

TRUNC_FORMATS = frozenset({'year', 'quarter', 'month'})
DATE_TRUNC_FORMATS = frozenset(
    {'year', 'quarter', 'month', 'day', 'hour', 'minute', 'second'})
CAST_TYPES = frozenset({'long', 'double', 'string'})

# name -> (min args, max args, per-argument shapes; the last shape repeats)
#   col   a Column expression
#   val   a Column expression or a number/string literal
#   str   a string literal          sstr  a non-empty string literal
#   int   an integer literal        lit   a scalar literal
_F_FUNCTIONS = {
    'col': (1, 1, ('str',)),
    'lit': (1, 1, ('lit',)),
    'when': (2, 2, ('col', 'val')),
    'coalesce': (1, _MAX_ARGS, ('col',)),
    'concat': (1, _MAX_ARGS, ('col',)),
    'abs': (1, 1, ('col',)),
    'sqrt': (1, 1, ('col',)),
    'exp': (1, 1, ('col',)),
    'sin': (1, 1, ('col',)),
    'cos': (1, 1, ('col',)),
    'tan': (1, 1, ('col',)),
    'asin': (1, 1, ('col',)),
    'acos': (1, 1, ('col',)),
    'atan': (1, 1, ('col',)),
    'degrees': (1, 1, ('col',)),
    'radians': (1, 1, ('col',)),
    'floor': (1, 1, ('col',)),
    'ceil': (1, 1, ('col',)),
    'log10': (1, 1, ('col',)),
    'log': (1, 2, None),
    'pow': (2, 2, ('col', 'col')),
    'round': (1, 2, ('col', 'int')),
    'to_date': (1, 1, ('col',)),
    'to_timestamp': (1, 1, ('col',)),
    'upper': (1, 1, ('col',)),
    'lower': (1, 1, ('col',)),
    'trim': (1, 1, ('col',)),
    'ltrim': (1, 1, ('col',)),
    'rtrim': (1, 1, ('col',)),
    'length': (1, 1, ('col',)),
    'regexp_replace': (3, 3, ('col', 'str', 'str')),
    'locate': (2, 3, ('sstr', 'col', 'int')),
    'split': (2, 2, ('col', 'sstr')),
    'current_date': (0, 0, ()),
    'current_timestamp': (0, 0, ()),
    'year': (1, 1, ('col',)),
    'month': (1, 1, ('col',)),
    'dayofmonth': (1, 1, ('col',)),
    'quarter': (1, 1, ('col',)),
    'dayofyear': (1, 1, ('col',)),
    'hour': (1, 1, ('col',)),
    'minute': (1, 1, ('col',)),
    'second': (1, 1, ('col',)),
    'datediff': (2, 2, ('col', 'col')),
    'trunc': (2, 2, ('col', 'str')),
    'date_trunc': (2, 2, ('str', 'col')),
}

_COLUMN_METHODS = {
    'isNull': (0, 0, ()),
    'isNotNull': (0, 0, ()),
    'cast': (1, 1, ('str',)),
    'contains': (1, 1, ('val',)),
    'startswith': (1, 1, ('val',)),
    'endswith': (1, 1, ('val',)),
    'substr': (2, 2, ('val', 'val')),
    'isin': (1, _MAX_ARGS, ('val',)),
    'otherwise': (1, 1, ('val',)),
    'when': (2, 2, ('col', 'val')),
    'getItem': (1, 1, ('int',)),
}

_SHAPE_TEXT = {
    'col': 'a Column expression',
    'val': 'a Column expression or a number/string literal',
    'str': 'a string literal',
    'sstr': 'a non-empty string literal',
    'int': 'an integer literal',
    'lit': 'a scalar literal',
}


class _Reject(Exception):
    """Carries the reason a statement is refused."""


def _fold(name):
    return name.casefold()


def _unquote(name):
    if len(name) >= 2 and name[0] == '`' and name[-1] == '`':
        return name[1:-1].replace('``', '`')
    return name


def _shape_ok(shape, kind, value):
    if shape == 'col':
        return kind == 'col'
    if shape == 'val':
        return kind in ('col', 'num', 'str')
    if shape == 'str':
        return kind == 'str'
    if shape == 'sstr':
        return kind == 'str' and value != ''
    if shape == 'int':
        return kind == 'num' and isinstance(value, int)
    if shape == 'lit':
        return kind in ('str', 'num', 'bool', 'none')
    return False


def _check_shapes(label, shapes, checked):
    for index, (kind, value) in enumerate(checked):
        shape = shapes[min(index, len(shapes) - 1)]
        if not _shape_ok(shape, kind, value):
            raise _Reject(
                f'{label} argument {index + 1} must be {_SHAPE_TEXT[shape]}')


def _when_chain_state(node):
    """'open' for F.when(...)/.when(...), 'closed' once .otherwise(...) is applied."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return None
    func = node.func
    if isinstance(func.value, ast.Name):
        return 'open' if func.value.id == 'F' and func.attr == 'when' else None
    if func.attr == 'when' and _when_chain_state(func.value) == 'open':
        return 'open'
    if func.attr == 'otherwise' and _when_chain_state(func.value) == 'open':
        return 'closed'
    return None


class _Checker:
    def __init__(self, available):
        self.available = available

    def check(self, node, depth=0):
        """Return ``(kind, value)``; kind is col, num, str, bool or none."""
        if depth > _MAX_DEPTH:
            raise _Reject('expression is nested too deeply')
        handler = _NODE_HANDLERS.get(type(node))
        if handler is None:
            raise _Reject(f'{type(node).__name__} is not allowed')
        return handler(self, node, depth)

    def _constant(self, node, depth):
        value = node.value
        if isinstance(value, bool):
            return 'bool', value
        if isinstance(value, int):
            if not INT64_MIN <= value <= INT64_MAX:
                raise _Reject('integer literal out of 64-bit range')
            return 'num', value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise _Reject('non-finite number literal')
            return 'num', value
        if isinstance(value, str):
            return 'str', value
        if value is None:
            return 'none', None
        raise _Reject(f'{type(value).__name__} literal is not allowed')

    def _unary(self, node, depth):
        kind, value = self.check(node.operand, depth + 1)
        if isinstance(node.op, ast.USub):
            if kind == 'num':
                negated = -value
                if isinstance(negated, int) and not INT64_MIN <= negated <= INT64_MAX:
                    raise _Reject('integer literal out of 64-bit range')
                return 'num', negated
            if kind == 'col':
                return 'col', None
            raise _Reject('unary minus needs a number or a Column')
        if isinstance(node.op, ast.Invert):
            if kind == 'col':
                return 'col', None
            raise _Reject("'~' needs a Column operand")
        raise _Reject(f'operator {type(node.op).__name__} is not allowed')

    def _binop(self, node, depth):
        left, _ = self.check(node.left, depth + 1)
        right, _ = self.check(node.right, depth + 1)
        if isinstance(node.op, (ast.BitAnd, ast.BitOr)):
            if left == 'col' and right == 'col':
                return 'col', None
            raise _Reject("'&' and '|' need Column operands")
        if isinstance(node.op, _ARITHMETIC):
            if left != 'col':
                raise _Reject('the left operand of an arithmetic operator must be a Column')
            if right not in ('col', 'num'):
                raise _Reject('the right operand of an arithmetic operator must be a Column or a number')
            return 'col', None
        raise _Reject(f'operator {type(node.op).__name__} is not allowed')

    def _compare(self, node, depth):
        if len(node.ops) != 1 or len(node.comparators) != 1:
            raise _Reject('chained comparisons are not allowed')
        if not isinstance(node.ops[0], _COMPARISONS):
            raise _Reject(f'operator {type(node.ops[0]).__name__} is not allowed')
        left, _ = self.check(node.left, depth + 1)
        right, _ = self.check(node.comparators[0], depth + 1)
        if left != 'col':
            raise _Reject('the left side of a comparison must be a Column')
        if right not in ('col', 'num', 'str'):
            raise _Reject('the right side of a comparison must be a Column, a number or a string')
        return 'col', None

    def _call(self, node, depth):
        if node.keywords:
            raise _Reject('keyword arguments are not allowed')
        func = node.func
        if not isinstance(func, ast.Attribute):
            raise _Reject('only F.<function>() and Column methods may be called')
        if isinstance(func.value, ast.Name) and func.value.id == 'F':
            return self._f_call(func.attr, node.args, depth)
        return self._method_call(func, node.args, depth)

    @staticmethod
    def _arity(label, lo, hi, args):
        if not lo <= len(args) <= hi:
            wanted = str(lo) if lo == hi else f'{lo} to {hi}'
            raise _Reject(f'{label} takes {wanted} argument(s), got {len(args)}')

    def _f_call(self, name, args, depth):
        spec = _F_FUNCTIONS.get(name)
        if spec is None:
            raise _Reject(f'F.{name}() is not allowed')
        lo, hi, shapes = spec
        label = f'F.{name}()'
        self._arity(label, lo, hi, args)
        checked = [self.check(arg, depth + 1) for arg in args]
        if name == 'log':
            self._check_log(checked)
        else:
            _check_shapes(label, shapes, checked)
        if name == 'col':
            self._check_column_name(checked[0][1])
        elif name == 'locate' and len(checked) == 3 and checked[2][1] < 1:
            raise _Reject('F.locate() position must be at least 1')
        elif name == 'trunc' and checked[1][1] not in TRUNC_FORMATS:
            raise _Reject(f'F.trunc() format {checked[1][1]!r} is not allowed')
        elif name == 'date_trunc' and checked[0][1] not in DATE_TRUNC_FORMATS:
            raise _Reject(f'F.date_trunc() format {checked[0][1]!r} is not allowed')
        return 'col', None

    @staticmethod
    def _check_log(checked):
        if len(checked) == 1:
            if checked[0][0] != 'col':
                raise _Reject('F.log() argument 1 must be a Column expression')
            return
        base_kind, base = checked[0]
        if base_kind != 'num' or not isinstance(base, float) or base <= 0 or base == 1:
            raise _Reject('F.log() base must be a float literal > 0 and != 1')
        if checked[1][0] != 'col':
            raise _Reject('F.log() argument 2 must be a Column expression')

    def _check_column_name(self, name):
        if not name:
            raise _Reject('F.col() needs a non-empty column name')
        if self.available is not None and _fold(_unquote(name)) not in self.available:
            raise _Reject(f'unknown column {name!r}')

    def _method_call(self, func, args, depth):
        name = func.attr
        spec = _COLUMN_METHODS.get(name)
        if spec is None:
            raise _Reject(f'.{name}() is not allowed')
        lo, hi, shapes = spec
        label = f'.{name}()'
        self._arity(label, lo, hi, args)
        if name in ('when', 'otherwise'):
            if _when_chain_state(func.value) != 'open':
                raise _Reject(f'{label} needs an open F.when(...) chain')
        receiver, _ = self.check(func.value, depth + 1)
        if receiver != 'col':
            raise _Reject(f'{label} must be called on a Column')
        checked = [self.check(arg, depth + 1) for arg in args]
        _check_shapes(label, shapes, checked)
        if name == 'cast' and checked[0][1] not in CAST_TYPES:
            raise _Reject(f'.cast() type {checked[0][1]!r} is not allowed')
        elif name in ('contains', 'startswith', 'endswith') and checked[0][0] == 'num':
            raise _Reject(f'{label} takes a Column or a string')
        elif name == 'substr':
            kinds = {kind for kind, _ in checked}
            ints = all(kind == 'num' and isinstance(value, int) for kind, value in checked)
            if not (ints or kinds == {'col'}):
                raise _Reject('.substr() needs two integer literals or two Columns')
        elif name == 'isin' and any(kind == 'col' for kind, _ in checked):
            raise _Reject('.isin() takes literal values only')
        elif name == 'getItem' and checked[0][1] < 0:
            raise _Reject('.getItem() index must not be negative')
        return 'col', None


_NODE_HANDLERS = {
    ast.Constant: _Checker._constant,
    ast.UnaryOp: _Checker._unary,
    ast.BinOp: _Checker._binop,
    ast.Compare: _Checker._compare,
    ast.Call: _Checker._call,
}


def _validate(statement, available_columns):
    if not isinstance(statement, str) or not statement.strip():
        raise _Reject('empty statement')
    try:
        tree = ast.parse(statement)
    except SyntaxError as exc:
        raise _Reject(f'not valid Python: {exc.msg}') from None
    if len(tree.body) != 1:
        raise _Reject('expected exactly one statement')
    node = tree.body[0]
    if not (isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name) and node.targets[0].id == 'df'):
        raise _Reject("expected 'df = df.withColumn(...)'")
    call = node.value
    func = call.func if isinstance(call, ast.Call) else None
    if not (isinstance(func, ast.Attribute) and func.attr == 'withColumn'
            and isinstance(func.value, ast.Name) and func.value.id == 'df'):
        raise _Reject("expected 'df = df.withColumn(...)'")
    if call.keywords or len(call.args) != 2:
        raise _Reject('withColumn takes exactly two positional arguments')
    name_node, expression = call.args
    if not (isinstance(name_node, ast.Constant) and isinstance(name_node.value, str)
            and name_node.value):
        raise _Reject('the column name must be a non-empty string literal')
    available = None
    if available_columns is not None:
        available = {_fold(_unquote(str(name))) for name in available_columns}
    kind, _ = _Checker(available).check(expression)
    if kind != 'col':
        raise _Reject('the expression is a Python literal, not a Column')


def validate_statement(statement, available_columns=None):
    """Return ``None`` when ``statement`` is acceptable, otherwise the reason.

    ``available_columns`` (optional) is every column name the statement may
    read; names are compared case-insensitively, as Spark resolves them.
    """
    try:
        _validate(statement, available_columns)
    except _Reject as exc:
        return str(exc)
    except (ValueError, RecursionError, MemoryError) as exc:
        return f'statement could not be analysed ({type(exc).__name__})'
    return None
