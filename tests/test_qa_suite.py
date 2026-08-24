"""
Tests for the Real-World Migration QA Suite (powerbi_import.qa_suite).

Sprint 207.5 — positive/negative fixtures for each of the five report-card
checks plus aggregate-report and HTML-rendering coverage.
"""

import json
import os
import sys
import tempfile
import shutil
import unittest

# Ensure parent dir on path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.qa_suite import (
    QACheck, QAReport,
    run_qa_suite, generate_qa_html,
    STRAY_SENTINELS, SENTINEL_NAMES,
    _check_no_stray_sentinels, _check_no_empty_visuals,
    _check_format_coverage, _check_zones_matched, _check_no_orphan_filters,
    _iter_visual_files, _find_report_dir, _encoded_field_count,
    _has_static_content, _text_run_values, _filter_has_field,
)


# ── Fixture builders ────────────────────────────────────────────────

def _chart_visual(projections=2, vtype="clusteredBarChart", with_format=True):
    """A data-bearing chart visual with `projections` projected fields."""
    proj = [{"field": {"Column": {"Property": f"c{i}"}}} for i in range(projections)]
    visual = {
        "visualType": vtype,
        "query": {"queryState": {"Category": {"projections": proj}}},
    }
    if with_format:
        visual["objects"] = {"general": [{"properties": {}}]}
    return {"visual": visual}


def _textbox_visual(text="Hello", with_format=True):
    visual = {
        "visualType": "textbox",
        "objects": {
            "general": [
                {"properties": {"paragraphs": [
                    {"textRuns": [{"value": text}]}
                ]}}
            ]
        } if with_format else {},
    }
    return {"visual": visual}


def _empty_visual(vtype="clusteredBarChart"):
    """No projections and not a static-content type → empty defect."""
    return {"visual": {"visualType": vtype, "objects": {"general": []}}}


def _image_visual():
    return {"visual": {"visualType": "image",
                       "objects": {"general": [{"properties": {"imageUrl": "x"}}]}}}


