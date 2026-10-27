"""Tests for the best-effort Power BI Desktop open self-check (desktop_probe.py).

All Desktop launches are mocked — no real GUI is spawned.
"""

import json
import os
import re
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from powerbi_import.desktop_probe import (
    DesktopProbeReport,
    find_pbi_desktop,
    probe_desktop_open,
    probe_desktop_reopen,
)


def _pbip(root):
    p = os.path.join(root, "P.pbip")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"version": "1.0"}')
    return p


class _FakeProc:
    """Minimal subprocess.Popen stand-in."""
    def __init__(self, pid=4242, poll_seq=None):
        self.pid = pid
        self._seq = list(poll_seq or [None])
        self._i = 0
        self.terminated = False
        self.killed = False

    def poll(self):
        v = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return v

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class TestFind(unittest.TestCase):
    def test_returns_none_when_absent(self):
        with mock.patch.dict(os.environ, {"ProgramFiles": "", "ProgramW6432": "",
                                          "ProgramFiles(x86)": ""}, clear=False):
            self.assertIsNone(find_pbi_desktop())

    def test_accepts_explicit_path(self):
        path = r"C:\pbi\PBIDesktop.exe"
        with mock.patch("powerbi_import.desktop_probe.os.path.isfile", return_value=True):
            self.assertEqual(find_pbi_desktop(path), path)


