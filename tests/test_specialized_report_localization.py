"""Actual specialized report outputs: opt-in UI translation, never evidence.

All inputs are small synthetic in-memory models.  In particular these tests do
not require Tableau Server, Power BI, a browser, or customer workbooks.
"""

import html
from pathlib import Path
import tempfile
import unittest

from powerbi_import import (
    artifact_diff, cutover_manager, dependency_graph, global_assessment,
    merge_assessment, permission_mapper, prep_lineage_report, qa_suite,
    recovery_report, server_assessment, subscription_generator,
)
from powerbi_import.html_template import get_report_js, html_close, html_open
from powerbi_import.prep_lineage import FlowProfile, PrepLineageGraph
from powerbi_import.report_localization import text
from powerbi_import.shared_model import MeasureConflict, MergeAssessment
from tests.test_report_localization import _Document


NAME = 'Sales <North> & "café"'
RAW = 'raw <script>alert("evidence")</script> & diagnostic'
FORMULA = 'IF([Amount] < 5, "A&B", "évidence")'


class TestSpecializedReportLocalization(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def path(self, name):
        return str(self.directory / (name + ".html"))

    def read(self, path):
        return Path(path).read_text(encoding="utf-8")

    def assert_report(self, source, title, section, header, evidence=(),
                      codes=(), theme=True):
        """Check rendered nodes, not just strings also present in JS/CSS."""
        document = _Document(source).root
        title_node, = list(document.descendants("title"))
        self.assertEqual(title_node.text(), title)
        self.assertEqual(title_node.attrs.get("data-i18n-en"), title)
        self.assertTrue(title_node.attrs.get("data-i18n-fr"))
        self.assertNotEqual(title_node.attrs["data-i18n-fr"], title)

        def localized_in(tags, english):
            matches = [
                node for tag in tags for node in document.descendants(tag)
                if english in node.text()
                and any(n.attrs.get("data-i18n-en") == english
                        for n in [node, *node.descendants()])
            ]
            self.assertTrue(matches, f"Missing localized {tags}: {english}")
            translations = [
                n.attrs["data-i18n-fr"]
                for node in matches for n in [node, *node.descendants()]
                if n.attrs.get("data-i18n-en") == english
            ]
            self.assertTrue(all(translations))
            self.assertTrue(any(value != english for value in translations))

        localized_in(("h1",), title if title !=
                     "Global Assessment — Merge Analysis" else
                     "Global Assessment — Cross-Workbook Merge Analysis")
        localized_in(("h2", "h3", "h4"), section)
        localized_in(("th",), header)

        language, = [n for n in document.descendants("select")
                     if n.attrs.get("id") == "report-language"]
        self.assertEqual(language.attrs["data-i18n-aria-label-en"],
                         "Choose report language")
        self.assertEqual(language.attrs["data-i18n-aria-label-fr"],
                         "Choisir la langue du rapport")
        self.assertEqual({n.attrs["value"] for n in language.descendants("option")},
                         {"en", "fr"})
        self.assertIn("reportlanguagechange", source)
        if theme:
            button, = [n for n in document.descendants("button")
                       if n.attrs.get("id") == "theme-toggle-btn"]
            self.assertEqual(button.attrs["data-i18n-aria-label-fr"],
                             "Changer le thème de couleurs")
            self.assertEqual(button.attrs["data-i18n-title-fr"],
                             "Changer le thème de couleurs")

        # The language switch assigns textContent. Translation hooks must only
        # wrap text leaves, never a section, row, link container, or code panel.
        for node in document.descendants(attribute="data-i18n-en"):
            self.assertFalse(list(node.descendants()),
                             f"Translation would erase markup: {node.tag} "
                             f"{node.attrs['data-i18n-en']!r}")

        for value in evidence:
            with self.subTest(evidence=value):
                self.assertIn(html.escape(value, quote=True), source)
                cells = [n for tag in ("td", "pre", "code")
                         for n in document.descendants(tag)
                         if value in n.text()]
                self.assertTrue(cells, f"Missing visible evidence: {value}")
                # Evidence is not a translation target, even when its wording
                # happens to resemble an authored UI label.
                for cell in cells:
                    self.assertNotIn("data-i18n-en", cell.attrs)
                    for node in cell.descendants(attribute="data-i18n-en"):
                        self.assertNotIn(value, node.attrs["data-i18n-en"])
        for node in document.descendants("script"):
            self.assertNotIn('alert("evidence")', node.text())

        for code in codes:
            with self.subTest(canonical_code=code):
                matches = [n for tag in ("td", "span", "code", "pre")
                           for n in document.descendants(tag)
                           if n.text() == code]
                self.assertTrue(matches, f"Canonical code lost: {code}")
                for node in matches:
                    if "data-i18n-en" in node.attrs:
                        self.assertEqual(node.attrs["data-i18n-fr"], code)
        return document

    def test_artifact_diff(self):
        report = artifact_diff.DiffReport([
            artifact_diff.DiffEntry("measure", "modified", NAME, parent="Facts",
                                    before=FORMULA, after=RAW, detail=RAW),
        ], old_path="before", new_path="after")
        source = artifact_diff.generate_diff_report(report, self.path("diff"))
        self.assertEqual(source, self.read(self.path("diff")))
        document = self.assert_report(
            source, "Artifact Diff Report", "Formula Changes", "Name",
            evidence=(NAME, RAW, FORMULA), codes=("MODIFIED",))
        self.assertTrue(list(document.descendants("pre")))

    def test_cutover_manager(self):
        path = cutover_manager.generate_cutover_dashboard(
            {"total_workbooks": 1, "cutover_date": "2026-10-15",
             "pre_checks": [{"label": NAME, "required": True, "status": "passed"}]},
            execution_result={"status": "failed", "stages": [
                {"id": "deploy_reports", "status": "failed", "errors": [RAW]}]},
            parallel_results={"status": "fail", "match_rate": 0,
                              "details": {"mismatched": [
                                  {"measure": NAME, "tableau": 1, "pbi": 2,
                                   "diff_pct": 100}]}},
            output_path=self.path("cutover"))
        self.assert_report(self.read(path), "Cutover Dashboard",
                           "Pre-Cutover Checklist", "Check",
                           evidence=(NAME, RAW), codes=("PASS", "FAIL"))

    def test_dependency_graph(self):
        path = dependency_graph.generate_topology_report(
            {"workbooks": [{"name": NAME, "project": {"name": RAW}}]},
            {"nodes": [{"id": "ds1", "name": NAME, "type": "datasource"}],
             "topological_order": ["ds1"],
             "shared_datasources": [{"ds_name": NAME, "workbook_count": 1,
                                     "workbook_names": [RAW]}]},
            {"summary": {"active_count": 1}}, {},
            self.path("topology"))
        self.assert_report(self.read(path), "Tableau Server — Site Topology Report",
                           "Workbook Inventory", "Workbook",
                           evidence=(NAME, RAW), codes=("datasource",))

    def test_permission_mapper(self):
        path = permission_mapper.generate_permission_report(
            [{"identity": NAME, "source_role": "Creator",
              "pbi_role": "Contributor", "groups": [RAW]}],
            {"unresolved": [{"role": "Sales_RLS", "tableau_name": NAME,
                             "reason": RAW}]},
            output_path=self.path("permissions"))
        self.assert_report(self.read(path), "Permission Migration Report",
                           "Workspace Role Assignments", "Identity",
                           evidence=(NAME, RAW),
                           codes=("Creator", "Contributor", "Sales_RLS"))

    def test_qa_suite(self):
        report = qa_suite.QAReport(
            NAME, checks=[
                qa_suite.QACheck("bindings", NAME, False, summary=RAW,
                                 evidence=[RAW, FORMULA]),
                qa_suite.QACheck("valid", "Bindings", True),
                qa_suite.QACheck("optional", "Optional", False, skipped=True),
            ], tableau_counts={"worksheets": 2}, pbi_counts={"charts": 1})
        path = qa_suite.generate_qa_html(report, self.path("qa"))
        self.assert_report(self.read(path), "Migration QA Report Card",
                           "QA Checks", "Evidence",
                           evidence=(NAME, RAW, FORMULA),
                           codes=("PASS", "FAIL", "SKIP", "worksheets"))

    def test_recovery_report(self):
        report = recovery_report.RecoveryReport("Synthetic workbook")
        report.record("m_query", "try_otherwise_wrap", item_name=NAME,
                      description=RAW, action=RAW, severity="warning",
                      original_value=FORMULA, repaired_value=RAW, follow_up=RAW)
        path = report.save_html(str(self.directory))
        document = self.assert_report(
            self.read(path), "Self-Healing Recovery Report",
            "Per-Artifact Repair Audit", "Repair Type",
            evidence=(NAME, RAW, FORMULA),
            codes=("m_query", "try_otherwise_wrap", "WARNING"))
        self.assertTrue(list(document.descendants("code")))
        search, = list(document.descendants("input"))
        self.assertEqual(search.attrs["data-i18n-placeholder-fr"],
                         "Filtrer les lignes…")

    def test_subscription_generator(self):
        path = subscription_generator.generate_subscription_report(
            [{"content_name": NAME, "recipient_email": "synthetic@example.invalid",
              "frequency": "daily", "schedule_name": RAW}],
            [{"subject": NAME, "condition": "above", "threshold": 10,
              "view_name": RAW}],
            [{"frequency": "daily"}], [],
            [{"severity": "error", "message": RAW}],
            self.path("subscriptions"))
        self.assert_report(self.read(path), "Subscription & Alert Migration Report",
                           "Subscription Mapping", "Recipient",
                           evidence=(NAME, RAW), codes=("daily", "above", "ERROR"))

    def test_global_assessment(self):
        result = global_assessment.GlobalAssessment(
            workbook_profiles=[
                global_assessment.WorkbookProfile(NAME, connection_types=["sqlserver"]),
                global_assessment.WorkbookProfile(RAW)],
            pairwise_scores=[global_assessment.PairwiseScore(NAME, RAW, 80, 1, "merge")],
            merge_clusters=[global_assessment.MergeCluster(
                0, [NAME, RAW], ["Facts"], 80)],
            total_workbooks=2, total_tables=2)
        path = global_assessment.generate_global_html_report(
            result, self.path("global"))
        document = self.assert_report(
            self.read(path), "Global Assessment — Merge Analysis",
            "Workbook Inventory", "Workbook",
            evidence=(NAME, RAW), codes=("MERGE", "sqlserver"), theme=False)
        self.assertTrue(list(document.descendants("strong")))

    def test_merge_assessment(self):
        result = MergeAssessment(
            workbooks=[NAME], recommendation="partial", merge_score=40,
            measure_conflicts=[MeasureConflict(NAME, "Facts", {NAME: FORMULA})])
        path = merge_assessment.generate_merge_html_report(
            result, self.path("merge"),
            rls_conflicts=[{"role_name": NAME, "table": "Facts",
                            "variants": {NAME: RAW}}],
            relationship_suggestions=[{"from_table": NAME, "confidence": "high"}])
        document = self.assert_report(
            self.read(path), "Shared Semantic Model — Merge Assessment",
            "Measure Conflicts", "Formula",
            evidence=(NAME, RAW, FORMULA), codes=("high",))
        self.assertTrue(list(document.descendants("code")))
        self.assertEqual(result.recommendation, "partial")

    def test_prep_lineage_report(self):
        graph = PrepLineageGraph(flows=[
            FlowProfile(NAME, "synthetic.tfl",
                        m_queries={NAME: RAW},
                        assessment={"grade": "YELLOW", "warn_count": 1})])
        path = prep_lineage_report.generate_prep_lineage_report(
            graph, [], self.path("prep"))
        document = self.assert_report(
            self.read(path), "Tableau Prep — Flow Lineage Report",
            "Flow Inventory", "Flow", evidence=(NAME, RAW), codes=("YELLOW",))
        self.assertTrue(list(document.descendants("pre")))

    def test_prep_empty_query_guidance_preserves_inline_cli_markup(self):
        path = prep_lineage_report.generate_prep_lineage_report(
            PrepLineageGraph(), [], self.path("prep-empty"))
        document = self.assert_report(
            self.read(path), "Tableau Prep — Flow Lineage Report",
            "Flow Inventory", "Flow", codes=("--prep-lineage",))
        option, = [n for n in document.descendants("code")
                   if n.text() == "--prep-lineage"]
        self.assertEqual(option.parent.tag, "p")
        self.assertNotIn("data-i18n-en", option.parent.attrs)
        self.assertTrue(list(option.parent.descendants(attribute="data-i18n-en")),
                        "Translate guidance around, not over, the CLI code")

    def test_server_assessment(self):
        result = server_assessment.ServerAssessment(
            workbook_results=[server_assessment.WorkbookReadiness(
                NAME, "YELLOW", connector_types=[RAW],
                lineage={"status": "not_available"},
                server_evidence={"risk": {"level": "not_run"}})],
            waves=[server_assessment.MigrationWave(1, "Medium", [NAME])],
            total_workbooks=1, yellow_count=1)
        path = server_assessment.generate_server_html_report(
            result, self.path("server"))
        document = self.assert_report(
            self.read(path), "Tableau Server — Portfolio Assessment",
            "Workbook Detail", "Workbook", evidence=(NAME, RAW),
            codes=("YELLOW", "not_available", "not_run", "Medium"))
        self.assertTrue(list(document.descendants("strong")))


class TestSharedThemeLocalization(unittest.TestCase):
    def test_theme_accessibility_and_language_change_hooks(self):
        source = html_open(text("Report", "Rapport"), language="fr") + html_close()
        document = _Document(source).root
        button, = [n for n in document.descendants("button")
                   if n.attrs.get("id") == "theme-toggle-btn"]
        self.assertIn("Sombre", button.text())
        for attribute in ("title", "aria-label"):
            self.assertEqual(button.attrs["data-i18n-" + attribute + "-en"],
                             "Switch color theme")
            self.assertEqual(button.attrs["data-i18n-" + attribute + "-fr"],
                             "Changer le thème de couleurs")
        script = get_report_js()
        self.assertIn("☼ Clair", script)
        self.assertIn("☾ Sombre", script)
        self.assertIn("ReportI18n.format(en, fr)", script)
        self.assertIn("'reportlanguagechange', updateThemeLabel", script)


if __name__ == "__main__":
    unittest.main()
