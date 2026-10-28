"""Contracts for schema-bound header promotion in generated Power Query M.

These are emission tests, NOT Power Query runtime tests. The existing scalar-M
test evaluator cannot execute tables, lists, lambdas, or Error.Record. In
particular, Python whitespace handling is not used as a substitute for M's
Text.Trim or ordinal header matching. Native refresh acceptance belongs to the
separate Excel/CSV CLI fixtures.
"""

import copy
import re
import unittest
from unittest import mock

from powerbi_import.m_validator import validate_m_query
from tableau_export.m_query_builder import (
    _m_header_literal,
    _promote_headers_with_schema,
    generate_power_query_m,
    m_transform_promote_headers,
    m_transform_wildcard_union,
)

_TYPED_PROMOTIONS = (
    (
        "Excel",
        {"filename": "facts.xlsx"},
        "[Exact$]",
        '#"Header Facts Sheet"',
        "Excel.Workbook",
    ),
    (
        "CSV",
        {"filename": "facts.csv"},
        "[facts#csv]",
        "Source",
        "Csv.Document",
    ),
    (
        "Google Sheets",
        {"spreadsheet_id": "PUBLIC_SPREADSHEET_PLACEHOLDER", "sheet_name": "Exact"},
        "",
        "Parsed",
        "Web.Contents",
    ),
    (
        "SharePoint",
        {"site_url": "https://synthetic.invalid/sites/public", "filename": "facts.xlsx"},
        "[Exact$]",
        "Sheet",
        "SharePoint.Files",
    ),
    (
        "PDF",
        {"filename": "facts.pdf"},
        "[Table001]",
        "Table1",
        "Pdf.Tables",
    ),
    (
        "Azure Blob",
        {"account": "publicplaceholder", "container": "public-container"},
        "",
        "Parsed",
        "AzureStorage.Blobs",
    ),
)
_FILE_PROMOTIONS = {"Excel", "CSV", "PDF"}


def _columns(*names_types):
    return [{"name": name, "datatype": datatype} for name, datatype in names_types]


def _query(case, columns=None, *, native=False):
    connector, details, relation, _, _ = case
    details = copy.deepcopy(details)
    if native and connector in _FILE_PROMOTIONS:
        details["directory"] = "Data"
        details["_source_base_dir"] = r"C:\Synthetic\HeaderBinding"
    return generate_power_query_m(
        {"type": connector, "details": details},
        {
            "name": "Header Facts",
            "source_table": relation,
            "columns": (
                _columns(("Id", "integer"), ("Amount", "real"))
                if columns is None else columns
            ),
        },
        use_original_file_paths=native,
    )


def _promoted_headers_step(query):
    """Extract emitted text, without interpreting a header or any M operation."""
    start = query.index('    #"Promoted Headers" = ')
    following = re.search(
        r'^    (?:#"Selected Columns"|#"Changed Types"|Result) = ',
        query[start:],
        re.M,
    )
    if following is None:
        raise AssertionError("Promoted headers must feed a declared downstream step")
    return query[start:start + following.start()].rstrip("\r\n")


