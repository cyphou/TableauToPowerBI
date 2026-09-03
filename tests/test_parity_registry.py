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
    collect_target_evidence, scan_project,
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

    def test_feature_evidence_is_preserved(self):
        conv = {
            "filters": [1],
            "_parity_evidence": {
                "filters": ["Report/definition/report.json#filterConfig"]
            },
        }
        usage = scan_workbook(conv).usages[0]
        self.assertEqual(usage.evidence, ["Report/definition/report.json#filterConfig"])
        self.assertEqual(scan_workbook(conv).to_dict()["usages"][0]["evidence"],
                         ["Report/definition/report.json#filterConfig"])


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

    def test_story_points_are_counted(self):
        scan = scan_workbook({
            "stories": [{"story_points": [{"caption": "Start"}, {"caption": "End"}]}]
        })
        usage = {u.key: u for u in scan.usages}["story_bookmarks"]
        self.assertEqual(usage.count, 2)
        self.assertEqual(usage.status, HEALED)

    def test_datasource_filters_are_separate_from_filters(self):
        scan = scan_workbook({"filters": [1], "datasource_filters": [1, 2]})
        keys = {u.key: u for u in scan.usages}
        self.assertEqual(keys["filters"].count, 1)
        self.assertEqual(keys["datasource_filter"].count, 2)
        self.assertEqual(keys["datasource_filter"].status, HEALED)

    def test_datasource_feature_families_are_detected(self):
        scan = scan_workbook({
            "table_extensions": [{"name": "Extension"}],
            "published_datasources": [{"name": "Published"}],
            "custom_geocoding": [{"name": "Postal"}],
            "linguistic_schema": {
                "Sales": ["Revenue", "Turnover"],
                "Region": ["Territory"],
            },
        })
        keys = {u.key: u for u in scan.usages}
        self.assertEqual(keys["table_extension"].count, 1)
        self.assertEqual(keys["published_datasource"].count, 1)
        self.assertEqual(keys["custom_geocoding"].count, 1)
        self.assertEqual(keys["linguistic_schema"].count, 2)

    def test_report_feature_families_are_detected(self):
        scan = scan_workbook({
            "dashboards": [{"name": "Sales"}],
            "aliases": [{"field": "Region"}, {"field": "Sales"}],
            "sort_orders": [{"field": "Month"}],
        })
        keys = {u.key: u for u in scan.usages}
        self.assertEqual(keys["dashboard"].count, 1)
        self.assertEqual(keys["alias"].count, 2)
        self.assertEqual(keys["sort_order"].count, 1)

    def test_operational_feature_families_are_detected(self):
        scan = scan_workbook({
            "schedules": [{"name": "Daily"}],
            "extract_tasks": [{"name": "Extract"}],
            "subscriptions": [{"name": "Finance"}],
        })
        keys = {u.key: u for u in scan.usages}
        self.assertEqual(keys["refresh_schedule"].count, 2)
        self.assertEqual(keys["subscription"].count, 1)


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
        self.assertIn("untracked_features", d)
        self.assertIn("evidence_coverage", d)

    def test_evidence_coverage_is_explicit(self):
        scan = scan_workbook({
            "filters": [1],
            "parameters": [1],
            "_parity_evidence": {"filters": ["report.json"]},
        })
        self.assertEqual(scan.evidence_coverage["tracked_features"], 2)
        self.assertEqual(scan.evidence_coverage["evidenced_features"], 1)
        self.assertEqual(scan.evidence_coverage["coverage_percent"], 50.0)

    def test_report_feature_families_are_no_longer_untracked(self):
        scan = scan_workbook({
            "dashboards": [{"name": "Dashboard"}],
            "aliases": [{"field": "Region"}],
            "sort_orders": [{"field": "Month"}],
        })
        self.assertEqual(scan.untracked_features, [])

        scan = scan_workbook({"future_feature": [{"name": "Unknown"}]})
        self.assertEqual(scan.untracked_features, [])

    def test_empty_untracked_source_features_are_ignored(self):
        scan = scan_workbook({"dashboards": [], "aliases": {}, "sort_orders": []})
        self.assertEqual(scan.untracked_features, [])

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


class TestTargetEvidence(unittest.TestCase):
    def test_collects_pbir_and_tmdl_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = os.path.join(tmp, "Demo.Report", "definition")
            model = os.path.join(tmp, "Demo.SemanticModel", "definition", "tables")
            os.makedirs(report, exist_ok=True)
            os.makedirs(model, exist_ok=True)
            with open(os.path.join(report, "report.json"), "w", encoding="utf-8") as fh:
                json.dump({"filterConfig": {"filters": [{"name": "f1"}]}}, fh)
            with open(os.path.join(model, "Orders.tmdl"), "w", encoding="utf-8") as fh:
                fh.write("table Orders\n\tmeasure 'Top N' = 10\n")
            evidence = collect_target_evidence(tmp, "Demo")

        self.assertEqual(evidence["filters"], ["Demo.Report/definition/report.json"])
        self.assertEqual(evidence["parameters"],
                         ["Demo.SemanticModel/definition/tables/Orders.tmdl"])

    def test_scan_project_attaches_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = os.path.join(tmp, "Demo.Report", "definition")
            os.makedirs(report, exist_ok=True)
            with open(os.path.join(report, "report.json"), "w", encoding="utf-8") as fh:
                json.dump({"filterConfig": {"filters": [{"name": "f1"}]}}, fh)
            scan = scan_project({"filters": [1]}, tmp, "Demo")

        self.assertEqual(scan.usages[0].evidence,
                         ["Demo.Report/definition/report.json"])

    def test_collects_bookmark_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            bookmark = os.path.join(tmp, "Demo.Report", "definition",
                                    "bookmarks", "b1", "bookmark.json")
            os.makedirs(os.path.dirname(bookmark), exist_ok=True)
            with open(bookmark, "w", encoding="utf-8") as fh:
                fh.write('{}')
            evidence = collect_target_evidence(tmp, "Demo")
        self.assertEqual(evidence["story_bookmarks"],
                         ["Demo.Report/definition/bookmarks/b1/bookmark.json"])

    def test_collects_dashboard_page_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = os.path.join(tmp, "Demo.Report", "definition", "pages",
                                "p1", "page.json")
            os.makedirs(os.path.dirname(page), exist_ok=True)
            with open(page, "w", encoding="utf-8") as fh:
                fh.write('{}')
            evidence = collect_target_evidence(tmp, "Demo")
        self.assertEqual(evidence["dashboard"], ["Demo.Report/definition/pages/p1/page.json"])

    def test_collects_operational_configuration_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            for filename in ("refresh_config.json", "pbi_subscriptions.json"):
                with open(os.path.join(tmp, filename), "w", encoding="utf-8") as fh:
                    fh.write('{}')
            evidence = collect_target_evidence(tmp, "Demo")
        self.assertEqual(evidence["refresh_schedule"], ["refresh_config.json"])
        self.assertEqual(evidence["subscription"], ["pbi_subscriptions.json"])


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
