"""Synthetic loose-file refresh bindings; no customer data or native runtime."""

import copy
import io
import json
import os
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from contextlib import redirect_stdout
from unittest import mock

import migrate
from powerbi_import.m_validator import validate_m_query
from powerbi_import.tmdl_generator import generate_tmdl
from tableau_export import datasource_extractor
from tableau_export.extract_tableau_data import TableauExtractor
from tableau_export.m_query_builder import _file_contents, generate_power_query_m
from tests.native_import_test_helpers import forbid_native_payload_io

_NATIVE_FILE_CASES = (
    ("textscan", "CSV", "csv", "[source#csv]", "Csv.Document"),
    ("excel-direct", "Excel", "xlsx", "[Exact$]", "Excel.Workbook"),
    ("ogrdirect", "GeoJSON", "geojson", "[Features]", "Json.Document"),
    ("json", "JSON", "json", "[Records]", "Json.Document"),
    ("xml", "XML", "xml", "[Records]", "Xml.Tables"),
    ("pdf", "PDF", "pdf", "[Table001]", "Pdf.Tables"),
)
_CSV_ATTRIBUTES = {
    "class": "textscan", "directory": "Data", "filename": "source.csv",
    # Header sniffing is existing extraction behavior, not part of this fix.
    "separator": ",", "charset": "utf-8",
}
_FILE_CONTENTS_LITERAL = re.compile(r'File\.Contents\("(?:[^"]|"")*"\)')


class TestNativeCsvColumnWidth(unittest.TestCase):
    def test_native_model_subset_does_not_limit_physical_csv_width(self):
        connection = {
            "type": "CSV",
            "details": {
                "filename": "source.csv", "directory": "Data",
                "_source_base_dir": r"C:\Synthetic\Workbook",
            },
        }
        table = {
            "name": "Facts",
            "columns": [
                {"name": "ID", "datatype": "integer"},
                {"name": "Trailing Field", "datatype": "string"},
            ],
        }
        query = generate_power_query_m(
            connection, table, use_original_file_paths=True)
        self.assertNotRegex(query, r"\bColumns\s*=")
        selected = (
            '#"Selected Columns" = Table.SelectColumns('
            '#"Promoted Headers", {"ID", "Trailing Field"})')
        self.assertIn(selected, query)
        self.assertLess(query.index(selected), query.index("Table.TransformColumnTypes"))
        self.assertIn('Table.TransformColumnTypes(#"Selected Columns",', query)
        self.assertNotIn("MissingField.Ignore", query)
        self.assertIn('{"ID", Int64.Type}', query)
        self.assertIn('{"Trailing Field", type text}', query)
        self.assertIn(
            r'File.Contents("C:\Synthetic\Workbook\Data\source.csv")', query)
        self.assertNotIn("DataFolder", query)

    def test_staged_csv_retains_its_explicit_column_count(self):
        query = generate_power_query_m(
            {"type": "CSV", "details": {"filename": "source.csv"}},
            {"name": "Facts", "columns": [
                {"name": "ID", "datatype": "integer"},
                {"name": "Amount", "datatype": "real"},
            ]},
        )
        self.assertIn("Columns=2,", query)
        self.assertIn(r'File.Contents(DataFolder & "\source.csv")', query)
        self.assertNotIn("Table.SelectColumns", query)


class TestNativeSpatialSourceFormat(unittest.TestCase):
    def test_native_shapefile_has_actionable_error_instead_of_json_parser(self):
        connection = {
            "type": "GeoJSON",
            "details": {"filename": "areas.SHP", "_source_base_dir": r"C:\Source"},
        }
        table = {"name": "Areas", "columns": [{"name": "ID", "datatype": "integer"}]}
        with self.assertLogs("tableau_export.m_query_builder", level="WARNING"):
            native = generate_power_query_m(
                connection, table, use_original_file_paths=True)
        self.assertIn('error Error.Record("SourceBinding.Error"', native)
        self.assertIn("Native shapefile is not GeoJSON", native)
        self.assertIn("Configure a supported GeoJSON source in Power Query", native)
        self.assertNotIn("Json.Document", native)
        self.assertNotIn("File.Contents", native)
        staged = generate_power_query_m(connection, table)
        self.assertIn("Json.Document", staged)
        self.assertIn("DataFolder", staged)


def _build_refresh_binding_datasource(bindings, *, named=True):
    """Build tiny native relations with explicit schema, never inferred rows."""
    datasource = ET.Element("datasource", name="SyntheticNative")
    if named:
        connection = ET.SubElement(datasource, "connection", {"class": "federated"})
        named_connections = ET.SubElement(connection, "named-connections")
    else:
        assert len(bindings) == 1, "A direct fixture has exactly one connection"
        connection = ET.SubElement(datasource, "connection", dict(bindings[0][1]))
    for index, (table_name, attributes, source_table) in enumerate(bindings, 1):
        relation_attributes = {
            "type": "table", "name": table_name, "table": source_table,
        }
        if named:
            reference = f"native-{index}"
            wrapper = ET.SubElement(named_connections, "named-connection", {
                "name": reference, "caption": table_name,
            })
            ET.SubElement(wrapper, "connection", dict(attributes))
            relation_attributes["connection"] = reference
        relation = ET.SubElement(connection, "relation", relation_attributes)
        columns = ET.SubElement(relation, "columns")
        for name, datatype in (("ID", "integer"), ("Amount", "real")):
            ET.SubElement(columns, "column", name=name, datatype=datatype)
    return datasource


class RefreshBindingFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="refresh_binding_")
        self.addCleanup(temporary.cleanup)
        self.root = os.path.abspath(temporary.name)

    def _put(self, relative, content):
        path = os.path.normpath(os.path.join(self.root, relative))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as stream:
            stream.write(content.encode("utf-8") if isinstance(content, str) else content)
        return path

    def _source(self, datasource, relative):
        root = copy.deepcopy(datasource)
        if not relative.lower().endswith(".tds"):
            root = ET.Element("workbook")
            ET.SubElement(root, "datasources").append(copy.deepcopy(datasource))
        return self._put(relative, ET.tostring(root, encoding="utf-8", xml_declaration=True))

    def _extract_metadata(self, source, tag):
        metadata = os.path.join(self.root, "metadata", tag)
        with redirect_stdout(io.StringIO()):
            self.assertTrue(TableauExtractor(source, output_dir=metadata).extract_all())
        with open(os.path.join(metadata, "datasources.json"), encoding="utf-8") as stream:
            datasources = json.load(stream)
        self.assertEqual(len(datasources), 1)
        return datasources[0], metadata

    def _write_metadata(self, datasource, tag):
        path = self._put(
            os.path.join("metadata", tag, "datasources.json"),
            json.dumps([datasource], ensure_ascii=False),
        )
        return os.path.dirname(path)

    def _import_tmdl(self, datasource, tag, mode="import"):
        project = os.path.join(self.root, "generated", tag)
        output = os.path.join(project, "Report.SemanticModel")
        with redirect_stdout(io.StringIO()):
            stats = generate_tmdl(
                [copy.deepcopy(datasource)], "Report", None, output,
                model_mode=mode, parameterize=False,
            )
        self.assertEqual(stats["source_issues"], [])
        contents = {}
        for table in datasource["tables"]:
            path = os.path.join(output, "definition", "tables", table["name"] + ".tmdl")
            with open(path, encoding="utf-8") as stream:
                content = stream.read()
            self.assertEqual(re.findall(r"^\s*mode:\s+(\w+)\s*$", content, re.M), ["import"])
            self.assertIn("sourceColumn: ID", content)
            self.assertIn("sourceColumn: Amount", content)
            self.assertIn("dataType: int64", content)
            self.assertIn("dataType: double", content)
            contents[table["name"]] = content
        return project, contents

    def _assert_native_literal(self, text, expression):
        self.assertEqual(_FILE_CONTENTS_LITERAL.findall(text), [expression], text)
        for forbidden in (
            "DataFolder", "otherwise", "#table(", "Migration.SourceUnavailable",
            "SourceBinding.Error",
        ):
            self.assertNotIn(forbidden, text)

    def _assert_no_native_copy(self, project, filenames):
        for directory, children, files in os.walk(project):
            self.assertNotIn("Data", children, directory)
            self.assertTrue(set(files).isdisjoint(filenames), (directory, files))


