"""Sprint 182 — Custom SQL & Native Query Depth tests."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'powerbi_import'))

import pytest

from sql_analyzer import (
    analyze_sql,
    detect_dialect,
    extract_parameters,
    to_native_query_m,
    SqlColumn,
    SqlAnalysis,
)


# ── Dialect detection ────────────────────────────────────────────────────────

class TestDialect:
    def test_tsql(self):
        assert detect_dialect("SELECT TOP 10 [Name] FROM Customers") == "tsql"

    def test_postgres(self):
        assert detect_dialect("SELECT id::int FROM t WHERE name ILIKE 'a%'") == "postgres"

    def test_mysql(self):
        assert detect_dialect("SELECT `id` FROM `users` LIMIT 5") == "mysql"

    def test_oracle(self):
        assert detect_dialect("SELECT NVL(x,0) FROM dual WHERE ROWNUM < 5") == "oracle"

    def test_snowflake(self):
        assert detect_dialect("SELECT IFF(x>0,1,0) FROM t QUALIFY row_number() over(...) = 1") == "snowflake"

    def test_bigquery(self):
        assert detect_dialect("SELECT SAFE_CAST(x AS INT64) FROM `proj.ds.tbl`") == "bigquery"

    def test_ansi_fallback(self):
        assert detect_dialect("SELECT a, b FROM t") == "ansi"


# ── Parameter extraction ─────────────────────────────────────────────────────

class TestParameters:
    def test_tableau_angle(self):
        params = extract_parameters("SELECT * FROM t WHERE region = <Parameters.Region>")
        assert params == ["Region"]

    def test_tableau_bracket(self):
        params = extract_parameters("SELECT * FROM t WHERE y = [Parameters].[Fiscal Year]")
        assert params == ["Fiscal Year"]

    def test_at_param(self):
        assert extract_parameters("SELECT * FROM t WHERE id = @CustomerId") == ["CustomerId"]

    def test_colon_param(self):
        assert extract_parameters("SELECT * FROM t WHERE id = :custid") == ["custid"]

    def test_colon_not_cast(self):
        # ::int cast should NOT be treated as a parameter
        assert extract_parameters("SELECT id::int FROM t") == []

    def test_dollar_brace(self):
        assert extract_parameters("SELECT * FROM ${schema}.t") == ["schema"]

    def test_dedup(self):
        params = extract_parameters("SELECT * FROM t WHERE a=@x AND b=@x")
        assert params == ["x"]


# ── SELECT list parsing ──────────────────────────────────────────────────────

class TestSelectList:
    def test_simple_columns(self):
        a = analyze_sql("SELECT id, name, city FROM customers")
        names = [c.name for c in a.columns]
        assert names == ["id", "name", "city"]
        assert not a.is_select_star

    def test_select_star(self):
        a = analyze_sql("SELECT * FROM customers")
        assert a.is_select_star
        assert a.columns == []

    def test_qualified_star(self):
        a = analyze_sql("SELECT c.* FROM customers c")
        assert a.is_select_star

    def test_explicit_alias(self):
        a = analyze_sql("SELECT SUM(amount) AS total FROM sales")
        assert a.columns[0].alias == "total"
        assert a.columns[0].is_aggregate

    def test_aggregate_inference(self):
        a = analyze_sql("SELECT COUNT(*) AS n, SUM(x) AS s FROM t")
        assert a.columns[0].inferred_type == "int64"
        assert a.columns[1].inferred_type == "double"

    def test_cast_type(self):
        a = analyze_sql("SELECT CAST(x AS int) AS xi FROM t")
        assert a.columns[0].inferred_type == "int64"

    def test_postgres_cast_type(self):
        a = analyze_sql("SELECT price::decimal AS p FROM t")
        assert a.columns[0].inferred_type == "decimal"

    def test_dotted_source_column(self):
        a = analyze_sql("SELECT c.name FROM customers c")
        assert a.columns[0].source_column == "name"

    def test_commas_in_function_not_split(self):
        a = analyze_sql("SELECT COALESCE(a, b, c) AS v, id FROM t")
        assert len(a.columns) == 2


# ── FROM / JOIN parsing ──────────────────────────────────────────────────────

class TestFromJoins:
    def test_single_table(self):
        a = analyze_sql("SELECT * FROM orders")
        assert a.tables == ["orders"]
        assert a.joins == []

    def test_schema_qualified(self):
        a = analyze_sql("SELECT * FROM dbo.orders")
        assert a.tables == ["dbo.orders"]

    def test_inner_join(self):
        a = analyze_sql("SELECT * FROM orders o INNER JOIN customers c ON o.cid = c.id")
        assert "orders" in a.tables
        assert "customers" in a.tables
        assert a.joins[0]["type"] == "inner"
        assert "o.cid = c.id" in a.joins[0]["on"]

    def test_left_join(self):
        a = analyze_sql("SELECT * FROM a LEFT JOIN b ON a.k = b.k")
        assert a.joins[0]["type"] == "left"

    def test_left_outer_join(self):
        a = analyze_sql("SELECT * FROM a LEFT OUTER JOIN b ON a.k = b.k")
        assert a.joins[0]["type"] == "left"

    def test_multiple_joins(self):
        a = analyze_sql(
            "SELECT * FROM a JOIN b ON a.k=b.k JOIN c ON b.j=c.j JOIN d ON c.i=d.i"
        )
        assert len(a.joins) == 3
        assert a.grade == "RED"

    def test_comma_join(self):
        a = analyze_sql("SELECT * FROM a, b WHERE a.k = b.k")
        assert set(a.tables) == {"a", "b"}


# ── WHERE / GROUP BY / ORDER BY ──────────────────────────────────────────────

class TestClauses:
    def test_where(self):
        a = analyze_sql("SELECT * FROM t WHERE x > 5 AND y < 10")
        assert a.where == "x > 5 AND y < 10"

    def test_group_by(self):
        a = analyze_sql("SELECT region, SUM(x) FROM t GROUP BY region")
        assert a.group_by == ["region"]

    def test_group_by_multiple(self):
        a = analyze_sql("SELECT a, b, SUM(x) FROM t GROUP BY a, b")
        assert a.group_by == ["a", "b"]

    def test_order_by(self):
        a = analyze_sql("SELECT * FROM t ORDER BY x DESC, y ASC")
        assert a.order_by == ["x DESC", "y ASC"]

    def test_where_with_group_by(self):
        a = analyze_sql("SELECT region, SUM(x) FROM t WHERE x>0 GROUP BY region ORDER BY region")
        assert a.where == "x>0"
        assert a.group_by == ["region"]
        assert a.order_by == ["region"]


# ── Grading ──────────────────────────────────────────────────────────────────

class TestGrading:
    def test_green_simple(self):
        a = analyze_sql("SELECT id, name FROM customers")
        assert a.grade == "GREEN"

    def test_yellow_select_star(self):
        a = analyze_sql("SELECT * FROM customers")
        assert a.grade == "YELLOW"

    def test_yellow_join(self):
        a = analyze_sql("SELECT a.x FROM a JOIN b ON a.k=b.k")
        assert a.grade == "YELLOW"

    def test_red_subquery(self):
        a = analyze_sql("SELECT * FROM (SELECT id FROM t) sub")
        assert a.has_subquery
        assert a.grade == "RED"

    def test_invalid_no_from(self):
        a = analyze_sql("UPDATE t SET x = 1")
        assert a.grade == "RED"


# ── Native query M emission ──────────────────────────────────────────────────

class TestNativeQueryM:
    def test_basic(self):
        m = to_native_query_m("SELECT id FROM t", "srv", "db")
        assert m.startswith("let")
        assert "Value.NativeQuery" in m
        assert 'Sql.Database("srv", "db")' in m
        assert "EnableFolding=true" in m
        assert m.rstrip().endswith("Result")

    def test_quotes_escaped(self):
        m = to_native_query_m('SELECT name FROM t WHERE name = "x"', "s", "d")
        assert '""x""' in m

    def test_named_instance_server_not_corrupted(self):
        # Backslash is literal in M strings — must NOT be doubled.
        m = to_native_query_m("SELECT id FROM t", "SERVER\\SQLEXPRESS", "db")
        assert 'Sql.Database("SERVER\\SQLEXPRESS", "db")' in m
        assert "\\\\" not in m

    def test_tableau_param_rewrite(self):
        m = to_native_query_m(
            "SELECT * FROM t WHERE region = <Parameters.Region>",
            "s", "d", params={"Region": "EMEA"},
        )
        assert "@Region" in m
        assert 'Region="EMEA"' in m

    def test_colon_param_rewrite(self):
        m = to_native_query_m("SELECT * FROM t WHERE id = :custId", "s", "d", params={"custId": "5"})
        assert "@custId" in m
        assert 'custId="5"' in m

    def test_no_params_null_record(self):
        m = to_native_query_m("SELECT 1 FROM t", "s", "d")
        assert ", null, " in m

    def test_param_with_space_sanitized(self):
        m = to_native_query_m(
            "SELECT * FROM t WHERE y = [Parameters].[Fiscal Year]",
            "s", "d", params={"Fiscal Year": "2025"},
        )
        assert "@Fiscal_Year" in m
        assert 'Fiscal_Year="2025"' in m

    def test_disable_folding(self):
        m = to_native_query_m("SELECT 1 FROM t", "s", "d", enable_folding=False)
        assert "EnableFolding" not in m

    def test_custom_source_func(self):
        m = to_native_query_m("SELECT 1 FROM t", "host", "cat", source_func="Snowflake.Databases")
        assert "Snowflake.Databases" in m


# ── Serialization ────────────────────────────────────────────────────────────

class TestToDict:
    def test_to_dict_shape(self):
        a = analyze_sql("SELECT region, SUM(x) AS total FROM sales WHERE x>0 GROUP BY region")
        d = a.to_dict()
        assert d["dialect"] in ("ansi", "tsql", "postgres", "mysql", "oracle", "snowflake", "bigquery")
        assert d["tables"] == ["sales"]
        assert d["group_by"] == ["region"]
        assert any(c["name"] == "total" for c in d["columns"])
        assert d["grade"] in ("GREEN", "YELLOW", "RED")

    def test_column_name_property(self):
        c = SqlColumn(expression="SUM(x)", alias="total")
        assert c.name == "total"
        c2 = SqlColumn(expression="name", source_column="name")
        assert c2.name == "name"
        c3 = SqlColumn(expression="literal")
        assert c3.name == "literal"
