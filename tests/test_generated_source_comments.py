"""Synthetic provenance-comment contracts; these do not establish value parity."""

import re
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from powerbi_import.powerbi_internal_lineage import build_powerbi_lineage_graph
from powerbi_import.pbip_generator import PowerBIProjectGenerator
from powerbi_import.tmdl_generator import _build_semantic_model, _write_table_tmdl
from powerbi_import.tmdl_writers import _dax_with_tableau_comment
from tableau_export.m_query_builder import annotate_m_provenance, generate_power_query_m
from tableau_export.prep_flow_parser import (
    _process_prep_node,
    _topological_sort,
    add_prep_provenance_comments,
    merge_prep_with_workbook,
    normalize_prep_flow,
    parse_prep_flow,
)
from tests.prep_flow_fixtures import operation, output, source, write_flow


_DAX_COMMENT = re.compile(r"/\* Tableau calculation; migrated to DAX[^*]*\*/ ")
_PREP_BLOCK = re.compile(
    r"\n    // Tableau Prep provenance \(metadata only\)\n"
    r"(?:    // [^\r\n]*\n)*?"
    r"    // End Tableau Prep provenance\n"
)


def _strip_prep(query):
    stripped, count = _PREP_BLOCK.subn("", query)
    if count != 1:
        raise AssertionError(f"Expected one generated Prep block, found {count}: {query}")
    return stripped


def _flow():
    return {"nodes": {
        "input": source("script", "Sales input"),
        "script": operation("RunScript", "predict", name="Script step",
                            language="Python"),
        "predict": operation("Prediction", "out", name="Prediction step"),
        "out": output("PublishedSales"),
    }, "connections": {}}


class TestGeneratedDaxSourceComments(unittest.TestCase):
    def test_migrated_measure_column_and_generated_helper(self):
        calculations = [
            {"name": "[CalcMeasure]", "caption": "Sales Total",
             "formula": "SUM([Amount])", "role": "measure", "datatype": "real"},
            {"name": "[CalcRow]", "caption": "Row Amount",
             "formula": "[Amount] * 2", "role": "dimension", "datatype": "real"},
        ]
        datasource = {
            "name": "Synthetic", "connection": {"type": "SQL Server",
                                                   "server": "localhost",
                                                   "database": "sample"},
            "tables": [{"name": "Facts", "columns": [
                {"name": "Amount", "datatype": "real"}]}],
            "calculations": calculations,
        }
        # Exercise the DAX fallback rather than M pushdown for the row calculation.
        with patch("powerbi_import.tmdl_generator._dax_to_m_expression", return_value=None):
            model = _build_semantic_model([datasource])
        table = next(t for t in model["model"]["tables"] if t["name"] == "Facts")
        measure = next(m for m in table["measures"] if m["name"] == "Sales Total")
        column = next(c for c in table["columns"] if c["name"] == "Row Amount")
        helper = next(m for m in table["measures"] if m["name"] == "Sales Total Formatted")
        with tempfile.TemporaryDirectory() as td:
            _write_table_tmdl(td, table)
            text = (Path(td) / "Facts.tmdl").read_text(encoding="utf-8")
        for kind, item, source_id, original in (
            ("measure", measure, "CalcMeasure", calculations[0]["formula"]),
            ("column", column, "CalcRow", calculations[1]["formula"]),
        ):
            with self.subTest(kind=kind):
                line = next(line for line in text.splitlines()
                            if line.startswith(f"\t{kind} '{item['name']}' = "))
                emitted = line.split(" = ", 1)[1]
                comment = _DAX_COMMENT.match(emitted)
                self.assertIsNotNone(comment, emitted)
                self.assertIn(source_id, comment.group())
                self.assertNotIn(original, comment.group())
                self.assertEqual(_DAX_COMMENT.sub("", emitted, count=1), item["expression"])
        helper_line = next(line for line in text.splitlines()
                           if line.startswith("\tmeasure 'Sales Total Formatted' = "))
        self.assertNotIn("Tableau calculation", helper_line)
        self.assertEqual(_dax_with_tableau_comment(helper["expression"], helper),
                         helper["expression"])

    def test_malicious_identity_cannot_escape_or_leak_into_comment(self):
        expression = "SUM('Facts'[Amount])"
        for malicious in ("name */ RETURN [Injected] /*", "name\nRETURN [Injected]",
                          "Password=SECRET_DAX_832", "host/SECRET_DAX_832",
                          "password SECRET_DAX_832",
                          "name\u2028RETURN [Injected]"):
            with self.subTest(malicious=malicious):
                item = {"_tableau_calculation_identity": {
                    "name": malicious, "caption": "Safe Caption"}}
                result = _dax_with_tableau_comment(expression, item)
                self.assertEqual(_DAX_COMMENT.sub("", result, count=1), expression)
                self.assertNotIn(malicious, result)
                self.assertNotIn("SECRET_DAX_832", result)
                self.assertNotIn("[Injected]", result)
                self.assertEqual(_dax_with_tableau_comment(result, item), result)


