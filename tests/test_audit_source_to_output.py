"""Permanent audit regressions using only synthetic, temporary source data.

The deliberately small DAX oracle executes the emitted subset, not the Tableau
formula. Unsupported syntax raises instead of silently returning a plausible
answer. No Desktop engine or private workbook is required.
"""

from collections import Counter
from contextlib import redirect_stdout
import io
import json
import math
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import migrate
from powerbi_import.tmdl_generator import generate_tmdl
from powerbi_import.pbip_generator import PowerBIProjectGenerator
from tableau_export.dax_converter import convert_tableau_formula_to_dax


def _split_arguments(text):
    """Split only at top-level commas, respecting DAX strings and references."""
    tokens = re.finditer(r'"(?:[^"]|"")*"|\'(?:[^\']|\'\')*\''
                         r'|\[(?:[^\]]|\]\])*\]|[(),]', text)
    depth, start, parts = 0, 0, []
    for match in tokens:
        token = match.group()
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif token == "," and depth == 0:
            parts.append(text[start:match.start()].strip())
            start = match.end()
    if depth:
        raise AssertionError("Unbalanced oracle input: " + text)
    return parts + [text[start:].strip()]


def _evaluate(expression, rows, measures=None, row=None):
    """Execute just the scalar/ALLEXCEPT subset used by these regressions."""
    expression = expression.strip()
    measures = measures or {}
    while expression.startswith("(") and expression.endswith(")"):
        # Strip grouping only when its closing parenthesis ends the expression.
        if len(_split_arguments(expression[1:-1])) != 1:
            break
        depth = 0
        closes_at_end = True
        for index, char in enumerate(expression):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if depth == 0 and index != len(expression) - 1:
                closes_at_end = False
                break
        if not closes_at_end:
            break
        expression = expression[1:-1].strip()
    # Comparisons outside calls/strings/column names.
    depth = 0
    for token in re.finditer(r'"(?:[^"]|"")*"|\'(?:[^\']|\'\')*\''
                             r'|\[(?:[^\]]|\]\])*\]|[()]|==|>=|<=|>|<|=', expression):
        value = token.group()
        if value == "(":
            depth += 1
        elif value == ")":
            depth -= 1
        elif depth == 0 and value in ("==", "=", ">", "<", ">=", "<="):
            left = _evaluate(expression[:token.start()], rows, measures, row)
            right = _evaluate(expression[token.end():], rows, measures, row)
            if value == "==":
                return type(left) is type(right) and left == right
            left, right = (0 if left is None else left), (0 if right is None else right)
            return {"=": lambda: left == right, ">": lambda: left > right,
                    "<": lambda: left < right, ">=": lambda: left >= right,
                    "<=": lambda: left <= right}[value]()
    call = re.fullmatch(r"([A-Za-z_]\w*)\((.*)\)", expression, re.S)
    if call:
        name, arguments = call[1].upper(), _split_arguments(call[2])
        if name == "BLANK" and arguments == [""]:
            return None
        if name == "CALCULATE":
            if len(arguments) != 2:
                raise AssertionError("Oracle requires one explicit grain: " + expression)
            grain = re.fullmatch(r"ALLEXCEPT\((.*)\)", arguments[1], re.S)
            if not grain:
                raise AssertionError("Unsupported oracle filter: " + arguments[1])
            keys = [re.fullmatch(r"'Facts'\[([^\]]+)\]", ref)[1]
                    for ref in _split_arguments(grain[1])[1:]]
            selected = rows if row is None else [
                candidate for candidate in rows
                if all(candidate[key] == row[key] for key in keys)]
            return _evaluate(arguments[0], selected, measures, row)
        if name == "SUM":
            ref = re.fullmatch(r"'Facts'\[([^\]]+)\]", arguments[0])
            if not ref:
                raise AssertionError("Unsupported SUM argument: " + arguments[0])
            values = [r[ref[1]] for r in rows if r[ref[1]] is not None]
            return sum(values) if values else None
        if name in ("COUNTX", "SUMX") and len(arguments) == 2:
            if arguments[0] != "'Facts'":
                raise AssertionError("Unsupported oracle iterator table")
            values = [_evaluate(arguments[1], rows, measures, candidate)
                      for candidate in rows]
            nonblank = [value for value in values if value is not None]
            if not nonblank:
                return None
            return len(nonblank) if name == "COUNTX" else sum(nonblank)
        if name == "COALESCE":
            for argument in arguments:
                value = _evaluate(argument, rows, measures, row)
                if value is not None:
                    return value
            return None
        if name == "DIVIDE":
            if len(arguments) != 2:
                raise AssertionError("Undefined ratios must not have an alternate result")
            numerator, denominator = [_evaluate(arg, rows, measures, row)
                                      for arg in arguments]
            return None if numerator is None or denominator in (None, 0) else numerator / denominator
        if name == "IF" and len(arguments) == 3:
            branch = 1 if _evaluate(arguments[0], rows, measures, row) else 2
            return _evaluate(arguments[branch], rows, measures, row)
        raise AssertionError("Unsupported oracle function: " + name)
    if re.fullmatch(r"-?\d+(?:\.\d+)?", expression):
        return float(expression) if "." in expression else int(expression)
    if re.fullmatch(r'"(?:[^"]|"")*"', expression):
        return expression[1:-1].replace('""', '"')
    ref = re.fullmatch(r"\[([^\]]+)\]", expression)
    if ref and ref[1] in measures:
        return _evaluate(measures[ref[1]], rows, measures, row)
    column = re.fullmatch(r"'Facts'\[([^\]]+)\]", expression)
    if column and row is not None:
        return row[column[1]]
    raise AssertionError("Unsupported oracle expression: " + expression)


