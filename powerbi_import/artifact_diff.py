"""Artifact Diff Engine — structured diff between two .pbip project directories.

Sprint 178: Compares TMDL semantic models (tables, columns, measures,
relationships, M partitions) and PBIR reports (pages, visuals, filters)
across two migration runs.  Produces a structured JSON diff and an
interactive HTML report.

Usage::

    from powerbi_import.artifact_diff import diff_projects, generate_diff_report
    result = diff_projects('artifacts/v1/MyProject', 'artifacts/v2/MyProject')
    print(result.summary())
    generate_diff_report(result, 'diff_report.html')

CLI::

    python migrate.py workbook.twbx --diff artifacts/previous/
    python migrate.py workbook.twbx --save-baseline baselines/v1
    python migrate.py workbook.twbx --check-baseline baselines/v1
"""

import glob
import hashlib
import json
import logging
import os
import re
import shutil
from datetime import datetime

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
#  Data structures
# ═══════════════════════════════════════════════════════════════════════

ADDED = 'added'
REMOVED = 'removed'
MODIFIED = 'modified'


class DiffEntry:
    """A single change between two .pbip projects."""

    __slots__ = ('category', 'change_type', 'name', 'parent', 'before', 'after', 'detail')

    def __init__(self, category, change_type, name, parent='',
                 before='', after='', detail=''):
        self.category = category       # table, column, measure, relationship, page, visual, filter, partition
        self.change_type = change_type  # added, removed, modified
        self.name = name
        self.parent = parent           # parent object (table for column/measure, page for visual)
        self.before = before           # previous value (for modified)
        self.after = after             # current value (for modified)
        self.detail = detail           # human-readable detail

    def __repr__(self):
        prefix = f'{self.parent}.' if self.parent else ''
        return f'DiffEntry({self.category}, {self.change_type}, {prefix}{self.name})'

    def to_dict(self):
        d = {
            'category': self.category,
            'change_type': self.change_type,
            'name': self.name,
        }
        if self.parent:
            d['parent'] = self.parent
        if self.before:
            d['before'] = self.before
        if self.after:
            d['after'] = self.after
        if self.detail:
            d['detail'] = self.detail
        return d


class DiffReport:
    """Collection of diff entries between two .pbip projects."""

    def __init__(self, entries=None, old_path='', new_path='', timestamp=None):
        self.entries = entries or []
        self.old_path = old_path
        self.new_path = new_path
        self.timestamp = timestamp or datetime.now().isoformat()

    @property
    def has_changes(self):
        return len(self.entries) > 0

    @property
    def added(self):
        return [e for e in self.entries if e.change_type == ADDED]

    @property
    def removed(self):
        return [e for e in self.entries if e.change_type == REMOVED]

    @property
    def modified(self):
        return [e for e in self.entries if e.change_type == MODIFIED]

    def by_category(self, category):
        return [e for e in self.entries if e.category == category]

    def summary(self):
        """Human-readable summary string."""
        if not self.has_changes:
            return 'No differences detected between the two projects.'
        lines = [f'Artifact diff: {len(self.entries)} change(s)']
        for cat in ('table', 'column', 'measure', 'relationship',
                    'page', 'visual', 'filter', 'partition', 'role'):
            items = self.by_category(cat)
            if items:
                added = sum(1 for e in items if e.change_type == ADDED)
                removed = sum(1 for e in items if e.change_type == REMOVED)
                modified = sum(1 for e in items if e.change_type == MODIFIED)
                parts = []
                if added:
                    parts.append(f'+{added}')
                if removed:
                    parts.append(f'-{removed}')
                if modified:
                    parts.append(f'~{modified}')
                lines.append(f'  {cat}: {", ".join(parts)}')
        return '\n'.join(lines)

    def to_dict(self):
        return {
            'old_path': self.old_path,
            'new_path': self.new_path,
            'timestamp': self.timestamp,
            'has_changes': self.has_changes,
            'total_changes': len(self.entries),
            'summary': {
                'added': len(self.added),
                'removed': len(self.removed),
                'modified': len(self.modified),
            },
            'by_category': {
                cat: len(self.by_category(cat))
                for cat in ('table', 'column', 'measure', 'relationship',
                            'page', 'visual', 'filter', 'partition', 'role')
                if self.by_category(cat)
            },
            'entries': [e.to_dict() for e in self.entries],
        }

    def save(self, path):
        """Write diff report as JSON."""
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════
#  TMDL Parsing — lightweight extraction of model objects
# ═══════════════════════════════════════════════════════════════════════

