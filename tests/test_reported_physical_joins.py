"""Synthetic regressions for declared physical joins and partial PBIP output.

No source is queried.  The packaged Hyper fixture proves only the extractor's
declared object/field binding; catalog validation and export remain pending.
"""

import json
import os
import socket
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from copy import deepcopy
from pathlib import Path
from unittest import mock

from powerbi_import.errors import GenerationError, PartialGenerationError
from powerbi_import.migration_quality import build_quality_report
from powerbi_import.openability import check_openability
from powerbi_import.pbip_generator import PowerBIProjectGenerator
from powerbi_import.relationship_inference_v2 import RelationshipInferenceEngine
from powerbi_import import shared_model
from powerbi_import.shared_model import (
    MergeAssessment,
    MergeManifest,
    add_to_model,
    build_merge_manifest,
    merge_semantic_models,
    remove_from_model,
    suggest_cross_workbook_relationships,
)
from powerbi_import.tmdl_generator import _build_semantic_model, generate_tmdl
from powerbi_import.tmdl_relationships import _predicate_resolution
from tableau_export.datasource_extractor import extract_datasource


SOURCE = "ReportedSource"
REPORTED_PAIR = frozenset(("Orders", "Lookup"))
OTHER_PAIR = frozenset(("Inventory", "Products"))


def _comparison(parent, left, right):
    expression = ET.SubElement(parent, "expression", op="=")
    ET.SubElement(expression, "expression", op=left)
    ET.SubElement(expression, "expression", op=right)


def _comparison_with_if(left, condition, when_true, when_false):
    comparison = ET.Element("expression", op="=")
    ET.SubElement(comparison, "expression", op=left)
    conditional = ET.SubElement(comparison, "expression", op="IF")
    for operand in (condition, when_true, when_false):
        ET.SubElement(conditional, "expression", op=operand)
    return comparison


def _join(left, right, terms):
    relation = ET.Element("relation", type="join", join="left")
    clause = ET.SubElement(relation, "clause", type="join")
    parent = (ET.SubElement(clause, "expression", op="AND")
              if len(terms) > 1 else clause)
    for term in terms:
        if isinstance(term, ET.Element):
            parent.append(term)
        elif isinstance(term, str):
            ET.SubElement(parent, "expression", op=term)
        else:
            _comparison(parent, *term)
    relation.extend((left, right))
    return relation


def _source_xml(*, kind="mixed", reverse=False, nested=None,
                independent=False, unique_fields=False, connector="sqlserver"):
    """Build a real extractor input, not a hand-written relation_metadata tree."""
    left_shared = "OrdersSharedID" if unique_fields else "SharedID"
    right_shared = "LookupSharedID" if unique_fields else "SharedID_Lookup"
    fields = {
        "Orders": [
            ("OrderKey", "integer"), (left_shared, "integer"),
            ("OrderFlag", "boolean"), ("RegionID", "integer"),
        ],
        "Lookup": [
            ("LookupKey", "integer"), (right_shared, "integer"),
            ("LookupFlag", "boolean"),
        ],
    }
    if nested:
        fields["Regions"] = [("RegionID_Regions", "integer"), ("RegionLabel", "string")]
    if independent:
        fields["Inventory"] = [("ProductID", "integer"), ("Units", "integer")]
        fields["Products"] = [("ProductID_Products", "integer"), ("ProductName", "string")]

    datasource = ET.Element("datasource", name=SOURCE)
    connection = ET.SubElement(datasource, "connection", {"class": "federated"})
    for alias in fields:
        named = ET.SubElement(connection, "named-connection", name=alias + "-ref")
        if connector == "excel-direct":
            attributes = {
                "class": connector, "directory": "Data", "filename": alias + ".xlsx",
            }
        else:
            attributes = {
                "class": connector, "server": "synthetic.example.invalid",
                "dbname": "synthetic_database", "port": "1433",
            }
        ET.SubElement(named, "connection", attributes)

    def table(alias):
        source = (f"[{alias}$]" if connector == "excel-direct"
                  else f"[dbo].[{alias.lower()}]")
        result = ET.Element(
            "relation", type="table", name=alias,
            connection=alias + "-ref", table=source,
        )
        columns = ET.SubElement(result, "columns")
        for ordinal, (name, datatype) in enumerate(fields[alias]):
            ET.SubElement(
                columns, "column", name=name, datatype=datatype, ordinal=str(ordinal))
        return result

    terms = [("[Orders].[OrderKey]", "[Lookup].[LookupKey]")]
    if kind == "mixed":
        terms += [
            (f"IF [Orders].[OrderFlag] THEN [Orders].[{left_shared}] ELSE NULL END",
             f"[Lookup].[{right_shared}]"),
            (f"[Orders].[{left_shared}]",
             f"IF [Lookup].[LookupFlag] THEN [Lookup].[{right_shared}] ELSE NULL END"),
        ]
    elif kind == "if_terms":
        terms += [
            f"IF [Orders].[OrderFlag] THEN [Orders].[{left_shared}] = "
            f"[Lookup].[{right_shared}] ELSE FALSE END",
            f"IF [Lookup].[LookupFlag] THEN [Orders].[RegionID] = "
            "[Lookup].[LookupKey] ELSE FALSE END",
        ]
    elif kind == "partial_if":
        terms.append(_comparison_with_if(
            "[Orders].[RegionID]", "[Orders].[OrderFlag]", '"north"', '"south"'))
    elif kind == "opaque_if":
        terms = [_comparison_with_if(
            "[Orders].[RegionID]", "[Orders].[OrderFlag]", '"north"', '"south"')]
    elif kind == "composite":
        terms.append((f"[Orders].[{left_shared}]", f"[Lookup].[{right_shared}]"))
    elif kind not in ("single", "opaque_if"):
        raise ValueError(kind)

    aliases = ("Orders", "Lookup")
    if reverse:
        aliases = tuple(reversed(aliases))
        terms = [(term[1], term[0]) if isinstance(term, tuple) else term
                 for term in terms]
    inner = _join(table(aliases[0]), table(aliases[1]), terms)
    if nested:
        outer_terms = [("[Orders].[RegionID]", "[Regions].[RegionID_Regions]")]
        if nested == "left":
            inner = _join(inner, table("Regions"), outer_terms)
        elif nested == "right":
            inner = _join(
                table("Regions"), inner,
                [(right, left) for left, right in outer_terms],
            )
        else:
            raise ValueError(nested)
    connection.append(inner)
    if independent:
        connection.extend((table("Inventory"), table("Products")))

    records = ET.SubElement(connection, "metadata-records")
    maps = ET.SubElement(connection, "cols")
    declared = set()
    for alias, columns in fields.items():
        for name, datatype in columns:
            record = ET.SubElement(records, "metadata-record", {"class": "column"})
            for tag, value in (
                ("local-name", f"[{name}]"), ("remote-name", name),
                ("parent-name", f"[{alias}]"), ("local-type", datatype),
            ):
                ET.SubElement(record, tag).text = value
            ET.SubElement(maps, "map", key=f"[{name}]", value=f"[{alias}].[{name}]")
            if name not in declared:
                ET.SubElement(datasource, "column", name=f"[{name}]", datatype=datatype)
                declared.add(name)
    return datasource, fields


