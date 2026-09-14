"""Confidence-scored DAX self-healing (v43/v44, Sprint 210.3).

A set of small, idempotent, span-aware healers that repair the most common
DAX defects produced during Tableau → Power BI conversion. Every heal records a
category, confidence, and before/after so it can be surfaced in the unified
healing/recovery report and reviewed.

Design principles:
    * Idempotent — running a healer twice yields no further change.
    * Non-degrading — a healer never alters already-valid DAX.
    * Span-aware — parens/commas/operators inside ``[bracketed ids]``, ``"strings"``
      and ``'table quotes'`` are literal and never touched (DAX escapes ``]]`` /
      ``""`` / ``''``).

Public API:
    heal_dax(dax, measure_names=None) -> HealReport
    heal_balance_parens / heal_trailing_comma / heal_double_equals /
    heal_sum_of_measure / heal_balance_brackets  (individual healers)
"""

from __future__ import annotations

import re
from typing import List, Optional, Set, Tuple

# Shared healing contract (canonical home is healing_core). Re-exported here for
# backward compatibility — existing code imports these from dax_healing.
from powerbi_import.healing_core import (  # noqa: F401
    HealAction, HealReport, HIGH, MEDIUM, LOW,
)


# ════════════════════════════════════════════════════════════════════
#  Span-aware scanning
# ════════════════════════════════════════════════════════════════════

def _spans(expr: str) -> List[Tuple[int, int]]:
    """Return [start, end) index ranges of opaque spans: [..], "..", '..'.

    Handles DAX escapes: ``]]`` inside a bracket, ``""`` inside a string, ``''``
    inside a table-quote.
    """
    spans: List[Tuple[int, int]] = []
    i, n = 0, len(expr)
    while i < n:
        ch = expr[i]
        if ch == '[':
            start = i
            i += 1
            while i < n:
                if expr[i] == ']' and i + 1 < n and expr[i + 1] == ']':
                    i += 2
                    continue
                if expr[i] == ']':
                    i += 1
                    break
                i += 1
            spans.append((start, i))
            continue
        if ch == '"':
            start = i
            i += 1
            while i < n:
                if expr[i] == '"' and i + 1 < n and expr[i + 1] == '"':
                    i += 2
                    continue
                if expr[i] == '"':
                    i += 1
                    break
                i += 1
            spans.append((start, i))
            continue
        if ch == "'":
            start = i
            i += 1
            while i < n:
                if expr[i] == "'" and i + 1 < n and expr[i + 1] == "'":
                    i += 2
                    continue
                if expr[i] == "'":
                    i += 1
                    break
                i += 1
            spans.append((start, i))
            continue
        i += 1
    return spans


def _in_span(idx: int, spans: List[Tuple[int, int]]) -> bool:
    for s, e in spans:
        if s <= idx < e:
            return True
        if idx < s:
            break
    return False


def _paren_profile(expr: str):
    """Return (net, went_negative) for parens outside opaque spans."""
    spans = _spans(expr)
    depth = 0
    went_negative = False
    for i, ch in enumerate(expr):
        if ch not in "()":
            continue
        if _in_span(i, spans):
            continue
        if ch == "(":
            depth += 1
        else:
            depth -= 1
            if depth < 0:
                went_negative = True
    return depth, went_negative


# ════════════════════════════════════════════════════════════════════
#  Individual healers — each returns (new_text, Optional[HealAction])
# ════════════════════════════════════════════════════════════════════

def heal_balance_parens(dax: str) -> Tuple[str, Optional[HealAction]]:
    """Append missing closing parens when the top-level paren count is short.

    Only fires when close-count never exceeds open-count mid-expression (a clean
    trailing shortfall), which is safely fixable. A close-before-open imbalance
    is left untouched (recorded elsewhere) to avoid corrupting the expression.
    """
    net, went_negative = _paren_profile(dax)
    if net <= 0 or went_negative:
        return dax, None
    healed = dax + (")" * net)
    conf = HIGH if net == 1 else MEDIUM
    return healed, HealAction(
        "balance_parens", "syntax", conf, dax, healed,
        f"appended {net} closing paren(s)")


def heal_balance_brackets(dax: str) -> Tuple[str, Optional[HealAction]]:
    """Close a single unterminated ``[bracketed identifier]`` at the tail.

    Conservative: only fixes exactly one unclosed ``[`` and only when it is the
    last ``[`` with no ``]`` after it.
    """
    # Find an unclosed '[' by scanning spans; an unterminated bracket produces a
    # span that runs to end-of-string with no closing ']'.
    i, n = 0, len(dax)
    unclosed_at = -1
    while i < n:
        ch = dax[i]
        if ch == '[':
            start = i
            i += 1
            closed = False
            while i < n:
                if dax[i] == ']' and i + 1 < n and dax[i + 1] == ']':
                    i += 2
                    continue
                if dax[i] == ']':
                    i += 1
                    closed = True
                    break
                i += 1
            if not closed:
                unclosed_at = start
            continue
        if ch in ('"', "'"):
            # skip strings/table-quotes
            q = ch
            i += 1
            while i < n:
                if dax[i] == q and i + 1 < n and dax[i + 1] == q:
                    i += 2
                    continue
                if dax[i] == q:
                    i += 1
                    break
                i += 1
            continue
        i += 1
    if unclosed_at == -1:
        return dax, None
    healed = dax + "]"
    return healed, HealAction(
        "balance_brackets", "syntax", MEDIUM, dax, healed,
        "closed an unterminated bracketed identifier")


