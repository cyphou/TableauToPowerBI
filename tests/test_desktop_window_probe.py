"""The old probe watched the process, so it could not fail: a corrupted
report, a deleted SemanticModel and invalid DAX all reported 'opened'.

Waiting for the report window and capturing it gives a verdict that can be
wrong in the useful direction. These tests drive that logic with doubles --
they never launch Power BI.
"""
import base64
import json
import os
import struct
import tempfile
import unittest
import zlib
from types import SimpleNamespace
from unittest import mock

from powerbi_import import desktop_accessibility as accessibility
from powerbi_import import desktop_window
from powerbi_import.desktop_probe import (
    WINDOW_VERIFIED_SCOPE,
    DesktopProbeReport,
    desktop_pids,
    probe_desktop_open,
    verify_desktop_data,
)
from tests.desktop_dialog_fixtures import layout_dialog_metadata, strict_dialog_metadata


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

    def test_native_capture_integrity_rejects_partial_corrupt_and_appended_pngs(self):
        png = desktop_window._png_bytes(
            2, 1, bytes([10, 20, 30, 255, 40, 50, 60, 255]))
        corrupt = bytearray(png)
        corrupt[-5] ^= 1
        invalid = (png[:20], png[:-1], png[:-12], bytes(corrupt), png + b"extra")
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'capture.png')
            for content in invalid:
                with self.subTest(length=len(content)):
                    with open(path, 'wb') as handle:
                        handle.write(content)
                    self.assertIsNone(desktop_window.inspect_capture_png(path))
            with open(path, 'wb') as handle:
                handle.write(png)
            self.assertEqual(desktop_window.inspect_capture_png(path), {
                'width': 2, 'height': 1, 'blank': False})


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

    def setUp(self):
        # Module-local tooling doubles work on every CI host without changing
        # global sys.platform/os.name or loading Windows native providers.
        self.enterContext(mock.patch.object(
            accessibility, 'sys', SimpleNamespace(platform='win32')))
        self.enterContext(mock.patch.object(
            accessibility, 'resolve_powershell_executable',
            return_value='/__synthetic_native_tooling__/powershell.exe'))

    def _probe(self, window, signals, shot=True, rc=None, fallback=None,
               verify_data=False, data_load=None):
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
                 mock.patch.object(desktop_window, 'window_process_id',
                                   return_value=4242), \
                 mock.patch.object(desktop_window, 'dialog_windows',
                                   return_value=[]), \
                 mock.patch.object(desktop_window, 'main_window',
                                   return_value=window or fallback), \
                 mock.patch.object(desktop_window, 'capture_window',
                                   side_effect=lambda h, p: p), \
                 mock.patch.object(desktop_window, 'is_blank',
                                   return_value=False), \
                 mock.patch('powerbi_import.desktop_probe.verify_desktop_data',
                            return_value=data_load):
                return probe_desktop_open(_pbip(d), timeout=1,
                                          screenshot_path=path,
                                          verify_data=verify_data)

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

    def test_empty_or_unavailable_data_skips_report_capture(self):
        for status in ('empty', 'unavailable'):
            with self.subTest(status=status):
                report = self._probe(
                    _win(), [], verify_data=True,
                    data_load={
                        'status': status, 'tables_checked': 1,
                        'tables_nonempty': 0, 'tables_empty': 1,
                        'tables_failed': 0, 'total_rows': 0,
                    })
                self.assertEqual(report.status, 'opened')
                self.assertTrue(report.window_loaded)
                self.assertIsNone(report.screenshot)

    def test_verified_data_permits_report_capture(self):
        report = self._probe(
            _win(), [], verify_data=True,
            data_load={
                'status': 'verified', 'tables_checked': 1,
                'tables_nonempty': 1, 'tables_empty': 0,
                'tables_failed': 0, 'total_rows': 3,
            })
        self.assertTrue(report.window_loaded)
        self.assertTrue(report.screenshot)

    def test_a_missing_window_fails(self):
        # A living process with no matching title is not crash evidence.
        report = self._probe(None, ['no window titled ... before the timeout'])
        self.assertEqual(report.status, 'timed_out')
        self.assertFalse(report.window_loaded)
        self.assertIn('no window titled ... before the timeout', report.signals)
        self.assertIn('opening deadline', report.note)

    def test_timeout_preserves_window_match_reason_and_owned_pid_scope(self):
        reason = "window match: document title matched, but no responsive observation was obtained"
        report = self._probe(None, [reason], fallback=_win('Report - Power BI Desktop'))
        self.assertEqual(report.status, 'timed_out')
        self.assertIn(reason, report.note)
        self.assertTrue(any("restricted to launched PID 4242" in signal
                            for signal in report.signals))
        self.assertTrue(any("existing-instance handoff is not accepted" in signal
                            for signal in report.signals))
        self.assertFalse(report.window_loaded)

    def test_progress_diagnostics_do_not_turn_a_live_process_into_a_crash(self):
        signals = [
            "busy dialog: 'Processing Request'",
            "busy dialog: 'Computing View Layout'",
            "no document window appeared before the timeout",
        ]
        report = self._probe(None, signals)
        self.assertEqual(report.status, 'timed_out')
        self.assertFalse(report.opened)
        self.assertFalse(report.window_loaded)
        self.assertTrue(all(signal in report.signals for signal in signals))

    def test_clean_launcher_exit_and_nonzero_exit_are_different_failures(self):
        for rc, status in ((0, 'error'), (1, 'crashed')):
            with self.subTest(rc=rc):
                report = self._probe(None, ['no matching document title'], rc=rc)
                self.assertEqual(report.status, status)
                self.assertFalse(report.window_loaded)
                self.assertIsNone(report.window_title)
                self.assertTrue(any(f'rc={rc}' in signal
                                    for signal in report.signals))
                if rc == 0:
                    self.assertIn('no process crash established', report.note)
                else:
                    self.assertIn('non-zero', report.note)
                    self.assertNotIn('no process crash established', report.note)

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
                 mock.patch.object(desktop_window, 'window_process_id',
                                   return_value=4242), \
                 mock.patch.object(desktop_window, '_dialog_metadata',
                                   return_value={'status': 'observed'}), \
                 mock.patch.object(desktop_window, 'dialog_windows',
                                   return_value=[_win('Fields that need to be '
                                                      'fixed', owned=True)]):
                report = probe_desktop_open(_pbip(d), timeout=1)
        self.assertEqual(report.status, 'error')
        self.assertFalse(report.window_loaded)
        self.assertTrue(any('error dialog' in s for s in report.signals))
        self.assertIn('Fields that need to be fixed', report.dialogs)


