"""What the converted expressions DO, run on the executable model in ``pyspark_stub``.

``test_pyspark_converter`` pins the generated text.  This file pins its behaviour.
Every expected value below was derived by hand from the Tableau documentation, never
copied from the converter's output.  Where the documentation is silent the row sits in
``MODEL`` and says so: the value is what the documented Spark rule gives.

All of it runs on ``tests/pyspark_stub``, a model written from the PySpark documentation
(and, for ``Column.substr``, Spark's ``UTF8String.substringSQL``).  A green run shows that
the generated code means what the reference says *under those documented rules*; it does
not show that a real Spark session agrees, and nothing here ran on one.
"""

import datetime as dt
import math
import operator
import os
import random
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests import pyspark_stub as stub  # noqa: E402
from tests.converter_support import (  # noqa: E402
    TYPES,
    convert,
    expression_of,
    fields_in,
    gate_reason,
)
from tests.test_pyspark_converter import EXACT  # noqa: E402

D = dt.date
T = dt.datetime


def v(**values):
    unknown = set(values) - set(TYPES)
    assert not unknown, f'not a test field: {sorted(unknown)}'
    return values


def many(formula, *cases):
    return [(formula, values, expected) for values, expected in cases]


def row(values):
    full = dict.fromkeys(TYPES)
    full.update(values)
    return full


def run(formula, **values):
    return stub.evaluate(expression_of(formula), row(values))


def same(actual, expected):
    """Equal value AND equal kind: True is not 1, a date is not a datetime, '' is not None."""
    if isinstance(expected, float):
        if math.isnan(expected):
            return isinstance(actual, float) and math.isnan(actual)
        return (isinstance(actual, (int, float)) and not isinstance(actual, bool)
                and actual == pytest.approx(expected))
    return type(actual) is type(expected) and actual == expected


def fits(datatype, value):
    """Is ``value`` a runtime value of the Tableau type the converter declared?"""
    kinds = {'integer': (int,), 'real': (int, float), 'string': (str,),
             'boolean': (bool,), 'date': (D,), 'datetime': (T,)}
    return datatype not in kinds or type(value) in kinds[datatype]


# ── Behaviour the Tableau documentation defines ────────────────────

ARITHMETIC = [
    *many('[a] + [b] * 2', (v(a=3, b=4), 11)),
    *many('([a] + [b]) * 2', (v(a=3, b=4), 14)),
    *many('[a] - [b] - [c]', (v(a=10, b=3, c=2), 5)),
    *many('[a] - ([b] - [c])', (v(a=10, b=3, c=2), 9)),
    *many('[a] / [b] / [c]', (v(a=12, b=3, c=2), 2.0)),
    *many('[a] / ([b] / [c])', (v(a=12, b=3, c=2), 8.0)),
    *many('[a] / [b] * [c]', (v(a=12, b=3, c=2), 8.0)),
    *many('[a] * 2 + [b] * 3', (v(a=1, b=2), 8)),
    *many('[a] / [b]', (v(a=7, b=2), 3.5), (v(a=7, b=0), None), (v(a=0, b=5), 0.0),
          (v(a=None, b=2), None), (v(a=7, b=None), None)),
    *many('[a] * [b]', (v(a=None, b=2), None), (v(a=2, b=None), None), (v(a=-2, b=3), -6)),
    *many('[a] ^ 2', (v(a=3), 9.0), (v(a=-3), 9.0), (v(a=None), None)),
    # Tableau's order of operations puts negation before the power operator.
    *many('-[a] ^ 2', (v(a=3), 9.0)),
    *many('2 * [a] ^ 2', (v(a=3), 18.0)),
    *many('-[a]', (v(a=3), -3), (v(a=-3), 3), (v(a=None), None)),
    *many('-[a] + [b]', (v(a=3, b=5), 2)),
    *many('[a] - -[b]', (v(a=3, b=5), 8)),
    *many('[a] + [x]', (v(a=1, x=0.5), 1.5)),
    *many('[x] * [y]', (v(x=1.5, y=2.0), 3.0)),
]

MATH = [
    *many('ABS([a])', (v(a=-3), 3), (v(a=3), 3), (v(a=None), None)),
    *many('ABS([x])', (v(x=-2.5), 2.5)),
    *many('SQRT([x])', (v(x=9.0), 3.0), (v(x=None), None)),
    *many('FLOOR([x])', (v(x=2.9), 2), (v(x=-2.1), -3), (v(x=None), None)),
    *many('CEILING([x])', (v(x=2.1), 3), (v(x=-2.9), -2), (v(x=None), None)),
    *many('POWER([a], 3)', (v(a=2), 8.0), (v(a=None), None)),
    *many('POWER([x], 0.5)', (v(x=16.0), 4.0)),
    *many('SQUARE([a])', (v(a=4), 16.0)),
    *many('EXP([x])', (v(x=0.0), 1.0), (v(x=1.0), math.e)),
    # LN(e) = 1 whereas log10(e) is about 0.434; LN(1) = 0 = log10(1) would not tell them apart.
    *many('LN([x])', (v(x=1.0), 0.0), (v(x=math.e), 1.0)),
    *many('LOG([x])', (v(x=100.0), 2.0), (v(x=1000.0), 3.0)),
    *many('LOG([x], 2)', (v(x=8.0), 3.0)),
    *many('PI()', (v(), math.pi)),
    # The classic angles: sin and tan, or sin and cos, agree at 0 and could not be told apart there.
    *many('SIN([x])', (v(x=0.0), 0.0), (v(x=math.pi / 2), 1.0), (v(x=math.pi / 6), 0.5)),
    *many('COS([x])', (v(x=0.0), 1.0), (v(x=math.pi), -1.0), (v(x=math.pi / 3), 0.5)),
    *many('TAN([x])', (v(x=0.0), 0.0), (v(x=math.pi / 4), 1.0)),
    *many('ATAN([x])', (v(x=1.0), math.pi / 4)),
    *many('ASIN([x])', (v(x=1.0), math.pi / 2)),
    *many('ACOS([x])', (v(x=1.0), 0.0)),
    *many('DEGREES([x])', (v(x=math.pi), 180.0)),
    *many('RADIANS([x])', (v(x=180.0), math.pi)),
    *many('INT([x])', (v(x=2.9), 2), (v(x=-2.9), -2), (v(x=None), None)),
    *many('INT([a])', (v(a=5), 5)),
    *many('FLOAT([a])', (v(a=3), 3.0), (v(a=None), None)),
    *many('STR([a])', (v(a=-3), '-3'), (v(a=0), '0'), (v(a=None), None)),
    *many('ZN([a])', (v(a=None), 0), (v(a=5), 5)),
    *many('ZN([x])', (v(x=None), 0.0), (v(x=1.5), 1.5)),
]

