"""Synthetic regressions for explicit logical-relationship SUM compilation."""

import copy
import unittest
from datetime import datetime, timedelta

from powerbi_import.tmdl_dax_postprocess import compile_logical_relationship_sum
from tests.logical_relationship_fixtures import (
    EmittedDax,
    bag_rows,
    compiler_arguments,
)


class TestEmittedDaxOracle(unittest.TestCase):
    """Guard the limited oracle independently from the compiler under test."""

    def test_iterators_preserve_duplicates_and_empty_sum_is_blank(self):
        rows = {"T": [{"n": 2}, {"n": 2}, {"n": None}]}
        self.assertEqual(EmittedDax("SUMX('T', 'T'[n])").evaluate(rows), 4)
        self.assertEqual(EmittedDax("SUM('T'[n])").evaluate(rows), 4)
        self.assertIsNone(EmittedDax("SUMX(FILTER('T', FALSE()), 'T'[n])")
                          .evaluate(rows))

    def test_var_captures_outer_row_before_inner_scan(self):
        expression = (
            "SUMX('L', VAR captured = 'L'[n] RETURN "
            "SUMX(FILTER('R', 'R'[n] < captured), 1))")
        self.assertEqual(EmittedDax(expression).evaluate(
            {"L": [{"n": 3}, {"n": 2}], "R": [{"n": 1}, {"n": 2}, {"n": 2}]}), 4)

    def test_all_scope_is_distinct_from_visible_rows(self):
        rows = {"T": [{"n": 2}, {"n": 5}]}
        visible = {"T": [{"n": 2}]}
        self.assertEqual(EmittedDax("SUMX('T', 'T'[n])").evaluate(rows, visible), 2)
        self.assertEqual(EmittedDax("SUMX(ALL('T'), 'T'[n])").evaluate(rows, visible), 7)

    def test_binary_plus_preserves_dax_blank_and_numeric_semantics(self):
        for expression, expected in (
            ("BLANK() + BLANK()", None),
            ("0 + BLANK()", 0),
            ("BLANK() + 0", 0),
            ("2 + BLANK()", 2),
            ("BLANK() + 2", 2),
            ("1 + 2 + 3", 6),
            ("1.5 + 2.25", 3.75),
        ):
            with self.subTest(expression=expression):
                actual = EmittedDax(expression).evaluate({})
                if expected is None:
                    self.assertIsNone(actual)
                else:
                    self.assertEqual(actual, expected)

    def test_binary_plus_precedence_parentheses_and_iterator_baseline(self):
        self.assertTrue(EmittedDax("1 + 2 == 3 && 4 == 2 + 2").evaluate({}))
        self.assertEqual(EmittedDax("(1 + 2) + (3 + 4)").evaluate({}), 10)
        expression = "SUMX('T', 0) + SUMX(FILTER('T', FALSE()), 1)"
        self.assertEqual(EmittedDax(expression).evaluate({"T": [{"n": 1}]}), 0)
        self.assertIsNone(EmittedDax(expression).evaluate({"T": []}))

    def test_unknown_functions_and_tokens_do_not_silently_evaluate(self):
        with self.assertRaisesRegex(AssertionError, "Unsupported oracle function"):
            EmittedDax("SELECTEDVALUE(1)").evaluate({})
        with self.assertRaisesRegex(AssertionError, "Unrecognized DAX"):
            EmittedDax("1 * 2")


