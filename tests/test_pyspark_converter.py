"""Pinned behaviour of the typed Tableau -> PySpark converter.

``EXACT`` pins the generated PySpark text, its inferred type and the number of
caveat notes for each formula; ``REFUSED`` pins the reason a formula is not
converted.  Both tables were reviewed row by row against the Tableau and
PySpark documentation, so a change here must be a deliberate one.

Nothing in this module runs Spark.  ``test_pyspark_converter_semantics`` checks
the values with an executable *model* of PySpark written from the same
documentation; neither proves agreement with a real Spark runtime.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from powerbi_import.pyspark_converter import Ref  # noqa: E402
from tests.converter_support import TYPES, convert, fields_in, gate_reason, resolve  # noqa: E402

EXACT = [
    # formula, expression, datatype, number of notes
    # -- arithmetic
    ('[a] + [b] * 2', 'F.col("a") + F.col("b") * 2', 'integer', 0),
    ('([a] + [b]) * 2', '(F.col("a") + F.col("b")) * 2', 'integer', 0),
    ('-[a] + 3', '-F.col("a") + 3', 'integer', 0),
    ('[a] - [b] - [c]', 'F.col("a") - F.col("b") - F.col("c")', 'integer', 0),
    ('[a] - ([b] - [c])', 'F.col("a") - (F.col("b") - F.col("c"))', 'integer', 0),
    ('[a] * ([b] + [c])', 'F.col("a") * (F.col("b") + F.col("c"))', 'integer', 0),
    ('[a] + 1', 'F.col("a") + 1', 'integer', 0),
    ('[a] + 1.5', 'F.col("a") + 1.5', 'real', 0),
    ('[a] * 1.5', 'F.col("a") * 1.5', 'real', 0),
    ('[a] + [x]', 'F.col("a") + F.col("x")', 'real', 0),
    ('[a] * -1', 'F.col("a") * -1', 'integer', 0),
    ('[a] - -1', 'F.col("a") - -1', 'integer', 0),
    ('[a] +1', 'F.col("a") + 1', 'integer', 0),
    ('[a]+-1', 'F.col("a") + -1', 'integer', 0),
    ('[a]--1', 'F.col("a") - -1', 'integer', 0),
    ('-[a]', '-F.col("a")', 'integer', 0),
    ('- -[a]', '-(-F.col("a"))', 'integer', 0),
    ('-(-[a])', '-(-F.col("a"))', 'integer', 0),
    ('1 + 2', 'F.lit(1) + 2', 'integer', 0),
    ('[a] / [b]', 'F.col("a") / F.col("b")', 'real', 1),
    ('[a] / [b] / [c]', 'F.col("a") / F.col("b") / F.col("c")', 'real', 1),
    ('[a] / 2', 'F.col("a") / 2', 'real', 1),
    ('[a] / 0', 'F.col("a") / 0', 'real', 1),
    ('[x] / 0', 'F.col("x") / 0', 'real', 1),
    ('1 / 0', 'F.lit(1) / 0', 'real', 1),
    ('5 / 2', 'F.lit(5) / 2', 'real', 1),
    ('[a] + [b] / 2', 'F.col("a") + F.col("b") / 2', 'real', 1),
    ('[u] + 1', 'F.col("u") + 1', 'unknown', 1),
    # -- constants and syntax
    ('5', 'F.lit(5)', 'integer', 0),
    ('-5', 'F.lit(-5)', 'integer', 0),
    ('-1.5', 'F.lit(-1.5)', 'real', 0),
    ('1.5 * 2', 'F.lit(1.5) * 2', 'real', 0),
    ('.5 + [a]', 'F.lit(0.5) + F.col("a")', 'real', 0),
    ('5. + [a]', 'F.lit(5.0) + F.col("a")', 'real', 0),
    ('1e3 + [a]', 'F.lit(1000.0) + F.col("a")', 'real', 0),
    ('1E+3 + [a]', 'F.lit(1000.0) + F.col("a")', 'real', 0),
    ('9223372036854775807', 'F.lit(9223372036854775807)', 'integer', 0),
    ('TRUE', 'F.lit(True)', 'boolean', 0),
    ('[flag]', 'F.col("flag")', 'boolean', 0),
    ('"abc"', "F.lit('abc')", 'string', 0),
    ("'abc'", "F.lit('abc')", 'string', 0),
    ('"it\'s"', '''F.lit("it's")''', 'string', 0),
    ("'it''s' + [s]", '''F.concat(F.lit("it's"), F.col("s"))''', 'string', 0),
    ('"say ""hi"""', '''F.lit('say "hi"')''', 'string', 0),
    ('"line\nbreak"', r"F.lit('line\nbreak')", 'string', 0),
    ("'// not' + [s]", '''F.concat(F.lit('// not'), F.col("s"))''', 'string', 0),
    ('"/* not */" + [s]', '''F.concat(F.lit('/* not */'), F.col("s"))''', 'string', 0),
    ('[a] // note\n + [b]', 'F.col("a") + F.col("b")', 'integer', 0),
    ('/* c */ [a] + /* d */ [b]', 'F.col("a") + F.col("b")', 'integer', 0),
    ('if [a] > 1 then "x" else "y" end', '''F.when(F.col("a") > 1, 'x').otherwise('y')''', 'string', 0),
    ('If [a] > 1 Then "x" Else "y" End', '''F.when(F.col("a") > 1, 'x').otherwise('y')''', 'string', 0),
    ('zn([a])', 'F.coalesce(F.col("a"), F.lit(0))', 'integer', 0),
    ('Len([s])', 'F.length(F.col("s"))', 'integer', 0),
    # -- math
    ('2 ^ 3', 'F.pow(F.lit(2), F.lit(3))', 'real', 0),
    ('[a] ^ 2', 'F.pow(F.col("a"), F.lit(2))', 'real', 0),
    ('[a] ^ 0.5', 'F.pow(F.col("a"), F.lit(0.5))', 'real', 0),
    ('[a] ^ -1', 'F.pow(F.col("a"), F.lit(-1))', 'real', 0),
    ('-[a] ^ 2', 'F.pow(-F.col("a"), F.lit(2))', 'real', 0),
    ('2 * [a] ^ 2', 'F.lit(2) * F.pow(F.col("a"), F.lit(2))', 'real', 0),
    ('ABS([a])', 'F.abs(F.col("a"))', 'integer', 0),
    ('ABS([x])', 'F.abs(F.col("x"))', 'real', 0),
    ('ABS([a]) + ABS([x])', 'F.abs(F.col("a")) + F.abs(F.col("x"))', 'real', 0),
    ('SQRT([x])', 'F.sqrt(F.col("x"))', 'real', 1),
    ('SQRT([a])', 'F.sqrt(F.col("a"))', 'real', 1),
    ('SQRT(4)', 'F.sqrt(F.lit(4))', 'real', 1),
    ('EXP([x])', 'F.exp(F.col("x"))', 'real', 0),
    ('SIN([x])', 'F.sin(F.col("x"))', 'real', 0),
    ('COS([x])', 'F.cos(F.col("x"))', 'real', 0),
    ('TAN([x])', 'F.tan(F.col("x"))', 'real', 0),
    ('ASIN([x])', 'F.asin(F.col("x"))', 'real', 1),
    ('ACOS([x])', 'F.acos(F.col("x"))', 'real', 1),
    ('ATAN([x])', 'F.atan(F.col("x"))', 'real', 0),
    ('DEGREES([x])', 'F.degrees(F.col("x"))', 'real', 0),
    ('RADIANS([x])', 'F.radians(F.col("x"))', 'real', 0),
    ('LN([x])', 'F.log(F.col("x"))', 'real', 0),
    ('LOG([x])', 'F.log10(F.col("x"))', 'real', 0),
    ('LOG([x], 2)', 'F.log(2.0, F.col("x"))', 'real', 0),
    ('LOG(100, 10)', 'F.log(10.0, F.lit(100))', 'real', 0),
    ('POWER([x], 2)', 'F.pow(F.col("x"), F.lit(2))', 'real', 0),
    ('POWER([x], 0.5)', 'F.pow(F.col("x"), F.lit(0.5))', 'real', 0),
    ('POWER([x], [a])', 'F.pow(F.col("x"), F.col("a"))', 'real', 0),
    ('POWER([x], [y])', 'F.pow(F.col("x"), F.col("y"))', 'real', 0),
    ('POWER(2, 3)', 'F.pow(F.lit(2), F.lit(3))', 'real', 0),
    ('POWER([a], -1)', 'F.pow(F.col("a"), F.lit(-1))', 'real', 0),
    ('SQUARE([x])', 'F.pow(F.col("x"), F.lit(2))', 'real', 0),
    ('PI()', 'F.lit(3.141592653589793)', 'real', 0),
    ('FLOOR([x])', 'F.floor(F.col("x"))', 'integer', 0),
    ('FLOOR([a])', 'F.floor(F.col("a"))', 'integer', 0),
    ('CEILING([x])', 'F.ceil(F.col("x"))', 'integer', 0),
    ('ROUND([x])', 'F.round(F.col("x"))', 'real', 1),
    ('ROUND([x], 2)', 'F.round(F.col("x"), 2)', 'real', 1),
    ('ROUND([x], -1)', 'F.round(F.col("x"), -1)', 'real', 1),
    ('ROUND([a])', 'F.round(F.col("a"))', 'integer', 1),
    ('ROUND([a], 2)', 'F.round(F.col("a"), 2)', 'integer', 1),
    ('INT([x])', '''F.col("x").cast('long')''', 'integer', 0),
    ('INT([a])', '''F.col("a").cast('long')''', 'integer', 0),
    ('INT(2.9)', "F.lit(2.9).cast('long')", 'integer', 0),
    ('INT(-[x])', '''(-F.col("x")).cast('long')''', 'integer', 0),
    ('FLOAT([a])', '''F.col("a").cast('double')''', 'real', 0),
    ('FLOAT([x])', '''F.col("x").cast('double')''', 'real', 0),
    ('FLOAT(1)', "F.lit(1).cast('double')", 'real', 0),
    # -- strings
    ('[s] + [t]', 'F.concat(F.col("s"), F.col("t"))', 'string', 0),
    ('[s] + "x"', '''F.concat(F.col("s"), F.lit('x'))''', 'string', 0),
    ('"a" + "b"', "F.concat(F.lit('a'), F.lit('b'))", 'string', 0),
    ('STR([a]) + [s]', '''F.concat(F.col("a").cast('string'), F.col("s"))''', 'string', 0),
    ('[u] + [s]', 'F.concat(F.col("u"), F.col("s"))', 'string', 1),
    ('LEFT([s], 3)', 'F.col("s").substr(1, 3)', 'string', 0),
    ('LEFT([s], 0)', 'F.col("s").substr(1, 0)', 'string', 0),
    ('LEFT("abc", 2)', "F.lit('abc').substr(1, 2)", 'string', 0),
    ('RIGHT([s], 3)', 'F.col("s").substr(-3, 3)', 'string', 0),
    ('MID([s], 2, 3)', 'F.col("s").substr(2, 3)', 'string', 0),
    ('MID([s], 2)', 'F.col("s").substr(F.lit(2), F.length(F.col("s")))', 'string', 0),
    ('LEFT([s], [n])', 'F.col("s").substr(F.lit(1), F.col("n"))', 'string', 1),
    ('RIGHT([s], [n])', 'F.col("s").substr(-F.col("n"), F.col("n"))', 'string', 1),
    ('MID([s], [a], [b])', 'F.col("s").substr(F.col("a"), F.col("b"))', 'string', 1),
    ('MID([s], 2, [b])', 'F.col("s").substr(F.lit(2), F.col("b"))', 'string', 1),
    ('MID([s], [a], 3)', 'F.col("s").substr(F.col("a"), F.lit(3))', 'string', 1),
    ('MID([s], [a])', 'F.col("s").substr(F.col("a"), F.length(F.col("s")))', 'string', 1),
    ('LEFT([s], 3) + MID([s], 2, 1)', 'F.concat(F.col("s").substr(1, 3), F.col("s").substr(2, 1))', 'string', 0),
    ('UPPER(LEFT([s], 1)) + LOWER(MID([s], 2))', 'F.concat(F.upper(F.col("s").substr(1, 1)), F.lower(F.col("s").substr(F.lit(2), F.length(F.col("s")))))', 'string', 0),
    ('UPPER([s])', 'F.upper(F.col("s"))', 'string', 0),
    ('UPPER("x")', "F.upper(F.lit('x'))", 'string', 0),
    ('UPPER([u])', 'F.upper(F.col("u"))', 'string', 1),
    ('LOWER([s])', 'F.lower(F.col("s"))', 'string', 0),
    ('LOWER(TRIM([s]))', 'F.lower(F.trim(F.col("s")))', 'string', 0),
    ('TRIM([s])', 'F.trim(F.col("s"))', 'string', 0),
    ('LTRIM([s])', 'F.ltrim(F.col("s"))', 'string', 0),
    ('LEN([s])', 'F.length(F.col("s"))', 'integer', 0),
    ('LEN("abc")', "F.length(F.lit('abc'))", 'integer', 0),
    ('LEN([u])', 'F.length(F.col("u"))', 'integer', 1),
    ('STR([a])', '''F.col("a").cast('string')''', 'string', 0),
    ('STR([s])', '''F.col("s").cast('string')''', 'string', 0),
    ('STR(5)', "F.lit(5).cast('string')", 'string', 0),
    ('STR(-[a])', '''(-F.col("a")).cast('string')''', 'string', 0),
    ('STR("x")', "F.lit('x').cast('string')", 'string', 0),
    ('CONTAINS([s], "a")', '''F.col("s").contains('a')''', 'boolean', 1),
    ('CONTAINS([s], "")', '''F.col("s").contains('')''', 'boolean', 1),
    ('CONTAINS([s], [t])', 'F.col("s").contains(F.col("t"))', 'boolean', 1),
    ('STARTSWITH([s], "a")', '''F.ltrim(F.col("s")).startswith('a')''', 'boolean', 2),
    ('STARTSWITH([s], [t])', 'F.ltrim(F.col("s")).startswith(F.col("t"))', 'boolean', 2),
    ('ENDSWITH([s], "a")', '''F.rtrim(F.col("s")).endswith('a')''', 'boolean', 2),
    ('REPLACE([s], "a", "b")', '''F.regexp_replace(F.col("s"), 'a', 'b')''', 'string', 0),
    ('REPLACE([s], ".", "-")', r'''F.regexp_replace(F.col("s"), '\\.', '-')''', 'string', 0),
    ('REPLACE([s], "a.b", "$1")', r'''F.regexp_replace(F.col("s"), 'a\\.b', '\\$1')''', 'string', 0),
    ('REPLACE([s], "[", "(")', r'''F.regexp_replace(F.col("s"), '\\[', '(')''', 'string', 0),
    ('REPLACE([s], "$", "x")', r'''F.regexp_replace(F.col("s"), '\\$', 'x')''', 'string', 0),
    ('REPLACE([s], "(a)", "[b]")', r'''F.regexp_replace(F.col("s"), '\\(a\\)', '[b]')''', 'string', 0),
    ('REPLACE([s], "a|b", "x")', r'''F.regexp_replace(F.col("s"), 'a\\|b', 'x')''', 'string', 0),
    ('REPLACE([s], "^", "x")', r'''F.regexp_replace(F.col("s"), '\\^', 'x')''', 'string', 0),
    ('REPLACE([s], "a", "")', '''F.regexp_replace(F.col("s"), 'a', '')''', 'string', 0),
    ('FIND([s], "a")', '''F.locate('a', F.col("s"))''', 'integer', 1),
    ('FIND([s], "a", 3)', '''F.locate('a', F.col("s"), 3)''', 'integer', 1),
    ('SPLIT([s], "-", 2)', '''F.split(F.col("s"), '-').getItem(1)''', 'string', 1),
    ('SPLIT([s], ".", 1)', r'''F.split(F.col("s"), '\\.').getItem(0)''', 'string', 1),
    ('SPLIT([s], "|", 2)', r'''F.split(F.col("s"), '\\|').getItem(1)''', 'string', 1),
    ('IFNULL([s], "")', '''F.coalesce(F.col("s"), F.lit(''))''', 'string', 0),
    # -- conditionals
    ('IF [a] > 0 THEN "p" ELSE "n" END', '''F.when(F.col("a") > 0, 'p').otherwise('n')''', 'string', 0),
    ('IF [a] > 0 THEN 1 END', 'F.when(F.col("a") > 0, 1)', 'integer', 0),
    ('IF [a] > 1 THEN "x" ELSEIF [a] > 0 THEN "y" ELSE "z" END', '''F.when(F.col("a") > 1, 'x').when(F.col("a") > 0, 'y').otherwise('z')''', 'string', 0),
    ('IF [a] > 1 THEN "x" ELSE IF [a] > 0 THEN "y" ELSE "z" END END', '''F.when(F.col("a") > 1, 'x').otherwise(F.when(F.col("a") > 0, 'y').otherwise('z'))''', 'string', 0),
    ('IF [a] > 0 THEN IF [b] > 0 THEN 1 ELSE 2 END ELSE 3 END', 'F.when(F.col("a") > 0, F.when(F.col("b") > 0, 1).otherwise(2)).otherwise(3)', 'integer', 0),
    ('IF [flag] THEN 1 ELSE 0 END', 'F.when(F.col("flag"), 1).otherwise(0)', 'integer', 0),
    ('IF [flag] THEN [s] END', 'F.when(F.col("flag"), F.col("s"))', 'string', 0),
    ('IF [u] THEN 1 END', 'F.when(F.col("u"), 1)', 'integer', 1),
    ('IF ISNULL([a]) THEN 0 ELSE [a] END', 'F.when(F.col("a").isNull(), 0).otherwise(F.col("a"))', 'integer', 0),
    ('IF [a] > 0 THEN 1 ELSE 2.5 END', 'F.when(F.col("a") > 0, 1).otherwise(2.5)', 'real', 0),
    ('IF [a] > 0 THEN [x] ELSE [a] END', 'F.when(F.col("a") > 0, F.col("x")).otherwise(F.col("a"))', 'real', 0),
    ('IF [a] > 1 THEN NULL ELSE 2 END', 'F.when(F.col("a") > 1, F.lit(None)).otherwise(2)', 'integer', 0),
    ('IF [a] > 1 THEN 1 ELSE NULL END', 'F.when(F.col("a") > 1, 1).otherwise(F.lit(None))', 'integer', 0),
    ('IF [a] > 1 THEN [d] ELSE [e] END', 'F.when(F.col("a") > 1, F.col("d")).otherwise(F.col("e"))', 'date', 0),
    ('IF [a] > 1 THEN [d] ELSE [ts] END', 'F.when(F.col("a") > 1, F.col("d")).otherwise(F.col("ts"))', 'datetime', 0),
    ('IIF([a] > 1, "x", "y")', '''F.when(F.col("a") > 1, 'x').when(~(F.col("a") > 1), 'y')''', 'string', 0),
    ('iif([a] > 1, "x", "y")', '''F.when(F.col("a") > 1, 'x').when(~(F.col("a") > 1), 'y')''', 'string', 0),
    ('IIF([a] > 1, "x", "y", "u")', '''F.when(F.col("a") > 1, 'x').when(~(F.col("a") > 1), 'y').otherwise('u')''', 'string', 0),
    ('IIF([a] > 0, 1, 0, -1)', 'F.when(F.col("a") > 0, 1).when(~(F.col("a") > 0), 0).otherwise(-1)', 'integer', 0),
    ('IIF([flag], 1, 2)', 'F.when(F.col("flag"), 1).when(~F.col("flag"), 2)', 'integer', 0),
    ('CASE [s] WHEN "a" THEN 1 WHEN "b" THEN 2 ELSE 3 END', '''F.when(F.col("s") == 'a', 1).when(F.col("s") == 'b', 2).otherwise(3)''', 'integer', 1),
    ('CASE [s] WHEN [t] THEN 1 END', 'F.when(F.col("s") == F.col("t"), 1)', 'integer', 1),
    ('CASE [a] WHEN 1 THEN "x" WHEN 2 THEN "y" END', '''F.when(F.col("a") == 1, 'x').when(F.col("a") == 2, 'y')''', 'string', 0),
    ('CASE [a] WHEN 1 THEN 1 END', 'F.when(F.col("a") == 1, 1)', 'integer', 0),
    # -- nulls
    ('ISNULL([a])', 'F.col("a").isNull()', 'boolean', 0),
    ('ISNULL([x])', 'F.col("x").isNull()', 'boolean', 0),
    ('ISNULL([s])', 'F.col("s").isNull()', 'boolean', 0),
    ('ISNULL([d])', 'F.col("d").isNull()', 'boolean', 0),
    ('ISNULL([flag])', 'F.col("flag").isNull()', 'boolean', 0),
    ('ISNULL(1)', 'F.lit(1).isNull()', 'boolean', 0),
    ('ISNULL([u])', 'F.col("u").isNull()', 'boolean', 1),
    ('ZN([a])', 'F.coalesce(F.col("a"), F.lit(0))', 'integer', 0),
    ('ZN([x])', 'F.coalesce(F.col("x"), F.lit(0))', 'real', 0),
    ('ZN([u])', 'F.coalesce(F.col("u"), F.lit(0))', 'unknown', 1),
    ('ZN([a] / [b])', 'F.coalesce(F.col("a") / F.col("b"), F.lit(0))', 'real', 1),
    ('IFNULL([a], 5)', 'F.coalesce(F.col("a"), F.lit(5))', 'integer', 0),
    ('IFNULL([x], 0)', 'F.coalesce(F.col("x"), F.lit(0))', 'real', 0),
    ('IFNULL([s], "none")', '''F.coalesce(F.col("s"), F.lit('none'))''', 'string', 0),
    ('IFNULL([s], [t])', 'F.coalesce(F.col("s"), F.col("t"))', 'string', 0),
    ('IFNULL([d], [e])', 'F.coalesce(F.col("d"), F.col("e"))', 'date', 0),
    ('IFNULL(NULL, [a])', 'F.coalesce(F.lit(None), F.col("a"))', 'integer', 0),
    # -- dates
    ('TODAY()', 'F.current_date()', 'date', 1),
    ('NOW()', 'F.current_timestamp()', 'datetime', 1),
    ('YEAR([d])', 'F.year(F.col("d"))', 'integer', 0),
    ('MONTH([d])', 'F.month(F.col("d"))', 'integer', 0),
    ('DAY([d])', 'F.dayofmonth(F.col("d"))', 'integer', 0),
    ('YEAR([ts])', 'F.year(F.col("ts"))', 'integer', 0),
    ('MONTH([ts])', 'F.month(F.col("ts"))', 'integer', 0),
    ('DAY([ts])', 'F.dayofmonth(F.col("ts"))', 'integer', 0),
    ('YEAR(TODAY())', 'F.year(F.current_date())', 'integer', 1),
    ('YEAR(#2024-03-05#)', "F.year(F.to_date(F.lit('2024-03-05')))", 'integer', 0),
    ('DATE([d])', 'F.to_date(F.col("d"))', 'date', 0),
    ('DATE([ts])', 'F.to_date(F.col("ts"))', 'date', 0),
    ('DATETIME([d])', 'F.to_timestamp(F.col("d"))', 'datetime', 0),
    ('DATETIME([ts])', 'F.to_timestamp(F.col("ts"))', 'datetime', 0),
    ('DATEPART("year", [d])', 'F.year(F.col("d"))', 'integer', 0),
    ('DATEPART("month", [ts])', 'F.month(F.col("ts"))', 'integer', 0),
    ("DATEPART('day', [d])", 'F.dayofmonth(F.col("d"))', 'integer', 0),
    ('DATEPART("quarter", [d])', 'F.quarter(F.col("d"))', 'integer', 0),
    ('DATEPART("dayofyear", [d])', 'F.dayofyear(F.col("d"))', 'integer', 0),
    ('DATEPART("hour", [ts])', 'F.hour(F.col("ts"))', 'integer', 0),
    ("DATEPART('minute', [ts])", 'F.minute(F.col("ts"))', 'integer', 0),
    ("DATEPART('second', [ts])", 'F.second(F.col("ts"))', 'integer', 0),
    ('DATEDIFF("day", [d], [e])', 'F.datediff(F.col("e"), F.col("d"))', 'integer', 0),
    ("DATEDIFF('Day', [d], [e])", 'F.datediff(F.col("e"), F.col("d"))', 'integer', 0),
    ('DATEDIFF("month", [d], [e])', '(F.year(F.col("e")) - F.year(F.col("d"))) * 12 + (F.month(F.col("e")) - F.month(F.col("d")))', 'integer', 0),
    ('DATEDIFF("quarter", [d], [e])', '(F.year(F.col("e")) - F.year(F.col("d"))) * 4 + (F.quarter(F.col("e")) - F.quarter(F.col("d")))', 'integer', 0),
    ('DATEDIFF("year", [d], [e])', 'F.year(F.col("e")) - F.year(F.col("d"))', 'integer', 0),
    ("DATEDIFF('day', [ts], [ts])", 'F.datediff(F.col("ts"), F.col("ts"))', 'integer', 0),
    ("DATEDIFF('day', [d], [ts])", 'F.datediff(F.col("ts"), F.col("d"))', 'integer', 0),
    ("DATEDIFF('day', TODAY(), [d])", 'F.datediff(F.col("d"), F.current_date())', 'integer', 1),
    ('DATETRUNC("month", [d])', '''F.trunc(F.col("d"), 'month')''', 'date', 0),
    ('DATETRUNC("quarter", [d])', '''F.trunc(F.col("d"), 'quarter')''', 'date', 0),
    ('DATETRUNC("year", [d])', '''F.trunc(F.col("d"), 'year')''', 'date', 0),
    ('DATETRUNC("day", [d])', 'F.col("d")', 'date', 0),
    ('DATETRUNC("month", [ts])', '''F.date_trunc('month', F.col("ts"))''', 'datetime', 0),
    ('DATETRUNC("quarter", [ts])', '''F.date_trunc('quarter', F.col("ts"))''', 'datetime', 0),
    ('DATETRUNC("year", [ts])', '''F.date_trunc('year', F.col("ts"))''', 'datetime', 0),
    ('DATETRUNC("day", [ts])', '''F.date_trunc('day', F.col("ts"))''', 'datetime', 0),
    ('DATETRUNC("hour", [ts])', '''F.date_trunc('hour', F.col("ts"))''', 'datetime', 0),
    ('DATETRUNC("minute", [ts])', '''F.date_trunc('minute', F.col("ts"))''', 'datetime', 0),
    ('DATETRUNC("month", TODAY())', "F.trunc(F.current_date(), 'month')", 'date', 1),
    ('[d] = #2024-03-05#', '''F.col("d") == F.to_date(F.lit('2024-03-05'))''', 'boolean', 0),
    ('[ts] >= #2024-03-05 10:20:30#', '''F.col("ts") >= F.to_timestamp(F.lit('2024-03-05 10:20:30'))''', 'boolean', 0),
    # -- logic and comparisons
    ('[a] = 1', 'F.col("a") == 1', 'boolean', 0),
    ('[a] == 1', 'F.col("a") == 1', 'boolean', 0),
    ('[a] <> 1', 'F.col("a") != 1', 'boolean', 0),
    ('[a] != 1', 'F.col("a") != 1', 'boolean', 0),
    ('[a] != [b]', 'F.col("a") != F.col("b")', 'boolean', 0),
    ('[a] == [b]', 'F.col("a") == F.col("b")', 'boolean', 0),
    ('[x] > 1', 'F.col("x") > 1', 'boolean', 0),
    ('[a] > 1.5', 'F.col("a") > 1.5', 'boolean', 0),
    ('[a] + [b] > [c] * 2', 'F.col("a") + F.col("b") > F.col("c") * 2', 'boolean', 0),
    ('([a] > 1) = ([b] > 1)', '(F.col("a") > 1) == (F.col("b") > 1)', 'boolean', 0),
    ('YEAR([d]) = 2020', 'F.year(F.col("d")) == 2020', 'boolean', 0),
    ('[d] = [e]', 'F.col("d") == F.col("e")', 'boolean', 0),
    ('[d] > [e]', 'F.col("d") > F.col("e")', 'boolean', 0),
    ('[d] < [ts]', 'F.col("d") < F.col("ts")', 'boolean', 0),
    ('[ts] > [d]', 'F.col("ts") > F.col("d")', 'boolean', 0),
    ('[flag] = TRUE', 'F.col("flag") == F.lit(True)', 'boolean', 0),
    ('[flag] = FALSE', 'F.col("flag") == F.lit(False)', 'boolean', 0),
    ('[flag] <> TRUE', 'F.col("flag") != F.lit(True)', 'boolean', 0),
    ('[s] = "x"', """F.col("s") == 'x'""", 'boolean', 1),
    ('[s] = ""', """F.col("s") == ''""", 'boolean', 1),
    ("[s] <> ''", """F.col("s") != ''""", 'boolean', 1),
    ('[s] > "a"', """F.col("s") > 'a'""", 'boolean', 1),
    ('[s] <= [t]', 'F.col("s") <= F.col("t")', 'boolean', 1),
    ('[s] > [t]', 'F.col("s") > F.col("t")', 'boolean', 1),
    ('[u] > 1', 'F.col("u") > 1', 'boolean', 1),
    ('[u] = [s]', 'F.col("u") == F.col("s")', 'boolean', 2),
    ('[a] IN (1, 2)', 'F.col("a").isin(1, 2)', 'boolean', 0),
    ('[a] IN (-1, 2)', 'F.col("a").isin(-1, 2)', 'boolean', 0),
    ('[a] IN (1.5, 2)', 'F.col("a").isin(1.5, 2)', 'boolean', 0),
    ('NOT [a] IN (1)', '~F.col("a").isin(1)', 'boolean', 0),
    ('[s] IN ("a", "b")', '''F.col("s").isin('a', 'b')''', 'boolean', 1),
    ('[s] IN ("a")', '''F.col("s").isin('a')''', 'boolean', 1),
    ('NOT [flag]', '~F.col("flag")', 'boolean', 0),
    ('NOT TRUE', '~F.lit(True)', 'boolean', 0),
    ('not ([a] > 1)', '~(F.col("a") > 1)', 'boolean', 0),
    ('NOT [a] > 1', '~(F.col("a") > 1)', 'boolean', 0),
    ('[flag] AND [g]', 'F.col("flag") & F.col("g")', 'boolean', 1),
    ('[flag] OR NOT [g]', 'F.col("flag") | ~F.col("g")', 'boolean', 1),
    ('[a] > 1 AND [b] < 2', '(F.col("a") > 1) & (F.col("b") < 2)', 'boolean', 1),
    ('[a] > 1 and [b] < 2', '(F.col("a") > 1) & (F.col("b") < 2)', 'boolean', 1),
    ('[a] > 1 or [b] < 2', '(F.col("a") > 1) | (F.col("b") < 2)', 'boolean', 1),
    ('[a] >= 1 AND [a] <= 5', '(F.col("a") >= 1) & (F.col("a") <= 5)', 'boolean', 1),
    ('[flag] AND [a] > 1', 'F.col("flag") & (F.col("a") > 1)', 'boolean', 1),
    ('[a] > 1 AND [flag] OR [g]', '(F.col("a") > 1) & F.col("flag") | F.col("g")', 'boolean', 1),
    ('NOT [flag] AND [g]', '~F.col("flag") & F.col("g")', 'boolean', 1),
    ('ISNULL([a]) AND ISNULL([b])', 'F.col("a").isNull() & F.col("b").isNull()', 'boolean', 1),
    ('FALSE OR TRUE', 'F.lit(False) | F.lit(True)', 'boolean', 1),
    ('TRUE AND FALSE', 'F.lit(True) & F.lit(False)', 'boolean', 1),
]

REFUSED = [
    # formula, reason, model_only
    # -- aggregates, table calculations and user functions stay in the model
    ('SUM([x])', 'aggregate function SUM (kept as a DAX measure)', True),
    ('SUM([a])', 'aggregate function SUM (kept as a DAX measure)', True),
    ('AVG([x])', 'aggregate function AVG (kept as a DAX measure)', True),
    ('AVG([a]) + 1', 'aggregate function AVG (kept as a DAX measure)', True),
    ('COUNTD([s])', 'aggregate function COUNTD (kept as a DAX measure)', True),
    ('MEDIAN([x])', 'aggregate function MEDIAN (kept as a DAX measure)', True),
    ('ATTR([s])', 'aggregate function ATTR (kept as a DAX measure)', True),
    ('MAX([x])', 'aggregate function MAX (kept as a DAX measure)', True),
    ('MIN([x])', 'aggregate function MIN (kept as a DAX measure)', True),
    ('RUNNING_SUM(SUM([a]))', 'aggregate function SUM (kept as a DAX measure)', True),
    ('INDEX()', 'table calculation INDEX (no row-level equivalent)', True),
    ('FIRST()', 'table calculation FIRST (no row-level equivalent)', True),
    ('SIZE()', 'table calculation SIZE (no row-level equivalent)', True),
    ('PREVIOUS_VALUE(0)', 'table calculation PREVIOUS_VALUE (no row-level equivalent)', True),
    ('USERNAME()', 'user function USERNAME (handled by row-level security)', True),
    ('FULLNAME()', 'user function FULLNAME (handled by row-level security)', True),
    ('USERDOMAIN()', 'user function USERDOMAIN (handled by row-level security)', True),
    ('ISMEMBEROF("g")', 'user function ISMEMBEROF (handled by row-level security)', True),
    # -- not converted: row-level but unsupported or unverified
    ('{FIXED [a] : SUM([x])}', 'LOD expression', False),
    ('{SUM([a])}', 'LOD expression', False),
    ('{ FIXED : SUM([a]) }', 'LOD expression', False),
    ('NULL', 'the formula is always NULL', False),
    ('[Parameters].[p]', 'qualified Parameters.p', False),
    ('[nope]', 'unknown field nope', False),
    ('UNKNOWNFUNC([a])', 'unsupported function UNKNOWNFUNC', False),
    ('ASCII([s])', 'unsupported function ASCII', False),
    ('SPACE(3)', 'unsupported function SPACE', False),
    ('REGEXP_MATCH([s], "a")', 'unsupported function REGEXP_MATCH', False),
    ('DATENAME("month", [d])', 'unsupported function DATENAME', False),
    ('MAKEPOINT([x], [y])', 'unsupported function MAKEPOINT', False),
    ('SCRIPT_REAL("x", [x])', 'unsupported function SCRIPT_REAL', False),
    ('RAWSQL_INT("1")', 'unsupported function RAWSQL_INT', False),
    ('[a] % 3', 'modulo (%) is not converted: negative-operand semantics are not verified', False),
    ('2 ^ 3 ^ 2', 'chained power operator', False),
    ('SIGN([a])', 'SIGN is not converted', False),
    ('DIV([a], [b])', 'DIV is not converted (negative-operand semantics not verified)', False),
    ('PROPER([s])', 'PROPER is not converted (Spark initcap differs after hyphens)', False),
    ('MAX([a], [b])', 'scalar MAX of two values is not converted (NULL handling differs)', False),
    ('MIN([x], [y])', 'scalar MIN of two values is not converted (NULL handling differs)', False),
    ('DATEADD("day", 1, [d])', 'DATEADD is not converted (month-end clamping and part semantics not verified)', False),
    ('[d] + 1', 'date arithmetic is not converted (use DATEDIFF/DATEADD)', False),
    ('[d] - [e]', 'date arithmetic is not converted (use DATEDIFF/DATEADD)', False),
    ('[d] * 2', 'date arithmetic is not converted (use DATEDIFF/DATEADD)', False),
    ('[x] + [d]', 'date arithmetic is not converted (use DATEDIFF/DATEADD)', False),
    ('TODAY() + 1', 'date arithmetic is not converted (use DATEDIFF/DATEADD)', False),
    ('NOW() - NOW()', 'date arithmetic is not converted (use DATEDIFF/DATEADD)', False),
    ('INT([s])', 'INT of a string is not converted (Tableau parses the text; the Spark cast differs)', False),
    ('FLOAT([s])', 'FLOAT of a string is not converted (Tableau parses the text; the Spark cast differs)', False),
    ('STR([x])', 'STR of a real value is not converted (text formatting differs)', False),
    ('STR([d])', 'STR of a date value is not converted (text formatting differs)', False),
    ('STR([flag])', 'STR of a boolean value is not converted (text formatting differs)', False),
    ('DATETRUNC("week", [d])', "DATETRUNC('week') is not converted", False),
    ('DATETRUNC("hour", [d])', "DATETRUNC('hour') is not converted", False),
    ('DATEPART("week", [d])', "DATEPART('week') is not converted", False),
    ('DATEPART("weekday", [d])', "DATEPART('weekday') is not converted", False),
    ('DATEPART("hour", [d])', "DATEPART('hour') needs a datetime argument", False),
    ('DATEDIFF("week", [d], [e])', "DATEDIFF('week') is not converted", False),
    ('DATEDIFF("hour", [ts], [ts])', "DATEDIFF('hour') is not converted", False),
    ("DATEDIFF('day', [d], [e], 'monday')", 'DATEDIFF with a start_of_week argument is not converted', False),
    ('DATEDIFF([s], [d], [e])', 'DATEDIFF needs a literal date part', False),
    ('DATE("2020-01-01")', 'DATE is only converted for date or datetime arguments', False),
    ('DATE([s])', 'DATE is only converted for date or datetime arguments', False),
    ('DATETIME([s])', 'DATETIME is only converted for date or datetime arguments', False),
    # -- arguments that must be literals
    ('ROUND([x], [n])', 'ROUND needs an integer literal for the precision', False),
    ('ROUND([x], 2.5)', 'ROUND needs an integer literal for the precision', False),
    ('LOG([x], 1)', 'LOG base must be a numeric literal > 0 and != 1', False),
    ('LOG([x], 0)', 'LOG base must be a numeric literal > 0 and != 1', False),
    ('LOG([x], -2)', 'LOG base must be a numeric literal > 0 and != 1', False),
    ('LOG([x], [n])', 'LOG base must be a numeric literal > 0 and != 1', False),
    ('REPLACE([s], "", "x")', 'REPLACE search string must not be empty', False),
    ('REPLACE([s], "a", [t])', 'REPLACE needs a string literal for the replacement', False),
    (r'REPLACE([s], "$", "\")', 'backslash in string literal', False),
    ('FIND([s], [t])', 'FIND needs a string literal for the substring', False),
    ('FIND([s], "a", [n])', 'FIND needs an integer literal for the start', False),
    ('FIND([s], "a", 0)', 'FIND start must be at least 1', False),
    ('FIND([s], "")', 'FIND substring must not be empty', False),
    ('SPLIT([s], "", 1)', 'SPLIT delimiter must not be empty', False),
    ('SPLIT([s], "|", -1)', 'SPLIT with a negative or zero token number is not converted', False),
    ('SPLIT([s], "|", 0)', 'SPLIT with a negative or zero token number is not converted', False),
    ('SPLIT([s], "|", [n])', 'SPLIT needs an integer literal for the token number', False),
    ('LEFT([s], -1)', 'LEFT length must be at least 0', False),
    ('RIGHT([s], 0)', 'RIGHT length must be at least 1', False),
    ('MID([s], 0)', 'MID start must be at least 1', False),
    ('MID([s], -1, 3)', 'MID start must be at least 1', False),
    ('MID([s], 2, -3)', 'MID length must be at least 0', False),
    ('"a" IN [s]', 'IN with a set or a field is not converted (literal list only)', False),
    ('[a] IN ()', "unexpected ')'", False),
    ('[a] IN (1, NULL)', 'IN list items must be number or string literals', False),
    ('[flag] IN (TRUE)', 'IN list items must be number or string literals', False),
    # -- type errors
    ('[a] + "x"', "'+' mixes a string with an integer value", False),
    ('[a] + [s]', "'+' mixes a string with an integer value", False),
    ('[s] + 1 + [t]', "'+' mixes a string with an integer value", False),
    ('[u] + [u]', "cannot tell numeric '+' from string concatenation: operand types are unknown", False),
    ('[flag] + 1', "operator '+' on a boolean value", False),
    ('[flag] * 2', "operator '*' on a boolean value", False),
    ('[s] * 2', "operator '*' on a string value", False),
    ('[s] - [t]', "operator '-' on a string value", False),
    ('ABS([s])', 'ABS expects a numeric argument, got a string value', False),
    ('INT([d])', 'INT expects a numeric argument, got a date value', False),
    ('INT([flag])', 'INT expects a numeric argument, got a boolean value', False),
    ('LOG([s])', 'LOG expects a numeric argument, got a string value', False),
    ('ROUND([s])', 'ROUND expects a numeric argument, got a string value', False),
    ('LEN([a])', 'LEN expects a string argument, got an integer value', False),
    ('UPPER([a])', 'UPPER expects a string argument, got an integer value', False),
    ('TRIM([a])', 'TRIM expects a string argument, got an integer value', False),
    ('CONTAINS([s], 1)', 'CONTAINS expects a string argument, got an integer value', False),
    ('CONTAINS([a], "1")', 'CONTAINS expects a string argument, got an integer value', False),
    ('DATETRUNC("month", [s])', 'DATETRUNC expects a date argument, got a string value', False),
    ('DATEPART("year", [s])', 'DATEPART expects a date argument, got a string value', False),
    ('DATEDIFF("day", [d], [s])', 'DATEDIFF expects date arguments, got a string value', False),
    ('ZN([s])', 'ZN expects a numeric argument, got a string value', False),
    ('ZN([flag])', 'ZN expects a numeric argument, got a boolean value', False),
    ('ZN([d])', 'ZN expects a numeric argument, got a date value', False),
    ('IFNULL([x], [s])', 'branches have incompatible types (real, string)', False),
    ('ISNULL()', 'ISNULL expects 1 argument(s), got 0', False),
    ('ZN()', 'ZN expects 1 argument(s), got 0', False),
    ('[s] = 1', 'cannot compare a string value with an integer value', False),
    ('[a] > [s]', 'cannot compare an integer value with a string value', False),
    ('[flag] = [a]', 'cannot compare a boolean value with an integer value', False),
    ('[d] > "2020-01-01"', 'cannot compare a date value with a string value', False),
    ('1 < [a] < 3', 'chained comparison', False),
    ('[a] > 1 = [b] > 1', 'chained comparison', False),
    ('NOT [a]', 'NOT operand is an integer value, not a boolean', False),
    ('NOT [d]', 'NOT operand is a date value, not a boolean', False),
    ('[a] OR [b]', 'OR operand is an integer value, not a boolean', False),
    ('[a] and [b]', 'AND operand is an integer value, not a boolean', False),
    ('[a] IN (1, "b")', 'branches have incompatible types (integer, string)', False),
    ('IF [a] > 0 THEN "p" ELSE 0 END', 'branches have incompatible types (integer, string)', False),
    ('IF [a] THEN 1 ELSE 0 END', 'IF condition is an integer value, not a boolean', False),
    ('IF [a] > 0 THEN 1 ELSEIF [s] THEN 2 END', 'IF condition is a string value, not a boolean', False),
    ('CASE [a] WHEN 1 THEN "x" WHEN 2 THEN 5 END', 'branches have incompatible types (integer, string)', False),
    ('CASE [a] WHEN "x" THEN 1 END', 'cannot compare an integer value with a string value', False),
    ('CASE WHEN [a] > 1 THEN 1 END', 'CASE without an expression (searched CASE) is not converted', False),
    ('IIF([a] > 1, 1)', 'IIF expects 3 to 4 argument(s), got 2', False),
    ('IIF([a], 1, 2)', 'IIF test is an integer value, not a boolean', False),
    ('IIF([a] > 1, "x", 2)', 'branches have incompatible types (integer, string)', False),
    # -- syntax and literals
    ('[a', 'unterminated field reference', False),
    ('"unterminated', 'unterminated string literal', False),
    ('[a] +', 'unexpected end of formula', False),
    ('[a] // c\n +', 'unexpected end of formula', False),
    ('[a] > 1 AND', 'unexpected end of formula', False),
    ('([a]', "expected ')', found end of formula", False),
    ('(([a]', "expected ')', found end of formula", False),
    ('IF [a] > 0 THEN 1', 'expected END, found end of formula', False),
    ('IF [a] > 0 THEN 1 ELSE 2', 'expected END, found end of formula', False),
    ('CASE [s] WHEN "a" THEN 1 ELSE 0', 'expected END, found end of formula', False),
    ('?', "unexpected character '?'", False),
    ('[a] ?? 1', "unexpected character '?'", False),
    ('1_000', 'malformed number', False),
    ('0x10', 'malformed number', False),
    ('1e999', 'numeric literal out of range', False),
    ('99999999999999999999', 'numeric literal out of range', False),
    ('9223372036854775808', 'numeric literal out of range', False),
    ('-9223372036854775808', 'numeric literal out of range', False),
    ('#2024-13-45#', 'invalid date literal', False),
    ('#2024-03-05 25:61:61#', 'invalid date literal', False),
    (r'"back\slash"', 'backslash in string literal', False),
    (r'REPLACE([s], "a\", "b$")', 'backslash in string literal', False),
    ('', 'empty formula', False),
    ('   ', 'empty formula', False),
    ('\n', 'empty formula', False),
]


def _ids(rows):
    return [row[0] for row in rows]


def test_tables_have_no_duplicate_formulas():
    for table in (EXACT, REFUSED):
        formulas = [row[0] for row in table]
        assert len(formulas) == len(set(formulas))
    assert not {row[0] for row in EXACT} & {row[0] for row in REFUSED}


def test_tables_only_use_fields_the_test_resolver_knows():
    # Guards the oracle: a typo in a field name would otherwise read as "refused".
    for formula, *_ in EXACT:
        assert all(name in TYPES for name in fields_in(formula)), formula


@pytest.mark.parametrize('formula, expression, datatype, nnotes', EXACT, ids=_ids(EXACT))
def test_exact_conversion(formula, expression, datatype, nnotes):
    result = convert(formula)
    assert result.ok, result.reason
    assert result.expression == expression
    assert result.datatype == datatype
    assert len(result.notes) == nnotes
    assert result.reason is None
    assert result.model_only is False
    # Independent oracle: a regular expression over the Tableau text.
    assert result.columns == fields_in(formula)
    assert gate_reason(expression) is None


@pytest.mark.parametrize('formula, reason, model_only', REFUSED, ids=_ids(REFUSED))
def test_refused_conversion(formula, reason, model_only):
    result = convert(formula)
    assert not result.ok
    assert result.reason == reason
    assert result.model_only is model_only
    # A refusal carries no partial result.
    assert result.expression is None
    assert result.columns == []
    assert result.notes == []
    assert result.datatype == 'unknown'


# ── caveat notes ───────────────────────────────────────────────────

NOTE_FRAGMENTS = {
    'logic': 'three-valued logic',
    'case': 'case-sensitive in Spark',
    'strcmp': 'string comparison follows Spark',
    'ansi': 'spark.sql.ansi.enabled',
    'round': 'HALF_UP',
    'nan': 'NaN outside',
    'substr': 'follow Spark substr()',
    'split': 'Spark split() semantics',
    'trim': 'removes spaces only',
    'snapshot': 'evaluated when the notebook runs',
    'unknown': 'is unknown',
    'cast': 'numeric cast',
}


def note_keys(result):
    return {key for key, fragment in NOTE_FRAGMENTS.items()
            if any(fragment in note for note in result.notes)}


NOTE_CASES = [
    ('[a] + [b] * 2', set()),
    ('[a] > 1', set()),
    ('IF [a] > 1 THEN 1 ELSE 2 END', set()),
    ('[a] IN (1, 2)', set()),
    ('CASE [a] WHEN 1 THEN 2 END', set()),
    ('ZN([a])', set()),
    ('LEFT([s], 3)', set()),
    ('DATEDIFF("day", [d], [e])', set()),
    ('[a] / [b]', {'ansi'}),
    ('[x] / 0', {'ansi'}),
    ('ZN([a] / [b])', {'ansi'}),
    ('[a] > 1 AND [b] < 2', {'logic'}),
    ('[flag] OR [g] OR [flag]', {'logic'}),
    ('NOT [flag]', set()),
    ('[s] = "x"', {'strcmp'}),
    ('[s] <= [t]', {'strcmp'}),
    ('[s] IN ("a", "b")', {'strcmp'}),
    ('CASE [s] WHEN "a" THEN 1 END', {'strcmp'}),
    ('CONTAINS([s], "a")', {'case'}),
    ('FIND([s], "a")', {'case'}),
    ('STARTSWITH([s], "a")', {'case', 'trim'}),
    ('ENDSWITH([s], "a")', {'case', 'trim'}),
    ('SPLIT([s], "|", 1)', {'split'}),
    ('ROUND([x])', {'round'}),
    ('ROUND([x], 2)', {'round'}),
    ('SQRT([x])', {'nan'}),
    ('ASIN([x])', {'nan'}),
    ('ACOS([x])', {'nan'}),
    ('LEFT([s], [n])', {'substr'}),
    ('RIGHT([s], [n])', {'substr'}),
    ('MID([s], [a], 3)', {'substr'}),
    ('TODAY()', {'snapshot'}),
    ('NOW()', {'snapshot'}),
    ('YEAR(TODAY())', {'snapshot'}),
    ('[u] + 1', {'unknown'}),
    ('INT([u])', {'unknown', 'cast'}),
    ('FLOAT([u])', {'unknown', 'cast'}),
    ('[a] / [b] > 1 AND CONTAINS([s], "a")', {'ansi', 'logic', 'case'}),
]


@pytest.mark.parametrize('formula, keys', NOTE_CASES, ids=_ids(NOTE_CASES))
def test_notes_appear_for_exactly_the_right_constructs(formula, keys):
    result = convert(formula)
    assert result.ok, result.reason
    assert note_keys(result) == keys
    # Every note belongs to one of the known caveats and none is repeated.
    assert len(result.notes) == len(set(result.notes))
    assert all(any(f in note for f in NOTE_FRAGMENTS.values()) for note in result.notes)


def test_a_note_is_added_once_however_often_the_construct_repeats():
    result = convert('[flag] AND [g] AND [flag] OR [g]')
    assert result.ok
    assert len(result.notes) == 1
    result = convert('[a] / [b] + [a] / [c]')
    assert len(result.notes) == 1


def test_the_logic_note_names_the_documented_divergence():
    note = convert('[flag] AND [g]').notes[0]
    assert 'NULL AND FALSE is FALSE' in note
    assert 'Tableau documentation' in note
    assert 'not verified' in note


def test_snapshot_notes_name_the_function():
    assert convert('TODAY()').notes[0].startswith('TODAY()')
    assert convert('NOW()').notes[0].startswith('NOW()')


def test_unknown_type_note_names_the_column():
    result = convert('[u] > 1')
    assert result.notes == ['the type of [u] is unknown; the expression assumes it fits the way it is used']


def test_known_types_never_carry_the_unknown_type_note():
    for formula, *_ in EXACT:
        if 'u' in fields_in(formula):
            continue
        assert not note_keys(convert(formula)) & {'unknown'}, formula


# ── the resolver contract ──────────────────────────────────────────

def test_the_resolver_is_called_with_qualifier_and_name():
    calls = []

    def spy(qualifier, name):
        calls.append((qualifier, name))
        return Ref('column', name=name, datatype='integer')

    assert convert('[a] + [Parameters].[p] + [b]', spy).ok
    assert calls == [(None, 'a'), ('Parameters', 'p'), (None, 'b')]


def test_a_missing_reference_refuses_with_its_reason():
    def missing(qualifier, name):
        return Ref('missing', reason=f'{name} is gone')

    result = convert('[a] + 1', missing)
    assert not result.ok
    assert result.reason == 'a is gone'


@pytest.mark.parametrize('ref', [
    Ref('missing'),
    Ref('column'),
    Ref('weird', name='a'),
], ids=['missing-without-reason', 'column-without-name', 'unknown-kind'])
def test_an_unusable_reference_is_refused(ref):
    result = convert('[a] + 1', lambda q, n: ref)
    assert not result.ok
    assert result.reason == 'unresolved field [a]'


def test_a_column_reference_may_rename_the_physical_column():
    result = convert('[a] + 1', lambda q, n: Ref('column', name='Order Amount', datatype='integer'))
    assert result.expression == 'F.col("Order Amount") + 1'
    assert result.columns == ['Order Amount']


def test_columns_are_unique_and_in_order_of_first_use():
    result = convert('[b] + [a] + [b] + [c] + [a]')
    assert result.columns == ['b', 'a', 'c']


def test_the_default_resolver_treats_fields_as_columns_of_unknown_type():
    result = convert_default('IF [k] THEN 1 END')
    assert result.ok
    assert result.expression == 'F.when(F.col("k"), 1)'
    assert result.datatype == 'integer'


def convert_default(formula):
    from powerbi_import.pyspark_converter import convert_to_pyspark
    return convert_to_pyspark(formula)


def test_the_default_resolver_refuses_qualified_references():
    result = convert_default('[Parameters].[p] + 1')
    assert not result.ok
    assert result.reason == 'qualified field reference [Parameters].[p]'


def test_the_converter_never_returns_a_conversion_with_both_expression_and_reason():
    for formula, *_ in EXACT + REFUSED:
        result = convert(formula)
        assert (result.expression is None) != (result.reason is None), formula


def test_resolve_is_the_documented_test_resolver():
    assert resolve(None, 'a').datatype == 'integer'
    assert resolve('P', 'a').kind == 'missing'
    assert resolve(None, 'zzz').kind == 'missing'
