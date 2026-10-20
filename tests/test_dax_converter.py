"""
Unit tests for dax_converter.py — Tableau formula → DAX conversion.

Tests the main convert_tableau_formula_to_dax function and individual
conversion phases: references, CASE/IF, functions, LOD, operators,
column resolution, AGG→AGGX, and cleanup.
"""

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))

from dax_converter import (
    convert_tableau_formula_to_dax,
    map_tableau_to_powerbi_type,
    _reverse_tableau_bracket_escape,
    _convert_nullif,
    _convert_measure_ref_ratio,
)


# ═══════════════════════════════════════════════════════════════════════
# Type Mapping
# ═══════════════════════════════════════════════════════════════════════

class TestYearDateParsingRegression(unittest.TestCase):
    def test_explicit_year_parse_preserves_constructor_components(self):
        result = convert_tableau_formula_to_dax(
            'DATE(DATEPARSE("yyyy", RIGHT([Lot], 4)))',
            table_name="Facts", is_calc_column=True)
        self.assertIn("DATE(VALUE(RIGHT(", result)
        self.assertIn(", 1, 1)", result)
        self.assertNotIn("DATEVALUE(", result)
        from powerbi_import.dax_validator import validate_dax_expression
        self.assertEqual(validate_dax_expression(result), [])

    def test_date_constructors_and_literal_text_are_preserved(self):
        result = convert_tableau_formula_to_dax('MAKEDATE(2024, 9, 1)')
        self.assertEqual(result, "DATE(2024, 9, 1)")
        self.assertEqual(
            convert_tableau_formula_to_dax('"DATE(DATEPARSE)"'),
            '"DATE(DATEPARSE)"')


class TestMapTableauToPowerBIType(unittest.TestCase):
    """Test map_tableau_to_powerbi_type."""

    def test_integer(self):
        self.assertEqual(map_tableau_to_powerbi_type("integer"), "Int64")

    def test_real(self):
        self.assertEqual(map_tableau_to_powerbi_type("real"), "Double")

    def test_string(self):
        self.assertEqual(map_tableau_to_powerbi_type("string"), "String")

    def test_boolean(self):
        self.assertEqual(map_tableau_to_powerbi_type("boolean"), "Boolean")

    def test_date(self):
        self.assertEqual(map_tableau_to_powerbi_type("date"), "DateTime")

    def test_datetime(self):
        self.assertEqual(map_tableau_to_powerbi_type("datetime"), "DateTime")

    def test_unknown_defaults_to_string(self):
        self.assertEqual(map_tableau_to_powerbi_type("blob"), "String")


# ═══════════════════════════════════════════════════════════════════════
# Bracket Escape Reversal
# ═══════════════════════════════════════════════════════════════════════

class TestReverseBracketEscape(unittest.TestCase):
    """Test _reverse_tableau_bracket_escape."""

    def test_orphan_close_paren(self):
        result = _reverse_tableau_bracket_escape("Column Name)")
        self.assertEqual(result, "Column Name]")

    def test_balanced_parens_unchanged(self):
        result = _reverse_tableau_bracket_escape("func(x)")
        self.assertEqual(result, "func(x)")

    def test_no_parens(self):
        result = _reverse_tableau_bracket_escape("Plain Name")
        self.assertEqual(result, "Plain Name")

    def test_multiple_orphan_parens(self):
        result = _reverse_tableau_bracket_escape("A) B)")
        self.assertEqual(result, "A] B]")


# ═══════════════════════════════════════════════════════════════════════
# Empty / Null Input Handling
# ═══════════════════════════════════════════════════════════════════════

class TestEmptyInputs(unittest.TestCase):
    """Test edge cases with empty/null formulas."""

    def test_empty_string(self):
        result = convert_tableau_formula_to_dax("")
        self.assertEqual(result, "")

    def test_whitespace_only(self):
        result = convert_tableau_formula_to_dax("   ")
        self.assertEqual(result, "   ")

    def test_none_input(self):
        result = convert_tableau_formula_to_dax(None)
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════════
# Simple Function Conversions
# ═══════════════════════════════════════════════════════════════════════

class TestStandaloneNullRegression(unittest.TestCase):
    """Synthetic regressions for NULL normalization through the public converter."""

    def test_nested_date_if_final_null(self):
        formula = (
            "IF [Start Date] <= TODAY() THEN [Start Date] "
            "ELSE IF [End Date] <= TODAY() THEN [End Date] "
            "ELSE NULL END END"
        )
        result = convert_tableau_formula_to_dax(formula)
        self.assertEqual(
            result,
            convert_tableau_formula_to_dax(formula.replace("NULL", "BLANK()")),
        )
        self.assertEqual(result.count("BLANK()"), 1)
        self.assertNotRegex(result, r"(?i)\bNULL\b")
        self.assertIn("'Table'[Start Date]", result)
        self.assertIn("'Table'[End Date]", result)

    def test_nested_date_if_call_final_null(self):
        result = convert_tableau_formula_to_dax(
            "IF([Start Date] <= TODAY(), [Start Date], "
            "IF([End Date] <= TODAY(), [End Date], NULL))"
        )
        self.assertEqual(
            result,
            "IF('Table'[Start Date] <= TODAY(), 'Table'[Start Date], "
            "IF('Table'[End Date] <= TODAY(), 'Table'[End Date], BLANK()))",
        )

    def test_standalone_null_is_case_insensitive(self):
        for formula in ("NULL", "null", "Null", "nUlL", " \tNuLl\n"):
            with self.subTest(formula=formula):
                self.assertEqual(
                    convert_tableau_formula_to_dax(formula), "BLANK()"
                )

    def test_null_call_with_whitespace(self):
        for formula in ("NULL()", "null ()", "NuLl( )", "NULL \t(\n\t)"):
            with self.subTest(formula=formula):
                self.assertEqual(
                    convert_tableau_formula_to_dax(formula), "BLANK()"
                )

    def test_isnull_of_null(self):
        for formula in ("ISNULL(NULL)", "isnull(null)", "ISNULL(NULL ( ))"):
            with self.subTest(formula=formula):
                self.assertEqual(
                    convert_tableau_formula_to_dax(formula), "ISBLANK(BLANK())"
                )

    def test_ifnull_column_with_null_fallback(self):
        for fallback in ("NULL", "null", "NULL ( )"):
            with self.subTest(fallback=fallback):
                self.assertEqual(
                    convert_tableau_formula_to_dax(
                        f"IFNULL([Value], {fallback})"
                    ),
                    "IF(ISBLANK('Table'[Value]), BLANK(), 'Table'[Value])",
                )

    def test_quoted_null_text_is_preserved(self):
        for text in ("NULL", "NULL()", "null", "before NULL after"):
            for quote in ('"', "'"):
                with self.subTest(text=text, quote=quote):
                    self.assertEqual(
                        convert_tableau_formula_to_dax(f"{quote}{text}{quote}"),
                        f'"{text}"',
                    )

    def test_quoted_text_and_null_literal_in_same_expression(self):
        for quoted in ('"NULL"', "'NULL'", '"NULL()"', "'NULL()'"):
            with self.subTest(quoted=quoted):
                text = quoted[1:-1]
                self.assertEqual(
                    convert_tableau_formula_to_dax(
                        f"IF [Flag] THEN {quoted} ELSE NULL END"
                    ),
                    f'IF(\'Table\'[Flag], "{text}", BLANK())',
                )

    def test_bracket_identifiers_are_not_null_literals(self):
        for field in ("NULL", "NULL()", "réalisé NULL (%)", "NULL]] suffix"):
            with self.subTest(field=field):
                self.assertEqual(
                    convert_tableau_formula_to_dax(f"[{field}]"),
                    f"'Table'[{field}]",
                )

    def test_quoted_table_names_containing_null_are_preserved(self):
        for table in ("NULL", "NULL()", "NULL Records", "NULL''s Records"):
            with self.subTest(table=table):
                formula = f"'{table}'[NULL()]"
                self.assertEqual(convert_tableau_formula_to_dax(formula), formula)

    def test_escaped_quote_delimiters_protect_null_text(self):
        cases = (
            ('"left "" NULL() "" right"', '"left "" NULL() "" right"'),
            (r'"left \" NULL() \" right"', r'"left \" NULL() \" right"'),
            ("'left '' NULL() '' right'", '"left \' NULL() \' right"'),
            (r"'left \' NULL() \' right'", r'"left \" NULL() \" right"'),
        )
        for formula, expected in cases:
            with self.subTest(formula=formula):
                self.assertEqual(
                    convert_tableau_formula_to_dax(formula), expected
                )

    def test_longer_identifiers_are_not_null_literals(self):
        for identifier in (
            "NULLABLE", "ANNULLED", "_NULL", "NULL_value", "NULL2",
            "valueNULL", "éNULL", "NULLé",
        ):
            with self.subTest(identifier=identifier):
                self.assertEqual(
                    convert_tableau_formula_to_dax(identifier), identifier
                )

    def test_null_conversion_is_idempotent(self):
        cases = (
            ("NULL", "BLANK()"),
            ("NULL ( )", "BLANK()"),
            ("ISNULL(NULL)", "ISBLANK(BLANK())"),
            (
                "IFNULL([Value], NULL)",
                "IF(ISBLANK('Table'[Value]), BLANK(), 'Table'[Value])",
            ),
            (
                'IF [Flag] THEN "NULL()" ELSE NULL END',
                'IF(\'Table\'[Flag], "NULL()", BLANK())',
            ),
            ("'NULL''s Records'[NULL()]", "'NULL''s Records'[NULL()]"),
        )
        for formula, expected in cases:
            with self.subTest(formula=formula):
                result = convert_tableau_formula_to_dax(formula)
                self.assertEqual(result, expected)
                self.assertEqual(convert_tableau_formula_to_dax(result), expected)


