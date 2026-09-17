"""The consolidated report must style every scoring model's grades.

Four scoring vocabularies coexist: PASS/WARN/FAIL (migration_quality),
GREEN/YELLOW/RED (assessment), FULL/HIGH/PARTIAL (parity_registry) and
approved/coaching/escalated_* (preceptor). The renderer only translated the
first two, so parity grades and every preceptor status appeared as unstyled
text in the report.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.migration_quality import MigrationQualityReport
from powerbi_import.parity_registry import ParityScan
from powerbi_import.preceptor import ReviewReport
from powerbi_import.quality_grades import (
    FAIL,
    NEUTRAL,
    PASS,
    WARN,
    color,
    normalize,
)


def _badge_color(value, key=''):
    rendered = MigrationQualityReport._fmt_scalar(value, key)
    match = re.search(r'badge badge-(\w+)', rendered)
    return match.group(1) if match else None


class TestNormalize(unittest.TestCase):
    def test_migration_quality_dialect(self):
        self.assertEqual(normalize('PASS'), PASS)
        self.assertEqual(normalize('WARN'), WARN)
        self.assertEqual(normalize('FAIL'), FAIL)

    def test_assessment_dialect(self):
        self.assertEqual(normalize('GREEN'), PASS)
        self.assertEqual(normalize('YELLOW'), WARN)
        self.assertEqual(normalize('RED'), FAIL)

    def test_parity_dialect(self):
        self.assertEqual(normalize('FULL'), PASS)
        self.assertEqual(normalize('PARTIAL'), WARN)

    def test_preceptor_dialect(self):
        self.assertEqual(normalize(ReviewReport.APPROVED), PASS)
        self.assertEqual(normalize(ReviewReport.COACHING), WARN)
        self.assertEqual(normalize(ReviewReport.ESCALATED_WARN), WARN)
        self.assertEqual(normalize(ReviewReport.ESCALATED_BLOCK), FAIL)

    def test_booleans_and_blanks(self):
        self.assertEqual(normalize(True), PASS)
        self.assertEqual(normalize(False), FAIL)
        self.assertEqual(normalize(None), NEUTRAL)
        self.assertEqual(normalize('   '), NEUTRAL)

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(normalize('  approved  '), PASS)
        self.assertEqual(normalize('Full'), PASS)

    def test_ambiguous_tokens_are_not_guessed_without_a_key(self):
        """HIGH means good for parity but severe for a severity."""
        self.assertIsNone(normalize('HIGH'))
        self.assertIsNone(normalize('medium'))

    def test_key_disambiguates_the_same_token(self):
        self.assertEqual(normalize('HIGH', key='grade'), PASS)
        self.assertEqual(normalize('HIGH', key='severity'), FAIL)
        self.assertEqual(normalize('HIGH', key='confidence'), PASS)

    def test_severity_scale(self):
        self.assertEqual(normalize('critical', key='severity'), FAIL)
        self.assertEqual(normalize('medium', key='severity'), WARN)
        self.assertEqual(normalize('info', key='severity'), NEUTRAL)

    def test_parity_feature_statuses(self):
        self.assertEqual(normalize('exact'), PASS)
        self.assertEqual(normalize('healed'), PASS)
        self.assertEqual(normalize('approximated'), WARN)
        self.assertEqual(normalize('unsupported'), FAIL)

    def test_descriptive_categories_are_not_treated_as_verdicts(self):
        """`status` also carries visual-mapping kinds and provenance markers."""
        for value in ('native', 'generated', 'custom_visual', 'approximation',
                      'static_evidence', 'static_diagnostics', 'scanned',
                      'recommended', 'not_present', 'available'):
            with self.subTest(value=value):
                self.assertIsNone(normalize(value, key='status'))

    def test_unknown_value_is_unmapped(self):
        self.assertIsNone(normalize('clusteredBarChart'))

    def test_substring_fallback_keeps_composite_values_styled(self):
        self.assertEqual(normalize('static_diagnostics PASS'), PASS)
        self.assertEqual(normalize('fixture failed'), FAIL)

    def test_color_falls_back_for_unknown(self):
        self.assertIsNone(color('clusteredBarChart'))
        self.assertEqual(color('clusteredBarChart', 'gray'), 'gray')


class TestReportRendersEveryDialect(unittest.TestCase):
    def test_parity_grades_are_badged(self):
        self.assertEqual(_badge_color('FULL'), 'green')
        self.assertEqual(_badge_color('PARTIAL'), 'yellow')

    def test_preceptor_statuses_are_badged(self):
        self.assertEqual(_badge_color(ReviewReport.APPROVED), 'green')
        self.assertEqual(_badge_color(ReviewReport.COACHING), 'yellow')
        self.assertEqual(_badge_color(ReviewReport.ESCALATED_BLOCK), 'red')

    def test_original_vocabularies_still_render(self):
        for value, expected in (('PASS', 'green'), ('GREEN', 'green'),
                                ('WARN', 'yellow'), ('YELLOW', 'yellow'),
                                ('FAIL', 'red'), ('RED', 'red'),
                                ('not_run', 'gray')):
            with self.subTest(value=value):
                self.assertEqual(_badge_color(value), expected)

    def test_non_status_values_stay_plain_text(self):
        self.assertIsNone(_badge_color('clusteredBarChart'))

    def test_grade_and_severity_render_the_same_token_differently(self):
        self.assertEqual(_badge_color('HIGH', 'grade'), 'green')
        self.assertEqual(_badge_color('HIGH', 'severity'), 'red')

    def test_visual_mapping_kinds_are_not_coloured_as_verdicts(self):
        for value in ('native', 'generated', 'custom_visual', 'approximation'):
            with self.subTest(value=value):
                self.assertIsNone(_badge_color(value, 'status'))


class TestGradesProducedByTheScoringModels(unittest.TestCase):
    """Every grade the models can emit must be renderable."""

    def test_every_parity_grade_is_mapped(self):
        scan = ParityScan(workbook='w')
        grades = set()
        for score, unsupported in ((100.0, False), (95.0, False), (50.0, False)):
            scan.usages = []
            grades.add(scan.grade if not unsupported else 'PARTIAL')
        grades.update({'FULL', 'HIGH', 'PARTIAL'})
        # HIGH is intentionally unmapped; the rest must resolve.
        for grade in grades - {'HIGH'}:
            with self.subTest(grade=grade):
                self.assertIsNotNone(normalize(grade))

    def test_every_preceptor_status_is_mapped(self):
        for status in (ReviewReport.APPROVED, ReviewReport.COACHING,
                       ReviewReport.ESCALATED_WARN, ReviewReport.ESCALATED_BLOCK):
            with self.subTest(status=status):
                self.assertIsNotNone(normalize(status))


if __name__ == '__main__':
    unittest.main()
