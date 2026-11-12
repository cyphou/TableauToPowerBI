"""Synthetic end-to-end coverage for the static Power BI lineage graph."""

import json
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from powerbi_import.errors import PartialGenerationError
from powerbi_import.pbip_generator import PowerBIProjectGenerator
from powerbi_import.powerbi_internal_lineage import (
    build_powerbi_lineage_graph,
    write_powerbi_lineage_artifacts,
)


_M_SECRET = "M_SECRET_SENTINEL_79c1"
_DAX_SECRET = "DAX_SECRET_SENTINEL_85e2"
_CONNECTION_SECRET = "CONNECTION_SECRET_SENTINEL_41a7"


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def _make_project(root):
    """Write a small PBIP-shaped M -> TMDL -> PBIR fixture."""
    model_dir = root / "Sales.SemanticModel"
    report_dir = root / "Sales.Report"
    tables_dir = model_dir / "definition" / "tables"
    tables_dir.mkdir(parents=True)

    orders_tmdl = tables_dir / "Orders.tmdl"
    orders_tmdl.write_text(
        "table Orders\n"
        "\tcolumn Amount\n"
        "\t\tdataType: double\n"
        "\t\tsourceColumn: Amount\n"
        "\tcolumn Region\n"
        "\t\tdataType: string\n"
        "\t\tsourceColumn: Region\n"
        "\tmeasure 'Net Sales' = "
        "SUM('Orders'[Amount]) + [Missing Measure] + [Region] "
        '+ "' + _DAX_SECRET + '"\n'
        "\tpartition Orders = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t\tCredential = "' + _M_SECRET + '",\n'
        '\t\t\t\tConnection = "Server=internal.example;Password='
        + _CONNECTION_SECRET + '",\n'
        '\t\t\t\tSource = #table(type table [Amount = number, Region = text], '
        '{{42, "West"}}),\n'
        '\t\t\t\tTyped = Table.SelectColumns(Source, {"Amount", "Region"})\n'
        "\t\t\tin\n"
        "\t\t\t\tTyped\n",
        encoding="utf-8",
    )
    (tables_dir / "Territories.tmdl").write_text(
        "table Territories\n"
        "\tcolumn Region\n"
        "\t\tdataType: string\n"
        "\t\tsourceColumn: Region\n"
        "\tpartition Territories = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t\tSource = #table(type table [Region = text], {{"West"}})\n'
        "\t\t\tin\n"
        "\t\t\t\tSource\n",
        encoding="utf-8",
    )

    pages_dir = report_dir / "definition" / "pages"
    page_id = "ReportSection"
    page_dir = pages_dir / page_id
    _write_json(page_dir / "page.json", {
        "name": page_id,
        "displayName": "Sales Overview",
    })
    _write_json(page_dir / "visuals" / "visual-001" / "visual.json", {
        "name": "visual-001",
        "visual": {
            "visualType": "card",
            "query": {
                "queryState": {
                    "Values": {
                        "projections": [{
                            "field": {
                                "Measure": {
                                    "Expression": {
                                        "SourceRef": {"Entity": "Orders"},
                                    },
                                    "Property": "Net Sales",
                                },
                            },
                            "queryRef": "Orders.Net Sales",
                        }],
                    },
                },
            },
        },
    })
    return {
        "orders_tmdl": orders_tmdl,
        "visual_json": page_dir / "visuals" / "visual-001" / "visual.json",
    }


def _node_by_label(graph, kind, label):
    matches = [node for node in graph["nodes"]
               if node["type"] == kind and node["label"] == label]
    if len(matches) != 1:
        raise AssertionError(
            f"Expected one {kind} node labeled {label!r}, found {len(matches)}")
    return matches[0]


