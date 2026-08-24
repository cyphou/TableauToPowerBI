"""Self-Healing v3.4 — PBIR / report-side healers (Sprint 140).

Companion to ``self_healing_v3.py``. While that module heals the TMDL
**model** dict in-memory before write, this module heals the **report**
side post-write by walking the ``<Name>.Report/`` directory and rewriting
JSON files in place.

Healers operate on a ``ReportState`` snapshot (loaded by ``load_report``)
that holds:

    {
        'def_dir': '.../<Name>.Report/definition',
        'pages_dir': '.../<Name>.Report/definition/pages',
        'report_json': {...},                       # report.json
        'pages_metadata': {...},                    # pages.json
        'pages': [
            {
                'dir': '.../ReportSectionXxx',
                'name': 'ReportSectionXxx',
                'json': {...},                      # page.json
                'visuals': [
                    {
                        'dir': '.../visuals/<vid>',
                        'name': '<vid>',
                        'json': {...},              # visual.json
                    },
                    ...
                ],
            },
            ...
        ],
        '_dirty_files': set[str],   # absolute paths needing re-write
    }

Each healer signature: ``(state, recovery=None) -> int`` returns the
number of repairs applied. Healers never raise — defensive try/except
in ``run_report_healers``.

Wired into ``pbip_generator.create_report_structure`` immediately after
the report directory is assembled. CLI flag-free; identical contract to
the model-side engine.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Default PBI report canvas dimensions (16:9 desktop default)
_DEFAULT_CANVAS_WIDTH = 1280
_DEFAULT_CANVAS_HEIGHT = 720

# Minimum sane dimensions for a visual
_MIN_VISUAL_W = 80
_MIN_VISUAL_H = 60


# ════════════════════════════════════════════════════════════════════
#  Loader / Writer
# ════════════════════════════════════════════════════════════════════

def _safe_load_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _safe_dump_json(path: str, data: Dict[str, Any]) -> bool:
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except OSError:
        return False


def load_report(report_dir: str) -> Optional[Dict[str, Any]]:
    """Load a ``<Name>.Report/`` directory into a healable state dict.

    Returns ``None`` if the directory is missing required files.
    """
    if not os.path.isdir(report_dir):
        return None
    def_dir = os.path.join(report_dir, 'definition')
    pages_dir = os.path.join(def_dir, 'pages')
    if not os.path.isdir(pages_dir):
        return None

    report_json = _safe_load_json(os.path.join(def_dir, 'report.json')) or {}
    pages_metadata = _safe_load_json(os.path.join(pages_dir, 'pages.json')) or {}

    pages: List[Dict[str, Any]] = []
    for entry in sorted(os.listdir(pages_dir)):
        page_path = os.path.join(pages_dir, entry)
        if not os.path.isdir(page_path):
            continue
        page_json_path = os.path.join(page_path, 'page.json')
        if not os.path.isfile(page_json_path):
            continue
        page_json = _safe_load_json(page_json_path) or {}
        visuals: List[Dict[str, Any]] = []
        visuals_dir = os.path.join(page_path, 'visuals')
        if os.path.isdir(visuals_dir):
            for vname in sorted(os.listdir(visuals_dir)):
                vpath = os.path.join(visuals_dir, vname)
                vfile = os.path.join(vpath, 'visual.json')
                if not os.path.isfile(vfile):
                    continue
                vjson = _safe_load_json(vfile) or {}
                visuals.append({
                    'dir': vpath,
                    'name': vname,
                    'json': vjson,
                })
        pages.append({
            'dir': page_path,
            'name': entry,
            'json': page_json,
            'visuals': visuals,
        })

    return {
        'def_dir': def_dir,
        'pages_dir': pages_dir,
        'report_json': report_json,
        'pages_metadata': pages_metadata,
        'pages': pages,
        '_dirty_files': set(),
    }


def _mark_dirty(state: Dict[str, Any], path: str) -> None:
    state['_dirty_files'].add(path)


def write_report(state: Dict[str, Any]) -> int:
    """Re-write all dirty files. Returns count of files written."""
    written = 0
    dirty: Set[str] = state.get('_dirty_files', set())
    if not dirty:
        return 0
    # Map paths to their data sources.
    file_data: Dict[str, Dict[str, Any]] = {}
    file_data[os.path.join(state['def_dir'], 'report.json')] = state['report_json']
    file_data[os.path.join(state['pages_dir'], 'pages.json')] = state['pages_metadata']
    for page in state['pages']:
        file_data[os.path.join(page['dir'], 'page.json')] = page['json']
        for visual in page['visuals']:
            file_data[os.path.join(visual['dir'], 'visual.json')] = visual['json']
    for path in dirty:
        data = file_data.get(path)
        if data is None:
            continue
        if _safe_dump_json(path, data):
            written += 1
    state['_dirty_files'].clear()
    return written


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════

def _record(recovery, healer: str, target: str, severity: str,
            description: str, action: str, follow_up: str = '') -> None:
    if recovery is None:
        return
    try:
        recovery.record(
            category='visual',
            repair_type=healer,
            description=description,
            action=action,
            severity=severity,
            follow_up=follow_up,
            item_name=target,
        )
    except Exception:
        pass


def _visual_position(visual_json: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the position dict of a visual, or None."""
    return visual_json.get('position') if isinstance(visual_json.get('position'), dict) else None


def _canvas_dimensions(report_json: Dict[str, Any]) -> tuple:
    """Read explicit canvas size from report.json or fall back to defaults."""
    settings = report_json.get('settings') or {}
    cw = settings.get('canvasWidth') or _DEFAULT_CANVAS_WIDTH
    ch = settings.get('canvasHeight') or _DEFAULT_CANVAS_HEIGHT
    try:
        return int(cw), int(ch)
    except (TypeError, ValueError):
        return _DEFAULT_CANVAS_WIDTH, _DEFAULT_CANVAS_HEIGHT


