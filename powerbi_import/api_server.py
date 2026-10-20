"""REST API server for Tableau-to-Power BI migration.

Sprint 110: Lightweight HTTP wrapper around the migration pipeline.
Sprint 176: API v2 — OpenAPI spec, pagination, webhooks, API key auth, batch.

Endpoints:
    POST /migrate          Retired (HTTP 410); use /migrate/batch
    POST /migrate/batch    Upload multiple files as ZIP → returns batch ID
    GET  /status/{id}      Check migration job status
    GET  /download/{id}    Download generated .pbip project as ZIP
    GET  /health           Health check
    GET  /jobs             List jobs (paginated, filterable)
    GET  /batch/{id}       Check batch migration status
    GET  /openapi.json     OpenAPI 3.0 specification

Uses Python stdlib ``http.server`` — zero external dependencies.

Usage:
    python -m powerbi_import.api_server --port 8000
    python -m powerbi_import.api_server --host 0.0.0.0 --port 8080
    python -m powerbi_import.api_server --api-key MY_SECRET_KEY
"""

import argparse
import collections
import hashlib
import hmac
import io
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
import ipaddress
import socket
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

# Add paths for project imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))

# ── Job store ─────────────────────────────────────────────────────────────────

_jobs = {}  # job_id -> {status, created, input_path, output_dir, error, stats}
_lock = threading.Lock()

MAX_UPLOAD_SIZE = 500 * 1024 * 1024  # 500 MB
MAX_CONCURRENT_JOBS = 50
MAX_BATCH_ENTRIES = 100
MAX_BATCH_EXPANDED_SIZE = 2 * 1024 * 1024 * 1024
JOB_TTL_SECONDS = 24 * 3600  # 24 hours

# Rate limiting: max requests per IP within a sliding window
_RATE_LIMIT_MAX = 10  # max jobs per IP per window
_RATE_LIMIT_WINDOW = 60  # seconds
_rate_tracker = {}  # ip -> deque of timestamps
_rate_lock = threading.Lock()

# Sprint 176: API key authentication (set via --api-key)
_API_KEY: str | None = None

# Sprint 176: Batch job store
_batches = {}  # batch_id -> {status, created, job_ids, completed, failed, total}
_batch_lock = threading.Lock()

# Sprint 176: Webhook signing secret (derived from API key)
_WEBHOOK_SECRET: str | None = None


def _confined_path(path, root):
    """Resolve a server-owned path, rejecting escapes and all reparse points."""
    from migrate import _confine_upload_path
    return _confine_upload_path(path, root)


def _uploaded_filename(name):
    """Use a portable non-special basename, never a client-supplied path."""
    import ntpath
    if not isinstance(name, str) or '\0' in name:
        raise ValueError("Invalid uploaded filename")
    filename = ntpath.basename(name)
    stem, extension = os.path.splitext(filename)
    if extension.lower() not in ('.twb', '.twbx', '.tds', '.tdsx'):
        raise ValueError("Unsupported uploaded filename")
    stem = re.sub(r'[^A-Za-z0-9_-]', '_', stem)[:100] or 'upload'
    if stem.upper() in {'CON', 'PRN', 'AUX', 'NUL', 'CLOCK$',
                        *('COM' + str(i) for i in range(10)),
                        *('LPT' + str(i) for i in range(10))}:
        stem = 'upload_' + stem
    return stem + extension.lower()


def _write_output_archive(source_dir, destination, job_dir):
    """Archive only regular files inside the recorded private job."""
    source_dir = _confined_path(source_dir, job_dir)
    if isinstance(destination, (str, bytes, os.PathLike)):
        destination = _confined_path(destination, job_dir)
    if not os.path.isdir(source_dir):
        raise ValueError("Output directory not found")

    def walk_error(error):
        raise error

    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for current, dirs, files in os.walk(source_dir, onerror=walk_error):
            _confined_path(current, source_dir)
            for name in dirs + files:
                _confined_path(os.path.join(current, name), source_dir)
            for name in files:
                full = _confined_path(os.path.join(current, name), source_dir)
                _confined_path(full, job_dir)
                if not stat.S_ISREG(os.stat(full, follow_symlinks=False).st_mode):
                    raise ValueError("Only regular output files may be downloaded")
                archive.write(full, os.path.relpath(full, source_dir))


