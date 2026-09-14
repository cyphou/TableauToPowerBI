"""Interactivity fidelity — Tableau filters, actions, parameters, and
Row-Level Security vs the generated PBIR/TMDL interactive surface.

Unlike ``visual_size_diff.py`` (per-visual geometry) and ``powerquery_diff.py``
(per-table data coverage), interactivity is spread across several independent
PBI mechanisms (report/page/visual filters, action-button visuals, What-If
parameter tables, RLS roles), so this module compares AGGREGATE COUNTS per
category rather than a 1:1 structural match.

Public API:
    compare_report_interface(extracted, project_dir, report_name) -> dict
"""

from __future__ import annotations

import glob
import json
import os
import re
from typing import Dict, List

from powerbi_import.artifact_diff import _parse_tmdl_table

# Only these Tableau action types become a dedicated actionButton visual;
# filter/highlight actions map to native PBI cross-filtering (no new visual).
_BUTTON_ACTION_TYPES = ('url', 'sheet-navigate', 'navigate')

_ROLE_RE = re.compile(r"^role\s+'?(.*?)'?\s*$")


def _load_json(path: str):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _count_generated_filter_configs(report_dir: str) -> Dict[str, int]:
    """Count filterConfig entries at each of the three PBIR filter scopes."""
    counts = {'report': 0, 'page': 0, 'visual': 0}
    report_json = _load_json(os.path.join(report_dir, 'definition', 'report.json')) or {}
    counts['report'] = len((report_json.get('filterConfig') or {}).get('filters', []))
    for page_path in glob.glob(os.path.join(report_dir, 'definition', 'pages', '*', 'page.json')):
        page_json = _load_json(page_path) or {}
        counts['page'] += len((page_json.get('filterConfig') or {}).get('filters', []))
    for visual_path in glob.glob(os.path.join(
            report_dir, 'definition', 'pages', '*', 'visuals', '*', 'visual.json')):
        visual_json = _load_json(visual_path) or {}
        counts['visual'] += len((visual_json.get('filterConfig') or {}).get('filters', []))
    return counts


def _count_generated_slicers(report_dir: str) -> int:
    """Visible Tableau quick filters become dedicated slicer visuals, not
    filterConfig entries — count them as a second, equally valid form of
    generated filter coverage."""
    count = 0
    for visual_path in glob.glob(os.path.join(
            report_dir, 'definition', 'pages', '*', 'visuals', '*', 'visual.json')):
        visual_json = _load_json(visual_path) or {}
        if (visual_json.get('visual') or {}).get('visualType') == 'slicer':
            count += 1
    return count


def _count_generated_action_buttons(report_dir: str) -> int:
    count = 0
    for visual_path in glob.glob(os.path.join(
            report_dir, 'definition', 'pages', '*', 'visuals', '*', 'visual.json')):
        visual_json = _load_json(visual_path) or {}
        if (visual_json.get('visual') or {}).get('visualType') == 'actionButton':
            count += 1
    return count


def _load_generated_symbols(semantic_model_dir: str):
    """Return (table_names_lower, measure_names_lower) across every TMDL table."""
    table_names = set()
    measure_names = set()
    pattern = os.path.join(semantic_model_dir, 'definition', 'tables', '*.tmdl')
    for path in sorted(glob.glob(pattern)):
        parsed = _parse_tmdl_table(path)
        if not parsed:
            continue
        table_names.add(parsed['name'].lower())
        measure_names.update(m['name'].lower() for m in parsed.get('measures', []))
    return table_names, measure_names


def _count_generated_parameter_coverage(semantic_model_dir: str,
                                        parameter_captions: List[str]) -> int:
    """A parameter is covered by either a dedicated What-If table (range/list
    domain) or a plain measure on the main table (any-domain parameters)."""
    table_names, measure_names = _load_generated_symbols(semantic_model_dir)
    covered = 0
    for caption in parameter_captions:
        key = (caption or '').lower()
        if key and (key in table_names or key in measure_names):
            covered += 1
    return covered


def _count_generated_roles(semantic_model_dir: str) -> int:
    roles_path = os.path.join(semantic_model_dir, 'definition', 'roles.tmdl')
    if not os.path.isfile(roles_path):
        return 0
    try:
        with open(roles_path, 'r', encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if _ROLE_RE.match(line.rstrip()))


def _dashboard_worksheet_names(extracted: Dict) -> set:
    """Worksheet names actually placed on a dashboard page (i.e. that produce
    a generated visual). Worksheets never placed on any dashboard don't map
    to any PBI artifact and must not inflate the expected filter count."""
    dashboards = extracted.get('dashboards', [])
    if isinstance(dashboards, dict):
        dashboards = dashboards.get('dashboards', [])
    names = set()
    for db in dashboards:
        for obj in db.get('objects', []):
            if obj.get('type') == 'worksheetReference':
                name = obj.get('worksheetName', '')
                if name:
                    names.add(name)
    return names


