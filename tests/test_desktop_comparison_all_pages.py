"""Mocked all-page inventory, mapping, navigation and report regressions."""
import copy
import hashlib
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock
from urllib.parse import quote, unquote, urlsplit

from powerbi_import import desktop_comparison as comparison
from powerbi_import import desktop_navigation, desktop_window
from powerbi_import.tableau_desktop_probe import TableauProbeReport
from tests.pixel_comparison_fixtures import forbid_network, tiny_png

TABLEAU_XML = """<?xml version="1.0"?>
<workbook>
  <worksheets>
    <worksheet name="Embedded"/>
    <worksheet name="Solo"/>
  </worksheets>
  <dashboards>
    <dashboard name="Dashboard"><zones><zone name="Embedded"/></zones></dashboard>
  </dashboards>
  <stories><story name="Story"/></stories>
</workbook>"""


class AllPagesFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source_dir = self.root / "tableau"
        self.output_root = self.root / "converted"
        self.source_dir.mkdir()
        self.output_root.mkdir()

    def write(self, path, content):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def assert_localized_ui(self, tree, english, french):
        matches = [node for node in tree.nodes(attribute="data-i18n-en")
                   if node.attrs["data-i18n-en"] == english]
        self.assertTrue(matches, f"Missing English localization metadata for {english!r}")
        self.assertTrue(any(node.attrs.get("data-i18n-fr") == french
                            and node.text() == french for node in matches),
                        f"Missing French default text for {english!r}")
        return matches

    def tableau_file(self, xml=TABLEAU_XML, name="Sales.twb"):
        return self.write(self.source_dir / name, xml)

    def pbip_project(self, names=("Dashboard", "Solo", "Story"), hidden=(),
                     tooltip=()):
        project = self.output_root / "Sales"
        pbip = self.write(project / "Sales.pbip", json.dumps({
            "artifacts": [{"report": {"path": "Sales.Report"}}]}))
        pages = project / "Sales.Report" / "definition" / "pages"
        order = []
        for index, display_name in enumerate(names):
            page_id = f"page-{index}"
            order.append(page_id)
            page = {"name": page_id, "displayName": display_name,
                    "visibility": "hidden" if display_name in hidden else "visible"}
            if display_name in tooltip:
                page["objects"] = {"pageInformation": [{
                    "properties": {"type": {"expr": {"Literal": {"Value": "'Tooltip'"}}}}
                }]}
            self.write(pages / page_id / "page.json", json.dumps(page))
        self.write(pages / "pages.json", json.dumps({"pageOrder": order}))
        return pbip


class _ComparisonHtmlNode:
    """Small semantic HTML tree; deliberately ignores CSS and layout classes."""

    def __init__(self, tag="", attrs=(), parent=None):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children = []

    def nodes(self, tag=None, attribute=None):
        for child in self.children:
            if isinstance(child, _ComparisonHtmlNode):
                if ((tag is None or child.tag == tag)
                        and (attribute is None or attribute in child.attrs)):
                    yield child
                yield from child.nodes(tag, attribute)

    def text(self):
        return "".join(child.text() if isinstance(child, _ComparisonHtmlNode)
                       else child for child in self.children)


