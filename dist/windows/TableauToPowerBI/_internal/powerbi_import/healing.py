"""Unified facade for the self-healing subsystem (v44).

One coherent import surface over the whole self-healing stack so callers don't
have to know which leaf module a symbol lives in. Layered as:

    detection   → check_openability, extract_m_partitions, OpenabilityReport
    healers     → heal_dax, heal_m, heal_visual, heal_visuals
    contract    → HealAction, HealReport, VisualHealReport, HIGH/MEDIUM/LOW
    orchestration → AutoHealer, heal_dax_expression, error sources
    convenience → heal_and_verify()

Example::

    from powerbi_import.healing import heal_dax, check_openability, AutoHealer

    report = check_openability(project_dir)
    if not report.openable:
        AutoHealer().heal_project(project_dir)
"""

from __future__ import annotations

import os
from typing import Optional

# ── Contract (canonical) ────────────────────────────────────────────
from powerbi_import.healing_core import (
    HealAction, HealReport, HIGH, MEDIUM, LOW,
    CONFIDENCE_LEVELS, CONFIDENCE_RANK,
)

# ── Deterministic healers ───────────────────────────────────────────
from powerbi_import.dax_healing import heal_dax, heal_measures
from powerbi_import.m_healing import heal_m
from powerbi_import.visual_healing import heal_visual, heal_visuals, VisualHealReport

# ── Detection / preflight ───────────────────────────────────────────
from powerbi_import.openability import (
    check_openability, extract_m_partitions, OpenabilityReport,
)
from powerbi_import.desktop_probe import (
    probe_desktop_open, find_pbi_desktop, DesktopProbeReport,
)

# ── Closed-loop orchestration ───────────────────────────────────────
from powerbi_import.autoheal import (
    AutoHealer, heal_dax_expression, ErrorRecord, AutoHealReport, RepairAttempt,
    ErrorSource, StaticValidatorSource, LogFileSource, PbiDesktopSource,
)

__all__ = [
    # contract
    "HealAction", "HealReport", "VisualHealReport",
    "HIGH", "MEDIUM", "LOW", "CONFIDENCE_LEVELS", "CONFIDENCE_RANK",
    # healers
    "heal_dax", "heal_measures", "heal_m", "heal_visual", "heal_visuals",
    # detection
    "check_openability", "extract_m_partitions", "OpenabilityReport",
    "probe_desktop_open", "find_pbi_desktop", "DesktopProbeReport",
    # orchestration
    "AutoHealer", "heal_dax_expression", "ErrorRecord", "AutoHealReport",
    "RepairAttempt", "ErrorSource", "StaticValidatorSource", "LogFileSource",
    "PbiDesktopSource",
    # convenience
    "heal_and_verify",
]


def heal_and_verify(project_dir: str, *, gateway=None, autofix: bool = False,
                    max_iterations: int = 3):
    """Run the closed-loop autohealer then a fresh openability preflight.

    Returns ``(AutoHealReport, OpenabilityReport)``. This is the one-call entry
    point that ties healing to verification: heal the project, then confirm it
    would actually open in Power BI Desktop.
    """
    if not project_dir or not os.path.isdir(project_dir):
        raise FileNotFoundError(f"project_dir not found: {project_dir}")
    heal_report = AutoHealer(gateway=gateway, autofix=autofix,
                             max_iterations=max_iterations).heal_project(project_dir)
    open_report = check_openability(project_dir)
    return heal_report, open_report
