"""The consolidated report must read the review the preceptor actually writes.

Fixtures are produced by the preceptor's own classes rather than hand-written
JSON, so a change to its serialization fails these tests instead of silently
leaving the consolidated report reading keys nobody emits.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.migration_quality import (  # noqa: E402
    _review_evidence,
    build_quality_report,
)
from powerbi_import.preceptor import (  # noqa: E402
    CoachingItem,
    ReviewCycle,
    ReviewReport,
)
from tests.test_migration_quality import (  # noqa: E402
    _Assessment,
    _Openability,
)


def _write_review(project_dir, *, status=ReviewReport.APPROVED,
                  coaching=(), escalation_reason=''):
    """Serialize a review through the producer's own to_json()."""
    report = ReviewReport('Demo')
    report.status = status
    report.escalation_reason = escalation_reason
    cycle = ReviewCycle(1)
    cycle.scorecard.set_score('dax_correctness', 4, '1 leak')
    for item in coaching:
        cycle.add_coaching(item)
    report.add_cycle(cycle)
    report.to_json(os.path.join(project_dir, 'preceptor_report.json'))
    return report


def _build_report(project_dir):
    with patch('powerbi_import.migration_quality.run_assessment',
               return_value=_Assessment()), \
         patch('powerbi_import.migration_quality.scan_project') as scan, \
         patch('powerbi_import.migration_quality.compare_report_tables',
               return_value={'summary': {'source_tables': 1, 'tables_found': 1}}), \
         patch('powerbi_import.migration_quality.compare_report_interface',
               return_value={'filters': {'covered': True},
                             'parameters': {'covered': True}}), \
         patch('powerbi_import.migration_quality.check_openability',
               return_value=_Openability()):
        scan.return_value.to_dict.return_value = {'gaps': []}
        return build_quality_report({}, project_dir, 'Demo')


class TestReviewEvidence(unittest.TestCase):
    def test_absent_review_is_not_run_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            review = _review_evidence(tmp)
        self.assertEqual(review['status'], 'not_run')
        self.assertEqual(review['coaching'], [])

    def test_unreadable_review_does_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'preceptor_report.json'), 'w',
                      encoding='utf-8') as handle:
                handle.write('{ not json')
            review = _review_evidence(tmp)
        self.assertEqual(review['status'], 'not_run')

    def test_coaching_is_read_from_the_emitted_shape(self):
        item = CoachingItem(
            dimension='dax_correctness', score=4,
            issue='Tableau function leaked into DAX: COUNTD(',
            location='fact_sales.tmdl',
            fix='Convert COUNTD( to its DAX equivalent',
        )
        with tempfile.TemporaryDirectory() as tmp:
            _write_review(tmp, coaching=[item])
            review = _review_evidence(tmp)
        self.assertEqual(review['status'], 'approved')
        self.assertEqual(len(review['coaching']), 1)
        entry = review['coaching'][0]
        self.assertEqual(entry['dimension'], 'dax_correctness')
        self.assertEqual(entry['fix'], 'Convert COUNTD( to its DAX equivalent')
        self.assertEqual(entry['cycle'], 1)

    def test_cycle_number_uses_the_key_the_producer_emits(self):
        """ReviewCycle serializes `cycle`, not `cycle_number`."""
        with tempfile.TemporaryDirectory() as tmp:
            _write_review(tmp, coaching=[CoachingItem(
                dimension='tmdl_structure', score=4, issue='x', fix='y')])
            path = os.path.join(tmp, 'preceptor_report.json')
            with open(path, encoding='utf-8') as handle:
                raw = json.load(handle)
            self.assertIn('cycle', raw['cycles'][0])
            self.assertNotIn('cycle_number', raw['cycles'][0])
            self.assertIsNotNone(_review_evidence(tmp)['coaching'][0]['cycle'])


