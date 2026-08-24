"""Tests for the closed-loop autoheal orchestrator (Sprint 221)."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'powerbi_import'))

from powerbi_import.autoheal import (  # noqa: E402
    AutoHealer, StaticValidatorSource, LogFileSource, PbiDesktopSource,
    ErrorRecord, AutoHealReport, heal_dax_expression, _measure_name, _strip_fences,
)


def _make_project(tmp, *, bad_measure="CALCULATE(SUM([Amt])",
                  good_measure="SUM([Amt])", bad_visual=True):
    sm = os.path.join(tmp, "Model.SemanticModel", "definition", "tables")
    os.makedirs(sm, exist_ok=True)
    tmdl = os.path.join(sm, "T.tmdl")
    with open(tmdl, "w", encoding="utf-8") as fh:
        fh.write("table T\n")
        fh.write(f"\tmeasure 'Bad' = {bad_measure}\n")
        fh.write(f"\tmeasure 'Good' = {good_measure}\n")
    if bad_visual:
        vdir = os.path.join(tmp, "Report", "definition", "pages", "p", "visuals", "v")
        os.makedirs(vdir, exist_ok=True)
        vf = os.path.join(vdir, "visual.json")
        with open(vf, "w", encoding="utf-8") as fh:
            json.dump({"name": "v",
                       "visual": {"visualType": "table",
                                  "annotations": [{"name": "x", "value": "1"}]}}, fh)
    return tmdl


class _FakeGateway:
    def __init__(self, fix_text, enabled=True):
        self._fix = fix_text
        self.enabled = enabled

    def complete(self, user, system=None):
        class _R:
            text = self._fix
        r = _R()
        r.text = self._fix
        return r


class TestHelpers(unittest.TestCase):
    def test_measure_name_quoted(self):
        self.assertEqual(_measure_name("\tmeasure 'Total Sales' = SUM([A])"), "Total Sales")

    def test_measure_name_bare(self):
        self.assertEqual(_measure_name("measure Simple = 1"), "Simple")

    def test_strip_fences(self):
        self.assertEqual(_strip_fences("```dax\nSUM([A])\n```"), "SUM([A])")
        self.assertEqual(_strip_fences("SUM([A])"), "SUM([A])")


class TestStaticValidatorSource(unittest.TestCase):
    def test_detects_broken_measure(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(tmp)
            errs = StaticValidatorSource().collect(tmp)
            dax = [e for e in errs if e.artifact == "dax"]
            self.assertTrue(any(e.location == "Bad" for e in dax))

    def test_detects_misplaced_annotations(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(tmp)
            errs = StaticValidatorSource().collect(tmp)
            self.assertTrue(any(e.artifact == "visual" for e in errs))


class TestDeterministicHeal(unittest.TestCase):
    def test_heals_project_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmdl = _make_project(tmp)
            report = AutoHealer().heal_project(tmp)
            self.assertTrue(report.changed)
            self.assertTrue(report.clean, report.to_dict())
            content = open(tmdl, encoding="utf-8").read()
            self.assertIn("CALCULATE(SUM([Amt]))", content)  # paren balanced

    def test_visual_annotations_moved(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(tmp)
            AutoHealer().heal_project(tmp)
            vf = os.path.join(tmp, "Report", "definition", "pages", "p",
                              "visuals", "v", "visual.json")
            data = json.load(open(vf, encoding="utf-8"))
            self.assertIn("annotations", data)
            self.assertNotIn("annotations", data["visual"])

    def test_valid_project_no_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(tmp, bad_measure="SUM([Amt])", bad_visual=False)
            report = AutoHealer().heal_project(tmp)
            self.assertFalse(report.changed)
            self.assertTrue(report.clean)

    def test_missing_dir(self):
        report = AutoHealer().heal_project("/no/such/dir")
        self.assertFalse(report.clean)


class TestLLMPass(unittest.TestCase):
    def _unfixable_by_determinism(self):
        # close-before-open: validator flags it, deterministic healer refuses.
        return "SUM([Amt]))"

    def test_llm_applies_validated_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmdl = _make_project(tmp, bad_measure=self._unfixable_by_determinism(),
                                 bad_visual=False)
            gw = _FakeGateway("SUM([Amt])")   # valid replacement
            report = AutoHealer(gateway=gw, autofix=True).heal_project(tmp)
            self.assertTrue(report.llm_used)
            self.assertTrue(report.clean, report.to_dict())
            self.assertIn("measure 'Bad' = SUM([Amt])",
                          open(tmdl, encoding="utf-8").read())

    def test_llm_rejects_invalid_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(tmp, bad_measure=self._unfixable_by_determinism(),
                          bad_visual=False)
            gw = _FakeGateway("STILL BROKEN(")   # invalid → must not apply
            report = AutoHealer(gateway=gw, autofix=True).heal_project(tmp)
            self.assertFalse(report.clean)

    def test_llm_not_used_when_autofix_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(tmp, bad_measure=self._unfixable_by_determinism(),
                          bad_visual=False)
            gw = _FakeGateway("SUM([Amt])")
            report = AutoHealer(gateway=gw, autofix=False).heal_project(tmp)
            self.assertFalse(report.llm_used)

    def test_llm_not_used_when_gateway_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(tmp, bad_measure=self._unfixable_by_determinism(),
                          bad_visual=False)
            gw = _FakeGateway("SUM([Amt])", enabled=False)
            report = AutoHealer(gateway=gw, autofix=True).heal_project(tmp)
            self.assertFalse(report.llm_used)


class TestErrorSources(unittest.TestCase):
    def test_logfile_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "err.txt")
            with open(log, "w", encoding="utf-8") as fh:
                fh.write("Something fine\n")
                fh.write("The measure 'Bad' has a syntax error near '('\n")
            errs = LogFileSource(log).collect(tmp)
            self.assertEqual(len(errs), 1)
            self.assertEqual(errs[0].location, "Bad")

    def test_pbi_desktop_source_guidance_without_log(self):
        errs = PbiDesktopSource().collect("/tmp")
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].severity, "info")

    def test_pbi_desktop_source_delegates_to_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "err.txt")
            with open(log, "w", encoding="utf-8") as fh:
                fh.write("DAX syntax error in measure [Bad]\n")
            errs = PbiDesktopSource(log_path=log).collect(tmp)
            self.assertTrue(errs and errs[0].artifact == "dax")

    def test_pbi_installed_returns_bool(self):
        self.assertIsInstance(PbiDesktopSource.pbi_desktop_installed(), bool)


class TestHealDaxExpressionHelper(unittest.TestCase):
    def test_deterministic(self):
        fixed, source = heal_dax_expression("CALCULATE(SUM([A])", set())
        self.assertEqual(source, "deterministic")
        self.assertTrue(fixed.endswith("))"))

    def test_none_when_valid(self):
        fixed, source = heal_dax_expression("SUM([A])", set())
        self.assertEqual(source, "none")

    def test_llm_branch(self):
        gw = _FakeGateway("SUM([A])")
        fixed, source = heal_dax_expression("SUM([A]))", set(), gateway=gw, autofix=True)
        self.assertEqual(source, "llm")
        self.assertEqual(fixed, "SUM([A])")


class TestReport(unittest.TestCase):
    def test_to_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(tmp)
            d = AutoHealer().heal_project(tmp).to_dict()
            self.assertIn("actions", d)
            self.assertIn("clean", d)
            self.assertIn("iterations", d)


class TestLogSourcedLLMFix(unittest.TestCase):
    def test_log_error_targets_measure_by_name(self):
        # Desktop-style log names the measure but not the file; LLM pass must
        # match by name across TMDL files.
        with tempfile.TemporaryDirectory() as tmp:
            tmdl = _make_project(tmp, bad_measure="SUM([Amt]))", bad_visual=False)
            log = os.path.join(tmp, "desktop_error.txt")
            with open(log, "w", encoding="utf-8") as fh:
                fh.write("DAX syntax error in measure 'Bad': unexpected ')'\n")
            gw = _FakeGateway("SUM([Amt])")
            report = AutoHealer(gateway=gw, autofix=True,
                                error_source=PbiDesktopSource(log_path=log)).heal_project(tmp)
            self.assertTrue(report.llm_used)
            self.assertIn("measure 'Bad' = SUM([Amt])",
                          open(tmdl, encoding="utf-8").read())


class TestMCPAutohealTool(unittest.TestCase):
    def test_tool_present(self):
        from powerbi_import.mcp_server import _tool_catalogue
        names = {t["name"] for t in _tool_catalogue()}
        self.assertIn("autoheal", names)

    def test_tool_heals_project(self):
        from powerbi_import.mcp_server import MigrationTools
        with tempfile.TemporaryDirectory() as tmp:
            _make_project(tmp)
            res = MigrationTools().autoheal({"project_dir": tmp})
            self.assertTrue(res["ok"])
            self.assertTrue(res["report"]["clean"])

    def test_tool_refuses_secret_arg(self):
        from powerbi_import.mcp_server import MigrationTools
        res = MigrationTools().autoheal({"project_dir": "/x", "api_key": "y"})
        self.assertFalse(res["ok"])

    def test_tool_bad_dir(self):
        from powerbi_import.mcp_server import MigrationTools
        res = MigrationTools().autoheal({"project_dir": "/no/such"})
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
