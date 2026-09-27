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


def _bookmark(name, display=''):
    return {
        'name': name,
        'displayName': display,
        'explorationState': {'version': '1.0', 'activeSection': 'ReportSection',
                             'sections': {'ReportSection': {}}},
    }


class TestBookmarkIndexIsWritten(unittest.TestCase):

    def _write(self, bookmarks):
        gen = PowerBIProjectGenerator.__new__(PowerBIProjectGenerator)
        tmp = tempfile.mkdtemp()
        gen._write_bookmark_files(tmp, bookmarks)
        return tmp

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
        names = [i['name'] for i in self._index(def_dir)['items']]
        self.assertEqual(names, ['Bookmark_a', 'Bookmark_b'])

    def test_index_declares_the_metadata_schema(self):
        def_dir = self._write([_bookmark('Bookmark_a')])
        self.assertIn('bookmarksMetadata', self._index(def_dir)['$schema'])

    def test_each_bookmark_still_gets_its_own_file(self):
        def_dir = self._write([_bookmark('Bookmark_a')])
        self.assertTrue(os.path.isfile(os.path.join(
            def_dir, 'bookmarks', 'Bookmark_a', 'bookmark.json')))

    def test_an_empty_display_name_falls_back_to_the_id(self):
        # An empty displayName shows as a blank entry in the bookmark pane.
        def_dir = self._write([_bookmark('Bookmark_a')])
        path = os.path.join(def_dir, 'bookmarks', 'Bookmark_a', 'bookmark.json')
        with open(path, encoding='utf-8') as handle:
            self.assertEqual(json.load(handle)['displayName'], 'Bookmark_a')

    def test_a_real_display_name_is_kept(self):
        def_dir = self._write([_bookmark('Bookmark_a', 'Story point 1')])
        path = os.path.join(def_dir, 'bookmarks', 'Bookmark_a', 'bookmark.json')
        with open(path, encoding='utf-8') as handle:
            self.assertEqual(json.load(handle)['displayName'], 'Story point 1')

    def test_no_bookmarks_writes_no_index(self):
        def_dir = self._write([])
        self.assertFalse(os.path.isfile(
            os.path.join(def_dir, 'bookmarks', 'bookmarks.json')))


class TestGateCatchesAMissingIndex(unittest.TestCase):

    def _project(self, with_index):
        root = tempfile.mkdtemp()
        sm = os.path.join(root, 'P.SemanticModel', 'definition')
        os.makedirs(sm)
        with open(os.path.join(sm, 'model.tmdl'), 'w', encoding='utf-8') as f:
            f.write('model Model\n')
        bm = os.path.join(root, 'P.Report', 'definition', 'bookmarks',
                          'Bookmark_a')
        os.makedirs(bm)
        with open(os.path.join(bm, 'bookmark.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(_bookmark('Bookmark_a'), f)
        if with_index:
            parent = os.path.dirname(bm)
            with open(os.path.join(parent, 'bookmarks.json'), 'w',
                      encoding='utf-8') as f:
                json.dump({'items': [{'name': 'Bookmark_a'}]}, f)
        return root

    def _check(self, root):
        return [c for c in check_openability(root).checks
                if c.name == 'bookmarks_index'][0]

    def test_a_missing_index_is_blocking(self):
        result = check_openability(self._project(with_index=False))
        self.assertFalse(self._check(self._project(with_index=False)).ok)
        self.assertTrue(any('bookmarks_index' in b
                            for b in result.blocking_issues))

    def test_an_index_passes(self):
        self.assertTrue(self._check(self._project(with_index=True)).ok)

    def test_a_project_without_bookmarks_passes(self):
        root = tempfile.mkdtemp()
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
