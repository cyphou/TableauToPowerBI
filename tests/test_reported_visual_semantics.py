"""Synthetic XML-to-PBIR regressions for reported visual defects 8 and 9.

The tests inspect extracted objects and serialized ``visual.json`` only.
They do not render visuals in Power BI or execute a semantic model.
"""

import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path

from tableau_export.extract_tableau_data import TableauExtractor
from powerbi_import.migration_quality import scan_self_heal_placeholder_columns
from powerbi_import.pbip_generator import (
    PowerBIProjectGenerator,
    _write_json,
)


_BASE = "[federated.synthetic].[sum:Base:qk]"
_TOTAL = "[federated.synthetic].[sum:Total:qk]"
_MISSING = "[federated.synthetic].[sum:Missing:qk]"
_LABEL_RUNS = [
    {"type": "literal", "text": 'Sur l\'ensemble "actif" : '},
    {"type": "field", "reference": _BASE},
    {"type": "literal", "text": " unités sur "},
    {"type": "field", "reference": _TOTAL},
    {"type": "literal", "text": " / manquant "},
    {"type": "field", "reference": _MISSING},
]
_VISIBLE_LABEL = (
    'Sur l\'ensemble "actif" : '
    f"⟦Tableau field not evaluated: {_BASE}⟧ unités sur "
    f"⟦Tableau field not evaluated: {_TOTAL}⟧ / manquant "
    f"⟦Tableau field not evaluated: {_MISSING}⟧"
)


def _literal_string(test, expression):
    """Decode a PBIR single-quoted string and reject unescaped apostrophes."""
    literal = expression["expr"]["Literal"]["Value"]
    test.assertRegex(literal, r"(?s)^'(?:[^']|'')*'$")
    return literal[1:-1].replace("''", "'")


class _VisualFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.generator = PowerBIProjectGenerator(output_dir=str(self.root))

    def extract(self, xml, kind):
        extractor = TableauExtractor(
            str(self.root / "synthetic.twb"), output_dir=str(self.root))
        with redirect_stdout(io.StringIO()):
            if kind == "dashboard":
                extractor.extract_dashboards(ET.fromstring(xml))
                items = extractor.workbook_data["dashboards"]
            else:
                extractor.extract_worksheets(ET.fromstring(xml))
                items = extractor.workbook_data["worksheets"]
        self.assertEqual(len(items), 1)
        return items[0]

    def read_only_visual(self, directory):
        paths = list(Path(directory).rglob("visual.json"))
        self.assertEqual(len(paths), 1, paths)
        return json.loads(paths[0].read_text(encoding="utf-8"))

    def write_payload(self, label, payload):
        path = self.root / label / "visual.json"
        _write_json(str(path), payload, ensure_ascii=False)
        return json.loads(path.read_text(encoding="utf-8"))

    def assert_unknown_background_omitted(self, payload, scope, raw=None):
        if "visual" in payload:
            objects = payload["visual"].get("visualContainerObjects", {})
        else:
            objects = payload.get("objects", {})
        self.assertFalse("background" in objects,
                         f"Unexpected PBIR background: {objects.get('background')!r}")
        notes = [
            item.get("value", "") for item in payload.get("annotations", [])
            if item.get("name") == "MigrationNote"
        ]
        matching = [
            note for note in notes
            if "transparen" in note.lower() and "omit" in note.lower()
            and scope in note.lower()
            and any(word in note.lower() for word in ("unverified", "unknown", "ambiguous"))
        ]
        self.assertTrue(matching, notes)
        if raw:
            self.assertTrue(any(raw in note for note in matching), matching)


