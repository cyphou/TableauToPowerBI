"""Per-operation ZIP limits, bounded Hyper archive copying and path containment."""

import io
import ntpath
import os
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from powerbi_import import security_validator as security
from powerbi_import.errors import ConfigurationError
from tests.link_test_helpers import forbid_link_traversal, make_directory_link

MIB = 1024 * 1024

# Reserved host name: the checks below never reach a network, they only prove
# that no filesystem resolution is attempted.
NON_LOCAL_WINDOWS_PATHS = (
    r"\\attacker.example\share\f.hyper",
    "//attacker.example/share/f.hyper",
    r"\\attacker.example",
    r"\/attacker.example/share/f.hyper",
    r"\\?\UNC\attacker.example\share\f.hyper",
    r"\\?\unc\attacker.example\share\f.hyper",
    r"\\.\UNC\attacker.example\share\f.hyper",
    r"\??\UNC\attacker.example\share\f.hyper",
    r"\??\C:\data\f.hyper",
    r"\\?\Volume{00000000-0000-0000-0000-000000000000}\f.hyper",
    r"\\.\pipe\synthetic",
)
LOCAL_PATHS = (
    r"C:\data\f.hyper",
    "c:/data/f.hyper",
    r"\\?\C:\data\f.hyper",
    r"\\.\C:\data\f.hyper",
    "//?/C:/data/f.hyper",
    r"relative\f.hyper",
    "/tmp/f.hyper",
    "",
    ".",
)


@contextmanager
def _forbid_filesystem_resolution():
    """Fail on any attempt to resolve a path through the filesystem."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"filesystem resolution attempted: {args!r}")

    with mock.patch.object(os.path, "realpath", side_effect=refuse), \
            mock.patch.object(ntpath, "realpath", side_effect=refuse), \
            mock.patch.object(ntpath, "_getfinalpathname",
                              side_effect=refuse, create=True), \
            mock.patch.object(os, "stat", side_effect=refuse), \
            mock.patch.object(os, "lstat", side_effect=refuse):
        yield


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


class TestNonLocalWindowsPaths(unittest.TestCase):
    """Paths read from project metadata must never be resolved remotely."""

    def test_unc_device_and_nt_object_paths_are_non_local(self):
        for path in NON_LOCAL_WINDOWS_PATHS:
            with self.subTest(path=path):
                self.assertTrue(security.is_non_local_windows_path(path))
                self.assertTrue(security.is_non_local_windows_path(os.fsencode(path)))

    def test_drive_relative_and_posix_paths_are_local(self):
        for path in LOCAL_PATHS:
            with self.subTest(path=path):
                self.assertFalse(security.is_non_local_windows_path(path))

    def test_non_local_keys_never_touch_the_filesystem(self):
        with _forbid_filesystem_resolution():
            for path in NON_LOCAL_WINDOWS_PATHS:
                with self.subTest(path=path):
                    self.assertEqual(
                        security.canonical_path_key(path),
                        ntpath.normcase(ntpath.normpath(path)),
                    )

    def test_equivalent_non_local_spellings_share_one_lexical_key(self):
        spellings = (
            r"\\Attacker.Example\Share\Data\f.hyper",
            "//attacker.example/share/Data/f.hyper",
            r"\\attacker.example\SHARE\Other\..\Data\.\f.hyper",
        )
        with _forbid_filesystem_resolution():
            keys = {security.canonical_path_key(spelling) for spelling in spellings}
        self.assertEqual(len(keys), 1, keys)

    def test_distinct_non_local_paths_keep_distinct_keys(self):
        with _forbid_filesystem_resolution():
            first = security.canonical_path_key(r"\\attacker.example\share\a.hyper")
            second = security.canonical_path_key(r"\\attacker.example\share\b.hyper")
        self.assertNotEqual(first, second)

    def test_local_paths_keep_their_resolved_identity(self):
        with tempfile.TemporaryDirectory() as root:
            direct = os.path.join(root, "f.hyper")
            with open(direct, "wb"):
                pass
            detour = os.path.join(root, "missing", "..", "f.hyper")
            expected = os.path.normcase(os.path.realpath(direct))
            self.assertEqual(security.canonical_path_key(direct), expected)
            self.assertEqual(security.canonical_path_key(detour), expected)
            self.assertEqual(
                security.canonical_path_key(os.fsencode(direct)), expected)

    def test_relative_local_paths_are_made_absolute_before_resolution(self):
        self.assertEqual(
            security.canonical_path_key("f.hyper"),
            os.path.normcase(os.path.realpath(os.path.abspath("f.hyper"))),
        )


class TestLexicalPathKey(unittest.TestCase):
    """Keys for project metadata are computed without any filesystem access."""

    def test_every_spelling_is_keyed_without_touching_the_filesystem(self):
        with _forbid_filesystem_resolution():
            for path in NON_LOCAL_WINDOWS_PATHS + LOCAL_PATHS:
                for value in (path, os.fsencode(path)):
                    with self.subTest(path=value):
                        self.assertIsInstance(security.lexical_path_key(value), str)

    def test_non_local_paths_keep_the_canonical_key(self):
        with _forbid_filesystem_resolution():
            for path in NON_LOCAL_WINDOWS_PATHS:
                with self.subTest(path=path):
                    self.assertEqual(
                        security.lexical_path_key(path),
                        security.canonical_path_key(path),
                    )

    def test_equivalent_local_spellings_collapse_to_one_absolute_key(self):
        with tempfile.TemporaryDirectory() as root:
            direct = os.path.join(root, "sub", "f.hyper")
            spellings = (
                direct,
                os.path.join(root, "sub", ".", "f.hyper"),
                os.path.join(root, "other", "..", "sub", "f.hyper"),
                os.fsencode(direct),
            )
            with _forbid_filesystem_resolution():
                keys = {security.lexical_path_key(value) for value in spellings}
        self.assertEqual(keys, {os.path.normcase(os.path.abspath(direct))})

    def test_relative_paths_are_made_absolute_without_resolution(self):
        with _forbid_filesystem_resolution():
            key = security.lexical_path_key("f.hyper")
        self.assertEqual(key, os.path.normcase(os.path.abspath("f.hyper")))

    @unittest.skipUnless(os.name == "nt", "case folding is a Windows rule")
    def test_windows_case_and_separator_spellings_share_one_key(self):
        spellings = (r"C:\Data\F.hyper", "c:/data/f.HYPER", r"c:\data\.\f.hyper")
        with _forbid_filesystem_resolution():
            keys = {security.lexical_path_key(value) for value in spellings}
        self.assertEqual(len(keys), 1, keys)

    def test_a_link_is_never_followed_and_keeps_its_own_key(self):
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "target")
            os.mkdir(target)
            alias = os.path.join(root, "alias")
            make_directory_link(self, alias, target)
            through_alias = os.path.join(alias, "f.hyper")
            with forbid_link_traversal(alias) as attempts:
                key = security.lexical_path_key(through_alias)
            canonical = security.canonical_path_key(through_alias)
        self.assertEqual(attempts, [])
        self.assertEqual(key, os.path.normcase(os.path.abspath(through_alias)))
        self.assertNotEqual(key, canonical)