_RE_TABLE_NAME = re.compile(r"^table\s+'?(.*?)'?\s*$", re.MULTILINE)
_RE_COLUMN = re.compile(
    r"^\tcolumn\s+'?(.*?)'?\s*$", re.MULTILINE
)
_RE_MEASURE = re.compile(
    r"^\tmeasure\s+'?(.*?)'?\s*=\s*$", re.MULTILINE
)
_RE_EXPRESSION = re.compile(
    r"^\t\texpression\s*=\s*$", re.MULTILINE
)
_RE_PARTITION = re.compile(
    r"^\tpartition\s+'?(.*?)'?\s*=\s*m\s*$", re.MULTILINE
)
_RE_RELATIONSHIP = re.compile(
    r"^\trelationship\s+'?(.*?)'?\s*$", re.MULTILINE
)
_RE_ROLE = re.compile(
    r"^role\s+'?(.*?)'?\s*$", re.MULTILINE
)


def _parse_tmdl_table(filepath):
    """Parse a single .tmdl table file.

    Returns:
        dict: {name, columns: [{name, dataType, dataCategory, isHidden}],
               measures: [{name, expression_hash}],
               partitions: [{name, content_hash}]}
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return None

    # Table name
    m = _RE_TABLE_NAME.search(text)
    table_name = m.group(1).replace("''", "'") if m else os.path.splitext(os.path.basename(filepath))[0]

    lines = text.split('\n')
    columns = []
    measures = []
    partitions = []

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip()

        # Column
        if stripped.startswith('\tcolumn ') or stripped.startswith('    column '):
            col_name = re.sub(r"^\s+column\s+'?(.*?)'?\s*$", r'\1', stripped)
            col_name = col_name.replace("''", "'")
            col_info = {'name': col_name}
            # Scan properties
            j = i + 1
            while j < len(lines) and lines[j].strip() and not (
                lines[j].startswith('\tcolumn ') or lines[j].startswith('\tmeasure ') or
                lines[j].startswith('\tpartition ') or lines[j].startswith('\thierarchy ') or
                lines[j].startswith('    column ') or lines[j].startswith('    measure ') or
                lines[j].startswith('    partition ') or lines[j].startswith('    hierarchy ')
            ):
                prop = lines[j].strip()
                if prop.startswith('dataType:'):
                    col_info['dataType'] = prop.split(':', 1)[1].strip()
                elif prop.startswith('dataCategory:'):
                    col_info['dataCategory'] = prop.split(':', 1)[1].strip()
                elif prop.startswith('isHidden'):
                    col_info['isHidden'] = True
                j += 1
            columns.append(col_info)

        # Measure
        elif stripped.startswith('\tmeasure ') or stripped.startswith('    measure '):
            meas_name = re.sub(r"^\s+measure\s+'?(.*?)'?\s*=\s*$", r'\1', stripped)
            if meas_name == stripped:
                # No trailing =, try without
                meas_name = re.sub(r"^\s+measure\s+'?(.*?)'?\s*$", r'\1', stripped)
            meas_name = meas_name.replace("''", "'")
            # Collect expression lines
            expr_lines = []
            j = i + 1
            while j < len(lines):
                lj = lines[j]
                lj_s = lj.strip()
                if not lj_s:
                    break
                if (lj.startswith('\tcolumn ') or lj.startswith('\tmeasure ') or
                    lj.startswith('\tpartition ') or lj.startswith('\thierarchy ') or
                    lj.startswith('    column ') or lj.startswith('    measure ') or
                    lj.startswith('    partition ') or lj.startswith('    hierarchy ')):
                    break
                # expression lines start with deeper indent or 'expression ='
                if lj_s.startswith('expression') or lj_s.startswith('```'):
                    expr_lines.append(lj_s)
                elif expr_lines:
                    expr_lines.append(lj_s)
                j += 1
            expr_text = '\n'.join(expr_lines)
            expr_hash = hashlib.sha256(expr_text.encode('utf-8')).hexdigest()[:16]
            measures.append({'name': meas_name, 'expression_hash': expr_hash,
                             'expression': expr_text})

        # Partition
        elif stripped.startswith('\tpartition ') or stripped.startswith('    partition '):
            part_name = re.sub(r"^\s+partition\s+'?(.*?)'?\s*=\s*m?\s*$", r'\1', stripped)
            if part_name == stripped:
                part_name = re.sub(r"^\s+partition\s+'?(.*?)'?\s*$", r'\1', stripped)
            part_name = part_name.replace("''", "'")
            # Collect partition content
            part_lines = []
            j = i + 1
            while j < len(lines):
                lj = lines[j]
                lj_s = lj.strip()
                if not lj_s:
                    break
                if (lj.startswith('\tcolumn ') or lj.startswith('\tmeasure ') or
                    lj.startswith('\tpartition ') or lj.startswith('\thierarchy ') or
                    lj.startswith('    column ') or lj.startswith('    measure ') or
                    lj.startswith('    partition ') or lj.startswith('    hierarchy ')):
                    break
                part_lines.append(lj_s)
                j += 1
            part_text = '\n'.join(part_lines)
            part_hash = hashlib.sha256(part_text.encode('utf-8')).hexdigest()[:16]
            partitions.append({'name': part_name, 'content_hash': part_hash})

        i += 1

    return {
        'name': table_name,
        'columns': columns,
        'measures': measures,
        'partitions': partitions,
    }


def _parse_relationships(filepath):
    """Parse relationships.tmdl and return list of relationship dicts."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return []

    relationships = []
    lines = text.split('\n')
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith('relationship '):
            rel_name = re.sub(r"^relationship\s+'?(.*?)'?\s*$", r'\1', stripped)
            rel_name = rel_name.replace("''", "'")
            rel_info = {'name': rel_name}
            j = i + 1
            while j < len(lines) and lines[j].strip() and not lines[j].strip().startswith('relationship '):
                prop = lines[j].strip()
                if prop.startswith('fromColumn:'):
                    rel_info['fromColumn'] = prop.split(':', 1)[1].strip().strip("'")
                elif prop.startswith('toColumn:'):
                    rel_info['toColumn'] = prop.split(':', 1)[1].strip().strip("'")
                elif prop.startswith('fromTable:'):
                    rel_info['fromTable'] = prop.split(':', 1)[1].strip().strip("'")
                elif prop.startswith('toTable:'):
                    rel_info['toTable'] = prop.split(':', 1)[1].strip().strip("'")
                elif prop.startswith('fromCardinality:'):
                    rel_info['fromCardinality'] = prop.split(':', 1)[1].strip()
                elif prop.startswith('toCardinality:'):
                    rel_info['toCardinality'] = prop.split(':', 1)[1].strip()
                elif prop.startswith('crossFilteringBehavior:'):
                    rel_info['crossFilter'] = prop.split(':', 1)[1].strip()
                j += 1
            # Build a signature for comparison
            sig_parts = [
                rel_info.get('fromTable', ''), rel_info.get('fromColumn', ''),
                rel_info.get('toTable', ''), rel_info.get('toColumn', ''),
                rel_info.get('fromCardinality', ''), rel_info.get('toCardinality', ''),
                rel_info.get('crossFilter', ''),
            ]
            rel_info['signature'] = '|'.join(sig_parts)
            relationships.append(rel_info)
        i += 1
    return relationships


