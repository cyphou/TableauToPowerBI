"""Gateway and OAuth configuration generator for Power BI data connections.

Generates data gateway connection references and OAuth redirect templates
that users can configure with their actual credentials and gateway IDs.

Usage:
    from powerbi_import.gateway_config import GatewayConfigGenerator
    gen = GatewayConfigGenerator()
    config = gen.generate_gateway_config(datasources)
    gen.write_config(project_dir, config)
"""

import json
import os
import uuid


# ═══════════════════════════════════════════════════════════════════
# Connector → OAuth / Gateway mapping
# ═══════════════════════════════════════════════════════════════════

OAUTH_CONNECTORS = {
    'bigquery': {
        'provider': 'Google',
        'auth_type': 'OAuth2',
        'auth_url': 'https://accounts.google.com/o/oauth2/v2/auth',
        'token_url': 'https://oauth2.googleapis.com/token',
        'scopes': ['https://www.googleapis.com/auth/bigquery.readonly'],
    },
    'snowflake': {
        'provider': 'Snowflake',
        'auth_type': 'OAuth2',
        'auth_url': 'https://<account>.snowflakecomputing.com/oauth/authorize',
        'token_url': 'https://<account>.snowflakecomputing.com/oauth/token-request',
        'scopes': ['session:role:<role>'],
    },
    'salesforce': {
        'provider': 'Salesforce',
        'auth_type': 'OAuth2',
        'auth_url': 'https://login.salesforce.com/services/oauth2/authorize',
        'token_url': 'https://login.salesforce.com/services/oauth2/token',
        'scopes': ['api', 'refresh_token'],
    },
    'google_sheets': {
        'provider': 'Google',
        'auth_type': 'OAuth2',
        'auth_url': 'https://accounts.google.com/o/oauth2/v2/auth',
        'token_url': 'https://oauth2.googleapis.com/token',
        'scopes': ['https://www.googleapis.com/auth/spreadsheets.readonly'],
    },
    'google_analytics': {
        'provider': 'Google',
        'auth_type': 'OAuth2',
        'auth_url': 'https://accounts.google.com/o/oauth2/v2/auth',
        'token_url': 'https://oauth2.googleapis.com/token',
        'scopes': ['https://www.googleapis.com/auth/analytics.readonly'],
    },
    'azure_sql': {
        'provider': 'AzureAD',
        'auth_type': 'OAuth2',
        'auth_url': 'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize',
        'token_url': 'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token',
        'scopes': ['https://database.windows.net/.default'],
    },
    'azure_synapse': {
        'provider': 'AzureAD',
        'auth_type': 'OAuth2',
        'auth_url': 'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize',
        'token_url': 'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token',
        'scopes': ['https://database.windows.net/.default'],
    },
    'sharepoint': {
        'provider': 'AzureAD',
        'auth_type': 'OAuth2',
        'auth_url': 'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize',
        'token_url': 'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token',
        'scopes': ['https://graph.microsoft.com/.default'],
    },
    'databricks': {
        'provider': 'Databricks',
        'auth_type': 'PersonalAccessToken',
        'auth_url': '',
        'token_url': '',
        'scopes': [],
    },
}

# Sprint 183: additional OAuth-capable connectors
OAUTH_CONNECTORS.update({
    'dynamics365': {
        'provider': 'AzureAD',
        'auth_type': 'OAuth2',
        'auth_url': 'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize',
        'token_url': 'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token',
        'scopes': ['https://{org}.crm.dynamics.com/.default'],
    },
    'dataverse': {
        'provider': 'AzureAD',
        'auth_type': 'OAuth2',
        'auth_url': 'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize',
        'token_url': 'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token',
        'scopes': ['https://{org}.crm.dynamics.com/.default'],
    },
    'servicenow': {
        'provider': 'ServiceNow',
        'auth_type': 'OAuth2',
        'auth_url': 'https://<instance>.service-now.com/oauth_auth.do',
        'token_url': 'https://<instance>.service-now.com/oauth_token.do',
        'scopes': ['useraccount'],
    },
    'azure_blob': {
        'provider': 'AzureAD',
        'auth_type': 'OAuth2',
        'auth_url': 'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize',
        'token_url': 'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token',
        'scopes': ['https://storage.azure.com/.default'],
    },
    'azure_data_lake': {
        'provider': 'AzureAD',
        'auth_type': 'OAuth2',
        'auth_url': 'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize',
        'token_url': 'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token',
        'scopes': ['https://storage.azure.com/.default'],
    },
    'fabric_lakehouse': {
        'provider': 'AzureAD',
        'auth_type': 'OAuth2',
        'auth_url': 'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize',
        'token_url': 'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token',
        'scopes': ['https://analysis.windows.net/powerbi/api/.default'],
    },
})

