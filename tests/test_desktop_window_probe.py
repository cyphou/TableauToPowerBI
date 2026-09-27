"""The old probe watched the process, so it could not fail: a corrupted
report, a deleted SemanticModel and invalid DAX all reported 'opened'.

Waiting for the report window and capturing it gives a verdict that can be
wrong in the useful direction. These tests drive that logic with doubles --
they never launch Power BI.
"""
import os
import struct
import sys
import tempfile
import unittest
import zlib
from unittest import mock

from powerbi_import import desktop_window
from powerbi_import.desktop_probe import (DesktopProbeReport,
                                          WINDOW_VERIFIED_SCOPE, desktop_pids,
                                          probe_desktop_open)


def _win(title='Report - Power BI Desktop', hwnd=11, owned=False,
         width=1200, height=800):
    return desktop_window.WindowInfo(hwnd=hwnd, title=title,
                                     class_name='HwndWrapper',
                                     width=width, height=height, owned=owned)


class _FakeProc:
    def __init__(self, rc=None):
        self.pid = 4242
        self.returncode = rc
        self._rc = rc

    def poll(self):
        return self._rc

    def terminate(self):
        self._rc = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._rc = -9


def _pbip(directory):
    path = os.path.join(directory, 'Report.pbip')
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write('{}')
    return path


class TestPngEncoding(unittest.TestCase):

    def test_encodes_a_readable_png(self):
        width, height = 3, 2
        bgra = bytes([10, 20, 30, 255] * (width * height))
        png = desktop_window._png_bytes(width, height, bgra)
        self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))
        w, h = struct.unpack('>II', png[16:24])
        self.assertEqual((w, h), (width, height))

    def test_channels_are_reordered_to_rgb(self):
        png = desktop_window._png_bytes(1, 1, bytes([1, 2, 3, 255]))
        start = png.find(b'IDAT') + 4
        size = struct.unpack('>I', png[start - 8:start - 4])[0]
        raw = zlib.decompress(png[start:start + size])
        self.assertEqual(raw, bytes([0, 3, 2, 1]))   # filter byte + RGB

    def test_blank_capture_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'flat.png')
            with open(path, 'wb') as handle:
                handle.write(desktop_window._png_bytes(
                    64, 64, bytes([0, 0, 0, 255] * 64 * 64)))
            self.assertTrue(desktop_window.is_blank(path))

    def test_varied_capture_is_not_blank(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'noise.png')
            pixels = bytes(bytearray(
                (i * 37) % 251 for i in range(64 * 64 * 4)))
            with open(path, 'wb') as handle:
                handle.write(desktop_window._png_bytes(64, 64, pixels))
            self.assertFalse(desktop_window.is_blank(path))

    def test_a_uniform_top_edge_does_not_make_it_blank(self):
        """The shape of every real capture: flat chrome, content below.

        Sampling only the first few thousand bytes never got past the title
        bar, so a perfectly good screenshot was reported as caught-nothing.
        """
        width = height = 64
        flat_rows = 24
        pixels = bytearray([0, 0, 0, 255] * width * flat_rows)
        pixels += bytearray(
            (i * 37) % 251 for i in range(width * (height - flat_rows) * 4))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'chrome.png')
            with open(path, 'wb') as handle:
                handle.write(desktop_window._png_bytes(
                    width, height, bytes(pixels)))
            self.assertFalse(desktop_window.is_blank(path))

    def test_a_truncated_image_is_unknown_not_blank(self):
        png = desktop_window._png_bytes(8, 8, bytes([7, 7, 7, 255] * 64))
        head = png[:16] + struct.pack('>II', 8, 4096) + png[24:]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'short.png')
            with open(path, 'wb') as handle:
                handle.write(head)
            self.assertIsNone(desktop_window.is_blank(path))

    def test_unreadable_file_is_unknown_not_blank(self):
        self.assertIsNone(desktop_window.is_blank(
            os.path.join(tempfile.gettempdir(), 'no_such_capture.png')))


