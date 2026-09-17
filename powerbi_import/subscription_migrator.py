"""
Subscription Migrator — Sprint 163

Migrates Tableau Server subscriptions and email notifications
to Power BI Service alert rules and notification configurations.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Subscription Model
# ═══════════════════════════════════════════════════════════════════

class SubscriptionMapping:
    """Maps a Tableau subscription to a PBI alert/notification."""

    def __init__(self, tableau_sub, pbi_alert=None, status='mapped',
                 notes=None):
        self.tableau_sub = tableau_sub
        self.pbi_alert = pbi_alert
        self.status = status  # mapped, unmapped, partial
        self.notes = notes or []

    def to_dict(self):
        return {
            'tableau_subscription': self.tableau_sub,
            'pbi_alert': self.pbi_alert,
            'status': self.status,
            'notes': self.notes,
        }


# ═══════════════════════════════════════════════════════════════════
# Schedule Conversion Utilities
# ═══════════════════════════════════════════════════════════════════

_DAY_MAP = {
    'Sunday': 0, 'Monday': 1, 'Tuesday': 2, 'Wednesday': 3,
    'Thursday': 4, 'Friday': 5, 'Saturday': 6,
}

_FREQUENCY_MAP = {
    'Hourly': 'Hours',
    'Daily': 'Days',
    'Weekly': 'Weeks',
    'Monthly': 'Months',
}


def convert_schedule_to_pbi(tableau_schedule):
    """Convert a Tableau schedule definition to PBI refresh schedule format.

    Args:
        tableau_schedule: Dict with Tableau schedule metadata.
            Keys: frequency (Hourly/Daily/Weekly/Monthly),
                  interval (hours/minutes between runs),
                  startTime, endTime, weekDay, monthDay, timezone

    Returns:
        dict: PBI refresh schedule configuration.
    """
    freq = tableau_schedule.get('frequency', 'Daily')
    interval = tableau_schedule.get('interval', 1)
    start_time = tableau_schedule.get('startTime', '02:00:00')
    end_time = tableau_schedule.get('endTime', '23:00:00')
    week_days = tableau_schedule.get('weekDays', [])
    timezone = tableau_schedule.get('timezone', 'UTC')

    # Parse start/end hours
    start_hour = int(start_time.split(':')[0]) if start_time else 2
    end_hour = int(end_time.split(':')[0]) if end_time else 23

    pbi_schedule = {
        'enabled': True,
        'notifyOption': 'MailOnFailure',
        'localTimeZoneId': _convert_timezone(timezone),
    }

    if freq == 'Hourly':
        # Generate time slots every N hours between start and end
        times = []
        hour = start_hour
        while hour <= end_hour:
            times.append(f'{hour:02d}:00')
            hour += int(interval) if interval else 1
        pbi_schedule['times'] = times if times else ['02:00']
        pbi_schedule['days'] = ['Monday', 'Tuesday', 'Wednesday',
                                'Thursday', 'Friday', 'Saturday', 'Sunday']
    elif freq == 'Daily':
        pbi_schedule['times'] = [f'{start_hour:02d}:00']
        pbi_schedule['days'] = ['Monday', 'Tuesday', 'Wednesday',
                                'Thursday', 'Friday', 'Saturday', 'Sunday']
    elif freq == 'Weekly':
        pbi_schedule['times'] = [f'{start_hour:02d}:00']
        pbi_schedule['days'] = week_days if week_days else ['Monday']
    elif freq == 'Monthly':
        # PBI doesn't have monthly refresh — approximate with weekly
        pbi_schedule['times'] = [f'{start_hour:02d}:00']
        pbi_schedule['days'] = ['Monday']
        pbi_schedule['_note'] = (
            'Tableau monthly schedule approximated as weekly. '
            'Configure scheduled refresh manually for monthly cadence.'
        )
    else:
        pbi_schedule['times'] = ['02:00']
        pbi_schedule['days'] = ['Monday', 'Tuesday', 'Wednesday',
                                'Thursday', 'Friday']

    return pbi_schedule


def _convert_timezone(tableau_tz):
    """Convert Tableau timezone string to PBI/Windows timezone ID."""
    tz_map = {
        'US/Eastern': 'Eastern Standard Time',
        'US/Central': 'Central Standard Time',
        'US/Mountain': 'Mountain Standard Time',
        'US/Pacific': 'Pacific Standard Time',
        'UTC': 'UTC',
        'Europe/London': 'GMT Standard Time',
        'Europe/Paris': 'Romance Standard Time',
        'Europe/Berlin': 'W. Europe Standard Time',
        'Asia/Tokyo': 'Tokyo Standard Time',
        'Asia/Shanghai': 'China Standard Time',
        'Australia/Sydney': 'AUS Eastern Standard Time',
        'America/New_York': 'Eastern Standard Time',
        'America/Chicago': 'Central Standard Time',
        'America/Denver': 'Mountain Standard Time',
        'America/Los_Angeles': 'Pacific Standard Time',
    }
    return tz_map.get(tableau_tz, tableau_tz or 'UTC')


# ═══════════════════════════════════════════════════════════════════
# Subscription → Alert Conversion
# ═══════════════════════════════════════════════════════════════════

def convert_subscriptions(subscriptions, user_upn_map=None):
    """Convert Tableau subscriptions to PBI alert rules.

    Args:
        subscriptions: List of Tableau subscription dicts.
        user_upn_map: Optional {tableau_username: azure_ad_upn} mapping.

    Returns:
        list[SubscriptionMapping]: Mapping results.
    """
    upn_map = user_upn_map or {}
    mappings = []

    for sub in (subscriptions or []):
        sub_id = sub.get('id', '')
        subject = sub.get('subject', '')
        user_name = sub.get('user', {}).get('name', '')
        schedule = sub.get('schedule', {})
        content = sub.get('content', {})

        # Try to map user to Azure AD UPN
        target_upn = upn_map.get(user_name)
        notes = []

        if not target_upn:
            # Attempt email-based matching
            if '@' in user_name:
                target_upn = user_name
            else:
                notes.append(f'No Azure AD UPN found for Tableau user "{user_name}"')

        pbi_alert = {
            'title': subject or f'Subscription: {content.get("name", sub_id)}',
            'alertType': 'DataRefreshFailure',
            'recipients': [target_upn] if target_upn else [],
            'enabled': True,
        }

        # If this is a data-driven subscription (condition-based)
        condition = sub.get('condition', sub.get('message', ''))
        if condition:
            pbi_alert['alertType'] = 'DataDriven'
            pbi_alert['condition'] = condition
            notes.append('Condition-based subscription → PBI data-driven alert')

        status = 'mapped' if target_upn else 'unmapped'
        mappings.append(SubscriptionMapping(
            tableau_sub=sub,
            pbi_alert=pbi_alert,
            status=status,
            notes=notes,
        ))

    return mappings


# ═══════════════════════════════════════════════════════════════════
# Conflict Detection
# ═══════════════════════════════════════════════════════════════════

def detect_schedule_conflicts(schedules, max_concurrent=8):
    """Detect refresh schedule conflicts for batch migration.

    Args:
        schedules: List of PBI refresh schedule dicts.
        max_concurrent: Maximum concurrent refreshes allowed.

    Returns:
        list[dict]: Conflict entries with time slot and overloaded count.
    """
    # Build time slot histogram
    slot_counts = {}  # {(day, hour): count}
    for sched in (schedules or []):
        days = sched.get('days', [])
        times = sched.get('times', [])
        for day in days:
            for time_str in times:
                hour = int(time_str.split(':')[0]) if ':' in time_str else 0
                key = (day, hour)
                slot_counts[key] = slot_counts.get(key, 0) + 1

    conflicts = []
    for (day, hour), count in sorted(slot_counts.items()):
        if count > max_concurrent:
            conflicts.append({
                'day': day,
                'hour': f'{hour:02d}:00',
                'concurrent_refreshes': count,
                'max_allowed': max_concurrent,
                'recommendation': (
                    f'Spread {count - max_concurrent} refresh(es) to adjacent time slots'
                ),
            })

    return conflicts


# ═══════════════════════════════════════════════════════════════════
# HTML Report
# ═══════════════════════════════════════════════════════════════════

def generate_subscription_report(mappings, conflicts=None):
    """Generate HTML report for subscription migration.

    Args:
        mappings: List of SubscriptionMapping.
        conflicts: Optional list of schedule conflicts.

    Returns:
        str: HTML report content.
    """
    mapped = sum(1 for m in mappings if m.status == 'mapped')
    unmapped = sum(1 for m in mappings if m.status == 'unmapped')
    total = len(mappings)

    html = [
        '<!DOCTYPE html><html><head>',
        '<title>Subscription Migration Report</title>',
        '<style>',
        'body{font-family:Segoe UI,sans-serif;margin:2rem;background:#f5f5f5}',
        'h1{color:#2b579a} h2{color:#444;margin-top:2rem}',
        'table{border-collapse:collapse;width:100%;margin:1rem 0}',
        'th,td{border:1px solid #ddd;padding:8px;text-align:left}',
        'th{background:#2b579a;color:white}',
        'tr:nth-child(even){background:#f9f9f9}',
        '.badge-green{background:#4caf50;color:white;padding:2px 8px;border-radius:4px}',
        '.badge-red{background:#f44336;color:white;padding:2px 8px;border-radius:4px}',
        '.badge-yellow{background:#ff9800;color:white;padding:2px 8px;border-radius:4px}',
        '.stat{display:inline-block;padding:1rem 2rem;margin:0.5rem;background:white;'
        'border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,.1)}',
        '.stat-value{font-size:2rem;font-weight:bold;color:#2b579a}',
        '</style></head><body>',
        '<h1>Subscription &amp; Schedule Migration Report</h1>',
        '<div>',
        f'<div class="stat"><div class="stat-value">{total}</div>Total</div>',
        f'<div class="stat"><div class="stat-value">{mapped}</div>Mapped</div>',
        f'<div class="stat"><div class="stat-value">{unmapped}</div>Unmapped</div>',
        '</div>',
    ]

    # Mappings table
    html.append('<h2>Subscription Mappings</h2>')
    html.append('<table><tr><th>#</th><th>Subject</th><th>User</th>'
                '<th>Status</th><th>Alert Type</th><th>Notes</th></tr>')
    for i, m in enumerate(mappings, 1):
        sub = m.tableau_sub
        subject = sub.get('subject', 'N/A')
        user = sub.get('user', {}).get('name', 'N/A')
        badge_class = 'badge-green' if m.status == 'mapped' else 'badge-red'
        alert_type = (m.pbi_alert or {}).get('alertType', 'N/A')
        notes_str = '; '.join(m.notes) if m.notes else '—'
        html.append(
            f'<tr><td>{i}</td><td>{subject}</td><td>{user}</td>'
            f'<td><span class="{badge_class}">{m.status}</span></td>'
            f'<td>{alert_type}</td><td>{notes_str}</td></tr>'
        )
    html.append('</table>')

    # Conflicts table
    if conflicts:
        html.append('<h2>Schedule Conflicts</h2>')
        html.append('<table><tr><th>Day</th><th>Time</th>'
                    '<th>Concurrent</th><th>Max</th><th>Recommendation</th></tr>')
        for c in conflicts:
            html.append(
                f'<tr><td>{c["day"]}</td><td>{c["hour"]}</td>'
                f'<td><span class="badge-yellow">{c["concurrent_refreshes"]}</span></td>'
                f'<td>{c["max_allowed"]}</td><td>{c["recommendation"]}</td></tr>'
            )
        html.append('</table>')

    html.append('</body></html>')
    return '\n'.join(html)
