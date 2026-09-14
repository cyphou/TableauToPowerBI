"""Runtime boundary for optional semantic-model execution checks.

The migration engine remains offline-first. Callers may inject an executor
function or object when an authorized Power BI/Fabric runtime is available;
without one, validation is explicitly reported as ``not_run``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional


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
        return {
            "name": self.name,
            "status": self.status,
            "query": self.query,
            "rows": self.rows,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "evidence": dict(self.evidence),
        }


def _execute(executor: Any, query: str) -> Any:
    if callable(executor):
        return executor(query)
    method = getattr(executor, "execute", None)
    if callable(method):
        return method(query)
    raise TypeError("semantic executor must be callable or expose execute(query)")


def _normalize_response(response: Any) -> tuple[str, Any, str, Dict[str, Any]]:
    if isinstance(response, dict):
        if response.get("error"):
            return "failed", response.get("rows"), str(response["error"]), response
        status = str(response.get("status", "passed")).lower()
        if status in {"failed", "error"}:
            return "failed", response.get("rows"), str(response.get("message", "execution failed")), response
        return "passed", response.get("rows", response.get("data")), "", response
    return "passed", response, "", {}


def _compare_values(actual: Any, expected: Any, tolerance: float, path: str = "value") -> List[str]:
    """Return deterministic mismatch descriptions for JSON-like query values."""
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        if abs(float(actual) - float(expected)) <= tolerance:
            return []
        return [f"{path}: expected {expected!r}, got {actual!r}"]
    if type(actual) is not type(expected):
        return [f"{path}: expected type {type(expected).__name__}, got {type(actual).__name__}"]
    if isinstance(actual, dict):
        mismatches: List[str] = []
        for key in sorted(set(actual) | set(expected), key=str):
            if key not in actual:
                mismatches.append(f"{path}.{key}: missing actual value")
            elif key not in expected:
                mismatches.append(f"{path}.{key}: unexpected actual value")
            else:
                mismatches.extend(_compare_values(actual[key], expected[key], tolerance, f"{path}.{key}"))
        return mismatches
    if isinstance(actual, list):
        mismatches = []
        if len(actual) != len(expected):
            mismatches.append(f"{path}: expected {len(expected)} row(s), got {len(actual)}")
        for index, (actual_item, expected_item) in enumerate(zip(actual, expected)):
            mismatches.extend(_compare_values(actual_item, expected_item, tolerance, f"{path}[{index}]"))
        return mismatches
    return [] if actual == expected else [f"{path}: expected {expected!r}, got {actual!r}"]


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
    executor never counts as success or failure; it returns ``not_run``.
    Runtime errors and result mismatches are captured per query so one bad query
    does not hide the remaining evidence.
    """
    selected = list(queries or [])[:max(0, max_queries)]
    if executor is None:
        return {
            "status": "not_run",
            "queries_requested": len(selected),
            "queries_run": 0,
            "passed": 0,
            "failed": 0,
            "results": [],
        }

    results: List[SemanticExecutionResult] = []
    for item in selected:
        name = str(item.get("name", "query"))
        query = str(item.get("dax", ""))
        expected = item.get("expected_rows", item.get("expected"))
        tolerance = float(item.get("tolerance", default_tolerance))
        started = time.perf_counter()
        try:
            response = _execute(executor, query)
            status, rows, error, evidence = _normalize_response(response)
            mismatches = []
            if status == "passed" and expected is not None:
                mismatches = _compare_values(rows, expected, tolerance)
                if mismatches:
                    status = "failed"
                    error = "semantic result mismatch"
                evidence = dict(evidence)
                evidence["comparison"] = {
                    "matched": not mismatches,
                    "tolerance": tolerance,
                    "mismatches": mismatches,
                }
        except Exception as exc:  # runtime boundary must return evidence, not raise
            status, rows, error, evidence = "failed", None, str(exc), {}
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        results.append(SemanticExecutionResult(
            name=name,
            status=status,
            query=query,
            rows=rows,
            duration_ms=duration_ms,
            error=error,
            evidence=evidence,
        ))

    passed = sum(result.status == "passed" for result in results)
    failed = len(results) - passed
    return {
        "status": "failed" if failed else "passed",
        "queries_requested": len(selected),
        "queries_run": len(results),
        "passed": passed,
        "failed": failed,
        "results": [result.to_dict() for result in results],
    }
