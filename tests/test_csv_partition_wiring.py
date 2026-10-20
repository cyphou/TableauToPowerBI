"""CSV partitions are wired to the right file and match its real header."""

import os
import csv
import json
import struct
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

import migrate

NAME = "Report"


class TestDeclaredLocalSourceStaging(unittest.TestCase):
    def stage(self, root, declarations, source_extension=".twb",
              connection_type="textscan"):
        source = os.path.join(root, "(Group)", "Workbook",
                              "Workbook" + source_extension)
        metadata = os.path.join(root, "metadata")
        tables = []
        for directory, filename in declarations:
            tables.append({
                "source_table": filename,
                "connection_details": {
                    "type": connection_type,
                    "details": {"directory": directory, "filename": filename},
                },
            })
        _write(os.path.join(metadata, "datasources.json"),
               json.dumps([{"tables": tables}]))
        project = os.path.join(root, "output")
        result = migrate._stage_declared_local_sources(source, project, metadata)
        return project, result

    def test_exact_relative_source_is_available_before_model_generation(self):
        with tempfile.TemporaryDirectory() as root:
            payload = "Name;Value\nexample;12\n"
            _write(os.path.join(root, "(Group)", "Workbook", "Data", "facts.csv"),
                   payload)
            project, result = self.stage(root, [("Data", "facts.csv")])
            self.assertEqual(result["copied"], 1)
            with open(os.path.join(project, "Data", "facts.csv"),
                      encoding="utf-8") as stream:
                self.assertEqual(stream.read(), payload)
            self.assertFalse(os.path.exists(os.path.join(project, "Report.SemanticModel")))

    def test_same_root_relocation_preserves_declared_sibling(self):
        with tempfile.TemporaryDirectory() as root:
            _write(os.path.join(root, "(Group)", "Workbook", "Data", "facts.csv"),
                   "ID\n1\n")
            _write(os.path.join(root, "(Group)", "Sibling", "Data", "places.csv"),
                   "ID\n2\n")
            old = r"Z:\unavailable-author-machine"
            project, result = self.stage(root, [
                (old + r"\(Group)\Workbook\Data", "facts.csv"),
                (old + r"\(Group)\Sibling\Data", "places.csv"),
            ])
            self.assertEqual(result["copied"], 2)
            with open(os.path.join(project, "Data", "places.csv"),
                      encoding="utf-8") as stream:
                self.assertEqual(stream.read(), "ID\n2\n")

    def test_unrelated_duplicate_is_not_a_source_substitute(self):
        with tempfile.TemporaryDirectory() as root:
            _write(os.path.join(root, "unrelated", "facts.csv"), "ID\n1\n")
            project, result = self.stage(root, [("Data", "facts.csv")])
            self.assertEqual(result["not_found"], 1)
            self.assertFalse(os.path.exists(os.path.join(project, "Data", "facts.csv")))

    def test_conflicting_declared_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            for folder, value in (("Data", "1"), ("Other", "2")):
                _write(os.path.join(root, "(Group)", "Workbook", folder, "facts.csv"),
                       f"ID\n{value}\n")
            with self.assertRaisesRegex(ValueError, "Conflicting declared local sources"):
                self.stage(root, [("Data", "facts.csv"), ("Other", "facts.csv")])

    def archive(self, root, members):
        source = os.path.join(root, "(Group)", "Workbook", "Workbook.twbx")
        os.makedirs(os.path.dirname(source), exist_ok=True)
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("Workbook.twb", "<workbook/>")
            for name, content in members:
                archive.writestr(name, content)
        return source

    def test_embedded_excel_exact_declared_path_is_staged_before_model_generation(self):
        with tempfile.TemporaryDirectory() as root:
            # No Excel parser is needed: staging must preserve opaque bytes.
            payload = b"PK\x03\x04synthetic-excel-binding\x00\xff"
            self.archive(root, [
                ("Data/sub/file.xlsx", payload),
                ("Other/file.xlsx", b"unrelated same-basename member"),
            ])
            # The embedded binding, not this loose decoy, is authoritative.
            _write(os.path.join(root, "(Group)", "Workbook", "Data", "sub",
                                "file.xlsx"), b"outer-folder decoy")
            project, result = self.stage(
                root, [("Data/sub", "file.xlsx")], ".twbx", "excel-direct")
            self.assertEqual(result, {
                "referenced": 1, "copied": 1, "already_present": 0, "not_found": 0})
            with open(os.path.join(project, "Data", "file.xlsx"), "rb") as stream:
                self.assertEqual(stream.read(), payload)
            self.assertFalse(os.path.exists(
                os.path.join(project, "Report.SemanticModel")))

    def test_embedded_excel_filename_can_contain_the_exact_declared_subpath(self):
        with tempfile.TemporaryDirectory() as root:
            payload = b"synthetic excel source"
            self.archive(root, [("Data/sub/file.xlsx", payload)])
            project, result = self.stage(
                root, [("", r"Data\sub\file.xlsx")], ".twbx", "excel")
            self.assertEqual(result["copied"], 1)
            with open(os.path.join(project, "Data", "file.xlsx"), "rb") as stream:
                self.assertEqual(stream.read(), payload)

    def test_missing_exact_member_is_not_replaced_by_same_basename(self):
        with tempfile.TemporaryDirectory() as root:
            self.archive(root, [("Other/file.xlsx", b"not the declared binding")])
            project, result = self.stage(
                root, [("Data/sub", "file.xlsx")], ".twbx", "excel-direct")
            self.assertEqual(result["referenced"], 1)
            self.assertEqual(result["not_found"], 1)
            self.assertEqual(result["copied"], 0)
            self.assertFalse(os.path.exists(os.path.join(project, "Data", "file.xlsx")))

    def test_differing_declared_members_cannot_collapse_to_one_output_basename(self):
        with tempfile.TemporaryDirectory() as root:
            self.archive(root, [
                ("Data/first/file.xlsx", b"first source"),
                ("Data/second/file.xlsx", b"second source"),
            ])
            with self.assertRaisesRegex(ValueError, "Conflicting declared local sources"):
                self.stage(root, [
                    ("Data/first", "file.xlsx"), ("Data/second", "file.xlsx"),
                ], ".twbx", "excel-direct")
            self.assertFalse(os.path.exists(
                os.path.join(root, "output", "Data", "file.xlsx")))

    def test_declared_archive_path_traversal_is_rejected(self):
        for directory in ("../Data", r"Data\..\outside"):
            with self.subTest(directory=directory), tempfile.TemporaryDirectory() as root:
                self.archive(root, [(directory.replace("\\", "/") + "/file.xlsx",
                                     b"must not escape")])
                with self.assertRaises(migrate.SecurityError):
                    self.stage(root, [(directory, "file.xlsx")],
                               ".twbx", "excel-direct")
                self.assertFalse(os.path.exists(
                    os.path.join(root, "output", "Data", "file.xlsx")))

    def test_xml_workbook_misnamed_twbx_retains_loose_relative_source_behavior(self):
        with tempfile.TemporaryDirectory() as root:
            folder = os.path.join(root, "(Group)", "Workbook")
            _write(os.path.join(folder, "Workbook.twbx"), "<workbook/>")
            payload = b"loose synthetic excel source"
            _write(os.path.join(folder, "Data", "sub", "file.xlsx"), payload)
            project, result = self.stage(
                root, [("Data/sub", "file.xlsx")], ".twbx", "excel-direct")
            self.assertEqual(result["copied"], 1)
            self.assertEqual(result["not_found"], 0)
            with open(os.path.join(project, "Data", "file.xlsx"), "rb") as stream:
                self.assertEqual(stream.read(), payload)


def _write(path, data, encoding="utf-8"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode, **({} if mode == "wb" else {"encoding": encoding, "newline": ""})) as f:
        f.write(data)
    return path


def _project(root, tables):
    """tables: {table_name: partition M body}. Returns the project dir."""
    project = os.path.join(root, NAME)
    definition = os.path.join(project, f"{NAME}.SemanticModel", "definition")
    data = os.path.join(project, "Data").replace("\\", "\\\\")
    _write(os.path.join(definition, "expressions.tmdl"),
           f'expression DataFolder = "{data}" meta [IsParameterQuery=true]\n')
    for table, body in tables.items():
        _write(os.path.join(definition, "tables", f"{table}.tmdl"),
               f"table {table}\n\tpartition {table} = m\n\t\tsource =\n{body}\n")
    return project


def _csv_partition(filename, columns, delimiter=",", count=None):
    types = ",\n".join('\t\t\t\t\t\t{"%s", type text}' % c.replace('"', '""') for c in columns)
    return (
        "\t\t\t\tlet\n"
        f'\t\t\t\t\tSource = Csv.Document(File.Contents(DataFolder & "\\\\{filename}"), '
        f'[Delimiter="{delimiter}", Columns={count or len(columns)}, Encoding=65001, '
        "QuoteStyle=QuoteStyle.None]),\n"
        '\t\t\t\t\t#"Promoted Headers" = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),\n'
        '\t\t\t\t\t#"Changed Types" = Table.TransformColumnTypes(#"Promoted Headers", {\n'
        f"{types}\n\t\t\t\t\t}}),\n"
        '\t\t\t\t\tResult = #"Changed Types"\n'
        "\t\t\t\tin\n\t\t\t\t\tResult")


def _excel_partition(filename, columns):
    cols = ", ".join('"%s"' % c for c in columns)
    types = ",\n".join('\t\t\t\t\t\t{"%s", type text}' % c for c in columns)
    return (
        "\t\t\t\tlet\n"
        "\t\t\t\t\tSource = try let\n"
        f'\t\t\t\t\t\t_src = Excel.Workbook(File.Contents(DataFolder & "\\\\{filename}"), null, true),\n'
        '\t\t\t\t\t\t_nav = _src{[Item="Sheet",Kind="Sheet"]}[Data]\n'
        "\t\t\t\t\tin _nav otherwise #table({" + cols + "}, {}),\n"
        '\t\t\t\t\t#"Promoted Headers" = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),\n'
        '\t\t\t\t\t#"Changed Types" = Table.TransformColumnTypes(#"Promoted Headers", {\n'
        f"{types}\n\t\t\t\t\t}}),\n"
        '\t\t\t\t\tResult = #"Changed Types"\n'
        "\t\t\t\tin\n\t\t\t\t\tResult")


def _read(project, table):
    with open(os.path.join(project, f"{NAME}.SemanticModel", "definition",
                           "tables", f"{table}.tmdl"), encoding="utf-8") as f:
        return f.read()


