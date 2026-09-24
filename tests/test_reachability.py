"""Guard the module half of the inert-surface contract.

``tests/test_cli_flag_wiring.py`` stops a flag being declared and never read.
Nothing stopped the same thing happening to a whole module: complete, tested,
imported by a test so it reads as covered, and reachable from no command a user
can run. That number was measured once in prose and could drift a whole cycle
before anyone noticed.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from scripts.check_reachability import (  # noqa: E402
    DECLARED_ENTRY_POINTS,
    KNOWN_UNREACHABLE,
    analyse,
)

_REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')


class TestReachability(unittest.TestCase):
    def setUp(self):
        self.modules, self.reached, self.unreachable = analyse()

    def test_no_new_unreachable_module(self):
        new = [m for m in self.unreachable if m not in KNOWN_UNREACHABLE]
        self.assertEqual(
            new, [],
            "modules no entry point reaches, and not on the baseline:\n  "
            + "\n  ".join(new))

    def test_the_baseline_only_shrinks(self):
        """A wired module left on the baseline hides the next regression."""
        stale = sorted(KNOWN_UNREACHABLE - set(self.unreachable))
        self.assertEqual(
            stale, [],
            "these are reachable now and must leave KNOWN_UNREACHABLE:\n  "
            + "\n  ".join(stale))

    def test_declared_entry_points_are_real_modules(self):
        missing = sorted(set(DECLARED_ENTRY_POINTS) - set(self.modules))
        self.assertEqual(
            missing, [],
            "declared entry points that are not source modules:\n  "
            + "\n  ".join(missing))

    def test_declared_entry_points_name_a_caller_that_exists(self):
        """A declaration nobody checks is the rubber stamp this guard is for."""
        for module, caller in sorted(DECLARED_ENTRY_POINTS.items()):
            with self.subTest(module=module):
                self.assertTrue(
                    os.path.exists(os.path.join(_REPO_ROOT, caller)),
                    f"{module} declares {caller} as its caller, "
                    f"but that path does not exist")

    def test_a_declaration_is_not_also_a_baseline_entry(self):
        both = sorted(set(DECLARED_ENTRY_POINTS) & KNOWN_UNREACHABLE)
        self.assertEqual(
            both, [],
            "declared and baselined at once, so neither list is true:\n  "
            + "\n  ".join(both))

    def test_the_cli_reaches_the_core_generators(self):
        """Without this, an analyser that resolved nothing would look perfect."""
        for module in ('powerbi_import.tmdl_generator',
                       'powerbi_import.pbip_generator',
                       'tableau_export.extract_tableau_data',
                       'tableau_export.dax_converter'):
            with self.subTest(module=module):
                self.assertIn(module, self.reached)

    def test_modules_are_discovered(self):
        self.assertGreater(len(self.modules), 100)


if __name__ == '__main__':
    unittest.main()
