"""Tests for the Plugin SDK v2 and Marketplace v2 (Sprint 188).

Covers: PluginManifest validation, MigrationPlugin versioned hooks + v1
backward-compat adapters, PluginSDK registration/dispatch/dependency checking,
PluginTestRunner assertions, and Marketplace v2 dependency resolution + remote
catalogue sync (mocked).
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.plugin_sdk import (
    SDK_API_VERSION,
    PluginManifest,
    PluginValidationError,
    validate_manifest,
    MigrationPlugin,
    PluginSDK,
    PluginTestRunner,
    PluginTestError,
    register_with_manager,
    _parse_version,
)
from powerbi_import.marketplace import PatternRegistry
from powerbi_import.plugins import reset_plugin_manager


# ── Manifest ──────────────────────────────────────────────────

class TestPluginManifest(unittest.TestCase):
    def test_from_dict_ok(self):
        m = PluginManifest.from_dict({'name': 'x', 'version': '1.0.0',
                                      'api_version': '2.0.0'})
        self.assertEqual(m.name, 'x')
        self.assertEqual(m.api_version, '2.0.0')

    def test_missing_required_raises(self):
        with self.assertRaises(PluginValidationError):
            PluginManifest.from_dict({'name': 'x'})

    def test_not_dict_raises(self):
        with self.assertRaises(PluginValidationError):
            PluginManifest.from_dict("nope")

    def test_roundtrip(self):
        d = {'name': 'p', 'version': '2.1.0', 'api_version': '2.0.0',
             'author': 'me', 'description': 'd', 'hooks': ['on_extract'],
             'dependencies': ['base>=1.0.0'], 'tags': ['t']}
        m = PluginManifest.from_dict(d)
        out = m.to_dict()
        self.assertEqual(out['hooks'], ['on_extract'])
        self.assertEqual(out['dependencies'], ['base>=1.0.0'])

    def test_repr(self):
        m = PluginManifest('p', '1.0.0')
        self.assertIn('p', repr(m))


class TestValidateManifest(unittest.TestCase):
    def test_compatible_no_warnings(self):
        m = PluginManifest('p', '1.0.0', api_version=SDK_API_VERSION,
                           hooks=['on_extract'])
        self.assertEqual(validate_manifest(m), [])

    def test_major_mismatch_raises(self):
        m = PluginManifest('p', '1.0.0', api_version='1.0.0')
        with self.assertRaises(PluginValidationError):
            validate_manifest(m)

    def test_unknown_hook_warns(self):
        m = PluginManifest('p', '1.0.0', hooks=['bogus_hook'])
        warns = validate_manifest(m)
        self.assertTrue(any('bogus_hook' in w for w in warns))

    def test_newer_api_warns(self):
        m = PluginManifest('p', '1.0.0', api_version='2.9.0')
        warns = validate_manifest(m)
        self.assertTrue(any('newer' in w.lower() for w in warns))

    def test_malformed_dependency_warns(self):
        m = PluginManifest('p', '1.0.0', dependencies=['@@bad@@'])
        warns = validate_manifest(m)
        self.assertTrue(any('dependency' in w.lower() for w in warns))

    def test_accepts_dict(self):
        warns = validate_manifest({'name': 'p', 'version': '1.0.0',
                                   'api_version': '2.0.0'})
        self.assertEqual(warns, [])

    def test_parse_version(self):
        self.assertEqual(_parse_version('2.1'), (2, 1, 0))
        self.assertEqual(_parse_version('bad'), (0, 0, 0))


# ── MigrationPlugin ───────────────────────────────────────────

class _ExtractPlugin(MigrationPlugin):
    name = 'extract_plugin'
    version = '1.0.0'

    def on_extract(self, extracted):
        extracted['touched'] = True
        return extracted


class _DaxPlugin(MigrationPlugin):
    name = 'dax_plugin'
    version = '1.0.0'

    def on_convert_dax(self, name, dax):
        return dax.replace('OLD', 'NEW')


class _VisualPlugin(MigrationPlugin):
    name = 'visual_plugin'
    version = '1.0.0'

    def on_generate_visual(self, tableau_mark, mapped_type):
        if tableau_mark == 'gantt':
            return 'ganttChart'
        return None


class TestMigrationPlugin(unittest.TestCase):
    def test_auto_manifest_detects_hooks(self):
        p = _ExtractPlugin()
        self.assertIn('on_extract', p.get_manifest().hooks)

    def test_name_synced(self):
        self.assertEqual(_DaxPlugin().name, 'dax_plugin')

    def test_v1_adapter_post_extraction(self):
        p = _ExtractPlugin()
        out = p.post_extraction({})
        self.assertTrue(out['touched'])

    def test_v1_adapter_transform_dax(self):
        p = _DaxPlugin()
        self.assertEqual(p.transform_dax('OLD()'), 'NEW()')

    def test_transform_dax_passthrough_when_none(self):
        p = _ExtractPlugin()  # no on_convert_dax override
        self.assertEqual(p.transform_dax('keep'), 'keep')

    def test_v1_adapter_visual_mapping(self):
        p = _VisualPlugin()
        self.assertEqual(p.custom_visual_mapping('gantt'), 'ganttChart')

    def test_explicit_dict_manifest(self):
        class P(MigrationPlugin):
            manifest = {'name': 'mp', 'version': '3.0.0', 'api_version': '2.0.0'}
        self.assertEqual(P().get_manifest().version, '3.0.0')


# ── PluginSDK ─────────────────────────────────────────────────

class TestPluginSDK(unittest.TestCase):
    def test_register_and_len(self):
        sdk = PluginSDK()
        sdk.register(_ExtractPlugin())
        self.assertEqual(len(sdk), 1)

    def test_register_rejects_non_plugin(self):
        sdk = PluginSDK()
        with self.assertRaises(PluginValidationError):
            sdk.register(object())

    def test_dispatch_extract(self):
        sdk = PluginSDK()
        sdk.register(_ExtractPlugin())
        out = sdk.dispatch_extract({})
        self.assertTrue(out['touched'])

    def test_dispatch_convert_dax_chains(self):
        sdk = PluginSDK()
        sdk.register(_DaxPlugin())
        self.assertEqual(sdk.dispatch_convert_dax('m', 'OLD'), 'NEW')

    def test_dispatch_generate_visual(self):
        sdk = PluginSDK()
        sdk.register(_VisualPlugin())
        self.assertEqual(sdk.dispatch_generate_visual('gantt', 'table'), 'ganttChart')
        # unmatched mark keeps mapped_type
        self.assertEqual(sdk.dispatch_generate_visual('bar', 'table'), 'table')

    def test_dispatch_validate_collects(self):
        class V(MigrationPlugin):
            name = 'v'
            def on_validate(self, report):
                return ['issue-1']
        sdk = PluginSDK()
        sdk.register(V())
        self.assertEqual(sdk.dispatch_validate({}), ['issue-1'])

    def test_strict_raises_on_warning(self):
        class Bad(MigrationPlugin):
            manifest = {'name': 'bad', 'version': '1.0.0',
                        'api_version': '2.0.0', 'hooks': ['nope']}
        sdk = PluginSDK(strict=True)
        with self.assertRaises(PluginValidationError):
            sdk.register(Bad())

    def test_dispatch_isolates_plugin_error(self):
        class Boom(MigrationPlugin):
            name = 'boom'
            def on_convert_dax(self, name, dax):
                raise RuntimeError('boom')
        sdk = PluginSDK()
        sdk.register(Boom())
        # error swallowed, original returned
        self.assertEqual(sdk.dispatch_convert_dax('m', 'x'), 'x')

    def test_check_dependencies_unmet(self):
        class Dep(MigrationPlugin):
            manifest = {'name': 'dep', 'version': '1.0.0',
                        'api_version': '2.0.0', 'dependencies': ['missing>=1.0.0']}
        sdk = PluginSDK()
        sdk.register(Dep())
        unmet = sdk.check_dependencies()
        self.assertTrue(any('missing' in u for u in unmet))

    def test_check_dependencies_satisfied(self):
        class Base(MigrationPlugin):
            manifest = {'name': 'base', 'version': '1.5.0', 'api_version': '2.0.0'}
        class Dep(MigrationPlugin):
            manifest = {'name': 'dep', 'version': '1.0.0',
                        'api_version': '2.0.0', 'dependencies': ['base>=1.0.0']}
        sdk = PluginSDK()
        sdk.register(Base())
        sdk.register(Dep())
        self.assertEqual(sdk.check_dependencies(), [])


# ── PluginTestRunner ──────────────────────────────────────────

class TestPluginTestRunner(unittest.TestCase):
    def setUp(self):
        self.r = PluginTestRunner()

    def test_dax_valid(self):
        self.assertTrue(self.r.assert_dax_valid('SUM([Sales])'))

    def test_dax_unbalanced(self):
        with self.assertRaises(PluginTestError):
            self.r.assert_dax_valid('SUM([Sales]')

    def test_dax_tableau_token(self):
        with self.assertRaises(PluginTestError):
            self.r.assert_dax_valid('IF(a == b, 1, 0)')

    def test_dax_empty(self):
        with self.assertRaises(PluginTestError):
            self.r.assert_dax_valid('')

    def test_m_valid(self):
        self.assertTrue(self.r.assert_m_valid('Table.SelectColumns(Source, {"A"})'))

    def test_m_unbalanced_quotes(self):
        with self.assertRaises(PluginTestError):
            self.r.assert_m_valid('Text.From("abc)')

    def test_m_escaped_quotes_ok(self):
        self.assertTrue(self.r.assert_m_valid('Text.From("a""b")'))

    def test_visual_schema_ok(self):
        self.assertTrue(self.r.assert_visual_schema({'visualType': 'barChart'}))

    def test_visual_schema_missing(self):
        with self.assertRaises(PluginTestError):
            self.r.assert_visual_schema({})

    def test_run_plugin_validates_dax(self):
        out = self.r.run_plugin(_DaxPlugin(), dax='OLD()')
        self.assertEqual(out['on_convert_dax'], 'NEW()')


# ── Bridge to legacy manager ──────────────────────────────────

class TestManagerBridge(unittest.TestCase):
    def test_register_with_manager(self):
        reset_plugin_manager()
        mgr = register_with_manager(_DaxPlugin())
        self.assertTrue(mgr.has_plugins())
        # legacy apply_transform works through the adapter
        self.assertEqual(mgr.apply_transform('transform_dax', 'OLD()'), 'NEW()')
        reset_plugin_manager()


# ── Marketplace v2 ────────────────────────────────────────────

class TestMarketplaceV2(unittest.TestCase):
    def _packs_registry(self):
        r = PatternRegistry()
        base = os.path.join(os.path.dirname(__file__), '..',
                            'examples', 'marketplace', 'packs')
        for industry in ('healthcare', 'finance', 'retail'):
            r.load(os.path.join(base, industry))
        return r

    def test_industry_packs_load(self):
        r = self._packs_registry()
        self.assertGreaterEqual(r.count, 6)

    def test_dependencies_parsed(self):
        r = self._packs_registry()
        deps = r.get_dependencies('healthcare_readmission_rate')
        self.assertEqual(deps, ['healthcare_core_model>=1.0.0'])

    def test_resolve_dependencies_order(self):
        r = self._packs_registry()
        res = r.resolve_dependencies('finance_operating_margin')
        self.assertEqual(res['missing'], [])
        self.assertEqual(res['conflicts'], [])
        self.assertLess(res['order'].index('finance_core_model'),
                        res['order'].index('finance_operating_margin'))

    def test_resolve_missing_dependency(self):
        r = PatternRegistry()
        r.register({'metadata': {'name': 'lonely', 'version': '1.0.0',
                                 'category': 'dax_recipe',
                                 'dependencies': ['ghost>=2.0.0']},
                    'payload': {}})
        res = r.resolve_dependencies('lonely')
        self.assertTrue(any('ghost' in m for m in res['missing']))

    def test_resolve_version_conflict(self):
        r = PatternRegistry()
        r.register({'metadata': {'name': 'base', 'version': '1.0.0',
                                 'category': 'model_template'}, 'payload': {}})
        r.register({'metadata': {'name': 'child', 'version': '1.0.0',
                                 'category': 'dax_recipe',
                                 'dependencies': ['base>=2.0.0']}, 'payload': {}})
        res = r.resolve_dependencies('child')
        self.assertTrue(res['conflicts'])

    def test_sync_remote_with_opener(self):
        catalogue = [
            {'metadata': {'name': 'remote_kpi', 'version': '1.0.0',
                          'category': 'dax_recipe', 'tags': ['remote']},
             'payload': {'inject': {'name': 'X', 'dax': 'SUM([Y])'}}},
        ]
        payload = json.dumps(catalogue).encode('utf-8')
        r = PatternRegistry()
        n = r.sync_remote('https://example.com/catalogue.json',
                          opener=lambda url, timeout: payload)
        self.assertEqual(n, 1)
        self.assertIsNotNone(r.get('remote_kpi'))

    def test_sync_remote_caches_to_dir(self):
        catalogue = [{'metadata': {'name': 'cached', 'version': '2.0.0',
                                   'category': 'dax_recipe'}, 'payload': {}}]
        payload = json.dumps(catalogue).encode('utf-8')
        with tempfile.TemporaryDirectory() as d:
            r = PatternRegistry()
            r.sync_remote('https://example.com/c.json', dest_dir=d,
                          opener=lambda url, timeout: payload)
            files = os.listdir(d)
            self.assertTrue(any('cached' in f for f in files))

    def test_sync_remote_rejects_bad_scheme(self):
        r = PatternRegistry()
        with self.assertRaises(ValueError):
            r.sync_remote('ftp://example.com/c.json',
                          opener=lambda url, timeout: b'[]')

    def test_sync_remote_dict_with_patterns_key(self):
        body = json.dumps({'patterns': [
            {'metadata': {'name': 'wrapped', 'version': '1.0.0',
                          'category': 'dax_recipe'}, 'payload': {}}]}).encode()
        r = PatternRegistry()
        n = r.sync_remote('https://example.com/c.json',
                          opener=lambda url, timeout: body)
        self.assertEqual(n, 1)


if __name__ == '__main__':
    unittest.main()
