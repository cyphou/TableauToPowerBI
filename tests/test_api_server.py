"""Model-mode contracts at the REST option and isolated-worker boundaries."""

import io
import os
import subprocess
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from powerbi_import import api_server


class TestRateLimiting(unittest.TestCase):
    def setUp(self):
        self._tracker = {
            client_ip: api_server.collections.deque(timestamps)
            for client_ip, timestamps in api_server._rate_tracker.items()
        }
        self._rate_limit_max = api_server._RATE_LIMIT_MAX
        self._rate_limit_window = api_server._RATE_LIMIT_WINDOW
        with api_server._rate_lock:
            api_server._rate_tracker.clear()
        api_server._RATE_LIMIT_MAX = 2
        api_server._RATE_LIMIT_WINDOW = 60

    def tearDown(self):
        with api_server._rate_lock:
            api_server._rate_tracker.clear()
            api_server._rate_tracker.update(self._tracker)
        api_server._RATE_LIMIT_MAX = self._rate_limit_max
        api_server._RATE_LIMIT_WINDOW = self._rate_limit_window

    def test_limit_rejects_requests_after_per_ip_quota(self):
        with patch.object(api_server.time, 'monotonic', return_value=100.0):
            self.assertTrue(api_server._check_rate_limit('192.0.2.10'))
            self.assertTrue(api_server._check_rate_limit('192.0.2.10'))
            self.assertFalse(api_server._check_rate_limit('192.0.2.10'))

    def test_window_expires_request_at_exact_boundary(self):
        with patch.object(api_server.time, 'monotonic', side_effect=(100.0, 160.0)):
            self.assertTrue(api_server._check_rate_limit('192.0.2.10'))
            self.assertTrue(api_server._check_rate_limit('192.0.2.10'))

    def test_quota_is_isolated_per_ip(self):
        with patch.object(api_server.time, 'monotonic', return_value=100.0):
            self.assertTrue(api_server._check_rate_limit('192.0.2.10'))
            self.assertTrue(api_server._check_rate_limit('192.0.2.11'))
            self.assertTrue(api_server._check_rate_limit('192.0.2.10'))
            self.assertFalse(api_server._check_rate_limit('192.0.2.10'))

    def test_concurrent_requests_cannot_exceed_quota(self):
        with patch.object(api_server.time, 'monotonic', return_value=100.0):
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(
                    api_server._check_rate_limit,
                    ['192.0.2.10'] * 20,
                ))
        self.assertEqual(sum(results), api_server._RATE_LIMIT_MAX)

    def test_batch_endpoint_returns_429_without_creating_jobs(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('Synthetic.twb', '<workbook/>')
        body = archive.getvalue()

        class Request:
            headers = {
                'Content-Length': str(len(body)),
                'Content-Type': 'application/zip',
            }
            client_address = ('192.0.2.10', 12345)
            rfile = io.BytesIO(body)

            def _get_path(self):
                return '/migrate/batch'

            def _check_auth(self):
                return True

            def _get_query_params(self):
                return {}

            def _send_error(self, status, message):
                self.error = (status, message)

            def _send_json(self, data, status=200):
                self.response = (status, data)

        request = Request()
        with patch.object(api_server, '_check_rate_limit', return_value=False), \
                patch.object(api_server, '_new_job') as create_job, \
                patch.object(api_server.threading, 'Thread') as start_worker:
            api_server.MigrationHandler.do_POST(request)

        self.assertEqual(request.error[0], 429)
        create_job.assert_not_called()
        start_worker.assert_not_called()

    def test_batch_rate_limit_rejects_before_reading_upload_body(self):
        class UnreadableBody:
            def read(self, _size=-1):
                raise AssertionError('rate-limited request body must not be read')

        class Request:
            headers = {'Content-Length': '1000000'}
            client_address = ('192.0.2.10', 12345)
            rfile = UnreadableBody()

            def _get_path(self):
                return '/migrate/batch'

            def _check_auth(self):
                return True

            def _send_error(self, status, message):
                self.error = (status, message)

        request = Request()
        with patch.object(api_server, '_check_rate_limit', return_value=False), \
                patch.object(api_server, '_migration_options') as parse_options:
            api_server.MigrationHandler.do_POST(request)

        self.assertEqual(request.error[0], 429)
        parse_options.assert_not_called()


class TestApiAccessLogging(unittest.TestCase):
    def test_http_access_log_uses_module_logger(self):
        with self.assertLogs(api_server.logger, level='INFO') as captured:
            api_server.MigrationHandler.log_message(
                unittest.mock.MagicMock(), 'request %s', 'GET /health')

        self.assertIn('request GET /health', captured.output[0])


class TestMigrationModeOptions(unittest.TestCase):
    def test_omitted_mode_is_auto_and_explicit_modes_are_preserved(self):
        self.assertEqual(api_server._migration_options({})["model_mode"], "auto")
        for mode in ("auto", "import", "directquery", "composite"):
            with self.subTest(mode=mode):
                params = {"model_mode": [mode], "culture": ["en-US"]}
                options = api_server._migration_options(params)
                self.assertEqual(options["model_mode"], mode)
                self.assertEqual(options["culture"], "en-US")
                self.assertEqual(params, {"model_mode": [mode], "culture": ["en-US"]})

    def test_invalid_modes_are_rejected_before_worker_dispatch(self):
        for mode in ("", "unsupported", "--output-dir=outside", None):
            with self.subTest(mode=mode), patch.object(
                    api_server, "_run_upload_worker") as worker:
                with self.assertRaises(ValueError):
                    api_server._migration_options({"model_mode": [mode]})
                worker.assert_not_called()

    def test_raw_worker_omitted_mode_is_auto_and_import_override_survives(self):
        with tempfile.TemporaryDirectory(prefix="rest_mode_") as temporary:
            root = os.path.realpath(temporary)
            source = os.path.join(root, "Synthetic.twb")
            with open(source, "w", encoding="utf-8") as stream:
                stream.write("<workbook/>")
            output = os.path.join(root, "output")
            os.mkdir(output)
            cases = (
                (None, "auto"),
                ({}, "auto"),
                ({"model_mode": "import"}, "import"),
                ({"mode": "import"}, "import"),
                ({"model_mode": "auto", "mode": "import"}, "auto"),
            )
            for options, expected in cases:
                with self.subTest(options=options), patch.object(
                        api_server.subprocess, "run") as run:
                    run.return_value = subprocess.CompletedProcess([], 0, "", "")
                    api_server._run_upload_worker(source, root, output, options)
                    run.assert_called_once()
                    command = run.call_args.args[0]
                    self.assertEqual(command.count("--mode"), 1)
                    self.assertEqual(command[command.index("--mode") + 1], expected)
                    environment = run.call_args.kwargs["env"]
                    self.assertEqual(environment["TTPBI_UPLOAD_ROOT"], root)
                    self.assertFalse(os.path.exists(environment["TTPBI_EXTRACT_DIR"]))
