"""End-to-end agent-surface release tests (v44, Sprint 220.2).

Exercises the skill lint + MCP contract + remediation + conversational assess
chain, and (when a sample workbook is present) a full assess → remediate → plan
flow through the real engine.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))

from scripts.validate_skills import validate_skills  # noqa: E402
from powerbi_import.mcp_server import MCPServer, _tool_catalogue  # noqa: E402
from powerbi_import.remediation import remediate_assessment  # noqa: E402
from powerbi_import.conversational import answer_question, build_plan_summary  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SAMPLE = os.path.join(_ROOT, "examples", "tableau_samples", "Superstore_Sales.twb")


class TestReleaseArtifacts(unittest.TestCase):
    def test_version_is_44(self):
        with open(os.path.join(_ROOT, "pyproject.toml"), encoding="utf-8") as fh:
            self.assertIn('version = "44.0.0"', fh.read())
        import powerbi_import
        self.assertEqual(powerbi_import.__version__, "44.0.0")

    def test_agent_surface_doc_exists(self):
        self.assertTrue(os.path.isfile(os.path.join(_ROOT, "docs", "AGENT_SURFACE.md")))

    def test_changelog_has_v44(self):
        with open(os.path.join(_ROOT, "CHANGELOG.md"), encoding="utf-8") as fh:
            self.assertIn("v44.0.0", fh.read())


class TestSkillLintClean(unittest.TestCase):
    def test_all_skills_pass(self):
        result = validate_skills()
        self.assertTrue(result["ok"], result["errors"])


class TestMCPContractStable(unittest.TestCase):
    def test_seven_tools(self):
        self.assertEqual(len(_tool_catalogue()), 10)

    def test_initialize_and_list(self):
        srv = MCPServer()
        init = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(init["result"]["serverInfo"]["version"], "44.0.0")
        lst = srv.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(len(lst["result"]["tools"]), 10)


class TestRemediationChain(unittest.TestCase):
    def test_synthetic_chain(self):
        payload = {
            "workbook_name": "WB", "overall_score": "RED",
            "totals": {"checks": 2, "pass": 0, "warn": 1, "fail": 1},
            "categories": [
                {"name": "Calculation", "checks": [
                    {"name": "LOD", "severity": "warn", "detail": "FIXED LOD", "recommendation": ""}]},
                {"name": "Data Model", "checks": [
                    {"name": "RLS", "severity": "fail", "detail": "user filter security",
                     "recommendation": "Assign AD members"}]},
            ],
        }
        rem = remediate_assessment(payload)
        self.assertEqual(len(rem.suggestions), 2)
        ans = answer_question(payload, "what won't migrate cleanly?")
        self.assertGreaterEqual(ans.data["failures"], 1)
        plan = build_plan_summary(payload)
        self.assertEqual(plan["estimated_effort"], "high")


@unittest.skipUnless(os.path.isfile(_SAMPLE), "sample workbook not present")
class TestFullFlow(unittest.TestCase):
    def test_assess_remediate_plan(self):
        srv = MCPServer()
        resp = srv.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "assess", "arguments": {"file": _SAMPLE}},
        })
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertTrue(payload["ok"], payload)
        report = payload["report"]

        rem = remediate_assessment(report)
        self.assertIsNotNone(rem.to_dict())

        plan = build_plan_summary(report)
        self.assertIn(plan["overall_score"], ("GREEN", "YELLOW", "RED"))
        self.assertTrue(plan["steps"])


if __name__ == "__main__":
    unittest.main()
