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
            json.dumps({"visual": {"visualType": "actionButton",
                        "drillFilterOtherVisuals": True}}))
    _write(os.path.join(report, "pages", "p1", "visuals", "v2", "visual.json"),
           json.dumps({"visual": {"visualType": "lineChart", "objects": {
               "valueAxis": [{"properties": {"referenceLine": [
                   {"type": "Constant", "value": "10D"}
               ]}}]}}}))
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
    _write(os.path.join(model, "tables", "Regions.tmdl"),
           "table Regions\n"
           "\tcolumn 'Region (Display)' = SWITCH('Regions'[Region], \"N\", \"North\", 'Regions'[Region])\n"
           "\t\tdataType: string\n\t\tdisplayFolder: Aliases\n")
    _write(os.path.join(model, "tables", "Measures.tmdl"),
           "table Measures\n"
           "\tmeasure 'Revenue' = SUM('Sales'[Amount])\n"
           "\t\tannotation MigrationNote = Restored from Tableau field alias: sum:Sales\n")
    _write(os.path.join(model, "tables", "Classifications.tmdl"),
           "table Classifications\n"
           "\tcolumn Grouped = SWITCH([Category], \"A\", \"Alpha\", \"Other\")\n"
           "\t\tdisplayFolder: Groups\n"
           "\tcolumn Bucket = FLOOR([Amount], 10)\n"
           "\t\tdisplayFolder: Bins\n")
    _write(os.path.join(model, "tables", "Calculations.tmdl"),
           "table Calculations\n"
           "\tmeasure 'Net Revenue' = SUM('Sales'[Amount])\n"
           "\t\tannotation Copilot_Description = Migrated from Tableau: SUM([Amount])\n"
           "\tmeasure 'Revenue per Customer' = "
           "CALCULATE(SUM('Sales'[Amount]), ALLEXCEPT('Sales', 'Sales'[customer_id]))\n"
           "\t\tannotation Copilot_Description = Migrated from Tableau: "
           "{FIXED [customer_id] : SUM([Amount])}\n"
           "\tmeasure 'Revenue Rank' = RANKX(ALL('Sales'), SUM('Sales'[Amount]))\n"
           "\t\tannotation Copilot_Description = Migrated from Tableau: RANK(SUM([Amount]))\n")
    _write(os.path.join(model, "cultures", "en-US.tmdl"),
           "culture en-US\n\tlinguisticMetadata =\n"
           "\t\t```\n\t\t{\"Entities\": {\"Region\": {}}}\n\t\t```\n")
    _write(os.path.join(model, "roles.tmdl"), "role Region\n")

    _write(os.path.join(root, "refresh_config.json"), "{}")
    _write(os.path.join(root, "pbi_subscriptions.json"), "[]")
    _write(os.path.join(root, "Data", "Extract.csv"), "Id\n1\n")
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

    def test_a_web_url_column_is_evidence_of_a_dynamic_url_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "Demo.SemanticModel", "definition",
                                "tables", "Sales.tmdl"),
                   "table Sales\n\tcolumn Link\n"
                   "\t\tdataCategory: WebUrl\n")
            evidence = collect_target_evidence(tmp, "Demo")
        self.assertIn("action_url", evidence)

    def test_a_visual_reference_line_is_evidence_of_reference_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "Demo.Report", "definition", "pages",
                                "p1", "visuals", "v1", "visual.json"),
                   json.dumps({"visual": {"objects": {
                       "valueAxis": [{"properties": {
                           "referenceLine": [{"type": "Constant"}]
                       }}]
                   }}}))
            evidence = collect_target_evidence(tmp, "Demo")
        self.assertIn("reference_line", evidence)

    def test_a_visual_without_reference_line_is_not_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "Demo.Report", "definition", "pages",
                                "p1", "visuals", "v1", "visual.json"),
                   json.dumps({"visual": {"visualType": "lineChart"}}))
            evidence = collect_target_evidence(tmp, "Demo")
        self.assertNotIn("reference_line", evidence)

    def test_cross_filter_enabled_visual_is_evidence_of_filter_action_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "Demo.Report", "definition", "pages",
                                "p1", "visuals", "v1", "visual.json"),
                   json.dumps({"visual": {"visualType": "barChart",
                                           "drillFilterOtherVisuals": True}}))
            evidence = collect_target_evidence(tmp, "Demo")
        self.assertIn("action_filter", evidence)

    def test_visual_without_cross_filtering_is_not_filter_action_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "Demo.Report", "definition", "pages",
                                "p1", "visuals", "v1", "visual.json"),
                   json.dumps({"visual": {"visualType": "barChart"}}))
            evidence = collect_target_evidence(tmp, "Demo")
        self.assertNotIn("action_filter", evidence)

    def test_staged_csv_is_evidence_of_hyper_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "Data", "Extract.csv"), "Id\n1\n")
            evidence = collect_target_evidence(tmp, "Demo")
        self.assertIn("extract_hyper", evidence)

    def test_missing_staged_data_is_not_hyper_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = collect_target_evidence(tmp, "Demo")
        self.assertNotIn("extract_hyper", evidence)


