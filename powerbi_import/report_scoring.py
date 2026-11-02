"""Read optional migration classification scores without inventing evidence.

The legacy ``fidelity_score`` name denotes a static classification heuristic,
not visual, data-value, or runtime parity.  Some older report formats stored it
at the top level; current migration reports put it in ``summary``.
"""

import math
from typing import Optional


def percentage_or_none(value) -> Optional[float]:
    """Return a finite percentage, or None when no usable score was supplied.

    Numeric strings are accepted for older reports.  Booleans, non-finite
    values, and percentages outside 0--100 are not scores.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return score if math.isfinite(score) and 0 <= score <= 100 else None


def migration_classification_score(report) -> Optional[float]:
    """Read a report's static classification score, respecting missing evidence.

    An explicitly unscored current summary takes precedence over any legacy
    top-level score.  A score with no classified items, or one that would
    hide an unverified visual, cannot stand for a workbook-wide score.
    """
    if not isinstance(report, dict):
        return None

    if any(isinstance(item, dict) and item.get("category") == "visual"
           and item.get("status") in ("unverified", "skipped")
           for item in (report.get("items") or [])):
        return None

    summary = report.get("summary")
    if isinstance(summary, dict):
        total = summary.get("total_items")
        skipped = summary.get("skipped", 0)
        unverified = summary.get("unverified", 0)
        if isinstance(total, int) and not isinstance(total, bool) and total == 0:
            return None
        counts = (total, skipped, unverified)
        if all(isinstance(n, int) and not isinstance(n, bool) and n >= 0
               for n in counts):
            if skipped + unverified >= total:
                return None

        by_category = summary.get("by_category") or {}
        visual = by_category.get("visual", {}) if isinstance(by_category, dict) else {}
        if isinstance(visual, dict) and (visual.get("unverified") or visual.get("skipped")):
            return None

        if "fidelity_score" in summary:
            return percentage_or_none(summary["fidelity_score"])

    for key in ("fidelity_score", "overall_fidelity"):
        if key in report:
            return percentage_or_none(report[key])
    return None