STRINGS = [
    *many('UPPER([s])', (v(s='abC'), 'ABC'), (v(s=''), ''), (v(s=None), None)),
    *many('LOWER([s])', (v(s='AbC'), 'abc'), (v(s=None), None)),
    *many('TRIM([s])', (v(s='  ab '), 'ab'), (v(s='   '), ''), (v(s=None), None)),
    *many('LTRIM([s])', (v(s='  ab '), 'ab ')),
    *many('RTRIM([s])', (v(s='  ab '), '  ab')),
    *many('LEN([s])', (v(s='abc'), 3), (v(s=''), 0), (v(s=None), None)),
    *many('[s] + [t]', (v(s='a', t='b'), 'ab'), (v(s='a', t=None), None),
          (v(s=None, t='b'), None), (v(s='', t=''), '')),
    *many('[s] + "!"', (v(s='a'), 'a!'), (v(s=None), None)),
    *many('STR([a]) + [s]', (v(a=5, s='x'), '5x')),
    *many('LEFT([s], 3)', (v(s='abcdef'), 'abc'), (v(s='ab'), 'ab'), (v(s=''), ''), (v(s=None), None)),
    *many('LEFT([s], 0)', (v(s='abc'), '')),
    *many('RIGHT([s], 3)', (v(s='abcdef'), 'def'), (v(s='ab'), 'ab'), (v(s=None), None)),
    *many('MID([s], 2, 3)', (v(s='abcdef'), 'bcd'), (v(s='abc'), 'bc'), (v(s='a'), ''), (v(s=None), None)),
    *many('MID([s], 2)', (v(s='abcdef'), 'bcdef'), (v(s='abc'), 'bc'), (v(s='a'), '')),
    *many('MID([s], 9)', (v(s='abc'), '')),
    *many('MID([s], 1)', (v(s='abc'), 'abc')),
    *many('LEFT([s], [n])', (v(s='abcdef', n=2), 'ab'), (v(s='abcdef', n=10), 'abcdef'),
          (v(s='abcdef', n=None), None), (v(s=None, n=2), None)),
    *many('RIGHT([s], [n])', (v(s='abcdef', n=2), 'ef'), (v(s='abcdef', n=10), 'abcdef'),
          (v(s='abcdef', n=None), None)),
    *many('MID([s], [a], [b])', (v(s='abcdef', a=2, b=3), 'bcd'), (v(s='abcdef', a=5, b=10), 'ef')),
    *many('MID([s], [a])', (v(s='abcdef', a=3), 'cdef')),
    *many('UPPER(LEFT([s], 1)) + LOWER(MID([s], 2))', (v(s='hELLO'), 'Hello')),
    *many('CONTAINS([s], "b")', (v(s='abc'), True), (v(s='xyz'), False), (v(s=None), None)),
    *many('CONTAINS([s], "")', (v(s='abc'), True), (v(s=''), True)),
    *many('CONTAINS([s], [t])', (v(s='abc', t='b'), True), (v(s='abc', t=None), None)),
    # Tableau ignores leading (trailing) white space for STARTSWITH (ENDSWITH).
    *many('STARTSWITH([s], "a")', (v(s='abc'), True), (v(s=' abc'), True), (v(s='bca'), False),
          (v(s=None), None)),
    *many('ENDSWITH([s], "c")', (v(s='abc'), True), (v(s='abc  '), True), (v(s='cab'), False),
          (v(s=None), None)),
    # REPLACE matches and substitutes text, never a pattern.
    *many('REPLACE([s], "a", "b")', (v(s='banana'), 'bbnbnb'), (v(s=None), None)),
    *many('REPLACE([s], "a", "")', (v(s='banana'), 'bnn')),
    *many('REPLACE([s], ".", "-")', (v(s='a.b.c'), 'a-b-c'), (v(s='abc'), 'abc')),
    *many('REPLACE([s], "$", "x")', (v(s='a$b'), 'axb')),
    *many('REPLACE([s], "a", "$")', (v(s='banana'), 'b$n$n$')),
    *many('REPLACE([s], "(a)", "[b]")', (v(s='x(a)y'), 'x[b]y'), (v(s='xay'), 'xay')),
    *many('REPLACE([s], "a|b", "x")', (v(s='a|b ab'), 'x ab')),
    *many('REPLACE([s], "^", "x")', (v(s='a^b'), 'axb'), (v(s='ab'), 'ab')),
    *many('REPLACE([s], "[", "(")', (v(s='a[b'), 'a(b')),
    *many('REPLACE([s], "a.b", "$1")', (v(s='xa.by'), 'x$1y'), (v(s='axb'), 'axb')),
    # FIND is one-based and 0 when the text is absent.
    *many('FIND([s], "b")', (v(s='abc'), 2), (v(s='xyz'), 0), (v(s=None), None)),
    *many('FIND([s], "a")', (v(s='abcabc'), 1)),
    # An instance before the start is ignored, one exactly at the start is found.
    *many('FIND([s], "a", 3)', (v(s='abcabc'), 4), (v(s='abc'), 0), (v(s='banana'), 4),
          (v(s='bcab'), 3), (v(s=None), None)),
    # SPLIT counts tokens from one.
    *many('SPLIT([s], "-", 1)', (v(s='a-b-c'), 'a')),
    *many('SPLIT([s], "-", 2)', (v(s='a-b-c'), 'b'), (v(s=None), None)),
    *many('SPLIT([s], "-", 3)', (v(s='a-b-c'), 'c')),
    *many('SPLIT([s], ".", 1)', (v(s='a.b'), 'a')),
    *many('SPLIT([s], "|", 2)', (v(s='a|b'), 'b')),
]

NULLS = [
    *many('IFNULL([a], [b])', (v(a=None, b=2), 2), (v(a=1, b=2), 1), (v(a=None, b=None), None)),
    *many('IFNULL([s], "")', (v(s=None), ''), (v(s='a'), 'a')),
    *many('ISNULL([a])', (v(a=None), True), (v(a=0), False)),
    *many('ISNULL([s])', (v(s=None), True), (v(s=''), False)),
    *many('IF ISNULL([a]) THEN 0 ELSE [a] END', (v(a=None), 0), (v(a=5), 5)),
]

