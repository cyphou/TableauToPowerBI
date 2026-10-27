"""Streamlit Web UI — browser-based migration wizard.

A 6-step interactive wizard for non-CLI users:
1. Upload — Upload .twb/.twbx file(s)
2. Configure — Set migration options (culture, mode, output format)
3. Assess — Pre-migration readiness assessment
4. Migrate — Run extraction + generation with progress
5. Validate — Artifact validation summary
6. Download — Download .pbip project as ZIP

Requires optional `streamlit` package:
    pip install streamlit
    streamlit run web/app.py

If streamlit is not installed, a fallback mode launches the stdlib
http.server-based wizard (no external deps).
"""

import json
import logging
import os
import shutil
import sys
import tempfile

from powerbi_import.api_server import (
    _confined_path,
    _run_upload_worker,
    _uploaded_filename,
    _write_output_archive,
)

logger = logging.getLogger('tableau_to_powerbi.web')

# ════════════════════════════════════════════════════════════════════
#  STREAMLIT APP
# ════════════════════════════════════════════════════════════════════

_HAS_STREAMLIT = False
try:
    import streamlit as st
    _HAS_STREAMLIT = True
except ImportError:
    st = None


def _new_job_dir():
    """Create a private job directory on its canonical path.

    Upload confinement compares real and lexical paths, so a temp root reached
    through an alias (Windows 8.3 names, macOS /var) is resolved up front, as
    the REST server does.
    """
    return os.path.realpath(tempfile.mkdtemp(prefix='tableau_migrate_'))


def _zip_directory(source_dir, zip_path, job_dir=None):
    """Create a ZIP archive of a directory."""
    if not job_dir:
        raise ValueError("Missing server-owned private job directory")
    _write_output_archive(source_dir, zip_path, job_dir)


def _save_uploaded_file(uploaded, source_dir):
    """Save a Streamlit uploaded file to the batch source directory."""
    os.makedirs(source_dir, exist_ok=True)
    filename = _uploaded_filename(uploaded.name)
    path = os.path.join(source_dir, filename)
    if os.path.exists(path):
        stem, extension = os.path.splitext(filename)
        index = 2
        while os.path.exists(path):
            path = os.path.join(source_dir, f'{stem}_{index}{extension}')
            index += 1
    from migrate import _confine_upload_path
    _confine_upload_path(path, source_dir)
    with open(path, 'xb') as f:
        f.write(uploaded.getbuffer())
    return path


def _summarize_batch_output(output_dir):
    """Collect aggregate artifact counts from generated PBIP projects."""
    stats = {'projects': 0, 'tables': 0, 'pages': 0, 'visuals': 0}
    for root, dirs, _files in os.walk(output_dir):
        for model_name in [d for d in dirs if d.endswith('.SemanticModel')]:
            stats['projects'] += 1
            tables_dir = os.path.join(root, model_name, 'definition', 'tables')
            if os.path.isdir(tables_dir):
                stats['tables'] += sum(
                    os.path.isdir(os.path.join(tables_dir, name))
                    for name in os.listdir(tables_dir)
                )
        for report_name in [d for d in dirs if d.endswith('.Report')]:
            pages_dir = os.path.join(root, report_name, 'definition', 'pages')
            if not os.path.isdir(pages_dir):
                continue
            page_names = [
                name for name in os.listdir(pages_dir)
                if os.path.isdir(os.path.join(pages_dir, name))
            ]
            stats['pages'] += len(page_names)
            for page_name in page_names:
                visuals_dir = os.path.join(pages_dir, page_name, 'visuals')
                if os.path.isdir(visuals_dir):
                    stats['visuals'] += sum(
                        os.path.isdir(os.path.join(visuals_dir, name))
                        for name in os.listdir(visuals_dir)
                    )
    return stats