def _attach_snapshot(datasource, fields, *, enabled="true"):
    """Declare one extract object matching the entire native join and its fields."""
    connection = datasource.find("./connection")
    native = connection.find("./relation")
    extract = ET.SubElement(datasource, "extract", enabled=enabled)
    hyper = ET.SubElement(
        extract, "connection", {
            "class": "hyper", "dbname": "Data/materialized.hyper",
            "schema": "Extract", "tablename": "JoinedRows",
        })
    physical = ET.SubElement(
        hyper, "relation", type="table", name="Extract",
        table="[Extract].[JoinedRows]",
    )
    columns = ET.SubElement(physical, "columns")
    records = ET.SubElement(hyper, "metadata-records")
    maps = ET.SubElement(hyper, "cols")
    for alias_fields in fields.values():
        for local, datatype in alias_fields:
            remote = "snapshot_" + local
            ET.SubElement(columns, "column", name=local, datatype=datatype)
            record = ET.SubElement(records, "metadata-record", {"class": "column"})
            for tag, value in (
                ("local-name", f"[{local}]"), ("remote-name", remote),
                ("parent-name", "[Extract]"), ("local-type", datatype),
            ):
                ET.SubElement(record, tag).text = value
            ET.SubElement(maps, "map", key=f"[{local}]", value=f"[Extract].[{remote}]")
    graph = ET.SubElement(datasource, "object-graph")
    obj = ET.SubElement(
        ET.SubElement(graph, "objects"), "object",
        id="joined-object", caption="Joined Snapshot",
    )
    ET.SubElement(obj, "properties", context="").append(deepcopy(native))
    ET.SubElement(obj, "properties", context="extract").append(deepcopy(physical))


def _source_pair(relationship):
    return frozenset(
        relationship.get(side, {}).get("table") for side in ("left", "right"))


def _model_pair(relationship):
    return frozenset((relationship.get("fromTable"), relationship.get("toTable")))


def _model_endpoints(relationship):
    return frozenset((
        (relationship.get("fromTable"), relationship.get("fromColumn")),
        (relationship.get("toTable"), relationship.get("toColumn")),
    ))


def _project_objects(datasource):
    return {
        "datasources": [datasource],
        "worksheets": [{
            "name": "Rows", "chart_type": "tableEx",
            "fields": [{"name": "OrderKey", "shelf": "rows"}],
        }],
        "dashboards": [{
            "name": "Overview", "size": {"width": 1280, "height": 720},
            "objects": [{
                "type": "worksheetReference", "worksheetName": "Rows",
                "position": {"x": 40, "y": 40, "w": 500, "h": 300},
            }],
        }],
    }


def _metadata_free_inference_source(name):
    """Two tied candidate keys; all identities are identical across calls."""
    return {
        "name": name,
        "tables": [{
            "name": table,
            "columns": [
                {"name": "AlphaID", "datatype": "integer"},
                {"name": "BetaID", "datatype": "integer"},
            ],
        } for table in ("Orders", "Lookup")],
        "relationships": [],
        "relation_metadata": {},
    }


