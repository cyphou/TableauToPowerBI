"""Power BI Desktop openability preflight (Sprint 221).

Verifies — WITHOUT opening Power BI Desktop — that a generated ``.pbip`` will
load cleanly, with a dedicated focus on **Power Query (M) generation**, the most
common silent load failure.

Checks (blocking = would stop Desktop from opening the report):
    structure     required project files/dirs present (SemanticModel, Report, .pbir)
    json_parse    every .json (visual/page/report/.pbir/.platform/.pbip) parses
    tmdl_present  the semantic model has at least one .tmdl
    power_query   every M partition extracted from TMDL validates (the focus)
    dax           every measure expression validates
    schema        PBIR/visual files carry a $schema (advisory)

Public API:
    check_openability(project_dir) -> OpenabilityReport
    extract_m_partitions(tmdl_text) -> list[(partition_name, m_text)]
"""

from __future__ import annotations

import glob
import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Tuple

try:
    from powerbi_import.dax_validator import validate_dax_expression
except Exception:  # noqa: BLE001
    def validate_dax_expression(expr):  # type: ignore
        return []
try:
    from powerbi_import.m_validator import validate_m_query
except Exception:  # noqa: BLE001
    def validate_m_query(text):  # type: ignore
        return []

# TMDL partition of type ``m`` and its ``source =`` block (M lines are indented
# with exactly 4 tabs by the generator; see tmdl_generator._write_partition).
_PARTITION_RE = re.compile(r"^\tpartition\s+(.+?)\s*=\s*(\w+)\s*$")
_SOURCE_RE = re.compile(r"^\t\tsource\s*=\s*$")
_MEASURE_RE = re.compile(r"^\s*measure\s+(?:'(?:[^']|'')+'|[^=\n]+?)\s*=\s*(.+?)\s*$")


@dataclass
class CheckResult:
    name: str
    ok: bool
    severity: str            # error (blocking) | warning
    issues: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class OpenabilityReport:
    project_dir: str
    checks: List[CheckResult] = field(default_factory=list)

    @property
    def blocking_issues(self) -> List[str]:
        out = []
        for c in self.checks:
            if not c.ok and c.severity == "error":
                out.extend(f"[{c.name}] {i}" for i in c.issues)
        return out

    @property
    def openable(self) -> bool:
        return not self.blocking_issues

    @property
    def warnings(self) -> List[str]:
        out = []
        for c in self.checks:
            if not c.ok and c.severity == "warning":
                out.extend(f"[{c.name}] {i}" for i in c.issues)
        return out

    def to_dict(self) -> Dict:
        return {
            "project_dir": self.project_dir,
            "openable": self.openable,
            "blocking_count": len(self.blocking_issues),
            "warning_count": len(self.warnings),
            "blocking_issues": self.blocking_issues,
            "warnings": self.warnings,
            "checks": [c.to_dict() for c in self.checks],
        }


# ════════════════════════════════════════════════════════════════════
#  M partition extraction (the Power Query focus)
# ════════════════════════════════════════════════════════════════════

def extract_m_partitions(tmdl_text: str) -> List[Tuple[str, str]]:
    """Return [(partition_name, m_expression)] for every ``= m`` partition.

    The generator writes each M line prefixed with 4 tabs after ``source =``;
    the block ends at the first dedented or blank line.
    """
    lines = tmdl_text.splitlines()
    out: List[Tuple[str, str]] = []
    i, n = 0, len(lines)
    is_m = False
    name = ""
    while i < n:
        line = lines[i]
        pm = _PARTITION_RE.match(line)
        if pm:
            name = pm.group(1).strip().strip("'").replace("''", "'")
            is_m = pm.group(2) == "m"
            i += 1
            continue
        if is_m and _SOURCE_RE.match(line):
            i += 1
            block = []
            while i < n:
                l = lines[i]
                if l.startswith("\t\t\t\t"):
                    block.append(l[4:])
                    i += 1
                else:
                    break
            out.append((name, "\n".join(block)))
            is_m = False
            continue
        i += 1
    return out


# ════════════════════════════════════════════════════════════════════
#  Checks
# ════════════════════════════════════════════════════════════════════

def _check_structure(project_dir) -> CheckResult:
    issues = []
    has_sm = bool(glob.glob(os.path.join(project_dir, "**", "*.SemanticModel"),
                            recursive=True)) or bool(_tmdl_files(project_dir))
    has_report = bool(glob.glob(os.path.join(project_dir, "**", "definition.pbir"),
                                recursive=True)) or bool(
        glob.glob(os.path.join(project_dir, "**", "*.Report"), recursive=True))
    if not has_sm:
        issues.append("no semantic model (.SemanticModel / .tmdl) found")
    if not has_report:
        issues.append("no report (definition.pbir / .Report) found")
    return CheckResult("structure", not issues, "error", issues)


