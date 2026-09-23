"""Documentation that promises a flag the CLI does not have.

This is the documentation half of the inert-flag defect. An inert flag is
accepted and does nothing; a phantom flag is rejected outright, and argparse
fails for a reason that looks unrelated -- when `--server-assess PROJECT` was
documented against a `store_true` flag, the project name bound to the workbook
positional and the error named the wrong thing entirely.

`docs/AGENTS.md` claimed the preceptorship loop also triggered "on --review".
That flag has never existed.

The guard has to be precise to be worth having. Judging every `--word` in the
docs produced twenty hits, nearly all of them pytest's `--cov`, git's
`--oneline` or VS Code's `--install-extension`. Judging only lines that invoke
migrate.py produced two, one of which was a blind spot in the guard rather
than a defect in the docs.
"""

import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.check_doc_claims import (  # noqa: E402
    KNOWN_PHANTOM,
    declared_flags,
    doc_files,
    find_phantoms,
    intercepted_flags,
    main,
)


class TestInterceptDetection(unittest.TestCase):
    """Flags handled before parse_args are real, and must not be reported."""

    def test_a_list_comparison_counts(self):
        self.assertIn("--advanced-help", intercepted_flags(textwrap.dedent("""
            if raw_args == ['--advanced-help']:
                parser.print_help()
        """)))

    def test_a_membership_test_counts(self):
        self.assertIn("--boom", intercepted_flags(textwrap.dedent("""
            if raw_args[0] in {'--boom', '--other'}:
                pass
        """)))

    def test_unrelated_strings_are_not_flags(self):
        self.assertEqual(set(), intercepted_flags(textwrap.dedent("""
            if mode == 'import':
                pass
        """)))


class TestPhantomDetection(unittest.TestCase):

    def _scan(self, markdown, real=("--real",)):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "GUIDE.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(textwrap.dedent(markdown))
            return find_phantoms(set(real), [path], repo_root=td)

    def test_an_undeclared_flag_is_reported(self):
        found = self._scan("python migrate.py wb.twbx --ghost\n")
        self.assertIn("--ghost", found)

    def test_a_declared_flag_is_not_reported(self):
        self.assertEqual({}, self._scan("python migrate.py wb.twbx --real\n"))

    def test_another_tools_flags_are_ignored(self):
        """pytest --cov and git --oneline are not our surface."""
        self.assertEqual({}, self._scan(
            "pytest -q --cov=powerbi_import\ngit log --oneline\n"))

    def test_a_flag_is_only_judged_in_our_own_command(self):
        """The same token is fine elsewhere and wrong in a migrate.py line."""
        self.assertEqual({}, self._scan("Run `tool --ghost` for details.\n"))
        self.assertIn("--ghost", self._scan("python migrate.py --ghost\n"))

    def test_the_location_is_reported(self):
        found = self._scan("intro\npython migrate.py --ghost\n")
        self.assertTrue(found["--ghost"][0].endswith("GUIDE.md:2"))


class TestRealDocumentation(unittest.TestCase):

    def test_the_documented_surface_is_real(self):
        phantom = find_phantoms(declared_flags(), doc_files())
        new = sorted(set(phantom) - KNOWN_PHANTOM)
        self.assertEqual([], new,
                         f"documented but not declared: {new}")

    def test_advanced_help_is_recognised_as_real(self):
        """It is compared against argv, not declared, and is not a phantom."""
        self.assertIn("--advanced-help", declared_flags())

    def test_strict_mode_passes(self):
        self.assertEqual(0, main(["--strict"]))

    def test_the_baseline_only_shrinks(self):
        phantom = set(find_phantoms(declared_flags(), doc_files()))
        fixed = sorted(KNOWN_PHANTOM - phantom)
        self.assertEqual([], fixed,
                         f"now real or removed: {fixed} — drop from KNOWN_PHANTOM")


if __name__ == "__main__":
    unittest.main()
