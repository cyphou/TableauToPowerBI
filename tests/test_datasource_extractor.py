"""
Tests for datasource_extractor.py — Sprint 27 coverage push.

Targets uncovered lines:
  - _detect_csv_delimiter()               lines 22-41
  - _read_csv_header_from_twbx()          lines 44-71
  - _parse_connection_class() textscan    lines 191-197
  - _parse_connection_class() fallback    line 212
  - _build_connection_map()               lines 246-296
  - extract_tables_with_columns() phases  lines 330-474
  - extract_relationships()               lines 550-643+
"""

import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tableau_export'))

from datasource_extractor import (
    _build_connection_map,
    _detect_csv_delimiter,
    _extract_col_local_name_map,
    _extract_col_type_map,
    _parse_connection_class,
    _read_csv_header_from_twbx,
    _rename_sqlproxy_tables,
    _sanitize_caption_for_table_name,
    extract_calculations,
    extract_column_metadata,
    extract_connection_details,
    extract_datasource,
    extract_relation_metadata,
    extract_relationships,
    extract_tables_with_columns,
)


class TestActiveMaterializedExtractContext(unittest.TestCase):
    """Entirely synthetic native trees paired with one explicit Hyper object."""

    def fixture(self, kind="join"):
        from copy import deepcopy
        ds = ET.Element("datasource", name="Synthetic")
        root = ET.SubElement(ds, "connection", {"class": "federated"})
        tree = ET.SubElement(root, "relation", type=kind)
        if kind == "join":
            tree.set("join", "left")
            clause = ET.SubElement(tree, "clause", type="join")
            comparison = ET.SubElement(clause, "expression", op="=")
            ET.SubElement(comparison, "expression", op="[Left].[ID]")
            ET.SubElement(comparison, "expression", op="[Right].[ID]")
        for alias in ("Left", "Right"):
            named = ET.SubElement(root, "named-connection", name=alias + "-ref")
            ET.SubElement(named, "connection", {
                "class": "excel-direct", "filename": alias + ".xlsx",
                "directory": "Data"})
            relation = ET.SubElement(tree, "relation", {
                "type": "table", "name": alias, "connection": alias + "-ref",
                "table": "[" + alias + "$]"})
            columns = ET.SubElement(relation, "columns")
            ET.SubElement(columns, "column", name=alias + " field",
                          datatype="integer" if alias == "Left" else "string")
        records = ET.SubElement(root, "metadata-records")
        extract = ET.SubElement(ds, "extract", enabled="true")
        ET.SubElement(extract, "filter", {"column": "[Left field]", "class": "quantitative"})
        conn = ET.SubElement(extract, "connection", {
            "class": "hyper", "dbname": "Data/exact.hyper",
            "schema": "Extract", "tablename": "Materialized"})
        physical = ET.SubElement(conn, "relation", {
            "name": "Extract", "type": "table", "table": "[Extract].[Materialized]"})
        physical_columns = ET.SubElement(physical, "columns")
        extract_records = ET.SubElement(conn, "metadata-records")
        maps = ET.SubElement(conn, "cols")
        for alias, dtype in (("Left", "integer"), ("Right", "string")):
            local, remote = alias + " field", alias.lower() + "_physical"
            ET.SubElement(ds, "column", name=f"[{local}]", datatype=dtype)
            ET.SubElement(physical_columns, "column", name=local, datatype=dtype)
            for parent, table, source in (
                (records, alias, alias + "_native"),
                (extract_records, "Extract", remote),
            ):
                record = ET.SubElement(parent, "metadata-record", {"class": "column"})
                for tag, text in (("local-name", f"[{local}]"),
                                  ("remote-name", source),
                                  ("parent-name", f"[{table}]"),
                                  ("local-type", dtype)):
                    ET.SubElement(record, tag).text = text
            ET.SubElement(maps, "map", key=f"[{local}]",
                          value=f"[Extract].[{remote}]")
        graph = ET.SubElement(ds, "object-graph")
        obj = ET.SubElement(ET.SubElement(graph, "objects"), "object",
                            id="logical-101", caption="Logical object")
        ET.SubElement(obj, "properties", context="").append(deepcopy(tree))
        ET.SubElement(obj, "properties", context="extract").append(deepcopy(physical))
        return ds

    def extract(self, ds):
        with tempfile.TemporaryDirectory() as directory:
            archive = os.path.join(directory, "synthetic.twbx")
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("Data/exact.hyper", b"opaque synthetic catalog placeholder")
            result = extract_datasource(ds, archive)
            return result, os.path.abspath(archive)

    def test_join_and_union_flatten_only_with_complete_typed_bijection(self):
        for kind in ("join", "union"):
            with self.subTest(kind=kind):
                ds = self.fixture(kind)
                before = ET.tostring(ds)
                result, workbook = self.extract(ds)
                self.assertEqual(ET.tostring(ds), before)
                self.assertEqual([t["name"] for t in result["tables"]], ["Logical object"])
                table = result["tables"][0]
                self.assertEqual(table["hyper_identity"], {
                    "workbook": workbook, "archive_member": "Data/exact.hyper",
                    "schema": "Extract", "table": "Materialized"})
                self.assertEqual(table["cols_physical_map"], {
                    "left_physical": "Left field", "right_physical": "Right field"})
                self.assertEqual([(c["name"], c["datatype"]) for c in table["columns"]],
                                 [("Left field", "integer"), ("Right field", "string")])
                self.assertEqual(result["col_local_name_map"], {
                    "Left field": "Logical object", "Right field": "Logical object"})
                self.assertEqual(result["relationships"], [])
                provenance = result["materialized_extract"]
                self.assertEqual(provenance["object_id"], "logical-101")
                self.assertEqual(provenance["catalog_validation"], "required_at_export")
                self.assertEqual(provenance["field_mapping_validation"],
                                 "exact_declared_extract_metadata")
                self.assertIn(f'type="{kind}"', provenance["native_context_xml"])
                self.assertTrue(provenance["extract_filters_xml"])
                self.assertIn("native_relation_metadata", provenance)
                self.assertIn("native_col_local_name_map", provenance)

    def test_ambiguous_or_incomplete_context_never_partially_flattens(self):
        def extra_object(ds):
            ET.SubElement(ds.find("object-graph/objects"), "object", id="second")

        def logical_relationship(ds):
            ET.SubElement(ET.SubElement(ds.find("object-graph"), "relationships"),
                          "relationship")

        for label, mutate in (
            ("disabled", lambda ds: ds.find("extract").set("enabled", "false")),
            ("multiple objects", extra_object),
            ("logical relationship", logical_relationship),
            ("missing type", lambda ds: ds.find(
                "extract/connection/metadata-records/metadata-record/local-type").clear()),
            ("wrong table", lambda ds: ds.find("extract/connection").set("tablename", "Other")),
            ("conflicting map", lambda ds: ds.find(
                "extract/connection/cols/map").set("value", "[Extract].[wrong]")),
        ):
            with self.subTest(shape=label):
                ds = self.fixture()
                mutate(ds)
                result, _ = self.extract(ds)
                self.assertNotIn("materialized_extract", result)
                self.assertNotIn("Logical object", [t["name"] for t in result["tables"]])
                for table in result["tables"]:
                    self.assertNotIn("hyper_identity", table)

    def test_disabled_extract_keeps_one_or_two_native_excel_bindings_and_provenance(self):
        for count in (1, 2):
            with self.subTest(native_excel_sources=count):
                ds = self.fixture()
                ds.find("extract").set("enabled", "false")
                if count == 1:
                    root = ds.find("connection")
                    tree = root.find("relation")
                    first = tree.find("relation")
                    root.remove(tree)
                    root.append(first)
                    props = ds.find("object-graph/objects/object/properties")
                    props.clear()
                    props.set("context", "")
                    from copy import deepcopy
                    props.append(deepcopy(first))
                result, _ = self.extract(ds)
                self.assertEqual({t["name"] for t in result["tables"]},
                                 {"Left", "Right"} if count == 2 else {"Left"})
                for table in result["tables"]:
                    self.assertEqual(table["connection_details"]["type"].lower(),
                                     "excel")
                    self.assertNotIn("hyper_identity", table)
                inactive = result["inactive_extract_context"]
                self.assertEqual(inactive["selection"], "native_no_enabled_extract")
                self.assertTrue(inactive["extract_xml"])
                self.assertTrue(inactive["object_extract_contexts_xml"])


