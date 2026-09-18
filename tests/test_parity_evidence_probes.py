"""P2: every parity evidence probe must have a positive and a negative control.

The registry was caught twice reporting something other than what it claimed —
a fixed `alias` verdict, and any TMDL measure counted as evidence of a
parameter. These tests pin the two properties that would have caught both: a
probe exists for exactly the features that declare one, and each probe finds
its artefact only when the artefact is really there.
"""
import json
import os
import tempfile
import unittest

from powerbi_import.parity_registry import (
    _DETECTORS, _EVIDENCE_PROBED, _FEATURE_BY_KEY, collect_target_evidence,
    scan_project)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _full_project(root, name="Demo"):
    """A generated project carrying one artefact for every declared probe."""
    report = os.path.join(root, f"{name}.Report", "definition")
    model = os.path.join(root, f"{name}.SemanticModel", "definition")

    _write(os.path.join(report, "report.json"),
           json.dumps({"filterConfig": {"filters": [{"name": "f1"}]}}))
    _write(os.path.join(report, "pages", "p1", "page.json"),
           json.dumps({"name": "p1"}))
    _write(os.path.join(report, "pages", "p1", "visuals", "v1", "visual.json"),
           json.dumps({"visual": {"visualType": "actionButton"}}))
    _write(os.path.join(report, "bookmarks", "b1", "bookmark.json"),
           json.dumps({"name": "b1"}))

    _write(os.path.join(model, "tables", "Top N.tmdl"),
           "table 'Top N'\n\tpartition 'Top N' = calculated\n"
           "\t\tsource = GENERATESERIES(1, 20, 1)\n")
    _write(os.path.join(model, "tables", "Calendar.tmdl"),
           "table Calendar\n\tcolumn MonthName\n\t\tsortByColumn: Month\n"
           "\thierarchy 'Date Hierarchy'\n")
    _write(os.path.join(model, "tables", "Orders.tmdl"),
           "table Orders\n\tpartition Orders = m\n"
           '\t\tsource = Value.NativeQuery(Source, "SELECT 1")\n')
    _write(os.path.join(model, "roles.tmdl"), "role Region\n")

    _write(os.path.join(root, "refresh_config.json"), "{}")
    _write(os.path.join(root, "pbi_subscriptions.json"), "[]")
    return root


class TestProbeDeclaration(unittest.TestCase):

    def test_every_declared_probe_names_a_real_feature(self):
        for key in _EVIDENCE_PROBED:
            with self.subTest(key=key):
                self.assertIn(key, _FEATURE_BY_KEY)
                self.assertIn(key, _DETECTORS)

    def test_a_full_project_triggers_every_declared_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = collect_target_evidence(_full_project(tmp), "Demo")
        self.assertEqual(set(), _EVIDENCE_PROBED - set(evidence))

    def test_no_probe_reports_a_feature_it_never_declared(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = collect_target_evidence(_full_project(tmp), "Demo")
        self.assertEqual(set(), set(evidence) - _EVIDENCE_PROBED)


class TestProbesCanFail(unittest.TestCase):
    """The control that matters: an empty project must evidence nothing."""

    def test_an_empty_project_evidences_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual({}, collect_target_evidence(tmp, "Demo"))

    def test_a_project_with_only_a_plain_table_evidences_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "Demo.SemanticModel", "definition",
                                "tables", "Customers.tmdl"),
                   "table Customers\n\tcolumn Name\n\t\tdataType: string\n")
            self.assertEqual({}, collect_target_evidence(tmp, "Demo"))

    def test_an_empty_filter_config_is_not_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "Demo.Report", "definition", "report.json"),
                   json.dumps({"filterConfig": {"filters": []}}))
            self.assertNotIn("filters", collect_target_evidence(tmp, "Demo"))

    def test_a_non_action_visual_is_not_evidence_of_an_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "Demo.Report", "definition", "pages", "p1",
                                "visuals", "v1", "visual.json"),
                   json.dumps({"visual": {"visualType": "barChart"}}))
            evidence = collect_target_evidence(tmp, "Demo")
        self.assertNotIn("action_url", evidence)
        self.assertNotIn("action_nav", evidence)


class TestScanReportsHowItKnows(unittest.TestCase):

    def test_a_probed_feature_without_its_artefact_reports_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan = scan_project({"filters": [1]}, tmp, "Demo")
        usage = next(u for u in scan.usages if u.key == "filters")
        self.assertEqual("not_found", usage.evidence_status)
        self.assertEqual(0.0, scan.evidence_coverage["coverage_percent"])

    def test_a_probed_feature_with_its_artefact_reports_evidenced(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan = scan_project({"filters": [1]}, _full_project(tmp), "Demo")
        usage = next(u for u in scan.usages if u.key == "filters")
        self.assertEqual("evidenced", usage.evidence_status)
        self.assertEqual(100.0, scan.evidence_coverage["coverage_percent"])

    def test_an_unprobed_feature_never_counts_as_a_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan = scan_project(
                {"filters": [1], "calculations": [{"formula": "[Sales]"}]},
                _full_project(tmp), "Demo")
        coverage = scan.evidence_coverage
        self.assertEqual(1, coverage["unchecked_features"])
        self.assertEqual(100.0, coverage["coverage_percent"])
        self.assertEqual(
            "not_checked",
            next(u for u in scan.usages if u.key == "calc_basic").evidence_status)

    def test_every_usage_says_how_its_evidence_was_established(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan = scan_project({"filters": [1], "sets": [1], "bins": [1]},
                                tmp, "Demo")
        self.assertTrue(scan.usages)
        for usage in scan.usages:
            with self.subTest(key=usage.key):
                self.assertIn(usage.evidence_status,
                              ("evidenced", "not_found", "not_checked"))


if __name__ == "__main__":
    unittest.main()
