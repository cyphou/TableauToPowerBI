"""
Tests for Sprint 133 — Multi-Tenant & Connection Hardening.

Covers:
- CredentialVault with env, json, keyvault backends
- Pre-deploy validation gate
- Connection drift detection
- Security: null bytes, path traversal, command injection, control chars
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'powerbi_import'))
sys.path.insert(0, os.path.join(ROOT, 'powerbi_import', 'deploy'))

from powerbi_import.deploy.credential_vault import (
    CredentialVault,
    EnvVarBackend,
    JsonFileBackend,
    AzureKeyVaultBackend,
    _validate_tenant_name,
    _validate_key,
    _validate_value,
)
from powerbi_import.deploy.multi_tenant import (
    TenantConfig,
    MultiTenantConfig,
    _pre_deploy_validate,
    deploy_multi_tenant,
    _apply_connection_overrides,
)
from powerbi_import.schema_drift import (
    detect_connection_drift,
    _extract_connections,
    SchemaDriftEntry,
)


# ── Validation Functions ─────────────────────────────────────────────────────

class TestValidationFunctions(unittest.TestCase):

    def test_valid_tenant_name(self):
        _validate_tenant_name('Contoso')
        _validate_tenant_name('tenant-1')
        _validate_tenant_name('My_Tenant_2')

    def test_empty_tenant_name(self):
        with self.assertRaises(ValueError):
            _validate_tenant_name('')

    def test_null_byte_tenant_name(self):
        with self.assertRaises(ValueError):
            _validate_tenant_name('Contoso\x00evil')

    def test_special_chars_tenant_name(self):
        with self.assertRaises(ValueError):
            _validate_tenant_name('Contoso/../../../etc/passwd')

    def test_command_injection_tenant_name(self):
        with self.assertRaises(ValueError):
            _validate_tenant_name('Contoso; rm -rf /')

    def test_valid_key(self):
        _validate_key('TENANT_SERVER')
        _validate_key('DATABASE')
        _validate_key('A1')

    def test_empty_key(self):
        with self.assertRaises(ValueError):
            _validate_key('')

    def test_null_byte_key(self):
        with self.assertRaises(ValueError):
            _validate_key('TENANT\x00SERVER')

    def test_lowercase_key(self):
        with self.assertRaises(ValueError):
            _validate_key('tenant_server')

    def test_key_starting_with_number(self):
        with self.assertRaises(ValueError):
            _validate_key('1BAD')

    def test_valid_value(self):
        _validate_value('contoso-sql.database.windows.net')
        _validate_value('some value with spaces')

    def test_null_byte_value(self):
        with self.assertRaises(ValueError):
            _validate_value('value\x00injected')

    def test_control_char_value(self):
        with self.assertRaises(ValueError):
            _validate_value('value\x01control')

    def test_tab_and_newline_allowed(self):
        _validate_value('value\twith\ttabs')
        _validate_value('value\nwith\nnewlines')

    def test_value_too_long(self):
        with self.assertRaises(ValueError):
            _validate_value('x' * 5000)


# ── Environment Variable Backend ─────────────────────────────────────────────

class TestEnvVarBackend(unittest.TestCase):

    def setUp(self):
        self.backend = EnvVarBackend()
        self._saved = {}

    def _set_env(self, key, value):
        self._saved[key] = os.environ.get(key)
        os.environ[key] = value

    def tearDown(self):
        for key, orig in self._saved.items():
            if orig is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = orig

    def test_get_existing(self):
        self._set_env('TENANT_CONTOSO_SERVER', 'contoso.database.windows.net')
        self.assertEqual(
            self.backend.get('Contoso', 'SERVER'),
            'contoso.database.windows.net',
        )

    def test_get_missing(self):
        self.assertIsNone(self.backend.get('Nonexistent', 'SERVER'))

    def test_has_existing(self):
        self._set_env('TENANT_CONTOSO_DATABASE', 'mydb')
        self.assertTrue(self.backend.has('Contoso', 'DATABASE'))

    def test_has_missing(self):
        self.assertFalse(self.backend.has('Nonexistent', 'DATABASE'))

    def test_list_keys(self):
        self._set_env('TENANT_ALPHA_SERVER', 'srv1')
        self._set_env('TENANT_ALPHA_DATABASE', 'db1')
        keys = self.backend.list_keys('Alpha')
        self.assertIn('SERVER', keys)
        self.assertIn('DATABASE', keys)

    def test_hyphen_tenant_name(self):
        self._set_env('TENANT_MY_TENANT_SERVER', 'srv')
        val = self.backend.get('my-tenant', 'SERVER')
        self.assertEqual(val, 'srv')


# ── JSON File Backend ────────────────────────────────────────────────────────

class TestJsonFileBackend(unittest.TestCase):

    def _write_creds(self, data):
        f = tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False, encoding='utf-8'
        )
        json.dump(data, f, indent=2)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_load_valid(self):
        path = self._write_creds({
            'tenants': {
                'Contoso': {
                    'TENANT_SERVER': 'contoso-sql.database.windows.net',
                    'TENANT_DATABASE': 'sales',
                }
            }
        })
        backend = JsonFileBackend(path, production=False)
        self.assertEqual(
            backend.get('Contoso', 'TENANT_SERVER'),
            'contoso-sql.database.windows.net',
        )

    def test_production_blocked(self):
        path = self._write_creds({'tenants': {}})
        with self.assertRaises(ValueError, msg='not allowed in production'):
            JsonFileBackend(path, production=True)

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            JsonFileBackend('/nonexistent/creds.json', production=False)

    def test_invalid_structure(self):
        path = self._write_creds({'not_tenants': {}})
        with self.assertRaises(ValueError):
            JsonFileBackend(path, production=False)

    def test_list_keys(self):
        path = self._write_creds({
            'tenants': {
                'Test': {'TENANT_SERVER': 'srv', 'TENANT_DATABASE': 'db'}
            }
        })
        backend = JsonFileBackend(path, production=False)
        keys = backend.list_keys('Test')
        self.assertIn('TENANT_SERVER', keys)
        self.assertIn('TENANT_DATABASE', keys)

    def test_has(self):
        path = self._write_creds({
            'tenants': {'T1': {'TENANT_SERVER': 'srv'}}
        })
        backend = JsonFileBackend(path, production=False)
        self.assertTrue(backend.has('T1', 'TENANT_SERVER'))
        self.assertFalse(backend.has('T1', 'TENANT_DATABASE'))

    def test_null_byte_in_value(self):
        path = self._write_creds({
            'tenants': {'Bad': {'TENANT_SERVER': 'srv\x00evil'}}
        })
        with self.assertRaises(ValueError):
            JsonFileBackend(path, production=False)

    def test_non_string_value(self):
        path = self._write_creds({
            'tenants': {'Bad': {'TENANT_SERVER': 123}}
        })
        with self.assertRaises(ValueError):
            JsonFileBackend(path, production=False)


# ── Credential Vault ─────────────────────────────────────────────────────────

class TestCredentialVault(unittest.TestCase):

    def setUp(self):
        self._saved = {}

    def _set_env(self, key, value):
        self._saved[key] = os.environ.get(key)
        os.environ[key] = value

    def tearDown(self):
        for key, orig in self._saved.items():
            if orig is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = orig

    def test_from_config_env(self):
        vault = CredentialVault.from_config({'backend': 'env'})
        self.assertIsInstance(vault.backend, EnvVarBackend)

    def test_from_config_json(self):
        f = tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False, encoding='utf-8'
        )
        json.dump({'tenants': {}}, f)
        f.close()
        self.addCleanup(os.unlink, f.name)

        vault = CredentialVault.from_config({
            'backend': 'json',
            'credentials_file': f.name,
        })
        self.assertIsInstance(vault.backend, JsonFileBackend)

    def test_from_config_unknown(self):
        with self.assertRaises(ValueError):
            CredentialVault.from_config({'backend': 'redis'})

    def test_from_config_json_missing_file(self):
        with self.assertRaises(ValueError):
            CredentialVault.from_config({'backend': 'json'})

    def test_resolve_overrides_explicit(self):
        vault = CredentialVault.from_config({'backend': 'env'})
        resolved = vault.resolve_overrides('Test', {
            '${TENANT_SERVER}': 'explicit-server',
        })
        self.assertEqual(resolved['${TENANT_SERVER}'], 'explicit-server')

    def test_resolve_overrides_vault_lookup(self):
        self._set_env('TENANT_CONTOSO_TENANT_SERVER', 'vault-server')
        vault = CredentialVault.from_config({'backend': 'env'})
        resolved = vault.resolve_overrides('Contoso', {
            '${TENANT_SERVER}': '${VAULT}',
        })
        self.assertEqual(resolved['${TENANT_SERVER}'], 'vault-server')

    def test_resolve_overrides_missing_credential(self):
        vault = CredentialVault.from_config({'backend': 'env'})
        with self.assertRaises(ValueError, msg='Credential not found'):
            vault.resolve_overrides('Missing', {
                '${TENANT_SERVER}': '${VAULT}',
            })

    def test_validate_tenant_credentials(self):
        self._set_env('TENANT_T1_SERVER', 'srv')
        vault = CredentialVault.from_config({'backend': 'env'})
        errors = vault.validate_tenant_credentials('T1', ['SERVER', 'DATABASE'])
        self.assertEqual(len(errors), 1)
        self.assertIn('DATABASE', errors[0])

    def test_validate_all_tenants(self):
        self._set_env('TENANT_A_SERVER', 'srv')
        vault = CredentialVault.from_config({'backend': 'env'})
        results = vault.validate_all_tenants([
            {'name': 'A', 'connection_overrides': {
                '${SERVER}': '${VAULT}',
                '${DATABASE}': '${VAULT}',
            }},
        ])
        self.assertIn('A', results)
        self.assertEqual(len(results['A']), 1)  # DATABASE missing


# ── Azure Key Vault Backend ──────────────────────────────────────────────────

class TestAzureKeyVaultBackend(unittest.TestCase):

    def test_missing_vault_url(self):
        saved = os.environ.pop('AZURE_KEYVAULT_URL', None)
        try:
            with self.assertRaises(ValueError):
                AzureKeyVaultBackend()
        finally:
            if saved:
                os.environ['AZURE_KEYVAULT_URL'] = saved

    def test_secret_name_format(self):
        saved = os.environ.get('AZURE_KEYVAULT_URL')
        os.environ['AZURE_KEYVAULT_URL'] = 'https://test.vault.azure.net'
        try:
            backend = AzureKeyVaultBackend()
            name = backend._secret_name('Contoso', 'TENANT_SERVER')
            self.assertEqual(name, 'tenant-contoso--tenant-server')
        finally:
            if saved:
                os.environ['AZURE_KEYVAULT_URL'] = saved
            else:
                os.environ.pop('AZURE_KEYVAULT_URL', None)


# ── Pre-Deploy Validation ────────────────────────────────────────────────────

class TestPreDeployValidation(unittest.TestCase):

    def setUp(self):
        self.model_dir = tempfile.mkdtemp()
        # Create a dummy model file
        with open(os.path.join(self.model_dir, 'model.tmdl'), 'w') as f:
            f.write('model Model\n')
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.model_dir, ignore_errors=True)

    def test_valid_tenant_passes(self):
        tenant = TenantConfig(
            name='Contoso',
            workspace_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            connection_overrides={'${TENANT_SERVER}': 'srv.database.windows.net'},
        )
        errors = _pre_deploy_validate(tenant, self.model_dir)
        self.assertEqual(errors, [])

    def test_missing_model_dir(self):
        tenant = TenantConfig(
            name='T1',
            workspace_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        )
        errors = _pre_deploy_validate(tenant, '/nonexistent/path')
        self.assertTrue(any('does not exist' in e for e in errors))

    def test_empty_model_dir(self):
        empty_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(empty_dir, True))
        tenant = TenantConfig(
            name='T1',
            workspace_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        )
        errors = _pre_deploy_validate(tenant, empty_dir)
        self.assertTrue(any('empty' in e for e in errors))

    def test_invalid_placeholder(self):
        tenant = TenantConfig(
            name='T1',
            workspace_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            connection_overrides={'bad_placeholder': 'value'},
        )
        errors = _pre_deploy_validate(tenant, self.model_dir)
        self.assertTrue(any('Invalid placeholder' in e for e in errors))

    def test_vault_required_but_missing(self):
        tenant = TenantConfig(
            name='T1',
            workspace_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            connection_overrides={'${TENANT_SERVER}': '${VAULT}'},
        )
        errors = _pre_deploy_validate(tenant, self.model_dir, credential_vault=None)
        self.assertTrue(any('vault' in e.lower() for e in errors))

    def test_null_byte_in_override_value(self):
        tenant = TenantConfig(
            name='T1',
            workspace_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            connection_overrides={'${TENANT_SERVER}': 'srv\x00evil'},
        )
        errors = _pre_deploy_validate(tenant, self.model_dir)
        self.assertTrue(any('null bytes' in e for e in errors))

    def test_control_chars_in_override_value(self):
        tenant = TenantConfig(
            name='T1',
            workspace_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            connection_overrides={'${TENANT_SERVER}': 'srv\x01ctrl'},
        )
        errors = _pre_deploy_validate(tenant, self.model_dir)
        self.assertTrue(any('control' in e for e in errors))

    def test_nested_placeholders_rejected(self):
        tenant = TenantConfig(
            name='T1',
            workspace_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            connection_overrides={
                '${TENANT_SERVER}': '${TENANT_OTHER_THING}'
            },
        )
        errors = _pre_deploy_validate(tenant, self.model_dir)
        self.assertTrue(any('unresolved' in e for e in errors))

    def test_dry_run_mode(self):
        config = MultiTenantConfig(tenants=[
            TenantConfig(
                name='DryRun',
                workspace_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                connection_overrides={'${TENANT_SERVER}': 'srv'},
            ),
        ])
        result = deploy_multi_tenant(
            self.model_dir, config, dry_run=True,
        )
        self.assertEqual(result.total_count, 1)
        self.assertTrue(result.results[0].success)


# ── Connection String Override Security ──────────────────────────────────────

class TestConnectionOverrideSecurity(unittest.TestCase):

    def setUp(self):
        self.src_dir = tempfile.mkdtemp()
        self.dst_dir = tempfile.mkdtemp()
        # Write a template TMDL file
        with open(os.path.join(self.src_dir, 'model.tmdl'), 'w') as f:
            f.write("server = '${TENANT_SERVER}'\ndatabase = '${TENANT_DATABASE}'\n")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(self.src_dir, ignore_errors=True)
        shutil.rmtree(self.dst_dir, ignore_errors=True)

    def test_normal_substitution(self):
        _apply_connection_overrides(self.src_dir, {
            '${TENANT_SERVER}': 'my-server.database.windows.net',
            '${TENANT_DATABASE}': 'mydb',
        }, self.dst_dir)
        with open(os.path.join(self.dst_dir, 'model.tmdl')) as f:
            content = f.read()
        self.assertIn('my-server.database.windows.net', content)
        self.assertIn('mydb', content)

    def test_null_byte_value_blocked(self):
        with self.assertRaises(ValueError):
            _apply_connection_overrides(self.src_dir, {
                '${TENANT_SERVER}': 'srv\x00evil',
            }, self.dst_dir)

    def test_invalid_placeholder_skipped(self):
        _apply_connection_overrides(self.src_dir, {
            'not_a_placeholder': 'value',
        }, self.dst_dir)
        with open(os.path.join(self.dst_dir, 'model.tmdl')) as f:
            content = f.read()
        self.assertIn('${TENANT_SERVER}', content)  # not replaced

    def test_tmdl_apostrophe_escaping(self):
        _apply_connection_overrides(self.src_dir, {
            "${TENANT_SERVER}": "O'Brien's server",
            "${TENANT_DATABASE}": 'db',
        }, self.dst_dir)
        with open(os.path.join(self.dst_dir, 'model.tmdl')) as f:
            content = f.read()
        self.assertIn("O''Brien''s server", content)


# ── Connection Drift Detection ───────────────────────────────────────────────

class TestConnectionDriftDetection(unittest.TestCase):

    def test_no_drift(self):
        ds = [{'name': 'DS1', 'connection_map': {
            'type': 'sqlserver', 'server': 'srv1', 'dbname': 'db1'
        }}]
        report = detect_connection_drift(ds, ds)
        self.assertFalse(report.has_drift)

    def test_server_changed(self):
        prev = [{'name': 'DS1', 'connection_map': {
            'type': 'sqlserver', 'server': 'old-srv', 'dbname': 'db1'
        }}]
        curr = [{'name': 'DS1', 'connection_map': {
            'type': 'sqlserver', 'server': 'new-srv', 'dbname': 'db1'
        }}]
        report = detect_connection_drift(curr, prev)
        self.assertTrue(report.has_drift)
        self.assertEqual(len(report.entries), 1)
        self.assertIn('server changed', report.entries[0].detail)

    def test_database_changed(self):
        prev = [{'name': 'DS1', 'connection_map': {
            'type': 'sqlserver', 'server': 'srv', 'dbname': 'old_db'
        }}]
        curr = [{'name': 'DS1', 'connection_map': {
            'type': 'sqlserver', 'server': 'srv', 'dbname': 'new_db'
        }}]
        report = detect_connection_drift(curr, prev)
        self.assertTrue(report.has_drift)
        self.assertIn('database changed', report.entries[0].detail)

    def test_connection_added(self):
        prev = []
        curr = [{'name': 'NewDS', 'connection_map': {
            'type': 'postgres', 'server': 'pg-srv'
        }}]
        report = detect_connection_drift(curr, prev)
        self.assertTrue(report.has_drift)
        self.assertEqual(report.entries[0].change_type, 'added')

    def test_connection_removed(self):
        prev = [{'name': 'OldDS', 'connection_map': {
            'type': 'sqlserver', 'server': 'old-srv'
        }}]
        curr = []
        report = detect_connection_drift(curr, prev)
        self.assertTrue(report.has_drift)
        self.assertEqual(report.entries[0].change_type, 'removed')

    def test_deployed_drift(self):
        curr = [{'name': 'DS1', 'connection_map': {
            'type': 'sqlserver', 'server': 'source-srv', 'dbname': 'source_db'
        }}]
        deployed = {
            'DS1': {'server': 'deployed-srv', 'database': 'deployed_db'},
        }
        report = detect_connection_drift(curr, curr, deployed_connections=deployed)
        self.assertTrue(report.has_drift)
        drift_entries = [e for e in report.entries if '(deployed)' in e.name]
        self.assertTrue(len(drift_entries) >= 1)

    def test_type_changed(self):
        prev = [{'name': 'DS1', 'connection_map': {
            'type': 'sqlserver', 'server': 'srv', 'dbname': 'db'
        }}]
        curr = [{'name': 'DS1', 'connection_map': {
            'type': 'postgres', 'server': 'srv', 'dbname': 'db'
        }}]
        report = detect_connection_drift(curr, prev)
        self.assertTrue(report.has_drift)
        self.assertIn('type changed', report.entries[0].detail)

    def test_extract_connections_fallback(self):
        ds = [{'name': 'FallbackDS', 'connection_type': 'oracle',
               'server': 'orasrv', 'database': 'oradb'}]
        conns = _extract_connections(ds)
        self.assertIn('FallbackDS', conns)
        self.assertEqual(conns['FallbackDS']['type'], 'oracle')

    def test_extract_connections_empty_values(self):
        ds = [{'name': 'EmptyDS', 'connection_map': {
            'type': '', 'server': '', 'dbname': ''
        }}]
        conns = _extract_connections(ds)
        # Empty values should be filtered out
        self.assertEqual(conns.get('EmptyDS', {}), {})

    def test_multiple_fields_changed(self):
        prev = [{'name': 'DS1', 'connection_map': {
            'type': 'sqlserver', 'server': 'old-srv', 'dbname': 'old_db',
            'port': '1433'
        }}]
        curr = [{'name': 'DS1', 'connection_map': {
            'type': 'sqlserver', 'server': 'new-srv', 'dbname': 'new_db',
            'port': '5432'
        }}]
        report = detect_connection_drift(curr, prev)
        self.assertTrue(report.has_drift)
        self.assertTrue(len(report.entries) >= 3)

    def test_schema_drift_summary_includes_connection(self):
        prev = [{'name': 'DS1', 'connection_map': {
            'type': 'sqlserver', 'server': 'old'
        }}]
        curr = [{'name': 'DS1', 'connection_map': {
            'type': 'sqlserver', 'server': 'new'
        }}]
        report = detect_connection_drift(curr, prev)
        summary = report.summary()
        self.assertIn('connection', summary)


# ── Multi-Tenant Config ──────────────────────────────────────────────────────

class TestMultiTenantConfigValidation(unittest.TestCase):

    def test_valid_config(self):
        config = MultiTenantConfig(tenants=[
            TenantConfig(
                name='T1',
                workspace_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            ),
        ])
        self.assertEqual(config.validate(), [])

    def test_empty_tenants(self):
        config = MultiTenantConfig(tenants=[])
        errors = config.validate()
        self.assertTrue(any('No tenants' in e for e in errors))

    def test_duplicate_names(self):
        config = MultiTenantConfig(tenants=[
            TenantConfig(name='T1', workspace_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'),
            TenantConfig(name='T1', workspace_id='bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee'),
        ])
        errors = config.validate()
        self.assertTrue(any('Duplicate tenant name' in e for e in errors))

    def test_duplicate_workspace_ids(self):
        config = MultiTenantConfig(tenants=[
            TenantConfig(name='T1', workspace_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'),
            TenantConfig(name='T2', workspace_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'),
        ])
        errors = config.validate()
        self.assertTrue(any('Duplicate workspace_id' in e for e in errors))

    def test_invalid_guid(self):
        config = MultiTenantConfig(tenants=[
            TenantConfig(name='T1', workspace_id='not-a-guid'),
        ])
        errors = config.validate()
        self.assertTrue(any('not a valid GUID' in e for e in errors))


# ── Config Load/Save ─────────────────────────────────────────────────────────

class TestMultiTenantConfigIO(unittest.TestCase):

    def test_load_save_roundtrip(self):
        tmp = tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False, encoding='utf-8'
        )
        data = {
            'tenants': [{
                'name': 'RT',
                'workspace_id': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                'connection_overrides': {'${TENANT_SERVER}': 'srv'},
                'rls_mappings': {'Admin': ['user@test.com']},
            }]
        }
        json.dump(data, tmp, indent=2)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)

        config = MultiTenantConfig.load(tmp.name)
        self.assertEqual(len(config.tenants), 1)
        self.assertEqual(config.tenants[0].name, 'RT')

        out_path = tmp.name + '.out'
        self.addCleanup(lambda: os.unlink(out_path) if os.path.exists(out_path) else None)
        config.save(out_path)

        config2 = MultiTenantConfig.load(out_path)
        self.assertEqual(config2.tenants[0].name, 'RT')

    def test_load_oversized_file(self):
        tmp = tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False, encoding='utf-8'
        )
        tmp.write(' ' * 2_000_000)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        with self.assertRaises(ValueError, msg='1 MB'):
            MultiTenantConfig.load(tmp.name)


if __name__ == '__main__':
    unittest.main()
