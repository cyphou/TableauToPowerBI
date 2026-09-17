"""Unified migration quality report.

Combines the existing deterministic assessment, parity, artifact comparison,
interface, and openability checks into one machine-readable quality contract.
The report is deliberately deterministic so an optional AI summary can be
based on verified findings rather than replacing validation logic.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from powerbi_import.assessment import run_assessment
from powerbi_import.assessment_evidence import build_assessment_evidence
from powerbi_import.html_template import (
    badge,
    data_table,
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
from powerbi_import.quality_grades import color as grade_color
from powerbi_import.semantic_execution_validator import SemanticExecutionValidator
from powerbi_import.semantic_runtime import validate_semantic_execution
from powerbi_import.semantic_fixtures import load_semantic_fixture
from powerbi_import.m_emitter_matrix import build_m_emitter_matrix, summarize_m_emitter_matrix
from powerbi_import.evidence_manifest import build_evidence_manifest
from powerbi_import.strategy_advisor import recommend_strategy
from powerbi_import.fabric_evidence import build_fabric_evidence
from powerbi_import.source_inventory import build_source_inventory
from powerbi_import.recovery_registry import build_recovery_registry
from powerbi_import.migration_ledger import build_migration_ledger
from powerbi_import.validation_contract import build_validation_contract
from powerbi_import.pbir_visual_recovery import scan_pbir_visual_recovery
from powerbi_import.visual_parity_contract import build_visual_parity_contract
from powerbi_import.page_composition import build_page_composition_contract
from powerbi_import.cross_validator import scan_visual_role_contract
from powerbi_import.interaction_graph import build_interaction_graph
from powerbi_import.roundtrip_validation import build_roundtrip_validation
from powerbi_import.visual_mapping_matrix import (
    build_visual_mapping_matrix,
    find_visual_approximations_in_use,
    summarize_visual_mapping_matrix,
)


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
    assessment_evidence: Dict[str, Any] = field(default_factory=dict)
    recovery: Dict[str, Any] = field(default_factory=dict)
    validation_contract: Dict[str, Any] = field(default_factory=dict)
    visual_recovery: Dict[str, Any] = field(default_factory=dict)
    page_composition: Dict[str, Any] = field(default_factory=dict)
    interaction_graph: Dict[str, Any] = field(default_factory=dict)
    visual_parity: Dict[str, Any] = field(default_factory=dict)
    roundtrip_validation: Dict[str, Any] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    openability_confidence: Dict[str, Any] = field(default_factory=dict)
    desktop: Dict[str, Any] = field(default_factory=dict)
    fabric: Dict[str, Any] = field(default_factory=dict)
    fabric_evidence: Dict[str, Any] = field(default_factory=dict)
    semantic_context: Dict[str, Any] = field(default_factory=dict)
    m_emitters: Dict[str, Any] = field(default_factory=dict)
    visual_mappings: Dict[str, Any] = field(default_factory=dict)
    source_inventory: Dict[str, Any] = field(default_factory=dict)
    migration_ledger: Dict[str, Any] = field(default_factory=dict)
    evidence_manifest: Dict[str, Any] = field(default_factory=dict)
    strategy: Dict[str, Any] = field(default_factory=dict)
    lineage: Dict[str, Any] = field(default_factory=dict)
    handoff_status: str = "UNVERIFIED"
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
            "assessment_evidence": self.assessment_evidence,
            "recovery": self.recovery,
            "validation_contract": self.validation_contract,
            "visual_recovery": self.visual_recovery,
            "page_composition": self.page_composition,
            "interaction_graph": self.interaction_graph,
            "visual_parity": self.visual_parity,
            "roundtrip_validation": self.roundtrip_validation,
            "parity": self.parity,
            "data": self.data,
            "interface": self.interface,
            "openability": self.openability,
            "openability_confidence": self.openability_confidence,
            "desktop": self.desktop,
            "fabric": self.fabric,
            "fabric_evidence": self.fabric_evidence,
            "semantic_context": self.semantic_context,
            "m_emitters": self.m_emitters,
            "visual_mappings": self.visual_mappings,
            "source_inventory": self.source_inventory,
            "migration_ledger": self.migration_ledger,
            "evidence_manifest": self.evidence_manifest,
            "strategy": self.strategy,
            "lineage": self.lineage,
            "handoff_status": self.handoff_status,
        }

    def save_json(self, path: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False, default=str)
        return path

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MigrationQualityReport":
        """Reconstruct a report from a saved quality JSON payload."""
        import dataclasses
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

    def render_body(self) -> str:
        """Return the readable report body (sections only, no page wrapper)."""
        html = stat_grid([
            stat_card(self.status, "Overall status",
                      accent={"PASS": "success", "WARN": "warn", "FAIL": "fail"}.get(
                          self.status, "")),
            stat_card(self.parity.get("parity_score", "n/a"), "Parity score"),
            stat_card(len(self.blockers), "Blockers", accent="fail"),
            stat_card(len(self.warnings), "Warnings", accent="warn"),
        ])
        html += section_open("quality-confidence", "Openability confidence", "OK")
        html += self._render_obj(self.openability_confidence)
        html += self._raw_details(self.openability_confidence)
        html += section_close()
        handoff = {
            "handoff_status": self.handoff_status,
            "strategy": self.strategy,
            "lineage": self.lineage,
            "checkpoints": self.evidence_manifest.get("checkpoints", {}),
        }
        html += section_open("quality-handoff", "Operator handoff", "->")
        html += self._render_obj(handoff)
        html += self._raw_details(handoff)
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
        details = {
            "parity": self.parity,
            "data": self.data,
            "interface": self.interface,
            "openability": self.openability,
            "fabric": self.fabric,
            "strategy": self.strategy,
            "lineage": self.lineage,
        }
        html += section_open("quality-details", "Validation details", "i")
        html += self._render_obj(details)
        html += self._raw_details(details)
        html += section_close()
        return html

    def save_html(self, path: str) -> str:
        """Write a self-contained human-readable quality report."""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        html = html_open(
            f"Migration quality — {self.report_name}",
            "Deterministic validation with optional grounded AI summary",
        ) + self.render_body() + html_close()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        return path

    @staticmethod
    def _html_list(items: list[str], empty_text: str) -> str:
        if not items:
            return f"<p>{esc(empty_text)}</p>"
        return "<ul>" + "".join(f"<li>{esc(item)}</li>" for item in items) + "</ul>"

    @staticmethod
    def _fmt_scalar(v: Any) -> str:
        """Render a leaf value as a badge for status-like values, else text."""
        if isinstance(v, bool):
            return badge("PASS" if v else "FAIL")
        if v is None or v == "":
            return badge("n/a", "gray")
        s = str(v)
        tone = grade_color(s)
        return badge(s, tone) if tone else esc(s)

    @classmethod
    def _render_list_of_dicts(cls, items: list) -> str:
        keys: list[str] = []
        for it in items:
            for k in it.keys():
                if k not in keys:
                    keys.append(k)
        rows = [[cls._fmt_scalar(it.get(k, "")) for k in keys] for it in items]
        return data_table([str(k) for k in keys], rows, detail=True)

    @classmethod
    def _render_obj(cls, obj: Any) -> str:
        """Render nested dict/list data as readable tables instead of raw JSON."""
        if isinstance(obj, dict):
            if not obj:
                return "<p>None.</p>"
            rows: list[list[str]] = []
            blocks: list[str] = []
            for k, v in obj.items():
                if isinstance(v, dict) and v:
                    blocks.append(f"<h4>{esc(str(k))}</h4>" + cls._render_obj(v))
                elif isinstance(v, list) and v and all(isinstance(i, dict) for i in v):
                    blocks.append(f"<h4>{esc(str(k))} ({len(v)})</h4>"
                                  + cls._render_list_of_dicts(v))
                elif isinstance(v, list):
                    joined = ", ".join(str(i) for i in v) if v else "\u2014"
                    rows.append([esc(str(k)), esc(joined)])
                else:
                    rows.append([esc(str(k)), cls._fmt_scalar(v)])
            table = data_table(["Field", "Value"], rows, detail=True) if rows else ""
            return table + "".join(blocks)
        if isinstance(obj, list):
            if obj and all(isinstance(i, dict) for i in obj):
                return cls._render_list_of_dicts(obj)
            return "<p>" + esc(", ".join(str(i) for i in obj) or "\u2014") + "</p>"
        return "<p>" + cls._fmt_scalar(obj) + "</p>"

    @staticmethod
    def _raw_details(obj: Any) -> str:
        """Collapsible raw JSON so no data is lost from the readable view."""
        return ("<details class=\"raw-json\"><summary>Raw JSON</summary><pre>"
                + esc(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
                + "</pre></details>")


def build_consolidated_quality_html(quality_json_paths: list,
                                    output_path: str,
                                    title: str = "Consolidated migration quality") -> str:
    """Merge per-workbook quality JSON reports into one readable HTML file.

    Each workbook becomes a tab; an overview tab summarizes all workbooks.
    """
    from powerbi_import.html_template import tab_bar, tab_content, badge, data_table

    reports: list[MigrationQualityReport] = []
    for path in quality_json_paths:
        try:
            with open(path, encoding="utf-8") as fh:
                reports.append(MigrationQualityReport.from_dict(json.load(fh)))
        except (OSError, ValueError, TypeError) as exc:
            logger = __import__("logging").getLogger(__name__)
            logger.warning("Skipping unreadable quality JSON %s: %s", path, exc)

    reports.sort(key=lambda r: r.report_name.lower())
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for rep in reports:
        counts[rep.status] = counts.get(rep.status, 0) + 1

    html = html_open(title,
                     "Merged deterministic validation across all migrated workbooks")
    html += stat_grid([
        stat_card(len(reports), "Workbooks"),
        stat_card(counts.get("PASS", 0), "Pass", accent="success"),
        stat_card(counts.get("WARN", 0), "Warnings", accent="warn"),
        stat_card(counts.get("FAIL", 0), "Failures", accent="fail"),
    ])

    overview_rows = [
        [
            esc(rep.report_name),
            badge(rep.status),
            esc(str(rep.parity.get("parity_score", "n/a"))),
            str(len(rep.blockers)),
            str(len(rep.warnings)),
            str(len(rep.priorities)),
        ]
        for rep in reports
    ]
    overview = section_open("consolidated-overview", "Overview", "i")
    overview += data_table(
        ["Workbook", "Status", "Parity", "Blockers", "Warnings", "Priorities"],
        overview_rows, table_id="consolidated-overview-table",
        sortable=True, searchable=True)
    overview += section_close()

    tabs = [("overview", "Overview", True)]
    tabs += [(f"wb{i}", esc(rep.report_name), False)
             for i, rep in enumerate(reports)]
    html += tab_bar("consolidated", tabs)
    html += tab_content("consolidated", "overview", overview, active=True)
    for i, rep in enumerate(reports):
        body = f"<h2>{esc(rep.report_name)}</h2>" + rep.render_body()
        html += tab_content("consolidated", f"wb{i}", body, active=False)
    html += html_close()

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return output_path


def _assessment_has_blocking_failures(assessment: Any) -> bool:
    """Return true for source incompatibility failures, not performance risk alone."""
    for category in getattr(assessment, "categories", []) or []:
        if getattr(category, "name", "") == "Performance":
            continue
        if any(getattr(check, "severity", "") == "fail"
               for check in getattr(category, "checks", []) or []):
            return True
    return False


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
    if static_pass and desktop_status == "reopened":
        level = "DESKTOP_REOPEN_PASS"
    elif static_pass and desktop_status == "opened":
        level = "DESKTOP_SMOKE_PASS"
    elif desktop_status in {"crashed", "timed_out", "error"}:
        level = "UNVERIFIED"
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


def _measure_context_validation(project_dir: str) -> Dict[str, Any]:
    """Collect target-model measure context diagnostics when a model exists."""
    try:
        model_dirs = [name for name in os.listdir(project_dir)
                      if name.endswith(".SemanticModel")]
    except OSError:
        model_dirs = []
    if not model_dirs:
        return {"status": "not_available", "issues": [], "issue_count": 0}
    fallback_issues = []
    try:
        model_root = os.path.join(project_dir, model_dirs[0], "definition", "tables")
        for filename in os.listdir(model_root):
            if not filename.endswith(".tmdl"):
                continue
            path = os.path.join(model_root, filename)
            with open(path, encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if "TODO: DAX conversion validation failed" in line:
                        fallback_issues.append({
                            "file": filename,
                            "line": line_number,
                            "issue": line.strip(),
                        })
    except OSError:
        fallback_issues = []
    try:
        from powerbi_import.validator import ArtifactValidator
        issues = ArtifactValidator.validate_measure_column_context(
            os.path.join(project_dir, model_dirs[0])
        )
    except (OSError, ValueError):
        issues = []
    return {
        "status": "static_diagnostics",
        "issues": list(issues),
        "fallbacks": fallback_issues,
        "issue_count": len(issues) + len(fallback_issues),
        "fallback_count": len(fallback_issues),
    }


def _filter_context_validation(project_dir: str) -> Dict[str, Any]:
    """Check generated DAX context modifiers against emitted TMDL columns."""
    try:
        project_entries = os.listdir(project_dir)
    except OSError:
        project_entries = []
    model_dir = next((os.path.join(project_dir, name) for name in project_entries
                      if name.endswith(".SemanticModel")), None)
    if not model_dir:
        return {"status": "not_available", "issues": [], "issue_count": 0}
    tables_dir = os.path.join(model_dir, "definition", "tables")
    if not os.path.isdir(tables_dir):
        return {"status": "not_available", "issues": [], "issue_count": 0}
    column_table_map: Dict[str, str] = {}
    measures = []
    for filename in os.listdir(tables_dir):
        if not filename.endswith(".tmdl"):
            continue
        path = os.path.join(tables_dir, filename)
        try:
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
        except OSError:
            continue
        table_match = re.search(r"^table\s+(.+?)\s*$", content, re.MULTILINE)
        table_name = table_match.group(1).strip(" '") if table_match else ""
        for column in re.findall(r"^\s*column\s+'((?:[^']|'')+)'", content, re.MULTILINE):
            column_table_map[column.replace("''", "'")] = table_name
        measures.extend(re.findall(r"^\s*measure\s+.*?=\s*(.+)$", content, re.MULTILINE))
    validator = SemanticExecutionValidator()
    issues = [issue for expression in measures for issue in
              validator.validate_filter_context_expression(expression, column_table_map)]
    return {"status": "static_diagnostics", "issues": issues, "issue_count": len(issues)}


def _handoff_status(status: str, openability: Dict[str, Any]) -> str:
    """Translate local quality into an operator-facing handoff state."""
    if status == "FAIL" or not openability.get("openable", False):
        return "BLOCKED"
    if status == "WARN":
        return "WARN"
    return "PASS"


_QUALITY_POLICIES = {
    "report": {"unresolved_lineage": "warning", "semantic_diagnostics": "ignore", "m_fallback": "warning", "fabric_bundle": "warning", "fabric_runtime": "ignore", "visual_approximation": "warning"},
    "enterprise": {"unresolved_lineage": "warning", "semantic_diagnostics": "blocker", "m_fallback": "blocker", "fabric_bundle": "blocker", "fabric_runtime": "ignore", "visual_approximation": "blocker"},
    "production": {"unresolved_lineage": "blocker", "semantic_diagnostics": "blocker", "m_fallback": "blocker", "fabric_bundle": "blocker", "fabric_runtime": "blocker", "visual_approximation": "blocker"},
}


def _quality_policy(name: str) -> Dict[str, str]:
    """Return a known quality policy, failing closed for unknown names."""
    return dict(_QUALITY_POLICIES.get(name, _QUALITY_POLICIES["production"]))


def _m_emitter_evidence(extracted: Dict[str, Any]) -> Dict[str, Any]:
    """Build matrix evidence and identify fallback connectors actually in use."""
    rows = build_m_emitter_matrix()
    summary = summarize_m_emitter_matrix(rows)
    source_types = set()
    for datasource in extracted.get("datasources", []) or []:
        connection = datasource.get("connection", {}) or {}
        source_types.add(str(connection.get("type", "")))
        source_types.add(str(connection.get("class", "")))
    fallback_rows = [
        row for row in rows
        if row.get("status") == "fallback" and row.get("connector") in source_types
    ]
    return {
        "status": "static_evidence",
        "summary": summary,
        "fallback_in_use": fallback_rows,
        "rows": rows,
    }


def _lineage_evidence(extracted: Dict[str, Any], data: Dict[str, Any],
                      interface: Dict[str, Any], parity: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize source-to-target coverage without inventing runtime lineage."""
    inventory = build_source_inventory(extracted)
    datasources = extracted.get("datasources", []) or []
    source_tables = sum(len(ds.get("tables", []) or []) for ds in datasources)
    source_columns = sum(
        len(table.get("columns", []) or [])
        for ds in datasources for table in (ds.get("tables", []) or [])
    )
    return {
        "status": "static_evidence",
        "source": {
            "datasources": len(datasources),
            "tables": source_tables,
            "columns": source_columns,
            "worksheets": len(extracted.get("worksheets", []) or []),
            "calculations": len(extracted.get("calculations", []) or []),
        },
        "target": {
            "tables": data.get("summary", {}).get("tables_found", 0),
            "visuals": interface.get("visuals", {}).get("target", 0),
            "calculation_features": parity.get("status_counts", {}),
        },
        "coverage": {
            "tables": {
                "source": source_tables,
                "target": data.get("summary", {}).get("tables_found", 0),
                "complete": data.get("summary", {}).get("tables_found", 0) >= source_tables,
            },
            "parity_evidence_percent": parity.get("evidence_coverage", {}).get(
                "coverage_percent", 0.0),
            "inventory_objects": inventory["object_count"],
            "inventory_object_types": {
                object_type: count for object_type, count in inventory["counts"].items()
                if count
            },
        },
        "unresolved": [
            {
                "stable_id": row["stable_id"],
                "object_type": row["object_type"],
                "name": row["name"],
                "reason": "missing_parent_reference",
            }
            for row in inventory["objects"] if row["orphan"]
        ],
        "runtime": "not_run",
    }


