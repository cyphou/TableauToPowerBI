"""Atomic text publication must not interfere with other open files."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from powerbi_import.atomic_file import atomic_write_text


class TestAtomicWriteText(unittest.TestCase):
    def test_replace_failure_does_not_close_reused_file_descriptor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "manifest.json"
            target.write_text("previous", encoding="utf-8")
            other = root / "other.txt"
            opened = []
            original_fdopen = os.fdopen
            temporary_fd = []

            def fdopen(fd, *args, **kwargs):
                temporary_fd.append(fd)
                return original_fdopen(fd, *args, **kwargs)

            def fail_replace(source, destination):
                fd = os.open(other, os.O_CREAT | os.O_WRONLY, 0o600)
                if fd != temporary_fd[0]:
                    try:
                        os.fstat(temporary_fd[0])
                    except OSError:
                        os.dup2(fd, temporary_fd[0])
                        os.close(fd)
                        fd = temporary_fd[0]
                    else:
                        os.close(fd)
                        self.skipTest("Temporary file descriptor is already in use")
                opened.append(fd)
                raise OSError("synthetic replace failure")

            try:
                with mock.patch("powerbi_import.atomic_file.os.fdopen", side_effect=fdopen), \
                        mock.patch("powerbi_import.atomic_file.os.replace",
                                   side_effect=fail_replace), \
                        self.assertRaisesRegex(OSError, "synthetic replace failure"):
                    atomic_write_text(target, "new")
                self.assertEqual(target.read_text(encoding="utf-8"), "previous")
                self.assertEqual(os.write(opened[0], b"unrelated"), 9)
                self.assertEqual(list(root.glob(".manifest.json.*.tmp")), [])
            finally:
                for fd in opened:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

    def test_fdopen_failure_closes_raw_descriptor_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            opened = []

            def fail_fdopen(fd, *args, **kwargs):
                opened.append(fd)
                raise ValueError("synthetic fdopen failure")

            with mock.patch("powerbi_import.atomic_file.os.fdopen", side_effect=fail_fdopen), \
                    self.assertRaisesRegex(ValueError, "synthetic fdopen failure"):
                atomic_write_text(root / "manifest.json", "new")

            with self.assertRaises(OSError):
                os.fstat(opened[0])
            self.assertEqual(list(root.glob(".manifest.json.*.tmp")), [])

    def test_interruption_entering_stream_closes_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original_fdopen = os.fdopen
            streams = []

            class InterruptedStream:
                def __init__(self, stream):
                    self.stream = stream

                def __enter__(self):
                    raise KeyboardInterrupt("synthetic interrupt")

                def __exit__(self, *args):
                    self.stream.close()

                def close(self):
                    self.stream.close()

            def fdopen(*args, **kwargs):
                stream = InterruptedStream(original_fdopen(*args, **kwargs))
                streams.append(stream)
                return stream

            with mock.patch("powerbi_import.atomic_file.os.fdopen", side_effect=fdopen), \
                    self.assertRaisesRegex(KeyboardInterrupt, "synthetic interrupt"):
                atomic_write_text(root / "manifest.json", "new")

            self.assertTrue(streams[0].stream.closed)
            self.assertEqual(list(root.glob(".manifest.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
