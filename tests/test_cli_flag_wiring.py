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
from unittest import mock

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


class TestServerAssess(unittest.TestCase):
    """--server-assess was declared, documented with a value, and inert.

    As store_true it could not accept the project name the docs show, so the
    documented command fed 'Marketing' to the workbook positional instead.
    """

    def setUp(self):
        import migrate
        self.parser = migrate._build_argument_parser()

    def test_absent_by_default(self):
        args = self.parser.parse_args(['wb.twbx'])
        self.assertIsNone(args.server_assess)

    def test_accepts_the_documented_project_name(self):
        args = self.parser.parse_args(
            ['--server', 'https://t.example.com', '--server-assess', 'Marketing'])
        self.assertEqual('Marketing', args.server_assess)

    def test_bare_flag_means_the_whole_site(self):
        """Empty is distinct from None: requested, but unscoped."""
        args = self.parser.parse_args(
            ['--server', 'https://t.example.com', '--server-assess'])
        self.assertEqual('', args.server_assess)

    def test_the_project_name_is_not_eaten_as_a_workbook(self):
        args = self.parser.parse_args(
            ['--server', 'https://t.example.com', '--server-assess', 'Marketing'])
        self.assertNotEqual('Marketing', getattr(args, 'tableau_file', None))

    def test_the_default_does_not_trigger_server_work(self):
        """No --server-assess must return before any network call."""
        import migrate
        args = self.parser.parse_args(['wb.twbx'])
        self.assertIsNone(migrate._handle_enterprise_server_ops(args))

    def test_it_routes_through_enterprise_operations(self):
        """A bare --server-assess must reach the assessment handler."""
        import migrate
        args = self.parser.parse_args(
            ['--server', 'https://t.example.com', '--server-assess'])
        called = {}

        def _fake(a, out):
            called['project'] = a.server_assess
            return migrate.ExitCode.SUCCESS

        with mock.patch('tableau_export.server_client.TableauServerClient'), \
                mock.patch.object(migrate, '_run_server_assessment', _fake):
            result = migrate._handle_enterprise_server_ops(args)

        self.assertEqual(migrate.ExitCode.SUCCESS, result)
        self.assertEqual('', called.get('project'),
                         "--server-assess never reached its handler")


class TestLiveConnection(unittest.TestCase):
    """--live-connection was inert while the generator already implemented it.

    ThinReportGenerator has accepted a live_connection argument all along and
    writes byConnection when given one; no caller ever passed it, so the
    documented shared-model command silently produced byPath.
    """

    def setUp(self):
        import migrate
        self.parser = migrate._build_argument_parser()

    def test_absent_by_default(self):
        self.assertIsNone(self.parser.parse_args(['wb.twbx']).live_connection)

    def test_accepts_workspace_and_model(self):
        args = self.parser.parse_args(
            ['wb.twbx', '--live-connection', 'ws-1234/SharedSales'])
        self.assertEqual('ws-1234/SharedSales', args.live_connection)

    def test_the_whole_chain_accepts_the_argument(self):
        """The signature gap is the failure mode: a caller passes what the
        callee never declared, and only a real run raises TypeError."""
        import inspect

        import migrate
        from powerbi_import.import_to_powerbi import PowerBIImporter
        from powerbi_import.thin_report_generator import ThinReportGenerator

        for func in (migrate.run_shared_model_migration,
                     PowerBIImporter.import_shared_model,
                     ThinReportGenerator.__init__):
            with self.subTest(func=func.__qualname__):
                self.assertIn("live_connection",
                              inspect.signature(func).parameters,
                              f"{func.__qualname__} cannot receive live_connection")

    def test_the_importer_hands_it_to_the_generator(self):
        """Constructing the generator directly proves nothing about the caller.

        Dropping ``live_connection=live_connection`` from the construction site
        leaves every other test in this class green while the flag goes inert
        again, so assert the handoff itself.
        """
        import inspect

        from powerbi_import import import_to_powerbi

        source = inspect.getsource(import_to_powerbi.PowerBIImporter.import_shared_model)
        self.assertRegex(
            source,
            r"ThinReportGenerator\([^)]*live_connection=live_connection",
            "import_shared_model builds the thin report generator without "
            "forwarding live_connection",
        )

    def test_byconnection_is_written_when_requested(self):
        import json
        import tempfile

        from powerbi_import.thin_report_generator import ThinReportGenerator

        with tempfile.TemporaryDirectory() as td:
            gen = ThinReportGenerator('SharedSales', td,
                                      live_connection='ws-1234/SharedSales')
            gen.generate_thin_report('Sales', {'worksheets': []})
            pbir = os.path.join(td, 'Sales.Report', 'definition.pbir')
            with open(pbir, encoding='utf-8') as fh:
                ref = json.load(fh)['datasetReference']

        self.assertIn('byConnection', ref)
        self.assertNotIn('byPath', ref)
        conn = ref['byConnection']['connectionString']
        self.assertIn('myorg/ws-1234', conn)
        self.assertIn('Initial Catalog=SharedSales', conn)

    def test_bypath_remains_the_default(self):
        import json
        import tempfile

        from powerbi_import.thin_report_generator import ThinReportGenerator

        with tempfile.TemporaryDirectory() as td:
            gen = ThinReportGenerator('SharedSales', td)
            gen.generate_thin_report('Sales', {'worksheets': []})
            pbir = os.path.join(td, 'Sales.Report', 'definition.pbir')
            with open(pbir, encoding='utf-8') as fh:
                ref = json.load(fh)['datasetReference']

        self.assertIn('byPath', ref)
        self.assertNotIn('byConnection', ref)


if __name__ == "__main__":
    unittest.main()