def _load_lineage_contract(project_dir: str) -> Dict[str, Any]:
    """Load the generated static lineage contract when available."""
    candidates = [
        os.path.join(project_dir, "lineage_map.json"),
        os.path.join(os.path.dirname(os.path.abspath(project_dir)), "lineage_map.json"),
    ]
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict) and isinstance(payload.get("contract"), dict):
                return payload["contract"]
        except (OSError, ValueError):
            continue
    return {"status": "not_found", "coverage": {}, "unresolved": []}


def _source_lineage_unresolved(contract: Dict[str, Any], extracted: Dict[str, Any]) -> list:
    """Keep unresolved lineage records that refer to source objects only."""
    source_names = set()
    for datasource in extracted.get("datasources", []) or []:
        for table in datasource.get("tables", []) or []:
            if table.get("name"):
                source_names.add(str(table["name"]))
            for column in table.get("columns", []) or []:
                if column.get("name"):
                    source_names.add(str(column["name"]))
    for calculation in extracted.get("calculations", []) or []:
        name = calculation.get("caption") or calculation.get("name")
        if name:
            source_names.add(str(name).strip("[]"))
    for worksheet in extracted.get("worksheets", []) or []:
        if worksheet.get("name"):
            source_names.add(str(worksheet["name"]))
    if not source_names:
        return list(contract.get("unresolved", []) or [])
    return [
        item for item in contract.get("unresolved", []) or []
        if str(item.get("target", item.get("name", ""))) in source_names
    ]