class TestLooseWorkbookRefreshBinding(RefreshBindingFixture):
    def test_original_loose_twb_base_survives_json_m_and_import_tmdl_without_payload_io(self):
        payload = b"ID,Amount\n17,3.5\n"
        for named in (False, True):
            with self.subTest(named=named):
                tag = "named" if named else "direct"
                xml = _build_refresh_binding_datasource([
                    ("NativeFacts", _CSV_ATTRIBUTES, "[source#csv]"),
                ], named=named)
                before_xml = ET.tostring(xml)
                folder = os.path.join("original", "réalisé", tag)
                source = self._source(xml, os.path.join(folder, "source.twb"))
                native = self._put(os.path.join(folder, "Data", "source.csv"), payload)
                expected = f'File.Contents("{native}")'
                with forbid_native_payload_io([native]) as attempts:
                    datasource, metadata = self._extract_metadata(source, tag)
                    connection = datasource["connection"]
                    table, = datasource["tables"]
                    self.assertEqual(connection["details"]["_source_base_dir"],
                                     os.path.dirname(source))
                    self.assertEqual(connection["details"]["directory"], "Data")
                    self.assertEqual(connection["details"]["filename"], "source.csv")
                    self.assertEqual(table["connection_details"], connection)
                    if named:
                        self.assertEqual(table["connection"], "native-1")
                        self.assertEqual(datasource["connection_map"], {"native-1": connection})
                    else:
                        self.assertEqual(table["connection"], "")
                        self.assertEqual(datasource["connection_map"], {})
                    before = copy.deepcopy(datasource)
                    query = generate_power_query_m(
                        table["connection_details"], table, use_original_file_paths=True,
                    )
                    self._assert_native_literal(query, expected)
                    self.assertEqual(validate_m_query(query), [])
                    self.assertEqual(datasource, before)
                    for mode in ("import", "auto"):
                        project, contents = self._import_tmdl(datasource, tag + mode, mode)
                        self._assert_native_literal(contents["NativeFacts"], expected)
                        self._assert_no_native_copy(project, {"source.csv"})
                    with mock.patch.dict(os.environ, {"TTPBI_UPLOAD_ROOT": ""}):
                        staged = migrate._stage_declared_local_sources(
                            source, project, metadata, copy_files=False,
                        )
                    self.assertEqual(staged["copied"], 0)
                    self._assert_no_native_copy(project, {"source.csv"})
                self.assertEqual(attempts, [])
                self.assertEqual(ET.tostring(xml), before_xml)
                with open(native, "rb") as stream:
                    self.assertEqual(stream.read(), payload)

    def test_named_same_basename_connections_keep_each_table_in_its_own_directory(self):
        bindings, payloads = [], {}
        for table, folder in (("WestFacts", "west"), ("EastFacts", "east")):
            attributes = dict(_CSV_ATTRIBUTES, directory=f"Data/{folder}")
            bindings.append((table, attributes, "[source#csv]"))
            payloads[table] = self._put(
                os.path.join("original", "Data", folder, "source.csv"), b"ID,Amount\n1,2\n",
            )
        source = self._source(_build_refresh_binding_datasource(bindings),
                              os.path.join("original", "source.twb"))
        with forbid_native_payload_io(payloads.values()):
            datasource, _ = self._extract_metadata(source, "same_basename")
            self.assertEqual(set(datasource["connection_map"]), {"native-1", "native-2"})
            self.assertEqual(datasource["connection"], datasource["connection_map"]["native-1"])
            project, contents = self._import_tmdl(datasource, "same_basename")
            for table in datasource["tables"]:
                binding = datasource["connection_map"][table["connection"]]
                self.assertEqual(table["connection_details"], binding)
                self.assertEqual(binding["details"]["_source_base_dir"], os.path.dirname(source))
                self.assertEqual(binding["details"]["filename"], "source.csv")
                self._assert_native_literal(
                    contents[table["name"]], f'File.Contents("{payloads[table["name"]]}")',
                )
            self._assert_no_native_copy(project, {"source.csv"})

    def test_loose_tds_and_xml_misnamed_twbx_use_original_directory_not_output(self):
        for suffix, attributes, relation in (
            (".tds", {"class": "excel-direct", "directory": "Data/books",
                       "filename": "source.xlsx"}, "[Exact$]"),
            (".TWB", _CSV_ATTRIBUTES, "[source#csv]"),
            (".twbx", _CSV_ATTRIBUTES, "[source#csv]"),
        ):
            with self.subTest(suffix=suffix):
                tag = suffix[1:]
                xml = _build_refresh_binding_datasource([
                    ("NativeFacts", attributes, relation),
                ], named=False)
                source = self._source(xml, os.path.join("original", tag, "source" + suffix))
                native = self._put(
                    os.path.join("original", tag, attributes["directory"], attributes["filename"]),
                    b"synthetic native source must not be opened",
                )
                with forbid_native_payload_io([native]):
                    datasource, _ = self._extract_metadata(source, tag)
                    self.assertEqual(datasource["connection"]["details"]["_source_base_dir"],
                                     os.path.dirname(source))
                    self.assertEqual(datasource["connection"]["details"]["directory"],
                                     attributes["directory"])
                    self.assertEqual(datasource["tables"][0]["connection_details"],
                                     datasource["connection"])
                    project, contents = self._import_tmdl(datasource, tag)
                    self._assert_native_literal(contents["NativeFacts"],
                                                f'File.Contents("{native}")')
                    if suffix == ".tds":
                        self.assertIn('Item="Exact",Kind="Sheet"', contents["NativeFacts"])
                    self._assert_no_native_copy(project, {attributes["filename"]})

    def test_all_six_native_generators_consume_persisted_context_but_staged_mode_ignores_it(self):
        bindings, payloads = [], {}
        for xml_class, connector, extension, relation, _ in _NATIVE_FILE_CASES:
            name = "Native" + connector
            directory = "Data/" + extension
            attributes = {
                "class": xml_class, "directory": directory,
                "filename": "source." + extension,
            }
            if xml_class == "textscan":
                attributes["separator"] = ","
            bindings.append((name, attributes, relation))
            payloads[name] = self._put(
                os.path.join("original", "all_native", directory, "source." + extension),
                b"opaque synthetic native body; migration must not parse this",
            )
        source = self._source(_build_refresh_binding_datasource(bindings),
                              os.path.join("original", "all_native", "source.twb"))
        with forbid_native_payload_io(payloads.values()):
            datasource, _ = self._extract_metadata(source, "all_native")
            tables = {table["name"]: table for table in datasource["tables"]}
            self.assertEqual(set(tables), set(payloads))
            self.assertEqual(len(datasource["tables"]), len(_NATIVE_FILE_CASES))
            self.assertEqual(len(datasource["connection_map"]), len(_NATIVE_FILE_CASES))
            project, contents = self._import_tmdl(datasource, "all_native")
            for _, connector, extension, _, function in _NATIVE_FILE_CASES:
                with self.subTest(connector=connector):
                    table = tables["Native" + connector]
                    connection = table["connection_details"]
                    self.assertEqual(connection["type"], connector)
                    self.assertEqual(connection, datasource["connection_map"][table["connection"]])
                    self.assertEqual(connection["details"]["directory"], "Data/" + extension)
                    self.assertEqual(connection["details"]["filename"], "source." + extension)
                    self.assertEqual(connection["details"]["_source_base_dir"],
                                     os.path.dirname(source))
                    before = copy.deepcopy((connection, table))
                    with mock.patch("builtins.open", side_effect=AssertionError(
                            "M generation must be pure metadata consumption")) as opened, \
                            mock.patch("io.open", side_effect=AssertionError(
                                "M generation must not inspect a native file")) as io_opened:
                        native_m = generate_power_query_m(
                            connection, table, use_original_file_paths=True,
                        )
                        staged_m = generate_power_query_m(connection, table)
                        without_context = copy.deepcopy(connection)
                        without_context["details"].pop("_source_base_dir")
                        self.assertEqual(staged_m, generate_power_query_m(without_context, table))
                    opened.assert_not_called()
                    io_opened.assert_not_called()
                    expression = f'File.Contents("{payloads[table["name"]]}")'
                    self._assert_native_literal(native_m, expression)
                    self._assert_native_literal(contents[table["name"]], expression)
                    self.assertIn(function + "(" + expression, native_m)
                    self.assertIn(f'File.Contents(DataFolder & "\\source.{extension}")', staged_m)
                    self.assertIn('{"ID", Int64.Type}', native_m)
                    self.assertIn('{"Amount", type number}', native_m)
                    self.assertEqual(validate_m_query(native_m), [])
                    if connector == "Excel":
                        self.assertIn('Item="Exact",Kind="Sheet"', native_m)
                    self.assertEqual((connection, table), before)
            self._assert_no_native_copy(
                project, {"source." + case[2] for case in _NATIVE_FILE_CASES},
            )

    def test_real_packages_never_rebase_to_outer_folder_or_extract_native_payloads(self):
        for suffix, member in ((".twbx", "nested/source.twb"), (".tdsx", "source.tds")):
            with self.subTest(suffix=suffix):
                tag = suffix[1:]
                xml = _build_refresh_binding_datasource([
                    ("NativeFacts", _CSV_ATTRIBUTES, "[source#csv]"),
                ])
                xml_source = self._source(xml, os.path.join("package_xml", "source" +
                                                          os.path.splitext(member)[1]))
                with open(xml_source, "rb") as stream:
                    workbook = stream.read()
                source = os.path.join(self.root, "original", tag, "source" + suffix)
                os.makedirs(os.path.dirname(source), exist_ok=True)
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr(member, workbook)
                    archive.writestr("Data/source.csv", b"ID,Amount\n1,2\n")
                decoy = self._put(os.path.join("original", tag, "Data", "source.csv"),
                                  b"ID,Amount\n9,9\n")
                with forbid_native_payload_io([decoy], archive_members={"Data/source.csv"}):
                    datasource, metadata = self._extract_metadata(source, tag)
                    self.assertNotIn("_source_base_dir", datasource["connection"]["details"])
                    self.assertNotIn("_source_base_dir",
                                     datasource["connection_map"]["native-1"]["details"])
                    table, = datasource["tables"]
                    self.assertEqual(table["connection_details"],
                                     datasource["connection_map"]["native-1"])
                    project, contents = self._import_tmdl(datasource, tag)
                    self._assert_native_literal(contents["NativeFacts"],
                                                r'File.Contents("Data\source.csv")')
                    with mock.patch.dict(os.environ, {"TTPBI_UPLOAD_ROOT": ""}):
                        self.assertEqual(migrate._stage_declared_local_sources(
                            source, project, metadata, copy_files=False,
                        )["copied"], 0)
                    self._assert_no_native_copy(project, {"source.csv"})

    def test_context_requires_existing_supported_loose_original_not_a_zip_or_directory(self):
        xml = _build_refresh_binding_datasource([
            ("NativeFacts", _CSV_ATTRIBUTES, "[source#csv]"),
        ])
        unsupported = self._source(xml, "source.xml")
        directory = os.path.join(self.root, "directory.twb")
        os.makedirs(directory)
        disguised_zip = os.path.join(self.root, "zip_named_loose.twb")
        with zipfile.ZipFile(disguised_zip, "w") as archive:
            archive.writestr("Data/source.csv", b"must not be read")
        for source in (None, os.path.join(self.root, "missing.twb"), directory,
                       unsupported, disguised_zip):
            with self.subTest(source=source), forbid_native_payload_io(
                    archive_members={"Data/source.csv"}):
                datasource = datasource_extractor.extract_datasource(xml, twbx_path=source)
                for connection in (datasource["connection"],
                                   *datasource["connection_map"].values()):
                    self.assertNotIn("_source_base_dir", connection["details"])
                self.assertEqual(datasource["connection"]["details"]["filename"], "source.csv")
                self.assertEqual(datasource["connection"]["details"]["directory"], "Data")

    def test_unreadable_original_does_not_fabricate_context_or_rewrite_raw_metadata(self):
        xml = _build_refresh_binding_datasource([
            ("NativeFacts", _CSV_ATTRIBUTES, "[source#csv]"),
        ])
        source = self._source(xml, "original/source.twb")
        before = ET.tostring(xml)
        with mock.patch.object(datasource_extractor, "open", create=True,
                               side_effect=PermissionError("synthetic unreadable original")):
            datasource = datasource_extractor.extract_datasource(xml, twbx_path=source)
        for connection in (datasource["connection"], *datasource["connection_map"].values()):
            self.assertNotIn("_source_base_dir", connection["details"])
            self.assertEqual(connection["details"]["directory"], "Data")
            self.assertEqual(connection["details"]["filename"], "source.csv")
        self.assertEqual(ET.tostring(xml), before)

    def test_missing_dataset_does_not_prevent_metadata_only_base_context(self):
        xml = _build_refresh_binding_datasource([
            ("NativeFacts", _CSV_ATTRIBUTES, "[source#csv]"),
        ], named=False)
        source = self._source(xml, os.path.join("original", "source.twb"))
        missing = os.path.join(os.path.dirname(source), "Data", "source.csv")
        self.assertFalse(os.path.exists(missing))
        with forbid_native_payload_io([missing]):
            datasource = datasource_extractor.extract_datasource(xml, twbx_path=source)
            table, = datasource["tables"]
            self.assertEqual(table["connection_details"], datasource["connection"])
            self._assert_native_literal(generate_power_query_m(
                table["connection_details"], table, use_original_file_paths=True,
            ), f'File.Contents("{missing}")')

    def test_unusable_named_connection_is_not_mistaken_for_a_direct_table_binding(self):
        xml = _build_refresh_binding_datasource([
            ("NativeFacts", _CSV_ATTRIBUTES, "[source#csv]"),
        ])
        xml.find(".//named-connection").set("name", "")
        xml.find(".//relation").attrib.pop("connection")
        source = self._source(xml, "source.twb")
        datasource = datasource_extractor.extract_datasource(xml, twbx_path=source)
        self.assertIn("_source_base_dir", datasource["connection"]["details"])
        self.assertEqual(datasource["connection_map"], {})
        self.assertEqual(datasource["tables"][0]["connection_details"], {})


