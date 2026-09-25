"""Phase 3 foundation tests for DAX conversion guards."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from powerbi_import.dax_validator import validate_dax_expression, mask_literal_spans
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