def _run_migration_pipeline(source_dir, options, progress_callback=None):
    """Run the batch migration engine for all files in a source directory.

    Args:
        source_dir: Directory containing uploaded Tableau workbooks
        options: dict with migration options
        progress_callback: optional callable(step, total, message)

    Returns:
        Aggregate batch results and output artifact counts.
    """
    # Add project root to path
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)

    result = {
        'success': False,
        'output_dir': options.get('output_dir', ''),
        'stats': {},
        'errors': [],
        'assessment': None,
        'job_dir': options.get('upload_root'),
    }

    try:
        if progress_callback:
            progress_callback(1, 3, "Migrating all uploaded workbooks...")

        upload_root = options.get('upload_root')
        if not upload_root:
            raise ValueError("Missing server-owned private upload directory")
        source_dir = _confined_path(source_dir, upload_root)
        result['output_dir'] = _confined_path(result['output_dir'], upload_root)
        os.makedirs(result['output_dir'], exist_ok=True)
        completed = _run_upload_worker(
            source_dir, upload_root, result['output_dir'], options)
        for current, dirs, files in os.walk(result['output_dir']):
            for name in dirs + files:
                _confined_path(os.path.join(current, name), result['output_dir'])
        result['stats'] = _summarize_batch_output(result['output_dir'])
        result['return_code'] = completed.returncode
        result['success'] = completed.returncode == 0 and result['stats']['projects'] > 0
        if completed.returncode:
            result['errors'].append(
                f"Batch migration finished with exit code {completed.returncode}."
            )
            if completed.stderr.strip():
                result['errors'].append(completed.stderr.strip()[-4000:])
        if result['stats']['projects'] == 0 and completed.stdout.strip():
            result['errors'].append(completed.stdout.strip()[-4000:])
        elif result['stats']['projects'] == 0 and not result['errors']:
            result['errors'].append("The batch run did not generate any Power BI projects.")

        if progress_callback:
            progress_callback(3, 3, "Batch migration complete.")

    except Exception as e:
        result['errors'].append(str(e))
        logger.exception("Migration pipeline error")

    return result