class TestReportedDashboardBackground(_VisualFixture):
    """Defect 8: declared layout fill follows only its proven descendants."""

    def dashboard_objects(self, zones):
        dashboard = self.extract(
            '<workbook><dashboards><dashboard name="Synthetic">'
            f"<zones>{zones}</zones>"
            "</dashboard></dashboards></workbook>",
            "dashboard",
        )
        objects = [item for item in dashboard["objects"] if item["type"] == "text"]
        self.assertEqual(len(objects), len({item["name"] for item in objects}))
        return {item["name"]: item for item in objects}

    def emit_text(self, obj):
        directory = self.root / ("text-" + obj["name"])
        self.generator._create_visual_textbox(str(directory), obj, 1, 1, 0)
        payload = self.read_only_visual(directory)
        self.assertEqual(payload["visual"]["visualType"], "textbox")
        return payload

    def assert_fill(self, obj, color):
        self.assertEqual(obj.get("background_color"), color)
        payload = self.emit_text(obj)
        background = payload["visual"]["visualContainerObjects"]["background"]
        self.assertEqual(len(background), 1)
        props = background[0]["properties"]
        self.assertEqual(_literal_string(self, props["color"]["solid"]["color"]), color)
        self.assertEqual(props["show"]["expr"]["Literal"]["Value"], "true")
        self.assertEqual(props["transparency"]["expr"]["Literal"]["Value"], "0D")
        return payload

    def assert_no_inherited_fill(self, obj):
        self.assertNotIn("background_color", obj)
        payload = self.emit_text(obj)
        objects = payload["visual"].get("visualContainerObjects", {})
        self.assertNotIn("background", objects)

    def emit_page(self, dashboard, label):
        directory = self.root / label
        with redirect_stdout(io.StringIO()):
            self.generator._create_dashboard_pages(
                str(directory), [dashboard], [], {}, {})
        paths = list(directory.rglob("page.json"))
        self.assertEqual(len(paths), 1, paths)
        return json.loads(paths[0].read_text(encoding="utf-8"))

    def test_white_text_inherits_explicit_dark_layout_fill_in_visual_json(self):
        objects = self.dashboard_objects("""
          <zone id="parent" type-v2="layout-flow" param="vert">
            <zone-style><format attr="background-color" value="#172b3a"/></zone-style>
            <zone id="1" name="Dark text" type-v2="text"
                  x="0" y="0" w="200" h="80">
              <formatted-text><run fontcolor="#ffffff">Libellé blanc</run></formatted-text>
            </zone>
          </zone>
        """)
        payload = self.assert_fill(objects["Dark text"], "#172b3a")
        paragraphs = payload["visual"]["objects"]["general"][0]["properties"]["paragraphs"]
        runs = [run for paragraph in paragraphs for run in paragraph["textRuns"]]
        self.assertEqual([(run["value"], run["textStyle"]["color"]) for run in runs],
                         [("Libellé blanc", "#ffffff")])

    def test_closest_layout_and_explicit_child_win_without_sibling_leakage(self):
        objects = self.dashboard_objects("""
          <zone id="root" type-v2="layout-flow" param="vert">
            <zone-style><format attr="background-color" value="#112233"/></zone-style>
            <zone id="1" name="Outer first" type-v2="text">
              <formatted-text><run fontcolor="#ffffff">Outer first</run></formatted-text>
            </zone>
            <zone id="nested" type-v2="layout-flow" param="horz">
              <zone-style><format attr="background-color" value="#224455"/></zone-style>
              <zone id="2" name="Inner first" type-v2="text">
                <formatted-text><run fontcolor="#ffffff">Inner first</run></formatted-text>
              </zone>
              <zone id="3" name="Child override" type-v2="text">
                <zone-style><format attr="background-color" value="#667788"/></zone-style>
                <formatted-text><run fontcolor="#ffffff">Override</run></formatted-text>
              </zone>
              <zone id="4" name="Inner after" type-v2="text">
                <formatted-text><run fontcolor="#ffffff">Inner after</run></formatted-text>
              </zone>
            </zone>
            <zone id="5" name="Outer after" type-v2="text">
              <formatted-text><run fontcolor="#ffffff">Outer after</run></formatted-text>
            </zone>
          </zone>
        """)
        for name, color in (
            ("Outer first", "#112233"), ("Inner first", "#224455"),
            ("Child override", "#667788"), ("Inner after", "#224455"),
            ("Outer after", "#112233"),
        ):
            with self.subTest(name=name):
                self.assert_fill(objects[name], color)

    def test_color_does_not_cross_sibling_branches_or_reach_standalone_text(self):
        objects = self.dashboard_objects("""
          <zone id="colored" type-v2="layout-flow">
            <zone-style><format attr="background-color" value="#882244"/></zone-style>
            <zone id="1" name="Colored branch" type-v2="text">
              <formatted-text><run>Colored branch</run></formatted-text>
            </zone>
          </zone>
          <zone id="plain" type-v2="layout-flow">
            <zone id="2" name="Plain branch" type-v2="text">
              <formatted-text><run>Plain branch</run></formatted-text>
            </zone>
          </zone>
          <zone id="3" name="Standalone" type-v2="text">
            <formatted-text><run>Standalone</run></formatted-text>
          </zone>
        """)
        self.assert_fill(objects["Colored branch"], "#882244")
        self.assert_no_inherited_fill(objects["Plain branch"])
        self.assert_no_inherited_fill(objects["Standalone"])

    def test_ambiguous_floating_sibling_never_supplies_or_proves_a_fill(self):
        objects = self.dashboard_objects("""
          <zone id="parent" type-v2="layout-flow">
            <zone-style><format attr="background-color" value="#112233"/></zone-style>
            <zone id="1" name="Tiled" type-v2="text" x="0" y="0" w="100" h="60">
              <formatted-text><run fontcolor="#ffffff">Tiled</run></formatted-text>
            </zone>
            <zone id="2" name="Floating" type-v2="text" is-floating="true">
              <zone-style><format attr="background-color" value="#556677"/></zone-style>
              <formatted-text><run>Floating</run></formatted-text>
            </zone>
            <zone id="3" name="Own fill" type-v2="text">
              <zone-style><format attr="background-color" value="#335577"/></zone-style>
              <formatted-text><run>Own fill</run></formatted-text>
            </zone>
          </zone>
        """)
        # The floating sibling has no complete rectangle; its relative layer
        # cannot establish the floating or parent fill on Tiled.
        self.assert_no_inherited_fill(objects["Tiled"])
        self.assert_fill(objects["Floating"], "#556677")
        self.assert_fill(objects["Own fill"], "#335577")

    def test_non_overlapping_floating_sibling_does_not_block_known_parent(self):
        objects = self.dashboard_objects("""
          <zone id="parent" type-v2="layout-flow">
            <zone-style><format attr="background-color" value="#102030"/></zone-style>
            <zone id="1" name="Tiled" type-v2="text" x="0" y="0" w="100" h="60">
              <formatted-text><run fontcolor="#ffffff">Tiled</run></formatted-text>
            </zone>
            <zone id="2" name="Floating" type-v2="text" is-floating="true"
                  x="300" y="300" w="40" h="40">
              <zone-style><format attr="background-color" value="#556677"/></zone-style>
              <formatted-text><run>Floating</run></formatted-text>
            </zone>
          </zone>
        """)
        self.assert_fill(objects["Tiled"], "#102030")
        self.assert_fill(objects["Floating"], "#556677")

    def test_floating_text_without_its_own_fill_does_not_inherit(self):
        objects = self.dashboard_objects("""
          <zone id="parent" type-v2="layout-flow">
            <zone-style><format attr="background-color" value="#102030"/></zone-style>
            <zone id="1" name="Floating" type-v2="text" is-floating="true"
                  x="0" y="0" w="100" h="50">
              <formatted-text><run>Floating</run></formatted-text>
            </zone>
          </zone>
        """)
        self.assertEqual(objects["Floating"]["layout"], "floating")
        self.assert_no_inherited_fill(objects["Floating"])

    def test_unrecognized_child_override_does_not_fall_back_to_parent(self):
        objects = self.dashboard_objects("""
          <zone id="parent" type-v2="layout-flow">
            <zone-style><format attr="background-color" value="#102030"/></zone-style>
            <zone id="1" name="Unknown override" type-v2="text">
              <zone-style><format attr="background-color" value=""/></zone-style>
              <formatted-text><run>Unknown</run></formatted-text>
            </zone>
          </zone>
        """)
        self.assert_no_inherited_fill(objects["Unknown override"])

    def test_argb_fill_without_separate_transparency_is_still_emitted(self):
        objects = self.dashboard_objects("""
          <zone id="1" name="Alpha" type-v2="text">
            <zone-style><format attr="background-color" value="#80445566"/></zone-style>
            <formatted-text><run>Alpha control</run></formatted-text>
          </zone>
        """)
        payload = self.emit_text(objects["Alpha"])
        props = payload["visual"]["visualContainerObjects"]["background"][0]["properties"]
        self.assertEqual(_literal_string(self, props["color"]["solid"]["color"]), "#445566")
        self.assertEqual(props["show"]["expr"]["Literal"]["Value"], "true")
        self.assertEqual(
            props["transparency"]["expr"]["Literal"]["Value"],
            f"{round((255 - 128) * 100 / 255, 6):g}D",
        )
        self.assertFalse(any(
            "background_transparency" in item.get("value", "").lower()
            for item in payload.get("annotations", [])
        ))

    def test_separate_zone_transparency_is_omitted_and_diagnosed(self):
        # None of these separate values supplies verified units or semantics,
        # even if it happens to look like 0% or 50%.
        for index, raw in enumerate(("0", "0.35", "50", "unknown")):
            with self.subTest(raw=raw):
                name = f"Separate {index}"
                objects = self.dashboard_objects(f"""
                  <zone id="{index}" name="{name}" type-v2="text">
                    <zone-style>
                      <format attr="background-color" value="#80445566"/>
                      <format attr="background-transparency" value="{raw}"/>
                    </zone-style>
                    <formatted-text><run>Visible text</run></formatted-text>
                  </zone>
                """)
                self.assertEqual(objects[name]["background_color"], "#80445566")
                self.assertEqual(objects[name]["background_transparency"], raw)
                with self.assertLogs("powerbi_import.pbip_generator", "WARNING") as logs:
                    payload = self.emit_text(objects[name])
                self.assert_unknown_background_omitted(payload, "dashboard zone", raw)
                self.assertTrue(any("background_transparency" in line
                                    and "omitted" in line.lower()
                                    for line in logs.output), logs.output)
                paragraphs = payload["visual"]["objects"]["general"][0]["properties"]["paragraphs"]
                self.assertEqual(paragraphs[0]["textRuns"][0]["value"], "Visible text")

    def test_separate_parent_transparency_does_not_override_a_child_fill(self):
        objects = self.dashboard_objects("""
          <zone id="parent" type-v2="layout-flow">
            <zone-style>
              <format attr="background-color" value="#112233"/>
              <format attr="background-transparency" value="35"/>
            </zone-style>
            <zone id="1" name="Inherited" type-v2="text">
              <formatted-text><run>Inherited</run></formatted-text>
            </zone>
            <zone id="2" name="Own opaque fill" type-v2="text">
              <zone-style><format attr="background-color" value="#abcdef"/></zone-style>
              <formatted-text><run>Own</run></formatted-text>
            </zone>
          </zone>
        """)
        self.assertEqual(objects["Inherited"]["background_transparency"], "35")
        with self.assertLogs("powerbi_import.pbip_generator", "WARNING"):
            inherited = self.emit_text(objects["Inherited"])
        self.assert_unknown_background_omitted(inherited, "dashboard zone", "35")
        self.assertNotIn("background_transparency", objects["Own opaque fill"])
        self.assert_fill(objects["Own opaque fill"], "#abcdef")

    def test_empty_or_conflicting_zone_transparency_never_becomes_opaque(self):
        cases = (
            ("Empty", "", '<format attr="background-transparency" value=""/>'),
            ("Missing value", "", '<format attr="background-transparency"/>'),
            ("Conflicting", 'background-transparency="0.2"',
             '<format attr="background-transparency" value="0.8"/>'),
        )
        for index, (name, attribute, transparency) in enumerate(cases):
            with self.subTest(case=name):
                objects = self.dashboard_objects(f"""
                  <zone id="{index}" name="{name}" type-v2="text" {attribute}>
                    <zone-style>
                      <format attr="background-color" value="#172b3a"/>
                      {transparency}
                    </zone-style>
                    <formatted-text><run>Uncertain transparency</run></formatted-text>
                  </zone>
                """)
                payload = self.emit_text(objects[name])
                self.assert_unknown_background_omitted(payload, "dashboard zone")

    def test_unknown_parent_transparency_is_not_silently_discarded(self):
        cases = (
            ("empty", "", '<format attr="background-transparency" value=""/>'),
            ("conflicting", 'background-transparency="0.2"',
             '<format attr="background-transparency" value="0.8"/>'),
        )
        for index, (label, attribute, transparency) in enumerate(cases):
            with self.subTest(case=label):
                name = f"Parent unknown {index}"
                objects = self.dashboard_objects(f"""
                  <zone id="parent" type-v2="layout-flow" {attribute}>
                    <zone-style>
                      <format attr="background-color" value="#172b3a"/>
                      {transparency}
                    </zone-style>
                    <zone id="{index}" name="{name}" type-v2="text">
                      <formatted-text><run>Inherited uncertainty</run></formatted-text>
                    </zone>
                  </zone>
                """)
                payload = self.emit_text(objects[name])
                self.assert_unknown_background_omitted(payload, "dashboard zone")

    def test_page_writer_omits_separate_transparency_instead_of_guessing(self):
        dashboard = self.extract("""
          <workbook><dashboards><dashboard name="Page control">
            <style><style-rule element="dashboard">
              <format attr="background-color" value="#FF102030"/>
            </style-rule></style>
            <zones/>
          </dashboard></dashboards></workbook>
        """, "dashboard")
        dashboard["formatting"]["background_transparency"] = "0.35"
        with self.assertLogs("powerbi_import.pbip_generator", "WARNING") as logs:
            page = self.emit_page(dashboard, "page-separate")
        self.assert_unknown_background_omitted(page, "dashboard", "0.35")
        self.assertTrue(any("background_transparency" in line
                            and "omitted" in line.lower()
                            for line in logs.output), logs.output)

    def test_page_transparency_from_source_xml_is_not_discarded(self):
        dashboard = self.extract("""
          <workbook><dashboards><dashboard name="Page from XML">
            <style><style-rule element="dashboard">
              <format attr="background-color" value="#FF102030"/>
              <format attr="background-transparency" value="0.35"/>
            </style-rule></style>
            <zones/>
          </dashboard></dashboards></workbook>
        """, "dashboard")
        page = self.emit_page(dashboard, "page-from-xml")
        self.assert_unknown_background_omitted(page, "dashboard", "0.35")


