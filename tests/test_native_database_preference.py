"""Offline acceptance regressions for database-first DB + Hyper selection.

The original XML is deliberately complete: two native tables, their ownership
metadata, a join, and a separately declared materialized Hyper object. Native
and snapshot remote column names differ. Ignored snapshots contain invalid
bytes; retained snapshots are real SQLite databases exported by the real
pipeline, never successful-export mocks. No test refreshes a database/model.
"""

import copy
import csv
import hashlib
import io
import json
import os
import re
import socket
import sqlite3
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from contextlib import ExitStack, contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

# Match the importer tests' setup for legacy sibling imports (pbip_generator).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "powerbi_import"))

import migrate
from powerbi_import.errors import ConfigurationError, ExtractionError, GenerationError
from powerbi_import.import_to_powerbi import PowerBIImporter
from powerbi_import.migration_quality import (
    MigrationQualityReport,
    build_hyper_import_evidence,
)
from powerbi_import.openability import extract_m_partitions
from powerbi_import.tmdl_generator import generate_tmdl
from tableau_export import (
    datasource_extractor,
    extract_tableau_data,
    hyper_reader,
    m_query_builder,
)
from tableau_export.m_query_builder import generate_power_query_m
from tests.test_tmdl_m_conversion import EmittedScalarM

REPORT = "Native"
SERVER = "database.example.invalid"
DATABASE = "migration_fixture_db"
USER = "migration_fixture_user"
SCHEMA = "reporting"
IGNORED = "Data/native/shared.hyper"
RETAINED = "Data/snapshot/shared.hyper"  # Same basename is intentional.
UNREADABLE_HYPER = b"not a readable Hyper database; native policy must skip even its header"
SNAPSHOT_TABLE = "SnapshotRows"
NATIVE_TABLES = (
    ("Orders Model", "orders-ref", "fact_orders", (
        ("order_id", "integer"),
        ("order_region_id", "integer"),
        ("amount", "real"),
    )),
    ("Regions Model", "regions-ref", "dim_regions", (
        ("region_id", "integer"),
        ("region_name", "string"),
    )),
)
NATIVE_FIELDS = tuple(
    (alias, name, datatype)
    for alias, _reference, _physical, columns in NATIVE_TABLES
    for name, datatype in columns
)
DATABASE_CASES = (
    ("postgres", "PostgreSQL", "5432", "PostgreSQL.Database", "directQuery"),
    ("sqlserver", "SQL Server", "1433", "Sql.Database", "directQuery"),
    ("mysql", "MySQL", "3306", "MySQL.Database", "import"),
)


def _record(parent, local, remote, alias, datatype, ordinal):
    record = ET.SubElement(parent, "metadata-record", {"class": "column"})
    for tag, text in (
        ("local-name", f"[{local}]"),
        ("remote-name", remote),
        ("parent-name", f"[{alias}]"),
        ("local-type", datatype),
        ("ordinal", str(ordinal)),
        ("contains-null", "true"),
    ):
        ET.SubElement(record, tag).text = text
    return record


def _native_database_xml(xml_class="postgres", *, member=IGNORED,
                         name="native-db", enabled="true", direct=False,
                         snapshot=True, filters=False):
    """A complete original join patterned on the source-decision XML fixture."""
    port = next(case[2] for case in DATABASE_CASES if case[0] == xml_class)
    datasource = ET.Element("datasource", {
        "name": name, "caption": "Synthetic Native Database",
    })
    attributes = {
        "class": xml_class, "server": SERVER, "port": port,
        "dbname": DATABASE, "username": USER, "schema": SCHEMA,
    }
    connection = ET.SubElement(
        datasource, "connection", attributes if direct else {"class": "federated"})
    if not direct:
        named = ET.SubElement(connection, "named-connections")
        for alias, reference, _physical, _columns in NATIVE_TABLES:
            wrapper = ET.SubElement(named, "named-connection", {
                "name": reference, "caption": alias,
            })
            ET.SubElement(wrapper, "connection", dict(attributes))
    tree = ET.SubElement(connection, "relation", {"type": "join", "join": "left"})
    clause = ET.SubElement(tree, "clause", {"type": "join"})
    comparison = ET.SubElement(clause, "expression", {"op": "="})
    ET.SubElement(comparison, "expression", {"op": "[Orders Model].[order_region_id]"})
    ET.SubElement(comparison, "expression", {"op": "[Regions Model].[region_id]"})
    for alias, reference, physical, fields in NATIVE_TABLES:
        relation_attributes = {
            "type": "table", "name": alias, "table": f"[{SCHEMA}].[{physical}]",
        }
        if not direct:
            relation_attributes["connection"] = reference
        relation = ET.SubElement(tree, "relation", relation_attributes)
        columns = ET.SubElement(relation, "columns")
        for ordinal, (column, datatype) in enumerate(fields):
            ET.SubElement(columns, "column", {
                "name": column, "datatype": datatype, "ordinal": str(ordinal),
            })
    records = ET.SubElement(connection, "metadata-records")
    maps = ET.SubElement(connection, "cols")
    for ordinal, (alias, local, datatype) in enumerate(NATIVE_FIELDS):
        _record(records, local, local, alias, datatype, ordinal)
        ET.SubElement(maps, "map", {
            "key": f"[{local}]", "value": f"[{alias}].[{local}]",
        })
        ET.SubElement(datasource, "column", {
            "name": f"[{local}]", "datatype": datatype,
            "role": "measure" if local == "amount" else "dimension",
        })

    graph = ET.SubElement(datasource, "object-graph")
    obj = ET.SubElement(ET.SubElement(graph, "objects"), "object", {
        "id": "native-object-1", "caption": "Snapshot Model",
    })
    ET.SubElement(obj, "properties", {"context": ""}).append(copy.deepcopy(tree))
    if snapshot:
        extract_attributes = {} if enabled is None else {"enabled": enabled}
        extract = ET.SubElement(datasource, "extract", extract_attributes)
        hyper = ET.SubElement(extract, "connection", {
            "class": "hyper", "dbname": member,
            "schema": "main", "tablename": SNAPSHOT_TABLE,
        })
        physical = ET.SubElement(hyper, "relation", {
            "type": "table", "name": "Extract",
            "table": f"[main].[{SNAPSHOT_TABLE}]",
        })
        columns = ET.SubElement(physical, "columns")
        records = ET.SubElement(hyper, "metadata-records")
        maps = ET.SubElement(hyper, "cols")
        for ordinal, (_alias, local, datatype) in enumerate(NATIVE_FIELDS):
            remote = "snapshot_" + local
            ET.SubElement(columns, "column", {
                "name": local, "datatype": datatype, "ordinal": str(ordinal),
            })
            _record(records, local, remote, "Extract", datatype, ordinal)
            ET.SubElement(maps, "map", {
                "key": f"[{local}]", "value": f"[Extract].[{remote}]",
            })
        ET.SubElement(obj, "properties", {"context": "extract"}).append(
            copy.deepcopy(physical))
    if filters:
        native_filter = ET.SubElement(datasource, "filter", {
            "class": "categorical", "column": "[region_name]",
        })
        ET.SubElement(native_filter, "groupfilter", {
            "function": "member", "level": "[region_name]", "member": '"Native North"',
        })
        native_security = ET.SubElement(datasource, "user-filter", {
            "name": "[NativeAccess]", "column": "[region_name]",
        })
        ET.SubElement(native_security, "member", {
            "user": "reader@example.invalid", "value": "Native North",
        })
        if snapshot:
            stale_filter = ET.SubElement(hyper, "filter", {
                "class": "categorical", "column": "[amount]",
            })
            ET.SubElement(stale_filter, "groupfilter", {
                "function": "member", "level": "[amount]", "member": '"SNAPSHOT_ONLY"',
            })
            stale_security = ET.SubElement(hyper, "user-filter", {
                "name": "[SnapshotAccess]", "column": "[order_id]",
            })
            ET.SubElement(stale_security, "member", {
                "user": "snapshot-reader@example.invalid", "value": "999",
            })
    return datasource


def _replace_original_connections(datasource, kind, *, only=None):
    """Change original sources only, preserving the explicit Hyper declaration."""
    attributes = {
        "excel-direct": {
            "class": "excel-direct", "directory": "Data", "filename": "original.xlsx",
        },
        "textscan": {
            "class": "textscan", "directory": "Data", "filename": "original.csv",
            "separator": ",", "charset": "utf-8",
        },
        "sqlproxy": {
            "class": "sqlproxy", "server": "tableau.example.invalid",
            "dbname": "unresolved_published_fixture", "port": "443",
        },
    }[kind]
    root = datasource.find("./connection")
    for wrapper in root.findall(".//named-connection"):
        if only is not None and wrapper.get("name") != only:
            continue
        connection = wrapper.find("./connection")
        connection.attrib.clear()
        connection.attrib.update(attributes)
        for relation in datasource.findall(".//relation"):
            if relation.get("connection") != wrapper.get("name"):
                continue
            if kind == "excel-direct":
                relation.set("table", "[" + relation.get("name") + "$]")
            elif kind == "textscan":
                relation.set("table", "[original#csv]")
    if kind == "sqlproxy":
        ET.SubElement(datasource, "repository-location", {
            "id": "published-fixture", "name": "Published Fixture",
            "path": "/datasources/published-fixture", "site": "synthetic",
        })
    return datasource


def _set_native_remotes(datasource, remotes, *, captions=None):
    """Keep complete native metadata/maps; never rewrite snapshot provenance.

    Omit redundant relation-local lists: authoritative local/remote metadata
    and qualified maps fully describe the columns, including physical swaps.
    """
    connection = datasource.find("./connection")
    for record in connection.findall("./metadata-records/metadata-record"):
        local = record.findtext("local-name").strip("[]")
        record.find("remote-name").text = remotes.get(local, local)
    for mapping in connection.findall("./cols/map"):
        local = mapping.get("key").strip("[]")
        alias = next(owner for owner, name, _datatype in NATIVE_FIELDS if name == local)
        mapping.set("value", f"[{alias}].[{remotes.get(local, local)}]")
    tree = connection.find("./relation")
    for relation in tree.findall(".//relation[@type='table']"):
        for columns in relation.findall("./columns"):
            relation.remove(columns)
    for properties in datasource.findall(".//properties[@context='']"):
        for relation in properties.findall("./relation"):
            properties.remove(relation)
        properties.append(copy.deepcopy(tree))
    for column in datasource.findall("./column"):
        local = column.get("name").strip("[]")
        if local in (captions or {}):
            column.set("caption", captions[local])
    return datasource


_M_NAME = r'(?:#"(?:[^"]|"")*"|[A-Za-z_]\w*)'
_M_STRING = r'"(?:[^"]|"")*"'


