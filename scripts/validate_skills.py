#!/usr/bin/env python3
"""Skill linter for repo-scoped Copilot skills (v44, Sprint 215.4).

Validates every ``.github/skills/**/SKILL.md``:
  * YAML-ish frontmatter present with non-empty ``name`` and ``description``
  * relative markdown links resolve to files that exist
  * every ``migrate.py`` flag referenced in the skill actually exists in the CLI
  * no obvious secrets embedded in examples

Stdlib-only. Importable (``validate_skills``) for tests; runnable as a script.

Usage:
    python scripts/validate_skills.py            # lint all skills, exit 1 on error
    python scripts/validate_skills.py --json      # machine-readable report
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKILLS_GLOB = os.path.join(_ROOT, ".github", "skills", "**", "SKILL.md")

# Patterns that look like leaked secrets in examples.
_SECRET_PATTERNS = [
    re.compile(r"--token-secret\s+[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\b(sk|pat)-[A-Za-z0-9]{16,}\b"),
    re.compile(r"password\s*[:=]\s*\S+", re.IGNORECASE),
]

# Relative link pattern: [text](rel/path) — skip http(s), anchors, mailto.
_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
# migrate.py flags referenced in skill text (e.g. --output-dir, --qa)
_FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]+)")


def _split_frontmatter(text: str):
    """Return (frontmatter_str, body) if the doc starts with a --- block."""
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end == -1:
        return "", text
    fm = text[3:end].strip()
    body = text[end + 4:]
    return fm, body


def _frontmatter_field(fm: str, field: str) -> str:
    """Extract a scalar/folded field value from simple YAML frontmatter."""
    # name: value   OR   description: >- (folded, following indented lines)
    m = re.search(rf"^{field}\s*:\s*(.*)$", fm, re.MULTILINE)
    if not m:
        return ""
    val = m.group(1).strip()
    if val in (">-", ">", "|", "|-"):
        # gather subsequent indented lines
        lines = fm[m.end():].splitlines()
        collected = []
        for ln in lines[1:] if lines and not lines[0].strip() else lines:
            if ln.strip() == "":
                if collected:
                    break
                continue
            if ln.startswith((" ", "\t")):
                collected.append(ln.strip())
            else:
                break
        return " ".join(collected).strip()
    return val.strip().strip("'\"")


def _known_cli_flags() -> set:
    """Scrape ``--flag`` tokens declared in migrate.py argparse definitions."""
    path = os.path.join(_ROOT, "migrate.py")
    flags = {"--help"}  # argparse built-in, always available
    try:
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
    except OSError:
        return flags
    # add_argument('--foo', ...) / "--foo"
    for m in re.finditer(r"""add_argument\(\s*['"](--[a-z0-9-]+)['"]""", src):
        flags.add(m.group(1))
    # also catch second alias forms: add_argument('-x', '--foo'
    for m in re.finditer(r"""['"](--[a-z][a-z0-9-]+)['"]""", src):
        flags.add(m.group(1))
    return flags


def validate_skill(path: str, known_flags=None) -> list:
    """Return a list of error strings for a single SKILL.md (empty == clean)."""
    errors = []
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    rel = os.path.relpath(path, _ROOT)

    fm, body = _split_frontmatter(text)
    if not fm:
        errors.append(f"{rel}: missing YAML frontmatter")
    else:
        if not _frontmatter_field(fm, "name"):
            errors.append(f"{rel}: frontmatter missing non-empty 'name'")
        if not _frontmatter_field(fm, "description"):
            errors.append(f"{rel}: frontmatter missing non-empty 'description'")

    base = os.path.dirname(path)
    for m in _LINK_RE.finditer(text):
        target = m.group(1).strip()
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        clean = target.split("#", 1)[0].split("?", 1)[0]
        if not clean:
            continue
        resolved = os.path.normpath(os.path.join(base, clean))
        if not os.path.exists(resolved):
            errors.append(f"{rel}: broken relative link -> {target}")

    if known_flags is None:
        known_flags = _known_cli_flags()
    if known_flags:
        for m in _FLAG_RE.finditer(body):
            flag = m.group(1)
            # ignore common non-CLI tokens
            if flag in ("--", "---"):
                continue
            if flag not in known_flags:
                errors.append(f"{rel}: references unknown migrate.py flag '{flag}'")

    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            errors.append(f"{rel}: possible embedded secret in example")

    return errors


def validate_skills(skills_glob: str = _SKILLS_GLOB) -> dict:
    """Validate all skills. Returns {'ok': bool, 'errors': [...], 'files': n}."""
    known_flags = _known_cli_flags()
    files = sorted(glob.glob(skills_glob, recursive=True))
    all_errors = []
    for path in files:
        all_errors.extend(validate_skill(path, known_flags=known_flags))
    return {"ok": not all_errors, "errors": all_errors, "files": len(files)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate repo Copilot skills")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    args = parser.parse_args(argv)
    result = validate_skills()
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["ok"]:
            print(f"OK — {result['files']} skill file(s) validated, no issues.")
        else:
            print(f"FAIL — {len(result['errors'])} issue(s) in {result['files']} file(s):")
            for e in result["errors"]:
                print(f"  - {e}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
