"""Versioned, sanitized semantic reference fixtures.

Fixtures contain generated DAX plus expected JSON-like rows. They are intended
for deterministic comparison tests and must not contain credentials or runtime
connection details.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List


FIXTURE_VERSION = 1
_SECRET_RE = re.compile(
    r"(?i)(password|passwd|secret|api[_-]?key|access[_-]?token|bearer\s+)"
)


def load_semantic_fixture(path: str) -> Dict[str, Any]:
    """Load and validate a versioned semantic reference fixture."""
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or payload.get("version") != FIXTURE_VERSION:
        raise ValueError(f"semantic fixture version must be {FIXTURE_VERSION}")
    queries = payload.get("queries")
    if not isinstance(queries, list):
        raise ValueError("semantic fixture queries must be a list")
    normalized: List[Dict[str, Any]] = []
    for index, query in enumerate(queries):
        if not isinstance(query, dict) or not query.get("name") or not query.get("dax"):
            raise ValueError(f"semantic fixture query {index} requires name and dax")
        serialized = json.dumps(query, ensure_ascii=False)
        if _SECRET_RE.search(serialized):
            raise ValueError(f"semantic fixture query {index} contains a credential-like field")
        normalized.append(dict(query))
    return {
        "version": FIXTURE_VERSION,
        "name": str(payload.get("name", "fixture")),
        "queries": normalized,
    }
