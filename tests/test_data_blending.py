"""Tests for the data blending graph builder — Sprint 180 (v39.0.0)."""

import unittest

from tableau_export.blend_graph import (
    build_blend_graph,
    blend_graph_to_relationships,
    blend_graph_to_merge_queries,
    blend_graph_dax_hint,
    assess_blend_graph,
    _infer_cardinality,
    _link_field,
    _grade_blend,
    VIRTUAL_SECONDARIES,
)


def _link(primary, secondary, column="", key="", expr=""):
    return {
        "datasource": primary,
        "secondary_datasource": secondary,
        "column": column,
        "link_key": key,
        "link_expression": expr,
    }


class TestBuildBlendGraph(unittest.TestCase):
    def test_empty_input_returns_empty(self):
        self.assertEqual(build_blend_graph([]), [])
        self.assertEqual(build_blend_graph(None), [])

    def test_single_blend_two_sources(self):
        data = [_link("Orders", "Returns", column="Order ID")]
        graph = build_blend_graph(data)
        self.assertEqual(len(graph), 1)
        self.assertEqual(graph[0]["primary"], "Orders")
        self.assertEqual(len(graph[0]["secondaries"]), 1)
        self.assertEqual(graph[0]["secondaries"][0]["datasource"], "Returns")

    def test_link_field_extracted(self):
        data = [_link("Orders", "Returns", column="Order ID")]
        graph = build_blend_graph(data)
        lfs = graph[0]["secondaries"][0]["link_fields"]
        self.assertEqual(lfs, [{"primary": "Order ID", "secondary": "Order ID"}])

    def test_virtual_parameters_secondary_skipped(self):
        data = [_link("Orders", "Parameters", column="P1")]
        graph = build_blend_graph(data)
        self.assertEqual(graph, [])

    def test_virtual_secondary_case_insensitive(self):
        data = [_link("Orders", "parameters", column="P1")]
        self.assertEqual(build_blend_graph(data), [])
        data2 = [_link("Orders", "PARAMETERS", column="P1")]
        self.assertEqual(build_blend_graph(data2), [])

    def test_skip_virtual_disabled(self):
        data = [_link("Orders", "Parameters", column="P1")]
        graph = build_blend_graph(data, skip_virtual=False)
        self.assertEqual(len(graph), 1)

    def test_missing_primary_skipped(self):
        data = [_link("", "Returns", column="X")]
        self.assertEqual(build_blend_graph(data), [])

    def test_missing_secondary_skipped(self):
        data = [_link("Orders", "", column="X")]
        self.assertEqual(build_blend_graph(data), [])

    def test_self_blend_skipped(self):
        data = [_link("Orders", "Orders", column="X")]
        self.assertEqual(build_blend_graph(data), [])

    def test_multiple_secondaries_same_primary(self):
        data = [
            _link("Orders", "Returns", column="Order ID"),
            _link("Orders", "Targets", column="Region"),
        ]
        graph = build_blend_graph(data)
        self.assertEqual(len(graph), 1)
        names = {s["datasource"] for s in graph[0]["secondaries"]}
        self.assertEqual(names, {"Returns", "Targets"})

    def test_multiple_primaries(self):
        data = [
            _link("Orders", "Returns", column="Order ID"),
            _link("People", "Regions", column="Region"),
        ]
        graph = build_blend_graph(data)
        self.assertEqual(len(graph), 2)
        primaries = {b["primary"] for b in graph}
        self.assertEqual(primaries, {"Orders", "People"})

    def test_duplicate_link_fields_deduped(self):
        data = [
            _link("Orders", "Returns", column="Order ID"),
            _link("Orders", "Returns", column="Order ID"),
        ]
        graph = build_blend_graph(data)
        lfs = graph[0]["secondaries"][0]["link_fields"]
        self.assertEqual(len(lfs), 1)

    def test_multiple_link_fields(self):
        data = [
            _link("Orders", "Returns", column="Order ID"),
            _link("Orders", "Returns", column="Region"),
        ]
        graph = build_blend_graph(data)
        lfs = graph[0]["secondaries"][0]["link_fields"]
        self.assertEqual(len(lfs), 2)

    def test_link_key_overrides_secondary(self):
        data = [_link("Orders", "Returns", column="OrderID", key="OID")]
        graph = build_blend_graph(data)
        lf = graph[0]["secondaries"][0]["link_fields"][0]
        self.assertEqual(lf["primary"], "OrderID")
        self.assertEqual(lf["secondary"], "OID")

    def test_brackets_stripped(self):
        data = [_link("Orders", "Returns", column="[Order ID]")]
        graph = build_blend_graph(data)
        lf = graph[0]["secondaries"][0]["link_fields"][0]
        self.assertEqual(lf["primary"], "Order ID")

    def test_direction_is_secondary(self):
        data = [_link("Orders", "Returns", column="Order ID")]
        graph = build_blend_graph(data)
        self.assertEqual(graph[0]["secondaries"][0]["direction"], "secondary")

    def test_cross_filter_one_direction(self):
        data = [_link("Orders", "Returns", column="Order ID")]
        graph = build_blend_graph(data)
        self.assertEqual(graph[0]["secondaries"][0]["cross_filter"], "oneDirection")