def _parse_roles(filepath):
    """Parse roles.tmdl and return list of role dicts."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return []

    roles = []
    lines = text.split('\n')
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith('role '):
            role_name = re.sub(r"^role\s+'?(.*?)'?\s*$", r'\1', stripped)
            role_name = role_name.replace("''", "'")
            # Collect role content for hashing
            role_lines = [stripped]
            j = i + 1
            while j < len(lines) and lines[j].strip() and not lines[j].strip().startswith('role '):
                role_lines.append(lines[j].strip())
                j += 1
            role_text = '\n'.join(role_lines)
            role_hash = hashlib.sha256(role_text.encode('utf-8')).hexdigest()[:16]
            roles.append({'name': role_name, 'content_hash': role_hash})
        i += 1
    return roles


# ═══════════════════════════════════════════════════════════════════════
#  PBIR Parsing — pages, visuals, filters
# ═══════════════════════════════════════════════════════════════════════

def _load_json(filepath):
    """Safely load a JSON file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _load_pages(report_dir):
    """Load all pages from a PBIR report definition directory.

    Returns:
        dict: {page_name: {displayName, visuals: [{name, visualType, title, fields}]}}
    """
    pages = {}
    pages_dir = os.path.join(report_dir, 'definition', 'pages')
    if not os.path.isdir(pages_dir):
        return pages

    for page_folder in sorted(os.listdir(pages_dir)):
        page_json_path = os.path.join(pages_dir, page_folder, 'page.json')
        if not os.path.isfile(page_json_path):
            continue
        page_data = _load_json(page_json_path)
        if not page_data:
            continue

        display_name = page_data.get('displayName', page_folder)
        page_type = page_data.get('pageType', '')

        # Load visuals
        visuals = []
        visuals_dir = os.path.join(pages_dir, page_folder, 'visuals')
        if os.path.isdir(visuals_dir):
            for vis_folder in sorted(os.listdir(visuals_dir)):
                vis_json_path = os.path.join(visuals_dir, vis_folder, 'visual.json')
                if not os.path.isfile(vis_json_path):
                    continue
                vis_data = _load_json(vis_json_path)
                if not vis_data:
                    continue
                vis_type = vis_data.get('visual', {}).get('visualType', 'unknown')
                vis_title = ''
                # Try extracting title from vcObjects
                vc = vis_data.get('visual', {}).get('vcObjects', {})
                title_obj = vc.get('title', [{}])
                if isinstance(title_obj, list) and title_obj:
                    props = title_obj[0].get('properties', {})
                    text_prop = props.get('text', {})
                    if isinstance(text_prop, dict):
                        vis_title = text_prop.get('expr', {}).get('Literal', {}).get('Value', '')
                        if vis_title.startswith("'") and vis_title.endswith("'"):
                            vis_title = vis_title[1:-1]

                # Extract field references
                fields = []
                queries = vis_data.get('visual', {}).get('query', {})
                if isinstance(queries, dict):
                    commands = queries.get('Commands', [])
                    if isinstance(commands, list):
                        for cmd in commands:
                            sq = cmd.get('SemanticQueryDataShapeCommand', {})
                            query_obj = sq.get('Query', {})
                            _extract_fields(query_obj, fields)

                visuals.append({
                    'name': vis_folder,
                    'visualType': vis_type,
                    'title': vis_title,
                    'field_count': len(fields),
                })

        pages[page_folder] = {
            'displayName': display_name,
            'pageType': page_type,
            'visual_count': len(visuals),
            'visuals': visuals,
        }

    return pages