class TestGeneratedPrepSourceComments(unittest.TestCase):
    def test_standalone_and_bound_flow_preserve_executable_query(self):
        flow = _flow()
        with tempfile.TemporaryDirectory() as td, redirect_stdout(StringIO()):
            path = write_flow(td, flow, "synthetic.tfl")
            parsed = parse_prep_flow(path)
            self.assertEqual(parsed, parse_prep_flow(path))
            ds = next(d for d in parsed if d["prep_role"] == "output")
            query = ds["m_query_override"]
            normalized = normalize_prep_flow(flow)
            nodes = normalized["nodes"]
            results, secondary = {}, set()
            for node_id in _topological_sort(nodes):
                _process_prep_node(node_id, nodes, normalized["connections"],
                                   results, secondary)
            self.assertEqual(_strip_prep(query), results["out"]["m_query"])
            comments = _PREP_BLOCK.search(query).group()
            for name in ("synthetic.tfl", "Sales input", "Script step", "Prediction step"):
                self.assertIn(name, comments)
            self.assertRegex(comments, r"Script step.*status: manual")
            self.assertRegex(comments, r"Prediction step.*status: manual")
            self.assertRegex(comments, r"Sales input.*status: converted")
            self.assertEqual(add_prep_provenance_comments(query, ds["prep_provenance"],
                             ordered_node_ids=_topological_sort(nodes)), query)
            workbook = [{"name": "Workbook", "tables": [
                {"name": "PublishedSales", "columns": [{"name": "Raw"}]}]}]
            merged = merge_prep_with_workbook(parsed, workbook)
            bound = merged[0]["m_query_overrides"]["PublishedSales"]
            self.assertEqual(bound, query)
            self.assertEqual(_strip_prep(bound), results["out"]["m_query"])

    def test_malicious_prep_identity_is_bounded_and_contains_no_connection_values(self):
        flow = _flow()
        flow["nodes"]["script"]["name"] = "Bad */\nInjected = SECRET_PREP_417"
        flow["nodes"]["script"]["script"] = "SECRET_SOURCE_FORMULA_99"
        flow["nodes"]["predict"]["name"] = "Password=SECRET_PREP_417"
        flow["nodes"]["input"]["connectionAttributes"]["password"] = "SECRET_PREP_417"
        with tempfile.TemporaryDirectory() as td, redirect_stdout(StringIO()):
            ds = parse_prep_flow(write_flow(td, flow))[0]
        query = ds["m_query_override"]
        comments = _PREP_BLOCK.search(query).group()
        self.assertNotIn("SECRET_PREP_417", comments)
        self.assertNotIn("SECRET_SOURCE_FORMULA_99", comments)
        self.assertNotIn("*/", comments)
        self.assertNotIn("\nInjected =", comments)
        self.assertIn("status: manual", comments)
        self.assertEqual(query.count("Tableau Prep provenance (metadata only)"), 1)

    def test_bound_prep_comments_survive_real_pbip_and_m_calculation_injection(self):
        input_node = source("out", "Orders input")
        input_node["connectionAttributes"] = {
            "class": "sqlserver", "server": "synthetic.invalid",
            "dbname": "sample", "table": "Orders",
        }
        flow = {"nodes": {"input": input_node, "out": output("PublishedSales")}}
        calculations = [
            {"name": "[DoubleRaw]", "caption": "Double Raw",
             "formula": "[Raw] * 2", "role": "dimension", "datatype": "integer"},
            {"name": "[SumRaw]", "caption": "Total Raw",
             "formula": "SUM([Raw])", "role": "measure", "datatype": "integer"},
        ]
        workbook = [{"name": "Workbook", "tables": [
            {"name": "PublishedSales", "columns": [{"name": "Raw"}]}],
            "calculations": calculations}]
        with tempfile.TemporaryDirectory() as td, redirect_stdout(StringIO()):
            prep = parse_prep_flow(write_flow(td, flow))
            bound = merge_prep_with_workbook(prep, workbook)
            raw = deepcopy(bound)
            raw[0]["m_query_overrides"]["PublishedSales"] = _strip_prep(
                raw[0]["m_query_overrides"]["PublishedSales"])
            model = _build_semantic_model(bound)
            baseline = _build_semantic_model(raw)
            table = next(t for t in model["model"]["tables"]
                         if t["name"] == "PublishedSales")
            baseline_table = next(t for t in baseline["model"]["tables"]
                                  if t["name"] == "PublishedSales")
            expression = table["partitions"][0]["source"]["expression"]
            baseline_expression = baseline_table["partitions"][0]["source"]["expression"]
            self.assertIn('Table.AddColumn(', expression)
            self.assertIn('"Double Raw"', expression)
            self.assertEqual(_strip_prep(expression), baseline_expression)
            project = Path(PowerBIProjectGenerator(output_dir=td).generate_project(
                "Synthetic", {"datasources": bound, "worksheets": [], "dashboards": []},
                parameterize=False))
            self.assertTrue((project / "Synthetic.pbip").is_file())
            text = (project / "Synthetic.SemanticModel" / "definition" / "tables"
                    / "PublishedSales.tmdl").read_text(encoding="utf-8")
        self.assertIn("// Tableau Prep provenance (metadata only)", text)
        self.assertIn("// Flow: synthetic.tfl", text)
        self.assertIn("// Step: Orders input", text)
        self.assertIn('Table.AddColumn(', text)
        self.assertIn('"Double Raw"', text)
        self.assertRegex(text, r"measure 'Total Raw' = /\* Tableau calculation;")


