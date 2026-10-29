"""Phase 3 foundation tests for DAX conversion guards."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from powerbi_import.dax_validator import mask_literal_spans, validate_dax_expression
from powerbi_import.validator import ArtifactValidator
from tableau_export.dax_converter import convert_tableau_formula_to_dax


class TestDaxValidatorPhase3(unittest.TestCase):
    def test_valid_dax_has_no_issues(self):
        issues = validate_dax_expression("SUM('Sales'[Amount])")
        self.assertEqual(issues, [])

    def test_unbalanced_brackets_detected(self):
        issues = validate_dax_expression("SUM('Sales'[Amount)")
        self.assertTrue(any('unmatched' in i or 'mismatched' in i for i in issues))

    def test_unterminated_quotes_detected(self):
        issues = validate_dax_expression("'Sales[Amount]")
        self.assertTrue(any('unterminated' in i for i in issues))

    def test_tableau_function_leak_detected(self):
        issues = validate_dax_expression("DATETRUNC('year', [Order Date])")
        self.assertTrue(any('Tableau function' in i for i in issues))

    def test_empty_expression(self):
        issues = validate_dax_expression('   ')
        self.assertIn('empty DAX expression', issues)

    def test_none_expression(self):
        issues = validate_dax_expression(None)
        self.assertIn('empty DAX expression', issues)

    def test_valid_complex_dax(self):
        expr = "CALCULATE(SUM('Sales'[Amount]), ALLEXCEPT('Sales', 'Sales'[Region]))"
        issues = validate_dax_expression(expr)
        self.assertEqual(issues, [])

    def test_escaped_quotes_are_valid(self):
        expr = """CALCULATE(SUM('Sales'[Amount]), 'Sales'[Name] = "O""Brien")"""
        issues = validate_dax_expression(expr)
        self.assertEqual(issues, [])

    def test_escaped_single_quotes_are_valid(self):
        expr = "SUM('O''Brien Sales'[Amount])"
        issues = validate_dax_expression(expr)
        self.assertEqual(issues, [])

    def test_invalid_literal_none(self):
        issues = validate_dax_expression("[Col] + None")
        self.assertTrue(any('invalid literal' in i for i in issues))

    def test_unterminated_block_comment(self):
        issues = validate_dax_expression("/* this is incomplete SUM([A])")
        self.assertTrue(any('unterminated block comment' in i for i in issues))

    def test_terminated_block_comment_is_ok(self):
        issues = validate_dax_expression("/* comment */ SUM([A])")
        self.assertEqual(issues, [])

    def test_apostrophe_inside_bracketed_identifier_is_valid(self):
        # Column/measure names may contain apostrophes; they are NOT single-quote
        # delimiters. Regression for real-world [% ou Nombre d 'appel] false positives.
        expr = 'IF([% ou Nombre d \'appel] = "% total", "yes", "no")'
        issues = validate_dax_expression(expr)
        self.assertEqual(issues, [])

    def test_double_quote_inside_bracketed_identifier_is_valid(self):
        expr = 'IF([Sales "USD"] > 0, "high", "low")'
        issues = validate_dax_expression(expr)
        self.assertEqual(issues, [])

    def test_apostrophe_in_bracket_with_string_literal_apostrophe(self):
        # Both the identifier and the string literal contain apostrophes.
        expr = '[% ou Nombre d \'appel] = "Nbr d\'appel"'
        issues = validate_dax_expression(expr)
        self.assertEqual(issues, [])

    def test_parens_inside_bracketed_identifier_are_literal(self):
        # A measure name with parentheses must not be counted as grouping.
        expr = 'IF([Show % (Indicateur Nat)] = "x", BLANK(), 1)'
        issues = validate_dax_expression(expr)
        self.assertEqual(issues, [])

    def test_brackets_inside_string_literal_not_counted(self):
        expr = 'CONCATENATE("prefix [", [Col])'
        issues = validate_dax_expression(expr)
        self.assertEqual(issues, [])

    def test_multiple_tableau_leak_tokens(self):
        for tok in ['DATETRUNC', 'DATEPART', 'IFNULL', 'ISNULL', 'COUNTD',
                     'WINDOW_SUM', 'RUNNING_SUM', 'RANK_UNIQUE', 'PREVIOUS_VALUE']:
            issues = validate_dax_expression(f"{tok}([X])")
            self.assertTrue(
                any('Tableau function' in i for i in issues),
                f"Expected Tableau leak detection for {tok}",
            )


class TestStrictEqualityValidatorRegression(unittest.TestCase):
    """NULLIF's strict equality is valid DAX, not a repairable Tableau leak."""

    def test_nullif_strict_equality_is_accepted_by_both_validators(self):
        expression = "IF((0) == (BLANK()), BLANK(), (0))"
        self.assertEqual(validate_dax_expression(expression), [])
        self.assertEqual(ArtifactValidator.validate_dax_formula(expression), [])

    def test_auto_repair_preserves_strict_equality_with_zero_repairs(self):
        for expression in (
            "IF((0) == (BLANK()), BLANK(), (0))",
            'IF(("") == (BLANK()), BLANK(), (""))',
        ):
            with self.subTest(expression=expression):
                fixed, repairs = ArtifactValidator.auto_fix_dax_leaks(expression)
                self.assertEqual(fixed, expression)
                self.assertEqual(repairs, [])
                self.assertEqual(
                    ArtifactValidator.auto_fix_dax_leaks(fixed), (expression, [])
                )

    def test_strict_equality_does_not_hide_genuine_countd_leak(self):
        expression = (
            "IF((0) == (BLANK()), BLANK(), COUNTD('Synthetic'[Id]))"
        )
        issues = ArtifactValidator.validate_dax_formula(expression)
        self.assertTrue(any(
            "Tableau function leak" in issue and "COUNTD" in issue
            for issue in issues
        ), issues)