# ════════════════════════════════════════════════════════════════════
#  v3.4 healers (10)
# ════════════════════════════════════════════════════════════════════

def _heal_visual_missing_position(state, recovery=None) -> int:
    """Visuals without an x/y position render at (0,0) and stack on top of
    each other. Default to a sensible offset so the user notices."""
    repairs = 0
    for page in state['pages']:
        offset = 0
        for visual in page['visuals']:
            pos = _visual_position(visual['json'])
            if pos is None:
                visual['json']['position'] = {
                    'x': 16, 'y': 16 + offset,
                    'width': 480, 'height': 280, 'z': offset,
                }
                _mark_dirty(state, os.path.join(visual['dir'], 'visual.json'))
                _record(recovery, 'visual_missing_position', visual['name'],
                        'warning',
                        'Visual has no position — would render at (0,0)',
                        'Inserted default position 16,16 size 480x280')
                repairs += 1
                offset += 16
            else:
                if 'x' not in pos or 'y' not in pos:
                    pos.setdefault('x', 16)
                    pos.setdefault('y', 16 + offset)
                    _mark_dirty(state, os.path.join(visual['dir'], 'visual.json'))
                    _record(recovery, 'visual_missing_position', visual['name'],
                            'warning',
                            "Visual position missing 'x' or 'y' coordinate",
                            'Filled missing coordinate with default')
                    repairs += 1
                    offset += 16
    return repairs


def _heal_visual_zero_size(state, recovery=None) -> int:
    """Visuals with width<=0 or height<=0 are invisible and trip PBI Desktop
    layout warnings. Reset to a sane minimum size."""
    repairs = 0
    for page in state['pages']:
        for visual in page['visuals']:
            pos = _visual_position(visual['json'])
            if not pos:
                continue
            w = pos.get('width', 0)
            h = pos.get('height', 0)
            try:
                w = float(w); h = float(h)
            except (TypeError, ValueError):
                w = h = 0
            if w <= 0:
                pos['width'] = max(_MIN_VISUAL_W, 480)
                _mark_dirty(state, os.path.join(visual['dir'], 'visual.json'))
                _record(recovery, 'visual_zero_size', visual['name'],
                        'warning',
                        f'Visual width was {w} (≤0) — invisible',
                        f"Reset width to {pos['width']}")
                repairs += 1
            if h <= 0:
                pos['height'] = max(_MIN_VISUAL_H, 280)
                _mark_dirty(state, os.path.join(visual['dir'], 'visual.json'))
                _record(recovery, 'visual_zero_size', visual['name'],
                        'warning',
                        f'Visual height was {h} (≤0) — invisible',
                        f"Reset height to {pos['height']}")
                repairs += 1
    return repairs


def _heal_visual_off_canvas(state, recovery=None) -> int:
    """Visual extending past canvas bounds is clipped in PBI Service.
    Clamp position+size to the canvas."""
    repairs = 0
    cw, ch = _canvas_dimensions(state['report_json'])
    for page in state['pages']:
        for visual in page['visuals']:
            pos = _visual_position(visual['json'])
            if not pos:
                continue
            try:
                x = float(pos.get('x', 0)); y = float(pos.get('y', 0))
                w = float(pos.get('width', 0)); h = float(pos.get('height', 0))
            except (TypeError, ValueError):
                continue
            changed = False
            if x < 0:
                pos['x'] = 0; changed = True
            if y < 0:
                pos['y'] = 0; changed = True
            if x + w > cw:
                pos['width'] = max(_MIN_VISUAL_W, cw - max(0, int(x)))
                changed = True
            if y + h > ch:
                pos['height'] = max(_MIN_VISUAL_H, ch - max(0, int(y)))
                changed = True
            if changed:
                _mark_dirty(state, os.path.join(visual['dir'], 'visual.json'))
                _record(recovery, 'visual_off_canvas', visual['name'],
                        'info',
                        f'Visual extended past canvas {cw}x{ch}',
                        'Clamped position/size to canvas bounds')
                repairs += 1
    return repairs


def _heal_visual_zindex_collision(state, recovery=None) -> int:
    """Two visuals on the same page sharing a zIndex render in undefined
    order. Re-assign sequential zIndex within each page (lowest first)."""
    repairs = 0
    for page in state['pages']:
        seen: Dict[int, str] = {}
        collisions: List[Dict[str, Any]] = []
        for visual in page['visuals']:
            pos = _visual_position(visual['json'])
            if not pos:
                continue
            z = pos.get('z')
            if z is None:
                continue
            try:
                z = int(z)
            except (TypeError, ValueError):
                continue
            if z in seen:
                collisions.append(visual)
            else:
                seen[z] = visual['name']
        if not collisions:
            continue
        # Reassign sequential z starting from max+1
        next_z = (max(seen.keys()) + 1) if seen else 0
        for visual in collisions:
            pos = _visual_position(visual['json'])
            if pos is None:
                continue
            old = pos.get('z')
            pos['z'] = next_z
            _mark_dirty(state, os.path.join(visual['dir'], 'visual.json'))
            _record(recovery, 'visual_zindex_collision', visual['name'],
                    'info',
                    f'Visual zIndex {old} collides with another visual',
                    f'Reassigned zIndex {old} → {next_z}')
            next_z += 1
            repairs += 1
    return repairs


