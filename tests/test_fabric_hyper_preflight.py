"""Offline Fabric ingestion preflight and file-routing regressions.

The Hyper identity below is metadata only: no Hyper catalog is opened, no
export is simulated, and no Fabric/Spark runtime is executed.  Generated
Notebook and Dataflow artifacts are inspected as static files.
"""

import io
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "powerbi_import"))

import migrate
from powerbi_import.dataflow_generator import DataflowGenerator
from powerbi_import.errors import PartialGenerationError
from powerbi_import.fabric_project_generator import FabricProjectGenerator
from powerbi_import.fabric_sources import preflight_fabric_ingestion
from powerbi_import.import_to_powerbi import PowerBIImporter
from powerbi_import.notebook_generator import NotebookGenerator


_KIND = "fabric_hyper_snapshot_ingestion"


def _objects(*datasources):
    return {
        "datasources": list(datasources),
        "worksheets": [], "dashboards": [], "calculations": [],
        "parameters": [], "filters": [], "stories": [], "actions": [],
        "sets": [], "groups": [], "bins": [], "hierarchies": [],
        "sort_orders": [], "aliases": {}, "custom_sql": [], "user_filters": [],
    }


def _identity(workbook):
    return {
        "workbook": str(workbook),
        "archive_member": "Data/synthetic.hyper",
        "schema": "Extract",
        "table": "SnapshotRows",
    }


def _active_snapshot(workbook):
    connection = {"type": "Excel", "details": {"filename": "source.xlsx"}}
    return {
        "name": "SnapshotSource",
        "connection": deepcopy(connection),
        "tables": [{
            "name": "Orders",
            "type": "table",
            "source_table": "[Extract].[SnapshotRows]",
            "connection_details": deepcopy(connection),
            "columns": [
                {"name": "OrderID", "datatype": "integer"},
                {"name": "Amount", "datatype": "real"},
            ],
            "hyper_identity": _identity(workbook),
        }],
        "relationships": [],
        "hyper_source_members": ["Data/synthetic.hyper"],
        "materialized_extract": {
            "binding": "single_object_materialized_extract",
            "object_id": "synthetic-object",
            "catalog_validation": "required_at_export",
            "field_mapping_validation": "exact_declared_extract_metadata",
        },
    }


def _file_source(name, connector, filename):
    connection = {"type": connector, "details": {"filename": filename}}
    return {
        "name": name + "Source",
        "connection": deepcopy(connection),
        "tables": [{
            "name": name,
            "type": "table",
            "source_table": filename,
            "connection_details": deepcopy(connection),
            "columns": [
                {"name": "OrderID", "datatype": "integer"},
                {"name": "Amount", "datatype": "real"},
            ],
        }],
        "relationships": [],
    }


def _native_selected(workbook):
    connection = {
        "type": "SQL Server",
        "details": {"server": "synthetic.example.invalid", "database": "fixture"},
    }
    return {
        "name": "NativeSource",
        "connection": deepcopy(connection),
        "tables": [{
            "name": "Orders",
            "type": "table",
            "source_table": "[dbo].[orders]",
            "connection_details": deepcopy(connection),
            "columns": [
                {"name": "OrderID", "datatype": "integer"},
                {"name": "Amount", "datatype": "real"},
            ],
            # A matching historical identity under an explicit native-source
            # selection is not an active Hyper binding.
            "hyper_identity": _identity(workbook),
        }],
        "relationships": [],
        "hyper_source_members": ["Data/synthetic.hyper"],
        "source_selection": {
            "policy": "prefer_native_database",
            "selected": "native_database",
            "workbook": str(workbook),
            "hyper_members": ["Data/synthetic.hyper"],
            "extract_xml": ['<extract enabled="true"/>'],
            "object_extract_contexts_xml": [],
        },
    }


def _tree(path):
    """Include directory creation as well as every pre-existing byte."""
    if not path.exists():
        return None
    result = {}
    for item in path.rglob("*"):
        relative = item.relative_to(path).as_posix()
        result[relative] = None if item.is_dir() else item.read_bytes()
    return result


def _sentinels(project, name):
    files = {
        f"{name}.pbip": b"existing PBIP marker\x00",
        "Data/orders.csv": b"old,sentinel\r\n1,keep\r\n",
        f"{name}.Dataflow/mashup.pq": b"existing dataflow bytes",
        f"{name}.Notebook/artifact.content.ipynb": b"existing notebook bytes",
        f"{name}.SemanticModel/definition/model.tmdl": b"existing model bytes",
    }
    for relative, content in files.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return files


