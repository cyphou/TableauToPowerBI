import json
import os
import tempfile
import unittest
from unittest import mock

from powerbi_import.cross_validator import scan_visual_role_contract
from powerbi_import.desktop_feedback_healing import heal_from_desktop_evidence
from powerbi_import.recovery_report import RecoveryReport
from scripts import heal_desktop_report


def _projection(kind, prop):
    return {"field": {kind: {"Property": prop}}}


class TestDesktopFeedbackHealing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        self.report = os.path.join(self.project, "Test.Report", "definition", "pages", "ReportSection")
        os.makedirs(os.path.join(self.report, "visuals", "v1"))
        with open(os.path.join(self.report, "page.json"), "w", encoding="utf-8") as stream:
            json.dump({}, stream)
        with open(os.path.join(self.report, "visuals", "v1", "visual.json"), "w", encoding="utf-8") as stream:
            json.dump({"name": "v1", "visual": {"visualType": "scatterChart", "query": {
                "queryState": {
                    "Y": {"projections": [_projection("Aggregation", "Revenue")]},
                    "Tooltips": {"projections": [
                        _projection("Aggregation", "Profit"), _projection("Column", "Region")
                    ]},
                }
            }}}, stream)
        self.shot = os.path.join(self.project, "shot.png")
        with open(self.shot, "wb") as stream:
            stream.write(b"test screenshot")

    def tearDown(self):
        self.tmp.cleanup()

    def _evidence(self, **extra):
        return {"pbip_path": "Test.pbip", "screenshot": self.shot,
            "visual_contract": scan_visual_role_contract(self.project), **extra}

    def test_repairs_scatter_only_from_existing_projections(self):
        result = heal_from_desktop_evidence(self.project, self._evidence())
        self.assertEqual(result["repairs"], 1)
        path = os.path.join(self.report, "visuals", "v1", "visual.json")
        with open(path, encoding="utf-8") as stream:
            visual = json.load(stream)
        state = visual["visual"]["query"]["queryState"]
        self.assertIn("Category", state)
        self.assertNotIn("Details", state)
        self.assertIn("X", state)
        self.assertEqual(state["Category"]["projections"][0], _projection("Column", "Region"))
        self.assertEqual(state["X"]["projections"][0], _projection("Aggregation", "Revenue"))

    def test_records_empty_desktop_data_without_inventing_rows(self):
        recovery = RecoveryReport("Test")
        result = heal_from_desktop_evidence(
            self.project,
            self._evidence(data_load={"status": "empty"}),
            recovery,
        )
        self.assertEqual(result["repairs"], 1)
        self.assertTrue(any(r["repair_type"] == "desktop_data_status" for r in recovery.repairs))

    def test_no_contract_evidence_does_not_add_roles(self):
        result = heal_from_desktop_evidence(self.project, {"pbip_path": "Test.pbip"})
        self.assertEqual(result["repairs"], 0)

    def test_missing_or_blank_screenshot_is_not_automatic_visual_correction(self):
        for shot in ("missing.png", self.shot):
            evidence = self._evidence(screenshot=shot, screenshot_blank=shot == self.shot)
            result = heal_from_desktop_evidence(self.project, evidence)
            self.assertEqual(result["repairs"], 0)

    def test_offline_runner_reads_existing_probe_and_keeps_original(self):
        pbip = os.path.join(self.project, "Test.pbip")
        shot = os.path.join(self.project, "shot.png")
        report = os.path.join(self.project, "opening_report.json")
        with open(pbip, "w", encoding="utf-8") as stream:
            stream.write("{}")
        with open(shot, "wb") as stream:
            stream.write(b"screenshot evidence")
        original = [self._evidence(pbip_path=pbip, screenshot=shot,
                                   data_load={"status": "verified"})]
        with open(report, "w", encoding="utf-8") as stream:
            json.dump(original, stream)
        with mock.patch.object(heal_desktop_report, "check_openability") as check:
            check.return_value.openable = True
            self.assertEqual(heal_desktop_report.main([report]), 0)
        with open(report, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), original)
        with open(os.path.join(self.project, "desktop_healing_report.json"), encoding="utf-8") as stream:
            results = json.load(stream)
        self.assertEqual(results[0]["feedback_repairs"], 1)
        self.assertEqual(results[0]["remaining_invalid_visuals"], 0)

    def test_report_healer_cannot_delete_existing_filters(self):
        report_json = os.path.join(self.project, "Test.Report", "definition", "report.json")
        with open(report_json, "w", encoding="utf-8") as stream:
            json.dump({"filterConfig": {"filters": [{"name": "preserve"}]}}, stream)

        def destructive_healer(state, recovery):
            state["report_json"]["filterConfig"]["filters"].clear()
            state["_dirty_files"].add(report_json)
            return 1

        recovery = RecoveryReport("Test")
        with mock.patch.object(heal_desktop_report, "run_report_healers", destructive_healer):
            self.assertEqual(heal_desktop_report._run_report_healers(self.project, recovery), 0)
        with open(report_json, encoding="utf-8") as stream:
            self.assertEqual(json.load(stream)["filterConfig"]["filters"], [{"name": "preserve"}])
        self.assertEqual(recovery.repairs[0]["repair_type"], "report_healing_preservation_guard")

    def test_saved_recovery_keeps_history_without_duplicating_current_run(self):
        original = RecoveryReport("Test")
        original.record("visual", "previous_fix", description="already fixed")
        heal_desktop_report._save_recovery(self.project, original)
        current = RecoveryReport("Test")
        current.record("visual", "previous_fix", description="already fixed")
        current.record("visual", "new_warning", description="still needs review")
        heal_desktop_report._save_recovery(self.project, current)
        heal_desktop_report._save_recovery(self.project, current)
        with open(os.path.join(self.project, "Test_recovery.json"), encoding="utf-8") as stream:
            saved = json.load(stream)
        self.assertEqual(len(saved["repairs"]), 2)
        self.assertEqual(len(current.repairs), 2)
