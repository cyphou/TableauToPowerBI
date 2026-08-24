"""
Tableau Server / Tableau Cloud REST API client.

Supports downloading workbooks (.twbx) from Tableau Server or
Tableau Cloud for migration. Handles authentication via:
  - Personal Access Token (PAT)
  - Username + password

Usage:
    client = TableauServerClient(
        server_url='https://tableau.company.com',
        token_name='my-pat',
        token_secret='secret...',
        site_id='my-site',
    )
    client.sign_in()
    workbooks = client.list_workbooks()
    client.download_workbook(workbooks[0]['id'], '/tmp/output.twbx')
    client.sign_out()
"""

import json
import logging
import os
import re
import unicodedata
import urllib.error
import urllib.parse

logger = logging.getLogger(__name__)

# Tableau REST API version
DEFAULT_API_VERSION = '3.21'


def _normalize_name(name):
    """Strip accents and lowercase for accent-insensitive comparison."""
    nfkd = unicodedata.normalize('NFKD', name)
    return ''.join(c for c in nfkd if not unicodedata.combining(c)).lower()

# Default page size for paginated requests
DEFAULT_PAGE_SIZE = 100


class TableauServerClient:
    """Lightweight Tableau Server REST API client (stdlib only)."""

    def __init__(self, server_url=None, token_name=None, token_secret=None,
                 username=None, password=None, site_id='',
                 api_version=None):
        """Initialize client.

        Auth priority:
          1. PAT (token_name + token_secret)
          2. Username + password
          3. Environment variables: TABLEAU_SERVER, TABLEAU_TOKEN_NAME,
             TABLEAU_TOKEN_SECRET, TABLEAU_SITE_ID

        Args:
            server_url: Tableau Server base URL (e.g., https://tableau.example.com).
            token_name: Personal Access Token name.
            token_secret: Personal Access Token secret.
            username: Tableau username (password auth).
            password: Tableau password.
            site_id: Site content URL (empty string for Default site).
            api_version: REST API version (default: 3.21).
        """
        self.server_url = (server_url or os.environ.get('TABLEAU_SERVER', '')).rstrip('/')
        self.token_name = token_name or os.environ.get('TABLEAU_TOKEN_NAME')
        self.token_secret = token_secret or os.environ.get('TABLEAU_TOKEN_SECRET')
        self.username = username or os.environ.get('TABLEAU_USERNAME')
        self.password = password or os.environ.get('TABLEAU_PASSWORD')
        self.site_id = site_id or os.environ.get('TABLEAU_SITE_ID', '')
        self.api_version = api_version or DEFAULT_API_VERSION

        self._auth_token = None
        self._site_luid = None

    @property
    def base_url(self):
        return f'{self.server_url}/api/{self.api_version}'

    @property
    def site_url(self):
        if not self._site_luid:
            raise RuntimeError('Not signed in — call sign_in() first')
        return f'{self.base_url}/sites/{self._site_luid}'

    def _request(self, method, url, headers=None, data=None, json_body=None,
                 stream_to=None):
        """Make an HTTP request using requests or urllib fallback.

        Args:
            method: HTTP method.
            url: Full URL.
            headers: Request headers.
            data: Raw body bytes.
            json_body: JSON body (dict).
            stream_to: If set, write response body to this file path.

        Returns:
            dict or None: Parsed JSON response, or None for downloads.
        """
        hdrs = dict(headers or {})
        if self._auth_token:
            hdrs['X-Tableau-Auth'] = self._auth_token
        # Accept JSON to avoid HTML responses from some servers
        if 'Accept' not in hdrs:
            hdrs['Accept'] = 'application/json'

        try:
            import requests as req_lib
            kwargs = {'headers': hdrs}
            if json_body is not None:
                kwargs['json'] = json_body
            elif data is not None:
                kwargs['data'] = data

            if stream_to:
                kwargs['stream'] = True
                resp = req_lib.request(method, url, **kwargs)
                resp.raise_for_status()
                with open(stream_to, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
                return None
            else:
                resp = req_lib.request(method, url, **kwargs)
                resp.raise_for_status()
                if resp.content:
                    return resp.json()
                return {}
        except ImportError:
            pass

        # urllib fallback
        import urllib.request
        import urllib.error
        import ssl

        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode('utf-8')
            hdrs['Content-Type'] = 'application/json'
        elif data is not None:
            body = data

        # Use default SSL context (picks up system/corporate certificates)
        ssl_context = ssl.create_default_context()
        if os.environ.get('TABLEAU_SSL_NO_VERIFY'):
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, context=ssl_context) as resp:
                if stream_to:
                    with open(stream_to, 'wb') as f:
                        while True:
                            chunk = resp.read(1024 * 256)
                            if not chunk:
                                break
                            f.write(chunk)
                    return None
                raw = resp.read()
                if raw:
                    text = raw.decode('utf-8', errors='replace')
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError as je:
                        # Server returned non-JSON (HTML login page, error page, etc.)
                        snippet = text[:500]
                        raise RuntimeError(
                            f'Tableau Server returned non-JSON response '
                            f'(HTTP {resp.status}). First 500 chars:\n{snippet}'
                        ) from je
                return {}
        except urllib.error.HTTPError as e:
            body_text = e.read().decode('utf-8', errors='replace')
            # Parse Tableau error JSON for a friendlier message
            detail = ''
            try:
                err_json = json.loads(body_text)
                err_obj = err_json.get('error', {})
                summary = err_obj.get('summary', '')
                detail = err_obj.get('detail', '')
                code = err_obj.get('code', '')
            except (json.JSONDecodeError, AttributeError):
                summary = ''
                code = ''

            if e.code == 403:
                hint = (
                    'Permission denied. The authenticated user does not have '
                    'access to this resource.\n'
                    '  Possible fixes:\n'
                    '  - Ensure the PAT user has at least "Explorer (can publish)" '
                    'or "Creator" site role\n'
                    '  - Check that the user has "Download/Save a Copy" permission '
                    'on the workbook/datasource\n'
                    '  - Ask a Tableau Server admin to grant download permissions'
                )
                if detail:
                    hint = f'{detail}\n\n  {hint}'
                raise RuntimeError(
                    f'Tableau API 403 Forbidden: {hint}'
                ) from e
            elif e.code == 401:
                raise RuntimeError(
                    'Tableau API 401 Unauthorized: Authentication failed. '
                    'Check your --token-name and --token-secret (or '
                    'TABLEAU_TOKEN_SECRET env var). The PAT may have expired.'
                ) from e
            elif e.code == 404:
                msg = detail or summary or 'Resource not found'
                raise RuntimeError(
                    f'Tableau API 404 Not Found: {msg}. '
                    'Check the workbook/datasource name or ID.'
                ) from e
            elif e.code == 409:
                msg = detail or summary or 'Conflict'
                raise RuntimeError(
                    f'Tableau API 409 Conflict: {msg}'
                ) from e
            else:
                raise RuntimeError(
                    f'Tableau API error {e.code}: '
                    f'{detail or summary or body_text[:1000]}'
                ) from e

    # ── Pagination helper ─────────────────────────────────────

    def _paginated_get(self, url, root_key, item_key,
                       page_size=DEFAULT_PAGE_SIZE):
        """Fetch all pages of a paginated REST API endpoint.

        Tableau REST API returns pagination metadata as:
            {"pagination": {"pageNumber": "1", "pageSize": "100", "totalAvailable": "250"}}

        Args:
            url: Base URL (without pageSize/pageNumber params).
            root_key: Top-level JSON key (e.g., 'workbooks').
            item_key: Nested key containing items (e.g., 'workbook').
            page_size: Items per page.

        Returns:
            list[dict]: All items across all pages.
        """
        all_items = []
        page_number = 1
        separator = '&' if '?' in url else '?'

        while True:
            paged_url = f'{url}{separator}pageSize={page_size}&pageNumber={page_number}'
            resp = self._request('GET', paged_url)

            items = resp.get(root_key, {}).get(item_key, [])
            if isinstance(items, dict):
                items = [items]
            all_items.extend(items)

            pagination = resp.get('pagination', {})
            total = int(pagination.get('totalAvailable', len(items)))
            if len(all_items) >= total or not items:
                break
            page_number += 1

        return all_items

    # ── Authentication ────────────────────────────────────────

    def sign_in(self):
        """Authenticate and obtain an auth token.

        Uses PAT if token_name/token_secret are set, otherwise
        falls back to username/password.

        Returns:
            str: The site LUID.
        """
        url = f'{self.base_url}/auth/signin'

        if self.token_name and self.token_secret:
            payload = {
                'credentials': {
                    'personalAccessTokenName': self.token_name,
                    'personalAccessTokenSecret': self.token_secret,
                    'site': {'contentUrl': self.site_id},
                }
            }
        elif self.username and self.password:
            payload = {
                'credentials': {
                    'name': self.username,
                    'password': self.password,
                    'site': {'contentUrl': self.site_id},
                }
            }
        else:
            raise ValueError(
                'No credentials provided. Set token_name/token_secret '
                'or username/password (or TABLEAU_TOKEN_NAME/TABLEAU_TOKEN_SECRET env vars).'
            )

        resp = self._request('POST', url, json_body=payload)
        creds = resp.get('credentials', {})
        self._auth_token = creds.get('token')
        self._site_luid = creds.get('site', {}).get('id')

        if not self._auth_token:
            raise RuntimeError('Sign-in failed — no token returned')

        logger.info(f'Signed in to {self.server_url} (site={self._site_luid})')
        return self._site_luid

    def sign_out(self):
        """Sign out and invalidate the auth token."""
        if self._auth_token:
            try:
                url = f'{self.base_url}/auth/signout'
                self._request('POST', url)
                logger.info('Signed out')
            except (urllib.error.URLError, OSError, RuntimeError) as e:
                logger.warning(f'Sign-out failed: {e}')
            finally:
                self._auth_token = None
                self._site_luid = None

    # ── Site info ─────────────────────────────────────────────

    def get_site_info(self):
        """Get site metadata.

        Returns:
            dict: Site info (id, name, contentUrl, state, etc.)
        """
        url = f'{self.base_url}/sites/{self._site_luid}'
        resp = self._request('GET', url)
        return resp.get('site', resp)

    # ── Workbooks ─────────────────────────────────────────────

    def list_workbooks(self, project_name=None):
        """List workbooks on the site (paginated).

        Args:
            project_name: Optional — filter by project name.

        Returns:
            list[dict]: Workbook metadata (id, name, project, contentUrl, etc.)
        """
        url = f'{self.site_url}/workbooks'
        if project_name:
            encoded = urllib.parse.quote(project_name, safe='')
            url += f'?filter=projectName:eq:{encoded}'

        workbooks = self._paginated_get(url, 'workbooks', 'workbook')
        logger.info(f'Found {len(workbooks)} workbooks')
        return workbooks

    def get_workbook(self, workbook_id):
        """Get workbook metadata by ID.

        Args:
            workbook_id: Workbook LUID.

        Returns:
            dict: Workbook metadata.
        """
        url = f'{self.site_url}/workbooks/{workbook_id}'
        resp = self._request('GET', url)
        return resp.get('workbook', resp)

    def get_workbook_connections(self, workbook_id):
        """Get data connections for a workbook.

        Args:
            workbook_id: Workbook LUID.

        Returns:
            list[dict]: Connection metadata (type, serverAddress, serverPort,
                        userName, dbClass, etc.)
        """
        url = f'{self.site_url}/workbooks/{workbook_id}/connections'
        resp = self._request('GET', url)
        conns = resp.get('connections', {}).get('connection', [])
        if isinstance(conns, dict):
            conns = [conns]
        return conns

    def download_workbook(self, workbook_id, output_path,
                          include_extract=True):
        """Download a workbook as .twbx.

        Args:
            workbook_id: Workbook LUID.
            output_path: Local file path to save the .twbx.
            include_extract: Include the data extract in the download.

        Returns:
            str: Path to the downloaded file.
        """
        url = f'{self.site_url}/workbooks/{workbook_id}/content'
        if not include_extract:
            url += '?includeExtract=false'

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        self._request('GET', url, stream_to=output_path)
        size = os.path.getsize(output_path)
        logger.info(f'Downloaded workbook {workbook_id} → {output_path} ({size} bytes)')
        return output_path

    def search_workbooks(self, name_pattern):
        """Search workbooks by name pattern.

        Supports regex matching, accent-insensitive matching, and
        contentUrl matching for names with special characters.

        Args:
            name_pattern: Regex pattern or plain name to match.

        Returns:
            list[dict]: Matching workbooks.
        """
        all_wb = self.list_workbooks()
        # 1. Try regex match on name
        try:
            pattern = re.compile(name_pattern, re.IGNORECASE)
            matches = [w for w in all_wb if pattern.search(w.get('name', ''))]
            if matches:
                return matches
        except re.error:
            pass  # name_pattern is not valid regex — continue with other strategies

        # 2. Accent-insensitive match (strips diacritics from both sides)
        norm_pattern = _normalize_name(name_pattern)
        matches = [
            w for w in all_wb
            if _normalize_name(w.get('name', '')) == norm_pattern
        ]
        if matches:
            return matches

        # 3. Accent-insensitive contains
        matches = [
            w for w in all_wb
            if norm_pattern in _normalize_name(w.get('name', ''))
        ]
        if matches:
            return matches

        # 4. contentUrl match (Tableau strips accents in contentUrl)
        matches = [
            w for w in all_wb
            if norm_pattern == _normalize_name(w.get('contentUrl', ''))
               or norm_pattern in _normalize_name(w.get('contentUrl', ''))
        ]
        return matches

    # ── Views ─────────────────────────────────────────────────

    def list_views(self):
        """List all views (sheets) on the site (paginated).

        Returns:
            list[dict]: View metadata (id, name, contentUrl, workbook, etc.)
        """
        url = f'{self.site_url}/views'
        return self._paginated_get(url, 'views', 'view')

    # ── Datasources ───────────────────────────────────────────

    def list_datasources(self):
        """List published datasources on the site (paginated).

        Returns:
            list[dict]: Datasource metadata.
        """
        url = f'{self.site_url}/datasources'
        return self._paginated_get(url, 'datasources', 'datasource')

    def download_datasource(self, datasource_id, output_path):
        """Download a published datasource as .tdsx.

        Args:
            datasource_id: Datasource LUID.
            output_path: Local file path.

        Returns:
            str: Path to the downloaded file.
        """
        url = f'{self.site_url}/datasources/{datasource_id}/content'
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        self._request('GET', url, stream_to=output_path)
        logger.info(f'Downloaded datasource {datasource_id} → {output_path}')
        return output_path

    # ── Projects ──────────────────────────────────────────────

    def list_projects(self):
        """List projects on the site (paginated).

        Returns:
            list[dict]: Project metadata.
        """
        url = f'{self.site_url}/projects'
        return self._paginated_get(url, 'projects', 'project')

    # ── Users ─────────────────────────────────────────────────

    def list_users(self):
        """List all users on the site (paginated).

        Returns:
            list[dict]: User metadata (id, name, siteRole, lastLogin, etc.)
        """
        url = f'{self.site_url}/users'
        return self._paginated_get(url, 'users', 'user')

    # ── Groups ────────────────────────────────────────────────

    def list_groups(self):
        """List all groups on the site (paginated).

        Returns:
            list[dict]: Group metadata (id, name, domain, etc.)
        """
        url = f'{self.site_url}/groups'
        return self._paginated_get(url, 'groups', 'group')

    # ── Schedules ─────────────────────────────────────────────

    def list_schedules(self):
        """List all schedules on the server (paginated).

        Returns:
            list[dict]: Schedule metadata (id, name, type, frequency, etc.)
        """
        url = f'{self.base_url}/schedules'
        return self._paginated_get(url, 'schedules', 'schedule')

    # ── Extract Tasks ─────────────────────────────────────────

    def get_workbook_extract_tasks(self, workbook_id):
        """Get extract refresh tasks for a specific workbook.

        Args:
            workbook_id: Workbook LUID.

        Returns:
            list[dict]: Extract task metadata (id, schedule, priority, type).
        """
        url = f'{self.site_url}/tasks/extractRefreshes'
        all_tasks = self._paginated_get(url, 'tasks', 'extractRefresh')
        # Filter to this workbook
        return [
            t for t in all_tasks
            if t.get('workbook', {}).get('id') == workbook_id
        ]

    def get_workbook_subscriptions(self, workbook_id):
        """Get email subscriptions for a specific workbook.

        Args:
            workbook_id: Workbook LUID.

        Returns:
            list[dict]: Subscription metadata (id, subject, user, schedule).
        """
        url = f'{self.site_url}/subscriptions'
        all_subs = self._paginated_get(url, 'subscriptions', 'subscription')
        # Filter to subscriptions targeting this workbook
        return [
            s for s in all_subs
            if s.get('content', {}).get('id') == workbook_id
            and s.get('content', {}).get('type', '').lower() == 'workbook'
        ]

    # ── Prep Flows ────────────────────────────────────────────

    def list_prep_flows(self):
        """List Prep flows on the site (paginated).

        Returns:
            list[dict]: Flow metadata (id, name, project, etc.)
        """
        url = f'{self.site_url}/flows'
        return self._paginated_get(url, 'flows', 'flow')

    def download_prep_flow(self, flow_id, output_path):
        """Download a Prep flow file.

        Args:
            flow_id: Flow LUID.
            output_path: Local file path to save the .tfl / .tflx.

        Returns:
            str: Path to the downloaded file.
        """
        url = f'{self.site_url}/flows/{flow_id}/content'
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        self._request('GET', url, stream_to=output_path)
        logger.info(f'Downloaded flow {flow_id} → {output_path}')
        return output_path

    # ── Batch download ────────────────────────────────────────

    def download_all_workbooks(self, output_dir, project_name=None):
        """Download all workbooks to a directory.

        Args:
            output_dir: Directory to save .twbx files.
            project_name: Optional project filter.

        Returns:
            list[dict]: Download results [{name, path, status, error?}].
        """
        workbooks = self.list_workbooks(project_name=project_name)
        os.makedirs(output_dir, exist_ok=True)
        results = []

        for wb in workbooks:
            wb_name = wb.get('name', 'unknown')
            safe_name = re.sub(r'[^\w\-.]', '_', wb_name)
            output_path = os.path.join(output_dir, f'{safe_name}.twbx')

            try:
                self.download_workbook(wb['id'], output_path)
                results.append({
                    'name': wb_name,
                    'path': output_path,
                    'status': 'success',
                })
            except (urllib.error.URLError, OSError, RuntimeError) as e:
                logger.error(f'Failed to download {wb_name}: {e}')
                results.append({
                    'name': wb_name,
                    'path': output_path,
                    'status': 'failed',
                    'error': str(e),
                })

        succeeded = sum(1 for r in results if r['status'] == 'success')
        logger.info(f'Downloaded {succeeded}/{len(results)} workbooks')
        return results

    # ── Context manager ───────────────────────────────────────

    def __enter__(self):
        self.sign_in()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.sign_out()
        return False

    # ═══════════════════════════════════════════════════════════════
    # Sprint 161 — Server Discovery & Metadata
    # ═══════════════════════════════════════════════════════════════

    def get_workbook_dependencies(self, workbook_id):
        """Get dependency graph for a workbook (datasources, views).

        Args:
            workbook_id: Workbook LUID.

        Returns:
            dict: {datasources: [ids], views: [ids], downstream: [wb_ids]}
        """
        # Connections show which datasources this workbook uses
        connections = self.get_workbook_connections(workbook_id)
        ds_ids = list({
            c.get('datasource', {}).get('id', '')
            for c in connections if c.get('datasource', {}).get('id')
        })

        # Find other workbooks using the same datasources
        downstream = set()
        all_workbooks = self.list_workbooks()
        for wb in all_workbooks:
            if wb.get('id') == workbook_id:
                continue
            try:
                wb_conns = self.get_workbook_connections(wb['id'])
                wb_ds_ids = {c.get('datasource', {}).get('id', '') for c in wb_conns}
                if wb_ds_ids & set(ds_ids):
                    downstream.add(wb['id'])
            except Exception:
                pass  # Skip workbooks we can't access

        return {
            'datasources': ds_ids,
            'views': [v.get('id') for v in (self.list_views() or [])
                      if v.get('workbook', {}).get('id') == workbook_id],
            'downstream_workbooks': list(downstream),
        }

    def get_published_datasource_details(self, datasource_id):
        """Get full metadata for a published datasource.

        Args:
            datasource_id: Datasource LUID.

        Returns:
            dict: Datasource metadata (name, type, connections, tables, owner).
        """
        url = f'{self.site_url}/datasources/{datasource_id}'
        resp = self._request('GET', url)
        return resp.get('datasource', {})

    def get_usage_stats(self, workbook_id, days=30):
        """Get workbook usage statistics (view count, last access).

        Args:
            workbook_id: Workbook LUID.
            days: Number of days to look back.

        Returns:
            dict: {totalViews, recentViews, lastAccessed, favorites}
        """
        # Views for this workbook
        views = self.list_views()
        wb_views = [v for v in (views or [])
                    if v.get('workbook', {}).get('id') == workbook_id]

        total_views = sum(v.get('usage', {}).get('totalViewCount', 0)
                         for v in wb_views)

        return {
            'totalViews': total_views,
            'viewCount': len(wb_views),
            'lastAccessed': max(
                (v.get('updatedAt', '') for v in wb_views), default=''),
        }

    def get_permissions(self, workbook_id):
        """Get workbook permissions (users, groups, capabilities).

        Args:
            workbook_id: Workbook LUID.

        Returns:
            list[dict]: Permission entries with granteeType, capabilities.
        """
        url = f'{self.site_url}/workbooks/{workbook_id}/permissions'
        resp = self._request('GET', url)
        perms_data = resp.get('permissions', {})
        grant_list = perms_data.get('granteeCapabilities', [])
        if isinstance(grant_list, dict):
            grant_list = [grant_list]

        permissions = []
        for grant in grant_list:
            grantee = grant.get('user') or grant.get('group', {})
            grantee_type = 'user' if 'user' in grant else 'group'
            capabilities = {}
            cap_list = grant.get('capabilities', {}).get('capability', [])
            if isinstance(cap_list, dict):
                cap_list = [cap_list]
            for cap in cap_list:
                capabilities[cap.get('name', '')] = cap.get('mode', '')

            permissions.append({
                'granteeType': grantee_type,
                'granteeName': grantee.get('name', ''),
                'granteeId': grantee.get('id', ''),
                'capabilities': capabilities,
            })

        return permissions

    def get_quality_warnings(self, content_type='workbook', content_id=None):
        """Get data quality warnings/certifications for content.

        Args:
            content_type: 'workbook', 'datasource', or 'table'.
            content_id: Content LUID (optional — returns all if None).

        Returns:
            list[dict]: Quality warning metadata.
        """
        url = f'{self.site_url}/dataQualityWarnings'
        if content_id:
            url = (f'{self.site_url}/{content_type}s/{content_id}'
                   f'/dataQualityWarnings')
        try:
            resp = self._request('GET', url)
            return resp.get('dataQualityWarnings', {}).get(
                'dataQualityWarning', [])
        except Exception:
            return []  # API may not be available on older servers

    def get_server_summary(self):
        """Get comprehensive server inventory summary.

        Returns:
            dict: {workbooks, datasources, users, groups, projects, schedules,
                   prep_flows, site_info}
        """
        return {
            'site_info': self.get_site_info(),
            'workbook_count': len(self.list_workbooks() or []),
            'datasource_count': len(self.list_datasources() or []),
            'user_count': len(self.list_users() or []),
            'group_count': len(self.list_groups() or []),
            'project_count': len(self.list_projects() or []),
            'schedule_count': len(self.list_schedules() or []),
            'prep_flow_count': len(self.list_prep_flows() or []),
        }

    # ═══════════════════════════════════════════════════════════════
    # Sprint 162 — Tableau Cloud & OAuth/JWT Authentication
    # ═══════════════════════════════════════════════════════════════

    def detect_cloud_vs_server(self):
        """Detect if connected to Tableau Cloud or Tableau Server.

        Returns:
            str: 'cloud' or 'server'
        """
        if not self.server_url:
            return 'server'
        cloud_domains = [
            'online.tableau.com',
            '10ax.online.tableau.com',
            'prod-useast-a.online.tableau.com',
            'prod-useast-b.online.tableau.com',
            'eu-west-1a.online.tableau.com',
            'prod-apnortheast-a.online.tableau.com',
        ]
        for domain in cloud_domains:
            if domain in self.server_url.lower():
                return 'cloud'
        return 'server'

    def sign_in_jwt(self, jwt_token, site_name=''):
        """Authenticate using JWT (Connected App or EAS).

        Tableau Cloud and Server 2021.4+ support JWT-based authentication
        via Connected Apps or External Authorization Server (EAS).

        Args:
            jwt_token: Pre-signed JWT token string.
            site_name: Site content URL (empty for default site).

        Returns:
            bool: True if authentication succeeded.
        """
        url = f'{self.server_url}/api/{self.api_version}/auth/signin'

        try:
            resp = self._request('POST', url, json_body={
                'credentials': {
                    'jwt': jwt_token,
                    'site': {'contentUrl': site_name or self.site_id},
                }
            })
            creds = resp.get('credentials', {})
            self._auth_token = creds.get('token', '')
            site_data = creds.get('site', {})
            self._site_luid = site_data.get('id', '')
            logger.info(f'JWT sign-in successful (site: {self._site_luid})')
            return True
        except Exception as e:
            logger.error(f'JWT sign-in failed: {e}')
            return False

    def get_metadata_graphql(self, query, variables=None):
        """Execute a Metadata API (GraphQL) query.

        Tableau Server 2019.3+ and Tableau Cloud expose a GraphQL-based
        Metadata API for lineage, schema, and quality information.

        Args:
            query: GraphQL query string.
            variables: Optional dict of query variables.

        Returns:
            dict: GraphQL response data.
        """
        url = f'{self.server_url}/api/metadata/graphql'

        try:
            resp = self._request('POST', url, json_body={
                'query': query,
                'variables': variables or {},
            })
            return resp.get('data') or {}
        except Exception as e:
            logger.error(f'Metadata GraphQL query failed: {e}')
            return {}

    # ═══════════════════════════════════════════════════════════════
    # Sprint 167 — Enterprise Server Migration Methods
    # ═══════════════════════════════════════════════════════════════

    def list_users_with_groups(self):
        """List all users enriched with their group memberships.

        Returns:
            list[dict]: User dicts with 'groups' key added.
        """
        users = self.list_users() or []
        groups = self.list_groups() or []

        user_groups = {}
        for group in groups:
            gid = group.get('id', '')
            gname = group.get('name', '')
            try:
                url = f'{self.site_url}/groups/{gid}/users'
                members = self._paginated_get(url, 'users', 'user')
                for member in members:
                    uid = member.get('id', '')
                    user_groups.setdefault(uid, []).append(gname)
            except Exception as e:
                logger.warning("Could not list members of group %s: %s", gname, e)

        for user in users:
            user['groups'] = user_groups.get(user.get('id', ''), [])

        return users

    def build_permission_matrix(self):
        """Build a site-wide permission matrix: user × workbook → capabilities.

        Returns:
            dict: {workbooks: [{id, name, permissions: [{user, caps}]}]}
        """
        workbooks = self.list_workbooks() or []
        matrix = []

        for wb in workbooks:
            wb_id = wb.get('id', '')
            wb_name = wb.get('name', '')
            try:
                perms = self.get_permissions(wb_id)
            except Exception:
                perms = []
            matrix.append({
                'id': wb_id,
                'name': wb_name,
                'project': wb.get('project', {}).get('name', ''),
                'permissions': perms,
            })

        return {'workbooks': matrix}

    def get_all_subscriptions(self):
        """Get all subscriptions site-wide (paginated).

        Returns:
            list[dict]: All subscription metadata.
        """
        url = f'{self.site_url}/subscriptions'
        return self._paginated_get(url, 'subscriptions', 'subscription')

    def list_data_alerts(self):
        """List all data-driven alerts on the site (Server 2018.3+).

        Returns:
            list[dict]: Alert metadata, or empty list if unsupported.
        """
        url = f'{self.site_url}/dataAlerts'
        try:
            resp = self._request('GET', url)
            alerts = resp.get('dataAlerts', {}).get('dataAlert', [])
            if isinstance(alerts, dict):
                alerts = [alerts]
            return alerts
        except Exception as e:
            logger.warning("Data alerts API not available: %s", e)
            return []

    def download_datasource_by_name(self, name, output_path):
        """Download a published datasource by name.

        Args:
            name: Datasource name (case-insensitive match).
            output_path: Local file path.

        Returns:
            str or None: Path to downloaded file, or None if not found.
        """
        datasources = self.list_datasources() or []
        match = None
        name_lower = name.lower()
        norm_name = _normalize_name(name)
        for ds in datasources:
            ds_name = ds.get('name', '')
            if ds_name == name or ds_name.lower() == name_lower:
                match = ds
                break
        # Accent-insensitive fallback
        if not match:
            for ds in datasources:
                if _normalize_name(ds.get('name', '')) == norm_name:
                    match = ds
                    break

        if not match:
            logger.warning("Published datasource '%s' not found", name)
            return None

        return self.download_datasource(match['id'], output_path)

    def get_site_topology(self):
        """Get comprehensive site topology for migration planning.

        Returns:
            dict: {site_info, projects, workbooks, datasources, users, groups,
                   schedules, prep_flows}
        """
        return {
            'site_info': self.get_site_info(),
            'projects': self.list_projects() or [],
            'workbooks': self.list_workbooks() or [],
            'datasources': self.list_datasources() or [],
            'users': self.list_users() or [],
            'groups': self.list_groups() or [],
            'schedules': self.list_schedules() or [],
            'prep_flows': self.list_prep_flows() or [],
        }

    def get_lineage_upstream(self, workbook_id):
        """Get upstream lineage for a workbook using Metadata API.

        Returns tables, databases, and datasources that feed this workbook.

        Args:
            workbook_id: Workbook LUID.

        Returns:
            dict: {databases: [...], tables: [...], datasources: [...]}
        """
        query = '''
        query GetWorkbookLineage($id: String!) {
            workbooks(filter: {luid: $id}) {
                upstreamDatasources {
                    name
                    id
                    upstreamTables {
                        name
                        fullName
                        database { name connectionType }
                    }
                }
                upstreamTables {
                    name
                    fullName
                    database { name connectionType }
                }
            }
        }'''
        data = self.get_metadata_graphql(query, {'id': workbook_id})
        workbooks = data.get('workbooks', [])
        if not workbooks:
            return {'databases': [], 'tables': [], 'datasources': []}

        wb = workbooks[0]
        tables = wb.get('upstreamTables', [])
        datasources = wb.get('upstreamDatasources', [])

        databases = list({
            t.get('database', {}).get('name', '')
            for t in tables if t.get('database')
        })

        return {
            'databases': databases,
            'tables': tables,
            'datasources': datasources,
        }

