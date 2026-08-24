"""Sprint 147 — PBI Desktop schema validator (Phase 7).

Tests ``powerbi_import/schema_validator.py`` deep structural checks.
"""

import json
import os
import tempfile
import unittest

from powerbi_import.schema_validator import (
    EXPECTED_SCHEMAS,
    KNOWN_VISUAL_TYPES,
    SchemaIssue,
    SchemaResult,
    detect_artifact_type,
    validate_artifact,
    validate_report_dir,
    _schema_version,
    _schema_family,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _report(**overrides):
    base = {
        '$schema': EXPECTED_SCHEMAS['report'],
        'themeCollection': {
            'baseTheme': {'name': 'CY26SU04', 'reportVersionAtImport': '5.58', 'type': 'SharedResources'}
        },
        'settings': {'hideVisualContainerHeader': True},
    }
    base.update(overrides)
    return base


def _page(**overrides):
    base = {
        '$schema': EXPECTED_SCHEMAS['page'],
        'name': 'ReportSection',
        'displayName': 'Page 1',
        'width': 1280,
        'height': 720,
    }
    base.update(overrides)
    return base


def _visual(**overrides):
    base = {
        '$schema': EXPECTED_SCHEMAS['visual'],
        'name': 'abc123def456abc12345',
        'position': {'x': 0, 'y': 0, 'z': 0, 'width': 400, 'height': 300, 'tabOrder': 0},
        'visual': {'visualType': 'clusteredBarChart', 'drillFilterOtherVisuals': True},
    }
    base.update(overrides)
    return base


def _bookmark(**overrides):
    base = {
        '$schema': EXPECTED_SCHEMAS['bookmark'],
        'name': 'Bookmark_abc123',
        'displayName': 'My Bookmark',
        'explorationState': {
            'version': '1.0',
            'activeSection': 'ReportSection',
        },
    }
    base.update(overrides)
    return base


def _pages_meta(**overrides):
    base = {
        '$schema': EXPECTED_SCHEMAS['pages_metadata'],
        'pageOrder': ['ReportSection'],
        'activePageName': 'ReportSection',
    }
    base.update(overrides)
    return base


# ════════════════════════════════════════════════════════════════════
#  Schema URL helpers
# ════════════════════════════════════════════════════════════════════

class TestSchemaHelpers(unittest.TestCase):
    def test_schema_version(self):
        self.assertEqual(_schema_version(EXPECTED_SCHEMAS['visual']), '2.7.0')
        self.assertEqual(_schema_version(EXPECTED_SCHEMAS['report']), '2.0.0')
        self.assertIsNone(_schema_version(''))

    def test_schema_family(self):
        self.assertEqual(_schema_family(EXPECTED_SCHEMAS['visual']), 'visual')
        self.assertEqual(_schema_family(EXPECTED_SCHEMAS['page']), 'page')
        self.assertEqual(_schema_family(EXPECTED_SCHEMAS['report']), 'report')
        self.assertEqual(_schema_family(EXPECTED_SCHEMAS['bookmark']), 'bookmark')
        self.assertEqual(_schema_family(EXPECTED_SCHEMAS['pages_metadata']), 'pages_metadata')
        self.assertEqual(_schema_family(EXPECTED_SCHEMAS['version']), 'version')
        self.assertEqual(_schema_family(EXPECTED_SCHEMAS['definition_pbir']), 'definition_pbir')
        self.assertIsNone(_schema_family(''))


class TestDetectArtifactType(unittest.TestCase):
    def test_from_visual(self):
        self.assertEqual(detect_artifact_type(_visual()), 'visual')

    def test_from_page(self):
        self.assertEqual(detect_artifact_type(_page()), 'page')

    def test_from_report(self):
        self.assertEqual(detect_artifact_type(_report()), 'report')

    def test_no_schema(self):
        self.assertIsNone(detect_artifact_type({'foo': 'bar'}))

    def test_non_dict(self):
        self.assertIsNone(detect_artifact_type([1, 2]))


# ════════════════════════════════════════════════════════════════════
#  Report validation
# ════════════════════════════════════════════════════════════════════

class TestReportValidation(unittest.TestCase):
    def test_valid_report(self):
        r = validate_artifact(_report(), 'report')
        self.assertTrue(r.ok)
        self.assertEqual(len(r.errors), 0)

    def test_missing_schema(self):
        data = _report()
        del data['$schema']
        r = validate_artifact(data, 'report')
        self.assertFalse(r.ok)

    def test_bad_theme_collection(self):
        r = validate_artifact(_report(themeCollection='bad'), 'report')
        self.assertTrue(any(i.path == 'themeCollection' for i in r.issues))

    def test_bad_settings(self):
        r = validate_artifact(_report(settings='bad'), 'report')
        self.assertTrue(any(i.path == 'settings' for i in r.issues))

    def test_bad_filter_config(self):
        r = validate_artifact(_report(filterConfig='bad'), 'report')
        self.assertTrue(any(i.path == 'filterConfig' for i in r.issues))

    def test_filters_not_list(self):
        r = validate_artifact(_report(filterConfig={'filters': 'bad'}), 'report')
        self.assertTrue(any('list' in i.message for i in r.issues))


# ════════════════════════════════════════════════════════════════════
#  Page validation
# ════════════════════════════════════════════════════════════════════

class TestPageValidation(unittest.TestCase):
    def test_valid_page(self):
        r = validate_artifact(_page(), 'page')
        self.assertTrue(r.ok)

    def test_missing_name(self):
        data = _page()
        data['name'] = ''
        r = validate_artifact(data, 'page')
        self.assertFalse(r.ok)
        self.assertTrue(any(i.path == 'name' for i in r.issues))

    def test_missing_display_name(self):
        data = _page()
        data['displayName'] = ''
        r = validate_artifact(data, 'page')
        self.assertFalse(r.ok)

    def test_string_width_coerced(self):
        data = _page(width='1280')
        r = validate_artifact(data, 'page')
        self.assertTrue(r.ok)
        self.assertEqual(data['width'], 1280)  # auto-repaired
        self.assertTrue(any(i.repaired for i in r.issues))

    def test_negative_height(self):
        r = validate_artifact(_page(height=-100), 'page')
        self.assertFalse(r.ok)

    def test_zero_width(self):
        r = validate_artifact(_page(width=0), 'page')
        self.assertFalse(r.ok)

    def test_unknown_page_type(self):
        r = validate_artifact(_page(pageType='BadType'), 'page')
        self.assertTrue(any(i.path == 'pageType' for i in r.issues))

    def test_valid_page_types(self):
        for pt in ('Tooltip', 'Drillthrough'):
            r = validate_artifact(_page(pageType=pt), 'page')
            self.assertTrue(r.ok, f'pageType {pt} should be valid')


# ════════════════════════════════════════════════════════════════════
#  Visual validation
# ════════════════════════════════════════════════════════════════════

class TestVisualValidation(unittest.TestCase):
    def test_valid_visual(self):
        r = validate_artifact(_visual(), 'visual')
        self.assertTrue(r.ok)
        self.assertEqual(len(r.errors), 0)

    def test_missing_schema(self):
        data = _visual()
        del data['$schema']
        r = validate_artifact(data, 'visual')
        self.assertFalse(r.ok)

    def test_string_position_coerced(self):
        data = _visual()
        data['position']['x'] = '10'
        data['position']['y'] = '20'
        r = validate_artifact(data, 'visual')
        self.assertTrue(r.ok)
        self.assertEqual(data['position']['x'], 10)
        self.assertEqual(data['position']['y'], 20)

    def test_negative_width(self):
        data = _visual()
        data['position']['width'] = -100
        r = validate_artifact(data, 'visual')
        self.assertFalse(r.ok)

    def test_unknown_visual_type_warning(self):
        data = _visual()
        data['visual']['visualType'] = 'totallyCustomVisual'
        r = validate_artifact(data, 'visual')
        self.assertTrue(r.ok)  # warning, not error
        self.assertTrue(any(i.severity == 'warning' and 'Unknown' in i.message
                           for i in r.issues))

    def test_known_visual_types(self):
        for vt in ('clusteredBarChart', 'lineChart', 'table', 'slicer', 'card'):
            data = _visual()
            data['visual']['visualType'] = vt
            r = validate_artifact(data, 'visual')
            self.assertTrue(r.ok, f'{vt} should be valid')

    def test_bad_visual_block(self):
        r = validate_artifact(_visual(visual='not_an_object'), 'visual')
        self.assertFalse(r.ok)

    def test_bad_query(self):
        data = _visual()
        data['visual']['query'] = 'bad'
        r = validate_artifact(data, 'visual')
        self.assertFalse(r.ok)

    def test_bad_query_state(self):
        data = _visual()
        data['visual']['query'] = {'queryState': 'bad'}
        r = validate_artifact(data, 'visual')
        self.assertFalse(r.ok)

    def test_position_not_dict(self):
        r = validate_artifact(_visual(position='bad'), 'visual')
        self.assertFalse(r.ok)

    def test_non_numeric_string_position(self):
        data = _visual()
        data['position']['x'] = 'abc'
        r = validate_artifact(data, 'visual')
        self.assertFalse(r.ok)


# ════════════════════════════════════════════════════════════════════
#  Bookmark validation
# ════════════════════════════════════════════════════════════════════

class TestBookmarkValidation(unittest.TestCase):
    def test_valid_bookmark(self):
        r = validate_artifact(_bookmark(), 'bookmark')
        self.assertTrue(r.ok)

    def test_missing_name(self):
        data = _bookmark()
        data['name'] = ''
        r = validate_artifact(data, 'bookmark')
        self.assertFalse(r.ok)

    def test_empty_display_name(self):
        data = _bookmark()
        data['displayName'] = ''
        r = validate_artifact(data, 'bookmark')
        # warning, not error
        self.assertTrue(r.ok)
        self.assertTrue(any(i.severity == 'warning' for i in r.issues))

    def test_bad_exploration_state(self):
        r = validate_artifact(_bookmark(explorationState='bad'), 'bookmark')
        self.assertFalse(r.ok)


# ════════════════════════════════════════════════════════════════════
#  Pages metadata
# ════════════════════════════════════════════════════════════════════

class TestPagesMetadataValidation(unittest.TestCase):
    def test_valid(self):
        r = validate_artifact(_pages_meta(), 'pages_metadata')
        self.assertTrue(r.ok)

    def test_missing_page_order(self):
        data = _pages_meta()
        del data['pageOrder']
        r = validate_artifact(data, 'pages_metadata')
        self.assertFalse(r.ok)

    def test_page_order_not_list(self):
        r = validate_artifact(_pages_meta(pageOrder='bad'), 'pages_metadata')
        self.assertFalse(r.ok)

    def test_duplicate_page_order(self):
        r = validate_artifact(_pages_meta(pageOrder=['A', 'A']), 'pages_metadata')
        self.assertTrue(any(i.severity == 'warning' and 'Duplicate' in i.message
                           for i in r.issues))


# ════════════════════════════════════════════════════════════════════
#  SchemaResult
# ════════════════════════════════════════════════════════════════════

class TestSchemaResult(unittest.TestCase):
    def test_ok_empty(self):
        r = SchemaResult(artifact_type='report')
        self.assertTrue(r.ok)
        self.assertEqual(r.to_dict()['ok'], True)

    def test_ok_after_repair(self):
        r = SchemaResult(artifact_type='page', issues=[
            SchemaIssue('error', 'width', 'Coerced', repaired=True),
        ])
        self.assertTrue(r.ok)  # repaired errors don't block

    def test_not_ok_with_error(self):
        r = SchemaResult(artifact_type='page', issues=[
            SchemaIssue('error', 'name', 'Missing'),
        ])
        self.assertFalse(r.ok)

    def test_to_dict(self):
        r = SchemaResult(artifact_type='visual', file_path='/tmp/v.json', issues=[
            SchemaIssue('warning', 'visual.visualType', 'Unknown', repaired=False),
        ])
        d = r.to_dict()
        self.assertEqual(d['artifact_type'], 'visual')
        self.assertEqual(d['warning_count'], 1)
        self.assertEqual(d['error_count'], 0)
        self.assertEqual(len(d['issues']), 1)

    def test_repairs_property(self):
        r = SchemaResult(artifact_type='page', issues=[
            SchemaIssue('warning', 'w', 'coerced', repaired=True),
            SchemaIssue('error', 'h', 'bad', repaired=False),
        ])
        self.assertEqual(len(r.repairs), 1)


# ════════════════════════════════════════════════════════════════════
#  Null / invalid inputs
# ════════════════════════════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):
    def test_none_input(self):
        r = validate_artifact(None, 'report')
        self.assertFalse(r.ok)

    def test_list_input(self):
        r = validate_artifact([], 'page')
        self.assertFalse(r.ok)

    def test_unknown_artifact_type(self):
        r = validate_artifact({'$schema': 'x'}, 'unknown_type')
        # warning but ok (no validator)
        self.assertTrue(r.ok)
        self.assertTrue(any(i.severity == 'warning' for i in r.issues))

    def test_schema_family_mismatch(self):
        """Report $schema URL in a visual artifact should flag mismatch."""
        data = _visual()
        data['$schema'] = EXPECTED_SCHEMAS['report']
        r = validate_artifact(data, 'visual')
        self.assertTrue(any('mismatch' in i.message.lower() for i in r.issues))


# ════════════════════════════════════════════════════════════════════
#  validate_report_dir
# ════════════════════════════════════════════════════════════════════

class TestValidateReportDir(unittest.TestCase):
    def test_missing_dir(self):
        results = validate_report_dir('/nonexistent/dir')
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)

    def test_valid_dir(self):
        with tempfile.TemporaryDirectory() as td:
            # Write a valid report.json
            with open(os.path.join(td, 'report.json'), 'w') as f:
                json.dump(_report(), f)
            # Write a valid page.json in pages/p1/
            p1 = os.path.join(td, 'pages', 'p1')
            os.makedirs(p1)
            with open(os.path.join(p1, 'page.json'), 'w') as f:
                json.dump(_page(), f)

            results = validate_report_dir(td)
            self.assertEqual(len(results), 2)
            self.assertTrue(all(r.ok for r in results))

    def test_invalid_json_file(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, 'broken.json'), 'w') as f:
                f.write('{not valid json')

            results = validate_report_dir(td)
            self.assertEqual(len(results), 1)
            self.assertFalse(results[0].ok)

    def test_non_pbir_json_skipped(self):
        """JSON files without a $schema are skipped."""
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, 'custom.json'), 'w') as f:
                json.dump({'foo': 'bar'}, f)

            results = validate_report_dir(td)
            self.assertEqual(len(results), 0)


if __name__ == '__main__':
    unittest.main()
