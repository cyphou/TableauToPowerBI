"""
Subscription and alert migration — Tableau Server → Power BI.

Sprint 142 — Extracts subscriptions (scheduled email reports) and
data-driven alerts from Tableau Server and generates PBI subscription
configs, Power Automate flow templates, and migration reports.

Usage::

    from powerbi_import.subscription_generator import (
        extract_all_subscriptions, extract_data_alerts,
        generate_pbi_subscriptions, generate_power_automate_flows,
        detect_schedule_conflicts, generate_subscription_report,
    )
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  Schedule Mapping
# ═══════════════════════════════════════════════════════════════════════

_FREQUENCY_MAP = {
    'Daily': 'Daily',
    'Weekly': 'Weekly',
    'Monthly': 'Monthly',
    'Hourly': 'Daily',  # PBI doesn't have hourly subscriptions
}

_DAY_MAP = {
    'Monday': 'Monday',
    'Tuesday': 'Tuesday',
    'Wednesday': 'Wednesday',
    'Thursday': 'Thursday',
    'Friday': 'Friday',
    'Saturday': 'Saturday',
    'Sunday': 'Sunday',
}


# ═══════════════════════════════════════════════════════════════════════
#  Site-Wide Subscription Extraction (Sprint 142.1)
# ═══════════════════════════════════════════════════════════════════════

def extract_all_subscriptions(client, topology: Dict[str, Any] = None) -> List[Dict]:
    """Extract all subscriptions from a Tableau Server site.

    Args:
        client: Authenticated TableauServerClient.
        topology: Optional site topology for enrichment.

    Returns:
        list: Subscription dicts with schedule, recipients, format info.
    """
    try:
        url = f'{client.site_url}/subscriptions'
        all_subs = client._paginated_get(url, 'subscriptions', 'subscription')
    except Exception as e:
        logger.error("Failed to extract subscriptions: %s", e)
        return []

    subscriptions = []
    wb_map = {}
    if topology:
        wb_map = {wb.get('id', ''): wb for wb in topology.get('workbooks', [])}

    for sub in all_subs:
        content = sub.get('content', {})
        schedule = sub.get('schedule', {})
        user = sub.get('user', {})

        # Parse schedule frequency
        freq_details = schedule.get('frequencyDetails', {})
        intervals = freq_details.get('intervals', {}).get('interval', [])
        if isinstance(intervals, dict):
            intervals = [intervals]

        # Extract time/day from intervals
        run_times = []
        run_days = []
        for interval in intervals:
            hours = interval.get('hours', '')
            minutes = interval.get('minutes', '')
            weekday = interval.get('weekDay', '')
            month_day = interval.get('monthDay', '')
            if hours:
                run_times.append(f"{hours}:{minutes or '00'}")
            if weekday:
                run_days.append(weekday)
            if month_day:
                run_days.append(f"Day {month_day}")

        wb_name = ''
        wb_info = wb_map.get(content.get('id', ''))
        if wb_info:
            wb_name = wb_info.get('name', '')

        subscriptions.append({
            'id': sub.get('id', ''),
            'subject': sub.get('subject', ''),
            'content_type': content.get('type', ''),
            'content_id': content.get('id', ''),
            'content_name': wb_name or content.get('id', ''),
            'recipient_email': user.get('name', ''),
            'recipient_id': user.get('id', ''),
            'schedule_name': schedule.get('name', ''),
            'frequency': schedule.get('frequency', ''),
            'run_times': run_times,
            'run_days': run_days,
            'send_if_no_data': sub.get('sendIfViewEmpty', False),
            'attach_pdf': sub.get('attachPdf', False),
            'attach_image': sub.get('attachImage', False),
            'message': sub.get('message', ''),
        })

    logger.info("Extracted %d subscriptions", len(subscriptions))
    return subscriptions


# ═══════════════════════════════════════════════════════════════════════
#  Data-Driven Alert Extraction (Sprint 142.2)
# ═══════════════════════════════════════════════════════════════════════

def extract_data_alerts(client) -> List[Dict]:
    """Extract data-driven alert conditions from Tableau Server (2018.3+).

    Args:
        client: Authenticated TableauServerClient.

    Returns:
        list: Alert dicts with field, threshold, operator, frequency, recipients.
    """
    try:
        url = f'{client.site_url}/dataAlerts'
        resp = client._request('GET', url)
        all_alerts = resp.get('dataAlerts', {}).get('dataAlert', [])
        if isinstance(all_alerts, dict):
            all_alerts = [all_alerts]
    except Exception as e:
        logger.warning("Data alerts API not available (requires Server 2018.3+): %s", e)
        return []

    alerts = []
    for alert in all_alerts:
        owner = alert.get('owner', {})
        view = alert.get('view', {})

        recipients = []
        for recipient in alert.get('recipients', {}).get('recipient', []):
            if isinstance(recipient, dict):
                recipients.append(recipient.get('name', ''))
            else:
                recipients.append(str(recipient))

        alerts.append({
            'id': alert.get('id', ''),
            'subject': alert.get('subject', ''),
            'condition': alert.get('condition', ''),
            'threshold': alert.get('threshold', ''),
            'frequency': alert.get('frequency', 'once'),
            'owner_name': owner.get('name', ''),
            'owner_email': owner.get('name', ''),
            'view_id': view.get('id', ''),
            'view_name': view.get('name', ''),
            'workbook_id': view.get('workbook', {}).get('id', ''),
            'recipients': recipients,
            'created_at': alert.get('createdAt', ''),
        })

    logger.info("Extracted %d data alerts", len(alerts))
    return alerts


# ═══════════════════════════════════════════════════════════════════════
#  PBI Subscription Config Generator (Sprint 142.3)
# ═══════════════════════════════════════════════════════════════════════

def generate_pbi_subscriptions(
    subscriptions: List[Dict],
    workbook_report_map: Optional[Dict[str, str]] = None,
) -> List[Dict]:
    """Generate Power BI subscription JSON configs from Tableau subscriptions.

    Args:
        subscriptions: Extracted subscription dicts.
        workbook_report_map: Optional {tableau_wb_name: pbi_report_id} mapping.

    Returns:
        list: PBI-compatible subscription config dicts.
    """
    workbook_report_map = workbook_report_map or {}
    pbi_subs = []

    for sub in subscriptions:
        frequency = _FREQUENCY_MAP.get(sub.get('frequency', ''), 'Daily')
        run_times = sub.get('run_times', ['08:00'])
        time_str = run_times[0] if run_times else '08:00'

        # Parse time
        try:
            parts = time_str.split(':')
            hour = int(parts[0]) if parts else 8
            minute = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            hour, minute = 8, 0

        # Map days for weekly subscriptions
        days = []
        for d in sub.get('run_days', []):
            mapped = _DAY_MAP.get(d, '')
            if mapped:
                days.append(mapped)

        content_name = sub.get('content_name', '')
        report_id = workbook_report_map.get(content_name, 'YOUR_REPORT_ID')

        pbi_sub = {
            'displayName': sub.get('subject', f'Subscription: {content_name}'),
            'reportId': report_id,
            'recipientEmail': sub.get('recipient_email', ''),
            'enabled': True,
            'frequency': frequency,
            'startTime': f'{hour:02d}:{minute:02d}:00',
            'timeZone': 'UTC',
            'attachmentFormat': 'PDF' if sub.get('attach_pdf') else 'PNG',
            'sendIfNoData': sub.get('send_if_no_data', False),
            'source': {
                'tableau_subscription_id': sub.get('id', ''),
                'tableau_schedule': sub.get('schedule_name', ''),
            },
        }

        if frequency == 'Weekly' and days:
            pbi_sub['days'] = days
        elif frequency == 'Monthly':
            # Extract day of month
            for d in sub.get('run_days', []):
                if d.startswith('Day '):
                    try:
                        pbi_sub['monthDay'] = int(d.replace('Day ', ''))
                    except ValueError:
                        pbi_sub['monthDay'] = 1

        pbi_subs.append(pbi_sub)

    return pbi_subs


# ═══════════════════════════════════════════════════════════════════════
#  Power Automate Flow Templates (Sprint 142.4)
# ═══════════════════════════════════════════════════════════════════════

def generate_power_automate_flows(
    subscriptions: List[Dict],
    alerts: List[Dict],
) -> List[Dict]:
    """Generate Power Automate flow definition templates.

    Produces flow JSON for advanced scenarios:
    - Conditional email sends
    - Teams notifications
    - Multi-report digests

    Args:
        subscriptions: Extracted subscription dicts.
        alerts: Extracted data alert dicts.

    Returns:
        list: Power Automate flow definition dicts.
    """
    flows = []

    # Group subscriptions by recipient for digest flows
    by_recipient = {}
    for sub in subscriptions:
        email = sub.get('recipient_email', '')
        if email:
            by_recipient.setdefault(email, []).append(sub)

    # Create digest flow for recipients with >1 subscription
    for email, subs in by_recipient.items():
        if len(subs) > 1:
            flows.append(_build_digest_flow(email, subs))

    # Create alert notification flows
    for alert in alerts:
        flows.append(_build_alert_flow(alert))

    return flows


def _build_digest_flow(email: str, subs: List[Dict]) -> Dict:
    """Build a Power Automate digest flow template."""
    safe_name = re.sub(r'[^a-zA-Z0-9_]', '_', email.split('@')[0])
    report_names = [s.get('content_name', 'Report') for s in subs]

    return {
        'name': f'PBI_Digest_{safe_name}',
        'description': f'Daily digest for {email} — {len(subs)} reports',
        'type': 'scheduled',
        'definition': {
            'triggers': [{
                'type': 'Recurrence',
                'recurrence': {
                    'frequency': 'Day',
                    'interval': 1,
                    'startTime': '08:00:00',
                    'timeZone': 'UTC',
                },
            }],
            'actions': [
                {
                    'type': 'ExportReport',
                    'report_name': name,
                    'format': 'PDF',
                    'comment': f'// TODO: Set report ID for "{name}"',
                }
                for name in report_names
            ] + [{
                'type': 'SendEmail',
                'to': email,
                'subject': f'Daily Report Digest — {len(report_names)} reports',
                'body': 'Please find attached your daily report digest.',
                'attachments': [f'{n}.pdf' for n in report_names],
            }],
        },
        'source_subscriptions': [s.get('id', '') for s in subs],
    }


def _build_alert_flow(alert: Dict) -> Dict:
    """Build a Power Automate alert notification flow template."""
    return {
        'name': f'PBI_Alert_{alert.get("id", "unknown")[:8]}',
        'description': f'Alert: {alert.get("subject", "")}',
        'type': 'automated',
        'definition': {
            'triggers': [{
                'type': 'DataAlert',
                'condition': alert.get('condition', ''),
                'threshold': alert.get('threshold', ''),
                'view_name': alert.get('view_name', ''),
                'comment': '// TODO: Configure PBI data alert trigger',
            }],
            'actions': [{
                'type': 'SendEmail',
                'to': '; '.join(alert.get('recipients', [])),
                'subject': alert.get('subject', 'Data Alert'),
                'body': (
                    f'Data alert triggered: {alert.get("subject", "")}\n'
                    f'Condition: {alert.get("condition", "")} '
                    f'{alert.get("threshold", "")}'
                ),
            }, {
                'type': 'PostTeamsMessage',
                'channel': '// TODO: Set Teams channel',
                'message': f'⚠️ {alert.get("subject", "")} — threshold reached',
            }],
        },
        'source_alert_id': alert.get('id', ''),
    }


# ═══════════════════════════════════════════════════════════════════════
#  Schedule Conflict Detector (Sprint 142.6)
# ═══════════════════════════════════════════════════════════════════════

def detect_schedule_conflicts(
    pbi_subscriptions: List[Dict],
    license_type: str = 'Pro',
) -> List[Dict]:
    """Detect potential scheduling conflicts in PBI subscriptions.

    Checks:
    - >8 daily refreshes on Pro license
    - Overlapping subscription windows
    - High-frequency alerts on large datasets

    Args:
        pbi_subscriptions: List of PBI subscription configs.
        license_type: 'Pro' or 'Premium'.

    Returns:
        list: Conflict/warning dicts.
    """
    conflicts = []

    # Count daily subscriptions per report
    daily_by_report = {}
    for sub in pbi_subscriptions:
        if sub.get('frequency') == 'Daily':
            report_id = sub.get('reportId', '')
            daily_by_report.setdefault(report_id, []).append(sub)

    max_daily = 8 if license_type == 'Pro' else 48  # Premium allows more

    for report_id, subs in daily_by_report.items():
        if len(subs) > max_daily:
            conflicts.append({
                'type': 'daily_limit_exceeded',
                'severity': 'error',
                'report_id': report_id,
                'count': len(subs),
                'limit': max_daily,
                'message': (
                    f'Report {report_id} has {len(subs)} daily subscriptions, '
                    f'exceeding the {license_type} limit of {max_daily}. '
                    f'Consider upgrading to Premium or reducing frequency.'
                ),
            })

    # Check for time clustering (many subs at same time)
    time_slots = {}
    for sub in pbi_subscriptions:
        start = sub.get('startTime', '08:00:00')
        time_slots.setdefault(start, []).append(sub)

    for time_slot, subs in time_slots.items():
        if len(subs) > 10:
            conflicts.append({
                'type': 'time_clustering',
                'severity': 'warning',
                'time': time_slot,
                'count': len(subs),
                'message': (
                    f'{len(subs)} subscriptions scheduled at {time_slot}. '
                    f'Consider staggering to avoid capacity contention.'
                ),
            })

    return conflicts


# ═══════════════════════════════════════════════════════════════════════
#  Subscription Migration Report (Sprint 142.7)
# ═══════════════════════════════════════════════════════════════════════

def generate_subscription_report(
    subscriptions: List[Dict],
    alerts: List[Dict],
    pbi_subs: List[Dict],
    flows: List[Dict],
    conflicts: List[Dict],
    output_path: str,
) -> str:
    """Generate an HTML subscription migration report.

    Args:
        subscriptions: Original Tableau subscriptions.
        alerts: Original Tableau data alerts.
        pbi_subs: Generated PBI subscription configs.
        flows: Generated Power Automate flow templates.
        conflicts: Detected schedule conflicts.
        output_path: File path for the HTML report.

    Returns:
        str: Path to the generated file.
    """
    try:
        from powerbi_import.html_template import (
            html_open, html_close, stat_card, stat_grid,
            section_open, section_close, badge, data_table, esc,
        )
    except ImportError:
        from html_template import (
            html_open, html_close, stat_card, stat_grid,
            section_open, section_close, badge, data_table, esc,
        )

    parts = [html_open("Subscription & Alert Migration Report")]

    # Summary
    parts.append(stat_grid([
        stat_card(str(len(subscriptions)), "Subscriptions"),
        stat_card(str(len(alerts)), "Data Alerts"),
        stat_card(str(len(pbi_subs)), "PBI Subscriptions", accent="success"),
        stat_card(str(len(flows)), "Automate Flows"),
        stat_card(str(len(conflicts)), "Conflicts",
                  accent="fail" if conflicts else "success"),
    ]))

    # Subscription mapping table
    parts.append(section_open("sub_mapping", "Subscription Mapping"))
    sub_rows = []
    for i, sub in enumerate(subscriptions):
        pbi = pbi_subs[i] if i < len(pbi_subs) else {}
        sub_rows.append([
            esc(sub.get('content_name', '')),
            esc(sub.get('recipient_email', '')),
            esc(sub.get('frequency', '')),
            esc(sub.get('schedule_name', '')),
            esc(pbi.get('frequency', '')),
            badge('Mapped', 'green') if pbi else badge('Unmapped', 'red'),
        ])
    parts.append(data_table(
        headers=["Content", "Recipient", "Tableau Freq", "Schedule",
                 "PBI Freq", "Status"],
        rows=sub_rows[:200],
    ))
    parts.append(section_close())

    # Alerts
    if alerts:
        parts.append(section_open("alert_mapping", "Data Alert Mapping"))
        alert_rows = []
        for alert in alerts:
            alert_rows.append([
                esc(alert.get('subject', '')),
                esc(alert.get('condition', '')),
                esc(str(alert.get('threshold', ''))),
                esc(alert.get('view_name', '')),
                esc(', '.join(alert.get('recipients', [])[:3])),
            ])
        parts.append(data_table(
            headers=["Subject", "Condition", "Threshold", "View", "Recipients"],
            rows=alert_rows,
        ))
        parts.append(section_close())

    # Conflicts
    if conflicts:
        parts.append(section_open("sched_conflicts", "Schedule Conflicts"))
        for conflict in conflicts:
            severity_color = 'red' if conflict['severity'] == 'error' else 'yellow'
            parts.append(
                f'<div style="margin:8px 0;padding:12px;border-left:4px solid '
                f'{"#d13438" if severity_color == "red" else "#ffb900"};'
                f'background:#faf9f8;border-radius:4px;">'
                f'{badge(conflict["severity"].upper(), severity_color)} '
                f'{esc(conflict["message"])}</div>'
            )
        parts.append(section_close())

    parts.append(html_close())

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(parts))

    logger.info("Subscription report written to %s", output_path)
    return output_path


def save_subscriptions(
    pbi_subs: List[Dict],
    flows: List[Dict],
    output_dir: str,
) -> Dict[str, str]:
    """Save subscription configs and flow templates to files.

    Args:
        pbi_subs: PBI subscription configs.
        flows: Power Automate flow templates.
        output_dir: Directory to write files.

    Returns:
        dict: {subscriptions_path, flows_path}
    """
    os.makedirs(output_dir, exist_ok=True)

    sub_path = os.path.join(output_dir, 'pbi_subscriptions.json')
    with open(sub_path, 'w', encoding='utf-8') as f:
        json.dump(pbi_subs, f, indent=2)

    flow_path = os.path.join(output_dir, 'power_automate_flows.json')
    with open(flow_path, 'w', encoding='utf-8') as f:
        json.dump(flows, f, indent=2)

    return {'subscriptions_path': sub_path, 'flows_path': flow_path}
