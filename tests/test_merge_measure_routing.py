"""A merged model silently lost every measure from its data tables.

Two faults compounded. The generator routes calculations to a table by
``datasource_name``, reading the copy held *inside* the datasource; merging
replaces the source datasources with one new one, so every calculation still
named ``federated.1`` while the merged datasource was ``SharedModel``, matched
nothing, and was dropped. Repointing them then exposed the second fault:
datasource routing sends everything to the widest table in the datasource, so
Financial_Report's measures landed on HR_Analytics's ``Employees`` table and
its thin report could not bind to them.

The visible symptom was a thin report referencing ``'transactions'.'Revenue'``
against a model where ``transactions`` had 8 columns and 0 measures.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from powerbi_import.shared_model import (  # noqa: E402
    _attribute_calculations_to_source_tables,
    _workbook_main_table,
    assess_merge,
    merge_semantic_models,
)


def _workbook(table, columns, calcs, extra_table=None):
    tables = [{"name": table, "columns": [{"name": c} for c in columns]}]
    if extra_table:
        tables.append({"name": extra_table[0],
                       "columns": [{"name": c} for c in extra_table[1]]})
    return {
        "datasources": [{
            "name": "federated.1",
            "tables": tables,
            "columns": [{"name": c} for c in columns],
            "calculations": [dict(c) for c in calcs],
        }],
        "calculations": [dict(c) for c in calcs],
        "worksheets": [],
        "dashboards": [],
    }


FINANCE = [
    {"name": "[Revenue]", "caption": "Revenue", "formula": "SUM([amount])",
     "role": "measure", "datasource_name": "federated.1"},
    {"name": "[Expenses]", "caption": "Expenses", "formula": "SUM([cost])",
     "role": "measure", "datasource_name": "federated.1"},
]
HR = [
    {"name": "[Headcount]", "caption": "Headcount", "formula": "COUNT([id])",
     "role": "measure", "datasource_name": "federated.1"},
]


class TestWorkbookMainTable(unittest.TestCase):

    def test_the_widest_table_wins(self):
        extracted = _workbook("transactions", ["a", "b", "c"], [],
                              extra_table=("lookup", ["k"]))
        self.assertEqual("transactions", _workbook_main_table(extracted))

    def test_no_tables_yields_empty(self):
        self.assertEqual("", _workbook_main_table({"datasources": []}))


class TestCalculationsSurviveTheMerge(unittest.TestCase):

    def setUp(self):
        self.converted = [
            _workbook("Employees", ["id", "name", "dept", "salary"], HR),
            _workbook("transactions", ["amount", "cost"], FINANCE),
        ]
        self.names = ["HR", "Finance"]
        self.merged = merge_semantic_models(
            self.converted, assess_merge(self.converted, self.names), "M")
        self.datasource = self.merged["datasources"][0]

    def test_no_calculation_is_lost(self):
        self.assertEqual(3, len(self.merged["calculations"]))

    def test_calculations_point_at_the_merged_datasource(self):
        """Pointing at a datasource that no longer exists routes nowhere."""
        merged_name = self.datasource["name"]
        for calc in self.merged["calculations"]:
            self.assertEqual(merged_name, calc.get("datasource_name"))

    def test_the_datasource_copy_is_repointed_too(self):
        """The generator reads this copy, not the top-level list."""
        merged_name = self.datasource["name"]
        for calc in self.datasource.get("calculations", []):
            self.assertEqual(merged_name, calc.get("datasource_name"))

    def test_each_measure_keeps_its_own_table(self):
        by_caption = {c.get("caption"): c.get("table")
                      for c in self.merged["calculations"]}
        self.assertEqual("transactions", by_caption["Revenue"])
        self.assertEqual("transactions", by_caption["Expenses"])
        self.assertEqual("Employees", by_caption["Headcount"])

    def test_measures_do_not_all_land_on_the_widest_table(self):
        """Employees is wider, so naive routing would capture everything."""
        tables = {c.get("table") for c in self.merged["calculations"]}
        self.assertIn("transactions", tables)
        self.assertGreater(len(tables), 1)


class TestAttributionRules(unittest.TestCase):

    def _merged(self, calcs, available=("transactions", "Employees")):
        merged = {"calculations": calcs}
        datasource = {"tables": [{"name": t} for t in available],
                      "calculations": []}
        _attribute_calculations_to_source_tables(
            merged, datasource,
            [_workbook("transactions", ["a", "b"], []),
             _workbook("Employees", ["a"], [])],
            ["Finance", "HR"])
        return calcs

    def test_an_explicit_table_is_never_overwritten(self):
        calcs = [{"caption": "X", "table": "Chosen",
                  "_source_workbooks": ["Finance"]}]
        self.assertEqual("Chosen", self._merged(calcs)[0]["table"])

    def test_a_shared_calculation_is_left_to_default_routing(self):
        """Defined in both workbooks with different tables: no safe answer."""
        calcs = [{"caption": "X", "_source_workbooks": ["Finance", "HR"]}]
        self.assertIsNone(self._merged(calcs)[0].get("table"))

    def test_a_table_missing_from_the_merged_model_is_not_used(self):
        calcs = [{"caption": "X", "_source_workbooks": ["Finance"]}]
        result = self._merged(calcs, available=("Employees",))
        self.assertIsNone(result[0].get("table"))

    def test_a_calculation_without_provenance_is_left_alone(self):
        calcs = [{"caption": "X"}]
        self.assertIsNone(self._merged(calcs)[0].get("table"))


class TestGeneratorHonoursAttribution(unittest.TestCase):
    """The merge can record the table; the generator has to use it.

    Datasource routing alone sends every calculation to the widest table, so
    an attribution the generator ignores changes nothing.
    """

    def _generate(self, attributed_table):
        import tempfile

        from powerbi_import import tmdl_generator

        calc = {"name": "[Revenue]", "caption": "Revenue",
                "formula": "SUM([amount])", "role": "measure",
                "datasource_name": "SharedModel"}
        if attributed_table:
            calc["table"] = attributed_table

        datasources = [{
            "name": "SharedModel",
            "connection": {"type": "excel"},
            "tables": [
                {"name": "Employees",
                 "columns": [{"name": n, "datatype": "string"}
                             for n in ("id", "name", "dept", "salary")]},
                {"name": "transactions",
                 "columns": [{"name": n, "datatype": "real"}
                             for n in ("amount", "cost")]},
            ],
            "columns": [],
            "calculations": [calc],
        }]

        with tempfile.TemporaryDirectory() as td:
            tmdl_generator.generate_tmdl(
                datasources=datasources,
                report_name="M",
                extra_objects={"calculations": [calc]},
                output_dir=td,
            )
            tables_dir = os.path.join(td, "definition", "tables")
            found = {}
            for filename in os.listdir(tables_dir):
                with open(os.path.join(tables_dir, filename),
                          encoding="utf-8") as fh:
                    found[filename[:-5]] = "measure Revenue" in fh.read() or \
                                           "measure 'Revenue'" in fh.read()
            return found

    def test_the_measure_follows_its_attribution(self):
        found = self._generate("transactions")
        self.assertTrue(found.get("transactions"),
                        f"Revenue not emitted on transactions: {found}")

    def test_without_attribution_it_falls_back_to_the_widest_table(self):
        """Documents the default the attribution exists to override."""
        found = self._generate(None)
        self.assertFalse(found.get("transactions", False),
                         "fallback unexpectedly routed to the narrow table")


if __name__ == "__main__":
    unittest.main()