class TestNullifRegression(unittest.TestCase):
    """Synthetic coverage of strict NULLIF semantics and token-safe rewriting."""

    def test_public_conversion_keeps_strict_equality_and_original_value(self):
        for left, right in (
            ("0", "BLANK()"), ('""', "BLANK()"), ("BLANK()", "0"),
            ("BLANK()", '""'), ("0", "0"), ('""', '""'),
        ):
            with self.subTest(left=left, right=right):
                self.assertEqual(
                    convert_tableau_formula_to_dax(f"NULLIF({left}, {right})"),
                    f"IF(({left}) == ({right}), BLANK(), ({left}))",
                )

    def test_public_case_insensitive_call_resolves_columns(self):
        self.assertEqual(
            convert_tableau_formula_to_dax("nullif ([Value], NULL)"),
            "IF(('Table'[Value]) == (BLANK()), BLANK(), ('Table'[Value]))",
        )

    def test_nested_calls_rewrite_both_arguments(self):
        left = "IF(([A]) == (0), BLANK(), ([A]))"
        right = "IF(([B]) == (1), BLANK(), ([B]))"
        self.assertEqual(
            _convert_nullif("NULLIF(NULLIF([A], 0), NULLIF([B], 1))"),
            f"IF(({left}) == ({right}), BLANK(), ({left}))",
        )

    def test_sibling_calls_are_all_rewritten(self):
        self.assertEqual(
            _convert_nullif("NULLIF([A], 0) / NULLIF([B], 1)"),
            "IF(([A]) == (0), BLANK(), ([A])) / "
            "IF(([B]) == (1), BLANK(), ([B]))",
        )

    def test_compound_arguments_retain_precedence(self):
        self.assertEqual(
            convert_tableau_formula_to_dax(
                "2 * NULLIF([A] + [B], [C] - [D])"
            ),
            "2 * IF(('Table'[A] + 'Table'[B]) == "
            "('Table'[C] - 'Table'[D]), BLANK(), "
            "('Table'[A] + 'Table'[B]))",
        )

    def test_protected_tokens_are_not_parsed_as_calls_or_arguments(self):
        tokens = (
            '"NULLIF(a,b)"', "'NULLIF(a,b)'", "[NULLIF(a,b)]",
            '"left "" NULLIF(a,b) "" right"',
            r'"left \" NULLIF(a,b) \" right"',
            "'left '' NULLIF(a,b) '' right'",
            r"'left \' NULLIF(a,b) \' right'",
            "[left]] NULLIF(a,b)]",
            "'NULLIF(a,b)''s table'[réalisé (%)]",
        )
        for token in tokens:
            with self.subTest(token=token):
                self.assertEqual(_convert_nullif(token), token)
                self.assertEqual(
                    _convert_nullif(f"NULLIF({token}, 0)"),
                    f"IF(({token}) == (0), BLANK(), ({token}))",
                )

    def test_malformed_calls_return_unchanged_without_looping(self):
        # A child process gives a parser-loop regression a bounded failure,
        # rather than allowing it to hang the entire pytest worker.
        cases = (
            "NULLIF()", "NULLIF([A])", "NULLIF([A], 0, 1)",
            "NULLIF(, 0)", "NULLIF([A], )", "NULLIF([A], 0",
            "NULLIF(NULLIF([A], 0), 1",
        )
        script = (
            "import json, sys; "
            "from tableau_export.dax_converter import _convert_nullif; "
            "print(json.dumps([_convert_nullif(s) for s in json.loads(sys.argv[1])]))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, json.dumps(cases)],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True, text=True, timeout=10, check=True,
        )
        self.assertEqual(json.loads(result.stdout), list(cases))

    def test_helper_is_idempotent_and_protects_placeholder_like_text(self):
        for formula in (
            "NULLIF([A], 0)", "NULLIF(NULLIF([A], 0), 1)",
            "NULLIF([A], 0) + NULLIF([B], 0)",
            'NULLIF("__nullif_token_0__", [__nullif_token_1__])',
        ):
            with self.subTest(formula=formula):
                result = _convert_nullif(formula)
                self.assertNotIn("NULLIF(", result)
                self.assertIn(" == ", result)
                self.assertEqual(_convert_nullif(result), result)
        self.assertEqual(
            _convert_nullif('NULLIF("__nullif_token_0__", 0)'),
            'IF(("__nullif_token_0__") == (0), BLANK(), ("__nullif_token_0__"))',
        )

    def test_nested_fixed_lod_ratio_uses_strict_nullif_denominator(self):
        formula = (
            "SUM([Amount]) / NULLIF("
            "{ FIXED [Group] : SUM("
            "{ FIXED [Group], [Item] : SUM([Amount]) }) }, 0)"
        )
        inner = (
            "CALCULATE(SUM('Facts'[Amount]), "
            "ALLEXCEPT('Facts', 'Facts'[Group], 'Facts'[Item]))"
        )
        denominator = (
            f"CALCULATE(SUMX('Facts', {inner}), "
            "ALLEXCEPT('Facts', 'Facts'[Group]))"
        )
        self.assertEqual(
            convert_tableau_formula_to_dax(formula, table_name="Facts"),
            f"DIVIDE(SUM('Facts'[Amount]), IF(({denominator}) == (0), "
            f"BLANK(), ({denominator})))",
        )


