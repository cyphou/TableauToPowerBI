"""Deterministic streaming progress regressions, with no clock sleeps."""

import io
import os
import tempfile
import unittest

from powerbi_import.progress import StreamingProgress


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class RecordingStream(io.StringIO):
    def __init__(self, clock, tty=False):
        super().__init__()
        self.clock = clock
        self.tty = tty
        self.records = []
        self.flush_count = 0

    def isatty(self):
        return self.tty

    def write(self, value):
        self.records.append((self.clock(), value))
        return super().write(value)

    def flush(self):
        self.flush_count += 1
        return super().flush()


class TestStreamingProgress(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "synthetic.csv")
        with open(self.path, "wb") as stream:
            stream.write(b"id\n1\n2\n")

    def _progress(self, tty=False, **kwargs):
        stream = RecordingStream(self.clock, tty=tty)
        progress = StreamingProgress(stream=stream, clock=self.clock, **kwargs)
        return progress, stream

    def test_non_tty_emits_newline_records_and_flushes_every_record(self):
        progress, stream = self._progress()
        progress.start("Synthetic")
        progress.set_total(2)
        self.clock.advance(2)
        progress.update(1)
        self.clock.advance(2)
        progress.finish(2, output_path=self.path)
        self.assertEqual(len(stream.records), 3)
        self.assertTrue(all(value.endswith("\n") for unused, value in stream.records))
        self.assertNotIn("\r", stream.getvalue())
        self.assertEqual(stream.flush_count, len(stream.records))
        self.assertIn("1/2 rows", stream.getvalue())
        self.assertIn(f"{os.path.getsize(self.path)} bytes", stream.getvalue())
        self.assertIn("complete", stream.getvalue().lower())

    def test_tty_has_ascii_bar_and_carriage_return_with_final_newline(self):
        progress, stream = self._progress(tty=True)
        progress.start("Synthetic", total_rows=2)
        self.clock.advance(2)
        progress.update(1)
        self.clock.advance(1)
        progress.finish(2, output_path=self.path)
        text = stream.getvalue()
        self.assertTrue(text.isascii())
        self.assertIn("\r", text)
        self.assertRegex(text, r"\[[#=>.\- ]+\]")
        self.assertIn("50%", text)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(stream.flush_count, len(stream.records))

    def test_monotonic_clock_throttles_intermediates_to_once_per_two_seconds(self):
        progress, stream = self._progress()
        progress.start("Synthetic", total_rows=100)
        for seconds, rows in ((0.25, 1), (1.65, 2), (0.1, 3),
                              (0.25, 4), (1.75, 5)):
            self.clock.advance(seconds)
            progress.update(rows)
        progress.finish(5, output_path=self.path, sampled=True)
        updates = [
            (when, value) for when, value in stream.records
            if "progress:" in value.lower()
        ]
        self.assertEqual([when for when, unused in updates], [2.0, 4.0])
        self.assertEqual(len(stream.records), 4, "start/final bypass the intermediate throttle")
        self.assertIn("sampled/capped", stream.records[-1][1])
        self.assertNotIn("100%", stream.getvalue())

    def test_rate_and_eta_use_actual_written_rows_even_when_clock_starts_at_zero(self):
        progress, stream = self._progress()
        progress.start("Synthetic", total_rows=100)
        self.clock.advance(2)
        progress.update(10)
        last = stream.records[-1][1]
        self.assertIn("10/100 rows", last)
        self.assertIn("5.0 rows/s", last)
        self.assertIn("ETA 18s", last)
        self.assertEqual(progress.rows_written, 10)

    def test_unknown_count_never_invents_percent_eta_or_source_total(self):
        progress, stream = self._progress()
        progress.start("Synthetic")
        progress.set_total(None)
        self.clock.advance(2)
        progress.update(2)
        self.clock.advance(1)
        progress.finish(2, output_path=self.path)
        text = stream.getvalue()
        self.assertNotIn("%", text)
        self.assertNotIn("ETA", text)
        self.assertNotIn("source rows", text)
        self.assertIn("2 rows", text)
        self.assertIsNone(progress.total_rows)

    def test_empty_and_failed_exports_do_not_report_false_100_percent(self):
        progress, stream = self._progress()
        progress.start("Empty", total_rows=0)
        progress.finish(0, output_path=self.path)
        self.assertNotIn("100%", stream.getvalue())

        progress, stream = self._progress()
        progress.start("Failure", total_rows=2)
        self.clock.advance(2)
        progress.update(2)
        progress.fail("synthetic write error", rows_written=2)
        self.assertNotIn("100%", stream.getvalue())
        self.assertNotIn("complete", stream.getvalue().lower())
        self.assertIn("failed", stream.records[-1][1].lower())
        self.assertIn("synthetic write error", stream.records[-1][1])

    def test_quiet_mode_has_no_output_or_flushes(self):
        progress, stream = self._progress(quiet=True)
        progress.start("Synthetic", total_rows=2)
        self.clock.advance(2)
        progress.update(1)
        progress.finish(2, output_path=self.path)
        self.assertEqual(stream.getvalue(), "")
        self.assertEqual(stream.flush_count, 0)