class TestDatediffShape(unittest.TestCase):
    """DAX DATEDIFF(<Date1>, <Date2>, <Interval>) takes a bare keyword unit.

    Tableau's DATEDIFF('unit', a, b, 'start_of_week') has no DAX equivalent, and
    the converter leaves a call it cannot rewrite untouched. The validator must
    flag it so the existing TODO fallback applies instead of emitting DAX that
    Desktop rejects.
    """

    def _datediff(self, expr):
        return [i for i in validate_dax_expression(expr) if 'DATEDIFF' in i]

    def test_every_documented_interval_is_accepted(self):
        for unit in ('SECOND', 'MINUTE', 'HOUR', 'DAY', 'WEEK', 'MONTH', 'QUARTER', 'YEAR'):
            with self.subTest(unit=unit):
                self.assertEqual(self._datediff(f"DATEDIFF([A], [B], {unit})"), [])

    def test_interval_keyword_is_case_insensitive_and_spaced(self):
        self.assertEqual(self._datediff("DATEDIFF( [A] ,\n [B] ,\n day )"), [])

    def test_four_argument_tableau_form_is_flagged(self):
        issues = self._datediff("DATEDIFF('week', [A], [B], 'monday')")
        self.assertEqual(len(issues), 1)
        self.assertIn('got 4', issues[0])

    def test_two_arguments_are_flagged(self):
        self.assertIn('got 2', self._datediff("DATEDIFF([A], [B])")[0])

    def test_no_arguments_are_flagged(self):
        self.assertIn('got 0', self._datediff("DATEDIFF()")[0])

    def test_unconverted_tableau_three_argument_form_is_flagged(self):
        issues = self._datediff("DATEDIFF('day', [A], [B])")
        self.assertEqual(len(issues), 1)
        self.assertIn('bare keyword', issues[0])

    def test_quoted_interval_is_flagged(self):
        self.assertTrue(self._datediff('DATEDIFF([A], [B], "DAY")'))
        self.assertTrue(self._datediff("DATEDIFF([A], [B], 'DAY')"))

    def test_expression_interval_is_flagged(self):
        self.assertTrue(self._datediff("DATEDIFF([A], [B], [Unit])"))

    def test_commas_inside_nested_calls_and_strings_are_not_arguments(self):
        expr = 'DATEDIFF(IF([A] > 0, [A], DATE(2020, 1, 1)), [B], DAY)'
        self.assertEqual(self._datediff(expr), [])
        self.assertEqual(self._datediff('DATEDIFF([a, b], [c], MONTH)'), [])

    def test_every_nested_call_is_checked(self):
        expr = 'DATEDIFF(DATEDIFF([A], [B], DAY), [C], MONTH) + DATEDIFF([D], [E])'
        issues = self._datediff(expr)
        self.assertEqual(len(issues), 1)
        self.assertIn('got 2', issues[0])

    def test_text_in_strings_comments_and_names_is_ignored(self):
        for expr in (
            '"DATEDIFF(x)"',
            '[DATEDIFF(a, b)] + 1',
            "/* DATEDIFF('week', a, b, 'monday') */ SUM([A])",
            "SUM([A]) // DATEDIFF(x)",
        ):
            with self.subTest(expr=expr):
                self.assertEqual(self._datediff(expr), [])

    def test_unterminated_call_is_left_to_the_balance_check(self):
        issues = validate_dax_expression("DATEDIFF([A], [B], DAY")
        self.assertEqual([i for i in issues if 'DATEDIFF' in i], [])
        self.assertTrue(any('unmatched' in i for i in issues))

    def test_converter_falls_back_to_a_todo_for_the_four_argument_form(self):
        result = convert_tableau_formula_to_dax(
            "DATEDIFF('week', [Order Date], [Ship Date], 'monday')",
            column_name='Lead Weeks',
            validate_output=True,
            fallback_on_invalid=True,
        )
        self.assertIn('TODO: DAX conversion validation failed for Lead Weeks', result)
        self.assertTrue(result.rstrip().endswith('BLANK()'))
        self.assertNotIn("'monday'", result.split('*/')[-1])

    def test_converter_still_converts_the_supported_three_argument_form(self):
        result = convert_tableau_formula_to_dax(
            "DATEDIFF('day', [Order Date], [Ship Date])",
            column_name='Lead Days',
            validate_output=True,
            fallback_on_invalid=True,
        )
        self.assertNotIn('TODO', result)
        self.assertEqual(self._datediff(result), [])
        self.assertIn('DAY)', result)


