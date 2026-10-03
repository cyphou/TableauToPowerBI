"""Open every generated .pbip in Power BI Desktop and screenshot verified data.

Unlike the static preflight this launches the real application, waits for the
report window to carry the report name and answer a message. It then presses
Home > Refresh > Schema and data (UI Automation), waits until the model holds
rows, and captures the rendered report. Screenshots are labelled verified or
unverified; --skip-data-check only captures the window state.

Usage:
    python scripts/probe_projects.py <projects-dir> [--shots DIR] [--timeout S]
                                     [--limit N] [--keep-open] [--skip-data-check]
                                     [--no-refresh] [--html FILE]

Exit code 1 when any project fails to load, so it can gate a release.
"""

from __future__ import annotations

import argparse
import glob
import html
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from powerbi_import.desktop_probe import (desktop_pids, find_pbi_desktop,
                                          probe_desktop_open, _safe_output_path)
from powerbi_import.cross_validator import scan_visual_role_contract
from powerbi_import.desktop_feedback_healing import heal_from_desktop_evidence
from powerbi_import.recovery_report import RecoveryReport


def _slug(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _write_private(path: str, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(text)


def _html_report(results, html_path: str) -> str:
    """Opening report: one row per project, green/red card, linked screenshot."""
    base = os.path.dirname(os.path.abspath(html_path))
    rows = []
    for item in results:
        name = html.escape(_slug(item.get('pbip_path') or ''))
        opened = bool(item.get('window_loaded'))
        card = ('<span class="card ok">OPENED</span>' if opened
                else '<span class="card ko">CRASHED</span>')
        data = item.get('data_load') or {}
        refresh = item.get('refresh') or {}
        contract = item.get('visual_contract') or {}
        cells = []
        for key in ('screenshot', 'dialog_screenshot'):
            shot = item.get(key)
            if shot and os.path.isfile(shot):
                rel = os.path.relpath(shot, base).replace(os.sep, '/')
                cells.append(f'<a href="{html.escape(rel)}"><img src="{html.escape(rel)}" '
                             'alt="screenshot" loading="lazy"></a>')
        verified = item.get('screenshot_data_verified')
        label = ('data verified' if verified else
                 'data not verified' if verified is False else 'window only')
        rows.append(
            f'<tr><td>{name}</td><td>{card}</td>'
            f'<td>{html.escape(str(refresh.get("mode", "none")))} / '
            f'{html.escape(str(refresh.get("status", "not_requested")))}</td>'
            f'<td>{html.escape(str(data.get("status", "not_requested")))}</td>'
            f'<td>{int(data.get("tables_nonempty", 0))}/{int(data.get("tables_checked", 0))}</td>'
            f'<td>{int(data.get("total_rows", 0))}</td>'
            f'<td>{int(contract.get("needs_review", 0))}</td>'
            f'<td>{"".join(cells) or "none"}<div class="lbl">{label}</div></td></tr>')
    opened_count = sum(1 for r in results if r.get('window_loaded'))
    verified_count = sum(1 for r in results
                         if (r.get('data_load') or {}).get('status') == 'verified')
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<title>Power BI Desktop opening validation</title><style>'
        'body{font-family:Segoe UI,Arial,sans-serif;margin:32px;background:#f5f7fa;color:#182230}'
        'table{border-collapse:collapse;width:100%;background:#fff}'
        'th,td{border:1px solid #d8dee6;padding:8px;text-align:left;vertical-align:top}'
        'th{background:#e9eef5}img{max-width:360px;max-height:220px;border:1px solid #ccd5df}'
        '.card{display:inline-block;min-width:86px;padding:5px 10px;border-radius:999px;'
        'text-align:center;font-size:12px;font-weight:700}'
        '.ok{background:#dff5e5;color:#126b35;border:1px solid #8bd3a1}'
        '.ko{background:#ffe1e1;color:#a51d2d;border:1px solid #ef9a9a}'
        '.lbl{font-size:12px;color:#52606d}'
        '</style></head><body><h1>Power BI Desktop opening validation</h1>'
        f'<p>{opened_count}/{len(results)} opened &middot; {verified_count}/{len(results)} '
        'with rows verified after Refresh (Schema and data). Only aggregate counts are '
        'recorded; keep this report outside the source repository.</p>'
        '<table><thead><tr><th>Report</th><th>Desktop</th><th>Refresh</th>'
        '<th>Data status</th><th>Tables with rows</th><th>Rows</th><th>Invalid visuals</th>'
        '<th>Screenshot</th></tr></thead><tbody>'
        + '\n'.join(rows) + '</tbody></table></body></html>')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('projects_dir')
    parser.add_argument('--shots', default='artifacts/desktop_shots',
                        help='directory for the screenshots')
    parser.add_argument('--timeout', type=int, default=180,
                        help='seconds to wait for each report window')
    parser.add_argument('--limit', type=int, default=0,
                        help='probe at most N projects')
    parser.add_argument('--keep-open', action='store_true',
                        help='leave Power BI Desktop running')
    parser.add_argument('--skip-data-check', action='store_true',
                        help='capture diagnostic screenshots without verifying model data')
    parser.add_argument('--no-refresh', action='store_true',
                        help='do not press Home > Refresh > Schema and data')
    parser.add_argument('--heal-from-desktop', action='store_true',
                        help='apply deterministic PBIR role repairs from Desktop evidence')
    parser.add_argument('--refresh-timeout', type=int, default=300,
                        help='seconds to wait for rows after the refresh click')
    parser.add_argument('--html', dest='html_path', default='',
                        help='write the opening HTML report here')
    parser.add_argument('--json', dest='json_path', default='',
                        help='write the full report here')
    args = parser.parse_args(argv)

    if args.shots and not _safe_output_path(args.shots):
        print(f'Cannot write screenshots through a symbolic link: {args.shots}')
        return 1

    if not find_pbi_desktop():
        print('Power BI Desktop not found - nothing to probe.')
        return 0
    running = desktop_pids()
    if running:
        print(f'WARNING: {len(running)} Power BI Desktop instance(s) already '
              'running; close them or a probe may watch the wrong window.')

    pattern = os.path.join(args.projects_dir, '**', '*.pbip')
    candidates = sorted(glob.glob(pattern, recursive=True))
    projects_by_name = {}
    for path in candidates:
        key = _slug(path).casefold()
        previous = projects_by_name.get(key)
        if previous is None or len(path) < len(previous):
            projects_by_name[key] = path
    projects = sorted(projects_by_name.values())
    if args.limit:
        projects = projects[:args.limit]
    if not projects:
        print(f'No .pbip found under {args.projects_dir}')
        return 1

    os.makedirs(args.shots, exist_ok=True)
    results = []
    failed = 0
    loaded_count = 0
    data_status_counts = {}
    data_totals = {
        'tables_checked': 0,
        'tables_nonempty': 0,
        'tables_empty': 0,
        'tables_failed': 0,
        'total_rows': 0,
    }
    print(f'Probing {len(projects)} project(s), timeout {args.timeout}s each\n')
    for path in projects:
        name = _slug(path)
        shot = os.path.join(args.shots, name + '.png')
        report = probe_desktop_open(path, timeout=args.timeout,
                                    screenshot_path=shot,
                                    close_after=not args.keep_open,
                                    verify_data=not args.skip_data_check,
                                    refresh=not args.no_refresh,
                                    refresh_timeout=args.refresh_timeout,
                                    capture_unverified=True)
        data = report.to_dict()
        data['visual_contract'] = scan_visual_role_contract(os.path.dirname(path))
        if args.heal_from_desktop:
            project_dir = os.path.dirname(path)
            recovery = RecoveryReport(name)
            healing = heal_from_desktop_evidence(project_dir, data, recovery)
            data['desktop_healing'] = healing
            data['visual_contract'] = scan_visual_role_contract(project_dir)
            if recovery.has_repairs:
                recovery.save(project_dir)
        data_load = data.get('data_load', {})
        data['data_load'] = data_load
        results.append(data)
        loaded = data['window_loaded']
        loaded_count += int(loaded)
        data_status = data_load.get('status', 'unavailable')
        data_status_counts[data_status] = data_status_counts.get(data_status, 0) + 1
        for key in data_totals:
            data_totals[key] += data_load.get(key, 0)
        successful = (loaded and data['visual_contract'].get('needs_review', 0) == 0
                      and (args.skip_data_check or data_status == 'verified'))
        if not successful:
            failed += 1
        mark = 'OK  ' if successful else 'FAIL'
        print(f'{mark} {name[:44]:<44} {data["duration_s"]:>6.1f}s '
              f'{"shot" if data["screenshot"] else "no shot"}')

    status_summary = ', '.join(
        f'{status}={count}' for status, count in sorted(data_status_counts.items()))
    totals_summary = ', '.join(
        f'{key}={value}' for key, value in data_totals.items())
    print(f'\nwindow_loaded {loaded_count}/{len(projects)}; '
          f'data statuses: {status_summary}; data totals: {totals_summary}; '
          f'successful {len(projects) - failed}/{len(projects)}; '
          f'screenshots in {args.shots}')
    if args.json_path:
        if not _safe_output_path(args.json_path):
            print(f'Cannot write JSON report through a symbolic link: {args.json_path}')
            return 1
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        no_follow = getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(args.json_path, flags | no_follow, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(results, handle, indent=2, ensure_ascii=False)
    if args.html_path:
        if not _safe_output_path(args.html_path):
            print(f'Cannot write HTML report through a symbolic link: {args.html_path}')
            return 1
        _write_private(args.html_path, _html_report(results, args.html_path))
        print(f'Opening report: {args.html_path}')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
