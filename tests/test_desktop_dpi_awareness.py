"""The probe must measure windows in real pixels, not scaled ones.

A DPI-unaware process gets GetWindowRect in logical coordinates. Measured
against a live Power BI window on a 125% display: 1550x926 unaware versus
1938x1158 aware. The bitmap was sized from the smaller rect while PrintWindow
drew at the true size, so every screenshot lost its bottom and right edges --
on an error dialog, the buttons.
"""
import ast
import os
import unittest

from powerbi_import import desktop_window


def _calls_in(function_name):
    """Names called inside *function_name*, read from the source."""
    path = os.path.join(os.path.dirname(desktop_window.__file__),
                        'desktop_window.py')
    with open(path, 'r', encoding='utf-8') as handle:
        tree = ast.parse(handle.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return {c.func.id for c in ast.walk(node)
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
    raise AssertionError('%s is not defined' % function_name)


class TestAwarenessIsClaimedBeforeMeasuring(unittest.TestCase):
    """Asserted from the source: the platform decides whether it runs."""

    def test_capture_asks_before_reading_the_rect(self):
        self.assertIn('_ensure_dpi_aware', _calls_in('capture_window'))

    def test_enumeration_asks_before_reading_the_rect(self):
        self.assertIn('_ensure_dpi_aware', _calls_in('windows_for_pid'))

    def test_the_reader_can_see_a_missing_call(self):
        # Guards the two assertions above: prove the detector can fail.
        self.assertNotIn('_ensure_dpi_aware', _calls_in('_ensure_dpi_aware'))


class TestEnsureDpiAware(unittest.TestCase):

    def setUp(self):
        self._saved = desktop_window._dpi_ready
        desktop_window._dpi_ready = False
        self.addCleanup(setattr, desktop_window, '_dpi_ready', self._saved)

    def _use(self, calls):
        self.addCleanup(setattr, desktop_window, '_dpi_calls',
                        desktop_window._dpi_calls)
        desktop_window._dpi_calls = lambda: calls

    def test_the_first_api_that_works_wins(self):
        log = []
        self._use([lambda: log.append('new') or True,
                   lambda: log.append('old') or True])
        self.assertTrue(desktop_window._ensure_dpi_aware())
        self.assertEqual(log, ['new'])

    def test_a_refused_api_falls_through_to_the_next(self):
        log = []
        self._use([lambda: log.append('new') or False,
                   lambda: log.append('old') or True])
        self.assertTrue(desktop_window._ensure_dpi_aware())
        self.assertEqual(log, ['new', 'old'])

    def test_a_raising_api_never_escapes(self):
        def boom():
            raise OSError('not available on this build')

        self._use([boom, lambda: True])
        self.assertTrue(desktop_window._ensure_dpi_aware())

    def test_no_api_at_all_is_survivable(self):
        self._use([])
        self.assertFalse(desktop_window._ensure_dpi_aware())

    def test_it_runs_at_most_once(self):
        # Awareness is process-wide and one-shot; retrying every poll would
        # be noise, and a host that already chose wins anyway.
        log = []
        self._use([lambda: log.append('try') or False])
        desktop_window._ensure_dpi_aware()
        desktop_window._ensure_dpi_aware()
        desktop_window._ensure_dpi_aware()
        self.assertEqual(log, ['try'])

    def test_a_failed_attempt_is_not_retried_either(self):
        self._use([lambda: False])
        self.assertFalse(desktop_window._ensure_dpi_aware())
        self._use([lambda: True])
        self.assertTrue(desktop_window._ensure_dpi_aware())   # memo, not a win


class TestDpiCalls(unittest.TestCase):

    def test_nothing_is_attempted_off_windows(self):
        self.addCleanup(setattr, desktop_window, 'IS_WINDOWS',
                        desktop_window.IS_WINDOWS)
        desktop_window.IS_WINDOWS = False
        self.assertEqual(desktop_window._dpi_calls(), [])

    @unittest.skipUnless(desktop_window.IS_WINDOWS, 'Windows only')
    def test_windows_offers_at_least_the_legacy_api(self):
        # SetProcessDPIAware has existed since Vista, so the list is never
        # empty on Windows -- an empty list would mean the lookup broke.
        self.assertTrue(desktop_window._dpi_calls())


if __name__ == '__main__':
    unittest.main()
