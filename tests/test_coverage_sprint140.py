"""Sprint 140 — Coverage uplift for user-facing entry points.

Targets the biggest coverage gaps reported by the Sprint 137 audit:

  - ``api_server.py``     58.8 % → bumped via additional endpoint + helper tests
  - ``monitoring.py``     74.1 % → all 4 backends + record_migration
  - ``notebook_api.py``   71.0 % → MigrationSession lifecycle / overrides

These are pure unit tests — no live HTTP server, no real workbook
extraction. Behaviour is exercised through the public API; failures
assert observable contracts (return values, file output, exceptions).
"""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock


# ════════════════════════════════════════════════════════════════════
#  api_server — additional helper / endpoint coverage
# ════════════════════════════════════════════════════════════════════

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'powerbi_import'))


from powerbi_import import api_server as api  # noqa: E402


class TestApiHelpersCoverage(unittest.TestCase):
    """Cover the small helper functions exercised by edge-case requests."""

    def test_int_param_negative(self):
        self.assertEqual(api._int_param({'k': ['-7']}, 'k'), -7)

    def test_int_param_empty_list(self):
        self.assertIsNone(api._int_param({'k': [None]}, 'k'))

    def test_int_param_whitespace(self):
        self.assertIsNone(api._int_param({'k': ['  ']}, 'k'))

    def test_get_version_returns_string(self):
        v = api._get_version()
        self.assertIsInstance(v, str)
        self.assertGreater(len(v), 0)


class TestRateLimit(unittest.TestCase):
    """Sliding-window per-IP rate limiter."""

    def setUp(self):
        # Reset tracker so tests are deterministic
        with api._rate_lock:
            api._rate_tracker.clear()

    def test_first_request_allowed(self):
        self.assertTrue(api._check_rate_limit('1.2.3.4'))

    def test_under_limit_allowed(self):
        ip = '5.6.7.8'
        for _ in range(api._RATE_LIMIT_MAX):
            self.assertTrue(api._check_rate_limit(ip))

    def test_over_limit_blocked(self):
        ip = '9.10.11.12'
        for _ in range(api._RATE_LIMIT_MAX):
            api._check_rate_limit(ip)
        self.assertFalse(api._check_rate_limit(ip))

    def test_isolated_ips(self):
        ip_a, ip_b = '1.1.1.1', '2.2.2.2'
        for _ in range(api._RATE_LIMIT_MAX):
            api._check_rate_limit(ip_a)
        # ip_a is over limit, ip_b should still be fresh
        self.assertFalse(api._check_rate_limit(ip_a))
        self.assertTrue(api._check_rate_limit(ip_b))


class TestPurgeStaleJobs(unittest.TestCase):
    """``_purge_stale_jobs`` removes completed/failed jobs older than TTL."""

    def setUp(self):
        with api._lock:
            api._jobs.clear()

    def test_completed_old_purged(self):
        with api._lock:
            api._jobs['old'] = {
                'status': 'completed',
                'created': 0,  # epoch — definitely older than TTL
                'input_path': '', 'output_dir': None,
                'error': None, 'stats': None,
            }
            api._purge_stale_jobs()
            self.assertNotIn('old', api._jobs)

    def test_running_not_purged(self):
        with api._lock:
            api._jobs['busy'] = {
                'status': 'running',
                'created': 0,
                'input_path': '', 'output_dir': None,
                'error': None, 'stats': None,
            }
            api._purge_stale_jobs()
            self.assertIn('busy', api._jobs)


class TestRunMigrationFailure(unittest.TestCase):
    """``_run_migration`` should record ``failed`` status on errors."""

    def setUp(self):
        with api._lock:
            api._jobs.clear()

    def test_invalid_input_marks_failed(self):
        jid = api._new_job('/nonexistent/path.twbx')
        api._run_migration(jid, '/nonexistent/path.twbx')
        job = api._get_job(jid)
        self.assertEqual(job['status'], 'failed')
        self.assertIsNotNone(job['error'])


class TestParseMultipartEdgeCases(unittest.TestCase):
    def test_dangerous_chars_replaced(self):
        # Filename with NUL byte and shell special chars
        body = (
            b'------b\r\n'
            b'Content-Disposition: form-data; name="file"; '
            b'filename="evil; rm -rf /.twbx"\r\n'
            b'\r\n'
            b'data\r\n'
            b'------b--\r\n'
        )
        result = api._parse_multipart(body, b'----b')
        self.assertIsNotNone(result)
        filename, _ = result
        # Must NOT contain shell metacharacters in raw form
        self.assertNotIn(';', filename)


# ════════════════════════════════════════════════════════════════════
#  monitoring — all 4 backends + MigrationMonitor + record_migration
# ════════════════════════════════════════════════════════════════════

from powerbi_import import monitoring  # noqa: E402


class TestNoneBackend(unittest.TestCase):
    def test_record_metric_noop(self):
        b = monitoring._NoneBackend()
        # Should not raise and not return anything meaningful
        self.assertIsNone(b.record_metric('x', 1))
        self.assertIsNone(b.record_event('e'))
        self.assertIsNone(b.flush())


