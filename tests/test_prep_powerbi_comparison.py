"""Synthetic Prep provenance -> workbook binding -> static PBIP evidence."""

import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from powerbi_import.migration_quality import (
    _prep_comparison,
    _render_prep_comparison,
    build_quality_report,
)
from powerbi_import.pbip_generator import PowerBIProjectGenerator
from powerbi_import.powerbi_internal_lineage import write_powerbi_lineage_artifacts
from tableau_export.prep_flow_parser import (
    PrepBindingError,
    merge_prep_with_workbook,
    parse_prep_flow,
)
from tests.prep_flow_fixtures import operation, output, source, write_flow


_SECRET = "SYNTHETIC_SECRET_DO_NOT_PUBLISH_41d9"
_TABLE = "Neutral_Output"


def _comparison_flow(manual=False, unused=False):
    """No files/servers are accessed: only a local synthetic flow is parsed."""
    nodes = {
        "input": source("manual" if manual else "out", name="NeutralInput"),
        "out": output(_TABLE),
    }
    if manual:
        nodes["manual"] = operation(
            "RunScript", "out", name="ManualReview", scriptLanguage="Python")
    if unused:
        nodes["unused_input"] = source("unused", name="UnusedInput")
        nodes["unused"] = output("Unused_Output")
    # Deliberately put sensitive-looking text only in non-display payloads.
    nodes["input"]["connectionAttributes"]["password"] = _SECRET
    return {"nodes": nodes, "connections": {}}


def _workbook(table=_TABLE, published=True):
    ds = {
        "name": "SyntheticWorkbookSource",
        "caption": table,
        "tables": [{
            "name": table,
            "columns": [
                {"name": "Raw", "datatype": "integer"},
                {"name": "Discard", "datatype": "string"},
            ],
        }],
        "calculations": [], "relationships": [],
        "connection": {"type": "csv", "details": {
            "filename": "synthetic_workbook.csv",
        }},
    }
    if published:
        ds["published_datasource"] = {"name": table}
    return [ds]


