"""Tests for the unified LLM gateway (Sprint 221)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'powerbi_import'))

from powerbi_import.llm_gateway import (  # noqa: E402
    LLMGateway, LLMResult, _host_port, DEFAULT_LOCAL_URL,
)


def _gw(**kw):
    # Construct with a clean env baseline unless overridden.
    return LLMGateway(**kw)


class TestHostPort(unittest.TestCase):
    def test_https_default_port(self):
        self.assertEqual(_host_port("https://api.openai.com/v1"), ("api.openai.com", 443))

    def test_explicit_port(self):
        self.assertEqual(_host_port("http://localhost:11434/v1"), ("localhost", 11434))

    def test_bare_host(self):
        self.assertEqual(_host_port("example.com"), ("example.com", 80))

    def test_empty(self):
        self.assertEqual(_host_port(""), ("localhost", 80))


class TestConstruction(unittest.TestCase):
    def test_defaults(self):
        g = _gw(mode="auto")
        self.assertEqual(g.mode, "auto")
        self.assertEqual(g.local_url, DEFAULT_LOCAL_URL)

    def test_invalid_mode_falls_back_auto(self):
        self.assertEqual(_gw(mode="nonsense").mode, "auto")

    def test_env_config(self):
        with mock.patch.dict(os.environ, {"LLM_MODE": "offline",
                                          "LLM_LOCAL_URL": "http://h:9/v1"}, clear=False):
            g = LLMGateway()
            self.assertEqual(g.mode, "offline")
            self.assertEqual(g.local_url, "http://h:9/v1")


class TestResolveRouting(unittest.TestCase):
    def test_offline_local_available(self):
        g = _gw(mode="offline")
        with mock.patch.object(g, "is_online", return_value=True):
            self.assertEqual(g.resolve()[0], "local")

    def test_offline_no_local(self):
        g = _gw(mode="offline")
        with mock.patch.object(g, "is_online", return_value=False):
            self.assertEqual(g.resolve()[0], "none")

    def test_online_needs_key_and_reachable(self):
        g = _gw(mode="online", api_key="k")
        with mock.patch.object(g, "is_online", return_value=True):
            route, provider, _ = g.resolve()
            self.assertEqual(route, "cloud")
            self.assertEqual(provider, "openai")

    def test_online_no_key(self):
        g = _gw(mode="online", api_key="")
        with mock.patch.object(g, "is_online", return_value=True):
            self.assertEqual(g.resolve()[0], "none")

    def test_auto_prefers_local(self):
        g = _gw(mode="auto", api_key="k")
        with mock.patch.object(g, "is_online", return_value=True):
            self.assertEqual(g.resolve()[0], "local")

    def test_auto_falls_back_to_cloud(self):
        g = _gw(mode="auto", api_key="k")

        def only_cloud_online(target=None):
            # local probe False, cloud probe True
            host = target[0] if target else ""
            return host == "api.openai.com"

        with mock.patch.object(g, "is_online", side_effect=only_cloud_online):
            self.assertEqual(g.resolve()[0], "cloud")

    def test_auto_none_when_nothing(self):
        g = _gw(mode="auto", api_key="")
        with mock.patch.object(g, "is_online", return_value=False):
            self.assertEqual(g.resolve()[0], "none")


class TestCall(unittest.TestCase):
    def test_none_route_returns_offline_result(self):
        g = _gw(mode="offline")
        with mock.patch.object(g, "is_online", return_value=False):
            r = g.complete("fix this DAX")
        self.assertIsInstance(r, LLMResult)
        self.assertEqual(r.route, "none")
        self.assertEqual(r.source, "offline")
        self.assertFalse(bool(r))

    def test_local_call_uses_client(self):
        g = _gw(mode="offline")
        fake = mock.Mock()
        fake.call.return_value = {"text": "FIXED DAX", "input_tokens": 5,
                                  "output_tokens": 3, "cost": 0.0}
        with mock.patch.object(g, "is_online", return_value=True), \
             mock.patch.object(g, "_make_client", return_value=fake):
            r = g.complete("bad dax", system="sys")
        self.assertEqual(r.text, "FIXED DAX")
        self.assertEqual(r.route, "local")
        self.assertEqual(r.source, "llm")
        self.assertTrue(bool(r))

    def test_complete_and_call_parity(self):
        g = _gw(mode="offline")
        fake = mock.Mock()
        fake.call.return_value = {"text": "X", "cost": 0.0}
        with mock.patch.object(g, "is_online", return_value=True), \
             mock.patch.object(g, "_make_client", return_value=fake):
            r1 = g.complete("p")
            r2 = g.call("You are a helpful assistant.", "p")
        self.assertEqual(r1.text, r2.text)

    def test_cache_hit(self):
        g = _gw(mode="offline")
        fake = mock.Mock()
        fake.call.return_value = {"text": "CACHED", "cost": 0.0}
        with mock.patch.object(g, "is_online", return_value=True), \
             mock.patch.object(g, "_make_client", return_value=fake):
            g.complete("same prompt")
            r2 = g.complete("same prompt")
        self.assertTrue(r2.cached)
        self.assertEqual(fake.call.call_count, 1)  # second served from cache

    def test_call_budget(self):
        g = _gw(mode="offline", max_calls=1)
        fake = mock.Mock()
        fake.call.return_value = {"text": "A", "cost": 0.0}
        with mock.patch.object(g, "is_online", return_value=True), \
             mock.patch.object(g, "_make_client", return_value=fake):
            g.complete("p1")
            r2 = g.complete("p2")  # different prompt, exceeds budget
        self.assertEqual(r2.error, "call_budget_exceeded")

    def test_cost_budget(self):
        g = _gw(mode="offline", max_cost_usd=0.001)
        fake = mock.Mock()
        fake.call.return_value = {"text": "A", "cost": 0.005}
        with mock.patch.object(g, "is_online", return_value=True), \
             mock.patch.object(g, "_make_client", return_value=fake):
            g.complete("p1")            # spends 0.005 > cap
            r2 = g.complete("p2")
        self.assertEqual(r2.error, "cost_budget_exceeded")

    def test_redaction_applied_before_send(self):
        g = _gw(mode="offline", redact=True)
        fake = mock.Mock()
        fake.call.return_value = {"text": "ok", "cost": 0.0}
        with mock.patch.object(g, "is_online", return_value=True), \
             mock.patch.object(g, "_make_client", return_value=fake), \
             mock.patch("security_validator.redact_credentials",
                        side_effect=lambda t: t.replace("SECRET", "***")) as red:
            g.complete("contains SECRET value")
        self.assertTrue(red.called)
        sent_user = fake.call.call_args[0][1]
        self.assertNotIn("SECRET", sent_user)

    def test_client_error_surfaced(self):
        g = _gw(mode="offline")
        fake = mock.Mock()
        fake.call.return_value = {"text": "", "cost": 0.0, "error": "http_500"}
        with mock.patch.object(g, "is_online", return_value=True), \
             mock.patch.object(g, "_make_client", return_value=fake):
            r = g.complete("p")
        self.assertEqual(r.error, "http_500")
        self.assertEqual(r.source, "offline")


class TestStatus(unittest.TestCase):
    def test_status_no_secrets(self):
        g = _gw(mode="auto", api_key="supersecret")
        with mock.patch.object(g, "is_online", return_value=False):
            st = g.status()
        blob = str(st)
        self.assertNotIn("supersecret", blob)
        self.assertIn("mode", st)
        self.assertIn("route", st)
        self.assertTrue(st["has_api_key"])

    def test_status_reports_disabled_when_offline(self):
        g = _gw(mode="online", api_key="")
        with mock.patch.object(g, "is_online", return_value=False):
            st = g.status()
        self.assertFalse(st["enabled"])
        self.assertEqual(st["route"], "none")


class TestRemediationWiring(unittest.TestCase):
    def test_refine_with_gateway_result(self):
        from powerbi_import.remediation import remediate_findings, refine_with_llm
        rep = remediate_findings([{"name": "x", "severity": "warn", "detail": "forecast"}])
        g = _gw(mode="offline")
        fake = mock.Mock()
        fake.call.return_value = {"text": "Use a DAX measure instead.", "cost": 0.0}
        with mock.patch.object(g, "is_online", return_value=True), \
             mock.patch.object(g, "_make_client", return_value=fake):
            refine_with_llm(rep, g)
        self.assertEqual(rep.suggestions[0].source, "llm")
        self.assertIn("DAX measure", rep.suggestions[0].suggested_action)


class TestLocalProvider(unittest.TestCase):
    def test_llm_client_local_url_build(self):
        from powerbi_import.llm_client import LLMClient
        c = LLMClient(provider="local", endpoint="http://localhost:11434/v1")
        self.assertTrue(c._build_url().endswith("/v1/chat/completions"))
        # local without key → no auth header
        self.assertNotIn("Authorization", c._build_headers())

    def test_local_requires_endpoint(self):
        from powerbi_import.llm_client import LLMClient
        with self.assertRaises(ValueError):
            LLMClient(provider="local")


class TestMCPLLMStatusTool(unittest.TestCase):
    def test_tool_present_and_runs(self):
        from powerbi_import.mcp_server import MigrationTools, _tool_catalogue
        names = {t["name"] for t in _tool_catalogue()}
        self.assertIn("llm_status", names)
        res = MigrationTools().llm_status({"mode": "offline"})
        self.assertTrue(res["ok"])
        self.assertIn("status", res)

    def test_refuses_secret_arg(self):
        from powerbi_import.mcp_server import MigrationTools
        res = MigrationTools().llm_status({"api_key": "x"})
        self.assertFalse(res["ok"])

    def test_invalid_mode(self):
        from powerbi_import.mcp_server import MigrationTools
        res = MigrationTools().llm_status({"mode": "bogus"})
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