def _extract_fields(query_obj, fields):
    """Recursively extract field names from a PBIR query object."""
    if isinstance(query_obj, dict):
        # Check for Column references
        col = query_obj.get('Column', {})
        if isinstance(col, dict) and 'Property' in col:
            fields.append(col['Property'])
        # Check for Measure references
        meas = query_obj.get('Measure', {})
        if isinstance(meas, dict) and 'Property' in meas:
            fields.append(meas['Property'])
        for v in query_obj.values():
            _extract_fields(v, fields)
    elif isinstance(query_obj, list):
        for item in query_obj:
            _extract_fields(item, fields)


def _load_report_filters(report_dir):
    """Load report-level filters from report.json."""
    report_json = os.path.join(report_dir, 'definition', 'report.json')
    data = _load_json(report_json)
    if not data:
        return []
    filters = data.get('filterConfig', {}).get('filters', [])
    result = []
    for f in filters:
        fname = f.get('name', '')
        ftype = f.get('type', '')
        result.append({'name': fname, 'type': ftype})
    return result


# ═══════════════════════════════════════════════════════════════════════
#  Project Loading
# ═══════════════════════════════════════════════════════════════════════

def _find_semantic_model_dir(project_dir):
    """Locate the .SemanticModel directory within a .pbip project."""
    for item in os.listdir(project_dir):
        full = os.path.join(project_dir, item)
        if item.endswith('.SemanticModel') and os.path.isdir(full):
            return full
    return None


def _find_report_dir(project_dir):
    """Locate the .Report directory within a .pbip project."""
    for item in os.listdir(project_dir):
        full = os.path.join(project_dir, item)
        if item.endswith('.Report') and os.path.isdir(full):
            return full
    return None


def load_project(project_dir):
    """Load a .pbip project into a structured dict for diffing.

    Returns:
        dict with keys: tables, relationships, roles, pages, filters, metadata
    """
    result = {
        'tables': {},
        'relationships': [],
        'roles': [],
        'pages': {},
        'filters': [],
        'path': project_dir,
    }

    sm_dir = _find_semantic_model_dir(project_dir)
    if sm_dir:
        defn_dir = os.path.join(sm_dir, 'definition')
        tables_dir = os.path.join(defn_dir, 'tables')

        # Parse tables
        if os.path.isdir(tables_dir):
            for tmdl_file in sorted(glob.glob(os.path.join(tables_dir, '*.tmdl'))):
                table = _parse_tmdl_table(tmdl_file)
                if table:
                    result['tables'][table['name']] = table

        # Parse relationships
        rel_path = os.path.join(defn_dir, 'relationships.tmdl')
        result['relationships'] = _parse_relationships(rel_path)

        # Parse roles
        roles_path = os.path.join(defn_dir, 'roles.tmdl')
        result['roles'] = _parse_roles(roles_path)

    report_dir = _find_report_dir(project_dir)
    if report_dir:
        result['pages'] = _load_pages(report_dir)
        result['filters'] = _load_report_filters(report_dir)

    return result


