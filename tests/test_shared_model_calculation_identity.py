"""Static regressions for shared-model calculation identity before merging."""

import builtins
import io
import os
import tempfile
import unittest
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from powerbi_import.dax_validator import validate_dax_expression
from powerbi_import.errors import PartialGenerationError
from powerbi_import.shared_model import (
    assess_merge,
    merge_preview,
    merge_semantic_models,
)
from powerbi_import.tmdl_generator import _build_semantic_model, generate_tmdl


def _workbook(identifier, caption, *, table="Fact", inventory="both"):
    calculation = {
        "name": f"[{identifier}]", "caption": caption,
        "formula": "SUM([Amount])", "role": "measure", "datatype": "real",
        "datasource_name": "Source",
    }
    return {
        "datasources": [{
            "name": "Source",
            "connection": {
                "type": "SQL Server",
                "details": {"server": "synthetic.invalid", "database": "fixture"},
            },
            "tables": [{"name": table, "columns": [
                {"name": "Amount", "datatype": "real"}]}],
            "relationships": [],
            "calculations": ([] if inventory == "workbook"
                             else [deepcopy(calculation)]),
        }],
        "calculations": ([] if inventory == "datasource"
                         else [deepcopy(calculation)]),
    }


@contextmanager
def _forbid_filesystem_writes():
    """Permit reads but make common Python filesystem mutation paths fail."""
    builtin_open, io_open, os_open = builtins.open, io.open, os.open

    def guarded_open(opener):
        def call(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                raise AssertionError(f"Unexpected write-open: {file!s}, {mode!r}")
            return opener(file, mode, *args, **kwargs)
        return call

    def guarded_os_open(file, flags, *args, **kwargs):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        if flags & write_flags:
            raise AssertionError(f"Unexpected low-level write-open: {file!s}")
        return os_open(file, flags, *args, **kwargs)

    def reject_mutation(*args, **kwargs):
        raise AssertionError(f"Unexpected filesystem mutation: {args!r}")

    with patch("builtins.open", side_effect=guarded_open(builtin_open)), \
         patch("io.open", side_effect=guarded_open(io_open)), \
         patch("os.open", side_effect=guarded_os_open), \
         patch("os.mkdir", side_effect=reject_mutation), \
         patch("os.makedirs", side_effect=reject_mutation), \
         patch("os.remove", side_effect=reject_mutation), \
         patch("os.unlink", side_effect=reject_mutation), \
         patch("os.rename", side_effect=reject_mutation), \
         patch("os.replace", side_effect=reject_mutation):
        yield


class TestSharedModelCalculationIdentity(unittest.TestCase):
    def test_caption_collision_refuses_merge_and_preview_before_any_write(self):
        # Exercise each original inventory independently as well as the usual
        # extractor shape, which repeats the declaration in both inventories.
        for inventory in ("both", "datasource", "workbook"):
            for reverse in (False, True):
                for operation in ("merge", "preview"):
                    with self.subTest(inventory=inventory, reverse=reverse,
                                      operation=operation):
                        workbooks = [
                            _workbook("A", "Shared total", inventory=inventory),
                            _workbook("B", "Shared total", inventory=inventory),
                        ]
                        names = ["WB_A", "WB_B"]
                        if reverse:
                            workbooks.reverse()
                            names.reverse()
                        original = deepcopy(workbooks)
                        assessment = assess_merge(workbooks, names)
                        self.assertEqual(assessment.measure_conflicts, [])
                        self.assertEqual(assessment.isolated_tables, {})
                        with tempfile.TemporaryDirectory() as tmp:
                            root = Path(tmp)
                            sentinel = root / "existing.txt"
                            sentinel.write_text("unchanged", encoding="utf-8")
                            old_cwd = os.getcwd()
                            try:
                                os.chdir(root)
                                with _forbid_filesystem_writes():
                                    with self.assertRaises(PartialGenerationError) as raised:
                                        if operation == "merge":
                                            merge_semantic_models(
                                                workbooks, assessment, "Shared")
                                        else:
                                            merge_preview(workbooks, names)
                            finally:
                                os.chdir(old_cwd)
                            self.assertEqual(list(root.iterdir()), [sentinel])
                            self.assertEqual(sentinel.read_text(encoding="utf-8"),
                                             "unchanged")
                        error = raised.exception
                        self.assertEqual(error.item, "Shared total")
                        self.assertEqual(error.recovery_kind,
                                         "calculation_identity_binding")
                        self.assertEqual(set(error.source.split(", ")), set(names))
                        self.assertIn("distinct or unproven source identities",
                                      error.message)
                        evidence = " ".join(error.required_evidence)
                        self.assertIn("'A'", evidence)
                        self.assertIn("'B'", evidence)
                        self.assertIn("'Fact'", evidence)
                        self.assertEqual(workbooks, original)

    def test_distinct_captions_remain_distinct_through_tmdl_generation(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                workbooks = [_workbook("A", "Total A"), _workbook("B", "Total B")]
                names = ["WB_A", "WB_B"]
                if reverse:
                    workbooks.reverse()
                    names.reverse()
                original = deepcopy(workbooks)
                assessment = assess_merge(workbooks, names)
                merged = merge_semantic_models(workbooks, assessment, "Shared")
                self.assertEqual(workbooks, original)
                for inventory in (merged["calculations"],
                                  merged["datasources"][0]["calculations"]):
                    self.assertEqual(
                        {(calc["name"], calc["caption"], calc["table"])
                         for calc in inventory},
                        {("[A]", "Total A", "Fact"), ("[B]", "Total B", "Fact")})
                    self.assertEqual(len(inventory), 2)

                model = _build_semantic_model(
                    deepcopy(merged["datasources"]), "Shared", extra_objects=deepcopy(merged),
                    model_mode="import")
                fact = next(table for table in model["model"]["tables"]
                            if table["name"] == "Fact")
                for caption, identifier in (("Total A", "[A]"), ("Total B", "[B]")):
                    measures = [measure for measure in fact["measures"]
                                if measure["name"] == caption]
                    self.assertEqual(len(measures), 1)
                    measure = measures[0]
                    self.assertEqual(measure["_tableau_calculation_identity"]["name"],
                                     identifier)
                    self.assertIn("SUM('Fact'[Amount])", measure["expression"])
                    self.assertNotIn("TODO", measure["expression"])
                    self.assertEqual(validate_dax_expression(measure["expression"]), [])

                # This is static local generation only: no connector is queried.
                with tempfile.TemporaryDirectory() as tmp:
                    stats = generate_tmdl(
                        deepcopy(merged["datasources"]), "Shared", deepcopy(merged), tmp,
                        model_mode="import", parameterize=False)
                    target = Path(tmp, "definition", "tables", "Fact.tmdl")
                    document = target.read_text(encoding="utf-8")
                    self.assertEqual(stats["source_issues"], [])
                    for caption, identifier in (("Total A", "A"), ("Total B", "B")):
                        declarations = [
                            line for line in document.splitlines()
                            if line.startswith(f"\tmeasure '{caption}' =")
                        ]
                        self.assertEqual(len(declarations), 1)
                        self.assertIn(f"identifier: {identifier}", declarations[0])
                        self.assertIn("SUM('Fact'[Amount])", declarations[0])
                        self.assertNotIn("TODO", declarations[0])

    def test_same_caption_on_distinct_proven_tables_survives_both_inventories(self):
        # This checks only Merger's inventories, not the global PBIR binder.
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                workbooks = [
                    _workbook("A", "Shared total", table="FactA"),
                    _workbook("B", "Shared total", table="FactB"),
                ]
                names = ["WB_A", "WB_B"]
                if reverse:
                    workbooks.reverse()
                    names.reverse()
                original = deepcopy(workbooks)
                assessment = assess_merge(workbooks, names)
                # This caller explicitly retains both independent target tables.
                assessment.isolated_tables = {}
                merged = merge_semantic_models(workbooks, assessment, "Shared")
                self.assertEqual(workbooks, original)
                datasource, = merged["datasources"]
                self.assertEqual({table["name"] for table in datasource["tables"]},
                                 {"FactA", "FactB"})
                for inventory in (merged["calculations"], datasource["calculations"]):
                    self.assertEqual(len(inventory), 2)
                    by_table = {calc["table"]: calc for calc in inventory}
                    self.assertEqual(set(by_table), {"FactA", "FactB"})
                    for table, identifier, workbook in (
                            ("FactA", "[A]", "WB_A"), ("FactB", "[B]", "WB_B")):
                        calc = by_table[table]
                        self.assertEqual(calc["name"], identifier)
                        self.assertEqual(calc["caption"], "Shared total")
                        self.assertEqual(calc["formula"], "SUM([Amount])")
                        self.assertEqual(calc["_merge_action"], "unique")
                        self.assertEqual(calc["_source_workbooks"], [workbook])
                        self.assertEqual(calc["datasource_name"], "SharedModel")


if __name__ == "__main__":
    unittest.main()
