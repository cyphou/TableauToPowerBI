"""The corpus gate must fail when the corpus regresses.

A release gate that only ever passes is a rubber stamp, so these tests feed it
crafted evidence and assert it reports each floor being breached, and that the
reviewed list of known limitations can excuse exactly what it records and
nothing else.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import scripts.check_corpus_gate as gate
from scripts.check_corpus_gate import (
    BATCHES,
    EXIT_BATCH_PARTIAL_FAIL,
    STATE_FILE,
    BaselineError,
    _account,
    _artefacts,
    _collect,
    _hyper_reader_problem,
    load_baseline,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


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


def _limit(category='hyper_import', details=('Required Hyper checks failed',),
           blockers=('needs Hyper',), reason='a legacy extract that the Hyper API refuses'):
    """One reviewed known limitation, shaped as load_baseline returns it."""
    return {'category': category, 'details': list(details),
            'blockers': list(blockers), 'reason': reason}


class _Batch(_EvidenceDir):
    """A source corpus and the output a migration of it left behind.

    ``self.dir`` is the batch's output directory; ``self.source`` its corpus.
    """

    def setUp(self):
        super().setUp()
        self.source = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.source, ignore_errors=True)
        self.base, self.dir = self.dir, os.path.join(self.dir, 'batch')
        os.makedirs(self.dir)
        self.state = {}

    def _artefact(self, name):
        path = os.path.join(self.source, *name.split('/'))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, 'w').close()

    def _record(self, artefact, status='success', category='', details='',
                 report=None, key=None):
        result = {}
        if report:
            result['quality_json'] = os.path.join(
                self.dir, f'migration_quality_{report}.json')
        if status == 'failed':
            result.update(error=category, error_details=details)
        self.state[key or artefact] = {
            'status': status,
            'error': category if status == 'failed' else '',
            'result': result,
        }

    def _healthy(self, stem, suffix='.twbx'):
        self._artefact(stem + suffix)
        self._project(stem)
        self._quality(stem)
        self._record(stem + suffix, report=stem)
        return stem + suffix

    def _failed(self, artefact, category='hyper_import', details='binding failed',
                blockers=()):
        self._artefact(artefact)
        stem = os.path.splitext(os.path.basename(artefact))[0]
        if blockers:
            self._quality(stem, status='FAIL', blockers=list(blockers))
        self._record(artefact, 'failed', category, details,
                      report=stem if blockers else None)

    def _write_state(self, text=None):
        with open(os.path.join(self.dir, STATE_FILE), 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(self.state) if text is None else text)

    def _judge(self, exit_code=0, baseline=None, state=True):
        if state:
            self._write_state()
        return _account('batch', self.source, self.dir, exit_code,
                        _collect(self.dir), baseline or {})


class TestAccountingAcceptsAHealthyBatch(_Batch):
    def test_every_artefact_migrated(self):
        self._healthy('Sales')
        self._healthy('Finance', '.twb')

        verdict = self._judge()

        self.assertEqual(verdict.failures, [])
        self.assertEqual(verdict.acknowledged, [])
        self.assertEqual((verdict.artefacts, verdict.migrated, verdict.blockers),
                         (2, 2, 0))

    def test_nested_artefact_is_matched_whatever_the_state_separator(self):
        self._artefact('sub/Nested.twb')
        self._project('Nested')
        self._quality('Nested')
        self._record('sub/Nested.twb', report='Nested', key='sub\\Nested.twb')

        self.assertEqual(self._judge().failures, [])

    def test_warnings_never_fail(self):
        self._healthy('Sales')
        self._quality('Sales', status='WARN')

        self.assertEqual(self._judge().failures, [])


class TestAccountingItemisesFailures(_Batch):
    def test_failed_artefact_is_named_with_its_category_and_details(self):
        self._healthy('Sales')
        self._failed('Broken.twbx', 'hyper_import', 'Required Hyper binding checks failed')

        verdict = self._judge(EXIT_BATCH_PARTIAL_FAIL)

        self.assertEqual(len(verdict.failures), 1)
        self.assertIn('batch/Broken.twbx', verdict.failures[0])
        self.assertIn('hyper_import', verdict.failures[0])
        self.assertIn('Required Hyper binding checks failed', verdict.failures[0])
        self.assertEqual((verdict.artefacts, verdict.migrated), (2, 1))

    def test_details_are_clipped_to_one_line(self):
        self._failed('Broken.twbx', details='first line\n' + 'x' * 1000)

        failure = self._judge(EXIT_BATCH_PARTIAL_FAIL).failures[0]

        self.assertNotIn('\n', failure)
        self.assertLess(len(failure), 400)

    def test_failure_without_a_category_reads_unknown(self):
        self._artefact('Broken.twbx')
        self.state['Broken.twbx'] = {'status': 'failed', 'result': {}}

        failure = self._judge(EXIT_BATCH_PARTIAL_FAIL).failures[0]

        self.assertIn('failed (unknown)', failure)

    def test_failure_recorded_by_the_exception_path_shows_its_message(self):
        self._artefact('Crashed.twbx')
        self.state['Crashed.twbx'] = {
            'status': 'failed', 'error': '',
            'result': {'error': 'division by zero', 'error_category': 'exception'}}

        failure = self._judge(EXIT_BATCH_PARTIAL_FAIL).failures[0]

        self.assertIn('division by zero', failure)


class TestAccountingChecksTheExitCode(_Batch):
    def test_partial_failure_exit_without_a_failed_artefact_is_reported(self):
        self._healthy('Sales')

        failures = self._judge(EXIT_BATCH_PARTIAL_FAIL).failures

        self.assertEqual(len(failures), 1)
        self.assertIn('exited 7, expected 0', failures[0])

    def test_clean_exit_with_a_failed_artefact_is_reported(self):
        self._failed('Broken.twbx')

        failures = self._judge(0).failures

        self.assertTrue(any('exited 0, expected 7' in text for text in failures), failures)

    def test_any_other_exit_code_is_reported(self):
        self._failed('Broken.twbx')

        failures = self._judge(1).failures

        self.assertTrue(any('exited 1, expected 7' in text for text in failures), failures)


class TestAccountingNeedsTheBatchState(_Batch):
    def test_missing_state_is_reported_and_excuses_nothing(self):
        self._healthy('Sales')
        self._quality('Sales', status='FAIL', blockers=['model invalid'])

        text = '\n'.join(self._judge(EXIT_BATCH_PARTIAL_FAIL, state=False).failures)

        self.assertIn('no readable batch state', text)
        self.assertIn('migration exited 7', text)
        self.assertIn('blocker — model invalid', text)

    def test_unusable_state_is_reported(self):
        for content in ('{ not json', '[]', '{}', '"text"'):
            with self.subTest(content=content):
                self._healthy('Sales')
                self._write_state(content)

                failures = self._judge(state=False).failures

                self.assertEqual(len(failures), 1)
                self.assertIn('no readable batch state', failures[0])

    def test_artefact_without_an_outcome_is_reported(self):
        self._healthy('Sales')
        self._artefact('Forgotten.twbx')

        failures = self._judge().failures

        self.assertEqual(len(failures), 1)
        self.assertIn('batch/Forgotten.twbx', failures[0])
        self.assertIn('no outcome', failures[0])

    def test_outcome_for_an_artefact_the_corpus_lacks_is_reported(self):
        self._healthy('Sales')
        self._record('Ghost.twbx')

        failures = self._judge().failures

        self.assertEqual(len(failures), 1)
        self.assertIn('batch/Ghost.twbx', failures[0])
        self.assertIn('stale batch state', failures[0])

    def test_unfinished_artefact_is_reported(self):
        self._healthy('Sales')
        self._artefact('Hung.twbx')
        self._record('Hung.twbx', 'running')

        failures = self._judge(EXIT_BATCH_PARTIAL_FAIL).failures

        self.assertEqual(len(failures), 1)
        self.assertIn('batch/Hung.twbx', failures[0])
        self.assertIn("never finished (status 'running')", failures[0])

    def test_corpus_without_artefacts_is_reported(self):
        failures = self._judge(1, state=False).failures

        self.assertTrue(any('no artefacts' in text for text in failures), failures)


class TestAccountingBlockersAndOpenability(_Batch):
    def test_blocker_of_a_migrated_artefact_fails(self):
        self._healthy('Sales')
        self._quality('Sales', status='FAIL', blockers=['semantic model invalid'])

        verdict = self._judge()

        self.assertEqual(verdict.failures, ['batch/Sales: blocker — semantic model invalid'])
        self.assertEqual(verdict.blockers, 1)

    def test_blockers_of_an_unlisted_failed_artefact_are_still_reported(self):
        self._failed('Broken.twbx', blockers=['needs Hyper'])

        failures = self._judge(EXIT_BATCH_PARTIAL_FAIL).failures

        self.assertEqual(len(failures), 2)
        self.assertTrue(any('batch/Broken.twbx: failed' in text for text in failures))
        self.assertTrue(any('blocker — needs Hyper' in text for text in failures))

    def test_project_that_will_not_open_fails(self):
        self._healthy('Sales')
        self._project('Sales', openable=False, issues=['invalid TMDL'])

        failures = self._judge().failures

        self.assertEqual(len(failures), 1)
        self.assertIn('batch/Sales: will not open', failures[0])

    def test_project_without_a_verdict_fails(self):
        self._artefact('Sales.twbx')
        self._record('Sales.twbx')
        self._project('Sales', openable=None)

        failures = self._judge().failures

        self.assertEqual(failures, ['batch/Sales: no openability verdict written'])


class TestKnownLimitations(_Batch):
    CACHE = {'batch/Cache.twbx': _limit()}

    def _legacy(self, blockers=('needs Hyper',)):
        """The failure CACHE records: a failed artefact reporting blockers."""
        self._failed('Cache.twbx', 'hyper_import',
                     'Required Hyper checks failed: C:\\somewhere\\Cache.twbx',
                     blockers=blockers)

    def test_a_listed_limitation_is_acknowledged_with_its_reason(self):
        self._healthy('Sales')
        self._legacy()

        verdict = self._judge(EXIT_BATCH_PARTIAL_FAIL, self.CACHE)

        self.assertEqual(verdict.failures, [])
        self.assertEqual(len(verdict.acknowledged), 1)
        self.assertIn('batch/Cache.twbx', verdict.acknowledged[0])
        self.assertIn('a legacy extract that the Hyper API refuses', verdict.acknowledged[0])
        self.assertEqual((verdict.artefacts, verdict.migrated, verdict.blockers),
                         (2, 1, 0))

    def test_a_limitation_without_blockers_is_acknowledged(self):
        self._failed('Empty.tfl', 'conversion', 'flow has no nodes')

        verdict = self._judge(
            EXIT_BATCH_PARTIAL_FAIL,
            {'batch/Empty.tfl': _limit('conversion', ('no nodes',), ())})

        self.assertEqual(verdict.failures, [])

    def test_a_listed_artefact_that_now_migrates_is_stale(self):
        self._healthy('Cache')

        failures = self._judge(0, self.CACHE).failures

        self.assertEqual(len(failures), 1)
        self.assertIn('now migrates', failures[0])

    def test_a_listed_artefact_missing_from_the_corpus_is_stale(self):
        self._healthy('Sales')

        failures = self._judge(0, self.CACHE).failures

        self.assertEqual(len(failures), 1)
        self.assertIn('batch/Cache.twbx', failures[0])
        self.assertIn('no such artefact', failures[0])

    def test_another_error_category_no_longer_matches(self):
        self._legacy()

        failures = self._judge(EXIT_BATCH_PARTIAL_FAIL,
                               {'batch/Cache.twbx': _limit(category='conversion')}).failures

        self.assertEqual(len(failures), 1)
        self.assertIn("error category is 'hyper_import', recorded 'conversion'", failures[0])

    def test_changed_error_details_no_longer_match(self):
        self._legacy()

        failures = self._judge(EXIT_BATCH_PARTIAL_FAIL,
                               {'batch/Cache.twbx': _limit(details=('Something else',))}).failures

        self.assertEqual(len(failures), 1)
        self.assertIn("no longer contain 'Something else'", failures[0])

    def test_an_unrecorded_blocker_no_longer_matches(self):
        self._legacy(blockers=('needs Hyper', 'brand new problem'))

        failures = self._judge(EXIT_BATCH_PARTIAL_FAIL, self.CACHE).failures

        self.assertEqual(len(failures), 1)
        self.assertIn("unrecorded blocker 'brand new problem'", failures[0])

    def test_a_blocker_that_disappeared_no_longer_matches(self):
        self._legacy()

        failures = self._judge(
            EXIT_BATCH_PARTIAL_FAIL,
            {'batch/Cache.twbx': _limit(blockers=('needs Hyper', 'long gone'))}).failures

        self.assertEqual(len(failures), 1)
        self.assertIn("recorded blocker no longer reported 'long gone'", failures[0])

    def test_blockers_are_counted_not_just_listed(self):
        self._legacy(blockers=('needs Hyper', 'needs Hyper'))

        failures = self._judge(EXIT_BATCH_PARTIAL_FAIL, self.CACHE).failures

        self.assertEqual(len(failures), 1)
        self.assertIn("unrecorded blocker 'needs Hyper'", failures[0])

    def test_a_limitation_of_another_batch_does_not_apply(self):
        self._legacy()

        failures = self._judge(EXIT_BATCH_PARTIAL_FAIL,
                               {'other/Cache.twbx': _limit()}).failures

        self.assertTrue(any('batch/Cache.twbx: failed (hyper_import)' in text
                            for text in failures), failures)

    def test_a_limitation_excuses_only_its_own_artefact(self):
        self._legacy()
        self._failed('Other.twbx', 'conversion', 'something broke')

        failures = self._judge(EXIT_BATCH_PARTIAL_FAIL, self.CACHE).failures

        self.assertEqual(len(failures), 1)
        self.assertIn('batch/Other.twbx: failed (conversion)', failures[0])

    def test_a_limitation_does_not_excuse_blockers_elsewhere(self):
        self._legacy()
        self._healthy('Sales')
        self._quality('Sales', status='FAIL', blockers=['semantic model invalid'])

        failures = self._judge(EXIT_BATCH_PARTIAL_FAIL, self.CACHE).failures

        self.assertEqual(failures, ['batch/Sales: blocker — semantic model invalid'])

    def test_a_limitation_does_not_excuse_an_unopenable_project(self):
        self._legacy()
        self._project('Cache', openable=False, issues=['invalid TMDL'])

        verdict = self._judge(EXIT_BATCH_PARTIAL_FAIL, self.CACHE)

        self.assertEqual(len(verdict.acknowledged), 1)
        self.assertEqual(len(verdict.failures), 1)
        self.assertIn('batch/Cache: will not open', verdict.failures[0])

    def test_a_limitation_does_not_excuse_the_exit_code(self):
        self._legacy()

        failures = self._judge(0, self.CACHE).failures

        self.assertEqual(len(failures), 1)
        self.assertIn('exited 0, expected 7', failures[0])


_MISSING = object()


def _document(key='real_world/Cache.twbx', **overrides):
    entry = {'category': 'hyper_import', 'details': ['d'], 'blockers': [], 'reason': 'r'}
    entry.update(overrides)
    for field in [name for name, value in overrides.items() if value is _MISSING]:
        del entry[field]
    return {'version': 1, 'known_limitations': {key: entry}}


class TestBaselineLoader(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def _load(self, content):
        path = os.path.join(self.dir, 'baseline.json')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(content if isinstance(content, str) else json.dumps(content))
        return load_baseline(path)

    def test_accepts_a_well_formed_entry(self):
        baseline = self._load(_document(blockers=['b1', 'b2']))

        self.assertEqual(baseline, {'real_world/Cache.twbx': {
            'category': 'hyper_import', 'details': ['d'],
            'blockers': ['b1', 'b2'], 'reason': 'r'}})

    def test_accepts_an_empty_baseline(self):
        self.assertEqual(self._load({'version': 1, 'known_limitations': {}}), {})

    def test_accepts_the_explanatory_comment(self):
        document = _document()
        document['_comment'] = 'why this file exists'

        self.assertEqual(list(self._load(document)), ['real_world/Cache.twbx'])

    def test_rejects_a_missing_file(self):
        with self.assertRaises(BaselineError):
            load_baseline(os.path.join(self.dir, 'absent.json'))

    def test_rejects_malformed_documents(self):
        cases = {
            'not json': '{ nope',
            'not an object': '[]',
            'wrong version': {'version': 2, 'known_limitations': {}},
            'missing version': {'known_limitations': {}},
            'unknown top-level key': {'version': 1, 'known_limitations': {}, 'extra': 1},
            'limitations not an object': {'version': 1, 'known_limitations': []},
            'entry not an object': {'version': 1, 'known_limitations': {
                'real_world/Cache.twbx': []}},
        }
        for name, content in cases.items():
            with self.subTest(name), self.assertRaises(BaselineError):
                self._load(content)

    def test_rejects_malformed_keys(self):
        for key in ('nowhere/Cache.twbx', 'real_world', 'real_world/notes.md',
                    'Cache.twbx', ''):
            with self.subTest(key), self.assertRaises(BaselineError):
                self._load(_document(key=key))

    def test_rejects_malformed_entries(self):
        cases = {
            'missing field': {'reason': _MISSING},
            'extra field': {'owner': 'me'},
            'no details': {'details': []},
            'blank detail': {'details': ['  ']},
            'details not a list': {'details': 'text'},
            'blank reason': {'reason': ' '},
            'blank category': {'category': ''},
            'category not a string': {'category': 3},
            'blockers not a list': {'blockers': 'x'},
            'blocker not a string': {'blockers': [1]},
            'blank blocker': {'blockers': ['']},
        }
        for name, overrides in cases.items():
            with self.subTest(name), self.assertRaises(BaselineError):
                self._load(_document(**overrides))

    def test_rejects_a_duplicated_key(self):
        entry = '{"category": "a", "details": ["d"], "blockers": [], "reason": "r"}'
        text = ('{"version": 1, "known_limitations": {'
                f'"real_world/Cache.twbx": {entry}, "real_world/Cache.twbx": {entry}}}}}')

        with self.assertRaises(BaselineError):
            self._load(text)


class TestShippedBaseline(unittest.TestCase):
    def test_it_loads(self):
        self.assertIsInstance(load_baseline(), dict)

    def test_every_entry_names_an_artefact_the_corpus_contains(self):
        corpus = dict(BATCHES)
        for key in load_baseline():
            batch, _, artefact = key.partition('/')
            with self.subTest(key):
                self.assertIn(
                    artefact, _artefacts(os.path.join(REPO_ROOT, corpus[batch])))

    def test_every_entry_explains_itself(self):
        for key, entry in load_baseline().items():
            with self.subTest(key):
                self.assertGreaterEqual(len(entry['reason'].split()), 8)


class TestHyperReaderRequirement(unittest.TestCase):
    def test_a_missing_reader_is_reported_with_the_remedy(self):
        with mock.patch.dict(sys.modules, {'tableauhyperapi': None}):
            problem = _hyper_reader_problem()

        self.assertIn('pip install tableauhyperapi', problem)

    def test_an_importable_reader_is_accepted(self):
        with mock.patch.dict(sys.modules, {'tableauhyperapi': types.ModuleType('tableauhyperapi')}):
            self.assertIsNone(_hyper_reader_problem())


class TestMain(_Batch):
    def _run(self, *, code=0, hyper_problem=None, baseline=None, batches=None):
        out = io.StringIO()
        with mock.patch.object(gate, 'BATCHES', batches or (('batch', self.source),)), \
                mock.patch.object(gate, '_migrate',
                                  return_value=(code, 'MIGRATION LOG')) as migrate, \
                mock.patch.object(gate, '_hyper_reader_problem', return_value=hyper_problem), \
                mock.patch.object(gate, 'load_baseline', return_value=baseline or {}), \
                contextlib.redirect_stdout(out):
            status = gate.main(['--output-dir', self.base])
        return status, out.getvalue(), migrate

    def test_a_healthy_corpus_passes(self):
        self._healthy('Sales')
        self._write_state()

        status, output, _ = self._run()

        self.assertEqual(status, 0, output)
        self.assertIn('Corpus gate passed.', output)
        self.assertIn('artefacts migrated : 1 of 1', output)

    def test_a_failed_artefact_is_listed_by_name(self):
        self._healthy('Sales')
        self._failed('Broken.twbx', details='kaboom')
        self._write_state()

        status, output, _ = self._run(code=EXIT_BATCH_PARTIAL_FAIL)

        self.assertEqual(status, 1)
        self.assertIn('CORPUS GATE FAILED (1)', output)
        self.assertIn('batch/Broken.twbx: failed (hyper_import) — kaboom', output)

    def test_a_known_limitation_is_reported_and_the_gate_passes(self):
        self._healthy('Sales')
        self._failed('Cache.twbx', 'hyper_import', 'Required Hyper checks failed',
                     blockers=['needs Hyper'])
        self._write_state()

        status, output, _ = self._run(code=EXIT_BATCH_PARTIAL_FAIL,
                                      baseline={'batch/Cache.twbx': _limit()})

        self.assertEqual(status, 0, output)
        self.assertIn('Known limitations, reviewed in', output)
        self.assertIn('a legacy extract that the Hyper API refuses', output)

    def test_a_missing_corpus_directory_fails(self):
        status, output, migrate = self._run(
            batches=(('batch', os.path.join(self.base, 'absent')),))

        self.assertEqual(status, 1)
        self.assertIn('corpus directory', output)
        migrate.assert_not_called()

    def test_the_batch_log_is_shown_only_when_the_batch_itself_misbehaves(self):
        self._healthy('Sales')
        self._write_state()
        _, healthy_output, _ = self._run()

        os.remove(os.path.join(self.dir, STATE_FILE))
        status, broken_output, _ = self._run()

        self.assertNotIn('MIGRATION LOG', healthy_output)
        self.assertEqual(status, 1)
        self.assertIn('MIGRATION LOG', broken_output)

    def test_it_refuses_to_run_without_the_hyper_reader(self):
        status, output, migrate = self._run(hyper_problem='no reader here')

        self.assertEqual(status, 1)
        self.assertIn('CORPUS GATE CANNOT RUN: no reader here', output)
        migrate.assert_not_called()

    def test_it_refuses_to_run_on_an_unreadable_baseline(self):
        out = io.StringIO()
        with mock.patch.object(gate, '_hyper_reader_problem', return_value=None), \
                mock.patch.object(gate, 'load_baseline', side_effect=BaselineError('bad file')), \
                mock.patch.object(gate, '_migrate') as migrate, \
                contextlib.redirect_stdout(out):
            status = gate.main(['--output-dir', self.base])

        self.assertEqual(status, 1)
        self.assertIn('CORPUS GATE CANNOT RUN: bad file', out.getvalue())
        migrate.assert_not_called()


class TestAgainstTheProduct(unittest.TestCase):
    """The gate reads what ``migrate.py --batch`` writes, so pin that contract."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def test_the_partial_failure_exit_code_is_the_products(self):
        import migrate

        self.assertEqual(EXIT_BATCH_PARTIAL_FAIL, migrate.ExitCode.BATCH_PARTIAL_FAIL)

    def test_artefact_discovery_matches_what_a_batch_picks_up(self):
        names = ['a.twb', 'b.TWBX', 'c.tds', 'd.tdsx', 'e.tfl', 'f.tflx', '~lock.twb',
                 'notes.md', 'x.hyper', 'y.pbix', 'z.json', 'sub/g.twb',
                 'sub/deep/h.TFL', 'sub/~i.twbx']
        for name in names:
            path = os.path.join(self.dir, *name.split('/'))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, 'w').close()

        proc = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, 'migrate.py'),
             '--batch', self.dir, '--dry-run'],
            cwd=REPO_ROOT, capture_output=True, text=True, errors='replace')

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        listed = sorted(line.strip().replace('\\', '/')
                        for line in proc.stdout.splitlines()
                        if line.startswith('  ') and not line.strip().startswith('Output:'))
        self.assertEqual(len(listed), 8, proc.stdout)
        self.assertEqual(_artefacts(self.dir), listed)

    def test_a_real_batch_is_accounted_against_the_shipped_baseline(self):
        """One artefact that migrates and one recorded limitation, end to end."""
        source = os.path.join(self.dir, 'real_world_subset')
        os.makedirs(source)
        for name in ('multiple_connections.twb', 'SampleFlow.tfl'):
            shutil.copy(os.path.join(REPO_ROOT, 'examples', 'real_world', name), source)
        out_dir = os.path.join(self.dir, 'out', 'real_world')
        # The other entries describe artefacts this subset leaves out.
        recorded = {key: entry for key, entry in load_baseline().items()
                    if key == 'real_world/SampleFlow.tfl'}
        self.assertEqual(len(recorded), 1)

        code, log = gate._migrate(source, out_dir)
        verdict = _account('real_world', source, out_dir, code,
                           _collect(out_dir), recorded)

        self.assertEqual(code, EXIT_BATCH_PARTIAL_FAIL, log[-2000:])
        self.assertEqual(verdict.failures, [], log[-2000:])
        self.assertEqual((verdict.artefacts, verdict.migrated), (2, 1))
        self.assertEqual(len(verdict.acknowledged), 1)
        self.assertIn('real_world/SampleFlow.tfl', verdict.acknowledged[0])


if __name__ == '__main__':
    unittest.main()