def _notebook_source(path):
    notebook = json.loads(path.read_text(encoding="utf-8"))
    return "".join(
        "".join(cell.get("source", [])) for cell in notebook["cells"]
        if cell.get("cell_type") == "code"
    )


class _OfflineFabricFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fabric-hyper-preflight-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for key in ("TTPBI_UPLOAD_ROOT", "TTPBI_EXTRACT_DIR",
                    "TTPBI_COMPOSITE_RELATIONSHIP_POLICIES",
                    "TTPBI_LOGICAL_RELATIONSHIP_POLICIES"):
            os.environ.pop(key, None)

        for target, attribute in (
            (socket.socket, "connect"), (socket.socket, "connect_ex"),
            (socket, "create_connection"), (socket, "getaddrinfo"),
            (subprocess, "Popen"),
        ):
            patcher = mock.patch.object(
                target, attribute, side_effect=AssertionError("Offline test only"))
            guarded = patcher.start()
            self.addCleanup(patcher.stop)
            self.addCleanup(guarded.assert_not_called)

    def assert_hyper_error(self, error, *, source="SnapshotSource", table="Orders"):
        self.assertEqual(error.recovery_kind, _KIND)
        self.assertEqual(error.source, source)
        # A datasource-wide marker may govern multiple tables, so the exact
        # source and reason suffice when no single item can be selected.
        if error.item is not None:
            self.assertEqual(error.item, table)
        self.assertRegex(str(error), r"(?i)fabric.*hyper")
        self.assertRegex(
            " ".join(error.required_evidence), r"(?i)(complete|full|unsampled)")
        self.assertRegex(
            " ".join(error.required_evidence), r"(?i)(column|lineage|mapping)")