def _run_assessment(file_path, upload_root=None):
    """Run pre-migration assessment, return assessment dict."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)

    try:
        if not upload_root:
            raise ValueError("Missing server-owned private upload directory")
        file_path = _confined_path(file_path, upload_root)
        with tempfile.TemporaryDirectory(prefix='assessment_', dir=upload_root) as output:
            _run_upload_worker(file_path, upload_root, output, assess=True)
            workbook_name = os.path.splitext(os.path.basename(file_path))[0]
            report_path = _confined_path(
                os.path.join(output, f'assessment_{workbook_name}.json'), output)
            with open(report_path, 'r', encoding='utf-8') as report:
                return json.load(report)
    except Exception as e:
        return {'error': str(e)}


# ════════════════════════════════════════════════════════════════════
#  STREAMLIT UI
# ════════════════════════════════════════════════════════════════════

def run_streamlit_app():
    """Launch the Streamlit migration wizard."""
    if not _HAS_STREAMLIT:
        print("Streamlit is not installed. Install with: pip install streamlit")
        print("Falling back to CLI wizard...")
        return False

    st.set_page_config(
        page_title="Tableau → Power BI Migration",
        page_icon="📊",
        layout="wide",
    )

    st.title("📊 Tableau → Power BI Migration Wizard")
    st.markdown("Migrate your Tableau workbooks to Power BI in seconds.")

    # ── Session State ──
    if 'step' not in st.session_state:
        st.session_state.step = 1
    if 'tmp_dir' not in st.session_state:
        st.session_state.tmp_dir = _new_job_dir()
    if 'file_paths' not in st.session_state:
        st.session_state.file_paths = []
    if 'assessment' not in st.session_state:
        st.session_state.assessment = None
    if 'migration_result' not in st.session_state:
        st.session_state.migration_result = None

    # ── Navigation ──
    steps = ["Upload", "Configure", "Assess", "Migrate", "Validate", "Download"]
    cols = st.columns(len(steps))
    for i, (col, name) in enumerate(zip(cols, steps)):
        step_num = i + 1
        if step_num == st.session_state.step:
            col.markdown(f"**{step_num}. {name}** ◀")
        elif step_num < st.session_state.step:
            col.markdown(f"~~{step_num}. {name}~~ ✓")
        else:
            col.markdown(f"{step_num}. {name}")

    st.divider()

    # ── Step 1: Upload ──
    if st.session_state.step == 1:
        st.header("Step 1: Upload Tableau Workbooks")
        uploaded = st.file_uploader(
            "Choose one or more .twb or .twbx files",
            type=['twb', 'twbx', 'tds', 'tdsx'],
            accept_multiple_files=True,
        )
        if uploaded:
            paths = []
            source_dir = os.path.join(st.session_state.tmp_dir, 'source')
            if os.path.isdir(source_dir):
                shutil.rmtree(source_dir)
            os.makedirs(source_dir, exist_ok=True)
            for f in uploaded:
                path = _save_uploaded_file(f, source_dir)
                paths.append(path)
            st.success(f"{len(paths)} workbook(s) ready for batch migration.")
            with st.expander("Uploaded files"):
                for f in uploaded:
                    st.write(f"{f.name} ({f.size / 1024:.1f} KB)")
            st.session_state.file_paths = paths
            st.session_state.source_dir = source_dir
            st.session_state.assessment = None
            st.session_state.migration_result = None

            if st.button("Next →", type="primary"):
                st.session_state.step = 2
                st.rerun()

    # ── Step 2: Configure ──
    elif st.session_state.step == 2:
        st.header("Step 2: Configure Migration")

        col1, col2 = st.columns(2)
        with col1:
            culture = st.selectbox("Culture", [
                'en-US', 'fr-FR', 'de-DE', 'es-ES', 'pt-BR',
                'ja-JP', 'zh-CN', 'ko-KR', 'it-IT', 'nl-NL',
            ])
            mode = st.selectbox("Storage Mode", ['import', 'directquery', 'composite'])

        with col2:
            output_format = st.selectbox("Output Format", ['pbip', 'fabric'])
            optimize = st.checkbox("Optimize DAX", value=True)
            time_intel = st.selectbox("Time Intelligence", ['none', 'auto', 'full'])

        if 'options' not in st.session_state:
            st.session_state.options = {}

        st.session_state.options = {
            'culture': culture,
            'mode': mode,
            'output_format': output_format,
            'optimize_dax': optimize,
            'time_intelligence': time_intel,
            'output_dir': os.path.join(st.session_state.tmp_dir, 'output'),
            'upload_root': st.session_state.tmp_dir,
        }

        c1, c2 = st.columns(2)
        with c1:
            if st.button("← Back"):
                st.session_state.step = 1
                st.rerun()
        with c2:
            if st.button("Next →", type="primary"):
                st.session_state.step = 3
                st.rerun()

    # ── Step 3: Assess ──
    elif st.session_state.step == 3:
        st.header("Step 3: Pre-Migration Assessment")

        if st.session_state.assessment is None:
            with st.spinner("Running assessment..."):
                if st.session_state.file_paths:
                    st.session_state.assessment = [
                        {'file': os.path.basename(path),
                         'result': _run_assessment(path, st.session_state.tmp_dir)}
                        for path in st.session_state.file_paths
                    ]

        assessment = st.session_state.assessment
        if assessment:
            for item in assessment:
                report = item['result']
                with st.expander(item['file'], expanded=len(assessment) == 1):
                    if 'error' in report:
                        st.error(f"Assessment error: {report['error']}")
                        continue
                    overall = report.get('overall_score', 'UNKNOWN')
                    color_map = {'GREEN': 'green', 'YELLOW': 'orange', 'RED': 'red'}
                    color = color_map.get(overall, 'gray')
                    totals = report.get('totals', {})
                    total_checks = totals.get('checks', 0)
                    pass_count = totals.get('pass', 0)
                    pct = round(pass_count / total_checks * 100) if total_checks else 0
                    st.markdown(f"### Overall Score: :{color}[{pct}%] ({overall})")
                    cols_t = st.columns(3)
                    cols_t[0].metric("Pass", totals.get('pass', 0))
                    cols_t[1].metric("Warn", totals.get('warn', 0))
                    cols_t[2].metric("Fail", totals.get('fail', 0))
                    for cat in report.get('categories', []):
                        if isinstance(cat, dict):
                            name = cat.get('name', '')
                            checks = cat.get('checks', [])
                            pass_c = sum(1 for c in checks if c.get('severity') == 'pass')
                            total_c = len(checks)
                            score = round(pass_c / total_c * 100) if total_c else 100
                            severity = cat.get('worst_severity', 'pass')
                            icon = {'pass': '✅', 'warn': '⚠️', 'fail': '❌'}.get(severity, '•')
                            st.progress(score / 100, text=f"{icon} {name}: {score}% ({pass_c}/{total_c})")

        c1, c2 = st.columns(2)
        with c1:
            if st.button("← Back"):
                st.session_state.step = 2
                st.rerun()
        with c2:
            if st.button("Migrate →", type="primary"):
                st.session_state.step = 4
                st.rerun()

    # ── Step 4: Migrate ──
    elif st.session_state.step == 4:
        st.header("Step 4: Migration")

        if st.session_state.migration_result is None:
            progress = st.progress(0, text="Starting migration...")
            status = st.empty()

            def _progress(step, total, msg):
                progress.progress(step / total, text=msg)
                status.info(msg)

            result = _run_migration_pipeline(
                st.session_state.source_dir,
                st.session_state.options,
                progress_callback=_progress,
            )
            st.session_state.migration_result = result

        result = st.session_state.migration_result
        if result['success']:
            failed = max(0, len(st.session_state.file_paths) - result['stats'].get('projects', 0))
            if failed or result.get('return_code'):
                st.warning(
                    f"Batch generated {result['stats'].get('projects', 0)} project(s) "
                    f"from {len(st.session_state.file_paths)} workbook(s)."
                )
            else:
                st.success(f"All {len(st.session_state.file_paths)} workbook(s) migrated.")
            stats = result.get('stats', {})
            if stats:
                cols = st.columns(5)
                cols[0].metric("Projects", stats.get('projects', 0))
                cols[1].metric("Tables", stats.get('tables', 0))
                cols[2].metric("Visuals", stats.get('visuals', 0))
                cols[3].metric("Pages", stats.get('pages', 0))
                cols[4].metric("Failed", failed)
            for error in result.get('errors', []):
                st.warning(error)
        else:
            st.error("Migration failed")
            for err in result.get('errors', []):
                st.error(err)

        if st.button("Next →", type="primary"):
            st.session_state.step = 5
            st.rerun()

    # ── Step 5: Validate ──
    elif st.session_state.step == 5:
        st.header("Step 5: Batch Summary")
        result = st.session_state.migration_result
        if result:
            stats = result.get('stats', {})
            failed = max(0, len(st.session_state.file_paths) - stats.get('projects', 0))
            st.metric("Projects generated", stats.get('projects', 0))
            if failed:
                st.warning(
                    f"{failed} of {len(st.session_state.file_paths)} uploaded workbook(s) "
                    "did not produce a project."
                )
            else:
                st.success("Every uploaded workbook produced a project.")
            for error in result.get('errors', []):
                st.warning(error)

        if st.button("Download →", type="primary"):
            st.session_state.step = 6
            st.rerun()

    # ── Step 6: Download ──
    elif st.session_state.step == 6:
        st.header("Step 6: Download")

        result = st.session_state.migration_result
        if result and result['success']:
            output_dir = result['output_dir']
            zip_path = os.path.join(st.session_state.tmp_dir, 'migration_output.zip')

            try:
                # Rebuild after final path validation; never serve a cached
                # archive that predates the private-boundary checks.
                _zip_directory(output_dir, zip_path, result['job_dir'])
                with open(_confined_path(zip_path, result['job_dir']), 'rb') as f:
                    st.download_button(
                        label="Download .pbip Project (ZIP)",
                        data=f.read(),
                        file_name="migration_output.zip",
                        mime="application/zip",
                        type="primary",
                    )
            except (ValueError, OSError) as exc:
                st.error(f"Unsafe download output: {exc}")

            st.info("Open the extracted .pbip file in Power BI Desktop (December 2025+).")
        else:
            st.error("No migration output available. Please run the migration first.")

        if st.button("Start Over"):
            # Cleanup
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            st.session_state.step = 1
            st.session_state.tmp_dir = _new_job_dir()
            st.rerun()


# ════════════════════════════════════════════════════════════════════
#  FALLBACK: STDLIB HTTP SERVER
# ════════════════════════════════════════════════════════════════════

def _generate_upload_html():
    """Generate a simple HTML upload form."""
    return """<!DOCTYPE html>