class TestNativeFileSourceBase(unittest.TestCase):
    def test_source_base_is_keyword_only_and_cannot_affect_the_staged_helper(self):
        _, contents = _file_contents(
            "Facts", "Data/source.csv", "Wrong", source_base_dir=r"C:\Workbook",
        )
        self.assertEqual(contents, r'File.Contents(DataFolder & "\source.csv")')
        with self.assertRaises(TypeError):
            _file_contents("Facts", "source.csv", "Data", True, r"C:\Workbook")

    def test_unambiguous_relative_paths_use_windows_unc_or_posix_base_without_filesystem_io(self):
        cases = (
            (r"C:\Synthetic\Workbook", "Data", "source.csv",
             r"C:\Synthetic\Workbook\Data\source.csv"),
            ("C:/Synthetic/Workbook/", "Data/sub", r"nested\source.csv",
             r"C:\Synthetic\Workbook\Data\sub\nested\source.csv"),
            ("C:\\", "Data", "source.csv", r"C:\Data\source.csv"),
            (r"\\synthetic.invalid\share\Workbook", "Data", "source.csv",
             r"\\synthetic.invalid\share\Workbook\Data\source.csv"),
            ("//synthetic.invalid/share/Workbook/", "Data", "source.csv",
             r"\\synthetic.invalid\share\Workbook\Data\source.csv"),
            ("/tmp/synthetic/Workbook", "Data/sub", r"nested\source.csv",
             "/tmp/synthetic/Workbook/Data/sub/nested/source.csv"),
            ("/tmp/synthetic/Workbook/", "Data", "source.csv",
             "/tmp/synthetic/Workbook/Data/source.csv"),
            ("/", "Data", "source.csv", "/Data/source.csv"),
            (r"C:\Synthetic\Workbook", "../sibling", "source.csv",
             r"C:\Synthetic\Workbook\..\sibling\source.csv"),
        )
        with mock.patch("builtins.open", side_effect=AssertionError("No path probing")) as opened, \
                mock.patch("io.open", side_effect=AssertionError("No native reads")) as io_opened, \
                mock.patch("os.getcwd", side_effect=AssertionError("No CWD-based binding")) as cwd:
            for base, directory, filename, expected in cases:
                with self.subTest(base=base, directory=directory, filename=filename):
                    label, contents = _file_contents(
                        "Facts", filename, directory, True, source_base_dir=base,
                    )
                    self.assertEqual(contents, f'File.Contents("{expected}")')
                    self.assertNotIn("not an absolute path", label)
        opened.assert_not_called()
        io_opened.assert_not_called()
        cwd.assert_not_called()

    def test_absolute_filenames_and_directories_are_not_rebased_or_replaced(self):
        cases = (
            (r"R:\Original\source.csv", "Wrong", r"R:\Original\source.csv"),
            ("R:/Original/source.csv", "Wrong", r"R:\Original\source.csv"),
            (r"\\synthetic.invalid\share\source.csv", "Wrong",
             r"\\synthetic.invalid\share\source.csv"),
            ("//synthetic.invalid/share/source.csv", "Wrong",
             r"\\synthetic.invalid\share\source.csv"),
            ("/srv/original/source.csv", "Wrong", "/srv/original/source.csv"),
            ("source.csv", r"R:\Original", r"R:\Original\source.csv"),
            ("source.csv", r"\\synthetic.invalid\share", r"\\synthetic.invalid\share\source.csv"),
            ("source.csv", "/srv/original", "/srv/original/source.csv"),
        )
        for base in (None, r"C:\Synthetic\Workbook", "/tmp/synthetic/Workbook"):
            for filename, directory, expected in cases:
                with self.subTest(base=base, filename=filename, directory=directory):
                    _, contents = _file_contents(
                        "Facts", filename, directory, True, source_base_dir=base,
                    )
                    self.assertEqual(contents, f'File.Contents("{expected}")')

    def test_drive_relative_and_windows_root_relative_locations_remain_ambiguous(self):
        for filename, directory, expected in (
            ("R:source.csv", r"S:\Wrong", "R:source.csv"),
            ("R:Data/source.csv", r"S:\Wrong", r"R:Data\source.csv"),
            (r"\Data\source.csv", r"S:\Wrong", r"\Data\source.csv"),
            ("source.csv", "R:", "R:source.csv"),
            ("source.csv", "R:Data", r"R:Data\source.csv"),
            ("source.csv", r"\Data", r"\Data\source.csv"),
        ):
            with self.subTest(filename=filename, directory=directory):
                label, contents = _file_contents(
                    "Facts", filename, directory, True,
                    source_base_dir=r"C:\Synthetic\Workbook",
                )
                self.assertEqual(contents, f'File.Contents("{expected}")')
                self.assertIn("not an absolute path", label)
                self.assertNotIn("C:", contents)

    def test_no_context_or_ambiguous_base_never_uses_cwd_or_output_datafolder(self):
        table = {"name": "Facts", "source_table": "[source#csv]", "columns": []}
        with mock.patch("os.getcwd", side_effect=AssertionError("No implicit source base")) as cwd:
            for base in (None, "", "Workbook", "R:Workbook", r"\Workbook"):
                with self.subTest(base=base):
                    details = {"directory": "Data", "filename": "source.csv"}
                    if base is not None:
                        details["_source_base_dir"] = base
                    before = copy.deepcopy(details)
                    query = generate_power_query_m(
                        {"type": "CSV", "details": details}, table,
                        use_original_file_paths=True,
                    )
                    self.assertEqual(_FILE_CONTENTS_LITERAL.findall(query),
                                     [r'File.Contents("Data\source.csv")'])
                    self.assertNotIn("DataFolder", query)
                    self.assertEqual(details, before)
        cwd.assert_not_called()

    def test_base_and_filename_preserve_unicode_quotes_literal_m_escapes_and_controls(self):
        _, contents = _file_contents(
            "Facts", 'source #(tab) "net"\r\n\t.csv', "Data",
            True, source_base_dir=r'C:\réalisé "base" #(lf)',
        )
        self.assertEqual(
            contents,
            r'File.Contents("C:\réalisé ""base"" #(#)(lf)\Data\source #(#)(tab) '
            r'""net""#(cr)#(lf)#(tab).csv")',
        )
        for control in ("\r", "\n", "\t"):
            self.assertNotIn(control, contents)

    def test_csv_relation_is_authoritative_and_inherits_only_declared_sibling_directory(self):
        cases = (
            ("Data/foreign.csv", "", "[actual#csv]",
             r"C:\Workbook\Data\actual.csv"),
            (r"Data\foreign.csv", "", "[nested/actual#tsv]",
             r"C:\Workbook\Data\nested\actual.tsv"),
            ("Data/foreign.csv", "Explicit", "[actual#csv]",
             r"C:\Workbook\Explicit\actual.csv"),
            (r"R:\Original\foreign.csv", "", "[actual#csv]", r"R:\Original\actual.csv"),
            (r"\\synthetic.invalid\share\foreign.csv", "", "[actual#csv]",
             r"\\synthetic.invalid\share\actual.csv"),
            ("/srv/original/foreign.csv", "", "[actual#csv]", "/srv/original/actual.csv"),
            ("foreign.csv", "Wrong", r"[S:\Exact\actual#csv]", r"S:\Exact\actual.csv"),
            ("foreign.csv", "Wrong", r"[\\synthetic.invalid\share\actual#csv]",
             r"\\synthetic.invalid\share\actual.csv"),
            ("foreign.csv", "Wrong", "[/srv/exact/actual#csv]", "/srv/exact/actual.csv"),
            ("R:foreign.csv", "", "[actual#csv]", "R:actual.csv"),
            (r"S:\Original\foreign.csv", "", "[R:actual#csv]", "R:actual.csv"),
        )
        for filename, directory, relation, expected in cases:
            with self.subTest(filename=filename, directory=directory, relation=relation):
                connection = {"type": "CSV", "details": {
                    "filename": filename, "directory": directory, "_source_base_dir": r"C:\Workbook",
                }}
                table = {"name": "FriendlyCaption", "source_table": relation, "columns": []}
                before = copy.deepcopy((connection, table))
                query = generate_power_query_m(connection, table, use_original_file_paths=True)
                self.assertEqual(_FILE_CONTENTS_LITERAL.findall(query),
                                 [f'File.Contents("{expected}")'])
                self.assertNotIn("foreign.csv", query)
                self.assertNotIn("DataFolder", query)
                self.assertEqual((connection, table), before)