class TestTypedPromotionHeaderBindingEmission(unittest.TestCase):
    """Shared promotion wiring and schema identity, without executing M."""

    def test_all_six_typed_promotions_bind_before_column_typing(self):
        for case in _TYPED_PROMOTIONS:
            connector, _, _, source_step, source_function = case
            with self.subTest(connector=connector):
                query = _query(case)
                promotion = _promoted_headers_step(query)
                self.assertIn(source_function + "(", query)
                self.assertEqual(query.count("Table.PromoteHeaders("), 1)
                self.assertIn(
                    f"Table.PromoteHeaders({source_step}, [PromoteAllScalars=true])",
                    promotion,
                )
                self.assertIn("Table.ColumnNames(", promotion)
                self.assertIn("Text.Trim(", promotion)
                self.assertIn("Comparer.Ordinal", promotion)
                self.assertIn("error Error.Record(", promotion)
                self.assertEqual(query.count("Table.TransformColumnTypes("), 1)
                self.assertIn(
                    'Table.TransformColumnTypes(#"Promoted Headers", {', query,
                )
                self.assertLess(
                    query.index(promotion), query.index("Table.TransformColumnTypes("),
                )
                self.assertIn('{"Id", Int64.Type}', query)
                self.assertIn('{"Amount", type number}', query)
                self.assertTrue(query.endswith('in\n    Result'))
                # This validator checks syntax shape, not refresh semantics.
                self.assertEqual(validate_m_query(query), [])

    def test_all_six_use_the_same_generated_binding_program(self):
        programs = []
        for case in _TYPED_PROMOTIONS:
            query = _query(case)
            promotion = _promoted_headers_step(query)
            source_step = case[3]
            original = (
                f"Table.PromoteHeaders({source_step}, [PromoteAllScalars=true])"
            )
            self.assertEqual(promotion.count(original), 1, case[0])
            self.assertIn("Text.Trim(", promotion, case[0])
            programs.append(promotion.replace(original, "PUBLIC_PROMOTION_PLACEHOLDER"))
        for program in programs[1:]:
            self.assertEqual(program, programs[0])

    def test_native_csv_binds_before_strict_projection_and_type_conversion(self):
        case = next(case for case in _TYPED_PROMOTIONS if case[0] == "CSV")
        query = _query(case, native=True)
        promotion = _promoted_headers_step(query)
        selected = (
            '#"Selected Columns" = Table.SelectColumns('
            '#"Promoted Headers", {"Id", "Amount"})'
        )
        self.assertIn("Text.Trim(", promotion)
        self.assertIn("error Error.Record(", promotion)
        self.assertIn(selected, query)
        self.assertLess(query.index(promotion), query.index(selected))
        self.assertLess(query.index(selected), query.index("Table.TransformColumnTypes("))
        self.assertIn('Table.TransformColumnTypes(#"Selected Columns", {', query)
        self.assertNotRegex(query, r"\bColumns\s*=")
        self.assertNotIn("MissingField.Ignore", query)
        self.assertNotIn("MissingField.UseNull", query)
        self.assertIn(
            r'File.Contents("C:\Synthetic\HeaderBinding\Data\facts.csv")', query,
        )
        self.assertEqual(validate_m_query(query), [])

    def test_native_excel_and_csv_keep_the_staged_header_binding_program(self):
        for case in _TYPED_PROMOTIONS:
            if case[0] not in ("Excel", "CSV"):
                continue
            with self.subTest(connector=case[0]):
                staged = _query(case)
                native = _query(case, native=True)
                self.assertEqual(
                    _promoted_headers_step(native), _promoted_headers_step(staged),
                )
                self.assertIn("Text.Trim(", _promoted_headers_step(native))
                self.assertNotIn("DataFolder", native)
                self.assertIn("DataFolder", staged)
                self.assertIn('{"Amount", type number}', native)
                self.assertEqual(validate_m_query(native), [])

    def test_empty_declared_schema_retains_original_pure_promotion(self):
        for case in _TYPED_PROMOTIONS:
            for native in (False, True):
                with self.subTest(connector=case[0], native=native):
                    query = _query(case, [], native=native)
                    self.assertEqual(
                        _promoted_headers_step(query),
                        '    #"Promoted Headers" = Table.PromoteHeaders('
                        + case[3] + ', [PromoteAllScalars=true]),',
                    )
                    self.assertIn('    Result = #"Promoted Headers"\nin\n    Result', query)
                    for absent in (
                        "Text.Trim(", "Table.ColumnNames(", "Comparer.Ordinal",
                        "Error.Record(", "Table.TransformColumnTypes(",
                        "Table.SelectColumns(",
                    ):
                        self.assertNotIn(absent, query)
                    self.assertEqual(validate_m_query(query), [])

    def test_generation_is_metadata_only_for_all_six_typed_promotions(self):
        columns = _columns(
            ("Id", "integer"),
            ("Amount", "real"),
            ("Trailing Metric ", "integer"),
        )
        before = copy.deepcopy((_TYPED_PROMOTIONS, columns))
        with mock.patch(
            "builtins.open",
            side_effect=AssertionError("Payload reads belong to native refresh"),
        ) as opened, mock.patch(
            "io.open",
            side_effect=AssertionError("Payload reads must not infer header bindings"),
        ) as path_opened:
            for case in _TYPED_PROMOTIONS:
                for native in (False, True):
                    with self.subTest(connector=case[0], native=native):
                        query = _query(case, columns, native=native)
                        self.assertIn("Table.ColumnNames(", _promoted_headers_step(query))
                        self.assertIn('{"Trailing Metric ", Int64.Type}', query)
                        connection = {"type": case[0], "details": copy.deepcopy(case[1])}
                        if native and case[0] in _FILE_PROMOTIONS:
                            connection["details"].update({
                                "directory": "Data",
                                "_source_base_dir": r"C:\Synthetic\HeaderBinding",
                            })
                        table = {
                            "name": "Header Facts", "source_table": case[2],
                            "columns": copy.deepcopy(columns),
                        }
                        inputs = copy.deepcopy((connection, table))
                        generate_power_query_m(
                            connection, table, use_original_file_paths=native,
                        )
                        self.assertEqual((connection, table), inputs)
        opened.assert_not_called()
        path_opened.assert_not_called()
        self.assertEqual((_TYPED_PROMOTIONS, columns), before)

    def test_declared_column_order_and_intentionally_spaced_identities_survive(self):
        columns = _columns(
            ("Trailing Metric ", "integer"),
            ("Amount", "real"),
            (" Amount", "real"),
            ("Id", "integer"),
        )
        columns[0]["caption"] = "Trailing Metric"
        columns[0]["sourceColumn"] = "Different Physical Alias"
        columns[2]["caption"] = "Alternate Amount"
        before = copy.deepcopy(columns)
        expected_types = (
            '        {"Trailing Metric ", Int64.Type},\n'
            '        {"Amount", type number},\n'
            '        {" Amount", type number},\n'
            '        {"Id", Int64.Type}'
        )
        for case in _TYPED_PROMOTIONS:
            with self.subTest(connector=case[0]):
                query = _query(case, columns)
                self.assertIn(
                    'Expected = {"Trailing Metric ", "Amount", " Amount", "Id"},',
                    query,
                )
                self.assertIn(expected_types, query)
                self.assertNotIn("Alternate Amount", query)
                self.assertNotIn("Different Physical Alias", query)
                self.assertNotIn('{"Trailing Metric", Int64.Type}', query)
                self.assertNotIn('{"Trailing Metric ", "Trailing Metric"}', query)
                self.assertNotIn("Table.TransformColumnNames(", query)
                self.assertEqual(validate_m_query(query), [])
        self.assertEqual(columns, before)

    def test_exact_escaped_names_feed_expected_schema_projection_and_types(self):
        names = (
            ("Id", '"Id"', "Int64.Type", "integer"),
            (
                'Metric "#(lf)"\tName\r\n',
                '"Metric ""#(#)(lf)""#(tab)Name#(cr)#(lf)"',
                "type text",
                "string",
            ),
            ("Trailing\u00a0", '"Trailing\u00a0"', "type number", "real"),
        )
        columns = _columns(*((name, datatype) for name, _, _, datatype in names))
        expected_list = "{" + ", ".join(literal for _, literal, _, _ in names) + "}"
        before = copy.deepcopy(columns)
        for case in _TYPED_PROMOTIONS:
            with self.subTest(connector=case[0]):
                query = _query(case, columns, native=True)
                self.assertIn("Expected = " + expected_list + ",", query)
                for _, literal, m_type, _ in names:
                    self.assertIn("{" + literal + ", " + m_type + "}", query)
                if case[0] == "CSV":
                    self.assertIn(
                        'Table.SelectColumns(#"Promoted Headers", '
                        + expected_list + ")",
                        query,
                    )
                self.assertNotIn('Metric ""#(lf)""', query)
                self.assertEqual(validate_m_query(query), [])
        self.assertEqual(columns, before)

    def test_existing_rows_and_missing_fields_are_not_hidden_by_fallbacks(self):
        for case in _TYPED_PROMOTIONS:
            with self.subTest(connector=case[0]):
                query = _query(case)
                promotion = _promoted_headers_step(query)
                for forbidden in (
                    "MissingField.Ignore", "MissingField.UseNull",
                    "otherwise", "#table(", "Table.RemoveRowsWithErrors(",
                    "Table.ReplaceErrorValues(", "Table.SelectRows(",
                    "Table.FirstN(", "Table.Skip(", "Table.RemoveColumns(",
                    "Table.SelectColumns(", "Table.Distinct(",
                    "Table.TransformColumnNames(", "Table.FromRows(",
                ):
                    self.assertNotIn(forbidden, promotion)
                self.assertIn("Table.RenameColumns(", promotion)
                self.assertIn("error Error.Record(", promotion)
                self.assertIn('{"Id", Int64.Type}', query)
                self.assertIn('{"Amount", type number}', query)

    def test_nonpromoting_connectors_do_not_acquire_header_normalization(self):
        for connector, details, source_function in (
            ("JSON", {"filename": "facts.json"}, "Json.Document"),
            ("XML", {"filename": "facts.xml"}, "Xml.Tables"),
            (
                "SQL Server",
                {"server": "synthetic.invalid", "database": "PublicPlaceholder"},
                "Sql.Database",
            ),
        ):
            with self.subTest(connector=connector):
                query = generate_power_query_m(
                    {"type": connector, "details": details},
                    {"name": "Facts", "columns": _columns(("Amount", "real"))},
                )
                self.assertIn(source_function + "(", query)
                for absent in (
                    "Table.PromoteHeaders(", "Text.Trim(", "Comparer.Ordinal",
                    "Table.ColumnNames(", "Error.Record(",
                ):
                    self.assertNotIn(absent, query)
                self.assertEqual(validate_m_query(query), [])


