"""Confidence-scored Power Query M self-healing (v43/v44, Sprint 210.4).

Companion to ``dax_healing`` for the M / Power Query side. Small, idempotent,
span-aware healers that repair the most common M defects produced during
Tableau → Power BI conversion and step injection.

Design principles (shared with ``dax_healing``):
    * Idempotent — running a healer twice yields no further change.
    * Non-degrading — a healer never alters already-valid M.
    * Span-aware — parens/commas inside ``"strings"``, ``[field access]``, and
      ``//`` / ``/* */`` comments are literal and never touched.

Public API:
    heal_m(m) -> HealReport
    heal_quote_identifiers / heal_balance_parens / heal_trailing_comma
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from powerbi_import.healing_core import HealAction, HealReport, HIGH, MEDIUM, LOW

# Reuse the project's canonical M identifier-quoting logic for consistency.
try:
    from calc_column_utils import _quote_m_ids  # type: ignore
except ImportError:  # pragma: no cover - path fallback
    from powerbi_import.calc_column_utils import _quote_m_ids


# ════════════════════════════════════════════════════════════════════
#  Span-aware scanning (M dialect)
# ════════════════════════════════════════════════════════════════════

def _m_spans(expr: str) -> List[Tuple[int, int]]:
    """Return [start, end) ranges of opaque M spans.

    Opaque spans: ``"strings"`` (``""`` escape), ``[field access]``,
    ``// line comments``, and ``/* block comments */``.
    """
    spans: List[Tuple[int, int]] = []
    i, n = 0, len(expr)
    while i < n:
        ch = expr[i]
        two = expr[i:i + 2]
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
        if two == "//":
            start = i
            while i < n and expr[i] not in "\r\n":
                i += 1
            spans.append((start, i))
            continue
        if two == "/*":
            start = i
            i += 2
            while i < n and expr[i:i + 2] != "*/":
                i += 1
            i = min(n, i + 2)
            spans.append((start, i))
            continue
        if ch == "[":
            start = i
            i += 1
            while i < n and expr[i] != "]":
                i += 1
            if i < n:
                i += 1  # include the closing ]
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
    spans = _m_spans(expr)
    depth = 0
    went_negative = False
    for i, ch in enumerate(expr):
        if ch not in "()":
            continue
        if _in_span(i, spans):
            continue
        depth += 1 if ch == "(" else -1
        if depth < 0:
            went_negative = True
    return depth, went_negative


# ════════════════════════════════════════════════════════════════════
#  Individual healers
# ════════════════════════════════════════════════════════════════════

def heal_quote_identifiers(m: str) -> Tuple[str, Optional[HealAction]]:
    """Quote ``[field]`` refs with special chars as ``[#"field"]`` (M rule)."""
    healed = _quote_m_ids(m)
    if healed == m:
        return m, None
    return healed, HealAction(
        "quote_identifiers", "m_syntax", HIGH, m, healed,
        "quoted field identifier(s) containing special characters")


def heal_balance_parens(m: str) -> Tuple[str, Optional[HealAction]]:
    """Append missing closing parens on a clean trailing shortfall."""
    net, went_negative = _paren_profile(m)
    if net <= 0 or went_negative:
        return m, None
    healed = m + (")" * net)
    conf = HIGH if net == 1 else MEDIUM
    return healed, HealAction(
        "balance_parens", "m_syntax", conf, m, healed,
        f"appended {net} closing paren(s)")


def heal_trailing_comma(m: str) -> Tuple[str, Optional[HealAction]]:
    """Remove dangling commas before ``)``, the ``in`` keyword, or end.

    In a ``let ... in`` block a comma before ``in`` (``let a=1, in a``) is a
    syntax error; likewise a comma before a close paren or end of expression.
    """
    spans = _m_spans(m)
    out = []
    changed = False
    i, n = 0, len(m)
    while i < n:
        ch = m[i]
        if ch == "," and not _in_span(i, spans):
            j = i + 1
            while j < n and m[j] in " \t\r\n":
                j += 1
            nxt = m[j] if j < n else ""
            is_in_kw = (m[j:j + 2] == "in"
                        and (j + 2 >= n or m[j + 2] in " \t\r\n"))
            if j >= n or nxt == ")" or is_in_kw:
                changed = True
                i += 1
                continue
        out.append(ch)
        i += 1
    if not changed:
        return m, None
    healed = "".join(out)
    return healed, HealAction(
        "trailing_comma", "m_syntax", HIGH, m, healed,
        "removed dangling comma before ')', 'in', or end")


# ════════════════════════════════════════════════════════════════════
#  Orchestrator
# ════════════════════════════════════════════════════════════════════

def heal_m(m: str) -> HealReport:
    """Apply all M healers idempotently and return a :class:`HealReport`."""
    original = m
    current = m
    actions: List[HealAction] = []

    def _apply(fn):
        nonlocal current
        new, action = fn(current)
        if action is not None and new != current:
            action.before = current
            action.after = new
            actions.append(action)
            current = new

    _apply(heal_quote_identifiers)
    _apply(heal_trailing_comma)
    _apply(heal_balance_parens)

    return HealReport(original=original, healed=current, actions=actions)