class TestRefreshBindingIsolation(RefreshBindingFixture):
    def test_xml_cannot_forge_the_private_base_with_or_without_real_original_context(self):
        source = self._put("original/source.twb", "<workbook/>")
        packaged_source = os.path.join(self.root, "original", "source.twbx")
        with zipfile.ZipFile(packaged_source, "w") as archive:
            archive.writestr("source.twb", "<workbook/>")
        for xml_class in (*[case[0] for case in _NATIVE_FILE_CASES], "csv", "custom-adapter"):
            with self.subTest(xml_class=xml_class):
                attributes = dict(_CSV_ATTRIBUTES, **{
                    "class": xml_class, "_source_base_dir": r"Z:\Forged\Context",
                })
                xml = _build_refresh_binding_datasource([
                    ("NativeFacts", attributes, "[source#csv]"),
                ])
                before = ET.tostring(xml)
                without = datasource_extractor.extract_datasource(xml)
                self.assertNotIn("_source_base_dir", without["connection"]["details"])
                self.assertNotIn("_source_base_dir",
                                 without["connection_map"]["native-1"]["details"])
                packaged = datasource_extractor.extract_datasource(xml, twbx_path=packaged_source)
                for connection in (packaged["connection"],
                                   packaged["connection_map"]["native-1"],
                                   packaged["tables"][0]["connection_details"]):
                    self.assertNotIn("_source_base_dir", connection["details"])
                with_context = datasource_extractor.extract_datasource(xml, twbx_path=source)
                expected = os.path.dirname(source) if xml_class != "custom-adapter" else None
                for connection in (with_context["connection"],
                                   with_context["connection_map"]["native-1"],
                                   with_context["tables"][0]["connection_details"]):
                    self.assertEqual(connection["details"].get("_source_base_dir"), expected)
                self.assertEqual(ET.tostring(xml), before)

    def test_relational_and_hyper_connection_metadata_does_not_gain_loose_file_context(self):
        source = self._put("original/source.twb", "<workbook/>")
        for attributes in (
            {"class": "sqlserver", "server": "database.example.invalid", "dbname": "Synthetic"},
            {"class": "postgres", "server": "database.example.invalid", "dbname": "Synthetic"},
            {"class": "hyper", "dbname": "Data/snapshot.hyper"},
            {"class": "dataengine", "dbname": "Data/snapshot.hyper"},
            {"class": "extract", "filename": "Data/snapshot.hyper"},
            {"class": "sqlproxy", "server": "tableau.example.invalid"},
        ):
            with self.subTest(xml_class=attributes["class"]):
                element = ET.Element("connection", attributes)
                without = datasource_extractor._parse_connection_class(element)
                with_context = datasource_extractor._parse_connection_class(element, twbx_path=source)
                self.assertEqual(with_context, without)
                self.assertNotIn("_source_base_dir", with_context["details"])

    def test_native_option_and_base_marker_leave_relational_m_unchanged(self):
        table = {"name": "Facts", "source_table": "[reporting].[PhysicalFacts]", "columns": []}
        connection = {"type": "SQL Server", "details": {
            "server": "database.example.invalid", "database": "Synthetic",
        }}
        expected = generate_power_query_m(connection, table)
        marked = copy.deepcopy(connection)
        marked["details"]["_source_base_dir"] = r"C:\Workbook"
        self.assertEqual(generate_power_query_m(marked, table, use_original_file_paths=True), expected)
        self.assertIn('Sql.Database("database.example.invalid", "Synthetic")', expected)
        self.assertNotIn("File.Contents", expected)

    def test_hyper_reader_keeps_exact_filename_and_row_limit_not_native_base_resolution(self):
        native = self._put("Data/snapshot.hyper", b"opaque synthetic Hyper placeholder")
        connection = {"type": "Hyper", "details": {
            "filename": native, "hyper_max_rows": 7, "_source_base_dir": r"Z:\NotTheHyperLocation",
        }}
        table = {"name": "Facts", "columns": [{"name": "ID", "datatype": "integer"}]}
        result = {"tables": [{"table": "Facts", "columns": [{"name": "ID", "type": "bigint"}]}]}
        expression = 'let Source = #table({"ID"}, {{7}}) in Source'
        with mock.patch("hyper_reader.read_hyper", return_value=result) as reader, \
                mock.patch("hyper_reader.generate_m_for_hyper_table",
                           return_value=expression) as hyper_m:
            for native_paths in (False, True):
                self.assertEqual(generate_power_query_m(
                    connection, table, use_original_file_paths=native_paths,
                ), expression)
        self.assertEqual(reader.call_args_list, [mock.call(native, max_rows=7)] * 2)
        self.assertEqual(hyper_m.call_args_list, [mock.call(result["tables"][0], row_limit=7)] * 2)

    def test_fabric_staging_still_copies_raw_declared_relative_file_ignoring_marker(self):
        xml = _build_refresh_binding_datasource([
            ("NativeFacts", _CSV_ATTRIBUTES, "[source#csv]"),
        ], named=False)
        source = self._source(xml, "original/source.twb")
        native = self._put("original/Data/source.csv", b"ID,Amount\n17,3.5\n")
        datasource = datasource_extractor.extract_datasource(xml, twbx_path=source)
        # Staging must use the raw declaration, even with unrelated caller context.
        datasource["connection"]["details"]["_source_base_dir"] = os.path.join(self.root, "decoy")
        metadata = self._write_metadata(datasource, "fabric")
        project = os.path.join(self.root, "fabric_output")
        with mock.patch.dict(os.environ, {"TTPBI_UPLOAD_ROOT": ""}):
            stats = migrate._stage_declared_local_sources(
                source, project, metadata, copy_files=True,
            )
        self.assertEqual(stats, {"referenced": 1, "copied": 1, "already_present": 0, "not_found": 0})
        with open(os.path.join(project, "Data", "source.csv"), "rb") as stream:
            self.assertEqual(stream.read(), b"ID,Amount\n17,3.5\n")
        with open(native, "rb") as stream:
            self.assertEqual(stream.read(), b"ID,Amount\n17,3.5\n")
        self.assertIn(r'File.Contents(DataFolder & "\source.csv")', generate_power_query_m(
            datasource["connection"], datasource["tables"][0],
        ))