# ═══════════════════════════════════════════════════════════════════════
#  Diff Engine
# ═══════════════════════════════════════════════════════════════════════

def _diff_tables(old_tables, new_tables):
    """Compare tables and return list of DiffEntry."""
    entries = []
    old_names = set(old_tables.keys())
    new_names = set(new_tables.keys())

    for name in sorted(new_names - old_names):
        t = new_tables[name]
        entries.append(DiffEntry(
            'table', ADDED, name,
            detail=f'{len(t["columns"])} columns, {len(t["measures"])} measures',
        ))

    for name in sorted(old_names - new_names):
        t = old_tables[name]
        entries.append(DiffEntry(
            'table', REMOVED, name,
            detail=f'{len(t["columns"])} columns, {len(t["measures"])} measures',
        ))

    # Compare shared tables
    for name in sorted(old_names & new_names):
        old_t = old_tables[name]
        new_t = new_tables[name]

        # Columns
        old_cols = {c['name']: c for c in old_t['columns']}
        new_cols = {c['name']: c for c in new_t['columns']}

        for cname in sorted(set(new_cols) - set(old_cols)):
            entries.append(DiffEntry('column', ADDED, cname, parent=name))

        for cname in sorted(set(old_cols) - set(new_cols)):
            entries.append(DiffEntry('column', REMOVED, cname, parent=name))

        for cname in sorted(set(old_cols) & set(new_cols)):
            oc = old_cols[cname]
            nc = new_cols[cname]
            changes = []
            for prop in ('dataType', 'dataCategory', 'isHidden'):
                ov = oc.get(prop, '')
                nv = nc.get(prop, '')
                if ov != nv:
                    changes.append(f'{prop}: {ov!r} → {nv!r}')
            if changes:
                entries.append(DiffEntry(
                    'column', MODIFIED, cname, parent=name,
                    detail='; '.join(changes),
                ))

        # Measures
        old_meas = {m['name']: m for m in old_t['measures']}
        new_meas = {m['name']: m for m in new_t['measures']}

        for mname in sorted(set(new_meas) - set(old_meas)):
            entries.append(DiffEntry('measure', ADDED, mname, parent=name))

        for mname in sorted(set(old_meas) - set(new_meas)):
            entries.append(DiffEntry('measure', REMOVED, mname, parent=name))

        for mname in sorted(set(old_meas) & set(new_meas)):
            if old_meas[mname]['expression_hash'] != new_meas[mname]['expression_hash']:
                entries.append(DiffEntry(
                    'measure', MODIFIED, mname, parent=name,
                    before=old_meas[mname].get('expression', ''),
                    after=new_meas[mname].get('expression', ''),
                ))

        # Partitions (M queries)
        old_parts = {p['name']: p for p in old_t['partitions']}
        new_parts = {p['name']: p for p in new_t['partitions']}

        for pname in sorted(set(new_parts) - set(old_parts)):
            entries.append(DiffEntry('partition', ADDED, pname, parent=name))

        for pname in sorted(set(old_parts) - set(new_parts)):
            entries.append(DiffEntry('partition', REMOVED, pname, parent=name))

        for pname in sorted(set(old_parts) & set(new_parts)):
            if old_parts[pname]['content_hash'] != new_parts[pname]['content_hash']:
                entries.append(DiffEntry(
                    'partition', MODIFIED, pname, parent=name,
                    detail='M query content changed',
                ))

    return entries


def _diff_relationships(old_rels, new_rels):
    """Compare relationships by signature."""
    entries = []
    old_by_name = {r['name']: r for r in old_rels}
    new_by_name = {r['name']: r for r in new_rels}

    for name in sorted(set(new_by_name) - set(old_by_name)):
        r = new_by_name[name]
        detail = f'{r.get("fromTable", "?")}.{r.get("fromColumn", "?")} → {r.get("toTable", "?")}.{r.get("toColumn", "?")}'
        entries.append(DiffEntry('relationship', ADDED, name, detail=detail))

    for name in sorted(set(old_by_name) - set(new_by_name)):
        r = old_by_name[name]
        detail = f'{r.get("fromTable", "?")}.{r.get("fromColumn", "?")} → {r.get("toTable", "?")}.{r.get("toColumn", "?")}'
        entries.append(DiffEntry('relationship', REMOVED, name, detail=detail))

    for name in sorted(set(old_by_name) & set(new_by_name)):
        if old_by_name[name].get('signature', '') != new_by_name[name].get('signature', ''):
            entries.append(DiffEntry(
                'relationship', MODIFIED, name,
                before=old_by_name[name].get('signature', ''),
                after=new_by_name[name].get('signature', ''),
            ))

    return entries


