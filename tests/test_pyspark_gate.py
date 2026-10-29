"""Tests for the default-deny PySpark statement gate (``pyspark_gate``).

The gate is a pure AST validator: it must accept the shapes the converter
emits, refuse everything else with a reason, and never raise.  A second check
runs every accepted statement in ``tests/pyspark_stub.py`` (an executable model
of the PySpark calling conventions, not Spark) and replays the refused hazards
there to confirm that each one really misbehaves.
"""

import datetime
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from powerbi_import import pyspark_gate as gate  # noqa: E402
from powerbi_import.pyspark_gate import validate_statement  # noqa: E402
from tests import pyspark_stub as stub  # noqa: E402

DAY = datetime.date(2024, 3, 17)
STAMP = datetime.datetime(2024, 5, 17, 3, 4, 5)


def statement(expression, name='c'):
    return f'df = df.withColumn("{name}", {expression})'


# (expression, a row that provides every column it reads)
ACCEPTED = [
    ('F.col("a")', {'a': 1}),
    ('F.lit(None)', {}),
    ('F.lit(1)', {}),
    ('F.lit(-1.5)', {}),
    ('F.lit("x")', {}),
    ('F.lit(True)', {}),
    ('F.col("a") * 2', {'a': 3}),
    ('F.col("a") + F.col("b") * F.lit(2)', {'a': 1, 'b': 2}),
    ('(F.col("a") + 1) / F.col("b")', {'a': 1, 'b': 2}),
    ('F.col("a") % 3', {'a': 7}),
    ('-F.col("a")', {'a': 1}),
    ('~F.col("a")', {'a': True}),
    ('F.when(F.col("flag"), 1)', {'flag': True}),
    ('F.col("a") + -5', {'a': 1}),
    ('(F.col("a") > 1) & (F.col("b") < 5)', {'a': 2, 'b': 3}),
    ('(F.col("a") > 1) | F.col("b").isNull()', {'a': 0, 'b': None}),
    ('~(F.col("a") > 1)', {'a': 2}),
    ('F.col("a") == F.lit(True)', {'a': True}),
    ('F.col("s") != "x"', {'s': 'y'}),
    ('F.when(F.col("a") > 1, F.lit("x")).otherwise(F.lit("y"))', {'a': 2}),
    ('F.when(F.col("a") > 1, "x").when(F.col("a") > 0, "y")', {'a': 1}),
    ('F.when(F.col("a") > 1, 1).when(F.col("a") < 0, 2).otherwise(3)', {'a': 0}),
    ('F.coalesce(F.col("a"), F.lit(0))', {'a': None}),
    ('F.concat(F.col("s"), F.lit("-"), F.col("t"))', {'s': 'a', 't': 'b'}),
    ('F.col("s").substr(1, 3)', {'s': 'abcdef'}),
    ('F.col("s").substr(-3, 3)', {'s': 'abcdef'}),
    ('F.col("s").substr(F.lit(2), F.length(F.col("s")))', {'s': 'abcdef'}),
    ('F.col("s").substr(-F.col("n"), F.col("n"))', {'s': 'abcdef', 'n': 2}),
    ('F.col("s").isin("a", "b")', {'s': 'a'}),
    ('F.col("s").isin(1, 2)', {'s': 1}),
    ('F.col("a").cast("long")', {'a': 2.5}),
    ('F.col("a").cast("double")', {'a': 2}),
    ('F.col("a").cast("string")', {'a': 2}),
    ('F.pow(F.col("a"), F.lit(2))', {'a': 3}),
    ('F.log(2.0, F.col("a"))', {'a': 8}),
    ('F.log(F.col("a"))', {'a': 8}),
    ('F.log10(F.col("a"))', {'a': 100}),
    ('F.sqrt(F.abs(F.col("a")))', {'a': -4}),
    ('F.round(F.col("a"))', {'a': 2.5}),
    ('F.round(F.col("a"), 2)', {'a': 2.675}),
    ('F.floor(F.col("a")) + F.ceil(F.col("a"))', {'a': 2.5}),
    ('F.locate("b", F.col("s"))', {'s': 'abc'}),
    ('F.locate("b", F.col("s"), 2)', {'s': 'abcb'}),
    (r'F.split(F.col("s"), "\\|").getItem(0)', {'s': 'a|b'}),
    ('F.trunc(F.col("d"), "month")', {'d': DAY}),
    ('F.date_trunc("month", F.col("t"))', {'t': STAMP}),
    ('F.datediff(F.col("d"), F.col("e"))', {'d': DAY, 'e': DAY}),
    ('F.regexp_replace(F.col("s"), "a", "b")', {'s': 'aa'}),
    ('F.current_date()', {}),
    ('F.datediff(F.current_date(), F.col("d"))', {'d': DAY}),
    ('F.year(F.col("d")) * 12 + F.month(F.col("d"))', {'d': DAY}),
    ('F.dayofmonth(F.col("d")) + F.dayofyear(F.col("d")) + F.quarter(F.col("d"))', {'d': DAY}),
    ('F.hour(F.col("t")) + F.minute(F.col("t")) + F.second(F.col("t"))', {'t': STAMP}),
    ('F.to_date(F.col("s"))', {'s': '2024-03-17'}),
    ('F.to_timestamp(F.col("s"))', {'s': '2024-03-17 01:02:03'}),
    ('F.upper(F.trim(F.col("s")))', {'s': ' a '}),
    ('F.lower(F.ltrim(F.rtrim(F.col("s"))))', {'s': ' A '}),
    ('F.col("s").startswith("a")', {'s': 'ab'}),
    ('F.col("s").endswith(F.col("t"))', {'s': 'ab', 't': 'b'}),
    ('F.col("s").contains(F.col("t"))', {'s': 'ab', 't': 'b'}),
    ('F.col("a").isNotNull()', {'a': 1}),
    ('F.sin(F.col("a")) + F.cos(F.col("a")) + F.tan(F.col("a")) + F.exp(F.col("a"))', {'a': 0.5}),
    ('F.asin(F.col("a")) + F.acos(F.col("a")) + F.atan(F.col("a"))', {'a': 0.5}),
    ('F.degrees(F.col("a")) + F.radians(F.col("a"))', {'a': 1.0}),
    ('F.col("`odd.name`")', {'`odd.name`': 1}),
]

