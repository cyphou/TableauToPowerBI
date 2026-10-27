"""
Tests for powerbi_import.m_query_generator — sample data M query generation.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from powerbi_import.m_query_generator import (
    export_final_power_query_files,
    extract_m_partitions_from_tmdl,
    generate_csv_query,
    generate_excel_query,
    generate_json_query,
    generate_m_query_from_datasource,
    generate_sample_data_query,
    generate_sql_query,
    generate_web_query,
    get_column_type_mapping,
    sync_final_calendar_source_expressions,
)


class TestFinalPowerQueryExport(unittest.TestCase):
    """Exports are literal final partitions, never reconstructed M queries."""

    EXPRESSION = (
        'let\n'
        '    // a source comment containing let / in and \\\\ escapes\n'
        '    Source = let\n'
        '        Source = Csv.Document(File.Contents(DataFolder & "\\\\facts.csv"), '
        '[Delimiter=";", Encoding=65001, QuoteStyle=QuoteStyle.Csv])\n'
        '    in Source,\n'
        '    Aliases = Table.DuplicateColumn(Source, "Label", "Label (other.csv)"),\n'
        '    Result = Table.SelectColumns(Aliases, {"Label", "Label (other.csv)"}, MissingField.UseNull)\n'
        'in\n'
        '    Result'
    )

    def partition(self, name, expression, fenced=False):
        source = "\t\tsource = ```\n" if fenced else "\t\tsource =\n"
        body = "\n".join("\t\t\t\t" + line for line in expression.splitlines())
        return (f"\tpartition '{name}' = m\n\t\tmode: import\n" + source
                + body + ("\n\t\t```" if fenced else "") + "\n")

    def test_nested_fenced_and_unfenced_partitions_preserve_literal_m(self):
        for fenced in (False, True):
            with self.subTest(fenced=fenced):
                content = ("table Facts\n"
                           + self.partition("Owner''s source", self.EXPRESSION, fenced)
                           + "\tannotation PBI_ResultType = Table\n")
                self.assertEqual(extract_m_partitions_from_tmdl(content),
                                 [("Owner's source", self.EXPRESSION)])

    def test_multiple_inline_partitions_ignore_non_m_and_stop_at_metadata(self):
        content = (
            "table Facts\n"
            "\tpartition Calculated = calculated\n\t\tsource = ROW(\"x\", 1)\n"
            "\tpartition First = m\n\t\tsource = #table({\"x\"}, {{1}})\n"
            "\tannotation End = true\n"
            + self.partition("Second", self.EXPRESSION))
        self.assertEqual(extract_m_partitions_from_tmdl(content), [
            ("First", '#table({"x"}, {{1}})'), ("Second", self.EXPRESSION)])

    def test_exports_all_partitions_without_colliding_with_real_table_names(self):
        with tempfile.TemporaryDirectory() as root:
            definition = Path(root, "Synthetic.SemanticModel", "definition")
            tables = definition / "tables"
            tables.mkdir(parents=True)
            (tables / "Facts.tmdl").write_text(
                "table Facts\n" + self.partition("One", self.EXPRESSION)
                + self.partition("Two", "let x = 2 in x"), encoding="utf-8-sig")
            (tables / "Facts__partition_2.tmdl").write_text(
                "table Facts__partition_2\n" + self.partition("One", "let x = 3 in x"),
                encoding="utf-8")
            shared = 'expression DataFolder = "C:\\\\synthetic"\n'
            (definition / "expressions.tmdl").write_text(shared, encoding="utf-8-sig")
            self.assertEqual(export_final_power_query_files(root, "Synthetic"), 4)
            exports = {p.name: p.read_text(encoding="utf-8")
                       for p in Path(root, "PowerQuery").glob("*.pq")}
            self.assertEqual(exports, {
                "Facts.pq": self.EXPRESSION, "Facts__partition_2.pq": "let x = 3 in x",
                "Facts__partition_3.pq": "let x = 2 in x",
                "_expressions.pq": shared.strip()})
            # The second invocation must read the new on-disk final partition.
            corrected = self.EXPRESSION.replace('Delimiter=";"', 'Delimiter="|"')
            (tables / "Facts.tmdl").write_text(
                "table Facts\n" + self.partition("One", corrected)
                + self.partition("Two", "let x = 2 in x"), encoding="utf-8")
            self.assertEqual(export_final_power_query_files(root, "Synthetic"), 4)
            self.assertEqual(Path(root, "PowerQuery", "Facts.pq").read_text(encoding="utf-8"),
                             corrected)

    def test_read_failure_propagates_without_partially_overwriting_exports(self):
        with tempfile.TemporaryDirectory() as root:
            tables = Path(root, "Synthetic.SemanticModel", "definition", "tables")
            tables.mkdir(parents=True)
            for name in ("A", "B"):
                (tables / f"{name}.tmdl").write_text(
                    f"table {name}\n" + self.partition("One", self.EXPRESSION),
                    encoding="utf-8")
            export_final_power_query_files(root, "Synthetic")
            destination = Path(root, "PowerQuery", "A.pq")
            destination.write_text("previous successful export", encoding="utf-8")
            read_text = Path.read_text

            def fail_second(path, *args, **kwargs):
                if path.name == "B.tmdl":
                    raise OSError("synthetic read failure")
                return read_text(path, *args, **kwargs)

            with patch.object(Path, "read_text", fail_second):
                with self.assertRaisesRegex(OSError, "synthetic read failure"):
                    export_final_power_query_files(root, "Synthetic")
            self.assertEqual(destination.read_text(encoding="utf-8"), "previous successful export")

    def test_missing_model_is_a_noop(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertEqual(export_final_power_query_files(root, "Synthetic"), 0)
            self.assertFalse(Path(root, "PowerQuery").exists())

    def test_calendar_sync_uses_exact_json_provenance_not_expression_or_filename(self):
        import json
        with tempfile.TemporaryDirectory() as root:
            definition = Path(root, "Synthetic.SemanticModel", "definition")
            tables = definition / "tables"
            tables.mkdir(parents=True)
            name = "Owner's.fact é"
            (tables / "shortened_hash.tmdl").write_text(
                "table 'Owner''s.fact é'\n" + self.partition("One", self.EXPRESSION),
                encoding="utf-8")
            unrelated = 'expression \'Calendar Source decoy\' =\r\n\t"unchanged"\r\n'
            annotation = "\tannotation Migration_CalendarSourceTable = " + json.dumps(name, ensure_ascii=False)
            shared = definition / "expressions.tmdl"
            original = (unrelated + "\r\nexpression 'Private unrelated name' =\r\n"
                        "\tlet old = 1 in old\r\n" + annotation
                        + "\r\n\tannotation Keep = preserved\r\n")
            shared.write_bytes(b"\xef\xbb\xbf" + original.encode("utf-8"))
            self.assertEqual(sync_final_calendar_source_expressions(root, "Synthetic"), 1)
            updated = shared.read_bytes()
            self.assertTrue(updated.startswith(b"\xef\xbb\xbf"))
            text = updated.decode("utf-8-sig")
            self.assertIn(unrelated, text)
            self.assertIn(annotation, text)
            self.assertIn("\tannotation Keep = preserved\r\n", text)
            self.assertIn("\t\t" + self.EXPRESSION.replace("\n", "\r\n\t\t"), text)
            from tests.test_tmdl_dates import _read_shared_expressions
            self.assertEqual(
                _read_shared_expressions(text)["Private unrelated name"], self.EXPRESSION)
            self.assertNotIn("let old = 1", text)
            self.assertEqual(sync_final_calendar_source_expressions(root, "Synthetic"), 0)
            self.assertEqual(shared.read_bytes(), updated)

    def test_invalid_or_ambiguous_calendar_provenance_fails_before_any_write(self):
        for annotation, duplicate_table in (
            ('"Missing"', False), ('{"not": "a string"}', False),
            ('"Facts"', True), ('not-json', False),
        ):
            with self.subTest(annotation=annotation, duplicate_table=duplicate_table):
                with tempfile.TemporaryDirectory() as root:
                    definition = Path(root, "Synthetic.SemanticModel", "definition")
                    tables = definition / "tables"
                    tables.mkdir(parents=True)
                    body = "table Facts\n" + self.partition("One", self.EXPRESSION)
                    (tables / "Facts.tmdl").write_text(body, encoding="utf-8")
                    if duplicate_table:
                        (tables / "Duplicate.tmdl").write_text(body, encoding="utf-8")
                    shared = definition / "expressions.tmdl"
                    original = (
                        'expression Good =\n\tlet x = 0 in x\n'
                        '\tannotation Migration_CalendarSourceTable = "Facts"\n'
                        'expression Invalid =\n\tlet x = 0 in x\n'
                        '\tannotation Migration_CalendarSourceTable = ' + annotation + "\n")
                    shared.write_text(original, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        sync_final_calendar_source_expressions(root, "Synthetic")
                    self.assertEqual(shared.read_text(encoding="utf-8"), original)


class TestGenerateSqlQuery(unittest.TestCase):
    """Tests for SQL connector M query generation."""

    def test_sql_query_contains_server_and_database(self):
        conn = {
            'connectionDetails': {'server': 'srv.db.windows.net', 'database': 'SalesDB'},
        }
        result = generate_sql_query(conn, 'Orders')
        self.assertIn('srv.db.windows.net', result)
        self.assertIn('SalesDB', result)
        self.assertIn('Sql.Database', result)

    def test_sql_query_uses_table_name(self):
        conn = {'connectionDetails': {'server': 's', 'database': 'd'}}
        result = generate_sql_query(conn, 'MyTable')
        self.assertIn('MyTable', result)

    def test_sql_query_defaults_when_missing_details(self):
        conn = {}
        result = generate_sql_query(conn, 'T')
        self.assertIn('YourServer', result)
        self.assertIn('YourDatabase', result)

    def test_sql_query_is_valid_let_in(self):
        conn = {'connectionDetails': {'server': 's', 'database': 'd'}}
        result = generate_sql_query(conn, 'T')
        self.assertTrue(result.strip().startswith('let'))
        self.assertTrue(result.strip().endswith('Result'))


class TestGenerateExcelQuery(unittest.TestCase):
    """Tests for Excel connector M query generation."""

    def test_excel_query_contains_workbook_call(self):
        conn = {}
        result = generate_excel_query(conn, 'Sheet1')
        self.assertIn('Excel.Workbook', result)

    def test_excel_query_uses_sheet_name(self):
        result = generate_excel_query({}, 'SalesData')
        self.assertIn('SalesData', result)

    def test_excel_query_promotes_headers(self):
        result = generate_excel_query({}, 'S')
        self.assertIn('PromoteHeaders', result)


class TestGenerateCsvQuery(unittest.TestCase):
    """Tests for CSV connector M query generation."""

    def test_csv_query_contains_csv_document(self):
        result = generate_csv_query({}, 'data')
        self.assertIn('Csv.Document', result)

    def test_csv_query_includes_table_name_in_path(self):
        result = generate_csv_query({}, 'employees')
        self.assertIn('employees.csv', result)

    def test_csv_query_specifies_utf8(self):
        result = generate_csv_query({}, 'x')
        self.assertIn('65001', result)


class TestGenerateWebQuery(unittest.TestCase):
    """Tests for Web connector M query generation."""

    def test_web_query_uses_web_contents(self):
        result = generate_web_query({}, 'api_data')
        self.assertIn('Web.Contents', result)

    def test_web_query_parses_json(self):
        result = generate_web_query({}, 'api_data')
        self.assertIn('Json.Document', result)


class TestGenerateJsonQuery(unittest.TestCase):
    """Tests for JSON file M query generation."""

    def test_json_query_reads_file(self):
        result = generate_json_query({}, 'config')
        self.assertIn('Json.Document', result)
        self.assertIn('File.Contents', result)

    def test_json_query_uses_table_name_in_path(self):
        result = generate_json_query({}, 'products')
        self.assertIn('products.json', result)

    def test_json_query_handles_list_or_record(self):
        result = generate_json_query({}, 'x')
        self.assertIn('Value.Is', result)
        self.assertIn('Table.FromRecords', result)


class TestGenerateSampleDataQuery(unittest.TestCase):
    """Tests for fallback sample data M query generation."""

    def test_sample_query_with_known_columns(self):
        ds = {
            'tables': [
                {
                    'name': 'Orders',
                    'columns': [
                        {'name': 'OrderID'},
                        {'name': 'Amount'},
                        {'name': 'OrderDate'},
                    ],
                }
            ]
        }
        result = generate_sample_data_query('Orders', ds)
        self.assertIn('"OrderID"', result)
        self.assertIn('"Amount"', result)
        self.assertIn('"OrderDate"', result)
        self.assertIn('#table', result)

    def test_sample_query_uses_default_columns(self):
        result = generate_sample_data_query('UnknownTable', {'tables': []})
        self.assertIn('"ID"', result)
        self.assertIn('"Name"', result)
        self.assertIn('"Value"', result)

    def test_sample_query_generates_5_rows(self):
        ds = {'tables': [{'name': 'T', 'columns': [{'name': 'x'}]}]}
        result = generate_sample_data_query('T', ds)
        # Each row starts with { — count data rows
        lines = [line.strip() for line in result.split('\n') if line.strip().startswith('{') and ',' in line]
        self.assertGreaterEqual(len(lines), 5)

    def test_sample_query_id_column_generates_numbers(self):
        ds = {'tables': [{'name': 'T', 'columns': [{'name': 'CustomerID'}]}]}
        result = generate_sample_data_query('T', ds)
        # Should have numeric values 1-5
        self.assertIn('{1}', result)
        self.assertIn('{5}', result)

    def test_sample_query_date_column_generates_dates(self):
        ds = {'tables': [{'name': 'T', 'columns': [{'name': 'sale_date'}]}]}
        result = generate_sample_data_query('T', ds)
        self.assertIn('#date(2024', result)

    def test_sample_query_includes_todo_comment(self):
        result = generate_sample_data_query('T', {'tables': []})
        self.assertIn('TODO', result)


class TestGenerateMQueryFromDatasource(unittest.TestCase):
    """Tests for the router function that dispatches to type-specific generators."""

    def _ds(self, conn_type):
        return {
            'dataSource': {
                'connectionType': conn_type,
                'connectionDetails': {'server': 's', 'database': 'd'},
            },
            'tables': [],
        }

    def test_sql_routing(self):
        result = generate_m_query_from_datasource(self._ds('SQL Server'), 'T')
        self.assertIn('Sql.Database', result)

    def test_postgres_routing(self):
        result = generate_m_query_from_datasource(self._ds('PostgreSQL'), 'T')
        self.assertIn('Sql.Database', result)

    def test_mysql_routing(self):
        result = generate_m_query_from_datasource(self._ds('MySQL'), 'T')
        self.assertIn('Sql.Database', result)

    def test_oracle_routing(self):
        result = generate_m_query_from_datasource(self._ds('Oracle'), 'T')
        self.assertIn('Sql.Database', result)

    def test_excel_routing(self):
        result = generate_m_query_from_datasource(self._ds('Excel'), 'T')
        self.assertIn('Excel.Workbook', result)

    def test_csv_routing(self):
        result = generate_m_query_from_datasource(self._ds('CSV'), 'T')
        self.assertIn('Csv.Document', result)

    def test_text_routing(self):
        result = generate_m_query_from_datasource(self._ds('Text File'), 'T')
        self.assertIn('Csv.Document', result)

    def test_web_routing(self):
        result = generate_m_query_from_datasource(self._ds('Web'), 'T')
        self.assertIn('Web.Contents', result)

    def test_json_routing(self):
        result = generate_m_query_from_datasource(self._ds('JSON'), 'T')
        self.assertIn('Json.Document', result)

    def test_unknown_falls_back_to_sample(self):
        result = generate_m_query_from_datasource(self._ds('UnknownDB'), 'T')
        self.assertIn('#table', result)

    def test_empty_datasource(self):
        result = generate_m_query_from_datasource({}, 'T')
        self.assertIn('#table', result)


class TestColumnTypeMapping(unittest.TestCase):
    """Tests for Tableau → M type mapping dictionary."""

    def test_mapping_has_all_expected_types(self):
        mapping = get_column_type_mapping()
        expected_keys = {'string', 'integer', 'real', 'decimal', 'boolean', 'date', 'datetime', 'time', 'spatial'}
        self.assertEqual(set(mapping.keys()), expected_keys)

    def test_string_maps_to_text(self):
        self.assertEqual(get_column_type_mapping()['string'], 'type text')

    def test_integer_maps_to_int64(self):
        self.assertEqual(get_column_type_mapping()['integer'], 'Int64.Type')

    def test_boolean_maps_to_logical(self):
        self.assertEqual(get_column_type_mapping()['boolean'], 'type logical')

    def test_date_maps_to_date(self):
        self.assertEqual(get_column_type_mapping()['date'], 'type date')

    def test_spatial_maps_to_text(self):
        self.assertEqual(get_column_type_mapping()['spatial'], 'type text')


if __name__ == '__main__':
    unittest.main()