class TestJsonBackendFlushFile(unittest.TestCase):
    def test_flush_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'out.jsonl')
            b = monitoring._JsonBackend(log_path=path)
            b.record_metric('m1', 1.5, dimensions={'tag': 'a'})
            b.record_event('e1', properties={'p': 1})
            written = b.flush()
            self.assertEqual(written, 2)
            self.assertTrue(os.path.isfile(path))
            with open(path) as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 2)
            entries = [json.loads(line) for line in lines]
            self.assertEqual(entries[0]['type'], 'metric')
            self.assertEqual(entries[1]['type'], 'event')

    def test_flush_empty_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'out.jsonl')
            b = monitoring._JsonBackend(log_path=path)
            self.assertIsNone(b.flush())


class TestAzureBackendFallback(unittest.TestCase):
    """Azure backend without SDK installed should not crash and should
    fall back to the JSON backend."""

    def test_record_uses_fallback(self):
        b = monitoring._AzureMonitorBackend(connection_string='')
        b.record_metric('m', 42)
        b.record_event('e', {'k': 'v'})
        # Fallback buffer should contain both entries
        self.assertEqual(len(b._fallback._buffer), 2)


class TestPrometheusBackendFallback(unittest.TestCase):
    def test_record_uses_fallback(self):
        b = monitoring._PrometheusBackend(gateway_url='')
        b.record_metric('m', 1)
        b.record_event('e')
        self.assertEqual(len(b._fallback._buffer), 2)

    def test_flush_no_gateway_returns_count(self):
        b = monitoring._PrometheusBackend(gateway_url='')
        b.record_metric('m', 1)
        # No gateway set → no push, but fallback flush still runs
        # (will write to artifacts/monitoring.jsonl by default)
        with tempfile.TemporaryDirectory() as tmp:
            b._fallback.log_path = os.path.join(tmp, 'p.jsonl')
            count = b.flush()
            # JsonBackend.flush returns count of entries written
            self.assertEqual(count, 1)


class TestMigrationMonitorRecordMigration(unittest.TestCase):
    """The high-level helper records 6 metrics + 1 event in one shot."""

    def test_record_migration_writes_seven_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, 'log.jsonl')
            m = monitoring.MigrationMonitor(backend='json', log_path=log)
            m.record_migration(
                workbook='WB',
                duration_seconds=12.0,
                fidelity=88.5,
                tables=3, measures=12, visuals=8, pages=2,
            )
            written = m.flush()
            self.assertEqual(written, 7)  # 6 metrics + 1 event
            with open(log) as f:
                entries = [json.loads(line) for line in f]
            metric_names = {e['name'] for e in entries if e['type'] == 'metric'}
            self.assertIn('migration.duration_seconds', metric_names)
            self.assertIn('migration.fidelity_percent', metric_names)
            self.assertIn('migration.tables', metric_names)
            self.assertIn('migration.measures', metric_names)
            self.assertIn('migration.visuals', metric_names)
            self.assertIn('migration.pages', metric_names)
            event_names = {e['name'] for e in entries if e['type'] == 'event'}
            self.assertEqual(event_names, {'migration.complete'})

    def test_unknown_backend_falls_back_to_json(self):
        m = monitoring.MigrationMonitor(backend='nonexistent_xyz')
        # Should pick the default JSON backend without raising
        self.assertIsInstance(m._backend, monitoring._JsonBackend)


class TestSanitizeMetricName(unittest.TestCase):
    def test_alphanumeric_unchanged(self):
        self.assertEqual(monitoring._sanitize_metric_name('hello_world'), 'hello_world')

    def test_special_chars_replaced(self):
        self.assertEqual(monitoring._sanitize_metric_name('hello.world-bar'),
                          'hello_world_bar')

    def test_leading_digit_prefixed(self):
        self.assertEqual(monitoring._sanitize_metric_name('1abc'), '_1abc')

    def test_empty_returns_unknown(self):
        self.assertEqual(monitoring._sanitize_metric_name(''), 'unknown')


class TestEscapeLabelValue(unittest.TestCase):
    def test_quote_escaped(self):
        self.assertEqual(monitoring._escape_label_value('a"b'), 'a\\"b')

    def test_backslash_escaped(self):
        self.assertEqual(monitoring._escape_label_value('a\\b'), 'a\\\\b')

    def test_newline_escaped(self):
        self.assertEqual(monitoring._escape_label_value('a\nb'), 'a\\nb')


class TestTelemetryToOpenmetrics(unittest.TestCase):
    def test_none_collector_returns_string(self):
        out = monitoring.telemetry_to_openmetrics(None)
        self.assertIsInstance(out, str)

    def test_decisions_rendered(self):
        class FakeCollector:
            def get_data(self):
                return {
                    'decisions': {
                        'visual_choice': {
                            'lineChart': {'count': 5},
                            'barChart': {'count': 3},
                        }
                    }
                }
        out = monitoring.telemetry_to_openmetrics(FakeCollector())
        self.assertIn('ttpbi_decisions_total', out)
        self.assertIn('lineChart', out)
        self.assertIn('5', out)


