"""Tests for natural-language remediation (v44, Sprint 217.5)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.remediation import (  # noqa: E402
    RemediationSuggestion, RemediationReport,
    explain_finding, remediate_findings, remediate_assessment,
    refine_with_llm, _route_owner, _match_template,
)


def _assessment_payload():
    return {
        "workbook_name": "WB",
        "overall_score": "YELLOW",
        "totals": {"checks": 4, "pass": 1, "warn": 2, "fail": 1},
        "categories": [
            {"name": "Calculation", "checks": [
                {"name": "LOD expression", "severity": "warn",
                 "detail": "FIXED LOD approximated", "recommendation": ""},
                {"name": "Table calc", "severity": "warn",
                 "detail": "WINDOW_SUM converted", "recommendation": ""},
            ]},
            {"name": "Data Model", "checks": [
                {"name": "RLS", "severity": "fail",
                 "detail": "user filter security role", "recommendation": "Assign AD members"},
            ]},
            {"name": "Scope", "checks": [
                {"name": "OK", "severity": "pass", "detail": "fine", "recommendation": ""},
            ]},
        ],
    }


class TestRouting(unittest.TestCase):
    def test_dax_route(self):
        self.assertEqual(_route_owner("LOD aggregation measure")[1], "@dax")

    def test_wiring_route(self):
        self.assertEqual(_route_owner("power query Table.AddColumn")[1], "@wiring")

    def test_semantic_route(self):
        self.assertEqual(_route_owner("relationship cardinality RLS")[1], "@semantic")

    def test_visual_route(self):
        self.assertEqual(_route_owner("slicer conditional format")[1], "@visual")

    def test_extractor_route(self):
        self.assertEqual(_route_owner("custom sql datasource")[1], "@extractor")

    def test_deployer_route(self):
        self.assertEqual(_route_owner("gateway refresh schedule")[1], "@deployer")

    def test_default_route(self):
        self.assertEqual(_route_owner("something unrelated")[1], "@orchestrator")


class TestTemplates(unittest.TestCase):
    def test_lod_template_high(self):
        _, _, conf = _match_template("FIXED LOD")
        self.assertEqual(conf, "high")

    def test_forecast_template_low(self):
        _, _, conf = _match_template("forecast analytics")
        self.assertEqual(conf, "low")

    def test_unknown_template_low(self):
        expl, action, conf = _match_template("zzz nothing matches")
        self.assertEqual(conf, "low")
        self.assertTrue(expl and action)


class TestExplainFinding(unittest.TestCase):
    def test_explain_rls(self):
        s = explain_finding({"category": "Data Model", "name": "RLS",
                             "severity": "fail", "detail": "user filter security"})
        self.assertIsInstance(s, RemediationSuggestion)
        self.assertEqual(s.owning_agent, "@semantic")
        self.assertEqual(s.confidence, "high")
        self.assertEqual(s.source, "template")

    def test_recommendation_overrides_action(self):
        s = explain_finding({"category": "X", "name": "y", "severity": "warn",
                             "detail": "custom sql", "recommendation": "Do the thing"})
        self.assertEqual(s.suggested_action, "Do the thing")

    def test_to_dict(self):
        s = explain_finding({"name": "n", "detail": "LOD"})
        d = s.to_dict()
        self.assertIn("confidence", d)
        self.assertIn("owning_file", d)


class TestRemediateFindings(unittest.TestCase):
    def test_filter_by_severity(self):
        findings = [
            {"name": "a", "severity": "pass", "detail": "x"},
            {"name": "b", "severity": "warn", "detail": "LOD"},
            {"name": "c", "severity": "fail", "detail": "RLS"},
        ]
        rep = remediate_findings(findings)
        names = [s.finding for s in rep.suggestions]
        self.assertNotIn("a", names)
        self.assertIn("b", names)
        self.assertIn("c", names)

    def test_sorted_by_confidence(self):
        findings = [
            {"name": "low1", "severity": "warn", "detail": "forecast"},
            {"name": "high1", "severity": "warn", "detail": "FIXED LOD"},
        ]
        rep = remediate_findings(findings)
        self.assertEqual(rep.suggestions[0].confidence, "high")


class TestRemediateAssessment(unittest.TestCase):
    def setUp(self):
        self.rep = remediate_assessment(_assessment_payload())

    def test_workbook_name(self):
        self.assertEqual(self.rep.workbook, "WB")

    def test_excludes_pass(self):
        self.assertTrue(all(s.severity != "pass" for s in self.rep.suggestions))

    def test_has_three_actionable(self):
        # 2 warn + 1 fail
        self.assertEqual(len(self.rep.suggestions), 3)

    def test_to_dict(self):
        d = self.rep.to_dict()
        self.assertEqual(d["count"], 3)
        self.assertIn("low_confidence_count", d)

    def test_save_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rem.json")
            self.rep.save_json(path)
            self.assertTrue(os.path.isfile(path))

    def test_html_render(self):
        html = self.rep.to_html()
        self.assertIn("<table", html)
        self.assertIn("Remediation report", html)


class TestLLMRefine(unittest.TestCase):
    def test_no_client_noop(self):
        rep = remediate_assessment(_assessment_payload())
        before = [s.source for s in rep.suggestions]
        rep2 = refine_with_llm(rep, None)
        self.assertEqual([s.source for s in rep2.suggestions], before)

    def test_client_refines_non_high(self):
        class FakeLLM:
            def complete(self, prompt):
                return "Refined action text."

        rep = remediate_findings([{"name": "x", "severity": "warn", "detail": "forecast"}])
        rep = refine_with_llm(rep, FakeLLM())
        self.assertEqual(rep.suggestions[0].source, "llm")
        self.assertEqual(rep.suggestions[0].suggested_action, "Refined action text.")

    def test_client_failure_falls_back(self):
        class BadLLM:
            def complete(self, prompt):
                raise RuntimeError("boom")

        rep = remediate_findings([{"name": "x", "severity": "warn", "detail": "forecast"}])
        rep = refine_with_llm(rep, BadLLM())
        self.assertEqual(rep.suggestions[0].source, "template")

    def test_high_confidence_not_refined(self):
        class FakeLLM:
            def complete(self, prompt):
                return "should not be used"

        rep = remediate_findings([{"name": "x", "severity": "warn", "detail": "FIXED LOD"}])
        rep = refine_with_llm(rep, FakeLLM())
        self.assertEqual(rep.suggestions[0].source, "template")


if __name__ == "__main__":
    unittest.main()
