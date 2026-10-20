"""Numerical regressions for emitted row-scalar Power Query M.

The small, fail-closed interpreter implements only the emitted subset needed
here. It parses the actual converter output and evaluates lazy if branches;
it never reconstructs a result from the source DAX.
"""

import math
import re
import unittest

from powerbi_import.tmdl_m_conversion import _dax_to_m_expression
from tableau_export.dax_converter import convert_tableau_formula_to_dax


class EmittedScalarM:
    PRECEDENCE = {"or": 10, "and": 20, "=": 30, "<>": 30,
                  "<": 30, ">": 30, "<=": 30, ">=": 30,
                  "+": 40, "-": 40, "*": 50, "/": 50}

    def __init__(self, text):
        pattern = re.compile(
            r'\s+|\[(?:[^\]]|\]\])+\]|"(?:[^"]|"")*"'
            r'|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
            r'|[A-Za-z_]\w*(?:\.\w+)*|<>|<=|>=|[()+*/=,<>\-]')
        self.tokens = []
        end = 0
        for match in pattern.finditer(text):
            if match.start() != end:
                raise AssertionError("Unsupported M token: " + text[end:])
            end = match.end()
            if not match.group().isspace():
                self.tokens.append(match.group())
        if end != len(text):
            raise AssertionError("Unsupported M suffix: " + text[end:])
        self.position = 0
        self.tree = self.parse()
        if self.position != len(self.tokens):
            raise AssertionError("Unconsumed M tokens: " + str(self.tokens[self.position:]))

    def peek(self):
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self, expected=None):
        token = self.peek()
        if token is None or (expected is not None and token != expected):
            raise AssertionError(f"Expected {expected!r}, got {token!r}")
        self.position += 1
        return token

    def parse(self, minimum=0):
        token = self.take()
        if token == "if":
            condition = self.parse()
            self.take("then")
            yes = self.parse()
            self.take("else")
            no = self.parse()
            left = ("if", condition, yes, no)
        elif token == "(":
            left = self.parse()
            self.take(")")
        elif token in ("+", "-"):
            left = ("unary", token, self.parse(60))
        elif token in ("null", "true", "false"):
            left = ("literal", {"null": None, "true": True, "false": False}[token])
        elif token.startswith("["):
            left = ("field", token[1:-1].replace("]]", "]"))
        elif token.startswith('"'):
            left = ("literal", token[1:-1].replace('""', '"'))
        elif re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", token):
            left = ("literal", float(token))
        elif self.peek() == "(":
            self.take("(")
            arguments = []
            if self.peek() != ")":
                while True:
                    arguments.append(self.parse())
                    if self.peek() != ",":
                        break
                    self.take(",")
            self.take(")")
            left = ("call", token, arguments)
        else:
            raise AssertionError("Unsupported M operand: " + token)
        while self.peek() in self.PRECEDENCE and self.PRECEDENCE[self.peek()] >= minimum:
            operator = self.take()
            right = self.parse(self.PRECEDENCE[operator] + 1)
            left = ("binary", operator, left, right)
        return left

    def evaluate(self, row):
        def run(node):
            kind = node[0]
            if kind == "literal":
                return node[1]
            if kind == "field":
                return row[node[1]]
            if kind == "if":
                condition = run(node[1])
                if not isinstance(condition, bool):
                    raise AssertionError("M if requires a logical condition")
                return run(node[2] if condition else node[3])
            if kind == "unary":
                value = run(node[2])
                return None if value is None else (+value if node[1] == "+" else -value)
            if kind == "call":
                arguments = [run(arg) for arg in node[2]]
                if node[1] == "Number.RoundDown" and len(arguments) == 1:
                    return None if arguments[0] is None else math.floor(arguments[0])
                raise AssertionError("Unsupported M function: " + node[1])
            if kind != "binary":
                raise AssertionError("Unsupported M node: " + kind)
            operator, left_node, right_node = node[1:]
            left = run(left_node)
            if operator == "or":
                return left or run(right_node)
            if operator == "and":
                return left and run(right_node)
            right = run(right_node)
            if operator == "=":
                return left == right
            if operator == "<>":
                return left != right
            if left is None or right is None:
                return None
            return {"+": lambda: left + right, "-": lambda: left - right,
                    "*": lambda: left * right, "/": lambda: left / right,
                    "<": lambda: left < right, ">": lambda: left > right,
                    "<=": lambda: left <= right, ">=": lambda: left >= right}[operator]()
        return run(self.tree)