def _run_upload_worker(input_path, upload_root, output_dir, options=None,
                       assess=False):
    """Run the batch-only CLI in a private process, preserving REST options."""
    options = options or {}
    input_path = _confined_path(input_path, upload_root)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    source_dir = input_path if os.path.isdir(input_path) else os.path.dirname(input_path)
    source_args = [input_path] if assess and os.path.isfile(input_path) else ['--batch', source_dir]
    command = [sys.executable, os.path.join(root, 'migrate.py'),
               *source_args, '--output-dir', output_dir]
    mode = options.get('model_mode', options.get('mode', 'import'))
    if mode not in ('import', 'directquery', 'composite'):
        raise ValueError("model_mode must be import, directquery, or composite")
    command.extend(['--mode', mode])
    for option, flag in (
        ('calendar_start', '--calendar-start'), ('calendar_end', '--calendar-end'),
        ('culture', '--culture'), ('output_format', '--output-format'),
        ('time_intelligence', '--time-intelligence'),
    ):
        if options.get(option) is not None:
            command.extend([flag, str(options[option])])
    if options.get('optimize_dax'):
        command.append('--optimize-dax')
    if assess:
        command.append('--assess')
    env = os.environ.copy()
    env['TTPBI_UPLOAD_ROOT'] = os.path.realpath(upload_root)
    env['PYTHONIOENCODING'] = 'utf-8'
    with tempfile.TemporaryDirectory(prefix='extraction_', dir=output_dir) as extraction:
        env['TTPBI_EXTRACT_DIR'] = extraction
        completed = subprocess.run(
            command, cwd=root, env=env, capture_output=True, text=True,
            encoding='utf-8', errors='replace', check=False, timeout=3600,
        )
    if completed.returncode:
        detail = (completed.stderr + '\n' + completed.stdout)[-4000:].strip()
        raise RuntimeError(
            f"Confined upload migration failed (exit {completed.returncode}): {detail}")
    return completed


def _migration_options(params):
    """Validate REST options before allocating any upload or job."""
    unknown = set(params) - {'calendar_start', 'calendar_end', 'culture',
                             'model_mode', 'webhook_url'}
    if unknown:
        raise ValueError('Unsupported migration option(s): ' + ', '.join(sorted(unknown)))
    options = {}
    for key in ('calendar_start', 'calendar_end'):
        value = _int_param(params, key)
        if key in params and (value is None or not 1900 <= value <= 2100):
            raise ValueError(f'{key} must be between 1900 and 2100')
        options[key] = value
    culture = params.get('culture', [None])[0]
    if culture and not re.fullmatch(r'[a-zA-Z]{2}(-[a-zA-Z]{2,4})?', culture):
        raise ValueError('Invalid culture format (expected e.g. en-US)')
    options['culture'] = culture
    mode = params.get('model_mode', ['import'])[0]
    if mode not in ('import', 'directquery', 'composite'):
        raise ValueError('model_mode must be import, directquery, or composite')
    options['model_mode'] = mode
    webhook_url = params.get('webhook_url', [None])[0]
    if webhook_url:
        if not webhook_url.startswith(('http://', 'https://')):
            raise ValueError('webhook_url must start with http:// or https://')
        options['webhook_url'] = webhook_url
    return options


def _purge_stale_jobs():
    """Remove completed/failed jobs older than JOB_TTL_SECONDS."""
    cutoff = time.time() - JOB_TTL_SECONDS
    stale = [jid for jid, j in _jobs.items()
             if j['status'] in ('completed', 'failed') and j['created'] < cutoff]
    for jid in stale:
        job = _jobs.pop(jid)
        for directory in (job.get('job_dir'), job.get('upload_root')):
            if directory and os.path.isdir(directory):
                shutil.rmtree(directory, ignore_errors=True)


def _check_rate_limit(client_ip):
    """Return True if the client IP is within rate limits."""
    now = time.time()
    with _rate_lock:
        if client_ip not in _rate_tracker:
            _rate_tracker[client_ip] = collections.deque()
        dq = _rate_tracker[client_ip]
        # Purge old entries
        while dq and dq[0] < now - _RATE_LIMIT_WINDOW:
            dq.popleft()
        if len(dq) >= _RATE_LIMIT_MAX:
            return False
        dq.append(now)
        return True


