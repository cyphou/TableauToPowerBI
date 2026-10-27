"""Zero-dependency, loopback-only UI runner. Run: python web/local_server.py.

This is a local-folder workflow, not an upload service or a general command API.
Native applications are launched only by explicitly confirmed diagnostic jobs.
"""
from __future__ import annotations

import argparse
import glob
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
import zipfile
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_PAYLOAD = 64 * 1024
MAX_JOBS = 30
MAX_LOG_LINES = 2000
MAX_LOG_LINE = 4096
MAX_ARTIFACTS = 1000
MIGRATION_TIMEOUT = 60 * 60
DIAGNOSTIC_TIMEOUT = 30 * 60
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 100000
ARTIFACT_EXTENSIONS = {".html", ".json", ".png", ".jpg", ".jpeg", ".pbip", ".zip"}
EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".cache", ".pbi",
    ".migration_extract", ".migration_extracts", "node_modules",
    "source_snapshots", ".source_snapshots",
}
SECRET_NAMES = {".env", "credentials.json", "secrets.json", "config.json"}
ACTIVE = {"queued", "running"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _no_links(path: Path) -> None:
    """Reject links/junctions anywhere along a path, including existing parents."""
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ValueError("Symbolic links and reparse points are not allowed")


def _directory(value, label, must_exist=False) -> Path:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(f"{label} must be a local directory path")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} contains control characters")
    expanded = os.path.expanduser(value.strip())
    if expanded.startswith(("\\\\", "//")):
        raise ValueError(f"{label} must be local, not a network share")
    path = Path(os.path.abspath(expanded))
    _no_links(path)
    path = path.resolve()
    if must_exist and not path.is_dir():
        raise ValueError(f"{label} must be an existing directory")
    if path.exists() and not path.is_dir():
        raise ValueError(f"{label} is not a directory")
    return path


def validate_payload(payload: dict) -> dict:
    """Return a normalized request without creating directories or launching apps."""
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    option_names = {"output_format", "model_mode", "culture", "calendar_start", "calendar_end",
                    "quality_policy", "resume", "all_pages", "screen_comparison"}
    allowed = {"action", "source_dir", "output_dir", "opts", "confirm_native"} | option_names
    if set(payload) - allowed:
        raise ValueError("Unknown request fields")
    action = payload.get("action", "migration")
    if action not in ("migration", "probe", "compare"):
        raise ValueError("action must be migration, probe or compare")
    source = _directory(payload.get("source_dir"), "source_dir", must_exist=True)
    output = _directory(payload.get("output_dir"), "output_dir",
                        must_exist=action != "migration")
    if output == Path(output.anchor) or output == REPO_ROOT:
        raise ValueError("output_dir cannot be a filesystem root or repository root")
    if _inside(output, source) or _inside(source, output):
        raise ValueError("Source and output directories must be separate, non-nested folders")
    opts = payload.get("opts", {})
    if not isinstance(opts, dict):
        raise ValueError("opts must be an object")
    if set(opts) - option_names:
        raise ValueError("Unknown option")
    flat_opts = {key: payload[key] for key in option_names if key in payload}
    if set(flat_opts) & set(opts):
        raise ValueError("Do not supply the same option both flat and inside opts")
    opts = {**opts, **flat_opts}
    if action != "migration" and "model_mode" in opts:
        raise ValueError("model_mode is only valid for migration requests")
    normalized = {
        "output_format": opts.get("output_format", "pbip"),
        "model_mode": opts.get("model_mode", "auto"),
        "quality_policy": opts.get("quality_policy", "report"),
        "resume": opts.get("resume", False),
        "all_pages": opts.get("all_pages", False),
        "screen_comparison": opts.get("screen_comparison", "manual"),
    }
    for key, choices in (
        ("output_format", ("pbip", "fabric")),
        ("model_mode", ("auto", "import", "directquery", "composite")),
        ("quality_policy", ("report", "enterprise", "production")),
        ("screen_comparison", ("manual", "local")),
    ):
        if normalized[key] not in choices:
            raise ValueError(f"Invalid {key}")
    if action != "migration":
        # The default migration mode is injected for migrations only. Keep it
        # out of normalized diagnostic requests so their command can be
        # validated again without mistaking the default for a user option.
        normalized.pop("model_mode")
    for key in ("resume", "all_pages"):
        if type(normalized[key]) is not bool:
            raise ValueError(f"{key} must be a boolean")
    culture = opts.get("culture")
    if culture not in (None, ""):
        if not isinstance(culture, str) or not re.fullmatch(
            r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}", culture
        ):
            raise ValueError("Invalid culture (example: en-US)")
        normalized["culture"] = culture
    for key in ("calendar_start", "calendar_end"):
        value = opts.get(key)
        if value in (None, ""):
            continue
        if type(value) is not int or not 1 <= value <= 9999:
            raise ValueError(f"{key} must be an integer year between 1 and 9999")
        normalized[key] = value
    if normalized.get("calendar_start", 2020) > normalized.get("calendar_end", 2030):
        raise ValueError("calendar_start cannot exceed calendar_end")
    confirm = payload.get("confirm_native", False)
    if type(confirm) is not bool:
        raise ValueError("confirm_native must be a boolean")
    if action != "migration" and not confirm:
        raise ValueError("Native diagnostics require explicit confirm_native: true")
    return {"action": action, "source_dir": str(source), "output_dir": str(output),
            "opts": normalized, "confirm_native": confirm}


