"""A bookmarks folder without its bookmarks.json index makes Power BI refuse
the whole project with a bare "Something went wrong", naming neither the
folder nor the bookmark. Six of ten sample projects were rejected this way.
"""
import json
import os
import tempfile
import unittest

from powerbi_import.openability import check_openability
from powerbi_import.pbip_generator import PowerBIProjectGenerator


def _bookmark(name, display='', options=None):
    bookmark = {
        'name': name,
        'displayName': display,
        'explorationState': {'version': '1.0', 'activeSection': 'ReportSection',
                             'sections': {'ReportSection': {}}},
    }
    if options is not None:
        bookmark['options'] = options
    return bookmark


class TestBookmarkIndexIsWritten(unittest.TestCase):

    def _write(self, bookmarks):
        gen = PowerBIProjectGenerator.__new__(PowerBIProjectGenerator)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        gen._write_bookmark_files(tmp.name, bookmarks)
        return tmp.name

    def _index(self, def_dir):
        path = os.path.join(def_dir, 'bookmarks', 'bookmarks.json')
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)

    def test_index_is_created(self):
        def_dir = self._write([_bookmark('Bookmark_a')])
        self.assertTrue(os.path.isfile(
            os.path.join(def_dir, 'bookmarks', 'bookmarks.json')))

    def test_index_lists_every_bookmark(self):
        def_dir = self._write([_bookmark('Bookmark_a'), _bookmark('Bookmark_b')])
        index = self._index(def_dir)
        names = [i['name'] for i in index['items']]
        self.assertEqual(names, ['Bookmark_a', 'Bookmark_b'])
        self.assertEqual(index['$schema'],
                         'https://developer.microsoft.com/json-schemas/fabric/item/'
                         'report/definition/bookmarksMetadata/1.0.0/schema.json')

    def test_index_declares_the_metadata_schema(self):
        def_dir = self._write([_bookmark('Bookmark_a')])
        self.assertEqual(
            self._index(def_dir)['$schema'],
            'https://developer.microsoft.com/json-schemas/fabric/item/report/'
            'definition/bookmarksMetadata/1.0.0/schema.json',
        )

    def test_each_bookmark_is_a_flat_file_in_the_indexed_folder(self):
        def_dir = self._write([_bookmark('Bookmark_a')])
        self.assertTrue(os.path.isfile(os.path.join(
            def_dir, 'bookmarks', 'Bookmark_a.bookmark.json')))
        self.assertFalse(os.path.exists(os.path.join(
            def_dir, 'bookmarks', 'Bookmark_a')))

    def test_an_empty_display_name_falls_back_to_the_id(self):
        # An empty displayName shows as a blank entry in the bookmark pane.
        def_dir = self._write([_bookmark('Bookmark_a')])
        path = os.path.join(def_dir, 'bookmarks', 'Bookmark_a.bookmark.json')
        with open(path, encoding='utf-8') as handle:
            self.assertEqual(json.load(handle)['displayName'], 'Bookmark_a')

    def test_a_real_display_name_is_kept(self):
        def_dir = self._write([_bookmark('Bookmark_a', 'Story point 1')])
        path = os.path.join(def_dir, 'bookmarks', 'Bookmark_a.bookmark.json')
        with open(path, encoding='utf-8') as handle:
            self.assertEqual(json.load(handle)['displayName'], 'Story point 1')

    def test_bookmark_schema_state_options_and_index_reference_are_preserved(self):
        state = {
            'version': '1.0',
            'activeSection': 'ReportSection99',
            'sections': {'ReportSection99': {'visualContainers': {'Visual A': {}}}},
        }
        options = {
            'applyOnlyToTargetVisuals': True,
            'targetVisualNames': ['Visual A'],
        }
        source = {
            'name': 'Bookmark_a',
            'displayName': 'Selected detail',
            'explorationState': state,
            'options': options,
        }
        def_dir = self._write([source])
        bookmark_path = os.path.join(
            def_dir, 'bookmarks', 'Bookmark_a.bookmark.json')
        with open(bookmark_path, encoding='utf-8') as handle:
            bookmark = json.load(handle)

        self.assertEqual(bookmark, {
            '$schema': 'https://developer.microsoft.com/json-schemas/fabric/item/'
            'report/definition/bookmark/2.1.0/schema.json',
            'name': 'Bookmark_a',
            'displayName': 'Selected detail',
            'explorationState': state,
            'options': options,
        })
        index = self._index(def_dir)
        self.assertEqual(index['items'], [{'name': 'Bookmark_a'}])
        self.assertTrue(os.path.isfile(bookmark_path))

    def test_fixed_source_bookmark_serializes_deterministically(self):
        source = _bookmark(
            'Bookmark_été',
            'Revue des ventes – Québec',
            options={'applyOnlyToTargetVisuals': True,
                     'targetVisualNames': ['Visual With Spaces']},
        )
        outputs = []
        for _ in range(2):
            def_dir = self._write([source])
            path = os.path.join(
                def_dir, 'bookmarks', 'Bookmark_été.bookmark.json')
            with open(path, encoding='utf-8') as handle:
                bookmark_text = handle.read()
            with open(os.path.join(
                    def_dir, 'bookmarks', 'bookmarks.json'),
                    encoding='utf-8') as handle:
                index_text = handle.read()
            outputs.append((bookmark_text, index_text))
        self.assertEqual(outputs[0], outputs[1])

    def test_no_bookmarks_writes_no_index(self):
        def_dir = self._write([])
        self.assertFalse(os.path.isfile(
            os.path.join(def_dir, 'bookmarks', 'bookmarks.json')))


