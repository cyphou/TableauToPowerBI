"""
Tests for Subscription & Alert Migration (powerbi_import.subscription_generator).
"""

import json
import os
import sys
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.subscription_generator import (
    extract_all_subscriptions,
    extract_data_alerts,
    generate_pbi_subscriptions,
    generate_power_automate_flows,
    detect_schedule_conflicts,
    generate_subscription_report,
    save_subscriptions,
)


# ── Mock client ─────────────────────────────────────────────────────

class _MockClient:
    """Mock that mimics the real server_client REST interface."""
    site_url = 'https://tableau.example.com/api/3.20/sites/site-id'

    def _paginated_get(self, url, wrapper_key, item_key):
        if 'subscriptions' in url:
            return [
                {
                    'id': 'sub1',
                    'subject': 'Daily Sales Report',
                    'schedule': {'name': 'DailyAM', 'frequency': 'Daily',
                                 'frequencyDetails': {'intervals': {'interval': [
                                     {'hours': '8', 'minutes': '00'}
                                 ]}}},
                    'content': {'type': 'Workbook', 'id': 'wb1'},
                    'user': {'id': 'u1', 'name': 'alice@co.com'},
                },
                {
                    'id': 'sub2',
                    'subject': 'Weekly Finance',
                    'schedule': {'name': 'WeeklyMon', 'frequency': 'Weekly',
                                 'frequencyDetails': {'intervals': {'interval': [
                                     {'hours': '9', 'minutes': '00',
                                      'weekDay': 'Monday'}
                                 ]}}},
                    'content': {'type': 'View', 'id': 'v1'},
                    'user': {'id': 'u2', 'name': 'bob@co.com'},
                },
            ]
        return []

    def _request(self, method, url):
        if 'dataAlerts' in url:
            return {
                'dataAlerts': {
                    'dataAlert': [
                        {
                            'id': 'alert1',
                            'subject': 'Revenue Alert',
                            'condition': 'above',
                            'threshold': '100000',
                            'frequency': 'once',
                            'owner': {'name': 'alice@co.com'},
                            'view': {'id': 'v1', 'name': 'Revenue View',
                                     'workbook': {'id': 'wb1'}},
                            'recipients': {'recipient': []},
                        },
                    ],
                },
            }
        return {}


# ── Normalized subscription fixtures (matches extract output) ───────

def _normalized_subs():
    """Return subscriptions as extract_all_subscriptions would produce."""
    return [
        {
            'id': 'sub1', 'subject': 'Daily Sales Report',
            'content_type': 'Workbook', 'content_id': 'wb1',
            'content_name': 'wb1',
            'recipient_email': 'alice@co.com', 'recipient_id': 'u1',
            'schedule_name': 'DailyAM', 'frequency': 'Daily',
            'run_times': ['8:00'], 'run_days': [],
            'send_if_no_data': False, 'attach_pdf': False,
            'attach_csv': False, 'message': '',
        },
        {
            'id': 'sub2', 'subject': 'Weekly Finance',
            'content_type': 'View', 'content_id': 'v1',
            'content_name': 'v1',
            'recipient_email': 'bob@co.com', 'recipient_id': 'u2',
            'schedule_name': 'WeeklyMon', 'frequency': 'Weekly',
            'run_times': ['9:00'], 'run_days': ['Monday'],
            'send_if_no_data': False, 'attach_pdf': False,
            'attach_csv': False, 'message': '',
        },
    ]


class TestExtractAllSubscriptions(unittest.TestCase):

    def test_extracts_from_mock(self):
        client = _MockClient()
        subs = extract_all_subscriptions(client)
        self.assertEqual(len(subs), 2)
        self.assertEqual(subs[0]['subject'], 'Daily Sales Report')
        self.assertEqual(subs[0]['frequency'], 'Daily')

    def test_with_topology(self):
        client = _MockClient()
        topology = {'workbooks': [{'id': 'wb1', 'name': 'Sales Dashboard'}]}
        subs = extract_all_subscriptions(client, topology)
        self.assertEqual(subs[0]['content_name'], 'Sales Dashboard')


