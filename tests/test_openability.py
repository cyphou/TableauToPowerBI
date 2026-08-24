"""Tests for the PBI Desktop openability preflight (openability.py)."""

import json
import os
import tempfile
import unittest

from powerbi_import.openability import (
    check_openability,
    extract_m_partitions,
    OpenabilityReport,
    CheckResult,
)


def _tmdl_with_m(m_lines):
    """Build a TMDL table with an M partition (4-tab indented, generator style)."""
    body = ["table 'Sales'", "\tcolumn A", "\t\tdataType: string", ""]
    body.append("\tpartition 'Sales-guid' = m")
    body.append("\t\tmode: import")
    body.append("\t\tsource =")
    for l in m_lines:
        body.append(f"\t\t\t\t{l}")
    body.append("")
    return "\n".join(body)


def _write_project(root, tmdl_text=None, visual=None, add_pbir=True):
    sm = os.path.join(root, "P.SemanticModel", "definition", "tables")
    os.makedirs(sm, exist_ok=True)
    if tmdl_text is not None:
        with open(os.path.join(sm, "Sales.tmdl"), "w", encoding="utf-8") as f:
            f.write(tmdl_text)
    rep = os.path.join(root, "P.Report", "definition")
    os.makedirs(rep, exist_ok=True)
    with open(os.path.join(root, "P.Report", "report.json"), "w",
              encoding="utf-8") as f:
        json.dump({"$schema": "x", "config": {}}, f)
    if add_pbir:
        with open(os.path.join(root, "P.Report", "definition.pbir"), "w",
                  encoding="utf-8") as f:
            json.dump({"version": "4.0", "datasetReference": {
                "byPath": {"path": "../P.SemanticModel"}}}, f)
    if visual is not None:
        pdir = os.path.join(rep, "pages", "p1")
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "page.json"), "w", encoding="utf-8") as f:
            json.dump({"$schema": "x", "name": "p1", "displayName": "P1"}, f)
        vdir = os.path.join(pdir, "visuals", "v1")
        os.makedirs(vdir, exist_ok=True)
        with open(os.path.join(vdir, "visual.json"), "w", encoding="utf-8") as f:
            json.dump(visual, f)


# ── extract_m_partitions ────────────────────────────────────────────

class TestExtract(unittest.TestCase):
    def test_extracts_single_partition(self):
        t = _tmdl_with_m(["let", "    Source = 1", "in", "    Source"])
        parts = extract_m_partitions(t)
        self.assertEqual(len(parts), 1)
        name, m = parts[0]
        self.assertEqual(name, "Sales-guid")
        self.assertIn("Source = 1", m)
        self.assertTrue(m.startswith("let"))

    def test_preserves_internal_blank_lines(self):
        # generator writes blank M lines with the 4-tab prefix
        t = _tmdl_with_m(["let", "", "    Source = 1", "in", "    Source"])
        _, m = extract_m_partitions(t)[0]
        self.assertIn("\n\n", m)

    def test_ignores_calculated_partition(self):
        t = ("table T\n\tpartition T-g = calculated\n\t\tmode: import\n"
             "\t\tsource = ```\n\t\t\t\tROW(\"x\",1)\n\t\t\t\t```\n")
        self.assertEqual(extract_m_partitions(t), [])

    def test_multiple_partitions(self):
        t = _tmdl_with_m(["let x = 1 in x"]) + "\n" + \
            "\tpartition 'T2-g' = m\n\t\tmode: import\n\t\tsource =\n\t\t\t\tlet y=2 in y\n"
        parts = extract_m_partitions(t)
        self.assertEqual(len(parts), 2)

    def test_empty_text(self):
        self.assertEqual(extract_m_partitions(""), [])

    def test_quoted_name_unescaped(self):
        t = "\tpartition 'a''b' = m\n\t\tmode: import\n\t\tsource =\n\t\t\t\tlet x=1 in x\n"
        name, _ = extract_m_partitions(t)[0]
        self.assertEqual(name, "a'b")


# ── check_openability ───────────────────────────────────────────────