class TestReconcileCsvPartitions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_semicolon_file_gets_semicolon_delimiter(self):
        project = _project(self.root, {"T": _csv_partition("a.csv", ["x", "y"])})
        _write(os.path.join(project, "Data", "a.csv"), "x;y\n1;2\n")
        stats = migrate._reconcile_csv_partitions(project, NAME)
        text = _read(project, "T")
        self.assertIn('Delimiter=";"', text)
        self.assertIn("QuoteStyle=QuoteStyle.Csv", text)
        self.assertEqual(stats["options_fixed"], 1)

    def test_windows_1252_file_gets_matching_encoding(self):
        project = _project(self.root, {"T": _csv_partition("a.csv", ["Année"])})
        _write(os.path.join(project, "Data", "a.csv"), "Année\n2024\n".encode("cp1252"))
        migrate._reconcile_csv_partitions(project, NAME)
        self.assertIn("Encoding=1252", _read(project, "T"))

    def test_fixed_column_count_is_removed_so_wide_files_are_not_truncated(self):
        project = _project(self.root, {"T": _csv_partition("a.csv", ["z"], count=1)})
        _write(os.path.join(project, "Data", "a.csv"), "a,b,z\n1,2,3\n")
        migrate._reconcile_csv_partitions(project, NAME)
        self.assertNotIn("Columns=", _read(project, "T"))

    def test_joined_column_caption_is_renamed_from_file_header(self):
        project = _project(self.root, {
            "T": _csv_partition("a.csv", ["id", "name (other.csv)"])})
        _write(os.path.join(project, "Data", "a.csv"), "id,name\n1,x\n")
        stats = migrate._reconcile_csv_partitions(project, NAME)
        text = _read(project, "T")
        self.assertIn('Table.TransformColumnTypes(Table.RenameColumns(#"Promoted Headers", '
                      '{{"name", "name (other.csv)"}})', text)
        self.assertEqual(stats["columns_renamed"], 1)

    def test_column_absent_from_file_becomes_empty_column_and_is_counted(self):
        project = _project(self.root, {"T": _csv_partition("a.csv", ["id", "ghost"])})
        _write(os.path.join(project, "Data", "a.csv"), "id\n1\n")
        stats = migrate._reconcile_csv_partitions(project, NAME)
        text = _read(project, "T")
        self.assertEqual(stats["columns_unmatched"], 1)
        self.assertIn('Table.AddColumn(#"Promoted Headers", "ghost", each null)', text)
        self.assertNotIn("RenameColumns", text)

    def test_missing_file_leaves_partition_untouched(self):
        body = _csv_partition("absent.csv", ["x"])
        project = _project(self.root, {"T": body})
        migrate._reconcile_csv_partitions(project, NAME)
        self.assertIn('Delimiter=","', _read(project, "T"))
        self.assertIn("Columns=1", _read(project, "T"))


class TestHyperCsvRouting(unittest.TestCase):
    def test_pick_requires_full_column_coverage(self):
        headers = {"a.csv": {"x", "y"}, "b.csv": {"x", "z"}}
        self.assertEqual(migrate._pick_csv_for_columns(["X", "Z"], headers), "b.csv")
        self.assertIsNone(migrate._pick_csv_for_columns(["x", "q"], headers))

    def test_pick_refuses_equal_ambiguity_but_prefers_tighter_header(self):
        self.assertIsNone(migrate._pick_csv_for_columns(
            ["x"], {"a.csv": {"x", "y"}, "b.csv": {"x", "z"}}))
        self.assertEqual(migrate._pick_csv_for_columns(
            ["x"], {"a.csv": {"x"}, "b.csv": {"x", "z"}}), "a.csv")

    def test_each_excel_partition_is_routed_to_its_own_extract_csv(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {
                "Sales": _excel_partition("sales.xlsx", ["Region", "Amount"]),
                "Stock": _excel_partition("stock.xlsx", ["Item", "Qty"]),
            })
            data = os.path.join(project, "Data")
            _write(os.path.join(data, "sales_extract.csv"), "Region,Amount\nN,1\n")
            _write(os.path.join(data, "stock_extract.csv"), "Item,Qty\nA,2\n")
            _write(os.path.join(data, "sales_extract.hyper"), b"x")
            _write(os.path.join(data, "stock_extract.hyper"), b"x")
            migrate._convert_hyper_to_csv_in_data(data, NAME, project)
            self.assertIn("sales_extract.csv", _read(project, "Sales"))
            self.assertIn("stock_extract.csv", _read(project, "Stock"))
            self.assertNotIn("stock_extract.csv", _read(project, "Sales"))

    def test_packaged_excel_file_is_kept(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"Sales": _excel_partition("sales.xlsx", ["Region"])})
            data = os.path.join(project, "Data")
            _write(os.path.join(data, "sales.xlsx"), b"xlsx")
            _write(os.path.join(data, "e.csv"), "Region\nN\n")
            _write(os.path.join(data, "e.hyper"), b"x")
            migrate._convert_hyper_to_csv_in_data(data, NAME, project)
            self.assertIn("Excel.Workbook", _read(project, "Sales"))

    def test_uses_datasource_metadata_from_active_extract_directory(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {
                "Orders": '\t\t\t\tSource = try #table({"Order ID"}, {}) '
                          'otherwise #table({"Order ID"}, {})\n'
            })
            data = os.path.join(project, "Data")
            _write(os.path.join(data, "orders_extract.csv"),
                   "PhysicalOrder\n123\n")
            _write(os.path.join(data, "orders_extract.hyper"), b"x")
            extract_dir = os.path.join(root, "extract")
            _write(os.path.join(extract_dir, "datasources.json"), json.dumps([{
                "tables": [{
                    "name": "Orders",
                    "source_table": "orders_extract",
                    "cols_physical_map": {"PhysicalOrder": "Order ID"},
                }]
            }]))

            with mock.patch.object(migrate._extract_dir_local, "path", extract_dir,
                                   create=True):
                migrate._convert_hyper_to_csv_in_data(data, NAME, project)

            with open(os.path.join(data, "Orders.csv"), encoding="utf-8",
                      newline="") as stream:
                self.assertEqual(next(csv.reader(stream)), ["Order ID"])

    def test_extract_csv_name_follows_hyper_file_not_inner_table(self):
        import csv
        with tempfile.TemporaryDirectory() as out:
            hyper = os.path.join(out, "federated_abc.hyper")
            _write(hyper, b"not a real hyper")
            name = os.path.splitext(os.path.basename(hyper))[0]
            self.assertTrue(migrate._hyper_to_csv_files(hyper, out, NAME, csv) == {}
                            or all(k.startswith(name) for k in
                                   migrate._hyper_to_csv_files(hyper, out, NAME, csv)))


