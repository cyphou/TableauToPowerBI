"""Open every generated .pbip in Power BI Desktop and screenshot verified data.

Unlike the static preflight this launches the real application, waits for the
report window to carry the report name and answer a message. By default it also
queries aggregate row counts through Desktop's local model and captures only
when the M-backed tables contain data. Use --skip-data-check only for a
diagnostic window screenshot.

Usage:
    python scripts/probe_projects.py <projects-dir> [--shots DIR] [--timeout S]
                                     [--limit N] [--keep-open] [--skip-data-check]

Exit code 1 when any project fails to load, so it can gate a release.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from powerbi_import.desktop_probe import (desktop_pids, find_pbi_desktop,
                                          probe_desktop_open, _safe_output_path)


def _slug(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


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
    projects = sorted(glob.glob(pattern, recursive=True))
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
                                    verify_data=not args.skip_data_check)
        data = report.to_dict()
        data_load = data.get('data_load', {})
        data['data_load'] = data_load
        results.append(data)
        loaded = data['window_loaded']
        loaded_count += int(loaded)
        data_status = data_load.get('status', 'unavailable')
        data_status_counts[data_status] = data_status_counts.get(data_status, 0) + 1
        for key in data_totals:
            data_totals[key] += data_load.get(key, 0)
        successful = loaded and (
            args.skip_data_check or data_status == 'verified')
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
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
