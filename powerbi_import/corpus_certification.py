"""Corpus-level certification contract for migration evidence summaries."""

from __future__ import annotations

from collections import Counter
import json
import os
from typing import Any, Iterable, Mapping


STATES = ("certified_static", "needs_review", "blocked")


def certify_corpus(reports: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Classify each workbook without promoting unavailable runtime evidence."""
    items = []
    for index, report in enumerate(reports):
        item = _classify_report(report, index)
        items.append(item)
    counts = Counter(item["state"] for item in items)
    return {
        "schema_version": "1.0",
        "status": "certified" if items and not counts["blocked"] else "needs_review",
        "corpus_count": len(items),
        "counts": {state: counts.get(state, 0) for state in STATES},
        "runtime_boundary": {
            "desktop": "not_run",
            "semantic_execution": "not_run",
            "refresh": "not_run",
            "deployment": "not_run",
        },
        "release_claim": "static_corpus_only",
        "reports": items,
    }


def certify_quality_files(paths: Iterable[str]) -> dict[str, Any]:
    """Load quality JSON files and certify the available corpus evidence."""
    reports = []
    load_errors = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                reports.append(json.load(handle))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            load_errors.append({"path": os.fspath(path), "error": str(exc)})
    result = certify_corpus(reports)
    result["quality_files"] = len(reports)
    result["load_errors"] = load_errors
    if load_errors:
        result["status"] = "needs_review"
    return result


def _classify_report(report: Mapping[str, Any], index: int) -> dict[str, Any]:
    name = str(report.get("report_name") or f"item-{index}")
    blockers = list(report.get("blockers", []) or [])
    summary = report.get("summary", report) or {}
    validation = summary.get("validation", {}) or {}
    recovery = summary.get("recovery", {}) or {}
    roundtrip = summary.get("roundtrip", {}) or {}
    reasons = []
    if blockers or report.get("status") == "FAIL":
        state = "blocked"
        reasons.extend(blockers or ["report status is FAIL"])
    elif not validation.get("release_ready", False):
        state = "needs_review"
        if validation.get("semantic_execution", "not_run") == "not_run":
            reasons.append("semantic execution is not_run")
        if recovery.get("status") != "complete":
            reasons.append("recovery is incomplete")
        if roundtrip.get("static_status") != "static_pass":
            reasons.append("static round-trip validation did not pass")
    else:
        state = "certified_static"
    return {
        "report_name": name,
        "state": state,
        "reasons": reasons,
        "desktop": "not_run",
        "semantic_execution": validation.get("semantic_execution", "not_run"),
    }
