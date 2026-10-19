"""Regression tests for upload-root confinement and local source staging.

The copy audit-hook tests run in fresh interpreters because audit hooks are
process-global and cannot be removed once installed.
"""

import io
from email.message import Message
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.response
import zipfile
from unittest.mock import patch


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import migrate
from powerbi_import import api_server
from web import app as web_app


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as stream:
        stream.write(content)
    return path


class TestWebhookRedirectConfinement(unittest.TestCase):
    def test_redirect_cannot_reach_unvalidated_destination(self):
        requests = []

        class RedirectTransport(urllib.request.HTTPHandler):
            def http_open(self, request):
                requests.append(request.full_url)
                headers = Message()
                headers['Location'] = 'http://127.0.0.1/private'
                response = urllib.response.addinfourl(
                    io.BytesIO(b''), headers, request.full_url, 302)
                response.msg = 'Found'
                return response

        build_opener = urllib.request.build_opener
        transport = RedirectTransport()
        job = {'status': 'completed', 'error': None}
        public = 'http://webhook.example/complete'
        with (
            patch.object(api_server, '_get_job', return_value=job),
            patch.object(api_server.socket, 'getaddrinfo',
                         return_value=[(2, 1, 6, '', ('93.184.216.34', 80))]),
            patch.object(urllib.request, 'build_opener',
                         side_effect=lambda *handlers: build_opener(*handlers, transport)),
            self.assertLogs(api_server.logger, level='WARNING') as captured,
        ):
            api_server._fire_webhook('synthetic-job', public)
        self.assertEqual(requests, [public])
        self.assertIn('Webhook redirects are not allowed', captured.output[0])


class UploadTreeTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='upload_confinement_test_')
        self.addCleanup(self.temp.cleanup)
        self.root = os.path.realpath(self.temp.name)

    def workbook(self, relative='project/source.twb', body='<workbook/>'):
        return _write(os.path.join(self.root, relative), body)

    def package(self, relative='project/source.twbx', members=None):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with zipfile.ZipFile(path, 'w') as archive:
            for name, data in (members or {}).items():
                archive.writestr(name, data)
        return path