# ════════════════════════════════════════════════════════════════════
#  notebook_api — MigrationSession lifecycle without real workbook
# ════════════════════════════════════════════════════════════════════

from powerbi_import import notebook_api  # noqa: E402


class TestMigrationSessionConfig(unittest.TestCase):
    def test_default_config(self):
        s = notebook_api.MigrationSession()
        cfg = s.get_config()
        self.assertEqual(cfg['calendar_start'], 2020)
        self.assertEqual(cfg['mode'], 'import')

    def test_configure_known_option(self):
        s = notebook_api.MigrationSession()
        new_cfg = s.configure(calendar_start=2018, culture='fr-FR')
        self.assertEqual(new_cfg['calendar_start'], 2018)
        self.assertEqual(new_cfg['culture'], 'fr-FR')

    def test_configure_unknown_option_warns(self):
        s = notebook_api.MigrationSession()
        # Unknown options are logged but ignored — no exception
        cfg = s.configure(this_is_not_a_real_option=42)
        self.assertNotIn('this_is_not_a_real_option', cfg)


class TestMigrationSessionOverrides(unittest.TestCase):
    def test_dax_override_lifecycle(self):
        s = notebook_api.MigrationSession()
        s.edit_dax('Total Sales', 'SUM(Sales[Amount])')
        self.assertEqual(s.get_dax_overrides(),
                          {'Total Sales': 'SUM(Sales[Amount])'})
        s.clear_dax_override('Total Sales')
        self.assertEqual(s.get_dax_overrides(), {})

    def test_clear_unknown_dax_override_silent(self):
        s = notebook_api.MigrationSession()
        s.clear_dax_override('NonExistent')  # should not raise
        self.assertEqual(s.get_dax_overrides(), {})

    def test_visual_override(self):
        s = notebook_api.MigrationSession()
        s.override_visual_type('SalesBars', 'lineChart')
        self.assertEqual(s._visual_overrides['SalesBars'], 'lineChart')


class TestMigrationSessionGuards(unittest.TestCase):
    """Methods requiring loaded data should raise without it."""

    def test_assess_without_load_raises(self):
        s = notebook_api.MigrationSession()
        with self.assertRaises(Exception):
            s.assess()

    def test_validate_without_generate_raises(self):
        s = notebook_api.MigrationSession()
        with self.assertRaises(RuntimeError):
            s.validate()

    def test_deploy_without_generate_raises(self):
        s = notebook_api.MigrationSession()
        with self.assertRaises(RuntimeError):
            s.deploy(workspace_id='abc')


class TestMigrationSessionWithFakeExtract(unittest.TestCase):
    """Inject a fake ``_extracted`` dict to exercise preview methods
    without going through full workbook extraction."""

    def _fake_session(self):
        s = notebook_api.MigrationSession()
        s._workbook_path = '/fake/wb.twbx'
        s._extracted = {
            'calculations': [
                {'name': 'Total', 'formula': 'SUM([Amount])'},
                {'name': 'Empty', 'formula': ''},  # skipped by preview
            ],
            'datasources': [{
                'name': 'ds1',
                'connection': {'class': 'sqlserver'},
                'tables': [{
                    'name': 'Sales',
                    'columns': [{'name': 'Amount'}],
                }],
            }],
            'worksheets': [{
                'name': 'Sheet1',
                'mark_type': 'bar',
                'fields': [{'name': 'Amount'}],
            }],
        }
        return s

    def test_preview_dax_returns_results(self):
        s = self._fake_session()
        results = s.preview_dax()
        # Empty-formula calc is skipped
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['name'], 'Total')

    def test_preview_dax_with_override_marks_overridden(self):
        s = self._fake_session()
        s.edit_dax('Total', 'SUM(Sales[Amount]) /* manual */')
        results = s.preview_dax()
        self.assertEqual(results[0]['status'], 'overridden')
        self.assertIn('manual', results[0]['dax_formula'])

    def test_list_approximated_subset(self):
        s = self._fake_session()
        items = s.list_approximated()
        self.assertIsInstance(items, list)
        # Every entry must have status approximated
        self.assertTrue(all(it['status'] == 'approximated' for it in items))

    def test_preview_visuals_returns_mapping(self):
        s = self._fake_session()
        results = s.preview_visuals()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['worksheet'], 'Sheet1')
        self.assertIn('pbi_visual_type', results[0])

    def test_preview_visuals_with_override(self):
        s = self._fake_session()
        s.override_visual_type('Sheet1', 'lineChart')
        results = s.preview_visuals()
        self.assertEqual(results[0]['pbi_visual_type'], 'lineChart')
        self.assertTrue(results[0]['overridden'])


if __name__ == '__main__':
    unittest.main()
