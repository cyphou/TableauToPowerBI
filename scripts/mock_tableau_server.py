"""
Mock Tableau Server — lightweight HTTP server for local testing.

Serves workbooks and prep flows from a local directory as if they were
hosted on Tableau Server REST API. Supports sign-in, list, download.

Usage:
    python scripts/mock_tableau_server.py --port 8765 --content-dir examples/tableau_samples

Then connect the migration tool:
    python migrate.py --server http://localhost:8765 --token-name test --token-secret test --server-batch Default
"""

import argparse
import glob
import http.server
import json
import os
import re
import threading
import uuid
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

DEFAULT_PORT = 8765
API_VERSION = '3.21'


class MockTableauHandler(http.server.BaseHTTPRequestHandler):
    """Handles Tableau REST API requests with local file content."""

    # Class-level references (set by serve())
    content_dir: str = ''
    site_luid: str = 'mock-site-0001'
    auth_tokens: dict = {}  # token → expiry (not enforced)

    def log_message(self, format, *args):
        """Suppress default logging; use our own."""
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, file_path):
        if not os.path.isfile(file_path):
            self._send_json({'error': {'code': '404', 'summary': 'Not Found'}}, 404)
            return
        with open(file_path, 'rb') as f:
            content = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(len(content)))
        fname = os.path.basename(file_path)
        self.send_header('Content-Disposition', f'attachment; filename="{fname}"')
        self.end_headers()
        self.wfile.write(content)

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def _get_workbooks(self):
        """Scan content_dir for .twb/.twbx files."""
        files = []
        for ext in ('*.twb', '*.twbx'):
            files.extend(glob.glob(os.path.join(self.content_dir, '**', ext), recursive=True))
        workbooks = []
        for f in sorted(files):
            name = Path(f).stem
            wb_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f))
            workbooks.append({
                'id': wb_id,
                'name': name,
                'contentUrl': name.replace(' ', '_'),
                'project': {'id': 'proj-001', 'name': 'Default'},
                'owner': {'id': 'user-001', 'name': 'mock-admin'},
                'createdAt': '2026-01-01T00:00:00Z',
                'updatedAt': '2026-05-01T00:00:00Z',
                '_local_path': f,  # internal use
            })
        return workbooks

    def _get_prep_flows(self):
        """Scan content_dir for .tfl/.tflx files."""
        files = []
        for ext in ('*.tfl', '*.tflx'):
            files.extend(glob.glob(os.path.join(self.content_dir, '**', ext), recursive=True))
        flows = []
        for f in sorted(files):
            name = Path(f).stem
            flow_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f))
            flows.append({
                'id': flow_id,
                'name': name,
                'project': {'id': 'proj-001', 'name': 'Default'},
                'owner': {'id': 'user-001', 'name': 'mock-admin'},
                'createdAt': '2026-01-01T00:00:00Z',
                'updatedAt': '2026-05-01T00:00:00Z',
                '_local_path': f,
            })
        return flows

    def _find_item_by_id(self, items, item_id):
        for it in items:
            if it['id'] == item_id:
                return it
        return None

    # ── Route dispatcher ──────────────────────────────────────────────────────

    def do_POST(self):
        path = self.path
        # Sign-in
        if re.match(rf'/api/{API_VERSION}/auth/signin', path):
            body = self._read_body()
            token = str(uuid.uuid4())
            self.auth_tokens[token] = True
            self._send_json({
                'credentials': {
                    'token': token,
                    'site': {'id': self.site_luid, 'contentUrl': ''},
                    'user': {'id': 'user-001', 'name': 'mock-admin'},
                }
            })
            print(f"  [MOCK] Sign-in OK → token={token[:8]}...")
            return

        # Sign-out
        if re.match(rf'/api/{API_VERSION}/auth/signout', path):
            self._send_json({})
            return

        self._send_json({'error': {'code': '404', 'summary': f'Unknown POST: {path}'}}, 404)

    def do_GET(self):
        path = self.path
        site = self.site_luid

        # Server info (no auth)
        if re.match(rf'/api/{API_VERSION}/serverinfo', path):
            self._send_json({
                'serverInfo': {
                    'productVersion': {'value': '2024.3.0', 'build': '20243.26.0101.0001'},
                    'restApiVersion': API_VERSION,
                    'platform': 'mock',
                }
            })
            return

        # Site info
        if re.match(rf'/api/{API_VERSION}/sites/{site}$', path):
            self._send_json({
                'site': {
                    'id': site,
                    'name': 'Mock Site',
                    'contentUrl': '',
                    'state': 'Active',
                }
            })
            return

        # List workbooks (paginated)
        m = re.match(rf'/api/{API_VERSION}/sites/{site}/workbooks(\?.*)?', path)
        if m and '/content' not in path:
            workbooks = self._get_workbooks()
            # Strip internal path
            clean = [{k: v for k, v in wb.items() if k != '_local_path'} for wb in workbooks]
            self._send_json({
                'pagination': {
                    'pageNumber': '1',
                    'pageSize': '100',
                    'totalAvailable': str(len(clean)),
                },
                'workbooks': {'workbook': clean},
            })
            return

        # Download workbook
        m = re.match(rf'/api/{API_VERSION}/sites/{site}/workbooks/([^/]+)/content', path)
        if m:
            wb_id = m.group(1)
            workbooks = self._get_workbooks()
            wb = self._find_item_by_id(workbooks, wb_id)
            if wb:
                self._send_file(wb['_local_path'])
                print(f"  [MOCK] Download workbook: {wb['name']}")
            else:
                self._send_json({'error': {'code': '404', 'summary': 'Workbook not found'}}, 404)
            return

        # List flows (paginated)
        m = re.match(rf'/api/{API_VERSION}/sites/{site}/flows(\?.*)?', path)
        if m and '/content' not in path:
            flows = self._get_prep_flows()
            clean = [{k: v for k, v in fl.items() if k != '_local_path'} for fl in flows]
            self._send_json({
                'pagination': {
                    'pageNumber': '1',
                    'pageSize': '100',
                    'totalAvailable': str(len(clean)),
                },
                'flows': {'flow': clean},
            })
            return

        # Download flow
        m = re.match(rf'/api/{API_VERSION}/sites/{site}/flows/([^/]+)/content', path)
        if m:
            flow_id = m.group(1)
            flows = self._get_prep_flows()
            fl = self._find_item_by_id(flows, flow_id)
            if fl:
                self._send_file(fl['_local_path'])
                print(f"  [MOCK] Download flow: {fl['name']}")
            else:
                self._send_json({'error': {'code': '404', 'summary': 'Flow not found'}}, 404)
            return

        # List datasources
        m = re.match(rf'/api/{API_VERSION}/sites/{site}/datasources(\?.*)?', path)
        if m:
            self._send_json({
                'pagination': {'pageNumber': '1', 'pageSize': '100', 'totalAvailable': '0'},
                'datasources': {'datasource': []},
            })
            return

        # List users
        m = re.match(rf'/api/{API_VERSION}/sites/{site}/users(\?.*)?', path)
        if m:
            self._send_json({
                'pagination': {'pageNumber': '1', 'pageSize': '100', 'totalAvailable': '1'},
                'users': {'user': [{'id': 'user-001', 'name': 'mock-admin', 'siteRole': 'SiteAdministratorCreator'}]},
            })
            return

        # List groups
        m = re.match(rf'/api/{API_VERSION}/sites/{site}/groups(\?.*)?', path)
        if m:
            self._send_json({
                'pagination': {'pageNumber': '1', 'pageSize': '100', 'totalAvailable': '0'},
                'groups': {'group': []},
            })
            return

        # List projects
        m = re.match(rf'/api/{API_VERSION}/sites/{site}/projects(\?.*)?', path)
        if m:
            self._send_json({
                'pagination': {'pageNumber': '1', 'pageSize': '100', 'totalAvailable': '1'},
                'projects': {'project': [{'id': 'proj-001', 'name': 'Default'}]},
            })
            return

        # List schedules
        m = re.match(rf'/api/{API_VERSION}/schedules(\?.*)?', path)
        if m:
            self._send_json({
                'pagination': {'pageNumber': '1', 'pageSize': '100', 'totalAvailable': '0'},
                'schedules': {'schedule': []},
            })
            return

        # List views
        m = re.match(rf'/api/{API_VERSION}/sites/{site}/views(\?.*)?', path)
        if m:
            self._send_json({
                'pagination': {'pageNumber': '1', 'pageSize': '100', 'totalAvailable': '0'},
                'views': {'view': []},
            })
            return

        # Fallback
        self._send_json({'error': {'code': '404', 'summary': f'Unknown GET: {path}'}}, 404)

    def do_PUT(self):
        self._send_json({})


