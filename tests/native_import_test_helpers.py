"""Scoped I/O guards for synthetic native-Import migration regressions."""

import builtins
import io
import os
import shutil
import zipfile
from contextlib import ExitStack, contextmanager
from unittest import mock


@contextmanager
def forbid_native_payload_io(paths=(), archive_members=()):
    """Allow metadata/artifact I/O, but detect native dataset body reads/copies.

    Use around generation/staging only, not Tableau metadata extraction. Paths
    and archive members are exact synthetic fixtures, never extension guesses.
    """
    blocked_paths = {
        os.path.normcase(os.path.abspath(os.fspath(path))) for path in paths
    }
    blocked_members = set(archive_members)
    attempts = []

    def check_path(path, operation):
        try:
            key = os.path.normcase(os.path.abspath(os.fspath(path)))
        except (TypeError, ValueError):
            return
        if key in blocked_paths:
            attempts.append((operation, os.fspath(path)))
            raise AssertionError(f"Native dataset {operation} must wait until refresh: {path}")

    def guarded_open(opener):
        def checked(path, *args, **kwargs):
            check_path(path, "open")
            return opener(path, *args, **kwargs)
        return checked

    def guarded_copy(copier):
        def checked(source, destination, *args, **kwargs):
            check_path(source, "copy")
            return copier(source, destination, *args, **kwargs)
        return checked

    archive_open = zipfile.ZipFile.open

    def checked_archive_open(archive, member, mode="r", *args, **kwargs):
        name = member.filename if isinstance(member, zipfile.ZipInfo) else member
        if mode == "r" and name in blocked_members:
            attempts.append(("archive read", name))
            raise AssertionError(f"Native archive dataset must not be extracted: {name}")
        return archive_open(archive, member, mode, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(mock.patch("builtins.open", guarded_open(builtins.open)))
        stack.enter_context(mock.patch("io.open", guarded_open(io.open)))
        stack.enter_context(mock.patch("shutil.copy2", guarded_copy(shutil.copy2)))
        stack.enter_context(mock.patch("shutil.copyfile", guarded_copy(shutil.copyfile)))
        stack.enter_context(mock.patch.object(zipfile.ZipFile, "open", checked_archive_open))
        yield attempts
    if attempts:
        raise AssertionError(f"Native payload I/O was attempted, even if swallowed: {attempts}")