class TestExtractDataAlerts(unittest.TestCase):

    def test_extracts_alerts(self):
        client = _MockClient()
        alerts = extract_data_alerts(client)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]['subject'], 'Revenue Alert')
        self.assertEqual(alerts[0]['condition'], 'above')


class TestGeneratePbiSubscriptions(unittest.TestCase):

    def test_converts_subscriptions(self):
        pbi_subs = generate_pbi_subscriptions(_normalized_subs())
        self.assertEqual(len(pbi_subs), 2)
        for sub in pbi_subs:
            self.assertIn('displayName', sub)
            self.assertIn('frequency', sub)
            self.assertIn('recipientEmail', sub)

    def test_weekly_has_days(self):
        pbi_subs = generate_pbi_subscriptions(_normalized_subs())
        weekly = [s for s in pbi_subs if s['frequency'] == 'Weekly']
        self.assertEqual(len(weekly), 1)
        self.assertIn('Monday', weekly[0].get('days', []))

    def test_empty_subscriptions(self):
        pbi_subs = generate_pbi_subscriptions([])
        self.assertEqual(pbi_subs, [])


class TestGeneratePowerAutomateFlows(unittest.TestCase):

    def test_generates_alert_flows(self):
        alerts = [
            {'id': 'a1', 'subject': 'Alert', 'condition': 'above',
             'threshold': '100', 'view_name': 'V1', 'recipients': ['user@co.com']},
        ]
        flows = generate_power_automate_flows([], alerts)
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]['type'], 'automated')

    def test_generates_digest_flow(self):
        subs = [
            {'id': 's1', 'recipient_email': 'alice@co.com', 'content_name': 'R1'},
            {'id': 's2', 'recipient_email': 'alice@co.com', 'content_name': 'R2'},
        ]
        flows = generate_power_automate_flows(subs, [])
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0]['type'], 'scheduled')

    def test_empty_inputs(self):
        flows = generate_power_automate_flows([], [])
        self.assertEqual(flows, [])


class TestDetectScheduleConflicts(unittest.TestCase):

    def test_no_conflicts(self):
        subs = [
            {'frequency': 'Daily', 'startTime': '08:00:00', 'reportId': 'r1'},
            {'frequency': 'Weekly', 'startTime': '09:00:00', 'reportId': 'r2'},
        ]
        conflicts = detect_schedule_conflicts(subs)
        self.assertIsInstance(conflicts, list)
        self.assertEqual(len(conflicts), 0)

    def test_daily_limit_exceeded(self):
        subs = [
            {'frequency': 'Daily', 'startTime': f'{h:02d}:00:00', 'reportId': 'r1'}
            for h in range(10)
        ]
        conflicts = detect_schedule_conflicts(subs, license_type='Pro')
        limit_errors = [c for c in conflicts if c['type'] == 'daily_limit_exceeded']
        self.assertGreaterEqual(len(limit_errors), 1)


class TestGenerateSubscriptionReport(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_generates_html(self):
        subs = _normalized_subs()
        alerts = [{'id': 'a1', 'subject': 'Alert', 'condition': 'above',
                   'threshold': '100', 'view_name': 'V1',
                   'recipients': [], 'owner_email': 'a@co.com'}]
        pbi_subs = generate_pbi_subscriptions(subs)
        flows = generate_power_automate_flows(subs, alerts)
        conflicts = detect_schedule_conflicts(pbi_subs)
        out = os.path.join(self.tmpdir, 'report.html')
        result = generate_subscription_report(subs, alerts, pbi_subs, flows,
                                              conflicts, out)
        self.assertTrue(os.path.isfile(result))
        with open(result, encoding='utf-8') as f:
            html = f.read()
        self.assertIn('html', html.lower())


class TestSaveSubscriptions(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_saves_files(self):
        pbi_subs = [{'displayName': 'Test', 'frequency': 'Daily'}]
        flows = [{'name': 'TestFlow'}]
        result = save_subscriptions(pbi_subs, flows, self.tmpdir)
        self.assertTrue(os.path.isfile(result['subscriptions_path']))
        self.assertTrue(os.path.isfile(result['flows_path']))


if __name__ == '__main__':
    unittest.main()
