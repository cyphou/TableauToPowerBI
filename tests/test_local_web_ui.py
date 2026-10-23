"""Real loopback HTTP coverage for the stdlib local runner; never launch apps."""

import ast
import http.client
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote
import zipfile

from web import local_server as local


class _FakeProcess:
    """Only the Popen interface used by the runner, with deterministic blocking."""

    def __init__(self, code=0, logs="first line\nsecond line\n", release=None,
                 timed_out=False):
        self.stdout = io.StringIO(logs)
        self.returncode = None
        self.code = code
        self.release = release
        self.timed_out = timed_out
        self.wait_timeouts = []
        self.pid = 123456789  # Never passed to a real termination function.

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self.timed_out:
            raise subprocess.TimeoutExpired("fake migration", timeout)
        if self.release is not None and not self.release.wait(5):
            raise AssertionError("Test did not release fake subprocess")
        self.returncode = self.code
        return self.code

    def poll(self):
        return self.returncode


class _LocalFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source with spaces"
        self.output = self.root / "generated"
        self.source.mkdir()
        self.output.mkdir()
        (self.source / "single.twb").write_text("<workbook/>", encoding="utf-8")

    def payload(self, action="migration", **updates):
        result = {"action": action, "source_dir": str(self.source),
                  "output_dir": str(self.output)}
        result.update(updates)
        return result

    def write(self, relative, data="generated"):
        path = self.output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
        return path


