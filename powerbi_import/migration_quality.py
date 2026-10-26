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
import csv
import hashlib
import ntpath
import stat
from pathlib import Path
from urllib.parse import quote
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from powerbi_import.assessment import run_assessment
from powerbi_import.report_localization import text, render as ui
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
from powerbi_import.openability import check_openability, extract_m_partitions
from powerbi_import.parity_registry import scan_project
from powerbi_import.powerquery_diff import compare_report_tables
from powerbi_import.quality_grades import color as grade_color
from powerbi_import.security_validator import canonical_path_key, lexical_path_key
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
    hyper_import: Dict[str, Any] = field(default_factory=dict)
    semantic_context: Dict[str, Any] = field(default_factory=dict)
    m_emitters: Dict[str, Any] = field(default_factory=dict)
    visual_mappings: Dict[str, Any] = field(default_factory=dict)
    source_inventory: Dict[str, Any] = field(default_factory=dict)
    migration_ledger: Dict[str, Any] = field(default_factory=dict)
    evidence_manifest: Dict[str, Any] = field(default_factory=dict)
    strategy: Dict[str, Any] = field(default_factory=dict)
    lineage: Dict[str, Any] = field(default_factory=dict)
    review: Dict[str, Any] = field(default_factory=dict)
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
            "hyper_import": self.hyper_import,
            "semantic_context": self.semantic_context,
            "m_emitters": self.m_emitters,
            "visual_mappings": self.visual_mappings,
            "source_inventory": self.source_inventory,
            "migration_ledger": self.migration_ledger,
            "evidence_manifest": self.evidence_manifest,
            "strategy": self.strategy,
            "lineage": self.lineage,
            "review": self.review,
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
            stat_card(self.status, text("Overall status", "État global"),
                      accent={"PASS": "success", "WARN": "warn", "FAIL": "fail"}.get(
                          self.status, "")),
            stat_card(self.parity.get("parity_score", "n/a"), text("Parity score", "Score de parité")),
            stat_card(len(self.blockers), text("Blockers", "Blocages"), accent="fail"),
            stat_card(len(self.warnings), text("Warnings", "Avertissements"), accent="warn"),
        ])
        html += section_open("quality-confidence", text("Openability confidence", "Confiance dans l’ouverture"), "OK")
        html += self._render_obj(self.openability_confidence)
        html += self._raw_details(self.openability_confidence)
        html += section_close()
        if self.hyper_import:
            hyper_status = self.hyper_import.get("status", "unverified")
            html += section_open(
                "quality-hyper-import",
                text("Hyper full Import evidence", "Preuves d’importation complète Hyper"),
                "⇄",
            )
            html += data_table(
                [text("Check", "Contrôle"), text("Evidence", "Preuve")],
                [
                    [text("Static status", "État statique"),
                     badge(str(hyper_status),
                           {"static_verified": "success", "failed": "fail"}.get(
                               hyper_status, "gray"))],
                    [text("Runtime", "Exécution"),
                     badge(str(self.hyper_import.get("runtime_status", "not_run")),
                           "gray")],
                    [text("Full data parity", "Parité complète des données"),
                     badge("not verified", "gray")],
                    [text("Exports", "Exportations"),
                     esc(str(len(self.hyper_import.get("exports", []) or [])))],
                ],
                detail=True,
            )
            export_rows = []
            for item in self.hyper_import.get("exports", []) or []:
                identity = item.get("identity") if isinstance(item, dict) else {}
                identity = identity if isinstance(identity, dict) else {}
                identity_label = ".".join(
                    str(identity.get(key, "")) for key in ("schema", "table")
                    if identity.get(key) is not None
                ) or text("identity unavailable", "identité indisponible")
                item_status = str(item.get("status", "unverified"))
                export_rows.append([
                    esc(str(item.get("filename") or "—")),
                    esc(str(item.get("format") or "—")),
                    esc(identity_label),
                    esc(f"{item.get('row_count', '—')} / "
                        f"{item.get('total_rows', '—')}"),
                    esc(str(len(item.get("bindings", []) or []))),
                    badge(item_status,
                          {"static_verified": "success", "failed": "fail"}.get(
                              item_status, "gray")),
                ])
            if export_rows:
                html += data_table(
                    [text("File", "Fichier"), text("Format", "Format"),
                     text("Physical identity", "Identité physique"),
                     text("Rows / total", "Lignes / total"),
                     text("Final bindings", "Liaisons finales"),
                     text("Status", "État")],
                    export_rows,
                    table_id="quality-hyper-import-exports",
                    sortable=True,
                    searchable=True,
                )
            html += self._html_list(
                self.hyper_import.get("diagnostics", []) or [],
                text("No static Hyper Import blockers.",
                     "Aucun blocage d’importation Hyper statique."),
            )
            html += "<p>" + ui(str(self.hyper_import.get(
                "verification_scope",
                "Static file and binding evidence only; runtime and full data parity are not verified.",
            ))) + "</p>"
            html += self._raw_details(self.hyper_import)
            html += section_close()
        handoff = {
            "handoff_status": self.handoff_status,
            "strategy": self.strategy,
            "lineage": self.lineage,
            "checkpoints": self.evidence_manifest.get("checkpoints", {}),
        }
        html += section_open("quality-handoff", text("Operator handoff", "Transmission à l’opérateur"), "->")
        html += self._render_obj(handoff)
        html += self._raw_details(handoff)
        html += section_close()

        html += section_open("quality-blockers", text("Blockers", "Blocages"), "!")
        html += self._html_list(self.blockers, text("No blockers.", "Aucun blocage."))
        html += section_close()
        html += section_open("quality-warnings", text("Warnings", "Avertissements"), "!")
        html += self._html_list(self.warnings, text("No warnings.", "Aucun avertissement."))
        html += section_close()
        html += section_open("quality-priorities", text("Remediation priorities", "Priorités de remédiation"), "->")
        if self.priorities:
            html += "<ol>" + "".join(
                f"<li><strong>{esc(item['priority'])}</strong> "
                f"{badge(_ACTION_LABEL.get(item.get('action_kind', ''), text('review', 'examiner')), _ACTION_TONE.get(item.get('action_kind', ''), 'gray'))} "
                f"{esc(item['action'])} "
                f"<em>({ui(text('owner:', 'responsable :'))} {esc(item['owner'])})</em>"
                + (f"<br><small>{ui(text('How:', 'Méthode :'))} {esc(item['fix'])}</small>"
                   if item.get("fix") else "")
                + "</li>"
                for item in self.priorities
            ) + "</ol>"
        else:
            html += "<p>" + ui(text("No remediation priorities.", "Aucune priorité de remédiation.")) + "</p>"
        html += section_close()
        html += section_open("quality-ai", text("AI summary", "Synthèse IA"), "AI")
        html += (f"<p>{esc(self.ai_summary)}</p>"
                 if self.ai_summary else "<p>" + ui(text("No AI summary was requested or available.", "Aucune synthèse IA demandée ou disponible.")) + "</p>")
        html += section_close()
        details = {
            "parity": self.parity,
            "data": self.data,
            "interface": self.interface,
            "openability": self.openability,
            "fabric": self.fabric,
            "hyper_import": self.hyper_import,
            "strategy": self.strategy,
            "lineage": self.lineage,
        }
        html += section_open("quality-details", text("Validation details", "Détails de validation"), "i")
        html += self._render_obj(details)
        html += self._raw_details(details)
        html += section_close()
        return html

    def save_html(self, path: str) -> str:
        """Write a self-contained human-readable quality report."""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        html = html_open(
            text(f"Migration quality — {self.report_name}", f"Qualité de migration — {self.report_name}"),
            text("Deterministic validation with optional grounded AI summary",
                 "Validation déterministe avec synthèse IA facultative fondée sur les preuves"),
        ) + _desktop_comparison_links(path) + self.render_body() + html_close()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        return path

    @staticmethod
    def _html_list(items: list[str], empty_text: str) -> str:
        if not items:
            return f"<p>{ui(empty_text)}</p>"
        return "<ul>" + "".join(f"<li>{esc(item)}</li>" for item in items) + "</ul>"

    @staticmethod
    def _fmt_scalar(v: Any, key: str = "") -> str:
        """Render a leaf value as a badge for status-like values, else text."""
        if isinstance(v, bool):
            return badge("PASS" if v else "FAIL")
        if v is None or v == "":
            return badge("n/a", "gray")
        s = str(v)
        tone = grade_color(s, key=key)
        return badge(s, tone) if tone else esc(s)

    @classmethod
    def _render_list_of_dicts(cls, items: list) -> str:
        keys: list[str] = []
        for it in items:
            for k in it.keys():
                if k not in keys:
                    keys.append(k)
        rows = [[cls._fmt_scalar(it.get(k, ""), k) for k in keys] for it in items]
        return data_table([str(k) for k in keys], rows, detail=True)

    @classmethod
    def _render_obj(cls, obj: Any) -> str:
        """Render nested dict/list data as readable tables instead of raw JSON."""
        if isinstance(obj, dict):
            if not obj:
                return "<p>" + ui(text("None.", "Aucun élément.")) + "</p>"
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
                    rows.append([esc(str(k)), cls._fmt_scalar(v, str(k))])
            table = data_table([text("Field", "Champ"), text("Value", "Valeur")], rows, detail=True) if rows else ""
            return table + "".join(blocks)
        if isinstance(obj, list):
            if obj and all(isinstance(i, dict) for i in obj):
                return cls._render_list_of_dicts(obj)
            return "<p>" + esc(", ".join(str(i) for i in obj) or "\u2014") + "</p>"
        return "<p>" + cls._fmt_scalar(obj) + "</p>"

    @staticmethod
    def _raw_details(obj: Any) -> str:
        """Collapsible raw JSON so no data is lost from the readable view."""
        return ("<details class=\"raw-json\"><summary>" + ui(text("Raw JSON", "JSON brut")) + "</summary><pre>"
                + esc(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
                + "</pre></details>")


_COMPARISON_LINKS_START = "<!-- desktop-comparison-links:start -->"
_COMPARISON_LINKS_END = "<!-- desktop-comparison-links:end -->"


def _desktop_comparison_links(html_path: str) -> str:
    """Discover existing run reports, using paths rather than manifest locations.

    The nearest comparison directory belongs to this output tree. Manifest
    paths may describe the original machine and are intentionally not followed.
    """
    parent = Path(html_path).resolve().parent
    comparison_root = next(
        (p / "desktop_comparisons" for p in (parent, *parent.parents)
         if (p / "desktop_comparisons").is_dir()), None)
    if comparison_root is None:
        return ""
    # Do not follow comparison directories/reports outside their output tree.
    output_root = comparison_root.parent
    if not comparison_root.resolve().is_relative_to(output_root):
        return ""
    rows = []
    for report in sorted(comparison_root.glob("*/comparison.html"), reverse=True):
        if not report.is_file() or not report.resolve().is_relative_to(comparison_root.resolve()):
            continue
        status = text("not_run — capture status unavailable", "not_run — état des captures indisponible")
        manifest_path = report.parent / "manifest.json"
        try:
            if manifest_path.resolve().is_relative_to(comparison_root.resolve()):
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                recorded = manifest.get("status") if isinstance(manifest, dict) else None
                if not isinstance(recorded, str):
                    recorded = None
                status = {
                    "captured": text("Captures recorded — manual review required", "Captures enregistrées — revue manuelle nécessaire"),
                    "failed": text("Capture failure / incomplete — not parity success", "Captures en échec / incomplètes — parité non validée"),
                }.get(recorded, status)
        except (OSError, ValueError):
            pass
        relative = os.path.relpath(report, parent).replace("\\", "/")
        url = esc(quote(relative, safe="/."))
        rows.append([
            f'<a href="{url}">{ui(text("Desktop screenshot comparison", "Comparaison des captures Desktop"))} — {esc(report.parent.name)}</a>',
            ui(status),
        ])
    if not rows:
        return ""
    return (
        _COMPARISON_LINKS_START
        + section_open("quality-desktop-comparisons", text("Desktop screenshot comparisons", "Comparaisons des captures Desktop"), "i")
        + "<p>" + ui(text(
            "Separate capture diagnostics, not a parity verdict. Capture failures are not equivalent reports; even successful captures do not verify data, rendering fidelity, refresh, or deployment.",
            "Diagnostics de capture distincts, sans verdict de parité. Un échec de capture ne signifie pas que les rapports sont équivalents ; même une capture réussie ne valide ni les données, ni la fidélité du rendu, ni l’actualisation, ni le déploiement.")) + "</p>"
        + data_table([text("Comparison report (all workbooks in run)", "Rapport comparatif (tous les classeurs de l’exécution)"), text("Recorded capture evidence", "Preuves de capture enregistrées")],
                     rows, table_id="quality-desktop-comparisons-table")
        + section_close() + _COMPARISON_LINKS_END + "\n"
    )


def refresh_desktop_comparison_links(output_root: str) -> list[str]:
    """Refresh only link sections in existing quality HTML; never run Desktop.

    Replaces the managed section instead of appending duplicates, and removes
    it if no comparison HTML remains. Quality scores and JSON are untouched.
    Returns paths whose HTML changed. Unreadable/unwritable reports are logged.
    """
    import logging

    root = Path(output_root).resolve()
    changed = []
    pattern = re.compile(re.escape(_COMPARISON_LINKS_START) + r".*?"
                         + re.escape(_COMPARISON_LINKS_END) + r"\n?", re.DOTALL)
    anchor = '<div class="container">'
    for path in sorted(root.rglob("*.html")):
        name = path.name.lower()
        if not (name.startswith("migration_quality_") or name == "migration_quality.html"):
            continue
        if not path.resolve().is_relative_to(root):
            continue
        try:
            original = path.read_text(encoding="utf-8")
            document = pattern.sub("", original)
            if anchor not in document:
                continue
            section = _desktop_comparison_links(str(path))
            if section:
                document = document.replace(anchor + "\n", anchor + "\n" + section, 1)
            if document != original:
                path.write_text(document, encoding="utf-8")
                changed.append(str(path))
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).warning("Cannot refresh comparison links in %s: %s", path, exc)
    return changed


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

    html = html_open(text(title, "Qualité de migration consolidée") if title == "Consolidated migration quality" else title,
                     text("Merged deterministic validation across all migrated workbooks",
                          "Validation déterministe consolidée de tous les classeurs migrés"))
    html += _desktop_comparison_links(output_path)
    html += stat_grid([
        stat_card(len(reports), text("Workbooks", "Classeurs")),
        stat_card(counts.get("PASS", 0), text("Pass", "Réussites"), accent="success"),
        stat_card(counts.get("WARN", 0), text("Warnings", "Avertissements"), accent="warn"),
        stat_card(counts.get("FAIL", 0), text("Failures", "Échecs"), accent="fail"),
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
    overview = section_open("consolidated-overview", text("Overview", "Vue d’ensemble"), "i")
    overview += data_table(
        [text("Workbook", "Classeur"), text("Status", "État"), text("Parity", "Parité"),
         text("Blockers", "Blocages"), text("Warnings", "Avertissements"), text("Priorities", "Priorités")],
        overview_rows, table_id="consolidated-overview-table",
        sortable=True, searchable=True)
    overview += section_close()

    tabs = [("overview", text("Overview", "Vue d’ensemble"), True)]
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


#: A failure here is a risk to weigh, not a source feature we cannot carry over.
_ADVISORY_ASSESSMENT_CATEGORIES = {"Performance"}


def _assessment_blocking_failures(assessment: Any) -> list[str]:
    """Name the source incompatibility failures, excluding performance risk alone."""
    failures = []
    for category in getattr(assessment, "categories", []) or []:
        name = getattr(category, "name", "")
        if name in _ADVISORY_ASSESSMENT_CATEGORIES:
            continue
        for check in getattr(category, "checks", []) or []:
            if getattr(check, "severity", "") == "fail":
                failures.append(f"{name} / {getattr(check, 'name', '') or 'check'}")
    return failures


def _assessment_check_findings(assessment: Any, findings: "_Findings") -> None:
    """Raise one finding per warning check instead of one for the whole report.

    A single "contains warnings" line collapsed unrecognised connectors, wide
    schemas and licensing limits into one owner and one action, so a reader
    could not act on any of them. Every check already carries its own detail
    and recommendation; this hands each to the agent that can answer it.
    """
    for category in getattr(assessment, "categories", []) or []:
        name = getattr(category, "name", "") or "Assessment"
        finding_id = _ASSESSMENT_CATEGORY_FINDINGS.get(name, "assessment_warnings")
        advisory = name in _ADVISORY_ASSESSMENT_CATEGORIES
        for check in getattr(category, "checks", []) or []:
            severity = getattr(check, "severity", "")
            # A blocking failure is already listed by finding_id assessment_failures.
            if severity == "fail" and not advisory:
                continue
            if severity not in ("warn", "fail"):
                continue
            detail = getattr(check, "detail", "") or ""
            label = getattr(check, "name", "") or "Check"
            findings.add(
                finding_id,
                f"{name} — {label}" + (f": {detail}" if detail else "."),
                blocker=False,
                fix=getattr(check, "recommendation", "") or "",
            )


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
            # The probe watches the process, not the document: a project with a
            # deleted semantic model still reports "opened".
            "verified": desktop.get("verified", "process_survival"),
        },
        "semantic_execution": "not_run",
        "refresh": "not_run",
        "deployment": "not_run",
    }


