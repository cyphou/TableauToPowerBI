"""Unified migration quality report.

Combines the existing deterministic assessment, parity, artifact comparison,
interface, and openability checks into one machine-readable quality contract.
The report is deliberately deterministic so an optional AI summary can be
based on verified findings rather than replacing validation logic.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from powerbi_import.assessment import run_assessment
from powerbi_import.html_template import (
    esc,
    html_close,
    html_open,
    section_close,
    section_open,
    stat_card,
    stat_grid,
)
from powerbi_import.interface_diff import compare_report_interface
from powerbi_import.openability import check_openability
from powerbi_import.parity_registry import scan_project
from powerbi_import.powerquery_diff import compare_report_tables
from powerbi_import.semantic_execution_validator import SemanticExecutionValidator


@dataclass
class MigrationQualityReport:
    """Aggregated quality result for one generated migration project."""

    report_name: str
    assessment: Dict[str, Any]
    parity: Dict[str, Any]
    data: Dict[str, Any]
    interface: Dict[str, Any]
    openability: Dict[str, Any]
    status: str
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    openability_confidence: Dict[str, Any] = field(default_factory=dict)
    desktop: Dict[str, Any] = field(default_factory=dict)
    fabric: Dict[str, Any] = field(default_factory=dict)
    semantic_context: Dict[str, Any] = field(default_factory=dict)
    priorities: list[Dict[str, Any]] = field(default_factory=list)
    ai_summary: str = ""
    ai_source: str = "none"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "report_name": self.report_name,
            "status": self.status,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "priorities": list(self.priorities),
            "ai_summary": self.ai_summary,
            "ai_source": self.ai_source,
            "assessment": self.assessment,
            "parity": self.parity,
            "data": self.data,
            "interface": self.interface,
            "openability": self.openability,
            "openability_confidence": self.openability_confidence,
            "desktop": self.desktop,
            "fabric": self.fabric,
            "semantic_context": self.semantic_context,
        }

    def save_json(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False, default=str)
        return path

    def save_html(self, path: str) -> str:
        """Write a self-contained human-readable quality report."""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        html = html_open(
            f"Migration quality — {self.report_name}",
            "Deterministic validation with optional grounded AI summary",
        )
        html += stat_grid([
            stat_card(self.status, "Overall status",
                      accent={"PASS": "success", "WARN": "warn", "FAIL": "fail"}.get(
                          self.status, "")),
            stat_card(self.parity.get("parity_score", "n/a"), "Parity score"),
            stat_card(len(self.blockers), "Blockers", accent="fail"),
            stat_card(len(self.warnings), "Warnings", accent="warn"),
        ])
        html += section_open("quality-confidence", "Openability confidence", "OK")
        html += "<pre>" + esc(json.dumps(
            self.openability_confidence, indent=2, ensure_ascii=False,
            default=str)) + "</pre>"
        html += section_close()

        html += section_open("quality-blockers", "Blockers", "!")
        html += self._html_list(self.blockers, "No blockers.")
        html += section_close()
        html += section_open("quality-warnings", "Warnings", "!")
        html += self._html_list(self.warnings, "No warnings.")
        html += section_close()
        html += section_open("quality-priorities", "Remediation priorities", "->")
        if self.priorities:
            html += "<ol>" + "".join(
                f"<li><strong>{esc(item['priority'])}</strong> "
                f"{esc(item['action'])} "
                f"<em>(owner: {esc(item['owner'])})</em></li>"
                for item in self.priorities
            ) + "</ol>"
        else:
            html += "<p>No remediation priorities.</p>"
        html += section_close()
        html += section_open("quality-ai", "AI summary", "AI")
        html += (f"<p>{esc(self.ai_summary)}</p>"
                 if self.ai_summary else "<p>No AI summary was requested or available.</p>")
        html += section_close()
        html += section_open("quality-details", "Validation details", "i")
        html += "<pre>" + esc(json.dumps({
            "parity": self.parity,
            "data": self.data,
            "interface": self.interface,
            "openability": self.openability,
            "fabric": self.fabric,
        }, indent=2, ensure_ascii=False, default=str)) + "</pre>"
        html += section_close()
        html += html_close()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        return path

    @staticmethod
    def _html_list(items: list[str], empty_text: str) -> str:
        if not items:
            return f"<p>{esc(empty_text)}</p>"
        return "<ul>" + "".join(f"<li>{esc(item)}</li>" for item in items) + "</ul>"


def _assessment_dict(report: Any) -> Dict[str, Any]:
    if hasattr(report, "to_dict"):
        return report.to_dict()
    return dict(report) if isinstance(report, dict) else {}


def _openability_dict(report: Any) -> Dict[str, Any]:
    if hasattr(report, "to_dict"):
        return report.to_dict()
    return dict(report) if isinstance(report, dict) else {}


def _fabric_validation(project_dir: str, report_name: str) -> Dict[str, Any]:
    """Validate a Fabric bundle when the generated project contains one."""
    lakehouse_dir = os.path.join(project_dir, f"{report_name}.Lakehouse")
    if not os.path.isdir(lakehouse_dir):
        return {"present": False, "valid": True, "errors": [], "warnings": []}
    try:
        from powerbi_import.fabric_validator import FabricProjectValidator
        result = FabricProjectValidator.validate(project_dir, report_name,
                                                  include_report=True)
        return {"present": True, **result}
    except Exception as exc:  # noqa: BLE001 - quality reporting must not crash migration
        return {"present": True, "valid": False, "errors": [str(exc)], "warnings": []}


def _openability_confidence(openability: Dict[str, Any], fabric: Dict[str, Any],
                            desktop: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build an honest evidence label; static validation never implies Desktop."""
    static_pass = bool(openability.get("openable"))
    if fabric.get("present") and not fabric.get("valid", False):
        static_pass = False
    desktop = desktop or {}
    desktop_status = desktop.get("status", "not_run")
    level = "STATIC_PASS" if static_pass else "UNVERIFIED"
    if static_pass and desktop_status == "opened":
        level = "DESKTOP_SMOKE_PASS"
    return {
        "level": level,
        "static_checks": {
            "passed": sum(1 for check in openability.get("checks", [])
                           if check.get("ok")),
            "failed": sum(1 for check in openability.get("checks", [])
                           if not check.get("ok")),
        },
        "desktop": {
            "status": desktop_status,
            "version": desktop.get("executable"),
            "signals": desktop.get("signals", []),
        },
        "semantic_execution": "not_run",
        "refresh": "not_run",
        "deployment": "not_run",
    }


