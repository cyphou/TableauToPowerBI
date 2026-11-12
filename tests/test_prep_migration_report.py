"""Per-query Power Query migration report contract tests."""

import hashlib
import io
import json
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tableau_export'))

from prep_flow_analyzer import analyze_flow
from prep_flow_parser import parse_prep_flow

import migrate
from scripts.check_prep_public_corpus import check_generated_files, compare_report
from scripts.check_prep_public_corpus import main as check_corpus
from scripts.fetch_prep_public_corpus import fetch_corpus
from tests.prep_flow_fixtures import operation, output, source, write_flow


class TestPrepMigrationReport(unittest.TestCase):
    def test_packaged_csv_is_staged_and_bound_in_batch_output(self):
        flow = {
            'nodes': {
                'input': operation('LoadCsv', 'out', name='Input', connectionId='source-id',
                                   fields=[{'name': 'Value', 'type': 'integer'}]),
                'out': output('Result'),
            },
            'connections': {'source-id': {
                'id': 'source-id', 'isPackaged': True,
                'connectionAttributes': {'class': 'textscan', 'filename': 'input.csv'},
            }},
        }
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / 'flow.tflx'
            with zipfile.ZipFile(archive_path, 'w') as archive:
                archive.writestr('flow', json.dumps(flow))
                archive.writestr('Data/source-id/input.csv', b'Value\n42\n')
            result = migrate._migrate_single_prep_flow(
                str(archive_path), 'flow', directory, 'flow')
            self.assertTrue(result['success'], result)
            staged = Path(directory) / 'flow' / 'Data' / 'source-id' / 'input.csv'
            self.assertEqual(staged.read_bytes(), b'Value\n42\n')
            query = (Path(directory) / 'flow' / 'PowerQuery' / 'Result.pq').read_text(encoding='utf-8')
            self.assertIn(str(staged), query)
            self.assertNotIn('DataFolder &', query)
            report = json.loads((staged.parents[2] / 'powerquery_migration_report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['packaged_sources'], [
                {'node_id': 'input', 'file': 'Data/source-id/input.csv'}])
            html = (staged.parents[2] / 'powerquery_migration_report.html').read_text(encoding='utf-8')
            self.assertIn('Data/source-id/input.csv', html)
            source_json = json.loads((staged.parents[2] / 'Sources' / 'Input.json').read_text(encoding='utf-8'))
            self.assertEqual(source_json['packaged_file'], 'Data/source-id/input.csv')
            rerun = migrate._migrate_single_prep_flow(str(archive_path), 'flow', directory, 'flow')
            self.assertTrue(rerun['success'], rerun)
            staged.write_bytes(b'user data')
            rejected = migrate._migrate_single_prep_flow(str(archive_path), 'flow', directory, 'flow')
            self.assertFalse(rejected['success'])
            self.assertEqual(staged.read_bytes(), b'user data')
            report = json.loads((staged.parents[2] / 'powerquery_migration_report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['blockers'][0]['code'], 'packaged_source_unresolved')

    def test_packaged_input_remove_columns_actions_are_preserved_in_order(self):
        flow = {
            'nodes': {
                'input': operation('LoadCsv', 'out', name='Input', connectionId='source-id',
                                   fields=[{'name': name, 'type': 'string'}
                                           for name in ('Keep', 'Drop First', 'Drop Second')],
                                   actions=[
                                       operation('RemoveColumns', columnNames=['Drop First']),
                                       operation('RemoveColumns', columnNames=['Drop Second']),
                                   ]),
                'out': output('Result'),
            },
            'connections': {'source-id': {
                'isPackaged': True,
                'connectionAttributes': {'class': 'textscan', 'filename': 'input.csv'},
            }},
        }
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / 'flow.tflx'
            with zipfile.ZipFile(archive_path, 'w') as archive:
                archive.writestr('flow', json.dumps(flow))
                archive.writestr('Data/source-id/input.csv', b'Keep,Drop First,Drop Second\na,b,c\n')
            result = migrate._migrate_single_prep_flow(str(archive_path), 'flow', directory, 'flow')
            self.assertTrue(result['success'], result)
            query = (Path(directory) / 'flow' / 'PowerQuery' / 'Result.pq').read_text(encoding='utf-8')
            self.assertLess(query.index('Drop First'), query.index('Drop Second'))
            self.assertIn('Table.RemoveColumns', query)

    def test_packaged_excel_is_staged_and_bound(self):
        flow = {
            'nodes': {
                'input': operation('LoadExcel', 'out', name='Display Name', connectionId='excel-id',
                                   relation={'type': 'table', 'table': '[Sheet1$]'},
                                   fields=[{'name': 'Value', 'type': 'integer'}]),
                'out': output('Result'),
            },
            'connections': {'excel-id': {
                'isPackaged': True,
                'connectionAttributes': {'class': 'excel-direct', 'filename': 'book.xlsx'},
            }},
        }
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / 'flow.tflx'
            with zipfile.ZipFile(archive_path, 'w') as archive:
                archive.writestr('flow', json.dumps(flow))
                archive.writestr('Data/excel-id/book.xlsx', b'synthetic workbook bytes')
            result = migrate._migrate_single_prep_flow(str(archive_path), 'flow', directory, 'flow')
            self.assertTrue(result['success'], result)
            staged = Path(directory) / 'flow' / 'Data' / 'excel-id' / 'book.xlsx'
            self.assertEqual(staged.read_bytes(), b'synthetic workbook bytes')
            query = (staged.parents[2] / 'PowerQuery' / 'Result.pq').read_text(encoding='utf-8')
            self.assertIn(str(staged), query)
            self.assertIn('Excel.Workbook(File.Contents(', query)
            self.assertIn('Item="Sheet1"', query)
            self.assertNotIn('Item="Display Name"', query)

    def test_packaged_source_refuses_missing_ambiguous_unsafe_and_union(self):
        for case in ('missing', 'ambiguous', 'unsafe', 'union', 'unsupported',
                     'missing_worksheet', 'csv_qualifier', 'csv_locale',
                     'symlink', 'wrong_extension'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                filename = '../escape.csv' if case == 'unsafe' else 'input.csv'
                node_type = ('LoadCsvInputUnion' if case == 'union' else
                             'LoadExcel' if case == 'missing_worksheet' else 'LoadCsv')
                if case == 'missing_worksheet':
                    filename = 'book.xlsx'
                if case == 'wrong_extension':
                    filename = 'input.xlsx'
                flow = {
                    'nodes': {'input': operation(node_type, 'out', connectionId='source-id'),
                              'out': output('Result')},
                    'connections': {'source-id': {
                        'isPackaged': True,
                        'connectionAttributes': {'class': ('json' if case == 'unsupported' else
                                                           'excel-direct' if case == 'missing_worksheet' else 'textscan'),
                                                 'filename': filename},
                    }},
                }
                if case == 'union':
                    flow['nodes']['input']['baseType'] = 'input'
                if case == 'csv_qualifier':
                    flow['nodes']['input']['textQualifier'] = 'A'
                if case == 'csv_locale':
                    flow['nodes']['input']['locale'] = 'en_US'
                archive_path = Path(directory) / 'flow.tflx'
                with zipfile.ZipFile(archive_path, 'w') as archive:
                    archive.writestr('flow', json.dumps(flow))
                    if case == 'symlink':
                        member = zipfile.ZipInfo('Data/source-id/input.csv')
                        member.create_system = 3
                        member.external_attr = (stat.S_IFLNK | 0o777) << 16
                        archive.writestr(member, 'outside.csv')
                    elif case != 'missing':
                        archive.writestr('Data/source-id/input.csv', b'Value\n42\n')
                    if case == 'ambiguous':
                        archive.writestr('Data/source-id/INPUT.CSV', b'different')
                with self.assertRaisesRegex(ValueError, 'Packaged Prep'):
                    migrate._stage_prep_packaged_files(str(archive_path), str(Path(directory) / 'out'))
                self.assertFalse((Path(directory) / 'escape.csv').exists())

    def test_packaged_source_obeys_zip_size_limit(self):
        flow = {'nodes': {'input': operation('LoadCsv', connectionId='source-id')},
                'connections': {'source-id': {'isPackaged': True,
                    'connectionAttributes': {'class': 'textscan', 'filename': 'input.csv'}}}}
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / 'flow.tflx'
            with zipfile.ZipFile(archive_path, 'w') as archive:
                archive.writestr('flow', json.dumps(flow))
                archive.writestr('Data/source-id/input.csv', b'x' * (1024 * 1024 + 1))
            with patch.dict('os.environ', {'TTPBI_MAX_ZIP_ENTRY_MB': '1'}):
                with self.assertRaisesRegex(ValueError, 'size limit'):
                    migrate._stage_prep_packaged_files(str(archive_path), str(Path(directory) / 'out'))
            data = Path(directory) / 'out' / 'Data' / 'source-id'
            self.assertEqual(list(data.iterdir()), [])

    def test_packaged_sources_with_same_name_keep_distinct_connections(self):
        flow = {
            'nodes': {
                'first': operation('LoadCsv', connectionId='first-id'),
                'second': operation('LoadCsv', connectionId='second-id'),
            },
            'connections': {
                identity: {'isPackaged': True,
                           'connectionAttributes': {'class': 'textscan', 'filename': 'same.csv'}}
                for identity in ('first-id', 'second-id')
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / 'flow.tflx'
            with zipfile.ZipFile(archive_path, 'w') as archive:
                archive.writestr('flow', json.dumps(flow))
                archive.writestr('Data/first-id/same.csv', b'first')
                archive.writestr('Data/second-id/same.csv', b'second')
            bindings = migrate._stage_prep_packaged_files(str(archive_path), str(Path(directory) / 'out'))
            self.assertEqual(Path(bindings['first']).read_bytes(), b'first')
            self.assertEqual(Path(bindings['second']).read_bytes(), b'second')
            self.assertNotEqual(bindings['first'], bindings['second'])

    def test_packaged_csv_runs_through_batch_cli(self):
        flow = {
            'nodes': {
                'input': operation('LoadCsv', 'out', name='Input', connectionId='source-id',
                                   fields=[{'name': 'Value', 'type': 'integer'}]),
                'out': output('Result'),
            },
            'connections': {'source-id': {
                'id': 'source-id', 'isPackaged': True,
                'connectionAttributes': {'class': 'textscan', 'filename': 'input.csv'},
            }},
        }
        with tempfile.TemporaryDirectory() as directory:
            input_dir = Path(directory) / 'input'
            input_dir.mkdir()
            output_dir = Path(directory) / 'output'
            with zipfile.ZipFile(input_dir / 'flow.tflx', 'w') as archive:
                archive.writestr('flow', json.dumps(flow))
                archive.writestr('Data/source-id/input.csv', b'Value\n42\n')
            run = subprocess.run(
                [sys.executable, str(ROOT / 'migrate.py'), 'batch', str(input_dir),
                 '--output-dir', str(output_dir), '--quiet'],
                cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace',
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            reports = [path for path in output_dir.rglob('powerquery_migration_report.json')
                       if 'packaged_sources' in json.loads(path.read_text(encoding='utf-8'))]
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text(encoding='utf-8'))
            staged = reports[0].parent / report['packaged_sources'][0]['file']
            query = (reports[0].parent / report['queries'][0]['m_file']).read_text(encoding='utf-8')
            self.assertEqual(staged.read_bytes(), b'Value\n42\n')
            self.assertIn(str(staged), query)

    def test_public_corpus_comparison_detects_regressions_and_allows_progress(self):
        baseline = {
            'ready': {'status': 'generated'},
            'blocked': {'status': 'blocked', 'blocker_codes': ['join', 'filter']},
        }
        report = {'flows': [
            {'flow': 'ready', 'status': 'generated', 'blockers': [],
             'summary': {'output_count': 1, 'query_count': 1,
                         'generated_queries': 1, 'blocked_queries': 0},
             'queries': [{'name': 'Out', 'query_role': 'output', 'output_node_id': 'out',
                          'status': 'generated', 'm_file': 'ready/PowerQuery/out.pq'}]},
            {'flow': 'blocked', 'status': 'blocked', 'blockers': [{'code': 'join'}],
             'summary': {'output_count': 1, 'query_count': 1,
                         'generated_queries': 0, 'blocked_queries': 1},
             'queries': [{'name': 'Out', 'query_role': 'output', 'output_node_id': 'out',
                          'status': 'blocked', 'm_file': None}]},
        ]}
        self.assertEqual(compare_report(report, baseline), [])
        report['flows'][1] = {
            'flow': 'blocked', 'status': 'generated', 'blockers': [],
            'summary': {'output_count': 1, 'query_count': 1,
                        'generated_queries': 1, 'blocked_queries': 0},
            'queries': [{'name': 'Out', 'query_role': 'output', 'output_node_id': 'out',
                         'status': 'generated', 'm_file': 'blocked/PowerQuery/out.pq'}],
        }
        self.assertEqual(compare_report(report, baseline), [])
        report['flows'][0]['status'] = 'blocked'
        self.assertTrue(compare_report(report, baseline))
        report['flows'].pop()
        self.assertTrue(compare_report(report, baseline))

    def test_public_corpus_comparison_rejects_missing_or_misleading_evidence(self):
        baseline = {'blocked': {'status': 'blocked', 'blocker_codes': ['join']}}
        summary = {'output_count': 1, 'query_count': 1,
                   'generated_queries': 0, 'blocked_queries': 1}
        blocked_query = {'name': 'Out', 'query_role': 'output', 'output_node_id': 'out',
                         'status': 'blocked', 'm_file': None}
        for flow in [
            {'flow': 'blocked', 'status': 'evidence_unavailable', 'queries': [], 'blockers': []},
            {'flow': 'blocked', 'status': 'blocked', 'queries': [], 'blockers': []},
            {'flow': 'blocked', 'status': 'blocked', 'blockers': [{'code': 'other'}],
             'summary': summary, 'queries': [blocked_query]},
            {'flow': 'blocked', 'status': 'blocked', 'blockers': [{'code': 'join'}],
             'summary': {**summary, 'generated_queries': 1, 'blocked_queries': 0},
             'queries': [{**blocked_query, 'status': 'generated', 'm_file': 'untrusted.pq'}]},
            {'flow': 'blocked', 'status': 'blocked', 'blockers': [{'code': 'join'}],
             'summary': {**summary, 'output_count': 2}, 'queries': [blocked_query]},
        ]:
            with self.subTest(flow=flow):
                self.assertTrue(compare_report({'flows': [flow]}, baseline))

    def test_public_corpus_check_requires_valid_current_m_file(self):
        query = {'name': 'Output', 'status': 'generated', 'm_file': 'flow/Output.pq',
                 'static_m_validation': {'status': 'pass'}}
        report = {'flows': [{'flow': 'flow', 'queries': [query]}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'flow' / 'Output.pq'
            path.parent.mkdir()
            path.write_text('let\n    Source = #table({}, {})\nin\n    Source',
                            encoding='utf-8')
            self.assertEqual(check_generated_files(report, directory), [])
            path.write_text('let Source = ( in Source', encoding='utf-8')
            self.assertTrue(check_generated_files(report, directory))
            query['m_file'] = '../outside.pq'
            self.assertTrue(check_generated_files(report, directory))
            query['m_file'] = 'flow/missing.pq'
            self.assertTrue(check_generated_files(report, directory))

    def test_public_corpus_gate_runs_real_batch_for_generated_and_blocked_flows(self):
        cases = [
            ({'nodes': {'input': source('out'), 'out': output(base_type=True)},
              'connections': {}},
             {'status': 'generated', 'required_queries': ['PublishedSales']}),
            ({'nodes': {'input': source('bad'),
                        'bad': operation('SyntheticUnsupported', 'out'),
                        'out': output(base_type=True)}, 'connections': {}},
             {'status': 'blocked', 'blocker_codes': ['conversion_blocked']}),
        ]
        for flow, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                input_dir = root / 'input'
                input_dir.mkdir()
                flow_path = Path(write_flow(input_dir, flow))
                manifest = {
                    'inputs': [{'filename': flow_path.name, 'repository': 'owner/repo',
                                'commit': 'a' * 40, 'path': 'flow.tfl',
                                'sha256': hashlib.sha256(flow_path.read_bytes()).hexdigest()}],
                    'baseline': {flow_path.stem: expected},
                }
                manifest_path = root / 'manifest.json'
                manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
                output_dir = root / 'output'
                with patch('scripts.check_prep_public_corpus.MANIFEST', manifest_path):
                    self.assertEqual(check_corpus([str(input_dir), str(output_dir)]), 0)
                report = json.loads((output_dir / 'powerquery_migration_report.json')
                                    .read_text(encoding='utf-8'))
                self.assertEqual(report['flows'][0]['status'], expected['status'])
                if expected['status'] == 'blocked':
                    self.assertFalse((output_dir / flow_path.stem / 'PowerQuery').exists())
                original_report = (output_dir / 'powerquery_migration_report.json').read_bytes()
                with patch('scripts.check_prep_public_corpus.MANIFEST', manifest_path):
                    with self.assertRaises(SystemExit) as failure:
                        check_corpus([str(input_dir), str(output_dir)])
                self.assertEqual(failure.exception.code, 1)
                self.assertEqual(
                    (output_dir / 'powerquery_migration_report.json').read_bytes(),
                    original_report,
                )

    def test_public_corpus_fetch_verifies_bytes_and_is_idempotent(self):
        payload = b'public synthetic flow'
        entry = {'filename': 'sample.tfl', 'repository': 'owner/repo',
                 'commit': 'a' * 40, 'path': 'example/flow file.tfl',
                 'sha256': hashlib.sha256(payload).hexdigest()}
        with tempfile.TemporaryDirectory() as directory:
            opener = unittest.mock.Mock(return_value=io.BytesIO(payload))
            result = fetch_corpus([entry], directory, opener=opener)
            self.assertEqual(result, {'verified': 0, 'downloaded': 1})
            self.assertIn('flow%20file.tfl', opener.call_args.args[0])
            self.assertEqual((Path(directory) / 'sample.tfl').read_bytes(), payload)
            result = fetch_corpus([entry], directory, verify_only=True)
            self.assertEqual(result, {'verified': 1, 'downloaded': 0})
            self.assertEqual(opener.call_count, 1)

    def test_public_corpus_fetch_rejects_hash_mismatch_without_overwrite(self):
        payload = b'expected'
        entry = {'filename': 'sample.tfl', 'repository': 'owner/repo',
                 'commit': 'a' * 40, 'path': 'flow.tfl',
                 'sha256': hashlib.sha256(payload).hexdigest()}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
                fetch_corpus([entry], directory,
                             opener=lambda *args, **kwargs: io.BytesIO(b'wrong'))
            self.assertEqual(list(Path(directory).iterdir()), [])
            target = Path(directory) / 'sample.tfl'
            target.write_bytes(b'original')
            with self.assertRaisesRegex(ValueError, 'Existing corpus file differs'):
                fetch_corpus([entry], directory)
            self.assertEqual(target.read_bytes(), b'original')

    def test_public_corpus_fetch_rejects_unsafe_names_and_missing_offline_files(self):
        entry = {'filename': 'sample.tfl', 'repository': 'owner/repo',
                 'commit': 'a' * 40, 'path': 'flow.tfl',
                 'sha256': hashlib.sha256(b'expected').hexdigest()}
        with tempfile.TemporaryDirectory() as directory:
            opener = unittest.mock.Mock()
            with self.assertRaisesRegex(ValueError, 'missing'):
                fetch_corpus([entry], directory, verify_only=True, opener=opener)
            opener.assert_not_called()
            for overrides in ({'filename': '../escape.tfl'},
                              {'path': '../escape.tfl'},
                              {'repository': '../..'},
                              {'repository': 'owner/repo/extra'}):
                with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                    fetch_corpus([{**entry, **overrides}], directory, opener=opener)
            with self.assertRaisesRegex(ValueError, 'Colliding'):
                fetch_corpus([entry, {**entry, 'filename': 'SAMPLE.tfl'}], directory,
                             opener=opener)
            opener.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_public_corpus_fetch_rejects_oversize_and_publication_races(self):
        payload = b'expected'
        entry = {'filename': 'sample.tfl', 'repository': 'owner/repo',
                 'commit': 'a' * 40, 'path': 'flow.tfl',
                 'sha256': hashlib.sha256(payload).hexdigest()}
        with tempfile.TemporaryDirectory() as directory:
            with patch('scripts.fetch_prep_public_corpus.MAX_BYTES', 3):
                with self.assertRaisesRegex(ValueError, 'size limit'):
                    fetch_corpus([entry], directory,
                                 opener=lambda *args, **kwargs: io.BytesIO(payload))
            self.assertEqual(list(Path(directory).iterdir()), [])

            target = Path(directory) / 'sample.tfl'
            def competing_publication(*args):
                target.write_bytes(b'other writer')
                raise FileExistsError('concurrent publication')

            with patch('scripts.fetch_prep_public_corpus.os.link',
                       side_effect=competing_publication):
                with self.assertRaises(FileExistsError):
                    fetch_corpus([entry], directory,
                                 opener=lambda *args, **kwargs: io.BytesIO(payload))
            self.assertEqual(target.read_bytes(), b'other writer')
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_public_corpus_manifest_has_pinned_sources_and_duplicate_identity(self):
        path = ROOT / 'tests' / 'prep_dialect_samples' / 'public_corpus_manifest.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        entries = manifest['inputs']
        by_name = {entry['filename']: entry for entry in entries}
        self.assertEqual(len(by_name), len(entries))
        self.assertGreaterEqual(len(entries), 7)
        self.assertGreaterEqual(len({entry['sha256'] for entry in entries}), 6)
        self.assertTrue(any(entry.get('duplicate_of') for entry in entries))
        for entry in entries:
            self.assertRegex(entry['commit'], r'^[0-9a-f]{40}$')
            self.assertRegex(entry['sha256'], r'^[0-9a-f]{64}$')
            self.assertRegex(entry['repository'], r'^[^/]+/[^/]+$')
            self.assertIn(entry['license'], {'MIT', 'Apache-2.0', 'CC-BY-4.0'})
            self.assertTrue(entry['path'].endswith(('.tfl', '.tflx')))
            if entry.get('duplicate_of'):
                original = by_name[entry['duplicate_of']]
                self.assertEqual(entry['sha256'], original['sha256'])
                self.assertEqual(entry['path'], original['path'])
                self.assertEqual(entry['commit'], original['commit'])

    def profile(self, m_queries):
        return SimpleNamespace(
            name='PublicFlow',
            inputs=[SimpleNamespace(node_id='input', name='PublicSource')],
            outputs=[SimpleNamespace(
                node_id='output', name='Orders',
                column_names=['Order ID', 'Discount'],
            )],
            transforms=[SimpleNamespace(
                node_id='filter', name='ValueFilter on Discount',
                transform_type='Clean',
                details={'operations': [{'type': 'filter', 'column': 'Discount'}]},
            )],
            node_graph={'input': ['filter'], 'filter': ['output'], 'output': []},
            m_queries=m_queries,
            diagnostics=[],
            assessment={'grade': 'GREEN'},
        )

    def test_generated_report_lists_query_file_sources_steps_and_m_validation(self):
        query = 'let\n    Source = #table({}, {})\nin\n    Source'
        with tempfile.TemporaryDirectory() as directory:
            paths = migrate._write_prep_query_migration_report(
                directory, 'PublicFlow', 'public.tfl',
                profile=self.profile({'Orders': query, 'PublicSource': query}),
            )
            report = json.loads(Path(paths['json']).read_text(encoding='utf-8'))
            html = Path(paths['html']).read_text(encoding='utf-8')

        self.assertEqual(report['status'], 'generated')
        self.assertEqual(report['summary']['generated_queries'], 2)
        output_query = next(query for query in report['queries']
                            if query['query_role'] == 'output')
        secondary_query = next(query for query in report['queries']
                               if query['query_role'] == 'secondary')
        self.assertEqual(output_query['m_file'], 'PowerQuery/Orders.pq')
        self.assertEqual(output_query['depends_on'], ['PublicSource'])
        self.assertEqual(output_query['steps'][0]['operations'], [
            {'type': 'filter', 'column': 'Discount'}
        ])
        self.assertEqual(output_query['static_m_validation']['status'], 'pass')
        self.assertEqual(secondary_query['name'], 'PublicSource')
        self.assertEqual(secondary_query['query_role'], 'secondary')
        self.assertIn('Orders.pq', html)

    def test_blocked_report_redacts_raw_union_details(self):
        failure = "ValueError: Prep union incompatible types for 'Discount': 'real', 'string'"
        with tempfile.TemporaryDirectory() as directory:
            paths = migrate._write_prep_query_migration_report(
                directory, 'PublicFlow', 'public.tfl',
                profile=self.profile({}), failure=failure,
            )
            report = json.loads(Path(paths['json']).read_text(encoding='utf-8'))
            html = Path(paths['html']).read_text(encoding='utf-8')

        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(report['summary']['blocked_queries'], 1)
        self.assertIsNone(report['queries'][0]['m_file'])
        serialized = json.dumps(report)
        self.assertNotIn("'real', 'string'", serialized)
        self.assertIn('union_schema_conflict', html)

    def test_missing_join_projection_has_specific_sanitized_blocker(self):
        failure = ("Prep SimpleJoin 'private-id': source-declared nonempty "
                   'fields projection is required; the observed public payload '
                   'has no output schema/projection')
        with tempfile.TemporaryDirectory() as directory:
            paths = migrate._write_prep_query_migration_report(
                directory, 'PublicFlow', 'public.tfl',
                profile=self.profile({}), failure=failure,
            )
            report = json.loads(Path(paths['json']).read_text(encoding='utf-8'))
        self.assertEqual(report['blockers'][0]['code'], 'join_projection_missing')
        self.assertEqual(report['queries'][0]['blocker_codes'], ['join_projection_missing'])
        self.assertNotIn('private-id', json.dumps(report))

    def test_colliding_output_names_remain_distinct_in_blocked_batch_report(self):
        profile = self.profile({'Orders_A': 'let Source = 1 in Source'})
        profile.outputs = [
            SimpleNamespace(node_id='output-one', name='Orders A', column_names=[]),
            SimpleNamespace(node_id='output-two', name='Orders_A', column_names=[]),
        ]
        with tempfile.TemporaryDirectory() as directory:
            with patch('prep_flow_analyzer.analyze_flow', return_value=profile):
                result = migrate._migrate_single_prep_flow(
                    'public.tfl', 'flow', directory, 'PublicFlow')
            report_path = Path(directory) / 'flow' / 'powerquery_migration_report.json'
            report = json.loads(report_path.read_text(encoding='utf-8'))
            self.assertFalse((Path(directory) / 'flow' / 'PowerQuery').exists())

        self.assertFalse(result['success'])
        self.assertEqual(report['summary']['output_count'], 2)
        self.assertEqual(report['summary']['blocked_queries'], 2)
        self.assertEqual(report['blockers'][0]['code'], 'duplicate_output_identity')
        self.assertEqual(
            {query['output_node_id'] for query in report['queries']},
            {'output-one', 'output-two'},
        )
        self.assertTrue(all(query['status'] == 'blocked' and query['m_file'] is None
                            for query in report['queries']))

    def test_incomplete_or_colliding_queries_have_actionable_blockers(self):
        cases = [
            ('Prep outputs have no M queries: private-name', 'missing_output_query'),
            ('Prep M query filenames collide; exporting would overwrite queries',
             'query_filename_collision'),
            ("Prep SimpleJoin 'private-id': semantics evidence policy is required "
             'before generating the join', 'simple_join_semantics_unverified'),
              ("Prep AddColumn: duplicate/colliding field 'private-name'",
               'duplicate_column_identity'),
        ]
        for failure, expected_code in cases:
            with self.subTest(failure=failure):
                blocker = migrate._prep_report_blocker(failure)
                self.assertEqual(blocker['code'], expected_code)
                self.assertNotIn('private-name', blocker['message'])

    def test_real_colliding_outputs_are_reported_without_publishing_m(self):
        input_node = source('first', name='PublicInput')
        input_node['nextNodes'].append({'nextNodeId': 'second'})
        flow = {
            'nodes': {
                'input': input_node,
                'first': output('Orders A', base_type=True),
                'second': output('Orders_A', base_type=True),
            },
            'connections': {},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = write_flow(directory, flow)
            result = migrate._migrate_single_prep_flow(path, 'flow', directory, 'PublicFlow')
            flow_dir = Path(directory) / 'flow'
            report = json.loads((flow_dir / 'powerquery_migration_report.json').read_text(
                encoding='utf-8'))
            self.assertFalse((flow_dir / 'PowerQuery').exists())
        self.assertFalse(result['success'])
        self.assertEqual(report['blockers'][0]['code'], 'duplicate_output_identity')
        self.assertEqual(report['summary']['blocked_queries'], 2)
        self.assertEqual({query['output_node_id'] for query in report['queries']},
                         {'first', 'second'})

    def test_multiple_profile_diagnostics_are_preserved_without_raw_values(self):
        profile = self.profile({})
        profile.diagnostics = [
            {'code': 'unsupported_quick_calc', 'message': 'raw formula/value omitted'},
            {'code': 'unsupported_action_annotations', 'message': 'raw payload omitted'},
        ]
        with tempfile.TemporaryDirectory() as directory:
            paths = migrate._write_prep_query_migration_report(
                directory, 'PublicFlow', 'public.tfl', profile=profile,
                failure='Prep conversion blocked by unsupported diagnostics',
            )
            report = json.loads(Path(paths['json']).read_text(encoding='utf-8'))

        self.assertEqual(
            {item['code'] for item in report['blockers']},
            {'conversion_blocked', 'unsupported_quick_calc',
               'unverified_action_annotations'},
        )
        self.assertNotIn('raw formula', json.dumps(report))

    def test_nonblocking_profile_diagnostic_does_not_block_generated_queries(self):
        profile = self.profile({'Orders': 'let Source = #table({}, {}) in Source'})
        profile.diagnostics = [{
            'code': 'quick_calc_unicode_version_pinned',
            'severity': 'warning',
            'message': 'native parity remains not_run',
            'blocks_conversion': False,
        }]
        with tempfile.TemporaryDirectory() as directory:
            paths = migrate._write_prep_query_migration_report(
                directory, 'PublicFlow', 'public.tfl', profile=profile)
            report = json.loads(Path(paths['json']).read_text(encoding='utf-8'))
        self.assertEqual(report['status'], 'generated')
        self.assertEqual(report['blockers'], [])
        self.assertEqual(report['diagnostic_codes'], ['quick_calc_unicode_version_pinned'])

    def test_analysis_failure_still_produces_a_blocker_report(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = migrate._write_prep_query_migration_report(
                directory, 'PublicFlow', 'public.tfl',
                failure=ValueError("Prep superNode id unsupported nodeType 'SuperExtensibilityNode'"),
            )
            report = json.loads(Path(paths['json']).read_text(encoding='utf-8'))

        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(report['queries'], [])
        self.assertEqual(report['blockers'][0]['code'], 'unsupported_node_type')
        self.assertNotIn('public.tfl', report['blockers'][0]['message'])

    def test_failed_rerun_marks_prior_m_files_without_deleting_them(self):
        with tempfile.TemporaryDirectory() as directory:
            flow_dir = Path(directory) / 'flow'
            pq_dir = flow_dir / 'PowerQuery'
            pq_dir.mkdir(parents=True)
            previous = pq_dir / 'old.pq'
            previous.write_text('let Source = 1 in Source', encoding='utf-8')
            paths = migrate._write_prep_query_migration_report(
                str(flow_dir), 'PublicFlow', 'public.tfl',
                profile=self.profile({}), failure='Prep outputs have no M queries',
            )
            report = json.loads(Path(paths['json']).read_text(encoding='utf-8'))
            html = Path(paths['html']).read_text(encoding='utf-8')
            migrate._write_prep_batch_query_migration_report({
                'flow': {'prep_flow': True, 'powerquery_migration_report': paths['json']},
            }, directory)
            batch = json.loads((Path(directory) / 'powerquery_migration_report.json')
                               .read_text(encoding='utf-8'))
            batch_html = (Path(directory) / 'powerquery_migration_report.html').read_text(
                encoding='utf-8')
            self.assertEqual(previous.read_text(encoding='utf-8'), 'let Source = 1 in Source')

        self.assertEqual(report['previous_m_files'], 1)
        self.assertEqual(batch['flows'][0]['previous_m_files'], 1)
        self.assertIn('not outputs of this failed run', html)
        self.assertIn('not outputs of this failed run', batch_html)
        self.assertTrue(all(query['m_file'] is None for query in report['queries']))

    def test_unknown_supernode_keeps_output_inventory_but_blocks_m(self):
        extension = {
            'nodeType': '.v2019_2_2.SuperExtensibilityNode',
            'id': 'extension',
            'name': 'Python extension',
            'baseType': 'superNode',
            'actionNode': {
                'nodeType': '.v2019_2_2.ExtensibilityNode',
                'id': 'extension-action',
                'baseType': 'transform',
            },
            'nextNodes': [{
                'namespace': 'Default', 'nextNodeId': 'output',
                'nextNamespace': 'Default',
            }],
        }
        flow = {
            'nodes': {
                'input': source('extension', name='PublicInput'),
                'extension': extension,
                'output': output('PublicOutput', base_type=True),
            },
            'connections': {},
        }
        with tempfile.TemporaryDirectory() as directory:
            profile = analyze_flow(
                write_flow(directory, flow), include_m_queries=True)
            paths = migrate._write_prep_query_migration_report(
                directory, 'PublicFlow', 'public.tfl', profile=profile,
                failure=profile.assessment['conversion_error'],
            )
            report = json.loads(Path(paths['json']).read_text(encoding='utf-8'))

        self.assertEqual([output.name for output in profile.outputs], ['PublicOutput'])
        self.assertEqual(profile.m_queries, {})
        self.assertEqual(report['summary']['blocked_queries'], 1)
        self.assertEqual(report['queries'][0]['name'], 'PublicOutput')
        self.assertEqual(report['blockers'][0]['code'], 'unsupported_node_type')

    def test_remap_values_do_not_leak_through_lineage_or_report_labels(self):
        input_node = source('remap', name='PublicInput')
        input_node['fields'] = [{'name': 'Label', 'type': 'string'}]
        flow = {
            'nodes': {
                'input': input_node,
                'remap': operation(
                    'Remap', 'result', name='Remap Label: secret to replacement',
                    columnName='Label', values={'"replacement"': ['"secret"']},
                ),
                'result': output('PublicOutput', base_type=True),
            },
            'connections': {},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = write_flow(directory, flow)
            datasource = parse_prep_flow(path)[0]
            profile = analyze_flow(path, include_m_queries=True)
            paths = migrate._write_prep_query_migration_report(
                directory, 'PublicFlow', path, profile=profile)
            report = Path(paths['json']).read_text(encoding='utf-8')
            html = Path(paths['html']).read_text(encoding='utf-8')

        self.assertIn('"secret"', datasource['m_query_override'])
        self.assertIn('"replacement"', datasource['m_query_override'])
        provenance = json.dumps(datasource['prep_provenance'])
        self.assertIn('Remap on Label', provenance)
        for metadata in (provenance, report, html):
            self.assertNotIn('secret', metadata)
            self.assertNotIn('replacement', metadata)

    def test_batch_report_is_written_when_all_flows_are_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            first = migrate._write_prep_query_migration_report(
                str(Path(directory) / 'extension-flow'), 'ExtensionFlow',
                'extension.tflx',
                failure=ValueError("unsupported nodeType 'SuperExtensibilityNode'"),
            )
            second = migrate._write_prep_query_migration_report(
                str(Path(directory) / 'union-flow'), 'UnionFlow', 'union.tfl',
                profile=self.profile({}), failure="Prep union incompatible types",
            )
            migrate._run_batch_prep_lineage({
                'ExtensionFlow': {
                    'prep_flow': True, 'success': False,
                    'powerquery_migration_report': first['json'],
                },
                'UnionFlow': {
                    'prep_flow': True, 'success': False,
                    'powerquery_migration_report': second['json'],
                },
            }, directory)

            report_path = Path(directory) / 'powerquery_migration_report.json'
            report = json.loads(report_path.read_text(encoding='utf-8'))
            html = (Path(directory) / 'powerquery_migration_report.html').read_text(
                encoding='utf-8')

        self.assertEqual(report['status'], 'blocked')
        self.assertEqual(report['summary']['flow_count'], 2)
        self.assertEqual(report['summary']['blocked_flows'], 2)
        self.assertEqual(len(report['flows']), 2)
        self.assertIn('SuperExtensibilityNode', html)
        self.assertIn('Union inputs disagree on a column type', html)

    def test_batch_report_retains_missing_and_invalid_flow_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            good = migrate._write_prep_query_migration_report(
                str(Path(directory) / 'good'), 'Good', 'public.tfl',
                profile=self.profile({'Orders': 'let Source = 1 in Source'}),
            )
            invalid = Path(directory) / 'invalid.json'
            invalid.write_text('[]', encoding='utf-8')
            migrate._write_prep_batch_query_migration_report({
                'good': {'prep_flow': True, 'report_name': 'Good',
                         'powerquery_migration_report': good['json']},
                'missing': {'prep_flow': True, 'report_name': 'Missing'},
                'invalid': {'prep_flow': True, 'report_name': 'Invalid',
                            'powerquery_migration_report': str(invalid)},
                'workbook': {'success': True},
            }, directory)
            report = json.loads((Path(directory) / 'powerquery_migration_report.json')
                                .read_text(encoding='utf-8'))
            html = (Path(directory) / 'powerquery_migration_report.html').read_text(
                encoding='utf-8')

        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['summary']['flow_count'], 3)
        self.assertEqual(report['summary']['unavailable_flows'], 2)
        unavailable = [flow for flow in report['flows']
                       if flow['status'] == 'evidence_unavailable']
        self.assertEqual({flow['flow'] for flow in unavailable}, {'Missing', 'Invalid'})
        self.assertTrue(all(flow['blockers'][0]['code'] == 'report_unavailable'
                            and flow['report_file'] is None
                            and flow['queries'] == [] for flow in unavailable))
        self.assertIn('query evidence is unavailable', html)


if __name__ == '__main__':
    unittest.main()