def build_command(payload: dict, diagnostic_root=None) -> list[str]:
    """Build only fixed, allowlisted CLI arguments; never executes a command."""
    request = validate_payload(payload)
    action, source, output = (request[k] for k in ("action", "source_dir", "output_dir"))
    opts = request["opts"]
    prefix = [sys.executable, "-u"]
    if action == "migration":
        command = prefix + [str(REPO_ROOT / "migrate.py"), "--batch", source,
                            "--output-dir", output, "--output-format", opts["output_format"],
                            "--mode", opts["model_mode"],
                            "--quality-policy", opts["quality_policy"]]
        for key in ("culture", "calendar_start", "calendar_end"):
            if key in opts:
                command += ["--" + key.replace("_", "-"), str(opts[key])]
        if opts["resume"]:
            command.append("--resume")
        return command
    if action == "probe":
        diag = Path(diagnostic_root) if diagnostic_root else Path(output) / "local_diagnostics"
        _no_links(diag.absolute())
        if not _inside(diag.resolve(), Path(output)):
            raise ValueError("Diagnostic directory must be inside output_dir")
        return prefix + [str(REPO_ROOT / "scripts" / "probe_projects.py"), output,
                         "--shots", str(diag / "screenshots"),
                         "--html", str(diag / "probe.html"),
                         "--json", str(diag / "opening_report.json")]
    command = prefix + [str(REPO_ROOT / "scripts" / "compare_desktop_reports.py"),
                        source, output, "--screen-comparison", opts["screen_comparison"]]
    if opts["all_pages"]:
        command.append("--all-pages")
    return command


def native_unavailable(action: str) -> str | None:
    """Availability check only. No importing optional GUI libraries or launching apps."""
    if action == "migration":
        return None
    if sys.platform != "win32":
        return "Native diagnostics require an interactive Windows session"
    # Use the same non-launching discovery routine as the diagnostic CLI.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from powerbi_import.desktop_probe import find_pbi_desktop
    powerbi = find_pbi_desktop()
    if not powerbi or not Path(powerbi).is_file():
        return "Power BI Desktop was not found (POWERBI_DESKTOP_PATH is supported)"
    if action == "compare":
        tableau = os.environ.get("TABLEAU_DESKTOP_PATH")
        candidates = [tableau] if tableau else []
        if not tableau:
            on_path = shutil.which("tableau.exe")
            if on_path:
                candidates.append(on_path)
            for base in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
                root = Path(os.environ.get(base, "C:/Program Files"))
                candidates.extend(glob.glob(str(root / "Tableau/Tableau */bin/tableau.exe")))
        candidates = [os.path.expandvars(os.path.expanduser(p)) for p in candidates if p]
        if not any(p and Path(p).is_file() and Path(p).name.lower() == "tableau.exe"
                   for p in candidates):
            return "Tableau Desktop was not found (TABLEAU_DESKTOP_PATH is supported)"
    return None


def _excluded(parts, browsing=False, allow_platform=False):
    return any(
        part.lower() in EXCLUDED_DIRS or part.lower() in SECRET_NAMES
        or (part.startswith(".") and not (
            allow_platform and not browsing and index == len(parts) - 1
            and part == ".platform"
        ))
        or (browsing and part.lower() == "data")
        for index, part in enumerate(parts)
    )