class TestMaskLiteralSpans(unittest.TestCase):
    """Masking keeps offsets so position-based messages stay accurate."""

    def test_length_is_preserved(self):
        expr = 'IF([Show % (Nat)] = "a (b)", 1, 0)'
        self.assertEqual(len(mask_literal_spans(expr)), len(expr))

    def test_parens_in_bracketed_name_are_masked(self):
        self.assertEqual(mask_literal_spans('[a (b)]').count('('), 0)

    def test_parens_in_string_are_masked(self):
        self.assertEqual(mask_literal_spans('"a (b)"').count('('), 0)

    def test_grouping_parens_survive(self):
        self.assertEqual(mask_literal_spans('SUM(([A]))').count('('), 2)

    def test_escaped_bracket_inside_name(self):
        self.assertEqual(mask_literal_spans('[a]]b (c)]').count('('), 0)

    def test_empty_input(self):
        self.assertEqual(mask_literal_spans(''), '')


class TestArtifactValidatorSpanAwareness(unittest.TestCase):
    """A Tableau calculation with no caption is named after its own formula,
    so a generated measure can legitimately be called
    ``[if SUM(Elec (GWh))>1 then SUM(Elec...]``. Counting those parentheses as
    grouping made the openability gate block projects whose DAX was correct.
    """

    def _parens(self, formula):
        return [i for i in ArtifactValidator.validate_dax_formula(formula)
                if 'parenthesis' in i]

    def test_parens_inside_measure_name_are_literal(self):
        expr = 'VAR _val = [if SUM(Elec (GWh))>1 then SUM(Elec...] RETURN _val'
        self.assertEqual(self._parens(expr), [])

    def test_nested_parens_inside_measure_name(self):
        expr = 'VAR _v = [if (SUM(Energie (GWh)))>=1 then X...] RETURN _v'
        self.assertEqual(self._parens(expr), [])

    def test_parens_inside_string_literal_are_literal(self):
        self.assertEqual(self._parens('FORMAT(_val, "#,0.00 (k)")'), [])

    def test_genuinely_unbalanced_open_is_still_caught(self):
        issues = self._parens('SUM(Sales[Amount]')
        self.assertTrue(any('opening' in i for i in issues))

    def test_genuinely_unbalanced_close_is_still_caught(self):
        issues = self._parens('SUM(Sales[Amount]))')
        self.assertTrue(any('closing' in i for i in issues))

    def test_unbalanced_paren_outside_a_named_reference_is_caught(self):
        # The name is well-formed; the surrounding call is not.
        issues = self._parens('IF([Show % (Nat)] = 1, BLANK()')
        self.assertTrue(any('opening' in i for i in issues))

    def test_line_comment_inside_name_is_not_flagged(self):
        issues = ArtifactValidator.validate_dax_formula('SUM([http://host])')
        self.assertEqual([i for i in issues if 'line comment' in i], [])

    def test_real_line_comment_is_still_flagged(self):
        issues = ArtifactValidator.validate_dax_formula('SUM(Sales[A]) // total')
        self.assertTrue(any('line comment' in i for i in issues))

    def test_parameter_reference_is_still_flagged(self):
        # Masking blanks brackets, so this check must keep reading the raw text.
        issues = ArtifactValidator.validate_dax_formula('[Parameters].[Rate]')
        self.assertTrue(any('Unresolved parameter' in i for i in issues))

    def test_tableau_leak_is_still_flagged(self):
        issues = ArtifactValidator.validate_dax_formula('COUNTD(Sales[Id])')
        self.assertTrue(any('Tableau function leak' in i for i in issues))