CONDITIONALS = [
    # ELSE catches "none of the tested expressions are true", so a NULL condition reaches it.
    *many('IF [a] > 0 THEN "p" ELSE "n" END', (v(a=1), 'p'), (v(a=0), 'n'), (v(a=None), 'n')),
    *many('IF [a] > 0 THEN 1 END', (v(a=1), 1), (v(a=0), None), (v(a=None), None)),
    *many('IF [a] > 1 THEN "x" ELSEIF [a] > 0 THEN "y" ELSE "z" END',
          (v(a=2), 'x'), (v(a=1), 'y'), (v(a=0), 'z'), (v(a=None), 'z')),
    *many('IF [a] > 1 THEN "x" ELSE IF [a] > 0 THEN "y" ELSE "z" END END',
          (v(a=2), 'x'), (v(a=1), 'y'), (v(a=0), 'z'), (v(a=None), 'z')),
    *many('IF [flag] THEN 1 ELSE 0 END', (v(flag=True), 1), (v(flag=False), 0), (v(flag=None), 0)),
    *many('IF [a] > 0 THEN 1 ELSE 2.5 END', (v(a=1), 1.0), (v(a=0), 2.5)),
    # IIF(test, then, else, [unknown]) returns ``unknown`` when the test is NULL.
    *many('IIF([a] > 1, "x", "y")', (v(a=2), 'x'), (v(a=0), 'y'), (v(a=None), None)),
    *many('IIF([a] > 1, "x", "y", "u")', (v(a=2), 'x'), (v(a=0), 'y'), (v(a=None), 'u')),
    *many('CASE [a] WHEN 1 THEN "x" WHEN 2 THEN "y" ELSE "z" END',
          (v(a=1), 'x'), (v(a=2), 'y'), (v(a=3), 'z')),
    *many('CASE [a] WHEN 1 THEN "x" WHEN 2 THEN "y" END', (v(a=2), 'y'), (v(a=3), None)),
    *many('CASE [s] WHEN "a" THEN 1 WHEN "b" THEN 2 ELSE 3 END',
          (v(s='a'), 1), (v(s='b'), 2), (v(s='c'), 3)),
]

DATES = [
    *many('YEAR([d])', (v(d=D(2024, 3, 15)), 2024), (v(d=None), None)),
    *many('MONTH([d])', (v(d=D(2024, 3, 15)), 3)),
    *many('DAY([d])', (v(d=D(2024, 3, 15)), 15)),
    *many('YEAR([ts])', (v(ts=T(2024, 3, 15, 10, 30, 45)), 2024)),
    *many('DATEPART("quarter", [d])', (v(d=D(2024, 3, 15)), 1), (v(d=D(2024, 4, 1)), 2),
          (v(d=D(2024, 12, 31)), 4)),
    *many('DATEPART("dayofyear", [d])', (v(d=D(2024, 3, 15)), 75), (v(d=D(2023, 3, 15)), 74)),
    *many('DATEPART("hour", [ts])', (v(ts=T(2024, 3, 15, 10, 30, 45)), 10)),
    *many('DATEPART("minute", [ts])', (v(ts=T(2024, 3, 15, 10, 30, 45)), 30)),
    *many('DATEPART("second", [ts])', (v(ts=T(2024, 3, 15, 10, 30, 45)), 45)),
    # DATEDIFF counts the boundaries crossed, not elapsed time.
    *many('DATEDIFF("day", [d], [e])', (v(d=D(2024, 1, 1), e=D(2024, 3, 1)), 60),
          (v(d=D(2024, 3, 1), e=D(2024, 1, 1)), -60), (v(d=D(2024, 1, 1), e=D(2024, 1, 1)), 0),
          (v(d=None, e=D(2024, 1, 1)), None), (v(d=D(2024, 1, 1), e=None), None)),
    *many('DATEDIFF("month", [d], [e])', (v(d=D(2024, 1, 31), e=D(2024, 2, 1)), 1),
          (v(d=D(2024, 2, 1), e=D(2024, 1, 31)), -1), (v(d=D(2024, 1, 1), e=D(2024, 1, 31)), 0),
          (v(d=D(2023, 11, 15), e=D(2024, 2, 15)), 3)),
    *many('DATEDIFF("quarter", [d], [e])', (v(d=D(2024, 3, 31), e=D(2024, 4, 1)), 1),
          (v(d=D(2024, 1, 1), e=D(2024, 3, 31)), 0), (v(d=D(2023, 12, 31), e=D(2024, 1, 1)), 1)),
    *many('DATEDIFF("year", [d], [e])', (v(d=D(2023, 12, 31), e=D(2024, 1, 1)), 1),
          (v(d=D(2024, 1, 1), e=D(2024, 12, 31)), 0), (v(d=D(2024, 1, 1), e=D(2020, 6, 1)), -4)),
    # Only the date part of a datetime counts for a day difference.
    *many("DATEDIFF('day', [d], [ts])", (v(d=D(2024, 1, 1), ts=T(2024, 1, 2, 23, 59, 59)), 1),
          (v(d=D(2024, 1, 2), ts=T(2024, 1, 2, 0, 0, 1)), 0)),
    *many('DATETRUNC("month", [d])', (v(d=D(2024, 3, 15)), D(2024, 3, 1)), (v(d=None), None)),
    *many('DATETRUNC("quarter", [d])', (v(d=D(2024, 3, 15)), D(2024, 1, 1)),
          (v(d=D(2024, 5, 20)), D(2024, 4, 1)), (v(d=D(2024, 12, 31)), D(2024, 10, 1))),
    *many('DATETRUNC("year", [d])', (v(d=D(2024, 3, 15)), D(2024, 1, 1))),
    *many('DATETRUNC("day", [d])', (v(d=D(2024, 3, 15)), D(2024, 3, 15))),
    *many('DATETRUNC("month", [ts])', (v(ts=T(2024, 3, 15, 10, 30, 45)), T(2024, 3, 1))),
    *many('DATETRUNC("quarter", [ts])', (v(ts=T(2024, 5, 15, 10, 30, 45)), T(2024, 4, 1))),
    *many('DATETRUNC("year", [ts])', (v(ts=T(2024, 3, 15, 10, 30, 45)), T(2024, 1, 1))),
    *many('DATETRUNC("day", [ts])', (v(ts=T(2024, 3, 15, 10, 30, 45)), T(2024, 3, 15))),
    *many('DATETRUNC("hour", [ts])', (v(ts=T(2024, 3, 15, 10, 30, 45)), T(2024, 3, 15, 10))),
    *many('DATETRUNC("minute", [ts])', (v(ts=T(2024, 3, 15, 10, 30, 45)), T(2024, 3, 15, 10, 30))),
    *many('DATE([ts])', (v(ts=T(2024, 3, 15, 10, 30, 45)), D(2024, 3, 15)), (v(ts=None), None)),
    *many('DATE([d])', (v(d=D(2024, 3, 15)), D(2024, 3, 15))),
    *many('DATETIME([d])', (v(d=D(2024, 3, 15)), T(2024, 3, 15))),
    *many('[d] = #2024-03-05#', (v(d=D(2024, 3, 5)), True), (v(d=D(2024, 3, 6)), False)),
    *many('[ts] >= #2024-03-05 10:20:30#', (v(ts=T(2024, 3, 5, 10, 20, 30)), True),
          (v(ts=T(2024, 3, 5, 10, 20, 29)), False)),
    *many('[d] < [e]', (v(d=D(2024, 1, 1), e=D(2024, 1, 2)), True), (v(d=D(2024, 1, 2), e=D(2024, 1, 2)), False)),
    # TODAY() and NOW() are evaluated when the notebook runs; the model fixes both.
    *many('TODAY()', (v(), stub.TODAY)),
    *many('NOW()', (v(), stub.NOW)),
    *many('YEAR(TODAY())', (v(), 2024)),
    *many("DATEDIFF('day', TODAY(), [d])", (v(d=D(2024, 3, 20)), 5)),
    *many('DATETRUNC("month", TODAY())', (v(), D(2024, 3, 1))),
]

