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

import os
import sys
import json
import shutil
import tempfile
import logging

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from powerbi_import.api_server import (
    _confined_path, _uploaded_filename, _run_upload_worker, _write_output_archive,
    _parse_multipart,
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


def _zip_directory(source_dir, zip_path, job_dir):
    """Create a download only from the recorded private job directory."""
    zip_path = _confined_path(zip_path, job_dir)
    _write_output_archive(source_dir, zip_path, job_dir)


def _save_uploaded_file(uploaded, tmp_dir):
    """Save a Streamlit uploaded file to a temp directory, return path."""
    filename = _uploaded_filename(uploaded.name)
    path = os.path.join(tmp_dir, filename)
    stem, extension = os.path.splitext(filename)
    index = 1
    while os.path.lexists(path):
        path = os.path.join(tmp_dir, f'{stem}_{index}{extension}')
        index += 1
    path = _confined_path(path, tmp_dir)
    with open(path, 'xb') as f:
        f.write(uploaded.getbuffer())
    return path


def _run_migration_pipeline(file_path, options, progress_callback=None):
    """Run the migration pipeline and return results dict.

    Args:
        file_path: Path to .twb/.twbx file
        options: dict with migration options
        progress_callback: optional callable(step, total, message)

    Returns:
        dict with keys: success, output_dir, stats, errors, assessment
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
            progress_callback(1, 4, "Extracting Tableau objects...")

        upload_root = options.get('upload_root')
        if not upload_root:
            raise ValueError("Missing server-owned private upload directory")
        file_path = _confined_path(file_path, upload_root)
        result['output_dir'] = _confined_path(result['output_dir'], upload_root)
        os.makedirs(result['output_dir'], exist_ok=True)
        _run_upload_worker(file_path, upload_root, result['output_dir'], options)
        if not os.listdir(result['output_dir']):
            raise RuntimeError("Migration produced no output artifacts")
        # Reject unsafe generated paths before inspecting or validating artifacts.
        for current, dirs, files in os.walk(result['output_dir']):
            for name in dirs + files:
                _confined_path(os.path.join(current, name), result['output_dir'])

        if progress_callback:
            progress_callback(2, 4, "Generating Power BI project...")

        # Extraction and generation ran together in the isolated worker.
        report_name = os.path.splitext(os.path.basename(file_path))[0]

        # Find the generated project folder for stats
        project_dir = os.path.join(result['output_dir'], report_name)
        stats = {}
        sm_dir = os.path.join(project_dir, f'{report_name}.SemanticModel')
        report_dir = os.path.join(project_dir, f'{report_name}.Report')
        if os.path.isdir(sm_dir):
            # Count tables from definition/tables/
            tables_dir = os.path.join(sm_dir, 'definition', 'tables')
            if os.path.isdir(tables_dir):
                stats['tables'] = len([d for d in os.listdir(tables_dir)
                                       if os.path.isdir(os.path.join(tables_dir, d))])
        if os.path.isdir(report_dir):
            # Count pages
            pages_dir = os.path.join(report_dir, 'definition', 'pages')
            if os.path.isdir(pages_dir):
                page_dirs = [d for d in os.listdir(pages_dir)
                             if os.path.isdir(os.path.join(pages_dir, d))]
                stats['pages'] = len(page_dirs)
                # Count visuals across all pages
                visuals = 0
                for pd_name in page_dirs:
                    vis_dir = os.path.join(pages_dir, pd_name, 'visuals')
                    if os.path.isdir(vis_dir):
                        visuals += len([v for v in os.listdir(vis_dir)
                                        if os.path.isdir(os.path.join(vis_dir, v))])
                stats['visuals'] = visuals
        result['stats'] = stats
        result['success'] = True

        if progress_callback:
            progress_callback(3, 4, "Validating artifacts...")

        # Step 3: Validation (optional)
        try:
            from powerbi_import.validator import ArtifactValidator
            val_result = ArtifactValidator.validate_project(project_dir)
            result['validation'] = val_result
        except Exception as e:
            result['validation'] = {'error': str(e)}

        if progress_callback:
            progress_callback(4, 4, "Done!")

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
        st.session_state.tmp_dir = tempfile.mkdtemp(prefix='tableau_migrate_')
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
        st.header("Step 1: Upload Tableau Workbook")
        uploaded = st.file_uploader(
            "Choose .twb or .twbx file(s)",
            type=['twb', 'twbx', 'tds', 'tdsx'],
            accept_multiple_files=True,
        )
        if uploaded:
            paths = []
            for f in uploaded:
                try:
                    path = _save_uploaded_file(f, st.session_state.tmp_dir)
                except (ValueError, OSError) as exc:
                    st.error(f"Upload failed: {exc}")
                    continue
                paths.append(path)
                st.success(f"Uploaded: {f.name} ({f.size / 1024:.1f} KB)")
            st.session_state.file_paths = paths

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
            time_intel = st.selectbox("Time Intelligence", ['none', 'auto'])

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
                    result = _run_assessment(st.session_state.file_paths[0],
                                             st.session_state.tmp_dir)
                    st.session_state.assessment = result

        assessment = st.session_state.assessment
        if assessment:
            if 'error' in assessment:
                st.error(f"Assessment error: {assessment['error']}")
            else:
                overall = assessment.get('overall_score', 'UNKNOWN')
                color_map = {'GREEN': 'green', 'YELLOW': 'orange', 'RED': 'red'}
                color = color_map.get(overall, 'gray')
                totals = assessment.get('totals', {})
                total_checks = totals.get('checks', 0)
                pass_count = totals.get('pass', 0)
                pct = round(pass_count / total_checks * 100) if total_checks else 0
                st.markdown(f"### Overall Score: :{color}[{pct}%] ({overall})")

                cols_t = st.columns(3)
                cols_t[0].metric("Pass", totals.get('pass', 0))
                cols_t[1].metric("Warn", totals.get('warn', 0))
                cols_t[2].metric("Fail", totals.get('fail', 0))

                categories = assessment.get('categories', [])
                for cat in categories:
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
                st.session_state.file_paths[0],
                st.session_state.options,
                progress_callback=_progress,
            )
            st.session_state.migration_result = result

        result = st.session_state.migration_result
        if result['success']:
            st.success("Migration completed successfully!")
            stats = result.get('stats', {})
            if stats:
                cols = st.columns(4)
                cols[0].metric("Tables", stats.get('tables', 0))
                cols[1].metric("Measures", stats.get('measures', 0))
                cols[2].metric("Visuals", stats.get('visuals', 0))
                cols[3].metric("Pages", stats.get('pages', 0))
        else:
            st.error("Migration failed")
            for err in result.get('errors', []):
                st.error(err)

        if st.button("Next →", type="primary"):
            st.session_state.step = 5
            st.rerun()

    # ── Step 5: Validate ──
    elif st.session_state.step == 5:
        st.header("Step 5: Validation")

        result = st.session_state.migration_result
        validation = result.get('validation', {}) if result else {}

        if 'error' in validation:
            st.warning(f"Validation: {validation['error']}")
        else:
            is_valid = validation.get('valid', validation.get('passed', False))
            errors = validation.get('errors', [])
            warnings = validation.get('warnings', [])
            if is_valid:
                st.success("All validation checks passed!")
            else:
                st.warning("Some validation issues found:")
                for err in errors:
                    st.error(f"  - {err}")
            if warnings:
                for w in warnings:
                    st.warning(f"  - {w}")

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
                # Never reuse an archive that predates the final path checks.
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
                st.error(f"Unsafe or unavailable download output: {exc}")

            st.info("Open the extracted .pbip file in Power BI Desktop (December 2025+).")
        else:
            st.error("No migration output available. Please run the migration first.")

        if st.button("Start Over"):
            # Cleanup
            tmp = st.session_state.tmp_dir
            shutil.rmtree(tmp, ignore_errors=True)
            for key in list(st.session_state.keys()):
                del st.session_state[key]
            st.session_state.step = 1
            st.session_state.tmp_dir = tempfile.mkdtemp(prefix='tableau_migrate_')
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
<p>Upload a .twb or .twbx file to migrate.</p>
<form method="POST" enctype="multipart/form-data" action="/migrate">
  <div class="upload-area">
    <input type="file" name="file" accept=".twb,.twbx,.tds,.tdsx" required>
  </div>
  <button type="submit">Migrate</button>
</form>
</body>
</html>"""


def run_fallback_server(port=8501):
    """Run a minimal stdlib-based web UI (no Streamlit dependency)."""
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from email.message import Message

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
                header = Message()
                header['Content-Type'] = content_type
                boundary = header.get_boundary()
                if not boundary:
                    raise ValueError("Invalid multipart boundary")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("Incomplete uploaded body")
                parsed = _parse_multipart(body, boundary.encode('utf-8'))
                if not parsed:
                    raise ValueError("No file uploaded")
                filename, file_data = parsed
                filename = _uploaded_filename(filename)
            except (TypeError, ValueError) as exc:
                self.send_error(400, str(exc))
                return

            try:
                with tempfile.TemporaryDirectory(prefix='tableau_migrate_') as tmp_dir:
                    file_path = _confined_path(os.path.join(tmp_dir, filename), tmp_dir)
                    with open(file_path, 'xb') as uploaded:
                        uploaded.write(file_data)
                    output_dir = os.path.join(tmp_dir, 'output')
                    result = _run_migration_pipeline(
                        file_path, {'output_dir': output_dir, 'upload_root': tmp_dir})
            except (ValueError, OSError) as exc:
                self.send_error(400, f"Unable to save upload: {exc}")
                return

            self.send_response(200 if result['success'] else 422)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            response = {
                'success': result['success'],
                'errors': result.get('errors', []),
                'stats': result.get('stats', {}),
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