class TestFabricDirectHyperPreflight(_OfflineFabricFixture):
    def generate(self, target, out, objects):
        if target == "project":
            return FabricProjectGenerator(str(out)).generate_project("Demo", objects)
        project = out / "Demo"
        if target == "dataflow":
            return DataflowGenerator(str(project), "Demo").generate(objects)
        return NotebookGenerator(str(project), "Demo").generate(objects)

    def datasource_with_context(self, location, context):
        datasource = _file_source("Orders", "CSV", "orders.csv")
        if location == "source-selection":
            datasource["source_selection"] = context
        elif location == "inactive":
            datasource["inactive_extract_context"] = context
        else:
            datasource["relation_metadata"] = {
                "version": 1, "physical_relations": [],
                "logical_tables": [], "logical_relationships": [],
                "extract_context_provenance": context,
            }
        return datasource

    def test_unsafe_and_malformed_retained_xml_is_refused_before_any_write(self):
        secret_marker = "unsafe-fragment-private-sentinel"
        external_dtd = (
            '<!DOCTYPE extract [<!ENTITY local SYSTEM '
            '"file:///nonexistent-test-only/sensitive.txt">]>'
            f'<extract enabled="false" marker="{secret_marker}">&local;</extract>'
        )
        late_dtd = (
            " " * 4097
            + '<!DOCTYPE extract [<!ENTITY value "unsafe-fragment-private-sentinel">]>'
              '<extract enabled="false">&value;</extract>'
        )
        self.assertNotIn("<!DOCTYPE", late_dtd[:4096])
        cases = (
            ("mismatched-extract", "extract_xml",
             '<extract enabled="false"><connection></extract>'),
            ("wrong-root", "extract_xml", '<not-an-extract/>'),
            ("doctype-without-entity", "extract_xml",
             '<!DOCTYPE extract><extract enabled="false"/>'),
            ("external-entity", "extract_xml", external_dtd),
            ("late-dtd", "extract_xml", late_dtd),
            ("mismatched-object", "object_extract_contexts_xml",
             '<properties context="extract"><object></properties>'),
            ("entity-in-object", "object_extract_contexts_xml",
             '<!DOCTYPE properties [<!ENTITY value "unsafe-fragment-private-sentinel">]>'
             '<properties context="extract">&value;</properties>'),
        )
        for target in ("project", "dataflow", "notebook"):
            for existing in (False, True):
                out = self.root / f"xml-{target}-{existing}"
                if existing:
                    _sentinels(out / "Demo", "Demo")
                before = _tree(out)
                for location in ("source-selection", "inactive", "physical-provenance"):
                    for label, key, fragment in cases:
                        with self.subTest(target=target, existing=existing,
                                          location=location, xml=label):
                            context = {"extract_xml": [],
                                       "object_extract_contexts_xml": []}
                            context[key] = [fragment]
                            objects = _objects(self.datasource_with_context(
                                location, context))
                            original = deepcopy(objects)
                            with self.assertRaises(PartialGenerationError) as raised:
                                self.generate(target, out, objects)
                            self.assert_hyper_error(raised.exception, source="OrdersSource")
                            self.assertIsNone(raised.exception.item)
                            self.assertIn(f"{location} extract context",
                                          str(raised.exception).lower())
                            self.assertRegex(str(raised.exception),
                                             r"(?i)unsafe or malformed or exceeds")
                            self.assertNotIn(secret_marker, str(raised.exception))
                            self.assertEqual(objects, original)
                            self.assertEqual(_tree(out), before)

    def test_aggregate_xml_limit_is_enforced_before_any_write(self):
        # Bound the test to a 64-byte limit: every complete fragment is below
        # it, while their aggregate is above it.  No large allocation is needed.
        context = {
            "extract_xml": ['<extract enabled="false"/>' for _ in range(3)],
            "object_extract_contexts_xml": ['<properties context="extract"/>'],
        }
        fragments = context["extract_xml"] + context["object_extract_contexts_xml"]
        self.assertTrue(all(len(fragment) <= 64 for fragment in fragments))
        self.assertGreater(sum(map(len, fragments)), 64)
        with mock.patch("powerbi_import.fabric_sources.MAX_XML_SIZE_MB",
                        64 / (1024 * 1024)):
            for target in ("project", "dataflow", "notebook"):
                for existing in (False, True):
                    out = self.root / f"xml-limit-{target}-{existing}"
                    if existing:
                        _sentinels(out / "Demo", "Demo")
                    before = _tree(out)
                    for location in ("source-selection", "inactive", "physical-provenance"):
                        with self.subTest(target=target, existing=existing,
                                          location=location):
                            objects = _objects(self.datasource_with_context(
                                location, deepcopy(context)))
                            original = deepcopy(objects)
                            with self.assertRaises(PartialGenerationError) as raised:
                                self.generate(target, out, objects)
                            self.assert_hyper_error(raised.exception, source="OrdersSource")
                            self.assertIn("xml size limit", str(raised.exception).lower())
                            self.assertEqual(objects, original)
                            self.assertEqual(_tree(out), before)

    def test_complete_native_bindings_with_and_without_tables_are_read_only(self):
        _sentinels(self.root / "existing" / "Demo", "Demo")
        before = _tree(self.root)
        for shape in ("table-override", "inherited", "no-tables", "connection-list"):
            with self.subTest(shape=shape):
                datasource = _native_selected(self.root / "native.twbx")
                if shape == "inherited":
                    del datasource["tables"][0]["connection_details"]
                elif shape in ("no-tables", "connection-list"):
                    datasource["tables"] = []
                    if shape == "connection-list":
                        datasource["connections"] = [datasource.pop("connection")]
                original = deepcopy(datasource)
                self.assertEqual(preflight_fabric_ingestion([datasource]), 0)
                self.assertEqual(datasource, original)
                self.assertEqual(_tree(self.root), before)

    def test_incomplete_native_bindings_with_and_without_tables_do_not_write(self):
        for tables_present in (True, False):
            for problem in ("missing-server", "missing-database", "blank-server",
                            "file-is-not-database", "unsupported-connector"):
                for target in ("project", "dataflow", "notebook"):
                    with self.subTest(tables_present=tables_present,
                                      problem=problem, target=target):
                        datasource = _native_selected(self.root / "native.twbx")
                        if not tables_present:
                            datasource["tables"] = []
                        connection = datasource["connection"]
                        if problem == "missing-server":
                            del connection["details"]["server"]
                        elif problem == "missing-database":
                            del connection["details"]["database"]
                        elif problem == "blank-server":
                            connection["details"]["server"] = "  "
                        elif problem == "file-is-not-database":
                            connection["type"] = "CSV"
                            connection["details"]["filename"] = "orders.csv"
                        else:
                            connection["type"] = "NotARealDatabase"
                        if tables_present:
                            datasource["tables"][0]["connection_details"] = deepcopy(connection)
                        objects = _objects(datasource)
                        original = deepcopy(objects)
                        out = self.root / f"unbound-{tables_present}-{problem}-{target}"
                        _sentinels(out / "Demo", "Demo")
                        before = _tree(out)
                        with self.assertRaises(PartialGenerationError) as raised:
                            self.generate(target, out, objects)
                        self.assert_hyper_error(raised.exception, source="NativeSource")
                        self.assertIn("no proven native database decision",
                                      str(raised.exception).lower())
                        self.assertEqual(objects, original)
                        self.assertEqual(_tree(out), before)

    def test_table_specific_incomplete_native_binding_and_identity_mismatch_are_refused(self):
        for problem in ("table-missing-database", "identity-mismatch"):
            for target in ("project", "dataflow", "notebook"):
                with self.subTest(problem=problem, target=target):
                    datasource = _native_selected(self.root / "native.twbx")
                    table = datasource["tables"][0]
                    if problem == "table-missing-database":
                        del table["connection_details"]["details"]["database"]
                        self.assertIn("database", datasource["connection"]["details"])
                    else:
                        table["hyper_identity"]["archive_member"] = "Data/other.hyper"
                    objects = _objects(datasource)
                    original = deepcopy(objects)
                    out = self.root / f"table-native-{problem}-{target}"
                    _sentinels(out / "Demo", "Demo")
                    before = _tree(out)
                    with self.assertRaises(PartialGenerationError) as raised:
                        self.generate(target, out, objects)
                    self.assert_hyper_error(raised.exception, source="NativeSource")
                    self.assertEqual(objects, original)
                    self.assertEqual(_tree(out), before)

    def test_active_identity_is_refused_before_any_generator_writes(self):
        for target in ("project", "dataflow", "notebook"):
            for existing in (False, True):
                with self.subTest(target=target, existing=existing):
                    out = self.root / (target + str(existing))
                    project = out / "Demo"
                    if existing:
                        _sentinels(project, "Demo")
                    before = _tree(project)
                    objects = _objects(_active_snapshot(self.root / "source.twbx"))
                    original = deepcopy(objects)
                    with self.assertRaises(PartialGenerationError) as raised:
                        self.generate(target, out, objects)
                    self.assert_hyper_error(raised.exception)
                    self.assertIn("materialized extract", str(raised.exception).lower())
                    self.assertEqual(objects, original)
                    self.assertEqual(_tree(project), before)

    def test_table_identity_without_datasource_marker_is_refused_with_table_context(self):
        datasource = _active_snapshot(self.root / "source.twbx")
        del datasource["materialized_extract"]
        with self.assertRaises(PartialGenerationError) as raised:
            self.generate("project", self.root / "table-identity", _objects(datasource))
        self.assert_hyper_error(raised.exception)
        self.assertEqual(raised.exception.item, "Orders")
        self.assertIsNone(_tree(self.root / "table-identity" / "Demo"))

    def test_hyper_connectors_without_table_identity_are_also_refused(self):
        for connector in ("hyper", "extract", "dataengine"):
            for target in ("project", "dataflow", "notebook"):
                with self.subTest(connector=connector, target=target):
                    datasource = _active_snapshot(self.root / "source.twbx")
                    datasource.pop("materialized_extract")
                    table = datasource["tables"][0]
                    table.pop("hyper_identity")
                    connection = {"type": connector, "details": {}}
                    datasource["connection"] = deepcopy(connection)
                    table["connection_details"] = deepcopy(connection)
                    out = self.root / (connector + "-" + target)
                    project = out / "Demo"
                    before = _tree(project)
                    with self.assertRaises(PartialGenerationError) as raised:
                        self.generate(target, out, _objects(datasource))
                    self.assert_hyper_error(raised.exception)
                    self.assertEqual(raised.exception.item, "Orders")
                    self.assertEqual(_tree(project), before)

    def test_datasource_materialized_extract_without_table_identity_is_not_silently_loaded(self):
        for target in ("project", "dataflow", "notebook"):
            with self.subTest(target=target):
                datasource = _active_snapshot(self.root / "source.twbx")
                del datasource["tables"][0]["hyper_identity"]
                self.assertEqual(
                    datasource["materialized_extract"]["binding"],
                    "single_object_materialized_extract")
                out = self.root / ("datasource-only-" + target)
                project = out / "Demo"
                _sentinels(project, "Demo")
                before = _tree(project)
                with self.assertRaises(PartialGenerationError) as raised:
                    self.generate(target, out, _objects(datasource))
                self.assert_hyper_error(raised.exception)
                self.assertIn("materialized extract", str(raised.exception).lower())
                self.assertEqual(_tree(project), before)

    def test_datasource_only_hyper_metadata_or_active_context_is_refused(self):
        for marker in ("field-metadata", "active-context"):
            for target in ("project", "dataflow", "notebook"):
                with self.subTest(marker=marker, target=target):
                    datasource = _active_snapshot(self.root / "source.twbx")
                    del datasource["tables"][0]["hyper_identity"]
                    del datasource["materialized_extract"]
                    if marker == "field-metadata":
                        datasource["hyper_extract_metadata"] = {
                            "field_local_name_map": {"OrderID": "OrderID"},
                        }
                    else:
                        datasource["relation_metadata"] = {
                            "version": 1,
                            "physical_relations": [],
                            "logical_tables": [],
                            "logical_relationships": [],
                            "extract_context_provenance": {
                                "extract_xml": [
                                    '<extract enabled="true"><connection class="hyper" '
                                    'dbname="Data/synthetic.hyper"/></extract>',
                                ],
                                "object_extract_contexts_xml": [],
                            },
                        }
                    out = self.root / (marker + "-" + target)
                    project = out / "Demo"
                    _sentinels(project, "Demo")
                    before = _tree(project)
                    with self.assertRaises(PartialGenerationError) as raised:
                        self.generate(target, out, _objects(datasource))
                    self.assert_hyper_error(raised.exception)
                    self.assertRegex(str(raised.exception), r"(?i)(metadata|context)")
                    self.assertEqual(_tree(project), before)

    def test_real_extracted_csv_object_graph_with_active_context_but_no_identity_is_refused(self):
        from tableau_export.datasource_extractor import extract_datasource
        from tests.test_csv_partition_wiring import TestHyperPlaceholderHandshakeRegressions

        # Reuse the established multi-object Tableau XML shape, with one
        # unpaired extract context.  No Hyper catalog or export is fabricated.
        xml = TestHyperPlaceholderHandshakeRegressions()._multi_object_graph_datasource(
            2, unpair_last=True)
        workbook = self.root / "source.twb"
        workbook.write_text("<workbook/>", encoding="utf-8")
        datasource = extract_datasource(xml, str(workbook))
        self.assertEqual(datasource["connection"]["type"], "CSV")
        self.assertGreaterEqual(len(datasource["tables"]), 2)
        self.assertTrue(all(
            "hyper_identity" not in table for table in datasource["tables"]))
        provenance = datasource["relation_metadata"]["extract_context_provenance"]
        # The retained XML, not this misleading selection label, proves
        # that the extract declaration is enabled.
        self.assertEqual(provenance["selection"], "native_no_enabled_extract")
        self.assertIn('enabled="true"', provenance["extract_xml"][0])
        self.assertIn('class="hyper"', provenance["extract_xml"][0])
        self.assertTrue(provenance["object_extract_contexts_xml"])

        for target in ("project", "dataflow", "notebook"):
            with self.subTest(target=target):
                out = self.root / ("real-extract-" + target)
                project = out / "Demo"
                _sentinels(project, "Demo")
                before = _tree(project)
                objects = _objects(deepcopy(datasource))
                original = deepcopy(objects)
                with self.assertRaises(PartialGenerationError) as raised:
                    self.generate(target, out, objects)
                self.assert_hyper_error(raised.exception, source="multi-object-source")
                self.assertIn("extract context", str(raised.exception).lower())
                self.assertEqual(objects, original)
                self.assertEqual(_tree(project), before)

    def test_mixed_file_and_hyper_tables_are_refused_in_either_order(self):
        for hyper_first in (True, False):
            with self.subTest(hyper_first=hyper_first):
                datasource = _file_source("Local", "CSV", "orders.csv")
                datasource["name"] = "MixedSource"
                hyper = deepcopy(datasource["tables"][0])
                hyper["name"] = "Snapshot"
                hyper["connection_details"] = {"type": "hyper", "details": {}}
                if hyper_first:
                    datasource["tables"].insert(0, hyper)
                else:
                    datasource["tables"].append(hyper)
                out = self.root / ("mixed-" + str(hyper_first))
                project = out / "Demo"
                _sentinels(project, "Demo")
                before = _tree(project)
                with self.assertRaises(PartialGenerationError) as raised:
                    self.generate("project", out, _objects(datasource))
                self.assert_hyper_error(raised.exception, source="MixedSource",
                                        table="Snapshot")
                self.assertEqual(raised.exception.item, "Snapshot")
                self.assertEqual(_tree(project), before)

    def test_hyper_datasource_without_tables_is_refused_before_any_generator_writes(self):
        datasource = {
            "name": "EmptyHyper", "connection": {"type": "hyper", "details": {}},
            "tables": [], "relationships": [],
        }
        for target in ("project", "dataflow", "notebook"):
            with self.subTest(target=target):
                out = self.root / ("empty-hyper-" + target)
                project = out / "Demo"
                _sentinels(project, "Demo")
                before = _tree(project)
                with self.assertRaises(PartialGenerationError) as raised:
                    self.generate(target, out, _objects(datasource))
                self.assert_hyper_error(raised.exception, source="EmptyHyper")
                self.assertIsNone(raised.exception.item)
                self.assertIn("no table", str(raised.exception).lower())
                self.assertEqual(_tree(project), before)

    def test_native_selection_with_matching_historical_identity_is_accepted(self):
        datasource = _native_selected(self.root / "native.twbx")
        out = self.root / "native"
        with redirect_stdout(io.StringIO()):
            result = FabricProjectGenerator(str(out)).generate_project(
                "Native", _objects(datasource))
        project = Path(result["project_path"])
        self.assertEqual(result["artifacts"]["dataflow"]["queries"], 1)
        mashup = (project / "Native.Dataflow" / "mashup.pq").read_text(
            encoding="utf-8")
        self.assertIn("Sql.Database(", mashup)
        self.assertIn("synthetic.example.invalid", mashup)
        notebook = _notebook_source(
            project / "Native.Notebook" / "artifact.content.ipynb")
        self.assertNotIn('load("Files/orders.csv")', notebook)
        self.assertEqual(result["quality"]["deployment"], "not_run")

    def test_explicit_file_binding_and_historical_metadata_use_canonical_csv_paths(self):
        datasources = []
        expected = []
        for name, connector, filename in (
            ("CSVOrders", "CSV", "original-one.csv"),
            ("MixedCSV", "cSv", "original-two.csv"),
            ("ExcelOrders", "Excel", "original-three.xlsx"),
            ("MixedExcel", "eXcEl", "original-four.xlsx"),
        ):
            datasource = _file_source(name, connector, filename)
            datasource["hyper_source_members"] = ["Data/old.hyper"]
            datasource["inactive_extract_context"] = {
                "selection": "native_no_enabled_extract",
                "extract_xml": ['<extract enabled="false"/>'],
                "object_extract_contexts_xml": [],
            }
            datasource["relation_metadata"] = {
                "version": 1, "physical_relations": [],
                "logical_tables": [], "logical_relationships": [],
                "extract_context_provenance": {
                    "extract_xml": [
                        '<extract enabled="false"><connection class="hyper" '
                        'dbname="Data/old.hyper"/></extract>',
                    ],
                    "object_extract_contexts_xml": [],
                },
            }
            self.assertNotIn("hyper_identity", datasource["tables"][0])
            datasources.append(datasource)
            expected.append(f'Files/{name.lower()}.csv')
        out = self.root / "files"
        with redirect_stdout(io.StringIO()):
            result = FabricProjectGenerator(str(out)).generate_project(
                "Files", _objects(*datasources))
        project = Path(result["project_path"])
        self.assertEqual(result["artifacts"]["dataflow"]["queries"], 0)
        notebook = _notebook_source(
            project / "Files.Notebook" / "artifact.content.ipynb")
        self.assertEqual(notebook.count('.format("csv")'), len(expected))
        for path in expected:
            self.assertIn(f'.load("{path}")', notebook)
        self.assertNotIn("original-one.csv", notebook)
        self.assertNotIn("original-three.xlsx", notebook)
        self.assertNotIn("spark.excel", notebook)
        self.assertEqual(result["quality"]["deployment"], "not_run")


