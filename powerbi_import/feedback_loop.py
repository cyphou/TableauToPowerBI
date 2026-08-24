"""
Phase 10 — Continuous Feedback Loop.

Turns every real-world failure into a regression test fixture so the
zero-touch open rate improves continuously.

Components:
    - ``--report-issue`` CLI flag: creates a redacted triage package
    - ``IssueCollector``: gathers failure context, redacts credentials
    - ``RegressionFixtureGenerator``: derives minimal repro fixture from
      triage package for ``tests/fixtures/regressions/``
    - ``ZeroTouchTracker``: computes and persists Zero-Touch Open Rate

Usage:
    from powerbi_import.feedback_loop import (
        IssueCollector, RegressionFixtureGenerator, ZeroTouchTracker,
    )

    collector = IssueCollector(project_dir, source_basename)
    package = collector.collect(verdict, source_file=twbx_path)

    gen = RegressionFixtureGenerator(package)
    fixture_path = gen.generate(output_dir='tests/fixtures/regressions/')

    tracker = ZeroTouchTracker()
    tracker.record('wb1.twbx', success=True)
    tracker.record('wb2.twbx', success=False, failure_mode='missing_table')
    rate = tracker.zero_touch_rate  # 0.5
    tracker.save('docs/zero_touch_history.json')
"""

import json
import logging
import os
import re
import zipfile
from datetime import datetime

logger = logging.getLogger('tableau_to_powerbi.feedback')


# ── Credential Redaction ────────────────────────────────────────────

_REDACT_PATTERNS = [
    (re.compile(r'(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)\s*[=:]\s*\S+', re.I),
     r'\1=***REDACTED***'),
    (re.compile(r'(Server|Data Source|Host)\s*=\s*[^;]+', re.I),
     r'\1=***REDACTED***'),
    (re.compile(r'(User\s*Id|uid|Username)\s*=\s*[^;]+', re.I),
     r'\1=***REDACTED***'),
    (re.compile(r'"(password|secret|token|apiKey|connectionString)"\s*:\s*"[^"]*"', re.I),
     r'"\1": "***REDACTED***"'),
]


def redact_text(text):
    """Remove credentials and connection strings from text."""
    result = text
    for pattern, replacement in _REDACT_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


# ── Issue Collector ─────────────────────────────────────────────────