def serve(port=DEFAULT_PORT, content_dir='.'):
    """Start the mock Tableau Server."""
    content_dir = os.path.abspath(content_dir)
    MockTableauHandler.content_dir = content_dir

    workbooks = MockTableauHandler(None, None, None)._get_workbooks() if False else []
    # Count content
    wb_count = len(glob.glob(os.path.join(content_dir, '**', '*.twb'), recursive=True)) + \
               len(glob.glob(os.path.join(content_dir, '**', '*.twbx'), recursive=True))
    fl_count = len(glob.glob(os.path.join(content_dir, '**', '*.tfl'), recursive=True)) + \
               len(glob.glob(os.path.join(content_dir, '**', '*.tflx'), recursive=True))

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║           MOCK TABLEAU SERVER — v{API_VERSION}                      ║
╠══════════════════════════════════════════════════════════════╣
║  URL:          http://localhost:{port}                        ║
║  Content dir:  {content_dir[:45]:<45}║
║  Workbooks:    {wb_count:<45}║
║  Prep flows:   {fl_count:<45}║
╠══════════════════════════════════════════════════════════════╣
║  Auth: any token_name / token_secret accepted               ║
║                                                              ║
║  Usage:                                                      ║
║    python migrate.py --server http://localhost:{port} \\        ║
║      --token-name test --token-secret test \\                 ║
║      --server-batch Default                                  ║
╚══════════════════════════════════════════════════════════════╝
""")

    server = http.server.HTTPServer(('0.0.0.0', port), MockTableauHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  [MOCK] Server stopped.")
        server.shutdown()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Mock Tableau Server for testing')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f'Port to listen on (default: {DEFAULT_PORT})')
    parser.add_argument('--content-dir', default='examples/tableau_samples',
                        help='Directory containing .twb/.twbx/.tfl/.tflx files')
    args = parser.parse_args()
    serve(port=args.port, content_dir=args.content_dir)
