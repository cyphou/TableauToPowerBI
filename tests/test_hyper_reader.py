"""Tests for tableau_export/hyper_reader.py — Sprint 30 coverage push."""

import contextlib
import io
import os
import sqlite3
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

import tableau_export.hyper_reader as _hyper_reader
from powerbi_import.errors import ConfigurationError, ExtractionError
from tableau_export.hyper_reader import (
    INLINE_ROW_THRESHOLD,
    _m_literal,
    _m_type_for,
    _parse_inserts,
    _read_hyper_header,
    _read_hyper_sqlite,
    _split_values,
    generate_m_csv_reference,
    generate_m_for_hyper_table,
    generate_m_inline_table,
    get_hyper_metadata,
    infer_hyper_relationships,
    read_hyper,
    read_hyper_from_twbx,
)
from tests.hyper_test_helpers import Nullability


class TestBoundedHyperFallbackReads(unittest.TestCase):
    """Header metadata is a bounded sample, never an eager whole-file read."""

    def test_header_fallback_reads_only_262144_bytes(self):
        payload = b"Hyper" + b"x" * (512 * 1024)
        requests = []

        class HeaderStream(io.BytesIO):
            def read(self, size=-1):
                if not 0 < size <= 262144:
                    raise AssertionError(f"unbounded Hyper fallback read: {size}")
                requests.append(size)
                return super().read(size)

        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "synthetic.hyper")
            with open(path, "wb") as stream:
                stream.write(payload)
            with mock.patch.object(_hyper_reader, "_read_hyper_api", return_value=None), \
                    mock.patch.object(_hyper_reader, "_read_hyper_sqlite", return_value=None), \
                    mock.patch.object(_hyper_reader, "_read_hyper_header",
                                      return_value=[]) as header, \
                    mock.patch.object(_hyper_reader, "open", create=True,
                                      side_effect=lambda *a, **k: HeaderStream(payload)):
                result = read_hyper(path, max_rows=2)
        header.assert_called_once_with(payload[:262144], max_rows=2)
        self.assertEqual(result["tables"], [])
        self.assertEqual(requests, [16, 262144])


