"""Streaming Hyper exports: data fidelity, truthful manifests, and atomicity."""

import csv
import datetime
import gc
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import tracemalloc
import unittest
import zipfile
from contextlib import ExitStack, redirect_stdout
from decimal import Decimal
from types import ModuleType, SimpleNamespace
from unittest import mock

import migrate
from powerbi_import.errors import ConfigurationError
from tests.hyper_test_helpers import StreamingHyperApi


class TestStreamingHyperCsvExport(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.hyper = os.path.join(self.root, "synthetic.hyper")
        with open(self.hyper, "wb") as stream:
            stream.write(b"Hyper synthetic fixture")
        self.identity = {
            "workbook": os.path.join(self.root, "synthetic.twbx"),
            "archive_member": "Outside/Case/synthetic.hyper",
        }

    def _export(self, api, **kwargs):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(
                "sys.modules", {"tableauhyperapi": api.module},
            ))
            sqlite_fallback = stack.enter_context(mock.patch(
                "sqlite3.connect", side_effect=AssertionError("unexpected SQLite fallback"),
            ))
            metadata_fallback = stack.enter_context(mock.patch(
                "tableau_export.hyper_reader.read_hyper",
                side_effect=AssertionError("metadata samples are not a data export"),
            ))
            stack.enter_context(redirect_stdout(io.StringIO()))
            result = migrate._hyper_to_csv_files(
                self.hyper, self.root, "Synthetic", csv, identity=self.identity,
                export_format="csv", **kwargs,
            )
            sqlite_fallback.assert_not_called()
            metadata_fallback.assert_not_called()
        api.connection.execute_list_query.assert_not_called()
        self.assertTrue(api.results, "Expected a streaming query result")
        self.assertTrue(all(result.closed for result in api.results))
        return result

    def _read_export(self, result):
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result.exports), 1)
        record = result.exports[0]
        path = os.path.join(self.root, record["csv_filename"])
        with open(path, encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        return record, path, rows

    def _assert_manifest(self, record, row_count, total_rows, max_rows=None,
                         sampled=False):
        self.assertEqual(record["format"], "csv")
        self.assertEqual(record["filename"], record["csv_filename"])
        self.assertEqual(record["row_count"], row_count)
        self.assertEqual(record["total_rows"], total_rows)
        self.assertEqual(record["max_rows"], max_rows)
        self.assertIs(record["truncated"], None if total_rows is None else sampled)
        self.assertIs(record["sampled"], sampled)

    def test_streaming_values_keep_csv_types_nulls_quotes_and_exact_identity(self):
        columns = ["ID", 'Text "réalisé"', "Null", "Date", "Timestamp", "Decimal", "Flag"]
        rows = [
            (1, 'comma, quote " and\nnewline', None, datetime.date(2026, 10, 22),
             datetime.datetime(2026, 10, 22, 13, 15, 5), Decimal("12.340"), True),
            (2, "é", "", datetime.date(2024, 2, 29), None, Decimal("-0.5"), False),
        ]
        schema, table = 'Schema."Case"', 'Physical."réalisé"'
        api = StreamingHyperApi(
            columns=columns, rows=rows, table_pairs=[(schema, table)],
        )
        record, path, actual = self._read_export(self._export(api))
        expected = io.StringIO(newline="")
        writer = csv.writer(expected)
        writer.writerow(columns)
        writer.writerows(rows)
        self.assertEqual(actual, list(csv.reader(io.StringIO(expected.getvalue()))))
        self.assertEqual(record["columns"], columns)
        self.assertEqual(record["identity"], dict(self.identity, schema=schema, table=table))
        self.assertRegex(os.path.basename(path), r"^hyper_[0-9a-f]{64}\.csv$")
        self._assert_manifest(record, row_count=2, total_rows=2)

    def test_empty_table_is_a_header_only_export_not_a_missing_table(self):
        api = StreamingHyperApi(columns=["id", "label"], rows=[], total_rows=0)
        record, unused, rows = self._read_export(self._export(api))
        self.assertEqual(rows, [["id", "label"]])
        self._assert_manifest(record, row_count=0, total_rows=0)

    def test_export_cap_is_pushed_into_query_and_reported_separately_from_total(self):
        api = StreamingHyperApi(
            row_factory=lambda: ((i, f"row-{i}") for i in range(37)), total_rows=37,
        )
        record, unused, rows = self._read_export(self._export(api, max_rows=7))
        self.assertEqual(rows, [["id", "label"]] + [[str(i), f"row-{i}"] for i in range(7)])
        self.assertTrue(any(
            re.search(r"\bLIMIT\s+7\b", sql, re.I)
            for sql in api.queries if not re.search(r"\bCOUNT\s*\(", sql, re.I)
        ), api.queries)
        self._assert_manifest(record, row_count=7, total_rows=37, max_rows=7, sampled=True)

    def test_count_failure_keeps_unknown_total_and_still_streams_actual_rows(self):
        api = StreamingHyperApi(count_error=RuntimeError("synthetic count unavailable"))
        record, unused, rows = self._read_export(self._export(api))
        self.assertEqual(rows[1:], [["1", "first"], ["2", "second"]])
        self._assert_manifest(record, row_count=2, total_rows=None)

    def test_progress_starts_before_count_and_finishes_after_close_and_publish(self):
        api = StreamingHyperApi()
        progress = mock.Mock()
        events = []
        output_streams = []
        accepted_rows = []
        real_writer = csv.writer
        real_replace = os.replace

        class ObservedWriter:
            def __init__(self, stream, *args, **kwargs):
                output_streams.append(stream)
                self.writer = real_writer(stream, *args, **kwargs)
                self.header_written = False

            def writerow(self, row):
                result = self.writer.writerow(row)
                if self.header_written:
                    accepted_rows.append(tuple(row))
                self.header_written = True
                return result

            def writerows(self, rows):
                for row in rows:
                    self.writerow(row)

        def count(sql):
            self.assertIn("start", events)
            self.assertFalse(accepted_rows)
            events.append("count")
            return 2

        def update(rows_written, total_rows=None):
            self.assertEqual(rows_written, len(accepted_rows))
            events.append("update")

        def replace(source, destination):
            self.assertTrue(all(stream.closed for stream in output_streams))
            real_replace(source, destination)
            events.append("publish")

        def finish(rows_written, total_rows=None, output_path=None, sampled=False):
            self.assertIn("publish", events)
            self.assertTrue(all(stream.closed for stream in output_streams))
            self.assertTrue(all(cursor.closed for cursor in api.results))
            self.assertTrue(os.path.isfile(output_path))
            self.assertGreater(os.path.getsize(output_path), 0)
            self.assertEqual(rows_written, len(accepted_rows))
            events.append("finish")

        progress.start.side_effect = lambda *a, **k: events.append("start")
        progress.update.side_effect = update
        progress.finish.side_effect = finish
        api.connection.execute_scalar_query.side_effect = count
        with mock.patch.object(csv, "writer", side_effect=ObservedWriter), \
                mock.patch.object(migrate.os, "replace", side_effect=replace):
            self._export(api, progress=progress)
        self.assertEqual(events[0:2], ["start", "count"])
        self.assertEqual(events[-2:], ["publish", "finish"])
        progress.fail.assert_not_called()

    def test_count_failure_is_visible_in_logs_and_non_tty_progress_has_no_fake_eta(self):
        from powerbi_import.progress import StreamingProgress
        from tests.test_progress import FakeClock, RecordingStream

        clock = FakeClock()
        stream = RecordingStream(clock)
        progress = StreamingProgress(stream=stream, clock=clock)
        api = StreamingHyperApi(count_error=RuntimeError("count deliberately unavailable"))
        with self.assertLogs(migrate.logger.name, level="WARNING") as warnings:
            result = self._export(api, progress=progress)
        self.assertTrue(any("unknown" in line.lower() for line in warnings.output))
        self.assertIsNone(result.exports[0]["total_rows"])
        self.assertNotIn("%", stream.getvalue())
        self.assertNotIn("ETA", stream.getvalue())
        self.assertNotIn("\r", stream.getvalue())
        self.assertTrue(all(text.endswith("\n") for unused, text in stream.records))
        self.assertEqual(stream.flush_count, len(stream.records))

    def test_cursor_failure_preserves_original_exception_and_existing_final_file(self):
        api = StreamingHyperApi()
        result = self._export(api)
        unused, path, unused_rows = self._read_export(result)
        with open(path, "rb") as stream:
            original = stream.read()
        baseline = set(os.listdir(self.root))
        failure = RuntimeError("synthetic failure after first written row")
        failed_api = StreamingHyperApi(fail_after=1, error=failure)
        with mock.patch.dict("sys.modules", {"tableauhyperapi": failed_api.module}), \
                mock.patch("sqlite3.connect") as sqlite_fallback, \
                mock.patch("tableau_export.hyper_reader.read_hyper") as metadata_fallback, \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError) as raised:
                migrate._hyper_to_csv_files(
                    self.hyper, self.root, "Synthetic", csv, identity=self.identity,
                    export_format="csv",
                )
        self.assertIs(raised.exception, failure)
        sqlite_fallback.assert_not_called()
        metadata_fallback.assert_not_called()
        self.assertTrue(all(result.closed for result in failed_api.results))
        with open(path, "rb") as stream:
            self.assertEqual(stream.read(), original)
        self.assertEqual(set(os.listdir(self.root)), baseline, "partial export leaked")

    def test_failed_atomic_replace_leaves_existing_export_intact_and_cleans_temp(self):
        api = StreamingHyperApi()
        unused, path, unused_rows = self._read_export(self._export(api))
        with open(path, "rb") as stream:
            original = stream.read()
        baseline = set(os.listdir(self.root))
        failure = PermissionError("synthetic atomic replace denied")
        progress = mock.Mock()
        real_replace = os.replace
        injected = []

        def fail_publication_once(source, destination):
            # Fail the temp -> final publication, not the earlier backup move.
            # Restoration of the old final must still be possible.
            if os.path.normcase(destination) == os.path.normcase(path) and not injected:
                injected.append((source, destination))
                raise failure
            return real_replace(source, destination)

        with mock.patch.dict("sys.modules", {"tableauhyperapi": api.module}), \
                mock.patch.object(migrate.os, "replace", side_effect=fail_publication_once), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(PermissionError) as raised:
                migrate._hyper_to_csv_files(
                    self.hyper, self.root, "Synthetic", csv, identity=self.identity,
                    export_format="csv", progress=progress,
                )
        self.assertIs(raised.exception, failure)
        self.assertEqual(len(injected), 1)
        progress.finish.assert_not_called()
        progress.fail.assert_called()
        with open(path, "rb") as stream:
            self.assertEqual(stream.read(), original)
        self.assertEqual(set(os.listdir(self.root)), baseline)

    def test_csv_writer_batches_never_exceed_10000_rows(self):
        count = 20003
        api = StreamingHyperApi(
            row_factory=lambda: ((i, f"row-{i}") for i in range(count)), total_rows=count,
        )
        real_writer = csv.writer
        batch_sizes = []

        class BoundedWriter:
            def __init__(self, *args, **kwargs):
                self.writer = real_writer(*args, **kwargs)

            def writerow(self, row):
                return self.writer.writerow(row)

            def writerows(self, rows):
                size = 0
                for row in rows:
                    size += 1
                    if size > 10000:
                        raise AssertionError("CSV export materialized an oversized batch")
                    self.writer.writerow(row)
                batch_sizes.append(size)

        with mock.patch.object(csv, "writer", side_effect=BoundedWriter):
            result = self._export(api)
        self.assertEqual(result.exports[0]["row_count"], count)
        self.assertEqual(sum(cursor.consumed for cursor in api.results), count)
        self.assertTrue(all(size <= 10000 for size in batch_sizes))

    def test_lazy_many_row_export_has_bounded_measurable_peak_memory(self):
        count = 80000
        api = StreamingHyperApi(
            row_factory=lambda: ((i, f"{i:08d}-" + "x" * 96) for i in range(count)),
            total_rows=count,
        )
        # This one double deliberately permits the old shortcut so tracemalloc
        # detects its >18 MiB materialized list, rather than failing at the mock.
        api.connection.execute_list_query.side_effect = lambda sql: list(api.row_factory())
        gc.collect()
        self.assertFalse(tracemalloc.is_tracing(), "memory probe requires its own trace")
        tracemalloc.start()
        try:
            with mock.patch.dict("sys.modules", {"tableauhyperapi": api.module}), \
                    redirect_stdout(io.StringIO()):
                result = migrate._hyper_to_csv_files(
                    self.hyper, self.root, "Synthetic", csv, identity=self.identity,
                    export_format="csv",
                )
            unused_current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        # Bounded 10,000-row batches fit; an 80,000-row materialized result does
        # not. Narrow rows leave headroom for overlapping batch lifetimes and
        # Python/CSV allocator differences without creating a large disk file.
        self.assertLess(peak, 6 * 1024 * 1024, f"export peak was {peak:,} bytes")
        self.assertEqual(result.exports[0]["row_count"], count)
        api.connection.execute_list_query.assert_not_called()