class TestUploadRootHelpers(UploadTreeTestCase):
    def test_upload_root_resolves_configured_root(self):
        with patch.dict(os.environ, {'TTPBI_UPLOAD_ROOT': self.root}):
            self.assertEqual(migrate._upload_root(), self.root)

    def test_confine_accepts_path_inside_one_of_multiple_roots(self):
        file_path = self.workbook()
        other_root = tempfile.TemporaryDirectory(prefix='other_upload_root_')
        self.addCleanup(other_root.cleanup)
        self.assertEqual(
            os.path.realpath(file_path),
            migrate._confine_upload_path(file_path, other_root.name, self.root),
        )

    def test_confine_rejects_path_outside_all_roots(self):
        outside = tempfile.TemporaryDirectory(prefix='outside_upload_root_')
        self.addCleanup(outside.cleanup)
        path = os.path.join(outside.name, 'secret.csv')
        _write(path, 'not for this upload')
        with self.assertRaises(ValueError):
            migrate._confine_upload_path(path, self.root)

    def test_confine_rejects_parent_traversal_even_when_target_exists(self):
        outside = tempfile.TemporaryDirectory(prefix='outside_upload_root_')
        self.addCleanup(outside.cleanup)
        source = _write(os.path.join(outside.name, 'secret.csv'), 'private')
        traversal = os.path.join(self.root, '..', os.path.basename(outside.name), 'secret.csv')
        self.assertTrue(os.path.exists(traversal))
        with self.assertRaises(ValueError):
            migrate._confine_upload_path(traversal, self.root)

    def test_confine_rejects_symlinked_file(self):
        target = _write(os.path.join(self.root, 'real.csv'), 'x')
        link = os.path.join(self.root, 'linked.csv')
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f'file symlinks unavailable: {exc}')
        with self.assertRaises(ValueError):
            migrate._confine_upload_path(link, self.root)

    def test_confine_rejects_symlinked_parent_directory(self):
        target_dir = tempfile.TemporaryDirectory(prefix='outside_link_target_')
        self.addCleanup(target_dir.cleanup)
        target = _write(os.path.join(target_dir.name, 'source.csv'), 'x')
        link_dir = os.path.join(self.root, 'linked')
        try:
            os.symlink(target_dir.name, link_dir, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f'directory symlinks unavailable: {exc}')
        with self.assertRaises(ValueError):
            migrate._confine_upload_path(os.path.join(link_dir, 'source.csv'), self.root)

    def test_relative_upload_path_is_preserved(self):
        self.assertEqual(
            'Data/source.csv',
            migrate._upload_relative_path('Data/source.csv'),
        )

    def test_relative_upload_path_rejects_parent_components(self):
        for value in ('../secret.csv', 'Data/../../secret.csv', '.\\..\\secret.csv'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                migrate._upload_relative_path(value)

    def test_relative_upload_path_rejects_absolute_and_unc_paths(self):
        for value in (
            '/etc/passwd',
            'C:\\outside\\secret.csv',
            '\\\\server\\share\\secret.csv',
            '//server/share/secret.csv',
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                migrate._upload_relative_path(value)


class TestUploadTreeValidation(UploadTreeTestCase):
    def test_valid_upload_tree_is_accepted(self):
        self.workbook(body='<workbook><datasources/></workbook>')
        _write(os.path.join(self.root, 'project', 'Data', 'source.csv'), 'a,b\n1,2\n')
        migrate._validate_upload_tree(self.root)

    def test_tree_rejects_symlinked_artifact(self):
        self.workbook()
        target = _write(os.path.join(self.root, 'outside.csv'), 'private')
        link = os.path.join(self.root, 'project', 'linked.csv')
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f'symlinks unavailable: {exc}')
        with self.assertRaises(ValueError):
            migrate._validate_upload_tree(self.root)

    def test_valid_package_references_relative_member(self):
        workbook = (
            '<workbook><datasources><datasource><connection '
            'class="textscan" filename="Data/source.csv"/></datasource>'
            '</datasources></workbook>'
        )
        self.package(members={'source.twb': workbook, 'Data/source.csv': 'x\n1\n'})
        migrate._validate_upload_tree(self.root)

    def test_package_rejects_traversing_zip_member(self):
        self.package(members={'source.twb': '<workbook/>', '../escaped.csv': 'secret'})
        with self.assertRaises((ValueError, zipfile.BadZipFile)):
            migrate._validate_upload_tree(self.root)

    def test_package_rejects_windows_absolute_zip_member(self):
        self.package(members={'source.twb': '<workbook/>', 'C:\\outside.csv': 'secret'})
        with self.assertRaises((ValueError, zipfile.BadZipFile)):
            migrate._validate_upload_tree(self.root)

    def test_package_rejects_symlink_member(self):
        path = os.path.join(self.root, 'project', 'source.twbx')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        info = zipfile.ZipInfo('Data/link.csv')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('source.twb', '<workbook/>')
            archive.writestr(info, '../../outside.csv')
        with self.assertRaises(ValueError):
            migrate._validate_upload_tree(self.root)

    def test_workbook_rejects_parent_relative_local_data_reference(self):
        xml = (
            '<workbook><datasources><datasource><connection '
            'class="textscan" filename="../outside.csv"/></datasource>'
            '</datasources></workbook>'
        )
        self.workbook(body=xml)
        with self.assertRaises(ValueError):
            migrate._validate_upload_tree(self.root)

    def test_workbook_rejects_absolute_local_data_reference(self):
        xml = (
            '<workbook><datasources><datasource><connection '
            'class="textscan" filename="C:\\outside\\source.csv"/></datasource>'
            '</datasources></workbook>'
        )
        self.workbook(body=xml)
        with self.assertRaises(ValueError):
            migrate._validate_upload_tree(self.root)

    def test_workbook_rejects_external_entity_declaration(self):
        xml = (
            '<!DOCTYPE workbook [<!ENTITY x SYSTEM "file:///secret">]>'
            '<workbook>&x;</workbook>'
        )
        self.workbook(body=xml)
        with self.assertRaises(ValueError):
            migrate._validate_upload_tree(self.root)


class TestLocalSourceConfinement(UploadTreeTestCase):
    def test_relative_local_data_file_is_found_in_workbook_folder(self):
        source = self.workbook('project/subdir/source.twb')
        csv_path = _write(
            os.path.join(self.root, 'project', 'subdir', 'Data', 'input.csv'),
            'a\n1\n',
        )
        with patch.dict(os.environ, {'TTPBI_UPLOAD_ROOT': self.root}):
            candidates = migrate._local_data_candidates(source, ['input.csv'])
        self.assertEqual([os.path.realpath(csv_path)], [
            os.path.realpath(p) for p in candidates['input.csv']
        ])

    def test_upload_search_does_not_read_parent_or_sibling_sources(self):
        source = self.workbook('project/workbook/source.twb')
        sibling = _write(os.path.join(self.root, 'project', 'parent.csv'), 'secret\n')
        outside = tempfile.TemporaryDirectory(prefix='parent_source_')
        self.addCleanup(outside.cleanup)
        external = _write(os.path.join(outside.name, 'external.csv'), 'secret\n')
        with patch.dict(os.environ, {'TTPBI_UPLOAD_ROOT': self.root}):
            with self.assertRaises(ValueError):
                migrate._local_data_candidates(
                    source, ['parent.csv', 'external.csv'],
                    search_root=outside.name,
                )
            # Parent discovery alone (no external search_root) finds no siblings.
            candidates = migrate._local_data_candidates(source, ['parent.csv'])
        self.assertEqual({}, candidates)
        self.assertTrue(os.path.isfile(sibling))
        self.assertTrue(os.path.isfile(external))

    def test_upload_search_rejects_symlinked_data(self):
        source = self.workbook()
        target_dir = tempfile.TemporaryDirectory(prefix='source_link_target_')
        self.addCleanup(target_dir.cleanup)
        target = _write(os.path.join(target_dir.name, 'secret.csv'), 'secret\n')
        link = os.path.join(self.root, 'project', 'secret.csv')
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f'symlinks unavailable: {exc}')
        with patch.dict(os.environ, {'TTPBI_UPLOAD_ROOT': self.root}):
            with self.assertRaises(ValueError):
                migrate._local_data_candidates(source, ['secret.csv'])

    @unittest.skipUnless(os.name == 'nt', 'Windows junctions only')
    def test_upload_search_rejects_windows_junction(self):
        source = self.workbook()
        target_dir = tempfile.TemporaryDirectory(prefix='junction_target_')
        self.addCleanup(target_dir.cleanup)
        _write(os.path.join(target_dir.name, 'secret.csv'), 'secret\n')
        link = os.path.join(self.root, 'project', 'junction')
        made = subprocess.run(['cmd', '/c', 'mklink', '/J', link, target_dir.name],
                              capture_output=True, text=True)
        if made.returncode != 0:
            self.skipTest('junctions unavailable: ' + (made.stderr or made.stdout))
        # rmdir removes only the junction, never the target contents.
        self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))
        with patch.dict(os.environ, {'TTPBI_UPLOAD_ROOT': self.root}):
            with self.assertRaises(ValueError):
                migrate._local_data_candidates(source, ['secret.csv'])
            with self.assertRaises(ValueError):
                migrate._validate_upload_tree(self.root)
            with self.assertRaises(ValueError):
                migrate._confine_upload_path(
                    os.path.join(link, 'secret.csv'), self.root)
        self.assertTrue(os.path.isfile(os.path.join(target_dir.name, 'secret.csv')))

    def test_local_cli_trust_keeps_parent_and_external_search_root(self):
        source = self.workbook('project/workbook/source.twb')
        sibling = _write(os.path.join(self.root, 'project', 'parent.csv'), 'a\n')
        outside = tempfile.TemporaryDirectory(prefix='trusted_search_root_')
        self.addCleanup(outside.cleanup)
        external = _write(os.path.join(outside.name, 'external.csv'), 'a\n')
        env = {k: v for k, v in os.environ.items() if k != 'TTPBI_UPLOAD_ROOT'}
        with patch.dict(os.environ, env, clear=True):
            found = migrate._local_data_candidates(
                source, ['parent.csv', 'external.csv'], search_root=outside.name)
        self.assertEqual([sibling], [os.path.normpath(p) for p in found['parent.csv']])
        self.assertEqual([external], [os.path.normpath(p) for p in found['external.csv']])

    def test_datafolder_inside_upload_project_is_accepted(self):
        project = os.path.join(self.root, 'project')
        data_dir = os.path.join(project, 'Data')
        os.makedirs(data_dir)
        expr = _write(
            os.path.join(project, 'definition', 'expressions.tmdl'),
            f'expression DataFolder = "{data_dir}"\n',
        )
        with patch.dict(os.environ, {'TTPBI_UPLOAD_ROOT': self.root}):
            content, data_folder = migrate._read_data_folder(expr)
        self.assertIn('DataFolder', content)
        self.assertEqual(os.path.realpath(data_dir), os.path.realpath(data_folder))

    def test_datafolder_outside_upload_root_is_rejected(self):
        outside = tempfile.TemporaryDirectory(prefix='external_datafolder_')
        self.addCleanup(outside.cleanup)
        project = os.path.join(self.root, 'project')
        os.makedirs(project)
        expr = _write(
            os.path.join(project, 'expressions.tmdl'),
            f'expression DataFolder = "{outside.name}"\n',
        )
        with patch.dict(os.environ, {'TTPBI_UPLOAD_ROOT': self.root}):
            with self.assertRaises(ValueError):
                migrate._read_data_folder(expr)


