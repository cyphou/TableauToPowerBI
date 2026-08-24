"""Tests for confidence-scored PBIR visual self-healing (Sprint 211.3)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.visual_healing import (  # noqa: E402
    heal_visual, heal_visuals, VisualHealReport,
    heal_annotations_placement, heal_position, heal_missing_name,
    heal_visual_type, _MIN_W, _MIN_H,
)
from powerbi_import.dax_healing import HIGH, MEDIUM, LOW  # noqa: E402


def _valid_container():
    return {
        "name": "v1",
        "position": {"x": 0, "y": 0, "z": 0, "width": 400, "height": 300},
        "visual": {"visualType": "clusteredBarChart", "query": {}},
        "annotations": [{"name": "a", "value": "1"}],
    }


class TestAnnotationsPlacement(unittest.TestCase):
    def test_moves_inner_annotations_to_root(self):
        c = {
            "name": "v", "position": {"width": 10, "height": 10},
            "visual": {"visualType": "table", "annotations": [{"name": "x", "value": "1"}]},
        }
        action = heal_annotations_placement(c)
        self.assertIsNotNone(action)
        self.assertEqual(action.confidence, HIGH)
        self.assertIn("annotations", c)
        self.assertNotIn("annotations", c["visual"])
        self.assertEqual(c["annotations"][0]["name"], "x")

    def test_merges_with_existing_root(self):
        c = {
            "name": "v", "position": {"width": 10, "height": 10},
            "annotations": [{"name": "root", "value": "1"}],
            "visual": {"visualType": "table", "annotations": [{"name": "inner", "value": "2"}]},
        }
        heal_annotations_placement(c)
        names = {a["name"] for a in c["annotations"]}
        self.assertEqual(names, {"root", "inner"})

    def test_no_inner_annotations_noop(self):
        c = _valid_container()
        self.assertIsNone(heal_annotations_placement(c))


class TestPosition(unittest.TestCase):
    def test_fixes_zero_size(self):
        c = {"position": {"x": 0, "y": 0, "width": 0, "height": 0}}
        action = heal_position(c)
        self.assertEqual(c["position"]["width"], _MIN_W)
        self.assertEqual(c["position"]["height"], _MIN_H)
        self.assertEqual(action.confidence, MEDIUM)

    def test_fixes_negative_coords(self):
        c = {"position": {"x": -5, "y": -10, "width": 100, "height": 100}}
        heal_position(c)
        self.assertEqual(c["position"]["x"], 0)
        self.assertEqual(c["position"]["y"], 0)

    def test_valid_position_noop(self):
        c = _valid_container()
        self.assertIsNone(heal_position(c))

    def test_missing_size_key(self):
        c = {"position": {"x": 0, "y": 0}}
        heal_position(c)
        self.assertEqual(c["position"]["width"], _MIN_W)


class TestMissingName(unittest.TestCase):
    def test_assigns_name(self):
        c = {"visual": {"visualType": "pieChart"}}
        action = heal_missing_name(c)
        self.assertEqual(c["name"], "healed_pieChart")
        self.assertEqual(action.confidence, MEDIUM)

    def test_present_name_noop(self):
        self.assertIsNone(heal_missing_name(_valid_container()))

    def test_deterministic(self):
        c1 = {"visual": {"visualType": "map"}}
        c2 = {"visual": {"visualType": "map"}}
        heal_missing_name(c1)
        heal_missing_name(c2)
        self.assertEqual(c1["name"], c2["name"])


class TestVisualType(unittest.TestCase):
    def test_defaults_when_content_present(self):
        c = {"visual": {"query": {"a": 1}}}
        action = heal_visual_type(c)
        self.assertEqual(c["visual"]["visualType"], "tableEx")
        self.assertEqual(action.confidence, LOW)

    def test_no_content_noop(self):
        c = {"visual": {}}
        self.assertIsNone(heal_visual_type(c))

    def test_present_type_noop(self):
        self.assertIsNone(heal_visual_type(_valid_container()))


class TestHealVisualOrchestrator(unittest.TestCase):
    def test_valid_unchanged(self):
        report = heal_visual(_valid_container())
        self.assertFalse(report.changed)
        self.assertEqual(report.actions, [])

    def test_does_not_mutate_input(self):
        c = {"visual": {"visualType": "table", "annotations": [{"name": "x"}]},
             "position": {"width": 0, "height": 0}}
        snapshot = {"visual": {"visualType": "table", "annotations": [{"name": "x"}]},
                    "position": {"width": 0, "height": 0}}
        heal_visual(c)
        self.assertEqual(c, snapshot)  # original untouched

    def test_combined_heal(self):
        c = {
            "position": {"width": 0, "height": 0},
            "visual": {"query": {"a": 1}, "annotations": [{"name": "x", "value": "1"}]},
        }
        report = heal_visual(c)
        self.assertTrue(report.changed)
        healers = {a.healer for a in report.actions}
        self.assertIn("annotations_placement", healers)
        self.assertIn("position", healers)
        self.assertIn("missing_name", healers)
        # healed container is valid
        self.assertNotIn("annotations", report.healed["visual"])
        self.assertGreater(report.healed["position"]["width"], 0)

    def test_idempotent(self):
        c = {
            "position": {"width": -1, "height": 0},
            "visual": {"query": {}, "annotations": [{"name": "x"}]},
        }
        r1 = heal_visual(c)
        r2 = heal_visual(r1.healed)
        self.assertFalse(r2.changed, f"not idempotent: {r2.healed!r}")

    def test_to_dict(self):
        c = {"visual": {"visualType": "table", "annotations": [{"name": "x"}]}}
        report = heal_visual(c)
        d = report.to_dict()
        self.assertTrue(d["changed"])
        self.assertIn("actions", d)


class TestHealVisualsBatch(unittest.TestCase):
    def test_reports_only_changed(self):
        containers = [
            _valid_container(),
            {"visual": {"visualType": "t", "annotations": [{"name": "x"}]}},
        ]
        reports = heal_visuals(containers)
        self.assertEqual(len(reports), 1)

    def test_skips_non_dict(self):
        reports = heal_visuals(["notadict", None, _valid_container()])
        self.assertEqual(reports, [])


class TestRecoveryIntegration(unittest.TestCase):
    def test_record_heal_as_visual(self):
        from powerbi_import.recovery_report import RecoveryReport
        rr = RecoveryReport("WB")
        report = heal_visual({"visual": {"visualType": "t", "annotations": [{"name": "x"}]}})
        rr.record_heal(report, category=RecoveryReport.VISUAL, item_name="v1")
        self.assertTrue(rr.has_repairs)
        self.assertEqual(rr.repairs[0]["category"], RecoveryReport.VISUAL)


if __name__ == "__main__":
    unittest.main()