class TestFabricSharedHyperPreflight(_OfflineFabricFixture):
    def test_active_source_is_refused_before_merge_can_discard_it_in_both_orders(self):
        active = _active_snapshot(self.root / "source.twbx")
        native = deepcopy(active)
        native["name"] = "ExplicitFileSource"
        native.pop("materialized_extract")
        native.pop("hyper_source_members")
        native["tables"][0].pop("hyper_identity")
        native["tables"][0]["source_table"] = "[Orders$]"
        active_objects = _objects(active)
        native_objects = _objects(native)

        for active_first in (True, False):
            with self.subTest(active_first=active_first):
                workbooks = [deepcopy(active_objects), deepcopy(native_objects)]
                names = ["Snapshot", "File"]
                if not active_first:
                    workbooks.reverse()
                    names.reverse()
                original = deepcopy(workbooks)
                out = self.root / ("shared-" + str(active_first))
                project = out / "Shared"
                _sentinels(project, "Shared")
                before = _tree(project)
                real_generate = FabricProjectGenerator.generate_project
                with mock.patch.object(
                        FabricProjectGenerator, "generate_project", autospec=True,
                        side_effect=real_generate) as generate, \
                        redirect_stdout(io.StringIO()):
                    with self.assertRaises(PartialGenerationError) as raised:
                        PowerBIImporter().import_shared_model(
                            model_name="Shared",
                            all_converted_objects=workbooks,
                            workbook_names=names,
                            output_dir=str(out),
                            output_format="fabric",
                            force_merge=True,
                        )
                self.assert_hyper_error(raised.exception)
                generate.assert_not_called()
                self.assertEqual(workbooks, original)
                self.assertEqual(_tree(project), before)


