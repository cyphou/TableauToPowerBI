"""Plugin SDK v2 — formal, versioned plugin interface (Sprint 188).

This module formalises the informal hook-based plugin system in ``plugins.py``
into a versioned SDK with a declared manifest, schema validation, and a built-in
testing framework. It is **backward-compatible**: a ``MigrationPlugin`` subclass
is also a valid ``PluginBase`` (duck-typed) and can be registered with the
existing ``PluginManager`` in ``plugins.py``.

Key concepts
------------
* :class:`PluginManifest` — declarative metadata (name, version, api_version,
  author, hooks, dependencies). Validated by :func:`validate_manifest`.
* :class:`MigrationPlugin` — base class with **versioned** lifecycle hooks:
  ``on_extract``, ``on_convert_dax``, ``on_generate_visual``, ``on_validate``.
  Legacy hook names (``post_extraction``, ``transform_dax`` …) are provided as
  thin adapters so a v2 plugin transparently works with the v1 manager.
* :class:`PluginTestRunner` — validates plugin output: ``assert_dax_valid``,
  ``assert_m_valid``, ``assert_visual_schema``.

Stdlib-only. No external dependencies.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger('tableau_to_powerbi.plugin_sdk')

# Current SDK API version. Plugins declare the api_version they target; the
# loader checks major-version compatibility.
SDK_API_VERSION = "2.0.0"

# Recognised versioned hook names (v2).
V2_HOOKS = (
    'on_extract',
    'on_convert_dax',
    'on_generate_visual',
    'on_validate',
)


# ════════════════════════════════════════════════════════════════
#  Manifest
# ════════════════════════════════════════════════════════════════

class PluginValidationError(ValueError):
    """Raised when a plugin manifest fails schema validation."""


class PluginManifest:
    """Declarative metadata describing a plugin.

    Required fields: ``name``, ``version``, ``api_version``.
    Optional: ``author``, ``description``, ``hooks`` (list of hook names the
    plugin implements), ``dependencies`` (list of ``name>=version`` specs),
    ``tags``.
    """

    REQUIRED = ('name', 'version', 'api_version')

    def __init__(self, name, version, api_version=SDK_API_VERSION,
                 author='', description='', hooks=None, dependencies=None,
                 tags=None):
        self.name = name
        self.version = version
        self.api_version = api_version
        self.author = author
        self.description = description
        self.hooks = list(hooks or [])
        self.dependencies = list(dependencies or [])
        self.tags = list(tags or [])

    @classmethod
    def from_dict(cls, data):
        """Build a manifest from a dict, validating required keys."""
        if not isinstance(data, dict):
            raise PluginValidationError("Manifest must be a dict")
        missing = [k for k in cls.REQUIRED if not data.get(k)]
        if missing:
            raise PluginValidationError(
                f"Manifest missing required field(s): {', '.join(missing)}")
        return cls(
            name=data['name'],
            version=data['version'],
            api_version=data.get('api_version', SDK_API_VERSION),
            author=data.get('author', ''),
            description=data.get('description', ''),
            hooks=data.get('hooks'),
            dependencies=data.get('dependencies'),
            tags=data.get('tags'),
        )

    def to_dict(self):
        return {
            'name': self.name,
            'version': self.version,
            'api_version': self.api_version,
            'author': self.author,
            'description': self.description,
            'hooks': list(self.hooks),
            'dependencies': list(self.dependencies),
            'tags': list(self.tags),
        }

    def __repr__(self):
        return f"<PluginManifest {self.name} v{self.version} (api {self.api_version})>"


def _parse_version(ver):
    """Parse a dotted version string into a comparable tuple."""
    parts = []
    for chunk in str(ver).split('.'):
        m = re.match(r'(\d+)', chunk.strip())
        parts.append(int(m.group(1)) if m else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def validate_manifest(manifest, sdk_version=SDK_API_VERSION):
    """Validate a manifest against the SDK schema.

    Checks required fields, hook-name validity, and major-version
    compatibility with the running SDK.

    Args:
        manifest: ``PluginManifest`` or dict.
        sdk_version: SDK API version to validate against.

    Returns:
        list[str]: validation warnings (empty if fully compatible).

    Raises:
        PluginValidationError: on hard schema violations.
    """
    if isinstance(manifest, dict):
        manifest = PluginManifest.from_dict(manifest)
    elif not isinstance(manifest, PluginManifest):
        raise PluginValidationError("manifest must be PluginManifest or dict")

    warnings = []

    # version strings must be parseable
    if _parse_version(manifest.version) == (0, 0, 0) and manifest.version not in ('0', '0.0', '0.0.0'):
        warnings.append(f"Unparseable version '{manifest.version}'")

    # major-version compatibility
    want = _parse_version(manifest.api_version)
    have = _parse_version(sdk_version)
    if want[0] != have[0]:
        raise PluginValidationError(
            f"Plugin targets API major v{want[0]}, SDK is v{have[0]} — incompatible")
    if want > have:
        warnings.append(
            f"Plugin targets newer API {manifest.api_version} than SDK {sdk_version}")

    # hook names
    for hook in manifest.hooks:
        if hook not in V2_HOOKS:
            warnings.append(f"Unknown hook '{hook}' (not in {V2_HOOKS})")

    # dependency spec format
    for dep in manifest.dependencies:
        if not re.match(r'^[\w.\-]+\s*([<>=!]=?\s*[\d.]+)?$', str(dep)):
            warnings.append(f"Malformed dependency spec '{dep}'")

    return warnings


# ════════════════════════════════════════════════════════════════
#  MigrationPlugin base class
# ════════════════════════════════════════════════════════════════

class MigrationPlugin:
    """Base class for SDK v2 plugins.

    Subclass and override the versioned hooks you need. Declare a ``manifest``
    class attribute (a ``PluginManifest`` or dict) or set ``name``/``version``.

    Versioned hooks (all optional, return value semantics noted):
        * ``on_extract(extracted: dict) -> dict | None`` — mutate/replace
          extracted data after extraction.
        * ``on_convert_dax(name: str, dax: str) -> str | None`` — transform a
          converted DAX formula.
        * ``on_generate_visual(tableau_mark: str, mapped_type: str) ->
          str | None`` — override the mapped PBI visual type.
        * ``on_validate(report: dict) -> list[str] | None`` — contribute extra
          validation issues.

    Backward-compat: the v1 hook names (``post_extraction``, ``transform_dax``,
    ``custom_visual_mapping``) are implemented here as adapters delegating to the
    v2 hooks, so a v2 plugin can be registered with the legacy ``PluginManager``.
    """

    #: Either a PluginManifest, a dict, or None (auto-built from name/version).
    manifest = None
    name = "migration_plugin"
    version = "1.0.0"
    api_version = SDK_API_VERSION

    def __init__(self):
        self._manifest = self._resolve_manifest()
        # keep `name` in sync for legacy PluginManager logging
        self.name = self._manifest.name

    # ── manifest resolution ──

    def _resolve_manifest(self):
        m = type(self).manifest
        if isinstance(m, PluginManifest):
            return m
        if isinstance(m, dict):
            return PluginManifest.from_dict(m)
        # auto-build from class attrs, inferring declared hooks
        declared = [h for h in V2_HOOKS if _overrides(type(self), h)]
        return PluginManifest(
            name=type(self).name,
            version=type(self).version,
            api_version=type(self).api_version,
            hooks=declared,
        )

    def get_manifest(self):
        """Return this plugin's resolved :class:`PluginManifest`."""
        return self._manifest

    # ── v2 versioned hooks (override these) ──

    def on_extract(self, extracted):
        return None

    def on_convert_dax(self, name, dax):
        return None

    def on_generate_visual(self, tableau_mark, mapped_type):
        return None

    def on_validate(self, report):
        return None

    # ── v1 compatibility adapters (do NOT override) ──

    def post_extraction(self, extracted_data):
        return self.on_extract(extracted_data)

    def transform_dax(self, dax_formula):
        result = self.on_convert_dax('', dax_formula)
        return result if result is not None else dax_formula

    def custom_visual_mapping(self, tableau_mark):
        return self.on_generate_visual(tableau_mark, '')