def _build_priorities(parity: Dict[str, Any], blockers: list[str],
                      warnings: list[str]) -> list[Dict[str, Any]]:
    """Create a stable, deterministic remediation queue from verified findings."""
    priorities = []
    for blocker in blockers:
        owner = "Orchestrator"
        if "DAX" in blocker or "feature" in blocker:
            owner = "DAX / Semantic"
        elif "table" in blocker or "model" in blocker:
            owner = "Semantic / Wiring"
        elif "open" in blocker.lower() or "reference" in blocker.lower():
            owner = "Visual / Orchestrator"
        priorities.append({
            "priority": "P0",
            "owner": owner,
            "action": blocker,
            "evidence": [],
        })
    for gap in parity.get("gaps", []):
        if gap.get("status") != "unsupported":
            continue
        priorities.append({
            "priority": "P1",
            "owner": "Assessor / domain owner",
            "action": f"Resolve unsupported feature: {gap.get('label', gap.get('key', 'unknown'))}",
            "evidence": list(gap.get("evidence", [])),
        })
    for warning in warnings:
        priorities.append({
            "priority": "P2",
            "owner": "Assessor",
            "action": warning,
            "evidence": [],
        })
    return priorities


def _semantic_context_validation(extracted: Dict[str, Any]) -> Dict[str, Any]:
    """Collect static LOD diagnostics without implying DAX execution."""
    column_table_map: Dict[str, str] = {}
    calculations = []
    relationships = []
    for datasource in extracted.get("datasources", []) or []:
        for table in datasource.get("tables", []) or []:
            table_name = table.get("name", "")
            for column in table.get("columns", []) or []:
                column_name = column.get("name", "")
                if column_name and table_name and column_name not in column_table_map:
                    column_table_map[column_name] = table_name
        calculations.extend(datasource.get("calculations", []) or [])
        relationships.extend(datasource.get("relationships", []) or [])
    calculations.extend(extracted.get("calculations", []) or [])
    validator = SemanticExecutionValidator()
    issues = []
    for calculation in calculations:
        formula = calculation.get("formula", "")
        if not formula:
            continue
        calculation_issues = validator.validate_lod_grain_compatibility(
            formula, column_table_map, relationships
        )
        calculation_issues.extend(validator.validate_table_calc_partition(
            calculation, column_table_map
        ))
        issues.extend({
            "calculation": calculation.get("caption", calculation.get("name", "")),
            "issue": issue,
        } for issue in calculation_issues)
    return {
        "status": "static_diagnostics",
        "calculations_scanned": len(calculations),
        "lod_issues": issues,
        "issue_count": len(issues),
        "execution": "not_run",
    }