class TestRelationMetadata(unittest.TestCase):
    """Source provenance is a predicate tree, not a guessed equality edge."""

    def fixture(self):
        from tests.logical_relationship_fixtures import relation_xml
        return relation_xml()

    def test_complete_composite_equality_and_non_equi_predicate_preserved(self):
        ds = self.fixture()
        metadata = extract_relation_metadata(ds)

        def field(table, column):
            return dict(
                table=table, column=column, remote_column=column,
                reference=f"[{table}].[{column}]")

        expected = {
            "left": {"object_id": "Current", "tables": ["Current"]},
            "right": {"object_id": "Prior", "tables": ["Prior"]},
            "join_type": None,
            "predicate": {
                "operator": "AND", "arguments": [
                    {"operator": operator,
                     "left": field("Current", left), "right": field("Prior", right)}
                    for operator, left, right in (
                        ("=", "C_Key", "P_Key"), ("=", "C_Group", "P_Group"),
                        (">", "C_Time", "P_Time"))
                ],
            },
            "requires_materialization": True,
        }
        self.assertEqual(metadata["version"], 1)
        self.assertEqual(metadata["logical_relationships"], [expected])
        self.assertEqual(metadata["physical_relations"], [])
        self.assertEqual(metadata["logical_tables"], [
            {"object_id": alias, "caption": alias + " display", "relations": [{
                "type": "table", "name": alias,
                "connection": "synthetic_connection", "source_table": "[Events]",
            }]} for alias in ("Current", "Prior")
        ])
        # Never emit the equality subset and silently lose the strict range.
        self.assertEqual(extract_relationships(ds, metadata), [])

    def test_same_source_and_rowid_never_synthesize_a_relationship(self):
        ds = self.fixture()
        graph = ds.find("object-graph")
        graph.remove(graph.find("relationships"))
        for alias in ("Current", "Prior"):
            record = ET.SubElement(ds, "metadata-record", {"class": "column"})
            for name, value in (("local-name", "[RowID]"),
                                ("remote-name", "RowID"),
                                ("parent-name", f"[{alias}]")):
                ET.SubElement(record, name).text = value
        metadata = extract_relation_metadata(ds)
        self.assertEqual(len(metadata["logical_tables"]), 2)
        self.assertEqual(metadata["logical_relationships"], [])
        self.assertEqual(extract_relationships(ds, metadata), [])

    def test_single_explicit_equality_retains_original_aliases_not_captions(self):
        ds = self.fixture()
        rel = ds.find(".//relationship")
        predicate = rel.find("expression")
        first = predicate[0]
        rel.remove(predicate)
        rel.append(first)
        metadata = extract_relation_metadata(ds)
        self.assertFalse(metadata["logical_relationships"][0]["requires_materialization"])
        self.assertEqual(extract_relationships(ds, metadata), [{
            "type": "left", "left": {"table": "Current", "column": "C_Key"},
            "right": {"table": "Prior", "column": "P_Key"},
        }])

    def test_composite_equality_retains_all_legacy_edges_but_requires_materialization(self):
        ds = self.fixture()
        conjunction = ds.find(".//relationship/expression")
        conjunction.remove(conjunction[-1])
        metadata = extract_relation_metadata(ds)
        self.assertTrue(metadata["logical_relationships"][0]["requires_materialization"])
        self.assertEqual(extract_relationships(ds, metadata), [
            {"type": "left", "left": {"table": "Current", "column": left},
             "right": {"table": "Prior", "column": right}}
            for left, right in (("C_Key", "P_Key"), ("C_Group", "P_Group"))
        ])

    def test_reversed_operands_and_local_remote_column_identity_are_not_rebound(self):
        ds = self.fixture()
        # Connector names are distinct from the local Tableau field identities.
        cols = ET.SubElement(ds, "cols")
        for alias, local, remote in (
            ("Current", "C_Time", "time_value"), ("Prior", "P_Time", "time_value"),
        ):
            ET.SubElement(cols, "map", key=f"[{local}]", value=f"[{alias}].[{remote}]")
        comparison = ds.find(".//relationship/expression")[-1]
        comparison.set("op", "<")
        comparison[0].set("op", "[Prior].[time_value]")
        comparison[1].set("op", "[Current].[time_value]")
        metadata = extract_relation_metadata(ds)
        predicate = metadata["logical_relationships"][0]["predicate"]["arguments"][-1]
        self.assertEqual(predicate, {
            "operator": "<",
            "left": {"table": "Prior", "column": "P_Time",
                     "remote_column": "time_value", "reference": "[Prior].[time_value]"},
            "right": {"table": "Current", "column": "C_Time",
                      "remote_column": "time_value", "reference": "[Current].[time_value]"},
        })
        self.assertEqual(extract_relationships(ds, metadata), [])

    def test_active_feature_attributes_and_graph_win_over_compatibility_copy(self):
        ds = self.fixture()
        rel = ds.find(".//relationship")
        comparison = rel.find("expression")[2]
        comparison.set("op", "=")
        comparison.set("feature.true...op", ">=")
        false_graph = ET.SubElement(ds, "feature.false...object-graph")
        ET.SubElement(false_graph, "relationship", expression="[Current].[RowID] = [Prior].[RowID]")
        metadata = extract_relation_metadata(ds)
        self.assertEqual(len(metadata["logical_relationships"]), 1)
        self.assertEqual(metadata["logical_relationships"][0]["predicate"]["arguments"][2]["operator"],
                         ">=")
        self.assertEqual(extract_relationships(ds, metadata), [])

    def test_opaque_or_disjunctive_predicates_never_project_equality_subset(self):
        for operator in ("OR", "unsupported_function"):
            with self.subTest(operator=operator):
                ds = self.fixture()
                ds.find(".//relationship/expression").set("op", operator)
                metadata = extract_relation_metadata(ds)
                rel = metadata["logical_relationships"][0]
                self.assertEqual(rel["predicate"]["operator"], operator)
                self.assertEqual(len(rel["predicate"]["arguments"]), 3)
                self.assertTrue(rel["requires_materialization"])
                self.assertEqual(extract_relationships(ds, metadata), [])
        ds = self.fixture()
        rel = ds.find(".//relationship")
        rel.remove(rel.find("expression"))
        raw = "[Current].[C_Key] = [Prior].[P_Key] AND [Current].[C_Time] > [Prior].[P_Time]"
        rel.set("expression", raw)
        metadata = extract_relation_metadata(ds)
        self.assertEqual(metadata["logical_relationships"][0]["predicate"],
                         {"operator": "", "expression": raw})
        self.assertEqual(extract_relationships(ds, metadata), [])

    def test_physical_join_keeps_order_join_type_and_full_predicate(self):
        ds = self.fixture()
        connection = ET.SubElement(ds, "connection")
        join = ET.SubElement(connection, "relation", type="join", join="left")
        for alias in ("Current", "Prior"):
            ET.SubElement(join, "relation", type="table", name=alias,
                          connection="synthetic_connection", table="[Events]")
        logical = ds.find(".//relationship")
        clause = ET.SubElement(join, "clause")
        clause.append(logical.find("expression"))
        metadata = extract_relation_metadata(ds)
        tree = metadata["physical_relations"][0]
        self.assertEqual(tree["join_type"], "left")
        self.assertEqual([child["name"] for child in tree["children"]], ["Current", "Prior"])
        self.assertEqual(tree["predicate"], metadata["logical_relationships"][0]["predicate"])
        self.assertTrue(tree["requires_materialization"])
        self.assertEqual(extract_relationships(ds, metadata), [])

    def test_public_extractor_includes_provenance_and_none_is_versioned_empty(self):
        ds = self.fixture()
        self.assertEqual(extract_datasource(ds)["relation_metadata"],
                         extract_relation_metadata(ds))
        expected = {"version": 1, "physical_relations": [], "logical_tables": [],
                    "logical_relationships": []}
        self.assertEqual(extract_relation_metadata(None), expected)
        self.assertEqual(extract_datasource(None)["relation_metadata"], expected)

    def test_logical_object_ids_display_captions_and_physical_aliases_are_distinct(self):
        ds = self.fixture()
        ids = {"Current": "object-101", "Prior": "object-202"}
        for obj in ds.findall(".//objects/object"):
            alias = obj.get("id")
            obj.set("id", ids[alias])
            obj.set("caption", f"{alias} friendly display")
        rel = ds.find(".//relationship")
        for endpoint in ("first-end-point", "second-end-point"):
            elem = rel.find(endpoint)
            elem.set("object-id", ids[elem.get("object-id")])
        result = extract_datasource(ds)
        metadata = result["relation_metadata"]
        relationship = metadata["logical_relationships"][0]
        self.assertEqual(relationship["left"],
                         {"object_id": "object-101", "tables": ["Current"]})
        self.assertEqual(relationship["right"],
                         {"object_id": "object-202", "tables": ["Prior"]})
        self.assertEqual(
            [(obj["object_id"], obj["caption"], obj["relations"][0]["name"],
              obj["relations"][0]["source_table"])
             for obj in metadata["logical_tables"]],
            [("object-101", "Current friendly display", "Current", "[Events]"),
             ("object-202", "Prior friendly display", "Prior", "[Events]")])
        self.assertEqual(relationship["predicate"],
                         extract_relation_metadata(self.fixture())[
                             "logical_relationships"][0]["predicate"])
        self.assertTrue(relationship["requires_materialization"])
        self.assertEqual(result["relationships"], [])

    def test_bare_columns_are_disambiguated_by_physical_endpoint_scope_not_object_id(self):
        ds = self.fixture()
        for obj, object_id in zip(ds.findall(".//objects/object"),
                                  ("object-101", "object-202")):
            obj.set("id", object_id)
        rel = ds.find(".//relationship")
        rel.find("first-end-point").set("object-id", "object-101")
        rel.find("second-end-point").set("object-id", "object-202")
        rel.remove(rel.find("expression"))
        comparison = ET.SubElement(rel, "expression", op="=")
        ET.SubElement(comparison, "expression", op="[SharedKey]")
        ET.SubElement(comparison, "expression", op="[SharedKey]")
        records = ET.SubElement(ds, "metadata-records")
        for alias, remote in (("Current", "current_key"), ("Prior", "prior_key")):
            record = ET.SubElement(records, "metadata-record", {"class": "column"})
            for tag, value in (("local-name", "[SharedKey]"),
                               ("parent-name", f"[{alias}]"),
                               ("remote-name", remote)):
                ET.SubElement(record, tag).text = value
        result = extract_datasource(ds)
        relationship = result["relation_metadata"]["logical_relationships"][0]
        self.assertEqual(relationship["predicate"], {
            "operator": "=",
            "left": {"table": "Current", "column": "SharedKey",
                     "remote_column": "current_key", "reference": "[SharedKey]"},
            "right": {"table": "Prior", "column": "SharedKey",
                      "remote_column": "prior_key", "reference": "[SharedKey]"},
        })
        self.assertFalse(relationship["requires_materialization"])
        self.assertEqual(result["relationships"], [{
            "type": "left", "left": {"table": "Current", "column": "SharedKey"},
            "right": {"table": "Prior", "column": "SharedKey"},
        }])

    def test_reversed_qualified_operands_do_not_swap_logical_endpoints(self):
        ds = self.fixture()
        objects = ds.findall(".//objects/object")
        objects[0].set("id", "object-101")
        objects[1].set("id", "object-202")
        rel = ds.find(".//relationship")
        rel.find("first-end-point").set("object-id", "object-101")
        rel.find("second-end-point").set("object-id", "object-202")
        rel.remove(rel.find("expression"))
        comparison = ET.SubElement(rel, "expression", op=">")
        ET.SubElement(comparison, "expression", op="[Prior].[P_Time]")
        ET.SubElement(comparison, "expression", op="[Current].[C_Time]")
        result = extract_datasource(ds)
        relationship = result["relation_metadata"]["logical_relationships"][0]
        self.assertEqual(relationship["left"],
                         {"object_id": "object-101", "tables": ["Current"]})
        self.assertEqual(relationship["right"],
                         {"object_id": "object-202", "tables": ["Prior"]})
        self.assertEqual(relationship["predicate"], {
            "operator": ">",
            "left": {"table": "Prior", "column": "P_Time", "remote_column": "P_Time",
                     "reference": "[Prior].[P_Time]"},
            "right": {"table": "Current", "column": "C_Time", "remote_column": "C_Time",
                      "reference": "[Current].[C_Time]"},
        })
        self.assertTrue(relationship["requires_materialization"])
        self.assertEqual(result["relationships"], [])


