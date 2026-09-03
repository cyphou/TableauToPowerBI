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


def _check_pbip_contract(project_dir) -> CheckResult:
    """Validate the required shell files for a standard PBIP project."""
    fabric_suffixes = ("Lakehouse", "Dataflow", "Notebook", "Pipeline")
    if any(glob.glob(os.path.join(project_dir, f"*.{suffix}"))
           for suffix in fabric_suffixes):
        return CheckResult("pbip_contract", True, "error", [])

    report_dirs = glob.glob(os.path.join(project_dir, "*.Report"))
    model_dirs = glob.glob(os.path.join(project_dir, "*.SemanticModel"))
    if not report_dirs and not model_dirs:
        return CheckResult("pbip_contract", True, "error", [])

    issues = []
    names = {os.path.basename(path).rsplit(".", 1)[0]
             for path in report_dirs + model_dirs}
    if len(names) != 1:
        issues.append("Report and SemanticModel names do not match")
        return CheckResult("pbip_contract", False, "error", issues)
    name = next(iter(names))
    required = [
        f"{name}.pbip",
        f"{name}.Report/.platform",
        f"{name}.Report/definition.pbir",
        f"{name}.Report/definition/version.json",
        f"{name}.Report/definition/report.json",
        f"{name}.Report/definition/pages/pages.json",
        f"{name}.SemanticModel/.platform",
        f"{name}.SemanticModel/definition.pbism",
        f"{name}.SemanticModel/definition/model.tmdl",
        f"{name}.SemanticModel/definition/database.tmdl",
        f"{name}.SemanticModel/definition/expressions.tmdl",
    ]
    for relative in required:
        if not os.path.isfile(os.path.join(project_dir, *relative.split("/"))):
            issues.append(f"missing required PBIP artifact: {relative}")
    tables_dir = os.path.join(project_dir, f"{name}.SemanticModel",
                              "definition", "tables")
    if not glob.glob(os.path.join(tables_dir, "*.tmdl")):
        issues.append("missing required PBIP artifact: SemanticModel/definition/tables/*.tmdl")
    return CheckResult("pbip_contract", not issues, "error", issues)


def _check_generated_content(project_dir) -> CheckResult:
    """Reject empty or invalid required semantic-model definition files."""
    try:
        from powerbi_import.validator import ArtifactValidator
    except Exception as exc:  # noqa: BLE001 - preflight must return a check result
        return CheckResult("generated_content", False, "error", [str(exc)])
    issues = []
    for model_dir in glob.glob(os.path.join(project_dir, "*.SemanticModel")):
        model_path = os.path.join(model_dir, "definition", "model.tmdl")
        if not os.path.isfile(model_path):
            continue
        valid, errors = ArtifactValidator.validate_tmdl_file(model_path)
        if not valid:
            issues.extend(f"{os.path.relpath(model_path, project_dir)}: {error}"
                          for error in errors)
    return CheckResult("generated_content", not issues, "error", issues)


def _check_report_content(project_dir) -> CheckResult:
    """Reject parseable but empty PBIR report/page/visual definitions."""
    issues = []
    report_dirs = glob.glob(os.path.join(project_dir, "*.Report"))
    helper_types = {"textbox", "image", "actionButton", "shape", "basicShape"}
    for report_dir in report_dirs:
        definition = os.path.join(report_dir, "definition")
        report_path = os.path.join(definition, "report.json")
        report = _read_json(report_path)
        if not isinstance(report, dict) or not report:
            issues.append(f"{os.path.relpath(report_path, project_dir)}: empty report definition")
        for page_path in glob.glob(os.path.join(definition, "pages", "*", "page.json")):
            page = _read_json(page_path)
            if not isinstance(page, dict) or not page:
                issues.append(f"{os.path.relpath(page_path, project_dir)}: empty page definition")
        for visual_path in glob.glob(os.path.join(
                definition, "pages", "*", "visuals", "*", "visual.json")):
            visual_json = _read_json(visual_path)
            visual = visual_json.get("visual") if isinstance(visual_json, dict) else None
            visual_type = visual.get("visualType") if isinstance(visual, dict) else None
            if not isinstance(visual_json, dict) or not visual_json or not isinstance(visual, dict):
                issues.append(f"{os.path.relpath(visual_path, project_dir)}: empty visual definition")
            elif not visual_type and not (visual_json.get("type") in helper_types):
                # Missing visualType remains a warning when the visual has a
                # meaningful definition; only a truly empty visual is blocking.
                if not visual:
                    issues.append(f"{os.path.relpath(visual_path, project_dir)}: empty visual definition")
    return CheckResult("report_content", not issues, "error", issues)


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


