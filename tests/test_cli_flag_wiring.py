"""A declared CLI flag that nothing reads must fail the build.

`--optimize-dax` was accepted by the parser and never consumed: the user passed
it, the run proceeded, and no DAX was optimized. Fifteen flags were measured in
that state. These tests drive the detector with synthetic parsers so its
judgement is verifiable, and freeze the measured set so the defect cannot grow.
"""

import os
import sys
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.check_cli_flags import (  # noqa: E402
    CLI_SOURCE,
    KNOWN_INERT,
    declared_flags,
    find_inert,
)


class TestDetectorJudgement(unittest.TestCase):
    """Synthetic parsers, so the rule is checkable without running the CLI."""

    def _inert(self, source):
        return {dest for _flag, dest, _line in find_inert(textwrap.dedent(source))}

    def test_a_flag_nobody_reads_is_reported(self):
        self.assertIn("ghost", self._inert("""
            def build(parser):
                parser.add_argument('--ghost', action='store_true')
        """))

    def test_attribute_access_counts_as_consumption(self):
        self.assertNotIn("used", self._inert("""
            def build(parser):
                parser.add_argument('--used', action='store_true')
            def run(args):
                return args.used
        """))

    def test_getattr_access_counts_as_consumption(self):
        self.assertNotIn("lazy", self._inert("""
            def build(parser):
                parser.add_argument('--lazy', action='store_true')
            def run(args):
                return getattr(args, 'lazy', None)
        """))

    def test_config_lookup_counts_as_consumption(self):
        self.assertNotIn("from_config", self._inert("""
            def build(parser):
                parser.add_argument('--from-config', action='store_true')
            def run(config):
                return config['from_config']
        """))

    def test_explicit_dest_is_honoured(self):
        """--no-x style flags rename the destination they write to."""
        inert = self._inert("""
            def build(parser):
                parser.add_argument('--no-thing', dest='thing', action='store_false')
        """)
        self.assertIn("thing", inert)
        self.assertNotIn("no_thing", inert)

    def test_a_hyphenated_flag_maps_to_its_underscore_destination(self):
        self.assertIn("two_words", self._inert("""
            def build(parser):
                parser.add_argument('--two-words', action='store_true')
        """))


class TestRealCliSurface(unittest.TestCase):

    def setUp(self):
        with open(CLI_SOURCE, encoding="utf-8-sig") as handle:
            self.source = handle.read()

    def test_the_cli_still_declares_flags(self):
        """A parse failure would empty the set and make every check vacuous."""
        self.assertGreater(len(declared_flags(self.source)), 100)

    def test_no_flag_outside_the_measured_set_is_inert(self):
        new = sorted(dest for _flag, dest, _line in find_inert(self.source)
                     if dest not in KNOWN_INERT)
        self.assertEqual([], new,
                         f"new inert flag(s): {new} — wire or remove them")

    def test_the_frozen_set_does_not_outlive_the_flags_it_describes(self):
        """Removing a flag must also remove its entry, or the set rots."""
        declared = set(declared_flags(self.source))
        stale = sorted(KNOWN_INERT - declared)
        self.assertEqual([], stale,
                         f"no longer declared: {stale} — drop them from KNOWN_INERT")

    def test_the_baseline_only_shrinks(self):
        """A flag that has been wired must leave the baseline.

        Without this the set would quietly keep entries that are no longer a
        defect, and it would stop describing the real inert surface.
        """
        still_inert = {dest for _flag, dest, _line in find_inert(self.source)}
        fixed = sorted(KNOWN_INERT - still_inert)
        self.assertEqual([], fixed,
                         f"now consumed: {fixed} — drop them from KNOWN_INERT")


if __name__ == "__main__":
    unittest.main()