# (statement or expression, a fragment of the expected reason)
REJECTED_EXPRESSIONS = [
    ('0 < F.col("a") < 5', 'chained comparisons are not allowed'),
    ('F.col("a") and F.col("b")', None),
    ('F.col("a") or F.col("b")', None),
    ('not F.col("a")', None),
    ('~5', "'~' needs a Column operand"),
    ('~"x"', "'~' needs a Column operand"),
    ('F.col("a") ** 2', None),
    ('F.col("a") // 2', None),
    ('F.col("a") in (1, 2)', None),
    ('F.col("a") is None', None),
    ('F.col("a") if True else F.col("b")', None),
    ('(lambda: F.col("a"))()', None),
    ('f"{F.col(\'a\')}"', None),
    ('F.col("a")[0]', None),
    ('[F.col("a")]', None),
    ('F.col("a").__class__', None),
    ('__import__("os").system("x")', None),
    ('open("x")', None),
    ('print(F.col("a"))', 'only F.<function>() and Column methods may be called'),
    ('F.foo(F.col("a"))', 'F.foo() is not allowed'),
    ('F.col("a").foo()', '.foo() is not allowed'),
    ('F.udf(F.col("a"))', None),
    ('F.expr("a + 1")', None),
    ('F.round(F.col("a"), scale=2)', 'keyword arguments are not allowed'),
    ('F.col("a").otherwise(1)', '.otherwise() needs an open F.when(...) chain'),
    ('F.otherwise(1)', 'F.otherwise() is not allowed'),
    ('F.when(F.col("a") > 1, 1).otherwise(2).otherwise(3)', None),
    ('F.when(F.col("a") > 1, 1).otherwise(2).when(F.col("a") > 0, 3)', None),
    ('F.when(5, 1)', None),
    ('F.upper("s")', None),
    ('F.coalesce(F.col("a"), 0)', None),
    ('F.col(5)', None),
    ('F.col("")', 'F.col() needs a non-empty column name'),
    ('F.col(F.col("a"))', None),
    ('F.lit(F.col("a"))', None),
    ('F.lit([1])', None),
    ('F.round(F.col("a"), 2.5)', None),
    ('F.round(F.col("a"), F.col("b"))', None),
    ('F.date_trunc(F.col("d"), "month")', None),
    ('F.trunc(F.col("d"), "week")', "F.trunc() format 'week' is not allowed"),
    ('F.trunc(F.col("d"), F.col("f"))', None),
    ('F.date_trunc("week", F.col("t"))', None),
    ('F.log(F.col("b"), F.col("a"))', 'F.log() base must be a float literal > 0 and != 1'),
    ('F.log(1.0, F.col("a"))', 'F.log() base must be a float literal > 0 and != 1'),
    ('F.log(0.0, F.col("a"))', 'F.log() base must be a float literal > 0 and != 1'),
    ('F.log(-2.0, F.col("a"))', 'F.log() base must be a float literal > 0 and != 1'),
    ('F.log(2, F.col("a"))', 'F.log() base must be a float literal > 0 and != 1'),
    ('F.locate("b", F.col("s"), 0)', 'F.locate() position must be at least 1'),
    ('F.locate("b", F.col("s"), -1)', 'F.locate() position must be at least 1'),
    ('F.locate("", F.col("s"))', None),
    ('F.locate(F.col("t"), F.col("s"))', None),
    ('F.split(F.col("s"), "")', None),
    ('F.regexp_replace(F.col("s"), F.col("p"), "x")', None),
    ('F.regexp_replace(F.col("s"), "a", 1)', None),
    ('F.col("s").substr(1, F.col("n"))', '.substr() needs two integer literals or two Columns'),
    ('F.col("s").substr(F.col("n"), 2)', '.substr() needs two integer literals or two Columns'),
    ('F.col("s").substr(1.5, 2)', None),
    ('F.col("a").cast("int")', ".cast() type 'int' is not allowed"),
    ('F.col("a").cast("decimal(10,2)")', None),
    ('F.col("a").cast(F.col("t"))', None),
    ('F.col("s").isin(F.col("t"))', '.isin() takes literal values only'),
    ('F.col("s").isin()', None),
    ('F.col("s").getItem(-1)', None),
    ('F.col("s").getItem(F.col("n"))', None),
    ('F.col("s").getItem(1.5)', None),
    ('F.col("a").contains(5)', None),
    ('5 + F.col("a")', 'the left operand of an arithmetic operator must be a Column'),
    ('-5 + F.col("a")', 'the left operand of an arithmetic operator must be a Column'),
    ('F.col("a") + "x"', None),
    ('"x" + F.col("a")', None),
    ('F.col("a") + None', None),
    ('F.col("a") & 1', None),
    ('5 > F.col("a")', None),
    ('F.col("a") == None', None),
    ('F.col("a") == True', None),
    ('F.col("a") > None', None),
    ('F.lit(1e999)', None),
    ('F.lit(1j)', None),
    ('F.lit(b"x")', None),
    ('F.lit(...)', None),
]

