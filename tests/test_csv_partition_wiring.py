"""CSV partitions are wired to the right file and match its real header."""

import base64
import csv
import hashlib
import io
import json
import ntpath
import os
import re
import sqlite3
import struct
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

import migrate
from powerbi_import.errors import GenerationError
from powerbi_import.tmdl_writers import _serialize_source_column
from tests.hyper_test_helpers import StreamingHyperApi
from tests.native_import_test_helpers import forbid_native_payload_io

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
        result = migrate._stage_declared_local_sources(
            source, project, metadata, copy_files=True,
        )
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


def _project(root, tables, *, identities=None, source_columns=None):
    """tables: {table_name: partition M body}. Returns the project dir."""
    project = os.path.join(root, NAME)
    definition = os.path.join(project, f"{NAME}.SemanticModel", "definition")
    data = os.path.join(project, "Data").replace("\\", "\\\\")
    _write(os.path.join(definition, "expressions.tmdl"),
           f'expression DataFolder = "{data}" meta [IsParameterQuery=true]\n')
    for table, body in tables.items():
        identity = (identities or {}).get(table)
        annotation = ""
        if identity is not None:
            annotation = (
                "\n\tannotation Migration_HyperIdentity = "
                + json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
                + "\n"
            )
        declarations = ""
        for column, datatype in (source_columns or {}).get(table, {}).items():
            quoted = "'" + column.replace("'", "''") + "'"
            declarations += (
                f"\tcolumn {quoted}\n\t\tdataType: {datatype}\n"
                f"\t\tsourceColumn: {quoted}\n\n"
            )
        mode = "\t\tmode: import\n" if identity is not None else ""
        _write(os.path.join(definition, "tables", f"{table}.tmdl"),
               f"table {table}\n{declarations}\tpartition {table} = m\n"
               f"{mode}\t\tsource =\n{body}\n{annotation}")
    return project