def _checkpoint_evidence(project_dir: str, checkpoint_path: Optional[str]) -> Dict[str, Any]:
    """Load checkpoint metadata while keeping the report portable and redacted."""
    path = checkpoint_path
    if not path:
        parent = os.path.dirname(os.path.abspath(project_dir))
        name = os.path.basename(project_dir)
        candidate = os.path.join(parent, f".{name}.migration_checkpoint.json")
        path = candidate if os.path.isfile(candidate) else None
    if not path or not os.path.isfile(path):
        return {"status": "not_found", "stages": {}}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {"status": "invalid", "stages": {}}
    stages = payload.get("stages", {})
    return {
        "status": "available",
        "version": payload.get("version"),
        "updated_at": payload.get("updated_at"),
        "stages": stages if isinstance(stages, dict) else {},
    }


def _artifact_evidence(project_dir: str) -> Dict[str, Any]:
    """List generated artifact families, not customer payloads."""
    try:
        entries = os.listdir(project_dir)
    except OSError:
        entries = []
    families = {
        "pbip": any(name.endswith(".pbip") for name in entries),
        "report": any(name.endswith(".Report") for name in entries),
        "semantic_model": any(name.endswith(".SemanticModel") for name in entries),
        "fabric": any(name.endswith(".Lakehouse") for name in entries),
    }
    return {"status": "generated" if any(families.values()) else "missing", "families": families}


