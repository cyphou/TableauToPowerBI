"""Sprint 145 — Self-Healing v3.6 (Phase 5 report-side healers).

Tests the 10 new healers added in Phase 5 of the Zero-Error roadmap.
"""

import unittest

from powerbi_import.self_healing_report import (
    _REPORT_HEALERS,
    _heal_visual_overlap_full,
    _heal_visual_filter_unknown_field,
    _heal_visual_query_unknown_measure,
    _heal_slicer_targets_missing_field,
    _heal_bookmark_targets_missing_visual,
    _heal_theme_dataColors_empty,
    _heal_page_no_visuals,
    _heal_pagesmeta_duplicate_pageorder,
    _heal_tooltip_page_oversized,
    _heal_mobile_layout_orphan_visual,
    run_report_healers,
)
from powerbi_import.recovery_report import RecoveryReport


# ────────────────────────────────────────────────────────────────────
#  Helpers
# ────────────────────────────────────────────────────────────────────

def _make_state(pages=None, report_json=None, pages_metadata=None):
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
#  Registry
# ════════════════════════════════════════════════════════════════════

class TestRegistryPhase5(unittest.TestCase):
    def test_healer_registry_size(self):
        # 21 base healers + 2 filter-literal preheal (v3.7) + 1 Sprint 79
        # defensive visualType normalizer = 24 total.
        self.assertEqual(len(_REPORT_HEALERS), 24)


# ════════════════════════════════════════════════════════════════════
#  visual_overlap_full
# ════════════════════════════════════════════════════════════════════

class TestVisualOverlapFull(unittest.TestCase):
    def test_identical_position_staggered(self):
        v1 = _visual('v1', {'position': {'x': 100, 'y': 100, 'width': 400, 'height': 300}})
        v2 = _visual('v2', {'position': {'x': 100, 'y': 100, 'width': 400, 'height': 300}})
        state = _make_state([_page('p1', [v1, v2])])
        repairs = _heal_visual_overlap_full(state)
        self.assertEqual(repairs, 1)
        pos2 = v2['json']['position']
        self.assertEqual(pos2['x'], 132)
        self.assertEqual(pos2['y'], 132)

    def test_different_positions_untouched(self):
        v1 = _visual('v1', {'position': {'x': 0, 'y': 0, 'width': 400, 'height': 300}})
        v2 = _visual('v2', {'position': {'x': 500, 'y': 0, 'width': 400, 'height': 300}})
        state = _make_state([_page('p1', [v1, v2])])
        self.assertEqual(_heal_visual_overlap_full(state), 0)

    def test_no_position_skipped(self):
        v1 = _visual('v1', {})
        v2 = _visual('v2', {})
        state = _make_state([_page('p1', [v1, v2])])
        self.assertEqual(_heal_visual_overlap_full(state), 0)


# ════════════════════════════════════════════════════════════════════
#  visual_filter_unknown_field
# ════════════════════════════════════════════════════════════════════

class TestVisualFilterUnknownField(unittest.TestCase):
    def test_unknown_filter_removed(self):
        v = _visual('v1', {
            'visual': {
                'visualType': 'table',
                'query': {
                    'queryState': {
                        'Values': {
                            'projections': [
                                {'field': {'Column': {'Expression': {'SourceRef': {'Entity': 'T'}}, 'Property': 'Sales'}}}
                            ]
                        }
                    }
                }
            }
        })
        state = _make_state(
            [_page('p1', [v])],
            report_json={'filters': [{'name': 'NoSuchField'}, {'name': 'Sales'}]}
        )
        repairs = _heal_visual_filter_unknown_field(state)
        self.assertEqual(repairs, 1)
        self.assertEqual(len(state['report_json']['filters']), 1)
        self.assertEqual(state['report_json']['filters'][0]['name'], 'Sales')

    def test_no_visuals_no_repair(self):
        state = _make_state(report_json={'filters': [{'name': 'X'}]})
        self.assertEqual(_heal_visual_filter_unknown_field(state), 0)

    def test_no_filters_no_repair(self):
        v = _visual('v1', {
            'visual': {
                'visualType': 'table',
                'query': {'queryState': {'Values': {'projections': [
                    {'field': {'Column': {'Expression': {}, 'Property': 'A'}}}
                ]}}}
            }
        })
        state = _make_state([_page('p1', [v])])
        self.assertEqual(_heal_visual_filter_unknown_field(state), 0)


# ════════════════════════════════════════════════════════════════════
#  visual_query_unknown_measure
# ════════════════════════════════════════════════════════════════════