class TestProbe(unittest.TestCase):
    def test_missing_pbip_errors(self):
        r = probe_desktop_open(os.path.join(tempfile.gettempdir(), "nope.pbip"))
        self.assertEqual(r.status, "error")
        self.assertFalse(r.opened)

    def test_desktop_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("powerbi_import.desktop_probe.find_pbi_desktop",
                            return_value=None):
                r = probe_desktop_open(_pbip(d))
        self.assertEqual(r.status, "unavailable")
        self.assertFalse(r.opened)
        self.assertIn("authoritative", r.note)

    def test_opened_when_process_stays_alive(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("powerbi_import.desktop_probe.find_pbi_desktop",
                            return_value="pbi.exe"), \
                 mock.patch("powerbi_import.desktop_probe.subprocess.Popen",
                            return_value=_FakeProc(poll_seq=[None])), \
                 mock.patch("powerbi_import.desktop_probe._recent_matches",
                            return_value=[]), \
                 mock.patch("powerbi_import.desktop_probe._scan_trace_errors",
                            return_value=[]), \
                 mock.patch("powerbi_import.desktop_probe.time.sleep"):
                r = probe_desktop_open(_pbip(d), settle=0, timeout=5,
                                       wait_for_window=False)
        self.assertEqual(r.status, "opened")
        self.assertTrue(r.opened)
        self.assertEqual(r.pid, 4242)

    def test_crashed_on_early_exit(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("powerbi_import.desktop_probe.find_pbi_desktop",
                            return_value="pbi.exe"), \
                 mock.patch("powerbi_import.desktop_probe.subprocess.Popen",
                            return_value=_FakeProc(poll_seq=[1])), \
                 mock.patch("powerbi_import.desktop_probe._recent_matches",
                            return_value=[]), \
                 mock.patch("powerbi_import.desktop_probe._scan_trace_errors",
                            return_value=[]), \
                 mock.patch("powerbi_import.desktop_probe.time.sleep"):
                r = probe_desktop_open(_pbip(d), settle=30, timeout=60,
                                       wait_for_window=False)
        self.assertEqual(r.status, "crashed")
        self.assertFalse(r.opened)
        self.assertTrue(any("exited early" in s for s in r.signals))

    def test_crashed_on_frowndump(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("powerbi_import.desktop_probe.find_pbi_desktop",
                            return_value="pbi.exe"), \
                 mock.patch("powerbi_import.desktop_probe.subprocess.Popen",
                            return_value=_FakeProc(poll_seq=[None, None])), \
                 mock.patch("powerbi_import.desktop_probe._recent_matches",
                            return_value=[r"C:\FrownDump1"]), \
                 mock.patch("powerbi_import.desktop_probe._scan_trace_errors",
                            return_value=[]), \
                 mock.patch("powerbi_import.desktop_probe.time.sleep"):
                r = probe_desktop_open(_pbip(d), settle=30, timeout=60,
                                       wait_for_window=False)
        self.assertEqual(r.status, "crashed")
        self.assertTrue(any("FrownDump" in s for s in r.signals))

    def test_trace_error_blocks_opened_status(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("powerbi_import.desktop_probe.find_pbi_desktop",
                            return_value="pbi.exe"), \
                 mock.patch("powerbi_import.desktop_probe.subprocess.Popen",
                            return_value=_FakeProc(poll_seq=[None])), \
                 mock.patch("powerbi_import.desktop_probe._recent_matches",
                            return_value=[]), \
                 mock.patch("powerbi_import.desktop_probe._scan_trace_errors",
                            return_value=["error tokens in trace Desktop.log"]), \
                 mock.patch("powerbi_import.desktop_probe.time.sleep"):
                r = probe_desktop_open(_pbip(d), settle=0, timeout=5,
                                       wait_for_window=False)
        self.assertEqual(r.status, "crashed")
        self.assertFalse(r.opened)

    def test_launch_failure_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("powerbi_import.desktop_probe.find_pbi_desktop",
                            return_value="pbi.exe"), \
                 mock.patch("powerbi_import.desktop_probe.subprocess.Popen",
                            side_effect=OSError("boom")):
                r = probe_desktop_open(_pbip(d))
        self.assertEqual(r.status, "error")

    def test_to_dict_includes_opened(self):
        r = DesktopProbeReport(pbip_path="x", status="opened")
        d = r.to_dict()
        self.assertTrue(d["opened"])
        self.assertEqual(d["status"], "opened")

    def test_reopen_requires_two_healthy_launches(self):
        first = DesktopProbeReport(pbip_path='P.pbip', status='opened')
        second = DesktopProbeReport(pbip_path='P.pbip', status='opened')
        with mock.patch('powerbi_import.desktop_probe.probe_desktop_open',
                        side_effect=[first, second]):
            result = probe_desktop_reopen('P.pbip')
        self.assertEqual(result.status, 'reopened')

    def test_reopen_returns_initial_failure(self):
        first = DesktopProbeReport(pbip_path='P.pbip', status='crashed')
        with mock.patch('powerbi_import.desktop_probe.probe_desktop_open',
                        return_value=first):
            result = probe_desktop_reopen('P.pbip')
        self.assertEqual(result.status, 'crashed')


class TestReportDefinitionLaunchOption(unittest.TestCase):
    """Diagnostic launch selection only; every process/window call is mocked."""

    def test_default_and_explicit_launch_keep_distinct_targets_and_evidence(self):
        from powerbi_import import desktop_probe as dp
        with tempfile.TemporaryDirectory() as root:
            pbip = _pbip(root)
            report_dir = os.path.join(root, "Synthetic.Report")
            os.makedirs(report_dir)
            definition = os.path.join(report_dir, "definition.pbir")
            with open(definition, "w", encoding="utf-8") as stream:
                json.dump({"version": "4.0"}, stream)
            with open(pbip, "w", encoding="utf-8") as stream:
                json.dump({"artifacts": [{"report": {"path": "Synthetic.Report"}}]}, stream)
            for explicit in (False, True):
                with self.subTest(explicit=explicit), \
                        mock.patch.object(dp, "find_pbi_desktop", return_value="pbi.exe"), \
                        mock.patch.object(dp, "desktop_pids", return_value=[]), \
                        mock.patch.object(dp.subprocess, "Popen",
                                          return_value=_FakeProc()) as launch, \
                        mock.patch.object(dp, "_watch_process", return_value=False), \
                        mock.patch.object(dp, "_scan_trace_errors", return_value=[]):
                    report = dp.probe_desktop_open(
                        pbip, wait_for_window=False, close_after=False,
                        launch_report_definition=explicit,
                    )
                if explicit:
                    launch.assert_called_once_with(
                        ["pbi.exe", os.path.realpath(definition)], cwd=root
                    )
                    self.assertIn("does not verify PBIP launching",
                                  "\n".join(report.to_dict()["warnings"]))
                else:
                    launch.assert_called_once_with(["pbi.exe", os.path.abspath(pbip)])
                    self.assertFalse(any("Diagnostic launch target" in warning
                                         for warning in report.warnings))
                self.assertEqual(report.pbip_path, pbip)
                self.assertEqual(report.status, "opened")

    def test_invalid_report_definition_request_does_not_fallback_or_launch(self):
        from powerbi_import import desktop_probe as dp
        manifests = (
            {}, {"artifacts": []},
            {"artifacts": [{"report": {"path": "../Foreign.Report"}}]},
            {"artifacts": [{"report": {"path": "Missing.Report"}}]},
            {"artifacts": [{"report": {"path": "One.Report"}},
                           {"report": {"path": "Two.Report"}}]},
        )
        with tempfile.TemporaryDirectory() as root:
            pbip = _pbip(root)
            for manifest in manifests:
                with self.subTest(manifest=manifest):
                    with open(pbip, "w", encoding="utf-8") as stream:
                        json.dump(manifest, stream)
                    with mock.patch.object(dp.subprocess, "Popen") as launch:
                        report = dp.probe_desktop_open(
                            pbip, launch_report_definition=True
                        )
                    launch.assert_not_called()
                    self.assertFalse(report.opened)
                    self.assertIn("invalid report-definition diagnostic launch", report.note)


class TestRefreshReport(unittest.TestCase):
    def test_applies_pending_changes_after_schema_refresh(self):
        from powerbi_import import desktop_probe as dp
        clicked = []

        def invoke(_hwnd, names):
            clicked.append(tuple(names))
            return 1 if tuple(names) == dp._APPLY_CHANGES_NAMES else 0

        verified = dp._data_load_result("verified", checked=1, nonempty=1, rows=5)
        with mock.patch.object(dp, "_invoke_schema_refresh", return_value=1), \
             mock.patch.object(dp, "_refresh_ui_state",
                               side_effect=["clear", "busy", "clear", "clear", "clear"]), \
             mock.patch.object(dp, "_click_button", side_effect=invoke), \
             mock.patch.object(dp, "_invoke_button", return_value=0), \
             mock.patch.object(dp, "verify_desktop_data", return_value=verified), \
             mock.patch.object(dp.time, "sleep"):
            result = dp.refresh_report(1, "P.pbip", 1, timeout=60, poll=0)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["mode"], "schema_and_data")
        self.assertTrue(result["applied_changes"])
        self.assertEqual(clicked.count(dp._APPLY_CHANGES_NAMES), 1)

    def run_refresh(self, states, data):
        from powerbi_import import desktop_probe as dp
        ticks = iter(index / 10 for index in range(1000))
        with mock.patch.object(dp, "_invoke_schema_refresh", return_value=1), \
             mock.patch.object(dp, "_click_button", return_value=0), \
             mock.patch.object(dp, "_invoke_button", return_value=0), \
             mock.patch.object(dp, "_refresh_ui_state", side_effect=states), \
             mock.patch.object(dp, "verify_desktop_data", return_value=data), \
             mock.patch.object(dp.time, "time", side_effect=lambda: next(ticks)), \
             mock.patch.object(dp.time, "sleep"):
            return dp.refresh_report(101, "P.pbip", 4242, timeout=1, poll=0)

    def test_pre_refresh_error_blocks_nonempty_count_promotion(self):
        from powerbi_import import desktop_probe as dp
        data = dp._data_load_result("verified", checked=1, nonempty=1, rows=50)
        result = self.run_refresh(lambda *args: "refresh_error", data)
        self.assertEqual(result["status"], "refresh_error")
        # Counts were never sampled; don't invent verification from stale rows.
        self.assertEqual(result["data_load"], dp._data_load_result("unavailable"))

    def test_post_refresh_error_preserves_counts_but_blocks_refresh_verification(self):
        from powerbi_import import desktop_probe as dp
        data = dp._data_load_result("verified", checked=1, nonempty=1, rows=50)
        result = self.run_refresh(["clear", "clear", "refresh_error"], data)
        self.assertEqual(result["status"], "refresh_error")
        self.assertEqual(result["data_load"], data)

    def test_busy_pending_and_unavailable_never_verify_despite_nonempty_counts(self):
        from powerbi_import import desktop_probe as dp
        data = dp._data_load_result("verified", checked=1, nonempty=1, rows=50)
        for state in ("busy", "pending_changes", "unavailable"):
            with self.subTest(state=state):
                result = self.run_refresh(lambda *args: state, data)
                self.assertEqual(result["status"], state)
                self.assertEqual(result["data_load"], data)

    def test_progress_then_clear_complete_counts_verify_without_rewriting_data_status(self):
        from powerbi_import import desktop_probe as dp
        for data in (
            dp._data_load_result("verified", checked=1, nonempty=1, rows=50),
            dp._data_load_result("partial", checked=2, nonempty=1, empty=1, rows=50),
            dp._data_load_result("empty", checked=1, empty=1),
        ):
            with self.subTest(data=data):
                result = self.run_refresh(
                    ["clear", "busy", "clear", "clear", "clear"], data)
                self.assertEqual(result["status"], "verified")
                self.assertEqual(result["data_load"], data)
                self.assertTrue(result["ui_state"]["observed_progress"])

    def test_clear_ui_does_not_make_incomplete_or_failed_counts_complete(self):
        from powerbi_import import desktop_probe as dp
        for data in (
            dp._data_load_result("verified", checked=2, nonempty=1, rows=50),
            dp._data_load_result("partial", checked=2, nonempty=1, failed=1, rows=50),
            dp._data_load_result("empty"),
        ):
            with self.subTest(data=data):
                result = self.run_refresh(lambda *args: "clear", data)
                self.assertNotEqual(result["status"], "verified")
                self.assertEqual(result["data_load"], data)

    def test_saved_blocked_query_banner_pre_and_post_blocks_complete_empty_counts(self):
        from powerbi_import import desktop_probe as dp
        banner = "2 queries are blocked by the following error:"
        state = TestRefreshUIState.observe_text(banner)
        self.assertEqual(state, "refresh_error")
        data = dp._data_load_result("empty", checked=2, empty=2)
        for states, expected_data in (
            ([state], dp._data_load_result("unavailable")),
            (["clear", "clear", state], data),
        ):
            with self.subTest(states=states):
                result = self.run_refresh(states, data)
                self.assertEqual(result["status"], "refresh_error")
                self.assertEqual(result["data_load"], expected_data)

    def test_clear_only_cannot_verify_existing_rows_or_complete_empty_tables(self):
        from powerbi_import import desktop_probe as dp
        for data in (
            dp._data_load_result("verified", checked=1, nonempty=1, rows=50),
            dp._data_load_result("empty", checked=2, empty=2),
        ):
            with self.subTest(data=data):
                result = self.run_refresh(lambda *args: "clear", data)
                self.assertEqual(result["status"], "waiting")
                self.assertEqual(result["data_load"], data)
                self.assertFalse(result["ui_state"]["observed_progress"])

    def test_stale_completion_cannot_verify_without_new_progress(self):
        from powerbi_import import desktop_probe as dp
        data = dp._data_load_result("empty", checked=2, empty=2)
        result = self.run_refresh(lambda *args: "completed", data)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["ui_state"]["initial"], "completed")
        self.assertFalse(result["ui_state"]["observed_progress"])
        self.assertEqual(result["data_load"], data)

    def test_fresh_completion_can_verify_empty_tables_without_observed_busy(self):
        from powerbi_import import desktop_probe as dp
        data = dp._data_load_result("empty", checked=2, empty=2)
        result = self.run_refresh(["clear", "clear", "completed"], data)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["data_load"], data)
        self.assertFalse(result["ui_state"]["observed_progress"])
        self.assertEqual(result["ui_state"]["after"], "completed")

    def test_unavailable_initial_observation_cannot_certify_fresh_completion(self):
        from powerbi_import import desktop_probe as dp
        data = dp._data_load_result("empty", checked=2, empty=2)
        states = iter(["unavailable"])
        result = self.run_refresh(lambda *args: next(states, "completed"), data)
        self.assertEqual(result["status"], "waiting")
        self.assertEqual(result["data_load"], data)

    def test_saved_smart_apostrophe_pending_banner_blocks_empty_count_verification(self):
        from powerbi_import import desktop_probe as dp
        banner = "There are pending changes in your queries that haven’t been applied"
        state = TestRefreshUIState.observe_text(banner)
        self.assertEqual(state, "pending_changes")
        data = dp._data_load_result("empty", checked=2, empty=2)
        result = self.run_refresh(lambda *args: state, data)
        self.assertEqual(result["status"], "pending_changes")
        self.assertEqual(result["data_load"], data)


class TestRefreshWindowVerification(unittest.TestCase):
    def watch(self, status, late_state="clear", capture_unverified=False, data=None):
        from powerbi_import import desktop_probe as dp
        report = DesktopProbeReport("P.pbip")
        if data is None:
            data = dp._data_load_result("verified", checked=1, nonempty=1, rows=50)
        refresh = {"status": status, "clicks": 1, "data_load": data}
        window = SimpleNamespace(hwnd=101, title="P")
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(dp, "desktop_pids", return_value=[4242]), \
                 mock.patch.object(dp.desktop_window, "wait_for_window",
                                   return_value=(window, [])), \
                 mock.patch.object(dp.desktop_window, "window_process_id",
                                   return_value=4242), \
                 mock.patch.object(dp, "_recent_matches", return_value=[]), \
                 mock.patch.object(dp, "_capture_dialogs"), \
                 mock.patch.object(dp, "refresh_report", return_value=refresh), \
                 mock.patch.object(dp, "verify_desktop_data") as recount, \
                 mock.patch.object(dp, "_wait_for_render") as render, \
                 mock.patch.object(dp, "_refresh_ui_state",
                                   return_value=late_state) as state, \
                 mock.patch.object(dp.desktop_window, "capture_window",
                                   return_value="synthetic.png") as capture, \
                 mock.patch.object(dp.desktop_window, "is_blank", return_value=False):
                crashed = dp._watch_window(
                    report, _FakeProc(), "P.pbip", 0, 1,
                    os.path.join(root, "capture.png"), verify_data=True,
                    refresh=True, capture_unverified=capture_unverified)
        self.assertFalse(crashed)
        recount.assert_not_called()
        return report, data, capture, render, state

    def test_failed_refresh_cannot_be_overridden_by_verified_nonempty_data(self):
        for status in ("refresh_error", "busy", "pending_changes", "unavailable"):
            with self.subTest(status=status):
                report, data, capture, render, state = self.watch(status)
                self.assertEqual(report.refresh["status"], status)
                self.assertEqual(report.data_load, data)
                self.assertFalse(report.screenshot_data_verified)
                capture.assert_not_called()
                render.assert_not_called()
                state.assert_not_called()

    def test_late_error_after_render_settle_revokes_screenshot_verification(self):
        report, data, capture, render, state = self.watch(
            "verified", late_state="refresh_error")
        render.assert_called_once()
        state.assert_called_once_with(101, 4242)
        capture.assert_called_once()
        self.assertEqual(report.refresh["status"], "refresh_error")
        self.assertEqual(report.data_load, data)
        self.assertFalse(report.screenshot_data_verified)

    def test_clear_after_render_keeps_completed_refresh_verified(self):
        report, data, capture, render, state = self.watch("verified")
        self.assertEqual(report.refresh["status"], "verified")
        self.assertEqual(report.data_load, data)
        self.assertTrue(report.screenshot_data_verified)
        capture.assert_called_once()

    def test_saved_blocked_query_banner_after_settle_revokes_complete_empty_refresh(self):
        from powerbi_import import desktop_probe as dp
        state = TestRefreshUIState.observe_text(
            "2 queries are blocked by the following error:")
        data = dp._data_load_result("empty", checked=2, empty=2)
        report, actual_data, capture, render, observation = self.watch(
            "verified", late_state=state, data=data)
        self.assertEqual(report.refresh["status"], "refresh_error")
        self.assertFalse(report.screenshot_data_verified)
        self.assertEqual(report.data_load, data)
        render.assert_called_once()
        observation.assert_called_once_with(101, 4242)
        capture.assert_called_once()


class TestRefreshUIState(unittest.TestCase):
    @staticmethod
    def observe_text(text):
        """Exercise the actual emitted UIA request patterns, without Windows."""
        from powerbi_import import desktop_probe as dp

        def read(script, request):
            if re.search(request["error_pattern"], text):
                return {"status": "refresh_error"}
            if re.search(request["pending_pattern"], text):
                return {"status": "pending_changes"}
            if re.search(request["busy_pattern"], text):
                return {"status": "busy"}
            return {"status": "clear"}

        with mock.patch.object(dp.desktop_window, "dialog_windows", return_value=[]), \
             mock.patch.object(dp, "_run_uia", side_effect=read):
            return dp._refresh_ui_state(101, 4242)

    def test_saved_blocked_query_variants_match_narrowly_in_english_and_french(self):
        for text in (
            "2 queries are blocked by the following error:",
            "1 query is blocked by the following error:",
            "2 queries are blocked by the following errors:",
            "2 requêtes sont bloquées par les erreurs suivantes :",
            "1 requête est bloquée par l’erreur suivante :",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.observe_text(text), "refresh_error")
        for text in (
            "0 queries are blocked by the following error:",
            "0 requêtes sont bloquées par les erreurs suivantes :",
            "Your data may be incomplete",
            "Vos données sont peut-être incomplètes",
            "0 errors", "0 erreurs",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.observe_text(text), "clear")

    def test_pending_changes_recognizes_both_apostrophes_in_actual_request_pattern(self):
        for apostrophe in ("'", "’"):
            text = ("There are pending changes in your queries that haven"
                    + apostrophe + "t been applied")
            with self.subTest(text=text):
                self.assertEqual(self.observe_text(text), "pending_changes")

    @unittest.skipUnless(os.name == "nt", "Production UIA filter uses Windows PowerShell")
    def test_production_provider_filter_admits_descendants_not_external_or_zero_pids(self):
        from powerbi_import import desktop_probe as dp

        source = dp._UIA_REFRESH_STATE_SCRIPT
        # Execute the production process-closure and element-admission code,
        # replacing only CIM/UIA inputs with synthetic records. No UI opened.
        closure = source.split("$providers = ", 1)[1].split(
            "$out = [ordered]", 1)[0]
        guard = source.split("foreach ($e in $elements) {", 1)[1].split(
            "    $name = $e.Current.Name", 1)[0]
        script = r'''
$ErrorActionPreference = 'Stop'
$request = @{ owner_pid = 32492 }
$processes = @(
    @{ ProcessId = 43084; ParentProcessId = 32492 },
    @{ ProcessId = 43085; ParentProcessId = 43084 },
    @{ ProcessId = 99999; ParentProcessId = 88888 }
)
$providers = ''' + closure + r'''
$elements = @(32492, 43084, 43085, 99999, 0) | ForEach-Object {
    @{ Current = @{
        ProcessId = $_; IsOffscreen = $false
        BoundingRectangle = @{ Width = 100; Height = 100 }
    }}
}
$admitted = @()
foreach ($e in $elements) {
''' + guard + r'''
    $admitted += [int]$e.Current.ProcessId
}
[Console]::Out.WriteLine((ConvertTo-Json -InputObject @($admitted) -Compress))
'''
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        admitted = json.loads(completed.stdout.strip())
        self.assertEqual(admitted, [32492, 43084, 43085])
        self.assertNotIn(99999, admitted)
        self.assertNotIn(0, admitted)

    def test_only_report_and_owned_dialog_roots_are_read_without_invocation(self):
        from powerbi_import import desktop_probe as dp
        dialogs = [SimpleNamespace(hwnd=202, title="Refresh"),
                   SimpleNamespace(hwnd=303, title="Other dialog")]
        with mock.patch.object(dp.desktop_window, "dialog_windows",
                               return_value=dialogs) as owned, \
             mock.patch.object(dp, "_run_uia", return_value={"status": "clear"}) as read, \
             mock.patch.object(dp, "_invoke_button") as invoke, \
             mock.patch.object(dp, "_click_button") as click:
            self.assertEqual(dp._refresh_ui_state(101, 4242), "clear")
        owned.assert_called_once_with(4242)
        self.assertEqual({call.args[1]["hwnd"] for call in read.call_args_list},
                         {101, 202, 303})
        for call in read.call_args_list:
            self.assertEqual(call.args[0], dp._UIA_REFRESH_STATE_SCRIPT)
            self.assertEqual(call.args[1]["owner_pid"], 4242)
            self.assertEqual(call.args[1]["refresh_dialog"],
                             call.args[1]["hwnd"] == 202)
        self.assertIn("$root.Current.ProcessId -ne [int]$request.owner_pid",
                      dp._UIA_REFRESH_STATE_SCRIPT)
        self.assertIn("$e.Current.ProcessId -ne [int]$request.owner_pid",
                      dp._UIA_REFRESH_STATE_SCRIPT)
        self.assertNotIn(".Invoke(", dp._UIA_REFRESH_STATE_SCRIPT)
        invoke.assert_not_called()
        click.assert_not_called()

    def test_missing_owner_or_failed_observation_is_unavailable(self):
        from powerbi_import import desktop_probe as dp
        with mock.patch.object(dp, "_run_uia", return_value={}) as read, \
             mock.patch.object(dp.desktop_window, "dialog_windows", return_value=[]):
            self.assertEqual(dp._refresh_ui_state(101, None), "unavailable")
            read.assert_not_called()
            self.assertEqual(dp._refresh_ui_state(101, 4242), "unavailable")

    def test_owned_dialog_error_dominates_other_refresh_states(self):
        from powerbi_import import desktop_probe as dp
        with mock.patch.object(dp.desktop_window, "dialog_windows", return_value=[
                SimpleNamespace(hwnd=202, title="Actualisation")]), \
             mock.patch.object(dp, "_run_uia", side_effect=[
                 {"status": "busy"}, {"status": "refresh_error"}]):
            self.assertEqual(dp._refresh_ui_state(101, 4242), "refresh_error")

    def test_english_and_french_nonzero_row_errors_match_but_benign_text_does_not(self):
        from powerbi_import import desktop_probe as dp
        for text in (
            "1 error", "1,638 errors", "1 638 erreurs", "1\u202f638 erreurs",
            "Queries contained errors", "Les requêtes contiennent des erreurs",
            "Expression.Error: invalid date", "Refresh failed",
            "Actualisation a échoué",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(re.search(dp._REFRESH_ERROR_PATTERN, text))
        for text in (
            "0 errors", "0 erreurs", "0.0 errors", "0,0 erreurs",
            "No data", "Aucune donnée",
            "Your data may be incomplete",
            "Vos données sont peut-être incomplètes",
        ):
            with self.subTest(text=text):
                self.assertIsNone(re.search(dp._REFRESH_ERROR_PATTERN, text))


if __name__ == "__main__":
    unittest.main()
