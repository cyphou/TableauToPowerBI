"""Synthetic Tableau XML to serialized PBIR visual audit regressions."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from powerbi_import.pbip_generator import PowerBIProjectGenerator
from powerbi_import.visual_generator import build_categorical_color_objects
from tests.test_pbip_generator import _extract_regression_worksheet


class TestAuditVisualSourceToOutput(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        self.generator = PowerBIProjectGenerator(output_dir=self.root)
        self.generator._main_table = "Facts"
        self.generator._field_map = {
            name: ("Facts", name)
            for name in ("Region", "Amount", "Ratio", "Event Date")}
        self.generator._actual_bim_symbols = set(self.generator._field_map.values())
        self.generator._actual_bim_column_types = {
            ("Facts", "Region"): "string", ("Facts", "Amount"): "double",
            ("Facts", "Event Date"): "datetime"}
        self.generator._actual_bim_measure_types = {("Facts", "Ratio"): "double"}
        self.generator._measure_names = {"Amount", "Ratio"}
        self.generator._bim_measure_names = {"Ratio"}
        self.generator._actual_bim_measure_names = {"Ratio"}

    def emit(self, xml):
        worksheet = _extract_regression_worksheet(xml, self.root)
        visuals = Path(self.root, "visuals")
        with redirect_stdout(io.StringIO()):
            self.generator._create_visual_worksheet(
                str(visuals), worksheet,
                {"worksheetName": worksheet["name"], "position": {}},
                1, 1, 0, [worksheet], {})
        paths = list(visuals.rglob("visual.json"))
        self.assertEqual(len(paths), 1)
        return worksheet, json.loads(paths[0].read_text(encoding="utf-8"))

    def test_text_kpi_cell_style_survives_extraction_and_serialization(self):
        worksheet, payload = self.emit("""
            <workbook><worksheet name="Synthetic KPI"><table>
              <cols/><rows/>
              <panes><pane><mark class="Text"/><encodings>
                <text column="[ds].[sum:Amount:qk]"/>
              </encodings></pane></panes>
              <style><style-rule element="cell">
                <format attr="font-color" value="#fe5716"/>
                <format attr="font-family" value="Arial"/>
                <format attr="font-size" value="24"/>
              </style-rule></style>
            </table></worksheet></workbook>
        """)
        self.assertEqual(worksheet["formatting"]["cell"]["font-color"], "#fe5716")
        self.assertEqual(payload["visual"]["visualType"], "card")
        labels = payload["visual"]["objects"]["labels"][0]["properties"]
        self.assertEqual(labels["color"]["solid"]["color"],
                         {"expr": {"Literal": {"Value": "'#fe5716'"}}})
        self.assertEqual(labels["fontFamily"], {"expr": {"Literal": {"Value": "'Arial'"}}})
        self.assertEqual(labels["fontSize"], {"expr": {"Literal": {"Value": "24D"}}})

    def test_percentage_format_is_scoped_to_bound_query_ref_without_rescaling_measure(self):
        worksheet, payload = self.emit("""
            <workbook><worksheet name="Synthetic ratio"><table>
              <cols>[ds].[none:Region:nk]</cols><rows>[ds].[usr:Ratio:qk]</rows>
              <panes><pane><mark class="Bar"/></pane></panes>
              <style><style-rule element="cell">
                <format attr="num-format" field="[ds].[usr:Ratio:qk]" value="0.0%"/>
                <format attr="num-format" field="[ds].[sum:Amount:qk]" value="0.00"/>
                <format attr="font-size" field="[ds].[sum:Amount:qk]" value="24"/>
              </style-rule></style>
            </table></worksheet></workbook>
        """)
        visual = payload["visual"]
        state = visual["query"]["queryState"]
        measure = state["Y"]["projections"][0]
        self.assertEqual(measure["field"], {"Measure": {
            "Expression": {"SourceRef": {"Entity": "Facts"}}, "Property": "Ratio"}})
        scoped = [item for item in visual["objects"]["general"]
                  if item.get("selector", {}).get("metadata") == measure["queryRef"]]
        self.assertEqual(len(scoped), 1)
        self.assertEqual(scoped[0]["properties"]["formatString"],
                         {"expr": {"Literal": {"Value": "'0.0%'"}}})
        self.assertNotIn("100", json.dumps(state))
        self.assertFalse(any(item.get("selector", {}).get("metadata") == "Facts.Amount"
                             for item in visual["objects"]["general"]))

    def test_synthetic_multiple_values_percentage_formats_all_three_rate_series_only(self):
        state = {"Y": {"projections": [
            {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": "Facts"}},
                                   "Property": name}},
             "queryRef": "Facts." + name}
            for name in ("Ready rate", "Eligible rate", "Completion rate")]},
            "Category": {"projections": [{
                "field": {"Column": {"Expression": {"SourceRef": {"Entity": "Facts"}},
                                      "Property": "Region"}}, "queryRef": "Facts.Region"}]}}
        worksheet = {"formatting": {"cell": {
            "field": "[ds].[Multiple Values]", "text-format": "p0.0%"},
            "field_formats": {"[ds].[Multiple Values]": "24"}}}
        objects = {}
        self.generator._build_field_format_objects(objects, worksheet, state)
        self.assertEqual(objects["general"], [
            {"properties": {"formatString": {"expr": {"Literal": {"Value": "'0.0%'"}}}},
             "selector": {"metadata": "Facts." + name}}
            for name in ("Ready rate", "Eligible rate", "Completion rate")])
        self.assertNotIn("Facts.Region", json.dumps(objects))
        self.assertNotIn("100", json.dumps(state))

    def test_pie_keeps_dimension_as_category_and_numeric_angle_as_value(self):
        _, payload = self.emit("""
            <workbook><worksheet name="Synthetic pie"><table><cols/><rows/>
              <panes><pane><mark class="Pie"/><encodings>
                <color column="[ds].[none:Region:nk]"/>
                <size column="[ds].[sum:Amount:qk]"/>
              </encodings></pane></panes>
            </table></worksheet></workbook>
        """)
        visual = payload["visual"]
        self.assertEqual(visual["visualType"], "pieChart")
        state = visual["query"]["queryState"]
        self.assertEqual(state["Category"]["projections"][0]["field"], {"Column": {
            "Expression": {"SourceRef": {"Entity": "Facts"}}, "Property": "Region"}})
        self.assertEqual(state["Y"]["projections"][0]["field"], {"Aggregation": {
            "Expression": {"Column": {"Expression": {"SourceRef": {"Entity": "Facts"}},
                                      "Property": "Amount"}}, "Function": 0}})

    def test_combined_axis_panes_retain_explicit_marks_and_shelf_roles_not_pane_order(self):
        worksheet, payload = self.emit("""
            <workbook><worksheet name="Synthetic overlay"><table>
              <cols>[ds].[none:Region:nk]</cols>
              <rows>([ds].[sum:Amount:qk] + [ds].[usr:Ratio:qk])</rows>
              <panes>
                <pane id="all"><mark class="Automatic"/></pane>
                <pane id="line-first-in-xml" y-axis-name="[ds].[usr:Ratio:qk]">
                  <mark class="Line"/>
                </pane>
                <pane id="bar-second-in-xml" y-axis-name="[ds].[sum:Amount:qk]">
                  <mark class="Bar"/>
                </pane>
              </panes>
            </table></worksheet></workbook>
        """)
        self.assertEqual(worksheet["original_mark_class"], "Line")
        self.assertTrue(worksheet["dual_axis"]["enabled"])
        panes = worksheet["dual_axis"]["panes"]
        self.assertEqual([(p["field"], p["axis_role"], p["mark_class"]) for p in panes],
                         [("Amount", "Y", "Bar"), ("Ratio", "Y2", "Line")])
        self.assertEqual(payload["visual"]["visualType"], "lineClusteredColumnComboChart")
        state = payload["visual"]["query"]["queryState"]
        self.assertEqual(state["Y"]["projections"][0]["field"]["Aggregation"]
                         ["Expression"]["Column"]["Property"], "Amount")
        self.assertEqual(state["Y2"]["projections"][0]["field"]["Measure"]["Property"], "Ratio")

    def test_packaged_image_existence_controls_missing_resource_disclosure(self):
        from tableau_export.dashboard_extractor import extract_dashboard_objects
        dashboard = ET.fromstring("""
            <dashboard><zones>
              <zone id="1" type-v2="bitmap" param="Images/present.png" x="0" y="0" w="100" h="100"/>
              <zone id="2" type-v2="bitmap" param="Images/missing.png" x="100" y="0" w="100" h="100"/>
            </zones></dashboard>
        """)
        package = Path(self.root, "synthetic.twbx")
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Workbook/synthetic.twb", "<workbook/>")
            archive.writestr("Workbook/Images/present.png", b"synthetic resource bytes")
        objects = extract_dashboard_objects(
            dashboard, workbook_path=str(package), archive_member="Workbook/synthetic.twb")
        by_source = {obj["source"]: obj for obj in objects if obj["type"] == "image"}
        self.assertNotIn("resource_missing", by_source["Images/present.png"])
        self.assertIs(by_source["Images/missing.png"]["resource_missing"], True)
        for source, expected in (("Images/present.png", "image"), ("Images/missing.png", "textbox")):
            with self.subTest(source=source):
                directory = Path(self.root, expected)
                self.generator._create_visual_image(
                    str(directory), by_source[source], 1, 1, 0)
                paths = list(directory.rglob("visual.json"))
                self.assertEqual(len(paths), 1)
                payload = json.loads(paths[0].read_text(encoding="utf-8"))
                self.assertEqual(payload["visual"]["visualType"], expected)
                if expected == "textbox":
                    self.assertIn("Source image unavailable", json.dumps(payload))
                    self.assertTrue(any(
                        annotation["name"] == "MigrationNote"
                        for annotation in payload["annotations"]))
                else:
                    self.assertNotIn("Source image unavailable", json.dumps(payload))

    def test_date_range_slicer_uses_actual_model_type_not_date_caption_heuristic(self):
        # No 'date' in the caption: model type and continuous pill control mode.
        self.generator._field_map["Moment"] = ("Facts", "Event Date")
        mode = self.generator._detect_slicer_mode(
            {"param": "[ds].[none:Moment:qk]"}, "Moment", {})
        self.assertEqual(mode, "Between")
        slicer = self.generator._create_slicer_visual(
            "synthetic", 0, 0, 200, 80, "Event Date", "Facts", 0, slicer_mode=mode)
        serialized = json.loads(json.dumps(slicer))
        self.assertEqual(serialized["visual"]["objects"]["data"][0]["properties"]["mode"],
                         {"expr": {"Literal": {"Value": "'Between'"}}})
        self.assertEqual(serialized["visual"]["query"]["queryState"]["Values"]
                         ["projections"][0]["field"], {"Column": {
                             "Expression": {"SourceRef": {"Entity": "Facts"}},
                             "Property": "Event Date"}})

    def test_physical_date_lineage_wins_over_duplicate_model_schema_in_combo(self):
        self.generator._field_map["DATE"] = ("WrongCopy", "DATE")
        self.generator._actual_bim_symbols.update({
            ("Extract", "DATE"), ("WrongCopy", "DATE")})
        self.generator._actual_bim_column_types.update({
            ("Extract", "DATE"): "datetime", ("WrongCopy", "DATE"): "datetime"})
        self.generator._datasources_ref = [{
            "name": "ds", "tables": [
                {"name": table, "columns": [{"name": "DATE", "datatype": "date"}],
                 "connection": {"type": "CSV"}}
                for table in ("WrongCopy", "Extract")],
            "col_local_name_map": {"DATE": "Extract"},
        }]
        self.generator._datasources_ref[0]["tables"].append({
            "name": "Facts", "columns": [
                {"name": "Amount", "datatype": "real"},
                {"name": "Ratio", "datatype": "real"}]})
        self.assertEqual(self.generator._resolve_field_entity(
            "DATE", datasource="ds"), ("Extract", "DATE"))
        self.assertEqual(self.generator._resolve_field_entity(
            "Caption", datasource="ds", source_table="Extract", source_column="DATE"),
            ("Extract", "DATE"))
        # Missing declared lineage must not silently substitute WrongCopy.DATE.
        self.assertEqual(self.generator._resolve_field_entity(
            "DATE", datasource="ds", source_table="Absent", source_column="DATE"),
            ("Absent", "DATE"))
        worksheet, payload = self.emit("""
            <workbook>
              <datasources><datasource name="ds"><metadata-records>
                <metadata-record class="column">
                  <local-name>[DATE]</local-name><remote-name>DATE</remote-name>
                  <parent-name>[Extract]</parent-name><local-type>date</local-type>
                </metadata-record>
              </metadata-records></datasource></datasources>
              <worksheet name="Lineage overlay"><table>
              <cols>[ds].[none:DATE:qk]</cols>
              <rows>([ds].[sum:Amount:qk] + [ds].[usr:Ratio:qk])</rows>
              <view><datasource-dependencies datasource="ds">
                <column name="[DATE]" datatype="date" role="dimension">
                  <desc><string>&lt;physical&gt;</string></desc>
                </column>
                <metadata-records><metadata-record class="column">
                  <local-name>[DATE]</local-name><remote-name>DATE</remote-name>
                  <parent-name>[Extract]</parent-name><local-type>date</local-type>
                </metadata-record></metadata-records>
              </datasource-dependencies></view>
              <panes>
                <pane id="bar" y-axis-name="[ds].[sum:Amount:qk]"><mark class="Bar"/></pane>
                <pane id="line" y-axis-name="[ds].[usr:Ratio:qk]"><mark class="Line"/></pane>
              </panes>
            </table></worksheet></workbook>
        """)
        date = next(field for field in worksheet["fields"] if field["name"] == "DATE")
        self.assertEqual(date["source_table"], "Extract")
        self.assertEqual(date["source_column"], "DATE")
        self.assertEqual(date["datatype"], "date")
        self.assertEqual(date["role"], "dimension")
        state = payload["visual"]["query"]["queryState"]
        self.assertEqual(state["Category"]["projections"][0]["field"], {"Column": {
            "Expression": {"SourceRef": {"Entity": "Extract"}}, "Property": "DATE"}})
        self.assertEqual(state["Y"]["projections"][0]["field"]["Aggregation"]
                         ["Expression"]["Column"]["Property"], "Amount")
        self.assertEqual(state["Y2"]["projections"][0]["field"]["Measure"]["Property"], "Ratio")

    def test_named_palette_is_not_gradient_and_category_swatches_have_identity_selectors(self):
        column = {"Column": {"Expression": {"SourceRef": {"Entity": "Facts"}},
                             "Property": "Region"}}
        state = {"Series": {"projections": [{"field": column, "queryRef": "Facts.Region"}]}}
        encoding = {"type": "categorical", "field": "Region", "palette": "named",
                    "palette_colors": ["#fe5716", "#abcdef"],
                    "color_values": {"North": "#fe5716", "O'Brien": "#abcdef"}}
        objects = {}
        self.generator._build_color_encoding_objects(objects, {}, "barChart", {
            "color": encoding}, state)
        self.assertEqual(len(objects["dataPoint"]), 2)
        for item, value, color in zip(objects["dataPoint"],
                                      ("'North'", "'O''Brien'"),
                                      ("'#fe5716'", "'#abcdef'")):
            comparison = item["selector"]["data"][0]["scopeId"]["Comparison"]
            self.assertEqual(comparison, {
                "ComparisonKind": 0, "Left": column,
                "Right": {"Literal": {"Value": value}}})
            self.assertEqual(item["properties"]["fill"]["solid"]["color"],
                             {"expr": {"Literal": {"Value": color}}})
            self.assertNotIn("fillRule", item["properties"])
        self.assertEqual(build_categorical_color_objects(
            encoding, {"Tooltips": state["Series"]}), [])


if __name__ == "__main__":
    unittest.main()