def _native_column_operations(query):
    """Trace the returned emitted M chain, not disconnected matching text.

    This fail-closed structural reader handles only our generated projection,
    rename and numeric AddColumn fixtures. It is not a Power Query runtime.
    """
    before, separator, returned = query.rpartition("\nin\n")
    if not separator:
        raise AssertionError("Expected a complete generated let/in query")
    assignments = {}
    for line in before.splitlines():
        line = line.strip().removesuffix(",")
        if not line or line == "let" or line.startswith("//"):
            continue
        match = re.fullmatch(rf"({_M_NAME})\s*=\s*(.+)", line)
        if match is None or match[1] in assignments:
            raise AssertionError("Unsupported or duplicate M assignment: " + line)
        assignments[match[1]] = match[2]
    operations, seen = [], set()
    step = returned.strip()
    while step in assignments and step not in seen:
        seen.add(step)
        expression = assignments[step]
        if re.fullmatch(_M_NAME, expression):
            step = expression
            continue
        match = re.fullmatch(
            rf"Table\.(SelectColumns|RenameColumns)\(({_M_NAME}),\s*(\{{.+\}})\)",
            expression)
        if match:
            kind, step, literal = match.groups()
            item = _M_STRING
            if kind == "RenameColumns":
                item = r"\{\s*" + _M_STRING + r"\s*,\s*" + _M_STRING + r"\s*\}"
            grammar = r"\{\s*" + item + r"(?:\s*,\s*" + item + r")*\s*\}"
            if re.fullmatch(grammar, literal) is None:
                raise AssertionError("Non-strict M column operation: " + expression)
            strings = [value.replace('""', '"')
                       for value in re.findall(r'"((?:[^"]|"")*)"', literal)]
            values = strings if kind == "SelectColumns" else list(
                zip(strings[::2], strings[1::2]))
            operations.append((kind, values))
            continue
        match = re.fullmatch(
            rf"Table\.AddColumn\(({_M_NAME}),\s*({_M_STRING}),\s*each (.+),\s*"
            r"(type number|Int64\.Type)\)", expression)
        if match:
            step, name, scalar, datatype = match.groups()
            operations.append(("AddColumn", name[1:-1].replace('""', '"'),
                               scalar, datatype))
            continue
        if re.fullmatch(rf"{_M_NAME}\{{\[.+\]\}}\[Data\]", expression):
            return list(reversed(operations))
        raise AssertionError("Unsupported reachable native M step: " + expression)
    raise AssertionError("Missing or cyclic returned M step: " + step)


def _apply_native_column_operations(operations, row):
    """Apply only the emitted strict name operations to a synthetic row."""
    row = dict(row)
    for kind, values in operations:
        if kind == "SelectColumns":
            if len(values) != len(set(values)):
                raise AssertionError("Duplicate physical projection")
            row = {name: row[name] for name in values}  # Missing fields must fail.
        elif kind == "RenameColumns":
            for old, new in values:
                if old not in row:
                    raise KeyError(old)
                if old != new and new in row:
                    raise ValueError("Rename collides with existing column: " + new)
                row = {new if name == old else name: value for name, value in row.items()}
        else:
            raise AssertionError("Unsupported synthetic name operation: " + kind)
    return row


def _snapshot_only_xml(member=RETAINED, *, name="snapshot-only",
                       alias="Retained Snapshot"):
    datasource = ET.Element("datasource", {"name": name, "caption": alias})
    root = ET.SubElement(datasource, "connection", {"class": "federated"})
    named = ET.SubElement(root, "named-connection", {"name": "snapshot-ref"})
    ET.SubElement(named, "connection", {"class": "hyper", "dbname": member})
    relation = ET.SubElement(root, "relation", {
        "name": alias, "type": "table", "connection": "snapshot-ref",
        "table": f"[main].[{SNAPSHOT_TABLE}]",
    })
    columns = ET.SubElement(relation, "columns")
    records = ET.SubElement(root, "metadata-records")
    maps = ET.SubElement(root, "cols")
    for ordinal, (local, datatype) in enumerate((("id", "integer"), ("label", "string"))):
        ET.SubElement(columns, "column", {
            "name": local, "datatype": datatype, "ordinal": str(ordinal),
        })
        _record(records, local, local, alias, datatype, ordinal)
        ET.SubElement(maps, "map", {
            "key": f"[{local}]", "value": f"[{alias}].[{local}]",
        })
        ET.SubElement(datasource, "column", {
            "name": f"[{local}]", "datatype": datatype, "role": "dimension",
        })
    return datasource


@contextmanager
def _hyper_io_guard(ignored_members=(), *, no_hyper=False):
    """Record actual ZIP reads and fail even if a forbidden call is swallowed."""
    ignored = set(ignored_members)
    reads, forbidden = [], []
    real_open = zipfile.ZipFile.open

    def checked_open(archive, member, mode="r", *args, **kwargs):
        name = member.filename if isinstance(member, zipfile.ZipInfo) else member
        if mode == "r":
            reads.append(name)
            if name in ignored:
                forbidden.append(("ZIP.open", name))
                raise AssertionError("Selected-out Hyper bytes must not be opened: " + name)
        return real_open(archive, member, mode, *args, **kwargs)

    def fail(label):
        def unexpected(*args, **kwargs):
            forbidden.append((label, "called"))
            raise AssertionError("Database-only migration must not call " + label)
        return unexpected

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(zipfile.ZipFile, "open", checked_open))
        if no_hyper:
            for module, attribute in (
                (extract_tableau_data, "read_hyper_from_twbx"),
                (hyper_reader, "read_hyper_from_twbx"),
                (hyper_reader, "read_hyper"),
                (hyper_reader, "_read_hyper_api"),
                (hyper_reader, "_read_hyper_sqlite"),
                (migrate, "_hyper_to_csv_files"),
                (sqlite3, "connect"),
                (extract_tableau_data.TableauExtractor, "_extract_hyper_sample_rows"),
            ):
                stack.enter_context(mock.patch.object(
                    module, attribute, side_effect=fail(attribute)))
        try:
            yield reads
        finally:
            if forbidden:
                raise AssertionError("Forbidden Hyper I/O was attempted: " + repr(forbidden))


class NativeDatabaseFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ndb_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ))
        for name in ("TTPBI_HYPER_EXPORT_MAX_ROWS", "TTPBI_UPLOAD_ROOT"):
            os.environ.pop(name, None)
        stack.enter_context(mock.patch.object(migrate, "_upload_write_root", None))
        # Reader unavailability is real. SQLite fixtures still use real SQLite
        # readers/exporters; no Hyper API success is supplied by a double.
        stack.enter_context(mock.patch.dict(sys.modules, {"tableauhyperapi": None}))
        network_attempts = []

        def network_forbidden(*args, **kwargs):
            network_attempts.append("socket")
            raise AssertionError("Policy acceptance tests must remain offline")

        for owner, attribute in (
            (socket, "create_connection"), (socket, "getaddrinfo"),
            (socket, "gethostbyname"), (socket.socket, "connect"),
            (socket.socket, "connect_ex"),
        ):
            stack.enter_context(mock.patch.object(
                owner, attribute, side_effect=network_forbidden))
        stack.callback(self.assertEqual, network_attempts, [])

    def _sqlite_payload(self, tag, *, materialized=False, count=23):
        path = str(self.root / (tag + ".hyper"))
        columns = (
            [("snapshot_" + local, datatype) for _alias, local, datatype in NATIVE_FIELDS]
            if materialized else [("id", "integer"), ("label", "string")]
        )
        sql_types = {"integer": "INTEGER", "real": "REAL", "string": "TEXT"}
        connection = sqlite3.connect(path)
        try:
            definition = ", ".join(
                '"' + name + '" ' + sql_types[datatype] for name, datatype in columns)
            connection.execute(f'CREATE TABLE "{SNAPSHOT_TABLE}" ({definition})')
            rows = (
                [(index, index % 3, index + 0.5, index % 3, "Native North")
                 for index in range(count)]
                if materialized else
                [(index, f"synthetic row,{index}") for index in range(count)]
            )
            placeholders = ",".join("?" for _ in columns)
            connection.executemany(
                f'INSERT INTO "{SNAPSHOT_TABLE}" VALUES ({placeholders})', rows)
            connection.commit()
        finally:
            connection.close()
        return Path(path).read_bytes()

    def _package(self, tag, datasources, members, *, worksheet=False):
        root = ET.Element("workbook", {"version": "2024.1"})
        parent = ET.SubElement(root, "datasources")
        for datasource in datasources:
            parent.append(copy.deepcopy(datasource))
        if worksheet:
            sheet = ET.SubElement(ET.SubElement(root, "worksheets"), "worksheet", {
                "name": "Native overview",
            })
            table = ET.SubElement(sheet, "table")
            view = ET.SubElement(table, "view")
            dependencies = ET.SubElement(view, "datasource-dependencies", {
                "datasource": datasources[0].get("name"),
            })
            for name, datatype, role in (
                ("region_name", "string", "dimension"), ("amount", "real", "measure"),
            ):
                ET.SubElement(dependencies, "column", {
                    "name": f"[{name}]", "datatype": datatype, "role": role,
                })
            ds_name = datasources[0].get("name")
            ET.SubElement(table, "cols").text = f"[{ds_name}].[none:region_name:nk]"
            ET.SubElement(table, "rows").text = f"[{ds_name}].[sum:amount:qk]"
            pane = ET.SubElement(ET.SubElement(table, "panes"), "pane")
            ET.SubElement(pane, "mark", {"class": "Bar"})
        path = self.root / (tag + ".twbx")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Workbook.twb", ET.tostring(root, encoding="utf-8"))
            for member, payload in members.items():
                archive.writestr(member, payload)
        return str(path)

    def _extract(self, workbook, tag):
        metadata = self.root / "metadata" / tag
        extractor = extract_tableau_data.TableauExtractor(
            workbook, output_dir=str(metadata), hyper_max_rows=1)
        with redirect_stdout(io.StringIO()):
            self.assertTrue(extractor.extract_all())
        extracted = {
            key: json.loads((metadata / (key + ".json")).read_text(encoding="utf-8"))
            for key in extractor.EXTRACTION_OUTPUT_TYPES
        }
        self.assertEqual(extracted["datasources"], extractor.workbook_data["datasources"])
        return str(metadata), extracted

    def _generate(self, metadata, extracted, tag, *, mode=None, importer=False):
        parent = self.root / "projects" / tag
        project = parent / REPORT
        options = {} if mode is None else {"model_mode": mode}
        with redirect_stdout(io.StringIO()):
            if importer:
                PowerBIImporter(source_dir=metadata).import_all(
                    report_name=REPORT, output_dir=str(parent), parameterize=False,
                    output_format="pbip", **options)
                self.assertTrue((project / (REPORT + ".pbip")).is_file())
            else:
                extra = copy.deepcopy(extracted)
                extra["_datasources"] = extra["datasources"]
                extra["_worksheets"] = extra["worksheets"]
                stats = generate_tmdl(
                    copy.deepcopy(extracted["datasources"]), REPORT, extra,
                    str(project / (REPORT + ".SemanticModel")),
                    parameterize=False, **options)
                self.assertEqual(stats["source_issues"], [])
        return str(project)

    def _post(self, workbook, project, metadata, **options):
        with redirect_stdout(io.StringIO()):
            migrate._process_twbx_post_generation(
                workbook, project, REPORT, extract_dir=metadata,
                hyper_export_format="csv", output_format="pbip", quiet=True,
                **options)

    def _tables(self, project):
        directory = Path(project) / (REPORT + ".SemanticModel") / "definition" / "tables"
        self.assertTrue(directory.is_dir())
        tables = {}
        for path in directory.glob("*.tmdl"):
            text = path.read_text(encoding="utf-8")
            declaration = re.search(r"(?m)^table\s+(.+)\s*$", text)
            self.assertIsNotNone(declaration, str(path))
            tables[declaration.group(1).strip().strip("'")] = text
        self.assertTrue(tables)
        return tables

    def _assert_native_datasource(self, datasource, *, member=IGNORED, workbook=None,
                                  connector="PostgreSQL"):
        selection = datasource["source_selection"]
        self.assertEqual(selection["policy"], "prefer_native_database")
        self.assertEqual(selection["selected"], "native_database")
        self.assertEqual(selection["hyper_members"], [member])
        self.assertEqual(selection["workbook"], os.path.abspath(workbook) if workbook else "")
        self.assertEqual(datasource["hyper_source_members"], [member])
        self.assertEqual(datasource["connection"]["type"], connector)
        self.assertNotIn("materialized_extract", datasource)
        self.assertNotIn("hyper_extract_metadata", datasource)
        self.assertEqual({table["name"] for table in datasource["tables"]},
                         {spec[0] for spec in NATIVE_TABLES})
        for table in datasource["tables"]:
            self.assertNotIn("hyper_identity", table)
            self.assertNotIn("hyper_extract_metadata", table)
            self.assertEqual(table["connection_details"]["type"], connector)
            expected = next(spec for spec in NATIVE_TABLES if spec[0] == table["name"])
            self.assertEqual(
                [(column["name"], column["datatype"]) for column in table["columns"]],
                list(expected[3]))
        self.assertEqual(datasource["col_local_name_map"], {
            local: alias for alias, local, _datatype in NATIVE_FIELDS
        })
        self.assertEqual(datasource["col_type_map"], {
            local: datatype for _alias, local, datatype in NATIVE_FIELDS
        })
        self.assertEqual(datasource["relationships"], [{
            "type": "left",
            "left": {"table": "Orders Model", "column": "order_region_id"},
            "right": {"table": "Regions Model", "column": "region_id"},
        }])
        self.assertNotIn("extract_context_provenance", datasource["relation_metadata"])

    def _assert_native_m(self, project, *, case=DATABASE_CASES[0], mode=None,
                         captions=None):
        _xml_class, _connector, port, function, auto_mode = case
        expected_mode = mode or auto_mode
        endpoint = SERVER if function == "Sql.Database" else SERVER + ":" + port
        tables = self._tables(project)
        for alias, _reference, physical, columns in NATIVE_TABLES:
            text = tables[alias]
            self.assertIn(f'{function}("{endpoint}", "{DATABASE}")', text)
            self.assertIn(f'[Schema="{SCHEMA}", Item="{physical}"]', text)
            self.assertNotIn(f'Item="{alias}"', text)
            self.assertEqual(re.findall(r"(?m)^\s*mode:\s+(\w+)\s*$", text), [expected_mode])
            for column, _datatype in columns:
                terminal = (captions or {}).get(column, column)
                self.assertRegex(
                    text, r"(?m)^\s*sourceColumn:\s+" + re.escape(terminal) + r"\s*$")
            for forbidden in (
                "snapshot_", "Migration_HyperIdentity", "Migration.HyperExportRequired",
                "SourceBinding.Error", "#table(", "File.Contents(", "otherwise",
                "Csv.Document", "Parquet.Document",
            ):
                self.assertNotIn(forbidden, text)
        relationships = (
            Path(project) / (REPORT + ".SemanticModel") / "definition" / "relationships.tmdl"
        ).read_text(encoding="utf-8")
        self.assertIn("'Orders Model'.order_region_id", relationships)
        self.assertIn("'Regions Model'.region_id", relationships)

    def _assert_ignored_inventory(self, extracted, member=IGNORED):
        entry, = [item for item in extracted["hyper_files"] if item["path"] == member]
        self.assertEqual(entry["selection"], "native_database")
        self.assertGreater(entry["size_bytes"], 0)
        for key in (
            "format", "tables", "sample_rows", "hyper_reader_tables",
            "hyper_reader_format", "actual_row_count", "estimated_row_count",
        ):
            self.assertNotIn(key, entry)

    def _assert_no_exports(self, project):
        self.assertFalse((Path(project) / "Data" / "hyper_exports.json").exists())
        self.assertEqual([
            str(path.relative_to(project)) for path in Path(project).rglob("*")
            if path.is_file() and path.suffix.casefold() in {".hyper", ".csv", ".parquet"}
        ], [])

    def _assert_decision_privacy(self, evidence):
        report = MigrationQualityReport(
            report_name=REPORT, assessment={}, parity={}, data={}, interface={},
            openability={}, status="WARN", hyper_import=evidence)
        html = report.render_body()
        if evidence["source_decisions"]:
            self.assertIn('id="quality-hyper-source-decisions"', html)
        if any(decision["retained"] == "native_database"
               for decision in evidence["source_decisions"]):
            self.assertIn('data-i18n-en="Original database connection"', html)
        for surface, text in (
            ("source decisions JSON", json.dumps(evidence["source_decisions"])),
            ("evidence JSON", json.dumps(evidence, ensure_ascii=False)),
            ("quality HTML", html),
        ):
            for value in (SERVER, DATABASE, USER, "tableau.example.invalid",
                          "unresolved_published_fixture", "reader@example.invalid"):
                with self.subTest(surface=surface, value=value):
                    self.assertNotIn(value, text)

    def _assert_native_gate(self, project, metadata, extracted, connector="PostgreSQL"):
        # Exercise the real metadata-loading consumer, not just an in-memory
        # dictionary that the extraction producer never persisted.
        evidence = migrate._require_hyper_import(project, extract_dir=metadata)
        self.assertEqual(evidence["status"], "not_applicable", evidence["diagnostics"])
        self.assertEqual(evidence["runtime_status"], "not_run")
        self.assertEqual(evidence["diagnostics"], [])
        self.assertEqual(evidence["exports"], [])
        self.assertEqual(evidence["bindings"], [])
        self.assertEqual(evidence["source_decisions"], [{
            "datasource": "Synthetic Native Database",
            "retained": "native_database", "not_used": "hyper_snapshot",
            "connectors": [connector],
        }])
        self.assertEqual(evidence, build_hyper_import_evidence(extracted, project))
        self._assert_decision_privacy(evidence)
        return evidence

    def _manifest(self, project):
        path = Path(project) / "Data" / "hyper_exports.json"
        self.assertTrue(path.is_file())
        return json.loads(path.read_text(encoding="utf-8"))

    def _assert_real_export(self, project, record, *, member, count, columns):
        self.assertEqual(record["identity"]["archive_member"], member)
        self.assertEqual(record["identity"]["schema"], "main")
        self.assertEqual(record["identity"]["table"], SNAPSHOT_TABLE)
        self.assertEqual(record["format"], "csv")
        self.assertEqual(record["columns"], columns)
        self.assertEqual(record["row_count"], count)
        self.assertEqual(record["total_rows"], count)
        self.assertIs(record["sampled"], False)
        self.assertIs(record["truncated"], False)
        self.assertIs(record["full_extract_verified"], True)
        path = Path(project) / "Data" / record["filename"]
        payload = path.read_bytes()
        self.assertEqual(record["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(record["size_bytes"], len(payload))
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows[0], columns)
        self.assertEqual(len(rows) - 1, count)
        return rows


class TestNativeDatabaseBindingIssues(NativeDatabaseFixture):
    def test_non_database_emitters_are_not_promoted_by_a_server_attribute(self):
        for kind in ("CSV", "Excel", "Hyper", "DATAENGINE", "Tableau Server",
                     "sqlproxy", "Unknown", "Custom SQL", "Web", "OData"):
            with self.subTest(connector=kind):
                self.assertIsNone(m_query_builder.native_database_binding_issues({
                    "type": kind, "details": {"server": SERVER, "database": DATABASE},
                }))

    def test_complete_metadata_does_not_require_credentials(self):
        cases = (
            ("SQL Server", {"server": SERVER, "database": DATABASE}),
            ("PostgreSQL", {"server": SERVER, "database": DATABASE}),
            ("  pOsTgReSqL ", {"server": SERVER, "database": DATABASE}),
            ("MySQL", {"server": SERVER, "database": DATABASE}),
            ("ClickHouse", {"server": SERVER, "database": DATABASE}),
            ("Starburst", {"server": SERVER, "catalog": "fixture_catalog", "schema": SCHEMA}),
            ("Oracle", {"server": SERVER, "service": "fixture_service"}),
            ("Snowflake", {"server": SERVER, "database": DATABASE, "warehouse": "fixture_wh"}),
            ("BigQuery", {"project": "fixture_project", "dataset": "fixture_dataset"}),
            ("Databricks", {
                "server": SERVER, "http_path": "/sql/fixture",
                "catalog": "fixture_catalog", "schema": SCHEMA,
            }),
        )
        for kind, details in cases:
            with self.subTest(connector=kind):
                self.assertEqual(m_query_builder.native_database_binding_issues({
                    "type": kind, "details": details,
                }), [])
                self.assertNotIn("username", details)
                self.assertNotIn("password", details)

    def test_incomplete_database_metadata_reports_exact_missing_keys(self):
        for kind in ("PostgreSQL", "SQL Server", "MySQL"):
            for value in ("", " \t ", None, 1433):
                with self.subTest(connector=kind, value=value):
                    self.assertEqual(m_query_builder.native_database_binding_issues({
                        "type": kind, "details": {"server": value, "database": DATABASE},
                    }), ["server"])
                    self.assertEqual(m_query_builder.native_database_binding_issues({
                        "type": kind, "details": {"server": SERVER, "database": value},
                    }), ["database"])
            with self.subTest(connector=kind, details="absent"):
                self.assertEqual(m_query_builder.native_database_binding_issues({
                    "type": kind, "details": {},
                }), ["server", "database"])
        self.assertEqual(m_query_builder.native_database_binding_issues({
            "type": "Snowflake", "details": {"server": SERVER, "database": DATABASE},
        }), ["warehouse"])
        self.assertEqual(m_query_builder.native_database_binding_issues({
            "type": "Databricks", "details": {"server": SERVER},
        }), ["http_path", "catalog", "schema"])
        self.assertEqual(m_query_builder.native_database_binding_issues({
            "type": "ClickHouse", "details": {"server": SERVER},
        }), ["database"])
        self.assertEqual(m_query_builder.native_database_binding_issues({
            "type": "Starburst", "details": {"server": SERVER, "catalog": "fixture_catalog"},
        }), ["schema"])


class TestNativeDatabaseContextSelection(NativeDatabaseFixture):
    def test_explicit_db_and_hyper_select_native_before_any_reader(self):
        for direct in (False, True):
            for enabled in ("true", "false", None):
                with self.subTest(direct=direct, enabled=enabled):
                    xml = _native_database_xml(direct=direct, enabled=enabled, filters=True)
                    original = ET.tostring(xml)
                    extract_xml = ET.tostring(xml.find("./extract"), encoding="unicode")
                    context_xml = ET.tostring(
                        xml.find(".//properties[@context='extract']"), encoding="unicode")
                    relative_workbook = os.path.join("synthetic", "original.twbx")
                    with _hyper_io_guard([IGNORED], no_hyper=True):
                        selected, selection = datasource_extractor.select_native_database_context(
                            xml, relative_workbook)
                    self.assertEqual(ET.tostring(xml), original)
                    self.assertIsNot(selected, xml)
                    self.assertEqual(selection, {
                        "policy": "prefer_native_database", "selected": "native_database",
                        "workbook": os.path.abspath(relative_workbook),
                        "hyper_members": [IGNORED],
                        "extract_xml": [extract_xml],
                        "object_extract_contexts_xml": [context_xml],
                    })
                    self.assertIsNone(selected.find(".//extract"))
                    self.assertIsNone(selected.find(".//properties[@context='extract']"))
                    self.assertIsNone(selected.find(".//connection[@class='hyper']"))
                    self.assertEqual(
                        {relation.get("table") for relation in selected.findall(".//relation")
                         if relation.get("type") == "table"},
                        {f"[{SCHEMA}].[{spec[2]}]" for spec in NATIVE_TABLES})
                    self.assertNotIn("snapshot_", ET.tostring(selected, encoding="unicode"))
                    self.assertIn("SNAPSHOT_ONLY", selection["extract_xml"][0])

    def test_isolated_datasource_keeps_native_columns_relationships_and_provenance(self):
        xml = _native_database_xml(filters=True)
        original = ET.tostring(xml)
        with _hyper_io_guard([IGNORED], no_hyper=True):
            datasource = datasource_extractor.extract_datasource(xml)
            self._assert_native_datasource(datasource)
            for table in datasource["tables"]:
                query = generate_power_query_m(table["connection_details"], table)
                self.assertIn(f'PostgreSQL.Database("{SERVER}:5432", "{DATABASE}")', query)
                self.assertNotIn("HyperExportRequired", query)
        self.assertEqual(ET.tostring(xml), original)
        self.assertIn("snapshot_amount", datasource["source_selection"]["extract_xml"][0])
        self.assertEqual(datasource["source_selection"]["workbook"], "")

    def test_missing_authentication_is_not_a_reason_to_retain_hyper(self):
        xml = _native_database_xml()
        for connection in xml.findall("./connection//named-connection/connection"):
            connection.attrib.pop("username")
            connection.attrib.pop("password", None)
        with _hyper_io_guard([IGNORED], no_hyper=True):
            datasource = datasource_extractor.extract_datasource(xml)
        self._assert_native_datasource(datasource)
        for connection in datasource["connection_map"].values():
            self.assertEqual(m_query_builder.native_database_binding_issues(connection), [])

    def test_no_explicit_hyper_does_not_fabricate_a_source_decision(self):
        xml = _native_database_xml(snapshot=False)
        original = ET.tostring(xml)
        selected, selection = datasource_extractor.select_native_database_context(xml)
        self.assertIsNone(selection)
        self.assertEqual(ET.tostring(selected), original)
        datasource = datasource_extractor.extract_datasource(xml)
        self.assertNotIn("source_selection", datasource)
        self.assertEqual(datasource.get("hyper_source_members", []), [])

    def test_non_database_and_unresolved_published_contexts_keep_hyper(self):
        cases = (
            ("Hyper only", _snapshot_only_xml()),
            ("Excel", _replace_original_connections(_native_database_xml(member=RETAINED),
                                                     "excel-direct")),
            ("CSV", _replace_original_connections(_native_database_xml(member=RETAINED),
                                                   "textscan")),
            ("published sqlproxy", _replace_original_connections(
                _native_database_xml(member=RETAINED), "sqlproxy")),
            ("mixed DB and file", _replace_original_connections(
                _native_database_xml(member=RETAINED), "excel-direct", only="regions-ref")),
            ("mixed DB and sqlproxy", _replace_original_connections(
                _native_database_xml(member=RETAINED), "sqlproxy", only="regions-ref")),
        )
        workbook = self._package("controls", [], {RETAINED: UNREADABLE_HYPER})
        for label, xml in cases:
            with self.subTest(source=label), _hyper_io_guard(no_hyper=True):
                original = ET.tostring(xml)
                selected, selection = datasource_extractor.select_native_database_context(
                    xml, workbook)
                self.assertIsNone(selection)
                self.assertEqual(ET.tostring(selected), original)
                datasource = datasource_extractor.extract_datasource(xml, workbook)
                self.assertNotIn("source_selection", datasource)
                self.assertEqual(datasource["hyper_source_members"], [RETAINED])
                identities = [table["hyper_identity"] for table in datasource["tables"]
                              if "hyper_identity" in table]
                self.assertTrue(identities, datasource["tables"])
                self.assertEqual({identity["archive_member"] for identity in identities},
                                 {RETAINED})

    def test_provenance_members_are_exact_xml_paths_not_basename_guesses(self):
        xml = _native_database_xml()
        second = ET.SubElement(xml, "extract", {"enabled": "true"})
        ET.SubElement(second, "connection", {"class": "hyper", "dbname": RETAINED})
        with _hyper_io_guard([IGNORED, RETAINED], no_hyper=True):
            _selected, selection = datasource_extractor.select_native_database_context(xml)
            datasource = datasource_extractor.extract_datasource(xml)
        self.assertCountEqual(selection["hyper_members"], [IGNORED, RETAINED])
        self.assertCountEqual(datasource["hyper_source_members"], [IGNORED, RETAINED])
        self.assertEqual(len(selection["extract_xml"]), 2)


class TestNativeDatabaseLineageFailures(NativeDatabaseFixture):
    @staticmethod
    def _native_leaves(xml):
        return [relation for relation in xml.findall(".//relation")
                if relation.get("connection") in {"orders-ref", "regions-ref"}]

    def _invalid(self, reason):
        xml = _native_database_xml()
        if reason in {"missing server", "missing database"}:
            attribute = "server" if reason == "missing server" else "dbname"
            for connection in xml.findall("./connection//named-connection/connection"):
                connection.attrib.pop(attribute)
        elif reason == "missing physical table":
            for relation in self._native_leaves(xml):
                if relation.get("name") == "Orders Model":
                    relation.attrib.pop("table")
        elif reason == "unresolved connection owner":
            for relation in self._native_leaves(xml):
                if relation.get("name") == "Orders Model":
                    relation.set("connection", "not-declared")
        elif reason == "duplicate connection owner":
            named = xml.find("./connection/named-connections")
            duplicate = copy.deepcopy(named[0])
            duplicate.find("./connection").set("server", "other-database.example.invalid")
            named.append(duplicate)
        elif reason == "missing column ownership":
            for relation in self._native_leaves(xml):
                columns = relation.find("./columns")
                for column in list(columns):
                    if column.get("name") == "amount":
                        columns.remove(column)
            for record in xml.findall("./connection/metadata-records/metadata-record"):
                if record.findtext("local-name") == "[amount]":
                    record.remove(record.find("parent-name"))
            maps = xml.find("./connection/cols")
            for mapping in list(maps):
                if mapping.get("key") == "[amount]":
                    maps.remove(mapping)
        elif reason == "conflicting column ownership":
            records = xml.find("./connection/metadata-records")
            _record(records, "amount", "amount", "Regions Model", "real", 5)
        elif reason == "missing physical column":
            for record in xml.findall("./connection/metadata-records/metadata-record"):
                if record.findtext("local-name") == "[amount]":
                    record.find("remote-name").text = ""
            maps = xml.find("./connection/cols")
            for mapping in list(maps):
                if mapping.get("key") == "[amount]":
                    maps.remove(mapping)
            for relation in self._native_leaves(xml):
                columns = relation.find("./columns")
                for column in list(columns):
                    if column.get("name") == "amount":
                        columns.remove(column)
        elif reason == "multiple locals for one physical column":
            _set_native_remotes(xml, {
                "order_id": "shared_key_native",
                "order_region_id": "shared_key_native",
            })
        elif reason == "unsupported custom SQL":
            for relation in self._native_leaves(xml):
                if relation.get("name") == "Orders Model":
                    relation.set("type", "text")
                    relation.attrib.pop("table")
                    relation.text = "SELECT order_id, order_region_id, amount FROM reporting.fact_orders"
        else:
            raise AssertionError("Unknown invalid fixture: " + reason)
        return xml

    def test_incomplete_or_ambiguous_explicit_database_never_falls_back_to_snapshot(self):
        reasons = (
            "missing server", "missing database", "missing physical table",
            "unresolved connection owner", "duplicate connection owner",
            "missing column ownership", "conflicting column ownership",
            "missing physical column", "multiple locals for one physical column",
            "unsupported custom SQL",
        )
        for index, reason in enumerate(reasons):
            with self.subTest(reason=reason):
                xml = self._invalid(reason)
                original = ET.tostring(xml)
                workbook = self._package(
                    "invalid-" + str(index), [xml], {IGNORED: UNREADABLE_HYPER})
                metadata = self.root / "invalid-metadata" / str(index)
                with _hyper_io_guard([IGNORED], no_hyper=True):
                    for operation in ("selection", "datasource", "producer"):
                        with self.subTest(operation=operation):
                            with self.assertRaises(ExtractionError) as raised, \
                                    redirect_stdout(io.StringIO()):
                                if operation == "selection":
                                    datasource_extractor.select_native_database_context(xml, workbook)
                                elif operation == "datasource":
                                    datasource_extractor.extract_datasource(xml, workbook)
                                else:
                                    extract_tableau_data.TableauExtractor(
                                        workbook, output_dir=str(metadata)).extract_all()
                            self.assertEqual(raised.exception.source, "native_database")
                    self.assertFalse((metadata / "datasources.json").exists())
                self.assertEqual(ET.tostring(xml), original)


class TestNativeDatabaseExplicitParserMetadata(NativeDatabaseFixture):
    DATABASES = {"Orders Model": "orders_fixture_db", "Regions Model": "regions_fixture_db"}

    @classmethod
    def _xml(cls, kind, *, unqualified=(), schema=SCHEMA):
        xml = _native_database_xml()
        for connection in xml.findall("./connection//named-connection/connection"):
            connection.set("class", kind)
            connection.set("port", "8123" if kind == "clickhouse" else "443")
            connection.attrib.pop("dbname")
            connection.attrib.pop("schema")
            if kind == "starburst":
                connection.set("catalog", "fixture_catalog")
                if schema is not None:
                    connection.set("schema", schema)
        if kind == "clickhouse":
            for relation in TestNativeDatabaseLineageFailures._native_leaves(xml):
                alias = relation.get("name")
                physical = next(spec[2] for spec in NATIVE_TABLES if spec[0] == alias)
                relation.set("table", f"[{physical}]" if alias in unqualified else
                             f"[{cls.DATABASES[alias]}].[{physical}]")
        return xml

    def test_parser_defaults_never_supply_missing_required_native_database_lineage(self):
        cases = (
            ("clickhouse-unqualified", "database", self._xml(
                "clickhouse", unqualified=("Orders Model", "Regions Model"))),
            # A different leaf's explicit database cannot repair this one.
            ("clickhouse-other-leaf-qualified", "database", self._xml(
                "clickhouse", unqualified=("Orders Model",))),
            # Even a qualified relation must not invent the required schema
            # setting of the Starburst connection from the parser's default.
            ("starburst-no-schema", "schema", self._xml("starburst", schema=None)),
        )
        for tag, missing, xml in cases:
            with self.subTest(case=tag):
                original = ET.tostring(xml)
                workbook = self._package(tag, [xml], {IGNORED: UNREADABLE_HYPER})
                metadata = self.root / "invalid-parser-metadata" / tag
                with _hyper_io_guard([IGNORED], no_hyper=True):
                    for operation in ("selection", "datasource", "producer"):
                        with self.subTest(operation=operation):
                            with self.assertRaises(ExtractionError) as raised, \
                                    redirect_stdout(io.StringIO()):
                                if operation == "selection":
                                    datasource_extractor.select_native_database_context(xml, workbook)
                                elif operation == "datasource":
                                    datasource_extractor.extract_datasource(xml, workbook)
                                else:
                                    extract_tableau_data.TableauExtractor(
                                        workbook, output_dir=str(metadata)).extract_all()
                            self.assertEqual(raised.exception.source, "native_database")
                            self.assertIn(missing, str(raised.exception).lower())
                    self.assertFalse((metadata / "datasources.json").exists())
                self.assertEqual(ET.tostring(xml), original)

    def test_explicit_same_leaf_database_and_explicit_starburst_schema_reach_real_m(self):
        for kind, connector in (("clickhouse", "ClickHouse"), ("starburst", "Starburst")):
            with self.subTest(connector=connector):
                xml = self._xml(kind)
                # ClickHouse has no dbname at all: each qualified original
                # relation, not the parser's "default", supplies its database.
                for connection in xml.findall("./connection//named-connection/connection"):
                    self.assertNotIn("dbname", connection.attrib)
                workbook = self._package(kind, [xml], {IGNORED: UNREADABLE_HYPER})
                with _hyper_io_guard([IGNORED], no_hyper=True):
                    selected, selection = datasource_extractor.select_native_database_context(
                        xml, workbook)
                    self.assertEqual(selection["selected"], "native_database")
                    self.assertIsNone(selected.find("./extract"))
                    isolated = datasource_extractor.extract_datasource(xml, workbook)
                    self._assert_native_datasource(
                        isolated, workbook=workbook, connector=connector)
                    metadata, extracted = self._extract(workbook, kind)
                    datasource, = extracted["datasources"]
                    self._assert_native_datasource(
                        datasource, workbook=workbook, connector=connector)
                    project = self._generate(metadata, extracted, kind)
                    self._post(workbook, project, metadata)
                    for alias, _reference, physical, fields in NATIVE_TABLES:
                        native, = [table for table in datasource["tables"] if table["name"] == alias]
                        details = native["connection_details"]["details"]
                        self.assertEqual(details["server"], SERVER)
                        text = self._tables(project)[alias]
                        self.assertIn(f'Name="{physical}"', text)
                        self.assertNotIn(f'Name="{alias}"', text)
                        if kind == "clickhouse":
                            database = self.DATABASES[alias]
                            self.assertEqual(details["database"], database)
                            self.assertIn(f"SERVER={SERVER};PORT=8123;DATABASE={database}", text)
                            self.assertNotIn("DATABASE=default", text)
                        else:
                            self.assertEqual(details["catalog"], "fixture_catalog")
                            self.assertEqual(details["schema"], SCHEMA)
                            self.assertIn(f"HOST={SERVER};PORT=443;SSL=1", text)
                            self.assertIn('Name="fixture_catalog"', text)
                            self.assertIn(f'Name="{SCHEMA}"', text)
                            self.assertNotIn('Name="default"', text)
                        self.assertEqual(re.findall(
                            r"(?m)^\s*mode:\s+(\w+)\s*$", text), ["import"])
                        self.assertEqual(set(re.findall(
                            r"(?m)^\s*sourceColumn:\s*([^\r\n]+)", text)),
                            {name for name, _datatype in fields})
                        for forbidden in ("#table(", "snapshot_", "Migration.HyperExportRequired"):
                            self.assertNotIn(forbidden, text)
                    self._assert_ignored_inventory(extracted)
                    self._assert_no_exports(project)
                    self._assert_native_gate(project, metadata, extracted, connector)


class TestUnusedHyperMembers(NativeDatabaseFixture):
    def _selected(self, workbook, member=IGNORED):
        return {
            "name": "chosen-native",
            "hyper_source_members": [member],
            "source_selection": {
                "policy": "prefer_native_database", "selected": "native_database",
                "workbook": os.path.abspath(workbook), "hyper_members": [member],
                "extract_xml": [], "object_extract_contexts_xml": [],
            },
            "tables": [],
        }

    def test_only_exact_selected_members_of_the_requested_workbook_are_unused(self):
        first = str(self.root / "one.twbx")
        second = str(self.root / "two.twbx")
        selected = [self._selected(first), self._selected(second, RETAINED)]
        result = datasource_extractor.unused_hyper_members(selected, first)
        self.assertIsInstance(result, set)
        self.assertEqual(result, {IGNORED})
        self.assertEqual(datasource_extractor.unused_hyper_members(selected, second),
                         {RETAINED})
        self.assertEqual(datasource_extractor.unused_hyper_members(selected),
                         {IGNORED, RETAINED})

    def test_nonselected_explicit_declaration_protects_a_shared_member_without_identity(self):
        workbook = str(self.root / "shared.twbx")
        chosen = self._selected(workbook)
        retained = {"name": "unresolved", "hyper_source_members": [IGNORED], "tables": []}
        original = copy.deepcopy([chosen, retained])
        self.assertEqual(
            datasource_extractor.unused_hyper_members([chosen, retained], workbook), set())
        self.assertEqual([chosen, retained], original)

    def test_active_identity_consumer_protects_a_member_even_without_declaration_list(self):
        workbook = str(self.root / "shared.twbx")
        chosen = self._selected(workbook)
        identity = {
            "workbook": os.path.abspath(workbook), "archive_member": IGNORED,
            "schema": "main", "table": SNAPSHOT_TABLE,
        }
        retained = {"name": "active-snapshot", "tables": [
            {"name": "Retained Snapshot", "hyper_identity": identity},
        ]}
        self.assertEqual(
            datasource_extractor.unused_hyper_members([chosen, retained], workbook), set())
        chosen["tables"] = retained["tables"]
        self.assertEqual(datasource_extractor.unused_hyper_members([chosen], workbook), set())

    def test_all_native_consumers_can_skip_but_one_retained_consumer_prevents_skipping(self):
        workbook = str(self.root / "shared.twbx")
        chosen = [self._selected(workbook), self._selected(workbook)]
        self.assertEqual(datasource_extractor.unused_hyper_members(chosen, workbook), {IGNORED})
        self.assertEqual(datasource_extractor.unused_hyper_members(
            chosen + [{"hyper_source_members": [IGNORED]}], workbook), set())

    def test_same_basename_in_another_directory_does_not_protect_the_wrong_member(self):
        workbook = str(self.root / "shared.twbx")
        self.assertEqual(datasource_extractor.unused_hyper_members([
            self._selected(workbook), {"hyper_source_members": [RETAINED]},
        ], workbook), {IGNORED})


class TestNativeDatabaseProducerConsumer(NativeDatabaseFixture):
    def test_default_and_explicit_import_keep_database_binding_without_hyper_runtime(self):
        for case in DATABASE_CASES:
            for mode in (None, "import"):
                xml_class, connector, _port, _function, _auto_mode = case
                tag = xml_class + "-" + (mode or "default")
                with self.subTest(connector=connector, mode=mode or "default"):
                    xml = _native_database_xml(xml_class)
                    workbook = self._package(tag, [xml], {IGNORED: UNREADABLE_HYPER})
                    with _hyper_io_guard([IGNORED], no_hyper=True) as reads:
                        metadata, extracted = self._extract(workbook, tag)
                        self._assert_native_datasource(
                            extracted["datasources"][0], workbook=workbook,
                            connector=connector)
                        self._assert_ignored_inventory(extracted)
                        project = self._generate(metadata, extracted, tag, mode=mode)
                        self._post(workbook, project, metadata)
                        self._assert_native_m(project, case=case, mode=mode)
                        self._assert_no_exports(project)
                        self._assert_native_gate(project, metadata, extracted, connector)
                    self.assertNotIn(IGNORED, reads)
                    self.assertIn("Workbook.twb", reads)

    def test_normalization_precedes_worksheet_lineage_filters_rls_and_real_pbip_generation(self):
        xml = _native_database_xml(filters=True)
        workbook = self._package(
            "mapped", [xml], {IGNORED: UNREADABLE_HYPER}, worksheet=True)
        with _hyper_io_guard([IGNORED], no_hyper=True):
            metadata, extracted = self._extract(workbook, "mapped")
            datasource, = extracted["datasources"]
            self._assert_native_datasource(datasource, workbook=workbook)
            worksheet, = extracted["worksheets"]
            fields = {field["name"]: field for field in worksheet["fields"]}
            self.assertEqual(fields["region_name"]["source_table"], "Regions Model")
            self.assertEqual(fields["region_name"]["source_column"], "region_name")
            self.assertEqual(fields["amount"]["source_table"], "Orders Model")
            self.assertEqual(fields["amount"]["source_column"], "amount")
            self.assertEqual(extracted["datasource_filters"], [{
                "datasource": "Synthetic Native Database",
                "column": "region_name", "filter_class": "categorical",
                "filter_type": "", "values": ['"Native North"'],
                "range_min": None, "range_max": None,
            }])
            self.assertEqual([item["name"] for item in extracted["user_filters"]],
                             ["NativeAccess"])
            self.assertEqual(extracted["user_filters"][0]["column"], "region_name")
            self.assertEqual(extracted["user_filters"][0]["user_mappings"], [{
                "user": "reader@example.invalid", "value": "Native North",
            }])
            self.assertEqual(len(extracted["filters"]), 1)
            self.assertNotIn("SNAPSHOT_ONLY", json.dumps(extracted["filters"]))
            self.assertNotIn("snapshot_", json.dumps(worksheet))
            self.assertNotIn("SnapshotAccess", json.dumps(extracted["user_filters"]))
            # Snapshot history is retained only as source-selection provenance.
            self.assertIn("SnapshotAccess", datasource["source_selection"]["extract_xml"][0])
            project = self._generate(metadata, extracted, "mapped", importer=True)
            self._post(workbook, project, metadata)
            self._assert_native_m(project)
            visuals = list((Path(project) / (REPORT + ".Report")).rglob("visual.json"))
            self.assertEqual(len(visuals), 1)
            visual = json.loads(visuals[0].read_text(encoding="utf-8"))["visual"]
            state = visual["query"]["queryState"]
            self.assertEqual(state["Category"]["projections"][0]["field"], {"Column": {
                "Expression": {"SourceRef": {"Entity": "Regions Model"}},
                "Property": "region_name",
            }})
            self.assertEqual(state["Y"]["projections"][0]["field"], {"Aggregation": {
                "Expression": {"Column": {
                    "Expression": {"SourceRef": {"Entity": "Orders Model"}},
                    "Property": "amount",
                }},
                "Function": 0,
            }})
            roles = (
                Path(project) / (REPORT + ".SemanticModel") / "definition" / "roles.tmdl"
            ).read_text(encoding="utf-8")
            self.assertIn("NativeAccess", roles)
            self.assertIn("tablePermission 'Regions Model'", roles)
            self.assertIn("[region_name]", roles)
            self.assertIn("USERPRINCIPALNAME()", roles)
            self.assertNotIn("SnapshotAccess", roles)
            self.assertNotIn("snapshot-reader", roles)
            self.assertNotIn("filterExpression = TRUE()", roles)
            self._assert_no_exports(project)
            self._assert_native_gate(project, metadata, extracted)

    def test_resolved_row_caps_do_not_reject_database_only_packages(self):
        for origin in ("explicit", "environment"):
            with self.subTest(cap_origin=origin):
                workbook = self._package(
                    "cap-" + origin, [_native_database_xml()], {IGNORED: UNREADABLE_HYPER})
                with _hyper_io_guard([IGNORED], no_hyper=True):
                    metadata, extracted = self._extract(workbook, "cap-" + origin)
                    project = self._generate(metadata, extracted, "cap-" + origin)
                    options = {"hyper_export_max_rows": 1} if origin == "explicit" else {}
                    environment = (
                        {"TTPBI_HYPER_EXPORT_MAX_ROWS": "1"}
                        if origin == "environment" else {})
                    with mock.patch.dict(os.environ, environment):
                        self._post(workbook, project, metadata, **options)
                    self._assert_native_m(project)
                    self._assert_no_exports(project)
                    self._assert_native_gate(project, metadata, extracted)


class TestNativeDatabaseColumnNormalization(NativeDatabaseFixture):
    @staticmethod
    def _renamed_xml(xml_class="postgres", *, calculation=False):
        remotes = {local: local + "_native" for _owner, local, _datatype in NATIVE_FIELDS}
        captions = {"amount": "SalesAmount", "region_name": "RegionLabel"}
        xml = _set_native_remotes(
            _native_database_xml(xml_class), remotes, captions=captions)
        if calculation:
            calculated = ET.SubElement(xml, "column", {
                "name": "[adjusted_amount]", "caption": "AdjustedSales",
                "datatype": "real", "role": "dimension",
            })
            ET.SubElement(calculated, "calculation", {
                "class": "tableau", "formula": "[amount] * 2 + [order_id]",
            })
        return xml, remotes, captions

    def _assert_physical_metadata(self, datasource, remotes):
        for alias, _reference, _physical, fields in NATIVE_TABLES:
            table, = [table for table in datasource["tables"] if table["name"] == alias]
            self.assertEqual([
                (column["name"], column["remote_name"], column["sourceColumn"])
                for column in table["columns"]
            ], [(local, remotes.get(local, local), remotes.get(local, local))
                for local, _datatype in fields])
            self.assertEqual(table["cols_physical_map"], {
                remotes[local]: local for local, _datatype in fields
                if remotes.get(local, local) != local
            })

    def test_native_physical_then_local_then_caption_precede_calculated_m_steps(self):
        for case in DATABASE_CASES:
            xml_class, connector, _port, _function, _auto_mode = case
            tag = "renamed-calculation-" + xml_class
            with self.subTest(connector=connector):
                xml, remotes, captions = self._renamed_xml(xml_class, calculation=True)
                workbook = self._package(tag, [xml], {IGNORED: UNREADABLE_HYPER})
                with _hyper_io_guard([IGNORED], no_hyper=True):
                    metadata, extracted = self._extract(workbook, tag)
                    datasource, = extracted["datasources"]
                    self._assert_native_datasource(
                        datasource, workbook=workbook, connector=connector)
                    self._assert_physical_metadata(datasource, remotes)
                    self.assertEqual([calc["formula"] for calc in datasource["calculations"]],
                                     ["[amount] * 2 + [order_id]"])
                    self.assertIn("snapshot_amount",
                                  datasource["source_selection"]["extract_xml"][0])
                    self._assert_ignored_inventory(extracted)
                    project = self._generate(metadata, extracted, tag)
                    self._post(workbook, project, metadata)
                    self._assert_native_m(project, case=case, captions=captions)
                    for alias, _reference, _physical, fields in NATIVE_TABLES:
                        text = self._tables(project)[alias]
                        (_partition, query), = extract_m_partitions(text)
                        operations = _native_column_operations(query)
                        expected = [
                            ("SelectColumns", [remotes[local] for local, _datatype in fields]),
                            ("RenameColumns", [(remotes[local], local)
                                               for local, _datatype in fields]),
                            ("RenameColumns", [(local, captions[local])
                                               for local, _datatype in fields
                                               if local in captions]),
                        ]
                        if alias == "Orders Model":
                            self.assertEqual(len(operations), 4, query)
                            self.assertEqual(operations[:-1], expected)
                            kind, name, expression, datatype = operations[-1]
                            self.assertEqual((kind, name, datatype),
                                             ("AddColumn", "AdjustedSales", "type number"))
                            self.assertEqual(set(re.findall(r"\[([^\]]+)\]", expression)),
                                             {"SalesAmount", "order_id"})
                            # Evaluate only the emitted scalar on the emitted name chain.
                            # Decoy local/caption fields must be projected out, not used.
                            row = _apply_native_column_operations(operations[:-1], {
                                remotes["order_id"]: 101, remotes["order_region_id"]: 2,
                                remotes["amount"]: 79.25, "amount": -1, "SalesAmount": -2,
                            })
                            self.assertEqual(row, {
                                "order_id": 101, "order_region_id": 2, "SalesAmount": 79.25,
                            })
                            row[name] = EmittedScalarM(expression).evaluate(row)
                            self.assertEqual(row["AdjustedSales"], 259.5)
                        else:
                            self.assertEqual(operations, expected)
                            row = _apply_native_column_operations(operations, {
                                remotes["region_id"]: 2,
                                remotes["region_name"]: "Native North",
                                "region_name": "STALE LOCAL", "RegionLabel": "STALE CAPTION",
                            })
                            self.assertEqual(row, {"region_id": 2, "RegionLabel": "Native North"})
                        self.assertEqual(set(re.findall(
                            r"(?m)^\s*sourceColumn:\s*([^\r\n]+)", text)), set(row))
                        for forbidden in ("MissingField.Ignore", "MissingField.UseNull",
                                          "__TableauNativeColumn_", "snapshot_"):
                            self.assertNotIn(forbidden, query)
                    self._assert_no_exports(project)
                    self._assert_native_gate(project, metadata, extracted, connector)

    def test_physical_worksheet_lineage_projects_terminal_captioned_pbip_columns(self):
        xml, remotes, captions = self._renamed_xml()
        workbook = self._package(
            "renamed-worksheet", [xml], {IGNORED: UNREADABLE_HYPER}, worksheet=True)
        with _hyper_io_guard([IGNORED], no_hyper=True):
            metadata, extracted = self._extract(workbook, "renamed-worksheet")
            datasource, = extracted["datasources"]
            self._assert_native_datasource(datasource, workbook=workbook)
            self._assert_physical_metadata(datasource, remotes)
            worksheet, = extracted["worksheets"]
            fields = {field["name"]: field for field in worksheet["fields"]}
            for name, owner in (("region_name", "Regions Model"), ("amount", "Orders Model")):
                self.assertEqual(fields[name]["source_table"], owner)
                self.assertEqual(fields[name]["source_column"], remotes[name])
            self.assertNotIn("snapshot_", json.dumps(worksheet))
            project = self._generate(
                metadata, extracted, "renamed-worksheet", importer=True)
            self._post(workbook, project, metadata)
            self._assert_native_m(project, captions=captions)
            visuals = list((Path(project) / (REPORT + ".Report")).rglob("visual.json"))
            self.assertEqual(len(visuals), 1)
            visual = json.loads(visuals[0].read_text(encoding="utf-8"))["visual"]
            state = visual["query"]["queryState"]
            self.assertEqual(state["Category"]["projections"][0]["field"], {"Column": {
                "Expression": {"SourceRef": {"Entity": "Regions Model"}},
                "Property": "RegionLabel",
            }})
            self.assertEqual(state["Y"]["projections"][0]["field"], {"Aggregation": {
                "Expression": {"Column": {
                    "Expression": {"SourceRef": {"Entity": "Orders Model"}},
                    "Property": "SalesAmount",
                }},
                "Function": 0,
            }})
            self._assert_no_exports(project)
            self._assert_native_gate(project, metadata, extracted)

    def test_local_physical_swap_uses_deterministic_noncolliding_temporary_names(self):
        remotes = {
            "order_id": "order_region_id", "order_region_id": "order_id",
            "amount": "__TableauNativeColumn_0",
        }
        xml = _set_native_remotes(_native_database_xml(), remotes)
        workbook = self._package("physical-swap", [xml], {IGNORED: UNREADABLE_HYPER})
        with _hyper_io_guard([IGNORED], no_hyper=True):
            metadata, extracted = self._extract(workbook, "physical-swap")
            datasource, = extracted["datasources"]
            self._assert_native_datasource(datasource, workbook=workbook)
            self._assert_physical_metadata(datasource, remotes)
            table, = [item for item in datasource["tables"] if item["name"] == "Orders Model"]
            query = generate_power_query_m(table["connection_details"], table)
            self.assertEqual(query, generate_power_query_m(table["connection_details"], table))
            operations = _native_column_operations(query)
            self.assertEqual([kind for kind, _values in operations],
                             ["SelectColumns", "RenameColumns", "RenameColumns"])
            physical = [remotes[name] for name, _datatype in NATIVE_TABLES[0][3]]
            self.assertEqual(operations[0], ("SelectColumns", physical))
            first, second = operations[1][1], operations[2][1]
            self.assertEqual([old for old, _new in first], physical)
            temporary = [new for _old, new in first]
            self.assertEqual(len(set(temporary)), 3)
            self.assertTrue(set(temporary).isdisjoint(
                set(physical) | {"order_id", "order_region_id", "amount"}))
            self.assertEqual(second, list(zip(temporary, ["order_id", "order_region_id", "amount"])))
            self.assertEqual(_apply_native_column_operations(operations, {
                "order_region_id": 11, "order_id": 22, "__TableauNativeColumn_0": 3.5,
                "unexpected": "must be dropped",
            }), {"order_id": 11, "order_region_id": 22, "amount": 3.5})
            project = self._generate(metadata, extracted, "physical-swap")
            self._post(workbook, project, metadata)
            self._assert_native_m(project)
            self._assert_no_exports(project)
            self._assert_native_gate(project, metadata, extracted)

    def test_projection_removes_unmodeled_collision_but_cannot_replace_missing_physical(self):
        xml = _set_native_remotes(_native_database_xml(), {"amount": "amount_native"})
        with _hyper_io_guard([IGNORED], no_hyper=True):
            datasource = datasource_extractor.extract_datasource(xml)
            self._assert_native_datasource(datasource)
            self._assert_physical_metadata(datasource, {"amount": "amount_native"})
            table, = [item for item in datasource["tables"] if item["name"] == "Orders Model"]
            query = generate_power_query_m(table["connection_details"], table)
        operations = _native_column_operations(query)
        self.assertEqual(operations, [
            ("SelectColumns", ["order_id", "order_region_id", "amount_native"]),
            ("RenameColumns", [("amount_native", "amount")]),
        ])
        physical = {"order_id": 1, "order_region_id": 2, "amount_native": 7.5, "amount": 999}
        self.assertEqual(_apply_native_column_operations(operations, physical), {
            "order_id": 1, "order_region_id": 2, "amount": 7.5,
        })
        # Without the actual first step, the decoy local name would collide.
        with self.assertRaisesRegex(ValueError, "collides"):
            _apply_native_column_operations(operations[1:], physical)
        with self.assertRaises(KeyError) as raised:
            _apply_native_column_operations(
                operations, {"order_id": 1, "order_region_id": 2, "amount": 999})
        self.assertEqual(raised.exception.args, ("amount_native",))
        for forbidden in ("MissingField.Ignore", "MissingField.UseNull", "try ", "otherwise"):
            self.assertNotIn(forbidden, query)

    def test_duplicate_physical_aliases_fail_explicitly_in_public_m_builder(self):
        for first in (
            {"name": "amount", "sourceColumn": "shared_native"},
            {"name": "shared_native", "sourceColumn": "shared_native"},
            {"name": "shared_native"},
        ):
            with self.subTest(first=first), _hyper_io_guard(no_hyper=True):
                table = {
                    "name": "Orders Model", "source_table": f"[{SCHEMA}].[fact_orders]",
                    "columns": [
                        dict(first, datatype="real"),
                        {"name": "amount_copy", "sourceColumn": "shared_native",
                         "datatype": "real"},
                    ],
                }
                with self.assertRaises((ConfigurationError, ValueError)) as raised:
                    generate_power_query_m({
                        "type": "PostgreSQL",
                        "details": {"server": SERVER, "database": DATABASE, "schema": SCHEMA},
                    }, table)
                self.assertRegex(str(raised.exception), r"(?i)(physical|sourceColumn|source column)")
                self.assertRegex(str(raised.exception), r"(?i)(multiple|duplicate|ambiguous)")

    def test_typing_uses_physical_columns_for_database_emitters_but_not_file_emitters(self):
        table = {
            "name": "Orders Model", "source_table": f"[{SCHEMA}].[fact_orders]",
            "columns": [{"name": "amount", "sourceColumn": "amount_native", "datatype": "real"}],
        }
        with _hyper_io_guard(no_hyper=True):
            query = generate_power_query_m({
                "type": "Impala", "details": {"server": SERVER, "port": "21050", "schema": SCHEMA},
            }, table)
            self.assertIn('Name="fact_orders"', query)
            self.assertIn('{"amount_native", type number}', query)
            self.assertNotIn('{"amount", type number}', query)
            self.assertLess(query.index("Table.TransformColumnTypes("),
                            query.index("Table.SelectColumns("))
            self.assertLess(query.index("Table.SelectColumns("),
                            query.index("Table.RenameColumns("))
            for connector, filename, source_table in (
                ("CSV", "synthetic.csv", "[synthetic#csv]"),
                ("Excel", "synthetic.xlsx", "[Synthetic$]"),
            ):
                with self.subTest(connector=connector):
                    file_table = dict(table, source_table=source_table)
                    query = generate_power_query_m({
                        "type": connector, "details": {
                            "filename": str(self.root / filename), "delimiter": ",",
                        },
                    }, file_table, use_original_file_paths=True)
                    self.assertIn("File.Contents(", query)
                    self.assertIn(filename, query)
                    self.assertIn('{"amount", type number}', query)
                    self.assertNotIn("amount_native", query)
                    self.assertNotIn("__TableauNativeColumn_", query)


class TestRetainedHyperPolicyControls(NativeDatabaseFixture):
    def _mixed(self, tag, *, shared=False, count=23):
        member = IGNORED if shared else RETAINED
        snapshots = [_snapshot_only_xml(member)]
        if shared:
            snapshots.append(_snapshot_only_xml(
                member, name="snapshot-two", alias="Second Snapshot"))
        members = {IGNORED: UNREADABLE_HYPER}
        members[member] = self._sqlite_payload(tag, count=count)
        workbook = self._package(
            tag, [_native_database_xml()] + snapshots, members)
        return workbook, member

    def test_hyper_only_package_requires_then_produces_a_real_full_import(self):
        workbook = self._package("hyper-only", [_snapshot_only_xml()], {
            RETAINED: self._sqlite_payload("hyper-only"),
        })
        with _hyper_io_guard() as reads:
            metadata, extracted = self._extract(workbook, "hyper-only")
            datasource, = extracted["datasources"]
            self.assertNotIn("source_selection", datasource)
            self.assertEqual(datasource["hyper_source_members"], [RETAINED])
            self.assertEqual(datasource_extractor.unused_hyper_members(
                extracted["datasources"], workbook), set())
            project = self._generate(metadata, extracted, "hyper-only")
            self.assertIn("Migration.HyperExportRequired",
                          self._tables(project)["Retained Snapshot"])
            with self.assertRaises(GenerationError) as raised:
                migrate._require_hyper_import(project, extract_dir=metadata)
            self.assertEqual(raised.exception.source, "hyper_import")
            self._post(workbook, project, metadata)
            record, = self._manifest(project)["exports"]
            self._assert_real_export(
                project, record, member=RETAINED, count=23, columns=["id", "label"])
            text = self._tables(project)["Retained Snapshot"]
            self.assertIn(record["filename"], text)
            self.assertIn("Migration_HyperIdentity", text)
            self.assertNotIn("Migration.HyperExportRequired", text)
            self.assertEqual(re.findall(r"(?m)^\s*mode:\s+(\w+)\s*$", text), ["import"])
            evidence = migrate._require_hyper_import(project, extract_dir=metadata)
            self.assertEqual(evidence["status"], "static_verified", evidence["diagnostics"])
            self.assertEqual(evidence["diagnostics"], [])
            self.assertEqual(evidence["source_decisions"], [])
            self.assertEqual(evidence["runtime_status"], "not_run")
            self.assertEqual(len(evidence["exports"]), 1)
            self.assertEqual(len(evidence["bindings"]), 1)
        self.assertIn(RETAINED, reads)

    def test_mixed_package_reads_and_exports_only_the_remaining_exact_member(self):
        workbook, member = self._mixed("mixed")
        with _hyper_io_guard([IGNORED]) as reads:
            metadata, extracted = self._extract(workbook, "mixed")
            self.assertEqual(len(extracted["datasources"]), 2)
            self._assert_native_datasource(extracted["datasources"][0], workbook=workbook)
            self._assert_ignored_inventory(extracted)
            snapshot = extracted["datasources"][1]
            self.assertNotIn("source_selection", snapshot)
            self.assertEqual(snapshot["hyper_source_members"], [member])
            self.assertEqual(datasource_extractor.unused_hyper_members(
                extracted["datasources"], workbook), {IGNORED})
            project = self._generate(metadata, extracted, "mixed")
            self._post(workbook, project, metadata)
            record, = self._manifest(project)["exports"]
            rows = self._assert_real_export(
                project, record, member=member, count=23, columns=["id", "label"])
            self.assertEqual(rows[1], ["0", "synthetic row,0"])
            self.assertEqual(rows[-1], ["22", "synthetic row,22"])
            self._assert_native_m(project)
            snapshot_tmdl = self._tables(project)["Retained Snapshot"]
            self.assertIn("Migration_HyperIdentity", snapshot_tmdl)
            self.assertIn(record["filename"], snapshot_tmdl)
            self.assertIn("Csv.Document", snapshot_tmdl)
            self.assertEqual(re.findall(r"(?m)^\s*mode:\s+(\w+)\s*$", snapshot_tmdl),
                             ["import"])
            self.assertNotIn("Migration.HyperExportRequired", snapshot_tmdl)
            evidence = migrate._require_hyper_import(project, extract_dir=metadata)
            self.assertEqual(evidence["status"], "static_verified", evidence["diagnostics"])
            self.assertEqual(evidence["diagnostics"], [])
            self.assertEqual(evidence["runtime_status"], "not_run")
            self.assertEqual(len(evidence["exports"]), 1)
            self.assertEqual(len(evidence["bindings"]), 1)
            self.assertEqual(evidence["source_decisions"], [{
                "datasource": "Synthetic Native Database", "retained": "native_database",
                "not_used": "hyper_snapshot", "connectors": ["PostgreSQL"],
            }])
            self._assert_decision_privacy(evidence)
        self.assertNotIn(IGNORED, reads)
        self.assertIn(member, reads)
        self.assertFalse((Path(project) / IGNORED).exists())

    def test_shared_member_stays_active_for_every_nonselected_datasource(self):
        workbook, member = self._mixed("shared", shared=True)
        with _hyper_io_guard() as reads:
            metadata, extracted = self._extract(workbook, "shared")
            self.assertEqual(len(extracted["datasources"]), 3)
            self.assertEqual(datasource_extractor.unused_hyper_members(
                extracted["datasources"], workbook), set())
            entry, = extracted["hyper_files"]
            self.assertNotEqual(entry.get("selection"), "native_database")
            project = self._generate(metadata, extracted, "shared")
            self._post(workbook, project, metadata)
            record, = self._manifest(project)["exports"]
            self._assert_real_export(
                project, record, member=member, count=23, columns=["id", "label"])
            self._assert_native_m(project)
            tables = self._tables(project)
            for alias in ("Retained Snapshot", "Second Snapshot"):
                self.assertIn(record["filename"], tables[alias])
                self.assertIn("Migration_HyperIdentity", tables[alias])
            evidence = migrate._require_hyper_import(project, extract_dir=metadata)
            self.assertEqual(evidence["status"], "static_verified", evidence["diagnostics"])
            self.assertEqual(len(evidence["exports"]), 1)
            self.assertEqual(len(evidence["bindings"]), 2)
            self.assertEqual(evidence["runtime_status"], "not_run")
        self.assertIn(member, reads)

    def test_non_database_or_mixed_original_sources_keep_a_real_full_hyper_import(self):
        cases = (
            ("excel", "excel-direct", None, ["Excel"]),
            ("csv", "textscan", None, ["CSV"]),
            ("published", "sqlproxy", None, ["Tableau Server"]),
            ("db-and-file", "excel-direct", "regions-ref", ["Excel", "PostgreSQL"]),
            ("db-and-published", "sqlproxy", "regions-ref", ["PostgreSQL", "Tableau Server"]),
        )
        for tag, kind, only, connectors in cases:
            with self.subTest(source=tag):
                xml = _replace_original_connections(
                    _native_database_xml(member=RETAINED), kind, only=only)
                workbook = self._package(tag, [xml], {
                    RETAINED: self._sqlite_payload(tag, materialized=True),
                })
                with _hyper_io_guard() as reads:
                    metadata, extracted = self._extract(workbook, tag)
                    datasource, = extracted["datasources"]
                    self.assertNotIn("source_selection", datasource)
                    self.assertEqual(datasource["hyper_source_members"], [RETAINED])
                    self.assertEqual(datasource_extractor.unused_hyper_members(
                        extracted["datasources"], workbook), set())
                    self.assertIn("materialized_extract", datasource)
                    table, = datasource["tables"]
                    self.assertEqual(table["hyper_identity"]["archive_member"], RETAINED)
                    self.assertEqual(datasource["relationships"], [])
                    project = self._generate(metadata, extracted, tag)
                    self._post(workbook, project, metadata)
                    record, = self._manifest(project)["exports"]
                    self._assert_real_export(
                        project, record, member=RETAINED, count=23,
                        columns=["snapshot_" + field[1] for field in NATIVE_FIELDS])
                    text = self._tables(project)["Snapshot Model"]
                    self.assertIn(record["filename"], text)
                    self.assertIn("Migration_HyperIdentity", text)
                    self.assertIn("Renamed Hyper Columns", text)
                    self.assertNotIn("Migration.HyperExportRequired", text)
                    self.assertNotIn("PostgreSQL.Database", text)
                    self.assertNotIn("Excel.Workbook", text)
                    self.assertEqual(re.findall(r"(?m)^\s*mode:\s+(\w+)\s*$", text),
                                     ["import"])
                    evidence = migrate._require_hyper_import(project, extract_dir=metadata)
                    self.assertEqual(evidence["status"], "static_verified",
                                     evidence["diagnostics"])
                    self.assertEqual(evidence["source_decisions"], [{
                        "datasource": "Synthetic Native Database",
                        "retained": "hyper_snapshot", "not_used": "original_connection",
                        "connectors": connectors,
                    }])
                    self._assert_decision_privacy(evidence)
                self.assertIn(RETAINED, reads)

    def test_retained_native_hyper_still_requires_a_full_reader(self):
        workbook = self._package(
            "no-reader", [_native_database_xml(), _snapshot_only_xml()],
            {IGNORED: UNREADABLE_HYPER, RETAINED: b"HyPe" + b"\x00" * 64})
        with _hyper_io_guard([IGNORED]):
            metadata, extracted = self._extract(workbook, "no-reader")
            project = self._generate(metadata, extracted, "no-reader")
            with self.assertRaises(ExtractionError) as raised:
                self._post(workbook, project, metadata)
            self.assertEqual(raised.exception.source, "hyper_export")
            self.assertIn("tableauhyperapi", str(raised.exception))
            self.assertIn("Metadata/sample readers cannot supply", str(raised.exception))
            self.assertFalse((Path(project) / "Data" / "hyper_exports.json").exists())
            with self.assertRaises(GenerationError) as gate:
                migrate._require_hyper_import(project, extract_dir=metadata)
            self.assertEqual(gate.exception.source, "hyper_import")

    def test_resolved_caps_still_reject_a_mixed_package_with_a_retained_snapshot(self):
        for origin in ("explicit", "environment"):
            with self.subTest(cap_origin=origin):
                tag = "mixed-cap-" + origin
                workbook, _member = self._mixed(tag)
                with _hyper_io_guard([IGNORED]):
                    metadata, extracted = self._extract(workbook, tag)
                    project = self._generate(metadata, extracted, tag)
                    options = {"hyper_export_max_rows": 1} if origin == "explicit" else {}
                    environment = (
                        {"TTPBI_HYPER_EXPORT_MAX_ROWS": "1"}
                        if origin == "environment" else {})
                    with mock.patch.dict(os.environ, environment), \
                            self.assertRaises(ConfigurationError) as raised:
                        self._post(workbook, project, metadata, **options)
                    self.assertIn("reject resolved row caps", str(raised.exception))
                    self.assertFalse((Path(project) / "Data" / "hyper_exports.json").exists())
                    with self.assertRaises(GenerationError):
                        migrate._require_hyper_import(project, extract_dir=metadata)

    def test_unclaimed_orphan_hyper_is_not_hidden_by_another_datasource_selection(self):
        orphan = "Data/orphan.hyper"
        workbook = self._package("orphan", [_native_database_xml()], {
            IGNORED: UNREADABLE_HYPER,
            orphan: self._sqlite_payload("orphan"),
        })
        with _hyper_io_guard([IGNORED]) as reads:
            metadata, extracted = self._extract(workbook, "orphan")
            self.assertEqual(datasource_extractor.unused_hyper_members(
                extracted["datasources"], workbook), {IGNORED})
            project = self._generate(metadata, extracted, "orphan")
            self._post(workbook, project, metadata)
            record, = self._manifest(project)["exports"]
            self._assert_real_export(
                project, record, member=orphan, count=23, columns=["id", "label"])
            evidence = build_hyper_import_evidence(extracted, project)
            self.assertEqual(evidence["status"], "failed")
            self.assertTrue(evidence["diagnostics"])
            with self.assertRaises(GenerationError) as raised:
                migrate._require_hyper_import(project, extract_dir=metadata)
            self.assertEqual(raised.exception.source, "hyper_import")
        self.assertIn(orphan, reads)
        self.assertNotIn(IGNORED, reads)

    def test_duplicate_retained_export_identity_still_fails_the_real_gate(self):
        workbook, _member = self._mixed("ambiguous")
        with _hyper_io_guard([IGNORED]):
            metadata, extracted = self._extract(workbook, "ambiguous")
            project = self._generate(metadata, extracted, "ambiguous")
            self._post(workbook, project, metadata)
            self.assertEqual(migrate._require_hyper_import(
                project, extract_dir=metadata)["status"], "static_verified")
            manifest = self._manifest(project)
            manifest["exports"].append(copy.deepcopy(manifest["exports"][0]))
            path = Path(project) / "Data" / "hyper_exports.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            evidence = build_hyper_import_evidence(extracted, project)
            self.assertEqual(evidence["status"], "failed")
            self.assertTrue(any("multiple records for one exact physical identity" in item
                                for item in evidence["diagnostics"]), evidence["diagnostics"])
            with self.assertRaises(GenerationError) as raised:
                migrate._require_hyper_import(project, extract_dir=metadata)
            self.assertEqual(raised.exception.source, "hyper_import")


class TestNativeSelectionDoesNotExcuseStaleHyper(NativeDatabaseFixture):
    def test_inventory_label_without_datasource_selection_cannot_disable_the_gate(self):
        workbook = self._package("missing-selection", [_native_database_xml()], {
            IGNORED: UNREADABLE_HYPER,
        })
        with _hyper_io_guard([IGNORED], no_hyper=True):
            metadata, extracted = self._extract(workbook, "missing-selection")
            project = self._generate(metadata, extracted, "missing-selection")
            self._post(workbook, project, metadata)
            self._assert_native_gate(project, metadata, extracted)
            extracted["datasources"][0].pop("source_selection")
            (Path(metadata) / "datasources.json").write_text(
                json.dumps(extracted["datasources"]), encoding="utf-8")
            self._assert_ignored_inventory(extracted)
            evidence = build_hyper_import_evidence(extracted, project)
            self.assertEqual(evidence["status"], "failed")
            self.assertTrue(evidence["diagnostics"])
            self.assertEqual(evidence["source_decisions"], [])
            with self.assertRaises(GenerationError) as raised:
                migrate._require_hyper_import(project, extract_dir=metadata)
            self.assertEqual(raised.exception.source, "hyper_import")

    def test_stale_data_manifest_annotation_and_pending_markers_remain_fail_closed(self):
        cases = ("hyper bytes", "hashed CSV", "hashed Parquet", "manifest",
                 "identity annotation", "pending source marker")
        for index, kind in enumerate(cases):
            with self.subTest(stale_artifact=kind):
                tag = "stale-" + str(index)
                workbook = self._package(tag, [_native_database_xml()], {
                    IGNORED: UNREADABLE_HYPER,
                })
                with _hyper_io_guard([IGNORED], no_hyper=True):
                    metadata, extracted = self._extract(workbook, tag)
                    project = self._generate(metadata, extracted, tag)
                    self._post(workbook, project, metadata)
                    self._assert_native_gate(project, metadata, extracted)
                    data = Path(project) / "Data"
                    data.mkdir(exist_ok=True)
                    if kind == "hyper bytes":
                        artifact = data / "stale.hyper"
                        artifact.write_bytes(UNREADABLE_HYPER)
                    elif kind in {"hashed CSV", "hashed Parquet"}:
                        extension = "csv" if kind == "hashed CSV" else "parquet"
                        artifact = data / ("hyper_" + "a" * 64 + "." + extension)
                        artifact.write_bytes(b"id\n1\n" if extension == "csv" else b"PAR1stalePAR1")
                    elif kind == "manifest":
                        artifact = data / "hyper_exports.json"
                        artifact.write_text(json.dumps({
                            "version": 1, "authoritative": True, "exports": [],
                        }), encoding="utf-8")
                    else:
                        artifact = (
                            Path(project) / (REPORT + ".SemanticModel") / "definition" /
                            "tables" / "Orders Model.tmdl")
                        text = artifact.read_text(encoding="utf-8")
                        if kind == "identity annotation":
                            identity = {
                                "workbook": os.path.abspath(workbook), "archive_member": IGNORED,
                                "schema": "main", "table": SNAPSHOT_TABLE,
                            }
                            text += "\n\tannotation Migration_HyperIdentity = " + json.dumps(identity) + "\n"
                        else:
                            source = f'PostgreSQL.Database("{SERVER}:5432", "{DATABASE}")'
                            self.assertEqual(text.count(source), 1)
                            text = text.replace(
                                source, 'error Error.Record("Migration.HyperExportRequired", '
                                '"stale pending extract", [])')
                        artifact.write_text(text, encoding="utf-8")
                    before = artifact.read_bytes()
                    self._post(workbook, project, metadata)
                    self.assertEqual(artifact.read_bytes(), before)
                    evidence = build_hyper_import_evidence(extracted, project)
                    self.assertEqual(evidence["status"], "failed")
                    self.assertTrue(evidence["diagnostics"], kind)
                    self.assertEqual(evidence["runtime_status"], "not_run")
                    self.assertEqual(evidence["source_decisions"][0]["retained"],
                                     "native_database")
                    with self.assertRaises(GenerationError) as raised:
                        migrate._require_hyper_import(project, extract_dir=metadata)
                    self.assertEqual(raised.exception.source, "hyper_import")


if __name__ == "__main__":
    unittest.main()
