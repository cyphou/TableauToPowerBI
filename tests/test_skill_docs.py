"""Tests for the repo Copilot skill docs + linter (v44, Sprint 215.5)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from scripts.validate_skills import (  # noqa: E402
    validate_skills, validate_skill, _split_frontmatter,
    _frontmatter_field, _known_cli_flags,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKILL = os.path.join(_ROOT, ".github", "skills", "tableau-to-powerbi", "SKILL.md")


class TestFrontmatter(unittest.TestCase):
    def test_split_frontmatter_present(self):
        fm, body = _split_frontmatter("---\nname: x\ndescription: y\n---\nbody here")
        self.assertIn("name: x", fm)
        self.assertIn("body here", body)

    def test_split_frontmatter_absent(self):
        fm, body = _split_frontmatter("no frontmatter here")
        self.assertEqual(fm, "")
        self.assertEqual(body, "no frontmatter here")

    def test_scalar_field(self):
        fm = "name: tableau-to-powerbi\ndescription: hello"
        self.assertEqual(_frontmatter_field(fm, "name"), "tableau-to-powerbi")
        self.assertEqual(_frontmatter_field(fm, "description"), "hello")

    def test_folded_field(self):
        fm = "name: x\ndescription: >-\n  line one\n  line two\n"
        val = _frontmatter_field(fm, "description")
        self.assertIn("line one", val)
        self.assertIn("line two", val)

    def test_missing_field(self):
        self.assertEqual(_frontmatter_field("name: x", "description"), "")


class TestKnownFlags(unittest.TestCase):
    def test_scrapes_core_flags(self):
        flags = _known_cli_flags()
        self.assertIn("--output-dir", flags)
        self.assertIn("--assess", flags)
        self.assertIn("--help", flags)


class TestSkillFile(unittest.TestCase):
    def test_skill_file_exists(self):
        self.assertTrue(os.path.isfile(_SKILL))

    def test_skill_has_name_and_description(self):
        with open(_SKILL, encoding="utf-8") as fh:
            fm, _ = _split_frontmatter(fh.read())
        self.assertEqual(_frontmatter_field(fm, "name"), "tableau-to-powerbi")
        self.assertTrue(len(_frontmatter_field(fm, "description")) > 40)

    def test_skill_passes_linter(self):
        errors = validate_skill(_SKILL)
        self.assertEqual(errors, [], f"skill lint errors: {errors}")

    def test_reference_bundle_present(self):
        ref = os.path.join(os.path.dirname(_SKILL), "references")
        for name in ("flags.md", "reading-reports.md", "deploy-runbook.md"):
            self.assertTrue(os.path.isfile(os.path.join(ref, name)), name)


class TestValidateSkillsAggregate(unittest.TestCase):
    def test_all_skills_clean(self):
        result = validate_skills()
        self.assertTrue(result["ok"], result["errors"])
        self.assertGreaterEqual(result["files"], 1)

    def test_broken_link_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = os.path.join(tmp, "SKILL.md")
            with open(skill, "w", encoding="utf-8") as fh:
                fh.write("---\nname: t\ndescription: d\n---\n[x](missing.md)\n")
            errors = validate_skill(skill, known_flags={"--help"})
            self.assertTrue(any("broken relative link" in e for e in errors))

    def test_missing_frontmatter_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = os.path.join(tmp, "SKILL.md")
            with open(skill, "w", encoding="utf-8") as fh:
                fh.write("no frontmatter\n")
            errors = validate_skill(skill, known_flags={"--help"})
            self.assertTrue(any("frontmatter" in e for e in errors))

    def test_unknown_flag_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = os.path.join(tmp, "SKILL.md")
            with open(skill, "w", encoding="utf-8") as fh:
                fh.write("---\nname: t\ndescription: d\n---\nrun --nonexistent-flag now\n")
            errors = validate_skill(skill, known_flags={"--help"})
            self.assertTrue(any("--nonexistent-flag" in e for e in errors))

    def test_embedded_secret_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill = os.path.join(tmp, "SKILL.md")
            with open(skill, "w", encoding="utf-8") as fh:
                fh.write("---\nname: t\ndescription: d\n---\n"
                         "--token-secret ABCDEF1234567890XYZ\n")
            errors = validate_skill(skill, known_flags={"--help", "--token-secret"})
            self.assertTrue(any("secret" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
