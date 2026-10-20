"""Tableau Desktop diagnostics with no GUI or real process launches."""
import itertools
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from powerbi_import import desktop_window
from powerbi_import import tableau_desktop_probe as probe


def window(title="Sales - Tableau", hwnd=11, **kwargs):
    return desktop_window.WindowInfo(
        hwnd=hwnd, title=title, class_name="Tableau",
        width=kwargs.get("width", 1200), height=kwargs.get("height", 800),
        owned=kwargs.get("owned", False))


class TestTableauDiscovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = self.enterContext(mock.patch.dict(os.environ, {}, clear=True))
        self.which = self.enterContext(mock.patch.object(probe.shutil, "which", return_value=None))

    def exe(self, relative):
        path = self.root / relative / "tableau.exe"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def test_missing_installation(self):
        self.assertIsNone(probe.find_tableau_desktop())

    def test_explicit_override_wins(self):
        chosen = self.exe("chosen")
        os.environ["TABLEAU_DESKTOP_PATH"] = str(self.exe("environment"))
        self.assertEqual(probe.find_tableau_desktop(str(chosen)), str(chosen.resolve()))
        self.which.assert_not_called()

    def test_invalid_override_never_falls_back(self):
        self.which.return_value = str(self.exe("fallback"))
        for invalid in (self.root / "missing.exe", self.root, self.root / "wrong.exe"):
            if invalid.suffix == ".exe" and invalid.name == "wrong.exe":
                invalid.touch()
            with self.subTest(path=invalid):
                self.assertIsNone(probe.find_tableau_desktop(str(invalid)))
        self.which.assert_not_called()

    def test_invalid_environment_override_never_falls_back(self):
        os.environ["TABLEAU_DESKTOP_PATH"] = str(self.root / "missing.exe")
        self.which.return_value = str(self.exe("fallback"))
        self.assertIsNone(probe.find_tableau_desktop())
        self.which.assert_not_called()

    def test_path_installation(self):
        path = self.exe("path")
        self.which.return_value = str(path)
        self.assertEqual(probe.find_tableau_desktop(), str(path.resolve()))

    def test_program_files_selects_numeric_newest_across_roots(self):
        os.environ["ProgramFiles"] = str(self.root / "pf")
        os.environ["ProgramW6432"] = str(self.root / "pf64")
        self.exe("pf/Tableau/Tableau 2025.9/bin")
        self.exe("pf/Tableau/Tableau 2024.12/bin")
        newest = self.exe("pf64/Tableau/Tableau 2025.10/bin")
        self.assertEqual(probe.find_tableau_desktop(), str(newest.resolve()))


class TestDocumentTitle(unittest.TestCase):
    def test_document_specific_titles(self):
        for title in ("Sales", "Sales.twb", "Sales.twbx", "Sales - Tableau",
                      "Tableau — Sales", "Sales* – Tableau", " sales - tableau "):
            with self.subTest(title=title):
                self.assertTrue(probe.document_title_matches(title, "Sales"))

    def test_similar_or_generic_titles_do_not_match(self):
        for title in ("Sales2 - Tableau", "Tableau", "Sales dashboard - Tableau",
                      "Other - Tableau", ""):
            with self.subTest(title=title):
                self.assertFalse(probe.document_title_matches(title, "Sales"))

    def test_dash_inside_document_name_is_not_split(self):
        self.assertTrue(probe.document_title_matches("Sales - East - Tableau", "Sales - East"))
        self.assertFalse(probe.document_title_matches("Sales - East - Tableau", "Sales"))