def _diff_roles(old_roles, new_roles):
    """Compare RLS roles."""
    entries = []
    old_by_name = {r['name']: r for r in old_roles}
    new_by_name = {r['name']: r for r in new_roles}

    for name in sorted(set(new_by_name) - set(old_by_name)):
        entries.append(DiffEntry('role', ADDED, name))

    for name in sorted(set(old_by_name) - set(new_by_name)):
        entries.append(DiffEntry('role', REMOVED, name))

    for name in sorted(set(old_by_name) & set(new_by_name)):
        if old_by_name[name].get('content_hash', '') != new_by_name[name].get('content_hash', ''):
            entries.append(DiffEntry('role', MODIFIED, name))

    return entries


def _diff_pages(old_pages, new_pages):
    """Compare pages and visuals."""
    entries = []
    old_keys = set(old_pages.keys())
    new_keys = set(new_pages.keys())

    for key in sorted(new_keys - old_keys):
        p = new_pages[key]
        entries.append(DiffEntry(
            'page', ADDED, p['displayName'],
            detail=f'{p["visual_count"]} visuals',
        ))

    for key in sorted(old_keys - new_keys):
        p = old_pages[key]
        entries.append(DiffEntry(
            'page', REMOVED, p['displayName'],
            detail=f'{p["visual_count"]} visuals',
        ))

    # Compare shared pages
    for key in sorted(old_keys & new_keys):
        old_p = old_pages[key]
        new_p = new_pages[key]
        page_name = new_p['displayName']

        # Page property changes
        if old_p.get('pageType', '') != new_p.get('pageType', ''):
            entries.append(DiffEntry(
                'page', MODIFIED, page_name,
                detail=f'pageType: {old_p.get("pageType", "")} → {new_p.get("pageType", "")}',
            ))

        # Visuals
        old_vis = {v['name']: v for v in old_p.get('visuals', [])}
        new_vis = {v['name']: v for v in new_p.get('visuals', [])}

        for vname in sorted(set(new_vis) - set(old_vis)):
            v = new_vis[vname]
            entries.append(DiffEntry(
                'visual', ADDED, v.get('title', vname),
                parent=page_name,
                detail=f'type: {v["visualType"]}',
            ))

        for vname in sorted(set(old_vis) - set(new_vis)):
            v = old_vis[vname]
            entries.append(DiffEntry(
                'visual', REMOVED, v.get('title', vname),
                parent=page_name,
                detail=f'type: {v["visualType"]}',
            ))

        for vname in sorted(set(old_vis) & set(new_vis)):
            ov = old_vis[vname]
            nv = new_vis[vname]
            changes = []
            if ov['visualType'] != nv['visualType']:
                changes.append(f'type: {ov["visualType"]} → {nv["visualType"]}')
            if ov.get('field_count', 0) != nv.get('field_count', 0):
                changes.append(f'fields: {ov.get("field_count", 0)} → {nv.get("field_count", 0)}')
            if changes:
                entries.append(DiffEntry(
                    'visual', MODIFIED, nv.get('title', vname),
                    parent=page_name,
                    detail='; '.join(changes),
                ))

    return entries


def _diff_filters(old_filters, new_filters):
    """Compare report-level filters."""
    entries = []
    old_by_name = {f['name']: f for f in old_filters}
    new_by_name = {f['name']: f for f in new_filters}

    for name in sorted(set(new_by_name) - set(old_by_name)):
        entries.append(DiffEntry('filter', ADDED, name))

    for name in sorted(set(old_by_name) - set(new_by_name)):
        entries.append(DiffEntry('filter', REMOVED, name))

    for name in sorted(set(old_by_name) & set(new_by_name)):
        if old_by_name[name].get('type', '') != new_by_name[name].get('type', ''):
            entries.append(DiffEntry(
                'filter', MODIFIED, name,
                detail=f'type: {old_by_name[name].get("type", "")} → {new_by_name[name].get("type", "")}',
            ))

    return entries


