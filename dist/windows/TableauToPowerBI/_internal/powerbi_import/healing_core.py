"""Canonical contract for the self-healing subsystem (v44).

This module is the single source of truth for the shared healing data model used
by every deterministic healer (``dax_healing``, ``m_healing``, ``visual_healing``)
and consumed by the orchestration (``autoheal``) and reporting (``recovery_report``)
layers.

Architecture (layers, top imports bottom — no cycles):

    healing.py (facade)            ← one import surface for everything below
        │
        ├── autoheal.py            ← closed-loop orchestration
        ├── openability.py         ← detection / preflight
        │
        ├── dax_healing.py         ← DAX healers
        ├── m_healing.py           ← Power Query (M) healers
        ├── visual_healing.py      ← PBIR visual healers
        │
        └── healing_core.py  (this) ← HealAction / HealReport / confidence

Keeping the contract here (rather than inside ``dax_healing``) removes the old
"leaf imports leaf" coupling where the M and visual healers reached into the DAX
module purely for the shared dataclasses.

Public API:
    HIGH / MEDIUM / LOW          confidence levels
    CONFIDENCE_LEVELS            ordered tuple (high→low)
    HealAction                   one applied/proposed repair
    HealReport                   result of healing one text expression
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List

# ── Confidence levels ───────────────────────────────────────────────
HIGH = "high"
MEDIUM = "medium"
LOW = "low"

#: Ordered high→low; useful for thresholding / escalation decisions.
CONFIDENCE_LEVELS = (HIGH, MEDIUM, LOW)

#: Numeric rank for comparisons (higher = more confident).
CONFIDENCE_RANK = {HIGH: 3, MEDIUM: 2, LOW: 1}


@dataclass
class HealAction:
    """A single applied (or proposed) repair.

    Shared by the DAX, M and visual healers so every fix is described uniformly
    (healer name, category, confidence, before/after, optional note) and can be
    surfaced in the unified recovery report or an autoheal ledger.
    """
    healer: str
    category: str
    confidence: str
    before: str
    after: str
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HealReport:
    """Result of healing one text expression (DAX or M)."""
    original: str
    healed: str
    actions: List[HealAction] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.original != self.healed

    def to_dict(self) -> dict:
        return {
            "original": self.original,
            "healed": self.healed,
            "changed": self.changed,
            "actions": [a.to_dict() for a in self.actions],
        }