class TestDaxToMCommentBoundary(unittest.TestCase):
    """Diagnostics stay in DAX; comment-like literal data stays convertible."""

    def test_validation_fallback_is_rejected_not_converted_to_null_or_arithmetic(self):
        # The quote in the validator diagnostic previously leaked into M when
        # the block-comment delimiters were interpreted as / and * operators.
        fallback = (
            '/* TODO: DAX conversion validation failed for Broken: '
            'unmatched opening "(" at pos 0 */ BLANK()'
        )
        self.assertIsNone(_dax_to_m_expression(fallback, "Facts"))
        self.assertIsNone(_dax_to_m_expression("/*TODO: invalid conversion*/BLANK()"))
        # A legitimate BLANK remains supported; only its diagnostic wrapper
        # prevents promotion of a failed conversion into a successful M column.
        self.assertEqual(_dax_to_m_expression("BLANK()"), "null")

    def test_actual_block_comments_are_rejected_at_every_expression_position(self):
        for dax in (
            "/* note */ [Value]",
            "[Value] /* note */",
            "[Value] + /* note */ 1",
            "IF([Value] > 0, /* note */ 1, 0)",
            "(/* note */ BLANK())",
            "/* first\nsecond */ 1",
            "/* unterminated diagnostic",
        ):
            with self.subTest(dax=dax):
                self.assertIsNone(_dax_to_m_expression(dax, "Facts"))

    def test_actual_line_comments_are_rejected_not_stripped_or_executed(self):
        for marker in ("//", "--"):
            for dax in (
                f"{marker} note\n[Value]",
                f"[Value] {marker} note",
                f"[Value] + 1 {marker} note\n+ 2",
                f'IF([Value] > 0, "yes", "no") {marker} note',
            ):
                with self.subTest(dax=dax):
                    self.assertIsNone(_dax_to_m_expression(dax, "Facts"))

    def test_comment_markers_in_double_quoted_strings_are_preserved_exactly(self):
        for literal in (
            '"/* TODO: not a diagnostic */ BLANK()"',
            '"// not a line comment"',
            '"-- not a line comment"',
            '"https://example.invalid/a"',
            '"He said ""// not a comment"" -- still text"',
            '"/* first\nsecond */"',
        ):
            with self.subTest(literal=literal):
                self.assertEqual(_dax_to_m_expression(literal, "Facts"), literal)

    def test_comment_markers_in_bracket_identifiers_are_preserved_as_m_fields(self):
        for name in ("Rate/*raw*/", "Rate//raw", "Rate--raw"):
            with self.subTest(name=name):
                self.assertEqual(_dax_to_m_expression(f"[{name}]", "Facts"),
                                 f'[#"{name}"]')
                self.assertEqual(_dax_to_m_expression(f"[{name}] + 1", "Facts"),
                                 f'([#"{name}"] + 1)')

    def test_comment_markers_in_quoted_self_table_identifiers_are_not_comments(self):
        for table in ("Facts/*raw*/", "Facts//raw", "Facts--raw"):
            with self.subTest(table=table):
                self.assertEqual(
                    _dax_to_m_expression(f"'{table}'[Value]", table), "[Value]")
                self.assertEqual(
                    _dax_to_m_expression(f"'{table}'[Value]", "Other"), None)

    def test_literal_or_identifier_marker_does_not_hide_a_later_real_comment(self):
        for dax in (
            '"/* literal */" /* actual comment */',
            '"He said ""// literal""" -- actual comment',
            "[Rate/*raw*/] // actual comment",
            "[Rate//raw] /* actual comment */",
            "'Facts--raw'[Value] -- actual comment",
        ):
            with self.subTest(dax=dax):
                self.assertIsNone(_dax_to_m_expression(dax, "Facts--raw"))

    def test_supported_comment_free_expressions_still_convert_exactly(self):
        cases = (
            ("[A] / [B]", "([A] / [B])"),
            ("[A] * [B] + 2", "(([A] * [B]) + 2)"),
            ("[A] - [B]", "([A] - [B])"),
            ('IF([Value] > 0, "yes", "no")',
             'if [Value] > 0 then "yes" else "no"'),
            ('IF([Value] > 0, "/* literal */", "// literal")',
             'if [Value] > 0 then "/* literal */" else "// literal"'),
            ("BLANK()", "null"),
            ("TRUE()", "true"),
            ("UPPER([Name])", "Text.Upper([Name])"),
        )
        for dax, expected in cases:
            with self.subTest(dax=dax):
                self.assertEqual(_dax_to_m_expression(dax, "Facts"), expected)


