"""Synthetic, lazy Hyper API doubles shared by export regression tests.

These implement the context-managed execute_query API, not the materializing
execute_list_query shortcut. No Tableau installation or workbook is needed.
"""

import itertools
import re
from types import SimpleNamespace
from unittest import mock


def quote_identifier(value):
    return '"' + value.replace('"', '""') + '"'


class HyperTableName:
    def __init__(self, schema, table):
        self.schema_name = SimpleNamespace(name=SimpleNamespace(unescaped=schema))
        self.name = SimpleNamespace(unescaped=table)

    def __str__(self):
        return ".".join((
            quote_identifier(self.schema_name.name.unescaped),
            quote_identifier(self.name.unescaped),
        ))


class StreamingResult:
    """Single-pass result; closing and consumed rows remain observable."""

    def __init__(self, rows, fail_after=None, error=None):
        self._rows = iter(rows)
        self.fail_after = fail_after
        self.error = error or RuntimeError("synthetic Hyper cursor failure")
        self.consumed = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()
        return False

    def __iter__(self):
        return self

    def __next__(self):
        if self.fail_after is not None and self.consumed >= self.fail_after:
            raise self.error
        row = next(self._rows)
        self.consumed += 1
        return row

    def close(self):
        self.closed = True


class StreamingHyperApi:
    """Small catalog with a fresh lazy result for every data query."""

    def __init__(self, columns=("id", "label"), rows=None, row_factory=None,
                 total_rows=2, count_error=None, fail_after=None, error=None,
                 table_pairs=None):
        self.columns = list(columns)
        self.row_factory = row_factory or (
            lambda: iter(rows if rows is not None else [(1, "first"), (2, "second")])
        )
        self.total_rows = total_rows
        self.count_error = count_error
        self.fail_after = fail_after
        self.error = error
        self.results = []
        self.queries = []
        self.tables = [HyperTableName(schema, table) for schema, table in (
            table_pairs or [("Extract", "Facts")]
        )]
        schemas = {}
        for table in self.tables:
            name = table.schema_name.name.unescaped
            table.schema_name = schemas.setdefault(name, table.schema_name)

        self.connection = mock.MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.connection.__exit__.side_effect = self._close_connection
        self.connection.catalog.get_schema_names.return_value = list(schemas.values())
        self.connection.catalog.get_table_names.side_effect = (
            lambda schema: [t for t in self.tables if t.schema_name is schema]
        )
        self.connection.catalog.get_table_definition.return_value = SimpleNamespace(
            columns=[SimpleNamespace(
                name=SimpleNamespace(unescaped=name),
                type="TEXT",
                nullability=SimpleNamespace(is_nullable=True),
            ) for name in self.columns],
        )
        self.connection.execute_scalar_query.side_effect = self._count
        self.connection.execute_query.side_effect = self._query
        self.connection.execute_list_query.side_effect = AssertionError(
            "Data export must use a streaming execute_query result"
        )
        self.process = mock.MagicMock(endpoint="synthetic-endpoint")
        self.process.__enter__.return_value = self.process
        self.process.__exit__.side_effect = self._close_process
        self.module = SimpleNamespace(
            HyperProcess=mock.Mock(return_value=self.process),
            Connection=mock.Mock(return_value=self.connection),
            Telemetry=SimpleNamespace(DO_NOT_SEND_USAGE_DATA_TO_TABLEAU=0),
            TableName=HyperTableName,
        )

    def _close_connection(self, *unused):
        self.connection.close()
        return False

    def _close_process(self, *unused):
        self.process.close()
        return False

    def _count(self, sql, *unused, **kwargs):
        self.queries.append(str(sql))
        if self.count_error is not None:
            raise self.count_error
        return self.total_rows

    def _query(self, sql, *unused, **kwargs):
        sql = str(sql)
        self.queries.append(sql)
        if "COUNT(" in sql.upper():
            if self.count_error is not None:
                raise self.count_error
            return StreamingResult([(self.total_rows,)])
        rows = self.row_factory()
        limit = re.search(r"\bLIMIT\s+(\d+)\b", sql, re.I)
        if limit:
            rows = itertools.islice(rows, int(limit[1]))
        result = StreamingResult(rows, fail_after=self.fail_after, error=self.error)
        self.results.append(result)
        return result
