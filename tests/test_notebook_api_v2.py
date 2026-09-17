"""Tests for the Notebook API v2 interactive layer (Sprint 187).

Covers: assess_interactive radar widget, explore_dax filterable table,
show_relationships Mermaid diagram, step-by-step migration phases, and the
rich-display helper objects/renderers.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))

from powerbi_import.notebook_api import (
    MigrationSession,
    _NotebookDisplay,
    _DaxExplorer,
    _category_scores,
    _render_radar_svg,
    _collect_relationships,
    _render_mermaid_er,
    _render_mermaid_html,
    _html_escape,
    _mermaid_id,
)


def _session_with_data():
    s = MigrationSession()
    s._extracted = {
        'calculations': [
            {'name': 'Total Sales', 'formula': 'SUM([Sales])'},
            {'name': 'Profit Ratio', 'formula': 'SUM([Profit]) / SUM([Sales])'},
            {'name': 'Empty', 'formula': ''},
        ],
        'datasources': [
            {
                'name': 'DS1',
                'tables': [
                    {'name': 'Orders', 'columns': [{'name': 'Sales'}, {'name': 'Profit'}]},
                    {'name': 'Customers', 'columns': [{'name': 'CustomerID'}]},
                ],
                'relationships': [
                    {
                        'from_table': 'Orders', 'to_table': 'Customers',
                        'cardinality': 'manyToOne',
                        'from_column': 'CustomerID', 'to_column': 'CustomerID',
                    },
                ],
            }
        ],
        'worksheets': [
            {'name': 'Sales Chart', 'mark_type': 'bar', 'fields': [{'name': 'Sales'}]},
        ],
    }
    return s


# ── _category_scores ──────────────────────────────────────────

class TestCategoryScores(unittest.TestCase):
    def test_pass_warn_fail_ratio(self):
        assessment = {
            'categories': [
                {'name': 'Datasource', 'checks': [
                    {'severity': 'pass'}, {'severity': 'fail'},
                ]},
            ]
        }
        scores = _category_scores(assessment)
        self.assertAlmostEqual(scores['Datasource'], 0.5)

    def test_info_severity(self):
        assessment = {'categories': [{'name': 'X', 'checks': [{'severity': 'info'}]}]}
        scores = _category_scores(assessment)
        self.assertAlmostEqual(scores['X'], 0.85)

    def test_empty_checks_uses_worst_severity(self):
        assessment = {'categories': [{'name': 'Y', 'worst_severity': 'warn', 'checks': []}]}
        scores = _category_scores(assessment)
        self.assertAlmostEqual(scores['Y'], 0.5)

    def test_no_categories(self):
        self.assertEqual(_category_scores({}), {})


# ── assess_interactive ────────────────────────────────────────

class TestAssessInteractive(unittest.TestCase):
    def test_returns_display_object(self):
        s = _session_with_data()
        widget = s.assess_interactive()
        self.assertIsInstance(widget, _NotebookDisplay)

    def test_html_contains_svg(self):
        s = _session_with_data()
        html = s.assess_interactive()._repr_html_()
        self.assertIn('<svg', html)

    def test_to_dict_has_scores(self):
        s = _session_with_data()
        data = s.assess_interactive().to_dict()
        self.assertIn('scores', data)
        self.assertIn('assessment', data)


class TestRenderRadar(unittest.TestCase):
    def test_empty_scores(self):
        html = _render_radar_svg({})
        self.assertIn('No assessment', html)

    def test_polygon_present(self):
        html = _render_radar_svg({'A': 1.0, 'B': 0.5, 'C': 0.0})
        self.assertIn('<polygon', html)
        self.assertIn('</svg>', html)

    def test_labels_rendered(self):
        html = _render_radar_svg({'Datasource': 0.8, 'Visuals': 0.4})
        self.assertIn('Datasource', html)
        self.assertIn('Visuals', html)

    def test_clamps_out_of_range(self):
        # Should not raise on values outside 0..1
        html = _render_radar_svg({'A': 1.5, 'B': -0.5})
        self.assertIn('<svg', html)


# ── explore_dax ───────────────────────────────────────────────

class TestExploreDax(unittest.TestCase):
    def test_returns_explorer(self):
        s = _session_with_data()
        self.assertIsInstance(s.explore_dax(), _DaxExplorer)

    def test_rows_have_confidence_and_note(self):
        s = _session_with_data()
        rows = s.explore_dax().to_dict()
        self.assertTrue(rows)
        for r in rows:
            self.assertIn('confidence', r)
            self.assertIn('migration_note', r)
            self.assertIsInstance(r['confidence'], float)

    def test_overridden_confidence(self):
        s = _session_with_data()
        s.edit_dax('Total Sales', 'CUSTOM()')
        rows = s.explore_dax().to_dict()
        ts = [r for r in rows if r['name'] == 'Total Sales'][0]
        self.assertEqual(ts['status'], 'overridden')
        self.assertEqual(ts['confidence'], 1.0)
        self.assertIn('override', ts['migration_note'].lower())

    def test_filter_method(self):
        s = _session_with_data()
        explorer = s.explore_dax()
        filtered = explorer.filter('exact')
        self.assertIsInstance(filtered, _DaxExplorer)
        for r in filtered:
            self.assertEqual(r['status'], 'exact')

    def test_status_arg(self):
        s = _session_with_data()
        rows = s.explore_dax(status='exact').to_dict()
        for r in rows:
            self.assertEqual(r['status'], 'exact')

    def test_html_table(self):
        s = _session_with_data()
        html = s.explore_dax()._repr_html_()
        self.assertIn('<table', html)
        self.assertIn('Confidence', html)

    def test_empty_html(self):
        explorer = _DaxExplorer([])
        self.assertIn('No DAX', explorer._repr_html_())

    def test_len_and_iter(self):
        s = _session_with_data()
        explorer = s.explore_dax()
        self.assertEqual(len(explorer), len(list(explorer)))

    def test_to_frame_no_pandas_fallback(self):
        # to_frame returns rows list when pandas missing; either way iterable
        explorer = _DaxExplorer([{'name': 'x', 'status': 'exact', 'confidence': 1.0,
                                  'tableau_formula': '', 'dax_formula': '',
                                  'migration_note': ''}])
        frame = explorer.to_frame()
        self.assertTrue(hasattr(frame, '__len__'))


# ── show_relationships ────────────────────────────────────────

class TestShowRelationships(unittest.TestCase):
    def test_returns_display(self):
        s = _session_with_data()
        self.assertIsInstance(s.show_relationships(), _NotebookDisplay)

    def test_mermaid_source(self):
        s = _session_with_data()
        data = s.show_relationships().to_dict()
        self.assertIn('mermaid', data)
        self.assertIn('erDiagram', data['mermaid'])
        self.assertIn('Orders', data['mermaid'])
        self.assertIn('Customers', data['mermaid'])

    def test_html_has_mermaid_div(self):
        s = _session_with_data()
        html = s.show_relationships()._repr_html_()
        self.assertIn('class="mermaid"', html)


class TestRelationshipHelpers(unittest.TestCase):
    def test_collect_dedup(self):
        extracted = {'datasources': [{'relationships': [
            {'from_table': 'A', 'to_table': 'B'},
            {'from_table': 'A', 'to_table': 'B'},
        ]}]}
        rels = _collect_relationships(extracted)
        self.assertEqual(len(rels), 1)

    def test_collect_skips_incomplete(self):
        extracted = {'datasources': [{'relationships': [
            {'from_table': 'A'},
        ]}]}
        self.assertEqual(_collect_relationships(extracted), [])

    def test_mermaid_er_empty(self):
        src = _render_mermaid_er([])
        self.assertIn('No relationships', src)

    def test_mermaid_cardinality_mapping(self):
        src = _render_mermaid_er([
            {'from_table': 'A', 'to_table': 'B', 'cardinality': 'manyToOne',
             'from_column': 'k'},
        ])
        self.assertIn('}o--||', src)

    def test_mermaid_id_sanitises(self):
        self.assertEqual(_mermaid_id('My Table!'), 'My_Table')

    def test_mermaid_html_wraps(self):
        html = _render_mermaid_html('erDiagram')
        self.assertIn('mermaid', html)


# ── step-by-step ──────────────────────────────────────────────

class TestStepByStep(unittest.TestCase):
    def test_step_extract_uses_loaded(self):
        s = _session_with_data()
        counts = s.step_extract()
        self.assertIn('calculations', counts)
        self.assertEqual(counts['calculations'], 3)

    def test_step_extract_requires_data(self):
        s = MigrationSession()
        with self.assertRaises(RuntimeError):
            s.step_extract()

    def test_step_convert_returns_previews(self):
        s = _session_with_data()
        result = s.step_convert()
        self.assertIn('dax', result)
        self.assertIn('visuals', result)
        self.assertIn('dax_count', result)
        self.assertGreaterEqual(result['dax_count'], 2)
        self.assertIn('approximated', result)

    def test_step_convert_requires_load(self):
        s = MigrationSession()
        with self.assertRaises(RuntimeError):
            s.step_convert()


# ── display helpers ───────────────────────────────────────────

class TestDisplayHelpers(unittest.TestCase):
    def test_notebook_display_repr(self):
        d = _NotebookDisplay('<b>hi</b>', {'k': 1}, text='hi')
        self.assertEqual(d._repr_html_(), '<b>hi</b>')
        self.assertEqual(repr(d), 'hi')
        self.assertEqual(d.to_dict(), {'k': 1})

    def test_html_escape(self):
        self.assertEqual(_html_escape('<a>&"'), '&lt;a&gt;&amp;&quot;')


if __name__ == '__main__':
    unittest.main()
