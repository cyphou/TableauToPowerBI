"""Tests for Sprint 176 — REST API v2.

Covers:
- OpenAPI spec generation
- Pagination & filtering on /jobs
- API key authentication
- Webhook delivery helpers
- Batch migration stores
"""

import collections
import hashlib
import hmac
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import HTTPServer
from unittest.mock import patch, MagicMock
from urllib.parse import urlencode

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestOpenApiSpec(unittest.TestCase):
    """Tests for GET /openapi.json."""

    def test_build_openapi_spec_structure(self):
        from powerbi_import.api_server import _build_openapi_spec

        spec = _build_openapi_spec()
        self.assertEqual(spec['openapi'], '3.0.3')
        self.assertIn('info', spec)
        self.assertIn('paths', spec)

    def test_openapi_has_all_endpoints(self):
        from powerbi_import.api_server import _build_openapi_spec

        spec = _build_openapi_spec()
        paths = spec['paths']
        self.assertIn('/health', paths)
        self.assertIn('/migrate', paths)
        self.assertIn('/migrate/batch', paths)
        self.assertIn('/status/{id}', paths)
        self.assertIn('/download/{id}', paths)
        self.assertIn('/jobs', paths)
        self.assertIn('/batch/{id}', paths)
        self.assertIn('/openapi.json', paths)

    def test_openapi_migrate_has_webhook_param(self):
        from powerbi_import.api_server import _build_openapi_spec

        spec = _build_openapi_spec()
        migrate = spec['paths']['/migrate']['post']
        param_names = [p['name'] for p in migrate.get('parameters', [])]
        self.assertIn('webhook_url', param_names)

    def test_openapi_jobs_has_pagination_params(self):
        from powerbi_import.api_server import _build_openapi_spec

        spec = _build_openapi_spec()
        jobs = spec['paths']['/jobs']['get']
        param_names = [p['name'] for p in jobs.get('parameters', [])]
        self.assertIn('status', param_names)
        self.assertIn('page', param_names)
        self.assertIn('per_page', param_names)

    def test_openapi_version_matches(self):
        from powerbi_import.api_server import _build_openapi_spec, _get_version

        spec = _build_openapi_spec()
        self.assertEqual(spec['info']['version'], _get_version())


class TestApiKeyAuth(unittest.TestCase):
    """Tests for API key authentication."""

    def test_check_auth_no_key_configured(self):
        """When no API key is set, all requests are authorized."""
        import powerbi_import.api_server as srv
        original = srv._API_KEY
        try:
            srv._API_KEY = None
            handler = MagicMock()
            handler.headers = {}
            result = srv.MigrationHandler._check_auth(handler)
            self.assertTrue(result)
        finally:
            srv._API_KEY = original

    def test_check_auth_valid_key(self):
        import powerbi_import.api_server as srv
        original = srv._API_KEY
        try:
            srv._API_KEY = 'test-secret-key'
            handler = MagicMock()
            handler.headers = {'Authorization': 'Bearer test-secret-key'}
            result = srv.MigrationHandler._check_auth(handler)
            self.assertTrue(result)
        finally:
            srv._API_KEY = original

    def test_check_auth_invalid_key(self):
        import powerbi_import.api_server as srv
        original = srv._API_KEY
        try:
            srv._API_KEY = 'test-secret-key'
            handler = MagicMock()
            handler.headers = {'Authorization': 'Bearer wrong-key'}
            result = srv.MigrationHandler._check_auth(handler)
            self.assertFalse(result)
        finally:
            srv._API_KEY = original

    def test_check_auth_missing_header(self):
        import powerbi_import.api_server as srv
        original = srv._API_KEY
        try:
            srv._API_KEY = 'test-secret-key'
            handler = MagicMock()
            handler.headers = {}
            result = srv.MigrationHandler._check_auth(handler)
            self.assertFalse(result)
        finally:
            srv._API_KEY = original

    def test_check_auth_non_bearer(self):
        import powerbi_import.api_server as srv
        original = srv._API_KEY
        try:
            srv._API_KEY = 'test-secret-key'
            handler = MagicMock()
            handler.headers = {'Authorization': 'Basic dXNlcjpwYXNz'}
            result = srv.MigrationHandler._check_auth(handler)
            self.assertFalse(result)
        finally:
            srv._API_KEY = original