class TestQuarterOffsetAndBooleanConversion(unittest.TestCase):
    """Source date offsets must not become M datetime-minus-number errors."""

    def test_source_quarter_filter_compiles_to_day_offset_and_logical_literals(self):
        from powerbi_import.dax_validator import validate_dax_expression

        dax = convert_tableau_formula_to_dax(
            'IF DATEPART("quarter", [Date de début]) <= '
            'DATEPART("quarter", NOW()-45) THEN TRUE ELSE FALSE END',
            column_name="R2", table_name="Facts", is_calc_column=True,
            column_table_map={"Date de début": "Facts"},
            validate_output=True, fallback_on_invalid=True)
        self.assertEqual(
            dax, "IF(QUARTER('Facts'[Date de début]) <= "
            "QUARTER(NOW()-45), TRUE, FALSE)")
        self.assertEqual(validate_dax_expression(dax), [])
        self.assertEqual(
            _dax_to_m_expression(dax, "Facts"),
            'if Date.QuarterOfYear([Date de début]) <= '
            'Date.QuarterOfYear(Date.AddDays(DateTime.LocalNow(), -(45))) '
            'then true else false')

    def test_proven_date_offsets_use_days_including_fractional_day_offsets(self):
        for dax, expected in (
            ("NOW()-45", "Date.AddDays(DateTime.LocalNow(), -(45))"),
            ("NOW()+0.5", "Date.AddDays(DateTime.LocalNow(), 0.5)"),
            ("0.5+NOW()", "Date.AddDays(DateTime.LocalNow(), 0.5)"),
            ("TODAY()-1", "Date.AddDays(Date.From(DateTime.LocalNow()), -(1))"),
            ("DATE(2024, 3, 1)+1", "Date.AddDays(#date(2024, 3, 1), 1)"),
        ):
            with self.subTest(dax=dax):
                self.assertEqual(_dax_to_m_expression(dax, "Facts"), expected)

    def test_unknown_offset_or_date_operands_are_not_guessed_as_numeric_days(self):
        for dax in (
            "NOW()-[Offset]", "NOW()+[Date de début]",
            "[Offset]+NOW()", "45-NOW()", "NOW()*2", "NOW()-TODAY()",
        ):
            with self.subTest(dax=dax):
                self.assertIsNone(_dax_to_m_expression(dax, "Facts"))
        # The bridge has no column datatype metadata: a suggestive name is
        # not proof of a date. Preserve generic numeric-column arithmetic.
        self.assertEqual(_dax_to_m_expression("[Date de début]-45", "Facts"),
                         "([Date de début] - 45)")
        self.assertEqual(_dax_to_m_expression("[Amount]-45", "Facts"),
                         "([Amount] - 45)")

    def test_boolean_literals_are_normalized_without_rewriting_literal_text(self):
        self.assertEqual(_dax_to_m_expression("IF(TRUE,FALSE,TRUE)", "Facts"),
                         "if true then false else true")
        self.assertEqual(_dax_to_m_expression("IF(TRUE(),FALSE(),TRUE())", "Facts"),
                         "if true then false else true")
        for literal in ('"TRUE FALSE TRUE() FALSE()"',
                        '"He said ""TRUE"" and ""FALSE"""'):
            with self.subTest(literal=literal):
                self.assertEqual(_dax_to_m_expression(literal, "Facts"), literal)

    def test_boolean_named_identifiers_keep_their_physical_identity(self):
        for field in ("[TRUE]", "[FALSE]", "[Is TRUE]", "[FALSE Flag]"):
            with self.subTest(field=field):
                self.assertEqual(_dax_to_m_expression(field, "Facts"), field)
        self.assertEqual(
            _dax_to_m_expression('IF([TRUE], "FALSE", [FALSE])', "Facts"),
            'if [TRUE] then "FALSE" else [FALSE]')


