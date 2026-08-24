"""Sprint 146 — Cross-artifact validator (Phase 6).

Tests ``powerbi_import/cross_validator.py`` checks that bridge the
TMDL semantic model and the PBIR report.
"""

import unittest

from powerbi_import.cross_validator import (
    CrossValidationResult,
    cross_validate,
    _build_model_index,
    _check_relationships,
    _check_rls,
    _check_visual_refs,
    _check_orphans,
    _extract_visual_refs,
)


# ────────────────────────────────────────────────────────────────────
#  Helpers
# ────────────────────────────────────────────────────────────────────

def _model(tables=None, relationships=None, roles=None):
    return {
        'model': {
            'tables': tables or [],
            'relationships': relationships or [],
            'roles': roles or [],
        }
    }


def _table(name, columns=None, measures=None):
    return {
        'name': name,
        'columns': [{'name': c, 'dataType': 'string'} for c in (columns or [])],
        'measures': [{'name': m, 'expression': 'SUM(1)'} for m in (measures or [])],
        'partitions': [],
    }


def _report_state(pages=None):
    return {
        'def_dir': '/tmp/fake/definition',
        'pages_dir': '/tmp/fake/definition/pages',
        'report_json': {},
        'pages_metadata': {'pageOrder': []},
        'pages': pages or [],
        '_dirty_files': set(),
    }


def _page(name, visuals=None):
    return {
        'dir': f'/tmp/fake/definition/pages/{name}',
        'name': name,
        'json': {},
        'visuals': visuals or [],
    }


def _visual_with_column(vid, table, column):
    return {
        'dir': f'/tmp/fake/definition/pages/p1/visuals/{vid}',
        'name': vid,
        'json': {
            'visual': {
                'visualType': 'table',
                'query': {'queryState': {'Values': {'projections': [
                    {'field': {'Column': {
                        'Expression': {'SourceRef': {'Entity': table}},
                        'Property': column,
                    }}}
                ]}}}
            }
        },
    }


def _visual_with_measure(vid, table, measure):
    return {
        'dir': f'/tmp/fake/definition/pages/p1/visuals/{vid}',
        'name': vid,
        'json': {
            'visual': {
                'visualType': 'card',
                'query': {'queryState': {'Values': {'projections': [
                    {'field': {'Measure': {
                        'Expression': {'SourceRef': {'Entity': table}},
                        'Property': measure,
                    }}}
                ]}}}
            }
        },
    }


# ════════════════════════════════════════════════════════════════════
#  Model index
# ════════════════════════════════════════════════════════════════════

class TestBuildModelIndex(unittest.TestCase):
    def test_basic_index(self):
        m = _model([_table('Sales', ['Amount', 'Date'], ['Total'])])
        tables, cols, meas = _build_model_index(m)
        self.assertEqual(tables, {'Sales'})
        self.assertEqual(cols['Sales'], {'Amount', 'Date'})
        self.assertEqual(meas['Sales'], {'Total'})

    def test_empty_model(self):
        tables, cols, meas = _build_model_index({'model': {}})
        self.assertEqual(tables, set())


# ════════════════════════════════════════════════════════════════════
#  Visual → Model
# ════════════════════════════════════════════════════════════════════

class TestVisualRefs(unittest.TestCase):
    def test_valid_column_ref(self):
        m = _model([_table('Sales', ['Amount'])])
        v = _visual_with_column('v1', 'Sales', 'Amount')
        rs = _report_state([_page('p1', [v])])
        tables, cols, meas = _build_model_index(m)
        issues = _check_visual_refs(rs, tables, cols, meas)
        self.assertEqual(len(issues), 0)

    def test_unknown_table_ref(self):
        m = _model([_table('Sales', ['Amount'])])
        v = _visual_with_column('v1', 'NoSuchTable', 'Amount')
        rs = _report_state([_page('p1', [v])])
        tables, cols, meas = _build_model_index(m)
        issues = _check_visual_refs(rs, tables, cols, meas)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, 'error')
        self.assertIn('NoSuchTable', issues[0].message)

    def test_unknown_column_ref(self):
        m = _model([_table('Sales', ['Amount'])])
        v = _visual_with_column('v1', 'Sales', 'NoSuchCol')
        rs = _report_state([_page('p1', [v])])
        tables, cols, meas = _build_model_index(m)
        issues = _check_visual_refs(rs, tables, cols, meas)
        self.assertEqual(len(issues), 1)
        self.assertIn('NoSuchCol', issues[0].message)

    def test_valid_measure_ref(self):
        m = _model([_table('Sales', [], ['Total'])])
        v = _visual_with_measure('v1', 'Sales', 'Total')
        rs = _report_state([_page('p1', [v])])
        tables, cols, meas = _build_model_index(m)
        issues = _check_visual_refs(rs, tables, cols, meas)
        self.assertEqual(len(issues), 0)

    def test_unknown_measure_ref(self):
        m = _model([_table('Sales', [], ['Total'])])
        v = _visual_with_measure('v1', 'Sales', 'NoMeasure')
        rs = _report_state([_page('p1', [v])])
        tables, cols, meas = _build_model_index(m)
        issues = _check_visual_refs(rs, tables, cols, meas)
        self.assertEqual(len(issues), 1)

    def test_measure_used_as_column_ok(self):
        """Measure referenced via Column block (happens with some visuals)."""
        m = _model([_table('Sales', [], ['Total'])])
        v = _visual_with_column('v1', 'Sales', 'Total')
        rs = _report_state([_page('p1', [v])])
        tables, cols, meas = _build_model_index(m)
        issues = _check_visual_refs(rs, tables, cols, meas)
        self.assertEqual(len(issues), 0)  # falls back to measure set