class TestReportedPhysicalJoins(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="reported-physical-joins-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in (
            "TTPBI_COMPOSITE_RELATIONSHIP_POLICIES",
            "TTPBI_LOGICAL_RELATIONSHIP_POLICIES",
            "TTPBI_UPLOAD_ROOT",
            "TTPBI_EXTRACT_DIR",
        ):
            os.environ.pop(name, None)

        # Also fail if an optional, best-effort path catches an attempted call.
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

    def source(self, **options):
        xml, _ = _source_xml(**options)
        return extract_datasource(xml)

    def snapshot_source(self, *, kind="mixed", enabled="true"):
        xml, fields = _source_xml(
            kind=kind, unique_fields=True, connector="excel-direct")
        _attach_snapshot(xml, fields, enabled=enabled)
        workbook = self.root / f"{kind}-{enabled}.twbx"
        with zipfile.ZipFile(workbook, "w") as archive:
            archive.writestr("Data/materialized.hyper", b"not a validated Hyper catalog")
        return extract_datasource(xml, str(workbook)), workbook

    def preprocess(self, generator, datasource):
        objects = {"datasources": [datasource]}
        return generator._preprocess_for_fidelity("Reported", objects)["datasources"][0]

    def assert_inference_recomputed(self, first, second):
        def schema(datasource):
            return sorted(
                (table["name"], tuple(sorted(column["name"] for column in table["columns"])))
                for table in datasource["tables"]
            )

        self.assertEqual(schema(first), schema(second))
        datasources = (first, second)
        expected = []
        for datasource in datasources:
            candidate, = RelationshipInferenceEngine().infer_relationships(
                datasource["tables"])
            expected.append(candidate)
        generator = PowerBIProjectGenerator(output_dir=str(self.root))
        real_infer = RelationshipInferenceEngine.infer_relationships
        with mock.patch.object(
            RelationshipInferenceEngine, "infer_relationships",
            autospec=True, side_effect=real_infer,
        ) as infer:
            for datasource in datasources:
                self.preprocess(generator, datasource)
        self.assertEqual(infer.call_count, 2)
        for datasource, candidate in zip(datasources, expected):
            edge, = datasource["relationships"]
            self.assertEqual(
                (edge["left"]["table"], edge["left"]["column"],
                 edge["right"]["table"], edge["right"]["column"], edge["confidence"]),
                (candidate["fromTable"], candidate["fromColumn"],
                 candidate["toTable"], candidate["toColumn"], candidate["confidence"]),
            )

    def assert_physical_join_blocked(self, datasource):
        with self.assertRaises(PartialGenerationError) as raised:
            _build_semantic_model(
                [deepcopy(datasource)], report_name="Reported", model_mode="import")
        error = raised.exception
        self.assertEqual(error.source, datasource["name"])
        message = str(error)
        self.assertIn("Orders", message)
        self.assertIn("Lookup", message)
        self.assertRegex(message, r"(?i)(physical|join|predicate|condition)")
        advice = " ".join([message, *map(str, error.required_evidence)])
        self.assertRegex(advice, r"(?i)(materializ|complete|full|preserv|verify|evidence)")
        return error

    def test_extractor_preserves_the_full_left_join_and_no_legacy_edge(self):
        datasource = self.source()
        self.assertEqual({t["name"] for t in datasource["tables"]}, {"Orders", "Lookup"})
        self.assertEqual(datasource["relationships"], [])
        metadata = datasource["relation_metadata"]
        self.assertEqual(metadata["logical_relationships"], [])
        tree, = metadata["physical_relations"]
        self.assertEqual(tree["type"], "join")
        self.assertEqual(tree["join_type"], "left")
        self.assertEqual([c["name"] for c in tree["children"]], ["Orders", "Lookup"])
        self.assertTrue(tree["requires_materialization"])
        predicate = tree["predicate"]
        self.assertEqual(predicate["operator"], "AND")
        first, second, third = predicate["arguments"]
        self.assertEqual([term["operator"] for term in (first, second, third)],
                         ["=", "=", "="])
        self.assertEqual((first["left"]["table"], first["left"]["column"]),
                         ("Orders", "OrderKey"))
        self.assertEqual((first["right"]["table"], first["right"]["column"]),
                         ("Lookup", "LookupKey"))
        self.assertIn("IF [Orders].[OrderFlag]", second["left"]["expression"])
        self.assertIn("IF [Lookup].[LookupFlag]", third["right"]["expression"])

    def test_preprocessing_does_not_guess_for_the_declared_pair_in_either_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasource = self.source(reverse=reverse)
                metadata = deepcopy(datasource["relation_metadata"])
                self.assertEqual(datasource["relationships"], [])
                generator = PowerBIProjectGenerator(output_dir=str(self.root))
                self.preprocess(generator, datasource)
                self.assertFalse(
                    [r for r in datasource["relationships"] if _source_pair(r) == REPORTED_PAIR],
                    datasource["relationships"],
                )
                self.assertEqual(datasource["relation_metadata"], metadata)

    def test_extractor_preserves_if_expressions_as_conjuncts(self):
        datasource = self.source(kind="if_terms")
        self.assertEqual(datasource["relationships"], [])
        tree, = datasource["relation_metadata"]["physical_relations"]
        self.assertEqual(tree["join_type"], "left")
        self.assertTrue(tree["requires_materialization"])
        predicate = tree["predicate"]
        self.assertEqual(predicate["operator"], "AND")
        first, second, third = predicate["arguments"]
        self.assertEqual(first["operator"], "=")
        self.assertTrue(second["operator"].startswith("IF [Orders].[OrderFlag]"))
        self.assertTrue(third["operator"].startswith("IF [Lookup].[LookupFlag]"))

    def test_unmaterialized_if_conjuncts_are_blocked(self):
        datasource = self.source(kind="if_terms")
        self.assertEqual(datasource["relationships"], [])
        self.assert_physical_join_blocked(datasource)

    def test_unmaterialized_complex_join_fails_without_any_inferred_edge(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasource = self.source(reverse=reverse)
                self.assertEqual(datasource["relationships"], [])
                self.assert_physical_join_blocked(datasource)

    def test_injected_partial_edge_cannot_bypass_the_semantic_model_guard(self):
        for inferred in (False, True):
            for reverse in (False, True):
                with self.subTest(inferred=inferred, reverse=reverse):
                    datasource = self.source()
                    endpoints = [
                        {"table": "Orders", "column": "SharedID"},
                        {"table": "Lookup", "column": "SharedID_Lookup"},
                    ]
                    if reverse:
                        endpoints.reverse()
                    datasource["relationships"] = [{
                        "type": "left", "left": endpoints[0], "right": endpoints[1],
                        "inferred": inferred,
                    }]
                    self.assert_physical_join_blocked(datasource)

    def test_declared_single_equality_survives_without_a_second_guessed_key(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasource = self.source(kind="single", reverse=reverse)
                tree, = datasource["relation_metadata"]["physical_relations"]
                self.assertFalse(tree["requires_materialization"])
                declared = deepcopy(datasource["relationships"])
                self.assertEqual(len(declared), 1)
                generator = PowerBIProjectGenerator(output_dir=str(self.root))
                self.preprocess(generator, datasource)
                self.assertEqual(datasource["relationships"], declared)
                model = _build_semantic_model([datasource], model_mode="import")
                relationships = model["model"]["relationships"]
                self.assertEqual(len(relationships), 1, relationships)
                self.assertEqual(
                    _model_endpoints(relationships[0]),
                    frozenset((("Orders", "OrderKey"), ("Lookup", "LookupKey"))),
                )

    def test_semantic_model_never_emits_stale_inference_for_single_equality(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasource = self.source(kind="single")
                endpoints = [
                    {"table": "Orders", "column": "SharedID"},
                    {"table": "Lookup", "column": "SharedID_Lookup"},
                ]
                if reverse:
                    endpoints.reverse()
                datasource["relationships"].append({
                    "type": "left", "left": endpoints[0], "right": endpoints[1],
                    "inferred": True,
                })
                try:
                    model = _build_semantic_model([datasource], model_mode="import")
                except PartialGenerationError:
                    # Rejecting contradictory stale metadata is also fail-closed.
                    continue
                relationships = model["model"]["relationships"]
                self.assertEqual(len(relationships), 1, relationships)
                self.assertEqual(
                    _model_endpoints(relationships[0]),
                    frozenset((("Orders", "OrderKey"), ("Lookup", "LookupKey"))),
                )

    def test_nested_join_preprocessing_protects_the_inner_pair_on_both_sides(self):
        for side in ("left", "right"):
            with self.subTest(nested=side):
                datasource = self.source(nested=side)
                root, = datasource["relation_metadata"]["physical_relations"]
                inner = root["children"][0 if side == "left" else 1]
                self.assertEqual(inner["type"], "join")
                self.assertTrue(inner["requires_materialization"])
                self.assertEqual({_source_pair(r) for r in datasource["relationships"]},
                                 {frozenset(("Orders", "Regions"))})
                declared = deepcopy(datasource["relationships"])
                generator = PowerBIProjectGenerator(output_dir=str(self.root))
                self.preprocess(generator, datasource)
                # The outer join governs both of its subtrees, including the
                # Lookup/Regions pair not mentioned by its equality operand.
                self.assertEqual(datasource["relationships"], declared)

    def test_nested_unmaterialized_join_fails_on_either_child(self):
        for side in ("left", "right"):
            with self.subTest(nested=side):
                datasource = self.source(nested=side, reverse=(side == "right"))
                self.assert_physical_join_blocked(datasource)

    def test_unrelated_pair_in_same_datasource_still_gets_inferred(self):
        datasource = self.source(independent=True)
        generator = PowerBIProjectGenerator(output_dir=str(self.root))
        self.preprocess(generator, datasource)
        inferred = [r for r in datasource["relationships"] if r.get("inferred") is True]
        self.assertTrue(any(_source_pair(r) == OTHER_PAIR for r in inferred), inferred)

    def test_metadata_free_inference_still_reaches_the_semantic_model(self):
        datasource = self.source()
        datasource["relation_metadata"] = {
            "version": 1, "physical_relations": [], "logical_tables": [],
            "logical_relationships": [],
        }
        generator = PowerBIProjectGenerator(output_dir=str(self.root))
        self.preprocess(generator, datasource)
        inferred = [r for r in datasource["relationships"] if r.get("inferred") is True]
        self.assertTrue(any(_source_pair(r) == REPORTED_PAIR for r in inferred), inferred)
        model = _build_semantic_model([datasource], model_mode="import")
        self.assertTrue(
            any(_model_endpoints(r) == frozenset((
                ("Orders", "SharedID"), ("Lookup", "SharedID_Lookup")))
                for r in model["model"]["relationships"]),
            model["model"]["relationships"],
        )

    def test_cached_metadata_free_guess_cannot_leak_into_a_declared_pair(self):
        declared = self.source()
        metadata_free = deepcopy(declared)
        metadata_free["name"] = "IndependentSource"
        metadata_free["relation_metadata"] = {}
        generator = PowerBIProjectGenerator(output_dir=str(self.root))
        self.preprocess(generator, metadata_free)
        self.assertTrue(any(r.get("inferred") is True
                            for r in metadata_free["relationships"]))
        self.preprocess(generator, declared)
        self.assertFalse(
            [r for r in declared["relationships"] if _source_pair(r) == REPORTED_PAIR],
            declared["relationships"],
        )

    def test_declared_pair_does_not_poison_cached_metadata_free_inference(self):
        declared = self.source()
        metadata_free = deepcopy(declared)
        metadata_free["name"] = "IndependentSource"
        metadata_free["relation_metadata"] = {}
        generator = PowerBIProjectGenerator(output_dir=str(self.root))
        self.preprocess(generator, declared)
        self.preprocess(generator, metadata_free)
        self.assertTrue(
            any(r.get("inferred") is True and _source_pair(r) == REPORTED_PAIR
                for r in metadata_free["relationships"]),
            metadata_free["relationships"],
        )

    def test_inference_candidates_do_not_leak_between_different_column_types(self):
        for together in (False, True):
            for reverse in (False, True):
                with self.subTest(together=together, reverse=reverse):
                    alpha = _metadata_free_inference_source("AlphaSource")
                    beta = _metadata_free_inference_source("BetaSource")
                    # Incompatible bool/integer pairs lose the type-score bonus.
                    alpha["tables"][1]["columns"][1]["datatype"] = "boolean"
                    beta["tables"][1]["columns"][0]["datatype"] = "boolean"
                    ordered = [alpha, beta]
                    if reverse:
                        ordered.reverse()
                    generator = PowerBIProjectGenerator(output_dir=str(self.root))
                    if together:
                        generator._preprocess_for_fidelity(
                            "Reported", {"datasources": ordered})
                    else:
                        for datasource in ordered:
                            self.preprocess(generator, datasource)
                    for datasource, key in ((alpha, "AlphaID"), (beta, "BetaID")):
                        inferred, = datasource["relationships"]
                        self.assertIs(inferred["inferred"], True)
                        self.assertEqual(
                            (inferred["left"]["column"], inferred["right"]["column"]),
                            (key, key),
                        )

    def test_inference_candidates_are_recomputed_for_changed_column_order(self):
        original = _metadata_free_inference_source("OriginalOrder")
        reordered = deepcopy(original)
        reordered["name"] = "ReorderedColumns"
        reordered["tables"][0]["columns"].reverse()
        self.assert_inference_recomputed(original, reordered)

    def test_inference_candidates_are_recomputed_for_changed_table_order(self):
        original = _metadata_free_inference_source("OriginalOrder")
        reordered = deepcopy(original)
        reordered["name"] = "ReorderedTables"
        reordered["tables"].reverse()
        self.assert_inference_recomputed(original, reordered)

    def test_changed_key_flags_recompute_the_real_inference_candidate(self):
        for flag in ("is_primary_key", "is_unique"):
            with self.subTest(flag=flag):
                no_key = _metadata_free_inference_source("NoKeyEvidence")
                with_key = deepcopy(no_key)
                with_key["name"] = "WithKeyEvidence"
                with_key["tables"][1]["columns"][0][flag] = True
                generator = PowerBIProjectGenerator(output_dir=str(self.root))
                real_infer = RelationshipInferenceEngine.infer_relationships
                candidates = []

                def record(engine, tables):
                    result = real_infer(engine, tables)
                    candidates.append(deepcopy(result))
                    return result

                # The preprocessor drops cardinality from its returned edge, so
                # observe the real engine rather than inventing a fake score.
                with mock.patch.object(
                    RelationshipInferenceEngine, "infer_relationships",
                    autospec=True, side_effect=record,
                ) as infer:
                    self.preprocess(generator, no_key)
                    self.preprocess(generator, with_key)
                self.assertEqual(infer.call_count, 2)
                self.assertEqual([result[0]["cardinality"] for result in candidates],
                                 ["manyToMany", "manyToOne"])

    def test_composite_equalities_are_not_emitted_as_independent_edges(self):
        datasource = self.source(kind="composite")
        tree, = datasource["relation_metadata"]["physical_relations"]
        self.assertTrue(tree["requires_materialization"])
        self.assertEqual(tree["predicate"]["operator"], "AND")
        self.assertEqual(len(tree["predicate"]["arguments"]), 2)
        # The extractor's legacy projection contains both components. It is
        # not proof that two independent semantic relationships are equivalent.
        self.assertEqual(len(datasource["relationships"]), 2)
        self.assertEqual({_source_pair(r) for r in datasource["relationships"]},
                         {REPORTED_PAIR})
        self.assert_physical_join_blocked(datasource)

    def test_bare_materialization_claim_does_not_excuse_an_active_join(self):
        datasource = self.source()
        datasource["materialized_extract"] = {"object_id": "unverified-object"}
        self.assert_physical_join_blocked(datasource)

    def test_disabled_packaged_snapshot_does_not_prove_the_join_materialized(self):
        datasource, _ = self.snapshot_source(enabled="false")
        self.assertIn("inactive_extract_context", datasource)
        self.assertNotIn("materialized_extract", datasource)
        self.assertEqual({t["name"] for t in datasource["tables"]}, {"Orders", "Lookup"})
        self.assert_physical_join_blocked(datasource)

    def test_bound_snapshot_does_not_replay_native_join_provenance(self):
        for kind in ("mixed", "if_terms", "composite"):
            with self.subTest(kind=kind):
                datasource, workbook = self.snapshot_source(kind=kind)
                provenance = datasource["materialized_extract"]
                self.assertEqual(provenance["binding"], "single_object_materialized_extract")
                self.assertEqual(provenance["field_mapping_validation"],
                                 "exact_declared_extract_metadata")
                self.assertEqual(provenance["catalog_validation"], "required_at_export")
                native, = provenance["native_relation_metadata"]["physical_relations"]
                self.assertTrue(native["requires_materialization"])
                self.assertEqual(native["predicate"]["operator"], "AND")
                active, = datasource["relation_metadata"]["physical_relations"]
                self.assertEqual(active["type"], "table")
                table, = datasource["tables"]
                self.assertEqual(table["name"], "Joined Snapshot")
                self.assertEqual(table["hyper_identity"], {
                    "workbook": str(workbook.resolve()),
                    "archive_member": "Data/materialized.hyper",
                    "schema": "Extract", "table": "JoinedRows",
                })
                self.assertEqual(datasource["relationships"], [])
                model = _build_semantic_model([datasource], model_mode="import")
                self.assertEqual(model["model"]["relationships"], [])
                generated, = model["model"]["tables"]
                self.assertEqual(generated["name"], "Joined Snapshot")
                expression = generated["partitions"][0]["source"]["expression"]
                # The binding is not evidence that the dummy catalog was exported.
                self.assertIn("Migration.HyperExportRequired", expression)

    def test_shared_merge_refuses_unresolved_source_before_metadata_is_lost(self):
        cases = (("mixed", None), ("if_terms", None),
                 ("composite", None), ("mixed", "right"))
        for kind, nested in cases:
            for unsafe_first in (False, True):
                with self.subTest(kind=kind, nested=nested, unsafe_first=unsafe_first):
                    unsafe = self.source(kind=kind, nested=nested)
                    physical, = unsafe["relation_metadata"]["physical_relations"]
                    if nested:
                        physical = physical["children"][1]
                    self.assertTrue(physical["requires_materialization"])
                    good = self.source(kind="single")
                    good["name"] = "OtherSource"
                    workbooks = [{"datasources": [unsafe]}, {"datasources": [good]}]
                    names = ["UnsafeWorkbook", "OtherWorkbook"]
                    if not unsafe_first:
                        workbooks.reverse()
                        names.reverse()
                    original = deepcopy(workbooks)
                    assessment = MergeAssessment(workbooks=names)
                    with self.assertRaises((ValueError, GenerationError)) as raised:
                        merge_semantic_models(workbooks, assessment, "Shared")
                    message = str(raised.exception)
                    self.assertIn(SOURCE, message)
                    self.assertRegex(message, r"(?i)(join|predicate|relation)")
                    self.assertEqual(workbooks, original)

    def test_shared_merge_keeps_valid_single_equality(self):
        first = self.source(kind="single")
        second = deepcopy(first)
        second["name"] = "OtherSource"
        workbooks = [{"datasources": [first]}, {"datasources": [second]}]
        assessment = MergeAssessment(workbooks=["One", "Two"])
        merged = merge_semantic_models(workbooks, assessment, "Shared")
        datasource, = merged["datasources"]
        self.assertTrue(any(
            _source_pair(rel) == REPORTED_PAIR
            and frozenset((rel["left"]["column"], rel["right"]["column"]))
            == frozenset(("OrderKey", "LookupKey"))
            for rel in datasource["relationships"]
        ), datasource["relationships"])
        generator = PowerBIProjectGenerator(output_dir=str(self.root))
        generator._preprocess_for_fidelity("Shared", merged)
        model = _build_semantic_model(merged["datasources"], model_mode="import")
        relationships = [r for r in model["model"]["relationships"]
                         if _model_pair(r) == REPORTED_PAIR]
        self.assertEqual(len(relationships), 1, relationships)
        self.assertEqual(
            _model_endpoints(relationships[0]),
            frozenset((("Orders", "OrderKey"), ("Lookup", "LookupKey"))),
        )

    def nested_shared_sources(self):
        """Two real declared edges and an attractive but undeclared third pair."""
        xml, _ = _source_xml(kind="single", nested="left")
        # Keep the extractor input and predicates coherent when using the names
        # of the three-table shared-model regression.
        xml = ET.fromstring(
            ET.tostring(xml, encoding="unicode")
            .replace("Lookup", "Returns").replace("Regions", "People"))
        nested = extract_datasource(xml)
        for table in nested["tables"]:
            if table["name"] in ("Returns", "People"):
                table["columns"].append({"name": "BridgeID", "datatype": "integer"})
        other = {
            "name": "Independent",
            "connection": {"type": "SQL Server", "details": {
                "server": "synthetic.example.invalid", "database": "synthetic",
            }},
            "tables": [{
                "name": "Independent",
                "columns": [{"name": "OtherOnly", "datatype": "string"}],
            }],
            "relationships": [],
        }
        sources = [{"datasources": [nested]}, {"datasources": [other]}]
        names = ["Nested", "Independent"]
        assessment = MergeAssessment(workbooks=names)
        merged = merge_semantic_models(sources, assessment, "Shared")
        return sources, names, assessment, merged

    def test_shared_nested_scope_retains_both_edges_but_never_invents_third(self):
        sources, names, assessment, merged = self.nested_shared_sources()
        nested = sources[0]["datasources"][0]
        declared = {_source_pair(rel) for rel in nested["relationships"]}
        self.assertEqual(declared, {
            frozenset(("Orders", "Returns")), frozenset(("Orders", "People")),
        })

        # Prove the negative control is meaningful: without source scope,
        # identical BridgeID fields are sufficiently attractive to infer an edge.
        unprotected = [
            table for table in nested["tables"]
            if table["name"] in ("Returns", "People")
        ]
        candidate, = RelationshipInferenceEngine().infer_relationships(unprotected)
        self.assertEqual(
            frozenset((candidate["fromTable"], candidate["toTable"])),
            frozenset(("Returns", "People")),
        )
        self.assertEqual((candidate["fromColumn"], candidate["toColumn"]),
                         ("BridgeID", "BridgeID"))

        datasource, = merged["datasources"]
        metadata = datasource["relation_metadata"]
        self.assertTrue(metadata["physical_relations"])
        self.assertEqual({_source_pair(rel) for rel in datasource["relationships"]},
                         declared)
        suggestions = suggest_cross_workbook_relationships(merged)
        self.assertFalse(any(
            frozenset((item["from_table"], item["to_table"]))
            == frozenset(("Returns", "People")) for item in suggestions
        ), suggestions)

        generator = PowerBIProjectGenerator(output_dir=str(self.root))
        generator._preprocess_for_fidelity("Shared", merged)
        self.assertEqual({_source_pair(rel) for rel in datasource["relationships"]},
                         declared)
        model = _build_semantic_model(
            merged["datasources"], report_name="Shared", model_mode="import")
        emitted = model["model"]["relationships"]
        self.assertEqual({_model_pair(rel) for rel in emitted}, declared)
        self.assertEqual(len(emitted), 2)
        self.assertEqual(
            {_model_endpoints(rel) for rel in emitted},
            {
                frozenset((("Orders", "OrderKey"), ("Returns", "ReturnsKey"))),
                frozenset((("Orders", "RegionID"), ("People", "RegionID_People"))),
            },
        )

        manifest = build_merge_manifest(
            "Shared", sources, names, None, merged, assessment)
        self.assertIs(manifest.physical_join_provenance_required, True)

    def test_incremental_rebuild_rejects_retained_or_unknown_scopes_before_reading_tmdl(self):
        sources, names, assessment, merged = self.nested_shared_sources()
        project = self.root / "shared"
        generate_tmdl(
            merged["datasources"], "Shared", merged,
            str(project / "Shared.SemanticModel"),
            model_mode="import", parameterize=False)
        manifest = build_merge_manifest(
            "Shared", sources, names, None, merged, assessment)
        self.assertIs(manifest.physical_join_provenance_required, True)
        manifest_path = Path(manifest.save(str(project)))
        self.assertTrue(list(project.rglob("*.tmdl")))

        # TMDL alone cannot recreate protected source scopes.  An absent field
        # in an old manifest (or a malformed non-boolean) is not evidence that
        # the previous source had no such scopes.
        for label, marker in (
            ("retained", True), ("legacy", "absent"),
            ("null", None), ("string-false", "false"),
        ):
            with self.subTest(manifest=label):
                payload = manifest.to_dict()
                if marker == "absent":
                    del payload["physical_join_provenance_required"]
                else:
                    payload["physical_join_provenance_required"] = marker
                manifest_path.write_text(json.dumps(payload), encoding="utf-8")
                loaded = MergeManifest.load(str(project))
                self.assertIs(
                    loaded.physical_join_provenance_required,
                    True if marker is True else None)
                before = {
                    path.relative_to(project).as_posix(): path.read_bytes()
                    for path in project.rglob("*") if path.is_file()
                }
                for operation in ("add", "remove"):
                    with self.subTest(operation=operation):
                        with mock.patch.object(
                                shared_model, "load_existing_model",
                                wraps=shared_model.load_existing_model) as load:
                            with self.assertRaises(PartialGenerationError) as raised:
                                if operation == "add":
                                    add_to_model(
                                        str(project), sources[1], "New", force=True)
                                else:
                                    remove_from_model(str(project), "Nested")
                            load.assert_not_called()
                        error = raised.exception
                        self.assertEqual(error.recovery_kind, "physical_join_predicate")
                        self.assertEqual(
                            error.item, "merge_manifest.physical_join_provenance_required")
                        self.assertEqual(error.source, "Shared")
                        self.assertRegex(str(error), r"(?i)(scope|provenance)")
                        self.assertRegex(str(error), r"(?i)original workbooks")
                        self.assertTrue(error.required_evidence)
                        self.assertEqual(before, {
                            path.relative_to(project).as_posix(): path.read_bytes()
                            for path in project.rglob("*") if path.is_file()
                        })

    def test_incremental_marker_false_does_not_block_scope_free_model(self):
        source = {
            "datasources": [{
                "name": "Independent",
                "connection": {"type": "SQL Server", "details": {
                    "server": "synthetic.example.invalid", "database": "synthetic",
                }},
                "tables": [{
                    "name": "Independent",
                    "columns": [{"name": "OtherOnly", "datatype": "string"}],
                }],
                "relationships": [],
            }],
        }
        names = ["Original"]
        assessment = MergeAssessment(workbooks=names)
        merged = merge_semantic_models([source], assessment, "Plain")
        manifest = build_merge_manifest(
            "Plain", [source], names, None, merged, assessment)
        self.assertIs(manifest.physical_join_provenance_required, False)
        project = self.root / "plain"
        generate_tmdl(
            merged["datasources"], "Plain", merged,
            str(project / "Plain.SemanticModel"),
            model_mode="import", parameterize=False)
        manifest.save(str(project))
        new_source = deepcopy(source)
        new_source["datasources"][0]["tables"][0]["name"] = "Second"
        added = add_to_model(str(project), new_source, "Second", force=True)
        self.assertEqual(added["status"], "added")
        self.assertIs(added["manifest"].physical_join_provenance_required, False)
        removed = remove_from_model(str(project), "Original")
        self.assertEqual(removed["status"], "removed")
        self.assertIn("Independent", removed["removed_tables"])

    def test_shared_merge_accepts_a_bound_snapshot_not_its_native_provenance(self):
        snapshot, _ = self.snapshot_source()
        other = self.source(kind="single")
        workbooks = [{"datasources": [snapshot]}, {"datasources": [other]}]
        assessment = MergeAssessment(workbooks=["Snapshot", "Other"])
        merged = merge_semantic_models(workbooks, assessment, "Shared")
        tables = merged["datasources"][0]["tables"]
        retained = [t for t in tables if t.get("name") == "Joined Snapshot"]
        self.assertEqual(len(retained), 1, tables)
        self.assertEqual(retained[0]["hyper_identity"], snapshot["tables"][0]["hyper_identity"])

    def assert_data_free_partial(self, project, original):
        self.assertTrue((project / "Reported.pbip").is_file())
        partial_path = project / "partial_generation.json"
        self.assertTrue(partial_path.is_file(), "A complete project would hide the lost join")
        diagnostic = json.loads(partial_path.read_text(encoding="utf-8"))
        self.assertEqual(diagnostic["status"], "partial")
        self.assertEqual(diagnostic["validation_status"], "FAIL")
        self.assertIs(diagnostic["data_retained"], False)
        self.assertEqual(diagnostic["recovery_scope"], "data_free_page_outlines")
        error, = diagnostic["errors"]
        self.assertEqual(error["error"], "PartialGenerationError")
        self.assertEqual(error["source"], SOURCE)
        self.assertIn("Orders", error["message"])
        self.assertIn("Lookup", error["message"])

        metadata = json.loads((project / "migration_metadata.json").read_text(
            encoding="utf-8"))
        self.assertEqual(metadata["generation_status"], "partial")
        self.assertEqual(metadata["validation_status"], "FAIL")
        definition = project / "Reported.SemanticModel" / "definition"
        tables = list((definition / "tables").glob("*.tmdl"))
        self.assertEqual([table.name for table in tables], ["MigrationRepair.tmdl"])
        self.assertIn('#table(type table [Status = text], {})',
                      tables[0].read_text(encoding="utf-8"))
        relationships = definition / "relationships.tmdl"
        if relationships.exists():
            self.assertNotRegex(relationships.read_text(encoding="utf-8"),
                                r"(?m)^\s*relationship\s+")
        self.assertFalse((definition / "roles.tmdl").exists())
        for name in ("Data", "DAX", "PowerQuery", "credentials_template.json"):
            self.assertFalse((project / name).exists(), name)

        visuals = list(project.glob("*.Report/definition/pages/*/visuals/*/visual.json"))
        self.assertTrue(visuals)
        for path in visuals:
            visual = json.loads(path.read_text(encoding="utf-8"))["visual"]
            self.assertEqual(visual["visualType"], "textbox")
            self.assertNotIn("query", visual)
        openability = check_openability(str(project))
        self.assertTrue(openability.openable, openability.blocking_issues)
        quality = build_quality_report(original, str(project), "Reported")
        self.assertEqual(quality.status, "FAIL")
        self.assertTrue(any(error["message"] in blocker for blocker in quality.blockers),
                        quality.blockers)
        self.assertTrue(any("partial_generation.json" in priority.get("evidence", [])
                            for priority in quality.priorities), quality.priorities)

    def test_nonstrict_pbip_keeps_model_and_reports_omitted_join(self):
        objects = _project_objects(self.source())
        generator = PowerBIProjectGenerator(output_dir=str(self.root))
        project = Path(generator.generate_project(
            "Reported", deepcopy(objects), model_mode="import",
            parameterize=False, strict_generation=False,
        ))
        self.assertTrue((project / "Reported.pbip").is_file())
        report_dir = project / "Reported.Report"
        self.assertTrue(report_dir.is_dir())
        self.assertFalse((project / "partial_generation.json").exists())

        diagnostics_path = project / "generation_diagnostics.json"
        self.assertTrue(diagnostics_path.is_file())
        diagnostic_file = json.loads(diagnostics_path.read_text(encoding="utf-8"))
        self.assertEqual(diagnostic_file["status"], "partial")
        self.assertTrue(diagnostic_file["data_retained"])
        diagnostic, = diagnostic_file["diagnostics"]
        self.assertEqual(diagnostic["source"], SOURCE)
        self.assertEqual(diagnostic["aliases"], ["Lookup", "Orders"])
        self.assertEqual(diagnostic["omitted_relationship_pairs"], [["Lookup", "Orders"]])
        self.assertEqual(diagnostic["predicate_resolution"], "partially_resolved")
        self.assertEqual(diagnostic["resolved_predicate_equalities"], [{
            "left_table": "Orders", "left_column": "OrderKey",
            "right_table": "Lookup", "right_column": "LookupKey",
        }])

        model_tables = sorted(
            path.stem for path in
            (project / "Reported.SemanticModel" / "definition" / "tables").glob("*.tmdl")
        )
        self.assertEqual(model_tables, ["Lookup", "Orders"])
        relationships_path = (
            project / "Reported.SemanticModel" / "definition" / "relationships.tmdl")
        if relationships_path.exists():
            self.assertNotRegex(relationships_path.read_text(encoding="utf-8"),
                                r"(?m)^relationship\s+")

        metadata = json.loads((project / "migration_metadata.json").read_text(
            encoding="utf-8"))
        self.assertEqual(metadata["generation_diagnostics"],
                         "generation_diagnostics.json")
        self.assertEqual(metadata["relationship_status"], "partial")

        summary_text = ""
        for page_path in (report_dir / "definition" / "pages").glob("*/page.json"):
            page = json.loads(page_path.read_text(encoding="utf-8"))
            if page.get("displayName") not in ("Migration Summary", "Bilan de migration"):
                continue
            page_visuals = page_path.parent / "visuals"
            summary_text = " ".join(
                path.read_text(encoding="utf-8")
                for path in page_visuals.glob("*/visual.json"))
            break
        self.assertIn("Join omitted", summary_text)
        self.assertIn("Predicate partially resolved", summary_text)
        self.assertIn("OrderKey", summary_text)
        self.assertIn("Orders", summary_text)
        self.assertIn("Lookup", summary_text)
        self.assertIn("generation_diagnostics.json", summary_text)
        self.assertTrue((project / "powerbi_lineage.html").is_file())

        openability = check_openability(str(project))
        self.assertTrue(openability.openable, openability.blocking_issues)
        PowerBIProjectGenerator.update_migration_summary_status(project, openability)
        summary_text = " ".join(
            path.read_text(encoding="utf-8")
            for path in (report_dir / "definition" / "pages").glob(
                "*/visuals/*/visual.json"))
        self.assertIn("Join omitted", summary_text)
        self.assertIn("Orders", summary_text)
        self.assertIn("Lookup", summary_text)

    def test_tolerant_composite_join_omits_every_component_edge(self):
        datasource = self.source(kind="composite")
        model = _build_semantic_model(
            [datasource], report_name="Reported", model_mode="import",
            extra_objects={"_tolerate_unrepresentable_physical_joins": True},
        )

        self.assertEqual(model["model"]["relationships"], [])
        diagnostic, = model["_physical_join_diagnostics"]
        self.assertEqual(diagnostic["predicate_resolution"], "fully_resolved")
        self.assertEqual(len(diagnostic["resolved_predicate_equalities"]), 2)
        self.assertEqual(diagnostic["omitted_relationship_pairs"],
                         [["Lookup", "Orders"]])
        self.assertEqual(len(diagnostic["omitted_relationships"]), 2)

    def test_predicate_diagnostics_distinguish_full_partial_and_opaque_and_terms(self):
        cases = (
            ("single", "fully_resolved", 1),
            ("composite", "fully_resolved", 2),
            ("partial_if", "partially_resolved", 1),
            ("opaque_if", "opaque", 0),
        )
        for kind, expected_status, expected_pairs in cases:
            with self.subTest(kind=kind):
                datasource = self.source(kind=kind)
                tree, = datasource["relation_metadata"]["physical_relations"]
                status, pairs = _predicate_resolution(tree["predicate"])
                self.assertEqual(status, expected_status)
                self.assertEqual(len(pairs), expected_pairs)
                if status == "partially_resolved":
                    self.assertEqual(pairs, [(
                        ("Orders", "OrderKey"), ("Lookup", "LookupKey"))])

    def test_zone_menage_join_reproduction_has_correct_row_counts(self):
        zones = [
            {"Secteur": "S-01", "Etat": "actif", "Categorie": "collectif"},
            {"Secteur": "S-02", "Etat": "actif", "Categorie": "social"},
            {"Secteur": "S-03", "Etat": "projet", "Categorie": "collectif"},
            {"Secteur": "S-04", "Etat": "projet", "Categorie": "social"},
            {"Secteur": "S-05", "Etat": "actif", "Categorie": "collectif"},
            {"Secteur": "S-06", "Etat": "projet", "Categorie": "maison"},
        ]
        menages = [
            {"Ref": 9001, "ZoneRef": "S-01", "Mode": "reseau", "ZoneEtat": "actif"},
            {"Ref": 9002, "ZoneRef": "S-01", "Mode": "reseau", "ZoneEtat": "actif"},
            {"Ref": 9003, "ZoneRef": "S-02", "Mode": "reseau", "ZoneEtat": "actif"},
            {"Ref": 9004, "ZoneRef": "S-02", "Mode": "autre", "ZoneEtat": "actif"},
            {"Ref": 9005, "ZoneRef": "S-03", "Mode": "autre", "ZoneEtat": "projet"},
            {"Ref": 9006, "ZoneRef": "S-03", "Mode": "autre", "ZoneEtat": "projet"},
            {"Ref": 9007, "ZoneRef": "S-05", "Mode": "reseau", "ZoneEtat": "actif"},
            {"Ref": 9008, "ZoneRef": "S-05", "Mode": "reseau", "ZoneEtat": "actif"},
            {"Ref": 9009, "ZoneRef": "S-04", "Mode": "autre", "ZoneEtat": "projet"},
            {"Ref": 9010, "ZoneRef": "S-04", "Mode": "autre", "ZoneEtat": "projet"},
        ]

        def exact_join(*, add_state_mode=False, add_matching_state=False):
            matches = []
            for zone in zones:
                for menage in menages:
                    expected_category = (
                        "collectif" if menage["Mode"] == "reseau" else "social")
                    if (zone["Secteur"] != menage["ZoneRef"]
                            or zone["Categorie"] != expected_category):
                        continue
                    if add_state_mode and zone["Etat"] != menage["Mode"]:
                        continue
                    if add_matching_state and zone["Etat"] != menage["ZoneEtat"]:
                        continue
                    matches.append(zone["Secteur"])
            return Counter(matches)

        expected = Counter({"S-01": 2, "S-02": 1, "S-04": 2, "S-05": 2})
        self.assertEqual(exact_join(), expected)
        self.assertEqual(exact_join(add_state_mode=True), Counter())
        self.assertEqual(exact_join(add_matching_state=True), expected)

    def test_existing_partial_repair_contract_when_model_rejects_the_join(self):
        # A separate control exercises the repair contract while the real
        # physical-join guard in the preceding end-to-end test is being fixed.
        objects = _project_objects(self.source())
        generator = PowerBIProjectGenerator(output_dir=str(self.root))
        error = PartialGenerationError(
            "Physical join Orders / Lookup requires the complete predicate",
            source=SOURCE, recovery_kind="physical_join",
            required_evidence=["Complete materialized source join"],
        )
        with mock.patch.object(generator, "create_tmdl_model", side_effect=error):
            project = Path(generator.generate_project(
                "Reported", deepcopy(objects), model_mode="import",
                parameterize=False, strict_generation=False,
            ))
        self.assert_data_free_partial(project, objects)

    def test_strict_pbip_propagates_the_physical_join_error(self):
        generator = PowerBIProjectGenerator(output_dir=str(self.root))
        with self.assertRaises(PartialGenerationError):
            generator.generate_project(
                "Reported", {"datasources": [self.source()]},
                model_mode="import", strict_generation=True,
            )
        self.assertFalse(list(self.root.rglob("*.pbip")))
        self.assertFalse(list(self.root.rglob("partial_generation.json")))
