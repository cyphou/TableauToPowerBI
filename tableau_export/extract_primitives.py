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


#: Marks-card encodings. Shared so the field extractor and the chart-type
#: inference cannot disagree about which shelves exist.
_MARK_ENCODINGS = ('color', 'size', 'shape', 'detail', 'tooltip', 'label', 'text')


def _read_filter_condition(filt):
    """Read what a ``<filter>`` actually keeps.

    Tableau never writes filter members as ``<value>`` text; they are the
    ``member`` attribute of a ``<groupfilter>``, and the parent's ``function``
    says whether they are kept, excluded or merely enumerated. Shared so the
    worksheet-level and workbook-level readers cannot disagree — they did, and
    the workbook-level list reported every one of the 167 corpus filters as
    having no values at all.

    Returns ``(type, values, min, max, exclude)``.
    """
    filter_type = ''
    values = []
    filter_min = None
    filter_max = None
    exclude_mode = False

    def _members(parent):
        return [gf.get('member', '').replace('&quot;', '"')
                for gf in parent.findall('.//groupfilter[@function="member"]')
                if gf.get('member')]

    groupfilter = filt.find('.//groupfilter')
    if groupfilter is not None:
        func = groupfilter.get('function', '')
        if func == 'member':
            filter_type = 'categorical'
            val = groupfilter.get('member', '')
            if val:
                values.append(val.replace('&quot;', '"'))
        elif func == 'union':
            filter_type = 'categorical'
            values.extend(_members(groupfilter))
        elif func == 'range':
            from_val = groupfilter.get('from', '')
            to_val = groupfilter.get('to', '')
            # Tableau also uses func="range" on text fields (from="A" to="Z")
            # to mean "everything", which is not a comparison filter.
            is_numeric = False
            for raw in (from_val, to_val):
                if raw:
                    try:
                        float(raw)
                        is_numeric = True
                    except (ValueError, TypeError):
                        pass
            if is_numeric:
                filter_type = 'range'
                filter_min = from_val or None
                filter_max = to_val or None
            else:
                filter_type = 'all'
        elif func == 'level-members':
            filter_type = 'all'  # every member selected
        elif func == 'crossjoin':
            filter_type = 'all'  # multi-field action filter
        elif func in ('except', 'not'):
            exclude_mode = True
            filter_type = 'categorical'
            values.extend(_members(groupfilter))

    for v in filt.findall('.//value'):
        if v.text:
            values.append(v.text)

    return filter_type, values, filter_min, filter_max, exclude_mode