def heal_trailing_comma(dax: str) -> Tuple[str, Optional[HealAction]]:
    """Remove dangling commas before a close-paren or end-of-expression."""
    spans = _spans(dax)
    out = []
    changed = False
    i, n = 0, len(dax)
    while i < n:
        ch = dax[i]
        if ch == "," and not _in_span(i, spans):
            # look ahead past whitespace
            j = i + 1
            while j < n and dax[j] in " \t\r\n":
                j += 1
            if j >= n or dax[j] == ")":
                # drop this comma (keep following whitespace)
                changed = True
                i += 1
                continue
        out.append(ch)
        i += 1
    if not changed:
        return dax, None
    healed = "".join(out)
    return healed, HealAction(
        "trailing_comma", "syntax", HIGH, dax, healed,
        "removed dangling comma before ')' or end")


def heal_double_equals(dax: str) -> Tuple[str, Optional[HealAction]]:
    """Convert Tableau ``==`` equality to DAX ``=`` outside opaque spans.

    Leaves ``<=``, ``>=``, ``<>`` untouched.
    """
    spans = _spans(dax)
    out = []
    i, n = 0, len(dax)
    changed = False
    while i < n:
        if (dax[i] == "=" and i + 1 < n and dax[i + 1] == "="
                and not _in_span(i, spans)
                and (i == 0 or dax[i - 1] not in "<>!=")):
            out.append("=")
            i += 2
            changed = True
            continue
        out.append(dax[i])
        i += 1
    if not changed:
        return dax, None
    healed = "".join(out)
    return healed, HealAction(
        "double_equals", "syntax", HIGH, dax, healed,
        "converted Tableau '==' to DAX '='")


_SUM_OF_MEASURE_RE = re.compile(
    r"\b(SUM|AVERAGE|MIN|MAX|COUNT)\s*\(\s*(\[[^\]]+\])\s*\)", re.IGNORECASE)


def heal_sum_of_measure(dax: str, measure_names: Optional[Set[str]] = None
                        ) -> Tuple[str, Optional[HealAction]]:
    """Unwrap ``SUM([Measure])`` → ``[Measure]`` when the arg is a measure.

    Aggregating a measure is invalid DAX. Only fires when the bracketed name is a
    known measure (case-insensitive).
    """
    if not measure_names:
        return dax, None
    lowered = {m.strip("[]").lower() for m in measure_names}

    def repl(m: re.Match) -> str:
        inner = m.group(2)
        name = inner.strip("[]").lower()
        if name in lowered:
            return inner
        return m.group(0)

    healed = _SUM_OF_MEASURE_RE.sub(repl, dax)
    if healed == dax:
        return dax, None
    return healed, HealAction(
        "sum_of_measure", "aggregation", HIGH, dax, healed,
        "unwrapped aggregation of a measure")


# ════════════════════════════════════════════════════════════════════
#  Orchestrator
# ════════════════════════════════════════════════════════════════════

# Order matters: fix operators/commas first, then balance delimiters last.
def heal_dax(dax: str, measure_names: Optional[Set[str]] = None) -> HealReport:
    """Apply all healers idempotently and return a :class:`HealReport`."""
    original = dax
    current = dax
    actions: List[HealAction] = []

    def _apply(fn, *args):
        nonlocal current
        new, action = fn(current, *args)
        if action is not None and new != current:
            # Re-anchor before/after to the incremental step.
            action.before = current
            action.after = new
            actions.append(action)
            current = new

    _apply(heal_double_equals)
    _apply(heal_sum_of_measure, measure_names)
    _apply(heal_trailing_comma)
    _apply(heal_balance_brackets)
    _apply(heal_balance_parens)

    return HealReport(original=original, healed=current, actions=actions)


def heal_measures(measures: List[dict], measure_names: Optional[Set[str]] = None
                  ) -> List[HealReport]:
    """Heal a list of measure dicts (each with an ``expression`` key).

    Returns a HealReport per measure that changed; unchanged measures are skipped.
    """
    names = measure_names or {m.get("name", "") for m in measures}
    reports = []
    for m in measures:
        expr = m.get("expression", "")
        if not isinstance(expr, str) or not expr:
            continue
        report = heal_dax(expr, names)
        if report.changed:
            reports.append(report)
    return reports
