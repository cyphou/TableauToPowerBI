"""CLI validation and exit status; never launches either Desktop app."""
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import compare_desktop_reports as cli


class TestComparisonCLI(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source"
        self.output = self.root / "output"
        self.source.mkdir()
        self.output.mkdir()
        self.args = [str(self.source), str(self.output)]
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.enterContext(redirect_stdout(self.stdout))
        self.enterContext(redirect_stderr(self.stderr))
        self.compare = self.enterContext(mock.patch.object(cli, "compare_desktop_reports"))

    def test_invalid_arguments_exit_two_without_capture(self):
        file = self.root / "single.twb"
        file.touch()
        for args in ([], [str(file), str(self.output)],
                     [str(self.source), str(self.root / "missing")],
                     self.args + ["--unknown"],
                     *(self.args + ["--timeout", value]
                       for value in ("0", "-1", "nan", "inf", "abc"))):
            with self.subTest(args=args), self.assertRaises(SystemExit) as raised:
                cli.main(args)
            self.assertEqual(raised.exception.code, 2)
        self.compare.assert_not_called()

    def test_success_forwards_overrides_and_reports_manual_review(self):
        self.compare.return_value = {
            "status": "captured", "run_dir": str(self.root), "workbooks": []}
        self.assertEqual(cli.main(self.args + [
            "--timeout", "12.5", "--tableau-desktop-path", "tableau.exe",
            "--powerbi-desktop-path", "PBIDesktop.exe"]), 0)
        self.compare.assert_called_once_with(
            str(self.source), str(self.output), timeout=12.5,
            tableau_desktop_path="tableau.exe", powerbi_desktop_path="PBIDesktop.exe",
            all_pages=False, page_timeout=30)
        self.assertIn("manual review only; no parity score", self.stdout.getvalue())
        self.assertIn("manifest.json", self.stdout.getvalue())
        self.assertIn("comparison.html", self.stdout.getvalue())

    def test_all_pages_flag_and_page_timeout_are_forwarded(self):
        self.compare.return_value = {
            "status": "failed", "run_dir": str(self.root), "workbooks": [],
            "totals": {"source": {"inventory": 0, "captured": 0}}}
        self.assertEqual(cli.main(self.args + ["--all-pages", "--page-timeout", "4.5"]), 1)
        self.compare.assert_called_once_with(
            str(self.source), str(self.output), timeout=90,
            tableau_desktop_path=None, powerbi_desktop_path=None,
            all_pages=True, page_timeout=4.5)
        self.assertIn("All-page totals", self.stdout.getvalue())

    def test_invalid_page_timeout_exits_two_without_comparison(self):
        for value in ("0", "-1", "nan", "inf", "bad"):
            with self.subTest(value=value), self.assertRaises(SystemExit) as raised:
                cli.main(self.args + ["--all-pages", "--page-timeout", value])
            self.assertEqual(raised.exception.code, 2)
        self.compare.assert_not_called()

    def test_failed_capture_returns_one_and_prints_each_side(self):
        self.compare.return_value = {
            "status": "failed", "run_dir": str(self.root),
            "workbooks": [{"relative_source": "Sales.twb", "pair_status": "paired",
                           "tableau": {"status": "timed_out"},
                           "powerbi": {"status": "captured"}}]}
        self.assertEqual(cli.main(self.args), 1)
        self.assertIn("Tableau=timed_out, Power BI=captured", self.stdout.getvalue())

    def test_validation_and_write_failure_return_one(self):
        for exception in (ValueError("invalid batch"), OSError("cannot write report")):
            with self.subTest(exception=exception):
                self.compare.side_effect = exception
                self.assertEqual(cli.main(self.args), 1)
                self.assertIn(str(exception), self.stderr.getvalue())

    def test_help_is_explicit_optional_and_does_not_capture(self):
        with self.assertRaises(SystemExit) as raised:
            cli.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        help_text = " ".join(self.stdout.getvalue().split())
        self.assertIn("Comparison-launched processes close after captures", help_text)
        self.assertIn("Power BI refreshes/verifies data; Tableau is not refreshed", help_text)
        self.assertIn("no automatic migration hook", " ".join(self.stdout.getvalue().split()))
        self.compare.assert_not_called()

    def test_local_mode_is_forwarded_without_changing_capture_exit_status(self):
        for status, expected_exit in (("captured", 0), ("failed", 1)):
            with self.subTest(status=status):
                self.compare.reset_mock()
                self.compare.return_value = {
                    "status": status, "run_dir": str(self.root), "workbooks": [],
                    "totals": {"source": {"captured": 0}}}
                self.assertEqual(cli.main(self.args + [
                    "--screen-comparison", "local", "--all-pages",
                    "--page-timeout", "2.5"]), expected_exit)
                self.compare.assert_called_once_with(
                    str(self.source), str(self.output), timeout=90,
                    tableau_desktop_path=None, powerbi_desktop_path=None,
                    all_pages=True, page_timeout=2.5, screen_comparison="local")
                self.assertIn("local non-semantic pixels; no parity score",
                              self.stdout.getvalue())

    def test_explicit_manual_preserves_legacy_call_contract(self):
        self.compare.return_value = {
            "status": "captured", "run_dir": str(self.root), "workbooks": []}
        self.assertEqual(cli.main(self.args + ["--screen-comparison", "manual"]), 0)
        self.compare.assert_called_once_with(
            str(self.source), str(self.output), timeout=90,
            tableau_desktop_path=None, powerbi_desktop_path=None,
            all_pages=False, page_timeout=30)

    def test_invalid_screen_mode_is_rejected_before_comparison(self):
        for value in ("ai", "ssim", "LOCAL", ""):
            with self.subTest(mode=value), self.assertRaises(SystemExit) as raised:
                cli.main(self.args + ["--screen-comparison", value])
            self.assertEqual(raised.exception.code, 2)
        self.compare.assert_not_called()
