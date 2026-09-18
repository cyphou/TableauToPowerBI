"""The corpus gate must fail when the corpus regresses.

A release gate that only ever passes is a rubber stamp, so these tests feed it
crafted evidence and assert it reports each floor being breached.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from scripts.check_corpus_gate import _collect


class _EvidenceDir(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _project(self, name, openable=True, issues=None, with_pbip=True):
        project = os.path.join(self.dir, name)
        os.makedirs(project, exist_ok=True)
        if with_pbip:
            open(os.path.join(project, f'{name}.pbip'), 'w').close()
        if openable is not None:
            with open(os.path.join(project, 'openability_report.json'), 'w',
                      encoding='utf-8') as fh:
                json.dump({'openable': openable,
                           'blocking_issues': issues or []}, fh)

    def _quality(self, name, status='PASS', blockers=None):
        path = os.path.join(self.dir, f'migration_quality_{name}.json')
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'status': status, 'blockers': blockers or []}, fh)


class TestGateAcceptsAHealthyCorpus(_EvidenceDir):
    def test_openable_project_with_no_blockers(self):
        self._project('Sales')
        self._quality('Sales')

        evidence = _collect(self.dir)

        self.assertEqual(evidence['openable'], ['Sales'])
        self.assertEqual(evidence['not_openable'], [])
        self.assertEqual(evidence['missing_gate'], [])
        self.assertEqual(evidence['blockers'], [])

    def test_warnings_do_not_count_as_blockers(self):
        self._project('Sales')
        self._quality('Sales', status='WARN')

        evidence = _collect(self.dir)

        self.assertEqual(evidence['blockers'], [])
        self.assertEqual(evidence['statuses'], {'WARN': 1})


class TestGateRejectsARegressedCorpus(_EvidenceDir):
    def test_project_that_will_not_open_is_reported(self):
        self._project('Broken', openable=False, issues=['invalid TMDL'])

        evidence = _collect(self.dir)

        self.assertEqual(evidence['openable'], [])
        self.assertEqual(len(evidence['not_openable']), 1)
        project, issues = evidence['not_openable'][0]
        self.assertEqual(project, 'Broken')
        self.assertIn('invalid TMDL', issues)

    def test_blocker_is_reported(self):
        self._project('Sales')
        self._quality('Sales', status='FAIL', blockers=['semantic model invalid'])

        evidence = _collect(self.dir)

        self.assertEqual(evidence['blockers'],
                         [('Sales', 'semantic model invalid')])

    def test_pbip_without_an_openability_verdict_is_reported(self):
        """A silently skipped gate must not read as a pass."""
        self._project('Unverified', openable=None)

        evidence = _collect(self.dir)

        self.assertEqual(evidence['missing_gate'], ['Unverified'])

    def test_unreadable_verdict_is_reported(self):
        project = os.path.join(self.dir, 'Corrupt')
        os.makedirs(project)
        open(os.path.join(project, 'Corrupt.pbip'), 'w').close()
        with open(os.path.join(project, 'openability_report.json'), 'w',
                  encoding='utf-8') as fh:
            fh.write('{ not json')

        evidence = _collect(self.dir)

        self.assertIn('Corrupt', evidence['missing_gate'])


class TestGateIgnoresNonProjectOutput(_EvidenceDir):
    def test_prep_flow_output_needs_no_openability_verdict(self):
        """Prep flows produce Power Query, not a .pbip, so there is nothing to open."""
        flow = os.path.join(self.dir, '01_Clean_Orders')
        os.makedirs(os.path.join(flow, 'PowerQuery'))
        open(os.path.join(flow, 'PowerQuery', 'out.pq'), 'w').close()

        evidence = _collect(self.dir)

        self.assertEqual(evidence['missing_gate'], [])
        self.assertEqual(evidence['not_openable'], [])


if __name__ == '__main__':
    unittest.main()