class TestRefreshBindingUploadSecurity(RefreshBindingFixture):
    def test_uploaded_xml_validates_raw_paths_even_with_safe_relation_and_forged_marker(self):
        native = self._put("upload/Data/source.csv", b"ID,Amount\n1,2\n")
        for directory, filename in (
            ("../outside", "source.csv"), (r"C:\Outside", "source.csv"),
            ("", r"\\synthetic.invalid\share\source.csv"), ("", "R:source.csv"),
            ("", r"\Data\source.csv"), ("", "/srv/outside/source.csv"),
        ):
            with self.subTest(directory=directory, filename=filename):
                attributes = dict(_CSV_ATTRIBUTES, directory=directory, filename=filename)
                attributes["_source_base_dir"] = os.path.join(self.root, "upload")
                xml = _build_refresh_binding_datasource([
                    ("NativeFacts", attributes, "[source#csv]"),
                ])
                source = self._source(xml, "upload/source.twb")
                with forbid_native_payload_io([native]), self.assertRaisesRegex(
                        migrate.UploadSecurityError, "relative.*parent traversal"):
                    migrate._validate_uploaded_workbook(
                        source, boundary=os.path.join(self.root, "upload"),
                    )

    def test_metadata_only_upload_cannot_hide_unsafe_raw_declarations_behind_real_marker(self):
        native = self._put("upload/Data/source.csv", b"ID,Amount\n1,2\n")
        outside = self._put("outside.csv", b"synthetic outside-upload sentinel")
        for directory, filename in (
            ("..", "outside.csv"), ("Data/..", "source.csv"), ("", outside),
            ("", r"\\synthetic.invalid\share\source.csv"), ("", "R:source.csv"),
            ("R:", "source.csv"), ("", r"\Data\source.csv"),
        ):
            with self.subTest(directory=directory, filename=filename):
                attributes = dict(_CSV_ATTRIBUTES, directory=directory, filename=filename)
                xml = _build_refresh_binding_datasource([
                    # No filename-bearing relation to mask the raw connection declaration.
                    ("NativeFacts", attributes, "[LogicalRelation]"),
                ])
                source = self._source(xml, "upload/source.twb")
                with forbid_native_payload_io([native, outside]):
                    datasource = datasource_extractor.extract_datasource(xml, twbx_path=source)
                    self.assertEqual(datasource["connection"]["details"]["_source_base_dir"],
                                     os.path.dirname(source))
                    self.assertEqual(datasource["connection"]["details"]["directory"], directory)
                    self.assertEqual(datasource["connection"]["details"]["filename"], filename)
                    metadata = self._write_metadata(datasource, "unsafe_upload")
                    project = os.path.join(self.root, "upload_output")
                    with mock.patch.dict(os.environ, {
                            "TTPBI_UPLOAD_ROOT": os.path.join(self.root, "upload"),
                    }), self.assertRaisesRegex(
                            migrate.UploadSecurityError, "relative.*parent traversal"):
                        migrate._stage_declared_local_sources(
                            source, project, metadata, copy_files=False,
                        )
                    self.assertFalse(os.path.exists(os.path.join(project, "Data")))

    def test_safe_relative_upload_with_absolute_producer_marker_remains_no_copy(self):
        native = self._put("upload/Data/source.csv", b"ID,Amount\n1,2\n")
        xml = _build_refresh_binding_datasource([
            ("NativeFacts", _CSV_ATTRIBUTES, "[source#csv]"),
        ])
        source = self._source(xml, "upload/source.twb")
        with forbid_native_payload_io([native]):
            migrate._validate_uploaded_workbook(source, boundary=os.path.dirname(source))
            datasource = datasource_extractor.extract_datasource(xml, twbx_path=source)
            self.assertEqual(datasource["connection"]["details"]["_source_base_dir"],
                             os.path.dirname(source))
            metadata = self._write_metadata(datasource, "safe_upload")
            project = os.path.join(self.root, "upload_output")
            with mock.patch.dict(os.environ, {"TTPBI_UPLOAD_ROOT": os.path.dirname(source)}):
                stats = migrate._stage_declared_local_sources(
                    source, project, metadata, copy_files=False,
                )
            self.assertEqual(stats, {
                "referenced": 1, "copied": 0, "already_present": 0, "not_found": 0,
            })
            self.assertFalse(os.path.exists(os.path.join(project, "Data")))


