"""
Credential Vault — pluggable per-tenant credential storage.

Provides a unified interface for credential lookup that prevents cleartext
secrets from being written to disk. Three backends:

1. **Environment variables** (default) — ``TENANT_{name}_SERVER``, etc.
2. **Azure Key Vault** — requires ``azure-identity`` + ``azure-keyvault-secrets``
3. **Plain JSON** (dev-only) — warns loudly; blocked in production mode

Usage::

    from powerbi_import.deploy.credential_vault import CredentialVault

    vault = CredentialVault.from_config({"backend": "env"})
    server = vault.get("Contoso", "TENANT_SERVER")

Sprint 133.1
"""

from __future__ import annotations

import json
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Security constants ───────────────────────────────────────────────────────

_SAFE_NAME_RE = re.compile(r'^[A-Za-z0-9_\-]+$')
_SAFE_KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]*$')
_MAX_VALUE_LEN = 4096


def _validate_tenant_name(name: str) -> None:
    """Raise ValueError if the tenant name is unsafe for use in lookups."""
    if not name:
        raise ValueError("Tenant name must not be empty")
    if '\x00' in name:
        raise ValueError("Tenant name contains null bytes")
    if not _SAFE_NAME_RE.match(name):
        raise ValueError(
            f"Tenant name '{name}' contains invalid characters; "
            "allowed: A-Z, a-z, 0-9, _, -"
        )


def _validate_key(key: str) -> None:
    """Raise ValueError if the credential key is unsafe."""
    if not key:
        raise ValueError("Credential key must not be empty")
    if '\x00' in key:
        raise ValueError("Credential key contains null bytes")
    if not _SAFE_KEY_RE.match(key):
        raise ValueError(
            f"Credential key '{key}' is invalid; must match [A-Z][A-Z0-9_]*"
        )


def _validate_value(value: str) -> None:
    """Raise ValueError if the credential value is unsafe."""
    if '\x00' in value:
        raise ValueError("Credential value contains null bytes")
    if any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        raise ValueError("Credential value contains control characters")
    if len(value) > _MAX_VALUE_LEN:
        raise ValueError(
            f"Credential value exceeds {_MAX_VALUE_LEN} character limit"
        )


# ── Abstract base ────────────────────────────────────────────────────────────

class CredentialBackend(ABC):
    """Abstract credential backend."""

    @abstractmethod
    def get(self, tenant_name: str, key: str) -> Optional[str]:
        """Retrieve a credential value for a tenant+key pair.

        Returns None if the credential is not found.
        """

    @abstractmethod
    def list_keys(self, tenant_name: str) -> List[str]:
        """List available credential keys for a tenant."""

    @abstractmethod
    def has(self, tenant_name: str, key: str) -> bool:
        """Check if a credential exists without retrieving it."""


# ── Environment variable backend ─────────────────────────────────────────────

class EnvVarBackend(CredentialBackend):
    """Read credentials from environment variables.

    Convention: ``TENANT_{TENANT_NAME}_{KEY}``
    Example: ``TENANT_CONTOSO_SERVER=contoso-sql.database.windows.net``
    """

    def _env_key(self, tenant_name: str, key: str) -> str:
        safe_name = tenant_name.upper().replace('-', '_')
        return f"TENANT_{safe_name}_{key}"

    def get(self, tenant_name: str, key: str) -> Optional[str]:
        _validate_tenant_name(tenant_name)
        _validate_key(key)
        env_key = self._env_key(tenant_name, key)
        value = os.environ.get(env_key)
        if value is not None:
            _validate_value(value)
        return value

    def list_keys(self, tenant_name: str) -> List[str]:
        _validate_tenant_name(tenant_name)
        prefix = f"TENANT_{tenant_name.upper().replace('-', '_')}_"
        keys = []
        for env_key in os.environ:
            if env_key.startswith(prefix):
                keys.append(env_key[len(prefix):])
        return keys

    def has(self, tenant_name: str, key: str) -> bool:
        _validate_tenant_name(tenant_name)
        _validate_key(key)
        return self._env_key(tenant_name, key) in os.environ


# ── Azure Key Vault backend ─────────────────────────────────────────────────