class TestCompileLogicalRelationshipSum(unittest.TestCase):
    def compile(self, **overrides):
        args = compiler_arguments()
        args.update(overrides)
        result = compile_logical_relationship_sum(**args)
        self.assertEqual(result["status"], "supported", result)
        self.assertEqual(result["home_table"], "Current")
        self.assertEqual(result["grain"], "logical_relationship_pairs")
        self.assertEqual(result["consumed_calculations"],
                         ["anchor", "row_score", "total_score"])
        return result["expression"]

    def test_duplicate_rows_timestamp_ties_and_statuses_preserve_pair_bag(self):
        expression = self.compile()
        rows = bag_rows()
        # Two indistinguishable current rows each see both qualifying prior
        # rows at tick 20. A waiting row at the same tick must contribute zero.
        self.assertEqual(EmittedDax(expression).evaluate(rows), 4)
        one_current = copy.deepcopy(rows)
        del one_current["Current"][0]
        self.assertEqual(EmittedDax(expression).evaluate(one_current), 2)
        one_prior = copy.deepcopy(rows)
        del one_prior["Prior"][1]
        self.assertEqual(EmittedDax(expression).evaluate(one_prior), 2)
        # A scalar lookup/MAX by RowID or timestamp would return one, not four.
        self.assertNotRegex(expression, r"\b(?:LOOKUPVALUE|SELECTEDVALUE|TOPN|"
                            r"DISTINCT|SUMMARIZE|CALCULATE|RELATED|EARLIER)\s*\(")
        self.assertNotIn("[RowID]", expression)
        self.assertIn("RETURN SUMX(__lrLeftRows,", expression)
        self.assertIn("SUMX(__lrPairs,", expression)

    def test_complete_composite_and_strict_range_predicate_are_executed(self):
        expression = self.compile()
        rows = bag_rows()
        # Each of the last three non-null competitors would change the FIXED
        # maximum (and the result) if a key, bucket, or strict > were omitted.
        self.assertEqual(EmittedDax(expression).evaluate(rows), 4)
        for column in ("Entity", "Bucket", "Tick"):
            self.assertIn(f"'Prior'[{column}]", expression)
        self.assertIn("EXACT(", expression)
        self.assertIn(") > ('Prior'[Tick])", expression)
        self.assertIn(
            "CALCULATETABLE('Prior', KEEPFILTERS(TREATAS({__lrLeft1}, "
            "'Prior'[Entity]))", expression)
        self.assertIn(
            "CALCULATETABLE('Prior', REMOVEFILTERS('Prior'), "
            "KEEPFILTERS(TREATAS({__lrGroup1}, 'Prior'[Entity]))", expression)

    def test_datetime_timestamp_ties_do_not_imply_one_candidate(self):
        args = compiler_arguments()
        for key in (("Current", "C_Time"), ("Prior", "P_Time")):
            args["field_types"][key] = "datetime"
        rows = bag_rows()
        for table in rows.values():
            for row in table:
                if row["Tick"] is not None:
                    row["Tick"] = datetime(2030, 1, 1) + timedelta(seconds=row["Tick"])
        self.assertEqual(EmittedDax(self.compile(**args)).evaluate(rows), 4)

    def test_source_context_scope_is_preserved_not_replaced_by_all(self):
        scopes = compiler_arguments()["row_scopes"]
        scopes["fixed_right"] = "FILTER(ALL('Prior'), 'Prior'[Tick] < 20)"
        expression = self.compile(row_scopes=scopes)
        self.assertIn("VAR __lrFixedRight = " + scopes["fixed_right"], expression)
        rows = bag_rows()
        # Source/context filter changes the FIXED max to tick 10. It is not an
        # ordinary visual filter, so replacing this scope by ALL would yield 4.
        # Exclude the nullable row from this particular context-filter fixture.
        rows["Prior"] = [row for row in rows["Prior"] if row["Tick"] is not None]
        self.assertEqual(EmittedDax(expression).evaluate(rows), 3)

    def test_fixed_scopes_ignore_ordinary_filter_but_pair_scope_does_not(self):
        expression = self.compile()
        rows = bag_rows()
        visible = dict(rows, Current=[rows["Current"][2]])
        # Tick-20 current row has a tick-10 candidate, but FIXED evaluated over
        # all current rows returns tick 20. Thus it must not count tick 10.
        self.assertEqual(EmittedDax(expression).evaluate(rows, visible), 0)
        scopes = compiler_arguments()["row_scopes"]
        scopes["fixed_left"] = "'Current'"
        filtered_fixed = self.compile(row_scopes=scopes)
        self.assertEqual(EmittedDax(filtered_fixed).evaluate(rows, visible), 1)
        self.assertIn("VAR __lrLeftRows = 'Current'\n", expression)
        self.assertIn("VAR __lrFixedLeft = ALL('Current')\n", expression)
        self.assertIn("VAR __lrFixedLeft = 'Current'\n", filtered_fixed)

    def test_fixed_right_scope_is_independent_of_visible_prior_rows(self):
        rows = bag_rows()
        visible = dict(rows, Prior=[rows["Prior"][0]])
        self.assertEqual(EmittedDax(self.compile()).evaluate(rows, visible), 0)
        scopes = compiler_arguments()["row_scopes"]
        scopes["fixed_right"] = "'Prior'"
        self.assertEqual(EmittedDax(self.compile(row_scopes=scopes))
                         .evaluate(rows, visible), 3)

    def test_null_keys_never_join_and_unmatched_else_zero_is_not_blank(self):
        rows = bag_rows()
        rows["Current"] = [dict(Entity=None, Bucket="x", Tick=30,
                                RowID=7, Amount=5)]
        rows["Prior"] = [dict(Entity=None, Bucket="x", Tick=20,
                              State="ready", RowID=7)]
        expression = self.compile()
        self.assertEqual(EmittedDax(expression).evaluate(rows), 0)
        self.assertIn("IF(ISEMPTY(__lrPairs),", expression)
        # With exclusion there is no null-extended pair and SUMX is blank.
        self.assertIsNone(EmittedDax(self.compile(unmatched_left="exclude"))
                          .evaluate(rows))

    def test_unmatched_evaluates_row_branch_once_not_always_zero_or_blank(self):
        args = compiler_arguments()
        args["calculations"][1]["formula"] = (
            'IF ISNULL([P_Time]) THEN 9 ELSE '
            'IF [P_Time] = [anchor] THEN 1 ELSE 0 END END')
        rows = bag_rows()
        rows["Current"] = [dict(Entity="unmatched", Bucket="x", Tick=30,
                                RowID=7, Amount=5)] * 2
        self.assertEqual(EmittedDax(self.compile(**args)).evaluate(rows), 18)

    def test_null_tick_or_status_does_not_match_or_raise(self):
        rows = bag_rows()
        rows["Prior"].append(dict(Entity="A", Bucket="x", Tick=29,
                                  State=None, RowID=7))
        rows["Current"].append(dict(Entity="A", Bucket="x", Tick=None,
                                    RowID=7, Amount=0))
        self.assertEqual(EmittedDax(self.compile()).evaluate(rows), 4)

    def test_source_contains_and_exact_status_equality_are_distinct(self):
        rows = bag_rows()
        rows["Prior"].append(dict(Entity="A", Bucket="x", Tick=29,
                                  State="READY", RowID=7))
        # Explicit case-sensitive source policy excludes uppercase READY.
        self.assertEqual(EmittedDax(self.compile()).evaluate(rows), 4)
        rows["Prior"][-1]["State"] = "almost ready"
        # CONTAINS now admits tick 29 to the source FIXED MAX, but the final
        # exact status comparison is false. Do not collapse statuses at ties.
        self.assertEqual(EmittedDax(self.compile()).evaluate(rows), 0)

    def test_case_insensitive_contains_escapes_source_literal_search_wildcards(self):
        args = compiler_arguments()
        args["contains_case_sensitive"] = False
        args["calculations"][0]["formula"] = args["calculations"][0]["formula"].replace(
            '"ready"', '"a*?~"')
        expression = self.compile(**args)
        self.assertIn('SEARCH("a~*~?~~",', expression)
        self.assertNotIn("FIND(", expression)

    def test_generic_sum_of_column_reference_has_no_name_heuristic(self):
        args = compiler_arguments()
        args["calculations"][2].update(
            name="[arbitrary_root]", caption="Any aggregate caption",
            formula="SUM([Pair score])")
        args["root_calculation"] = "arbitrary_root"
        result = compile_logical_relationship_sum(**args)
        self.assertEqual(result["status"], "supported", result)
        self.assertEqual(result["root_calculation"], "arbitrary_root")
        self.assertEqual(EmittedDax(result["expression"]).evaluate(bag_rows()), 4)

    def test_only_source_declared_fixed_max_may_collapse_pairs(self):
        expression = self.compile()
        self.assertEqual(expression.count("MAXX("), 2)
        self.assertIn("MAXX(FILTER(__lrFixedLeft,", expression)
        self.assertIn("RETURN MAXX(\n                FILTER(", expression)
        self.assertIn("CALCULATETABLE('Prior', REMOVEFILTERS('Prior'),", expression)
        self.assertNotRegex(expression, r"\b(?:MAX|MIN|FIRSTNONBLANK|LASTNONBLANK)\(")
        self.assertNotIn("MAXX(__lrPairs", expression)

    def test_coarse_pushed_key_does_not_admit_case_distinct_residual(self):
        rows = bag_rows()
        rows["Current"] = [dict(Entity="A", Bucket="x", Tick=30,
                                RowID=1, Amount=1)]
        case_variant = dict(Entity="a", Bucket="x", Tick=29,
                            State="ready", RowID=2)
        rows["Prior"] = [case_variant]
        args = compiler_arguments()
        # Keep the anchor dependency, but make the coarse-matched residual row
        # observable even though FIXED's own EXACT key test excludes it.
        args["calculations"][1]["formula"] = (
            "IF [P_Time] = [anchor] OR [P_Time] = 29 THEN 1 ELSE 0 END")
        expression = self.compile(**args)
        # TREATAS may coarse-match "a" and "A" under model collation, but the
        # explicit EXACT residual rejects the former for both pair and FIXED
        # candidate selection.
        self.assertEqual(EmittedDax(expression).evaluate(rows), 0)
        self.assertIn("KEEPFILTERS(TREATAS({__lrLeft1}, 'Prior'[Entity]))",
                      expression)
        self.assertIn("EXACT(__lrLeft1, 'Prior'[Entity])", expression)
        # An exact-key candidate still survives alongside that coarse match.
        rows["Prior"].append(
            dict(Entity="A", Bucket="x", Tick=20, State="ready", RowID=3))
        self.assertEqual(EmittedDax(expression).evaluate(rows), 1)

    def test_pushed_key_filter_preserves_subset_nulls_and_duplicate_rows(self):
        rows = bag_rows()
        rows["Current"] = [dict(Entity="A", Bucket="x", Tick=30,
                                RowID=1, Amount=1)]
        duplicate = dict(Entity="A", Bucket="x", Tick=20,
                         State="ready", RowID=2)
        rows["Prior"] = [
            duplicate.copy(), duplicate.copy(),
            dict(Entity=None, Bucket="x", Tick=29, State="ready", RowID=3),
            dict(Entity="other", Bucket="x", Tick=29, State="ready", RowID=4),
        ]
        expression = self.compile()
        # The pushed predicate narrows the row bag to the current key, while
        # null guards and exact residuals remain in place; equal rows survive
        # as two distinct contributors.
        self.assertEqual(EmittedDax(expression).evaluate(rows), 2)
        self.assertIn("NOT(ISBLANK('Prior'[Entity]))", expression)
        self.assertIn("KEEPFILTERS(TREATAS({__lrLeft1}, 'Prior'[Entity]))",
                      expression)

    def test_all_scope_resets_filter_then_reapplies_pushed_key(self):
        args = compiler_arguments()
        args["row_scopes"]["right"] = "ALL('Prior')"
        rows = bag_rows()
        rows["Current"] = [dict(Entity="A", Bucket="x", Tick=30,
                                RowID=1, Amount=1)]
        same_key = dict(Entity="A", Bucket="x", Tick=20,
                        State="ready", RowID=2)
        rows["Prior"] = [
            same_key.copy(), same_key.copy(),
            dict(Entity="other", Bucket="x", Tick=29, State="ready", RowID=3),
        ]
        # The ordinary visible context contains only the unrelated row.
        visible = dict(rows, Prior=[rows["Prior"][2]])
        expression = self.compile(**args)
        self.assertEqual(EmittedDax(expression).evaluate(rows, visible), 2)
        # ALL is reset first, then each current key is reapplied. Without the
        # reset KEEPFILTERS would intersect with visible "other" and go empty;
        # without TREATAS the unrelated row could affect the scoped maximum.
        self.assertIn(
            "CALCULATETABLE('Prior', REMOVEFILTERS('Prior'), "
            "KEEPFILTERS(TREATAS({__lrLeft1}, 'Prior'[Entity]))", expression)
        self.assertIn(
            "CALCULATETABLE('Prior', REMOVEFILTERS('Prior'), "
            "KEEPFILTERS(TREATAS({__lrGroup1}, 'Prior'[Entity]))", expression)

    def test_explicit_zero_tree_prunes_before_pair_and_fixed_work_in_left_scope(self):
        args = compiler_arguments()
        args["calculations"][1]["formula"] = (
            "IF [C_Amount] > 0 THEN "
            "IF [P_Time] = [anchor] THEN 1 ELSE 0 END ELSE 0 END")
        expression = self.compile(**args)
        outer_guard = expression.split("SUMX(__lrLeftRows, 0),", 1)[0]
        self.assertIn("RETURN IF(ISEMPTY(FILTER(__lrLeftRows,", outer_guard)
        self.assertIn("'Current'[Amount]", outer_guard)
        self.assertNotIn("'Prior'", outer_guard)
        self.assertNotIn("VAR __lrFixed", outer_guard)
        self.assertNotIn("VAR __lrPairs", outer_guard)

        rows = bag_rows()
        # No status test in this row body: both ready ties and the waiting
        # tie contribute for each of the two duplicate current rows.
        self.assertEqual(EmittedDax(expression).evaluate(rows), 6)
        rows["Current"].append(dict(Entity="A", Bucket="x", Tick=30,
                                    RowID=8, Amount=0))
        visible = dict(rows, Current=[rows["Current"][-1]])
        # Eligible rows still exist in ALL(Current), but only the explicit
        # pair-left scope decides whether this visual can be pruned.
        self.assertEqual(EmittedDax(expression).evaluate(rows, visible), 0)
        self.assertIsNone(EmittedDax(expression).evaluate(
            rows, dict(rows, Current=[])))

    def test_sparse_nonempty_domains_prune_outer_rows_not_pair_duplicates(self):
        args = compiler_arguments()
        args["calculations"][1]["formula"] = (
            'IF [C_Amount] > 0 THEN IF [P_Time] = [anchor] '
            'AND [P_State] = "ready" THEN 1 ELSE 0 END ELSE 0 END')
        expression = self.compile(**args)
        # The original bag supplies only the zero baseline. The actual outer
        # iterator must filter BEFORE constructing any retained row's pair bag.
        self.assertIn(
            "RETURN SUMX(__lrLeftRows, 0) + SUMX(FILTER(__lrLeftRows,",
            expression)
        filtered_outer = expression.index(
            "SUMX(FILTER(__lrLeftRows,")
        self.assertLess(filtered_outer, expression.index("VAR __lrPairs"))
        self.assertIn("'Current'[Amount]", expression[filtered_outer:
                                                     expression.index("VAR __lrPairs")])
        self.assertIn("SUMX(__lrPairs,", expression)
        rows = bag_rows()
        # These zero-output rows leave BOTH key domains nonempty, so the
        # empty-domain fast path cannot hide a broken filtered outer iterator.
        rows["Current"].extend([
            dict(Entity="A", Bucket="x", Tick=30, RowID=7, Amount=0),
            dict(Entity="A", Bucket="x", Tick=30, RowID=7, Amount=0),
            dict(Entity="unmatched", Bucket="x", Tick=30, RowID=7, Amount=0),
        ])
        self.assertEqual(EmittedDax(expression).evaluate(rows), 4)
        for table, index in (("Current", 0), ("Prior", 1)):
            with self.subTest(removed_duplicate_from=table):
                reduced = copy.deepcopy(rows)
                del reduced[table][index]
                self.assertEqual(EmittedDax(expression).evaluate(reduced), 2)
        # A ready/waiting timestamp tie is not a status-equivalent row.
        waiting_becomes_ready = copy.deepcopy(rows)
        waiting_becomes_ready["Prior"][3]["State"] = "ready"
        self.assertEqual(EmittedDax(expression).evaluate(waiting_becomes_ready), 6)

    def test_pruned_zero_row_still_changes_fixed_max_in_independent_scopes(self):
        args = compiler_arguments()
        args["calculations"][1]["formula"] = (
            'IF [C_Amount] > 0 AND [P_Time] = [anchor] '
            'AND [P_State] = "ready" THEN 1 ELSE 0 END')
        expression = self.compile(**args)
        rows = bag_rows()
        self.assertEqual(EmittedDax(expression).evaluate(rows), 4)
        # This row contributes zero itself, but admits tick 30 into the FIXED
        # group. Retained tick-30 rows cannot pair with that strict-range tie.
        zero_row = dict(Entity="A", Bucket="x", Tick=40, RowID=7, Amount=0)
        rows["Current"].append(zero_row)
        self.assertEqual(EmittedDax(expression).evaluate(rows), 0)
        visible = dict(rows, Current=rows["Current"][:-1])
        self.assertEqual(EmittedDax(expression).evaluate(rows, visible), 0)
        self.assertIn("VAR __lrFixedLeft = ALL('Current')", expression)
        self.assertIn("MAXX(FILTER(__lrFixedLeft,", expression)

        # Only an explicit FIXED scope change may remove the zero row's
        # influence; the pair-left pruning must never do this implicitly.
        args["row_scopes"]["fixed_left"] = "'Current'"
        visible_fixed = self.compile(**args)
        self.assertEqual(EmittedDax(visible_fixed).evaluate(rows, visible), 4)
        args["row_scopes"]["fixed_left"] = (
            "FILTER(ALL('Current'), 'Current'[Amount] > 0)")
        source_fixed = self.compile(**args)
        self.assertEqual(EmittedDax(source_fixed).evaluate(rows), 4)
        # FIXED-right also remains independent of the visible pair-right bag.
        visible = dict(rows, Prior=rows["Prior"][:4])
        self.assertEqual(EmittedDax(expression).evaluate(rows, visible), 0)
        args["row_scopes"]["fixed_left"] = "ALL('Current')"
        args["row_scopes"]["fixed_right"] = "'Prior'"
        self.assertEqual(EmittedDax(self.compile(**args)).evaluate(rows, visible), 4)

    def test_sparse_guard_empty_left_and_all_zero_nonempty_domains(self):
        args = compiler_arguments()
        args["calculations"][1]["formula"] = (
            "IF [C_Amount] > 0 AND [P_Time] = [anchor] THEN 1 ELSE 0 END")
        expression = self.compile(**args)
        rows = bag_rows()
        for row in rows["Current"]:
            row["Amount"] = 0
        self.assertEqual(EmittedDax(expression).evaluate(rows), 0)
        self.assertIsNone(EmittedDax(expression).evaluate(
            dict(rows, Current=[])))
        self.assertIsNone(EmittedDax(expression).evaluate(
            rows, dict(rows, Current=[])))
        # Keep eligible rows and both domains nonempty, but make every full
        # pair output zero. This must also return numeric zero, not BLANK.
        rows = bag_rows()
        for row in rows["Prior"]:
            row["State"] = "waiting"
        self.assertEqual(EmittedDax(expression).evaluate(rows), 0)

    def test_left_guard_distinguishes_empty_string_from_null_key(self):
        args = compiler_arguments()
        args["calculations"][1]["formula"] = (
            'IF [C_Key] = "" AND [P_Time] = [anchor] THEN 1 ELSE 0 END')
        expression = self.compile(**args)
        guard = expression.split("SUMX(__lrLeftRows, 0),", 1)[0]
        self.assertIn('EXACT(\'Current\'[Entity], "")', guard)
        self.assertIn("ISBLANK('Current'[Entity])", guard)
        rows = {
            "Current": [dict(Entity="", Bucket="x", Tick=30, RowID=1, Amount=1)],
            "Prior": [dict(Entity="", Bucket="x", Tick=20,
                           State="ready", RowID=2)] * 2,
        }
        self.assertEqual(EmittedDax(expression).evaluate(rows), 2)
        rows["Current"][0]["Entity"] = None
        self.assertEqual(EmittedDax(expression).evaluate(rows), 0)

    def test_right_dependent_not_is_conservative_inside_left_guard(self):
        args = compiler_arguments()
        args["calculations"][1]["formula"] = (
            'IF [C_Amount] > 0 AND NOT('
            '[P_State] = "blocked" AND [P_Time] = [anchor]) '
            'THEN 1 ELSE 0 END')
        expression = self.compile(**args)
        guard = expression.split("SUMX(__lrLeftRows, 0),", 1)[0]
        self.assertIn("'Current'[Amount]", guard)
        self.assertNotIn("'Prior'", guard)
        self.assertNotIn("__lrLod", guard)
        # Treating an unknown right atom as TRUE and then negating it would
        # prune every row. All nine actual pairs pass this NOT condition.
        self.assertEqual(EmittedDax(expression).evaluate(bag_rows()), 9)
        rows = bag_rows()
        for row in rows["Current"]:
            row["Amount"] = 0
        self.assertEqual(EmittedDax(expression).evaluate(rows), 0)

    def test_exclusion_and_implicit_null_leaves_are_not_zero_hoisted(self):
        for unmatched, tail, expected in (
            ("exclude", " ELSE 0 END", 0),
            ("null_extend", " END", None),
        ):
            with self.subTest(unmatched=unmatched, tail=tail):
                args = compiler_arguments()
                args["unmatched_left"] = unmatched
                args["calculations"][1]["formula"] = (
                    "IF [C_Amount] > 0 AND [P_Time] = [anchor] THEN 1" + tail)
                expression = self.compile(**args)
                self.assertNotIn("SUMX(__lrLeftRows, 0),", expression)
                rows = bag_rows()
                for row in rows["Current"]:
                    row["Amount"] = 0
                self.assertEqual(EmittedDax(expression).evaluate(rows), expected)
                rows["Prior"] = []
                self.assertIsNone(EmittedDax(expression).evaluate(rows))

    def test_absent_or_ambiguous_provenance_and_policy_fail_closed(self):
        base = compiler_arguments()
        variants = []
        for key, value in (
            ("row_scopes", {}), ("unmatched_left", None),
            ("contains_case_sensitive", None), ("comparison_case_sensitive", None),
            ("field_types", {}), ("reference_bindings", {}),
        ):
            args = copy.deepcopy(base)
            args[key] = value
            variants.append((key, args))
        for label in ("missing_endpoint", "opaque_predicate", "ambiguous_relationship",
                      "physical_join"):
            args = copy.deepcopy(base)
            rel = args["relation_metadata"]["logical_relationships"][0]
            if label == "missing_endpoint":
                rel["left"]["object_id"] = "unknown"
            elif label == "opaque_predicate":
                rel["predicate"] = {"operator": "OR", "arguments": []}
            elif label == "ambiguous_relationship":
                args["relation_metadata"]["logical_relationships"].append(copy.deepcopy(rel))
            else:
                rel["join_type"] = "left"
            variants.append((label, args))
        for label, args in variants:
            with self.subTest(label=label):
                result = compile_logical_relationship_sum(**args)
                self.assertEqual(result["status"], "unsupported")
                self.assertTrue(result["reason"])
                self.assertNotIn("expression", result)

    def test_cyclic_and_arbitrary_aggregate_intermediates_fail_closed(self):
        for formula in ("[row_score]", "MAX([P_Time])",
                        '{ FIXED [P_Key] : MAX([P_Time]) }'):
            with self.subTest(formula=formula):
                args = compiler_arguments()
                args["calculations"][1]["formula"] = formula
                result = compile_logical_relationship_sum(**args)
                self.assertEqual(result["status"], "unsupported")
                self.assertNotIn("expression", result)