class TestFabricEntrypointHyperPreflight(_OfflineFabricFixture):
    def prepare_inputs(self, case, *, active):
        source = self.root / case / "input"
        source.mkdir(parents=True)
        workbook = source / "Demo.twb"
        workbook.write_text('<workbook version="2024.1"/>', encoding="utf-8")
        csv_bytes = b"OrderID,Amount\r\n1,12.5\r\n"
        (source / "orders.csv").write_bytes(csv_bytes)
        local = _file_source("LocalCSV" if active else "orders", "CSV", "orders.csv")
        datasources = [_active_snapshot(workbook), local] if active else [local]
        extraction = self.root / case / "extracted"
        extraction.mkdir()
        (extraction / "datasources.json").write_text(
            json.dumps(datasources), encoding="utf-8")
        os.environ["TTPBI_EXTRACT_DIR"] = str(extraction)
        return workbook, csv_bytes, _tree(extraction)

    def run_entrypoint(self, route, workbook, out):
        common = [
            "--output-dir", str(out), "--output-format", "fabric",
            "--skip-extraction", "--no-verify-open", "--quiet",
        ]
        if route == "single":
            args = migrate._build_argument_parser().parse_args(
                [str(workbook), *common])
            return migrate._run_single_migration(args)
        if route == "batch":
            argv = ["migrate", "--batch", str(workbook.parent), *common]
        else:
            config = workbook.parent.parent / "batch.json"
            config.write_text(json.dumps([{"file": str(workbook)}]), encoding="utf-8")
            argv = ["migrate", "--batch-config", str(config), *common]
        with mock.patch.object(sys, "argv", argv):
            return migrate.main()

    def test_active_hyper_is_refused_before_local_file_staging_or_project_mutation(self):
        for route in ("single", "batch", "batch-config"):
            for existing in (False, True):
                with self.subTest(route=route, existing=existing):
                    case = f"{route}-{existing}"
                    workbook, csv_bytes, extraction_before = self.prepare_inputs(
                        case, active=True)
                    out = self.root / case / "out"
                    project = out / "Demo"
                    if existing:
                        _sentinels(project, "Demo")
                    before = _tree(project)
                    stream = io.StringIO()
                    handler = logging.StreamHandler(stream)
                    migrate.logger.addHandler(handler)
                    try:
                        with mock.patch.object(
                            migrate, "_stage_declared_local_sources",
                            wraps=migrate._stage_declared_local_sources,
                        ) as stage, redirect_stdout(stream):
                            result = self.run_entrypoint(route, workbook, out)
                    finally:
                        migrate.logger.removeHandler(handler)
                    self.assertNotEqual(result, migrate.ExitCode.SUCCESS, stream.getvalue())
                    # Do not let the temporary directory name containing
                    # "fabric-hyper" stand in for an actual diagnostic.
                    self.assertRegex(
                        stream.getvalue(),
                        r"(?i)fabric (?:cannot|requires|refus\w*|unsupported).{0,100}\bhyper\b")
                    self.assertIn("SnapshotSource", stream.getvalue())
                    stage.assert_not_called()
                    self.assertEqual(_tree(project), before)
                    self.assertEqual((workbook.parent / "orders.csv").read_bytes(), csv_bytes)
                    self.assertEqual(
                        _tree(self.root / case / "extracted"), extraction_before)

    def test_actual_cli_batch_stages_a_valid_csv_without_hyper(self):
        workbook, csv_bytes, extraction_before = self.prepare_inputs(
            "native-csv", active=False)
        out = self.root / "native-csv" / "out"
        stream = io.StringIO()
        with mock.patch.object(
            migrate, "_stage_declared_local_sources",
            wraps=migrate._stage_declared_local_sources,
        ) as stage, redirect_stdout(stream):
            result = self.run_entrypoint("batch", workbook, out)
        self.assertEqual(result, migrate.ExitCode.SUCCESS, stream.getvalue())
        stage.assert_called_once()
        project = out / "Demo"
        self.assertEqual((project / "Data" / "orders.csv").read_bytes(), csv_bytes)
        notebook = _notebook_source(
            project / "Demo.Notebook" / "artifact.content.ipynb")
        self.assertIn('load("Files/orders.csv")', notebook)
        self.assertEqual(_tree(self.root / "native-csv" / "extracted"), extraction_before)