class TestReportedFormattedMarkLabel(_VisualFixture):
    """Defect 9: visible static fallback, not invented dynamic field values."""

    _xml = """
      <workbook><worksheets><worksheet name="Synthetic Label"><table>
        <cols/><rows/><panes><pane><mark class="Text"/><encodings>
          <text column="[federated.synthetic].[sum:Base:qk]">
            <formatted-text>
              <run>Sur l'ensemble "actif" : </run>
              <run>&lt;[federated.synthetic].</run><run>[sum:Base:qk]&gt;</run>
              <run> unités sur </run>
              <run>&lt;[federated.synthetic].[sum:Total:qk]&gt;</run>
              <run> / manquant </run>
              <run>&lt;[federated.synthetic].[sum:Missing:qk]&gt;</run>
            </formatted-text>
          </text>
          <text column="[federated.synthetic].[sum:Total:qk]"/>
        </encodings></pane></panes>
      </table></worksheet></worksheets></workbook>
    """

    def setUp(self):
        super().setUp()
        self.generator._main_table = "Facts"
        self.generator._field_map = {
            name: ("Facts", name) for name in ("Base", "Total")}
        self.generator._actual_bim_symbols = set(self.generator._field_map.values())
        self.generator._actual_bim_column_types = {
            ("Facts", name): "double" for name in ("Base", "Total")}
        self.generator._measure_names = {"Base", "Total"}
        self.generator._bim_measure_names = set()
        self.generator._actual_bim_measure_names = set()

    def emit_worksheet(self, worksheet, label):
        directory = self.root / label
        data = deepcopy(worksheet)
        with redirect_stdout(io.StringIO()):
            self.generator._create_visual_worksheet(
                str(directory), data,
                {"worksheetName": worksheet["name"],
                 "position": {"x": 0, "y": 0, "w": 600, "h": 200}},
                1, 1, 0, [data], {})
        return self.read_only_visual(directory)

    def baseline(self, worksheet, label):
        baseline = deepcopy(worksheet)
        baseline.pop("formatted_label_runs", None)
        baseline.pop("formatted_label_blocks", None)
        return self.emit_worksheet(baseline, label)

    def visible_subtitle(self, payload):
        subtitle, = payload["visual"]["visualContainerObjects"]["subTitle"]
        props = subtitle["properties"]
        self.assertEqual(props["show"]["expr"]["Literal"]["Value"], "true")
        return _literal_string(self, props["text"])

    def assert_existing_subtitle_blocks_fallback(self, entries, label):
        worksheet = self.extract(self._xml, "worksheet")
        payload = self.baseline(worksheet, "before-" + label)
        query = deepcopy(payload["visual"]["query"])
        payload["visual"]["visualContainerObjects"]["subTitle"] = deepcopy(entries)
        self.generator._apply_visual_formatting(
            payload, worksheet["name"], deepcopy(worksheet))
        once = deepcopy(payload)
        self.generator._apply_visual_formatting(
            payload, worksheet["name"], deepcopy(worksheet))
        self.assertEqual(payload, once)
        serialized = self.write_payload("after-" + label, payload)
        self.assertEqual(serialized["visual"]["query"], query)
        self.assertEqual(
            serialized["visual"]["visualContainerObjects"]["subTitle"], entries)
        notes = [
            item.get("value", "").lower() for item in serialized.get("annotations", [])
            if item.get("name") == "MigrationNote"
        ]
        blocked = [note for note in notes
                   if "subtitle" in note and "not emitted" in note]
        self.assertEqual(len(blocked), 1, notes)
        self.assertIn("not evaluated", blocked[0])
        stored = [item for item in serialized["annotations"]
                  if item.get("name") == "TableauFormattedLabelRuns"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(json.loads(stored[0]["value"]), _LABEL_RUNS)

    def test_xml_runs_render_in_order_and_keep_existing_projections(self):
        worksheet = self.extract(self._xml, "worksheet")
        self.assertEqual(worksheet["formatted_label_runs"], _LABEL_RUNS)
        self.assertEqual(
            [(field["name"], field["shelf"], field["aggregation"])
             for field in worksheet["fields"]],
            [("Base", "text", "sum"), ("Total", "text", "sum")],
        )
        without_label = self.baseline(worksheet, "without-label")
        payload = self.emit_worksheet(worksheet, "with-label")
        visual = payload["visual"]
        self.assertEqual(visual["visualType"], "multiRowCard")
        self.assertEqual(visual["query"], without_label["visual"]["query"])
        projections = visual["query"]["queryState"]["Values"]["projections"]
        self.assertEqual(
            [projection["field"]["Aggregation"]["Expression"]["Column"]["Property"]
             for projection in projections],
            ["Base", "Total"],
        )
        self.assertEqual(self.visible_subtitle(payload), _VISIBLE_LABEL)
        raw = visual["visualContainerObjects"]["subTitle"][0]["properties"]["text"]
        self.assertIn("l''ensemble", raw["expr"]["Literal"]["Value"])
        # Missing is displayed as an unevaluated source token, not fabricated
        # as a third query projection or passed off as an actual field value.
        self.assertNotIn("Missing", json.dumps(visual["query"]))
        annotations = payload.get("annotations", [])
        stored = [item for item in annotations
                  if item.get("name") == "TableauFormattedLabelRuns"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(json.loads(stored[0]["value"]), _LABEL_RUNS)
        self.assertTrue(any("not interpolated" in item.get("value", "").lower()
                            and "projection" in item.get("value", "").lower()
                            for item in annotations), annotations)

    def test_unresolved_reference_apostrophe_is_escaped_without_fake_binding(self):
        reference = "[federated.synthetic].[sum:O'Brien:qk]"
        worksheet = self.extract(self._xml.replace(_MISSING, reference), "worksheet")
        self.assertEqual(worksheet["formatted_label_runs"][-1],
                         {"type": "field", "reference": reference})
        baseline = self.baseline(worksheet, "without-apostrophe-reference")
        payload = self.emit_worksheet(worksheet, "with-apostrophe-reference")
        self.assertEqual(payload["visual"]["query"], baseline["visual"]["query"])
        self.assertNotIn("O'Brien", json.dumps(payload["visual"]["query"]))
        self.assertEqual(self.visible_subtitle(payload),
                         _VISIBLE_LABEL.replace(_MISSING, reference))
        raw = payload["visual"]["visualContainerObjects"]["subTitle"][0]["properties"]["text"]
        self.assertIn("O''Brien", raw["expr"]["Literal"]["Value"])

    def test_existing_static_subtitle_is_kept_escaped_and_not_duplicated(self):
        worksheet = self.extract(self._xml, "worksheet")
        payload = self.baseline(worksheet, "before-static-subtitle")
        query = deepcopy(payload["visual"]["query"])
        previous = {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "text": {"expr": {"Literal": {"Value": "'L''été \"actif\"'"}}},
            "fontSize": {"expr": {"Literal": {"Value": "12D"}}},
        }
        payload["visual"]["visualContainerObjects"]["subTitle"] = [
            {"properties": deepcopy(previous)}]
        self.generator._apply_visual_formatting(
            payload, worksheet["name"], deepcopy(worksheet))
        once = deepcopy(payload)
        self.generator._apply_visual_formatting(
            payload, worksheet["name"], deepcopy(worksheet))
        self.assertEqual(payload, once)
        serialized = self.write_payload("after-static-subtitle", payload)
        self.assertEqual(serialized["visual"]["query"], query)
        self.assertEqual(self.visible_subtitle(serialized),
                         'L\'été "actif" | ' + _VISIBLE_LABEL)
        props = serialized["visual"]["visualContainerObjects"]["subTitle"][0]["properties"]
        self.assertEqual(props["fontSize"], previous["fontSize"])

    def test_existing_dynamic_subtitle_is_not_overwritten_or_misrepresented(self):
        worksheet = self.extract(self._xml, "worksheet")
        payload = self.baseline(worksheet, "before-dynamic-subtitle")
        query = deepcopy(payload["visual"]["query"])
        dynamic = {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "text": {"expr": {"Measure": {
                "Expression": {"SourceRef": {"Entity": "Facts"}},
                "Property": "Dynamic subtitle",
            }}},
        }
        payload["visual"]["visualContainerObjects"]["subTitle"] = [
            {"properties": deepcopy(dynamic)}]
        self.generator._apply_visual_formatting(
            payload, worksheet["name"], deepcopy(worksheet))
        serialized = self.write_payload("after-dynamic-subtitle", payload)
        self.assertEqual(serialized["visual"]["query"], query)
        self.assertEqual(
            serialized["visual"]["visualContainerObjects"]["subTitle"][0]["properties"],
            dynamic,
        )
        notes = [item.get("value", "").lower()
                 for item in serialized.get("annotations", [])
                 if item.get("name") == "MigrationNote"]
        self.assertTrue(any("not emitted" in note and "dynamic" in note
                            and "not evaluated" in note for note in notes), notes)
        stored = next(item for item in serialized["annotations"]
                      if item.get("name") == "TableauFormattedLabelRuns")
        self.assertEqual(json.loads(stored["value"]), _LABEL_RUNS)

    def test_hidden_and_dynamic_visibility_subtitles_remain_unchanged(self):
        text = {"expr": {"Literal": {"Value": "'Existing text'"}}}
        dynamic_show = {"expr": {"Measure": {
            "Expression": {"SourceRef": {"Entity": "Facts"}},
            "Property": "Show subtitle",
        }}}
        cases = (
            ("hidden", {"expr": {"Literal": {"Value": "false"}}}),
            ("dynamic-show", dynamic_show),
        )
        for label, show in cases:
            with self.subTest(case=label):
                self.assert_existing_subtitle_blocks_fallback(
                    [{"properties": {"show": show, "text": text}}], label)

    def test_ambiguous_subtitles_are_not_replaced_or_merged(self):
        properties = {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "text": {"expr": {"Literal": {"Value": "'Existing'"}}},
        }
        cases = (
            ("selected", [{"selector": {"id": "0"},
                           "properties": deepcopy(properties)}]),
            ("multiple", [
                {"properties": deepcopy(properties)},
                {"properties": {
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                    "text": {"expr": {"Literal": {"Value": "'Second'"}}},
                }},
            ]),
        )
        for label, entries in cases:
            with self.subTest(case=label):
                self.assert_existing_subtitle_blocks_fallback(entries, label)

    def test_multiple_or_pane_scoped_templates_are_not_flattened(self):
        def text_encoding(reference, literal=None):
            if literal is None:
                return f'<text column="{reference}"/>'
            return (
                f'<text column="{reference}"><formatted-text>'
                f'<run>{literal}</run><run>&lt;{reference}&gt;</run>'
                '</formatted-text></text>'
            )

        def pane(identity, encodings):
            return (f'<pane id="{identity}"><mark class="Text"/>'
                    f'<encodings>{encodings}</encodings></pane>')

        first = text_encoding(_BASE, "First: ")
        second = text_encoding(_TOTAL, "Second: ")
        cases = (
            ("two-panes-two-labels",
             pane("first", first) + pane("second", second),
             [(0, "first", _BASE, "First: "), (1, "second", _TOTAL, "Second: ")]),
            ("two-panes-one-label",
             pane("first", first) + pane("second", text_encoding(_TOTAL)),
             [(0, "first", _BASE, "First: ")]),
            ("one-pane-two-labels", pane("only", first + second),
             [(0, "only", _BASE, "First: "), (0, "only", _TOTAL, "Second: ")]),
        )
        for label, panes, expected in cases:
            with self.subTest(case=label):
                worksheet = self.extract(
                    '<workbook><worksheets><worksheet name="Multi Label"><table>'
                    f'<cols/><rows/><panes>{panes}</panes>'
                    '</table></worksheet></worksheets></workbook>', "worksheet")
                blocks = worksheet["formatted_label_blocks"]
                self.assertEqual(len(blocks), len(expected))
                for block, (index, pane_id, reference, literal) in zip(blocks, expected):
                    self.assertEqual((block["pane_index"], block["pane_id"]),
                                     (index, pane_id))
                    self.assertEqual(block["runs"], [
                        {"type": "literal", "text": literal},
                        {"type": "field", "reference": reference},
                    ])
                self.assertEqual(len({block["source_path"] for block in blocks}),
                                 len(blocks))
                if len(blocks) > 1:
                    self.assertNotIn("formatted_label_runs", worksheet)
                else:
                    self.assertEqual(worksheet["formatted_label_runs"], blocks[0]["runs"])

                baseline = self.baseline(worksheet, "without-" + label)
                payload = self.emit_worksheet(worksheet, "with-" + label)
                visual = payload["visual"]
                self.assertEqual(visual["visualType"], "multiRowCard")
                self.assertEqual(visual["query"], baseline["visual"]["query"])
                self.assertEqual(
                    [projection["field"]["Aggregation"]["Expression"]["Column"]["Property"]
                     for projection in visual["query"]["queryState"]["Values"]["projections"]],
                    ["Base", "Total"],
                )
                self.assertNotIn("subTitle", visual.get("visualContainerObjects", {}))
                stored = [item for item in payload.get("annotations", [])
                          if item.get("name") == "TableauFormattedLabelBlocks"]
                self.assertEqual(len(stored), 1)
                self.assertEqual(json.loads(stored[0]["value"]), blocks)
                notes = [item.get("value", "").lower()
                         for item in payload.get("annotations", [])
                         if item.get("name") == "MigrationNote"]
                self.assertTrue(any("not emitted" in note
                                    and ("pane" in note or "multiple" in note)
                                    for note in notes), notes)

    def test_row_shelf_template_is_not_misrepresented_as_one_card_subtitle(self):
        xml = self._xml.replace("<rows/>", f"<rows>{_BASE}</rows>", 1)
        worksheet = self.extract(xml, "worksheet")
        self.assertTrue(any(field["shelf"] == "rows" for field in worksheet["fields"]))
        baseline = self.baseline(worksheet, "row-shelf-without-template")
        payload = self.emit_worksheet(worksheet, "row-shelf-with-template")
        self.assertEqual(payload["visual"]["query"], baseline["visual"]["query"])
        self.assertNotIn("subTitle",
                         payload["visual"].get("visualContainerObjects", {}))
        stored = [item for item in payload.get("annotations", [])
                  if item.get("name") == "TableauFormattedLabelRuns"]
        self.assertEqual(len(stored), 1)
        self.assertEqual(json.loads(stored[0]["value"]), _LABEL_RUNS)
        notes = [item.get("value", "").lower() for item in payload.get("annotations", [])
                 if item.get("name") == "MigrationNote"]
        self.assertTrue(any("not reconstructed or rendered" in note
                            and "query" in note for note in notes), notes)

    def test_unknown_pane_transparency_preserves_label_and_projections(self):
        cases = (
            ("attribute", '<pane background-color="#80445566" '
                          'background-transparency="0">', "0"),
            ("empty", '<pane background-color="#80445566" '
                      'background-transparency="">', ""),
            ("format", '<pane><format attr="background-color" value="#80445566"/>'
                       '<format attr="background-transparency" value="50"/>', "50"),
        )
        for label, pane_start, raw in cases:
            with self.subTest(case=label):
                worksheet = self.extract(
                    self._xml.replace("<pane>", pane_start, 1), "worksheet")
                formatting = worksheet["formatting"]
                self.assertEqual(formatting["background_color"], "#80445566")
                self.assertEqual(formatting["background_transparency"], raw)
                control = deepcopy(worksheet)
                del control["formatting"]["background_transparency"]
                control_payload = self.emit_worksheet(control, "pane-control-" + label)
                control_background = (
                    control_payload["visual"]["visualContainerObjects"]["background"]
                    [0]["properties"])
                self.assertEqual(
                    control_background["transparency"]["expr"]["Literal"]["Value"],
                    f"{round((255 - 128) * 100 / 255, 6):g}D",
                )
                with self.assertLogs("powerbi_import.pbip_generator", "WARNING") as logs:
                    payload = self.emit_worksheet(worksheet, "pane-unknown-" + label)
                self.assert_unknown_background_omitted(payload, "worksheet pane", raw)
                self.assertTrue(any("background_transparency" in line
                                    and "omitted" in line.lower()
                                    for line in logs.output), logs.output)
                self.assertEqual(payload["visual"]["query"], control_payload["visual"]["query"])
                self.assertEqual(self.visible_subtitle(payload), _VISIBLE_LABEL)

    def test_pane_style_rule_transparency_is_not_lost_before_pbir(self):
        color = '<format attr="background-color" value="#80445566"/>'

        def worksheet_with_style(formats):
            style = f'<style><style-rule element="pane">{formats}</style-rule></style>'
            return self.extract(self._xml.replace("<table>", style + "<table>", 1),
                                "worksheet")

        control = worksheet_with_style(color)
        control_payload = self.emit_worksheet(control, "style-rule-control")
        background = control_payload["visual"]["visualContainerObjects"]["background"]
        self.assertEqual(
            background[0]["properties"]["transparency"]["expr"]["Literal"]["Value"],
            f"{round((255 - 128) * 100 / 255, 6):g}D",
        )
        cases = (
            ("known-raw-unknown-units",
             '<format attr="background-transparency" value="50"/>', "50"),
            ("empty", '<format attr="background-transparency" value=""/>', ""),
            ("missing-value", '<format attr="background-transparency"/>', ""),
        )
        for label, transparency, raw in cases:
            with self.subTest(case=label):
                worksheet = worksheet_with_style(color + transparency)
                self.assertEqual(
                    worksheet["formatting"]["pane"]["background-color"], "#80445566")
                payload = self.emit_worksheet(worksheet, "style-rule-" + label)
                self.assert_unknown_background_omitted(payload, "worksheet pane", raw)
                self.assertEqual(payload["visual"]["query"], control_payload["visual"]["query"])
                self.assertEqual(self.visible_subtitle(payload), _VISIBLE_LABEL)


class TestPlaceholderMetadataModelScope(_VisualFixture):
    """The metadata scanner must not borrow a sibling SemanticModel."""

    def write_model(self, model, table, placeholder):
        directory = self.root / (model + ".SemanticModel") / "definition" / "tables"
        directory.mkdir(parents=True)
        (directory / (table + ".tmdl")).write_text(
            f"table '{table}'\n"
            f"\tcolumn '{placeholder}' = BLANK()\n"
            "\t\tdataType: string\n"
            "\t\tannotation MigrationNote = Self-heal: placeholder column created "
            "to satisfy a missing reference\n"
            "\tcolumn 'Ordinary blank' = BLANK()\n"
            "\t\tdataType: string\n"
            '\tmeasure Mention = "Self-heal: placeholder column created to satisfy text"\n',
            encoding="utf-8",
        )

    def metadata(self, model):
        self.generator.create_metadata(str(self.root), model, {})
        metadata = json.loads((self.root / "migration_metadata.json").read_text(
            encoding="utf-8"))
        self.assertEqual(metadata["report_name"], model)
        return metadata["degradations"]

    def test_scanner_and_metadata_use_only_the_current_model_in_both_orders(self):
        self.write_model("Current", "CurrentTable", "CurrentMissing")
        self.write_model("Other", "OtherTable", "OtherMissing")
        unscoped = scan_self_heal_placeholder_columns(str(self.root))
        self.assertEqual(unscoped["count"], 2)
        self.assertEqual({item["column"] for item in unscoped["columns"]},
                         {"CurrentMissing", "OtherMissing"})

        for model, table, placeholder in (
            ("Current", "CurrentTable", "CurrentMissing"),
            ("Other", "OtherTable", "OtherMissing"),
            ("Current", "CurrentTable", "CurrentMissing"),
        ):
            with self.subTest(model=model):
                scoped = scan_self_heal_placeholder_columns(
                    str(self.root), model_name=model + ".SemanticModel")
                self.assertEqual(scoped["status"], "static_evidence")
                self.assertEqual(scoped["count"], 1)
                self.assertEqual([item["column"] for item in scoped["columns"]],
                                 [placeholder])
                self.assertTrue(scoped["columns"][0]["target_path"].startswith(
                    model + ".SemanticModel/"))
                degradations = self.metadata(model)
                self.assertEqual(degradations["self_heal_placeholder_columns"], scoped)
                self.assertEqual(degradations["self_healed_tables"], [table])

    def test_absent_current_model_is_not_reported_as_sibling_evidence(self):
        self.write_model("Other", "OtherTable", "OtherMissing")
        evidence = self.metadata("Absent")["self_heal_placeholder_columns"]
        self.assertEqual(evidence["status"], "not_run")
        self.assertEqual(evidence["count"], 0)
        self.assertEqual(evidence["columns"], [])

    def test_model_name_with_glob_metacharacters_is_treated_literally(self):
        self.write_model("Current[Q1]", "CurrentTable", "CurrentMissing")
        self.write_model("CurrentQ", "OtherTable", "OtherMissing")
        evidence = self.metadata("Current[Q1]")["self_heal_placeholder_columns"]
        self.assertEqual(evidence["status"], "static_evidence")
        self.assertEqual(evidence["count"], 1)
        self.assertEqual(evidence["columns"][0]["column"], "CurrentMissing")
        self.assertTrue(evidence["columns"][0]["target_path"].startswith(
            "Current[Q1].SemanticModel/"))

    def test_scannable_current_model_with_no_placeholders_stays_empty(self):
        self.write_model("Other", "OtherTable", "OtherMissing")
        directory = self.root / "Current.SemanticModel" / "definition" / "tables"
        directory.mkdir(parents=True)
        (directory / "CurrentTable.tmdl").write_text(
            "table 'CurrentTable'\n"
            "\tcolumn 'Ordinary blank' = BLANK()\n"
            "\t\tdataType: string\n"
            '\tmeasure Mention = "BLANK() is ordinary text"\n',
            encoding="utf-8",
        )
        scoped = scan_self_heal_placeholder_columns(
            str(self.root), model_name="Current.SemanticModel")
        self.assertEqual(scoped["status"], "static_evidence")
        self.assertEqual(scoped["files_found"], 1)
        self.assertEqual(scoped["count"], 0)
        self.assertEqual(scoped["columns"], [])
        degradations = self.metadata("Current")
        self.assertEqual(degradations["self_heal_placeholder_columns"], scoped)
        self.assertEqual(degradations["self_healed_tables"], [])
        self.assertEqual(
            scan_self_heal_placeholder_columns(str(self.root))["count"], 1)

    def test_unreadable_current_table_is_partial_not_sibling_evidence(self):
        self.write_model("Current", "CurrentTable", "CurrentMissing")
        self.write_model("Other", "OtherTable", "OtherMissing")
        broken = (self.root / "Current.SemanticModel" / "definition" / "tables"
                  / "Unreadable.tmdl")
        broken.write_bytes(b"\xff\xfeinvalid utf-8")
        evidence = scan_self_heal_placeholder_columns(
            str(self.root), model_name="Current.SemanticModel")
        self.assertEqual(evidence["status"], "partial")
        self.assertEqual(evidence["files_found"], 2)
        self.assertEqual(evidence["files_unreadable"], [
            "Current.SemanticModel/definition/tables/Unreadable.tmdl"])
        self.assertEqual(evidence["count"], 1)
        self.assertEqual([item["column"] for item in evidence["columns"]],
                         ["CurrentMissing"])

    def test_malformed_current_model_is_not_reported_as_a_clean_scan(self):
        self.write_model("Other", "OtherTable", "OtherMissing")
        directory = self.root / "Current.SemanticModel" / "definition" / "tables"
        directory.mkdir(parents=True)
        (directory / "Broken.tmdl").write_text(
            "not a TMDL table declaration\n", encoding="utf-8")
        evidence = self.metadata("Current")["self_heal_placeholder_columns"]
        self.assertEqual(evidence["status"], "not_run")
        self.assertEqual(evidence["files_found"], 1)
        self.assertEqual(evidence["malformed"], [
            "Current.SemanticModel/definition/tables/Broken.tmdl"])
        self.assertEqual(evidence["count"], 0)
        self.assertEqual(evidence["columns"], [])
