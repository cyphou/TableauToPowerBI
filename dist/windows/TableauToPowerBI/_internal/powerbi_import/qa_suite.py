"""Real-world migration QA suite (Sprint 207).

Codifies the manual UC80 QA we used to run (zero stray sentinel glyphs,
no empty visuals, full format coverage, every dashboard zone matched, no
orphan filters) into an automated, repeatable report card that runs against
a generated ``.pbip`` project.

Public API
----------
- :class:`QACheck`     — result of a single check (pass/fail + evidence).
- :class:`QAReport`    — aggregate of all checks for one workbook.
- :func:`run_qa_suite` — execute every check against a ``.pbip`` project.
- :func:`generate_qa_html` — render an HTML report card via ``html_template``.

The module is standard-library only and self-contained: every check reads
the PBIR ``visual.json`` files (and, when available, the Tableau extraction
JSON) and never mutates the project. It is therefore safe to call after
generation, from ``--qa``/``--qa-strict``, or from ``--autoplay``.
"""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# Tableau run-separator sentinels and the non-breaking space that occasionally
# leak into text runs when rich-text parsing mishandles paragraph boundaries.
# A bare occurrence of any of these in a textRun value is a defect.
STRAY_SENTINELS: Tuple[str, ...] = ("\u00c6", "\u0152", "\u00a0")  # Æ, Œ, nbsp
SENTINEL_NAMES: Dict[str, str] = {
    "\u00c6": "Æ (paragraph sentinel)",
    "\u0152": "Œ (run sentinel)",
    "\u00a0": "non-breaking space",
}

# Visual types that carry their own static content (no data query required).
STATIC_CONTENT_TYPES: Tuple[str, ...] = (
    "textbox",
    "image",
    "shape",
    "actionButton",
    "basicShape",
    "pageNavigator",
    "bookmarkNavigator",
)

_MAX_EVIDENCE = 25  # cap per-check evidence rows to keep reports readable


# ─────────────────────────────────────────────────────────────────────────
#  Result containers
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class QACheck:
    """Outcome of a single QA check."""

    key: str
    name: str
    passed: bool
    severity: str = "error"          # error | warning | info
    summary: str = ""
    evidence: List[str] = field(default_factory=list)
    skipped: bool = False

    @property
    def status(self) -> str:
        if self.skipped:
            return "SKIP"
        return "PASS" if self.passed else "FAIL"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "status": self.status,
            "passed": self.passed,
            "skipped": self.skipped,
            "severity": self.severity,
            "summary": self.summary,
            "evidence": self.evidence,
        }


@dataclass
class QAReport:
    """Aggregate QA result for one migrated workbook."""

    workbook: str
    checks: List[QACheck] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    tableau_counts: Dict[str, int] = field(default_factory=dict)
    pbi_counts: Dict[str, int] = field(default_factory=dict)
    fidelity: Optional[float] = None

    # -- aggregate helpers ------------------------------------------------
    @property
    def active_checks(self) -> List[QACheck]:
        return [c for c in self.checks if not c.skipped]

    @property
    def pass_count(self) -> int:
        return sum(1 for c in self.active_checks if c.passed)

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.active_checks if not c.passed)

    @property
    def skip_count(self) -> int:
        return sum(1 for c in self.checks if c.skipped)

    @property
    def total(self) -> int:
        return len(self.active_checks)

    @property
    def passed(self) -> bool:
        """True when every non-skipped check passed."""
        return all(c.passed for c in self.active_checks)

    @property
    def has_error_failure(self) -> bool:
        """True when any *error*-severity check failed (CI gating signal)."""
        return any(
            (not c.passed) and c.severity == "error" for c in self.active_checks
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workbook": self.workbook,
            "timestamp": self.timestamp,
            "passed": self.passed,
            "summary": f"{self.pass_count}/{self.total} checks passed",
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "skip_count": self.skip_count,
            "fidelity": self.fidelity,
            "tableau_counts": self.tableau_counts,
            "pbi_counts": self.pbi_counts,
            "checks": [c.to_dict() for c in self.checks],
        }

    def save_json(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False, default=str)
        return path


# ─────────────────────────────────────────────────────────────────────────
#  PBIR / extraction parsing helpers
# ─────────────────────────────────────────────────────────────────────────