COMPARISONS = [
    *many('[a] > 1', (v(a=2), True), (v(a=1), False)),
    *many('[a] = [b]', (v(a=1, b=1), True), (v(a=1, b=2), False)),
    *many('[a] <> [b]', (v(a=1, b=2), True), (v(a=1, b=1), False)),
    *many('[x] >= 1.5', (v(x=1.5), True), (v(x=1.4), False)),
    *many('[a] >= 1 AND [a] <= 5', (v(a=1), True), (v(a=5), True), (v(a=6), False), (v(a=0), False)),
    *many('[a] IN (1, 2)', (v(a=1), True), (v(a=3), False)),
    *many('[a] IN (-1, 2)', (v(a=-1), True), (v(a=1), False)),
    *many('NOT [a] IN (1)', (v(a=1), False), (v(a=2), True)),
    *many('[s] IN ("a", "b")', (v(s='b'), True), (v(s='c'), False)),
    *many('NOT [flag]', (v(flag=True), False), (v(flag=False), True)),
    *many('NOT [a] > 1', (v(a=2), False), (v(a=0), True)),
    *many('[flag] = TRUE', (v(flag=True), True), (v(flag=False), False)),
    *many('[flag] AND [a] > 1', (v(flag=True, a=2), True), (v(flag=True, a=0), False),
          (v(flag=False, a=2), False)),
    *many('[a] > 1 AND [flag] OR [g]', (v(a=0, flag=True, g=True), True), (v(a=0, flag=True, g=False), False),
          (v(a=2, flag=True, g=False), True)),
    *many('NOT [flag] AND [g]', (v(flag=False, g=True), True), (v(flag=True, g=True), False)),
]

# ── Behaviour the documentation leaves open: what the documented Spark rule gives ──

MODEL = [
    # Spark rounds HALF_UP (ties away from zero); Tableau does not document its tie-breaking.
    *many('ROUND([x])', (v(x=2.5), 3.0), (v(x=-2.5), -3.0), (v(x=2.4), 2.0), (v(x=2.6), 3.0)),
    *many('ROUND([x], 1)', (v(x=2.25), 2.3), (v(x=2.34), 2.3), (v(x=-2.25), -2.3)),
    # Strings compare byte by byte, so case matters and 'B' sorts before 'a'.
    *many('[s] = "x"', (v(s='x'), True), (v(s='X'), False), (v(s=None), None)),
    *many('[s] < "a"', (v(s='B'), True), (v(s='a'), False), (v(s='A'), True)),
    *many('[s] <> [t]', (v(s='a', t='A'), True), (v(s='a', t='a'), False)),
    # A NULL operand gives NULL.
    *many('[a] > 1', (v(a=None), None)),
    *many('[a] = [b]', (v(a=None, b=1), None), (v(a=1, b=None), None), (v(a=None, b=None), None)),
    *many('[a] <> [b]', (v(a=None, b=None), None)),
    *many('NOT [a] > 1', (v(a=None), None)),
]

SEMANTICS = [*ARITHMETIC, *MATH, *STRINGS, *NULLS, *CONDITIONALS, *DATES, *COMPARISONS, *MODEL]


def _case_id(case):
    formula, values, _ = case
    return f'{formula} | ' + ' '.join(f'{k}={x!r}' for k, x in values.items())


def test_the_tables_only_use_fields_the_test_resolver_knows():
    for formula, values, _ in SEMANTICS:
        assert all(name in TYPES for name in fields_in(formula)), formula
        assert set(values) >= set(fields_in(formula)) or not fields_in(formula), (
            f'{formula} does not give every field it reads a value')


@pytest.mark.parametrize('formula, values, expected', SEMANTICS, ids=[_case_id(c) for c in SEMANTICS])
def test_semantics(formula, values, expected):
    result = convert(formula)
    assert result.ok, result.reason
    actual = stub.evaluate(result.expression, row(values))
    assert same(actual, expected), f'{actual!r} != {expected!r}'
    if actual is not None:
        assert fits(result.datatype, actual), f'{result.datatype} declared, {actual!r} computed'


# Three-valued logic, as Spark defines it for a NULL operand.
AND3 = {(True, True): True, (True, False): False, (False, True): False, (False, False): False,
        (True, None): None, (None, True): None, (False, None): False, (None, False): False,
        (None, None): None}
OR3 = {(True, True): True, (True, False): True, (False, True): True, (False, False): False,
       (True, None): True, (None, True): True, (False, None): None, (None, False): None,
       (None, None): None}
NOT3 = {True: False, False: True, None: None}


@pytest.mark.parametrize('left, right', sorted(AND3, key=repr), ids=repr)
def test_and_follows_spark_three_valued_logic(left, right):
    assert run('[flag] AND [g]', flag=left, g=right) is AND3[(left, right)]


@pytest.mark.parametrize('left, right', sorted(OR3, key=repr), ids=repr)
def test_or_follows_spark_three_valued_logic(left, right):
    assert run('[flag] OR [g]', flag=left, g=right) is OR3[(left, right)]


@pytest.mark.parametrize('operand', [True, False, None], ids=repr)
def test_not_follows_spark_three_valued_logic(operand):
    assert run('NOT [flag]', flag=operand) is NOT3[operand]