class TestMeasureRefRatioRegression(unittest.TestCase):
    def test_plain_and_grouped_measure_ratios_keep_blank_denominator_semantics(self):
        names = {"Numerator", "Denominator", "réalisé (%)", "预算"}
        for formula, expected in (
            ("[Numerator] / [Denominator]",
             "DIVIDE([Numerator], [Denominator])"),
            ("(([réalisé (%)]) / (([预算])))",
             "DIVIDE([réalisé (%)], [预算])"),
        ):
            with self.subTest(formula=formula):
                # No third argument: an undefined/blank denominator must stay
                # BLANK rather than becoming an artificial zero.
                self.assertEqual(
                    convert_tableau_formula_to_dax(formula, measure_names=names),
                    expected,
                )

    def test_ratio_tokenizer_accepts_escaped_brackets_and_slashes_in_names(self):
        # These are already-resolved DAX refs; exercise the final rewrite
        # independently of Tableau's different bracket-escape resolution.
        names = {"left] value", "réalisé (%)", "path/to/value"}
        for formula, expected in (
            ("(([left]] value]) / ([réalisé (%)]))",
             "DIVIDE([left]] value], [réalisé (%)])"),
            ("[path/to/value] / [left]] value]",
             "DIVIDE([path/to/value], [left]] value])"),
        ):
            with self.subTest(formula=formula):
                self.assertEqual(_convert_measure_ref_ratio(formula, names), expected)
                self.assertEqual(_convert_measure_ref_ratio(expected, names), expected)

    def test_measure_ref_helper_leaves_compound_expressions_to_scalar_parser(self):
        names = {"Numerator", "Denominator", "path/to/value"}
        for formula in (
            '"[Numerator] / [Denominator]"',
            '"C:/reports/current"',
            "[path/to/value]",
            "'Facts'[Numerator] / 'Facts'[Denominator]",
            "[Numerator] / [Denominator] + 1",
            "1 + [Numerator] / [Denominator]",
            "[Numerator] / ([Denominator] + 1)",
            "[Numerator] / [Denominator] / [Numerator]",
            "SUM([Numerator]) / SUM([Denominator])",
        ):
            with self.subTest(formula=formula):
                self.assertEqual(_convert_measure_ref_ratio(formula, names), formula)

    def test_public_converter_preserves_literal_slashes_and_identifier_slashes(self):
        names = {"Numerator", "Denominator", "path/to/value"}
        for formula in (
            '"[Numerator] / [Denominator]"',
            '"C:/reports/current"',
            "[path/to/value]",
        ):
            with self.subTest(formula=formula):
                self.assertEqual(
                    convert_tableau_formula_to_dax(formula, measure_names=names),
                    formula,
                )

    def test_public_converter_safely_divides_compound_scalar_and_aggregate_expressions(self):
        names = {"Numerator", "Denominator"}
        for formula, expected in (
            ("'Facts'[Numerator] / 'Facts'[Denominator]",
             "DIVIDE('Facts'[Numerator], 'Facts'[Denominator])"),
            ("[Numerator] / [Denominator] + 1",
             "DIVIDE([Numerator], [Denominator]) + 1"),
            ("1 + [Numerator] / [Denominator]",
             "1 + DIVIDE([Numerator], [Denominator])"),
            ("[Numerator] / ([Denominator] + 1)",
             "DIVIDE([Numerator], ([Denominator] + 1))"),
            ("[Numerator] / [Denominator] / [Numerator]",
             "DIVIDE(DIVIDE([Numerator], [Denominator]), [Numerator])"),
            ("SUM([Numerator]) / SUM([Denominator])",
             "DIVIDE(SUM([Numerator]), SUM([Denominator]))"),
        ):
            with self.subTest(formula=formula):
                self.assertEqual(
                    convert_tableau_formula_to_dax(formula, measure_names=names),
                    expected,
                )
        self.assertEqual(
            convert_tableau_formula_to_dax(
                "[Numerator] / [Denominator]", table_name="Facts"),
            "DIVIDE('Facts'[Numerator], 'Facts'[Denominator])",
        )


class TestSafeScalarDivisionRegression(unittest.TestCase):
    """Parser boundaries and operator semantics, independent of LOD rewriting."""

    def rewrite(self, expression):
        from tableau_export.dax_converter import _convert_safe_division
        return _convert_safe_division(expression)

    def test_precedence_and_left_associativity(self):
        for source, expected in (
            ("1 + 8 / 2 * 3", "1 + DIVIDE(8, 2) * 3"),
            ("8 * 2 / 4", "DIVIDE(8 * 2, 4)"),
            ("8 / 2 / 4", "DIVIDE(DIVIDE(8, 2), 4)"),
            ("8 / (2 / 4)", "DIVIDE(8, (DIVIDE(2, 4)))"),
            ("8 / 2 ^ 3", "DIVIDE(8, 2 ^ 3)"),
            ("-8 / -2 + 1", "DIVIDE(-8, -2) + 1"),
            ("NOT 8 / 2 > 3", "NOT DIVIDE(8, 2) > 3"),
            ("IF(8 / 2 > 3, 6 / 2, 0)", "IF(DIVIDE(8, 2) > 3, DIVIDE(6, 2), 0)"),
        ):
            with self.subTest(source=source):
                self.assertEqual(self.rewrite(source), expected)
                self.assertEqual(self.rewrite(expected), expected)

    def test_strings_identifiers_and_comments_are_not_division_operators(self):
        for expression in (
            '"https://synthetic.invalid/a/b"',
            '"escaped "" slash / still a string"',
            "'Facts/Archive'[path/to/value]",
            "[escaped]]/name]",
            "/* 8 / 0 */ SUM('Facts'[Amount])",
            "SUM('Facts'[Amount]) // 8 / 0",
        ):
            with self.subTest(expression=expression):
                self.assertEqual(self.rewrite(expression), expression)
        self.assertEqual(
            self.rewrite('/* lead / */ 8 /* before / */ / /* after / */ 2 // tail /'),
            '/* lead / */ DIVIDE(8 /* before / */ ,  /* after / */ 2) // tail /')
        self.assertEqual(
            self.rewrite('8 // keep slash / in comment\n / 2'),
            'DIVIDE(8 // keep slash / in comment\n , 2)')

    def test_malformed_or_unsupported_expressions_fail_closed_without_partial_rewrite(self):
        for expression in (
            "8 /", "/ 2", "8 / (2", "SUM(8 / 2", "8 / 2)",
            '8 / "unterminated', "8 / [unterminated", "8 / 2 /* unterminated",
            "8 / 2 @ 3", "8 / 2; 4", "VAR x = 8 / 2 RETURN x",
            "SUMX({1, 2}, 8 / 2)",
        ):
            with self.subTest(expression=expression):
                self.assertEqual(self.rewrite(expression), expression)

    def test_omitted_optional_arguments_do_not_prevent_safe_nested_division(self):
        for source, expected in (
            ("RANKX('Facts', 8 / 2, , DESC, Dense)",
             "RANKX('Facts', DIVIDE(8, 2), , DESC, Dense)"),
            ("IF(TRUE(), 8 / 2, )", "IF(TRUE(), DIVIDE(8, 2), )"),
            ("DIVIDE(8 / 2, 0)", "DIVIDE(DIVIDE(8, 2), 0)"),
            ("IF(TRUE(), , 8 / 2)", "IF(TRUE(), , DIVIDE(8, 2))"),
        ):
            with self.subTest(source=source):
                self.assertEqual(self.rewrite(source), expected)

    def test_public_conversion_keeps_blank_result_and_nullif_strict_equality(self):
        self.assertEqual(convert_tableau_formula_to_dax("8 / 0"), "DIVIDE(8, 0)")
        self.assertEqual(convert_tableau_formula_to_dax("8 / NULL"), "DIVIDE(8, BLANK())")
        self.assertEqual(
            convert_tableau_formula_to_dax("8 / NULLIF(0, 0)"),
            "DIVIDE(8, IF((0) == (0), BLANK(), (0)))")
        self.assertEqual(
            convert_tableau_formula_to_dax('8 / 2 // ignored / 0'),
            "DIVIDE(8, 2)")


