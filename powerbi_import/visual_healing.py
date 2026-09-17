"""Confidence-scored PBIR visual self-healing (v43/v44, Sprint 211.3).

Companion to ``dax_healing`` / ``m_healing`` for the PBIR report side. Small,
idempotent healers that repair malformed ``visual.json`` containers so Power BI
Desktop loads them instead of failing.

The flagship healer fixes the **critical annotation-placement bug** (PBIR
annotations MUST live at the visual.json *container* root, never inside the inner
``visual`` object — otherwise the report fails to load).

Design principles (shared with the DAX/M healers):
    * Idempotent — running a healer twice yields no further change.
    * Non-degrading — a healer never alters an already-valid container.
    * Confidence-scored — every repair records a confidence for review.

Public API:
    heal_visual(container) -> VisualHealReport
    heal_visuals(containers) -> list[VisualHealReport]
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from powerbi_import.healing_core import HealAction, HIGH, MEDIUM, LOW

# Minimum sane visual dimensions (Power BI rejects zero/negative sizes).
_MIN_W = 40.0
_MIN_H = 40.0


@dataclass
class VisualHealReport:
    """Result of healing one PBIR visual container."""
    original: Dict
    healed: Dict
    actions: List[HealAction] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.original != self.healed

    def to_dict(self) -> Dict:
        return {
            "changed": self.changed,
            "name": self.healed.get("name", ""),
            "actions": [a.to_dict() for a in self.actions],
        }


def _snip(obj) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(obj)
    return s if len(s) <= 200 else s[:197] + "..."


# ════════════════════════════════════════════════════════════════════
#  Individual healers — each mutates ``container`` in place, returns action
# ════════════════════════════════════════════════════════════════════

def heal_annotations_placement(container: Dict) -> Optional[HealAction]:
    """Move annotations from the inner ``visual`` object to the container root.

    PBIR requires annotations at the container root (sibling of name/position/
    visual). An inner-visual placement is a CRITICAL load failure.
    """
    visual = container.get("visual")
    if not isinstance(visual, dict):
        return None
    inner = visual.get("annotations")
    if not inner:
        return None
    before = _snip({"visual.annotations": inner})
    root = container.get("annotations")
    if isinstance(root, list) and isinstance(inner, list):
        # merge, avoiding duplicates
        merged = root + [a for a in inner if a not in root]
        container["annotations"] = merged
    else:
        container["annotations"] = inner
    del visual["annotations"]
    return HealAction(
        "annotations_placement", "visual", HIGH, before,
        _snip({"annotations": container["annotations"]}),
        "moved annotations from inner visual to container root (PBIR load fix)")


def heal_position(container: Dict) -> Optional[HealAction]:
    """Ensure the container position has non-negative x/y and sane width/height."""
    pos = container.get("position")
    if not isinstance(pos, dict):
        return None
    before = dict(pos)
    changed = False
    for key, minimum in (("width", _MIN_W), ("height", _MIN_H)):
        val = pos.get(key)
        if not isinstance(val, (int, float)) or val <= 0:
            pos[key] = minimum
            changed = True
    for key in ("x", "y"):
        val = pos.get(key)
        if isinstance(val, (int, float)) and val < 0:
            pos[key] = 0
            changed = True
    if not changed:
        return None
    return HealAction(
        "position", "visual", MEDIUM, _snip(before), _snip(pos),
        "repaired invalid visual position/size")


def heal_missing_name(container: Dict) -> Optional[HealAction]:
    """Ensure the container has a non-empty ``name``.

    Derives a deterministic name from the visual type when missing so repeated
    heals produce the same name (idempotent).
    """
    name = container.get("name")
    if isinstance(name, str) and name.strip():
        return None
    visual = container.get("visual")
    vtype = visual.get("visualType", "visual") if isinstance(visual, dict) else "visual"
    new_name = f"healed_{vtype}"
    container["name"] = new_name
    return HealAction(
        "missing_name", "visual", MEDIUM, _snip({"name": name}),
        _snip({"name": new_name}), "assigned a deterministic visual name")


def heal_visual_type(container: Dict) -> Optional[HealAction]:
    """Default a missing ``visualType`` to a table when the visual has content.

    Only fires when the inner ``visual`` exists and carries a query/projections
    but no ``visualType`` — an empty-type visual otherwise fails to render.
    """
    visual = container.get("visual")
    if not isinstance(visual, dict):
        return None
    if visual.get("visualType"):
        return None
    has_content = bool(visual.get("query") or visual.get("projections")
                       or visual.get("objects"))
    if not has_content:
        return None
    visual["visualType"] = "tableEx"
    return HealAction(
        "visual_type", "visual", LOW, _snip({"visualType": None}),
        _snip({"visualType": "tableEx"}),
        "defaulted missing visualType to a table (nearest safe fallback)")


# ════════════════════════════════════════════════════════════════════
#  Orchestrator
# ════════════════════════════════════════════════════════════════════

_HEALERS = (
    heal_annotations_placement,
    heal_position,
    heal_missing_name,
    heal_visual_type,
)


def heal_visual(container: Dict) -> VisualHealReport:
    """Apply all visual healers idempotently and return a report.

    The input container is not mutated; the healed copy is returned in the report.
    """
    original = copy.deepcopy(container)
    working = copy.deepcopy(container)
    actions: List[HealAction] = []
    for healer in _HEALERS:
        action = healer(working)
        if action is not None:
            actions.append(action)
    return VisualHealReport(original=original, healed=working, actions=actions)


def heal_visuals(containers: List[Dict]) -> List[VisualHealReport]:
    """Heal a list of visual containers; return a report per *changed* visual."""
    reports = []
    for c in containers:
        if not isinstance(c, dict):
            continue
        report = heal_visual(c)
        if report.changed:
            reports.append(report)
    return reports
