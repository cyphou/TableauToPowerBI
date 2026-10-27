"""Batch destination preflight regressions, without parsing private inputs.

The extension matrix uses unittest subTests so all 15 unordered pairs run
through fresh/resume and normal/dry-run entry points.
"""
import io
import itertools
import json
import os
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import TestCase, mock

import migrate
from powerbi_import.desktop_paths import (
    DesktopPathError,
    plan_desktop_project_name,
)
from tests.test_bug_bash_resume_paths import _publish, _root_at

EXTENSION_PAIRS = tuple(itertools.combinations(
    ('twb', 'twbx', 'tds', 'tdsx', 'tfl', 'tflx'), 2))
PLACEHOLDER = b'Public regression placeholder; must never be extracted.\r\n'


def _snapshot(root):
    """Include empty directories as well as exact bytes of every existing file."""
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob('*')
    }


def _source(directory, relative):
    path = directory / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PLACEHOLDER)
    return path


class TestBatchOutputCollisions(TestCase):
    def _assert_rejected(self, root, sources, output, relatives, *,
                         resume=False, dry_run=False, parallel=None):
        before = _snapshot(root)
        text = io.StringIO()
        with mock.patch.object(migrate, 'run_extraction') as extract, \
                mock.patch.object(migrate, 'run_generation') as generate, \
                mock.patch.object(migrate, '_migrate_single_workbook') as worker, \
                mock.patch.object(migrate, 'run_batch_html_dashboard') as dashboard, \
                mock.patch.object(migrate.os, 'makedirs') as mkdir, \
                redirect_stdout(text):
            result = migrate.run_batch_migration(
                str(sources), output_dir=str(output), resume=resume,
                dry_run=dry_run, parallel=parallel,
                jsonl_log=str(root / 'events.jsonl'), verify_open=False)
        self.assertEqual(result, migrate.ExitCode.VALIDATION_FAILED)
        diagnostic = text.getvalue()
        self.assertIn('Batch output project collisions (Windows path identity)',
                      diagnostic)
        self.assertIn('Target:', diagnostic)
        self.assertIn('Rename the conflicting inputs', diagnostic)
        for relative in relatives:
            self.assertIn(f'Source: {relative}', diagnostic)
        self.assertNotIn('Resume: skipped', diagnostic)
        self.assertNotIn('All workbooks already completed', diagnostic)
        extract.assert_not_called()
        generate.assert_not_called()
        worker.assert_not_called()
        dashboard.assert_not_called()
        mkdir.assert_not_called()
        self.assertEqual(_snapshot(root), before)
        return diagnostic

    def test_all_15_extension_pairs_without_existing_output(self):
        self.assertEqual(len(EXTENSION_PAIRS), 15)
        for pair, resume, dry_run in itertools.product(
                EXTENSION_PAIRS, (False, True), (False, True)):
            with self.subTest(pair=pair, resume=resume, dry_run=dry_run), \
                    tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                sources = root / 'sources'
                relatives = [f'Orders.{extension}' for extension in pair]
                for relative in relatives:
                    _source(sources, relative)
                output = root / 'output'
                self._assert_rejected(
                    root, sources, output, relatives,
                    resume=resume, dry_run=dry_run, parallel=2)
                self.assertFalse(output.exists())
                self.assertFalse((root / 'events.jsonl').exists())

    def test_all_15_extension_pairs_preserve_checkpoint_marker_and_jsonl(self):
        for pair, resume, dry_run in itertools.product(
                EXTENSION_PAIRS, (False, True), (False, True)):
            with self.subTest(pair=pair, resume=resume, dry_run=dry_run), \
                    tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                sources = root / 'sources'
                relatives = [f'Orders.{extension}' for extension in pair]
                for relative in relatives:
                    _source(sources, relative)
                output = root / 'output'
                project = output / 'Orders'
                project.mkdir(parents=True)
                # Deliberate whitespace/CRLF makes reserialization detectable.
                (output / '.migration_batch_state.json').write_bytes(
                    b'{\r\n  "Orders.twb": {"status": "success"}\r\n}\r\n')
                (project / 'Orders.pbip').write_bytes(b'{ "original": true }\r\n')
                (root / 'events.jsonl').write_bytes(
                    b'{"event": "original", "public": true}\r\n')
                self._assert_rejected(
                    root, sources, output, relatives,
                    resume=resume, dry_run=dry_run)

    def test_real_completed_output_cannot_hide_new_collision_on_resume(self):
        for with_checkpoint in (False, True):
            with self.subTest(with_checkpoint=with_checkpoint), \
                    tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                sources = root / 'sources'
                _source(sources, 'Orders.twb')
                output = root / 'output'
                options = dict(
                    batch_dir=str(sources), output_dir=str(output),
                    verify_open=False, jsonl_log=str(root / 'events.jsonl'))

                def publish(**kwargs):
                    _publish(kwargs['basename'], kwargs['workbook_output_dir'])
                    return {'success': True, 'report_name': kwargs['basename']}

                with mock.patch.object(migrate, '_migrate_single_workbook',
                                       side_effect=publish) as worker, \
                        mock.patch.object(migrate, 'run_batch_html_dashboard'), \
                        redirect_stdout(io.StringIO()):
                    self.assertEqual(migrate.run_batch_migration(**options),
                                     migrate.ExitCode.SUCCESS)
                    worker.assert_called_once()
                    checkpoint = output / '.migration_batch_state.json'
                    if not with_checkpoint:
                        checkpoint.unlink()
                    worker.reset_mock()
                    # Establish that this exact output really is skippable.
                    self.assertEqual(
                        migrate.run_batch_migration(**options, resume=True),
                        migrate.ExitCode.SUCCESS)
                    worker.assert_not_called()
                _source(sources, 'Orders.twbx')
                self._assert_rejected(
                    root, sources, output, ['Orders.twb', 'Orders.twbx'],
                    resume=True)
                self.assertEqual(checkpoint.exists(), with_checkpoint)

    def test_planned_short_workbook_identity_collides_with_prep_basename(self):
        for extension, resume, dry_run in itertools.product(
                ('tfl', 'tflx'), (False, True), (False, True)):
            with self.subTest(extension=extension, resume=resume, dry_run=dry_run), \
                    tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                output = Path(_root_at(root / 'output', 120))
                basename = 'Workbook_' + 'a' * 85
                planned = plan_desktop_project_name(output, basename)
                self.assertNotEqual(planned, basename)
                self.assertTrue(planned.startswith('P_'))
                self.assertEqual(plan_desktop_project_name(output, planned), planned)
                sources = root / 'sources'
                relatives = [f'{basename}.twb', f'{planned}.{extension}']
                for relative in relatives:
                    _source(sources, relative)
                diagnostic = self._assert_rejected(
                    root, sources, output, relatives,
                    resume=resume, dry_run=dry_run)
                self.assertIn(f'Target: {output / planned}', diagnostic)
                self.assertFalse(output.exists())

    def test_windows_case_only_names_and_directories_use_mocked_enumeration(self):
        # Do not try to create two case-only files/directories on Windows.
        # Both walk orders must yield the same sorted, explicit diagnostic.
        cases = (
            ('Orders.twb', 'orders.twb'),
            (os.path.join('North', 'Orders.twb'),
             os.path.join('north', 'Orders.twb')),
        )
        for relatives, resume, dry_run in itertools.product(
                cases, (False, True), (False, True)):
            with self.subTest(relatives=relatives, resume=resume, dry_run=dry_run), \
                    tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                sources = root / 'sources'
                sources.mkdir()
                output = root / 'output'
                diagnostics = []
                real_walk = os.walk
                for reverse in (False, True):
                    entries = list(reversed(relatives)) if reverse else relatives
                    rows = [(str(sources / os.path.dirname(relative)), [],
                             [os.path.basename(relative)]) for relative in entries]

                    def walk(path, *args, **kwargs):
                        if os.path.abspath(path) == str(sources):
                            return iter(rows)
                        return real_walk(path, *args, **kwargs)

                    with mock.patch.object(migrate.os, 'walk', side_effect=walk):
                        diagnostics.append(self._assert_rejected(
                            root, sources, output, relatives,
                            resume=resume, dry_run=dry_run))
                self.assertEqual(diagnostics[0], diagnostics[1])
                self.assertFalse(output.exists())

    def test_same_stem_in_distinct_subdirectories_runs_unique_worker_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = root / 'sources'
            relatives = [os.path.join(folder, 'Orders.twb')
                         for folder in ('North', 'South')]
            for relative in relatives:
                _source(sources, relative)
            output = root / 'output'
            records = []

            def publish(**kwargs):
                records.append((
                    kwargs['tableau_file'], kwargs['display_name'],
                    str(Path(kwargs['workbook_output_dir'], kwargs['basename']))))
                _publish(kwargs['basename'], kwargs['workbook_output_dir'])
                return {'success': True, 'report_name': kwargs['basename']}

            with mock.patch.object(migrate, '_migrate_single_workbook',
                                   side_effect=publish) as worker, \
                    mock.patch.object(migrate, 'run_batch_html_dashboard'), \
                    mock.patch.object(
                        migrate, '_print_batch_summary',
                        wraps=migrate._print_batch_summary) as summary, \
                    redirect_stdout(io.StringIO()):
                result = migrate.run_batch_migration(
                    str(sources), output_dir=str(output), verify_open=False)
            self.assertEqual(result, migrate.ExitCode.SUCCESS)
            self.assertEqual(worker.call_count, 2)
            self.assertEqual(records, [
                (str(sources / relative), os.path.splitext(relative)[0],
                 str(output / os.path.splitext(relative)[0]))
                for relative in relatives
            ])
            results = summary.call_args.args[0]
            self.assertEqual(set(results), {os.path.splitext(r)[0] for r in relatives})
            self.assertTrue(all(result['success'] for result in results.values()))
            checkpoint = json.loads(
                (output / '.migration_batch_state.json').read_text(encoding='utf-8'))
            self.assertEqual(set(checkpoint), set(relatives))
            for relative in relatives:
                self.assertEqual(checkpoint[relative]['status'], 'success')
                project = output / os.path.splitext(relative)[0]
                self.assertTrue((project / 'Orders.pbip').is_file())

    def test_impossible_budget_is_isolated_and_healthy_sibling_still_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = root / 'sources'
            output = Path(_root_at(root / 'output', 120))
            impossible_base = Path(_root_at(output, 210))
            deep_relative = impossible_base.relative_to(output) / 'TooDeep.twb'
            _source(sources, deep_relative)
            _source(sources, 'Healthy.twb')
            with self.assertRaises(DesktopPathError):
                plan_desktop_project_name(impossible_base, 'TooDeep')
            healthy = plan_desktop_project_name(output, 'Healthy')

            def publish(**kwargs):
                _publish(kwargs['basename'], kwargs['workbook_output_dir'])
                return {'success': True, 'report_name': kwargs['basename']}

            text = io.StringIO()
            with mock.patch.object(migrate, '_migrate_single_workbook',
                                   side_effect=publish) as worker, \
                    mock.patch.object(migrate, 'run_batch_html_dashboard'), \
                    mock.patch.object(
                        migrate, '_print_batch_summary',
                        wraps=migrate._print_batch_summary) as summary, \
                    redirect_stdout(text):
                result = migrate.run_batch_migration(
                    str(sources), output_dir=str(output), skip_extraction=True,
                    verify_open=False)
            self.assertEqual(result, migrate.ExitCode.BATCH_PARTIAL_FAIL)
            worker.assert_called_once()
            self.assertEqual(worker.call_args.kwargs['basename'], healthy)
            self.assertTrue((output / healthy / f'{healthy}.pbip').is_file())
            self.assertNotIn('Batch output project collisions', text.getvalue())
            results = summary.call_args.args[0]
            self.assertTrue(results['Healthy']['success'])
            self.assertFalse(results[str(deep_relative.with_suffix(''))]['success'])
            self.assertIn('shorter --output-dir',
                          results[str(deep_relative.with_suffix(''))]['error'])