class TestConverterGuardMode(unittest.TestCase):
    def test_default_behavior_unchanged(self):
        result = convert_tableau_formula_to_dax("SUM([Sales])", table_name='Orders')
        self.assertIn('SUM', result)

    def test_validate_output_no_fallback(self):
        result = convert_tableau_formula_to_dax(
            "DATETRUNC('year', [OrderDate])",
            validate_output=True,
            fallback_on_invalid=False,
        )
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_validate_output_with_fallback(self):
        result = convert_tableau_formula_to_dax(
            "[Sales] + \"unterminated",  # intentionally malformed string literal
            column_name='WinSum',
            validate_output=True,
            fallback_on_invalid=True,
        )
        self.assertIn('TODO: DAX conversion validation failed', result)
        self.assertIn('BLANK()', result)

    def test_validate_output_clean_formula_passes(self):
        result = convert_tableau_formula_to_dax(
            "SUM([Sales])",
            column_name='TotalSales',
            table_name='Orders',
            validate_output=True,
            fallback_on_invalid=True,
        )
        self.assertNotIn('TODO', result)
        self.assertIn('SUM', result)


class TestMInjectionValidation(unittest.TestCase):
    """Test that _inject_m_steps_into_partition validates after injection."""

    def test_inject_valid_m_steps(self):
        from powerbi_import.tmdl_generator import _inject_m_steps_into_partition
        table = {
            'name': 'Sales',
            'partitions': [{
                'name': 'Partition0',
                'source': {
                    'type': 'm',
                    'expression': 'let\n    Source = #table({"A"}, {{1}})\nin\n    Source',
                }
            }]
        }
        step = ('#"Added Col"', 'Table.AddColumn({prev}, "Col", each 1)')
        result = _inject_m_steps_into_partition(table, [step])
        self.assertTrue(result)
        expr = table['partitions'][0]['source']['expression']
        self.assertIn('Added Col', expr)

    def test_inject_no_steps_returns_false(self):
        from powerbi_import.tmdl_generator import _inject_m_steps_into_partition
        table = {'name': 'T', 'partitions': [{'source': {'type': 'm', 'expression': 'x'}}]}
        result = _inject_m_steps_into_partition(table, [])
        self.assertFalse(result)


class TestPostProcessingDaxSweep(unittest.TestCase):
    """Test that _build_table applies post-processing DAX validation."""

    def test_valid_formula_survives_sweep(self):
        """A well-formed formula through _build_table should not be replaced."""
        result = convert_tableau_formula_to_dax(
            "SUM([Sales])",
            table_name='Orders',
            validate_output=True,
            fallback_on_invalid=True,
        )
        self.assertNotIn('TODO', result)
        self.assertIn('SUM', result)


if __name__ == '__main__':
    unittest.main()
