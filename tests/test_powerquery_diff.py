"""Tests for powerbi_import/powerquery_diff.py.

Verifies the Tableau-vs-PowerBI table/column data-fidelity comparison.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.powerquery_diff import (
    compare_caption_metadata,
        compare_calculation_metadata,
    compare_report_counts,
    _find_generated_match,
    _normalize_table_name,
    compare_report_metadata,
    compare_report_tables,
    compare_semantic_metadata,
)


def _write_tmdl_table(semantic_model_dir, filename, table_name, columns):
    tables_dir = os.path.join(semantic_model_dir, 'definition', 'tables')
    os.makedirs(tables_dir, exist_ok=True)
    lines = [f"table {table_name}"]
    for col in columns:
        lines.append(f"\tcolumn '{col}'")
        lines.append("\t\tdataType: string")
    lines.append("\tpartition 'p' = m")
    lines.append("\t\tmode: import")
    lines.append("\t\tsource = ```")
    lines.append("\t\t\tlet Source = \"x\" in Source")
    lines.append("\t\t```")
    with open(os.path.join(tables_dir, filename), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


class TestNormalizeTableName(unittest.TestCase):
    def test_strips_brackets_and_lowercases(self):
        self.assertEqual(_normalize_table_name('[Orders]'), 'orders')

    def test_strips_qualified_bracket_path(self):
        self.assertEqual(_normalize_table_name('[federated.abc123].[Orders]'), 'orders')


class TestFindGeneratedMatch(unittest.TestCase):
    def test_exact_match(self):
        generated = {'orders': {'name': 'Orders', 'columns': []}}
        match = _find_generated_match('orders', generated)
        self.assertIsNotNone(match)
        self.assertEqual(match['name'], 'Orders')

    def test_no_match_returns_none(self):
        generated = {'customers': {'name': 'Customers', 'columns': []}}
        self.assertIsNone(_find_generated_match('orders', generated))


class TestCompareReportTables(unittest.TestCase):
    def test_counts_keep_generated_model_objects_separate_from_source(self):
        extracted = {'datasources': [{'name': 'ds', 'tables': [
            {'name': 'Sales', 'columns': [{'name': 'Amount'}, {'name': 'Date'}]}]}],
            'calculations': [{'name': '[Revenue]', 'role': 'measure',
                              'datatype': 'real'}]}
        with tempfile.TemporaryDirectory() as project:
            tables_dir = os.path.join(project, 'Report.SemanticModel', 'definition', 'tables')
            os.makedirs(tables_dir)
            with open(os.path.join(tables_dir, 'Sales.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write('table Sales\n\tcolumn Amount\n\t\tsourceColumn: Amount\n'
                             '\tcolumn Date\n\t\tsourceColumn: Date\n'
                             "\tcolumn Flag = TRUE()\n\tmeasure Revenue = SUM([Amount])\n"
                             '\tmeasure Automatic = COUNTROWS(Sales)\n')
            with open(os.path.join(tables_dir, 'Calendar.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write('table Calendar\n\tcolumn Date\n\t\tsourceColumn: Date\n')
            result = compare_report_counts(extracted, project, 'Report')
        self.assertEqual(result['tableau'], {
            'tables': 1, 'physical_columns': 2, 'measure_calculations': 1})
        self.assertEqual(result['powerbi'], {
            'tables': 2, 'physical_columns': 3, 'calculated_columns': 1, 'measures': 2})
        self.assertEqual(result['source_to_target']['physical_columns_found'], 2)
        self.assertEqual(result['source_to_target']['measure_calculations_as_measures'], 1)

    def test_coverage_requires_both_case_distinct_physical_columns(self):
        extracted = {'datasources': [{'tables': [{'name': 'Data', 'columns': [
            {'name': 'Siren'}, {'name': 'siren'}]}]}]}
        with tempfile.TemporaryDirectory() as project:
            _write_tmdl_table(os.path.join(project, 'Report.SemanticModel'),
                              'Data.tmdl', 'Data', ['Siren'])
            result = compare_report_tables(extracted, project, 'Report')
        self.assertEqual(result['tables'][0]['missing_columns'], ['siren'])
        self.assertEqual(result['tables'][0]['column_coverage_percent'], 50.0)

    def test_coverage_accepts_display_name_when_partition_renames_source(self):
        extracted = {'datasources': [{'tables': [{'name': 'Data', 'columns': [
            {'name': 'commune'}]}]}]}
        with tempfile.TemporaryDirectory() as project:
            tables_dir = os.path.join(project, 'Report.SemanticModel', 'definition', 'tables')
            os.makedirs(tables_dir)
            with open(os.path.join(tables_dir, 'Data.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write('table Data\n\tcolumn commune\n'
                             '\t\tsourceColumn: Commune\n')
            result = compare_report_tables(extracted, project, 'Report')
        self.assertEqual(result['tables'][0]['column_coverage_percent'], 100.0)

    def test_duplicate_table_names_are_scoped_to_each_datasource(self):
        extracted = {'datasources': [
            {'name': 'First', 'tables': [{'name': 'Extract', 'columns': [{'name': 'A'}]}]},
            {'name': 'Second', 'tables': [{'name': 'Extract', 'columns': [{'name': 'B'}]}]},
        ]}
        with tempfile.TemporaryDirectory() as project:
            model_dir = os.path.join(project, 'Report.SemanticModel')
            _write_tmdl_table(model_dir, 'Extract.tmdl', 'Extract', ['A'])
            _write_tmdl_table(model_dir, 'Extract (Second).tmdl',
                              "'Extract (Second)'", ['Wrong'])
            result = compare_report_tables(extracted, project, 'Report')
        self.assertEqual(result['summary']['source_tables'], 2)
        self.assertEqual(result['tables'][0]['missing_columns'], [])
        self.assertEqual(result['tables'][1]['missing_columns'], ['B'])

    def test_explicit_semantic_role_and_hidden_flag_are_compared(self):
        extracted = {'datasources': [{'name': 'ds', 'columns': [
            {'name': '[Latitude]', 'semantic_role': '[Latitude]',
             'datatype': 'real', 'hidden': True},
            {'name': '[:Measure Names]', 'hidden': True},
        ], 'tables': [{'name': 'Sites', 'columns': [
            {'name': 'Latitude', 'datatype': 'real'}]}]}]}
        with tempfile.TemporaryDirectory() as project:
            tables_dir = os.path.join(project, 'Report.SemanticModel', 'definition', 'tables')
            os.makedirs(tables_dir)
            with open(os.path.join(tables_dir, 'Sites.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write('table Sites\n\tcolumn Latitude\n\t\tdataType: double\n'
                             '\t\tdataCategory: Latitude\n\t\tisHidden\n')
            result = compare_semantic_metadata(extracted, project, 'Report')
        self.assertEqual(result['summary']['category_match'], 1)
        self.assertEqual(result['summary']['hidden_match'], 1)
        self.assertEqual(len(result['columns']), 1)

    def test_hidden_field_does_not_match_homonym_in_unrelated_table(self):
        extracted = {'datasources': [{'name': 'ds', 'columns': [
            {'name': '[Invoice Amount]', 'hidden': True}],
            'tables': [{'name': 'Billing', 'columns': [{'name': 'Invoice Amount'}]}]}]}
        with tempfile.TemporaryDirectory() as project:
            tables_dir = os.path.join(project, 'Report.SemanticModel', 'definition', 'tables')
            os.makedirs(tables_dir)
            with open(os.path.join(tables_dir, 'Billing.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write("table Billing\n\tcolumn 'Invoice Amount'\n\t\tisHidden\n")
            with open(os.path.join(tables_dir, 'Other.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write("table Other\n\tcolumn 'Invoice Amount'\n")
            result = compare_semantic_metadata(extracted, project, 'Report')
        self.assertEqual(result['summary']['hidden_match'], 1)
        self.assertEqual(result['summary']['hidden_mismatch'], 0)

    def test_hidden_physical_source_ignores_visible_calculated_case_homonym(self):
        extracted = {'datasources': [{'name': 'ds', 'columns': [
            {'name': '[Year Origin]', 'hidden': True}],
            'tables': [{'name': 'Invoices', 'columns': [{'name': 'Year Origin'}]}]}]}
        with tempfile.TemporaryDirectory() as project:
            tables_dir = os.path.join(project, 'Report.SemanticModel', 'definition', 'tables')
            os.makedirs(tables_dir)
            with open(os.path.join(tables_dir, 'Invoices.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write("table Invoices\n\tcolumn 'Year Origin (source)'\n"
                             "\t\tsourceColumn: Year Origin\n\t\tisHidden\n"
                             "\tcolumn 'Year origin' = UPPER([Other])\n")
            result = compare_semantic_metadata(extracted, project, 'Report')
        self.assertEqual(result['summary']['hidden_match'], 1)
        self.assertEqual(result['summary']['hidden_mismatch'], 0)
    def test_explicit_source_caption_not_present_in_model_is_reported(self):
        extracted = {'datasources': [{'name': 'ds', 'columns': [
            {'name': '[raw_code]', 'caption': 'Postal Code'},
            {'name': '[Calculation_1]', 'caption': 'Calculated Label',
             'calculation': {'formula': '[raw_code]'}},
            {'name': '[:Measure Names]', 'caption': 'Measure Names'}]}]}
        with tempfile.TemporaryDirectory() as project:
            tables_dir = os.path.join(project, 'Report.SemanticModel', 'definition', 'tables')
            os.makedirs(tables_dir)
            with open(os.path.join(tables_dir, 'T.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write('table T\n\tcolumn raw_code\n\t\tdataType: string\n')
            result = compare_caption_metadata(extracted, project, 'Report')
        self.assertEqual(result['summary']['not_found_in_model'], 1)
        self.assertEqual(len(result['captions']), 1)

    def test_caption_homonym_in_another_table_does_not_cover_source(self):
        extracted = {'datasources': [{'name': 'ds', 'columns': [
            {'name': '[ville]', 'caption': 'État'}], 'tables': [
            {'name': 'Places', 'columns': [{'name': 'ville'}]}]}]}
        with tempfile.TemporaryDirectory() as project:
            tables_dir = os.path.join(project, 'Report.SemanticModel', 'definition', 'tables')
            os.makedirs(tables_dir)
            with open(os.path.join(tables_dir, 'Places.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write('table Places\n\tcolumn ville\n\t\tsourceColumn: ville\n')
            with open(os.path.join(tables_dir, 'Other.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write("table Other\n\tcolumn 'État'\n\t\tsourceColumn: ville\n")
            result = compare_caption_metadata(extracted, project, 'Report')
        self.assertEqual(result['summary']['not_found_in_model'], 1)

    def test_caption_keeps_accents_even_when_case_changes(self):
        extracted = {'datasources': [{'name': 'ds', 'columns': [
            {'name': '[region]', 'caption': 'Région'},
            {'name': '[pays]', 'caption': 'État'}], 'tables': [
            {'name': 'Places', 'columns': [{'name': 'region'}, {'name': 'pays'}]}]}]}
        with tempfile.TemporaryDirectory() as project:
            tables_dir = os.path.join(project, 'Report.SemanticModel', 'definition', 'tables')
            os.makedirs(tables_dir)
            with open(os.path.join(tables_dir, 'Places.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write("table Places\n\tcolumn 'région'\n"
                             "\t\tsourceColumn: region\n\tcolumn Etat\n"
                             "\t\tsourceColumn: pays\n")
            result = compare_caption_metadata(extracted, project, 'Report')
        self.assertEqual(result['summary']['retained_in_model'], 1)
        self.assertEqual(result['summary']['not_found_in_model'], 1)

    def test_calculation_declaration_does_not_claim_expression_parity(self):
        extracted = {'calculations': [{'name': '[Calculation_1]',
                                       'caption': 'Weighted Total',
                                       'datatype': 'real', 'role': 'measure',
                                       'formula': 'SUM([Amount])'}]}
        with tempfile.TemporaryDirectory() as project:
            tables_dir = os.path.join(project, 'Report.SemanticModel', 'definition', 'tables')
            os.makedirs(tables_dir)
            with open(os.path.join(tables_dir, 'Sales.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write("table Sales\n\tcolumn 'Weighted Total'\n"
                             "\t\tdataType: double\n")
            result = compare_calculation_metadata(extracted, project, 'Report')
        self.assertEqual(result['summary']['column_declared'], 1)
        self.assertEqual(result['calculations'][0]['expression_equivalence'], 'not_verified')

    def test_calculations_differing_only_by_case_match_exact_symbols(self):
        extracted = {'calculations': [
            {'name': '[Calculation_1]', 'caption': 'point_france', 'role': 'measure'},
            {'name': '[Calculation_2]', 'caption': 'Point_France', 'role': 'measure'}]}
        with tempfile.TemporaryDirectory() as project:
            tables_dir = os.path.join(project, 'Report.SemanticModel', 'definition', 'tables')
            os.makedirs(tables_dir)
            with open(os.path.join(tables_dir, 'Places.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write('table Places\n\tcolumn point_france = TRUE()\n')
            with open(os.path.join(tables_dir, 'Facts.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write('table Facts\n\tcolumn Point_France = TRUE()\n')
            result = compare_calculation_metadata(extracted, project, 'Report')
        self.assertEqual(result['summary']['column_declared'], 2)
        self.assertEqual(result['summary']['ambiguous_symbol'], 0)

    def test_metadata_detects_wrong_type_even_with_full_column_coverage(self):
        extracted = {'datasources': [{'tables': [{'name': 'Orders', 'columns': [
            {'name': 'raw_total', 'datatype': 'real'}]}]}]}
        with tempfile.TemporaryDirectory() as project:
            table_dir = os.path.join(project, 'Report.SemanticModel', 'definition', 'tables')
            os.makedirs(table_dir)
            with open(os.path.join(table_dir, 'Orders.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write('table Orders\n\tcolumn Total\n\t\tdataType: string\n'
                             '\t\tsourceColumn: raw_total\n')
            result = compare_report_metadata(extracted, project, 'Report')
        self.assertEqual(result['summary']['type_mismatch'], 1)
        self.assertEqual(result['columns'][0]['target_datatype'], 'string')

    def test_metadata_matches_case_distinct_physical_source_columns_exactly(self):
        extracted = {'datasources': [{'tables': [{'name': 'Data', 'columns': [
            {'name': 'Siren', 'datatype': 'string'},
            {'name': 'siren', 'datatype': 'integer'}]}]}]}
        with tempfile.TemporaryDirectory() as project:
            tables_dir = os.path.join(project, 'Report.SemanticModel', 'definition', 'tables')
            os.makedirs(tables_dir)
            with open(os.path.join(tables_dir, 'Data.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write('table Data\n\tcolumn Siren\n\t\tdataType: string\n'
                             '\t\tsourceColumn: Siren\n\tcolumn \'siren (source)\'\n'
                             '\t\tdataType: int64\n\t\tsourceColumn: siren\n')
            result = compare_report_metadata(extracted, project, 'Report')
        self.assertEqual(result['summary']['type_match'], 2)
        self.assertEqual(result['summary']['ambiguous_column'], 0)

    def test_metadata_cannot_borrow_case_distinct_source_column(self):
        extracted = {'datasources': [{'tables': [{'name': 'Data', 'columns': [
            {'name': 'TYPEMD', 'datatype': 'integer'},
            {'name': 'Typemd', 'datatype': 'integer'}]}]}]}
        with tempfile.TemporaryDirectory() as project:
            tables_dir = os.path.join(project, 'Report.SemanticModel', 'definition', 'tables')
            os.makedirs(tables_dir)
            with open(os.path.join(tables_dir, 'Data.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write('table Data\n\tcolumn TYPEMD\n'
                             '\t\tdataType: int64\n\t\tsourceColumn: TYPEMD\n')
            result = compare_report_metadata(extracted, project, 'Report')
        self.assertEqual(result['summary']['type_match'], 1)
        self.assertEqual(result['summary']['unmatched_column'], 1)

    def test_metadata_refuses_fuzzy_table_and_unknown_type(self):
        extracted = {'datasources': [{'tables': [{'name': 'Order', 'columns': [
            {'name': 'Amount', 'datatype': 'real'}]}]}]}
        with tempfile.TemporaryDirectory() as project:
            table_dir = os.path.join(project, 'Report.SemanticModel', 'definition', 'tables')
            os.makedirs(table_dir)
            with open(os.path.join(table_dir, 'Orders.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write('table Orders\n\tcolumn Amount\n\t\tdataType: double\n')
            result = compare_report_metadata(extracted, project, 'Report')
        self.assertEqual(result['summary']['unverified_table'], 1)

    def test_metadata_accepts_unique_exact_caption_after_source_column_rename(self):
        extracted = {'datasources': [{'tables': [{'name': 'Places', 'columns': [
            {'name': 'Geometry (Shapes)', 'datatype': 'spatial'}]}]}]}
        with tempfile.TemporaryDirectory() as project:
            tables_dir = os.path.join(project, 'Report.SemanticModel', 'definition', 'tables')
            os.makedirs(tables_dir)
            with open(os.path.join(tables_dir, 'Places.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write("table Places\n\tcolumn 'Geometry (Shapes)'\n"
                             "\t\tdataType: string\n\t\tsourceColumn: Géométrie\n")
            result = compare_report_metadata(extracted, project, 'Report')
        self.assertEqual(result['summary']['unmatched_column'], 0)
        self.assertEqual(result['summary']['datatype_not_checked'], 1)

    def test_numeric_relationship_key_converted_to_text_needs_review(self):
        extracted = {'datasources': [{'tables': [{'name': 'Facts', 'columns': [
            {'name': 'RegionKey', 'datatype': 'real'}]}]}]}
        with tempfile.TemporaryDirectory() as project:
            definition = os.path.join(project, 'Report.SemanticModel', 'definition')
            os.makedirs(os.path.join(definition, 'tables'))
            with open(os.path.join(definition, 'tables', 'Facts.tmdl'),
                      'w', encoding='utf-8') as stream:
                stream.write('table Facts\n\tcolumn RegionKey\n\t\tdataType: string\n'
                             '\t\tsourceColumn: RegionKey\n')
            with open(os.path.join(definition, 'relationships.tmdl'),
                      'w', encoding='utf-8') as stream:
                stream.write('relationship r\n\tfromColumn: Facts.RegionKey\n'
                             '\ttoColumn: Places.RegionKey\n')
            result = compare_report_metadata(extracted, project, 'Report')
        self.assertEqual(result['summary']['relationship_key_type_review'], 1)
        self.assertEqual(result['summary']['type_match'], 0)

    def test_duplicate_table_names_follow_datasource_caption(self):
        extracted = {'datasources': [
            {'name': 'first', 'caption': 'First', 'tables': [
                {'name': 'Extract', 'columns': [{'name': 'Alpha', 'datatype': 'string'}]}]},
            {'name': 'second', 'caption': 'Second', 'tables': [
                {'name': 'Extract', 'columns': [{'name': 'Beta', 'datatype': 'real'}]}]},
        ]}
        with tempfile.TemporaryDirectory() as project:
            tables_dir = os.path.join(project, 'Report.SemanticModel', 'definition', 'tables')
            os.makedirs(tables_dir)
            with open(os.path.join(tables_dir, 'Extract.tmdl'), 'w', encoding='utf-8') as stream:
                stream.write('table Extract\n\tcolumn Alpha\n\t\tdataType: string\n')
            with open(os.path.join(tables_dir, 'Extract (Second).tmdl'), 'w', encoding='utf-8') as stream:
                stream.write("table 'Extract (Second)'\n\tcolumn Beta\n\t\tdataType: double\n")
            result = compare_report_metadata(extracted, project, 'Report')
        self.assertEqual(result['summary']['type_match'], 2)
        self.assertEqual(result['summary']['unmatched_column'], 0)

    def test_full_coverage(self):
        extracted = {
            'datasources': [{
                'name': 'ds1',
                'tables': [{
                    'name': 'Orders',
                    'columns': [{'name': 'OrderID'}, {'name': 'Amount'}],
                }],
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = os.path.join(tmp, 'Report')
            semantic_model_dir = os.path.join(project_dir, 'Report.SemanticModel')
            _write_tmdl_table(semantic_model_dir, 'Orders.tmdl', 'Orders',
                              ['OrderID', 'Amount'])
            result = compare_report_tables(extracted, project_dir, 'Report')

        self.assertEqual(result['summary']['source_tables'], 1)
        self.assertEqual(result['summary']['tables_found'], 1)
        self.assertEqual(result['summary']['avg_column_coverage_percent'], 100.0)

    def test_missing_table_flagged(self):
        extracted = {
            'datasources': [{
                'name': 'ds1',
                'tables': [{
                    'name': 'Orders',
                    'columns': [{'name': 'OrderID'}],
                }],
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = os.path.join(tmp, 'Report')
            semantic_model_dir = os.path.join(project_dir, 'Report.SemanticModel')
            _write_tmdl_table(semantic_model_dir, 'Customers.tmdl', 'Customers',
                              ['CustomerID'])
            result = compare_report_tables(extracted, project_dir, 'Report')

        self.assertEqual(result['summary']['tables_found'], 0)
        self.assertEqual(result['tables'][0]['table'], 'Orders')
        self.assertFalse(result['tables'][0]['found'])

    def test_partial_column_coverage(self):
        extracted = {
            'datasources': [{
                'name': 'ds1',
                'tables': [{
                    'name': 'Orders',
                    'columns': [{'name': 'OrderID'}, {'name': 'Amount'},
                               {'name': 'Discount'}],
                }],
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = os.path.join(tmp, 'Report')
            semantic_model_dir = os.path.join(project_dir, 'Report.SemanticModel')
            _write_tmdl_table(semantic_model_dir, 'Orders.tmdl', 'Orders',
                              ['OrderID', 'Amount'])
            result = compare_report_tables(extracted, project_dir, 'Report')

        entry = result['tables'][0]
        self.assertTrue(entry['found'])
        self.assertIn('Discount', entry['missing_columns'])
        self.assertAlmostEqual(entry['column_coverage_percent'], 200 / 3, places=1)


if __name__ == '__main__':
    unittest.main()
