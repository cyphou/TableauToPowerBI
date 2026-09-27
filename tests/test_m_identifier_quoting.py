"""An M generalized identifier joins its parts with single spaces.

Tableau column names routinely keep an edge space ('Montant Articles '), and
such a name emitted bare inside [...] makes Power BI Desktop fail the whole
model load with "M Engine error: Invalid identifier" -- no line, no column, no
partition named. Quoting it as [#"..."] is always legal, so the rule is simply
to quote whenever the bare form is not.
"""
import unittest

from powerbi_import.calc_column_utils import (_m_identifier_needs_quoting,
                                              _quote_m_ids)
from powerbi_import.m_validator import validate_m_query


class TestIdentifierPredicate(unittest.TestCase):

    def test_plain_name_is_legal(self):
        self.assertFalse(_m_identifier_needs_quoting('Amount'))

    def test_single_interior_spaces_are_legal(self):
        self.assertFalse(_m_identifier_needs_quoting('Montant Articles HT'))

    def test_accented_name_is_legal(self):
        self.assertFalse(_m_identifier_needs_quoting('Degré incertitude'))

    def test_trailing_space_needs_quoting(self):
        self.assertTrue(_m_identifier_needs_quoting('Montant Articles '))

    def test_leading_space_needs_quoting(self):
        self.assertTrue(_m_identifier_needs_quoting(' Montant'))

    def test_double_space_needs_quoting(self):
        self.assertTrue(_m_identifier_needs_quoting('Montant  Articles'))

    def test_tab_needs_quoting(self):
        self.assertTrue(_m_identifier_needs_quoting('Montant\tArticles'))

    def test_empty_needs_quoting(self):
        self.assertTrue(_m_identifier_needs_quoting(''))

    def test_special_characters_still_need_quoting(self):
        for name in ('% part', 'Nb(logts)/2', 'A&B', 'Coût, à payer'):
            self.assertTrue(_m_identifier_needs_quoting(name), name)

    def test_curly_apostrophe_needs_quoting(self):
        # U+2019 is punctuation, not a letter, so it cannot appear bare. A
        # blacklist of forbidden characters missed it and Power BI refused
        # the whole model with "Invalid identifier".
        self.assertTrue(_m_identifier_needs_quoting("Surface d\u2019usage"))

    def test_other_unicode_punctuation_needs_quoting(self):
        for name in ('Marge \u2013 nette', 'Taux \u00ab net \u00bb',
                     'Ratio \u2044 an', 'Co\u00fbt\u2026 total'):
            self.assertTrue(_m_identifier_needs_quoting(name), name)

    def test_the_rule_is_a_whitelist_not_a_blacklist(self):
        # Any character that is neither a letter, a digit, an underscore nor
        # a single space must be quoted, whatever it is.
        for codepoint in (0x2019, 0x00A0, 0x2013, 0x2212, 0x00B7, 0x203A):
            name = 'A' + chr(codepoint) + 'B'
            self.assertTrue(_m_identifier_needs_quoting(name),
                            'U+%04X' % codepoint)


class TestQuotingInExpressions(unittest.TestCase):

    def test_trailing_space_selector_is_quoted(self):
        self.assertEqual(_quote_m_ids('[Montant Articles ]'),
                         '[#"Montant Articles "]')

    def test_leading_space_selector_is_quoted(self):
        self.assertEqual(_quote_m_ids('[ Montant]'), '[#" Montant"]')

    def test_clean_selector_is_left_alone(self):
        self.assertEqual(_quote_m_ids('[Montant Articles]'),
                         '[Montant Articles]')

    def test_already_quoted_is_not_double_quoted(self):
        self.assertEqual(_quote_m_ids('[#"Montant "]'), '[#"Montant "]')

    def test_record_literal_is_left_alone(self):
        expr = 'Csv.Document(Source, [Delimiter=",", Encoding=1252])'
        self.assertEqual(_quote_m_ids(expr), expr)

    def test_quoting_makes_the_expression_valid(self):
        bad = 'let x = Table.AddColumn(S, "c", each [Montant Articles ]) in x'
        self.assertTrue(validate_m_query(bad))
        self.assertEqual(validate_m_query(_quote_m_ids(bad)), [])


class TestValidatorDetectsInvalidSelectors(unittest.TestCase):
    def test_trailing_space_selector_is_reported(self):
        issues = validate_m_query('let x = [Montant Articles ] in x')
        self.assertTrue(any('Invalid' in i or 'not a valid M identifier' in i
                            for i in issues), issues)

    def test_clean_query_has_no_selector_issue(self):
        issues = validate_m_query('let x = [Montant Articles] in x')
        self.assertEqual(issues, [])

    def test_record_literal_is_not_reported(self):
        issues = validate_m_query(
            'let x = Csv.Document(S, [Delimiter=",", Columns=3]) in x')
        self.assertEqual(issues, [])

    def test_list_literal_is_not_reported(self):
        issues = validate_m_query('let x = Table.SelectColumns(S, {"a"}) in x')
        self.assertEqual(issues, [])


class TestDuplicatedPredicatesAgree(unittest.TestCase):
    """m_validator keeps its own copy so the gate stays stdlib-only."""

    def test_both_copies_agree(self):
        from powerbi_import.m_validator import (
            _m_identifier_needs_quoting as gate_copy)
        names = ['Amount', 'Montant Articles', 'Montant Articles ', ' M',
                 'Montant  Articles', 'Montant\tArticles', '', 'Degré',
                 '% part', 'Nb(logts)/2', 'A&B', 'a.b', 'Order Date 2026',
                 'Surface d\u2019usage', 'Marge \u2013 nette']
        for n in names:
            self.assertEqual(_m_identifier_needs_quoting(n), gate_copy(n), n)


if __name__ == '__main__':
    unittest.main()
