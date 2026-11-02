"""The equivalence suite could not fail.

`run_full_equivalence_suite` reported `status: pass` and `fidelity_percent:
100.0` for a completely empty project and for one whose tables and fields
bore no relation to the source. It never read the generated artifact at all:
the row-count test was a hardcoded `True`, the calculation test only checked
that the *Tableau* formula was non-empty, and field coverage compared the
Tableau fields against themselves.

A flag that does nothing wastes a run. A validator that always passes is
worse: it invites someone to ship on the strength of it.

The fix has two halves, and both need guarding. Tests now read the generated
model, and a test that cannot run is reported as `not_run` and excluded from
the fidelity ratio rather than counted as a pass.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from powerbi_import.equivalence_tester_v2 import (  # noqa: E402
    _PSEUDO_FIELDS,
    EquivalenceTester,
    _read_generated_model,
    run_full_equivalence_suite,
)

TABLEAU = {
    "datasources": [{"name": "ds", "row_count": 1000}],
    "calculations": [{"name": "[Revenue]", "caption": "Revenue",
                      "formula": "SUM([amount])"}],
    "worksheets": [{"name": "Sheet1", "fields": [{"name": "amount"},
                                                 {"name": "Revenue"}]}],
}


def _model_project(root, measures=("Revenue",), columns=("amount",)):
    """A minimal generated project the reader can parse."""
    tables = os.path.join(root, "P.SemanticModel", "definition", "tables")
    os.makedirs(tables, exist_ok=True)
    lines = ["table Sales", ""]
    for column in columns:
        lines += [f"\tcolumn {column}", "\t\tdataType: double", ""]
    for measure in measures:
        lines += [f"\tmeasure {measure} = SUM(Sales[amount])", ""]
    with open(os.path.join(tables, "Sales.tmdl"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return root


class TestEquivalenceReportErrors(unittest.TestCase):
    def test_generation_failure_is_visible_without_claiming_a_report(self):
        from powerbi_import.pbip_generator import PowerBIProjectGenerator

        with tempfile.TemporaryDirectory() as root:
            generator = PowerBIProjectGenerator(root)
            with mock.patch('powerbi_import.equivalence_tester_v2.run_full_equivalence_suite',
                            side_effect=ValueError('bad source')), \
                    self.assertLogs('powerbi_import.pbip_generator', level='WARNING') as logs:
                generator._run_equivalence_checks(root, 'Synthetic', {})
            self.assertIn('bad source', '\n'.join(logs.output))
            self.assertIsNone(generator._equivalence_report)
            self.assertFalse(os.path.exists(os.path.join(root, 'equivalence_report_v2.json')))

    def test_unexpected_programming_errors_are_not_silently_swallowed(self):
        from powerbi_import.pbip_generator import PowerBIProjectGenerator

        with tempfile.TemporaryDirectory() as root:
            generator = PowerBIProjectGenerator(root)
            with mock.patch('powerbi_import.equivalence_tester_v2.run_full_equivalence_suite',
                            side_effect=RuntimeError('unexpected bug')):
                with self.assertRaisesRegex(RuntimeError, 'unexpected bug'):
                    generator._run_equivalence_checks(root, 'Synthetic', {})


class TestItCannotClaimAnUnearnedPass(unittest.TestCase):
    """The defect this file exists for."""

    def test_no_artifact_is_not_run_rather_than_pass(self):
        report = run_full_equivalence_suite(TABLEAU, {})
        self.assertEqual("not_run", report["status"])
        self.assertEqual(0, report["compared"])
        self.assertNotEqual(100.0, report["fidelity_percent"])

    def test_a_missing_project_directory_is_not_run(self):
        report = run_full_equivalence_suite(
            TABLEAU, {"project_dir": os.path.join("no", "such", "dir")})
        self.assertEqual("not_run", report["status"])

    def test_an_empty_project_does_not_score_full_fidelity(self):
        with tempfile.TemporaryDirectory() as td:
            report = run_full_equivalence_suite(TABLEAU, {"project_dir": td})
        self.assertNotEqual("pass", report["status"])

    def test_tests_that_cannot_run_are_excluded_from_fidelity(self):
        """Counting them as passes is what produced the false 100%."""
        tester = EquivalenceTester()
        report = tester.generate_report([
            {"test": "a", "passed": False, "status": "not_run", "severity": "info"},
            {"test": "b", "passed": True, "severity": "pass"},
        ])
        self.assertEqual(1, report["compared"])
        self.assertEqual(1, report["not_run"])
        self.assertEqual(100.0, report["fidelity_percent"])

    def test_only_not_run_yields_no_verdict(self):
        tester = EquivalenceTester()
        report = tester.generate_report([
            {"test": "a", "passed": False, "status": "not_run", "severity": "info"},
        ])
        self.assertEqual("not_run", report["status"])
        self.assertEqual(0.0, report["fidelity_percent"])

    def test_empty_source_visual_fields_have_no_coverage_verdict(self):
        passed, coverage = EquivalenceTester().test_visual_field_coverage(
            set(), {"Amount"}, "Empty worksheet")
        self.assertIsNone(passed)
        self.assertIsNone(coverage)


class TestItDetectsRealDefects(unittest.TestCase):

    def test_a_measure_missing_from_the_model_fails(self):
        with tempfile.TemporaryDirectory() as td:
            _model_project(td, measures=())
            report = run_full_equivalence_suite(TABLEAU, {"project_dir": td})
        failures = [d for d in report["details"]
                    if d["test"].startswith("calc:") and not d["passed"]]
        self.assertTrue(failures, "a dropped measure went unreported")

    def test_a_measure_present_in_the_model_passes(self):
        with tempfile.TemporaryDirectory() as td:
            _model_project(td)
            report = run_full_equivalence_suite(TABLEAU, {"project_dir": td})
        calc = [d for d in report["details"] if d["test"] == "calc:Revenue"]
        self.assertTrue(calc and calc[0]["passed"], report["details"])

    def test_row_count_is_reported_as_needing_a_deployed_model(self):
        with tempfile.TemporaryDirectory() as td:
            _model_project(td)
            report = run_full_equivalence_suite(TABLEAU, {"project_dir": td})
        row = [d for d in report["details"] if d["test"].startswith("row_count:")]
        self.assertEqual("not_run", row[0]["status"])

    def test_model_name_presence_is_diagnostic_not_visual_coverage(self):
        tableau = {
            "datasources": [],
            "calculations": [],
            "worksheets": [{"name": "S", "fields": [
                {"name": "amount"}, {"name": "never_generated"}]}],
        }
        with tempfile.TemporaryDirectory() as td:
            _model_project(td)
            report = run_full_equivalence_suite(tableau, {"project_dir": td})
        coverage = [d for d in report["details"]
                    if d["test"].startswith("model_name_presence:")][0]
        self.assertEqual(["amount"], coverage["matched_model_names"])
        self.assertEqual(["never_generated"], coverage["unmatched_model_names"])
        self.assertFalse(coverage["passed"])
        self.assertEqual("unqualified_model_names", coverage["scope"])
        self.assertEqual("not_run", coverage["status"])
        self.assertNotIn("coverage_percent", coverage)
        self.assertIsNone(report["fidelity_percent"])

    def test_model_names_do_not_prove_visual_projection_coverage(self):
        tableau = {
            "datasources": [], "calculations": [],
            "worksheets": [{"name": "S", "fields": [
                {"name": "amount"}, {"name": "Revenue"}]}],
        }
        with tempfile.TemporaryDirectory() as td:
            _model_project(td)
            visuals = os.path.join(
                td, "P.Report", "definition", "pages", "unrelated",
                "visuals", "v")
            os.makedirs(visuals)
            with open(os.path.join(visuals, "visual.json"), "w",
                      encoding="utf-8") as fh:
                fh.write('{"visual": {"query": {"queryState": {}}}}')
            report = run_full_equivalence_suite(tableau, {"project_dir": td})
        presence, = [d for d in report["details"]
                     if d["test"] == "model_name_presence:S"]
        visual, = [d for d in report["details"]
                   if d["test"] == "visual_coverage:S"]
        self.assertEqual(["Revenue", "amount"], presence["matched_model_names"])
        self.assertNotIn("coverage_percent", presence)
        self.assertEqual("not_run", visual["status"])
        self.assertFalse(visual["passed"])
        self.assertNotIn("coverage_percent", visual)
        self.assertIn("projection", visual["message"].lower())
        self.assertEqual("not_run_visual_projection_equivalence",
                         report["fidelity_percent_scope"])
        self.assertEqual(1, report["generated_visual_artifact_count"])
        self.assertIsNone(report["fidelity_percent"])

    def test_unique_calculation_identifier_resolution_is_not_coverage(self):
        tableau = {
            "datasources": [],
            "calculations": [{
                "name": "[Calculation_1]", "caption": "Revenue",
                "datasource_name": "source-a",
            }],
            "worksheets": [{"name": "S", "fields": [
                {"name": "Calculation_1", "datasource": "source-a"}]}],
        }
        with tempfile.TemporaryDirectory() as td:
            _model_project(td)
            report = run_full_equivalence_suite(tableau, {"project_dir": td})
        presence, = [d for d in report["details"]
                     if d["test"] == "model_name_presence:S"]
        self.assertEqual(["Revenue"], presence["matched_model_names"])
        self.assertFalse(presence["passed"])
        self.assertEqual("not_run", presence["status"])
        self.assertNotIn("coverage_percent", presence)

    def test_ambiguous_calculation_captions_are_not_guessed(self):
        tableau = {
            "datasources": [],
            "calculations": [
                {"name": "[Calculation_1]", "caption": "Revenue",
                 "datasource_name": "source-a"},
                {"name": "[Calculation_1]", "caption": "Cost",
                 "datasource_name": "source-b"},
            ],
            "worksheets": [{"name": "S", "fields": [
                {"name": "Calculation_1"}]}],
        }
        with tempfile.TemporaryDirectory() as td:
            _model_project(td)
            report = run_full_equivalence_suite(tableau, {"project_dir": td})
        presence, = [d for d in report["details"]
                     if d["test"] == "model_name_presence:S"]
        self.assertEqual("not_run", presence["status"])
        self.assertTrue(presence["ambiguous_fields"])
        self.assertNotIn("coverage_percent", presence)

    def test_datasource_resolves_colliding_calculation_identifiers(self):
        tableau = {
            "datasources": [],
            "calculations": [
                {"name": "[Calculation_1]", "caption": "Revenue",
                 "datasource_name": "source-a"},
                {"name": "[Calculation_1]", "caption": "Cost",
                 "datasource_name": "source-b"},
            ],
            "worksheets": [{"name": "S", "fields": [
                {"name": "Calculation_1", "datasource": "source-a"}]}],
        }
        with tempfile.TemporaryDirectory() as td:
            _model_project(td)
            report = run_full_equivalence_suite(tableau, {"project_dir": td})
        presence, = [d for d in report["details"]
                     if d["test"] == "model_name_presence:S"]
        self.assertEqual(["Revenue"], presence["matched_model_names"])
        self.assertEqual("unqualified_model_names", presence["scope"])
        self.assertIn("not visual field coverage", presence["message"])


class TestPseudoFields(unittest.TestCase):
    """Tableau shelf pseudo-fields have no model equivalent by design."""

    def test_they_do_not_count_as_missing_coverage(self):
        tableau = {
            "datasources": [],
            "calculations": [],
            "worksheets": [{"name": "S", "fields": [
                {"name": "amount"}, {"name": "Measure Names"},
                {"name": "Measure Values"}]}],
        }
        with tempfile.TemporaryDirectory() as td:
            _model_project(td)
            report = run_full_equivalence_suite(tableau, {"project_dir": td})
        coverage = [d for d in report["details"]
                    if d["test"].startswith("model_name_presence:")]
        self.assertEqual(["amount"], coverage[0]["matched_model_names"],
                         "pseudo-fields were counted as source model-name diagnostics")
        self.assertNotIn("coverage_percent", coverage[0])

    def test_the_exclusion_list_is_not_empty(self):
        self.assertIn("Measure Names", _PSEUDO_FIELDS)


class TestModelReader(unittest.TestCase):

    def test_it_reads_measures_and_columns(self):
        with tempfile.TemporaryDirectory() as td:
            _model_project(td)
            model = _read_generated_model({"project_dir": td})
        self.assertIn("Revenue", model["measures"])
        self.assertIn("amount", model["columns"])

    def test_an_empty_project_reads_as_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(_read_generated_model({"project_dir": td}))


class TestFlagWiring(unittest.TestCase):

    def setUp(self):
        import migrate
        self.migrate = migrate
        self.parser = migrate._build_argument_parser()

    def test_it_defaults_off(self):
        self.assertFalse(self.parser.parse_args(['wb.twbx']).validate_data)

    def test_it_parses(self):
        self.assertTrue(
            self.parser.parse_args(['wb.twbx', '--validate-data']).validate_data)

    def test_the_flag_reaches_the_runner(self):
        import inspect
        source = inspect.getsource(self.migrate)
        self.assertRegex(
            source,
            r"getattr\(args, 'validate_data', False\)[^\n]*\n[^\n]*\n\s+_run_data_validation\(args",
            "--validate-data is never acted on")

    def test_a_missing_project_is_skipped_not_fatal(self):
        class _Args:
            output_dir = os.path.join("no", "such", "dir")
            validate_data = True
            dry_run = False
        self.migrate._run_data_validation(_Args(), "Nope")


if __name__ == "__main__":
    unittest.main()