class TestLocalCommands(_LocalFixture):
    def assert_script_flags_exist(self, command):
        # Check the real script's argparse declarations without importing GUI code.
        tree = ast.parse(Path(command[2]).read_text(encoding="utf-8-sig"))
        flags = {
            arg.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            for arg in node.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            and arg.value.startswith("--")
        }
        for arg in command[3:]:
            if arg.startswith("--"):
                self.assertIn(arg, flags, f"{arg} not declared by {command[2]}")

    def test_mode_defaults_to_auto_and_import_override_reaches_cli(self):
        normalized = local.validate_payload(self.payload())
        self.assertEqual(normalized["opts"]["model_mode"], "auto")
        for mode in ("auto", "import"):
            with self.subTest(mode=mode):
                payload = self.payload(opts={"model_mode": mode})
                request = local.validate_payload(payload)
                self.assertEqual(request["opts"]["model_mode"], mode)
                command = local.build_command(payload)
                self.assertEqual(command.count("--mode"), 1)
                index = command.index("--mode")
                self.assertEqual(command[index + 1], mode)
                self.assert_script_flags_exist(command)

    def test_mode_flat_and_nested_payloads_agree_and_duplicate_is_rejected(self):
        flat = self.payload(model_mode="import")
        nested = self.payload(opts={"model_mode": "import"})
        self.assertEqual(local.validate_payload(flat), local.validate_payload(nested))
        self.assertEqual(local.build_command(flat), local.build_command(nested))
        with self.assertRaisesRegex(ValueError, "same option"):
            local.validate_payload(self.payload(
                model_mode="auto", opts={"model_mode": "import"},
            ))

    def test_ui_mode_is_allowlisted_and_cannot_inject_cli_arguments(self):
        for mode in ("unsupported", "import --resume", "", None,
                     True, ["import"], {"mode": "import"}):
            with self.subTest(mode=mode), patch.object(local.subprocess, "Popen") as popen:
                with self.assertRaises(ValueError):
                    local.build_command(self.payload(opts={"model_mode": mode}))
                popen.assert_not_called()

    def test_single_workbook_folder_uses_bulk_migration(self):
        command = local.build_command(self.payload())
        self.assertEqual(command, [
            sys.executable, "-u", str(local.REPO_ROOT / "migrate.py"),
            "--batch", str(self.source), "--output-dir", str(self.output),
            "--output-format", "pbip", "--mode", "auto", "--quality-policy", "report"])
        self.assert_script_flags_exist(command)

    def test_fabric_culture_calendar_quality_and_resume_fixed_arguments(self):
        command = local.build_command(self.payload(opts={
            "output_format": "fabric", "culture": "fr-FR",
            "calendar_start": 2010, "calendar_end": 2040,
            "quality_policy": "production", "resume": True}))
        self.assertEqual(command, [
            sys.executable, "-u", str(local.REPO_ROOT / "migrate.py"),
            "--batch", str(self.source), "--output-dir", str(self.output),
            "--output-format", "fabric", "--mode", "auto", "--quality-policy", "production",
            "--culture", "fr-FR", "--calendar-start", "2010",
            "--calendar-end", "2040", "--resume"])
        self.assert_script_flags_exist(command)

    def test_probe_fixed_paths_and_no_healing(self):
        diag = self.output / "local_diagnostics" / "test-run"
        command = local.build_command(
            self.payload("probe", confirm_native=True), diagnostic_root=diag)
        self.assertEqual(command, [
            sys.executable, "-u", str(local.REPO_ROOT / "scripts/probe_projects.py"),
            str(self.output), "--shots", str(diag / "screenshots"),
            "--html", str(diag / "probe.html"),
            "--json", str(diag / "opening_report.json")])
        self.assertNotIn("--heal-from-desktop", command)
        self.assert_script_flags_exist(command)

    def test_probe_rejects_diagnostic_directory_outside_output(self):
        with self.assertRaisesRegex(ValueError, "inside output"):
            local.build_command(self.payload("probe", confirm_native=True),
                                diagnostic_root=self.source)

    def test_compare_fixed_flags_match_actual_script(self):
        command = local.build_command(self.payload(
            "compare", confirm_native=True,
            opts={"all_pages": True, "screen_comparison": "local"}))
        self.assertEqual(command, [
            sys.executable, "-u",
            str(local.REPO_ROOT / "scripts/compare_desktop_reports.py"),
            str(self.source), str(self.output),
            "--screen-comparison", "local", "--all-pages"])
        self.assert_script_flags_exist(command)
        self.assertFalse(any("heal" in arg for arg in command))

    def test_default_compare_is_manual_not_all_pages(self):
        command = local.build_command(self.payload("compare", confirm_native=True))
        self.assertEqual(command[-2:], ["--screen-comparison", "manual"])
        self.assertNotIn("--all-pages", command)

    def test_validation_has_no_filesystem_or_process_side_effects(self):
        target = self.root / "not-created"
        with patch.object(local.subprocess, "Popen") as popen:
            result = local.validate_payload(self.payload(output_dir=str(target)))
        self.assertEqual(result["output_dir"], str(target))
        self.assertFalse(target.exists())
        popen.assert_not_called()

    def test_flat_options_equal_nested_options_and_duplicates_rejected(self):
        opts = {"output_format": "fabric", "culture": "de-DE",
                "calendar_start": 2000, "calendar_end": 2050,
                "quality_policy": "enterprise", "resume": True}
        self.assertEqual(local.validate_payload(self.payload(**opts)),
                         local.validate_payload(self.payload(opts=opts)))
        self.assertEqual(local.build_command(self.payload(**opts)),
                         local.build_command(self.payload(opts=opts)))
        for key, value in opts.items():
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "same option"):
                local.validate_payload(self.payload(opts={key: value}, **{key: value}))

    def test_invalid_options_are_not_command_injection_surface(self):
        cases = [
            {"output_format": "pbip --resume"}, {"culture": "en-US; whoami"},
            {"calendar_start": True}, {"calendar_end": "2030"},
            {"calendar_start": 2040, "calendar_end": 2030},
            {"calendar_start": 0}, {"calendar_end": 10000},
            {"quality_policy": "ignore"}, {"resume": 1}, {"all_pages": "true"},
            {"screen_comparison": "cloud"}, {"args": ["--heal-from-desktop"]},
        ]
        for opts in cases:
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                local.build_command(self.payload(opts=opts))

    def test_invalid_or_nested_paths_rejected(self):
        cases = [
            {"source_dir": str(self.root / "missing")},
            {"output_dir": str(self.source)},
            {"output_dir": str(self.source / "nested")},
            {"output_dir": str(self.root)},
            {"output_dir": str(local.REPO_ROOT)},
            {"output_dir": str(Path(self.output.anchor))},
            {"source_dir": "//server/share"},
            {"output_dir": "bad\npath"},
            {"source_dir": str(self.source / "single.twb")},
        ]
        for updates in cases:
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                local.validate_payload(self.payload(**updates))

    def test_native_availability_checks_do_not_launch_or_import_gui(self):
        with patch.object(local.sys, "platform", "win32"), \
                patch.object(local.os, "environ", {
                    "POWERBI_DESKTOP_PATH": str(self.root / "PBIDesktop.exe"),
                    "TABLEAU_DESKTOP_PATH": str(self.root / "tableau.exe")}), \
                patch.object(local.subprocess, "Popen") as popen:
            self.assertIn("Power BI", local.native_unavailable("probe"))
            (self.root / "PBIDesktop.exe").touch()
            self.assertIsNone(local.native_unavailable("probe"))
            self.assertIn("Tableau", local.native_unavailable("compare"))
            (self.root / "tableau.exe").touch()
            self.assertIsNone(local.native_unavailable("compare"))
            self.assertIsNone(local.native_unavailable("migration"))
            popen.assert_not_called()

    def test_non_windows_native_unavailable(self):
        with patch.object(local.sys, "platform", "linux"):
            self.assertIn("Windows", local.native_unavailable("probe"))
            self.assertIn("Windows", local.native_unavailable("compare"))
            self.assertIsNone(local.native_unavailable("migration"))


