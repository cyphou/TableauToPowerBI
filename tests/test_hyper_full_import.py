"""Synthetic regressions for complete Hyper export and PBIP Import evidence.

These fixtures never contain customer data and do not require Tableau Desktop.
The SDK-dependent cases create their own native Hyper files and skip only when
the optional ``tableauhyperapi`` package is unavailable.
"""

import base64
import csv
import hashlib
import io
import json
import ntpath
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
import warnings
import zipfile
from contextlib import ExitStack, contextmanager, redirect_stdout
from copy import deepcopy
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tableau_export"))

import migrate
from powerbi_import.errors import MigrationError
from tests.hyper_test_helpers import StreamingHyperApi
from tests.link_test_helpers import (
    forbid_link_traversal,
    make_directory_link,
    make_file_link,
    make_junction,
)

REPORT_NAME = "Synthetic"
FULL_EXPORT_THRESHOLD = 10 * 1024 * 1024
HYPER_MEMBER = "Data/synthetic.hyper"
COPY_PATH_PATTERN = re.compile(r"\bTO\s+'((?:[^']|'')*)'", re.IGNORECASE)


def _temporary_directory(prefix):
    """Keep deterministic fixtures under the ASCII-only repository path."""
    return tempfile.TemporaryDirectory(prefix=prefix, dir=os.getcwd())


@contextmanager
def _local_hyper_environment():
    """Keep ambient cap/upload settings from changing local Hyper fixtures."""
    keys = ("TTPBI_HYPER_EXPORT_MAX_ROWS", "TTPBI_UPLOAD_ROOT")
    previous = {}
    for key in keys:
        value = os.environ.pop(key, None)
        if value is not None:
            previous[key] = value
    previous_write_root = migrate._upload_write_root
    migrate._upload_write_root = None
    try:
        yield
    finally:
        migrate._upload_write_root = previous_write_root
        os.environ.update(previous)


def _write(path, payload):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    mode = "wb" if isinstance(payload, bytes) else "w"
    options = {} if mode == "wb" else {"encoding": "utf-8", "newline": ""}
    with open(path, mode, **options) as stream:
        stream.write(payload)
    return path


# UNC and device-namespace spellings, excluding the local ``\\?\C:\`` form.
_REMOTE_LOOKING = re.compile(r"\\\\(?![?.]\\[A-Za-z]:)|\\\?\?\\")


@contextmanager
def _guard_remote_resolution():
    """Resolve local paths normally but refuse and record UNC/device paths."""
    attempts = []

    def guard(real):
        def guarded(path, *args, **kwargs):
            text = os.fsdecode(path)
            if _REMOTE_LOOKING.match(text.replace("/", "\\")):
                attempts.append(text)
                raise OSError("synthetic guard: remote resolution is forbidden")
            return real(path, *args, **kwargs)
        return guarded

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(
            os.path, "realpath", guard(os.path.realpath)))
        final_path = getattr(ntpath, "_getfinalpathname", None)
        if final_path is not None:
            stack.enter_context(mock.patch.object(
                ntpath, "_getfinalpathname", guard(final_path)))
        yield attempts


def _package_identity(workbook, member=HYPER_MEMBER):
    return {
        "workbook": os.path.abspath(workbook),
        "archive_member": member,
    }


def _complete_identity(package_identity, schema, table):
    return dict(package_identity, schema=schema, table=table)


