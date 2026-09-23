#!/usr/bin/env python3
"""Report advisory code that can reach the artifact it is only meant to judge.

Migration output has to be reproducible: the same workbook must produce the
same project, or the golden fixtures, the parity registry and the regression
suite all stop meaning anything. Advisory code -- LLM refinement, remediation
suggestions, conversational Q&A, marketplace recipes, plugins -- is allowed to
be non-deterministic precisely because it only ever reads evidence and writes
its own report.

That split is currently intact and entirely undocumented, which makes it one
edit away from being lost. Writing refined DAX straight into the model would
look like an improvement and would quietly break reproducibility.

Two rules, both checkable without running anything:

1. Advisory modules must not import an artifact generator.
2. Advisory modules must not name a migration artifact extension.

    python scripts/check_advisory_boundary.py            # report
    python scripts/check_advisory_boundary.py --strict   # exit 1 on a breach
"""

from __future__ import annotations

import argparse
import ast
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_DIR = os.path.join(_REPO_ROOT, "powerbi_import")

#: Modules whose output is advisory: read evidence, emit an opinion.
ADVISORY_MODULES = frozenset({
    "llm_client",
    "llm_gateway",
    "conversational",
    "remediation",
    "marketplace",
    "dax_recipes",
    "model_templates",
    "plugins",
    "plugin_sdk",
})

#: Modules that write the artifacts the migration ships.
ARTIFACT_GENERATORS = frozenset({
    "tmdl_generator",
    "tmdl_m_conversion",
    "tmdl_self_heal",
    "pbip_generator",
    "visual_generator",
    "thin_report_generator",
    "lakehouse_generator",
    "dataflow_generator",
    "notebook_generator",
    "pipeline_generator",
    "fabric_semantic_model_generator",
    "fabric_project_generator",
    "autoheal",
    "self_healing_v3",
})

#: Extensions that belong to the shipped project, not to a report.
ARTIFACT_SUFFIXES = (".tmdl", ".pbir", ".pbip", ".bim", ".pq")


def _module_names(node: ast.AST) -> set:
    """Every module name a single import statement pulls in."""
    names = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            names.update(alias.name.split("."))
    elif isinstance(node, ast.ImportFrom):
        names.update((node.module or "").split("."))
        names.update(alias.name for alias in node.names)
    return names


def breaches(source: str) -> list:
    """Return ``(rule, detail, line)`` for everything that crosses the line."""
    found = []
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for hit in sorted(_module_names(node) & ARTIFACT_GENERATORS):
                found.append(("imports-generator", hit, node.lineno))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.lower()
            for suffix in ARTIFACT_SUFFIXES:
                # A bare extension is a mention; a path is an intent to touch.
                if text.endswith(suffix) and len(text) > len(suffix):
                    found.append(("names-artifact", node.value, node.lineno))
                    break
    return found


def scan(package_dir: str = PACKAGE_DIR) -> dict:
    """Map each advisory module to its breaches."""
    report = {}
    for name in sorted(ADVISORY_MODULES):
        path = os.path.join(package_dir, f"{name}.py")
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            report[name] = breaches(handle.read())
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 if any advisory module can reach an artifact")
    args = parser.parse_args(argv)

    report = scan()
    total = sum(len(v) for v in report.values())

    print(f"advisory modules : {len(report)}")
    print(f"boundary breaches: {total}")
    if total:
        print()
        for name, items in report.items():
            for rule, detail, line in items:
                print(f"      {name}.py:{line}  {rule}  {detail}")
        print("\nAdvisory code may propose, never apply. Emit a suggestion and let"
              "\nthe deterministic pipeline decide whether to act on it.")

    return 1 if (args.strict and total) else 0


if __name__ == "__main__":
    sys.exit(main())