class TestDivideDaxToMSemantics(unittest.TestCase):
    def convert(self, dax):
        expression = _dax_to_m_expression(dax, table_name="Facts")
        self.assertIsNotNone(expression, dax)
        return EmittedScalarM(expression)

    def test_oracle_laziness_null_propagation_and_fail_closed_behavior(self):
        self.assertEqual(EmittedScalarM("if true then 7 else 1 / 0").evaluate({}), 7)
        self.assertIsNone(EmittedScalarM("null / 2").evaluate({}))
        self.assertEqual(EmittedScalarM("Number.RoundDown(-1.2)").evaluate({}), -2)
        with self.assertRaises(ZeroDivisionError):
            EmittedScalarM("1 / 0").evaluate({})
        with self.assertRaisesRegex(AssertionError, "Unsupported M"):
            EmittedScalarM("UNKNOWN(1)").evaluate({})
        with self.assertRaisesRegex(AssertionError, "Unsupported M"):
            EmittedScalarM("1 @ 2")

    def test_default_alternate_preserves_blank_zero_and_finite_ratios(self):
        emitted = self.convert("DIVIDE('Facts'[Numerator], 'Facts'[Denominator])")
        for numerator, denominator, expected in (
            (8, 2, 4), (-9, 3, -3), (0, 2, 0),
            (None, 2, None), (8, 0, None), (0, 0, None),
            (None, 0, None), (8, None, None), (None, None, None),
        ):
            with self.subTest(numerator=numerator, denominator=denominator):
                actual = emitted.evaluate({"Numerator": numerator, "Denominator": denominator})
                self.assertEqual(actual, expected)
                if expected is None:
                    self.assertIsNone(actual)
                else:
                    self.assertTrue(math.isfinite(actual))

    def test_explicit_alternate_applies_only_to_zero_or_null_denominator(self):
        for alternate in (0, -7, 42):
            emitted = self.convert(f"DIVIDE([Numerator], [Denominator], {alternate})")
            for numerator, denominator, expected in (
                (9, 3, 3), (0, 3, 0), (None, 3, None),
                (9, 0, alternate), (0, 0, alternate), (None, 0, alternate),
                (9, None, alternate), (None, None, alternate),
            ):
                with self.subTest(alternate=alternate, numerator=numerator, denominator=denominator):
                    actual = emitted.evaluate({"Numerator": numerator, "Denominator": denominator})
                    self.assertEqual(actual, expected)
                    if expected is None:
                        self.assertIsNone(actual, "A null numerator must not use the alternate")

    def test_blank_alternate_and_nested_divide(self):
        emitted = self.convert("DIVIDE([Numerator], [Denominator], BLANK())")
        self.assertIsNone(emitted.evaluate({"Numerator": 1, "Denominator": 0}))
        nested = self.convert("DIVIDE(DIVIDE([Numerator], [Denominator]), 2, 99)")
        self.assertEqual(nested.evaluate({"Numerator": 8, "Denominator": 2}), 2)
        self.assertIsNone(nested.evaluate({"Numerator": 8, "Denominator": 0}))

    def test_if_operands_keep_grouping_and_numerator_is_not_evaluated_on_fallback(self):
        emitted = self.convert(
            "DIVIDE(IF([Flag], [Numerator], 20), IF([Flag], [Denominator], 4), -1)")
        self.assertEqual(emitted.evaluate({"Flag": False}), 5)
        self.assertEqual(emitted.evaluate({"Flag": True, "Numerator": 8, "Denominator": 2}), 4)
        # Missing numerator deliberately raises KeyError if the unused branch
        # is evaluated. An unrelated source error is not a DIVIDE alternate.
        self.assertEqual(emitted.evaluate({"Flag": True, "Denominator": 0}), -1)
        with self.assertRaises(KeyError):
            emitted.evaluate({"Flag": True, "Denominator": 2})
        with self.assertRaises(TypeError):
            self.convert("DIVIDE([Numerator], 2, -1)").evaluate({"Numerator": "bad source"})

    def test_unsupported_arity_and_cross_table_conversion_boundaries(self):
        for dax in ("DIVIDE(1)", "DIVIDE(1, 2, 3, 4)",
                    "DIVIDE(RELATED('Other'[Value]), 2)",
                    "DIVIDE(1, CALCULATE(SUM([Value])))"):
            with self.subTest(dax=dax):
                self.assertIsNone(_dax_to_m_expression(dax, "Facts"))

    def test_quoted_divide_text_is_literal_not_an_unconverted_function(self):
        self.assertEqual(_dax_to_m_expression('"DIVIDE(8, 0)"', "Facts"), '"DIVIDE(8, 0)"')

    def test_tableau_interval_bins_execute_correctly_after_safe_division_conversion(self):
        # Source formula -> DAX safe division -> actual emitted M. Negative
        # inputs distinguish floor bins from truncation or identity fallback.
        for size, peg, cases in (
            (27, 0, [(-1, -27), (0, 0), (26, 0), (27, 27), (55, 54)]),
            (2.5, 0.5, [(-2.1, -4.5), (0, -2), (0.5, 0.5), (3, 3)]),
            (0.25, -1.5, [(-1.6, -1.75), (-1.5, -1.5), (0.1, 0)]),
        ):
            formula = f"FLOOR(([Energy] - ({peg})) / ({size})) * ({size}) + ({peg})"
            dax = convert_tableau_formula_to_dax(formula, table_name="Facts", is_calc_column=True)
            self.assertIn("DIVIDE(", dax)
            emitted = self.convert(dax)
            for energy, expected in cases + [(None, None)]:
                with self.subTest(size=size, peg=peg, energy=energy):
                    actual = emitted.evaluate({"Energy": energy})
                    if expected is None:
                        self.assertIsNone(actual)
                    else:
                        self.assertAlmostEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