def diff_projects(old_dir, new_dir):
    """Compare two .pbip project directories.

    Args:
        old_dir: Path to the previous (baseline) .pbip project
        new_dir: Path to the current (new) .pbip project

    Returns:
        DiffReport with all detected changes
    """
    logger.info("Loading old project: %s", old_dir)
    old = load_project(old_dir)

    logger.info("Loading new project: %s", new_dir)
    new = load_project(new_dir)

    entries = []
    entries.extend(_diff_tables(old['tables'], new['tables']))
    entries.extend(_diff_relationships(old['relationships'], new['relationships']))
    entries.extend(_diff_roles(old['roles'], new['roles']))
    entries.extend(_diff_pages(old['pages'], new['pages']))
    entries.extend(_diff_filters(old['filters'], new['filters']))

    return DiffReport(
        entries=entries,
        old_path=old_dir,
        new_path=new_dir,
    )


# ═══════════════════════════════════════════════════════════════════════
#  Baseline Management
# ═══════════════════════════════════════════════════════════════════════

BASELINE_MANIFEST = '.artifact_baseline'


def save_baseline(project_dir, baseline_dir):
    """Save the current .pbip project as a baseline for future comparison.

    Copies the project structure to the baseline directory with a manifest
    file recording the source and timestamp.

    Args:
        project_dir: Path to the .pbip project to snapshot
        baseline_dir: Path to store the baseline

    Returns:
        str: Path to the baseline directory
    """
    if os.path.isdir(baseline_dir):
        shutil.rmtree(baseline_dir)
    shutil.copytree(project_dir, baseline_dir)

    # Write manifest
    manifest = {
        'source': os.path.abspath(project_dir),
        'timestamp': datetime.now().isoformat(),
        'version': '1.0',
    }
    manifest_path = os.path.join(baseline_dir, BASELINE_MANIFEST)
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return baseline_dir


def check_baseline(project_dir, baseline_dir):
    """Compare a project against a stored baseline.

    Args:
        project_dir: Path to the current .pbip project
        baseline_dir: Path to the stored baseline

    Returns:
        tuple: (passed: bool, report: DiffReport)
    """
    if not os.path.isdir(baseline_dir):
        logger.warning("No baseline found at %s", baseline_dir)
        return False, DiffReport(old_path=baseline_dir, new_path=project_dir)

    report = diff_projects(baseline_dir, project_dir)
    return not report.has_changes, report


# ═══════════════════════════════════════════════════════════════════════
#  HTML Report Generator
# ═══════════════════════════════════════════════════════════════════════

_CSS_EXTRA = """
/* artifact-diff extras */
.diff-added { color: var(--success); }
.diff-removed { color: var(--fail); }
.diff-modified { color: #ca5010; }
.diff-badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
              font-size: 0.75rem; font-weight: 600; color: #fff; }
.diff-badge.added { background: var(--success); }
.diff-badge.removed { background: var(--fail); }
.diff-badge.modified { background: #ca5010; }
.diff-before-after { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.diff-before-after .panel { padding: 0.75rem; border-radius: 6px; font-size: 0.82rem; }
.diff-before-after .panel.before { background: #fde7e9; border: 1px solid #d13438; }
.diff-before-after .panel.after { background: #dff6dd; border: 1px solid #107c10; }
.diff-before-after .panel h5 { margin: 0 0 0.5rem; font-size: 0.75rem; text-transform: uppercase; }
.diff-before-after pre { margin: 0; background: transparent; padding: 0; white-space: pre-wrap; }
.no-changes { text-align: center; padding: 3rem; color: var(--pbi-gray); }
.no-changes .icon { font-size: 3rem; margin-bottom: 1rem; }
@media print {
  .diff-before-after { grid-template-columns: 1fr; }
}
"""