#: What the reader must actually do about a finding, and who owns it.
#:
#: Keyed by a stable id recorded where the finding is raised, because the owner
#: used to be guessed by searching the message text for "DAX" or "table" — the
#: same brittle pattern that let a producer and a consumer drift apart elsewhere.
#:
#: repair  something is measurably wrong and the pipeline can fix it
#: decide  a human judgement is required before anything can be done
#: verify  we produced an approximation; confirm it matches intent
#: note    evidence or coverage information; nothing to act on
_FINDING_ACTIONS = {
    "openability": ("repair", "Visual / Orchestrator"),
    "fabric_bundle": ("repair", "Fabric"),
    "fabric_runtime": ("note", "Deployer"),
    "tables_missing": ("repair", "Semantic / Wiring"),
    "hyper_manifest": ("repair", "Wiring / Orchestrator"),
    "hyper_sampling": ("decide", "Wiring / domain owner"),
    "hyper_import": ("repair", "Wiring / Orchestrator"),
    "parameter_coverage": ("repair", "Semantic"),
    "semantic_static": ("repair", "Semantic / DAX"),
    "semantic_runtime": ("repair", "Semantic / DAX"),
    "assessment_failures": ("decide", "Assessor"),
    "assessment_red": ("decide", "Assessor"),
    "assessment_warnings": ("decide", "Assessor"),
    # Assessment warnings, one entry per kind of warning rather than one for the
    # whole report. Connector, volume and licensing warnings are environment
    # decisions nobody but the deploying tenant can make; conversion warnings
    # ask the owning agent to confirm its approximation; the rest is context.
    "assessment_connector": ("decide", "Deployer"),
    "assessment_connection_security": ("decide", "Deployer"),
    "assessment_extract": ("decide", "Wiring"),
    "assessment_volume": ("decide", "Deployer"),
    "assessment_licensing": ("decide", "Deployer"),
    "assessment_calculation": ("verify", "DAX"),
    "assessment_prep": ("verify", "Wiring"),
    "assessment_visual": ("verify", "Visual"),
    "assessment_interaction": ("verify", "Visual"),
    "assessment_model": ("note", "Semantic"),
    "assessment_performance": ("note", "Semantic"),
    "assessment_scope": ("note", "Assessor"),
    "assessment_functionality": ("note", "Evidence"),
    "parity_unsupported": ("decide", "Assessor / domain owner"),
    "filter_coverage": ("verify", "Visual"),
    "m_fallback": ("verify", "Wiring"),
    "visual_approximation": ("verify", "Visual"),
    "parity_untracked": ("note", "Evidence"),
    "unresolved_lineage": ("note", "Evidence"),
    # Preceptorship review. Coaching items are the only findings that say *how*
    # to fix something, so they enter the queue as repairs owned by the agent
    # that owns the artifact. Visual equivalence is a similarity judgement
    # rather than a defect, so it asks for confirmation instead.
    "review_escalated": ("decide", "Reviewer"),
    "review_completeness": ("repair", "Orchestrator"),
    "review_dax_correctness": ("repair", "DAX"),
    "review_m_query_validity": ("repair", "Wiring"),
    "review_tmdl_structure": ("repair", "Semantic"),
    "review_pbir_fidelity": ("repair", "Visual"),
    "review_visual_equivalence": ("verify", "Visual"),
}

#: Which finding a warning becomes, chosen by its assessment category.
#:
#: The category is the only field that reliably says what *kind* of problem a
#: check reports; check names are free text and vary per connector, so matching
#: on them would drift the moment a new connector appears. An unmapped category
#: falls back to the generic assessment_warnings entry above.
_ASSESSMENT_CATEGORY_FINDINGS = {
    "Datasource Compatibility": "assessment_connector",
    "Connection String Security": "assessment_connection_security",
    "Data Extracts & Packaging": "assessment_extract",
    "Data Volume": "assessment_volume",
    "Licensing": "assessment_licensing",
    "Calculation Readiness": "assessment_calculation",
    "Prep Complexity": "assessment_prep",
    "Visual & Dashboard Coverage": "assessment_visual",
    "Filter & Parameter Complexity": "assessment_interaction",
    "Interactivity & Actions": "assessment_interaction",
    "Data Model Complexity": "assessment_model",
    "Multi-Datasource": "assessment_model",
    "Performance": "assessment_performance",
    "Migration Scope & Effort": "assessment_scope",
    "Functionality Parity": "assessment_functionality",
}

#: Fix what is broken before deciding what to do about what is merely different.
_ACTION_RANK = {"repair": 0, "decide": 1, "verify": 2, "note": 3}

#: P0 must be fixed to ship, P1 needs someone to act, P2 is advisory.
_ACTION_TIER = {"repair": "P1", "decide": "P1", "verify": "P2", "note": "P2"}

#: How the queue reads to a human, and how it is coloured.
_ACTION_LABEL = {"repair": text("fix", "corriger"), "decide": text("decide", "décider"),
                 "verify": text("verify", "vérifier"), "note": text("informational", "information")}
_ACTION_TONE = {"repair": "red", "decide": "yellow", "verify": "blue",
                "note": "gray"}


class _Findings:
    """Collect findings with the action they require, not just their text."""

    def __init__(self):
        self.blockers: list[str] = []
        self.warnings: list[str] = []
        self.records: list[Dict[str, Any]] = []

    def add(self, finding_id: str, message: str, *, blocker: bool,
            fix: str = "", evidence: Optional[list] = None) -> None:
        (self.blockers if blocker else self.warnings).append(message)
        action, owner = _FINDING_ACTIONS.get(finding_id, ("decide", "Assessor"))
        self.records.append({
            "id": finding_id,
            "message": message,
            "blocker": blocker,
            "action_kind": action,
            "owner": owner,
            "fix": fix,
            "evidence": list(evidence or []),
        })

    def by_policy(self, finding_id: str, message: str, verdict: str) -> None:
        """Record according to a configured blocker/warning/ignore verdict."""
        if verdict == "blocker":
            self.add(finding_id, message, blocker=True)
        elif verdict == "warning":
            self.add(finding_id, message, blocker=False)


def _build_priorities(parity: Dict[str, Any], findings: "_Findings"
                      ) -> list[Dict[str, Any]]:
    """Order the remediation queue by what the reader must do about each finding.

    Blockers first, then repair before decide before verify before note. Ties
    keep discovery order, so the queue is stable across runs.
    """
    entries = []
    for order, record in enumerate(findings.records):
        entries.append((
            record["blocker"], record["action_kind"], order,
            record["message"], record["owner"], record.get("evidence", []),
            record.get("fix", ""),
        ))

    for gap in parity.get("gaps", []):
        if gap.get("status") != "unsupported":
            continue
        entries.append((
            False, "decide", len(entries),
            f"Resolve unsupported feature: {gap.get('label', gap.get('key', 'unknown'))}",
            "Assessor / domain owner", list(gap.get("evidence", [])), "",
        ))

    entries.sort(key=lambda e: (not e[0], _ACTION_RANK.get(e[1], 9), e[2]))

    return [{
        "priority": "P0" if blocker else _ACTION_TIER.get(action, "P2"),
        "action_kind": action,
        "owner": owner,
        "action": message,
        "fix": fix,
        "evidence": evidence,
    } for blocker, action, _order, message, owner, evidence, fix in entries]



#: Written beside the project by default; ``--no-preceptor`` opts out.
_REVIEW_FILENAME = "preceptor_report.json"

#: Review outcomes that need someone to act, and whether they block the handoff.
_REVIEW_ESCALATIONS = {"escalated_block": True, "escalated_warn": False}