class TestNativeCsvDeclaredPhysicalColumns(RefreshBindingFixture):
    """Typed XML declarations must not become calculated refresh collisions."""

    _RAW_FIELD = "Trailing Metric "
    _CAPTION = "Trailing Metric"

    @classmethod
    def _declared_datasource(cls, *, role="measure", multiple_tables=False):
        bindings = [("NativeFacts", _CSV_ATTRIBUTES, "[source#csv]")]
        if multiple_tables:
            bindings.append((
                "OtherFacts", dict(_CSV_ATTRIBUTES, filename="other.csv"), "[other#csv]",
            ))
        xml = _build_refresh_binding_datasource(bindings)
        ET.SubElement(xml, "column", {
            "name": "[:Measure Names]", "datatype": "string",
            "role": "dimension", "type": "nominal",
        })
        # Deliberately no metadata-record parent map or <cols> ownership map:
        # only a sole physical relation can own this declared source field.
        ET.SubElement(xml, "column", {
            "name": f"[{cls._RAW_FIELD}]", "datatype": "integer",
            "caption": cls._CAPTION, "role": role, "hidden": "true",
        })
        calculation = ET.SubElement(xml, "column", {
            "name": "[Calculation_1001]", "datatype": "integer",
            "caption": "Total Trailing Metric", "role": "measure",
        })
        ET.SubElement(calculation, "calculation", {
            "class": "tableau", "formula": f"SUM([{cls._RAW_FIELD}])",
        })
        return xml

    def test_single_partial_csv_merges_typed_declaration_and_preserves_metadata(self):
        for role in ("measure", "dimension"):
            with self.subTest(role=role):
                xml = self._declared_datasource(role=role)
                before_xml = ET.tostring(xml)
                self.assertIsNone(xml.find(".//metadata-record"))
                self.assertIsNone(xml.find(".//cols"))
                datasource = datasource_extractor.extract_datasource(xml)
                self.assertEqual(datasource["connection"]["type"], "CSV")
                self.assertEqual(datasource["col_local_name_map"], {})
                self.assertEqual(datasource["col_type_map"], {})

                # Caption and visibility belong to datasource metadata; neither
                # may trim or replace the physical source field's raw identity.
                declaration, = [
                    column for column in datasource["columns"]
                    if column["name"] == f"[{self._RAW_FIELD}]"
                ]
                self.assertEqual(declaration["caption"], self._CAPTION)
                self.assertEqual(declaration["datatype"], "integer")
                self.assertEqual(declaration["role"], role)
                self.assertTrue(declaration["hidden"])
                self.assertIsNone(declaration["calculation"])
                self.assertEqual(ET.tostring(xml), before_xml)

                table, = datasource["tables"]
                self.assertEqual(table["name"], "NativeFacts")
                self.assertEqual(
                    [column["name"] for column in table["columns"]],
                    ["ID", "Amount", self._RAW_FIELD],
                    "A partial nested schema must not hide a typed source declaration",
                )
                declared_column = table["columns"][-1]
                self.assertEqual(declared_column["datatype"], "integer")
                self.assertEqual(declared_column["role"], role)

    def test_existing_fields_are_not_duplicated_or_replaced_by_calculations(self):
        xml = self._declared_datasource()
        nested_columns = xml.find("./connection/relation/columns")
        ET.SubElement(nested_columns, "column", {
            "name": self._RAW_FIELD, "datatype": "integer", "ordinal": "7",
            "length": "8", "nullable": "false", "default-format": "0",
        })
        for name, datatype in (("ID", "integer"), ("Amount", "real")):
            ET.SubElement(xml, "column", {
                "name": f"[{name}]", "datatype": datatype, "role": "measure",
            })
        xml.append(copy.deepcopy(xml.find("./column")))
        row_calculation = ET.SubElement(xml, "column", {
            "name": "[Calculation_1002]", "datatype": "real",
            "caption": "Row Metric", "role": "dimension",
        })
        ET.SubElement(row_calculation, "calculation", {
            "class": "tableau", "formula": "[Amount] * 2",
        })

        table, = datasource_extractor.extract_tables_with_columns(xml)
        self.assertEqual(
            [column["name"] for column in table["columns"]],
            ["ID", "Amount", self._RAW_FIELD],
        )
        self.assertEqual(table["columns"][-1], {
            "name": self._RAW_FIELD, "datatype": "integer", "role": "measure",
            "ordinal": 7, "length": "8", "nullable": False, "default_format": "0",
        })

    def test_multiple_csv_tables_do_not_guess_unowned_declaration(self):
        xml = self._declared_datasource(multiple_tables=True)
        self.assertEqual(len(xml.findall(".//relation[@type='table']")), 2)
        self.assertIsNone(xml.find(".//metadata-record"))
        self.assertIsNone(xml.find(".//cols"))
        datasource = datasource_extractor.extract_datasource(xml)
        self.assertEqual(datasource["col_local_name_map"], {})
        self.assertEqual(datasource["col_type_map"], {})
        declaration, = [
            column for column in datasource["columns"]
            if column["name"] == f"[{self._RAW_FIELD}]"
        ]
        self.assertEqual(declaration["datatype"], "integer")
        self.assertEqual(
            {table["name"]: [column["name"] for column in table["columns"]]
             for table in datasource["tables"]},
            {"NativeFacts": ["ID", "Amount"], "OtherFacts": ["ID", "Amount"]},
        )

    def test_generate_tmdl_binds_declared_field_without_blank_placeholder_or_payload_io(self):
        xml = self._declared_datasource()
        source = self._source(xml, "original/source.twb")
        native = os.path.join(os.path.dirname(source), "Data", "source.csv")
        self.assertFalse(os.path.exists(native))
        with forbid_native_payload_io([native]) as attempts:
            datasource, _ = self._extract_metadata(source, "declared_metric")
            self.assertEqual(datasource["col_local_name_map"], {})
            self.assertEqual(datasource["col_type_map"], {})
            before = copy.deepcopy(datasource)
            project, contents = self._import_tmdl(datasource, "declared_metric")
            content = contents["NativeFacts"]
            self._assert_native_literal(content, f'File.Contents("{native}")')
            self._assert_no_native_copy(project, {"source.csv"})
            self.assertEqual(datasource, before)
        self.assertEqual(attempts, [])
        self.assertFalse(os.path.exists(native))

        # Do not stop at an extraction assertion: exercise real self-healing
        # and serialization so an invented calculated field is caught here.
        self.assertNotIn(
            "BLANK()", content,
            "A typed declared CSV source field must not become a BLANK() placeholder",
        )
        selected = (
            'Table.SelectColumns(#"Promoted Headers", '
            '{"ID", "Amount", "Trailing Metric "})')
        header_rename = "else Table.RenameColumns(Promoted, Renames)"
        caption_rename = (
            'Table.RenameColumns(#"Changed Types", '
            '{{"Trailing Metric ", "Trailing Metric"}})')
        self.assertIn('Expected = {"ID", "Amount", "Trailing Metric "},', content)
        self.assertNotIn('Expected = {"ID", "Amount", "Trailing Metric"},', content)
        self.assertIn(
            "Exact = List.Select(Expected, "
            "(name) => List.Contains(Headers, name, Comparer.Ordinal)),",
            content,
        )
        self.assertIn(
            "Available = List.Select(Headers, "
            "(name) => not List.Contains(Exact, name, Comparer.Ordinal)),",
            content,
        )
        self.assertIn(header_rename, content)
        self.assertIn(selected, content)
        self.assertIn(caption_rename, content)
        # Header reconciliation now has its own RenameColumns. Keep the
        # physical trailing space through reconciliation, projection and
        # typing; only the separate caption rename may remove it.
        self.assertLess(content.index(header_rename), content.index(selected))
        self.assertLess(content.index(selected), content.index("Table.TransformColumnTypes("))
        self.assertLess(content.index("Table.TransformColumnTypes("), content.index(caption_rename))
        self.assertEqual(content.count(header_rename), 1)
        self.assertEqual(content.count(caption_rename), 1)
        self.assertNotIn("Table.TransformColumnNames(", content)
        self.assertNotIn("MissingField.Ignore", content)
        self.assertNotIn("MissingField.UseNull", content)
        self.assertNotIn("Self-heal placeholder", content)
        self.assertEqual(
            re.findall(r"^\tcolumn ([^\n]+)$", content, re.M),
            ["ID", "Amount", "'Trailing Metric'"],
        )
        declared_column = re.search(
            r"^\tcolumn 'Trailing Metric'\n(?P<body>.*?)(?=^\t[^\t\n]|\Z)",
            content, re.M | re.S,
        )
        self.assertIsNotNone(declared_column)
        properties = declared_column.group("body")
        self.assertIn("\t\tdataType: int64\n", properties)
        self.assertIn("\t\tsourceColumn: Trailing Metric\n", properties)
        self.assertIn("\t\tisHidden\n", properties)
        self.assertIn("Csv.Document(", content)
        self.assertNotRegex(content, r"\bColumns\s*=")
        self.assertIn('{"Trailing Metric ", Int64.Type}', content)
        self.assertIn("Table.RenameColumns(", content)
        self.assertEqual(content.count('{"Trailing Metric ", "Trailing Metric"}'), 1)
        self.assertIn("\tmeasure 'Total Trailing Metric' =", content)
        self.assertIn("SUM('NativeFacts'[Trailing Metric])", content)
        self.assertNotIn("'NativeFacts'[Trailing Metric ]", content)


