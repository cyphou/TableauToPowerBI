"""The preceptorship review must be reachable from the CLI.

The loop was fully implemented but never invoked: nothing outside the module
itself referenced PreceptorLoop or run_preceptor_review, so the six-dimension
quality gate never ran on a real migration. These tests keep it wired.
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import migrate
from powerbi_import.preceptor import ReviewReport


def _args(**kwargs):
    defaults = {'output_dir': None, 'preceptor_block': False, 'quiet': True}
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


class TestPreceptorFlagsExist(unittest.TestCase):
    def setUp(self):
        self.parser = migrate._build_argument_parser()

    def test_preceptor_flag_defaults_off(self):
        args = self.parser.parse_args(['wb.twbx'])
        self.assertFalse(args.preceptor)

    def test_preceptor_flag_can_be_enabled(self):
        args = self.parser.parse_args(['wb.twbx', '--preceptor'])
        self.assertTrue(args.preceptor)

    def test_preceptor_block_flag_defaults_off(self):
        args = self.parser.parse_args(['wb.twbx', '--preceptor'])
        self.assertFalse(args.preceptor_block)

    def test_preceptor_block_flag_can_be_enabled(self):
        args = self.parser.parse_args(
            ['wb.twbx', '--preceptor', '--preceptor-block'])
        self.assertTrue(args.preceptor_block)


class TestPreceptorRunner(unittest.TestCase):
    def test_missing_project_dir_is_skipped_not_failed(self):
        args = _args(output_dir=os.path.join('nonexistent', 'dir'))
        self.assertTrue(migrate._run_preceptor(args, 'Nope'))

    def test_escalated_block_reports_failure(self):
        report = ReviewReport('X')
        report.status = ReviewReport.ESCALATED_BLOCK
        self.assertFalse(self._run_with(report, block=True))

    def test_escalated_warn_does_not_fail_the_migration(self):
        report = ReviewReport('X')
        report.status = ReviewReport.ESCALATED_WARN
        self.assertTrue(self._run_with(report))

    def test_approved_passes(self):
        report = ReviewReport('X')
        report.status = ReviewReport.APPROVED
        self.assertTrue(self._run_with(report))

    def test_review_error_never_fails_the_migration(self):
        """The review is advisory instrumentation, not a correctness oracle."""
        def boom(*a, **k):
            raise RuntimeError('reviewer exploded')
        self.assertTrue(self._run_with(boom))

    def test_block_flag_selects_block_escalation(self):
        seen = {}

        def capture(pbip, extract, **kwargs):
            seen.update(kwargs)
            return ReviewReport('X')

        self._run_with(capture, block=True)
        self.assertEqual(seen.get('on_escalate'), 'block')

    def test_default_selects_warn_escalation(self):
        seen = {}

        def capture(pbip, extract, **kwargs):
            seen.update(kwargs)
            return ReviewReport('X')

        self._run_with(capture)
        self.assertEqual(seen.get('on_escalate'), 'warn')

    def _run_with(self, report_or_fn, block=False):
        import powerbi_import.preceptor as prec

        if callable(report_or_fn) and not isinstance(report_or_fn, ReviewReport):
            stub = report_or_fn
        else:
            def stub(*a, **k):
                return report_or_fn

        original = prec.run_preceptor_review
        prec.run_preceptor_review = stub
        try:
            args = _args(output_dir=os.path.dirname(__file__),
                         preceptor_block=block)
            return migrate._run_preceptor(args, '')
        finally:
            prec.run_preceptor_review = original


if __name__ == '__main__':
    unittest.main()
