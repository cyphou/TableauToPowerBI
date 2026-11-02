"""Runtime boundary for optional semantic-model execution checks.

The migration engine remains offline-first. Callers may inject an executor
function or object when an authorized Power BI/Fabric runtime is available;
without one, validation is explicitly reported as ``not_run``.
"""

from __future__ import annotations

import json
import math
import re
import reprlib
import time
from copy import deepcopy
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Optional

# Leave headroom for the result/report envelope and recursive comparison.
_MAX_JSON_DEPTH = 128


@dataclass
class SemanticExecutionResult:
    """Normalized result for one runtime semantic query."""

    name: str
    status: str
    query: str = ""
    rows: Any = None
    duration_ms: Optional[float] = None
    error: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "name": self.name,
            "status": self.status,
            "query": self.query,
            "rows": self.rows,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "evidence": dict(self.evidence),
        }
        if self.evidence.get("observation") == "invalid":
            del result["rows"]
        return result


def _execute(executor: Any, query: str) -> Any:
    if callable(executor):
        return executor(query)
    method = getattr(executor, "execute", None)
    if callable(method):
        return method(query)
    raise TypeError("semantic executor must be callable or expose execute(query)")


def describe_executor(executor: Any) -> Dict[str, Any]:
    """Return redacted executor capability evidence without exposing credentials."""
    if executor is None:
        return {"status": "not_run", "provider": None, "version": None}
    metadata = getattr(executor, "metadata", {})
    if callable(metadata):
        metadata = metadata()
    metadata = metadata if isinstance(metadata, dict) else {}
    provider = metadata.get("provider", getattr(executor, "provider", None))
    version = metadata.get("version", getattr(executor, "version", None))
    return {
        "status": "configured",
        "provider": _redact_value(provider),
        "version": _redact_value(version),
        "capabilities": sorted(str(item) for item in metadata.get("capabilities", []) or []),
        "endpoint_present": bool(metadata.get("endpoint") or getattr(executor, "endpoint", None)),
    }


def _redact_value(value: Any) -> Any:
    if value is None:
        return None
    text = str(value)
    if re.search(r"secret|token|password|key|credential", text, re.IGNORECASE):
        return "[REDACTED]"
    return text[:128]


def _json_value_error(value: Any, path: str) -> str:
    """Reject non-JSON values, cycles and excessive nesting without repairing them."""
    ancestors: set[int] = set()

    def visit(item: Any, location: str, depth: int) -> str:
        if depth > _MAX_JSON_DEPTH:
            return f"{location}: JSON nesting exceeds {_MAX_JSON_DEPTH} levels"
        kind = type(item)
        if item is None or kind in (str, bool, int):
            return ""
        if kind is float:
            return "" if math.isfinite(item) else f"{location}: non-finite number"
        if kind not in (dict, list):
            return f"{location}: unsupported JSON value type {kind.__name__}"
        identity = id(item)
        if identity in ancestors:
            return f"{location}: cyclic JSON value"
        ancestors.add(identity)
        try:
            if kind is dict:
                for key, child in item.items():
                    if type(key) is not str:
                        return f"{location}: object keys must be strings"
                    error = visit(child, f"{location}.{key}", depth + 1)
                    if error:
                        return error
            else:
                for index, child in enumerate(item):
                    error = visit(child, f"{location}[{index}]", depth + 1)
                    if error:
                        return error
        finally:
            ancestors.remove(identity)
        return ""

    try:
        error = visit(value, path, 0)
        if error:
            return error
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        return f"{path}: JSON serialization failed: {_exception_message(exc)}"
    return ""


def _value_diagnostic(value: Any, error: str) -> Dict[str, str]:
    """Record why a raw value was omitted, retaining bounded representation context."""
    try:
        representation = reprlib.repr(value)
    except Exception as exc:  # injected values may have a broken __repr__
        representation = f"<representation unavailable: {_exception_message(exc)}>"
    return {"error": error, "type": type(value).__name__, "repr": representation}


