"""Synthetic regressions for visual-wide native query reference names."""

import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from powerbi_import.pbip_generator import _write_json
from powerbi_import.visual_generator import (
    AmbiguousNativeQueryRefError,
    create_visual_container,
)


class TestNativeQueryRefFinalization(unittest.TestCase):
    @staticmethod
    def projection(native_ref, query_ref, function):
        column = {
            "Column": {
                "Expression": {"SourceRef": {"Entity": "Synthetic"}},
                "Property": "Metric",
            }
        }
        field = (column if function is None else {
            "Aggregation": {"Expression": column, "Function": function}
        })
        return {
            "field": field,
            "queryRef": query_ref,
            "nativeQueryRef": native_ref,
            "active": True,
        }

    def write_and_read(self, state):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "visual.json")
            source = {"visual": {"query": {"queryState": state}}}
            _write_json(path, source)
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)["visual"]["query"]["queryState"]

    def test_distinct_aggregations_get_unique_aliases_without_changing_fields(self):
        first = self.projection("Metric", "Average(Synthetic.Metric)", 1)
        second = self.projection("Metric", "Synthetic.Metric", 0)
        result = self.write_and_read({
            "Values": {"projections": [first, second]},
        })
        output = result["Values"]["projections"]
        self.assertEqual(len({item["nativeQueryRef"] for item in output}), 2)
        self.assertEqual(
            [item["queryRef"] for item in output],
            ["Average(Synthetic.Metric)", "Synthetic.Metric"])
        self.assertEqual([item["field"] for item in output],
                         [first["field"], second["field"]])
        self.assertEqual(output[0]["nativeQueryRef"], "Metric")
        self.assertEqual(output[1]["nativeQueryRef"], "Metric__2")

    def test_repeated_unaggregated_selection_is_not_dropped(self):
        projection = self.projection("Metric", "Synthetic.Metric", None)
        result = self.write_and_read({
            "Values": {"projections": [projection, projection]},
        })
        output = result["Values"]["projections"]
        self.assertEqual(len(output), 2)
        self.assertEqual([item["field"] for item in output],
                         [projection["field"], projection["field"]])
        self.assertEqual(
            [item["nativeQueryRef"] for item in output],
            ["Metric", "Metric__2"])

    def test_native_alias_reserves_existing_suffix_and_is_idempotent(self):
        state = {
            "Values": {"projections": [
                self.projection("Metric", "Synthetic.Metric", None),
                self.projection("Metric", "Sum(Synthetic.Metric)", 0),
                self.projection("Metric__2", "Synthetic.Metric__2", None),
            ]},
        }
        first = self.write_and_read(state)
        second = self.write_and_read(first)
        aliases = [item["nativeQueryRef"]
                   for item in first["Values"]["projections"]]
        self.assertEqual(len(aliases), len(set(aliases)))
        self.assertEqual(aliases, ["Metric", "Metric__3", "Metric__2"])
        self.assertEqual(second, first)

    def test_repeated_reference_in_different_roles_is_unique(self):
        projection = self.projection("Metric", "Synthetic.Metric", None)
        result = self.write_and_read({
            "Category": {"projections": [projection]},
            "Y2": {"projections": [projection]},
        })
        aliases = [
            item["nativeQueryRef"]
            for role in result.values()
            for item in role["projections"]
        ]
        self.assertEqual(aliases, ["Metric", "Metric__2"])


