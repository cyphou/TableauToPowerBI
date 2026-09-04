"""Versioned, redaction-friendly evidence manifest for migration runs."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

MANIFEST_VERSION = "1.0"


def file_sha256(path: str) -> Optional[str]:
    """Return a file hash, or None when the source is unavailable."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def build_evidence_manifest(
    source_path: Optional[str] = None,
    *,
    target_path: Optional[str] = None,
    validation: Optional[Dict[str, Any]] = None,
    repairs: Optional[list] = None,
    environment: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a deterministic-shaped manifest without credentials or payloads."""
    source = {
        "name": os.path.basename(source_path) if source_path else None,
        "sha256": file_sha256(source_path) if source_path else None,
    }
    target = {
        "path": os.path.abspath(target_path) if target_path else None,
    }
    return {
        "manifest_version": MANIFEST_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "target": target,
        "validation": validation or {},
        "repairs": repairs or [],
        "environment": environment or {
            "semantic_execution": "not_run",
            "desktop": "not_run",
            "refresh": "not_run",
            "deployment": "not_run",
        },
    }


def save_evidence_manifest(manifest: Dict[str, Any], path: str) -> str:
    """Save a manifest as UTF-8 JSON."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False, default=str)
    return path
