"""DataFolder must point where the data actually is.

The derivation prefixed a drive letter onto whatever Tableau named, so an
already-rooted path came out corrupt: C:\\Users\\alice\\data became
C:\\C:\\Users\\alice\\data. That path never exists, so a workbook whose data
was still on the machine could never load it -- the migration silently fell
back to an empty local Data folder instead.

Worse for a share: \\\\server\\bi became C:\\server\\bi, a local path that is
wrong rather than merely absent, and would read the wrong files if it ever
happened to exist.

Measured on 14 real workbooks: 13 directories named, 5 UNC and 4
drive-absolute -- so 9 of 13 were corrupted.
"""
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace

from powerbi_import.tmdl_writers import (
    _common_ancestor, _data_folder_from, _write_expressions_tmdl)

_FOLDER = re.compile(r'expression DataFolder = "((?:[^"\\]|\\.)*)"')


class TestRootedPathsAreKept(unittest.TestCase):

    def test_a_drive_letter_is_not_added_twice(self):
        self.assertEqual(_data_folder_from([r'C:/Users/alice/data']),
                         r'C:\Users\alice\data')

    def test_another_drive_survives(self):
        self.assertEqual(_data_folder_from([r'D:/shared/bi']), r'D:\shared\bi')

    def test_a_share_stays_a_share(self):
        # C:\server\bi is not "close enough" -- it is a different machine.
        self.assertEqual(_data_folder_from(['//fileserver/bi']),
                         r'\\fileserver\bi')

    def test_a_backslash_share_stays_a_share(self):
        self.assertEqual(_data_folder_from([r'\\\\fileserver\\bi'.replace(
            '\\\\', '\\')]), r'\\fileserver\bi')


class TestUnrootedPathsStillMapToC(unittest.TestCase):
    """Unchanged behaviour: it is what makes a Mac-authored workbook usable."""

    def test_a_posix_path_is_read_off_the_c_drive(self):
        self.assertEqual(_data_folder_from(['/Users/alice/data']),
                         r'C:\Users\alice\data')

    def test_a_relative_path_is_read_off_the_c_drive(self):
        self.assertEqual(_data_folder_from(['data/exports']),
                         r'C:\data\exports')

    def test_nothing_named_yields_nothing(self):
        self.assertEqual(_data_folder_from([]), '')
        self.assertEqual(_data_folder_from(['', None]), '')


class TestCommonAncestor(unittest.TestCase):

    def test_the_shared_parent_is_used(self):
        self.assertEqual(
            _data_folder_from([r'C:/bi/2024/raw', r'C:/bi/2025/raw']),
            r'C:\bi')

    def test_it_never_cuts_mid_segment(self):
        # A character-wise prefix of /data/2024 and /data/2025 is /data/202,
        # a directory that does not exist.
        self.assertEqual(_common_ancestor(['/data/2024', '/data/2025']),
                         '/data')

    def test_unrelated_roots_fall_back_to_the_first(self):
        self.assertEqual(
            _data_folder_from([r'D:/bi/raw', '//server/share/raw']),
            r'D:\bi\raw')

    def test_one_directory_is_used_as_is(self):
        self.assertEqual(_common_ancestor(['/data/raw']), '/data/raw')


class TestWrittenExpression(unittest.TestCase):

    def _write(self, filename):
        tables = [{'partitions': [{'source': {'expression':
                   'let S = Csv.Document(File.Contents('
                   'DataFolder & "\\orders.csv")) in S'}}]}]
        datasources = [{'connection': {}, 'connection_map': {
            'c1': {'details': {'filename': filename}}}}]
        with tempfile.TemporaryDirectory() as d:
            _write_expressions_tmdl(d, tables, datasources)
            text = open(os.path.join(d, 'expressions.tmdl'),
                        encoding='utf-8').read()
        return _FOLDER.search(text).group(1).replace('\\\\', '\\')

    def test_a_windows_source_round_trips(self):
        self.assertEqual(self._write(r'C:\Users\alice\data\orders.csv'),
                         r'C:\Users\alice\data')

    def test_a_share_round_trips(self):
        self.assertEqual(self._write(r'\\fileserver\bi\orders.csv'),
                         r'\\fileserver\bi')

    def test_no_file_source_keeps_the_placeholder(self):
        tables = [{'partitions': [{'source': {'expression':
                   'let S = Sql.Database("srv", "db") in S'}}]}]
        with tempfile.TemporaryDirectory() as d:
            _write_expressions_tmdl(d, tables, [{'connection': {}}])
            text = open(os.path.join(d, 'expressions.tmdl'),
                        encoding='utf-8').read()
        self.assertEqual(_FOLDER.search(text).group(1).replace('\\\\', '\\'),
                         r'C:\Data')


class TestTheTwbFallbackRunsInBothModes(unittest.TestCase):
    """Batch created a local Data folder and told the user; single did not.

    _extract_twbx_data_files returned early for anything not .twbx, so a
    single .twb migration shipped a DataFolder pointing nowhere, with no
    hint of where to put the data. Same workbook, two answers.
    """

    def _project(self, root, folder):
        project = os.path.join(root, 'Sales')
        definition = os.path.join(project, 'Sales.SemanticModel', 'definition')
        os.makedirs(definition)
        with open(os.path.join(definition, 'expressions.tmdl'), 'w',
                  encoding='utf-8') as fh:
            fh.write('expression DataFolder = "%s" meta [Type="Text"]\n'
                     % folder.replace('\\', '\\\\'))
        return project, definition

    def _folder_after(self, args_source, root, folder):
        import migrate
        self._project(root, folder)
        args = SimpleNamespace(tableau_file=args_source, output_dir=root,
                               output_format='pbip')
        buffer = StringIO()
        with redirect_stdout(buffer):
            migrate._extract_twbx_data_files(args, 'Sales')
        definition = os.path.join(root, 'Sales', 'Sales.SemanticModel',
                                  'definition')
        text = open(os.path.join(definition, 'expressions.tmdl'),
                    encoding='utf-8').read()
        return _FOLDER.search(text).group(1).replace('\\\\', '\\'), \
            buffer.getvalue()

    def test_a_single_twb_gets_its_data_folder(self):
        with tempfile.TemporaryDirectory() as d:
            folder, out = self._folder_after('book.twb', d, r'C:\Nowhere')
            self.assertEqual(folder,
                             os.path.abspath(os.path.join(d, 'Sales', 'Data')))
            self.assertTrue(os.path.isdir(folder))
            self.assertIn('Place your data files in', out)

    def test_a_folder_that_exists_is_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, 'real')
            os.makedirs(real)
            folder, _out = self._folder_after('book.twb', d, real)
            self.assertEqual(folder, real)

    def test_the_path_shown_is_not_the_tmdl_escaped_one(self):
        with tempfile.TemporaryDirectory() as d:
            _folder, out = self._folder_after('book.twb', d, r'C:\Nowhere')
            self.assertNotIn('\\\\', out)


if __name__ == '__main__':
    unittest.main()