class TestHyperPhysicalIdentityRegressions(unittest.TestCase):
    """Only exact packaged physical identities may select an extract."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.workbook = os.path.join(self.root, "synthetic.twbx")
        self.package_identity = {
            "workbook": self.workbook,
            "archive_member": "Extracts/Case-sensitive/data.hyper",
        }

    def _identity(self, schema="Extract", table="Physical"):
        return dict(self.package_identity, schema=schema, table=table)

    def _routing_project(self, identities):
        project = _project(self.root, {"Model": _excel_partition("missing.xlsx", ["id"])})
        data = os.path.join(project, "Data")
        extract = os.path.join(self.root, "extract")
        _write(os.path.join(extract, "datasources.json"), json.dumps([
            {"name": f"source-{index}", "tables": [{
                "name": "Model", "source_table": "legacy",
                **({"hyper_identity": identity} if identity is not None else {}),
            }]}
            for index, identity in enumerate(identities)
        ]))
        patcher = mock.patch.object(migrate, "_get_extract_dir", return_value=extract)
        patcher.start()
        self.addCleanup(patcher.stop)
        return project, data

    def _manifest(self, data, records):
        return _write(os.path.join(data, "hyper_exports.json"),
                      json.dumps({"version": 1, "exports": records}))

    def test_hyper_api_preserves_exact_catalog_identity_and_ordered_columns(self):
        columns = ['zéro', 'A "quoted"', "last"]
        long_name = 'Exact."table"-%-Name-' + "Ab" * 180
        catalog_pairs = [
            ('Schema."A"', long_name), ('schema."a"', long_name),
            ('Schema."A"', long_name.lower()),
        ]
        # SchemaName wraps Name; unlike Name it has no .unescaped attribute.
        schemas = [
            SimpleNamespace(name=SimpleNamespace(unescaped=name))
            for name in ('Schema."A"', 'schema."a"')
        ]
        tables = [
            SimpleNamespace(schema_name=next(
                                s for s in schemas if s.name.unescaped == schema),
                            name=SimpleNamespace(unescaped=table))
            for schema, table in catalog_pairs
        ]
        connection = mock.Mock()
        connection.catalog.get_schema_names.return_value = schemas
        connection.catalog.get_table_names.side_effect = (
            lambda schema: [t for t in tables if t.schema_name is schema]
        )
        connection.catalog.get_table_definition.return_value = SimpleNamespace(
            columns=[SimpleNamespace(name=SimpleNamespace(unescaped=c)) for c in columns]
        )
        connection.execute_list_query.return_value = [[1, "synthetic", 3]]
        api = SimpleNamespace(
            HyperProcess=mock.Mock(return_value=mock.Mock(endpoint="synthetic")),
            Telemetry=SimpleNamespace(DO_NOT_SEND_USAGE_DATA_TO_TABLEAU=0),
            Connection=mock.Mock(return_value=connection),
        )
        with mock.patch.dict("sys.modules", {"tableauhyperapi": api}), \
                mock.patch("sqlite3.connect") as sqlite_reader, \
                mock.patch("tableau_export.hyper_reader.read_hyper") as fallback_reader:
            result = migrate._hyper_to_csv_files(
                os.path.join(self.root, "staged.hyper"), self.root, NAME, csv,
                identity=self.package_identity,
            )
            connection.close.assert_called_once()
            migrate._convert_hyper_to_csv_in_data(
                self.root, NAME, self.root,
                packaged_hyper=[{
                    "path": os.path.join(self.root, "staged.hyper"),
                    "identity": self.package_identity,
                }],
            )
            sqlite_reader.assert_not_called()
            fallback_reader.assert_not_called()
        with open(os.path.join(self.root, "hyper_exports.json"),
                  encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), {"version": 1, "exports": result.exports})
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result.exports), 3)
        self.assertEqual(
            {(r["identity"]["schema"], r["identity"]["table"]) for r in result.exports},
            set(catalog_pairs),
        )
        self.assertEqual(len({r["csv_filename"] for r in result.exports}), 3)
        for record in result.exports:
            self.assertEqual(set(record["identity"]),
                             {"workbook", "archive_member", "schema", "table"})
            self.assertEqual(record["identity"]["workbook"], self.workbook)
            self.assertEqual(record["identity"]["archive_member"],
                             self.package_identity["archive_member"])
            self.assertEqual(record["columns"], columns)
            self.assertRegex(record["csv_filename"], r"^hyper_[0-9a-f]{64}\.csv$")
            with open(os.path.join(self.root, record["csv_filename"]),
                      encoding="utf-8", newline="") as stream:
                self.assertEqual(list(csv.reader(stream)), [columns, ["1", "synthetic", "3"]])
        self.assertEqual(connection.close.call_count, 2)

    def test_project_reader_fallback_preserves_long_identity_and_column_order(self):
        names = ["Long." + "Ab-" * 120, "Long." + "ab-" * 120]
        columns = ["z", "réalisé (%)", "a"]
        tables = [
            {"schema": "Extract", "table": name,
             "columns": [{"name": column, "type": "text"} for column in columns],
             "sample_rows": [[1, 2, 3]]}
            for name in names
        ]
        def export(table, out_dir, csv_filename):
            return _write(os.path.join(out_dir, csv_filename), "z,réalisé (%),a\n1,2,3\n")

        with mock.patch.dict("sys.modules", {"tableauhyperapi": None}), \
                mock.patch("sqlite3.connect", side_effect=OSError("synthetic non-SQLite file")), \
                mock.patch("tableau_export.hyper_reader.read_hyper",
                           return_value={"tables": tables}), \
                mock.patch("tableau_export.hyper_reader.export_hyper_to_csv",
                           side_effect=export) as writer:
            result = migrate._hyper_to_csv_files(
                "synthetic.hyper", self.root, NAME, csv, identity=self.package_identity
            )
        self.assertEqual(writer.call_count, 2)
        self.assertEqual([r["identity"] for r in result.exports],
                         [self._identity(table=name) for name in names])
        self.assertEqual([r["columns"] for r in result.exports], [columns, columns])
        self.assertEqual(len(set(result.values())), 2)
        for filename in result.values():
            self.assertRegex(filename, r"^hyper_[0-9a-f]{64}\.csv$")

    def test_explicit_package_reads_exact_path_despite_prefix_csv_and_writes_manifest(self):
        project, data = self._routing_project([None])
        path = _write(os.path.join(project, "Elsewhere", "data.hyper"), b"synthetic")
        _write(os.path.join(data, "data_old.csv"), "id\nwrong\n")
        record = {"identity": self._identity(), "csv_filename": "hyper_" + "a" * 64 + ".csv",
                  "columns": ["id"]}
        _write(os.path.join(data, record["csv_filename"]), "id\nright\n")
        converted = migrate._HyperCsvExports()
        converted[os.path.splitext(record["csv_filename"])[0]] = record["csv_filename"]
        converted.exports = [record]
        with mock.patch.object(migrate, "_hyper_to_csv_files", return_value=converted) as reader:
            migrate._convert_hyper_to_csv_in_data(
                data, NAME, project,
                packaged_hyper=[{"path": path, "identity": self.package_identity}],
            )
        reader.assert_called_once_with(path, data, NAME, csv, identity=self.package_identity)
        with open(os.path.join(data, "hyper_exports.json"), encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), {"version": 1, "exports": [record]})

    def test_failed_reader_publishes_empty_authoritative_manifest(self):
        project, data = self._routing_project([None])
        path = _write(os.path.join(data, "data.hyper"), b"synthetic")
        _write(os.path.join(data, "data_old.csv"), "id\nwrong\n")
        before = _read(project, "Model")
        with mock.patch.object(migrate, "_hyper_to_csv_files",
                               return_value=migrate._HyperCsvExports()) as reader:
            migrate._convert_hyper_to_csv_in_data(
                data, NAME, project,
                packaged_hyper=[{"path": path, "identity": self.package_identity}],
            )
        reader.assert_called_once()
        with open(os.path.join(data, "hyper_exports.json"), encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), {"version": 1, "exports": []})
        self.assertEqual(_read(project, "Model"), before)
        with mock.patch.object(migrate, "_hyper_to_csv_files") as second_reader:
            migrate._convert_hyper_to_csv_in_data(data, NAME, project)
        second_reader.assert_not_called()
        self.assertEqual(_read(project, "Model"), before)

    def test_identical_headers_route_only_by_exact_identity_and_manifest_on_repeat(self):
        identity = self._identity(table="ExactCase")
        project, data = self._routing_project([identity])
        records = [
            {"identity": self._identity(table=name),
             "csv_filename": "hyper_" + digit * 64 + ".csv", "columns": ["id"]}
            for name, digit in (("exactcase", "a"), ("ExactCase", "b"))
        ]
        for record in records:
            _write(os.path.join(data, record["csv_filename"]), "id\nsynthetic\n")
        self._manifest(data, records)
        with mock.patch.object(migrate, "_hyper_to_csv_files") as reader:
            migrate._convert_hyper_to_csv_in_data(data, NAME, project)
            self.assertIn(records[1]["csv_filename"], _read(project, "Model"))
            self.assertNotIn(records[0]["csv_filename"], _read(project, "Model"))
            first = _read(project, "Model")
            migrate._convert_hyper_to_csv_in_data(data, NAME, project)
        reader.assert_not_called()
        self.assertEqual(_read(project, "Model"), first)

    def test_missing_incomplete_or_foreign_identity_never_uses_matching_headers(self):
        for identity in (
            None, {"table": "Physical"},
            dict(self._identity(), archive_member="other/data.hyper"),
            dict(self._identity(), workbook=self.workbook.upper()),
        ):
            with self.subTest(identity=identity):
                project, data = self._routing_project([identity])
                filename = "hyper_" + "a" * 64 + ".csv"
                _write(os.path.join(data, filename), "id\nwrong\n")
                _write(os.path.join(data, "legacy.csv"), "id\nwrong\n")
                self._manifest(data, [{
                    "identity": self._identity(), "csv_filename": filename, "columns": ["id"],
                }])
                before = _read(project, "Model")
                migrate._convert_hyper_to_csv_in_data(data, NAME, project)
                self.assertEqual(_read(project, "Model"), before)

    def test_duplicate_identity_exports_are_ambiguous_not_header_routed(self):
        project, data = self._routing_project([self._identity()])
        records = []
        for digit in ("a", "b"):
            filename = "hyper_" + digit * 64 + ".csv"
            _write(os.path.join(data, filename), "id\nsynthetic\n")
            records.append({"identity": self._identity(), "csv_filename": filename,
                            "columns": ["id"]})
        self._manifest(data, records)
        before = _read(project, "Model")
        migrate._convert_hyper_to_csv_in_data(data, NAME, project)
        self.assertEqual(_read(project, "Model"), before)

    def test_same_model_name_with_distinct_datasource_identities_is_ambiguous(self):
        identities = [self._identity(table=name) for name in ("One", "Two")]
        project, data = self._routing_project(identities)
        records = []
        for identity, digit in zip(identities, ("a", "b")):
            filename = "hyper_" + digit * 64 + ".csv"
            _write(os.path.join(data, filename), "id\nsynthetic\n")
            records.append({"identity": identity, "csv_filename": filename, "columns": ["id"]})
        self._manifest(data, records)
        before = _read(project, "Model")
        migrate._convert_hyper_to_csv_in_data(data, NAME, project)
        self.assertEqual(_read(project, "Model"), before)

    def test_malformed_or_unsupported_manifest_blocks_legacy_fallback(self):
        for content in ("not JSON", "[]", '{"version":2,"exports":[]}', '{"version":1,"exports":{}}'):
            with self.subTest(content=content):
                project, data = self._routing_project([None])
                _write(os.path.join(data, "legacy.hyper"), b"synthetic")
                _write(os.path.join(data, "legacy.csv"), "id\nwrong\n")
                _write(os.path.join(data, "hyper_exports.json"), content)
                before = _read(project, "Model")
                with mock.patch.object(migrate, "_hyper_to_csv_files") as reader:
                    migrate._convert_hyper_to_csv_in_data(data, NAME, project)
                reader.assert_not_called()
                self.assertEqual(_read(project, "Model"), before)

    def test_caption_staging_preserves_raw_export_and_parent_identity(self):
        project, data = self._routing_project([self._identity()])
        extract = migrate._get_extract_dir()
        _write(os.path.join(extract, "datasources.json"), json.dumps([{"tables": [{
            "name": "Model", "hyper_identity": self._identity(),
            "cols_physical_map": {"id": "Identifier"},
        }]}]))
        body = (
            '\t\t\t\tlet\n'
            '\t\t\t\t\tSource = try #table({"Identifier"}, {}) '
            'otherwise #table({"Identifier"}, {})\n'
            '\t\t\t\tin\n\t\t\t\t\tSource'
        )
        project = _project(self.root, {"Model": body})
        filename = "hyper_" + "a" * 64 + ".csv"
        raw = "id\nsynthetic\n"
        _write(os.path.join(data, filename), raw)
        record = {"identity": self._identity(), "csv_filename": filename, "columns": ["id"]}
        self._manifest(data, [record])
        migrate._convert_hyper_to_csv_in_data(data, NAME, project)
        with open(os.path.join(data, "hyper_exports.json"), encoding="utf-8") as stream:
            manifest = json.load(stream)
        exported = manifest["exports"][0]
        self.assertEqual(exported["identity"], self._identity())
        self.assertEqual(exported["csv_filename"], filename)
        self.assertEqual(exported["columns"], ["id"])
        self.assertEqual(len(exported["staged_files"]), 1)
        staged = exported["staged_files"][0]
        self.assertEqual(staged["table"], "Model")
        self.assertRegex(staged["csv_filename"], r"^hyper_staged_[0-9a-f]{64}\.csv$")
        self.assertIn(staged["csv_filename"], _read(project, "Model"))
        with open(os.path.join(data, filename), encoding="utf-8") as stream:
            self.assertEqual(stream.read(), raw)
        with open(os.path.join(data, staged["csv_filename"]), encoding="utf-8") as stream:
            self.assertEqual(stream.read(), "Identifier\nsynthetic\n")
        migrate._convert_hyper_to_csv_in_data(data, NAME, project)
        with open(os.path.join(data, "hyper_exports.json"), encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), manifest)

    def test_post_generation_passes_exact_members_without_installing_reader(self):
        members = ["Data/a/shared.hyper", "Outside/b/shared.hyper"]
        with zipfile.ZipFile(self.workbook, "w") as archive:
            archive.writestr("synthetic.twb", "<workbook/>")
            for index, member in enumerate(members):
                archive.writestr(member, f"synthetic-{index}")
        project = _project(self.root, {})
        def check_staged_members(*args, **kwargs):
            for index, member in enumerate(members):
                with open(os.path.join(project, member), "rb") as stream:
                    self.assertEqual(stream.read(), f"synthetic-{index}".encode())

        with mock.patch.object(migrate, "_convert_hyper_to_csv_in_data",
                               side_effect=check_staged_members) as converter, \
                mock.patch("subprocess.run") as installer, \
                mock.patch.object(migrate, "_export_power_query_files"), \
                mock.patch.object(migrate, "_export_dax_files"):
            migrate._process_twbx_post_generation(self.workbook, project, NAME)
        converter.assert_called_once_with(
            os.path.join(project, "Data"), NAME, project,
            packaged_hyper=[
                {"path": os.path.join(project, member),
                 "identity": {"workbook": self.workbook, "archive_member": member}}
                for member in members
            ],
        )
        installer.assert_not_called()


class TestHyperPlaceholderHandshakeRegressions(unittest.TestCase):
    """Exact producer identity must survive semantic placeholders and staging."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic réalisé ")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.data = os.path.join(self.root, "Data réalisé")
        os.makedirs(self.data)
        self.identity = {
            "workbook": os.path.join(self.root, "synthetic.twbx"),
            "archive_member": "Outside/Exact.hyper",
            "schema": "Extract.Case",
            "table": 'Physical "réalisé"',
        }
        self.table = "O'Brien réalisé"
        self.filename = "hyper_" + "a" * 64 + ".csv"
        self.record = {
            "identity": dict(self.identity), "csv_filename": self.filename,
            "columns": ["id", "Amount"],
        }
        _write(os.path.join(self.data, self.filename), "id,Amount\n1,2.5\n")

    def _datasource(self, name="source-a", identity=None, columns=None):
        return {
            "name": name, "connection": {"type": "hyper", "details": {}},
            "connection_map": {}, "calculations": [], "relationships": [], "columns": [],
            "tables": [{
                "name": self.table,
                "hyper_identity": dict(self.identity if identity is None else identity),
                "columns": columns or [
                    {"name": "id", "datatype": "integer"},
                    {"name": "Amount", "datatype": "real"},
                ],
            }],
        }

    def _object_graph_datasource(self):
        """Build the bounded native-logical/packaged-physical Tableau shape."""
        import xml.etree.ElementTree as ET

        datasource = ET.Element("datasource", {"name": "object-graph-source"})
        native_root = ET.SubElement(datasource, "connection", {"class": "federated"})
        named = ET.SubElement(native_root, "named-connection", {
            "name": "native-ref",
        })
        ET.SubElement(named, "connection", {
            "class": "textscan", "filename": "missing-native-source.csv",
            "directory": "unavailable-native-source", "separator": ",",
        })
        cols = ET.SubElement(native_root, "cols")
        ET.SubElement(cols, "map", {
            "key": "[Identifier]", "value": "[Extract].[raw_id]",
        })

        extract = ET.SubElement(datasource, "extract", {"enabled": "true"})
        extract_connection = ET.SubElement(extract, "connection", {
            "class": "hyper",
            "dbname": self.identity["archive_member"],
            "schema": self.identity["schema"],
            "tablename": self.identity["table"],
        })
        physical = ET.SubElement(extract_connection, "relation", {
            "name": "Extract", "type": "table",
            "table": f'[{self.identity["schema"]}].[{self.identity["table"]}]',
        })
        ET.SubElement(physical, "columns")

        graph = ET.SubElement(datasource, "object-graph")
        objects = ET.SubElement(graph, "objects")
        obj = ET.SubElement(objects, "object", {"id": "synthetic-object-id"})
        native_props = ET.SubElement(obj, "properties", {"context": ""})
        logical = ET.SubElement(native_props, "relation", {
            "name": "Data", "type": "table", "connection": "native-ref",
            "table": "[Data$]",
        })
        native_columns = ET.SubElement(logical, "columns")
        ET.SubElement(native_columns, "column", {
            "name": "Identifier", "datatype": "integer", "ordinal": "0",
        })
        extract_props = ET.SubElement(obj, "properties", {"context": "extract"})
        graph_physical = ET.SubElement(extract_props, "relation", {
            "name": "Extract", "type": "table",
            "table": f'[{self.identity["schema"]}].[{self.identity["table"]}]',
        })
        physical_columns = ET.SubElement(graph_physical, "columns")
        ET.SubElement(physical_columns, "column", {
            "name": "raw_id", "datatype": "integer", "ordinal": "0",
        })
        return datasource

    def _extract_object_graph(self, mutate=None):
        from tableau_export.datasource_extractor import extract_datasource

        datasource = self._object_graph_datasource()
        if mutate is not None:
            mutate(datasource)
        with zipfile.ZipFile(self.identity["workbook"], "w") as archive:
            archive.writestr(self.identity["archive_member"], b"synthetic catalog fixture")
        return extract_datasource(datasource, self.identity["workbook"])

    def _model(self, datasources=None, extra=None):
        import io
        from contextlib import redirect_stdout
        from powerbi_import.tmdl_generator import _build_semantic_model
        with redirect_stdout(io.StringIO()):
            return _build_semantic_model(
                datasources or [self._datasource()], NAME, extra or {}
            )

    def _content(self, table=None):
        from powerbi_import.tmdl_generator import _write_table_tmdl
        table = table or self._model()["model"]["tables"][0]
        directory = os.path.join(self.root, "written")
        os.makedirs(directory, exist_ok=True)
        _write_table_tmdl(directory, table)
        paths = [os.path.join(directory, filename) for filename in os.listdir(directory)
                 if filename.endswith(".tmdl")]
        self.assertEqual(len(paths), 1)
        with open(paths[0], encoding="utf-8") as stream:
            return stream.read()

    @staticmethod
    def _expression(content):
        return content.split("\n\t\tsource =\n", 1)[1].split(
            "\n\tannotation", 1
        )[0].strip()

    def test_identity_prevents_inline_hyper_sample_overwriting_pending_error(self):
        from powerbi_import import tmdl_generator
        extra = {"hyper_files": [{"hyper_reader_tables": [{
            "table": self.table, "schema": "Foreign",
            "columns": [{"name": "id", "type": "integer"}],
            "sample_rows": [[999]],
        }]}]}
        with mock.patch.object(tmdl_generator, "generate_m_from_hyper",
                               return_value="#table({\"id\"}, {{999}})") as inline:
            model = self._model(extra=extra)
        table = model["model"]["tables"][0]
        self.assertEqual(table["hyper_identity"], self.identity)
        expression = table["partitions"][0]["source"]["expression"]
        self.assertIn("Migration.HyperExportRequired", expression)
        self.assertNotIn("999", expression)
        inline.assert_not_called()

    def test_extracted_identity_survives_datasource_json_model_and_csv_staging(self):
        import xml.etree.ElementTree as ET
        from tableau_export.datasource_extractor import extract_datasource
        datasource = ET.Element("datasource", {"name": "source-a"})
        extract = ET.SubElement(datasource, "extract")
        connection = ET.SubElement(extract, "connection", {
            "class": "hyper", "dbname": self.identity["archive_member"],
        })
        ET.SubElement(connection, "relation", {
            "name": self.table, "type": "table",
            "table": f'[{self.identity["schema"]}].[{self.identity["table"]}]',
        })
        for name, datatype in (("id", "integer"), ("Amount", "real")):
            ET.SubElement(datasource, "column", {
                "name": f"[{name}]", "datatype": datatype,
                "role": "measure", "type": "quantitative",
            })
        with zipfile.ZipFile(self.identity["workbook"], "w") as archive:
            archive.writestr(self.identity["archive_member"], b"synthetic catalog fixture")
        produced = extract_datasource(datasource, self.identity["workbook"])
        path = _write(os.path.join(self.root, "extract", "datasources.json"),
                      json.dumps([produced], ensure_ascii=False))
        with open(path, encoding="utf-8") as stream:
            persisted = json.load(stream)
        self.assertEqual(persisted[0]["tables"][0]["hyper_identity"], self.identity)
        self.assertEqual(set(persisted[0]["tables"][0]["hyper_identity"]),
                         {"workbook", "archive_member", "schema", "table"})
        table = self._model(persisted)["model"]["tables"][0]
        self.assertEqual(table["hyper_identity"], self.record["identity"])
        original = self._content(table)
        bound = migrate._stage_pending_hyper_partition(original, self.data, [self.record])
        self.assertIn(self.filename, bound)
        self.assertNotIn("Migration.HyperExportRequired", bound)

    def test_object_graph_identity_roundtrips_to_tmdl_and_stages_exact_export(self):
        from powerbi_import.tmdl_generator import _audit_local_partition_sources

        extracted = self._extract_object_graph()
        self.assertEqual([table["name"] for table in extracted["tables"]], ["Data"])
        self.assertEqual(extracted["tables"][0]["hyper_identity"], self.identity)
        self.assertEqual(extracted["tables"][0]["cols_physical_map"],
                         {"raw_id": "Identifier"})

        serialized = json.dumps([extracted], ensure_ascii=False)
        persisted = json.loads(serialized)
        semantic = self._model(persisted)
        table = semantic["model"]["tables"][0]
        self.assertEqual(table["name"], "Data")
        self.assertEqual(table["hyper_identity"], self.identity)
        content = self._content(table)
        self.assertIn("Migration.HyperExportRequired", content)

        # This source would be reported as a missing local file if the bound
        # identity were lost before the native-source audit.
        self.assertEqual(_audit_local_partition_sources(semantic, self.root, persisted), [])
        unbound_sources = json.loads(serialized)
        unbound_sources[0]["tables"][0].pop("hyper_identity")
        unbound_model = self._model(unbound_sources)
        self.assertTrue(_audit_local_partition_sources(
            unbound_model, self.root, unbound_sources
        ))
        record = {
            "identity": dict(self.identity), "csv_filename": self.filename,
            "columns": ["Identifier"],
        }
        _write(os.path.join(self.data, self.filename), "Identifier\n17\n")
        bound = migrate._stage_pending_hyper_partition(content, self.data, [record])
        self.assertNotIn("Migration.HyperExportRequired", bound)
        self.assertIn("Table.PromoteHeaders(Csv.Document(", bound)
        self.assertIn('{"Identifier", Int64.Type}', bound)
        self.assertIn(self.filename, bound)

    def test_object_graph_rejects_incomplete_or_ambiguous_extract_shapes(self):
        def find_extract(datasource):
            return datasource.find("extract")

        def remove_extract_connection(datasource):
            extract = find_extract(datasource)
            extract.remove(extract.find("connection"))

        def add_second_extract_connection(datasource):
            extract = find_extract(datasource)
            import xml.etree.ElementTree as ET
            ET.SubElement(extract, "connection", {
                "class": "hyper", "dbname": self.identity["archive_member"],
                "schema": self.identity["schema"],
                "tablename": self.identity["table"],
            })

        def add_second_object(datasource):
            import xml.etree.ElementTree as ET
            objects = datasource.find("object-graph/objects")
            ET.SubElement(objects, "object", {"id": "second-object"})

        def add_join(datasource):
            import xml.etree.ElementTree as ET
            ET.SubElement(datasource, "relation", {
                "name": "Joined", "type": "join",
            })

        def add_union(datasource):
            import xml.etree.ElementTree as ET
            ET.SubElement(datasource, "relation", {
                "name": "Unioned", "type": "union",
            })

        def add_conflicting_native_declaration(datasource):
            import xml.etree.ElementTree as ET
            native_root = datasource.find("connection")
            ET.SubElement(native_root, "relation", {
                "name": "Data", "type": "table", "connection": "native-ref",
                "table": "[DifferentData$]",
            })

        def add_ambiguous_native_connection(datasource):
            import xml.etree.ElementTree as ET
            native_root = datasource.find("connection")
            named = ET.SubElement(native_root, "named-connection", {
                "name": "native-ref",
            })
            ET.SubElement(named, "connection", {"class": "sqlserver"})

        def add_ambiguous_physical_name(datasource):
            import xml.etree.ElementTree as ET
            ET.SubElement(datasource.find("connection"), "relation", {
                "name": "Extract", "type": "table",
                "table": f'[{self.identity["schema"]}].[{self.identity["table"]}]',
            })

        mutations = (
            ("disabled extract", lambda ds: find_extract(ds).set("enabled", "false")),
            ("missing enabled flag", lambda ds: find_extract(ds).attrib.pop("enabled")),
            ("missing Hyper class", lambda ds: find_extract(ds).find("connection").attrib.pop("class")),
            ("missing member", lambda ds: find_extract(ds).find("connection").attrib.pop("dbname")),
            ("missing schema", lambda ds: find_extract(ds).find("connection").attrib.pop("schema")),
            ("missing tablename", lambda ds: find_extract(ds).find("connection").attrib.pop("tablename")),
            ("missing connection", remove_extract_connection),
            ("multiple connections", add_second_extract_connection),
            ("schema/table mismatch", lambda ds: find_extract(ds).find(
                "connection/relation").set("table", "[Other].[Physical]")),
            ("multiple objects", add_second_object),
            ("join", add_join),
            ("union", add_union),
            ("conflicting native declaration", add_conflicting_native_declaration),
            ("ambiguous native connection name", add_ambiguous_native_connection),
            ("ambiguous physical name", add_ambiguous_physical_name),
        )
        for label, mutation in mutations:
            with self.subTest(shape=label):
                extracted = self._extract_object_graph(mutation)
                by_name = {table["name"]: table for table in extracted["tables"]}
                self.assertIn("Data", by_name)
                if label == "disabled extract":
                    self.assertNotIn("Extract", by_name)
                    self.assertEqual(extracted["inactive_extract_context"]["selection"],
                                     "native_no_enabled_extract")
                    self.assertTrue(extracted["inactive_extract_context"]["extract_xml"])
                    self.assertTrue(extracted["inactive_extract_context"][
                        "object_extract_contexts_xml"])
                else:
                    self.assertIn("Extract", by_name)
                self.assertNotIn("hyper_identity", by_name["Data"])

    def test_object_graph_cleanup_never_deletes_an_independent_physical_table(self):
        import zipfile
        from tableau_export.datasource_extractor import (
            _bind_object_context_extract, extract_tables_with_columns,
        )

        datasource = self._object_graph_datasource()
        with zipfile.ZipFile(self.identity["workbook"], "w") as archive:
            archive.writestr(self.identity["archive_member"], b"synthetic catalog fixture")
        tables = extract_tables_with_columns(datasource)
        independent = {
            "name": "Independent", "type": "table", "columns": [],
            "connection": "", "connection_details": {}, "source_table": "Lookup",
        }
        tables.append(independent)
        with zipfile.ZipFile(self.identity["workbook"]) as archive:
            members = archive.namelist()
        _bind_object_context_extract(
            datasource, tables, self.identity["workbook"], members,
        )
        self.assertEqual([table["name"] for table in tables], ["Data", "Independent"])
        self.assertIs(tables[1], independent)
        self.assertEqual(tables[0]["hyper_identity"], self.identity)

    def test_actual_semantic_placeholder_stages_typed_valid_m_and_is_idempotent(self):
        from powerbi_import.m_validator import validate_m_query
        content = self._content()
        self.assertEqual(content.count("Migration.HyperExportRequired"), 1)
        self.assertIn("table 'O''Brien réalisé'", content)
        records = [self.record]
        bound = migrate._stage_pending_hyper_partition(content, self.data, records)
        self.assertNotEqual(bound, content)
        self.assertNotIn("Migration.HyperExportRequired", bound)
        self.assertIn("Table.PromoteHeaders(Csv.Document(", bound)
        self.assertIn('{"id", Int64.Type}', bound)
        self.assertIn('{"Amount", type number}', bound)
        self.assertIn(self.filename, bound)
        expression = self._expression(bound)
        self.assertEqual(validate_m_query(expression), [])
        self.assertEqual(records[0]["staged_files"], [{
            "table": self.table, "csv_filename": self.filename,
        }])
        self.assertEqual(
            migrate._stage_pending_hyper_partition(bound, self.data, records), bound
        )
        self.assertEqual(
            migrate._stage_pending_hyper_partition(content, self.data, records), bound
        )
        self.assertEqual(len(records[0]["staged_files"]), 1)

    def _transformed_datasource(self):
        datasource = self._datasource(columns=[
            {"name": "Code", "datatype": "string"},
            {"name": "Amount", "datatype": "real"},
        ])
        datasource["tables"][0]["cols_physical_map"] = {
            "raw_code": "Code", "Amount": "Amount",
        }
        datasource["columns"] = [
            {"name": "Code", "datatype": "string", "caption": "Display Code"},
            {"name": "Amount", "datatype": "real"},
        ]
        # Exercise the real calculation converter, not a mocked AddColumn step.
        datasource["calculations"] = [{
            "name": "DoubleAmount", "caption": "Double Amount",
            "formula": "[Amount] * 2", "role": "measure", "datatype": "real",
        }]
        return datasource

    def test_pending_source_is_seeded_before_build_table_and_keeps_real_m_steps(self):
        from powerbi_import import tmdl_generator
        from powerbi_import.m_validator import validate_m_query

        self.table = "Synthetic Facts réalisé"
        extra = {
            "groups": [{
                "name": "Code Group", "source_field": "Display Code",
                "members": {"Alpha": ["A"]},
            }],
            "bins": [{
                "name": "Amount Bin", "source_field": "Amount", "bin_size": 10,
            }],
        }
        with mock.patch.object(tmdl_generator, "_build_table",
                               wraps=tmdl_generator._build_table) as builder, \
                mock.patch.object(tmdl_generator, "generate_power_query_m",
                                  side_effect=AssertionError("must use pending Source")):
            model = self._model([self._transformed_datasource()], extra)
        builder.assert_called_once()
        override = builder.call_args.kwargs["m_query_override"]
        self.assertTrue(override.startswith("let\n    Source = error Error.Record("))
        self.assertIn("Migration.HyperExportRequired", override)
        self.assertIn('Table.RenameColumns(Source, {{"raw_code", "Code"}})', override)

        table = model["model"]["tables"][0]
        self.assertEqual(table["hyper_identity"], self.identity)
        self.assertEqual(table["partitions"][0]["source"]["type"], "m")
        expression = table["partitions"][0]["source"]["expression"]
        expected_steps = [
            '#"Renamed Hyper Columns" = Table.RenameColumns(Source, {{"raw_code", "Code"}})',
            '#"Renamed Columns" = Table.RenameColumns(#"Renamed Hyper Columns", '
            '{{"Code", "Display Code"}})',
            '#"Added Double Amount" = Table.AddColumn(#"Renamed Columns", '
            '"Double Amount", each ([Amount] * 2), type number)',
            '#"Added Code Group" = Table.AddColumn(#"Added Double Amount", '
            '"Code Group", each if [Display Code] = "A" then "Alpha" else "Other", type text)',
            '#"Added Amount Bin" = Table.AddColumn(#"Added Code Group", '
            '"Amount Bin", each (Number.RoundDown([Amount] / 10) * 10), type number)',
        ]
        for step in expected_steps:
            self.assertIn(step, expression)
        self.assertTrue(expression.endswith('in\n    Result'))
        self.assertNotIn("otherwise", expression)
        self.assertNotIn("#table", expression)
        self.assertEqual(validate_m_query(expression), [])
        columns = {column["name"]: column for column in table["columns"]}
        self.assertEqual(set(columns), {
            "Display Code", "Amount", "Double Amount", "Code Group", "Amount Bin",
        })
        for name, column in columns.items():
            self.assertEqual(column["sourceColumn"], name)
            self.assertNotIn("expression", column)

        # Persist a synthetic export manifest and CSV with exact physical headers.
        record = dict(self.record, columns=["raw_code", "Amount"])
        _write(os.path.join(self.data, self.filename), "raw_code,Amount\nA,12.5\n")
        manifest_path = _write(
            os.path.join(self.data, "hyper_exports.json"),
            json.dumps({"version": 1, "exports": [record]}, ensure_ascii=False),
        )
        with open(manifest_path, encoding="utf-8") as stream:
            records = json.load(stream)["exports"]
        original = self._content(table)
        bound = migrate._stage_pending_hyper_partition(original, self.data, records)
        self.assertNotEqual(bound, original)
        self.assertNotIn("Migration.HyperExportRequired", bound)
        self.assertIn('Source = Table.TransformColumnTypes(Table.PromoteHeaders('
                      'Csv.Document(File.Contents(DataFolder & ', bound)
        self.assertIn('{"Amount", type number}', bound)
        self.assertIn(self.filename, bound)
        self.assertIn("QuoteStyle=QuoteStyle.Csv", bound)
        self.assertNotIn("otherwise", bound)
        self.assertNotIn("#table", bound)
        for step in expected_steps:
            self.assertIn(step, bound)
        # Staging must replace only Source's Error.Record, not rebuild the query.
        tail_start = '#"Renamed Hyper Columns" = '
        self.assertEqual(original.count(tail_start), 1)
        self.assertEqual(bound.count(tail_start), 1)
        self.assertEqual(original.split(tail_start, 1)[1],
                         bound.split(tail_start, 1)[1])
        self.assertEqual(validate_m_query(self._expression(bound)), [])
        self.assertEqual(records[0]["staged_files"], [{
            "table": self.table, "csv_filename": self.filename,
        }])
        self.assertEqual(migrate._stage_pending_hyper_partition(bound, self.data, records),
                         bound)

    def test_transformed_pending_partition_stays_an_error_without_exact_export(self):
        self.table = "Synthetic Facts réalisé"
        table = self._model([self._transformed_datasource()])["model"]["tables"][0]
        original = self._content(table)
        record = dict(self.record, identity=dict(self.identity, table="wrong-physical"),
                      columns=["raw_code", "Amount"])
        _write(os.path.join(self.data, self.filename), "raw_code,Amount\nA,12.5\n")
        self.assertEqual(
            migrate._stage_pending_hyper_partition(original, self.data, [record]),
            original,
        )
        self.assertIn("Source = error Error.Record(", original)
        self.assertIn('Table.AddColumn(#"Renamed Columns", "Double Amount", '
                      'each ([Amount] * 2), type number)', original)
        self.assertNotIn("otherwise", original)
        self.assertNotIn("#table", original)
        self.assertNotIn("staged_files", record)

    def test_exact_case_column_metadata_and_source_types_do_not_leak(self):
        from powerbi_import.m_validator import validate_m_query

        metadata = [
            {"name": "siren", "datatype": "integer", "description": "lower-case number"},
            {"name": "Siren", "datatype": "string", "description": "case-exact text"},
        ]
        for reverse in (False, True):
            with self.subTest(reverse_metadata=reverse):
                datasource = self._datasource(columns=[
                    {"name": "Siren", "datatype": "string"},
                    {"name": "siren", "datatype": "integer"},
                ])
                datasource["columns"] = metadata[::-1] if reverse else metadata
                table = self._model([datasource])["model"]["tables"][0]
                # Power BI model names are case-insensitive; the lower-case
                # field receives a display suffix but keeps its exact source.
                columns = {column["sourceColumn"]: column for column in table["columns"]}
                self.assertEqual(set(columns), {"Siren", "siren"})
                self.assertEqual(columns["Siren"]["name"], "Siren")
                self.assertEqual(columns["siren"]["name"], "siren (source)")
                self.assertEqual(columns["Siren"]["dataType"], "String")
                self.assertEqual(columns["siren"]["dataType"], "Int64")
                self.assertEqual(columns["Siren"]["description"], "case-exact text")
                self.assertEqual(columns["siren"]["description"], "lower-case number")
                self.assertEqual(columns["Siren"]["sourceColumn"], "Siren")
                self.assertEqual(columns["siren"]["sourceColumn"], "siren")
                record = dict(self.record, columns=["Siren", "siren"])
                _write(os.path.join(self.data, self.filename), "Siren,siren\n00123,7\n")
                bound = migrate._stage_pending_hyper_partition(
                    self._content(table), self.data, [record],
                )
                self.assertNotIn("Migration.HyperExportRequired", bound)
                self.assertIn('{"Siren", type text}', bound)
                self.assertIn('{"siren", Int64.Type}', bound)
                self.assertNotIn('{"Siren", Int64.Type}', bound)
                self.assertNotIn('{"siren", type text}', bound)
                self.assertNotIn("otherwise", bound)
                self.assertEqual(validate_m_query(self._expression(bound)), [])

    def test_explicit_prep_overrides_win_without_losing_caption_or_calculation(self):
        from powerbi_import import tmdl_generator
        from powerbi_import.m_validator import validate_m_query

        self.table = "Synthetic Facts réalisé"
        prep = ('let\n    Source = error Error.Record("Synthetic.PrepFailure", '
                '"Prep source failed", []),\n'
                '    #"Prep Kept" = Table.SelectRows(Source, each [Amount] > 0)\n'
                'in\n    #"Prep Kept"')
        for override_key in ("m_query_override", "m_query_overrides"):
            with self.subTest(override_key=override_key):
                datasource = self._transformed_datasource()
                datasource[override_key] = (
                    {self.table: prep} if override_key == "m_query_overrides" else prep
                )
                with mock.patch.object(tmdl_generator, "_build_table",
                                       wraps=tmdl_generator._build_table) as builder:
                    table = self._model([datasource])["model"]["tables"][0]
                self.assertEqual(builder.call_args.kwargs["m_query_override"], prep)
                expression = table["partitions"][0]["source"]["expression"]
                self.assertIn('Source = error Error.Record("Synthetic.PrepFailure"', expression)
                self.assertIn('Table.SelectRows(Source, each [Amount] > 0)', expression)
                self.assertIn('Table.RenameColumns(#"Prep Kept", '
                              '{{"Code", "Display Code"}})', expression)
                self.assertIn('Table.AddColumn(#"Renamed Columns", "Double Amount", '
                              'each ([Amount] * 2), type number)', expression)
                self.assertNotIn("Renamed Hyper Columns", expression)
                self.assertNotIn("raw_code", expression)
                self.assertNotIn("Migration.HyperExportRequired", expression)
                self.assertNotIn("otherwise", expression)
                self.assertNotIn("#table", expression)
                self.assertEqual(validate_m_query(expression), [])
                content = self._content(table)
                self.assertEqual(migrate._stage_pending_hyper_partition(
                    content, self.data, [self.record],
                ), content)

    def test_each_wrong_or_missing_identity_component_leaves_original_error(self):
        content = self._content()
        for key in ("workbook", "archive_member", "schema", "table"):
            for missing in (False, True):
                with self.subTest(key=key, missing=missing):
                    identity = dict(self.identity)
                    if missing:
                        del identity[key]
                    else:
                        identity[key] += "-different"
                    record = dict(self.record, identity=identity)
                    self.assertEqual(
                        migrate._stage_pending_hyper_partition(
                            content, self.data, [record]
                        ), content,
                    )
                    self.assertNotIn("staged_files", record)

    def test_absent_duplicate_or_extra_key_identity_exports_remain_errors(self):
        content = self._content()
        for records in (
            [], [dict(self.record), dict(self.record)],
            [dict(self.record, identity=dict(self.identity, extra="not-exact-four"))],
            [dict(self.record, identity=None)],
        ):
            with self.subTest(records=records):
                self.assertEqual(
                    migrate._stage_pending_hyper_partition(content, self.data, records),
                    content,
                )
                self.assertTrue(all("staged_files" not in record for record in records))

    def test_ordered_case_sensitive_header_mismatch_never_borrows_another_csv(self):
        content = self._content()
        _write(os.path.join(self.data, "matching.csv"), "id,Amount\n1,999\n")
        for header in ("Amount,id", "ID,Amount", "id,amount", "id,Amount,extra"):
            with self.subTest(header=header):
                _write(os.path.join(self.data, self.filename), header + "\n1,2\n")
                self.assertEqual(
                    migrate._stage_pending_hyper_partition(
                        content, self.data, [self.record]
                    ), content,
                )
                self.assertNotIn("staged_files", self.record)

    def test_error_table_must_match_declaration_and_marker_must_be_unique(self):
        content = self._content()
        for invalid in (
            content.replace('Table = "O\'Brien réalisé"', 'Table = "Foreign"'),
            content.replace("table 'O''Brien réalisé'", "table Foreign"),
            content + '\n// Migration.HyperExportRequired\n',
            content.replace('Identity = "', 'Identity = "not-json'),
        ):
            with self.subTest(content=invalid):
                self.assertEqual(
                    migrate._stage_pending_hyper_partition(
                        invalid, self.data, [self.record]
                    ), invalid,
                )

    def test_renamed_final_semantic_table_roundtrips_through_authoritative_manifest(self):
        from powerbi_import.tmdl_generator import _write_table_tmdl
        from powerbi_import.m_validator import validate_m_query
        second_identity = dict(self.identity, table="Physical second")
        sources = [
            self._datasource(),
            self._datasource("source-b", second_identity, [
                {"name": "id", "datatype": "integer"},
                {"name": "Other", "datatype": "real"},
            ]),
        ]
        model = self._model(sources)
        final_name = model["_table_rename_map"][("source-b", self.table)]
        self.assertNotEqual(final_name, self.table)
        table = next(t for t in model["model"]["tables"] if t["name"] == final_name)
        self.assertIn(f'Table = "{final_name}"',
                      table["partitions"][0]["source"]["expression"])
        project = _project(self.root, {})
        data = os.path.join(project, "Data")
        filename = "hyper_" + "b" * 64 + ".csv"
        _write(os.path.join(data, filename), "id,Other\n1,3.5\n")
        record = {"identity": second_identity, "csv_filename": filename,
                  "columns": ["id", "Other"]}
        _write(os.path.join(data, "hyper_exports.json"),
               json.dumps({"version": 1, "exports": [record]}))
        directory = os.path.join(project, f"{NAME}.SemanticModel", "definition", "tables")
        os.makedirs(directory, exist_ok=True)
        _write_table_tmdl(directory, table)
        original_path = os.path.join(directory, os.listdir(directory)[0])
        # Binding must use the declaration, not this unrelated artifact basename.
        path = os.path.join(directory, "unrelated-artifact.tmdl")
        os.replace(original_path, path)
        extract = os.path.join(self.root, "extract")
        _write(os.path.join(extract, "datasources.json"), json.dumps(sources))
        with mock.patch.object(migrate, "_get_extract_dir", return_value=extract), \
                mock.patch.object(migrate, "_hyper_to_csv_files") as reader:
            migrate._convert_hyper_to_csv_in_data(data, NAME, project)
            with open(path, encoding="utf-8") as stream:
                bound = stream.read()
            migrate._convert_hyper_to_csv_in_data(data, NAME, project)
        reader.assert_not_called()
        self.assertNotIn("Migration.HyperExportRequired", bound)
        self.assertIn(filename, bound)
        self.assertEqual(validate_m_query(self._expression(bound)), [])
        with open(path, encoding="utf-8") as stream:
            self.assertEqual(stream.read(), bound)
        with open(os.path.join(data, "hyper_exports.json"), encoding="utf-8") as stream:
            self.assertEqual(json.load(stream)["exports"][0]["staged_files"], [{
                "table": final_name, "csv_filename": filename,
            }])

    def test_authoritative_conversion_does_not_fallback_for_unmatched_placeholder(self):
        content = self._content()
        project = _project(self.root, {})
        data = os.path.join(project, "Data")
        path = os.path.join(project, f"{NAME}.SemanticModel", "definition",
                            "tables", "legacy.tmdl")
        _write(path, content)
        _write(os.path.join(data, self.filename), "id,Amount\n1,2.5\n")
        _write(os.path.join(data, "legacy.csv"), "id,Amount\n1,999\n")
        record = dict(self.record, identity=dict(self.identity, schema="foreign"))
        _write(os.path.join(data, "hyper_exports.json"),
               json.dumps({"version": 1, "exports": [record]}))
        extract = os.path.join(self.root, "extract")
        _write(os.path.join(extract, "datasources.json"), json.dumps([{
            "tables": [{"name": "legacy", "source_table": "legacy",
                        "hyper_identity": self.identity}],
        }]))
        with mock.patch.object(migrate, "_get_extract_dir", return_value=extract):
            migrate._convert_hyper_to_csv_in_data(data, NAME, project)
        with open(path, encoding="utf-8") as stream:
            self.assertEqual(stream.read(), content)


