"""Sprint 140 — Self-Healing v3.4 (PBIR / report-side healers).

Tests every healer in ``powerbi_import/self_healing_report.py``.
"""

import json
import os
import shutil
import tempfile
import unittest

from powerbi_import.self_healing_report import (
    _REPORT_HEALERS,
    _heal_visual_missing_position,
    _heal_visual_zero_size,
    _heal_visual_off_canvas,
    _heal_visual_zindex_collision,
    _heal_visual_missing_visualtype,
    _heal_visual_negative_zindex,
    _heal_filter_dangling_field,
    _heal_bookmark_dangling_page,
    _heal_pagesmeta_orphan_pageorder,
    _heal_pagesmeta_missing_active,
    _heal_visual_query_no_select,
    _heal_filter_literal_null_placeholder,
    _heal_filter_empty_in_expression,
    _heal_invalid_visualtype,
    heal_report,
    load_report,
    run_report_healers,
    write_report,
)
from powerbi_import.recovery_report import RecoveryReport


# ────────────────────────────────────────────────────────────────────
#  Helpers
# ────────────────────────────────────────────────────────────────────

def _make_state(pages=None, report_json=None, pages_metadata=None):
    """Build an in-memory ReportState without touching disk."""
    return {
        'def_dir': '/tmp/fake/definition',
        'pages_dir': '/tmp/fake/definition/pages',
        'report_json': report_json or {},
        'pages_metadata': pages_metadata or {'pageOrder': [], 'activePageName': ''},
        'pages': pages or [],
        '_dirty_files': set(),
    }


def _page(name, visuals=None, page_json=None):
    return {
        'dir': f'/tmp/fake/definition/pages/{name}',
        'name': name,
        'json': page_json or {'displayName': name},
        'visuals': visuals or [],
    }


def _visual(name, visual_json=None):
    return {
        'dir': f'/tmp/fake/definition/pages/p1/visuals/{name}',
        'name': name,
        'json': visual_json or {},
    }


# ════════════════════════════════════════════════════════════════════
#  Registry / public surface
# ════════════════════════════════════════════════════════════════════

class TestRegistry(unittest.TestCase):
    def test_eleven_healers(self):
        # v3.4: 11, v3.6: +10 = 21, v3.7: +2 (filter literal preheal) = 23,
        # Sprint 79: +1 (invalid visualType) = 24
        self.assertEqual(len(_REPORT_HEALERS), 24)

    def test_run_on_empty_state_returns_zero(self):
        self.assertEqual(run_report_healers(None), 0)
        self.assertEqual(run_report_healers({}), 0)

    def test_run_on_clean_state_returns_zero(self):
        state = _make_state()
        self.assertEqual(run_report_healers(state), 0)


# ════════════════════════════════════════════════════════════════════
#  H1 — visual_missing_position
# ════════════════════════════════════════════════════════════════════

class TestMissingPosition(unittest.TestCase):
    def test_missing_position_added(self):
        state = _make_state(pages=[_page('p1', [_visual('v1', {})])])
        n = _heal_visual_missing_position(state)
        self.assertEqual(n, 1)
        pos = state['pages'][0]['visuals'][0]['json']['position']
        self.assertEqual(pos['x'], 16)
        self.assertEqual(pos['y'], 16)

    def test_partial_position_filled(self):
        state = _make_state(pages=[_page('p1', [_visual('v1', {'position': {'width': 100}})])])
        n = _heal_visual_missing_position(state)
        self.assertEqual(n, 1)
        self.assertIn('x', state['pages'][0]['visuals'][0]['json']['position'])

    def test_full_position_unchanged(self):
        v = _visual('v1', {'position': {'x': 1, 'y': 2, 'width': 10, 'height': 20}})
        state = _make_state(pages=[_page('p1', [v])])
        self.assertEqual(_heal_visual_missing_position(state), 0)


# ════════════════════════════════════════════════════════════════════
#  H2 — visual_zero_size
# ════════════════════════════════════════════════════════════════════

