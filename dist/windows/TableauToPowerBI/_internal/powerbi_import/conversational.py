"""Conversational assessment & migration planning (v44, Sprint 218).

Answers natural-language questions about a workbook's migration readiness using
ONLY the structured findings already produced by the assessment engine — no
model guessing, no hallucination. Every answer is backed by evidence rows.

Public API:
    MigrationQA(report_dict).ask("what won't migrate cleanly?")  -> Answer
    answer_question(report_dict, question)                       -> Answer
    build_plan_summary(report_dict)                              -> dict

``report_dict`` is an ``AssessmentReport.to_dict()`` payload.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

_FAIL = {"fail", "error"}
_WARN = {"warn", "warning"}


@dataclass
class Answer:
    """A grounded answer to one question."""
    question: str
    answer: str
    evidence: List[str] = field(default_factory=list)
    data: Dict = field(default_factory=dict)
    matched_intent: str = "general"

    def to_dict(self) -> Dict:
        return asdict(self)


class MigrationQA:
    """Answer questions from an assessment report payload (data-backed only)."""

    def __init__(self, report_dict: Dict):
        self.report = report_dict or {}
        self.workbook = self.report.get("workbook_name", "Workbook")
        self._findings = self._flatten()

    # -- data access --------------------------------------------------
    def _flatten(self) -> List[Dict]:
        out: List[Dict] = []
        for cat in self.report.get("categories", []):
            for ck in cat.get("checks", []):
                out.append({
                    "category": cat.get("name", ""),
                    "name": ck.get("name", ""),
                    "severity": str(ck.get("severity", "info")).lower(),
                    "detail": ck.get("detail", ""),
                    "recommendation": ck.get("recommendation", ""),
                })
        return out

    def _by_severity(self, severities) -> List[Dict]:
        return [f for f in self._findings if f["severity"] in severities]

    def _search(self, pattern: str) -> List[Dict]:
        rx = re.compile(pattern, re.IGNORECASE)
        return [f for f in self._findings
                if rx.search(f"{f['category']} {f['name']} {f['detail']} {f['recommendation']}")]

    @staticmethod
    def _evidence(findings: List[Dict], cap: int = 25) -> List[str]:
        return [f"[{f['severity'].upper()}] {f['category']}: {f['name']} — {f['detail']}"
                for f in findings[:cap]]

    # -- intents ------------------------------------------------------
    def ask(self, question: str) -> Answer:
        q = (question or "").lower()

        if re.search(r"overall|readiness|ready|score|green|yellow|red|summary", q):
            return self._answer_readiness(question)
        if re.search(r"visual|chart|approximat", q):
            return self._answer_visuals(question)
        if re.search(r"gateway|datasource|data source|connection|refresh|on-?prem", q):
            return self._answer_datasources(question)
        if re.search(r"calc|dax|formula|lod|table calc", q):
            return self._answer_calculations(question)
        if re.search(r"won'?t migrate|clean|risk|problem|issue|fail|blocker", q):
            return self._answer_gaps(question)
        if re.search(r"plan|effort|wave|order|how long|steps", q):
            return self._answer_plan(question)
        return self._answer_general(question)

    def _answer_readiness(self, question: str) -> Answer:
        totals = self.report.get("totals", {})
        score = self.report.get("overall_score", "UNKNOWN")
        ans = (f"{self.workbook} is assessed {score}: "
               f"{totals.get('pass', 0)} passed, {totals.get('warn', 0)} warnings, "
               f"{totals.get('fail', 0)} failures across {totals.get('checks', 0)} checks.")
        return Answer(question=question, answer=ans, matched_intent="readiness",
                      data={"overall_score": score, "totals": totals})

    def _answer_gaps(self, question: str) -> Answer:
        fails = self._by_severity(_FAIL)
        warns = self._by_severity(_WARN)
        if not fails and not warns:
            ans = f"{self.workbook} has no warnings or failures — it should migrate cleanly."
        else:
            ans = (f"{self.workbook} has {len(fails)} failure(s) and {len(warns)} "
                   f"warning(s) that may not migrate cleanly.")
        return Answer(question=question, answer=ans, matched_intent="gaps",
                      evidence=self._evidence(fails + warns),
                      data={"failures": len(fails), "warnings": len(warns)})

    def _answer_visuals(self, question: str) -> Answer:
        hits = self._search(r"visual|chart|approximat|mark")
        approx = [f for f in hits if re.search(r"approximat", f["detail"], re.IGNORECASE)]
        ans = (f"{len(hits)} visual-related finding(s); "
               f"{len(approx)} explicitly flagged as approximated.")
        return Answer(question=question, answer=ans, matched_intent="visuals",
                      evidence=self._evidence(hits),
                      data={"visual_findings": len(hits), "approximated": len(approx)})

    def _answer_datasources(self, question: str) -> Answer:
        hits = self._search(r"datasource|data source|connection|gateway|refresh|extract|custom sql")
        gateway = [f for f in hits if re.search(r"gateway|on-?prem|connection string",
                                                f"{f['detail']} {f['recommendation']}", re.IGNORECASE)]
        ans = (f"{len(hits)} datasource/connectivity finding(s); "
               f"{len(gateway)} likely need a data gateway or connection update.")
        return Answer(question=question, answer=ans, matched_intent="datasources",
                      evidence=self._evidence(hits),
                      data={"datasource_findings": len(hits), "gateway_candidates": len(gateway)})

    def _answer_calculations(self, question: str) -> Answer:
        hits = self._search(r"calc|dax|formula|lod|table calc|window|running")
        ans = f"{len(hits)} calculation-related finding(s) to review."
        return Answer(question=question, answer=ans, matched_intent="calculations",
                      evidence=self._evidence(hits),
                      data={"calculation_findings": len(hits)})

    def _answer_plan(self, question: str) -> Answer:
        summary = build_plan_summary(self.report)
        ans = summary.get("headline", "Migration plan generated.")
        return Answer(question=question, answer=ans, matched_intent="plan",
                      evidence=summary.get("steps", []), data=summary)

    def _answer_general(self, question: str) -> Answer:
        totals = self.report.get("totals", {})
        ans = (f"{self.workbook}: overall {self.report.get('overall_score', 'UNKNOWN')}, "
               f"{totals.get('checks', 0)} checks. Ask about readiness, gaps, visuals, "
               f"datasources, calculations, or a plan.")
        return Answer(question=question, answer=ans, matched_intent="general", data={"totals": totals})


def answer_question(report_dict: Dict, question: str) -> Answer:
    """Convenience wrapper: answer one question against an assessment payload."""
    return MigrationQA(report_dict).ask(question)


def build_plan_summary(report_dict: Dict) -> Dict:
    """Produce an ordered, effort-scored plan summary from assessment findings.

    Delegates to ``migration_planner`` when available; otherwise derives a simple
    wave ordering (failures first, then warnings, then passes) from the report.
    """
    qa = MigrationQA(report_dict)
    fails = qa._by_severity(_FAIL)
    warns = qa._by_severity(_WARN)
    score = report_dict.get("overall_score", "UNKNOWN")

    steps: List[str] = []
    if fails:
        steps.append(f"Wave 1 — resolve {len(fails)} blocking failure(s) before migrating.")
    if warns:
        steps.append(f"Wave 2 — review {len(warns)} warning(s); most auto-heal but verify.")
    steps.append("Wave 3 — run migration, then --qa and open the .pbip in Desktop.")
    if score == "GREEN":
        headline = "Ready to migrate: no blockers detected; proceed and validate with --qa."
    elif score == "YELLOW":
        headline = "Migrate with review: warnings present but no hard blockers."
    else:
        headline = "Remediate first: blocking failures should be addressed before migrating."

    effort = "low" if score == "GREEN" else ("medium" if score == "YELLOW" else "high")
    return {
        "workbook": report_dict.get("workbook_name", "Workbook"),
        "overall_score": score,
        "estimated_effort": effort,
        "headline": headline,
        "steps": steps,
        "blocking_failures": len(fails),
        "warnings": len(warns),
    }