class TestHyperCsvTypeChanges(unittest.TestCase):
    """Only the original inline Source schema may be typed before M calculations."""

    def test_extraction_types_preserve_numeric_and_temporal_distinctions(self):
        types = {
            "ID": "integer", "Amount": "REAL", "Order Date": "date",
            "Updated": "datetime", "At": "time", "Active": "boolean",
            "Label": "string", "Zoned": "datetimezone", "Elapsed": "duration",
        }
        self.assertEqual(migrate._hyper_csv_type_changes("", list(types), types), [
            '{"ID", Int64.Type}', '{"Amount", type number}',
            '{"Order Date", type date}', '{"Updated", type datetime}',
            '{"At", type time}', '{"Active", type logical}',
            '{"Label", type text}', '{"Zoned", type datetimezone}',
            '{"Elapsed", type duration}',
        ])

    def test_metadata_wins_over_model_but_missing_metadata_uses_source_column(self):
        content = (
            "\tcolumn 'Displayed Date'\n\t\tdataType: dateTime\n"
            "\t\tsourceColumn: Order Date\n"
            "\tcolumn 'Displayed Amount'\n\t\tdataType: double\n"
            "\t\tsourceColumn: Amount\n"
        )
        self.assertEqual(migrate._hyper_csv_type_changes(
            content, ["Order Date", "Amount"], {"Order Date": "date"}), [
                '{"Order Date", type date}', '{"Amount", type number}',
            ])

    def test_model_scalar_types_use_source_column_not_display_name(self):
        expected_types = {
            "string": "type text", "int64": "Int64.Type",
            "double": "type number", "decimal": "Currency.Type",
            "boolean": "type logical", "dateTime": "type datetime",
            "date": "type date", "time": "type time", "binary": "type binary",
            "datetimezone": "type datetimezone", "duration": "type duration",
        }
        for datatype, expected in expected_types.items():
            with self.subTest(datatype=datatype):
                content = (
                    "\tcolumn 'Model Caption'\n"
                    f"\t\tdataType: {datatype}\n"
                    "\t\tsourceColumn: Physical Name\n"
                )
                self.assertEqual(migrate._hyper_csv_type_changes(
                    content, ["Physical Name"], {}), [
                        '{"Physical Name", ' + expected + '}',
                    ])
                self.assertEqual(migrate._hyper_csv_type_changes(
                    content, ["Model Caption"], {}), [])

    def test_quoted_source_names_are_unescaped_then_escaped_as_m_literals(self):
        content = (
            "\tcolumn 'Customer''s Caption'\n\t\tdataType: double\n"
            "\t\tsourceColumn: 'Customer''s \"Amount\"'\n"
        )
        expected = ['{"Customer\'s ""Amount""", type number}']
        self.assertEqual(migrate._hyper_csv_type_changes(
            content, ['Customer\'s "Amount"'], {}), expected)
        self.assertEqual(migrate._hyper_csv_type_changes(
            "", ['Customer\'s "Amount"'],
            {'Customer\'s "Amount"': "real"}), expected)

    def test_derived_columns_and_measures_are_not_typed_at_source(self):
        content = (
            "\tcolumn Amount\n\t\tdataType: double\n\t\tsourceColumn: Amount\n"
            "\tcolumn Margin\n\t\tdataType: double\n\t\tsourceColumn: Margin\n"
            "\tcolumn 'DAX Only' = [Amount] * 2\n\t\tdataType: double\n"
            "\tmeasure Total = SUM([Amount])\n\t\tdataType: double\n"
            "\t\tsourceColumn: MeasureOnly\n"
        )
        self.assertEqual(migrate._hyper_csv_type_changes(
            content, ["Amount"], {"Amount": "real", "Margin": "real"}), [
                '{"Amount", type number}',
            ])
        self.assertEqual(migrate._hyper_csv_type_changes(
            content, ["DAX Only", "MeasureOnly", "Unknown"], {}), [])

    def test_unknown_model_type_or_missing_declaration_is_not_guessed(self):
        content = (
            "\tcolumn Unknown\n\t\tdataType: unsupported\n\t\tsourceColumn: Unknown\n"
            "\tcolumn Untyped\n\t\tsourceColumn: Untyped\n"
        )
        self.assertEqual(migrate._hyper_csv_type_changes(
            content, ["Unknown", "Untyped"], {}), [])


