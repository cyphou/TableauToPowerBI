#!/usr/bin/env python3
"""Report source modules no production entry point can reach.

Same defect class as an inert CLI flag: a module reads as working because a
test imports it, while nothing a user can run ever executes a line of it.
``check_cli_flags.py`` ratchets the flag half of that surface; this ratchets
the module half, which until now was measured once in prose and could drift
silently between cycles.

Reachability is computed from ``migrate.py`` plus the entry points declared
below, with full dotted-path resolution and the rule that importing a submodule
also executes its package ``__init__`` -- that rule alone is why
``deploy.utils`` is reachable, so omitting it overstates the problem.

    python scripts/check_reachability.py            # report
    python scripts/check_reachability.py --strict   # exit 1 on a NEW unreachable module
"""

from __future__ import annotations

import argparse
import ast
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MODULE_DIRS = ("powerbi_import", "tableau_export")
CLI_ENTRY = "migrate.py"

#: Modules that are genuine entry points without being CLI-reachable. Each one
#: is invoked by something outside ``migrate.py``, so it is declared rather than
#: wired. The value names that caller, and ``tests/test_reachability.py``
#: asserts the named file really references the module -- a declaration nobody
#: checks is the same rubber stamp as a flag nobody reads.
DECLARED_ENTRY_POINTS = {
    "powerbi_import.api_server": "Dockerfile",
    "powerbi_import.mcp_server": "powerbi_import/mcp_server.py",
    "powerbi_import.notebook_api": "powerbi_import/notebook_api.py",
    "powerbi_import.plugin_sdk": "examples/plugins",
    "powerbi_import.plugins": "powerbi_import/plugins.py",
    "powerbi_import.visual_size_diff": "scripts/compare_visual_sizes.py",
}

#: Modules measured as unreachable when this guard was introduced. The set
#: exists to stop the defect growing, not to bless it: each entry still needs a
#: wire, declare or retire decision, and one that gets wired must be dropped
#: from here, so the baseline can only ever shrink.
KNOWN_UNREACHABLE = frozenset({
    "powerbi_import.alerts_generator",
    "powerbi_import.conversational",
    "powerbi_import.connection_rewriter",
    "powerbi_import.dax_query_generator",
    "powerbi_import.dax_recipes",
    "powerbi_import.gateway_config",
    "powerbi_import.geo_passthrough",
    "powerbi_import.healing",
    "powerbi_import.marketplace",
    "powerbi_import.model_templates",
    "powerbi_import.regression_suite",
    "powerbi_import.remediation",
    "powerbi_import.subscription_migrator",
    "powerbi_import.visual_diff",
})


def _module_name(path: str) -> str:
    rel = os.path.relpath(path, _REPO_ROOT).replace("\\", "/")
    rel = rel[: -len(".py")]
    if rel.endswith("/__init__"):
        rel = rel[: -len("/__init__")]
    return rel.replace("/", ".")


def source_modules() -> dict:
    """Map dotted module name to path for every source module."""
    found = {}
    for directory in MODULE_DIRS:
        for root, _, files in os.walk(os.path.join(_REPO_ROOT, directory)):
            if "__pycache__" in root:
                continue
            for name in files:
                if name.endswith(".py"):
                    path = os.path.join(root, name)
                    found[_module_name(path)] = path
    return found


def _imported_names(path: str, package: str) -> set:
    """Every dotted path an import statement in ``path`` could refer to."""
    with open(path, encoding="utf-8-sig", errors="replace") as handle:
        try:
            tree = ast.parse(handle.read())
        except SyntaxError:
            return set()

    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `from . import x` / `from .m import y` inside a package.
                parts = package.split(".") if package else []
                base = ".".join(parts[: len(parts) - node.level + 1])
                prefix = f"{base}.{node.module}" if node.module else base
            else:
                prefix = node.module or ""
            if prefix:
                names.add(prefix)
                # `from a.b import c` may import a module or a plain name.
                names.update(f"{prefix}.{a.name}" for a in node.names)
    return names


def _with_packages(module: str) -> set:
    """A module plus every package `__init__` executed on the way to it."""
    reached = {module}
    parts = module.split(".")
    for depth in range(1, len(parts)):
        reached.add(".".join(parts[:depth]))
    return reached


def _candidates(name: str, package: str = "") -> set:
    """Every module this import could bind to.

    The CLI inserts the package directories on ``sys.path`` and then imports
    bare names (``from governance import ...``), and ``deploy/`` does the same
    with its own directory (``from config.settings import ...``). A
    dotted-path-only resolver reports both as unreachable.
    """
    found = set(_with_packages(name))
    for directory in MODULE_DIRS:
        found |= _with_packages(f"{directory}.{name}")
    if package:
        found |= _with_packages(f"{package}.{name}")
    return found


def reachable_modules(modules: dict, roots) -> set:
    """Breadth-first closure of imports from ``roots``."""
    seen = set()
    queue = list(roots)
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        path = modules.get(current)
        if path is None:
            continue
        # A package's `__init__` is its own package; a plain module's is its parent.
        if os.path.basename(path) == "__init__.py":
            package = current
        else:
            package = current.rsplit(".", 1)[0] if "." in current else current
        for name in _imported_names(path, package):
            for candidate in _candidates(name, package):
                if candidate in modules and candidate not in seen:
                    queue.append(candidate)
    return seen


def analyse() -> tuple:
    modules = source_modules()

    cli_path = os.path.join(_REPO_ROOT, CLI_ENTRY)
    roots = set()
    for name in _imported_names(cli_path, ""):
        roots.update(c for c in _candidates(name) if c in modules)
    roots.update(name for name in DECLARED_ENTRY_POINTS if name in modules)

    reached = reachable_modules(modules, roots)
    unreachable = sorted(m for m in modules if m not in reached)
    return modules, reached, unreachable


def _line_count(path: str) -> int:
    with open(path, encoding="utf-8-sig", errors="replace") as handle:
        return sum(1 for _ in handle)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero when a module outside the known set is unreachable")
    args = parser.parse_args(argv)

    modules, reached, unreachable = analyse()
    regressions = [m for m in unreachable if m not in KNOWN_UNREACHABLE]
    dead_lines = sum(_line_count(modules[m]) for m in unreachable)

    print(f"source modules      : {len(modules)}")
    print(f"reachable           : {len(reached)}")
    print(f"unreachable         : {len(unreachable)} ({dead_lines} lines)")
    print(f"new unreachable     : {len(regressions)}\n")
    for module in unreachable:
        marker = "NEW " if module not in KNOWN_UNREACHABLE else "    "
        print(f"  {marker}{module:48s} {_line_count(modules[module]):5d} lines")

    if regressions:
        print("\nA module no entry point reaches is shipped and untouchable. "
              "Wire it to a command, declare its external caller, or remove it.")
        if args.strict:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
