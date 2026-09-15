"""Release-readiness scorecard built from corpus certification evidence."""

from __future__ import annotations

from typing import Any, Mapping


RUNTIME_GATES = ("desktop", "semantic_execution", "refresh", "deployment")


def build_release_readiness(
    certification: Mapping[str, Any],
    *,
    compatibility: Mapping[str, Any] | None = None,
    known_limitations: list[str] | None = None,
) -> dict[str, Any]:
    """Build a static release scorecard without hiding unresolved gates."""
    counts = certification.get("counts", {}) or {}
    corpus_count = int(certification.get("corpus_count", 0) or 0)
    blocked = int(counts.get("blocked", 0) or 0)
    review = int(counts.get("needs_review", 0) or 0)
    gates = {
        "corpus_present": {"status": "passed" if corpus_count else "failed"},
        "no_blocked_workbooks": {"status": "passed" if not blocked else "failed", "count": blocked},
        "no_review_workbooks": {"status": "passed" if not review else "warning", "count": review},
        "static_only_claim": {
            "status": "passed" if certification.get("release_claim") == "static_corpus_only" else "failed"
        },
    }
    for gate in RUNTIME_GATES:
        gates[f"runtime_{gate}"] = {"status": "not_run"}
    static_pass = all(gate["status"] == "passed" for gate in gates.values() if gate["status"] != "not_run")
    return {
        "schema_version": "1.0",
        "status": "ready_for_static_release" if static_pass else "needs_review",
        "release_claim": "static_corpus_only",
        "gates": gates,
        "compatibility": dict(compatibility or {}),
        "known_limitations": list(known_limitations or []),
        "corpus": {
            "count": corpus_count,
            "certified_static": int(counts.get("certified_static", 0) or 0),
            "needs_review": review,
            "blocked": blocked,
        },
    }