class TestHyperCsvSourceTyping(unittest.TestCase):
    """Regression: Csv.Document must not expose text to numeric/date calculations."""

    def _make_project(self, root):
        source = (
            '\t\t\t\tlet\n'
            '\t\t\t\t\tSource = try #table({"Qty", "Amount", "Order Date", "Updated"}, '
            '{{2, 1234.5, #date(2026, 10, 15), #datetime(2026, 10, 15, 9, 30, 0)}}) '
            'otherwise #table({"Qty", "Amount", "Order Date", "Updated"}, {}),\n'
        )
        downstream = (
            '\t\t\t\t\t#"Added Margin" = Table.AddColumn(Source, "Margin", '
            'each [Amount] / [Qty], type number),\n'
            '\t\t\t\t\t#"Added Year" = Table.AddColumn(#"Added Margin", "Year", '
            'each Date.Year([Order Date]), Int64.Type),\n'
            '\t\t\t\t\t#"Changed Types" = Table.TransformColumnTypes(#"Added Year", '
            '{{"Margin", type number}, {"Year", Int64.Type}})\n'
            '\t\t\t\tin\n\t\t\t\t\t#"Changed Types"'
        )
        project = _project(root, {"Orders": source + downstream})
        path = os.path.join(project, f"{NAME}.SemanticModel", "definition",
                            "tables", "Orders.tmdl")
        with open(path, "a", encoding="utf-8") as stream:
            for display, physical, dtype in (
                ("Quantity Caption", "Qty", "int64"),
                ("Amount Caption", "Amount", "double"),
                ("Date Caption", "Order Date", "dateTime"),
                ("Updated Caption", "Updated", "dateTime"),
                ("Margin", "Margin", "double"),
                ("Year", "Year", "int64"),
            ):
                stream.write(f"\n\tcolumn '{display}'\n\t\tdataType: {dtype}\n"
                             f"\t\tsourceColumn: {physical}\n")
        data = os.path.join(project, "Data")
        _write(os.path.join(data, "orders_extract.hyper"), b"unused: CSV already exists")
        _write(os.path.join(data, "orders_extract.csv"),
               "qty_raw,amount_raw,date_raw,updated_raw\n"
               "2,1234.5,2026-10-15,2026-10-15 09:30:00\n")
        extract = os.path.join(root, "extract")
        metadata = [{
            "tables": [{
                "name": "Orders", "source_table": "orders_extract",
                "cols_physical_map": {
                    "qty_raw": "Qty", "amount_raw": "Amount",
                    "date_raw": "Order Date", "updated_raw": "Updated",
                },
                "columns": [
                    {"name": "qty_raw", "datatype": "integer"},
                    {"name": "amount_raw", "datatype": "real"},
                    {"name": "date_raw", "datatype": "date"},
                    {"name": "updated_raw", "datatype": "datetime"},
                    {"name": "Margin", "datatype": "real"},
                    {"name": "Year", "datatype": "integer"},
                ],
            }],
        }]
        _write(os.path.join(extract, "datasources.json"), json.dumps(metadata))
        return project, data, extract, downstream, metadata

    def _assert_source_types(self, text, entries, downstream, table="Orders"):
        expected_source = (
            'Source = Table.TransformColumnTypes(Table.PromoteHeaders('
            'Csv.Document(File.Contents(DataFolder & "\\\\' + table + '.csv"), '
            '[Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]), '
            '[PromoteAllScalars=true]), {' + ", ".join(entries) + '}, "en-US"),\n'
        )
        self.assertIn(expected_source + downstream, text)
        source = text.split('Source = ', 1)[1].split('\n', 1)[0]
        self.assertNotIn('"Margin"', source)
        self.assertNotIn('"Year"', source)
        self.assertNotIn("#table(", text)

    def test_restores_metadata_types_immediately_after_promotion_before_calculations(self):
        with tempfile.TemporaryDirectory() as root:
            project, data, extract, downstream, _ = self._make_project(root)
            with mock.patch.object(migrate._extract_dir_local, "path", extract, create=True):
                migrate._convert_hyper_to_csv_in_data(data, NAME, project)
            self._assert_source_types(_read(project, "Orders"), [
                '{"Qty", Int64.Type}', '{"Amount", type number}',
                '{"Order Date", type date}', '{"Updated", type datetime}',
            ], downstream)
            with open(os.path.join(data, "Orders.csv"), encoding="utf-8", newline="") as stream:
                self.assertEqual(list(csv.reader(stream)), [
                    ["Qty", "Amount", "Order Date", "Updated"],
                    ["2", "1234.5", "2026-10-15", "2026-10-15 09:30:00"],
                ])

    def test_partial_metadata_uses_model_types_for_remaining_physical_columns(self):
        with tempfile.TemporaryDirectory() as root:
            project, data, extract, downstream, metadata = self._make_project(root)
            metadata[0]["tables"][0]["columns"] = [{"name": "date_raw", "datatype": "date"}]
            _write(os.path.join(extract, "datasources.json"), json.dumps(metadata))
            with mock.patch.object(migrate._extract_dir_local, "path", extract, create=True):
                migrate._convert_hyper_to_csv_in_data(data, NAME, project)
            self._assert_source_types(_read(project, "Orders"), [
                '{"Qty", Int64.Type}', '{"Amount", type number}',
                '{"Order Date", type date}', '{"Updated", type datetime}',
            ], downstream)

    def test_unavailable_metadata_falls_back_to_model_source_columns(self):
        for snapshot in (None, "{invalid json", "[]"):
            with self.subTest(snapshot=snapshot), tempfile.TemporaryDirectory() as root:
                project, data, extract, downstream, _ = self._make_project(root)
                path = os.path.join(extract, "datasources.json")
                if snapshot is None:
                    os.unlink(path)
                else:
                    _write(path, snapshot)
                # Without metadata routing, matching is based on actual caption headers.
                _write(os.path.join(data, "orders_extract.csv"),
                       "Qty,Amount,Order Date,Updated\n"
                       "2,1234.5,2026-10-15,2026-10-15 09:30:00\n")
                with mock.patch.object(migrate._extract_dir_local, "path", extract, create=True):
                    migrate._convert_hyper_to_csv_in_data(data, NAME, project)
                self._assert_source_types(_read(project, "Orders"), [
                    '{"Qty", Int64.Type}', '{"Amount", type number}',
                    '{"Order Date", type datetime}', '{"Updated", type datetime}',
                ], downstream)

    def test_quoted_headers_survive_csv_routing_and_source_type_literals(self):
        with tempfile.TemporaryDirectory() as root:
            body = (
                'Source = try #table({"Customer\'s ""Amount"""}, {}) '
                'otherwise #table({"Customer\'s ""Amount"""}, {})'
            )
            project = _project(root, {"Orders": body})
            path = os.path.join(project, f"{NAME}.SemanticModel", "definition",
                                "tables", "Orders.tmdl")
            with open(path, "a", encoding="utf-8") as stream:
                stream.write("\n\tcolumn 'Display Caption'\n\t\tdataType: double\n"
                             "\t\tsourceColumn: 'Customer''s \"Amount\"'\n")
            data = os.path.join(project, "Data")
            _write(os.path.join(data, "orders_extract.hyper"), b"x")
            _write(os.path.join(data, "orders_extract.csv"),
                   '"Customer\'s ""Amount"""\n1234.5\n')
            with mock.patch.object(migrate._extract_dir_local, "path", root, create=True):
                migrate._convert_hyper_to_csv_in_data(data, NAME, project)
            text = _read(project, "Orders")
            self.assertIn('[PromoteAllScalars=true]), '
                          '{{"Customer\'s ""Amount""", type number}}, "en-US")', text)
            self.assertNotIn("#table(", text)
            with open(os.path.join(data, "Orders.csv"), encoding="utf-8", newline="") as stream:
                self.assertEqual(list(csv.reader(stream)), [['Customer\'s "Amount"'], ["1234.5"]])

    def test_untyped_schema_still_promotes_headers_without_guessing_types(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {
                "Orders": 'Source = try #table({"Unknown"}, {}) '
                          'otherwise #table({"Unknown"}, {})',
            })
            data = os.path.join(project, "Data")
            _write(os.path.join(data, "orders_extract.hyper"), b"x")
            _write(os.path.join(data, "orders_extract.csv"), "Unknown\n1234.5\n")
            with mock.patch.object(migrate._extract_dir_local, "path", root, create=True):
                migrate._convert_hyper_to_csv_in_data(data, NAME, project)
            text = _read(project, "Orders")
            self.assertIn("Source = Table.PromoteHeaders(Csv.Document(", text)
            self.assertNotIn("Table.TransformColumnTypes", text)
            self.assertNotIn("#table(", text)