def _new_job(input_path, upload_root=None):
    job_id = uuid.uuid4().hex[:12]
    with _lock:
        _purge_stale_jobs()
        _jobs[job_id] = {
            'status': 'queued',
            'created': time.time(),
            'input_path': input_path,
            'upload_root': upload_root,
            'job_dir': None,
            'output_dir': None,
            'error': None,
            'stats': None,
        }
    return job_id


def _get_job(job_id):
    with _lock:
        return _jobs.get(job_id)


def _update_job(job_id, **kwargs):
    with _lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


# ── Migration worker ──────────────────────────────────────────────────────────

def _run_migration(job_id, input_path, options=None):
    """Run migration in a background thread."""
    options = options or {}
    _update_job(job_id, status='running')

    try:
        job = _get_job(job_id)
        if not job or not job.get('upload_root'):
            raise ValueError("Missing server-owned private upload directory")
        upload_root = job['upload_root']
        job_dir = os.path.realpath(tempfile.mkdtemp(prefix=f'pbi_api_{job_id}_'))
        output_dir = os.path.join(job_dir, 'output')
        os.mkdir(output_dir)
        _update_job(job_id, job_dir=job_dir, output_dir=output_dir)
        _run_upload_worker(input_path, upload_root, output_dir, options)
        if not os.listdir(output_dir):
            raise RuntimeError("Migration produced no output artifacts")
        report_name = os.path.splitext(os.path.basename(input_path))[0]

        _update_job(job_id, status='completed', stats={
            'report_name': report_name,
            'output_dir': output_dir,
        })

    except Exception as exc:
        logger.exception("Migration failed for job %s", job_id)
        _update_job(job_id, status='failed', error=str(exc))
    finally:
        job = _get_job(job_id)
        if job and job.get('upload_root'):
            shutil.rmtree(job['upload_root'], ignore_errors=True)

    # Sprint 176: Fire webhook if configured
    webhook_url = (options or {}).get('webhook_url')
    if webhook_url:
        _fire_webhook(job_id, webhook_url)

    # Sprint 176: Update batch status if this job belongs to a batch
    batch_id = (options or {}).get('batch_id')
    if batch_id:
        _update_batch_progress(batch_id, job_id)


# ── Sprint 176: Webhook delivery ─────────────────────────────────────────────

def _fire_webhook(job_id: str, url: str) -> None:
    """POST job result to the configured webhook URL with HMAC signature."""
    job = _get_job(job_id)
    if not job:
        return
    payload = json.dumps({
        'event': 'migration.completed' if job['status'] == 'completed' else 'migration.failed',
        'job_id': job_id,
        'status': job['status'],
        'error': job['error'],
        'timestamp': time.time(),
    }).encode('utf-8')

    # HMAC-SHA256 signature
    secret = (_WEBHOOK_SECRET or 'unsigned').encode('utf-8')
    signature = hmac.new(secret, payload, hashlib.sha256).hexdigest()

    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('webhook URL must be HTTP(S) with a hostname')
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError('webhook destination resolves to a private/reserved address')
        import urllib.request
        class NoWebhookRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise ValueError("Webhook redirects are not allowed")

        req = urllib.request.Request(
            url, data=payload,
            headers={
                'Content-Type': 'application/json',
                'X-Signature-SHA256': signature,
                'X-Job-ID': job_id,
            },
            method='POST',
        )
        opener = urllib.request.build_opener(NoWebhookRedirect())
        with opener.open(req, timeout=10) as resp:
            logger.info("Webhook delivered to %s — HTTP %s", url, resp.status)
    except Exception as exc:
        logger.warning("Webhook delivery to %s failed: %s", url, exc)


def _update_batch_progress(batch_id: str, job_id: str) -> None:
    """Update batch progress when a child job completes."""
    job = _get_job(job_id)
    if not job:
        return
    with _batch_lock:
        batch = _batches.get(batch_id)
        if not batch:
            return
        if job['status'] == 'completed':
            batch['completed'] += 1
        elif job['status'] == 'failed':
            batch['failed'] += 1
        done = batch['completed'] + batch['failed']
        if done >= batch['total']:
            batch['status'] = 'completed'


# ── Sprint 176: OpenAPI specification ─────────────────────────────────────────