def _artifact_path(root: Path, relative: str) -> Path:
    if "\\" in relative or ":" in relative or "\x00" in relative:
        raise ValueError("Invalid artifact path")
    parts = relative.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("Invalid artifact path")
    if _excluded(parts, browsing=True):
        raise ValueError("Artifact is not available for browsing")
    path = root.joinpath(*parts)
    _no_links(path)
    if not _inside(path.resolve(), root.resolve()) or not path.is_file():
        raise ValueError("Artifact not found")
    if path.suffix.lower() not in ARTIFACT_EXTENSIONS:
        raise ValueError("Artifact type is not available for browsing")
    return path


def _walk_output(root: Path, browsing=False):
    _no_links(root)
    if not root.is_dir():
        return

    def onerror(error):
        raise error

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        base = Path(directory)
        _no_links(base)
        for name in list(dirs):
            child = base / name
            # Reject links, rather than silently emitting an incomplete archive.
            _no_links(child)
            if _excluded(child.relative_to(root).parts, browsing):
                dirs.remove(name)
        for name in sorted(files):
            path = base / name
            _no_links(path)
            relative = path.relative_to(root)
            if _excluded(relative.parts, browsing, allow_platform=True):
                continue
            if not browsing and path.suffix.lower() in {".twb", ".twbx", ".hyper", ".tde"}:
                continue
            if not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("Output contains a non-regular file")
            yield path, relative.as_posix()


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        # A specific owned PID, never executable-name-based process termination.
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait(timeout=5)


class JobManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = {}
        self.processes = {}
        self.stopping = False
        self.picking = False

    def submit(self, payload):
        request = validate_payload(payload)
        with self.lock:
            if self.stopping or self.picking or any(j["status"] in ACTIVE for j in self.jobs.values()):
                raise RuntimeError("Another job or folder dialog is already active")
            while len(self.jobs) >= MAX_JOBS:
                del self.jobs[next(iter(self.jobs))]
            job_id = uuid.uuid4().hex
            job = dict(request, id=job_id, status="queued", created_at=_now(),
                       started_at=None, finished_at=None, exit_code=None, error=None,
                       logs=deque(maxlen=MAX_LOG_LINES))
            self.jobs[job_id] = job
            threading.Thread(target=self._run, args=(job,), daemon=True,
                             name=f"local-job-{job_id[:8]}").start()
            return job

    def _run(self, job):
        process = None
        try:
            reason = native_unavailable(job["action"])
            if reason:
                with self.lock:
                    job.update(status="not_run", error=reason)
                    job["logs"].append(reason)
                return
            with self.lock:
                if self.stopping:
                    job.update(status="not_run", error="Server is stopping")
                    return
                job.update(status="running", started_at=_now())
            output = Path(job["output_dir"])
            _no_links(output)
            output.mkdir(parents=True, exist_ok=True)
            diag = output / "local_diagnostics" / job["id"]
            command = build_command(
                {key: job[key] for key in ("action", "source_dir", "output_dir", "opts", "confirm_native")},
                diagnostic_root=diag,
            )
            env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
            with self.lock:
                if self.stopping:
                    job.update(status="not_run", error="Server is stopping")
                    return
                process = subprocess.Popen(
                    command, cwd=str(REPO_ROOT), env=env, shell=False,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                    start_new_session=os.name != "nt",
                )
                self.processes[job["id"]] = process

            def read_logs():
                try:
                    while True:
                        line = process.stdout.readline(MAX_LOG_LINE)
                        if not line:
                            break
                        with self.lock:
                            job["logs"].append(line.rstrip("\r\n"))
                except (OSError, ValueError) as exc:
                    with self.lock:
                        if not self.stopping and not process.stdout.closed:
                            message = f"Log stream read failed: {exc}"
                            job["log_error"] = message
                            job["logs"].append(message[:MAX_LOG_LINE])
                            job.update(status="failed", error=message)

            reader = threading.Thread(target=read_logs, daemon=True)
            reader.start()
            timeout = MIGRATION_TIMEOUT if job["action"] == "migration" else DIAGNOSTIC_TIMEOUT
            try:
                code = process.wait(timeout=timeout)
                reader.join(timeout=2)
                with self.lock:
                    job.update(exit_code=code, status=(
                        "succeeded" if code == 0 and not job.get("log_error") else "failed"
                    ))
                    if code:
                        job["error"] = f"Command exited with status {code}"
            except subprocess.TimeoutExpired:
                _terminate(process)
                with self.lock:
                    job.update(status="failed", exit_code=process.returncode,
                               error=f"Job timed out after {timeout} seconds")
        except Exception as exc:
            with self.lock:
                job.update(status="failed", error=str(exc))
                job["logs"].append(str(exc))
        finally:
            if process is not None:
                try:
                    if process.poll() is None:
                        _terminate(process)
                    if process.stdout:
                        process.stdout.close()
                except (OSError, subprocess.SubprocessError) as exc:
                    with self.lock:
                        job.update(status="failed", error=f"Process cleanup failed: {exc}")
            with self.lock:
                self.processes.pop(job["id"], None)
                job["finished_at"] = _now()

    def snapshot(self, job):
        with self.lock:
            result = {k: list(v) if k == "logs" else v for k, v in job.items()}
        artifacts = []
        try:
            for path, relative in _walk_output(Path(job["output_dir"]), browsing=True):
                if path.suffix.lower() in ARTIFACT_EXTENSIONS:
                    artifacts.append({"name": path.name, "path": relative,
                                      "url": f"/api/artifacts/{job['id']}/{quote(relative, safe='/')}"})
                    if len(artifacts) >= MAX_ARTIFACTS:
                        result["artifacts_truncated"] = True
                        break
        except (OSError, ValueError) as exc:
            result["artifact_error"] = str(exc)
        result["artifacts"] = artifacts
        result["download_url"] = f"/api/download/{job['id']}"
        return result

    def close(self):
        with self.lock:
            self.stopping = True
            processes = list(self.processes.values())
        for process in processes:
            try:
                _terminate(process)
            except (OSError, subprocess.SubprocessError):
                if process.poll() is None:
                    process.kill()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port=DEFAULT_PORT):
        self.token = secrets.token_urlsafe(32)
        self.jobs = JobManager()
        super().__init__((HOST, port), LocalHandler)
        self.origin = f"http://{HOST}:{self.server_port}"

    def server_close(self):
        self.jobs.close()
        super().server_close()