def build_quality_report(extracted: Dict, project_dir: str,
                         report_name: str) -> MigrationQualityReport:
    """Run all local quality checks and aggregate their verified results."""
    assessment = run_assessment(extracted or {}, workbook_name=report_name)
    parity = scan_project(extracted or {}, project_dir, report_name).to_dict()
    data = compare_report_tables(extracted or {}, project_dir, report_name)
    interface = compare_report_interface(extracted or {}, project_dir, report_name)
    openability = check_openability(project_dir)
    fabric = _fabric_validation(project_dir, report_name)
    semantic_context = _semantic_context_validation(extracted or {})
    openability_dict = _openability_dict(openability)
    confidence = _openability_confidence(openability_dict, fabric)

    blockers = []
    warnings = []
    if not openability.openable:
        blockers.extend(openability.blocking_issues)
    if fabric.get("present") and not fabric.get("valid", False):
        blockers.append("Fabric-native artifact bundle failed validation.")
    if assessment.overall_score == "RED":
        blockers.append("Pre-migration assessment contains blocking failures.")
    if any(gap.get("status") == "unsupported"
           for gap in parity.get("gaps", [])):
        blockers.append("Unsupported Tableau features remain in use.")
    if parity.get("untracked_features"):
        names = ", ".join(parity["untracked_features"])
        warnings.append(f"Feature families lack parity mappings: {names}.")
    if data.get("summary", {}).get("tables_found", 0) < data.get("summary", {}).get("source_tables", 0):
        blockers.append("One or more extracted source tables are missing from the target model.")
    if not interface.get("filters", {}).get("covered", True):
        warnings.append("Interface filter coverage is below the extracted source count.")
    if not interface.get("parameters", {}).get("covered", True):
        warnings.append("One or more extracted parameters lack a target symbol.")
    if assessment.overall_score == "YELLOW":
        warnings.append("Pre-migration assessment contains warnings.")

    status = "FAIL" if blockers else "WARN" if warnings else "PASS"
    priorities = _build_priorities(parity, blockers, warnings)
    return MigrationQualityReport(
        report_name=report_name,
        assessment=_assessment_dict(assessment),
        parity=parity,
        data=data,
        interface=interface,
        openability=openability_dict,
        openability_confidence=confidence,
        fabric=fabric,
        semantic_context=semantic_context,
        status=status,
        blockers=blockers,
        warnings=warnings,
        priorities=priorities,
    )


def build_quality_prompt(report: MigrationQualityReport) -> str:
    """Build a compact prompt from verified findings for an optional AI call."""
    payload = {
        "report_name": report.report_name,
        "status": report.status,
        "blockers": report.blockers,
        "warnings": report.warnings,
        "priorities": report.priorities,
        "parity": report.parity,
        "data": report.data,
        "interface": report.interface,
        "openability": report.openability,
    }
    return (
        "Summarize this verified Tableau-to-Power BI migration quality report. "
        "Use only the supplied facts. Do not invent tests, blockers, coverage, "
        "or deployment status. Return three short sections: outcome, highest "
        "priority actions, and residual risks.\n\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    )


def apply_desktop_probe(report: MigrationQualityReport, probe: Any) -> MigrationQualityReport:
    """Attach Desktop smoke evidence without upgrading to reopen/production status."""
    evidence = probe.to_dict() if hasattr(probe, "to_dict") else dict(probe or {})
    report.desktop = evidence
    report.openability_confidence = _openability_confidence(
        report.openability, report.fabric, evidence)
    return report


def add_ai_summary(report: MigrationQualityReport, gateway: Any) -> MigrationQualityReport:
    """Attach an optional AI summary; deterministic findings remain authoritative."""
    if gateway is None:
        return report
    result = gateway.complete(
        build_quality_prompt(report),
        system=("You summarize verified migration QA evidence. Never change or "
                "reinterpret the report status, blockers, or warnings."),
    )
    text = getattr(result, "text", result)
    if text:
        report.ai_summary = str(text).strip()
        report.ai_source = getattr(result, "source", "llm") or "llm"
    return report
