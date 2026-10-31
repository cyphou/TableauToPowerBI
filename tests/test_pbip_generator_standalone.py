"""Synthetic regressions for dashboard + standalone worksheet report pages."""

import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from powerbi_import.pbip_generator import PowerBIProjectGenerator
from powerbi_import.thin_report_generator import ThinReportGenerator

DASHBOARD_NAMES = ["Dashboard A", "Dashboard B", "Dashboard C"]
STANDALONE_NAMES = [
    "Text Mention", "Control Mention", "Field Mention", "Tooltip Detail",
    "Hidden Label",
]


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _worksheet(name, **metadata):
    return {
        "name": name, "chart_type": "clusteredBarChart",
        "fields": [
            {"name": "Category", "shelf": "rows"},
            {"name": "Amount", "shelf": "columns", "aggregation": "sum"},
        ],
        "filters": [], "formatting": {}, "mark_encoding": {},
        **metadata,
    }


def _reference(name, x=0):
    return {
        "type": "worksheetReference", "worksheetName": name,
        "position": {"x": x, "y": 0, "w": 600, "h": 400},
    }


def _workbook(worksheets=(), dashboards=(), **metadata):
    return {
        "worksheets": list(worksheets), "dashboards": list(dashboards),
        "datasources": [{
            "name": "Synthetic Source",
            "tables": [{
                "name": "Metrics",
                "columns": [
                    {"name": "Category", "datatype": "string"},
                    {"name": "Amount", "datatype": "real", "role": "measure"},
                    {"name": "Field Mention", "datatype": "string"},
                ],
            }],
        }],
        "calculations": [], "parameters": [], "filters": [], "stories": [],
        "actions": [], "datasource_filters": [], "custom_shapes": [],
        **metadata,
    }


def _mixed_workbook():
    worksheets = [_worksheet(name) for name in (
        "Embedded View", "Shared View", "Second Embedded", "Nested View")]
    worksheets[0]["fields"].append(
        {"name": "Field Mention", "shelf": "tooltip"})
    visibility = [{}, {"hidden": False}, {"is_hidden": False},
                  {"hidden": "false"}, {"is_hidden": " FALSE "}]
    worksheets.extend(_worksheet(name, **flags)
                      for name, flags in zip(STANDALONE_NAMES, visibility))
    worksheets.extend([
        _worksheet("Hidden Tooltip", hidden=True),
        _worksheet("Hidden Drill", is_hidden=" TRUE "),
    ])
    dashboards = [
        {
            "name": DASHBOARD_NAMES[0],
            "objects": [
                _reference("Embedded View"), _reference("Shared View", 640),
                {"type": "text", "name": "Text Mention",
                 "worksheetName": "Text Mention", "text": "Text Mention",
                 "position": {"x": 0, "y": 450, "w": 600, "h": 60}},
            ],
        },
        {
            "name": DASHBOARD_NAMES[1],
            "objects": [
                _reference("Second Embedded"), _reference("Shared View", 640),
                {"type": "filter_control", "name": "Control Mention",
                 "worksheetName": "Control Mention", "field": "Field Mention",
                 "position": {"x": 0, "y": 450, "w": 300, "h": 80}},
            ],
        },
        {
            "name": DASHBOARD_NAMES[2], "objects": [],
            "zone_hierarchy": {
                "zone_type": "layout-flow", "children": [{
                    "zone_type": "layout-flow", "children": [{
                        "zone_type": "worksheet", "name": "Nested View",
                    }],
                }],
            },
        },
    ]
    return _workbook(worksheets, dashboards)