REJECTED_STATEMENTS = [
    ('', 'empty statement'),
    ('   \n  ', 'empty statement'),
    ('# only a comment', 'expected exactly one statement'),
    (None, 'empty statement'),
    (123, 'empty statement'),
    (b'df = df.withColumn("a", F.col("a"))', 'empty statement'),
    ('df = df.withColumn("a", F.col("a"))\ndf = df.withColumn("b", F.col("a"))',
     'expected exactly one statement'),
    ('x = df.withColumn("a", F.col("a"))', "expected 'df = df.withColumn(...)'"),
    ('df = df.select("a")', "expected 'df = df.withColumn(...)'"),
    ('df = other.withColumn("a", F.col("a"))', "expected 'df = df.withColumn(...)'"),
    ('df.withColumn("a", F.col("a"))', "expected 'df = df.withColumn(...)'"),
    ('import os', "expected 'df = df.withColumn(...)'"),
    ('df = df.withColumn("", F.col("a"))', None),
    ('df = df.withColumn(1, F.col("a"))', None),
    ('df = df.withColumn(name, F.col("a"))', None),
    ('df = df.withColumn("a")', None),
    ('df = df.withColumn("a", F.col("a"), 3)', None),
    ('df = df.withColumn("a", col=F.col("a"))', None),
    ('df = df.withColumn("a", 5)', 'the expression is a Python literal, not a Column'),
    ('df = df.withColumn("a", "x")', 'the expression is a Python literal, not a Column'),
    ('df = df.withColumn("a", None)', 'the expression is a Python literal, not a Column'),
    ('df = df.withColumn("a", F.col("a")', 'not valid Python'),
    ('df = df.withColumn("a", F.col("a"))\x00', None),
]