def generate_diff_report(report, output_path=None):
    """Generate an interactive HTML diff report.

    Args:
        report: DiffReport instance
        output_path: Path to write the HTML file

    Returns:
        str: HTML content (also written to output_path if provided)
    """
    try:
        from powerbi_import.html_template import (
            html_open, html_close, stat_card, stat_grid, section_open,
            section_close, data_table, badge, esc, donut_chart,
        )
    except ImportError:
        from html_template import (
            html_open, html_close, stat_card, stat_grid, section_open,
            section_close, data_table, badge, esc, donut_chart,
        )

    ts = datetime.now().strftime('%Y-%m-%d %H:%M')
    old_label = os.path.basename(report.old_path) if report.old_path else 'Previous'
    new_label = os.path.basename(report.new_path) if report.new_path else 'Current'

    html = html_open(
        title='Artifact Diff Report',
        subtitle=f'{old_label} → {new_label}',
        timestamp=ts,
        version='',
    )
    html += f'<style>{_CSS_EXTRA}</style>\n'

    # ── Summary stats ──────────────────────────────────────
    if not report.has_changes:
        html += '<div class="no-changes"><div class="icon">✓</div>'
        html += '<h2>No Differences Detected</h2>'
        html += '<p>The two projects are identical.</p></div>\n'
        html += html_close()
        if output_path:
            os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(html)
        return html

    added_count = len(report.added)
    removed_count = len(report.removed)
    modified_count = len(report.modified)

    html += stat_grid([
        stat_card(str(len(report.entries)), 'Total Changes', accent='blue'),
        stat_card(str(added_count), 'Added', accent='success'),
        stat_card(str(removed_count), 'Removed', accent='fail'),
        stat_card(str(modified_count), 'Modified', accent='warn'),
    ])

    # ── Donut chart ────────────────────────────────────────
    if report.has_changes:
        _COLORS = ['#0078d4', '#107c10', '#d13438', '#ca5010',
                    '#881798', '#008272', '#005b70', '#7719aa', '#004e8c']
        categories = []
        for cat in ('table', 'column', 'measure', 'relationship',
                    'page', 'visual', 'filter', 'partition', 'role'):
            count = len(report.by_category(cat))
            if count:
                color = _COLORS[len(categories) % len(_COLORS)]
                categories.append((cat.capitalize(), count, color))
        if categories:
            html += '<h3 style="margin:1.5rem 0 0.5rem;">Changes by Category</h3>\n'
            html += donut_chart(
                categories,
                center_text=str(len(report.entries)),
            )

    # ── Per-category sections ──────────────────────────────
    category_icons = {
        'table': '🗃️', 'column': '📊', 'measure': '📐',
        'relationship': '🔗', 'page': '📄', 'visual': '👁️',
        'filter': '🔍', 'partition': '⚡', 'role': '🔒',
    }

    for cat in ('table', 'column', 'measure', 'relationship',
                'page', 'visual', 'filter', 'partition', 'role'):
        items = report.by_category(cat)
        if not items:
            continue

        icon = category_icons.get(cat, '📦')
        html += section_open(
            f'section-{cat}',
            f'{icon} {cat.capitalize()} Changes ({len(items)})',
        )

        # Summary table
        headers = ['Name', 'Change', 'Parent', 'Detail']
        rows = []
        for e in items:
            change_badge = f'<span class="diff-badge {e.change_type}">{e.change_type.upper()}</span>'
            rows.append([
                esc(e.name),
                change_badge,
                esc(e.parent),
                esc(e.detail),
            ])

        html += data_table(
            headers, rows,
            table_id=f'tbl-{cat}',
            sortable=True,
            searchable=len(rows) > 10,
        )

        # Before/After panels for modified measures
        if cat == 'measure':
            modified_measures = [e for e in items if e.change_type == MODIFIED and (e.before or e.after)]
            if modified_measures:
                html += '<h4 style="margin: 1.5rem 0 0.5rem;">Formula Changes</h4>\n'
                for e in modified_measures:
                    html += f'<p style="font-weight:600; margin: 1rem 0 0.25rem;">{esc(e.parent)}.{esc(e.name)}</p>\n'
                    html += '<div class="diff-before-after">\n'
                    html += f'<div class="panel before"><h5>Previous</h5><pre>{esc(e.before)}</pre></div>\n'
                    html += f'<div class="panel after"><h5>Current</h5><pre>{esc(e.after)}</pre></div>\n'
                    html += '</div>\n'

        html += section_close()

    html += html_close()

    if output_path:
        os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)
        logger.info("Diff report written to %s", output_path)

    return html


# ═══════════════════════════════════════════════════════════════════════
#  CLI entry point (standalone usage)
# ═══════════════════════════════════════════════════════════════════════

def main():
    """CLI: compare two .pbip project directories."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Compare two .pbip project directories and generate a diff report.',
    )
    parser.add_argument('old_dir', help='Path to the previous/baseline .pbip project')
    parser.add_argument('new_dir', help='Path to the current/new .pbip project')
    parser.add_argument('--output', '-o', default=None,
                        help='Output path for the HTML diff report')
    parser.add_argument('--json', '-j', default=None,
                        help='Output path for the JSON diff report')

    args = parser.parse_args()

    report = diff_projects(args.old_dir, args.new_dir)
    print(report.summary())

    if args.json:
        report.save(args.json)
        print(f'\nJSON report: {args.json}')

    html_path = args.output
    if html_path is None and report.has_changes:
        html_path = 'artifact_diff_report.html'

    if html_path:
        generate_diff_report(report, html_path)
        print(f'HTML report: {html_path}')


if __name__ == '__main__':
    main()