class TestWorkbookMSourceComments(unittest.TestCase):
    def test_only_real_relation_is_annotated_not_connection_values(self):
        connection = {"type": "SQL Server", "details": {
            "server": "secret-host.example", "database": "secret-db",
            "password": "SECRET_CONNECTION_24"}}
        table = {"name": "Orders", "source_table": "[dbo].[Orders]",
                 "columns": [{"name": "Amount", "datatype": "real"}]}
        query = generate_power_query_m(connection, table)
        comments = "\n".join(line for line in query.splitlines()
                             if "Tableau source relation:" in line)
        self.assertIn("Orders", comments)
        for secret in ("secret-host.example", "secret-db", "SECRET_CONNECTION_24"):
            self.assertNotIn(secret, comments)
        self.assertEqual(query, generate_power_query_m(connection, table))
        with patch("tableau_export.m_query_builder._source_relation_comment",
                   side_effect=lambda original, *_: original):
            without_generated_comment = generate_power_query_m(connection, table)
        self.assertEqual(query.replace(comments + "\n", "", 1), without_generated_comment)
        self.assertEqual(annotate_m_provenance(query, source_relation=table["source_table"]),
                         query)

    def test_missing_and_hostile_relation_metadata_never_inject_code_or_secrets(self):
        connection = {"type": "SQL Server", "details": {
            "server": "server", "database": "database"}}
        base = {"name": "Orders", "columns": []}
        query = generate_power_query_m(connection, base)
        self.assertNotIn("Tableau source relation:", query)
        for value in ("Password=SECRET_CONNECTION_24",
                      "Bearer token hunter2", "password hunter2",
                      "api key hunter2",
                      'Orders\nInjected = "SECRET_CONNECTION_24"',
                      "Orders*/\nInjected = 1", "https://host/SECRET_CONNECTION_24"):
            with self.subTest(value=value):
                result = generate_power_query_m(connection, dict(base, source_table=value))
                comments = "\n".join(line for line in result.splitlines()
                                     if "Tableau source relation:" in line)
                self.assertNotIn("SECRET_CONNECTION_24", comments)
                self.assertNotIn("hunter2", comments)
                self.assertNotIn("\nInjected =", comments)

    def test_shared_m_comment_helper_escapes_control_characters_and_is_idempotent(self):
        baseline = "let\n    Source = 1\nin\n    Source"
        for value in ("Name\nInjected = 1", "Name\r\nInjected = 1",
                      "Name\u2028Injected = 1", "Name */ injected // suffix"):
            with self.subTest(value=value):
                result = annotate_m_provenance(baseline, source_relation=value)
                metadata_lines = [line for line in result.splitlines()
                                  if line.startswith("    // Tableau source relation:")]
                self.assertLessEqual(len(metadata_lines), 1)
                self.assertNotIn("\nInjected = 1", result)
                stripped = (result.replace(metadata_lines[0] + "\n", "", 1)
                            if metadata_lines else result)
                self.assertEqual(stripped, baseline)
                self.assertEqual(annotate_m_provenance(result, source_relation=value), result)