class TestCoachingReachesThePriorityQueue(unittest.TestCase):
    def test_coaching_becomes_a_repair_owned_by_the_artifact_owner(self):
        item = CoachingItem(
            dimension='dax_correctness', score=4,
            issue='Tableau function leaked into DAX: COUNTD(',
            location='fact_sales.tmdl',
            fix='Convert COUNTD( to its DAX equivalent',
        )
        with tempfile.TemporaryDirectory() as tmp:
            _write_review(tmp, coaching=[item])
            report = _build_report(tmp)

        entries = [p for p in report.priorities if 'COUNTD' in p['action']]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry['action_kind'], 'repair')
        self.assertEqual(entry['owner'], 'DAX')
        self.assertEqual(entry['priority'], 'P1')
        self.assertEqual(entry['fix'], 'Convert COUNTD( to its DAX equivalent')
        self.assertEqual(entry['evidence'], ['fact_sales.tmdl'])

    def test_each_dimension_routes_to_its_owner(self):
        cases = {
            'dax_correctness': ('repair', 'DAX'),
            'm_query_validity': ('repair', 'Wiring'),
            'tmdl_structure': ('repair', 'Semantic'),
            'pbir_fidelity': ('repair', 'Visual'),
            'completeness': ('repair', 'Orchestrator'),
            'visual_equivalence': ('verify', 'Visual'),
        }
        for dimension, (kind, owner) in cases.items():
            with self.subTest(dimension=dimension):
                item = CoachingItem(dimension=dimension, score=4,
                                    issue=f'issue-{dimension}', fix='do this')
                with tempfile.TemporaryDirectory() as tmp:
                    _write_review(tmp, coaching=[item])
                    report = _build_report(tmp)
                entry = next(p for p in report.priorities
                             if f'issue-{dimension}' in p['action'])
                self.assertEqual(entry['action_kind'], kind)
                self.assertEqual(entry['owner'], owner)

    def test_visual_equivalence_asks_to_verify_not_repair(self):
        """Screenshot similarity is a judgement, not a measurable defect."""
        item = CoachingItem(dimension='visual_equivalence', score=3,
                            issue='SSIM 0.71 below threshold', fix='Compare visually')
        with tempfile.TemporaryDirectory() as tmp:
            _write_review(tmp, coaching=[item])
            report = _build_report(tmp)
        entry = next(p for p in report.priorities if 'SSIM' in p['action'])
        self.assertEqual(entry['action_kind'], 'verify')
        self.assertEqual(entry['priority'], 'P2')

    def test_blocking_escalation_is_a_blocker(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_review(tmp, status=ReviewReport.ESCALATED_BLOCK,
                          escalation_reason='score 2.1 after 3 cycles')
            report = _build_report(tmp)
        self.assertEqual(report.status, 'FAIL')
        self.assertTrue(any('escalated' in b.lower() for b in report.blockers))
        self.assertEqual(report.priorities[0]['priority'], 'P0')

    def test_warning_escalation_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_review(tmp, status=ReviewReport.ESCALATED_WARN,
                          escalation_reason='score 3.4 after 3 cycles')
            report = _build_report(tmp)
        self.assertEqual(report.status, 'WARN')
        self.assertTrue(any('escalated' in w.lower() for w in report.warnings))

    def test_approved_review_adds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_review(tmp)
            report = _build_report(tmp)
        self.assertEqual(report.status, 'PASS')
        self.assertEqual(report.priorities, [])
        self.assertEqual(report.review['status'], 'approved')

    def test_no_review_leaves_the_report_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = _build_report(tmp)
        self.assertEqual(report.status, 'PASS')
        self.assertEqual(report.review['status'], 'not_run')

    def test_fix_text_is_rendered_in_the_html(self):
        item = CoachingItem(dimension='tmdl_structure', score=4,
                            issue='No date table generated',
                            fix='Enable auto-Calendar for date columns')
        with tempfile.TemporaryDirectory() as tmp:
            _write_review(tmp, coaching=[item])
            report = _build_report(tmp)
            html_path = report.save_html(os.path.join(tmp, 'q.html'))
            with open(html_path, encoding='utf-8') as handle:
                html = handle.read()
        self.assertIn('Enable auto-Calendar for date columns', html)


if __name__ == '__main__':
    unittest.main()