class LocalHandler(BaseHTTPRequestHandler):
    server_version = "LocalMigration/1"
    timeout = 15

    def setup(self):
        super().setup()
        self.connection.settimeout(self.timeout)

    def log_message(self, fmt, *args):
        # Avoid logging URL-embedded local paths or control characters.
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if getattr(self, "_serving_artifact", False):
            self.send_header("Content-Security-Policy",
                             "sandbox allow-scripts; default-src 'none'; "
                             "script-src 'unsafe-inline'; connect-src 'none'; "
                             f"img-src {self.server.origin} data:; "
                             "style-src 'unsafe-inline'; form-action 'none'; "
                             "base-uri 'none'; frame-ancestors 'self'")
        else:
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; style-src 'self'; "
                             "img-src 'self' data:; connect-src 'self'; frame-src 'self'; "
                             "object-src 'none'; base-uri 'none'; form-action 'self'; "
                             "frame-ancestors 'self'")
        super().end_headers()

    def _json(self, status, body):
        data = json.dumps(body, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _guard(self, post=False):
        hosts = self.headers.get_all("Host", [])
        if hosts != [f"{HOST}:{self.server.server_port}"]:
            self._json(403, {"error": "Host must match the bound loopback address and port"})
            return False
        origins = self.headers.get_all("Origin", [])
        if origins and origins != [self.server.origin]:
            self._json(403, {"error": "Cross-origin requests are forbidden"})
            return False
        if self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
            self._json(403, {"error": "Cross-site requests are forbidden"})
            return False
        if post and (origins != [self.server.origin] or not secrets.compare_digest(
            self.headers.get("X-Local-Token", ""), self.server.token
        )):
            self._json(403, {"error": "Same-origin request and X-Local-Token are required"})
            return False
        return True

    def _job(self, job_id):
        with self.server.jobs.lock:
            job = self.server.jobs.jobs.get(job_id)
        if job is None:
            raise ValueError("Unknown or expired job")
        return job

    def do_GET(self):
        if not self._guard():
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/session":
                self._json(200, {"token": self.server.token, "csrf_token": self.server.token,
                                 "origin": self.server.origin})
            elif path == "/api/jobs":
                with self.server.jobs.lock:
                    jobs = list(self.server.jobs.jobs.values())
                self._json(200, {"jobs": [self.server.jobs.snapshot(j) for j in reversed(jobs)]})
            elif path.startswith("/api/artifacts/"):
                pieces = path.split("/", 4)
                if len(pieces) != 5:
                    raise ValueError("Invalid artifact URL")
                job = self._job(pieces[3])
                file = _artifact_path(Path(job["output_dir"]), unquote(pieces[4]))
                self._file(file, artifact=True)
            elif path.startswith("/api/download/"):
                self._download(self._job(path.removeprefix("/api/download/")))
            elif path in ("/", "/index.html", "/app.js", "/style.css"):
                self._file(WEB_ROOT / ("index.html" if path == "/" else path[1:]))
            else:
                self._json(404, {"error": "Not found"})
        except (ValueError, OSError) as exc:
            self._json(404, {"error": str(exc)})

    def _file(self, path, artifact=False):
        _no_links(path)
        with path.open("rb") as handle:
            self._serving_artifact = artifact
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(os.fstat(handle.fileno()).st_size))
            if artifact:
                # end_headers enforces sandbox even for a top-level artifact tab.
                if path.suffix.lower() in {".zip", ".pbip"}:
                    self.send_header("Content-Disposition", "attachment")
            self.end_headers()
            shutil.copyfileobj(handle, self.wfile)

    def _download(self, job):
        if job["status"] in ACTIVE:
            self._json(409, {"error": "Wait for the job to finish before downloading"})
            return
        root = Path(job["output_dir"])
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as spool:
            total = count = 0
            with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for path, relative in _walk_output(root):
                    total += path.stat().st_size
                    count += 1
                    if total > MAX_ARCHIVE_BYTES or count > MAX_ARCHIVE_FILES:
                        self._json(413, {"error": "Output exceeds local ZIP limit; use the output folder directly"})
                        return
                    archive.write(path, arcname=f"generated-output/{relative}")
            if not count:
                self._json(404, {"error": "No generated files available"})
                return
            length = spool.tell()
            spool.seek(0)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Disposition",
                             f'attachment; filename="migration-{job["id"]}.zip"')
            self.end_headers()
            shutil.copyfileobj(spool, self.wfile)

    def do_POST(self):
        self.close_connection = True
        if not self._guard(post=True):
            return
        if self.headers.get("Transfer-Encoding"):
            self._json(400, {"error": "Chunked requests are not supported"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            self._json(411, {"error": "A single Content-Length is required"})
            return
        if len(lengths[0]) > 10:
            self._json(413, {"error": "JSON payload exceeds 64 KB"})
            return
        length = int(lengths[0])
        if length > MAX_PAYLOAD:
            self._json(413, {"error": "JSON payload exceeds 64 KB"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "Content-Type must be application/json"})
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete request body")
            payload = json.loads(raw.decode("utf-8"))
            path = urlsplit(self.path).path
            if path == "/api/jobs":
                job = self.server.jobs.submit(payload)
                self._json(202, {"job": self.server.jobs.snapshot(job)})
            elif path == "/api/pick-folder":
                self._pick_folder(payload)
            else:
                self._json(404, {"error": "Not found"})
        except (ValueError, UnicodeError) as exc:
            self._json(400, {"error": str(exc)})
        except RuntimeError as exc:
            self._json(409, {"error": str(exc)})
        except OSError as exc:
            self._json(500, {"error": str(exc)})

    def _pick_folder(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"title"}:
            raise ValueError("Folder picker accepts only an optional title")
        title = payload.get("title", "Choose a local folder")
        if not isinstance(title, str) or len(title) > 100:
            raise ValueError("Invalid dialog title")
        manager = self.server.jobs
        with manager.lock:
            if manager.picking or any(j["status"] in ACTIVE for j in manager.jobs.values()):
                raise RuntimeError("Another job or folder dialog is already active")
            manager.picking = True
        root = None
        try:
            import tkinter
            from tkinter import filedialog
            root = tkinter.Tk()
            root.withdraw()
            selected = filedialog.askdirectory(parent=root, title=title, mustexist=True)
            self._json(200, {"path": selected or None})
        except Exception:
            self._json(503, {"error": "Native folder picker unavailable; type or paste a folder path"})
        finally:
            if root is not None:
                root.destroy()
            with manager.lock:
                manager.picking = False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="Loopback HTTP port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true",
                        help="Do not open the local UI in a browser")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        server = LocalServer(args.port)
    except OSError as exc:
        parser.exit(1, f"Cannot bind {HOST}:{args.port}: {exc}\n")
    print(f"Local migration UI: {server.origin}/", flush=True)
    print("Native diagnostics run only after explicit confirmation. Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        threading.Thread(target=webbrowser.open, args=(server.origin + "/",),
                         daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
