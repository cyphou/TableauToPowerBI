"""Synthetic source-to-serialized-output Calendar lexical-scope regressions."""

import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from powerbi_import.tmdl_dates import (
    _add_date_table,
    _refresh_calendar_source_expressions,
    _runtime_calendar_dates,
    _worksheet_date_usage,
)
from powerbi_import.tmdl_writers import _write_tmdl_files


def _nested_source(filename):
    # Both facts intentionally reuse every binding, including an inner let.
    # Record option fields are NOT let bindings and must never be suffixed.
    return (
        'let\n'
        '    Source = try let\n'
        f'        Source = Csv.Document(File.Contents(DataFolder & "\\\\{filename}"), '
        '[Delimiter=";", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n'
        '        #"Promoted Headers" = Table.PromoteHeaders(Source, [PromoteAllScalars=true])\n'
        '    in #"Promoted Headers" otherwise #table({"Event Date"}, {}),\n'
        '    #"Changed Types" = Table.TransformColumnTypes(Source, {{"Event Date", type date}})\n'
        'in\n'
        '    #"Changed Types"'
    )


def _model():
    return {"model": {
        "name": "Synthetic", "culture": "en-US", "relationships": [],
        "expressions": [{"name": "DataFolder", "kind": "m", "expression": '"C:\\\\synthetic"'}],
        "tables": [{
            "name": name, "columns": [
                {"name": "Event Date", "dataType": "DateTime", "sourceColumn": "Event Date"}],
            "measures": [], "partitions": [{
                "name": name, "mode": "import", "source": {
                    "type": "m", "expression": _nested_source(name + ".csv")}}],
        } for name in ("First", "Second")],
    }}


def _read_shared_expressions(content):
    """Read TMDL continuations by indentation, never by annotation text.

    A multiline value establishes its continuation depth on its first line.
    Metadata must dedent from that depth; otherwise it is part of the M value,
    exactly the serialization error Desktop rejected at runtime.
    """
    expressions = {}
    for block in re.split(r"(?m)^expression ", content)[1:]:
        header, _, body = block.partition("\n")
        name, _, inline = header.partition(" =")
        lines = [inline.strip()]
        continuation_depth = None
        metadata_started = False
        for line in body.splitlines():
            if not line.strip():
                continue
            depth = len(line) - len(line.lstrip("\t"))
            if continuation_depth is None:
                continuation_depth = depth
                if depth < 1:
                    raise AssertionError("Shared M value must be indented")
            if depth < continuation_depth:
                metadata_started = True
                continue
            if metadata_started:
                raise AssertionError("M continuation resumed after metadata")
            value = line[continuation_depth:]
            if value.startswith("annotation "):
                raise AssertionError("TMDL annotation consumed as M continuation")
            lines.append(value)
        expressions[name.strip("'")] = "\n".join(
            line for line in lines if line and line != "```").strip()
    return expressions


