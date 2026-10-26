"""Real directory and file links used to prove metadata paths are never followed.

A link planted in a project can point at a network share, and following it
contacts that host exactly like a UNC path does. The guard below records and
refuses every call that opens, resolves or lists through a link, so these
checks never reach a network.
"""

import builtins
import io
import ntpath
import os
import subprocess
from contextlib import ExitStack, contextmanager
from unittest import mock


def _normal(path):
    return os.path.normcase(os.path.abspath(os.fsdecode(path)))


def make_directory_link(testcase, link, target):
    """Create a directory symlink, else a Windows junction, else skip the test."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if _try_junction(link, target):
        return
    testcase.skipTest("directory links cannot be created in this environment")


def make_junction(testcase, link, target):
    """Create a Windows junction, or skip the test where none exists."""
    if not _try_junction(link, target):
        testcase.skipTest("directory junctions need Windows")


def _try_junction(link, target):
    if os.name != "nt":
        return False
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", link, target],
        capture_output=True, check=False)
    return completed.returncode == 0 and os.path.isdir(link)


def make_file_link(testcase, link, target):
    """Create a file symlink, or skip the test when the platform forbids it."""
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        testcase.skipTest("file symlinks cannot be created in this environment")


@contextmanager
def forbid_link_traversal(*links):
    """Record and refuse any filesystem call that follows one of ``links``.

    Inspecting a link itself with ``lstat`` stays allowed because that is how a
    link is recognised. Anything strictly below it, and every call that opens,
    resolves, lists or stats the link, would follow it.
    """
    roots = tuple(_normal(link) for link in links)
    attempts = []

    def guard(owner, name, inspect_only):
        real = getattr(owner, name)

        def guarded(*args, **kwargs):
            path = args[0] if args else next(iter(kwargs.values()), None)
            try:
                text = _normal(path)
            except (TypeError, ValueError):
                text = None
            if text is not None and any(
                    text.startswith(root + os.sep)
                    or (not inspect_only and text == root)
                    for root in roots):
                attempts.append((name, text))
                raise OSError(f"synthetic guard: {name} must not follow a link")
            return real(*args, **kwargs)
        return guarded

    following = [
        (os.path, "realpath"), (os.path, "isdir"), (os.path, "isfile"),
        (os.path, "exists"), (os.path, "getsize"), (os, "stat"),
        (os, "readlink"), (os, "scandir"), (os, "listdir"),
        (builtins, "open"), (io, "open"),
    ]
    if hasattr(ntpath, "_getfinalpathname"):
        following.append((ntpath, "_getfinalpathname"))
    inspecting = [(os, "lstat"), (os.path, "islink")]
    with ExitStack() as stack:
        for owner, name in following:
            stack.enter_context(mock.patch.object(
                owner, name, guard(owner, name, False)))
        for owner, name in inspecting:
            stack.enter_context(mock.patch.object(
                owner, name, guard(owner, name, True)))
        yield attempts
