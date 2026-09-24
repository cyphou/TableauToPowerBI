"""Tests for the self-healing architecture: healing_core contract + healing facade.

Locks in the layering so the shared contract stays canonical and the facade
keeps exposing one coherent import surface over the whole subsystem.
"""

import ast
import os
import tempfile
import unittest

_REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')

#: Symbols the facade owns. A consumer importing one of these from a leaf
#: module bypasses the documented surface, which is how `healing.py` became
#: unreachable while reading as the entry point for the whole subsystem.
_FACADE_SYMBOLS = {
    'AutoHealer', 'LogFileSource', 'PbiDesktopSource', 'StaticValidatorSource',
    'check_openability', 'heal_dax', 'heal_m', 'heal_visual',
}
_LEAF_MODULES = {'autoheal', 'openability', 'dax_healing', 'm_healing',
                 'visual_healing'}
_FACADE_CONSUMERS = ('migrate.py', 'powerbi_import/mcp_server.py')


class TestFacadeIsTheImportSurface(unittest.TestCase):
    """The facade only exists if its consumers actually go through it."""

    def _leaf_imports(self, relpath):
        path = os.path.join(_REPO_ROOT, relpath)
        with open(path, encoding='utf-8-sig') as handle:
            tree = ast.parse(handle.read())
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.module.split('.')[-1] not in _LEAF_MODULES:
                continue
            taken = {a.name for a in node.names} & _FACADE_SYMBOLS
            if taken:
                offenders.append((node.lineno, node.module, sorted(taken)))
        return offenders

    def test_consumers_import_from_the_facade(self):
        for relpath in _FACADE_CONSUMERS:
            with self.subTest(consumer=relpath):
                offenders = self._leaf_imports(relpath)
                self.assertEqual(
                    offenders, [],
                    f"{relpath} bypasses powerbi_import.healing:\n  "
                    + "\n  ".join(f"line {ln}: from {mod} import "
                                  + ", ".join(names)
                                  for ln, mod, names in offenders))

    def test_the_detector_can_see_a_leaf_import(self):
        """Without this, a parse failure would make the check vacuous."""
        tree = ast.parse("from powerbi_import.autoheal import AutoHealer\n")
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom))
        self.assertIn(node.module.split('.')[-1], _LEAF_MODULES)
        self.assertTrue({a.name for a in node.names} & _FACADE_SYMBOLS)


class TestHealingCore(unittest.TestCase):
    def test_confidence_levels(self):
        from powerbi_import.healing_core import (
            HIGH, MEDIUM, LOW, CONFIDENCE_LEVELS, CONFIDENCE_RANK)
        self.assertEqual(CONFIDENCE_LEVELS, (HIGH, MEDIUM, LOW))
        self.assertGreater(CONFIDENCE_RANK[HIGH], CONFIDENCE_RANK[MEDIUM])
        self.assertGreater(CONFIDENCE_RANK[MEDIUM], CONFIDENCE_RANK[LOW])

    def test_heal_action_to_dict(self):
        from powerbi_import.healing_core import HealAction, HIGH
        a = HealAction("h", "cat", HIGH, "b", "a", "note")
        self.assertEqual(a.to_dict(), {
            "healer": "h", "category": "cat", "confidence": "high",
            "before": "b", "after": "a", "note": "note"})

    def test_heal_report_changed(self):
        from powerbi_import.healing_core import HealReport
        self.assertTrue(HealReport("x", "y").changed)
        self.assertFalse(HealReport("x", "x").changed)

    def test_heal_report_to_dict(self):
        from powerbi_import.healing_core import HealReport
        d = HealReport("x", "y").to_dict()
        self.assertEqual(set(d), {"original", "healed", "changed", "actions"})


class TestCanonicalContract(unittest.TestCase):
    """The contract must be one shared class, not per-module duplicates."""

    def test_healaction_is_shared_identity(self):
        from powerbi_import.healing_core import HealAction as Core
        from powerbi_import.dax_healing import HealAction as Dax
        from powerbi_import.m_healing import HealAction as M
        from powerbi_import.visual_healing import HealAction as Vis
        self.assertIs(Core, Dax)
        self.assertIs(Core, M)
        self.assertIs(Core, Vis)

    def test_healreport_is_shared_identity(self):
        from powerbi_import.healing_core import HealReport as Core
        from powerbi_import.dax_healing import HealReport as Dax
        from powerbi_import.m_healing import HealReport as M
        self.assertIs(Core, Dax)
        self.assertIs(Core, M)

    def test_no_import_cycle(self):
        # Importing the facade must not raise (would fail on a cycle).
        import importlib
        mod = importlib.import_module("powerbi_import.healing")
        self.assertTrue(hasattr(mod, "__all__"))

    def test_repairattempt_distinct_from_healaction(self):
        # autoheal.RepairAttempt (applied fix + validation outcome) must NOT
        # collide with healing_core.HealAction (a single deterministic edit).
        from powerbi_import.autoheal import RepairAttempt
        from powerbi_import.healing_core import HealAction
        self.assertIsNot(RepairAttempt, HealAction)
        self.assertIn("validated", RepairAttempt.__dataclass_fields__)
        self.assertNotIn("validated", HealAction.__dataclass_fields__)


class TestFacade(unittest.TestCase):
    def test_all_exports_present(self):
        from powerbi_import import healing
        for name in healing.__all__:
            self.assertTrue(hasattr(healing, name), f"missing export: {name}")

    def test_healers_reachable(self):
        from powerbi_import.healing import heal_dax, heal_m, heal_visual
        self.assertEqual(heal_dax("SUM([x]", set()).healed, "SUM([x])")
        self.assertFalse(heal_m("let x = 1 in x").changed)
        self.assertFalse(heal_visual({"name": "v", "visual": {"visualType": "card"}}).changed)

    def test_detection_reachable(self):
        from powerbi_import.healing import check_openability, extract_m_partitions
        t = ("\tpartition 'T-g' = m\n\t\tmode: import\n\t\tsource =\n"
             "\t\t\t\tlet x=1 in x\n")
        self.assertEqual(len(extract_m_partitions(t)), 1)

    def test_orchestration_reachable(self):
        from powerbi_import.healing import AutoHealer, StaticValidatorSource
        self.assertTrue(callable(AutoHealer))
        self.assertTrue(callable(StaticValidatorSource))

    def test_heal_and_verify_missing_dir_raises(self):
        from powerbi_import.healing import heal_and_verify
        with self.assertRaises(FileNotFoundError):
            heal_and_verify(os.path.join(tempfile.gettempdir(), "nope-xyz-123"))

    def test_heal_and_verify_returns_pair(self):
        from powerbi_import.healing import heal_and_verify, AutoHealReport, OpenabilityReport
        from tests.test_openability import _write_project
        with tempfile.TemporaryDirectory() as d:
            _write_project(d, tmdl_text=(
                "table 'T'\n\tpartition 'T-g' = m\n\t\tmode: import\n"
                "\t\tsource =\n\t\t\t\tlet x = 1 in x\n"))
            heal_report, open_report = heal_and_verify(d)
            self.assertIsInstance(heal_report, AutoHealReport)
            self.assertIsInstance(open_report, OpenabilityReport)
            self.assertTrue(open_report.openable, open_report.blocking_issues)


if __name__ == "__main__":
    unittest.main()