class _SqliteCursor:
    def __init__(self, cursor, statements, batches):
        self.cursor = cursor
        self.statements = statements
        self.batches = batches
        self.sql = ""

    def execute(self, sql, *args):
        self.sql = sql
        self.statements.append(sql)
        self.cursor.execute(sql, *args)
        return self

    def fetchall(self):
        is_select = re.match(r"\s*SELECT\b", self.sql, re.I)
        is_catalog = re.search(r"\b(?:sqlite_master|sqlite_schema)\b", self.sql, re.I)
        is_count = re.match(r"\s*SELECT\s+COUNT\s*\(", self.sql, re.I)
        if is_select and not is_catalog and not is_count:
            raise AssertionError("SQLite data must not be materialized by fetchall")
        return self.cursor.fetchall()

    def fetchmany(self, size=10000):
        if not 0 < size <= 10000:
            raise AssertionError(f"unbounded SQLite fetchmany({size})")
        rows = self.cursor.fetchmany(size)
        self.batches.append(len(rows))
        return rows

    def __iter__(self):
        return iter(self.cursor)

    def __getattr__(self, name):
        return getattr(self.cursor, name)


class _SqliteConnection:
    def __init__(self, connection):
        self.connection = connection
        self.statements = []
        self.batches = []

    def cursor(self):
        return _SqliteCursor(self.connection.cursor(), self.statements, self.batches)

    def execute(self, sql, *args):
        return self.cursor().execute(sql, *args)

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()
        return False

    def __getattr__(self, name):
        return getattr(self.connection, name)


