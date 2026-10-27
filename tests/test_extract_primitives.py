"""Synthetic interval-bin regressions, including the generated import partition.

An integer bin over fractional Energy values must not become an identity
AddColumn typed Int64 (the original refresh failure reported error 131699).
No workbook, source data, or engine installation is needed by these tests.
"""

import copy
import io
import json
import os
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "powerbi_import"))
sys.path.insert(0, os.path.join(ROOT, "tableau_export"))

from datasource_extractor import (
    extract_calculations,
    extract_column_metadata,
    extract_datasource,
)
from extract_primitives import _extract_calculation_formula
from extract_tableau_data import TableauExtractor

from powerbi_import.tmdl_generator import _build_semantic_model


def _bin_column(size="27", peg="0", datatype="integer", formula="[Energy]"):
    column = ET.Element("column", {
        "name": "[Energy Bin]", "caption": "Energy Bin",
        "datatype": datatype, "role": "dimension", "type": "quantitative",
    })
    attrs = {"class": "bin", "formula": formula}
    if size is not None:
        attrs["size"] = size
    if peg is not None:
        attrs["peg"] = peg
    ET.SubElement(column, "calculation", attrs)
    return column


def _datasource(column):
    """One physical real-valued CSV column, one synthetic derived column."""
    datasource = ET.fromstring("""
        <datasource name="EnergySource">
          <connection class="textscan" filename="energy.csv" directory="Data">
            <relation name="EnergyData" table="[energy.csv]" type="table">
              <columns><column name="Energy" datatype="real" ordinal="0"/></columns>
            </relation>
          </connection>
          <column name="[Energy]" datatype="real"
                  role="measure" type="quantitative"/>
        </datasource>
    """)
    datasource.append(copy.deepcopy(column))
    return datasource


def _workbook(column, worksheet_only=False):
    root = ET.Element("workbook")
    datasources = ET.SubElement(root, "datasources")
    datasource = _datasource(column)
    if worksheet_only:
        datasource.remove(datasource.find("column[@name='[Energy Bin]']"))
    datasources.append(datasource)
    worksheets = ET.SubElement(root, "worksheets")
    worksheet = ET.SubElement(worksheets, "worksheet", {"name": "Energy Sheet"})
    table = ET.SubElement(worksheet, "table")
    dependency = ET.SubElement(table, "datasource-dependencies", {
        "datasource": "EnergySource",
    })
    dependency.append(copy.deepcopy(column))
    return root


class TestIntervalBinFormula(unittest.TestCase):
    def test_critical_integer_bin_is_interval_not_identity(self):
        calc = _bin_column().find("calculation")
        self.assertEqual(
            _extract_calculation_formula(calc, "[Energy Bin]"),
            "FLOOR((([Energy]) - (0)) / (27)) * (27) + (0)",
        )

    def test_formula_shape_preserves_fractional_negative_and_shifted_intervals(self):
        # Assert the emitted expression, not a test-only evaluator. FLOOR is
        # essential for negatives: truncating -0.25 / 27 would give 0, not -27.
        cases = [
            ("27", "0", "[Energy]",
             "FLOOR((([Energy]) - (0)) / (27)) * (27) + (0)"),
            ("2.5", "0.5", "[Energy]",
             "FLOOR((([Energy]) - (0.5)) / (2.5)) * (2.5) + (0.5)"),
            ("0.25", "-1.5", "[Energy]",
             "FLOOR((([Energy]) - (-1.5)) / (0.25)) * (0.25) + (-1.5)"),
            ("27", "0", "54.5",
             "FLOOR(((54.5) - (0)) / (27)) * (27) + (0)"),
            ("27", "0", "-0.25",
             "FLOOR(((-0.25) - (0)) / (27)) * (27) + (0)"),
            (" 2.7e1 ", "+1.5", "[Energy] + 0.25",
             "FLOOR((([Energy] + 0.25) - (1.5)) / (27)) * (27) + (1.5)"),
        ]
        for size, peg, value, expected in cases:
            with self.subTest(size=size, peg=peg, value=value):
                calc = _bin_column(size, peg, formula=value).find("calculation")
                self.assertEqual(_extract_calculation_formula(calc), expected)

    def test_only_absent_peg_defaults_to_zero(self):
        calc = _bin_column(peg=None).find("calculation")
        self.assertEqual(
            _extract_calculation_formula(calc),
            "FLOOR((([Energy]) - (0)) / (27)) * (27) + (0)",
        )

    def test_non_bin_calculations_are_unchanged(self):
        for attrs in (
            {"formula": "[Energy] * 2"},
            {"class": "tableau", "formula": "SUM([Energy])"},
            {"class": "categorical-bin"},
        ):
            with self.subTest(attrs=attrs):
                self.assertEqual(
                    _extract_calculation_formula(ET.Element("calculation", attrs)),
                    attrs.get("formula", ""),
                )


