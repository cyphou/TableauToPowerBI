"""Open every generated .pbip in Power BI Desktop and screenshot what loads.

Unlike the static preflight this launches the real application, waits for the
report window to carry the report name and answer a message, then captures it.
That is the only check that can catch a project Desktop silently refuses.

Usage:
    python scripts/probe_projects.py <projects-dir> [--shots DIR] [--timeout S]
                                     [--limit N] [--keep-open]

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
                                          probe_desktop_open)


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
    parser.add_argument('--json', dest='json_path', default='',
                        help='write the full report here')
    args = parser.parse_args(argv)

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
    print(f'Probing {len(projects)} project(s), timeout {args.timeout}s each\n')
    for path in projects:
        name = _slug(path)
        shot = os.path.join(args.shots, name + '.png')
        report = probe_desktop_open(path, timeout=args.timeout,
                                    screenshot_path=shot,
                                    close_after=not args.keep_open)
        data = report.to_dict()
        results.append(data)
        loaded = data['window_loaded']
        if not loaded:
            failed += 1
        mark = 'OK  ' if loaded else 'FAIL'
        print(f'{mark} {name[:44]:<44} {data["duration_s"]:>6.1f}s '
              f'{"shot" if data["screenshot"] else "no shot"}')
        for signal in data['signals'][:2]:
            print(f'       {signal[:110]}')

    print(f'\nloaded {len(projects) - failed}/{len(projects)}, '
          f'screenshots in {args.shots}')
    if args.json_path:
        with open(args.json_path, 'w', encoding='utf-8') as handle:
            json.dump(results, handle, indent=2, ensure_ascii=False)
        print(f'report written to {args.json_path}')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