def _geo_partition(filename, columns):
    from tableau_export.m_query_builder import _gen_m_geojson, wrap_source_with_try_otherwise
    cols = [{"name": c, "datatype": "string"} for c in columns]
    m = _gen_m_geojson({"filename": filename}, "T", cols)
    m = wrap_source_with_try_otherwise(m, columns)
    return "\n".join("\t\t\t\t" + line for line in m.splitlines())


def _dbf(fields, rows):
    """Minimal dBase III file: fields [(name, length)], rows of strings."""
    import struct
    record_len = 1 + sum(length for _n, length in fields)
    header_len = 32 + 32 * len(fields) + 1
    out = bytearray(struct.pack("<BBBBIHH20x", 3, 124, 1, 1, len(rows), header_len, record_len))
    for name, length in fields:
        out += name.encode("ascii").ljust(11, b"\x00") + b"C" + b"\x00" * 4
        out += bytes([length, 0]) + b"\x00" * 14
    out += b"\x0d"
    for row in rows:
        out += b" " + b"".join(v.encode("utf-8").ljust(length)[:length]
                              for v, (_n, length) in zip(row, fields))
    return bytes(out + b"\x1a")


class TestReconcileHeaderVariants(unittest.TestCase):
    def test_case_only_difference_is_renamed(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"T": _csv_partition("a.csv", ["INSEE"])})
            _write(os.path.join(project, "Data", "a.csv"), "insee\n1\n")
            migrate._reconcile_csv_partitions(project, NAME)
            self.assertIn('Table.RenameColumns(#"Promoted Headers", {{"insee", "INSEE"}})',
                          _read(project, "T"))

    def test_one_file_column_feeding_two_captions_is_copied(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"T": _csv_partition(
                "a.csv", ["geometry", "geometry (a.geojson)", "geometry (b.geojson)"])})
            _write(os.path.join(project, "Data", "a.csv"), "geometry\nx\n")
            migrate._reconcile_csv_partitions(project, NAME)
            text = _read(project, "T")
            self.assertIn('"geometry", "geometry (a.geojson)")', text)
            self.assertIn('"geometry", "geometry (b.geojson)")', text)
            self.assertNotIn("RenameColumns", text)


