"""Embedded data files must end up where the generated M looks for them.

The M asks for ``DataFolder & "\\basename"``. A file left in a .twbx subfolder
is never found: File.Contents fails, the try/otherwise fallback yields an
empty table and every visual renders blank.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from migrate import _flatten_data_files

_EXT = {'.xlsx', '.csv', '.geojson'}


def _write(path, content=b'x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(content)


class TestFlattenDataFiles(unittest.TestCase):

    def test_file_in_a_subfolder_is_moved_up(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, '1- Sources', 'sales.xlsx'))
            moved = _flatten_data_files(d, _EXT)
            self.assertEqual(moved, ['sales.xlsx'])
            self.assertTrue(os.path.isfile(os.path.join(d, 'sales.xlsx')))

    def test_files_from_several_subfolders_all_land_in_the_root(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, 'a', 'one.csv'))
            _write(os.path.join(d, 'b deep', 'two.xlsx'))
            _flatten_data_files(d, _EXT)
            for name in ('one.csv', 'two.xlsx'):
                self.assertTrue(os.path.isfile(os.path.join(d, name)), name)

    def test_file_already_in_the_root_is_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, 'sales.xlsx'), b'root')
            moved = _flatten_data_files(d, _EXT)
            self.assertEqual(moved, [])
            with open(os.path.join(d, 'sales.xlsx'), 'rb') as f:
                self.assertEqual(f.read(), b'root')

    def test_colliding_basename_is_left_in_place(self):
        # The reference would be ambiguous; binding the wrong file silently
        # is worse than leaving it unresolved.
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, 'sales.xlsx'), b'root')
            _write(os.path.join(d, 'other', 'sales.xlsx'), b'nested')
            moved = _flatten_data_files(d, _EXT)
            self.assertEqual(moved, [])
            with open(os.path.join(d, 'sales.xlsx'), 'rb') as f:
                self.assertEqual(f.read(), b'root')
            self.assertTrue(os.path.isfile(os.path.join(d, 'other', 'sales.xlsx')))

    def test_non_data_extensions_are_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            _write(os.path.join(d, 'sub', 'notes.txt'))
            self.assertEqual(_flatten_data_files(d, _EXT), [])
            self.assertFalse(os.path.exists(os.path.join(d, 'notes.txt')))

    def test_missing_directory_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                _flatten_data_files(os.path.join(d, 'nope'), _EXT), [])


if __name__ == '__main__':
    unittest.main()
