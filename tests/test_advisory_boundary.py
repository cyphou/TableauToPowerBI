"""Advisory code may propose, never apply.

Migration output has to be reproducible or the golden fixtures, the parity
registry and the regression suite stop meaning anything. Non-deterministic
code earns its freedom by staying out of the artifact path: it reads evidence
and writes its own report.

That split holds today by habit, not by rule. `llm_client` writes a report
JSON, `remediation` writes its own JSON and HTML, and no advisory module
imports a generator. Nothing enforced any of it, so writing refined DAX
straight into the model would have looked like an improvement.

These tests drive the detector with synthetic modules so its judgement is
checkable, then assert the real boundary is intact.
"""

import os
import sys
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.check_advisory_boundary import (  # noqa: E402
    ADVISORY_MODULES,
    ARTIFACT_GENERATORS,
    PACKAGE_DIR,
    breaches,
    main,
    scan,
)


class TestDetectorJudgement(unittest.TestCase):
    """Synthetic sources, so the rule is verifiable without the real tree."""

    def _rules(self, source):
        return {rule for rule, _detail, _line in breaches(textwrap.dedent(source))}

    def test_importing_a_generator_is_a_breach(self):
        self.assertIn("imports-generator", self._rules("""
            from powerbi_import.tmdl_generator import generate_tmdl
        """))

    def test_plain_import_of_a_generator_is_a_breach(self):
        self.assertIn("imports-generator", self._rules("""
            import powerbi_import.pbip_generator
        """))

    def test_aliased_generator_import_is_a_breach(self):
        """`as` must not launder the import past the check."""
        self.assertIn("imports-generator", self._rules("""
            from powerbi_import import visual_generator as vg
        """))

    def test_naming_an_artifact_path_is_a_breach(self):
        self.assertIn("names-artifact", self._rules("""
            TARGET = "definition/model.tmdl"
        """))

    def test_reading_evidence_is_not_a_breach(self):
        """The whole point: advisory code consumes reports freely."""
        self.assertEqual(set(), self._rules("""
            import json
            from powerbi_import.migration_quality import load_report
            def suggest(path):
                with open("remediation_report.json", "w") as fh:
                    json.dump({}, fh)
        """))

    def test_a_bare_extension_is_not_a_breach(self):
        """Documenting ".tmdl" in prose must not trip the detector."""
        self.assertEqual(set(), self._rules("""
            SUFFIX = ".tmdl"
        """))

    def test_importing_a_non_generator_is_not_a_breach(self):
        self.assertEqual(set(), self._rules("""
            from powerbi_import.quality_grades import normalize
        """))


class TestRealBoundary(unittest.TestCase):
    """The measured state of the repository."""

    def test_the_advisory_surface_is_clean(self):
        offenders = {name: items for name, items in scan().items() if items}
        self.assertEqual({}, offenders,
                         "advisory code reached the artifact path: "
                         f"{offenders}")

    def test_every_named_advisory_module_exists(self):
        """A frozen set that outlives its modules stops describing anything."""
        missing = sorted(
            name for name in ADVISORY_MODULES
            if not os.path.isfile(os.path.join(PACKAGE_DIR, f"{name}.py"))
        )
        self.assertEqual([], missing,
                         f"no longer present: {missing} — drop them from ADVISORY_MODULES")

    def test_every_named_generator_exists(self):
        missing = sorted(
            name for name in ARTIFACT_GENERATORS
            if not os.path.isfile(os.path.join(PACKAGE_DIR, f"{name}.py"))
        )
        self.assertEqual([], missing,
                         f"no longer present: {missing} — drop them from ARTIFACT_GENERATORS")

    def test_the_two_sets_never_overlap(self):
        """A module cannot both judge the artifact and write it."""
        self.assertEqual(frozenset(), ADVISORY_MODULES & ARTIFACT_GENERATORS)

    def test_strict_mode_passes_on_a_clean_tree(self):
        self.assertEqual(0, main(["--strict"]))


if __name__ == "__main__":
    unittest.main()
