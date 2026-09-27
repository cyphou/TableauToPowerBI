"""A PBIR literal must be something Power BI can actually parse.

Two ways to get it wrong, both verified against Power BI Desktop
2.158.908.0 on a real migrated report:

* A bare number crashes ``SQExprValidationVisitor.visitIn`` with
  ``e.accept is not a function`` and the report refuses to render.
  ``2025L``, ``2025D`` and ``'2025'`` all open; ``2025`` does not.
* A quoted string whose inner apostrophe is not doubled closes the
  literal early. French display text hits this constantly.
"""
import json
import os
import tempfile
import unittest

from powerbi_import.openability import _check_literal_grammar
from powerbi_import.pbip_generator import _L, _pbi_literal, _filter_literal


class TestNumericLiteralIsTyped(unittest.TestCase):
    """A bare number is never emitted, whatever route builds it."""

    def test_year_filter_value_carries_a_type_suffix(self):
        # The case that broke Complex_Enterprise: a report-level "In"
        # filter on a year column.
        self.assertEqual(_pbi_literal('2025', column_type='int64'), '2025L')

    def test_untyped_numeric_still_gets_a_suffix(self):
        self.assertEqual(_pbi_literal('2025'), '2025L')
        self.assertEqual(_pbi_literal('12.5'), '12.5D')

    def test_the_column_type_decides_not_the_value_shape(self):
        self.assertEqual(_pbi_literal('7', column_type='double'), '7D')
        self.assertEqual(_pbi_literal('7.0', column_type='int64'), '7L')

    def test_range_and_in_filters_agree(self):
        # _filter_literal and _pbi_literal are two doors to one rule.
        self.assertEqual(_filter_literal('2025'), _pbi_literal('2025'))

    def test_a_value_with_no_literal_form_is_not_faked(self):
        for value in ('nan', 'inf', '-inf'):
            self.assertEqual(_pbi_literal(value), "'%s'" % value)

    def test_non_numeric_is_left_as_a_string(self):
        self.assertEqual(_pbi_literal('hello'), "'hello'")


class TestApostropheInLiteral(unittest.TestCase):
    """_L escapes on behalf of its callers, so every site is right."""

    def test_french_title_is_escaped(self):
        got = _L("'Taux d'utilisation'")
        self.assertEqual(got['expr']['Literal']['Value'],
                         "'Taux d''utilisation'")

    def test_escaping_is_idempotent(self):
        # A caller that already escaped must not be escaped twice.
        once = _L("'Taux d''utilisation'")['expr']['Literal']['Value']
        twice = _L(once)['expr']['Literal']['Value']
        self.assertEqual(once, "'Taux d''utilisation'")
        self.assertEqual(twice, once)

    def test_several_apostrophes_in_one_title(self):
        got = _L("'Part d'audience et taux d'emploi'")
        self.assertEqual(got['expr']['Literal']['Value'],
                         "'Part d''audience et taux d''emploi'")

    def test_accents_are_left_alone(self):
        got = _L("'Chiffre d'affaires réalisé à Genève'")
        self.assertEqual(got['expr']['Literal']['Value'],
                         "'Chiffre d''affaires réalisé à Genève'")

    def test_a_curly_apostrophe_needs_no_escaping(self):
        # U+2019 is not the literal delimiter, so it passes through.
        got = _L("'Taux d\u2019utilisation'")
        self.assertEqual(got['expr']['Literal']['Value'],
                         "'Taux d\u2019utilisation'")

    def test_an_unquoted_literal_is_untouched(self):
        for raw in ('true', 'false', '2025L', '12.5D'):
            self.assertEqual(_L(raw)['expr']['Literal']['Value'], raw)

    def test_an_empty_literal_survives(self):
        self.assertEqual(_L("''")['expr']['Literal']['Value'], "''")


def _project_with_literal(directory, value):
    path = os.path.join(directory, 'Report', 'definition', 'pages', 'p', 'visuals', 'v')
    os.makedirs(path)
    with open(os.path.join(path, 'visual.json'), 'w', encoding='utf-8') as fh:
        json.dump({'visual': {'objects': {'title': [{'properties': {
            'text': {'expr': {'Literal': {'Value': value}}}}}]}}}, fh)


class TestLiteralGrammarCheck(unittest.TestCase):
    """The gate catches both faults without opening Desktop."""

    def _run(self, value):
        with tempfile.TemporaryDirectory() as d:
            _project_with_literal(d, value)
            return _check_literal_grammar(d)

    def test_bare_number_is_blocking(self):
        result = self._run('2025')
        self.assertFalse(result.ok)
        self.assertIn('no type suffix', result.issues[0])

    def test_undoubled_apostrophe_is_blocking(self):
        result = self._run("'Taux d'utilisation'")
        self.assertFalse(result.ok)
        self.assertIn('undoubled apostrophe', result.issues[0])

    def test_a_correct_literal_passes(self):
        for value in ("'Taux d''utilisation'", '2025L', '12.5D', 'true',
                      "'Chiffre d''affaires réalisé'", "''"):
            self.assertTrue(self._run(value).ok, value)

    def test_the_check_is_blocking_not_advisory(self):
        self.assertEqual(self._run('2025').severity, 'error')


if __name__ == '__main__':
    unittest.main()