def test_the_and_or_null_divergence_is_pinned_together_with_its_note():
    # Spark gives NULL AND FALSE = FALSE.  The Tableau documentation says AND returns NULL when
    # either operand is NULL; that was not verified against the source, so the conversion
    # carries a note instead of claiming the two agree.
    assert run('[flag] AND [g]', flag=None, g=False) is False
    notes = convert('[flag] AND [g]').notes
    assert any('three-valued' in note and 'Tableau documentation' in note for note in notes)


def test_the_model_rows_that_need_a_caveat_carry_one():
    for formula in ('ROUND([x], 1)', '[s] = "x"', '[s] < "a"', '[s] <> [t]'):
        assert convert(formula).notes, formula
    assert any('HALF_UP' in note for note in convert('ROUND([x])').notes)
    assert any('case-sensitive' in note for note in convert('[s] = "x"').notes)


# ── Every pinned expression is runnable API use ────────────────────

TYPICAL = row(v(a=3, b=4, c=5, n=2, x=0.5, y=2.5, s='abc', t='b', flag=True, g=False,
                d=D(2024, 1, 31), e=D(2024, 3, 1), ts=stub.NOW))


@pytest.mark.parametrize('formula, expression', [(f, e) for f, e, *_ in EXACT],
                         ids=[f for f, *_ in EXACT])
def test_every_pinned_expression_runs_on_the_model(formula, expression):
    # Building raises where PySpark's Column API would: bool(Column), int(Column), a bare string
    # taken for a column name, a second otherwise(), a mixed substr() ...
    column = stub.build(expression)
    assert {name.casefold() for name in column.reads} == {name.casefold() for name in fields_in(formula)}
    column.value(stub.Row(dict.fromkeys(TYPES)))
    if 'u' not in fields_in(formula):
        column.value(stub.Row(TYPICAL))
        return
    # ``u`` has no known type: at least one reading of it must execute.
    failures = []
    for guess in (7, 'q', True):
        try:
            column.value(stub.Row({**TYPICAL, 'u': guess}))
            return
        except TypeError as error:
            failures.append(str(error))
    pytest.fail(f'no reading of the unknown-type field executes: {failures}')


# ── Random expressions against an independent reference ────────────
#
# Each formula is built as a TREE with a reference value computed over the tree, then
# rendered with the minimum of parentheses (plus some redundant ones).  The renderer
# follows Tableau's order of operations and never looks at the converter, so a wrong
# precedence or associativity in the converter shows up as a different value.

ATOM, NEG, POW, MUL, ADD, CMP, NOT, AND, OR = 100, 90, 80, 70, 60, 50, 40, 30, 20
REDUNDANT_PARENS = 0.2

COLUMN_KIND = {**dict.fromkeys(('a', 'b', 'c', 'n'), 'int'), **dict.fromkeys(('x', 'y'), 'real'),
               **dict.fromkeys(('s', 't'), 'str'), **dict.fromkeys(('flag', 'g'), 'bool'),
               **dict.fromkeys(('d', 'e'), 'date')}
POOLS = {
    'int': [None, -3, -1, 0, 1, 2, 7],
    'real': [None, -2.5, -0.5, 0.0, 1.5, 2.0, 10.25],
    'str': [None, '', 'a', 'ab', ' ab ', 'a.b', 'abc$', 'XyZ', 'x y'],
    'bool': [None, True, False],
    'date': [None, D(2023, 12, 31), D(2024, 1, 1), D(2024, 1, 31), D(2024, 2, 29), D(2024, 3, 31),
             D(2024, 4, 1)],
}
INT_COLUMNS = tuple(name for name, kind in COLUMN_KIND.items() if kind == 'int')
EXPECTED_TYPE = {'int': 'integer', 'real': 'real', 'bool': 'boolean', 'str': 'string'}
COMPARE = {'<': operator.lt, '<=': operator.le, '>': operator.gt, '>=': operator.ge,
           '=': operator.eq, '==': operator.eq, '<>': operator.ne, '!=': operator.ne}


class Node:
    __slots__ = ('text', 'prec', 'kind', 'ref')

    def __init__(self, text, prec, kind, ref):
        self.text = text
        self.prec = prec
        self.kind = kind
        self.ref = ref


def merged(nodes):
    kinds = {node.kind for node in nodes}
    if kinds == {'int', 'real'}:
        return 'real'
    assert len(kinds) == 1, kinds
    return kinds.pop()