class TestOpenability(unittest.TestCase):
    def test_clean_project_openable(self):
        with tempfile.TemporaryDirectory() as d:
            _write_project(d, _tmdl_with_m(["let", "    Source = 1", "in", "    Source"]),
                           visual={"$schema": "x", "name": "v1", "visual": {}})
            r = check_openability(d)
            self.assertIsInstance(r, OpenabilityReport)
            self.assertTrue(r.openable, r.blocking_issues)
            self.assertEqual(r.blocking_issues, [])

    def test_broken_m_blocks_open(self):
        with tempfile.TemporaryDirectory() as d:
            # unmatched paren -> invalid M
            _write_project(d, _tmdl_with_m(["let", "    Source = Table.X(a", "in", "    Source"]))
            r = check_openability(d)
            self.assertFalse(r.openable)
            self.assertTrue(any("power_query" in b for b in r.blocking_issues))

    def test_power_query_check_names_partition(self):
        with tempfile.TemporaryDirectory() as d:
            _write_project(d, _tmdl_with_m(["let Source = f(( in Source"]))
            r = check_openability(d)
            pq = [c for c in r.checks if c.name == "power_query"][0]
            self.assertFalse(pq.ok)
            self.assertTrue(any("Sales-guid" in i for i in pq.issues))

    def test_bad_json_blocks_open(self):
        with tempfile.TemporaryDirectory() as d:
            _write_project(d, _tmdl_with_m(["let x=1 in x"]))
            bad = os.path.join(d, "P.Report", "definition", "broken.json")
            with open(bad, "w", encoding="utf-8") as f:
                f.write("{not valid json")
            r = check_openability(d)
            self.assertFalse(r.openable)
            self.assertTrue(any("json_parse" in b for b in r.blocking_issues))

    def test_missing_structure_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "empty"))
            r = check_openability(d)
            self.assertFalse(r.openable)

    def test_missing_dir_returns_not_openable(self):
        r = check_openability(os.path.join(tempfile.gettempdir(), "does-not-exist-xyz"))
        self.assertFalse(r.openable)

    def test_missing_schema_is_warning_not_blocking(self):
        with tempfile.TemporaryDirectory() as d:
            _write_project(d, _tmdl_with_m(["let x=1 in x"]),
                           visual={"name": "v1", "visual": {}})  # no $schema
            r = check_openability(d)
            self.assertTrue(r.openable)          # warning only
            self.assertTrue(any("schema" in w for w in r.warnings))

    def test_bad_dax_blocks_open(self):
        with tempfile.TemporaryDirectory() as d:
            tmdl = ("table 'Sales'\n\tmeasure 'M' = SUM([a]\n"  # unmatched paren
                    + _tmdl_with_m(["let x=1 in x"]))
            _write_project(d, tmdl)
            r = check_openability(d)
            self.assertFalse(r.openable)
            self.assertTrue(any("dax" in b for b in r.blocking_issues))

    def test_report_to_dict_shape(self):
        with tempfile.TemporaryDirectory() as d:
            _write_project(d, _tmdl_with_m(["let x=1 in x"]))
            data = check_openability(d).to_dict()
            for key in ("project_dir", "openable", "blocking_count",
                        "warning_count", "blocking_issues", "warnings", "checks"):
                self.assertIn(key, data)
            self.assertEqual(len(data["checks"]), 9)

    def test_check_names_present(self):
        with tempfile.TemporaryDirectory() as d:
            _write_project(d, _tmdl_with_m(["let x=1 in x"]))
            names = {c.name for c in check_openability(d).checks}
            self.assertEqual(names, {"structure", "json_parse", "tmdl_present",
                                     "tmdl_partitions", "power_query", "dax",
                                     "references", "report_structure", "schema"})

    def test_no_tmdl_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            # report present but semantic model has no .tmdl
            rep = os.path.join(d, "P.Report", "definition")
            os.makedirs(rep, exist_ok=True)
            os.makedirs(os.path.join(d, "P.SemanticModel", "definition"), exist_ok=True)
            with open(os.path.join(d, "P.Report", "definition.pbir"), "w",
                      encoding="utf-8") as f:
                json.dump({"version": "4.0"}, f)
            r = check_openability(d)
            self.assertFalse(r.openable)
            self.assertTrue(any("tmdl_present" in b for b in r.blocking_issues))

    def test_empty_m_partition_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            _write_project(d, _tmdl_with_m([]))  # source = with no M lines
            r = check_openability(d)
            pq = [c for c in r.checks if c.name == "power_query"][0]
            self.assertTrue(pq.ok)

    def test_dangling_model_reference_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            _write_project(d, _tmdl_with_m(["let x=1 in x"]))
            # point the report at a non-existent model
            with open(os.path.join(d, "P.Report", "definition.pbir"), "w",
                      encoding="utf-8") as f:
                json.dump({"version": "4.0", "datasetReference": {
                    "byPath": {"path": "../Does.Not.Exist.SemanticModel"}}}, f)
            r = check_openability(d)
            self.assertFalse(r.openable)
            self.assertTrue(any("references" in b for b in r.blocking_issues))

    def test_valid_model_reference_ok(self):
        with tempfile.TemporaryDirectory() as d:
            _write_project(d, _tmdl_with_m(["let x=1 in x"]))
            r = check_openability(d)
            refs = [c for c in r.checks if c.name == "references"][0]
            self.assertTrue(refs.ok, refs.issues)

    def test_missing_report_json_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            _write_project(d, _tmdl_with_m(["let x=1 in x"]))
            report_json = os.path.join(d, "P.Report", "report.json")
            if os.path.exists(report_json):
                os.remove(report_json)
            r = check_openability(d)
            self.assertFalse(r.openable)
            self.assertTrue(any("report_structure" in b for b in r.blocking_issues))

    def test_missing_page_json_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            _write_project(
                d,
                _tmdl_with_m(["let x=1 in x"]),
                visual={"$schema": "x", "name": "v1", "visual": {}},
            )
            page_json = os.path.join(
                d, "P.Report", "definition", "pages", "p1", "page.json")
            with open(page_json, "w", encoding="utf-8") as f:
                json.dump({"name": "p1", "displayName": "P1"}, f)
            os.remove(page_json)
            r = check_openability(d)
            self.assertFalse(r.openable)
            self.assertTrue(any("report_structure" in b for b in r.blocking_issues))

    def test_table_without_partition_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            tmdl = "table 'Sales'\n\tcolumn A\n\t\tdataType: string\n"
            _write_project(d, tmdl)
            r = check_openability(d)
            self.assertFalse(r.openable)
            self.assertTrue(any("tmdl_partitions" in b for b in r.blocking_issues))

    def test_m_partition_without_source_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            tmdl = (
                "table 'Sales'\n"
                "\tcolumn A\n"
                "\t\tdataType: string\n"
                "\tpartition 'Sales-guid' = m\n"
                "\t\tmode: import\n"
            )
            _write_project(d, tmdl)
            r = check_openability(d)
            self.assertFalse(r.openable)
            self.assertTrue(any("tmdl_partitions" in b for b in r.blocking_issues))


class TestCheckResult(unittest.TestCase):
    def test_to_dict(self):
        c = CheckResult("x", False, "error", ["boom"])
        self.assertEqual(c.to_dict(),
                         {"name": "x", "ok": False, "severity": "error",
                          "issues": ["boom"]})


if __name__ == "__main__":
    unittest.main()