class TestZeroSize(unittest.TestCase):
    def test_zero_width_reset(self):
        v = _visual('v1', {'position': {'x': 0, 'y': 0, 'width': 0, 'height': 100}})
        state = _make_state(pages=[_page('p1', [v])])
        self.assertEqual(_heal_visual_zero_size(state), 1)
        self.assertGreaterEqual(v['json']['position']['width'], 80)

    def test_negative_height_reset(self):
        v = _visual('v1', {'position': {'x': 0, 'y': 0, 'width': 100, 'height': -5}})
        state = _make_state(pages=[_page('p1', [v])])
        self.assertEqual(_heal_visual_zero_size(state), 1)
        self.assertGreaterEqual(v['json']['position']['height'], 60)

    def test_invalid_size_string_treated_as_zero(self):
        v = _visual('v1', {'position': {'width': 'oops', 'height': 'oops'}})
        state = _make_state(pages=[_page('p1', [v])])
        self.assertEqual(_heal_visual_zero_size(state), 2)

    def test_no_position_skipped(self):
        v = _visual('v1', {})
        state = _make_state(pages=[_page('p1', [v])])
        self.assertEqual(_heal_visual_zero_size(state), 0)


# ════════════════════════════════════════════════════════════════════
#  H3 — visual_off_canvas
# ════════════════════════════════════════════════════════════════════

class TestOffCanvas(unittest.TestCase):
    def test_clamp_negative_origin(self):
        v = _visual('v1', {'position': {'x': -10, 'y': -5, 'width': 100, 'height': 100}})
        state = _make_state(pages=[_page('p1', [v])])
        self.assertEqual(_heal_visual_off_canvas(state), 1)
        pos = v['json']['position']
        self.assertEqual(pos['x'], 0)
        self.assertEqual(pos['y'], 0)

    def test_clamp_overflow_width(self):
        v = _visual('v1', {'position': {'x': 1200, 'y': 0, 'width': 500, 'height': 100}})
        state = _make_state(pages=[_page('p1', [v])])
        self.assertEqual(_heal_visual_off_canvas(state), 1)
        self.assertLessEqual(v['json']['position']['x'] + v['json']['position']['width'], 1280)

    def test_within_canvas_unchanged(self):
        v = _visual('v1', {'position': {'x': 0, 'y': 0, 'width': 100, 'height': 100}})
        state = _make_state(pages=[_page('p1', [v])])
        self.assertEqual(_heal_visual_off_canvas(state), 0)


# ════════════════════════════════════════════════════════════════════
#  H4 — visual_zindex_collision
# ════════════════════════════════════════════════════════════════════

class TestZIndexCollision(unittest.TestCase):
    def test_two_visuals_same_z_reassigned(self):
        v1 = _visual('v1', {'position': {'x': 0, 'y': 0, 'width': 1, 'height': 1, 'z': 0}})
        v2 = _visual('v2', {'position': {'x': 1, 'y': 1, 'width': 1, 'height': 1, 'z': 0}})
        state = _make_state(pages=[_page('p1', [v1, v2])])
        self.assertEqual(_heal_visual_zindex_collision(state), 1)
        self.assertNotEqual(v1['json']['position']['z'], v2['json']['position']['z'])

    def test_no_collision_unchanged(self):
        v1 = _visual('v1', {'position': {'z': 0}})
        v2 = _visual('v2', {'position': {'z': 1}})
        state = _make_state(pages=[_page('p1', [v1, v2])])
        self.assertEqual(_heal_visual_zindex_collision(state), 0)


# ════════════════════════════════════════════════════════════════════
#  H5 — visual_missing_visualtype
# ════════════════════════════════════════════════════════════════════

class TestMissingVisualType(unittest.TestCase):
    def test_missing_visualtype_defaulted(self):
        v = _visual('v1', {'visual': {'visualType': ''}})
        state = _make_state(pages=[_page('p1', [v])])
        self.assertEqual(_heal_visual_missing_visualtype(state), 1)
        self.assertEqual(v['json']['visual']['visualType'], 'tableEx')

    def test_existing_visualtype_unchanged(self):
        v = _visual('v1', {'visual': {'visualType': 'lineChart'}})
        state = _make_state(pages=[_page('p1', [v])])
        self.assertEqual(_heal_visual_missing_visualtype(state), 0)

    def test_no_visual_block_skipped(self):
        v = _visual('v1', {})
        state = _make_state(pages=[_page('p1', [v])])
        self.assertEqual(_heal_visual_missing_visualtype(state), 0)


# ════════════════════════════════════════════════════════════════════
#  H6 — visual_negative_zindex
# ════════════════════════════════════════════════════════════════════