def _build_openapi_spec() -> dict:
    """Return an OpenAPI 3.0 spec dict."""
    return {
        'openapi': '3.0.3',
        'info': {
            'title': 'Tableau to Power BI Migration API',
            'version': _get_version(),
            'description': 'REST API for automated Tableau-to-Power BI migration.',
        },
        'paths': {
            '/health': {
                'get': {
                    'summary': 'Health check',
                    'responses': {'200': {'description': 'API status and version'}},
                },
            },
            '/migrate': {
                'post': {
                    'summary': 'Retired single-file migration endpoint',
                    'description': 'Single-file generation has been retired. Use POST /migrate/batch with a ZIP containing one or more Tableau files.',
                    'deprecated': True,
                    'responses': {
                        '410': {'description': 'Gone — use POST /migrate/batch with a ZIP of Tableau files'},
                        '401': {'description': 'Unauthorized'},
                    },
                },
            },
            '/migrate/batch': {
                'post': {
                    'summary': 'Submit batch migration (ZIP of workbooks)',
                    'requestBody': {
                        'content': {
                            'multipart/form-data': {
                                'schema': {
                                    'type': 'object',
                                    'properties': {
                                        'file': {'type': 'string', 'format': 'binary'},
                                    },
                                },
                            },
                        },
                    },
                    'responses': {
                        '202': {'description': 'Batch created'},
                        '400': {'description': 'No valid workbooks in ZIP'},
                        '401': {'description': 'Unauthorized'},
                    },
                },
            },
            '/status/{id}': {
                'get': {
                    'summary': 'Get job status',
                    'parameters': [{'name': 'id', 'in': 'path', 'required': True,
                                    'schema': {'type': 'string'}}],
                    'responses': {
                        '200': {'description': 'Job status'},
                        '404': {'description': 'Job not found'},
                    },
                },
            },
            '/download/{id}': {
                'get': {
                    'summary': 'Download migration output as ZIP',
                    'parameters': [{'name': 'id', 'in': 'path', 'required': True,
                                    'schema': {'type': 'string'}}],
                    'responses': {
                        '200': {'description': 'ZIP file download'},
                        '400': {'description': 'Job not completed'},
                        '404': {'description': 'Job not found'},
                    },
                },
            },
            '/jobs': {
                'get': {
                    'summary': 'List jobs with pagination and filtering',
                    'parameters': [
                        {'name': 'status', 'in': 'query', 'schema': {'type': 'string'}},
                        {'name': 'page', 'in': 'query', 'schema': {'type': 'integer', 'default': 1}},
                        {'name': 'per_page', 'in': 'query', 'schema': {'type': 'integer', 'default': 20}},
                    ],
                    'responses': {'200': {'description': 'Paginated job list'}},
                },
            },
            '/batch/{id}': {
                'get': {
                    'summary': 'Get batch migration status',
                    'parameters': [{'name': 'id', 'in': 'path', 'required': True,
                                    'schema': {'type': 'string'}}],
                    'responses': {
                        '200': {'description': 'Batch status'},
                        '404': {'description': 'Batch not found'},
                    },
                },
            },
            '/openapi.json': {
                'get': {
                    'summary': 'OpenAPI specification',
                    'responses': {'200': {'description': 'OpenAPI 3.0 JSON spec'}},
                },
            },
        },
    }


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class MigrationHandler(BaseHTTPRequestHandler):
    """HTTP request handler for migration API."""

    def _send_json(self, data, status=200):
        body = json.dumps(data, indent=2).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, message):
        self._send_json({'error': message}, status=status)

    def _drain_request_body(self):
        """Discard an unread request body before replying.

        Closing the connection while the client's body is still unread makes the
        stack send RST, which discards the response we already wrote — the client
        sees a reset instead of our status code. Only call this on paths that
        never read the body.
        """
        try:
            remaining = int(self.headers.get('Content-Length', 0) or 0)
        except (TypeError, ValueError):
            return
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _get_path(self):
        parsed = urlparse(self.path)
        return parsed.path.rstrip('/')

    def _get_query_params(self):
        """Parse query string parameters."""
        parsed = urlparse(self.path)
        return parse_qs(parsed.query)

    def _check_auth(self) -> bool:
        """Validate API key if configured. Returns True if authorized."""
        if not _API_KEY:
            client_ip = self.client_address[0] if self.client_address else ''
            return client_ip in ('127.0.0.1', '::1', 'localhost')
        auth_header = self.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:].strip()
            return hmac.compare_digest(token, _API_KEY)
        return False

    def do_GET(self):
        path = self._get_path()

        # Public endpoints (no auth required)
        # GET /health
        if path == '/health':
            self._send_json({'status': 'ok', 'version': _get_version()})
            return

        # GET /openapi.json — Sprint 176
        if path == '/openapi.json':
            self._send_json(_build_openapi_spec())
            return

        # All other endpoints require auth
        if not self._check_auth():
            self._send_error(401, 'Unauthorized — provide Authorization: Bearer <api-key>')
            return

        # GET /metrics — Sprint 131.4 OpenMetrics scrape endpoint
        if path == '/metrics':
            try:
                from powerbi_import import telemetry as _tel_mod
                from powerbi_import.monitoring import telemetry_to_openmetrics
                collector = getattr(_tel_mod, '_GLOBAL_COLLECTOR', None)
                body = telemetry_to_openmetrics(collector).encode('utf-8')
                self.send_response(200)
                self.send_header(
                    'Content-Type',
                    'application/openmetrics-text; version=1.0.0; charset=utf-8'
                )
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:  # never crash the server
                self._send_error(500, f'metrics export failed: {exc}')
            return

        # GET /status/{id}
        if path.startswith('/status/'):
            job_id = path.split('/status/')[-1]
            job = _get_job(job_id)
            if not job:
                self._send_error(404, f'Job {job_id} not found')
                return
            self._send_json({
                'job_id': job_id,
                'status': job['status'],
                'error': job['error'],
                'created': job['created'],
            })
            return

        # GET /download/{id}
        if path.startswith('/download/'):
            job_id = path.split('/download/')[-1]
            job = _get_job(job_id)
            if not job:
                self._send_error(404, f'Job {job_id} not found')
                return
            if job['status'] != 'completed':
                self._send_error(400, f'Job {job_id} is {job["status"]}, not completed')
                return
            output_dir = job.get('output_dir')
            if not output_dir or not os.path.isdir(output_dir):
                self._send_error(500, 'Output directory not found')
                return

            # Recheck real paths at the final download boundary, too.
            try:
                buf = io.BytesIO()
                if not job.get('job_dir'):
                    raise ValueError("Missing recorded private job directory")
                _write_output_archive(output_dir, buf, job['job_dir'])
            except (ValueError, OSError) as exc:
                self._send_error(400, f'Unsafe download output: {exc}')
                return
            zip_bytes = buf.getvalue()

            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Disposition',
                             f'attachment; filename="{job_id}.zip"')
            self.send_header('Content-Length', str(len(zip_bytes)))
            self.end_headers()
            self.wfile.write(zip_bytes)
            return

        # GET /jobs — Sprint 176: pagination & filtering
        if path == '/jobs':
            params = self._get_query_params()
            status_filter = params.get('status', [None])[0]
            try:
                page = max(1, int(params.get('page', ['1'])[0]))
            except (ValueError, IndexError):
                page = 1
            try:
                per_page = min(100, max(1, int(params.get('per_page', ['20'])[0])))
            except (ValueError, IndexError):
                per_page = 20

            with _lock:
                all_jobs = [
                    {'job_id': jid, 'status': j['status'], 'created': j['created']}
                    for jid, j in _jobs.items()
                ]
            # Filter
            if status_filter:
                all_jobs = [j for j in all_jobs if j['status'] == status_filter]
            # Sort by created descending
            all_jobs.sort(key=lambda j: j['created'], reverse=True)
            total = len(all_jobs)
            total_pages = max(1, (total + per_page - 1) // per_page)
            start = (page - 1) * per_page
            page_jobs = all_jobs[start:start + per_page]

            self._send_json({
                'jobs': page_jobs,
                'page': page,
                'per_page': per_page,
                'total': total,
                'total_pages': total_pages,
            })
            return

        # GET /batch/{id} — Sprint 176
        if path.startswith('/batch/'):
            batch_id = path.split('/batch/')[-1]
            with _batch_lock:
                batch = _batches.get(batch_id)
            if not batch:
                self._send_error(404, f'Batch {batch_id} not found')
                return
            # Collect per-job status
            job_statuses = []
            for jid in batch.get('job_ids', []):
                job = _get_job(jid)
                if job:
                    job_statuses.append({
                        'job_id': jid,
                        'status': job['status'],
                        'error': job['error'],
                    })
            self._send_json({
                'batch_id': batch_id,
                'status': batch['status'],
                'total': batch['total'],
                'completed': batch['completed'],
                'failed': batch['failed'],
                'jobs': job_statuses,
            })
            return

        self._send_error(404, 'Not found')

    def do_POST(self):
        path = self._get_path()

        # Auth check for POST endpoints
        if not self._check_auth():
            self._send_error(401, 'Unauthorized — provide Authorization: Bearer <api-key>')
            return

        # Retired endpoint: authenticate above, but never create a job or file.
        if path == '/migrate':
            self._drain_request_body()
            self._send_error(
                410,
                'Single-file project generation has been retired. '
                'Use POST /migrate/batch with a ZIP containing one or more '
                'Tableau files (.twb/.twbx/.tds/.tdsx).',
            )
            return

        # POST /migrate/batch — Sprint 176
        if path == '/migrate/batch':
            try:
                batch_options = _migration_options(self._get_query_params())
                content_length = int(self.headers.get('Content-Length', 0))
                if content_length < 0:
                    raise ValueError('Invalid Content-Length')
            except (TypeError, ValueError) as exc:
                self._send_error(400, str(exc))
                return
            if content_length > MAX_UPLOAD_SIZE:
                self._send_error(413, 'File too large (max 500 MB)')
                return
            if content_length == 0:
                self._send_error(400, 'No file uploaded')
                return

            body = self.rfile.read(content_length)
            content_type = self.headers.get('Content-Type', '')

            # Accept multipart with a ZIP file
            file_data = body
            if 'multipart/form-data' in content_type:
                parts = content_type.split('boundary=')
                if len(parts) < 2:
                    self._send_error(400, 'Invalid multipart boundary')
                    return
                boundary = parts[1].strip().encode()
                parsed = _parse_multipart(body, boundary)
                if not parsed:
                    self._send_error(400, 'No file found in multipart data')
                    return
                _, file_data = parsed

            # Expect a ZIP containing workbooks
            try:
                zf = zipfile.ZipFile(io.BytesIO(file_data))
            except zipfile.BadZipFile:
                self._send_error(400, 'Uploaded file is not a valid ZIP archive')
                return

            # Extract valid workbook files
            valid_ext = ('.twb', '.twbx', '.tds', '.tdsx')
            workbook_names = [
                n for n in zf.namelist()
                if os.path.splitext(n)[1].lower() in valid_ext
                and not n.startswith('__MACOSX')
                and not os.path.basename(n).startswith('.')
            ]
            if any(stat.S_ISLNK(zf.getinfo(name).external_attr >> 16)
                   for name in workbook_names):
                self._send_error(400, 'Symlink workbook entries are not allowed')
                zf.close()
                return
            if not workbook_names:
                self._send_error(400, 'No valid workbooks (.twb/.twbx) found in ZIP')
                zf.close()
                return
            if len(workbook_names) > MAX_BATCH_ENTRIES:
                self._send_error(413, f'Too many workbooks in ZIP (max {MAX_BATCH_ENTRIES})')
                zf.close()
                return
            expanded_size = sum(zf.getinfo(name).file_size for name in workbook_names)
            if expanded_size > MAX_BATCH_EXPANDED_SIZE:
                self._send_error(413, 'Expanded ZIP content exceeds batch limit')
                zf.close()
                return
            with _lock:
                active = sum(1 for job in _jobs.values()
                             if job['status'] in ('queued', 'running'))
            if active + len(workbook_names) > MAX_CONCURRENT_JOBS:
                self._send_error(429, 'Batch exceeds concurrent job capacity')
                zf.close()
                return

            # Rate limit
            client_ip = self.client_address[0] if self.client_address else 'unknown'
            if not _check_rate_limit(client_ip):
                self._send_error(429, 'Too many requests. Please try again later.')
                zf.close()
                return

            # Create batch
            batch_id = uuid.uuid4().hex[:12]
            job_ids = []
            with _batch_lock:
                _batches[batch_id] = {
                    'status': 'running', 'created': time.time(), 'job_ids': [],
                    'completed': 0, 'failed': 0, 'total': len(workbook_names),
                }

            for wb_name in workbook_names:
                upload_root = os.path.realpath(tempfile.mkdtemp(prefix='pbi_upload_'))
                try:
                    filename = _uploaded_filename(wb_name)
                    input_path = _confined_path(os.path.join(upload_root, filename),
                                                upload_root)
                    with zf.open(wb_name) as source, open(input_path, 'xb') as uploaded:
                        shutil.copyfileobj(source, uploaded)
                except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
                    shutil.rmtree(upload_root, ignore_errors=True)
                    with _batch_lock:
                        _batches[batch_id]['status'] = 'failed'
                    zf.close()
                    self._send_error(400, f'Unable to save batch workbook: {exc}')
                    return

                jid = _new_job(input_path, upload_root=upload_root)
                job_ids.append(jid)
                with _batch_lock:
                    _batches[batch_id]['job_ids'].append(jid)
                options = {**batch_options, 'batch_id': batch_id}
                thread = threading.Thread(
                    target=_run_migration,
                    args=(jid, input_path, options),
                    daemon=True,
                )
                thread.start()

            zf.close()

            self._send_json({
                'batch_id': batch_id,
                'status': 'running',
                'total': len(job_ids),
                'job_ids': job_ids,
            }, status=202)
            return

        self._drain_request_body()
        self._send_error(404, 'Not found')

    def log_message(self, format, *args):
        logger.info(format, *args)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_multipart(body, boundary):
    """Extract first file from multipart/form-data body."""
    parts = body.split(b'--' + boundary)
    for part in parts:
        if b'filename=' not in part:
            continue
        # Extract filename
        header_end = part.find(b'\r\n\r\n')
        if header_end < 0:
            continue
        headers = part[:header_end].decode('utf-8', errors='replace')
        data = part[header_end + 4:]
        # Trim trailing \r\n
        if data.endswith(b'\r\n'):
            data = data[:-2]

        filename = 'upload.twbx'
        for line in headers.split('\r\n'):
            if 'filename=' in line:
                parts2 = line.split('filename=')
                if len(parts2) > 1:
                    filename = parts2[1].strip('"').strip("'")
                    # Security: strip path components and dangerous chars
                    filename = os.path.basename(filename)
                    filename = ''.join(
                        c for c in filename if ord(c) >= 32 and c != '\x00'
                    )
                    if not filename or not re.match(r'^[\w\-. ]+$', filename):
                        filename = f'upload_{uuid.uuid4().hex[:8]}.twbx'
                break
        return filename, data
    return None


def _int_param(params, key):
    vals = params.get(key, [None])
    val = vals[0]
    if val is not None:
        try:
            return int(val)
        except (ValueError, TypeError):
            pass
    return None


def _get_version():
    try:
        from powerbi_import import __version__
        return __version__
    except ImportError:
        return 'unknown'


# ── Server entry point ────────────────────────────────────────────────────────

def run_server(host='127.0.0.1', port=8000, api_key=None):
    """Start the migration API server."""
    global _API_KEY, _WEBHOOK_SECRET
    if not api_key and host not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError(
            'api_key is required when binding the migration API beyond loopback')
    if api_key:
        _API_KEY = api_key
        _WEBHOOK_SECRET = hashlib.sha256(api_key.encode()).hexdigest()[:32]

    server = HTTPServer((host, port), MigrationHandler)
    print(f"Migration API server running on http://{host}:{port}")
    print(f"  POST /migrate          Retired (410); use /migrate/batch")
    print(f"  POST /migrate/batch    Upload ZIP of workbooks for batch migration")
    print(f"  GET  /status/{{id}}      Check job status")
    print(f"  GET  /download/{{id}}    Download result as ZIP")
    print(f"  GET  /batch/{{id}}       Check batch migration status")
    print(f"  GET  /health           Health check")
    print(f"  GET  /jobs             List all jobs (paginated)")
    print(f"  GET  /openapi.json     OpenAPI 3.0 specification")
    if _API_KEY:
        print(f"  Auth: Bearer token required (--api-key set)")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description='Migration REST API server')
    parser.add_argument('--host', default='127.0.0.1',
                        help='Bind address (default: 127.0.0.1)')
    parser.add_argument('--port', type=int, default=8000,
                        help='Port number (default: 8000)')
    parser.add_argument('--api-key', default=None,
                        help='API key for Bearer token authentication')
    args = parser.parse_args()
    run_server(args.host, args.port, api_key=args.api_key)


if __name__ == '__main__':
    main()
