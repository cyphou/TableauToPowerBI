"""Opt-in migrate.py Desktop workflows, with no extraction, network or GUI."""

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import migrate
from powerbi_import import desktop_comparison
from tests.pixel_comparison_fixtures import forbid_network


def _snapshot(root):
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


class MigrationDesktopFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.output = self.root / "output"
        self.events = []
        self.failed_sources = set()
        self.diagnostic_status = "captured"
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.enterContext(redirect_stdout(self.stdout))
        self.enterContext(redirect_stderr(self.stderr))
        forbid_network(self)
        self.enterContext(mock.patch(
            "subprocess.Popen",
            side_effect=AssertionError("Tests must not launch native apps")))
        self.enterContext(mock.patch.object(migrate, "_install_upload_copy_guard"))
        self.logging = self.enterContext(mock.patch.object(migrate, "setup_logging"))
        self.dashboard = self.enterContext(mock.patch.object(
            migrate, "run_batch_html_dashboard"))
        self.worker = self.enterContext(mock.patch.object(
            migrate, "_migrate_single_workbook", side_effect=self.migrate_workbook))
        self.extract = self.enterContext(mock.patch.object(
            migrate, "run_extraction",
            side_effect=AssertionError("Tests must not extract real workbooks")))
        self.generate = self.enterContext(mock.patch.object(
            migrate, "run_generation",
            side_effect=AssertionError("Tests must not generate real projects")))
        self.compare = self.enterContext(mock.patch.object(
            desktop_comparison, "compare_desktop_reports",
            side_effect=self.capture_evidence))

    def source(self, name):
        path = self.sources / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<workbook/>", encoding="utf-8")
        return path

    @staticmethod
    def publish(output, name):
        pbip = Path(output) / name / f"{name}.pbip"
        pbip.parent.mkdir(parents=True, exist_ok=True)
        pbip.write_text('{"public_test_project": true}', encoding="utf-8")
        return pbip

    def migrate_workbook(self, **options):
        source = Path(options["tableau_file"])
        self.events.append(("worker", source.name))
        result = {
            "success": source.stem not in self.failed_sources,
            "report_name": options["basename"],
            "output_dir": options["workbook_output_dir"],
        }
        if source.suffix.lower() in {".tfl", ".tflx"}:
            result["prep_flow"] = True
        elif result["success"]:
            self.publish(options["workbook_output_dir"], options["basename"])
        return result

    def capture_evidence(self, source_dir, output_root, **options):
        self.events.append(("diagnostic", output_root))
        pairs = copy.deepcopy(options["workbook_pairs"])
        for pair in pairs:
            self.assertTrue(Path(pair["source"]).is_file())
            self.assertTrue(Path(pair["pbip"]).is_file())
            pair["powerbi"] = {"status": "captured", "refresh": {"status": "verified"}}
            pair["tableau"] = {
                "status": "captured" if options["capture_tableau"] else "not_requested"}
        run_dir = Path(output_root) / "desktop_comparisons" / f"mock-{self.compare.call_count}"
        run_dir.mkdir(parents=True)
        manifest = {
            "status": self.diagnostic_status, "run_dir": str(run_dir),
            "workbooks": pairs, "parity_score": None,
        }
        (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (run_dir / "comparison.html").write_text("<html>Public test evidence</html>",
                                                encoding="utf-8")
        return manifest

    def main(self, argv):
        with mock.patch.object(sys, "argv", ["migrate.py", *map(str, argv)]):
            return migrate.main()

    def batch(self, **options):
        return migrate.run_batch_migration(
            str(self.sources), output_dir=str(self.output),
            skip_extraction=True, verify_open=False, **options)

    def assert_pair(self, pair, source, pbip, *, source_root=None):
        source_root = source_root or self.sources
        self.assertEqual(pair, {
            "source": str(source.resolve()),
            "relative_source": os.path.relpath(source, source_root).replace(os.sep, "/"),
            "pair_status": "paired", "pbip": str(pbip.resolve()),
            "candidates": [str(pbip.resolve())], "issues": [],
        })

    def assert_cli_rejected(self, argv):
        before = _snapshot(self.root)
        with mock.patch.object(migrate, "run_batch_migration") as batch, \
                mock.patch.object(migrate, "_run_batch_config") as config, \
                mock.patch.object(migrate, "_run_single_inspection") as inspect:
            with self.assertRaises(SystemExit) as raised:
                self.main(argv)
            self.assertEqual(raised.exception.code, 2)
        batch.assert_not_called()
        config.assert_not_called()
        inspect.assert_not_called()
        self.worker.assert_not_called()
        self.extract.assert_not_called()
        self.generate.assert_not_called()
        self.compare.assert_not_called()
        self.assertEqual(_snapshot(self.root), before)


class TestDesktopWorkflowCLI(MigrationDesktopFixture):
    def test_defaults_are_nonlaunching_and_keep_existing_static_preflight(self):
        args = migrate._build_argument_parser().parse_args(["--batch", str(self.sources)])
        self.assertFalse(args.desktop_probe)
        self.assertFalse(args.compare_desktop)
        self.assertFalse(args.all_pages)
        self.assertTrue(args.verify_open)
        self.assertEqual((args.desktop_timeout, args.refresh_timeout, args.page_timeout),
                         (90, 300, 30))
        self.assertEqual(args.screen_comparison, "manual")
        self.assertIsNone(args.tableau_desktop_path)
        self.assertIsNone(args.powerbi_desktop_path)
        with mock.patch.object(migrate, "run_batch_migration", return_value=0) as batch:
            self.assertEqual(self.main(["migrate", self.sources]), 0)
        self.assertIsNone(batch.call_args.kwargs["desktop_options"])
        self.compare.assert_not_called()

    def test_screenshots_and_legacy_alias_use_desktop_probe_destination(self):
        for flag in ("--screenshots", "--desktop-probe"):
            with self.subTest(flag=flag):
                args = migrate._build_argument_parser().parse_args(
                    ["--batch", str(self.sources), flag])
                self.assertIs(args.desktop_probe, True)
                self.assertFalse(args.compare_desktop)
                self.assertFalse(hasattr(args, "screenshots"))
                with mock.patch.object(migrate, "run_batch_migration",
                                       return_value=0) as batch:
                    self.assertEqual(self.main(["migrate", self.sources, flag]), 0)
                self.assertEqual(batch.call_args.kwargs["desktop_options"],
                                 migrate.DesktopDiagnosticOptions())
        self.compare.assert_not_called()

    def test_primary_modes_are_mutually_exclusive_even_with_legacy_alias(self):
        for flag in ("--screenshots", "--desktop-probe"):
            with self.subTest(flag=flag):
                self.assert_cli_rejected(
                    ["--batch", self.sources, flag, "--compare-desktop"])

    def test_nonpositive_nonfinite_and_nonnumeric_timeouts_are_parse_errors(self):
        for flag in ("--desktop-timeout", "--refresh-timeout", "--page-timeout"):
            for value in ("0", "-1", "nan", "inf", "-inf", "not-seconds"):
                with self.subTest(flag=flag, value=value):
                    self.assert_cli_rejected(
                        ["--batch", self.sources, "--screenshots", flag, value])

    def test_explicit_orphan_modifiers_are_rejected_even_at_default_values(self):
        for modifier in (
                ["--all-pages"], ["--desktop-timeout", "90"],
                ["--refresh-timeout", "300"], ["--page-timeout", "30"],
                ["--screen-comparison", "manual"], ["--screen-comparison", "local"],
                ["--tableau-desktop-path", "tableau.exe"],
                ["--powerbi-desktop-path", "PBIDesktop.exe"],
        ):
            with self.subTest(modifier=modifier):
                self.assert_cli_rejected(["--batch", self.sources, *modifier])

    def test_tableau_override_and_any_screen_mode_require_paired_comparison(self):
        for modifier in (
                ["--tableau-desktop-path", "tableau.exe"],
                ["--screen-comparison", "manual"], ["--screen-comparison", "local"],
        ):
            with self.subTest(modifier=modifier):
                self.assert_cli_rejected(
                    ["--batch", self.sources, "--screenshots", *modifier])

    def test_comparison_forwards_finite_timeouts_page_scope_and_both_overrides(self):
        with mock.patch.object(migrate, "run_batch_migration", return_value=0) as batch:
            self.assertEqual(self.main([
                "migrate", self.sources, "--compare-desktop", "--all-pages",
                "--desktop-timeout", "0.25", "--refresh-timeout", "12.5",
                "--page-timeout", "1.5", "--screen-comparison", "local",
                "--tableau-desktop-path", "tableau-custom.exe",
                "--powerbi-desktop-path", "powerbi-custom.exe",
            ]), 0)
        self.assertEqual(batch.call_args.kwargs["desktop_options"],
                         migrate.DesktopDiagnosticOptions(
                             capture_tableau=True, timeout=0.25,
                             refresh_timeout=12.5, all_pages=True, page_timeout=1.5,
                             screen_comparison="local",
                             tableau_desktop_path="tableau-custom.exe",
                             powerbi_desktop_path="powerbi-custom.exe"))

    def test_manual_comparison_is_valid_but_unknown_screen_algorithms_are_not(self):
        with mock.patch.object(migrate, "run_batch_migration", return_value=0) as batch:
            self.assertEqual(self.main([
                "--batch", self.sources, "--compare-desktop",
                "--screen-comparison", "manual",
            ]), 0)
        self.assertTrue(batch.call_args.kwargs["desktop_options"].capture_tableau)
        self.assertEqual(batch.call_args.kwargs["desktop_options"].screen_comparison,
                         "manual")
        for mode in ("ai", "ssim", "LOCAL", ""):
            with self.subTest(mode=mode):
                self.assert_cli_rejected([
                    "--batch", self.sources, "--compare-desktop",
                    "--screen-comparison", mode,
                ])

    def test_fabric_shared_model_and_inspection_routes_reject_before_work(self):
        first = self.source("Sales.twb")
        second = self.source("Finance.twb")
        for argv in (
                ["fabric", self.sources, "--screenshots"],
                ["--batch", self.sources, "--compare-desktop",
                 "--output-format", "fabric"],
                ["--batch", self.sources, "--screenshots", "--prep-to-dataflow"],
                ["--shared-model", first, second, "--screenshots"],
                ["assess", self.sources, "--screenshots"],
                ["parity", first, "--compare-desktop"],
                ["quality", first, "--screenshots"],
                ["--bulk-assess", self.sources, "--compare-desktop"],
        ):
            with self.subTest(argv=argv):
                self.assert_cli_rejected(argv)

    def test_batch_dry_run_never_launches_extracts_or_writes(self):
        self.source("Sales.twb")
        before = _snapshot(self.root)
        result = self.main([
            "migrate", self.sources, "--screenshots", "--all-pages", "--dry-run",
            "--output-dir", self.output, "--jsonl-log", self.root / "events.jsonl",
            "--log-file", self.root / "migration.log",
        ])
        self.assertEqual(result, migrate.ExitCode.SUCCESS)
        self.assertEqual(_snapshot(self.root), before)
        self.worker.assert_not_called()
        self.extract.assert_not_called()
        self.generate.assert_not_called()
        self.compare.assert_not_called()
        self.assertIsNone(self.logging.call_args.kwargs["log_file"])
        self.assertIn("DRY RUN", self.stdout.getvalue())

    def test_manifest_route_keeps_entries_and_forwards_desktop_options(self):
        self.source("Sales.twb")
        entries = [{"file": "Sales.twb", "culture": "fr-FR", "mode": "import"}]
        manifest = self.sources / "portfolio.json"
        manifest.write_text(json.dumps(entries), encoding="utf-8")
        with mock.patch.object(migrate, "run_batch_migration", return_value=0) as batch:
            self.assertEqual(self.main([
                "--manifest", manifest, "--compare-desktop",
                "--refresh-timeout", "18.5",
            ]), 0)
        self.assertEqual(batch.call_args.kwargs["batch_dir"], str(self.sources))
        self.assertEqual(batch.call_args.kwargs["manifest"], entries)
        self.assertEqual(batch.call_args.kwargs["desktop_options"],
                         migrate.DesktopDiagnosticOptions(
                             capture_tableau=True, refresh_timeout=18.5))
        self.assertEqual(json.loads(manifest.read_text("utf-8")), entries)

    def test_server_dry_run_never_authenticates_downloads_or_launches(self):
        before = _snapshot(self.root)
        with mock.patch.object(migrate, "_download_from_server") as download, \
                mock.patch.object(migrate, "_handle_enterprise_server_ops") as server:
            result = self.main([
                "--server", "https://tableau.invalid", "--workbook", "Sales",
                "--screenshots", "--dry-run", "--output-dir", self.output,
            ])
        self.assertEqual(result, migrate.ExitCode.SUCCESS)
        download.assert_not_called()
        server.assert_not_called()
        self.worker.assert_not_called()
        self.compare.assert_not_called()
        self.assertEqual(_snapshot(self.root), before)


class TestPostGenerationDesktopBatch(MigrationDesktopFixture):
    def test_default_batch_never_invokes_desktop_diagnostics(self):
        self.source("Sales.twb")
        self.assertEqual(self.batch(), migrate.ExitCode.SUCCESS)
        self.worker.assert_called_once()
        self.compare.assert_not_called()

    def test_parallel_workers_finish_before_one_sequential_exact_pair_diagnostic(self):
        first = self.source("region/Sales.twb")
        second = self.source("Finance.twbx")
        self.source("Broken.twb")
        self.source("OnlyPrep.tfl")
        self.source("Datasource.tds")
        self.failed_sources.add("Broken")
        self.publish(self.output, "UnrelatedStale")
        options = migrate.DesktopDiagnosticOptions(
            capture_tableau=True, all_pages=True, timeout=7.5,
            refresh_timeout=14.5, page_timeout=2.5, screen_comparison="local",
            tableau_desktop_path="tableau.exe", powerbi_desktop_path="powerbi.exe")
        result = self.batch(parallel=2, desktop_options=options)
        self.assertEqual(result, migrate.ExitCode.BATCH_PARTIAL_FAIL)
        self.assertEqual(self.worker.call_count, 5)
        self.compare.assert_called_once()
        call = self.compare.call_args
        self.assertEqual(call.args, (str(self.sources.resolve()), str(self.output.resolve())))
        pairs = {pair["source"]: pair for pair in call.kwargs["workbook_pairs"]}
        self.assertEqual(set(pairs), {str(first.resolve()), str(second.resolve())})
        self.assert_pair(pairs[str(first.resolve())], first,
                         self.output / "region" / "Sales" / "Sales.pbip")
        self.assert_pair(pairs[str(second.resolve())], second,
                         self.output / "Finance" / "Finance.pbip")
        self.assertEqual({key: value for key, value in call.kwargs.items()
                          if key != "workbook_pairs"}, {
            "timeout": 7.5, "all_pages": True, "page_timeout": 2.5,
            "screen_comparison": "local", "tableau_desktop_path": "tableau.exe",
            "powerbi_desktop_path": "powerbi.exe", "refresh_timeout": 14.5,
            "capture_tableau": True,
        })
        diagnostic = next(i for i, event in enumerate(self.events)
                          if event[0] == "diagnostic")
        self.assertTrue(all(i < diagnostic for i, event in enumerate(self.events)
                            if event[0] == "worker"))

    def test_diagnostic_failure_returns_eight_without_retracting_valid_pbip_or_checkpoint(self):
        self.source("Sales.twb")
        self.diagnostic_status = "failed"
        result = self.batch(desktop_options=migrate.DesktopDiagnosticOptions())
        self.assertEqual(result, 8)
        self.assertEqual(result, migrate.ExitCode.DESKTOP_DIAGNOSTIC_FAILED)
        pbip = self.output / "Sales" / "Sales.pbip"
        self.assertEqual(pbip.read_text("utf-8"), '{"public_test_project": true}')
        state = json.loads((self.output / ".migration_batch_state.json").read_text("utf-8"))
        self.assertEqual(state["Sales.twb"]["status"], "success")
        self.assertTrue(state["Sales.twb"]["result"]["success"])

    def test_diagnostic_exception_is_nonzero_and_preserves_a_successful_project(self):
        self.source("Sales.twb")
        self.compare.side_effect = OSError("synthetic evidence write failed")
        self.assertEqual(self.batch(desktop_options=migrate.DesktopDiagnosticOptions()),
                         migrate.ExitCode.DESKTOP_DIAGNOSTIC_FAILED)
        self.assertTrue((self.output / "Sales" / "Sales.pbip").is_file())
        self.assertIn("synthetic evidence write failed", self.stdout.getvalue())

    def test_diagnostics_cannot_replace_an_existing_migration_failure_exit(self):
        self.source("Sales.twb")
        self.source("Broken.twb")
        self.failed_sources.add("Broken")
        for status in ("captured", "failed"):
            with self.subTest(status=status):
                self.diagnostic_status = status
                self.assertEqual(
                    self.batch(desktop_options=migrate.DesktopDiagnosticOptions()),
                    migrate.ExitCode.BATCH_PARTIAL_FAIL)
                self.assertTrue((self.output / "Sales" / "Sales.pbip").is_file())
                pair, = self.compare.call_args.kwargs["workbook_pairs"]
                self.assertEqual(Path(pair["source"]).name, "Sales.twb")
        self.compare.side_effect = RuntimeError("synthetic capture failed")
        self.assertEqual(self.batch(desktop_options=migrate.DesktopDiagnosticOptions()),
                         migrate.ExitCode.BATCH_PARTIAL_FAIL)
        self.assertTrue((self.output / "Sales" / "Sales.pbip").is_file())

    def test_all_resumed_outputs_still_get_exact_current_diagnostics(self):
        first = self.source("region/Sales.twb")
        second = self.source("Finance.twbx")
        self.assertEqual(self.batch(), migrate.ExitCode.SUCCESS)
        self.worker.reset_mock()
        self.compare.reset_mock()
        self.events.clear()
        self.assertEqual(self.batch(
            resume=True, desktop_options=migrate.DesktopDiagnosticOptions()),
            migrate.ExitCode.SUCCESS)
        self.worker.assert_not_called()
        self.compare.assert_called_once()
        pairs = {pair["source"]: pair
                 for pair in self.compare.call_args.kwargs["workbook_pairs"]}
        self.assertEqual(set(pairs), {str(first.resolve()), str(second.resolve())})
        self.assert_pair(pairs[str(first.resolve())], first,
                         self.output / "region" / "Sales" / "Sales.pbip")
        self.assert_pair(pairs[str(second.resolve())], second,
                         self.output / "Finance" / "Finance.pbip")

    def test_mixed_resume_new_failure_and_prep_exclude_stale_or_nonworkbook_outputs(self):
        old = self.source("Old.twb")
        self.assertEqual(self.batch(), migrate.ExitCode.SUCCESS)
        new = self.source("nested/New.twbx")
        self.source("Broken.twb")
        self.source("OnlyPrep.tfl")
        self.source("Datasource.tds")
        self.failed_sources.add("Broken")
        self.publish(self.output, "Stale")
        self.worker.reset_mock()
        self.assertEqual(self.batch(
            resume=True, desktop_options=migrate.DesktopDiagnosticOptions()),
            migrate.ExitCode.BATCH_PARTIAL_FAIL)
        migrated = {Path(call.kwargs["tableau_file"]).name
                    for call in self.worker.call_args_list}
        self.assertNotIn("Old.twb", migrated)
        pairs = self.compare.call_args.kwargs["workbook_pairs"]
        self.assertEqual({pair["source"] for pair in pairs},
                         {str(old.resolve()), str(new.resolve())})
        self.assertEqual(len(pairs), 2)

    def test_no_successful_workbooks_never_falls_back_to_scanning_stale_pbips(self):
        self.source("Broken.twb")
        self.failed_sources.add("Broken")
        self.publish(self.output, "Stale")
        self.assertEqual(self.batch(desktop_options=migrate.DesktopDiagnosticOptions()),
                         migrate.ExitCode.BATCH_PARTIAL_FAIL)
        self.compare.assert_not_called()

    def test_prep_only_batch_rejects_desktop_before_workers_or_output_creation(self):
        self.source("OnlyPrep.tfl")
        before = _snapshot(self.root)
        self.assertEqual(self.batch(desktop_options=migrate.DesktopDiagnosticOptions()),
                         migrate.ExitCode.DESKTOP_DIAGNOSTIC_FAILED)
        self.worker.assert_not_called()
        self.compare.assert_not_called()
        self.assertEqual(_snapshot(self.root), before)

    def test_invalid_python_api_options_reject_before_any_output_or_worker(self):
        self.source("Sales.twb")
        before = _snapshot(self.root)
        for options, output_format in (
                ({}, "pbip"),
                (migrate.DesktopDiagnosticOptions(timeout=float("nan")), "pbip"),
                (migrate.DesktopDiagnosticOptions(refresh_timeout=False), "pbip"),
                (migrate.DesktopDiagnosticOptions(page_timeout=0), "pbip"),
                (migrate.DesktopDiagnosticOptions(screen_comparison="local"), "pbip"),
                (migrate.DesktopDiagnosticOptions(), "fabric"),
        ):
            with self.subTest(options=options, output_format=output_format):
                with self.assertRaises(migrate.ConfigurationError):
                    self.batch(desktop_options=options, output_format=output_format)
        self.worker.assert_not_called()
        self.compare.assert_not_called()
        self.assertEqual(_snapshot(self.root), before)


class TestBatchConfigDesktopWiring(MigrationDesktopFixture):
    def setUp(self):
        super().setUp()
        self.generation_failures = set()
        self.generate.side_effect = self.generate_config_project
        self.extract.side_effect = None
        self.extract.return_value = True
        self.enterContext(mock.patch.object(migrate, "run_migration_report", return_value={}))
        for name in (
                "_stage_declared_local_sources", "_collect_local_data_files",
                "_convert_hyper_to_csv_in_data", "_process_twbx_post_generation",
                "_sync_final_calendar_source_expressions", "_export_power_query_files",
                "_shorten_report_artifact_path", "_require_hyper_import"):
            self.enterContext(mock.patch.object(migrate, name, return_value={}))

    def generate_config_project(self, *, report_name, output_dir, **options):
        self.events.append(("generation", report_name))
        if report_name in self.generation_failures:
            return False
        self.publish(output_dir, report_name)
        return True

    def config(self, entries, *flags):
        path = self.root / "batch.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        return self.main([
            "--batch-config", path, "--output-dir", self.output,
            "--skip-extraction", "--no-verify-open", *flags,
        ])

    def test_groups_exact_successes_by_output_and_excludes_failed_stale_and_prep(self):
        first = self.source("Sales.twb")
        second = self.source("nested/Finance.twbx")
        broken = self.source("Broken.twb")
        prep = self.source("OnlyPrep.tfl")
        self.generation_failures.add("Broken")
        other_output = self.root / "other-output"
        self.publish(self.output, "Stale")
        entries = [
            {"file": str(first)},
            {"file": str(second), "output_dir": str(other_output)},
            {"file": str(broken)}, {"file": str(prep)},
        ]
        result = self.config(entries, "--compare-desktop", "--refresh-timeout", "11.5")
        self.assertEqual(result, migrate.ExitCode.BATCH_PARTIAL_FAIL)
        self.assertEqual(self.generate.call_count, 4)
        self.assertEqual(self.compare.call_count, 2)
        calls = {call.args[1]: call for call in self.compare.call_args_list}
        for source, output in ((first, self.output), (second, other_output)):
            call = calls[str(output.resolve())]
            self.assertEqual(call.args[0], str(self.root.resolve()))
            self.assertTrue(call.kwargs["capture_tableau"])
            self.assertEqual(call.kwargs["refresh_timeout"], 11.5)
            pair, = call.kwargs["workbook_pairs"]
            self.assert_pair(pair, source, output / source.stem / f"{source.stem}.pbip",
                             source_root=self.root)
        first_diagnostic = next(i for i, event in enumerate(self.events)
                                if event[0] == "diagnostic")
        self.assertTrue(all(i < first_diagnostic for i, event in enumerate(self.events)
                            if event[0] == "generation"))

    def test_diagnostic_failure_in_one_group_does_not_skip_other_groups_or_remove_pbips(self):
        first = self.source("Sales.twb")
        second = self.source("Finance.twb")
        other_output = self.root / "other-output"
        self.diagnostic_status = "failed"
        result = self.config([
            {"file": str(first)},
            {"file": str(second), "output_dir": str(other_output)},
        ], "--screenshots")
        self.assertEqual(result, migrate.ExitCode.DESKTOP_DIAGNOSTIC_FAILED)
        self.assertEqual(self.compare.call_count, 2)
        self.assertTrue((self.output / "Sales" / "Sales.pbip").is_file())
        self.assertTrue((other_output / "Finance" / "Finance.pbip").is_file())

    def test_later_failed_rewrite_invalidates_earlier_pair_instead_of_capturing_stale_pbip(self):
        first = self.source("a/Sales.twb")
        second = self.source("b/Sales.twb")
        calls = []

        def generate(**options):
            calls.append(options)
            return (self.generate_config_project(**options) if len(calls) == 1
                    else False)

        self.generate.side_effect = generate
        result = self.config([{"file": str(first)}, {"file": str(second)}],
                             "--screenshots")
        self.assertEqual(result, migrate.ExitCode.BATCH_PARTIAL_FAIL)
        self.assertEqual(self.generate.call_count, 2)
        self.compare.assert_not_called()

    def test_later_successful_rewrite_pairs_only_the_current_source(self):
        first = self.source("a/Sales.twb")
        second = self.source("b/Sales.twb")
        self.assertEqual(self.config([
            {"file": str(first)}, {"file": str(second)},
        ], "--screenshots"), migrate.ExitCode.SUCCESS)
        self.compare.assert_called_once()
        pair, = self.compare.call_args.kwargs["workbook_pairs"]
        self.assert_pair(pair, second, self.output / "Sales" / "Sales.pbip",
                         source_root=self.root)

    def test_batch_config_dry_run_has_no_generation_evidence_or_filesystem_writes(self):
        workbook = self.source("Sales.twb")
        config = self.root / "batch.json"
        config.write_text(json.dumps([{"file": str(workbook)}]), encoding="utf-8")
        before = _snapshot(self.root)
        self.assertEqual(self.main([
            "--batch-config", config, "--output-dir", self.output,
            "--compare-desktop", "--all-pages", "--dry-run",
        ]), migrate.ExitCode.SUCCESS)
        self.generate.assert_not_called()
        self.extract.assert_not_called()
        self.worker.assert_not_called()
        self.compare.assert_not_called()
        self.assertEqual(_snapshot(self.root), before)


