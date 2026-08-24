"""Shared Fabric item identity and manifest helpers."""

from __future__ import annotations

import json
import os
import uuid

_PLATFORM_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/"
    "gitIntegration/platformProperties/2.0.0/schema.json"
)
_ITEM_NAMESPACE = uuid.UUID("fbc51465-8f87-4b5f-89df-3a93d8237fd9")


def logical_id(project_name: str, item_type: str) -> str:
    """Return a stable UUID for a generated Fabric item."""
    key = f"tableau-to-powerbi:{project_name}:{item_type}".lower()
    return str(uuid.uuid5(_ITEM_NAMESPACE, key))


def build_item_registry(project_name: str) -> dict:
    """Build stable logical IDs shared by every item in a Fabric bundle."""
    item_types = (
        "Lakehouse",
        "Dataflow",
        "Notebook",
        "SemanticModel",
        "Report",
        "DataPipeline",
    )
    return {
        item_type: logical_id(project_name, item_type)
        for item_type in item_types
    }


def write_platform(directory: str, item_type: str, display_name: str,
                   item_id: str) -> str:
    """Write a Fabric Git integration ``.platform`` manifest."""
    platform = {
        "$schema": _PLATFORM_SCHEMA,
        "metadata": {
            "type": item_type,
            "displayName": display_name,
        },
        "config": {
            "version": "2.0",
            "logicalId": item_id,
        },
    }
    path = os.path.join(directory, ".platform")
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(platform, stream, indent=2)
    return path