# Sprint 183: connectors that support service principal (app-only) auth in PBI
SERVICE_PRINCIPAL_CAPABLE = frozenset({
    'azure_sql', 'azure_synapse', 'sharepoint', 'dataverse', 'dynamics365',
    'azure_blob', 'azure_data_lake', 'fabric_lakehouse', 'snowflake',
})

# Sprint 183: Tableau auth-mode → Power BI credential type
TABLEAU_AUTH_MAP = {
    'username-password': 'Basic',
    'password': 'Basic',
    'sspi': 'Windows',
    'integrated': 'Windows',
    'oauth': 'OAuth2',
    'oauth2': 'OAuth2',
    'token': 'Key',
    'pat': 'Key',
    'personal-access-token': 'Key',
    'service-account': 'ServicePrincipal',
    'kerberos': 'Windows',
    'saml': 'OAuth2',
    '': 'Anonymous',
}

GATEWAY_CONNECTORS = frozenset({
    'sqlserver', 'postgresql', 'mysql', 'oracle', 'sap_hana', 'sap_bw',
    'teradata', 'db2', 'informix', 'odbc', 'oledb',
})


class GatewayConfigGenerator:
    """Generates gateway and OAuth configuration files for PBI data connections."""

    def generate_gateway_config(self, datasources):
        """Analyze datasources and generate connection configs.

        Args:
            datasources: List of datasource dicts from extraction.

        Returns:
            dict with 'connections', 'gateway', and 'oauth' sections.
        """
        connections = []
        gateway_needed = False
        oauth_configs = []

        for ds in (datasources or []):
            conn_type = (ds.get('connection_type') or ds.get('type') or '').lower().replace(' ', '_')
            conn_info = ds.get('connection', {}) if isinstance(ds.get('connection'), dict) else {}
            server = conn_info.get('server', ds.get('server', ''))
            database = conn_info.get('database', ds.get('database', ''))
            ds_name = ds.get('name', ds.get('caption', f'Datasource_{len(connections) + 1}'))

            conn_entry = {
                'id': str(uuid.uuid4()),
                'name': ds_name,
                'connection_type': conn_type,
                'server': server,
                'database': database,
                'auth_type': 'Windows',  # default
            }

            # Check if this connector needs a gateway
            if conn_type in GATEWAY_CONNECTORS or (server and not server.startswith('http')):
                gateway_needed = True
                conn_entry['requires_gateway'] = True
                conn_entry['gateway_id'] = '${GATEWAY_ID}'
            else:
                conn_entry['requires_gateway'] = False

            # Check if this connector supports OAuth
            if conn_type in OAUTH_CONNECTORS:
                oauth = OAUTH_CONNECTORS[conn_type].copy()
                oauth['datasource_name'] = ds_name
                oauth['client_id'] = '${CLIENT_ID}'
                oauth['client_secret'] = '${CLIENT_SECRET}'
                oauth['redirect_uri'] = 'https://login.microsoftonline.com/common/oauth2/nativeclient'
                oauth_configs.append(oauth)
                conn_entry['auth_type'] = 'OAuth2'

            connections.append(conn_entry)

        return {
            'connections': connections,
            'gateway': {
                'required': gateway_needed,
                'gateway_id': '${GATEWAY_ID}' if gateway_needed else None,
                'gateway_name': '${GATEWAY_NAME}' if gateway_needed else None,
                'cluster_id': '${GATEWAY_CLUSTER_ID}' if gateway_needed else None,
                'note': 'Configure these values with your on-premises data gateway installation' if gateway_needed else 'No gateway required — all connections are cloud-based',
            },
            'oauth': oauth_configs,
        }

    def write_config(self, project_dir, config):
        """Write gateway/OAuth config files to the project directory.

        Args:
            project_dir: Path to the .pbip project root.
            config: Config dict from ``generate_gateway_config()``.
        """
        config_dir = os.path.join(project_dir, 'ConnectionConfig')
        os.makedirs(config_dir, exist_ok=True)

        # Main gateway config
        gateway_file = os.path.join(config_dir, 'gateway_config.json')
        with open(gateway_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

        # OAuth redirect templates (one per OAuth-enabled datasource)
        for oauth in config.get('oauth', []):
            safe_name = (oauth.get('datasource_name', 'ds')
                         .replace(' ', '_').replace('/', '_')[:50])
            oauth_file = os.path.join(config_dir, f'oauth_{safe_name}.json')
            with open(oauth_file, 'w', encoding='utf-8') as f:
                json.dump(oauth, f, indent=2, ensure_ascii=False)

        return config_dir

    def generate_and_write(self, project_dir, datasources):
        """Convenience: generate config and write to project in one call."""
        config = self.generate_gateway_config(datasources)
        return self.write_config(project_dir, config)

    # ── Sprint 183: OAuth & Authentication Flow Migration ───────────────────

    @staticmethod
    def map_tableau_auth(auth_mode):
        """Map a Tableau connection auth mode to a Power BI credential type."""
        key = (auth_mode or '').strip().lower()
        return TABLEAU_AUTH_MAP.get(key, 'Basic')

    def generate_credential_template_v2(self, datasources):
        """Sprint 183: structured credential template per datasource.

        Returns a dict keyed by datasource name describing the recommended
        Power BI credential type (mapped from Tableau auth mode), whether a
        gateway is required, and OAuth/service-principal hints. Secrets are
        emitted as ``${PLACEHOLDER}`` tokens — never real values.
        """
        template = {'version': 2, 'datasources': {}}
        for ds in (datasources or []):
            conn_type = (ds.get('connection_type') or ds.get('type') or '').lower().replace(' ', '_')
            conn_info = ds.get('connection', {}) if isinstance(ds.get('connection'), dict) else {}
            auth_mode = (conn_info.get('authentication')
                         or ds.get('authentication')
                         or ds.get('auth_mode') or '')
            ds_name = ds.get('name', ds.get('caption', f'Datasource_{len(template["datasources"]) + 1}'))
            cred_type = self.map_tableau_auth(auth_mode)
            # OAuth connectors override to OAuth2
            if conn_type in OAUTH_CONNECTORS and cred_type in ('Basic', 'Anonymous', 'Windows'):
                cred_type = 'OAuth2'

            entry = {
                'connection_type': conn_type,
                'tableau_auth_mode': auth_mode or '(unspecified)',
                'powerbi_credential_type': cred_type,
                'requires_gateway': conn_type in GATEWAY_CONNECTORS,
                'supports_service_principal': conn_type in SERVICE_PRINCIPAL_CAPABLE,
            }
            if cred_type == 'Basic':
                entry['username'] = '${USERNAME}'
                entry['password'] = '${PASSWORD}'
            elif cred_type == 'OAuth2':
                entry['oauth'] = {
                    'provider': OAUTH_CONNECTORS.get(conn_type, {}).get('provider', 'Custom'),
                    'client_id': '${CLIENT_ID}',
                    'client_secret': '${CLIENT_SECRET}',
                    'tenant_id': '${TENANT_ID}',
                }
            elif cred_type == 'Key':
                entry['key'] = '${ACCESS_TOKEN}'
            elif cred_type == 'ServicePrincipal':
                entry['service_principal'] = {
                    'tenant_id': '${TENANT_ID}',
                    'client_id': '${SP_CLIENT_ID}',
                    'client_secret': '${SP_CLIENT_SECRET}',
                }
            template['datasources'][ds_name] = entry
        return template

    def generate_service_principal_config(self, datasources=None):
        """Sprint 183: Azure AD service principal (app-only) config template.

        Lists the connectors in the workbook that can use SP auth and emits a
        placeholder app-registration block for unattended refresh.
        """
        sp_connectors = []
        for ds in (datasources or []):
            conn_type = (ds.get('connection_type') or ds.get('type') or '').lower().replace(' ', '_')
            if conn_type in SERVICE_PRINCIPAL_CAPABLE:
                sp_connectors.append({
                    'datasource': ds.get('name', ds.get('caption', conn_type)),
                    'connection_type': conn_type,
                })
        return {
            'service_principal': {
                'tenant_id': '${TENANT_ID}',
                'client_id': '${SP_CLIENT_ID}',
                'client_secret': '${SP_CLIENT_SECRET}',
                'certificate_thumbprint': '${SP_CERT_THUMBPRINT}',
                'authority': 'https://login.microsoftonline.com/${TENANT_ID}',
            },
            'applicable_datasources': sp_connectors,
            'api_permissions': [
                'https://analysis.windows.net/powerbi/api/.default',
            ],
            'note': ('Register an Azure AD app, grant it dataset/datasource '
                     'permissions, and store the secret in Azure Key Vault. '
                     'Enable "Allow service principals to use Power BI APIs" '
                     'in the tenant admin portal.'),
        }

    def generate_connection_test_script(self, config):
        """Sprint 183: emit a PowerShell script that validates connections.

        The script iterates the configured connections and calls the Power BI
        REST API to verify datasource bindings and (for gateway connections)
        gateway reachability. Returns the script text.
        """
        connections = config.get('connections', []) if isinstance(config, dict) else []
        lines = [
            '# Auto-generated by Tableau→Power BI migration (Sprint 183)',
            '# Connection validation script — requires MicrosoftPowerBIMgmt module',
            '#requires -Modules MicrosoftPowerBIMgmt',
            '',
            'param(',
            '    [Parameter(Mandatory=$true)][string]$WorkspaceId,',
            '    [Parameter(Mandatory=$true)][string]$DatasetId',
            ')',
            '',
            'Write-Host "Connecting to Power BI Service..." -ForegroundColor Cyan',
            'Connect-PowerBIServiceAccount | Out-Null',
            '',
            '$datasources = Get-PowerBIDatasource -DatasetId $DatasetId -WorkspaceId $WorkspaceId',
            'Write-Host "Found $($datasources.Count) datasource(s) bound to dataset $DatasetId"',
            '',
            '$expected = @(',
        ]
        for conn in connections:
            name = str(conn.get('name', '')).replace("'", "''")
            ctype = str(conn.get('connection_type', '')).replace("'", "''")
            gw = 'true' if conn.get('requires_gateway') else 'false'
            lines.append(f"    @{{ Name = '{name}'; Type = '{ctype}'; Gateway = ${gw} }},")
        if connections:
            lines[-1] = lines[-1].rstrip(',')
        lines += [
            ')',
            '',
            'foreach ($e in $expected) {',
            '    $match = $datasources | Where-Object { $_.connectionDetails -ne $null }',
            '    if ($e.Gateway) {',
            '        Write-Host "[$($e.Name)] requires gateway — verify gateway binding." -ForegroundColor Yellow',
            '    } else {',
            '        Write-Host "[$($e.Name)] cloud connection — checking credentials." -ForegroundColor Green',
            '    }',
            '}',
            '',
            'Write-Host "Connection validation complete." -ForegroundColor Cyan',
        ]
        return '\n'.join(lines) + '\n'

    def write_auth_artifacts(self, project_dir, datasources):
        """Sprint 183: write credential template v2, SP config and test script."""
        config_dir = os.path.join(project_dir, 'ConnectionConfig')
        os.makedirs(config_dir, exist_ok=True)
        config = self.generate_gateway_config(datasources)

        cred_v2 = self.generate_credential_template_v2(datasources)
        with open(os.path.join(config_dir, 'credentials_v2.json'), 'w', encoding='utf-8') as f:
            json.dump(cred_v2, f, indent=2, ensure_ascii=False)

        sp_config = self.generate_service_principal_config(datasources)
        with open(os.path.join(config_dir, 'service_principal.json'), 'w', encoding='utf-8') as f:
            json.dump(sp_config, f, indent=2, ensure_ascii=False)

        test_script = self.generate_connection_test_script(config)
        with open(os.path.join(config_dir, 'test_connections.ps1'), 'w', encoding='utf-8') as f:
            f.write(test_script)

        return config_dir