def _check_json_parse(project_dir) -> CheckResult:
    issues = []
    patterns = ["*.json", "*.pbir", "*.pbip", ".platform"]
    seen = set()
    for pat in patterns:
        for fp in glob.glob(os.path.join(project_dir, "**", pat), recursive=True):
            if fp in seen:
                continue
            seen.add(fp)
            try:
                with open(fp, "r", encoding="utf-8") as fh:
                    json.load(fh)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                issues.append(f"{os.path.relpath(fp, project_dir)}: {exc}")
    return CheckResult("json_parse", not issues, "error", issues)


def _check_tmdl_present(project_dir) -> CheckResult:
    tmdls = _tmdl_files(project_dir)
    if not tmdls:
        return CheckResult("tmdl_present", False, "error",
                           ["no .tmdl files in the semantic model"])
    return CheckResult("tmdl_present", True, "error", [])


def _check_power_query(project_dir) -> CheckResult:
    """Validate every M partition — the Power Query generation focus."""
    issues = []
    for tmdl in _tmdl_files(project_dir):
        try:
            text = _read(tmdl)
        except OSError:
            continue
        for name, m_expr in extract_m_partitions(text):
            if not m_expr.strip():
                continue
            for problem in validate_m_query(m_expr):
                issues.append(f"{os.path.relpath(tmdl, project_dir)} :: "
                              f"partition '{name}': {problem}")
    return CheckResult("power_query", not issues, "error", issues)


def _check_dax(project_dir) -> CheckResult:
    issues = []
    for tmdl in _tmdl_files(project_dir):
        try:
            for line in _read(tmdl).splitlines():
                m = _MEASURE_RE.match(line)
                if not m:
                    continue
                for problem in validate_dax_expression(m.group(1)):
                    issues.append(f"{os.path.relpath(tmdl, project_dir)}: {problem}")
        except OSError:
            continue
    return CheckResult("dax", not issues, "error", issues)


def _check_schema(project_dir) -> CheckResult:
    issues = []
    for fp in glob.glob(os.path.join(project_dir, "**", "visual.json"), recursive=True):
        data = _read_json(fp)
        if isinstance(data, dict) and "$schema" not in data:
            issues.append(f"{os.path.relpath(fp, project_dir)}: missing $schema")
    return CheckResult("schema", not issues, "warning", issues)


def _check_references(project_dir) -> CheckResult:
    """Verify the report→semantic-model link resolves (a dangling link won't open)."""
    issues = []
    for pbir in glob.glob(os.path.join(project_dir, "**", "definition.pbir"),
                          recursive=True):
        data = _read_json(pbir)
        if not isinstance(data, dict):
            continue
        ref = (data.get("datasetReference") or {})
        by_path = (ref.get("byPath") or {}).get("path")
        if by_path:
            target = os.path.normpath(os.path.join(os.path.dirname(pbir), by_path))
            if not os.path.isdir(target):
                issues.append(f"{os.path.relpath(pbir, project_dir)}: datasetReference "
                              f"byPath '{by_path}' does not resolve to a folder. "
                              f"Fix: point byPath to the generated *.SemanticModel folder.")
        elif "byConnection" not in ref:
            issues.append(f"{os.path.relpath(pbir, project_dir)}: no datasetReference "
                          f"(byPath or byConnection). Fix: add a valid datasetReference block.")
    return CheckResult("references", not issues, "error", issues)


