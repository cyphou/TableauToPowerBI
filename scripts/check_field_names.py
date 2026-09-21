#!/usr/bin/env python3
"""Report reads of extracted fields the extractor never writes.

The recurring defect in this codebase is a consumer addressing an extracted
object by a key the extractor does not emit: the inventory looked for
`worksheet` on actions while the extractor writes `source_worksheets`, and 216
healthy objects were reported as orphans. Nothing failed, so nothing was noticed.

This walks the consumer modules, tracks which variables hold extracted objects
of which type, collects the keys read from them, and compares that against the
vocabulary a real extraction actually produces.

    python scripts/check_field_names.py            # report
    python scripts/check_field_names.py --strict   # exit 1 on any unknown key
"""

from __future__ import annotations

import argparse
import ast
import glob
import json
import os
import shutil
import sys
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, 'tableau_export'))

#: Genuine Tableau files: hand-written samples do not always reproduce the schema.
VOCABULARY_WORKBOOKS = (
    os.path.join('examples', 'real_world', 'superstore_sales_dashboard.twbx'),
    os.path.join('examples', 'real_world', 'World Indicators.twbx'),
    os.path.join('examples', 'real_world', 'Salesforce.twbx'),
    os.path.join('examples', 'tableau_samples', 'Complex_Enterprise.twb'),
    os.path.join('examples', 'tableau_samples', 'Ventes_France.twb'),
)

#: Keyed by field name rather than holding records, so "keys" are data.
NOT_RECORD_SHAPED = {'aliases', 'linguistic_schema'}

#: Consumer fallbacks accepted for callers supplying enriched objects directly.
#: Production extraction emits the corresponding dashboard/color-encoding data
#: through different keys; keep these exceptions narrow and typed.
EXTERNAL_CONSUMER_FIELDS = frozenset({
    ('worksheets', 'conditionalFormatting'),
    ('worksheets', 'dynamic_visibility'),
    ('worksheets', 'zone_visibility'),
})


def build_vocabulary() -> dict[str, set[str]]:
    """Field names a real extraction emits, per object type."""
    from extract_tableau_data import TableauExtractor

    vocab: dict[str, set[str]] = {}
    for relative in VOCABULARY_WORKBOOKS:
        workbook = os.path.join(_REPO_ROOT, relative)
        if not os.path.isfile(workbook):
            continue
        out = tempfile.mkdtemp()
        try:
            TableauExtractor(workbook, output_dir=out).extract_all()
            for filename in os.listdir(out):
                if not filename.endswith('.json'):
                    continue
                kind = filename[:-len('.json')]
                if kind in NOT_RECORD_SHAPED:
                    continue
                try:
                    with open(os.path.join(out, filename), encoding='utf-8') as fh:
                        payload = json.load(fh)
                except (OSError, json.JSONDecodeError):
                    continue
                items = payload if isinstance(payload, list) else [payload]
                keys = vocab.setdefault(kind, set())
                for item in items:
                    if isinstance(item, dict):
                        keys.update(item.keys())
        finally:
            shutil.rmtree(out, ignore_errors=True)
    return vocab


def declared_keys() -> set[str]:
    """Every key the extractor assigns anywhere, regardless of the corpus.

    A vocabulary built by running the extractor proves a key is emitted; it can
    never prove one is not. `hyper_files['tables']` is set on a branch our sample
    workbooks do not take, so observation alone reports it as missing. Reading
    the assignment sites closes that gap.
    """
    keys: set[str] = set()
    for path in glob.glob(os.path.join(_REPO_ROOT, 'tableau_export', '*.py')):
        try:
            tree = ast.parse(open(path, encoding='utf-8').read())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            # entry['tables'] = ...
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript):
                        key = _constant_str(target.slice)
                        if key:
                            keys.add(key)
            # {'tables': ...} and dict(tables=...)
            elif isinstance(node, ast.Dict):
                for k in node.keys:
                    key = _constant_str(k)
                    if key:
                        keys.add(key)
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg:
                        keys.add(kw.arg)
    return keys


def _constant_str(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _extracted_type(node) -> str | None:
    """If the expression reads extracted['x'] or extracted.get('x'), return x."""
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        if node.value.id in ('extracted', 'extraction', 'data'):
            return _constant_str(node.slice)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'get'
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in ('extracted', 'extraction', 'data')
            and node.args):
        return _constant_str(node.args[0])
    return None


