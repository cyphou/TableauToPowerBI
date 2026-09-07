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


def validate_semantic_execution(
    queries: Iterable[Dict[str, Any]],
    executor: Any = None,
    *,
    max_queries: int = 25,
) -> Dict[str, Any]:
    """Execute bounded semantic queries and return portable evidence.

    ``queries`` entries require ``name`` and ``dax`` keys. An absent executor
    never counts as success or failure; it returns ``not_run``. Runtime errors
    are captured per query so one bad query does not hide the remaining evidence.
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
        started = time.perf_counter()
        try:
            response = _execute(executor, query)
            status, rows, error, evidence = _normalize_response(response)
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