class TestVisualQueryUnknownMeasure(unittest.TestCase):
    def test_suspicious_ref_tagged(self):
        v = _visual('v1', {
            'visual': {
                'visualType': 'table',
                'query': {'queryState': {'Values': {'projections': [
                    {'field': {'Measure': {'Expression': {}, 'Property': '[ds.Sales]'}}}
                ]}}}
            }
        })
        state = _make_state([_page('p1', [v])])
        repairs = _heal_visual_query_unknown_measure(state)
        self.assertEqual(repairs, 1)
        # PBIR v4.0: annotations live at the visual.json root, not in visual.
        notes = v['json']['annotations']
        self.assertTrue(any(a['name'] == 'MigrationNote_BadRef' for a in notes))

    def test_clean_ref_untouched(self):
        v = _visual('v1', {
            'visual': {
                'visualType': 'table',
                'query': {'queryState': {'Values': {'projections': [
                    {'field': {'Measure': {'Expression': {}, 'Property': 'Total Sales'}}}
                ]}}}
            }
        })
        state = _make_state([_page('p1', [v])])
        self.assertEqual(_heal_visual_query_unknown_measure(state), 0)


# ════════════════════════════════════════════════════════════════════
#  slicer_targets_missing_field
# ════════════════════════════════════════════════════════════════════

class TestSlicerTargetsMissingField(unittest.TestCase):
    def test_slicer_no_projections_tagged(self):
        v = _visual('v1', {
            'visual': {
                'visualType': 'slicer',
                'query': {'queryState': {'Values': {'projections': []}}}
            }
        })
        state = _make_state([_page('p1', [v])])
        repairs = _heal_slicer_targets_missing_field(state)
        self.assertEqual(repairs, 1)

    def test_slicer_with_field_untouched(self):
        v = _visual('v1', {
            'visual': {
                'visualType': 'slicer',
                'query': {'queryState': {'Values': {'projections': [
                    {'field': {'Column': {'Property': 'Category'}}}
                ]}}}
            }
        })
        state = _make_state([_page('p1', [v])])
        self.assertEqual(_heal_slicer_targets_missing_field(state), 0)

    def test_non_slicer_ignored(self):
        v = _visual('v1', {
            'visual': {
                'visualType': 'table',
                'query': {'queryState': {'Values': {'projections': []}}}
            }
        })
        state = _make_state([_page('p1', [v])])
        self.assertEqual(_heal_slicer_targets_missing_field(state), 0)


# ════════════════════════════════════════════════════════════════════
#  bookmark_targets_missing_visual
# ════════════════════════════════════════════════════════════════════

class TestBookmarkTargetsMissingVisual(unittest.TestCase):
    def test_orphan_visual_state_removed(self):
        v = _visual('vis1', {})
        bm = {
            'name': 'bm1',
            'explorationState': {
                'visualStates': {
                    'vis1': {'expanded': True},
                    'gone': {'expanded': False},
                }
            }
        }
        state = _make_state([_page('p1', [v])], report_json={'bookmarks': [bm]})
        repairs = _heal_bookmark_targets_missing_visual(state)
        self.assertEqual(repairs, 1)
        self.assertNotIn('gone', bm['explorationState']['visualStates'])
        self.assertIn('vis1', bm['explorationState']['visualStates'])

    def test_no_orphans_no_repair(self):
        v = _visual('vis1', {})
        bm = {
            'name': 'bm1',
            'explorationState': {'visualStates': {'vis1': {'expanded': True}}}
        }
        state = _make_state([_page('p1', [v])], report_json={'bookmarks': [bm]})
        self.assertEqual(_heal_bookmark_targets_missing_visual(state), 0)


# ════════════════════════════════════════════════════════════════════
#  theme_dataColors_empty
# ════════════════════════════════════════════════════════════════════

class TestThemeDataColorsEmpty(unittest.TestCase):
    def test_empty_palette_filled(self):
        rj = {
            'resourcePackages': [{
                'items': [{
                    'content': {'theme': {'dataColors': []}}
                }]
            }]
        }
        state = _make_state(report_json=rj)
        repairs = _heal_theme_dataColors_empty(state)
        self.assertEqual(repairs, 1)
        colors = rj['resourcePackages'][0]['items'][0]['content']['theme']['dataColors']
        self.assertEqual(len(colors), 8)
        self.assertTrue(all(c.startswith('#') for c in colors))

    def test_non_empty_palette_untouched(self):
        rj = {
            'resourcePackages': [{
                'items': [{
                    'content': {'theme': {'dataColors': ['#FF0000']}}
                }]
            }]
        }
        state = _make_state(report_json=rj)
        self.assertEqual(_heal_theme_dataColors_empty(state), 0)

    def test_no_theme_no_crash(self):
        state = _make_state(report_json={'resourcePackages': [{}]})
        self.assertEqual(_heal_theme_dataColors_empty(state), 0)


# ════════════════════════════════════════════════════════════════════
#  page_no_visuals
# ════════════════════════════════════════════════════════════════════

class TestPageNoVisuals(unittest.TestCase):
    def test_empty_page_tagged(self):
        state = _make_state([_page('p1', [])])
        repairs = _heal_page_no_visuals(state)
        self.assertEqual(repairs, 1)
        annotations = state['pages'][0]['json']['annotations']
        self.assertTrue(any(a['name'] == 'MigrationNote_EmptyPage' for a in annotations))

    def test_page_with_visuals_untouched(self):
        v = _visual('v1', {})
        state = _make_state([_page('p1', [v])])
        self.assertEqual(_heal_page_no_visuals(state), 0)

    def test_idempotent(self):
        state = _make_state([_page('p1', [])])
        _heal_page_no_visuals(state)
        self.assertEqual(_heal_page_no_visuals(state), 0)