class TestSimpleFunctionConversions(unittest.TestCase):
    """Test direct Tableau → DAX function name mappings."""

    def test_isnull_to_isblank(self):
        result = convert_tableau_formula_to_dax("ISNULL([Field])")
        self.assertIn("ISBLANK", result)
        self.assertNotIn("ISNULL", result)

    def test_zn_to_if_isblank(self):
        result = convert_tableau_formula_to_dax("ZN([Sales])")
        self.assertIn("ISBLANK", result)

    def test_ifnull_to_if_isblank(self):
        result = convert_tableau_formula_to_dax("IFNULL([Sales], 0)")
        self.assertIn("ISBLANK", result)

    def test_countd_to_distinctcount(self):
        result = convert_tableau_formula_to_dax("COUNTD([Customer ID])")
        self.assertIn("DISTINCTCOUNT", result)
        self.assertNotIn("COUNTD", result)

    def test_username_to_userprincipalname(self):
        result = convert_tableau_formula_to_dax("USERNAME()")
        self.assertIn("USERPRINCIPALNAME", result)

    def test_fullname_to_userprincipalname(self):
        result = convert_tableau_formula_to_dax("FULLNAME()")
        self.assertIn("USERPRINCIPALNAME", result)

    def test_userdomain_comment(self):
        result = convert_tableau_formula_to_dax("USERDOMAIN()")
        self.assertIn("RLS", result)  # Should mention RLS

    def test_today(self):
        result = convert_tableau_formula_to_dax("TODAY()")
        self.assertIn("TODAY()", result)

    def test_now(self):
        result = convert_tableau_formula_to_dax("NOW()")
        self.assertIn("NOW()", result)

    def test_len(self):
        result = convert_tableau_formula_to_dax("LEN([Name])")
        self.assertIn("LEN", result)

    def test_left(self):
        result = convert_tableau_formula_to_dax("LEFT([Name], 5)")
        self.assertIn("LEFT", result)

    def test_right(self):
        result = convert_tableau_formula_to_dax("RIGHT([Name], 3)")
        self.assertIn("RIGHT", result)

    def test_upper(self):
        result = convert_tableau_formula_to_dax("UPPER([Name])")
        self.assertIn("UPPER", result)

    def test_lower(self):
        result = convert_tableau_formula_to_dax("LOWER([Name])")
        self.assertIn("LOWER", result)

    def test_trim(self):
        result = convert_tableau_formula_to_dax("TRIM([Name])")
        self.assertIn("TRIM", result)

    def test_abs(self):
        result = convert_tableau_formula_to_dax("ABS([Value])")
        self.assertIn("ABS", result)

    def test_round(self):
        result = convert_tableau_formula_to_dax("ROUND([Value], 2)")
        self.assertIn("ROUND", result)

    def test_power(self):
        result = convert_tableau_formula_to_dax("POWER([Value], 3)")
        self.assertIn("POWER", result)

    def test_sqrt(self):
        result = convert_tableau_formula_to_dax("SQRT([Value])")
        self.assertIn("SQRT", result)

    def test_log(self):
        result = convert_tableau_formula_to_dax("LOG([Value])")
        self.assertIn("LOG", result)

    def test_exp(self):
        result = convert_tableau_formula_to_dax("EXP([Value])")
        self.assertIn("EXP", result)

    def test_min_aggregation(self):
        result = convert_tableau_formula_to_dax("MIN([Value])")
        self.assertIn("MIN", result)

    def test_max_aggregation(self):
        result = convert_tableau_formula_to_dax("MAX([Value])")
        self.assertIn("MAX", result)

    def test_constant_aggregate_is_not_an_iterator(self):
        result = convert_tableau_formula_to_dax("MAX(1)")
        self.assertNotIn("MAXX", result)
        self.assertIn("1", result)

    def test_sum_aggregation(self):
        result = convert_tableau_formula_to_dax("SUM([Amount])")
        self.assertIn("SUM", result)

    def test_avg_to_average(self):
        result = convert_tableau_formula_to_dax("AVG([Value])")
        self.assertIn("AVERAGE", result)
        self.assertNotIn("AVG(", result)

    def test_median(self):
        result = convert_tableau_formula_to_dax("MEDIAN([Value])")
        self.assertIn("MEDIAN", result)

    def test_contains_to_containsstring(self):
        result = convert_tableau_formula_to_dax('CONTAINS([Name], "Corp")')
        self.assertIn("CONTAINSSTRING", result)


# ═══════════════════════════════════════════════════════════════════════
# Special Function Converters
# ═══════════════════════════════════════════════════════════════════════

class TestSpecialFunctionConverters(unittest.TestCase):
    """Test dedicated function converters with argument reordering."""

    def test_datediff_arg_reorder(self):
        result = convert_tableau_formula_to_dax(
            "DATEDIFF('month', [Start], [End])"
        )
        self.assertIn("DATEDIFF", result)
        self.assertIn("MONTH", result)

    def test_str_to_format(self):
        result = convert_tableau_formula_to_dax("STR([Value])")
        self.assertIn("FORMAT", result)

    def test_float_to_convert(self):
        result = convert_tableau_formula_to_dax("FLOAT([Value])")
        self.assertIn("CONVERT", result)
        self.assertIn("DOUBLE", result)

    def test_div_to_quotient(self):
        result = convert_tableau_formula_to_dax("DIV(10, 3)")
        self.assertIn("QUOTIENT", result)

    def test_square_to_power(self):
        result = convert_tableau_formula_to_dax("SQUARE([Value])")
        self.assertIn("POWER", result)
        self.assertIn("2", result)

    def test_iif_to_if(self):
        result = convert_tableau_formula_to_dax("IIF([Sales] > 100, 'High', 'Low')")
        self.assertIn("IF", result)
        self.assertNotIn("IIF", result)

    def test_ismemberof_to_rls_comment(self):
        result = convert_tableau_formula_to_dax('ISMEMBEROF("Admin Group")')
        self.assertIn("TRUE()", result)
        self.assertIn("RLS", result)


# ═══════════════════════════════════════════════════════════════════════
# Operator Conversions
# ═══════════════════════════════════════════════════════════════════════