class TestPrepComparisonBindingContract(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def _parse(self, **kwargs):
        path = write_flow(self.root, _comparison_flow(**kwargs),
                          filename="neutral.tfl")
        with redirect_stdout(StringIO()):
            return parse_prep_flow(path)

    def test_parser_provenance_is_topological_metadata_not_source_payload(self):
        parsed = self._parse(manual=True, unused=True)
        bound = next(ds for ds in parsed
                     if ds["prep_provenance"]["output_name"] == _TABLE)
        provenance = bound["prep_provenance"]
        self.assertEqual(set(provenance),
                         {"flow", "output_node_id", "output_name", "nodes"})
        self.assertEqual(provenance["flow"], "neutral.tfl")
        self.assertEqual(provenance["output_node_id"], "out")
        nodes = {node["id"]: node for node in provenance["nodes"]}
        self.assertEqual(set(nodes), {"input", "manual", "out"})
        self.assertEqual(nodes["input"]["upstream_ids"], [])
        self.assertEqual(nodes["manual"]["upstream_ids"], ["input"])
        self.assertEqual(nodes["out"]["upstream_ids"], ["manual"])
        self.assertEqual(nodes["manual"]["status"], "manual")
        self.assertEqual(nodes["input"]["status"], "converted")
        self.assertEqual(nodes["out"]["status"], "converted")
        self.assertEqual(nodes["manual"]["kind"], "transform")
        for node in nodes.values():
            self.assertEqual(set(node),
                             {"id", "name", "kind", "upstream_ids", "status"})
        self.assertNotIn(_SECRET, json.dumps(provenance))
        self.assertNotIn("connectionAttributes", json.dumps(provenance))

    def test_merge_records_binding_unused_output_and_preserves_input_objects(self):
        parsed = self._parse(unused=True)
        workbook = _workbook()
        before = deepcopy((parsed, workbook))
        with redirect_stdout(StringIO()):
            merged, coverage = merge_prep_with_workbook(
                parsed, workbook, return_report=True)
        self.assertEqual((parsed, workbook), before)
        self.assertEqual(coverage["status"], "complete")
        self.assertEqual(coverage["unused_outputs"], ["Unused_Output"])
        self.assertEqual(coverage["unmatched_published"], [])
        self.assertEqual(coverage["unmatched_local"], [])
        self.assertEqual(coverage["secondary_queries"], [])
        self.assertEqual(len(merged), 1)
        binding, = merged[0]["prep_bindings"]
        self.assertEqual(set(binding), {"table", "output", "method", "provenance"})
        self.assertEqual(binding["table"], _TABLE)
        self.assertEqual(binding["output"], _TABLE)
        self.assertEqual(binding["method"], "exact_name_or_caption")
        self.assertEqual(coverage["matched"], [{
            "datasource": workbook[0]["name"], "table": _TABLE,
            "output": _TABLE, "method": "exact_name_or_caption",
        }])
        self.assertEqual(merged[0]["prep_binding_coverage"], coverage)
        original = next(ds for ds in parsed
                        if ds["tables"][0]["name"] == _TABLE)
        self.assertEqual(binding["provenance"], original["prep_provenance"])
        binding["provenance"]["nodes"][0]["name"] = "ChangedCopy"
        merged[0]["prep_binding_coverage"]["unused_outputs"].clear()
        self.assertEqual((parsed, workbook), before)
        self.assertEqual(coverage["unused_outputs"], ["Unused_Output"])

    def test_rejected_missing_published_binding_is_transactional(self):
        parsed = self._parse()
        workbook = _workbook()
        workbook.extend(_workbook("Missing_Output"))
        before = deepcopy((parsed, workbook))
        with self.assertRaises(PrepBindingError) as raised:
            merge_prep_with_workbook(parsed, workbook)
        self.assertEqual((parsed, workbook), before)
        coverage = raised.exception.coverage
        self.assertEqual(coverage["status"], "rejected")
        self.assertEqual(coverage["unmatched_published"], [{
            "datasource": "SyntheticWorkbookSource", "table": "Missing_Output",
        }])
        self.assertTrue(coverage["matched"])
        self.assertNotIn("prep_bindings", workbook[0])

    def test_ambiguous_alias_is_rejected_without_partial_metadata(self):
        parsed = self._parse(unused=True)
        for ds in parsed:
            ds["caption"] = "AmbiguousCaption"
        workbook = _workbook()
        before = deepcopy((parsed, workbook))
        with self.assertRaisesRegex(PrepBindingError, "Ambiguous"):
            merge_prep_with_workbook(parsed, workbook)
        self.assertEqual((parsed, workbook), before)

    def test_repeated_merge_replaces_metadata_without_accumulating_bindings(self):
        parsed = self._parse(unused=True)
        with redirect_stdout(StringIO()):
            first = merge_prep_with_workbook(parsed, _workbook())
            repeated = merge_prep_with_workbook(parsed, first)
        self.assertEqual(repeated, first)
        self.assertEqual(len(repeated[0]["prep_bindings"]), 1)
        repeated[0]["prep_bindings"][0]["provenance"]["nodes"][0]["name"] = "Copy"
        repeated[0]["prep_binding_coverage"]["unused_outputs"].clear()
        self.assertNotEqual(repeated, first)
        self.assertEqual(first[0]["prep_binding_coverage"]["unused_outputs"],
                         ["Unused_Output"])
        self.assertNotEqual(first[0]["prep_bindings"][0]["provenance"]
                            ["nodes"][0]["name"], "Copy")

    def test_current_merge_removes_stale_coverage_from_nonfirst_datasource(self):
        parsed = self._parse()
        workbook = _workbook()
        local = _workbook("Unrelated_Local", published=False)[0]
        local["name"] = "LocalWorkbookSource"
        local["prep_bindings"] = [{
            "table": "Obsolete", "output": "Obsolete", "method": "stale",
        }]
        local["prep_binding_coverage"] = {
            "status": "stale", "unused_outputs": ["Obsolete"],
        }
        workbook.append(local)
        before = deepcopy(workbook)
        with redirect_stdout(StringIO()):
            merged = merge_prep_with_workbook(parsed, workbook)
        self.assertEqual(workbook, before)
        self.assertNotIn("prep_bindings", merged[1])
        self.assertNotIn("prep_binding_coverage", merged[1])
        self.assertEqual(merged[0]["prep_binding_coverage"]["status"], "complete")
        self.assertEqual(merged[0]["prep_binding_coverage"]["unmatched_local"], [{
            "datasource": "LocalWorkbookSource", "table": "Unrelated_Local",
        }])


class TestPrepPowerBIComparison(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        path = write_flow(self.root, _comparison_flow(manual=True, unused=True),
                          filename="neutral.tfl")
        with redirect_stdout(StringIO()):
            parsed = parse_prep_flow(path)
            merged = merge_prep_with_workbook(parsed, _workbook())
            self.extracted = {
                "datasources": merged,
                "worksheets": [{
                    "name": "NeutralView", "chart_type": "clusteredBarChart",
                    "fields": [
                        {"name": "Discard", "shelf": "rows"},
                        {"name": "sum:Raw", "shelf": "columns",
                         "aggregation": "sum"},
                    ],
                }],
                "dashboards": [{
                    "name": "NeutralOverview",
                    "size": {"width": 1280, "height": 720},
                    "objects": [{
                        "type": "worksheetReference",
                        "worksheetName": "NeutralView",
                        "position": {"x": 20, "y": 20, "w": 600, "h": 400},
                    }],
                }],
                "calculations": [],
            }
            generator = PowerBIProjectGenerator(
                output_dir=str(self.root / "output"))
            self.project = Path(generator.generate_project(
                "NeutralProject", self.extracted))
        self.graph_path = self.project / "powerbi_lineage.json"
        self.graph = json.loads(self.graph_path.read_text(encoding="utf-8"))

    def _write_graph(self, graph):
        self.graph_path.write_text(json.dumps(graph), encoding="utf-8")

    def test_parser_merge_real_generated_graph_quality_report_chain(self):
        self.assertFalse((self.project / "partial_generation.json").exists())
        # Match the actual batch pipeline: source inventory remains original,
        # while the post-Prep binding snapshot is supplemental quality input.
        quality_input = deepcopy(self.extracted)
        quality_input["prep_datasources"] = quality_input["datasources"]
        quality_input["datasources"] = _workbook()
        with redirect_stdout(StringIO()):
            report = build_quality_report(
                quality_input, str(self.project), "NeutralProject",
                prep_flow=True)
        comparison = report.to_dict()["lineage"]["prep_comparison"]
        self.assertEqual(comparison["schema_version"], "1.0")
        self.assertEqual(comparison["status"], "static_evidence")
        self.assertEqual(comparison["runtime"], "not_run")
        self.assertEqual(comparison["value_parity"], "not_run")
        self.assertEqual(comparison["graph"], {
            "path": "powerbi_lineage.json", "status": "available",
            "diagnostics": [],
        })
        self.assertEqual(comparison["summary"]["resolved"], 1)
        self.assertEqual(comparison["summary"]["manual_nodes"], 1)
        self.assertEqual(comparison["coverage"]["unused_outputs"],
                         ["Unused_Output"])
        row, = comparison["bindings"]
        self.assertEqual(row["source_path"],
                         "prep_datasources[0].prep_bindings[0]")
        self.assertEqual(row["provenance"]["flow"], "neutral.tfl")
        self.assertEqual(row["provenance"]["output_node_id"], "out")
        self.assertEqual(row["table"], _TABLE)
        self.assertEqual(row["output"], _TABLE)
        self.assertEqual(row["resolution"], "resolved")
        expected = {node["id"]: node for node in self.graph["nodes"]}
        actual = {node["id"]: node for node in row["generated_nodes"]}
        self.assertTrue(actual)
        table, = [node for node in actual.values()
                  if node["type"] == "table" and node["label"] == _TABLE]
        self.assertEqual(table, expected[table["id"]])
        self.assertTrue({"partition", "column", "visual", "page"}
                        .issubset({node["type"] for node in actual.values()}))
        for node in actual.values():
            self.assertEqual(node, expected[node["id"]])
            for evidence in node["evidence"]:
                self.assertFalse(Path(evidence["path"]).is_absolute())
                self.assertTrue((self.project / evidence["path"]).is_file())
        graph_edges = {edge["id"]: edge for edge in self.graph["edges"]}
        self.assertTrue(row["generated_edges"])
        self.assertIn("projects", {edge["type"]
                                  for edge in row["generated_edges"]})
        for edge in row["generated_edges"]:
            self.assertEqual(edge, graph_edges[edge["id"]])
            self.assertIn(edge["source"], actual)
            self.assertIn(edge["target"], actual)
            self.assertTrue((self.project / edge["evidence"]["path"]).is_file())
        json_path = self.root / "quality.json"
        report.save_json(str(json_path))
        self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))
                         ["lineage"]["prep_comparison"], comparison)
        html_path = self.root / "quality.html"
        report.save_html(str(html_path))
        html = html_path.read_text(encoding="utf-8")
        self.assertIn('id="quality-prep-comparison"', html)
        self.assertIn('data-i18n-en="Prep flow → Power BI comparison"', html)
        self.assertIn('data-i18n-fr="Comparaison flux Prep → Power BI"', html)
        self.assertIn("neutral.tfl", html)
        self.assertIn("ManualReview", html)
        self.assertIn("Unused_Output", html)
        self.assertIn("not_run", html)

    def test_no_prep_is_not_applicable_even_when_graph_exists(self):
        comparison = _prep_comparison(
            {"datasources": _workbook(published=False)}, str(self.project))
        self.assertEqual(comparison["status"], "not_applicable")
        self.assertEqual(comparison["bindings"], [])
        self.assertEqual(comparison["runtime"], "not_run")
        self.assertEqual(comparison["value_parity"], "not_run")
        self.assertEqual(_render_prep_comparison(comparison), "")

    def test_transitive_dax_consumers_reach_chained_measures_and_visual_only(self):
        bound_table, = [node for node in self.graph["nodes"]
                        if node["type"] == "table" and node["label"] == _TABLE]
        table_path = self.project / bound_table["evidence"][0]["path"]
        content = table_path.read_text(encoding="utf-8")
        partition_start = content.index("\tpartition ")
        table_path.write_text(
            content[:partition_start]
            + f"\tcolumn Derived = '{_TABLE}'[Raw] * 2\n"
              "\t\tdataType: int64\n"
            + content[partition_start:], encoding="utf-8")
        # Measures intentionally live outside the bound table. Its contains
        # edges cannot explain their inclusion: reverse DAX traversal must.
        (table_path.parent / "DerivedMetrics.tmdl").write_text(
            "table DerivedMetrics\n"
            "\tcolumn MetricKey\n"
            "\t\tdataType: int64\n"
            "\t\tsourceColumn: MetricKey\n"
            f"\tmeasure Base = SUM('{_TABLE}'[Derived])\n"
            "\tmeasure Adjusted = [Base] + 1\n"
            "\tpartition DerivedMetrics = m\n"
            "\t\tmode: import\n"
            "\t\tsource =\n"
            "\t\t\tlet Source = #table(type table [MetricKey = Int64.Type], "
            "{{1}}) in Source\n", encoding="utf-8")
        (table_path.parent / "Unrelated_Static.tmdl").write_text(
            "table Unrelated_Static\n"
            "\tcolumn UnrelatedValue\n"
            "\t\tdataType: int64\n"
            "\t\tsourceColumn: UnrelatedValue\n"
            "\tmeasure UnrelatedTotal = SUM('Unrelated_Static'[UnrelatedValue])\n"
            "\tpartition Unrelated_Static = m\n"
            "\t\tmode: import\n"
            "\t\tsource =\n"
            "\t\t\tlet Source = #table(type table [UnrelatedValue = Int64.Type], "
            "{{1}}) in Source\n", encoding="utf-8")
        visual_node = next(node for node in self.graph["nodes"]
                           if node["type"] == "visual")
        visual_path = self.project / visual_node["evidence"][0]["path"]
        visual = json.loads(visual_path.read_text(encoding="utf-8"))
        visual["visual"]["query"] = {"queryState": {"Values": {
            "projections": [{
                "field": {"Measure": {
                    "Expression": {"SourceRef": {"Entity": "DerivedMetrics"}},
                    "Property": "Adjusted",
                }},
                "queryRef": "DerivedMetrics.Adjusted",
            }],
        }}}
        visual_path.write_text(json.dumps(visual), encoding="utf-8")
        write_powerbi_lineage_artifacts(self.project)
        graph = json.loads(self.graph_path.read_text(encoding="utf-8"))
        by_label = {node["label"]: node for node in graph["nodes"]}
        raw = by_label[f"{_TABLE}.Raw"]
        derived = by_label[f"{_TABLE}.Derived"]
        base = by_label["DerivedMetrics.Base"]
        adjusted = by_label["DerivedMetrics.Adjusted"]
        rebuilt_visual, = [node for node in graph["nodes"]
                           if node["id"] == visual_node["id"]]
        chain_edges = []
        for owner, dependency in ((derived, raw), (base, derived), (adjusted, base)):
            edge, = [edge for edge in graph["edges"]
                     if edge["type"] == "depends_on"
                     and edge["source"] == owner["id"]
                     and edge["target"] == dependency["id"]]
            self.assertEqual(edge["status"], "exact")
            self.assertEqual(edge["evidence"]["role"], "dax_reference")
            self.assertTrue((self.project / edge["evidence"]["path"]).is_file())
            self.assertIsInstance(edge["evidence"]["line"], int)
            chain_edges.append(edge)
        projection, = [edge for edge in graph["edges"]
                       if edge["type"] == "projects"
                       and edge["source"] == rebuilt_visual["id"]]
        self.assertEqual(projection["status"], "exact")
        self.assertEqual(projection["target"], adjusted["id"])
        self.assertEqual(projection["evidence"]["path"],
                         visual_path.relative_to(self.project).as_posix())
        comparison = _prep_comparison(self.extracted, str(self.project))
        row, = comparison["bindings"]
        self.assertEqual(row["resolution"], "resolved")
        selected_ids = {node["id"] for node in row["generated_nodes"]}
        self.assertTrue({raw["id"], derived["id"], base["id"], adjusted["id"],
                         rebuilt_visual["id"]}.issubset(selected_ids))
        for edge in [*chain_edges, projection]:
            self.assertIn(edge, row["generated_edges"])
        unrelated_ids = {
            node["id"] for node in graph["nodes"]
            if node["label"] == "Unrelated_Static"
            or node["label"].startswith("Unrelated_Static.")
        }
        self.assertTrue(unrelated_ids)
        self.assertTrue(unrelated_ids.isdisjoint(selected_ids))
        self.assertFalse(any(edge["source"] in unrelated_ids
                             or edge["target"] in unrelated_ids
                             for edge in row["generated_edges"]))
        self.assertEqual(graph["runtime_status"], "not_run")
        self.assertEqual(comparison["runtime"], "not_run")
        self.assertEqual(comparison["value_parity"], "not_run")

    def test_supplemental_prep_snapshot_is_preferred_without_replacing_source(self):
        extracted = deepcopy(self.extracted)
        expected = deepcopy(extracted["datasources"])
        extracted["prep_datasources"] = extracted["datasources"]
        extracted["datasources"] = _workbook(published=False)
        before = deepcopy(extracted)
        comparison = _prep_comparison(extracted, str(self.project))
        self.assertEqual(extracted, before)
        self.assertEqual(comparison["status"], "static_evidence")
        self.assertEqual(comparison["summary"]["resolved"], 1)
        self.assertEqual(comparison["coverage"],
                         expected[0]["prep_binding_coverage"])
        self.assertEqual(comparison["bindings"][0]["provenance"],
                         expected[0]["prep_bindings"][0]["provenance"])

    def test_missing_generated_target_is_unresolved_not_name_inferred(self):
        extracted = deepcopy(self.extracted)
        extracted["datasources"][0]["prep_bindings"][0]["table"] = "MissingTarget"
        comparison = _prep_comparison(extracted, str(self.project))
        row, = comparison["bindings"]
        self.assertEqual(row["resolution"], "unresolved")
        self.assertIn("generated_target_missing", row["diagnostics"])
        self.assertEqual(row["target_candidates"], [])
        self.assertEqual(row["generated_nodes"], [])
        self.assertEqual(row["generated_edges"], [])
        self.assertEqual(comparison["summary"]["unresolved"], 1)

    def test_duplicate_generated_table_identity_is_ambiguous_never_first_match(self):
        graph = deepcopy(self.graph)
        duplicate = deepcopy(next(node for node in graph["nodes"]
                                  if node["type"] == "table"
                                  and node["label"] == _TABLE))
        duplicate["id"] += "-synthetic-duplicate"
        duplicate["label"] = _TABLE.lower()
        graph["nodes"].append(duplicate)
        self._write_graph(graph)
        comparison = _prep_comparison(self.extracted, str(self.project))
        row, = comparison["bindings"]
        self.assertEqual(row["resolution"], "ambiguous")
        self.assertEqual(len(row["target_candidates"]), 2)
        self.assertIn("duplicate_generated_target", row["diagnostics"])
        self.assertEqual(row["generated_nodes"], [])
        self.assertEqual(row["generated_edges"], [])
        self.assertEqual(comparison["summary"]["ambiguous"], 1)

    def test_missing_graph_is_not_silently_rescanned_or_repaired(self):
        self.graph_path.unlink()
        comparison = _prep_comparison(self.extracted, str(self.project))
        self.assertEqual(comparison["graph"]["status"], "missing")
        self.assertIn("generated_graph_missing", comparison["diagnostics"])
        self.assertEqual(comparison["bindings"][0]["resolution"], "unresolved")
        self.assertIn("generated_graph_unavailable",
                      comparison["bindings"][0]["diagnostics"])
        self.assertFalse(self.graph_path.exists())

    def test_malformed_graph_contracts_do_not_claim_available_evidence(self):
        cases = {}
        duplicate = deepcopy(self.graph)
        duplicate["nodes"].append(deepcopy(duplicate["nodes"][0]))
        cases["duplicate_node_id"] = duplicate
        dangling = deepcopy(self.graph)
        dangling["edges"][0]["target"] = "missing-endpoint"
        cases["dangling_edge"] = dangling
        for path in ("../outside.tmdl", "C:/outside.tmdl", "/outside.tmdl"):
            unsafe = deepcopy(self.graph)
            unsafe["nodes"][0]["evidence"][0]["path"] = path
            cases[path] = unsafe
        cases["wrong_version"] = {**self.graph, "schema_version": "999"}
        cases["empty_nodes"] = {**self.graph, "nodes": []}
        for name, graph in cases.items():
            with self.subTest(case=name):
                self._write_graph(graph)
                comparison = _prep_comparison(
                    self.extracted, str(self.project))
                self.assertEqual(comparison["graph"]["status"], "invalid")
                self.assertIn("generated_graph_invalid_contract",
                              comparison["diagnostics"])
                self.assertEqual(comparison["summary"]["resolved"], 0)
                self.assertEqual(comparison["bindings"][0]["generated_nodes"], [])
        self.graph_path.write_text("{invalid", encoding="utf-8")
        comparison = _prep_comparison(self.extracted, str(self.project))
        self.assertEqual(comparison["graph"]["status"], "invalid")
        self.assertIn("generated_graph_unreadable_or_invalid_json",
                      comparison["diagnostics"])

    def test_manual_placeholder_is_not_promoted_to_conversion_or_value_parity(self):
        comparison = _prep_comparison(self.extracted, str(self.project))
        row, = comparison["bindings"]
        manual, = [node for node in row["provenance"]["nodes"]
                   if node["status"] == "manual"]
        self.assertEqual(manual["id"], "manual")
        self.assertEqual(row["manual_nodes"], 1)
        self.assertEqual(row["resolution"], "resolved")
        self.assertEqual(comparison["runtime"], "not_run")
        self.assertEqual(comparison["value_parity"], "not_run")
        html = _render_prep_comparison(comparison)
        self.assertIn("Static correspondence only", html)
        self.assertIn("Correspondance statique uniquement", html)
        self.assertIn("does not prove", html)
        self.assertIn("manual", html)

    def test_comparison_whitelists_metadata_and_excludes_secrets_and_formulas(self):
        extracted = deepcopy(self.extracted)
        ds = extracted["datasources"][0]
        binding = ds["prep_bindings"][0]
        binding["m_query"] = _SECRET
        binding["provenance"]["flow"] = "C:\\private\\neutral.tfl"
        binding["provenance"]["connection"] = {"password": _SECRET}
        binding["provenance"]["nodes"][0]["expression"] = _SECRET
        ds["prep_binding_coverage"]["matched"][0]["password"] = _SECRET
        graph = deepcopy(self.graph)
        graph["nodes"][0]["m_text"] = _SECRET
        graph["edges"][0]["expression"] = _SECRET
        graph["edges"][0]["evidence"]["connection"] = _SECRET
        self._write_graph(graph)
        comparison = _prep_comparison(extracted, str(self.project))
        self.assertEqual(comparison["bindings"][0]["provenance"]["flow"],
                         "neutral.tfl")
        for content in (json.dumps(comparison), _render_prep_comparison(comparison)):
            self.assertNotIn(_SECRET, content)
            self.assertNotIn("private", content)
            self.assertNotIn("m_query", content)
            self.assertNotIn("password", content)
            self.assertNotIn("m_text", content)
        self.assertEqual(comparison["graph"]["status"], "available")