def _identity_filename(identity, extension):
    digest = hashlib.sha256(json.dumps(
        identity, sort_keys=True, ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return f"hyper_{digest}.{extension}"


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_destination(sql):
    match = COPY_PATH_PATTERN.search(sql)
    if not match:
        raise AssertionError(f"Hyper COPY did not contain a quoted target: {sql}")
    return match.group(1).replace("''", "'")


def _parquet_marker_api(api, *, count=2, payload=b"PAR1synthetic COPY markerPAR1",
                        on_copy=None):
    paths = []

    def execute(sql):
        destination = _copy_destination(sql)
        paths.append(destination)
        if on_copy is not None:
            return on_copy(destination, sql)
        _write(destination, payload)
        return count

    api.connection.execute_command.side_effect = execute
    return paths


def _main_federated_hyper_datasource(member):
    """A named Hyper connection on the main federated connection, no <extract>."""
    import xml.etree.ElementTree as ET

    datasource = ET.Element("datasource", {
        "name": "synthetic-main-source",
        "caption": "Synthetic Main Source Caption",
    })
    federated = ET.SubElement(datasource, "connection", {"class": "federated"})
    named = ET.SubElement(federated, "named-connection", {
        "name": "synthetic-hyper-ref",
        "caption": "Physical Connection Caption",
    })
    ET.SubElement(named, "connection", {"class": "hyper", "dbname": member})
    relation = ET.SubElement(federated, "relation", {
        "connection": "synthetic-hyper-ref",
        "name": "Logical Events",
        "caption": "Event Caption",
        "table": "[Extract].[Physical Events]",
        "type": "table",
    })
    columns = ET.SubElement(relation, "columns")
    ET.SubElement(columns, "column", {
        "name": "id", "datatype": "integer", "ordinal": "0",
    })
    ET.SubElement(columns, "column", {
        "name": "label", "datatype": "string", "ordinal": "1",
    })
    connection_columns = ET.SubElement(federated, "cols")
    ET.SubElement(connection_columns, "map", {
        "key": "[id]", "value": "[Logical Events].[id]",
    })
    ET.SubElement(connection_columns, "map", {
        "key": "[label]", "value": "[Logical Events].[label]",
    })
    ET.SubElement(datasource, "column", {
        "name": "[id]", "caption": "Display Event ID",
        "datatype": "integer", "role": "dimension",
    })
    ET.SubElement(datasource, "column", {
        "name": "[label]", "caption": "Display Event Label",
        "datatype": "string", "role": "dimension",
    })
    return datasource


def _alias_hyper_datasource(member, *, extract_aliases=None):
    """A Hyper join tree whose one physical table backs two explicit aliases."""
    import xml.etree.ElementTree as ET

    aliases = (
        ("Current User", "Current User Caption", "Current User ID"),
        ("Manager User", "Manager User Caption", "Manager User ID"),
    )
    extract_aliases = list(extract_aliases or [alias[0] for alias in aliases])
    datasource = ET.Element("datasource", {
        "name": "synthetic-alias-source",
        "caption": "Synthetic Alias Source Caption",
    })
    federated = ET.SubElement(datasource, "connection", {"class": "federated"})
    named = ET.SubElement(federated, "named-connection", {
        "name": "synthetic-native-ref",
        "caption": "Native Connection Caption",
    })
    ET.SubElement(named, "connection", {
        "class": "textscan", "directory": "Data", "filename": "native.csv",
        "separator": ",",
    })

    def table_relation(parent, name, caption, table, *, connection=None,
                       column_name="id", column_type="integer"):
        attributes = {
            "name": name, "caption": caption, "table": table, "type": "table",
        }
        if connection is not None:
            attributes["connection"] = connection
        relation = ET.SubElement(parent, "relation", attributes)
        relation_columns = ET.SubElement(relation, "columns")
        ET.SubElement(relation_columns, "column", {
            "name": column_name, "datatype": column_type, "ordinal": "0",
        })
        return relation

    native_join = ET.SubElement(federated, "relation", {
        "name": "Native User Join", "type": "join", "join": "inner",
    })
    for name, caption, local_column in aliases:
        table_relation(
            native_join, name, caption, "[User$]",
            connection="synthetic-native-ref", column_name=local_column,
        )

    extract = ET.SubElement(datasource, "extract", {"enabled": "true"})
    hyper_connection = ET.SubElement(extract, "connection", {
        "class": "hyper", "dbname": member,
        "schema": "Extract", "tablename": "Physical User",
    })
    metadata_records = ET.SubElement(hyper_connection, "metadata-records")
    for name, unused_caption, local_column in aliases:
        record = ET.SubElement(metadata_records, "metadata-record", {
            "class": "column",
        })
        ET.SubElement(record, "parent-name").text = f"[{name}]"
        ET.SubElement(record, "remote-name").text = "id"
        ET.SubElement(record, "local-name").text = f"[{local_column}]"
        ET.SubElement(record, "local-type").text = "integer"
        ET.SubElement(record, "ordinal").text = "0"
    extract_join = ET.SubElement(hyper_connection, "relation", {
        "name": "Extract User Join", "type": "join", "join": "inner",
    })
    alias_lookup = {name: (caption, local) for name, caption, local in aliases}
    for name in extract_aliases:
        caption, unused_local = alias_lookup.get(
            name, (f"Unmatched {name} Caption", f"{name} ID"))
        table_relation(
            extract_join, name, caption, "[Extract].[Physical User]",
            column_name="id",
        )

    # The paired object contexts are relational joins. This deliberately
    # exercises the old early relational guard without using private metadata.
    graph = ET.SubElement(datasource, "object-graph")
    objects = ET.SubElement(graph, "objects")
    obj = ET.SubElement(objects, "object", {"id": "synthetic-alias-object"})
    ET.SubElement(obj, "properties", {"context": ""}).append(deepcopy(native_join))
    ET.SubElement(obj, "properties", {"context": "extract"}).append(
        deepcopy(extract_join))

    connection_columns = ET.SubElement(federated, "cols")
    for name, caption, local_column in aliases:
        ET.SubElement(connection_columns, "map", {
            "key": f"[{local_column}]",
            "value": f"[{name}].[id]",
        })
        ET.SubElement(datasource, "column", {
            "name": f"[{local_column}]",
            "caption": caption,
            "datatype": "integer",
            "role": "dimension",
        })
    return datasource


def _write_synthetic_package(root, member=HYPER_MEMBER):
    workbook = os.path.abspath(os.path.join(root, "Synthetic.twbx"))
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.writestr("Synthetic.twb", "<workbook/>")
        archive.writestr(member, b"synthetic Hyper package member")
    staged_hyper = os.path.join(root, "staged-synthetic.hyper")
    _write(staged_hyper, b"synthetic Hyper catalog placeholder")
    return workbook, staged_hyper, _package_identity(workbook, member)


def _annotation_identities(content):
    prefix = "\tannotation Migration_HyperIdentity = "
    return [
        (line, json.loads(line[len(prefix):]))
        for line in content.splitlines()
        if line.startswith(prefix)
    ]


def _all_table_files(project):
    tables_dir = os.path.join(
        project, f"{REPORT_NAME}.SemanticModel", "definition", "tables",
    )
    if not os.path.isdir(tables_dir):
        return []
    return [
        os.path.join(tables_dir, filename)
        for filename in sorted(os.listdir(tables_dir))
        if filename.endswith(".tmdl")
    ]


def _build_bound_fixture(root, *, shape="main", export_format="csv",
                         rows=((1, "first"), (2, "second")), total_rows=2):
    """Run synthetic XML extraction, TMDL generation, and PBIP file binding."""
    from powerbi_import.tmdl_generator import generate_tmdl
    from tableau_export.datasource_extractor import extract_datasource

    workbook, staged_hyper, package_identity = _write_synthetic_package(root)
    if shape == "main":
        xml_datasource = _main_federated_hyper_datasource(HYPER_MEMBER)
        schema, physical_table = "Extract", "Physical Events"
    elif shape == "aliases":
        xml_datasource = _alias_hyper_datasource(HYPER_MEMBER)
        schema, physical_table = "Extract", "Physical User"
    else:
        raise ValueError(f"Unknown synthetic Hyper shape: {shape}")
    datasource = extract_datasource(xml_datasource, workbook)

    project = os.path.join(root, REPORT_NAME)
    data_dir = os.path.join(project, "Data")
    os.makedirs(data_dir, exist_ok=True)
    extract_dir = os.path.join(root, "extracted")
    _write(
        os.path.join(extract_dir, "datasources.json"),
        json.dumps([datasource], ensure_ascii=False),
    )
    with redirect_stdout(io.StringIO()):
        generate_tmdl(
            [datasource], REPORT_NAME, None,
            os.path.join(project, f"{REPORT_NAME}.SemanticModel"),
            model_mode="import", parameterize=True,
        )

    api_columns = ("id", "label") if shape == "main" else ("id",)
    api_rows = list(rows) if shape == "main" else [
        (row[0],) for row in rows
    ]
    api = StreamingHyperApi(
        columns=api_columns,
        rows=api_rows,
        total_rows=total_rows,
        table_pairs=[(schema, physical_table)],
    )
    copy_paths = []
    if export_format == "parquet":
        copy_paths = _parquet_marker_api(api)
    with _local_hyper_environment(), \
            mock.patch.dict("sys.modules", {"tableauhyperapi": api.module}), \
            mock.patch(
                "sqlite3.connect",
                side_effect=AssertionError("native Hyper must not use SQLite"),
            ), \
            mock.patch(
                "tableau_export.hyper_reader.read_hyper",
                side_effect=AssertionError("full PBIP export must not use samples"),
            ), \
            redirect_stdout(io.StringIO()):
        migrate._convert_hyper_to_csv_in_data(
            data_dir, REPORT_NAME, project,
            packaged_hyper=[{
                "path": staged_hyper,
                "identity": package_identity,
            }],
            hyper_export_format=export_format,
            output_format="pbip", quiet=True, extract_dir=extract_dir,
        )
        migrate._set_data_folder_for_hyper_exports(project, REPORT_NAME)

    expressions_path = os.path.join(
        project, f"{REPORT_NAME}.SemanticModel", "definition", "expressions.tmdl",
    )
    with open(expressions_path, encoding="utf-8") as stream:
        expressions = stream.read()
    folders = re.findall(
        r'(?m)^expression DataFolder\s*=\s*"((?:[^"]|"")*)"', expressions,
    )
    if len(folders) != 1:
        raise AssertionError(
            f"Bound fixture needs exactly one literal DataFolder: {expressions}"
        )
    folder = folders[0].replace('""', '"').replace("\\\\", "\\")
    if (not os.path.isabs(folder) or not os.path.isdir(folder)
            or os.path.normcase(os.path.realpath(folder))
            != os.path.normcase(os.path.realpath(data_dir))):
        raise AssertionError(
            f"Bound fixture DataFolder {folder!r} does not resolve to {data_dir!r}"
        )

    manifest_path = os.path.join(data_dir, "hyper_exports.json")
    with open(manifest_path, encoding="utf-8") as stream:
        manifest = json.load(stream)
    extracted = {
        "datasources": [datasource],
        "hyper_files": [{
            "name": HYPER_MEMBER,
            "size_bytes": os.path.getsize(staged_hyper),
        }],
    }
    return {
        "root": root,
        "workbook": workbook,
        "staged_hyper": staged_hyper,
        "package_identity": package_identity,
        "datasource": datasource,
        "extracted": extracted,
        "project": project,
        "data_dir": data_dir,
        "extract_dir": extract_dir,
        "manifest_path": manifest_path,
        "manifest": manifest,
        "api": api,
        "copy_paths": copy_paths,
        "format": export_format,
    }


def _record_export_path(fixture, record=None):
    record = record or fixture["manifest"]["exports"][0]
    return os.path.join(
        fixture["data_dir"], record.get("filename") or record["csv_filename"],
    )


def _save_manifest(fixture, manifest):
    _write(fixture["manifest_path"], json.dumps(manifest, ensure_ascii=False))


def _refresh_file_integrity(record, path):
    record["sha256"] = _file_sha256(path)
    record["size_bytes"] = os.path.getsize(path)


def _tmdl_files_with_hyper_annotation(project):
    result = []
    for path in _all_table_files(project):
        with open(path, encoding="utf-8") as stream:
            content = stream.read()
        if "annotation Migration_HyperIdentity = " in content:
            result.append((path, content))
    return result


class TestHyperFullExportContracts(unittest.TestCase):
    """Full exports carry verified output integrity and reject incomplete data."""

    def setUp(self):
        self.temp = _temporary_directory("hyper-full-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.hyper = os.path.join(self.root, "synthetic.hyper")
        _write(self.hyper, b"synthetic Hyper input")
        self.package = _package_identity(
            os.path.join(self.root, "synthetic.twbx"),
        )

    def _export(self, api, **kwargs):
        with _local_hyper_environment(), \
                mock.patch.dict("sys.modules", {"tableauhyperapi": api.module}), \
                mock.patch(
                    "sqlite3.connect",
                    side_effect=AssertionError("unexpected SQLite fallback"),
                ), \
                mock.patch(
                    "tableau_export.hyper_reader.read_hyper",
                    side_effect=AssertionError("unexpected metadata sample"),
                ), \
                redirect_stdout(io.StringIO()):
            return migrate._hyper_to_csv_files(
                self.hyper, self.root, REPORT_NAME, csv,
                identity=self.package, quiet=True, **kwargs,
            )

    def test_full_csv_record_hashes_and_sizes_the_published_file(self):
        api = StreamingHyperApi(
            columns=("id", "label"),
            rows=[(1, "synthetic row"), (2, "second row")],
            total_rows=2,
        )
        result = self._export(api, export_format="csv", require_full=True)
        self.assertEqual(len(result.exports), 1)
        record = result.exports[0]
        path = os.path.join(self.root, record["filename"])
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(record["format"], "csv")
        self.assertEqual(record["row_count"], 2)
        self.assertEqual(record["total_rows"], 2)
        self.assertIs(record["sampled"], False)
        self.assertIs(record["truncated"], False)
        self.assertIs(record["full_extract_verified"], True)
        self.assertEqual(record["row_count_source"], "csv_writer")
        self.assertEqual(record["sha256"], _file_sha256(path))
        self.assertEqual(record["size_bytes"], os.path.getsize(path))
        with open(path, encoding="utf-8", newline="") as stream:
            self.assertEqual(list(csv.reader(stream)), [
                ["id", "label"], ["1", "synthetic row"], ["2", "second row"],
            ])

    def test_lower_level_cap_remains_available_as_explicit_sample_diagnostics(self):
        api = StreamingHyperApi(
            row_factory=lambda: ((i, f"row-{i}") for i in range(9)),
            total_rows=9,
        )
        result = self._export(
            api, export_format="csv", max_rows=2, require_full=False,
        )
        record = result.exports[0]
        self.assertEqual(record["row_count"], 2)
        self.assertEqual(record["total_rows"], 9)
        self.assertEqual(record["max_rows"], 2)
        self.assertIs(record["sampled"], True)
        self.assertIs(record["truncated"], True)
        self.assertIn("LIMIT", " ".join(api.queries).upper())

    def test_full_mode_rejects_cap_unknown_count_and_measured_count_mismatch(self):
        cases = (
            ("explicit cap", {
                "rows": [(1, "a"), (2, "b"), (3, "c")],
                "total_rows": 3, "max_rows": 1,
            }),
            ("unknown source count", {
                "rows": [(1, "a"), (2, "b")],
                "total_rows": 2, "count_error": RuntimeError("count unavailable"),
            }),
            ("measured count mismatch", {
                "rows": [(1, "a"), (2, "b")], "total_rows": 3,
            }),
        )
        for label, options in cases:
            with self.subTest(case=label), _local_hyper_environment(), \
                    _temporary_directory("hyper-full-reject-") as root:
                hyper = _write(os.path.join(root, "synthetic.hyper"), b"synthetic")
                package = _package_identity(os.path.join(root, "synthetic.twbx"))
                expected = _identity_filename(
                    _complete_identity(package, "Extract", "Facts"), "csv",
                )
                previous = b"previous published export"
                _write(os.path.join(root, expected), previous)
                manifest_path = _write(
                    os.path.join(root, "hyper_exports.json"),
                    b'{"version":1,"authoritative":true,"exports":[]}\n',
                )
                old_manifest = open(manifest_path, "rb").read()
                old_files = set(os.listdir(root))
                api = StreamingHyperApi(
                    columns=("id", "label"),
                    rows=options["rows"],
                    total_rows=options["total_rows"],
                    count_error=options.get("count_error"),
                )
                kwargs = {
                    key: value for key, value in options.items()
                    if key not in ("rows", "total_rows", "count_error")
                }
                with mock.patch.dict(
                        "sys.modules", {"tableauhyperapi": api.module}), \
                        mock.patch(
                            "sqlite3.connect",
                            side_effect=AssertionError("unexpected SQLite fallback"),
                        ), \
                        mock.patch(
                            "tableau_export.hyper_reader.read_hyper",
                            side_effect=AssertionError("unexpected sample fallback"),
                        ), \
                        redirect_stdout(io.StringIO()), \
                        self.assertRaises(MigrationError):
                    migrate._hyper_to_csv_files(
                        hyper, root, REPORT_NAME, csv, identity=package,
                        export_format="csv", require_full=True, quiet=True,
                        **kwargs,
                    )
                with open(os.path.join(root, expected), "rb") as stream:
                    self.assertEqual(stream.read(), previous)
                with open(manifest_path, "rb") as stream:
                    self.assertEqual(stream.read(), old_manifest)
                self.assertEqual(set(os.listdir(root)), old_files)


class TestHyperParquetUnicodeAndTransit(unittest.TestCase):
    """Native COPY uses unique ASCII staging, then publishes Unicode targets."""

    def _sdk(self):
        try:
            import tableauhyperapi as api
        except ImportError:
            self.skipTest("optional tableauhyperapi SDK is not installed")
        return api

    def _create_typed_hyper(self, api, root, path, *, large=False):
        schema = "Extract"
        table_name = api.TableName(schema, "Synthetic Facts")
        if large:
            types = [api.SqlType.big_int(), api.SqlType.text()]
            columns = ["row_id", "payload"]
        else:
            types = [
                api.SqlType.big_int(), api.SqlType.text(),
                api.SqlType.numeric(12, 3), api.SqlType.date(),
            ]
            columns = ["row_id", "label", "amount", "event_date"]
        definition = api.TableDefinition(
            table_name, [
                api.TableDefinition.Column(name, data_type)
                for name, data_type in zip(columns, types)
            ],
        )
        telemetry = api.Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU
        with api.HyperProcess(
                telemetry, parameters={"log_dir": root}) as process:
            with api.Connection(
                    process.endpoint, path, api.CreateMode.CREATE_AND_REPLACE,
            ) as connection:
                connection.catalog.create_schema(api.SchemaName(schema))
                connection.catalog.create_table(definition)
                if large:
                    generator = random.Random(20261026)
                    rows = [
                        (
                            index,
                            base64.b64encode(
                                generator.randbytes(1_200_000),
                            ).decode("ascii"),
                        )
                        for index in range(16)
                    ]
                    with api.Inserter(connection, definition) as inserter:
                        inserter.add_rows(rows)
                        inserter.execute()
                else:
                    connection.execute_command(
                        f"INSERT INTO {table_name} VALUES "
                        "(1, 'O''Brien réalisé', 12.340, DATE '2026-10-26'), "
                        "(2, 'second row', -0.500, DATE '2024-02-29')"
                    )
                    source_rows = connection.execute_list_query(
                        f"SELECT * FROM {table_name} ORDER BY 1",
                    )
        if large:
            return table_name, columns, types, None
        return table_name, columns, types, source_rows

    def test_real_sdk_parquet_round_trip_with_unicode_absolute_output_path(self):
        api = self._sdk()
        with _temporary_directory("hyper-sdk-source-") as source_root, \
                _temporary_directory("hyper-sdk-unicode-") as output_root:
            hyper = os.path.join(source_root, "synthetic.hyper")
            _write(os.path.join(source_root, "sentinel"), b"")
            table_name, columns, types, source_rows = self._create_typed_hyper(
                api, source_root, hyper,
            )
            unicode_parent = os.path.join(
                output_root, "O'Brien exports réalisé",
            )
            out_dir = os.path.abspath(os.path.join(unicode_parent, "Parquet final"))
            self.assertTrue(os.path.isabs(out_dir))
            self.assertIn("O'Brien", out_dir)
            self.assertIn("réalisé", out_dir)
            identity = {
                "workbook": os.path.join(output_root, "synthetic réalisé.twbx"),
                "archive_member": "Outside/fixture.hyper",
            }
            native_process = api.HyperProcess

            def local_process(*args, **kwargs):
                parameters = dict(kwargs.get("parameters") or {})
                parameters["log_dir"] = source_root
                kwargs["parameters"] = parameters
                return native_process(*args, **kwargs)

            with _local_hyper_environment(), \
                    mock.patch.object(api, "HyperProcess", side_effect=local_process), \
                    mock.patch(
                        "sqlite3.connect",
                        side_effect=AssertionError("native Hyper must use the SDK"),
                    ), \
                    mock.patch(
                        "tableau_export.hyper_reader.read_hyper",
                        side_effect=AssertionError("native Hyper must not use samples"),
                    ), \
                    redirect_stdout(io.StringIO()):
                result = migrate._hyper_to_csv_files(
                    hyper, out_dir, REPORT_NAME, csv, identity=identity,
                    export_format="parquet", require_full=True, quiet=True,
                )

            self.assertEqual(len(result.exports), 1)
            record = result.exports[0]
            parquet_path = os.path.join(out_dir, record["filename"])
            self.assertTrue(os.path.isfile(parquet_path))
            self.assertEqual(record["format"], "parquet")
            self.assertEqual(record["columns"], columns)
            self.assertEqual(record["row_count"], 2)
            self.assertEqual(record["total_rows"], 2)
            self.assertIs(record["sampled"], False)
            self.assertIs(record["truncated"], False)
            self.assertIs(record["full_extract_verified"], True)
            self.assertEqual(record["row_count_source"], "hyper_copy")
            self.assertEqual(record["sha256"], _file_sha256(parquet_path))
            self.assertEqual(record["size_bytes"], os.path.getsize(parquet_path))

            # Hyper's external() path parser has the same Unicode-path issue
            # on some SDK builds. Copy only this synthetic output to an ASCII
            # readback path before independently checking Parquet schema/rows.
            ascii_readback = os.path.join(source_root, "readback.parquet")
            shutil.copyfile(parquet_path, ascii_readback)
            readback_path = ascii_readback.replace("\\", "/")
            telemetry = api.Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU
            with native_process(
                    telemetry, parameters={"log_dir": source_root}) as process:
                with api.Connection(process.endpoint, hyper) as connection:
                    query = (
                        "SELECT * FROM external("
                        f"{api.escape_string_literal(readback_path)}, "
                        "FORMAT => 'parquet') ORDER BY 1"
                    )
                    with connection.execute_query(query) as result_set:
                        actual_columns = list(result_set.schema.columns)
                        actual_rows = [list(row) for row in result_set]
            self.assertEqual(
                [column.name.unescaped for column in actual_columns], columns,
            )
            self.assertEqual([column.type for column in actual_columns], types)
            self.assertEqual(actual_rows, [list(row) for row in source_rows])

            self.assertEqual(os.listdir(out_dir), [record["filename"]])
            for current, directories, files in os.walk(unicode_parent):
                self.assertFalse(any(name.endswith(".tmp") for name in files))
                self.assertFalse(any(
                    any(token in name for token in ("Ã", "Â", "�"))
                    for name in directories + files
                ))
            self.assertEqual(os.listdir(unicode_parent), ["Parquet final"])
            self.assertEqual(
                os.listdir(output_root), ["O'Brien exports réalisé"],
            )

    def test_real_generated_hyper_above_threshold_selects_parquet(self):
        api = self._sdk()
        with _temporary_directory("hyper-large-generated-") as root:
            hyper = os.path.join(root, "generated-large.hyper")
            table_name, columns, unused_types, unused_rows = self._create_typed_hyper(
                api, root, hyper, large=True,
            )
            actual_source_size = os.path.getsize(hyper)
            self.assertGreater(actual_source_size, FULL_EXPORT_THRESHOLD)
            identity = {
                "workbook": os.path.join(root, "synthetic.twbx"),
                "archive_member": "Data/generated-large.hyper",
            }
            native_process = api.HyperProcess

            def local_process(*args, **kwargs):
                parameters = dict(kwargs.get("parameters") or {})
                parameters["log_dir"] = root
                kwargs["parameters"] = parameters
                return native_process(*args, **kwargs)

            with _local_hyper_environment(), \
                    mock.patch.object(api, "HyperProcess", side_effect=local_process), \
                    mock.patch(
                        "sqlite3.connect",
                        side_effect=AssertionError("native Hyper must use the SDK"),
                    ), \
                    mock.patch(
                        "tableau_export.hyper_reader.read_hyper",
                        side_effect=AssertionError("large full export must not sample"),
                    ), \
                    redirect_stdout(io.StringIO()):
                result = migrate._hyper_to_csv_files(
                    hyper, os.path.join(root, "exports"), REPORT_NAME, csv,
                    identity=identity, export_format="auto",
                    require_full=True, quiet=True,
                )
            self.assertEqual(len(result.exports), 1)
            record = result.exports[0]
            self.assertEqual(record["format"], "parquet")
            self.assertEqual(record["columns"], columns)
            self.assertEqual(record["row_count"], 16)
            self.assertEqual(record["total_rows"], 16)
            self.assertEqual(record["size_bytes"], os.path.getsize(
                os.path.join(root, "exports", record["filename"]),
            ))
            self.assertEqual(record["sha256"], _file_sha256(
                os.path.join(root, "exports", record["filename"]),
            ))

    def test_copy_targets_are_unique_ascii_and_confined_to_upload_root(self):
        with _temporary_directory("hyper-upload-ascii-") as root:
            upload_root = os.path.realpath(root)
            project = os.path.join(root, "project")
            out_dir = os.path.join(project, "Data")
            hyper = _write(os.path.join(root, "staged.hyper"), b"synthetic")
            workbook = _write(os.path.join(root, "Synthetic.twbx"), b"synthetic")
            api = StreamingHyperApi(
                columns=("id", "label"),
                rows=[(1, "first"), (2, "second")],
                total_rows=2,
                table_pairs=[("Extract", "First"), ("Extract", "Second")],
            )
            copy_paths = _parquet_marker_api(api)
            with _local_hyper_environment(), \
                    mock.patch.dict(
                        os.environ, {"TTPBI_UPLOAD_ROOT": upload_root}), \
                    mock.patch.object(migrate, "_upload_write_root", None), \
                    mock.patch.dict("sys.modules", {"tableauhyperapi": api.module}), \
                    redirect_stdout(io.StringIO()):
                result = migrate._hyper_to_csv_files(
                    hyper, out_dir, REPORT_NAME, csv,
                    identity=_package_identity(workbook),
                    export_format="parquet", require_full=True, quiet=True,
                )
            self.assertEqual(len(result.exports), 2)
            self.assertEqual(len(copy_paths), 2)
            self.assertEqual(len(set(copy_paths)), 2)
            for path in copy_paths:
                self.assertTrue(os.path.isabs(path), path)
                self.assertTrue(path.isascii(), path)
                self.assertEqual(
                    os.path.commonpath((upload_root, os.path.realpath(path))),
                    upload_root,
                )
                self.assertFalse(os.path.exists(path), "private COPY staging must be removed")
            for record in result.exports:
                self.assertEqual(record["format"], "parquet")
                target = os.path.join(out_dir, record["filename"])
                self.assertTrue(os.path.isfile(target))
                self.assertEqual(record["sha256"], _file_sha256(target))
                self.assertEqual(record["size_bytes"], os.path.getsize(target))


class TestHyperFullMigrationSafety(unittest.TestCase):
    """PBIP full mode rejects incomplete exports without replacing prior state."""

    def _existing_state(self, root, extension="csv"):
        data_dir = os.path.join(root, "Synthetic", "Data")
        os.makedirs(data_dir, exist_ok=True)
        workbook = os.path.join(root, "Synthetic.twbx")
        hyper = _write(os.path.join(root, "staged.hyper"), b"synthetic")
        package_identity = _package_identity(workbook)
        filename = _identity_filename(
            _complete_identity(package_identity, "Extract", "Facts"),
            extension,
        )
        old_file = _write(
            os.path.join(data_dir, filename), b"previous published bytes",
        )
        manifest_path = _write(
            os.path.join(data_dir, "hyper_exports.json"),
            b'{"version":1,"authoritative":true,"exports":[]}\n',
        )
        return {
            "project": os.path.join(root, "Synthetic"),
            "data_dir": data_dir,
            "hyper": hyper,
            "package_identity": package_identity,
            "filename": filename,
            "old_file": old_file,
            "old_file_bytes": open(old_file, "rb").read(),
            "manifest_path": manifest_path,
            "manifest_bytes": open(manifest_path, "rb").read(),
        }

    def _assert_previous_state(self, state):
        with open(state["old_file"], "rb") as stream:
            self.assertEqual(stream.read(), state["old_file_bytes"])
        with open(state["manifest_path"], "rb") as stream:
            self.assertEqual(stream.read(), state["manifest_bytes"])
        self.assertEqual(
            set(os.listdir(state["data_dir"])),
            {os.path.basename(state["old_file"]), "hyper_exports.json"},
        )

    def test_pbip_migration_rejects_resolved_environment_cap_atomically(self):
        with _temporary_directory("hyper-env-cap-") as root:
            state = self._existing_state(root)
            api = StreamingHyperApi(
                rows=[(1, "first"), (2, "second"), (3, "third")],
                total_rows=3,
            )
            with _local_hyper_environment(), \
                    mock.patch.dict(
                    os.environ, {"TTPBI_HYPER_EXPORT_MAX_ROWS": "1"}), \
                    mock.patch.dict("sys.modules", {"tableauhyperapi": api.module}), \
                    mock.patch.object(
                        migrate, "_get_extract_dir", return_value=os.path.join(root, "metadata"),
                    ), \
                    redirect_stdout(io.StringIO()), \
                    self.assertRaises(MigrationError):
                migrate._convert_hyper_to_csv_in_data(
                    state["data_dir"], REPORT_NAME, state["project"],
                    packaged_hyper=[{
                        "path": state["hyper"],
                        "identity": state["package_identity"],
                    }],
                    hyper_export_format="csv", hyper_export_max_rows=None,
                    output_format="pbip", quiet=True,
                )
            self._assert_previous_state(state)

    def test_pbip_migration_rejects_sample_only_reader_fallback_atomically(self):
        with _temporary_directory("hyper-metadata-sample-") as root:
            state = self._existing_state(root)
            sample = {
                "schema": "Extract",
                "table": "Facts",
                "columns": [{"name": "id", "type": "integer"}],
                "sample_rows": [[7]],
                "sample_row_count": 1,
                "row_count": 3,
            }
            with _local_hyper_environment(), \
                    mock.patch.dict("sys.modules", {"tableauhyperapi": None}), \
                    mock.patch(
                        "sqlite3.connect",
                        side_effect=sqlite3.DatabaseError("synthetic non-SQLite Hyper"),
                    ), \
                    mock.patch(
                        "tableau_export.hyper_reader.read_hyper",
                        return_value={"tables": [sample]},
                    ) as sample_reader, \
                    mock.patch.object(
                        migrate, "_get_extract_dir", return_value=os.path.join(root, "metadata"),
                    ), \
                    redirect_stdout(io.StringIO()), \
                    self.assertRaises(MigrationError) as raised:
                migrate._convert_hyper_to_csv_in_data(
                    state["data_dir"], REPORT_NAME, state["project"],
                    packaged_hyper=[{
                        "path": state["hyper"],
                        "identity": state["package_identity"],
                    }],
                    hyper_export_format="csv", output_format="pbip", quiet=True,
                )
            sample_reader.assert_not_called()
            self.assertIn(
                "Metadata/sample readers cannot supply a PBIP migration",
                str(raised.exception),
            )
            self._assert_previous_state(state)

    def test_pbip_migration_rejects_measured_count_mismatch_atomically(self):
        with _temporary_directory("hyper-count-mismatch-") as root:
            state = self._existing_state(root)
            api = StreamingHyperApi(
                rows=[(1, "first"), (2, "second")],
                total_rows=3,
            )
            with _local_hyper_environment(), \
                    mock.patch.dict("sys.modules", {"tableauhyperapi": api.module}), \
                    mock.patch(
                        "sqlite3.connect",
                        side_effect=AssertionError("unexpected SQLite fallback"),
                    ), \
                    mock.patch(
                        "tableau_export.hyper_reader.read_hyper",
                        side_effect=AssertionError("unexpected sample fallback"),
                    ), \
                    mock.patch.object(
                        migrate, "_get_extract_dir",
                        return_value=os.path.join(root, "metadata"),
                    ), \
                    redirect_stdout(io.StringIO()), \
                    self.assertRaises(MigrationError):
                migrate._convert_hyper_to_csv_in_data(
                    state["data_dir"], REPORT_NAME, state["project"],
                    packaged_hyper=[{
                        "path": state["hyper"],
                        "identity": state["package_identity"],
                    }],
                    hyper_export_format="csv",
                    output_format="pbip", quiet=True,
                )
            self._assert_previous_state(state)

    def test_fabric_auto_keeps_csv_sample_diagnostics(self):
        with _temporary_directory("hyper-fabric-sample-") as root:
            project = os.path.join(root, "Synthetic")
            data_dir = os.path.join(project, "Data")
            os.makedirs(data_dir, exist_ok=True)
            workbook = os.path.join(root, "Synthetic.twbx")
            hyper = _write(os.path.join(root, "staged.hyper"), b"synthetic")
            package_identity = _package_identity(workbook)
            sample = {
                "schema": "Extract",
                "table": "Facts",
                "columns": [{"name": "id", "type": "integer"}],
                "sample_rows": [[7]],
                "sample_row_count": 1,
                "row_count": 3,
            }
            with _local_hyper_environment(), \
                    mock.patch.dict("sys.modules", {"tableauhyperapi": None}), \
                    mock.patch(
                        "sqlite3.connect",
                        side_effect=sqlite3.DatabaseError("synthetic non-SQLite Hyper"),
                    ), \
                    mock.patch(
                        "tableau_export.hyper_reader.read_hyper",
                        return_value={"tables": [sample]},
                    ) as sample_reader, \
                    mock.patch.object(
                        migrate, "_get_extract_dir", return_value=os.path.join(root, "metadata"),
                    ), \
                    redirect_stdout(io.StringIO()):
                migrate._convert_hyper_to_csv_in_data(
                    data_dir, REPORT_NAME, project,
                    packaged_hyper=[{"path": hyper, "identity": package_identity}],
                    hyper_export_format="auto", output_format="fabric", quiet=True,
                )
            sample_reader.assert_called_once()
            with open(os.path.join(data_dir, "hyper_exports.json"),
                      encoding="utf-8") as stream:
                record = json.load(stream)["exports"][0]
            self.assertEqual(record["format"], "csv")
            self.assertEqual(record["row_count"], 1)
            self.assertEqual(record["total_rows"], 3)
            self.assertIs(record["sampled"], True)

    def test_fabric_is_not_subject_to_the_pbip_hyper_import_gate(self):
        with _temporary_directory("hyper-fabric-quality-gate-") as root:
            project = os.path.join(root, REPORT_NAME)
            os.makedirs(project, exist_ok=True)
            extracted = {"hyper_files": [{"name": HYPER_MEMBER}]}
            self.assertIsNone(migrate._require_hyper_import(
                project, extracted=extracted, output_format="fabric",
            ))

    def test_no_hyper_leaves_native_partition_and_data_untouched(self):
        with _temporary_directory("hyper-native-preserved-") as root:
            project = os.path.join(root, "Synthetic")
            data_dir = os.path.join(project, "Data")
            table_path = os.path.join(
                project, "Synthetic.SemanticModel", "definition",
                "tables", "Native Facts.tmdl",
            )
            source_bytes = b"id,label\n1,native\n"
            _write(os.path.join(data_dir, "native.csv"), source_bytes)
            original_tmdl = (
                'table \'Native Facts\'\n'
                '\tpartition \'Native Facts\' = m\n'
                '\t\tmode: import\n'
                '\t\tsource =\n'
                '\t\t\t\tCsv.Document(File.Contents("native.csv"))\n'
            )
            _write(table_path, original_tmdl)
            with mock.patch.object(migrate, "_hyper_to_csv_files") as exporter:
                migrate._convert_hyper_to_csv_in_data(
                    data_dir, REPORT_NAME, project,
                    output_format="pbip", quiet=True,
                )
            exporter.assert_not_called()
            self.assertFalse(os.path.exists(
                os.path.join(data_dir, "hyper_exports.json"),
            ))
            with open(table_path, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), original_tmdl)
            with open(os.path.join(data_dir, "native.csv"), "rb") as stream:
                self.assertEqual(stream.read(), source_bytes)

    def test_packaged_excel_without_hyper_stays_opaque_and_unmodified(self):
        with _temporary_directory("hyper-packaged-excel-") as root:
            workbook = os.path.join(root, "Synthetic.twbx")
            payload = b"PK\x03\x04synthetic opaque workbook\x00\xff"
            with zipfile.ZipFile(workbook, "w") as archive:
                archive.writestr("Synthetic.twb", "<workbook/>")
                archive.writestr("Data/lookup.xlsx", payload)
            extract_dir = os.path.join(root, "extracted")
            datasource = {
                "name": "synthetic-excel-source",
                "tables": [{
                    "source_table": "[Exact$]",
                    "connection_details": {
                        "type": "excel-direct",
                        "details": {
                            "directory": "Data",
                            "filename": "lookup.xlsx",
                        },
                    },
                }],
            }
            _write(
                os.path.join(extract_dir, "datasources.json"),
                json.dumps([datasource]),
            )
            project = os.path.join(root, "Synthetic")
            stats = migrate._stage_declared_local_sources(
                workbook, project, extract_dir, copy_files=True,
            )
            self.assertEqual(stats["copied"], 1)
            staged_excel = os.path.join(project, "Data", "lookup.xlsx")
            with open(staged_excel, "rb") as stream:
                self.assertEqual(stream.read(), payload)
            tmdl_path = os.path.join(
                project, "Synthetic.SemanticModel", "definition",
                "tables", "Excel Facts.tmdl",
            )
            original_tmdl = (
                'table \'Excel Facts\'\n'
                '\tpartition \'Excel Facts\' = m\n'
                '\t\tmode: import\n'
                '\t\tsource =\n'
                '\t\t\t\tExcel.Workbook(File.Contents("lookup.xlsx"))\n'
            )
            _write(tmdl_path, original_tmdl)
            with mock.patch.object(migrate, "_hyper_to_csv_files") as exporter:
                migrate._convert_hyper_to_csv_in_data(
                    os.path.join(project, "Data"), REPORT_NAME, project,
                    output_format="pbip", quiet=True, extract_dir=extract_dir,
                )
            exporter.assert_not_called()
            self.assertFalse(os.path.exists(
                os.path.join(project, "Data", "hyper_exports.json"),
            ))
            with open(tmdl_path, encoding="utf-8") as stream:
                self.assertEqual(stream.read(), original_tmdl)
            with open(staged_excel, "rb") as stream:
                self.assertEqual(stream.read(), payload)


class TestHyperCopyFailureRollback(unittest.TestCase):
    """Export, publication, and late partition/manifest failures preserve state."""

    def _fixture(self, root):
        project = os.path.join(root, "Synthetic")
        data_dir = os.path.join(project, "Data")
        os.makedirs(data_dir, exist_ok=True)
        workbook = _write(os.path.join(root, "Synthetic.twbx"), b"synthetic")
        hyper = _write(os.path.join(root, "staged.hyper"), b"synthetic")
        package_identity = _package_identity(workbook)
        identity = _complete_identity(package_identity, "Extract", "Facts")
        filename = _identity_filename(identity, "parquet")
        previous_file = _write(
            os.path.join(data_dir, filename), b"previous Parquet bytes",
        )
        manifest_path = _write(
            os.path.join(data_dir, "hyper_exports.json"),
            b'{"version":1,"authoritative":true,"exports":[]}\n',
        )
        return {
            "project": project,
            "data_dir": data_dir,
            "hyper": hyper,
            "package_identity": package_identity,
            "filename": filename,
            "previous_file": previous_file,
            "previous_file_bytes": open(previous_file, "rb").read(),
            "manifest_path": manifest_path,
            "manifest_bytes": open(manifest_path, "rb").read(),
        }

    def _assert_unchanged(self, fixture):
        with open(fixture["previous_file"], "rb") as stream:
            self.assertEqual(stream.read(), fixture["previous_file_bytes"])
        with open(fixture["manifest_path"], "rb") as stream:
            self.assertEqual(stream.read(), fixture["manifest_bytes"])
        self.assertEqual(
            set(os.listdir(fixture["data_dir"])),
            {fixture["filename"], "hyper_exports.json"},
        )

    def _run_conversion(self, fixture, api, *, merge_manifest=False):
        with _local_hyper_environment(), \
                mock.patch.dict("sys.modules", {"tableauhyperapi": api.module}), \
                mock.patch(
                    "sqlite3.connect",
                    side_effect=AssertionError("unexpected SQLite fallback"),
                ), \
                mock.patch(
                    "tableau_export.hyper_reader.read_hyper",
                    side_effect=AssertionError("unexpected metadata fallback"),
                ), \
                mock.patch.object(
                    migrate, "_get_extract_dir",
                    return_value=os.path.join(
                        fixture["project"], "synthetic-extracted",
                    ),
                ), \
                redirect_stdout(io.StringIO()):
            migrate._convert_hyper_to_csv_in_data(
                fixture["data_dir"], REPORT_NAME, fixture["project"],
                packaged_hyper=[{
                    "path": fixture["hyper"],
                    "identity": fixture["package_identity"],
                }],
                hyper_export_format="parquet",
                output_format="pbip", quiet=True,
                merge_manifest=merge_manifest,
            )

    def _fixture_with_pending_aliases(self, root):
        """Keep late-write rollback independent of XML extraction repairs."""
        from powerbi_import.tmdl_generator import generate_tmdl

        fixture = self._fixture(root)
        identity = _complete_identity(
            fixture["package_identity"], "Extract", "Facts",
        )
        aliases = ("First Pending Alias", "Second Pending Alias")
        datasource = {
            "name": "synthetic-write-rollback-source",
            "connection": {"type": "hyper", "details": {}},
            "tables": [{
                "name": alias,
                "hyper_identity": dict(identity),
                "columns": [
                    {"name": "id", "datatype": "integer"},
                    {"name": "label", "datatype": "string"},
                ],
            } for alias in aliases],
            "calculations": [], "relationships": [], "columns": [],
        }
        _write(
            os.path.join(
                fixture["project"], "synthetic-extracted", "datasources.json",
            ),
            json.dumps([datasource]),
        )
        with redirect_stdout(io.StringIO()):
            generate_tmdl(
                [datasource], REPORT_NAME, None,
                os.path.join(
                    fixture["project"], f"{REPORT_NAME}.SemanticModel",
                ),
                model_mode="import", parameterize=False,
            )
        originals, original_text, table_names = {}, {}, {}
        for path in _all_table_files(fixture["project"]):
            with open(path, "rb") as stream:
                originals[path] = stream.read()
            with open(path, encoding="utf-8") as stream:
                original_text[path] = stream.read()
            self.assertIn("Migration.HyperExportRequired", original_text[path])
            table_names[path] = re.search(
                r"(?m)^table\s+(.+?)\s*$", original_text[path],
            ).group(1).strip("'")
        self.assertEqual(len(originals), 2)
        self.assertEqual(set(table_names.values()), set(aliases))

        # These old claims must not authorize a freshly exported file before
        # each alias's new partition write has actually succeeded.
        old_record = {
            "identity": identity, "format": "parquet",
            "filename": fixture["filename"], "columns": ["id", "label"],
            "row_count": 1, "total_rows": 1, "max_rows": None,
            "sampled": False, "truncated": False,
            "full_extract_verified": True, "row_count_source": "hyper_copy",
            "staged_files": [{
                "table": alias, "filename": fixture["filename"],
                "format": "parquet",
            } for alias in aliases],
        }
        _refresh_file_integrity(old_record, fixture["previous_file"])
        _write(fixture["manifest_path"], json.dumps({
            "version": 1, "authoritative": True, "exports": [old_record],
        }, indent=2) + "\n")
        with open(fixture["manifest_path"], "rb") as stream:
            fixture["manifest_bytes"] = stream.read()
        fixture.update(
            tmdl_originals=originals, tmdl_original_text=original_text,
            table_names=table_names,
        )
        return fixture

    def _assert_late_write_rollback(self, fixture, *, failure):
        """Observe real publication/writes; fail one final atomic replacement."""
        payload = b"PAR1synthetic fresh full exportPAR1"
        api = StreamingHyperApi(total_rows=2)
        copy_paths = _parquet_marker_api(api, payload=payload)
        real_stage = migrate._stage_pending_hyper_partition
        real_text = migrate._atomic_write_text
        real_json = migrate._atomic_write_json
        real_replace = os.replace
        marker = f"synthetic late {failure} write failure"
        state = {
            "record": None, "staging": [], "writes": [], "written": [],
            "manifests": [], "fail_tmdl_path": None, "failed": False,
        }

        def read_bytes(path):
            with open(path, "rb") as stream:
                return stream.read()

        def bindings(tables):
            return [{
                "table": name, "filename": fixture["filename"],
                "format": "parquet",
            } for name in tables]

        def observe_stage(content, data_dir, records, **kwargs):
            record = records[0]
            before = deepcopy(record.get("staged_files", []))
            written = list(state["written"])
            updated = real_stage(content, data_dir, records, **kwargs)
            state["record"] = record
            state["staging"].append({
                "before": before,
                "after": deepcopy(record.get("staged_files", [])),
                "written": written,
                "planned": [
                    deepcopy(binding)
                    for unused_record, binding in kwargs["binding_updates"]
                ],
            })
            return updated

        def observe_text(path, content, encoding="utf-8"):
            if (path not in fixture["tmdl_original_text"]
                    or content == fixture["tmdl_original_text"][path]):
                return real_text(path, content, encoding=encoding)
            attempt = {
                "table": fixture["table_names"][path],
                "written": list(state["written"]),
                "before": deepcopy(state["record"].get("staged_files", [])),
                "data": read_bytes(fixture["previous_file"]),
                "manifest": read_bytes(fixture["manifest_path"]),
                "old_tmdl": read_bytes(path),
                "path": path,
            }
            state["writes"].append(attempt)
            if failure == "TMDL" and len(state["written"]) == 1:
                state["fail_tmdl_path"] = os.path.abspath(path)
            try:
                result = real_text(path, content, encoding=encoding)
                attempt["published_tmdl"] = read_bytes(path)
            finally:
                attempt["after"] = deepcopy(
                    state["record"].get("staged_files", []),
                )
                state["fail_tmdl_path"] = None
            state["written"].append(attempt["table"])
            return result

        def observe_json(path, value):
            if os.path.abspath(path) == os.path.abspath(fixture["manifest_path"]):
                state["manifests"].append(deepcopy(value))
            return real_json(path, value)

        def fail_replace(source, destination):
            target = os.path.abspath(destination)
            should_fail = (
                target == state["fail_tmdl_path"]
                or (failure == "manifest"
                    and target == os.path.abspath(fixture["manifest_path"]))
            )
            if should_fail and not state["failed"]:
                state["failed"] = True
                raise OSError(marker)
            return real_replace(source, destination)

        with mock.patch.object(
                migrate, "_stage_pending_hyper_partition",
                side_effect=observe_stage), \
                mock.patch.object(
                    migrate, "_atomic_write_text", side_effect=observe_text), \
                mock.patch.object(
                    migrate, "_atomic_write_json", side_effect=observe_json), \
                mock.patch("os.replace", side_effect=fail_replace), \
                self.assertRaises(MigrationError) as raised:
            self._run_conversion(fixture, api, merge_manifest=True)

        # Check outside the wrapped migration, so an observation assertion
        # cannot be mistaken for the deliberately injected MigrationError.
        self.assertTrue(state["failed"], "the late write failure must be reached")
        self.assertIn(marker, str(raised.exception))
        self.assertEqual(len(state["staging"]), 2)
        self.assertEqual(len(state["writes"]), 2)
        self.assertEqual(len(state["written"]), 1 if failure == "TMDL" else 2)
        for staging in state["staging"]:
            self.assertEqual(staging["before"], bindings(staging["written"]))
            self.assertEqual(staging["after"], staging["before"])
            self.assertEqual(len(staging["planned"]), 1)
        for attempt in state["writes"]:
            self.assertEqual(attempt["data"], payload, "final Data was published")
            self.assertEqual(attempt["manifest"], fixture["manifest_bytes"])
            self.assertEqual(
                attempt["old_tmdl"],
                fixture["tmdl_originals"][attempt["path"]],
            )
            self.assertEqual(attempt["before"], bindings(attempt["written"]))
            self.assertEqual(attempt["after"], attempt["before"])
            if "published_tmdl" in attempt:
                self.assertIn(
                    fixture["filename"].encode("utf-8"),
                    attempt["published_tmdl"],
                )
                self.assertNotIn(
                    b"Migration.HyperExportRequired", attempt["published_tmdl"],
                )
        if failure == "TMDL":
            self.assertEqual(state["manifests"], [])
            failed_alias = state["writes"][-1]["table"]
            self.assertNotIn(failed_alias, [
                binding["table"]
                for binding in state["record"].get("staged_files", [])
            ])
        else:
            self.assertEqual(len(state["manifests"]), 1)
            exports = state["manifests"][0]["exports"]
            self.assertEqual(len(exports), 1)
            self.assertEqual(exports[0]["staged_files"], bindings(state["written"]))

        self._assert_unchanged(fixture)
        for path, original in fixture["tmdl_originals"].items():
            self.assertEqual(read_bytes(path), original)
        tables_dir = os.path.dirname(next(iter(fixture["tmdl_originals"])))
        self.assertEqual(set(os.listdir(tables_dir)), {
            os.path.basename(path) for path in fixture["tmdl_originals"]
        })
        self.assertTrue(copy_paths, "native COPY must precede the late failure")
        self.assertTrue(all(not os.path.exists(path) for path in copy_paths))

    def test_tmdl_write_failure_after_data_publication_restores_earlier_partition(self):
        with _temporary_directory("hyper-tmdl-write-rollback-") as root:
            fixture = self._fixture_with_pending_aliases(root)
            self._assert_late_write_rollback(fixture, failure="TMDL")

    def test_manifest_write_failure_restores_data_manifest_and_rewritten_partitions(self):
        with _temporary_directory("hyper-manifest-write-rollback-") as root:
            fixture = self._fixture_with_pending_aliases(root)
            self._assert_late_write_rollback(fixture, failure="manifest")

    def test_copy_exception_and_missing_transit_output_preserve_old_manifest_and_data(self):
        cases = (
            ("COPY raises after partial output", "synthetic Hyper COPY failure"),
            ("COPY reports success without a transit file", "COPY did not create"),
        )
        for label, expected_message in cases:
            with self.subTest(case=label), _temporary_directory(
                    "hyper-copy-rollback-") as root:
                fixture = self._fixture(root)
                api = StreamingHyperApi(total_rows=2)

                def failure(destination, unused_sql):
                    if label.startswith("COPY raises"):
                        _write(destination, b"partial native COPY output")
                        raise RuntimeError("synthetic Hyper COPY failure")
                    return 2

                _parquet_marker_api(api, on_copy=failure)
                with self.assertRaises(MigrationError) as raised:
                    self._run_conversion(fixture, api)
                self.assertIn(expected_message.lower(), str(raised.exception).lower())
                self._assert_unchanged(fixture)

    def test_python_transfer_failure_preserves_old_manifest_and_data(self):
        with _temporary_directory("hyper-transfer-rollback-") as root:
            fixture = self._fixture(root)
            api = StreamingHyperApi(total_rows=2)
            _parquet_marker_api(api)
            with mock.patch(
                    "shutil.copyfileobj",
                    side_effect=OSError("synthetic transit transfer failure"),
            ), self.assertRaises(MigrationError) as raised:
                self._run_conversion(fixture, api)
            self.assertIn(
                "synthetic transit transfer failure",
                str(raised.exception),
            )
            self._assert_unchanged(fixture)

    def test_final_publication_failure_rolls_back_replaced_file_and_manifest(self):
        with _temporary_directory("hyper-publish-rollback-") as root:
            fixture = self._fixture(root)
            api = StreamingHyperApi(total_rows=2)
            _parquet_marker_api(api)
            real_replace = os.replace
            export_complete = False
            failed_once = False
            real_export = migrate._hyper_to_csv_files

            def export_then_arm(*args, **kwargs):
                nonlocal export_complete
                result = real_export(*args, **kwargs)
                export_complete = True
                return result

            def fail_final_publish(source, destination):
                nonlocal failed_once
                if (export_complete and not failed_once
                        and os.path.abspath(destination)
                        == os.path.abspath(os.path.join(
                            fixture["data_dir"], fixture["filename"],
                        ))):
                    failed_once = True
                    raise OSError("synthetic final publication failure")
                return real_replace(source, destination)

            with mock.patch.object(
                    migrate, "_hyper_to_csv_files", side_effect=export_then_arm), \
                    mock.patch("os.replace", side_effect=fail_final_publish), \
                    self.assertRaises(MigrationError):
                self._run_conversion(fixture, api)
            self.assertTrue(failed_once, "the injected failure must reach final publication")
            self._assert_unchanged(fixture)

    def test_no_ascii_transit_inside_unicode_upload_is_rejected_without_escape(self):
        with _temporary_directory("hyper-upload-unicode-") as root:
            upload_root = os.path.realpath(os.path.join(root, "upload réalisé"))
            project = os.path.join(upload_root, "Synthetic")
            data_dir = os.path.join(project, "Data")
            os.makedirs(data_dir, exist_ok=True)
            workbook = _write(os.path.join(upload_root, "Synthetic.twbx"), b"synthetic")
            hyper = _write(os.path.join(upload_root, "staged.hyper"), b"synthetic")
            package_identity = _package_identity(workbook)
            filename = _identity_filename(
                _complete_identity(package_identity, "Extract", "Facts"),
                "parquet",
            )
            previous = _write(os.path.join(data_dir, filename), b"previous")
            manifest_path = _write(
                os.path.join(data_dir, "hyper_exports.json"),
                b'{"version":1,"authoritative":true,"exports":[]}\n',
            )
            previous_manifest = open(manifest_path, "rb").read()
            api = StreamingHyperApi(total_rows=2)
            copy_paths = _parquet_marker_api(api)
            with _local_hyper_environment(), \
                    mock.patch.dict(
                        os.environ, {"TTPBI_UPLOAD_ROOT": upload_root}), \
                    mock.patch.object(migrate, "_upload_write_root", None), \
                    mock.patch.dict("sys.modules", {"tableauhyperapi": api.module}), \
                    mock.patch.object(
                        migrate, "_get_extract_dir",
                        return_value=os.path.join(upload_root, "metadata"),
                    ), \
                    redirect_stdout(io.StringIO()), \
                    self.assertRaises(MigrationError) as raised:
                migrate._convert_hyper_to_csv_in_data(
                    data_dir, REPORT_NAME, project,
                    packaged_hyper=[{
                        "path": hyper, "identity": package_identity,
                    }],
                    hyper_export_format="parquet",
                    output_format="pbip", quiet=True,
                )
            self.assertRegex(
                str(raised.exception),
                r"(?i)(ascii|transit|upload|confine|authorized)",
            )
            self.assertEqual(copy_paths, [])
            with open(previous, "rb") as stream:
                self.assertEqual(stream.read(), b"previous")
            with open(manifest_path, "rb") as stream:
                self.assertEqual(stream.read(), previous_manifest)
            self.assertEqual(
                set(os.listdir(data_dir)), {filename, "hyper_exports.json"},
            )


class TestHyperIdentityAnnotation(unittest.TestCase):
    """TMDL stores only the exact, nonempty four-part source identity."""

    @staticmethod
    def _write_table(root, identity):
        from powerbi_import.tmdl_writers import _write_table_tmdl

        _write_table_tmdl(root, {
            "name": "Synthetic Identity",
            "hyper_identity": identity,
        })
        paths = [
            os.path.join(root, name)
            for name in os.listdir(root)
            if name.endswith(".tmdl")
        ]
        if len(paths) != 1:
            raise AssertionError(f"Expected one TMDL table, got {paths!r}")
        with open(paths[0], encoding="utf-8") as stream:
            return stream.read()

    def test_annotation_is_compact_ascii_json_with_only_the_four_identity_fields(self):
        with _temporary_directory("hyper-identity-annotation-") as root:
            identity = {
                "workbook": os.path.join(root, "source café.twbx"),
                "archive_member": HYPER_MEMBER,
                "schema": "Extract",
                "table": "Physical Facts",
                "untrusted_extra": "must not be emitted",
            }
            content = self._write_table(root, identity)
            annotations = _annotation_identities(content)
            self.assertEqual(len(annotations), 1)
            line, serialized = annotations[0]
            expected = {
                key: identity[key]
                for key in ("workbook", "archive_member", "schema", "table")
            }
            self.assertEqual(serialized, expected)
            self.assertEqual(
                line,
                "\tannotation Migration_HyperIdentity = "
                + json.dumps(
                    expected, ensure_ascii=True, separators=(",", ":"),
                ),
            )
            self.assertIn("\\u00e9", line)
            self.assertNotIn("untrusted_extra", line)

    def test_incomplete_empty_or_nonstring_identity_is_not_annotated(self):
        valid = {
            "workbook": "synthetic.twbx",
            "archive_member": HYPER_MEMBER,
            "schema": "Extract",
            "table": "Physical Facts",
        }
        invalid_identities = (
            {"workbook": valid["workbook"]},
            dict(valid, schema=""),
            dict(valid, table=42),
            dict(valid, workbook=None),
        )
        for identity in invalid_identities:
            with self.subTest(identity=identity), _temporary_directory(
                    "hyper-identity-invalid-") as root:
                content = self._write_table(root, identity)
                self.assertNotIn(
                    "annotation Migration_HyperIdentity = ", content,
                )


class TestHyperSourceMetadataClassification(unittest.TestCase):
    """Physical Hyper fields stay distinct from logical declaration metadata."""

    def test_main_named_hyper_metadata_excludes_group_and_table_pseudo_fields(self):
        import xml.etree.ElementTree as ET

        from tableau_export.datasource_extractor import extract_datasource

        group_name = "__synthetic_label_group"
        pseudo_name = "__synthetic_table_object"
        scalar_types = {"id": "integer", "label": "string"}
        declaration_types = {
            **scalar_types, group_name: "string", pseudo_name: "table",
        }
        routing_types = {
            **scalar_types,
            "Display Event ID": "integer", "Display Event Label": "string",
        }
        for scope_name in ("main_connection", "named_hyper_connection"):
            with self.subTest(scope=scope_name), _temporary_directory(
                    "hyper-source-metadata-") as root:
                workbook, unused_hyper, package = _write_synthetic_package(root)
                xml = _main_federated_hyper_datasource(HYPER_MEMBER)
                main = xml.find("./connection")
                relation = main.find("relation")
                # Require actual source metadata, not a leaf-schema fallback.
                relation.remove(relation.find("columns"))
                source = main
                if scope_name == "named_hyper_connection":
                    source = main.find("./named-connection/connection")
                    for child in (relation, main.find("cols")):
                        main.remove(child)
                        source.append(child)
                physical_records = ET.SubElement(source, "metadata-records")
                logical_records = ET.SubElement(xml, "metadata-records")
                for records, types in (
                    (physical_records, scalar_types),
                    (logical_records, {group_name: "string", pseudo_name: "table"}),
                ):
                    for ordinal, (name, datatype) in enumerate(types.items()):
                        record = ET.SubElement(
                            records, "metadata-record", {"class": "column"},
                        )
                        for tag, value in (
                            ("parent-name", "[Logical Events]"),
                            ("remote-name", name), ("local-name", f"[{name}]"),
                            ("local-type", datatype), ("ordinal", str(ordinal)),
                        ):
                            ET.SubElement(record, tag).text = value
                xml.append(ET.fromstring(f"""
                    <column name="[{group_name}]" caption="Synthetic Label Group"
                            datatype="string" role="dimension">
                      <calculation class="categorical-bin" column="[label]" formula="">
                        <bin value='"Synthetic Category"'>
                          <value>"synthetic-member"</value>
                        </bin>
                      </calculation>
                    </column>
                """))
                ET.SubElement(xml, "column", {
                    "name": f"[{pseudo_name}]", "datatype": "table",
                    "role": "dimension",
                })
                original_xml = ET.tostring(xml)

                extracted = extract_datasource(xml, workbook)

                self.assertEqual(ET.tostring(xml), original_xml)
                self.assertEqual(len(extracted["tables"]), 1)
                table = extracted["tables"][0]
                self.assertEqual(table["name"], "Logical Events")
                self.assertEqual(
                    [(column["name"], column["remote_name"], column["datatype"])
                     for column in table["columns"]],
                    [("id", "id", "integer"), ("label", "label", "string")],
                )
                self.assertEqual(table["cols_physical_map"], {
                    "id": "id", "label": "label",
                })
                self.assertEqual(
                    table["hyper_identity"],
                    _complete_identity(package, "Extract", "Physical Events"),
                )
                self.assertEqual(extracted["col_local_name_map"], {
                    name: "Logical Events" for name in routing_types
                })
                self.assertEqual(extracted["col_type_map"], routing_types)
                routing = extracted["hyper_extract_metadata"]
                self.assertEqual(routing["field_local_name_map"], {
                    "id": "id", "label": "label",
                    "Display Event ID": "id", "Display Event Label": "label",
                })
                self.assertEqual(routing["native_col_local_name_map"], {
                    name: "Logical Events" for name in declaration_types
                })
                self.assertEqual(
                    routing["native_col_type_map"], declaration_types,
                )
                provenance = table["hyper_extract_metadata"]
                self.assertEqual(provenance["binding"], "declared_main_hyper_source")
                self.assertEqual(provenance["source_metadata_scope"], scope_name)
                self.assertEqual(
                    {column["name"]: column["datatype"]
                     for column in provenance["native_columns"]},
                    declaration_types,
                )
                # Pre-binding native maps record renames only; this fixture's
                # physical and local names are identical, so none existed.
                self.assertEqual(provenance["native_cols_physical_map"], {})
                self.assertIn(
                    pseudo_name, provenance["excluded_nonphysical_columns"],
                )
                self.assertEqual(len(extracted["columns"]), len(declaration_types))
                declarations = {
                    column["name"]: column for column in extracted["columns"]
                }
                self.assertEqual(
                    {name: column["datatype"]
                     for name, column in declarations.items()},
                    {f"[{name}]": datatype
                     for name, datatype in declaration_types.items()},
                )
                self.assertEqual(
                    declarations[f"[{group_name}]"]["calculation"],
                    {"class": "categorical-bin", "formula": ""},
                )
                self.assertIsNone(
                    declarations[f"[{pseudo_name}]"]["calculation"],
                )
                self.assertEqual(extracted["calculations"], [])

    def test_categorical_membership_is_group_only_and_numeric_bins_are_preserved(self):
        import xml.etree.ElementTree as ET

        from tableau_export.extract_tableau_data import TableauExtractor

        xml = ET.fromstring("""
            <workbook>
              <datasources>
                <datasource name="synthetic-bin-source">
                  <column name="[__synthetic_category]" datatype="string"/>
                  <column name="[__synthetic_amount]" datatype="real"/>
                  <column name="[__synthetic_category_group]"
                          caption="Synthetic Category Group" datatype="string">
                    <calculation class="categorical-bin"
                                 column="[__synthetic_category]" formula="">
                      <bin value='"Synthetic Category A"'>
                        <value>"synthetic-alpha"</value>
                        <value>"synthetic-beta"</value>
                      </bin>
                      <bin value='"Synthetic Category B"'>
                        <value>"synthetic-gamma"</value>
                      </bin>
                    </calculation>
                  </column>
                  <column name="[__synthetic_legacy_interval]"
                          caption="Synthetic Legacy Interval" datatype="integer">
                    <bin source="[__synthetic_amount]" size="7"/>
                  </column>
                  <column name="[__synthetic_calculated_interval]"
                          caption="Synthetic Calculated Interval"
                          datatype="real" role="dimension">
                    <calculation class="bin" formula="[__synthetic_amount]"
                                 size="2.5" peg="-0.5"/>
                  </column>
                </datasource>
              </datasources>
            </workbook>
        """)
        original_xml = ET.tostring(xml)
        with _temporary_directory("hyper-bin-metadata-") as root:
            extractor = TableauExtractor(
                os.path.join(root, "Synthetic.twb"),
                output_dir=os.path.join(root, "metadata"),
            )
            with redirect_stdout(io.StringIO()):
                extractor.extract_datasources(xml)
                extractor.extract_calculations(xml)
                extractor.extract_groups(xml)
                extractor.extract_bins(xml)
            data = extractor.workbook_data

        self.assertEqual(ET.tostring(xml), original_xml)
        self.assertEqual(data["groups"], [{
            "name": "__synthetic_category_group", "group_type": "values",
            "source_field": "__synthetic_category", "source_fields": [],
            "members": {
                "Synthetic Category A": ["synthetic-alpha", "synthetic-beta"],
                "Synthetic Category B": ["synthetic-gamma"],
            },
        }])
        # Membership <bin> nodes have neither a numeric source nor a size:
        # they must not produce a second entry with an empty source/default 10.
        self.assertEqual(data["bins"], [{
            "name": "Synthetic Legacy Interval",
            "source_field": "__synthetic_amount",
            "size": "7", "datatype": "integer",
        }])
        self.assertEqual(
            [(calculation["name"], calculation["class"], calculation["formula"])
             for calculation in data["calculations"]],
            [(
                "[__synthetic_calculated_interval]", "bin",
                "FLOOR((([__synthetic_amount]) - (-0.5)) / (2.5)) * (2.5) + (-0.5)",
            )],
        )
        self.assertEqual(data["calculations"][0]["datatype"], "real")
        self.assertEqual(
            data["datasources"][0]["calculations"], data["calculations"],
        )


class TestHyperIdentityExtractionAndBinding(unittest.TestCase):
    """Exact XML identity flows through annotations, exports, and final M."""

    def test_new_xml_shapes_bind_exact_physical_identity_to_import_partitions(self):
        cases = (
            ("main federated Hyper connection", "main"),
            ("paired Hyper join aliases", "aliases"),
        )
        for shape_label, shape in cases:
            for export_format in ("csv", "parquet"):
                with self.subTest(shape=shape_label, format=export_format), \
                        _temporary_directory("hyper-identity-e2e-") as root:
                    fixture_root = os.path.join(root, "O'Brien réalisé")
                    os.makedirs(fixture_root)
                    fixture = _build_bound_fixture(
                        fixture_root, shape=shape, export_format=export_format,
                    )
                    datasource = fixture["datasource"]
                    expected_names = [table["name"] for table in datasource["tables"]]
                    self.assertTrue(expected_names)
                    if shape == "main":
                        self.assertEqual(expected_names, ["Logical Events"])
                        self.assertEqual(
                            datasource["tables"][0]["caption"], "Event Caption",
                        )
                    else:
                        self.assertEqual(
                            set(expected_names), {"Current User", "Manager User"},
                        )
                        self.assertEqual(
                            {table["name"]: table["caption"]
                             for table in datasource["tables"]},
                            {
                                "Current User": "Current User Caption",
                                "Manager User": "Manager User Caption",
                            },
                        )
                    for table in datasource["tables"]:
                        identity = table.get("hyper_identity")
                        self.assertEqual(
                            set(identity or ()),
                            {"workbook", "archive_member", "schema", "table"},
                        )
                        self.assertEqual(identity["workbook"], fixture["workbook"])
                        self.assertEqual(identity["archive_member"], HYPER_MEMBER)
                        self.assertEqual(identity["schema"], "Extract")
                        physical_name = (
                            "Physical Events" if shape == "main"
                            else "Physical User"
                        )
                        self.assertEqual(identity["table"], physical_name)

                    records = fixture["manifest"]["exports"]
                    self.assertEqual(len(records), 1)
                    record = records[0]
                    self.assertEqual(
                        record["identity"],
                        _complete_identity(
                            fixture["package_identity"],
                            "Extract",
                            "Physical Events" if shape == "main" else "Physical User",
                        ),
                    )
                    exported_path = _record_export_path(fixture, record)
                    self.assertEqual(record["sha256"], _file_sha256(exported_path))
                    self.assertEqual(record["size_bytes"], os.path.getsize(exported_path))
                    self.assertEqual(record["row_count"], 2)
                    self.assertEqual(record["total_rows"], 2)
                    self.assertIs(record["sampled"], False)

                    annotated = _tmdl_files_with_hyper_annotation(fixture["project"])
                    self.assertEqual(len(annotated), len(expected_names))
                    bound_tables = set()
                    for path, content in annotated:
                        table_name = re.search(
                            r"(?m)^table\s+(.+?)\s*$", content,
                        ).group(1).strip("'")
                        bound_tables.add(table_name)
                        annotations = _annotation_identities(content)
                        self.assertEqual(len(annotations), 1)
                        annotation_line, annotation = annotations[0]
                        self.assertEqual(
                            annotation,
                            record["identity"],
                        )
                        self.assertEqual(
                            annotation_line,
                            "\tannotation Migration_HyperIdentity = "
                            + json.dumps(
                                record["identity"],
                                ensure_ascii=True, separators=(",", ":"),
                            ),
                        )
                        self.assertIn("\\u00e9", annotation_line)
                        self.assertIn("\t\tmode: import", content)
                        self.assertNotIn("Migration.HyperExportRequired", content)
                        self.assertIn(record["filename"], content)
                        if export_format == "csv":
                            self.assertIn("Csv.Document(File.Contents(", content)
                            self.assertNotIn("Parquet.Document", content)
                        else:
                            self.assertIn("Parquet.Document(File.Contents(", content)
                            self.assertNotIn("Csv.Document", content)
                        self.assertTrue(os.path.isabs(path))
                    self.assertEqual(bound_tables, set(expected_names))

                    staged_bindings = record.get("staged_files", [])
                    self.assertEqual(
                        {binding["table"] for binding in staged_bindings},
                        set(expected_names),
                    )
                    from powerbi_import.migration_quality import (
                        build_hyper_import_evidence,
                    )
                    evidence = build_hyper_import_evidence(
                        fixture["extracted"], fixture["project"],
                    )
                    self.assertEqual(
                        evidence["status"], "static_verified", evidence["diagnostics"],
                    )
                    self.assertEqual(evidence["runtime_status"], "not_run")
                    self.assertEqual(evidence["diagnostics"], [])

    def test_native_only_history_is_excluded_unless_required_by_join_or_calculation(self):
        import xml.etree.ElementTree as ET

        from powerbi_import.errors import ExtractionError
        from tableau_export.datasource_extractor import extract_datasource

        historical = "__synthetic_historical_native_only"
        remote = "__synthetic_historical_remote"
        with _temporary_directory("hyper-native-history-") as root:
            workbook, unused_hyper, package = _write_synthetic_package(root)
            for dependency in ("unused", "join", "calculation"):
                with self.subTest(dependency=dependency):
                    xml = _alias_hyper_datasource(HYPER_MEMBER)
                    native_leaves = xml.findall(
                        ".//relation[@name='Current User']"
                        "[@connection='synthetic-native-ref']",
                    )
                    self.assertEqual(len(native_leaves), 2)
                    for leaf in native_leaves:
                        ET.SubElement(leaf.find("columns"), "column", {
                            "name": historical, "datatype": "integer",
                            "ordinal": "1",
                        })
                    ET.SubElement(xml, "column", {
                        "name": f"[{historical}]", "datatype": "integer",
                        "role": "dimension",
                    })
                    ET.SubElement(xml.find("./connection/cols"), "map", {
                        "key": f"[{historical}]",
                        "value": f"[Current User].[{remote}]",
                    })
                    # Keep both native and extract object copies as real joins.
                    # Only the negative join case depends on the absent field.
                    for join in xml.findall(".//relation[@type='join']"):
                        if join.get("name") == "Native User Join":
                            left = (historical if dependency == "join"
                                    else "Current User ID")
                            right = "Manager User ID"
                        else:
                            left = right = "id"
                        clause = ET.SubElement(join, "clause", {"type": "join"})
                        comparison = ET.SubElement(
                            clause, "expression", {"op": "="},
                        )
                        ET.SubElement(comparison, "expression", {
                            "op": f"[Current User].[{left}]",
                        })
                        ET.SubElement(comparison, "expression", {
                            "op": f"[Manager User].[{right}]",
                        })
                    if dependency == "calculation":
                        calculation = ET.SubElement(xml, "column", {
                            "name": "[__synthetic_snapshot_dependency_probe]",
                            "datatype": "integer", "role": "measure",
                        })
                        ET.SubElement(calculation, "calculation", {
                            "class": "tableau",
                            "formula": f"[Current User].[{historical}] + 1",
                        })

                    if dependency != "unused":
                        with self.assertRaises(ExtractionError) as raised:
                            extract_datasource(xml, workbook)
                        self.assertIn(historical, str(raised.exception))
                        continue

                    extracted = extract_datasource(xml, workbook)
                    tables = {table["name"]: table for table in extracted["tables"]}
                    self.assertEqual(set(tables), {"Current User", "Manager User"})
                    expected_identity = _complete_identity(
                        package, "Extract", "Physical User",
                    )
                    for alias, table in tables.items():
                        self.assertEqual(table["hyper_identity"], expected_identity)
                        self.assertEqual(
                            [column["name"] for column in table["columns"]],
                            [f"{alias} ID"],
                        )
                        self.assertEqual(
                            table["cols_physical_map"], {"id": f"{alias} ID"},
                        )
                    provenance = tables["Current User"]["hyper_extract_metadata"]
                    self.assertEqual(
                        {column["name"] for column in provenance["native_columns"]},
                        {"Current User ID", historical},
                    )
                    self.assertEqual(provenance["excluded_native_columns"], [historical])
                    self.assertEqual(
                        provenance["native_cols_physical_map"][remote], historical,
                    )
                    self.assertEqual(
                        tables["Manager User"]["hyper_extract_metadata"]
                        ["excluded_native_columns"], [],
                    )
                    self.assertNotIn("materialized_extract", extracted)

    def test_alias_mismatch_does_not_guess_the_missing_logical_identity(self):
        from tableau_export.datasource_extractor import extract_datasource

        with _temporary_directory("hyper-alias-mismatch-") as root:
            workbook, unused_hyper, unused_package = _write_synthetic_package(root)
            xml_datasource = _alias_hyper_datasource(
                HYPER_MEMBER,
                extract_aliases=("Current User", "Unrelated Alias"),
            )
            extracted = extract_datasource(xml_datasource, workbook)
            tables = {table["name"]: table for table in extracted["tables"]}
            self.assertIn("Current User", tables)
            self.assertIn("Manager User", tables)
            self.assertNotIn("hyper_identity", tables["Manager User"])
            self.assertNotIn("materialized_extract", extracted)

    def test_snapshot_calculation_maps_replace_excluded_alias_native_history(self):
        import xml.etree.ElementTree as ET

        from powerbi_import.errors import ExtractionError
        from tableau_export.datasource_extractor import extract_datasource

        field = "__synthetic_snapshot_date"
        remote = "__synthetic_date_value"
        with _temporary_directory("hyper-snapshot-routing-") as root:
            workbook, unused_hyper, package = _write_synthetic_package(root)
            xml = _alias_hyper_datasource(HYPER_MEMBER)
            native = xml.find("./connection")
            records = ET.SubElement(native, "metadata-records")

            def declare(parent, alias, physical, datatype):
                record = ET.SubElement(parent, "metadata-record", {"class": "column"})
                for key, value in (
                    ("parent-name", f"[{alias}]"), ("remote-name", physical),
                    ("local-name", f"[{field}]"), ("local-type", datatype),
                    ("ordinal", "1"),
                ):
                    ET.SubElement(record, key).text = value

            # Native history routes the same local field to the other alias.
            declare(records, "Manager User", "__synthetic_legacy_date", "string")
            for leaf in xml.findall(
                    ".//relation[@name='Manager User']"
                    "[@connection='synthetic-native-ref']"):
                ET.SubElement(leaf.find("columns"), "column", {
                    "name": field, "datatype": "string", "ordinal": "1",
                })
            ET.SubElement(native.find("cols"), "map", {
                "key": f"[{field}]",
                "value": "[Manager User].[__synthetic_legacy_date]",
            })
            ET.SubElement(xml, "column", {
                "name": f"[{field}]", "datatype": "datetime", "role": "dimension",
            })
            declare(
                xml.find("./extract/connection/metadata-records"),
                "Current User", remote, "datetime",
            )
            # Corroborate the new snapshot column in both extract object copies.
            for scope in (
                xml.find("./extract/connection"),
                xml.find("./object-graph/objects/object/properties[@context='extract']"),
            ):
                leaf = scope.find(".//relation[@name='Current User']/columns")
                ET.SubElement(leaf, "column", {
                    "name": remote, "datatype": "datetime", "ordinal": "1",
                })
            calculation = ET.SubElement(xml, "column", {
                "name": "[__synthetic_snapshot_year]", "datatype": "integer",
                "role": "dimension",
            })
            formula = f"DATEPART('year', [Current User].[{field}])"
            ET.SubElement(calculation, "calculation", {
                "class": "tableau", "formula": formula,
            })

            extracted = extract_datasource(xml, workbook)
            tables = {table["name"]: table for table in extracted["tables"]}
            self.assertEqual(set(tables), {"Current User", "Manager User"})
            self.assertEqual(
                {column["name"] for column in tables["Current User"]["columns"]},
                {"Current User ID", field},
            )
            self.assertEqual(
                [column["name"] for column in tables["Manager User"]["columns"]],
                ["Manager User ID"],
            )
            self.assertEqual(extracted["col_local_name_map"][field], "Current User")
            self.assertEqual(extracted["col_type_map"][field], "datetime")
            provenance = extracted["hyper_extract_metadata"]
            self.assertEqual(
                provenance["native_col_local_name_map"][field], "Manager User",
            )
            self.assertEqual(provenance["native_col_type_map"][field], "string")
            self.assertEqual(
                tables["Manager User"]["hyper_extract_metadata"]
                ["excluded_native_columns"], [field],
            )
            self.assertEqual(
                tables["Current User"]["cols_physical_map"][remote], field,
            )
            self.assertEqual(extracted["calculations"][0]["formula"], formula)
            for table in tables.values():
                self.assertEqual(
                    table["hyper_identity"],
                    _complete_identity(package, "Extract", "Physical User"),
                )
            # Rebuilding routing must not authorize the absent snapshot field
            # merely because that alias's historical native schema declared it.
            wrong_alias = deepcopy(xml)
            wrong_alias.find("./column/calculation").set(
                "formula", f"DATEPART('year', [Manager User].[{field}])",
            )
            with self.assertRaises(ExtractionError) as raised:
                extract_datasource(wrong_alias, workbook)
            self.assertIn(field, str(raised.exception))

    def test_foreign_or_duplicate_archive_member_is_not_a_hyper_identity(self):
        from tableau_export.datasource_extractor import extract_datasource

        cases = (
            ("foreign member", ("Data/other.hyper",)),
            ("duplicate exact member", (HYPER_MEMBER, HYPER_MEMBER)),
        )
        for label, members in cases:
            with self.subTest(case=label), _temporary_directory(
                    "hyper-member-identity-") as root:
                workbook = os.path.abspath(os.path.join(root, "Synthetic.twbx"))
                with zipfile.ZipFile(workbook, "w") as archive:
                    archive.writestr("Synthetic.twb", "<workbook/>")
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", UserWarning)
                        for member in members:
                            archive.writestr(member, b"synthetic Hyper member")
                datasource = _main_federated_hyper_datasource(HYPER_MEMBER)
                extracted = extract_datasource(datasource, workbook)
                self.assertTrue(extracted["tables"])
                self.assertTrue(all(
                    "hyper_identity" not in table
                    for table in extracted["tables"]
                ))


class TestHyperImportEvidence(unittest.TestCase):
    """Static quality evidence validates final partitions, not manifest claims."""

    @staticmethod
    def _builder():
        # The evidence API is added by the evidence owner in the same sprint.
        # Keep its import local so collection stays possible during integration.
        from powerbi_import.migration_quality import build_hyper_import_evidence
        return build_hyper_import_evidence

    def _fixture(self, *, export_format="csv", rows=((1, "first"), (2, "second")),
                 total_rows=2):
        temp = _temporary_directory("hyper-quality-evidence-")
        self.addCleanup(temp.cleanup)
        return _build_bound_fixture(
            temp.name, export_format=export_format,
            rows=rows, total_rows=total_rows,
        )

    def _write_bound_csv_query(self, fixture, tail, final_step, columns):
        """Author executable final M over a real, verified synthetic CSV."""
        from powerbi_import.m_validator import validate_m_query
        from powerbi_import.tmdl_writers import _write_table_tmdl
        from tableau_export.m_query_builder import _m_literal

        record = fixture["manifest"]["exports"][0]
        expression = (
            "let\n"
            "    Source = Table.TransformColumnTypes(Table.PromoteHeaders("
            "Csv.Document(File.Contents(DataFolder & "
            + _m_literal("\\" + record["filename"])
            + '), [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]), '
            '[PromoteAllScalars=true]), {{"id", Int64.Type}, '
            '{"label", type text}}, "en-US"),\n'
            + tail + "\nin\n    " + final_step
        )
        self.assertEqual(validate_m_query(expression), [], expression)
        files = _all_table_files(fixture["project"])
        self.assertEqual(len(files), 1)
        _write_table_tmdl(os.path.dirname(files[0]), {
            "name": fixture["datasource"]["tables"][0]["name"],
            "hyper_identity": record["identity"],
            "columns": [
                {"name": name, "sourceColumn": name, "dataType": datatype}
                for name, datatype in columns.items()
            ],
            "partitions": [{
                "mode": "import", "source": {"type": "m", "expression": expression},
            }],
        })
        self.assertEqual(_all_table_files(fixture["project"]), files)

    def test_bound_lookup_table_join_survives_repeat_conversion_byte_for_byte(self):
        fixture = self._fixture()
        self._write_bound_csv_query(
            fixture,
            '    Lookup = #table(type table [Key = text, Value = text], '
            '{{"first", "Group One"}, {"second", "Group Two"}}),\n'
            '    Joined = Table.NestedJoin(Source, {"label"}, Lookup, {"Key"}, '
            '"__synthetic_lookup", JoinKind.LeftOuter),\n'
            '    Expanded = Table.ExpandTableColumn(Joined, "__synthetic_lookup", '
            '{"Value"}, {"__synthetic_lookup_value"})',
            "Expanded", {"id": "int64", "label": "string"},
        )
        evidence = self._builder()(fixture["extracted"], fixture["project"])
        self.assertEqual(
            evidence["status"], "static_verified", evidence["diagnostics"],
        )

        def snapshot():
            contents = {}
            for directory, unused_dirs, filenames in os.walk(fixture["project"]):
                for filename in filenames:
                    path = os.path.join(directory, filename)
                    with open(path, "rb") as stream:
                        contents[os.path.relpath(path, fixture["project"])] = stream.read()
            return contents

        before = snapshot()
        data_entries = set(os.listdir(fixture["data_dir"]))
        with _local_hyper_environment(), mock.patch.object(
                migrate, "_hyper_to_csv_files",
                side_effect=AssertionError("A verified bound source must not be re-exported"),
        ) as reader, redirect_stdout(io.StringIO()):
            for unused_attempt in range(2):
                migrate._convert_hyper_to_csv_in_data(
                    fixture["data_dir"], REPORT_NAME, fixture["project"],
                    extract_dir=fixture["extract_dir"], quiet=True,
                )
                self.assertEqual(snapshot(), before)
                self.assertEqual(set(os.listdir(fixture["data_dir"])), data_entries)
        reader.assert_not_called()
        evidence = self._builder()(fixture["extracted"], fixture["project"])
        self.assertEqual(
            evidence["status"], "static_verified", evidence["diagnostics"],
        )

    def test_evidence_requires_resolvable_renamed_and_derived_column_lineage(self):
        fixture = self._fixture()
        record = fixture["manifest"]["exports"][0]
        self.assertEqual(record["columns"], ["id", "label"])
        renamed, derived, peer = (
            "__synthetic_renamed_id", "__synthetic_derived_id", "__synthetic_cycle_peer",
        )
        rename_step = (
            '    Renamed = Table.RenameColumns(Source, {{"id", "' + renamed + '"}})'
        )
        cases = (
            ("explicit rename then derivation",
             f'    Derived = Table.AddColumn(Renamed, "{derived}", '
             f'each [{renamed}] + 1, Int64.Type)', "Derived", "static_verified"),
            ("missing derivation", "", "Renamed", "failed"),
            ("unknown dependency",
             f'    Derived = Table.AddColumn(Renamed, "{derived}", '
             'each [__synthetic_unknown_id] + 1, Int64.Type)', "Derived", "failed"),
            ("unknown table derivation",
             "    Derived = __synthetic_unknown_transform(Renamed)",
             "Derived", "failed"),
            ("cyclic field derivation",
             f'    Derived = Table.AddColumn(Renamed, "{derived}", '
             f'each [{peer}] + 1, Int64.Type),\n'
             f'    Peer = Table.AddColumn(Derived, "{peer}", '
             f'each [{derived}] + 1, Int64.Type)', "Peer", "failed"),
        )
        for label, additions, final_step, status in cases:
            with self.subTest(lineage=label):
                self._write_bound_csv_query(
                    fixture, rename_step + (",\n" + additions if additions else ""),
                    final_step, {renamed: "int64", derived: "int64", "label": "string"},
                )
                evidence = self._builder()(fixture["extracted"], fixture["project"])
                diagnostics = evidence["diagnostics"]
                self.assertEqual(evidence["status"], status, diagnostics)
                self.assertEqual(evidence["runtime_status"], "not_run")
                self.assertEqual(len(evidence["bindings"]), 1, diagnostics)
                schema = evidence["bindings"][0]["schema"]
                if status == "static_verified":
                    self.assertEqual(schema["status"], "static_verified", diagnostics)
                    self.assertTrue(
                        {renamed, derived}.issubset(schema["derived_source_columns"]),
                        schema,
                    )
                else:
                    self.assertEqual(schema["status"], "unverified", diagnostics)
                    self.assertTrue(any(derived in issue for issue in diagnostics), diagnostics)

    def test_evidence_accepts_intrinsic_scalar_date_time_duration_constructors(self):
        fixture = self._fixture()
        derived = "__synthetic_scalar_value"
        cases = (
            ("date", "Date.Day(#date(2032, 1, [id]))"),
            ("time", "Time.Hour(#time([id], 2, 3))"),
            ("datetime",
             "Date.Day(DateTime.Date(#datetime(2032, 1, [id], 2, 3, 4)))"),
            ("datetimezone",
             "Date.Day(DateTime.Date(DateTimeZone.RemoveZone("
             "#datetimezone(2032, 1, [id], 2, 3, 4, 0, 0))))"),
            ("duration", "Duration.Days(#duration([id], 2, 3, 4))"),
        )
        for constructor, expression in cases:
            with self.subTest(constructor=constructor):
                self._write_bound_csv_query(
                    fixture,
                    f'    Derived = Table.AddColumn(Source, "{derived}", '
                    f'each {expression}, Int64.Type)',
                    "Derived", {"id": "int64", "label": "string", derived: "int64"},
                )
                evidence = self._builder()(fixture["extracted"], fixture["project"])
                diagnostics = evidence["diagnostics"]
                self.assertEqual(evidence["status"], "static_verified", diagnostics)
                self.assertEqual(diagnostics, [])
                self.assertEqual(evidence["runtime_status"], "not_run")
                self.assertEqual(len(evidence["bindings"]), 1, diagnostics)
                schema = evidence["bindings"][0]["schema"]
                self.assertEqual(schema["status"], "static_verified", diagnostics)
                self.assertIn(derived, schema["derived_source_columns"], schema)
                self.assertEqual(schema["missing_source_columns"], [], schema)
                self.assertEqual(schema["runtime_row_schema"], "not_verified")

    def test_scalar_constructor_support_does_not_hide_unproven_dependencies(self):
        fixture = self._fixture()
        derived = "__synthetic_scalar_value"
        valid_date = "Date.Day(#date(2032, 1, [id]))"
        cases = (
            ("unknown hash symbol", "Source", "#__synthetic_scalar([id])",
             "#__synthetic_scalar"),
            ("constructor name lookalike", "Source",
             "#date_unknown(2032, 1, [id])", "#date_unknown"),
            ("constructor used without a call", "Source",
             "Date.Day(#date) + [id]", "#date"),
            ("quoted identifier is not an intrinsic", "Source",
             '#"#date"(2032, 1, [id])', "#date"),
            ("table constructor is not a scalar", "Source",
             '#table({"value"}, {{[id]}})', "#table"),
            ("unknown source binding", '#"__synthetic_missing_source"',
             valid_date, "__synthetic_missing_source"),
            ("missing field inside recognized constructor", "Source",
             "Date.Day(#date(2032, 1, [__synthetic_absent_id]))",
             "__synthetic_absent_id"),
        )
        for label, source, expression, blocker in cases:
            with self.subTest(dependency=label):
                self._write_bound_csv_query(
                    fixture,
                    f'    Derived = Table.AddColumn({source}, "{derived}", '
                    f'each {expression}, Int64.Type)',
                    "Derived", {"id": "int64", "label": "string", derived: "int64"},
                )
                evidence = self._builder()(fixture["extracted"], fixture["project"])
                diagnostics = evidence["diagnostics"]
                self.assertEqual(evidence["status"], "failed", diagnostics)
                self.assertEqual(evidence["runtime_status"], "not_run")
                self.assertEqual(len(evidence["bindings"]), 1, diagnostics)
                self.assertEqual(
                    evidence["bindings"][0]["schema"]["status"],
                    "unverified", diagnostics,
                )
                self.assertTrue(
                    any(blocker in issue for issue in diagnostics), diagnostics,
                )

    def _rewrite_manifest(self, fixture, mutator):
        with open(fixture["manifest_path"], encoding="utf-8") as stream:
            manifest = json.load(stream)
        mutator(manifest)
        _save_manifest(fixture, manifest)

    def _mutate_header(self, fixture):
        record = fixture["manifest"]["exports"][0]
        path = _record_export_path(fixture, record)
        _write(path, "wrong,label\n1,first\n2,second\n")

        def update(manifest):
            _refresh_file_integrity(manifest["exports"][0], path)

        self._rewrite_manifest(fixture, update)

    def _mutate_rows(self, fixture):
        record = fixture["manifest"]["exports"][0]
        path = _record_export_path(fixture, record)
        _write(path, "id,label\n1,first\n")

        def update(manifest):
            _refresh_file_integrity(manifest["exports"][0], path)

        self._rewrite_manifest(fixture, update)

    def _mutate_binding(self, fixture):
        record = fixture["manifest"]["exports"][0]
        for path, content in _tmdl_files_with_hyper_annotation(fixture["project"]):
            self.assertIn(record["filename"], content)
            _write(path, content.replace(
                record["filename"], "unrelated-unconsumed.csv", 1,
            ))
        # This remains an intentionally false claim. Static evidence must read
        # each final TMDL partition rather than trusting staged_files.
        def update(manifest):
            manifest["exports"][0]["staged_files"] = [{
                "table": table["name"],
                "csv_filename": record["filename"],
            } for table in fixture["datasource"]["tables"]]
        self._rewrite_manifest(fixture, update)

    def _mutate_empty_placeholder(self, fixture):
        for path, content in _tmdl_files_with_hyper_annotation(fixture["project"]):
            marker = "\t\tsource =\n"
            start = content.index(marker) + len(marker)
            end = content.index("\n\tannotation PBI_ResultType", start)
            empty = '\t\t\t\t#table({"id", "label"}, {})\n'
            _write(path, content[:start] + empty + content[end:])
        def update(manifest):
            manifest["exports"][0]["staged_files"] = [{
                "table": table["name"],
                "csv_filename": manifest["exports"][0]["filename"],
            } for table in fixture["datasource"]["tables"]]
        self._rewrite_manifest(fixture, update)

    def _mutate_wrong_annotation(self, fixture):
        for path, content in _tmdl_files_with_hyper_annotation(fixture["project"]):
            annotation_lines = _annotation_identities(content)
            self.assertEqual(len(annotation_lines), 1)
            line, identity = annotation_lines[0]
            identity["table"] = "Caption Is Not Physical Identity"
            replacement = (
                "\tannotation Migration_HyperIdentity = "
                + json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
            )
            _write(path, content.replace(line, replacement, 1))

    def _mutate_missing_annotation(self, fixture):
        for path, content in _tmdl_files_with_hyper_annotation(fixture["project"]):
            _write(path, "\n".join(
                line for line in content.splitlines()
                if not line.startswith(
                    "\tannotation Migration_HyperIdentity = ",
                )
            ) + "\n")

    def _mutate_unconsumed_export(self, fixture):
        first = fixture["manifest"]["exports"][0]
        identity = dict(first["identity"], table="Unconsumed Physical Table")
        filename = _identity_filename(identity, "csv")
        path = _write(
            os.path.join(fixture["data_dir"], filename),
            "id,label\n9,orphan\n",
        )
        new_record = {
            "identity": identity,
            "format": "csv",
            "filename": filename,
            "csv_filename": filename,
            "columns": list(first["columns"]),
            "row_count": 1,
            "total_rows": 1,
            "max_rows": None,
            "sampled": False,
            "truncated": False,
            "full_extract_verified": True,
            "row_count_source": "csv_writer",
            "sha256": _file_sha256(path),
            "size_bytes": os.path.getsize(path),
        }
        self._rewrite_manifest(
            fixture,
            lambda manifest: manifest["exports"].append(new_record),
        )

    def test_complete_binding_is_static_verified_but_runtime_remains_unrun(self):
        fixture = self._fixture()
        evidence = self._builder()(fixture["extracted"], fixture["project"])
        self.assertEqual(
            evidence["status"], "static_verified", evidence["diagnostics"],
        )
        self.assertEqual(evidence["runtime_status"], "not_run")
        self.assertEqual(evidence["diagnostics"], [])

    def test_native_zero_row_export_is_valid_when_schema_and_count_match(self):
        fixture = self._fixture(rows=(), total_rows=0)
        record = fixture["manifest"]["exports"][0]
        self.assertEqual(record["row_count"], 0)
        self.assertEqual(record["total_rows"], 0)
        path = _record_export_path(fixture, record)
        with open(path, encoding="utf-8", newline="") as stream:
            self.assertEqual(list(csv.reader(stream)), [["id", "label"]])
        evidence = self._builder()(fixture["extracted"], fixture["project"])
        self.assertEqual(
            evidence["status"], "static_verified", evidence["diagnostics"],
        )
        self.assertEqual(evidence["runtime_status"], "not_run")

    def test_no_hyper_is_not_applicable(self):
        with _temporary_directory("hyper-evidence-not-applicable-") as root:
            project = os.path.join(root, "Native")
            os.makedirs(os.path.join(project, "Data"), exist_ok=True)
            evidence = self._builder()(
                {"datasources": [{
                    "connection": {"type": "excel"},
                    "tables": [{"name": "Native Facts"}],
                }]},
                project,
            )
            self.assertEqual(
                evidence["status"], "not_applicable", evidence["diagnostics"],
            )
            self.assertEqual(evidence["runtime_status"], "not_run")
            self.assertEqual(evidence["diagnostics"], [])

    def test_orchestrator_pbip_gate_rejects_failed_static_evidence(self):
        from powerbi_import import migration_quality

        with _temporary_directory("hyper-evidence-orchestrator-gate-") as root:
            extracted = {"hyper_files": [{"name": HYPER_MEMBER}]}
            evidence = {
                "status": "failed",
                "diagnostics": ["Synthetic final partition is unbound"],
                "runtime_status": "not_run",
            }
            with mock.patch.object(
                    migration_quality, "build_hyper_import_evidence",
                    return_value=evidence,
            ) as builder, self.assertRaises(MigrationError) as raised:
                migrate._require_hyper_import(
                    root, extracted=extracted, output_format="pbip",
                )
            builder.assert_called_once_with(extracted, root)
            self.assertIn(
                "Synthetic final partition is unbound",
                str(raised.exception),
            )

    def test_orchestrator_pbip_gate_rejects_unknown_success_like_status(self):
        from powerbi_import import migration_quality

        with _temporary_directory("hyper-evidence-unknown-status-") as root:
            extracted = {"hyper_files": [{"name": HYPER_MEMBER}]}
            for status in ("success", "verified", "static_verified_extra"):
                with self.subTest(status=status):
                    evidence = {
                        "status": status,
                        "diagnostics": [],
                        "runtime_status": "not_run",
                    }
                    with mock.patch.object(
                            migration_quality, "build_hyper_import_evidence",
                            return_value=evidence,
                    ) as builder, self.assertRaises(MigrationError) as raised:
                        migrate._require_hyper_import(
                            root, extracted=extracted, output_format="pbip",
                        )
                    builder.assert_called_once_with(extracted, root)
                    self.assertIn(status, str(raised.exception))

    def test_manifest_file_integrity_final_partition_and_consumption_fail_closed(self):
        cases = (
            ("missing manifest", lambda fixture: os.unlink(fixture["manifest_path"])),
            ("invalid manifest", lambda fixture: _write(
                fixture["manifest_path"], "not-json",
            )),
            ("incomplete manifest", lambda fixture: self._rewrite_manifest(
                fixture, lambda manifest: manifest["exports"][0].pop("size_bytes"),
            )),
            ("capped export", lambda fixture: self._rewrite_manifest(
                fixture, lambda manifest: manifest["exports"][0].update({
                    "max_rows": 1, "sampled": True, "truncated": True,
                }),
            )),
            ("unknown source count", lambda fixture: self._rewrite_manifest(
                fixture, lambda manifest: manifest["exports"][0].update({
                    "total_rows": None,
                }),
            )),
            ("measured count mismatch", lambda fixture: self._rewrite_manifest(
                fixture, lambda manifest: manifest["exports"][0].update({
                    "total_rows": 3,
                }),
            )),
            ("wrong sha256", lambda fixture: self._rewrite_manifest(
                fixture, lambda manifest: manifest["exports"][0].update({
                    "sha256": "0" * 64,
                }),
            )),
            ("wrong byte size", lambda fixture: self._rewrite_manifest(
                fixture, lambda manifest: manifest["exports"][0].update({
                    "size_bytes": manifest["exports"][0]["size_bytes"] + 1,
                }),
            )),
            ("CSV header mismatch", self._mutate_header),
            ("CSV row count mismatch", self._mutate_rows),
            ("unsafe filename", lambda fixture: self._rewrite_manifest(
                fixture, lambda manifest: manifest["exports"][0].update({
                    "filename": "../outside.csv",
                    "csv_filename": "../outside.csv",
                }),
            )),
            ("duplicate identity", lambda fixture: self._rewrite_manifest(
                fixture, lambda manifest: manifest["exports"].append(
                    dict(manifest["exports"][0]),
                ),
            )),
            ("final partition has wrong file despite staged_files",
             self._mutate_binding),
            ("empty placeholder despite staged_files",
             self._mutate_empty_placeholder),
            ("wrong final annotation", self._mutate_wrong_annotation),
            ("missing final annotation", self._mutate_missing_annotation),
            ("unconsumed export", self._mutate_unconsumed_export),
        )
        for label, mutate in cases:
            with self.subTest(case=label):
                fixture = self._fixture()
                mutate(fixture)
                evidence = self._builder()(
                    fixture["extracted"], fixture["project"],
                )
                self.assertEqual(evidence["status"], "failed")
                self.assertTrue(evidence["diagnostics"], label)
                self.assertEqual(evidence["runtime_status"], "not_run")

    def test_report_policy_still_blocks_a_false_staged_files_claim(self):
        from powerbi_import import migration_quality

        fixture = self._fixture()
        baseline = self._builder()(fixture["extracted"], fixture["project"])
        self.assertEqual(
            baseline["status"], "static_verified", baseline["diagnostics"],
        )
        self._mutate_empty_placeholder(fixture)
        assessment = SimpleNamespace(
            overall_score="GREEN",
            categories=[],
            to_dict=lambda: {"overall_score": "GREEN", "categories": []},
        )
        openability = SimpleNamespace(
            openable=True,
            blocking_issues=[],
            to_dict=lambda: {"openable": True, "checks": []},
        )
        scan = mock.Mock()
        scan.return_value.to_dict.return_value = {"gaps": []}
        with mock.patch.object(
                migration_quality, "run_assessment", return_value=assessment), \
                mock.patch.object(migration_quality, "scan_project", scan), \
                mock.patch.object(
                    migration_quality, "compare_report_tables",
                    return_value={
                        "summary": {"source_tables": 1, "tables_found": 1},
                    },
                ), \
                mock.patch.object(
                    migration_quality, "compare_report_interface",
                    return_value={
                        "filters": {"covered": True},
                        "parameters": {"covered": True},
                    },
                ), \
                mock.patch.object(
                    migration_quality, "check_openability",
                    return_value=openability,
                ):
            report = migration_quality.build_quality_report(
                fixture["extracted"], fixture["project"], REPORT_NAME,
                quality_policy="report",
            )
        self.assertEqual(report.status, "FAIL")
        self.assertTrue(report.blockers)
        diagnostics = report.hyper_import["diagnostics"]
        self.assertEqual(report.hyper_import["status"], "failed", diagnostics)
        self.assertTrue(any(
            "final M partition must have exactly one reachable File.Contents read"
            in diagnostic for diagnostic in diagnostics
        ), diagnostics)
        for diagnostic in diagnostics:
            self.assertIn(diagnostic, report.blockers)


class TestHyperSourceDecisionEvidence(unittest.TestCase):
    """Name the source an active extract retains without changing the gate.

    Every connection here uses reserved example names: no live connection is
    opened and nothing is compared with real data.
    """

    SENSITIVE = ("db.example.invalid", "synthetic_db", "synthetic_user")

    @staticmethod
    def _builder():
        from powerbi_import.migration_quality import build_hyper_import_evidence
        return build_hyper_import_evidence

    @staticmethod
    def _live_extract_xml(enabled):
        """A live PostgreSQL join, with one explicit Hyper extract object."""
        import xml.etree.ElementTree as ET

        datasource = ET.Element("datasource", {
            "name": "synthetic-live-source", "caption": "Synthetic Live Source",
        })
        root = ET.SubElement(datasource, "connection", {"class": "federated"})
        tree = ET.SubElement(root, "relation", {"type": "join", "join": "left"})
        clause = ET.SubElement(tree, "clause", {"type": "join"})
        comparison = ET.SubElement(clause, "expression", {"op": "="})
        ET.SubElement(comparison, "expression", {"op": "[Left].[ID]"})
        ET.SubElement(comparison, "expression", {"op": "[Right].[ID]"})
        for alias in ("Left", "Right"):
            named = ET.SubElement(root, "named-connection", {"name": alias + "-ref"})
            ET.SubElement(named, "connection", {
                "class": "postgres", "server": "db.example.invalid",
                "port": "5432", "dbname": "synthetic_db",
                "username": "synthetic_user",
            })
            relation = ET.SubElement(tree, "relation", {
                "type": "table", "name": alias, "connection": alias + "-ref",
                "table": "[public].[" + alias + "]",
            })
            columns = ET.SubElement(relation, "columns")
            ET.SubElement(columns, "column", {
                "name": alias + " field",
                "datatype": "integer" if alias == "Left" else "string",
            })
        native_records = ET.SubElement(root, "metadata-records")
        extract = ET.SubElement(datasource, "extract", {"enabled": enabled})
        hyper = ET.SubElement(extract, "connection", {
            "class": "hyper", "dbname": "Data/exact.hyper",
            "schema": "Extract", "tablename": "Materialized",
        })
        physical = ET.SubElement(hyper, "relation", {
            "name": "Extract", "type": "table",
            "table": "[Extract].[Materialized]",
        })
        physical_columns = ET.SubElement(physical, "columns")
        extract_records = ET.SubElement(hyper, "metadata-records")
        maps = ET.SubElement(hyper, "cols")
        for alias, datatype in (("Left", "integer"), ("Right", "string")):
            local, remote = alias + " field", alias.lower() + "_physical"
            ET.SubElement(datasource, "column", {
                "name": f"[{local}]", "datatype": datatype,
            })
            ET.SubElement(physical_columns, "column", {
                "name": local, "datatype": datatype,
            })
            for parent, table, source in (
                    (native_records, alias, alias + "_native"),
                    (extract_records, "Extract", remote)):
                record = ET.SubElement(parent, "metadata-record", {
                    "class": "column",
                })
                for tag, value in (("local-name", f"[{local}]"),
                                   ("remote-name", source),
                                   ("parent-name", f"[{table}]"),
                                   ("local-type", datatype)):
                    ET.SubElement(record, tag).text = value
            ET.SubElement(maps, "map", {
                "key": f"[{local}]", "value": f"[Extract].[{remote}]",
            })
        graph = ET.SubElement(datasource, "object-graph")
        obj = ET.SubElement(
            ET.SubElement(graph, "objects"), "object",
            {"id": "logical-101", "caption": "Logical object"})
        ET.SubElement(obj, "properties", {"context": ""}).append(deepcopy(tree))
        ET.SubElement(obj, "properties", {"context": "extract"}).append(
            deepcopy(physical))
        return datasource

    def _extracted(self, *, enabled="true"):
        from tableau_export.datasource_extractor import extract_datasource

        temp = _temporary_directory("hyper-source-decision-")
        self.addCleanup(temp.cleanup)
        workbook, _staged, _identity = _write_synthetic_package(
            temp.name, member="Data/exact.hyper")
        datasource = extract_datasource(self._live_extract_xml(enabled), workbook)
        project = os.path.join(temp.name, REPORT_NAME)
        os.makedirs(project)
        extracted = {
            "datasources": [datasource],
            "hyper_files": [{
                "path": "Data/exact.hyper", "filename": "exact.hyper",
            }],
        }
        return extracted, project

    def _bound_fixture(self):
        temp = _temporary_directory("hyper-source-decision-bound-")
        self.addCleanup(temp.cleanup)
        return _build_bound_fixture(temp.name)

    @staticmethod
    def _report(evidence):
        from powerbi_import.migration_quality import MigrationQualityReport
        return MigrationQualityReport(
            report_name=REPORT_NAME, assessment={}, parity={}, data={},
            interface={}, openability={}, status="WARN", hyper_import=evidence,
        )

    def test_active_extract_over_a_live_connection_names_the_retained_source(self):
        extracted, project = self._extracted(enabled="true")
        datasource = extracted["datasources"][0]
        self.assertIn("materialized_extract", datasource)
        self.assertEqual(datasource["connection"]["type"], "PostgreSQL")
        evidence = self._builder()(extracted, project)
        self.assertEqual(evidence["source_decisions"], [{
            "datasource": "Synthetic Live Source",
            "retained": "hyper_snapshot",
            "not_used": "original_connection",
            "connectors": ["PostgreSQL"],
        }])
        self.assertFalse(
            [item for item in evidence["diagnostics"] if item.startswith("Hint:")],
            evidence["diagnostics"])

    def test_decision_never_exposes_connection_details(self):
        extracted, project = self._extracted(enabled="true")
        serialized_source = json.dumps(extracted)
        for value in self.SENSITIVE:
            self.assertIn(value, serialized_source)
        evidence = self._builder()(extracted, project)
        self.assertTrue(evidence["source_decisions"])
        rendered = {
            "json": json.dumps(evidence, ensure_ascii=False, default=str),
            "html": self._report(evidence).render_body(),
        }
        for label, document in rendered.items():
            for value in self.SENSITIVE:
                with self.subTest(document=label, value=value):
                    self.assertNotIn(value, document)

    def test_decision_does_not_change_the_gate_outcome(self):
        fixture = self._bound_fixture()
        baseline = self._builder()(fixture["extracted"], fixture["project"])
        self.assertEqual(
            baseline["status"], "static_verified", baseline["diagnostics"])
        self.assertEqual(baseline["source_decisions"], [])

        extracted = deepcopy(fixture["extracted"])
        datasource = extracted["datasources"][0]
        datasource["materialized_extract"] = {"object_id": "synthetic-object"}
        datasource["connection_map"]["synthetic-live-ref"] = {
            "type": "PostgreSQL",
            "details": {"server": "db.example.invalid"},
        }
        evidence = self._builder()(extracted, fixture["project"])
        self.assertEqual(evidence["status"], "static_verified", evidence["diagnostics"])
        self.assertEqual(evidence["diagnostics"], [])
        self.assertEqual(
            [item["connectors"] for item in evidence["source_decisions"]],
            [["PostgreSQL"]])
        for key in ("exports", "bindings", "manifest_status", "runtime_status"):
            with self.subTest(key=key):
                self.assertEqual(evidence[key], baseline[key])

    def test_only_an_active_extract_over_a_non_hyper_connection_is_reported(self):
        builder = self._builder()
        cases = {
            "no extract": {"datasources": [{
                "name": "Plain", "connection": {"type": "PostgreSQL"},
                "connection_map": {"ref": {"type": "PostgreSQL"}},
                "tables": [],
            }]},
            "hyper connection only": {"datasources": [{
                "name": "Direct", "materialized_extract": {"object_id": "x"},
                "connection": {"type": "HYPER"},
                "connection_map": {"ref": {"type": "Hyper"}},
                "tables": [],
            }]},
            "unknown connection": {"datasources": [{
                "name": "Opaque", "materialized_extract": {"object_id": "x"},
                "connection": {"type": "Unknown", "details": {}},
                "tables": [],
            }]},
            "malformed records": {"datasources": [
                "text", None, {"name": "Odd", "materialized_extract": {"x": 1},
                               "connection": "PostgreSQL",
                               "connection_map": ["PostgreSQL"]},
            ]},
            "no datasources": {},
        }
        project = _temporary_directory("hyper-source-decision-empty-")
        self.addCleanup(project.cleanup)
        for label, extracted in cases.items():
            with self.subTest(case=label):
                self.assertEqual(
                    builder(extracted, project.name)["source_decisions"], [])

    def test_connectors_are_distinct_sorted_and_hyper_free(self):
        extracted = {"datasources": [{
            "name": "Mixed", "caption": "", "materialized_extract": {"object_id": "x"},
            "connection": {"type": "PostgreSQL", "details": {"server": "x"}},
            "connection_map": {
                "a": {"type": "PostgreSQL"}, "b": {"type": "excel"},
                "c": {"type": "DATAENGINE"}, "d": {"type": "  "},
                "e": {"type": None}, "f": "text",
            },
        }, {
            "name": "Second", "caption": "Second Caption",
            "materialized_extract": {"object_id": "y"},
            "connection_map": {"a": {"type": "Tableau Server"}},
        }]}
        project = _temporary_directory("hyper-source-decision-empty-")
        self.addCleanup(project.cleanup)
        decisions = self._builder()(extracted, project.name)["source_decisions"]
        self.assertEqual(
            [(item["datasource"], item["connectors"]) for item in decisions],
            [("Mixed", ["excel", "PostgreSQL"]),
             ("Second Caption", ["Tableau Server"])])

    def test_disabled_extract_with_a_packaged_snapshot_adds_one_conditional_hint(self):
        builder = self._builder()
        extracted, project = self._extracted(enabled="false")
        datasource = extracted["datasources"][0]
        self.assertIn("inactive_extract_context", datasource)
        self.assertNotIn("materialized_extract", datasource)

        evidence = builder(extracted, project)
        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(evidence["source_decisions"], [])
        hints = [item for item in evidence["diagnostics"] if item.startswith("Hint:")]
        self.assertEqual(len(hints), 1, evidence["diagnostics"])
        self.assertIn("'Synthetic Live Source'", hints[0])
        self.assertIn("If that snapshot belongs to the disabled extract", hints[0])
        self.assertIn("remove the extract and its .hyper", hints[0])
        self.assertIn("no live source is substituted automatically", hints[0])
        for value in self.SENSITIVE:
            self.assertNotIn(value, hints[0])

        baseline_source = deepcopy(extracted)
        baseline_source["datasources"][0].pop("inactive_extract_context")
        baseline = builder(baseline_source, project)
        self.assertEqual(baseline["status"], "failed")
        self.assertEqual(
            [item for item in evidence["diagnostics"] if item != hints[0]],
            baseline["diagnostics"])

    def test_hint_is_never_added_to_a_passing_or_unrelated_evidence(self):
        builder = self._builder()
        fixture = self._bound_fixture()
        extracted = deepcopy(fixture["extracted"])
        extracted["datasources"][0]["inactive_extract_context"] = {
            "selection": "native_no_enabled_extract",
        }
        passing = builder(extracted, fixture["project"])
        self.assertEqual(passing["status"], "static_verified", passing["diagnostics"])
        self.assertEqual(passing["diagnostics"], [])

        active, project = self._extracted(enabled="true")
        failing = builder(active, project)
        self.assertEqual(failing["status"], "failed")
        self.assertFalse(
            [item for item in failing["diagnostics"] if item.startswith("Hint:")])

        disabled, project = self._extracted(enabled="false")
        disabled["hyper_files"] = []
        disabled["datasources"].append({
            "name": "Broken", "connection": {"type": "Hyper"},
            "tables": [{"name": "Table"}],
        })
        unrelated = builder(disabled, project)
        self.assertEqual(unrelated["status"], "failed")
        self.assertTrue(unrelated["diagnostics"])
        self.assertFalse(
            [item for item in unrelated["diagnostics"] if item.startswith("Hint:")])

    def test_hint_reaches_the_generation_gate_error(self):
        from powerbi_import.errors import GenerationError

        extracted, project = self._extracted(enabled="false")
        with self.assertRaises(GenerationError) as raised:
            migrate._require_hyper_import(project, extracted=extracted)
        self.assertEqual(raised.exception.source, "hyper_import")
        self.assertIn("Hint:", str(raised.exception))

    def test_hint_lists_a_bounded_number_of_datasource_names(self):
        extracted, project = self._extracted(enabled="false")
        base = extracted["datasources"][0]
        extracted["datasources"] = [
            dict(base, name=f"ds-{index}", caption=f"Source {index}")
            for index in range(5)
        ]
        evidence = self._builder()(extracted, project)
        hint, = [item for item in evidence["diagnostics"] if item.startswith("Hint:")]
        self.assertIn("'Source 0', 'Source 1', 'Source 2' (+2 more)", hint)
        self.assertNotIn("Source 3", hint)

    def test_report_renders_the_decision_table_with_escaped_values(self):
        evidence = {
            "status": "static_verified", "runtime_status": "not_run",
            "diagnostics": [], "exports": [],
            "source_decisions": [{
                "datasource": '<b>Sales</b> & "Ops"',
                "retained": "hyper_snapshot",
                "not_used": "original_connection",
                "connectors": ["PostgreSQL", "<i>Excel</i>"],
            }, "not a record"],
        }
        body = self._report(evidence).render_body()
        self.assertIn('id="quality-hyper-source-decisions"', body)
        self.assertIn('data-i18n-en="Packaged Hyper snapshot (Import)"', body)
        self.assertIn('data-i18n-fr="Instantané Hyper empaqueté (Import)"', body)
        self.assertIn('data-i18n-en="Original source connection"', body)
        self.assertIn('data-i18n-fr="Connexion source d’origine"', body)
        self.assertIn("&lt;b&gt;Sales&lt;/b&gt; &amp; &quot;Ops&quot;", body)
        self.assertIn("PostgreSQL, &lt;i&gt;Excel&lt;/i&gt;", body)
        self.assertNotIn("<b>Sales</b>", body)
        self.assertNotIn("<i>Excel</i>", body)

    def test_report_omits_the_decision_table_without_decisions(self):
        stub = {
            "status": "not_applicable", "runtime_status": "not_run",
            "diagnostics": [], "exports": [],
            "verification_scope": "Fabric DirectLake uses separate evidence.",
        }
        for label, evidence in (
                ("fabric stub", stub),
                ("empty list", dict(stub, source_decisions=[])),
                ("not a list", dict(stub, source_decisions="PostgreSQL")),
                ("only malformed", dict(stub, source_decisions=[None, "x"]))):
            with self.subTest(case=label):
                body = self._report(evidence).render_body()
                self.assertNotIn("quality-hyper-source-decisions", body)
                self.assertIn('id="quality-hyper-import"', body)


class TestHyperMetadataPathContainment(unittest.TestCase):
    """Paths read from a project are compared lexically, never resolved remotely.

    ``os.path.realpath`` opens its argument, so resolving a crafted UNC value
    would make the verifier contact that host. The guard records any such
    attempt and refuses it, so these tests never reach a network.
    """

    REMOTE_SOURCES = (
        r"\\attacker.example\share\f.hyper",
        "//attacker.example/share/f.hyper",
        r"\\?\UNC\attacker.example\share\f.hyper",
        r"\\.\UNC\attacker.example\share\f.hyper",
    )
    REMOTE_DATA_DIR = r"\\attacker.example\share\Data"
    DATA_FOLDER_ISSUE = (
        "DataFolder does not resolve to the existing project Data directory")
    EXACT_FILE_ISSUE = (
        "final M File.Contents path does not resolve to the exact canonical "
        "manifest export file")

    def _fixture(self):
        temp = _temporary_directory("hyper-path-containment-")
        self.addCleanup(temp.cleanup)
        return _build_bound_fixture(temp.name)

    @staticmethod
    def _lexical(path):
        return ntpath.normcase(ntpath.normpath(path))

    @staticmethod
    def _data_folder_declaration(literal):
        return (
            'expression DataFolder = "' + literal.replace("\\", "\\\\")
            + '" meta [IsParameterQuery=true, Type="Text", '
            "IsParameterQueryRequired=true]\n"
        )

    def test_loose_identity_keys_do_not_resolve_remote_sources(self):
        from powerbi_import import migration_quality
        owners = (
            ("migrate", migrate._hyper_binding_identity_key),
            ("quality", migration_quality._local_hyper_identity_key),
        )
        for source in self.REMOTE_SOURCES:
            expected = ("local", self._lexical(source), "Extract", "Extract")
            for owner, key_of in owners:
                identity = {
                    "source_file": source, "schema": "Extract", "table": "Extract",
                }
                with self.subTest(source=source, owner=owner):
                    with _guard_remote_resolution() as attempts:
                        key = key_of(identity)
                    self.assertEqual(attempts, [])
                    if os.name == "nt":
                        self.assertEqual(key, expected)
                    else:
                        self.assertIn(key, (None, expected))

    def test_remote_data_folder_literal_is_rejected_without_resolution(self):
        from powerbi_import import migration_quality
        temp = _temporary_directory("hyper-data-folder-")
        self.addCleanup(temp.cleanup)
        definition = os.path.join(temp.name, "definition")
        data_dir = os.path.join(temp.name, "Data")
        os.makedirs(data_dir)
        for literal in (
                self.REMOTE_DATA_DIR, "//attacker.example/share/Data",
                r"\\?\UNC\attacker.example\share\Data"):
            _write(os.path.join(definition, "expressions.tmdl"),
                   self._data_folder_declaration(literal))
            with self.subTest(literal=literal):
                with _guard_remote_resolution() as attempts:
                    resolved, issue = migration_quality._hyper_data_folder(
                        definition, temp.name, data_dir)
                self.assertEqual(attempts, [])
                self.assertIsNone(resolved)
                self.assertEqual(issue, self.DATA_FOLDER_ISSUE)

    def test_operator_selected_remote_output_folder_still_matches(self):
        from powerbi_import import migration_quality
        temp = _temporary_directory("hyper-remote-output-")
        self.addCleanup(temp.cleanup)
        definition = os.path.join(temp.name, "definition")
        selected = r"\\Fileserver.example\Reports\Out\Data"
        _write(os.path.join(definition, "expressions.tmdl"),
               self._data_folder_declaration(r"\\fileserver.example\reports\Out\Data"))
        with mock.patch("os.path.isdir", return_value=True) as is_dir, \
                _guard_remote_resolution() as attempts:
            resolved, issue = migration_quality._hyper_data_folder(
                definition, temp.name, selected)
        self.assertEqual(attempts, [])
        self.assertIsNone(issue)
        self.assertEqual(resolved, self._lexical(selected))
        is_dir.assert_called_once_with(resolved)

    def test_remote_file_contents_literal_is_compared_without_resolution(self):
        from powerbi_import import migration_quality
        for source in self.REMOTE_SOURCES:
            tokens = migration_quality._tokenize_hyper_m('"' + source + '"')
            with self.subTest(source=source):
                with _guard_remote_resolution() as attempts:
                    resolved, via = migration_quality._hyper_m_path(
                        tokens, None, os.getcwd())
                self.assertEqual(attempts, [])
                self.assertEqual(via, "literal")
                self.assertEqual(resolved, self._lexical(source))

    def _tamper_data_folder(self, fixture):
        path = os.path.join(
            fixture["project"], f"{REPORT_NAME}.SemanticModel", "definition",
            "expressions.tmdl")
        _write(path, self._data_folder_declaration(self.REMOTE_DATA_DIR))

    def _tamper_file_contents(self, fixture):
        record = fixture["manifest"]["exports"][0]
        marker = 'DataFolder & "\\' + record["filename"] + '"'
        tampered = '"' + self.REMOTE_DATA_DIR + "\\" + record["filename"] + '"'
        for path, content in _tmdl_files_with_hyper_annotation(fixture["project"]):
            self.assertIn(marker, content)
            _write(path, content.replace(marker, tampered, 1))

    def _tamper_manifest_identity(self, fixture):
        with open(fixture["manifest_path"], encoding="utf-8") as stream:
            manifest = json.load(stream)
        manifest["exports"][0]["identity"] = {
            "source_file": self.REMOTE_SOURCES[0],
            "schema": "Extract",
            "table": "Physical Events",
        }
        _save_manifest(fixture, manifest)

    def test_tampered_remote_paths_fail_evidence_without_resolution(self):
        from powerbi_import.migration_quality import build_hyper_import_evidence
        cases = (
            ("DataFolder", self._tamper_data_folder, self.DATA_FOLDER_ISSUE),
            ("File.Contents", self._tamper_file_contents, self.EXACT_FILE_ISSUE),
            ("manifest identity", self._tamper_manifest_identity, None),
        )
        for label, tamper, expected_issue in cases:
            with self.subTest(tampered=label):
                fixture = self._fixture()
                baseline = build_hyper_import_evidence(
                    fixture["extracted"], fixture["project"])
                self.assertEqual(
                    baseline["status"], "static_verified", baseline["diagnostics"])
                tamper(fixture)
                with _guard_remote_resolution() as attempts:
                    evidence = build_hyper_import_evidence(
                        fixture["extracted"], fixture["project"])
                self.assertEqual(attempts, [])
                issues = list(evidence["diagnostics"]) + [
                    issue for binding in evidence["bindings"]
                    for issue in binding["diagnostics"]
                ]
                self.assertEqual(evidence["status"], "failed", issues)
                if expected_issue:
                    self.assertTrue(
                        any(expected_issue in issue for issue in issues), issues)


class TestHyperMetadataLinkContainment(unittest.TestCase):
    """A link planted in a project is recognised and refused, never followed.

    Following a link reaches wherever it points, which can be a remote host,
    exactly like a UNC path. Each guard records and refuses any call that opens,
    resolves or lists through the link, so these tests never reach a network.
    """

    DATA_FOLDER_ISSUE = TestHyperMetadataPathContainment.DATA_FOLDER_ISSUE
    EXACT_FILE_ISSUE = TestHyperMetadataPathContainment.EXACT_FILE_ISSUE
    DATA_LINK_ISSUE = "Project Data path must be a regular directory, not a link."
    FILE_LINK_ISSUE = "file is not a regular non-link artifact"
    EXPRESSIONS_LINK_ISSUE = (
        "expressions.tmdl is not a regular file; DataFolder is unresolved")

    @staticmethod
    def _evidence(fixture, project=None):
        from powerbi_import.migration_quality import build_hyper_import_evidence
        return build_hyper_import_evidence(
            fixture["extracted"], project or fixture["project"])

    @staticmethod
    def _issues(evidence):
        return list(evidence["diagnostics"]) + [
            issue for binding in evidence["bindings"]
            for issue in binding["diagnostics"]
        ]

    @staticmethod
    def _expressions_path(fixture):
        return os.path.join(
            fixture["project"], f"{REPORT_NAME}.SemanticModel", "definition",
            "expressions.tmdl")

    def _fixture(self):
        temp = _temporary_directory("hyper-link-containment-")
        self.addCleanup(temp.cleanup)
        fixture = _build_bound_fixture(temp.name)
        baseline = self._evidence(fixture)
        self.assertEqual(
            baseline["status"], "static_verified", baseline["diagnostics"])
        return fixture

    def _data_folder_through(self, fixture, literal):
        _write(
            self._expressions_path(fixture),
            TestHyperMetadataPathContainment._data_folder_declaration(literal))

    def test_data_folder_literal_through_a_link_is_not_followed(self):
        from powerbi_import import migration_quality
        fixture = self._fixture()
        alias = os.path.join(fixture["root"], "data-alias")
        make_directory_link(self, alias, fixture["data_dir"])
        self._data_folder_through(fixture, alias)
        with forbid_link_traversal(alias) as attempts:
            resolved, issue = migration_quality._hyper_data_folder(
                os.path.dirname(self._expressions_path(fixture)),
                fixture["project"], fixture["data_dir"])
            evidence = self._evidence(fixture)
        self.assertEqual(attempts, [])
        self.assertIsNone(resolved)
        self.assertEqual(issue, self.DATA_FOLDER_ISSUE)
        issues = self._issues(evidence)
        self.assertEqual(evidence["status"], "failed", issues)
        self.assertTrue(any(self.DATA_FOLDER_ISSUE in item for item in issues), issues)

    def test_file_contents_literal_through_a_link_is_not_followed(self):
        fixture = self._fixture()
        alias = os.path.join(fixture["root"], "data-alias")
        make_directory_link(self, alias, fixture["data_dir"])
        record = fixture["manifest"]["exports"][0]
        marker = 'DataFolder & "\\' + record["filename"] + '"'
        through_alias = '"' + os.path.join(alias, record["filename"]) + '"'
        for path, content in _tmdl_files_with_hyper_annotation(fixture["project"]):
            self.assertIn(marker, content)
            _write(path, content.replace(marker, through_alias, 1))
        with forbid_link_traversal(alias) as attempts:
            evidence = self._evidence(fixture)
        self.assertEqual(attempts, [])
        issues = self._issues(evidence)
        self.assertEqual(evidence["status"], "failed", issues)
        self.assertTrue(any(self.EXACT_FILE_ISSUE in item for item in issues), issues)

    def test_loose_identity_keys_do_not_follow_links(self):
        from powerbi_import import migration_quality
        temp = _temporary_directory("hyper-identity-link-")
        self.addCleanup(temp.cleanup)
        real = os.path.join(temp.name, "real")
        _write(os.path.join(real, "f.hyper"), b"synthetic Hyper placeholder")
        alias = os.path.join(temp.name, "alias")
        make_directory_link(self, alias, real)
        source = os.path.join(alias, "f.hyper")
        identity = {"source_file": source, "schema": "Extract", "table": "Extract"}
        expected = (
            "local", os.path.normcase(os.path.abspath(source)), "Extract", "Extract")
        owners = (
            ("migrate", migrate._hyper_binding_identity_key),
            ("quality", migration_quality._local_hyper_identity_key),
        )
        for owner, key_of in owners:
            with self.subTest(owner=owner):
                with forbid_link_traversal(alias) as attempts:
                    key = key_of(identity)
                self.assertEqual(attempts, [])
                self.assertEqual(key, expected)

    def test_a_data_directory_link_is_refused_without_being_entered(self):
        fixture = self._fixture()
        relocated = os.path.join(fixture["project"], "Data-relocated")
        os.rename(fixture["data_dir"], relocated)
        make_directory_link(self, fixture["data_dir"], relocated)
        with forbid_link_traversal(fixture["data_dir"]) as attempts:
            evidence = self._evidence(fixture)
        self.assertEqual(attempts, [])
        self.assertEqual(evidence["status"], "failed", evidence["diagnostics"])
        self.assertTrue(
            any(self.DATA_LINK_ISSUE in item for item in evidence["diagnostics"]),
            evidence["diagnostics"])
        self.assertEqual(evidence["exports"], [])
        self.assertFalse([
            binding for binding in evidence["bindings"]
            if binding.get("status") == "static_verified"
        ])

    def test_an_export_replaced_by_a_link_is_refused_without_being_read(self):
        fixture = self._fixture()
        export = _record_export_path(fixture)
        outside = os.path.join(fixture["root"], "outside-export")
        shutil.move(export, outside)
        make_file_link(self, export, outside)
        with forbid_link_traversal(export) as attempts:
            evidence = self._evidence(fixture)
        self.assertEqual(attempts, [])
        issues = self._issues(evidence)
        self.assertEqual(evidence["status"], "failed", issues)
        self.assertTrue(any(self.FILE_LINK_ISSUE in item for item in issues), issues)

    def test_a_linked_expressions_file_is_refused_without_being_opened(self):
        from powerbi_import import migration_quality
        fixture = self._fixture()
        expressions = self._expressions_path(fixture)
        outside = os.path.join(fixture["root"], "outside-expressions.tmdl")
        shutil.move(expressions, outside)
        make_file_link(self, expressions, outside)
        with forbid_link_traversal(expressions) as attempts:
            resolved, issue = migration_quality._hyper_data_folder(
                os.path.dirname(expressions), fixture["project"],
                fixture["data_dir"])
            evidence = self._evidence(fixture)
        self.assertEqual(attempts, [])
        self.assertIsNone(resolved)
        self.assertEqual(issue, self.EXPRESSIONS_LINK_ISSUE)
        issues = self._issues(evidence)
        self.assertEqual(evidence["status"], "failed", issues)
        self.assertTrue(
            any(self.EXPRESSIONS_LINK_ISSUE in item for item in issues), issues)

    def test_trusted_file_keys_name_one_plain_basename_and_never_resolve_it(self):
        from powerbi_import import migration_quality
        temp = _temporary_directory("hyper-trusted-keys-")
        self.addCleanup(temp.cleanup)
        unsafe_names = (
            "..\\x.csv", "sub/x.csv", "sub\\x.csv", ".", "..", "", "x\x00.csv",
            None, 5, "C:\\x.csv", "\\\\attacker.example\\share\\x.csv",
        )
        with mock.patch.object(
                os.path, "realpath", side_effect=AssertionError("must not resolve")):
            for name in unsafe_names:
                with self.subTest(name=name):
                    self.assertEqual(
                        migration_quality._hyper_trusted_file_keys(temp.name, name),
                        frozenset())
        outside = _write(os.path.join(temp.name, "outside"), b"synthetic")
        export = os.path.join(temp.name, "hyper_x.csv")
        make_file_link(self, export, outside)
        with forbid_link_traversal(export) as attempts:
            keys = migration_quality._hyper_trusted_file_keys(
                temp.name, "hyper_x.csv")
        self.assertEqual(attempts, [])
        self.assertIn(os.path.normcase(os.path.abspath(export)), keys)

    def test_a_project_reached_through_an_operator_link_still_verifies(self):
        fixture = self._fixture()
        alias = os.path.join(fixture["root"], "project-alias")
        make_directory_link(self, alias, fixture["project"])
        evidence = self._evidence(fixture, alias)
        self.assertEqual(
            evidence["status"], "static_verified", evidence["diagnostics"])
        self.assertTrue(evidence["bindings"])

    def test_the_sampling_evidence_does_not_open_a_linked_manifest(self):
        from powerbi_import import migration_quality
        fixture = self._fixture()
        manifest = fixture["manifest_path"]
        outside = os.path.join(fixture["root"], "outside-manifest.json")
        shutil.move(manifest, outside)
        make_file_link(self, manifest, outside)
        with forbid_link_traversal(manifest) as attempts:
            evidence = migration_quality._hyper_sampling_evidence(
                fixture["extracted"], fixture["project"])
        self.assertEqual(attempts, [])
        self.assertEqual(evidence["status"], "invalid", evidence["diagnostics"])
        self.assertTrue(any(
            "Hyper export manifest must not be a link" in item
            for item in evidence["diagnostics"]), evidence["diagnostics"])
        self.assertEqual(evidence["exports"], [])

    def test_the_sampling_evidence_does_not_enter_a_linked_data_directory(self):
        from powerbi_import import migration_quality
        fixture = self._fixture()
        relocated = os.path.join(fixture["project"], "Data-relocated")
        os.rename(fixture["data_dir"], relocated)
        make_directory_link(self, fixture["data_dir"], relocated)
        with forbid_link_traversal(fixture["data_dir"]) as attempts:
            evidence = migration_quality._hyper_sampling_evidence(
                fixture["extracted"], fixture["project"])
            unrelated = migration_quality._hyper_sampling_evidence(
                {}, fixture["project"])
        self.assertEqual(attempts, [])
        self.assertEqual(evidence["status"], "invalid", evidence["diagnostics"])
        self.assertTrue(any(
            "Project Data path must not be a link" in item
            for item in evidence["diagnostics"]), evidence["diagnostics"])
        self.assertEqual(unrelated["status"], "not_found")
        self.assertEqual(unrelated["diagnostics"], [])

    def _assert_data_scan_skips_the_link(self, make_link):
        from powerbi_import import migration_quality
        temp = _temporary_directory("hyper-data-scan-")
        self.addCleanup(temp.cleanup)
        project = os.path.join(temp.name, "Project")
        regular = os.path.join(project, "Data", "regular")
        _write(os.path.join(regular, "retained.hyper"), b"synthetic placeholder")
        detected = migration_quality._hyper_sampling_evidence({}, project)
        self.assertTrue(any(
            "manifest Data/hyper_exports.json is missing" in item
            for item in detected["diagnostics"]), detected["diagnostics"])

        shutil.rmtree(os.path.join(project, "Data"))
        outside = os.path.join(temp.name, "outside")
        _write(os.path.join(outside, "retained.hyper"), b"synthetic placeholder")
        link = os.path.join(project, "Data", "linked")
        os.makedirs(os.path.dirname(link))
        make_link(self, link, outside)
        with forbid_link_traversal(link) as attempts:
            skipped = migration_quality._hyper_sampling_evidence({}, project)
        self.assertEqual(attempts, [])
        self.assertEqual(skipped["status"], "not_found")
        self.assertEqual(skipped["diagnostics"], [])

    def test_the_data_scan_does_not_descend_into_a_linked_directory(self):
        self._assert_data_scan_skips_the_link(make_directory_link)

    def test_the_data_scan_does_not_descend_into_a_junction(self):
        self._assert_data_scan_skips_the_link(make_junction)

    def test_data_folder_spelled_through_the_operator_link_still_verifies(self):
        fixture = self._fixture()
        alias = os.path.join(fixture["root"], "project-alias")
        make_directory_link(self, alias, fixture["project"])
        self._data_folder_through(fixture, os.path.join(alias, "Data"))
        evidence = self._evidence(fixture, alias)
        self.assertEqual(
            evidence["status"], "static_verified", evidence["diagnostics"])
        self.assertTrue(evidence["bindings"])