class TestCalendarMonthStart(unittest.TestCase):
    def test_calendar_exposes_month_start_for_continuous_axes(self):
        model = _model()
        with redirect_stdout(io.StringIO()):
            _add_date_table(model)
        calendar = next(
            table for table in model['model']['tables']
            if table['name'] == 'Calendar')
        month_start = next(
            column for column in calendar['columns']
            if column['name'] == 'MonthStart')
        self.assertEqual(month_start['sourceColumn'], 'MonthStart')
        self.assertEqual(month_start['dataType'], 'DateTime')
        self.assertIn(
            'Date.StartOfMonth([Date])',
            calendar['partitions'][0]['source']['expression'])

    def test_calendar_bounds_read_physical_source_column_not_display_name(self):
        model = _model()
        model['model']['tables'][0]['columns'][0]['sourceColumn'] = ' Date\'s "physical" '
        with redirect_stdout(io.StringIO()):
            _add_date_table(model)
        calendar = next(t for t in model['model']['tables'] if t['name'] == 'Calendar')
        expression = calendar['partitions'][0]['source']['expression']
        self.assertIn(
            'Table.Column(#"Calendar Source First.Event Date", '
            '" Date\'s ""physical"" ")', expression)

    def test_runtime_calendar_dates_declines_real_semantic_table_reference(self):
        model = _model()
        model['model']['tables'][1]['name'] = 'Lookup'
        model['model']['tables'][0]['partitions'][0]['source']['expression'] = (
            'let Source = #"Lookup" in Source')

        result = _runtime_calendar_dates(model, [('First', 'Event Date')])

        self.assertEqual(result, '')
        self.assertFalse(any(
            expression.get('_calendar_source_table')
            for expression in model['model']['expressions']))

    def test_runtime_calendar_date_bindings_do_not_collide_after_concatenation(self):
        model = _model()
        first, second = model['model']['tables']
        first['name'] = 'A.B'
        first['columns'][0]['name'] = 'C'
        first['columns'][0]['sourceColumn'] = 'C'
        second['name'] = 'A'
        second['columns'][0]['name'] = 'B.C'
        second['columns'][0]['sourceColumn'] = 'B.C'

        result = _runtime_calendar_dates(model, [('A.B', 'C'), ('A', 'B.C')])

        steps = re.findall(
            r'(?m)^    (#[^\r\n]+) = List\.RemoveNulls\(List\.Transform\(\n'
            r'        Table\.Column\((#[^,\r\n]+), "((?:[^"]|"")*)"\),\n'
            r'        each Date\.From\(_\)\)\),$',
            result,
        )
        self.assertEqual(len(steps), 2)
        self.assertEqual(len({identifier for identifier, _, _ in steps}), 2)
        self.assertEqual(
            [(query, column) for _, query, column in steps],
            [('#"Calendar Source A.B.C"', 'C'), ('#"Calendar Source A.B.C 2"', 'B.C')],
        )

    def test_excel_filename_comments_do_not_block_runtime_calendar_dates(self):
        from tableau_export.m_query_builder import _gen_m_excel

        names = ("Orders", "Reviews", "Shipments", "Returns")
        model = {"model": {
            "name": "ExcelSources", "culture": "en-US", "relationships": [],
            "expressions": [],
            "tables": [{
                "name": name,
                "columns": [{
                    "name": "Event Date", "dataType": "DateTime",
                    "sourceColumn": "Event Date",
                }],
                "measures": [],
                "partitions": [{
                    "name": name, "mode": "import", "source": {
                        "type": "m",
                        "expression": _gen_m_excel(
                                {"filename": f"{name}.xlsx", "_source_table": name}, name,
                            [{"name": "Event Date", "datatype": "date"}],
                        ),
                    },
                }],
            } for name in names],
        }}

        with redirect_stdout(io.StringIO()):
            _add_date_table(model)

        calendar = next(t for t in model["model"]["tables"] if t["name"] == "Calendar")
        bounds = calendar["_calendar_bounds"]
        expression = calendar["partitions"][0]["source"]["expression"]
        source_queries = bounds["source_queries"]
        self.assertEqual(bounds["mode"], "source")
        self.assertEqual(len(source_queries), 4)
        self.assertEqual(len(set(source_queries)), 4)
        self.assertIn(
            "Date.StartOfYear(List.Min(List.Combine({SourceDates, {#date(2020, 1, 1)}})))",
            expression,
        )
        self.assertIn(
            "Date.EndOfYear(List.Max(List.Combine({SourceDates, {#date(2030, 12, 31)}})))",
            expression,
        )
        self.assertIn(
            "SourceDates = List.Combine({"
            + ", ".join(f'#"{name}.Event Date Dates"' for name in names)
            + "})",
            expression,
        )
        copied_sources = {
            item["_calendar_source_table"]: item["name"]
            for item in model["model"]["expressions"]
            if item.get("_calendar_source_table")
        }
        self.assertEqual(set(copied_sources), set(names))
        self.assertEqual(set(copied_sources.values()), set(source_queries))


