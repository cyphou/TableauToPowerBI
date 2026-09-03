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
from powerbi_import.interface_diff import compare_report_interface
from powerbi_import.openability import check_openability
from powerbi_import.parity_registry import scan_project
from powerbi_import.powerquery_diff import compare_report_tables


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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "report_name": self.report_name,
            "status": self.status,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "assessment": self.assessment,
            "parity": self.parity,
            "data": self.data,
            "interface": self.interface,
            "openability": self.openability,
        }

    def save_json(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False, default=str)
        return path


def _assessment_dict(report: Any) -> Dict[str, Any]:
    if hasattr(report, "to_dict"):
        return report.to_dict()
    return dict(report) if isinstance(report, dict) else {}


def _openability_dict(report: Any) -> Dict[str, Any]:
    if hasattr(report, "to_dict"):
        return report.to_dict()
    return dict(report) if isinstance(report, dict) else {}


def build_quality_report(extracted: Dict, project_dir: str,
                         report_name: str) -> MigrationQualityReport:
    """Run all local quality checks and aggregate their verified results."""
    assessment = run_assessment(extracted or {}, workbook_name=report_name)
    parity = scan_project(extracted or {}, project_dir, report_name).to_dict()
    data = compare_report_tables(extracted or {}, project_dir, report_name)
    interface = compare_report_interface(extracted or {}, project_dir, report_name)
    openability = check_openability(project_dir)

    blockers = []
    warnings = []
    if not openability.openable:
        blockers.extend(openability.blocking_issues)
    if assessment.overall_score == "RED":
        blockers.append("Pre-migration assessment contains blocking failures.")
    if any(gap.get("status") == "unsupported"
           for gap in parity.get("gaps", [])):
        blockers.append("Unsupported Tableau features remain in use.")
    if data.get("summary", {}).get("tables_found", 0) < data.get("summary", {}).get("source_tables", 0):
        blockers.append("One or more extracted source tables are missing from the target model.")
    if not interface.get("filters", {}).get("covered", True):
        warnings.append("Interface filter coverage is below the extracted source count.")
    if not interface.get("parameters", {}).get("covered", True):
        warnings.append("One or more extracted parameters lack a target symbol.")
    if assessment.overall_score == "YELLOW":
        warnings.append("Pre-migration assessment contains warnings.")

    status = "FAIL" if blockers else "WARN" if warnings else "PASS"
    return MigrationQualityReport(
        report_name=report_name,
        assessment=_assessment_dict(assessment),
        parity=parity,
        data=data,
        interface=interface,
        openability=_openability_dict(openability),
        status=status,
        blockers=blockers,
        warnings=warnings,
    )