class TestStandaloneWorksheetSelection(unittest.TestCase):
    def test_only_exact_typed_flat_and_nested_references_suppress_sheets(self):
        source = _mixed_workbook()
        extra_names = ["Shared View Extra", "shared view"]
        source["worksheets"].extend(_worksheet(name) for name in extra_names)
        source["dashboards"][0]["objects"].extend([
            {"type": "worksheet", "worksheetName": extra_names[0]},
            {"type": "text", "worksheetName": extra_names[1]},
            {"type": "worksheetReference", "name": "Text Mention"},
            {"type": "worksheetReference", "worksheetName": None},
        ])
        source["dashboards"][2]["zone_hierarchy"]["children"].extend([
            {"zone_type": "text", "name": "Text Mention"},
            {"zone_type": "filter", "name": "Control Mention"},
            {"zone_type": "worksheet", "name": ""},
        ])
        original = copy.deepcopy(source)

        selected = PowerBIProjectGenerator._get_standalone_worksheets(
            source["dashboards"], source["worksheets"])

        self.assertEqual([ws["name"] for ws in selected],
                         STANDALONE_NAMES + extra_names)
        self.assertEqual(source, original)
        for ws in selected:
            self.assertTrue(any(ws is original_ws
                                for original_ws in source["worksheets"]))

    def test_hidden_and_is_hidden_require_explicit_true(self):
        cases = [
            ("Missing", {}, True),
            ("Hidden false", {"hidden": False}, True),
            ("Is hidden false", {"is_hidden": False}, True),
            ("Text false", {"hidden": " FALSE "}, True),
            ("Other text false", {"is_hidden": "false"}, True),
            ("Unknown", {"hidden": None, "is_hidden": ""}, True),
            ("Hidden true", {"hidden": True}, False),
            ("Is hidden true", {"is_hidden": True}, False),
            ("Text true", {"hidden": " TrUe "}, False),
            ("Other text true", {"is_hidden": "true"}, False),
            ("First true wins", {"hidden": True, "is_hidden": False}, False),
            ("Second true wins", {"hidden": False, "is_hidden": True}, False),
        ]
        worksheets = [_worksheet(name, **flags) for name, flags, _ in cases]
        selected = PowerBIProjectGenerator._get_standalone_worksheets(
            [{"name": "Dashboard", "objects": []}], worksheets)
        self.assertEqual([ws["name"] for ws in selected],
                         [name for name, _, visible in cases if visible])


