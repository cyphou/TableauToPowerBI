#!/usr/bin/env python3
"""Prove each guard's tests fail when the thing they guard is broken.

A test that cannot fail is worse than no test: it reports success and hides
the defect it was written for. This session produced three of them — a check
satisfied by the ``def`` line it was meant to find a call for, an assertion
matched by a docstring instead of a call site, and a harness that silently
never wrote its mutations and reported every control as passing.

``tests/test_mutation.py`` does not inject mutations; its docstring says so.
This does. Each control breaks one specific behaviour, confirms the edit
reached disk, runs the guard's own tests, and requires a failure. The file is
always restored.

    python scripts/verify_controls.py            # report
    python scripts/verify_controls.py --strict   # exit 1 if a control is silent
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from tests.control_specs import CONTROLS  # noqa: E402


class ControlResult:
    __slots__ = ("name", "status", "detail")

    def __init__(self, name, status, detail=""):
        self.name = name
        self.status = status  # fired | silent | invalid
        self.detail = detail


def _dirty_files() -> set:
    """Tracked files with uncommitted changes."""
    proc = subprocess.run(["git", "status", "--porcelain"],
                          capture_output=True, text=True, cwd=_REPO_ROOT)
    dirty = set()
    for line in (proc.stdout or "").splitlines():
        path = line[3:].strip().replace("\\", "/")
        if path:
            dirty.add(path)
    return dirty


def _run_tests(test_path: str) -> tuple:
    """Return (failures, summary) for one test file."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test_path, "-q", "--no-header"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=_REPO_ROOT,
    )
    summary = ""
    for line in (proc.stdout or "").splitlines():
        if "passed" in line or "failed" in line or "error" in line:
            summary = line.strip()
    failed = "failed" in summary or "error" in summary
    return failed, summary


def run_control(spec: dict) -> ControlResult:
    target = os.path.join(_REPO_ROOT, spec["file"])
    backup = target + ".controlbak"

    with open(target, encoding="utf-8") as handle:
        original = handle.read()

    if spec["old"] not in original:
        return ControlResult(spec["name"], "invalid",
                             f"pattern not found in {spec['file']}")

    mutated = original.replace(spec["old"], spec["new"],
                               -1 if spec.get("all") else 1)
    if mutated == original:
        return ControlResult(spec["name"], "invalid", "replacement changed nothing")

    shutil.copyfile(target, backup)
    try:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(mutated)
        # The harness that lied did so by never writing; confirm it landed.
        with open(target, encoding="utf-8") as handle:
            if handle.read() != mutated:
                return ControlResult(spec["name"], "invalid",
                                     "mutation did not reach disk")
        failed, summary = _run_tests(spec["test"])
        if failed:
            return ControlResult(spec["name"], "fired", summary)
        return ControlResult(spec["name"], "silent",
                             f"{spec['test']} still passed: {summary}")
    finally:
        shutil.copyfile(backup, target)
        os.remove(backup)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 if any control fails to fire")
    parser.add_argument("--filter", default=None,
                        help="only run controls whose name contains this text")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="run even if a file to be mutated has uncommitted work")
    args = parser.parse_args(argv)

    specs = [s for s in CONTROLS
             if not args.filter or args.filter.lower() in s["name"].lower()]

    # Each control edits a real source file. If this is interrupted mid-run the
    # file is left mutated, so require it to be recoverable with git checkout.
    if not args.allow_dirty:
        targets = {s["file"].replace("\\", "/") for s in specs}
        at_risk = sorted(targets & _dirty_files())
        if at_risk:
            print("Refusing to run: these files would be mutated but have "
                  "uncommitted changes,\nso an interrupted run could not be "
                  "recovered with git checkout:\n")
            for path in at_risk:
                print(f"      {path}")
            print("\nCommit or stash them, or pass --allow-dirty.")
            return 2

    results = [run_control(spec) for spec in specs]
    fired = [r for r in results if r.status == "fired"]
    broken = [r for r in results if r.status != "fired"]

    print(f"controls : {len(results)}")
    print(f"fired    : {len(fired)}")
    print(f"silent   : {len([r for r in broken if r.status == 'silent'])}")
    print(f"invalid  : {len([r for r in broken if r.status == 'invalid'])}")
    print()
    for result in results:
        mark = {"fired": "ok", "silent": "SILENT", "invalid": "INVALID"}[result.status]
        print(f"  [{mark:7}] {result.name}")
        if result.status != "fired":
            print(f"             {result.detail}")

    if broken:
        print("\nA silent control means the test cannot detect the defect it"
              "\nexists for. Fix the test, not the control.")

    return 1 if (args.strict and broken) else 0


if __name__ == "__main__":
    sys.exit(main())