class TestCalendarGenerationIntegration(unittest.TestCase):
    @staticmethod
    def datasource():
        return {
            'name': 'source',
            'connection': {'type': 'CSV', 'details': {'filename': 'facts.csv'}},
            'tables': [{
                'name': 'Facts', 'source_table': '[facts#csv]',
                'columns': [
                    {'name': 'Order Date', 'datatype': 'date'},
                    {'name': 'Refresh Date', 'datatype': 'date'},
                ],
            }],
            'calculations': [], 'columns': [], 'relationships': [],
        }

    def test_model_uses_worksheet_date_and_extracted_hyper_bounds(self):
        from powerbi_import.tmdl_generator import _build_semantic_model

        extra = {
            '_worksheets': [{
                'name': 'Timeline', 'fields': [{
                    'name': 'Refresh Date', 'datasource': 'source',
                    'shelf': 'columns', 'date_part': 'month',
                }],
            }],
            'hyper_files': [{
                'hyper_reader_tables': [{
                    'table': 'Facts',
                    'columns': [{'name': 'Refresh Date', 'datatype': 'date'}],
                    'column_stats': {
                        'Refresh Date': {'min': '1901-02-01', 'max': '2098-12-01'},
                    },
                }],
            }],
        }
        with redirect_stdout(io.StringIO()):
            model = _build_semantic_model([self.datasource()], extra_objects=extra)
        calendar = next(t for t in model['model']['tables'] if t['name'] == 'Calendar')
        self.assertEqual(calendar['_calendar_bounds']['start_year'], 1901)
        self.assertEqual(calendar['_calendar_bounds']['end_year'], 2098)
        relationship = next(
            r for r in model['model']['relationships'] if r['toTable'] == 'Calendar')
        self.assertEqual(relationship['fromColumn'], 'Refresh Date')

    def test_public_generation_refreshes_calendar_copy_after_partition_repair(self):
        from powerbi_import.tmdl_generator import generate_tmdl

        datasource = self.datasource()
        datasource['m_query_override'] = (
            'let Source = #table({"Order Date", "Refresh Date"}, '
            '{{#date(2020, 1, 1), #date(2020, 1, 1)}}) in Source')
        repaired = (
            'let Source = #table({"Order Date", "Refresh Date"}, '
            '{{#date(1980, 1, 1), #date(1980, 1, 1)}}) in Source')

        def repair(model, recovery=None):
            fact = next(t for t in model['model']['tables'] if t['name'] == 'Facts')
            fact['partitions'][0]['source']['expression'] = repaired
            return 1

        with tempfile.TemporaryDirectory() as output:
            with patch('powerbi_import.tmdl_generator._self_heal_model', side_effect=repair), \
                    redirect_stdout(io.StringIO()):
                generate_tmdl([datasource], 'Synthetic', {}, output)
            expressions = Path(
                output, 'definition', 'expressions.tmdl').read_text(encoding='utf-8')
        self.assertIn(repaired, expressions)

    def test_merged_source_resolves_a_unique_original_datasource_field(self):
        source = {
            'name': 'Facts', '_source_workbooks': ['Workbook'],
            'columns': [{'name': 'Order Date'}],
        }
        model = {
            'model': {'tables': [{
                'name': 'Facts', 'columns': [{
                    'name': 'Order Date', 'dataType': 'DateTime',
                }],
            }]},
            '_datasources': [{'name': 'SharedModel', 'tables': [source]}],
        }
        worksheets = [{'fields': [{
            'name': 'Order Date', 'datasource': 'original-source',
            'shelf': 'columns', 'date_part': 'month',
        }]}]
        self.assertIn(('Facts', 'Order Date'),
                      _worksheet_date_usage(model, worksheets, {}))

        model['_datasources'][0]['tables'].append({
            'name': 'Other', '_source_workbooks': ['Other Workbook'],
            'columns': [{'name': 'Order Date'}],
        })
        self.assertEqual(_worksheet_date_usage(model, worksheets, {}), {})

        model['_datasources'][0]['tables'] = [{
            'name': 'Facts', 'columns': [{'name': 'Order Date'}],
        }]
        self.assertEqual(_worksheet_date_usage(model, worksheets, {}), {})