class TestNegativeZIndex(unittest.TestCase):
    def test_negative_clamped_to_zero(self):
        v = _visual('v1', {'position': {'z': -5}})
        state = _make_state(pages=[_page('p1', [v])])
        self.assertEqual(_heal_visual_negative_zindex(state), 1)
        self.assertEqual(v['json']['position']['z'], 0)

    def test_zero_unchanged(self):
        v = _visual('v1', {'position': {'z': 0}})
        state = _make_state(pages=[_page('p1', [v])])
        self.assertEqual(_heal_visual_negative_zindex(state), 0)


# ════════════════════════════════════════════════════════════════════
#  H7 — filter_dangling_field
# ════════════════════════════════════════════════════════════════════

class TestDanglingFilter(unittest.TestCase):
    def test_report_filter_no_field_dropped(self):
        state = _make_state(report_json={'filters': [{'value': 'x'}, {'field': 'OK'}]})
        self.assertEqual(_heal_filter_dangling_field(state), 1)
        self.assertEqual(len(state['report_json']['filters']), 1)

    def test_page_filter_no_field_dropped(self):
        page = _page('p1')
        page['json']['filters'] = [{'something': 1}]
        state = _make_state(pages=[page])
        self.assertEqual(_heal_filter_dangling_field(state), 1)
        self.assertEqual(page['json']['filters'], [])

    def test_visual_filter_no_field_dropped(self):
        v = _visual('v1', {'filters': [{'foo': 'bar'}, {'expression': 'col=1'}]})
        state = _make_state(pages=[_page('p1', [v])])
        self.assertEqual(_heal_filter_dangling_field(state), 1)
        self.assertEqual(len(v['json']['filters']), 1)


# ════════════════════════════════════════════════════════════════════
#  H8 — bookmark_dangling_page
# ════════════════════════════════════════════════════════════════════

class TestDanglingBookmark(unittest.TestCase):
    def test_bookmark_to_missing_page_dropped(self):
        state = _make_state(
            pages=[_page('PageA')],
            report_json={'bookmarks': [
                {'name': 'bm1', 'targetPage': 'PageA'},
                {'name': 'bm2', 'targetPage': 'PageGhost'},
            ]},
        )
        self.assertEqual(_heal_bookmark_dangling_page(state), 1)
        self.assertEqual(len(state['report_json']['bookmarks']), 1)

    def test_no_bookmarks_returns_zero(self):
        state = _make_state(report_json={})
        self.assertEqual(_heal_bookmark_dangling_page(state), 0)


# ════════════════════════════════════════════════════════════════════
#  H9 — pagesmeta_orphan_pageorder
# ════════════════════════════════════════════════════════════════════

class TestOrphanPageOrder(unittest.TestCase):
    def test_orphan_removed_and_missing_appended(self):
        state = _make_state(
            pages=[_page('P1'), _page('P2')],
            pages_metadata={'pageOrder': ['P1', 'PGhost'], 'activePageName': 'P1'},
        )
        n = _heal_pagesmeta_orphan_pageorder(state)
        self.assertEqual(n, 2)  # 1 ghost removed + 1 (P2) appended
        self.assertEqual(state['pages_metadata']['pageOrder'], ['P1', 'P2'])

    def test_clean_returns_zero(self):
        state = _make_state(
            pages=[_page('P1')],
            pages_metadata={'pageOrder': ['P1'], 'activePageName': 'P1'},
        )
        self.assertEqual(_heal_pagesmeta_orphan_pageorder(state), 0)


# ════════════════════════════════════════════════════════════════════
#  H10 — pagesmeta_missing_active
# ════════════════════════════════════════════════════════════════════

class TestMissingActive(unittest.TestCase):
    def test_empty_active_set_to_first(self):
        state = _make_state(
            pages=[_page('P1'), _page('P2')],
            pages_metadata={'pageOrder': ['P1', 'P2'], 'activePageName': ''},
        )
        self.assertEqual(_heal_pagesmeta_missing_active(state), 1)
        self.assertEqual(state['pages_metadata']['activePageName'], 'P1')

    def test_active_pointing_at_ghost_reset(self):
        state = _make_state(
            pages=[_page('P1')],
            pages_metadata={'pageOrder': ['P1'], 'activePageName': 'PGhost'},
        )
        self.assertEqual(_heal_pagesmeta_missing_active(state), 1)
        self.assertEqual(state['pages_metadata']['activePageName'], 'P1')

    def test_no_pages_returns_zero(self):
        state = _make_state(pages_metadata={'activePageName': '', 'pageOrder': []})
        self.assertEqual(_heal_pagesmeta_missing_active(state), 0)

    def test_valid_active_unchanged(self):
        state = _make_state(
            pages=[_page('P1')],
            pages_metadata={'pageOrder': ['P1'], 'activePageName': 'P1'},
        )
        self.assertEqual(_heal_pagesmeta_missing_active(state), 0)