class TestStreamingSqliteHyperExport(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.path = os.path.join(self.root, "synthetic.hyper")
        self.table = 'Orders "exact"'
        connection = sqlite3.connect(self.path)
        connection.execute('CREATE TABLE "Orders ""exact""" (id INTEGER, label TEXT)')
        connection.executemany(
            'INSERT INTO "Orders ""exact""" VALUES (?, ?)',
            ((i, None if i == 1 else f"row,{i}") for i in range(23)),
        )
        connection.execute("CREATE TABLE Empty (id INTEGER, label TEXT)")
        connection.commit()
        connection.close()
        self.wrapped = _SqliteConnection(sqlite3.connect(self.path))
        self.addCleanup(self.wrapped.close)

    def _export(self, **kwargs):
        with mock.patch.dict("sys.modules", {"tableauhyperapi": None}), \
                mock.patch("sqlite3.connect", return_value=self.wrapped), \
                redirect_stdout(io.StringIO()):
            return migrate._hyper_to_csv_files(
                self.path, self.root, "Synthetic", csv, export_format="csv", **kwargs,
            )

    def test_schema_fetchall_is_allowed_but_data_streams_and_empty_header_survives(self):
        result = self._export()
        by_table = {record["identity"]["table"]: record for record in result.exports}
        self.assertEqual(set(by_table), {self.table, "Empty"})
        for table, expected in (
            (self.table, [["id", "label"]] + [
                [str(i), "" if i == 1 else f"row,{i}"] for i in range(23)
            ]),
            ("Empty", [["id", "label"]]),
        ):
            record = by_table[table]
            with open(os.path.join(self.root, record["csv_filename"]),
                      encoding="utf-8", newline="") as stream:
                self.assertEqual(list(csv.reader(stream)), expected)
            self.assertEqual(record["row_count"], len(expected) - 1)
        self.assertTrue(all(size <= 10000 for size in self.wrapped.batches))

    def test_sqlite_row_cap_uses_limit_and_manifest_never_claims_full_data(self):
        result = self._export(max_rows=5)
        record = next(r for r in result.exports if r["identity"]["table"] == self.table)
        self.assertEqual(record["row_count"], 5)
        self.assertEqual(record["total_rows"], 23)
        self.assertEqual(record["max_rows"], 5)
        self.assertIs(record["sampled"], True)
        self.assertIs(record["truncated"], True)
        self.assertTrue(any(
            re.search(r"\bLIMIT\s+5\b", sql, re.I) for sql in self.wrapped.statements
        ))


class TestNativeHyperExportFormats(unittest.TestCase):
    """COPY syntax is mocked here; real SDK round-trips run separately."""

    THRESHOLD = 10 * 1024 * 1024

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="hyper O'Brien ")
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.path = os.path.join(self.root, "synthetic.hyper")
        with open(self.path, "wb") as stream:
            stream.write(b"Hyper synthetic fixture")
        self.identity = {
            "workbook": os.path.join(self.root, "synthetic.twbx"),
            "archive_member": "Outside/Exact/synthetic.hyper",
        }
        self.copy_paths = []

    def _api(self):
        api = StreamingHyperApi(
            columns=["Z", "réalisé"], rows=[(1, "first"), (2, "second")],
            table_pairs=[('Schema."Case"', 'Physical."réalisé"')],
        )

        def native_copy(sql):
            match = re.search(r"\bTO\s+'((?:[^']|'')*)'", sql, re.I)
            self.assertIsNotNone(match, f"COPY destination is not SQL quoted: {sql}")
            path = match[1].replace("''", "'")
            self.copy_paths.append(path)
            with open(path, "wb") as stream:
                # A marker only, not a claim to encode a real Parquet file.
                stream.write(b"PAR1synthetic native COPY markerPAR1")
            cap = re.search(r"\bLIMIT\s+(\d+)\b", sql, re.I)
            return min(2, int(cap[1])) if cap else 2

        api.connection.execute_command.side_effect = native_copy
        return api

    def _set_size(self, size):
        with open(self.path, "r+b") as stream:
            stream.truncate(size)

    def _typed_api(self, columns_and_tags, rows=None):
        api = self._api()
        api.columns = [name for name, tag in columns_and_tags]
        api.connection.catalog.get_table_definition.return_value.columns = [
            SimpleNamespace(
                name=SimpleNamespace(unescaped=name),
                type=SimpleNamespace(tag=SimpleNamespace(name=tag)),
                nullability=SimpleNamespace(is_nullable=True),
            )
            for name, tag in columns_and_tags
        ]
        if rows is not None:
            api.row_factory = lambda: iter(rows)
        return api

    def _export(self, api, export_format="auto", **kwargs):
        with mock.patch.dict("sys.modules", {"tableauhyperapi": api.module}), \
                mock.patch("sqlite3.connect") as sqlite_fallback, \
                mock.patch("tableau_export.hyper_reader.read_hyper") as metadata_fallback:
            result = migrate._hyper_to_csv_files(
                self.path, self.root, "Synthetic", csv, identity=self.identity,
                export_format=export_format, **kwargs,
            )
        sqlite_fallback.assert_not_called()
        metadata_fallback.assert_not_called()
        api.connection.execute_list_query.assert_not_called()
        self.assertEqual(len(result.exports), 1)
        return result.exports[0]

    def test_auto_csv_at_or_below_ten_mib_parquet_strictly_above(self):
        cases = (
            (1024, "csv"), (self.THRESHOLD - 1, "csv"),
            (self.THRESHOLD, "csv"), (self.THRESHOLD + 1, "parquet"),
        )
        for size, expected in cases:
            with self.subTest(size=size):
                self._set_size(size)
                api = self._api()
                record = self._export(api)
                self.assertEqual(record["format"], expected)
                self.assertTrue(record["filename"].endswith("." + expected))
                self.assertEqual(record["columns"], ["Z", "réalisé"])
                self.assertEqual(record["row_count"], 2)
                self.assertEqual(record["total_rows"], 2)
                self.assertIs(record["sampled"], False)
                self.assertIs(record["truncated"], False)
                if expected == "csv":
                    self.assertEqual(record["csv_filename"], record["filename"])
                    api.connection.execute_command.assert_not_called()
                    self.assertTrue(api.results)
                else:
                    self.assertNotIn("csv_filename", record)
                    api.connection.execute_command.assert_called_once()
                    api.connection.execute_query.assert_not_called()

    def test_native_copy_quotes_exact_table_and_path_and_uses_capped_subselect(self):
        api = self._api()
        record = self._export(api, export_format="parquet", max_rows=1)
        sql = api.connection.execute_command.call_args.args[0]
        self.assertIn('COPY (SELECT * FROM "Schema.""Case"""."Physical.""réalisé""" LIMIT 1)', sql)
        self.assertIn("O''Brien", sql)
        self.assertRegex(sql, r"(?i)\bWITH\s*\(\s*FORMAT\s+PARQUET\s*\)")
        self.assertEqual(record["row_count"], 1)
        self.assertEqual(record["total_rows"], 2)
        self.assertEqual(record["max_rows"], 1)
        self.assertIs(record["sampled"], True)
        self.assertIs(record["truncated"], True)
        self.assertEqual(
            record["identity"],
            dict(self.identity, schema='Schema."Case"', table='Physical."réalisé"'),
        )
        self.assertTrue(os.path.isfile(os.path.join(self.root, record["filename"])))
        self.assertTrue(all(not os.path.exists(path) for path in self.copy_paths))

    def test_missing_sdk_cannot_substitute_csv_for_required_large_auto_parquet(self):
        self._set_size(self.THRESHOLD + 1)
        baseline = set(os.listdir(self.root))
        with mock.patch.dict("sys.modules", {"tableauhyperapi": None}), \
                mock.patch("sqlite3.connect") as sqlite_fallback, \
                mock.patch("tableau_export.hyper_reader.read_hyper") as metadata_fallback:
            with self.assertRaisesRegex(ConfigurationError, "(?i)(parquet|tableauhyperapi)"):
                migrate._hyper_to_csv_files(self.path, self.root, "Synthetic", csv)
        sqlite_fallback.assert_not_called()
        metadata_fallback.assert_not_called()
        self.assertEqual(set(os.listdir(self.root)), baseline)

    def test_large_sqlite_header_auto_rejects_without_csv_or_partial_artifacts(self):
        with open(self.path, "wb") as stream:
            stream.write(b"SQLite format 3\x00synthetic header-only fixture")
        baseline = set(os.listdir(self.root))
        api = self._api()
        for sdk_present in (True, False):
            with self.subTest(sdk_present=sdk_present):
                with mock.patch.dict("sys.modules", {
                        "tableauhyperapi": api.module if sdk_present else None,
                }), \
                        mock.patch.object(
                            migrate.os.path, "getsize", return_value=self.THRESHOLD + 1,
                        ), \
                        mock.patch("sqlite3.connect") as sqlite_fallback, \
                        mock.patch(
                            "tableau_export.hyper_reader.read_hyper",
                        ) as metadata_fallback:
                    with self.assertRaises(ConfigurationError) as raised:
                        migrate._hyper_to_csv_files(
                            self.path, self.root, "Synthetic", csv, export_format="auto",
                        )
                message = str(raised.exception).lower()
                self.assertIn("parquet", message)
                # A known SQLite header may reject native Parquet before SDK
                # prerequisites are checked. Either clear failure is valid.
                self.assertRegex(message, r"sqlite|tableauhyperapi")
                sqlite_fallback.assert_not_called()
                metadata_fallback.assert_not_called()
                api.module.HyperProcess.assert_not_called()
                api.module.Connection.assert_not_called()
                self.assertEqual(set(os.listdir(self.root)), baseline)

    def test_explicit_parquet_without_sdk_fails_before_creating_output(self):
        baseline = set(os.listdir(self.root))
        with mock.patch.dict("sys.modules", {"tableauhyperapi": None}), \
                mock.patch("sqlite3.connect") as sqlite_fallback:
            with self.assertRaisesRegex(ConfigurationError, "(?i)(parquet|tableauhyperapi)"):
                migrate._hyper_to_csv_files(
                    self.path, self.root, "Synthetic", csv, export_format="parquet",
                )
        sqlite_fallback.assert_not_called()
        self.assertEqual(set(os.listdir(self.root)), baseline)

    def test_parquet_progress_is_truthful_start_final_without_fake_row_updates(self):
        from powerbi_import.progress import StreamingProgress
        from tests.test_progress import FakeClock, RecordingStream

        clock = FakeClock()
        stream = RecordingStream(clock)
        progress = StreamingProgress(stream=stream, clock=clock)
        api = self._api()
        record = self._export(api, export_format="parquet", progress=progress)
        self.assertEqual(len(stream.records), 2)
        self.assertIn("started", stream.records[0][1])
        self.assertIn("complete", stream.records[-1][1])
        self.assertIn("2 rows", stream.records[-1][1])
        self.assertIn(
            f"{os.path.getsize(os.path.join(self.root, record['filename']))} bytes",
            stream.records[-1][1],
        )
        self.assertNotIn("\r", stream.getvalue())
        self.assertEqual(stream.flush_count, 2)

    def test_parquet_unknown_count_does_not_invent_written_rows_or_completion_percent(self):
        from powerbi_import.progress import StreamingProgress
        from tests.test_progress import FakeClock, RecordingStream

        clock = FakeClock()
        stream = RecordingStream(clock)
        progress = StreamingProgress(stream=stream, clock=clock)
        api = self._api()
        api.connection.execute_scalar_query.side_effect = RuntimeError("count unavailable")
        native_copy = api.connection.execute_command.side_effect

        def copy_without_rowcount(sql):
            native_copy(sql)
            return None

        api.connection.execute_command.side_effect = copy_without_rowcount
        with self.assertLogs(migrate.logger.name, level="WARNING"):
            record = self._export(api, export_format="parquet", progress=progress)
        self.assertIsNone(record["row_count"])
        self.assertIsNone(record["total_rows"])
        self.assertIsNone(record["truncated"])
        self.assertIn("unknown", stream.records[-1][1].lower())
        self.assertNotIn("0 rows", stream.getvalue())
        self.assertNotIn("%", stream.getvalue())
        self.assertNotIn("ETA", stream.getvalue())

    def test_copy_failure_preserves_existing_parquet_and_removes_partial_temp(self):
        record = self._export(self._api(), export_format="parquet")
        path = os.path.join(self.root, record["filename"])
        original = b"PAR1existing-final-test-markerPAR1"
        with open(path, "wb") as stream:
            stream.write(original)
        baseline = set(os.listdir(self.root))
        api = self._api()
        failure = RuntimeError("synthetic native COPY failure")
        native_copy = api.connection.execute_command.side_effect

        def partial_copy_then_fail(sql):
            native_copy(sql)
            raise failure

        api.connection.execute_command.side_effect = partial_copy_then_fail
        with self.assertRaises(RuntimeError) as raised:
            self._export(api, export_format="parquet")
        self.assertIs(raised.exception, failure)
        with open(path, "rb") as stream:
            self.assertEqual(stream.read(), original)
        self.assertEqual(set(os.listdir(self.root)), baseline)

    def test_parquet_casts_only_metadata_geography_and_preserves_column_order(self):
        columns = [
            ("ID", "BIG_INT"), ("Location", "TABGEOGRAPHY"),
            ("geography", "TEXT"), ("Payload", "BYTE_A"),
            ("Boundary", "TABGEOGRAPHY"),
        ]
        api = self._typed_api(columns)
        record = self._export(api, export_format="parquet", quiet=True)
        sql = api.connection.execute_command.call_args.args[0]
        self.assertEqual(
            sql.partition(" TO '")[0],
            'COPY (SELECT "ID", CAST("Location" AS TEXT) AS "Location", '
            '"geography", "Payload", CAST("Boundary" AS TEXT) AS "Boundary" '
            'FROM "Schema.""Case"""."Physical.""réalisé""")',
        )
        self.assertNotIn("st_astext", sql.lower())
        self.assertEqual(record["columns"], [name for name, tag in columns])
        self.assertEqual(
            record["identity"],
            dict(self.identity, schema='Schema."Case"', table='Physical."réalisé"'),
        )
        self.assertEqual(record["format"], "parquet")
        self.assertEqual(record["row_count"], 2)
        self.assertEqual(record["total_rows"], 2)
        self.assertIs(record["sampled"], False)
        self.assertIs(record["truncated"], False)
        self.assertNotIn("csv_filename", record)
        api.connection.execute_command.assert_called_once()
        api.connection.execute_query.assert_not_called()
        self.assertTrue(all(not os.path.exists(path) for path in self.copy_paths))

    def test_geography_projection_escapes_column_and_same_name_alias_with_limit(self):
        columns = [
            ("select", "BIG_INT"), ('Geo."réalisé"', "TABGEOGRAPHY"),
            ("O'Brien", "TEXT"),
        ]
        api = self._typed_api(columns)
        record = self._export(
            api, export_format="parquet", max_rows=1, quiet=True,
        )
        sql = api.connection.execute_command.call_args.args[0]
        self.assertEqual(
            sql.partition(" TO '")[0],
            'COPY (SELECT "select", '
            'CAST("Geo.""réalisé""" AS TEXT) AS "Geo.""réalisé""", '
            '"O\'Brien" FROM "Schema.""Case"""."Physical.""réalisé""" LIMIT 1)',
        )
        self.assertIn("O''Brien", sql)
        self.assertRegex(sql, r"(?i)\bWITH\s*\(\s*FORMAT\s+PARQUET\s*\)")
        self.assertEqual(record["columns"], [name for name, tag in columns])
        self.assertEqual(record["max_rows"], 1)
        self.assertEqual(record["row_count"], 1)
        self.assertEqual(record["total_rows"], 2)
        self.assertIs(record["sampled"], True)
        self.assertIs(record["truncated"], True)

    def test_parquet_without_geography_metadata_keeps_original_star_projection(self):
        for metadata in ("native_tags", "type_strings_only"):
            with self.subTest(metadata=metadata):
                columns = [
                    ("geography", "TEXT"), ("Shape", "BYTE_A"), ("Day", "DATE"),
                ]
                api = self._typed_api(columns)
                if metadata == "type_strings_only":
                    definition = api.connection.catalog.get_table_definition.return_value
                    definition.columns[0].type = "TABLEAU.TABGEOGRAPHY"
                record = self._export(api, export_format="parquet", quiet=True)
                sql = api.connection.execute_command.call_args.args[0]
                self.assertEqual(
                    sql.partition(" TO '")[0],
                    'COPY (SELECT * FROM "Schema.""Case"""."Physical.""réalisé""")',
                )
                self.assertEqual(record["columns"], [name for name, tag in columns])
                api.connection.execute_command.assert_called_once()
                api.connection.execute_query.assert_not_called()

    def test_unsupported_non_geography_type_still_raises_without_coercion_or_fallback(self):
        for has_geography in (False, True):
            with self.subTest(has_geography=has_geography):
                columns = [("Duration", "INTERVAL")]
                if has_geography:
                    columns.insert(0, ("Location", "TABGEOGRAPHY"))
                api = self._typed_api(columns)
                failure = RuntimeError("INTERVAL is unsupported by native Parquet COPY")
                native_copy = api.connection.execute_command.side_effect
                baseline = set(os.listdir(self.root))

                def unsupported_copy(sql):
                    native_copy(sql)
                    raise failure

                api.connection.execute_command.side_effect = unsupported_copy
                with mock.patch.dict("sys.modules", {"tableauhyperapi": api.module}), \
                        mock.patch("sqlite3.connect") as sqlite_fallback, \
                        mock.patch(
                            "tableau_export.hyper_reader.read_hyper",
                        ) as metadata_fallback:
                    with self.assertRaises(RuntimeError) as raised:
                        migrate._hyper_to_csv_files(
                            self.path, self.root, "Synthetic", csv,
                            identity=self.identity, export_format="parquet", quiet=True,
                        )
                self.assertIs(raised.exception, failure)
                sql = api.connection.execute_command.call_args.args[0]
                projection = (
                    'CAST("Location" AS TEXT) AS "Location", "Duration"'
                    if has_geography else '*'
                )
                self.assertEqual(
                    sql.partition(" TO '")[0],
                    f'COPY (SELECT {projection} FROM '
                    '"Schema.""Case"""."Physical.""réalisé""")',
                )
                sqlite_fallback.assert_not_called()
                metadata_fallback.assert_not_called()
                api.connection.execute_command.assert_called_once()
                api.connection.execute_query.assert_not_called()
                api.connection.execute_list_query.assert_not_called()
                api.connection.close.assert_called_once()
                api.process.close.assert_called_once()
                self.assertEqual(set(os.listdir(self.root)), baseline)

    def test_geography_metadata_leaves_csv_values_as_bytes_repr_and_nulls(self):
        geography = b"\x07\xaa\x00\xff"
        columns = [
            ("ID", "BIG_INT"), ("Location", "TABGEOGRAPHY"), ("geography", "TEXT"),
        ]
        api = self._typed_api(
            columns, rows=[(1, geography, "POINT(9 9)"), (2, None, None)],
        )
        record = self._export(api, export_format="csv", quiet=True)
        with open(os.path.join(self.root, record["csv_filename"]),
                  encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows, [
            ["ID", "Location", "geography"],
            ["1", repr(geography), "POINT(9 9)"],
            ["2", "", ""],
        ])
        self.assertEqual(record["columns"], [name for name, tag in columns])
        self.assertEqual(record["row_count"], 2)
        api.connection.execute_query.assert_called_once_with(
            'SELECT * FROM "Schema.""Case"""."Physical.""réalisé"""',
        )
        api.connection.execute_command.assert_not_called()
        self.assertTrue(api.results)
        self.assertTrue(all(result.closed for result in api.results))