class TestLocalHTTP(_LocalFixture):
    def setUp(self):
        super().setUp()
        # Assets are a separately built frontend; isolate route tests from it.
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.asset_bytes = {
            "index.html": b"<!doctype html><title>Local migration</title>",
            "app.js": b"'use strict';",
            "style.css": b"body { color: black; }",
        }
        for name, content in self.asset_bytes.items():
            (self.assets / name).write_bytes(content)
        self.enterContext(patch.object(local, "WEB_ROOT", self.assets))
        # Fail loudly if any route unexpectedly tries to run an application.
        self.popen = self.enterContext(patch.object(
            local.subprocess, "Popen",
            side_effect=AssertionError("Unexpected native process launch")))
        self.server = local.LocalServer(port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def request(self, path="/api/session", method="GET", body=None, headers=None):
        conn = http.client.HTTPConnection(local.HOST, self.server.server_port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def post(self, payload, path="/api/jobs", headers=None):
        defaults = {"Origin": self.server.origin,
                    "X-Local-Token": self.server.token,
                    "Content-Type": "application/json"}
        if headers:
            defaults.update(headers)
        return self.request(path, "POST", json.dumps(payload).encode(), defaults)

    def wait_job(self, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, _, body = self.request("/api/jobs")
            self.assertEqual(status, 200)
            job = next(j for j in json.loads(body)["jobs"] if j["id"] == job_id)
            # finished_at is written after process cleanup, unlike status.
            if job["finished_at"]:
                return job
            time.sleep(0.01)
        self.fail("Job did not finish within 5 seconds")

    def submit_fake(self, process=None):
        process = process or _FakeProcess()
        self.popen.side_effect = None
        self.popen.return_value = process
        status, _, body = self.post(self.payload())
        self.assertEqual(status, 202, body)
        job = self.wait_job(json.loads(body)["job"]["id"])
        return job, process

    def test_loopback_ephemeral_server_and_session(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertGreater(self.server.server_port, 0)
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        session = json.loads(body)
        self.assertEqual(session["origin"], self.server.origin)
        self.assertEqual(session["token"], self.server.token)
        self.assertGreaterEqual(len(session["token"]), 32)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.popen.assert_not_called()

    def test_assets_are_exactly_allowlisted(self):
        for route, name in [("/", "index.html"), ("/index.html", "index.html"),
                            ("/app.js", "app.js"), ("/style.css", "style.css")]:
            with self.subTest(route=route):
                status, headers, body = self.request(route)
                self.assertEqual(status, 200)
                self.assertEqual(body, self.asset_bytes[name])
                self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        for route in ["/local_server.py", "/../local_server.py",
                      "/%2e%2e/local_server.py", "/api/unknown"]:
            with self.subTest(route=route):
                self.assertEqual(self.request(route)[0], 404)
        self.assertEqual(json.loads(self.request("/api/jobs")[2]), {"jobs": []})
        self.popen.assert_not_called()

    def test_exact_host_rejection(self):
        for host in ["localhost:" + str(self.server.server_port), "127.0.0.1",
                     "127.0.0.1:1", "127.0.0.1.evil:" + str(self.server.server_port),
                     self.server.origin, "evil.example"]:
            with self.subTest(host=host):
                self.assertEqual(self.request(headers={"Host": host})[0], 403)

    def test_missing_and_duplicate_host_or_origin_rejected(self):
        for headers in [
            [],
            [("Host", f"127.0.0.1:{self.server.server_port}")] * 2,
            [("Host", f"127.0.0.1:{self.server.server_port}"),
             ("Origin", self.server.origin), ("Origin", self.server.origin)],
        ]:
            with self.subTest(headers=headers):
                conn = http.client.HTTPConnection(local.HOST, self.server.server_port,
                                                  timeout=5)
                try:
                    conn.putrequest("GET", "/api/session", skip_host=True)
                    for name, value in headers:
                        conn.putheader(name, value)
                    conn.endheaders()
                    response = conn.getresponse()
                    self.assertEqual(response.status, 403)
                    response.read()
                finally:
                    conn.close()

    def test_exact_origin_and_fetch_site_rejection(self):
        for origin in ["null", "https://evil.example", self.server.origin + "/",
                       self.server.origin.replace("127.0.0.1", "localhost"),
                       self.server.origin.replace("http:", "https:")]:
            with self.subTest(origin=origin):
                self.assertEqual(self.request(headers={"Origin": origin})[0], 403)
        self.assertEqual(self.request(headers={"Origin": self.server.origin})[0], 200)
        for site in ["cross-site", "same-site"]:
            self.assertEqual(self.request(headers={"Sec-Fetch-Site": site})[0], 403)

    def test_posts_require_both_origin_and_token(self):
        for headers in [
            {"Content-Type": "application/json"},
            {"Content-Type": "application/json", "Origin": self.server.origin},
            {"Content-Type": "application/json", "X-Local-Token": self.server.token},
            {"Content-Type": "application/json", "Origin": self.server.origin,
             "X-Local-Token": "incorrect"},
        ]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request(
                    "/api/jobs", "POST", b"{}", headers)[0], 403)
        self.popen.assert_not_called()

    def test_bad_json_content_type_and_oversize_rejected(self):
        headers = {"Origin": self.server.origin, "X-Local-Token": self.server.token,
                   "Content-Type": "application/json"}
        for body in [b"{broken", b"\xff", b"[]", b"null"]:
            with self.subTest(body=body):
                self.assertEqual(self.request("/api/jobs", "POST", body, headers)[0], 400)
        self.assertEqual(self.post(self.payload(), headers={
            "Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("/api/jobs", "POST",
                                     b" " * (local.MAX_PAYLOAD + 1), headers)[0], 413)
        self.assertEqual(self.post(self.payload(), headers={
            "Transfer-Encoding": "chunked"})[0], 400)
        self.popen.assert_not_called()

    def test_missing_invalid_and_duplicate_content_lengths_rejected(self):
        for lengths, expected in [([], 411), (["invalid"], 411),
                                  (["-1"], 411), (["2", "2"], 411),
                                  ([str(local.MAX_PAYLOAD + 1)], 413)]:
            with self.subTest(lengths=lengths):
                conn = http.client.HTTPConnection(local.HOST, self.server.server_port,
                                                  timeout=5)
                try:
                    conn.putrequest("POST", "/api/jobs")
                    conn.putheader("Origin", self.server.origin)
                    conn.putheader("X-Local-Token", self.server.token)
                    conn.putheader("Content-Type", "application/json")
                    for value in lengths:
                        conn.putheader("Content-Length", value)
                    conn.endheaders()
                    response = conn.getresponse()
                    self.assertEqual(response.status, expected)
                    response.read()
                finally:
                    conn.close()
        self.popen.assert_not_called()

    def test_unknown_action_fields_options_and_route_rejected(self):
        for payload in [self.payload("shell"), self.payload(command="whoami"),
                        self.payload(opts={"heal": True})]:
            with self.subTest(payload=payload):
                self.assertEqual(self.post(payload)[0], 400)
        self.assertEqual(self.post({}, path="/api/unknown")[0], 404)
        self.assertEqual(self.request("/api/download/missing")[0], 404)
        self.popen.assert_not_called()

    def test_diagnostics_require_explicit_boolean_confirmation(self):
        for action in ["probe", "compare"]:
            for confirm in [None, False, "true", 1]:
                with self.subTest(action=action, confirm=confirm):
                    self.assertEqual(self.post(self.payload(
                        action, confirm_native=confirm))[0], 400)
        self.popen.assert_not_called()

    def test_confirmed_unavailable_diagnostics_are_not_run(self):
        # Narrow platform mock guarantees no installed apps or GUI libraries needed.
        with patch.object(local.sys, "platform", "linux"):
            for action in ["probe", "compare"]:
                status, _, body = self.post(self.payload(action, confirm_native=True))
                self.assertEqual(status, 202)
                job = self.wait_job(json.loads(body)["job"]["id"])
                self.assertEqual(job["status"], "not_run")
                self.assertIn("Windows", job["error"])
                self.assertIsNone(job["exit_code"])
                self.assertTrue(job["logs"])
        self.popen.assert_not_called()

    def test_success_logs_and_safe_popen_no_automatic_diagnostics(self):
        job, process = self.submit_fake()
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["exit_code"], 0)
        self.assertIsNone(job["error"])
        self.assertEqual(job["logs"], ["first line", "second line"])
        self.assertTrue(job["started_at"])
        self.assertTrue(process.stdout.closed)
        self.assertEqual(process.wait_timeouts, [local.MIGRATION_TIMEOUT])
        self.popen.assert_called_once()
        args, kwargs = self.popen.call_args
        self.assertEqual(args[0], local.build_command(self.payload()))
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(kwargs["cwd"], str(local.REPO_ROOT))
        self.assertEqual(kwargs["env"]["PYTHONUTF8"], "1")
        self.assertEqual(kwargs["env"]["PYTHONUNBUFFERED"], "1")
        self.assertEqual(len(self.server.jobs.jobs), 1)
        self.assertFalse((self.output / "local_diagnostics").exists())

    def test_nonzero_subprocess_exit_is_failed_with_logs(self):
        job, _ = self.submit_fake(_FakeProcess(code=7, logs="diagnostic failure\n"))
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["exit_code"], 7)
        self.assertIn("7", job["error"])
        self.assertEqual(job["logs"], ["diagnostic failure"])

    def test_log_buffer_is_bounded_and_jobs_are_newest_first(self):
        lines = [f"log {i}" for i in range(local.MAX_LOG_LINES + 10)]
        first, _ = self.submit_fake(_FakeProcess(logs="\n".join(lines) + "\n"))
        self.assertEqual(first["logs"], lines[-local.MAX_LOG_LINES:])
        second, _ = self.submit_fake()
        jobs = json.loads(self.request("/api/jobs")[2])["jobs"]
        self.assertEqual([j["id"] for j in jobs], [second["id"], first["id"]])

    def test_log_read_failure_cannot_report_successful_exit_as_success(self):
        class BrokenStream(io.StringIO):
            def readline(self, size=-1):
                raise OSError("fake log stream failure")

        process = _FakeProcess()
        process.stdout = BrokenStream()
        job, _ = self.submit_fake(process)
        self.assertEqual(job["exit_code"], 0)
        self.assertEqual(job["status"], "failed")
        self.assertIn("fake log stream failure", job["log_error"])
        self.assertIn("fake log stream failure", job["error"])
        self.assertTrue(any("fake log stream failure" in line for line in job["logs"]))
        self.assertTrue(process.stdout.closed)
        self.assertFalse(self.server.jobs.processes)

    def test_explicit_available_probe_uses_diagnostic_timeout_and_single_process(self):
        self.popen.side_effect = None
        process = _FakeProcess()
        self.popen.return_value = process
        with patch.object(local, "native_unavailable", return_value=None):
            status, _, body = self.post(self.payload("probe", confirm_native=True))
            self.assertEqual(status, 202)
            job = self.wait_job(json.loads(body)["job"]["id"])
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(process.wait_timeouts, [local.DIAGNOSTIC_TIMEOUT])
        self.popen.assert_called_once()
        command = self.popen.call_args.args[0]
        self.assertEqual(command, local.build_command(
            self.payload("probe", confirm_native=True),
            diagnostic_root=self.output / "local_diagnostics" / job["id"]))
        self.assertNotIn("--heal-from-desktop", command)

    def test_subprocess_start_error_is_failed_not_http_crash(self):
        self.popen.side_effect = OSError("fake executable unavailable")
        status, _, body = self.post(self.payload())
        self.assertEqual(status, 202)
        job = self.wait_job(json.loads(body)["job"]["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("fake executable unavailable", job["error"])
        self.assertIn("fake executable unavailable", job["logs"])

    def test_timeout_terminates_only_fake_owned_process(self):
        process = _FakeProcess(timed_out=True)

        def terminate(owned):
            self.assertIs(owned, process)
            owned.returncode = -15

        with patch.object(local, "_terminate", side_effect=terminate) as stop, \
                patch.object(local, "MIGRATION_TIMEOUT", 0.125):
            job, _ = self.submit_fake(process)
        stop.assert_called_once_with(process)
        self.assertEqual(process.wait_timeouts, [0.125])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["exit_code"], -15)
        self.assertIn("timed out", job["error"])
        self.assertFalse(self.server.jobs.processes)

    def test_busy_job_download_and_folder_picker_conflict(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.popen.side_effect = None
        self.popen.return_value = _FakeProcess(release=release)
        status, _, body = self.post(self.payload())
        self.assertEqual(status, 202)
        job_id = json.loads(body)["job"]["id"]
        try:
            self.assertEqual(self.post(self.payload())[0], 409)
            self.assertEqual(self.post({}, path="/api/pick-folder")[0], 409)
            self.assertEqual(self.request("/api/download/" + job_id)[0], 409)
        finally:
            release.set()
        self.assertEqual(self.wait_job(job_id)["status"], "succeeded")
        self.popen.assert_called_once()

    def test_active_picker_blocks_submission_without_launch(self):
        self.server.jobs.picking = True
        try:
            self.assertEqual(self.post(self.payload())[0], 409)
        finally:
            self.server.jobs.picking = False
        self.popen.assert_not_called()

    def test_artifact_inventory_and_html_sandbox(self):
        html = "<script>alert('not allowed')</script><h1>Report</h1>"
        self.write("reports/report with space.html", html)
        self.write("summary.json", '{"ok": true}')
        self.write("data/private.json", "private")
        self.write(".env", "secret")
        self.write("config.json", "credentials")
        self.write("notes.txt", "not browsable")
        job, _ = self.submit_fake()
        self.assertEqual({a["path"] for a in job["artifacts"]},
                         {"reports/report with space.html", "summary.json"})
        artifact = next(a for a in job["artifacts"] if a["path"].endswith(".html"))
        self.assertIn("%20", artifact["url"])
        status, headers, body = self.request(artifact["url"])
        self.assertEqual(status, 200)
        self.assertEqual(body.decode(), html)
        self.assertIn("text/html", headers["Content-Type"])
        csp = headers["Content-Security-Policy"]
        directives = dict(
            (parts[0], parts[1:])
            for directive in csp.split(";")
            if (parts := directive.strip().split())
        )
        # Inline report tabs can run, but only inside an opaque-origin sandbox.
        self.assertEqual(directives["sandbox"], ["allow-scripts"])
        self.assertIn("default-src 'none'", csp)
        self.assertEqual(directives["script-src"], ["'unsafe-inline'"])
        self.assertEqual(directives["connect-src"], ["'none'"])
        self.assertEqual(directives["form-action"], ["'none'"])
        self.assertEqual(directives["base-uri"], ["'none'"])
        self.assertNotIn("allow-same-origin", csp)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_artifact_encoded_traversal_and_private_paths_rejected(self):
        self.write("safe.html")
        self.write("data/private.json", "PRIVATE")
        self.write("credentials.json", "SECRET")
        (self.root / "outside.html").write_text("OUTSIDE", encoding="utf-8")
        job, _ = self.submit_fake()
        prefix = f"/api/artifacts/{job['id']}/"
        for relative in ["../outside.html", "%2e%2e/outside.html",
                         "%2e%2e%2foutside.html", "%252e%252e/outside.html",
                         "%2e%2e%5coutside.html", "C%3A/outside.html",
                         "%2foutside.html", "safe.html%00", "./safe.html",
                         "data/private.json", "credentials.json", ".env",
                         "notes.txt"]:
            with self.subTest(relative=relative):
                status, _, body = self.request(prefix + relative)
                self.assertEqual(status, 404)
                self.assertNotIn(b"OUTSIDE", body)
                self.assertNotIn(b"PRIVATE", body)
                self.assertNotIn(b"SECRET", body)

    def test_reparse_artifact_and_zip_rejected(self):
        target = self.write("linked.html", "MUST NOT BE SERVED")
        job, _ = self.submit_fake()
        real_lstat = Path.lstat

        def lstat(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if path == target:
                class Reparse:
                    st_mode = info.st_mode
                    st_file_attributes = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                return Reparse()
            return info

        with patch.object(Path, "lstat", lstat):
            status, _, body = self.request(f"/api/artifacts/{job['id']}/linked.html")
            self.assertEqual(status, 404)
            self.assertNotIn(b"MUST NOT BE SERVED", body)
            self.assertEqual(self.request(job["download_url"])[0], 404)
            snapshot = json.loads(self.request("/api/jobs")[2])["jobs"][0]
            self.assertIn("reparse", snapshot["artifact_error"])

    def test_zip_contains_generated_project_not_source_or_private_paths(self):
        expected = {
            "Report.pbip": '{"version": "1.0"}',
            "Report.Report/.platform": '{"metadata": {"type": "Report"}}',
            "Model.SemanticModel/.platform": '{"metadata": {"type": "SemanticModel"}}',
            "Report.Report/definition/report.json": '{"sections": []}',
            "Model.SemanticModel/definition/model.tmdl": "model Model",
            "Model.SemanticModel/definition/expressions.tmdl": "expression Source = 1",
            "summary.html": "<h1>Migration</h1>",
        }
        for name, data in expected.items():
            self.write(name, data)
        for name in [".env", "config.json", "credentials.json", "secrets.json",
                     ".git/config", ".pbi/cache.json", ".cache/private.json",
                     ".platform/private.json", ".hidden/.platform",
                     "source_snapshots/source.json",
                     "input.twb", "input.twbx", "extract.hyper", "extract.tde"]:
            self.write(name, "PRIVATE")
        (self.root / "outside.json").write_text("OUTSIDE", encoding="utf-8")
        job, _ = self.submit_fake()
        self.assertFalse(any(a["path"].endswith(".platform") for a in job["artifacts"]))
        self.assertEqual(self.request(
            f"/api/artifacts/{job['id']}/Report.Report/.platform")[0], 404)
        status, headers, body = self.request(job["download_url"])
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertIn("attachment;", headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(set(archive.namelist()),
                             {"generated-output/" + name for name in expected})
            for name, data in expected.items():
                self.assertEqual(archive.read("generated-output/" + name).decode(), data)
        # A download identifies a known job, never an arbitrary filesystem input.
        self.assertEqual(self.request("/api/download/" + quote(str(self.source), safe=""))[0],
                         404)
        self.assertEqual(self.request("/api/download/../outside.json")[0], 404)

    def test_empty_output_and_zip_resource_limits(self):
        job, _ = self.submit_fake()
        self.assertEqual(self.request(job["download_url"])[0], 404)
        self.write("generated.json", '{"generated": true}')
        with patch.object(local, "MAX_ARCHIVE_BYTES", 1):
            self.assertEqual(self.request(job["download_url"])[0], 413)
        with patch.object(local, "MAX_ARCHIVE_FILES", 0):
            self.assertEqual(self.request(job["download_url"])[0], 413)


if __name__ == "__main__":
    unittest.main()