def _overrides(cls, method_name):
    """True if *cls* overrides *method_name* relative to MigrationPlugin."""
    own = getattr(cls, method_name, None)
    base = getattr(MigrationPlugin, method_name, None)
    return own is not None and own is not base


# ════════════════════════════════════════════════════════════════
#  SDK registry (loader with manifest validation)
# ════════════════════════════════════════════════════════════════

class PluginSDK:
    """Registry for v2 plugins with manifest validation and hook dispatch.

    Unlike the legacy ``PluginManager`` (which is duck-typed and unvalidated),
    ``PluginSDK`` validates each plugin's manifest on registration and exposes
    typed dispatch for the four versioned hooks.
    """

    def __init__(self, sdk_version=SDK_API_VERSION, strict=False):
        self._plugins = []
        self.sdk_version = sdk_version
        self.strict = strict
        self.warnings = []

    def register(self, plugin):
        """Validate and register a :class:`MigrationPlugin` instance."""
        if not isinstance(plugin, MigrationPlugin):
            raise PluginValidationError(
                "PluginSDK only accepts MigrationPlugin instances")
        warns = validate_manifest(plugin.get_manifest(), self.sdk_version)
        for w in warns:
            msg = f"[{plugin.name}] {w}"
            self.warnings.append(msg)
            if self.strict:
                raise PluginValidationError(msg)
            logger.warning(msg)
        self._plugins.append(plugin)
        logger.info("SDK plugin registered: %s v%s",
                    plugin.get_manifest().name, plugin.get_manifest().version)
        return plugin

    def check_dependencies(self):
        """Verify each plugin's declared dependencies are satisfied by peers.

        Returns:
            list[str]: unmet dependency messages (empty if all satisfied).
        """
        installed = {p.get_manifest().name: _parse_version(p.get_manifest().version)
                     for p in self._plugins}
        unmet = []
        for p in self._plugins:
            for dep in p.get_manifest().dependencies:
                name, op, ver = _split_dep(dep)
                if name not in installed:
                    unmet.append(f"{p.name} requires '{name}' (not loaded)")
                    continue
                if ver is not None and not _satisfies(installed[name], op, _parse_version(ver)):
                    unmet.append(
                        f"{p.name} requires {name}{op}{ver}, found {installed[name]}")
        return unmet

    # ── dispatch ──

    def dispatch_extract(self, extracted):
        for p in self._plugins:
            try:
                ret = p.on_extract(extracted)
                if ret is not None:
                    extracted = ret
            except Exception as e:  # noqa: BLE001
                logger.error("Plugin '%s' on_extract failed: %s", p.name, e)
        return extracted

    def dispatch_convert_dax(self, name, dax):
        for p in self._plugins:
            try:
                ret = p.on_convert_dax(name, dax)
                if ret is not None:
                    dax = ret
            except Exception as e:  # noqa: BLE001
                logger.error("Plugin '%s' on_convert_dax failed: %s", p.name, e)
        return dax

    def dispatch_generate_visual(self, tableau_mark, mapped_type):
        for p in self._plugins:
            try:
                ret = p.on_generate_visual(tableau_mark, mapped_type)
                if ret:
                    mapped_type = ret
            except Exception as e:  # noqa: BLE001
                logger.error("Plugin '%s' on_generate_visual failed: %s", p.name, e)
        return mapped_type

    def dispatch_validate(self, report):
        issues = []
        for p in self._plugins:
            try:
                ret = p.on_validate(report)
                if ret:
                    issues.extend(ret)
            except Exception as e:  # noqa: BLE001
                logger.error("Plugin '%s' on_validate failed: %s", p.name, e)
        return issues

    @property
    def plugins(self):
        return list(self._plugins)

    def __len__(self):
        return len(self._plugins)