def _heal_visual_missing_visualtype(state, recovery=None) -> int:
    """Visual containers without a ``visualType`` render as a blank rectangle
    in PBI Desktop. Default to ``tableEx`` so the data is at least visible."""
    repairs = 0
    for page in state['pages']:
        for visual in page['visuals']:
            vj = visual['json']
            visual_block = vj.get('visual') if isinstance(vj.get('visual'), dict) else None
            if visual_block is None:
                continue
            vt = visual_block.get('visualType')
            if vt is None or vt == '':
                visual_block['visualType'] = 'tableEx'
                _mark_dirty(state, os.path.join(visual['dir'], 'visual.json'))
                _record(recovery, 'visual_missing_visualtype', visual['name'],
                        'warning',
                        'Visual container missing visualType — would render blank',
                        "Defaulted to 'tableEx'",
                        follow_up='Pick a more appropriate visual type if needed')
                repairs += 1
    return repairs


def _heal_visual_negative_zindex(state, recovery=None) -> int:
    """Negative zIndex values cause rendering glitches in PBI Service.
    Clamp to 0."""
    repairs = 0
    for page in state['pages']:
        for visual in page['visuals']:
            pos = _visual_position(visual['json'])
            if not pos:
                continue
            z = pos.get('z')
            try:
                z = int(z) if z is not None else 0
            except (TypeError, ValueError):
                z = 0
            if z < 0:
                pos['z'] = 0
                _mark_dirty(state, os.path.join(visual['dir'], 'visual.json'))
                _record(recovery, 'visual_negative_zindex', visual['name'],
                        'info',
                        f'Visual zIndex was {z} (<0)',
                        'Clamped zIndex to 0')
                repairs += 1
    return repairs


def _heal_filter_dangling_field(state, recovery=None) -> int:
    """Report-, page-, or visual-level filters with no ``field`` reference
    are silently dropped by PBI but produce noisy load warnings. Strip them."""
    repairs = 0

    def _strip(filters: List[Any], owner: str) -> int:
        local = 0
        keep = []
        for f in filters:
            if not isinstance(f, dict):
                continue
            if not f.get('field') and not f.get('Expression') and not f.get('expression'):
                _record(recovery, 'filter_dangling_field', owner,
                        'warning',
                        'Filter has no field reference',
                        'Removed dangling filter entry')
                local += 1
                continue
            keep.append(f)
        if local:
            filters[:] = keep
        return local

    rj = state['report_json']
    if isinstance(rj.get('filters'), list):
        n = _strip(rj['filters'], 'report')
        if n:
            _mark_dirty(state, os.path.join(state['def_dir'], 'report.json'))
            repairs += n

    for page in state['pages']:
        if isinstance(page['json'].get('filters'), list):
            n = _strip(page['json']['filters'], page['name'])
            if n:
                _mark_dirty(state, os.path.join(page['dir'], 'page.json'))
                repairs += n
        for visual in page['visuals']:
            if isinstance(visual['json'].get('filters'), list):
                n = _strip(visual['json']['filters'], visual['name'])
                if n:
                    _mark_dirty(state, os.path.join(visual['dir'], 'visual.json'))
                    repairs += n
    return repairs


def _heal_bookmark_dangling_page(state, recovery=None) -> int:
    """Bookmarks targeting a page that no longer exists fail silently in PBI
    Desktop and break the bookmark navigation pane. Drop them."""
    repairs = 0
    rj = state['report_json']
    bookmarks = rj.get('bookmarks')
    if not isinstance(bookmarks, list):
        return 0
    page_names = {p['name'] for p in state['pages']}
    keep: List[Any] = []
    for bm in bookmarks:
        if not isinstance(bm, dict):
            continue
        target = (
            bm.get('targetPage')
            or bm.get('explorationState', {}).get('activeSection')
            if isinstance(bm.get('explorationState'), dict)
            else bm.get('targetPage')
        )
        if target and target not in page_names:
            _record(recovery, 'bookmark_dangling_page',
                    bm.get('name', 'unnamed_bookmark'),
                    'warning',
                    f"Bookmark targets page '{target}' which doesn't exist",
                    'Removed dangling bookmark')
            repairs += 1
            continue
        keep.append(bm)
    if repairs:
        rj['bookmarks'] = keep
        _mark_dirty(state, os.path.join(state['def_dir'], 'report.json'))
    return repairs


def _heal_pagesmeta_orphan_pageorder(state, recovery=None) -> int:
    """``pages.json`` ``pageOrder`` listing pages whose folder no longer
    exists makes PBI Desktop fail with "page not found". Sync to disk."""
    pm = state['pages_metadata']
    order = pm.get('pageOrder')
    if not isinstance(order, list):
        return 0
    on_disk = {p['name'] for p in state['pages']}
    bad = [n for n in order if n not in on_disk]
    missing = [p['name'] for p in state['pages'] if p['name'] not in order]
    if not bad and not missing:
        return 0
    new_order = [n for n in order if n in on_disk] + missing
    pm['pageOrder'] = new_order
    _mark_dirty(state, os.path.join(state['pages_dir'], 'pages.json'))
    if bad:
        _record(recovery, 'pagesmeta_orphan_pageorder', 'pages.json',
                'warning',
                f'pageOrder lists {len(bad)} non-existent pages: {bad}',
                'Removed orphan entries from pageOrder')
    if missing:
        _record(recovery, 'pagesmeta_orphan_pageorder', 'pages.json',
                'info',
                f'{len(missing)} pages on disk missing from pageOrder',
                'Appended to pageOrder')
    return len(bad) + len(missing)