# ═══════════════════════════════════════════════════════════════════
# _detect_csv_delimiter
# ═══════════════════════════════════════════════════════════════════

class TestDetectCsvDelimiter(unittest.TestCase):
    """Test CSV delimiter auto-detection."""

    def test_comma_delimiter(self):
        self.assertEqual(_detect_csv_delimiter('Name,Age,City'), ',')

    def test_semicolon_delimiter(self):
        self.assertEqual(_detect_csv_delimiter('Name;Age;City'), ';')

    def test_tab_delimiter(self):
        self.assertEqual(_detect_csv_delimiter('Name\tAge\tCity'), '\t')

    def test_pipe_delimiter(self):
        self.assertEqual(_detect_csv_delimiter('Name|Age|City'), '|')

    def test_empty_returns_comma(self):
        self.assertEqual(_detect_csv_delimiter(''), ',')

    def test_none_returns_comma(self):
        self.assertEqual(_detect_csv_delimiter(None), ',')

    def test_no_delimiter_returns_comma(self):
        self.assertEqual(_detect_csv_delimiter('SingleColumn'), ',')

    def test_mixed_prefers_sniffer_result(self):
        # csv.Sniffer may pick comma over semicolons; just verify we get a result
        result = _detect_csv_delimiter('A;B;C;D,E')
        self.assertIn(result, (',', ';'))


# ═══════════════════════════════════════════════════════════════════
# _read_csv_header_from_twbx
# ═══════════════════════════════════════════════════════════════════

class TestReadCsvHeaderFromTwbx(unittest.TestCase):
    """Test reading CSV header from inside .twbx archives."""

    def test_reads_csv_from_twbx(self):
        with tempfile.TemporaryDirectory() as td:
            twbx = os.path.join(td, 'test.twbx')
            with zipfile.ZipFile(twbx, 'w') as z:
                z.writestr('Data/sales.csv', 'Name,Revenue,Region\nrow1,100,US')
            result = _read_csv_header_from_twbx(twbx, 'Data', 'sales.csv')
            self.assertEqual(result, 'Name,Revenue,Region')

    def test_reads_csv_partial_path_match(self):
        with tempfile.TemporaryDirectory() as td:
            twbx = os.path.join(td, 'test.twbx')
            with zipfile.ZipFile(twbx, 'w') as z:
                z.writestr('some/path/data.csv', 'A;B;C\n1;2;3')
            result = _read_csv_header_from_twbx(twbx, '', 'data.csv')
            self.assertEqual(result, 'A;B;C')

    def test_returns_none_for_nonexistent_file(self):
        result = _read_csv_header_from_twbx('/nonexistent/path.twbx', '', 'x.csv')
        self.assertIsNone(result)

    def test_returns_none_for_non_twbx(self):
        with tempfile.NamedTemporaryFile(suffix='.twb', delete=False) as f:
            f.write(b'<workbook/>')
            path = f.name
        try:
            result = _read_csv_header_from_twbx(path, '', 'x.csv')
            self.assertIsNone(result)
        finally:
            os.unlink(path)

    def test_returns_none_for_none_path(self):
        result = _read_csv_header_from_twbx(None, '', 'x.csv')
        self.assertIsNone(result)

    def test_returns_none_if_csv_not_in_archive(self):
        with tempfile.TemporaryDirectory() as td:
            twbx = os.path.join(td, 'test.twbx')
            with zipfile.ZipFile(twbx, 'w') as z:
                z.writestr('other.twb', '<workbook/>')
            result = _read_csv_header_from_twbx(twbx, '', 'missing.csv')
            self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════
# _parse_connection_class
# ═══════════════════════════════════════════════════════════════════

class TestParseConnectionClass(unittest.TestCase):
    """Test connection class parsing for various connector types."""

    def test_excel_connection(self):
        xml = '<connection class="excel-direct" filename="sales.xlsx" />'
        elem = ET.fromstring(xml)
        result = _parse_connection_class(elem)
        self.assertEqual(result['type'], 'Excel')
        self.assertEqual(result['details']['filename'], 'sales.xlsx')

    def test_excel_with_named_conn(self):
        xml = '<connection class="excel-direct" filename="sales.xlsx" />'
        named = ET.fromstring('<named-connection caption="Sales File" />')
        elem = ET.fromstring(xml)
        result = _parse_connection_class(elem, named_conn=named)
        self.assertEqual(result['details']['caption'], 'Sales File')

    def test_textscan_connection(self):
        xml = '<connection class="textscan" filename="data.csv" directory="/data" separator=";" charset="utf-8" />'
        elem = ET.fromstring(xml)
        result = _parse_connection_class(elem)
        self.assertEqual(result['type'], 'CSV')
        self.assertEqual(result['details']['delimiter'], ';')
        self.assertEqual(result['details']['filename'], 'data.csv')

    def test_textscan_auto_detect_delimiter(self):
        """When no separator attr, delimiter is auto-detected from twbx."""
        xml = '<connection class="textscan" filename="data.csv" directory="Data" />'
        elem = ET.fromstring(xml)
        with tempfile.TemporaryDirectory() as td:
            twbx = os.path.join(td, 'test.twbx')
            with zipfile.ZipFile(twbx, 'w') as z:
                z.writestr('Data/data.csv', 'A\tB\tC\n1\t2\t3')
            result = _parse_connection_class(elem, twbx_path=twbx)
            self.assertEqual(result['type'], 'CSV')
            self.assertEqual(result['details']['delimiter'], '\t')

    def test_textscan_no_twbx_defaults_comma(self):
        xml = '<connection class="textscan" filename="data.csv" />'
        elem = ET.fromstring(xml)
        result = _parse_connection_class(elem)
        self.assertEqual(result['details']['delimiter'], ',')

    def test_sqlserver_connection(self):
        xml = '<connection class="sqlserver" server="db.co" dbname="sales" authentication="sspi" />'
        elem = ET.fromstring(xml)
        result = _parse_connection_class(elem)
        self.assertEqual(result['type'], 'SQL Server')
        self.assertEqual(result['details']['server'], 'db.co')
        self.assertEqual(result['details']['database'], 'sales')

    def test_postgres_connection(self):
        xml = '<connection class="postgres" server="pg.co" port="5433" dbname="analytics" username="admin" />'
        elem = ET.fromstring(xml)
        result = _parse_connection_class(elem)
        self.assertEqual(result['type'], 'PostgreSQL')
        self.assertEqual(result['details']['port'], '5433')

    def test_bigquery_connection(self):
        xml = '<connection class="bigquery" project="my-proj" dataset="ds1" />'
        elem = ET.fromstring(xml)
        result = _parse_connection_class(elem)
        self.assertEqual(result['type'], 'BigQuery')
        self.assertEqual(result['details']['project'], 'my-proj')

    def test_oracle_connection(self):
        xml = '<connection class="oracle" server="ora.co" service="ORCL" port="1521" />'
        elem = ET.fromstring(xml)
        result = _parse_connection_class(elem)
        self.assertEqual(result['type'], 'Oracle')
        self.assertEqual(result['details']['service'], 'ORCL')

    def test_mysql_connection(self):
        xml = '<connection class="mysql" server="mysql.co" port="3307" dbname="app" />'
        elem = ET.fromstring(xml)
        result = _parse_connection_class(elem)
        self.assertEqual(result['type'], 'MySQL')
        self.assertEqual(result['details']['port'], '3307')

    def test_snowflake_connection(self):
        xml = '<connection class="snowflake" server="sf.co" dbname="db" schema="pub" warehouse="wh" role="analyst" />'
        elem = ET.fromstring(xml)
        result = _parse_connection_class(elem)
        self.assertEqual(result['type'], 'Snowflake')
        self.assertEqual(result['details']['warehouse'], 'wh')

    def test_sapbw_connection(self):
        xml = '<connection class="sapbw" server="sap.co" />'
        elem = ET.fromstring(xml)
        result = _parse_connection_class(elem)
        self.assertEqual(result['type'], 'SAP BW')

    def test_unknown_connector_fallback(self):
        xml = '<connection class="exasol" server="ex.co" custom="val" />'
        elem = ET.fromstring(xml)
        result = _parse_connection_class(elem)
        self.assertEqual(result['type'], 'EXASOL')
        self.assertEqual(result['details']['server'], 'ex.co')
        self.assertEqual(result['details']['custom'], 'val')

    def test_geojson_connection(self):
        xml = '<connection class="ogrdirect" filename="map.geojson" directory="/maps" />'
        elem = ET.fromstring(xml)
        result = _parse_connection_class(elem)
        self.assertEqual(result['type'], 'GeoJSON')

    def test_sqlproxy_connection(self):
        xml = ('<connection class="sqlproxy" channel="https" server="tableau.example.com" '
               'port="443" dbname="E_Formation" server-ds-friendly-name="E_Formation" />')
        elem = ET.fromstring(xml)
        result = _parse_connection_class(elem)
        self.assertEqual(result['type'], 'Tableau Server')
        self.assertEqual(result['details']['server'], 'tableau.example.com')
        self.assertEqual(result['details']['port'], '443')
        self.assertEqual(result['details']['dbname'], 'E_Formation')
        self.assertEqual(result['details']['channel'], 'https')
        self.assertEqual(result['details']['server_ds_name'], 'E_Formation')


