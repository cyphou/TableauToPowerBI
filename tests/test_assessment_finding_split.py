"""Assessment warnings must reach the queue as separate, actionable findings.

A single "contains warnings" entry named one owner and one action for
unrecognised connectors, wide schemas and licensing limits alike, so nobody
could act on it. These tests pin the split, and pin that it can still fail.
"""
import unittest

from powerbi_import.assessment import (
    AssessmentReport, CategoryResult, CheckItem, run_assessment)
from powerbi_import.migration_quality import (
    _ASSESSMENT_CATEGORY_FINDINGS, _FINDING_ACTIONS,
    _ADVISORY_ASSESSMENT_CATEGORIES, _assessment_blocking_failures,
    _assessment_check_findings, _Findings)


def _check(category, name, severity, detail="d", recommendation="r"):
    return CheckItem(category=category, name=name, severity=severity,
                     detail=detail, recommendation=recommendation)


def _report(*categories):
    return AssessmentReport(workbook_name="wb", timestamp="t",
                            categories=list(categories))


def _collect(report):
    findings = _Findings()
    _assessment_check_findings(report, findings)
    return findings.records


class TestCategoryMapping(unittest.TestCase):
    """The map keys into another table; a typo there would be invisible."""

    def test_every_mapped_id_has_an_action_and_owner(self):
        for category, finding_id in _ASSESSMENT_CATEGORY_FINDINGS.items():
            with self.subTest(category=category):
                self.assertIn(finding_id, _FINDING_ACTIONS)

    def test_every_category_the_assessment_emits_is_mapped(self):
        # Built from the producer, so a new category cannot slip in unmapped.
        emitted = {cat.name for cat in run_assessment({}).categories}
        self.assertTrue(emitted)
        self.assertEqual(set(), emitted - set(_ASSESSMENT_CATEGORY_FINDINGS))

    def test_advisory_category_is_a_real_category(self):
        emitted = {cat.name for cat in run_assessment({}).categories}
        self.assertEqual(set(), _ADVISORY_ASSESSMENT_CATEGORIES - emitted)

    def test_owners_are_not_all_the_same(self):
        owners = {_FINDING_ACTIONS[i][1]
                  for i in _ASSESSMENT_CATEGORY_FINDINGS.values()}
        self.assertGreater(len(owners), 1)


class TestSplit(unittest.TestCase):

    def test_each_warning_becomes_its_own_finding(self):
        report = _report(
            CategoryResult("Datasource Compatibility",
                           [_check("Datasource Compatibility", "Connector: X", "warn")]),
            CategoryResult("Calculation Readiness",
                           [_check("Calculation Readiness", "Partial fns", "warn")]),
        )
        records = _collect(report)
        self.assertEqual(2, len(records))
        self.assertEqual({"assessment_connector", "assessment_calculation"},
                         {r["id"] for r in records})

    def test_a_connector_warning_and_a_schema_warning_differ_in_owner(self):
        records = _collect(_report(
            CategoryResult("Datasource Compatibility",
                           [_check("Datasource Compatibility", "Connector: X", "warn")]),
            CategoryResult("Data Model Complexity",
                           [_check("Data Model Complexity", "Column count", "warn")]),
        ))
        by_id = {r["id"]: r for r in records}
        self.assertNotEqual(by_id["assessment_connector"]["owner"],
                            by_id["assessment_model"]["owner"])
        self.assertNotEqual(by_id["assessment_connector"]["action_kind"],
                            by_id["assessment_model"]["action_kind"])

    def test_the_check_recommendation_becomes_the_fix_line(self):
        records = _collect(_report(CategoryResult(
            "Licensing",
            [_check("Licensing", "Premium features", "warn",
                    detail="1284 columns", recommendation="Consider PPU.")])))
        self.assertEqual("Consider PPU.", records[0]["fix"])
        self.assertIn("1284 columns", records[0]["message"])
        self.assertIn("Premium features", records[0]["message"])

    def test_a_check_without_detail_still_names_itself(self):
        records = _collect(_report(CategoryResult(
            "Licensing", [_check("Licensing", "Premium features", "warn", detail="")])))
        self.assertIn("Premium features", records[0]["message"])

    def test_an_unmapped_category_falls_back_rather_than_vanishing(self):
        records = _collect(_report(CategoryResult(
            "Brand New Category", [_check("Brand New Category", "c", "warn")])))
        self.assertEqual(["assessment_warnings"], [r["id"] for r in records])

    def test_no_finding_is_a_blocker(self):
        records = _collect(_report(CategoryResult(
            "Performance", [_check("Performance", "Query complexity", "warn")])))
        self.assertFalse(any(r["blocker"] for r in records))


class TestItCanStillFail(unittest.TestCase):
    """A detector that only ever reports the same thing is a rubber stamp."""

    def test_a_clean_assessment_produces_nothing(self):
        records = _collect(_report(CategoryResult(
            "Performance", [_check("Performance", "ok", "pass"),
                            _check("Performance", "fyi", "info")])))
        self.assertEqual([], records)

    def test_a_workbook_with_no_categories_produces_nothing(self):
        self.assertEqual([], _collect(_report()))

    def test_more_warnings_means_more_findings(self):
        few = _collect(_report(CategoryResult(
            "Datasource Compatibility",
            [_check("Datasource Compatibility", "Connector: A", "warn")])))
        many = _collect(_report(CategoryResult(
            "Datasource Compatibility",
            [_check("Datasource Compatibility", "Connector: A", "warn"),
             _check("Datasource Compatibility", "Connector: B", "warn"),
             _check("Datasource Compatibility", "Connector: C", "warn")])))
        self.assertEqual(1, len(few))
        self.assertEqual(3, len(many))


class TestFailuresAreNotDuplicated(unittest.TestCase):

    def test_a_blocking_failure_is_not_repeated_as_a_warning(self):
        report = _report(CategoryResult(
            "Datasource Compatibility",
            [_check("Datasource Compatibility", "Connector: X", "fail")]))
        self.assertEqual([], _collect(report))
        self.assertEqual(["Datasource Compatibility / Connector: X"],
                         _assessment_blocking_failures(report))

    def test_an_advisory_failure_is_enumerated_because_nothing_else_names_it(self):
        report = _report(CategoryResult(
            "Performance", [_check("Performance", "DAX expression count", "fail")]))
        records = _collect(report)
        self.assertEqual(["assessment_performance"], [r["id"] for r in records])
        self.assertEqual([], _assessment_blocking_failures(report))

    def test_blocking_failures_are_named_not_merely_counted(self):
        report = _report(
            CategoryResult("Datasource Compatibility",
                           [_check("Datasource Compatibility", "Connector: X", "fail")]),
            CategoryResult("Calculation Readiness",
                           [_check("Calculation Readiness", "Unsupported", "fail")]),
        )
        self.assertEqual(
            ["Datasource Compatibility / Connector: X",
             "Calculation Readiness / Unsupported"],
            _assessment_blocking_failures(report))

    def test_nothing_failing_means_no_blocking_failures(self):
        report = _report(CategoryResult(
            "Datasource Compatibility",
            [_check("Datasource Compatibility", "Connector: X", "warn")]))
        self.assertEqual([], _assessment_blocking_failures(report))


if __name__ == "__main__":
    unittest.main()