def _heal_pagesmeta_missing_active(state, recovery=None) -> int:
    """``activePageName`` empty or pointing at a deleted page → PBI opens to
    a blank canvas. Default to first entry in ``pageOrder``."""
    pm = state['pages_metadata']
    active = pm.get('activePageName', '')
    order = pm.get('pageOrder') or []
    on_disk = {p['name'] for p in state['pages']}
    if not on_disk:
        return 0
    if active and active in on_disk:
        return 0
    new_active = order[0] if order and order[0] in on_disk else next(iter(sorted(on_disk)))
    pm['activePageName'] = new_active
    _mark_dirty(state, os.path.join(state['pages_dir'], 'pages.json'))
    _record(recovery, 'pagesmeta_missing_active', 'pages.json',
            'warning',
            f"activePageName was '{active}' (invalid or empty)",
            f"Set to '{new_active}'")
    return 1


def _heal_visual_query_no_select(state, recovery=None) -> int:
    """Visual ``query.queryState`` with no Values/Category projections renders
    as an empty visual. Tag with a MigrationNote so the user knows."""
    repairs = 0
    for page in state['pages']:
        for visual in page['visuals']:
            vj = visual['json']
            visual_block = vj.get('visual') if isinstance(vj.get('visual'), dict) else None
            if not visual_block:
                continue
            query = visual_block.get('query')
            if not isinstance(query, dict):
                continue
            qstate = query.get('queryState')
            if not isinstance(qstate, dict):
                continue
            # Count projection roles with at least one item
            non_empty = sum(
                1 for k, v in qstate.items()
                if isinstance(v, dict) and v.get('projections')
            )
            if non_empty > 0:
                continue
            # Add a MigrationNote so it's visible in version control diffs
            # PBIR v4.0: annotations live at the visual.json root (vj).
            notes = vj.setdefault('annotations', [])
            if not any(
                isinstance(a, dict) and a.get('name') == 'MigrationNote'
                for a in notes
            ):
                notes.append({
                    'name': 'MigrationNote',
                    'value': 'Visual has no projections — empty query state',
                })
                _mark_dirty(state, os.path.join(visual['dir'], 'visual.json'))
                _record(recovery, 'visual_query_no_select', visual['name'],
                        'info',
                        'Visual queryState has no projections — empty visual',
                        'Tagged with MigrationNote for review')
                repairs += 1
    return repairs


# ════════════════════════════════════════════════════════════════════
#  Phase 5 — v3.6 report healers (Sprint 145)
# ════════════════════════════════════════════════════════════════════


def _overlap_sort_key(visual: Dict[str, Any]) -> tuple:
    """Stable ordering for overlap healing: lowest z-order (backdrop) first so
    it stays anchored, higher z-order (foreground) visuals get staggered.
    Visual directory names are random UUIDs, so relying on listdir order makes
    the nudge target non-deterministic; this key restores determinism."""
    vj = visual.get('json', {})
    pos = vj.get('position') if isinstance(vj.get('position'), dict) else {}
    return (pos.get('z', 0), pos.get('tabOrder', 0), visual.get('name', ''))


def _heal_visual_overlap_full(state, recovery=None) -> int:
    """Two visuals with identical (x, y, width, height) on the same page →
    stagger the higher z-order one by 32 px on both axes so PBI Desktop doesn't
    hide one. Visuals are processed in a deterministic z-order (not random UUID
    directory order) so the staggered visual is always the same across runs."""
    repairs = 0
    for page in state['pages']:
        seen: Dict[tuple, Dict[str, Any]] = {}  # (x,y,w,h) → first visual json
        for visual in sorted(page['visuals'], key=_overlap_sort_key):
            vj = visual['json']
            pos = vj.get('position') if isinstance(vj.get('position'), dict) else None
            if pos is None:
                continue
            key = (pos.get('x', 0), pos.get('y', 0),
                   pos.get('width', 0), pos.get('height', 0))
            if key in seen:
                pos['x'] = pos.get('x', 0) + 32
                pos['y'] = pos.get('y', 0) + 32
                _mark_dirty(state, os.path.join(visual['dir'], 'visual.json'))
                _record(recovery, 'visual_overlap_full', visual['name'],
                        'info',
                        f"Visual fully overlaps another at ({key[0]},{key[1]})",
                        'Staggered by +32 px on x and y')
                repairs += 1
            else:
                seen[key] = vj
    return repairs


def _heal_visual_filter_unknown_field(state, recovery=None) -> int:
    """Report/page/visual filter references a column not present in any
    visual's queryState on the same page. Remove the filter entry."""
    repairs = 0
    # Collect all known field names across all visuals
    known_fields: set = set()
    for page in state['pages']:
        for visual in page['visuals']:
            vj = visual['json']
            vblock = vj.get('visual') if isinstance(vj.get('visual'), dict) else None
            if not vblock:
                continue
            qs = vblock.get('query', {}).get('queryState') if isinstance(vblock.get('query'), dict) else None
            if isinstance(qs, dict):
                for role_val in qs.values():
                    if isinstance(role_val, dict):
                        for proj in (role_val.get('projections') or []):
                            if isinstance(proj, dict):
                                field = proj.get('field', {})
                                col = field.get('Column', {}).get('Property') if isinstance(field.get('Column'), dict) else None
                                measure = field.get('Measure', {}).get('Property') if isinstance(field.get('Measure'), dict) else None
                                if col:
                                    known_fields.add(col)
                                if measure:
                                    known_fields.add(measure)
    if not known_fields:
        return 0
    # Check report-level filters
    rj = state['report_json']
    rfilters = rj.get('filters') if isinstance(rj.get('filters'), list) else None
    if rfilters is not None:
        keep = []
        for f in rfilters:
            if not isinstance(f, dict):
                keep.append(f)
                continue
            fname = f.get('name') or f.get('field') or ''
            if fname and fname not in known_fields:
                _record(recovery, 'visual_filter_unknown_field', fname,
                        'warning',
                        f"Report filter references unknown field '{fname}'",
                        'Removed dangling report filter')
                repairs += 1
                continue
            keep.append(f)
        if repairs:
            rj['filters'] = keep
            _mark_dirty(state, os.path.join(state['def_dir'], 'report.json'))
    return repairs