class _ComparisonHtmlParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                 "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.root = _ComparisonHtmlNode()
        self.current = self.root
        self.feed(document)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = _ComparisonHtmlNode(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in self.VOID_TAGS:
            self.current = node

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


class TestBusinessComparisonReport(AllPagesFixture):
    """Business-facing output contracts, without Desktop or browser launches."""

    def setUp(self):
        super().setUp()
        forbid_network(self)
        self.run_dir = self.root / "report"
        self.run_dir.mkdir()
        self.manifest = {
            "all_pages": True, "schema_version": 3, "status": "captured",
            "created_at": "2026-10-15T09:00:00Z",
            "initial_captures_counted": False, "parity_score": None,
            "workbooks": [self.workbook("Sales.twb", 2),
                          self.workbook("Finance.twb", 1)],
        }
        self.manifest["totals"] = comparison._page_totals(self.manifest["workbooks"])

    def capture(self, filename):
        path = self.run_dir / "captures" / filename
        path.parent.mkdir(exist_ok=True)
        # Distinct evidence must stay distinguishable even when presentation
        # assets are deduplicated by content.
        pixel = tuple(hashlib.sha256(filename.encode("utf-8")).digest()[:3]) + (255,)
        path.write_bytes(tiny_png((pixel,)))
        return {"status": "captured", "screenshot": str(path),
                "screenshot_blank": False, "selection": {"status": "verified"},
                "post_capture_selection": {"status": "verified"},
                "note": "Full capture evidence"}

    def workbook(self, name, count):
        sources, targets, mappings = [], [], []
        for index in range(count):
            sources.append({
                "id": f"source-{index}", "name": f"Tableau page {index + 1}",
                "kind": "dashboard", "hidden": False,
                "capture": self.capture(f"{name}-source-{index}.png")})
            targets.append({
                "id": f"target-{index}", "name": f"PBI page {index + 1}",
                "kind": "page", "hidden": False, "tooltip": False,
                "capture": self.capture(f"{name}-target-{index}.png")})
            mappings.append({
                "source_id": sources[-1]["id"], "target_id": targets[-1]["id"],
                "status": "mapped", "candidate_source_ids": []})
        return {
            "relative_source": name, "pair_status": "paired",
            "status": "manual_review", "issues": [],
            "source_inventory": sources, "target_inventory": targets,
            "page_mappings": mappings,
            "tableau": self.capture(f"{name}-initial-tableau.png"),
            "powerbi": self.capture(f"{name}-initial-powerbi.png"),
        }

    def render(self):
        comparison.write_comparison_reports(self.manifest, self.run_dir)
        return self.read_html("comparison.html")

    def read_html(self, filename):
        return _ComparisonHtmlParser(
            (self.run_dir / filename).read_text("utf-8")).root

    def by_id(self, tree, identity):
        matches = [node for node in tree.nodes() if node.attrs.get("id") == identity]
        self.assertEqual(len(matches), 1, identity)
        return matches[0]

    def details(self, tree, label):
        matches = [node for node in tree.nodes("details")
                   if any(child.tag == "summary" and child.text() == label
                          for child in node.children
                          if isinstance(child, _ComparisonHtmlNode))]
        self.assertEqual(len(matches), 1, label)
        self.assertNotIn("open", matches[0].attrs)
        return matches[0]

    def localized_details(self, tree, english, french, *, expanded=False):
        matches = []
        for details in tree.nodes("details"):
            summaries = [node for node in details.nodes("summary")]
            if any(any(node.attrs.get("data-i18n-en") == english
                       and node.attrs.get("data-i18n-fr") == french
                       for node in summary.nodes(attribute="data-i18n-en"))
                   and summary.text() == french for summary in summaries):
                matches.append(details)
        self.assertEqual(len(matches), 1, english)
        if expanded:
            self.assertIn("open", matches[0].attrs)
        else:
            self.assertNotIn("open", matches[0].attrs)
        return matches[0]

    def test_single_report_selector_and_report_scoped_page_navigation(self):
        tree = self.render()
        selector = self.by_id(tree, "report-selector")
        self.assertEqual(selector.tag, "select")
        reports = list(tree.nodes(attribute="data-comparison-report"))
        self.assertEqual([option.attrs["value"] for option in selector.nodes("option")],
                         [report.attrs["id"] for report in reports])
        self.assertEqual([option.text() for option in selector.nodes("option")],
                         ["Sales.twb", "Finance.twb"])
        page_ids = []
        for report, count in zip(reports, (2, 1)):
            pages = list(report.nodes(attribute="data-comparison-page"))
            navigation, = report.nodes("nav", "data-page-navigation")
            page_selector, = navigation.nodes("select", "data-page-selector")
            self.assertEqual(len(pages), count)
            self.assertEqual(
                [option.attrs["value"] for option in page_selector.nodes("option")],
                [page.attrs["id"] for page in pages])
            previous, = navigation.nodes("button", "data-page-previous")
            following, = navigation.nodes("button", "data-page-next")
            self.assertEqual(previous.attrs["type"], "button")
            self.assertEqual(following.attrs["type"], "button")
            position, = navigation.nodes(attribute="data-page-position")
            self.assertEqual(position.attrs["role"], "status")
            self.assertEqual(position.attrs["aria-live"], "polite")
            page_ids.extend(page.attrs["id"] for page in pages)
        self.assertEqual(len(page_ids), len(set(page_ids)))

    def test_individual_evidence_links_return_to_exact_report_page_anchor(self):
        tree = self.render()
        pages = list(tree.nodes(attribute="data-comparison-page"))
        filenames = []
        for page in pages:
            link, = [link for link in page.nodes("a")
                     if any(node.attrs.get("data-i18n-en") == "Open individual page evidence"
                            and node.attrs.get("data-i18n-fr") == "Ouvrir les preuves de cette page"
                            for node in link.nodes(attribute="data-i18n-en"))]
            self.assert_localized_ui(
                link, "Open individual page evidence", "Ouvrir les preuves de cette page")
            filenames.append(link.attrs["href"])
            individual = self.read_html(link.attrs["href"])
            back, = [link for link in individual.nodes("a")
                     if any(node.attrs.get("data-i18n-en") == "Back to report and page navigation"
                            and node.attrs.get("data-i18n-fr")
                            == "Retour à la navigation des rapports et pages"
                            for node in link.nodes(attribute="data-i18n-en"))]
            self.assert_localized_ui(
                back, "Back to report and page navigation",
                "Retour à la navigation des rapports et pages")
            self.assertEqual(back.attrs["href"], f'comparison.html#{page.attrs["id"]}')
            self.assertEqual([image.attrs for image in individual.nodes("img")],
                             [image.attrs for image in page.nodes("img")])
        self.assertEqual(set(filenames),
                         {path.name for path in self.run_dir.glob("page-*.html")})

    def test_paired_page_images_are_tableau_first_powerbi_second_and_clickable(self):
        tree = self.render()
        for page in tree.nodes(attribute="data-comparison-page"):
            images = list(page.nodes("img"))
            self.assertEqual(len(images), 2)
            self.assertTrue(images[0].attrs["alt"].startswith("Tableau Tableau page "))
            self.assertTrue(images[1].attrs["alt"].startswith("Power BI PBI page "))
            for image in images:
                self.assertEqual(image.parent.tag, "a")
                self.assertEqual(image.parent.attrs["href"], image.attrs["src"])
                self.assertTrue((self.run_dir / image.attrs["src"]).is_file())
            self.assert_localized_ui(
                page, "Screenshots ready for visual review", "Captures prêtes à comparer")
            self.assert_localized_ui(
                page, "Verified captures; business validation still required.",
                "Captures vérifiées ; validation métier encore nécessaire.")

    def test_ready_requires_verified_existing_nonblank_captures_on_both_sides(self):
        original = copy.deepcopy(self.manifest)
        for inventory in ("source_inventory", "target_inventory"):
            for change in (
                    {"status": "error"}, {"screenshot": None},
                    {"screenshot": str(self.run_dir / "missing.png")},
                    {"screenshot_blank": True}, {"screenshot_blank": None},
                    {"selection": None}, {"selection": {"status": "unverified"}},
                    {"post_capture_selection": {"status": "unverified"}}):
                with self.subTest(inventory=inventory, change=change):
                    self.manifest = copy.deepcopy(original)
                    self.manifest["workbooks"][0][inventory][0]["capture"].update(change)
                    page = next(self.render().nodes(attribute="data-comparison-page"))
                    self.assert_localized_ui(
                        page, "Comparison incomplete", "Comparaison incomplète")
                    self.assert_localized_ui(
                        page, "A mapping or verified capture is missing, without substitution.",
                        "Une correspondance ou une capture vérifiée manque, sans substitution.")
                    self.assertFalse(any(node.attrs.get("data-i18n-en")
                                         == "Screenshots ready for visual review"
                                         for node in page.nodes(attribute="data-i18n-en")))
                    self.assert_localized_ui(
                        page, "Opening captures are not used in its place.",
                        "Les captures initiales ne remplacent pas les captures manquantes.")

    def test_ready_requires_workbook_pair_and_page_correspondence(self):
        original = copy.deepcopy(self.manifest)
        for field, value in (("pair_status", "ambiguous"), ("status", "ambiguous"),
                             ("source_id", None), ("target_id", None)):
            with self.subTest(field=field):
                self.manifest = copy.deepcopy(original)
                row = self.manifest["workbooks"][0]
                (row if field == "pair_status" else row["page_mappings"][0])[field] = value
                page = next(self.render().nodes(attribute="data-comparison-page"))
                self.assert_localized_ui(
                    page, "Comparison incomplete", "Comparaison incomplète")
                self.assertFalse(any(node.attrs.get("data-i18n-en")
                                     == "Screenshots ready for visual review"
                                     for node in page.nodes(attribute="data-i18n-en")))
                if field.endswith("_id"):
                    self.assert_localized_ui(
                        page, "No verified page mapping.",
                        "Aucune correspondance de page vérifiée.")
                    self.assertEqual(len(list(page.nodes("img"))), 1)

    def test_initial_images_are_expanded_separately_and_never_fill_missing_pages(self):
        row = self.manifest["workbooks"][0]
        row["source_inventory"][0]["capture"].update(status="inaccessible", screenshot=None)
        tree = self.render()
        reports = list(tree.nodes(attribute="data-comparison-report"))
        for report in reports:
            initial = self.localized_details(
                report, "Initial opening captures — page identity unverified",
                "Captures initiales — identité de page non vérifiée", expanded=True)
            self.assertIn("data-opening-captures", initial.attrs)
            # Opening evidence must precede page evidence without another collapsed
            # disclosure (or hidden ancestor) preventing its default visibility.
            descendants = list(report.nodes())
            self.assertLess(descendants.index(initial), descendants.index(
                next(report.nodes("nav", "data-page-navigation"))))
            self.assertLess(descendants.index(initial), descendants.index(
                next(report.nodes(attribute="data-comparison-page"))))
            ancestor = initial
            while ancestor is not report:
                self.assertNotIn("hidden", ancestor.attrs)
                if ancestor.tag == "details":
                    self.assertIn("open", ancestor.attrs)
                ancestor = ancestor.parent
            images = list(initial.nodes("img"))
            self.assertEqual(len(images), 2)
            for image in images:
                self.assertIn("identité de page non vérifiée", image.attrs["alt"])
            self.assert_localized_ui(
                initial,
                "These are initial/opening document-window captures, not verified page mappings. "
                "They are not included in page totals or page pixel comparisons and do not establish parity.",
                "Ces captures initiales de fenêtres de document ne sont pas des correspondances de pages vérifiées. "
                "Elles sont exclues des totaux de pages et des comparaisons pixel par page, et ne démontrent pas la parité.")
            for page in report.nodes(attribute="data-comparison-page"):
                self.assertFalse(list(page.nodes(attribute="data-opening-captures")))
                for image in page.nodes("img"):
                    self.assertNotIn("identité de page non vérifiée", image.attrs["alt"])
        page = next(reports[0].nodes(attribute="data-comparison-page"))
        self.assertEqual(len(list(page.nodes("img"))), 1)
        self.assertIn("No capture: inaccessible", page.text())
        self.assertNotIn("initial opening capture", self.read_html("page-00001.html").text())

    def test_opening_images_require_captured_status_and_existing_original_file(self):
        original = copy.deepcopy(self.manifest)
        for field in ("tableau", "powerbi"):
            for change in (
                    {"status": "error"}, {"status": "unavailable"},
                    {"status": "not_requested"}, {"screenshot": None},
                    {"screenshot": str(self.run_dir / "absent opening.png")},
                    {"screenshot": str(self.run_dir)}):
                with self.subTest(field=field, change=change):
                    self.manifest = copy.deepcopy(original)
                    # Render once first: a cached image must not establish evidence
                    # when its recorded capture subsequently becomes unavailable.
                    self.render()
                    self.manifest["workbooks"][0][field].update(change)
                    before = copy.deepcopy(self.manifest)
                    tree = self.render()
                    report = next(tree.nodes(attribute="data-comparison-report"))
                    opening, = report.nodes("details", "data-opening-captures")
                    self.assertIn("open", opening.attrs)
                    self.assertEqual(len(list(opening.nodes("img"))), 1)
                    self.assertEqual(len(list(opening.nodes(
                        attribute="data-capture-status"))), 1)
                    self.assertEqual(self.manifest, before)

    def test_identical_opening_and_page_bytes_share_assets_not_page_identity(self):
        row = self.manifest["workbooks"][0]
        captures = [row["tableau"], row["powerbi"],
                    row["source_inventory"][0]["capture"],
                    row["target_inventory"][0]["capture"]]
        payload = tiny_png()
        for capture in captures:
            Path(capture["screenshot"]).write_bytes(payload)
        self.assertEqual(len({capture["screenshot"] for capture in captures}), 4)
        before = copy.deepcopy(self.manifest)
        url = f"assets/{hashlib.sha256(payload).hexdigest()}.png"
        documents = []
        for _ in range(2):
            tree = self.render()
            documents.append((self.run_dir / "comparison.html").read_bytes())
            report = next(tree.nodes(attribute="data-comparison-report"))
            opening, = report.nodes("details", "data-opening-captures")
            page = next(report.nodes(attribute="data-comparison-page"))
            individual = self.read_html("page-00001.html")
            for scope in (opening, page, individual):
                self.assertEqual([image.attrs["src"] for image in scope.nodes("img")],
                                 [url, url])
            for image in opening.nodes("img"):
                self.assertIn("identité de page non vérifiée", image.attrs["alt"])
            for scope in (page, individual):
                self.assertFalse(list(scope.nodes(attribute="data-opening-captures")))
                self.assertEqual([image.attrs["alt"] for image in scope.nodes("img")],
                                 ["Tableau Tableau page 1", "Power BI PBI page 1"])
            self.assertEqual(self.manifest, before)
        self.assertEqual(documents[0], documents[1])
        self.assertEqual((self.run_dir / url).read_bytes(), payload)
        self.assertEqual(len(list((self.run_dir / "assets").glob(
            f"{hashlib.sha256(payload).hexdigest()}*"))), 1)
        for capture in captures:
            self.assertEqual(Path(capture["screenshot"]).read_bytes(), payload)
        self.assertEqual(json.loads((self.run_dir / "manifest.json").read_text("utf-8")),
                         before)

    def test_cached_asset_does_not_hide_deleted_original_capture(self):
        row = self.manifest["workbooks"][0]
        # Cover both opening and per-page evidence, after successful cache creation.
        for capture in (row["tableau"], row["source_inventory"][0]["capture"]):
            path = Path(capture["screenshot"])
            payload = path.read_bytes()
            self.render()
            asset = self.run_dir / "assets" / f"{hashlib.sha256(payload).hexdigest()}.png"
            self.assertEqual(asset.read_bytes(), payload)
            path.unlink()
        before = copy.deepcopy(self.manifest)
        tree = self.render()
        report = next(tree.nodes(attribute="data-comparison-report"))
        opening, = report.nodes("details", "data-opening-captures")
        page = next(report.nodes(attribute="data-comparison-page"))
        for scope in (opening, page, self.read_html("page-00001.html")):
            self.assertEqual(len(list(scope.nodes("img"))), 1)
            missing, = scope.nodes(attribute="data-capture-status")
            self.assertEqual(missing.attrs["data-capture-status"], "captured")
            self.assertIn("screenshot file unavailable",
                          missing.attrs["data-capture-diagnostic"])
        self.assert_localized_ui(page, "Comparison incomplete", "Comparaison incomplète")
        self.assertEqual(self.manifest, before)
        for capture in (row["tableau"], row["source_inventory"][0]["capture"]):
            self.assertFalse(Path(capture["screenshot"]).exists())
        self.assertEqual(json.loads((self.run_dir / "manifest.json").read_text("utf-8")),
                         before)

    def test_opening_visibility_does_not_promote_launches_into_page_totals(self):
        row = self.manifest["workbooks"][0]
        for inventory in ("source_inventory", "target_inventory"):
            for page in row[inventory]:
                page["capture"].update(status="inaccessible", screenshot=None)
        self.manifest["totals"] = comparison._page_totals(self.manifest["workbooks"])
        before = copy.deepcopy(self.manifest)
        tree = self.render()
        self.assertEqual(self.manifest, before)
        self.assertEqual(json.loads((self.run_dir / "manifest.json").read_text("utf-8")),
                         before)
        self.assertFalse(self.manifest["initial_captures_counted"])
        for side in ("source", "target"):
            self.assertEqual(self.manifest["totals"][side]["captured"], 1)
            self.assertEqual(self.manifest["totals"][side]["inventory"], 3)
        self.assert_localized_ui(
            tree,
            "Page capture status: Tableau 1/3 with status “captured”; Power BI 1/3 "
            "with status “captured”. Initial captures are excluded from page totals.",
            "État de capture des pages : Tableau 1/3 au statut « captured » ; Power BI "
            "1/3 au statut « captured ». Les captures initiales sont exclues des totaux de pages.")
        report = next(tree.nodes(attribute="data-comparison-report"))
        initial, = report.nodes("details", "data-opening-captures")
        self.assertIn("open", initial.attrs)
        self.assertEqual(len(list(initial.nodes("img"))), 2)
        for page in report.nodes(attribute="data-comparison-page"):
            self.assertEqual(list(page.nodes("img")), [])
            self.assert_localized_ui(page, "Comparison incomplete", "Comparaison incomplète")

    def test_existing_unicode_capture_paths_use_byte_identical_hashed_assets(self):
        row = self.manifest["workbooks"][0]
        captures = [
            row["tableau"], row["powerbi"],
            row["source_inventory"][0]["capture"],
            row["target_inventory"][0]["capture"],
        ]
        paths = []
        for index, capture in enumerate(captures):
            replacement = self.capture(f"été 東京 {index} #100% & capture.png")
            capture.update(replacement)
            paths.append(Path(replacement["screenshot"]))
        before = copy.deepcopy(self.manifest)
        tree = self.render()
        report = next(tree.nodes(attribute="data-comparison-report"))
        initial, = report.nodes("details", "data-opening-captures")
        page = next(report.nodes(attribute="data-comparison-page"))
        individual = self.read_html("page-00001.html")
        for scope, expected in ((initial, paths[:2]), (page, paths[2:]),
                                (individual, paths[2:])):
            images = list(scope.nodes("img"))
            self.assertEqual(len(images), 2)
            for image, path in zip(images, expected):
                payload = path.read_bytes()
                relative = f"assets/{hashlib.sha256(payload).hexdigest()}.png"
                url = image.attrs["src"]
                self.assertEqual(url, quote(relative, safe="/."))
                self.assertEqual(unquote(url), relative)
                self.assertEqual(urlsplit(url).fragment, "")
                self.assertTrue((self.run_dir / unquote(url)).is_file())
                self.assertEqual((self.run_dir / unquote(url)).read_bytes(), payload)
                self.assertEqual(image.parent.tag, "a")
                self.assertEqual(image.parent.attrs["href"], url)
                self.assertTrue(any(
                    link.attrs.get("href") == quote(relative, safe="/.")
                    and link.text().strip() and not list(link.nodes("img"))
                    for link in scope.nodes("a")),
                    f"Missing presentation-copy link for {relative}")
        self.assertEqual(self.manifest, before)

    def test_technical_diagnostics_are_collapsed_without_losing_evidence(self):
        self.manifest["screen_comparison"] = "local"
        row = self.manifest["workbooks"][0]
        row["issues"] = ["diagnostic one", "diagnostic two", "FULL ISSUE THREE"]
        mapping = row["page_mappings"][0]
        mapping["pixel_comparison"] = comparison.compare_png_files(
            row["source_inventory"][0]["capture"]["screenshot"],
            row["target_inventory"][0]["capture"]["screenshot"])
        row["page_pixel_comparison_summary"] = {
            "status": "compared", "counts": {"compared": 1, "not_comparable": 0, "error": 0},
            "note": "Full local summary"}
        tree = self.render()
        self.localized_details(tree, "Detailed coverage totals", "Totaux détaillés de couverture")
        self.localized_details(tree, "Scope and technical limitations",
                               "Périmètre et limites techniques")
        self.localized_details(tree, "Capture limitations", "Limites des captures")
        report = next(tree.nodes(attribute="data-comparison-report"))
        diagnostics = self.localized_details(
            report, "Technical diagnostics and inventories",
            "Diagnostics techniques et inventaires")
        self.assert_localized_ui(diagnostics, "Inventory", "Inventaire")
        self.assertIn("Tableau", diagnostics.text())
        self.assertIn("Power BI", diagnostics.text())
        self.assertIn("Full local summary", diagnostics.text())
        evidence = self.localized_details(
            report, "Full workbook evidence", "Preuves complètes du classeur")
        self.assertIn("FULL ISSUE THREE", evidence.text())
        for document in (next(report.nodes(attribute="data-comparison-page")),
                         self.read_html("page-00001.html")):
            pixels = self.localized_details(
                document, "Technical pixel diagnostics (non-semantic)",
                "Diagnostics techniques des pixels (non sémantiques)")
            self.assert_localized_ui(
                pixels, "Different RGBA pixels", "Pixels RGBA différents")
            self.localized_details(document, "Mapping details", "Détails de correspondance")
            for details in document.nodes("details"):
                self.assertNotIn("open", details.attrs)

    def test_no_javascript_keeps_every_report_page_and_evidence_link_visible(self):
        tree = self.render()
        fallback, = tree.nodes("noscript")
        self.assertIn("all reports and pages are shown below", fallback.text())
        self.assertIn("Individual page evidence links remain available", fallback.text())
        for node in list(tree.nodes(attribute="data-comparison-report")) + list(
                tree.nodes(attribute="data-comparison-page")):
            ancestor = node
            while ancestor.parent is not None:
                self.assertNotIn("hidden", ancestor.attrs)
                self.assertNotIn("display:none",
                                 ancestor.attrs.get("style", "").replace(" ", ""))
                ancestor = ancestor.parent
            self.assertTrue(list(node.nodes("a")))
        self.assertIn("hidden", self.by_id(tree, "report-navigation").attrs)
        for navigation in tree.nodes(attribute="data-page-navigation"):
            self.assertIn("hidden", navigation.attrs)

    def test_empty_workbook_list_and_reports_without_page_mappings_render_safely(self):
        self.manifest["workbooks"] = []
        self.manifest["totals"] = comparison._page_totals([])
        tree = self.render()
        self.assert_localized_ui(
            tree, "0 workbooks; 0/0 initial captures recorded.",
            "0 classeurs ; 0/0 captures initiales enregistrées.")
        self.assertFalse(list(self.by_id(tree, "report-selector").nodes("option")))
        self.assertFalse(list(tree.nodes(attribute="data-comparison-report")))
        self.assertFalse(list(tree.nodes(attribute="data-comparison-page")))
        self.assertFalse(list(self.run_dir.glob("page-*.html")))
        self.manifest["workbooks"] = [self.workbook("Opening only.twb", 0)]
        tree = self.render()
        report, = tree.nodes(attribute="data-comparison-report")
        self.assert_localized_ui(
            report, "No page correspondences recorded: see diagnostics.",
            "Aucune correspondance de page : consultez les diagnostics.")
        self.assertFalse(list(report.nodes(attribute="data-page-navigation")))
        self.localized_details(
            report, "Initial opening captures — page identity unverified",
            "Captures initiales — identité de page non vérifiée", expanded=True)
        self.localized_details(
            report, "Technical diagnostics and inventories",
            "Diagnostics techniques et inventaires")

    def test_selector_labels_escape_user_text_and_rendering_preserves_manifest(self):
        hostile = '<img src=x onerror="alert(1)"> & café'
        row = self.manifest["workbooks"][0]
        row["relative_source"] = hostile
        row["source_inventory"][0]["name"] = hostile
        row["target_inventory"][0]["name"] = hostile
        before = copy.deepcopy(self.manifest)
        tree = self.render()
        self.assertEqual(self.manifest, before)
        self.assertEqual(json.loads((self.run_dir / "manifest.json").read_text("utf-8")),
                         before)
        selector = self.by_id(tree, "report-selector")
        self.assertEqual(next(selector.nodes("option")).text(), hostile)
        page_selector = next(tree.nodes("select", "data-page-selector"))
        self.assertIn(hostile, next(page_selector.nodes("option")).text())
        page = next(tree.nodes(attribute="data-comparison-page"))
        self.assertEqual([image.attrs["alt"] for image in page.nodes("img")],
                         [f"Tableau {hostile}", f"Power BI {hostile}"])
        for image in tree.nodes("img"):
            self.assertNotIn("onerror", image.attrs)
            self.assertNotEqual(image.attrs["src"], "x")


class TestLocalAllPagePixels(AllPagesFixture):
    """Only verified page correspondences count, not successful launch images."""

    def setUp(self):
        super().setUp()
        self.tableau_file()
        self.pbip_project()
        forbid_network(self)
        self.page_overrides = {}
        self.page_images = {}
        self.enterContext(mock.patch.object(
            comparison, "probe_tableau_open", side_effect=self.tableau_launch))
        self.enterContext(mock.patch.object(
            comparison, "_pbi_capture", side_effect=self.powerbi_launch))
        self.tabs = self.enterContext(mock.patch.object(
            comparison, "capture_document_tab", side_effect=self.page_capture))
        self.enterContext(mock.patch.object(
            comparison.desktop_window, "windows_for_pid", side_effect=self.windows))
        for probe in (comparison.desktop_probe, comparison.tableau_desktop_probe):
            self.enterContext(mock.patch.object(
                probe, "close_probe_process", return_value={"status": "closed"}))
        self.enterContext(mock.patch.object(
            comparison.desktop_probe.subprocess, "Popen",
            side_effect=AssertionError("Tests must not launch Desktop")))

    def tableau_launch(self, source, *, screenshot_path, **kwargs):
        # Deliberately invalid launch images must never enter page metrics.
        Path(screenshot_path).write_bytes(b"not a PNG launch capture")
        return TableauProbeReport(
            source, status="captured", screenshot=screenshot_path,
            screenshot_blank=False, pid=111)

    def powerbi_launch(self, pbip, shot, timeout, desktop_path):
        shot.write_bytes(b"another invalid launch image")
        return {"status": "captured", "screenshot": str(shot),
                "screenshot_blank": False, "pid": 222, "note": ""}

    @staticmethod
    def windows(pid):
        title = "Sales - Tableau" if pid == 111 else "Sales - Power BI Desktop"
        return [desktop_window.WindowInfo(pid, title, "Desktop", 1200, 800, False)]

    def page_capture(self, hwnd, pid, name, path, *, timeout):
        Path(path).write_bytes(self.page_images.get((pid, name), tiny_png()))
        return dict({
            "status": "captured", "screenshot": path, "screenshot_blank": False,
            "selection": {"status": "verified"},
            "post_capture_selection": {"status": "verified"}, "note": "verified",
        }, **self.page_overrides.get((pid, name), {}))

    def run_comparison(self, **kwargs):
        return comparison.compare_desktop_reports(
            str(self.source_dir), str(self.output_root), all_pages=True, **kwargs)

    def test_all_page_metrics_and_individual_html_exclude_launch_images(self):
        self.page_images[(222, "Solo")] = tiny_png(
            ((10, 20, 30, 0, 40, 50, 60, 255),))
        with mock.patch.object(comparison, "compare_png_files",
                               wraps=comparison.compare_png_files) as decode:
            manifest = self.run_comparison(screen_comparison="local")
        self.assertEqual(manifest["status"], "captured")
        self.assertEqual(manifest["schema_version"], 3)
        self.assertFalse(manifest["initial_captures_counted"])
        self.assertIsNone(manifest["parity_score"])
        row, = manifest["workbooks"]
        self.assertNotIn("pixel_comparison", row)
        summary = row["page_pixel_comparison_summary"]
        self.assertFalse(summary["initial_captures_compared"])
        self.assertEqual(summary["scope"], "page_correspondences_only")
        self.assertEqual(summary["status"], "compared")
        self.assertEqual(summary["counts"], {"compared": 3, "not_comparable": 0, "error": 0})
        self.assertEqual(self.tabs.call_count, 6)
        source_pages = {page["id"]: page for page in row["source_inventory"]}
        target_pages = {page["id"]: page for page in row["target_inventory"]}
        compared_paths = [tuple(map(str, call.args[:2]))
                          for call in decode.call_args_list]
        expected_paths = [
            (str(source_pages[mapping["source_id"]]["capture"]["screenshot"]),
             str(target_pages[mapping["target_id"]]["capture"]["screenshot"]))
            for mapping in row["page_mappings"]
        ]
        self.assertCountEqual(compared_paths, expected_paths)
        opening_paths = {row[field]["screenshot"] for field in ("tableau", "powerbi")}
        self.assertTrue(opening_paths.isdisjoint(
            path for pair in compared_paths for path in pair))
        self.assertEqual(manifest["totals"]["source"]["captured"], 3)
        self.assertEqual(manifest["totals"]["target"]["captured"], 3)
        metrics = [m["pixel_comparison"]["metrics"] for m in row["page_mappings"]]
        self.assertEqual(sorted(m["absolute_channel_difference_sum"] for m in metrics),
                         [0, 0, 255])
        run_dir = Path(manifest["run_dir"])
        self.assertEqual(json.loads((run_dir / "manifest.json").read_text("utf-8")), manifest)
        pages = sorted(run_dir.glob("page-*.html"))
        self.assertEqual(len(pages), 3)
        for path in [run_dir / "comparison.html", *pages]:
            document = path.read_text("utf-8")
            for text in ("Local pixel comparison (non-semantic)", "Different RGBA pixels",
                         "RGBA channels are compared literally", "Initial launch captures are excluded"):
                self.assertIn(text, document)
        self.assertIn("Local page pixel diagnostics",
                      (run_dir / "comparison.html").read_text("utf-8"))
        tree = _ComparisonHtmlParser(
            (run_dir / "comparison.html").read_text("utf-8")).root
        opening, = tree.nodes("details", "data-opening-captures")
        self.assertIn("open", opening.attrs)
        self.assertEqual(len(list(opening.nodes("img"))), 2)

    def test_unavailable_and_corrupt_pages_are_not_zero_metrics_or_launch_fallbacks(self):
        self.page_overrides[(111, "Dashboard")] = {"status": "unavailable"}
        self.page_images[(222, "Solo")] = tiny_png()[:-1]
        with mock.patch.object(comparison, "compare_png_files",
                               wraps=comparison.compare_png_files) as decode:
            manifest = self.run_comparison(screen_comparison="local")
        self.assertEqual(decode.call_count, 2)
        self.assertEqual(manifest["status"], "failed")
        row, = manifest["workbooks"]
        self.assertEqual(row["tableau"]["status"], "captured")
        self.assertEqual(row["powerbi"]["status"], "captured")
        self.assertEqual(row["page_pixel_comparison_summary"]["status"], "partial")
        self.assertEqual(row["page_pixel_comparison_summary"]["counts"],
                         {"compared": 1, "not_comparable": 1, "error": 1})
        results = {m["pixel_comparison"]["status"]: m["pixel_comparison"]
                   for m in row["page_mappings"]}
        self.assertEqual(results["not_comparable"]["reason_code"], "capture_unavailable")
        self.assertEqual(results["error"]["reason_code"], "corrupt_png")
        self.assertIsNone(results["not_comparable"]["metrics"])
        self.assertIsNone(results["error"]["metrics"])
        self.assertEqual(manifest["totals"]["source"]["captured"], 2)
        document = (Path(manifest["run_dir"]) / "comparison.html").read_text("utf-8")
        self.assertIn("No capture: unavailable", document)
        self.assertNotIn('alt="Tableau Dashboard"', document)

    def test_page_dimension_mismatch_does_not_change_capture_coverage(self):
        self.page_images[(222, "Story")] = tiny_png(((10, 20, 30, 255),))
        manifest = self.run_comparison(screen_comparison="local")
        self.assertEqual(manifest["status"], "captured")
        row, = manifest["workbooks"]
        self.assertEqual(row["page_pixel_comparison_summary"]["counts"],
                         {"compared": 2, "not_comparable": 1, "error": 0})
        result, = [m["pixel_comparison"] for m in row["page_mappings"]
                   if m["pixel_comparison"]["status"] == "not_comparable"]
        self.assertEqual(result["reason_code"], "dimension_mismatch")
        self.assertIsNone(result["metrics"])

    def test_all_pages_without_verified_selection_are_not_comparable(self):
        for pid in (111, 222):
            for name in ("Dashboard", "Solo", "Story"):
                self.page_overrides[(pid, name)] = {"post_capture_selection": None}
        with mock.patch.object(comparison, "compare_png_files") as decode:
            manifest = self.run_comparison(screen_comparison="local")
            decode.assert_not_called()
        self.assertEqual(manifest["status"], "captured")
        row, = manifest["workbooks"]
        self.assertEqual(row["page_pixel_comparison_summary"]["status"], "not_comparable")
        self.assertEqual(row["page_pixel_comparison_summary"]["counts"],
                         {"compared": 0, "not_comparable": 3, "error": 0})
        for mapping in row["page_mappings"]:
            self.assertEqual(mapping["pixel_comparison"]["reason_code"], "capture_unverified")
            self.assertIsNone(mapping["pixel_comparison"]["metrics"])

    def test_unmatched_pages_are_not_compared_despite_successful_captures(self):
        self.pbip_project(names=("Dashboard", "Solo", "Target only"))
        manifest = self.run_comparison(screen_comparison="local")
        self.assertEqual(manifest["status"], "failed")
        row, = manifest["workbooks"]
        self.assertEqual(row["page_pixel_comparison_summary"]["counts"],
                         {"compared": 2, "not_comparable": 2, "error": 0})
        for mapping in row["page_mappings"]:
            if mapping["status"] != "mapped":
                self.assertEqual(mapping["pixel_comparison"]["reason_code"], "page_not_matched")
                self.assertIsNone(mapping["pixel_comparison"]["metrics"])

    def test_manual_all_pages_preserves_legacy_page_call_and_no_pixel_fields(self):
        with mock.patch.object(comparison, "compare_png_files") as decode, \
             mock.patch.object(comparison, "_capture_all_pages",
                               wraps=comparison._capture_all_pages) as capture:
            manifest = self.run_comparison()
            decode.assert_not_called()
        self.assertEqual(capture.call_args.kwargs, {})
        self.assertNotIn("screen_comparison", manifest)
        row, = manifest["workbooks"]
        self.assertNotIn("page_pixel_comparison_summary", row)
        self.assertTrue(all("pixel_comparison" not in m for m in row["page_mappings"]))


class TestTableauPageInventory(AllPagesFixture):
    def test_twb_inventory_distinguishes_dashboard_standalone_worksheet_and_story(self):
        path = self.tableau_file("""<workbook>
          <worksheets><worksheet name="Embedded"/><worksheet name="Solo"/>
            <worksheet name="Hidden Sheet" hidden="true"/></worksheets>
          <dashboards><dashboard name="Overview"><zones>
            <zone name="Embedded"/></zones></dashboard></dashboards>
          <stories><story name="Journey"/></stories>
          <windows><window name="Hidden Sheet" hidden="true"/></windows>
        </workbook>""")
        before = path.read_bytes()
        records = comparison.inventory_tableau_pages(str(path))
        self.assertEqual([(r["kind"], r["name"]) for r in records], [
            ("dashboard", "Overview"), ("worksheet", "Solo"),
            ("worksheet", "Hidden Sheet"), ("story", "Journey")])
        self.assertTrue(next(r for r in records if r["name"] == "Hidden Sheet")["hidden"])
        self.assertEqual(len({r["id"] for r in records}), len(records))
        self.assertEqual(path.read_bytes(), before)

    def test_packaged_workbook_is_read_without_extracting_archive_paths(self):
        archive = self.source_dir / "Sales.twbx"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("workbook/Sales.twb", TABLEAU_XML)
            package.writestr("../../outside.txt", "must not be extracted")
        records = comparison.inventory_tableau_pages(str(archive))
        self.assertEqual([r["name"] for r in records], ["Dashboard", "Solo", "Story"])
        self.assertFalse((self.root.parent / "outside.txt").exists())
        self.assertFalse((self.root / "outside.txt").exists())

    def test_malformed_or_entity_bearing_workbooks_fail_closed(self):
        for xml in ("<workbook>", '<!DOCTYPE workbook [<!ENTITY x "unsafe">]><workbook/>'):
            path = self.tableau_file(xml)
            expected = ValueError if "DOCTYPE" in xml else ET.ParseError
            with self.subTest(xml=xml), self.assertRaises(expected):
                comparison.inventory_tableau_pages(str(path))

    def test_packaged_workbook_with_ambiguous_twb_entries_is_rejected(self):
        archive = self.source_dir / "ambiguous.twbx"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("one.twb", "<workbook/>")
            package.writestr("two.twb", "<workbook/>")
        with self.assertRaisesRegex(ValueError, "ambiguous TWB"):
            comparison.inventory_tableau_pages(str(archive))

    def test_extraction_snapshot_preserves_page_kinds_and_omits_embedded_sheet(self):
        extracted = self.root / "extraction"
        self.write(extracted / "dashboards.json", json.dumps([
            {"name": "Overview", "objects": [{"type": "worksheet", "name": "Embedded"}]}]))
        self.write(extracted / "worksheets.json", json.dumps([
            {"name": "Embedded"}, {"name": "Solo", "hidden": True}]))
        self.write(extracted / "stories.json", json.dumps([{"name": "Journey"}]))
        rows = comparison.inventory_tableau_pages("unused.twb", extraction_dir=str(extracted))
        self.assertEqual([(r["kind"], r["name"]) for r in rows], [
            ("dashboard", "Overview"), ("worksheet", "Solo"), ("story", "Journey")])
        self.assertTrue(next(r for r in rows if r["name"] == "Solo")["hidden"])


class TestPowerBIPageInventory(AllPagesFixture):
    def test_includes_ordered_unordered_hidden_and_tooltip_pages(self):
        pbip = self.pbip_project(
            ("Visible", "Hidden", "Tooltip", "Unordered"),
            hidden=("Hidden",), tooltip=("Tooltip",))
        pages_json = Path(pbip).parent / "Sales.Report" / "definition" / "pages" / "pages.json"
        pages_json.write_text(json.dumps({"pageOrder": ["page-0", "page-1", "page-2"]}),
                              encoding="utf-8")
        records = comparison.inventory_powerbi_pages(str(pbip))
        self.assertEqual([r["name"] for r in records],
                         ["Visible", "Hidden", "Tooltip", "Unordered"])
        hidden = next(r for r in records if r["name"] == "Hidden")
        tooltip = next(r for r in records if r["name"] == "Tooltip")
        self.assertTrue(hidden["hidden"])
        self.assertTrue(tooltip["tooltip"])
        self.assertFalse(next(r for r in records if r["name"] == "Unordered")["ordered"])

    def test_escaping_report_reference_is_rejected(self):
        project = self.output_root / "Sales"
        pbip = self.write(project / "Sales.pbip", json.dumps({
            "artifacts": [{"report": {"path": "../outside"}}]}))
        with self.assertRaisesRegex(ValueError, "escapes project"):
            comparison.inventory_powerbi_pages(str(pbip))


class TestPageMapping(AllPagesFixture):
    def test_ambiguous_duplicate_names_and_unmatched_pages_are_not_guessed(self):
        sources = [
            {"id": "s1", "name": "Overview", "kind": "dashboard"},
            {"id": "s2", "name": "Overview", "kind": "worksheet"},
            {"id": "s3", "name": "Sales - East", "kind": "worksheet"},
            {"id": "s4", "name": "Orphan", "kind": "story"},
        ]
        targets = [
            {"id": "p1", "name": "Overview"},
            {"id": "p2", "name": "Sales"},
            {"id": "p3", "name": "Target Only"},
        ]
        rows = comparison.map_desktop_pages(sources, targets)
        by_target = {r["target_id"]: r for r in rows if r["target_id"]}
        self.assertEqual(by_target["p1"]["status"], "ambiguous")
        self.assertEqual(by_target["p1"]["candidate_source_ids"], ["s1", "s2"])
        self.assertEqual(by_target["p2"]["status"], "unmatched_target")
        self.assertEqual(by_target["p3"]["status"], "unmatched_target")
        self.assertEqual({r["source_id"] for r in rows if r["status"] == "unmatched_source"},
                         {"s3", "s4"})

    def test_explicit_lineage_maps_only_the_named_source(self):
        sources = [{"id": "s1", "name": "Old Dashboard", "kind": "dashboard"}]
        targets = [{"id": "p1", "name": "Renamed Report"}]
        result, = comparison.map_desktop_pages(
            sources, targets,
            {"dashboards": [{"tableau_dashboard": "Old Dashboard",
                             "pbi_page": "Renamed Report"}]})
        self.assertEqual(result["status"], "mapped")
        self.assertEqual(result["source_id"], "s1")
        unrelated = comparison.map_desktop_pages(
            sources, [{"id": "p2", "name": "Renamed Report 2"}])[0]
        self.assertEqual(unrelated["status"], "unmatched_target")


class TestVerifiedTabCapture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.shot = self.root / "page.png"

    def test_unverified_navigation_never_captures(self):
        with mock.patch.object(desktop_navigation, "select_document_tab",
                               return_value={"status": "inaccessible", "note": "no tab"}), \
             mock.patch.object(desktop_window, "capture_window") as capture:
            result = desktop_navigation.capture_document_tab(
                10, 20, "Dashboard", str(self.shot), timeout=3)
        self.assertEqual(result["status"], "inaccessible")
        self.assertIsNone(result["screenshot"])
        capture.assert_not_called()
        self.assertFalse(self.shot.exists())

    def test_capture_requires_post_capture_selection_verification(self):
        verification = {"status": "verified", "selected": {"name": "Dashboard"}}
        select = mock.Mock(side_effect=[verification, {"status": "inaccessible"}])

        def save(_hwnd, _pid, path, _timeout):
            Path(path).write_bytes(b"fresh mocked capture")
            return {"status": "captured", "screenshot_blank": False}

        with mock.patch.object(desktop_navigation, "select_document_tab", select), \
             mock.patch.object(desktop_navigation.time, "sleep"), \
             mock.patch.object(desktop_navigation, "_bounded_capture", side_effect=save):
            result = desktop_navigation.capture_document_tab(
                10, 20, "Dashboard", str(self.shot), timeout=5)
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["screenshot"])
        self.assertEqual(select.call_count, 2)
        self.assertTrue(select.call_args_list[1].kwargs["verify_only"])

    def test_blank_capture_is_not_accepted_as_page_evidence(self):
        verified = {"status": "verified", "selected": {"name": "Dashboard"}}

        def save(_hwnd, _pid, path, _timeout):
            Path(path).write_bytes(b"blank image")
            return {"status": "error", "screenshot_blank": True, "note": "Blank capture"}

        with mock.patch.object(desktop_navigation, "select_document_tab",
                               return_value=verified), \
             mock.patch.object(desktop_navigation.time, "sleep"), \
             mock.patch.object(desktop_navigation, "_bounded_capture", side_effect=save):
            result = desktop_navigation.capture_document_tab(
                10, 20, "Dashboard", str(self.shot), timeout=5)
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["screenshot"])
        self.assertTrue(result["screenshot_blank"])


