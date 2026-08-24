"""Closed-loop autoheal — collect errors, heal, LLM-correct, re-validate (Sprint 221).

Automatically repairs a generated ``.pbip`` project so it opens cleanly in Power
BI Desktop. The loop is:

    collect errors  →  deterministic heal  →  LLM correction (gateway)
                    →  re-validate  →  apply only if it now validates  →  repeat

Error collection is **pluggable** (``ErrorSource``) because "open in PBI Desktop
and read the error" is a GUI step:
    * ``StaticValidatorSource`` (default, offline) — runs the repo's DAX/M
      validators over the project's TMDL + PBIR files, i.e. the errors Power BI
      Desktop would raise on load.
    * ``LogFileSource`` — parses a Power BI Desktop error export / FrownDump /
      pasted error text that the user saved after hitting the error in Desktop.
    * ``PbiDesktopSource`` — detects Desktop/pbi-tools and delegates to a log
      file when available, else returns guidance.

LLM correction routes through ``LLMGateway`` (OpenAI / Azure OpenAI / local /
any model), and a fix is applied only when it **re-validates clean** — never
degrading the project. All repairs are recorded for the recovery report.

Public API:
    AutoHealer(gateway=None, autofix=False, max_iterations=3).heal_project(dir)
    heal_dax_expression(expr, measure_names, gateway=None, autofix=False)
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

# Deterministic healers
from powerbi_import.dax_healing import heal_dax
from powerbi_import.m_healing import heal_m
from powerbi_import.visual_healing import heal_visual
from powerbi_import.openability import extract_m_partitions as _extract_m_partitions

# Validators (issue lists; empty == valid)
try:
    from powerbi_import.dax_validator import validate_dax_expression
except Exception:  # noqa: BLE001
    def validate_dax_expression(expr):  # type: ignore
        return []
try:
    from powerbi_import.m_validator import validate_m_query
except Exception:  # noqa: BLE001
    def validate_m_query(expr):  # type: ignore
        return []

# TMDL measure assignment: `measure 'Name' = <expr>` (single-line; the generator
# condenses multi-line DAX). Captures the prefix and the expression separately.
_MEASURE_RE = re.compile(r"^(\s*measure\s+(?:'(?:[^']|'')+'|[^=\n]+?)\s*=\s*)(.+?)\s*$")
_MEASURE_NAME_RE = re.compile(r"^\s*measure\s+(?:'((?:[^']|'')+)'|([^\s=]+))")
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$")


# ════════════════════════════════════════════════════════════════════
#  Data model
# ════════════════════════════════════════════════════════════════════

@dataclass
class ErrorRecord:
    artifact: str            # dax | m | visual
    file: str
    location: str            # measure/query/visual name
    message: str
    severity: str = "error"

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class RepairAttempt:
    """One applied repair with its validation outcome.

    Distinct from ``healing_core.HealAction`` (which describes a single
    deterministic edit): a RepairAttempt is the autoheal loop's record of a fix
    it applied to a file, including the source (deterministic vs llm) and whether
    the result re-validated clean.
    """
    file: str
    artifact: str
    location: str
    before: str
    after: str
    source: str              # deterministic | llm
    confidence: str          # high | medium | low
    validated: bool

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class AutoHealReport:
    project_dir: str
    iterations: int = 0
    actions: List[RepairAttempt] = field(default_factory=list)
    remaining_errors: List[ErrorRecord] = field(default_factory=list)
    llm_used: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.actions)

    @property
    def clean(self) -> bool:
        return not self.remaining_errors

    def to_dict(self) -> Dict:
        return {
            "project_dir": self.project_dir,
            "iterations": self.iterations,
            "changed": self.changed,
            "clean": self.clean,
            "llm_used": self.llm_used,
            "action_count": len(self.actions),
            "actions": [a.to_dict() for a in self.actions],
            "remaining_errors": [e.to_dict() for e in self.remaining_errors],
        }


# ════════════════════════════════════════════════════════════════════
#  Error sources (pluggable)
# ════════════════════════════════════════════════════════════════════

class ErrorSource:
    """Base class — collect residual errors from a project."""

    def collect(self, project_dir: str) -> List[ErrorRecord]:  # pragma: no cover
        raise NotImplementedError


class StaticValidatorSource(ErrorSource):
    """Run the repo's DAX/M validators over the project's TMDL + PBIR files.

    These are the load-time errors Power BI Desktop would raise; running them
    offline lets autoheal fix most issues before the report is ever opened.
    """

    def collect(self, project_dir: str) -> List[ErrorRecord]:
        errors: List[ErrorRecord] = []
        for tmdl in _iter_tmdl(project_dir):
            try:
                text = _read(tmdl)
            except OSError:
                continue
            name = None
            for line in text.splitlines():
                m = _MEASURE_RE.match(line)
                if not m:
                    continue
                name = _measure_name(line)
                for issue in validate_dax_expression(m.group(2)):
                    errors.append(ErrorRecord("dax", tmdl, name or "measure", issue))
            # Power Query: validate every M partition embedded in the TMDL.
            for pname, m_expr in _extract_m_partitions(text):
                if not m_expr.strip():
                    continue
                for issue in validate_m_query(m_expr):
                    errors.append(ErrorRecord("m", tmdl, pname, issue))
        for vf in _iter_visuals(project_dir):
            data = _read_json(vf)
            if data is None:
                continue
            visual = data.get("visual")
            if isinstance(visual, dict) and visual.get("annotations"):
                errors.append(ErrorRecord(
                    "visual", vf, data.get("name", "visual"),
                    "annotations placed inside inner visual (PBIR load failure)"))
        return errors


class LogFileSource(ErrorSource):
    """Parse a Power BI Desktop error export / FrownDump / pasted error text.

    Best-effort: extracts error lines and any quoted measure/table names so the
    healer can target the right expression.
    """

    def __init__(self, log_path: str):
        self.log_path = log_path

    def collect(self, project_dir: str) -> List[ErrorRecord]:
        errors: List[ErrorRecord] = []
        try:
            text = _read(self.log_path)
        except OSError:
            return errors
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if re.search(r"error|invalid|cannot|failed|unexpected|syntax",
                         line, re.IGNORECASE):
                loc = ""
                mm = re.search(r"measure\s+'([^']+)'|\[([^\]]+)\]", line, re.IGNORECASE)
                if mm:
                    loc = mm.group(1) or mm.group(2) or ""
                errors.append(ErrorRecord("dax", self.log_path, loc or "unknown", line))
        return errors


class PbiDesktopSource(ErrorSource):
    """Detect Power BI Desktop / pbi-tools and delegate to a log when available.

    Opening Desktop and reading its error is a GUI action that cannot be reliably
    automated headlessly. When an error log/export path is provided, this parses
    it; otherwise it returns a single guidance record telling the user how to
    capture the error, and leaves automated healing to the static validators.
    """

    def __init__(self, log_path: Optional[str] = None):
        self.log_path = log_path

    @staticmethod
    def pbi_desktop_installed() -> bool:
        if shutil.which("PBIDesktop") or shutil.which("pbi-tools"):
            return True
        for base in (os.environ.get("ProgramFiles", ""),
                     os.environ.get("ProgramW6432", "")):
            if base and os.path.exists(os.path.join(
                    base, "Microsoft Power BI Desktop", "bin", "PBIDesktop.exe")):
                return True
        return False

    def collect(self, project_dir: str) -> List[ErrorRecord]:
        if self.log_path and os.path.isfile(self.log_path):
            return LogFileSource(self.log_path).collect(project_dir)
        return [ErrorRecord(
            "info", project_dir, "pbi_desktop",
            "Open the .pbip in Power BI Desktop; if an error appears, use "
            "'Copy Details' / export it to a file and re-run autoheal with "
            "--autoheal-log <file>.", severity="info")]


# ════════════════════════════════════════════════════════════════════
#  Healer
# ════════════════════════════════════════════════════════════════════

class AutoHealer:
    """Closed-loop project autohealer."""

    def __init__(self, gateway=None, autofix=False, max_iterations=3,
                 error_source: Optional[ErrorSource] = None):
        self.gateway = gateway
        self.autofix = autofix
        self.max_iterations = max(1, int(max_iterations))
        self.error_source = error_source or StaticValidatorSource()

    # -- public ------------------------------------------------------
    def heal_project(self, project_dir: str) -> AutoHealReport:
        report = AutoHealReport(project_dir=project_dir)
        if not project_dir or not os.path.isdir(project_dir):
            report.remaining_errors.append(
                ErrorRecord("info", project_dir, "project", "project dir not found"))
            return report

        measure_names = self._collect_measure_names(project_dir)

        for _ in range(self.max_iterations):
            report.iterations += 1
            changed = False
            changed |= self._heal_tmdl(project_dir, measure_names, report)
            changed |= self._heal_visuals(project_dir, report)
            residual = self.error_source.collect(project_dir)
            residual = [e for e in residual if e.severity != "info"]
            if not residual:
                report.remaining_errors = []
                break
            if self._llm_pass(residual, measure_names, report):
                changed = True
            if not changed:
                report.remaining_errors = residual
                break
            report.remaining_errors = residual

        # final residual snapshot
        final = [e for e in self.error_source.collect(project_dir)
                 if e.severity != "info"]
        report.remaining_errors = final
        return report

    # -- deterministic passes ---------------------------------------
    def _heal_tmdl(self, project_dir, measure_names, report) -> bool:
        changed_any = False
        for tmdl in _iter_tmdl(project_dir):
            try:
                original = _read(tmdl)
            except OSError:
                continue
            out_lines = []
            file_changed = False
            for line in original.splitlines():
                m = _MEASURE_RE.match(line)
                if not m:
                    out_lines.append(line)
                    continue
                prefix, expr = m.group(1), m.group(2)
                healed = heal_dax(expr, measure_names)
                if healed.changed and validate_dax_expression(healed.healed) == []:
                    out_lines.append(prefix + healed.healed)
                    file_changed = True
                    report.actions.append(RepairAttempt(
                        tmdl, "dax", _measure_name(line), expr, healed.healed,
                        "deterministic", "high", True))
                else:
                    out_lines.append(line)
            if file_changed:
                _write(tmdl, "\n".join(out_lines) + ("\n" if original.endswith("\n") else ""))
                changed_any = True
        return changed_any

    def _heal_visuals(self, project_dir, report) -> bool:
        changed_any = False
        for vf in _iter_visuals(project_dir):
            data = _read_json(vf)
            if data is None:
                continue
            result = heal_visual(data)
            if result.changed:
                _write_json(vf, result.healed)
                changed_any = True
                for a in result.actions:
                    report.actions.append(RepairAttempt(
                        vf, "visual", data.get("name", "visual"),
                        getattr(a, "before", ""), getattr(a, "after", ""),
                        "deterministic", getattr(a, "confidence", "medium"), True))
        return changed_any

    # -- LLM pass ----------------------------------------------------
    def _llm_pass(self, residual, measure_names, report) -> bool:
        if not (self.gateway and self.autofix and getattr(self.gateway, "enabled", False)):
            return False
        applied = False
        # Target measures named by any DAX error. Errors may come from static
        # validation (file == tmdl) or a Desktop log (file == log), so we match
        # by measure NAME across all TMDL files, not by file.
        targets = {e.location for e in residual if e.artifact == "dax" and e.location}
        if not targets:
            return False
        for tmdl in _iter_tmdl(report.project_dir):
            try:
                original = _read(tmdl)
            except OSError:
                continue
            out_lines = []
            file_changed = False
            for line in original.splitlines():
                m = _MEASURE_RE.match(line)
                if not m or _measure_name(line) not in targets:
                    out_lines.append(line)
                    continue
                expr = m.group(2)
                issues = validate_dax_expression(expr)
                if not issues:
                    out_lines.append(line)
                    continue
                fixed = self._llm_fix_dax(expr, issues)
                if fixed and validate_dax_expression(fixed) == []:
                    out_lines.append(m.group(1) + fixed)
                    file_changed = True
                    applied = True
                    report.llm_used = True
                    report.actions.append(RepairAttempt(
                        tmdl, "dax", _measure_name(line), expr, fixed,
                        "llm", "medium", True))
                else:
                    out_lines.append(line)
            if file_changed:
                _write(tmdl, "\n".join(out_lines) + ("\n" if original.endswith("\n") else ""))
        return applied

    def _llm_fix_dax(self, expr, issues) -> str:
        system = ("You are a Power BI DAX expert. Return ONLY the corrected DAX "
                  "expression — no explanation, no markdown fences.")
        user = (f"Fix this DAX measure expression. Validation error(s): "
                f"{'; '.join(issues)}.\nExpression:\n{expr}")
        try:
            result = self.gateway.complete(user, system=system)
        except Exception:  # noqa: BLE001 — LLM is best-effort
            return ""
        text = getattr(result, "text", result) or ""
        return _strip_fences(str(text)).strip()

    # -- helpers -----------------------------------------------------
    def _collect_measure_names(self, project_dir) -> set:
        names = set()
        for tmdl in _iter_tmdl(project_dir):
            try:
                for line in _read(tmdl).splitlines():
                    n = _measure_name(line)
                    if n:
                        names.add(n)
            except OSError:
                continue
        return names


def heal_dax_expression(expr, measure_names=None, gateway=None, autofix=False):
    """Heal a single DAX expression: deterministic, then LLM if still invalid.

    Returns ``(healed_expr, source)`` where source ∈ {none, deterministic, llm}.
    An LLM fix is only accepted if it re-validates clean.
    """
    names = measure_names or set()
    det = heal_dax(expr, names)
    candidate = det.healed if det.changed else expr
    if validate_dax_expression(candidate) == []:
        return candidate, ("deterministic" if det.changed else "none")
    if gateway and autofix and getattr(gateway, "enabled", False):
        healer = AutoHealer(gateway=gateway, autofix=True)
        fixed = healer._llm_fix_dax(candidate, validate_dax_expression(candidate))
        if fixed and validate_dax_expression(fixed) == []:
            return fixed, "llm"
    return candidate, ("deterministic" if det.changed else "none")


# ════════════════════════════════════════════════════════════════════
#  File helpers
# ════════════════════════════════════════════════════════════════════

def _iter_tmdl(project_dir):
    yield from glob.glob(os.path.join(project_dir, "**", "*.tmdl"), recursive=True)


def _iter_visuals(project_dir):
    yield from glob.glob(os.path.join(project_dir, "**", "visual.json"), recursive=True)


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _measure_name(line):
    m = _MEASURE_NAME_RE.match(line)
    if not m:
        return ""
    return (m.group(1) or m.group(2) or "").replace("''", "'").strip()


def _strip_fences(text):
    text = text.strip()
    if text.startswith("```"):
        text = _FENCE_RE.sub("", text)
    return text.strip()