class TestUploadCopyAuditGuard(UploadTreeTestCase):
    def _guard_process(self, output_dir, source, destination, should_allow):
        script = (
            "import shutil, sys, threading\n"
            "import migrate\n"
            "migrate._install_upload_copy_guard(sys.argv[1])\n"
            "result = []\n"
            "def copy_file():\n"
            "    try:\n"
            "        shutil.copy2(sys.argv[2], sys.argv[3])\n"
            "        result.append('copied')\n"
            "    except OSError:\n"
            "        result.append('blocked')\n"
            "thread = threading.Thread(target=copy_file)\n"
            "thread.start()\n"
            "thread.join()\n"
            "print(result[0])\n"
        )
        env = os.environ.copy()
        env['TTPBI_UPLOAD_ROOT'] = self.root
        env['PYTHONPATH'] = PROJECT_ROOT + os.pathsep + env.get('PYTHONPATH', '')
        completed = subprocess.run(
            [sys.executable, '-c', script, output_dir, source, destination],
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        expected = 'copied' if should_allow else 'blocked'
        self.assertEqual(completed.stdout.strip(), expected)

    def _run_guarded(self, output_dir, body, *args):
        script = (
            "import shutil, sys, threading\n"
            "from shutil import copy2 as imported_copy2\n"
            "import migrate\n"
            "migrate._install_upload_copy_guard(sys.argv[1])\n"
            "result = []\n"
            "def work():\n"
            "    try:\n"
            + "".join('        ' + line + '\n' for line in body)
            + "        result.append('allowed')\n"
            "    except (OSError, shutil.Error):\n"
            "        result.append('blocked')\n"
            "thread = threading.Thread(target=work)\n"
            "thread.start()\n"
            "thread.join()\n"
            "print(result[0])\n"
        )
        env = os.environ.copy()
        env['TTPBI_UPLOAD_ROOT'] = self.root
        env['PYTHONPATH'] = PROJECT_ROOT + os.pathsep + env.get('PYTHONPATH', '')
        completed = subprocess.run(
            [sys.executable, '-c', script, output_dir, *args],
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def test_late_copy_from_external_source_is_blocked_across_thread(self):
        output = os.path.join(self.root, 'output')
        os.makedirs(output)
        outside = tempfile.TemporaryDirectory(prefix='copy_guard_source_')
        self.addCleanup(outside.cleanup)
        secret = _write(os.path.join(outside.name, 'secret.csv'), 'private\n')
        destination = os.path.join(output, 'stolen.csv')
        result = self._run_guarded(
            output, ['shutil.copy2(sys.argv[2], sys.argv[3])'], secret, destination)
        self.assertEqual('blocked', result)
        self.assertFalse(os.path.exists(destination))

    def test_preimported_copy2_rejects_external_source_across_thread(self):
        output = os.path.join(self.root, 'output')
        os.makedirs(output)
        outside = tempfile.TemporaryDirectory(prefix='preimported_copy_source_')
        self.addCleanup(outside.cleanup)
        secret = _write(os.path.join(outside.name, 'secret.csv'), 'private\n')
        destination = os.path.join(output, 'stolen.csv')
        self.assertEqual('blocked', self._run_guarded(
            output, ['imported_copy2(sys.argv[2], sys.argv[3])'],
            secret, destination))
        self.assertFalse(os.path.exists(destination))

    def test_copytree_default_copy_function_rejects_external_file_symlink(self):
        output = os.path.join(self.root, 'output')
        source = os.path.join(self.root, 'tree')
        os.makedirs(output)
        os.makedirs(source)
        outside = tempfile.TemporaryDirectory(prefix='copytree_external_source_')
        self.addCleanup(outside.cleanup)
        secret = _write(os.path.join(outside.name, 'secret.csv'), 'private\n')
        try:
            os.symlink(secret, os.path.join(source, 'linked.csv'))
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f'file symlinks unavailable: {exc}')
        destination = os.path.join(output, 'copied')
        self.assertEqual('blocked', self._run_guarded(
            output, ['shutil.copytree(sys.argv[2], sys.argv[3])'],
            source, destination))
        self.assertFalse(os.path.lexists(os.path.join(destination, 'linked.csv')))
        if os.path.isdir(destination):
            self.assertEqual([], os.listdir(destination))

    def test_generator_catching_guard_denial_leaves_security_violation_flag(self):
        output = os.path.join(self.root, 'output')
        os.makedirs(output)
        outside = tempfile.TemporaryDirectory(prefix='caught_guard_source_')
        self.addCleanup(outside.cleanup)
        secret = _write(os.path.join(outside.name, 'secret.csv'), 'private\n')
        destination = os.path.join(output, 'stolen.csv')
        self.assertEqual('allowed', self._run_guarded(output, [
            'assert migrate._upload_security_violation is False',
            'def generator():',
            '    try:',
            '        imported_copy2(sys.argv[2], sys.argv[3])',
            '    except OSError:',
            '        return "fallback"',
            'assert generator() == "fallback"',
            'assert migrate._upload_security_violation is True',
        ], secret, destination))
        self.assertFalse(os.path.exists(destination))

    def test_late_direct_open_of_external_data_is_blocked_across_thread(self):
        output = os.path.join(self.root, 'output')
        os.makedirs(output)
        outside = tempfile.TemporaryDirectory(prefix='open_guard_source_')
        self.addCleanup(outside.cleanup)
        secret = _write(os.path.join(outside.name, 'secret.csv'), 'private\n')
        for body in (["open(sys.argv[2], 'rb').read()"],
                     ["open(sys.argv[2], encoding='utf-8').read()"]):
            with self.subTest(body=body[0]):
                self.assertEqual('blocked', self._run_guarded(output, body, secret))

    def test_late_direct_open_of_upload_data_is_allowed_across_thread(self):
        output = os.path.join(self.root, 'output')
        os.makedirs(output)
        own = _write(os.path.join(self.root, 'project', 'own.csv'), 'a\n')
        self.assertEqual('allowed', self._run_guarded(
            output, ["open(sys.argv[2], 'rb').read()"], own))

    def test_copy_from_upload_to_private_output_is_allowed_across_thread(self):
        source = self.workbook()
        output = os.path.join(self.root, 'output')
        os.makedirs(output)
        self._guard_process(output, source, os.path.join(output, 'copy.twb'), True)

    def test_copy_from_upload_to_external_destination_is_blocked_across_thread(self):
        source = self.workbook()
        output = os.path.join(self.root, 'output')
        os.makedirs(output)
        outside = tempfile.TemporaryDirectory(prefix='copy_guard_destination_')
        self.addCleanup(outside.cleanup)
        destination = os.path.join(outside.name, 'stolen.twb')
        self._guard_process(output, source, destination, False)
        self.assertFalse(os.path.exists(destination))


class TestUploadWorkersAndEntrypoints(UploadTreeTestCase):
    def test_rest_options_keep_model_mode_and_reject_unsupported_fields(self):
        options = api_server._migration_options({
            'model_mode': ['directquery'],
            'culture': ['en-US'],
        })
        self.assertEqual('directquery', options['model_mode'])
        with self.assertRaises(ValueError):
            api_server._migration_options({
                'model_mode': ['unsupported'],
            })
        with self.assertRaises(ValueError):
            api_server._migration_options({
                'unexpected_cli_option': ['--output-dir=/outside'],
            })

    def test_worker_forwards_model_mode_and_private_roots(self):
        source = self.workbook()
        output = os.path.join(self.root, 'output')
        os.makedirs(output)
        captured = {}

        def fake_run(command, **kwargs):
            captured['command'] = command
            captured.update(kwargs)
            return subprocess.CompletedProcess(command, 0, '', '')

        with patch.object(api_server.subprocess, 'run', side_effect=fake_run):
            api_server._run_upload_worker(
                source, self.root, output,
                {'model_mode': 'composite', 'culture': 'en-US'},
            )
        command = captured['command']
        self.assertIn('composite', command)
        self.assertIn('--mode', command)
        self.assertIn('en-US', command)
        self.assertEqual(self.root, captured['env']['TTPBI_UPLOAD_ROOT'])
        self.assertTrue(os.path.commonpath(
            [output, captured['env']['TTPBI_EXTRACT_DIR']]
        ) == output)

    def test_worker_rejects_external_input_before_starting_subprocess(self):
        outside = tempfile.TemporaryDirectory(prefix='outside_worker_input_')
        self.addCleanup(outside.cleanup)
        source = _write(os.path.join(outside.name, 'source.twb'), '<workbook/>')
        with patch.object(api_server.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                api_server._run_upload_worker(
                    source, self.root, self.root, {'model_mode': 'import'},
                )
        run.assert_not_called()

    def test_concurrent_worker_contexts_keep_upload_roots_separate(self):
        roots, captured = [], []
        for index in range(2):
            root = tempfile.TemporaryDirectory(prefix=f'concurrent_upload_{index}_')
            self.addCleanup(root.cleanup)
            roots.append(os.path.realpath(root.name))
            _write(os.path.join(root.name, f'book{index}.twb'), '<workbook/>')
            os.mkdir(os.path.join(root.name, 'output'))

        def fake_run(command, **kwargs):
            captured.append((command, kwargs['env']['TTPBI_UPLOAD_ROOT']))
            return subprocess.CompletedProcess(command, 0, '', '')

        errors = []

        def invoke(index):
            try:
                api_server._run_upload_worker(
                    os.path.join(roots[index], f'book{index}.twb'),
                    roots[index], os.path.join(roots[index], 'output'), {},
                )
            except Exception as exc:  # surfaced in the joining test thread
                errors.append(exc)

        with patch.object(api_server.subprocess, 'run', side_effect=fake_run):
            threads = [threading.Thread(target=invoke, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertEqual([], errors)
        self.assertCountEqual(roots, [root for _, root in captured])

    def test_rest_job_worker_uses_its_recorded_upload_root(self):
        source = self.workbook()
        job_id = api_server._new_job(source, upload_root=self.root)
        calls = []

        def fake_worker(input_path, upload_root, output_dir, options):
            calls.append((input_path, upload_root, output_dir, options))
            _write(os.path.join(output_dir, 'result.txt'), 'complete')

        with patch.object(api_server, '_run_upload_worker', side_effect=fake_worker):
            api_server._run_migration(job_id, source, {'model_mode': 'directquery'})
        job = api_server._get_job(job_id)
        try:
            self.assertEqual('completed', job['status'])
            self.assertEqual(1, len(calls))
            self.assertEqual(self.root, calls[0][1])
            self.assertEqual('directquery', calls[0][3]['model_mode'])
            self.assertTrue(
                os.path.commonpath([job['job_dir'], calls[0][2]]) == job['job_dir']
            )
        finally:
            shutil.rmtree(job['job_dir'], ignore_errors=True)

    def test_ui_pipeline_passes_confined_upload_and_output_roots_to_worker(self):
        source = self.workbook()
        output = os.path.join(self.root, 'output')
        calls = []

        def fake_worker(input_path, upload_root, output_dir, options):
            calls.append((input_path, upload_root, output_dir))
            os.makedirs(os.path.join(output_dir, 'source'), exist_ok=True)

        with patch.object(web_app, '_run_upload_worker', side_effect=fake_worker):
            with patch('powerbi_import.validator.ArtifactValidator.validate_project',
                       return_value={'valid': True}):
                result = web_app._run_migration_pipeline(
                    source, {'upload_root': self.root, 'output_dir': output},
                )
        self.assertTrue(result['success'], result['errors'])
        self.assertEqual(1, len(calls))
        self.assertEqual(os.path.realpath(source), os.path.realpath(calls[0][0]))
        self.assertEqual(self.root, calls[0][1])
        self.assertEqual(os.path.realpath(output), os.path.realpath(calls[0][2]))

    def test_fallback_ui_upload_routes_through_confined_pipeline(self):
        from http.server import HTTPServer

        ready = threading.Event()
        server_ref = {}

        class CapturingServer(HTTPServer):
            def __init__(self, address, handler):
                super().__init__(address, handler)
                server_ref['server'] = self
                ready.set()

        pipeline_calls = []

        def fake_pipeline(file_path, options):
            # Capture actual uploaded location while the per-request temporary
            # directory is still alive.
            pipeline_calls.append((file_path, dict(options), os.path.isfile(file_path)))
            return {'success': True, 'errors': [], 'stats': {}}

        thread = threading.Thread(
            target=web_app.run_fallback_server, kwargs={'port': 0}, daemon=True,
        )
        with patch('http.server.HTTPServer', CapturingServer):
            with patch.object(web_app, '_run_migration_pipeline',
                              side_effect=fake_pipeline):
                thread.start()
                self.assertTrue(ready.wait(5), 'fallback server did not start')
                server = server_ref['server']
                try:
                    boundary = 'upload-source-test-boundary'
                    body = (
                        f'--{boundary}\r\n'
                        'Content-Disposition: form-data; name="file"; filename="book.twb"\r\n'
                        'Content-Type: application/octet-stream\r\n\r\n'
                        '<workbook/>\r\n'
                        f'--{boundary}--\r\n'
                    ).encode('utf-8')
                    request = urllib.request.Request(
                        f'http://127.0.0.1:{server.server_port}/migrate',
                        data=body,
                        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
                        method='POST',
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(200, response.status)
                finally:
                    server.shutdown()
                    server.server_close()
        self.assertEqual(1, len(pipeline_calls))
        file_path, options, existed_during_pipeline = pipeline_calls[0]
        self.assertTrue(existed_during_pipeline)
        self.assertEqual(os.path.dirname(file_path), options['upload_root'])
        self.assertEqual(
            os.path.join(options['upload_root'], 'output'),
            options['output_dir'],
        )

    def test_ui_pipeline_rejects_output_outside_private_root(self):
        source = self.workbook()
        outside = tempfile.TemporaryDirectory(prefix='outside_ui_output_')
        self.addCleanup(outside.cleanup)
        with patch.object(web_app, '_run_upload_worker') as worker:
            result = web_app._run_migration_pipeline(
                source, {'upload_root': self.root, 'output_dir': outside.name},
            )
        self.assertFalse(result['success'])
        self.assertTrue(result['errors'])
        worker.assert_not_called()

    def test_archive_writer_accepts_only_job_scoped_output(self):
        output = os.path.join(self.root, 'output')
        _write(os.path.join(output, 'artifact.txt'), 'contents')
        archive_bytes = io.BytesIO()
        api_server._write_output_archive(output, archive_bytes, self.root)
        with zipfile.ZipFile(io.BytesIO(archive_bytes.getvalue())) as archive:
            self.assertEqual(['artifact.txt'], archive.namelist())
            self.assertEqual(b'contents', archive.read('artifact.txt'))

    def test_archive_writer_rejects_external_output_directory(self):
        outside = tempfile.TemporaryDirectory(prefix='outside_archive_')
        self.addCleanup(outside.cleanup)
        _write(os.path.join(outside.name, 'artifact.txt'), 'contents')
        with self.assertRaises(ValueError):
            api_server._write_output_archive(outside.name, io.BytesIO(), self.root)

    def test_archive_writer_rejects_external_file_destination_without_overwrite(self):
        output = os.path.join(self.root, 'output')
        _write(os.path.join(output, 'artifact.txt'), 'contents')
        outside = tempfile.TemporaryDirectory(prefix='external_archive_destination_')
        self.addCleanup(outside.cleanup)
        destination = _write(os.path.join(outside.name, 'result.zip'), 'keep intact')
        with self.assertRaises(ValueError):
            api_server._write_output_archive(output, destination, self.root)
        with open(destination, encoding='utf-8') as stream:
            self.assertEqual('keep intact', stream.read())

    def test_archive_writer_rejects_link_inside_output(self):
        output = os.path.join(self.root, 'output')
        os.makedirs(output)
        target = _write(os.path.join(self.root, 'outside.txt'), 'private')
        try:
            os.symlink(target, os.path.join(output, 'linked.txt'))
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f'symlinks unavailable: {exc}')
        with self.assertRaises(ValueError):
            api_server._write_output_archive(output, io.BytesIO(), self.root)


if __name__ == '__main__':
    unittest.main()


_CSV_TWB = (
    "<?xml version='1.0' encoding='utf-8'?>"
    "<workbook version='18.1'><datasources>"
    "<datasource caption='Sales' name='federated.1'>"
    "<connection class='federated'><named-connections>"
    "<named-connection caption='sales' name='textscan.1'>"
    "<connection class='textscan' directory='Data' filename='sales.csv' "
    "password='' server=''/></named-connection></named-connections>"
    "<relation connection='textscan.1' name='sales.csv' table='[sales#csv]' "
    "type='table'><columns header='yes' separator=','>"
    "<column datatype='string' name='a' ordinal='0'/>"
    "<column datatype='integer' name='b' ordinal='1'/></columns></relation>"
    "<metadata-records>"
    "<metadata-record class='column'><remote-name>a</remote-name>"
    "<remote-type>130</remote-type><local-name>[a]</local-name>"
    "<parent-name>[sales.csv]</parent-name><local-type>string</local-type>"
    "</metadata-record>"
    "<metadata-record class='column'><remote-name>b</remote-name>"
    "<remote-type>20</remote-type><local-name>[b]</local-name>"
    "<parent-name>[sales.csv]</parent-name><local-type>integer</local-type>"
    "</metadata-record></metadata-records></connection>"
    "<column datatype='string' name='[a]' role='dimension' type='nominal'/>"
    "<column datatype='integer' name='[b]' role='measure' type='quantitative'/>"
    "</datasource></datasources><worksheets/></workbook>"
)


class TestHostileWorkerMigrations(UploadTreeTestCase):
    """Real UI/REST worker subprocesses must report traversal as failure."""

    def _hostile_upload(self):
        upload_root = os.path.join(self.root, 'upload')
        secret = 'HOST_ONLY_SECRET_8c72e610'
        external = _write(os.path.join(self.root, 'external.csv'),
                          f'a,b\n{secret},2\n')
        workbook = _CSV_TWB.replace(
            "directory='Data' filename='sales.csv'",
            "directory='' filename='../external.csv'",
        )
        source = _write(os.path.join(upload_root, 'w.twb'), workbook)
        return upload_root, source, external, secret

    def _assert_no_secret_archived(self, output, job_dir, secret):
        self.assertTrue(os.path.isdir(output))
        archive_bytes = io.BytesIO()
        api_server._write_output_archive(output, archive_bytes, job_dir)
        with zipfile.ZipFile(io.BytesIO(archive_bytes.getvalue())) as archive:
            for name in archive.namelist():
                with self.subTest(artifact=name):
                    self.assertNotEqual('external.csv', os.path.basename(name))
                    self.assertNotIn(secret.encode('utf-8'), archive.read(name))

    def test_ui_worker_explicitly_fails_parent_relative_source_without_archiving_secret(self):
        upload_root, source, external, secret = self._hostile_upload()
        output = os.path.join(upload_root, 'output')
        result = web_app._run_migration_pipeline(
            source, {'upload_root': upload_root, 'output_dir': output},
        )
        self.assertFalse(result['success'])
        self.assertTrue(result['errors'])
        self.assertIn('Confined upload migration failed', '\n'.join(result['errors']))
        self.assertIn('parent traversal', '\n'.join(result['errors']))
        self._assert_no_secret_archived(output, upload_root, secret)
        with open(external, encoding='utf-8') as stream:
            self.assertEqual(f'a,b\n{secret},2\n', stream.read())

    def test_rest_worker_fails_traversal_and_cleans_only_its_own_upload_root(self):
        upload_root, source, external, secret = self._hostile_upload()
        other_upload = _write(os.path.join(self.root, 'other_upload', 'keep.csv'),
                              'unrelated upload')
        job_id = api_server._new_job(source, upload_root=upload_root)
        api_server._run_migration(job_id, source, {'model_mode': 'import'})
        job = api_server._get_job(job_id)
        if job.get('job_dir'):
            self.addCleanup(shutil.rmtree, job['job_dir'], ignore_errors=True)
        self.assertEqual('failed', job['status'])
        self.assertIn('Confined upload migration failed', job['error'])
        self.assertIn('parent traversal', job['error'])
        self.assertFalse(os.path.exists(upload_root))
        with open(external, encoding='utf-8') as stream:
            self.assertEqual(f'a,b\n{secret},2\n', stream.read())
        with open(other_upload, encoding='utf-8') as stream:
            self.assertEqual('unrelated upload', stream.read())
        self._assert_no_secret_archived(job['output_dir'], job['job_dir'], secret)


class TestPositiveWorkerMigrations(UploadTreeTestCase):
    """Real worker subprocesses: confined uploads with relative data succeed."""

    def _migrate(self, source):
        output = os.path.join(self.root, 'out')
        os.makedirs(output)
        api_server._run_upload_worker(source, self.root, output, {'model_mode': 'import'})
        return output

    def _assert_project(self, output):
        project = os.path.join(output, 'w')
        self.assertTrue(os.path.isfile(os.path.join(project, 'w.pbip')))
        self.assertTrue(os.path.isfile(
            os.path.join(project, 'w.SemanticModel', 'definition', 'tables', 'sales.csv.tmdl')))
        return project

    def test_worker_migrates_workbook_with_relative_csv(self):
        _write(os.path.join(self.root, 'p', 'Data', 'sales.csv'), 'a,b\nx,2\n')
        source = _write(os.path.join(self.root, 'p', 'w.twb'), _CSV_TWB)
        migrate._validate_upload_tree(self.root)
        project = self._assert_project(self._migrate(source))
        with open(os.path.join(project, 'Data', 'sales.csv'), encoding='utf-8') as f:
            self.assertEqual('a,b\nx,2\n', f.read())

    def test_worker_migrates_package_with_relative_csv(self):
        source = self.package('p/w.twbx', {'w.twb': _CSV_TWB, 'Data/sales.csv': 'a,b\nx,2\n'})
        migrate._validate_upload_tree(self.root)
        self._assert_project(self._migrate(source))

    def test_local_cli_without_upload_root_keeps_trusted_access(self):
        work = os.path.join(self.root, 'cli')
        _write(os.path.join(work, 'Data', 'sales.csv'), 'a,b\nx,2\n')
        source = _write(os.path.join(work, 'w.twb'), _CSV_TWB)
        output = os.path.join(work, 'out')
        os.makedirs(output)
        env = {k: v for k, v in os.environ.items()
               if k not in ('TTPBI_UPLOAD_ROOT', 'TTPBI_EXTRACT_DIR')}
        env['PYTHONIOENCODING'] = 'utf-8'
        completed = subprocess.run(
            [sys.executable, os.path.join(PROJECT_ROOT, 'migrate.py'), source,
             '--output-dir', output],
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=300,
        )
        self.assertEqual(0, completed.returncode, completed.stdout[-2000:] + completed.stderr[-2000:])
        self._assert_project(output)