def _check_semantic_validation(project_dir) -> CheckResult:
    """Block unresolved model references that syntax-only DAX checks miss."""
    try:
        from powerbi_import.validator import ArtifactValidator
    except Exception as exc:  # noqa: BLE001 - preflight must return a check result
        return CheckResult("semantic_validation", False, "error", [str(exc)])
    issues = []
    model_dirs = glob.glob(os.path.join(project_dir, "*.SemanticModel"))
    for model_dir in model_dirs:
        for issue in ArtifactValidator.validate_semantic_references(model_dir):
            if "Unknown column/measure" in issue:
                issues.append(issue)
        for issue in ArtifactValidator.validate_relationship_columns(model_dir):
            if "not found in table" in issue:
                issues.append(issue)
    return CheckResult("semantic_validation", not issues, "error", issues)


def _check_executable_tmdl_dax(project_dir) -> CheckResult:
    """Validate DAX beyond measures, including columns and RLS definitions."""
    try:
        from powerbi_import.validator import ArtifactValidator
    except Exception as exc:  # noqa: BLE001 - preflight must return a check result
        return CheckResult("executable_dax", False, "error", [str(exc)])
    issues = []
    paths = glob.glob(os.path.join(
        project_dir, "*.SemanticModel", "definition", "tables", "*.tmdl"))
    paths.extend(glob.glob(os.path.join(
        project_dir, "*.SemanticModel", "definition", "roles.tmdl")))
    for path in paths:
        for issue in ArtifactValidator.validate_tmdl_dax(path):
            issues.append(f"{os.path.relpath(path, project_dir)}: {issue}")
        try:
            lines = _read(path).splitlines()
        except OSError:
            continue
        for line in lines:
            inline = re.match(r"^\s*column\s+(.+?)\s*=\s*(.+?)\s*$", line)
            if inline:
                for issue in ArtifactValidator.validate_dax_formula(
                        inline.group(2), f"column {inline.group(1)}"):
                    issues.append(f"{os.path.relpath(path, project_dir)}: {issue}")
            role_expr = re.match(r"^\s*filterExpression\s*=\s*(.+?)\s*$", line)
            if role_expr:
                for issue in ArtifactValidator.validate_dax_formula(
                        role_expr.group(1), "RLS filterExpression"):
                    issues.append(f"{os.path.relpath(path, project_dir)}: {issue}")
        partition_name = ""
        calculated_partition = False
        for index, line in enumerate(lines):
            partition = _PARTITION_RE.match(line)
            if partition:
                partition_name = partition.group(1).strip().strip("'")
                calculated_partition = partition.group(2) == "calculated"
                continue
            if not calculated_partition or not re.match(r"^\t\tsource\s*=", line):
                continue
            expression = line.split("=", 1)[1].strip()
            if expression == "```":
                body = []
                for body_line in lines[index + 1:]:
                    if body_line.strip() == "```":
                        break
                    body.append(body_line.strip())
                expression = "\n".join(body)
            if expression and not expression.startswith("let"):
                for issue in ArtifactValidator.validate_dax_formula(
                        expression, f"calculated partition {partition_name}"):
                    issues.append(f"{os.path.relpath(path, project_dir)}: {issue}")
            calculated_partition = False
    return CheckResult("executable_dax", not issues, "error", issues)


def _check_visual_bindings(project_dir) -> CheckResult:
    """Block PBIR visuals that reference missing model fields."""
    try:
        from powerbi_import.artifact_diff import _parse_tmdl_table
        from powerbi_import.cross_validator import _check_visual_refs, _build_model_index
        from powerbi_import.self_healing_report import load_report
    except Exception as exc:  # noqa: BLE001 - preflight must return a check result
        return CheckResult("visual_bindings", False, "error", [str(exc)])

    model = {"model": {"tables": []}}
    for model_dir in glob.glob(os.path.join(project_dir, "*.SemanticModel")):
        for path in glob.glob(os.path.join(model_dir, "definition", "tables", "*.tmdl")):
            parsed = _parse_tmdl_table(path)
            if parsed:
                model["model"]["tables"].append(parsed)
    if not model["model"]["tables"]:
        return CheckResult("visual_bindings", True, "error", [])

    report_dirs = glob.glob(os.path.join(project_dir, "*.Report"))
    if not report_dirs:
        return CheckResult("visual_bindings", True, "error", [])
    report_state = load_report(report_dirs[0])
    if not report_state:
        return CheckResult("visual_bindings", False, "error",
                           ["report state could not be loaded"])
    table_names, columns, measures = _build_model_index(model)
    issues = _check_visual_refs(report_state, table_names, columns, measures)
    return CheckResult("visual_bindings", not issues, "error", [
        f"{issue.location}: {issue.message}" for issue in issues
    ])


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
        _check_pbip_contract(project_dir),
        _check_generated_content(project_dir),
        _check_report_content(project_dir),
        _check_json_parse(project_dir),
        _check_tmdl_present(project_dir),
        _check_tmdl_partitions(project_dir),
        _check_power_query(project_dir),
        _check_dax(project_dir),
        _check_semantic_validation(project_dir),
        _check_executable_tmdl_dax(project_dir),
        _check_visual_bindings(project_dir),
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