def build_quality_report(extracted: Dict, project_dir: str,
                         report_name: str, *, source_path: Optional[str] = None,
                         checkpoint_path: Optional[str] = None,
                         prep_flow: bool = False,
                         quality_policy: str = "report",
                         semantic_queries: Optional[list] = None,
                         semantic_executor: Any = None,
                         semantic_fixture_path: Optional[str] = None) -> MigrationQualityReport:
    """Run all local quality checks and aggregate their verified results."""
    policy = _quality_policy(quality_policy)
    m_emitters = _m_emitter_evidence(extracted or {})
    visual_rows = build_visual_mapping_matrix()
    visual_mappings = {"status": "static_evidence", "summary": summarize_visual_mapping_matrix(visual_rows), "rows": visual_rows}
    assessment = run_assessment(extracted or {}, workbook_name=report_name)
    assessment_evidence = build_assessment_evidence(assessment)
    parity = scan_project(extracted or {}, project_dir, report_name).to_dict()
    data = compare_report_tables(extracted or {}, project_dir, report_name)
    interface = compare_report_interface(extracted or {}, project_dir, report_name)
    openability = check_openability(project_dir)
    fabric = _fabric_validation(project_dir, report_name)
    fabric_evidence = build_fabric_evidence(project_dir, report_name)
    semantic_context = _semantic_context_validation(extracted or {})
    measure_context = _measure_context_validation(project_dir)
    semantic_context["measure_context"] = measure_context
    semantic_context["filter_context"] = _filter_context_validation(project_dir)
    fixture_error = ""
    if semantic_fixture_path:
        try:
            semantic_queries = load_semantic_fixture(semantic_fixture_path)["queries"]
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            fixture_error = str(exc)
    if fixture_error:
        semantic_context["execution"] = {
            "status": "failed",
            "queries_requested": 0,
            "queries_run": 0,
            "passed": 0,
            "failed": 1,
            "results": [{"name": "fixture", "status": "failed", "error": fixture_error}],
        }
    else:
        semantic_context["execution"] = validate_semantic_execution(
            semantic_queries or [], semantic_executor)
    semantic_issue_count = (
        semantic_context.get("issue_count", 0)
        + measure_context.get("issue_count", 0)
        + semantic_context["filter_context"].get("issue_count", 0)
    )
    semantic_runtime_failures = semantic_context["execution"].get("failed", 0)
    openability_dict = _openability_dict(openability)
    confidence = _openability_confidence(openability_dict, fabric)

    blockers = []
    warnings = []
    if not openability.openable:
        blockers.extend(openability.blocking_issues)
    fabric_invalid = fabric_evidence.get("status") == "invalid"
    if fabric_invalid:
        message = "Fabric-native artifact bundle failed validation."
        if policy["fabric_bundle"] == "blocker":
            blockers.append(message)
        elif policy["fabric_bundle"] == "warning":
            warnings.append(message)
    if policy["fabric_runtime"] == "blocker" and fabric_evidence.get("status") == "locally_valid":
        runtime = fabric_evidence.get("runtime", {})
        missing_runtime = [name for name in (
            "deployment", "refresh", "semantic_execution", "post_deploy"
        ) if runtime.get(name) != "passed"]
        if missing_runtime:
            blockers.append(
                "Fabric production evidence is incomplete: "
                + ", ".join(missing_runtime) + "."
            )
    assessment_blocking_failures = _assessment_has_blocking_failures(assessment)
    if assessment_blocking_failures:
        blockers.append("Pre-migration assessment contains blocking failures.")
    elif assessment.overall_score == "RED":
        warnings.append(
            "Pre-migration assessment is RED due to performance risk; review before production."
        )
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

    lineage = _lineage_evidence(extracted or {}, data, interface, parity)
    lineage_contract = _load_lineage_contract(project_dir)
    lineage["contract"] = lineage_contract
    unresolved_lineage = _source_lineage_unresolved(lineage_contract, extracted or {})
    lineage_contract["unresolved_source"] = unresolved_lineage
    if unresolved_lineage:
        message = (
            f"Semantic lineage has {len(unresolved_lineage)} unresolved "
            "source-to-target record(s)."
        )
        if policy["unresolved_lineage"] == "blocker":
            blockers.append(message)
        else:
            warnings.append(message)
    if semantic_issue_count:
        message = (
            f"Semantic validation found {semantic_issue_count} static "
            "context issue(s)."
        )
        if policy["semantic_diagnostics"] == "blocker":
            blockers.append(message)
        elif policy["semantic_diagnostics"] == "warning":
            warnings.append(message)
    if semantic_runtime_failures:
        message = (
            f"Semantic runtime validation failed for {semantic_runtime_failures} "
            "query(ies)."
        )
        if policy["semantic_diagnostics"] == "blocker":
            blockers.append(message)
        elif policy["semantic_diagnostics"] == "warning":
            warnings.append(message)
    fallback_in_use = m_emitters.get("fallback_in_use", [])
    if fallback_in_use:
        message = (
            f"M fallback emitters are in use for {len(fallback_in_use)} "
            "connector path(s)."
        )
        if policy["m_fallback"] == "blocker":
            blockers.append(message)
        elif policy["m_fallback"] == "warning":
            warnings.append(message)
    visual_approximations = find_visual_approximations_in_use(extracted or {})
    if visual_approximations:
        message = f"Visual mapping contains {len(visual_approximations)} explicit approximation(s)."
        if policy["visual_approximation"] == "blocker":
            blockers.append(message)
        elif policy["visual_approximation"] == "warning":
            warnings.append(message)

    status = "FAIL" if blockers else "WARN" if warnings else "PASS"
    source_inventory = build_source_inventory(extracted or {})
    recovery = build_recovery_registry(source_inventory)
    migration_ledger = build_migration_ledger(source_inventory, recovery)
    validation_contract = build_validation_contract(
        semantic_context, m_emitters, recovery
    )
    visual_recovery = scan_pbir_visual_recovery(project_dir)
    visual_role_evidence = scan_visual_role_contract(project_dir)
    interaction_graph = build_interaction_graph(extracted or {}, project_dir, report_name)
    page_composition = build_page_composition_contract(
        extracted or {}, project_dir, report_name
    )
    visual_parity = build_visual_parity_contract(
        extracted or {}, visual_mappings, visual_recovery, visual_role_evidence
    )
    roundtrip_validation = build_roundtrip_validation(
        openability_dict,
        visual_recovery,
        visual_parity,
    )
    priorities = _build_priorities(parity, blockers, warnings)
    strategy = recommend_strategy(extracted or {}, prep_flow=prep_flow).to_dict()
    strategy["status"] = "recommended"
    checkpoints = _checkpoint_evidence(project_dir, checkpoint_path)
    artifacts = _artifact_evidence(project_dir)
    handoff_status = _handoff_status(status, openability_dict)
    next_action = priorities[0]["action"] if priorities else (
        "Run authorized Desktop/Fabric checks" if confidence["level"] == "STATIC_PASS"
        else "Generate and validate a target project")
    evidence_manifest = build_evidence_manifest(
        source_path=source_path,
        target_path=project_dir,
        validation={"status": status, "handoff_status": handoff_status,
                    "blockers": blockers, "warnings": warnings,
                "next_action": next_action,
                "quality_policy": quality_policy},
        environment=confidence,
        checkpoints=checkpoints,
        strategy=strategy,
        lineage=lineage,
        artifacts={
            **artifacts,
            "m_emitters": m_emitters.get("summary", {}),
            "fabric": fabric_evidence.get("artifacts", {}),
            "visual_mappings": visual_mappings.get("summary", {}),
            "source_inventory": {
                "object_count": source_inventory["object_count"],
                "orphan_count": source_inventory["orphan_count"],
                "duplicate_name_count": len(source_inventory["duplicate_names"]),
            },
            "migration_ledger": {
                "status": migration_ledger["status"],
                "object_count": migration_ledger["object_count"],
                "unresolved_count": migration_ledger["unresolved_count"],
                "pending_count": migration_ledger["validation"]["pending"],
            },
            "recovery": {
                "status": recovery["status"],
                "object_count": recovery["object_count"],
                "pending_count": recovery["counts"]["pending_validation"],
            },
            "validation": {
                "release_ready": validation_contract["release_ready"],
                "semantic_execution": validation_contract["semantic"]["execution_status"],
                "m_status": validation_contract["m"]["status"],
            },
            "visual_recovery": {
                "status": visual_recovery["status"],
                "visual_count": visual_recovery["visual_count"],
                "empty_count": visual_recovery["counts"]["empty"],
                "orphaned_count": visual_recovery["counts"]["orphaned"],
            },
            "visual_role_contract": {
                "status": visual_role_evidence["status"],
                "visual_count": visual_role_evidence["visual_count"],
                "needs_review": visual_role_evidence["needs_review"],
            },
            "interaction_graph": interaction_graph["summary"],
            "page_composition": {
                "status": page_composition["status"],
                "source_pages": page_composition["summary"]["source_pages"],
                "matched_pages": page_composition["summary"]["matched_pages"],
                "mismatched_objects": page_composition["summary"]["mismatched_objects"],
            },
            "visual_parity": {
                "source_approximation_count": visual_parity["source_used"]["approximation_count"],
                "target_risk_count": visual_parity["target_recovery"]["risk_count"],
                "runtime": visual_parity["runtime"],
            },
            "roundtrip_validation": {
                "static_status": roundtrip_validation["static"]["status"],
                "desktop_status": roundtrip_validation["desktop"]["status"],
                "visual_risk_count": roundtrip_validation["static"]["visual_risk_count"],
            },
        },
    )
    return MigrationQualityReport(
        report_name=report_name,
        assessment=_assessment_dict(assessment),
        assessment_evidence=assessment_evidence,
        recovery=recovery,
        validation_contract=validation_contract,
        visual_recovery=visual_recovery,
        page_composition=page_composition,
        interaction_graph=interaction_graph,
        visual_parity=visual_parity,
        roundtrip_validation=roundtrip_validation,
        parity=parity,
        data=data,
        interface=interface,
        openability=openability_dict,
        openability_confidence=confidence,
        fabric=fabric,
        fabric_evidence=fabric_evidence,
        semantic_context=semantic_context,
        m_emitters=m_emitters,
        visual_mappings=visual_mappings,
        source_inventory=source_inventory,
        migration_ledger=migration_ledger,
        status=status,
        blockers=blockers,
        warnings=warnings,
        priorities=priorities,
        strategy=strategy,
        lineage=lineage,
        handoff_status=handoff_status,
        evidence_manifest=evidence_manifest,
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
