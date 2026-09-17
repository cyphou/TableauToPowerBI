"""Visual size fidelity comparison — Tableau zone size vs generated PBIR size.

For every migrated dashboard/page, recomputes the *expected* generated visual
width/height directly from the extracted Tableau zone geometry (reusing the
same grid-layout and scale-factor rules as ``pbip_generator.py``) and compares
it against the *actual* size written into each ``visual.json`` on disk.

Only width/height are compared (not x/y), since position-only adjustments
made after layout (e.g. floating-overlap staggering) never change a visual's
size — a real size mismatch always indicates a scale/clamp regression.

Public API:
    compare_dashboard_visual_sizes(dashboard, report_dir) -> dict
    compare_report_visual_sizes(extracted, project_dir, report_name) -> dict
"""

from __future__ import annotations

import glob
import json
import os
from typing import Dict, List, Optional

from powerbi_import.pbip_generator import PowerBIProjectGenerator

_SIZE_TOLERANCE_PX = 2


def _load_json(path: str) -> Optional[Dict]:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _page_dir_for_dashboard(report_dir: str, dashboard_name: str) -> Optional[str]:
    """Find the generated page directory whose displayName matches a dashboard."""
    pages_root = os.path.join(report_dir, 'definition', 'pages')
    if not os.path.isdir(pages_root):
        return None
    for entry in sorted(os.listdir(pages_root)):
        page_dir = os.path.join(pages_root, entry)
        page_json = _load_json(os.path.join(page_dir, 'page.json'))
        if page_json and page_json.get('displayName') == dashboard_name:
            return page_dir
    return None


def _actual_visual_sizes(page_dir: str) -> List[Dict]:
    """Read width/height of every generated visual on a page."""
    sizes = []
    pattern = os.path.join(page_dir, 'visuals', '*', 'visual.json')
    for vfile in sorted(glob.glob(pattern)):
        data = _load_json(vfile)
        if not data:
            continue
        pos = data.get('position', {})
        w, h = pos.get('width'), pos.get('height')
        if w is not None and h is not None:
            sizes.append({
                'path': vfile,
                'width': w,
                'height': h,
                'visual_type': data.get('visual', {}).get('visualType', ''),
            })
    return sizes


def _expected_object_sizes(dashboard: Dict):
    """Recompute the expected generated size for every sizable dashboard object.

    Reuses ``PowerBIProjectGenerator``'s own grid-layout and scale-factor
    logic (pure geometry, no I/O) so the comparison stays in sync with the
    real generator rules instead of a hand-duplicated formula.
    """
    generator = PowerBIProjectGenerator.__new__(PowerBIProjectGenerator)
    size = dashboard.get('size', {})
    page_width = size.get('width', 1280)
    page_height = size.get('height', 720)
    generator._current_page_width = page_width
    generator._current_page_height = page_height

    zone_hierarchy = dashboard.get('zone_hierarchy', {})
    layout_map = generator._build_zone_layout_map(zone_hierarchy, page_width, page_height)

    db_objects = dashboard.get('objects', [])
    max_x = max((o.get('position', {}).get('x', 0) + o.get('position', {}).get('w', 0)
                for o in db_objects), default=page_width)
    max_y = max((o.get('position', {}).get('y', 0) + o.get('position', {}).get('h', 0)
                for o in db_objects), default=page_height)
    scale_x = min(1.0, page_width / max(max_x, 1))
    scale_y = min(1.0, page_height / max(max_y, 1))

    expected = []
    for obj in db_objects:
        obj_type = obj.get('type', '')
        # Only these object types get a first-class content visual sized via
        # _make_visual_position; actions/slicers derived from them are not
        # part of the source zone geometry and are excluded here.
        if obj_type not in ('worksheetReference', 'text', 'image',
                            'filter_control', 'parameter_control'):
            continue
        obj_name = obj.get('worksheetName', '') or obj.get('name', '') or obj.get('param_name', '')
        pos = obj.get('position', {})
        if obj_type not in ('filter_control', 'parameter_control') and obj_name in layout_map:
            eff_pos, eff_sx, eff_sy = layout_map[obj_name], 1.0, 1.0
        else:
            eff_pos, eff_sx, eff_sy = pos, scale_x, scale_y
        made = generator._make_visual_position(
            eff_pos, eff_sx, eff_sy, 0,
            page_width=page_width, page_height=page_height)
        expected.append({
            'name': obj_name or obj_type,
            'type': obj_type,
            'width': made['width'],
            'height': made['height'],
        })
    return expected, page_width, page_height


def compare_dashboard_visual_sizes(dashboard: Dict, report_dir: str,
                                   tolerance: int = _SIZE_TOLERANCE_PX) -> Dict:
    """Compare expected vs actual visual sizes for one migrated dashboard/page."""
    dashboard_name = dashboard.get('name', '')
    expected, _page_width, _page_height = _expected_object_sizes(dashboard)
    page_dir = _page_dir_for_dashboard(report_dir, dashboard_name)

    result = {
        'page': dashboard_name,
        'canvas': {
            'tableau': {'width': dashboard.get('size', {}).get('width'),
                       'height': dashboard.get('size', {}).get('height')},
            'powerbi': None,
            'matched': False,
        },
        'expected_count': len(expected),
        'actual_count': 0,
        'matched': [],
        'mismatched': [],
        'page_found': page_dir is not None,
    }
    if page_dir is None:
        result['mismatched'] = [dict(e, reason='page not found') for e in expected]
        return result

    page_json = _load_json(os.path.join(page_dir, 'page.json')) or {}
    pbi_w, pbi_h = page_json.get('width'), page_json.get('height')
    result['canvas']['powerbi'] = {'width': pbi_w, 'height': pbi_h}
    result['canvas']['matched'] = (
        pbi_w == dashboard.get('size', {}).get('width')
        and pbi_h == dashboard.get('size', {}).get('height'))

    actual = _actual_visual_sizes(page_dir)
    result['actual_count'] = len(actual)
    remaining = list(actual)
    for exp in expected:
        match = next((cand for cand in remaining
                     if abs(cand['width'] - exp['width']) <= tolerance
                     and abs(cand['height'] - exp['height']) <= tolerance), None)
        if match:
            remaining.remove(match)
            result['matched'].append({
                **exp,
                'actual_width': match['width'],
                'actual_height': match['height'],
                'visual_type': match.get('visual_type'),
            })
        else:
            result['mismatched'].append({**exp, 'reason': 'no matching actual size'})
    return result


def compare_report_visual_sizes(extracted: Dict, project_dir: str, report_name: str,
                                tolerance: int = _SIZE_TOLERANCE_PX) -> Dict:
    """Compare visual sizes for every dashboard/page in a migrated report."""
    dashboards = extracted.get('dashboards', [])
    if isinstance(dashboards, dict):
        dashboards = dashboards.get('dashboards', [])
    report_dir = os.path.join(project_dir, f'{report_name}.Report')

    pages = [compare_dashboard_visual_sizes(db, report_dir, tolerance=tolerance)
             for db in dashboards]

    total_expected = sum(p['expected_count'] for p in pages)
    total_matched = sum(len(p['matched']) for p in pages)
    total_mismatched = sum(len(p['mismatched']) for p in pages)
    return {
        'report_name': report_name,
        'pages': pages,
        'summary': {
            'expected_visuals': total_expected,
            'matched': total_matched,
            'mismatched': total_mismatched,
            'match_rate_percent': (round(total_matched / total_expected * 100, 1)
                                   if total_expected else 100.0),
        },
    }