class TestLineageIgnoresSourceComments(unittest.TestCase):
    def test_comment_references_are_not_fields_but_real_references_are(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tables = root / "Project.SemanticModel" / "definition" / "tables"
            tables.mkdir(parents=True)
            report = root / "Project.Report" / "definition" / "report.json"
            report.parent.mkdir(parents=True)
            report.write_text("{}", encoding="utf-8")
            (tables / "Facts.tmdl").write_text(
                "table Facts\n"
                "\tcolumn Amount\n\t\tdataType: double\n\t\tsourceColumn: Amount\n"
                "\tcolumn Ghost\n\t\tdataType: double\n\t\tsourceColumn: Ghost\n"
                "\tmeasure Total = ```\n"
                "\t\t\t// 'Facts'[CommentOnly]\n"
                "\t\t\t/* 'Facts'[BlockOnly] */ SUM('Facts'[Amount])\n"
                "\t\t\t```\n"
                "\tpartition Facts = m\n\t\tmode: import\n\t\tsource =\n"
                "\t\t\tlet\n"
                "\t\t\t\t// Tableau Prep step: [Ghost]\n"
                '\t\t\t\tSource = #table({"Amount"}, {{1}})\n'
                "\t\t\tin\n\t\t\t\tSource\n",
                encoding="utf-8")
            graph = build_powerbi_lineage_graph(root)
        nodes = graph["nodes"]
        labels = {n["id"]: n["label"] for n in nodes}
        measure = next(n["id"] for n in nodes
                       if n["type"] == "measure" and n["label"] == "Facts.Total")
        deps = [e for e in graph["edges"]
                if e["type"] == "depends_on" and e["source"] == measure]
        with self.subTest(language="DAX"):
            self.assertEqual({labels[e["target"]] for e in deps}, {"Facts.Amount"})
        ghost = next(n["id"] for n in nodes
                     if n["type"] == "column" and n["label"] == "Facts.Ghost")
        with self.subTest(language="M"):
            amount = next(n["id"] for n in nodes
                          if n["type"] == "column" and n["label"] == "Facts.Amount")
            self.assertTrue(any(e["type"] == "m_origin" and e["target"] == amount
                                and e["status"] == "exact" for e in graph["edges"]))
            self.assertFalse(any(e["type"] == "m_origin" and e["target"] == ghost
                                 and e["status"] == "exact" for e in graph["edges"]))
