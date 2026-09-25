"""Leaf helpers shared by the Tableau extractors.

Field-reference cleaning, bracket stripping, safe int coercion and the
line-break sentinel handling. Nothing here knows about ``TableauExtractor``,
which is the point: per-type extractor modules import these without importing
``extract_tableau_data`` back.

Owned by **@extractor**.
"""

import re


def _safe_int(val, default=0):
    """Safely convert a value to int, handling floats and non-numeric strings."""
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


# ── Pre-compiled shared regex patterns ────────────────────────────────────────

_RE_FIELD_REF = re.compile(r'\[([^\]]+)\]\.\[([^\]]+)\]')
_RE_DERIVATION_PREFIX = re.compile(
    r'^(none|sum|avg|count|cnt|ctd|countd|min|max|usr|yr|mn|dy|qr|wk|attr|md|mdy|hms|hr|mt|sc|thr|trunc|tyr|tqr|tmn|tdy|twk):'
)
_RE_TABLE_CALC_PREFIX = re.compile(
    r'^(pcto|pctd|diff|running_sum|running_avg|running_count|running_min|running_max|rank|rank_unique|rank_dense):(sum|avg|count|min|max|countd)?:?'
)
_RE_TYPE_SUFFIX = re.compile(r':(nk|qk|ok|fn|tn)$')

def _clean_field_ref(raw):
    """Strip Tableau derivation prefixes, table calc prefixes, and type suffixes.

    Handles patterns like ``yr:Order Date:ok`` → ``Order Date``,
    ``none:Ship Mode:nk`` → ``Ship Mode``, ``tyr:Date:qk`` → ``Date``,
    ``pcto:sum:Sales:nk`` → ``Sales``.
    """
    clean = _RE_DERIVATION_PREFIX.sub('', raw)
    clean = _RE_TYPE_SUFFIX.sub('', clean)
    clean = _RE_TABLE_CALC_PREFIX.sub('', clean)
    return clean


def _strip_brackets(s):
    """Remove Tableau bracket notation from a field/table name."""
    return s.replace('[', '').replace(']', '')


# Tableau line-break sentinel char (Æ or non-breaking space) optionally
# surrounded by whitespace, AND the run carries no font/style attributes.
_TABLEAU_LB_SENTINEL_RE = re.compile(r'^[\s]*[\u00c6\u00a0]+[\s]*$')
_TABLEAU_RUN_STYLE_ATTRS = (
    'fontname', 'fontsize', 'fontcolor', 'color',
    'bold', 'italic', 'underline', 'href', 'url',
    'font-family', 'font-size', 'font-color',
    'fontstyle', 'fontweight',
)


def _clean_tableau_run_text(run_elem):
    """Return run text with Tableau line-break sentinel artifacts stripped.

    If a ``<run>`` has no font/style attributes and its text consists solely
    of Tableau's line-break sentinel character(s) (``Æ`` U+00C6 or
    non-breaking space U+00A0) plus surrounding whitespace, the sentinel
    characters are dropped (newlines are preserved so paragraph splitting
    still works downstream). Styled runs and runs with mixed content are
    returned unchanged.
    """
    text = run_elem.text or ''
    if not text or not _TABLEAU_LB_SENTINEL_RE.match(text):
        return text
    if any(run_elem.get(a) for a in _TABLEAU_RUN_STYLE_ATTRS):
        return text
    return text.replace('\u00c6', '').replace('\u00a0', '')
