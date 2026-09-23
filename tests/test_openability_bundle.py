"""The openability gate assumed one report per project.

A shared model is one SemanticModel beside N thin reports, so three checks
were wrong for it:

* `pbip_contract` demanded every `.Report` and `.SemanticModel` share one
  name, which a bundle never does.
* `manifest_coherence` derived the report folder from the model name instead
  of reading the path each `.pbip` declares.
* `visual_bindings` validated `report_dirs[0]` only, so the verdict depended
  on glob order. A bundle whose empty model-explorer report sorted first
  passed while its thin reports went unexamined -- renaming the model from
  "DiagModel" to "MTFinal" flipped the same project from OPENS to BLOCKED.

The first two produced false failures. The third produced false passes, which
is worse, and is what these tests mainly exist to prevent.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from powerbi_import.openability import (  # noqa: E402
    _check_manifest_coherence,
    _check_pbip_contract,
    _check_visual_bindings,
    _declared_reports,
)


def _write(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def _report(root, name, model_name="Shared"):
    folder = os.path.join(root, f"{name}.Report")
    _write(os.path.join(folder, ".platform"), {
        "metadata": {"type": "Report", "displayName": name},
        "config": {"version": "2.0", "logicalId": f"id-{name}"},
    })
    _write(os.path.join(folder, "definition.pbir"), {
        "version": "4.0",
        "datasetReference": {"byPath": {"path": f"../{model_name}.SemanticModel"}},
    })
    _write(os.path.join(folder, "definition", "version.json"), {"dataFormatVersion": "2.0"})
    _write(os.path.join(folder, "definition", "report.json"), {"themeCollection": {}})
    _write(os.path.join(folder, "definition", "pages", "pages.json"),
           {"pageOrder": ["ReportSection"], "activePageName": "ReportSection"})
    _write(os.path.join(folder, "definition", "pages", "ReportSection", "page.json"),
           {"name": "ReportSection", "displayName": name})
    return folder


def _model(root, name="Shared"):
    folder = os.path.join(root, f"{name}.SemanticModel")
    _write(os.path.join(folder, ".platform"), {
        "metadata": {"type": "SemanticModel", "displayName": name},
        "config": {"version": "2.0", "logicalId": f"id-{name}"},
    })
    _write(os.path.join(folder, "definition.pbism"), {"version": "4.0"})
    definition = os.path.join(folder, "definition")
    os.makedirs(definition, exist_ok=True)
    for stem in ("model", "database", "expressions"):
        with open(os.path.join(definition, f"{stem}.tmdl"),
                  "w", encoding="utf-8") as fh:
            fh.write(f"{stem}\n")
    tables = os.path.join(definition, "tables")
    os.makedirs(tables, exist_ok=True)
    with open(os.path.join(tables, "Sales.tmdl"), "w", encoding="utf-8") as fh:
        fh.write("table Sales\n\tcolumn Amount\n\t\tdataType: int64\n")
    return folder


def _pbip(root, stem, report_folder):
    _write(os.path.join(root, f"{stem}.pbip"),
           {"version": "1.0",
            "artifacts": [{"report": {"path": report_folder}}]})


class TestDeclaredReports(unittest.TestCase):

    def test_it_reads_the_declared_path(self):
        with tempfile.TemporaryDirectory() as td:
            _pbip(td, "Shared", "Shared_Model.Report")
            self.assertEqual(["Shared_Model.Report"],
                             list(_declared_reports(td).values()))

    def test_a_pbip_declaring_nothing_is_reported_as_none(self):
        with tempfile.TemporaryDirectory() as td:
            _write(os.path.join(td, "Shared.pbip"), {"version": "1.0", "artifacts": []})
            self.assertEqual([None], list(_declared_reports(td).values()))


class TestSingleProjectStillValidates(unittest.TestCase):
    """The common layout must behave exactly as before."""

    def test_a_matching_pair_passes(self):
        with tempfile.TemporaryDirectory() as td:
            _model(td, "Sales")
            _report(td, "Sales", model_name="Sales")
            _pbip(td, "Sales", "Sales.Report")
            self.assertTrue(_check_pbip_contract(td).ok)
            self.assertTrue(_check_manifest_coherence(td).ok)

    def test_a_missing_shell_file_still_fails(self):
        with tempfile.TemporaryDirectory() as td:
            _model(td, "Sales")
            folder = _report(td, "Sales", model_name="Sales")
            _pbip(td, "Sales", "Sales.Report")
            os.remove(os.path.join(folder, "definition", "report.json"))
            self.assertFalse(_check_pbip_contract(td).ok)


class TestBundleLayout(unittest.TestCase):

    def _bundle(self, root):
        _model(root, "Shared")
        _report(root, "Shared_Model")
        _report(root, "HR")
        _report(root, "Finance")
        _pbip(root, "Shared", "Shared_Model.Report")
        _pbip(root, "HR", "HR.Report")
        _pbip(root, "Finance", "Finance.Report")

    def test_a_bundle_passes_the_contract(self):
        with tempfile.TemporaryDirectory() as td:
            self._bundle(td)
            result = _check_pbip_contract(td)
            self.assertTrue(result.ok, result.issues)

    def test_a_bundle_passes_manifest_coherence(self):
        with tempfile.TemporaryDirectory() as td:
            self._bundle(td)
            result = _check_manifest_coherence(td)
            self.assertTrue(result.ok, result.issues)

    def test_an_undeclared_report_is_caught(self):
        with tempfile.TemporaryDirectory() as td:
            self._bundle(td)
            _report(td, "Rogue")  # no .pbip declares it
            result = _check_pbip_contract(td)
            self.assertFalse(result.ok)
            self.assertTrue(any("Rogue" in issue for issue in result.issues))

    def test_a_declared_report_that_does_not_exist_is_caught(self):
        with tempfile.TemporaryDirectory() as td:
            self._bundle(td)
            _pbip(td, "Ghost", "Ghost.Report")
            result = _check_manifest_coherence(td)
            self.assertFalse(result.ok)
            self.assertTrue(any("Ghost" in issue for issue in result.issues))

    def test_two_semantic_models_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            self._bundle(td)
            _model(td, "Second")
            self.assertFalse(_check_pbip_contract(td).ok)


class TestVisualBindingsCoverEveryReport(unittest.TestCase):
    """The false-pass this file mainly exists for."""

    def _project(self, root, bad_report_name):
        _model(root, "Shared")
        for name in ("AAA_Model", "ZZZ_Thin"):
            _report(root, name)
            _pbip(root, name, f"{name}.Report")
        # Give one report a visual bound to a column the model lacks.
        page_dir = os.path.join(root, f"{bad_report_name}.Report", "definition",
                                "pages", "ReportSection", "visuals", "v1")
        _write(os.path.join(page_dir, "visual.json"), {
            "name": "v1",
            "visual": {"visualType": "card", "query": {"queryState": {"Values": {
                "projections": [{"field": {"Column": {
                    "Expression": {"SourceRef": {"Entity": "Sales"}},
                    "Property": "NotAColumn"}}}]}}}},
        })

    def test_a_defect_in_the_last_report_is_still_found(self):
        """Previously only report_dirs[0] was examined."""
        with tempfile.TemporaryDirectory() as td:
            self._project(td, "ZZZ_Thin")
            result = _check_visual_bindings(td)
            self.assertFalse(result.ok, "orphaned binding in a later report missed")
            self.assertTrue(any("ZZZ_Thin" in issue for issue in result.issues),
                            result.issues)

    def test_a_defect_in_the_first_report_is_found(self):
        with tempfile.TemporaryDirectory() as td:
            self._project(td, "AAA_Model")
            result = _check_visual_bindings(td)
            self.assertFalse(result.ok)
            self.assertTrue(any("AAA_Model" in issue for issue in result.issues),
                            result.issues)

    def test_issues_name_the_report_they_came_from(self):
        with tempfile.TemporaryDirectory() as td:
            self._project(td, "ZZZ_Thin")
            for issue in _check_visual_bindings(td).issues:
                self.assertIn(".Report/", issue)


if __name__ == "__main__":
    unittest.main()
