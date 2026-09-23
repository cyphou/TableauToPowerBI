#!/usr/bin/env python3
"""Report CLI flags that are declared but never consumed.

A flag in ``--help`` that nothing reads is the same defect class this codebase
keeps finding: a surface that reads as working while doing nothing. The user
passes ``--optimize-dax``, the parser accepts it, and no code path ever looks at
the value.

    python scripts/check_cli_flags.py            # report
    python scripts/check_cli_flags.py --strict   # exit 1 on a NEW inert flag
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI_SOURCE = os.path.join(_REPO_ROOT, "migrate.py")

#: Flags measured as inert when this guard was introduced. The set exists to
#: stop the defect growing, not to bless it: each entry still needs a wire or
#: remove decision, and a flag that gets wired must be dropped from here, so
#: the baseline can only ever shrink.
KNOWN_INERT = frozenset({
    "live_connection",
    "merge_preview",
    "multi_tenant",
    "no_ds_cache",
    "parallel_run",
    "prep_to_dataflow",
    "resolve_published_ds",
    "skip_conversion",
    "sync",
    "validate_data",
})


def declared_flags(source: str) -> dict:
    """Map every ``add_argument`` destination to its flag name and line."""
    declared = {}
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        dest = None
        for keyword in node.keywords:
            if keyword.arg == "dest" and isinstance(keyword.value, ast.Constant):
                dest = keyword.value.value
        options = [a.value for a in node.args
                   if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        long_options = [o for o in options if o.startswith("--")]
        if dest is None and options:
            chosen = long_options[0] if long_options else options[0]
            dest = chosen.lstrip("-").replace("-", "_")
        if dest:
            flag = long_options[0] if long_options else (options[0] if options else dest)
            # A later declaration reusing a dest (--x / --no-x) keeps the first name.
            declared.setdefault(dest, (flag, node.lineno))
    return declared


def _is_consumed(dest: str, source: str) -> bool:
    """Whether any code path reads the parsed value.

    Covers attribute access, ``getattr`` by name, and config-dictionary lookup,
    because the CLI uses all three.
    """
    patterns = (
        rf"\bargs\.{re.escape(dest)}\b",
        rf"getattr\(\s*args\s*,\s*['\"]{re.escape(dest)}['\"]",
        rf"\[['\"]{re.escape(dest)}['\"]\]",
        rf"\.get\(\s*['\"]{re.escape(dest)}['\"]",
    )
    return any(re.search(p, source) for p in patterns)


def find_inert(source: str) -> list:
    """Return ``(flag, dest, lineno)`` for every declared-but-unread flag."""
    return [(flag, dest, line)
            for dest, (flag, line) in sorted(declared_flags(source).items())
            if not _is_consumed(dest, source)]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero when a flag outside the known set is inert")
    args = parser.parse_args(argv)

    with open(CLI_SOURCE, encoding="utf-8-sig") as handle:
        source = handle.read()

    declared = declared_flags(source)
    inert = find_inert(source)
    regressions = [i for i in inert if i[1] not in KNOWN_INERT]

    print(f"declared flags : {len(declared)}")
    print(f"inert flags    : {len(inert)}")
    print(f"new inert flags: {len(regressions)}\n")
    for flag, dest, line in inert:
        marker = "NEW " if dest not in KNOWN_INERT else "    "
        print(f"  {marker}{flag:32s} dest={dest:26s} migrate.py:{line}")

    if regressions:
        print("\nA flag that nothing reads is accepted by the parser and then "
              "ignored. Wire it to its implementation or remove it.")
        if args.strict:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