# ════════════════════════════════════════════════════════════════════
#  pagesmeta_duplicate_pageorder
# ════════════════════════════════════════════════════════════════════

class TestPagesmetaDuplicatePageorder(unittest.TestCase):
    def test_duplicates_removed(self):
        pm = {'pageOrder': ['p1', 'p2', 'p1', 'p3', 'p2']}
        state = _make_state(pages_metadata=pm)
        repairs = _heal_pagesmeta_duplicate_pageorder(state)
        self.assertEqual(repairs, 2)
        self.assertEqual(pm['pageOrder'], ['p1', 'p2', 'p3'])

    def test_no_duplicates_no_repair(self):
        pm = {'pageOrder': ['p1', 'p2']}
        state = _make_state(pages_metadata=pm)
        self.assertEqual(_heal_pagesmeta_duplicate_pageorder(state), 0)

    def test_single_entry_untouched(self):
        pm = {'pageOrder': ['p1']}
        state = _make_state(pages_metadata=pm)
        self.assertEqual(_heal_pagesmeta_duplicate_pageorder(state), 0)


# ════════════════════════════════════════════════════════════════════
#  tooltip_page_oversized
# ════════════════════════════════════════════════════════════════════

class TestTooltipPageOversized(unittest.TestCase):
    def test_oversized_tooltip_clamped(self):
        pj = {'displayName': 'Tip', 'pageType': 'Tooltip', 'width': 800, 'height': 600}
        state = _make_state([_page('p1', [], pj)])
        repairs = _heal_tooltip_page_oversized(state)
        self.assertEqual(repairs, 1)
        self.assertEqual(pj['width'], 480)
        self.assertEqual(pj['height'], 320)

    def test_correct_tooltip_untouched(self):
        pj = {'displayName': 'Tip', 'pageType': 'Tooltip', 'width': 480, 'height': 320}
        state = _make_state([_page('p1', [], pj)])
        self.assertEqual(_heal_tooltip_page_oversized(state), 0)

    def test_non_tooltip_ignored(self):
        pj = {'displayName': 'Main', 'width': 1280, 'height': 720}
        state = _make_state([_page('p1', [], pj)])
        self.assertEqual(_heal_tooltip_page_oversized(state), 0)


# ════════════════════════════════════════════════════════════════════
#  mobile_layout_orphan_visual
# ════════════════════════════════════════════════════════════════════

class TestMobileLayoutOrphanVisual(unittest.TestCase):
    def test_orphan_removed(self):
        v = _visual('v1', {})
        pj = {
            'displayName': 'p1',
            'mobileState': {
                'visuals': {
                    'v1': {'x': 0, 'y': 0},
                    'gone': {'x': 100, 'y': 100},
                }
            }
        }
        state = _make_state([_page('p1', [v], pj)])
        repairs = _heal_mobile_layout_orphan_visual(state)
        self.assertEqual(repairs, 1)
        self.assertNotIn('gone', pj['mobileState']['visuals'])
        self.assertIn('v1', pj['mobileState']['visuals'])

    def test_no_orphans_no_repair(self):
        v = _visual('v1', {})
        pj = {'displayName': 'p1', 'mobileState': {'visuals': {'v1': {'x': 0}}}}
        state = _make_state([_page('p1', [v], pj)])
        self.assertEqual(_heal_mobile_layout_orphan_visual(state), 0)

    def test_no_mobile_state_no_crash(self):
        state = _make_state([_page('p1', [_visual('v1')])])
        self.assertEqual(_heal_mobile_layout_orphan_visual(state), 0)


# ════════════════════════════════════════════════════════════════════
#  Phase 5 integration
# ════════════════════════════════════════════════════════════════════

class TestPhase5Integration(unittest.TestCase):
    def test_recovery_report_populated(self):
        v1 = _visual('v1', {'position': {'x': 0, 'y': 0, 'width': 400, 'height': 300}})
        v2 = _visual('v2', {'position': {'x': 0, 'y': 0, 'width': 400, 'height': 300}})
        state = _make_state([_page('p1', [v1, v2])])
        recovery = RecoveryReport('Test')
        _heal_visual_overlap_full(state, recovery=recovery)
        report = recovery.to_dict()
        self.assertGreater(len(report['repairs']), 0)

    def test_multiple_healers_compose(self):
        """Run all healers on a state with multiple issues."""
        v = _visual('v1', {
            'visual': {
                'visualType': 'slicer',
                'query': {'queryState': {'Values': {'projections': []}}}
            }
        })
        pj = {'displayName': 'Tip', 'pageType': 'Tooltip', 'width': 800, 'height': 600}
        pm = {'pageOrder': ['p1', 'p1'], 'activePageName': 'p1'}
        state = _make_state([_page('p1', [v], pj)], pages_metadata=pm)
        repairs = run_report_healers(state)
        self.assertGreaterEqual(repairs, 2)  # at least tooltip + pageorder


if __name__ == '__main__':
    unittest.main()