class TestFallbackPageAppend(unittest.TestCase):
    def test_append_retries_reserved_disk_and_new_page_collisions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pages_dir = Path(temp_dir, "pages")
            reserved = ["ReportSection", "ReportSectionaaaaaa"]
            for name in ("ReportSection", "ReportSectionbbbbbb"):
                page_dir = pages_dir / name
                page_dir.mkdir(parents=True)
                (page_dir / "page.json").write_text(
                    json.dumps({"name": name, "displayName": "Reserved"}),
                    encoding="utf-8")
                (page_dir / "sentinel.txt").write_text("keep", encoding="utf-8")
            # A disk reservation can be a file, not just an indexed page folder.
            (pages_dir / "ReportSectioncccccc").write_text(
                "reserved file", encoding="utf-8")
            before = {p: p.read_bytes() for p in pages_dir.rglob("*")
                      if p.is_file()}
            source = _workbook([_worksheet("First"), _worksheet("Second")])
            generator = PowerBIProjectGenerator(output_dir=temp_dir)
            generator._build_field_mapping(source)
            # Page IDs collide with an in-memory reservation, disk directory,
            # disk file, and then the first newly appended page. Visual IDs are
            # interleaved, so the real helper still writes complete visuals.
            tokens = ["aaaaaa", "bbbbbb", "cccccc", "dddddd", "11111111",
                      "dddddd", "eeeeee", "22222222"]
            with patch("powerbi_import.pbip_generator.uuid.uuid4",
                       side_effect=[UUID(hex=token.ljust(32, "0"))
                                    for token in tokens]), \
                    redirect_stdout(io.StringIO()):
                added = generator._create_fallback_page(
                    str(pages_dir), source["worksheets"], source,
                    existing_page_names=reserved)

            self.assertEqual(added, ["ReportSectiondddddd", "ReportSectioneeeeee"])
            self.assertEqual(len(added), len(set(added)))
            self.assertTrue(set(added).isdisjoint(reserved))
            self.assertEqual(reserved, ["ReportSection", "ReportSectionaaaaaa"])
            self.assertFalse((pages_dir / "ReportSectionaaaaaa").exists())
            for path, content in before.items():
                self.assertEqual(path.read_bytes(), content)
            for name, display in zip(added, ("First", "Second")):
                page = _read_json(pages_dir / name / "page.json")
                self.assertEqual((page["name"], page["displayName"]),
                                 (name, display))
                self.assertEqual(len(list(
                    (pages_dir / name / "visuals").glob("*/visual.json"))), 1)

    def test_empty_append_is_a_noop_even_with_an_empty_reserved_list(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            pages_dir = Path(temp_dir, "pages")
            page_dir = pages_dir / "ReportSection"
            page_dir.mkdir(parents=True)
            page_file = page_dir / "page.json"
            page_file.write_text('{"displayName": "Keep"}', encoding="utf-8")
            before = page_file.read_bytes()
            generator = PowerBIProjectGenerator(output_dir=temp_dir)
            for reserved in ([], ["ReportSection"]):
                with self.subTest(reserved=reserved):
                    self.assertEqual(generator._create_fallback_page(
                        str(pages_dir), [], {},
                        existing_page_names=reserved), [])
                    self.assertEqual(page_file.read_bytes(), before)
                    self.assertEqual(list(pages_dir.iterdir()), [page_dir])


class TestMixedWorkbookReportPages(unittest.TestCase):
    def _generate(self, mode, source):
        output = self.enterContext(tempfile.TemporaryDirectory(
            prefix="standalone_pages_"))
        with redirect_stdout(io.StringIO()):
            if mode == "full":
                generator = PowerBIProjectGenerator(output_dir=output)
                report_dir = generator.create_report_structure(
                    output, "SyntheticReport", copy.deepcopy(source))
                model_name = "SyntheticReport"
            else:
                # This public path invokes _generate_report_definition_content,
                # which must not replace the thin report's shared-model header.
                generator = ThinReportGenerator("SyntheticShared", output)
                report_dir = generator.generate_thin_report(
                    "SyntheticReport", copy.deepcopy(source))
                model_name = "SyntheticShared"
                self.assertTrue(Path(output, "SyntheticReport.pbip").is_file())
        report_dir = Path(report_dir)
        self.assertTrue((report_dir / ".platform").is_file())
        self.assertEqual(
            _read_json(report_dir / "definition.pbir")["datasetReference"],
            {"byPath": {"path": f"../{model_name}.SemanticModel"}})
        return report_dir

    def _pages(self, report_dir):
        pages_dir = report_dir / "definition" / "pages"
        metadata = _read_json(pages_dir / "pages.json")
        order = metadata["pageOrder"]
        self.assertTrue(order)
        self.assertEqual(len(order), len(set(order)))
        self.assertEqual(metadata["activePageName"], order[0])
        self.assertEqual(set(order), {p.name for p in pages_dir.iterdir()
                                     if p.is_dir()})
        pages = [_read_json(pages_dir / name / "page.json") for name in order]
        self.assertEqual([page["name"] for page in pages], order)
        return pages_dir, pages

    def _visuals(self, pages_dir, name):
        visuals = []
        for path in sorted((pages_dir / name / "visuals").glob("*/visual.json")):
            visual = _read_json(path)
            self.assertEqual(visual["name"], path.parent.name)
            visuals.append(visual)
        return visuals

    def _assert_mixed_pages(self, mode):
        pages_dir, pages = self._pages(self._generate(mode, _mixed_workbook()))
        self.assertEqual([page["displayName"] for page in pages],
                         DASHBOARD_NAMES + STANDALONE_NAMES)
        self.assertEqual(pages[0]["name"], "ReportSection")
        self.assertTrue(all("pageType" not in page for page in pages))
        for page in pages[3:]:
            visuals = self._visuals(pages_dir, page["name"])
            self.assertEqual(len(visuals), 1)
            self.assertIn("query", visuals[0]["visual"])

    def test_full_report_keeps_three_dashboards_and_five_standalone_pages(self):
        self._assert_mixed_pages("full")

    def test_thin_report_keeps_three_dashboards_and_five_standalone_pages(self):
        self._assert_mixed_pages("thin")

    def test_scoped_navigation_and_story_resolve_the_actual_standalone_page_id(self):
        source = _mixed_workbook()
        target_name = "Field Mention"
        navigation_actions = [
            {"type": "sheet-navigate", "name": label, key: value,
             "source_dashboards": ["Dashboard B"]}
            for label, key, value in (
                ("By Sheet", "target_sheet", target_name),
                ("By Worksheet", "target_worksheet", target_name),
                ("By Worksheets", "target_worksheets", [target_name]),
            )
        ]
        source["actions"] = [
            {"type": "url", "name": "Public Before",
             "url": "https://example.com/before",
             "source_dashboards": ["Dashboard B"]},
            *navigation_actions,
            {"type": "url", "name": "Public After",
             "url": "https://example.com/after",
             "source_dashboards": ["Dashboard B"]},
        ]
        source["stories"] = [{
            "name": "Synthetic Story",
            "story_points": [
                {"caption": "Dashboard Start", "captured_sheet": "Dashboard A"},
                {"caption": "Standalone Detail", "captured_sheet": target_name},
            ],
        }]
        expected_labels = [
            "Public Before", "By Sheet", "By Worksheet", "By Worksheets",
            "Public After",
        ]

        for mode in ("full", "thin"):
            with self.subTest(mode=mode):
                report_dir = self._generate(mode, source)
                pages_dir, pages = self._pages(report_dir)
                self.assertEqual([page["displayName"] for page in pages],
                                 DASHBOARD_NAMES + STANDALONE_NAMES)
                self.assertEqual(pages[0]["name"], "ReportSection")
                target, = [page for page in pages
                           if page["displayName"] == target_name]
                target_id = target["name"]
                self.assertNotEqual(target_id, target_name)
                self.assertNotIn(target_id, [page["name"] for page in pages[:3]])

                scoped_buttons = []
                for index, page in enumerate(pages):
                    visuals = self._visuals(pages_dir, page["name"])
                    buttons = [v for v in visuals
                               if v["visual"]["visualType"] == "actionButton"]
                    self.assertEqual(len(buttons),
                                     len(expected_labels) if index == 1 else 0)
                    if index == 1:
                        scoped_buttons = sorted(
                            buttons, key=lambda button: button["position"]["y"])
                        non_button_count = len(visuals) - len(buttons)
                self.assertEqual(
                    [button["visual"]["objects"]["text"][0]["properties"]
                     ["text"]["expr"]["Literal"]["Value"]
                     for button in scoped_buttons],
                    [f"'{label}'" for label in expected_labels])
                for offset, button in enumerate(scoped_buttons):
                    self.assertEqual(button["position"], {
                        "x": 10, "y": 10 + offset * 50,
                        "z": (non_button_count + offset) * 1000,
                        "height": 40, "width": 200,
                        "tabOrder": (non_button_count + offset) * 1000,
                    })
                    visual = button["visual"]
                    self.assertNotIn("action", visual["objects"])
                    properties = visual["visualContainerObjects"][
                        "visualLink"][0]["properties"]
                    expected = {
                        "show": {"expr": {"Literal": {"Value": "true"}}},
                    }
                    if offset in (0, 4):
                        url_suffix = "before" if offset == 0 else "after"
                        expected.update({
                            "type": {"expr": {"Literal": {"Value": "'WebUrl'"}}},
                            "webUrl": {"expr": {"Literal": {
                                "Value": f"'https://example.com/{url_suffix}'"}}},
                        })
                    else:
                        expected.update({
                            "type": {"expr": {"Literal": {
                                "Value": "'PageNavigation'"}}},
                            "navigationSection": {"expr": {"Literal": {
                                "Value": f"'{target_id}'"}}},
                        })
                    self.assertEqual(properties, expected)

                bookmarks_dir = report_dir / "definition" / "bookmarks"
                index = _read_json(bookmarks_dir / "bookmarks.json")
                names = [item["name"] for item in index["items"]]
                self.assertEqual(len(names), 2)
                self.assertEqual(len(set(names)), 2)
                bookmarks = [
                    _read_json(bookmarks_dir / f"{name}.bookmark.json")
                    for name in names
                ]
                self.assertCountEqual(
                    [bookmark["displayName"] for bookmark in bookmarks],
                    ["Dashboard Start", "Standalone Detail"])
                expected_targets = {
                    "Dashboard Start": pages[0]["name"],
                    "Standalone Detail": target_id,
                }
                for bookmark in bookmarks:
                    section = expected_targets[bookmark["displayName"]]
                    self.assertEqual(bookmark["explorationState"], {
                        "version": "1.0", "activeSection": section,
                        "sections": {section: {"visualContainers": {}}},
                    })

    def test_all_empty_dashboards_survive_with_or_without_standalone_sheets(self):
        dashboards = [
            {"name": name, "objects": [],
             "size": {"width": 900 + index * 100, "height": 500 + index * 50}}
            for index, name in enumerate(DASHBOARD_NAMES)
        ]
        for mode in ("full", "thin"):
            for names in ([], STANDALONE_NAMES):
                with self.subTest(mode=mode, standalone=bool(names)):
                    source = _workbook([_worksheet(n) for n in names], dashboards)
                    pages_dir, pages = self._pages(self._generate(mode, source))
                    self.assertEqual([page["displayName"] for page in pages],
                                     DASHBOARD_NAMES + names)
                    for page, dashboard in zip(pages[:3], dashboards):
                        self.assertEqual(
                            (page["width"], page["height"]),
                            (dashboard["size"]["width"], dashboard["size"]["height"]))
                        self.assertEqual(self._visuals(pages_dir, page["name"]), [])

    def test_worksheet_only_and_empty_default_keep_the_original_fallback(self):
        for mode in ("full", "thin"):
            for names in (["First Sheet", "Second Sheet"], []):
                with self.subTest(mode=mode, worksheets=bool(names)):
                    source = _workbook([_worksheet(n) for n in names])
                    pages_dir, pages = self._pages(self._generate(mode, source))
                    self.assertEqual([page["displayName"] for page in pages],
                                     names or ["Tableau Migration"])
                    self.assertEqual(pages[0]["name"], "ReportSection")
                    for page in pages:
                        self.assertEqual(
                            len(self._visuals(pages_dir, page["name"])),
                            1 if names else 0)

    def test_append_preserves_auxiliary_pages_bookmarks_and_action_scope(self):
        source = _mixed_workbook()
        source["worksheets"][-2]["tooltip"] = {"viz_in_tooltip": True}
        source["dashboards"][2]["device_layouts"] = [{
            "device_type": "phone", "auto_generated": False,
            "zones": [{"name": "Nested View",
                       "position": {"x": 0, "y": 0, "w": 320, "h": 568}}],
        }]
        source["dashboards"][1]["dynamic_zone_visibility"] = [
            {"zone_name": name, "field": "Display Mode", "value": str(index)}
            for index, name in enumerate(("Shared View", "Second Embedded"))
        ]
        source["stories"] = [{
            "name": "Synthetic Story",
            "story_points": [{"caption": "Start", "captured_sheet": "Dashboard A"}],
        }]
        source["actions"] = [
            {"type": "url", "name": "Public Link",
             "url": "https://example.com/overview",
             "source_dashboards": ["Dashboard A"]},
            {"type": "filter", "name": "Detail Filter", "field": "Category",
             "target_worksheets": ["Hidden Drill", "Hidden Drill"]},
        ]
        expected_names = DASHBOARD_NAMES + STANDALONE_NAMES + [
            "Tooltip - Hidden Tooltip", "Dashboard C (Phone)",
            "Drillthrough - Hidden Drill",
        ]
        for mode in ("full", "thin"):
            with self.subTest(mode=mode):
                report_dir = self._generate(mode, source)
                pages_dir, pages = self._pages(report_dir)
                self.assertEqual([page["displayName"] for page in pages],
                                 expected_names)
                tooltip, mobile, drillthrough = pages[8:]
                self.assertEqual(tooltip["pageType"], "Tooltip")
                self.assertEqual((tooltip["width"], tooltip["height"]), (480, 320))
                self.assertEqual((mobile["width"], mobile["height"]), (320, 568))
                self.assertEqual(drillthrough["pageType"], "Drillthrough")
                column = drillthrough["drillthrough"]["filters"][0]["field"]["Column"]
                self.assertEqual(column, {
                    "Expression": {"SourceRef": {"Entity": "Metrics"}},
                    "Property": "Category",
                })
                for index, page in enumerate(pages):
                    visuals = self._visuals(pages_dir, page["name"])
                    buttons = [v for v in visuals
                               if v["visual"]["visualType"] == "actionButton"]
                    self.assertEqual(len(buttons), 1 if index == 0 else 0)
                    if index >= 3:
                        self.assertEqual(len(visuals), 1)
                action = self._visuals(pages_dir, pages[0]["name"])
                button, = [v for v in action
                           if v["visual"]["visualType"] == "actionButton"]
                self.assertNotIn("action", button["visual"]["objects"])
                self.assertEqual(
                    button["visual"]["visualContainerObjects"]["visualLink"],
                    [{"properties": {
                        "type": {"expr": {"Literal": {"Value": "'WebUrl'"}}},
                        "webUrl": {"expr": {"Literal": {
                            "Value": "'https://example.com/overview'"}}},
                        "show": {"expr": {"Literal": {"Value": "true"}}},
                    }}])

                bookmarks_dir = report_dir / "definition" / "bookmarks"
                index = _read_json(bookmarks_dir / "bookmarks.json")
                indexed = [item["name"] for item in index["items"]]
                self.assertEqual(len(indexed), 3)
                self.assertEqual(len(set(indexed)), 3)
                self.assertEqual(
                    {p.name for p in bookmarks_dir.glob("*.bookmark.json")},
                    {f"{name}.bookmark.json" for name in indexed})
                expected_targets = {
                    "Start": pages[0]["name"],
                    "Show Shared View": pages[1]["name"],
                    "Show Second Embedded": pages[1]["name"],
                }
                bookmarks = [
                    _read_json(bookmarks_dir / f"{name}.bookmark.json")
                    for name in indexed
                ]
                self.assertCountEqual(
                    [bookmark["displayName"] for bookmark in bookmarks],
                    expected_targets)
                for name, bookmark in zip(indexed, bookmarks):
                    self.assertEqual(bookmark["name"], name)
                    target = expected_targets[bookmark["displayName"]]
                    state = bookmark["explorationState"]
                    self.assertEqual(state["activeSection"], target)
                    self.assertEqual(set(state["sections"]), {target})


if __name__ == "__main__":
    unittest.main()