class TestReportWindowSelection(unittest.TestCase):

    def test_picks_the_window_carrying_the_report_name(self):
        with mock.patch.object(desktop_window, 'windows_for_pid',
                               side_effect=lambda pid: {
                                   1: [_win('Untitled - Power BI Desktop')],
                                   2: [_win('Sales - Power BI Desktop', hwnd=22)],
                               }[pid]):
            win = desktop_window.report_window([1, 2], 'Sales')
        self.assertEqual(win.hwnd, 22)

    def test_ignores_owned_dialog_windows(self):
        with mock.patch.object(desktop_window, 'windows_for_pid',
                               return_value=[_win('Sales', owned=True)]):
            self.assertIsNone(desktop_window.report_window([1], 'Sales'))

    def test_returns_none_when_no_title_matches(self):
        with mock.patch.object(desktop_window, 'windows_for_pid',
                               return_value=[_win('Untitled - Power BI Desktop')]):
            self.assertIsNone(desktop_window.report_window([1], 'Sales'))


class TestDesktopPids(unittest.TestCase):

    def test_parses_tasklist_csv(self):
        out = ('"PBIDesktop.exe","1234","Console","1","500 K"\n'
               '"PBIDesktop.exe","5678","Console","1","900 K"\n')
        with mock.patch('powerbi_import.desktop_probe.os.name', 'nt'), \
             mock.patch('powerbi_import.desktop_probe.subprocess.run',
                        return_value=mock.Mock(stdout=out)):
            self.assertEqual(desktop_pids(), [1234, 5678])

    def test_no_instances_yields_empty(self):
        out = 'INFO: No tasks are running which match the specified criteria.\n'
        with mock.patch('powerbi_import.desktop_probe.os.name', 'nt'), \
             mock.patch('powerbi_import.desktop_probe.subprocess.run',
                        return_value=mock.Mock(stdout=out)):
            self.assertEqual(desktop_pids(), [])

    def test_a_broken_subprocess_never_raises(self):
        with mock.patch('powerbi_import.desktop_probe.os.name', 'nt'), \
             mock.patch('powerbi_import.desktop_probe.subprocess.run',
                        side_effect=OSError('boom')):
            self.assertEqual(desktop_pids(), [])


class TestWindowAwareProbe(unittest.TestCase):

    def _probe(self, window, signals, shot=True, rc=None, fallback=None):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'shot.png') if shot else None
            with mock.patch('powerbi_import.desktop_probe.find_pbi_desktop',
                            return_value='pbi.exe'), \
                 mock.patch('powerbi_import.desktop_probe.subprocess.Popen',
                            return_value=_FakeProc(rc)), \
                 mock.patch('powerbi_import.desktop_probe.desktop_pids',
                            return_value=[4242]), \
                 mock.patch('powerbi_import.desktop_probe._recent_matches',
                            return_value=[]), \
                 mock.patch('powerbi_import.desktop_probe._scan_trace_errors',
                            return_value=[]), \
                 mock.patch.object(desktop_window, 'IS_WINDOWS', True), \
                 mock.patch.object(desktop_window, 'wait_for_window',
                                   return_value=(window, signals)), \
                 mock.patch.object(desktop_window, 'dialog_windows',
                                   return_value=[]), \
                 mock.patch.object(desktop_window, 'main_window',
                                   return_value=window or fallback), \
                 mock.patch.object(desktop_window, 'capture_window',
                                   side_effect=lambda h, p: p), \
                 mock.patch.object(desktop_window, 'is_blank',
                                   return_value=False):
                return probe_desktop_open(_pbip(d), timeout=1,
                                          screenshot_path=path)

    def test_a_loaded_window_is_opened(self):
        report = self._probe(_win('Report - Power BI Desktop'), [])
        self.assertEqual(report.status, 'opened')
        self.assertTrue(report.window_loaded)
        self.assertEqual(report.to_dict()['verified'], WINDOW_VERIFIED_SCOPE)

    def test_an_already_running_instance_warns_but_does_not_fail(self):
        report = self._probe(_win(), [])
        self.assertEqual(report.status, 'opened')
        self.assertTrue(any('already running' in w for w in report.warnings))

    def test_a_loaded_window_is_captured(self):
        report = self._probe(_win(), [])
        self.assertTrue(report.screenshot)
        self.assertFalse(report.screenshot_blank)

    def test_a_missing_window_fails(self):
        # The point of the rewrite: this verdict was impossible before.
        report = self._probe(None, ['no window titled ... before the timeout'])
        self.assertEqual(report.status, 'crashed')
        self.assertFalse(report.window_loaded)

    def test_a_missing_window_still_captures_evidence(self):
        # Desktop sits on an Untitled window when it refuses the project, and
        # that window is the evidence worth keeping.
        report = self._probe(None, ['no window titled ... before the timeout'],
                             fallback=_win('Untitled - Power BI Desktop'))
        self.assertTrue(report.screenshot)

    def test_a_failed_load_never_claims_a_window(self):
        report = self._probe(None, ['no window'])
        self.assertIsNone(report.window_title)
        self.assertFalse(report.to_dict()['window_loaded'])

    def test_an_open_dialog_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch('powerbi_import.desktop_probe.find_pbi_desktop',
                            return_value='pbi.exe'), \
                 mock.patch('powerbi_import.desktop_probe.subprocess.Popen',
                            return_value=_FakeProc()), \
                 mock.patch('powerbi_import.desktop_probe.desktop_pids',
                            return_value=[4242]), \
                 mock.patch('powerbi_import.desktop_probe._recent_matches',
                            return_value=[]), \
                 mock.patch('powerbi_import.desktop_probe._scan_trace_errors',
                            return_value=[]), \
                 mock.patch.object(desktop_window, 'IS_WINDOWS', True), \
                 mock.patch.object(desktop_window, 'wait_for_window',
                                   return_value=(_win(), [])), \
                 mock.patch.object(desktop_window, 'dialog_windows',
                                   return_value=[_win('Fields that need to be '
                                                      'fixed', owned=True)]):
                report = probe_desktop_open(_pbip(d), timeout=1)
        self.assertEqual(report.status, 'crashed')
        self.assertTrue(any('error dialog' in s for s in report.signals))
        self.assertIn('Fields that need to be fixed', report.dialogs)