# ════════════════════════════════════════════════════════════════════
#  Relationships
# ════════════════════════════════════════════════════════════════════

class TestRelationships(unittest.TestCase):
    def test_valid_relationship(self):
        m = _model(
            [_table('Sales', ['ProductID']), _table('Products', ['ID'])],
            [{'fromTable': 'Sales', 'fromColumn': 'ProductID',
              'toTable': 'Products', 'toColumn': 'ID'}],
        )
        tables, cols, _ = _build_model_index(m)
        issues = _check_relationships(m, tables, cols)
        self.assertEqual(len(issues), 0)

    def test_missing_from_table(self):
        m = _model(
            [_table('Products', ['ID'])],
            [{'fromTable': 'Gone', 'fromColumn': 'X',
              'toTable': 'Products', 'toColumn': 'ID'}],
        )
        tables, cols, _ = _build_model_index(m)
        issues = _check_relationships(m, tables, cols)
        self.assertTrue(any('Gone' in i.message for i in issues))

    def test_missing_column(self):
        m = _model(
            [_table('Sales', ['Amount']), _table('Products', ['ID'])],
            [{'fromTable': 'Sales', 'fromColumn': 'NoCol',
              'toTable': 'Products', 'toColumn': 'ID'}],
        )
        tables, cols, _ = _build_model_index(m)
        issues = _check_relationships(m, tables, cols)
        self.assertEqual(len(issues), 1)
        self.assertIn('NoCol', issues[0].message)


# ════════════════════════════════════════════════════════════════════
#  RLS
# ════════════════════════════════════════════════════════════════════

class TestRLS(unittest.TestCase):
    def test_valid_rls(self):
        m = _model(
            [_table('Sales', ['Region'])],
            roles=[{'name': 'RegionRole', 'tablePermissions': [
                {'name': 'Sales', 'filterExpression': '[Region] = "West"'}
            ]}],
        )
        tables, _, _ = _build_model_index(m)
        issues = _check_rls(m, tables)
        self.assertEqual(len(issues), 0)

    def test_rls_missing_table(self):
        m = _model(
            [_table('Sales', ['Region'])],
            roles=[{'name': 'BadRole', 'tablePermissions': [
                {'name': 'NonExistentTable', 'filterExpression': 'TRUE()'}
            ]}],
        )
        tables, _, _ = _build_model_index(m)
        issues = _check_rls(m, tables)
        self.assertEqual(len(issues), 1)
        self.assertIn('NonExistentTable', issues[0].message)

    def test_empty_roles_ok(self):
        m = _model([_table('T', ['C'])], roles=[])
        tables, _, _ = _build_model_index(m)
        self.assertEqual(len(_check_rls(m, tables)), 0)


# ════════════════════════════════════════════════════════════════════
#  Orphan detection
# ════════════════════════════════════════════════════════════════════

