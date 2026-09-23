"""The controls that prove the guards can fail.

A test that cannot fail reports success and hides the defect it was written
for. Three appeared in a single session: a check satisfied by the ``def`` line
it was meant to find a call for, an assertion matched by a docstring rather
than a call site, and a harness that silently never wrote its mutations and
so reported every control as passing.

``tests/control_specs.py`` records each deliberate defect and the test that
must notice it. These tests keep the specs honest without running them: a spec
whose pattern no longer appears is describing code that has moved on, and a
stale spec is worse than none, because the scorecard still shows a row.

Running the controls themselves is ``python scripts/verify_controls.py``,
which mutates real files and is therefore not a unit test.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.verify_controls import _REPO_ROOT, run_control  # noqa: E402
from tests.control_specs import CONTROLS  # noqa: E402


class TestSpecsAreWellFormed(unittest.TestCase):

    def test_there_are_controls(self):
        self.assertGreater(len(CONTROLS), 0)

    def test_every_spec_has_the_required_keys(self):
        for spec in CONTROLS:
            with self.subTest(name=spec.get("name")):
                for key in ("name", "file", "old", "new", "test"):
                    self.assertIn(key, spec)

    def test_names_are_unique(self):
        names = [s["name"] for s in CONTROLS]
        self.assertEqual(len(names), len(set(names)))

    def test_a_mutation_must_change_something(self):
        for spec in CONTROLS:
            with self.subTest(name=spec["name"]):
                self.assertNotEqual(spec["old"], spec["new"])


class TestSpecsStillDescribeTheCode(unittest.TestCase):
    """A spec that no longer matches is silently not testing anything."""

    def test_every_target_file_exists(self):
        missing = sorted({s["file"] for s in CONTROLS
                          if not os.path.isfile(os.path.join(_REPO_ROOT, s["file"]))})
        self.assertEqual([], missing, f"target files gone: {missing}")

    def test_every_test_file_exists(self):
        missing = sorted({s["test"] for s in CONTROLS
                          if not os.path.isfile(os.path.join(_REPO_ROOT, s["test"]))})
        self.assertEqual([], missing, f"test files gone: {missing}")

    def test_every_pattern_is_present(self):
        for spec in CONTROLS:
            with self.subTest(name=spec["name"]):
                path = os.path.join(_REPO_ROOT, spec["file"])
                with open(path, encoding="utf-8") as fh:
                    source = fh.read()
                self.assertIn(spec["old"], source,
                              f"{spec['file']} no longer contains the text this "
                              f"control breaks — the control tests nothing")

    def test_every_pattern_is_unambiguous(self):
        """replace(..., 1) hits the first match, so one match is required.

        A pattern occurring twice mutates only the first site, which can leave
        the behaviour intact and the control silent. Such a spec must opt into
        ``all`` so every occurrence is broken.
        """
        for spec in CONTROLS:
            with self.subTest(name=spec["name"]):
                path = os.path.join(_REPO_ROOT, spec["file"])
                with open(path, encoding="utf-8") as fh:
                    source = fh.read()
                count = source.count(spec["old"])
                if spec.get("all"):
                    self.assertGreaterEqual(count, 1)
                else:
                    self.assertEqual(
                        1, count,
                        f"{spec['file']}: pattern appears {count} times; narrow "
                        f"it, or set \"all\": True to break every occurrence")


class TestRunnerJudgement(unittest.TestCase):

    def test_a_missing_pattern_is_invalid_not_a_pass(self):
        """An unapplied mutation must never be reported as a fired control."""
        result = run_control({
            "name": "synthetic", "file": "migrate.py",
            "old": "this text does not appear anywhere in the file",
            "new": "x", "test": "tests/test_doc_claims.py",
        })
        self.assertEqual("invalid", result.status)

    def test_the_target_file_is_left_untouched(self):
        path = os.path.join(_REPO_ROOT, "migrate.py")
        with open(path, encoding="utf-8") as fh:
            before = fh.read()
        run_control({
            "name": "synthetic", "file": "migrate.py",
            "old": "nope-not-here", "new": "x",
            "test": "tests/test_doc_claims.py",
        })
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(before, fh.read())


if __name__ == "__main__":
    unittest.main()
