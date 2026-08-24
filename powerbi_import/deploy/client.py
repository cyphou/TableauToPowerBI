"""
Fabric API client for making HTTP requests.

Provides a resilient HTTP client with retry strategy for
Microsoft Fabric REST API operations.

Requires: pip install requests (optional dependency)
"""

import logging
import json as _json
import time
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

logger = logging.getLogger(__name__)


class FabricClient:
    """HTTP client for Fabric API requests.

    Uses urllib from stdlib by default. If `requests` is installed,
    it will use requests with retry strategy instead.
    """

    def __init__(self, authenticator=None):
        """
        Initialize Fabric API Client.

        Args:
            authenticator: FabricAuthenticator instance (creates default if None)
        """
        if authenticator is None:
            from .auth import FabricAuthenticator
            authenticator = FabricAuthenticator()

        self.authenticator = authenticator

        import sys
        sys.path.insert(0, __file__.rsplit('\\', 1)[0] if '\\' in __file__
                        else __file__.rsplit('/', 1)[0])
        from config.settings import get_settings

        settings = get_settings()
        self.base_url = settings.fabric_api_base_url
        self.workspace_id = settings.fabric_workspace_id
        self.timeout = settings.deployment_timeout
        self.retry_attempts = settings.retry_attempts
        self.retry_delay = settings.retry_delay
        self._session = None

        # Try to use requests if available
        try:
            import requests  # type: ignore[import-not-found]
            from requests.adapters import HTTPAdapter  # type: ignore[import-not-found]
            from urllib3.util.retry import Retry  # type: ignore[import-not-found]

            session = requests.Session()
            retry_strategy = Retry(
                total=self.retry_attempts,
                backoff_factor=1,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'],
            )
            adapter = HTTPAdapter(max_retries=retry_strategy)
            session.mount('http://', adapter)
            session.mount('https://', adapter)
            self._session = session
            self._use_requests = True
            logger.debug('Using requests library with retry strategy')
        except ImportError:
            self._use_requests = False
            logger.debug('Using urllib (stdlib) — install requests for retry support')

    def _request(self, method, endpoint, data=None, params=None):
        """
        Make HTTP request to Fabric API.

        Args:
            method: HTTP method (GET, POST, PUT, PATCH, DELETE)
            endpoint: API endpoint path
            data: Request body data (dict)
            params: Query parameters (dict)

        Returns:
            Response dict

        Raises:
            Exception: If request fails
        """
        url = f'{self.base_url}{endpoint}'
        if params:
            url = f'{url}?{urlencode(params)}'

        headers = self.authenticator.get_headers()

        logger.debug(f'{method} {url}')

        status_code, response_headers, payload = self._send_request(
            method, url, data, headers)
        if status_code == 202:
            return self._poll_operation(response_headers, headers)
        return payload

    def _send_request(self, method, url, data, headers, timeout=None):
        """Send one request without interpreting long-running operations."""
        request_timeout = self.timeout if timeout is None else timeout
        if self._use_requests and self._session is not None:
            response = self._session.request(
                method=method,
                url=url,
                json=data,
                headers=headers,
                timeout=request_timeout,
            )
            response.raise_for_status()
            payload = response.json() if response.text else {}
            return response.status_code, response.headers, payload

        body = _json.dumps(data).encode('utf-8') if data else None
        req = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(req, timeout=request_timeout) as resp:
                resp_body = resp.read().decode('utf-8')
                payload = _json.loads(resp_body) if resp_body else {}
                status_code = getattr(resp, 'status', None)
                if status_code is None:
                    status_code = resp.getcode()
                return status_code, resp.headers, payload
        except HTTPError as e:
            logger.error(f'HTTP {e.code}: {e.reason}')
            raise
        except URLError as e:
            logger.error(f'URL error: {e.reason}')
            raise

    def _poll_operation(self, initial_headers, auth_headers):
        """Poll a Fabric long-running operation and return its result."""
        location = self._get_header(initial_headers, 'Location')
        if not location:
            raise RuntimeError(
                'Fabric operation returned HTTP 202 without a Location header')

        deadline = time.monotonic() + self.timeout
        retry_headers = initial_headers
        running_statuses = {'running', 'inprogress', 'notstarted'}
        failed_statuses = {'failed', 'cancelled'}

        while True:
            self._wait_for_retry(retry_headers, deadline, location)
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f'Fabric operation timed out after {self.timeout} seconds: '
                    f'{location}')

            _, retry_headers, payload = self._send_request(
                'GET', location, None, auth_headers,
                timeout=self._remaining_timeout(deadline, location))
            status = str(payload.get('status', '')).strip().lower()

            if status in running_statuses:
                continue
            if status == 'completed':
                return payload
            if status == 'succeeded':
                result_url = f"{location.rstrip('/')}/result"
                _, _, result = self._send_request(
                    'GET', result_url, None, auth_headers,
                    timeout=self._remaining_timeout(deadline, location))
                return result
            if status in failed_statuses:
                detail = self._operation_error(payload)
                raise RuntimeError(
                    f'Fabric operation {status} at {location}: {detail}')

            raise RuntimeError(
                f"Fabric operation returned unknown status "
                f"{payload.get('status')!r} at {location}")

    def _wait_for_retry(self, headers, deadline, location):
        """Wait for Retry-After without exceeding the operation deadline."""
        raw_delay = self._get_header(headers, 'Retry-After')
        try:
            delay = float(raw_delay) if raw_delay is not None else float(
                self.retry_delay)
        except (TypeError, ValueError):
            delay = float(self.retry_delay)
        delay = max(0.0, delay)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or delay > remaining:
            raise RuntimeError(
                f'Fabric operation timed out after {self.timeout} seconds: '
                f'{location}')
        if delay:
            time.sleep(delay)

    def _remaining_timeout(self, deadline, location):
        """Return the remaining LRO budget or raise a clear timeout error."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f'Fabric operation timed out after {self.timeout} seconds: '
                f'{location}')
        return remaining

    @staticmethod
    def _get_header(headers, name):
        """Read a response header from case-sensitive or plain mappings."""
        if headers is None:
            return None
        value = headers.get(name)
        if value is not None:
            return value
        target = name.lower()
        for key, candidate in headers.items():
            if str(key).lower() == target:
                return candidate
        return None

    @staticmethod
    def _operation_error(payload):
        """Extract a useful API error from an operation response."""
        error = payload.get('error')
        if isinstance(error, dict):
            message = error.get('message') or error.get('code')
            if message:
                return str(message)
            return _json.dumps(error)
        if error:
            return str(error)
        message = payload.get('message') or payload.get('errorMessage')
        code = payload.get('errorCode')
        if code and message:
            return f'{code}: {message}'
        return str(message or code or 'No API error message was provided')

    def get(self, endpoint, params=None):
        """GET request."""
        return self._request('GET', endpoint, params=params)

    def get_absolute(self, url):
        """GET an authenticated absolute URL using the configured transport."""
        headers = self.authenticator.get_headers()
        status_code, response_headers, payload = self._send_request(
            'GET', url, None, headers)
        if status_code == 202:
            return self._poll_operation(response_headers, headers)
        return payload

    def upload_onelake_file(self, workspace_id, lakehouse_id, relative_path,
                            content):
        """Create or replace a file in a Lakehouse through OneLake DFS."""
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError('OneLake file content must be bytes')
        safe_path = '/'.join(
            quote(segment, safe='')
            for segment in str(relative_path).replace('\\', '/').split('/')
            if segment
        )
        if not safe_path:
            raise ValueError('OneLake relative path must not be empty')

        base_url = (
            'https://onelake.dfs.fabric.microsoft.com/'
            f'{quote(str(workspace_id), safe="")}/'
            f'{quote(str(lakehouse_id), safe="")}.Lakehouse/{safe_path}'
        )
        from .auth import FabricAuthenticator
        headers = self.authenticator.get_headers(
            scopes=FabricAuthenticator.STORAGE_SCOPE,
            content_type='application/octet-stream',
        )
        headers['x-ms-version'] = '2023-11-03'

        self._send_binary_request(
            'PUT', f'{base_url}?resource=file', b'', headers)
        position = 0
        chunk_size = 4 * 1024 * 1024
        for offset in range(0, len(content), chunk_size):
            chunk = bytes(content[offset:offset + chunk_size])
            self._send_binary_request(
                'PATCH',
                f'{base_url}?action=append&position={position}',
                chunk,
                headers,
            )
            position += len(chunk)
        self._send_binary_request(
            'PATCH', f'{base_url}?action=flush&position={position}',
            b'', headers)
        return {'path': relative_path, 'size': len(content)}

    def _send_binary_request(self, method, url, data, headers):
        """Send a binary data-plane request and return response metadata."""
        if self._use_requests and self._session is not None:
            response = self._session.request(
                method=method,
                url=url,
                data=data,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.status_code, response.headers

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status_code = getattr(response, 'status', None)
                if status_code is None:
                    status_code = response.getcode()
                return status_code, response.headers
        except HTTPError as exc:
            logger.error('HTTP %s: %s', exc.code, exc.reason)
            raise
        except URLError as exc:
            logger.error('URL error: %s', exc.reason)
            raise

    def post(self, endpoint, data):
        """POST request."""
        return self._request('POST', endpoint, data=data)

    def put(self, endpoint, data):
        """PUT request."""
        return self._request('PUT', endpoint, data=data)

    def patch(self, endpoint, data):
        """PATCH request."""
        return self._request('PATCH', endpoint, data=data)

    def delete(self, endpoint):
        """DELETE request."""
        return self._request('DELETE', endpoint)

    def list_workspaces(self):
        """List all accessible workspaces."""
        return self.get('/workspaces')

    def get_workspace(self, workspace_id):
        """Get workspace details."""
        return self.get(f'/workspaces/{workspace_id}')

    def list_items(self, workspace_id, item_type=None):
        """
        List items in workspace.

        Args:
            workspace_id: Workspace ID
            item_type: Filter by item type (Dataset, Report, etc.)
        """
        endpoint = f'/workspaces/{workspace_id}/items'
        params = {}
        if item_type:
            params['$filter'] = f"type eq '{item_type}'"
        return self.get(endpoint, params=params)