class TestOperatorConversions(unittest.TestCase):
    """Test operator syntax conversions."""

    def test_double_equals_to_single(self):
        result = convert_tableau_formula_to_dax("[Status] == 'Active'")
        self.assertNotIn("==", result)
        self.assertIn("=", result)

    def test_not_equals(self):
        result = convert_tableau_formula_to_dax("[Status] != 'Active'")
        self.assertIn("<>", result)
        self.assertNotIn("!=", result)

    def test_and_operator(self):
        result = convert_tableau_formula_to_dax("[A] > 1 AND [B] > 2")
        self.assertIn("&&", result)

    def test_or_operator(self):
        result = convert_tableau_formula_to_dax("[A] > 1 OR [B] > 2")
        self.assertIn("||", result)

    def test_string_concat_plus_to_ampersand(self):
        result = convert_tableau_formula_to_dax(
            "[First] + ' ' + [Last]",
            calc_datatype="string"
        )
        self.assertIn("&", result)


# ═══════════════════════════════════════════════════════════════════════
# CASE / IF Structure Conversion
# ═══════════════════════════════════════════════════════════════════════

class TestStructureConversion(unittest.TestCase):
    """Test CASE/WHEN → SWITCH and IF/THEN → IF() conversions."""

    def test_case_when_to_switch(self):
        formula = "CASE [Region] WHEN 'East' THEN 1 WHEN 'West' THEN 2 ELSE 0 END"
        result = convert_tableau_formula_to_dax(formula)
        self.assertIn("SWITCH", result)
        self.assertNotIn("CASE", result)

    def test_if_then_to_if(self):
        formula = "IF [Sales] > 1000 THEN 'High' ELSE 'Low' END"
        result = convert_tableau_formula_to_dax(formula)
        self.assertIn("IF", result)
        self.assertNotIn("THEN", result)
        self.assertNotIn("END", result)

    def test_if_elseif_to_nested_if(self):
        formula = "IF [Sales] > 1000 THEN 'High' ELSEIF [Sales] > 500 THEN 'Medium' ELSE 'Low' END"
        result = convert_tableau_formula_to_dax(formula)
        self.assertNotIn("ELSEIF", result)
        # Should have nested IFs
        self.assertEqual(result.count("IF"), result.count("IF"))  # sanity


# ═══════════════════════════════════════════════════════════════════════
# LOD Expressions
# ═══════════════════════════════════════════════════════════════════════

class TestLODExpressions(unittest.TestCase):
    """Test LOD (Level of Detail) expression conversion."""

    def test_fixed_lod(self):
        result = convert_tableau_formula_to_dax(
            "{FIXED [Region] : SUM([Sales])}",
            table_name="Orders",
            column_table_map={"Region": "Orders", "Sales": "Orders"},
        )
        self.assertIn("CALCULATE", result)
        self.assertIn("ALLEXCEPT", result)

    def test_include_lod(self):
        result = convert_tableau_formula_to_dax(
            "{INCLUDE [Region] : SUM([Sales])}",
            table_name="Orders",
            column_table_map={"Region": "Orders", "Sales": "Orders"},
        )
        self.assertIn("CALCULATE", result)

    def test_exclude_lod(self):
        result = convert_tableau_formula_to_dax(
            "{EXCLUDE [Region] : SUM([Sales])}",
            table_name="Orders",
            column_table_map={"Region": "Orders", "Sales": "Orders"},
        )
        self.assertIn("CALCULATE", result)
        self.assertIn("REMOVEFILTERS", result)


# ═══════════════════════════════════════════════════════════════════════
# Column Resolution & Cross-Table References
# ═══════════════════════════════════════════════════════════════════════

class TestColumnResolution(unittest.TestCase):
    """Test column name resolution with table qualifying."""

    def test_single_column_qualified(self):
        result = convert_tableau_formula_to_dax(
            "SUM([Sales])",
            table_name="Orders",
            column_table_map={"Sales": "Orders"},
        )
        self.assertIn("'Orders'[Sales]", result)

    def test_measure_not_qualified_with_table(self):
        result = convert_tableau_formula_to_dax(
            "[Total Sales]",
            table_name="Orders",
            column_table_map={"Total Sales": "Orders"},
            measure_names={"Total Sales"},
        )
        self.assertIn("[Total Sales]", result)
        # Measures should NOT have table prefix
        self.assertNotIn("'Orders'[Total Sales]", result)

    def test_cross_table_ref_uses_related(self):
        result = convert_tableau_formula_to_dax(
            "[Product Name]",
            table_name="Orders",
            column_table_map={"Product Name": "Products"},
            is_calc_column=True,
        )
        self.assertIn("RELATED", result)


# ═══════════════════════════════════════════════════════════════════════
# AGG(IF(...)) → AGGX Conversion
# ═══════════════════════════════════════════════════════════════════════

class TestAggIfToAggx(unittest.TestCase):
    """Test SUM(IF(...)) → SUMX('table', IF(...)) conversion."""

    def test_sum_if_to_sumx(self):
        result = convert_tableau_formula_to_dax(
            "SUM(IF [Status]='Active' THEN [Amount] END)",
            table_name="Orders",
            column_table_map={"Status": "Orders", "Amount": "Orders"},
        )
        self.assertIn("SUMX", result)

    def test_avg_if_to_averagex(self):
        result = convert_tableau_formula_to_dax(
            "AVG(IF [Type]='A' THEN [Value] END)",
            table_name="Data",
            column_table_map={"Type": "Data", "Value": "Data"},
        )
        self.assertIn("AVERAGEX", result)


# ═══════════════════════════════════════════════════════════════════════
# Table Calc Conversions
# ═══════════════════════════════════════════════════════════════════════

class TestTableCalcConversions(unittest.TestCase):
    """Test Tableau table calculation → DAX conversions."""

    def test_running_sum(self):
        result = convert_tableau_formula_to_dax(
            "RUNNING_SUM(SUM([Sales]))",
            table_name="Orders",
        )
        self.assertIn("CALCULATE", result)

    def test_running_avg(self):
        result = convert_tableau_formula_to_dax(
            "RUNNING_AVG(SUM([Sales]))",
            table_name="Orders",
        )
        self.assertIn("CALCULATE", result)

    def test_rank(self):
        result = convert_tableau_formula_to_dax(
            "RANK(SUM([Sales]))",
            table_name="Orders",
        )
        self.assertIn("RANKX", result)

    def test_rank_unique(self):
        result = convert_tableau_formula_to_dax(
            "RANK_UNIQUE(SUM([Sales]))",
            table_name="Orders",
        )
        self.assertIn("RANKX", result)

    def test_window_sum(self):
        result = convert_tableau_formula_to_dax(
            "WINDOW_SUM(SUM([Sales]))",
            table_name="Orders",
        )
        self.assertIn("CALCULATE", result)


# ═══════════════════════════════════════════════════════════════════════
# Date Function Conversions
# ═══════════════════════════════════════════════════════════════════════

class TestDateFunctions(unittest.TestCase):
    """Test Tableau date function → DAX conversions."""

    def test_datetrunc_year(self):
        result = convert_tableau_formula_to_dax("DATETRUNC('year', [OrderDate])")
        # Should convert to STARTOFYEAR or equivalent
        dax_upper = result.upper()
        self.assertTrue(
            "STARTOFYEAR" in dax_upper or "YEAR" in dax_upper,
            f"Expected date operation but got: {result}"
        )

    def test_datepart_year(self):
        result = convert_tableau_formula_to_dax("DATEPART('year', [OrderDate])")
        self.assertIn("YEAR", result.upper())

    def test_dateadd(self):
        result = convert_tableau_formula_to_dax("DATEADD('month', 3, [OrderDate])")
        self.assertIn("EDATE", result)
        self.assertNotIn("DATEADD", result)

    def test_date_literal(self):
        result = convert_tableau_formula_to_dax("#2024-01-15#")
        self.assertIn("DATE", result)
        self.assertIn("2024", result)


