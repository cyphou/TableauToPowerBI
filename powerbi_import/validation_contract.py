"""Cross-artifact semantic and Power Query validation contract."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Dict

from .errors import ValidationError


def build_validation_contract(
    semantic_context: Mapping[str, Any],
    m_emitters: Mapping[str, Any],
    recovery: Mapping[str, Any],
) -> Dict[str, Any]:
    """Summarize existing validation evidence without changing policy outcomes."""
    for name, evidence in (
        ("semantic_context", semantic_context),
        ("m_emitters", m_emitters),
        ("recovery", recovery),
    ):
        if not isinstance(evidence, Mapping):
            raise ValidationError(
                f"{name} evidence must be a mapping, got {type(evidence).__name__}",
                item=name,
                source="validation_contract",
            )
    semantic_execution = semantic_context.get("execution", {}) or {}
    semantic_status = _status_from_execution(semantic_execution)
    semantic_diagnostics = _execution_diagnostics(semantic_execution)
    if (
        isinstance(semantic_execution, Mapping)
        and semantic_execution.get("status") == "passed"
    ):
        evidence_issues = _passed_execution_issues(semantic_execution)
        if evidence_issues:
            semantic_status = "failed"
            semantic_diagnostics.append(
                "Reported semantic status 'passed' was refused: "
                + "; ".join(evidence_issues)
            )
    elif (
        isinstance(semantic_execution, Mapping)
        and "status" in semantic_execution
        and semantic_status == "failed"
        and not (
            isinstance(semantic_execution.get("status"), str)
            and semantic_execution.get("status") in {"failed", "passed", "not_run"}
        )
    ):
        semantic_diagnostics.append(
            "Semantic execution status is not a recognized terminal status."
        )

    static_issues = sum(int(semantic_context.get(key, 0) or 0) for key in ("issue_count",))
    static_issues += int((semantic_context.get("measure_context", {}) or {}).get("issue_count", 0) or 0)
    static_issues += int((semantic_context.get("filter_context", {}) or {}).get("issue_count", 0) or 0)
    emitter_summary = m_emitters.get("summary", {}) or {}
    fallback_count = len(m_emitters.get("fallback_in_use", []) or [])
    error_count = int(emitter_summary.get("errors", 0) or 0)
    pending_count = int((recovery.get("counts", {}) or {}).get("pending_validation", 0) or 0)
    return {
        "schema_version": "1.0",
        "status": "static_evidence",
        "semantic": {
            "static_status": "failed" if static_issues else "passed",
            "static_issue_count": static_issues,
            "execution_status": semantic_status,
            "runtime": "not_run" if semantic_status == "not_run" else "evidence_recorded",
            "diagnostics": semantic_diagnostics,
        },
        "m": {
            "status": "failed" if error_count else ("warning" if fallback_count else "passed"),
            "fallback_in_use": fallback_count,
            "emitter_errors": error_count,
        },
        "recovery": {
            "status": "pending" if pending_count else "complete",
            "pending_validation": pending_count,
        },
        "release_ready": (
            not static_issues
            and semantic_status == "passed"
            and not error_count
            and not pending_count
        ),
    }


def _status_from_execution(execution: Mapping[str, Any]) -> str:
    if not isinstance(execution, Mapping):
        return "not_run"
    if "status" not in execution:
        failed = execution.get("failed")
        if type(failed) is int and failed > 0:
            return "failed"
        return "not_run"
    status = execution.get("status")
    if isinstance(status, str) and status in {"passed", "failed", "not_run"}:
        if status == "passed" and _passed_execution_issues(execution):
            return "failed"
        return status
    return "failed"


def _execution_diagnostics(execution: Any) -> list[str]:
    if not isinstance(execution, Mapping) or "diagnostics" not in execution:
        return []
    diagnostics = execution.get("diagnostics")
    if not isinstance(diagnostics, list):
        return ["Semantic execution diagnostics are malformed."]
    result = [item for item in diagnostics if isinstance(item, str) and item]
    if any(not isinstance(item, str) for item in diagnostics):
        result.append("Semantic execution diagnostics include a non-string entry.")
    return result


def _valid_count(execution: Mapping[str, Any], key: str) -> tuple[Any, str]:
    if key not in execution:
        return None, f"missing {key} count"
    value = execution.get(key)
    if type(value) is not int or value < 0:
        return None, f"{key} count must be a non-negative integer"
    return value, ""


def _passed_execution_issues(execution: Mapping[str, Any]) -> list[str]:
    """Return structural reasons a claimed adapter pass is not complete evidence."""
    issues: list[str] = []
    executor = execution.get("executor")
    if not isinstance(executor, Mapping):
        issues.append("executor descriptor is missing or malformed")
    else:
        if executor.get("status") != "configured":
            issues.append("executor is not recorded as configured")
        for key in ("provider", "version"):
            if key not in executor or (
                executor.get(key) is not None and not isinstance(executor.get(key), str)
            ):
                issues.append(f"executor {key} evidence is missing or malformed")
        capabilities = executor.get("capabilities")
        if not isinstance(capabilities, list) or any(
            not isinstance(item, str) for item in capabilities
        ):
            issues.append("executor capabilities evidence is missing or malformed")
        if type(executor.get("endpoint_present")) is not bool:
            issues.append("executor endpoint evidence is missing or malformed")

    counts: Dict[str, int] = {}
    for key in ("queries_requested", "queries_run", "passed", "failed"):
        value, error = _valid_count(execution, key)
        if error:
            issues.append(error)
        else:
            counts[key] = value
    if "queries_not_run" in execution:
        not_run, error = _valid_count(execution, "queries_not_run")
        if error:
            issues.append(error)
        elif not_run != 0:
            issues.append("queries_not_run count contradicts a passed execution")

    requested = counts.get("queries_requested")
    ran = counts.get("queries_run")
    passed = counts.get("passed")
    failed = counts.get("failed")
    if requested is not None and requested == 0:
        issues.append("requested query suite is empty")
    if requested is not None and ran is not None and ran != requested:
        issues.append("queries_run does not cover every requested query")
    if ran is not None and passed is not None and passed != ran:
        issues.append("passed count does not equal queries_run")
    if failed is not None and failed != 0:
        issues.append("failed count is nonzero")

    results = execution.get("results")
    if not isinstance(results, list):
        issues.append("query results are missing or malformed")
        results = []
    if requested is not None and len(results) != requested:
        issues.append("query result count does not equal queries_requested")
    if not results:
        issues.append("query result evidence is empty")

    for index, result in enumerate(results):
        prefix = f"result {index}"
        if not isinstance(result, Mapping):
            issues.append(f"{prefix} is malformed")
            continue
        if result.get("status") != "passed":
            issues.append(f"{prefix} is not a passed terminal observation")
        for key in ("name", "query"):
            value = result.get(key)
            if not isinstance(value, str) or not value.strip():
                issues.append(f"{prefix} {key} evidence is missing or malformed")
        if "rows" not in result:
            issues.append(f"{prefix} has no declared rows observation")
        if type(result.get("error")) is not str or result.get("error") != "":
            issues.append(f"{prefix} contains an error or malformed error field")
        evidence = result.get("evidence")
        if not isinstance(evidence, Mapping):
            issues.append(f"{prefix} adapter evidence is missing or malformed")
        else:
            if evidence.get("observation") != "received":
                issues.append(f"{prefix} has no received observation evidence")
            if evidence.get("executor_called") is not True:
                issues.append(f"{prefix} has no executor-call evidence")
            response_kind = evidence.get("response_kind")
            if not isinstance(response_kind, str) or response_kind not in {
                "raw", "rows", "data",
            }:
                issues.append(f"{prefix} response kind is missing or malformed")
            if "comparison" in evidence:
                comparison = evidence.get("comparison")
                mismatches = (
                    comparison.get("mismatches")
                    if isinstance(comparison, Mapping)
                    else None
                )
                if (
                    not isinstance(comparison, Mapping)
                    or comparison.get("matched") is not True
                    or not isinstance(mismatches, list)
                    or mismatches
                ):
                    issues.append(f"{prefix} comparison evidence contradicts a pass")
                else:
                    tolerance = comparison.get("tolerance")
                    try:
                        valid_tolerance = (
                            not isinstance(tolerance, bool)
                            and isinstance(tolerance, (int, float))
                            and math.isfinite(tolerance)
                            and tolerance >= 0
                        )
                    except (OverflowError, TypeError, ValueError):
                        valid_tolerance = False
                    if not valid_tolerance:
                        issues.append(f"{prefix} comparison tolerance is invalid")

    if "diagnostics" in execution:
        diagnostics = execution.get("diagnostics")
        if not isinstance(diagnostics, list) or diagnostics:
            issues.append("diagnostics contradict a passed execution")
        elif any(not isinstance(item, str) for item in diagnostics):
            issues.append("diagnostics are malformed")
    return issues