class TestPrepComparisonBatchPipeline(unittest.TestCase):
    """Run the public batch dispatcher and real worker, not a surrogate worker."""

    def _batch(self, directory, *, output_format="pbip", partial=False,
               generation_ok=True, snapshot_present=True, report_intended=True):
        import migrate
        from powerbi_import import import_to_powerbi

        root = Path(directory)
        sources = root / "sources"
        sources.mkdir()
        (sources / "Neutral.twb").write_text("<workbook/>", encoding="utf-8")
        prep_path = root / "neutral.tfl"
        prep_path.write_text("{}", encoding="utf-8")
        extracted_dir = root / "extracted"
        extracted_dir.mkdir()
        output = root / "output"
        original = {"datasources": _workbook(),
                    "worksheets": [{"name": "NeutralView"}] if report_intended else [],
                    "dashboards": [], "calculations": []}
        snapshot = deepcopy(original) if snapshot_present else {}
        merged = _workbook()
        merged[0]["prep_bindings"] = [{
            "table": _TABLE, "output": _TABLE,
            "method": "exact_name_or_caption", "provenance": {
                "flow": "neutral.tfl", "output_node_id": "out",
                "output_name": _TABLE,
                "nodes": [{"id": "out", "name": _TABLE, "kind": "output",
                           "upstream_ids": [], "status": "converted"}],
            },
        }]
        merged[0]["prep_binding_coverage"] = {
            "status": "complete", "matched": [], "unmatched_published": [],
            "unmatched_local": [], "unused_outputs": [], "secondary_queries": [],
        }
        events = []
        datasource_reads = []
        real_open = open

        def watch_open(file, mode="r", *args, **kwargs):
            if Path(file) == extracted_dir / "datasources.json" and "r" in mode:
                datasource_reads.append(str(file))
            return real_open(file, mode, *args, **kwargs)

        def prep(*args, **kwargs):
            events.append("prep")
            (extracted_dir / "datasources.json").write_text(
                json.dumps(merged), encoding="utf-8")
            return True

        def generate(**kwargs):
            events.append("generate")
            project = Path(kwargs["output_dir"]) / kwargs["report_name"]
            project.mkdir(parents=True, exist_ok=True)
            if partial:
                (project / "partial_generation.json").write_text(
                    '{"data_retained": true, "errors": []}', encoding="utf-8")
            if generation_ok:
                (project / (kwargs["report_name"] + ".pbip")).write_text(
                    "{}", encoding="utf-8")
            return generation_ok

        quality_report = Mock(status="WARN")
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(StringIO()))
            stack.enter_context(patch.dict(
                sys.modules, {"import_to_powerbi": import_to_powerbi}))
            stack.enter_context(patch.object(
                import_to_powerbi.PowerBIImporter, "_load_converted_objects",
                return_value=snapshot))
            stack.enter_context(patch("builtins.open", side_effect=watch_open))
            for name, value in {
                "setup_logging": None, "_install_upload_copy_guard": None,
                "_get_extract_dir": str(extracted_dir),
                "run_extraction": True,
                "run_migration_report": {},
                "_stage_declared_local_sources": {},
                "_collect_local_data_files": {},
                "_convert_hyper_to_csv_in_data": {},
                "_fix_twb_data_folder": None,
                "_set_data_folder_for_hyper_exports": None,
                "_rebind_staged_file_partitions": None,
                "_postprocess_spatial_sources": {"mapped": 0},
                "_sync_final_calendar_source_expressions": None,
                "_require_hyper_import": None,
                "run_batch_html_dashboard": None,
                "_run_batch_prep_lineage": None,
                "_finalize_failed_workbook": None,
            }.items():
                stack.enter_context(patch.object(migrate, name, return_value=value))
            stack.enter_context(patch.object(
                migrate, "run_prep_flow", side_effect=prep))
            stack.enter_context(patch.object(
                migrate, "run_generation", side_effect=generate))
            stack.enter_context(patch.object(
                migrate, "_export_power_query_files",
                side_effect=lambda *a, **k: events.append("export_m")))
            stack.enter_context(patch.object(
                migrate, "_shorten_report_artifact_path",
                side_effect=lambda *a, **k: events.append("shorten")))
            stack.enter_context(patch.object(
                migrate, "_run_openability_gate",
                side_effect=lambda *a, **k: events.append("openability") or True))
            writer = stack.enter_context(patch(
                "powerbi_import.powerbi_internal_lineage.write_powerbi_lineage_artifacts",
                side_effect=lambda *a, **k: events.append("lineage")))
            quality = stack.enter_context(patch(
                "powerbi_import.migration_quality.build_quality_report",
                side_effect=lambda *a, **k: events.append("quality") or quality_report))
            stack.enter_context(patch(
                "powerbi_import.evidence_package.write_evidence_package",
                side_effect=lambda *a, **k: events.append("evidence")))
            result = migrate.run_batch_migration(
                str(sources), output_dir=str(output), prep_file=str(prep_path),
                output_format=output_format, verify_open=True,
                salvage_partial=False)
        return {
            "result": result, "events": events, "quality": quality,
            "writer": writer, "snapshot": snapshot, "original": original,
            "merged": merged, "reads": datasource_reads,
            "project": output / "Neutral",
        }

    def test_batch_keeps_source_snapshot_and_passes_postprep_snapshot_to_quality(self):
        import migrate
        with tempfile.TemporaryDirectory() as directory:
            run = self._batch(directory)
        self.assertEqual(run["result"], migrate.ExitCode.SUCCESS)
        self.assertEqual(len(run["reads"]), 1)
        run["quality"].assert_called_once()
        received = run["quality"].call_args.args[0]
        self.assertEqual(received["datasources"], run["original"]["datasources"])
        self.assertEqual(received["prep_datasources"], run["merged"])
        self.assertNotIn("prep_bindings", received["datasources"][0])
        self.assertTrue(run["quality"].call_args.kwargs["prep_flow"])
        run["writer"].assert_called_once()
        self.assertEqual(Path(run["writer"].call_args.args[0]), run["project"])
        events = run["events"]
        for earlier, later in (
            ("prep", "generate"), ("generate", "export_m"),
            ("export_m", "shorten"), ("shorten", "lineage"),
            ("lineage", "openability"), ("openability", "quality"),
            ("quality", "evidence"),
        ):
            self.assertLess(events.index(earlier), events.index(later))

    def test_batch_never_refreshes_graph_for_partial_fabric_or_failed_generation(self):
        for options in ({"partial": True}, {"output_format": "fabric"},
                        {"generation_ok": False}):
            with self.subTest(options=options), \
                    tempfile.TemporaryDirectory() as directory:
                run = self._batch(directory, **options)
                run["writer"].assert_not_called()
                self.assertNotIn("lineage", run["events"])
                if options.get("generation_ok") is False:
                    run["quality"].assert_not_called()

    def test_absent_extraction_snapshot_does_not_create_prep_only_quality_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self._batch(directory, snapshot_present=False)
        self.assertEqual(run["snapshot"], {})
        self.assertEqual(run["reads"], [])
        run["quality"].assert_not_called()

    def test_batch_does_not_refresh_graph_when_no_report_is_intended(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self._batch(directory, report_intended=False)
            self.assertTrue(run["project"].is_dir())
            self.assertFalse(any(run["project"].glob("*.Report")))
        run["writer"].assert_not_called()
        self.assertNotIn("lineage", run["events"])


if __name__ == "__main__":
    unittest.main()