def _find_report_dir(project_dir: str) -> Optional[str]:
    """Return the ``*.Report`` directory inside a ``.pbip`` project, if any."""
    if not project_dir or not os.path.isdir(project_dir):
        return None
    # The project dir itself may be the *.Report folder.
    if project_dir.rstrip(os.sep).endswith(".Report"):
        return project_dir
    for entry in sorted(os.listdir(project_dir)):
        full = os.path.join(project_dir, entry)
        if entry.endswith(".Report") and os.path.isdir(full):
            return full
    return None


def _iter_visual_files(project_dir: str) -> List[str]:
    """All ``visual.json`` paths under the report's pages, sorted."""
    report_dir = _find_report_dir(project_dir)
    if not report_dir:
        return []
    pattern = os.path.join(
        report_dir, "definition", "pages", "**", "visuals", "**", "visual.json"
    )
    return sorted(glob.glob(pattern, recursive=True))


def _load_json(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _short_path(project_dir: str, path: str) -> str:
    """Compact, deterministic label for a visual (page/visual-id)."""
    try:
        parts = path.replace("\\", "/").split("/")
        vid = parts[-2] if len(parts) >= 2 else os.path.basename(path)
        # find the page segment (the dir right after "pages")
        page = ""
        if "pages" in parts:
            i = parts.index("pages")
            if i + 1 < len(parts):
                page = parts[i + 1]
        return f"{page}/{vid}" if page else vid
    except Exception:  # pragma: no cover - defensive
        return os.path.basename(os.path.dirname(path))


def _visual_type(data: Any) -> str:
    if isinstance(data, dict):
        return str(data.get("visual", {}).get("visualType", "")) if isinstance(
            data.get("visual"), dict
        ) else ""
    return ""


def _text_run_values(data: Any) -> List[str]:
    """Collect every ``textRuns[].value`` string anywhere in the visual."""
    values: List[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            runs = node.get("textRuns")
            if isinstance(runs, list):
                for run in runs:
                    if isinstance(run, dict) and isinstance(run.get("value"), str):
                        values.append(run["value"])
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return values


def _encoded_field_count(data: Any) -> int:
    """Number of projected fields across all query roles."""
    if not isinstance(data, dict):
        return 0
    visual = data.get("visual")
    if not isinstance(visual, dict):
        return 0
    query = visual.get("query")
    if not isinstance(query, dict):
        return 0
    state = query.get("queryState")
    if not isinstance(state, dict):
        return 0
    count = 0
    for role in state.values():
        if isinstance(role, dict):
            projections = role.get("projections")
            if isinstance(projections, list):
                count += len(projections)
    return count


def _has_static_content(data: Any) -> bool:
    """True when a content-bearing visual actually carries non-empty content."""
    vtype = _visual_type(data)
    if vtype not in STATIC_CONTENT_TYPES:
        return False
    # Any non-whitespace text run counts as content (textbox, button label).
    for value in _text_run_values(data):
        if value.strip():
            return True
    # Image / shape: presence of an objects block with an image/url property.
    visual = data.get("visual", {}) if isinstance(data, dict) else {}
    objects = visual.get("objects") if isinstance(visual, dict) else None
    if isinstance(objects, dict):
        blob = json.dumps(objects)
        if any(tok in blob for tok in ("image", "imageUrl", "url", "shape")):
            return True
        # image / button visuals with any populated objects are intentional
        if vtype in ("image", "shape", "actionButton", "basicShape") and objects:
            return True
    return False


def _has_format(data: Any) -> bool:
    visual = data.get("visual", {}) if isinstance(data, dict) else {}
    objects = visual.get("objects") if isinstance(visual, dict) else None
    return isinstance(objects, dict) and len(objects) > 0


def _iter_filter_configs(node: Any):
    """Yield every ``filterConfig`` dict found anywhere in *node*."""
    if isinstance(node, dict):
        if isinstance(node.get("filterConfig"), dict):
            yield node["filterConfig"]
        for v in node.values():
            yield from _iter_filter_configs(v)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_filter_configs(item)


def _filter_has_field(flt: Any) -> bool:
    """A filter is well-formed when it references a resolvable field."""
    if not isinstance(flt, dict):
        return False
    fld = flt.get("field")
    if not isinstance(fld, dict) or not fld:
        return False
    blob = json.dumps(fld)
    return ("Property" in blob) or ("Entity" in blob)


# ─────────────────────────────────────────────────────────────────────────
#  Individual checks
# ─────────────────────────────────────────────────────────────────────────

def _check_no_stray_sentinels(project_dir: str, visuals: List[Tuple[str, Any]]) -> QACheck:
    evidence: List[str] = []
    for path, data in visuals:
        for value in _text_run_values(data):
            hits = [SENTINEL_NAMES[s] for s in STRAY_SENTINELS if s in value]
            if hits:
                label = _short_path(project_dir, path)
                snippet = value.strip()[:40] or "(whitespace)"
                evidence.append(f"{label}: {', '.join(hits)} in \"{snippet}\"")
            if len(evidence) >= _MAX_EVIDENCE:
                break
        if len(evidence) >= _MAX_EVIDENCE:
            break
    passed = not evidence
    summary = (
        "No stray sentinel glyphs in any text run."
        if passed
        else f"{len(evidence)} text run(s) contain stray sentinel glyphs."
    )
    return QACheck(
        key="no_stray_sentinels",
        name="No stray sentinel glyphs",
        passed=passed,
        severity="error",
        summary=summary,
        evidence=evidence,
    )


def _check_no_empty_visuals(project_dir: str, visuals: List[Tuple[str, Any]]) -> QACheck:
    evidence: List[str] = []
    for path, data in visuals:
        if _encoded_field_count(data) == 0 and not _has_static_content(data):
            vtype = _visual_type(data) or "unknown"
            evidence.append(f"{_short_path(project_dir, path)} (type={vtype})")
            if len(evidence) >= _MAX_EVIDENCE:
                break
    passed = not evidence
    summary = (
        "Every visual has encoded fields or static content."
        if passed
        else f"{len(evidence)} empty visual(s) (no fields and no static content)."
    )
    return QACheck(
        key="no_empty_visuals",
        name="No empty visuals",
        passed=passed,
        severity="error",
        summary=summary,
        evidence=evidence,
    )


def _check_format_coverage(project_dir: str, visuals: List[Tuple[str, Any]]) -> QACheck:
    evidence: List[str] = []
    for path, data in visuals:
        if not _has_format(data):
            vtype = _visual_type(data) or "unknown"
            evidence.append(f"{_short_path(project_dir, path)} (type={vtype})")
            if len(evidence) >= _MAX_EVIDENCE:
                break
    passed = not evidence
    summary = (
        "All visuals carry format (objects) properties."
        if passed
        else f"{len(evidence)} visual(s) missing format properties."
    )
    return QACheck(
        key="format_coverage",
        name="Format property coverage",
        passed=passed,
        severity="warning",
        summary=summary,
        evidence=evidence,
    )


def _check_zones_matched(
    project_dir: str, visuals: List[Tuple[str, Any]], extraction_dir: Optional[str]
) -> QACheck:
    """Every Tableau dashboard worksheet zone should map to a PBI visual."""
    if not extraction_dir:
        return QACheck(
            key="zones_matched",
            name="Dashboard zones matched",
            passed=True,
            severity="warning",
            summary="Skipped — no Tableau extraction directory provided.",
            skipped=True,
        )
    dash_path = os.path.join(extraction_dir, "dashboards.json")
    dashboards = _load_json(dash_path)
    if not isinstance(dashboards, list):
        return QACheck(
            key="zones_matched",
            name="Dashboard zones matched",
            passed=True,
            severity="warning",
            summary="Skipped — dashboards.json not found.",
            skipped=True,
        )

    worksheet_zones = 0
    for dash in dashboards:
        if not isinstance(dash, dict):
            continue
        for obj in dash.get("objects", []) or []:
            if isinstance(obj, dict) and obj.get("type") == "worksheet":
                if obj.get("name") or obj.get("worksheet"):
                    worksheet_zones += 1

    chart_visuals = sum(1 for _p, d in visuals if _encoded_field_count(d) > 0)
    passed = chart_visuals >= worksheet_zones
    evidence = []
    if not passed:
        evidence.append(
            f"{worksheet_zones} worksheet zone(s) in dashboards but only "
            f"{chart_visuals} data-bearing PBI visual(s)."
        )
    summary = (
        f"{chart_visuals} PBI data visuals cover {worksheet_zones} worksheet zone(s)."
        if passed
        else f"Coverage gap: {worksheet_zones - chart_visuals} unmatched zone(s)."
    )
    return QACheck(
        key="zones_matched",
        name="Dashboard zones matched",
        passed=passed,
        severity="warning",
        summary=summary,
        evidence=evidence,
    )


def _check_no_orphan_filters(project_dir: str, visuals: List[Tuple[str, Any]]) -> QACheck:
    """Filters at any scope must reference a resolvable field."""
    report_dir = _find_report_dir(project_dir)
    evidence: List[str] = []
    total_filters = 0

    scan_paths: List[Tuple[str, Any]] = list(visuals)
    if report_dir:
        for extra in ("report.json",):
            p = os.path.join(report_dir, "definition", extra)
            data = _load_json(p)
            if data is not None:
                scan_paths.append((p, data))
        for page in glob.glob(
            os.path.join(report_dir, "definition", "pages", "**", "page.json"),
            recursive=True,
        ):
            data = _load_json(page)
            if data is not None:
                scan_paths.append((page, data))

    for path, data in scan_paths:
        for cfg in _iter_filter_configs(data):
            filters = cfg.get("filters")
            if not isinstance(filters, list):
                continue
            for flt in filters:
                total_filters += 1
                if not _filter_has_field(flt):
                    fname = flt.get("name", "?") if isinstance(flt, dict) else "?"
                    evidence.append(
                        f"{_short_path(project_dir, path)}: orphan filter '{fname}'"
                    )
                    if len(evidence) >= _MAX_EVIDENCE:
                        break
    passed = not evidence
    summary = (
        f"All {total_filters} filter(s) reference a valid field."
        if passed
        else f"{len(evidence)} orphan filter(s) with no resolvable field."
    )
    return QACheck(
        key="no_orphan_filters",
        name="No orphan filters",
        passed=passed,
        severity="warning",
        summary=summary,
        evidence=evidence,
    )


# ─────────────────────────────────────────────────────────────────────────
#  Counts
# ─────────────────────────────────────────────────────────────────────────

def _tableau_counts(extraction_dir: Optional[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    if not extraction_dir or not os.path.isdir(extraction_dir):
        return counts
    for key, fname in (
        ("worksheets", "worksheets.json"),
        ("dashboards", "dashboards.json"),
        ("calculations", "calculations.json"),
        ("filters", "filters.json"),
        ("parameters", "parameters.json"),
    ):
        data = _load_json(os.path.join(extraction_dir, fname))
        if isinstance(data, list):
            counts[key] = len(data)
    return counts


def _pbi_counts(project_dir: str, visuals: List[Tuple[str, Any]]) -> Dict[str, int]:
    report_dir = _find_report_dir(project_dir)
    pages = 0
    if report_dir:
        pages = len(
            glob.glob(
                os.path.join(report_dir, "definition", "pages", "**", "page.json"),
                recursive=True,
            )
        )
    textboxes = sum(1 for _p, d in visuals if _visual_type(d) == "textbox")
    charts = sum(1 for _p, d in visuals if _encoded_field_count(d) > 0)
    return {
        "pages": pages,
        "visuals": len(visuals),
        "charts": charts,
        "textboxes": textboxes,
    }


# ─────────────────────────────────────────────────────────────────────────
#  Orchestration
# ─────────────────────────────────────────────────────────────────────────

def run_qa_suite(
    project_dir: str,
    extraction_dir: Optional[str] = None,
    workbook: str = "",
    fidelity: Optional[float] = None,
) -> QAReport:
    """Run all real-world QA checks against a generated ``.pbip`` project.

    Args:
        project_dir: Path to the generated project (or its ``*.Report`` dir).
        extraction_dir: Optional Tableau extraction JSON directory; enables
            the zone-matching check and Tableau↔PBI count comparison.
        workbook: Display name for the report (defaults to dir basename).
        fidelity: Optional fidelity score to surface on the report card.

    Returns:
        A :class:`QAReport` with one :class:`QACheck` per check.
    """
    name = workbook or os.path.basename(os.path.normpath(project_dir))
    visual_files = _iter_visual_files(project_dir)
    visuals: List[Tuple[str, Any]] = []
    for vf in visual_files:
        data = _load_json(vf)
        if data is not None:
            visuals.append((vf, data))

    checks = [
        _check_no_stray_sentinels(project_dir, visuals),
        _check_no_empty_visuals(project_dir, visuals),
        _check_format_coverage(project_dir, visuals),
        _check_zones_matched(project_dir, visuals, extraction_dir),
        _check_no_orphan_filters(project_dir, visuals),
    ]

    return QAReport(
        workbook=name,
        checks=checks,
        tableau_counts=_tableau_counts(extraction_dir),
        pbi_counts=_pbi_counts(project_dir, visuals),
        fidelity=fidelity,
    )


# ─────────────────────────────────────────────────────────────────────────
#  HTML report card
# ─────────────────────────────────────────────────────────────────────────

def generate_qa_html(report: QAReport, output_path: str) -> str:
    """Render the QA report card to ``output_path`` and return the path."""
    try:  # local import keeps the module importable without the report deps
        from powerbi_import.html_template import (
            badge,
            data_table,
            esc,
            fidelity_bar,
            html_close,
            html_open,
            section_close,
            section_open,
            stat_card,
            stat_grid,
        )
    except ImportError:  # pragma: no cover - fallback path
        from html_template import (  # type: ignore
            badge,
            data_table,
            esc,
            fidelity_bar,
            html_close,
            html_open,
            section_close,
            section_open,
            stat_card,
            stat_grid,
        )

    try:
        from powerbi_import import __version__ as tool_version
    except ImportError:  # pragma: no cover
        tool_version = ""

    overall = "PASS" if report.passed else "FAIL"
    parts: List[str] = [
        html_open(
            title="Migration QA Report Card",
            subtitle=f"{report.workbook} — {report.pass_count}/{report.total} checks passed",
            version=tool_version,
        )
    ]

    # Stat cards
    cards = [
        stat_card(report.pass_count, "Checks passed", accent="success"),
        stat_card(report.fail_count, "Checks failed",
                  accent="fail" if report.fail_count else "success"),
        stat_card(report.skip_count, "Checks skipped", accent="blue"),
        stat_card(overall, "Overall", accent="success" if report.passed else "fail"),
    ]
    if report.fidelity is not None:
        cards.append(stat_card(f"{report.fidelity:.1f}%", "Fidelity", accent="purple"))
    parts.append(stat_grid(cards))

    # Checks section
    parts.append(section_open("qa-checks", "QA Checks", icon="&#9989;"))
    rows: List[List[str]] = []
    for chk in report.checks:
        ev = "<br>".join(esc(e) for e in chk.evidence[:_MAX_EVIDENCE]) or "&mdash;"
        rows.append([
            badge(chk.status),
            esc(chk.name),
            esc(chk.severity),
            esc(chk.summary),
            ev,
        ])
    parts.append(
        data_table(
            headers=["Status", "Check", "Severity", "Summary", "Evidence"],
            rows=rows,
            table_id="qa-check-table",
            sortable=True,
        )
    )
    parts.append(section_close())

    # Counts section
    if report.tableau_counts or report.pbi_counts:
        parts.append(section_open("qa-counts", "Tableau &#8594; Power BI Counts",
                                  icon="&#128202;"))
        keys = sorted(set(report.tableau_counts) | {
            "worksheets", "dashboards", "filters", "parameters",
        })
        count_rows: List[List[str]] = []
        pbi_map = {
            "worksheets": report.pbi_counts.get("charts", 0),
            "dashboards": report.pbi_counts.get("pages", 0),
            "filters": "",
            "parameters": "",
        }
        for k in keys:
            count_rows.append([
                esc(k),
                str(report.tableau_counts.get(k, "")),
                str(pbi_map.get(k, "")),
            ])
        parts.append(
            data_table(
                headers=["Object", "Tableau", "Power BI"],
                rows=count_rows,
                table_id="qa-count-table",
            )
        )
        parts.append(section_close())

    parts.append(html_close(version=tool_version))

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))
    return output_path