def _normalize_response(response: Any) -> tuple[str, Any, str, Dict[str, Any]]:
    """Keep provider context separate from library-owned observation qualification."""
    evidence: Dict[str, Any] = {
        "observation": "not_received",
        "response_kind": "wrapper" if isinstance(response, dict) else "raw",
    }
    response_error = _json_value_error(response, "response")
    if response_error:
        evidence["observation"] = "invalid"
        evidence["response_diagnostic"] = _value_diagnostic(response, response_error)
        if type(response) is dict:
            # Preserve safe provenance without presenting a partial wrapper as original.
            evidence["provider_response_fields"] = {
                key: deepcopy(value)
                for key, value in response.items()
                if type(key) is str and not _json_value_error(value, f"response.{key}")
            }
        return "failed", None, response_error, evidence

    response = deepcopy(response)
    if isinstance(response, dict):
        evidence["provider_response"] = response
        if response.get("error"):
            return "failed", None, str(response["error"]), evidence

        provider_status: Optional[str] = None
        if "status" in response:
            raw_status = response["status"]
            if not isinstance(raw_status, str):
                return (
                    "failed",
                    None,
                    "executor response status must be a string",
                    evidence,
                )
            provider_status = raw_status.strip().lower()
            evidence["provider_status"] = provider_status
            if provider_status in {"failed", "error"}:
                message = response.get("message")
                detail = str(message) if message not in (None, "") else "execution failed"
                return (
                    "failed",
                    None,
                    detail,
                    evidence,
                )
            if provider_status in {
                "not_run", "unavailable", "pending", "running", "queued",
                "skipped", "cancelled", "canceled", "not_started",
            }:
                message = response.get("message")
                detail = (
                    f"provider status {provider_status!r} indicates execution was not run"
                )
                if message not in (None, ""):
                    detail += f": {message}"
                return (
                    "not_run",
                    None,
                    detail,
                    evidence,
                )
            if provider_status not in {"passed", "success", "succeeded"}:
                return (
                    "failed",
                    None,
                    f"unknown executor response status {provider_status!r}",
                    evidence,
                )

        if "rows" in response:
            rows = response["rows"]
            response_kind = "rows"
        elif "data" in response:
            rows = response["data"]
            response_kind = "data"
        else:
            message = (
                "successful executor response is missing rows/data"
                if provider_status is not None
                else "executor response object must declare rows or data"
            )
            return (
                "failed",
                None,
                message,
                evidence,
            )

        evidence["observation"] = "received"
        evidence["response_kind"] = response_kind
        return "passed", rows, "", evidence

    if response is None:
        return (
            "failed",
            None,
            "executor returned no response; declare rows/data to record a null result",
            evidence,
        )
    evidence["observation"] = "received"
    return "passed", response, "", evidence


def _validated_tolerance(value: Any, label: str) -> tuple[Optional[float], str]:
    """Accept only finite, non-negative JSON-style int/float tolerances."""
    if type(value) not in (int, float):
        return None, f"{label} must be a finite non-negative number (not a boolean)"
    try:
        tolerance = float(value)
    except (OverflowError, TypeError, ValueError):
        return None, f"{label} must be a finite non-negative number"
    if not math.isfinite(tolerance) or tolerance < 0:
        return None, f"{label} must be a finite non-negative number"
    return tolerance, ""


def _value_description(value: Any) -> str:
    if value is None or isinstance(value, (str, bool, int, float)):
        return repr(value)
    return f"<{type(value).__name__}>"