# ═══════════════════════════════════════════════════════════════════
# _build_connection_map
# ═══════════════════════════════════════════════════════════════════

class TestBuildConnectionMap(unittest.TestCase):
    """Test connection map construction from datasource XML."""

    def test_federated_with_named_connections(self):
        xml = '''
        <datasource name="ds1">
          <connection class="federated">
            <named-connections>
              <named-connection name="conn1" caption="Sales DB">
                <connection class="sqlserver" server="db.co" dbname="sales" />
              </named-connection>
              <named-connection name="conn2">
                <connection class="postgres" server="pg.co" dbname="analytics" />
              </named-connection>
            </named-connections>
          </connection>
        </datasource>
        '''
        ds_elem = ET.fromstring(xml)
        conn_map = _build_connection_map(ds_elem)
        self.assertIn('conn1', conn_map)
        self.assertIn('conn2', conn_map)
        self.assertEqual(conn_map['conn1']['type'], 'SQL Server')
        self.assertEqual(conn_map['conn2']['type'], 'PostgreSQL')

    def test_no_connection_returns_empty(self):
        xml = '<datasource name="ds1" />'
        ds_elem = ET.fromstring(xml)
        conn_map = _build_connection_map(ds_elem)
        self.assertEqual(conn_map, {})

    def test_non_federated_connection(self):
        xml = '''
        <datasource name="ds1">
          <connection class="sqlserver" server="db.co" dbname="sales">
            <named-connection name="conn1">
              <connection class="sqlserver" server="db.co" dbname="sales" />
            </named-connection>
          </connection>
        </datasource>
        '''
        ds_elem = ET.fromstring(xml)
        conn_map = _build_connection_map(ds_elem)
        self.assertIn('conn1', conn_map)


# ═══════════════════════════════════════════════════════════════════
# extract_tables_with_columns — phase 2 (ds-level cols), phase 3 (metadata)
# ═══════════════════════════════════════════════════════════════════

