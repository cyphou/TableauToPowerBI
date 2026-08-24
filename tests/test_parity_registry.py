"""Tests for the functionality parity registry & scan (v43/v44, Sprint 209)."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))

from powerbi_import import parity_registry as pr  # noqa: E402
from powerbi_import.parity_registry import (  # noqa: E402
    scan_workbook, features_by_category, ParityScan,
    EXACT, HEALED, APPROXIMATED, UNSUPPORTED, _FEATURES, _DETECTORS,
)


def _calc(formula):
    return {"name": "c", "formula": formula, "role": "measure"}


class TestRegistryStructure(unittest.TestCase):
    def test_every_feature_has_detector(self):
        for feat in _FEATURES:
            self.assertIn(feat.key, _DETECTORS, f"no detector for {feat.key}")

    def test_every_feature_valid_status(self):
        for feat in _FEATURES:
            self.assertIn(feat.status, (EXACT, HEALED, APPROXIMATED, UNSUPPORTED))

    def test_features_by_category(self):
        cats = features_by_category()
        self.assertIn("Calculations", cats)
        self.assertIn("Analytics", cats)
        # every feature appears exactly once
        total = sum(len(v) for v in cats.values())
        self.assertEqual(total, len(_FEATURES))

    def test_registry_version(self):
        scan = scan_workbook({}, "x")
        self.assertEqual(scan.registry_version, pr.REGISTRY_VERSION)


class TestCalculationClassification(unittest.TestCase):
    def test_lod_detected(self):
        conv = {"calculations": [_calc("{ FIXED [Region] : SUM([Sales]) }")]}
        scan = scan_workbook(conv)
        keys = {u.key: u for u in scan.usages}
        self.assertIn("calc_lod", keys)
        self.assertEqual(keys["calc_lod"].status, HEALED)

    def test_table_calc_detected(self):
        conv = {"calculations": [_calc("RUNNING_SUM(SUM([Sales]))")]}
        keys = {u.key: u for u in scan_workbook(conv).usages}
        self.assertIn("calc_table", keys)

    def test_basic_calc_detected(self):
        conv = {"calculations": [_calc("[Sales] - [Cost]")]}
        keys = {u.key: u for u in scan_workbook(conv).usages}
        self.assertIn("calc_basic", keys)
        self.assertEqual(keys["calc_basic"].status, EXACT)

    def test_lod_not_counted_as_basic(self):
        conv = {"calculations": [_calc("{ FIXED [R] : SUM([S]) }")]}
        keys = {u.key for u in scan_workbook(conv).usages}
        self.assertNotIn("calc_basic", keys)


class TestStructuredAnalyticsDetection(unittest.TestCase):
    def test_empty_analytics_lists_count_zero(self):
        conv = {"worksheets": [
            {"forecasting": [], "clustering": [], "trend_lines": [], "reference_lines": []}
        ]}
        keys = {u.key for u in scan_workbook(conv).usages}
        self.assertNotIn("forecast", keys)
        self.assertNotIn("cluster", keys)
        self.assertNotIn("trend_line", keys)
        self.assertNotIn("reference_line", keys)

    def test_reference_lines_counted(self):
        conv = {"worksheets": [{"reference_lines": [{"type": "line"}, {"type": "band"}]}]}
        keys = {u.key: u for u in scan_workbook(conv).usages}
        self.assertEqual(keys["reference_line"].count, 2)
        self.assertEqual(keys["reference_line"].status, EXACT)

    def test_forecast_unsupported(self):
        conv = {"worksheets": [{"forecasting": [{"model": "auto"}]}]}
        scan = scan_workbook(conv)
        keys = {u.key: u for u in scan.usages}
        self.assertEqual(keys["forecast"].status, UNSUPPORTED)
        self.assertTrue(scan.unsupported_in_use)
        self.assertEqual(scan.grade, "PARTIAL")

    def test_trend_line_approximated(self):
        conv = {"worksheets": [{"trend_lines": [{"type": "linear"}]}]}
        keys = {u.key: u for u in scan_workbook(conv).usages}
        self.assertEqual(keys["trend_line"].status, APPROXIMATED)


class TestOtherDetectors(unittest.TestCase):
    def test_len_detectors(self):
        conv = {
            "parameters": [1, 2], "filters": [1], "sets": [1], "groups": [1, 2, 3],
            "bins": [1], "hierarchies": [1], "user_filters": [1],
            "custom_sql": [1], "data_blending": [1, 2], "hyper_files": [1],
        }
        keys = {u.key: u.count for u in scan_workbook(conv).usages}
        self.assertEqual(keys["parameters"], 2)
        self.assertEqual(keys["groups"], 3)
        self.assertEqual(keys["data_blending"], 2)

    def test_rls_healed(self):
        conv = {"user_filters": [{"name": "r"}]}
        keys = {u.key: u for u in scan_workbook(conv).usages}
        self.assertEqual(keys["rls"].status, HEALED)

    def test_action_classification(self):
        conv = {"actions": [
            {"type": "filter"}, {"type": "highlight"},
            {"type": "url"}, {"type": "navigate"},
        ]}
        keys = {u.key: u for u in scan_workbook(conv).usages}
        self.assertEqual(keys["action_filter"].count, 2)
        self.assertEqual(keys["action_url"].status, APPROXIMATED)
        self.assertEqual(keys["action_nav"].status, EXACT)

    def test_data_blending_approximated(self):
        keys = {u.key: u for u in scan_workbook({"data_blending": [1]}).usages}
        self.assertEqual(keys["data_blending"].status, APPROXIMATED)


class TestScoring(unittest.TestCase):
    def test_empty_workbook_full_score(self):
        scan = scan_workbook({})
        self.assertEqual(scan.parity_score, 100.0)
        self.assertEqual(scan.grade, "FULL")

    def test_all_exact_full(self):
        conv = {"filters": [1, 2, 3]}
        scan = scan_workbook(conv)
        self.assertEqual(scan.parity_score, 100.0)

    def test_approximated_half_credit(self):
        # 1 exact + 1 approximated => (1 + 0.5) / 2 = 75%
        conv = {"filters": [1], "data_blending": [1]}
        scan = scan_workbook(conv)
        self.assertEqual(scan.parity_score, 75.0)

    def test_unsupported_zero_credit(self):
        # 1 exact + 1 unsupported => (1 + 0) / 2 = 50%
        conv = {"filters": [1], "worksheets": [{"forecasting": [{"m": 1}]}]}
        scan = scan_workbook(conv)
        self.assertEqual(scan.parity_score, 50.0)
        self.assertEqual(scan.grade, "PARTIAL")

    def test_gaps_list(self):
        conv = {"filters": [1], "data_blending": [1],
                "worksheets": [{"forecasting": [{"m": 1}]}]}
        scan = scan_workbook(conv)
        gap_keys = {g.key for g in scan.gaps}
        self.assertEqual(gap_keys, {"data_blending", "forecast"})


class TestSerialization(unittest.TestCase):
    def setUp(self):
        self.scan = scan_workbook({"filters": [1], "data_blending": [1]}, "WB")

    def test_to_dict(self):
        d = self.scan.to_dict()
        self.assertEqual(d["workbook"], "WB")
        self.assertIn("parity_score", d)
        self.assertIn("status_counts", d)
        self.assertIn("usages", d)
        self.assertIn("gaps", d)

    def test_save_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "parity.json")
            self.scan.save_json(path)
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            self.assertEqual(loaded["workbook"], "WB")

    def test_to_html(self):
        html = self.scan.to_html()
        self.assertIn("Functionality parity", html)
        self.assertIn("<table", html)


class TestMCPIntegration(unittest.TestCase):
    """The MCP parity_scan tool should now return a real scan, not 'unavailable'."""

    def test_parity_scan_tool_uses_registry(self):
        from powerbi_import.mcp_server import MigrationTools
        sample = os.path.join(os.path.dirname(__file__), '..',
                              'examples', 'tableau_samples', 'Superstore_Sales.twb')
        if not os.path.isfile(sample):
            self.skipTest("sample workbook not present")
        res = MigrationTools().parity_scan({"file": sample})
        self.assertTrue(res["ok"])
        report = res["report"]
        # A real scan has parity_score; the old stub had status='unavailable'.
        self.assertIn("parity_score", report)
        self.assertNotEqual(report.get("status"), "unavailable")


if __name__ == "__main__":
    unittest.main()
