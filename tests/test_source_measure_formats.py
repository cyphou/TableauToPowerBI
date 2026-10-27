"""Source percentage-mask propagation: metadata only, explicit bindings only."""

import copy
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from powerbi_import.tmdl_generator import _apply_source_measure_formats, generate_tmdl
from tests.test_audit_source_to_output import _datasource


class TestSourceMeasureFormats(unittest.TestCase):
    def fixture(self):
        model = {"model": {"tables": [{
            "name": table, "columns": [{"name": "Date", "dataType": "dateTime",
                                       "formatString": "dd/mm/yyyy"}],
            "measures": [{"name": name, "expression": "DIVIDE(1, 4)",
                          "formatString": "0" if name == "Count" else "#,0.00"}
                         for name in ("Rate A", "Rate B", "Rate C", "Count")],
        } for table in ("Facts A", "Facts B")]}}
        datasources = [{
            "name": source,
            "calculations": [{"name": f"[{name}]", "caption": name, "datatype": "real"}
                             for name in ("Rate A", "Rate B", "Rate C", "Count")],
        } for source in ("dsA", "dsB")]
        context = {"table_datasource_set": {"Facts A": {"dsA"}, "Facts B": {"dsB"}}}
        return model, datasources, context

    def worksheet(self, mask="p0.0%", chart="columnChart", source="dsA"):
        return {
            "chart_type": chart,
            "fields": [{"name": "Multiple Values", "shelf": "rows", "datasource": source}]
            + [{"name": name, "shelf": "measure_value", "datasource": source}
               for name in ("Rate A", "Rate B", "Rate C")],
            "formatting": {"field_formats": {
                f"[{source}].[Multiple Values]": mask}},
        }

    def masks(self, model, table="Facts A"):
        owner = next(t for t in model["model"]["tables"] if t["name"] == table)
        return {m["name"]: m["formatString"] for m in owner["measures"]}

    def test_typed_multiple_values_recovers_font_overwrite_and_respects_datasource_scope(self):
        model, sources, context = self.fixture()
        before = copy.deepcopy(model)
        worksheet = self.worksheet("7")
        worksheet["formatting"]["cell"] = {
            "field": "[dsA].[Multiple Values]", "text-format": "p0.0%"}
        _apply_source_measure_formats(model, sources, {"worksheets": [worksheet]}, context)
        self.assertEqual(self.masks(model), {
            "Rate A": "0.0%", "Rate B": "0.0%", "Rate C": "0.0%", "Count": "0"})
        self.assertEqual(self.masks(model, "Facts B"), self.masks(before, "Facts B"))
        for actual, original in zip(model["model"]["tables"], before["model"]["tables"]):
            self.assertEqual(actual["columns"], original["columns"])
            self.assertEqual([m["expression"] for m in actual["measures"]],
                             [m["expression"] for m in original["measures"]])

    def test_individual_measure_overrides_synthetic_shelf_default(self):
        model, sources, context = self.fixture()
        worksheet = self.worksheet()
        worksheet["formatting"]["field_formats"]["[dsA].[Rate B]"] = "p0.00%"
        _apply_source_measure_formats(model, sources, {"worksheets": [worksheet]}, context)
        self.assertEqual(self.masks(model), {
            "Rate A": "0.0%", "Rate B": "0.00%", "Rate C": "0.0%", "Count": "0"})

    def test_axis_over_table_precision_and_measure_default_over_axis(self):
        model, sources, context = self.fixture()
        sources[0]["calculations"][0]["default_format"] = "p0.000%"
        _apply_source_measure_formats(model, sources, {"worksheets": [
            self.worksheet("p0.00%", "tableEx"), self.worksheet("p0.0%")]}, context)
        self.assertEqual(self.masks(model), {
            "Rate A": "0.000%", "Rate B": "0.0%", "Rate C": "0.0%", "Count": "0"})

    def test_equal_priority_conflict_retains_existing_format_independent_of_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                model, sources, context = self.fixture()
                worksheets = [self.worksheet("p0.0%"), self.worksheet("p0.00%")]
                if reverse:
                    worksheets.reverse()
                original = copy.deepcopy(model)
                _apply_source_measure_formats(model, sources, {"worksheets": worksheets}, context)
                self.assertEqual(model, original)

    def test_unscoped_cell_noise_and_nonpercentage_masks_do_not_guess_percentages(self):
        for mask in ("7", "Arial", "dd/mm/yyyy", "#,0.00", ""):
            with self.subTest(mask=mask):
                model, sources, context = self.fixture()
                original = copy.deepcopy(model)
                worksheet = self.worksheet(mask)
                worksheet["formatting"]["cell"] = {"text-format": "p0.0%"}
                _apply_source_measure_formats(model, sources, {"worksheets": [worksheet]}, context)
                self.assertEqual(model, original)

    def test_actual_three_rate_tmdl_measures_receive_masks_but_count_and_date_do_not(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, "Data", "synthetic.csv")
            source.parent.mkdir()
            source.write_text("Entity,Amount,Units,Event Date\nA,1,4,2026-01-01\n",
                              encoding="utf-8")
            datasource = _datasource(source)
            datasource["tables"][0]["columns"].append(
                {"name": "Event Date", "datatype": "date"})
            datasource["calculations"] = [
                {"name": f"[Rate {name}]", "caption": f"Rate {name}",
                 "role": "measure", "datatype": "real",
                 "formula": "SUM([Amount]) / SUM([Units])"} for name in ("A", "B", "C")]
            datasource["calculations"].append({
                "name": "[Count]", "caption": "Count", "role": "measure",
                "datatype": "integer", "formula": "COUNT([Units])"})
            worksheet = self.worksheet("7", source="Synthetic")
            worksheet["formatting"]["cell"] = {
                "field": "[Synthetic].[Multiple Values]", "text-format": "p0.0%"}
            with redirect_stdout(io.StringIO()):
                generate_tmdl([datasource], "Synthetic",
                              {"worksheets": []}, output_dir=str(Path(root, "baseline")))
                generate_tmdl([datasource], "Synthetic",
                              {"worksheets": [worksheet]}, output_dir=root)
            content = Path(root, "definition", "tables", "Facts.tmdl").read_text(encoding="utf-8")
            baseline = Path(root, "baseline", "definition", "tables", "Facts.tmdl").read_text(
                encoding="utf-8")
        for name in ("Rate A", "Rate B", "Rate C"):
            block = content.split(f"\tmeasure '{name}' =", 1)[1].split("\n\tmeasure ", 1)[0]
            self.assertIn("\t\tformatString: 0.0%\n", block)
            self.assertIn("DIVIDE(SUM('Facts'[Amount]), SUM('Facts'[Units]))", block)
        def metadata(text, declaration, terminator):
            block = text.split(declaration, 1)[1].split(terminator, 1)[0]
            block = block.split("\n\tpartition ", 1)[0]
            return [line for line in block.splitlines() if "lineageTag:" not in line]

        self.assertEqual(metadata(content, "\tmeasure Count =", "\n\tmeasure "),
                         metadata(baseline, "\tmeasure Count =", "\n\tmeasure "))
        date = content.split("\tcolumn 'Event Date'", 1)[1].split("\n\tcolumn ", 1)[0]
        self.assertIn("dataType: dateTime", date)
        self.assertNotIn("formatString: 0.0%", date)
        self.assertEqual(metadata(content, "\tcolumn 'Event Date'", "\n\tcolumn "),
                         metadata(baseline, "\tcolumn 'Event Date'", "\n\tcolumn "))


if __name__ == "__main__":
    unittest.main()
