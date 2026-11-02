"""Tests for redacted semantic executor evidence."""

import json
import unittest

from powerbi_import.semantic_runtime import (
    describe_executor,
    validate_semantic_execution,
)


class _Executor:
    provider = "Fabric"
    version = "2026.09"
    endpoint = "https://fabric.example.test"
    metadata = {
        "capabilities": ["dax", "rows"],
        "token": "do-not-export",
    }

    def execute(self, query):
        return {"rows": [{"Value": 1}]}


class _RecordingExecutor:
    """Inert local executor for exercising the runtime evidence boundary."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def execute(self, query):
        self.calls.append(query)
        if callable(self.response):
            return self.response(query)
        return self.response


class TestSemanticRuntimeEvidence(unittest.TestCase):
    def test_missing_executor_is_explicitly_not_run(self):
        evidence = validate_semantic_execution([], None)

        self.assertEqual(evidence["status"], "not_run")
        self.assertEqual(evidence["executor"]["status"], "not_run")

    def test_executor_metadata_is_redacted_and_attached(self):
        executor = _Executor()

        descriptor = describe_executor(executor)
        evidence = validate_semantic_execution(
            [{"name": "smoke", "dax": "EVALUATE ROW(\"Value\", 1)"}], executor
        )

        self.assertEqual(descriptor["provider"], "Fabric")
        self.assertEqual(descriptor["capabilities"], ["dax", "rows"])
        self.assertEqual(descriptor["endpoint_present"], True)
        self.assertEqual(evidence["executor"]["version"], "2026.09")
        self.assertNotIn("do-not-export", str(evidence))
        self.assertEqual(evidence["status"], "passed")


class TestSemanticRuntimeRegressions(unittest.TestCase):
    @staticmethod
    def _query(name="smoke", **values):
        escaped_name = str(name).replace('"', '""')
        query = {"name": name, "dax": f'EVALUATE ROW("Name", "{escaped_name}")'}
        query.update(values)
        return query

    @staticmethod
    def _values_for_key(value, target):
        if isinstance(value, dict):
            found = [value[target]] if target in value else []
            for child in value.values():
                found.extend(TestSemanticRuntimeRegressions._values_for_key(child, target))
            return found
        if isinstance(value, list):
            found = []
            for child in value:
                found.extend(TestSemanticRuntimeRegressions._values_for_key(child, target))
            return found
        return []

    def test_empty_suite_missing_executor_and_zero_budget_are_not_run(self):
        configured_empty = _RecordingExecutor({"rows": [{"Value": 1}]})
        zero_budget = _RecordingExecutor({"rows": [{"Value": 1}]})
        cases = (
            ("empty suite", [], configured_empty, {}, 0, configured_empty),
            (
                "missing executor",
                [self._query("first"), self._query("second")],
                None,
                {},
                2,
                None,
            ),
            (
                "zero query budget",
                [self._query()],
                zero_budget,
                {"max_queries": 0},
                1,
                zero_budget,
            ),
        )

        for label, queries, executor, options, requested, recorder in cases:
            evidence = validate_semantic_execution(queries, executor, **options)
            with self.subTest(case=label, check="original request count"):
                self.assertEqual(evidence["queries_requested"], requested)
            with self.subTest(case=label, check="run count"):
                self.assertEqual(evidence["queries_run"], 0)
            with self.subTest(case=label, check="status"):
                self.assertEqual(evidence["status"], "not_run")
                self.assertIn(evidence["status"], {"passed", "failed", "not_run"})
            with self.subTest(case=label, check="no invocation"):
                if recorder is None:
                    self.assertEqual(evidence["executor"]["status"], "not_run")
                else:
                    self.assertEqual(recorder.calls, [])

    def test_query_budget_counts_original_suite_and_cannot_pass_partial_coverage(self):
        executor = _RecordingExecutor({"rows": [{"Value": 1}]})
        queries = [self._query("first"), self._query("second")]

        evidence = validate_semantic_execution(queries, executor, max_queries=1)

        with self.subTest(check="bounded call count"):
            self.assertEqual(len(executor.calls), 1)
        with self.subTest(check="original request count"):
            self.assertEqual(evidence["queries_requested"], len(queries))
        with self.subTest(check="run count"):
            self.assertEqual(evidence["queries_run"], len(executor.calls))
        with self.subTest(check="partial suite is not passed"):
            self.assertNotEqual(evidence["status"], "passed")
            self.assertIn(evidence["status"], {"passed", "failed", "not_run"})
        # A caller must be able to tell that the configured budget left work
        # unexecuted, without relying on an undocumented result-list shape.
        explanation = str(evidence).lower()
        with self.subTest(check="budget reason is visible"):
            self.assertRegex(
                explanation,
                r"budget|limit|truncat|incomplete|not[_ ]run",
            )

    def test_provider_nonterminal_statuses_are_not_success_or_compared(self):
        for provider_status in ("not_run", "unavailable", "pending", "running", "queued"):
            with self.subTest(provider_status=provider_status):
                executor = _RecordingExecutor(
                    {"status": provider_status, "rows": [{"Value": 1}]}
                )
                evidence = validate_semantic_execution(
                    [self._query(expected_rows=[{"Value": 1}])], executor
                )

                self.assertEqual(len(executor.calls), 1)
                self.assertNotEqual(evidence["status"], "passed")
                self.assertIn(evidence["status"], {"passed", "failed", "not_run"})
                result = evidence["results"][0]
                self.assertNotEqual(result["status"], "passed")
                self.assertIn(result["status"], {"passed", "failed", "not_run"})
                self.assertNotIn("comparison", result["evidence"])

    def test_declared_execution_errors_remain_failed_and_visible(self):
        responses = (
            {"status": "failed", "message": "provider failure"},
            {"status": "error", "message": "provider error"},
            {"error": "provider rejected the query"},
        )
        for response in responses:
            with self.subTest(response=response):
                evidence = validate_semantic_execution(
                    [self._query()], _RecordingExecutor(response)
                )

                self.assertEqual(evidence["status"], "failed")
                self.assertEqual(evidence["results"][0]["status"], "failed")
                self.assertTrue(evidence["results"][0]["error"])

    def test_explicit_null_expectations_are_compared_but_missing_is_smoke_only(self):
        for expectation_key in ("expected_rows", "expected"):
            with self.subTest(explicit_expectation=expectation_key):
                query = self._query(**{expectation_key: None})
                evidence = validate_semantic_execution(
                    [query],
                    _RecordingExecutor({"rows": [{"Value": 1}]}),
                )

                self.assertEqual(evidence["status"], "failed")
                comparison = evidence["results"][0]["evidence"]["comparison"]
                self.assertFalse(comparison["matched"])

        smoke_evidence = validate_semantic_execution(
            [self._query()],
            _RecordingExecutor({"rows": [{"Value": 1}]}),
        )
        self.assertEqual(smoke_evidence["status"], "passed")
        self.assertNotIn("comparison", smoke_evidence["results"][0]["evidence"])

    def test_missing_result_data_is_not_forged_as_an_observed_null(self):
        evidence = validate_semantic_execution(
            [self._query(expected_rows=None)],
            _RecordingExecutor({"status": "passed"}),
        )

        self.assertNotEqual(evidence["status"], "passed")
        self.assertNotEqual(evidence["results"][0]["status"], "passed")
        self.assertNotIn("comparison", evidence["results"][0]["evidence"])

    def test_rows_data_raw_and_genuine_empty_or_null_results_are_supported(self):
        cases = (
            ({"rows": [{"Value": 1}]}, [{"Value": 1}]),
            ({"data": [{"Value": 1}]}, [{"Value": 1}]),
            ([{"Value": 1}], [{"Value": 1}]),
            ({"rows": []}, []),
            ([], []),
            ({"rows": None}, None),
        )
        for response, expected in cases:
            with self.subTest(response=response, expected=expected):
                evidence = validate_semantic_execution(
                    [self._query(expected_rows=expected)],
                    _RecordingExecutor(response),
                )

                self.assertEqual(evidence["status"], "passed")
                self.assertTrue(
                    evidence["results"][0]["evidence"]["comparison"]["matched"]
                )

    def test_boolean_values_are_not_numeric_comparison_matches(self):
        for actual, expected in ((True, 1), (False, 0), (1, True), (0, False)):
            with self.subTest(actual=actual, expected=expected):
                evidence = validate_semantic_execution(
                    [self._query(expected_rows=expected)],
                    _RecordingExecutor({"rows": actual}),
                )

                self.assertEqual(evidence["status"], "failed")
                self.assertFalse(
                    evidence["results"][0]["evidence"]["comparison"]["matched"]
                )

    def test_nested_type_key_list_and_null_mismatches_remain_strict(self):
        mismatches = (
            ([{"nested": {"flag": False}}], [{"nested": {"flag": 0}}]),
            ({"nested": [None]}, {"nested": [False]}),
            ([{"a": 1}], [{"b": 1}]),
            ([{"a": [1, 2]}], [{"a": [1]}]),
        )
        for actual, expected in mismatches:
            with self.subTest(actual=actual, expected=expected):
                evidence = validate_semantic_execution(
                    [self._query(expected_rows=expected)],
                    _RecordingExecutor({"rows": actual}),
                )

                self.assertEqual(evidence["status"], "failed")
                self.assertFalse(
                    evidence["results"][0]["evidence"]["comparison"]["matched"]
                )

    def test_finite_numeric_tolerance_is_inclusive_but_not_overinclusive(self):
        cases = (
            (1, 1.0, 0.0, "passed"),
            (1.25, 1.0, 0.25, "passed"),
            (1.2501, 1.0, 0.25, "failed"),
        )
        for actual, expected, tolerance, status in cases:
            with self.subTest(actual=actual, expected=expected, tolerance=tolerance):
                evidence = validate_semantic_execution(
                    [
                        self._query(
                            expected_rows=expected,
                            tolerance=tolerance,
                        )
                    ],
                    _RecordingExecutor({"rows": actual}),
                )

                self.assertEqual(evidence["status"], status)

    def test_mixed_integer_float_comparison_preserves_integer_precision(self):
        actual = int(2**53 + 1)
        expected = float(2**53)
        evidence = validate_semantic_execution(
            [
                self._query(
                    expected_rows=expected,
                    tolerance=0.25,
                )
            ],
            _RecordingExecutor({"rows": actual}),
        )

        result = evidence["results"][0]
        comparison = result["evidence"]["comparison"]
        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(comparison["matched"])
        self.assertTrue(comparison["mismatches"])
        self.assertIn(str(actual), comparison["mismatches"][0])

    def test_smoke_observations_must_be_finite_and_json_safe(self):
        for rows in (object(), float("inf"), float("-inf"), float("nan")):
            with self.subTest(observation_type=type(rows).__name__, rows=repr(rows)):
                evidence = validate_semantic_execution(
                    [self._query()],
                    _RecordingExecutor({"rows": rows}),
                )

                self.assertEqual(evidence["status"], "failed")
                self.assertEqual(evidence["results"][0]["status"], "failed")
                self.assertTrue(evidence["results"][0]["error"])
                json.dumps(evidence, allow_nan=False)

    def test_cyclic_expected_rows_are_rejected_as_json_safe_failure_evidence(self):
        cyclic_expected = []
        cyclic_expected.append(cyclic_expected)
        cyclic_observed = []
        cyclic_observed.append(cyclic_observed)
        executor = _RecordingExecutor({"rows": cyclic_observed})

        try:
            evidence = validate_semantic_execution(
                [self._query(expected_rows=cyclic_expected)],
                executor,
            )
        except RecursionError:
            self.fail(
                "cyclic semantic values must be captured, not escape as RecursionError"
            )

        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(evidence["results"][0]["status"], "failed")
        self.assertTrue(evidence["results"][0]["error"])
        json.dumps(evidence, allow_nan=False)

    def test_wrapper_provenance_is_retained_without_spoofing_comparison_evidence(self):
        retained = validate_semantic_execution(
            [self._query(expected_rows=1)],
            _RecordingExecutor({
                "rows": 1,
                "evidence": {"observation_id": "synthetic-check-1"},
                "duration_ms": 7,
            }),
        )
        retained_result = retained["results"][0]
        retained_json = json.dumps(retained, allow_nan=False)
        self.assertEqual(retained["status"], "passed")
        self.assertIn("synthetic-check-1", retained_json)
        self.assertEqual(retained_result["duration_ms"], 7)

        spoofed = validate_semantic_execution(
            [self._query(expected_rows=2)],
            _RecordingExecutor({
                "rows": 1,
                "evidence": {
                    "comparison": {"matched": True, "mismatches": []},
                    "observation": "not_received",
                    "executor_called": False,
                },
            }),
        )
        spoofed_result = spoofed["results"][0]
        self.assertEqual(spoofed["status"], "failed")
        self.assertEqual(spoofed_result["status"], "failed")
        self.assertFalse(spoofed_result["evidence"]["comparison"]["matched"])
        self.assertTrue(spoofed_result["evidence"]["comparison"]["mismatches"])
        self.assertEqual(spoofed_result["evidence"]["observation"], "received")
        self.assertTrue(spoofed_result["evidence"]["executor_called"])
        json.dumps(spoofed, allow_nan=False)

    def test_reused_response_rows_and_nested_provider_response_are_snapshotted(self):
        shared_row = {"n": 1}
        shared_provider_response = {"trace": {"sequence": 1}}
        shared_response = {
            "rows": [shared_row],
            "evidence": {
                "observation_id": "synthetic-response-1",
                "provider_response": shared_provider_response,
            },
            "duration_ms": 1,
        }
        calls = []

        def execute(query):
            calls.append(query)
            if len(calls) == 2:
                shared_row["n"] = 2
                shared_provider_response["trace"]["sequence"] = 2
                shared_response["evidence"]["observation_id"] = "synthetic-response-2"
                shared_response["duration_ms"] = 2
            return shared_response

        queries = [
            self._query("first", expected_rows=[{"n": 1}]),
            self._query("second", expected_rows=[{"n": 2}]),
        ]
        execution = validate_semantic_execution(queries, _RecordingExecutor(execute))
        first, second = execution["results"]

        self.assertEqual(calls, [query["dax"] for query in queries])
        self.assertEqual(execution["status"], "passed")
        self.assertTrue(first["evidence"]["comparison"]["matched"])
        self.assertTrue(second["evidence"]["comparison"]["matched"])
        self.assertEqual(first["rows"], [{"n": 1}])
        self.assertEqual(second["rows"], [{"n": 2}])
        self.assertIn("synthetic-response-1", json.dumps(first))
        self.assertIn("synthetic-response-2", json.dumps(second))
        self.assertIn(
            {"trace": {"sequence": 1}},
            self._values_for_key(first, "provider_response"),
        )
        self.assertIn(
            {"trace": {"sequence": 2}},
            self._values_for_key(second, "provider_response"),
        )

        shared_row["n"] = 3
        shared_provider_response["trace"]["sequence"] = 3
        shared_response["evidence"]["observation_id"] = "mutated-after-return"
        shared_response["duration_ms"] = 3
        self.assertEqual(first["rows"], [{"n": 1}])
        self.assertEqual(second["rows"], [{"n": 2}])
        self.assertIn("synthetic-response-1", json.dumps(first))
        self.assertIn("synthetic-response-2", json.dumps(second))
        self.assertNotIn("mutated-after-return", json.dumps(first))
        self.assertNotIn("mutated-after-return", json.dumps(second))
        self.assertIn(
            {"trace": {"sequence": 1}},
            self._values_for_key(first, "provider_response"),
        )
        self.assertIn(
            {"trace": {"sequence": 2}},
            self._values_for_key(second, "provider_response"),
        )

    def test_executor_mutation_cannot_change_current_expected_rows(self):
        query = self._query("self-mutating", expected_rows=[{"n": 1}])

        def execute(dax):
            query["expected_rows"][0]["n"] = 99
            return {"rows": [{"n": 99}]}

        execution = validate_semantic_execution(
            [query],
            _RecordingExecutor(execute),
        )

        result = execution["results"][0]
        self.assertEqual(query["expected_rows"], [{"n": 99}])
        self.assertEqual(execution["status"], "failed")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["evidence"]["comparison"]["matched"])
        self.assertTrue(result["evidence"]["comparison"]["mismatches"])
        self.assertIn(
            "expected 1, got 99",
            result["evidence"]["comparison"]["mismatches"][0],
        )

    def test_first_callback_cannot_mutate_later_query_plan(self):
        first_query = self._query("first")
        later_query = self._query("later", expected_rows=[{"n": 1}])
        first_dax = first_query["dax"]
        planned_later_dax = later_query["dax"]
        mutated_later_dax = 'EVALUATE ROW("Name", "mutated-later")'
        calls = []

        def execute(dax):
            calls.append(dax)
            if dax == first_dax:
                later_query["expected_rows"][0]["n"] = 99
                later_query["dax"] = mutated_later_dax
                return {"rows": []}
            return {"rows": [{"n": 99}]}

        execution = validate_semantic_execution(
            [first_query, later_query],
            _RecordingExecutor(execute),
        )

        first, later = execution["results"]
        self.assertEqual(calls, [first_dax, planned_later_dax])
        self.assertEqual(later_query["dax"], mutated_later_dax)
        self.assertEqual(later_query["expected_rows"], [{"n": 99}])
        self.assertEqual(later["query"], planned_later_dax)
        self.assertEqual(execution["status"], "failed")
        self.assertEqual(first["status"], "passed")
        self.assertEqual(later["status"], "failed")
        self.assertFalse(later["evidence"]["comparison"]["matched"])
        self.assertTrue(later["evidence"]["comparison"]["mismatches"])
        self.assertIn(
            "expected 1, got 99",
            later["evidence"]["comparison"]["mismatches"][0],
        )

    def test_invalid_query_schemas_and_tolerances_are_rejected_without_invocation(self):
        invalid_queries = [
            {"name": "missing-dax"},
            {"dax": 'EVALUATE ROW("Value", 1)'},
        ]
        bad_tolerances = (
            ("negative", -0.01),
            ("nan", float("nan")),
            ("infinite", float("inf")),
            ("negative-infinite", float("-inf")),
            ("boolean", True),
        )
        for name, tolerance in bad_tolerances:
            invalid_queries.append(
                self._query(
                    name,
                    expected_rows=[{"Value": 1}],
                    tolerance=tolerance,
                )
            )
        valid_query = self._query("valid", expected_rows=[{"Value": 1}])
        queries = [*invalid_queries, valid_query]

        def response_for_query(dax):
            if '"infinite"' in dax:
                return {"rows": [{"Value": 1e100}]}
            return {"rows": [{"Value": 1}]}

        executor = _RecordingExecutor(response_for_query)

        evidence = validate_semantic_execution(queries, executor)

        with self.subTest(check="original request count"):
            self.assertEqual(evidence["queries_requested"], len(queries))
        with self.subTest(check="invalid queries are not invoked"):
            self.assertEqual(executor.calls, [valid_query["dax"]])
        with self.subTest(check="invalid schema and tolerance fail the suite"):
            self.assertEqual(evidence["status"], "failed")
        by_query = {result["query"]: result for result in evidence["results"]}
        for invalid_query in invalid_queries:
            with self.subTest(invalid_query=invalid_query):
                result = by_query[invalid_query.get("dax", "")]
                self.assertEqual(result["status"], "failed")
                self.assertTrue(result["error"])
        with self.subTest(check="remaining valid query still runs"):
            self.assertEqual(by_query[valid_query["dax"]]["status"], "passed")

    def test_infinite_tolerance_cannot_hide_a_large_mismatch(self):
        executor = _RecordingExecutor({"rows": [{"Value": 1e100}]})
        evidence = validate_semantic_execution(
            [
                self._query(
                    expected_rows=[{"Value": 0}],
                    tolerance=float("inf"),
                )
            ],
            executor,
        )

        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(executor.calls, [])
        self.assertTrue(evidence["results"][0]["error"])

    def test_non_mapping_query_schema_fails_without_aborting_valid_queries(self):
        valid_query = self._query("valid", expected_rows=[{"Value": 1}])
        executor = _RecordingExecutor({"rows": [{"Value": 1}]})

        evidence = validate_semantic_execution([None, valid_query], executor)

        self.assertEqual(evidence["queries_requested"], 2)
        self.assertEqual(executor.calls, [valid_query["dax"]])
        self.assertEqual(evidence["status"], "failed")
        invalid_results = [
            result for result in evidence["results"] if result["name"] != "valid"
        ]
        self.assertEqual(len(invalid_results), 1)
        self.assertEqual(invalid_results[0]["status"], "failed")
        self.assertTrue(invalid_results[0]["error"])
        self.assertEqual(
            next(result for result in evidence["results"] if result["name"] == "valid")[
                "status"
            ],
            "passed",
        )

    def test_non_numeric_query_tolerances_fail_without_raising_or_invoking(self):
        invalid_queries = [
            self._query(
                "none-tolerance",
                expected_rows=[],
                tolerance=None,
            ),
            self._query(
                "text-tolerance",
                expected_rows=[],
                tolerance="not-a-number",
            ),
        ]
        valid_query = self._query("valid", expected_rows=[])
        executor = _RecordingExecutor({"rows": []})

        evidence = validate_semantic_execution(
            [*invalid_queries, valid_query],
            executor,
        )

        self.assertEqual(executor.calls, [valid_query["dax"]])
        self.assertEqual(evidence["status"], "failed")
        results_by_name = {result["name"]: result for result in evidence["results"]}
        for invalid_query in invalid_queries:
            with self.subTest(query=invalid_query["name"]):
                self.assertEqual(results_by_name[invalid_query["name"]]["status"], "failed")
                self.assertTrue(results_by_name[invalid_query["name"]]["error"])
        self.assertEqual(results_by_name["valid"]["status"], "passed")

    def test_invalid_default_tolerance_fails_without_invoking_executor(self):
        for tolerance in (
            float("nan"),
            float("inf"),
            float("-inf"),
            -0.01,
            True,
            None,
            "not-a-number",
        ):
            with self.subTest(default_tolerance=tolerance):
                executor = _RecordingExecutor({"rows": [{"Value": 1}]})
                evidence = validate_semantic_execution(
                    [self._query()],
                    executor,
                    default_tolerance=tolerance,
                )

                self.assertEqual(evidence["status"], "failed")
                self.assertEqual(executor.calls, [])
                self.assertTrue(evidence["results"][0]["error"])

    def test_nonfinite_numeric_observations_cannot_match(self):
        nonfinite = float("nan")
        cases = (
            (nonfinite, nonfinite),
            (float("inf"), float("inf")),
            (float("-inf"), float("-inf")),
            (float("inf"), 0.0),
        )
        for actual, expected in cases:
            with self.subTest(actual=actual, expected=expected):
                evidence = validate_semantic_execution(
                    [self._query(expected_rows=expected)],
                    _RecordingExecutor({"rows": actual}),
                )

                self.assertEqual(evidence["status"], "failed")
                self.assertFalse(
                    evidence["results"][0]["evidence"]["comparison"]["matched"]
                )


if __name__ == "__main__":
    unittest.main()