class AzureKeyVaultBackend(CredentialBackend):
    """Read credentials from Azure Key Vault.

    Secret naming convention: ``tenant-{name}--{key}``
    (Key Vault secret names allow alphanumerics and hyphens only.)

    Requires:
        - ``azure-identity`` package
        - ``azure-keyvault-secrets`` package
        - ``AZURE_KEYVAULT_URL`` environment variable
    """

    def __init__(self, vault_url: Optional[str] = None):
        self._vault_url = vault_url or os.environ.get('AZURE_KEYVAULT_URL', '')
        if not self._vault_url:
            raise ValueError(
                "Azure Key Vault URL is required. "
                "Set AZURE_KEYVAULT_URL or pass vault_url."
            )
        self._client = None  # lazy init

    def _get_client(self):
        if self._client is None:
            try:
                from azure.identity import DefaultAzureCredential
                from azure.keyvault.secrets import SecretClient
            except ImportError:
                raise ImportError(
                    "Azure Key Vault backend requires 'azure-identity' and "
                    "'azure-keyvault-secrets' packages. "
                    "Install with: pip install azure-identity azure-keyvault-secrets"
                )
            credential = DefaultAzureCredential()
            self._client = SecretClient(
                vault_url=self._vault_url, credential=credential
            )
        return self._client

    def _secret_name(self, tenant_name: str, key: str) -> str:
        """Build Key Vault secret name from tenant+key."""
        safe_name = tenant_name.lower().replace('_', '-')
        safe_key = key.lower().replace('_', '-')
        return f"tenant-{safe_name}--{safe_key}"

    def get(self, tenant_name: str, key: str) -> Optional[str]:
        _validate_tenant_name(tenant_name)
        _validate_key(key)
        try:
            secret = self._get_client().get_secret(
                self._secret_name(tenant_name, key)
            )
            value = secret.value
            if value is not None:
                _validate_value(value)
            return value
        except Exception as e:
            if 'SecretNotFound' in type(e).__name__ or '404' in str(e):
                return None
            logger.warning(
                "Key Vault lookup failed for %s/%s: %s",
                tenant_name, key, e
            )
            return None

    def list_keys(self, tenant_name: str) -> List[str]:
        _validate_tenant_name(tenant_name)
        prefix = f"tenant-{tenant_name.lower().replace('_', '-')}--"
        keys = []
        try:
            for prop in self._get_client().list_properties_of_secrets():
                if prop.name.startswith(prefix):
                    raw_key = prop.name[len(prefix):]
                    keys.append(raw_key.upper().replace('-', '_'))
        except Exception as e:
            logger.warning("Key Vault list failed for %s: %s", tenant_name, e)
        return keys

    def has(self, tenant_name: str, key: str) -> bool:
        return self.get(tenant_name, key) is not None


# ── Plain JSON backend (dev only) ───────────────────────────────────────────

class JsonFileBackend(CredentialBackend):
    """Read credentials from a plain JSON file (development only).

    Format::

        {
            "tenants": {
                "Contoso": {
                    "TENANT_SERVER": "localhost",
                    "TENANT_DATABASE": "dev_db"
                }
            }
        }

    WARNING: This backend stores secrets in cleartext. It is intended for
    local development only and emits a warning on initialization.
    """

    def __init__(self, path: str, production: bool = False):
        if production:
            raise ValueError(
                "JSON file credential backend is not allowed in production mode. "
                "Use 'env' or 'keyvault' backend instead."
            )
        logger.warning(
            "⚠ Using plain JSON credential backend at '%s'. "
            "Do NOT use in production — secrets are stored in cleartext.",
            path,
        )
        self._data: Dict[str, Dict[str, str]] = {}
        self._load(path)

    def _load(self, path: str) -> None:
        resolved = os.path.realpath(path)
        if not os.path.exists(resolved):
            raise FileNotFoundError(f"Credential file not found: {resolved}")

        with open(resolved, 'r', encoding='utf-8') as f:
            raw = f.read()

        if len(raw) > 1_048_576:
            raise ValueError("Credential file exceeds 1 MB size limit")

        data = json.loads(raw)
        if not isinstance(data, dict) or 'tenants' not in data:
            raise ValueError(
                "Credential file must be a JSON object with a 'tenants' key"
            )

        tenants = data['tenants']
        if not isinstance(tenants, dict):
            raise ValueError("'tenants' must be a JSON object")

        for name, creds in tenants.items():
            _validate_tenant_name(name)
            if not isinstance(creds, dict):
                raise ValueError(f"Credentials for '{name}' must be a JSON object")
            validated = {}
            for k, v in creds.items():
                _validate_key(k)
                if not isinstance(v, str):
                    raise ValueError(
                        f"Credential '{name}/{k}' must be a string, "
                        f"got {type(v).__name__}"
                    )
                _validate_value(v)
                validated[k] = v
            self._data[name] = validated

    def get(self, tenant_name: str, key: str) -> Optional[str]:
        _validate_tenant_name(tenant_name)
        _validate_key(key)
        tenant_creds = self._data.get(tenant_name, {})
        return tenant_creds.get(key)

    def list_keys(self, tenant_name: str) -> List[str]:
        _validate_tenant_name(tenant_name)
        return list(self._data.get(tenant_name, {}).keys())

    def has(self, tenant_name: str, key: str) -> bool:
        _validate_tenant_name(tenant_name)
        _validate_key(key)
        return key in self._data.get(tenant_name, {})