class TestGrading(unittest.TestCase):
    def test_single_secondary_green(self):
        data = [_link("Orders", "Returns", column="Order ID")]
        graph = build_blend_graph(data)
        self.assertEqual(graph[0]["grade"], "GREEN")

    def test_two_secondaries_yellow(self):
        data = [
            _link("Orders", "Returns", column="A"),
            _link("Orders", "Targets", column="B"),
        ]
        graph = build_blend_graph(data)
        self.assertEqual(graph[0]["grade"], "YELLOW")

    def test_circular_blend_red(self):
        data = [
            _link("Orders", "Returns", column="A"),
            _link("Returns", "Orders", column="A"),
        ]
        graph = build_blend_graph(data)
        for b in graph:
            self.assertEqual(b["grade"], "RED")

    def test_grade_blend_helper(self):
        self.assertEqual(_grade_blend([], is_circular=False), "GREEN")
        self.assertEqual(_grade_blend([{}], is_circular=False), "GREEN")
        self.assertEqual(_grade_blend([{}, {}], is_circular=False), "YELLOW")
        self.assertEqual(_grade_blend([{}], is_circular=True), "RED")


class TestCardinality(unittest.TestCase):
    def test_default_many_to_one(self):
        self.assertEqual(_infer_cardinality("A", "B", {}), "manyToOne")

    def test_wide_secondary_many_to_many(self):
        counts = {"A": 10, "B": 10}
        self.assertEqual(_infer_cardinality("A", "B", counts), "manyToMany")

    def test_narrow_secondary_many_to_one(self):
        counts = {"A": 10, "B": 2}
        self.assertEqual(_infer_cardinality("A", "B", counts), "manyToOne")

    def test_cardinality_with_datasources(self):
        data = [_link("Orders", "Returns", column="Order ID")]
        datasources = [
            {"caption": "Orders", "columns": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]},
            {"caption": "Returns", "columns": [1, 2]},
        ]
        graph = build_blend_graph(data, datasources)
        self.assertEqual(graph[0]["secondaries"][0]["cardinality"], "manyToOne")

    def test_cardinality_peer_tables(self):
        data = [_link("Orders", "Returns", column="Order ID")]
        datasources = [
            {"caption": "Orders", "columns": [1, 2, 3, 4]},
            {"caption": "Returns", "columns": [1, 2, 3, 4]},
        ]
        graph = build_blend_graph(data, datasources)
        self.assertEqual(graph[0]["secondaries"][0]["cardinality"], "manyToMany")

    def test_columns_from_tables(self):
        data = [_link("Orders", "Returns", column="Order ID")]
        datasources = [
            {"caption": "Orders", "tables": [{"columns": [1, 2, 3, 4, 5, 6, 7, 8]}]},
            {"caption": "Returns", "tables": [{"columns": [1]}]},
        ]
        graph = build_blend_graph(data, datasources)
        self.assertEqual(graph[0]["secondaries"][0]["cardinality"], "manyToOne")


class TestLinkField(unittest.TestCase):
    def test_column_only(self):
        pf, sf = _link_field({"column": "Order ID"})
        self.assertEqual((pf, sf), ("Order ID", "Order ID"))

    def test_key_override(self):
        pf, sf = _link_field({"column": "Order ID", "link_key": "OID"})
        self.assertEqual(pf, "Order ID")
        self.assertEqual(sf, "OID")

    def test_expression_fallback(self):
        pf, sf = _link_field({"column": "OID", "link_expression": "EXPR"})
        self.assertEqual(pf, "OID")
        self.assertEqual(sf, "EXPR")

    def test_empty(self):
        pf, sf = _link_field({})
        self.assertEqual((pf, sf), ("", ""))