class _SyntheticExtractorTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.extractor = TableauExtractor(
            os.path.join(directory.name, "synthetic.twb"),
            output_dir=os.path.join(directory.name, "metadata"),
        )

    def _extract(self, root):
        with redirect_stdout(io.StringIO()):
            self.extractor.extract_datasources(root)
            self.extractor.extract_calculations(root)
            self.extractor.extract_bins(root)
        return self.extractor.workbook_data


class TestIntervalBinExtraction(_SyntheticExtractorTestCase):
    def test_bin_keeps_original_field_id_and_physical_input_provenance_deterministically(self):
        root = _workbook(_bin_column("2.5", "-0.5", "real"))
        bin_id = "[Calculation_123456789012345678]"
        for column in root.findall(".//column[@name='[Energy Bin]']"):
            column.set("name", bin_id)
            column.set("caption", "Energy interval")
        datasource = root.find("./datasources/datasource")
        record = ET.SubElement(
            ET.SubElement(datasource, "metadata-records"),
            "metadata-record", {"class": "column"})
        for tag, value in (
            ("local-name", "[Energy]"), ("parent-name", "[EnergyData]"),
            ("remote-name", "energy_kwh"), ("local-type", "real"),
        ):
            ET.SubElement(record, tag).text = value
        table = root.find("./worksheets/worksheet/table")
        ET.SubElement(table, "cols").text = (
            f"[EnergySource].[none:{bin_id.strip('[]')}:qk]")
        ET.SubElement(table, "rows").text = "[EnergySource].[sum:Energy:qk]"
        before = ET.tostring(root)

        outputs = []
        for _ in range(2):
            # Independent extractors guard against cache/order-dependent IDs.
            with tempfile.TemporaryDirectory() as directory:
                extractor = TableauExtractor(
                    os.path.join(directory, "synthetic.twb"), output_dir=directory)
                with redirect_stdout(io.StringIO()):
                    extractor.extract_datasources(root)
                    extractor.extract_calculations(root)
                    extractor.extract_bins(root)
                    extractor.extract_worksheets(root)
                outputs.append(extractor.workbook_data)

        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(ET.tostring(root), before)
        data = outputs[0]
        self.assertEqual(len(data["calculations"]), 1)
        calculation = data["calculations"][0]
        self.assertEqual(calculation["name"], bin_id)
        self.assertEqual(calculation["caption"], "Energy interval")
        self.assertEqual(calculation["class"], "bin")
        self.assertEqual(calculation["datatype"], "real")
        self.assertEqual(calculation["formula"],
                         "FLOOR((([Energy]) - (-0.5)) / (2.5)) * (2.5) + (-0.5)")
        self.assertEqual(data["datasources"][0]["calculations"], [calculation])
        self.assertEqual(data["bins"], [])
        fields = data["worksheets"][0]["fields"]
        self.assertEqual([field["name"] for field in fields],
                         [bin_id.strip("[]"), "Energy"])
        self.assertEqual(fields[0]["datasource"], "EnergySource")
        self.assertEqual(fields[0]["datatype"], "real")
        self.assertEqual(fields[0]["role"], "dimension")
        self.assertEqual(fields[1]["source_table"], "EnergyData")
        self.assertEqual(fields[1]["source_column"], "energy_kwh")

    def test_datasource_calculation_and_column_metadata_share_normalization(self):
        column = _bin_column()
        datasource = _datasource(column)
        expected = "FLOOR((([Energy]) - (0)) / (27)) * (27) + (0)"
        calculations = extract_calculations(datasource)
        self.assertEqual(len(calculations), 1)
        self.assertEqual(calculations[0]["formula"], expected)
        self.assertEqual(calculations[0]["datatype"], "integer")
        self.assertEqual(calculations[0]["class"], "bin")
        metadata = next(c for c in extract_column_metadata(datasource)
                        if c["name"] == "[Energy Bin]")
        self.assertEqual(metadata["datatype"], "integer")
        self.assertEqual(metadata["calculation"], {"class": "bin", "formula": expected})
        full = extract_datasource(datasource)
        self.assertEqual(full["calculations"][0]["formula"], expected)
        self.assertEqual(next(c for c in full["columns"]
                              if c["name"] == "[Energy Bin]"), metadata)

    def test_worksheet_only_bin_is_normalized_and_injected_into_datasource(self):
        data = self._extract(_workbook(_bin_column("2.5", "-0.5", "real"),
                                      worksheet_only=True))
        self.assertEqual(len(data["calculations"]), 1)
        calculation = data["calculations"][0]
        self.assertEqual(calculation["formula"],
                         "FLOOR((([Energy]) - (-0.5)) / (2.5)) * (2.5) + (-0.5)")
        self.assertEqual(calculation["datatype"], "real")
        self.assertEqual(calculation["class"], "bin")
        self.assertEqual(data["datasources"][0]["calculations"], [calculation])
        self.assertEqual(data["bins"], [])

    def test_repeated_datasource_and_worksheet_bin_is_not_duplicated(self):
        root = _workbook(_bin_column())
        worksheets = root.find("worksheets")
        worksheets.append(copy.deepcopy(worksheets[0]))
        # Some files also contain the older representation on the same column.
        column = root.find(".//datasources/datasource/column[@name='[Energy Bin]']")
        ET.SubElement(column, "bin", {"source": "[Energy]", "size": "27"})
        data = self._extract(root)
        self.assertEqual(len(data["calculations"]), 1)
        self.assertEqual(len(data["datasources"][0]["calculations"]), 1)
        self.assertEqual(data["bins"], [])

    def test_legacy_bin_payload_is_unchanged(self):
        root = _workbook(_bin_column())
        ds = root.find("./datasources/datasource")
        for name, attrs in (
            ("Legacy Energy", {"source": "[Energy]", "size": "5"}),
            ("Default Energy", {"source": "[Energy]"}),
        ):
            col = ET.SubElement(ds, "column", {
                "name": f"[{name}]", "datatype": "integer",
            })
            ET.SubElement(col, "bin", attrs)
        data = self._extract(root)
        self.assertEqual(data["bins"], [
            {"name": "Legacy Energy", "source_field": "Energy",
             "size": "5", "datatype": "integer"},
            {"name": "Default Energy", "source_field": "Energy",
             "size": "10", "datatype": "integer"},
        ])
        self.assertEqual(len(data["calculations"]), 1)

    def test_invalid_declarations_raise_logged_errors_on_every_extraction_path(self):
        invalid = [
            ("size", None), ("size", ""), ("size", " "),
            ("size", "NaN"), ("size", "Infinity"), ("size", "-Infinity"),
            ("size", "0"), ("size", "-0"), ("size", "-27"),
            ("size", "-0.25"), ("size", "not-a-number"),
            ("size", "27) + [Energy]"),
            ("peg", ""), ("peg", " "), ("peg", "NaN"),
            ("peg", "Infinity"), ("peg", "-Infinity"),
            ("peg", "invalid"), ("peg", "0) + [Energy]"),
            ("formula", ""), ("formula", " "),
        ]
        for attr, value in invalid:
            column = _bin_column()
            calc = column.find("calculation")
            if value is None:
                del calc.attrib[attr]
            else:
                calc.set(attr, value)
            ds = _datasource(column)
            paths = {
                "helper": lambda: _extract_calculation_formula(calc, "[Energy Bin]"),
                "calculations": lambda: extract_calculations(ds),
                "metadata": lambda: extract_column_metadata(ds),
                "datasource": lambda: extract_datasource(ds),
                "worksheet": lambda: self._extract(_workbook(column, worksheet_only=True)),
            }
            for path, extract in paths.items():
                with self.subTest(attribute=attr, value=value, path=path):
                    # Reset state so a prior valid extraction cannot mask an error.
                    self.extractor.workbook_data = {}
                    self.extractor._clear_xml_node_cache()
                    with self.assertLogs(level="WARNING") as logs:
                        with self.assertRaisesRegex(
                            ValueError, r"Invalid bin calculation.*Energy Bin"
                        ):
                            extract()
                    self.assertTrue(any("Invalid bin calculation" in message
                                        and "Energy Bin" in message
                                        and attr in message for message in logs.output))