class TestFabricReportlessQuality(_OfflineFabricFixture):
    def generate_reportless(self):
        datasource = _file_source("orders", "CSV", "orders.csv")
        datasource["relation_metadata"] = {
            "version": 1,
            "physical_relations": [{"type": "table", "name": "orders",
                                    "children": []}],
            "logical_tables": [], "logical_relationships": [],
        }
        extracted = _objects(datasource)
        # Source bytes are real; no Spark execution or data refresh is claimed.
        (self.root / "orders.csv").write_bytes(b"OrderID,Amount\n1,12.5\n")
        with redirect_stdout(io.StringIO()):
            generated = FabricProjectGenerator(str(self.root / "out")).generate_project(
                "Demo", extracted, include_report=False)
        self.assertTrue(generated["validation"]["valid"])
        self.assertEqual(generated["validation"]["artifacts_checked"], 5)
        return extracted, Path(generated["project_path"])

    def test_real_five_artifact_bundle_reports_unrun_pbir_and_consistent_evidence(self):
        from powerbi_import.fabric_evidence import build_fabric_evidence
        from powerbi_import.migration_quality import build_quality_report

        extracted, project = self.generate_reportless()
        report = build_quality_report(extracted, str(project), "Demo")
        self.assertEqual(report.status, "WARN")
        self.assertEqual(report.handoff_status, "WARN")
        self.assertFalse(report.blockers)
        self.assertTrue(report.fabric["valid"])
        self.assertEqual(report.fabric["artifacts_checked"], 5)
        self.assertEqual(report.openability["scope"], "fabric_model_only")
        self.assertIsNone(report.openability["openable"])
        self.assertEqual(report.openability["pbip_validation"], "not_run")
        self.assertEqual(report.visual_parity["status"], "not_run")
        self.assertEqual(report.roundtrip_validation["status"], "not_run")
        self.assertEqual(report.roundtrip_validation["desktop"]["status"], "not_run")

        quality_evidence = report.fabric_evidence
        standalone = json.loads((project / "fabric_evidence.json").read_text(
            encoding="utf-8"))
        rebuilt = build_fabric_evidence(str(project), "Demo")
        for label, evidence in (
            ("quality", quality_evidence),
            ("standalone", standalone),
            ("rebuilt", rebuilt),
        ):
            with self.subTest(evidence=label):
                self.assertEqual(evidence["status"], "locally_valid")
                self.assertTrue(evidence["validation"]["valid"])
                self.assertEqual(evidence["validation"]["artifacts_checked"], 5)
                self.assertEqual(evidence["artifacts"]["Report"]["status"], "not_run")
                self.assertEqual(evidence["runtime"]["deployment"], "not_run")
                self.assertEqual(evidence["runtime"]["semantic_execution"], "not_run")

    def test_missing_artifact_incomplete_pbip_and_contradictory_models_fail(self):
        from powerbi_import.fabric_evidence import build_fabric_evidence
        from powerbi_import.migration_quality import build_quality_report

        extracted, original = self.generate_reportless()
        for problem in ("missing-pipeline", "incomplete-pbip",
                        "contradictory-metadata", "multiple-models"):
            with self.subTest(problem=problem):
                project = self.root / problem / "Demo"
                shutil.copytree(original, project)
                if problem == "missing-pipeline":
                    shutil.rmtree(project / "Demo.Pipeline")
                elif problem == "incomplete-pbip":
                    (project / "Demo.pbip").write_text("{}", encoding="utf-8")
                elif problem == "contradictory-metadata":
                    metadata_file = project / "fabric_project_metadata.json"
                    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
                    metadata["artifacts"]["report"] = {}
                    metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
                else:
                    shutil.copytree(project / "Demo.SemanticModel",
                                    project / "Other.SemanticModel")
                report = build_quality_report(extracted, str(project), "Demo")
                self.assertEqual(report.status, "FAIL", report.to_dict())
                self.assertTrue(report.blockers)
                standalone = build_fabric_evidence(str(project), "Demo")
                self.assertEqual(standalone["status"], "invalid")
                self.assertIs(standalone["validation"]["valid"], False)
