#!/usr/bin/env python3
"""Report CLI flags that documentation promises and the CLI does not have.

This is the documentation half of the defect this repo keeps finding. A flag
in ``--help`` that nothing reads does nothing; a flag in the guide that the
parser never declares fails outright, and fails for a reason that looks
unrelated to the flag. ``docs/AGENTS.md`` claimed the preceptorship loop ran
"on ``--review``"; that flag has never existed.

Only lines that actually invoke ``migrate.py`` are judged, so pytest's
``--cov``, git's ``--oneline`` and VS Code's ``--install-extension`` are not
mistaken for our own surface.

    python scripts/check_doc_claims.py            # report
    python scripts/check_doc_claims.py --strict   # exit 1 on a new phantom
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI_SOURCE = os.path.join(_REPO_ROOT, "migrate.py")

#: Phantom flags measured when this guard was introduced. Each still needs a
#: wire or a doc fix; the set only ever shrinks.
KNOWN_PHANTOM = frozenset()

_FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]{2,})")
_DOC_ROOTS = ("docs", ".github")


def intercepted_flags(source: str) -> set:
    """Flags handled before ``parse_args`` ever sees them.

    ``--advanced-help`` is compared against ``sys.argv`` directly, so it is
    absent from the parser while being entirely real. Missing these would make
    the guard report the docs as wrong when they are right.
    """
    found = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare):
            continue
        for operand in [node.left, *node.comparators]:
            if isinstance(operand, (ast.List, ast.Tuple, ast.Set)):
                for element in operand.elts:
                    if (isinstance(element, ast.Constant)
                            and isinstance(element.value, str)
                            and element.value.startswith("--")):
                        found.add(element.value)
            elif (isinstance(operand, ast.Constant)
                    and isinstance(operand.value, str)
                    and operand.value.startswith("--")):
                found.add(operand.value)
    return found


def declared_flags() -> set:
    """Every option string the CLI genuinely accepts."""
    sys.path.insert(0, _REPO_ROOT)
    import migrate

    flags = set()
    for action in migrate._build_argument_parser()._actions:
        flags.update(o for o in action.option_strings if o.startswith("--"))

    with open(CLI_SOURCE, encoding="utf-8") as handle:
        flags |= intercepted_flags(handle.read())
    return flags


def doc_files(repo_root: str = _REPO_ROOT) -> list:
    paths = [os.path.join(repo_root, "README.md")]
    for root in _DOC_ROOTS:
        base = os.path.join(repo_root, root)
        for dirpath, _dirs, files in os.walk(base):
            paths += [os.path.join(dirpath, f) for f in files if f.endswith(".md")]
    return [p for p in paths if os.path.isfile(p)]


def find_phantoms(real: set, paths: list, repo_root: str = _REPO_ROOT) -> dict:
    """Map each undeclared flag to where a migrate.py command claims it."""
    phantom = {}
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            if "migrate.py" not in line:
                continue
            for match in _FLAG_RE.finditer(line):
                flag = match.group(1)
                if flag not in real:
                    rel = os.path.relpath(path, repo_root)
                    phantom.setdefault(flag, []).append(f"{rel}:{number}")
    return phantom


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 when a documented flag does not exist")
    args = parser.parse_args(argv)

    real = declared_flags()
    phantom = find_phantoms(real, doc_files())

    print(f"real flags    : {len(real)}")
    print(f"phantom flags : {len(phantom)}")
    new = sorted(set(phantom) - KNOWN_PHANTOM)
    if phantom:
        print()
        for flag, where in sorted(phantom.items()):
            mark = " (NEW)" if flag in new else ""
            print(f"      {flag:28} {', '.join(where[:3])}{mark}")
        print("\nEither declare the flag or correct the document. A command that"
              "\ncannot run fails for a reason unrelated to the flag.")

    return 1 if (args.strict and new) else 0


if __name__ == "__main__":
    sys.exit(main())
