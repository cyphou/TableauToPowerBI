#!/usr/bin/env python3
"""Report tests that patch a name their target module only re-exports.

``patch.object(tmdl_generator, '_write_table_tmdl')`` stopped intercepting the
moment both that function and its caller moved to ``tmdl_writers``. The patched
module still carries the attribute, so the patch succeeds and changes nothing.
It failed loudly that time; in general such a test passes while proving
nothing, which is this repository's most persistent defect shape.

Patching an imported name is normal and correct when the target module *calls*
it -- ``patch(llm_client, 'urlopen')`` is how you stub a dependency. The defect
is narrower: the module imports the name, never uses it, and exists only to
re-export it. Then the real caller resolves the name somewhere else entirely.

    python scripts/check_patch_targets.py            # report
    python scripts/check_patch_targets.py --strict   # exit 1 on a NEW offender
"""

from __future__ import annotations

import argparse
import ast
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_DIRS = ("powerbi_import", "tableau_export")
TEST_DIR = "tests"

#: Offenders measured when this guard was introduced. Empty: the extraction
#: that motivated it was fixed first. The set exists so a new one fails the
#: build, not to bless any.
KNOWN_OFFENDERS = frozenset()


def _module_facts(path):
    """Return (defined, imported, used) name sets for one module."""
    with open(path, encoding="utf-8-sig") as handle:
        try:
            tree = ast.parse(handle.read())
        except SyntaxError:
            return set(), set(), set()

    defined, imported = set(), set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined.add(target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported.add(alias.asname or alias.name.split(".")[0])

    # A name is "used" if it appears anywhere outside its own import statement.
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            value = node.value
            if isinstance(value, ast.Name):
                used.add(value.id)
    return defined, imported, used


def source_modules():
    found = {}
    for directory in MODULE_DIRS:
        for root, _dirs, files in os.walk(os.path.join(_REPO_ROOT, directory)):
            if "__pycache__" in root:
                continue
            for name in files:
                if name.endswith(".py"):
                    found[name[:-3]] = _module_facts(os.path.join(root, name))
    return found


def _patch_targets(tree, modules):
    """Yield (lineno, module, attribute) for every patch of a source module."""
    alias = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    alias[a.asname] = a.name.split(".")[-1]
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                if a.name in modules:
                    alias[a.asname or a.name] = a.name

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "object"
                and getattr(func.value, "id", "") == "patch"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Name)
                and isinstance(node.args[1], ast.Constant)):
            target = alias.get(node.args[0].id)
            if target:
                yield node.lineno, target, node.args[1].value
        elif (isinstance(func, ast.Name) and func.id == "patch" and node.args
              and isinstance(node.args[0], ast.Constant)
              and isinstance(node.args[0].value, str)
              and "." in node.args[0].value):
            dotted, attr = node.args[0].value.rsplit(".", 1)
            yield node.lineno, dotted.split(".")[-1], attr


def find_offenders():
    """Patches of a name the target module imports but never uses."""
    modules = source_modules()
    offenders = []
    test_root = os.path.join(_REPO_ROOT, TEST_DIR)
    for name in sorted(os.listdir(test_root)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        path = os.path.join(test_root, name)
        with open(path, encoding="utf-8-sig") as handle:
            try:
                tree = ast.parse(handle.read())
            except SyntaxError:
                continue
        for lineno, module, attr in _patch_targets(tree, modules):
            if module not in modules:
                continue
            defined, imported, used = modules[module]
            if attr in imported and attr not in defined and attr not in used:
                offenders.append((name, lineno, module, attr))
    return offenders


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero on an offender outside the known set")
    args = parser.parse_args(argv)

    offenders = find_offenders()
    new = [o for o in offenders if f"{o[0]}:{o[3]}" not in KNOWN_OFFENDERS]

    print(f"patches of a pure re-export : {len(offenders)}")
    print(f"new                         : {len(new)}\n")
    for test, lineno, module, attr in offenders:
        marker = "NEW " if f"{test}:{attr}" not in KNOWN_OFFENDERS else "    "
        print(f"  {marker}{test}:{lineno}  patches {module}.{attr}, "
              f"which {module} only re-exports")

    if new:
        print("\nThe patched module carries the attribute, so the patch succeeds "
              "and intercepts nothing. Patch the module that calls the name.")
        if args.strict:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