def _split_dep(spec):
    """Split 'name>=1.2.0' → ('name', '>=', '1.2.0'). op/ver may be None."""
    m = re.match(r'^([\w.\-]+)\s*([<>=!]=?)?\s*([\d.]+)?$', str(spec).strip())
    if not m:
        return str(spec), None, None
    return m.group(1), m.group(2), m.group(3)


def _satisfies(have, op, want):
    if op in (None, '=='):
        return have == want
    if op == '>=':
        return have >= want
    if op == '<=':
        return have <= want
    if op == '>':
        return have > want
    if op == '<':
        return have < want
    if op == '!=':
        return have != want
    return True


# ════════════════════════════════════════════════════════════════
#  Plugin testing framework
# ════════════════════════════════════════════════════════════════

class PluginTestError(AssertionError):
    """Raised when a plugin-output assertion fails."""


class PluginTestRunner:
    """Validate plugin output against expected schemas.

    Lightweight, dependency-free assertions a plugin author can use in their
    own test suite or that the SDK can run against plugin output.
    """

    # Minimal set of balanced-delimiter / token checks — not a full parser.
    _DAX_FORBIDDEN = ('==', ' or ', ' and ', 'ELSEIF')

    def assert_dax_valid(self, dax):
        """Assert a DAX string is plausibly valid (balanced, no Tableau-isms)."""
        if not isinstance(dax, str) or not dax.strip():
            raise PluginTestError("DAX is empty")
        if dax.count('(') != dax.count(')'):
            raise PluginTestError(f"Unbalanced parentheses in DAX: {dax!r}")
        if dax.count('[') != dax.count(']'):
            raise PluginTestError(f"Unbalanced brackets in DAX: {dax!r}")
        low = dax
        for tok in self._DAX_FORBIDDEN:
            if tok in low:
                raise PluginTestError(f"Tableau-style token '{tok.strip()}' in DAX: {dax!r}")
        return True

    def assert_m_valid(self, m_query):
        """Assert a Power Query M string is plausibly valid."""
        if not isinstance(m_query, str) or not m_query.strip():
            raise PluginTestError("M query is empty")
        if m_query.count('(') != m_query.count(')'):
            raise PluginTestError("Unbalanced parentheses in M query")
        if m_query.count('[') != m_query.count(']'):
            raise PluginTestError("Unbalanced brackets in M query")
        # quotes must be balanced once doubled-quote escapes are removed
        stripped = m_query.replace('""', '')
        if stripped.count('"') % 2 != 0:
            raise PluginTestError("Unbalanced quotes in M query")
        return True

    def assert_visual_schema(self, visual):
        """Assert a visual descriptor dict has required keys & a known shape."""
        if not isinstance(visual, dict):
            raise PluginTestError("Visual must be a dict")
        if 'visualType' not in visual and 'visual_type' not in visual:
            raise PluginTestError("Visual missing 'visualType'")
        vtype = visual.get('visualType', visual.get('visual_type'))
        if not isinstance(vtype, str) or not vtype:
            raise PluginTestError("Visual 'visualType' must be a non-empty string")
        return True

    def run_plugin(self, plugin, *, extracted=None, dax=None, visual_mark=None):
        """Exercise a plugin's hooks and validate every produced artifact.

        Returns:
            dict: collected outputs keyed by hook.
        """
        out = {}
        if extracted is not None:
            out['on_extract'] = plugin.on_extract(extracted)
        if dax is not None:
            result = plugin.on_convert_dax('test', dax)
            if result is not None:
                self.assert_dax_valid(result)
            out['on_convert_dax'] = result
        if visual_mark is not None:
            result = plugin.on_generate_visual(visual_mark, 'table')
            out['on_generate_visual'] = result
        return out


# ════════════════════════════════════════════════════════════════
#  Bridge to legacy PluginManager
# ════════════════════════════════════════════════════════════════

def register_with_manager(plugin, manager=None):
    """Register a v2 :class:`MigrationPlugin` with the legacy PluginManager.

    Because ``MigrationPlugin`` implements the v1 adapter methods, this lets v2
    plugins participate in the existing migration pipeline unchanged.
    """
    from powerbi_import.plugins import get_plugin_manager
    manager = manager or get_plugin_manager()
    manager.register(plugin)
    return manager