class TestAcceptedShapes:
    @pytest.mark.parametrize('expression,row', ACCEPTED, ids=[e for e, _ in ACCEPTED])
    def test_gate_accepts_and_the_model_runs_it(self, expression, row):
        assert validate_statement(statement(expression)) is None
        # The accepted grammar never needs a Python builtin and never raises
        # a calling-convention error in the model.
        name, _ = stub.run_statement(statement(expression), row)
        assert name == 'c'

    def test_statement_may_use_single_quotes_and_unicode_names(self):
        assert validate_statement("df = df.withColumn('prix net', F.col('é'))") is None

    def test_trailing_newline_and_comments_are_tolerated(self):
        assert validate_statement(statement('F.col("a")') + '  # note\n') is None


class TestRejectedShapes:
    @pytest.mark.parametrize('expression,reason', REJECTED_EXPRESSIONS,
                             ids=[e for e, _ in REJECTED_EXPRESSIONS])
    def test_expression_is_refused_with_a_reason(self, expression, reason):
        got = validate_statement(statement(expression))
        assert isinstance(got, str) and got
        if reason:
            assert reason in got

    @pytest.mark.parametrize('text,reason', REJECTED_STATEMENTS,
                             ids=[repr(t)[:60] for t, _ in REJECTED_STATEMENTS])
    def test_statement_shape_is_refused_with_a_reason(self, text, reason):
        got = validate_statement(text)
        assert isinstance(got, str) and got
        if reason:
            assert reason in got


# (expression, a row, what the model raises)
HAZARDS = [
    ('0 < F.col("a") < 5', {'a': 1}, ValueError),
    ('F.col("a") and F.col("b")', {'a': True, 'b': True}, ValueError),
    ('F.col("a") or F.col("b")', {'a': True, 'b': True}, ValueError),
    ('not F.col("a")', {'a': True}, ValueError),
    ('F.when(F.col("a") > 1, 1).otherwise(2).otherwise(3)', {'a': 1}, ValueError),
    ('F.upper("s")', {'a': 'x'}, KeyError),
    ('F.coalesce(F.col("a"), 0)', {'a': None}, TypeError),
    ('F.col("a") + "x"', {'a': 'y'}, TypeError),
    ('F.col("a") & 1', {'a': True}, TypeError),
    ('F.col(5)', {}, TypeError),
    ('F.round(F.col("a"), 2.5)', {'a': 1.0}, TypeError),
    ('F.date_trunc(F.col("d"), "month")', {'d': DAY}, TypeError),
    ('F.col("s").substr(1, F.col("n"))', {'s': 'abc', 'n': 2}, TypeError),
    ('F.col("a").contains(5)', {'a': 'x'}, TypeError),
]


class TestRefusedHazardsMisbehave:
    """Every pattern the gate refuses for safety fails (or lies) in the model too."""

    @pytest.mark.parametrize('expression,row,error', HAZARDS, ids=[h[0] for h in HAZARDS])
    def test_gate_refuses_what_the_model_rejects(self, expression, row, error):
        assert validate_statement(statement(expression)) is not None
        with pytest.raises(error):
            stub.evaluate(expression, row)

    def test_bare_python_value_is_not_a_column(self):
        assert 'Python literal' in validate_statement('df = df.withColumn("a", 5)')
        with pytest.raises(TypeError):
            stub.run_statement('df = df.withColumn("a", 5)', {})

    def test_comparison_with_none_is_always_null(self):
        assert validate_statement(statement('F.col("a") == None')) is not None
        assert stub.evaluate('F.col("a") == None', {'a': 1}) is None
        assert stub.evaluate('F.col("a") == None', {'a': None}) is None

    def test_int_of_a_column_is_a_type_error(self):
        with pytest.raises(TypeError):
            int(stub.F.col('a'))


class TestColumnList:
    def test_unknown_column_is_refused(self):
        got = validate_statement(statement('F.col("zz")'), ['a', 'b'])
        assert "unknown column 'zz'" in got

    def test_known_columns_are_accepted(self):
        assert validate_statement(statement('F.col("a") + F.col("b")'), ['a', 'b']) is None

    def test_match_is_case_insensitive(self):
        assert validate_statement(statement('F.col("A")'), ['a']) is None

    def test_backticks_are_unquoted(self):
        assert validate_statement(statement('F.col("`a b`")'), ['a b']) is None
        assert validate_statement(statement('F.col("a b")'), ['a b']) is None

    def test_empty_list_refuses_every_column(self):
        assert validate_statement(statement('F.col("a")'), []) is not None
        assert validate_statement(statement('F.lit(1)'), []) is None

    def test_none_means_unchecked(self):
        assert validate_statement(statement('F.col("zz")'), None) is None

    def test_list_is_not_modified(self):
        columns = ['a', 'b']
        validate_statement(statement('F.col("a")'), columns)
        assert columns == ['a', 'b']