class TestGateCatchesAMissingIndex(unittest.TestCase):

    def _project(self, with_index, layout='legacy', index_data=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        sm = os.path.join(root, 'P.SemanticModel', 'definition')
        os.makedirs(sm)
        with open(os.path.join(sm, 'model.tmdl'), 'w', encoding='utf-8') as f:
            f.write('model Model\n')
        bookmarks_dir = os.path.join(
            root, 'P.Report', 'definition', 'bookmarks')
        if layout == 'flat':
            os.makedirs(bookmarks_dir)
            bookmark_path = os.path.join(
                bookmarks_dir, 'Bookmark_a.bookmark.json')
        else:
            bookmark_dir = os.path.join(bookmarks_dir, 'Bookmark_a')
            os.makedirs(bookmark_dir)
            bookmark_path = os.path.join(bookmark_dir, 'bookmark.json')
        bookmark_data = _bookmark('Bookmark_a', 'Bookmark_a')
        if layout == 'flat':
            bookmark_data['$schema'] = (
                'https://developer.microsoft.com/json-schemas/fabric/item/report/'
                'definition/bookmark/2.1.0/schema.json'
            )
        with open(bookmark_path, 'w', encoding='utf-8') as f:
            json.dump(bookmark_data, f)
        if with_index:
            if index_data is None:
                index_data = {'items': [{'name': 'Bookmark_a'}]}
            with open(os.path.join(bookmarks_dir, 'bookmarks.json'), 'w',
                      encoding='utf-8') as f:
                json.dump(index_data, f)
        return root

    def _check(self, root):
        return [c for c in check_openability(root).checks
                if c.name == 'bookmarks_index'][0]

    def test_a_missing_index_is_blocking(self):
        for layout in ('legacy', 'flat'):
            with self.subTest(layout=layout):
                root = self._project(with_index=False, layout=layout)
                result = check_openability(root)
                check = self._check(root)
                self.assertFalse(check.ok)
                self.assertTrue(any('bookmarks.json' in issue
                                    for issue in check.issues))
                self.assertTrue(any('bookmarks_index' in issue
                                    for issue in result.blocking_issues))

    def test_minimal_legacy_bookmark_index_still_passes(self):
        # Historical nested fixtures intentionally omit the optional schema
        # field; the gate must continue accepting this legacy layout.
        self.assertTrue(self._check(self._project(with_index=True)).ok)

    def test_flat_index_references_resolve_to_flat_bookmark_files(self):
        index_data = {
            '$schema': 'https://developer.microsoft.com/json-schemas/fabric/item/'
            'report/definition/bookmarksMetadata/1.0.0/schema.json',
            'items': [{'name': 'Bookmark_a'}],
        }
        root = self._project(
            with_index=True, layout='flat', index_data=index_data)
        self.assertTrue(self._check(root).ok)

    def test_invalid_index_shape_has_a_specific_error(self):
        index_data = {
            '$schema': 'https://developer.microsoft.com/json-schemas/fabric/item/'
            'report/definition/bookmarksMetadata/1.0.0/schema.json',
            'items': {'name': 'Bookmark_a'},
        }
        check = self._check(self._project(
            with_index=True, layout='flat', index_data=index_data))
        self.assertFalse(check.ok)
        self.assertTrue(any(
            'items' in issue.lower() or 'shape' in issue.lower()
            for issue in check.issues
        ), check.issues)

    def test_invalid_bookmark_name_has_a_specific_error(self):
        index_data = {
            '$schema': 'https://developer.microsoft.com/json-schemas/fabric/item/'
            'report/definition/bookmarksMetadata/1.0.0/schema.json',
            'items': [{'name': ''}],
        }
        check = self._check(self._project(
            with_index=True, layout='flat', index_data=index_data))
        self.assertFalse(check.ok)
        self.assertTrue(any(
            'name' in issue.lower() or 'invalid' in issue.lower()
            for issue in check.issues
        ), check.issues)

    def test_index_reference_to_missing_bookmark_has_a_specific_error(self):
        index_data = {
            '$schema': 'https://developer.microsoft.com/json-schemas/fabric/item/'
            'report/definition/bookmarksMetadata/1.0.0/schema.json',
            'items': [{'name': 'Missing target'}],
        }
        check = self._check(self._project(
            with_index=True, layout='flat', index_data=index_data))
        self.assertFalse(check.ok)
        self.assertTrue(any(
            'Missing target' in issue for issue in check.issues
        ), check.issues)

    def test_a_project_without_bookmarks_passes(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = temporary.name
        sm = os.path.join(root, 'P.SemanticModel', 'definition')
        os.makedirs(sm)
        with open(os.path.join(sm, 'model.tmdl'), 'w', encoding='utf-8') as f:
            f.write('model Model\n')
        self.assertTrue(self._check(root).ok)


class TestBookmarkSectionNamesARealPage(unittest.TestCase):
    """A section naming no page makes Power BI refuse the project."""

    def _make(self, story_points, pages):
        gen = PowerBIProjectGenerator.__new__(PowerBIProjectGenerator)
        stories = [{'name': 'Story', 'story_points': story_points}]
        return gen._create_bookmarks(stories, pages)

    def _sections(self, bookmarks):
        return [b['explorationState']['activeSection'] for b in bookmarks]

    def test_an_empty_captured_sheet_falls_back_to_a_real_page(self):
        # dict.get returns the empty value when the key exists, so the
        # default never applied and the section was written as ''.
        marks = self._make([{'caption': 'p1', 'captured_sheet': ''}],
                           ['ReportSection', 'ReportSection99'])
        self.assertEqual(self._sections(marks), ['ReportSection'])

    def test_a_missing_captured_sheet_falls_back(self):
        marks = self._make([{'caption': 'p1'}], ['ReportSectionAA'])
        self.assertEqual(self._sections(marks), ['ReportSectionAA'])

    def test_a_tableau_sheet_name_is_not_used_as_a_page(self):
        marks = self._make([{'caption': 'p1', 'captured_sheet': 'Sales Sheet'}],
                           ['ReportSection'])
        self.assertEqual(self._sections(marks), ['ReportSection'])

    def test_a_real_page_name_is_kept(self):
        marks = self._make([{'caption': 'p1',
                             'captured_sheet': 'ReportSection99'}],
                           ['ReportSection', 'ReportSection99'])
        self.assertEqual(self._sections(marks), ['ReportSection99'])

    def test_the_section_key_matches_the_active_section(self):
        marks = self._make([{'caption': 'p1', 'captured_sheet': ''}],
                           ['ReportSectionZ'])
        state = marks[0]['explorationState']
        self.assertEqual(list(state['sections']), [state['activeSection']])

    def test_no_pages_yet_uses_the_conventional_first_page(self):
        marks = self._make([{'caption': 'p1', 'captured_sheet': ''}], [])
        self.assertEqual(self._sections(marks), ['ReportSection'])


if __name__ == '__main__':
    unittest.main()