class _Reads(ast.NodeVisitor):
    """Group the keys read from extracted collections by logical value.

    A key is only suspicious when it is the *sole* way a value is read. Consumers
    deliberately chain alternatives — ``ws.get("chart_type") or ws.get("mark_type")``
    and ``uf.get("table", uf.get("datasource", "default"))`` — and flagging the
    unused branch of a working fallback is noise, so alternatives are collected
    into one group and reported only if none of them is ever emitted.
    """

    def __init__(self):
        self.collections: dict[str, str] = {}   # var holding a list of records
        self.records: dict[str, str] = {}       # var holding one record
        self.groups: list[tuple[str, frozenset, int]] = []
        self._claimed: set[int] = set()         # id() of nodes already grouped

    # -- binding ------------------------------------------------------
    def visit_Assign(self, node):
        kind = _extracted_type(node.value)
        if kind:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.collections[target.id] = kind
        elif isinstance(node.value, ast.Name) and node.value.id in self.collections:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.collections[target.id] = self.collections[node.value.id]
        self.generic_visit(node)

    def _bind_loop(self, node):
        kind = _extracted_type(node.iter)
        if kind is None and isinstance(node.iter, ast.Name):
            kind = self.collections.get(node.iter.id)
        if kind and isinstance(node.target, ast.Name):
            self.records[node.target.id] = kind

    def visit_For(self, node):
        self._bind_loop(node)
        self.generic_visit(node)

    def visit_comprehension(self, node):
        self._bind_loop(node)
        self.generic_visit(node)

    # -- reading ------------------------------------------------------
    def _record_of(self, node) -> str | None:
        return self.records.get(node.id) if isinstance(node, ast.Name) else None

    def _read(self, node) -> tuple[str, str] | None:
        """(object_type, key) if this node reads a key off an extracted record."""
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'get' and node.args):
            kind = self._record_of(node.func.value)
            key = _constant_str(node.args[0])
            if kind and key:
                return kind, key
        if isinstance(node, ast.Subscript):
            kind = self._record_of(node.value)
            key = _constant_str(node.slice)
            if kind and key:
                return kind, key
        return None

    def _alternatives(self, node, kind, keys):
        """Walk a fallback chain, collecting every key it can read."""
        read = self._read(node)
        if read and read[0] == kind:
            keys.add(read[1])
            self._claimed.add(id(node))
            if isinstance(node, ast.Call) and len(node.args) > 1:
                self._alternatives(node.args[1], kind, keys)
            return
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            for value in node.values:
                self._alternatives(value, kind, keys)

    def _open_group(self, node):
        read = self._read(node)
        if not read or id(node) in self._claimed:
            return
        kind, _key = read
        keys: set[str] = set()
        self._alternatives(node, kind, keys)
        if keys:
            self.groups.append((kind, frozenset(keys), node.lineno))

    def visit_BoolOp(self, node):
        if isinstance(node.op, ast.Or):
            for value in node.values:
                read = self._read(value)
                if read and id(value) not in self._claimed:
                    kind = read[0]
                    keys: set[str] = set()
                    for sibling in node.values:
                        self._alternatives(sibling, kind, keys)
                    if keys:
                        self.groups.append((kind, frozenset(keys), node.lineno))
                    break
        self.generic_visit(node)

    def visit_Call(self, node):
        self._open_group(node)
        self.generic_visit(node)

    def visit_Subscript(self, node):
        self._open_group(node)
        self.generic_visit(node)

    # -- scoping ------------------------------------------------------
    def _visit_scope(self, node):
        """Bindings do not cross function boundaries.

        A module-wide pass lets a `worksheets` bound in one function label an
        unrelated `worksheets` in the next, which is how this reported comparison
        rows as extracted records.
        """
        outer = (self.collections, self.records)
        self.collections, self.records = {}, {}
        try:
            self.generic_visit(node)
        finally:
            self.collections, self.records = outer

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope


def scan_module(path: str) -> list[tuple[str, frozenset, int]]:
    try:
        tree = ast.parse(open(path, encoding='utf-8').read())
    except (OSError, SyntaxError):
        return []
    visitor = _Reads()
    visitor.visit(tree)
    return visitor.groups


def find_unknown(vocab: dict[str, set[str]],
                 declared: set[str] | None = None,
                 external: frozenset[tuple[str, str]] = EXTERNAL_CONSUMER_FIELDS,
                 ) -> list[tuple[str, str, frozenset, int]]:
    """Return (module, object_type, keys, lineno) for values with no known source.

    A key is known if the corpus emitted it for that type, or if the extractor
    assigns it anywhere. Keys prefixed with `_` are excluded: that is the
    convention for fields added downstream, by the merge engine and friends.
    """
    declared = declared or set()
    unknown = []
    for path in sorted(glob.glob(os.path.join(_REPO_ROOT, 'powerbi_import', '*.py'))):
        module = os.path.basename(path)
        for kind, keys, lineno in sorted(scan_module(path),
                                         key=lambda g: (g[2], sorted(g[1]))):
            known = vocab.get(kind)
            if known is None:
                continue  # the corpus produced none of this type: cannot judge
            if any(k.startswith('_') for k in keys):
                continue  # enriched downstream, not an extraction field
            if (keys & known or keys & declared
                    or any((kind, key) in external for key in keys)):
                continue
            unknown.append((module, kind, keys, lineno))
    return unknown


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strict', action='store_true',
                        help='Exit non-zero when a value has no known source')
    args = parser.parse_args(argv)

    vocab = build_vocabulary()
    if not vocab:
        print('No sample workbooks available; nothing to check.')
        return 0

    declared = declared_keys()
    unknown = find_unknown(vocab, declared)
    print(f'object types observed      : {len(vocab)}')
    print(f'keys declared by extractor : {len(declared)}')
    print(f'values with no known source: {len(unknown)}\n')
    for module, kind, keys, lineno in unknown:
        names = ' / '.join(sorted(f"'{k}'" for k in keys))
        print(f"  {module}:{lineno}  {kind}[{names}]")

    if unknown:
        print('\nEach of these reads a field the extractor neither emitted on the '
              'sample corpus nor assigns anywhere. Verify before deleting: a key '
              'set only on a rare branch can still be legitimate.')
    if unknown and args.strict:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