def _write_sqlite_hyper(path, columns, rows, table="Physical"):
    """A genuine, fully countable SQLite-compatible extract, not a sample."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)

    def quote(name):
        return '"' + name.replace('"', '""') + '"'

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE " + quote(table) + " ("
            + ", ".join(quote(column) + " TEXT" for column in columns) + ")"
        )
        connection.executemany(
            "INSERT INTO " + quote(table) + " VALUES ("
            + ", ".join("?" for unused in columns) + ")",
            rows,
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _sqlite_hyper_fixture(root, data, filename, columns, rows, table="Physical"):
    """Package only public synthetic data and retain its exact source identity."""
    path = _write_sqlite_hyper(os.path.join(data, filename), columns, rows, table)
    workbook = os.path.abspath(os.path.join(root, "synthetic.twbx"))
    member = "Data/" + filename
    new_archive = not os.path.exists(workbook)
    with zipfile.ZipFile(workbook, "w" if new_archive else "a") as archive:
        if new_archive:
            archive.writestr("synthetic.twb", "<workbook/>")
        archive.write(path, member)
    package = {"workbook": workbook, "archive_member": member}
    return dict(package, schema="main", table=table), {
        "path": path, "identity": package,
    }


def _full_export_record(path, identity, *, columns=None, row_count=None,
                        fmt="csv"):
    """Build fixture proof from finished bytes and measured CSV rows/header."""
    if fmt == "csv":
        with open(path, encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream)
            actual_columns = next(reader)
            actual_rows = 0
            for row in reader:
                if len(row) != len(actual_columns):
                    raise AssertionError("Synthetic full CSV has a malformed row")
                actual_rows += 1
        if columns is not None and list(columns) != actual_columns:
            raise AssertionError("Synthetic full CSV has the wrong header")
        if row_count is not None and row_count != actual_rows:
            raise AssertionError("Synthetic full CSV has the wrong row count")
        columns, row_count = actual_columns, actual_rows
    elif not columns or type(row_count) is not int or row_count < 0:
        raise AssertionError("Synthetic Parquet proof needs declared COPY metadata")
    digest = hashlib.sha256()
    size_bytes = 0
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size_bytes += len(chunk)
    record = {
        "identity": dict(identity), "format": fmt,
        "filename": os.path.basename(path), "columns": list(columns),
        "row_count": row_count, "total_rows": row_count,
        "max_rows": None, "sampled": False, "truncated": False,
        "full_extract_verified": True,
        "row_count_source": "csv_writer" if fmt == "csv" else "hyper_copy",
        "sha256": digest.hexdigest(), "size_bytes": size_bytes,
    }
    if fmt == "csv":
        record["csv_filename"] = record["filename"]
    return record


def _pending_hyper_error(table, identity):
    def m_string(value):
        return '"' + value.replace('"', '""') + '"'

    return (
        'error Error.Record("Migration.HyperExportRequired", '
        '"Packaged Hyper export has not been staged to CSV or Parquet", '
        "[Table = " + m_string(table) + ", Identity = "
        + m_string(json.dumps(identity, ensure_ascii=True, separators=(",", ":")))
        + "])"
    )


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


class TestPbipNativeDataPolicy(unittest.TestCase):
    """Supported PBIP routes keep native bindings, Hyper data, and report assets distinct."""

    @staticmethod
    def _native_sources(directory=None):
        tables, connections, expected, payloads = [], {}, {}, {}
        for name, connector, folder, filename, relation in (
            ("NativeWest", "CSV", "west", "facts.csv", "[facts#csv]"),
            ("NativeEast", "CSV", "east", "facts.csv", "[facts#csv]"),
            ("NativeBook", "Excel", "books", "facts.xlsx", "[Exact$]"),
            ("NativeJson", "JSON", "json", "facts.json", "facts.json"),
        ):
            source_directory = os.path.join(directory, folder) if directory else f"Native/{folder}"
            path = os.path.join(source_directory, filename)
            payloads[path] = ("synthetic native body " + name).encode("utf-8")
            expected[name] = path
            connection = {
                "type": connector,
                "details": {"directory": source_directory, "filename": filename},
            }
            connections[name] = connection
            tables.append({
                "name": name, "source_table": relation, "connection": name,
                "connection_details": connection,
                "columns": [{"name": "id", "datatype": "integer"},
                            {"name": "label", "datatype": "string"}],
            })
        datasource = {
            "name": "NativeFiles", "connection": connections["NativeWest"],
            "connection_map": connections, "tables": tables,
            "calculations": [], "relationships": [],
        }
        return datasource, expected, payloads

    def _generate(self, project, datasources):
        from powerbi_import.tmdl_generator import generate_tmdl

        with redirect_stdout(io.StringIO()):
            return generate_tmdl(
                datasources, NAME, None, os.path.join(project, NAME + ".SemanticModel"),
                model_mode="import", parameterize=False,
            )

    def _assert_original_bindings(self, project, expected):
        for table, path in expected.items():
            content = _read(project, table)
            paths = re.findall(r'File\.Contents\("((?:[^"]|"")*)"\)', content)
            self.assertEqual(len(paths), 1, content)
            self.assertEqual(
                ntpath.normpath(paths[0].replace('""', '"')), ntpath.normpath(path),
            )
            self.assertIn("mode: import", content)
            self.assertNotIn("DataFolder", content)
            self.assertNotIn("Migration.SourceUnavailable", content)
            self.assertNotIn("otherwise", content)

    def test_pbip_package_skips_native_bodies_but_retains_assets_and_hyper_exports(self):
        threshold = 10 * 1024 * 1024
        for expected_format, hyper_size in ((None, None), ("csv", threshold),
                                             ("parquet", threshold + 1)):
            with self.subTest(hyper_format=expected_format), \
                    tempfile.TemporaryDirectory() as root:
                native, expected, payloads = self._native_sources()
                archive_members = {
                    path.replace("\\", "/"): payload for path, payload in payloads.items()
                }
                source = os.path.join(root, NAME + ".twbx")
                datasources = [native]
                identity = {
                    "workbook": source, "archive_member": "Data/Extract/snapshot.hyper",
                    "schema": "Extract", "table": "Facts",
                }
                if expected_format:
                    datasources.append({
                        "name": "Snapshot",
                        "connection": {"type": "Hyper", "details": {}},
                        "tables": [{
                            "name": "Snapshot", "source_table": "[Extract].[Facts]",
                            "hyper_identity": identity,
                            "columns": [{"name": "id", "datatype": "integer"},
                                        {"name": "label", "datatype": "string"}],
                        }],
                        "calculations": [], "relationships": [],
                    })
                project = os.path.join(root, NAME)
                self._generate(project, datasources)
                self._assert_original_bindings(project, expected)
                metadata = os.path.join(root, "metadata")
                _write(os.path.join(metadata, "datasources.json"), json.dumps(datasources))
                definition = os.path.join(project, NAME + ".SemanticModel", "definition")
                # Force a real DataFolder update; native literal bindings must not follow it.
                expressions = os.path.join(definition, "expressions.tmdl")
                existing = ""
                if os.path.isfile(expressions):
                    with open(expressions, encoding="utf-8") as stream:
                        existing = stream.read()
                existing = re.sub(
                    r"(?m)^expression DataFolder\b[^\n]*\n?", "", existing,
                )
                _write(expressions, existing + '\nexpression DataFolder = "unresolved-before-postprocess"\n')
                image = b"\x89PNG\r\nsynthetic-report-asset"
                visual_path = os.path.join(
                    project, NAME + ".Report", "definition", "pages", "page",
                    "visuals", "image", "visual.json",
                )
                _write(visual_path, json.dumps({"visual": {"objects": {"general": [{
                    "properties": {"imageUrl": {"expr": {"Literal": {
                        "Value": "'Assets/logo.png'",
                    }}}},
                }]}}}))
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr(NAME + ".twb", "<workbook/>")
                    for member, payload in archive_members.items():
                        archive.writestr(member, payload)
                    archive.writestr("Assets/logo.png", image)
                    if expected_format:
                        archive.writestr(identity["archive_member"], b"synthetic Hyper fixture")

                api = StreamingHyperApi()

                def copy_parquet(sql):
                    destination = re.search(r"\bTO\s+'((?:[^']|'')*)'", sql, re.I)
                    self.assertIsNotNone(destination, sql)
                    # Marker only: parent-owned native-SDK validation verifies Parquet bytes.
                    _write(destination.group(1).replace("''", "'"), b"PAR1syntheticCOPYmarkerPAR1")
                    return 2

                api.connection.execute_command.side_effect = copy_parquet
                real_size = os.path.getsize
                staged_hyper = os.path.normcase(os.path.join(project, *identity["archive_member"].split("/")))

                def size(path):
                    if os.path.normcase(os.fspath(path)) == staged_hyper and hyper_size:
                        return hyper_size
                    return real_size(path)

                args = SimpleNamespace(
                    tableau_file=source, output_dir=root, output_format="pbip", quiet=True,
                )
                with forbid_native_payload_io(archive_members=archive_members), \
                        mock.patch.object(migrate, "_get_extract_dir", return_value=metadata), \
                        mock.patch.dict("sys.modules", {"tableauhyperapi": api.module}), \
                        mock.patch.object(os.path, "getsize", side_effect=size), \
                        redirect_stdout(io.StringIO()):
                    migrate._extract_twbx_data_files(args, NAME)

                self._assert_original_bindings(project, expected)
                for member in archive_members:
                    self.assertFalse(os.path.exists(os.path.join(project, *member.split("/"))))
                    self.assertFalse(os.path.exists(os.path.join(
                        project, "Data", ntpath.basename(member),
                    )))
                with open(os.path.join(project, "Data", "Assets", "logo.png"), "rb") as stream:
                    self.assertEqual(stream.read(), image)
                with open(visual_path, encoding="utf-8") as stream:
                    visual = json.load(stream)
                literal = visual["visual"]["objects"]["general"][0]["properties"]["imageUrl"]["expr"]["Literal"]["Value"]
                self.assertEqual(
                    literal.strip("'"),
                    "data:image/png;base64," + base64.b64encode(image).decode("ascii"),
                )
                with open(expressions, encoding="utf-8") as stream:
                    final_expressions = stream.read()
                if expected_format:
                    self.assertNotIn("unresolved-before-postprocess", final_expressions)
                else:
                    self.assertIn(
                        'expression DataFolder = "unresolved-before-postprocess"',
                        final_expressions,
                    )
                manifest = os.path.join(project, "Data", "hyper_exports.json")
                if expected_format is None:
                    api.module.HyperProcess.assert_not_called()
                    self.assertFalse(os.path.exists(manifest))
                    continue
                api.module.HyperProcess.assert_called_once()
                api.connection.execute_list_query.assert_not_called()
                with open(manifest, encoding="utf-8") as stream:
                    records = json.load(stream)["exports"]
                self.assertEqual(len(records), 1)
                record = records[0]
                self.assertEqual(record["identity"], identity)
                self.assertEqual(record["format"], expected_format)
                self.assertEqual(record["columns"], ["id", "label"])
                self.assertEqual(record["row_count"], 2)
                self.assertFalse(record["sampled"])
                self.assertFalse(record["truncated"])
                export = os.path.join(project, "Data", record["filename"])
                if expected_format == "csv":
                    with open(export, newline="", encoding="utf-8") as stream:
                        self.assertEqual(list(csv.reader(stream)), [
                            ["id", "label"], ["1", "first"], ["2", "second"],
                        ])
                else:
                    with open(export, "rb") as stream:
                        self.assertEqual(stream.read(), b"PAR1syntheticCOPYmarkerPAR1")
                snapshot = _read(project, "Snapshot")
                self.assertIn("mode: import", snapshot)
                self.assertIn(record["filename"], snapshot)
                self.assertIn("DataFolder", snapshot)
                self.assertIn("Csv.Document" if expected_format == "csv"
                              else "Parquet.Document", snapshot)
                if expected_format == "parquet":
                    self.assertNotIn("PromoteHeaders", snapshot)

    def test_supported_pbip_worker_never_stages_or_collects_loose_native_bodies(self):
        with tempfile.TemporaryDirectory() as root:
            datasource, expected, payloads = self._native_sources(
                os.path.join(root, "native-sources"),
            )
            for path, payload in payloads.items():
                _write(path, payload)
            source = _write(os.path.join(root, "Workbook.twb"), "<workbook/>")
            metadata = os.path.join(root, "metadata")
            _write(os.path.join(metadata, "datasources.json"), json.dumps([datasource]))
            output = os.path.join(root, "output")
            project = os.path.join(output, NAME)

            def generate(**kwargs):
                self.assertEqual(kwargs["report_name"], NAME)
                self.assertEqual(kwargs["output_format"], "pbip")
                self.assertEqual(kwargs["model_mode"], "import")
                self._generate(project, [datasource])
                _write(os.path.join(project, NAME + ".pbip"), "{}")
                return True

            with forbid_native_payload_io(payloads), \
                    mock.patch.object(migrate, "_get_extract_dir", return_value=metadata), \
                    mock.patch.object(migrate, "run_generation", side_effect=generate), \
                    mock.patch.object(migrate, "run_migration_report", return_value=None), \
                    redirect_stdout(io.StringIO()):
                result = migrate._migrate_single_workbook(
                    source, NAME, output, NAME, skip_extraction=True,
                    wb_prep=None, wb_cal_start=None, wb_cal_end=None, wb_culture=None,
                    verify_open=False, output_format="pbip", model_mode="import", quiet=True,
                )
            self.assertTrue(result["success"], result)
            self._assert_original_bindings(project, expected)
            for path, payload in payloads.items():
                with open(path, "rb") as stream:
                    self.assertEqual(stream.read(), payload)
                self.assertFalse(os.path.exists(os.path.join(project, "Data", os.path.basename(path))))

    def test_explicit_fabric_still_stages_csv_body_and_canonical_notebook_input(self):
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"NativeFacts": _csv_partition(
                "facts.csv", ["id", "label"],
            )})
            os.makedirs(os.path.join(project, NAME + ".Notebook"))
            metadata = os.path.join(root, "metadata")
            _write(os.path.join(metadata, "datasources.json"), json.dumps([{
                "name": "NativeCSV",
                "connection": {"type": "CSV", "details": {
                    "directory": "Data", "filename": "facts.csv",
                }},
                "tables": [{
                    "name": "NativeFacts", "source_table": "[facts#csv]",
                    "columns": [{"name": "id"}, {"name": "label"}],
                }],
            }]))
            source = os.path.join(root, NAME + ".twbx")
            payload = b"id,label\n1,first\n2,second\n"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr(NAME + ".twb", "<workbook/>")
                archive.writestr("Data/facts.csv", payload)
            args = SimpleNamespace(
                tableau_file=source, output_dir=root, output_format="fabric", quiet=True,
            )
            with mock.patch.object(migrate, "_get_extract_dir", return_value=metadata), \
                    redirect_stdout(io.StringIO()):
                migrate._extract_twbx_data_files(args, NAME)
            with open(os.path.join(project, "Data", "facts.csv"), "rb") as stream:
                self.assertEqual(stream.read(), payload)
            with open(os.path.join(project, "Data", "nativefacts.csv"),
                      newline="", encoding="utf-8") as stream:
                self.assertEqual(list(csv.reader(stream)), [
                    ["id", "label"], ["1", "first"], ["2", "second"],
                ])
            self.assertIn("DataFolder", _read(project, "NativeFacts"))
            with open(os.path.join(project, NAME + ".SemanticModel", "definition",
                                   "expressions.tmdl"), encoding="utf-8") as stream:
                self.assertIn(
                    os.path.abspath(os.path.join(project, "Data")).replace("\\", "\\\\"),
                    stream.read(),
                )

    def test_skipped_native_zip_payload_does_not_bypass_traversal_validation(self):
        for member in ("../outside.csv", "Native/../../outside.xlsx"):
            with self.subTest(member=member), tempfile.TemporaryDirectory() as root:
                source = os.path.join(root, NAME + ".twbx")
                project = os.path.join(root, NAME)
                os.makedirs(project)
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr(NAME + ".twb", "<workbook/>")
                    archive.writestr("Data/safe.csv", b"id\n1\n")
                    archive.writestr(member, b"must not escape")
                with forbid_native_payload_io(archive_members={"Data/safe.csv", member}), \
                        self.assertRaisesRegex(
                            RuntimeError, "Could not extract TWBX data files",
                        ) as raised:
                    migrate._process_twbx_post_generation(
                        source, project, NAME, output_format="pbip",
                    )
                self.assertIsInstance(raised.exception.__cause__, migrate.SecurityError)
                self.assertFalse(os.path.exists(os.path.join(root, "outside.csv")))
                self.assertFalse(os.path.exists(os.path.join(root, "outside.xlsx")))
                self.assertFalse(os.path.exists(os.path.join(project, "Data", "safe.csv")))


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
            data = os.path.join(root, NAME, "Data")
            identities, members, metadata = {}, [], []
            for model, filename, columns, rows in (
                ("Sales", "sales_extract.hyper", ["Region", "Amount"], [("N", "1")]),
                ("Stock", "stock_extract.hyper", ["Item", "Qty"], [("A", "2")]),
            ):
                identity, member = _sqlite_hyper_fixture(
                    root, data, filename, columns, rows, table=model + " Physical",
                )
                identities[model] = identity
                members.append(member)
                metadata.append({
                    "name": model, "hyper_identity": identity,
                    "cols_physical_map": {column: column for column in columns},
                    "columns": [{"name": column, "datatype": "string"}
                                for column in columns],
                })
            project = _project(root, {
                "Sales": _excel_partition("sales.xlsx", ["Region", "Amount"]),
                "Stock": _excel_partition("stock.xlsx", ["Item", "Qty"]),
            }, identities=identities, source_columns={
                "Sales": {"Region": "string", "Amount": "string"},
                "Stock": {"Item": "string", "Qty": "string"},
            })
            stale = {
                "sales_extract.csv": b"Region,Amount\nstale,-1\n",
                "stock_extract.csv": b"Item,Qty\nstale,-1\n",
            }
            for filename, payload in stale.items():
                _write(os.path.join(data, filename), payload)
            extract = os.path.join(root, "extract")
            _write(os.path.join(extract, "datasources.json"),
                   json.dumps([{"tables": metadata}]))
            migrate._convert_hyper_to_csv_in_data(
                data, NAME, project, packaged_hyper=members, extract_dir=extract,
            )
            with open(os.path.join(data, "hyper_exports.json"), encoding="utf-8") as stream:
                records = json.load(stream)["exports"]
            self.assertEqual(len(records), 2)
            exports = {record["identity"]["table"]: record for record in records}
            for model, columns, row in (
                ("Sales", ["Region", "Amount"], ["N", "1"]),
                ("Stock", ["Item", "Qty"], ["A", "2"]),
            ):
                record = exports[model + " Physical"]
                filename = record["filename"]
                self.assertEqual(record["identity"], identities[model])
                self.assertEqual(
                    record, _full_export_record(os.path.join(data, filename),
                                                identities[model]),
                )
                self.assertIn(filename, _read(project, model))
                self.assertNotIn(filename, _read(
                    project, "Stock" if model == "Sales" else "Sales",
                ))
                with open(os.path.join(data, filename), newline="",
                          encoding="utf-8") as stream:
                    self.assertEqual(list(csv.reader(stream)), [columns, row])
            for filename, payload in stale.items():
                with open(os.path.join(data, filename), "rb") as stream:
                    self.assertEqual(stream.read(), payload)

    def test_packaged_excel_file_is_kept(self):
        with tempfile.TemporaryDirectory() as root:
            data = os.path.join(root, NAME, "Data")
            identity, member = _sqlite_hyper_fixture(
                root, data, "e.hyper", ["Region"], [("N",)],
            )
            project = _project(root, {
                "Sales": _excel_partition("sales.xlsx", ["Region"]),
                "Snapshot": _excel_partition("snapshot-not-packaged.xlsx", ["Region"]),
            }, identities={"Snapshot": identity},
                source_columns={"Snapshot": {"Region": "string"}})
            excel_bytes = b"PK\x03\x04synthetic-packaged-excel\x00\xff"
            _write(os.path.join(data, "sales.xlsx"), excel_bytes)
            old_csv = b"Region\nstale\n"
            _write(os.path.join(data, "e.csv"), old_csv)
            _write(os.path.join(root, "datasources.json"), json.dumps([{"tables": [{
                "name": "Snapshot", "hyper_identity": identity,
                "cols_physical_map": {"Region": "Region"},
                "columns": [{"name": "Region", "datatype": "string"}],
            }]}]))
            before = _read(project, "Sales")
            migrate._convert_hyper_to_csv_in_data(
                data, NAME, project, packaged_hyper=[member], extract_dir=root,
            )
            self.assertEqual(_read(project, "Sales"), before)
            self.assertIn("Excel.Workbook", before)
            with open(os.path.join(data, "sales.xlsx"), "rb") as stream:
                self.assertEqual(stream.read(), excel_bytes)
            with open(os.path.join(data, "e.csv"), "rb") as stream:
                self.assertEqual(stream.read(), old_csv)
            with open(os.path.join(data, "hyper_exports.json"), encoding="utf-8") as stream:
                records = json.load(stream)["exports"]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0], _full_export_record(
                os.path.join(data, records[0]["filename"]), identity,
            ))
            self.assertIn(records[0]["filename"], _read(project, "Snapshot"))

    def test_uses_datasource_metadata_from_active_extract_directory(self):
        with tempfile.TemporaryDirectory() as root:
            data = os.path.join(root, NAME, "Data")
            identity, member = _sqlite_hyper_fixture(
                root, data, "orders_extract.hyper", ["PhysicalOrder"], [("123",)],
            )
            project = _project(root, {
                "Orders": '\t\t\t\tSource = try #table({"Order ID"}, {}) '
                          'otherwise #table({"Order ID"}, {})\n'
            }, identities={"Orders": identity},
                source_columns={"Orders": {"Order ID": "string"}})
            _write(os.path.join(data, "orders_extract.csv"),
                   "PhysicalOrder\nstale\n")
            extract_dir = os.path.join(root, "extract")
            _write(os.path.join(extract_dir, "datasources.json"), json.dumps([{
                "tables": [{
                    "name": "Orders",
                    "source_table": "orders_extract",
                    "hyper_identity": identity,
                    "cols_physical_map": {"PhysicalOrder": "Order ID"},
                    "columns": [{"name": "Order ID", "datatype": "string"}],
                }]
            }]))

            with mock.patch.object(migrate._extract_dir_local, "path", extract_dir,
                                   create=True):
                migrate._convert_hyper_to_csv_in_data(
                    data, NAME, project, packaged_hyper=[member],
                )

            with open(os.path.join(data, "hyper_exports.json"), encoding="utf-8") as stream:
                record = json.load(stream)["exports"][0]
            self.assertEqual(record["identity"], identity)
            staged = record["staged_files"][0]["csv_filename"]
            self.assertRegex(staged, r"^hyper_staged_[0-9a-f]{64}\.csv$")
            self.assertIn(staged, _read(project, "Orders"))
            with open(os.path.join(data, staged), encoding="utf-8",
                      newline="") as stream:
                self.assertEqual(list(csv.reader(stream)), [["Order ID"], ["123"]])
            raw_proof = _full_export_record(
                os.path.join(data, record["filename"]), identity,
            )
            self.assertEqual({key: record[key] for key in raw_proof}, raw_proof)
            with open(os.path.join(data, "orders_extract.csv"), "rb") as stream:
                self.assertEqual(stream.read(), b"PhysicalOrder\nstale\n")

    def test_extract_csv_name_follows_hyper_file_not_inner_table(self):
        with tempfile.TemporaryDirectory() as out:
            hyper = os.path.join(out, "federated_abc.hyper")
            _write_sqlite_hyper(hyper, ["id"], [("synthetic",)], table="Inner Physical")
            name = os.path.splitext(os.path.basename(hyper))[0]
            result = migrate._hyper_to_csv_files(
                hyper, out, NAME, csv, require_full=True,
            )
            self.assertEqual(dict(result), {name: name + ".csv"})
            self.assertEqual(result.exports, [_full_export_record(
                os.path.join(out, name + ".csv"),
                {"schema": "main", "table": "Inner Physical"},
            )])
            with open(os.path.join(out, name + ".csv"), encoding="utf-8",
                      newline="") as stream:
                self.assertEqual(list(csv.reader(stream)), [["id"], ["synthetic"]])


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
        declared = {}
        if (len(identities) == 1 and isinstance(identities[0], dict)
                and set(identities[0]) == {"workbook", "archive_member", "schema", "table"}):
            declared["Model"] = identities[0]
        project = _project(
            self.root, {"Model": _excel_partition("missing.xlsx", ["id"])},
            identities=declared, source_columns={"Model": {"id": "string"}},
        )
        data = os.path.join(project, "Data")
        extract = os.path.join(self.root, "extract")
        _write(os.path.join(extract, "datasources.json"), json.dumps([
            {"name": f"source-{index}", "tables": [{
                "name": "Model", "source_table": "legacy",
                "columns": [{"name": "id", "datatype": "string"}],
                "cols_physical_map": {"id": "id"},
                **({"hyper_identity": identity} if identity is not None else {}),
            }]}
            for index, identity in enumerate(identities)
        ]))
        patcher = mock.patch.object(migrate, "_get_extract_dir", return_value=extract)
        patcher.start()
        self.addCleanup(patcher.stop)
        return project, data

    def _manifest(self, data, records):
        # Identity/ambiguity tests must not pass just because old fixture
        # records lack the independent full-export prerequisite.
        for record in records:
            filename = record.get("filename") or record["csv_filename"]
            record.update(_full_export_record(
                os.path.join(data, filename), record["identity"],
                columns=record["columns"],
            ))
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
        # Real Hyper results are context-managed single-pass cursors.
        api = StreamingHyperApi(
            columns=columns, rows=[(1, "synthetic", 3)], total_rows=1,
            table_pairs=catalog_pairs,
        )
        connection = api.connection
        _write(os.path.join(self.root, "staged.hyper"), b"Hyper synthetic fixture")
        with mock.patch.dict("sys.modules", {"tableauhyperapi": api.module}), \
                mock.patch("sqlite3.connect") as sqlite_reader, \
                mock.patch("tableau_export.hyper_reader.read_hyper") as fallback_reader:
            result = migrate._hyper_to_csv_files(
                os.path.join(self.root, "staged.hyper"), self.root, NAME, csv,
                identity=self.package_identity, require_full=True,
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
            self.assertEqual(json.load(stream), {
                "version": 1, "authoritative": True, "exports": result.exports,
            })
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
            self.assertEqual(record, _full_export_record(
                os.path.join(self.root, record["filename"]), record["identity"],
                columns=columns, row_count=1,
            ))
            with open(os.path.join(self.root, record["csv_filename"]),
                      encoding="utf-8", newline="") as stream:
                self.assertEqual(list(csv.reader(stream)), [columns, ["1", "synthetic", "3"]])
        self.assertEqual(connection.close.call_count, 2)
        connection.execute_list_query.assert_not_called()
        self.assertTrue(all(result.closed for result in api.results))

    def test_project_reader_fallback_preserves_long_identity_and_column_order(self):
        names = ["Long." + "Ab-" * 120, "Long." + "ab-" * 120]
        columns = ["z", "réalisé (%)", "a"]
        tables = [
            {"schema": "Extract", "table": name,
             "columns": [{"name": column, "type": "text"} for column in columns],
             "sample_rows": [[1, 2, 3]], "row_count": 9, "sample_row_count": 1}
            for name in names
        ]
        with mock.patch.dict("sys.modules", {"tableauhyperapi": None}), \
                mock.patch("sqlite3.connect", side_effect=OSError("synthetic non-SQLite file")), \
                mock.patch("tableau_export.hyper_reader.read_hyper",
                           return_value={"tables": tables}), \
                self.assertLogs(migrate.logger.name, level="WARNING") as warnings:
            result = migrate._hyper_to_csv_files(
                "synthetic.hyper", self.root, NAME, csv,
                identity=self.package_identity, require_full=False,
            )
        self.assertEqual([r["identity"] for r in result.exports],
                         [self._identity(table=name) for name in names])
        self.assertEqual([r["columns"] for r in result.exports], [columns, columns])
        self.assertEqual(len(set(result.values())), 2)
        for filename in result.values():
            self.assertRegex(filename, r"^hyper_[0-9a-f]{64}\.csv$")
            with open(os.path.join(self.root, filename), encoding="utf-8",
                      newline="") as stream:
                self.assertEqual(list(csv.reader(stream)), [columns, ["1", "2", "3"]])
        for record in result.exports:
            self.assertEqual(record["row_count"], 1)
            self.assertEqual(record["total_rows"], 9)
            self.assertIs(record["sampled"], True)
            self.assertIs(record["truncated"], True)
            self.assertIn("sample", record["sampling_method"])
        self.assertTrue(any("not a full extract" in line for line in warnings.output))

    def test_explicit_package_reads_exact_path_despite_prefix_csv_and_writes_manifest(self):
        project, data = self._routing_project([None])
        path = _write(os.path.join(project, "Elsewhere", "data.hyper"), b"synthetic")
        _write(os.path.join(data, "data_old.csv"), "id\nwrong\n")
        api = StreamingHyperApi(
            columns=["id"], rows=[("right",)], total_rows=1,
            table_pairs=[("Extract", "Physical")],
        )
        real_reader = migrate._hyper_to_csv_files
        fresh_records = []

        def export_exact_member(hyper_path, output_dir, prefix, csv_module, **kwargs):
            self.assertEqual(hyper_path, path)
            self.assertEqual(kwargs["identity"], self.package_identity)
            self.assertEqual(os.path.dirname(output_dir), data)
            self.assertNotEqual(output_dir, data)
            converted = real_reader(hyper_path, output_dir, prefix, csv_module, **kwargs)
            fresh_records.extend(dict(record) for record in converted.exports)
            return converted

        with mock.patch.dict("sys.modules", {"tableauhyperapi": api.module}), \
                mock.patch.object(migrate, "_hyper_to_csv_files",
                               side_effect=export_exact_member) as reader:
            migrate._convert_hyper_to_csv_in_data(
                data, NAME, project,
                packaged_hyper=[{"path": path, "identity": self.package_identity}],
            )
        reader.assert_called_once()
        export_stage = reader.call_args.args[1]
        reader.assert_called_once_with(
            path, export_stage, NAME, csv, identity=self.package_identity,
            export_format="auto", max_rows=None, quiet=False, require_full=True,
        )
        self.assertFalse(os.path.exists(export_stage))
        self.assertEqual(len(fresh_records), 1)
        record = fresh_records[0]
        filename = record["filename"]
        self.assertEqual(record, _full_export_record(
            os.path.join(data, filename), self._identity(), row_count=1,
        ))
        with open(os.path.join(data, filename), encoding="utf-8") as stream:
            self.assertEqual(stream.read(), "id\nright\n")
        with open(os.path.join(data, "data_old.csv"), encoding="utf-8") as stream:
            self.assertEqual(stream.read(), "id\nwrong\n")
        with open(os.path.join(data, "hyper_exports.json"), encoding="utf-8") as stream:
            self.assertEqual(json.load(stream), {
                "version": 1, "authoritative": True, "exports": [record],
            })
        api.connection.execute_list_query.assert_not_called()
        self.assertTrue(api.results)
        self.assertTrue(all(result.closed for result in api.results))

    def test_empty_selected_reader_raises_and_preserves_manifest_without_fallback(self):
        project, data = self._routing_project([None])
        path = _write(os.path.join(data, "data.hyper"), b"Hyper synthetic fixture")
        self.assertTrue(os.path.isfile(path))
        _write(os.path.join(data, "data_old.csv"), "id\nwrong\n")
        _write(os.path.join(data, "legacy.csv"), "id\nstale\n")
        before = _read(project, "Model")
        manifest_path = os.path.join(data, "hyper_exports.json")
        for existing_manifest in (False, True):
            with self.subTest(existing_manifest=existing_manifest):
                if existing_manifest:
                    self._manifest(data, [{
                        "identity": self._identity(), "format": "csv",
                        "filename": "data_old.csv", "csv_filename": "data_old.csv",
                        "columns": ["id"], "row_count": 1, "total_rows": 1,
                        "max_rows": None, "sampled": False, "truncated": False,
                    }])
                    with open(manifest_path, "rb") as stream:
                        previous_manifest = stream.read()
                baseline = set(os.listdir(data))
                staged_directories = []

                def empty_export(hyper_path, output_dir, *args, **kwargs):
                    self.assertEqual(hyper_path, path)
                    self.assertEqual(kwargs["identity"], self.package_identity)
                    self.assertNotEqual(output_dir, data)
                    self.assertTrue(os.path.isdir(output_dir))
                    staged_directories.append(output_dir)
                    # No injected exception: production must reject an empty
                    # export set. A valid empty table still has an export record.
                    return migrate._HyperCsvExports()

                with mock.patch.object(migrate, "_hyper_to_csv_files",
                                       side_effect=empty_export) as reader, \
                        mock.patch.object(migrate, "_pick_csv_for_columns") as fallback, \
                        mock.patch.object(migrate, "_stage_pending_hyper_partition") as binding:
                    for attempt in range(2):
                        with self.assertRaises(GenerationError) as raised:
                            migrate._convert_hyper_to_csv_in_data(
                                data, NAME, project,
                                packaged_hyper=[{
                                    "path": path, "identity": self.package_identity,
                                }],
                            )
                        self.assertRegex(str(raised.exception), "(?i)hyper.*no exportable")
                        self.assertEqual(
                            raised.exception.item, self.package_identity["archive_member"],
                        )
                        self.assertEqual(reader.call_count, attempt + 1)
                    fallback.assert_not_called()
                    binding.assert_not_called()
                self.assertEqual(len(staged_directories), 2)
                self.assertTrue(all(not os.path.exists(p) for p in staged_directories))
                self.assertEqual(set(os.listdir(data)), baseline)
                if existing_manifest:
                    with open(manifest_path, "rb") as stream:
                        self.assertEqual(stream.read(), previous_manifest)
                else:
                    self.assertFalse(os.path.exists(manifest_path))
                self.assertEqual(_read(project, "Model"), before)
                with open(os.path.join(data, "data_old.csv"), encoding="utf-8") as stream:
                    self.assertEqual(stream.read(), "id\nwrong\n")
                with open(os.path.join(data, "legacy.csv"), encoding="utf-8") as stream:
                    self.assertEqual(stream.read(), "id\nstale\n")

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
            "columns": [{"name": "Identifier", "datatype": "string"}],
        }]}]))
        body = (
            '\t\t\t\tlet\n'
            '\t\t\t\t\tSource = try #table({"Identifier"}, {}) '
            'otherwise #table({"Identifier"}, {})\n'
            '\t\t\t\tin\n\t\t\t\t\tSource'
        )
        project = _project(
            self.root, {"Model": body}, identities={"Model": self._identity()},
            source_columns={"Model": {"Identifier": "string"}},
        )
        filename = "hyper_" + "a" * 64 + ".csv"
        raw = "id\nsynthetic\n"
        _write(os.path.join(data, filename), raw)
        record = {"identity": self._identity(), "csv_filename": filename, "columns": ["id"]}
        self._manifest(data, [record])
        raw_proof = dict(record)
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
        with open(os.path.join(data, filename), "rb") as stream:
            self.assertEqual(stream.read(), raw.encode("utf-8"))
        self.assertEqual(
            {key: exported[key] for key in raw_proof}, raw_proof,
        )
        staged_proof = _full_export_record(
            os.path.join(data, staged["csv_filename"]), self._identity(),
            columns=["Identifier"], row_count=1,
        )
        for key in ("sha256", "size_bytes", "row_count"):
            self.assertEqual(staged[key], staged_proof[key])
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
                with open(os.path.join(project, *member.split("/")), "rb") as stream:
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
                {"path": os.path.join(project, *member.split("/")),
                 "identity": {"workbook": self.workbook, "archive_member": member}}
                for member in members
            ],
            hyper_export_format="auto", hyper_export_max_rows=None,
            output_format="pbip", quiet=False, max_zip_entry_mb=1024,
            merge_manifest=False, extract_dir=None,
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
            _bind_object_context_extract,
            extract_tables_with_columns,
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

    def test_parquet_binding_keeps_captions_types_and_downstream_m_without_headers(self):
        from powerbi_import.m_validator import validate_m_query

        # Keep this source-binding regression independent of the pre-existing
        # DAX apostrophe-quoting bug; other identity tests retain O'Brien.
        self.table = "Synthetic Facts réalisé"
        datasource = self._transformed_datasource()
        model = self._model([datasource])
        content = self._content(model["model"]["tables"][0])
        filename = "hyper_" + "b" * 64 + ".parquet"
        _write(os.path.join(self.data, filename), b"PAR1synthetic-markerPAR1")
        record = {
            "identity": dict(self.identity), "format": "parquet", "filename": filename,
            "columns": ["raw_code", "Amount"], "row_count": 2, "total_rows": 2,
            "max_rows": None, "sampled": False, "truncated": False,
        }
        bound = migrate._stage_pending_hyper_partition(content, self.data, [record])
        self.assertNotEqual(bound, content)
        self.assertIn("Parquet.Document(File.Contents(", bound)
        self.assertIn(filename, bound)
        self.assertNotIn("Csv.Document", bound)
        self.assertNotIn("PromoteHeaders", bound)
        self.assertNotIn("Migration.HyperExportRequired", bound)
        self.assertEqual(
            bound.split("\n\tpartition ", 1)[0], content.split("\n\tpartition ", 1)[0],
            "Parquet source replacement must not change model captions or column types",
        )
        for step in (
            'Table.RenameColumns(Source, {{"raw_code", "Code"}})',
            '{{"Code", "Display Code"}}',
            '"Double Amount", each ([Amount] * 2), type number)',
        ):
            self.assertIn(step, content)
            self.assertIn(step, bound)
        self.assertEqual(validate_m_query(self._expression(bound)), [])
        self.assertEqual(record["identity"], self.identity)
        self.assertEqual(record["columns"], ["raw_code", "Amount"])
        self.assertEqual(
            migrate._stage_pending_hyper_partition(bound, self.data, [record]), bound,
        )

    def test_parquet_manifest_is_routed_in_postprocessing_by_exact_identity_not_headers(self):
        content = self._content()
        project = _project(self.root, {})
        data = os.path.join(project, "Data")
        path = os.path.join(project, f"{NAME}.SemanticModel", "definition",
                            "tables", "unrelated-artifact.tmdl")
        _write(path, content)
        filename = "hyper_" + "c" * 64 + ".parquet"
        artifact = _write(os.path.join(data, filename), b"PAR1synthetic-markerPAR1")
        # Static routing fixture only; native SDK tests validate real Parquet.
        record = _full_export_record(
            artifact, self.identity, columns=["id", "Amount"],
            row_count=2, fmt="parquet",
        )
        manifest = os.path.join(data, "hyper_exports.json")
        extract = os.path.join(self.root, "metadata")
        _write(os.path.join(extract, "datasources.json"), json.dumps([self._datasource()]))
        # Matching headers are not permission to borrow a different physical table.
        foreign = dict(record, identity=dict(self.identity, schema="foreign"))
        _write(manifest, json.dumps({"version": 1, "exports": [foreign]}))
        with mock.patch.object(migrate, "_get_extract_dir", return_value=extract), \
                mock.patch.object(migrate, "_hyper_to_csv_files") as reader:
            migrate._convert_hyper_to_csv_in_data(data, NAME, project)
            with open(path, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), content)
            _write(manifest, json.dumps({"version": 1, "exports": [record]}))
            migrate._convert_hyper_to_csv_in_data(data, NAME, project)
            with open(path, encoding="utf-8") as stream:
                bound = stream.read()
            migrate._convert_hyper_to_csv_in_data(data, NAME, project)
        reader.assert_not_called()
        self.assertIn("Parquet.Document", bound)
        self.assertIn(filename, bound)
        self.assertNotIn("PromoteHeaders", bound)
        self.assertNotIn("Migration.HyperExportRequired", bound)
        with open(path, encoding="utf-8") as stream:
            self.assertEqual(stream.read(), bound)

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
        from powerbi_import.m_validator import validate_m_query
        from powerbi_import.tmdl_generator import _write_table_tmdl
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
        artifact = _write(os.path.join(data, filename), "id,Other\n1,3.5\n")
        record = _full_export_record(
            artifact, second_identity, columns=["id", "Other"], row_count=1,
        )
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
        artifact = _write(os.path.join(data, self.filename), "id,Amount\n1,2.5\n")
        _write(os.path.join(data, "legacy.csv"), "id,Amount\n1,999\n")
        record = _full_export_record(
            artifact, dict(self.identity, schema="foreign"),
            columns=["id", "Amount"], row_count=1,
        )
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
            '\t\tsourceColumn: "Customer\'s ""Amount"""\n'
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

    def _make_project(self, root, *, hyper_filename="orders_extract.hyper",
                      rows=None, pending=False, physical_columns=None):
        data = os.path.join(root, NAME, "Data")
        self.physical_columns = physical_columns or [
            "qty_raw", "amount_raw", "date_raw", "updated_raw",
        ]
        self.source_rows = rows if rows is not None else [
            ["2", "1234.5", "2026-10-15", "2026-10-15 09:30:00"],
        ]
        self.identity, member = _sqlite_hyper_fixture(
            root, data, hyper_filename, self.physical_columns, self.source_rows,
            table="Physical Orders",
        )
        self.packaged_hyper = [member]
        self.pending = pending
        source = (
            '\t\t\t\tlet\n'
            '\t\t\t\t\tSource = try #table({"Qty", "Amount", "Order Date", "Updated"}, '
            '{{2, 1234.5, #date(2026, 10, 15), #datetime(2026, 10, 15, 9, 30, 0)}}) '
            'otherwise #table({"Qty", "Amount", "Order Date", "Updated"}, {}),\n'
        )
        if pending:
            source = (
                '\t\t\t\tlet\n\t\t\t\t\tSource = '
                + _pending_hyper_error("Orders", self.identity) + ",\n"
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
        project = _project(
            root, {"Orders": source + downstream}, identities={"Orders": self.identity},
        )
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
        prior = io.StringIO(newline="")
        writer = csv.writer(prior)
        writer.writerow(self.physical_columns)
        writer.writerows(self.source_rows)
        self.legacy_bytes = prior.getvalue().encode("utf-8")
        self.legacy_csv = os.path.join(
            data, os.path.splitext(hyper_filename)[0] + ".csv",
        )
        _write(self.legacy_csv, self.legacy_bytes)
        extract = os.path.join(root, "extract")
        metadata = [{
            "tables": [{
                "name": "Orders", "source_table": os.path.splitext(hyper_filename)[0],
                "hyper_identity": self.identity,
                "cols_physical_map": dict(zip(
                    self.physical_columns, ["Qty", "Amount", "Order Date", "Updated"],
                )),
                "columns": [
                    *({"name": column, "datatype": datatype}
                      for column, datatype in zip(self.physical_columns, (
                          "integer", "real", "date", "datetime",
                      ))),
                    {"name": "Margin", "datatype": "real"},
                    {"name": "Year", "datatype": "integer"},
                ],
            }],
        }]
        _write(os.path.join(extract, "datasources.json"), json.dumps(metadata))
        return project, data, extract, downstream, metadata

    def _convert(self, data, project):
        source = self.packaged_hyper[0]["path"]
        with open(source, "rb") as stream:
            source_bytes = stream.read()
        migrate._convert_hyper_to_csv_in_data(
            data, NAME, project, packaged_hyper=self.packaged_hyper,
        )
        with open(os.path.join(data, "hyper_exports.json"), encoding="utf-8") as stream:
            manifest = json.load(stream)
        self.assertIs(manifest["authoritative"], True)
        self.assertEqual(len(manifest["exports"]), 1)
        self.export = manifest["exports"][0]
        raw_path = os.path.join(data, self.export["filename"])
        proof = _full_export_record(
            raw_path, self.identity, columns=self.physical_columns,
            row_count=len(self.source_rows),
        )
        self.assertEqual({key: self.export[key] for key in proof}, proof)
        self.assertEqual(len(self.export["staged_files"]), 1)
        binding = self.export["staged_files"][0]
        self.assertEqual(binding["table"], "Orders")
        self.bound_filename = binding["csv_filename"]
        self.assertNotEqual(self.bound_filename, os.path.basename(self.legacy_csv))
        if not self.pending:
            staged_proof = _full_export_record(
                os.path.join(data, self.bound_filename), self.identity,
                row_count=len(self.source_rows),
            )
            for key in ("sha256", "size_bytes", "row_count"):
                self.assertEqual(binding[key], staged_proof[key])
        with open(self.legacy_csv, "rb") as stream:
            self.assertEqual(stream.read(), self.legacy_bytes)
        with open(source, "rb") as stream:
            self.assertEqual(stream.read(), source_bytes)

    def _assert_source_types(self, text, entries, downstream):
        separator = "\\" if self.pending else "\\\\"
        expected_source = (
            'Source = Table.TransformColumnTypes(Table.PromoteHeaders('
            'Csv.Document(File.Contents(DataFolder & "' + separator
            + self.bound_filename + '"), '
            '[Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]), '
            '[PromoteAllScalars=true]), {' + ", ".join(entries) + '}, "en-US"),\n'
        )
        self.assertIn(expected_source + downstream, text)
        source = text.split('Source = ', 1)[1].split('\n', 1)[0]
        self.assertNotIn('"Margin"', source)
        self.assertNotIn('"Year"', source)
        self.assertNotIn("#table(", text)

    def _same_path_case(self, root):
        rows = [
            ["2", "1234.5", "2026-10-15", "2026-10-15 09:30:00"],
            ["7", "0.25", "2026-10-22", "2026-10-22 13:15:05"],
        ]
        project, data, extract, downstream, unused_metadata = self._make_project(
            root, hyper_filename="Orders.hyper", rows=rows,
        )
        return project, data, extract, downstream, rows

    def _assert_same_path_result(self, project, data, downstream, rows):
        with open(os.path.join(data, self.bound_filename),
                  encoding="utf-8", newline="") as stream:
            self.assertEqual(
                list(csv.reader(stream)),
                [["Qty", "Amount", "Order Date", "Updated"]] + rows,
                "Caption staging must preserve every row of the complete source",
            )
        self._assert_source_types(
            _read(project, "Orders"),
            ['{"Qty", Int64.Type}', '{"Amount", type number}',
             '{"Order Date", type date}', '{"Updated", type datetime}'],
            downstream,
        )

    def test_source_equals_staged_csv_retains_both_rows_captions_and_source_types(self):
        for output_format in ("pbip", "fabric"):
            with self.subTest(output_format=output_format), \
                    tempfile.TemporaryDirectory() as root:
                project, data, extract, downstream, rows = self._same_path_case(root)
                with mock.patch.object(migrate, "_get_extract_dir", return_value=extract):
                    if output_format == "pbip":
                        self._convert(data, project)
                    else:
                        # Genuine legacy diagnostic path: only this mode can
                        # reuse and rewrite Orders.csv in place. Full PBIP
                        # instead proves a fresh raw export and a separate alias.
                        migrate._convert_hyper_to_csv_in_data(
                            data, NAME, project, output_format="fabric",
                        )
                        self.bound_filename = "Orders.csv"
                self._assert_same_path_result(project, data, downstream, rows)
                if output_format == "pbip":
                    self.assertRegex(
                        self.bound_filename, r"^hyper_staged_[0-9a-f]{64}\.csv$",
                    )
                    self.assertEqual(set(os.listdir(data)), {
                        "Orders.hyper", "Orders.csv", "hyper_exports.json",
                        self.export["filename"], self.bound_filename,
                    })
                else:
                    self.assertEqual(set(os.listdir(data)), {"Orders.hyper", "Orders.csv"})

    def test_caption_staging_consumes_csv_as_a_stream_not_list_reader(self):
        real_reader = csv.reader

        class StreamingOnlyReader:
            def __init__(self, *args, **kwargs):
                self.reader = real_reader(*args, **kwargs)

            def __iter__(self):
                return self

            def __next__(self):
                return next(self.reader)

            def __length_hint__(self):
                raise AssertionError("caption staging must not materialize list(reader)")

        with tempfile.TemporaryDirectory() as root:
            project, data, extract, downstream, rows = self._same_path_case(root)
            with mock.patch.object(migrate, "_get_extract_dir", return_value=extract), \
                    mock.patch.object(csv, "reader", side_effect=StreamingOnlyReader):
                self._convert(data, project)
            self._assert_same_path_result(project, data, downstream, rows)

    def test_restores_metadata_types_immediately_after_promotion_before_calculations(self):
        with tempfile.TemporaryDirectory() as root:
            project, data, extract, downstream, _ = self._make_project(root)
            with mock.patch.object(migrate._extract_dir_local, "path", extract, create=True):
                self._convert(data, project)
            self._assert_source_types(_read(project, "Orders"), [
                '{"Qty", Int64.Type}', '{"Amount", type number}',
                '{"Order Date", type date}', '{"Updated", type datetime}',
            ], downstream)
            with open(os.path.join(data, self.bound_filename),
                      encoding="utf-8", newline="") as stream:
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
                self._convert(data, project)
            self._assert_source_types(_read(project, "Orders"), [
                '{"Qty", Int64.Type}', '{"Amount", type number}',
                '{"Order Date", type date}', '{"Updated", type datetime}',
            ], downstream)

    def test_unavailable_metadata_falls_back_to_model_source_columns(self):
        for snapshot in (None, "{invalid json", "[]"):
            with self.subTest(snapshot=snapshot), tempfile.TemporaryDirectory() as root:
                project, data, extract, downstream, _ = self._make_project(
                    root, pending=True,
                    physical_columns=["Qty", "Amount", "Order Date", "Updated"],
                )
                path = os.path.join(extract, "datasources.json")
                if snapshot is None:
                    os.unlink(path)
                else:
                    _write(path, snapshot)
                # The semantic producer's explicit identity, not header overlap,
                # authorizes the full source when type metadata is unavailable.
                with mock.patch.object(migrate._extract_dir_local, "path", extract, create=True):
                    self._convert(data, project)
                self._assert_source_types(_read(project, "Orders"), [
                    '{"Qty", Int64.Type}', '{"Amount", type number}',
                    '{"Order Date", type datetime}', '{"Updated", type datetime}',
                ], downstream)

    def test_quoted_headers_survive_csv_routing_and_source_type_literals(self):
        with tempfile.TemporaryDirectory() as root:
            data = os.path.join(root, NAME, "Data")
            self.physical_columns = ['Customer\'s "Amount"']
            self.source_rows = [["1234.5"]]
            self.identity, member = _sqlite_hyper_fixture(
                root, data, "orders_extract.hyper",
                self.physical_columns, self.source_rows,
            )
            self.packaged_hyper = [member]
            self.pending = False
            body = (
                'Source = try #table({"Customer\'s ""Amount"""}, {}) '
                'otherwise #table({"Customer\'s ""Amount"""}, {})'
            )
            project = _project(root, {"Orders": body}, identities={"Orders": self.identity})
            path = os.path.join(project, f"{NAME}.SemanticModel", "definition",
                                "tables", "Orders.tmdl")
            with open(path, "a", encoding="utf-8") as stream:
                stream.write("\n\tcolumn 'Display Caption'\n\t\tdataType: double\n"
                             "\t\tsourceColumn: Customer's \"Amount\"\n")
            self.legacy_csv = os.path.join(data, "orders_extract.csv")
            self.legacy_bytes = b'"Customer\'s ""Amount"""\n1234.5\n'
            _write(self.legacy_csv, self.legacy_bytes)
            _write(os.path.join(root, "datasources.json"), json.dumps([{"tables": [{
                "name": "Orders", "hyper_identity": self.identity,
                "cols_physical_map": {self.physical_columns[0]: self.physical_columns[0]},
                "columns": [{"name": self.physical_columns[0]}],
            }]}]))
            with mock.patch.object(migrate._extract_dir_local, "path", root, create=True):
                self._convert(data, project)
            text = _read(project, "Orders")
            self.assertIn('[PromoteAllScalars=true]), '
                          '{{"Customer\'s ""Amount""", type number}}, "en-US")', text)
            self.assertNotIn("#table(", text)
            with open(os.path.join(data, self.bound_filename),
                      encoding="utf-8", newline="") as stream:
                self.assertEqual(list(csv.reader(stream)), [['Customer\'s "Amount"'], ["1234.5"]])

    def test_untyped_schema_still_promotes_headers_without_guessing_types(self):
        with tempfile.TemporaryDirectory() as root:
            data = os.path.join(root, NAME, "Data")
            self.physical_columns = ["Unknown"]
            self.source_rows = [["1234.5"]]
            self.identity, member = _sqlite_hyper_fixture(
                root, data, "orders_extract.hyper",
                self.physical_columns, self.source_rows,
            )
            self.packaged_hyper = [member]
            self.pending = False
            project = _project(root, {
                "Orders": 'Source = try #table({"Unknown"}, {}) '
                          'otherwise #table({"Unknown"}, {})',
            }, identities={"Orders": self.identity})
            self.legacy_csv = os.path.join(data, "orders_extract.csv")
            self.legacy_bytes = b"Unknown\n1234.5\n"
            _write(self.legacy_csv, self.legacy_bytes)
            _write(os.path.join(root, "datasources.json"), json.dumps([{"tables": [{
                "name": "Orders", "hyper_identity": self.identity,
                "cols_physical_map": {"Unknown": "Unknown"},
                "columns": [{"name": "Unknown"}],
            }]}]))
            with mock.patch.object(migrate._extract_dir_local, "path", root, create=True):
                self._convert(data, project)
            text = _read(project, "Orders")
            self.assertIn("Source = Table.PromoteHeaders(Csv.Document(", text)
            self.assertNotIn("Table.TransformColumnTypes", text)
            self.assertNotIn("#table(", text)


class TestPendingHyperCsvExtractionTypes(unittest.TestCase):
    """Authoritative pending sources must type raw, not renamed, CSV headers."""

    maxDiff = None
    table = "RenamedSales"
    filename = "hyper_" + "a" * 64 + ".csv"

    def _fixture(self, root, headers, rows, physical_map, columns,
                 model_columns, downstream):
        identity = {
            "workbook": os.path.join(root, "synthetic.twbx"),
            "archive_member": "Data/physical.hyper",
            "schema": "Extract",
            "table": "PhysicalSales",
        }
        source = (
            "\t\t\t\tlet\n\t\t\t\t\tSource = "
            + _pending_hyper_error(self.table, identity) + ",\n"
        )
        project = _project(
            root, {self.table: source + downstream},
            identities={self.table: identity},
        )
        tables = os.path.join(
            project, f"{NAME}.SemanticModel", "definition", "tables",
        )
        original_path = os.path.join(tables, self.table + ".tmdl")
        with open(original_path, encoding="utf-8") as stream:
            original = stream.read()

        def quote(value):
            return "'" + value.replace("'", "''") + "'"

        declarations = "".join(
            f"\tcolumn {quote(caption)}\n\t\tdataType: {datatype}\n"
            f"\t\tsourceColumn: {_serialize_source_column(source_column)}\n\n"
            for caption, source_column, datatype in model_columns
        )
        declaration = f"table {self.table}\n"
        self.assertIn(declaration, original)
        original = original.replace(declaration, declaration + declarations, 1)
        _write(original_path, original)
        # Neither the final table name nor this filename matches the extractor
        # table name; the placeholder's physical identity is authoritative.
        table_path = os.path.join(tables, "unrelated-artifact.tmdl")
        os.replace(original_path, table_path)

        data = os.path.join(project, "Data")
        csv_buffer = io.StringIO(newline="")
        writer = csv.writer(csv_buffer)
        writer.writerow(headers)
        writer.writerows(rows)
        raw = csv_buffer.getvalue().encode("utf-8")
        raw_path = _write(os.path.join(data, self.filename), raw)
        proof = _full_export_record(
            raw_path, identity, columns=headers, row_count=len(rows),
        )
        manifest_path = _write(
            os.path.join(data, "hyper_exports.json"),
            json.dumps({"version": 1, "authoritative": True, "exports": [proof]}),
        )
        # A foreign table deliberately has the final name and matching headers
        # but conflicting types. It must not supply metadata for this identity.
        foreign = {
            "name": self.table,
            "hyper_identity": dict(identity, table="ForeignPhysical"),
            "cols_physical_map": dict(physical_map),
            "columns": [dict(column, datatype="string") for column in columns],
        }
        extraction = {
            "name": "OriginalTableauSales",
            "source_table": "PhysicalSales",
            "hyper_identity": identity,
            "cols_physical_map": physical_map,
            "columns": columns,
        }
        extract = os.path.join(root, "extract")
        _write(
            os.path.join(extract, "datasources.json"),
            json.dumps([{"tables": [foreign, extraction]}]),
        )
        return {
            "project": project, "data": data, "extract": extract,
            "table_path": table_path, "original": original,
            "identity": identity, "headers": headers, "rows": rows,
            "raw_path": raw_path, "raw": raw, "proof": proof,
            "manifest_path": manifest_path, "extraction": extraction,
        }

    def _orders_fixture(self, root, *, partial=False):
        headers = ["raw_qty", "raw_amount", "raw_date", "raw_updated",
                   "raw_flag", "Label"]
        rows = [
            ["2", "1234.5", "2026-10-15", "2026-10-15 09:30:00", "true", "open"],
            ["7", "0.25", "2026-10-22", "2026-10-22 13:15:05", "false", "closed"],
        ]
        physical_map = {
            "raw_qty": "Quantity", "raw_amount": "Amount",
            "raw_date": "Order Date", "raw_updated": "Updated",
            "raw_flag": "Logical Flag",
        }
        columns = [
            {"name": "Quantity", "remote_name": "raw_qty", "datatype": "integer"},
            {"name": "Amount", "remote_name": "raw_amount", "datatype": "real"},
            {"name": "Order Date", "remote_name": "raw_date", "datatype": "date"},
            {"name": "Updated", "remote_name": "raw_updated", "datatype": "datetime"},
            {"name": "Logical Flag", "remote_name": "raw_flag", "datatype": "boolean"},
            {"name": "Label", "remote_name": "Label", "datatype": "string"},
            {"name": "Derived Year", "datatype": "integer"},
            {"name": "Derived Date", "datatype": "date"},
            {"name": "Derived Value", "datatype": "real"},
        ]
        if partial:
            for column in columns:
                if column["name"] in {"Quantity", "Amount", "Updated", "Logical Flag"}:
                    column.pop("datatype")
                    # The physical-to-logical map, not remote_name or the
                    # display caption, is the remaining binding evidence.
                    column.pop("remote_name")
        model_columns = [
            ("Quantity Caption", "Quantity", "int64"),
            ("Amount Caption", "Amount", "double"),
            # Tableau date must win over the model's dateTime fallback.
            ("Date Caption", "Order Date", "dateTime"),
            ("Updated Caption", "Updated", "dateTime"),
            ("Flag Caption", "Logical Flag", "boolean"),
            ("Visible Label", "Visible Label", "string"),
            ("Derived Year", "Derived Year", "int64"),
            ("Derived Date", "Derived Date", "dateTime"),
            ("Derived Value", "Derived Value", "double"),
        ]
        downstream = (
            '\t\t\t\t\t#"Renamed Hyper Columns" = Table.RenameColumns(Source, '
            '{{"raw_qty", "Quantity"}, {"raw_amount", "Amount"}, '
            '{"raw_date", "Order Date"}, {"raw_updated", "Updated"}, '
            '{"raw_flag", "Logical Flag"}}),\n'
            '\t\t\t\t\t#"Renamed Columns" = Table.RenameColumns(#"Renamed Hyper Columns", '
            '{{"Label", "Visible Label"}}),\n'
            '\t\t\t\t\t#"Added Year" = Table.AddColumn(#"Renamed Columns", '
            '"Derived Year", each Date.Year([Order Date]), Int64.Type),\n'
            '\t\t\t\t\t#"Added Date" = Table.AddColumn(#"Added Year", '
            '"Derived Date", each Date.From([Updated]), type date),\n'
            '\t\t\t\t\t#"Added Value" = Table.AddColumn(#"Added Date", '
            '"Derived Value", each if [Logical Flag] then [Amount] / [Quantity] '
            'else 0, type number),\n'
            '\t\t\t\t\tResult = #"Added Value"\n'
            '\t\t\t\tin\n\t\t\t\t\tResult'
        )
        return self._fixture(
            root, headers, rows, physical_map, columns, model_columns, downstream,
        )

    def _source_line(self, content):
        lines = [line for line in content.splitlines()
                 if line.startswith("\t\t\t\t\tSource = ")]
        self.assertEqual(len(lines), 1)
        return lines[0]

    def _bound_twice(self, fixture):
        from powerbi_import.m_validator import validate_m_query

        with mock.patch.object(
                migrate, "_hyper_to_csv_files",
                side_effect=AssertionError("verified manifest must not invoke a Hyper reader")):
            migrate._convert_hyper_to_csv_in_data(
                fixture["data"], NAME, fixture["project"],
                extract_dir=fixture["extract"], quiet=True,
            )
            with open(fixture["table_path"], encoding="utf-8") as stream:
                bound = stream.read()
            self.assertNotIn("Migration.HyperExportRequired", bound)
            source_line = self._source_line(bound)
            original_line = self._source_line(fixture["original"])
            self.assertEqual(
                bound.replace(source_line, "<SOURCE>", 1),
                fixture["original"].replace(original_line, "<SOURCE>", 1),
                "Only the pending Source may change; preserve all later M steps",
            )
            expression = bound.split("\n\t\tsource =\n", 1)[1].split(
                "\n\tannotation", 1,
            )[0].strip()
            self.assertEqual(validate_m_query(expression), [], expression)

            with open(fixture["raw_path"], "rb") as stream:
                self.assertEqual(stream.read(), fixture["raw"])
            self.assertEqual(
                _full_export_record(
                    fixture["raw_path"], fixture["identity"],
                    columns=fixture["headers"], row_count=len(fixture["rows"]),
                ),
                fixture["proof"],
            )
            with open(fixture["manifest_path"], "rb") as stream:
                manifest_bytes = stream.read()
            self.assertEqual(json.loads(manifest_bytes), {
                "version": 1, "authoritative": True,
                "exports": [dict(fixture["proof"], staged_files=[{
                    "table": self.table, "csv_filename": self.filename,
                }])],
            })
            with open(fixture["table_path"], "rb") as stream:
                table_bytes = stream.read()
            migrate._convert_hyper_to_csv_in_data(
                fixture["data"], NAME, fixture["project"],
                extract_dir=fixture["extract"], quiet=True,
            )
            for path, expected in (
                (fixture["raw_path"], fixture["raw"]),
                (fixture["manifest_path"], manifest_bytes),
                (fixture["table_path"], table_bytes),
            ):
                with open(path, "rb") as stream:
                    self.assertEqual(stream.read(), expected)
        return bound

    def _assert_source_types(self, bound, entries):
        expected = (
            '\t\t\t\t\tSource = Table.TransformColumnTypes('
            'Table.PromoteHeaders(Csv.Document(File.Contents(DataFolder & "\\'
            + self.filename + '"), [Delimiter=",", Encoding=65001, '
            'QuoteStyle=QuoteStyle.Csv]), [PromoteAllScalars=true]), {'
            + ", ".join(entries) + '}, "en-US"),'
        )
        self.assertEqual(
            self._source_line(bound), expected,
            "Type only the exact physical CSV headers before any M rename or calculation",
        )

    def test_pending_renamed_table_uses_identity_metadata_before_preserved_m_steps(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = self._orders_fixture(root)
            bound = self._bound_twice(fixture)
            for step in (
                '#"Renamed Hyper Columns" = Table.RenameColumns(Source,',
                '#"Renamed Columns" = Table.RenameColumns(#"Renamed Hyper Columns",',
                "Date.Year([Order Date])",
                "Date.From([Updated])",
                "if [Logical Flag] then [Amount] / [Quantity] else 0",
            ):
                self.assertIn(step, bound)
            for derived in ("Derived Year", "Derived Date", "Derived Value"):
                self.assertNotIn(derived, self._source_line(bound))
            self._assert_source_types(bound, [
                '{"raw_qty", Int64.Type}', '{"raw_amount", type number}',
                '{"raw_date", type date}', '{"raw_updated", type datetime}',
                '{"raw_flag", type logical}', '{"Label", type text}',
            ])

    def test_pending_partial_types_fall_back_via_physical_to_logical_source_column(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = self._orders_fixture(root, partial=True)
            self.assertEqual(
                {column["name"] for column in fixture["extraction"]["columns"]
                 if "datatype" not in column},
                {"Quantity", "Amount", "Updated", "Logical Flag"},
            )
            self.assertEqual(
                {column["name"] for column in fixture["extraction"]["columns"]
                 if column["name"] in {"Quantity", "Amount", "Updated", "Logical Flag"}
                 and "remote_name" not in column},
                {"Quantity", "Amount", "Updated", "Logical Flag"},
            )
            bound = self._bound_twice(fixture)
            self._assert_source_types(bound, [
                '{"raw_qty", Int64.Type}', '{"raw_amount", type number}',
                '{"raw_date", type date}', '{"raw_updated", type datetime}',
                '{"raw_flag", type logical}', '{"Label", type text}',
            ])

    def test_pending_partial_alias_collision_uses_mapped_source_column_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            headers = ["raw_date", "Alias"]
            rows = [["2026-10-15", "3.25"]]
            physical_map = {"raw_date": "Alias", "Alias": "Other"}
            columns = [
                {"name": "Alias", "datatype": "date"},
                {"name": "Other"},
            ]
            model_columns = [
                ("Event Caption", "Alias", "dateTime"),
                ("Value Caption", "Other", "double"),
            ]
            downstream = (
                '\t\t\t\t\t#"Renamed Hyper Columns" = Table.RenameColumns(Source, '
                '{{"Alias", "Other"}, {"raw_date", "Alias"}}),\n'
                '\t\t\t\t\t#"Added Value" = Table.AddColumn(#"Renamed Hyper Columns", '
                '"Year Plus Value", each Date.Year([Alias]) + [Other], type number),\n'
                '\t\t\t\t\tResult = #"Added Value"\n'
                '\t\t\t\tin\n\t\t\t\t\tResult'
            )
            fixture = self._fixture(
                root, headers, rows, physical_map, columns, model_columns, downstream,
            )
            bound = self._bound_twice(fixture)
            self.assertIn("Date.Year([Alias]) + [Other]", bound)
            self._assert_source_types(bound, [
                '{"raw_date", type date}', '{"Alias", type number}',
            ])

    def test_pending_exact_aliases_and_remote_name_are_one_hop_without_guessing(self):
        with tempfile.TemporaryDirectory() as root:
            quoted = 'Customer\'s "Amount"'
            headers = [
                "raw_date", "Alias", "case_raw", "Case_raw", quoted,
                "remote_only", "actual_value", "case_sensitive", "Caption Trap",
            ]
            rows = [[
                "2026-10-15", "3.25", "true", "19", "1.5",
                "false", "42.5", "7", "unused",
            ]]
            physical_map = {
                "raw_date": "Alias", "Alias": "Other",
                "case_raw": "Lower Logical", "Case_raw": "Upper Logical",
                quoted: "Quoted Logical", "actual_value": "Actual Logical",
            }
            columns = [
                {"name": "Alias", "datatype": "date"},
                {"name": "Other", "datatype": "real"},
                {"name": "Lower Logical", "datatype": "boolean"},
                {"name": "Upper Logical", "datatype": "integer"},
                {"name": "Quoted Logical", "datatype": "real"},
                {"name": "Remote Logical", "remote_name": "remote_only",
                 "datatype": "boolean"},
                {"name": "Actual Logical", "datatype": "real"},
                {"name": "Case Missing", "remote_name": "CASE_SENSITIVE",
                 "datatype": "integer"},
            ]
            model_columns = [
                ("Alias Caption", "Alias", "dateTime"),
                ("Other Caption", "Other", "double"),
                ("Lower Caption", "Lower Logical", "boolean"),
                ("Upper Caption", "Upper Logical", "int64"),
                ("Quoted Caption", "Quoted Logical", "double"),
                ("Remote Caption", "Remote Logical", "boolean"),
                ("Caption Trap", "Actual Logical", "double"),
            ]
            downstream = (
                '\t\t\t\t\t#"Renamed Hyper Columns" = Table.RenameColumns(Source, '
                '{{"Alias", "Other"}, {"raw_date", "Alias"}, '
                '{"case_raw", "Lower Logical"}, {"Case_raw", "Upper Logical"}, '
                '{"Customer\'s ""Amount""", "Quoted Logical"}, '
                '{"remote_only", "Remote Logical"}, {"actual_value", "Actual Logical"}}),\n'
                '\t\t\t\t\tResult = #"Renamed Hyper Columns"\n'
                '\t\t\t\tin\n\t\t\t\t\tResult'
            )
            fixture = self._fixture(
                root, headers, rows, physical_map, columns, model_columns, downstream,
            )
            bound = self._bound_twice(fixture)
            self._assert_source_types(bound, [
                '{"raw_date", type date}', '{"Alias", type number}',
                '{"case_raw", type logical}', '{"Case_raw", Int64.Type}',
                '{"Customer\'s ""Amount""", type number}',
                '{"remote_only", type logical}', '{"actual_value", type number}',
            ])

    def test_foreign_or_duplicate_full_exports_never_bind_even_with_matching_metadata(self):
        for variant in ("foreign", "duplicate"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as root:
                fixture = self._orders_fixture(root)
                if variant == "foreign":
                    records = [dict(
                        fixture["proof"],
                        identity=dict(fixture["identity"], schema="ForeignSchema"),
                    )]
                else:
                    second = _write(
                        os.path.join(fixture["data"], "hyper_" + "b" * 64 + ".csv"),
                        fixture["raw"],
                    )
                    records = [
                        fixture["proof"],
                        _full_export_record(
                            second, fixture["identity"],
                            columns=fixture["headers"], row_count=len(fixture["rows"]),
                        ),
                    ]
                manifest = {"version": 1, "authoritative": True, "exports": records}
                _write(fixture["manifest_path"], json.dumps(manifest))
                with mock.patch.object(
                        migrate, "_hyper_to_csv_files",
                        side_effect=AssertionError("must not invoke a Hyper reader")):
                    for _ in range(2):
                        migrate._convert_hyper_to_csv_in_data(
                            fixture["data"], NAME, fixture["project"],
                            extract_dir=fixture["extract"], quiet=True,
                        )
                        with open(fixture["table_path"], encoding="utf-8") as stream:
                            self.assertEqual(stream.read(), fixture["original"])
                        with open(fixture["manifest_path"], encoding="utf-8") as stream:
                            self.assertEqual(json.load(stream), manifest)
                        self.assertTrue(all("staged_files" not in record for record in records))


def _geo_partition(filename, columns):
    from tableau_export.m_query_builder import (
        _gen_m_geojson,
        wrap_source_with_try_otherwise,
    )
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
        for output_format in ("pbip", "fabric"):
            with self.subTest(output_format=output_format):
                order = []
                names = ('_fix_twb_data_folder', '_collect_local_data_files',
                         '_convert_hyper_to_csv_in_data', '_route_spatial_partitions',
                         '_reconcile_csv_partitions', '_wire_shapefile_map_coordinates',
                         '_restrict_file_partitions_to_model', '_shorten_report_artifact_path')

                def record_step(step):
                    order.append(step)
                    if step == '_collect_local_data_files':
                        return {
                            'referenced': 0, 'already_present': 0, 'copied': 0,
                            'ambiguous': 0, 'not_found': 0,
                        }

                hooks = {
                    name: mock.Mock(side_effect=lambda *args, step=name, **kwargs: record_step(step))
                    for name in names
                }
                with mock.patch.multiple(migrate, **hooks):
                    migrate._extract_twbx_data_files(
                        SimpleNamespace(tableau_file='sample.twb', output_dir='out',
                                        output_format=output_format), NAME)
                expected = names[:-1] if output_format == "fabric" else (
                    '_collect_local_data_files', '_convert_hyper_to_csv_in_data',
                    '_shorten_report_artifact_path',
                )
                self.assertEqual(order, list(expected))
                if output_format == "pbip":
                    for name in (
                        '_fix_twb_data_folder', '_route_spatial_partitions',
                        '_reconcile_csv_partitions', '_wire_shapefile_map_coordinates',
                        '_restrict_file_partitions_to_model',
                    ):
                        hooks[name].assert_not_called()
                else:
                    hooks["_shorten_report_artifact_path"].assert_not_called()
                hooks["_collect_local_data_files"].assert_called_once_with(
                    "sample.twb", os.path.join("out", NAME), NAME,
                    hyper_only=output_format == "pbip",
                )
                hooks["_convert_hyper_to_csv_in_data"].assert_called_once_with(
                    os.path.join("out", NAME, "Data"), NAME, os.path.join("out", NAME),
                    hyper_export_format="auto", hyper_export_max_rows=None,
                    output_format=output_format, quiet=False, max_zip_entry_mb=1024,
                )
                if output_format == "fabric":
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
