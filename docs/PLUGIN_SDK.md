# Plugin SDK v2 Guide

The Plugin SDK formalizes the previously-informal hook system into a versioned,
schema-validated API. It lives in `powerbi_import/plugin_sdk.py` and is fully
backward compatible with the legacy `plugins.py` auto-discovery mechanism.

## Concepts

- **`MigrationPlugin`** — the base class you subclass. Override only the hooks
  you need.
- **`PluginManifest`** — declares a plugin's identity (`name`, `version`,
  `api_version`), `hooks`, `dependencies`, and `tags`.
- **`PluginSDK`** — the host registry that validates and dispatches plugins.
- **`PluginTestRunner`** — a helper to assert your plugin's output is valid.

The current SDK API version is exposed as `SDK_API_VERSION`.

## Writing a plugin

```python
from powerbi_import.plugin_sdk import MigrationPlugin, PluginManifest


class UppercaseMeasures(MigrationPlugin):
    manifest = PluginManifest(
        name="uppercase-measures",
        version="1.0.0",
        author="you",
        description="Force measure DAX identifiers to a house style.",
        tags=["dax", "style"],
    )

    def on_convert_dax(self, name, dax):
        # Return a new string to replace the converted DAX, or None to skip.
        return dax.replace("SUM(", "SUM (")
```

You may also declare the manifest as a plain dict, or simply set `name` /
`version` class attributes — the SDK auto-builds a manifest and infers which
hooks you overrode.

### Versioned hooks

| Hook | Signature | Return |
|------|-----------|--------|
| `on_extract` | `(extracted: dict)` | replacement dict or `None` |
| `on_convert_dax` | `(name: str, dax: str)` | replacement DAX or `None` |
| `on_generate_visual` | `(tableau_mark: str, mapped_type: str)` | replacement PBI visual type or `None` |
| `on_validate` | `(report: dict)` | list of extra issue strings or `None` |

Returning `None` from a hook means "no change".

### Backward compatibility

The legacy v1 hook names (`post_extraction`, `transform_dax`,
`custom_visual_mapping`) are implemented on `MigrationPlugin` as adapters that
delegate to the v2 hooks. A v2 plugin can therefore be registered with the old
`PluginManager` from `plugins.py` without modification.

## Registering and dispatching

```python
from powerbi_import.plugin_sdk import PluginSDK

sdk = PluginSDK()
sdk.register(UppercaseMeasures())          # validates manifest on register

# Engine dispatch points (each is error-isolated):
extracted = sdk.dispatch_extract(extracted)
dax = sdk.dispatch_convert_dax("Sales", dax)
visual = sdk.dispatch_generate_visual("Bar", "clusteredBarChart")
issues = sdk.dispatch_validate(report)
```

`register()` raises `PluginValidationError` if the manifest is missing required
fields or declares an incompatible `api_version`. Dispatch is error-isolated:
a hook that raises is logged and skipped rather than aborting the migration.

## Manifest validation

```python
from powerbi_import.plugin_sdk import validate_manifest, PluginManifest

m = PluginManifest.from_dict({"name": "x", "version": "1.0.0"})
validate_manifest(m)   # raises PluginValidationError on mismatch
```

## Testing plugins

```python
from powerbi_import.plugin_sdk import PluginTestRunner

runner = PluginTestRunner()
runner.assert_dax_valid("CALCULATE(SUM(Sales[Amount]))")
runner.assert_m_valid('let Source = Table.FromRows({}) in Source')
runner.assert_visual_schema({"visualType": "clusteredBarChart"})

# Or exercise a whole plugin in one call:
runner.run_plugin(UppercaseMeasures(), dax="SUM(Sales[Amount])")
```

Assertions raise `PluginTestError` on failure.

## Marketplace v2

`powerbi_import/marketplace.py` provides a versioned `PatternRegistry` for
sharing DAX recipes, visual mappings, and M templates.

- **`register(pattern_dict)`** — add a versioned pattern to the catalogue.
- **`search(tags=, category=, name_pattern=)`** — discover patterns.
- **`resolve_dependencies(name, version=None)`** — transitively resolve a
  pattern's declared dependencies (cycle-safe).
- **`sync_remote(catalogue_url, dest_dir=None)`** — fetch a remote catalogue
  (e.g. a GitHub release asset) into the local registry.
- **`apply_dax_recipes(measures, ...)`** / **`apply_visual_overrides(map)`** —
  apply catalogued patterns to a migration in flight.

Curated industry packs (Healthcare, Finance, Retail) ship under
`examples/marketplace/packs/` and can be loaded into the registry.

## Files

- `powerbi_import/plugin_sdk.py` — SDK v2 implementation.
- `powerbi_import/marketplace.py` — Marketplace v2 / `PatternRegistry`.
- `powerbi_import/plugins.py` — legacy auto-discovery (still supported).
- `tests/test_plugin_sdk.py` — SDK test suite.
