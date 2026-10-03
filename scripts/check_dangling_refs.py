"""Count report field references that no longer resolve in the semantic model.

A dangling reference is what Power BI Desktop reports as
``Missing_References`` / "Something's wrong with one or more fields", so this
is the static proxy for that dialog.
"""
import collections
import glob
import json
import os
import re
import sys


def model_symbols(root):
    syms = set()
    for path in glob.glob(os.path.join(root, '**', 'tables', '*.tmdl'), recursive=True):
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
        match = re.search(r"^table\s+('(?:[^']|'')+'|\S+)", text, re.M)
        if not match:
            continue
        table = match.group(1).strip("'").replace("''", "'")
        for sym in re.finditer(r"^\t(?:column|measure)\s+('(?:[^']|'')+'|[^\s=]+)", text, re.M):
            syms.add((table, sym.group(1).strip("'").replace("''", "'")))
    return syms


def report_refs(root):
    refs = []

    def walk(node):
        if isinstance(node, dict):
            for key in ('Column', 'Measure', 'HierarchyLevel'):
                inner = node.get(key)
                if isinstance(inner, dict) and 'Property' in inner:
                    entity = inner.get('Expression', {}).get('SourceRef', {}).get('Entity')
                    if entity:
                        refs.append((entity, inner['Property']))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    pattern = os.path.join(root, '*.Report', 'definition', '**', '*.json')
    for path in glob.glob(pattern, recursive=True):
        try:
            with open(path, encoding='utf-8') as handle:
                walk(json.load(handle))
        except (ValueError, OSError):
            continue
    return refs


def main(base):
    total = dangling = 0
    per = collections.Counter()
    for root in sorted(glob.glob(os.path.join(base, '*'))):
        if not os.path.isdir(root):
            continue
        syms = model_symbols(root)
        if not syms:
            continue
        for entity, prop in report_refs(root):
            total += 1
            if (entity, prop) not in syms:
                dangling += 1
                per[(os.path.basename(root), entity, prop)] += 1
    print(f'{base}: {total} report field references, {dangling} dangling')
    for (report, entity, prop), count in per.most_common(12):
        print(f'    {report[:32]:32} | {entity[:34]:34} | {prop} x{count}')
    return dangling


if __name__ == '__main__':
    for arg in sys.argv[1:]:
        main(arg)