class TestCalculationFamiliesAreDistinguished(unittest.TestCase):
    """A measure proves *some* calculation converted, never which kind.

    Each family is therefore keyed on its distinguishing DAX artifact, and a
    declaration only counts when it carries migration provenance.
    """

    def _families(self, table_body):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "Demo.SemanticModel", "definition",
                                "tables", "T.tmdl"), table_body)
            return collect_target_evidence(tmp, "Demo")

    def test_aggregation_evidences_a_basic_calculation(self):
        evidence = self._families(
            "table T\n\tmeasure 'Total' = SUM('T'[Amount])\n"
            "\t\tannotation Copilot_Description = Migrated from Tableau: SUM([Amount])\n")
        self.assertIn("calc_basic", evidence)
        self.assertNotIn("calc_lod", evidence)
        self.assertNotIn("calc_table", evidence)

    def test_grain_override_evidences_a_lod(self):
        evidence = self._families(
            "table T\n\tmeasure 'Per Customer' = "
            "CALCULATE(SUM('T'[Amount]), ALLEXCEPT('T', 'T'[customer_id]))\n"
            "\t\tannotation Copilot_Description = Migrated from Tableau: "
            "{FIXED [customer_id] : SUM([Amount])}\n")
        self.assertIn("calc_lod", evidence)
        self.assertNotIn("calc_basic", evidence)

    def test_window_semantics_evidence_a_table_calculation(self):
        evidence = self._families(
            "table T\n\tmeasure 'Rank' = RANKX(ALL('T'), SUM('T'[Amount]))\n"
            "\t\tannotation Copilot_Description = Migrated from Tableau: RANK(SUM([Amount]))\n")
        self.assertIn("calc_table", evidence)
        self.assertNotIn("calc_basic", evidence)

    def test_a_windowed_expression_using_allexcept_is_not_read_as_a_lod(self):
        """WINDOW_* converts to CALCULATE(..., ALLEXCEPT), which would collide."""
        evidence = self._families(
            "table T\n\tmeasure 'Window' = "
            "CALCULATE(SUM('T'[Amount]), ALLSELECTED('T'), ALLEXCEPT('T', 'T'[region]))\n"
            "\t\tannotation Copilot_Description = Migrated from Tableau: "
            "WINDOW_SUM(SUM([Amount]))\n")
        self.assertIn("calc_table", evidence)
        self.assertNotIn("calc_lod", evidence)

    def test_a_generated_helper_is_not_a_migrated_calculation(self):
        """The R2 measure uses RANKX but came from no Tableau calculation."""
        evidence = self._families(
            "table T\n\tmeasure 'R2 Sheet1' = "
            "POWER(CORREL(ADDCOLUMNS(ALL('T'), \"_idx\", "
            "RANKX(ALL('T'), [Amount],,ASC,Dense)), [_idx], [Amount]), 2)\n"
            "\t\tdisplayFolder: Analytics\n")
        self.assertNotIn("calc_table", evidence)
        self.assertNotIn("calc_basic", evidence)

    def test_a_plain_column_evidences_no_calculation_family(self):
        evidence = self._families(
            "table T\n\tcolumn Name\n\t\tdataType: string\n")
        for key in ("calc_basic", "calc_lod", "calc_table"):
            self.assertNotIn(key, evidence)

    def test_linguistic_culture_is_target_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "Demo.SemanticModel", "definition",
                                "cultures", "en-US.tmdl"),
                   "culture en-US\n\tlinguisticMetadata =\n"
                   "\t\t```\n\t\t{\"Entities\": {\"Region\": {}}}\n\t\t```\n")
            evidence = collect_target_evidence(tmp, "Demo")
        self.assertIn("linguistic_schema", evidence)

    def test_missing_linguistic_culture_is_not_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = collect_target_evidence(tmp, "Demo")
        self.assertNotIn("linguistic_schema", evidence)

    def test_a_plain_measure_is_not_evidence_of_an_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "Demo.SemanticModel", "definition",
                                "tables", "Measures.tmdl"),
                   "table Measures\n"
                   "\tmeasure 'Revenue' = SUM('Sales'[Amount])\n")
            evidence = collect_target_evidence(tmp, "Demo")
        self.assertNotIn("alias_measure_name", evidence)

    def test_unclassified_columns_are_not_group_or_bin_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write(os.path.join(tmp, "Demo.SemanticModel", "definition",
                                "tables", "Plain.tmdl"),
                   "table Plain\n\tcolumn Value\n\t\tdataType: string\n")
            evidence = collect_target_evidence(tmp, "Demo")
        self.assertNotIn("groups", evidence)
        self.assertNotIn("bins", evidence)


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
                {"filters": [1], "sets": [1]},
                _full_project(tmp), "Demo")
        coverage = scan.evidence_coverage
        self.assertEqual(1, coverage["unchecked_features"])
        self.assertEqual(100.0, coverage["coverage_percent"])
        self.assertEqual(
            "not_checked",
            next(u for u in scan.usages if u.key == "sets").evidence_status)

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