def _heal_visual_query_unknown_measure(state, recovery=None) -> int:
    """Visual query projects a measure name that looks like a raw Tableau
    field ref (contains ``//`` or ``[``+``]`` with dots). Tag with
    MigrationNote — we can't remove the projection safely."""
    repairs = 0
    import re
    _BAD_REF = re.compile(r'\[.*\..*\]|//')
    for page in state['pages']:
        for visual in page['visuals']:
            vj = visual['json']
            vblock = vj.get('visual') if isinstance(vj.get('visual'), dict) else None
            if not vblock:
                continue
            qs = vblock.get('query', {}).get('queryState') if isinstance(vblock.get('query'), dict) else None
            if not isinstance(qs, dict):
                continue
            for role_val in qs.values():
                if not isinstance(role_val, dict):
                    continue
                for proj in (role_val.get('projections') or []):
                    if not isinstance(proj, dict):
                        continue
                    field = proj.get('field', {})
                    prop = None
                    if isinstance(field.get('Measure'), dict):
                        prop = field['Measure'].get('Property', '')
                    elif isinstance(field.get('Column'), dict):
                        prop = field['Column'].get('Property', '')
                    if prop and _BAD_REF.search(prop):
                        # PBIR v4.0: annotations must live at the visual.json
                        # root (vj), not inside the ``visual`` block (vblock).
                        notes = vj.setdefault('annotations', [])
                        if not any(isinstance(a, dict) and a.get('name') == 'MigrationNote_BadRef'
                                   for a in notes):
                            notes.append({
                                'name': 'MigrationNote_BadRef',
                                'value': f'Suspicious field ref: {prop}',
                            })
                            _mark_dirty(state, os.path.join(visual['dir'], 'visual.json'))
                            _record(recovery, 'visual_query_unknown_measure',
                                    visual['name'], 'warning',
                                    f"Query references suspicious field '{prop}'",
                                    'Tagged with MigrationNote_BadRef')
                            repairs += 1
    return repairs


def _heal_slicer_targets_missing_field(state, recovery=None) -> int:
    """Slicer visual whose target column name is empty or whitespace-only →
    PBI Desktop renders a blank slicer with no data. Tag with MigrationNote."""
    repairs = 0
    for page in state['pages']:
        for visual in page['visuals']:
            vj = visual['json']
            vblock = vj.get('visual') if isinstance(vj.get('visual'), dict) else None
            if not vblock:
                continue
            if vblock.get('visualType') != 'slicer':
                continue
            qs = vblock.get('query', {}).get('queryState') if isinstance(vblock.get('query'), dict) else None
            if not isinstance(qs, dict):
                continue
            has_field = False
            for role_val in qs.values():
                if isinstance(role_val, dict):
                    for proj in (role_val.get('projections') or []):
                        if isinstance(proj, dict) and proj.get('field'):
                            has_field = True
                            break
                if has_field:
                    break
            if not has_field:
                # PBIR v4.0: annotations live at the visual.json root (vj).
                notes = vj.setdefault('annotations', [])
                if not any(isinstance(a, dict) and a.get('name') == 'MigrationNote_SlicerNoField'
                           for a in notes):
                    notes.append({
                        'name': 'MigrationNote_SlicerNoField',
                        'value': 'Slicer has no target field configured',
                    })
                    _mark_dirty(state, os.path.join(visual['dir'], 'visual.json'))
                    _record(recovery, 'slicer_targets_missing_field',
                            visual['name'], 'warning',
                            'Slicer visual has no field projections',
                            'Tagged with MigrationNote_SlicerNoField')
                    repairs += 1
    return repairs


def _heal_bookmark_targets_missing_visual(state, recovery=None) -> int:
    """Bookmark ``explorationState.visualStates`` references a visual GUID
    that doesn't exist on the target page. Remove the orphan entry."""
    repairs = 0
    rj = state['report_json']
    bookmarks = rj.get('bookmarks')
    if not isinstance(bookmarks, list):
        return 0
    all_visual_ids: set = set()
    for page in state['pages']:
        for visual in page['visuals']:
            all_visual_ids.add(visual['name'])
    changed = False
    for bm in bookmarks:
        if not isinstance(bm, dict):
            continue
        es = bm.get('explorationState')
        if not isinstance(es, dict):
            continue
        vs = es.get('visualStates')
        if not isinstance(vs, dict):
            continue
        bad_keys = [k for k in vs if k not in all_visual_ids]
        for k in bad_keys:
            del vs[k]
            _record(recovery, 'bookmark_targets_missing_visual',
                    bm.get('name', 'unnamed'),
                    'info',
                    f"Bookmark visual state references non-existent visual '{k}'",
                    'Removed orphan visual state entry')
            repairs += 1
            changed = True
    if changed:
        _mark_dirty(state, os.path.join(state['def_dir'], 'report.json'))
    return repairs