class TestRelationships(unittest.TestCase):
    def test_basic_relationship(self):
        data = [_link("Orders", "Returns", column="Order ID")]
        graph = build_blend_graph(data)
        rels = blend_graph_to_relationships(graph)
        self.assertEqual(len(rels), 1)
        r = rels[0]
        self.assertEqual(r["fromTable"], "Orders")
        self.assertEqual(r["fromColumn"], "Order ID")
        self.assertEqual(r["toTable"], "Returns")
        self.assertEqual(r["toColumn"], "Order ID")
        self.assertEqual(r["crossFilteringBehavior"], "oneDirection")
        self.assertTrue(r["isBlend"])

    def test_relationship_dedup(self):
        data = [
            _link("Orders", "Returns", column="Order ID"),
            _link("Orders", "Returns", column="Order ID"),
        ]
        graph = build_blend_graph(data)
        rels = blend_graph_to_relationships(graph)
        self.assertEqual(len(rels), 1)

    def test_relationship_cardinality_propagates(self):
        data = [_link("Orders", "Returns", column="Order ID")]
        datasources = [
            {"caption": "Orders", "columns": [1, 2, 3, 4]},
            {"caption": "Returns", "columns": [1, 2, 3, 4]},
        ]
        graph = build_blend_graph(data, datasources)
        rels = blend_graph_to_relationships(graph)
        self.assertEqual(rels[0]["cardinality"], "manyToMany")

    def test_empty_graph_no_relationships(self):
        self.assertEqual(blend_graph_to_relationships([]), [])

    def test_multiple_secondaries_multiple_relationships(self):
        data = [
            _link("Orders", "Returns", column="A"),
            _link("Orders", "Targets", column="B"),
        ]
        graph = build_blend_graph(data)
        rels = blend_graph_to_relationships(graph)
        self.assertEqual(len(rels), 2)


class TestMergeQueries(unittest.TestCase):
    def test_basic_merge_query(self):
        data = [_link("Orders", "Returns", column="Order ID")]
        graph = build_blend_graph(data)
        queries = blend_graph_to_merge_queries(graph)
        self.assertIn("Orders", queries)
        m = queries["Orders"]
        self.assertIn("Table.NestedJoin", m)
        self.assertIn("Returns", m)

    def test_many_to_one_left_join(self):
        data = [_link("Orders", "Returns", column="Order ID")]
        datasources = [
            {"caption": "Orders", "columns": list(range(10))},
            {"caption": "Returns", "columns": [1]},
        ]
        graph = build_blend_graph(data, datasources)
        queries = blend_graph_to_merge_queries(graph)
        self.assertIn("JoinKind.LeftOuter", queries["Orders"])

    def test_many_to_many_full_join(self):
        data = [_link("Orders", "Returns", column="Order ID")]
        datasources = [
            {"caption": "Orders", "columns": [1, 2, 3, 4]},
            {"caption": "Returns", "columns": [1, 2, 3, 4]},
        ]
        graph = build_blend_graph(data, datasources)
        queries = blend_graph_to_merge_queries(graph)
        self.assertIn("JoinKind.FullOuter", queries["Orders"])

    def test_multiple_secondaries_chained(self):
        data = [
            _link("Orders", "Returns", column="A"),
            _link("Orders", "Targets", column="B"),
        ]
        graph = build_blend_graph(data)
        queries = blend_graph_to_merge_queries(graph)
        # The last merge in the chain references the previous blend step.
        self.assertIn("Orders_Blend1", queries["Orders"])

    def test_empty_graph_no_queries(self):
        self.assertEqual(blend_graph_to_merge_queries([]), {})


class TestDaxHint(unittest.TestCase):
    def test_many_to_one_related(self):
        self.assertEqual(blend_graph_dax_hint("manyToOne"), "RELATED")

    def test_many_to_many_lookupvalue(self):
        self.assertEqual(blend_graph_dax_hint("manyToMany"), "LOOKUPVALUE")


class TestAssessBlendGraph(unittest.TestCase):
    def test_empty_graph_green(self):
        summary = assess_blend_graph([])
        self.assertEqual(summary["grade"], "GREEN")
        self.assertEqual(summary["primary_count"], 0)

    def test_counts(self):
        data = [
            _link("Orders", "Returns", column="A"),
            _link("Orders", "Targets", column="B"),
        ]
        graph = build_blend_graph(data)
        summary = assess_blend_graph(graph)
        self.assertEqual(summary["primary_count"], 1)
        self.assertEqual(summary["secondary_count"], 2)
        self.assertEqual(summary["link_field_count"], 2)

    def test_overall_grade_worst(self):
        data = [
            _link("Orders", "Returns", column="A"),
            _link("Returns", "Orders", column="A"),
        ]
        graph = build_blend_graph(data)
        summary = assess_blend_graph(graph)
        self.assertEqual(summary["grade"], "RED")

    def test_yellow_when_fanout(self):
        data = [
            _link("Orders", "Returns", column="A"),
            _link("Orders", "Targets", column="B"),
        ]
        graph = build_blend_graph(data)
        summary = assess_blend_graph(graph)
        self.assertEqual(summary["grade"], "YELLOW")

    def test_missing_link_key_count(self):
        # A blend entry whose column resolves to empty produces no link field.
        data = [_link("Orders", "Returns", column="")]
        graph = build_blend_graph(data)
        summary = assess_blend_graph(graph)
        self.assertEqual(summary["missing_link_key_count"], 1)


class TestVirtualSecondariesConstant(unittest.TestCase):
    def test_parameters_in_set(self):
        self.assertIn("parameters", VIRTUAL_SECONDARIES)
        self.assertIn("", VIRTUAL_SECONDARIES)


if __name__ == "__main__":
    unittest.main()