def _compare_values(actual: Any, expected: Any, tolerance: float, path: str = "value") -> List[str]:
    """Return deterministic mismatch descriptions for JSON-like query values."""
    actual_is_number = type(actual) in (int, float)
    expected_is_number = type(expected) in (int, float)
    if actual_is_number and expected_is_number:
        if (
            (isinstance(actual, float) and not math.isfinite(actual))
            or (isinstance(expected, float) and not math.isfinite(expected))
        ):
            return [f"{path}: non-finite numeric values cannot match"]
        try:
            if isinstance(actual, int) and isinstance(expected, int):
                matched = abs(actual - expected) <= tolerance
            else:
                matched = abs(Fraction(actual) - Fraction(expected)) <= tolerance
        except (OverflowError, TypeError, ValueError):
            matched = False
        if matched:
            return []
        return [
            f"{path}: expected {_value_description(expected)}, "
            f"got {_value_description(actual)}"
        ]
    if type(actual) is not type(expected):
        return [
            f"{path}: expected type {type(expected).__name__}, "
            f"got {type(actual).__name__}"
        ]
    if type(actual) is dict:
        mismatches: List[str] = []
        if any(not isinstance(key, str) for key in actual) or any(
            not isinstance(key, str) for key in expected
        ):
            return [f"{path}: object keys must be strings"]
        for key in sorted(set(actual) | set(expected)):
            if key not in actual:
                mismatches.append(f"{path}.{key}: missing actual value")
            elif key not in expected:
                mismatches.append(f"{path}.{key}: unexpected actual value")
            else:
                mismatches.extend(_compare_values(actual[key], expected[key], tolerance, f"{path}.{key}"))
        return mismatches
    if type(actual) is list:
        mismatches = []
        if len(actual) != len(expected):
            mismatches.append(f"{path}: expected {len(expected)} row(s), got {len(actual)}")
        for index, (actual_item, expected_item) in enumerate(zip(actual, expected)):
            mismatches.extend(_compare_values(actual_item, expected_item, tolerance, f"{path}[{index}]"))
        return mismatches
    if actual is None or type(actual) in (str, bool):
        return [] if actual == expected else [
            f"{path}: expected {_value_description(expected)}, "
            f"got {_value_description(actual)}"
        ]
    return [f"{path}: unsupported comparison value type {type(actual).__name__}"]


def _exception_message(exc: Exception) -> str:
    """Keep arbitrary injected runtime exceptions inside the evidence boundary."""
    try:
        message = str(exc)
    except Exception:  # an injected exception may have a broken __str__
        message = f"{type(exc).__name__} (message unavailable)"
    return message or type(exc).__name__


def _query_fields(
    item: Any,
    index: int,
    default_tolerance: Any,
) -> tuple[str, str, bool, Any, Optional[float], str]:
    """Validate a query record without invoking the runtime executor."""
    fallback_name = f"query-{index + 1}"
    if not isinstance(item, dict):
        return (
            fallback_name,
            "",
            False,
            None,
            None,
            "query entry must be an object with name and dax fields",
        )

    errors: List[str] = []
    raw_name = item.get("name")
    if isinstance(raw_name, str) and raw_name.strip():
        name = raw_name
    else:
        name = fallback_name
        errors.append("query name must be a non-empty string")

    raw_query = item.get("dax")
    if isinstance(raw_query, str):
        query = raw_query
        if not raw_query.strip():
            errors.append("query dax must be a non-empty string")
    else:
        query = ""
        errors.append("query dax must be a non-empty string")

    has_expected = "expected_rows" in item or "expected" in item
    if "expected_rows" in item:
        expected = item["expected_rows"]
    elif "expected" in item:
        expected = item["expected"]
    else:
        expected = None

    if "tolerance" in item:
        tolerance, tolerance_error = _validated_tolerance(item["tolerance"], "tolerance")
    else:
        tolerance, tolerance_error = _validated_tolerance(
            default_tolerance, "default_tolerance"
        )
    if tolerance_error:
        errors.append(tolerance_error)

    return name, query, has_expected, expected, tolerance, "; ".join(errors)


def _result(
    name: str,
    query: str,
    status: str,
    error: str = "",
    *,
    rows: Any = None,
    duration_ms: Optional[float] = None,
    evidence: Optional[Dict[str, Any]] = None,
) -> SemanticExecutionResult:
    return SemanticExecutionResult(
        name=name,
        status=status,
        query=query,
        rows=rows,
        duration_ms=duration_ms,
        error=error,
        evidence=evidence or {},
    )