class TestIntervalBinSemanticModel(_SyntheticExtractorTestCase):
    def _assert_model_bin(self, datatype, size, peg, worksheet_only=False):
        data = self._extract(_workbook(_bin_column(size, peg, datatype), worksheet_only))
        # Exercise the JSON extraction/import boundary as well as the real builder.
        data = json.loads(json.dumps(data))
        with redirect_stdout(io.StringIO()):
            model = _build_semantic_model(
                data["datasources"], "SyntheticBins", {"bins": data["bins"]},
            )
        tables = [t for t in model["model"]["tables"] if t["name"] == "EnergyData"]
        self.assertEqual(len(tables), 1)
        table = tables[0]
        bins = [c for c in table["columns"] if c["name"] == "Energy Bin"]
        self.assertEqual(len(bins), 1)
        column = bins[0]
        self.assertEqual(column["dataType"], "Int64" if datatype == "integer" else "Double")
        partition = table["partitions"][0]["source"]
        self.assertEqual(partition["type"], "m")
        expression = partition["expression"]
        steps = re.findall(
            r'Table\.AddColumn\([^\n]*"Energy Bin",[^\n]*', expression,
        )
        self.assertEqual(len(steps), 1, expression)
        step = steps[0]
        self.assertIn("Number.RoundDown", step)
        self.assertIn("[Energy]", step)
        # Guard against the old `each [Energy], Int64.Type` refresh failure.
        self.assertNotRegex(step, r'each\s+\[Energy\]\s*,')
        self.assertIn(f"- ({peg})", step)
        self.assertIn(f"/ ({size})", step)
        self.assertIn(f"* ({size})", step)
        self.assertIn(f"+ ({peg})", step)
        self.assertIn("Int64.Type" if datatype == "integer" else "type number", step)
        if datatype == "real":
            self.assertNotIn("Int64.Type", step)
        self.assertEqual(column.get("sourceColumn"), "Energy Bin",
                         "The bin must be materialized in M, not silently left as DAX")
        self.assertNotIn("expression", column)

    def test_integer_bin_materializes_floor_in_actual_partition_m(self):
        self._assert_model_bin("integer", "27", "0")

    def test_fractional_real_bin_materializes_numeric_m_not_integer_identity(self):
        self._assert_model_bin("real", "2.5", "0.5")

    def test_worksheet_bin_with_negative_peg_materializes_once_in_m(self):
        self._assert_model_bin("real", "0.25", "-1.5", worksheet_only=True)