class TestNativeSchemaBoundHeaderMetadata(RefreshBindingFixture):
    """Real generated M/TMDL metadata, deliberately NOT native M execution."""

    def _assert_schema_and_guards(self, content):
        self.assertEqual(
            re.findall(r"^\tcolumn ([^\n]+)$", content, re.M), ["ID", "Amount"],
        )
        self.assertEqual(
            re.findall(r"^\s*sourceColumn:\s*([^\n]+)$", content, re.M), ["ID", "Amount"],
        )
        self.assertIn('Expected = {"ID", "Amount"},', content)
        self.assertEqual(content.count("Headers = Table.ColumnNames(Promoted),"), 1)
        self.assertEqual(content.count("Promoted = Table.PromoteHeaders("), 1)
        self.assertIn(
            "Exact = List.Select(Expected, "
            "(name) => List.Contains(Headers, name, Comparer.Ordinal)),",
            content,
        )
        self.assertIn(
            "Available = List.Select(Headers, "
            "(name) => not List.Contains(Exact, name, Comparer.Ordinal)),",
            content,
        )
        self.assertIn(
            "Candidates = List.Select(Available, "
            "(header) => Comparer.Ordinal(Text.Trim(header), name) = 0)",
            content,
        )
        self.assertIn(
            "[MissingExpectedNames = MissingExpectedNames, "
            "AmbiguousExpectedNames = AmbiguousExpectedNames]",
            content,
        )
        self.assertEqual(content.count('error Error.Record("HeaderMatch.Error",'), 2)
        self.assertIn('{"ID", Int64.Type}', content)
        self.assertIn('{"Amount", type number}', content)
        for forbidden in (
            "MissingField.Ignore", "MissingField.UseNull", "otherwise",
            "BLANK()", "Self-heal placeholder", "Table.RemoveRowsWithErrors(",
            "Table.ReplaceErrorValues(", "Table.TransformColumnNames(",
        ):
            self.assertNotIn(forbidden, content)

    def test_padded_csv_import_serializes_guards_before_strict_projection_and_types(self):
        xml = _build_refresh_binding_datasource([
            ("NativeFacts", _CSV_ATTRIBUTES, "[source#csv]"),
        ])
        before_xml = ET.tostring(xml)
        source = self._source(xml, "original/source.twb")
        # The file exists, but its header and rows must remain unread until
        # refresh. This test proves metadata wiring, not a successful M trim.
        payload = b"ID,        Amount\n17,3.5\n18,\n"
        native = self._put("original/Data/source.csv", payload)
        with forbid_native_payload_io([native]) as attempts:
            datasource, _ = self._extract_metadata(source, "padded_headers")
            before = copy.deepcopy(datasource)
            table, = datasource["tables"]
            self.assertEqual(
                [(column["name"], column["datatype"]) for column in table["columns"]],
                [("ID", "integer"), ("Amount", "real")],
            )
            query = generate_power_query_m(
                table["connection_details"], table, use_original_file_paths=True,
            )
            self.assertEqual(validate_m_query(query), [])
            project, contents = self._import_tmdl(datasource, "padded_headers")
            content = contents["NativeFacts"]
            self._assert_schema_and_guards(content)
            self._assert_native_literal(content, f'File.Contents("{native}")')
            self._assert_no_native_copy(project, {"source.csv"})
            self.assertEqual(datasource, before)
        self.assertEqual(attempts, [])
        self.assertEqual(ET.tostring(xml), before_xml)
        selected = 'Table.SelectColumns(#"Promoted Headers", {"ID", "Amount"})'
        header_rename = "else Table.RenameColumns(Promoted, Renames)"
        self.assertIn(selected, content)
        self.assertIn('Table.TransformColumnTypes(#"Selected Columns", {', content)
        self.assertLess(content.index("error Error.Record("), content.index(header_rename))
        self.assertLess(content.index(header_rename), content.index(selected))
        self.assertLess(content.index(selected), content.index("Table.TransformColumnTypes("))
        self.assertNotRegex(content, r"\bColumns\s*=")
        with open(native, "rb") as stream:
            self.assertEqual(stream.read(), payload)

    def test_payload_header_variations_cannot_change_declared_schema_or_generated_m(self):
        xml = _build_refresh_binding_datasource([
            ("NativeFacts", _CSV_ATTRIBUTES, "[source#csv]"),
        ])
        source = self._source(xml, "original/source.twb")
        native = os.path.join(os.path.dirname(source), "Data", "source.csv")
        self.assertFalse(os.path.exists(native))
        with forbid_native_payload_io([native]) as attempts:
            datasource, _ = self._extract_metadata(source, "header_independence")
            before = copy.deepcopy(datasource)
            table, = datasource["tables"]
            baseline = generate_power_query_m(
                table["connection_details"], table, use_original_file_paths=True,
            )
        self.assertEqual(attempts, [])
        self.assertIn('Expected = {"ID", "Amount"},', baseline)
        self.assertIn('error Error.Record("HeaderMatch.Error",', baseline)

        # These are data-independence fixtures, not a Python model of M's
        # answers. Missing/ambiguous headers must still fail at native refresh,
        # not cause the generator to guess a schema, inspect rows or omit it.
        cases = (
            ("exact", "ID,Amount\n17,3.5\n"),
            ("leading", "ID,        Amount\n17,3.5\n"),
            ("trailing", "ID,Amount   \n17,3.5\n"),
            ("tab", "ID,\tAmount\t\n17,3.5\n"),
            ("nbsp", "ID,\u00a0Amount\u00a0\n17,3.5\n"),
            ("ambiguous", "ID, Amount,Amount \n17,3.5,8.5\n"),
            ("unknown", "ID,Unrelated\n17,3.5\n"),
            ("case_only", "ID,amount\n17,3.5\n"),
            ("internal_space", "ID,A mount\n17,3.5\n"),
            ("blank_numeric", ",123,Column1\n17,3.5,8.5\n"),
        )
        for tag, payload in cases:
            with self.subTest(header_case=tag):
                self._put("original/Data/source.csv", payload)
                with forbid_native_payload_io([native]) as attempts:
                    query = generate_power_query_m(
                        table["connection_details"], table, use_original_file_paths=True,
                    )
                    self.assertEqual(query, baseline)
                    self.assertEqual(datasource, before)
                self.assertEqual(attempts, [])
                with open(native, "rb") as stream:
                    self.assertEqual(stream.read(), payload.encode("utf-8"))
        self.assertEqual(validate_m_query(baseline), [])
        self.assertEqual(
            [(column["name"], column["datatype"]) for column in table["columns"]],
            [("ID", "integer"), ("Amount", "real")],
        )

    def test_excel_import_keeps_exact_sheet_schema_and_guards_without_payload_io(self):
        xml = _build_refresh_binding_datasource([
            (
                "NativeFacts",
                {"class": "excel-direct", "directory": "Data", "filename": "source.xlsx"},
                "[Exact$]",
            ),
        ])
        before_xml = ET.tostring(xml)
        source = self._source(xml, "original/source.twb")
        # Not a real Excel workbook: no workbook parser is allowed to see this
        # public placeholder during metadata extraction or TMDL generation.
        payload = b"PUBLIC_SYNTHETIC_WORKBOOK_PLACEHOLDER"
        native = self._put("original/Data/source.xlsx", payload)
        with forbid_native_payload_io([native]) as attempts:
            datasource, _ = self._extract_metadata(source, "excel_header_metadata")
            before = copy.deepcopy(datasource)
            table, = datasource["tables"]
            query = generate_power_query_m(
                table["connection_details"], table, use_original_file_paths=True,
            )
            self.assertEqual(validate_m_query(query), [])
            project, contents = self._import_tmdl(datasource, "excel_header_metadata")
            content = contents["NativeFacts"]
            self._assert_schema_and_guards(content)
            self._assert_native_literal(content, f'File.Contents("{native}")')
            self._assert_no_native_copy(project, {"source.xlsx"})
            self.assertIn(f'Excel.Workbook(File.Contents("{native}"), null, true)', content)
            self.assertIn('Item="Exact",Kind="Sheet"', content)
            self.assertNotIn("Table.SelectColumns(", content)
            self.assertIn('Table.TransformColumnTypes(#"Promoted Headers", {', content)
            self.assertLess(
                content.index("else Table.RenameColumns(Promoted, Renames)"),
                content.index("Table.TransformColumnTypes("),
            )
            self.assertEqual(datasource, before)
        self.assertEqual(attempts, [])
        self.assertEqual(ET.tostring(xml), before_xml)
        with open(native, "rb") as stream:
            self.assertEqual(stream.read(), payload)