# ════════════════════════════════════════════════════════════════════
#  H11 — visual_query_no_select
# ════════════════════════════════════════════════════════════════════

class TestQueryNoSelect(unittest.TestCase):
    def test_empty_querystate_tagged(self):
        v = _visual('v1', {'visual': {'visualType': 'lineChart',
                                       'query': {'queryState': {}}}})
        state = _make_state(pages=[_page('p1', [v])])
        self.assertEqual(_heal_visual_query_no_select(state), 1)
        # PBIR v4.0: annotations live at the visual.json root, not in visual.
        annotations = v['json'].get('annotations', [])
        self.assertTrue(any(a.get('name') == 'MigrationNote' for a in annotations))

    def test_with_projections_unchanged(self):
        v = _visual('v1', {'visual': {'query': {'queryState': {
            'Values': {'projections': [{'field': {}}]}
        }}}})
        state = _make_state(pages=[_page('p1', [v])])
        self.assertEqual(_heal_visual_query_no_select(state), 0)

    def test_no_query_skipped(self):
        v = _visual('v1', {'visual': {}})
        state = _make_state(pages=[_page('p1', [v])])
        self.assertEqual(_heal_visual_query_no_select(state), 0)


# ════════════════════════════════════════════════════════════════════
#  RecoveryReport integration
# ════════════════════════════════════════════════════════════════════

class TestRecoveryIntegration(unittest.TestCase):
    def test_repairs_recorded(self):
        v = _visual('v1', {'position': {'z': -5}})
        state = _make_state(pages=[_page('p1', [v])])
        rec = RecoveryReport('test')
        run_report_healers(state, recovery=rec)
        self.assertTrue(rec.has_repairs)
        # All repairs from this engine carry category='visual'
        self.assertTrue(all(r['category'] == 'visual' for r in rec.repairs))

    def test_no_repairs_no_recovery_entries(self):
        rec = RecoveryReport('test')
        run_report_healers(_make_state(), recovery=rec)
        self.assertFalse(rec.has_repairs)


# ════════════════════════════════════════════════════════════════════
#  load_report / write_report end-to-end
# ════════════════════════════════════════════════════════════════════

class TestLoadWriteRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='heal_report_')
        self.report_dir = os.path.join(self.tmp, 'My.Report')
        self.def_dir = os.path.join(self.report_dir, 'definition')
        self.pages_dir = os.path.join(self.def_dir, 'pages')
        os.makedirs(self.pages_dir)
        # report.json
        with open(os.path.join(self.def_dir, 'report.json'), 'w', encoding='utf-8') as f:
            json.dump({'bookmarks': [{'name': 'bm', 'targetPage': 'GHOST'}]}, f)
        # pages.json
        with open(os.path.join(self.pages_dir, 'pages.json'), 'w', encoding='utf-8') as f:
            json.dump({'pageOrder': ['P1', 'GHOST'], 'activePageName': 'GHOST'}, f)
        # P1/page.json
        os.makedirs(os.path.join(self.pages_dir, 'P1', 'visuals', 'V1'))
        with open(os.path.join(self.pages_dir, 'P1', 'page.json'), 'w', encoding='utf-8') as f:
            json.dump({'displayName': 'P1'}, f)
        # P1/visuals/V1/visual.json — broken position + visualType
        with open(os.path.join(self.pages_dir, 'P1', 'visuals', 'V1', 'visual.json'),
                  'w', encoding='utf-8') as f:
            json.dump({
                'visual': {'visualType': ''},
                'position': {'x': -5, 'y': -5, 'width': 0, 'height': 0, 'z': -1},
            }, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_report_returns_state(self):
        state = load_report(self.report_dir)
        self.assertIsNotNone(state)
        self.assertEqual(len(state['pages']), 1)
        self.assertEqual(len(state['pages'][0]['visuals']), 1)

    def test_load_missing_dir_returns_none(self):
        self.assertIsNone(load_report('/nonexistent/path'))

    def test_load_missing_pages_dir_returns_none(self):
        bad = tempfile.mkdtemp()
        try:
            self.assertIsNone(load_report(bad))
        finally:
            shutil.rmtree(bad, ignore_errors=True)

    def test_heal_report_end_to_end(self):
        repaired = heal_report(self.report_dir)
        # Expect: ghost bookmark + ghost pageOrder + ghost active +
        # zero width + zero height + visualType + neg z + off-canvas
        self.assertGreaterEqual(repaired, 6)

        # Re-load and verify fixes persisted
        state = load_report(self.report_dir)
        self.assertEqual(state['pages_metadata']['activePageName'], 'P1')
        self.assertNotIn('GHOST', state['pages_metadata']['pageOrder'])
        self.assertEqual(state['report_json']['bookmarks'], [])
        v = state['pages'][0]['visuals'][0]['json']
        self.assertEqual(v['visual']['visualType'], 'tableEx')
        self.assertGreater(v['position']['width'], 0)
        self.assertGreater(v['position']['height'], 0)
        self.assertGreaterEqual(v['position']['z'], 0)
        self.assertGreaterEqual(v['position']['x'], 0)
        self.assertGreaterEqual(v['position']['y'], 0)

    def test_heal_report_idempotent(self):
        first = heal_report(self.report_dir)
        second = heal_report(self.report_dir)
        self.assertGreater(first, 0)
        self.assertEqual(second, 0)

    def test_heal_report_missing_dir_returns_zero(self):
        self.assertEqual(heal_report('/nonexistent'), 0)

    def test_write_report_no_dirty_returns_zero(self):
        state = load_report(self.report_dir)
        self.assertEqual(write_report(state), 0)


if __name__ == '__main__':
    unittest.main()


# ════════════════════════════════════════════════════════════════════
#  H22 — filter_literal_null_placeholder
# ════════════════════════════════════════════════════════════════════

def _make_in_filter(values):
    """Build a filter dict whose Where[0] is an In-expression with the given values."""
    return {
        'field': 'X',
        'filter': {
            'Version': 2,
            'From': [{'Name': 't', 'Entity': 'T', 'Type': 0}],
            'Where': [{
                'Condition': {
                    'In': {
                        'Expressions': [{
                            'Column': {
                                'Expression': {'SourceRef': {'Source': 't'}},
                                'Property': 'X',
                            }
                        }],
                        'Values': [[{'Literal': {'Value': v}}] for v in values],
                    }
                }
            }]
        }
    }


class TestHealFilterLiteralNullPlaceholder(unittest.TestCase):
    def test_drops_null_placeholder_row(self):
        f = _make_in_filter(["'a'", '%null%', "'b'"])
        state = _make_state(pages=[_page('p1', [_visual('v1', {'filters': [f]})])])
        n = _heal_filter_literal_null_placeholder(state)
        self.assertEqual(n, 1)
        kept = [row[0]['Literal']['Value']
                for row in f['filter']['Where'][0]['Condition']['In']['Values']]
        self.assertEqual(kept, ["'a'", "'b'"])

    def test_handles_quoted_null_placeholder(self):
        f = _make_in_filter(["'a'", "'%null%'"])
        state = _make_state(pages=[_page('p1', [_visual('v1', {'filters': [f]})])])
        n = _heal_filter_literal_null_placeholder(state)
        self.assertEqual(n, 1)

    def test_no_op_when_no_placeholder(self):
        f = _make_in_filter(["'a'", "'b'"])
        state = _make_state(pages=[_page('p1', [_visual('v1', {'filters': [f]})])])
        n = _heal_filter_literal_null_placeholder(state)
        self.assertEqual(n, 0)

    def test_drops_clause_when_only_placeholder(self):
        f = _make_in_filter(['%null%'])
        state = _make_state(pages=[_page('p1', [_visual('v1', {'filters': [f]})])])
        n = _heal_filter_literal_null_placeholder(state)
        self.assertEqual(n, 1)
        # Whole clause should be removed
        self.assertEqual(f['filter']['Where'], [])

    def test_scrubs_inside_not_wrapper(self):
        f = _make_in_filter(["'a'", '%null%'])
        # Wrap with Not (exclude filter)
        original_in = f['filter']['Where'][0]['Condition'].pop('In')
        f['filter']['Where'][0]['Condition']['Not'] = {'Expression': {'In': original_in}}
        state = _make_state(pages=[_page('p1', [_visual('v1', {'filters': [f]})])])
        n = _heal_filter_literal_null_placeholder(state)
        self.assertEqual(n, 1)

    def test_marks_visual_json_dirty(self):
        f = _make_in_filter(['%null%', "'a'"])
        state = _make_state(pages=[_page('p1', [_visual('v1', {'filters': [f]})])])
        _heal_filter_literal_null_placeholder(state)
        self.assertTrue(any('visual.json' in p for p in state['_dirty_files']))


# ════════════════════════════════════════════════════════════════════
#  H23 — filter_empty_in_expression
# ════════════════════════════════════════════════════════════════════

class TestHealFilterEmptyInExpression(unittest.TestCase):
    def test_drops_filter_with_empty_values(self):
        f = _make_in_filter([])
        state = _make_state(pages=[_page('p1', [_visual('v1', {'filters': [f]})])])
        n = _heal_filter_empty_in_expression(state)
        self.assertEqual(n, 1)
        self.assertEqual(state['pages'][0]['visuals'][0]['json']['filters'], [])

    def test_keeps_filter_with_values(self):
        f = _make_in_filter(["'a'"])
        state = _make_state(pages=[_page('p1', [_visual('v1', {'filters': [f]})])])
        n = _heal_filter_empty_in_expression(state)
        self.assertEqual(n, 0)

    def test_handles_page_level(self):
        f = _make_in_filter([])
        page = _page('p1', [])
        page['json']['filters'] = [f]
        state = _make_state(pages=[page])
        n = _heal_filter_empty_in_expression(state)
        self.assertEqual(n, 1)

    def test_handles_report_level(self):
        f = _make_in_filter([])
        state = _make_state(report_json={'filters': [f]})
        n = _heal_filter_empty_in_expression(state)
        self.assertEqual(n, 1)


# ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
#  H24 — invalid_visualtype (Sprint 79)
# ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

class TestHealInvalidVisualType(unittest.TestCase):
    def test_raw_tableau_bar_normalized(self):
        # Raw Tableau mark name 'bar' is invalid as a PBI visualType
        # (renders blank). Healer must rewrite to 'clusteredBarChart'.
        v = _visual('v1', {'visual': {'visualType': 'bar'}})
        state = _make_state(pages=[_page('p1', [v])])
        n = _heal_invalid_visualtype(state)
        self.assertEqual(n, 1)
        self.assertEqual(v['json']['visual']['visualType'], 'clusteredBarChart')

    def test_valid_visualtype_unchanged(self):
        v = _visual('v1', {'visual': {'visualType': 'lineChart'}})
        state = _make_state(pages=[_page('p1', [v])])
        n = _heal_invalid_visualtype(state)
        self.assertEqual(n, 0)
        self.assertEqual(v['json']['visual']['visualType'], 'lineChart')

    def test_capitalized_pie_normalized(self):
        v = _visual('v1', {'visual': {'visualType': 'Pie'}})
        state = _make_state(pages=[_page('p1', [v])])
        n = _heal_invalid_visualtype(state)
        self.assertEqual(n, 1)
        self.assertEqual(v['json']['visual']['visualType'], 'pieChart')

    def test_unknown_type_falls_back_to_tableEx(self):
        v = _visual('v1', {'visual': {'visualType': 'totallyMadeUpType'}})
        state = _make_state(pages=[_page('p1', [v])])
        n = _heal_invalid_visualtype(state)
        self.assertEqual(n, 1)
        self.assertEqual(v['json']['visual']['visualType'], 'tableEx')

    def test_missing_visual_block_skipped(self):
        v = _visual('v1', {})
        state = _make_state(pages=[_page('p1', [v])])
        n = _heal_invalid_visualtype(state)
        self.assertEqual(n, 0)

    def test_slicer_unchanged(self):
        v = _visual('v1', {'visual': {'visualType': 'slicer'}})
        state = _make_state(pages=[_page('p1', [v])])
        n = _heal_invalid_visualtype(state)
        self.assertEqual(n, 0)