class TestDesktopDiagnosticSummaries(unittest.TestCase):
    def summary(self, record):
        before = copy.deepcopy(record)
        output = io.StringIO()
        with redirect_stdout(output):
            migrate._print_desktop_statuses(record, "Sales.twb")
        self.assertEqual(record, before)
        return output.getvalue()

    def test_raw_stage_statuses_and_page_totals_do_not_dump_nested_success_evidence(self):
        record = {
            "status": "failed", "pair_status": "paired",
            "tableau": {"status": "captured"},
            "powerbi": {
                "status": "error", "probe_status": "opened",
                "refresh": {"status": "refresh_error"},
                "data_load": {"status": "partial"},
                "capture_gate": {"status": "failed"}, "cleanup": {"status": "closed"},
                "note": "User-visible failure",
            },
            "target_inventory": [
                {"name": f"Page {index}", "capture": {
                    "status": "captured",
                    "selection": {"status": "NESTED_SELECTION_SUCCESS_NOT_FOR_STDOUT"},
                    "post_capture_selection": {
                        "details": "NESTED_POST_CAPTURE_PAYLOAD_NOT_FOR_STDOUT"},
                }}
                for index in range(100)
            ],
        }
        text = self.summary(record)
        for expected in (
                "failed", "paired", "capture=error", "open=opened",
                "refresh=refresh_error", "data_load=partial",
                "capture_gate=failed", "cleanup=closed", "captured=100"):
            self.assertIn(expected, text)
        self.assertNotIn("NESTED_SELECTION_SUCCESS_NOT_FOR_STDOUT", text)
        self.assertNotIn("NESTED_POST_CAPTURE_PAYLOAD_NOT_FOR_STDOUT", text)
        self.assertNotIn("post_capture_selection", text)

    def test_failure_notes_and_issues_are_bounded_with_manifest_omission_counts(self):
        pages = [
            {"name": f"Page {index}", "capture": {
                "status": "timed_out",
                "note": f"PUBLIC_FAILURE_NOTE_{index} " + "x" * 600 + "\nSHOULD_NOT_LEAK",
            }}
            for index in range(12)
        ]
        record = {
            "status": "failed", "pair_status": "paired",
            "source_inventory": pages[:6], "target_inventory": pages[6:],
            "issues": [f"PUBLIC_ISSUE_{index}" for index in range(5)],
        }
        text = self.summary(record)
        for index in range(5):
            self.assertIn(f"PUBLIC_FAILURE_NOTE_{index} ", text)
        for index in range(5, 12):
            self.assertNotIn(f"PUBLIC_FAILURE_NOTE_{index} ", text)
        self.assertIn("7 more page failures; see evidence manifest", text)
        for index in range(3):
            self.assertIn(f"PUBLIC_ISSUE_{index}", text)
        for index in range(3, 5):
            self.assertNotIn(f"PUBLIC_ISSUE_{index}", text)
        self.assertIn("2 more issues; see evidence manifest", text)
        self.assertNotIn("SHOULD_NOT_LEAK", text)
        self.assertNotIn("x" * 241, text)

    def test_unrequested_source_inventory_is_not_presented_as_tableau_page_coverage(self):
        text = self.summary({
            "status": "manual_review", "pair_status": "paired",
            "inventory_requested": {"source": False, "target": True},
            "tableau": {"status": "not_requested"},
            "powerbi": {"status": "captured"},
            "source_inventory": [{"name": "Stale", "capture": {"status": "captured"}}],
            "target_inventory": [{"name": "Current", "capture": {"status": "captured"}}],
        })
        self.assertIn("powerbi pages: captured=1", text)
        self.assertNotIn("tableau pages:", text)
        self.assertNotIn("Stale", text)