def _expected_filter_count(extracted: Dict) -> int:
    """Sum per-worksheet filters for worksheets actually shown on a dashboard.

    Deliberately excludes the workbook-wide ``filters`` extraction: it scans
    the same ``<filter>`` XML elements already captured per-worksheet, so
    summing both would double-count the same source filters.

    Also excludes parameter-driven worksheet filters (``datasource ==
    "Parameters"``) — these don't produce a distinct PBI filter/slicer
    object; the parameter's value is instead baked directly into the
    referencing measures' DAX conditional logic (e.g. ``IF([Param]=...)``),
    so counting them as "missing" filter coverage is a false positive.

    Also excludes Tableau dashboard-action filter placeholders (fields
    named ``Action (...)``) — these are internal artifacts of a filter/
    highlight action between worksheets on the same dashboard. Power BI
    reproduces the same behaviour natively via automatic cross-filtering
    between visuals on a page, so no distinct filter/slicer object is ever
    generated (or needed) for them.

    Also excludes the Tableau-internal ``:Measure Names`` filter — it
    selects WHICH measures appear on a combo worksheet, not which rows are
    included. That selection is already reflected structurally in the
    generated visual's field list, so no separate PBI filter object is
    expected for it either.

    Also excludes non-restrictive filter-shelf placeholders: Tableau
    records a filter entry for any field dragged onto the Filters shelf
    even when its domain is left at "All values" (``type == "all"``) with
    no explicit values/min/max. Such an entry restricts nothing, so it has
    no PBI counterpart to check for.
    """
    worksheets = extracted.get('worksheets', [])
    if isinstance(worksheets, dict):
        worksheets = worksheets.get('worksheets', [])
    on_dashboard = _dashboard_worksheet_names(extracted)
    total = 0
    for ws in worksheets:
        if ws.get('name') not in on_dashboard:
            continue
        for f in ws.get('filters', []) or []:
            if f.get('datasource') == 'Parameters':
                continue
            if str(f.get('field', '')).startswith('Action ('):
                continue
            if f.get('field') == ':Measure Names':
                continue
            if (f.get('type') == 'all' and not f.get('values')
                    and f.get('min') is None and f.get('max') is None):
                continue
            total += 1
    return total


def compare_report_interface(extracted: Dict, project_dir: str, report_name: str) -> Dict:
    """Compare Tableau filters/actions/parameters/RLS to the generated PBI surface."""
    report_dir = os.path.join(project_dir, f'{report_name}.Report')
    semantic_model_dir = os.path.join(project_dir, f'{report_name}.SemanticModel')

    expected_filter_count = _expected_filter_count(extracted)
    filter_config_counts = _count_generated_filter_configs(report_dir)
    generated_slicers = _count_generated_slicers(report_dir)
    generated_filter_total = sum(filter_config_counts.values()) + generated_slicers

    actions = extracted.get('actions', [])
    if isinstance(actions, dict):
        actions = actions.get('actions', [])
    expected_action_buttons = sum(1 for a in actions if a.get('type') in _BUTTON_ACTION_TYPES)
    generated_action_buttons = _count_generated_action_buttons(report_dir)

    parameters = extracted.get('parameters', [])
    if isinstance(parameters, dict):
        parameters = parameters.get('parameters', [])
    parameter_captions = [p.get('caption') or p.get('name', '') for p in parameters]
    generated_parameter_coverage = _count_generated_parameter_coverage(
        semantic_model_dir, parameter_captions)

    user_filters = extracted.get('user_filters', [])
    if isinstance(user_filters, dict):
        user_filters = user_filters.get('user_filters', [])
    generated_roles = _count_generated_roles(semantic_model_dir)

    return {
        'report_name': report_name,
        'filters': {
            'expected': expected_filter_count,
            'generated_filter_configs': filter_config_counts,
            'generated_slicers': generated_slicers,
            'generated_total': generated_filter_total,
            'covered': generated_filter_total >= expected_filter_count,
        },
        'actions': {
            'expected_buttons': expected_action_buttons,
            'generated_buttons': generated_action_buttons,
            'covered': generated_action_buttons >= expected_action_buttons,
        },
        'parameters': {
            'expected': len(parameters),
            'generated_coverage': generated_parameter_coverage,
            'covered': generated_parameter_coverage >= len(parameters),
        },
        'rls': {
            # user_filters is a lower bound: ISMEMBEROF("group") calculations
            # expand into one role per group and aren't counted here.
            'expected_at_least': len(user_filters),
            'generated_roles': generated_roles,
            'covered': generated_roles >= len(user_filters),
        },
    }