def _serialized_dax(content, kind, name):
    """Read an actual TMDL expression, excluding metadata and fence markers."""
    declaration = re.search(
        rf"^\t{kind} (?:'{re.escape(name)}'|{re.escape(name)}) =([^\n]*)\n",
        content, re.M)
    if not declaration:
        raise AssertionError(f"Missing serialized {kind} {name}:\n{content}")
    lines = [declaration[1].strip()]
    for line in content[declaration.end():].splitlines():
        if line.strip() == "```":
            if len(lines) > 1:
                break
            continue
        if not line.startswith("\t\t\t"):
            break
        lines.append(line.strip())
    return "\n".join(line for line in lines if line and line != "```")


def _datasource(filename, nullif=True):
    denominator = "NULLIF(SUM([Units]), 0)" if nullif else "SUM([Units])"
    return {
        "name": "Synthetic", "caption": "Synthetic",
        "connection": {"type": "CSV", "details": {"filename": str(filename)}},
        "tables": [{"name": "Facts", "source_table": Path(filename).name,
                    "type": "table", "columns": [
            {"name": name, "datatype": datatype} for name, datatype in (
                ("Entity", "string"), ("Amount", "real"), ("Units", "real"))]}],
        "calculations": [
            {"name": "[ratio]", "caption": "Group ratio", "role": "measure",
             "datatype": "real",
             "formula": "{FIXED [Entity] : SUM([Amount]) / " + denominator + "}"},
            {"name": "[band]", "caption": "Group band", "role": "dimension",
             "datatype": "string",
             "formula": 'IF [ratio] > 2 THEN "High" ELSE "Low" END'},
        ],
        "columns": [], "relationships": [], "connection_map": {},
    }


