"""Functionality parity registry & gap scan (v43/v44, Sprint 209).

A single source of truth mapping every supported Tableau feature to a Power BI
coverage status — ``exact`` / ``approximated`` / ``healed`` / ``unsupported`` —
with a target mechanism and remediation note. A per-workbook scan resolves the
status of every feature actually *in use* and computes a parity score.

Coverage status semantics:
    exact         Maps 1:1 to a native Power BI feature.
    healed        No 1:1 target, but auto-converted to a faithful equivalent.
    approximated  Nearest-equivalent with a documented behavioural difference.
    unsupported   No Power BI target; surfaced as a note, never silently dropped.

Public API (matches the MCP ``parity_scan`` tool contract):
    scan_workbook(converted)      -> ParityScan
    ParityScan.to_dict()          -> dict
    ParityScan.to_html(path=None) -> str
    features_by_category()        -> dict[str, list[Feature]]
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, List, Optional

REGISTRY_VERSION = "1.0.0"

EXACT = "exact"
HEALED = "healed"
APPROXIMATED = "approximated"
UNSUPPORTED = "unsupported"

# Score weight per status (approximated is a partial credit).
_STATUS_WEIGHT = {EXACT: 1.0, HEALED: 1.0, APPROXIMATED: 0.5, UNSUPPORTED: 0.0}


# ════════════════════════════════════════════════════════════════════
#  Feature registry
# ════════════════════════════════════════════════════════════════════

@dataclass
class Feature:
    """One tracked Tableau feature and its Power BI coverage."""
    key: str
    category: str
    label: str
    status: str
    target: str
    remediation: str = ""

    def to_dict(self) -> Dict:
        return {k: v for k, v in asdict(self).items()}


# Static registry — the coverage contract. Detectors live in _DETECTORS below.
_FEATURES: List[Feature] = [
    Feature("calc_basic", "Calculations", "Basic calculated field", EXACT,
            "DAX measure/column", "Verify aggregation context in Power BI."),
    Feature("calc_lod", "Calculations", "Level-of-Detail (LOD) expression", HEALED,
            "CALCULATE + ALLEXCEPT/REMOVEFILTERS",
            "Confirm grain matches the FIXED/INCLUDE/EXCLUDE dimensions."),
    Feature("calc_table", "Calculations", "Table calculation", HEALED,
            "Windowed CALCULATE / RANKX / OFFSET",
            "Confirm partition/addressing direction on the visual."),
    Feature("parameters", "Interactivity", "Parameter", EXACT,
            "What-If parameter / field parameter", ""),
    Feature("filters", "Filters", "Filter", EXACT, "Report/page/visual filter", ""),
    Feature("sets", "Data Model", "Set", HEALED, "Boolean calculated column",
            "Cross-table sets fall back to DAX."),
    Feature("groups", "Data Model", "Group", HEALED, "SWITCH calculated column", ""),
    Feature("bins", "Data Model", "Bin", HEALED, "FLOOR calculated column", ""),
    Feature("hierarchies", "Data Model", "Hierarchy", EXACT, "Power BI hierarchy", ""),
    Feature("rls", "Security", "User filter / RLS", HEALED,
            "RLS role (USERPRINCIPALNAME)", "Assign Azure AD members per role."),
    Feature("action_filter", "Interactivity", "Filter/highlight action", EXACT,
            "Cross-filtering / cross-highlighting", ""),
    Feature("action_url", "Interactivity", "URL action", APPROXIMATED,
            "Action button / web URL", "PBI URL actions differ from Tableau's."),
    Feature("action_nav", "Interactivity", "Navigate action", EXACT,
            "Page navigation button", ""),
    Feature("custom_sql", "Datasource", "Custom SQL", EXACT,
            "Power Query native query", "Ensure gateway/credentials for refresh."),
    Feature("data_blending", "Datasource", "Data blending", APPROXIMATED,
            "Model relationship",
            "Blends become relationships; verify direction/cardinality."),
    Feature("extract_hyper", "Datasource", "Hyper extract", HEALED,
            "Import / inlined table", "Choose Import vs DirectQuery per strategy."),
    Feature("trend_line", "Analytics", "Trend line", APPROXIMATED,
            "Analytics-pane trend line", "Regression type may differ."),
    Feature("reference_line", "Analytics", "Reference line", EXACT,
            "Constant line (analytics pane)", ""),
    Feature("forecast", "Analytics", "Forecast", UNSUPPORTED,
            "(no native target)",
            "No native forecast; use a DAX measure or documented manual step."),
    Feature("cluster", "Analytics", "Clustering", UNSUPPORTED,
            "(no native target)",
            "No native clustering; approximate with a DAX/grouping alternative."),
]

_FEATURE_BY_KEY = {f.key: f for f in _FEATURES}


def features_by_category() -> Dict[str, List[Feature]]:
    out: Dict[str, List[Feature]] = {}
    for f in _FEATURES:
        out.setdefault(f.category, []).append(f)
    return out


# ════════════════════════════════════════════════════════════════════
#  Detectors — count in-use instances per feature from extracted objects
# ════════════════════════════════════════════════════════════════════

_LOD_RE = re.compile(r"\{\s*(FIXED|INCLUDE|EXCLUDE)\b", re.IGNORECASE)
_TABLECALC_RE = re.compile(
    r"\b(RUNNING_\w+|WINDOW_\w+|RANK\w*|INDEX\s*\(|SIZE\s*\(|FIRST\s*\(|LAST\s*\("
    r"|LOOKUP\s*\(|TOTAL\s*\(|PREVIOUS_VALUE)\b", re.IGNORECASE)


def _calc_formulas(converted: Dict) -> List[str]:
    out = []
    for c in converted.get("calculations", []) or []:
        if isinstance(c, dict):
            f = c.get("formula")
            if isinstance(f, str):
                out.append(f)
    return out


def _count_lod(converted: Dict) -> int:
    return sum(1 for f in _calc_formulas(converted) if _LOD_RE.search(f))


def _count_tablecalc(converted: Dict) -> int:
    return sum(1 for f in _calc_formulas(converted)
               if _TABLECALC_RE.search(f) and not _LOD_RE.search(f))


def _count_basic_calc(converted: Dict) -> int:
    n = 0
    for f in _calc_formulas(converted):
        if _LOD_RE.search(f) or _TABLECALC_RE.search(f):
            continue
        n += 1
    return n


def _len(key: str) -> Callable[[Dict], int]:
    def detector(converted: Dict) -> int:
        val = converted.get(key)
        if isinstance(val, list):
            return len(val)
        if isinstance(val, dict):
            return len(val)
        return 0
    return detector


def _count_actions(kind: str) -> Callable[[Dict], int]:
    aliases = {
        "filter": ("filter", "highlight"),
        "url": ("url",),
        "nav": ("navigate", "goto", "nav"),
    }[kind]

    def detector(converted: Dict) -> int:
        n = 0
        for a in converted.get("actions", []) or []:
            if not isinstance(a, dict):
                continue
            t = str(a.get("type", a.get("action_type", ""))).lower()
            if any(al in t for al in aliases):
                n += 1
        return n
    return detector


def _count_worksheet_list(key: str) -> Callable[[Dict], int]:
    """Sum the lengths of a per-worksheet list-valued field (e.g. trend_lines).

    This inspects the *value* of a structured worksheet key, NOT the key name —
    so an empty ``forecasting: []`` correctly counts as zero usage.
    """
    def detector(converted: Dict) -> int:
        n = 0
        for ws in converted.get("worksheets", []) or []:
            if not isinstance(ws, dict):
                continue
            val = ws.get(key)
            if isinstance(val, list):
                n += len(val)
            elif val:
                n += 1
        return n
    return detector


_DETECTORS: Dict[str, Callable[[Dict], int]] = {
    "calc_basic": _count_basic_calc,
    "calc_lod": _count_lod,
    "calc_table": _count_tablecalc,
    "parameters": _len("parameters"),
    "filters": _len("filters"),
    "sets": _len("sets"),
    "groups": _len("groups"),
    "bins": _len("bins"),
    "hierarchies": _len("hierarchies"),
    "rls": _len("user_filters"),
    "action_filter": _count_actions("filter"),
    "action_url": _count_actions("url"),
    "action_nav": _count_actions("nav"),
    "custom_sql": _len("custom_sql"),
    "data_blending": _len("data_blending"),
    "extract_hyper": _len("hyper_files"),
    "trend_line": _count_worksheet_list("trend_lines"),
    "reference_line": _count_worksheet_list("reference_lines"),
    "forecast": _count_worksheet_list("forecasting"),
    "cluster": _count_worksheet_list("clustering"),
}


# ════════════════════════════════════════════════════════════════════
#  Scan
# ════════════════════════════════════════════════════════════════════

@dataclass
class FeatureUsage:
    """A single in-use feature with its resolved coverage."""
    key: str
    label: str
    category: str
    status: str
    count: int
    target: str
    remediation: str

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ParityScan:
    """Per-workbook functionality-parity result."""
    workbook: str
    registry_version: str = REGISTRY_VERSION
    usages: List[FeatureUsage] = field(default_factory=list)

    # -- aggregates ---------------------------------------------------
    @property
    def parity_score(self) -> float:
        total = sum(u.count for u in self.usages)
        if total == 0:
            return 100.0
        weighted = sum(_STATUS_WEIGHT.get(u.status, 0.0) * u.count for u in self.usages)
        return round(weighted / total * 100.0, 1)

    @property
    def status_counts(self) -> Dict[str, int]:
        counts = {EXACT: 0, HEALED: 0, APPROXIMATED: 0, UNSUPPORTED: 0}
        for u in self.usages:
            counts[u.status] = counts.get(u.status, 0) + u.count
        return counts

    @property
    def gaps(self) -> List[FeatureUsage]:
        """In-use features that are approximated or unsupported."""
        return [u for u in self.usages if u.status in (APPROXIMATED, UNSUPPORTED)]

    @property
    def unsupported_in_use(self) -> List[FeatureUsage]:
        return [u for u in self.usages if u.status == UNSUPPORTED]

    @property
    def grade(self) -> str:
        s = self.parity_score
        if self.unsupported_in_use:
            return "PARTIAL"
        if s >= 99.0:
            return "FULL"
        if s >= 90.0:
            return "HIGH"
        return "PARTIAL"

    def to_dict(self) -> Dict:
        return {
            "workbook": self.workbook,
            "registry_version": self.registry_version,
            "parity_score": self.parity_score,
            "grade": self.grade,
            "status_counts": self.status_counts,
            "usages": [u.to_dict() for u in self.usages],
            "gaps": [u.to_dict() for u in self.gaps],
        }

    def save_json(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False, default=str)
        return path

    def to_html(self, output_path: Optional[str] = None) -> str:
        html = _render_html(self)
        if output_path:
            with open(output_path, "w", encoding="utf-8") as fh:
                fh.write(html)
        return html


def scan_workbook(converted: Dict, workbook: str = "Workbook") -> ParityScan:
    """Resolve the parity status of every feature in use in ``converted``."""
    converted = converted or {}
    usages: List[FeatureUsage] = []
    for feat in _FEATURES:
        detector = _DETECTORS.get(feat.key)
        if detector is None:
            continue
        try:
            count = int(detector(converted))
        except Exception:  # noqa: BLE001 — detectors are best-effort
            count = 0
        if count <= 0:
            continue
        usages.append(FeatureUsage(
            key=feat.key, label=feat.label, category=feat.category,
            status=feat.status, count=count, target=feat.target,
            remediation=feat.remediation,
        ))
    return ParityScan(workbook=workbook, usages=usages)


# ════════════════════════════════════════════════════════════════════
#  HTML scorecard (self-contained)
# ════════════════════════════════════════════════════════════════════

def _esc(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


_STATUS_COLOR = {EXACT: "#107c10", HEALED: "#0078d4",
                 APPROXIMATED: "#ca5010", UNSUPPORTED: "#a4262c"}


def _render_html(scan: ParityScan) -> str:
    rows = []
    for u in scan.usages:
        color = _STATUS_COLOR.get(u.status, "#605e5c")
        rows.append(
            "<tr>"
            f"<td>{_esc(u.label)}</td>"
            f"<td>{_esc(u.category)}</td>"
            f'<td style="color:{color};font-weight:600">{_esc(u.status)}</td>'
            f"<td style=\"text-align:right\">{u.count}</td>"
            f"<td>{_esc(u.target)}</td>"
            f"<td>{_esc(u.remediation)}</td>"
            "</tr>"
        )
    counts = scan.status_counts
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Parity — {_esc(scan.workbook)}</title>
<style>
 body {{ font-family: Segoe UI, sans-serif; margin: 24px; color: #201f1e; }}
 h1 {{ font-size: 20px; }}
 .score {{ font-size: 34px; font-weight: 700; }}
 table {{ border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 12px; }}
 th, td {{ border: 1px solid #edebe9; padding: 8px; text-align: left; vertical-align: top; }}
 th {{ background: #f3f2f1; }}
</style></head><body>
<h1>Functionality parity — {_esc(scan.workbook)}</h1>
<p class="score">{scan.parity_score}% <span style="font-size:14px">({_esc(scan.grade)})</span></p>
<p>exact {counts[EXACT]} · healed {counts[HEALED]} ·
   approximated {counts[APPROXIMATED]} · unsupported {counts[UNSUPPORTED]}
   · registry v{_esc(scan.registry_version)}</p>
<table><thead><tr>
<th>Feature</th><th>Category</th><th>Status</th><th>Count</th><th>Target</th><th>Remediation</th>
</tr></thead><tbody>
{''.join(rows)}
</tbody></table>
</body></html>"""