class TestSchemaBoundPromotionGuardEmission(unittest.TestCase):
    """Pin the emitted algorithm, not a Python reimplementation of M matching."""

    _EXACT = (
        "Exact = List.Select(Expected, "
        "(name) => List.Contains(Headers, name, Comparer.Ordinal)),"
    )
    _AVAILABLE = (
        "Available = List.Select(Headers, "
        "(name) => not List.Contains(Exact, name, Comparer.Ordinal)),"
    )
    _MISSING = (
        "Missing = List.Select(Expected, "
        "(name) => not List.Contains(Exact, name, Comparer.Ordinal)),"
    )
    _CANDIDATES = (
        "Candidates = List.Select(Available, "
        "(header) => Comparer.Ordinal(Text.Trim(header), name) = 0)"
    )
    _ERROR_TAIL = '''    in
        if not List.IsEmpty(BlankExpectedNames) or not List.IsEmpty(DuplicateExpectedNames) then
            error Error.Record("HeaderSchema.Error",
                "Declared source column names must be nonblank and unique.",
                [BlankExpectedNames = BlankExpectedNames, DuplicateExpectedNames = DuplicateExpectedNames])
        else if not List.IsEmpty(MissingExpectedNames) or not List.IsEmpty(AmbiguousExpectedNames) then
            error Error.Record("HeaderMatch.Error",
                "Source headers must uniquely match the declared source column names.",
                [MissingExpectedNames = MissingExpectedNames, AmbiguousExpectedNames = AmbiguousExpectedNames])
        else if List.Count(List.Distinct(RenameSources, Comparer.Ordinal)) <> List.Count(Renames) then
            error Error.Record("HeaderMatch.Error",
                "Source header matches must be one-to-one.",
                [ExpectedNames = Missing])
        else Table.RenameColumns(Promoted, Renames)'''

    def _expression(self, *names):
        return _promote_headers_with_schema(
            "PublicRawTable",
            _columns(*((name, "string") for name in (names or ("Id", "Amount")))),
        )

    def test_leading_trailing_whitespace_fallback_compares_raw_trim_to_unchanged_target(self):
        # This is the emitted predicate needed for "        Amount", "Amount ",
        # NBSP, and tab padding. It does not assert those M runtime results.
        expression = self._expression()
        self.assertIn('Expected = {"Id", "Amount"},', expression)
        self.assertIn(self._AVAILABLE, expression)
        self.assertIn(self._MISSING, expression)
        self.assertIn(
            "Matches = List.Transform(Missing, (name) => [\n"
            "            ExpectedName = name,\n"
            "            " + self._CANDIDATES + "\n"
            "        ]),",
            expression,
        )
        self.assertNotIn("Text.Trim(name)", expression)
        self.assertNotIn("Text.Trim([ExpectedName])", expression)
        self.assertEqual(expression.count("Text.Trim("), 2)

    def test_exact_normal_header_wins_over_any_trim_lookalike(self):
        expression = self._expression("Amount")
        # An exact Amount is removed from Missing, regardless of how many
        # additional physical names could trim to Amount.
        self.assertIn(self._EXACT, expression)
        self.assertIn(self._MISSING, expression)
        self.assertIn("Matches = List.Transform(Missing,", expression)
        self.assertNotIn("Matches = List.Transform(Expected,", expression)
        self.assertIn(
            "Renames = List.Transform(Matches, each {[Candidates]{0}, [ExpectedName]}),",
            expression,
        )
        self.assertTrue(expression.endswith("else Table.RenameColumns(Promoted, Renames)"))

    def test_intentionally_spaced_declared_header_is_reserved_exactly_not_captioned(self):
        columns = [
            {
                "name": "Trailing Metric ", "datatype": "integer",
                "caption": "Trailing Metric", "sourceColumn": "Different Physical Alias",
            },
        ]
        before = copy.deepcopy(columns)
        expression = _promote_headers_with_schema("PublicRawTable", columns)
        self.assertEqual(
            expression,
            _promote_headers_with_schema("PublicRawTable", [{"name": "Trailing Metric "}]),
            "The promotion helper must read name only, not caption/sourceColumn/type metadata",
        )
        self.assertIn('Expected = {"Trailing Metric "},', expression)
        self.assertNotIn('"Trailing Metric"', expression)
        self.assertNotIn("Different Physical Alias", expression)
        self.assertIn(self._EXACT, expression)
        self.assertIn(self._AVAILABLE, expression)
        self.assertIn(self._CANDIDATES, expression)
        self.assertNotIn("Table.TransformColumnNames(", expression)
        self.assertEqual(columns, before)

    def test_fallback_cannot_alias_a_raw_header_reserved_by_an_exact_other_field(self):
        expression = self._expression("Amount", " Amount ")
        self.assertIn('Expected = {"Amount", " Amount "},', expression)
        self.assertIn(self._EXACT, expression)
        self.assertIn(self._AVAILABLE, expression)
        self.assertIn(self._CANDIDATES, expression)
        self.assertLess(expression.index(self._EXACT), expression.index(self._AVAILABLE))
        self.assertLess(expression.index(self._AVAILABLE), expression.index(self._CANDIDATES))
        self.assertNotIn(
            "Candidates = List.Select(Headers,", expression,
            "The exact physical ' Amount ' must not be stolen to satisfy Amount",
        )
        self.assertIn(
            "List.Select(Matches, each List.Count([Candidates]) = 0), each [ExpectedName]",
            expression,
        )

    def test_two_raw_candidates_for_one_expected_name_have_an_explicit_error_guard(self):
        expression = self._expression("Amount")
        self.assertIn(
            "AmbiguousExpectedNames = List.Transform(\n"
            "            List.Select(Matches, each List.Count([Candidates]) > 1), "
            "each [ExpectedName]),",
            expression,
        )
        self.assertIn(
            "else if not List.IsEmpty(MissingExpectedNames) "
            "or not List.IsEmpty(AmbiguousExpectedNames) then\n"
            '            error Error.Record("HeaderMatch.Error",',
            expression,
        )
        self.assertIn(
            "[MissingExpectedNames = MissingExpectedNames, "
            "AmbiguousExpectedNames = AmbiguousExpectedNames]",
            expression,
        )
        self.assertNotIn("List.First(", expression)
        self.assertNotIn("otherwise", expression)

    def test_unknown_missing_header_has_no_default_or_ignore_path(self):
        expression = self._expression("Unknown Required Metric")
        self.assertIn('Expected = {"Unknown Required Metric"},', expression)
        self.assertIn(
            "MissingExpectedNames = List.Transform(\n"
            "            List.Select(Matches, each List.Count([Candidates]) = 0), "
            "each [ExpectedName]),",
            expression,
        )
        self.assertIn(self._ERROR_TAIL, expression)
        for forbidden in (
            "MissingField.Ignore", "MissingField.UseNull", "otherwise",
            "List.First(", "List.FirstN(", "Record.FieldOrDefault(",
            "Table.AddColumn(", "#table(",
        ):
            self.assertNotIn(forbidden, expression)

    def test_schema_missing_and_ambiguity_guards_precede_lazy_rename_consumption(self):
        expression = self._expression()
        # M let bindings are lazy: Candidates{0} may occur in the Renames
        # binding, but the result must reject missing/ambiguous matches before
        # evaluating RenameSources or applying that rename list.
        self.assertEqual(expression[expression.index("    in\n"):], self._ERROR_TAIL)
        self.assertEqual(expression.count("error Error.Record("), 3)
        self.assertEqual(expression.count("Table.RenameColumns("), 1)
        self.assertNotIn("Table.RenameColumns(Promoted, Renames,", expression)

    def test_raw_rename_sources_are_checked_for_one_to_one_ordinal_mapping(self):
        expression = self._expression()
        self.assertIn("RenameSources = List.Transform(Renames, each _{0})", expression)
        self.assertIn(
            "else if List.Count(List.Distinct(RenameSources, Comparer.Ordinal)) "
            "<> List.Count(Renames) then\n"
            '            error Error.Record("HeaderMatch.Error",\n'
            '                "Source header matches must be one-to-one.",\n'
            "                [ExpectedNames = Missing])",
            expression,
        )
        self.assertNotIn("Renames = List.Distinct(", expression)

    def test_blank_declared_names_remain_explicit_schema_errors_not_promote_defaults(self):
        for name, literal in (
            ("", '""'),
            ("   ", '"   "'),
            ("\t", '"#(tab)"'),
            ("\u00a0", '"\u00a0"'),
        ):
            with self.subTest(name=repr(name)):
                expression = self._expression(name)
                self.assertIn("Expected = {" + literal + "},", expression)
                self.assertIn(
                    'BlankExpectedNames = List.Select(Expected, each Text.Trim(_) = ""),',
                    expression,
                )
                self.assertIn(self._ERROR_TAIL, expression)
                self.assertNotIn('"Column1"', expression)

    def test_duplicate_declared_names_are_rejected_without_deduplicating_schema(self):
        expression = self._expression("Id", "Id")
        self.assertIn('Expected = {"Id", "Id"},', expression)
        self.assertIn(
            "DuplicateExpectedNames = List.Select(List.Distinct(Expected, Comparer.Ordinal),\n"
            "            (name) => List.Count(List.Select(Expected, "
            "(other) => Comparer.Ordinal(name, other) = 0)) > 1),",
            expression,
        )
        self.assertIn(self._ERROR_TAIL, expression)
        self.assertNotIn("Expected = List.Distinct(", expression)

    def test_nontext_declared_names_are_not_coerced_or_inferred_from_rows(self):
        for column in (
            {"datatype": "string"},
            {"name": None, "datatype": "string"},
            {"name": 123, "datatype": "string"},
            {"name": 1.5, "datatype": "string"},
            {"name": False, "datatype": "string"},
        ):
            with self.subTest(column=column):
                before = copy.deepcopy(column)
                with self.assertRaisesRegex(
                    ValueError, "Declared source header names must be text",
                ):
                    _promote_headers_with_schema("PublicRawTable", [column])
                self.assertEqual(column, before)

    def test_numeric_and_promote_default_names_use_actual_promoted_column_names(self):
        expression = self._expression("123", "Column1", "Column2", "1.5", "false")
        self.assertIn(
            'Expected = {"123", "Column1", "Column2", "1.5", "false"},',
            expression,
        )
        self.assertIn(
            "Promoted = Table.PromoteHeaders(PublicRawTable, [PromoteAllScalars=true]),\n"
            "        Headers = Table.ColumnNames(Promoted),",
            expression,
        )
        self.assertIn(self._EXACT, expression)
        self.assertIn(self._CANDIDATES, expression)
        for forbidden in (
            "Table.First(", "Table.FirstN(", "Table.ToRows(", "Table.ToColumns(",
            "Text.From(", "Number.FromText(", "Text.StartsWith(", "Table.FromRows(",
        ):
            self.assertNotIn(forbidden, expression)

    def test_matching_does_not_casefold_collapse_internal_spaces_or_suffix_names(self):
        expression = self._expression(
            "Amount", "amount", "Net Amount", "Net  Amount", "Trailing Metric ",
        )
        self.assertIn(
            'Expected = {"Amount", "amount", "Net Amount", "Net  Amount", "Trailing Metric "},',
            expression,
        )
        self.assertIn(self._CANDIDATES, expression)
        for forbidden in (
            "Comparer.OrdinalIgnoreCase", "Comparer.FromCulture", "Text.Upper(",
            "Text.Lower(", "Text.Replace(", "Text.Remove(", "Text.Clean(",
            "Text.Combine(", "Text.Split(", "Text.TrimStart(", "Text.TrimEnd(",
            "Text.Normalize(", "Table.TransformColumnNames(",
        ):
            self.assertNotIn(forbidden, expression)

    def test_error_records_do_not_disclose_actual_headers_rows_or_source_endpoints(self):
        expression = self._expression("Amount")
        tail = expression[expression.index("    in\n"):]
        self.assertEqual(tail, self._ERROR_TAIL)
        for forbidden in (
            "Headers = Headers", "Available = Available", "Candidates =",
            "Promoted =", "PublicRawTable", "File.Contents", "Web.Contents",
            "Table.ToRows", "Table.First",
        ):
            self.assertNotIn(forbidden, tail)