class TestOrphans(unittest.TestCase):
    def test_unused_measure_warning(self):
        m = _model([_table('Sales', ['Amount'], ['Total', 'Unused'])])
        v = _visual_with_measure('v1', 'Sales', 'Total')
        rs = _report_state([_page('p1', [v])])
        tables, cols, meas = _build_model_index(m)
        issues = _check_orphans(rs, tables, cols, meas)
        self.assertEqual(len(issues), 1)
        self.assertIn('Unused', issues[0].message)
        self.assertEqual(issues[0].severity, 'warning')

    def test_all_used_no_warnings(self):
        m = _model([_table('Sales', [], ['Total'])])
        v = _visual_with_measure('v1', 'Sales', 'Total')
        rs = _report_state([_page('p1', [v])])
        tables, cols, meas = _build_model_index(m)
        issues = _check_orphans(rs, tables, cols, meas)
        self.assertEqual(len(issues), 0)

    def test_calendar_skipped(self):
        m = _model([_table('Calendar', [], ['YearMeasure'])])
        rs = _report_state([_page('p1', [])])
        tables, cols, meas = _build_model_index(m)
        issues = _check_orphans(rs, tables, cols, meas)
        self.assertEqual(len(issues), 0)  # Calendar is skipped


# ════════════════════════════════════════════════════════════════════
#  cross_validate (integration)
# ════════════════════════════════════════════════════════════════════

class TestCrossValidate(unittest.TestCase):
    def test_clean_model_and_report(self):
        m = _model([_table('Sales', ['Amount'], ['Total'])])
        v = _visual_with_measure('v1', 'Sales', 'Total')
        rs = _report_state([_page('p1', [v])])
        result = cross_validate(m, rs)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.errors), 0)

    def test_model_only(self):
        m = _model(
            [_table('Sales', ['Amount'])],
            [{'fromTable': 'Sales', 'fromColumn': 'Amount',
              'toTable': 'Gone', 'toColumn': 'X'}],
        )
        result = cross_validate(m)
        self.assertFalse(result.ok)
        self.assertGreater(len(result.errors), 0)

    def test_empty_model(self):
        result = cross_validate({})
        self.assertTrue(result.ok)

    def test_none_model(self):
        result = cross_validate(None)
        self.assertTrue(result.ok)

    def test_to_dict(self):
        m = _model([_table('Sales', ['Amount'], ['Total'])])
        result = cross_validate(m)
        d = result.to_dict()
        self.assertIn('ok', d)
        self.assertIn('error_count', d)
        self.assertIn('issues', d)

    def test_multiple_issues(self):
        m = _model(
            [_table('Sales', ['Amount'])],
            [{'fromTable': 'Sales', 'fromColumn': 'Bad',
              'toTable': 'Sales', 'toColumn': 'Amount'}],
            roles=[{'name': 'R', 'tablePermissions': [
                {'name': 'Gone', 'filterExpression': 'TRUE()'}
            ]}],
        )
        v = _visual_with_column('v1', 'NoTable', 'X')
        rs = _report_state([_page('p1', [v])])
        result = cross_validate(m, rs)
        self.assertFalse(result.ok)
        categories = {i.category for i in result.issues}
        self.assertIn('relationship', categories)
        self.assertIn('rls', categories)
        self.assertIn('visual', categories)


# ════════════════════════════════════════════════════════════════════
#  Extract visual refs
# ════════════════════════════════════════════════════════════════════

class TestExtractVisualRefs(unittest.TestCase):
    def test_column_ref(self):
        vj = {
            'visual': {
                'query': {'queryState': {'Values': {'projections': [
                    {'field': {'Column': {
                        'Expression': {'SourceRef': {'Entity': 'T'}},
                        'Property': 'C',
                    }}}
                ]}}}
            }
        }
        refs = _extract_visual_refs(vj)
        self.assertEqual(refs, [('T', 'C', 'column')])

    def test_measure_ref(self):
        vj = {
            'visual': {
                'query': {'queryState': {'Values': {'projections': [
                    {'field': {'Measure': {
                        'Expression': {'SourceRef': {'Entity': 'T'}},
                        'Property': 'M',
                    }}}
                ]}}}
            }
        }
        refs = _extract_visual_refs(vj)
        self.assertEqual(refs, [('T', 'M', 'measure')])

    def test_aggregation_ref(self):
        vj = {
            'visual': {
                'query': {'queryState': {'Values': {'projections': [
                    {'field': {'Aggregation': {
                        'Expression': {'Column': {
                            'Expression': {'SourceRef': {'Entity': 'T'}},
                            'Property': 'Amount',
                        }},
                        'Function': 0,
                    }}}
                ]}}}
            }
        }
        refs = _extract_visual_refs(vj)
        self.assertEqual(refs, [('T', 'Amount', 'column')])

    def test_empty_visual(self):
        self.assertEqual(_extract_visual_refs({}), [])
        self.assertEqual(_extract_visual_refs({'visual': {}}), [])


if __name__ == '__main__':
    unittest.main()
