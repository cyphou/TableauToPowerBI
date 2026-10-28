"""Focused regressions for bilingual, evidence-preserving HTML reports."""

import html
import json
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from powerbi_import.desktop_comparison import (
    _page_totals,
    write_comparison_reports,
)
from powerbi_import.html_template import data_table, esc, html_close, html_open
from powerbi_import.migration_quality import (
    MigrationQualityReport,
    build_consolidated_quality_html,
)
from powerbi_import.report_localization import text
from tests.pixel_comparison_fixtures import forbid_network


class _Node:
    def __init__(self, tag="", attrs=(), parent=None):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children = []

    def descendants(self, tag=None, attribute=None):
        for child in self.children:
            if isinstance(child, _Node):
                if ((tag is None or child.tag == tag)
                        and (attribute is None or attribute in child.attrs)):
                    yield child
                yield from child.descendants(tag, attribute)

    def text(self):
        return "".join(child.text() if isinstance(child, _Node) else child
                       for child in self.children)


class _Document(HTMLParser):
    VOID_TAGS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.current = self.root
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in self.VOID_TAGS:
            self.current = node

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                break
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def _localized_node(document, english):
    matches = [
        node for node in document.descendants(attribute="data-i18n-en")
        if node.attrs["data-i18n-en"] == english
    ]
    if len(matches) != 1:
        raise AssertionError(f"Expected one localized node for {english!r}, got {len(matches)}")
    return matches[0]


class TestSharedHtmlTemplateLocalization(unittest.TestCase):
    def test_language_title_aria_and_search_hooks_keep_opt_in_translation(self):
        title = text('Workbook <North> & "West"', 'Classeur <Nord> & "Ouest"')
        document = html_open(title, text("Validation summary", "Résumé de validation"),
                             timestamp="2026-10-15 12:00", language="fr")
        document += data_table(
            [text("Workbook", "Classeur")], [[esc("Sales <raw> & evidence")]],
            table_id="localization-table", searchable=True,
        )
        document += html_close(timestamp="2026-10-15 12:00")
        parsed = _Document(document).root

        html_node, = list(parsed.descendants("html"))
        self.assertEqual(html_node.attrs["lang"], "fr")
        title_node, = list(parsed.descendants("title"))
        self.assertEqual(title_node.text(), 'Classeur <Nord> & "Ouest"')
        self.assertEqual(title_node.attrs["data-i18n-en"],
                         'Workbook <North> & "West"')
        self.assertEqual(title_node.attrs["data-i18n-fr"],
                         'Classeur <Nord> & "Ouest"')

        selector, = list(parsed.descendants("select"))
        self.assertEqual(selector.attrs["id"], "report-language")
        self.assertEqual(selector.attrs["data-i18n-aria-label-en"],
                         "Choose report language")
        self.assertEqual(selector.attrs["data-i18n-aria-label-fr"],
                         "Choisir la langue du rapport")
        self.assertEqual(selector.attrs["aria-label"], "Choisir la langue du rapport")
        self.assertEqual(_localized_node(parsed, "Validation summary").text(),
                         "Résumé de validation")
        workbook_header, = [node for node in parsed.descendants(attribute="data-i18n-en")
                            if node.attrs["data-i18n-en"] == "Workbook"]
        self.assertEqual(workbook_header.attrs["data-i18n-fr"], "Classeur")

        search, = [node for node in parsed.descendants("input")
                   if node.attrs.get("id") == "search-localization-table"]
        self.assertEqual(search.attrs["data-i18n-placeholder-en"], "Filter rows...")
        self.assertEqual(search.attrs["data-i18n-placeholder-fr"], "Filtrer les lignes…")
        self.assertEqual(search.attrs["data-i18n-aria-label-en"], "Filter rows")
        # Ordinary evidence is escaped but has no translation metadata.
        self.assertIn("Sales &lt;raw&gt; &amp; evidence", document)
        self.assertNotIn("data-i18n-en=\"Sales", document)


