"""Folder pairing, independent capture evidence and honest manual-review reports."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from powerbi_import import desktop_accessibility as accessibility
from powerbi_import import desktop_comparison as comparison
from powerbi_import import desktop_window
from powerbi_import.desktop_probe import DesktopProbeReport
from powerbi_import.tableau_desktop_probe import TableauProbeReport
from tests.pixel_comparison_fixtures import forbid_network, tiny_png


class FolderFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source"
        self.output = self.root / "output"
        self.source.mkdir()
        self.output.mkdir()
        self.enterContext(mock.patch.object(
            comparison, "powershell_preflight",
            return_value={"status": "ready", "executable": "SyntheticPowerShell.exe"}))

    def file(self, root, name, text="{}"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def pair(self, name="Sales.twb"):
        source = self.file(self.source, name, "<workbook/>")
        relative = Path(name)
        pbip = self.file(self.output, relative.parent / relative.stem / (relative.stem + ".pbip"))
        return source, pbip

    def pairs(self):
        return comparison.pair_workbooks(str(self.source), str(self.output))


class TestLocalInitialPixels(FolderFixture):
    """Real offline decoding behind mocked Desktop captures, never an endpoint."""

    def setUp(self):
        super().setUp()
        self.pair()
        forbid_network(self)
        self.source_image = tiny_png()
        self.target_image = tiny_png()
        self.tableau = self.enterContext(mock.patch.object(
            comparison, "probe_tableau_open", side_effect=self.tableau_capture))
        self.powerbi = self.enterContext(mock.patch.object(
            comparison, "_pbi_capture", side_effect=self.powerbi_capture))
        for probe in (comparison.desktop_probe, comparison.tableau_desktop_probe):
            self.enterContext(mock.patch.object(
                probe, "close_probe_process", return_value={"status": "closed"}))
        self.enterContext(mock.patch.object(
            comparison.desktop_probe.subprocess, "Popen",
            side_effect=AssertionError("Tests must not launch Desktop")))

    def tableau_capture(self, source, *, screenshot_path, **kwargs):
        Path(screenshot_path).write_bytes(self.source_image)
        return TableauProbeReport(source, status="captured",
                                  screenshot=screenshot_path,
                                  screenshot_blank=False, pid=111)

    def powerbi_capture(self, pbip, shot, timeout, desktop_path, *, refresh_timeout=300):
        shot.write_bytes(self.target_image)
        return {"status": "captured", "screenshot": str(shot),
                "screenshot_blank": False, "pid": 222, "note": "",
                "screenshot_data_verified": True,
                "data_load": {"status": "verified", "tables_checked": 1,
                              "tables_nonempty": 1, "tables_empty": 0,
                              "tables_failed": 0, "total_rows": 5},
                "refresh": {"status": "verified"}}

    def run_comparison(self, **kwargs):
        return comparison.compare_desktop_reports(
            str(self.source), str(self.output), **kwargs)

    def test_default_and_explicit_manual_keep_manifest_schema_and_skip_decoder(self):
        with mock.patch.object(comparison, "compare_png_files") as decode:
            for options in ({}, {"screen_comparison": "manual"}):
                with self.subTest(options=options):
                    manifest = self.run_comparison(**options)
                    self.assertEqual(manifest["schema_version"], 2)
                    self.assertEqual(manifest["status"], "captured")
                    self.assertIsNone(manifest["parity_score"])
                    self.assertNotIn("screen_comparison", manifest)
                    self.assertNotIn("screen_comparison_limitations", manifest)
                    row, = manifest["workbooks"]
                    self.assertNotIn("pixel_comparison", row)
                    self.assertNotIn("page_pixel_comparison_summary", row)
                    document = (Path(manifest["run_dir"]) / "comparison.html").read_text("utf-8")
                    self.assertNotIn("Local pixel comparison", document)
            decode.assert_not_called()

    def test_initial_metrics_persist_in_json_html_without_parity_verdict(self):
        self.target_image = tiny_png(((20, 20, 30, 255, 40, 50, 60, 255),))
        manifest = self.run_comparison(screen_comparison="local")
        self.assertEqual(manifest["status"], "captured")
        self.assertEqual(manifest["screen_comparison"], "local")
        self.assertIsNone(manifest["parity_score"])
        row, = manifest["workbooks"]
        self.assertEqual(row["status"], "manual_review")
        result = row["pixel_comparison"]
        self.assertEqual(result["status"], "compared")
        self.assertEqual(result["metrics"]["different_pixel_count"], 1)
        self.assertEqual(result["metrics"]["absolute_channel_difference_sum"], 10)
        run_dir = Path(manifest["run_dir"])
        self.assertEqual(json.loads((run_dir / "manifest.json").read_text("utf-8")), manifest)
        document = (run_dir / "comparison.html").read_text("utf-8")
        for text in ("Local pixel comparison (non-semantic)", "Different RGBA pixels",
                     "Mean absolute RGBA channel error", "2 × 1",
                     "Initial captures establish workbook pairing only",
                     "No SSIM or parity score", result["source_sha256"]):
            self.assertIn(text, document)
        self.assertEqual(row["tableau"]["cleanup"]["status"], "closed")
        self.assertEqual(row["powerbi"]["cleanup"]["status"], "closed")

    def test_bad_image_metrics_do_not_change_capture_status(self):
        for image, code, pixel_status in (
                (b"not PNG", "unsupported_image", "not_comparable"),
                (tiny_png()[:-1], "corrupt_png", "error"),
                (tiny_png(((10, 20, 30, 255),)), "dimension_mismatch", "not_comparable")):
            with self.subTest(reason=code):
                self.target_image = image
                manifest = self.run_comparison(screen_comparison="local")
                self.assertEqual(manifest["status"], "captured")
                row, = manifest["workbooks"]
                self.assertEqual(row["status"], "manual_review")
                self.assertEqual(row["pixel_comparison"]["status"], pixel_status)
                self.assertEqual(row["pixel_comparison"]["reason_code"], code)
                self.assertIsNone(row["pixel_comparison"]["metrics"])

    def test_unavailable_capture_is_excluded_even_with_a_png_on_disk(self):
        stale = self.root / "stale.png"
        stale.write_bytes(tiny_png())
        self.powerbi.side_effect = None
        self.powerbi.return_value = {
            "status": "unavailable", "screenshot": str(stale),
            "screenshot_blank": False, "note": "app unavailable"}
        with mock.patch.object(comparison, "compare_png_files") as decode:
            manifest = self.run_comparison(screen_comparison="local")
            decode.assert_not_called()
        self.assertEqual(manifest["status"], "failed")
        row, = manifest["workbooks"]
        self.assertEqual(row["pixel_comparison"]["reason_code"], "capture_unavailable")
        self.assertIsNone(row["pixel_comparison"]["metrics"])
        document = (Path(manifest["run_dir"]) / "comparison.html").read_text("utf-8")
        self.assertIn("No capture: unavailable", document)
        self.assertNotIn('alt="Power BI capture"', document)

    def test_invalid_mode_is_rejected_before_pairing_or_capture(self):
        for mode in ("ai", "LOCAL", "", None):
            with self.subTest(mode=mode), \
                 mock.patch.object(comparison, "pair_workbooks") as pair, \
                 self.assertRaisesRegex(ValueError, "screen_comparison"):
                self.run_comparison(screen_comparison=mode)
            pair.assert_not_called()
        self.tableau.assert_not_called()
        self.powerbi.assert_not_called()
        self.assertFalse((self.output / "desktop_comparisons").exists())

    def test_provenance_nonblank_and_page_selection_are_required_before_decode(self):
        source_path, target_path = self.root / "a.png", self.root / "b.png"
        source_path.write_bytes(tiny_png())
        target_path.write_bytes(tiny_png())
        source = {"status": "captured", "screenshot": str(source_path),
                  "screenshot_blank": False, "selection": {"status": "verified"},
                  "post_capture_selection": {"status": "verified"}}
        target = dict(source, screenshot=str(target_path))
        cases = (
            ({"screenshot_blank": None}, source_path, False, "capture_unverified"),
            ({"screenshot_blank": True}, source_path, False, "capture_unverified"),
            ({}, self.root / "different.png", False, "capture_not_current"),
            ({}, None, False, "capture_not_current"),
            ({"selection": None}, source_path, True, "capture_unverified"),
            ({"post_capture_selection": {"status": "failed"}}, source_path,
             True, "capture_unverified"),
        )
        with mock.patch.object(comparison, "compare_png_files") as decode:
            for overrides, expected, pages, code in cases:
                with self.subTest(overrides=overrides, expected=expected, pages=pages):
                    result = comparison._local_capture_pixels(
                        dict(source, **overrides), target, expected, target_path, pages=pages)
                    self.assertEqual(result["reason_code"], code)
                    self.assertIsNone(result["metrics"])
            decode.assert_not_called()


class TestBatchPairing(FolderFixture):
    def test_recursive_only_workbooks_case_insensitive_and_deterministic(self):
        self.pair("z/Sales.TWBX")
        self.pair("a/Sales.twb")
        for name in ("ignored.tds", "ignored.tdsx", "ignored.hyper", "ignored.pbip"):
            self.file(self.source, name)
        rows = self.pairs()
        self.assertEqual([row["relative_source"] for row in rows], ["a/Sales.twb", "z/Sales.TWBX"])
        self.assertEqual([row["pair_status"] for row in rows], ["paired", "paired"])
        self.assertEqual(rows, self.pairs())
        self.assertNotEqual(rows[0]["pbip"], rows[1]["pbip"])

    def test_missing_pair_does_not_use_global_basename(self):
        self.file(self.source, "region/Sales.twb")
        self.file(self.output, "elsewhere/Sales/Sales.pbip")
        row, = self.pairs()
        self.assertEqual(row["pair_status"], "missing")
        self.assertEqual(row["candidates"], [])
        self.assertIsNone(row["pbip"])

    def test_sanitized_batch_name_is_supported(self):
        self.file(self.source, "region/Sales & Profit.twb")
        target = self.file(self.output, "region/Sales___Profit/Sales___Profit.pbip")
        row, = self.pairs()
        self.assertEqual(row["pair_status"], "paired")
        self.assertEqual(row["pbip"], str(target.resolve()))

    def test_exact_and_sanitized_conflict_is_ambiguous(self):
        self.pair("Sales & Profit.twb")
        self.file(self.output, "Sales___Profit/Sales___Profit.pbip")
        row, = self.pairs()
        self.assertEqual(row["pair_status"], "ambiguous")
        self.assertEqual(len(row["candidates"]), 2)
        self.assertIsNone(row["pbip"])

    def test_two_source_extensions_cannot_claim_one_project(self):
        self.pair("Sales.twb")
        self.file(self.source, "Sales.twbx")
        rows = self.pairs()
        self.assertEqual([row["pair_status"] for row in rows], ["ambiguous", "ambiguous"])
        self.assertTrue(all(row["pbip"] is None and row["issues"] for row in rows))

    def mapping(self, json_path, report_name="Renamed", key="region/Sales"):
        quality = self.file(self.output, "region/quality.json",
                            json.dumps({"report_name": report_name}))
        self.file(self.output, "batch_quality_summary.json",
                  json.dumps({"workbooks": {key: {"json_path": json_path or str(quality)}}}))

    def test_quality_summary_resolves_renamed_report(self):
        self.file(self.source, "region/Sales.twb")
        target = self.file(self.output, "region/Renamed/Report.pbip")
        self.mapping(None)
        row, = self.pairs()
        self.assertEqual(row["pair_status"], "paired")
        self.assertEqual(row["pbip"], str(target.resolve()))

    def test_quality_mapping_and_layout_conflict_is_ambiguous(self):
        self.pair("region/Sales.twb")
        self.file(self.output, "region/Renamed/Report.pbip")
        self.mapping(None)
        self.assertEqual(self.pairs()[0]["pair_status"], "ambiguous")

    def test_malformed_or_outside_mapping_fails_closed(self):
        self.pair("region/Sales.twb")
        summary = self.file(self.output, "batch_quality_summary.json", "not json")
        self.assertEqual(self.pairs()[0]["pair_status"], "error")
        self.assertIsNone(self.pairs()[0]["pbip"])
        outside = self.file(self.root, "outside.json", '{"report_name":"Renamed"}')
        self.mapping(str(outside))
        self.assertEqual(self.pairs()[0]["pair_status"], "error")
        summary.unlink()

    def test_single_workbook_or_single_pbip_arguments_rejected(self):
        source, pbip = self.pair()
        for first, second in ((source, self.output), (self.source, pbip)):
            with self.subTest(first=first, second=second), self.assertRaises(ValueError):
                comparison.pair_workbooks(str(first), str(second))


class TestPowerBICapture(FolderFixture):
    def setUp(self):
        super().setUp()
        _, self.pbip = self.pair()
        self.shot = self.root / "fresh.png"
        self.enterContext(mock.patch.object(desktop_window, "IS_WINDOWS", True))
        self.probe = self.enterContext(mock.patch.object(
            comparison.desktop_probe, "probe_desktop_open"))

    def report(self, **kwargs):
        values = dict(pbip_path=str(self.pbip), status="opened",
                      window_title="Sales - Power BI Desktop", screenshot=str(self.shot),
                      screenshot_blank=False, screenshot_data_verified=True, note="",
                      data_load={"status": "verified", "tables_checked": 1,
                                 "tables_nonempty": 1, "tables_empty": 0,
                                 "tables_failed": 0, "total_rows": 5},
                      refresh={"status": "verified", "clicks": 1, "waited_s": 1.0})
        values.update(kwargs)
        return DesktopProbeReport(**values)

    def capture(self):
        return comparison._pbi_capture(str(self.pbip), self.shot, 7, "override")

    def test_verified_current_nonblank_window_capture_required(self):
        def capture(*args, **kwargs):
            self.shot.write_bytes(b"new image")
            return self.report()
        self.probe.side_effect = capture
        result = self.capture()
        self.assertEqual(result["status"], "captured")
        self.assertEqual(result["screenshot"], str(self.shot))
        self.probe.assert_called_once_with(
            str(self.pbip), timeout=7, close_after=False, wait_for_window=True,
            screenshot_path=str(self.shot), desktop_path="override",
            verify_data=True, refresh=True, refresh_timeout=300)

    def test_process_survival_fallback_blank_and_wrong_path_are_not_evidence(self):
        for overrides in ({"window_title": None}, {"window_title": "Sales2 - Power BI Desktop"},
                          {"screenshot_blank": True},
                          {"screenshot_blank": None}, {"screenshot": None},
                          {"screenshot": str(self.root / "other.png")},
                          {"status": "error"}, {"status": "timed_out"}):
            with self.subTest(overrides=overrides):
                self.shot.unlink(missing_ok=True)
                report = self.report(**overrides)
                def capture(*args, **kwargs):
                    self.shot.write_bytes(b"current image")
                    return report
                self.probe.side_effect = capture
                result = self.capture()
                self.assertNotEqual(result["status"], "captured")
                self.assertIsNone(result["screenshot"])

    def test_preexisting_destination_cannot_be_claimed_as_fresh_capture(self):
        self.shot.write_bytes(b"stale")
        self.probe.return_value = self.report()
        result = self.capture()
        self.assertNotEqual(result["status"], "captured")
        self.assertIsNone(result["screenshot"])
        self.probe.assert_not_called()

    def test_missing_file_and_probe_exception_fail(self):
        self.probe.return_value = self.report()
        self.assertEqual(self.capture()["status"], "error")
        self.probe.side_effect = OSError("capture failed")
        result = self.capture()
        self.assertEqual(result["status"], "error")
        self.assertIn("capture failed", result["note"])

    def test_nonwindows_never_launches(self):
        with mock.patch.object(desktop_window, "IS_WINDOWS", False):
            self.assertEqual(self.capture()["status"], "unavailable")
        self.probe.assert_not_called()


class TestPowerBIProcessSafety(FolderFixture):
    """Exercise the wrapper's real probe path, with process/Win32 doubles."""
    def setUp(self):
        super().setUp()
        _, self.pbip = self.pair()
        self.shot = self.root / "fresh.png"
        self.enterContext(mock.patch.object(desktop_window, "IS_WINDOWS", True))
        # Only the provider prerequisite is synthetic, not the host's os.name.
        # Exercise the actual shared preflight without resolving a real tool.
        self.enterContext(mock.patch.object(
            accessibility, "sys", SimpleNamespace(platform="win32")))
        self.resolver = self.enterContext(mock.patch.object(
            accessibility, "resolve_powershell_executable",
            return_value="/__synthetic_native_tooling__/powershell.exe"))
        # Healthy mocked PID enumeration must not inherit a failed real query
        # from an earlier test; the probe intentionally fails closed on it.
        self.enterContext(mock.patch.object(
            comparison.desktop_probe, "_DESKTOP_PIDS_QUERY_FAILED", False))
        self.enterContext(mock.patch.object(
            comparison.desktop_probe, "_scan_trace_errors", return_value=[]))
        self.enterContext(mock.patch.object(
            comparison.desktop_probe, "_recent_matches", return_value=[]))
        self.proc = mock.Mock(pid=4242, returncode=0)
        self.proc.poll.return_value = 0
        self.launch = self.enterContext(mock.patch.object(
            comparison.desktop_probe.subprocess, "Popen", return_value=self.proc))

    def capture(self, override=None):
        return comparison._pbi_capture(str(self.pbip), self.shot, 1, override)

    def test_missing_app_and_invalid_override_do_not_launch(self):
        fallback = self.file(self.root, "pf/Microsoft Power BI Desktop/bin/PBIDesktop.exe")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.capture()["status"], "unavailable")
            os.environ["ProgramFiles"] = str(fallback.parents[2])
            # Even with a valid default installation, an invalid override fails closed.
            self.assertEqual(self.capture(str(self.root / "missing.exe"))["status"], "unavailable")
        self.launch.assert_not_called()

    def test_existing_instance_forwarding_never_borrows_or_terminates(self):
        def wait(pids, name, **kwargs):
            self.assertEqual(pids(), [4242])
            self.assertEqual(name, "Sales")
            return None, ["document window timeout"]
        with mock.patch.object(comparison.desktop_probe, "find_pbi_desktop",
                               return_value="PBIDesktop.exe"), \
             mock.patch.object(comparison.desktop_probe, "desktop_pids",
                               side_effect=[[999], [999, 4242], [999, 4242]]), \
             mock.patch.object(desktop_window, "wait_for_window", side_effect=wait), \
             mock.patch.object(desktop_window, "dialog_windows", return_value=[]) as dialogs, \
             mock.patch.object(desktop_window, "main_window", return_value=None) as main, \
             mock.patch.object(desktop_window, "capture_window") as capture:
            result = self.capture()
        self.assertNotEqual(result["status"], "captured")
        self.assertIsNone(result["screenshot"])
        self.assertTrue(dialogs.called)
        self.assertTrue(all(call.args == (4242,) for call in dialogs.call_args_list))
        self.assertTrue(all(call.args == (4242,) for call in main.call_args_list))
        capture.assert_not_called()
        self.proc.terminate.assert_not_called()
        self.proc.kill.assert_not_called()
        self.proc.wait.assert_not_called()
        self.resolver.assert_called_once_with()

    def test_missing_or_nonwindows_native_tooling_stops_before_launch_or_borrowing(self):
        for platform, executable, cause in (
                ("win32", None, "powershell_not_found"),
                ("linux", None, "windows_required"),
                ("linux", "/__synthetic_native_tooling__/powershell.exe", "windows_required")):
            with self.subTest(platform=platform, executable=executable):
                self.resolver.reset_mock()
                self.resolver.return_value = executable
                with mock.patch.object(accessibility, "sys", SimpleNamespace(platform=platform)), \
                        mock.patch.object(comparison.desktop_probe, "find_pbi_desktop",
                                          return_value="PBIDesktop.exe"), \
                        mock.patch.object(comparison.desktop_probe, "desktop_pids") as pids, \
                        mock.patch.object(desktop_window, "wait_for_window") as wait, \
                        mock.patch.object(desktop_window, "dialog_windows") as dialogs, \
                        mock.patch.object(desktop_window, "capture_window") as capture:
                    result = self.capture()
                self.assertEqual((result["status"], result["cause"]), ("tooling_unavailable", cause))
                self.assertEqual(result["native_error"]["cause"], cause)
                self.assertIs(result["native_error"]["retryable"], False)
                self.assertIsNone(result["pid"])
                self.assertIsNone(result["screenshot"])
                pids.assert_not_called()
                wait.assert_not_called()
                dialogs.assert_not_called()
                capture.assert_not_called()
                if platform == "win32":
                    self.resolver.assert_called_once_with()
                else:
                    self.resolver.assert_not_called()
        self.launch.assert_not_called()
        self.proc.terminate.assert_not_called()
        self.proc.kill.assert_not_called()
        self.proc.wait.assert_not_called()


