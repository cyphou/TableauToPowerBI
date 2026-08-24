"""Agent-surface safety guards (v44, Sprint 219.2).

Asserts the agent surface never leaks secrets, gates deployment, and keeps the
LLM path opt-in.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.mcp_server import MigrationTools, _tool_catalogue  # noqa: E402
from powerbi_import.remediation import (  # noqa: E402
    remediate_findings, refine_with_llm, _build_prompt, explain_finding,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestDeployGating(unittest.TestCase):
    def setUp(self):
        self.tools = MigrationTools()

    def test_no_push_without_confirm(self):
        res = self.tools.deploy({"project_dir": _ROOT, "workspace_id": "ws"})
        self.assertFalse(res["performed"])

    def test_rejects_secretish_arg_names(self):
        for key in ("client_secret", "api_key", "password", "access_token"):
            res = self.tools.deploy({"project_dir": _ROOT, "workspace_id": "ws", key: "x"})
            self.assertFalse(res["ok"], f"{key} should be rejected")
            self.assertIn("credentials", res["error"])

    def test_deploy_tool_description_states_env_only(self):
        deploy = next(t for t in _tool_catalogue() if t["name"] == "deploy")
        self.assertIn("environment", deploy["description"].lower())


class TestNoSecretsInReports(unittest.TestCase):
    def test_remediation_prompt_has_no_secret_fields(self):
        s = explain_finding({"name": "x", "detail": "gateway credential"})
        prompt = _build_prompt(s)
        self.assertNotRegex(prompt, r"(?i)(password|secret|token)\s*[:=]\s*\S")

    def test_remediation_output_serializable_no_creds(self):
        rep = remediate_findings([{"name": "conn", "severity": "warn",
                                   "detail": "connection string gateway"}])
        blob = str(rep.to_dict())
        self.assertNotIn("sk-", blob)


class TestLLMOptIn(unittest.TestCase):
    def test_llm_disabled_by_default(self):
        rep = remediate_findings([{"name": "x", "severity": "warn", "detail": "forecast"}])
        # No client → stays on template source (offline path).
        rep = refine_with_llm(rep, None)
        self.assertTrue(all(s.source == "template" for s in rep.suggestions))


class TestSkillSafetyGuidance(unittest.TestCase):
    def setUp(self):
        skill = os.path.join(_ROOT, ".github", "skills",
                             "tableau-to-powerbi", "SKILL.md")
        with open(skill, encoding="utf-8") as fh:
            self.text = fh.read()

    def test_skill_warns_about_secrets(self):
        lowered = self.text.lower()
        self.assertTrue("secret" in lowered or "token" in lowered)
        self.assertIn("terminal", lowered)

    def test_skill_has_no_embedded_real_secret(self):
        self.assertNotRegex(self.text, r"--token-secret\s+[A-Za-z0-9_\-]{12,}")
        self.assertNotRegex(self.text, r"\bsk-[A-Za-z0-9]{16,}\b")


if __name__ == "__main__":
    unittest.main()
