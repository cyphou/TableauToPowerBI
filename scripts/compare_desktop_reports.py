#!/usr/bin/env python
"""Standalone Desktop diagnostic using the migration-shared capture engine."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import TypedDict

# Support direct execution from any working directory, without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from powerbi_import.desktop_comparison import compare_desktop_reports


class _ComparisonOptions(TypedDict, total=False):
    screen_comparison: str


def _directory(value: str) -> str:
    path = Path(value).expanduser().absolute()
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"Not an existing directory: {value}")
    return str(path)


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Timeout must be numeric") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("Timeout must be finite and positive")
    return seconds


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Launch Tableau and Power BI for paired workbooks in folders; "
                    "save fresh full-window captures and manual-review JSON/HTML. "
                    "Requires an interactive Windows session and installed apps. "
                    "Power BI refreshes/verifies data; Tableau is not refreshed. "
                    "Comparison-launched processes close after captures. "
                    "This standalone script does not run migration.")
    parser.add_argument("source_dir", type=_directory,
                        help="Tableau source folder, recursively containing .twb/.twbx")
    parser.add_argument("output_root", type=_directory,
                        help="Generated batch project root (not a single PBIP file)")
    parser.add_argument("--tableau-desktop-path", help="Explicit tableau.exe override")
    parser.add_argument("--powerbi-desktop-path", help="Explicit PBIDesktop.exe override")
    parser.add_argument("--timeout", type=_positive_seconds, default=90,
                        help="Document-window wait seconds per application (default: 90)")
    parser.add_argument("--all-pages", action="store_true",
                        help="Inventory all pages and capture only UIA-verified selected tabs; "
                             "unmatched/ambiguous/inaccessible pages remain explicit")
    parser.add_argument("--page-timeout", type=_positive_seconds, default=30,
                        help="Selection/verification/capture deadline per page (default: 30)")
    parser.add_argument("--screen-comparison", choices=("manual", "local"), default="manual",
                        help="manual (default), or local stdlib-only decoded PNG pixel "
                             "differences; no AI/network, SSIM or semantic parity verdict; "
                             "no resizing/cropping. Does not affect capture exit status")
    args = parser.parse_args(argv)
    try:
        # Preserve the existing default call contract for API wrappers.
        comparison_options: _ComparisonOptions = (
            {"screen_comparison": "local"} if args.screen_comparison == "local" else {})
        manifest = compare_desktop_reports(
            args.source_dir, args.output_root, timeout=args.timeout,
            tableau_desktop_path=args.tableau_desktop_path,
            powerbi_desktop_path=args.powerbi_desktop_path,
            all_pages=args.all_pages, page_timeout=args.page_timeout,
            **comparison_options)
    except (ValueError, OSError) as exc:
        print(f"Comparison failed: {exc}", file=sys.stderr)
        return 1
    scope = ("local non-semantic pixels; no parity score"
             if args.screen_comparison == "local" else "manual review only; no parity score")
    print(f"Capture status: {manifest['status']} ({scope})")
    print(f"Manifest: {Path(manifest['run_dir']) / 'manifest.json'}")
    print(f"HTML: {Path(manifest['run_dir']) / 'comparison.html'}")
    if args.all_pages:
        print(f"All-page totals (excluding launch captures): {manifest['totals']}")
    for row in manifest["workbooks"]:
        print(f"{row['relative_source']}: pair={row['pair_status']}, "
              f"Tableau={row['tableau']['status']}, Power BI={row['powerbi']['status']}")
    return 0 if manifest["status"] == "captured" else 1


if __name__ == "__main__":
    raise SystemExit(main())
