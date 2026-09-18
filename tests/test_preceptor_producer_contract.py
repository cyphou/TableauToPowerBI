"""Regression tests: the preceptor must read what the generator writes.

Every case here corresponds to a false positive the reviewer once raised
because it consumed a key, path or vocabulary the generator never produced.
Each test pins the *producer's* real shape, so a future divergence fails
loudly instead of silently penalising correct output.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from powerbi_import.preceptor import (  # noqa: E402
    _iter_dax_expressions,
    _iter_m_expressions,
    _review_completeness,
    _review_dax_correctness,
    _review_m_query_validity,
    _review_pbir_fidelity,
    _review_tmdl_structure,
)
from tests.test_preceptor import _make_pbip_project  # noqa: E402


#: A partition exactly as tmdl_generator writes M: a bare ``source =``
#: followed by an indented body, with no fence.
_REAL_M_PARTITION = (
    "table Orders\n"
    "\n"
    "\tpartition Orders = m\n"
    "\t\tmode: import\n"
    "\t\tsource =\n"
    "\t\t\t\tlet\n"
    "\t\t\t\t    Source = Csv.Document(File.Contents(\"orders.csv\")),\n"
    "\t\t\t\t    Renamed = Table.RenameColumns(Source, {{\"a\", \"b\"}})\n"
    "\t\t\t\tin\n"
    "\t\t\t\t    Renamed\n"
)

#: A calculated table: fenced, and written in DAX — never M.
_FENCED_DAX_TABLE = (
    "table 'Select Dimension FieldParam'\n"
    "\n"
    "\tpartition 'Select Dimension FieldParam' = calculated\n"
    "\t\tsource = ```\n"
    "\t\t\t\t{\n"
    "\t\t\t\t    (NAMEOF('Owned By'[Account]), 0, \"Account\"),\n"
    "\t\t\t\t    (NAMEOF('Owned By'[Open Flag]), 1, \"Open Flag\")\n"
    "\t\t\t\t}\n"
    "\t\t\t\t```\n"
)


#: A measure block exactly as tmdl_generator emits it: the DAX sits on the
#: ``measure`` line, and the original Tableau formula is preserved in an
#: annotation for Copilot.
_REAL_MEASURE_TMDL = (
    "table fact_sales\n"
    "\n"
    "\tmeasure 'Order Count' = DISTINCTCOUNT('fact_sales'[sale_id])\n"
    "\t\tdisplayFolder: Measures\n"
    "\t\tlineageTag: 6799b751-df0c-46f5-a267-cbf56e9fc64b\n"
    "\t\tannotation Copilot_Description = "
    "Migrated from Tableau: COUNTD([sale_id]) | DAX: DISTINCTCOUNT('fact_sales'[sale_id])\n"
)


class TestDaxSiteParsing(unittest.TestCase):
    """The parser must separate generated DAX from documentation text."""

    def test_annotation_text_is_not_dax(self):
        exprs = list(_iter_dax_expressions(_REAL_MEASURE_TMDL))
        self.assertEqual(exprs, ["DISTINCTCOUNT('fact_sales'[sale_id])"])

    def test_tableau_formula_in_annotation_is_not_a_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp, tmdl_content=_REAL_MEASURE_TMDL)
            score, detail, coaching = _review_dax_correctness(pbip, {})
        self.assertEqual(score, 5, f'annotation misread as DAX: {detail}')
        self.assertEqual(coaching, [])

    def test_truncated_annotation_is_not_a_paren_error(self):
        """Annotations hold unbalanced parens by nature; they are prose."""
        tmdl = (
            "table fact_sales\n"
            "\n"
            "\tmeasure 'Revenue' = SUM('fact_sales'[amount])\n"
            "\t\tannotation Copilot_Description = From Tableau: WINDOW_MAX(SUM([qty]\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp, tmdl_content=tmdl)
            score, _, coaching = _review_dax_correctness(pbip, {})
        self.assertEqual(score, 5)
        self.assertEqual(coaching, [])

    def test_calculation_item_expression_is_still_scanned(self):
        """Calculation groups emit ``expression = <dax>``; keep covering them."""
        tmdl = (
            "table 'Time Intelligence'\n"
            "\tcalculationGroup\n"
            "\t\tcalculationItem YTD\n"
            "\t\t\texpression = COUNTD('fact'[id])\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp, tmdl_content=tmdl)
            score, _, coaching = _review_dax_correctness(pbip, {})
        self.assertLess(score, 5)
        self.assertTrue(any('COUNTD' in c.issue for c in coaching))

    def test_genuine_leak_is_still_caught(self):
        """Negative control: silencing false positives must not blind us."""
        tmdl = (
            "table fact_sales\n"
            "\tmeasure 'Bad' = COUNTD('fact_sales'[sale_id])\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp, tmdl_content=tmdl)
            score, _, coaching = _review_dax_correctness(pbip, {})
        self.assertLess(score, 5)
        self.assertTrue(any('COUNTD' in c.issue for c in coaching))


class TestPbirFidelityReadsGeneratedShape(unittest.TestCase):
    #: Any source with a renderable surface, so the checks below actually run.
    _SURFACE = {'worksheets': [{'name': 'WS0'}]}

    def test_definition_pbir_found_beside_definition_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp)
            _, _, coaching = _review_pbir_fidelity(pbip, self._SURFACE)
        self.assertFalse(
            [c for c in coaching if 'definition.pbir' in c.issue],
            'definition.pbir reported missing though the generator wrote it',
        )

    def test_missing_definition_pbir_is_still_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp, definition_pbir=False)
            _, _, coaching = _review_pbir_fidelity(pbip, self._SURFACE)
        self.assertTrue(any('definition.pbir' in c.issue for c in coaching))

    def test_report_filters_read_from_filter_config(self):
        """PBIR nests report-level filters under ``filterConfig``."""
        report_json = {
            '$schema': 'https://example/report/2.0.0/schema.json',
            'filterConfig': {'filters': [{'name': 'f1'}]},
        }
        extraction = dict(self._SURFACE,
                          filters=[{'type': 'categorical', 'values': ['A']}])
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp, report_json=report_json)
            _, _, coaching = _review_pbir_fidelity(pbip, extraction)
        self.assertFalse([c for c in coaching if 'report-level filters' in c.issue])

    def test_non_restrictive_shelf_entries_are_not_expected_filters(self):
        """A field dropped on the shelf with no domain selects everything."""
        extraction = dict(self._SURFACE, filters=[
            {'field': '[federated.global].[year]', 'type': '', 'values': []},
            {'field': '[is_active]', 'type': '', 'values': []},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp)
            _, _, coaching = _review_pbir_fidelity(pbip, extraction)
        self.assertFalse([c for c in coaching if 'report-level filters' in c.issue])

    def test_restrictive_filter_without_output_is_still_reported(self):
        extraction = dict(self._SURFACE,
                          filters=[{'type': 'categorical', 'values': ['A']}])
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp)
            _, _, coaching = _review_pbir_fidelity(pbip, extraction)
        self.assertTrue(any('report-level filters' in c.issue for c in coaching))


class TestDateTableRecognition(unittest.TestCase):
    """The model needs *a* date dimension, not one literally named Calendar."""

    _EXTRACTION = {'datasources': [{'tables': [
        {'columns': [{'name': 'order_date', 'datatype': 'date'}]}
    ]}]}

    def _project_with_table(self, tmp, table_name):
        pbip = _make_pbip_project(tmp)
        tables = Path(pbip) / 'TestReport.SemanticModel' / 'definition' / 'tables'
        (tables / f'{table_name}.tmdl').write_text(
            f'table {table_name}\n', encoding='utf-8')
        return pbip

    def test_source_date_dimension_satisfies_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            pbip = self._project_with_table(tmp, 'dim_date')
            _, _, coaching = _review_tmdl_structure(pbip, self._EXTRACTION)
        self.assertFalse([c for c in coaching if 'date' in c.issue.lower()])

    def test_missing_date_dimension_is_still_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            pbip = self._project_with_table(tmp, 'fact_sales')
            _, _, coaching = _review_tmdl_structure(pbip, self._EXTRACTION)
        self.assertTrue(any('date' in c.issue.lower() for c in coaching))


class TestMReviewTargetsRealM(unittest.TestCase):
    """The M reviewer must read M partitions, not fenced DAX blocks."""

    def test_bare_source_partition_is_seen_as_m(self):
        blocks = list(_iter_m_expressions(_REAL_M_PARTITION))
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0].startswith('let'))

    def test_fenced_dax_table_is_not_treated_as_m(self):
        self.assertEqual(list(_iter_m_expressions(_FENCED_DAX_TABLE)), [])

    def test_field_parameter_dax_raises_no_m_finding(self):
        """NAMEOF('Table'[Col]) is valid DAX; its quotes are not M strings."""
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp, tmdl_content=_FENCED_DAX_TABLE)
            score, detail, coaching = _review_m_query_validity(pbip, {})
        self.assertEqual(score, 5, detail)
        self.assertEqual(coaching, [])

    def test_m_partitions_are_actually_counted(self):
        """Guards against a reviewer that scores 5 by inspecting nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp, tmdl_content=_REAL_M_PARTITION)
            score, detail, _ = _review_m_query_validity(pbip, {})
        self.assertEqual(score, 5)
        self.assertIn('1 M expressions', detail)

    def test_genuine_m_defects_are_caught(self):
        tmdl = (
            "table Orders\n"
            "\tpartition Orders = m\n"
            "\t\tsource =\n"
            "\t\t\t\tlet\n"
            "\t\t\t\t    Bad = if 1 = 1 then {'a', 'b'},\n"
            "\t\t\t\t    Source = #table(type table [], {})\n"
            "\t\t\t\tin\n"
            "\t\t\t\t    Source\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp, tmdl_content=tmdl)
            score, _, coaching = _review_m_query_validity(pbip, {})
        self.assertLess(score, 5)
        issues = ' '.join(c.issue for c in coaching)
        self.assertIn('if/else', issues)
        self.assertIn('Single-quoted', issues)

    def test_apostrophe_inside_double_quoted_string_is_text(self):
        """A column called Customer's Name is not a single-quoted literal."""
        tmdl = (
            "table Orders\n"
            "\tpartition Orders = m\n"
            "\t\tsource =\n"
            "\t\t\t\tlet\n"
            "\t\t\t\t    R = Table.SelectColumns(S, {\"Customer's Name\", \"Rep's Code\"})\n"
            "\t\t\t\tin\n"
            "\t\t\t\t    R\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp, tmdl_content=tmdl)
            score, _, coaching = _review_m_query_validity(pbip, {})
        self.assertEqual(score, 5)
        self.assertEqual(coaching, [])


class TestCompletenessCountsRenderableWorksheets(unittest.TestCase):
    """Worksheets absent from every dashboard map to no PBI visual."""

    def test_worksheets_off_dashboard_are_not_expected(self):
        extraction = {
            'worksheets': [{'name': f'WS{i}'} for i in range(7)],
            'dashboards': [{'name': 'D1', 'objects': [
                {'type': 'worksheetReference', 'worksheetName': 'WS0'},
                {'type': 'worksheetReference', 'worksheetName': 'WS1'},
            ]}],
        }
        pages = {'p1': [{'$schema': 's', 'name': 'v1'}, {'$schema': 's', 'name': 'v2'}]}
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp, pages=pages)
            _, _, coaching = _review_completeness(pbip, extraction)
        self.assertFalse([c for c in coaching if 'worksheets missing visuals' in c.issue])

    def test_missing_visual_for_placed_worksheet_is_still_reported(self):
        extraction = {
            'worksheets': [{'name': 'WS0'}, {'name': 'WS1'}],
            'dashboards': [{'name': 'D1', 'objects': [
                {'type': 'worksheetReference', 'worksheetName': 'WS0'},
                {'type': 'worksheetReference', 'worksheetName': 'WS1'},
            ]}],
        }
        pages = {'p1': [{'$schema': 's', 'name': 'v1'}]}
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp, pages=pages)
            _, _, coaching = _review_completeness(pbip, extraction)
        self.assertTrue(any('worksheets missing visuals' in c.issue for c in coaching))


class TestSemanticModelOnlyProject(unittest.TestCase):
    """A datasource-only workbook has no report surface to lose."""

    def test_no_report_expected_without_worksheets_or_dashboards(self):
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp)
            # Remove the whole Report item, as the generator does.
            import shutil
            shutil.rmtree(Path(pbip) / 'TestReport.Report')
            score, detail, coaching = _review_pbir_fidelity(
                pbip, {'worksheets': [], 'dashboards': []})
        self.assertEqual(score, 5, detail)
        self.assertEqual(coaching, [])

    def test_missing_report_is_reported_when_source_has_worksheets(self):
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp)
            import shutil
            shutil.rmtree(Path(pbip) / 'TestReport.Report')
            score, _, coaching = _review_pbir_fidelity(
                pbip, {'worksheets': [{'name': 'WS0'}]})
        self.assertLess(score, 5)
        self.assertTrue(any('report.json' in c.issue for c in coaching))


if __name__ == '__main__':
    unittest.main()
