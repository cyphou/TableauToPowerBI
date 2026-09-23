"""--merge-preview was declared, backed by a tested function, and inert.

`merge_preview()` computes the whole dry run — merge candidates, renamed
measures, isolated tables, RLS conflicts — and writes nothing. Nothing called
it, so the flag documented as "show what would be merged ... without writing
any files" silently ran a full migration instead.

The reporting layer needs its own guard. Reading a key that does not exist
returns None, the guarded print is skipped, and the summary loses a line while
still looking like a working report.
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import migrate  # noqa: E402


def _converted(table_name, measure_name):
    return {
        "datasources": [{
            "name": "DS",
            "tables": [{"name": table_name,
                        "columns": [{"name": "Id"}, {"name": "Amount"}]}],
            "columns": [{"name": "Id"}, {"name": "Amount"}],
        }],
        "calculations": [{"name": measure_name, "formula": "SUM([Amount])"}],
        "worksheets": [],
        "dashboards": [],
    }


class TestMergePreviewOutput(unittest.TestCase):

    def _render(self, converted=None, names=None):
        converted = converted or [_converted("Orders", "Total"),
                                  _converted("Orders", "Total")]
        names = names or ["WB1", "WB2"]
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = migrate._print_merge_preview(converted, names)
        return code, buffer.getvalue()

    def test_it_reports_success(self):
        code, _ = self._render()
        self.assertEqual(migrate.ExitCode.SUCCESS, code)

    def test_it_names_itself_a_dry_run(self):
        _, out = self._render()
        self.assertIn("DRY RUN", out)
        self.assertIn("Nothing was written", out)

    def test_the_merge_score_is_actually_printed(self):
        """Guards the defect this reporter shipped with.

        The score lived under 'merge_score'; reading 'overall_score' returned
        None, the guarded print was skipped, and the summary quietly lost its
        headline number while still rendering as a report.
        """
        _, out = self._render()
        self.assertRegex(out, r"Merge score: \d+/100")

    def test_the_table_savings_are_printed(self):
        _, out = self._render()
        self.assertRegex(out, r"Tables: \d+ across workbooks")

    def test_it_writes_nothing(self):
        """The flag's entire promise."""
        with tempfile.TemporaryDirectory() as td:
            before = os.listdir(td)
            cwd = os.getcwd()
            os.chdir(td)
            try:
                self._render()
            finally:
                os.chdir(cwd)
            self.assertEqual(before, os.listdir(td))


class TestMergePreviewIsWired(unittest.TestCase):

    def test_flag_defaults_off(self):
        args = migrate._build_argument_parser().parse_args(['wb.twbx'])
        self.assertFalse(args.merge_preview)

    def test_flag_parses(self):
        args = migrate._build_argument_parser().parse_args(
            ['wb.twbx', '--merge-preview'])
        self.assertTrue(args.merge_preview)

    def test_the_flag_reaches_the_pipeline(self):
        import inspect
        source = inspect.getsource(migrate)
        self.assertRegex(
            source,
            r"preview_only=getattr\(args, 'merge_preview'",
            "--merge-preview is never forwarded to the shared-model run",
        )

    def test_the_pipeline_short_circuits_before_migrating(self):
        """Preview must return before import_shared_model, or it writes."""
        import inspect
        source = inspect.getsource(migrate.run_shared_model_migration)
        preview_at = source.find("return _print_merge_preview")
        # The docstring names import_shared_model too, so match the call.
        import_at = source.find("importer.import_shared_model(")
        self.assertNotEqual(-1, preview_at, "preview branch missing")
        self.assertNotEqual(-1, import_at, "migration call missing")
        self.assertLess(preview_at, import_at,
                        "preview must short-circuit before the migration runs")

    def test_run_shared_model_migration_accepts_preview_only(self):
        import inspect
        self.assertIn(
            "preview_only",
            inspect.signature(migrate.run_shared_model_migration).parameters)


if __name__ == "__main__":
    unittest.main()