class TestHeaderLiteralEscaping(unittest.TestCase):
    """Exact escaping is executable Python coverage, not an M trim emulator."""

    def test_quotes_are_doubled_without_changing_header_whitespace(self):
        self.assertEqual(
            _m_header_literal('  Amount "net"  '), '"  Amount ""net""  "',
        )

    def test_literal_m_escape_introducers_are_not_interpreted_as_controls(self):
        for raw, expected in (
            ("Amount #(lf)", '"Amount #(#)(lf)"'),
            ("Amount #(tab)", '"Amount #(#)(tab)"'),
            ("Amount #(", '"Amount #(#)("'),
            ("Amount #(#)(lf)", '"Amount #(#)(#)(lf)"'),
            ("Amount #", '"Amount #"'),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(_m_header_literal(raw), expected)

    def test_actual_cr_lf_and_tab_are_emitted_as_m_control_escapes(self):
        self.assertEqual(
            _m_header_literal("A\r\nB\tC"), '"A#(cr)#(lf)B#(tab)C"',
        )

    def test_unicode_nbsp_internal_spaces_and_backslashes_are_preserved_exactly(self):
        self.assertEqual(
            _m_header_literal("\u00a0Montant  réalisé\u00a0"),
            '"\u00a0Montant  réalisé\u00a0"',
        )
        self.assertEqual(
            _m_header_literal(r"C:\Public\Amount"), r'"C:\Public\Amount"',
        )

    def test_mixed_literal_introducers_and_real_controls_are_escaped_in_correct_order(self):
        self.assertEqual(
            _m_header_literal('Metric "#(lf)"\tName\r\n'),
            '"Metric ""#(#)(lf)""#(tab)Name#(cr)#(lf)"',
        )


class TestGenericPromotionRemainsUnchanged(unittest.TestCase):
    def test_generic_promote_headers_transform_remains_schema_free(self):
        self.assertEqual(
            m_transform_promote_headers(),
            (
                '#"Promoted Headers"',
                'Table.PromoteHeaders({prev}, [PromoteAllScalars=true])',
            ),
        )

    def test_wildcard_union_remains_schema_free_and_byte_for_byte_unchanged(self):
        self.assertEqual(
            m_transform_wildcard_union(
                "C:/Synthetic/PublicCsv", file_extension=".csv", delimiter=",",
            ),
            '''let
    // Wildcard Union: all .csv files in folder
    Source = Folder.Files("C:\\Synthetic\\PublicCsv"),
    #"Filtered Files" = Table.SelectRows(Source, each Text.EndsWith([Name], ".csv")),
    #"Added Tables" = Table.AddColumn(#"Filtered Files", "ParsedTable",
        each Csv.Document([Content], [Delimiter=",", Encoding=65001])),
    #"Combined" = Table.Combine(#"Added Tables"[ParsedTable]),
    #"Promoted Headers" = Table.PromoteHeaders(#"Combined", [PromoteAllScalars=true])
in
    #"Promoted Headers"''',
        )