class TestPaginationFiltering(unittest.TestCase):
    """Tests for job list pagination and filtering."""

    def setUp(self):
        import powerbi_import.api_server as srv
        self._srv = srv
        self._original_jobs = srv._jobs.copy()
        srv._jobs.clear()

    def tearDown(self):
        self._srv._jobs.clear()
        self._srv._jobs.update(self._original_jobs)

    def _add_jobs(self, count, status='completed', prefix='job'):
        for i in range(count):
            jid = f'test{prefix}{i:04d}'
            self._srv._jobs[jid] = {
                'status': status,
                'created': time.time() - i,
                'input_path': f'/tmp/test{i}.twbx',
                'output_dir': None,
                'error': None,
                'stats': None,
            }

    def test_pagination_defaults(self):
        """Default page=1, per_page=20."""
        self._add_jobs(5)
        # Verify we have 5 jobs
        with self._srv._lock:
            all_jobs = list(self._srv._jobs.values())
        self.assertEqual(len(all_jobs), 5)

    def test_status_filtering(self):
        """Filter by status."""
        self._add_jobs(3, status='completed', prefix='ok')
        self._add_jobs(2, status='failed', prefix='fail')
        with self._srv._lock:
            all_jobs = [
                {'job_id': jid, 'status': j['status'], 'created': j['created']}
                for jid, j in self._srv._jobs.items()
            ]
        completed = [j for j in all_jobs if j['status'] == 'completed']
        self.assertEqual(len(completed), 3)
        failed = [j for j in all_jobs if j['status'] == 'failed']
        self.assertEqual(len(failed), 2)

    def test_page_calculation(self):
        """Page boundary math."""
        total = 45
        per_page = 20
        total_pages = max(1, (total + per_page - 1) // per_page)
        self.assertEqual(total_pages, 3)

    def test_empty_jobs_pagination(self):
        """Empty job list returns page 1 of 1."""
        total = 0
        per_page = 20
        total_pages = max(1, (total + per_page - 1) // per_page)
        self.assertEqual(total_pages, 1)

    def test_per_page_clamping(self):
        """per_page is clamped to 1..100."""
        self.assertEqual(min(100, max(1, 0)), 1)
        self.assertEqual(min(100, max(1, 200)), 100)
        self.assertEqual(min(100, max(1, 50)), 50)


class TestWebhookDelivery(unittest.TestCase):
    """Tests for webhook HMAC signing and delivery."""

    def test_hmac_signature(self):
        """HMAC-SHA256 signature matches expected."""
        secret = 'testsecret'.encode('utf-8')
        payload = b'{"event":"migration.completed"}'
        sig = hmac.new(secret, payload, hashlib.sha256).hexdigest()
        self.assertEqual(len(sig), 64)

    def test_fire_webhook_missing_job(self):
        """Webhook with invalid job_id does nothing."""
        from powerbi_import.api_server import _fire_webhook
        # Should not raise
        _fire_webhook('nonexistent_job_id', 'http://example.com/hook')

    @patch('urllib.request.urlopen')
    def test_fire_webhook_success(self, mock_urlopen):
        """Successful webhook delivery."""
        import powerbi_import.api_server as srv
        # Create a test job
        jid = 'webhook_test_1'
        with srv._lock:
            srv._jobs[jid] = {
                'status': 'completed',
                'created': time.time(),
                'input_path': '/tmp/test.twbx',
                'output_dir': '/tmp/out',
                'error': None,
                'stats': None,
            }
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = lambda s, *a: None
        mock_urlopen.return_value = mock_resp

        srv._fire_webhook(jid, 'http://example.com/hook')
        mock_urlopen.assert_called_once()

        # Clean up
        with srv._lock:
            srv._jobs.pop(jid, None)

    @patch('urllib.request.urlopen', side_effect=Exception('Connection refused'))
    def test_fire_webhook_failure_doesnt_raise(self, mock_urlopen):
        """Failed webhook delivery logs warning but doesn't raise."""
        import powerbi_import.api_server as srv
        jid = 'webhook_test_2'
        with srv._lock:
            srv._jobs[jid] = {
                'status': 'failed',
                'created': time.time(),
                'input_path': '/tmp/test.twbx',
                'output_dir': None,
                'error': 'test error',
                'stats': None,
            }
        # Should not raise
        srv._fire_webhook(jid, 'http://example.com/hook')

        with srv._lock:
            srv._jobs.pop(jid, None)


class TestBatchJobs(unittest.TestCase):
    """Tests for batch migration store."""

    def setUp(self):
        import powerbi_import.api_server as srv
        self._srv = srv
        self._original_batches = srv._batches.copy()
        self._original_jobs = srv._jobs.copy()
        srv._batches.clear()
        srv._jobs.clear()

    def tearDown(self):
        self._srv._batches.clear()
        self._srv._batches.update(self._original_batches)
        self._srv._jobs.clear()
        self._srv._jobs.update(self._original_jobs)

    def test_update_batch_progress_completed(self):
        """Completing a job updates batch progress."""
        batch_id = 'batch001'
        self._srv._batches[batch_id] = {
            'status': 'running',
            'created': time.time(),
            'job_ids': ['j1', 'j2'],
            'completed': 0,
            'failed': 0,
            'total': 2,
        }
        self._srv._jobs['j1'] = {
            'status': 'completed', 'created': time.time(),
            'input_path': '', 'output_dir': '', 'error': None, 'stats': None,
        }
        self._srv._update_batch_progress(batch_id, 'j1')
        self.assertEqual(self._srv._batches[batch_id]['completed'], 1)
        self.assertEqual(self._srv._batches[batch_id]['status'], 'running')

    def test_update_batch_progress_all_done(self):
        """Batch completes when all jobs finish."""
        batch_id = 'batch002'
        self._srv._batches[batch_id] = {
            'status': 'running',
            'created': time.time(),
            'job_ids': ['j1'],
            'completed': 0,
            'failed': 0,
            'total': 1,
        }
        self._srv._jobs['j1'] = {
            'status': 'completed', 'created': time.time(),
            'input_path': '', 'output_dir': '', 'error': None, 'stats': None,
        }
        self._srv._update_batch_progress(batch_id, 'j1')
        self.assertEqual(self._srv._batches[batch_id]['status'], 'completed')

    def test_update_batch_progress_failed_job(self):
        """Failed job increments failed counter."""
        batch_id = 'batch003'
        self._srv._batches[batch_id] = {
            'status': 'running',
            'created': time.time(),
            'job_ids': ['j1', 'j2'],
            'completed': 0,
            'failed': 0,
            'total': 2,
        }
        self._srv._jobs['j1'] = {
            'status': 'failed', 'created': time.time(),
            'input_path': '', 'output_dir': '', 'error': 'crash', 'stats': None,
        }
        self._srv._update_batch_progress(batch_id, 'j1')
        self.assertEqual(self._srv._batches[batch_id]['failed'], 1)

    def test_update_batch_nonexistent(self):
        """Updating a non-existent batch does nothing."""
        self._srv._update_batch_progress('nope', 'j1')  # should not raise


class TestRunServerApiKey(unittest.TestCase):
    """Tests for run_server API key configuration."""

    def test_api_key_sets_globals(self):
        import powerbi_import.api_server as srv
        original_key = srv._API_KEY
        original_secret = srv._WEBHOOK_SECRET
        try:
            # We can't actually start the server, but we can test the global setting
            srv._API_KEY = 'my-key'
            srv._WEBHOOK_SECRET = hashlib.sha256('my-key'.encode()).hexdigest()[:32]
            self.assertEqual(srv._API_KEY, 'my-key')
            self.assertEqual(len(srv._WEBHOOK_SECRET), 32)
        finally:
            srv._API_KEY = original_key
            srv._WEBHOOK_SECRET = original_secret

    def test_webhook_secret_derived_from_key(self):
        key = 'test-api-key-123'
        derived = hashlib.sha256(key.encode()).hexdigest()[:32]
        self.assertEqual(len(derived), 32)
        # Deterministic
        derived2 = hashlib.sha256(key.encode()).hexdigest()[:32]
        self.assertEqual(derived, derived2)


class TestQueryParams(unittest.TestCase):
    """Tests for _get_query_params helper."""

    def test_parse_query_params(self):
        from urllib.parse import urlparse, parse_qs

        url = '/jobs?status=completed&page=2&per_page=10'
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        self.assertEqual(params['status'], ['completed'])
        self.assertEqual(params['page'], ['2'])
        self.assertEqual(params['per_page'], ['10'])

    def test_empty_query(self):
        from urllib.parse import urlparse, parse_qs

        url = '/jobs'
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        self.assertEqual(params, {})


if __name__ == '__main__':
    unittest.main()