class TestAllowedSets:
    @pytest.mark.parametrize('fmt', sorted(gate.TRUNC_FORMATS))
    def test_trunc_formats_are_accepted(self, fmt):
        assert validate_statement(statement(f'F.trunc(F.col("d"), "{fmt}")')) is None
        stub.run_statement(statement(f'F.trunc(F.col("d"), "{fmt}")'), {'d': DAY})

    @pytest.mark.parametrize('fmt', sorted(gate.DATE_TRUNC_FORMATS))
    def test_date_trunc_formats_are_accepted(self, fmt):
        assert validate_statement(statement(f'F.date_trunc("{fmt}", F.col("t"))')) is None
        stub.run_statement(statement(f'F.date_trunc("{fmt}", F.col("t"))'), {'t': STAMP})

    @pytest.mark.parametrize('fmt', sorted(gate.CAST_TYPES))
    def test_cast_types_are_accepted(self, fmt):
        assert validate_statement(statement(f'F.col("a").cast("{fmt}")')) is None
        stub.run_statement(statement(f'F.col("a").cast("{fmt}")'), {'a': 2})

    def test_week_is_not_a_trunc_format(self):
        assert 'week' not in gate.TRUNC_FORMATS
        assert 'week' not in gate.DATE_TRUNC_FORMATS

    def test_every_allowed_function_exists_in_the_model(self):
        missing = [name for name in gate._F_FUNCTIONS if not hasattr(stub.F, name)]
        assert missing == []

    def test_every_allowed_column_method_exists_in_the_model(self):
        missing = [name for name in gate._COLUMN_METHODS if not hasattr(stub.Col, name)]
        assert missing == []


class TestIntegerLiterals:
    def test_int64_bounds(self):
        assert validate_statement(statement(f'F.lit({gate.INT64_MAX})')) is None
        assert validate_statement(statement(f'F.lit({gate.INT64_MAX + 1})')) is not None
        assert validate_statement(statement(f'F.lit(-{gate.INT64_MAX})')) is None
        assert validate_statement(statement(f'F.lit(-{gate.INT64_MAX + 2})')) is not None

    def test_large_integer_in_arithmetic_is_refused(self):
        assert validate_statement(statement(f'F.col("a") + {gate.INT64_MAX + 1}')) is not None

    def test_non_finite_float_is_refused(self):
        assert validate_statement(statement('F.lit(1e999)')) is not None
        assert validate_statement(statement('F.lit(-1e999)')) is not None


class TestRobustness:
    def test_deep_nesting_is_refused_not_raised(self):
        expression = 'F.col("a")'
        for _ in range(130):
            expression = f'F.abs({expression})'
        got = validate_statement(statement(expression))
        assert isinstance(got, str) and got

    def test_moderate_nesting_is_accepted(self):
        expression = 'F.col("a")'
        for _ in range(40):
            expression = f'F.abs({expression})'
        assert validate_statement(statement(expression)) is None

    def test_very_long_operator_chain_never_raises(self):
        expression = ' + '.join(['F.col("a")'] * 5000)
        got = validate_statement(statement(expression))
        assert got is None or isinstance(got, str)

    def test_very_many_arguments_never_raise(self):
        expression = 'F.coalesce(' + ', '.join(['F.col("a")'] * 400) + ')'
        got = validate_statement(statement(expression))
        assert isinstance(got, str) and got

    def test_nul_byte_is_refused(self):
        assert isinstance(validate_statement('df = df.withColumn("a\x00", F.col("a"))'), str)

    def test_hostile_text_never_raises(self):
        for text in ('\ud800', '(' * 500, 'df = df.withColumn("a", ' + '(' * 300 + ')',
                     'df = df.withColumn("a", F.col("a")) ' * 3, '\\', '"""', 'lambda'):
            got = validate_statement(text)
            assert isinstance(got, str) and got

    def test_result_is_stable(self):
        text = statement('F.col("a") + 1')
        assert validate_statement(text) == validate_statement(text)