<html>
<head>
<title>Tableau to Power BI Migration</title>
<style>
  body { font-family: 'Segoe UI', sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; }
  h1 { color: #0078D4; }
  .upload-area { border: 2px dashed #0078D4; padding: 40px; text-align: center;
                 border-radius: 8px; margin: 20px 0; background: #f8f9fa; }
  input[type=file] { margin: 10px; }
  button { background: #0078D4; color: white; padding: 12px 24px; border: none;
           border-radius: 4px; font-size: 16px; cursor: pointer; }
  button:hover { background: #005a9e; }
</style>
</head>
<body>
<h1>Tableau → Power BI Migration</h1>
<p>Upload one or more Tableau workbooks for batch migration.</p>
<form method="POST" enctype="multipart/form-data" action="/migrate">
  <div class="upload-area">
    <input type="file" name="file" accept=".twb,.twbx,.tds,.tdsx" multiple required>
  </div>
  <button type="submit">Migrate</button>
</form>
</body>
</html>"""


def run_fallback_server(port=8501):
    """Run a minimal stdlib-based web UI (no Streamlit dependency)."""
    from email import policy
    from email.parser import BytesParser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(_generate_upload_html().encode('utf-8'))

        def do_POST(self):
            if self.path != '/migrate':
                self.send_error(404)
                return

            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type:
                self.send_error(400, "Expected multipart/form-data")
                return

            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 500 * 1024 * 1024:
                    raise ValueError("Upload must be between 1 byte and 500 MB")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("Incomplete uploaded body")
                message = BytesParser(policy=policy.default).parsebytes(
                    b'Content-Type: ' + content_type.encode('utf-8')
                    + b'\r\nMIME-Version: 1.0\r\n\r\n' + body)
                if not message.is_multipart() or not message.get_boundary():
                    raise ValueError("Invalid multipart boundary")
                file_items = [
                    (_uploaded_filename(part.get_filename()), part.get_payload(decode=True))
                    for part in message.iter_parts()
                    if part.get_param('name', header='content-disposition') == 'file'
                    and part.get_filename()
                ]
                if not file_items:
                    raise ValueError("No file uploaded")
                with tempfile.TemporaryDirectory(prefix='tableau_migrate_') as created_dir:
                    tmp_dir = os.path.realpath(created_dir)
                    source_dir = os.path.join(tmp_dir, 'source')
                    os.mkdir(source_dir)
                    for filename, data in file_items:
                        stem, extension = os.path.splitext(filename)
                        destination = os.path.join(source_dir, filename)
                        index = 2
                        while os.path.exists(destination):
                            destination = os.path.join(source_dir, f'{stem}_{index}{extension}')
                            index += 1
                        destination = _confined_path(destination, source_dir)
                        with open(destination, 'xb') as uploaded:
                            uploaded.write(data)
                    output_dir = os.path.join(tmp_dir, 'output')
                    result = _run_migration_pipeline(
                        source_dir, {'output_dir': output_dir, 'upload_root': tmp_dir})
            except (TypeError, ValueError, OSError) as exc:
                self.send_error(400, str(exc))
                return

            self.send_response(200 if result['success'] else 422)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            response = {
                'success': result['success'],
                'errors': result.get('errors', []),
                'stats': result.get('stats', {}),
                'uploaded': len(file_items),
            }
            self.wfile.write(json.dumps(response, indent=2).encode('utf-8'))

        def log_message(self, fmt, *args):
            logger.info(fmt, *args)

    server = HTTPServer(('127.0.0.1', port), Handler)
    print(f"Migration Web UI running at http://127.0.0.1:{port}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


# ════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════

def launch_web_ui(port=8501):
    """Launch the web UI — Streamlit if available, fallback otherwise."""
    if _HAS_STREAMLIT:
        run_streamlit_app()
    else:
        run_fallback_server(port)


if __name__ == '__main__':
    if _HAS_STREAMLIT:
        run_streamlit_app()
    else:
        run_fallback_server()
