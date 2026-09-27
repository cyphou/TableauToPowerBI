"""A model that cannot read its data still opens, and says nothing.

Seen in Power BI Desktop on a real migration: the report loads, the probe
calls it OK, and every visual draws its axes over an empty canvas under two
banners -- "One or more relationships have been modified" and "Some of the
tables have incomplete or no data" -- neither of which names a file.

This is a warning and never a blocker. A .twb carries no data at all, so the
files are frequently external by nature; only the person running the
migration can supply them.
"""
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from unittest import mock

from powerbi_import.openability import (
    CheckResult, OpenabilityReport, _check_data_files_present)
import powerbi_import.healing            # so at least one patch target exists


def _model(root, folder, refs, model_name='Sales.SemanticModel'):
    definition = os.path.join(root, model_name, 'definition')
    os.makedirs(os.path.join(definition, 'tables'))
    with open(os.path.join(definition, 'expressions.tmdl'), 'w',
              encoding='utf-8') as fh:
        fh.write('expression DataFolder = "%s" meta [IsParameterQuery=true]\n'
                 % folder.replace('\\', '\\\\'))
    for index, ref in enumerate(refs):
        with open(os.path.join(definition, 'tables', 't%d.tmdl' % index), 'w',
                  encoding='utf-8') as fh:
            fh.write('table T%d\n\tpartition P = m\n\t\tsource =\n'
                     '\t\t\tlet S = Csv.Document(File.Contents('
                     'DataFolder & "\\%s")) in S\n' % (index, ref))
    return definition


class TestDataFilesPresent(unittest.TestCase):

    def test_a_missing_file_is_named(self):
        with tempfile.TemporaryDirectory() as d:
            data = os.path.join(d, 'Data')
            os.makedirs(data)
            open(os.path.join(data, 'orders.csv'), 'w').close()
            _model(d, data, ['orders.csv', 'returns.csv'])
            result = _check_data_files_present(d)
            self.assertFalse(result.ok)
            self.assertIn('returns.csv', result.issues[0])
            self.assertNotIn('orders.csv', result.issues[0])

    def test_every_file_present_is_silent(self):
        with tempfile.TemporaryDirectory() as d:
            data = os.path.join(d, 'Data')
            os.makedirs(data)
            open(os.path.join(data, 'orders.csv'), 'w').close()
            _model(d, data, ['orders.csv'])
            self.assertTrue(_check_data_files_present(d).ok)

    def test_an_absent_data_folder_is_reported_once(self):
        with tempfile.TemporaryDirectory() as d:
            _model(d, os.path.join(d, 'Nowhere'), ['a.csv', 'b.csv', 'c.csv'])
            result = _check_data_files_present(d)
            self.assertFalse(result.ok)
            self.assertEqual(len(result.issues), 1)
            self.assertIn('does not exist', result.issues[0])

    def test_it_never_blocks(self):
        # The project opens; it just has nothing in it.
        with tempfile.TemporaryDirectory() as d:
            _model(d, os.path.join(d, 'Nowhere'), ['a.csv'])
            self.assertEqual(_check_data_files_present(d).severity, 'warning')

    def test_a_model_with_no_file_sources_is_silent(self):
        with tempfile.TemporaryDirectory() as d:
            _model(d, os.path.join(d, 'Data'), [])
            self.assertTrue(_check_data_files_present(d).ok)

    def test_no_semantic_model_at_all_is_silent(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(_check_data_files_present(d).ok)

    def test_the_backslashes_survive_the_round_trip(self):
        # TMDL doubles backslashes; reading them back naively points the
        # check at a path that never exists, so it would fire on everything.
        with tempfile.TemporaryDirectory() as d:
            data = os.path.join(d, 'Sub Dir', 'Data')
            os.makedirs(data)
            open(os.path.join(data, 'orders.csv'), 'w').close()
            _model(d, data, ['orders.csv'])
            self.assertTrue(_check_data_files_present(d).ok)

    def test_each_model_is_reported_separately(self):
        with tempfile.TemporaryDirectory() as d:
            _model(d, os.path.join(d, 'N1'), ['a.csv'], 'One.SemanticModel')
            _model(d, os.path.join(d, 'N2'), ['b.csv'], 'Two.SemanticModel')
            issues = _check_data_files_present(d).issues
            self.assertEqual(len(issues), 2)


class TestTheWarningReachesTheUser(unittest.TestCase):
    """Blocking issues were listed and warnings only counted.

    A warning whose whole value is the file name it carries is worthless as
    a number.
    """

    def _run(self, warnings):
        import migrate
        try:                       # migrate puts its own dir on sys.path
            import healing         # noqa: F401
        except ImportError:
            pass
        report = OpenabilityReport(project_dir='x')
        report.checks = [CheckResult('data_files_present', not warnings,
                                     'warning', warnings)]
        # _run_openability_gate imports the name locally and falls back
        # between two spellings of the same module, so whichever one wins
        # depends on what the rest of the suite has already imported.
        targets = [name for name in ('healing', 'powerbi_import.healing')
                   if name in sys.modules]
        self.assertTrue(targets, 'healing module never imported')
        buffer = StringIO()
        with ExitStack() as stack:
            for name in targets:
                stack.enter_context(mock.patch(
                    name + '.check_openability', return_value=report))
            d = stack.enter_context(tempfile.TemporaryDirectory())
            stack.enter_context(redirect_stdout(buffer))
            migrate._run_openability_gate(d)
        return buffer.getvalue()

    def test_the_warning_text_is_printed(self):
        out = self._run(['Sales: 1 of 2 data file(s) missing: returns.csv'])
        self.assertIn('returns.csv', out)

    def test_a_clean_project_stays_quiet(self):
        self.assertNotIn('•', self._run([]))

    def test_a_long_list_is_capped(self):
        out = self._run(['missing file_%02d.csv' % i for i in range(9)])
        self.assertIn('and 4 more', out)
        self.assertNotIn('file_08.csv', out)


if __name__ == '__main__':
    unittest.main()