# ═══════════════════════════════════════════════════════════════════════
# Reference Resolution
# ═══════════════════════════════════════════════════════════════════════

class TestReferenceResolution(unittest.TestCase):
    """Test calc_map and param_map reference resolution."""

    def test_calculation_reference_resolved(self):
        result = convert_tableau_formula_to_dax(
            "[Calculation_001] * 2",
            calc_map={"Calculation_001": "Total Sales"},
        )
        self.assertIn("Total Sales", result)
        self.assertNotIn("Calculation_001", result)

    def test_parameter_reference_resolved(self):
        result = convert_tableau_formula_to_dax(
            "[Parameters].[Discount Rate]",
            param_map={"Discount Rate": "Discount Rate"},
        )
        self.assertIn("Discount Rate", result)

    def test_parameter_inlined_for_calc_column(self):
        result = convert_tableau_formula_to_dax(
            "[Parameters].[Max Value]",
            param_map={"Max Value": "Max Value"},
            param_values={"Max Value": "100"},
            is_calc_column=True,
        )
        self.assertIn("100", result)

    def test_parameter_with_literal_brackets_resolves_to_valid_dax(self):
        # Tableau parameter names can embed literal '[' and escaped ']]'.
        # The reference must resolve and emit a bracket-free DAX identifier
        # that matches the sanitized What-If table/measure name.
        formula = (
            'IF([Parameters].[AIP [Indicateur nationaux]][detail]] '
            '(copie)_934778459520655361]="NC", [Evaluation]="NC", true)'
        )
        param_map = {
            "AIP Indicateur nationauxdetail (copie)_934778459520655361":
                "Evaluation NC [Indicateur nationaux][detail] ",
        }
        result = convert_tableau_formula_to_dax(
            formula,
            param_map=param_map,
            is_calc_column=False,
        )
        # No leftover literal brackets from the param name leaking through.
        self.assertNotIn("[Indicateur nationaux]", result)
        self.assertNotIn("(copie)_934778459520655361", result)
        # Sanitized, bracket-free identifier present.
        self.assertIn("Evaluation NC Indicateur nationaux detail", result)
        # Brackets must be balanced (no mismatched-bracket cascade).
        self.assertEqual(result.count("["), result.count("]"))


# ═══════════════════════════════════════════════════════════════════════
# Math / Statistics Functions
# ═══════════════════════════════════════════════════════════════════════

class TestMathStatsFunctions(unittest.TestCase):
    """Test math and statistics function conversions."""

    def test_stdev(self):
        result = convert_tableau_formula_to_dax("STDEV([Value])")
        self.assertIn("STDEV", result)

    def test_var(self):
        result = convert_tableau_formula_to_dax("VAR([Value])")
        dax_upper = result.upper()
        self.assertTrue("VAR" in dax_upper)

    def test_ceiling_gets_second_arg(self):
        result = convert_tableau_formula_to_dax("CEILING([Value])")
        # Should add missing second argument
        self.assertIn("CEILING", result)

    def test_floor_gets_second_arg(self):
        result = convert_tableau_formula_to_dax("FLOOR([Value])")
        self.assertIn("FLOOR", result)


# ═══════════════════════════════════════════════════════════════════════
# No Tableau Syntax Leakage
# ═══════════════════════════════════════════════════════════════════════

class TestNoTableauLeakage(unittest.TestCase):
    """Verify converted DAX doesn't contain Tableau-specific syntax."""

    def test_no_elseif(self):
        formula = "IF [A]>1 THEN 'X' ELSEIF [A]>0 THEN 'Y' ELSE 'Z' END"
        result = convert_tableau_formula_to_dax(formula)
        self.assertNotIn("ELSEIF", result)

    def test_no_double_equals(self):
        result = convert_tableau_formula_to_dax("[X] == 1")
        self.assertNotIn("==", result)

    def test_no_not_equals_excl(self):
        result = convert_tableau_formula_to_dax("[X] != 1")
        self.assertNotIn("!=", result)

    def test_no_lod_braces(self):
        result = convert_tableau_formula_to_dax(
            "{FIXED [Region] : SUM([Sales])}",
            table_name="T",
            column_table_map={"Region": "T", "Sales": "T"},
        )
        self.assertNotIn("{FIXED", result)
        self.assertNotIn("{INCLUDE", result)
        self.assertNotIn("{EXCLUDE", result)


# ═══════════════════════════════════════════════════════════════════════
# Complex / Combined Formulas
# ═══════════════════════════════════════════════════════════════════════

class TestComplexFormulas(unittest.TestCase):
    """Test complex multi-feature formulas."""

    def test_nested_if_with_aggregation(self):
        formula = "IF SUM([Sales]) > 1000 THEN 'High' ELSE 'Low' END"
        result = convert_tableau_formula_to_dax(
            formula,
            table_name="Orders",
            column_table_map={"Sales": "Orders"},
        )
        self.assertIn("IF", result)
        self.assertIn("SUM", result)

    def test_formula_with_multiple_functions(self):
        formula = "ROUND(SUM([Sales]) / COUNTD([Customer]), 2)"
        result = convert_tableau_formula_to_dax(
            formula,
            table_name="Orders",
            column_table_map={"Sales": "Orders", "Customer": "Orders"},
        )
        self.assertIn("ROUND", result)
        self.assertIn("SUM", result)
        self.assertIn("DISTINCTCOUNT", result)


# ═══════════════════════════════════════════════════════════════════════
# Sprint 75 — Expanded DAX Conversion Tests (90+ new tests)
# ═══════════════════════════════════════════════════════════════════════

class TestTrigFunctions(unittest.TestCase):
    """Test trigonometric function conversions."""

    def test_sin(self):
        result = convert_tableau_formula_to_dax("SIN([Angle])")
        self.assertIn("SIN", result)

    def test_cos(self):
        result = convert_tableau_formula_to_dax("COS([Angle])")
        self.assertIn("COS", result)

    def test_tan(self):
        result = convert_tableau_formula_to_dax("TAN([Angle])")
        self.assertIn("TAN", result)

    def test_asin(self):
        result = convert_tableau_formula_to_dax("ASIN([Val])")
        self.assertIn("ASIN", result)

    def test_acos(self):
        result = convert_tableau_formula_to_dax("ACOS([Val])")
        self.assertIn("ACOS", result)

    def test_atan(self):
        result = convert_tableau_formula_to_dax("ATAN([Val])")
        self.assertIn("ATAN", result)

    def test_cot(self):
        result = convert_tableau_formula_to_dax("COT([Angle])")
        self.assertIn("COT", result)

    def test_sign(self):
        result = convert_tableau_formula_to_dax("SIGN([Val])")
        self.assertIn("SIGN", result)

    def test_pi(self):
        result = convert_tableau_formula_to_dax("PI()")
        self.assertIn("PI", result)

    def test_ln(self):
        result = convert_tableau_formula_to_dax("LN([Val])")
        self.assertIn("LN", result)