class IssueCollector:
    """Collects failure context into a redacted issue package.

    The package is a ZIP containing:
    - ``issue_metadata.json``: verdict, timestamp, failure modes
    - ``extraction/*.json``: redacted extraction JSONs
    - ``triage.html``: rendered triage report
    - ``fixture_hint.json``: minimal XML skeleton for regression fixture
    """

    def __init__(self, project_dir, source_basename, extract_dir=None):
        self.project_dir = project_dir
        self.source_basename = source_basename
        self.extract_dir = extract_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'tableau_export'
        )

    def collect(self, verdict=None, source_file=None, output_dir=None):
        """Create a redacted issue package ZIP.

        Args:
            verdict: Verdict object or dict from rollback engine
            source_file: path to original .twbx (not included, only metadata)
            output_dir: where to write the ZIP (default: project_dir parent)

        Returns:
            str: path to the created ZIP file, or None on failure
        """
        out = output_dir or os.path.dirname(self.project_dir)
        os.makedirs(out, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        zip_name = f'{self.source_basename}_issue_{ts}.zip'
        zip_path = os.path.join(out, zip_name)

        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                # 1. Metadata
                meta = self._build_metadata(verdict, source_file)
                zf.writestr('issue/metadata.json',
                            json.dumps(meta, indent=2, ensure_ascii=False))

                # 2. Redacted extraction JSONs
                self._add_extraction_jsons(zf)

                # 3. QA report
                qa_path = os.path.join(self.project_dir, 'qa_report.json')
                if os.path.isfile(qa_path):
                    with open(qa_path, 'r', encoding='utf-8') as f:
                        content = redact_text(f.read())
                    zf.writestr('issue/qa_report.json', content)

                # 4. Fixture hint
                hint = self._build_fixture_hint(verdict)
                zf.writestr('issue/fixture_hint.json',
                            json.dumps(hint, indent=2, ensure_ascii=False))

            logger.info("Issue package created: %s", zip_path)
            return zip_path
        except OSError as exc:
            logger.error("Failed to create issue package: %s", exc)
            return None

    def _build_metadata(self, verdict, source_file):
        meta = {
            'source_basename': self.source_basename,
            'timestamp': datetime.now().isoformat(),
            'source_file_name': os.path.basename(source_file) if source_file else None,
            'source_file_size': (
                os.path.getsize(source_file)
                if source_file and os.path.isfile(source_file) else None
            ),
        }
        if verdict:
            if hasattr(verdict, 'to_dict'):
                meta['verdict'] = verdict.to_dict()
            elif isinstance(verdict, dict):
                meta['verdict'] = verdict
        return meta

    def _add_extraction_jsons(self, zf):
        if not os.path.isdir(self.extract_dir):
            return
        for name in os.listdir(self.extract_dir):
            if not name.endswith('.json'):
                continue
            full = os.path.join(self.extract_dir, name)
            if not os.path.isfile(full):
                continue
            try:
                with open(full, 'r', encoding='utf-8') as f:
                    content = f.read()
                zf.writestr(f'issue/extraction/{name}', redact_text(content))
            except (OSError, UnicodeDecodeError):
                pass

    def _build_fixture_hint(self, verdict):
        """Build a hint for regression fixture generation."""
        hint = {
            'source_basename': self.source_basename,
            'failure_modes': [],
            'affected_areas': [],
        }
        if verdict:
            issues = []
            if hasattr(verdict, 'issues'):
                issues = verdict.issues
            elif isinstance(verdict, dict):
                issues = verdict.get('issues', [])

            for item in issues:
                if isinstance(item, (list, tuple)) and len(item) >= 3:
                    sev, src, msg = item[0], item[1], item[2]
                elif isinstance(item, dict):
                    sev = item.get('severity', '')
                    src = item.get('source', '')
                    msg = item.get('message', '')
                else:
                    continue

                if sev in ('error', 'critical'):
                    hint['failure_modes'].append(msg)
                if src not in hint['affected_areas']:
                    hint['affected_areas'].append(src)

        return hint


# ── Regression Fixture Generator ────────────────────────────────────

class RegressionFixtureGenerator:
    """Generates a minimal regression test fixture from an issue package."""

    def __init__(self, issue_package_path):
        self.package_path = issue_package_path

    def generate(self, output_dir='tests/fixtures/regressions'):
        """Extract fixture hint and create a test fixture directory.

        Returns:
            str: path to the created fixture directory, or None on failure
        """
        os.makedirs(output_dir, exist_ok=True)

        try:
            with zipfile.ZipFile(self.package_path, 'r') as zf:
                # Read metadata
                meta = {}
                if 'issue/metadata.json' in zf.namelist():
                    meta = json.loads(zf.read('issue/metadata.json'))

                # Read fixture hint
                hint = {}
                if 'issue/fixture_hint.json' in zf.namelist():
                    hint = json.loads(zf.read('issue/fixture_hint.json'))

                name = meta.get('source_basename', 'unknown')
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                fixture_name = f'{name}_{ts}'
                fixture_dir = os.path.join(output_dir, fixture_name)
                os.makedirs(fixture_dir, exist_ok=True)

                # Write fixture metadata
                fixture_meta = {
                    'source': name,
                    'created': datetime.now().isoformat(),
                    'failure_modes': hint.get('failure_modes', []),
                    'affected_areas': hint.get('affected_areas', []),
                }
                with open(os.path.join(fixture_dir, 'fixture.json'), 'w',
                           encoding='utf-8') as f:
                    json.dump(fixture_meta, f, indent=2, ensure_ascii=False)

                # Copy extraction JSONs as fixture input
                for entry in zf.namelist():
                    if entry.startswith('issue/extraction/') and entry.endswith('.json'):
                        basename = os.path.basename(entry)
                        content = zf.read(entry).decode('utf-8')
                        with open(os.path.join(fixture_dir, basename), 'w',
                                   encoding='utf-8') as f:
                            f.write(content)

                logger.info("Regression fixture created: %s", fixture_dir)
                return fixture_dir

        except (zipfile.BadZipFile, OSError, json.JSONDecodeError) as exc:
            logger.error("Failed to generate fixture: %s", exc)
            return None


# ── Zero-Touch Open Rate Tracker ────────────────────────────────────

class ZeroTouchTracker:
    """Tracks Zero-Touch Open Rate across migration runs.

    Records per-workbook success/failure and computes the running rate.
    Persists history to JSON for dashboard consumption.
    """

    def __init__(self, history_path=None):
        self.history_path = history_path
        self.records = []
        if history_path and os.path.isfile(history_path):
            try:
                with open(history_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.records = data.get('records', [])
            except (json.JSONDecodeError, OSError):
                self.records = []

    def record(self, workbook_name, *, success, failure_mode='',
               verdict_severity='', issues_count=0):
        """Record a migration outcome."""
        self.records.append({
            'workbook': workbook_name,
            'timestamp': datetime.now().isoformat(),
            'success': bool(success),
            'failure_mode': failure_mode,
            'verdict_severity': verdict_severity,
            'issues_count': issues_count,
        })

    @property
    def zero_touch_rate(self):
        """Compute Zero-Touch Open Rate (0.0 – 1.0)."""
        if not self.records:
            return 0.0
        successes = sum(1 for r in self.records if r['success'])
        return successes / len(self.records)

    @property
    def total_count(self):
        return len(self.records)

    @property
    def success_count(self):
        return sum(1 for r in self.records if r['success'])

    @property
    def failure_count(self):
        return sum(1 for r in self.records if not r['success'])

    def top_failure_modes(self, limit=10):
        """Return top failure modes by frequency."""
        counts = {}
        for r in self.records:
            if not r['success'] and r.get('failure_mode'):
                mode = r['failure_mode']
                counts[mode] = counts.get(mode, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]

    def get_summary(self):
        """Return summary dict for dashboard."""
        return {
            'total': self.total_count,
            'success': self.success_count,
            'failed': self.failure_count,
            'zero_touch_rate': round(self.zero_touch_rate * 100, 1),
            'top_failure_modes': self.top_failure_modes(),
        }

    def save(self, path=None):
        """Persist history to JSON file."""
        out = path or self.history_path
        if not out:
            return None
        try:
            os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
            data = {
                'updated_at': datetime.now().isoformat(),
                'summary': self.get_summary(),
                'records': self.records,
            }
            with open(out, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return out
        except OSError as exc:
            logger.error("Failed to save tracker: %s", exc)
            return None

    def render_dashboard_html(self):
        """Render a Zero-Touch Open Rate HTML dashboard."""
        summary = self.get_summary()
        rate = summary['zero_touch_rate']
        rate_color = '#107c10' if rate >= 99 else '#ff8c00' if rate >= 90 else '#d13438'

        failure_rows = ''
        for mode, count in summary['top_failure_modes']:
            esc_mode = mode.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            failure_rows += f'<tr><td>{esc_mode}</td><td>{count}</td></tr>\n'

        recent_rows = ''
        for r in reversed(self.records[-20:]):
            status = '&#9989;' if r['success'] else '&#10060;'
            wb = r['workbook'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            mode = (r.get('failure_mode', '') or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            ts = r['timestamp'][:16].replace('T', ' ')
            recent_rows += f'<tr><td>{status}</td><td>{wb}</td><td>{mode}</td><td>{ts}</td></tr>\n'

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Zero-Touch Open Rate Dashboard</title>
<style>
  body {{ font-family: 'Segoe UI', sans-serif; margin: 2rem; background: #fafafa; }}
  .header {{ background: linear-gradient(135deg, #0078d4, #004578);
             color: white; padding: 2rem; border-radius: 8px; margin-bottom: 2rem; }}
  .header h1 {{ margin: 0; font-size: 1.6rem; }}
  .rate {{ font-size: 3rem; font-weight: bold; color: {rate_color};
           margin: 1rem 0; }}
  .stats {{ display: flex; gap: 2rem; margin: 1rem 0; }}
  .stat {{ background: rgba(255,255,255,0.15); padding: 0.5rem 1rem;
           border-radius: 4px; }}
  .stat .num {{ font-size: 1.4rem; font-weight: bold; }}
  h2 {{ color: #333; border-bottom: 2px solid #0078d4; padding-bottom: 0.5rem; }}
  table {{ width: 100%; border-collapse: collapse; background: white;
           box-shadow: 0 1px 3px rgba(0,0,0,0.1); border-radius: 8px;
           overflow: hidden; margin-bottom: 2rem; }}
  th {{ background: #f3f3f3; text-align: left; padding: 0.7rem 1rem;
        border-bottom: 2px solid #ddd; }}
  td {{ padding: 0.5rem 1rem; border-bottom: 1px solid #eee; }}
  .target {{ color: #666; font-size: 0.9rem; }}
</style>
</head>
<body>
<div class="header">
  <h1>Zero-Touch Open Rate</h1>
  <div class="rate">{rate}%</div>
  <p class="target">Target: &ge; 99% | {summary['total']} workbooks tracked</p>
  <div class="stats">
    <div class="stat"><div class="num">{summary['success']}</div>Success</div>
    <div class="stat"><div class="num">{summary['failed']}</div>Failed</div>
  </div>
</div>

<h2>Top Failure Modes</h2>
<table>
  <thead><tr><th>Failure Mode</th><th>Count</th></tr></thead>
  <tbody>{failure_rows if failure_rows else '<tr><td colspan="2">None</td></tr>'}</tbody>
</table>

<h2>Recent Migrations (last 20)</h2>
<table>
  <thead><tr><th>Status</th><th>Workbook</th><th>Failure Mode</th><th>Timestamp</th></tr></thead>
  <tbody>{recent_rows if recent_rows else '<tr><td colspan="4">No records</td></tr>'}</tbody>
</table>
</body>
</html>"""

    def save_dashboard(self, output_path='docs/zero_error_dashboard.html'):
        """Write dashboard HTML to file."""
        try:
            os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
            html = self.render_dashboard_html()
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(html)
            return output_path
        except OSError as exc:
            logger.error("Failed to save dashboard: %s", exc)
            return None