class TestDesktopComparisonLocalization(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.run_dir = Path(temporary.name) / "comparison"
        captures = self.run_dir / "captures"
        captures.mkdir(parents=True)
        forbid_network(self)
        for filename in ("source.png", "target.png", "opening.png"):
            (captures / filename).write_bytes(b"local test evidence")

        def capture(filename):
            return {
                "status": "captured",
                "screenshot": str(captures / filename),
                "screenshot_blank": False,
                "selection": {"status": "verified"},
                "post_capture_selection": {"status": "verified"},
                "note": '<capture & raw "diagnostic">',
            }

        def missing_capture():
            return {"status": "error", "screenshot": None, "note": "Page capture failed"}

        # A page is ready for review only when the Power BI launch also passed
        # refresh and aggregate data verification.
        powerbi_launch = {
            **capture("opening.png"),
            "screenshot_data_verified": True,
            "data_load": {"status": "verified", "tables_checked": 1,
                          "tables_nonempty": 1, "tables_empty": 0,
                          "tables_failed": 0, "total_rows": 5},
            "refresh": {"status": "verified"},
        }

        self.workbook_name = 'Sales <North> & "West".twb'
        self.page_name = 'Revenue <script>alert("raw")</script> & café'
        incomplete_page = "Unmapped <page> & evidence"
        row = {
            "relative_source": self.workbook_name,
            "pair_status": "paired",
            "status": "manual_review",
            "issues": ['Unchanged issue: <source> & "raw"'],
            "source_inventory": [{
                "id": "tableau-page", "name": self.page_name,
                "kind": "dashboard", "hidden": False,
                "capture": capture("source.png"),
            }, {
                "id": "tableau-page-incomplete", "name": incomplete_page,
                "kind": "worksheet", "hidden": False,
                "capture": missing_capture(),
            }],
            "target_inventory": [{
                "id": "powerbi-page", "name": 'PBI <target> & café',
                "kind": "page", "hidden": False, "tooltip": False,
                "capture": capture("target.png"),
            }, {
                "id": "powerbi-page-incomplete", "name": "Unverified target",
                "kind": "page", "hidden": False, "tooltip": False,
                "capture": missing_capture(),
            }],
            "page_mappings": [{
                "source_id": "tableau-page", "target_id": "powerbi-page",
                "status": "mapped", "candidate_source_ids": [],
            }, {
                "source_id": "tableau-page-incomplete",
                "target_id": "powerbi-page-incomplete",
                "status": "mapped", "candidate_source_ids": [],
            }],
            "tableau": capture("opening.png"),
            "powerbi": powerbi_launch,
        }
        self.manifest = {
            "all_pages": True,
            "schema_version": 3,
            "status": "captured",
            "created_at": "2026-10-15T12:00:00Z",
            "initial_captures_counted": False,
            "parity_score": None,
            "workbooks": [row],
        }
        self.manifest["totals"] = _page_totals(self.manifest["workbooks"])

    def document(self, filename):
        return _Document((self.run_dir / filename).read_text(encoding="utf-8")).root

    def test_main_and_per_page_reports_localize_ui_but_preserve_evidence(self):
        write_comparison_reports(self.manifest, self.run_dir)
        main = self.document("comparison.html")
        page = self.document("page-00001.html")

        for rendered in (main, page):
            html_node, = list(rendered.descendants("html"))
            self.assertEqual(html_node.attrs["lang"], "fr")
            language_select, = [node for node in rendered.descendants("select")
                                if node.attrs.get("id") == "report-language"]
            self.assertEqual(language_select.attrs["data-i18n-aria-label-en"],
                             "Choose report language")

        report_selector, = [node for node in main.descendants("select")
                            if node.attrs.get("id") == "report-selector"]
        self.assertEqual(report_selector.attrs["aria-label"], "Choisir un rapport")
        self.assertEqual(report_selector.attrs["data-i18n-aria-label-en"], "Choose a report")
        self.assertEqual(report_selector.attrs["data-i18n-aria-label-fr"], "Choisir un rapport")
        page_nav, = list(main.descendants("nav", "data-page-navigation"))
        self.assertEqual(page_nav.attrs["data-i18n-aria-label-en"], "Pages of report 1")
        self.assertEqual(page_nav.attrs["data-i18n-aria-label-fr"], "Pages du rapport 1")
        page_selector, = list(page_nav.descendants("select", "data-page-selector"))
        self.assertEqual(page_selector.attrs["aria-label"], "Choisir une page")
        previous, = list(page_nav.descendants("button", "data-page-previous"))
        following, = list(page_nav.descendants("button", "data-page-next"))
        self.assertEqual(previous.attrs["data-i18n-aria-label-en"], "Previous page")
        self.assertEqual(following.attrs["data-i18n-aria-label-fr"], "Page suivante")

        # Both ready and incomplete status text are authored bilingual UI. The
        # machine status remains a stable English evidence code.
        for english, french in (
                ("Screenshots ready for visual review", "Captures prêtes à comparer"),
                ("Comparison incomplete", "Comparaison incomplète")):
            localized_statuses = [
                node for node in main.descendants(attribute="data-i18n-en")
                if node.attrs["data-i18n-en"] == english
            ]
            self.assertGreaterEqual(len(localized_statuses), 1)
            self.assertTrue(all(node.attrs["data-i18n-fr"] == french
                                for node in localized_statuses))
            self.assertTrue(any(node.text() == french for node in localized_statuses))
            status_wrapper, = [node for node in main.descendants(attribute="data-status")
                               if node.attrs["data-status"] == english]
            self.assertEqual(status_wrapper.attrs["role"], "status")
        self.assertIn("reportlanguagechange", main.text())
        self.assertIn("ReportI18n.format", main.text())
        self.assertIn("Page {current} of {total}", main.text())
        self.assertIn("Page {current} sur {total}", main.text())

        page_html = (self.run_dir / "page-00001.html").read_text(encoding="utf-8")
        page_title, = list(page.descendants("title"))
        self.assertIn("Capture evidence", page_title.attrs["data-i18n-en"])
        self.assertIn(html.escape(self.workbook_name), page_html)
        self.assertIn(html.escape(self.page_name), page_html)
        self.assertIn("&lt;capture &amp; raw &quot;diagnostic&quot;&gt;", page_html)
        main_html = (self.run_dir / "comparison.html").read_text(encoding="utf-8")
        self.assertIn("&lt;source&gt; &amp; &quot;raw&quot;", main_html)
        self.assertIn("page-00002", main_html)
        saved_manifest = json.loads((self.run_dir / "manifest.json").read_text("utf-8"))
        self.assertEqual(saved_manifest, self.manifest)


class TestMigrationQualityLocalization(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        forbid_network(self)
        self.report = MigrationQualityReport(
            report_name='Sales <North> & "West"',
            assessment={"source_note": '<raw & "unchanged">'},
            parity={"parity_score": 73},
            data={"source_note": '<raw & "unchanged">',
                  "source_page": 'Revenue <script> & café'},
            interface={},
            openability={},
            status="WARN",
            warnings=['Keep exact evidence: <warning> & "detail"'],
        )
        self.quality_json = self.root / "quality.json"
        self.report.save_json(str(self.quality_json))
        self.single_path = self.root / "migration_quality_sales.html"
        self.consolidated_path = self.root / "migration_quality.html"

    def test_individual_and_consolidated_reports_localize_authored_labels_only(self):
        self.report.save_html(str(self.single_path))
        build_consolidated_quality_html(
            [str(self.quality_json)], str(self.consolidated_path))

        single_html = self.single_path.read_text(encoding="utf-8")
        consolidated_html = self.consolidated_path.read_text(encoding="utf-8")
        single = _Document(single_html).root
        consolidated = _Document(consolidated_html).root
        for rendered in (single, consolidated):
            html_node, = list(rendered.descendants("html"))
            self.assertEqual(html_node.attrs["lang"], "en")
            self.assertIsNotNone(_localized_node(rendered, "Overall status"))
            self.assertIsNotNone(_localized_node(rendered, "Openability confidence"))

        self.assertEqual(_localized_node(single, "Overall status").text(), "Overall status")
        self.assertEqual(_localized_node(consolidated, "Overall status").attrs["data-i18n-fr"],
                         "État global")
        overview_nodes = [node for node in consolidated.descendants(attribute="data-i18n-en")
                          if node.attrs["data-i18n-en"] == "Overview"]
        self.assertEqual(len(overview_nodes), 2)
        self.assertTrue(all(node.attrs["data-i18n-fr"] == "Vue d’ensemble"
                            for node in overview_nodes))
        self.assertIn("Migration quality — Sales", _Document(single_html).root.text())
        self.assertIn(html.escape(self.report.report_name), single_html)
        self.assertIn(html.escape(self.report.report_name), consolidated_html)
        for rendered_html in (single_html, consolidated_html):
            self.assertIn("&lt;raw &amp; &quot;unchanged&quot;&gt;", rendered_html)
            self.assertIn("&lt;warning&gt; &amp; &quot;detail&quot;", rendered_html)
            self.assertIn("&lt;script&gt; &amp; café", rendered_html)
            self.assertNotIn('data-i18n-en="Keep exact evidence', rendered_html)
            # Status is an evidence value and is not localized.
            self.assertIn(">WARN<", rendered_html)
        self.assertEqual(json.loads(self.quality_json.read_text("utf-8")),
                         self.report.to_dict())