def _review_evidence(project_dir: str) -> Dict[str, Any]:
    """Read the preceptorship review left beside the project, if available.

    The CLI runs the review by default, with ``--no-preceptor`` as an opt-out.
    A report may still be absent if the review was skipped or unavailable, or
    could not be read; those cases are reported as ``not_run``. Its coaching
    is the only evidence in the report that states *how* to fix a finding, so
    it is preserved verbatim.
    """
    path = os.path.join(project_dir, _REVIEW_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return {"status": "not_run", "coaching": [], "dimensions": {}}

    cycles = data.get("cycles") or []
    coaching = []
    for cycle in cycles:
        for item in cycle.get("coaching_items") or []:
            coaching.append({
                "dimension": item.get("dimension", ""),
                "score": item.get("score"),
                "issue": item.get("issue", ""),
                "fix": item.get("fix", ""),
                "location": item.get("location", ""),
                "example_before": item.get("example_before", ""),
                "example_after": item.get("example_after", ""),
                "cycle": cycle.get("cycle"),
            })

    return {
        "status": data.get("status", "unknown"),
        "score": data.get("final_score"),
        "passed": data.get("final_passed"),
        "cycles": data.get("total_cycles"),
        "escalation_reason": data.get("escalation_reason", ""),
        "dimensions": (cycles[-1].get("scorecard", {}) if cycles else {}),
        "coaching": coaching,
    }


def _record_review_findings(review: Dict[str, Any], findings: "_Findings") -> None:
    """Fold review coaching into the remediation queue, carrying its fix text."""
    status = str(review.get("status", "")).lower()
    if status in _REVIEW_ESCALATIONS:
        reason = review.get("escalation_reason") or "review did not reach the pass score"
        findings.add("review_escalated",
                     f"Preceptorship review escalated: {reason}",
                     blocker=_REVIEW_ESCALATIONS[status])

    for item in review.get("coaching", []):
        dimension = item.get("dimension", "")
        evidence = [item["location"]] if item.get("location") else []
        findings.add(
            f"review_{dimension}",
            f"{item.get('issue', 'Review finding')} ({dimension.replace('_', ' ')})",
            blocker=False,
            fix=item.get("fix", ""),
            evidence=evidence,
        )


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


def _hyper_sampling_evidence(extracted: Dict[str, Any],
                             project_dir: str) -> Dict[str, Any]:
    """Read export limits, not row parity; even a complete manifest is static.

    A missing manifest is a finding only when Hyper is actually in use.
    Invalid or incomplete metadata must never be treated as an unsampled export.
    """
    manifest_name = "Data/hyper_exports.json"
    data_dir = Path(project_dir) / "Data"
    evidence: Dict[str, Any] = {
        "status": "not_found",
        "manifest": manifest_name,
        "full_data_verified": False,
        "sampled_export_count": 0,
        "exports": [],
        "diagnostics": [],
    }
    diagnostics = evidence["diagnostics"]
    uses_hyper = bool(extracted.get("hyper_files")) or any(
        str((ds.get("connection") or {}).get(key, "")).lower()
        in {"hyper", "dataengine"}
        for ds in extracted.get("datasources", []) or []
        for key in ("type", "class")
    ) or any(
        table.get("hyper_identity")
        for ds in extracted.get("datasources", []) or []
        for table in ds.get("tables", []) or []
    )
    manifest_path = data_dir / "hyper_exports.json"
    linked = _hyper_manifest_link(str(data_dir), str(manifest_path))
    if linked:
        # The project is untrusted input: a link here can point at a remote host.
        if uses_hyper:
            evidence["status"] = "invalid"
            diagnostics.append(
                f"{linked} must not be a link; full-data coverage is unverified.")
        return evidence
    try:
        with manifest_path.open(encoding="utf-8") as handle:
            manifest = json.load(handle)
    except FileNotFoundError:
        # Direct quality callers may have artifacts but no source inventory.
        if not uses_hyper:
            uses_hyper, scan_issues = _hyper_project_data_signals(str(data_dir))
            diagnostics.extend(
                f"{issue} Full-data coverage is unverified."
                for issue in scan_issues)
        if uses_hyper:
            diagnostics.append(
                f"Hyper export manifest {manifest_name} is missing; "
                "sampled or capped data cannot be ruled out.")
        return evidence
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        evidence["status"] = "invalid"
        diagnostics.append(
            f"Hyper export manifest {manifest_name} cannot be read "
            f"({type(exc).__name__}); full-data coverage is unverified.")
        return evidence

    if (not isinstance(manifest, dict)
            or type(manifest.get("version")) is not int
            or manifest["version"] != 1
            or not isinstance(manifest.get("exports"), list)):
        evidence["status"] = "invalid"
        diagnostics.append(
            f"Hyper export manifest {manifest_name} requires version 1 and "
            "an exports list; full-data coverage is unverified.")
        return evidence

    evidence["status"] = "static_evidence"
    if uses_hyper and not manifest["exports"]:
        diagnostics.append(
            "Hyper export manifest has no export records for the source "
            "extracts; full-data coverage is unverified.")
    for index, record in enumerate(manifest["exports"]):
        if not isinstance(record, dict):
            diagnostics.append(f"Hyper export record {index} must be an object.")
            continue
        invalid = []
        for key in ("row_count", "total_rows", "max_rows"):
            value = record.get(key)
            if (key not in record
                    or (value is None and key == "row_count")
                    or (value is not None and (
                        type(value) is not int
                        or value < (1 if key == "max_rows" else 0)))):
                invalid.append(key)
        for key in ("sampled", "truncated"):
            value = record.get(key)
            if (key not in record
                    or (type(value) is not bool
                        and not (key == "truncated" and value is None))):
                invalid.append(key)
        rows, total, cap = (record.get(key) for key in
                            ("row_count", "total_rows", "max_rows"))
        counts_valid = not any(key in invalid for key in
                               ("row_count", "total_rows", "max_rows"))
        if counts_valid and (
                (total is not None and rows > total)
                or (cap is not None and rows > cap)):
            invalid.append("inconsistent row counts")
        if invalid:
            diagnostics.append(
                f"Hyper export record {index} has missing or invalid "
                f"sampling metadata: {', '.join(invalid)}; "
                "full-data coverage is unverified.")
        limited = (
            record.get("sampled") is True or record.get("truncated") is True
            or (type(cap) is int and cap > 0)
            or (counts_valid and total is not None and rows < total)
        )
        identity = record.get("identity")
        evidence["exports"].append({
            "table": identity.get("table") if isinstance(identity, dict) else None,
            "filename": record.get("filename") or record.get("csv_filename"),
            **{key: record.get(key) for key in (
                "row_count", "total_rows", "max_rows", "sampled", "truncated",
                "sampling_method")},
            "limited": limited,
        })
        evidence["sampled_export_count"] += int(limited)
    if diagnostics:
        evidence["status"] = "invalid"
    return evidence


_HYPER_PACKAGED_IDENTITY_FIELDS = (
    "workbook", "archive_member", "schema", "table",
)
_HYPER_LOCAL_IDENTITY_FIELDS = ("source_file", "schema", "table")
_HYPER_MANIFEST = "Data/hyper_exports.json"
_HYPER_EXPORT_FILE_RE = re.compile(
    r"hyper_(?:staged_)?[0-9a-f]{64}\.(?:csv|parquet)$", re.IGNORECASE)
_HYPER_TMDL_TABLE_RE = re.compile(
    r"(?m)^table[ \t]+('(?:[^']|'')*'|[^\r\n]+?)[ \t]*$")
_HYPER_ANNOTATION_RE = re.compile(
    r"^[ \t]+annotation[ \t]+Migration_HyperIdentity[ \t]*="
    r"[ \t]*(.*?)[ \t]*\r?$", re.MULTILINE)
_HYPER_PARTITION_RE = re.compile(
    r"^\tpartition[ \t]+(.+?)[ \t]*=[ \t]*(\w+)[ \t]*$")
_HYPER_PARTITION_MODE_RE = re.compile(
    r"^\t\tmode[ \t]*:[ \t]*(\S+)[ \t]*$")
_HYPER_DATA_FOLDER_DECL_RE = re.compile(
    r"^expression[ \t]+(?:'DataFolder'|DataFolder)[ \t]*=")
_HYPER_DATA_FOLDER_LITERAL_RE = re.compile(
    r'^expression[ \t]+(?:\'DataFolder\'|DataFolder)[ \t]*=[ \t]*'
    r'("(?:[^"]|"")*")(?:[ \t]+meta\b.*)?[ \t]*$')


def _hyper_identity_key(identity: Any) -> Optional[tuple[str, ...]]:
    """Return the exact packaged identity key used by the migration binder.

    Keep this local instead of importing ``migrate``: quality is also imported
    by the orchestration module, and workbook captions or filenames must never
    be used to reconstruct the physical archive identity.
    """
    if (not isinstance(identity, dict)
            or set(identity) != set(_HYPER_PACKAGED_IDENTITY_FIELDS)):
        return None
    values: list[str] = []
    for field_name in _HYPER_PACKAGED_IDENTITY_FIELDS:
        value = identity.get(field_name)
        if not isinstance(value, str) or not value:
            return None
        values.append(value)
    return tuple(values)


def _local_hyper_identity_key(identity: Any) -> Optional[tuple[str, ...]]:
    """Return an explicit loose-file identity without inventing an archive member.

    ``source_file`` is project metadata, so it is compared lexically and never
    resolved through the filesystem.
    """
    if (not isinstance(identity, dict)
            or set(identity) != set(_HYPER_LOCAL_IDENTITY_FIELDS)):
        return None
    source_file = identity.get("source_file")
    schema = identity.get("schema")
    table = identity.get("table")
    if not isinstance(source_file, str) or not source_file:
        return None
    if not isinstance(schema, str) or not schema:
        return None
    if not isinstance(table, str) or not table:
        return None
    if "\x00" in source_file or not os.path.isabs(source_file):
        return None
    try:
        normalized_source = lexical_path_key(source_file)
    except (OSError, ValueError):
        return None
    return ("local", normalized_source, schema, table)


def _hyper_binding_identity_key(identity: Any) -> Optional[tuple[str, ...]]:
    """Return a namespaced packaged or explicitly declared local identity."""
    packaged = _hyper_identity_key(identity)
    if packaged is not None:
        return ("packaged",) + packaged
    return _local_hyper_identity_key(identity)


def _hyper_identity_payload(identity: Any) -> Any:
    """Return a stable, field-ordered identity payload for evidence JSON."""
    if _hyper_identity_key(identity) is not None:
        return {field: identity[field] for field in _HYPER_PACKAGED_IDENTITY_FIELDS}
    if _local_hyper_identity_key(identity) is not None:
        return {field: identity[field] for field in _HYPER_LOCAL_IDENTITY_FIELDS}
    return identity


def _hyper_identity_label(identity: Any) -> str:
    if not isinstance(identity, dict):
        return "identity unavailable"
    schema, table = identity.get("schema"), identity.get("table")
    if isinstance(schema, str) and isinstance(table, str):
        return f"{schema}.{table}"
    return "identity unavailable"


@dataclass(frozen=True)
class _HyperMToken:
    """One small M lexical token; strings and comments stay distinguishable."""

    kind: str
    value: str
    start: int = -1
    end: int = -1


class _HyperMParseError(ValueError):
    """Raised when M structure cannot be proven by the static binding reader."""


def _hyper_m_word(token: _HyperMToken, value: Optional[str] = None) -> bool:
    """Match M keywords while allowing tokens hidden in an inactive branch."""
    if token.kind not in {"word", "inactive"}:
        return False
    return value is None or token.value.casefold() == value.casefold()


def _hyper_m_active_word(token: _HyperMToken, value: str) -> bool:
    """Match an active M identifier, excluding tokens in constant dead branches."""
    return token.kind == "word" and token.value.casefold() == value.casefold()


def _tokenize_hyper_m(source: str) -> list[_HyperMToken]:
    """Tokenize the small M surface used for Import file binding proofs.

    Strings and quoted identifiers remain opaque tokens; line and nested block
    comments are discarded so filenames mentioned only in comments cannot
    authorize a partition.
    """
    tokens: list[_HyperMToken] = []
    index, length = 0, len(source)
    while index < length:
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            start = index
            depth = 1
            index += 2
            while index < length and depth:
                if source.startswith("/*", index):
                    depth += 1
                    index += 2
                elif source.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise _HyperMParseError(
                    f"unterminated block comment at character {start}")
            continue
        if source.startswith('#"', index):
            start = index
            index += 2
            value = []
            while index < length:
                if source[index] == '"':
                    if index + 1 < length and source[index + 1] == '"':
                        value.append('"')
                        index += 2
                        continue
                    index += 1
                    break
                value.append(source[index])
                index += 1
            else:
                raise _HyperMParseError(
                    f"unterminated quoted identifier at character {start}")
            tokens.append(_HyperMToken("qident", "".join(value), start, index))
            continue
        if char == '"':
            start = index
            index += 1
            value = []
            while index < length:
                if source[index] == '"':
                    if index + 1 < length and source[index + 1] == '"':
                        value.append('"')
                        index += 2
                        continue
                    index += 1
                    break
                value.append(source[index])
                index += 1
            else:
                raise _HyperMParseError(
                    f"unterminated text literal at character {start}")
            tokens.append(_HyperMToken("string", "".join(value), start, index))
            continue
        if char == "#" and index + 1 < length and (
                source[index + 1].isalpha() or source[index + 1] == "_"):
            start = index
            index += 2
            while index < length and (
                    source[index].isalnum() or source[index] == "_"):
                index += 1
            tokens.append(_HyperMToken("word", source[start:index], start, index))
            continue
        if char.isalpha() or char == "_":
            start = index
            index += 1
            while index < length and (
                    source[index].isalnum() or source[index] == "_"):
                index += 1
            tokens.append(_HyperMToken("word", source[start:index], start, index))
            continue
        if char.isdigit():
            start = index
            index += 1
            while index < length and (
                    source[index].isalnum() or source[index] in "._"):
                index += 1
            tokens.append(_HyperMToken("number", source[start:index], start, index))
            continue
        tokens.append(_HyperMToken("symbol", char, index, index + 1))
        index += 1
    return tokens


def _hyper_m_delimiters(tokens: list[_HyperMToken], stop: int) -> list[int]:
    """Return the open-delimiter stack immediately before ``stop``."""
    stack: list[int] = []
    opening = {"(": ")", "[": "]", "{": "}"}
    closing = {value: key for key, value in opening.items()}
    for index, token in enumerate(tokens[:stop]):
        if token.kind != "symbol":
            continue
        if token.value in opening:
            stack.append(index)
        elif token.value in closing:
            if not stack or tokens[stack[-1]].value != closing[token.value]:
                raise _HyperMParseError(
                    f"unbalanced closing delimiter at token {index}")
            stack.pop()
    return stack


def _hyper_m_conditional_bounds(tokens: list[_HyperMToken], start: int
                                ) -> tuple[int, int]:
    """Find a matching top-level ``then`` and ``else`` for one M if."""
    stack = _hyper_m_delimiters(tokens, start)
    base = list(stack)
    nested_ifs = 0
    then_index = -1
    for index in range(start + 1, len(tokens)):
        token = tokens[index]
        if token.kind == "symbol":
            if token.value in "([{":
                stack.append(index)
            elif token.value in ")]}":
                if not stack or tokens[stack[-1]].value != {
                    ")": "(", "]": "[", "}": "{",
                }[token.value]:
                    raise _HyperMParseError(
                        f"unbalanced delimiter at token {index}")
                stack.pop()
            continue
        if stack != base:
            continue
        if _hyper_m_word(token, "if"):
            nested_ifs += 1
        elif _hyper_m_word(token, "then"):
            if then_index < 0:
                then_index = index
        elif _hyper_m_word(token, "else"):
            if nested_ifs:
                nested_ifs -= 1
            elif then_index >= 0:
                return then_index, index
    raise _HyperMParseError(
        f"conditional at token {start} has no matching then/else")


def _hyper_m_else_end(tokens: list[_HyperMToken], else_index: int) -> int:
    """Return a conservative end for an else expression at its delimiter level."""
    base = _hyper_m_delimiters(tokens, else_index)
    stack = list(base)
    nested_lets = 0
    nested_ifs = 0
    opening = {"(": ")", "[": "]", "{": "}"}
    closing = {value: key for key, value in opening.items()}
    for index in range(else_index + 1, len(tokens)):
        token = tokens[index]
        if token.kind == "symbol":
            if token.value in opening:
                stack.append(index)
            elif token.value in closing:
                if not stack or tokens[stack[-1]].value != closing[token.value]:
                    raise _HyperMParseError(
                        f"unbalanced delimiter at token {index}")
                if len(stack) == len(base):
                    return index
                stack.pop()
            elif token.value == "," and stack == base and nested_lets == 0:
                return index
            continue
        if stack != base:
            continue
        if _hyper_m_word(token, "let"):
            nested_lets += 1
        elif _hyper_m_word(token, "in"):
            if nested_lets:
                nested_lets -= 1
            else:
                return index
        elif _hyper_m_word(token, "if"):
            nested_ifs += 1
        elif _hyper_m_word(token, "else"):
            if nested_ifs:
                nested_ifs -= 1
            else:
                return index
    return len(tokens)


def _hyper_m_hide_constant_dead_branches(
        tokens: list[_HyperMToken]) -> list[_HyperMToken]:
    """Hide literal-boolean dead branches without evaluating general M.

    Unknown conditions are left intact. A conservative expression boundary
    can hide more than the dead branch, which can only cause a missing-binding
    failure; it cannot make an unreachable source file count as active.
    """
    dead: set[int] = set()
    for index, token in enumerate(tokens):
        if not _hyper_m_word(token, "if") or index in dead:
            continue
        try:
            then_index, else_index = _hyper_m_conditional_bounds(tokens, index)
        except _HyperMParseError:
            continue
        condition = tokens[index + 1:then_index]
        if (len(condition) == 1 and condition[0].kind == "word"
                and condition[0].value.casefold() in {"true", "false"}):
            if condition[0].value.casefold() == "false":
                dead.update(range(then_index + 1, else_index))
            else:
                dead.update(range(else_index + 1,
                                  _hyper_m_else_end(tokens, else_index)))
    return [
        _HyperMToken(
            "inactive" if index in dead and token.kind != "symbol"
            else token.kind,
            token.value, token.start, token.end,
        )
        for index, token in enumerate(tokens)
    ]


def _hyper_m_root_let(tokens: list[_HyperMToken]
                      ) -> Optional[tuple[int, Dict[str, list[_HyperMToken]],
                                          list[_HyperMToken]]]:
    """Parse a root M let expression and return bindings plus its body."""
    if not tokens or not _hyper_m_word(tokens[0], "let"):
        return None
    stack: list[int] = []
    nested_lets = 0
    in_index = -1
    opening = {"(": ")", "[": "]", "{": "}"}
    closing = {value: key for key, value in opening.items()}
    for index in range(1, len(tokens)):
        token = tokens[index]
        if token.kind == "symbol":
            if token.value in opening:
                stack.append(index)
            elif token.value in closing:
                if not stack or tokens[stack[-1]].value != closing[token.value]:
                    raise _HyperMParseError(
                        f"unbalanced delimiter at token {index}")
                stack.pop()
            continue
        if stack:
            continue
        if _hyper_m_word(token, "let"):
            nested_lets += 1
        elif _hyper_m_word(token, "in"):
            if nested_lets:
                nested_lets -= 1
            else:
                in_index = index
                break
    if in_index < 0:
        raise _HyperMParseError("root let expression has no matching in")

    bindings: Dict[str, list[_HyperMToken]] = {}
    segment_start = 1
    stack = []
    nested_lets = 0
    segments = []
    for index in range(1, in_index + 1):
        token = tokens[index]
        if token.kind == "symbol":
            if token.value in opening:
                stack.append(index)
            elif token.value in closing:
                if not stack or tokens[stack[-1]].value != closing[token.value]:
                    raise _HyperMParseError(
                        f"unbalanced delimiter at token {index}")
                stack.pop()
            elif token.value == "," and not stack and nested_lets == 0:
                segments.append(tokens[segment_start:index])
                segment_start = index + 1
            continue
        if stack:
            continue
        if index < in_index and _hyper_m_word(token, "let"):
            nested_lets += 1
        elif index < in_index and _hyper_m_word(token, "in") and nested_lets:
            nested_lets -= 1
    segments.append(tokens[segment_start:in_index])
    for segment in segments:
        if (len(segment) < 2
                or segment[0].kind not in {"word", "qident"}
                or segment[1].kind != "symbol"
                or segment[1].value != "="):
            raise _HyperMParseError("root let contains an unsupported binding")
        name = segment[0].value
        if name in bindings:
            raise _HyperMParseError(
                f"root let repeats binding {name!r}")
        bindings[name] = segment[2:]
    return in_index, bindings, tokens[in_index + 1:]


def _hyper_m_references(tokens: list[_HyperMToken],
                        names: set[str]) -> set[str]:
    """Find simple lexical references to root let names, not record fields."""
    found: set[str] = set()
    square_depth = 0
    for index, token in enumerate(tokens):
        if token.kind == "symbol":
            if token.value == "[":
                square_depth += 1
            elif token.value == "]" and square_depth:
                square_depth -= 1
            continue
        if (square_depth or token.kind not in {"word", "qident"}
                or token.value not in names):
            continue
        previous = tokens[index - 1] if index else None
        if previous and previous.kind == "symbol" and previous.value == ".":
            continue
        found.add(token.value)
    return found


def _hyper_m_file_calls(tokens: list[_HyperMToken]
                        ) -> list[tuple[int, int, list[_HyperMToken]]]:
    """Find direct reachable ``File.Contents(...)`` calls in a token stream."""
    calls = []
    for index in range(len(tokens) - 3):
        if not (_hyper_m_active_word(tokens[index], "File")
                and tokens[index + 1].kind == "symbol"
                and tokens[index + 1].value == "."
                and _hyper_m_active_word(tokens[index + 2], "Contents")
                and tokens[index + 3].kind == "symbol"
                and tokens[index + 3].value == "("):
            continue
        stack = []
        close_index = -1
        for cursor in range(index + 3, len(tokens)):
            token = tokens[cursor]
            if token.kind != "symbol":
                continue
            if token.value in "([{":
                stack.append(token.value)
            elif token.value in ")]}":
                if (not stack or stack[-1] != {
                    ")": "(", "]": "[", "}": "{",
                }[token.value]):
                    raise _HyperMParseError(
                        f"File.Contents call has unbalanced delimiter at token {cursor}")
                stack.pop()
                if not stack:
                    close_index = cursor
                    break
        if close_index < 0:
            raise _HyperMParseError(
                f"File.Contents call at token {index} is unterminated")
        argument_end = close_index
        argument_stack = []
        for cursor in range(index + 4, close_index):
            token = tokens[cursor]
            if token.kind != "symbol":
                continue
            if token.value in "([{":
                argument_stack.append(token.value)
            elif token.value in ")]}":
                if argument_stack:
                    argument_stack.pop()
            elif token.value == "," and not argument_stack:
                argument_end = cursor
                break
        calls.append((index, close_index,
                      tokens[index + 4:argument_end]))
    return calls


def _hyper_m_active_segments(source: str) -> list[list[_HyperMToken]]:
    """Return the terminal expression plus its reachable root-let bindings."""
    tokens = _hyper_m_hide_constant_dead_branches(_tokenize_hyper_m(source))
    parsed = _hyper_m_root_let(tokens)
    if parsed is None:
        if any(_hyper_m_word(token, "let") for token in tokens):
            if _hyper_m_file_calls(tokens):
                raise _HyperMParseError(
                    "file reads inside a non-root or nested let are not "
                    "statically resolved")
        return [tokens]

    _in_index, bindings, body = parsed
    names = set(bindings)
    reachable = _hyper_m_references(body, names)
    pending = list(sorted(reachable))
    while pending:
        name = pending.pop()
        newly_referenced = _hyper_m_references(bindings[name], names) - reachable
        reachable.update(newly_referenced)
        pending.extend(sorted(newly_referenced))

    active_segments = [body] + [
        bindings[name] for name in sorted(reachable)
    ]
    for segment in active_segments:
        if any(_hyper_m_word(token, "let") for token in segment):
            if _hyper_m_file_calls(segment):
                raise _HyperMParseError(
                    "file reads inside a nested let are not statically resolved")
    return active_segments


def _hyper_m_active_file_calls(source: str
                               ) -> list[tuple[int, int, list[_HyperMToken]]]:
    """Return file reads in reachable let bindings and the final expression."""
    calls: list[tuple[int, int, list[_HyperMToken]]] = []
    for segment in _hyper_m_active_segments(source):
        calls.extend(_hyper_m_file_calls(segment))
    return calls


def _hyper_m_conditional_source_issues(source: str) -> list[str]:
    """Do not treat a conditionally unused file binding as the final source."""
    tokens = _hyper_m_hide_constant_dead_branches(_tokenize_hyper_m(source))
    parsed = _hyper_m_root_let(tokens)
    if parsed is None:
        segments = [tokens]
        file_binding_names: set[str] = set()
    else:
        _in_index, bindings, body = parsed
        names = set(bindings)
        dependencies = {
            name: _hyper_m_references(value, names)
            for name, value in bindings.items()
        }
        reachable = _hyper_m_references(body, names)
        pending = list(reachable)
        while pending:
            name = pending.pop()
            new_names = dependencies[name] - reachable
            reachable.update(new_names)
            pending.extend(new_names)
        file_binding_names = {
            name for name, value in bindings.items()
            if name in reachable and _hyper_m_file_calls(value)
        }
        changed = True
        while changed:
            changed = False
            for name, deps in dependencies.items():
                if (name in reachable and name not in file_binding_names
                        and deps & file_binding_names):
                    file_binding_names.add(name)
                    changed = True
        segments = [body] + [
            bindings[name] for name in sorted(reachable)
        ]

    issues = []
    for segment in segments:
        for index, token in enumerate(segment):
            if not _hyper_m_active_word(token, "if"):
                continue
            try:
                then_index, else_index = _hyper_m_conditional_bounds(
                    segment, index)
                else_end = _hyper_m_else_end(segment, else_index)
            except _HyperMParseError:
                issues.append("M conditional structure is not statically resolved")
                continue
            condition = segment[index + 1:then_index]
            if (len(condition) == 1 and condition[0].kind == "word"
                    and condition[0].value.casefold() in {"true", "false"}):
                continue
            branches = (
                segment[then_index + 1:else_index],
                segment[else_index + 1:else_end],
            )
            condition_refs = _hyper_m_references(
                condition, file_binding_names)
            branch_refs = set().union(*(
                _hyper_m_references(branch, file_binding_names)
                for branch in branches
            ))
            if condition_refs and not branch_refs:
                issues.append(
                    "file-backed let binding is used only to choose an "
                    "unverified conditional result")
            if any(_hyper_m_file_calls(branch) for branch in branches):
                issues.append(
                    "File.Contents is inside a condition whose selected branch "
                    "cannot be proven")
                continue
            if file_binding_names and any(
                    _hyper_m_references(branch, file_binding_names)
                    for branch in branches):
                issues.append(
                    "a file-backed let binding is conditionally selected against "
                    "an unverified alternative")
    return sorted(set(issues))


def _hyper_safe_basename(value: Any) -> bool:
    """Accept only one ordinary filename component, never a link or traversal."""
    if (not isinstance(value, str) or not value or "\x00" in value
            or value in {".", ".."} or "/" in value or "\\" in value
            or os.path.basename(value) != value or ntpath.basename(value) != value):
        return False
    return True


def _hyper_realpath(path: str) -> str:
    """Resolve a path the operator chose, never a value read from metadata.

    Only the project's own Data directory and artifacts already verified not to
    be links reach this function; metadata paths use ``lexical_path_key``.
    """
    return canonical_path_key(path)


def _hyper_trusted_dir_keys(directory: str) -> tuple[str, str]:
    """Return the lexical and resolved keys of an operator-chosen directory."""
    return lexical_path_key(directory), _hyper_realpath(directory)


def _hyper_trusted_file_keys(directory: str, filename: Any) -> frozenset[str]:
    """Return the keys naming one manifest export inside the verified directory.

    Only the directory is resolved. The file name is a checked basename and the
    file itself may be a link, so it is never handed to the filesystem.
    """
    if not _hyper_safe_basename(filename):
        return frozenset()
    return frozenset(
        lexical_path_key(os.path.join(key, filename))
        for key in _hyper_trusted_dir_keys(directory))


def _hyper_is_link(path: str, info: os.stat_result) -> bool:
    """Reject symlinks, Windows reparse points, and multiply-linked files."""
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    return (
        stat.S_ISLNK(info.st_mode)
        or bool(reparse and attributes & reparse)
        or (stat.S_ISREG(info.st_mode) and getattr(info, "st_nlink", 1) > 1)
        or os.path.islink(path)
    )


def _hyper_manifest_link(data_dir: str, manifest_path: str) -> Optional[str]:
    """Name a link met on the way to the manifest, without following it."""
    for label, path in (("Project Data path", data_dir),
                        ("Hyper export manifest", manifest_path)):
        try:
            info = os.lstat(path)
        except OSError:
            return None
        if _hyper_is_link(path, info):
            return label
    return None


def _hyper_file_digest(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size_bytes += len(chunk)
    return digest.hexdigest(), size_bytes


def _hyper_csv_evidence(path: str, expected_columns: list[str],
                        expected_rows: int) -> tuple[bool, Optional[int], str]:
    """Count CSV records incrementally and verify the physical header/schema."""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            header = next(reader, None)
            if header is None or not header:
                return False, None, "CSV has no physical header row"
            if header != expected_columns:
                return False, None, (
                    "CSV header does not match the export's declared physical columns")
            row_count = 0
            for row_count, row in enumerate(reader, start=1):
                if len(row) != len(header):
                    return False, row_count - 1, (
                        f"CSV record {row_count} has {len(row)} fields; "
                        f"the declared schema has {len(header)}")
            if row_count != expected_rows:
                return False, row_count, (
                    f"CSV contains {row_count} data rows; manifest declares "
                    f"{expected_rows}")
            return True, row_count, ""
    except (OSError, UnicodeError, csv.Error) as exc:
        return False, None, (
            f"CSV cannot be streamed for header/count verification "
            f"({type(exc).__name__})")


def _hyper_record_proof(record: Dict[str, Any], index: int
                        ) -> tuple[Optional[tuple[str, ...]], list[str]]:
    """Validate manifest-only proof fields and return an exact identity key."""
    issues: list[str] = []
    identity = record.get("identity")
    key = _hyper_binding_identity_key(identity)
    if key is None:
        issues.append("identity must be an exact packaged or explicit local identity")
    record_format = record.get("format")
    if not isinstance(record_format, str) or record_format not in {"csv", "parquet"}:
        issues.append("format must be csv or parquet")
    file_name = record.get("filename")
    if not isinstance(file_name, str) or not _hyper_safe_basename(file_name):
        issues.append("filename must be a safe basename without links or traversal")
    elif os.path.splitext(file_name)[1].lower() != (
            ".csv" if record.get("format") == "csv" else ".parquet"):
        issues.append("filename extension does not match format")
    columns = record.get("columns")
    if (not isinstance(columns, list) or not columns
            or not all(isinstance(column, str) for column in columns)
            or len(set(columns)) != len(columns)):
        issues.append("columns must be a nonempty list of unique strings")
    for field_name in ("row_count", "total_rows"):
        value = record.get(field_name)
        if type(value) is not int or value < 0:
            issues.append(f"{field_name} must be a nonnegative nonboolean integer")
    if (type(record.get("row_count")) is int
            and type(record.get("total_rows")) is int
            and record.get("row_count") != record.get("total_rows")):
        issues.append("row_count must equal total_rows")
    if "max_rows" not in record or record.get("max_rows") is not None:
        issues.append("max_rows must be present and null for a complete export")
    if record.get("sampled") is not False:
        issues.append("sampled must be false for a complete export")
    if record.get("truncated") is not False:
        issues.append("truncated must be false for a complete export")
    if record.get("full_extract_verified") is not True:
        issues.append("full_extract_verified proof is missing")
    expected_count_source = {
        "csv": "csv_writer",
        "parquet": "hyper_copy",
    }.get(record_format) if isinstance(record_format, str) else None
    if record.get("row_count_source") != expected_count_source:
        issues.append(
            "row_count_source must identify the measured CSV writer or native "
            "Parquet COPY count")
    digest = record.get("sha256")
    if (not isinstance(digest, str) or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)):
        issues.append("sha256 must be a lowercase 64-character digest")
    size_bytes = record.get("size_bytes")
    if type(size_bytes) is not int or size_bytes <= 0:
        issues.append("size_bytes must be a positive nonboolean integer")
    return key, issues


def _hyper_annotation_identity(raw: str, relative_path: str
                               ) -> tuple[Optional[Any], Optional[str]]:
    try:
        identity = json.loads(raw)
    except json.JSONDecodeError:
        return None, (
            f"{relative_path}: Migration_HyperIdentity is not valid JSON")
    if _hyper_binding_identity_key(identity) is None:
        return identity, (
            f"{relative_path}: Migration_HyperIdentity is not an exact packaged "
            "or explicit local identity")
    return identity, None


def _hyper_tmdl_partition_blocks(content: str
                                 ) -> list[dict[str, Any]]:
    """Read final partition kinds and modes from actual TMDL declarations."""
    lines = content.splitlines()
    blocks = []
    starts = []
    for index, line in enumerate(lines):
        match = _HYPER_PARTITION_RE.match(line)
        if match:
            name = match.group(1).strip()
            if name.startswith("'") and name.endswith("'"):
                name = name[1:-1].replace("''", "'")
            starts.append((index, name, match.group(2).casefold()))
    for position, (start, name, kind) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        modes = [
            match.group(1).casefold()
            for line in lines[start + 1:end]
            if (match := _HYPER_PARTITION_MODE_RE.match(line))
        ]
        blocks.append({
            "name": name,
            "kind": kind,
            "modes": modes,
            "line": start + 1,
        })
    return blocks


def _hyper_data_folder(model_definition: str, project_root: str,
                       expected_data_dir: str
                       ) -> tuple[Optional[str], Optional[str]]:
    """Match one authored DataFolder literal against the project's Data dir.

    The literal is project metadata: it is compared lexically with the keys of
    the operator-chosen directory and is never resolved or opened.
    """
    expressions_path = os.path.join(model_definition, "expressions.tmdl")
    try:
        expressions_info = os.lstat(expressions_path)
        if (_hyper_is_link(expressions_path, expressions_info)
                or not stat.S_ISREG(expressions_info.st_mode)):
            return None, (
                "expressions.tmdl is not a regular file; DataFolder is unresolved")
        with open(expressions_path, "r", encoding="utf-8-sig") as stream:
            lines = stream.read().splitlines()
    except FileNotFoundError:
        return None, "expressions.tmdl is missing; DataFolder is unresolved"
    except (OSError, UnicodeError) as exc:
        return None, (
            f"expressions.tmdl cannot be read ({type(exc).__name__}); "
            "DataFolder is unresolved")

    declarations = []
    for line in lines:
        stripped = line.strip()
        if not _HYPER_DATA_FOLDER_DECL_RE.match(stripped):
            continue
        match = _HYPER_DATA_FOLDER_LITERAL_RE.match(stripped)
        if not match:
            declarations.append(None)
            continue
        raw = match.group(1)[1:-1].replace('""', '"')
        raw = raw.replace("\\\\", "\\").replace('\\"', '"')
        declarations.append(raw)
    if len(declarations) != 1 or not isinstance(declarations[0], str):
        return None, (
            "expressions.tmdl must contain one literal DataFolder declaration")
    value = declarations[0]
    if "\x00" in value:
        return None, "DataFolder contains a NUL character"
    if not (os.path.isabs(value) or ntpath.isabs(value)):
        value = os.path.join(project_root, value)
    lexical_key, resolved = _hyper_trusted_dir_keys(expected_data_dir)
    if (lexical_path_key(value) not in (lexical_key, resolved)
            or not os.path.isdir(resolved)):
        return None, (
            "DataFolder does not resolve to the existing project Data directory")
    return resolved, None


def _hyper_m_path(argument: list[_HyperMToken], data_folder: Optional[str],
                  project_root: str) -> tuple[Optional[str], str]:
    """Resolve the narrow literal or DataFolder path forms used by generated M."""
    while (len(argument) >= 2 and argument[0].kind == "symbol"
           and argument[0].value == "(" and argument[-1].kind == "symbol"
           and argument[-1].value == ")"):
        depth = 0
        encloses = True
        for index, token in enumerate(argument):
            if token.kind != "symbol":
                continue
            if token.value == "(":
                depth += 1
            elif token.value == ")":
                depth -= 1
                if depth == 0 and index != len(argument) - 1:
                    encloses = False
                    break
        if not encloses:
            break
        argument = argument[1:-1]
    value: Optional[str] = None
    via = "literal"
    if len(argument) == 1 and argument[0].kind == "string":
        value = argument[0].value
    elif (len(argument) == 3 and _hyper_m_word(argument[0], "DataFolder")
          and argument[1].kind == "symbol" and argument[1].value == "&"
          and argument[2].kind == "string"):
        if data_folder is None:
            return None, "DataFolder cannot be resolved"
        suffix = argument[2].value.lstrip("\\/")
        value = os.path.join(data_folder, suffix)
        via = "DataFolder"
    else:
        return None, "File.Contents path is not a supported literal or DataFolder expression"
    if not value or "\x00" in value:
        return None, "File.Contents path is empty or contains a NUL character"
    if os.path.isabs(value) or ntpath.isabs(value):
        return lexical_path_key(value), via
    return None, "relative File.Contents path must use the declared DataFolder"


def _hyper_m_call_arguments(tokens: list[_HyperMToken], qualifier: str,
                            method: str
                            ) -> list[list[list[_HyperMToken]]]:
    """Return positional argument token lists for direct dotted M calls."""
    calls = []
    for index in range(len(tokens) - 3):
        if not (_hyper_m_active_word(tokens[index], qualifier)
                and tokens[index + 1].kind == "symbol"
                and tokens[index + 1].value == "."
                and _hyper_m_active_word(tokens[index + 2], method)
                and tokens[index + 3].kind == "symbol"
                and tokens[index + 3].value == "("):
            continue
        stack = []
        end = -1
        for cursor in range(index + 3, len(tokens)):
            token = tokens[cursor]
            if token.kind != "symbol":
                continue
            if token.value in "([{":
                stack.append(token.value)
            elif token.value in ")]}":
                if (not stack or stack[-1] != {
                    ")": "(", "]": "[", "}": "{",
                }[token.value]):
                    raise _HyperMParseError(
                        f"{qualifier}.{method} call has unbalanced delimiters")
                stack.pop()
                if not stack:
                    end = cursor
                    break
        if end < 0:
            raise _HyperMParseError(
                f"{qualifier}.{method} call has no closing parenthesis")
        args = []
        start = index + 4
        nested = []
        for cursor in range(start, end):
            token = tokens[cursor]
            if token.kind != "symbol":
                continue
            if token.value in "([{":
                nested.append(token.value)
            elif token.value in ")]}":
                if nested:
                    nested.pop()
            elif token.value == "," and not nested:
                args.append(tokens[start:cursor])
                start = cursor + 1
        args.append(tokens[start:end])
        calls.append(args)
    return calls


def _hyper_m_call_positions(tokens: list[_HyperMToken], qualifier: str,
                            method: str) -> list[int]:
    return [
        index for index in range(len(tokens) - 3)
        if (_hyper_m_active_word(tokens[index], qualifier)
            and tokens[index + 1].kind == "symbol"
            and tokens[index + 1].value == "."
            and _hyper_m_active_word(tokens[index + 2], method)
            and tokens[index + 3].kind == "symbol"
            and tokens[index + 3].value == "(")
    ]


def _hyper_m_reader_consumes_file(source: str, reader: str) -> int:
    """Count reachable format readers whose input is the exact File.Contents value."""
    tokens = _hyper_m_hide_constant_dead_branches(_tokenize_hyper_m(source))
    parsed = _hyper_m_root_let(tokens)
    if parsed is None:
        if any(_hyper_m_word(token, "let") for token in tokens) \
                and _hyper_m_file_calls(tokens):
            raise _HyperMParseError(
                "file reads inside a non-root or nested let are not "
                "statically resolved")
        segments = [tokens]
        file_backed: set[str] = set()
    else:
        _in_index, bindings, body = parsed
        names = set(bindings)
        dependencies = {
            name: _hyper_m_references(expression, names)
            for name, expression in bindings.items()
        }
        file_backed = {
            name for name, expression in bindings.items()
            if _hyper_m_file_calls(expression)
        }
        changed = True
        while changed:
            changed = False
            for name, dependencies_for_name in dependencies.items():
                if name not in file_backed and dependencies_for_name & file_backed:
                    file_backed.add(name)
                    changed = True
        # Reuse reachability parsing so unused root bindings cannot count.
        segments = _hyper_m_active_segments(source)

    count = 0
    for segment in segments:
        for args in _hyper_m_call_arguments(segment, reader, "Document"):
            if not args:
                continue
            if _hyper_m_file_calls(args[0]):
                count += 1
                continue
            if file_backed and _hyper_m_references(args[0], file_backed):
                count += 1
    return count


def _hyper_m_unwrap(tokens: list[_HyperMToken]) -> list[_HyperMToken]:
    """Remove only parentheses enclosing the entire expression."""
    while (len(tokens) >= 2 and tokens[0].kind == "symbol"
           and tokens[0].value == "("):
        depth = 0
        closing = -1
        for index, token in enumerate(tokens):
            if token.kind != "symbol":
                continue
            if token.value == "(":
                depth += 1
            elif token.value == ")":
                depth -= 1
                if depth == 0:
                    closing = index
                    break
        if closing != len(tokens) - 1:
            break
        tokens = tokens[1:-1]
    return tokens


def _hyper_m_container(tokens: list[_HyperMToken], opening: str, closing: str
                       ) -> Optional[list[list[_HyperMToken]]]:
    """Split a complete literal container, without mining nested strings."""
    if (len(tokens) < 2 or tokens[0].kind != "symbol"
            or tokens[0].value != opening or tokens[-1].kind != "symbol"
            or tokens[-1].value != closing):
        return None
    items = []
    start = 1
    stack = []
    for index, token in enumerate(tokens[1:-1], 1):
        if token.kind != "symbol":
            continue
        if token.value in "([{":
            stack.append(token.value)
        elif token.value in ")]}":
            if not stack or stack.pop() != {
                    ")": "(", "]": "[", "}": "{",
            }[token.value]:
                return None
        elif token.value == "," and not stack:
            items.append(tokens[start:index])
            start = index + 1
    if stack:
        return None
    if start < len(tokens) - 1:
        items.append(tokens[start:-1])
    elif items:
        return None
    return items


def _hyper_m_literal_list(tokens: list[_HyperMToken]
                          ) -> Optional[list[list[_HyperMToken]]]:
    return _hyper_m_container(_hyper_m_unwrap(tokens), "{", "}")


def _hyper_m_outer_call(tokens: list[_HyperMToken]
                        ) -> Optional[tuple[str, list[list[_HyperMToken]]]]:
    """Read one complete call, not a nested/unreachable function mention."""
    tokens = _hyper_m_unwrap(tokens)
    if tokens and _hyper_m_active_word(tokens[0], "#table"):
        args = _hyper_m_container(tokens[1:], "(", ")")
        return ("#table", args) if args is not None else None
    if (len(tokens) < 5 or tokens[0].kind != "word"
            or tokens[1].kind != "symbol" or tokens[1].value != "."
            or tokens[2].kind != "word"):
        return None
    args = _hyper_m_container(tokens[3:], "(", ")")
    return ((tokens[0].value + "." + tokens[2].value, args)
            if args is not None else None)


def _hyper_m_identifier(tokens: list[_HyperMToken], source: str
                        ) -> Optional[str]:
    """Read a literal quoted/generalized field name without M evaluation."""
    if len(tokens) == 1 and tokens[0].kind == "qident":
        name = tokens[0].value
    elif tokens and all(token.kind in {"word", "number"} for token in tokens):
        name = source[tokens[0].start:tokens[-1].end]
    else:
        return None
    if not name or "#(" in name or "/*" in name or "//" in name:
        return None
    return name


def _hyper_m_field_references(tokens: list[_HyperMToken], source: str
                              ) -> set[str]:
    """Read row selectors, including names such as ``[Close Date]``.

    Computed selectors, record literals and explicit non-row selectors stay
    unsupported rather than silently supplying a column dependency.
    """
    found: set[str] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind != "symbol" or token.value != "[":
            index += 1
            continue
        closing = index + 1
        while closing < len(tokens) and not (
                tokens[closing].kind == "symbol"
                and tokens[closing].value == "]"):
            closing += 1
        name = _hyper_m_identifier(tokens[index + 1:closing], source)
        if closing == len(tokens) or name is None:
            raise _HyperMParseError(
                "computed/record field expression has unsupported row dependencies")
        previous = tokens[index - 1] if index else None
        if previous is not None and (
                previous.kind in {"qident", "string", "number"}
                or (previous.kind == "word"
                    and previous.value.casefold() not in {
                        "each", "if", "then", "else", "and", "or", "not",
                        "try", "otherwise", "_",
                    })
                or (previous.kind == "symbol" and previous.value in ")]}")):
            raise _HyperMParseError(
                "explicit non-row field selector is not statically inferred")
        found.add(name)
        index = closing + 1
    return found


@dataclass
class _HyperMSchema:
    """Private static shape, with literal lookups distinct from file tables."""

    kind: str
    columns: dict[str, str] = field(default_factory=dict)
    nested: dict[str, "_HyperMSchema"] = field(default_factory=dict)
    literal_rows: Optional[list[dict[str, tuple[str, str]]]] = None


def _hyper_m_schema_derivations(
        source: str, raw_columns: set[str]
        ) -> tuple[set[str], set[str], set[str], list[str]]:
    """Propagate names along actual table inputs, never a union of step names.

    Let aliases resolve by dependency. A later, sibling, invalid or cyclic
    addition cannot supply a field to an earlier row expression. Only explicit
    literal lookups may add nested join/expansion columns. This does not execute
    M or certify column values, loaded rows, or source-to-target parity.
    """
    tokens = _hyper_m_unwrap(
        _hyper_m_hide_constant_dead_branches(_tokenize_hyper_m(source)))
    parsed = _hyper_m_root_let(tokens)
    bindings = parsed[1] if parsed else {}
    body = parsed[2] if parsed else tokens
    issues: list[str] = []
    cache: dict[str, Optional[_HyperMSchema]] = {}
    visiting: set[str] = set()

    def literal_text(value: list[_HyperMToken]) -> Optional[str]:
        value = _hyper_m_unwrap(value)
        if (len(value) == 1 and value[0].kind == "string"
                and "#(" not in value[0].value):
            return value[0].value
        return None

    def literal_names(value: list[_HyperMToken]) -> Optional[list[str]]:
        items = _hyper_m_literal_list(value)
        if items is None:
            name = literal_text(value)
            return [name] if name is not None else None
        names = []
        for item in items:
            name = literal_text(item)
            if name is None:
                return None
            names.append(name)
        return names

    def literal_scalar(value: list[_HyperMToken]
                       ) -> Optional[tuple[str, str]]:
        value = _hyper_m_unwrap(value)
        if len(value) == 1:
            token = value[0]
            if token.kind == "string" and "#(" not in token.value:
                return "text", token.value
            if token.kind == "word" and token.value in {"null", "true", "false"}:
                return ("null" if token.value == "null" else "logical",
                        token.value)
        text = "".join(token.value for token in value)
        if (value and all(token.kind == "number" or (
                token.kind == "symbol" and token.value in "+-")
                for token in value)
                and re.fullmatch(r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", text)):
            return "number", text
        return None

    def row_dependencies(value: list[_HyperMToken],
                         available: dict[str, str], label: str) -> bool:
        value = _hyper_m_unwrap(value)
        if not value or not _hyper_m_active_word(value[0], "each"):
            issues.append(
                f"{label}: only an explicit each row expression is statically inferred")
            return False
        expression = _hyper_m_unwrap(value[1:])
        if literal_scalar(expression) == ("null", "null"):
            issues.append(f"{label} fabricates its output with a literal null")
            return False
        if any(_hyper_m_active_word(token, keyword)
               for token in expression for keyword in ("let", "each")):
            issues.append(f"{label}: nested row-scope dependencies are unverified")
            return False
        external = _hyper_m_references(expression, set(bindings))
        if external:
            issues.append(
                f"{label}: non-row dependencies are not statically inferred: "
                + ", ".join(sorted(external)))
            return False
        if any(_hyper_m_call_positions(expression, "Record", method)
               for method in ("Field", "FieldOrDefault", "SelectFields")):
            issues.append(
                f"{label}: dynamic Record field dependencies are unverified")
            return False
        if _hyper_m_call_positions(expression, "Value", "NativeQuery"):
            issues.append(f"{label}: native-query scalar dependencies are unverified")
            return False
        try:
            fields = _hyper_m_field_references(expression, source)
        except _HyperMParseError as exc:
            issues.append(f"{label}: {exc}")
            return False
        missing = fields - set(available)
        if missing:
            issues.append(
                f"{label} references field(s) absent from its proven input "
                "schema: " + ", ".join(sorted(missing)))
            return False
        null_terms = [
            token for token in expression
            if token.kind != "symbol"
            and not any(_hyper_m_active_word(token, keyword)
                        for keyword in ("try", "otherwise"))
        ]
        if null_terms and all(_hyper_m_active_word(token, "null")
                              for token in null_terms):
            issues.append(f"{label} fabricates its output with literal nulls")
            return False
        # Do not infer arbitrary global parameters/functions as row fields.
        keywords = {
            "if", "then", "else", "and", "or", "not", "null", "true", "false",
            "try", "otherwise", "each", "type", "as", "is", "nullable",
            "text", "number", "logical", "date", "datetime", "time",
        }
        scalar_namespaces = {
            "Text", "Number", "Date", "DateTime", "DateTimeZone", "Time",
            "Duration", "Logical", "List", "Binary", "Value", "Character",
            "Int64", "Currency", "Percentage", "Day", "Week", "RoundingMode",
            "Precision", "RelativePosition", "Comparer", "Occurrence", "Order",
        }
        scalar_constructors = {
            "#date", "#datetime", "#datetimezone", "#time", "#duration",
        }
        square_depth = 0
        for index, token in enumerate(expression):
            if token.kind == "symbol":
                if token.value == "[":
                    square_depth += 1
                elif token.value == "]":
                    square_depth -= 1
                continue
            if square_depth or token.kind not in {"word", "qident"}:
                continue
            previous = expression[index - 1] if index else None
            following = expression[index + 1] if index + 1 < len(expression) else None
            dotted = any(item is not None and item.kind == "symbol"
                         and item.value == "." for item in (previous, following))
            if (following is not None and following.kind == "symbol"
                    and following.value == "."
                    and not (previous is not None and previous.kind == "symbol"
                             and previous.value == ".")
                    and token.value not in scalar_namespaces):
                issues.append(
                    f"{label}: qualified scalar dependency {token.value!r} "
                    "is not supported by static inference")
                return False
            row_selector = (token.value == "_" and following is not None
                            and following.kind == "symbol" and following.value == "[")
            # Only intrinsic scalar constructors in call position qualify.
            # Quoted identifiers, unknown hash symbols and #table/#binary
            # constructors remain unsupported; arguments still pass all field
            # and scalar-dependency checks above and below.
            scalar_constructor = (
                token.kind == "word" and token.value in scalar_constructors
                and not dotted and following is not None
                and following.kind == "symbol" and following.value == "("
            )
            if (not dotted and not row_selector and not scalar_constructor
                    and token.value not in keywords):
                issues.append(
                    f"{label}: scalar dependency {token.value!r} is not statically inferred")
                return False
        return True

    def csv_reader_options(args: list[list[_HyperMToken]]) -> bool:
        if len(args) == 1:
            return True
        options = _hyper_m_container(_hyper_m_unwrap(args[1]), "[", "]")
        if options is None:
            issues.append("Csv.Document options are not an explicit literal record")
            return False
        seen: set[str] = set()
        for option in options:
            if (len(option) < 3 or option[1].kind != "symbol"
                    or option[1].value != "="):
                issues.append("Csv.Document has an unsupported option declaration")
                return False
            name = _hyper_m_identifier(option[:1], source)
            value = _hyper_m_unwrap(option[2:])
            dotted = "".join(token.value for token in value)
            supported = (
                (name == "Delimiter" and literal_text(value) == ",")
                or (name == "Encoding" and literal_scalar(value) == ("number", "65001"))
                or (name == "QuoteStyle" and dotted == "QuoteStyle.Csv")
                or (name == "CsvStyle" and dotted in {
                    "CsvStyle.QuoteAfterDelimiter", "CsvStyle.QuoteAlways"})
                or (name == "ExtraValues" and dotted in {
                    "ExtraValues.Error", "ExtraValues.Ignore"})
                or (name == "Columns" and (
                    literal_scalar(value) == ("null", "null")
                    or literal_scalar(value) == ("number", str(len(raw_columns)))))
            )
            if name is None or name in seen or not supported:
                issues.append(
                    f"Csv.Document option {name!r} does not prove the physical "
                    "UTF-8 CSV schema used for header/count verification")
                return False
            seen.add(name)
        return True

    def literal_lookup(args: list[list[_HyperMToken]]) -> Optional[_HyperMSchema]:
        if len(args) != 2:
            issues.append("literal #table lookup requires an explicit schema and rows")
            return None
        schema = _hyper_m_unwrap(args[0])
        names = literal_names(schema)
        if names is None and len(schema) >= 4 and (
                _hyper_m_active_word(schema[0], "type")
                and _hyper_m_active_word(schema[1], "table")):
            declarations = _hyper_m_container(schema[2:], "[", "]")
            names = []
            for declaration in declarations if declarations is not None else [[]]:
                equals = next((index for index, item in enumerate(declaration)
                               if item.kind == "symbol" and item.value == "="), -1)
                name = _hyper_m_identifier(declaration[:equals], source) if equals > 0 else None
                datatype = [item.value for item in declaration[equals + 1:]]
                if datatype[:1] == ["nullable"]:
                    datatype = datatype[1:]
                if name is None or datatype not in (
                        ["text"], ["number"], ["logical"], ["any"]):
                    names = None
                    break
                names.append(name)
        rows = _hyper_m_literal_list(args[1])
        if (not names or len(set(names)) != len(names) or rows is None or not rows):
            issues.append(
                "lookup #table requires unique literal columns and nonempty literal rows")
            return None
        values: list[dict[str, tuple[str, str]]] = []
        for row in rows:
            cells = _hyper_m_literal_list(row)
            if cells is None or len(cells) != len(names):
                issues.append("lookup #table row width does not match its declared schema")
                return None
            measured = []
            for cell in cells:
                scalar = literal_scalar(cell)
                if scalar is None:
                    issues.append("lookup #table contains a nonliteral/unsupported cell")
                    return None
                measured.append(scalar)
            values.append(dict(zip(names, measured)))
        return _HyperMSchema(
            "lookup", {name: "literal" for name in names}, literal_rows=values)

    def infer(value: list[_HyperMToken], depth: int = 0) -> Optional[_HyperMSchema]:
        value = _hyper_m_unwrap(value)
        if depth > 128:
            issues.append("M schema dependency depth exceeds the static inference limit")
            return None
        if len(value) == 1 and value[0].kind in {"word", "qident"}:
            name = value[0].value
            if name in cache:
                return cache[name]
            if name not in bindings:
                issues.append(f"M dependency {name!r} is not statically resolved")
                return None
            if name in visiting:
                issues.append(f"M schema dependency cycle includes {name!r}")
                return None
            visiting.add(name)
            state = infer(bindings[name], depth + 1)
            visiting.remove(name)
            cache[name] = state
            return state
        if value and _hyper_m_active_word(value[0], "try"):
            issues.append(
                "table/source-level try fallback prevents proof that the "
                "produced Hyper file supplies the final table")
            return None
        call = _hyper_m_outer_call(value)
        if call is None:
            issues.append("M table expression has unsupported static schema dependencies")
            return None
        method, args = call
        if method == "#table":
            return literal_lookup(args)
        arities = {
            "File.Contents": {1, 2}, "Binary.Buffer": {1},
            "Csv.Document": {1, 2}, "Parquet.Document": {1},
            "Table.PromoteHeaders": {1, 2}, "Table.RenameColumns": {2, 3},
            "Table.AddColumn": {3, 4}, "Table.SelectColumns": {2, 3},
            "Table.RemoveColumns": {2, 3}, "Table.ReorderColumns": {2, 3},
            "Table.TransformColumnTypes": {2, 3}, "Table.NestedJoin": {5, 6},
            "Table.ExpandTableColumn": {3, 4}, "Table.ReplaceValue": {5},
            "Table.SelectRows": {2}, "Table.Buffer": {1, 2},
            "Table.Sort": {2}, "Table.Distinct": {1, 2},
        }
        if method in arities and len(args) not in arities[method]:
            issues.append(f"{method} has an unsupported static argument count")
            return None
        if method == "File.Contents":
            return _HyperMSchema("binary")
        if not args or not args[0]:
            issues.append(f"{method} has no statically inferred input")
            return None
        state = infer(args[0], depth + 1)
        if state is None:
            return None
        if method in {"Csv.Document", "Parquet.Document", "Binary.Buffer"}:
            if state.kind != "binary":
                issues.append(f"{method} does not consume the proven file binary")
                return None
            if method == "Binary.Buffer":
                return state
            if method == "Csv.Document":
                if not csv_reader_options(args):
                    return None
                return _HyperMSchema("csv")  # Physical names require promotion.
            return _HyperMSchema("table", {name: "raw" for name in raw_columns})
        if method == "Table.PromoteHeaders":
            if state.kind != "csv":
                issues.append(
                    "Table.PromoteHeaders has no unmodified physical CSV header proof")
                return None
            return _HyperMSchema("table", {name: "raw" for name in raw_columns})
        if state.kind != "table":
            issues.append(
                f"{method} input is not a proven file-backed named table; "
                "CSV requires physical header promotion")
            return None
        output = dict(state.columns)
        nested = dict(state.nested)
        if method == "Table.RenameColumns":
            pairs = _hyper_m_literal_list(args[1]) if len(args) >= 2 else None
            if pairs and all(literal_text(pair) is not None for pair in pairs):
                pairs = [args[1]]  # M also permits one {"old", "new"} pair.
            if pairs is None:
                issues.append("Table.RenameColumns requires explicit literal pairs")
                return None
            for pair in pairs:
                names = literal_names(pair)
                if names is None or len(names) != 2:
                    issues.append("Table.RenameColumns contains an unsupported rename pair")
                    return None
                old, new = names
                if old not in output:
                    issues.append(
                        f"Table.RenameColumns source {old!r} is absent from "
                        "its proven input schema")
                elif old != new:
                    if new in output:
                        issues.append(f"Table.RenameColumns target {new!r} already exists")
                    else:
                        del output[old]
                        output[new] = "renamed"
                        if old in nested:
                            nested[new] = nested.pop(old)
        elif method == "Table.AddColumn":
            name = literal_text(args[1]) if len(args) >= 2 else None
            if name is None or len(args) < 3:
                issues.append("Table.AddColumn requires an explicit literal name")
                return None
            if name in output:
                issues.append(f"Table.AddColumn target {name!r} already exists in its input")
            elif row_dependencies(args[2], output, f"Table.AddColumn {name!r}"):
                output[name] = "added"
        elif method in {
                "Table.SelectColumns", "Table.RemoveColumns",
                "Table.ReorderColumns", "Table.TransformColumnTypes"}:
            items = args[1] if len(args) >= 2 else []
            names = literal_names(items)
            if method == "Table.TransformColumnTypes":
                pairs = _hyper_m_literal_list(items)
                if pairs is None:
                    issues.append(f"{method} has unsupported dynamic column names")
                    return None
                type_names: list[str] = []
                for pair in pairs:
                    entry = _hyper_m_literal_list(pair)
                    name = literal_text(entry[0]) if entry else None
                    if entry is None or len(entry) != 2 or name is None:
                        issues.append(f"{method} has unsupported dynamic column names")
                        return None
                    type_names.append(name)
                names = type_names
            if names is None:
                issues.append(f"{method} has unsupported dynamic column names")
                return None
            missing = set(names) - set(output)
            if missing:
                issues.append(
                    f"{method} references field(s) absent from its proven input "
                    "schema: " + ", ".join(sorted(missing)))
            if method == "Table.SelectColumns":
                output = {name: output[name] for name in names if name in output}
            elif method == "Table.RemoveColumns":
                output = {name: origin for name, origin in output.items()
                          if name not in names}
            nested = {name: shape for name, shape in nested.items() if name in output}
        elif method == "Table.NestedJoin":
            right = infer(args[2], depth + 1) if len(args) >= 5 else None
            left_keys = literal_names(args[1]) if len(args) >= 2 else None
            right_keys = literal_names(args[3]) if len(args) >= 4 else None
            name = literal_text(args[4]) if len(args) >= 5 else None
            join_kind = "".join(item.value for item in args[5]) if len(args) == 6 else ""
            if (right is None or right.kind != "lookup"
                    or right.literal_rows is None or not left_keys or not right_keys
                    or len(left_keys) != len(right_keys) or name is None
                    or len(args) not in {5, 6}
                    or join_kind not in {"", "JoinKind.LeftOuter"}):
                issues.append(
                    "Table.NestedJoin requires explicit left-outer keys and "
                    "a proven literal lookup")
                return None
            if (set(left_keys) - set(output)
                    or set(right_keys) - set(right.columns) or name in output):
                issues.append(
                    "Table.NestedJoin has missing input/lookup keys or an "
                    "existing nested-column target")
                return None
            keys = [tuple(row[key] for key in right_keys) for row in right.literal_rows]
            if (len(keys) != len(set(keys))
                    or any(cell[0] == "null" for key in keys for cell in key)):
                issues.append(
                    "literal lookup join keys must be unique and non-null")
                return None
            output[name] = "nested"
            nested[name] = right
        elif method == "Table.ExpandTableColumn":
            name = literal_text(args[1]) if len(args) >= 2 else None
            fields = literal_names(args[2]) if len(args) >= 3 else None
            targets = literal_names(args[3]) if len(args) == 4 else fields
            lookup = nested.get(name) if name is not None else None
            if (name is None or lookup is None
                    or lookup.literal_rows is None or not fields
                    or targets is None or len(fields) != len(targets)
                    or len(args) not in {3, 4} or len(set(targets)) != len(targets)
                    or set(fields) - set(lookup.columns)
                    or set(targets) & (set(output) - {name})):
                issues.append(
                    "Table.ExpandTableColumn requires explicit proven lookup "
                    "fields and unique output names")
                return None
            for column in fields:
                if all(row[column][0] == "null" for row in lookup.literal_rows):
                    issues.append(
                        f"lookup expansion {column!r} fabricates a literal-null column")
                    return None
            del output[name]
            del nested[name]
            output.update({target: "lookup" for target in targets})
        elif method == "Table.ReplaceValue":
            names = literal_names(args[4]) if len(args) == 5 else None
            replacer = "".join(item.value for item in args[3]) if len(args) == 5 else ""
            if (names is None or set(names) - set(output)
                    or literal_scalar(args[1]) is None
                    or literal_scalar(args[2]) is None
                    or replacer not in {"Replacer.ReplaceValue", "Replacer.ReplaceText"}):
                issues.append(
                    "Table.ReplaceValue has unsupported replacement/column dependencies")
                return None
        elif method == "Table.SelectRows":
            if len(args) < 2 or not row_dependencies(
                    args[1], output, "Table.SelectRows"):
                return None
        elif method not in {"Table.Buffer", "Table.Sort", "Table.Distinct"}:
            issues.append(f"{method} output schema is not supported by static inference")
            return None
        return _HyperMSchema("table", output, nested)

    state = infer(body)
    output = state.columns if state is not None and state.kind == "table" else {}
    if state is not None and state.kind != "table":
        issues.append(
            "final M output has no proven file-backed named table schema "
            "(CSV header promotion is required)")
    return (
        {name for name in output if name not in state.nested} if state else set(),
        {name for name, origin in output.items() if origin in {"added", "lookup"}},
        {name for name, origin in output.items() if origin == "renamed"},
        sorted(set(issues)),
    )


def _hyper_schema_evidence(table_path: str, expression: str,
                           raw_columns: list[str]
                           ) -> tuple[Dict[str, Any], list[str]]:
    """Check declared sourceColumn mappings against raw and statically inferred M."""
    from powerbi_import.artifact_diff import _parse_tmdl_table
    from powerbi_import.openability import _literal_final_projection

    relative = os.path.basename(table_path)
    diagnostics: list[str] = []
    parsed = _parse_tmdl_table(table_path)
    if not isinstance(parsed, dict):
        return ({"status": "unverified", "checked_source_columns": 0,
                 "derived_source_columns": [], "missing_source_columns": []},
                [f"{relative}: TMDL columns could not be statically parsed"])
    columns = parsed.get("columns")
    if not isinstance(columns, list) or not columns:
        return ({"status": "unverified", "checked_source_columns": 0,
                 "derived_source_columns": [], "missing_source_columns": []},
                [f"{relative}: final model columns are unavailable for source-schema checks"])

    raw = set(raw_columns)
    segments = _hyper_m_active_segments(expression)
    available, added, renamed, derivation_issues = _hyper_m_schema_derivations(
        expression, raw)
    diagnostics.extend(f"{relative}: {issue}" for issue in derivation_issues)
    projection = _literal_final_projection(expression)
    active_missing_ignore = any(
        _hyper_m_word(segment[index], "MissingField")
        and index + 2 < len(segment)
        and segment[index + 1].kind == "symbol"
        and segment[index + 1].value == "."
        and _hyper_m_word(segment[index + 2], "Ignore")
        for segment in segments
        for index in range(len(segment))
        if segment[index].kind == "word"
    )
    if active_missing_ignore:
        diagnostics.append(
            f"{relative}: active M uses MissingField.Ignore; required "
            "sourceColumn output coverage cannot be proven statically")
    mapping_names = []
    missing = []
    derived = []
    for column in columns:
        if not isinstance(column, dict) or "sourceColumn" not in column:
            continue
        source_column = column.get("sourceColumn")
        if not isinstance(source_column, str) or not source_column:
            missing.append(str(column.get("name") or "<unnamed>"))
            diagnostics.append(
                f"{relative}: sourceColumn is empty or invalid on a final model column")
            continue
        mapping_names.append(source_column)
        if source_column not in available:
            missing.append(source_column)
            if active_missing_ignore:
                reason = (
                    "MissingField.Ignore cannot establish a required source mapping")
            else:
                reason = (
                    "sourceColumn is absent from the proven final M schema; "
                    "the export and supported transformations do not establish it")
            diagnostics.append(
                f"{relative}: {reason}: {source_column!r}")
        elif source_column in added or source_column in renamed:
            derived.append(source_column)
        if projection is not None and source_column not in projection:
            diagnostics.append(
                f"{relative}: sourceColumn {source_column!r} is absent from the "
                'terminal #"Model Columns" projection')
            if source_column not in missing:
                missing.append(source_column)

    if not mapping_names:
        diagnostics.append(
            f"{relative}: no final sourceColumn mapping is available; "
            "raw-to-model schema coverage is unverified")
    return ({
        "status": "static_verified" if not diagnostics else "unverified",
        "checked_source_columns": len(mapping_names),
        "derived_source_columns": sorted(set(derived)),
        "missing_source_columns": sorted(set(missing)),
        "inferred_output_columns": sorted(available),
        "terminal_projection": (
            sorted(projection) if projection is not None else None),
        "runtime_row_schema": "not_verified",
    }, diagnostics)


def _hyper_value_signals(value: Any, key_hint: str = "") -> bool:
    """Recognize explicit Hyper connector/file evidence without caption guesses."""
    hint = key_hint.casefold()
    if isinstance(value, dict):
        if value.get("hyper_identity") is not None:
            return True
        if hint == "hyper_files" and bool(value):
            return True
        return any(_hyper_value_signals(item, str(key))
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        if hint == "hyper_files" and bool(value):
            return True
        return any(_hyper_value_signals(item, key_hint) for item in value)
    if not isinstance(value, str):
        return False
    if hint == "hyper_files" and value.strip():
        return True
    candidate = value.strip()
    if re.search(r"(?i)(?:^|[\\/])[^\\/]+\.hyper(?:$|[?#])", candidate):
        return True
    if hint in {
            "type", "class", "connector", "connection_type", "dbtype",
            "connection"}:
        return candidate.casefold() in {"hyper", "dataengine"}
    return False


def _hyper_source_identity_inventory(
        extracted: Dict[str, Any]) -> tuple[dict[tuple[str, ...], set[str]],
                                            list[str], bool]:
    """Collect explicit source identities and Hyper connector omissions."""
    identities: dict[tuple[str, ...], set[str]] = {}
    diagnostics: list[str] = []
    uses_hyper = _hyper_value_signals(extracted)
    seen: set[int] = set()
    stack = [extracted]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        if isinstance(current, dict):
            seen.add(id(current))
            if "hyper_identity" in current:
                identity = current.get("hyper_identity")
                key = _hyper_binding_identity_key(identity)
                if key is None:
                    diagnostics.append(
                        "Extracted Hyper source table has an invalid or incomplete "
                        "explicit physical identity.")
                else:
                    alias = current.get("name")
                    alias_names = identities.setdefault(key, set())
                    if isinstance(alias, str) and alias:
                        alias_names.add(alias)
                    else:
                        alias_names.add("")
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, (list, tuple)):
            seen.add(id(current))
            stack.extend(reversed(list(current)))

    datasources = extracted.get("datasources", [])
    if isinstance(datasources, dict):
        datasources = [datasources]
    if isinstance(datasources, list):
        for datasource in datasources:
            if not isinstance(datasource, dict):
                continue
            connection_values = [
                datasource.get("connection"),
                datasource.get("connection_details"),
                datasource.get("connection_map"),
            ]
            is_hyper_ds = any(
                _hyper_value_signals(value) for value in connection_values)
            if not is_hyper_ds:
                continue
            tables = datasource.get("tables", [])
            if not isinstance(tables, list):
                diagnostics.append(
                    "Hyper datasource has no readable table inventory.")
                continue
            for table in tables:
                if not isinstance(table, dict):
                    diagnostics.append(
                        "Hyper datasource contains a malformed table record.")
                elif table.get("hyper_identity") is None:
                    diagnostics.append(
                        "Hyper datasource table lacks an exact packaged or "
                        "explicit local source identity.")
    return identities, diagnostics, uses_hyper


def _hyper_project_tmdl_tables(project_root: str
                               ) -> tuple[list[dict[str, Any]], list[str], bool]:
    """Read final model tables and detect unresolved Hyper source markers."""
    tables: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    uses_hyper = False
    try:
        with os.scandir(project_root) as entries:
            model_entries = sorted(
                (entry for entry in entries
                 if entry.name.endswith(".SemanticModel")),
                key=lambda entry: entry.name.casefold(),
            )
    except OSError as exc:
        return [], [
            f"Cannot inspect final semantic models ({type(exc).__name__})."
        ], False
    for model_entry in model_entries:
        model_path = model_entry.path
        try:
            model_stat = os.lstat(model_path)
        except OSError as exc:
            diagnostics.append(
                f"{model_entry.name}: cannot inspect semantic model "
                f"({type(exc).__name__})")
            continue
        if _hyper_is_link(model_path, model_stat):
            diagnostics.append(
                f"{model_entry.name}: semantic model directory is a link")
            continue
        try:
            if not model_entry.is_dir(follow_symlinks=False):
                continue
        except OSError as exc:
            diagnostics.append(
                f"{model_entry.name}: cannot inspect semantic model "
                f"({type(exc).__name__})")
            continue
        definition = os.path.join(model_path, "definition")
        try:
            definition_stat = os.lstat(definition)
        except FileNotFoundError:
            continue
        except OSError as exc:
            diagnostics.append(
                f"{model_entry.name}: cannot inspect model definition "
                f"({type(exc).__name__})")
            continue
        if (_hyper_is_link(definition, definition_stat)
                or not stat.S_ISDIR(definition_stat.st_mode)):
            diagnostics.append(
                f"{model_entry.name}: model definition is not a regular directory")
            continue
        tables_dir = os.path.join(definition, "tables")
        try:
            tables_stat = os.lstat(tables_dir)
        except FileNotFoundError:
            continue
        except OSError as exc:
            diagnostics.append(
                f"{model_entry.name}: cannot inspect final table directory "
                f"({type(exc).__name__})")
            continue
        if (_hyper_is_link(tables_dir, tables_stat)
                or not stat.S_ISDIR(tables_stat.st_mode)):
            diagnostics.append(
                f"{model_entry.name}: final table directory is not a regular directory")
            continue
        try:
            with os.scandir(tables_dir) as entries:
                table_entries = sorted(
                    (entry for entry in entries
                     if entry.name.lower().endswith(".tmdl")),
                    key=lambda entry: entry.name.casefold(),
                )
        except OSError as exc:
            diagnostics.append(
                f"{model_entry.name}: cannot list final tables "
                f"({type(exc).__name__})")
            continue
        for table_entry in table_entries:
            relative = os.path.relpath(table_entry.path, project_root)
            try:
                table_info = os.lstat(table_entry.path)
            except OSError as exc:
                diagnostics.append(
                    f"{relative}: cannot inspect final table file "
                    f"({type(exc).__name__})")
                continue
            if (_hyper_is_link(table_entry.path, table_info)
                    or not stat.S_ISREG(table_info.st_mode)):
                diagnostics.append(f"{relative}: final TMDL file is not regular")
                continue
            try:
                with open(table_entry.path, "r", encoding="utf-8-sig") as stream:
                    content = stream.read()
            except (OSError, UnicodeError) as exc:
                diagnostics.append(
                    f"{relative}: final TMDL cannot be read "
                    f"({type(exc).__name__})")
                continue
            annotations = list(_HYPER_ANNOTATION_RE.finditer(content))
            if annotations:
                uses_hyper = True
            m_partitions = extract_m_partitions(content)
            unresolved_marker = False
            for _partition_name, expression in m_partitions:
                try:
                    active_segments = _hyper_m_active_segments(expression)
                except _HyperMParseError:
                    try:
                        active_segments = [_tokenize_hyper_m(expression)]
                    except _HyperMParseError:
                        active_segments = []
                for segment in active_segments:
                    if any(
                            token.kind == "string"
                            and token.value == "Migration.HyperExportRequired"
                            for token in segment):
                        unresolved_marker = True
                    for index in range(len(segment) - 2):
                        if (_hyper_m_active_word(segment[index], "Migration")
                                and segment[index + 1].kind == "symbol"
                                and segment[index + 1].value == "."
                                and _hyper_m_active_word(
                                    segment[index + 2], "HyperExportRequired")):
                            unresolved_marker = True
                            break
                    if unresolved_marker:
                        break
                if unresolved_marker:
                    break
            if unresolved_marker:
                uses_hyper = True
                diagnostics.append(
                    f"{relative}: unresolved Migration.HyperExportRequired "
                    "marker remains in an active final M partition")
            if not annotations:
                for _partition_name, expression in m_partitions:
                    try:
                        calls = _hyper_m_active_file_calls(expression)
                    except _HyperMParseError:
                        continue
                    for _start, _end, argument in calls:
                        if any(
                                token.kind == "string"
                                and (
                                    token.value.casefold().endswith(".hyper")
                                    or _HYPER_EXPORT_FILE_RE.fullmatch(
                                        ntpath.basename(token.value.rstrip("\\/")))
                                )
                                for token in argument):
                            uses_hyper = True
                            break
                    if uses_hyper:
                        break
            if len(annotations) > 1:
                diagnostics.append(
                    f"{relative}: table has multiple Migration_HyperIdentity "
                    "annotations")
                continue
            if not annotations:
                continue
            declaration = _HYPER_TMDL_TABLE_RE.search(content)
            table_name = None
            if declaration:
                table_name = declaration.group(1).strip()
                if table_name.startswith("'") and table_name.endswith("'"):
                    table_name = table_name[1:-1].replace("''", "'")
            if not table_name:
                diagnostics.append(
                    f"{relative}: annotated TMDL table declaration is missing")
                continue
            identity, identity_issue = _hyper_annotation_identity(
                annotations[0].group(1), relative)
            if identity_issue:
                diagnostics.append(identity_issue)
            tables.append({
                "path": table_entry.path,
                "relative_path": relative,
                "definition": definition,
                "content": content,
                "name": table_name,
                "identity": identity,
                "identity_key": _hyper_binding_identity_key(identity),
            })
    return tables, diagnostics, uses_hyper


def _hyper_manifest_preflight(manifest_path: str
                              ) -> tuple[str, Optional[Dict[str, Any]], bool,
                                         Optional[str]]:
    """Inspect manifest existence/shape before deciding Hyper applicability."""
    data_dir = os.path.dirname(manifest_path)
    try:
        data_info = os.lstat(data_dir)
    except FileNotFoundError:
        return "missing", None, False, None
    except OSError as exc:
        return "unreadable", None, True, (
            f"Hyper export manifest cannot be inspected "
            f"({type(exc).__name__}).")
    if _hyper_is_link(data_dir, data_info) or not stat.S_ISDIR(data_info.st_mode):
        # Opening anything through a Data link would follow it to wherever the
        # project metadata points, so the manifest is not read. The Data
        # diagnostics already report this when the project really uses Hyper.
        return "unreadable", None, False, None
    try:
        info = os.lstat(manifest_path)
    except FileNotFoundError:
        return "missing", None, False, None
    except OSError as exc:
        return "unreadable", None, True, (
            f"Hyper export manifest cannot be inspected "
            f"({type(exc).__name__}).")
    if _hyper_is_link(manifest_path, info) or not stat.S_ISREG(info.st_mode):
        return "invalid", None, True, (
            "Hyper export manifest must be a regular file, not a link.")
    try:
        with open(manifest_path, "r", encoding="utf-8") as stream:
            manifest = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return "invalid", None, True, (
            f"Hyper export manifest cannot be read "
            f"({type(exc).__name__}).")
    if not isinstance(manifest, dict):
        return "invalid", None, True, (
            "Hyper export manifest must contain a JSON object.")
    exports = manifest.get("exports")
    if not isinstance(exports, list):
        return "invalid", None, True, (
            "Hyper export manifest requires an exports list.")
    if type(manifest.get("version")) is not int or manifest.get("version") != 1:
        return "invalid", None, True, (
            "Hyper export manifest requires version 1.")
    return "available", manifest, bool(exports), None


def _hyper_safe_artifact_file(data_dir: str, filename: str
                              ) -> tuple[Optional[str], Optional[os.stat_result],
                                         Optional[str]]:
    """Resolve a manifest basename only when it is an ordinary Data file."""
    if not _hyper_safe_basename(filename):
        return None, None, "filename is not a safe basename"
    path = os.path.join(data_dir, filename)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, None, "file is missing from the project Data directory"
    except OSError as exc:
        return None, None, f"file cannot be inspected ({type(exc).__name__})"
    if _hyper_is_link(path, info) or not stat.S_ISREG(info.st_mode):
        return None, info, "file is not a regular non-link artifact"
    data_real = _hyper_realpath(data_dir)
    file_real = _hyper_realpath(path)
    try:
        contained = os.path.commonpath((data_real, file_real)) == data_real
    except ValueError:
        contained = False
    if not contained:
        return None, info, "file resolves outside the project Data directory"
    return path, info, None


def _hyper_validate_export_record(raw_record: Dict[str, Any], index: int,
                                  data_dir: str
                                  ) -> tuple[dict[str, Any],
                                             Optional[tuple[str, ...]]]:
    """Verify one manifest record against the final artifact bytes."""
    key, proof_issues = _hyper_record_proof(raw_record, index)
    record_diagnostics = [
        f"Hyper export record {index}: {issue}" for issue in proof_issues
    ]
    filename = raw_record.get("filename")
    fmt = raw_record.get("format")
    identity = raw_record.get("identity")
    item: dict[str, Any] = {
        "manifest_index": index,
        "filename": filename,
        "format": fmt,
        "identity": _hyper_identity_payload(identity),
        "row_count": raw_record.get("row_count"),
        "total_rows": raw_record.get("total_rows"),
        "max_rows": raw_record.get("max_rows"),
        "sampled": raw_record.get("sampled"),
        "truncated": raw_record.get("truncated"),
        "columns": list(raw_record.get("columns", []))
        if isinstance(raw_record.get("columns"), list) else None,
        "sha256": raw_record.get("sha256"),
        "size_bytes": raw_record.get("size_bytes"),
        "row_count_source": raw_record.get("row_count_source"),
        "bindings": [],
        "diagnostics": record_diagnostics,
        "status": "failed",
    }
    artifact_path = None
    initial_info = None
    if isinstance(filename, str) and _hyper_safe_basename(filename):
        artifact_path, initial_info, file_issue = _hyper_safe_artifact_file(
            data_dir, filename)
        if file_issue:
            record_diagnostics.append(
                f"Hyper export record {index}: {file_issue}.")
    else:
        file_issue = None
    if artifact_path and initial_info is not None:
        try:
            digest, size_bytes = _hyper_file_digest(artifact_path)
            after_hash = os.lstat(artifact_path)
            signature_before = (
                initial_info.st_size, initial_info.st_mtime_ns,
                initial_info.st_ino, initial_info.st_dev,
            )
            signature_after = (
                after_hash.st_size, after_hash.st_mtime_ns,
                after_hash.st_ino, after_hash.st_dev,
            )
            if signature_before != signature_after:
                record_diagnostics.append(
                    f"Hyper export record {index}: file changed during "
                    "integrity verification.")
            if digest != raw_record.get("sha256"):
                record_diagnostics.append(
                    f"Hyper export record {index}: SHA-256 does not match "
                    "the final artifact.")
            if size_bytes != raw_record.get("size_bytes"):
                record_diagnostics.append(
                    f"Hyper export record {index}: byte size does not match "
                    "the final artifact.")
            if fmt == "csv" and isinstance(raw_record.get("columns"), list) \
                    and type(raw_record.get("row_count")) is int:
                csv_ok, csv_rows, csv_issue = _hyper_csv_evidence(
                    artifact_path, raw_record["columns"],
                    raw_record["row_count"])
                item["csv_rows_measured"] = csv_rows
                if not csv_ok:
                    record_diagnostics.append(
                        f"Hyper export record {index}: {csv_issue}.")
                try:
                    after_csv = os.lstat(artifact_path)
                except OSError as exc:
                    record_diagnostics.append(
                        f"Hyper export record {index}: file cannot be restated "
                        f"after CSV counting ({type(exc).__name__}).")
                else:
                    csv_signature = (
                        after_csv.st_size, after_csv.st_mtime_ns,
                        after_csv.st_ino, after_csv.st_dev,
                    )
                    if csv_signature != signature_before:
                        record_diagnostics.append(
                            f"Hyper export record {index}: file changed during "
                            "CSV verification.")
            elif fmt == "parquet":
                item["parquet_row_count_evidence"] = "measured_hyper_copy"
                item["parquet_recount"] = "not_run"
        except OSError as exc:
            record_diagnostics.append(
                f"Hyper export record {index}: file integrity read failed "
                f"({type(exc).__name__}).")
    item["diagnostics"] = record_diagnostics
    return item, key


def _hyper_project_data_signals(data_dir: str
                                ) -> tuple[bool, list[str]]:
    """Recognize retained Hyper sources/exports without trusting file contents."""
    try:
        root_info = os.lstat(data_dir)
    except FileNotFoundError:
        return False, []
    except OSError as exc:
        return False, [
            f"Project Data artifacts cannot be inspected "
            f"({type(exc).__name__})."
        ]
    if (_hyper_is_link(data_dir, root_info)
            or not stat.S_ISDIR(root_info.st_mode)):
        return False, ["Project Data path is not a regular directory."]
    found = False
    issues: list[str] = []
    pending = [data_dir]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                ordered = sorted(entries, key=lambda item: item.name.casefold())
        except OSError as exc:
            relative = os.path.relpath(directory, data_dir)
            label = "Data" if relative == "." else f"Data/{relative}"
            issues.append(
                f"{label}: cannot inspect Hyper artifacts "
                f"({type(exc).__name__}).")
            continue
        for entry in ordered:
            lower_name = entry.name.casefold()
            if (lower_name.endswith(".hyper")
                    or _HYPER_EXPORT_FILE_RE.fullmatch(entry.name)):
                found = True
            try:
                info = os.lstat(entry.path)
            except OSError as exc:
                relative = os.path.relpath(entry.path, data_dir)
                issues.append(
                    f"Data/{relative}: cannot inspect artifact "
                    f"({type(exc).__name__}).")
                continue
            if _hyper_is_link(entry.path, info):
                continue
            try:
                if stat.S_ISDIR(info.st_mode):
                    pending.append(entry.path)
            except OSError as exc:
                relative = os.path.relpath(entry.path, data_dir)
                issues.append(
                    f"Data/{relative}: cannot inspect artifact "
                    f"({type(exc).__name__}).")
    return found, issues


def _hyper_verify_table_binding(
        table: Dict[str, Any], export: Dict[str, Any],
        project_root: str, data_dir: str,
        data_folder_cache: Dict[str, tuple[Optional[str], Optional[str]]]
        ) -> tuple[dict[str, Any], list[str]]:
    """Verify one annotation, final Import M partition, exact file and schema."""
    relative = table["relative_path"]
    issues: list[str] = []
    blocks = _hyper_tmdl_partition_blocks(table["content"])
    m_partitions = extract_m_partitions(table["content"])
    if len(blocks) != 1:
        issues.append(
            "final table must have exactly one partition for a full Hyper Import binding")
    if len(m_partitions) != 1:
        issues.append(
            "final table must have exactly one reachable M partition")
    partition = blocks[0] if len(blocks) == 1 else {}
    partition_name = partition.get("name") if partition else None
    expression = next((
        query for name, query in m_partitions if name == partition_name
    ), None)
    if partition:
        if partition.get("kind") != "m":
            issues.append(
                "final Hyper partition must be M, not a calculated/direct-query partition")
        if partition.get("modes") != ["import"]:
            issues.append("final Hyper partition mode must be exactly import")
    if expression is None:
        issues.append(
            "final Import M partition source expression is missing or ambiguous")

    path_via = "unresolved"
    schema_evidence: Dict[str, Any] = {
        "status": "unverified",
        "checked_source_columns": 0,
        "derived_source_columns": [],
        "missing_source_columns": [],
        "runtime_row_schema": "not_verified",
    }
    if expression is not None:
        try:
            conditional_issues = _hyper_m_conditional_source_issues(expression)
            issues.extend(conditional_issues)
            active_segments = _hyper_m_active_segments(expression)
            calls = []
            for segment in active_segments:
                calls.extend(_hyper_m_file_calls(segment))
            if len(calls) != 1:
                issues.append(
                    "final M partition must have exactly one reachable "
                    "File.Contents read; unused bindings and #table "
                    "substitutes are not source proof")
                call_argument = None
            else:
                call_argument = calls[0][2]

            expected_reader = (
                "Csv" if export.get("format") == "csv" else
                "Parquet" if export.get("format") == "parquet" else None
            )
            reader_count = (
                _hyper_m_reader_consumes_file(expression, expected_reader)
                if expected_reader else 0
            )
            if not reader_count:
                issues.append(
                    "final M partition does not pass the produced file to the "
                    "document reader matching its CSV/Parquet format")

            if call_argument is not None and isinstance(
                    export.get("filename"), str):
                needs_data_folder = any(
                    _hyper_m_word(token, "DataFolder")
                    for token in call_argument)
                data_folder = None
                if needs_data_folder:
                    definition = table["definition"]
                    if definition not in data_folder_cache:
                        data_folder_cache[definition] = _hyper_data_folder(
                            definition, project_root, data_dir)
                    data_folder, folder_issue = data_folder_cache[definition]
                    if folder_issue:
                        issues.append(folder_issue)
                resolved_path, path_result = _hyper_m_path(
                    call_argument, data_folder, project_root)
                if resolved_path is None:
                    issues.append(path_result)
                else:
                    path_via = path_result
                    if resolved_path not in _hyper_trusted_file_keys(
                            data_dir, export["filename"]):
                        issues.append(
                            "final M File.Contents path does not resolve to the "
                            "exact canonical manifest export file; caption-staged "
                            "alias files are not verified by this contract and "
                            "staged_files is informational only")
        except _HyperMParseError as exc:
            issues.append(
                "final M source reachability is unsupported "
                f"({str(exc)}); exact file binding is unverified")
    if expression is not None and isinstance(export.get("columns"), list):
        try:
            schema_evidence, schema_issues = _hyper_schema_evidence(
                table["path"], expression, export["columns"])
            issues.extend(schema_issues)
        except _HyperMParseError as exc:
            issues.append(
                "final sourceColumn schema inference is unsupported "
                f"({str(exc)})")
    elif expression is not None:
        issues.append(
            "declared export columns are unavailable for final sourceColumn checks")

    binding = {
        "table": table["name"],
        "table_file": relative,
        "partition": partition_name,
        "mode": ((partition.get("modes") or [None])[0]
                 if partition else None),
        "path_source": path_via,
        "schema": schema_evidence,
        "status": "static_verified" if not issues
        and not export.get("diagnostics") else "failed",
        "diagnostics": sorted(set(issues)),
    }
    return binding, binding["diagnostics"]


def build_hyper_import_evidence(extracted: Dict[str, Any],
                                project_dir: str) -> Dict[str, Any]:
    """Build fail-closed static evidence for complete Hyper Import artifacts.

    This report verifies export metadata, final artifact bytes, CSV physical
    rows/header where applicable, exact identities and reachable M partition
    bindings. It never executes M, reads Parquet through an SDK, refreshes a
    model, or certifies runtime-loaded rows or source-to-target data parity.
    """
    extracted = extracted if isinstance(extracted, dict) else {}
    diagnostics: list[str] = []
    result: Dict[str, Any] = {
        "status": "not_applicable",
        "runtime_status": "not_run",
        "manifest": _HYPER_MANIFEST,
        "manifest_status": "not_applicable",
        "diagnostics": diagnostics,
        "exports": [],
        "bindings": [],
        "verification_scope": (
            "Static manifest, file integrity, CSV header/count, sourceColumn "
            "and final Import binding evidence only. Runtime load/refresh, "
            "Parquet row recount, deployment, and full data parity are not verified. "
            "Bindings must consume the canonical hashed manifest export; "
            "caption-staged aliases are not authorized by staged_files."
        ),
    }
    try:
        project_root = os.path.abspath(os.fspath(project_dir))
    except (TypeError, ValueError, OSError) as exc:
        result["status"] = "failed"
        result["manifest_status"] = "unavailable"
        diagnostics.append(
            f"Hyper evidence project path is unavailable ({type(exc).__name__}).")
        return result

    source_identities, source_issues, extracted_uses_hyper = (
        _hyper_source_identity_inventory(extracted))
    diagnostics.extend(source_issues)
    tmdl_tables, tmdl_issues, tmdl_uses_hyper = (
        _hyper_project_tmdl_tables(project_root))
    data_dir = os.path.join(project_root, "Data")
    data_uses_hyper, data_issues = _hyper_project_data_signals(data_dir)
    manifest_path = os.path.join(data_dir, "hyper_exports.json")
    manifest_status, manifest, manifest_uses_hyper, manifest_issue = (
        _hyper_manifest_preflight(manifest_path))
    if manifest_issue:
        diagnostics.append(manifest_issue)
    uses_hyper = (
        extracted_uses_hyper or tmdl_uses_hyper or manifest_uses_hyper
        or data_uses_hyper)
    if not uses_hyper:
        result["manifest_status"] = manifest_status
        result["diagnostics"] = []
        return result

    result["status"] = "failed"
    result["manifest_status"] = manifest_status
    diagnostics.extend(tmdl_issues)
    diagnostics.extend(data_issues)
    if manifest_status == "missing":
        diagnostics.append(
            "Hyper is required, but Data/hyper_exports.json is missing; "
            "full export proof cannot be established.")

    data_missing = False
    try:
        data_info = os.lstat(data_dir)
    except FileNotFoundError:
        data_info = None
        data_missing = True
    except OSError as exc:
        data_info = None
        diagnostics.append(
            f"Project Data directory cannot be inspected "
            f"({type(exc).__name__}).")
    if data_info is None and data_missing:
        diagnostics.append(
            "Hyper is required, but the project Data directory is missing.")
    elif data_info is not None and (
            _hyper_is_link(data_dir, data_info)
          or not stat.S_ISDIR(data_info.st_mode)):
        diagnostics.append(
            "Project Data path must be a regular directory, not a link.")

    records = manifest.get("exports", []) if isinstance(manifest, dict) else []
    if manifest is not None and not records:
        diagnostics.append(
            "Hyper is required, but the versioned export manifest has no records.")
    exports: list[dict[str, Any]] = []
    identity_to_exports: dict[tuple[str, ...], list[int]] = {}
    filename_to_indices: dict[str, list[int]] = {}
    for index, raw_record in enumerate(records):
        if not isinstance(raw_record, dict):
            message = f"Hyper export record {index} must be a JSON object."
            diagnostics.append(message)
            exports.append({
                "manifest_index": index,
                "filename": None,
                "format": None,
                "identity": None,
                "row_count": None,
                "total_rows": None,
                "bindings": [],
                "status": "failed",
                "diagnostics": [message],
            })
            continue
        item, key = _hyper_validate_export_record(
            raw_record, index, data_dir)
        if key is not None:
            identity_to_exports.setdefault(key, []).append(index)
        filename = raw_record.get("filename")
        if isinstance(filename, str):
            filename_to_indices.setdefault(filename, []).append(index)
        exports.append(item)
        diagnostics.extend(item["diagnostics"])

    for key, indices in sorted(
            identity_to_exports.items(), key=lambda pair: pair[0]):
        if len(indices) > 1:
            identity = exports[indices[0]].get("identity")
            diagnostics.append(
                "Hyper export manifest contains multiple records for one exact "
                f"physical identity ({_hyper_identity_label(identity)}).")
    for filename, indices in sorted(filename_to_indices.items()):
        if len(indices) > 1:
            diagnostics.append(
                f"Hyper export manifest reuses filename {filename!r} across records.")
            for index in indices:
                message = (
                    f"Hyper export record {index}: filename is duplicated in "
                    "the manifest.")
                if message not in exports[index].get("diagnostics", []):
                    exports[index].setdefault("diagnostics", []).append(message)

    annotated_identities: dict[tuple[str, ...], int] = {}
    for table in tmdl_tables:
        identity_key = table.get("identity_key")
        if identity_key is not None:
            annotated_identities[identity_key] = (
                annotated_identities.get(identity_key, 0) + 1)
    for key, aliases in sorted(source_identities.items(), key=lambda pair: pair[0]):
        label = _hyper_identity_label({
            "schema": key[-2], "table": key[-1],
        })
        if key not in annotated_identities:
            diagnostics.append(
                f"Extracted Hyper identity has no final table annotation: {label}.")
        alias_count = len({alias for alias in aliases if alias}) or 1
        if annotated_identities.get(key, 0) < alias_count:
            diagnostics.append(
                "Final Hyper table annotations do not cover every extracted alias "
                f"for {label} ({annotated_identities.get(key, 0)} final / "
                f"{alias_count} source).")
    if not tmdl_tables:
        diagnostics.append(
            "Hyper source evidence has no final table with an exact "
            "Migration_HyperIdentity annotation.")

    data_folder_cache: Dict[
        str, tuple[Optional[str], Optional[str]]
    ] = {}
    for table in tmdl_tables:
        identity_key = table.get("identity_key")
        relative = table["relative_path"]
        if identity_key is None:
            diagnostics.append(
                f"{relative}: exact Hyper identity cannot be bound to an export.")
            continue
        matching = identity_to_exports.get(identity_key, [])
        if len(matching) != 1:
            diagnostics.append(
                f"{relative}: exact Hyper identity resolves to {len(matching)} "
                "manifest export records; exactly one is required.")
            continue
        export_index = matching[0]
        export = exports[export_index]
        binding, binding_issues = _hyper_verify_table_binding(
            table, export, project_root, data_dir, data_folder_cache)
        export["bindings"].append(binding)
        result["bindings"].append({
            "identity": export.get("identity"),
            "filename": export.get("filename"),
            **binding,
        })
        diagnostics.extend(
            f"{relative}: {issue}" for issue in binding_issues)

    for index, export in enumerate(exports):
        if not export.get("bindings"):
            message = (
                "required Hyper export is not consumed by a final annotated "
                "Import table")
            export.setdefault("diagnostics", []).append(message)
            diagnostics.append(
                f"Hyper export record {index} is unconsumed by a final annotated "
                "Import table.")
        export["status"] = (
            "static_verified"
            if not export.get("diagnostics")
            and export.get("bindings")
            and all(binding.get("status") == "static_verified"
                    for binding in export["bindings"])
            else "failed"
        )
    if extracted_uses_hyper and not source_identities and not tmdl_tables:
        diagnostics.append(
            "Hyper source is present, but no exact explicit physical identity "
            "is available; filename/table heuristics are not accepted.")

    result["exports"] = exports
    result["bindings"] = sorted(
        result["bindings"],
        key=lambda item: (
            str(item.get("table_file", "")).casefold(),
            str(item.get("partition", "")).casefold(),
            str(item.get("filename", "")).casefold(),
        ),
    )
    result["diagnostics"] = sorted(set(diagnostics))
    result["status"] = (
        "static_verified" if not result["diagnostics"] else "failed")
    result["source_identity_count"] = len(source_identities)
    result["final_annotated_table_count"] = len(tmdl_tables)
    result["runtime_status"] = "not_run"
    return result


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
            "parity_features_unchecked": parity.get("evidence_coverage", {}).get(
                "unchecked_features", 0),
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


def resolve_quality_report_name(project_dir: str) -> str:
    """Resolve the generated identity, never the source filename or first match.

    Artifacts and migration metadata must agree. Missing artifacts can still be
    diagnosed when metadata supplies the identity; absent or ambiguous identity
    is an explicit error rather than a misleading quality report.
    """
    from powerbi_import.qa_suite import _find_report_dir

    identities = set()
    reports = []
    for entry in os.listdir(project_dir):
        path = os.path.join(project_dir, entry)
        stem, suffix = os.path.splitext(entry)
        if suffix == '.pbip' and os.path.isfile(path):
            identities.add(stem)
        elif suffix in {'.Report', '.SemanticModel', '.Lakehouse'} and os.path.isdir(path):
            if suffix == '.Report':
                reports.append(entry)
            else:
                identities.add(stem)
    if len(reports) > 1:
        raise ValueError("Ambiguous project identity: multiple .Report artifacts")
    if reports:
        # Reuse the report-directory discovery helper only after ruling out
        # its first-match behaviour for ambiguous projects.
        report_dir = _find_report_dir(project_dir)
        identities.add(os.path.basename(report_dir)[:-len('.Report')])

    metadata_path = os.path.join(project_dir, 'migration_metadata.json')
    if os.path.isfile(metadata_path):
        with open(metadata_path, encoding='utf-8') as stream:
            metadata = json.load(stream)
        name = metadata.get('report_name') if isinstance(metadata, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Project metadata has no valid report_name")
        identities.add(name)
    if not identities:
        raise ValueError("Project identity unavailable: no named artifacts or metadata")
    if len(identities) != 1:
        raise ValueError(
            "Ambiguous project identity: artifacts/metadata disagree: "
            + ', '.join(sorted(identities)))
    name = identities.pop()
    if name in {'.', '..'} or any(c in name for c in '/\\\x00:'):
        raise ValueError("Invalid project identity")
    return name


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

    findings = _Findings()
    blockers = findings.blockers
    warnings = findings.warnings
    hyper_sampling = _hyper_sampling_evidence(extracted or {}, project_dir)
    if fabric_evidence.get("status") == "not_present":
        hyper_import = build_hyper_import_evidence(
            extracted or {}, project_dir)
    else:
        hyper_import = {
            "status": "not_applicable",
            "runtime_status": "not_run",
            "manifest_status": "not_applicable",
            "manifest": _HYPER_MANIFEST,
            "diagnostics": [],
            "exports": [],
            "bindings": [],
            "verification_scope": (
                "Fabric DirectLake uses separate current sampling evidence; "
                "full PBIP Hyper Import validation was not invoked."),
        }
    data = {
        **data,
        "hyper_sampling": hyper_sampling,
        "hyper_import": hyper_import,
    }
    for diagnostic in hyper_import.get("diagnostics", []) or []:
        findings.add(
            "hyper_import",
            diagnostic,
            blocker=True,
            fix="Repair full Hyper export proof or final Import binding, then "
                "rerun the deterministic static quality gate.",
            evidence=[hyper_import.get("manifest", _HYPER_MANIFEST)],
        )
    for diagnostic in hyper_sampling["diagnostics"]:
        findings.add("hyper_manifest", diagnostic, blocker=False,
                     evidence=[hyper_sampling["manifest"]])
    if hyper_sampling["sampled_export_count"]:
        findings.add(
            "hyper_sampling",
            f"{hyper_sampling['sampled_export_count']} Hyper export(s) are "
            "sampled, capped or truncated; migration quality does not represent "
            "full source data or verify full-row parity.",
            blocker=False,
            fix="Export without row caps or sample-only readers, then run "
                "authorized full-data validation.",
            evidence=[hyper_sampling["manifest"]],
        )
    if not openability.openable:
        for issue in openability.blocking_issues:
            findings.add("openability", issue, blocker=True)
    fabric_invalid = fabric_evidence.get("status") == "invalid"
    if fabric_invalid:
        findings.by_policy("fabric_bundle",
                           "Fabric-native artifact bundle failed validation.",
                           policy["fabric_bundle"])
    if policy["fabric_runtime"] == "blocker" and fabric_evidence.get("status") == "locally_valid":
        runtime = fabric_evidence.get("runtime", {})
        missing_runtime = [name for name in (
            "deployment", "refresh", "semantic_execution", "post_deploy"
        ) if runtime.get(name) != "passed"]
        if missing_runtime:
            findings.add(
                "fabric_runtime",
                "Fabric production evidence is incomplete: "
                + ", ".join(missing_runtime) + ".",
                blocker=True,
            )
    assessment_failures = _assessment_blocking_failures(assessment)
    assessment_blocking_failures = bool(assessment_failures)
    if assessment_blocking_failures:
        findings.add("assessment_failures",
                     "Pre-migration assessment contains blocking failures.",
                     blocker=True, evidence=assessment_failures)
    elif assessment.overall_score == "RED":
        findings.add(
            "assessment_red",
            "Pre-migration assessment is RED due to performance risk; review before production.",
            blocker=False,
        )
    if any(gap.get("status") == "unsupported"
           for gap in parity.get("gaps", [])):
        findings.add("parity_unsupported",
                     "Unsupported Tableau features remain in use.", blocker=True)
    if parity.get("untracked_features"):
        names = ", ".join(parity["untracked_features"])
        findings.add("parity_untracked",
                     f"Feature families lack parity mappings: {names}.",
                     blocker=False)
    if data.get("summary", {}).get("tables_found", 0) < data.get("summary", {}).get("source_tables", 0):
        findings.add(
            "tables_missing",
            "One or more extracted source tables are missing from the target model.",
            blocker=True,
        )
    if not interface.get("filters", {}).get("covered", True):
        findings.add(
            "filter_coverage",
            "Interface filter coverage is below the extracted source count.",
            blocker=False,
        )
    if not interface.get("parameters", {}).get("covered", True):
        findings.add("parameter_coverage",
                     "One or more extracted parameters lack a target symbol.",
                     blocker=False)
    _assessment_check_findings(assessment, findings)

    lineage = _lineage_evidence(extracted or {}, data, interface, parity)
    lineage_contract = _load_lineage_contract(project_dir)
    lineage["contract"] = lineage_contract
    unresolved_lineage = _source_lineage_unresolved(lineage_contract, extracted or {})
    lineage_contract["unresolved_source"] = unresolved_lineage
    if unresolved_lineage:
        findings.by_policy(
            "unresolved_lineage",
            f"Semantic lineage has {len(unresolved_lineage)} unresolved "
            "source-to-target record(s).",
            policy["unresolved_lineage"],
        )
    if semantic_issue_count:
        findings.by_policy(
            "semantic_static",
            f"Semantic validation found {semantic_issue_count} static "
            "context issue(s).",
            policy["semantic_diagnostics"],
        )
    if semantic_runtime_failures:
        findings.by_policy(
            "semantic_runtime",
            f"Semantic runtime validation failed for {semantic_runtime_failures} "
            "query(ies).",
            policy["semantic_diagnostics"],
        )
    fallback_in_use = m_emitters.get("fallback_in_use", [])
    if fallback_in_use:
        findings.by_policy(
            "m_fallback",
            f"M fallback emitters are in use for {len(fallback_in_use)} "
            "connector path(s).",
            policy["m_fallback"],
        )
    visual_approximations = find_visual_approximations_in_use(extracted or {})
    if visual_approximations:
        findings.by_policy(
            "visual_approximation",
            f"Visual mapping contains {len(visual_approximations)} explicit approximation(s).",
            policy["visual_approximation"],
        )

    review = _review_evidence(project_dir)
    _record_review_findings(review, findings)

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
    priorities = _build_priorities(parity, findings)
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
    evidence_manifest["hyper_sampling"] = hyper_sampling
    evidence_manifest["hyper_import"] = hyper_import
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
        hyper_import=hyper_import,
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
        review=review,
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