class TestAmbiguousNativeQueryRefGuards(unittest.TestCase):
    """Refuse uncertain links before alias mutation or visual.json publication."""

    @staticmethod
    def projections():
        return [
            TestNativeQueryRefFinalization.projection(
                "Metric", "Avg(Synthetic.Metric)", 1),
            TestNativeQueryRefFinalization.projection(
                "Metric", "Sum(Synthetic.Metric)", 0),
        ]

    def payload(self):
        return {"visual": {
            "visualType": "tableEx",
            "query": {"queryState": {
                "Values": {"projections": self.projections()},
            }},
        }}

    def assert_rejected_before_publication(self, original, location):
        # Check both creation and replacement; otherwise refusing too late could
        # silently truncate a previously valid visual.json.
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "visual-dir" / "visual.json"
                sentinel = b'{ "preserve": "previous visual" }\r\n'
                if existing:
                    path.parent.mkdir()
                    path.write_bytes(sentinel)
                candidate = deepcopy(original)
                before = deepcopy(candidate)
                projections = candidate["visual"]["query"]["queryState"]["Values"]["projections"]
                original_objects = list(projections)
                with self.assertRaises(AmbiguousNativeQueryRefError) as raised:
                    _write_json(str(path), candidate)
                diagnostic = str(raised.exception)
                self.assertIn(location, diagnostic)
                self.assertIn("Metric", diagnostic)
                self.assertRegex(diagnostic, r"(?i)(alias|ambiguous|unambiguous)")
                self.assertEqual(candidate, before)
                for actual, original_object in zip(projections, original_objects):
                    self.assertIs(actual, original_object)
                if existing:
                    self.assertEqual(path.read_bytes(), sentinel)
                else:
                    self.assertFalse(path.parent.exists())

    def worksheet(self, *, extra_field=False):
        measures = [
            {"name": "Metric", "label": "Metric", "expression": "AVG(Metric)"},
            {"name": "Metric", "label": "Metric", "expression": "SUM(Metric)"},
        ]
        if extra_field:
            measures.append({
                "name": "Metric__2", "label": "Metric__2",
                "expression": "SUM(Metric__2)",
            })
        return {"name": "Synthetic", "visualType": "tableEx", "measures": measures}

    def create(self, worksheet):
        return create_visual_container(
            worksheet, visual_id="synthetic-native-ref",
            col_table_map={"Metric": "Synthetic", "Metric__2": "Synthetic"},
        )

    def serialize(self, payload):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "visual.json"
            _write_json(str(path), payload)
            first = json.loads(path.read_text(encoding="utf-8"))
            _write_json(str(path), first)
            second = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(first, second)
            return first

    def test_ambiguous_format_selectors_fail_before_alias_or_file_mutation(self):
        for container, key, reference in (
            ("objects", "metadata", "Metric"),
            ("visualContainerObjects", "metadata", "Metric"),
            # This suffix is planned for the second occurrence; it was not
            # authored as a source field or as a semantic queryRef.
            ("objects", "metadata", "Metric__2"),
            ("objects", "nativeQueryRef", "Metric"),
        ):
            with self.subTest(container=container, key=key, reference=reference):
                payload = self.payload()
                payload["visual"][container] = {
                    "columnFormatting": [{
                        "selector": {key: reference},
                        "properties": {"formatString": {
                            "expr": {"Literal": {"Value": "'0.0%'"}},
                        }},
                    }],
                }
                self.assert_rejected_before_publication(payload, "selector")

    def test_ambiguous_sort_refs_fail_before_alias_or_file_mutation(self):
        for sort in (
            {"field": "Metric", "direction": "Ascending"},
            {"queryRef": "Metric__2", "direction": "Descending"},
            {"nativeQueryRef": "Metric", "direction": "Ascending"},
        ):
            with self.subTest(sort=sort):
                payload = self.payload()
                payload["visual"]["query"]["sortDefinition"] = {"sort": [sort]}
                self.assert_rejected_before_publication(payload, "sortDefinition")

    def test_queryref_which_also_names_another_alias_is_ambiguous(self):
        payload = self.payload()
        projections = payload["visual"]["query"]["queryState"]["Values"]["projections"]
        projections[1]["queryRef"] = "Metric"
        projections[1]["nativeQueryRef"] = "Other"
        payload["visual"]["objects"] = {
            "columnFormatting": [{"selector": {"metadata": "Metric"}}],
        }
        self.assert_rejected_before_publication(payload, "selector")

    def test_same_queryref_for_different_field_expressions_is_ambiguous(self):
        payload = self.payload()
        projections = payload["visual"]["query"]["queryState"]["Values"]["projections"]
        projections[0]["queryRef"] = projections[1]["queryRef"] = "Synthetic.Metric"
        payload["visual"]["objects"] = {
            "columnFormatting": [{"selector": {"metadata": "Synthetic.Metric"}}],
        }
        self.assert_rejected_before_publication(payload, "selector")

    def test_ambiguous_data_bar_is_refused_without_mutating_the_source(self):
        worksheet = self.worksheet()
        worksheet["dataBars"] = [{"field": "Metric"}]
        before = deepcopy(worksheet)
        with self.assertRaises(AmbiguousNativeQueryRefError) as raised:
            self.create(worksheet)
        self.assertIn("Data bar", str(raised.exception))
        self.assertIn("Metric", str(raised.exception))
        self.assertRegex(str(raised.exception), r"(?i)(multiple|ambiguous)")
        self.assertEqual(worksheet, before)

    def test_generated_suffix_alone_cannot_bind_a_data_bar(self):
        worksheet = self.worksheet()
        worksheet["dataBars"] = [{"field": "Metric__2"}]
        before = deepcopy(worksheet)
        with self.assertRaises(AmbiguousNativeQueryRefError) as raised:
            self.create(worksheet)
        self.assertIn("Data bar", str(raised.exception))
        self.assertIn("Metric__2", str(raised.exception))
        self.assertIn("alias", str(raised.exception).lower())
        self.assertEqual(worksheet, before)

    def test_explicit_queryref_data_bar_keeps_both_projection_identities(self):
        worksheet = self.worksheet()
        baseline = self.create(deepcopy(worksheet))["visual"]["query"]["queryState"]
        worksheet["dataBars"] = [{"queryRef": "Avg(Synthetic.Metric)"}]
        payload = self.serialize(self.create(worksheet))
        visual = payload["visual"]
        state = visual["query"]["queryState"]
        self.assertEqual(state, baseline)
        projections = state["Values"]["projections"]
        self.assertEqual([item["queryRef"] for item in projections],
                         ["Avg(Synthetic.Metric)", "Sum(Synthetic.Metric)"])
        self.assertEqual([item["nativeQueryRef"] for item in projections],
                         ["Metric", "Metric__2"])
        self.assertEqual(
            [item["field"]["Aggregation"]["Function"] for item in projections],
            [1, 0],
        )
        bars = visual["objects"]["columnFormatting"]
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["selector"],
                         {"metadata": "Avg(Synthetic.Metric)"})
        self.assertIn("dataBars", bars[0]["properties"])

    def test_semantic_format_and_sort_survive_alias_finalization(self):
        payload = self.payload()
        query = payload["visual"]["query"]
        projections = query["queryState"]["Values"]["projections"]
        before_fields = [deepcopy(item["field"]) for item in projections]
        before_refs = [item["queryRef"] for item in projections]
        sort = {"sort": [{"field": deepcopy(projections[1]["field"]),
                          "direction": "Descending"}]}
        query["sortDefinition"] = deepcopy(sort)
        formatting = [{"selector": {"metadata": before_refs[0]},
                       "properties": {"formatString": {
                           "expr": {"Literal": {"Value": "'0.0%'"}},
                       }}}]
        payload["visual"]["objects"] = {"columnFormatting": deepcopy(formatting)}
        visual = self.serialize(payload)["visual"]
        output = visual["query"]["queryState"]["Values"]["projections"]
        self.assertEqual([item["field"] for item in output], before_fields)
        self.assertEqual([item["queryRef"] for item in output], before_refs)
        self.assertEqual([item["nativeQueryRef"] for item in output],
                         ["Metric", "Metric__2"])
        self.assertEqual(visual["query"]["sortDefinition"], sort)
        self.assertEqual(visual["objects"]["columnFormatting"], formatting)

    def test_literal_suffix_is_a_real_field_not_an_invented_alias(self):
        worksheet = self.worksheet(extra_field=True)
        worksheet["dataBars"] = [{"field": "Metric__2"}]
        payload = self.create(worksheet)
        # A PBIR literal is a value, not a binding merely because its text
        # resembles a native alias.
        literal = {"expr": {"Literal": {"Value": "'Metric__2'"}}}
        payload["visual"].setdefault("objects", {})["general"] = [{
            "properties": {"formatString": deepcopy(literal)},
        }]
        visual = self.serialize(payload)["visual"]
        projections = visual["query"]["queryState"]["Values"]["projections"]
        self.assertEqual([item["nativeQueryRef"] for item in projections],
                         ["Metric", "Metric__3", "Metric__2"])
        self.assertEqual(projections[2]["queryRef"], "Sum(Synthetic.Metric__2)")
        self.assertEqual(
            projections[2]["field"]["Aggregation"]["Expression"]["Column"]["Property"],
            "Metric__2",
        )
        bars = visual["objects"]["columnFormatting"]
        self.assertEqual(bars[0]["selector"],
                         {"metadata": "Sum(Synthetic.Metric__2)"})
        self.assertEqual(visual["objects"]["general"][0]["properties"]["formatString"],
                         literal)