def _dialog_metadata(**changes):
    return {
        'status': 'observed', 'texts': [], 'buttons': [], 'progress': 0,
        'interactive': 0, 'truncated': False, 'native_complete': True,
        **changes,
    }


class TestNativeProgressClassification(unittest.TestCase):
    def test_english_and_french_progress_titles_allow_waiting_not_attention(self):
        titles = (
            "Opening workbook 'Synthetic.twb'...",
            "Ouverture du classeur «Synthetic.twb»…",
            'Processing Request', 'Processing Request...',
            'Computing View Layout', 'Computing View Layout…',
            'Traitement de la demande...', 'Traitement de la requête…',
            'Traitement en cours', 'Running Query...', 'Executing Queries',
            'Exécution de la requête', 'Exécution des requêtes…',
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(desktop_window._classify_dialog_metadata(
                    title, _dialog_metadata()), 'busy')

    def test_native_progress_and_cancel_plus_progress_text_are_supported(self):
        cases = [
            _dialog_metadata(progress=1),
            _dialog_metadata(texts=['Loading view...'],
                             buttons=[{'name': 'Cancel'}]),
            _dialog_metadata(texts=['Calcul de la disposition de la vue…'],
                             buttons=[{'name': 'Annuler'}]),
            _dialog_metadata(texts=['Chargement des données…'],
                             buttons=[{'name': 'Localized cancellation',
                                       'control_id': 2}]),
        ]
        for metadata in cases:
            with self.subTest(metadata=metadata):
                self.assertEqual(desktop_window._classify_dialog_metadata(
                    'Synthetic Activity', metadata), 'busy')

    def test_cancel_alone_incomplete_unknown_or_interactive_is_not_progress(self):
        cases = [
            _dialog_metadata(),
            _dialog_metadata(buttons=[{'name': 'Cancel', 'control_id': 2}]),
            _dialog_metadata(status='timed_out', progress=1),
            _dialog_metadata(progress=1, native_complete=False),
            _dialog_metadata(progress=1, truncated=True),
            _dialog_metadata(progress=1, interactive=1),
            _dialog_metadata(progress=1, buttons=[{'name': 'OK'}]),
            _dialog_metadata(progress=1, buttons=[
                {'name': 'Cancel', 'control_id': 2}, {'name': 'Retry'}]),
            _dialog_metadata(status='ownership_changed', progress=1),
        ]
        for metadata in cases:
            with self.subTest(metadata=metadata):
                self.assertEqual(desktop_window._classify_dialog_metadata(
                    'Synthetic Activity', metadata), 'attention')

    def test_authentication_and_errors_dominate_a_progress_title_and_bar(self):
        for text in ('Sign in', 'Authentication required', 'Password',
                     'Authentification requise', 'Mot de passe', 'Identifiants',
                     'Error loading data', 'Erreur de chargement', 'Échec',
                     'License activation'):
            with self.subTest(text=text):
                metadata = _dialog_metadata(
                    progress=1, texts=[text],
                    buttons=[{'name': 'Cancel', 'control_id': 2}])
                self.assertEqual(desktop_window._classify_dialog_metadata(
                    'Processing Request', metadata), 'attention')

    def test_dialog_inspection_preserves_fractional_budget_and_exact_owner(self):
        dialog = _win('Computing View Layout', hwnd=31, owned=True)
        with mock.patch.object(desktop_window, 'IS_WINDOWS', True), \
                mock.patch.object(desktop_window, 'window_process_id',
                                  return_value=42) as owner, \
                mock.patch.object(desktop_window, '_dialog_metadata',
                                  return_value=_dialog_metadata()) as inspect:
            state = desktop_window.dialog_state(dialog, 42, timeout=.025)
        self.assertEqual(state['status'], 'busy')
        self.assertEqual(owner.call_args_list, [mock.call(31), mock.call(31)])
        self.assertEqual(state['owner_pid_before'], 42)
        self.assertEqual(state['owner_pid_after'], 42)
        inspect.assert_called_once_with(31, 42, .025)

    def test_wrong_owner_and_invalid_budget_never_query_accessibility(self):
        dialog = _win('Processing Request', hwnd=31, owned=True)
        with mock.patch.object(desktop_window, 'IS_WINDOWS', True), \
                mock.patch.object(desktop_window, 'window_process_id',
                                  return_value=99), \
                mock.patch.object(desktop_window, '_dialog_metadata') as inspect:
            state = desktop_window.dialog_state(dialog, 42, timeout=1)
            self.assertEqual(state['status'], 'attention')
            self.assertEqual(state['basis'], 'ownership_changed')
            for timeout in (0, -1, float('nan'), float('inf'), None):
                with self.subTest(timeout=timeout):
                    self.assertEqual(desktop_window.dialog_state(
                        dialog, 42, timeout=timeout)['status'], 'attention')
        inspect.assert_not_called()


class TestStrictNativeProgressClassification(unittest.TestCase):
    def test_require_progress_consumes_only_the_shared_complete_decision_not_busy_titles(self):
        title = 'Processing Request'
        layout = layout_dialog_metadata()
        layout['strict_classification'] = {
            'status': 'busy', 'basis': 'tableau_layout_timer', 'complete': True,
            'progress_required': True, 'reason': 'complete_owned_layout_timer_structure',
        }
        for metadata in (strict_dialog_metadata(), layout):
            with self.subTest(basis=metadata['strict_classification']['basis']):
                self.assertEqual(desktop_window._classify_dialog_metadata(
                    title, metadata, require_progress=True), 'busy')
        for strict in (
                None, {}, {'status': 'busy'}, {'basis': 'different', 'status': 'busy',
                                             'complete': True, 'progress_required': True},
                {'basis': 'shared_strict_uia', 'status': 'busy', 'complete': False,
                 'progress_required': True},
                {'basis': 'shared_strict_uia', 'status': 'busy', 'complete': True,
                 'progress_required': False},
                {'basis': 'shared_strict_uia', 'status': 'attention', 'complete': True,
                 'progress_required': True}):
            with self.subTest(strict=strict):
                self.assertEqual(desktop_window._classify_dialog_metadata(
                    title, strict_dialog_metadata(strict_classification=strict),
                    require_progress=True), 'attention')
        for metadata in (_dialog_metadata(), _dialog_metadata(
                buttons=[{'name': 'Cancel', 'control_id': 2}])):
            with self.subTest(metadata=metadata):
                self.assertEqual(desktop_window._classify_dialog_metadata(title, metadata), 'busy')
                self.assertEqual(desktop_window._classify_dialog_metadata(
                    title, metadata, require_progress=True), 'attention')

    def test_strict_inspection_preserves_budget_identity_and_rejects_partial_paint_or_recovery(self):
        dialog = desktop_window.WindowInfo(31, 'Processing Request', 'SyntheticModal',
                                           400, 300, True)
        with mock.patch.object(desktop_window, 'IS_WINDOWS', True), \
                mock.patch.object(desktop_window, 'window_process_id', return_value=42), \
                mock.patch.object(desktop_window, '_dialog_metadata') as inspect:
            metadata = strict_dialog_metadata()
            inspect.return_value = metadata
            state = desktop_window.dialog_state(dialog, 42, timeout=.025, require_progress=True)
            self.assertEqual(state['status'], 'busy')
            self.assertIs(state['metadata'], metadata)
            inspect.assert_called_once_with(31, 42, .025)
            for status in ('unavailable', 'identity_changed', 'timed_out'):
                with self.subTest(status=status):
                    metadata = strict_dialog_metadata(status=status, native_complete=False,
                        strict_classification={'status': 'attention', 'basis': 'shared_strict_uia',
                                               'complete': False, 'progress_required': True})
                    inspect.return_value = metadata
                    state = desktop_window.dialog_state(
                        dialog, 42, timeout=.025, require_progress=True)
                    self.assertEqual(state['status'], 'attention')
                    self.assertIs(state['metadata'], metadata)
            for phase in ('native_before', 'native_after'):
                with self.subTest(phase=phase):
                    metadata = strict_dialog_metadata()
                    metadata[phase]['class_name'] = 'OtherModal'
                    inspect.return_value = metadata
                    state = desktop_window.dialog_state(
                        dialog, 42, timeout=.025, require_progress=True)
                    self.assertEqual((state['status'], state['cause']),
                                     ('attention', 'dialog_identity_unverified'))
                    self.assertIs(state['retryable'], False)
                    self.assertIs(state['metadata'], metadata)
            inspect.reset_mock()
            recovery = dialog._replace(title='File Recovery')
            self.assertEqual(desktop_window.dialog_state(
                recovery, 42, timeout=.025, require_progress=True)['status'], 'attention')
            inspect.assert_not_called()


class _NativeWaitClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError('Negative wait')
        self.sleeps.append(seconds)
        self.now += seconds


class TestExitedDesktopLauncher(unittest.TestCase):
    """A synthetic process exit must stop inspection of its former PID."""

    def setUp(self):
        from powerbi_import import desktop_probe

        self.dp = desktop_probe
        self.clock = _NativeWaitClock()
        patches = (
            mock.patch.object(desktop_window, 'IS_WINDOWS', True),
            mock.patch.object(desktop_window.time, 'monotonic', self.clock.monotonic),
            mock.patch.object(desktop_window.time, 'sleep', self.clock.sleep),
            mock.patch.object(self.dp, 'find_pbi_desktop', return_value='PBIDesktop.exe'),
            mock.patch.object(self.dp, 'powershell_preflight',
                              return_value={'status': 'ready'}),
            mock.patch.object(self.dp, 'desktop_pids', return_value=[]),
            mock.patch.object(self.dp, '_DESKTOP_PIDS_QUERY_FAILED', False),
            mock.patch.object(self.dp, '_scan_trace_errors', return_value=[]),
            mock.patch.object(self.dp, '_recent_matches', return_value=[]),
            mock.patch.object(desktop_window, 'dialog_windows', return_value=[]),
            mock.patch.object(desktop_window, 'report_window', return_value=None),
            mock.patch.object(desktop_window, 'main_window', return_value=None),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_process_exit_stops_wait_at_next_observation(self):
        clock = self.clock

        class ExitsAfterTwoSeconds(_FakeProc):
            def poll(self):
                return 0 if clock.now >= 2 else None

        with tempfile.TemporaryDirectory() as directory:
            proc = ExitsAfterTwoSeconds()
            with mock.patch.object(self.dp.subprocess, 'Popen', return_value=proc), \
                    mock.patch.object(self.dp, '_capture_any') as capture_any, \
                    mock.patch.object(self.dp, '_capture_dialogs') as capture_dialogs:
                report = probe_desktop_open(
                    _pbip(directory), timeout=300, wait_for_window=True,
                    screenshot_path=os.path.join(directory, 'screen.png'))

        self.assertEqual(self.clock.now, 2)
        self.assertEqual(report.status, 'error')
        self.assertIn('exited', report.note)
        capture_any.assert_not_called()
        capture_dialogs.assert_not_called()

    def test_exit_after_window_wait_prevents_refresh_and_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = _FakeProc(rc=0)
            with mock.patch.object(self.dp.subprocess, 'Popen', return_value=proc), \
                    mock.patch.object(desktop_window, 'wait_for_window',
                                      return_value=(_win(), [])), \
                    mock.patch.object(desktop_window, 'window_process_id',
                                      return_value=proc.pid), \
                    mock.patch.object(self.dp, 'refresh_report') as refresh, \
                    mock.patch.object(self.dp, '_capture_dialogs') as capture_dialogs, \
                    mock.patch.object(desktop_window, 'capture_window') as capture:
                report = probe_desktop_open(
                    _pbip(directory), timeout=300, wait_for_window=True,
                    refresh=True, screenshot_path=os.path.join(directory, 'screen.png'))

        self.assertEqual(report.status, 'error')
        self.assertFalse(report.window_loaded)
        refresh.assert_not_called()
        capture_dialogs.assert_not_called()
        capture.assert_not_called()

    def test_exit_during_refresh_prevents_followup_capture(self):
        proc = _FakeProc()

        def finish_after_exit(*args, **kwargs):
            proc._rc = 0
            return {'status': 'verified', 'data_load': {'status': 'verified'}}

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(self.dp.subprocess, 'Popen', return_value=proc), \
                    mock.patch.object(desktop_window, 'wait_for_window',
                                      return_value=(_win(), [])), \
                    mock.patch.object(desktop_window, 'window_process_id',
                                      return_value=proc.pid), \
                    mock.patch.object(self.dp, 'refresh_report',
                                      side_effect=finish_after_exit), \
                    mock.patch.object(self.dp, '_capture_dialogs') as dialogs, \
                    mock.patch.object(desktop_window, 'capture_window') as capture:
                report = probe_desktop_open(
                    _pbip(directory), timeout=300, wait_for_window=True,
                    refresh=True, screenshot_path=os.path.join(directory, 'screen.png'))

        self.assertEqual(report.status, 'error')
        dialogs.assert_called_once()
        capture.assert_not_called()

    def test_render_wait_stops_before_capture_after_process_exit(self):
        with mock.patch.object(self.dp.time, 'time', self.clock.monotonic), \
                mock.patch.object(desktop_window, 'capture_window') as capture:
            self.dp._wait_for_render(
                11, 'synthetic.png', timeout=60, poll=2,
                stop_when=lambda: self.clock.now >= 2)

        self.assertEqual(self.clock.now, 2)
        capture.assert_not_called()

    def test_render_wait_never_captures_after_its_deadline(self):
        with mock.patch.object(desktop_window, 'capture_window') as capture:
            self.dp._wait_for_render(
                11, 'synthetic.png', timeout=1, poll=5)

        self.assertEqual(self.clock.now, 1)
        capture.assert_not_called()

    def test_clean_exit_does_not_hide_recent_crash_dump(self):
        proc = _FakeProc(rc=0)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(self.dp.subprocess, 'Popen', return_value=proc), \
                    mock.patch.object(self.dp, '_recent_matches',
                                      return_value=['synthetic FrownDump']), \
                    mock.patch.object(self.dp, '_capture_any') as capture_any, \
                    mock.patch.object(self.dp, '_capture_dialogs') as capture_dialogs:
                report = probe_desktop_open(
                    _pbip(directory), timeout=1, wait_for_window=True)

        self.assertEqual(report.status, 'crashed')
        self.assertTrue(any('FrownDump' in signal for signal in report.signals))
        self.assertIn('crash dump', report.note.lower())
        self.assertNotIn('no process crash established', report.note)
        capture_any.assert_not_called()
        capture_dialogs.assert_not_called()

    def test_exit_during_owner_verification_never_inspects_or_refreshes_window(self):
        proc = _FakeProc()

        def verify_owner(_hwnd):
            proc._rc = 0
            return proc.pid

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(self.dp.subprocess, 'Popen', return_value=proc), \
                    mock.patch.object(desktop_window, 'wait_for_window',
                                      return_value=(_win(), [])), \
                    mock.patch.object(desktop_window, 'window_process_id',
                                      side_effect=verify_owner), \
                    mock.patch.object(self.dp, '_capture_dialogs') as dialogs, \
                    mock.patch.object(self.dp, 'refresh_report') as refresh, \
                    mock.patch.object(desktop_window, 'capture_window') as capture:
                report = probe_desktop_open(
                    _pbip(directory), timeout=5, refresh=True,
                    screenshot_path=os.path.join(directory, 'screen.png'))

        self.assertEqual(report.status, 'error')
        self.assertFalse(report.window_loaded)
        dialogs.assert_not_called()
        refresh.assert_not_called()
        capture.assert_not_called()

    def test_exit_during_dialog_enumeration_prevents_ui_inspection(self):
        proc = _FakeProc()

        def enumerate_dialogs(_pid):
            proc._rc = 0
            return [_win('Synthetic dialog', owned=True)]

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(self.dp.subprocess, 'Popen', return_value=proc), \
                    mock.patch.object(desktop_window, 'dialog_windows',
                                      side_effect=enumerate_dialogs), \
                    mock.patch.object(desktop_window, 'dialog_state') as inspect, \
                    mock.patch.object(desktop_window, 'capture_window') as capture:
                report = probe_desktop_open(
                    _pbip(directory), timeout=5,
                    screenshot_path=os.path.join(directory, 'screen.png'))

        self.assertEqual(report.status, 'error')
        inspect.assert_not_called()
        capture.assert_not_called()

    def test_exit_during_fallback_window_enumeration_prevents_capture(self):
        proc = _FakeProc()

        def enumerate_main(_pid):
            proc._rc = 0
            return _win('Synthetic fallback')

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(self.dp.subprocess, 'Popen', return_value=proc), \
                    mock.patch.object(desktop_window, 'wait_for_window',
                                      return_value=(None, [])), \
                    mock.patch.object(desktop_window, 'main_window',
                                      side_effect=enumerate_main), \
                    mock.patch.object(desktop_window, 'capture_window') as capture:
                report = probe_desktop_open(
                    _pbip(directory), timeout=5,
                    screenshot_path=os.path.join(directory, 'screen.png'))

        self.assertEqual(report.status, 'error')
        capture.assert_not_called()

    def test_exit_during_refresh_baseline_prevents_refresh_and_apply_clicks(self):
        proc = _FakeProc()

        def baseline(*_args, **_kwargs):
            proc._rc = 0
            return self.dp._partition_observation_result('unavailable', 'synthetic')

        with mock.patch.object(self.dp, '_refresh_ui_state', return_value='clear'), \
                mock.patch.object(self.dp, 'observe_desktop_partitions',
                                  side_effect=baseline), \
                mock.patch.object(self.dp, '_invoke_schema_refresh') as schema, \
                mock.patch.object(self.dp, '_invoke_button') as invoke, \
                mock.patch.object(self.dp, '_click_button') as click:
            result = self.dp.refresh_report(
                11, 'synthetic.pbip', proc.pid, timeout=30,
                stop_when=lambda: proc.poll() is not None)

        self.assertEqual((result['status'], result['cause']),
                         ('error', 'owner_process_exited'))
        self.assertEqual(result['completion_basis'], 'none')
        self.assertEqual(result['clicks'], 0)
        schema.assert_not_called()
        invoke.assert_not_called()
        click.assert_not_called()

    def test_exit_after_refresh_click_prevents_further_actions(self):
        proc = _FakeProc()

        def request(*_args, **_kwargs):
            proc._rc = 0
            return 1

        with mock.patch.object(self.dp, '_refresh_ui_state', return_value='clear'), \
                mock.patch.object(
                    self.dp, 'observe_desktop_partitions',
                    return_value=self.dp._partition_observation_result(
                        'unavailable', 'synthetic')), \
                mock.patch.object(desktop_window, 'window_process_id',
                                  return_value=proc.pid), \
                mock.patch.object(self.dp, '_invoke_schema_refresh',
                                  side_effect=request), \
                mock.patch.object(self.dp, '_invoke_button') as invoke, \
                mock.patch.object(self.dp, '_click_button') as click, \
                mock.patch.object(self.dp, 'verify_desktop_data') as counts:
            result = self.dp.refresh_report(
                11, 'synthetic.pbip', proc.pid, timeout=30,
                stop_when=lambda: proc.poll() is not None)

        self.assertEqual((result['status'], result['cause']),
                         ('error', 'owner_process_exited'))
        self.assertEqual(result['clicks'], 1)
        invoke.assert_not_called()
        click.assert_not_called()
        counts.assert_not_called()

    def test_exit_during_refresh_ui_observation_prevents_next_native_query(self):
        proc = _FakeProc()

        def observe(*_args, **_kwargs):
            proc._rc = 0
            return {'status': 'clear'}

        with mock.patch.object(self.dp, '_run_uia', side_effect=observe) as uia, \
                mock.patch.object(self.dp, 'observe_desktop_partitions') as baseline, \
                mock.patch.object(self.dp, '_invoke_schema_refresh') as schema:
            result = self.dp.refresh_report(
                11, 'synthetic.pbip', proc.pid, timeout=30,
                stop_when=lambda: proc.poll() is not None)

        self.assertEqual((result['status'], result['cause']),
                         ('error', 'owner_process_exited'))
        self.assertEqual(uia.call_count, 1)
        baseline.assert_not_called()
        schema.assert_not_called()

    def test_reused_window_handle_blocks_refresh_action(self):
        proc = _FakeProc()
        with mock.patch.object(self.dp, '_refresh_ui_state', return_value='clear'), \
                mock.patch.object(
                    self.dp, 'observe_desktop_partitions',
                    return_value=self.dp._partition_observation_result(
                        'unavailable', 'synthetic')), \
                mock.patch.object(desktop_window, 'window_process_id',
                                  return_value=999), \
                mock.patch.object(self.dp, '_invoke_schema_refresh') as schema, \
                mock.patch.object(self.dp, '_invoke_button') as invoke, \
                mock.patch.object(self.dp, '_click_button') as click:
            result = self.dp.refresh_report(
                11, 'synthetic.pbip', proc.pid, timeout=30,
                stop_when=lambda: proc.poll() is not None)

        self.assertEqual((result['status'], result['cause']),
                         ('error', 'window_ownership_changed'))
        self.assertEqual(result['clicks'], 0)
        schema.assert_not_called()
        invoke.assert_not_called()
        click.assert_not_called()

    def test_exit_during_capture_never_publishes_screenshot(self):
        proc = _FakeProc()

        def capture_then_exit(_hwnd, path):
            proc._rc = 0
            return path

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(self.dp.subprocess, 'Popen', return_value=proc), \
                    mock.patch.object(desktop_window, 'wait_for_window',
                                      return_value=(_win(), [])), \
                    mock.patch.object(desktop_window, 'window_process_id',
                                      return_value=proc.pid), \
                    mock.patch.object(desktop_window, 'capture_window',
                                      side_effect=capture_then_exit):
                report = probe_desktop_open(
                    _pbip(directory), timeout=5,
                    screenshot_path=os.path.join(directory, 'screen.png'))

        self.assertEqual(report.status, 'error')
        self.assertIsNone(report.screenshot)

    def test_exit_after_verified_refresh_revokes_screenshot_data_evidence(self):
        proc = _FakeProc()

        def exit_during_render(*_args, **_kwargs):
            proc._rc = 0

        refresh_result = {
            'status': 'verified', 'clicks': 1,
            'data_load': {'status': 'verified'},
        }
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(self.dp.subprocess, 'Popen', return_value=proc), \
                    mock.patch.object(desktop_window, 'wait_for_window',
                                      return_value=(_win(), [])), \
                    mock.patch.object(desktop_window, 'window_process_id',
                                      return_value=proc.pid), \
                    mock.patch.object(self.dp, '_capture_dialogs'), \
                    mock.patch.object(self.dp, 'refresh_report',
                                      return_value=refresh_result), \
                    mock.patch.object(self.dp, '_wait_for_render',
                                      side_effect=exit_during_render), \
                    mock.patch.object(desktop_window, 'capture_window') as capture:
                report = probe_desktop_open(
                    _pbip(directory), timeout=5, verify_data=True, refresh=True,
                    screenshot_path=os.path.join(directory, 'screen.png'))

        self.assertEqual(report.status, 'error')
        self.assertIs(report.screenshot_data_verified, False)
        self.assertIsNone(report.screenshot)
        capture.assert_not_called()

    def test_reused_handle_revokes_screenshot_data_evidence(self):
        proc = _FakeProc()
        refresh_result = {
            'status': 'verified', 'clicks': 0,
            'data_load': {'status': 'verified'},
        }
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(self.dp.subprocess, 'Popen', return_value=proc), \
                    mock.patch.object(desktop_window, 'wait_for_window',
                                      return_value=(_win(), [])), \
                    mock.patch.object(desktop_window, 'window_process_id',
                                      side_effect=[proc.pid, 999]), \
                    mock.patch.object(self.dp, '_capture_dialogs'), \
                    mock.patch.object(self.dp, 'refresh_report',
                                      return_value=refresh_result), \
                    mock.patch.object(desktop_window, 'capture_window') as capture:
                report = probe_desktop_open(
                    _pbip(directory), timeout=5, verify_data=True, refresh=True,
                    screenshot_path=os.path.join(directory, 'screen.png'))

        self.assertEqual(report.status, 'error')
        self.assertIs(report.screenshot_data_verified, False)
        self.assertIsNone(report.screenshot)
        capture.assert_not_called()

    def test_owner_change_during_capture_revokes_screenshot_data_evidence(self):
        proc = _FakeProc()
        refresh_result = {
            'status': 'verified', 'clicks': 0,
            'data_load': {'status': 'verified'},
        }
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(self.dp.subprocess, 'Popen', return_value=proc), \
                    mock.patch.object(desktop_window, 'wait_for_window',
                                      return_value=(_win(), [])), \
                    mock.patch.object(desktop_window, 'window_process_id',
                                      side_effect=[proc.pid, proc.pid, 999]), \
                    mock.patch.object(self.dp, '_capture_dialogs'), \
                    mock.patch.object(self.dp, 'refresh_report',
                                      return_value=refresh_result), \
                    mock.patch.object(desktop_window, 'capture_window',
                                      return_value='synthetic.png') as capture:
                report = probe_desktop_open(
                    _pbip(directory), timeout=5, verify_data=True, refresh=True,
                    screenshot_path=os.path.join(directory, 'screen.png'))

        self.assertEqual(report.status, 'error')
        self.assertIs(report.screenshot_data_verified, False)
        self.assertIsNone(report.screenshot)
        capture.assert_called_once()

    def test_exit_after_window_watch_revokes_screenshot_data_evidence(self):
        proc = _FakeProc()

        def watch(report, *_args, **_kwargs):
            report.window_title = 'Synthetic'
            report.screenshot_data_verified = True
            proc._rc = 0
            return False

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(self.dp.subprocess, 'Popen', return_value=proc), \
                    mock.patch.object(self.dp, '_watch_window', side_effect=watch):
                report = probe_desktop_open(_pbip(directory), timeout=5)

        self.assertEqual(report.status, 'error')
        self.assertIs(report.screenshot_data_verified, False)

    def test_render_wait_rejects_reused_foreign_window_handle(self):
        with mock.patch.object(desktop_window, 'window_process_id',
                               return_value=999), \
                mock.patch.object(desktop_window, 'capture_window') as capture:
            self.dp._wait_for_render(
                11, 'synthetic.png', timeout=5, poll=1, owner_pid=4242)
        capture.assert_not_called()


class TestBoundedNativeDocumentWaits(unittest.TestCase):
    def setUp(self):
        self.clock = _NativeWaitClock()
        self.enterContext(mock.patch.object(desktop_window, 'IS_WINDOWS', True))
        self.enterContext(mock.patch.object(desktop_window.time, 'monotonic',
                                           side_effect=self.clock.monotonic))
        self.enterContext(mock.patch.object(desktop_window.time, 'sleep',
                                           side_effect=self.clock.sleep))
        self.owner = self.enterContext(mock.patch.object(
            desktop_window, 'window_process_id', return_value=42))
        self.dialogs = self.enterContext(mock.patch.object(
            desktop_window, 'dialog_windows', return_value=[]))
        self.inspect = self.enterContext(mock.patch.object(
            desktop_window, '_dialog_metadata', return_value=_dialog_metadata()))
        self.document = _win('Synthetic - Tableau', hwnd=11)
        self.report = self.enterContext(mock.patch.object(
            desktop_window, 'report_window', return_value=self.document))
        self.main_window = self.enterContext(mock.patch.object(
            desktop_window, 'main_window', return_value=self.document))
        self.responsive = self.enterContext(mock.patch.object(
            desktop_window, 'is_responsive', return_value=True))
        self.capture = self.enterContext(mock.patch.object(
            desktop_window, 'capture_window'))

    def wait(self, mode, timeout=.6, stable_for=.2):
        if mode == 'open':
            window, signals = desktop_window.wait_for_window(
                [42], 'Synthetic', timeout=timeout,
                stable_for=stable_for, poll=.1)
            return {'status': 'ready' if window else 'not_ready',
                    'signals': signals}
        return desktop_window.wait_for_document_ready(
            11, 42, timeout=timeout, stable_for=stable_for, poll=.1)

    def test_open_timeout_reports_title_response_and_stability_separately(self):
        scenarios = (
            (None, True, 'no document-titled candidate was observed'),
            (self.document, False, 'no responsive observation was obtained'),
            (self.document, True, 'did not remain stable before the deadline'),
        )
        for window, responsive, expected in scenarios:
            with self.subTest(expected=expected):
                self.clock.now = 0
                self.report.return_value = window
                self.responsive.return_value = responsive
                self.main_window.return_value = _win(
                    'Other - Power BI Desktop' if window is None else window.title)
                result = self.wait('open', timeout=.3, stable_for=.5)
                self.assertEqual(result['status'], 'not_ready')
                self.assertTrue(any(expected in signal for signal in result['signals']))
                self.assertAlmostEqual(self.clock.now, .3)
        self.capture.assert_not_called()

    def closing_popup(self, phase, *, metadata=None, remaining_owner=None,
                      title="Opening workbook 'Synthetic.twb'..."):
        """Deliver one popup after an initial stable-window observation."""
        self.clock.now = 0
        self.clock.sleeps.clear()
        for observed in (self.inspect, self.report, self.responsive):
            observed.reset_mock()
        popup = _win(title, hwnd=31, owned=True)
        observation = {'delivered': False, 'queried': False}
        if metadata is None:
            metadata = (_dialog_metadata(status='ownership_changed')
                        if phase == 'during' else _dialog_metadata(progress=1))

        def popups(pid):
            self.assertEqual(pid, 42)
            if self.clock.now >= .1 and not observation['delivered']:
                observation['delivered'] = True
                return [popup]
            return []

        def owner(hwnd):
            if hwnd == 11:
                return 42
            self.assertEqual(hwnd, popup.hwnd)
            if phase == 'before' or observation['queried']:
                return remaining_owner
            return 42

        def inspect(hwnd, pid, timeout):
            self.assertNotEqual(phase, 'before')
            self.assertEqual((hwnd, pid), (popup.hwnd, 42))
            self.assertGreater(timeout, 0)
            observation['queried'] = True
            self.clock.now += .025
            return metadata

        self.dialogs.side_effect = popups
        self.owner.side_effect = owner
        self.inspect.side_effect = inspect
        return popup, metadata

    def test_destroyed_popup_before_during_or_after_metadata_restarts_stability(self):
        for mode in ('open', 'exact'):
            for phase in ('before', 'during', 'after'):
                with self.subTest(mode=mode, phase=phase):
                    popup, _ = self.closing_popup(phase)
                    result = self.wait(mode, timeout=1)
                    self.assertEqual(result['status'], 'ready')
                    self.assertGreaterEqual(self.clock.now, .4)
                    self.assertGreaterEqual(self.responsive.call_count, 4)
                    self.assertTrue(any(popup.title in signal
                                        for signal in result['signals']))
                    self.assertFalse(any(signal.startswith('error dialog:')
                                         for signal in result['signals']))
                    if phase == 'before':
                        self.inspect.assert_not_called()
                    else:
                        self.inspect.assert_called_once()
        self.capture.assert_not_called()

    def test_destroyed_popup_is_wait_only_and_cannot_restart_the_deadline(self):
        for mode in ('open', 'exact'):
            for phase in ('before', 'during', 'after'):
                with self.subTest(mode=mode, phase=phase):
                    self.closing_popup(phase)
                    result = self.wait(mode, timeout=.3, stable_for=.25)
                    self.assertEqual(result['status'],
                                     'not_ready' if mode == 'open' else 'timed_out')
                    self.assertAlmostEqual(self.clock.now, .3)
                    self.assertFalse(any(signal.startswith('error dialog:')
                                         for signal in result['signals']))
        self.capture.assert_not_called()

    def test_live_foreign_popup_is_terminal_even_when_absent_from_owned_inventory(self):
        for mode in ('open', 'exact'):
            for phase in ('before', 'during', 'after'):
                with self.subTest(mode=mode, phase=phase):
                    popup, _ = self.closing_popup(phase, remaining_owner=99)
                    result = self.wait(mode, timeout=1)
                    self.assertEqual(result['status'],
                                     'not_ready' if mode == 'open' else 'inaccessible')
                    self.assertLess(self.clock.now, .2)
                    self.assertEqual(self.responsive.call_count, 1)
                    if mode == 'open':
                        self.assertTrue(any(
                            signal.startswith('error dialog:') and popup.title in signal
                            for signal in result['signals']))
                    else:
                        self.assertEqual(result['dialog']['basis'], 'ownership_changed')
        self.capture.assert_not_called()

    def test_observed_error_auth_and_trust_survive_popup_disappearance(self):
        for mode in ('open', 'exact'):
            for text in ('Error loading data', 'Authentication required',
                         'Trust certificate'):
                for evidence in ('title', 'metadata'):
                    with self.subTest(mode=mode, text=text, evidence=evidence):
                        snapshot = _dialog_metadata(
                            native_complete=False, progress=1, texts=[text])
                        popup, _ = self.closing_popup(
                            'before' if evidence == 'title' else 'after',
                            title=text if evidence == 'title'
                            else "Opening workbook 'Synthetic.twb'...",
                            metadata=snapshot)
                        result = self.wait(mode, timeout=1)
                        self.assertEqual(
                            result['status'],
                            'not_ready' if mode == 'open' else 'inaccessible')
                        self.assertLess(self.clock.now, .2)
                        self.assertEqual(self.responsive.call_count, 1)
                        self.assertTrue(any(popup.title in signal
                                            for signal in result['signals']))
                        if mode == 'exact':
                            self.assertIs(result['dialog']['explicit_attention'], True)
                            if evidence == 'metadata':
                                self.assertEqual(result['dialog']['metadata'], snapshot)
        self.capture.assert_not_called()

    def test_progress_disappearance_restarts_the_stable_interval(self):
        loading = _win('Processing Request', hwnd=31, owned=True)
        layout = _win('Computing View Layout', hwnd=32, owned=True)
        for mode in ('open', 'exact'):
            with self.subTest(mode=mode):
                self.clock.now = 0
                states = iter(([], [loading], [layout]))
                self.dialogs.side_effect = lambda pid: next(states, [])
                result = self.wait(mode, timeout=1)
                self.assertEqual(result['status'], 'ready')
                self.assertGreaterEqual(self.clock.now, .5)
                self.assertTrue(any('Processing Request' in signal
                                    for signal in result['signals']))
                self.assertTrue(any('Computing View Layout' in signal
                                    for signal in result['signals']))
        self.capture.assert_not_called()

    def test_persistent_incomplete_painting_uses_one_finite_deadline(self):
        self.dialogs.return_value = [
            _win('Computing View Layout', hwnd=31, owned=True)]
        self.inspect.return_value = _dialog_metadata(
            status='timed_out', native_complete=False)
        for mode in ('open', 'exact'):
            with self.subTest(mode=mode):
                self.clock.now = 0
                self.inspect.reset_mock()
                result = self.wait(mode, timeout=.35)
                self.assertEqual(result['status'],
                                 'not_ready' if mode == 'open' else 'timed_out')
                self.assertAlmostEqual(self.clock.now, .35)
                self.assertTrue(self.inspect.called)
                budgets = [call.args[2] for call in self.inspect.call_args_list]
                self.assertTrue(all(0 < budget <= .35 for budget in budgets))
                self.assertEqual(budgets, sorted(budgets, reverse=True))
                self.assertFalse(any(signal.startswith('error dialog:')
                                     for signal in result['signals']))
        self.report.assert_not_called()
        self.responsive.assert_not_called()
        self.capture.assert_not_called()

    def test_inspection_consuming_the_budget_cannot_be_followed_by_readiness(self):
        self.dialogs.return_value = [
            _win('Processing Request', hwnd=31, owned=True)]

        def consume_budget(_hwnd, _pid, timeout):
            self.clock.now += timeout
            return _dialog_metadata(status='timed_out', native_complete=False)

        self.inspect.side_effect = consume_budget
        for mode in ('open', 'exact'):
            with self.subTest(mode=mode):
                self.clock.now = 0
                self.inspect.reset_mock()
                result = self.wait(mode, timeout=.025, stable_for=0)
                self.assertNotEqual(result['status'], 'ready')
                self.assertAlmostEqual(self.clock.now, .025)
                self.inspect.assert_called_once_with(31, 42, .025)
        self.responsive.assert_not_called()
        self.capture.assert_not_called()

    def test_auth_error_and_mixed_dialogs_never_prove_document_readiness(self):
        busy = _win('Processing Request', hwnd=31, owned=True)
        cases = [
            (_win('Sign in', hwnd=32, owned=True), _dialog_metadata()),
            (_win('Erreur', hwnd=32, owned=True), _dialog_metadata()),
            (_win('Computing View Layout', hwnd=32, owned=True),
             _dialog_metadata(progress=1, texts=['Mot de passe'])),
            (_win('Synthetic Dialog', hwnd=32, owned=True),
             _dialog_metadata(progress=1, buttons=[{'name': 'Retry'}])),
        ]
        for dialog, metadata in cases:
            for mode in ('open', 'exact'):
                with self.subTest(title=dialog.title, mode=mode):
                    self.clock.now = 0
                    self.dialogs.return_value = [busy, dialog]
                    self.inspect.side_effect = (
                        lambda hwnd, pid, timeout, data=metadata:
                        data if hwnd == 32 else _dialog_metadata())
                    result = self.wait(mode)
                    self.assertEqual(result['status'],
                                     'not_ready' if mode == 'open'
                                     else 'inaccessible')
                    self.assertEqual(self.clock.now, 0)
                    self.assertTrue(any(dialog.title in signal
                                        for signal in result['signals']))
        self.responsive.assert_not_called()
        self.capture.assert_not_called()

    def test_exact_handle_ownership_change_cannot_borrow_a_ready_window(self):
        self.owner.return_value = 99
        result = self.wait('exact')
        self.assertEqual(result['status'], 'error')
        self.assertIn('ownership changed', result['note'])
        self.dialogs.assert_not_called()
        self.responsive.assert_not_called()
        self.capture.assert_not_called()


class TestErrorDialogEndsTheWait(unittest.TestCase):
    """A popup is what makes a project unusable, so it must end the wait at
    once rather than let the probe sit out its timeout."""

    def _wait(self, dialogs, report=None, timeout=4):
        clock = _NativeWaitClock()
        with mock.patch.object(desktop_window, 'IS_WINDOWS', True), \
             mock.patch.object(desktop_window, 'window_process_id', return_value=1), \
             mock.patch.object(desktop_window, '_dialog_metadata',
                               return_value=_dialog_metadata()), \
             mock.patch.object(desktop_window, 'dialog_windows',
                               return_value=dialogs), \
             mock.patch.object(desktop_window, 'report_window',
                               return_value=report), \
             mock.patch.object(desktop_window, 'main_window',
                               return_value=None), \
             mock.patch.object(desktop_window, 'is_responsive',
                               return_value=True), \
             mock.patch.object(desktop_window.time, 'monotonic',
                               side_effect=clock.monotonic), \
             mock.patch.object(desktop_window.time, 'sleep', side_effect=clock.sleep):
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

    def test_data_load_is_aggregate_only_and_redacted(self):
        report = DesktopProbeReport(
            pbip_path='x.pbip',
            data_load={
                'status': 'verified', 'tables_checked': 2,
                'tables_nonempty': 2, 'tables_empty': 0,
                'tables_failed': 0, 'total_rows': 37,
            })
        data = report.to_dict()
        self.assertEqual(set(data['data_load']), {
            'status', 'tables_checked', 'tables_nonempty', 'tables_empty',
            'tables_failed', 'total_rows',
        })
        rendered = json.dumps(data)
        for sensitive in ('Customers', 'secret value', 'exception detail'):
            self.assertNotIn(sensitive, rendered)


class TestDataVerificationFailsClosed(unittest.TestCase):

    def setUp(self):
        self.enterContext(mock.patch.object(
            accessibility, 'sys', SimpleNamespace(platform='win32')))
        self.resolver = self.enterContext(mock.patch.object(
            accessibility, 'resolve_powershell_executable',
            return_value='/__synthetic_native_tooling__/powershell.exe'))

    def _verify(self, payload, tables=('Customers',)):
        completed = mock.Mock(returncode=0, stdout=json.dumps(payload))
        with mock.patch('powerbi_import.desktop_probe._m_backed_tables',
                        return_value=list(tables)), \
             mock.patch.object(desktop_window, 'IS_WINDOWS', True), \
             mock.patch.object(accessibility.subprocess, 'run',
                               return_value=completed) as child:
            result = verify_desktop_data('Report.pbip', 4242)
        child.assert_called_once()
        command = child.call_args.args[0]
        self.assertEqual(command[:3], [
            '/__synthetic_native_tooling__/powershell.exe', '-NoProfile', '-NonInteractive'])
        self.assertEqual(
            json.loads(base64.b64decode(child.call_args.kwargs['input']).decode('utf-8')),
            {'owner_pid': 4242, 'tables': list(tables)})
        self.assertGreater(child.call_args.kwargs['timeout'], 0)
        self.assertLessEqual(child.call_args.kwargs['timeout'], 60)
        return result

    def test_malformed_or_failed_results_are_not_verified(self):
        malformed = {
            'status': 'verified', 'tables_checked': 1,
            'tables_nonempty': 1, 'tables_empty': 0,
            'tables_failed': 0, 'total_rows': 0,
            'table': 'Customers', 'error': 'secret value',
        }
        failed = {
            'status': 'query_failed', 'tables_checked': 1,
            'tables_nonempty': 0, 'tables_empty': 0,
            'tables_failed': 1, 'total_rows': 0,
            'table': 'Customers', 'error': 'exception detail',
        }
        self.assertEqual(self._verify(malformed)['status'], 'unavailable')
        result = self._verify(failed)
        self.assertEqual(result['status'], 'query_failed')
        self.assertNotEqual(result['status'], 'verified')
        self.assertNotIn('Customers', json.dumps(result))
        self.assertNotIn('exception detail', json.dumps(result))

    def _verify_two(self, payload):
        return self._verify(payload, tables=('Customers', 'Orders'))

    def test_one_empty_table_is_partial_not_verified(self):
        partial = {'status': 'partial', 'tables_checked': 2,
                   'tables_nonempty': 1, 'tables_empty': 1,
                   'tables_failed': 0, 'total_rows': 5}
        self.assertEqual(self._verify_two(partial)['status'], 'partial')
        claimed = dict(partial, status='verified')
        self.assertEqual(self._verify_two(claimed)['status'], 'unavailable')

    def test_all_tables_with_rows_is_verified(self):
        full = {'status': 'verified', 'tables_checked': 2,
                'tables_nonempty': 2, 'tables_empty': 0,
                'tables_failed': 0, 'total_rows': 9}
        self.assertEqual(self._verify_two(full)['status'], 'verified')

    def test_missing_or_nonwindows_tooling_is_terminal_without_running_a_query(self):
        for platform, executable, cause in (
                ('win32', None, 'powershell_not_found'),
                ('linux', None, 'windows_required'),
                ('linux', '/__synthetic_native_tooling__/powershell.exe', 'windows_required')):
            with self.subTest(platform=platform, executable=executable):
                self.resolver.reset_mock()
                self.resolver.return_value = executable
                with mock.patch.object(accessibility, 'sys', SimpleNamespace(platform=platform)), \
                        mock.patch('powerbi_import.desktop_probe._m_backed_tables',
                                   return_value=['Customers']), \
                        mock.patch.object(desktop_window, 'IS_WINDOWS', True), \
                        mock.patch.object(accessibility.subprocess, 'run') as child:
                    result = verify_desktop_data('Report.pbip', 4242)
                self.assertEqual((result['status'], result['cause']), ('tooling_unavailable', cause))
                self.assertEqual(result['note'], cause)
                self.assertIs(result['retryable'], False)
                for key in ('tables_checked', 'tables_nonempty', 'tables_empty',
                            'tables_failed', 'total_rows'):
                    self.assertEqual(result[key], 0)
                child.assert_not_called()
                if platform == 'win32':
                    self.resolver.assert_called_once_with()
                else:
                    self.resolver.assert_not_called()


if __name__ == '__main__':
    unittest.main()
