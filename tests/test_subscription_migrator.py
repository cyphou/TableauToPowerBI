"""Tests for Sprint 163 - subscription_migrator module."""

import unittest
from powerbi_import.subscription_migrator import (
    convert_schedule_to_pbi,
    convert_subscriptions,
    detect_schedule_conflicts,
    generate_subscription_report,
)


class TestConvertScheduleToPbi(unittest.TestCase):
    """Test Tableau schedule -> PBI refresh schedule conversion."""

    def test_daily_schedule(self):
        """Daily schedule converts correctly."""
        schedule = {
            'frequency': 'Daily',
            'startTime': '06:00:00',
            'timezone': 'America/New_York',
        }
        result = convert_schedule_to_pbi(schedule)
        self.assertTrue(result['enabled'])
        self.assertIn('06:00', result['times'][0])
        self.assertEqual(result['localTimeZoneId'], 'Eastern Standard Time')

    def test_hourly_schedule(self):
        """Hourly schedule maps to multiple daily times."""
        schedule = {
            'frequency': 'Hourly',
            'interval': 2,
            'startTime': '06:00:00',
            'endTime': '20:00:00',
        }
        result = convert_schedule_to_pbi(schedule)
        self.assertGreater(len(result['times']), 1)

    def test_weekly_schedule(self):
        """Weekly schedule with day mapping."""
        schedule = {
            'frequency': 'Weekly',
            'weekDays': ['Monday', 'Wednesday'],
            'startTime': '08:00:00',
        }
        result = convert_schedule_to_pbi(schedule)
        self.assertIn('Monday', result.get('days', []))

    def test_monthly_schedule(self):
        """Monthly schedule converted to weekly approximation."""
        schedule = {
            'frequency': 'Monthly',
            'startTime': '05:00:00',
        }
        result = convert_schedule_to_pbi(schedule)
        self.assertIn('_note', result)  # approximation note

    def test_unknown_frequency(self):
        """Unknown frequency still returns valid schedule."""
        schedule = {'frequency': 'Continuous', 'startTime': '12:00:00'}
        result = convert_schedule_to_pbi(schedule)
        self.assertTrue(result['enabled'])
        self.assertIn('times', result)


class TestConvertSubscriptions(unittest.TestCase):
    """Test subscription migration with user mappings."""

    def test_basic_subscription_conversion(self):
        """Basic subscription maps to PBI alert config."""
        subscriptions = [{
            'id': 'sub-1',
            'subject': 'Sales Report',
            'user': {'name': 'jsmith'},
            'schedule': {'frequency': 'Daily', 'startTime': '08:00:00'},
        }]
        user_mapping = {'jsmith': 'john.smith@company.com'}
        result = convert_subscriptions(subscriptions, user_mapping)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].pbi_alert['recipients'][0],
                         'john.smith@company.com')
        self.assertEqual(result[0].status, 'mapped')

    def test_unmapped_user(self):
        """Subscriptions for unmapped users are flagged."""
        subscriptions = [{
            'id': 'sub-1',
            'subject': 'Report',
            'user': {'name': 'unknown_user'},
            'schedule': {'frequency': 'Daily', 'startTime': '08:00:00'},
        }]
        result = convert_subscriptions(subscriptions, {})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].status, 'unmapped')

    def test_empty_subscriptions(self):
        """Empty input returns empty output."""
        result = convert_subscriptions([], {})
        self.assertEqual(result, [])


class TestDetectScheduleConflicts(unittest.TestCase):
    """Test schedule conflict detection."""

    def test_no_conflict(self):
        """Non-overlapping schedules show no conflict."""
        schedules = [
            {'days': ['Monday'], 'times': ['06:00']},
            {'days': ['Monday'], 'times': ['18:00']},
        ]
        result = detect_schedule_conflicts(schedules, max_concurrent=8)
        self.assertEqual(len(result), 0)

    def test_conflict_detected(self):
        """Many schedules at same time detected as conflict."""
        schedules = [
            {'days': ['Monday'], 'times': ['06:00']}
            for _ in range(10)
        ]
        result = detect_schedule_conflicts(schedules, max_concurrent=8)
        self.assertGreater(len(result), 0)
        self.assertEqual(result[0]['day'], 'Monday')


class TestGenerateSubscriptionReport(unittest.TestCase):
    """Test HTML report generation."""

    def test_report_contains_html(self):
        """Report output contains HTML structure."""
        from powerbi_import.subscription_migrator import SubscriptionMapping
        mappings = [
            SubscriptionMapping(
                tableau_sub={'id': 'sub-1', 'subject': 'Test',
                             'user': {'name': 'u1'}},
                pbi_alert={'alertType': 'DataRefreshFailure',
                           'recipients': ['u@test.com']},
                status='mapped',
            )
        ]
        result = generate_subscription_report(mappings)
        self.assertIn('<html', result)
        self.assertIn('Test', result)

    def test_empty_report(self):
        """Empty subscriptions produce valid HTML."""
        result = generate_subscription_report([])
        self.assertIn('<html', result)


if __name__ == '__main__':
    unittest.main()
