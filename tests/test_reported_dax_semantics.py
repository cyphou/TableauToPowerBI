"""Synthetic regressions for reported DAX defects 3--6.

The small oracle below evaluates the *emitted* DAX subset, not the Tableau
formula.  It fails on unsupported syntax and is not a DAX engine: these
value checks are bounded synthetic evidence, not Desktop/Fabric validation.
FIXED cases without source-grain evidence are checked as static diagnostics.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from powerbi_import.errors import PartialGenerationError
from powerbi_import.migration_quality import build_quality_report
from powerbi_import.tmdl_dax_postprocess import (
    _unwrap_aggregations_of_measures,
    _validate_measures_after_rewrites,
)
from powerbi_import.tmdl_generator import (
    _build_semantic_model,
    _build_table,
    _collect_semantic_context,
    _create_semantic_tables,
    generate_tmdl,
)
from tableau_export.dax_converter import (
    convert_tableau_formula_to_dax,
    is_supported_scalar_numeric_minmax,
)
from tableau_export.worksheet_extractor import extract_worksheet_fields

_FALLBACK = "TODO: DAX conversion validation failed"
_COLUMN_OWNERS = {
    "Facture": "Zone",
    "Logements": "Zone",
    "Secteur": "Zone",
    "Categorie": "Menage",
    "Poids": "Menage",
    "Revenu": "Menage",
}
_ZONE_COLUMNS = ["Secteur", "Logements", "Facture"]
_S01_HOUSEHOLDS = (
    {"Ref": 9001, "ZoneRef": "S-01", "Poids": 40, "Revenu": 18000},
    {"Ref": 9002, "ZoneRef": "S-01", "Poids": 35, "Revenu": 42000},
)
_HOUSING_BY_SECTOR = (
    ("S-01", 120), ("S-02", 80), ("S-03", 45),
    ("S-04", 30), ("S-05", 200), ("S-06", 12),
)


def _assert_unsupported(test, expression, *reason_words):
    """A BLANK alone is not an explicit, auditable unsupported result."""
    test.assertRegex(
        expression,
        r"(?s)^/\*\s*TODO: DAX conversion validation failed\b.+\*/\s*BLANK\(\)\s*$",
    )
    for word in reason_words:
        test.assertIn(word.lower(), expression.lower(), expression)


def _verified_joined_scope():
    # This is an explicit assumption supplied by the semantic owner.  The
    # fixture does not establish that an actual physical join was materialized.
    return {
        "table": "Joined",
        "source_row_bag_verified": True,
        "filter_context_verified": True,
        "comparison_policy_verified": True,
        "numeric_result_verified": True,
        "shelf_aggregation": "AGG",
        "total_grain": "source_rows",
    }


def _joined_s01():
    # Facture is already materialized in this narrowly scoped row-bag fixture.
    facture = 900 * 60.4 / 120
    return [dict(household, Secteur="S-01", Facture=facture)
            for household in _S01_HOUSEHOLDS]


class _EmittedDax:
    """Strict interpreter for the scalar/iterator subset used by this file.

    It models DAX's loose BLANK comparison and boolean coercion deliberately;
    otherwise a test could mistakenly validate a literal Tableau translation.
    Only named tables, physical columns, VAR, a few scalar calls and SUMX
    are supported.  Unsupported tokens, functions, types and scopes raise.
    """

    _token = re.compile(
        r'\s+|\'(?:[^\']|\'\')*\'|\[(?:[^\]]|\]\])+\]|'
        r'"(?:[^"]|"")*"|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|'
        r'[A-Za-z_]\w*|==|<>|<=|>=|&&|\|\||[()+\-*,=<>]'
    )
    _precedence = {
        "||": 10, "&&": 20,
        "=": 30, "==": 30, "<>": 30, "<": 30, "<=": 30,
        ">": 30, ">=": 30,
        "+": 40, "-": 40, "*": 50,
    }
    _arities = {
        "BLANK": (0,), "TRUE": (0,), "FALSE": (0,),
        "IF": (2, 3), "ISBLANK": (1,), "NOT": (1,),
        "AND": (2,), "OR": (2,), "DIVIDE": (2,),
        "SUM": (1,), "AVERAGE": (1,), "MIN": (1,), "MAX": (1,),
        "SUMX": (2,),
    }

    def __init__(self, expression):
        self.tokens = []
        end = 0
        for match in self._token.finditer(expression):
            if match.start() != end:
                raise AssertionError("Unsupported DAX token: " + expression[end:])
            end = match.end()
            if not match.group().isspace():
                self.tokens.append(match.group())
        if end != len(expression):
            raise AssertionError("Unsupported DAX suffix: " + expression[end:])
        self.position = 0
        self.tree = self._parse()
        if self.position != len(self.tokens):
            raise AssertionError("Unconsumed DAX: " + str(self.tokens[self.position:]))

    def _peek(self):
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _take(self, expected=None):
        token = self._peek()
        if token is None or (expected is not None and token.upper() != expected):
            raise AssertionError(f"Expected {expected!r}, got {token!r}")
        self.position += 1
        return token

    def _parse(self, minimum=0):
        token = self._take()
        upper = token.upper()
        if token == "(":
            node = self._parse()
            self._take(")")
        elif upper == "VAR":
            assignments = []
            while True:
                name = self._take()
                if not re.fullmatch(r"[A-Za-z_]\w*", name):
                    raise AssertionError("Unsupported DAX variable: " + name)
                self._take("=")
                assignments.append((name.casefold(), self._parse()))
                if (self._peek() or "").upper() != "VAR":
                    break
                self._take("VAR")
            self._take("RETURN")
            node = ("let", assignments, self._parse())
        elif token in ("+", "-"):
            node = ("unary", token, self._parse(60))
        elif token.startswith("'"):
            name = token[1:-1].replace("''", "'")
            if (self._peek() or "").startswith("["):
                column = self._take()[1:-1].replace("]]", "]")
                node = ("column", name, column)
            else:
                node = ("table", name)
        elif token.startswith('"'):
            node = ("value", token[1:-1].replace('""', '"'))
        elif re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", token):
            node = ("value", float(token))
        elif token.startswith("["):
            raise AssertionError("Unqualified DAX references are outside the oracle")
        elif self._peek() == "(":
            if upper not in self._arities:
                raise AssertionError("Unsupported DAX function: " + token)
            self._take("(")
            args = []
            if self._peek() != ")":
                while True:
                    args.append(self._parse())
                    if self._peek() != ",":
                        break
                    self._take(",")
            self._take(")")
            if len(args) not in self._arities[upper]:
                raise AssertionError("Unsupported DAX arity: " + token)
            node = ("call", upper, args)
        elif upper in ("TRUE", "FALSE"):
            node = ("value", upper == "TRUE")
        else:
            node = ("variable", token.casefold())

        while self._peek() in self._precedence:
            operator = self._peek()
            priority = self._precedence[operator]
            if priority < minimum:
                break
            self._take()
            node = ("binary", operator, node, self._parse(priority + 1))
        return node

    @staticmethod
    def _boolean(value):
        if value is None:
            return False  # DAX logical operators coerce BLANK to FALSE.
        if type(value) is not bool:
            raise AssertionError("Oracle requires a boolean or BLANK")
        return value

    @staticmethod
    def _numeric(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AssertionError("Oracle requires a numeric value")
        return value

    @classmethod
    def _comparison(cls, operator, left, right):
        if operator == "==" and (left is None or right is None):
            return left is None and right is None
        if left is None:
            left = "" if isinstance(right, str) else False if type(right) is bool else 0
        if right is None:
            right = "" if isinstance(left, str) else False if type(left) is bool else 0
        if type(left) is bool or type(right) is bool:
            if type(left) is not bool or type(right) is not bool:
                raise AssertionError("Unsupported mixed DAX comparison")
        elif isinstance(left, str) or isinstance(right, str):
            if not isinstance(left, str) or not isinstance(right, str):
                raise AssertionError("Unsupported mixed DAX comparison")
            left, right = left.casefold(), right.casefold()
        else:
            cls._numeric(left)
            cls._numeric(right)
        return {
            "=": lambda: left == right, "==": lambda: left == right,
            "<>": lambda: left != right, "<": lambda: left < right,
            "<=": lambda: left <= right, ">": lambda: left > right,
            ">=": lambda: left >= right,
        }[operator]()

    def evaluate(self, tables, *, row=None):
        def run(node, variables, row_context):
            kind = node[0]
            if kind == "value":
                return node[1]
            if kind == "variable":
                if node[1] not in variables:
                    raise AssertionError("Unknown DAX variable: " + node[1])
                return variables[node[1]]
            if kind == "column":
                if node[1] not in row_context:
                    raise AssertionError("Physical column without row context: " + node[1])
                return row_context[node[1]][node[2]]
            if kind == "table":
                raise AssertionError("Table value outside a supported iterator")
            if kind == "let":
                local = dict(variables)
                for name, value in node[1]:
                    local[name] = run(value, local, row_context)
                return run(node[2], local, row_context)
            if kind == "unary":
                value = run(node[2], variables, row_context)
                if value is None:
                    return None
                value = self._numeric(value)
                return value if node[1] == "+" else -value
            if kind == "binary":
                operator = node[1]
                left = run(node[2], variables, row_context)
                if operator == "&&":
                    return self._boolean(left) and self._boolean(
                        run(node[3], variables, row_context))
                if operator == "||":
                    return self._boolean(left) or self._boolean(
                        run(node[3], variables, row_context))
                right = run(node[3], variables, row_context)
                if operator in ("=", "==", "<>", "<", "<=", ">", ">="):
                    return self._comparison(operator, left, right)
                if operator == "*" and (left is None or right is None):
                    return None
                if left is None and right is None:
                    return None
                left = 0 if left is None else self._numeric(left)
                right = 0 if right is None else self._numeric(right)
                return {"+": lambda: left + right, "-": lambda: left - right,
                        "*": lambda: left * right}[operator]()

            if kind != "call":
                raise AssertionError("Unsupported DAX node: " + kind)
            name, args = node[1:]
            if name == "BLANK":
                return None
            if name in ("TRUE", "FALSE"):
                return name == "TRUE"
            if name == "IF":
                condition = self._boolean(run(args[0], variables, row_context))
                if condition:
                    return run(args[1], variables, row_context)
                return run(args[2], variables, row_context) if len(args) == 3 else None
            if name == "ISBLANK":
                return run(args[0], variables, row_context) is None
            if name == "NOT":
                return not self._boolean(run(args[0], variables, row_context))
            if name in ("AND", "OR"):
                left = self._boolean(run(args[0], variables, row_context))
                right = self._boolean(run(args[1], variables, row_context))
                return left and right if name == "AND" else left or right
            if name == "DIVIDE":
                numerator = run(args[0], variables, row_context)
                denominator = run(args[1], variables, row_context)
                if numerator is None or denominator is None or denominator == 0:
                    return None
                return self._numeric(numerator) / self._numeric(denominator)
            if name in ("SUM", "AVERAGE", "MIN", "MAX"):
                if args[0][0] != "column":
                    raise AssertionError("Oracle requires an aggregate column")
                _, table, column = args[0]
                values = [self._numeric(item[column]) for item in tables[table]
                          if item[column] is not None]
                if not values:
                    return None
                if name == "SUM":
                    return sum(values)
                if name == "AVERAGE":
                    return sum(values) / len(values)
                return min(values) if name == "MIN" else max(values)
            if name == "SUMX":
                if args[0][0] != "table":
                    raise AssertionError("Oracle requires a named SUMX row bag")
                table = args[0][1]
                values = []
                for item in tables[table]:
                    current = dict(row_context, **{table: item})
                    value = run(args[1], variables, current)
                    if value is not None:
                        values.append(self._numeric(value))
                return sum(values) if values else None
            raise AssertionError("Unsupported DAX function: " + name)

        return run(self.tree, {}, row or {})


class TestBoundedDaxOracle(unittest.TestCase):
    """Controls for the interpreter; these do not execute Power BI."""

    def test_loose_blank_comparison_and_boolean_coercion_are_dax_like(self):
        for expression, expected in (
            ("BLANK() = 0", True),
            ("BLANK() == 0", False),
            ("BLANK() < 1", True),
            ("NOT(BLANK())", True),
            ("BLANK() && TRUE()", False),
            ("BLANK() || TRUE()", True),
            ("ISBLANK(0)", False),
        ):
            with self.subTest(expression=expression):
                self.assertIs(_EmittedDax(expression).evaluate({}), expected)
        self.assertIsNone(_EmittedDax("2 * BLANK()").evaluate({}))

    def test_iterator_preserves_duplicates_and_blank_empty_sum(self):
        expression = ("SUMX('Joined', VAR x = 'Joined'[Poids] "
                      "RETURN IF(ISBLANK(x), 0, x))")
        oracle = _EmittedDax(expression)
        self.assertEqual(oracle.evaluate(
            {"Joined": [{"Poids": 2}, {"Poids": 2}, {"Poids": None}]}), 4)
        self.assertIsNone(oracle.evaluate({"Joined": []}))

    def test_if_evaluates_only_the_chosen_branch(self):
        expression = "IF(TRUE(), 4, 'Missing'[Value])"
        self.assertEqual(_EmittedDax(expression).evaluate({}), 4)

    def test_unsupported_syntax_does_not_silently_return_a_value(self):
        for expression in (
            "UNKNOWN(1)",
            "IF(TRUE(), 1, UNKNOWN(2))",
            "SUMX(ALL('Joined'), 1)",
            "1 / 2",
            "/* TODO: unresolved */ BLANK()",
            "1 trailing",
        ):
            with self.subTest(expression=expression):
                with self.assertRaises(AssertionError):
                    _EmittedDax(expression).evaluate({"Joined": []})


class TestReportedRowGrain(unittest.TestCase):
    """Defect 3: row-wise conditional aggregation versus IF of aggregates."""

    _row_formula = "IF [Facture] > 0.02 * [Revenu] THEN [Poids] ELSE 0 END"

    def test_unproven_cross_table_row_measure_is_an_explicit_diagnostic(self):
        expression = convert_tableau_formula_to_dax(
            self._row_formula, table_name="Zone", column_table_map=_COLUMN_OWNERS,
            table_columns=_ZONE_COLUMNS)
        _assert_unsupported(self, expression, "row", "scope", "grain")
        self.assertNotRegex(expression, r"(?i)\b(?:MAX|SUM|SUMX)\s*\(")

    def test_cross_table_row_aggregation_cannot_be_proved_by_one_table_flag(self):
        scope = dict(_verified_joined_scope(), table="Zone")
        expression = convert_tableau_formula_to_dax(
            f"SUM({self._row_formula})", table_name="Zone",
            column_table_map=_COLUMN_OWNERS, table_columns=_ZONE_COLUMNS,
            verified_row_scope=scope)
        _assert_unsupported(self, expression, "spans tables", "row bag")
        self.assertNotRegex(expression, r"(?i)\b(?:MAX|SUM|SUMX)\s*\(")

    def test_incomplete_or_wrong_single_table_evidence_is_not_a_proof(self):
        valid = _verified_joined_scope()
        variants = [("absent", None)]
        for key in ("source_row_bag_verified", "filter_context_verified",
                    "comparison_policy_verified", "numeric_result_verified"):
            missing = dict(valid)
            del missing[key]
            variants.append(("missing " + key, missing))
            variants.append(("false " + key, dict(valid, **{key: False})))
        variants.extend((
            ("wrong table", dict(valid, table="Zone")),
            ("wrong shelf", dict(valid, shelf_aggregation="SUM")),
            ("wrong total", dict(valid, total_grain="visual_marks")),
            ("non-boolean evidence", dict(valid, source_row_bag_verified=1)),
        ))
        for label, scope in variants:
            with self.subTest(evidence=label):
                expression = convert_tableau_formula_to_dax(
                    f"SUM({self._row_formula})", table_name="Joined",
                    table_columns=["Facture", "Revenu", "Poids"],
                    verified_row_scope=scope)
                _assert_unsupported(self, expression, "row", "grain")

    def test_verified_materialized_row_bag_evaluates_corrected_s01_thresholds(self):
        rows = _joined_s01()
        self.assertEqual(rows[0]["Facture"], 453)
        for threshold, expected in (("0.08", 0), ("0.02", 40)):
            with self.subTest(threshold=threshold):
                source = (f"SUM(IF [Facture] > {threshold} * [Revenu] "
                          "THEN [Poids] ELSE 0 END)")
                expression = convert_tableau_formula_to_dax(
                    source, table_name="Joined",
                    table_columns=["Facture", "Revenu", "Poids"],
                    verified_row_scope=_verified_joined_scope())
                self.assertNotIn(_FALLBACK, expression)
                self.assertRegex(expression, r"(?i)^\s*SUMX\s*\(")
                self.assertEqual(_EmittedDax(expression).evaluate({"Joined": rows}),
                                 expected)

        # The reported IF-on-aggregates happens to agree at 0.08, but returns
        # 0 rather than 40 at 0.02.  This evaluates that bad DAX as a control.
        bad = ("IF(MAX('Zone'[Facture]) > 0.02 * SUM('Menage'[Revenu]), "
               "SUM('Menage'[Poids]), 0)")
        self.assertEqual(_EmittedDax(bad).evaluate({
            "Zone": [{"Facture": 453}], "Menage": list(_S01_HOUSEHOLDS)}), 0)

    def test_verified_iterator_preserves_duplicate_rows_and_current_scope(self):
        source = (f"SUM({self._row_formula})")
        expression = convert_tableau_formula_to_dax(
            source, table_name="Joined", table_columns=["Facture", "Revenu", "Poids"],
            verified_row_scope=_verified_joined_scope())
        self.assertNotIn(_FALLBACK, expression)
        oracle = _EmittedDax(expression)
        s01 = _joined_s01()
        s02 = {"Secteur": "S-02", "Facture": 610 * 60.4 / 80,
               "Poids": 25, "Revenu": 15000}
        self.assertEqual(oracle.evaluate({"Joined": s01 + [s02]}), 65)
        self.assertEqual(oracle.evaluate({"Joined": s01}), 40)
        self.assertEqual(oracle.evaluate({"Joined": [s01[1]]}), 0)
        self.assertEqual(oracle.evaluate({"Joined": s01 + [dict(s01[0])]}), 80)
        self.assertEqual(oracle.evaluate({"Joined": s01 + [
            {"Facture": 453, "Revenu": None, "Poids": 100},
            {"Facture": None, "Revenu": 18000, "Poids": 100},
        ]}), 40)
        self.assertIsNone(oracle.evaluate({"Joined": []}))

    def test_verified_single_table_calculated_column_stays_row_scoped(self):
        expression = convert_tableau_formula_to_dax(
            self._row_formula, table_name="Joined",
            table_columns=["Facture", "Revenu", "Poids"], is_calc_column=True)
        self.assertNotIn(_FALLBACK, expression)
        oracle = _EmittedDax(expression)
        rows = _joined_s01()
        self.assertEqual(
            [oracle.evaluate({"Joined": rows}, row={"Joined": item}) for item in rows],
            [40, 0],
        )

    def test_source_if_over_aggregates_remains_an_aggregate_calculation(self):
        source = ("IF AVG([Facture]) > 0.02 * SUM([Revenu]) "
                  "THEN SUM([Poids]) ELSE 0 END")
        expression = convert_tableau_formula_to_dax(
            source, table_name="Zone", column_table_map=_COLUMN_OWNERS,
            table_columns=_ZONE_COLUMNS)
        self.assertNotIn(_FALLBACK, expression)
        self.assertIn("AVERAGE('Zone'[Facture])", expression)
        self.assertIn("SUM('Menage'[Revenu])", expression)
        self.assertNotRegex(expression, r"(?i)\bSUMX\s*\(")
        self.assertEqual(_EmittedDax(expression).evaluate({
            "Zone": [{"Facture": 453}], "Menage": list(_S01_HOUSEHOLDS)}), 0)

    def test_nullable_string_functions_in_conditions_propagate_blank(self):
        cases = (
              ("IF LEFT([Code], 1) = \"X\" THEN 1 ELSE 0 END",
               "LEFT(__TableauNullable"),
            ("IF RIGHT([Code], 1) = \"X\" THEN 1 ELSE 0 END",
             "RIGHT(__TableauNullable"),
              ("IF MID([Code], 1, 1) = \"X\" THEN 1 ELSE 0 END",
               "MID(__TableauNullable"),
              ("IF UPPER([Code]) = \"X\" THEN 1 ELSE 0 END",
               "UPPER(__TableauNullable"),
            ("IF LOWER([Code]) = \"x\" THEN 1 ELSE 0 END",
             "LOWER(__TableauNullable"),
              ("IF TRIM([Code]) = \"X\" THEN 1 ELSE 0 END",
               "TRIM(__TableauNullable"),
              ("IF LEN([Code]) = 1 THEN 1 ELSE 0 END",
               "LEN(__TableauNullable"),
            ("IF STR([Amount]) = \"1\" THEN 1 ELSE 0 END",
             'FORMAT(__TableauNullable'),
        )
        for formula, expected_call in cases:
            with self.subTest(formula=formula):
                expression = convert_tableau_formula_to_dax(
                    formula, table_name="T", table_columns={"Code", "Amount"},
                    column_table_map={"Code": "T", "Amount": "T"},
                    is_calc_column=True, calc_datatype="real",
                    validate_output=True, fallback_on_invalid=True)
                self.assertNotIn(_FALLBACK, expression)
                self.assertIn(expected_call, expression)
                self.assertIn("RETURN IF(ISBLANK(", expression)
                self.assertIn("ISBLANK", expression)
                self.assertIn("BLANK()", expression)

    def test_nullable_string_functions_reject_unsupported_arity(self):
        for formula in (
            "IF RIGHT([Code]) = \"X\" THEN 1 ELSE 0 END",
            "IF MID([Code], 1) = \"X\" THEN 1 ELSE 0 END",
            "IF LOWER([Code], [Code]) = \"X\" THEN 1 ELSE 0 END",
        ):
            with self.subTest(formula=formula):
                expression = convert_tableau_formula_to_dax(
                    formula, table_name="T", table_columns={"Code"},
                    column_table_map={"Code": "T"}, is_calc_column=True,
                    calc_datatype="real", validate_output=True,
                    fallback_on_invalid=True)
                _assert_unsupported(self, expression, "grammar")


class TestReportedFixedGrainStatic(unittest.TestCase):
    """Defects 4--5: exact, annotated approximation, or diagnostic fallback."""

    def test_include_compiles_at_explicit_shelf_grain(self):
        expression = convert_tableau_formula_to_dax(
            "{ INCLUDE [Region] : SUM([Sales]) }",
            table_name="Fact",
            table_columns={"Region", "Sales"},
            numeric_columns={"Sales"},
            column_table_map={"Region": "Fact", "Sales": "Fact"},
            lod_shelf_aggregation="SUM")
        self.assertEqual(
            expression,
            "/* shelf (sum:) INCLUDE approximation; source filters and totals "
            "unverified */ SUMX(VALUES('Fact'[Region]), "
            "CALCULATE(SUM('Fact'[Sales])))")

    def test_include_expression_uses_row_iterator_for_physical_product(self):
        from powerbi_import.dax_validator import validate_dax_expression

        expression = convert_tableau_formula_to_dax(
            "{ INCLUDE [Channel] : SUM([Quantity] * [UnitPrice]) }",
            table_name="Fact",
            table_columns={"Channel", "Quantity", "UnitPrice"},
            numeric_columns={"Quantity", "UnitPrice"},
            column_table_map={
                "Channel": "Fact", "Quantity": "Fact", "UnitPrice": "Fact"},
            lod_shelf_aggregation="SUM", validate_output=True)
        self.assertIn("shelf (sum:) INCLUDE approximation", expression)
        self.assertIn("SUMX(VALUES('Fact'[Channel])", expression)
        self.assertIn("CALCULATE(SUMX('Fact',", expression)
        self.assertEqual(validate_dax_expression(expression), [])

    def test_include_preserves_every_physical_key(self):
        expression = convert_tableau_formula_to_dax(
            "{ INCLUDE [Region], [Category] : SUM([Sales]) }",
            table_name="Fact",
            table_columns={"Region", "Category", "Sales"},
            numeric_columns={"Sales"},
            column_table_map={
                "Region": "Fact", "Category": "Fact", "Sales": "Fact"},
            lod_shelf_aggregation="SUM")
        self.assertEqual(
            expression,
            "/* shelf (sum:) INCLUDE approximation; source filters and totals "
            "unverified */ SUMX(SUMMARIZE('Fact', 'Fact'[Region], "
            "'Fact'[Category]), CALCULATE(SUM('Fact'[Sales])))")

    def test_exclude_removes_all_physical_keys(self):
        expression = convert_tableau_formula_to_dax(
            "{ EXCLUDE [Region], [Category] : SUM([Sales]) }",
            table_name="Fact",
            table_columns={"Region", "Category", "Sales"},
            numeric_columns={"Sales"},
            column_table_map={
                "Region": "Fact", "Category": "Fact", "Sales": "Fact"})
        self.assertEqual(
            expression,
            "/* EXCLUDE dimension-filter approximation; totals unverified */ "
            "CALCULATE(SUM('Fact'[Sales]), REMOVEFILTERS('Fact'[Region], "
            "'Fact'[Category]))")

    def test_exclude_removes_only_a_local_physical_dimension(self):
        expression = convert_tableau_formula_to_dax(
            "{ EXCLUDE [Region] : SUM([Sales]) }",
            table_name="Fact",
            table_columns={"Region", "Sales"},
            numeric_columns={"Sales"},
            column_table_map={"Region": "Fact", "Sales": "Fact"})
        self.assertEqual(
            expression,
            "/* EXCLUDE dimension-filter approximation; totals unverified */ "
            "CALCULATE(SUM('Fact'[Sales]), REMOVEFILTERS('Fact'[Region]))")

    def test_include_exclude_fail_closed_without_proven_grain(self):
        cases = (
            ("{ INCLUDE [Region] : SUM([Sales]) }", {
                "lod_shelf_aggregation": None}),
            ("SUM({ INCLUDE [Region] : SUM([Sales]) })", {
                "lod_shelf_aggregation": "AVG"}),
            ("{ INCLUDE [Region] : SUM([Sales]) }", {
                "lod_shelf_aggregation": "SUM", "numeric_columns": {"Region"}}),
            ("{ INCLUDE [Region] : SUM([Sales]) }", {
                "lod_shelf_aggregation": "SUM",
                "column_table_map": {"Region": "Other", "Sales": "Fact"}}),
            ("{ INCLUDE [Region], [Region] : SUM([Sales]) }", {}),
            ("{ EXCLUDE [Region] : SUM([Sales]) }", {
                "column_table_map": {"Region": "Other", "Sales": "Fact"}}),
            ("SUM({ EXCLUDE [Region] : SUM([Sales]) })", {}),
        )
        for formula, overrides in cases:
            with self.subTest(formula=formula, overrides=overrides):
                options = {
                    "table_name": "Fact",
                    "table_columns": {"Region", "Sales"},
                    "numeric_columns": {"Sales"},
                    "column_table_map": {"Region": "Fact", "Sales": "Fact"},
                    "lod_shelf_aggregation": "SUM",
                }
                options.update(overrides)
                _assert_unsupported(
                    self, convert_tableau_formula_to_dax(formula, **options),
                    "INCLUDE" if "INCLUDE" in formula else "EXCLUDE")

    @staticmethod
    def _fixed_scope():
        return {
            "table": "Housing", "fixed_dimensions": ["Sector"],
            "value_column": "Homes", "physical_fields": ["Sector", "Homes"],
            "source_filters": [], "target_filters": [],
            "filter_policy": "target_context_equals_fixed_input_per_key",
            "view_grains": [["Sector"], []], "total_grain": [],
            "total_aggregation": "SUM_DISTINCT_FIXED_KEYS",
            "shelf_aggregation": "SUM", "value_type": "numeric",
            "source_row_bag_verified": True, "physical_fields_verified": True,
            "fixed_key_domain_verified": True, "filter_equivalence_verified": True,
            "group_key_and_blank_equivalence_verified": True,
            "numeric_and_null_equivalence_verified": True,
            "all_target_uses_and_totals_verified": True, "engine_support_verified": True,
        }

    def test_fixed_four_shelves_accept_bare_and_matching_outer_aggregate(self):
        from powerbi_import.dax_validator import validate_dax_expression

        fixed = "{ FIXED [Sector] : MIN([Homes]) }"
        for shelf, iterator in (("SUM", "SUMX"), ("AVG", "AVERAGEX"),
                                ("MIN", "MINX"), ("MAX", "MAXX")):
            for formula in (fixed, f"{shelf}({fixed})"):
                with self.subTest(shelf=shelf, formula=formula):
                    expression = convert_tableau_formula_to_dax(
                        formula, table_name="Housing",
                        table_columns=["Sector", "Homes"],
                        column_table_map={"Sector": "Housing", "Homes": "Housing"},
                        fixed_shelf_aggregation=shelf, validate_output=True,
                        fallback_on_invalid=True)
                    self.assertEqual(
                        expression,
                        f"/* shelf ({shelf.lower()}:) approximation; source filter "
                        f"scope and totals unverified */ {iterator}("
                        "VALUES('Housing'[Sector]), CALCULATE(MIN('Housing'[Homes])))")
                    self.assertEqual(validate_dax_expression(expression), [])

    def test_fixed_multiple_physical_keys_use_summarize(self):
        expression = convert_tableau_formula_to_dax(
            "{ FIXED [Sector], [Category] : AVG([Homes]) }",
            table_name="Housing", table_columns=["Sector", "Category", "Homes"],
            fixed_shelf_aggregation="SUM")
        self.assertIn("shelf (sum:) approximation", expression)
        self.assertTrue(expression.endswith(
            "SUMX(SUMMARIZE('Housing', 'Housing'[Sector], 'Housing'[Category]), "
            "CALCULATE(AVERAGE('Housing'[Homes])))"), expression)
        self.assertNotIn("ALLEXCEPT", expression)

    def test_fixed_missing_unknown_or_mismatched_shelf_is_unsupported(self):
        fixed = "{ FIXED [Sector] : MIN([Homes]) }"
        for formula, shelf in ((fixed, None), (fixed, "ATTR"), (fixed, "MEDIAN"),
                               (fixed, ""), (fixed, 1), (f"AVG({fixed})", "SUM")):
            with self.subTest(formula=formula, shelf=shelf):
                expression = convert_tableau_formula_to_dax(
                    formula, table_name="Housing", table_columns=["Sector", "Homes"],
                    fixed_shelf_aggregation=shelf)
                _assert_unsupported(self, expression, "FIXED")
                self.assertNotIn("ALLEXCEPT", expression)
                self.assertNotIn("approximation;", expression)

    def test_fixed_shelf_requires_physical_single_table_keys_and_direct_inner(self):
        fixed = "{ FIXED [Sector] : MIN([Homes]) }"
        cases = (
            (fixed, {"table_columns": None}),
            (fixed, {"table_columns": ["Homes"]}),
            (fixed, {"column_table_map": {"Sector": "OtherHousing"}}),
            (fixed, {"column_table_map": {"Homes": "OtherHousing"}}),
            (fixed, {"measure_names": {"Sector"}}),
            (fixed, {"measure_names": {"Homes"}}),
            (fixed, {"is_calc_column": True}),
            ("{ FIXED [OtherHousing].[Sector] : MIN([Homes]) }", {}),
            ("{ FIXED [Sector], [Sector] : MIN([Homes]) }", {}),
            ("{ FIXED [Sector] : MIN([Homes] + 1) }", {}),
            ("{ FIXED [Sector] : MIN([Homes]) / SUM([Homes]) }", {}),
            ("{ FIXED [Sector] : [Homes] }", {}),
        )
        for formula, overrides in cases:
            with self.subTest(formula=formula, overrides=overrides):
                options = {"table_name": "Housing",
                           "table_columns": ["Sector", "Homes"],
                           "fixed_shelf_aggregation": "SUM"}
                options.update(overrides)
                expression = convert_tableau_formula_to_dax(formula, **options)
                _assert_unsupported(self, expression, "FIXED", "physical")
                self.assertNotIn("ALLEXCEPT", expression)

    def test_fixed_unresolved_original_foreign_qualification_is_not_local(self):
        expression = convert_tableau_formula_to_dax(
            "{ FIXED [federated.foreign].[Sector] : MIN([Homes]) }",
            table_name="Housing", table_columns=["Sector", "Homes"],
            column_table_map={"Sector": "Housing", "Homes": "Housing"},
            fixed_shelf_aggregation="SUM")
        _assert_unsupported(self, expression, "FIXED", "physical")
        self.assertNotIn("approximation;", expression)
        self.assertNotIn("SUMX(", expression)

    def test_fixed_explicit_same_table_qualification_still_approximates(self):
        expression = convert_tableau_formula_to_dax(
            "{ FIXED [Housing].[Sector] : MIN([Housing].[Homes]) }",
            table_name="Housing", table_columns=["Sector", "Homes"],
            column_table_map={"Sector": "Housing", "Homes": "Housing"},
            fixed_shelf_aggregation="SUM")
        self.assertEqual(expression,
            "/* shelf (sum:) approximation; source filter scope and totals "
            "unverified */ SUMX(VALUES('Housing'[Sector]), "
            "CALCULATE(MIN('Housing'[Homes])))")

    def test_fixed_exact_proof_takes_precedence_over_shelf_approximation(self):
        fixed = "{ FIXED [Sector] : MIN([Homes]) }"
        for outer in (False, True):
            with self.subTest(outer=outer):
                scope = self._fixed_scope()
                if outer:
                    scope["shelf_aggregation"] = "AGG"
                options = {"table_name": "Housing",
                           "table_columns": ["Sector", "Homes"],
                           "verified_fixed_scope": scope, "validate_output": True}
                formula = f"SUM({fixed})" if outer else fixed
                exact = convert_tableau_formula_to_dax(formula, **options)
                with_shelf = convert_tableau_formula_to_dax(
                    formula, fixed_shelf_aggregation="SUM", **options)
                self.assertEqual(exact, with_shelf)
                self.assertEqual(exact,
                    "SUMX(SUMMARIZE('Housing', 'Housing'[Sector]), "
                    "CALCULATE(MIN('Housing'[Homes])))")
                self.assertNotIn("unverified", with_shelf)

    def test_fixed_incomplete_exact_evidence_never_silently_claims_exactness(self):
        proof = self._fixed_scope()
        for missing in proof:
            for shelf in (None, "SUM"):
                with self.subTest(missing=missing, shelf=shelf):
                    scope = deepcopy(proof)
                    del scope[missing]
                    expression = convert_tableau_formula_to_dax(
                        "{ FIXED [Sector] : MIN([Homes]) }", table_name="Housing",
                        table_columns=["Sector", "Homes"], verified_fixed_scope=scope,
                        fixed_shelf_aggregation=shelf)
                    if shelf is None:
                        _assert_unsupported(self, expression, "FIXED", "grain")
                    else:
                        self.assertIn("shelf (sum:) approximation; source filter "
                                      "scope and totals unverified", expression)
                        self.assertIn("SUMX(VALUES('Housing'[Sector])", expression)

    def test_report_reference_total_is_not_a_dax_execution(self):
        # This is just the report's synthetic reference arithmetic.  It does
        # not claim that any emitted expression or native engine returned 487.
        per_sector = {sector: housing for sector, housing in _HOUSING_BY_SECTOR}
        self.assertEqual(sum(per_sector.values()), 487)
        self.assertEqual(min(per_sector.values()), 12)

    def test_unproven_fixed_and_its_outer_aggregate_are_not_allexcept_guesses(self):
        fixed = "{ FIXED [Secteur] : MIN([Logements]) }"
        for source in (fixed, f"SUM({fixed})", f"AVG({fixed})"):
            with self.subTest(source=source):
                expression = convert_tableau_formula_to_dax(
                    source, table_name="Zone", column_table_map=_COLUMN_OWNERS,
                    table_columns=_ZONE_COLUMNS)
                _assert_unsupported(self, expression, "FIXED", "filter", "grain",
                                    "aggregation", "total")
                self.assertNotIn("ALLEXCEPT", expression.upper())

    def test_multitable_fixed_does_not_remove_its_own_dimensions(self):
        for dimensions in ("[Secteur], [Categorie]", "[Categorie], [Secteur]"):
            with self.subTest(dimensions=dimensions):
                source = (f"{{ FIXED {dimensions} : "
                          "SUM([EstAuDessus]) / SUM([Poids]) }")
                expression = convert_tableau_formula_to_dax(
                    source, table_name="Zone", column_table_map=_COLUMN_OWNERS,
                    measure_names={"EstAuDessus"}, table_columns=_ZONE_COLUMNS)
                _assert_unsupported(self, expression, "FIXED", "filter", "grain")
                # No silent EXCLUDE-like removal of either FIXED dimension.
                self.assertNotIn("REMOVEFILTERS", expression.upper())

    def test_aggregating_a_fixed_measure_is_not_silently_unwrapped(self):
        expression = convert_tableau_formula_to_dax(
            "SUM([Logements par secteur])", table_name="Zone",
            measure_names={"Logements par secteur"}, table_columns=_ZONE_COLUMNS)
        _assert_unsupported(self, expression, "aggregation", "shelf", "grain")
        self.assertNotEqual(expression, "[Logements par secteur]")

        table = {
            "name": "Zone",
            "columns": [{"name": "Logements"}],
            "measures": [
                {"name": "Logements par secteur", "expression": "1"},
                {"name": "Total", "expression": "SUM([Logements par secteur])"},
                {"name": "Physical sum", "expression": "SUM('Zone'[Logements])"},
            ],
        }
        _unwrap_aggregations_of_measures(table)
        measures = {measure["name"]: measure["expression"]
                    for measure in table["measures"]}
        _assert_unsupported(self, measures["Total"], "aggregation", "shelf", "grain")
        self.assertEqual(measures["Physical sum"], "SUM('Zone'[Logements])")

    def test_dependent_measure_does_not_hide_an_unsupported_fixed_as_a_number(self):
        fixed = convert_tableau_formula_to_dax(
            "{ FIXED [Secteur] : MIN([Logements]) }", table_name="Zone",
            column_table_map=_COLUMN_OWNERS, table_columns=_ZONE_COLUMNS)
        table = {
            "name": "Zone", "columns": [],
            "measures": [
                {"name": "Fixed", "expression": fixed},
                {"name": "Derived", "expression": "[Fixed] + 1"},
                {"name": "Further", "expression": "[Derived] + 2"},
                {"name": "Independent", "expression": "SUM('Zone'[Logements])"},
            ],
        }
        _validate_measures_after_rewrites(table)
        measures = {measure["name"]: measure["expression"]
                    for measure in table["measures"]}
        _assert_unsupported(self, measures["Fixed"], "FIXED", "grain")
        _assert_unsupported(self, measures["Derived"], "depends", "unsupported")
        _assert_unsupported(self, measures["Further"], "depends", "unsupported")
        self.assertEqual(measures["Independent"], "SUM('Zone'[Logements])")


class TestReportedNullableLogic(unittest.TestCase):
    """Defect 6: evaluate emitted DAX with a bounded BLANK-aware oracle."""

    def _convert(self, source):
        expression = convert_tableau_formula_to_dax(
            source, table_name="Zone", column_table_map=_COLUMN_OWNERS,
            table_columns=_ZONE_COLUMNS)
        self.assertNotIn(_FALLBACK, expression, expression)
        return _EmittedDax(expression)

    def test_report_indice_s06_null_is_bas_but_real_zero_is_not_null(self):
        source = (
            'IF AVG([Logements]) > 2*SUM([Poids]) THEN "haut" '
            'ELSEIF AVG([Logements]) > 1*SUM([Poids]) '
            'AND AVG([Logements]) < 2*SUM([Poids]) THEN "moyen" '
            'ELSE "bas" END'
        )
        oracle = self._convert(source)
        for housing, weights, expected in (
            (12, [], "bas"),
            (12, [None], "bas"),
            (12, [0], "haut"),
            (120, [40, 35], "moyen"),
            (120, [60], "bas"),  # equality to 2 * SUM is not > or <
            (80, [25], "haut"),
        ):
            with self.subTest(housing=housing, weights=weights):
                tables = {"Zone": [{"Logements": housing}],
                          "Menage": [{"Poids": value} for value in weights]}
                self.assertEqual(oracle.evaluate(tables), expected)

        bad = ("IF(AVERAGE('Zone'[Logements]) > 2 * SUM('Menage'[Poids]), "
               '"haut", "bas")')
        self.assertEqual(_EmittedDax(bad).evaluate(
            {"Zone": [{"Logements": 12}], "Menage": []}), "haut")

    def test_comparisons_outside_if_preserve_unknown_versus_zero(self):
        expected = {
            "=": (True, False),
            "<>": (False, True),
            ">": (False, True),
            ">=": (True, True),
            "<": (False, False),
            "<=": (True, False),
        }
        for operator, (zero, positive) in expected.items():
            with self.subTest(operator=operator):
                oracle = self._convert(f"SUM([Poids]) {operator} 0")
                for weights, result in (([], None), ([None], None),
                                        ([0], zero), ([3], positive)):
                    with self.subTest(weights=weights):
                        actual = oracle.evaluate(
                            {"Menage": [{"Poids": value} for value in weights]})
                        self.assertIs(actual, result)

    def test_explicit_null_and_reversed_comparisons_are_not_coerced(self):
        for source in ("NULL = 0", "0 = NULL", "NULL <> NULL",
                       "NULL = NULL", "1 > NULL", "NULL < 1"):
            with self.subTest(source=source):
                self.assertIsNone(self._convert(source).evaluate({}))
        oracle = self._convert("0 < SUM([Poids])")
        self.assertIsNone(oracle.evaluate({"Menage": []}))
        self.assertIs(oracle.evaluate({"Menage": [{"Poids": 0}]}), False)
        self.assertIs(oracle.evaluate({"Menage": [{"Poids": 3}]}), True)

    def test_not_preserves_unknown_instead_of_negating_coerced_blank(self):
        for source in ("NOT (SUM([Poids]) = 0)", "NOT(SUM([Poids]) = 0)"):
            with self.subTest(source=source):
                oracle = self._convert(source)
                self.assertIsNone(oracle.evaluate({"Menage": []}))
                self.assertIs(oracle.evaluate({"Menage": [{"Poids": 0}]}), False)
                self.assertIs(oracle.evaluate({"Menage": [{"Poids": 3}]}), True)

    def test_and_or_follow_three_valued_truth_tables_outside_if(self):
        truth = {
            "AND": {
                (None, None): None, (None, False): False, (None, True): None,
                (False, None): False, (False, False): False, (False, True): False,
                (True, None): None, (True, False): False, (True, True): True,
            },
            "OR": {
                (None, None): None, (None, False): None, (None, True): True,
                (False, None): None, (False, False): False, (False, True): True,
                (True, None): True, (True, False): True, (True, True): True,
            },
        }
        left_values = ((None, None), (0, True), (1, False))
        right_values = ((None, None), (0, False), (1, True))
        for operator in ("AND", "OR"):
            oracle = self._convert(
                f"(SUM([Poids]) = 0) {operator} (AVG([Logements]) > 0)")
            for weight, left in left_values:
                for housing, right in right_values:
                    with self.subTest(operator=operator, left=left, right=right):
                        tables = {"Menage": [{"Poids": weight}],
                                  "Zone": [{"Logements": housing}]}
                        self.assertIs(oracle.evaluate(tables), truth[operator][left, right])

    def test_if_and_iif_handle_unknown_conditions_with_their_own_policies(self):
        ordinary_if = self._convert(
            "IF SUM([Poids]) > 0 THEN 1 ELSE 2 END")
        iif_with_unknown = self._convert(
            "IIF(SUM([Poids]) > 0, 1, 2, 3)")
        iif_without_unknown = self._convert(
            "IIF(SUM([Poids]) > 0, 1, 2)")
        for weights, expected in (
            ([], (2, 3, None)),
            ([0], (2, 2, 2)),
            ([7], (1, 1, 1)),
        ):
            with self.subTest(weights=weights):
                tables = {"Menage": [{"Poids": value} for value in weights]}
                actual = tuple(oracle.evaluate(tables) for oracle in (
                    ordinary_if, iif_with_unknown, iif_without_unknown))
                self.assertEqual(actual, expected)

    def test_nested_not_or_inside_iif_preserves_the_unknown_branch(self):
        oracle = self._convert(
            "IIF(NOT (SUM([Poids]) = 0) OR (AVG([Logements]) < 0), 11, 22, 33)")
        for weight, housing, expected in (
            (None, 1, 33), (0, 1, 22), (2, 1, 11),
            (None, -1, 11), (0, None, 33),
        ):
            with self.subTest(weight=weight, housing=housing):
                self.assertEqual(oracle.evaluate({
                    "Menage": [{"Poids": weight}],
                    "Zone": [{"Logements": housing}],
                }), expected)

    def test_unverified_comparison_operand_is_an_explicit_diagnostic(self):
        # The current bounded contract does not prove ABS's NULL behaviour
        # here.  It must not emit a plausible but unverified IF.
        expression = convert_tableau_formula_to_dax(
            "IF ABS(SUM([Poids])) > 2 THEN 1 ELSE 0 END",
            table_name="Zone", column_table_map=_COLUMN_OWNERS,
            table_columns=_ZONE_COLUMNS)
        _assert_unsupported(self, expression, "nullable", "grammar")

    def test_isnull_remains_a_boolean_test_not_an_unknown_comparison(self):
        oracle = self._convert("ISNULL(SUM([Poids]))")
        self.assertIs(oracle.evaluate({"Menage": []}), True)
        self.assertIs(oracle.evaluate({"Menage": [{"Poids": None}]}), True)
        self.assertIs(oracle.evaluate({"Menage": [{"Poids": 0}]}), False)
        self.assertIs(oracle.evaluate({"Menage": [{"Poids": 3}]}), False)

    def test_null_keywords_in_a_result_literal_are_not_rewritten(self):
        source = ('IF SUM([Poids]) = NULL THEN "NULL OR NOT" '
                  'ELSE "ELSE = NULL" END')
        expression = convert_tableau_formula_to_dax(
            source, table_name="Zone", column_table_map=_COLUMN_OWNERS,
            table_columns=_ZONE_COLUMNS)
        self.assertNotIn(_FALLBACK, expression)
        self.assertIn('"NULL OR NOT"', expression)
        self.assertIn('"ELSE = NULL"', expression)
        oracle = _EmittedDax(expression)
        self.assertEqual(oracle.evaluate({"Menage": []}), "ELSE = NULL")
        self.assertEqual(oracle.evaluate({"Menage": [{"Poids": 0}]}), "ELSE = NULL")


class TestCalculationDependencyIdentity(unittest.TestCase):
    """Source IDs must not be conflated just because captions coincide."""

    def build(self, consumer, *, bad_first):
        table = {
            "name": "Sales",
            "columns": [{"name": "Region", "datatype": "string"},
                        {"name": "Amount", "datatype": "real"}],
        }
        connection = {
            "type": "SQL Server",
            "details": {"server": "synthetic.invalid", "database": "fixture"},
        }
        bad = {
            "name": "[bad]", "caption": "Shared caption", "role": "measure",
            "datatype": "real", "formula": "{ FIXED [Region] : MIN([Amount]) }",
        }
        good = {
            "name": "[good]", "caption": "Shared caption", "role": "measure",
            "datatype": "real", "formula": "SUM([Amount])",
        }
        calculations = ([bad, good] if bad_first else [good, bad]) + [{
            "name": "[consumer]", "caption": "Consumer", "role": "measure",
            "datatype": "real", "formula": consumer,
        }]
        datasource = {
            "name": "Synthetic", "connection": connection,
            "tables": [table], "calculations": calculations,
        }
        return _build_semantic_model(
            [datasource], "Synthetic", calendar_start=2020, calendar_end=2021)

    @staticmethod
    def measures(model):
        return [measure for table in model["model"]["tables"]
                for measure in table.get("measures", [])]

    def test_explicit_good_identity_stays_valid_and_points_to_good_measure(self):
        from powerbi_import.dax_validator import validate_dax_expression

        for bad_first in (True, False):
            with self.subTest(bad_first=bad_first):
                result = self.build("[good] + 1", bad_first=bad_first)
                measures = self.measures(result)
                good = [measure for measure in measures
                        if measure.get("_tableau_calculation_identity", {}).get("name")
                        == "[good]"]
                bad = [measure for measure in measures
                       if measure.get("_tableau_calculation_identity", {}).get("name")
                       == "[bad]"]
                self.assertEqual(len(good), 1, measures)
                self.assertEqual(len(bad), 1, measures)
                self.assertNotEqual(good[0]["name"].casefold(), bad[0]["name"].casefold())
                _assert_unsupported(self, bad[0]["expression"], "FIXED", "grain")
                self.assertRegex(good[0]["expression"], r"(?i)\bSUM\s*\(.*\[Amount\]")
                self.assertNotIn(_FALLBACK, good[0]["expression"])
                consumer = next(m for m in measures if m["name"] == "Consumer")
                expression = consumer["expression"]
                self.assertNotIn(_FALLBACK, expression)
                self.assertEqual(validate_dax_expression(expression), [])
                # Either preserve the valid target identity or inline its
                # aggregate; a reference to the unsupported namesake is wrong.
                good_ref = "[" + good[0]["name"].replace("]", "]]") + "]"
                if good_ref not in expression:
                    self.assertRegex(expression, r"(?i)\bSUM\s*\(.*\[Amount\]")

    def test_explicit_bad_identity_propagates_diagnostic_in_both_source_orders(self):
        for bad_first in (True, False):
            with self.subTest(bad_first=bad_first):
                result = self.build("[bad] + 1", bad_first=bad_first)
                consumer = next(m for m in self.measures(result)
                                if m["name"] == "Consumer")
                expression = consumer["expression"]
                _assert_unsupported(self, expression)
                self.assertRegex(
                    expression,
                    r"(?i)(depends.*unsupported|unresolved.*(?:identity|target))")

    def test_shared_caption_without_unique_identity_is_explicitly_refused(self):
        for bad_first in (True, False):
            with self.subTest(bad_first=bad_first):
                try:
                    result = self.build("[Shared caption] + 1", bad_first=bad_first)
                except PartialGenerationError as error:
                    self.assertRegex(str(error), r"(?i)ambig")
                else:
                    consumer = next(
                        m for m in self.measures(result) if m["name"] == "Consumer")
                    _assert_unsupported(self, consumer["expression"], "ambig")

    def test_final_distinct_target_identities_resolve_both_ids_and_refuse_caption(self):
        from powerbi_import.tmdl_dax_postprocess import (
            propagate_unsupported_model_dependencies,
        )

        source = [
            {"name": "[bad]", "caption": "Shared caption",
             "formula": "{ FIXED [Region] : MIN([Amount]) }"},
            {"name": "[good]", "caption": "Shared caption",
             "formula": "SUM([Amount])"},
            {"name": "[usegood]", "caption": "UseGood", "formula": "[good] + 1"},
            {"name": "[usebad]", "caption": "UseBad", "formula": "[bad] + 1"},
            {"name": "[usecaption]", "caption": "UseCaption",
             "formula": "[Shared caption] + 1"},
        ]

        def target(name, expression, calculation):
            return {
                "name": name, "expression": expression,
                "_tableau_calculation_identity": {
                    "name": calculation["name"], "caption": calculation["caption"]},
                "_original_formula": calculation["formula"],
            }

        for bad_first in (True, False):
            with self.subTest(bad_first=bad_first):
                bad = target(
                    "Shared caption",
                    "/* TODO: DAX conversion validation failed for measure: "
                    "FIXED source grain */ BLANK()", source[0])
                good = target("Shared caption (Good)", "SUM('Good'[Amount])", source[1])
                consumers = [
                    target(calc["caption"], "[Shared caption] + 1", calc)
                    for calc in source[2:]
                ]
                tables = [
                    {"name": "Bad", "columns": [], "measures": [bad]},
                    {"name": "Good", "columns": [
                        {"name": "Amount", "sourceColumn": "Amount"}],
                     "measures": [good, *consumers]},
                ]
                model = {
                    "_datasources": [{"name": "Synthetic", "calculations": (
                        source if bad_first else [source[1], source[0], *source[2:]])}],
                    "model": {"tables": tables if bad_first else list(reversed(tables))},
                }
                diagnostics = propagate_unsupported_model_dependencies(model)
                self.assertEqual(good["expression"], "SUM('Good'[Amount])")
                _assert_unsupported(self, bad["expression"], "FIXED", "grain")
                self.assertEqual(consumers[0]["expression"], "[Shared caption (Good)] + 1")
                _assert_unsupported(self, consumers[1]["expression"], "depends", "unsupported")
                _assert_unsupported(self, consumers[2]["expression"], "ambig")
                self.assertTrue(any(
                    item.get("field") == "UseCaption"
                    and "ambig" in item.get("issue", "").lower()
                    for item in diagnostics))


class TestMultiReferenceDependencyIdentity(unittest.TestCase):
    """Keep full, datasource-scoped source declarations when resolving refs."""

    @staticmethod
    def datasource(*, conflicting=False, reverse=False):
        first = {
            "name": "[id]", "caption": "BaseTotal", "formula": "SUM([Amount])",
            "role": "measure", "datatype": "real", "datasource_name": "Synthetic",
        }
        duplicate = dict(first, datatype="integer")
        repeated = [first, duplicate] if conflicting else [first]
        if reverse:
            repeated.reverse()
        return {
            "name": "Synthetic",
            "connection": {"type": "SQL Server", "details": {
                "server": "synthetic.invalid", "database": "fixture"}},
            "tables": [{"name": "Sales", "columns": [
                {"name": "Amount", "datatype": "real"},
                {"name": "Quantity", "datatype": "integer"},
            ]}],
            "calculations": repeated + [{
                "name": "[other]", "caption": "OtherTotal",
                "formula": "SUM([Quantity])", "role": "measure",
                "datatype": "integer", "datasource_name": "Synthetic",
            }, {
                "name": "[consumer]", "caption": "Consumer",
                "formula": "[id] + [other]", "role": "measure",
                "datatype": "real", "datasource_name": "Synthetic",
            }],
        }

    @staticmethod
    def final_model(datasource):
        """A possible target after a duplicate was collapsed; not source proof."""
        calculations = datasource["calculations"]

        def target(name, expression):
            source = next(calc for calc in calculations if calc["caption"] == name)
            return {
                "name": name, "expression": expression,
                "_tableau_calculation_identity": {
                    "name": source["name"], "caption": source["caption"],
                    "datasource_name": datasource["name"],
                },
                "_original_formula": source["formula"],
            }

        measures = [
            target("BaseTotal", "SUM('Sales'[Amount])"),
            target("OtherTotal", "SUM('Sales'[Quantity])"),
            target("Consumer", "[BaseTotal] + [OtherTotal]"),
        ]
        return {
            # Retain the entire original list, including both datatypes; a
            # flattened or already-deduplicated inventory would hide the bug.
            "_datasources": [datasource],
            "model": {"tables": [{
                "name": "Sales",
                "columns": [
                    {"name": "Amount", "sourceColumn": "Amount"},
                    {"name": "Quantity", "sourceColumn": "Quantity"},
                ],
                "measures": measures,
            }]},
        }, measures

    def assert_collision_inventory(self, datasource):
        records = [calc for calc in datasource["calculations"] if calc["name"] == "[id]"]
        self.assertEqual(len(records), 2)
        self.assertEqual({record["datatype"] for record in records}, {"real", "integer"})
        self.assertEqual(records[0]["datasource_name"], datasource["name"])
        self.assertEqual(records[1]["datasource_name"], datasource["name"])
        self.assertEqual(
            {key: value for key, value in records[0].items() if key != "datatype"},
            {key: value for key, value in records[1].items() if key != "datatype"},
        )
        consumer = next(calc for calc in datasource["calculations"]
                        if calc["name"] == "[consumer]")
        self.assertEqual(consumer["formula"], "[id] + [other]")

    def assert_refusal_context(self, error):
        self.assertRegex(str(error), r"(?i)(ambig|conflict|non.identical|metadata|unresolved)")
        self.assertIn("[id]", str(error))

    def assert_conflict_diagnostic(self, expression):
        _assert_unsupported(self, expression)
        self.assertRegex(
            expression, r"(?i)(ambig|conflict|non.identical|metadata|unresolved)")

    def test_multi_reference_duplicate_id_different_datatype_is_diagnosed(self):
        from powerbi_import.tmdl_dax_postprocess import (
            propagate_unsupported_model_dependencies,
        )

        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasource = self.datasource(conflicting=True, reverse=reverse)
                self.assert_collision_inventory(datasource)
                model, measures = self.final_model(datasource)
                before = deepcopy(datasource)
                try:
                    diagnostics = propagate_unsupported_model_dependencies(model)
                except PartialGenerationError as error:
                    self.assert_refusal_context(error)
                else:
                    self.assert_conflict_diagnostic(measures[2]["expression"])
                    self.assertTrue(any(
                        item.get("field") == "Consumer"
                        and re.search(
                            r"(?i)(ambig|conflict|non.identical|metadata|unresolved)",
                            item.get("issue", ""))
                        for item in diagnostics), diagnostics)
                    self.assertEqual(measures[1]["expression"],
                                     "SUM('Sales'[Quantity])")
                self.assertEqual(model["_datasources"], [before])

    def test_full_model_refuses_or_diagnoses_multi_reference_metadata_conflict(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasource = self.datasource(conflicting=True, reverse=reverse)
                self.assert_collision_inventory(datasource)
                original = deepcopy(datasource)
                try:
                    model = _build_semantic_model(
                        [datasource], report_name="Synthetic", model_mode="import",
                        calendar_start=2020, calendar_end=2021)
                except PartialGenerationError as error:
                    self.assert_refusal_context(error)
                    self.assertIn("Synthetic", error.source or "")
                else:
                    # The target cannot establish uniqueness by retaining only
                    # one flattened source declaration.
                    self.assertEqual(model["_datasources"], [original])
                    measures = [measure for table in model["model"]["tables"]
                                for measure in table.get("measures", [])]
                    consumer = next(measure for measure in measures
                                    if measure.get("_tableau_calculation_identity", {})
                                    .get("name") == "[consumer]")
                    self.assert_conflict_diagnostic(consumer["expression"])

    def test_unambiguous_multi_reference_stays_valid_in_postprocess_and_model(self):
        from powerbi_import.dax_validator import validate_dax_expression
        from powerbi_import.tmdl_dax_postprocess import (
            propagate_unsupported_model_dependencies,
        )

        datasource = self.datasource()
        original = deepcopy(datasource)
        model, measures = self.final_model(datasource)
        diagnostics = propagate_unsupported_model_dependencies(model)
        self.assertEqual(diagnostics, [])
        self.assertEqual(model["_datasources"], [original])
        self.assertEqual(measures[0]["expression"], "SUM('Sales'[Amount])")
        self.assertEqual(measures[1]["expression"], "SUM('Sales'[Quantity])")
        self.assertEqual(measures[2]["expression"], "[BaseTotal] + [OtherTotal]")
        self.assertEqual(validate_dax_expression(measures[2]["expression"]), [])

        model = _build_semantic_model(
            [deepcopy(original)], report_name="Synthetic", model_mode="import",
            calendar_start=2020, calendar_end=2021)
        self.assertEqual(model["_datasources"], [original])
        actual = {
            measure.get("_tableau_calculation_identity", {}).get("name"): measure
            for table in model["model"]["tables"] for measure in table.get("measures", [])
        }
        self.assertIn("[id]", actual)
        self.assertIn("[other]", actual)
        self.assertIn("[consumer]", actual)
        consumer = actual["[consumer]"]["expression"]
        self.assertNotIn(_FALLBACK, consumer)
        self.assertEqual(validate_dax_expression(consumer), [])
        self.assertIn(f'[{actual["[id]"]["name"]}]', consumer)
        self.assertIn(f'[{actual["[other]"]["name"]}]', consumer)


class TestTypedYearExtrema(unittest.TestCase):
    @staticmethod
    def _convert(formula, **overrides):
        options = {
            "table_name": "Records", "table_columns": {"EventDate", "Amount"},
            "column_table_map": {"EventDate": "Records", "Amount": "Records"},
            "date_columns": {"EventDate"}, "calc_datatype": "string",
            "validate_output": True, "fallback_on_invalid": True,
        }
        options.update(overrides)
        return convert_tableau_formula_to_dax(formula, **options)

    def _assert_guarded_year(self, expression, aggregate, table="Records",
                             column="EventDate"):
        reference = "'" + table.replace("'", "''") + f"'[{column}]"
        extrema = rf"{aggregate}\s*\(\s*{re.escape(reference)}\s*\)"
        self.assertNotIn("TODO", expression, expression)
        self.assertRegex(
            expression,
            rf"IF\s*\(\s*ISBLANK\s*\(\s*{extrema}\s*\)\s*,\s*"
            rf"BLANK\s*\(\s*\)\s*,\s*YEAR\s*\(\s*{extrema}\s*\)\s*\)",
        )
        self.assertNotRegex(expression, rf"{aggregate}\s*\(\s*YEAR\b")
        self.assertNotIn("1900", expression)
        self.assertNotIn("COALESCE", expression)

    def test_typed_year_extrema_preserve_null_guard_inside_string_and_subtraction(self):
        for aggregate in ("MIN", "MAX"):
            for container in (set, list, frozenset, tuple):
                for wrapper in ("{}", "STR({})", "STR({} - 1)"):
                    with self.subTest(aggregate=aggregate, container=container,
                                      wrapper=wrapper):
                        expression = self._convert(
                            wrapper.format(f"{aggregate}(YEAR([EventDate]))"),
                            date_columns=container(["EventDate"]))
                        self._assert_guarded_year(expression, aggregate)
                        if wrapper.startswith("STR"):
                            self.assertIn("FORMAT(", expression)
                        if "- 1" in wrapper:
                            self.assertIn("VAR __TableauNullable1 = 1", expression)
                            self.assertIn("ISBLANK(__TableauNullable0) || "
                                          "ISBLANK(__TableauNullable1), BLANK()", expression)
                            self.assertIn("(__TableauNullable0 - __TableauNullable1)",
                                          expression)

    def test_datetrunc_extrema_lower_to_guarded_scalar_dates(self):
        granularities = ("year", "quarter", "month")
        for granularity in granularities:
            for aggregate in ("MIN", "MAX"):
                with self.subTest(granularity=granularity, aggregate=aggregate):
                    expression = self._convert(
                        f"DATETRUNC('{granularity}', {aggregate}([EventDate]))")
                    reference = "'Records'[EventDate]"
                    extrema = rf"{aggregate}\s*\(\s*{re.escape(reference)}\s*\)"

                    self.assertNotIn("TODO", expression, expression)
                    assignment = re.search(
                        rf"\bVAR\s+([A-Za-z_]\w*)\s*=\s*{extrema}",
                        expression,
                        re.IGNORECASE,
                    )
                    self.assertIsNotNone(assignment, expression)
                    variable = assignment.group(1)
                    self.assertEqual(
                        len(re.findall(extrema, expression, re.IGNORECASE)), 1,
                        expression,
                    )
                    self.assertRegex(
                        expression,
                        rf"IF\s*\(\s*ISBLANK\s*\(\s*{re.escape(variable)}\s*\)"
                        rf"\s*,\s*BLANK\s*\(\s*\)\s*,",
                    )
                    self.assertRegex(expression, r"\bDATE\s*\(")
                    self.assertRegex(
                        expression,
                        rf"\bYEAR\s*\(\s*{re.escape(variable)}\s*\)",
                    )
                    if granularity in ("quarter", "month"):
                        self.assertRegex(
                            expression,
                            rf"\bMONTH\s*\(\s*{re.escape(variable)}\s*\)",
                        )
                    if granularity == "year":
                        self.assertRegex(
                            expression,
                            rf"DATE\s*\(\s*YEAR\s*\(\s*{re.escape(variable)}\s*\)"
                            rf"\s*,\s*1\s*,\s*1\s*\)",
                        )
                    self.assertNotRegex(
                        expression, r"\bSTARTOF(?:YEAR|QUARTER|MONTH)\s*\(|\bQUARTER\s*\(")

    def test_datetrunc_extrema_without_date_or_extrema_proof_remain_diagnostic(self):
        cases = (
            ("DATETRUNC('year', MIN([EventDate]))", {"date_columns": None}),
            ("DATETRUNC('year', SUM([EventDate]))", {}),
        )
        for formula, overrides in cases:
            with self.subTest(formula=formula, overrides=overrides):
                _assert_unsupported(
                    self, self._convert(formula, **overrides), "grain")

    def test_local_owner_and_escaped_table_name_are_preserved(self):
        for table in ("Records", "Records' Archive"):
            with self.subTest(table=table):
                owner = table.replace("'", "''")
                expression = self._convert(
                    f"STR(MAX(YEAR('{owner}'[EventDate])))",
                    table_name=table,
                    column_table_map={"EventDate": table})
                self._assert_guarded_year(expression, "MAX", table)

    def test_missing_or_invalid_date_proof_remains_diagnostic(self):
        for proof in (None, set(), "EventDate", {"EventDate": "date"},
                      True, 1, ["EventDate", None], ["EventDate", ""],
                      ["EventDate", 1], {"Amount"}):
            with self.subTest(proof=proof):
                _assert_unsupported(
                    self, self._convert("MAX(YEAR([EventDate]))", date_columns=proof),
                    "grain")

    def test_no_physical_column_proof_does_not_enable_lowering(self):
        for columns in (set(), {"Amount"}):
            with self.subTest(columns=columns):
                _assert_unsupported(
                    self, self._convert("MAX(YEAR([EventDate]))", table_columns=columns))

    def test_foreign_owner_and_calculation_measure_collisions_remain_diagnostic(self):
        cases = (
            ("MAX(YEAR('OtherRecords'[EventDate]))", {}),
            ("MAX(YEAR([EventDate]))", {
                "column_table_map": {"EventDate": "OtherRecords"}}),
            ("MAX(YEAR([EventDate]))", {"measure_names": {"EventDate"}}),
            ("MAX(YEAR([EventDate]))", {"calc_map": {"EventDate": "[Amount] + 1"}}),
        )
        for formula, overrides in cases:
            with self.subTest(formula=formula, overrides=overrides):
                _assert_unsupported(self, self._convert(formula, **overrides))

    def test_date_proof_does_not_authorize_nonmonotone_or_row_grain_rewrites(self):
        for formula in (
            "MAX(MONTH([EventDate]))", "MIN(DAY([EventDate]))",
            "SUM(IF [Amount] > 0 THEN [Amount] ELSE 0 END)",
            "{ FIXED [EventDate] : SUM([Amount]) }",
        ):
            with self.subTest(formula=formula):
                _assert_unsupported(self, self._convert(formula), "grain")

    @staticmethod
    def _datasource(owner, table, datatype):
        column = {"name": "EventDate", "caption": "Date"}
        if datatype is not None:
            column["datatype"] = datatype
        calculations = [
            {"name": "[YearLabel]", "caption": "Year Label",
             "formula": "STR(MAX(YEAR([EventDate])))"},
            {"name": "[PriorYearLabel]", "caption": "Prior Year Label",
             "formula": "STR(MAX(YEAR([EventDate])) - 1)"},
            {"name": "[LabelConsumer]", "caption": "Label Consumer",
             "formula": '[YearLabel] + " / " + [PriorYearLabel]'},
        ]
        for calculation in calculations:
            calculation.update(role="measure", datatype="string",
                               datasource_name=owner)
        return {
            "name": owner, "caption": owner,
            "connection": {"type": "sqlserver", "details": {
                "server": "synthetic.invalid", "database": "synthetic"}},
            "tables": [{"name": table, "columns": [column]}],
            "calculations": calculations, "columns": [], "relationships": [],
            "connection_map": {},
        }

    @staticmethod
    def _generate_tables(datasources):
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary) / "Synthetic.SemanticModel"
            generate_tmdl(datasources, "Synthetic", {}, str(model_dir),
                          parameterize=False)
            return {path.stem: path.read_text(encoding="utf-8")
                    for path in (model_dir / "definition" / "tables").glob("*.tmdl")}

    def _assert_generated_year_labels(self, content, table, supported, suffix=""):
        for caption in ("Year Label", "Prior Year Label", "Label Consumer"):
            declaration = re.search(
                rf"(?m)^\tmeasure '{re.escape(caption + suffix)}' =([^\n]*)", content)
            self.assertIsNotNone(declaration, content)
            expression = declaration.group(1).strip()
            if supported:
                self.assertNotIn("TODO", expression, expression)
                self.assertNotEqual(expression, "BLANK()")
                if caption != "Label Consumer":
                    self._assert_guarded_year(expression, "MAX", table, "Date")
                    self.assertIn("FORMAT(", expression)
                else:
                    self.assertIn("[Year Label]", expression)
                    self.assertIn("[Prior Year Label]", expression)
            else:
                self.assertIn(_FALLBACK, expression)
                _assert_unsupported(self, expression[expression.index("/* TODO:"):])

    def test_generate_tmdl_uses_only_declared_physical_date_types(self):
        for datatype in ("date", "datetime", "string", None):
            with self.subTest(datatype=datatype):
                content = self._generate_tables([
                    self._datasource("Synthetic", "Records", datatype)])["Records"]
                self._assert_generated_year_labels(
                    content, "Records", datatype in ("date", "datetime"))

    def test_generate_tmdl_date_proof_does_not_cross_datasource_identity(self):
        for date_first in (True, False):
            with self.subTest(date_first=date_first):
                sources = [self._datasource("TypedSource", "Records", "date"),
                           self._datasource("TextSource", "OtherRecords", "string")]
                for calculation in sources[1]["calculations"]:
                    calculation["name"] = calculation["name"][:-1] + "Text]"
                    calculation["caption"] += " Text"
                    calculation["formula"] = (
                        calculation["formula"].replace("[YearLabel]", "[YearLabelText]")
                        .replace("[PriorYearLabel]", "[PriorYearLabelText]"))
                if not date_first:
                    sources.reverse()
                contents = self._generate_tables(sources)
                self._assert_generated_year_labels(contents["Records"], "Records", True)
                self._assert_generated_year_labels(
                    contents["OtherRecords"], "OtherRecords", False, " Text")

    def test_generate_tmdl_calculation_date_type_is_not_physical_proof(self):
        datasource = self._datasource("Synthetic", "Records", "string")
        datasource["calculations"].append({
            "name": "[CalculatedDate]", "caption": "Calculated Date",
            "formula": "DATE([EventDate])", "datatype": "date",
            "role": "dimension", "datasource_name": "Synthetic",
        })
        for calculation in datasource["calculations"][:2]:
            calculation["formula"] = calculation["formula"].replace(
                "[EventDate]", "[CalculatedDate]")
        content = self._generate_tables([datasource])["Records"]
        self._assert_generated_year_labels(content, "Records", False)


class TestReportedScalarNumericMinMax(unittest.TestCase):
    """Static AST and real generation checks, not native DAX execution."""

    @staticmethod
    def _proof():
        return {
            "table_name": "Facts", "table_columns": {"Amount"},
            "numeric_columns": {"Amount"},
            "column_table_map": {"Amount": "Facts"},
            "measure_names": set(), "calc_map": {}, "param_map": {},
        }

    @staticmethod
    def _datasource(formula):
        return {
            "name": "Synthetic",
            "connection": {"type": "SQL Server", "details": {
                "server": "synthetic.invalid", "database": "synthetic"}},
            "tables": [{"name": "Facts", "columns": [
                {"name": "Amount", "datatype": "real"},
                {"name": "RowKey", "datatype": "integer"}]}],
            "calculations": [{
                "name": "[C]", "caption": "C", "formula": formula,
                "datatype": "real", "role": "measure",
                "datasource_name": "Synthetic",
            }, {
                "name": "[Total]", "caption": "Total", "formula": "SUM([C])",
                "datatype": "real", "role": "measure",
                "datasource_name": "Synthetic",
            }],
        }

    def _pipeline(self, datasource, extra=None):
        extra = extra or {}
        context = _collect_semantic_context([datasource], extra)
        model = {"model": {"tables": []}}
        _create_semantic_tables(model, context, [datasource], extra)
        tables = [table for table in model["model"]["tables"]
                  if table["name"] == "Facts"]
        self.assertEqual(len(tables), 1)
        with tempfile.TemporaryDirectory() as temporary:
            generate_tmdl([datasource], "Synthetic", extra, temporary,
                          parameterize=False)
            path = Path(temporary) / "definition" / "tables" / "Facts.tmdl"
            self.assertTrue(path.is_file())
            content = path.read_text(encoding="utf-8")
        return context, tables[0], content

    def _declaration(self, content, kind, name):
        matches = re.findall(
            rf"(?m)^\t{kind} (?:{re.escape(name)}|'{re.escape(name)}') =([^\n]+)",
            content)
        self.assertEqual(len(matches), 1, content)
        return re.sub(r"^/\* Tableau calculation;[^\n]*?\*/\s*", "",
                      matches[0].strip(), count=1)

    def _assert_nullable_ast(self, expression, function, literal, reverse):
        tree = _EmittedDax(expression).tree
        self.assertEqual(tree[:2], ("call", "IF"))
        guard, blank, choice = tree[2]
        self.assertEqual(guard, ("call", "ISBLANK", [("column", "Facts", "Amount")]))
        self.assertEqual(blank, ("call", "BLANK", []))
        self.assertEqual(choice[:2], ("call", "IF"))
        condition, left, right = choice[2]
        self.assertEqual(right if reverse else left, ("column", "Facts", "Amount"))

        def nodes(value):
            if isinstance(value, (tuple, list)):
                yield value
                for child in value:
                    yield from nodes(child)

        literal_nodes = list(nodes(left if reverse else right))
        self.assertIn(("value", abs(float(literal))), literal_nodes)
        if literal.startswith(("+", "-")):
            self.assertTrue(any(node[:2] == ("unary", literal[0])
                                for node in literal_nodes))
        self.assertTrue(any(node[:2] == ("binary", "<" if function == "MIN" else ">")
                            for node in nodes(condition)))
        self.assertNotIn("TODO", expression)
        self.assertNotRegex(expression, r"\b(?:MIN|MAX|SUMX|SUM)\s*\(")

    def test_binary_numeric_literals_both_orders_are_null_guarded_scalar_ast(self):
        for function in ("MIN", "MAX"):
            for literal in ("0", "-2.5", "+2.5", "-1e2", "+.5"):
                for reverse in (False, True):
                    args = (literal, "[Amount]") if reverse else ("[Amount]", literal)
                    formula = f"{function}({', '.join(args)})"
                    with self.subTest(formula=formula):
                        self.assertTrue(is_supported_scalar_numeric_minmax(
                            formula, **self._proof()))
                        expression = convert_tableau_formula_to_dax(
                            formula, **self._proof(), is_calc_column=True,
                            validate_output=True, fallback_on_invalid=True)
                        self._assert_nullable_ast(expression, function, literal, reverse)

    def test_helper_requires_physical_numeric_nonparameter_noncalculated_proof(self):
        cases = (
            {"table_name": ""}, {"table_columns": None},
            {"table_columns": set()}, {"numeric_columns": None},
            {"numeric_columns": set()},
            {"column_table_map": {"Amount": None}},
            {"column_table_map": {"Amount": "OtherFacts"}},
            {"measure_names": {"amount"}},
            {"calc_map": {"Amount": "Calculated Amount"}},
            {"calc_map": {"RawAmount": "Amount"}},
            {"param_map": {"Amount": "Threshold"}},
            {"param_map": {"RawParameter": "Amount"}},
        )
        for function in ("MIN", "MAX"):
            for reverse in (False, True):
                args = "-2, [Amount]" if reverse else "[Amount], -2"
                for overrides in cases:
                    with self.subTest(function=function, reverse=reverse, proof=overrides):
                        proof = {**self._proof(), **overrides}
                        self.assertFalse(is_supported_scalar_numeric_minmax(
                            f"{function}({args})", **proof))

    def test_unsupported_binary_shapes_retain_explicit_diagnostics(self):
        for function in ("MIN", "MAX"):
            for operand in ("NULL", "TRUE", '"2"', "[RowKey]", "[Unknown]",
                            "[OtherFacts].[Amount]", "[Amount] + 1"):
                for reverse in (False, True):
                    args = (operand, "[Amount]") if reverse else ("[Amount]", operand)
                    formula = f"{function}({', '.join(args)})"
                    with self.subTest(formula=formula):
                        self.assertFalse(is_supported_scalar_numeric_minmax(
                            formula, **self._proof()))
                        _assert_unsupported(self, convert_tableau_formula_to_dax(
                            formula, **self._proof(), is_calc_column=True,
                            validate_output=True, fallback_on_invalid=True))

    def test_real_pipeline_emits_calculated_column_and_qualified_sum(self):
        for function in ("MIN", "MAX"):
            for literal in ("0", "-2.5", "+2.5"):
                for reverse in (False, True):
                    args = (literal, "[Amount]") if reverse else ("[Amount]", literal)
                    formula = f"{function}({', '.join(args)})"
                    with self.subTest(formula=formula):
                        datasource = self._datasource(formula)
                        context, table, content = self._pipeline(datasource)
                        calculation = context["all_calculations"][0]
                        self.assertIn(id(calculation),
                                      context["dax_context"]["scalar_minmax_contexts"])
                        self.assertNotIn("C", context["measure_names"])
                        self.assertIn("Total", context["measure_names"])
                        self.assertEqual(context["column_table_map"]["C"], "Facts")
                        columns = [column for column in table["columns"]
                                   if column["name"] == "C"]
                        self.assertEqual(len(columns), 1)
                        if "expression" in columns[0]:
                            self.assertTrue(columns[0]["isCalculated"])
                            expression = columns[0]["expression"]
                            self._assert_nullable_ast(expression, function, literal, reverse)
                            self.assertEqual(self._declaration(content, "column", "C"), expression)
                        else:
                            self.assertEqual(literal, "0")
                            self.assertEqual(columns[0]["sourceColumn"], "C")
                            partition = table["partitions"][0]["source"]["expression"]
                            steps = [line.strip() for line in partition.splitlines()
                                     if "Table.AddColumn(" in line and '"C"' in line]
                            self.assertEqual(len(steps), 1, partition)
                            left, right = ("0", "[Amount]") if reverse else ("[Amount]", "0")
                            comparison = "<" if function == "MIN" else ">"
                            self.assertIn(
                                '"C", each if (([Amount] = null)) = true then null else '
                                f'if ({left} {comparison} {right}) = true then {left} '
                                f'else {right}, type number)', steps[0])
                            self.assertIn(steps[0], content)
                            self.assertRegex(content, r"(?m)^\tcolumn C\s*$")
                        measures = {measure["name"]: measure["expression"]
                                    for measure in table["measures"]}
                        self.assertNotIn("C", measures)
                        self.assertEqual(measures["Total"], "SUM('Facts'[C])")
                        self.assertEqual(self._declaration(content, "measure", "Total"),
                                         "SUM('Facts'[C])")
                        self.assertNotRegex(content, r"(?m)^\tmeasure '?C'? =")

    def test_real_pipeline_unary_minmax_remain_aggregate_measures(self):
        for function in ("MIN", "MAX"):
            with self.subTest(function=function):
                datasource = self._datasource(f"{function}([Amount])")
                context, table, content = self._pipeline(datasource)
                self.assertEqual(context["dax_context"]["scalar_minmax_contexts"], {})
                self.assertIn("C", context["measure_names"])
                self.assertNotIn("C", {column["name"] for column in table["columns"]})
                measures = {measure["name"]: measure["expression"]
                            for measure in table["measures"]}
                self.assertEqual(measures["C"], f"{function}('Facts'[Amount])")
                self.assertEqual(self._declaration(content, "measure", "C"), measures["C"])
                _assert_unsupported(self, measures["Total"])

    def test_real_pipeline_does_not_invent_missing_or_ambiguous_evidence(self):
        cases = ("unknown column", "missing type", "text type", "missing owner",
                 "unknown owner", "foreign column", "ambiguous column",
                 "calculated operand", "parameter operand", "physical conflict",
                 "metadata calculation", "declaration conflict")
        for function in ("MIN", "MAX"):
            for case in cases:
                with self.subTest(function=function, case=case):
                    datasource = self._datasource(f"{function}([Amount], -2)")
                    calculation = datasource["calculations"][0]
                    physical = datasource["tables"][0]["columns"][0]
                    extra = {}
                    if case == "unknown column":
                        calculation["formula"] = f"{function}([Missing], -2)"
                    elif case == "missing type":
                        physical.pop("datatype")
                    elif case == "text type":
                        physical["datatype"] = "string"
                    elif case in ("missing owner", "unknown owner"):
                        calculation["datasource_name"] = "" if case == "missing owner" else "Unknown"
                        calculation["table"] = "Facts"
                    elif case in ("foreign column", "ambiguous column"):
                        datasource["tables"].append({"name": "OtherFacts", "columns": [
                            {"name": "Amount", "datatype": "real"}]})
                        if case == "foreign column":
                            calculation["formula"] = f"{function}([OtherFacts].[Amount], -2)"
                    elif case == "calculated operand":
                        physical["calculation"] = "[RowKey] + 1"
                        datasource["calculations"].append({
                            "name": "[Amount]", "caption": "Amount", "formula": "[RowKey] + 1",
                            "role": "dimension", "datatype": "real", "datasource_name": "Synthetic"})
                    elif case == "parameter operand":
                        extra = {"parameters": [{
                            "name": "[Parameters].[Amount]", "caption": "Amount",
                            "datatype": "real", "domain_type": "any", "value": "2"}]}
                    elif case == "physical conflict":
                        physical["_source_declaration_conflicts"] = [{"datatype": "string"}]
                    elif case == "metadata calculation":
                        datasource["columns"] = [{
                            "name": "[Amount]", "datatype": "real", "calculation": "[RowKey] + 1"}]
                    elif case == "declaration conflict":
                        calculation["_source_declaration_conflicts"] = [{
                            **calculation, "formula": f"{function}([Amount], -3)"}]
                    context, table, content = self._pipeline(datasource, extra)
                    self.assertEqual(context["dax_context"]["scalar_minmax_contexts"], {})
                    self.assertIn("C", context["measure_names"])
                    self.assertNotIn("C", {column["name"] for column in table["columns"]})
                    measures = {measure["name"]: measure["expression"]
                                for measure in table["measures"]}
                    _assert_unsupported(self, measures["C"])
                    _assert_unsupported(self, measures["Total"])
                    for name in ("C", "Total"):
                        self.assertEqual(self._declaration(content, "measure", name), measures[name])


class TestReportedNumericConstantHomonyms(unittest.TestCase):
    @staticmethod
    def _datasources(values=("1", "1")):
        datasources = []
        for owner, table, column, value in (
                ("SourceA", "FactsA", "Other", values[0]),
                ("SourceB", "FactsB", "Kind", values[1])):
            calculations = []
            if owner == "SourceB":
                calculations.append({
                    "name": "[Chosen]", "caption": "Chosen",
                    "formula": 'IF [Kind] = "X" THEN [Unit] END',
                    "role": "measure", "datatype": "real", "datasource_name": owner,
                })
            calculations.append({
                "name": "[Unit]", "caption": "Unit", "formula": value,
                "role": "measure", "datatype": "real", "datasource_name": owner,
            })
            datasources.append({
                "name": owner,
                "connection": {"type": "SQL Server", "details": {
                    "server": "synthetic.invalid", "database": "synthetic"}},
                "tables": [{"name": table, "columns": [
                    {"name": column, "datatype": "string"}]}],
                "calculations": calculations,
            })
        return datasources

    def _pipeline(self, datasources, extra=None):
        extra = extra or {}
        context = _collect_semantic_context(datasources, extra)
        model = {"model": {"tables": []}}
        _create_semantic_tables(model, context, datasources, extra)
        tables = {table["name"]: table for table in model["model"]["tables"]}
        with tempfile.TemporaryDirectory() as temporary:
            generate_tmdl(datasources, "Synthetic", extra, temporary,
                          parameterize=False)
            contents = {path.stem: path.read_text(encoding="utf-8")
                        for path in (Path(temporary) / "definition" / "tables").glob("*.tmdl")}
        self.assertEqual(set(contents), set(tables))
        return context, tables, contents

    def _assert_materialized(self, table, content, name, body):
        columns = [column for column in table["columns"] if column["name"] == name]
        self.assertEqual(len(columns), 1)
        self.assertEqual(columns[0]["sourceColumn"], name)
        partition = table["partitions"][0]["source"]["expression"]
        steps = [line.strip() for line in partition.splitlines()
                 if "Table.AddColumn(" in line and f'"{name}", each ' in line]
        self.assertEqual(len(steps), 1, partition)
        self.assertIn(f'"{name}", each {body}, type number)', steps[0])
        self.assertIn(steps[0], content)
        self.assertRegex(content, rf"(?m)^\tcolumn (?:{name}|'{name}')\s*$")
        self.assertNotIn(name, {measure["name"] for measure in table["measures"]})
        self.assertNotRegex(content, rf"(?m)^\tmeasure (?:{name}|'{name}') =")
        self.assertNotIn("TODO", partition)
        self.assertNotIn("BLANK()", partition)

    def _assert_local_constants(self, tables, contents, values):
        for table_name, value in zip(("FactsA", "FactsB"), values):
            self._assert_materialized(tables[table_name], contents[table_name], "Unit", value)

    def test_source_b_alone_materializes_measure_role_row_consumer(self):
        _, tables, contents = self._pipeline(self._datasources()[1:])
        self._assert_materialized(tables["FactsB"], contents["FactsB"], "Unit", "1")
        self._assert_materialized(
            tables["FactsB"], contents["FactsB"], "Chosen",
            'if ([Kind] = "X") = true then 1 else null')

    def test_homonymous_numeric_constants_bind_local_targets_in_both_source_orders(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasources = self._datasources()
                if reverse:
                    datasources.reverse()
                context, tables, contents = self._pipeline(datasources)
                self.assertEqual({(calc["datasource_name"], calc["name"], calc["caption"])
                                  for calc in context["all_calculations"]}, {
                    ("SourceA", "[Unit]", "Unit"), ("SourceB", "[Unit]", "Unit"),
                    ("SourceB", "[Chosen]", "Chosen")})
                bindings = {name: owner for name, owner in context["column_table_map"].items()
                            if name.startswith("Unit")}
                self.assertEqual(len(bindings), 2)
                self.assertNotIn("Unit", bindings)
                self.assertEqual(set(bindings.values()), {"FactsA", "FactsB"})
                self.assertEqual(context["column_table_map"]["Chosen"], "FactsB")
                self._assert_local_constants(tables, contents, ("1", "1"))
                self._assert_materialized(
                    tables["FactsB"], contents["FactsB"], "Chosen",
                    'if ([Kind] = "X") = true then 1 else null')
                self.assertEqual({column["name"] for column in tables["FactsA"]["columns"]},
                                 {"Other", "Unit"})
                self.assertEqual({column["name"] for column in tables["FactsB"]["columns"]},
                                 {"Kind", "Chosen", "Unit"})
                for content in contents.values():
                    self.assertNotIn("TODO", content)
                    self.assertNotIn("BLANK()", content)

    def test_distinct_homonymous_constants_do_not_inline_foreign_value(self):
        """Same-caption literals retain their datasource-local values."""
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasources = self._datasources(("2", "7"))
                if reverse:
                    datasources.reverse()
                _, tables, contents = self._pipeline(datasources)
                self._assert_local_constants(tables, contents, ("2", "7"))
                self._assert_materialized(
                    tables["FactsB"], contents["FactsB"], "Chosen",
                    'if ([Kind] = "X") = true then 7 else null')

    def test_literal_alias_chain_is_resolved_within_its_datasource(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasources = self._datasources(("2", "7"))
                for datasource in datasources:
                    owner = datasource["name"]
                    datasource["calculations"].extend(({
                        "name": "[LocalUnit]", "caption": "LocalUnit",
                        "formula": "[Unit]", "role": "measure", "datatype": "real",
                        "datasource_name": owner,
                    }, {
                        "name": "[LocalUnit2]", "caption": "LocalUnit2",
                        "formula": "[LocalUnit]", "role": "measure", "datatype": "real",
                        "datasource_name": owner,
                    }))
                source_b = next(ds for ds in datasources if ds["name"] == "SourceB")
                source_b["calculations"][0]["formula"] = (
                    'IF [Kind] = "X" THEN [LocalUnit2] END')
                if reverse:
                    datasources.reverse()
                _, tables, contents = self._pipeline(datasources)
                self._assert_local_constants(tables, contents, ("2", "7"))
                self._assert_materialized(
                    tables["FactsB"], contents["FactsB"], "Chosen",
                    'if ([Kind] = "X") = true then 7 else null')

    def test_parameter_value_wins_over_homonymous_calculation_literal(self):
        datasources = self._datasources(("2", "7"))
        source_b = next(ds for ds in datasources if ds["name"] == "SourceB")
        source_b["tables"][0]["columns"].append(
            {"name": "Unit", "datatype": "real"})
        source_b["calculations"][0]["formula"] = (
            'IF [Kind] = "X" THEN [Parameters].[UnitParam] END')
        extra = {"parameters": [{
            "name": "[Parameters].[UnitParam]", "caption": "Unit",
            "datatype": "real", "domain_type": "any", "value": "9",
        }]}
        context, tables, contents = self._pipeline(datasources, extra)
        self.assertEqual(context["dax_context"]["param_values"]["Unit"], "9")
        self.assertEqual(context["dax_context"]["parameter_values"]["Unit"], "9")
        self._assert_materialized(
            tables["FactsB"], contents["FactsB"], "Chosen",
            'if ([Kind] = "X") = true then 9 else null')

    def test_conflicting_local_literal_aliases_remain_diagnostic(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasources = self._datasources(("2", "7"))
                source_b = next(ds for ds in datasources if ds["name"] == "SourceB")
                original = next(calc for calc in source_b["calculations"]
                                if calc["caption"] == "Unit")
                alternative = {**original, "formula": "9"}
                conflict = [dict(original), alternative]
                original["_source_declaration_conflicts"] = conflict
                alternative["_source_declaration_conflicts"] = conflict
                source_b["calculations"].append(alternative)
                if reverse:
                    datasources.reverse()
                _, tables, contents = self._pipeline(datasources)
                for name in ("Unit", "Chosen"):
                    column = next(column for column in tables["FactsB"]["columns"]
                                  if column["name"] == name)
                    _assert_unsupported(
                        self, column["expression"],
                        "conflicting source calculation declarations")
                    self.assertNotRegex(
                        contents["FactsB"],
                        rf'Table\.AddColumn\([^\n]*"{name}"')
                self._assert_materialized(
                    tables["FactsA"], contents["FactsA"], "Unit", "2")

    def test_foreign_constant_does_not_shadow_local_physical_column(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasources = self._datasources(("2", "7"))
                source_b = next(ds for ds in datasources if ds["name"] == "SourceB")
                source_b["tables"][0]["columns"].append(
                    {"name": "Unit", "datatype": "real"})
                source_b["calculations"] = [
                    calc for calc in source_b["calculations"]
                    if calc["caption"] != "Unit"
                ]
                if reverse:
                    datasources.reverse()
                _, tables, contents = self._pipeline(datasources)
                self._assert_materialized(
                    tables["FactsB"], contents["FactsB"], "Chosen",
                    'if ([Kind] = "X") = true then [Unit] else null')
                self.assertEqual(
                    tables["FactsB"]["columns"][0]["name"], "Kind")
                self.assertIn("Unit", {column["name"]
                                        for column in tables["FactsB"]["columns"]})

    def test_transitive_homonymous_constants_keep_local_value(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasources = self._datasources(("2", "7"))
                for datasource in datasources:
                    datasource["calculations"].append({
                        "name": "[LocalUnit]", "caption": "LocalUnit",
                        "formula": "[Unit]", "role": "measure", "datatype": "real",
                        "datasource_name": datasource["name"],
                    })
                datasources[1]["calculations"][0]["formula"] = (
                    'IF [Kind] = "X" THEN [LocalUnit] END')
                if reverse:
                    datasources.reverse()
                _, tables, contents = self._pipeline(datasources)
                self._assert_local_constants(tables, contents, ("2", "7"))
                self._assert_materialized(
                    tables["FactsB"], contents["FactsB"], "Chosen",
                    'if ([Kind] = "X") = true then 7 else null')

    def test_missing_consumer_owner_remains_diagnostic(self):
        """Missing ownership never borrows a literal from another datasource."""
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasources = self._datasources()
                datasources[1]["calculations"][0].pop("datasource_name")
                if reverse:
                    datasources.reverse()
                _, tables, contents = self._pipeline(datasources)
                declarations = [item for table in tables.values()
                                for kind in ("columns", "measures")
                                for item in table[kind] if item["name"] == "Chosen"]
                self.assertEqual(len(declarations), 1)
                _assert_unsupported(self, declarations[0].get("expression", ""))
                self.assertTrue(any(_FALLBACK in content for content in contents.values()))
                for content in contents.values():
                    self.assertNotRegex(content, r'Table\.AddColumn\([^\n]*"Chosen"')

    def test_conflicting_local_constant_and_consumer_remain_diagnostic(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasources = self._datasources()
                constant = datasources[1]["calculations"][1]
                constant["_source_declaration_conflicts"] = [dict(constant, formula="2")]
                if reverse:
                    datasources.reverse()
                _, tables, contents = self._pipeline(datasources)
                columns = {column["name"]: column for column in tables["FactsB"]["columns"]}
                for name in ("Unit", "Chosen"):
                    _assert_unsupported(self, columns[name]["expression"],
                                        "conflicting source calculation declarations")
                    self.assertIn(columns[name]["expression"], contents["FactsB"])
                    self.assertNotRegex(contents["FactsB"],
                                        rf'Table\.AddColumn\([^\n]*"{name}"')
                self._assert_materialized(tables["FactsA"], contents["FactsA"], "Unit", "1")

    def test_true_cross_table_row_measure_remains_diagnostic(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasources = self._datasources()
                datasources[1]["tables"].append({"name": "LookupB", "columns": [
                    {"name": "Weight", "datatype": "real"}]})
                datasources[1]["calculations"][0]["formula"] = (
                    'IF [Kind] = "X" THEN [Weight] END')
                if reverse:
                    datasources.reverse()
                _, tables, contents = self._pipeline(datasources)
                measures = {measure["name"]: measure["expression"]
                            for measure in tables["FactsB"]["measures"]}
                _assert_unsupported(self, measures["Chosen"], "row", "scope", "grain")
                self.assertIn(measures["Chosen"], contents["FactsB"])
                self.assertNotIn("Chosen", {column["name"]
                                            for column in tables["FactsB"]["columns"]})
                self.assertNotRegex(contents["FactsB"], r'Table\.AddColumn\([^\n]*"Chosen"')
                self._assert_local_constants(tables, contents, ("1", "1"))


class TestReportedDaxDiagnosticsIntegration(unittest.TestCase):
    """Static pipeline and quality checks; no semantic engine is invoked."""

    @staticmethod
    def _write_worksheet_only_cli_workbook(input_dir, dependency_datasource="Synthetic"):
        source = input_dir / "Coordinates.csv"
        source.write_text("x,y\n1,2\n3,4\n", encoding="utf-8")
        workbook = ET.Element("workbook", version="18.1")
        datasource = ET.SubElement(
            ET.SubElement(workbook, "datasources"), "datasource",
            name="Synthetic", caption="Synthetic", inline="true")
        connection = ET.SubElement(datasource, "connection", {
            "class": "textscan", "directory": str(input_dir),
            "filename": source.name, "separator": ",", "header": "yes",
        })
        relation = ET.SubElement(
            connection, "relation", type="table", name="Coordinates",
            table="[Coordinates.csv]")
        columns = ET.SubElement(relation, "columns", header="yes", separator=",")
        records = ET.SubElement(connection, "metadata-records")
        for ordinal, name in enumerate(("x", "y")):
            ET.SubElement(columns, "column", name=name, datatype="real",
                          ordinal=str(ordinal))
            ET.SubElement(datasource, "column", name=f"[{name}]",
                          datatype="real", role="measure")
            record = ET.SubElement(records, "metadata-record", {"class": "column"})
            for tag, value in (("local-name", f"[{name}]"), ("remote-name", name),
                               ("parent-name", "[Coordinates]"), ("local-type", "real")):
                ET.SubElement(record, tag).text = value
        worksheet = ET.SubElement(
            ET.SubElement(workbook, "worksheets"), "worksheet", name="Coordinate Total")
        table = ET.SubElement(worksheet, "table")
        view = ET.SubElement(table, "view")
        ET.SubElement(ET.SubElement(view, "datasources"), "datasource",
                      name=dependency_datasource, caption=dependency_datasource)
        dependencies = ET.SubElement(
            view, "datasource-dependencies", datasource=dependency_datasource)
        calculation = ET.SubElement(
            dependencies, "column", name="[WorksheetTotal]", caption="Coordinate Total",
            role="measure", datatype="real", type="quantitative")
        ET.SubElement(calculation, "calculation", {
            "class": "tableau", "formula": "SUM([x]) + SUM([y])",
        })
        ET.SubElement(dependencies, "column-instance", {
            "column": "[WorksheetTotal]", "derivation": "User",
            "name": "[usr:WorksheetTotal:qk]", "type": "quantitative",
        })
        ET.SubElement(table, "cols")
        ET.SubElement(table, "rows").text = (
            f"[{dependency_datasource}].[usr:WorksheetTotal:qk]")
        ET.SubElement(ET.SubElement(ET.SubElement(table, "panes"), "pane"),
                      "mark", {"class": "Text"})
        path = input_dir / "SyntheticWorksheet.twb"
        ET.ElementTree(workbook).write(path, encoding="utf-8", xml_declaration=True)
        return path

    def _extract_scoped_worksheet_calculations(self, root, global_datasources=()):
        from tableau_export.extract_tableau_data import TableauExtractor

        path = self._write_worksheet_only_cli_workbook(root)
        workbook = ET.parse(path).getroot()
        datasource = deepcopy(workbook.find("./datasources/datasource"))
        datasource.set("name", "OtherSynthetic")
        datasource.set("caption", "OtherSynthetic")
        source = root / "OtherCoordinates.csv"
        source.write_text("u,v\n5,6\n7,8\n", encoding="utf-8")
        connection = datasource.find("connection")
        connection.set("filename", source.name)
        relation = connection.find("relation")
        relation.set("name", "OtherCoordinates")
        relation.set("table", "[OtherCoordinates.csv]")
        for column, name in zip(relation.findall("./columns/column"), ("u", "v")):
            column.set("name", name)
        for column, name in zip(datasource.findall("column"), ("u", "v")):
            column.set("name", f"[{name}]")
        for record, name in zip(connection.findall("./metadata-records/metadata-record"),
                                ("u", "v")):
            record.find("local-name").text = f"[{name}]"
            record.find("remote-name").text = name
            record.find("parent-name").text = "[OtherCoordinates]"
        workbook.find("datasources").append(datasource)
        worksheet = deepcopy(workbook.find("./worksheets/worksheet"))
        worksheet.set("name", "Other Total")
        reference = worksheet.find("./table/view/datasources/datasource")
        reference.set("name", "OtherSynthetic")
        reference.set("caption", "OtherSynthetic")
        dependencies = worksheet.find("./table/view/datasource-dependencies")
        dependencies.set("datasource", "OtherSynthetic")
        dependencies.find("column").set("caption", "Other Total")
        dependencies.find("./column/calculation").set("formula", "SUM([u]) * SUM([v])")
        worksheet.find("./table/rows").text = "[OtherSynthetic].[usr:WorksheetTotal:qk]"
        workbook.find("worksheets").append(worksheet)
        for declared in workbook.findall("./datasources/datasource"):
            if declared.get("name") in global_datasources:
                owner = declared.get("name")
                dependency = next(
                    item for item in workbook.findall(
                        "./worksheets/worksheet/table/view/datasource-dependencies")
                    if item.get("datasource") == owner)
                calculation = deepcopy(dependency.find("column"))
                if owner == "Synthetic":
                    calculation.find("calculation").set("formula", "SUM([x]) - SUM([y])")
                declared.append(calculation)
        ET.ElementTree(workbook).write(path, encoding="utf-8", xml_declaration=True)
        extractor = TableauExtractor(str(path), output_dir=str(root / "extract"))
        extractor.extract_datasources(workbook)
        extractor.extract_calculations(workbook)
        return extractor.workbook_data

    def _assert_scoped_worksheet_calculations(self, data, first_formula):
        expected = {
            "Synthetic": first_formula,
            "OtherSynthetic": "SUM([u]) * SUM([v])",
        }
        calculations = data["calculations"]
        self.assertEqual(len(calculations), 2)
        self.assertEqual({item["name"] for item in calculations}, {"[WorksheetTotal]"})
        self.assertEqual(
            {item["datasource_name"]: item["formula"] for item in calculations}, expected)
        self.assertEqual({item["name"] for item in data["datasources"]}, set(expected))
        for datasource in data["datasources"]:
            self.assertEqual(len(datasource["calculations"]), 1)
            calculation = datasource["calculations"][0]
            self.assertEqual(calculation["name"], "[WorksheetTotal]")
            self.assertEqual(calculation["datasource_name"], datasource["name"])
            self.assertEqual(calculation["formula"], expected[datasource["name"]])
            self.assertIn(calculation, calculations)

    def test_worksheet_only_same_raw_id_survives_in_two_datasources(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = self._extract_scoped_worksheet_calculations(Path(temporary))
            self._assert_scoped_worksheet_calculations(data, "SUM([x]) + SUM([y])")
            for calculation in data["calculations"]:
                self.assertNotIn("_source_declaration_conflicts", calculation)

    def test_global_and_other_datasource_worksheet_same_raw_id_both_survive(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = self._extract_scoped_worksheet_calculations(
                Path(temporary), global_datasources=("Synthetic",))
            self._assert_scoped_worksheet_calculations(data, "SUM([x]) - SUM([y])")

    def test_global_same_raw_id_survives_in_two_datasources(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = self._extract_scoped_worksheet_calculations(
                Path(temporary), global_datasources=("Synthetic", "OtherSynthetic"))
            self._assert_scoped_worksheet_calculations(data, "SUM([x]) - SUM([y])")

    def test_worksheet_only_same_datasource_repeated_across_sheets_is_deduplicated(self):
        from tableau_export.extract_tableau_data import TableauExtractor

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self._write_worksheet_only_cli_workbook(root)
            workbook = ET.parse(path).getroot()
            repeated = deepcopy(workbook.find("./worksheets/worksheet"))
            repeated.set("name", "Repeated Coordinate Total")
            workbook.find("worksheets").append(repeated)
            extractor = TableauExtractor(str(path), output_dir=str(root / "extract"))
            extractor.extract_datasources(workbook)
            extractor.extract_calculations(workbook)
            calculations = extractor.workbook_data["calculations"]
            self.assertEqual(len(calculations), 1)
            self.assertEqual(calculations[0]["name"], "[WorksheetTotal]")
            self.assertEqual(calculations[0]["datasource_name"], "Synthetic")
            self.assertEqual(calculations[0]["formula"], "SUM([x]) + SUM([y])")
            self.assertEqual(extractor.workbook_data["datasources"][0]["calculations"],
                             calculations)
            self.assertNotIn("_source_declaration_conflicts", calculations[0])

    def _write_source_declaration_workbook(self, root, alternates, dependents=False):
        path = self._write_worksheet_only_cli_workbook(root)
        workbook = ET.parse(path).getroot()
        original = workbook.find("./worksheets/worksheet")
        for index, changes in enumerate(alternates):
            worksheet = deepcopy(original)
            worksheet.set("name", f"Alternate Total {index}")
            column = worksheet.find("./table/view/datasource-dependencies/column")
            for key, value in changes.items():
                target = column.find("calculation") if key in ("formula", "class") else column
                target.set(key, value)
            workbook.find("worksheets").append(worksheet)
        if dependents:
            dependencies = original.find("./table/view/datasource-dependencies")
            for name, formula in (("Consumer", "[WorksheetTotal] + 1"),
                                  ("Unrelated", "SUM([x])")):
                column = ET.SubElement(
                    dependencies, "column", name=f"[{name}]", caption=name,
                    role="measure", datatype="real", type="quantitative")
                ET.SubElement(column, "calculation", {"class": "tableau", "formula": formula})
        ET.ElementTree(workbook).write(path, encoding="utf-8", xml_declaration=True)
        return path

    @staticmethod
    def _extract_source_declaration_workbook(path):
        from tableau_export.extract_tableau_data import TableauExtractor

        workbook = ET.parse(path).getroot()
        extractor = TableauExtractor(str(path), output_dir=str(path.parent / "extract"))
        extractor.extract_datasources(workbook)
        extractor.extract_calculations(workbook)
        return extractor.workbook_data

    def test_source_declaration_conflicts_preserve_distinct_full_alternates(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_source_declaration_workbook(Path(temporary), [
                {"formula": "MIN([x]) + MIN([y])"},
                {"formula": "MAX([x]) + MAX([y])"},
                {"formula": "MIN([x]) + MIN([y])"},
                {},
            ])
            data = self._extract_source_declaration_workbook(path)
            self.assertEqual(len(data["calculations"]), 1)
            canonical = data["calculations"][0]
            self.assertIs(data["datasources"][0]["calculations"][0], canonical)
            self.assertEqual(canonical["formula"], "SUM([x]) + SUM([y])")
            expected = [{
                "name": "[WorksheetTotal]", "datasource_name": "Synthetic",
                "caption": "Coordinate Total", "formula": formula, "class": "tableau",
                "datatype": "real", "role": "measure", "type": "quantitative",
            } for formula in ("MIN([x]) + MIN([y])", "MAX([x]) + MAX([y])")]
            self.assertEqual(canonical["_source_declaration_conflicts"], expected)
            serialized = json.loads(json.dumps(data))
            for calculation in (serialized["calculations"][0],
                                serialized["datasources"][0]["calculations"][0]):
                self.assertEqual(calculation["formula"], "SUM([x]) + SUM([y])")
                self.assertEqual(calculation["_source_declaration_conflicts"], expected)

    def test_source_declaration_conflicts_detect_each_semantic_field(self):
        for key, value in (("formula", "MIN([x]) + MIN([y])"), ("class", "synthetic"),
                           ("datatype", "integer"), ("role", "dimension"),
                           ("type", "ordinal"), ("caption", "Alternate Caption")):
            with self.subTest(field=key), tempfile.TemporaryDirectory() as temporary:
                path = self._write_source_declaration_workbook(
                    Path(temporary), [{key: value}, {key: value}])
                data = self._extract_source_declaration_workbook(path)
                self.assertEqual(len(data["calculations"]), 1)
                canonical = data["calculations"][0]
                expected = {name: content for name, content in canonical.items()
                            if name != "_source_declaration_conflicts"}
                expected[key] = value
                self.assertEqual(canonical["_source_declaration_conflicts"], [expected])
                self.assertIs(data["datasources"][0]["calculations"][0], canonical)

    def test_source_declaration_conflicts_global_precedence_is_evidence_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = self._extract_scoped_worksheet_calculations(
                root, global_datasources=("Synthetic",))
            canonical = next(calculation for calculation in data["calculations"]
                             if calculation["datasource_name"] == "Synthetic")
            self.assertEqual(canonical["formula"], "SUM([x]) - SUM([y])")
            self.assertEqual(canonical["_source_declaration_conflicts"], [{
                "name": "[WorksheetTotal]", "datasource_name": "Synthetic",
                "caption": "Coordinate Total", "formula": "SUM([x]) + SUM([y])",
                "class": "tableau", "datatype": "real", "role": "measure",
                "type": "quantitative",
            }])
            self.assertIs(data["datasources"][0]["calculations"][0], canonical)
            contents = self._generate_fixed_tmdl(data["datasources"], [])
            _assert_unsupported(self, self._fixed_declaration(
                contents["Coordinates"], "WorksheetTotal", "Coordinate Total"),
                "conflicting source calculation declarations")
            self.assertEqual(self._fixed_declaration(
                contents["OtherCoordinates"], "WorksheetTotal", "Other Total"),
                "SUM('OtherCoordinates'[u]) * SUM('OtherCoordinates'[v])")

    def _assert_source_declaration_tmdl(self, content):
        affected = self._fixed_declaration(content, "WorksheetTotal", "Coordinate Total")
        _assert_unsupported(self, affected, "conflicting source calculation declarations")
        consumer = self._fixed_declaration(content, "Consumer", "Consumer")
        _assert_unsupported(self, consumer, "depend")
        self.assertEqual(self._fixed_declaration(content, "Unrelated", "Unrelated"),
                         "SUM('Coordinates'[x])")
        for caption in ("Coordinate Total", "Consumer"):
            declaration = re.search(
                rf"(?ms)^\tmeasure '?{re.escape(caption)}'? =.*?(?=^\t(?:measure|column) |\Z)",
                content)
            self.assertIsNotNone(declaration, content)
            self.assertIn("annotation MigrationNote =", declaration.group())
        self.assertIn("DAX conversion failed: conflicting source calculation declarations",
                      content)

    def test_source_declaration_conflicts_emit_diagnostics_without_model_abort(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_source_declaration_workbook(
                Path(temporary), [{"formula": "MIN([x]) + MIN([y])"}], dependents=True)
            data = self._extract_source_declaration_workbook(path)
            content = self._generate_fixed_tmdl(data["datasources"], [])["Coordinates"]
            self._assert_source_declaration_tmdl(content)

    def test_source_declaration_conflicts_dimension_emits_diagnostic_column(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_source_declaration_workbook(Path(temporary), [])
            workbook = ET.parse(path).getroot()
            column = workbook.find(
                "./worksheets/worksheet/table/view/datasource-dependencies/column")
            column.set("role", "dimension")
            column.find("calculation").set("formula", "[x] + [y]")
            alternate = deepcopy(workbook.find("./worksheets/worksheet"))
            alternate.set("name", "Alternate Dimension")
            alternate.find("./table/view/datasource-dependencies/column/calculation").set(
                "formula", "[x] - [y]")
            workbook.find("worksheets").append(alternate)
            ET.ElementTree(workbook).write(path, encoding="utf-8", xml_declaration=True)
            data = self._extract_source_declaration_workbook(path)
            content = self._generate_fixed_tmdl(data["datasources"], [])["Coordinates"]
            declaration = re.search(
                r"(?m)^\tcolumn 'Coordinate Total' =([^\n]*)", content)
            self.assertIsNotNone(declaration, content)
            expression = re.sub(r"^/\* Tableau calculation;[^\n]*?\*/\s*", "",
                                declaration.group(1).strip(), count=1)
            _assert_unsupported(self, expression,
                                "conflicting source calculation declarations")
            self.assertRegex(content, r'annotation MigrationNote = "?DAX conversion failed: '
                             r'conflicting source calculation declarations')

    def test_source_declaration_conflicts_duplicate_global_dimensions_canonicalize(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = self._write_worksheet_only_cli_workbook(Path(temporary))
            workbook = ET.parse(path).getroot()
            workbook.remove(workbook.find("worksheets"))
            datasource = workbook.find("./datasources/datasource")
            for formula in ("[x] + 1", "[x] + 2"):
                column = ET.SubElement(
                    datasource, "column", name="[GlobalDimension]",
                    caption="Global Dimension", role="dimension", datatype="real",
                    type="quantitative")
                ET.SubElement(column, "calculation", {"class": "tableau", "formula": formula})
            ET.ElementTree(workbook).write(path, encoding="utf-8", xml_declaration=True)
            data = self._extract_source_declaration_workbook(path)
            self.assertEqual(len(data["calculations"]), 1)
            self.assertEqual(len(data["datasources"][0]["calculations"]), 1)
            canonical = data["calculations"][0]
            self.assertIs(data["datasources"][0]["calculations"][0], canonical)
            self.assertEqual(canonical["formula"], "[x] + 1")
            self.assertEqual(canonical["_source_declaration_conflicts"], [{
                "name": "[GlobalDimension]", "datasource_name": "Synthetic",
                "caption": "Global Dimension", "formula": "[x] + 2",
                "class": "tableau", "datatype": "real", "role": "dimension",
                "type": "quantitative", "description": "",
            }])
            serialized = json.loads(json.dumps(data))
            self.assertEqual(serialized["datasources"][0]["calculations"],
                             serialized["calculations"])
            content = self._generate_fixed_tmdl(serialized["datasources"], [])["Coordinates"]
            self._assert_conflicted_tmdl_declaration(
                content, "Global Dimension", "conflicting source calculation declarations")
            self.assertNotRegex(content, r'Table\.AddColumn\([^\n]*"Global Dimension"')
            self.assertNotRegex(content, r'each\s+(?:\([^\n]*\))?\s*\[x\]\s*\+\s*2')

    def _literal_conflict_datasource(self, *, string=False):
        datasource = self._fixed_datasource(table="Coordinates")
        datasource["tables"][0]["columns"] = [{"name": "x", "datatype": "real"}]
        constant = {
            "name": "[K]", "caption": "K", "datasource_name": "Synthetic",
            "role": "measure", "datatype": "string" if string else "real",
            "formula": '"FIRST"' if string else "1", "class": "tableau",
            "type": "nominal" if string else "quantitative",
        }
        constant["_source_declaration_conflicts"] = [dict(
            constant, formula='"SECOND"' if string else "2")]
        consumers = (("Bare", "[K]", "measure"),) if string else (
            ("Arithmetic", "[x] + [K]", "dimension"),
            ("Aggregate", "SUM([K])", "measure"),
            ("Transitive", "[Arithmetic] + 10", "dimension"),
        )
        datasource["calculations"] = [constant] + [{
            "name": f"[{name}]", "caption": name, "datasource_name": "Synthetic",
            "role": role, "datatype": "string" if string else "real", "formula": formula,
        } for name, formula, role in consumers]
        return datasource

    def _assert_conflicted_tmdl_declaration(self, content, caption, *reason_words):
        declaration = re.search(
            rf"(?m)^\t(?:column|measure) (?:'{re.escape(caption)}'|{re.escape(caption)})"
            r" =([^\n]*)", content)
        self.assertIsNotNone(declaration, content)
        expression = re.sub(r"^/\* Tableau calculation;[^\n]*?\*/\s*", "",
                            declaration.group(1).strip(), count=1)
        _assert_unsupported(self, expression, *reason_words)
        self.assertNotRegex(content, rf'Table\.AddColumn\([^\n]*"{re.escape(caption)}"')

    def test_source_declaration_conflicts_numeric_literal_consumers_stay_diagnostic(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasource = self._literal_conflict_datasource()
                if reverse:
                    datasource["calculations"].reverse()
                content = self._generate_fixed_tmdl([datasource], [])["Coordinates"]
                self._assert_conflicted_tmdl_declaration(
                    content, "K", "conflicting source calculation declarations")
                for caption in ("Arithmetic", "Aggregate"):
                    self._assert_conflicted_tmdl_declaration(content, caption, "depend")
                self.assertNotRegex(content, r'each\s+\[x\]\s*\+\s*1')
                self.assertNotIn("COUNTROWS(", content)

    def test_source_declaration_conflicts_string_literal_bare_consumer_stays_diagnostic(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasource = self._literal_conflict_datasource(string=True)
                if reverse:
                    datasource["calculations"].reverse()
                content = self._generate_fixed_tmdl([datasource], [])["Coordinates"]
                self._assert_conflicted_tmdl_declaration(
                    content, "K", "conflicting source calculation declarations")
                self._assert_conflicted_tmdl_declaration(content, "Bare", "depend")
                self.assertNotRegex(content, r'(?m)=\s*"FIRST"\s*$')

    def test_source_declaration_conflicts_literal_dependency_is_transitive(self):
        datasource = self._literal_conflict_datasource()
        datasource["calculations"].reverse()
        content = self._generate_fixed_tmdl([datasource], [])["Coordinates"]
        self._assert_conflicted_tmdl_declaration(content, "Transitive", "depend")
        self.assertNotRegex(content, r'Table\.AddColumn\([^\n]*"(?:Arithmetic|Transitive)"')

    def test_source_declaration_conflicts_unrelated_literal_and_table_fold_survive(self):
        datasource = self._literal_conflict_datasource()
        datasource["calculations"].extend([{
            "name": f"[{name}]", "caption": name, "datasource_name": "Synthetic",
            "role": role, "datatype": "real", "formula": formula,
        } for name, formula, role in (
            ("ValidK", "3", "measure"),
            ("ValidArithmetic", "[x] + [ValidK]", "dimension"),
            ("ValidAggregate", "SUM([ValidK])", "measure"),
        )])
        content = self._generate_fixed_tmdl([datasource], [])["Coordinates"]
        self._assert_conflicted_tmdl_declaration(content, "Aggregate", "depend")
        self.assertRegex(content, r'Table\.AddColumn\([^\n]*"ValidArithmetic"[^\n]*\[x\]\s*\+\s*3')
        self.assertRegex(content, r'Table\.AddColumn\([^\n]*"ValidK",\s*each\s+3\b')
        self.assertEqual(self._fixed_declaration(content, "ValidAggregate", "ValidAggregate"),
                 "SUM('Coordinates'[ValidK])")

    def test_source_declaration_conflicts_foreign_literal_homonym_is_unaffected(self):
        datasource = self._literal_conflict_datasource()
        foreign = self._fixed_datasource(name="OtherSynthetic", table="OtherCoordinates")
        foreign["tables"][0]["columns"] = [{"name": "u", "datatype": "real"}]
        foreign["calculations"] = [{
            "name": "[K]", "caption": "Foreign K", "datasource_name": "OtherSynthetic",
            "role": "measure", "datatype": "string", "formula": '"FOREIGN"',
        }]
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                datasources = [deepcopy(datasource), deepcopy(foreign)]
                contents = self._generate_fixed_tmdl(
                    list(reversed(datasources)) if reverse else datasources, [])
                self._assert_conflicted_tmdl_declaration(
                    contents["Coordinates"], "K", "conflicting source calculation declarations")
                self.assertEqual(self._fixed_declaration(
                    contents["OtherCoordinates"], "K", "Foreign K"), '"FOREIGN"')
                self.assertNotIn(_FALLBACK, contents["OtherCoordinates"])

    def test_source_declaration_conflicts_override_verified_fixed_and_approximation(self):
        scope = TestReportedFixedGrainStatic._fixed_scope()
        for mode, extra in (("exact", {"verified_fixed_scopes": {
                "Synthetic": {"FixedHomes": scope}}}), ("approximation", {
                    "verified_fixed_scopes": {}, "worksheets": [{
                        "name": "Total", "dimensions": [], "fields": [{
                            "name": "[FixedHomes]", "datasource": "Synthetic",
                            "aggregation": "SUM", "aggregation_evidence": "explicit",
                            "aggregation_candidates": ["sum"]}]}]})):
            with self.subTest(mode=mode):
                datasource = self._fixed_datasource()
                control = self._generate_fixed_tmdl(
                    [deepcopy(datasource)], **{"worksheets": [], **extra})["Housing"]
                expression = self._fixed_declaration(control)
                self.assertNotIn("TODO:", expression)
                self.assertIn("SUMX(", expression)
                self.assertEqual("approximation" in expression, mode == "approximation")
                canonical = datasource["calculations"][0]
                canonical["_source_declaration_conflicts"] = [dict(
                    canonical, formula="{ FIXED [Sector] : MAX([Homes]) }")]
                content = self._generate_fixed_tmdl(
                    [datasource], **{"worksheets": [], **extra})["Housing"]
                _assert_unsupported(self, self._fixed_declaration(content),
                                    "conflicting source calculation declarations")
                self.assertNotIn("approximation", self._fixed_declaration(content))
                self.assertRegex(content, r'annotation MigrationNote = "?DAX conversion failed: '
                                 r'conflicting source calculation declarations')

    def test_worksheet_only_same_raw_id_tmdl_keeps_each_physical_table(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = self._extract_scoped_worksheet_calculations(root)
            self._assert_scoped_worksheet_calculations(data, "SUM([x]) + SUM([y])")
            model_dir = root / "Synthetic.SemanticModel"
            generate_tmdl(data["datasources"], "Synthetic", {}, str(model_dir),
                          parameterize=False)
            for table, caption, expected in (
                    ("Coordinates", "Coordinate Total",
                     "SUM('Coordinates'[x]) + SUM('Coordinates'[y])"),
                    ("OtherCoordinates", "Other Total",
                     "SUM('OtherCoordinates'[u]) * SUM('OtherCoordinates'[v])")):
                with self.subTest(table=table):
                    content = (model_dir / "definition" / "tables" /
                               f"{table}.tmdl").read_text(encoding="utf-8")
                    expression = self._fixed_declaration(
                        content, raw_name="WorksheetTotal", caption=caption)
                    self.assertEqual(expression, expected)

    def _run_batch_cli(self, root, input_dir, output_dir, expected_returncode=0):
        repository = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, str(repository / "migrate.py"), "--batch", str(input_dir),
             "--output-dir", str(output_dir)],
            cwd=root, env={**os.environ, "PYTHONIOENCODING": "utf-8",
                           "TTPBI_VERIFIED_FIXED_SCOPES": "{}"},
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120)
        self.assertEqual(result.returncode, expected_returncode, result.stdout + result.stderr)

    def test_worksheet_only_calculation_keeps_dependency_datasource_identity(self):
        from tableau_export.extract_tableau_data import TableauExtractor

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self._write_worksheet_only_cli_workbook(root)
            workbook = ET.parse(path).getroot()
            self.assertEqual(workbook.findall("./datasources/datasource/column/calculation"), [])
            extractor = TableauExtractor(str(path), output_dir=str(root / "extract"))
            extractor.extract_datasources(workbook)
            extractor.extract_calculations(workbook)
            global_calculations = extractor.workbook_data["calculations"]
            self.assertEqual(len(global_calculations), 1)
            self.assertEqual(global_calculations[0]["name"], "[WorksheetTotal]")
            self.assertEqual(global_calculations[0]["datasource_name"], "Synthetic")
            self.assertEqual(global_calculations[0]["formula"], "SUM([x]) + SUM([y])")
            datasources = extractor.workbook_data["datasources"]
            self.assertEqual([item["name"] for item in datasources], ["Synthetic"])
            self.assertEqual(datasources[0]["calculations"], global_calculations)
            self.assertEqual(datasources[0]["calculations"][0]["datasource_name"], "Synthetic")

    def test_worksheet_only_unknown_dependency_datasource_is_not_rebound(self):
        from tableau_export.extract_tableau_data import TableauExtractor

        for dependency_datasource in ("UnknownSynthetic", "", None):
            with self.subTest(datasource=dependency_datasource), \
                    tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                path = self._write_worksheet_only_cli_workbook(
                    root, dependency_datasource or "")
                workbook = ET.parse(path).getroot()
                if dependency_datasource is None:
                    workbook.find("./worksheets/worksheet/table/view/"
                                  "datasource-dependencies").attrib.pop("datasource")
                extractor = TableauExtractor(str(path), output_dir=str(root / "extract"))
                extractor.extract_datasources(workbook)
                extractor.extract_calculations(workbook)
                calculations = extractor.workbook_data["calculations"]
                self.assertEqual(len(calculations), 1)
                self.assertEqual(calculations[0]["name"], "[WorksheetTotal]")
                self.assertEqual(calculations[0]["datasource_name"],
                                 dependency_datasource or "")
                self.assertEqual(calculations[0]["formula"], "SUM([x]) + SUM([y])")
                datasources = extractor.workbook_data["datasources"]
                self.assertEqual([item["name"] for item in datasources], ["Synthetic"])
                for datasource in datasources:
                    self.assertEqual(datasource.get("calculations", []), [])

    def test_worksheet_only_sum_survives_ordinary_batch_cli(self):
        from powerbi_import.openability import check_openability

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            output_dir = root / "output"
            self._write_worksheet_only_cli_workbook(input_dir)
            self._run_batch_cli(root, input_dir, output_dir)
            project = output_dir / "SyntheticWorksheet"
            self.assertTrue((project / "SyntheticWorksheet.pbip").is_file())
            persisted_gate = json.loads(
                (project / "openability_report.json").read_text(encoding="utf-8"))
            for gate in (persisted_gate, check_openability(str(project)).to_dict()):
                self.assertTrue(gate["openable"], gate)
                self.assertEqual(gate["blocking_issues"], [])
            snapshot = output_dir / ".migration_extract" / "SyntheticWorksheet.twb"
            calculations = json.loads(
                (snapshot / "calculations.json").read_text(encoding="utf-8"))
            datasources = json.loads(
                (snapshot / "datasources.json").read_text(encoding="utf-8"))
            self.assertEqual(len(calculations), 1)
            self.assertEqual(calculations[0]["datasource_name"], "Synthetic")
            self.assertEqual(datasources[0]["calculations"], calculations)
            content = (project / "SyntheticWorksheet.SemanticModel" / "definition" /
                       "tables" / "Coordinates.tmdl").read_text(encoding="utf-8")
            expression = self._fixed_declaration(
                content, raw_name="WorksheetTotal", caption="Coordinate Total")
            self.assertEqual(expression, "SUM('Coordinates'[x]) + SUM('Coordinates'[y])")
            self.assertNotIn("BLANK()", expression)
            self.assertNotIn(_FALLBACK, expression)

    def test_source_declaration_conflicts_ordinary_batch_retains_diagnostic_project(self):
        from powerbi_import.openability import check_openability

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            output_dir = root / "output"
            self._write_source_declaration_workbook(
                input_dir, [{"formula": "MIN([x]) + MIN([y])"}], dependents=True)
            self._run_batch_cli(root, input_dir, output_dir, expected_returncode=7)
            project = output_dir / "SyntheticWorksheet"
            self.assertTrue((project / "SyntheticWorksheet.pbip").is_file())
            content = (project / "SyntheticWorksheet.SemanticModel" / "definition" /
                       "tables" / "Coordinates.tmdl").read_text(encoding="utf-8")
            self._assert_source_declaration_tmdl(content)
            persisted_gate = json.loads(
                (project / "openability_report.json").read_text(encoding="utf-8"))
            for gate in (persisted_gate, check_openability(str(project)).to_dict()):
                self.assertFalse(gate["openable"], gate)
                self.assertTrue(gate["blocking_issues"], gate)
            snapshot = output_dir / ".migration_extract" / "SyntheticWorksheet.twb"
            calculations = json.loads(
                (snapshot / "calculations.json").read_text(encoding="utf-8"))
            datasources = json.loads(
                (snapshot / "datasources.json").read_text(encoding="utf-8"))
            canonical = next(item for item in calculations if item["name"] == "[WorksheetTotal]")
            self.assertEqual(canonical["formula"], "SUM([x]) + SUM([y])")
            self.assertEqual(canonical["_source_declaration_conflicts"], [{
                "name": "[WorksheetTotal]", "datasource_name": "Synthetic",
                "caption": "Coordinate Total", "formula": "MIN([x]) + MIN([y])",
                "class": "tableau", "datatype": "real", "role": "measure",
                "type": "quantitative",
            }])
            self.assertEqual(datasources[0]["calculations"], calculations)
            quality = json.loads((output_dir / "migration_quality_SyntheticWorksheet.json")
                                 .read_text(encoding="utf-8"))
            self.assertEqual(quality["status"], "FAIL", quality)
            environment = quality["evidence_manifest"]["environment"]
            self.assertEqual(environment["desktop"]["status"], "not_run", environment)
            for stage in ("semantic_execution", "refresh"):
                self.assertEqual(environment[stage], "not_run", environment)
            summary = json.loads((output_dir / "batch_quality_summary.json")
                                 .read_text(encoding="utf-8"))
            self.assertEqual(summary["generation_counts"]["complete"], 0, summary)
            self.assertEqual(list(output_dir.rglob("*desktop*.json")), [])
            self.assertEqual(list(output_dir.rglob("*native*.json")), [])
            self.assertEqual(list(output_dir.rglob("*.png")), [])

    def test_r4_fixed_explicit_sum_survives_ordinary_batch_cli(self):
        from powerbi_import.openability import check_openability

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            input_dir.mkdir()
            output_dir = root / "output"
            source = input_dir / "Housing.csv"
            source.write_text("Sector,Homes\nA,12\nA,12\nB,7\n", encoding="utf-8")
            workbook = ET.Element("workbook", version="18.1")
            datasource = ET.SubElement(
                ET.SubElement(workbook, "datasources"), "datasource",
                name="Synthetic", caption="Synthetic", inline="true")
            connection = ET.SubElement(datasource, "connection", {
                "class": "textscan", "directory": str(input_dir),
                "filename": source.name, "separator": ",", "header": "yes",
            })
            relation = ET.SubElement(
                connection, "relation", type="table", name="Housing",
                table="[Housing.csv]")
            columns = ET.SubElement(relation, "columns", header="yes", separator=",")
            records = ET.SubElement(connection, "metadata-records")
            for ordinal, (name, datatype, role) in enumerate((
                    ("Sector", "string", "dimension"), ("Homes", "real", "measure"))):
                ET.SubElement(columns, "column", name=name, datatype=datatype,
                              ordinal=str(ordinal))
                ET.SubElement(datasource, "column", name=f"[{name}]",
                              datatype=datatype, role=role)
                record = ET.SubElement(records, "metadata-record", {"class": "column"})
                for tag, value in (("local-name", f"[{name}]"), ("remote-name", name),
                                   ("parent-name", "[Housing]"), ("local-type", datatype)):
                    ET.SubElement(record, tag).text = value
            calculation = ET.SubElement(
                datasource, "column", name="[FixedHomes]", caption="Homes by Sector",
                role="measure", datatype="real", type="quantitative")
            ET.SubElement(calculation, "calculation", {
                "class": "tableau", "formula": "{ FIXED [Sector] : MIN([Homes]) }",
            })
            worksheet = ET.SubElement(
                ET.SubElement(workbook, "worksheets"), "worksheet", name="Housing Total")
            table = ET.SubElement(worksheet, "table")
            view = ET.SubElement(table, "view")
            ET.SubElement(ET.SubElement(view, "datasources"), "datasource",
                          name="Synthetic", caption="Synthetic")
            dependencies = ET.SubElement(view, "datasource-dependencies", datasource="Synthetic")
            dependencies.append(deepcopy(calculation))
            ET.SubElement(dependencies, "column-instance", {
                "column": "[FixedHomes]", "derivation": "Sum",
                "name": "[sum:FixedHomes:qk]", "type": "quantitative",
            })
            ET.SubElement(table, "cols")
            ET.SubElement(table, "rows").text = "[Synthetic].[sum:FixedHomes:qk]"
            ET.SubElement(ET.SubElement(ET.SubElement(table, "panes"), "pane"),
                          "mark", {"class": "Text"})
            ET.ElementTree(workbook).write(
                input_dir / "SyntheticR4.twb", encoding="utf-8", xml_declaration=True)

            self._run_batch_cli(root, input_dir, output_dir)
            project = output_dir / "SyntheticR4"
            self.assertTrue((project / "SyntheticR4.pbip").is_file())
            persisted_gate = json.loads(
                (project / "openability_report.json").read_text(encoding="utf-8"))
            self.assertTrue(persisted_gate["openable"], persisted_gate)
            self.assertEqual(persisted_gate["blocking_issues"], [])
            live_gate = check_openability(str(project)).to_dict()
            self.assertTrue(live_gate["openable"], live_gate)
            self.assertEqual(live_gate["blocking_issues"], [])

            snapshot = output_dir / ".migration_extract" / "SyntheticR4.twb"
            worksheets = json.loads((snapshot / "worksheets.json").read_text(encoding="utf-8"))
            self.assertEqual(len(worksheets), 1)
            self.assertFalse(worksheets[0].get("dimensions"))
            fields = [field for field in worksheets[0]["fields"]
                      if field["name"].strip("[]") == "FixedHomes"]
            self.assertEqual(len(fields), 1)
            self.assertEqual(fields[0]["datasource"], "Synthetic")
            self.assertEqual(fields[0]["aggregation"].lower(), "sum")
            self.assertEqual(fields[0]["aggregation_evidence"], "explicit")
            self.assertEqual(fields[0]["aggregation_candidates"], ["sum"])

            content = (project / "SyntheticR4.SemanticModel" / "definition" /
                       "tables" / "Housing.tmdl").read_text(encoding="utf-8")
            expression = self._fixed_declaration(content)
            note = "shelf (sum:) approximation; source filter scope and totals unverified"
            self.assertEqual(expression,
                f"/* {note} */ SUMX(VALUES('Housing'[Sector]), "
                "CALCULATE(MIN('Housing'[Homes])))")
            self.assertNotIn("BLANK()", expression)
            self.assertNotIn("ALLEXCEPT", expression)
            self.assertIn('annotation MigrationNote = "FIXED ' + note + '"', content)
            pages = [json.loads(path.read_text(encoding="utf-8")) for path in
                     (project / "SyntheticR4.Report" / "definition" / "pages").glob("*/page.json")]
            self.assertEqual(sum(page["displayName"] == "Housing Total" for page in pages), 1)
            quality = json.loads(
                (output_dir / "migration_quality_SyntheticR4.json").read_text(encoding="utf-8"))
            environment = quality["evidence_manifest"]["environment"]
            self.assertEqual(environment["desktop"]["status"], "not_run", environment)
            for stage in ("semantic_execution", "refresh"):
                self.assertEqual(environment[stage], "not_run", environment)
            self.assertEqual(list(output_dir.rglob("*desktop*.json")), [])
            self.assertEqual(list(output_dir.rglob("*native*.json")), [])
            self.assertEqual(list(output_dir.rglob("*.png")), [])

    @staticmethod
    def _fixed_datasource(name="Synthetic", table="Housing"):
        return {
            "name": name, "caption": name + " Caption", "id": name + "-id",
            "luid": name + "-luid",
            "connection": {"type": "SQL Server", "details": {
                "server": "synthetic.invalid", "database": "synthetic"}},
            "tables": [{"name": table, "columns": [
                {"name": "Sector", "datatype": "string"},
                {"name": "Homes", "datatype": "real"}]}],
            "calculations": [{
                "name": "[FixedHomes]", "caption": "Homes by Sector",
                "datasource_name": name, "role": "measure", "datatype": "real",
                "formula": "{ FIXED [Sector] : MIN([Homes]) }"}],
            "columns": [], "relationships": [], "connection_map": {},
        }

    def _generate_fixed_tmdl(self, datasources, worksheets, **extra):
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary) / "Synthetic.SemanticModel"
            objects = {"worksheets": worksheets, **extra}
            with patch.dict(os.environ, {"TTPBI_VERIFIED_FIXED_SCOPES": "{}"}):
                generate_tmdl(datasources, "Synthetic", objects, str(model_dir),
                              parameterize=False)
            return {path.stem: path.read_text(encoding="utf-8")
                    for path in (model_dir / "definition" / "tables").glob("*.tmdl")}

    def _fixed_declaration(self, content, raw_name="FixedHomes", caption="Homes by Sector"):
        quoted_name = rf"'{re.escape(caption)}(?: \({re.escape(raw_name)}\))?'"
        declaration = re.search(
            rf"(?m)^\tmeasure (?:{quoted_name}|{re.escape(caption)}) =([^\n]*)",
            content)
        self.assertIsNotNone(declaration, content)
        return re.sub(r"^/\* Tableau calculation;[^\n]*?\*/\s*", "",
                      declaration.group(1).strip(), count=1)

    def test_tmdl_generation_wires_include_shelf_and_keeps_unproven_diagnostic(self):
        datasource = self._fixed_datasource()
        datasource["tables"][0]["columns"] = [
            {"name": "Sector", "datatype": "string"},
            {"name": "Homes", "datatype": "real"},
            {"name": "Price", "datatype": "real"},
        ]
        datasource["calculations"] = [
            {"name": "[IncludedHomes]", "caption": "Homes by Sector",
             "datasource_name": "Synthetic", "role": "measure",
             "datatype": "real",
             "formula": "{INCLUDE [Sector] : SUM([Homes])}"},
            {"name": "[IncludedRevenue]", "caption": "Revenue by Sector",
             "datasource_name": "Synthetic", "role": "measure",
             "datatype": "real",
             "formula": "{INCLUDE [Sector] : SUM([Homes] * [Price])}"},
            {"name": "[ExcludedHomes]", "caption": "Homes excl Sector",
             "datasource_name": "Synthetic", "role": "measure",
             "datatype": "real",
             "formula": "{EXCLUDE [Sector] : SUM([Homes])}"},
            {"name": "[UnprovenInclude]", "caption": "Unproven include",
             "datasource_name": "Synthetic", "role": "measure",
             "datatype": "real",
             "formula": "{INCLUDE [Sector] : SUM([Homes])}"},
        ]
        shelf_field = lambda name: {
            "name": name, "datasource": "Synthetic", "aggregation": "SUM",
            "aggregation_evidence": "explicit",
            "aggregation_candidates": ["sum"],
        }
        worksheets = [{
            "name": "LOD shelf evidence",
            "fields": [shelf_field("[IncludedHomes]"),
                       shelf_field("[IncludedRevenue]"),
                       shelf_field("[ExcludedHomes]")],
        }]

        content = self._generate_fixed_tmdl([datasource], worksheets)["Housing"]
        self.assertEqual(self._fixed_declaration(
            content, "IncludedHomes", "Homes by Sector"),
            "/* shelf (sum:) INCLUDE approximation; source filters and totals "
            "unverified */ SUMX(VALUES('Housing'[Sector]), "
            "CALCULATE(SUM('Housing'[Homes])))")
        revenue = self._fixed_declaration(
            content, "IncludedRevenue", "Revenue by Sector")
        self.assertIn("shelf (sum:) INCLUDE approximation", revenue)
        self.assertIn("SUMX(VALUES('Housing'[Sector])", revenue)
        self.assertIn("CALCULATE(SUMX('Housing',", revenue)
        self.assertIn(
            "CALCULATE(SUM('Housing'[Homes]), REMOVEFILTERS('Housing'[Sector]))",
            self._fixed_declaration(content, "ExcludedHomes", "Homes excl Sector"))
        _assert_unsupported(
            self, self._fixed_declaration(content, "UnprovenInclude", "Unproven include"),
            "INCLUDE", "explicit", "shelf")

    def test_r4_fixed_sum_shelf_without_view_dimensions_survives_tmdl_postprocess(self):
        from powerbi_import.dax_validator import validate_dax_expression

        worksheets = [{"name": "Synthetic Total", "dimensions": [], "fields": [{
            "name": "[FixedHomes]", "datasource": "Synthetic", "aggregation": "SUM"}]}]
        with self.assertLogs("powerbi_import.tmdl_generator", level="WARNING") as logs:
            content = self._generate_fixed_tmdl(
                [self._fixed_datasource()], worksheets)["Housing"]
        expression = self._fixed_declaration(content)
        note = ("shelf (sum:) approximation; source filter scope and totals unverified")
        self.assertEqual(expression,
            f"/* {note} */ SUMX(VALUES('Housing'[Sector]), "
            "CALCULATE(MIN('Housing'[Homes])))")
        self.assertIn('annotation MigrationNote = "FIXED ' + note + '"', content)
        self.assertTrue(any("FIXED " + note in message for message in logs.output),
                        logs.output)
        self.assertNotIn("BLANK()", expression)
        self.assertNotIn("ALLEXCEPT", content)
        self.assertEqual(validate_dax_expression(expression), [])

    def test_fixed_missing_or_ambiguous_shelf_evidence_serializes_blank(self):
        valid = {"name": "[FixedHomes]", "datasource": "Synthetic",
                 "aggregation": "SUM"}
        cases = {
            "no usage": [],
            "missing aggregation": [{key: value for key, value in valid.items()
                                     if key != "aggregation"}],
            "missing datasource": [{key: value for key, value in valid.items()
                                    if key != "datasource"}],
            "unknown datasource": [{**valid, "datasource": "Unknown"}],
            "unknown calculation": [{**valid, "name": "[Unknown]"}],
            "attr": [{**valid, "aggregation": "ATTR"}],
            "sum and avg": [valid, {**valid, "aggregation": "AVG"}],
            "sum and missing": [valid, {key: value for key, value in valid.items()
                                        if key != "aggregation"}],
            "sum and attr": [valid, {**valid, "aggregation": "ATTR"}],
        }
        for label, fields in cases.items():
            with self.subTest(evidence=label):
                worksheets = [{"name": f"Synthetic {index}", "fields": [field]}
                              for index, field in enumerate(fields)]
                content = self._generate_fixed_tmdl(
                    [self._fixed_datasource()], worksheets)["Housing"]
                _assert_unsupported(self, self._fixed_declaration(content), "FIXED")
                self.assertNotIn("shelf (sum:) approximation", content)
                self.assertNotIn("ALLEXCEPT", content)

    def test_fixed_datasource_and_calculation_caption_aliases_reach_emitted_tmdl(self):
        for datasource_key in ("name", "caption", "id", "luid"):
            for calculation_alias in ("[FixedHomes]", "FixedHomes", "Homes by Sector"):
                with self.subTest(datasource_key=datasource_key,
                                  calculation_alias=calculation_alias):
                    datasource = self._fixed_datasource()
                    worksheets = [{"name": "Synthetic Total", "fields": [{
                        "name": calculation_alias,
                        "datasource": "[" + datasource[datasource_key] + "]",
                        "aggregation": "sum"}]}]
                    content = self._generate_fixed_tmdl(
                        [datasource], worksheets)["Housing"]
                    self.assertIn("SUMX(VALUES('Housing'[Sector])",
                                  self._fixed_declaration(content))
                    self.assertIn('annotation MigrationNote = "FIXED shelf (sum:)', content)

    def test_fixed_same_caption_in_different_datasources_does_not_share_shelf(self):
        first = self._fixed_datasource()
        second = self._fixed_datasource("OtherSynthetic", "OtherHousing")
        second["tables"][0]["columns"] = [
            {"name": "OtherSector", "datatype": "string"},
            {"name": "OtherHomes", "datatype": "real"}]
        second["calculations"][0]["name"] = "[OtherFixedHomes]"
        second["calculations"][0]["formula"] = (
            "{ FIXED [OtherSector] : MIN([OtherHomes]) }")
        for second_shelf in (None, "AVG"):
            with self.subTest(second_shelf=second_shelf):
                worksheets = [{"name": "Synthetic Sum", "fields": [{
                    "name": "Homes by Sector", "datasource": "Synthetic Caption",
                    "aggregation": "SUM"}]}]
                if second_shelf:
                    worksheets.append({"name": "Synthetic Average", "fields": [{
                        "name": "Homes by Sector", "datasource": "OtherSynthetic-luid",
                        "aggregation": second_shelf}]})
                contents = self._generate_fixed_tmdl(
                    deepcopy([first, second]), worksheets)
                self.assertIn("SUMX(VALUES('Housing'[Sector])",
                              self._fixed_declaration(contents["Housing"]))
                other = self._fixed_declaration(contents["OtherHousing"], "OtherFixedHomes")
                if second_shelf:
                    self.assertIn("shelf (avg:) approximation", other)
                    self.assertIn("AVERAGEX(VALUES('OtherHousing'[OtherSector])", other)
                    self.assertIn("CALCULATE(MIN('OtherHousing'[OtherHomes]))", other)
                    self.assertNotIn("shelf (sum:)", other)
                else:
                    _assert_unsupported(self, other, "FIXED", "grain")

    def test_fixed_second_datasource_owns_identically_named_physical_columns(self):
        first = self._fixed_datasource()
        first["calculations"] = []
        second = self._fixed_datasource("OtherSynthetic", "OtherHousing")
        worksheets = [{"name": "Second Average", "fields": [{
            "name": "[FixedHomes]", "datasource": "OtherSynthetic",
            "aggregation": "AVG"}]}]
        contents = self._generate_fixed_tmdl([first, second], worksheets)
        expression = self._fixed_declaration(contents["OtherHousing"])
        self.assertEqual(expression,
            "/* shelf (avg:) approximation; source filter scope and totals "
            "unverified */ AVERAGEX(VALUES('OtherHousing'[Sector]), "
            "CALCULATE(MIN('OtherHousing'[Homes])))")
        self.assertNotIn("'Housing'[", expression)
        self.assertNotIn("measure 'Homes by Sector'", contents["Housing"])

    def test_fixed_foreign_measure_caption_does_not_shadow_local_physical_key(self):
        local = self._fixed_datasource()
        foreign = self._fixed_datasource("OtherSynthetic", "OtherHousing")
        foreign["calculations"] = [{
            "name": "[SectorMeasure]", "caption": "Sector",
            "datasource_name": "OtherSynthetic", "role": "MEASURE",
            "datatype": "real", "formula": "SUM([Homes])"}]
        worksheets = [{"name": "Local Average", "fields": [{
            "name": "[FixedHomes]", "datasource": "Synthetic",
            "aggregation": "AVG", "aggregation_evidence": "explicit",
            "aggregation_candidates": ["avg"]}]}]
        contents = self._generate_fixed_tmdl([local, foreign], worksheets)
        content = contents["Housing"]
        expression = self._fixed_declaration(content)
        note = "shelf (avg:) approximation; source filter scope and totals unverified"
        self.assertEqual(expression,
            f"/* {note} */ AVERAGEX(VALUES('Housing'[Sector]), "
            "CALCULATE(MIN('Housing'[Homes])))")
        self.assertIn('annotation MigrationNote = "FIXED ' + note + '"', content)
        self.assertNotIn("BLANK()", expression)
        self.assertNotIn("ALLEXCEPT", expression)
        self.assertEqual(self._fixed_declaration(
            contents["OtherHousing"], "SectorMeasure", "Sector (Measure)"),
            "SUM('OtherHousing'[Homes])")

    def test_fixed_local_measure_caption_collision_refuses_physical_key(self):
        datasource = self._fixed_datasource()
        datasource["calculations"].append({
            "name": "[SectorMeasure]", "caption": "Sector",
            "datasource_name": "Synthetic", "role": "MEASURE",
            "datatype": "real", "formula": "SUM([Homes])"})
        worksheets = [{"name": "Local Average", "fields": [{
            "name": "[FixedHomes]", "datasource": "Synthetic",
            "aggregation": "AVG", "aggregation_evidence": "explicit",
            "aggregation_candidates": ["avg"]}]}]
        content = self._generate_fixed_tmdl([datasource], worksheets)["Housing"]
        expression = self._fixed_declaration(content)
        _assert_unsupported(self, expression, "FIXED", "physical")
        self.assertNotIn("AVERAGEX", expression)
        self.assertNotIn("approximation;", expression)

    def test_fixed_same_datasource_one_hop_dependency_and_reference_shelf(self):
        for shelf, iterator in (("SUM", "SUMX"), ("AVG", "AVERAGEX"),
                                ("MIN", "MINX"), ("MAX", "MAXX")):
            for outer in (None, "SUM", "AVG", "MIN", "MAX"):
                formula = f"{outer}([FixedHomes])" if outer else "[FixedHomes]"
                with self.subTest(shelf=shelf, formula=formula):
                    datasource = self._fixed_datasource()
                    datasource["calculations"].append({
                        "name": "[Consumer]", "caption": "Consumer",
                        "datasource_name": "Synthetic", "role": "measure",
                        "datatype": "real", "formula": formula})
                    worksheets = [{"name": "Consumer Total", "fields": [{
                        "name": "[Consumer]", "datasource": "Synthetic",
                        "aggregation": shelf, "aggregation_evidence": "explicit",
                        "aggregation_candidates": [shelf.lower()]}]}]
                    content = self._generate_fixed_tmdl(
                        [datasource], worksheets)["Housing"]
                    fixed = self._fixed_declaration(content)
                    consumer = self._fixed_declaration(content, "Consumer", "Consumer")
                    if outer is not None and outer != shelf:
                        _assert_unsupported(self, fixed, "FIXED")
                        _assert_unsupported(self, consumer)
                        self.assertNotIn("approximation;", content)
                    else:
                        expected = (
                            f"/* shelf ({shelf.lower()}:) approximation; source filter "
                            f"scope and totals unverified */ {iterator}("
                            "VALUES('Housing'[Sector]), "
                            "CALCULATE(MIN('Housing'[Homes])))")
                        self.assertEqual(fixed, expected)
                        self.assertEqual(consumer, expected)

    def test_fixed_dependency_cycles_and_longer_chains_do_not_infer_shelf(self):
        cases = {
            "self cycle": [("Consumer", "SUM([Consumer])")],
            "mutual cycle": [("Consumer", "[Other]"), ("Other", "[Consumer]")],
            "reference chain": [("Other", "[FixedHomes]"),
                                ("Consumer", "[Other]")],
            "aggregate chain": [("Other", "SUM([FixedHomes])"),
                                ("Consumer", "SUM([Other])")],
        }
        for label, declarations in cases.items():
            with self.subTest(dependency=label):
                datasource = self._fixed_datasource()
                datasource["calculations"].extend({
                    "name": f"[{name}]", "caption": name,
                    "datasource_name": "Synthetic", "role": "measure",
                    "datatype": "real", "formula": formula,
                } for name, formula in declarations)
                worksheets = [{"name": "Consumer Total", "fields": [{
                    "name": "[Consumer]", "datasource": "Synthetic",
                    "aggregation": "SUM"}]}]
                content = self._generate_fixed_tmdl([datasource], worksheets)["Housing"]
                _assert_unsupported(self, self._fixed_declaration(content), "FIXED")
                self.assertNotIn("approximation;", content)
                self.assertNotIn("SUMX(VALUES(", content)

    def test_fixed_ambiguous_dependency_target_does_not_infer_shelf(self):
        datasource = self._fixed_datasource()
        datasource["calculations"].append(dict(
            datasource["calculations"][0], name="[OtherFixedHomes]"))
        datasource["calculations"].append({
            "name": "[Consumer]", "caption": "Consumer",
            "datasource_name": "Synthetic", "role": "measure", "datatype": "real",
            "formula": "SUM([Homes by Sector])"})
        worksheets = [{"name": "Consumer Total", "fields": [{
            "name": "[Consumer]", "datasource": "Synthetic", "aggregation": "SUM"}]}]
        content = self._generate_fixed_tmdl([datasource], worksheets)["Housing"]
        _assert_unsupported(self, self._fixed_declaration(content), "FIXED")
        _assert_unsupported(self, self._fixed_declaration(
            content, "Consumer", "Consumer"))
        self.assertNotIn("approximation;", content)

    def test_fixed_cross_datasource_dependency_does_not_infer_shelf(self):
        first = self._fixed_datasource()
        second = self._fixed_datasource("OtherSynthetic", "OtherHousing")
        second["calculations"] = [{
            "name": "[Consumer]", "caption": "Consumer",
            "datasource_name": "OtherSynthetic", "role": "measure",
            "datatype": "real", "formula": "SUM([FixedHomes])"}]
        worksheets = [{"name": "Foreign Consumer", "fields": [{
            "name": "[Consumer]", "datasource": "OtherSynthetic",
            "aggregation": "SUM"}]}]
        contents = self._generate_fixed_tmdl([first, second], worksheets)
        _assert_unsupported(self, self._fixed_declaration(contents["Housing"]), "FIXED")
        _assert_unsupported(self, self._fixed_declaration(
            contents["OtherHousing"], "Consumer", "Consumer"))
        self.assertNotIn("approximation;", "\n".join(contents.values()))

    def test_fixed_extracted_measure_values_conflict_prevents_approximation(self):
        for derivations in (("Sum", "Avg"), ("Avg", "Sum")):
            with self.subTest(derivations=derivations):
                worksheet = ET.fromstring("""
                    <worksheet name="Conflicting Measure Values"><table>
                      <cols>[Synthetic].[:Measure Names]</cols>
                      <rows>[Synthetic].[Multiple Values]</rows>
                      <datasource-dependencies datasource="Synthetic">
                        <column name="[FixedHomes]" role="measure" />
                      </datasource-dependencies>
                    </table></worksheet>""")
                dependencies = worksheet.find("./table/datasource-dependencies")
                for derivation in derivations:
                    ET.SubElement(dependencies, "column-instance", {
                        "column": "[FixedHomes]", "derivation": derivation,
                        "name": f"[{derivation.lower()}:FixedHomes:qk]"})
                fields = extract_worksheet_fields(worksheet)
                fixed_fields = [field for field in fields if field["name"] == "FixedHomes"]
                self.assertEqual(len(fixed_fields), 1)
                field = fixed_fields[0]
                self.assertEqual(field["aggregation_evidence"], "explicit")
                self.assertEqual(field["aggregation_candidates"],
                                 [value.lower() for value in derivations])
                self.assertEqual(field["aggregation"], derivations[0].lower())
                content = self._generate_fixed_tmdl([self._fixed_datasource()], [{
                    "name": worksheet.get("name"), "fields": fields}])["Housing"]
                _assert_unsupported(self, self._fixed_declaration(content), "FIXED")
                self.assertNotIn("approximation;", content)

    def test_fixed_extracted_measure_values_without_aggregation_is_not_evidence(self):
        for instances, evidence in (("", "inferred"), (
                '<column-instance column="[FixedHomes]" derivation="User" '
                'name="[usr:FixedHomes:qk]" />', "unspecified")):
            with self.subTest(evidence=evidence):
                worksheet = ET.fromstring(f"""
                    <worksheet name="Unspecified Measure Values"><table>
                      <cols>[Synthetic].[:Measure Names]</cols>
                      <rows>[Synthetic].[Multiple Values]</rows>
                      <datasource-dependencies datasource="Synthetic">
                        <column name="[FixedHomes]" role="measure" />
                        {instances}
                      </datasource-dependencies>
                    </table></worksheet>""")
                fields = extract_worksheet_fields(worksheet)
                fixed_fields = [field for field in fields if field["name"] == "FixedHomes"]
                self.assertEqual(len(fixed_fields), 1)
                field = fixed_fields[0]
                self.assertEqual(field["aggregation_candidates"], ["unspecified"])
                self.assertEqual(field["aggregation_evidence"], evidence)
                if evidence == "inferred":
                    self.assertEqual(field["aggregation"], "sum")
                else:
                    self.assertNotIn("aggregation", field)
                content = self._generate_fixed_tmdl([self._fixed_datasource()], [{
                    "name": worksheet.get("name"), "fields": fields}])["Housing"]
                _assert_unsupported(self, self._fixed_declaration(content), "FIXED")
                self.assertNotIn("approximation;", content)

    def test_fixed_extracted_normal_sum_prefix_enables_approximation(self):
        worksheet = ET.fromstring("""
            <worksheet name="Explicit Sum"><table>
              <rows>[Synthetic].[sum:FixedHomes:qk]</rows>
            </table></worksheet>""")
        fields = extract_worksheet_fields(worksheet)
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0]["name"], "FixedHomes")
        self.assertEqual(fields[0]["aggregation"], "sum")
        self.assertEqual(fields[0]["aggregation_evidence"], "explicit")
        self.assertEqual(fields[0]["aggregation_candidates"], ["sum"])
        content = self._generate_fixed_tmdl([self._fixed_datasource()], [{
            "name": worksheet.get("name"), "fields": fields}])["Housing"]
        self.assertEqual(self._fixed_declaration(content),
            "/* shelf (sum:) approximation; source filter scope and totals "
            "unverified */ SUMX(VALUES('Housing'[Sector]), "
            "CALCULATE(MIN('Housing'[Homes])))")

    def test_unsupported_makepoint_measure_is_retained_with_diagnostic(self):
        table = {
            "name": "Zone",
            "columns": [{"name": "Secteur", "datatype": "string"}],
        }
        calculation = {
            "name": "[Point]", "caption": "Point", "role": "measure",
            "datatype": "real", "formula": "MAKEPOINT(1, 2)",
        }
        result = _build_table(
            table, {"type": "csv", "details": {"filename": "synthetic.csv"}},
            [calculation], [],
            dax_context={"calc_map": {}, "param_map": {},
                         "column_table_map": {}, "measure_names": set(),
                         "param_values": {}},
            col_metadata_map={},
        )

        measure = next(
            (item for item in result["measures"] if item["name"] == "Point"),
            None,
        )
        self.assertIsNotNone(measure, result["measures"])
        self.assertIn(_FALLBACK, measure["expression"])
        self.assertIn("BLANK()", measure["expression"])
        self.assertIn("MAKEPOINT", measure.get('_migration_note', '').upper())

    def test_makepoint_todo_and_migration_note_are_serialized_to_tmdl(self):
        calculation = {
            "name": "[Point]", "caption": "Point", "role": "measure",
            "datatype": "real", "formula": "MAKEPOINT(1, 2)",
            "datasource_name": "Synthetic",
        }
        datasource = {
            "name": "Synthetic", "caption": "Synthetic",
            "connection": {"type": "sqlserver", "details": {
                "server": "synthetic.invalid", "database": "synthetic",
            }},
            "tables": [{
                "name": "Zone", "columns": [
                    {"name": "Secteur", "datatype": "string"},
                    {"name": "Lat", "datatype": "real"},
                    {"name": "Lon", "datatype": "real"}],
            }],
            "calculations": [calculation, {
                "name": "[PointByCoordinates]", "caption": "Point By Coordinates",
                "role": "dimension", "datatype": "real",
                "formula": "MAKEPOINT([Lat], [Lon])",
                "datasource_name": "Synthetic",
            }],
            "columns": [], "relationships": [], "connection_map": {},
        }
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary) / "Synthetic.SemanticModel"
            generate_tmdl(
                [datasource], "Synthetic", {"calculations": [calculation]},
                str(model_dir), parameterize=False,
            )
            content = (model_dir / "definition" / "tables" / "Zone.tmdl").read_text(
                encoding="utf-8")

        self.assertRegex(content, r"(?m)^\tmeasure '?Point'? =")
        self.assertIn("TODO: DAX conversion validation failed for measure", content)
        self.assertIn("annotation MigrationNote =", content)
        self.assertIn("MAKEPOINT has no scalar DAX equivalent", content)
        self.assertRegex(content, r"(?m)^\tcolumn 'Point By Coordinates' =")
        self.assertRegex(
            content,
            r'(?s)column \'Point By Coordinates\' =.*?annotation MigrationNote = '
            r'DAX conversion failed: MAKEPOINT has no scalar DAX equivalent',
        )

    def test_tableau_escaped_quote_is_serialized_as_dax_measure(self):
        calculation = {
            "name": "[Label]", "caption": "Label", "role": "measure",
            "datatype": "string", "formula": r"'it\'s ready'",
            "datasource_name": "Synthetic",
        }
        datasource = {
            "name": "Synthetic", "caption": "Synthetic",
            "connection": {"type": "sqlserver", "details": {
                "server": "synthetic.invalid", "database": "synthetic",
            }},
            "tables": [{"name": "Sales", "columns": [
                {"name": "Amount", "datatype": "real"}]}],
            "calculations": [calculation], "columns": [], "relationships": [],
            "connection_map": {},
        }
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary) / "Synthetic.SemanticModel"
            generate_tmdl([datasource], "Synthetic", {"calculations": [calculation]},
                          str(model_dir), parameterize=False)
            content = (model_dir / "definition" / "tables" / "Sales.tmdl").read_text(
                encoding="utf-8")
        measure = next(line for line in content.splitlines()
                       if line.strip().startswith('measure Label ='))
        self.assertIn('"it\'s ready"', measure)
        self.assertNotIn(_FALLBACK, measure)

    def test_anchored_slug_regex_generates_clean_calculated_column_tmdl(self):
        calculation = {
            "name": "[Calc_HasValidUTM]", "caption": "Has Valid UTM",
            "role": "dimension", "datatype": "boolean",
            "formula": 'REGEXP_MATCH([utm_campaign], "^[a-z0-9_-]+$")',
            "datasource_name": "Synthetic",
        }
        datasource = {
            "name": "Synthetic", "caption": "Synthetic",
            "connection": {"type": "sqlserver", "details": {
                "server": "synthetic.invalid", "database": "synthetic",
            }},
            "tables": [{"name": "Campaigns", "columns": [
                {"name": "utm_campaign", "datatype": "string"}]}],
            "calculations": [calculation], "columns": [], "relationships": [],
            "connection_map": {},
        }
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary) / "Synthetic.SemanticModel"
            generate_tmdl(
                [datasource], "Synthetic", {"calculations": [calculation]},
                str(model_dir), parameterize=False,
            )
            content = (model_dir / "definition" / "tables" / "Campaigns.tmdl").read_text(
                encoding="utf-8")
        column = content.split("\tcolumn 'Has Valid UTM' = ", 1)[1].split("\n", 1)[0]
        self.assertIn('GENERATESERIES', column)
        self.assertIn("'Campaigns'[utm_campaign]", column)
        self.assertIn('MID(__TableauRegexText, [Value], 1)', column)
        self.assertNotIn("'Campaigns'[Value]", column)
        self.assertNotIn('verify manually', column)
        self.assertNotIn('placeholder column created to satisfy missing reference', content)

    def test_unanchored_class_regex_generates_clean_calculated_column_tmdl(self):
        calculation = {
            "name": "[Calc_HasLowercase]", "caption": "Has Lowercase",
            "role": "dimension", "datatype": "boolean",
            "formula": 'REGEXP_MATCH([campaign], "[a-z]")',
            "datasource_name": "Synthetic",
        }
        datasource = {
            "name": "Synthetic", "caption": "Synthetic",
            "connection": {"type": "sqlserver", "details": {
                "server": "synthetic.invalid", "database": "synthetic",
            }},
            "tables": [{"name": "Campaigns", "columns": [
                {"name": "campaign", "datatype": "string"}]}],
            "calculations": [calculation], "columns": [], "relationships": [],
            "connection_map": {},
        }
        with tempfile.TemporaryDirectory() as temporary:
            model_dir = Path(temporary) / "Synthetic.SemanticModel"
            generate_tmdl(
                [datasource], "Synthetic", {"calculations": [calculation]},
                str(model_dir), parameterize=False,
            )
            content = (model_dir / "definition" / "tables" / "Campaigns.tmdl").read_text(
                encoding="utf-8")
        column = content.split("\tcolumn 'Has Lowercase' = ", 1)[1].split("\n", 1)[0]
        self.assertIn('GENERATESERIES', column)
        self.assertIn('UNICODE', column)
        self.assertIn('[Value]', column)
        self.assertNotIn("'Campaigns'[Value]", column)
        self.assertNotIn('verify manually', column)
        self.assertNotIn('placeholder column created to satisfy missing reference', content)

    def test_explicit_fixed_scope_compiles_through_full_tmdl_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "synthetic_zone.csv"
            source.write_text(
                "Secteur,Logements\n"
                + "".join(f"{sector},{housing}\n"
                         for sector, housing in _HOUSING_BY_SECTOR),
                encoding="utf-8",
            )
            datasource = {
                "name": "Synthetic", "caption": "Synthetic",
                "connection": {"type": "CSV", "details": {"filename": str(source)}},
                "tables": [{
                    "name": "Zone", "source_table": source.name, "type": "table",
                    "columns": [{"name": "Secteur", "datatype": "string"},
                                {"name": "Logements", "datatype": "real"}],
                }],
                "calculations": [{
                    "name": "[FixedHousing]", "caption": "Logements par secteur",
                    "datasource_name": "Synthetic", "role": "measure",
                    "datatype": "real",
                    "formula": "{ FIXED [Secteur] : MIN([Logements]) }",
                }],
                "columns": [], "relationships": [], "connection_map": {},
            }
            scope = {
                "table": "Zone",
                "fixed_dimensions": ["Secteur"],
                "value_column": "Logements",
                "physical_fields": ["Secteur", "Logements"],
                "source_filters": [],
                "target_filters": [],
                "filter_policy": "target_context_equals_fixed_input_per_key",
                "view_grains": [["Secteur"], []],
                "total_grain": [],
                "total_aggregation": "SUM_DISTINCT_FIXED_KEYS",
                "shelf_aggregation": "SUM",
                "value_type": "numeric",
                "source_row_bag_verified": True,
                "physical_fields_verified": True,
                "fixed_key_domain_verified": True,
                "filter_equivalence_verified": True,
                "group_key_and_blank_equivalence_verified": True,
                "numeric_and_null_equivalence_verified": True,
                "all_target_uses_and_totals_verified": True,
                "engine_support_verified": True,
            }
            model_dir = root / "Demo.SemanticModel"
            from powerbi_import.dax_validator import validate_dax_expression
            for shelf in (None, "SUM"):
                with self.subTest(shelf=shelf):
                    objects = {"verified_fixed_scopes": {
                        "Synthetic": {"FixedHousing": scope},
                    }}
                    if shelf is not None:
                        objects["worksheets"] = [{"name": "Synthetic Total", "fields": [{
                            "name": "[FixedHousing]", "datasource": "Synthetic",
                            "aggregation": shelf, "aggregation_evidence": "explicit",
                            "aggregation_candidates": [shelf.lower()]}]}]
                    generate_tmdl(
                        [deepcopy(datasource)], "Demo", objects, str(model_dir),
                    )

                    content = (
                        model_dir / "definition" / "tables" / "Zone.tmdl"
                    ).read_text(encoding="utf-8")
                    declaration = re.search(
                        r"(?m)^\tmeasure 'Logements par secteur' =([^\n]*)", content)
                    self.assertIsNotNone(declaration, content)
                    expression = declaration.group(1).strip()
                    self.assertNotIn(_FALLBACK, expression)
                    self.assertTrue(
                        expression.endswith(
                            "SUMX(SUMMARIZE('Zone', 'Zone'[Secteur]), "
                            "CALCULATE(MIN('Zone'[Logements])))"),
                        expression,
                    )
                    self.assertNotIn("approximation", expression.lower())
                    self.assertNotRegex(
                        content, r"(?im)^\s*annotation MigrationNote = .*approximation")
                    self.assertEqual(validate_dax_expression(expression), [])

            environment_model_dir = root / "EnvironmentDemo.SemanticModel"
            with patch.dict(os.environ, {
                "TTPBI_VERIFIED_FIXED_SCOPES": json.dumps({
                    "Synthetic": {"FixedHousing": scope},
                }),
            }):
                generate_tmdl(
                    [datasource], "EnvironmentDemo", {},
                    str(environment_model_dir),
                )
            environment_content = (
                environment_model_dir / "definition" / "tables" / "Zone.tmdl"
            ).read_text(encoding="utf-8")
            environment_declaration = re.search(
                r"(?m)^\tmeasure 'Logements par secteur' =([^\n]*)",
                environment_content,
            )
            self.assertIsNotNone(environment_declaration, environment_content)
            self.assertTrue(
                environment_declaration.group(1).rstrip().endswith(
                    "SUMX(SUMMARIZE('Zone', 'Zone'[Secteur]), "
                    "CALCULATE(MIN('Zone'[Logements])))"),
                environment_declaration.group(1),
            )

    def test_table_generation_retains_unproven_calculations_and_dependencies(self):
        table = {
            "name": "Zone",
            "columns": [
                {"name": "Secteur", "datatype": "string"},
                {"name": "Logements", "datatype": "real"},
                {"name": "Facture", "datatype": "real"},
            ],
        }
        calculations = [
            {"name": "EstAuDessus", "caption": "EstAuDessus",
             "formula": TestReportedRowGrain._row_formula,
             "role": "measure", "datatype": "real"},
            {"name": "FixedHousing", "caption": "FixedHousing",
             "formula": "{ FIXED [Secteur] : MIN([Logements]) }",
             "role": "measure", "datatype": "real"},
            {"name": "TotalHousing", "caption": "TotalHousing",
             "formula": "SUM([FixedHousing])", "role": "measure", "datatype": "real"},
            {"name": "Derived", "caption": "Derived",
             "formula": "[EstAuDessus] + 1", "role": "measure", "datatype": "real"},
        ]
        context = {
            "calc_map": {}, "param_map": {}, "column_table_map": dict(_COLUMN_OWNERS),
            "measure_names": set(), "param_values": {},
        }
        result = _build_table(
            table, {"type": "sqlserver", "server": "synthetic.invalid",
                    "database": "synthetic"},
            calculations, [], dax_context=context, col_metadata_map={})
        measures = {measure["name"]: measure["expression"]
                    for measure in result["measures"]}
        _assert_unsupported(self, measures["EstAuDessus"], "row", "grain")
        _assert_unsupported(self, measures["FixedHousing"], "FIXED", "grain")
        _assert_unsupported(self, measures["TotalHousing"], "aggregation", "grain")
        _assert_unsupported(self, measures["Derived"], "depends", "unsupported")

    def test_serialized_fallbacks_reach_production_quality_without_runtime_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "synthetic_zone.csv"
            source.write_text(
                "Secteur,Logements\n"
                + "".join(f"{sector},{housing}\n" for sector, housing in _HOUSING_BY_SECTOR),
                encoding="utf-8",
            )
            datasource = {
                "name": "Synthetic", "caption": "Synthetic",
                "connection": {"type": "CSV", "details": {"filename": str(source)}},
                "tables": [{
                    "name": "Zone", "source_table": source.name, "type": "table",
                    "columns": [{"name": "Secteur", "datatype": "string"},
                                {"name": "Logements", "datatype": "real"}],
                }],
                "calculations": [
                    {"name": "[FixedHousing]", "caption": "Logements par secteur",
                     "role": "measure", "datatype": "real",
                     "formula": "{ FIXED [Secteur] : MIN([Logements]) }"},
                    {"name": "[TotalHousing]", "caption": "Somme Logements",
                     "role": "measure", "datatype": "real",
                     "formula": "SUM([FixedHousing])"},
                ],
                "columns": [], "relationships": [], "connection_map": {},
            }
            model_dir = root / "Demo.SemanticModel"
            generate_tmdl([datasource], "Demo", {}, str(model_dir))
            content = (model_dir / "definition" / "tables" / "Zone.tmdl").read_text(
                encoding="utf-8")
            for name in ("Logements par secteur", "Somme Logements"):
                with self.subTest(measure=name):
                    declaration = re.search(
                        rf"(?m)^\tmeasure '{re.escape(name)}' =([^\n]*)", content)
                    self.assertIsNotNone(declaration, content)
                    self.assertIn(_FALLBACK, declaration.group(1))
                    self.assertIn("BLANK()", declaration.group(1))

            report = build_quality_report(
                {"datasources": [datasource]}, str(root), "Demo",
                quality_policy="production")
            self.assertEqual(report.status, "FAIL")
            self.assertGreaterEqual(
                report.semantic_context["measure_context"]["fallback_count"], 2)
            self.assertEqual(report.semantic_context["execution"]["status"], "not_run")
            for name in ("Logements par secteur", "Somme Logements"):
                self.assertTrue(any(name in blocker and "fallback" in blocker.lower()
                                    for blocker in report.blockers), report.blockers)
