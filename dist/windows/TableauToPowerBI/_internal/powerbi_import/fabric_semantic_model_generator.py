"""
Standalone Semantic Model generator for Microsoft Fabric.

Generates a Fabric SemanticModel item definition using DirectLake
mode with entity partitions referencing a Lakehouse.

Output structure:
    {model_name}.SemanticModel/
    ├── definition/
    │   ├── model.tmdl
    │   ├── database.tmdl
    │   ├── expressions.tmdl
    │   ├── relationships.tmdl
    │   ├── roles/
    │   │   └── *.tmdl
    │   └── tables/
    │       └── *.tmdl
    └── .platform
"""

import os
import json

# Import the existing tmdl_generator from the same package
from . import tmdl_generator
from .fabric_item import logical_id, write_platform


class FabricSemanticModelGenerator:
    """Generate a standalone Fabric SemanticModel artifact with DirectLake mode."""

    def __init__(self, project_dir, model_name, lakehouse_name=None,
                 item_id=None, lakehouse_id=None, workspace_id=None):
        self.project_dir = project_dir
        self.model_name = model_name
        self.lakehouse_name = lakehouse_name or f'{model_name}_Lakehouse'
        self.item_id = item_id or logical_id(model_name, 'SemanticModel')
        self.lakehouse_id = lakehouse_id or logical_id(model_name, 'Lakehouse')
        self.workspace_id = workspace_id or logical_id(model_name, 'Workspace')
        self.sm_dir = os.path.join(project_dir, f'{model_name}.SemanticModel')
        os.makedirs(self.sm_dir, exist_ok=True)

    def generate(self, extracted, calendar_start=None, calendar_end=None,
                 culture=None, languages=None):
        """Generate semantic-model files from extracted Tableau objects.

        Delegates TMDL generation to the existing tmdl_generator, then
        wraps the output in a Fabric SemanticModel item structure with
        DirectLake metadata.

        Args:
            extracted: dict with keys like 'datasources', 'calculations',
                       'hierarchies', 'parameters', 'user_filters', etc.
            calendar_start: Start year for Calendar table (default: 2020)
            calendar_end: End year for Calendar table (default: 2030)
            culture: Override culture/locale for semantic model (e.g., fr-FR)
            languages: Comma-separated additional locales

        Returns:
            dict with generation statistics.
        """
        datasources = extracted.get('datasources', [])
        extra_objects = {
            'sets': extracted.get('sets', []),
            'groups': extracted.get('groups', []),
            'bins': extracted.get('bins', []),
            'hierarchies': extracted.get('hierarchies', []),
            'parameters': extracted.get('parameters', []),
            'user_filters': extracted.get('user_filters', []),
            'sort_orders': extracted.get('sort_orders', []),
            'aliases': extracted.get('aliases', {}),
            'data_blending': extracted.get('data_blending', []),
            '_direct_lake': {
                'workspace_id': self.workspace_id,
                'lakehouse_id': self.lakehouse_id,
            },
        }

        # Output TMDL inside SemanticModel/definition
        # Note: generate_tmdl() creates its own 'definition/' subdirectory,
        # so pass sm_dir (not a pre-created definition_dir) to avoid double-nesting.
        os.makedirs(self.sm_dir, exist_ok=True)

        stats = tmdl_generator.generate_tmdl(
            datasources=datasources,
            report_name=self.model_name,
            extra_objects=extra_objects,
            output_dir=self.sm_dir,
            calendar_start=calendar_start,
            calendar_end=calendar_end,
            culture=culture,
            model_mode='directLake',
            languages=languages,
        )

        # Create .platform manifest
        self._write_platform_file()
        self._write_definition_file()

        # Create item metadata with DirectLake indicator
        self._write_item_metadata(stats)

        return stats

    def _write_platform_file(self):
        """Write the .platform manifest for the SemanticModel item."""
        write_platform(
            self.sm_dir,
            'SemanticModel',
            self.model_name,
            self.item_id,
        )

    def _write_definition_file(self):
        """Write the Fabric SemanticModel definition properties part."""
        definition = {
            "$schema": (
                "https://developer.microsoft.com/json-schemas/fabric/item/"
                "semanticModel/definitionProperties/1.0.0/schema.json"
            ),
            "version": "4.2",
            "settings": {"qnaEnabled": True},
        }
        path = os.path.join(self.sm_dir, 'definition.pbism')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(definition, f, indent=2)

    def _write_item_metadata(self, stats):
        """Write a metadata JSON for the semantic model."""

        def _make_safe(v):
            if isinstance(v, set):
                return list(v)
            if isinstance(v, tuple):
                return list(v)
            if isinstance(v, dict):
                out = {}
                for sk, sv in v.items():
                    # JSON only allows str/int/float/bool/None keys;
                    # tuple keys (e.g. (table, column) → dataType) are
                    # rewritten as "table::column" so the metadata
                    # remains valid JSON and round-trip readable.
                    if isinstance(sk, tuple):
                        sk = "::".join(str(p) for p in sk)
                    elif not isinstance(sk, (str, int, float, bool)) and sk is not None:
                        sk = str(sk)
                    out[sk] = _make_safe(sv)
                return out
            if isinstance(v, list):
                return [_make_safe(x) for x in v]
            return v

        safe_stats = {k: _make_safe(v) for k, v in stats.items()}

        meta = {
            "displayName": self.model_name,
            "type": "SemanticModel",
            "mode": "DirectLake",
            "lakehouse": self.lakehouse_name,
            "stats": safe_stats,
        }
        path = os.path.join(self.sm_dir, 'semantic_model_metadata.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, indent=2)