def _heal_theme_dataColors_empty(state, recovery=None) -> int:
    """Theme JSON with empty ``dataColors`` array → PBI Desktop falls back to
    grey. Inject a safe default palette."""
    _DEFAULT_PALETTE = [
        '#118DFF', '#12239E', '#E66C37', '#6B007B',
        '#E044A7', '#744EC2', '#D9B300', '#D64550',
    ]
    rj = state['report_json']
    resources = rj.get('resourcePackages')
    if not isinstance(resources, list):
        return 0
    repairs = 0
    for rp in resources:
        if not isinstance(rp, dict):
            continue
        items = rp.get('items')
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            content = item.get('content')
            if not isinstance(content, dict):
                continue
            theme = content.get('theme')
            if not isinstance(theme, dict):
                continue
            dc = theme.get('dataColors')
            if isinstance(dc, list) and len(dc) == 0:
                theme['dataColors'] = list(_DEFAULT_PALETTE)
                _mark_dirty(state, os.path.join(state['def_dir'], 'report.json'))
                _record(recovery, 'theme_dataColors_empty', 'theme',
                        'warning',
                        'Theme dataColors array was empty',
                        'Injected default 8-color palette')
                repairs += 1
    return repairs


def _heal_page_no_visuals(state, recovery=None) -> int:
    """Page with zero visuals → add MigrationNote annotation to page.json.
    PBI Desktop shows a blank page but doesn't error."""
    repairs = 0
    for page in state['pages']:
        if page['visuals']:
            continue
        pj = page['json']
        annotations = pj.setdefault('annotations', [])
        if any(isinstance(a, dict) and a.get('name') == 'MigrationNote_EmptyPage'
               for a in annotations):
            continue
        annotations.append({
            'name': 'MigrationNote_EmptyPage',
            'value': 'Page has no visuals — review migration output',
        })
        _mark_dirty(state, os.path.join(page['dir'], 'page.json'))
        _record(recovery, 'page_no_visuals', page['name'],
                'info',
                'Page contains zero visuals',
                'Tagged with MigrationNote_EmptyPage')
        repairs += 1
    return repairs


def _heal_pagesmeta_duplicate_pageorder(state, recovery=None) -> int:
    """``pageOrder`` array in pages.json lists the same page name more than
    once → PBI Desktop shows the page twice in the navigator. Deduplicate."""
    pm = state['pages_metadata']
    order = pm.get('pageOrder')
    if not isinstance(order, list) or len(order) < 2:
        return 0
    seen: set = set()
    deduped: list = []
    dupes = 0
    for name in order:
        if name in seen:
            dupes += 1
        else:
            seen.add(name)
            deduped.append(name)
    if dupes == 0:
        return 0
    pm['pageOrder'] = deduped
    _mark_dirty(state, os.path.join(state['pages_dir'], 'pages.json'))
    _record(recovery, 'pagesmeta_duplicate_pageorder', 'pages.json',
            'warning',
            f'{dupes} duplicate entries in pageOrder',
            'Deduplicated pageOrder')
    return dupes


def _heal_tooltip_page_oversized(state, recovery=None) -> int:
    """Tooltip page whose dimensions exceed 480×320 → PBI Desktop warns.
    Clamp to the canonical tooltip size."""
    _TOOLTIP_W = 480
    _TOOLTIP_H = 320
    repairs = 0
    for page in state['pages']:
        pj = page['json']
        if pj.get('pageType') != 'Tooltip':
            continue
        w = pj.get('width', _TOOLTIP_W)
        h = pj.get('height', _TOOLTIP_H)
        if w <= _TOOLTIP_W and h <= _TOOLTIP_H:
            continue
        pj['width'] = min(w, _TOOLTIP_W)
        pj['height'] = min(h, _TOOLTIP_H)
        _mark_dirty(state, os.path.join(page['dir'], 'page.json'))
        _record(recovery, 'tooltip_page_oversized', page['name'],
                'info',
                f'Tooltip page was {w}×{h} (max 480×320)',
                f"Clamped to {pj['width']}×{pj['height']}")
        repairs += 1
    return repairs


def _heal_mobile_layout_orphan_visual(state, recovery=None) -> int:
    """Mobile layout references visual GUIDs that no longer exist on the
    page. Remove orphan entries."""
    repairs = 0
    for page in state['pages']:
        pj = page['json']
        mobile = pj.get('mobileState')
        if not isinstance(mobile, dict):
            continue
        visuals_layout = mobile.get('visuals')
        if not isinstance(visuals_layout, dict):
            continue
        page_visual_ids = {v['name'] for v in page['visuals']}
        orphans = [k for k in visuals_layout if k not in page_visual_ids]
        for k in orphans:
            del visuals_layout[k]
            _record(recovery, 'mobile_layout_orphan_visual', page['name'],
                    'info',
                    f"Mobile layout references non-existent visual '{k}'",
                    'Removed orphan mobile visual entry')
            repairs += 1
        if orphans:
            _mark_dirty(state, os.path.join(page['dir'], 'page.json'))
    return repairs


# ════════════════════════════════════════════════════════════════════
#  Preheal — filter literal hygiene (defense-in-depth for visitIn crash)
# ════════════════════════════════════════════════════════════════════
#
# Background: PBI Desktop's SQExprValidationVisitor.visitIn rejects literal
# values that violate the column/measure type contract. Three known
# malformations crash the visitor or surface as
# "Something's wrong with one or more filters":
#   1. ``%null%`` Tableau sentinel passed through as a literal value.
#   2. Bare ``null`` token (case-insensitive) used as a string literal.
#   3. Empty ``In`` expressions left behind after upstream filtering.
#
# Generation paths in pbip_generator.py already prevent these, but this
# healer is a last-line check that scans the persisted ``Where`` clauses
# and scrubs survivors. Operates JSON-only — no TMDL/column-type lookup.

_NULL_PLACEHOLDERS = ('%null%', "'%null%'")