class TestExtractTablesWithColumns(unittest.TestCase):
    """Test table extraction with column population fallbacks."""

    def test_basic_table_with_columns(self):
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="table" name="Orders" connection="conn1">
              <columns>
                <column name="OrderID" datatype="integer" ordinal="0" />
                <column name="Amount" datatype="real" ordinal="1" />
              </columns>
            </relation>
          </connection>
        </datasource>
        '''
        ds = ET.fromstring(xml)
        tables = extract_tables_with_columns(ds)
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]['name'], 'Orders')
        self.assertEqual(len(tables[0]['columns']), 2)

    def test_phase2_cols_map_fallback(self):
        """Tables with no nested columns use <cols><map> + <column> elements."""
        xml = '''
        <datasource>
          <connection class="federated">
            <cols>
              <map key="[OrderID]" value="[Orders].[OrderID]" />
              <map key="[Amount]" value="[Orders].[Amount]" />
            </cols>
            <relation type="table" name="Orders" connection="conn1" />
          </connection>
          <column name="[OrderID]" datatype="integer" role="dimension" />
          <column name="[Amount]" datatype="real" role="measure" />
        </datasource>
        '''
        ds = ET.fromstring(xml)
        tables = extract_tables_with_columns(ds)
        self.assertEqual(len(tables), 1)
        self.assertEqual(len(tables[0]['columns']), 2)
        col_names = {c['name'] for c in tables[0]['columns']}
        self.assertIn('OrderID', col_names)
        self.assertIn('Amount', col_names)

    def test_phase2_skips_calculations(self):
        """Columns with <calculation> children should be skipped in fallback."""
        xml = '''
        <datasource>
          <connection class="federated">
            <cols>
              <map key="[OrderID]" value="[Orders].[OrderID]" />
            </cols>
            <relation type="table" name="Orders" connection="conn1" />
          </connection>
          <column name="[OrderID]" datatype="integer" />
          <column name="[Profit]" datatype="real">
            <calculation class="tableau" formula="[Revenue] - [Cost]" />
          </column>
        </datasource>
        '''
        ds = ET.fromstring(xml)
        tables = extract_tables_with_columns(ds)
        self.assertEqual(len(tables), 1)
        # Only OrderID should appear (Profit is a calculation)
        col_names = {c['name'] for c in tables[0]['columns']}
        self.assertIn('OrderID', col_names)
        self.assertNotIn('Profit', col_names)

    def test_phase3_metadata_records_fallback(self):
        """Tables with no columns fall back to <metadata-record> elements."""
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="table" name="Customers" connection="conn1" />
            <metadata-records>
              <metadata-record class="column">
                <remote-name>CustID</remote-name>
                <local-name>[CustID]</local-name>
                <parent-name>[Customers]</parent-name>
                <local-type>integer</local-type>
                <ordinal>0</ordinal>
                <contains-null>false</contains-null>
              </metadata-record>
              <metadata-record class="column">
                <remote-name>Name</remote-name>
                <local-name>[Name]</local-name>
                <parent-name>[Customers]</parent-name>
                <local-type>string</local-type>
                <ordinal>1</ordinal>
              </metadata-record>
            </metadata-records>
          </connection>
        </datasource>
        '''
        ds = ET.fromstring(xml)
        tables = extract_tables_with_columns(ds)
        self.assertEqual(len(tables), 1)
        self.assertEqual(len(tables[0]['columns']), 2)
        self.assertEqual(tables[0]['columns'][0]['name'], 'CustID')
        self.assertFalse(tables[0]['columns'][0]['nullable'])

    def test_phase3_metadata_merge_recovers_worksheet_dependency(self):
        """A worksheet dependency must survive a metadata-only parent alias."""
        xml = '''
        <workbook>
          <datasources>
            <datasource name="SyntheticSource">
              <connection class="federated">
                <relation type="table" name="FactRelation">
                  <columns>
                    <column name="Amount" datatype="real" ordinal="0" />
                  </columns>
                </relation>
                <metadata-records>
                  <metadata-record class="column">
                    <local-name>Category</local-name>
                    <remote-name>Category</remote-name>
                    <parent-name>UpstreamTable</parent-name>
                    <local-type>string</local-type>
                    <ordinal>1</ordinal>
                  </metadata-record>
                </metadata-records>
              </connection>
              <column name="[Amount]" datatype="real" role="measure" />
            </datasource>
          </datasources>
          <worksheets>
            <worksheet name="SyntheticSheet">
              <table>
                <view>
                  <datasource-dependencies datasource="SyntheticSource">
                    <column name="[Category]" datatype="string"
                            role="dimension" type="nominal" />
                  </datasource-dependencies>
                </view>
                <rows>[SyntheticSource].[none:Category:nk]</rows>
              </table>
            </worksheet>
          </worksheets>
        </workbook>
        '''
        workbook = ET.fromstring(xml)
        dependencies = workbook.findall(
            './worksheets/worksheet/table/view/datasource-dependencies/column')
        self.assertEqual([column.get('name') for column in dependencies],
                         ['[Category]'])
        ds = workbook.findall('./datasources/datasource')[0]
        self.assertIsNone(ds.find('./column[@name="[Category]"]'))

        tables = extract_tables_with_columns(ds)
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]['name'], 'FactRelation')
        self.assertEqual(
            [(column['name'], column['datatype'], column['role'])
             for column in tables[0]['columns']],
            [('Amount', 'real', 'measure'), ('Category', 'string', 'dimension')])

    def test_phase3_metadata_merge_parent_alias_populates_empty_singleton(self):
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="table" name="FactRelation" />
            <metadata-records>
              <metadata-record class="column">
                <local-name>[Category]</local-name>
                <remote-name>Category</remote-name>
                <parent-name>[UpstreamTable]</parent-name>
                <local-type>string</local-type>
                <ordinal>1</ordinal>
                <contains-null>false</contains-null>
              </metadata-record>
            </metadata-records>
          </connection>
        </datasource>
        '''
        tables = extract_tables_with_columns(ET.fromstring(xml))
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]['name'], 'FactRelation')
        self.assertEqual(tables[0]['columns'], [{
            'name': 'Category', 'datatype': 'string', 'role': 'dimension',
            'ordinal': 1, 'length': None, 'nullable': False,
        }])

    def test_phase3_metadata_merge_exact_parent_selects_owning_table(self):
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="join" join="left">
              <relation type="table" name="FactRelation">
                <columns><column name="Amount" datatype="real" /></columns>
              </relation>
              <relation type="table" name="DimensionRelation">
                <columns><column name="ItemID" datatype="integer" /></columns>
              </relation>
            </relation>
            <metadata-records>
              <metadata-record class="column">
                <local-name>[Category]</local-name>
                <remote-name>Category</remote-name>
                <parent-name>[DimensionRelation]</parent-name>
                <local-type>string</local-type>
                <ordinal>1</ordinal>
              </metadata-record>
            </metadata-records>
          </connection>
        </datasource>
        '''
        tables = extract_tables_with_columns(ET.fromstring(xml))
        self.assertEqual(len(tables), 2)
        self.assertEqual(
            {table['name']: [column['name'] for column in table['columns']]
             for table in tables},
            {'FactRelation': ['Amount'],
             'DimensionRelation': ['ItemID', 'Category']})
        category = next(table for table in tables
                        if table['name'] == 'DimensionRelation')['columns'][1]
        self.assertEqual((category['datatype'], category['role']),
                         ('string', 'dimension'))

    def test_phase3_metadata_merge_unassigned_multi_table_stays_unassigned(self):
        """An unowned record or declaration cannot supply either table's schema."""
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="table" name="FactRelation">{fact_columns}</relation>
            <relation type="table" name="DimensionRelation">{dim_columns}</relation>
            <metadata-records>
              <metadata-record class="column">
                <local-name>[Category]</local-name>
                <remote-name>Category</remote-name>
                {parent_xml}
                <local-type>string</local-type>
              </metadata-record>
            </metadata-records>
          </connection>
          {declaration}
        </datasource>
        '''
        for populated in (True, False):
            for declared in (False, True):
                for parent_xml in (
                        '<parent-name>[UpstreamTable]</parent-name>',
                        '<parent-name />', ''):
                    with self.subTest(populated=populated, declared=declared,
                                      parent_xml=parent_xml):
                        ds = ET.fromstring(xml.format(
                            fact_columns=(
                                '<columns><column name="Amount" datatype="real" />'
                                '</columns>' if populated else ''),
                            dim_columns=(
                                '<columns><column name="ItemID" datatype="integer" />'
                                '</columns>' if populated else ''),
                            parent_xml=parent_xml,
                            declaration=(
                                '<column name="[Category]" datatype="string" '
                                'role="dimension" />' if declared else ''),
                        ))
                        tables = extract_tables_with_columns(ds)
                        self.assertEqual(len(tables), 2)
                        self.assertEqual(
                            {table['name']: [column['name']
                                            for column in table['columns']]
                             for table in tables},
                            {'FactRelation': ['Amount'] if populated else [],
                             'DimensionRelation': ['ItemID'] if populated else []})

    def test_phase3_metadata_merge_preserves_unicode_names(self):
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="table" name="Faits été">
              <columns><column name="Montant (€)" datatype="real" /></columns>
            </relation>
            <metadata-records>
              <metadata-record class="column">
                <local-name>[Catégorie / état]</local-name>
                <remote-name>Catégorie / état</remote-name>
                <parent-name>[Table amont]</parent-name>
                <local-type>string</local-type>
                <ordinal>1</ordinal>
              </metadata-record>
            </metadata-records>
          </connection>
        </datasource>
        '''
        tables = extract_tables_with_columns(ET.fromstring(xml))
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]['name'], 'Faits été')
        self.assertEqual(
            [(column['name'], column['datatype'], column['role'])
             for column in tables[0]['columns']],
            [('Montant (€)', 'real', 'measure'),
             ('Catégorie / état', 'string', 'dimension')])

    def test_phase3_metadata_merge_keeps_case_distinct_names(self):
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="table" name="FactRelation">
              <columns><column name="Category" datatype="string" /></columns>
            </relation>
            <relation type="table" name="factrelation">
              <columns><column name="Amount" datatype="real" /></columns>
            </relation>
            <metadata-records>
              <metadata-record class="column">
                <local-name>[Category]</local-name>
                <parent-name>[FactRelation]</parent-name>
                <local-type>integer</local-type>
              </metadata-record>
              <metadata-record class="column">
                <local-name>[category]</local-name>
                <parent-name>[FactRelation]</parent-name>
                <local-type>string</local-type>
                <ordinal>1</ordinal>
              </metadata-record>
              <metadata-record class="column">
                <local-name>[category]</local-name>
                <parent-name>[FactRelation]</parent-name>
                <local-type>real</local-type>
                <ordinal>2</ordinal>
              </metadata-record>
              <metadata-record class="column">
                <local-name>[CATEGORY]</local-name>
                <parent-name>[factrelation]</parent-name>
                <local-type>string</local-type>
                <ordinal>1</ordinal>
              </metadata-record>
            </metadata-records>
          </connection>
        </datasource>
        '''
        tables = extract_tables_with_columns(ET.fromstring(xml))
        self.assertEqual(len(tables), 2)
        self.assertEqual(
            {table['name']: [(column['name'], column['datatype'], column['role'])
                            for column in table['columns']]
             for table in tables},
            {'FactRelation': [('Category', 'string', 'dimension'),
                              ('category', 'string', 'dimension')],
             'factrelation': [('Amount', 'real', 'measure'),
                              ('CATEGORY', 'string', 'dimension')]})

    def test_phase3_metadata_merge_preserves_existing_column_metadata(self):
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="table" name="FactRelation">
              <columns>
                <column name="Amount" datatype="real" ordinal="7" length="8"
                        nullable="false" default-format="#,##0.00" />
              </columns>
            </relation>
            <metadata-records>
              <metadata-record class="column">
                <local-name>[Amount]</local-name>
                <parent-name>[FactRelation]</parent-name>
                <local-type>string</local-type>
                <ordinal>0</ordinal>
                <contains-null>true</contains-null>
              </metadata-record>
              <metadata-record class="column">
                <local-name>[Category]</local-name>
                <parent-name>[FactRelation]</parent-name>
                <local-type>string</local-type>
                <ordinal>1</ordinal>
              </metadata-record>
            </metadata-records>
          </connection>
          <column name="[Amount]" datatype="real" role="dimension"
                  default-format="#,##0.00" />
        </datasource>
        '''
        tables = extract_tables_with_columns(ET.fromstring(xml))
        self.assertEqual(len(tables), 1)
        self.assertEqual([column['name'] for column in tables[0]['columns']],
                         ['Amount', 'Category'])
        self.assertEqual(tables[0]['columns'][0], {
            'name': 'Amount', 'datatype': 'real', 'role': 'dimension',
            'ordinal': 7, 'length': '8', 'nullable': False,
            'default_format': '#,##0.00',
        })

    def test_phase3_metadata_merge_honors_datasource_declarations(self):
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="table" name="FactRelation">
              <columns><column name="Amount" datatype="real" /></columns>
            </relation>
            <metadata-records>
              <metadata-record class="column">
                <local-name>[Code]</local-name>
                <parent-name>[UpstreamTable]</parent-name>
                <local-type>real</local-type>
                <ordinal>1</ordinal>
              </metadata-record>
              <metadata-record class="column">
                <local-name>[Category]</local-name>
                <parent-name>[UpstreamTable]</parent-name>
                <local-type>integer</local-type>
                <ordinal>2</ordinal>
              </metadata-record>
            </metadata-records>
          </connection>
          <column name="[Code]" datatype="integer" role="dimension"
                  default-format="0000" />
          <column name="[Category]" datatype="string" role="dimension"
                  default-format="@" />
        </datasource>
        '''
        tables = extract_tables_with_columns(ET.fromstring(xml))
        self.assertEqual(len(tables), 1)
        self.assertEqual([column['name'] for column in tables[0]['columns']],
                         ['Amount', 'Code', 'Category'])
        columns = {column['name']: column for column in tables[0]['columns']}
        for name, datatype, default_format in (
                ('Code', 'integer', '0000'), ('Category', 'string', '@')):
            with self.subTest(name=name):
                self.assertEqual(columns[name]['datatype'], datatype)
                self.assertEqual(columns[name]['role'], 'dimension')
                self.assertEqual(columns[name]['default_format'], default_format)

    def test_phase4_last_resort_ds_columns(self):
        """Tables with no columns after phase 3 use ds-level <column> elements."""
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="table" name="Products" connection="conn1" />
          </connection>
          <column name="[ProdID]" datatype="integer" />
          <column name="[ProdName]" datatype="string" />
        </datasource>
        '''
        ds = ET.fromstring(xml)
        tables = extract_tables_with_columns(ds)
        self.assertEqual(len(tables), 1)
        self.assertEqual(len(tables[0]['columns']), 2)

    def test_deduplication_keeps_most_columns(self):
        """Duplicate table names keep the version with more columns."""
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="table" name="Orders" connection="conn1">
              <columns>
                <column name="ID" datatype="integer" />
              </columns>
            </relation>
            <relation type="table" name="Orders" connection="conn1">
              <columns>
                <column name="ID" datatype="integer" />
                <column name="Amount" datatype="real" />
              </columns>
            </relation>
          </connection>
        </datasource>
        '''
        ds = ET.fromstring(xml)
        tables = extract_tables_with_columns(ds)
        self.assertEqual(len(tables), 1)
        self.assertEqual(len(tables[0]['columns']), 2)

    def test_skips_join_relations(self):
        """Only type='table' relations are extracted, not joins."""
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="join" join="inner">
              <relation type="table" name="Orders" connection="conn1">
                <columns><column name="ID" datatype="integer" /></columns>
              </relation>
              <relation type="table" name="Products" connection="conn1">
                <columns><column name="ProdID" datatype="integer" /></columns>
              </relation>
            </relation>
          </connection>
        </datasource>
        '''
        ds = ET.fromstring(xml)
        tables = extract_tables_with_columns(ds)
        self.assertEqual(len(tables), 2)
        names = {t['name'] for t in tables}
        self.assertEqual(names, {'Orders', 'Products'})

    def test_role_override_from_ds_columns(self):
        """Phase 2.5: Role overrides from datasource-level <column> elements."""
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="table" name="Orders" connection="conn1">
              <columns>
                <column name="RowID" datatype="integer" ordinal="0" />
              </columns>
            </relation>
          </connection>
          <column name="[RowID]" role="dimension" />
        </datasource>
        '''
        ds = ET.fromstring(xml)
        tables = extract_tables_with_columns(ds)
        self.assertEqual(tables[0]['columns'][0]['role'], 'dimension')


# ═══════════════════════════════════════════════════════════════════
# extract_relationships
# ═══════════════════════════════════════════════════════════════════

class TestExtractRelationships(unittest.TestCase):
    """Test relationship extraction from Tableau join XML."""

    def test_simple_join(self):
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="join" join="inner">
              <relation type="table" name="Orders" />
              <relation type="table" name="Products" />
              <clause>
                <expression op="=">
                  <expression op="[Orders].[ProductID]" />
                  <expression op="[Products].[ProductID]" />
                </expression>
              </clause>
            </relation>
          </connection>
        </datasource>
        '''
        ds = ET.fromstring(xml)
        rels = extract_relationships(ds)
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]['type'], 'inner')
        self.assertEqual(rels[0]['left']['table'], 'Orders')
        self.assertEqual(rels[0]['left']['column'], 'ProductID')
        self.assertEqual(rels[0]['right']['table'], 'Products')
        self.assertEqual(rels[0]['right']['column'], 'ProductID')

    def test_bare_column_format(self):
        """Bare [Column] format infers table from child relations."""
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="join" join="left">
              <relation type="table" name="Orders" />
              <relation type="table" name="Customers" />
              <clause>
                <expression op="=">
                  <expression op="[CustID]" />
                  <expression op="[CustID]" />
                </expression>
              </clause>
            </relation>
          </connection>
        </datasource>
        '''
        ds = ET.fromstring(xml)
        rels = extract_relationships(ds)
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]['left']['table'], 'Orders')
        self.assertEqual(rels[0]['right']['table'], 'Customers')

    def test_multiple_clauses(self):
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="join" join="inner">
              <relation type="table" name="A" />
              <relation type="table" name="B" />
              <clause>
                <expression op="=">
                  <expression op="[A].[ID]" />
                  <expression op="[B].[ID]" />
                </expression>
              </clause>
              <clause>
                <expression op="=">
                  <expression op="[A].[Date]" />
                  <expression op="[B].[Date]" />
                </expression>
              </clause>
            </relation>
          </connection>
        </datasource>
        '''
        ds = ET.fromstring(xml)
        rels = extract_relationships(ds)
        self.assertEqual(len(rels), 2)

    def test_deduplication(self):
        """Same relationship should not appear twice."""
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="join" join="inner">
              <relation type="table" name="A" />
              <relation type="table" name="B" />
              <clause>
                <expression op="=">
                  <expression op="[A].[ID]" />
                  <expression op="[B].[ID]" />
                </expression>
              </clause>
            </relation>
            <relation type="join" join="inner">
              <relation type="table" name="A" />
              <relation type="table" name="B" />
              <clause>
                <expression op="=">
                  <expression op="[A].[ID]" />
                  <expression op="[B].[ID]" />
                </expression>
              </clause>
            </relation>
          </connection>
        </datasource>
        '''
        ds = ET.fromstring(xml)
        rels = extract_relationships(ds)
        self.assertEqual(len(rels), 1)

    def test_no_joins_returns_empty(self):
        xml = '''
        <datasource>
          <connection class="federated">
            <relation type="table" name="Orders" />
          </connection>
        </datasource>
        '''
        ds = ET.fromstring(xml)
        rels = extract_relationships(ds)
        self.assertEqual(rels, [])

    def test_object_graph_relationships(self):
        """Modern Tableau object-graph relationship format."""
        xml = '''
        <datasource>
          <object-graph>
            <relationship expression="[Orders].[CustID] = [Customers].[CustID]" type="Left" />
          </object-graph>
        </datasource>
        '''
        ds = ET.fromstring(xml)
        rels = extract_relationships(ds)
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]['left']['table'], 'Orders')
        self.assertEqual(rels[0]['right']['table'], 'Customers')


# ═══════════════════════════════════════════════════════════════════
# extract_datasource (integration)
# ═══════════════════════════════════════════════════════════════════

class TestExtractDatasource(unittest.TestCase):
    """Integration test for the full extract_datasource function."""

    def test_full_extraction(self):
        xml = '''
        <datasource name="Sales Data" caption="Sales Data">
          <connection class="federated">
            <named-connections>
              <named-connection name="conn1">
                <connection class="sqlserver" server="db.co" dbname="sales" />
              </named-connection>
            </named-connections>
            <relation type="table" name="Orders" connection="conn1">
              <columns>
                <column name="ID" datatype="integer" ordinal="0" />
                <column name="Revenue" datatype="real" ordinal="1" />
              </columns>
            </relation>
          </connection>
          <column name="[Profit]" datatype="real" role="measure" caption="Profit">
            <calculation class="tableau" formula="[Revenue] - [Cost]" />
          </column>
        </datasource>
        '''
        ds_elem = ET.fromstring(xml)
        ds = extract_datasource(ds_elem)
        self.assertEqual(ds['name'], 'Sales Data')
        self.assertEqual(ds['caption'], 'Sales Data')
        self.assertEqual(ds['connection']['type'], 'SQL Server')
        self.assertGreater(len(ds['tables']), 0)
        self.assertGreater(len(ds['calculations']), 0)
        self.assertIn('connection_map', ds)

    def test_none_datasource_is_safe(self):
        ds = extract_datasource(None)
        self.assertEqual(ds['name'], 'Unknown')
        self.assertEqual(ds['caption'], 'Unknown')
        self.assertEqual(ds['connection_map'], {})
        self.assertEqual(ds['tables'], [])
        self.assertEqual(ds['calculations'], [])
        self.assertEqual(ds['relationships'], [])


class TestRenameSqlproxyTables(unittest.TestCase):
    """Verify that published-datasource (sqlproxy) tables are renamed to the
    user-facing caption rather than leaking the internal Tableau class token.
    """

    def test_sanitize_strips_brackets_and_whitespace(self):
        self.assertEqual(_sanitize_caption_for_table_name('  [My DS]  '), 'My DS')
        self.assertEqual(_sanitize_caption_for_table_name('Plain Name'), 'Plain Name')
        self.assertEqual(_sanitize_caption_for_table_name(''), '')
        self.assertEqual(_sanitize_caption_for_table_name(None), '')

    def test_renames_sqlproxy_to_caption(self):
        ds = {
            'name': 'sqlproxy.103ekax0erob871al9eyl0bq5wh0',
            'caption': 'SAMPLE_OBSERVATION (2)',
            'tables': [{'name': 'sqlproxy', 'columns': []}],
            'relationships': [],
            'calculations': [],
        }
        _rename_sqlproxy_tables(ds)
        self.assertEqual(ds['tables'][0]['name'], 'SAMPLE_OBSERVATION (2)')

    def test_propagates_rename_to_relationships(self):
        ds = {
            'name': 'sqlproxy.abc',
            'caption': 'My Published DS',
            'tables': [{'name': 'sqlproxy', 'columns': []}],
            'relationships': [
                {'left': {'table': 'sqlproxy', 'column': 'Id'},
                 'right': {'table': 'Other', 'column': 'Id'}},
            ],
            'calculations': [],
        }
        _rename_sqlproxy_tables(ds)
        self.assertEqual(ds['relationships'][0]['left']['table'], 'My Published DS')
        self.assertEqual(ds['relationships'][0]['right']['table'], 'Other')

    def test_propagates_rename_to_calc_column_table(self):
        ds = {
            'name': 'sqlproxy.abc',
            'caption': 'Sales',
            'tables': [{'name': 'sqlproxy', 'columns': []}],
            'relationships': [],
            'calculations': [
                {'name': '[Profit]', 'column_table': 'sqlproxy'},
                {'name': '[Other]', 'column_table': 'NotMatched'},
            ],
        }
        _rename_sqlproxy_tables(ds)
        self.assertEqual(ds['calculations'][0]['column_table'], 'Sales')
        self.assertEqual(ds['calculations'][1]['column_table'], 'NotMatched')

    def test_fallback_when_caption_missing(self):
        ds = {
            'name': 'sqlproxy.abc123',
            'caption': '',
            'tables': [{'name': 'sqlproxy', 'columns': []}],
            'relationships': [],
            'calculations': [],
        }
        _rename_sqlproxy_tables(ds)
        # Falls back to dropping the 'sqlproxy.' prefix
        self.assertEqual(ds['tables'][0]['name'], 'abc123')

    def test_fallback_when_caption_equals_name(self):
        ds = {
            'name': 'federated.xyz789',
            'caption': 'federated.xyz789',
            'tables': [{'name': 'sqlproxy', 'columns': []}],
            'relationships': [],
            'calculations': [],
        }
        _rename_sqlproxy_tables(ds)
        self.assertEqual(ds['tables'][0]['name'], 'xyz789')

    def test_does_not_rename_non_sqlproxy_tables(self):
        ds = {
            'name': 'sqlserver.db',
            'caption': 'My DB',
            'tables': [
                {'name': 'Orders', 'columns': []},
                {'name': 'Customers', 'columns': []},
            ],
            'relationships': [],
            'calculations': [],
        }
        _rename_sqlproxy_tables(ds)
        names = [t['name'] for t in ds['tables']]
        self.assertEqual(names, ['Orders', 'Customers'])

    def test_empty_datasource_is_safe(self):
        # No tables, no crash
        ds = {'name': 'x', 'caption': 'y', 'tables': []}
        _rename_sqlproxy_tables(ds)
        self.assertEqual(ds['tables'], [])

    def test_extract_datasource_integration(self):
        # End-to-end: a published-datasource XML snippet should yield a
        # table named after the caption rather than 'sqlproxy'.
        xml = '''
        <datasource name="sqlproxy.abc" caption="My Published DS">
          <connection class="sqlproxy" server="tableau.example.com" dbname="ds-id" />
          <relation type="table" name="sqlproxy">
            <columns>
              <column name="Id" datatype="integer" ordinal="0" />
            </columns>
          </relation>
        </datasource>
        '''
        ds_elem = ET.fromstring(xml)
        ds = extract_datasource(ds_elem)
        table_names = [t['name'] for t in ds['tables']]
        self.assertNotIn('sqlproxy', table_names)
        self.assertIn('My Published DS', table_names)


class TestColumnLocalNameMapFallback(unittest.TestCase):
    """Explicit connection maps supplement ownership, never guess it."""

    @staticmethod
    def _datasource(maps=(), records=()):
        datasource = ET.Element('datasource')
        connection = ET.SubElement(datasource, 'connection', {'class': 'federated'})
        cols = ET.SubElement(connection, 'cols')
        for attributes in maps:
            ET.SubElement(cols, 'map', attributes)
        metadata = ET.SubElement(connection, 'metadata-records')
        for fields in records:
            record = ET.SubElement(metadata, 'metadata-record', {'class': 'column'})
            for tag, text in fields.items():
                ET.SubElement(record, tag).text = text
        return datasource

    def test_maps_only_use_local_keys_and_explicit_parents(self):
        datasource = self._datasource(maps=(
            {'key': '[Region]', 'value': '[Orders].[Region]'},
            {'key': '[Display Region]', 'value': '[Orders].[physical_region]'},
            {'key': '[Manager]', 'value': '[People].[manager_name]'},
        ))
        self.assertEqual(_extract_col_local_name_map(datasource), {
            'Region': 'Orders', 'Display Region': 'Orders', 'Manager': 'People',
        })

    def test_partial_metadata_is_supplemented_per_field(self):
        datasource = self._datasource(
            maps=(
                {'key': '[Sales]', 'value': '[Orders].[Sales]'},
                {'key': '[Region]', 'value': '[Orders].[Region]'},
                {'key': '[Manager]', 'value': '[People].[manager_name]'},
            ),
            records=(
                {'local-name': '[Row ID]', 'parent-name': '[Orders]'},
                {'local-name': '[Sales]', 'parent-name': '[Orders]'},
            ),
        )
        self.assertEqual(_extract_col_local_name_map(datasource), {
            'Row ID': 'Orders', 'Sales': 'Orders',
            'Region': 'Orders', 'Manager': 'People',
        })

    def test_metadata_without_parent_can_be_supplemented(self):
        for fields in (
            {'local-name': '[Region]', 'local-type': 'string'},
            {'local-name': '[Region]', 'parent-name': ''},
        ):
            with self.subTest(record=fields):
                datasource = self._datasource(
                    maps=({'key': '[Region]', 'value': '[Orders].[Region]'},),
                    records=(fields,),
                )
                self.assertEqual(_extract_col_local_name_map(datasource),
                                 {'Region': 'Orders'})

    def test_metadata_ownership_wins_fallback_conflicts(self):
        for values in (
            ('[People].[Region]',),
            ('[Orders].[Region]', '[People].[Region]'),
            ('[People].[Region]', '[Orders].[Region]'),
        ):
            with self.subTest(values=values):
                datasource = self._datasource(
                    maps=tuple({'key': '[Region]', 'value': value} for value in values),
                    records=({'local-name': '[Region]', 'parent-name': '[Orders]'},),
                )
                self.assertEqual(_extract_col_local_name_map(datasource),
                                 {'Region': 'Orders'})

    def test_descendant_metadata_scopes_remain_authoritative(self):
        for path in ('./metadata-records', './connection/named-connection/connection'):
            with self.subTest(path=path):
                datasource = self._datasource(maps=(
                    {'key': '[Region]', 'value': '[People].[Region]'},
                ))
                parent = datasource
                for tag in path.removeprefix('./').split('/'):
                    parent = ET.SubElement(parent, tag)
                record = ET.SubElement(parent, 'metadata-record', {'class': 'column'})
                ET.SubElement(record, 'local-name').text = '[Region]'
                ET.SubElement(record, 'parent-name').text = '[Orders]'
                self.assertEqual(_extract_col_local_name_map(datasource),
                                 {'Region': 'Orders'})

    def test_conflicting_metadata_cannot_be_overridden_by_a_map(self):
        datasource = self._datasource(
            maps=({'key': '[Region]', 'value': '[Orders].[Region]'},),
            records=(
                {'local-name': '[Region]', 'parent-name': '[Orders]'},
                {'local-name': '[Region]', 'parent-name': '[People]'},
            ),
        )
        with self.assertLogs('datasource_extractor', level='WARNING') as logs:
            self.assertEqual(_extract_col_local_name_map(datasource), {})
        self.assertIn('ownership remains unresolved', '\n'.join(logs.output))

    def test_nested_or_inactive_map_scopes_cannot_supply_fallback(self):
        datasource = self._datasource()
        for tag, attributes in (
            ('extract', {'enabled': 'false'}),
            ('object-graph', {}),
            ('datasource', {'name': 'other'}),
        ):
            scope = ET.SubElement(datasource, tag, attributes)
            connection = ET.SubElement(scope, 'connection')
            cols = ET.SubElement(connection, 'cols')
            ET.SubElement(cols, 'map', key='[Region]', value='[People].[Region]')
        self.assertEqual(_extract_col_local_name_map(datasource), {})

    def test_identical_duplicate_maps_are_safe(self):
        datasource = self._datasource(maps=(
            {'key': '[Region]', 'value': '[Orders].[Region]'},
            {'key': '[Region]', 'value': '[Orders].[Region]'},
            {'key': '[Region]', 'value': '[Orders].[Region]'},
        ))
        self.assertEqual(_extract_col_local_name_map(datasource),
                         {'Region': 'Orders'})

    def test_conflicting_map_values_never_choose_first_or_last(self):
        # A different physical column is a conflict even on the same table.
        for other in ('[People].[Region]', '[Orders].[Other Region]'):
            for values in (
                ('[Orders].[Region]', other),
                (other, '[Orders].[Region]'),
                ('[Orders].[Region]', other, '[Orders].[Region]'),
            ):
                with self.subTest(values=values):
                    datasource = self._datasource(maps=tuple(
                        {'key': '[Region]', 'value': value} for value in values))
                    self.assertEqual(_extract_col_local_name_map(datasource), {})

    def test_conflicting_maps_do_not_hide_unambiguous_siblings(self):
        datasource = self._datasource(maps=(
            {'key': '[Region]', 'value': '[Orders].[Region]'},
            {'key': '[Sales]', 'value': '[Orders].[Sales]'},
            {'key': '[Region]', 'value': '[People].[Region]'},
            {'key': '[Manager]', 'value': '[People].[manager_name]'},
        ))
        self.assertEqual(_extract_col_local_name_map(datasource),
                         {'Sales': 'Orders', 'Manager': 'People'})

    def test_malformed_missing_and_unqualified_maps_never_invent_parent(self):
        invalid_maps = [
            {}, {'key': '[Region]'}, {'value': '[Orders].[Region]'},
            {'key': '', 'value': '[Orders].[Region]'},
            {'key': '[]', 'value': '[Orders].[Region]'},
        ]
        invalid_maps.extend(
            {'key': key, 'value': '[Orders].[Region]'}
            for key in ('Region', '[Region', '[Region]]',
                        '[Orders].[Region]', '[Region] trailing')
        )
        invalid_maps.extend(
            {'key': '[Region]', 'value': value}
            for value in (
                '', 'Region', '[Region]', 'Orders.Region', 'Orders.[Region]',
                '[Orders].Region', '[Orders].[Region', '[Orders].[Region] trailing',
                '[Orders].[Region].[Extra]', '[].[Region]', '[Orders].[]',
                '[Orders].[Region] + [People].[Region]',
            )
        )
        for attributes in invalid_maps:
            with self.subTest(mapping=attributes):
                datasource = self._datasource(maps=(attributes,))
                self.assertEqual(_extract_col_local_name_map(datasource), {})

    def test_same_local_name_is_scoped_to_its_datasource(self):
        workbook = ET.Element('workbook')
        datasources = ET.SubElement(workbook, 'datasources')
        left = self._datasource(maps=(
            {'key': '[Region]', 'value': '[Orders].[Region]'},
            {'key': '[Sales]', 'value': '[Orders].[Sales]'},
        ))
        right = self._datasource(maps=(
            {'key': '[Region]', 'value': '[People].[Region]'},
            {'key': '[Manager]', 'value': '[People].[Manager]'},
        ))
        empty = self._datasource()
        for name, datasource in (('left', left), ('right', right), ('empty', empty)):
            datasource.set('name', name)
            datasources.append(datasource)
        for datasource, expected in (
            (left, {'Region': 'Orders', 'Sales': 'Orders'}),
            (right, {'Region': 'People', 'Manager': 'People'}),
            (empty, {}),
            (left, {'Region': 'Orders', 'Sales': 'Orders'}),
        ):
            with self.subTest(datasource=datasource.get('name')):
                self.assertEqual(_extract_col_local_name_map(datasource), expected)

    def test_no_map_leaves_duplicate_physical_column_unowned(self):
        datasource = ET.fromstring('''
            <datasource>
              <connection class="federated">
                <relation type="join" join="left">
                  <relation name="Orders" type="table"><columns>
                    <column name="Region" datatype="string"/>
                  </columns></relation>
                  <relation name="People" type="table"><columns>
                    <column name="Region" datatype="string"/>
                  </columns></relation>
                </relation>
              </connection>
              <column name="[Region]" datatype="string"/>
            </datasource>
        ''')
        self.assertEqual(_extract_col_local_name_map(datasource), {})

    def test_special_unicode_and_dotted_identifiers_are_preserved(self):
        for key, value, local, parent in (
            ('[Net sales (%)/€]', "[Orders & Returns' archive].[net-sales (%)]",
             'Net sales (%)/€', "Orders & Returns' archive"),
            ('[Région – 東京]', '[Commandes_日本].[physical_region]',
             'Région – 東京', 'Commandes_日本'),
            ('[local.part]', '[schema.table].[physical.part]',
             'local.part', 'schema.table'),
        ):
            with self.subTest(key=key, value=value):
                datasource = self._datasource(maps=({'key': key, 'value': value},))
                # Exercise XML attribute escaping as well as identifier parsing.
                datasource = ET.fromstring(ET.tostring(datasource))
                self.assertEqual(_extract_col_local_name_map(datasource),
                                 {local: parent})

    def test_escaped_brackets_are_decoded_without_stripping_content(self):
        for key, value, local, parent in (
            ('[Local]]name]', '[Orders]]archive].[physical]]field]',
             'Local]name', 'Orders]archive'),
            ('[Tail]]]', '[Orders]]].[physical]]]',
             'Tail]', 'Orders]'),
            ('[[Local]]]', '[[Orders]]].[physical]',
             '[Local]', '[Orders]'),
        ):
            with self.subTest(key=key, value=value):
                datasource = self._datasource(maps=({'key': key, 'value': value},))
                self.assertEqual(_extract_col_local_name_map(datasource),
                                 {local: parent})


class TestDatasourceGuardHelpers(unittest.TestCase):
    def test_local_name_map_none(self):
        self.assertEqual(_extract_col_local_name_map(None), {})

    def test_type_map_none(self):
        self.assertEqual(_extract_col_type_map(None), {})

    def test_local_name_map_malformed_object(self):
        class BadElem:
            def findall(self, *_args, **_kwargs):
                raise AttributeError('broken')

        self.assertEqual(_extract_col_local_name_map(BadElem()), {})

    def test_type_map_malformed_object(self):
        class BadElem:
            def findall(self, *_args, **_kwargs):
                raise AttributeError('broken')

        self.assertEqual(_extract_col_type_map(BadElem()), {})


# ═══════════════════════════════════════════════════════════════════
# extract_calculations
# ═══════════════════════════════════════════════════════════════════

class TestExtractCalculations(unittest.TestCase):
    """Test calculation extraction from datasource XML."""

    def test_basic_calculation(self):
        xml = '''
        <datasource>
          <column name="[Profit]" datatype="real" role="measure" caption="Profit">
            <calculation class="tableau" formula="[Revenue] - [Cost]" />
          </column>
        </datasource>
        '''
        ds = ET.fromstring(xml)
        calcs = extract_calculations(ds)
        self.assertEqual(len(calcs), 1)
        self.assertEqual(calcs[0]['caption'], 'Profit')
        self.assertEqual(calcs[0]['formula'], '[Revenue] - [Cost]')

    def test_skips_empty_formula(self):
        xml = '''
        <datasource>
          <column name="[Empty]" datatype="real">
            <calculation class="tableau" formula="   " />
          </column>
        </datasource>
        '''
        ds = ET.fromstring(xml)
        calcs = extract_calculations(ds)
        self.assertEqual(len(calcs), 0)

    def test_skips_categorical_bin(self):
        xml = '''
        <datasource>
          <column name="[Group]" datatype="string">
            <calculation class="categorical-bin" formula="" />
          </column>
        </datasource>
        '''
        ds = ET.fromstring(xml)
        calcs = extract_calculations(ds)
        self.assertEqual(len(calcs), 0)

    def test_table_calc_addressing(self):
        xml = '''
        <datasource>
          <column name="[RunSum]" datatype="real" role="measure" caption="RunSum">
            <calculation class="tableau" formula="RUNNING_SUM(SUM([Revenue]))">
              <table-calc type="Cumulative" ordering-type="Rows">
                <addressing-field name="[Date]" />
                <partitioning-field name="[Region]" />
              </table-calc>
            </calculation>
          </column>
        </datasource>
        '''
        ds = ET.fromstring(xml)
        calcs = extract_calculations(ds)
        self.assertEqual(len(calcs), 1)
        self.assertIn('table_calc_addressing', calcs[0])
        self.assertEqual(calcs[0]['table_calc_addressing'], ['Date'])
        self.assertEqual(calcs[0]['table_calc_partitioning'], ['Region'])
        self.assertEqual(calcs[0]['table_calc_type'], 'Cumulative')


# ═══════════════════════════════════════════════════════════════════
# extract_column_metadata
# ═══════════════════════════════════════════════════════════════════

class TestExtractColumnMetadata(unittest.TestCase):
    """Test column metadata extraction."""

    def test_column_with_all_attributes(self):
        xml = '''
        <datasource>
          <column name="[City]" caption="City" datatype="string"
                  role="dimension" type="nominal" hidden="true"
                  semantic-role="[Geographical].[City]"
                  default-format="#,##0" />
        </datasource>
        '''
        ds = ET.fromstring(xml)
        cols = extract_column_metadata(ds)
        self.assertEqual(len(cols), 1)
        c = cols[0]
        self.assertEqual(c['name'], '[City]')
        self.assertEqual(c['caption'], 'City')
        self.assertTrue(c['hidden'])
        self.assertEqual(c['semantic_role'], '[Geographical].[City]')

    def test_column_with_calculation(self):
        xml = '''
        <datasource>
          <column name="[Profit]" datatype="real">
            <calculation class="tableau" formula="[Rev] - [Cost]" />
          </column>
        </datasource>
        '''
        ds = ET.fromstring(xml)
        cols = extract_column_metadata(ds)
        self.assertEqual(len(cols), 1)
        self.assertIsNotNone(cols[0]['calculation'])
        self.assertEqual(cols[0]['calculation']['formula'], '[Rev] - [Cost]')


# ═══════════════════════════════════════════════════════════════════
# extract_connection_details
# ═══════════════════════════════════════════════════════════════════

class TestExtractConnectionDetails(unittest.TestCase):
    """Test connection details extraction."""

    def test_no_connection_returns_unknown(self):
        xml = '<datasource name="ds1" />'
        ds = ET.fromstring(xml)
        result = extract_connection_details(ds)
        self.assertEqual(result['type'], 'Unknown')

    def test_federated_with_named_conn(self):
        xml = '''
        <datasource>
          <connection class="federated">
            <named-connection name="conn1">
              <connection class="postgres" server="pg.co" dbname="db" />
            </named-connection>
          </connection>
        </datasource>
        '''
        ds = ET.fromstring(xml)
        result = extract_connection_details(ds)
        self.assertEqual(result['type'], 'PostgreSQL')


if __name__ == '__main__':
    unittest.main()
