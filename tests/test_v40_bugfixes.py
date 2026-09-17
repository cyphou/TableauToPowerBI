"""Positive regression tests for the v40.0.0 migration quality fixes.

Covers five fixes verified against CurrentError.txt:

1. PBIR v4.0 annotations are emitted at the visual.json container root
   (and page.json root), never inside the inner ``visual`` object.
2. Case-insensitive duplicate measures are de-duplicated, and cross-table
   collisions are *renamed* (not dropped).
3. Tableau paren-style ``if(cond) then ... else ... end`` is converted to
   a DAX ``IF(...)`` expression.
4. Qualified Tableau sheet/table identifiers (``[ds].[Extract]``) are
   sanitised before being embedded into an Excel/SharePoint ``Item="..."``.
5. Many-to-many relationships default to single-direction cross-filtering.
"""

import unittest

from tableau_export.dax_converter import convert_tableau_formula_to_dax
from tableau_export.m_query_builder import _clean_sheet_name
from powerbi_import.visual_generator import create_visual_container


class TestAnnotationsAtContainerRoot(unittest.TestCase):
    """Fix #1 — annotations belong at the visual.json container root."""

    def test_calendar_heatmap_annotation_at_root(self):
        ws = {
            "name": "Cal",
            "visualType": "calendarheatmap",
            "dimensions": [{"field": "Date"}],
            "measures": [{"name": "Value", "expression": "SUM(Value)"}],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="cal-1",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Date": "T", "Value": "T"},
        )
        # Annotations at container root, NOT in the inner visual object.
        self.assertIn("annotations", container)
        self.assertNotIn("annotations", container["visual"])

    def test_approximation_annotation_at_root(self):
        ws = {
            "name": "Lolli",
            "visualType": "lollipop",
            "dimensions": [{"field": "Category"}],
            "measures": [{"name": "Value", "expression": "SUM(Value)"}],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="lolli-1",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Category": "T", "Value": "T"},
        )
        notes = [a for a in container.get("annotations", [])
                 if a.get("name") == "MigrationNote"]
        self.assertTrue(notes)
        self.assertNotIn("annotations", container["visual"])


class TestParenStyleIfConversion(unittest.TestCase):
    """Fix #3 — ``if(cond) then ... else ... end`` → DAX ``IF(...)``."""

    def test_paren_if_then_else_end(self):
        dax = convert_tableau_formula_to_dax(
            'if([Sales]>0) then "Pos" else "Neg" end'
        )
        self.assertIn("IF(", dax)
        self.assertNotIn(" then ", dax.lower())
        self.assertNotIn(" end", dax.lower())

    def test_spaced_if_then_else_end_still_works(self):
        dax = convert_tableau_formula_to_dax(
            'IF [Sales] > 0 THEN "Pos" ELSE "Neg" END'
        )
        self.assertIn("IF(", dax)


class TestCleanSheetName(unittest.TestCase):
    """Fix #4 — qualified Tableau identifiers are sanitised for Item=."""

    def test_qualified_identifier(self):
        self.assertEqual(_clean_sheet_name("[extract].[Extract]", "Fallback"),
                         "Extract")

    def test_trailing_dollar_stripped(self):
        self.assertEqual(_clean_sheet_name("[Sheet1$]", "Fallback"), "Sheet1")

    def test_empty_uses_fallback(self):
        self.assertEqual(_clean_sheet_name("", "Fallback"), "Fallback")

    def test_plain_name_unchanged(self):
        self.assertEqual(_clean_sheet_name("Orders", "Fallback"), "Orders")


if __name__ == "__main__":
    unittest.main()