# ── Unified Vault Facade ────────────────────────────────────────────────────

@dataclass
class CredentialVault:
    """Unified credential vault with pluggable backend.

    The vault resolves ``${TENANT_*}`` placeholders in tenant configs by
    looking up the corresponding value from the configured backend.
    """
    backend: CredentialBackend
    production: bool = False

    @staticmethod
    def from_config(config: Dict[str, Any]) -> 'CredentialVault':
        """Create a vault from a configuration dictionary.

        Args:
            config: Dictionary with keys:
                - ``backend``: ``"env"`` | ``"keyvault"`` | ``"json"``
                - ``vault_url``: (keyvault only) Azure Key Vault URL
                - ``credentials_file``: (json only) path to credentials JSON
                - ``production``: bool, defaults to False
        """
        backend_type = config.get('backend', 'env')
        production = config.get('production', False)

        if backend_type == 'env':
            backend = EnvVarBackend()
        elif backend_type == 'keyvault':
            vault_url = config.get('vault_url')
            backend = AzureKeyVaultBackend(vault_url=vault_url)
        elif backend_type == 'json':
            creds_file = config.get('credentials_file', '')
            if not creds_file:
                raise ValueError(
                    "JSON backend requires 'credentials_file' in config"
                )
            backend = JsonFileBackend(path=creds_file, production=production)
        else:
            raise ValueError(
                f"Unknown credential backend: '{backend_type}'. "
                "Supported: env, keyvault, json"
            )

        return CredentialVault(backend=backend, production=production)

    def get(self, tenant_name: str, key: str) -> Optional[str]:
        """Retrieve a credential for a tenant."""
        return self.backend.get(tenant_name, key)

    def has(self, tenant_name: str, key: str) -> bool:
        """Check if a credential exists."""
        return self.backend.has(tenant_name, key)

    def list_keys(self, tenant_name: str) -> List[str]:
        """List available credential keys for a tenant."""
        return self.backend.list_keys(tenant_name)

    def resolve_overrides(self, tenant_name: str,
                          overrides: Dict[str, str]) -> Dict[str, str]:
        """Resolve placeholders using the vault.

        For each override whose value is empty or ``"${VAULT}"`` or starts
        with ``"${VAULT:"``, look up the actual value from the backend.

        Example::

            # In tenants.json:
            "connection_overrides": {
                "${TENANT_SERVER}": "${VAULT}",
                "${TENANT_DATABASE}": "explicit_value"
            }

        The ``${VAULT}`` value triggers a vault lookup using the
        placeholder name (without ``${}``).

        Returns:
            New dict with resolved values. Raises ValueError if a
            required credential is missing.
        """
        resolved = {}
        for placeholder, value in overrides.items():
            if value == '${VAULT}' or value == '':
                # Extract key from placeholder: ${TENANT_SERVER} → TENANT_SERVER
                key = placeholder.strip('${}')
                _validate_key(key)
                vault_value = self.backend.get(tenant_name, key)
                if vault_value is None:
                    raise ValueError(
                        f"Credential not found: tenant='{tenant_name}', "
                        f"key='{key}' (placeholder='{placeholder}')"
                    )
                resolved[placeholder] = vault_value
            else:
                resolved[placeholder] = value
        return resolved

    def validate_tenant_credentials(
        self,
        tenant_name: str,
        required_keys: List[str],
    ) -> List[str]:
        """Validate that all required credentials exist for a tenant.

        Returns:
            List of error messages (empty if all credentials are present).
        """
        errors = []
        for key in required_keys:
            try:
                _validate_key(key)
            except ValueError as e:
                errors.append(str(e))
                continue
            if not self.backend.has(tenant_name, key):
                errors.append(
                    f"Missing credential: tenant='{tenant_name}', key='{key}'"
                )
        return errors

    def validate_all_tenants(
        self,
        tenants: List[Dict[str, Any]],
    ) -> Dict[str, List[str]]:
        """Validate credentials for all tenants.

        Args:
            tenants: List of dicts with 'name' and 'connection_overrides'.

        Returns:
            Dict mapping tenant name → list of error messages.
        """
        results = {}
        for tenant in tenants:
            name = tenant.get('name', '')
            overrides = tenant.get('connection_overrides', {})
            required_keys = []
            for placeholder, value in overrides.items():
                if value in ('${VAULT}', ''):
                    key = placeholder.strip('${}')
                    required_keys.append(key)
            errors = self.validate_tenant_credentials(name, required_keys)
            if errors:
                results[name] = errors
        return results
