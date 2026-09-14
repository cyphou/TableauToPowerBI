"""
Authentication module for Microsoft Fabric API.

Supports two authentication methods:
1. Service Principal (client ID + secret)
2. Managed Identity (for Azure-hosted environments)

Requires: pip install azure-identity (optional dependency)
"""

import logging

logger = logging.getLogger(__name__)

# Lazy import — azure-identity is optional
try:
    from azure.identity import ClientSecretCredential, DefaultAzureCredential
except ImportError:
    ClientSecretCredential = None
    DefaultAzureCredential = None


class FabricAuthenticator:
    """Handles authentication with Microsoft Fabric API."""

    AUTHORITY_URL = 'https://login.microsoftonline.com'
    SCOPE = ['https://analysis.windows.net/powerbi/api/.default']
    STORAGE_SCOPE = ['https://storage.azure.com/.default']

    def __init__(self, use_managed_identity=False):
        """
        Initialize Fabric Authenticator.

        Args:
            use_managed_identity: Use Managed Identity instead of Service Principal
        """
        self.use_managed_identity = use_managed_identity
        self._token = None
        self._credential = None
        self._init_credential()

    def _init_credential(self):
        """Initialize the appropriate credential based on configuration."""
        import sys
        sys.path.insert(0, __file__.rsplit('\\', 1)[0] if '\\' in __file__
                        else __file__.rsplit('/', 1)[0])
        from config.settings import get_settings

        settings = get_settings()

        if self.use_managed_identity:
            if DefaultAzureCredential is None:
                raise ImportError(
                    'pip install azure-identity is required for authentication'
                )
            logger.info('Initializing Managed Identity credential')
            self._credential = DefaultAzureCredential()
        else:
            if ClientSecretCredential is None:
                raise ImportError(
                    'pip install azure-identity is required for authentication'
                )
            logger.info('Initializing Service Principal credential')
            self._credential = ClientSecretCredential(
                tenant_id=settings.fabric_tenant_id,
                client_id=settings.fabric_client_id,
                client_secret=settings.fabric_client_secret,
            )

    def get_token(self, scopes=None):
        """
        Get an access token for the requested API scopes.

        Returns:
            Access token string

        Raises:
            Exception: If token acquisition fails
        """
        try:
            token = self._credential.get_token(*(scopes or self.SCOPE))
            logger.debug('Successfully acquired access token')
            self._token = token.token
            return self._token
        except Exception as e:
            logger.error(f'Failed to acquire access token: {str(e)}')
            raise

    def get_headers(self, scopes=None, content_type='application/json'):
        """
        Get HTTP headers with authorization token.

        Returns:
            Dictionary of HTTP headers
        """
        token = self.get_token(scopes=scopes)
        return {
            'Authorization': f'Bearer {token}',
            'Content-Type': content_type,
            'Accept': 'application/json',
        }