class TestCalendarNestedSourceScope(unittest.TestCase):
    def test_same_depth_annotation_is_invalid_even_with_blank_separator(self):
        for separator in ("", "\n"):
            with self.subTest(blank_separator=bool(separator)):
                broken = (
                    "expression 'Calendar Source Synthetic.DATE' =\n"
                    "\tlet\n\t    Source = 1\n\tin\n\t    Source\n"
                    + separator
                    + '\tannotation Migration_CalendarSourceTable = "Synthetic"\n')
                with self.assertRaisesRegex(AssertionError, "annotation consumed as M"):
                    _read_shared_expressions(broken)

    def test_dedented_metadata_terminates_m_continuation_without_blank_separator(self):
        content = (
            "expression 'Private source' =\n"
            "\t\tlet\n\t\t    Source = 1\n\t\tin\n\t\t    Source\n"
            '\tannotation Migration_CalendarSourceTable = "Synthetic"\n')
        self.assertEqual(_read_shared_expressions(content),
                         {"Private source": "let\n    Source = 1\nin\n    Source"})

    def test_second_source_preserves_inner_let_bindings_and_option_keys_in_output(self):
        model = _model()
        originals = {t["name"]: t["partitions"][0]["source"]["expression"]
                     for t in model["model"]["tables"]}
        with redirect_stdout(io.StringIO()):
            _add_date_table(model)
        with tempfile.TemporaryDirectory() as output:
            _write_tmdl_files(model, output)
            shared = _read_shared_expressions(Path(
                output, "definition", "expressions.tmdl").read_text(encoding="utf-8"))
            serialized = Path(output, "definition", "expressions.tmdl").read_text(encoding="utf-8")
            calendar = Path(output, "definition", "tables", "Calendar.tmdl").read_text(
                encoding="utf-8")
        for name in ("First", "Second"):
            with self.subTest(source=name):
                query = f"Calendar Source {name}.Event Date"
                self.assertIn(query, shared)
                self.assertIn(f'annotation Migration_CalendarSourceTable = "{name}"', serialized)
                # Exact source copying is a stronger lexical check than counting
                # let/in: the known-valid nested fixture cannot gain free names.
                self.assertEqual(shared[query], originals[name])
                self.assertIn(f'Table.Column(#"{query}", "Event Date")', calendar)
                self.assertIn('in #"Promoted Headers" otherwise', shared[query])
                self.assertIn('[PromoteAllScalars=true]', shared[query])
                self.assertIn('QuoteStyle=QuoteStyle.Csv', shared[query])
                self.assertNotRegex(shared[query], r"(?:Delimiter|Encoding|QuoteStyle|PromoteAllScalars)_\d+\s*=")
        self.assertIn("List.Combine", calendar)
        self.assertIn("Date.StartOfYear(List.Min", calendar)
        self.assertIn("Date.EndOfYear(List.Max", calendar)
        self.assertNotRegex(calendar, r"otherwise\s*\{\s*\}")
        # No dependency on the final fact queries (which may depend on Calendar).
        self.assertNotRegex(calendar, r'Table\.Column\((?:First|Second|#"First"|#"Second"),')

    def test_final_partition_repairs_are_refreshed_into_serialized_calendar_copy(self):
        model = _model()
        with redirect_stdout(io.StringIO()):
            _add_date_table(model)
        second = model["model"]["tables"][1]
        corrected = second["partitions"][0]["source"]["expression"].replace(
            'Table.TransformColumnTypes(Source,',
            'Table.TransformColumnTypes(Table.RenameColumns(Source, '
            '{{"event_date", "Event Date"}}),')
        second["partitions"][0]["source"]["expression"] = corrected
        _refresh_calendar_source_expressions(model)
        with tempfile.TemporaryDirectory() as output:
            _write_tmdl_files(model, output)
            shared = _read_shared_expressions(Path(
                output, "definition", "expressions.tmdl").read_text(encoding="utf-8"))
        self.assertEqual(shared["Calendar Source Second.Event Date"], corrected)
        self.assertEqual(shared["Calendar Source First.Event Date"],
                         _nested_source("First.csv"))

    def test_refresh_source_error_is_propagated_not_hidden_as_empty_calendar_dates(self):
        model = _model()
        with redirect_stdout(io.StringIO()):
            _add_date_table(model)
        failure = 'error Error.Record("Synthetic.SourceUnavailable", "missing source", [])'
        model["model"]["tables"][1]["partitions"][0]["source"]["expression"] = failure
        _refresh_calendar_source_expressions(model)
        with tempfile.TemporaryDirectory() as output:
            _write_tmdl_files(model, output)
            shared = _read_shared_expressions(Path(
                output, "definition", "expressions.tmdl").read_text(encoding="utf-8"))
            calendar = Path(output, "definition", "tables", "Calendar.tmdl").read_text(
                encoding="utf-8")
        self.assertEqual(shared["Calendar Source Second.Event Date"], failure)
        self.assertIn('Table.Column(#"Calendar Source Second.Event Date", "Event Date")', calendar)
        self.assertNotRegex(calendar, r"otherwise\s*\{\s*\}")


