#!/usr/bin/env python3
"""Per-workbook pixel-perfect golden fixture generator (Sprint 205).

Migrates a curated set of committed Tableau sample workbooks to ``.pbip``
projects and captures a *deterministic* snapshot of the pixel-relevant
attributes of every generated visual (position, size, type, encoded field
count, format presence, title font).  The snapshots are written to
``tests/golden/<workbook>/visuals.json`` and act as regression baselines for
``tests/test_pixel_golden.py``.

Re-run this script whenever an intentional, reviewed change to layout or
formatting shifts the golden baseline::

    python scripts/generate_pixel_fixtures.py            # regenerate all
    python scripts/generate_pixel_fixtures.py --check    # diff only, exit 1 on drift

The snapshot is order-normalised (sorted by page/type/x/y) so it never depends
on visual UUID filenames or filesystem ordering.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from powerbi_import.qa_suite import (  # noqa: E402
    _encoded_field_count,
    _has_format,
    _iter_visual_files,
    _load_json,
    _short_path,
    _visual_type,
)

# ── Curated fixture workbooks (committed under examples/tableau_samples/) ──
# NOTE: Enterprise_Sales contains two heavily overlapping zones (a textbox
# backdrop behind a tableEx). Sprint 204 made the overlap-stagger healing
# deterministic (z-order keyed, not random UUID directory order), so it is now
# part of the golden set — the backdrop textbox stays anchored and the
# foreground worksheet is staggered by a stable +32 px.
GOLDEN_WORKBOOKS = {
    "BigQuery_Analytics": "BigQuery_Analytics.twb",
    "Enterprise_Sales": "Enterprise_Sales.twb",
    "Financial_Report": "Financial_Report.twb",
    "HR_Analytics": "HR_Analytics.twb",
    "Manufacturing_IoT": "Manufacturing_IoT.twb",
    "Marketing_Campaign": "Marketing_Campaign.twb",
    "Superstore_Sales": "Superstore_Sales.twb",
}

SAMPLES_DIR = os.path.join(_REPO_ROOT, "examples", "tableau_samples")
GOLDEN_DIR = os.path.join(_REPO_ROOT, "tests", "golden")


# ── Snapshot extraction ────────────────────────────────────────────

def _page_of(path: str) -> str:
    """Stable page identity = the page's displayName (folder names are UUIDs)."""
    parts = path.replace("\\", "/").split("/")
    if "pages" in parts:
        i = parts.index("pages")
        if i + 1 < len(parts):
            # Reconstruct the OS path to the page folder and read page.json.
            page_folder = os.sep.join(path.replace("/", os.sep).split(os.sep)[: -2])
            # page.json lives two levels up from visual.json: <pagefolder>/visuals/<vid>/visual.json
            page_dir = os.path.dirname(os.path.dirname(os.path.dirname(path)))
            page_json = _load_json(os.path.join(page_dir, "page.json"))
            if isinstance(page_json, dict):
                name = page_json.get("displayName") or page_json.get("name")
                if isinstance(name, str) and name:
                    return name
            return parts[i + 1]
    return ""


def _title_font(data) -> dict:
    """Best-effort extraction of a title's fontFamily / fontSize."""
    visual = data.get("visual", {}) if isinstance(data, dict) else {}
    objects = visual.get("objects") if isinstance(visual, dict) else None
    family = ""
    size = ""

    def _walk(node):
        nonlocal family, size
        if isinstance(node, dict):
            for key, val in node.items():
                if key == "fontFamily" and isinstance(val, (str, int, float)) and not family:
                    family = str(val)
                elif key == "fontSize" and isinstance(val, (str, int, float)) and not size:
                    size = str(val)
                else:
                    _walk(val)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    title = objects.get("title") if isinstance(objects, dict) else None
    if title is not None:
        _walk(title)
    return {"fontFamily": family, "fontSize": size}


def snapshot_project(pbip_dir: str) -> dict:
    """Deterministic pixel-attribute snapshot of every visual in a project."""
    visuals = []
    for vf in _iter_visual_files(pbip_dir):
        data = _load_json(vf)
        if not isinstance(data, dict):
            continue
        pos = data.get("position", {}) if isinstance(data.get("position"), dict) else {}
        title = _title_font(data)
        visuals.append({
            "page": _page_of(vf),
            "type": _visual_type(data),
            "x": pos.get("x", 0),
            "y": pos.get("y", 0),
            "width": pos.get("width", 0),
            "height": pos.get("height", 0),
            "z": pos.get("z", 0),
            "fields": _encoded_field_count(data),
            "has_format": _has_format(data),
            "title_font": title["fontFamily"],
            "title_size": title["fontSize"],
        })
    visuals.sort(key=lambda v: (
        v["page"], v["type"], float(v["x"]), float(v["y"]),
        float(v["width"]), float(v["height"]), float(v["z"]),
        v["fields"], str(v["title_font"]), str(v["title_size"]),
        bool(v["has_format"]),
    ))
    return {"visual_count": len(visuals), "visuals": visuals}


# ── Migration helper ───────────────────────────────────────────────

def migrate_workbook(twb_path: str, out_dir: str) -> str:
    """Run migrate.py for *twb_path*, returning the generated project dir."""
    cmd = [
        sys.executable, os.path.join(_REPO_ROOT, "migrate.py"),
        twb_path, "--output-dir", out_dir, "--quiet",
    ]
    subprocess.run(cmd, cwd=_REPO_ROOT, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # migrate.py creates <out_dir>/<name>/<name>.pbip alongside .Report/.SemanticModel
    for entry in sorted(os.listdir(out_dir)):
        full = os.path.join(out_dir, entry)
        if os.path.isdir(full):
            return full
    return out_dir


def build_snapshot_for_workbook(twb_name: str) -> dict:
    """Migrate a sample workbook to a temp dir and return its snapshot."""
    twb_path = os.path.join(SAMPLES_DIR, twb_name)
    if not os.path.isfile(twb_path):
        raise FileNotFoundError(twb_path)
    tmp = tempfile.mkdtemp(prefix="pixel_golden_")
    try:
        pbip_dir = migrate_workbook(twb_path, tmp)
        return snapshot_project(pbip_dir)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ── CLI ────────────────────────────────────────────────────────────

def _golden_path(name: str) -> str:
    return os.path.join(GOLDEN_DIR, name, "visuals.json")


def regenerate(check: bool = False) -> int:
    drift = 0
    for name, twb in GOLDEN_WORKBOOKS.items():
        try:
            snap = build_snapshot_for_workbook(twb)
        except FileNotFoundError:
            print(f"  [skip] {name}: sample not found ({twb})")
            continue
        path = _golden_path(name)
        if check:
            existing = _load_json(path)
            if existing != snap:
                drift += 1
                old = (existing or {}).get("visual_count", "?")
                print(f"  [drift] {name}: {old} -> {snap['visual_count']} visuals")
            else:
                print(f"  [ok]    {name}: {snap['visual_count']} visuals")
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(snap, fh, indent=1, ensure_ascii=False, sort_keys=True)
            print(f"  [wrote] {name}: {snap['visual_count']} visuals -> {path}")
    return 1 if (check and drift) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate pixel-perfect golden fixtures")
    parser.add_argument("--check", action="store_true",
                        help="Compare against committed fixtures; exit 1 on drift")
    args = parser.parse_args()
    return regenerate(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