class TestAllPagesWorkflow(AllPagesFixture):
    def setUp(self):
        super().setUp()
        self.source = self.tableau_file()
        self.pbip = self.pbip_project()
        self.tableau_initial = self.root / "initial-tableau.png"
        self.powerbi_initial = self.root / "initial-powerbi.png"
        self.tableau = mock.Mock(return_value=TableauProbeReport(
            str(self.source), status="captured", screenshot=str(self.tableau_initial),
            screenshot_blank=False, pid=111, window_title="Sales - Tableau"))
        self.powerbi = mock.Mock(return_value={
            "status": "captured", "screenshot": str(self.powerbi_initial),
            "screenshot_blank": False, "pid": 222,
            "data_load": {"status": "verified"},
            "refresh": {"status": "verified"},
            "window_title": "Sales - Power BI Desktop", "note": ""})
        self.events = []
        self.enterContext(mock.patch.object(comparison, "probe_tableau_open", self.tableau))
        self.enterContext(mock.patch.object(comparison, "_pbi_capture", self.powerbi))
        self.enterContext(mock.patch.object(
            comparison.desktop_window, "windows_for_pid", side_effect=self.window_for_pid))
        self.tableau_cleanup = self.enterContext(mock.patch.object(
            comparison.tableau_desktop_probe, "close_probe_process",
            side_effect=self.cleanup_tableau))
        self.powerbi_cleanup = self.enterContext(mock.patch.object(
            comparison.desktop_probe, "close_probe_process",
            side_effect=self.cleanup_powerbi))

    def cleanup_tableau(self, pid):
        self.events.append(("cleanup", "tableau", pid))
        return {"status": "closed", "note": "mock closed"}

    def cleanup_powerbi(self, pid):
        self.events.append(("cleanup", "powerbi", pid))
        return {"status": "closed", "note": "mock closed"}

    @staticmethod
    def window_for_pid(pid):
        title = "Sales - Tableau" if pid == 111 else "Sales - Power BI Desktop"
        return [desktop_window.WindowInfo(pid, title, "Desktop", 1200, 800, False)]

    def run_comparison(self, **kwargs):
        return comparison.compare_desktop_reports(
            str(self.source_dir), str(self.output_root), **kwargs)

    def opening_only_manifest(self):
        """No page evidence is available; never navigate or launch real apps."""
        forbid_network(self)
        with mock.patch.object(comparison.desktop_window, "windows_for_pid",
                               return_value=[]), \
             mock.patch.object(comparison, "capture_document_tab") as tabs, \
             mock.patch.object(comparison, "compare_png_files") as pixels:
            manifest = self.run_comparison(all_pages=True, screen_comparison="local")
        tabs.assert_not_called()
        pixels.assert_not_called()
        return manifest

    def test_existing_initial_tableau_image_is_visible_but_not_page_evidence(self):
        self.tableau_initial.write_bytes(tiny_png())
        manifest = self.opening_only_manifest()
        row, = manifest["workbooks"]
        self.assertEqual(row["tableau"]["status"], "captured")
        self.assertFalse(self.powerbi_initial.exists())
        self.assertEqual(manifest["status"], "failed")
        self.assertIsNone(manifest["parity_score"])
        self.assertFalse(manifest["initial_captures_counted"])
        self.assertNotIn("pixel_comparison", row)
        for side, inventory in (("source", "source_inventory"),
                                ("target", "target_inventory")):
            self.assertEqual(manifest["totals"][side]["inventory"], 3)
            self.assertEqual(manifest["totals"][side]["captured"], 0)
            self.assertEqual(manifest["totals"][side]["error"], 3)
            self.assertTrue(all(page["capture"]["status"] == "inaccessible"
                                and page["capture"]["screenshot"] is None
                                for page in row[inventory]))
        summary = row["page_pixel_comparison_summary"]
        self.assertFalse(summary["initial_captures_compared"])
        self.assertEqual(summary["scope"], "page_correspondences_only")
        self.assertEqual(summary["status"], "not_comparable")
        self.assertEqual(summary["counts"],
                         {"compared": 0, "not_comparable": 3, "error": 0})
        self.assertTrue(all(mapping["pixel_comparison"]["status"] == "not_comparable"
                            and mapping["pixel_comparison"]["metrics"] is None
                            for mapping in row["page_mappings"]))

        run_dir = Path(manifest["run_dir"])
        saved = (run_dir / "manifest.json").read_bytes()
        before = copy.deepcopy(manifest)
        comparison._write_all_page_html(manifest, run_dir)
        self.assertEqual(manifest, before)
        self.assertEqual((run_dir / "manifest.json").read_bytes(), saved)
        self.assertEqual(json.loads(saved), before)
        document = (run_dir / "comparison.html").read_text("utf-8")
        self.assertEqual(document.count("<img "), 1)
        parsed_document = _ComparisonHtmlParser(document).root
        image, = list(parsed_document.nodes("img"))
        self.assertEqual(
            image.attrs["alt"],
            "Tableau — capture initiale, identité de page non vérifiée")
        self.assertEqual(
            image.attrs["data-i18n-alt-en"],
            "Tableau initial opening capture — page identity unverified")
        self.assertEqual(
            image.attrs["data-i18n-alt-fr"],
            "Tableau — capture initiale, identité de page non vérifiée")
        self.assert_localized_ui(
            parsed_document,
            "Initial opening captures — page identity unverified",
            "Captures initiales — identité de page non vérifiée")
        self.assert_localized_ui(
            parsed_document,
            "These are initial/opening document-window captures, not verified page mappings. "
            "They are not included in page totals or page pixel comparisons and do not establish parity.",
            "Ces captures initiales de fenêtres de document ne sont pas des correspondances de pages vérifiées. "
            "Elles sont exclues des totaux de pages et des comparaisons pixel par page, et ne démontrent pas la parité.")
        page_reports = list(run_dir.glob("page-*.html"))
        self.assertEqual(len(page_reports), 3)
        for report in page_reports:
            self.assertNotIn("<img ", report.read_text("utf-8"))

    def test_initial_error_missing_or_nonfile_screenshots_are_not_rendered(self):
        self.tableau_initial.write_bytes(tiny_png())
        manifest = self.opening_only_manifest()
        run_dir = Path(manifest["run_dir"])
        saved = (run_dir / "manifest.json").read_bytes()
        for status, screenshot, message in (
                ("error", self.tableau_initial, "No capture: error"),
                ("captured", self.root / "missing.png",
                 "No capture: captured (screenshot file unavailable)"),
                ("captured", self.source_dir,
                 "No capture: captured (screenshot file unavailable)"),
                ("captured", None,
                 "No capture: captured (screenshot file unavailable)")):
            with self.subTest(status=status, screenshot=screenshot):
                candidate = copy.deepcopy(manifest)
                candidate["workbooks"][0]["tableau"].update(
                    status=status, screenshot=str(screenshot) if screenshot else None)
                before = copy.deepcopy(candidate)
                comparison._write_all_page_html(candidate, run_dir)
                document = (run_dir / "comparison.html").read_text("utf-8")
                self.assertNotIn("<img ", document)
                self.assertIn(message, document)
                self.assertEqual(candidate, before)
                self.assertEqual((run_dir / "manifest.json").read_bytes(), saved)

    def test_initial_capture_links_use_hashed_assets_and_html_escaped_notes(self):
        manifest = self.opening_only_manifest()
        run_dir = Path(manifest["run_dir"])
        image = run_dir.parent / "opening captures" / "Tableau & café #1's.png"
        image.parent.mkdir()
        image.write_bytes(tiny_png())
        manifest["workbooks"][0]["tableau"].update(
            screenshot=str(image), note='<script>alert("opening & capture")</script>')
        before = copy.deepcopy(manifest)
        comparison._write_all_page_html(manifest, run_dir)
        document = (run_dir / "comparison.html").read_text("utf-8")
        payload = image.read_bytes()
        url = f"assets/{hashlib.sha256(payload).hexdigest()}.png"
        self.assertIn(f'<a href="{url}"><img width="100%" src="{url}"', document)
        self.assertEqual((run_dir / url).read_bytes(), payload)
        self.assertEqual(image.read_bytes(), payload)
        self.assertIn(
            "&lt;script&gt;alert(&quot;opening &amp; capture&quot;)&lt;/script&gt;",
            document)
        self.assertNotIn('<script>alert("opening & capture")</script>', document)
        self.assertNotIn(f'src="{image}"', document)
        self.assertNotIn(f'href="{image}"', document)
        self.assertEqual(manifest, before)

    def test_all_pages_uses_fresh_unique_paths_and_writes_explicit_page_reports(self):
        paths = []

        def capture(hwnd, pid, name, path, *, timeout):
            self.events.append(("capture", pid, name))
            paths.append(path)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(f"mock image {pid} {name}".encode())
            return {"status": "captured", "screenshot": path, "note": "verified"}

        with mock.patch.object(comparison, "capture_document_tab", side_effect=capture) as tabs:
            manifest = self.run_comparison(all_pages=True, page_timeout=6)

        self.assertEqual(manifest["status"], "captured")
        self.assertEqual(manifest["schema_version"], 3)
        self.assertTrue(manifest["initial_captures_counted"] is False)
        row, = manifest["workbooks"]
        self.assertEqual(row["status"], "manual_review")
        self.assertEqual([p["name"] for p in row["source_inventory"]],
                         ["Dashboard", "Solo", "Story"])
        self.assertEqual(len(paths), 6)
        self.assertEqual(len(set(paths)), len(paths))
        self.assertTrue(all(Path(path).is_file() for path in paths))
        self.assertTrue(all("/source/" in path.replace("\\", "/") for path in paths[:3]))
        self.assertTrue(all("/output/" in path.replace("\\", "/") for path in paths[3:]))
        self.assertTrue(all(call.kwargs["timeout"] == 6 for call in tabs.call_args_list))
        self.assertTrue(all(p["capture"]["status"] == "captured"
                            for p in row["source_inventory"] + row["target_inventory"]))
        self.assertEqual(row["tableau"]["cleanup"]["status"], "closed")
        self.assertEqual(row["powerbi"]["cleanup"]["status"], "closed")
        self.assertLess(
            max(i for i, event in enumerate(self.events) if event[0] == "capture"),
            min(i for i, event in enumerate(self.events) if event[0] == "cleanup"))
        self.assertEqual(self.events[-2:], [
            ("cleanup", "tableau", 111), ("cleanup", "powerbi", 222)])

        run_dir = Path(manifest["run_dir"])
        saved = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, manifest)
        html = (run_dir / "comparison.html").read_text(encoding="utf-8")
        self.assertIn("All-page diagnostic", html)
        self.assertIn("Dashboard", html)
        self.assertIn("Solo", html)
        self.assertIn("Story", html)
        self.assertIn("No SSIM, data parity or fidelity score", html)

    def test_page_failures_are_isolated_and_initial_success_is_not_coverage(self):
        statuses = iter(("error", "timed_out", "captured",
                         "inaccessible", "captured", "captured"))

        def capture(_hwnd, _pid, _name, path, *, timeout):
            status = next(statuses)
            screenshot = None
            if status == "captured":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_bytes(b"nonblank mocked image")
                screenshot = path
            return {"status": status, "screenshot": screenshot, "note": status}

        with mock.patch.object(comparison, "capture_document_tab", side_effect=capture) as tabs:
            manifest = self.run_comparison(all_pages=True)

        row, = manifest["workbooks"]
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(row["tableau"]["status"], "captured")
        self.assertEqual(row["powerbi"]["status"], "captured")
        self.assertEqual([p["capture"]["status"] for p in row["source_inventory"]],
                         ["error", "timed_out", "captured"])
        self.assertEqual([p["capture"]["status"] for p in row["target_inventory"]],
                         ["inaccessible", "captured", "captured"])
        self.assertEqual(tabs.call_count, 6)
        self.assertFalse(manifest["initial_captures_counted"])
        self.assertEqual(manifest["totals"]["source"]["captured"], 1)
        self.assertEqual(manifest["totals"]["target"]["captured"], 2)
        html = (Path(manifest["run_dir"]) / "comparison.html").read_text("utf-8")
        for status in ("error", "timed_out", "inaccessible", "captured"):
            self.assertIn(status, html)
        saved = json.loads((Path(manifest["run_dir"]) / "manifest.json").read_text("utf-8"))
        self.assertEqual(
            [p["capture"]["status"] for p in saved["workbooks"][0]["source_inventory"]],
            ["error", "timed_out", "captured"])

    def test_unavailable_source_window_does_not_block_target_page_captures(self):
        def capture(_hwnd, pid, _name, path, *, timeout):
            self.assertEqual(pid, 222)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"target capture")
            return {"status": "captured", "screenshot": path, "note": ""}

        def windows(pid):
            return [] if pid == 111 else self.window_for_pid(pid)

        with mock.patch.object(comparison.desktop_window, "windows_for_pid",
                               side_effect=windows), \
             mock.patch.object(comparison, "capture_document_tab", side_effect=capture) as tabs:
            manifest = self.run_comparison(all_pages=True)

        row, = manifest["workbooks"]
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(row["tableau"]["status"], "captured")
        self.assertEqual(row["powerbi"]["status"], "captured")
        self.assertTrue(all(p["capture"]["status"] == "inaccessible"
                            for p in row["source_inventory"]))
        self.assertTrue(all(p["capture"]["status"] == "captured"
                            for p in row["target_inventory"]))
        self.assertEqual(tabs.call_count, len(row["target_inventory"]))

    def test_default_remains_initial_capture_only(self):
        with mock.patch.object(comparison, "inventory_tableau_pages") as tableau_inventory, \
             mock.patch.object(comparison, "inventory_powerbi_pages") as pbi_inventory, \
             mock.patch.object(comparison, "capture_document_tab") as page_capture:
            manifest = self.run_comparison()
        self.assertEqual(manifest["status"], "captured")
        self.assertNotIn("all_pages", manifest)
        self.assertEqual(manifest["schema_version"], 2)
        tableau_inventory.assert_not_called()
        pbi_inventory.assert_not_called()
        page_capture.assert_not_called()

    def test_hidden_pages_remain_in_inventory_and_failed_exposure_is_reported(self):
        self.source.write_text("""<workbook>
          <worksheets><worksheet name="Dashboard"/><worksheet name="Secret" hidden="true"/></worksheets>
          <dashboards><dashboard name="Dashboard"><zones><zone name="Dashboard"/></zones></dashboard></dashboards>
          <stories><story name="Story"/></stories>
          <windows><window name="Secret" hidden="true"/></windows>
        </workbook>""", encoding="utf-8")
        self.pbip = self.pbip_project(("Dashboard", "Secret", "Story"), hidden=("Secret",))
        statuses = iter(("captured", "inaccessible", "captured",
                         "captured", "inaccessible", "captured"))

        def capture(_hwnd, _pid, _name, path, *, timeout):
            status = next(statuses)
            screenshot = None
            if status == "captured":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_bytes(b"mock image")
                screenshot = path
            return {"status": status, "screenshot": screenshot, "note": status}

        with mock.patch.object(comparison, "capture_document_tab", side_effect=capture):
            manifest = self.run_comparison(all_pages=True)
        row, = manifest["workbooks"]
        for inventory in (row["source_inventory"], row["target_inventory"]):
            secret, = [p for p in inventory if p["name"] == "Secret"]
            self.assertTrue(secret["hidden"])
            self.assertEqual(secret["capture"]["status"], "inaccessible")
            self.assertIn("hidden/tooltip page", secret["capture"]["note"])
        self.assertEqual(manifest["status"], "failed")
