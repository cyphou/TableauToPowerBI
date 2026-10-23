"""Per-operation ZIP limits and bounded Hyper archive copying."""

import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from powerbi_import import security_validator as security
from powerbi_import.errors import ConfigurationError

MIB = 1024 * 1024


class _BoundedReader(io.BytesIO):
    def __init__(self, payload):
        super().__init__(payload)
        self.requests = []

    def read(self, size=-1):
        if not 0 < size <= MIB:
            raise AssertionError(f"unbounded ZIP read: {size}")
        self.requests.append(size)
        return super().read(size)


class _Archive:
    """Declared and observed sizes may differ, as in a dishonest ZIP header."""

    def __init__(self, payload=b"synthetic", declared_size=None):
        self.reader = _BoundedReader(payload)
        self.declared_size = len(payload) if declared_size is None else declared_size
        self.open = mock.Mock(return_value=self.reader)

    def getinfo(self, member):
        return SimpleNamespace(filename=member, file_size=self.declared_size)


class TestZipEntryLimit(unittest.TestCase):
    def test_default_is_1024_mib_without_environment(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(security.get_zip_entry_limit_mb(), 1024)

    def test_explicit_positive_limits_do_not_mutate_shared_default(self):
        before = security.MAX_ZIP_ENTRY_SIZE_MB
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(security.get_zip_entry_limit_mb(7), 7)
            self.assertEqual(security.get_zip_entry_limit_mb("2048"), 2048)
            self.assertEqual(security.get_zip_entry_limit_mb(), 1024)
        self.assertEqual(security.MAX_ZIP_ENTRY_SIZE_MB, before)

    def test_invalid_explicit_limit_fails_instead_of_becoming_unlimited(self):
        for value in (0, -1, "0", "-2", "not-a-limit", True, 1.5):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                security.get_zip_entry_limit_mb(value)

    def test_environment_limit_is_validated_without_mutating_default(self):
        before = security.MAX_ZIP_ENTRY_SIZE_MB
        with mock.patch.dict(os.environ, {"TTPBI_MAX_ZIP_ENTRY_MB": "17"}):
            self.assertEqual(security.get_zip_entry_limit_mb(), 17)
            self.assertEqual(security.get_zip_entry_limit_mb(3), 3)
        self.assertEqual(security.MAX_ZIP_ENTRY_SIZE_MB, before)

    def test_invalid_environment_is_explicit_but_cli_override_still_wins(self):
        for value in ("", "0", "-1", "nan", "unlimited"):
            with self.subTest(value=value), mock.patch.dict(
                    os.environ, {"TTPBI_MAX_ZIP_ENTRY_MB": value}):
                with self.assertRaisesRegex(ConfigurationError, "TTPBI_MAX_ZIP_ENTRY_MB"):
                    security.get_zip_entry_limit_mb()
                self.assertEqual(security.get_zip_entry_limit_mb(2), 2)


class TestSafeZipCopyMember(unittest.TestCase):
    def test_streams_in_at_most_one_mib_reads_and_closes_member(self):
        payload = b"x" * (2 * MIB + 13)
        archive = _Archive(payload)
        output = io.BytesIO()
        copied = security.safe_zip_copy_member(
            archive, "Data/synthetic.hyper", output, max_size_mb=3,
        )
        self.assertEqual(copied, len(payload))
        self.assertEqual(output.getvalue(), payload)
        self.assertGreater(len(archive.reader.requests), 2)
        self.assertLessEqual(max(archive.reader.requests), MIB)
        self.assertTrue(archive.reader.closed)
        self.assertFalse(output.closed, "The caller owns its output stream")

    def test_declared_oversize_is_rejected_before_open_or_write(self):
        archive = _Archive(declared_size=MIB + 1)
        output = io.BytesIO()
        with self.assertRaisesRegex(ValueError, "(?i)(size|limit)"):
            security.safe_zip_copy_member(
                archive, "Data/synthetic.hyper", output, max_size_mb=1,
            )
        archive.open.assert_not_called()
        self.assertEqual(output.getvalue(), b"")

    def test_observed_oversize_cannot_bypass_declared_size_guard(self):
        archive = _Archive(b"x" * (MIB + 1), declared_size=1)
        output = io.BytesIO()
        with self.assertRaisesRegex(ValueError, "(?i)(size|limit)"):
            security.safe_zip_copy_member(
                archive, "Data/synthetic.hyper", output, max_size_mb=1,
            )
        self.assertLessEqual(len(output.getvalue()), MIB)
        self.assertTrue(archive.reader.closed)

    def test_exact_limit_is_accepted(self):
        archive = _Archive(b"x" * MIB)
        output = io.BytesIO()
        security.safe_zip_copy_member(
            archive, "Data/synthetic.hyper", output, max_size_mb=1,
        )
        self.assertEqual(len(output.getvalue()), MIB)

    def test_unsafe_paths_are_rejected_before_member_is_opened(self):
        paths = (
            "../outside.hyper", r"Data\..\outside.hyper",
            "/outside.hyper", r"C:\outside.hyper", r"\\server\share\outside.hyper",
        )
        with tempfile.TemporaryDirectory() as root:
            for member in paths:
                archive = _Archive()
                with self.subTest(member=member), self.assertRaises(security.SecurityError):
                    security.safe_zip_copy_member(
                        archive, member, io.BytesIO(), target_dir=root, max_size_mb=1,
                    )
                archive.open.assert_not_called()

    def test_failed_output_write_still_closes_member(self):
        archive = _Archive()
        output = mock.Mock()
        failure = OSError("synthetic disk failure")
        output.write.side_effect = failure
        with self.assertRaises(OSError) as raised:
            security.safe_zip_copy_member(
                archive, "Data/synthetic.hyper", output, max_size_mb=1,
            )
        self.assertIs(raised.exception, failure)
        self.assertTrue(archive.reader.closed)

    def test_short_output_write_is_an_error_not_a_successful_copy(self):
        archive = _Archive()
        output = mock.Mock()
        output.write.return_value = 1
        with self.assertRaisesRegex(OSError, "(?i)short write"):
            security.safe_zip_copy_member(
                archive, "Data/synthetic.hyper", output, max_size_mb=1,
            )
        self.assertTrue(archive.reader.closed)

    def test_bytes_compatibility_helper_obeys_optional_limit_and_bounded_reads(self):
        archive = _Archive(b"x" * (MIB + 1))
        self.assertEqual(
            security.safe_zip_extract_member(
                archive, "Data/synthetic.hyper", max_size_mb=2,
            ),
            b"x" * (MIB + 1),
        )
        self.assertTrue(archive.reader.closed)
        self.assertLessEqual(max(archive.reader.requests), MIB)
        with self.assertRaises(ValueError):
            security.safe_zip_extract_member(
                _Archive(declared_size=MIB + 1), "Data/synthetic.hyper", max_size_mb=1,
            )