class _ProjectFixture(unittest.TestCase):
    """Builds a minimal .pbip project tree under a temp dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qa_suite_test_")
        self.name = "wb"
        self.report_dir = os.path.join(self.tmp, f"{self.name}.Report")
        os.makedirs(os.path.join(self.report_dir, "definition", "pages"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_visual(self, page, vid, data):
        vdir = os.path.join(self.report_dir, "definition", "pages",
                            page, "visuals", vid)
        os.makedirs(vdir, exist_ok=True)
        with open(os.path.join(vdir, "visual.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def add_page_json(self, page, data):
        pdir = os.path.join(self.report_dir, "definition", "pages", page)
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "page.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def add_report_json(self, data):
        with open(os.path.join(self.report_dir, "definition", "report.json"),
                  "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def make_extraction_dir(self, dashboards):
        ext = os.path.join(self.tmp, "extract")
        os.makedirs(ext, exist_ok=True)
        with open(os.path.join(ext, "dashboards.json"), "w", encoding="utf-8") as fh:
            json.dump(dashboards, fh)
        return ext

    def load_visuals(self):
        out = []
        for vf in _iter_visual_files(self.tmp):
            with open(vf, "r", encoding="utf-8") as fh:
                out.append((vf, json.load(fh)))
        return out


# ── Helper-function tests ───────────────────────────────────────────

class TestHelpers(unittest.TestCase):

    def test_text_run_values_collects_nested(self):
        vals = _text_run_values(_textbox_visual("ABC"))
        self.assertIn("ABC", vals)

    def test_encoded_field_count(self):
        self.assertEqual(_encoded_field_count(_chart_visual(3)), 3)
        self.assertEqual(_encoded_field_count(_empty_visual()), 0)

    def test_has_static_content_textbox(self):
        self.assertTrue(_has_static_content(_textbox_visual("text")))
        self.assertFalse(_has_static_content(_textbox_visual("")))

    def test_has_static_content_image(self):
        self.assertTrue(_has_static_content(_image_visual()))

    def test_has_static_content_chart_is_false(self):
        self.assertFalse(_has_static_content(_chart_visual(2)))

    def test_filter_has_field_positive(self):
        self.assertTrue(_filter_has_field({"field": {"Column": {"Property": "x"}}}))

    def test_filter_has_field_negative(self):
        self.assertFalse(_filter_has_field({"field": {}}))
        self.assertFalse(_filter_has_field({}))


# ── Check: no stray sentinels ───────────────────────────────────────

class TestStraySentinels(_ProjectFixture):

    def test_clean_passes(self):
        self.add_visual("p1", "v1", _textbox_visual("Clean title"))
        chk = _check_no_stray_sentinels(self.tmp, self.load_visuals())
        self.assertTrue(chk.passed)
        self.assertEqual(chk.severity, "error")

    def test_ae_sentinel_fails(self):
        self.add_visual("p1", "v1", _textbox_visual("Bad\u00c6title"))
        chk = _check_no_stray_sentinels(self.tmp, self.load_visuals())
        self.assertFalse(chk.passed)
        self.assertTrue(chk.evidence)

    def test_oe_sentinel_fails(self):
        self.add_visual("p1", "v1", _textbox_visual("x\u0152y"))
        chk = _check_no_stray_sentinels(self.tmp, self.load_visuals())
        self.assertFalse(chk.passed)

    def test_nbsp_sentinel_fails(self):
        self.add_visual("p1", "v1", _textbox_visual("a\u00a0b"))
        chk = _check_no_stray_sentinels(self.tmp, self.load_visuals())
        self.assertFalse(chk.passed)

    def test_all_sentinels_named(self):
        for s in STRAY_SENTINELS:
            self.assertIn(s, SENTINEL_NAMES)


# ── Check: no empty visuals ─────────────────────────────────────────

class TestEmptyVisuals(_ProjectFixture):

    def test_chart_with_fields_passes(self):
        self.add_visual("p1", "v1", _chart_visual(2))
        chk = _check_no_empty_visuals(self.tmp, self.load_visuals())
        self.assertTrue(chk.passed)

    def test_textbox_with_text_passes(self):
        self.add_visual("p1", "v1", _textbox_visual("Title"))
        chk = _check_no_empty_visuals(self.tmp, self.load_visuals())
        self.assertTrue(chk.passed)

    def test_empty_chart_fails(self):
        self.add_visual("p1", "v1", _empty_visual())
        chk = _check_no_empty_visuals(self.tmp, self.load_visuals())
        self.assertFalse(chk.passed)
        self.assertEqual(chk.severity, "error")

    def test_image_visual_passes(self):
        self.add_visual("p1", "v1", _image_visual())
        chk = _check_no_empty_visuals(self.tmp, self.load_visuals())
        self.assertTrue(chk.passed)


# ── Check: format coverage ──────────────────────────────────────────

class TestFormatCoverage(_ProjectFixture):

    def test_with_objects_passes(self):
        self.add_visual("p1", "v1", _chart_visual(2, with_format=True))
        chk = _check_format_coverage(self.tmp, self.load_visuals())
        self.assertTrue(chk.passed)

    def test_missing_objects_fails(self):
        self.add_visual("p1", "v1", _chart_visual(2, with_format=False))
        chk = _check_format_coverage(self.tmp, self.load_visuals())
        self.assertFalse(chk.passed)
        self.assertEqual(chk.severity, "warning")


# ── Check: dashboard zones matched ──────────────────────────────────

class TestZonesMatched(_ProjectFixture):

    def test_skipped_when_no_extraction(self):
        self.add_visual("p1", "v1", _chart_visual(2))
        chk = _check_zones_matched(self.tmp, self.load_visuals(), None)
        self.assertTrue(chk.skipped)
        self.assertEqual(chk.status, "SKIP")

    def test_skipped_when_no_dashboards_json(self):
        self.add_visual("p1", "v1", _chart_visual(2))
        empty_ext = os.path.join(self.tmp, "empty_ext")
        os.makedirs(empty_ext)
        chk = _check_zones_matched(self.tmp, self.load_visuals(), empty_ext)
        self.assertTrue(chk.skipped)

    def test_full_coverage_passes(self):
        self.add_visual("p1", "v1", _chart_visual(2))
        ext = self.make_extraction_dir([
            {"objects": [{"type": "worksheet", "name": "Sheet1"}]}
        ])
        chk = _check_zones_matched(self.tmp, self.load_visuals(), ext)
        self.assertTrue(chk.passed)
        self.assertFalse(chk.skipped)

    def test_coverage_gap_fails(self):
        # No data visuals but two worksheet zones.
        self.add_visual("p1", "v1", _textbox_visual("just text"))
        ext = self.make_extraction_dir([
            {"objects": [
                {"type": "worksheet", "name": "S1"},
                {"type": "worksheet", "name": "S2"},
            ]}
        ])
        chk = _check_zones_matched(self.tmp, self.load_visuals(), ext)
        self.assertFalse(chk.passed)
        self.assertTrue(chk.evidence)


# ── Check: no orphan filters ────────────────────────────────────────

class TestOrphanFilters(_ProjectFixture):

    def test_no_filters_passes(self):
        self.add_visual("p1", "v1", _chart_visual(2))
        chk = _check_no_orphan_filters(self.tmp, self.load_visuals())
        self.assertTrue(chk.passed)

    def test_valid_filter_passes(self):
        data = _chart_visual(2)
        data["filterConfig"] = {"filters": [
            {"name": "f1", "field": {"Column": {"Property": "c0"}}}
        ]}
        self.add_visual("p1", "v1", data)
        chk = _check_no_orphan_filters(self.tmp, self.load_visuals())
        self.assertTrue(chk.passed)

    def test_orphan_filter_fails(self):
        data = _chart_visual(2)
        data["filterConfig"] = {"filters": [{"name": "orphan", "field": {}}]}
        self.add_visual("p1", "v1", data)
        chk = _check_no_orphan_filters(self.tmp, self.load_visuals())
        self.assertFalse(chk.passed)
        self.assertEqual(chk.severity, "warning")

    def test_orphan_filter_in_page_json_fails(self):
        self.add_visual("p1", "v1", _chart_visual(2))
        self.add_page_json("p1", {"filterConfig": {
            "filters": [{"name": "pageorphan", "field": {}}]
        }})
        chk = _check_no_orphan_filters(self.tmp, self.load_visuals())
        self.assertFalse(chk.passed)


# ── Aggregate report tests ──────────────────────────────────────────

class TestQAReportAggregate(_ProjectFixture):

    def test_clean_project_overall_pass(self):
        self.add_visual("p1", "v1", _chart_visual(2))
        self.add_visual("p1", "v2", _textbox_visual("Title"))
        report = run_qa_suite(self.tmp, workbook="wb")
        self.assertTrue(report.passed)
        self.assertEqual(report.fail_count, 0)
        self.assertGreaterEqual(report.total, 4)

    def test_defect_project_overall_fail(self):
        self.add_visual("p1", "v1", _textbox_visual("Bad\u00c6"))
        report = run_qa_suite(self.tmp, workbook="wb")
        self.assertFalse(report.passed)
        self.assertTrue(report.has_error_failure)

    def test_pass_fail_skip_counts(self):
        self.add_visual("p1", "v1", _chart_visual(2))
        report = run_qa_suite(self.tmp, workbook="wb")
        # zones_matched is skipped (no extraction dir)
        self.assertEqual(report.skip_count, 1)
        self.assertEqual(report.pass_count + report.fail_count, report.total)

    def test_to_dict_roundtrip(self):
        self.add_visual("p1", "v1", _chart_visual(2))
        report = run_qa_suite(self.tmp, workbook="wb")
        d = report.to_dict()
        self.assertIn("checks", d)
        self.assertIn("passed", d)
        self.assertEqual(d["workbook"], "wb")

    def test_save_json(self):
        self.add_visual("p1", "v1", _chart_visual(2))
        report = run_qa_suite(self.tmp, workbook="wb")
        out = os.path.join(self.tmp, "qa.json")
        report.save_json(out)
        self.assertTrue(os.path.exists(out))
        with open(out, "r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        self.assertIn("checks", loaded)

    def test_warning_failure_not_error_failure(self):
        # Missing format → warning-severity failure only.
        self.add_visual("p1", "v1", _chart_visual(2, with_format=False))
        report = run_qa_suite(self.tmp, workbook="wb")
        self.assertFalse(report.has_error_failure)

    def test_fidelity_surfaced(self):
        self.add_visual("p1", "v1", _chart_visual(2))
        report = run_qa_suite(self.tmp, workbook="wb", fidelity=92.5)
        self.assertEqual(report.fidelity, 92.5)


class TestQACheckDataclass(unittest.TestCase):

    def test_status_pass(self):
        c = QACheck(key="k", name="n", passed=True)
        self.assertEqual(c.status, "PASS")

    def test_status_fail(self):
        c = QACheck(key="k", name="n", passed=False)
        self.assertEqual(c.status, "FAIL")

    def test_status_skip(self):
        c = QACheck(key="k", name="n", passed=True, skipped=True)
        self.assertEqual(c.status, "SKIP")

    def test_to_dict(self):
        c = QACheck(key="k", name="n", passed=True, summary="ok")
        d = c.to_dict()
        self.assertEqual(d["status"], "PASS")
        self.assertEqual(d["key"], "k")


# ── HTML report tests ───────────────────────────────────────────────

class TestQAHtml(_ProjectFixture):

    def test_generate_html(self):
        self.add_visual("p1", "v1", _chart_visual(2))
        report = run_qa_suite(self.tmp, workbook="wb", fidelity=90.0)
        out = os.path.join(self.tmp, "qa_report.html")
        path = generate_qa_html(report, out)
        self.assertEqual(path, out)
        self.assertTrue(os.path.exists(out))
        with open(out, "r", encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("QA Checks", html)
        self.assertIn("wb", html)

    def test_html_contains_check_names(self):
        self.add_visual("p1", "v1", _textbox_visual("Bad\u00c6"))
        report = run_qa_suite(self.tmp, workbook="wb")
        out = os.path.join(self.tmp, "qa_report.html")
        generate_qa_html(report, out)
        with open(out, "r", encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("sentinel", html.lower())


# ── Autoplay integration (Sprint 207.4) ─────────────────────────────

class TestAutoplayQAStep(_ProjectFixture):

    def _make_pbip(self):
        # A .pbip file + SemanticModel so autoplay's earlier steps don't crash.
        with open(os.path.join(self.tmp, f"{self.name}.pbip"), "w",
                  encoding="utf-8") as fh:
            fh.write("{}")

    def test_autoplay_includes_qa_step(self):
        from scripts.autoplay import run_autoplay
        self._make_pbip()
        self.add_visual("p1", "v1", _chart_visual(2))
        results = run_autoplay(self.tmp)
        self.assertIn("6_qa_report", results["steps"])
        s6 = results["steps"]["6_qa_report"]
        self.assertIn(s6["status"], ("pass", "warn", "fail", "skip"))
        self.assertEqual(s6.get("total"), 4)

    def test_autoplay_qa_error_fails_overall(self):
        from scripts.autoplay import run_autoplay
        self._make_pbip()
        self.add_visual("p1", "v1", _textbox_visual("Bad\u00c6"))
        results = run_autoplay(self.tmp)
        s6 = results["steps"]["6_qa_report"]
        self.assertEqual(s6["status"], "fail")
        self.assertFalse(results["overall_pass"])


if __name__ == "__main__":
    unittest.main()