class TestStreamingHyperArchiveCleanup(unittest.TestCase):
    """Synthetic ZIP entries stream to temporary files that never leak."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.archive = os.path.join(self.root, "synthetic.twbx")
        self.member = "Data/Exact/synthetic.hyper"
        self.payload = b"Hyper" + b"x" * (2 * 1024 * 1024 + 17)
        with zipfile.ZipFile(self.archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(self.member, self.payload)
        self.temporary_paths = []
        real_temporary_directory = tempfile.TemporaryDirectory

        def temporary_directory(*args, **kwargs):
            kwargs["dir"] = self.root
            result = real_temporary_directory(*args, **kwargs)
            self.temporary_paths.append(result.name)
            return result

        self.temp_patcher = mock.patch.object(
            _hyper_reader.tempfile, "TemporaryDirectory", side_effect=temporary_directory,
        )
        self.temp_patcher.start()
        self.addCleanup(self.temp_patcher.stop)

    def _assert_clean(self):
        self.assertTrue(self.temporary_paths, "The member must be staged on disk")
        for path in self.temporary_paths:
            self.assertFalse(os.path.exists(path), f"leaked Hyper temporary file: {path}")
        self.assertEqual(os.listdir(self.root), ["synthetic.twbx"])

    def test_archive_does_not_use_zipfile_read_and_copies_in_bounded_chunks(self):
        requests = []
        real_read = zipfile.ZipExtFile.read

        def bounded_read(stream, size=-1):
            self.assertGreater(size, 0)
            self.assertLessEqual(size, 1024 * 1024)
            requests.append(size)
            return real_read(stream, size)

        def inspect_staged_file(path, max_rows):
            self.assertEqual(max_rows, 3)
            with open(path, "rb") as stream:
                self.assertEqual(stream.read(), self.payload)
            return {"tables": [], "format": "hyper", "file_path": path}

        with mock.patch.object(zipfile.ZipFile, "read",
                               side_effect=AssertionError("whole ZIP member read")), \
                mock.patch.object(zipfile.ZipExtFile, "read", bounded_read), \
                mock.patch.object(_hyper_reader, "read_hyper",
                                  side_effect=inspect_staged_file):
            result = read_hyper_from_twbx(self.archive, max_rows=3)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["archive_path"], self.member)
        self.assertGreater(len(requests), 2)
        self._assert_clean()

    def test_reader_failure_removes_staged_member(self):
        failure = RuntimeError("synthetic metadata reader failure")
        with mock.patch.object(_hyper_reader, "read_hyper", side_effect=failure):
            with self.assertRaises(RuntimeError) as raised:
                read_hyper_from_twbx(self.archive)
        self.assertIs(raised.exception, failure)
        self._assert_clean()

    def test_copy_failure_removes_partial_temporary_file(self):
        real_read = zipfile.ZipExtFile.read
        reads = []

        def fail_after_first_chunk(stream, size=-1):
            self.assertGreater(size, 0)
            self.assertLessEqual(size, 1024 * 1024)
            if reads:
                raise OSError("synthetic archive stream failure")
            reads.append(size)
            return real_read(stream, size)

        with mock.patch.object(zipfile.ZipExtFile, "read", fail_after_first_chunk), \
                mock.patch.object(_hyper_reader, "read_hyper") as reader:
            with self.assertRaises(ExtractionError) as raised:
                read_hyper_from_twbx(self.archive)
        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertIn("synthetic archive stream failure", str(raised.exception.__cause__))
        reader.assert_not_called()
        self._assert_clean()

    def test_zip_limit_alias_and_conflict_are_explicit(self):
        with mock.patch.object(_hyper_reader, "read_hyper",
                               return_value={"tables": []}) as reader:
            result = read_hyper_from_twbx(self.archive, max_size_mb=3)
        self.assertEqual(len(result), 1)
        reader.assert_called_once()
        self._assert_clean()
        with self.assertRaises(ConfigurationError):
            read_hyper_from_twbx(self.archive, max_zip_entry_mb=3, max_size_mb=4)

    def test_declared_oversize_is_not_opened_and_no_temporary_file_leaks(self):
        with mock.patch.object(zipfile.ZipFile, "open",
                               side_effect=AssertionError("oversized member was opened")), \
                mock.patch.object(_hyper_reader, "read_hyper") as reader:
            with self.assertRaisesRegex(ValueError, "(?i)(size|limit)"):
                read_hyper_from_twbx(self.archive, max_zip_entry_mb=1)
        reader.assert_not_called()
        self.assertTrue(all(not os.path.exists(path) for path in self.temporary_paths))
        self.assertEqual(os.listdir(self.root), ["synthetic.twbx"])


# ── _m_type_for ────────────────────────────────────────────────────


class TestMTypeFor(unittest.TestCase):
    def test_known_types(self):
        self.assertEqual(_m_type_for('text'), 'Text.Type')
        self.assertEqual(_m_type_for('INTEGER'), 'Int64.Type')
        self.assertEqual(_m_type_for('boolean'), 'Logical.Type')
        self.assertEqual(_m_type_for('date'), 'Date.Type')
        self.assertEqual(_m_type_for('timestamp'), 'DateTime.Type')
        self.assertEqual(_m_type_for('timestamptz'), 'DateTimeZone.Type')

    def test_unknown_type_returns_any(self):
        self.assertEqual(_m_type_for('xml_blob'), 'Any.Type')

    def test_whitespace_stripped(self):
        self.assertEqual(_m_type_for('  float  '), 'Number.Type')

    def test_none_returns_any(self):
        self.assertEqual(_m_type_for(None), 'Any.Type')


# ── _m_literal ─────────────────────────────────────────────────────


class TestMLiteral(unittest.TestCase):
    def test_none_returns_null(self):
        self.assertEqual(_m_literal(None), 'null')

    def test_logical_true(self):
        self.assertEqual(_m_literal(True, 'Logical.Type'), 'true')

    def test_logical_false(self):
        self.assertEqual(_m_literal(False, 'Logical.Type'), 'false')

    def test_logical_zero_is_false(self):
        self.assertEqual(_m_literal(0, 'Logical.Type'), 'false')

    def test_int64(self):
        self.assertEqual(_m_literal(42, 'Int64.Type'), '42')

    def test_number(self):
        self.assertEqual(_m_literal(3.14, 'Number.Type'), '3.14')

    def test_date_iso(self):
        result = _m_literal('2024-01-15', 'Date.Type')
        self.assertEqual(result, '#date(2024, 01, 15)')

    def test_date_non_iso_fallback(self):
        result = _m_literal('not-a-date', 'Date.Type')
        self.assertEqual(result, '"not-a-date"')

    def test_datetime_iso(self):
        result = _m_literal('2024-03-15 10:30:45', 'DateTime.Type')
        self.assertEqual(result, '#datetime(2024, 03, 15, 10, 30, 45)')

    def test_datetimezone(self):
        result = _m_literal('2024-03-15 10:30:45', 'DateTimeZone.Type')
        self.assertEqual(result, '#datetime(2024, 03, 15, 10, 30, 45)')

    def test_datetime_non_iso_fallback(self):
        result = _m_literal('not-a-datetime', 'DateTime.Type')
        self.assertEqual(result, '"not-a-datetime"')

    def test_text_default(self):
        self.assertEqual(_m_literal('hello'), '"hello"')

    def test_text_escapes_quotes(self):
        self.assertEqual(_m_literal('say "hi"'), '"say ""hi"""')