class TestNativeHyperGeographyRoundTrip(unittest.TestCase):
    def test_optional_sdk_synthetic_parquet_wkt_and_csv_bytes_roundtrip(self):
        try:
            import tableauhyperapi as api
        except ImportError:
            self.skipTest("optional tableauhyperapi SDK is not installed")

        tmp = tempfile.TemporaryDirectory(prefix="hyper geography O'Brien ")
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        hyper = os.path.join(root, "synthetic.hyper")
        out_dir = os.path.join(root, "exports")
        schema, table = 'Schema."Case"', 'Physical."réalisé"'
        identity = {
            "workbook": os.path.join(root, "synthetic.twbx"),
            "archive_member": "Data/synthetic.hyper",
        }
        columns = [
            'Row "ID"', 'Geo."réalisé"', "geography", "Amount", "Date", "Secondary",
        ]
        types = [
            api.SqlType.big_int(), api.SqlType.geography(), api.SqlType.text(),
            api.SqlType.numeric(12, 3), api.SqlType.date(), api.SqlType.geography(),
        ]
        table_name = api.TableName(schema, table)
        label = "quoted 'label', réalisé"
        native_process = api.HyperProcess
        telemetry = api.Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU
        with native_process(telemetry, parameters={"log_dir": root}) as process:
            with api.Connection(
                    process.endpoint, hyper, api.CreateMode.CREATE_AND_REPLACE,
            ) as connection:
                connection.catalog.create_schema(api.SchemaName(schema))
                connection.catalog.create_table(api.TableDefinition(
                    table_name, [
                        api.TableDefinition.Column(name, dtype)
                        for name, dtype in zip(columns, types)
                    ],
                ))
                connection.execute_command(
                    f"INSERT INTO {table_name} VALUES "
                    "(1, CAST('POINT (1 2)' AS GEOGRAPHY), "
                    f"{api.escape_string_literal(label)}, 12.340, DATE '2024-02-29', "
                    "CAST('POINT (3 4)' AS GEOGRAPHY)), "
                    "(2, NULL, NULL, NULL, NULL, NULL)"
                )
                raw_rows = connection.execute_list_query(
                    f"SELECT * FROM {table_name} ORDER BY 1",
                )
                wkt_rows = connection.execute_list_query(
                    f"SELECT CAST({api.Name(columns[1])} AS TEXT), "
                    f"CAST({api.Name(columns[5])} AS TEXT) FROM {table_name} ORDER BY 1",
                )
        self.assertIsInstance(raw_rows[0][1], bytes)
        self.assertIsInstance(raw_rows[0][5], bytes)

        def local_process(*args, **kwargs):
            # Keep even the exporter's SDK logs in the disposable fixture.
            kwargs["parameters"] = dict(kwargs.get("parameters") or {}, log_dir=root)
            return native_process(*args, **kwargs)

        records = {}
        with mock.patch.object(api, "HyperProcess", side_effect=local_process), \
                mock.patch("sqlite3.connect") as sqlite_fallback, \
                mock.patch("tableau_export.hyper_reader.read_hyper") as metadata_fallback:
            for export_format in ("parquet", "csv"):
                result = migrate._hyper_to_csv_files(
                    hyper, out_dir, "Synthetic", csv, identity=identity,
                    export_format=export_format, quiet=True,
                )
                self.assertEqual(len(result.exports), 1)
                records[export_format] = result.exports[0]
        sqlite_fallback.assert_not_called()
        metadata_fallback.assert_not_called()
        for export_format, record in records.items():
            with self.subTest(export_format=export_format):
                self.assertEqual(record["format"], export_format)
                self.assertEqual(record["columns"], columns)
                self.assertEqual(
                    record["identity"], dict(identity, schema=schema, table=table),
                )
                self.assertEqual(record["row_count"], 2)
                self.assertEqual(record["total_rows"], 2)
                self.assertIs(record["sampled"], False)
                self.assertIs(record["truncated"], False)

        parquet = os.path.join(out_dir, records["parquet"]["filename"]).replace("\\", "/")
        with native_process(telemetry, parameters={"log_dir": root}) as process:
            with api.Connection(process.endpoint, hyper) as connection:
                with connection.execute_query(
                    f"SELECT * FROM external({api.escape_string_literal(parquet)}, "
                    "FORMAT => 'parquet') ORDER BY 1",
                ) as result:
                    actual_columns = list(result.schema.columns)
                    parquet_rows = list(result)
        self.assertEqual([column.name.unescaped for column in actual_columns], columns)
        expected_types = list(types)
        expected_types[1] = expected_types[5] = api.SqlType.text()
        self.assertEqual([column.type for column in actual_columns], expected_types)
        expected_rows = [list(row) for row in raw_rows]
        for row, wkt in zip(expected_rows, wkt_rows):
            row[1], row[5] = wkt
        self.assertEqual(parquet_rows, expected_rows)
        self.assertRegex(
            parquet_rows[0][1], r"^POINT\s*\(\s*1(?:\.0+)?\s+2(?:\.0+)?\s*\)$",
        )
        self.assertRegex(
            parquet_rows[0][5], r"^POINT\s*\(\s*3(?:\.0+)?\s+4(?:\.0+)?\s*\)$",
        )
        self.assertIsNone(parquet_rows[1][1])
        self.assertIsNone(parquet_rows[1][5])

        with open(os.path.join(out_dir, records["csv"]["csv_filename"]),
                  encoding="utf-8", newline="") as stream:
            csv_rows = list(csv.reader(stream))
        self.assertEqual(csv_rows[0], columns)
        self.assertEqual(
            sorted(csv_rows[1:], key=lambda row: int(row[0])),
            [[str(value) if value is not None else "" for value in row] for row in raw_rows],
        )