def validate_semantic_execution(
    queries: Iterable[Dict[str, Any]],
    executor: Any = None,
    *,
    max_queries: int = 25,
    default_tolerance: float = 0.0,
) -> Dict[str, Any]:
    """Execute bounded semantic queries and return portable evidence.

    ``queries`` entries require ``name`` and ``dax`` keys and may provide
    ``expected_rows``/``expected`` plus a numeric ``tolerance``. An absent
    executor never counts as success; valid queries are explicitly ``not_run``.
    A suite passes only when every requested query has a terminal successful
    observation within the executor-call budget. Runtime errors and result
    mismatches are captured per query so one bad query does not hide the
    remaining evidence.

    JSON-like values are limited to 128 nesting levels. Valid wrappers are
    retained in ``evidence.provider_response``; invalid responses have a
    ``response_diagnostic`` and only safe ``provider_response_fields``, with
    no serialized ``rows`` observation. Provider timing, when a non-negative
    number, is retained as ``duration_ms`` with adapter timing and its source
    recorded separately in evidence.

    All planned query fields and validated references are snapshotted before
    executor callbacks. Validated observations and provider context are also
    snapshotted so later provider mutations cannot rewrite retained evidence.
    """
    try:
        requested = [] if queries is None else list(queries)
    except TypeError:
        requested = []
        query_input_error = "queries must be an iterable of query objects"
    else:
        query_input_error = ""

    if isinstance(max_queries, bool) or not isinstance(max_queries, int):
        query_budget = 0
        budget_error = "max_queries must be a non-negative integer"
        budget_note = ""
    elif max_queries < 0:
        query_budget = 0
        budget_error = ""
        budget_note = "Negative max_queries was treated as a zero query budget."
    else:
        query_budget = max_queries
        budget_error = ""
        budget_note = ""

    query_plan: List[tuple[str, str, bool, Any, Optional[float], str]] = []
    expected_diagnostics: Dict[int, Dict[str, str]] = {}
    for index, item in enumerate(requested):
        name, query, has_expected, expected, tolerance, validation_error = _query_fields(
            item, index, default_tolerance
        )
        if validation_error or tolerance is None:
            expected = None
        elif has_expected:
            expected_error = _json_value_error(expected, "expected")
            if not expected_error:
                try:
                    expected = deepcopy(expected)
                except (TypeError, ValueError, OverflowError, RecursionError) as exc:
                    expected_error = (
                        f"expected: JSON snapshot failed: {_exception_message(exc)}"
                    )
            if expected_error:
                expected_diagnostics[index] = _value_diagnostic(expected, expected_error)
                expected = None
        query_plan.append((name, query, has_expected, expected, tolerance, validation_error))

    executor_evidence = describe_executor(executor)

    results: List[SemanticExecutionResult] = []
    diagnostics: List[str] = []
    executor_calls = 0
    budget_skipped = 0
    if query_input_error:
        diagnostics.append(query_input_error)
    if budget_error:
        diagnostics.append(budget_error)
    if budget_note and requested:
        diagnostics.append(budget_note)
    if not requested and not query_input_error:
        diagnostics.append("No semantic queries were requested; runtime validation was not run.")

    for index, (
        name, query, has_expected, expected, tolerance, validation_error,
    ) in enumerate(query_plan):
        if validation_error or tolerance is None:
            validation_error = validation_error or "query tolerance is unavailable"
            if budget_error:
                validation_error = f"{validation_error}; {budget_error}"
            results.append(_result(
                name,
                query,
                "failed",
                validation_error,
                evidence={
                    "observation": "not_received",
                    "executor_called": False,
                    "reason": "invalid_query",
                },
            ))
            diagnostics.append(f"Query {name!r} was rejected: {validation_error}")
            continue

        if index in expected_diagnostics:
            expected_diagnostic = expected_diagnostics[index]
            expected_error = expected_diagnostic["error"]
            results.append(_result(
                name,
                query,
                "failed",
                expected_error,
                evidence={
                    "observation": "not_received",
                    "executor_called": False,
                    "reason": "invalid_expectation",
                    "expected_diagnostic": expected_diagnostic,
                    "comparison": {
                        "status": "not_run",
                        "matched": False,
                        "tolerance": tolerance,
                        "mismatches": [],
                        "error": expected_error,
                    },
                },
            ))
            diagnostics.append(f"Query {name!r} was rejected: {expected_error}")
            continue

        if query_input_error:
            reason = query_input_error
            results.append(_result(
                name,
                query,
                "failed",
                reason,
                evidence={
                    "observation": "not_received",
                    "executor_called": False,
                    "reason": "invalid_query_suite",
                },
            ))
            continue

        if budget_error:
            results.append(_result(
                name,
                query,
                "failed",
                budget_error,
                evidence={
                    "observation": "not_received",
                    "executor_called": False,
                    "reason": "invalid_query_budget",
                },
            ))
            continue

        if executor is None:
            reason = "semantic executor is not configured; execution was not run"
            results.append(_result(
                name,
                query,
                "not_run",
                reason,
                evidence={
                    "observation": "not_received",
                    "executor_called": False,
                    "reason": "executor_unavailable",
                },
            ))
            continue

        if executor_calls >= query_budget:
            reason = (
                f"query budget of {query_budget} exhausted; execution was not run"
            )
            results.append(_result(
                name,
                query,
                "not_run",
                reason,
                evidence={
                    "observation": "not_received",
                    "executor_called": False,
                    "reason": "budget_exhausted",
                },
            ))
            budget_skipped += 1
            continue

        executor_calls += 1
        started = time.perf_counter()
        evidence: Dict[str, Any]
        try:
            response = _execute(executor, query)
            status, rows, error, evidence = _normalize_response(response)
        except Exception as exc:  # injected executor/response failures are runtime evidence
            status, rows, error = "failed", None, _exception_message(exc)
            evidence = {"observation": "not_received"}

        evidence["executor_called"] = True
        if status == "passed" and has_expected:
            try:
                mismatches = _compare_values(rows, expected, tolerance)
            except (TypeError, ValueError, OverflowError, RecursionError) as exc:
                status = "failed"
                error = f"semantic result comparison failed: {_exception_message(exc)}"
                evidence["comparison"] = {
                    "matched": False,
                    "tolerance": tolerance,
                    "mismatches": [],
                    "error": error,
                    "error_type": type(exc).__name__,
                }
            else:
                evidence["comparison"] = {
                    "matched": not mismatches,
                    "tolerance": tolerance,
                    "mismatches": mismatches,
                }
                if mismatches:
                    status = "failed"
                    error = "semantic result mismatch"
        elif has_expected and evidence.get("observation") == "invalid":
            evidence["comparison"] = {
                "status": "not_run",
                "matched": False,
                "tolerance": tolerance,
                "mismatches": [],
                "error": error,
            }

        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        provider_response = evidence.get("provider_response")
        if isinstance(provider_response, dict):
            provider_duration = provider_response.get("duration_ms")
            if (
                isinstance(provider_duration, (int, float))
                and not isinstance(provider_duration, bool)
                and provider_duration >= 0
            ):
                evidence["adapter_duration_ms"] = duration_ms
                evidence["duration_ms_source"] = "provider"
                duration_ms = provider_duration
        results.append(_result(
            name,
            query,
            status,
            error,
            rows=rows,
            duration_ms=duration_ms,
            evidence=evidence,
        ))
        if status == "failed":
            diagnostics.append(f"Query {name!r} failed: {error}")
        elif status == "not_run":
            diagnostics.append(f"Query {name!r} was not run: {error}")

    if executor is None and any(result.status == "not_run" for result in results):
        diagnostics.append(
            "Semantic executor is not configured; runtime execution remains not_run."
        )
    if budget_skipped:
        diagnostics.append(
            f"Query budget limit {query_budget} left {budget_skipped} "
            "requested query(ies) not run."
        )

    passed = sum(result.status == "passed" for result in results)
    failed = sum(result.status == "failed" for result in results)
    not_run = sum(result.status == "not_run" for result in results)
    if failed or query_input_error or (budget_error and not requested):
        overall_status = "failed"
    elif (
        requested
        and passed == len(requested)
        and executor_calls == len(requested)
        and not_run == 0
    ):
        overall_status = "passed"
    else:
        overall_status = "not_run"

    return {
        "status": overall_status,
        "executor": executor_evidence,
        "queries_requested": len(requested),
        "queries_run": executor_calls,
        "passed": passed,
        "failed": failed,
        "queries_not_run": not_run,
        "results": [result.to_dict() for result in results],
        "diagnostics": diagnostics,
    }