class TestHyperTableGuardrails(unittest.TestCase):
    def test_generate_inline_table_none(self):
        result = generate_m_inline_table(None)
        self.assertIn('#table', result)

    def test_generate_csv_reference_none(self):
        result = generate_m_csv_reference(None)
        self.assertIn('Csv.Document', result)

    def test_generate_for_table_none(self):
        result = generate_m_for_hyper_table(None)
        self.assertIn('#table', result)

    def test_infer_relationships_none(self):
        self.assertEqual(infer_hyper_relationships(None), [])

    def test_get_metadata_nonexistent_file(self):
        result = get_hyper_metadata('/nonexistent/path.hyper')
        self.assertEqual(result['tables'], [])
        self.assertEqual(result['total_rows'], 0)


# ── _split_values ──────────────────────────────────────────────────


class TestSplitValues(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(_split_values("1, 'abc', 3"), ['1', "'abc'", '3'])

    def test_quoted_comma(self):
        result = _split_values("'a,b', 2")
        self.assertEqual(result, ["'a,b'", '2'])

    def test_empty(self):
        self.assertEqual(_split_values(''), [])

    def test_null(self):
        self.assertEqual(_split_values("NULL, 'x'"), ['NULL', "'x'"])


# ── _read_hyper_sqlite ─────────────────────────────────────────────


class TestHyperApiSchemaIdentity(unittest.TestCase):
    """SchemaName.name is a Name; SchemaName itself has no .unescaped."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "synthetic.hyper")
        with open(self.path, "wb") as stream:
            stream.write(b"Hyper" + b"\0" * 11)
        self.identities = [
            ('Schema."MiXeD"', 'Table."MiXeD"'),
            ('Schema."MiXeD"', 'table."mixed"'),
            ('schema."mixed"', 'Table."MiXeD"'),
        ]
        schemas = [
            SimpleNamespace(name=SimpleNamespace(unescaped=name))
            for name in dict.fromkeys(schema for schema, _ in self.identities)
        ]

        class TableName:
            def __init__(self, schema, name):
                self.schema_name = schema
                self.name = SimpleNamespace(unescaped=name)

            def __str__(self):
                return ".".join(
                    '"' + name.replace('"', '""') + '"'
                    for name in (self.schema_name.name.unescaped, self.name.unescaped)
                )

        self.tables = [
            TableName(next(s for s in schemas if s.name.unescaped == schema), name)
            for schema, name in self.identities
        ]
        self.connection = mock.MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.connection.catalog.get_schema_names.return_value = schemas
        self.connection.catalog.get_table_names.side_effect = (
            lambda schema: [t for t in self.tables if t.schema_name is schema]
        )
        self.connection.catalog.get_table_definition.return_value = SimpleNamespace(
            columns=[SimpleNamespace(
                name=SimpleNamespace(unescaped="Value"),
                type="INTEGER",
                nullability=Nullability.NULLABLE,
            )]
        )
        self.connection.execute_scalar_query.return_value = 2
        self.connection.execute_query.return_value.__enter__.return_value = [[7], [9]]
        process = mock.MagicMock()
        process.__enter__.return_value = process
        api = SimpleNamespace(
            HyperProcess=mock.Mock(return_value=process),
            Connection=mock.Mock(return_value=self.connection),
            Telemetry=SimpleNamespace(DO_NOT_SEND_USAGE_DATA_TO_TABLEAU=0),
            TableName=TableName,
            Nullability=Nullability,
        )
        patcher = mock.patch.dict("sys.modules", {"tableauhyperapi": api})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_read_hyper_preserves_exact_schema_table_metadata_without_fallback(self):
        with mock.patch.object(_hyper_reader, "_read_hyper_sqlite") as sqlite_reader, \
                mock.patch.object(_hyper_reader, "_read_hyper_header") as header_reader:
            result = read_hyper(self.path, max_rows=2)
            sqlite_reader.assert_not_called()
            header_reader.assert_not_called()
        self.assertEqual(result["format"], "hyper_api")
        self.assertEqual(
            [(t["schema"], t["table"]) for t in result["tables"]],
            self.identities,
        )
        self.assertEqual(
            [t["qualified_table"] for t in result["tables"]],
            [str(t) for t in self.tables],
        )
        for table in result["tables"]:
            self.assertEqual(table["columns"], [{"name": "Value", "hyper_type": "INTEGER"}])
            self.assertEqual(table["column_count"], 1)
            self.assertEqual(table["row_count"], 2)
            self.assertEqual(table["sample_rows"], [{"Value": 7}, {"Value": 9}])
        for table in self.tables:
            self.connection.execute_query.assert_any_call(f"SELECT * FROM {table} LIMIT 2")

    def test_discover_multi_table_preserves_exact_identity_without_sqlite_fallback(self):
        with mock.patch("sqlite3.connect") as sqlite_reader:
            tables = _hyper_reader.discover_multi_table_hyper(self.path)
            sqlite_reader.assert_not_called()
        self.assertEqual([(t["schema"], t["table"]) for t in tables], self.identities)
        for table in tables:
            self.assertEqual(table["row_count"], 2)
            self.assertEqual(table["columns"], [{
                "name": "Value", "hyper_type": "INTEGER",
                "m_type": "Int64.Type", "nullable": True,
            }])

    def test_discover_multi_table_maps_each_nullability_member(self):
        self.connection.catalog.get_table_definition.return_value = SimpleNamespace(
            columns=[
                SimpleNamespace(
                    name=SimpleNamespace(unescaped=name), type="INTEGER",
                    nullability=nullability,
                )
                for name, nullability in (
                    ("Required", Nullability.NOT_NULLABLE),
                    ("Optional", Nullability.NULLABLE),
                )
            ]
        )
        with mock.patch("sqlite3.connect") as sqlite_reader:
            tables = _hyper_reader.discover_multi_table_hyper(self.path)
            sqlite_reader.assert_not_called()
        self.assertEqual(len(tables), len(self.identities))
        for table in tables:
            self.assertEqual(
                [(c["name"], c["nullable"]) for c in table["columns"]],
                [("Required", False), ("Optional", True)],
            )


class TestHyperApiRealSdk(unittest.TestCase):
    """The real SDK has an enum Nullability and an Optional TableName.schema_name."""

    def setUp(self):
        try:
            import tableauhyperapi as api
        except ImportError:
            self.skipTest("optional tableauhyperapi SDK is not installed")
        tmp = tempfile.TemporaryDirectory(prefix="hyper-discover-", dir=os.getcwd())
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.path = os.path.join(self.root, "synthetic.hyper")
        telemetry = api.Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU
        with api.HyperProcess(telemetry, parameters={"log_dir": self.root}) as process:
            with api.Connection(
                    process.endpoint, self.path, api.CreateMode.CREATE_AND_REPLACE,
            ) as connection:
                for schema in ("Extract", "Other Schema"):
                    connection.catalog.create_schema(api.SchemaName(schema))
                orders = api.TableName("Extract", "Orders")
                connection.catalog.create_table(api.TableDefinition(orders, [
                    api.TableDefinition.Column(
                        "required", api.SqlType.text(), api.Nullability.NOT_NULLABLE),
                    api.TableDefinition.Column("optional", api.SqlType.double()),
                ]))
                connection.execute_command(
                    f"INSERT INTO {orders} VALUES ('a', 1.5), ('b', NULL)")
                facts = api.TableName("Other Schema", "Facts")
                connection.catalog.create_table(api.TableDefinition(facts, [
                    api.TableDefinition.Column("amount", api.SqlType.double()),
                ]))
                connection.execute_command(f"INSERT INTO {facts} VALUES (2.5)")

    def test_discover_multi_table_reports_real_nullability_and_schemas(self):
        # The readers start Hyper without a log_dir, so hyperd.log lands in the cwd.
        with contextlib.chdir(self.root):
            tables = _hyper_reader.discover_multi_table_hyper(self.path)
        found = {(t["schema"], t["table"]): t for t in tables}
        self.assertEqual(set(found), {("Extract", "Orders"), ("Other Schema", "Facts")})
        self.assertEqual(found[("Extract", "Orders")]["row_count"], 2)
        self.assertEqual(found[("Other Schema", "Facts")]["row_count"], 1)
        self.assertEqual(
            [(c["name"], c["nullable"]) for c in found[("Extract", "Orders")]["columns"]],
            [("required", False), ("optional", True)],
        )

    def test_read_hyper_reports_real_schema_names(self):
        with contextlib.chdir(self.root):
            result = read_hyper(self.path, max_rows=2)
        self.assertEqual(result["format"], "hyper_api")
        self.assertEqual(
            {(t["schema"], t["table"]) for t in result["tables"]},
            {("Extract", "Orders"), ("Other Schema", "Facts")},
        )


class TestReadHyperSqlite(unittest.TestCase):
    def _create_test_db(self, rows=None):
        """Create a temp SQLite DB and return path."""
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute(
            'CREATE TABLE orders (id INTEGER, product TEXT, amount REAL)'
        )
        for r in (rows or []):
            conn.execute('INSERT INTO orders VALUES (?, ?, ?)', r)
        conn.commit()
        conn.close()
        return path

    def test_reads_sqlite_db(self):
        path = self._create_test_db([(1, 'Widget', 9.99), (2, 'Gadget', 19.99)])
        try:
            tables = _read_hyper_sqlite(path, max_rows=10)
            self.assertIsNotNone(tables)
            self.assertEqual(len(tables), 1)
            t = tables[0]
            self.assertEqual(t['table'], 'orders')
            self.assertEqual(t['column_count'], 3)
            self.assertEqual(t['row_count'], 2)
            self.assertEqual(len(t['sample_rows']), 2)
            self.assertEqual(t['sample_rows'][0]['product'], 'Widget')
        finally:
            os.unlink(path)

    def test_empty_table(self):
        path = self._create_test_db([])
        try:
            tables = _read_hyper_sqlite(path, max_rows=5)
            self.assertIsNotNone(tables)
            self.assertEqual(tables[0]['row_count'], 0)
            self.assertEqual(tables[0]['sample_rows'], [])
        finally:
            os.unlink(path)

    def test_max_rows_limit(self):
        rows = [(i, f'item{i}', float(i)) for i in range(50)]
        path = self._create_test_db(rows)
        try:
            tables = _read_hyper_sqlite(path, max_rows=5)
            self.assertEqual(len(tables[0]['sample_rows']), 5)
            self.assertEqual(tables[0]['row_count'], 50)
        finally:
            os.unlink(path)

    def test_non_sqlite_file_returns_none(self):
        fd, path = tempfile.mkstemp(suffix='.hyper')
        os.write(fd, b'HyPe\x00not-a-sqlite-file-at-all')
        os.close(fd)
        try:
            result = _read_hyper_sqlite(path)
            self.assertIsNone(result)
        finally:
            os.unlink(path)

    def test_no_tables_returns_none(self):
        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.close()
        try:
            result = _read_hyper_sqlite(path)
            self.assertIsNone(result)
        finally:
            os.unlink(path)


# ── _read_hyper_header ─────────────────────────────────────────────


class TestReadHyperHeader(unittest.TestCase):
    def test_parses_create_table(self):
        text = (
            'HyPe\x00CREATE TABLE "Orders" (id integer, name text)\n'
            'INSERT INTO "Orders" VALUES (1, \'Alice\')\n'
        )
        raw = text.encode('utf-8')
        tables = _read_hyper_header(raw, max_rows=5)
        self.assertIsNotNone(tables)
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]['table'], 'Orders')
        self.assertEqual(len(tables[0]['columns']), 2)
        self.assertGreater(len(tables[0]['sample_rows']), 0)

    def test_no_create_returns_none(self):
        raw = b'HyPe\x00\x00\x00just random binary data'
        result = _read_hyper_header(raw)
        self.assertIsNone(result)

    def test_unicode_error_returns_none(self):
        # Passing empty bytes should return None
        result = _read_hyper_header(b'')
        self.assertIsNone(result)


# ── _parse_inserts ─────────────────────────────────────────────────


class TestParseInserts(unittest.TestCase):
    def test_basic_insert(self):
        text = "INSERT INTO orders VALUES (1, 'Widget', 9.99)"
        columns = [
            {'name': 'id', 'hyper_type': 'integer'},
            {'name': 'product', 'hyper_type': 'text'},
            {'name': 'amount', 'hyper_type': 'real'},
        ]
        rows = _parse_inserts(text, 'orders', columns, 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], '1')
        self.assertEqual(rows[0]['product'], 'Widget')

    def test_null_value(self):
        text = "INSERT INTO t VALUES (NULL, 'x')"
        columns = [
            {'name': 'a', 'hyper_type': 'text'},
            {'name': 'b', 'hyper_type': 'text'},
        ]
        rows = _parse_inserts(text, 't', columns, 10)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['a'])

    def test_max_rows_respected(self):
        text = (
            "INSERT INTO t VALUES (1, 'a'), (2, 'b'), (3, 'c'), (4, 'd')"
        )
        columns = [
            {'name': 'id', 'hyper_type': 'integer'},
            {'name': 'val', 'hyper_type': 'text'},
        ]
        rows = _parse_inserts(text, 't', columns, 2)
        self.assertEqual(len(rows), 2)


# ── read_hyper ─────────────────────────────────────────────────────


class TestReadHyper(unittest.TestCase):
    def test_nonexistent_file(self):
        result = read_hyper('/nonexistent/path.hyper')
        self.assertEqual(result['tables'], [])
        self.assertEqual(result['format'], 'unknown')

    def test_none_path(self):
        result = read_hyper(None)
        self.assertEqual(result['tables'], [])

    def test_empty_path(self):
        result = read_hyper('')
        self.assertEqual(result['tables'], [])

    def test_sqlite_format_detected(self):
        """Real SQLite file → format='sqlite'."""
        fd, path = tempfile.mkstemp(suffix='.hyper')
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE t (id INTEGER, name TEXT)')
        conn.execute("INSERT INTO t VALUES (1, 'Alice')")
        conn.commit()
        conn.close()
        try:
            result = read_hyper(path)
            self.assertEqual(result['format'], 'sqlite')
            self.assertEqual(len(result['tables']), 1)
            self.assertEqual(result['tables'][0]['table'], 't')
        finally:
            os.unlink(path)

    def test_hyper_format_header_fallback(self):
        """File with HyPe magic + CREATE TABLE → header scan."""
        content = (
            b'HyPe\x00\x00\x00\x00'
            b'CREATE TABLE "data" (id integer, val text)\n'
            b"INSERT INTO \"data\" VALUES (1, 'test')\n"
        )
        fd, path = tempfile.mkstemp(suffix='.hyper')
        os.write(fd, content)
        os.close(fd)
        try:
            result = read_hyper(path)
            self.assertEqual(result['format'], 'hyper')
            self.assertGreater(len(result['tables']), 0)
        finally:
            os.unlink(path)

    def test_unknown_format(self):
        """File with unknown magic bytes → unknown format."""
        fd, path = tempfile.mkstemp(suffix='.hyper')
        os.write(fd, b'\x00\x00\x00\x00random binary data')
        os.close(fd)
        try:
            result = read_hyper(path)
            self.assertEqual(result['format'], 'unknown')
        finally:
            os.unlink(path)


# ── read_hyper_from_twbx ──────────────────────────────────────────


class TestReadHyperFromTwbx(unittest.TestCase):
    def _create_twbx_with_hyper(self, hyper_data, hyper_name='Data/Extract.hyper'):
        """Create a temp .twbx ZIP containing a .hyper entry."""
        fd, path = tempfile.mkstemp(suffix='.twbx')
        os.close(fd)
        with zipfile.ZipFile(path, 'w') as zf:
            zf.writestr(hyper_name, hyper_data)
        return path

    def test_reads_hyper_from_twbx(self):
        # Create a SQLite-based hyper file content
        fd, hyper_path = tempfile.mkstemp(suffix='.hyper')
        os.close(fd)
        conn = sqlite3.connect(hyper_path)
        conn.execute('CREATE TABLE t (a INTEGER)')
        conn.execute('INSERT INTO t VALUES (42)')
        conn.commit()
        conn.close()
        with open(hyper_path, 'rb') as f:
            hyper_bytes = f.read()
        os.unlink(hyper_path)

        twbx_path = self._create_twbx_with_hyper(hyper_bytes)
        try:
            results = read_hyper_from_twbx(twbx_path)
            self.assertEqual(len(results), 1)
            self.assertIn('archive_path', results[0])
            self.assertIn('original_filename', results[0])
        finally:
            os.unlink(twbx_path)

    def test_filter_by_hyper_filename(self):
        fd, hyper_path = tempfile.mkstemp(suffix='.hyper')
        os.close(fd)
        conn = sqlite3.connect(hyper_path)
        conn.execute('CREATE TABLE t (x TEXT)')
        conn.commit()
        conn.close()
        with open(hyper_path, 'rb') as f:
            hyper_bytes = f.read()
        os.unlink(hyper_path)

        fd, twbx_path = tempfile.mkstemp(suffix='.twbx')
        os.close(fd)
        with zipfile.ZipFile(twbx_path, 'w') as zf:
            zf.writestr('Data/Extract.hyper', hyper_bytes)
            zf.writestr('Data/Other.hyper', hyper_bytes)
        try:
            results = read_hyper_from_twbx(twbx_path, hyper_filename='Extract.hyper')
            self.assertEqual(len(results), 1)
            self.assertIn('Extract.hyper', results[0]['original_filename'])
        finally:
            os.unlink(twbx_path)

    def test_nonexistent_twbx(self):
        results = read_hyper_from_twbx('/nonexistent/file.twbx')
        self.assertEqual(results, [])

    def test_none_path(self):
        self.assertEqual(read_hyper_from_twbx(None), [])

    def test_not_a_zip(self):
        fd, path = tempfile.mkstemp(suffix='.twbx')
        os.write(fd, b'not a zip file')
        os.close(fd)
        try:
            with self.assertRaises(ExtractionError) as raised:
                read_hyper_from_twbx(path)
            self.assertIsInstance(raised.exception.__cause__, zipfile.BadZipFile)
        finally:
            os.unlink(path)


# ── generate_m_inline_table ────────────────────────────────────────


class TestGenerateMInlineTable(unittest.TestCase):
    def test_basic_inline_table(self):
        info = {
            'table': 'Sales',
            'columns': [
                {'name': 'id', 'hyper_type': 'integer'},
                {'name': 'product', 'hyper_type': 'text'},
            ],
            'sample_rows': [
                {'id': 1, 'product': 'Widget'},
                {'id': 2, 'product': 'Gadget'},
            ],
        }
        m = generate_m_inline_table(info)
        self.assertIn('#table(', m)
        self.assertIn('Int64.Type', m)
        self.assertIn('Text.Type', m)
        self.assertIn('"Widget"', m)
        self.assertIn('"Gadget"', m)
        self.assertIn('let', m)
        self.assertIn('in', m)

    def test_empty_columns(self):
        info = {'table': 'Empty', 'columns': [], 'sample_rows': []}
        m = generate_m_inline_table(info)
        self.assertIn('No columns found', m)

    def test_no_rows(self):
        info = {
            'table': 'Schema',
            'columns': [{'name': 'x', 'hyper_type': 'text'}],
            'sample_rows': [],
        }
        m = generate_m_inline_table(info)
        self.assertIn('{}', m)
        self.assertIn('[x]', m)

    def test_null_values(self):
        info = {
            'table': 'WithNulls',
            'columns': [{'name': 'a', 'hyper_type': 'text'}],
            'sample_rows': [{'a': None}],
        }
        m = generate_m_inline_table(info)
        self.assertIn('null', m)


# ── generate_m_csv_reference ──────────────────────────────────────


class TestGenerateMCsvReference(unittest.TestCase):
    def test_basic_csv_reference(self):
        info = {
            'table': 'BigData',
            'columns': [
                {'name': 'id', 'hyper_type': 'integer'},
                {'name': 'value', 'hyper_type': 'real'},
            ],
        }
        m = generate_m_csv_reference(info)
        self.assertIn('Csv.Document', m)
        self.assertIn('BigData.csv', m)
        self.assertIn('Int64.Type', m)
        self.assertIn('Number.Type', m)

    def test_custom_csv_filename(self):
        info = {
            'table': 'T',
            'columns': [{'name': 'x', 'hyper_type': 'text'}],
        }
        m = generate_m_csv_reference(info, csv_filename='custom.csv')
        self.assertIn('custom.csv', m)
        self.assertNotIn('T.csv', m)

    def test_real_hyper_magic_is_detected(self):
        with tempfile.NamedTemporaryFile(suffix='.hyper', delete=False) as handle:
            path = handle.name
            handle.write(b'Hyper' + b'\0' * 11)
        try:
            with mock.patch.object(_hyper_reader, '_read_hyper_api', return_value=[]), \
                 mock.patch.object(_hyper_reader, '_read_hyper_sqlite', return_value=[]), \
                 mock.patch.object(_hyper_reader, '_read_hyper_header', return_value=[]):
                self.assertEqual(_hyper_reader.read_hyper(path)['format'], 'hyper')
        finally:
            os.unlink(path)


# ── generate_m_for_hyper_table ─────────────────────────────────────


class TestGenerateMForHyperTable(unittest.TestCase):
    def test_small_table_uses_inline(self):
        info = {
            'table': 'Small',
            'columns': [{'name': 'id', 'hyper_type': 'integer'}],
            'sample_rows': [{'id': 1}],
            'row_count': 10,
        }
        m = generate_m_for_hyper_table(info)
        self.assertIn('#table(', m)
        self.assertNotIn('Csv.Document', m)

    def test_large_table_uses_csv(self):
        info = {
            'table': 'Big',
            'columns': [{'name': 'id', 'hyper_type': 'integer'}],
            'sample_rows': [],
            'row_count': INLINE_ROW_THRESHOLD + 1,
        }
        m = generate_m_for_hyper_table(info)
        self.assertIn('Csv.Document', m)

    def test_threshold_boundary_uses_inline(self):
        info = {
            'table': 'Edge',
            'columns': [{'name': 'id', 'hyper_type': 'integer'}],
            'sample_rows': [],
            'row_count': INLINE_ROW_THRESHOLD,
        }
        m = generate_m_for_hyper_table(info)
        # At threshold, should use inline
        self.assertIn('#table(', m)


if __name__ == '__main__':
    unittest.main()
