"""Synthetic regressions for reported DAX defects 3--6.

The small oracle below evaluates the *emitted* DAX subset, not the Tableau
formula.  It fails on unsupported syntax and is not a DAX engine: these
value checks are bounded synthetic evidence, not Desktop/Fabric validation.
FIXED cases without source-grain evidence are checked as static diagnostics.
"""

import json
import os
import re
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from powerbi_import.errors import PartialGenerationError
from powerbi_import.migration_quality import build_quality_report
from powerbi_import.tmdl_dax_postprocess import (
    _unwrap_aggregations_of_measures,
    _validate_measures_after_rewrites,
)
from powerbi_import.tmdl_generator import (
    _build_semantic_model,
    _build_table,
    generate_tmdl,
)
from tableau_export.dax_converter import convert_tableau_formula_to_dax


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


class TestReportedFixedGrainStatic(unittest.TestCase):
    """Defects 4--5: fail closed until the complete FIXED grain is proven."""

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


class TestReportedDaxDiagnosticsIntegration(unittest.TestCase):
    """Static pipeline and quality checks; no semantic engine is invoked."""

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
            generate_tmdl(
                [datasource], "Demo",
                {"verified_fixed_scopes": {
                    "Synthetic": {"FixedHousing": scope},
                }},
                str(model_dir),
            )

            content = (model_dir / "definition" / "tables" / "Zone.tmdl").read_text(
                encoding="utf-8")
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
            from powerbi_import.dax_validator import validate_dax_expression
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
