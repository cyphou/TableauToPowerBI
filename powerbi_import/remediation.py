"""Natural-language remediation for migration findings (v44, Sprint 217).

Turns structured findings — assessment checks, healing ledger entries, parity
gaps — into plain-language explanations plus *actionable, source-targeted* fix
suggestions, each carrying a confidence score and the owning file/agent.

Design principles (see docs/ROADMAP.md v44.0.0):
    * AI is assistive, never authoritative — suggestions are labelled with a
      confidence and never auto-applied.
    * Offline-first — a deterministic template layer produces useful output with
      NO network access. An optional LLM pass (``refine_with_llm``) only *augments*.
    * Grounded — routing to owning agent/file comes from the repo's ownership map,
      not from a model guess.

Public API:
    explain_finding(finding)               -> RemediationSuggestion
    remediate_assessment(report_dict)      -> RemediationReport
    remediate_findings(findings)           -> RemediationReport
    refine_with_llm(report, llm_client)    -> RemediationReport  (optional)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

# ── Ownership map: keyword → (owning file, owning agent) ─────────────────────
# Ordered most-specific first; first match wins.
_OWNERSHIP = [
    (r"\bDAX\b|measure|aggregation|LOD|table calc|RUNNING_|WINDOW_|RANK|SUMX",
     ("tableau_export/dax_converter.py", "@dax")),
    (r"\bM\b|power query|mashup|Table\.|connector|\.pq\b|partition",
     ("tableau_export/m_query_builder.py", "@wiring")),
    (r"relationship|cardinality|RLS|role|hierarchy|calendar|calc group|field parameter",
     ("powerbi_import/tmdl_generator.py", "@semantic")),
    (r"visual|slicer|bookmark|theme|conditional format|reference line|axis|tooltip|drill",
     ("powerbi_import/visual_generator.py", "@visual")),
    (r"datasource|extract|hyper|custom sql|blend|connection string",
     ("tableau_export/extract_tableau_data.py", "@extractor")),
    (r"deploy|gateway|workspace|fabric|refresh schedule|credential",
     ("powerbi_import/deploy/", "@deployer")),
    (r"prep flow|\.tfl|lineage",
     ("tableau_export/prep_flow_parser.py", "@extractor")),
]

# ── Remediation templates: keyword → (explanation, suggested action, confidence) ─
# Confidence: high (deterministic mapping), medium (general guidance), low (unknown).
_TEMPLATES = [
    (r"\bLOD\b|FIXED|INCLUDE|EXCLUDE",
     "This calculation uses a Tableau Level-of-Detail (LOD) expression. LODs map to "
     "DAX CALCULATE with ALLEXCEPT/REMOVEFILTERS; nested or mixed-grain LODs may only "
     "approximate.",
     "Review the generated DAX for the LOD measure; verify the grain matches the "
     "Tableau {FIXED}/{INCLUDE}/{EXCLUDE} dimensions. If approximated, add the missing "
     "filter context in tableau_export/dax_converter.py.",
     "high"),
    (r"table calc|RUNNING_|WINDOW_|RANK|LOOKUP|FIRST\(|LAST\(|INDEX\(",
     "This uses a Tableau table calculation whose addressing/partitioning has no exact "
     "DAX equivalent and is converted to a windowed CALCULATE/RANKX pattern.",
     "Confirm the partition/addressing direction in the visual matches the intended "
     "window. For WINDOW_* variants, verify the DAX aggregation choice in dax_converter.py.",
     "high"),
    (r"blend|data blending|multiple datasource",
     "Tableau data blending (primary/secondary linked on shared dimensions) becomes a "
     "Power BI model relationship, which behaves differently (no per-sheet blend).",
     "Verify the inferred relationship direction and cardinality in the model; if the "
     "blend was directional, set crossFilteringBehavior accordingly.",
     "medium"),
    (r"RLS|user filter|USERNAME|ISMEMBEROF|security",
     "Tableau user filters / security functions map to Power BI Row-Level Security roles "
     "using USERPRINCIPALNAME(); group logic (ISMEMBEROF) becomes one role per group.",
     "Assign Azure AD members to each generated RLS role in the Power BI Service, then "
     "validate with 'View as role' in Desktop.",
     "high"),
    (r"relationship|cardinality|circular|ambiguous",
     "A model relationship could not be resolved unambiguously (cardinality, direction, "
     "or a circular/ambiguous path).",
     "Open Model view, verify the active relationship and cardinality; break ambiguity "
     "by making one path inactive or adding a bridge table.",
     "medium"),
    (r"forecast|cluster|trend line|distribution band",
     "This Tableau analytics-pane item (forecast/clustering/trend/band) has no exact "
     "native Power BI target and is approximated or noted.",
     "Use the nearest Power BI analytics feature (trend line / reference line). For "
     "forecast/clustering, consider a DAX measure or a documented manual step.",
     "low"),
    (r"extract|\.hyper|tde|data volume|large",
     "The workbook relies on a Tableau extract (.hyper/.tde) or large data volume.",
     "Choose Import vs DirectQuery per the strategy advisor; for large data prefer "
     "DirectQuery or an incremental-refresh policy.",
     "medium"),
    (r"custom sql",
     "A custom SQL datasource was detected. It migrates to a Power Query native query.",
     "Validate the generated native query against the source and ensure the gateway/"
     "credentials are configured for refresh.",
     "medium"),
    (r"connection string|gateway|on-?prem",
     "The datasource connection needs a Power BI data gateway or updated connection "
     "details to refresh in the Service.",
     "Bind the dataset to an on-premises data gateway and set the datasource "
     "credentials after deployment.",
     "medium"),
    (r"unsupported|no equivalent|cannot|dropped",
     "This feature has no supported Power BI equivalent and would otherwise be lost.",
     "Review the migration note; implement the closest supported alternative or accept "
     "the documented gap. Nothing is silently dropped.",
     "low"),
]

_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


@dataclass
class RemediationSuggestion:
    """A single plain-language explanation + suggested fix for one finding."""
    finding: str
    category: str
    severity: str
    explanation: str
    suggested_action: str
    owning_file: str
    owning_agent: str
    confidence: str
    source: str = "template"          # template | llm

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class RemediationReport:
    """A set of remediation suggestions for one workbook/migration."""
    workbook: str
    suggestions: List[RemediationSuggestion] = field(default_factory=list)

    @property
    def low_confidence(self) -> List[RemediationSuggestion]:
        return [s for s in self.suggestions if s.confidence == "low"]

    def to_dict(self) -> Dict:
        return {
            "workbook": self.workbook,
            "count": len(self.suggestions),
            "low_confidence_count": len(self.low_confidence),
            "suggestions": [s.to_dict() for s in self.suggestions],
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


# ════════════════════════════════════════════════════════════════════
#  Core (offline, deterministic)
# ════════════════════════════════════════════════════════════════════

def _route_owner(text: str):
    for pattern, owner in _OWNERSHIP:
        if re.search(pattern, text, re.IGNORECASE):
            return owner
    return ("migrate.py", "@orchestrator")


def _match_template(text: str):
    for pattern, explanation, action, confidence in _TEMPLATES:
        if re.search(pattern, text, re.IGNORECASE):
            return explanation, action, confidence
    return (
        "This finding needs review; no specific remediation template matched.",
        "Inspect the finding detail and the owning module; confirm the generated "
        "artifact behaves as intended in Power BI Desktop.",
        "low",
    )


def explain_finding(finding: Dict) -> RemediationSuggestion:
    """Produce a remediation suggestion for a single finding dict.

    ``finding`` may come from an assessment check, a healing ledger entry, or a
    parity gap. Recognised keys (all optional): ``category``, ``name``/``title``,
    ``severity``, ``detail``/``description``, ``recommendation``.
    """
    category = str(finding.get("category", "") or "")
    name = str(finding.get("name") or finding.get("title") or "")
    severity = str(finding.get("severity", "") or finding.get("status", "") or "info")
    detail = str(finding.get("detail") or finding.get("description") or "")
    recommendation = str(finding.get("recommendation", "") or "")

    haystack = " ".join([category, name, detail, recommendation])
    explanation, action, confidence = _match_template(haystack)
    owning_file, owning_agent = _route_owner(haystack)

    # Prefer an explicit upstream recommendation when present.
    if recommendation:
        action = recommendation

    return RemediationSuggestion(
        finding=name or category or "(finding)",
        category=category or "General",
        severity=severity,
        explanation=explanation,
        suggested_action=action,
        owning_file=owning_file,
        owning_agent=owning_agent,
        confidence=confidence,
        source="template",
    )


def remediate_findings(findings: List[Dict], workbook: str = "Workbook",
                       min_severity: str = "warn") -> RemediationReport:
    """Build a remediation report from a flat list of finding dicts.

    ``min_severity`` filters out PASS/INFO by default (keep warn/fail/error).
    """
    keep = _severity_filter(min_severity)
    suggestions = [explain_finding(f) for f in findings if keep(f)]
    # Sort by confidence desc then severity for a readable report.
    suggestions.sort(key=lambda s: -_CONFIDENCE_RANK.get(s.confidence, 0))
    return RemediationReport(workbook=workbook, suggestions=suggestions)


def remediate_assessment(report_dict: Dict, min_severity: str = "warn") -> RemediationReport:
    """Build a remediation report from an ``AssessmentReport.to_dict()`` payload."""
    workbook = report_dict.get("workbook_name", "Workbook")
    findings: List[Dict] = []
    for cat in report_dict.get("categories", []):
        cat_name = cat.get("name", "")
        for ck in cat.get("checks", []):
            findings.append({
                "category": cat_name,
                "name": ck.get("name", ""),
                "severity": ck.get("severity", "info"),
                "detail": ck.get("detail", ""),
                "recommendation": ck.get("recommendation", ""),
            })
    return remediate_findings(findings, workbook=workbook, min_severity=min_severity)


def _severity_filter(min_severity: str):
    order = {"pass": 0, "info": 1, "warn": 2, "warning": 2, "fail": 3, "error": 3}
    threshold = order.get(str(min_severity).lower(), 2)

    def keep(f: Dict) -> bool:
        sev = str(f.get("severity", "") or f.get("status", "") or "info").lower()
        return order.get(sev, 1) >= threshold

    return keep


# ════════════════════════════════════════════════════════════════════
#  Optional LLM refinement (augments; never required)
# ════════════════════════════════════════════════════════════════════

def refine_with_llm(report: RemediationReport, llm_client, max_items: int = 20) -> RemediationReport:
    """Optionally sharpen low/medium-confidence suggestions via an LLM.

    ``llm_client`` must expose ``complete(prompt) -> str`` (or be falsy to no-op).
    Never raises on LLM failure; falls back to the template suggestion.
    """
    if not llm_client:
        return report
    refined = 0
    for s in report.suggestions:
        if refined >= max_items or s.confidence == "high":
            continue
        try:
            prompt = _build_prompt(s)
            raw = llm_client.complete(prompt)  # type: ignore[attr-defined]
            # Accept either a plain string or an object with a ``.text`` attribute
            # (e.g. LLMGateway's LLMResult), so the real gateway and test doubles
            # both work.
            text = getattr(raw, "text", raw)
            if isinstance(text, str) and text.strip():
                s.suggested_action = text.strip()
                s.source = "llm"
                refined += 1
        except Exception:  # noqa: BLE001 — LLM is best-effort
            continue
    return report


def _build_prompt(s: RemediationSuggestion) -> str:
    return (
        "You are helping migrate Tableau to Power BI. Given this finding, propose a "
        "concise, concrete fix (2-3 sentences). Do not invent Power BI features.\n\n"
        f"Category: {s.category}\nSeverity: {s.severity}\nFinding: {s.finding}\n"
        f"Context: {s.explanation}\nOwning module: {s.owning_file} ({s.owning_agent})\n"
    )


# ════════════════════════════════════════════════════════════════════
#  HTML rendering (self-contained; no external deps)
# ════════════════════════════════════════════════════════════════════

def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


_CONF_COLOR = {"high": "#107c10", "medium": "#ca5010", "low": "#a4262c"}


def _render_html(report: RemediationReport) -> str:
    rows = []
    for s in report.suggestions:
        color = _CONF_COLOR.get(s.confidence, "#605e5c")
        rows.append(
            "<tr>"
            f"<td>{_esc(s.finding)}</td>"
            f"<td>{_esc(s.category)}</td>"
            f"<td>{_esc(s.severity)}</td>"
            f"<td>{_esc(s.explanation)}</td>"
            f"<td>{_esc(s.suggested_action)}</td>"
            f"<td><code>{_esc(s.owning_file)}</code><br>{_esc(s.owning_agent)}</td>"
            f'<td style="color:{color};font-weight:600">{_esc(s.confidence)}</td>'
            f"<td>{_esc(s.source)}</td>"
            "</tr>"
        )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Remediation — {_esc(report.workbook)}</title>
<style>
 body {{ font-family: Segoe UI, sans-serif; margin: 24px; color: #201f1e; }}
 h1 {{ font-size: 20px; }}
 table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
 th, td {{ border: 1px solid #edebe9; padding: 8px; text-align: left; vertical-align: top; }}
 th {{ background: #f3f2f1; }}
 code {{ background: #f3f2f1; padding: 1px 4px; border-radius: 3px; }}
</style></head><body>
<h1>Remediation report — {_esc(report.workbook)}</h1>
<p>{len(report.suggestions)} suggestion(s), {len(report.low_confidence)} low-confidence.</p>
<table><thead><tr>
<th>Finding</th><th>Category</th><th>Severity</th><th>Explanation</th>
<th>Suggested action</th><th>Owner</th><th>Confidence</th><th>Source</th>
</tr></thead><tbody>
{''.join(rows)}
</tbody></table>
</body></html>"""
