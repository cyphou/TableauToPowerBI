"""One-command Desktop evidence contracts; all GUI/process calls are doubles."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from powerbi_import import desktop_comparison as comparison
from powerbi_import import desktop_window
from powerbi_import.desktop_probe import DesktopProbeReport
from powerbi_import.tableau_desktop_probe import TableauProbeReport
from tests.pixel_comparison_fixtures import forbid_network, tiny_png


def _counts(status="verified", *, checked=2, nonempty=2, empty=0, failed=0):
    return {
        "status": status, "tables_checked": checked,
        "tables_nonempty": nonempty, "tables_empty": empty,
        "tables_failed": failed, "total_rows": 7 if nonempty else 0,
    }


class _Headings(HTMLParser):
    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.headings = []
        self.panels = []
        self.current = None
        self.feed(document)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag in {"title", "h1", "h2", "h3"}:
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag in {"title", "h1", "h2", "h3"} and self.current is not None:
            heading = "".join(self.current)
            self.headings.append(heading)
            if tag == "h3":
                self.panels.append(heading)
            self.current = None


class DesktopEvidenceFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source_dir = self.root / "source"
        self.output = self.root / "output"
        self.source = self.write(
            self.source_dir / "region" / "Sales.twb",
            '<workbook><worksheets><worksheet name="Detail"/></worksheets>'
            '<dashboards><dashboard name="Overview"/></dashboards></workbook>')
        self.pbip = self.write(
            self.output / "region" / "Current" / "Current.pbip",
            json.dumps({"artifacts": [{"report": {"path": "Current.Report"}}]}))
        pages = self.pbip.parent / "Current.Report" / "definition" / "pages"
        self.write(pages / "pages.json", json.dumps({"pageOrder": ["p0", "p1"]}))
        for index, name in enumerate(("Overview", "Detail")):
            self.write(pages / f"p{index}" / "page.json", json.dumps({
                "name": f"p{index}", "displayName": name,
                "visibility": "visible",
            }))
        self.pair = {
            "source": str(self.source.resolve()),
            "relative_source": "region/Sales.twb",
            "pair_status": "paired",
            "pbip": str(self.pbip.resolve()),
            "candidates": [str(self.pbip.resolve())], "issues": [],
        }
        self.overrides = {}
        self.events = []
        forbid_network(self)
        self.preflight = self.enterContext(mock.patch.object(
            comparison, "powershell_preflight",
            return_value={"status": "ready", "executable": "SyntheticPowerShell.exe"}))
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(mock.patch.object(desktop_window, "IS_WINDOWS", True))
        self.enterContext(mock.patch.object(
            comparison.desktop_probe.subprocess, "Popen",
            side_effect=AssertionError("Regression tests must not launch apps")))
        self.probe = self.enterContext(mock.patch.object(
            comparison.desktop_probe, "probe_desktop_open",
            side_effect=self.powerbi_capture))
        self.tableau = self.enterContext(mock.patch.object(
            comparison, "probe_tableau_open", side_effect=self.tableau_capture))
        self.tableau_inventory = self.enterContext(mock.patch.object(
            comparison, "inventory_tableau_pages",
            wraps=comparison.inventory_tableau_pages))
        self.tableau_cleanup = self.enterContext(mock.patch.object(
            comparison.tableau_desktop_probe, "close_probe_process",
            side_effect=lambda pid: self.cleanup("tableau", pid)))
        self.powerbi_cleanup = self.enterContext(mock.patch.object(
            comparison.desktop_probe, "close_probe_process",
            side_effect=lambda pid: self.cleanup("powerbi", pid)))
        self.tabs = self.enterContext(mock.patch.object(
            comparison, "capture_document_tab", side_effect=self.page_capture))
        self.enterContext(mock.patch.object(
            desktop_window, "windows_for_pid", side_effect=self.windows))
        self.enterContext(mock.patch.object(
            comparison.desktop_probe, "_refresh_ui_state", return_value="clear"))
        for owner in (comparison.desktop_probe, comparison.tableau_desktop_probe):
            self.enterContext(mock.patch.dict(owner._OPEN_PROBE_PROCESSES, clear=True))

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def powerbi_capture(self, pbip, *, screenshot_path, **options):
        Path(screenshot_path).write_bytes(tiny_png())
        values = {
            "pbip_path": pbip, "status": "opened", "pid": 222,
            "window_title": f"{Path(pbip).stem} - Power BI Desktop",
            "screenshot": screenshot_path, "screenshot_blank": False,
            "screenshot_data_verified": True,
            "data_load": _counts(),
            "refresh": {"status": "verified", "clicks": 1, "waited_s": 0.1},
        }
        values.update(copy.deepcopy(self.overrides))
        return DesktopProbeReport(**values)

    @staticmethod
    def tableau_capture(source, *, screenshot_path, **options):
        Path(screenshot_path).write_bytes(tiny_png())
        return TableauProbeReport(
            source, status="captured", pid=111,
            window_title=f"{Path(source).stem} - Tableau",
            screenshot=screenshot_path, screenshot_blank=False)

    def page_capture(self, hwnd, pid, name, path, *, timeout):
        self.events.append(("page", pid, name))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(tiny_png())
        return {
            "status": "captured", "screenshot": path, "screenshot_blank": False,
            "selection": {"status": "verified"},
            "post_capture_selection": {"status": "verified"}, "note": "",
        }

    @staticmethod
    def windows(pid):
        title = "Sales - Tableau" if pid == 111 else "Current - Power BI Desktop"
        return [desktop_window.WindowInfo(pid, title, "Desktop", 1200, 800, False)]

    def cleanup(self, side, pid):
        self.events.append(("cleanup", side, pid))
        return {"status": "closed"}

    def compare(self, **options):
        return comparison.compare_desktop_reports(
            str(self.source_dir), str(self.output),
            workbook_pairs=[copy.deepcopy(self.pair)], **options)

    def assert_no_tableau(self):
        self.tableau.assert_not_called()
        self.tableau_inventory.assert_not_called()
        self.tableau_cleanup.assert_not_called()
        self.assertTrue(all(call.args[1] == 222 for call in self.tabs.call_args_list))


class TestExactCurrentDesktopPairs(DesktopEvidenceFixture):
    def test_explicit_pairs_never_scan_or_capture_stale_outputs(self):
        self.write(self.source_dir / "Stale.twb", "<workbook/>")
        self.write(self.source_dir / "Prep.tfl", "{}")
        self.write(self.output / "Stale" / "Stale.pbip", "{}")
        self.write(self.output / "Sales" / "Sales.pbip", "{}")
        with mock.patch.object(comparison, "pair_workbooks") as scan:
            manifest = self.compare(capture_tableau=False)
        scan.assert_not_called()
        self.assertEqual(manifest["status"], "captured")
        row, = manifest["workbooks"]
        for key in ("source", "relative_source", "pair_status", "pbip", "candidates"):
            self.assertEqual(row[key], self.pair[key])
        self.assertEqual(self.probe.call_args.args, (str(self.pbip.resolve()),))
        self.assert_no_tableau()

    def test_selected_pairs_are_copied_not_mutated_into_capture_results(self):
        pairs = [copy.deepcopy(self.pair)]
        before = copy.deepcopy(pairs)
        comparison.compare_desktop_reports(
            str(self.source_dir), str(self.output),
            capture_tableau=False, workbook_pairs=pairs)
        self.assertEqual(pairs, before)

    def test_explicit_empty_pairs_do_not_fall_back_to_folder_scan(self):
        with mock.patch.object(comparison, "pair_workbooks") as scan:
            with self.assertRaises(ValueError):
                comparison.compare_desktop_reports(
                    str(self.source_dir), str(self.output),
                    capture_tableau=False, workbook_pairs=[])
        scan.assert_not_called()
        self.probe.assert_not_called()
        self.assert_no_tableau()
        self.assertFalse((self.output / "desktop_comparisons").exists())

    def test_malformed_failed_prep_and_outside_pairs_fail_before_any_capture(self):
        prep = self.write(self.source_dir / "OnlyPrep.tfl", "{}")
        outside = self.write(self.root / "outside" / "Other.pbip", "{}")
        for overrides in (
                {"source": str(prep.resolve())},
                {"source": "region/Sales.twb"},
                {"pbip": str(outside.resolve()), "candidates": [str(outside.resolve())]},
                {"pbip": str(self.output / "missing.pbip")},
                {"pair_status": "failed", "issues": ["generation failed"]},
                {"candidates": []},
                {"candidates": [str(self.pbip), str(outside)]},
        ):
            with self.subTest(overrides=overrides), \
                    mock.patch.object(comparison, "pair_workbooks") as scan:
                pair = dict(self.pair, **overrides)
                with self.assertRaises((ValueError, OSError)):
                    comparison.compare_desktop_reports(
                        str(self.source_dir), str(self.output),
                        workbook_pairs=[pair], capture_tableau=False)
                scan.assert_not_called()
        self.probe.assert_not_called()
        self.assert_no_tableau()
        self.assertFalse((self.output / "desktop_comparisons").exists())

    def test_duplicate_pair_cannot_launch_the_same_output_twice(self):
        with self.assertRaisesRegex(ValueError, "duplicate|already selected"):
            comparison.compare_desktop_reports(
                str(self.source_dir), str(self.output),
                workbook_pairs=[copy.deepcopy(self.pair), copy.deepcopy(self.pair)])
        self.probe.assert_not_called()
        self.tableau.assert_not_called()
        self.assertFalse((self.output / "desktop_comparisons").exists())

    def test_default_api_still_pairs_folders_and_captures_both_apps(self):
        with mock.patch.object(comparison, "pair_workbooks",
                               return_value=[copy.deepcopy(self.pair)]) as scan:
            manifest = comparison.compare_desktop_reports(
                str(self.source_dir), str(self.output))
        scan.assert_called_once_with(str(self.source_dir), str(self.output))
        self.tableau.assert_called_once()
        self.probe.assert_called_once()
        self.assertEqual(manifest["status"], "captured")
        self.assertEqual(self.probe.call_args.kwargs["refresh_timeout"], 300)

    def test_timeouts_and_executable_overrides_reach_the_owned_probes(self):
        manifest = self.compare(
            timeout=8.5, refresh_timeout=12.5, page_timeout=3.5,
            tableau_desktop_path="tableau-override.exe",
            powerbi_desktop_path="powerbi-override.exe")
        self.assertEqual(manifest["status"], "captured")
        self.assertEqual(self.tableau.call_args.kwargs["timeout"], 8.5)
        self.assertEqual(self.tableau.call_args.kwargs["desktop_path"],
                         "tableau-override.exe")
        options = self.probe.call_args.kwargs
        self.assertEqual(options["timeout"], 8.5)
        self.assertEqual(options["refresh_timeout"], 12.5)
        self.assertEqual(options["desktop_path"], "powerbi-override.exe")
        self.assertIs(options["refresh"], True)
        self.assertIs(options["verify_data"], True)
        self.assertIs(options["close_after"], False)

    def test_invalid_refresh_deadlines_fail_before_pairing_launch_or_write(self):
        for timeout in (0, -1, float("nan"), float("inf"), -float("inf")):
            with self.subTest(timeout=timeout), \
                    mock.patch.object(comparison, "pair_workbooks") as scan:
                with self.assertRaisesRegex(ValueError, "refresh_timeout"):
                    self.compare(refresh_timeout=timeout)
                scan.assert_not_called()
        self.probe.assert_not_called()
        self.assert_no_tableau()
        self.assertFalse((self.output / "desktop_comparisons").exists())


class TestExternalDesktopEvidenceLayout(DesktopEvidenceFixture):
    def snapshot(self, directory):
        return {p.relative_to(directory): p.read_bytes()
                for p in directory.rglob("*") if p.is_file()}

    def assert_layout(self, manifest, *, external, comparison_root):
        self.assertEqual(manifest["status"], "captured")
        self.assertEqual(manifest["schema_version"], 3)
        run = Path(manifest["run_dir"])
        self.assertEqual(run.parent, comparison_root)
        self.assertEqual(manifest["evidence_location"], {
            "layout_version": 1, "comparison_root": str(comparison_root),
            "screenshot_root": str(run / "screenshots"),
            "external_to_output_root": external,
        })
        self.assertEqual(json.loads((run / "manifest.json").read_text("utf-8")), manifest)
        for row in manifest["workbooks"]:
            shots = run / "screenshots" / row["evidence_id"]
            self.assertEqual(Path(row["screenshot_dir"]), shots)
            self.assertRegex(row["evidence_id"], r"^[0-9a-f]{64}$")
            for label, subdir in (("tableau", "source"), ("powerbi", "output")):
                captures = [row[label], *[
                    page["capture"] for page in row[
                        "source_inventory" if label == "tableau" else "target_inventory"]]]
                for capture in captures:
                    path = Path(capture["screenshot"])
                    self.assertTrue(path.is_file())
                    self.assertTrue(path.is_relative_to(shots / subdir))
                    self.assertFalse(path.is_relative_to(Path(row["pbip"]).parent))
        return run

    def test_batch_initial_and_page_evidence_never_modify_the_pbip_project(self):
        before = self.snapshot(self.pbip.parent)
        legacy = self.write(
            self.pbip.parent / "screenshots" / "legacy.txt", "keep legacy evidence")
        before[legacy.relative_to(self.pbip.parent)] = legacy.read_bytes()
        manifest = self.compare(all_pages=True)
        self.assert_layout(
            manifest, external=False, comparison_root=self.output / "desktop_comparisons")
        self.assertEqual(self.snapshot(self.pbip.parent), before)

    def test_standalone_pbip_root_uses_sibling_tree_and_preserves_legacy_evidence(self):
        standalone = self.pbip.parent
        self.write(standalone / "desktop_comparisons" / "old-run" / "manifest.json",
                   '{"schema_version":2,"status":"captured"}')
        self.write(standalone / "desktop_comparisons" / "old-run" / "comparison.html",
                   "<html>Legacy evidence</html>")
        before = self.snapshot(standalone)
        manifest = comparison.compare_desktop_reports(
            str(self.source_dir), str(standalone), all_pages=True,
            workbook_pairs=[copy.deepcopy(self.pair)])
        self.assert_layout(
            manifest, external=True,
            comparison_root=standalone.with_name(
                standalone.name + ".desktop_comparisons"))
        self.assertEqual(self.snapshot(standalone), before)

    def test_equal_display_names_use_distinct_stable_pair_directories_across_runs(self):
        second_source = self.write(
            self.source_dir / "other" / "Sales.twb", self.source.read_text("utf-8"))
        second_pbip = self.write(
            self.output / "other" / "Current" / "Current.pbip",
            self.pbip.read_text("utf-8"))
        # A second valid report tree is needed; identical names are deliberate.
        original_pages = self.pbip.parent / "Current.Report"
        for path in original_pages.rglob("*"):
            if path.is_file():
                self.write(second_pbip.parent / path.relative_to(self.pbip.parent),
                           path.read_text("utf-8"))
        second_pair = dict(
            self.pair, source=str(second_source.resolve()), pbip=str(second_pbip.resolve()),
            candidates=[str(second_pbip.resolve())], relative_source="region/Sales.twb")
        pairs = [copy.deepcopy(self.pair), second_pair]
        first = comparison.compare_desktop_reports(
            str(self.source_dir), str(self.output), all_pages=True, workbook_pairs=pairs)
        first_run = self.assert_layout(
            first, external=False, comparison_root=self.output / "desktop_comparisons")
        before = self.snapshot(first_run)
        second = comparison.compare_desktop_reports(
            str(self.source_dir), str(self.output), all_pages=True,
            workbook_pairs=list(reversed(pairs)))
        self.assert_layout(
            second, external=False, comparison_root=self.output / "desktop_comparisons")
        self.assertNotEqual(first["run_dir"], second["run_dir"])
        self.assertEqual(self.snapshot(first_run), before)
        self.assertEqual(
            {row["source"]: row["evidence_id"] for row in first["workbooks"]},
            {row["source"]: row["evidence_id"] for row in second["workbooks"]})
        self.assertEqual(len({row["screenshot_dir"] for row in first["workbooks"]}), 2)

    def test_fixed_run_identity_collision_never_overwrites_or_recaptures(self):
        from datetime import datetime, timezone
        with mock.patch.object(comparison, "datetime") as clock, \
                mock.patch.object(comparison.uuid, "uuid4",
                                  return_value=mock.Mock(hex="synthetic-run")):
            clock.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
            first = self.compare(all_pages=True)
            run = Path(first["run_dir"])
            before = self.snapshot(run)
            self.probe.reset_mock()
            self.tableau.reset_mock()
            self.tabs.reset_mock()
            with self.assertRaises(FileExistsError):
                self.compare(all_pages=True)
        self.assertEqual(self.snapshot(run), before)
        self.probe.assert_not_called()
        self.tableau.assert_not_called()
        self.tabs.assert_not_called()

    def test_missing_tooling_preflight_writes_failed_reports_without_any_native_work(self):
        failure = {
            "status": "tooling_unavailable", "cause": "powershell_not_found",
            "note": "powershell_not_found", "retryable": False}
        self.preflight.return_value = failure
        manifest = self.compare(all_pages=True)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["preflight"], failure)
        row, = manifest["workbooks"]
        for label in ("tableau", "powerbi"):
            self.assertEqual(row[label]["status"], "tooling_unavailable")
            self.assertEqual(row[label]["native_error"], failure)
            self.assertIsNone(row[label]["screenshot"])
        self.assertEqual(row["powerbi"]["refresh"]["status"], "not_run")
        self.assertEqual(row["powerbi"]["data_load"]["status"], "not_run")
        run = Path(manifest["run_dir"])
        self.assertTrue((run / "comparison.html").is_file())
        self.assertEqual(json.loads((run / "manifest.json").read_text("utf-8")), manifest)
        self.assertIn("powershell_not_found", (run / "comparison.html").read_text("utf-8"))
        self.assertFalse(Path(row["screenshot_dir"]).exists())
        self.preflight.assert_called_once()
        self.tableau.assert_not_called()
        self.probe.assert_not_called()
        self.tabs.assert_not_called()
        self.tableau_inventory.assert_not_called()
        self.tableau_cleanup.assert_not_called()
        self.powerbi_cleanup.assert_not_called()


class TestPowerBIOnlyDesktopEvidence(DesktopEvidenceFixture):
    def test_initial_capture_is_success_without_any_tableau_action(self):
        manifest = self.compare(capture_tableau=False)
        self.assertEqual(manifest["status"], "captured")
        self.assertIs(manifest["capture_tableau"], False)
        self.assertIsNone(manifest["parity_score"])
        row, = manifest["workbooks"]
        self.assertEqual(row["powerbi"]["status"], "captured")
        self.assertNotEqual(row["tableau"]["status"], "captured")
        self.assert_no_tableau()
        self.powerbi_cleanup.assert_called_once_with(222)
        run_dir = Path(manifest["run_dir"])
        self.assertEqual(json.loads((run_dir / "manifest.json").read_text("utf-8")),
                         manifest)
        document = _Headings((run_dir / "comparison.html").read_text("utf-8"))
        self.assertTrue(any("Power BI" in value for value in document.headings),
                        document.headings)
        self.assertFalse(any("Tableau" in value for value in document.panels),
                         document.panels)

    def test_all_pages_inventories_and_navigates_only_the_target(self):
        manifest = self.compare(
            capture_tableau=False, all_pages=True, page_timeout=2.5)
        self.assertEqual(manifest["status"], "captured")
        row, = manifest["workbooks"]
        self.assertEqual(row.get("source_inventory", []), [])
        self.assertEqual([page["name"] for page in row["target_inventory"]],
                         ["Overview", "Detail"])
        self.assertEqual(self.tabs.call_count, 2)
        for call in self.tabs.call_args_list:
            self.assertEqual(call.args[1], 222)
            self.assertEqual(call.kwargs["timeout"], 2.5)
        self.assert_no_tableau()
        self.powerbi_cleanup.assert_called_once_with(222)
        page_indices = [i for i, event in enumerate(self.events) if event[0] == "page"]
        cleanup_index = self.events.index(("cleanup", "powerbi", 222))
        self.assertTrue(all(index < cleanup_index for index in page_indices))
        document = _Headings(
            (Path(manifest["run_dir"]) / "comparison.html").read_text("utf-8"))
        self.assertFalse(any("Tableau" in value for value in document.panels),
                         document.panels)
        self.assertTrue(any("Power BI" in value for value in document.headings),
                        document.headings)

    def test_local_pixels_without_tableau_are_rejected_not_silently_ignored(self):
        with self.assertRaisesRegex(ValueError, "Tableau"):
            self.compare(capture_tableau=False, screen_comparison="local")
        self.probe.assert_not_called()
        self.assert_no_tableau()
        self.assertFalse((self.output / "desktop_comparisons").exists())

    def test_page_exceptions_keep_failure_and_close_the_owned_powerbi_launch(self):
        self.tabs.side_effect = RuntimeError("synthetic tab capture failed")
        manifest = self.compare(capture_tableau=False, all_pages=True)
        self.assertEqual(manifest["status"], "failed")
        row, = manifest["workbooks"]
        self.assertTrue(row["target_inventory"])
        self.assertTrue(all(page["capture"]["status"] != "captured"
                            for page in row["target_inventory"]))
        self.powerbi_cleanup.assert_called_once_with(222)
        self.assert_no_tableau()

    def test_unavailable_capture_retains_raw_status_and_does_not_invent_success(self):
        self.overrides.update(
            status="unavailable", pid=None, screenshot=None,
            screenshot_data_verified=False,
            refresh={"status": "unavailable"}, data_load=_counts("unavailable",
                                                               checked=0,
                                                               nonempty=0,
                                                               empty=0))
        manifest = self.compare(capture_tableau=False, all_pages=True)
        self.assertEqual(manifest["status"], "failed")
        row, = manifest["workbooks"]
        self.assertEqual(row["powerbi"]["probe_status"], "unavailable")
        self.assertEqual(row["powerbi"]["refresh"]["status"], "unavailable")
        self.assertEqual(row["powerbi"]["data_load"]["status"], "unavailable")
        self.assertNotEqual(row["powerbi"]["status"], "captured")
        self.tabs.assert_not_called()
        self.powerbi_cleanup.assert_not_called()
        self.assert_no_tableau()


class TestVerifiedRefreshEvidenceGate(DesktopEvidenceFixture):
    def test_busy_pending_error_and_unavailable_refresh_block_existing_rows_and_png(self):
        for status in ("busy", "pending_changes", "refresh_error",
                       "unavailable", "waiting", "not_requested"):
            with self.subTest(status=status):
                self.overrides["refresh"] = {"status": status, "clicks": 1}
                manifest = self.compare(capture_tableau=False)
                self.assertEqual(manifest["status"], "failed")
                row, = manifest["workbooks"]
                self.assertEqual(row["powerbi"]["probe_status"], "opened")
                self.assertEqual(row["powerbi"]["refresh"]["status"], status)
                self.assertEqual(row["powerbi"]["data_load"], _counts())
                self.assertNotEqual(row["powerbi"]["status"], "captured")
                self.assertIsNone(row["powerbi"]["screenshot"])

    def test_missing_or_false_screenshot_verification_blocks_verified_counts(self):
        for verified in (False, None):
            with self.subTest(verified=verified):
                self.overrides["screenshot_data_verified"] = verified
                manifest = self.compare(capture_tableau=False)
                self.assertEqual(manifest["status"], "failed")
                row, = manifest["workbooks"]
                self.assertEqual(row["powerbi"]["refresh"]["status"], "verified")
                self.assertIs(row["powerbi"]["screenshot_data_verified"], verified)
                self.assertNotEqual(row["powerbi"]["status"], "captured")

    def test_incomplete_failed_or_unavailable_counts_cannot_certify_capture(self):
        for data in (
                _counts(checked=3),
                _counts(checked=2, empty=0, failed=1),
                _counts("query_failed"),
                _counts("unavailable", checked=0, nonempty=0, empty=0),
                {"status": "verified"},
                _counts(checked=True, nonempty=True, empty=0),
        ):
            with self.subTest(data=data):
                self.overrides["data_load"] = data
                manifest = self.compare(capture_tableau=False)
                self.assertEqual(manifest["status"], "failed")
                row, = manifest["workbooks"]
                self.assertNotEqual(row["powerbi"]["status"], "captured")
                self.assertEqual(row["powerbi"]["data_load"], data)

    def test_complete_partial_and_empty_table_results_preserve_their_raw_status(self):
        for data in (_counts("partial", nonempty=1, empty=1),
                     _counts("empty", nonempty=0, empty=2)):
            with self.subTest(status=data["status"]):
                self.overrides["data_load"] = data
                manifest = self.compare(capture_tableau=False)
                self.assertEqual(manifest["status"], "captured")
                row, = manifest["workbooks"]
                self.assertEqual(row["powerbi"]["data_load"], data)
                self.assertEqual(row["powerbi"]["refresh"]["status"], "verified")
                self.assertIs(row["powerbi"]["screenshot_data_verified"], True)

    def test_conflicting_or_incomplete_refresh_checks_cannot_use_top_level_rows(self):
        for data in (_counts(checked=3), _counts("query_failed"),
                     _counts("empty", nonempty=0, empty=2)):
            with self.subTest(refresh_data=data):
                self.overrides["refresh"] = {
                    "status": "verified", "data_load": data, "clicks": 1,
                }
                manifest = self.compare(capture_tableau=False)
                self.assertEqual(manifest["status"], "failed")
                row, = manifest["workbooks"]
                self.assertEqual(row["powerbi"]["data_load"], _counts())
                self.assertEqual(row["powerbi"]["refresh"]["data_load"], data)
                self.assertNotEqual(row["powerbi"]["status"], "captured")

    def test_blocked_refresh_never_enters_initial_local_pixel_comparison(self):
        self.overrides["refresh"] = {"status": "refresh_error", "clicks": 1}
        with mock.patch.object(comparison, "compare_png_files") as pixels:
            manifest = self.compare(screen_comparison="local")
        pixels.assert_not_called()
        self.assertEqual(manifest["status"], "failed")
        row, = manifest["workbooks"]
        self.assertNotEqual(row["pixel_comparison"]["status"], "compared")
        self.assertIsNone(row["pixel_comparison"]["metrics"])
        self.tableau_cleanup.assert_called_once_with(111)
        self.powerbi_cleanup.assert_called_once_with(222)

    def test_blocked_refresh_cannot_gain_success_by_navigating_target_pages(self):
        self.overrides["refresh"] = {"status": "refresh_error", "clicks": 1}
        with mock.patch.object(comparison, "compare_png_files") as pixels:
            manifest = self.compare(all_pages=True, screen_comparison="local")
        pixels.assert_not_called()
        self.assertEqual(manifest["status"], "failed")
        self.assertTrue(all(call.args[1] != 222 for call in self.tabs.call_args_list))
        row, = manifest["workbooks"]
        self.assertTrue(all(page["capture"]["status"] != "captured"
                            for page in row["target_inventory"]))
        self.tableau_cleanup.assert_called_once_with(111)
        self.powerbi_cleanup.assert_called_once_with(222)

    def test_page_capture_exceptions_still_close_both_owned_launches(self):
        self.tabs.side_effect = RuntimeError("synthetic navigation failure")
        manifest = self.compare(all_pages=True)
        self.assertEqual(manifest["status"], "failed")
        self.tableau_cleanup.assert_called_once_with(111)
        self.powerbi_cleanup.assert_called_once_with(222)
        row, = manifest["workbooks"]
        self.assertEqual(row["tableau"]["cleanup"]["status"], "closed")
        self.assertEqual(row["powerbi"]["cleanup"]["status"], "closed")

    def test_exception_escaping_page_workflow_still_runs_owned_cleanup_finally(self):
        with mock.patch.object(
                comparison, "_capture_all_pages",
                side_effect=RuntimeError("synthetic uncaught page workflow failure")):
            manifest = self.compare(all_pages=True)
        self.assertEqual(manifest["status"], "failed")
        self.tableau_cleanup.assert_called_once_with(111)
        self.powerbi_cleanup.assert_called_once_with(222)
        row, = manifest["workbooks"]
        self.assertTrue(any("synthetic uncaught page workflow failure" in issue
                            for issue in row["issues"]))

    def test_probe_result_conversion_exception_keeps_pid_for_final_cleanup(self):
        with mock.patch.object(
                DesktopProbeReport, "to_dict",
                side_effect=RuntimeError("synthetic evidence conversion failed")):
            manifest = self.compare()
        self.assertEqual(manifest["status"], "failed")
        self.tableau_cleanup.assert_called_once_with(111)
        self.powerbi_cleanup.assert_called_once_with(222)
        row, = manifest["workbooks"]
        self.assertEqual(row["powerbi"]["pid"], 222)
        self.assertEqual(row["powerbi"]["cleanup"]["status"], "closed")
