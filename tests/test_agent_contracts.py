"""Agent-surface contract snapshots (v44, Sprint 219.1).

Guards the MCP tool contract and the skill command surface so changes can't
silently break agents. Update the golden constants deliberately when the
contract intentionally changes.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.mcp_server import (  # noqa: E402
    _tool_catalogue, _resource_catalogue, PROTOCOL_VERSION, SERVER_NAME,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── Golden contract ─────────────────────────────────────────────────
GOLDEN_TOOLS = {
    "assess": {"file"},
    "migrate": {"file"},
    "qa": {"project_dir"},
    "parity_scan": {"file"},
    "shared_model": {"files"},
    "diff": {"extraction_dir", "project_dir"},
    "deploy": {"project_dir", "workspace_id"},
    "llm_status": set(),
    "autoheal": {"project_dir"},
    "verify_open": {"project_dir"},
}

GOLDEN_RESOURCES = {
    "ttpbi://reports/assessment",
    "ttpbi://reports/qa",
    "ttpbi://reports/parity",
}


class TestToolContract(unittest.TestCase):
    def test_tool_names_match_golden(self):
        names = {t["name"] for t in _tool_catalogue()}
        self.assertEqual(names, set(GOLDEN_TOOLS))

    def test_required_fields_match_golden(self):
        for tool in _tool_catalogue():
            required = set(tool["inputSchema"].get("required", []))
            self.assertEqual(required, GOLDEN_TOOLS[tool["name"]],
                             f"required fields drifted for {tool['name']}")

    def test_tool_order_is_stable(self):
        names = [t["name"] for t in _tool_catalogue()]
        self.assertEqual(names, ["assess", "migrate", "qa", "parity_scan",
                                 "shared_model", "diff", "deploy", "llm_status",
                                 "autoheal", "verify_open"])

    def test_resource_contract(self):
        uris = {r["uri"] for r in _resource_catalogue()}
        self.assertEqual(uris, GOLDEN_RESOURCES)

    def test_protocol_metadata(self):
        self.assertEqual(SERVER_NAME, "tableau-to-powerbi")
        self.assertTrue(PROTOCOL_VERSION)


class TestSkillCommandSurface(unittest.TestCase):
    """The skill must document the canonical commands agents rely on."""

    def setUp(self):
        skill = os.path.join(_ROOT, ".github", "skills",
                             "tableau-to-powerbi", "SKILL.md")
        with open(skill, encoding="utf-8") as fh:
            self.text = fh.read()

    def test_documents_core_commands(self):
        for token in ("migrate.py", "--assess", "--output-dir", "--qa", "--batch"):
            self.assertIn(token, self.text, f"skill missing {token}")

    def test_documents_ownership_map(self):
        for agent in ("@dax", "@wiring", "@semantic", "@visual", "@extractor"):
            self.assertIn(agent, self.text)

    def test_documents_pipeline(self):
        self.assertIn("Extraction", self.text)
        self.assertIn("Generation", self.text)


if __name__ == "__main__":
    unittest.main()