class TestPowerBILineageGraph(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_dir = Path(self.temp_dir.name) / "SyntheticProject"
        self.artifacts = _make_project(self.project_dir)
        self.graph = build_powerbi_lineage_graph(self.project_dir)

    def test_nodes_edges_and_ids_are_stable_across_scans(self):
        repeated = build_powerbi_lineage_graph(self.project_dir)
        self.assertEqual(
            [node["id"] for node in self.graph["nodes"]],
            [node["id"] for node in repeated["nodes"]],
        )
        self.assertEqual(
            [edge["id"] for edge in self.graph["edges"]],
            [edge["id"] for edge in repeated["edges"]],
        )
        node_ids = {node["id"] for node in self.graph["nodes"]}
        edge_ids = [edge["id"] for edge in self.graph["edges"]]
        self.assertEqual(len(edge_ids), len(set(edge_ids)))
        self.assertTrue(all(edge["source"] in node_ids
                            and edge["target"] in node_ids
                            for edge in self.graph["edges"]))

    def test_graph_covers_m_origin_dax_dependencies_and_visual_projection(self):
        nodes = self.graph["nodes"]
        edges = self.graph["edges"]
        orders = _node_by_label(self.graph, "table", "Orders")
        amount = _node_by_label(self.graph, "column", "Orders.Amount")
        measure = _node_by_label(self.graph, "measure", "Orders.Net Sales")
        visual = _node_by_label(self.graph, "visual", "visual-001 (card)")

        m_edge = next(edge for edge in edges
                      if edge["type"] == "m_origin"
                      and edge["source"] != edge["target"]
                      and edge["target"] == amount["id"])
        self.assertEqual(m_edge["status"], "exact")
        self.assertEqual(m_edge["source"],
                         next(node["id"] for node in nodes
                              if node["type"] == "partition"
                              and node["label"] == "Orders.Orders"))
        self.assertEqual(m_edge["evidence"]["path"],
                         "Sales.SemanticModel/definition/tables/Orders.tmdl")
        self.assertEqual(m_edge["evidence"]["role"], "m_column_name")
        self.assertIsInstance(m_edge["evidence"]["line"], int)

        dax_edges = [edge for edge in edges
                     if edge["type"] == "depends_on"
                     and edge["source"] == measure["id"]]
        dax_by_status = {edge["status"]: edge for edge in dax_edges}
        self.assertEqual(set(dax_by_status), {"exact", "unresolved", "ambiguous"})
        self.assertEqual(
            dax_by_status["exact"]["target"], amount["id"])
        self.assertEqual(
            _node_by_label(
                self.graph, "unresolved_reference", "Missing Measure")["id"],
            dax_by_status["unresolved"]["target"],
        )
        self.assertEqual(
            _node_by_label(
                self.graph, "ambiguous_reference", "Region")["id"],
            dax_by_status["ambiguous"]["target"],
        )
        for edge in dax_edges:
            evidence = edge["evidence"]
            self.assertEqual(
                evidence["path"],
                "Sales.SemanticModel/definition/tables/Orders.tmdl",
            )
            self.assertEqual(evidence["role"], "dax_reference")
            self.assertIsInstance(evidence["line"], int)
            self.assertIsInstance(evidence["index"], int)

        projection, = [edge for edge in edges
                       if edge["type"] == "projects"
                       and edge["source"] == visual["id"]]
        self.assertEqual(projection["status"], "exact")
        self.assertEqual(projection["target"], measure["id"])
        self.assertEqual(
            projection["evidence"]["path"],
            "Sales.Report/definition/pages/ReportSection/visuals/"
            "visual-001/visual.json",
        )
        self.assertEqual(projection["evidence"]["role"], "Values")
        self.assertEqual(projection["evidence"]["index"], 0)

        self.assertEqual(self.graph["schema_version"], "1.1")
        self.assertEqual(self.graph["runtime_status"], "not_run")

    def test_no_formula_m_text_or_connection_secrets_escape_into_graph(self):
        serialized = json.dumps(self.graph, ensure_ascii=False)
        for forbidden in (
                _M_SECRET, _DAX_SECRET, _CONNECTION_SECRET,
                "SUM('Orders'[Amount])", "Credential =", "internal.example",
                "#table(type table", "West"):
            self.assertNotIn(forbidden, serialized)
        for node in self.graph["nodes"]:
            self.assertNotIn("expression", node)
            self.assertNotIn("m_text", node)
        for edge in self.graph["edges"]:
            self.assertNotIn("expression", edge)
            self.assertNotIn("m_text", edge)

    def test_writer_emits_adjacent_deterministic_json_and_self_contained_html(self):
        json_path, html_path = write_powerbi_lineage_artifacts(self.project_dir)
        self.assertEqual(json_path, self.project_dir / "powerbi_lineage.json")
        self.assertEqual(html_path, self.project_dir / "powerbi_lineage.html")
        self.assertEqual(json_path.parent, html_path.parent)
        first_json = json_path.read_bytes()
        first_html = html_path.read_bytes()

        second_paths = write_powerbi_lineage_artifacts(self.project_dir)
        self.assertEqual(second_paths, (json_path, html_path))
        self.assertEqual(json_path.read_bytes(), first_json)
        self.assertEqual(html_path.read_bytes(), first_html)

        canonical_graph = json.loads(first_json.decode("utf-8"))
        self.assertEqual(canonical_graph, self.graph)
        rendered = first_html.decode("utf-8")
        self.assertIn('<script id="lineage-data" type="application/json">',
                      rendered)
        embedded = re.search(
            r'<script id="lineage-data" type="application/json">(.*?)</script>',
            rendered, re.DOTALL,
        )
        self.assertIsNotNone(embedded)
        self.assertEqual(json.loads(embedded.group(1)), canonical_graph)
        self.assertIn("Sales Overview", rendered)
        self.assertIn("Orders.Net Sales", rendered)
        self.assertIn("ambiguous", rendered)
        self.assertNotIn(_M_SECRET, rendered)
        self.assertNotIn(_DAX_SECRET, rendered)
        self.assertNotIn(_CONNECTION_SECRET, rendered)
        self.assertNotRegex(rendered, r"<script[^>]+src\s*=")
        self.assertNotRegex(rendered, r"<link[^>]+href\s*=")
        self.assertNotRegex(rendered, r"https?://")

    def test_tmdl_only_and_fabric_like_folders_do_not_emit_artifacts(self):
        for folder_kind in ("tmdl_only", "fabric"):
            with self.subTest(folder_kind=folder_kind):
                root = Path(self.temp_dir.name) / folder_kind
                if folder_kind == "tmdl_only":
                    tables = root / "Only.SemanticModel" / "definition" / "tables"
                    tables.mkdir(parents=True)
                    (tables / "Only.tmdl").write_text(
                        "table Only\n\tcolumn Value\n\t\tsourceColumn: Value\n",
                        encoding="utf-8",
                    )
                else:
                    (root / "Only.Dataflow" / "definition").mkdir(parents=True)
                with self.assertRaises(ValueError):
                    write_powerbi_lineage_artifacts(root)
                self.assertFalse((root / "powerbi_lineage.json").exists())
                self.assertFalse((root / "powerbi_lineage.html").exists())

    def test_partial_pbip_generation_does_not_emit_lineage_artifacts(self):
        output_dir = Path(self.temp_dir.name) / "partial-output"
        generator = PowerBIProjectGenerator(output_dir=str(output_dir))
        converted_objects = {
            "datasources": [],
            "dashboards": [{
                "name": "Overview",
                "size": {"width": 1280, "height": 720},
                "objects": [{
                    "type": "worksheetReference",
                    "worksheetName": "Orders",
                    "position": {"x": 100, "y": 150, "w": 500, "h": 300},
                }],
            }],
        }
        with patch.object(
                generator, "create_tmdl_model",
                side_effect=PartialGenerationError(
                    "Synthetic semantic-model generation failure")):
            with redirect_stdout(StringIO()):
                project = Path(generator.generate_project(
                    "PartialProject", converted_objects))

        self.assertTrue((project / "partial_generation.json").is_file())
        self.assertFalse((project / "powerbi_lineage.json").exists())
        self.assertFalse((project / "powerbi_lineage.html").exists())


class TestEmptyReportLineageRegression(unittest.TestCase):
    """Zero pages are valid only when explicit PBIR metadata proves that."""

    def test_datasource_only_generator_omits_report_and_lineage_but_keeps_model(self):
        with tempfile.TemporaryDirectory() as directory:
            generator = PowerBIProjectGenerator(output_dir=directory)
            objects = {
                "datasources": [{
                    "name": "SyntheticSource",
                    "tables": [{
                        "name": "SyntheticTable",
                        "columns": [{"name": "ID", "datatype": "integer"}],
                    }],
                    "m_query_override": (
                        "let Source = #table(type table [ID = Int64.Type], "
                        "{{1}}) in Source"),
                    "calculations": [], "relationships": [],
                }],
                "worksheets": [], "dashboards": [],
            }
            with redirect_stdout(StringIO()):
                project = Path(generator.generate_project(
                    "ModelOnlySynthetic", objects))
            self.assertFalse((project / "partial_generation.json").exists())
            self.assertTrue(any(project.glob(
                "*.SemanticModel/definition/tables/SyntheticTable.tmdl")))
            self.assertFalse(any(project.glob("*.Report")))
            self.assertFalse((project / "powerbi_lineage.json").exists())
            self.assertFalse((project / "powerbi_lineage.html").exists())
            with self.assertRaises(ValueError):
                build_powerbi_lineage_graph(project)

    def test_real_generated_report_without_visuals_has_static_model_lineage(self):
        with tempfile.TemporaryDirectory() as directory:
            generator = PowerBIProjectGenerator(output_dir=directory)
            project = Path(directory) / "EmptySynthetic"
            objects = {
                "datasources": [{
                    "name": "SyntheticSource",
                    "tables": [{
                        "name": "SyntheticTable",
                        "columns": [{"name": "ID", "datatype": "integer"}],
                    }],
                    "m_query_override": (
                        "let Source = #table(type table [ID = Int64.Type], "
                        "{{1}}) in Source"),
                    "calculations": [], "relationships": [],
                }],
                "worksheets": [], "dashboards": [],
            }
            with redirect_stdout(StringIO()):
                # The project entrypoint intentionally omits the Report for
                # datasource-only sources. Explicitly create a real empty PBIR
                # here to exercise the generated default page without visuals.
                generator.create_semantic_model_structure(
                    str(project), "EmptySynthetic", objects)
                generator.create_report_structure(
                    str(project), "EmptySynthetic", objects)
                metadata, = project.glob("*.Report/definition/pages/pages.json")
                page_metadata = json.loads(metadata.read_text(encoding="utf-8"))
                self.assertEqual(len(page_metadata["pageOrder"]), 2)
                for page_name in page_metadata["pageOrder"][1:]:
                    shutil.rmtree(metadata.parent / page_name)
                page_metadata["pageOrder"] = ["ReportSection"]
                metadata.write_text(json.dumps(page_metadata), encoding="utf-8")
                write_powerbi_lineage_artifacts(project)
            self.assertFalse((project / "partial_generation.json").exists())
            metadata, = project.glob("*.Report/definition/pages/pages.json")
            self.assertEqual(json.loads(metadata.read_text(
                encoding="utf-8"))["pageOrder"], ["ReportSection"])
            graph_path = project / "powerbi_lineage.json"
            self.assertTrue(graph_path.is_file())
            graph = json.loads(graph_path.read_text(encoding="utf-8"))
            self.assertEqual(graph["runtime_status"], "not_run")
            self.assertTrue(any(node["type"] == "table"
                                for node in graph["nodes"]))
            self.assertEqual(sum(node["type"] == "page"
                                 for node in graph["nodes"]), 1)
            self.assertFalse(any(node["type"] == "visual"
                                 for node in graph["nodes"]))
            self.assertEqual(build_powerbi_lineage_graph(project), graph)
            # A zero-page PBIR can retain report.json but omit pages entirely.
            # Keep the real generated report definition, rather than inventing
            # a permissive empty JSON object for this scanner regression.
            report_definition, = project.glob("*.Report/definition/report.json")
            shutil.rmtree(metadata.parent)
            self.assertTrue(report_definition.is_file())
            write_powerbi_lineage_artifacts(project)
            zero_page_graph = json.loads(graph_path.read_text(encoding="utf-8"))
            self.assertEqual(zero_page_graph["runtime_status"], "not_run")
            self.assertEqual(
                {node["id"] for node in zero_page_graph["nodes"]},
                {node["id"] for node in graph["nodes"]
                 if node["type"] != "page"},
            )
            self.assertFalse(any(node["type"] in ("page", "visual")
                                 for node in zero_page_graph["nodes"]))

    def test_empty_metadata_accepts_model_but_does_not_invent_report_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_project(root)
            pages = root / "Sales.Report" / "definition" / "pages"
            shutil.rmtree(pages)
            _write_json(pages / "pages.json", {"pageOrder": []})
            graph = build_powerbi_lineage_graph(root)
            self.assertEqual(graph["runtime_status"], "not_run")
            self.assertTrue(any(n["type"] == "partition" for n in graph["nodes"]))
            self.assertFalse(any(n["type"] in ("page", "visual")
                                 for n in graph["nodes"]))

    def test_malformed_page_metadata_is_not_treated_as_zero_pages(self):
        invalid_metadata = (
            [], {}, {"pageOrder": None}, {"pageOrder": "not-an-array"},
            {"pageOrder": ["missing"]}, {"pageOrder": ["same", "same"]},
            {"pageOrder": [1]}, {"pageOrder": [""]},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_project(root)
            pages = root / "Sales.Report" / "definition" / "pages"
            shutil.rmtree(pages)
            for value in invalid_metadata:
                with self.subTest(metadata=value):
                    _write_json(pages / "pages.json", value)
                    with self.assertRaises(ValueError):
                        write_powerbi_lineage_artifacts(root)
                    self.assertFalse((root / "powerbi_lineage.json").exists())
                    self.assertFalse((root / "powerbi_lineage.html").exists())

    def test_missing_page_file_and_corrupt_json_still_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _make_project(root)
            pages = root / "Sales.Report" / "definition" / "pages"
            shutil.rmtree(pages)
            _write_json(pages / "pages.json", {"pageOrder": ["ReportSection"]})
            with self.assertRaises(ValueError):
                build_powerbi_lineage_graph(root)
            (pages / "pages.json").write_text("{invalid", encoding="utf-8")
            with self.assertRaises(ValueError):
                build_powerbi_lineage_graph(root)


if __name__ == "__main__":
    unittest.main()