class TestTableauCapture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "Sales.twb"
        self.source.write_text("<workbook/>", encoding="utf-8")
        self.shot = self.root / "captures" / "Sales.png"
        self.proc = mock.Mock(pid=4242, returncode=None)
        self.proc.poll.return_value = None
        self.launch = self.enterContext(mock.patch.object(
            probe.subprocess, "Popen", return_value=self.proc))
        self.find = self.enterContext(mock.patch.object(
            probe, "find_tableau_desktop", return_value="C:/Tableau/tableau.exe"))
        self.enterContext(mock.patch.object(desktop_window, "IS_WINDOWS", True))
        self.windows = self.enterContext(mock.patch.object(
            desktop_window, "windows_for_pid", return_value=[window()]))
        self.dialogs = self.enterContext(mock.patch.object(
            desktop_window, "dialog_windows", return_value=[]))
        self.responsive = self.enterContext(mock.patch.object(
            desktop_window, "is_responsive", return_value=True))
        self.capture = self.enterContext(mock.patch.object(
            desktop_window, "capture_window", side_effect=self.save))
        self.blank = self.enterContext(mock.patch.object(
            desktop_window, "is_blank", return_value=False))
        ticks = itertools.count()
        self.enterContext(mock.patch.object(
            probe.time, "monotonic", side_effect=lambda: next(ticks) * .1))
        self.enterContext(mock.patch.object(probe.time, "sleep"))

    def save(self, hwnd, path):
        Path(path).write_bytes(b"mock screenshot")
        return True

    def run_probe(self, **kwargs):
        options = dict(timeout=2, stable_for=0, poll=.1)
        options.update(kwargs)
        result = probe.probe_tableau_open(
            str(self.source), screenshot_path=str(self.shot), **options)
        self.proc.terminate.assert_not_called()
        self.proc.kill.assert_not_called()
        self.proc.wait.assert_not_called()
        return result

    def test_success_is_pid_scoped_and_never_closes_process(self):
        result = self.run_probe(desktop_path="override")
        self.assertEqual(result.status, "captured")
        self.assertEqual(result.screenshot, str(self.shot))
        self.assertFalse(result.screenshot_blank)
        self.assertEqual(result.pid, 4242)
        self.launch.assert_called_once_with(
            ["C:/Tableau/tableau.exe", str(self.source.resolve())], shell=False)
        self.find.assert_called_once_with("override")
        self.windows.assert_called_once_with(4242)
        self.dialogs.assert_called_once_with(4242)
        self.capture.assert_called_once_with(11, str(self.shot))
        self.assertIn("not proof", result.limitation)
        self.assertEqual(result.to_dict()["status"], "captured")

    def test_packaged_workbook_supported(self):
        self.source = self.source.with_suffix(".TWBX")
        self.source.touch()
        self.assertEqual(self.run_probe().status, "captured")

    def test_invalid_source_or_destination_never_launches(self):
        for suffix in (".tds", ".tdsx", ".hyper", ".pbip"):
            with self.subTest(suffix=suffix):
                self.source = self.root / ("Sales" + suffix)
                self.source.touch()
                self.assertEqual(self.run_probe().status, "error")
        self.source = self.root / "missing.twb"
        self.assertEqual(self.run_probe().status, "error")
        self.source = self.root / "Sales.twb"
        self.shot = self.root / "shot.jpg"
        self.assertEqual(self.run_probe().status, "error")
        self.launch.assert_not_called()

    def test_stale_destination_is_rejected_without_launch(self):
        self.shot.parent.mkdir()
        self.shot.write_bytes(b"old evidence")
        result = self.run_probe()
        self.assertEqual(result.status, "error")
        self.assertIsNone(result.screenshot)
        self.assertEqual(self.shot.read_bytes(), b"old evidence")
        self.launch.assert_not_called()

    def test_invalid_timing_never_launches(self):
        for args in ({"timeout": 0}, {"timeout": -1}, {"poll": 0}, {"stable_for": -1},
                     {"timeout": float("nan")}, {"timeout": float("inf")},
                     {"stable_for": float("nan")}, {"poll": float("inf")}):
            with self.subTest(args=args):
                self.assertEqual(self.run_probe(**args).status, "error")
        self.launch.assert_not_called()

    def test_linked_destination_fails_before_launch(self):
        with mock.patch.object(Path, "is_junction", return_value=True):
            result = self.run_probe()
        self.assertEqual(result.status, "error")
        self.assertIn("link or junction", result.note)
        self.launch.assert_not_called()

    def test_missing_app_and_nonwindows_fail_closed(self):
        self.find.return_value = None
        self.assertEqual(self.run_probe().status, "unavailable")
        with mock.patch.object(desktop_window, "IS_WINDOWS", False):
            self.assertEqual(self.run_probe().status, "unavailable")
        self.launch.assert_not_called()

    def test_forwarding_to_existing_instance_is_not_success(self):
        self.windows.return_value = []
        self.proc.poll.return_value = 0
        self.proc.returncode = 0
        result = self.run_probe()
        self.assertEqual(result.status, "error")
        self.assertIn("handoff", result.note)
        self.assertIsNone(result.screenshot)
        self.capture.assert_not_called()
        self.assertTrue(all(call.args == (4242,) for call in self.windows.call_args_list))

    def test_wrong_owned_or_small_windows_timeout(self):
        for win in (window("Sales2 - Tableau"), window(owned=True), window(width=399),
                    window(height=299)):
            with self.subTest(window=win):
                self.windows.return_value = [win]
                self.assertEqual(self.run_probe().status, "timed_out")
        self.capture.assert_not_called()

    def test_unresponsive_document_times_out(self):
        self.responsive.return_value = False
        self.assertEqual(self.run_probe().status, "timed_out")
        self.capture.assert_not_called()

    def test_response_must_remain_stable_not_just_once(self):
        self.responsive.side_effect = itertools.cycle((True, False))
        result = self.run_probe(stable_for=.5)
        self.assertEqual(result.status, "timed_out")
        self.capture.assert_not_called()

    def test_transient_owned_loading_dialog_waits_then_captures(self):
        loading = window("Opening workbook 'Sales.twb'...", owned=True)
        self.dialogs.side_effect = ([loading], [], [])

        result = self.run_probe()

        self.assertEqual(result.status, "captured")
        self.assertEqual(result.screenshot, str(self.shot))
        self.assertIsNone(result.dialog_screenshot)
        self.assertIn(loading.title, result.signals)
        self.capture.assert_called_once_with(11, str(self.shot))

    def test_persistent_owned_loading_dialog_times_out_without_capture(self):
        self.dialogs.return_value = [
            window("Opening workbook 'Sales.twb'...", owned=True)]

        result = self.run_probe(timeout=.5)

        self.assertEqual(result.status, "timed_out")
        self.assertIsNone(result.screenshot)
        self.assertIsNone(result.dialog_screenshot)
        self.assertIn("Opening workbook 'Sales.twb'...", result.signals)
        self.capture.assert_not_called()
        self.windows.assert_not_called()

    def test_loading_dialog_disappearance_resets_stability_period(self):
        loading = window("Opening workbook 'Sales.twb'...", owned=True)
        dialogs = iter(([], [loading]))
        self.dialogs.side_effect = lambda pid: next(dialogs, [])

        result = self.run_probe(stable_for=.5)

        self.assertEqual(result.status, "captured")
        self.assertGreaterEqual(self.windows.call_count, 4)
        self.assertIn(loading.title, result.signals)
        self.capture.assert_called_once_with(11, str(self.shot))

    def test_ambiguous_documents_and_owned_dialogs_fail(self):
        self.windows.return_value = [window(), window(hwnd=12)]
        self.assertEqual(self.run_probe().status, "error")
        dialog = window("Sign in", hwnd=21, owned=True)
        self.dialogs.return_value = [dialog]
        result = self.run_probe()
        self.assertEqual(result.status, "error")
        self.assertIn("Sign in", result.signals)
        self.assertIn("manual attention", result.note)
        self.assertIsNone(result.screenshot)
        self.assertEqual(result.dialog_screenshot,
                         str(self.shot.with_name("Sales-dialog.png")))
        self.assertFalse(self.shot.exists())
        self.capture.assert_called_once_with(
            dialog.hwnd, str(self.shot.with_name("Sales-dialog.png")))

    def test_stale_dialog_screenshot_destination_is_rejected(self):
        self.shot.parent.mkdir(parents=True)
        dialog_shot = self.shot.with_name("Sales-dialog.png")
        dialog_shot.write_bytes(b"old dialog evidence")
        self.dialogs.return_value = [window("Sign in", hwnd=21, owned=True)]

        result = self.run_probe()

        self.assertEqual(result.status, "error")
        self.assertIn("dialog screenshot destination", result.note)
        self.assertIsNone(result.screenshot)
        self.assertIsNone(result.dialog_screenshot)
        self.assertFalse(self.shot.exists())
        self.assertEqual(dialog_shot.read_bytes(), b"old dialog evidence")
        self.capture.assert_not_called()

    def test_blank_or_unreadable_image_is_not_capture(self):
        for blank in (True, None):
            with self.subTest(blank=blank):
                self.shot.unlink(missing_ok=True)
                self.blank.return_value = blank
                result = self.run_probe()
                self.assertEqual(result.status, "error")
                self.assertIsNone(result.screenshot)

    def test_failed_capture_even_with_partial_file_is_not_capture(self):
        def partial(hwnd, path):
            Path(path).write_bytes(b"partial")
            return False
        self.capture.side_effect = partial
        result = self.run_probe()
        self.assertEqual(result.status, "error")
        self.assertIsNone(result.screenshot)
        self.blank.assert_not_called()

    def test_capture_exception_is_reported(self):
        self.capture.side_effect = OSError("capture denied")
        result = self.run_probe()
        self.assertEqual(result.status, "error")
        self.assertIn("capture denied", result.note)
        self.assertIsNone(result.screenshot)

    def test_launch_error_is_reported(self):
        self.launch.side_effect = OSError("launch denied")
        result = self.run_probe()
        self.assertEqual(result.status, "error")
        self.assertIn("launch denied", result.note)