class TestAuditFixedSourceToOutput(unittest.TestCase):
    def test_source_conditional_count_kpi_zero_allnull_and_empty_are_numeric_zero(self):
        source = 'COUNT(IF [Status] = "Ready" THEN [Value] END)'
        emitted = convert_tableau_formula_to_dax(source, table_name="Facts")
        self.assertEqual(emitted,
                         'COALESCE(COUNTX(\'Facts\', IF(\'Facts\'[Status] = "Ready", '
                         "'Facts'[Value], BLANK())), 0)")
        for rows, expected in (
            ([], 0),
            ([{"Status": "Ready", "Value": None}], 0),
            ([{"Status": "Other", "Value": 9}], 0),
            ([{"Status": "Ready", "Value": 0}], 1),
            ([{"Status": "Ready", "Value": 0},
              {"Status": "Ready", "Value": 4},
              {"Status": "Ready", "Value": None}], 2),
        ):
            with self.subTest(rows=rows):
                actual = _evaluate(emitted, rows)
                self.assertEqual(actual, expected)
                self.assertIsNotNone(actual)

    def test_source_count_ratio_zero_denominator_stays_blank_and_sum_is_not_coalesced(self):
        source = ('COUNT(IF [Status] = "Ready" THEN [Value] END) / '
                  'COUNT(IF [Status] = "Eligible" THEN [Value] END)')
        emitted = convert_tableau_formula_to_dax(source, table_name="Facts")
        self.assertEqual(emitted,
                         'DIVIDE(COALESCE(COUNTX(\'Facts\', IF(\'Facts\'[Status] = "Ready", '
                         "'Facts'[Value], BLANK())), 0), "
                         'COALESCE(COUNTX(\'Facts\', IF(\'Facts\'[Status] = "Eligible", '
                         "'Facts'[Value], BLANK())), 0))")
        for rows in ([], [{"Status": "Ready", "Value": 7}],
                     [{"Status": "Eligible", "Value": None}]):
            with self.subTest(rows=rows):
                self.assertIsNone(_evaluate(emitted, rows))
        self.assertEqual(_evaluate(emitted, [{"Status": "Eligible", "Value": 1}]), 0)
        self.assertEqual(_evaluate(emitted, [
            {"Status": "Eligible", "Value": 1}, {"Status": "Ready", "Value": 0}]), 1)
        sum_expression = convert_tableau_formula_to_dax(
            'SUM(IF [Status] = "Ready" THEN [Value] END)', table_name="Facts")
        self.assertEqual(sum_expression,
                         'SUMX(\'Facts\', IF(\'Facts\'[Status] = "Ready", '
                         "'Facts'[Value], BLANK()))")
        self.assertIsNone(_evaluate(sum_expression, []))
        self.assertIsNone(_evaluate(sum_expression, [{"Status": "Ready", "Value": None}]))
        self.assertEqual(_evaluate(sum_expression, [{"Status": "Ready", "Value": 0}]), 0)

    def test_oracle_rejects_unsupported_syntax_and_distinguishes_blank_from_zero(self):
        self.assertIsNone(_evaluate("DIVIDE(1, 0)", []))
        self.assertEqual(_evaluate("DIVIDE(0, 2)", []), 0)
        self.assertFalse(_evaluate("BLANK() == 0", []))
        with self.assertRaisesRegex(AssertionError, "Unsupported"):
            _evaluate("UNKNOWN(1)", [])
        with self.assertRaisesRegex(AssertionError, "Unsupported"):
            _evaluate("1 / 0", [])

    def test_converted_fixed_aggregate_division_executes_undefined_as_blank(self):
        for nullif in (False, True):
            source = _datasource("unused.csv", nullif)["calculations"][0]["formula"]
            expression = convert_tableau_formula_to_dax(source, table_name="Facts")
            with self.subTest(nullif=nullif):
                self.assertNotIn("NULLIF(", expression.upper())
                self.assertNotIn("/", expression)
                self.assertIn("ALLEXCEPT('Facts', 'Facts'[Entity])", expression)
                for numerator, denominator, expected in (
                        (0, 0, None), (7, 0, None), (0, None, None),
                        (0, 2, 0), (9, 3, 3), (-9, 3, -3)):
                    with self.subTest(numerator=numerator, denominator=denominator):
                        actual = _evaluate(expression, [
                            {"Entity": "A", "Amount": numerator, "Units": denominator}])
                        if expected is None:
                            self.assertIsNone(actual, "Undefined Tableau ratios must be BLANK, not NaN/Infinity")
                        else:
                            self.assertEqual(actual, expected)
                            self.assertTrue(math.isfinite(actual))

    def test_fixed_denominator_cancellation_is_blank_not_an_average_of_row_ratios(self):
        for nullif in (False, True):
            with self.subTest(nullif=nullif):
                source = _datasource("unused.csv", nullif)["calculations"][0]["formula"]
                expression = convert_tableau_formula_to_dax(source, table_name="Facts")
                rows = [{"Entity": "A", "Amount": 8, "Units": 2},
                        {"Entity": "A", "Amount": 4, "Units": -2}]
                self.assertIsNone(_evaluate(expression, rows, row=rows[0]))

    def test_final_tmdl_category_is_a_groupable_column_with_non_global_distribution(self):
        # Deliberately unequal group sizes: row-local division and one global
        # category both produce distributions different from the source FIXED.
        rows = [
            {"Entity": "A", "Amount": 9, "Units": 1},
            {"Entity": "A", "Amount": 0, "Units": 2},
            {"Entity": "B", "Amount": 2, "Units": 2},
            {"Entity": "C", "Amount": 0, "Units": 0},
            {"Entity": "C", "Amount": 5, "Units": 0},
        ]
        with tempfile.TemporaryDirectory() as root:
            source = Path(root, "Data", "synthetic.csv")
            source.parent.mkdir()
            source.write_text("Entity,Amount,Units\nA,9,1\nA,0,2\nB,2,2\nC,0,0\nC,5,0\n",
                              encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                datasource = _datasource(source)
                stats = generate_tmdl([datasource], "Synthetic", None,
                                      output_dir=root)
            content = Path(root, "definition", "tables", "Facts.tmdl").read_text(encoding="utf-8")
            # Use the production openability entry point, not a bypass or
            # weakened dependency policy, against the serialized category.
            from powerbi_import.openability import check_openability
            from tests.test_openability import _write_project
            validation_root = Path(root, "validation")
            validation_root.mkdir()
            _write_project(str(validation_root))
            definition = validation_root / "P.SemanticModel" / "definition"
            (definition / "tables" / "Facts.tmdl").write_text(content, encoding="utf-8")
            (definition / "expressions.tmdl").write_text(
                Path(root, "definition", "expressions.tmdl").read_text(encoding="utf-8"),
                encoding="utf-8")
            openability = check_openability(str(validation_root))
            self.assertTrue(openability.openable, openability.blocking_issues)
            dependencies = [check for check in openability.checks
                            if check.name == "calc_column_dependencies"]
            self.assertEqual(len(dependencies), 1)
            self.assertTrue(dependencies[0].ok, dependencies[0].issues)
            # Feed the semantic owner's authoritative symbol contract through
            # the real report binder, rather than hard-coding a Column field.
            generator = PowerBIProjectGenerator(output_dir=root)
            generator._actual_bim_symbols = stats["actual_bim_symbols"]
            generator._actual_bim_measure_names = stats["actual_bim_measures"]
            generator._actual_bim_column_types = stats["actual_bim_column_types"]
            generator._build_field_mapping({"datasources": [datasource]})
            worksheet = {
                "name": "Grouped distribution", "chart_type": "columnChart",
                "original_mark_class": "Bar",
                "fields": [
                    {"name": "band", "shelf": "columns", "datasource": "Synthetic"},
                    {"name": "Amount", "shelf": "rows", "datasource": "Synthetic",
                     "aggregation": "sum"}],
            }
            visuals = Path(root, "visuals")
            with redirect_stdout(io.StringIO()):
                generator._create_visual_worksheet(
                    str(visuals), worksheet, {"worksheetName": worksheet["name"],
                                            "position": {}},
                    1, 1, 0, [worksheet], {})
            paths = list(visuals.rglob("visual.json"))
            self.assertEqual(len(paths), 1)
            visual = json.loads(paths[0].read_text(encoding="utf-8"))["visual"]
        self.assertGreaterEqual(stats["tables"], 1)
        self.assertNotIn("Migration.SourceUnavailable", content)
        self.assertNotRegex(content, r"(?m)^\tmeasure 'Group band' =")
        classifier = _serialized_dax(content, "column", "Group band")
        ratio = _serialized_dax(content, "measure", "Group ratio")
        self.assertIn("ALLEXCEPT('Facts', 'Facts'[Entity])", classifier)
        # Refresh-time grouping must be self-contained. A measure call here
        # creates an unsafe dependency/context-transition closure and fails the
        # real openability gate even when the scalar result looks correct.
        self.assertNotIn("[Group ratio]", classifier)
        self.assertIn("DIVIDE(", classifier)
        self.assertIn("SUM('Facts'[Amount])", classifier)
        self.assertIn("SUM('Facts'[Units])", classifier)
        code = re.sub(r'"(?:[^"]|"")*"', '""', classifier)
        references = re.findall(r"('(?:[^']|'')+'|\b[A-Za-z_]\w*)?\[([^\]]+)\]", code)
        self.assertTrue(references)
        self.assertTrue(all(owner == "'Facts'" and name in {"Entity", "Amount", "Units"}
                            for owner, name in references), references)
        self.assertIn("summarizeBy: none", content)
        measures = {"Group ratio": ratio}
        actual = [_evaluate(classifier, rows, measures, row) for row in rows]
        self.assertEqual(actual, ["High", "High", "Low", "Low", "Low"])
        self.assertEqual(Counter(actual), {"High": 2, "Low": 3})
        self.assertEqual(_evaluate(ratio, rows, measures, rows[0]), 3)
        self.assertEqual(_evaluate(ratio, rows, measures, rows[2]), 1)
        self.assertIsNone(_evaluate(ratio, rows, measures, rows[3]))
        state = visual["query"]["queryState"]
        self.assertEqual(state["Category"]["projections"][0]["field"], {
            "Column": {"Expression": {"SourceRef": {"Entity": "Facts"}},
                       "Property": "Group band"}})
        self.assertNotIn("Group band", stats["actual_bim_measures"])
        self.assertEqual(stats["actual_bim_column_types"][("Facts", "Group band")], "string")
        materialized = stats["materialized_dimension_columns"]
        self.assertEqual(set(materialized), {("Facts", "Group band")})
        self.assertEqual(materialized[("Facts", "Group band")]["grain_columns"], ["Entity"])
        self.assertEqual(materialized[("Facts", "Group band")]["fixed_measures"], ["Group ratio"])


class TestAuditFinalPowerQueryExport(unittest.TestCase):
    def test_single_twb_exports_final_alias_quote_and_projection_repairs(self):
        self._assert_final_export(packaged=False)

    def test_single_twbx_exports_final_alias_quote_and_projection_repairs(self):
        self._assert_final_export(packaged=True)

    def test_batch_twb_exports_final_alias_quote_and_projection_repairs(self):
        self._assert_final_export(packaged=False, batch=True)

    def test_batch_twbx_exports_final_alias_quote_and_projection_repairs(self):
        self._assert_final_export(packaged=True, batch=True)

    def _assert_final_export(self, packaged, batch=False):
        # Seed the old PowerQuery artifact from pre-repair M, then persist the
        # repaired TMDL state before exercising real post-generation
        # orchestration. The final export must come from that on-disk state.
        from tests.test_csv_partition_wiring import (
            NAME, _csv_partition, _project, _read, _write,
        )
        with tempfile.TemporaryDirectory() as root:
            project = _project(root, {"Facts": _csv_partition(
                "synthetic.csv", ["ID", "Label (other.csv)"], count=1)})
            definition = Path(project, f"{NAME}.SemanticModel", "definition")
            # Calendar starts as the earlier model snapshot. The final alias
            # and projection mutations must reach this persisted private copy
            # after restriction, before either PowerQuery export is written.
            shared_path = definition / "expressions.tmdl"
            with shared_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    '\nexpression \'Unrelated private date source\' =\n'
                    '\tlet Source = #table({"Label"}, {}) in Source\n'
                    '\tannotation Migration_CalendarSourceTable = "Facts"\n')
            table_path = definition / "tables" / "Facts.tmdl"
            with table_path.open("a", encoding="utf-8") as stream:
                stream.write("\n\tcolumn ID\n\t\tsourceColumn: ID\n"
                             "\n\tcolumn 'Label (other.csv)'\n"
                             "\t\tsourceColumn: Label (other.csv)\n")
            _write(str(Path(project, "Data", "synthetic.csv")),
                   'ID;Label;Unused\n1;"first;second";extra\n')
            workbook = Path(root, "synthetic.twb")
            workbook.write_text("<workbook/>", encoding="utf-8")
            if packaged:
                workbook = workbook.with_suffix(".twbx")
                with zipfile.ZipFile(workbook, "w") as archive:
                    archive.writestr("synthetic.twb", "<workbook/>")
                    archive.writestr("Data/synthetic.csv",
                                     'ID;Label;Unused\n1;"first;second";extra\n')
            with redirect_stdout(io.StringIO()):
                migrate._export_power_query_files(project, NAME)
                stale = Path(project, "PowerQuery", "Facts.pq").read_text(encoding="utf-8")
                self.assertIn('Delimiter=","', stale)
                self.assertIn("Columns=1", stale)
                self.assertIn("QuoteStyle=QuoteStyle.None", stale)
                # This fixture stands in for repairs already persisted by the
                # model-generation stage. Use the real normalizers against
                # synthetic data; the PBIP orchestration below only publishes
                # the persisted model and must not read/copy native sources.
                migrate._reconcile_csv_partitions(project, NAME)
                migrate._restrict_file_partitions_to_model(project, NAME)
                if batch:
                    # Generation and upstream repairs are fixture preconditions;
                    # run real post-generation synchronization/export through
                    # the batch entry point.
                    with patch.object(migrate, "run_generation", return_value=True), \
                            patch.object(migrate, "run_migration_report", return_value={}):
                        result = migrate._migrate_single_workbook(
                            tableau_file=str(workbook), basename=NAME,
                            workbook_output_dir=root, display_name="Synthetic",
                            skip_extraction=True, wb_prep=None, wb_cal_start=None,
                            wb_cal_end=None, wb_culture=None, verify_open=False)
                    self.assertTrue(result["success"])
                else:
                    migrate._extract_twbx_data_files(SimpleNamespace(
                        tableau_file=str(workbook), output_dir=root, output_format="pbip"), NAME)
            final = _read(project, "Facts")
            exported = Path(project, "PowerQuery", "Facts.pq").read_text(encoding="utf-8")
            # Independently strip only TMDL indentation from the partition.
            body = final.split("\t\tsource =\n", 1)[1].split("\n\tcolumn ", 1)[0]
            partition = "\n".join(line[4:] for line in body.splitlines()
                                  if line.startswith("\t\t\t\t")).strip()
            self.assertNotEqual(stale, exported)
            self.assertEqual(exported.strip(), partition)
            self.assertIn('Delimiter=";"', exported)
            self.assertIn("QuoteStyle=QuoteStyle.Csv", exported)
            self.assertNotIn("Columns=1", exported)
            self.assertIn('{{"Label", "Label (other.csv)"}}', exported)
            self.assertIn("Table.SelectColumns", exported)
            self.assertIn("MissingField.UseNull", exported)
            shared = Path(project, "PowerQuery", "_expressions.pq").read_text(encoding="utf-8")
            self.assertEqual(shared.strip(), (definition / "expressions.tmdl").read_text(
                encoding="utf-8").strip())
            from tests.test_tmdl_dates import _read_shared_expressions
            copies = _read_shared_expressions(shared)
            self.assertEqual(copies["Unrelated private date source"], exported.strip())
            self.assertIn('annotation Migration_CalendarSourceTable = "Facts"', shared)
            self.assertNotIn('#table({"Label"}, {})', shared)


if __name__ == "__main__":
    unittest.main()