def _literal_is_null_placeholder(lit_value: Any) -> bool:
    """Return True if a Literal.Value is a Tableau null sentinel."""
    if not isinstance(lit_value, str):
        return False
    v = lit_value.strip()
    if not v:
        return False
    if v.lower() in _NULL_PLACEHOLDERS:
        return True
    # Quoted form: literal value is wrapped in single quotes per PBI grammar
    if (v.startswith("'") and v.endswith("'") and
            v.strip("'").lower() == 'null'):
        # Note: legitimate string "null" (e.g. status code) is rare; PBI
        # accepts it, so only flag the explicit %null% wrapper above.
        return False
    return False


def _scrub_filter_where(where: Any, owner: str, recovery=None) -> int:
    """Walk a filter ``Where`` list and drop null-sentinel literals from
    every ``In.Values`` row. Returns count of removed entries."""
    if not isinstance(where, list):
        return 0
    repairs = 0
    surviving_where: List[Any] = []
    for clause in where:
        if not isinstance(clause, dict):
            surviving_where.append(clause)
            continue
        cond = clause.get('Condition') if isinstance(clause.get('Condition'), dict) else None
        if not cond:
            surviving_where.append(clause)
            continue
        # Unwrap optional Not wrapper: {Not: {Expression: {In: ...}}}
        target = cond
        not_wrapper = cond.get('Not') if isinstance(cond.get('Not'), dict) else None
        if not_wrapper and isinstance(not_wrapper.get('Expression'), dict):
            target = not_wrapper['Expression']
        in_expr = target.get('In') if isinstance(target.get('In'), dict) else None
        if not in_expr:
            surviving_where.append(clause)
            continue
        values = in_expr.get('Values')
        if not isinstance(values, list):
            surviving_where.append(clause)
            continue
        kept_rows: List[Any] = []
        for row in values:
            if not isinstance(row, list):
                kept_rows.append(row)
                continue
            kept_cells = []
            row_has_null = False
            for cell in row:
                if (isinstance(cell, dict)
                        and isinstance(cell.get('Literal'), dict)
                        and _literal_is_null_placeholder(cell['Literal'].get('Value'))):
                    row_has_null = True
                    repairs += 1
                    continue
                kept_cells.append(cell)
            if row_has_null and not kept_cells:
                # Whole row was the null placeholder → drop the row
                continue
            kept_rows.append(kept_cells if row_has_null else row)
        if not kept_rows:
            # In-expression became empty → drop the whole clause
            _record(recovery, 'filter_literal_null_placeholder', owner,
                    'warning',
                    'Filter In-expression became empty after dropping %null% sentinels',
                    'Removed entire filter clause to avoid visitIn crash')
            continue
        in_expr['Values'] = kept_rows
        surviving_where.append(clause)
    if repairs and isinstance(where, list):
        where[:] = surviving_where
    return repairs


def _heal_filter_literal_null_placeholder(state, recovery=None) -> int:
    """Scrub Tableau ``%null%`` sentinels from filter ``In`` expressions.

    Defense-in-depth for the visitIn crash. Generation drops these
    upstream (see pbip_generator._is_null_placeholder); this healer
    catches anything that slipped through (e.g. report-level filters
    constructed in another path)."""
    if not state:
        return 0
    repairs = 0

    rj = state.get('report_json') or {}
    if isinstance(rj.get('filters'), list):
        for f in rj['filters']:
            if isinstance(f, dict) and isinstance(f.get('filter'), dict):
                n = _scrub_filter_where(f['filter'].get('Where'), 'report', recovery)
                if n:
                    _mark_dirty(state, os.path.join(state['def_dir'], 'report.json'))
                    repairs += n

    for page in state['pages']:
        pj = page['json']
        if isinstance(pj.get('filters'), list):
            for f in pj['filters']:
                if isinstance(f, dict) and isinstance(f.get('filter'), dict):
                    n = _scrub_filter_where(f['filter'].get('Where'), page['name'], recovery)
                    if n:
                        _mark_dirty(state, os.path.join(page['dir'], 'page.json'))
                        repairs += n
        for visual in page['visuals']:
            if isinstance(visual['json'].get('filters'), list):
                for f in visual['json']['filters']:
                    if isinstance(f, dict) and isinstance(f.get('filter'), dict):
                        n = _scrub_filter_where(f['filter'].get('Where'),
                                                visual['name'], recovery)
                        if n:
                            _mark_dirty(state, os.path.join(visual['dir'], 'visual.json'))
                            repairs += n
    return repairs


def _heal_filter_empty_in_expression(state, recovery=None) -> int:
    """Drop filter entries whose ``In.Values`` list is empty.

    PBI Desktop logs a "filter has no values" warning and may crash
    visitIn. Cause: upstream pipelines that prune categorical values
    (e.g. removing %null%) can leave an empty Values array."""
    if not state:
        return 0
    repairs = 0

    def _prune(filters: List[Any], owner: str, json_path: str) -> int:
        local = 0
        keep = []
        for f in filters:
            if not isinstance(f, dict) or not isinstance(f.get('filter'), dict):
                keep.append(f)
                continue
            where = f['filter'].get('Where')
            empty_in = False
            if isinstance(where, list):
                for clause in where:
                    cond = (clause.get('Condition') if isinstance(clause, dict) else None) or {}
                    target = cond
                    if isinstance(cond.get('Not'), dict) and isinstance(cond['Not'].get('Expression'), dict):
                        target = cond['Not']['Expression']
                    in_expr = target.get('In') if isinstance(target, dict) else None
                    if isinstance(in_expr, dict):
                        vals = in_expr.get('Values')
                        if isinstance(vals, list) and not vals:
                            empty_in = True
                            break
            if empty_in:
                _record(recovery, 'filter_empty_in_expression', owner,
                        'warning',
                        'Filter In-expression has zero Values',
                        'Removed empty filter to avoid visitIn crash')
                local += 1
                continue
            keep.append(f)
        if local:
            filters[:] = keep
            _mark_dirty(state, json_path)
        return local

    rj = state.get('report_json') or {}
    if isinstance(rj.get('filters'), list):
        repairs += _prune(rj['filters'], 'report',
                          os.path.join(state['def_dir'], 'report.json'))
    for page in state['pages']:
        if isinstance(page['json'].get('filters'), list):
            repairs += _prune(page['json']['filters'], page['name'],
                              os.path.join(page['dir'], 'page.json'))
        for visual in page['visuals']:
            if isinstance(visual['json'].get('filters'), list):
                repairs += _prune(visual['json']['filters'], visual['name'],
                                  os.path.join(visual['dir'], 'visual.json'))
    return repairs


