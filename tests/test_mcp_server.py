"""Tests for the migration MCP server (v44, Sprint 216.5)."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))

from powerbi_import.mcp_server import (  # noqa: E402
    MCPServer, MigrationTools, _tool_catalogue, _resource_catalogue,
    _validate_input_file, METHOD_NOT_FOUND, INVALID_REQUEST, PARSE_ERROR,
    INVALID_PARAMS, SERVER_NAME, SERVER_VERSION,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SAMPLE = os.path.join(_ROOT, "examples", "tableau_samples", "Superstore_Sales.twb")


def _req(method, params=None, req_id=1):
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}


class TestProtocol(unittest.TestCase):
    def setUp(self):
        self.server = MCPServer()

    def test_initialize(self):
        resp = self.server.handle_request(_req("initialize"))
        info = resp["result"]["serverInfo"]
        self.assertEqual(info["name"], SERVER_NAME)
        self.assertEqual(info["version"], SERVER_VERSION)
        self.assertIn("capabilities", resp["result"])

    def test_ping(self):
        resp = self.server.handle_request(_req("ping"))
        self.assertEqual(resp["result"], {})

    def test_tools_list(self):
        resp = self.server.handle_request(_req("tools/list"))
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(names, ["assess", "migrate", "qa", "parity_scan",
                                 "shared_model", "diff", "deploy", "llm_status",
                                 "autoheal", "verify_open"])

    def test_resources_list(self):
        resp = self.server.handle_request(_req("resources/list"))
        uris = [r["uri"] for r in resp["result"]["resources"]]
        self.assertIn("ttpbi://reports/assessment", uris)

    def test_unknown_method(self):
        resp = self.server.handle_request(_req("does/not/exist"))
        self.assertEqual(resp["error"]["code"], METHOD_NOT_FOUND)

    def test_invalid_jsonrpc(self):
        resp = self.server.handle_request({"id": 1, "method": "ping"})
        self.assertEqual(resp["error"]["code"], INVALID_REQUEST)

    def test_notification_returns_none(self):
        # No 'id' → notification; unknown method notification is swallowed.
        resp = self.server.handle_request({"jsonrpc": "2.0", "method": "unknown"})
        self.assertIsNone(resp)

    def test_handle_line_parse_error(self):
        out = self.server.handle_line("{not json")
        self.assertEqual(json.loads(out)["error"]["code"], PARSE_ERROR)

    def test_handle_line_roundtrip(self):
        out = self.server.handle_line(json.dumps(_req("tools/list")))
        self.assertIn("assess", out)


class TestToolCatalogue(unittest.TestCase):
    def test_every_tool_has_schema(self):
        for tool in _tool_catalogue():
            self.assertIn("name", tool)
            self.assertIn("description", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertIn("required", tool["inputSchema"])

    def test_every_tool_has_handler(self):
        tools = MigrationTools()
        for tool in _tool_catalogue():
            self.assertTrue(callable(getattr(tools, tool["name"], None)),
                            f"missing handler for {tool['name']}")

    def test_resource_catalogue_shape(self):
        for res in _resource_catalogue():
            self.assertTrue(res["uri"].startswith("ttpbi://reports/"))
            self.assertEqual(res["mimeType"], "application/json")


class TestInputValidation(unittest.TestCase):
    def test_missing_file(self):
        self.assertIn("missing", _validate_input_file(None))

    def test_null_byte(self):
        self.assertIn("null byte", _validate_input_file("a\x00b.twbx"))

    def test_bad_extension(self):
        self.assertIn("unsupported", _validate_input_file("report.pdf"))

    def test_not_found(self):
        self.assertIn("not found", _validate_input_file("nope.twbx"))


class TestToolsCall(unittest.TestCase):
    def setUp(self):
        self.server = MCPServer()

    def _call(self, name, arguments):
        resp = self.server.handle_request(
            _req("tools/call", {"name": name, "arguments": arguments}))
        payload = json.loads(resp["result"]["content"][0]["text"])
        return resp, payload

    def test_unknown_tool(self):
        resp = self.server.handle_request(
            _req("tools/call", {"name": "frobnicate", "arguments": {}}))
        self.assertEqual(resp["error"]["code"], METHOD_NOT_FOUND)

    def test_assess_missing_file_is_error(self):
        resp, payload = self._call("assess", {"file": "nope.twbx"})
        self.assertTrue(resp["result"]["isError"])
        self.assertFalse(payload["ok"])

    def test_migrate_bad_format(self):
        # Bad format is caught before extraction, so a nonexistent file with a
        # valid extension still fails on the file check first.
        resp, payload = self._call("migrate", {"file": "x.twbx", "output_format": "xml"})
        self.assertFalse(payload["ok"])

    def test_qa_bad_dir(self):
        resp, payload = self._call("qa", {"project_dir": "/no/such/dir"})
        self.assertFalse(payload["ok"])

    def test_shared_model_needs_two(self):
        resp, payload = self._call("shared_model", {"files": ["a.twbx"]})
        self.assertFalse(payload["ok"])
        self.assertIn("two", payload["error"])

    def test_diff_missing_dirs(self):
        resp, payload = self._call("diff", {"extraction_dir": "/no", "project_dir": "/no"})
        self.assertFalse(payload["ok"])


class TestDeployGuard(unittest.TestCase):
    def setUp(self):
        self.tools = MigrationTools()

    def test_dry_run_by_default(self):
        # Use repo root as an existing dir stand-in for project_dir.
        res = self.tools.deploy({"project_dir": _ROOT, "workspace_id": "ws1"})
        self.assertTrue(res["ok"])
        self.assertFalse(res["performed"])
        self.assertTrue(res["dry_run"])

    def test_refuses_secret_in_args(self):
        res = self.tools.deploy({"project_dir": _ROOT, "workspace_id": "ws1",
                                 "client_secret": "shh"})
        self.assertFalse(res["ok"])
        self.assertIn("credentials", res["error"])

    def test_confirm_but_dry_run_does_nothing(self):
        res = self.tools.deploy({"project_dir": _ROOT, "workspace_id": "ws1",
                                 "confirm": True, "dry_run": True})
        self.assertFalse(res["performed"])

    def test_missing_workspace(self):
        res = self.tools.deploy({"project_dir": _ROOT, "workspace_id": ""})
        self.assertFalse(res["ok"])


class TestParityScanGraceful(unittest.TestCase):
    def test_parity_scan_unavailable_is_ok(self):
        # parity_registry is not shipped yet (Sprint 209 pending) → graceful.
        tools = MigrationTools()
        if not os.path.isfile(_SAMPLE):
            self.skipTest("sample workbook not present")
        res = tools.parity_scan({"file": _SAMPLE})
        self.assertTrue(res["ok"])
        # Either a real scan or the documented 'unavailable' status.
        self.assertIn("report", res)


@unittest.skipUnless(os.path.isfile(_SAMPLE), "sample workbook not present")
class TestAssessIntegration(unittest.TestCase):
    """End-to-end: MCP assess tool over a real .twb workbook."""

    def setUp(self):
        self.server = MCPServer()

    def test_assess_real_workbook(self):
        resp = self.server.handle_request(
            _req("tools/call", {"name": "assess", "arguments": {"file": _SAMPLE}}))
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"], payload)
        self.assertIn("overall_score", payload["report"])

    def test_assess_then_resource_read(self):
        self.server.handle_request(
            _req("tools/call", {"name": "assess", "arguments": {"file": _SAMPLE}}))
        resp = self.server.handle_request(
            _req("resources/read", {"uri": "ttpbi://reports/assessment"}))
        text = resp["result"]["contents"][0]["text"]
        self.assertIn("overall_score", text)

    def test_resource_read_without_run_errors(self):
        resp = self.server.handle_request(
            _req("resources/read", {"uri": "ttpbi://reports/qa"}))
        self.assertEqual(resp["error"]["code"], INVALID_PARAMS)


if __name__ == "__main__":
    unittest.main()
