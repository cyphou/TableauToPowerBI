"""Sprint 143 — DAX expression validator (lightweight).

Phase 3 conversion guardrail utility used to verify generated DAX strings
before they are emitted into model artifacts.
"""

import re
from typing import List


__all__ = ['validate_dax_expression', 'DaxExpressionValidator']


_TABLEAU_FUNC_LEAK = re.compile(
    r'\b(DATETRUNC|DATEPART|IFNULL|ISNULL|COUNTD'
    r'|WINDOW_SUM|WINDOW_AVG|WINDOW_MAX|WINDOW_MIN|WINDOW_COUNT'
    r'|RUNNING_SUM|RUNNING_AVG|RUNNING_COUNT|RUNNING_MAX|RUNNING_MIN'
    r'|RANK_UNIQUE|RANK_DENSE|RANK_MODIFIED|RANK_PERCENTILE'
    r'|LOOKUP|PREVIOUS_VALUE)\b',
    re.IGNORECASE,
)


def _check_balanced(expr: str) -> List[str]:
    issues = []
    pairs = {')': '(', '}': '{'}
    openers = set(pairs.values())
    stack = []
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        # Skip double-quoted string literals — brackets/parens inside are data.
        if ch == '"':
            i += 1
            while i < n:
                if expr[i] == '"' and i + 1 < n and expr[i + 1] == '"':
                    i += 2
                    continue
                if expr[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        # Skip single-quoted table identifiers.
        if ch == "'":
            i += 1
            while i < n:
                if expr[i] == "'" and i + 1 < n and expr[i + 1] == "'":
                    i += 2
                    continue
                if expr[i] == "'":
                    i += 1
                    break
                i += 1
            continue
        # Treat [Col name] bracketed identifiers as atomic, self-balancing
        # spans — parens inside a name (e.g. [Show % (Indicateur Nat)]) are
        # literal characters, not grouping operators.
        if ch == '[':
            start = i
            i += 1
            closed = False
            while i < n:
                if expr[i] == ']' and i + 1 < n and expr[i + 1] == ']':
                    i += 2
                    continue
                if expr[i] == ']':
                    i += 1
                    closed = True
                    break
                i += 1
            if not closed:
                issues.append(f'unmatched opening "[" at pos {start}')
            continue
        if ch in openers:
            stack.append((ch, i))
        elif ch in pairs:
            if not stack:
                issues.append(f'unmatched closing "{ch}" at pos {i}')
            elif stack[-1][0] != pairs[ch]:
                issues.append(
                    f'mismatched bracket at pos {i}: expected close for "{stack[-1][0]}" but got "{ch}"'
                )
                stack.pop()
            else:
                stack.pop()
        i += 1
    for ch, idx in stack:
        issues.append(f'unmatched opening "{ch}" at pos {idx}')
    return issues


def _check_quotes(expr: str) -> List[str]:
    issues = []
    in_double = False
    in_single = False
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        # Bracketed identifiers [Col name] are opaque: apostrophes and double
        # quotes inside a column/measure name (e.g. [% ou Nombre d 'appel] or
        # [Sales "USD"]) are literal characters, not quote delimiters. Skip the
        # whole bracket span (DAX escapes a literal ] as ]]).
        if ch == '[' and not in_double and not in_single:
            i += 1
            while i < n:
                if expr[i] == ']' and i + 1 < n and expr[i + 1] == ']':
                    i += 2
                    continue
                if expr[i] == ']':
                    i += 1
                    break
                i += 1
            continue
        if ch == '"' and not in_single:
            # DAX escapes quotes with doubled ""
            if in_double and i + 1 < n and expr[i + 1] == '"':
                i += 2
                continue
            in_double = not in_double
            i += 1
            continue
        if ch == "'" and not in_double:
            # DAX table quoting escapes single quote with doubled ''
            if in_single and i + 1 < n and expr[i + 1] == "'":
                i += 2
                continue
            in_single = not in_single
            i += 1
            continue
        i += 1
    if in_double:
        issues.append('unterminated double-quoted string literal')
    if in_single:
        issues.append('unterminated single-quoted identifier')
    return issues


def validate_dax_expression(expr: str) -> List[str]:
    """Validate a generated DAX expression.

    Returns a list of issue messages. Empty list means valid enough to emit.
    """
    if not expr or not str(expr).strip():
        return ['empty DAX expression']

    text = str(expr)
    issues: List[str] = []
    issues.extend(_check_quotes(text))
    issues.extend(_check_balanced(text))

    if _TABLEAU_FUNC_LEAK.search(text):
        issues.append('unconverted Tableau function token detected in DAX output')

    if re.search(r'\b(None|undefined)\b', text, re.IGNORECASE):
        issues.append('invalid literal token detected in DAX output')

    if '/*' in text and '*/' not in text:
        issues.append('unterminated block comment')

    return issues


class DaxExpressionValidator:
    """Class wrapper for compatibility with validator patterns."""

    @staticmethod
    def validate_expression(expr: str) -> List[str]:
        return validate_dax_expression(expr)