class TestComparisonWorkflow(FolderFixture):
    def setUp(self):
        super().setUp()
        self.pair()
        self.tableau = self.enterContext(mock.patch.object(
            comparison, "probe_tableau_open", side_effect=self.tableau_capture))
        self.powerbi = self.enterContext(mock.patch.object(
            comparison, "_pbi_capture", side_effect=self.powerbi_capture))
        self.tableau_cleanup = self.enterContext(mock.patch.object(
            comparison.tableau_desktop_probe, "close_probe_process",
            return_value={"status": "closed", "note": "mock closed"}))
        self.powerbi_cleanup = self.enterContext(mock.patch.object(
            comparison.desktop_probe, "close_probe_process",
            return_value={"status": "closed", "note": "mock closed"}))

    def image(self, path):
        Path(path).write_bytes(desktop_window._png_bytes(
            16, 16, bytes((i * 37) % 251 for i in range(16 * 16 * 4))))

    def tableau_capture(self, source, *, screenshot_path, **kwargs):
        self.image(screenshot_path)
        return TableauProbeReport(
            source, status="captured", screenshot=screenshot_path,
            screenshot_blank=False, pid=111)

    def powerbi_capture(self, pbip, shot, timeout, desktop_path, *, refresh_timeout=300):
        self.image(shot)
        return {"status": "captured", "screenshot": str(shot), "note": "", "pid": 222,
                "screenshot_blank": False, "screenshot_data_verified": True,
                "data_load": {"status": "verified", "tables_checked": 1,
                              "tables_nonempty": 1, "tables_empty": 0,
                              "tables_failed": 0, "total_rows": 5},
                "refresh": {"status": "verified"}}

    def run_comparison(self, **kwargs):
        return comparison.compare_desktop_reports(str(self.source), str(self.output), **kwargs)

    def test_success_has_real_paired_pngs_json_html_and_honest_scope(self):
        manifest = self.run_comparison(
            timeout=8, tableau_desktop_path="tableau override",
            powerbi_desktop_path="powerbi override")
        self.assertEqual(manifest["status"], "captured")
        self.assertIsNone(manifest["parity_score"])
        row, = manifest["workbooks"]
        self.assertEqual(row["status"], "manual_review")
        run_dir = Path(manifest["run_dir"])
        self.assertEqual(json.loads((run_dir / "manifest.json").read_text("utf-8")), manifest)
        document = (run_dir / "comparison.html").read_text("utf-8")
        for side in ("tableau", "powerbi"):
            shot = Path(row[side]["screenshot"])
            self.assertTrue(shot.read_bytes().startswith(b"\x89PNG"))
            self.assertFalse(desktop_window.is_blank(str(shot)))
            payload = shot.read_bytes()
            url = f"assets/{hashlib.sha256(payload).hexdigest()}.png"
            self.assertIn(f'src="{url}"', document)
            self.assertIn(f'href="{url}"', document)
            self.assertEqual((run_dir / url).read_bytes(), payload)
        self.assertIn("not proof of loaded data or visual fidelity", document)
        self.assertIn("No SSIM or parity score", document)
        self.assertIn("initially opened sheet/page", document)
        self.assertEqual(self.tableau.call_args.kwargs["desktop_path"], "tableau override")
        self.assertEqual(self.powerbi.call_args.args[2:], (8, "powerbi override"))
        self.assertEqual(row["tableau"]["cleanup"]["status"], "closed")
        self.assertEqual(row["powerbi"]["cleanup"]["status"], "closed")
        self.tableau_cleanup.assert_called_once_with(111)
        self.powerbi_cleanup.assert_called_once_with(222)

    def test_repeat_runs_never_reuse_old_capture_paths(self):
        first = self.run_comparison()
        second = self.run_comparison()
        self.assertNotEqual(first["run_id"], second["run_id"])
        for side in ("tableau", "powerbi"):
            self.assertNotEqual(first["workbooks"][0][side]["screenshot"],
                                second["workbooks"][0][side]["screenshot"])

    def test_returned_tableau_failure_does_not_block_powerbi(self):
        self.tableau.side_effect = None
        self.tableau.return_value = TableauProbeReport("Sales.twb", status="timed_out")
        manifest = self.run_comparison()
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["workbooks"][0]["powerbi"]["status"], "captured")
        self.powerbi.assert_called_once()

    def test_returned_powerbi_failure_preserves_tableau(self):
        self.powerbi.side_effect = None
        self.powerbi.return_value = {"status": "timed_out", "screenshot": None, "note": ""}
        manifest = self.run_comparison()
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["workbooks"][0]["tableau"]["status"], "captured")
        self.assertEqual(manifest["workbooks"][0]["powerbi"]["status"], "timed_out")
        self.tableau.assert_called_once()

    def test_tableau_exception_does_not_block_powerbi_or_next_workbook(self):
        self.pair("Other.twbx")
        self.tableau.side_effect = OSError("Tableau capture denied")
        manifest = self.run_comparison()
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(self.powerbi.call_count, 2)
        for row in manifest["workbooks"]:
            self.assertEqual(row["tableau"]["status"], "error")
            self.assertEqual(row["powerbi"]["status"], "captured")

    def test_powerbi_exception_preserves_tableau_and_continues(self):
        self.pair("Other.twbx")
        self.powerbi.side_effect = OSError("Power BI capture denied")
        manifest = self.run_comparison()
        self.assertEqual(self.tableau.call_count, 2)
        self.assertEqual(manifest["status"], "failed")
        for row in manifest["workbooks"]:
            self.assertEqual(row["tableau"]["status"], "captured")
            self.assertEqual(row["powerbi"]["status"], "error")

    def test_missing_pair_is_recorded_without_capture(self):
        self.file(self.source, "Missing.twb")
        manifest = self.run_comparison()
        missing = next(r for r in manifest["workbooks"] if r["relative_source"] == "Missing.twb")
        self.assertEqual(missing["pair_status"], "missing")
        self.assertEqual(missing["status"], "failed")
        self.assertEqual(missing["tableau"]["status"], "not_requested")
        self.assertEqual(missing["powerbi"]["status"], "not_requested")
        self.tableau.assert_called_once()
        self.powerbi.assert_called_once()

    def test_ambiguous_pair_never_launches_either_app(self):
        self.file(self.source, "Sales.twbx")
        manifest = self.run_comparison()
        self.assertEqual(manifest["status"], "failed")
        self.assertTrue(all(row["pair_status"] == "ambiguous" for row in manifest["workbooks"]))
        self.tableau.assert_not_called()
        self.powerbi.assert_not_called()

    def test_linked_output_fails_before_capture(self):
        with mock.patch.object(Path, "is_junction", return_value=True), \
             self.assertRaisesRegex(ValueError, "link or junction"):
            self.run_comparison()
        self.tableau.assert_not_called()
        self.powerbi.assert_not_called()

    def test_empty_source_and_invalid_timeouts_do_not_capture(self):
        for workbook in self.source.iterdir():
            workbook.unlink()
        with self.assertRaises(ValueError):
            self.run_comparison()
        self.pair()
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.run_comparison(timeout=timeout)
        self.tableau.assert_not_called()
        self.powerbi.assert_not_called()

    def test_html_escapes_user_fields_and_never_links_failed_stale_image(self):
        manifest = self.run_comparison()
        run_dir = Path(manifest["run_dir"])
        row = manifest["workbooks"][0]
        payload = '<script>alert("x")</script> &'
        row["relative_source"] = payload
        row["issues"] = [payload]
        row["tableau"]["note"] = payload
        stale = row["tableau"]["screenshot"]
        row["tableau"]["status"] = "error"
        row["status"] = "failed"
        manifest["status"] = "failed"
        comparison.write_comparison_reports(manifest, run_dir)
        document = (run_dir / "comparison.html").read_text("utf-8")
        self.assertNotIn("<script>", document)
        self.assertIn("&lt;script&gt;", document)
        self.assertIn("&quot;x&quot;", document)
        self.assertIn("&amp;", document)
        self.assertEqual(document.count("<img "), 1)
        self.assertIn('data-capture-status="error"', document)
        self.assertIn('data-capture-diagnostic="No capture: error"', document)
        self.assertEqual(row["tableau"]["screenshot"], stale)
        # A valid peer may share the same asset bytes; the failed evidence is
        # excluded by status, not by forbidding its content-addressed URL.
        self.assertTrue(Path(stale).is_file())

    def test_html_uses_hashed_asset_for_image_path_special_characters(self):
        manifest = self.run_comparison()
        run_dir = Path(manifest["run_dir"])
        row = manifest["workbooks"][0]
        original = Path(row["tableau"]["screenshot"])
        renamed = original.with_name("Sales & café 東京 #1.png")
        original.rename(renamed)
        row["tableau"]["screenshot"] = str(renamed)
        comparison.write_comparison_reports(manifest, run_dir)
        document = (run_dir / "comparison.html").read_text("utf-8")
        payload = renamed.read_bytes()
        url = f"assets/{hashlib.sha256(payload).hexdigest()}.png"
        self.assertIn(f'src="{url}"', document)
        self.assertIn(f'href="{url}"', document)
        self.assertEqual((run_dir / url).read_bytes(), payload)
        self.assertEqual(renamed.read_bytes(), payload)
        self.assertEqual(json.loads((run_dir / "manifest.json").read_text("utf-8")),
                         manifest)
        self.assertEqual(document.count("<img "), 2)

    def test_schema2_legacy_screenshot_paths_render_without_new_evidence_fields(self):
        manifest = self.run_comparison()
        run_dir = Path(manifest["run_dir"])
        manifest.pop("evidence_location")
        manifest.pop("preflight")
        row, = manifest["workbooks"]
        row.pop("evidence_id")
        row.pop("screenshot_dir")
        legacy = Path(row["pbip"]).parent / "screenshots"
        legacy.mkdir()
        originals = {}
        for side in ("tableau", "powerbi"):
            path = legacy / f"{side}.png"
            payload = Path(row[side]["screenshot"]).read_bytes()
            path.write_bytes(payload)
            originals[path] = payload
            row[side]["screenshot"] = str(path)
        comparison.write_comparison_reports(manifest, run_dir)
        self.assertEqual(manifest["schema_version"], 2)
        self.assertNotIn("evidence_location", manifest)
        self.assertEqual(json.loads((run_dir / "manifest.json").read_text("utf-8")),
                         manifest)
        document = (run_dir / "comparison.html").read_text("utf-8")
        self.assertEqual(document.count("<img "), 2)
        for path, payload in originals.items():
            self.assertEqual(path.read_bytes(), payload)
            asset = f"assets/{hashlib.sha256(payload).hexdigest()}.png"
            self.assertIn(f'src="{asset}"', document)
            self.assertEqual((run_dir / asset).read_bytes(), payload)
