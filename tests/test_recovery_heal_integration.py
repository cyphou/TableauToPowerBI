"""Tests for wiring DAX/M healers into the recovery report (Sprint 210.3/210.4)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'powerbi_import'))

from powerbi_import.recovery_report import RecoveryReport  # noqa: E402
from powerbi_import.dax_healing import heal_dax  # noqa: E402
from powerbi_import.m_healing import heal_m  # noqa: E402


class TestRecordHeal(unittest.TestCase):
    def test_dax_heal_recorded_as_tmdl(self):
        rr = RecoveryReport("WB")
        report = heal_dax("CALCULATE(SUM([Amt])", {"X"})  # missing paren → high conf
        n = rr.record_heal(report, item_name="My Measure")
        self.assertEqual(n, 1)
        self.assertTrue(rr.has_repairs)
        entry = rr.repairs[0]
        self.assertEqual(entry["category"], RecoveryReport.TMDL)
        self.assertEqual(entry["item_name"], "My Measure")
        self.assertEqual(entry["severity"], RecoveryReport.INFO)  # high confidence
        self.assertNotIn("follow_up", entry)  # high conf → no follow-up

    def test_m_heal_recorded_as_m_query(self):
        rr = RecoveryReport("WB")
        report = heal_m("each [Rev/Cost]")  # quote identifiers → high conf, category m_syntax
        n = rr.record_heal(report, item_name="Query1")
        self.assertEqual(n, 1)
        self.assertEqual(rr.repairs[0]["category"], RecoveryReport.M_QUERY)

    def test_low_confidence_gets_follow_up(self):
        rr = RecoveryReport("WB")
        # two missing parens → medium confidence
        report = heal_dax("CALCULATE(SUM(IF([x], 1, 0)", {"X"})
        rr.record_heal(report)
        # at least one entry has a follow-up recommendation
        follow = [r for r in rr.repairs if r.get("follow_up")]
        self.assertTrue(follow)
        self.assertEqual(rr.repairs[0]["severity"], RecoveryReport.WARNING)

    def test_explicit_category_override(self):
        rr = RecoveryReport("WB")
        report = heal_dax("SUM([M]", {"M"})
        rr.record_heal(report, category=RecoveryReport.RELATIONSHIP)
        self.assertEqual(rr.repairs[0]["category"], RecoveryReport.RELATIONSHIP)

    def test_no_change_records_nothing(self):
        rr = RecoveryReport("WB")
        report = heal_dax("SUM([Sales])", {"Total"})  # already valid
        n = rr.record_heal(report)
        self.assertEqual(n, 0)
        self.assertFalse(rr.has_repairs)

    def test_before_after_captured(self):
        rr = RecoveryReport("WB")
        report = heal_dax("IF([x] == 1, 1, 0)", None)
        rr.record_heal(report)
        entry = rr.repairs[0]
        self.assertIn("==", entry["original_value"])
        self.assertNotIn("==", entry["repaired_value"])
        self.assertIn("->", entry["action"])

    def test_summary_reflects_heals(self):
        rr = RecoveryReport("WB")
        rr.record_heal(heal_dax("CALCULATE(SUM([A])", {"X"}))
        rr.record_heal(heal_m("each [A/B]"))
        summary = rr.get_summary()
        self.assertEqual(summary["total_repairs"], 2)
        self.assertIn(RecoveryReport.TMDL, summary["by_category"])
        self.assertIn(RecoveryReport.M_QUERY, summary["by_category"])

    def test_to_dict_serializable(self):
        import json
        rr = RecoveryReport("WB")
        rr.record_heal(heal_dax("CALCULATE(SUM([A])", {"X"}))
        json.dumps(rr.to_dict())  # must not raise


if __name__ == "__main__":
    unittest.main()