def datediff(part, start, end):
    """Boundaries crossed between two dates, worked out from the calendar."""
    if part == 'day':
        return (end - start).days
    if part == 'month':
        return (end.year - start.year) * 12 + (end.month - start.month)
    if part == 'quarter':
        return (end.year - start.year) * 4 + ((end.month - 1) // 3 - (start.month - 1) // 3)
    return end.year - start.year


def and3(left, right):
    if left is False or right is False:
        return False
    return None if left is None or right is None else True


def or3(left, right):
    if left is True or right is True:
        return True
    return None if left is None or right is None else False


class Gen:
    """Typed random expressions: every method returns a :class:`Node` (text, precedence, kind, value)."""

    def __init__(self, rng):
        self.rng = rng

    def paren(self, node, need, strict=False):
        if node.prec < need or (strict and node.prec == need) or self.rng.random() < REDUNDANT_PARENS:
            return f'({node.text})'
        return node.text

    def call(self, name, kind, args, ref):
        return Node(f'{name}({", ".join(a.text for a in args)})', ATOM, kind, ref)

    def either(self, *makers, depth):
        return self.rng.choice(makers)(depth)

    # -- atoms
    def column(self, name):
        return Node(f'[{name}]', ATOM, COLUMN_KIND[name], lambda r: r[name])

    def literal(self, kind, value):
        text = {'str': f'"{value}"', 'bool': 'TRUE' if value is True else 'FALSE'}.get(kind, repr(value))
        return Node(text, ATOM, kind, lambda r: value)

    def int_atom(self):
        if self.rng.random() < 0.75:
            return self.column(self.rng.choice(INT_COLUMNS))
        return self.literal('int', self.rng.randint(0, 9))

    def real_atom(self):
        if self.rng.random() < 0.7:
            return self.column(self.rng.choice(('x', 'y')))
        return self.literal('real', self.rng.choice((0.5, 1.5, 2.0, 10.25)))

    def text_atom(self):
        if self.rng.random() < 0.7:
            return self.column(self.rng.choice(('s', 't')))
        return self.literal('str', self.rng.choice(('', 'a', 'x y', '$1', '.', 'a|b')))

    def bool_atom(self):
        if self.rng.random() < 0.75:
            return self.column(self.rng.choice(('flag', 'g')))
        return self.literal('bool', self.rng.random() < 0.5)

    def date_atom(self):
        return self.column(self.rng.choice(('d', 'e')))

    # -- numbers
    def numeric(self, depth):
        return self.rng.choice((self.integer, self.real))(depth)

    def integer(self, depth):
        if depth <= 0 or self.rng.random() < 0.2:
            return self.int_atom()
        return self.either(self.int_arith, self.int_arith, self.int_arith, self.int_neg, self.int_abs,
                           self.int_zn, self.int_ifnull, self.int_len, self.int_find, self.int_date,
                           self.int_datediff, self.int_round, self.int_if, self.int_iif, depth=depth - 1)

    def real(self, depth):
        if depth <= 0 or self.rng.random() < 0.2:
            return self.real_atom()
        return self.either(self.real_arith, self.real_arith, self.real_divide, self.real_divide, self.real_neg,
                           self.real_abs, self.real_zn, self.power, self.power, self.real_float, self.real_if,
                           depth=depth - 1)

    def arith(self, op, left, right):
        prec = MUL if op in '*/' else ADD
        text = f'{self.paren(left, prec)} {op} {self.paren(right, prec, strict=True)}'
        kind = 'int' if op != '/' and left.kind == right.kind == 'int' else 'real'

        def ref(r):
            a, b = left.ref(r), right.ref(r)
            if a is None or b is None:
                return None
            return {'+': operator.add, '-': operator.sub, '*': operator.mul}[op](a, b) if op != '/' \
                else (None if b == 0 else a / b)
        return Node(text, prec, kind, ref)

    def int_arith(self, depth):
        return self.arith(self.rng.choice('+-*'), self.integer(depth), self.integer(depth))

    def real_arith(self, depth):
        return self.arith(self.rng.choice('+-*'), self.numeric(depth), self.numeric(depth))

    def real_divide(self, depth):
        return self.arith('/', self.numeric(depth), self.numeric(depth))

    def neg(self, operand):
        return Node('-' + self.paren(operand, NEG, strict=True), NEG, operand.kind,
                    lambda r: None if (x := operand.ref(r)) is None else -x)

    def int_neg(self, depth):
        return self.neg(self.integer(depth))

    def real_neg(self, depth):
        return self.neg(self.real(depth))

    def power(self, depth):
        left = self.numeric(depth)
        return Node(f'{self.paren(left, POW, strict=True)} ^ 2', POW, 'real',
                    lambda r: None if (x := left.ref(r)) is None else float(x) ** 2)

    def int_abs(self, depth):
        arg = self.integer(depth)
        return self.call('ABS', 'int', [arg], lambda r: None if (x := arg.ref(r)) is None else abs(x))

    def real_abs(self, depth):
        arg = self.real(depth)
        return self.call('ABS', arg.kind, [arg], lambda r: None if (x := arg.ref(r)) is None else abs(x))

    def int_zn(self, depth):
        arg = self.integer(depth)
        return self.call('ZN', 'int', [arg], lambda r: 0 if (x := arg.ref(r)) is None else x)

    def real_zn(self, depth):
        arg = self.real(depth)
        return self.call('ZN', arg.kind, [arg], lambda r: 0 if (x := arg.ref(r)) is None else x)

    def int_ifnull(self, depth):
        first, second = self.integer(depth), self.integer(depth)
        return self.call('IFNULL', 'int', [first, second],
                         lambda r: second.ref(r) if (x := first.ref(r)) is None else x)

    def int_len(self, depth):
        arg = self.text(depth)
        return self.call('LEN', 'int', [arg], lambda r: None if (x := arg.ref(r)) is None else len(x))

    def int_find(self, depth):
        arg = self.text(depth)
        needle = self.literal('str', self.rng.choice(('a', 'b', '.', 'ab', 'x y')))
        if self.rng.random() < 0.5:
            return self.call('FIND', 'int', [arg, needle],
                             lambda r: None if (x := arg.ref(r)) is None else x.find(needle.ref(r)) + 1)
        start = self.literal('int', self.rng.randint(1, 3))
        return self.call('FIND', 'int', [arg, needle, start],
                         lambda r: None if (x := arg.ref(r)) is None
                         else x.find(needle.ref(r), start.ref(r) - 1) + 1)

    def int_date(self, depth):
        name = self.rng.choice(('YEAR', 'MONTH', 'DAY', 'quarter', 'dayofyear'))
        arg = self.date_atom()
        part = {'YEAR': lambda d: d.year, 'MONTH': lambda d: d.month, 'DAY': lambda d: d.day,
                'quarter': lambda d: (d.month - 1) // 3 + 1,
                'dayofyear': lambda d: d.timetuple().tm_yday}[name]
        def ref(r):
            return None if (x := arg.ref(r)) is None else part(x)

        if name in ('quarter', 'dayofyear'):
            return Node(f'DATEPART("{name}", {arg.text})', ATOM, 'int', ref)
        return self.call(name, 'int', [arg], ref)

    def int_datediff(self, depth):
        part = self.rng.choice(('day', 'month', 'quarter', 'year'))
        start, end = self.date_atom(), self.date_atom()
        return Node(f'DATEDIFF("{part}", {start.text}, {end.text})', ATOM, 'int',
                    lambda r: None if start.ref(r) is None or end.ref(r) is None
                    else datediff(part, start.ref(r), end.ref(r)))

    def int_round(self, depth):
        name = self.rng.choice(('FLOOR', 'CEILING', 'INT'))
        arg = self.numeric(depth)
        fn = {'FLOOR': math.floor, 'CEILING': math.ceil, 'INT': int}[name]
        return self.call(name, 'int', [arg], lambda r: None if (x := arg.ref(r)) is None else fn(x))

    def real_float(self, depth):
        arg = self.integer(depth)
        return self.call('FLOAT', 'real', [arg], lambda r: None if (x := arg.ref(r)) is None else float(x))

    def if_node(self, depth, branch):
        pairs = [(self.boolean(depth), branch(depth)) for _ in range(self.rng.choice((1, 1, 2)))]
        other = branch(depth) if self.rng.random() < 0.7 else None
        parts = [f'IF {pairs[0][0].text} THEN {pairs[0][1].text}']
        parts += [f'ELSEIF {c.text} THEN {x.text}' for c, x in pairs[1:]]
        if other is not None:
            parts.append(f'ELSE {other.text}')

        def ref(r):
            for cond, value in pairs:
                if cond.ref(r) is True:
                    return value.ref(r)
            return None if other is None else other.ref(r)
        return Node(' '.join(parts) + ' END', ATOM,
                    merged([x for _, x in pairs] + ([other] if other else [])), ref)

    def iif_node(self, depth, branch):
        cond, then, other = self.boolean(depth), branch(depth), branch(depth)
        unknown = branch(depth) if self.rng.random() < 0.3 else None
        args = [cond, then, other] + ([unknown] if unknown else [])

        def ref(r):
            test = cond.ref(r)
            if test is None:
                return None if unknown is None else unknown.ref(r)
            return then.ref(r) if test else other.ref(r)
        return self.call('IIF', merged(args[1:]), args, ref)

    def int_if(self, depth):
        return self.if_node(depth, self.integer)

    def real_if(self, depth):
        return self.if_node(depth, self.numeric)

    def int_iif(self, depth):
        return self.iif_node(depth, self.integer)

    # -- strings
    def text(self, depth):
        if depth <= 0 or self.rng.random() < 0.2:
            return self.text_atom()
        return self.either(self.concat, self.case, self.trim, self.left, self.right, self.mid,
                           self.replace, self.from_int, self.text_ifnull, self.text_if, self.text_iif,
                           depth=depth - 1)

    def concat(self, depth):
        left, right = self.text(depth), self.text(depth)
        return Node(f'{self.paren(left, ADD)} + {self.paren(right, ADD, strict=True)}', ADD, 'str',
                    lambda r: None if left.ref(r) is None or right.ref(r) is None
                    else left.ref(r) + right.ref(r))

    def map_text(self, name, fn, arg):
        return self.call(name, 'str', [arg], lambda r: None if (x := arg.ref(r)) is None else fn(x))

    def case(self, depth):
        name, fn = self.rng.choice((('UPPER', str.upper), ('LOWER', str.lower)))
        return self.map_text(name, fn, self.text(depth))

    def trim(self, depth):
        name, fn = self.rng.choice((('TRIM', lambda s: s.strip(' ')), ('LTRIM', lambda s: s.lstrip(' ')),
                                    ('RTRIM', lambda s: s.rstrip(' '))))
        return self.map_text(name, fn, self.text(depth))

    def left(self, depth):
        arg, count = self.text(depth), self.literal('int', self.rng.randint(0, 3))
        return self.call('LEFT', 'str', [arg, count],
                         lambda r: None if (x := arg.ref(r)) is None else x[:count.ref(r)])

    def right(self, depth):
        arg, count = self.text(depth), self.literal('int', self.rng.randint(1, 3))
        return self.call('RIGHT', 'str', [arg, count],
                         lambda r: None if (x := arg.ref(r)) is None else x[-count.ref(r):])

    def mid(self, depth):
        arg, pos = self.text(depth), self.literal('int', self.rng.randint(1, 3))
        if self.rng.random() < 0.5:
            return self.call('MID', 'str', [arg, pos],
                             lambda r: None if (x := arg.ref(r)) is None else x[pos.ref(r) - 1:])
        length = self.literal('int', self.rng.randint(0, 3))
        return self.call('MID', 'str', [arg, pos, length],
                         lambda r: None if (x := arg.ref(r)) is None
                         else x[pos.ref(r) - 1:pos.ref(r) - 1 + length.ref(r)])

    def replace(self, depth):
        arg = self.text(depth)
        search = self.literal('str', self.rng.choice(('a', '.', '$', 'ab', 'a.b', '|')))
        repl = self.literal('str', self.rng.choice(('x', '$1', '-', '')))
        return self.call('REPLACE', 'str', [arg, search, repl],
                         lambda r: None if (x := arg.ref(r)) is None else x.replace(search.ref(r), repl.ref(r)))

    def from_int(self, depth):
        arg = self.integer(depth)
        return self.call('STR', 'str', [arg], lambda r: None if (x := arg.ref(r)) is None else str(x))

    def text_ifnull(self, depth):
        first, second = self.text(depth), self.text(depth)
        return self.call('IFNULL', 'str', [first, second],
                         lambda r: second.ref(r) if (x := first.ref(r)) is None else x)

    def text_if(self, depth):
        return self.if_node(depth, self.text)

    def text_iif(self, depth):
        return self.iif_node(depth, self.text)

    # -- booleans
    def boolean(self, depth):
        if depth <= 0 or self.rng.random() < 0.2:
            return self.bool_atom()
        return self.either(self.compare, self.compare, self.in_list, self.is_null, self.contains,
                           self.negation, self.conjunction, self.disjunction, depth=depth - 1)

    def compare(self, depth):
        family = self.rng.choice(('num', 'num', 'str', 'date'))
        left, right = {'num': lambda: (self.numeric(depth), self.numeric(depth)),
                       'str': lambda: (self.text(depth), self.text(depth)),
                       'date': lambda: (self.date_atom(), self.date_atom())}[family]()
        op = self.rng.choice(sorted(COMPARE))
        fn = COMPARE[op]
        return Node(f'{self.paren(left, CMP, strict=True)} {op} {self.paren(right, CMP, strict=True)}', CMP,
                    'bool', lambda r: None if left.ref(r) is None or right.ref(r) is None
                    else fn(left.ref(r), right.ref(r)))

    def in_list(self, depth):
        if self.rng.random() < 0.5:
            operand = self.integer(depth)
            values = self.rng.sample([-1, 0, 1, 2, 7], self.rng.randint(1, 3))
            listing = ', '.join(str(x) for x in values)
        else:
            operand = self.text(depth)
            values = self.rng.sample(['a', 'ab', 'x y', '', 'XyZ'], self.rng.randint(1, 3))
            listing = ', '.join(f'"{x}"' for x in values)
        return Node(f'{self.paren(operand, CMP, strict=True)} IN ({listing})', CMP, 'bool',
                    lambda r: None if (x := operand.ref(r)) is None else x in values)

    def is_null(self, depth):
        arg = self.either(self.integer, self.real, self.text, lambda _: self.date_atom(), depth=depth)
        return self.call('ISNULL', 'bool', [arg], lambda r: arg.ref(r) is None)

    def contains(self, depth):
        arg = self.text(depth)
        needle = self.literal('str', self.rng.choice(('', 'a', 'b', 'ab', '.')))
        return self.call('CONTAINS', 'bool', [arg, needle],
                         lambda r: None if (x := arg.ref(r)) is None else needle.ref(r) in x)

    def negation(self, depth):
        operand = self.boolean(depth)
        return Node('NOT ' + self.paren(operand, NOT, strict=True), NOT, 'bool',
                    lambda r: None if (x := operand.ref(r)) is None else not x)

    def logic(self, word, prec, combine, depth):
        left, right = self.boolean(depth), self.boolean(depth)
        return Node(f'{self.paren(left, prec)} {word} {self.paren(right, prec, strict=True)}', prec, 'bool',
                    lambda r: combine(left.ref(r), right.ref(r)))

    def conjunction(self, depth):
        return self.logic('AND', AND, and3, depth)

    def disjunction(self, depth):
        return self.logic('OR', OR, or3, depth)


def generated(seed, count=60):
    gen = Gen(random.Random(seed))
    for _ in range(count):
        node = gen.rng.choice((gen.numeric, gen.boolean, gen.text))(gen.rng.randint(1, 5))
        yield node, gen.paren(node, 0)


def sample_rows(seed, count=6):
    rng = random.Random(f'rows-{seed}')
    rows = [dict.fromkeys(TYPES), row(v(a=2, b=-3, c=7, n=1, x=1.5, y=-2.5, s='ab', t=' ab ', flag=True,
                                        g=False, d=D(2024, 1, 31), e=D(2024, 3, 31)))]
    for _ in range(count):
        rows.append(row({name: rng.choice(POOLS[kind]) for name, kind in COLUMN_KIND.items()}))
    return rows


def matches(actual, expected, kind):
    if expected is None:
        return actual is None
    if kind == 'bool':
        return actual is expected
    if kind == 'str':
        return type(actual) is str and actual == expected
    if kind == 'int':
        return type(actual) is int and actual == expected
    return isinstance(actual, (int, float)) and not isinstance(actual, bool) and actual == pytest.approx(expected)


@pytest.mark.parametrize('seed', range(8))
def test_random_expressions_compute_the_reference_value(seed):
    rows = sample_rows(seed)
    for node, text in generated(seed):
        result = convert(text)
        assert result.ok, f'{text}: {result.reason}'
        assert result.datatype == EXPECTED_TYPE[node.kind], text
        assert result.columns == fields_in(text), text
        assert gate_reason(result.expression) is None, text
        column = stub.build(result.expression)
        assert {n.casefold() for n in column.reads} == {n.casefold() for n in result.columns}, text
        for values in rows:
            actual, expected = column.value(stub.Row(values)), node.ref(values)
            assert matches(actual, expected, node.kind), (
                f'{text} on {values}: computed {actual!r}, reference {expected!r}')


def chain_case(rng):
    """``x0 op x1 op x2 ...`` at ONE precedence level, no parentheses: Tableau folds from the left."""
    level = rng.choice(('+-', '*/'))
    ops = [rng.choice(level) for _ in range(rng.randint(2, 5))]
    operands = [rng.choice([f'[{n}]' for n in INT_COLUMNS] + [str(rng.randint(1, 9))])
                for _ in range(len(ops) + 1)]
    text = operands[0] + ''.join(f' {op} {operand}' for op, operand in zip(ops, operands[1:]))

    def read(token, values):
        return values[token[1:-1]] if token.startswith('[') else int(token)

    def reference(values):
        acc = read(operands[0], values)
        for op, token in zip(ops, operands[1:]):
            other = read(token, values)
            if acc is None or other is None:
                acc = None
            elif op == '/':
                acc = None if other == 0 else acc / other
            else:
                acc = {'+': operator.add, '-': operator.sub, '*': operator.mul}[op](acc, other)
        return acc
    return text, ('real' if '/' in ops else 'int'), reference


def chains(seed, count=40):
    rng = random.Random(f'chain-{seed}')
    for _ in range(count):
        yield chain_case(rng)


@pytest.mark.parametrize('seed', range(6))
def test_flat_chains_at_one_precedence_level_fold_from_the_left(seed):
    rows = sample_rows(seed)
    for text, kind, reference in chains(seed):
        result = convert(text)
        assert result.ok, f'{text}: {result.reason}'
        assert result.datatype == EXPECTED_TYPE[kind], text
        column = stub.build(result.expression)
        for values in rows:
            actual, expected = column.value(stub.Row(values)), reference(values)
            assert matches(actual, expected, kind), f'{text} on {values}: {actual!r} != {expected!r}'


def test_the_chain_generator_reaches_both_levels_and_long_chains():
    texts = [text for seed in range(6) for text, _, _ in chains(seed)]
    levels = {'+-': sum(' + ' in t or ' - ' in t for t in texts), '*/': sum(' * ' in t or ' / ' in t for t in texts)}
    assert min(levels.values()) >= 80, levels
    assert len(set(texts)) >= 0.8 * len(texts)
    assert sum(t.count(' - ') >= 2 for t in texts) >= 25 and sum(t.count(' / ') >= 2 for t in texts) >= 25
    assert max(t.count(' ') for t in texts) >= 10


ATOMIC = re.compile(r'\(*(\[\w+\]|TRUE|FALSE|"[^"]*"|[\d.]+)\)*')
CONSTRUCTS = ('IF ', 'ELSEIF', 'IIF(', ' ^ 2', ' / ', ' - ', 'NOT ', ' AND ', ' OR ', ' IN (', 'DATEDIFF(',
              'DATEPART(', 'MID(', 'LEFT(', 'RIGHT(', 'REPLACE(', 'FIND(', 'ISNULL(', 'CONTAINS(', 'IFNULL(',
              'ZN(', 'STR(', 'FLOAT(', 'FLOOR(', 'CEILING(', 'INT(', 'ABS(', 'UPPER(', 'LTRIM(', 'LEN(', '-[')
PRECEDENCE_SENSITIVE = {' - ': 20, ' / ': 20, ' ^ 2': 10, '-[': 6, 'NOT ': 15, ' AND ': 15, ' OR ': 15}
MIN_SEEN = 5


def test_the_generator_exercises_what_it_claims_to():
    # A guard on the guard: the random formulas must actually reach the constructs the property
    # is about, or a silent change to the generator would hollow the test out.
    texts = [text for seed in range(8) for _, text in generated(seed)]
    joined = '\n'.join(texts)
    for construct in CONSTRUCTS:
        assert joined.count(construct) >= PRECEDENCE_SENSITIVE.get(construct, MIN_SEEN), (
            f'{construct!r} appears {joined.count(construct)} times')
    compound = [text for text in texts if not ATOMIC.fullmatch(text)]
    assert len(compound) >= 0.6 * len(texts), 'too many formulas are a bare field or literal'
    assert len(set(compound)) >= 0.95 * len(compound), 'the generator repeats itself'
    assert max(len(text) for text in texts) > 150, 'no formula is deep enough to stress precedence'