class TestRestrictToModelColumns(unittest.TestCase):
    def test_partition_ends_on_declared_source_columns_only(self):
        with tempfile.TemporaryDirectory() as root:
            body = _csv_partition("a.csv", ["x", "y"])
            project = _project(root, {"T": body})
            path = os.path.join(project, f"{NAME}.SemanticModel", "definition", "tables", "T.tmdl")
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n\tcolumn x\n\t\tsourceColumn: x\n\n\tcolumn 'a b'\n\t\tsourceColumn: a b\n")
            self.assertEqual(migrate._restrict_file_partitions_to_model(project, NAME), 1)
            text = _read(project, "T")
            self.assertIn('#"Model Columns" = Table.SelectColumns(Result, {"x", "a b"}, '
                          'MissingField.UseNull)', text)
            self.assertIn('in\n\t\t\t\t\t#"Model Columns"', text)
            self.assertEqual(migrate._restrict_file_partitions_to_model(project, NAME), 0)


class TestSpatialRouting(unittest.TestCase):
    def test_single_workbook_runs_coordinate_wiring_after_csv_routing(self):
        order = []
        names = ('_fix_twb_data_folder', '_collect_local_data_files',
                 '_convert_hyper_to_csv_in_data', '_route_spatial_partitions',
                 '_reconcile_csv_partitions', '_wire_shapefile_map_coordinates',
                 '_restrict_file_partitions_to_model', '_shorten_report_artifact_path')
        with mock.patch.multiple(migrate, **{
            name: mock.Mock(side_effect=lambda *args, step=name: order.append(step))
            for name in names
        }):
            migrate._extract_twbx_data_files(
                SimpleNamespace(tableau_file='sample.twb', output_dir='out',
                                output_format='pbip'), NAME)
        self.assertLess(order.index('_route_spatial_partitions'),
                        order.index('_wire_shapefile_map_coordinates'))
        self.assertLess(order.index('_reconcile_csv_partitions'),
                        order.index('_wire_shapefile_map_coordinates'))
        self.assertLess(order.index('_wire_shapefile_map_coordinates'),
                        order.index('_restrict_file_partitions_to_model'))

    def test_wgs84_polygon_points_join_to_map_fact_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {
                'Facts': _csv_partition('facts.csv', ['code_insee', 'Amount']),
                'Places': _csv_partition('zones.shp.csv', ['insee']),
            })
            definition = os.path.join(project, f'{NAME}.SemanticModel', 'definition')
            shp = os.path.join(project, 'Data', 'zones.shp')
            ring = [(-1., -1.), (1., -1.), (1., 1.), (-1., 1.), (-1., -1.)]
            polygon = (struct.pack('<I4dII', 5, -1., -1., 1., 1., 1, len(ring))
                       + struct.pack('<I', 0)
                       + b''.join(struct.pack('<2d', *point) for point in ring))
            header = bytearray(100)
            struct.pack_into('>I', header, 0, 9994)
            struct.pack_into('<I', header, 32, 5)
            _write(shp, bytes(header) + struct.pack('>2I', 1, len(polygon) // 2) + polygon)
            _write(os.path.join(project, 'Data', 'zones.prj'), 'GEOGCS["GCS_WGS_1984"]')
            _write(os.path.join(project, 'Data', 'zones.shp.csv'), 'insee\n001\n')
            _write(os.path.join(project, 'Data', 'facts.csv'),
                   'code_insee,Amount\n001,12\n002,13\n')
            fact_table_path = os.path.join(definition, 'tables', 'Facts.tmdl')
            with open(fact_table_path, 'a', encoding='utf-8') as stream:
                stream.write('\n\tcolumn code_insee\n\t\tsourceColumn: code_insee\n'
                             '\n\tcolumn Amount\n\t\tsourceColumn: Amount\n')
            self.assertEqual(migrate._restrict_file_partitions_to_model(project, NAME), 1)
            _write(os.path.join(definition, 'relationships.tmdl'),
                   'relationship link\n\tfromColumn: Facts.code_insee\n'
                   '\ttoColumn: Places.insee\n')
            visual_path = os.path.join(project, f'{NAME}.Report', 'definition',
                                       'pages', 'page', 'visuals', 'visual', 'visual.json')
            visual = {
                'visual': {'visualType': 'map', 'query': {'queryState': {
                    'Size': {'projections': [{
                        'field': {'Aggregation': {'Expression': {'Column': {
                            'Expression': {'SourceRef': {'Entity': 'Facts'}},
                            'Property': 'Amount'}}}},
                        'queryRef': 'Facts.Amount'}]}}}},
                'annotations': [{'name': 'MigrationNote',
                                 'value': 'Tableau-generated map coordinates require spatial geometry; no geocodable Location is bound.'}],
            }
            _write(visual_path, json.dumps(visual))
            first = migrate._wire_shapefile_map_coordinates(project, NAME)
            self.assertEqual(first, {'mapped': 1, 'unmatched': 1})
            with open(os.path.join(project, 'Data', 'facts.csv'), newline='', encoding='utf-8') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['MigrationLatitude'], '0.0000000000')
            self.assertEqual(rows[1]['MigrationLatitude'], '')
            self.assertIn('dataCategory: Latitude', _read(project, 'Facts'))
            self.assertIn('"Amount", "MigrationLatitude", "MigrationLongitude"',
                          _read(project, 'Facts'))
            with open(visual_path, encoding='utf-8') as stream:
                bound = json.load(stream)['visual']
            self.assertEqual(bound['visualType'], 'azureMap')
            roles = bound['query']['queryState']
            self.assertEqual(set(roles), {'Category', 'Y', 'X', 'Size'})
            for role, column in (('Y', 'MigrationLatitude'),
                                 ('X', 'MigrationLongitude')):
                self.assertEqual(len(roles[role]['projections']), 1)
                self.assertEqual(roles[role]['projections'][0]['field'], {
                    'Column': {'Expression': {'SourceRef': {'Entity': 'Facts'}},
                               'Property': column},
                })
            self.assertEqual(roles['Category']['projections'], roles['Y']['projections'])
            self.assertEqual(roles['Size'],
                             visual['visual']['query']['queryState']['Size'])
            self.assertEqual(migrate._wire_shapefile_map_coordinates(project, NAME),
                             {'mapped': 0, 'unmatched': 0})

    def test_catalog_shapefile_map_promotes_label_and_retains_extra_tooltip(self):
        with tempfile.TemporaryDirectory() as root:
            columns = ['RecordKey', 'Amount', 'Record Label', 'Record Note']
            project = _project(root, {
                'Facts': _csv_partition('facts.csv', columns),
                'Places': _csv_partition('zones.shp.csv', ['GeoKey']),
            })
            definition = os.path.join(project, f'{NAME}.SemanticModel', 'definition')
            shp_path = _write(os.path.join(project, 'Data', 'zones.shp'), b'synthetic')
            fact_rows = [[f'{index:03d}', str(index + 1),
                          f'Record {index}', f'Note {index}'] for index in range(22)]
            coordinates = [(10.0 + index / 100, 20.0 + index / 100)
                           for index in range(len(fact_rows))]
            for filename, headers, records in (
                    ('facts.csv', columns, fact_rows),
                    ('zones.shp.csv', ['GeoKey'], [[row[0]] for row in fact_rows])):
                with open(os.path.join(project, 'Data', filename), 'w',
                          encoding='utf-8', newline='') as stream:
                    writer = csv.writer(stream)
                    writer.writerow(headers)
                    writer.writerows(records)
            fact_table_path = os.path.join(definition, 'tables', 'Facts.tmdl')
            with open(fact_table_path, 'a', encoding='utf-8') as stream:
                for column in columns:
                    stream.write(f"\n\tcolumn '{column}'\n\t\tsourceColumn: {column}\n")
            _write(os.path.join(definition, 'relationships.tmdl'),
                   'relationship link\n\tfromColumn: Facts.RecordKey\n'
                   '\ttoColumn: Places.GeoKey\n')
            label = {
                'field': {'Column': {'Expression': {'SourceRef': {'Entity': 'Facts'}},
                                     'Property': 'Record Label'}},
                'queryRef': 'Facts.Record Label', 'active': False,
            }
            note = {
                'field': {'Column': {'Expression': {'SourceRef': {'Entity': 'Facts'}},
                                     'Property': 'Record Note'}},
                'queryRef': 'Facts.Record Note', 'active': True,
            }
            size = {'projections': [{
                'field': {'Aggregation': {
                    'Expression': {'Column': {
                        'Expression': {'SourceRef': {'Entity': 'Facts'}},
                        'Property': 'Amount',
                    }},
                    'Function': 0,
                }},
                'queryRef': 'Facts.Amount', 'active': True,
            }]}
            query_state = {'Size': size, 'Tooltips': {'projections': [label, note, label]}}
            for obsolete_role in ('Location', 'Latitude', 'Longitude', 'Color'):
                query_state[obsolete_role] = {'projections': [label]}
            visual_path = os.path.join(project, f'{NAME}.Report', 'definition',
                                       'pages', 'page', 'visuals', 'visual', 'visual.json')
            _write(visual_path, json.dumps({
                'visual': {'visualType': 'map', 'query': {'queryState': query_state}},
                'annotations': [{'name': 'MigrationNote',
                                 'value': 'Tableau-generated map coordinates require spatial geometry.'}],
            }))
            with mock.patch.object(migrate, '_shp_polygon_centers',
                                   return_value=coordinates) as centers:
                self.assertEqual(migrate._wire_shapefile_map_coordinates(project, NAME),
                                 {'mapped': len(fact_rows), 'unmatched': 0})
                centers.assert_called_once_with(shp_path)
            with open(visual_path, encoding='utf-8') as stream:
                bound = json.load(stream)['visual']
            self.assertEqual(bound['visualType'], 'azureMap')
            roles = bound['query']['queryState']
            self.assertEqual(set(roles), {'Category', 'Y', 'X', 'Size', 'Tooltips'})
            self.assertEqual(roles['Category']['projections'], [{**label, 'active': True}])
            self.assertEqual(roles['Tooltips']['projections'], [note])
            self.assertEqual(roles['Size'], size)
            for role, column in (('Y', 'MigrationLatitude'),
                                 ('X', 'MigrationLongitude')):
                self.assertEqual(roles[role]['projections'], [{
                    'field': {'Column': {'Expression': {'SourceRef': {'Entity': 'Facts'}},
                                         'Property': column}},
                    'queryRef': f'Facts.{column}', 'nativeQueryRef': column,
                    'active': True,
                }])
            with open(os.path.join(project, 'Data', 'facts.csv'),
                      encoding='utf-8', newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), len(fact_rows))
            for row, original, (latitude, longitude) in zip(rows, fact_rows, coordinates):
                self.assertEqual([row[column] for column in columns], original)
                self.assertEqual(row['MigrationLatitude'], f'{latitude:.10f}')
                self.assertEqual(row['MigrationLongitude'], f'{longitude:.10f}')

    def test_polygon_reader_returns_inside_point_and_refuses_other_crs(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "a.shp")
            ring = [(-1., -1.), (1., -1.), (1., 1.), (-1., 1.), (-1., -1.)]
            polygon = (struct.pack('<I4dII', 5, -1., -1., 1., 1., 1, len(ring))
                       + struct.pack('<I', 0)
                       + b''.join(struct.pack('<2d', *point) for point in ring))
            header = bytearray(100)
            struct.pack_into('>I', header, 0, 9994)
            struct.pack_into('<I', header, 32, 5)
            _write(path, bytes(header) + struct.pack('>2I', 1, len(polygon) // 2) + polygon)
            _write(os.path.join(root, 'a.prj'), 'GEOGCS["GCS_WGS_1984"]')
            self.assertEqual(list(migrate._shp_polygon_centers(path)), [(0., 0.)])
            _write(os.path.join(root, 'a.prj'), 'PROJCS["Lambert_93"]')
            with self.assertRaisesRegex(ValueError, 'WGS84'):
                list(migrate._shp_polygon_centers(path))

    def test_generated_geojson_requires_a_features_property(self):
        self.assertIn("features = Source[features],",
                      _geo_partition("a.geojson", ["Name"]))
        self.assertNotIn("otherwise {}", _geo_partition("a.geojson", ["Name"]))

    def test_missing_geojson_is_served_by_its_extract_csv(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"T": _geo_partition("zone.geojson", ["Name", "Geometry"])})
            data = os.path.join(project, "Data")
            _write(os.path.join(data, "federated_x.csv"), "Name,Geometry\nA,POINT\n")
            _write(os.path.join(data, "federated_y.csv"), "Other\n1\n")
            stats = migrate._route_spatial_partitions(project, NAME)
            text = _read(project, "T")
            self.assertEqual(stats["extract_csv"], 1)
            self.assertIn('Csv.Document(File.Contents(DataFolder & "\\federated_x.csv")', text)
            self.assertNotIn("Json.Document", text)
            self.assertIn('{"Geometry", type text}', text)

    def test_shapefile_attributes_are_read_from_dbf(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"T": _geo_partition("c.shp", ["nom", "geometry"])})
            data = os.path.join(project, "Data")
            _write(os.path.join(data, "c.shp"), b"\x00\x00\x27\x0a")
            _write(os.path.join(data, "c.dbf"), _dbf([("nom", 10)], [["Paris"], ["Lyon"]]))
            stats = migrate._route_spatial_partitions(project, NAME)
            migrate._reconcile_csv_partitions(project, NAME)
            text = _read(project, "T")
            with open(os.path.join(data, "c.shp.csv"), encoding="utf-8") as f:
                self.assertEqual(f.read().split(), ["nom", "Paris", "Lyon"])
            self.assertEqual(stats["shapefile_csv"], 1)
            self.assertIn('c.shp.csv"), [Delimiter=","', text)
            self.assertNotIn("Json.Document", text)
            self.assertIn('"geometry", each null', text)

    def test_joined_suffix_columns_match_extract_on_base_name(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"T": _geo_partition(
                "a.geojson", ["site", "site (b.geojson)"])})
            _write(os.path.join(project, "Data", "federated_x.csv"), "site\n1\n")
            migrate._route_spatial_partitions(project, NAME)
            migrate._reconcile_csv_partitions(project, NAME)
            text = _read(project, "T")
            self.assertIn("federated_x.csv", text)
            self.assertIn('"site", "site (b.geojson)")', text)

    def test_dbf_is_fetched_for_a_shapefile_already_in_data(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"T": _geo_partition("c.shp", ["nom"])})
            _write(os.path.join(project, "Data", "c.shp"), b"shp")
            src = os.path.join(root, "src")
            _write(os.path.join(src, "c.shp"), b"shp")
            _write(os.path.join(src, "c.dbf"), _dbf([("nom", 5)], [["A"]]))
            workbook = _write(os.path.join(src, "w.twb"), "<workbook/>")
            migrate._collect_local_data_files(workbook, project, NAME)
            self.assertTrue(os.path.isfile(os.path.join(project, "Data", "c.dbf")))

    def test_present_geojson_file_is_left_alone(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"T": _geo_partition("a.geojson", ["Name"])})
            _write(os.path.join(project, "Data", "a.geojson"), '{"features": []}')
            _write(os.path.join(project, "Data", "e.csv"), "Name\nA\n")
            migrate._route_spatial_partitions(project, NAME)
            self.assertIn("Json.Document", _read(project, "T"))


if __name__ == "__main__":
    unittest.main()