class TestErrorDialogEndsTheWait(unittest.TestCase):
    """A popup is what makes a project unusable, so it must end the wait at
    once rather than let the probe sit out its timeout."""

    def _wait(self, dialogs, report=None, timeout=4):
        with mock.patch.object(desktop_window, 'IS_WINDOWS', True), \
             mock.patch.object(desktop_window, 'dialog_windows',
                               return_value=dialogs), \
             mock.patch.object(desktop_window, 'report_window',
                               return_value=report), \
             mock.patch.object(desktop_window, 'main_window',
                               return_value=None), \
             mock.patch.object(desktop_window, 'is_responsive',
                               return_value=True), \
             mock.patch.object(desktop_window.time, 'sleep'):
            return desktop_window.wait_for_window([1], 'Sales', timeout=timeout)

    def test_a_dialog_stops_the_wait(self):
        win, signals = self._wait([_win('Something went wrong', owned=True)])
        self.assertIsNone(win)
        self.assertTrue(any('Something went wrong' in s for s in signals))

    def test_the_dialog_title_is_reported(self):
        _win_, signals = self._wait([_win('Fields that need to be fixed',
                                          owned=True)])
        self.assertTrue(any('error dialog' in s for s in signals))

    def test_no_dialog_lets_a_good_load_through(self):
        loaded = _win('Sales - Power BI Desktop')
        win, signals = self._wait([], report=loaded)
        self.assertIs(win, loaded)
        self.assertEqual(signals, [])

    def test_no_dialog_and_no_window_still_times_out(self):
        win, signals = self._wait([])
        self.assertIsNone(win)
        self.assertTrue(any('timeout' in s for s in signals))


class TestReportShape(unittest.TestCase):

    def test_new_fields_are_serialised(self):
        data = DesktopProbeReport(pbip_path='x.pbip').to_dict()
        for key in ('window_title', 'screenshot', 'screenshot_blank',
                    'dialogs', 'window_loaded'):
            self.assertIn(key, data)

    def test_window_loaded_defaults_to_false(self):
        self.assertFalse(DesktopProbeReport(pbip_path='x.pbip').window_loaded)


if __name__ == '__main__':
    unittest.main()