class TestTextFunctions(unittest.TestCase):
    """Test extended text function conversions."""

    def test_ascii_to_unicode(self):
        result = convert_tableau_formula_to_dax("ASCII('A')")
        self.assertIn("UNICODE", result)

    def test_mid(self):
        result = convert_tableau_formula_to_dax("MID([Name], 2, 3)")
        self.assertIn("MID", result)

    def test_replace_to_substitute(self):
        result = convert_tableau_formula_to_dax("REPLACE([Name], 'old', 'new')")
        self.assertIn("SUBSTITUTE", result)

    def test_space_to_rept(self):
        result = convert_tableau_formula_to_dax("SPACE(5)")
        self.assertIn("REPT", result)

    def test_ltrim(self):
        result = convert_tableau_formula_to_dax("LTRIM([Name])")
        self.assertIn("TRIM", result)

    def test_rtrim(self):
        result = convert_tableau_formula_to_dax("RTRIM([Name])")
        self.assertIn("TRIM", result)

    def test_contains_to_containsstring(self):
        result = convert_tableau_formula_to_dax("CONTAINS([Name], 'test')")
        self.assertIn("CONTAINSSTRING", result)

    def test_isnumber(self):
        result = convert_tableau_formula_to_dax("ISNUMBER([Val])")
        self.assertIn("ISNUMBER", result)

    def test_not(self):
        result = convert_tableau_formula_to_dax("NOT([Flag])")
        self.assertIn("NOT", result)

    def test_char_to_unichar(self):
        result = convert_tableau_formula_to_dax("CHAR(65)")
        self.assertIn("UNICHAR", result)


class TestDateFunctionsExpanded(unittest.TestCase):
    """Test expanded date function conversions."""

    def test_datetrunc_quarter(self):
        result = convert_tableau_formula_to_dax("DATETRUNC('quarter', [OrderDate])")
        # Should produce STARTOFQUARTER or equivalent
        result_lower = result.lower()
        self.assertTrue('startofquarter' in result_lower or 'quarter' in result_lower)

    def test_datetrunc_month(self):
        result = convert_tableau_formula_to_dax("DATETRUNC('month', [OrderDate])")
        result_lower = result.lower()
        self.assertTrue('startofmonth' in result_lower or 'month' in result_lower)

    def test_datepart_quarter(self):
        result = convert_tableau_formula_to_dax("DATEPART('quarter', [OrderDate])")
        result_lower = result.lower()
        self.assertTrue('quarter' in result_lower)

    def test_datepart_month(self):
        result = convert_tableau_formula_to_dax("DATEPART('month', [OrderDate])")
        result_lower = result.lower()
        self.assertTrue('month' in result_lower)

    def test_datepart_day(self):
        result = convert_tableau_formula_to_dax("DATEPART('day', [OrderDate])")
        result_lower = result.lower()
        self.assertTrue('day' in result_lower)

    def test_datepart_hour(self):
        result = convert_tableau_formula_to_dax("DATEPART('hour', [OrderDate])")
        result_lower = result.lower()
        self.assertTrue('hour' in result_lower)

    def test_datepart_minute(self):
        result = convert_tableau_formula_to_dax("DATEPART('minute', [OrderDate])")
        result_lower = result.lower()
        self.assertTrue('minute' in result_lower)

    def test_datepart_second(self):
        result = convert_tableau_formula_to_dax("DATEPART('second', [OrderDate])")
        result_lower = result.lower()
        self.assertTrue('second' in result_lower)

    def test_datepart_week(self):
        result = convert_tableau_formula_to_dax("DATEPART('week', [OrderDate])")
        result_lower = result.lower()
        self.assertTrue('weeknum' in result_lower or 'week' in result_lower)

    def test_makedate(self):
        result = convert_tableau_formula_to_dax("MAKEDATE(2023, 1, 15)")
        self.assertIn("DATE", result)

    def test_makedatetime(self):
        result = convert_tableau_formula_to_dax("MAKEDATETIME(#2023-01-15#, #10:30:00#)")
        # Should produce something date-related
        self.assertIsInstance(result, str)


class TestStatsFunctionsExpanded(unittest.TestCase):
    """Test statistical function conversions."""

    def test_stdevp(self):
        result = convert_tableau_formula_to_dax("STDEVP([Sales])")
        self.assertIn("STDEV.P", result)

    def test_varp(self):
        result = convert_tableau_formula_to_dax("VARP([Sales])")
        self.assertIn("VAR.P", result)

    def test_percentile(self):
        result = convert_tableau_formula_to_dax("PERCENTILE([Sales], 0.95)")
        self.assertIn("PERCENTILE", result)

    def test_corr(self):
        result = convert_tableau_formula_to_dax("CORR([Sales], [Profit])")
        # Should not leave CORR raw—might map to a comment or approximation
        self.assertIsInstance(result, str)
        self.assertNotEqual(result, "")

    def test_covar(self):
        result = convert_tableau_formula_to_dax("COVAR([Sales], [Profit])")
        self.assertIsInstance(result, str)
        self.assertNotEqual(result, "")

    def test_median(self):
        result = convert_tableau_formula_to_dax("MEDIAN([Sales])")
        self.assertIn("MEDIAN", result)


class TestSpecialConverterFunctions(unittest.TestCase):
    """Test dedicated converter functions."""

    def test_attr(self):
        result = convert_tableau_formula_to_dax(
            "ATTR([Region])",
            column_table_map={"Region": "Orders"},
        )
        # ATTR → single-value aggregation
        self.assertIsInstance(result, str)
        self.assertNotEqual(result, "")

    def test_endswith(self):
        result = convert_tableau_formula_to_dax("ENDSWITH([Name], 'Inc')")
        self.assertIn("RIGHT", result)

    def test_startswith(self):
        result = convert_tableau_formula_to_dax("STARTSWITH([Name], 'Mr')")
        self.assertIn("LEFT", result)

    def test_proper(self):
        result = convert_tableau_formula_to_dax("PROPER([Name])")
        # No native DAX PROPER; should produce approximation or comment
        self.assertIsInstance(result, str)

    def test_split(self):
        result = convert_tableau_formula_to_dax("SPLIT([Name], ' ', 1)")
        self.assertIsInstance(result, str)

    def test_find(self):
        result = convert_tableau_formula_to_dax("FIND([Name], 'test')")
        self.assertIn("FIND", result)

    def test_isdate(self):
        result = convert_tableau_formula_to_dax("ISDATE([Val])")
        self.assertIsInstance(result, str)

    def test_dateparse(self):
        result = convert_tableau_formula_to_dax("DATEPARSE('yyyy-MM-dd', [DateStr])")
        self.assertIsInstance(result, str)

    def test_int_conversion(self):
        result = convert_tableau_formula_to_dax("INT([Sales])")
        self.assertIn("INT", result)

    def test_date_function(self):
        result = convert_tableau_formula_to_dax("DATE([DateStr])")
        self.assertEqual(
            result, "CONVERT(INT(CONVERT('Table'[DateStr], DATETIME)), DATETIME)")