def _heal_invalid_visualtype(state, recovery=None) -> int:
    """Visuals whose ``visualType`` isn't a recognised PBI visual identifier
    render as a blank rectangle in PBI Desktop. Common cause: raw Tableau
    mark names (``'bar'``, ``'Bar'``, ``'Heat Map'``) leaking through the
    extractor instead of being normalised to a PBI type
    (``'clusteredBarChart'``, ``'matrix'``, etc.).

    Resolve using :func:`powerbi_import.visual_generator.resolve_visual_type`
    which canonicalises through ``VISUAL_TYPE_MAP`` / ``APPROXIMATION_MAP``.
    """
    try:
        from visual_generator import (
            VISUAL_TYPE_MAP,
            APPROXIMATION_MAP,
            resolve_visual_type,
        )
    except ImportError:
        return 0

    # Build the set of valid PBI visual types from both maps + a few
    # legitimately-empty container types the extractor produces.
    valid = set(VISUAL_TYPE_MAP.values())
    for _key, _entry in APPROXIMATION_MAP.items():
        if isinstance(_entry, tuple) and _entry:
            valid.add(_entry[0])
    valid.update({
        'textbox', 'image', 'shape', 'basicShape', 'actionButton',
        'pageNavigator', 'bookmarkNavigator', 'slicer',
        'scriptVisual', 'pythonVisual', 'rVisual',
    })

    repairs = 0
    for page in state['pages']:
        for visual in page['visuals']:
            vj = visual['json']
            visual_block = vj.get('visual') if isinstance(vj.get('visual'), dict) else None
            if visual_block is None:
                continue
            vt = visual_block.get('visualType')
            if not vt or vt in valid:
                continue
            new_vt = resolve_visual_type(vt)
            if new_vt == vt:
                # resolve_visual_type returned the same invalid string —
                # final fallback to tableEx so the visual still renders.
                new_vt = 'tableEx'
            visual_block['visualType'] = new_vt
            _mark_dirty(state, os.path.join(visual['dir'], 'visual.json'))
            _record(recovery, 'visual_invalid_visualtype', visual['name'],
                    'warning',
                    f"visualType '{vt}' is not a recognised PBI visual",
                    f"Rewrote to '{new_vt}'",
                    follow_up='Pick a more appropriate visual type if needed')
            repairs += 1
    return repairs


# ════════════════════════════════════════════════════════════════════
#  Healer registry
# ════════════════════════════════════════════════════════════════════

_REPORT_HEALERS = (
    # v3.4 — PBIR / report-side
    _heal_visual_missing_position,           # 1
    _heal_visual_zero_size,                  # 2
    _heal_visual_off_canvas,                 # 3
    _heal_visual_zindex_collision,           # 4
    _heal_visual_missing_visualtype,         # 5
    _heal_visual_negative_zindex,            # 6
    _heal_filter_dangling_field,             # 7
    _heal_bookmark_dangling_page,            # 8
    _heal_pagesmeta_orphan_pageorder,        # 9
    _heal_pagesmeta_missing_active,          # 10
    _heal_visual_query_no_select,            # 11
    # v3.6 — Phase 5 report-side healers
    _heal_visual_overlap_full,               # 12
    _heal_visual_filter_unknown_field,       # 13
    _heal_visual_query_unknown_measure,      # 14
    _heal_slicer_targets_missing_field,      # 15
    _heal_bookmark_targets_missing_visual,   # 16
    _heal_theme_dataColors_empty,            # 17
    _heal_page_no_visuals,                   # 18
    _heal_pagesmeta_duplicate_pageorder,     # 19
    _heal_tooltip_page_oversized,            # 20
    _heal_mobile_layout_orphan_visual,       # 21
    # v3.7 — filter literal preheal (visitIn crash defense)
    _heal_filter_literal_null_placeholder,   # 22
    _heal_filter_empty_in_expression,        # 23
    # Sprint 79 — defensive visualType normalization
    _heal_invalid_visualtype,                # 24
)


def run_report_healers(state, recovery=None) -> int:
    """Run all v3.4+v3.6 report healers on a loaded ReportState. Never raises."""
    if not state:
        return 0
    total = 0
    for healer in _REPORT_HEALERS:
        try:
            total += healer(state, recovery=recovery)
        except Exception as exc:  # never block migration
            logger.warning("Report healer %s raised: %s", healer.__name__, exc)
    return total


def heal_report(report_dir: str, recovery=None) -> int:
    """Convenience wrapper: load → heal → write. Returns repair count."""
    state = load_report(report_dir)
    if not state:
        return 0
    repairs = run_report_healers(state, recovery=recovery)
    if repairs:
        write_report(state)
    return repairs
