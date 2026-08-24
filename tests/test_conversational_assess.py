"""Tests for conversational assessment & planning (v44, Sprint 218.4)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.conversational import (  # noqa: E402
    MigrationQA, Answer, answer_question, build_plan_summary,
)


def _payload(score="YELLOW", fails=1, warns=1):
    checks_cal = [{"name": "LOD", "severity": "warn",
                   "detail": "FIXED LOD approximated", "recommendation": ""}] * warns
    checks_dm = [{"name": "RLS", "severity": "fail",
                  "detail": "user filter needs gateway connection",
                  "recommendation": "Assign AD members"}] * fails
    return {
        "workbook_name": "WB",
        "overall_score": score,
        "totals": {"checks": 2 + fails + warns, "pass": 2, "warn": warns, "fail": fails},
        "categories": [
            {"name": "Calculation", "checks": checks_cal + [
                {"name": "OK", "severity": "pass", "detail": "fine", "recommendation": ""}]},
            {"name": "Datasource Compatibility", "checks": checks_dm + [
                {"name": "Visual", "severity": "info",
                 "detail": "gantt approximated as bar", "recommendation": ""}]},
        ],
    }


class TestIntents(unittest.TestCase):
    def setUp(self):
        self.qa = MigrationQA(_payload())

    def test_readiness_intent(self):
        a = self.qa.ask("what's the overall readiness?")
        self.assertEqual(a.matched_intent, "readiness")
        self.assertEqual(a.data["overall_score"], "YELLOW")

    def test_gaps_intent(self):
        a = self.qa.ask("what won't migrate cleanly?")
        self.assertEqual(a.matched_intent, "gaps")
        self.assertGreaterEqual(a.data["failures"], 1)
        self.assertTrue(a.evidence)

    def test_visuals_intent(self):
        a = self.qa.ask("how many visuals are approximated?")
        self.assertEqual(a.matched_intent, "visuals")
        self.assertGreaterEqual(a.data["approximated"], 1)

    def test_datasources_intent(self):
        a = self.qa.ask("which datasources need a gateway?")
        self.assertEqual(a.matched_intent, "datasources")
        self.assertGreaterEqual(a.data["gateway_candidates"], 1)

    def test_calculations_intent(self):
        a = self.qa.ask("any DAX calculation issues?")
        self.assertEqual(a.matched_intent, "calculations")
        self.assertGreaterEqual(a.data["calculation_findings"], 1)

    def test_plan_intent(self):
        a = self.qa.ask("give me a migration plan")
        self.assertEqual(a.matched_intent, "plan")
        self.assertTrue(a.evidence)

    def test_general_fallback(self):
        a = self.qa.ask("tell me something")
        self.assertEqual(a.matched_intent, "general")


class TestGroundedness(unittest.TestCase):
    def test_evidence_is_data_backed(self):
        qa = MigrationQA(_payload(fails=2, warns=0))
        a = qa.ask("what are the risks?")
        self.assertEqual(a.data["failures"], 2)
        # every evidence row references an actual finding severity
        self.assertTrue(all(e.startswith("[") for e in a.evidence))

    def test_clean_workbook(self):
        qa = MigrationQA(_payload(score="GREEN", fails=0, warns=0))
        a = qa.ask("what won't migrate cleanly?")
        self.assertIn("cleanly", a.answer)
        self.assertEqual(a.data["failures"], 0)

    def test_empty_report(self):
        a = answer_question({}, "readiness?")
        self.assertIsInstance(a, Answer)


class TestPlan(unittest.TestCase):
    def test_green_plan(self):
        plan = build_plan_summary(_payload(score="GREEN", fails=0, warns=0))
        self.assertEqual(plan["estimated_effort"], "low")
        self.assertIn("Ready", plan["headline"])
        self.assertTrue(plan["steps"])

    def test_red_plan(self):
        plan = build_plan_summary(_payload(score="RED", fails=3, warns=0))
        self.assertEqual(plan["estimated_effort"], "high")
        self.assertEqual(plan["blocking_failures"], 3)
        self.assertIn("Remediate", plan["headline"])

    def test_yellow_plan(self):
        plan = build_plan_summary(_payload(score="YELLOW", fails=0, warns=2))
        self.assertEqual(plan["estimated_effort"], "medium")
        self.assertEqual(plan["warnings"], 2)


class TestAnswerSerialization(unittest.TestCase):
    def test_to_dict(self):
        a = answer_question(_payload(), "readiness")
        d = a.to_dict()
        self.assertIn("answer", d)
        self.assertIn("matched_intent", d)
        self.assertIn("data", d)


if __name__ == "__main__":
    unittest.main()