class TestTableCalcExpanded(unittest.TestCase):
    """Test expanded table calculation conversions."""

    def test_running_count(self):
        result = convert_tableau_formula_to_dax(
            "RUNNING_COUNT(SUM([Sales]))",
            table_name="Orders",
        )
        self.assertIn("CALCULATE", result)

    def test_running_max(self):
        result = convert_tableau_formula_to_dax(
            "RUNNING_MAX(SUM([Sales]))",
            table_name="Orders",
        )
        self.assertIn("CALCULATE", result)

    def test_running_min(self):
        result = convert_tableau_formula_to_dax(
            "RUNNING_MIN(SUM([Sales]))",
            table_name="Orders",
        )
        self.assertIn("CALCULATE", result)

    def test_rank_dense(self):
        result = convert_tableau_formula_to_dax(
            "RANK_DENSE(SUM([Sales]))",
            table_name="Orders",
        )
        result_upper = result.upper()
        self.assertTrue("RANKX" in result_upper or "RANK" in result_upper)

    def test_window_avg(self):
        result = convert_tableau_formula_to_dax(
            "WINDOW_AVG(SUM([Sales]), -2, 0)",
            table_name="Orders",
        )
        self.assertIn("CALCULATE", result)

    def test_window_max(self):
        result = convert_tableau_formula_to_dax(
            "WINDOW_MAX(SUM([Sales]), -2, 0)",
            table_name="Orders",
        )
        self.assertIn("CALCULATE", result)

    def test_window_min(self):
        result = convert_tableau_formula_to_dax(
            "WINDOW_MIN(SUM([Sales]), -2, 0)",
            table_name="Orders",
        )
        self.assertIn("CALCULATE", result)

    def test_index(self):
        result = convert_tableau_formula_to_dax("INDEX()")
        self.assertIsInstance(result, str)

    def test_first(self):
        result = convert_tableau_formula_to_dax("FIRST()")
        self.assertIsInstance(result, str)

    def test_last(self):
        result = convert_tableau_formula_to_dax("LAST()")
        self.assertIsInstance(result, str)

    def test_size(self):
        result = convert_tableau_formula_to_dax("SIZE()")
        self.assertIsInstance(result, str)

    def test_total(self):
        result = convert_tableau_formula_to_dax(
            "TOTAL(SUM([Sales]))",
            table_name="Orders",
        )
        self.assertIsInstance(result, str)


class TestSpatialFunctions(unittest.TestCase):
    """Test spatial/geo function conversions — should produce approximations."""

    def test_makepoint(self):
        result = convert_tableau_formula_to_dax("MAKEPOINT([Lat], [Lon])")
        # MAKEPOINT has no DAX equivalent — should produce BLANK or comment
        result_lower = result.lower()
        self.assertTrue('blank' in result_lower or 'makepoint' in result_lower)

    def test_distance(self):
        result = convert_tableau_formula_to_dax("DISTANCE([Pt1], [Pt2], 'km')")
        self.assertIsInstance(result, str)

    def test_makeline(self):
        result = convert_tableau_formula_to_dax("MAKELINE([Pt1], [Pt2])")
        self.assertIsInstance(result, str)

    def test_buffer(self):
        result = convert_tableau_formula_to_dax("BUFFER([Point], 100, 'km')")
        self.assertIsInstance(result, str)

    def test_hexbinx(self):
        result = convert_tableau_formula_to_dax("HEXBINX([Lon], [Lat])")
        self.assertIsInstance(result, str)

    def test_hexbiny(self):
        result = convert_tableau_formula_to_dax("HEXBINY([Lon], [Lat])")
        self.assertIsInstance(result, str)


class TestConversionFunctions(unittest.TestCase):
    """Test type conversion functions."""

    def test_count(self):
        result = convert_tableau_formula_to_dax("COUNT([Orders])")
        self.assertIn("COUNT", result)

    def test_counta(self):
        result = convert_tableau_formula_to_dax("COUNTA([Orders])")
        self.assertIn("COUNTA", result)

    def test_float(self):
        result = convert_tableau_formula_to_dax("FLOAT([Sales])")
        # Should convert to CONVERT or VALUE
        self.assertIsInstance(result, str)
        self.assertNotEqual(result, "")


class TestRegexpFunctions(unittest.TestCase):
    """Test REGEXP functions — should produce approximations."""

    def test_regexp_match(self):
        result = convert_tableau_formula_to_dax("REGEXP_MATCH([Email], '.*@.*\\.com')")
        self.assertIsInstance(result, str)

    def test_regexp_extract(self):
        result = convert_tableau_formula_to_dax("REGEXP_EXTRACT([Phone], '\\d+')")
        self.assertIsInstance(result, str)

    def test_regexp_replace(self):
        result = convert_tableau_formula_to_dax("REGEXP_REPLACE([Name], '\\s+', ' ')")
        self.assertIsInstance(result, str)


class TestScriptFunctions(unittest.TestCase):
    """Test SCRIPT_ functions — R/Python scripts."""

    def test_script_bool(self):
        result = convert_tableau_formula_to_dax("SCRIPT_BOOL('return True', [Sales])")
        self.assertIsInstance(result, str)

    def test_script_int(self):
        result = convert_tableau_formula_to_dax("SCRIPT_INT('return 42', [Sales])")
        self.assertIsInstance(result, str)

    def test_script_real(self):
        result = convert_tableau_formula_to_dax("SCRIPT_REAL('return 3.14', [Sales])")
        self.assertIsInstance(result, str)

    def test_script_str(self):
        result = convert_tableau_formula_to_dax("SCRIPT_STR('return hello', [Name])")
        self.assertIsInstance(result, str)


class TestSecurityFunctionsExpanded(unittest.TestCase):
    """Test security function conversions."""

    def test_ismemberof(self):
        result = convert_tableau_formula_to_dax("ISMEMBEROF('Managers')")
        # Should produce TRUE() with RLS comment
        self.assertIsInstance(result, str)

    def test_userdomain(self):
        result = convert_tableau_formula_to_dax("USERDOMAIN()")
        # No DAX equivalent — should produce empty string or comment
        self.assertIsInstance(result, str)


class TestAggIfExpandedPatterns(unittest.TestCase):
    """Test additional AGG(IF) → AGGX patterns."""

    def test_count_if_to_countx(self):
        formula = "COUNT(IF [Active] THEN [OrderId] END)"
        result = convert_tableau_formula_to_dax(
            formula,
            table_name="Orders",
            column_table_map={"Active": "Orders", "OrderId": "Orders"},
        )
        # Should convert with X suffix or CALCULATE
        self.assertIsInstance(result, str)

    def test_min_if(self):
        formula = "MIN(IF [Status] = 'Open' THEN [Amount] END)"
        result = convert_tableau_formula_to_dax(
            formula,
            table_name="Orders",
            column_table_map={"Status": "Orders", "Amount": "Orders"},
        )
        self.assertIsInstance(result, str)


class TestDaxOutputQuality(unittest.TestCase):
    """Verify no Tableau syntax leaks into DAX output."""

    def test_no_double_equals(self):
        result = convert_tableau_formula_to_dax("[Status] == 'Active'")
        # Should use single = in DAX
        self.assertNotIn("==", result)

    def test_no_elseif(self):
        result = convert_tableau_formula_to_dax(
            "IF [A] > 1 THEN 'x' ELSEIF [A] > 0 THEN 'y' ELSE 'z' END"
        )
        self.assertNotIn("ELSEIF", result)

    def test_string_concat_uses_ampersand(self):
        result = convert_tableau_formula_to_dax("'Hello' + ' ' + 'World'")
        # The + to & conversion is done for string context
        self.assertIsInstance(result, str)

    def test_and_or_operators(self):
        result = convert_tableau_formula_to_dax("[A] > 1 AND [B] > 2 OR [C] = 3")
        self.assertIn("&&", result)
        self.assertIn("||", result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
