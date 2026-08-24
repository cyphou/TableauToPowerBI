"""
Tests for gateway_config Sprint 183 — OAuth & Authentication Flow Migration.

Covers:
  - Tableau auth-mode → Power BI credential type mapping
  - Credential template v2 generation (per-datasource cred blocks)
  - Service principal config generation
  - Connection test script (PowerShell) generation
  - write_auth_artifacts file I/O
  - New OAuth connectors (dynamics365, dataverse, servicenow, fabric_lakehouse...)
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.gateway_config import (
    GatewayConfigGenerator,
    OAUTH_CONNECTORS,
    SERVICE_PRINCIPAL_CAPABLE,
    TABLEAU_AUTH_MAP,
)


def _ds(name, conn_type, **kw):
    d = {'name': name, 'connection_type': conn_type}
    d.update(kw)
    return d


class TestAuthMapping(unittest.TestCase):
    def setUp(self):
        self.gen = GatewayConfigGenerator()

    def test_basic_from_username_password(self):
        self.assertEqual(self.gen.map_tableau_auth('username-password'), 'Basic')

    def test_windows_from_sspi(self):
        self.assertEqual(self.gen.map_tableau_auth('sspi'), 'Windows')

    def test_oauth(self):
        self.assertEqual(self.gen.map_tableau_auth('oauth'), 'OAuth2')

    def test_key_from_pat(self):
        self.assertEqual(self.gen.map_tableau_auth('personal-access-token'), 'Key')

    def test_service_principal(self):
        self.assertEqual(self.gen.map_tableau_auth('service-account'), 'ServicePrincipal')

    def test_anonymous_from_empty(self):
        self.assertEqual(self.gen.map_tableau_auth(''), 'Anonymous')

    def test_unknown_defaults_basic(self):
        self.assertEqual(self.gen.map_tableau_auth('mystery-mode'), 'Basic')

    def test_none_input(self):
        self.assertEqual(self.gen.map_tableau_auth(None), 'Anonymous')

    def test_case_insensitive(self):
        self.assertEqual(self.gen.map_tableau_auth('OAuth2'), 'OAuth2')


class TestNewOAuthConnectors(unittest.TestCase):
    def test_dynamics365_present(self):
        self.assertIn('dynamics365', OAUTH_CONNECTORS)
        self.assertEqual(OAUTH_CONNECTORS['dynamics365']['provider'], 'AzureAD')

    def test_dataverse_present(self):
        self.assertIn('dataverse', OAUTH_CONNECTORS)

    def test_servicenow_present(self):
        self.assertIn('servicenow', OAUTH_CONNECTORS)
        self.assertEqual(OAUTH_CONNECTORS['servicenow']['provider'], 'ServiceNow')

    def test_fabric_lakehouse_present(self):
        self.assertIn('fabric_lakehouse', OAUTH_CONNECTORS)

    def test_azure_blob_and_data_lake(self):
        self.assertIn('azure_blob', OAUTH_CONNECTORS)
        self.assertIn('azure_data_lake', OAUTH_CONNECTORS)

    def test_sp_capable_set(self):
        self.assertIn('azure_sql', SERVICE_PRINCIPAL_CAPABLE)
        self.assertIn('snowflake', SERVICE_PRINCIPAL_CAPABLE)
        self.assertIn('fabric_lakehouse', SERVICE_PRINCIPAL_CAPABLE)


class TestCredentialTemplateV2(unittest.TestCase):
    def setUp(self):
        self.gen = GatewayConfigGenerator()

    def test_version_field(self):
        tpl = self.gen.generate_credential_template_v2([])
        self.assertEqual(tpl['version'], 2)
        self.assertEqual(tpl['datasources'], {})

    def test_basic_auth_datasource(self):
        tpl = self.gen.generate_credential_template_v2([
            _ds('SQL Prod', 'sqlserver', authentication='username-password'),
        ])
        entry = tpl['datasources']['SQL Prod']
        self.assertEqual(entry['powerbi_credential_type'], 'Basic')
        self.assertEqual(entry['username'], '${USERNAME}')
        self.assertEqual(entry['password'], '${PASSWORD}')
        self.assertTrue(entry['requires_gateway'])

    def test_oauth_connector_forces_oauth2(self):
        tpl = self.gen.generate_credential_template_v2([
            _ds('BQ', 'bigquery', authentication='username-password'),
        ])
        entry = tpl['datasources']['BQ']
        self.assertEqual(entry['powerbi_credential_type'], 'OAuth2')
        self.assertIn('oauth', entry)
        self.assertEqual(entry['oauth']['client_id'], '${CLIENT_ID}')

    def test_service_principal_entry(self):
        tpl = self.gen.generate_credential_template_v2([
            _ds('Synapse', 'azure_synapse', authentication='service-account'),
        ])
        entry = tpl['datasources']['Synapse']
        self.assertEqual(entry['powerbi_credential_type'], 'ServicePrincipal')
        self.assertIn('service_principal', entry)
        self.assertTrue(entry['supports_service_principal'])

    def test_key_auth_entry(self):
        tpl = self.gen.generate_credential_template_v2([
            _ds('DBX', 'databricks', authentication='pat'),
        ])
        entry = tpl['datasources']['DBX']
        self.assertEqual(entry['powerbi_credential_type'], 'Key')
        self.assertEqual(entry['key'], '${ACCESS_TOKEN}')

    def test_no_secrets_leaked(self):
        tpl = self.gen.generate_credential_template_v2([
            _ds('SQL', 'sqlserver', authentication='username-password',
                password='hunter2', username='admin'),
        ])
        blob = json.dumps(tpl)
        self.assertNotIn('hunter2', blob)
        self.assertIn('${PASSWORD}', blob)


class TestServicePrincipalConfig(unittest.TestCase):
    def setUp(self):
        self.gen = GatewayConfigGenerator()

    def test_structure(self):
        cfg = self.gen.generate_service_principal_config([])
        self.assertIn('service_principal', cfg)
        self.assertEqual(cfg['service_principal']['tenant_id'], '${TENANT_ID}')
        self.assertIn('api_permissions', cfg)

    def test_lists_sp_capable_connectors(self):
        cfg = self.gen.generate_service_principal_config([
            _ds('Synapse', 'azure_synapse'),
            _ds('Excel', 'excel'),
        ])
        names = [c['datasource'] for c in cfg['applicable_datasources']]
        self.assertIn('Synapse', names)
        self.assertNotIn('Excel', names)

    def test_no_secrets(self):
        cfg = self.gen.generate_service_principal_config([
            _ds('Synapse', 'azure_synapse'),
        ])
        self.assertEqual(cfg['service_principal']['client_secret'], '${SP_CLIENT_SECRET}')


class TestConnectionTestScript(unittest.TestCase):
    def setUp(self):
        self.gen = GatewayConfigGenerator()

    def test_script_header(self):
        cfg = self.gen.generate_gateway_config([_ds('SQL', 'sqlserver')])
        script = self.gen.generate_connection_test_script(cfg)
        self.assertIn('MicrosoftPowerBIMgmt', script)
        self.assertIn('Connect-PowerBIServiceAccount', script)

    def test_includes_connection_names(self):
        cfg = self.gen.generate_gateway_config([
            _ds('My SQL Server', 'sqlserver'),
        ])
        script = self.gen.generate_connection_test_script(cfg)
        self.assertIn('My SQL Server', script)

    def test_empty_connections(self):
        script = self.gen.generate_connection_test_script({'connections': []})
        self.assertIn('Connect-PowerBIServiceAccount', script)
        self.assertTrue(script.endswith('\n'))

    def test_single_quote_escaping(self):
        cfg = self.gen.generate_gateway_config([_ds("O'Brien DB", 'sqlserver')])
        script = self.gen.generate_connection_test_script(cfg)
        self.assertIn("O''Brien DB", script)

    def test_gateway_flag_rendered(self):
        cfg = self.gen.generate_gateway_config([_ds('PG', 'postgresql')])
        script = self.gen.generate_connection_test_script(cfg)
        self.assertIn('$true', script)


class TestWriteAuthArtifacts(unittest.TestCase):
    def setUp(self):
        self.gen = GatewayConfigGenerator()

    def test_writes_three_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.gen.write_auth_artifacts(tmp, [
                _ds('Synapse', 'azure_synapse', authentication='service-account'),
            ])
            self.assertTrue(os.path.isfile(os.path.join(out, 'credentials_v2.json')))
            self.assertTrue(os.path.isfile(os.path.join(out, 'service_principal.json')))
            self.assertTrue(os.path.isfile(os.path.join(out, 'test_connections.ps1')))

    def test_credentials_v2_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.gen.write_auth_artifacts(tmp, [_ds('SQL', 'sqlserver')])
            with open(os.path.join(out, 'credentials_v2.json'), encoding='utf-8') as f:
                data = json.load(f)
            self.assertEqual(data['version'], 2)

    def test_empty_datasources(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.gen.write_auth_artifacts(tmp, [])
            self.assertTrue(os.path.isdir(out))


if __name__ == '__main__':
    unittest.main()