class TestExplicitCalendarIsolation(unittest.TestCase):
    def test_fixed_bounds_do_not_copy_or_reference_a_failing_source(self):
        from powerbi_import.openability import _refresh_diagnostics

        model = _model()
        model['_calendar_start'] = 2018
        model['_calendar_end'] = 2035
        failing = 'error Error.Record("Synthetic.SourceUnavailable", "missing source", [])'
        model['model']['tables'][0]['partitions'][0]['source']['expression'] = failing
        with redirect_stdout(io.StringIO()):
            _add_date_table(model)
        calendar = next(t for t in model['model']['tables'] if t['name'] == 'Calendar')
        expression = calendar['partitions'][0]['source']['expression']
        self.assertIn('StartDate = #date(2018, 1, 1)', expression)
        self.assertIn('EndDate = #date(2035, 12, 31)', expression)
        self.assertNotIn('SourceDates', expression)
        self.assertNotIn('Table.Column', expression)
        self.assertNotIn('otherwise', expression)
        self.assertEqual(model['model']['tables'][0]['partitions'][0]['source']['expression'],
                         failing)
        self.assertFalse(any(e.get('_calendar_source_table')
                             for e in model['model']['expressions']))
        with tempfile.TemporaryDirectory() as output:
            model_path = Path(output, 'Synthetic.SemanticModel')
            _write_tmdl_files(model, str(model_path))
            content = (model_path / 'definition' / 'tables' / 'Calendar.tmdl').read_text(
                encoding='utf-8')
            annotation = re.search(
                r'(?m)^\tannotation Migration_CalendarBounds = (.+)$', content)
            provenance = json.loads(annotation.group(1))
            self.assertEqual(provenance['mode'], 'fixed')
            self.assertEqual(provenance['source_queries'], [])
            self.assertEqual(provenance['coverage'], 'not_runtime_verified')
            diagnostics, check = _refresh_diagnostics(output)
            self.assertEqual(diagnostics['calendar_ranges'][0]['start_year'], 2018)
            self.assertIn('fixed bounds 2018-2035', '\n'.join(check.issues))

    def test_default_source_mode_and_one_sided_bounds_still_expand(self):
        for start, end in ((None, None), (2018, None), (None, 2035)):
            with self.subTest(start=start, end=end):
                model = _model()
                model.update(_calendar_start=start, _calendar_end=end)
                with redirect_stdout(io.StringIO()):
                    _add_date_table(model)
                calendar = next(t for t in model['model']['tables'] if t['name'] == 'Calendar')
                self.assertEqual(calendar['_calendar_bounds']['mode'], 'source')
                self.assertEqual(len(calendar['_calendar_bounds']['source_queries']), 2)


if __name__ == "__main__":
    unittest.main()