class TestHyperOptionWiring(unittest.TestCase):
    """Drive real CLI dispatch/config resolution; mock only expensive workers."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.sources = os.path.join(self.root, "sources")
        self.output = os.path.join(self.root, "output")
        os.makedirs(self.sources)
        self.workbook = os.path.join(self.sources, "Synthetic.twb")
        with open(self.workbook, "w", encoding="utf-8") as stream:
            stream.write("<workbook/>")

    def _main(self, arguments, target="run_batch_migration"):
        with mock.patch.object(sys, "argv", [
                "migrate.py", "--output-dir", self.output, *arguments]), \
                mock.patch.object(migrate, "_install_upload_copy_guard"), \
                mock.patch.object(migrate, "setup_logging"), \
                mock.patch.object(migrate, target,
                                  return_value=migrate.ExitCode.SUCCESS) as worker, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(migrate.main(), migrate.ExitCode.SUCCESS)
        worker.assert_called_once()
        return worker.call_args.kwargs

    def test_cli_default_auto_and_explicit_import_override_reach_batch(self):
        for flags, expected in (([], "auto"), (["--mode", "import"], "import")):
            with self.subTest(mode=expected):
                kwargs = self._main(["--batch", self.sources, *flags])
                self.assertEqual(kwargs["model_mode"], expected)

    def test_explicit_export_and_zip_flags_reach_batch_with_sampling_limit_distinct(self):
        arguments = [
            "--batch", self.sources, "--mode", "import",
            "--hyper-rows", "2", "--hyper-export-max-rows", "7",
            "--hyper-export-format", "csv", "--max-zip-entry-mb", "3",
        ]
        parsed = migrate._build_argument_parser().parse_args(arguments)
        self.assertEqual(parsed.hyper_rows, 2)
        self.assertEqual(parsed.hyper_export_max_rows, 7)
        kwargs = self._main(arguments)
        self.assertEqual(kwargs["hyper_export_max_rows"], 7)
        self.assertEqual(kwargs["hyper_export_format"], "csv")
        self.assertEqual(kwargs["max_zip_entry_mb"], 3)
        self.assertEqual(kwargs["model_mode"], "import")

    def test_config_options_reach_dispatch_and_explicit_cli_auto_import_and_csv_win(self):
        config = os.path.join(self.root, "config.json")
        with open(config, "w", encoding="utf-8") as stream:
            json.dump({
                "model": {"mode": "import"},
                "migration": {
                    "max_zip_entry_mb": 5, "hyper_export_format": "parquet",
                    "hyper_export_max_rows": 11,
                },
            }, stream)
        foreign_config = ModuleType("config")
        foreign_config.__path__ = []
        # An unrelated package imported earlier must not shadow our config
        # loader. patch.dict restores the original module state on exit.
        with mock.patch.dict(sys.modules, {"config": foreign_config}):
            kwargs = self._main(["--batch", self.sources, "--config", config])
            self.assertEqual(kwargs["model_mode"], "import")
            self.assertEqual(kwargs["max_zip_entry_mb"], 5)
            self.assertEqual(kwargs["hyper_export_format"], "parquet")
            self.assertEqual(kwargs["hyper_export_max_rows"], 11)
            self.assertIs(sys.modules["config"], foreign_config)
            kwargs = self._main([
                "--batch", self.sources, "--config", config, "--mode", "auto",
                "--hyper-export-format", "csv", "--hyper-export-max-rows", "2",
                "--max-zip-entry-mb", "3",
            ])
            self.assertEqual(kwargs["model_mode"], "auto")
            self.assertEqual(kwargs["max_zip_entry_mb"], 3)
            self.assertEqual(kwargs["hyper_export_format"], "csv")
            self.assertEqual(kwargs["hyper_export_max_rows"], 2)
            self.assertIs(sys.modules["config"], foreign_config)

    def test_cli_shared_dispatch_forwards_hyper_export_and_zip_options(self):
        kwargs = self._main([
            "--shared-model", self.workbook, "--model-name", "Shared",
            "--mode", "import", "--hyper-export-format", "csv",
            "--hyper-export-max-rows", "7", "--max-zip-entry-mb", "3",
        ], target="run_shared_model_migration")
        self.assertEqual(kwargs["model_mode"], "import")
        self.assertEqual(kwargs["hyper_export_format"], "csv")
        self.assertEqual(kwargs["hyper_export_max_rows"], 7)
        self.assertEqual(kwargs["max_zip_entry_mb"], 3)

    def test_shared_orchestrator_forwards_options_to_extractor_and_postprocessing(self):
        package = os.path.join(self.root, "Synthetic.twbx")
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("Synthetic.twb", "<workbook/>")
            archive.writestr("Data/synthetic.hyper", b"Hyper synthetic fixture")
        converted = {"datasources": [{
            "name": "SyntheticSource", "connection": {"type": "Hyper", "details": {}},
            "tables": [{"name": "Facts", "columns": [{"name": "id", "datatype": "integer"}]}],
        }]}
        module_root = os.path.dirname(os.path.abspath(migrate.__file__))
        # These legacy module names are normally exposed by the orchestrator
        # only after this test must resolve its patch targets. Scope the path
        # setup so this case also works when run alone in a fresh interpreter.
        with mock.patch.object(sys, "path", [
                os.path.join(module_root, "tableau_export"),
                os.path.join(module_root, "powerbi_import"),
                *sys.path,
        ]), \
                mock.patch("extract_tableau_data.TableauExtractor") as extractor, \
                mock.patch("import_to_powerbi.PowerBIImporter") as importer, \
                mock.patch.object(migrate, "_process_twbx_post_generation") as post, \
                redirect_stdout(io.StringIO()):
            extractor.return_value.extract_all.return_value = True
            importer.return_value._load_converted_objects.return_value = converted
            importer.return_value.import_shared_model.return_value = {
                "model_path": os.path.join(self.output, "Shared", "Shared.SemanticModel"),
            }
            result = migrate.run_shared_model_migration(
                [package], model_name="Shared", output_dir=self.output,
                model_mode="import", verify_open=False,
                max_zip_entry_mb=3, hyper_export_format="csv", hyper_export_max_rows=7,
            )
        self.assertEqual(result, migrate.ExitCode.SUCCESS)
        extractor.assert_called_once()
        self.assertEqual(extractor.call_args.args[0], package)
        self.assertEqual(extractor.call_args.kwargs["max_zip_entry_mb"], 3)
        call = importer.return_value.import_shared_model.call_args.kwargs
        self.assertEqual(call["model_mode"], "import")
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], package)
        self.assertEqual(post.call_args.kwargs["hyper_export_format"], "csv")
        self.assertEqual(post.call_args.kwargs["hyper_export_max_rows"], 7)
        self.assertEqual(post.call_args.kwargs["max_zip_entry_mb"], 3)

    def test_batch_config_entry_overrides_global_options_before_extraction_and_export(self):
        config = os.path.join(self.root, "batch.json")
        with open(config, "w", encoding="utf-8") as stream:
            json.dump([{
                "file": self.workbook, "model_mode": "import",
                "max_zip_entry_mb": 3, "hyper_export_format": "csv",
                "hyper_export_max_rows": 7,
            }], stream)
        args = migrate._build_argument_parser().parse_args([
            "--batch-config", config, "--output-dir", self.output,
            "--mode", "auto", "--max-zip-entry-mb", "5",
            "--hyper-export-max-rows", "20", "--no-verify-open",
        ])
        with ExitStack() as stack:
            extraction = stack.enter_context(mock.patch.object(
                migrate, "run_extraction", return_value=True,
            ))
            generation = stack.enter_context(mock.patch.object(
                migrate, "run_generation", return_value=True,
            ))
            post = stack.enter_context(mock.patch.object(
                migrate, "_convert_hyper_to_csv_in_data",
            ))
            for hook in ("_stage_declared_local_sources", "_fix_twb_data_folder",
                         "_collect_local_data_files", "run_migration_report",
                         "run_batch_html_dashboard"):
                stack.enter_context(mock.patch.object(migrate, hook))
            stack.enter_context(redirect_stdout(io.StringIO()))
            self.assertEqual(migrate._run_batch_config(args), migrate.ExitCode.SUCCESS)
        self.assertEqual(extraction.call_args.kwargs["max_zip_entry_mb"], 3)
        self.assertEqual(generation.call_args.kwargs["model_mode"], "import")
        self.assertEqual(post.call_args.kwargs["hyper_export_format"], "csv")
        self.assertEqual(post.call_args.kwargs["hyper_export_max_rows"], 7)

    def test_zip_limit_and_export_cap_reject_invalid_cli_values(self):
        for flag in ("--max-zip-entry-mb", "--hyper-export-max-rows"):
            for value in ("0", "-1", "not-a-number"):
                with self.subTest(flag=flag, value=value), \
                        mock.patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as raised:
                        migrate._build_argument_parser().parse_args([
                            "--batch", self.sources, flag, value,
                        ])
                self.assertEqual(raised.exception.code, 2)

    def test_environment_caps_are_validated_cli_wins_and_resolution_does_not_mutate_env(self):
        with mock.patch.dict(os.environ, {
                "TTPBI_HYPER_EXPORT_MAX_ROWS": "9", "TTPBI_MAX_ZIP_ENTRY_MB": "5"}):
            before = dict(os.environ)
            default = migrate._resolve_hyper_options()
            explicit = migrate._resolve_hyper_options(
                hyper_export_max_rows=2, max_zip_entry_mb=3,
            )
            self.assertEqual(default["hyper_export_max_rows"], 9)
            self.assertEqual(default["max_zip_entry_mb"], 5)
            self.assertEqual(explicit["hyper_export_max_rows"], 2)
            self.assertEqual(explicit["max_zip_entry_mb"], 3)
            self.assertEqual(dict(os.environ), before)
        for value in ("", "0", "-1", "unlimited"):
            with self.subTest(value=value), mock.patch.dict(
                    os.environ, {"TTPBI_HYPER_EXPORT_MAX_ROWS": value}):
                with self.assertRaises(ConfigurationError):
                    migrate._resolve_hyper_options()
                self.assertEqual(
                    migrate._resolve_hyper_options(hyper_export_max_rows=2)[
                        "hyper_export_max_rows"],
                    2,
                )

    def test_fabric_explicit_parquet_is_rejected_before_workers_or_output_creation(self):
        with mock.patch.object(migrate, "run_extraction") as extraction, \
                mock.patch.object(migrate, "run_generation") as generation:
            with self.assertRaisesRegex(ConfigurationError, "(?i)(fabric|csv)"):
                migrate.run_batch_migration(
                    self.sources, output_dir=self.output, output_format="fabric",
                    hyper_export_format="parquet",
                )
        extraction.assert_not_called()
        generation.assert_not_called()
        self.assertFalse(os.path.exists(self.output))