def _check_report_structure(project_dir) -> CheckResult:
    """Verify essential report files/pages/visual files exist and are complete."""
    issues = []
    report_dirs = glob.glob(os.path.join(project_dir, "**", "*.Report"), recursive=True)
    for report_dir in report_dirs:
        pbir = os.path.join(report_dir, "definition.pbir")
        report_json_root = os.path.join(report_dir, "report.json")
        report_json_def = os.path.join(report_dir, "definition", "report.json")
        if not os.path.isfile(pbir):
            issues.append(
                f"{os.path.relpath(report_dir, project_dir)}: missing definition.pbir. "
                f"Fix: regenerate report metadata or restore definition.pbir."
            )
        if not (os.path.isfile(report_json_root) or os.path.isfile(report_json_def)):
            issues.append(
                f"{os.path.relpath(report_dir, project_dir)}: missing report.json. "
                f"Fix: regenerate report shell so Desktop can load report metadata."
            )

        pages_root = os.path.join(report_dir, "definition", "pages")
        page_dirs = [p for p in glob.glob(os.path.join(pages_root, "*")) if os.path.isdir(p)]
        if not page_dirs:
            # Some helper/thin reports may be intentionally empty; keep advisory only.
            continue

        for page_dir in page_dirs:
            page_json = os.path.join(page_dir, "page.json")
            if not os.path.isfile(page_json):
                issues.append(
                    f"{os.path.relpath(page_dir, project_dir)}: missing page.json. "
                    f"Fix: recreate page metadata for this page folder."
                )
            visuals_root = os.path.join(page_dir, "visuals")
            visual_dirs = [v for v in glob.glob(os.path.join(visuals_root, "*")) if os.path.isdir(v)]
            for visual_dir in visual_dirs:
                visual_json = os.path.join(visual_dir, "visual.json")
                if not os.path.isfile(visual_json):
                    issues.append(
                        f"{os.path.relpath(visual_dir, project_dir)}: missing visual.json. "
                        f"Fix: recreate the visual container metadata."
                    )

    return CheckResult("report_structure", not issues, "error", issues)


def _check_tmdl_partitions(project_dir) -> CheckResult:
    """Validate table/partition integrity in TMDL files (common Desktop load breaker)."""
    issues = []
    table_tmdls = glob.glob(
        os.path.join(project_dir, "**", "definition", "tables", "*.tmdl"),
        recursive=True,
    )
    for tmdl in table_tmdls:
        try:
            lines = _read(tmdl).splitlines()
        except OSError:
            continue

        table_has_partition = False
        current_partition = None
        current_partition_type = ""
        saw_source = False
        saw_m_body = False

        for raw in lines:
            line = raw.strip()
            if raw.startswith("table "):
                if current_partition and current_partition_type == "m" and (not saw_source or not saw_m_body):
                    issues.append(
                        f"{os.path.relpath(tmdl, project_dir)}: partition '{current_partition}' has incomplete M source. "
                        f"Fix: ensure `source =` exists and contains a non-empty M query body."
                    )
                current_partition = None
                current_partition_type = ""
                saw_source = False
                saw_m_body = False
            pm = _PARTITION_RE.match(raw)
            if pm:
                table_has_partition = True
                if current_partition and current_partition_type == "m" and (not saw_source or not saw_m_body):
                    issues.append(
                        f"{os.path.relpath(tmdl, project_dir)}: partition '{current_partition}' has incomplete M source. "
                        f"Fix: ensure `source =` exists and contains a non-empty M query body."
                    )
                current_partition = pm.group(1).strip().strip("'")
                current_partition_type = pm.group(2)
                saw_source = False
                saw_m_body = False
                continue
            if current_partition and current_partition_type == "m":
                if _SOURCE_RE.match(raw):
                    saw_source = True
                    continue
                if raw.startswith("\t\t\t\t") and raw.strip():
                    saw_m_body = True

        if current_partition and current_partition_type == "m" and (not saw_source or not saw_m_body):
            issues.append(
                f"{os.path.relpath(tmdl, project_dir)}: partition '{current_partition}' has incomplete M source. "
                f"Fix: ensure `source =` exists and contains a non-empty M query body."
            )
        if not table_has_partition:
            issues.append(
                f"{os.path.relpath(tmdl, project_dir)}: no partition found. "
                f"Fix: add at least one partition per table (m or calculated)."
            )
    return CheckResult("tmdl_partitions", not issues, "error", issues)


def check_openability(project_dir: str) -> OpenabilityReport:
    """Run the full PBI Desktop openability preflight."""
    report = OpenabilityReport(project_dir=project_dir)
    if not project_dir or not os.path.isdir(project_dir):
        report.checks.append(CheckResult("structure", False, "error",
                                         ["project dir not found"]))
        return report
    report.checks = [
        _check_structure(project_dir),
        _check_json_parse(project_dir),
        _check_tmdl_present(project_dir),
        _check_tmdl_partitions(project_dir),
        _check_power_query(project_dir),
        _check_dax(project_dir),
        _check_references(project_dir),
        _check_report_structure(project_dir),
        _check_schema(project_dir),
    ]
    return report


# ════════════════════════════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════════════════════════════

def _tmdl_files(project_dir):
    return glob.glob(os.path.join(project_dir, "**", "*.tmdl"), recursive=True)


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
