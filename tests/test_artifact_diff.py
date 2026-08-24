"""Tests for powerbi_import.artifact_diff — Sprint 178.

Covers:
  - DiffEntry / DiffReport data structures
  - TMDL parsing (tables, columns, measures, partitions)
  - Relationship parsing
  - Role parsing
  - PBIR page/visual/filter loading
  - Full project loading
  - Diff engine (tables, columns, measures, relationships, pages, visuals, filters, partitions, roles)
  - Baseline save / check
  - HTML report generation
  - JSON serialisation round-trip
  - No-change detection
  - CLI main() entry point
"""

import json
import os
import shutil
import sys
import tempfile
import textwrap

import pytest

# Ensure project root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.artifact_diff import (
    ADDED,
    MODIFIED,
    REMOVED,
    DiffEntry,
    DiffReport,
    _diff_filters,
    _diff_pages,
    _diff_relationships,
    _diff_roles,
    _diff_tables,
    _load_json,
    _load_pages,
    _load_report_filters,
    _parse_relationships,
    _parse_roles,
    _parse_tmdl_table,
    check_baseline,
    diff_projects,
    generate_diff_report,
    load_project,
    save_baseline,
)


# ═══════════════════════════════════════════════════════════════════════
#  Fixtures — build minimal .pbip projects on disk
# ═══════════════════════════════════════════════════════════════════════

def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2)


def _build_project(base_dir, name='TestProject', tables=None, relationships='',
                   roles='', pages=None, report_json=None):
    """Build a minimal .pbip project on disk.

    Args:
        base_dir: Root directory
        name: Project name
        tables: dict of table_name → tmdl_content
        relationships: TMDL content for relationships.tmdl
        roles: TMDL content for roles.tmdl
        pages: dict of page_folder → {page_json, visuals: {folder: visual_json}}
        report_json: dict for report.json
    """
    proj = os.path.join(base_dir, name)
    os.makedirs(proj, exist_ok=True)

    # SemanticModel
    sm_dir = os.path.join(proj, f'{name}.SemanticModel', 'definition')
    tables_dir = os.path.join(sm_dir, 'tables')
    os.makedirs(tables_dir, exist_ok=True)

    _write(os.path.join(sm_dir, 'model.tmdl'), 'model Model\n')
    _write(os.path.join(sm_dir, 'database.tmdl'), 'database\n')

    if tables:
        for tname, content in tables.items():
            _write(os.path.join(tables_dir, f'{tname}.tmdl'), content)

    _write(os.path.join(sm_dir, 'relationships.tmdl'), relationships)
    _write(os.path.join(sm_dir, 'roles.tmdl'), roles)

    # Report
    report_dir = os.path.join(proj, f'{name}.Report')
    defn_dir = os.path.join(report_dir, 'definition')
    os.makedirs(defn_dir, exist_ok=True)

    _write_json(os.path.join(defn_dir, 'report.json'), report_json or {})

    pages_dir = os.path.join(defn_dir, 'pages')
    if pages:
        for page_folder, page_info in pages.items():
            page_path = os.path.join(pages_dir, page_folder)
            os.makedirs(page_path, exist_ok=True)
            _write_json(os.path.join(page_path, 'page.json'), page_info.get('page_json', {}))
            if 'visuals' in page_info:
                for vis_folder, vis_json in page_info['visuals'].items():
                    vis_path = os.path.join(page_path, 'visuals', vis_folder)
                    os.makedirs(vis_path, exist_ok=True)
                    _write_json(os.path.join(vis_path, 'visual.json'), vis_json)

    return proj


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp(prefix='artifact_diff_test_')
    yield d
    shutil.rmtree(d, ignore_errors=True)


SAMPLE_TABLE_TMDL = textwrap.dedent("""\
    table 'Orders'

    \tcolumn 'OrderID'
    \t\tdataType: int64
    \t\tisHidden

    \tcolumn 'Amount'
    \t\tdataType: decimal

    \tmeasure 'Total Sales' =
    \t\texpression = SUM('Orders'[Amount])

    \tpartition 'Orders-partition' = m
    \t\tmode: import
    \t\tsource = let Source = Sql.Database("server", "db") in Source
""")

SAMPLE_TABLE_TMDL_V2 = textwrap.dedent("""\
    table 'Orders'

    \tcolumn 'OrderID'
    \t\tdataType: int64

    \tcolumn 'Amount'
    \t\tdataType: decimal
    \t\tdataCategory: Currency

    \tcolumn 'Region'
    \t\tdataType: string

    \tmeasure 'Total Sales' =
    \t\texpression = CALCULATE(SUM('Orders'[Amount]))

    \tmeasure 'Order Count' =
    \t\texpression = COUNTROWS('Orders')

    \tpartition 'Orders-partition' = m
    \t\tmode: import
    \t\tsource = let Source = Sql.Database("server2", "db") in Source
""")

SAMPLE_RELATIONSHIPS = textwrap.dedent("""\
    relationship 'rel-orders-customers'
    \tfromTable: 'Orders'
    \tfromColumn: 'CustomerID'
    \ttoTable: 'Customers'
    \ttoColumn: 'CustomerID'
    \tfromCardinality: many
    \ttoCardinality: one
    \tcrossFilteringBehavior: oneDirection
""")

SAMPLE_ROLES = textwrap.dedent("""\
    role 'RegionFilter'
    \ttablePermission 'Orders'
    \t\tfilterExpression = [Region] = "West"
""")


# ═══════════════════════════════════════════════════════════════════════
#  DiffEntry tests
# ═══════════════════════════════════════════════════════════════════════

class TestDiffEntry:
    def test_basic_creation(self):
        e = DiffEntry('table', ADDED, 'Orders')
        assert e.category == 'table'
        assert e.change_type == ADDED
        assert e.name == 'Orders'
        assert e.parent == ''

    def test_with_parent(self):
        e = DiffEntry('column', REMOVED, 'Amount', parent='Orders')
        assert e.parent == 'Orders'

    def test_to_dict_minimal(self):
        e = DiffEntry('measure', MODIFIED, 'Total Sales')
        d = e.to_dict()
        assert d == {'category': 'measure', 'change_type': 'modified', 'name': 'Total Sales'}
        assert 'parent' not in d

    def test_to_dict_full(self):
        e = DiffEntry('measure', MODIFIED, 'Total Sales', parent='Orders',
                      before='SUM(x)', after='CALCULATE(SUM(x))', detail='formula changed')
        d = e.to_dict()
        assert d['parent'] == 'Orders'
        assert d['before'] == 'SUM(x)'
        assert d['after'] == 'CALCULATE(SUM(x))'
        assert d['detail'] == 'formula changed'

    def test_repr(self):
        e = DiffEntry('column', ADDED, 'Region', parent='Orders')
        assert 'Orders.Region' in repr(e)


# ═══════════════════════════════════════════════════════════════════════
#  DiffReport tests
# ═══════════════════════════════════════════════════════════════════════

class TestDiffReport:
    def test_empty_report(self):
        r = DiffReport()
        assert not r.has_changes
        assert r.summary() == 'No differences detected between the two projects.'

    def test_with_entries(self):
        entries = [
            DiffEntry('table', ADDED, 'Products'),
            DiffEntry('column', REMOVED, 'Old', parent='Orders'),
            DiffEntry('measure', MODIFIED, 'Sales', parent='Orders'),
        ]
        r = DiffReport(entries=entries)
        assert r.has_changes
        assert len(r.added) == 1
        assert len(r.removed) == 1
        assert len(r.modified) == 1
        assert len(r.by_category('table')) == 1

    def test_summary_format(self):
        entries = [
            DiffEntry('table', ADDED, 'A'),
            DiffEntry('table', REMOVED, 'B'),
            DiffEntry('column', MODIFIED, 'C', parent='A'),
        ]
        r = DiffReport(entries=entries)
        s = r.summary()
        assert 'table: +1, -1' in s
        assert 'column: ~1' in s

    def test_to_dict(self):
        entries = [DiffEntry('table', ADDED, 'X')]
        r = DiffReport(entries=entries, old_path='/old', new_path='/new')
        d = r.to_dict()
        assert d['has_changes'] is True
        assert d['total_changes'] == 1
        assert d['summary']['added'] == 1
        assert d['by_category']['table'] == 1
        assert len(d['entries']) == 1

    def test_save_and_load(self, tmp_dir):
        entries = [DiffEntry('measure', MODIFIED, 'M', before='a', after='b')]
        r = DiffReport(entries=entries)
        path = os.path.join(tmp_dir, 'diff.json')
        r.save(path)
        with open(path, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
        assert loaded['total_changes'] == 1
        assert loaded['entries'][0]['before'] == 'a'


# ═══════════════════════════════════════════════════════════════════════
#  TMDL Parsing tests
# ═══════════════════════════════════════════════════════════════════════

class TestTMDLParsing:
    def test_parse_table_basic(self, tmp_dir):
        path = os.path.join(tmp_dir, 'Orders.tmdl')
        _write(path, SAMPLE_TABLE_TMDL)
        result = _parse_tmdl_table(path)
        assert result is not None
        assert result['name'] == 'Orders'

    def test_parse_table_columns(self, tmp_dir):
        path = os.path.join(tmp_dir, 'Orders.tmdl')
        _write(path, SAMPLE_TABLE_TMDL)
        result = _parse_tmdl_table(path)
        cols = {c['name']: c for c in result['columns']}
        assert 'OrderID' in cols
        assert cols['OrderID'].get('dataType') == 'int64'
        assert cols['OrderID'].get('isHidden') is True
        assert 'Amount' in cols
        assert cols['Amount'].get('dataType') == 'decimal'

    def test_parse_table_measures(self, tmp_dir):
        path = os.path.join(tmp_dir, 'Orders.tmdl')
        _write(path, SAMPLE_TABLE_TMDL)
        result = _parse_tmdl_table(path)
        assert len(result['measures']) == 1
        assert result['measures'][0]['name'] == 'Total Sales'
        assert len(result['measures'][0]['expression_hash']) == 16

    def test_parse_table_partitions(self, tmp_dir):
        path = os.path.join(tmp_dir, 'Orders.tmdl')
        _write(path, SAMPLE_TABLE_TMDL)
        result = _parse_tmdl_table(path)
        assert len(result['partitions']) == 1
        assert result['partitions'][0]['name'] == 'Orders-partition'

    def test_parse_nonexistent_file(self):
        result = _parse_tmdl_table('/nonexistent/path.tmdl')
        assert result is None

    def test_parse_relationships(self, tmp_dir):
        path = os.path.join(tmp_dir, 'relationships.tmdl')
        _write(path, SAMPLE_RELATIONSHIPS)
        rels = _parse_relationships(path)
        assert len(rels) == 1
        assert rels[0]['name'] == 'rel-orders-customers'
        assert rels[0]['fromTable'] == 'Orders'
        assert rels[0]['toTable'] == 'Customers'

    def test_parse_relationships_nonexistent(self):
        rels = _parse_relationships('/nonexistent/path.tmdl')
        assert rels == []

    def test_parse_roles(self, tmp_dir):
        path = os.path.join(tmp_dir, 'roles.tmdl')
        _write(path, SAMPLE_ROLES)
        roles = _parse_roles(path)
        assert len(roles) == 1
        assert roles[0]['name'] == 'RegionFilter'
        assert len(roles[0]['content_hash']) == 16

    def test_parse_roles_nonexistent(self):
        roles = _parse_roles('/nonexistent/path.tmdl')
        assert roles == []


# ═══════════════════════════════════════════════════════════════════════
#  PBIR Parsing tests
# ═══════════════════════════════════════════════════════════════════════

class TestPBIRParsing:
    def test_load_json_valid(self, tmp_dir):
        path = os.path.join(tmp_dir, 'test.json')
        _write_json(path, {'key': 'value'})
        result = _load_json(path)
        assert result == {'key': 'value'}

    def test_load_json_invalid(self, tmp_dir):
        path = os.path.join(tmp_dir, 'bad.json')
        _write(path, 'not json{')
        assert _load_json(path) is None

    def test_load_json_missing(self):
        assert _load_json('/nonexistent.json') is None

    def test_load_pages_empty(self, tmp_dir):
        os.makedirs(os.path.join(tmp_dir, 'definition', 'pages'))
        pages = _load_pages(tmp_dir)
        assert pages == {}

    def test_load_pages_with_visuals(self, tmp_dir):
        report_dir = tmp_dir
        page_dir = os.path.join(report_dir, 'definition', 'pages', 'Page1')
        vis_dir = os.path.join(page_dir, 'visuals', 'vis-001')
        os.makedirs(vis_dir)
        _write_json(os.path.join(page_dir, 'page.json'), {
            'displayName': 'Sales Overview',
        })
        _write_json(os.path.join(vis_dir, 'visual.json'), {
            'visual': {'visualType': 'clusteredBarChart'},
        })
        pages = _load_pages(report_dir)
        assert 'Page1' in pages
        assert pages['Page1']['displayName'] == 'Sales Overview'
        assert pages['Page1']['visual_count'] == 1
        assert pages['Page1']['visuals'][0]['visualType'] == 'clusteredBarChart'

    def test_load_report_filters(self, tmp_dir):
        defn_dir = os.path.join(tmp_dir, 'definition')
        os.makedirs(defn_dir)
        _write_json(os.path.join(defn_dir, 'report.json'), {
            'filterConfig': {
                'filters': [
                    {'name': 'DateFilter', 'type': 'categorical'},
                    {'name': 'RegionFilter', 'type': 'range'},
                ]
            }
        })
        filters = _load_report_filters(tmp_dir)
        assert len(filters) == 2
        assert filters[0]['name'] == 'DateFilter'

    def test_load_report_filters_empty(self, tmp_dir):
        defn_dir = os.path.join(tmp_dir, 'definition')
        os.makedirs(defn_dir)
        _write_json(os.path.join(defn_dir, 'report.json'), {})
        filters = _load_report_filters(tmp_dir)
        assert filters == []


# ═══════════════════════════════════════════════════════════════════════
#  Diff Engine tests
# ═══════════════════════════════════════════════════════════════════════

class TestDiffTables:
    def test_added_table(self):
        old = {}
        new = {'Orders': {'name': 'Orders', 'columns': [], 'measures': [], 'partitions': []}}
        entries = _diff_tables(old, new)
        assert len(entries) == 1
        assert entries[0].change_type == ADDED
        assert entries[0].name == 'Orders'

    def test_removed_table(self):
        old = {'Orders': {'name': 'Orders', 'columns': [], 'measures': [], 'partitions': []}}
        new = {}
        entries = _diff_tables(old, new)
        assert len(entries) == 1
        assert entries[0].change_type == REMOVED

    def test_added_column(self):
        old = {'T': {'name': 'T', 'columns': [{'name': 'A'}], 'measures': [], 'partitions': []}}
        new = {'T': {'name': 'T', 'columns': [{'name': 'A'}, {'name': 'B'}], 'measures': [], 'partitions': []}}
        entries = _diff_tables(old, new)
        assert any(e.category == 'column' and e.change_type == ADDED and e.name == 'B' for e in entries)

    def test_removed_column(self):
        old = {'T': {'name': 'T', 'columns': [{'name': 'A'}, {'name': 'B'}], 'measures': [], 'partitions': []}}
        new = {'T': {'name': 'T', 'columns': [{'name': 'A'}], 'measures': [], 'partitions': []}}
        entries = _diff_tables(old, new)
        assert any(e.category == 'column' and e.change_type == REMOVED and e.name == 'B' for e in entries)

    def test_modified_column_datatype(self):
        old = {'T': {'name': 'T', 'columns': [{'name': 'A', 'dataType': 'int64'}],
                     'measures': [], 'partitions': []}}
        new = {'T': {'name': 'T', 'columns': [{'name': 'A', 'dataType': 'string'}],
                     'measures': [], 'partitions': []}}
        entries = _diff_tables(old, new)
        assert len(entries) == 1
        assert entries[0].change_type == MODIFIED
        assert 'dataType' in entries[0].detail

    def test_added_measure(self):
        old = {'T': {'name': 'T', 'columns': [],
                     'measures': [{'name': 'M1', 'expression_hash': 'aaa'}],
                     'partitions': []}}
        new = {'T': {'name': 'T', 'columns': [],
                     'measures': [{'name': 'M1', 'expression_hash': 'aaa'},
                                  {'name': 'M2', 'expression_hash': 'bbb'}],
                     'partitions': []}}
        entries = _diff_tables(old, new)
        assert any(e.category == 'measure' and e.change_type == ADDED and e.name == 'M2' for e in entries)

    def test_modified_measure(self):
        old = {'T': {'name': 'T', 'columns': [],
                     'measures': [{'name': 'M', 'expression_hash': 'aaa', 'expression': 'SUM(x)'}],
                     'partitions': []}}
        new = {'T': {'name': 'T', 'columns': [],
                     'measures': [{'name': 'M', 'expression_hash': 'bbb', 'expression': 'CALCULATE(SUM(x))'}],
                     'partitions': []}}
        entries = _diff_tables(old, new)
        assert len(entries) == 1
        assert entries[0].change_type == MODIFIED
        assert entries[0].before == 'SUM(x)'
        assert entries[0].after == 'CALCULATE(SUM(x))'

    def test_modified_partition(self):
        old = {'T': {'name': 'T', 'columns': [], 'measures': [],
                     'partitions': [{'name': 'P', 'content_hash': 'aaa'}]}}
        new = {'T': {'name': 'T', 'columns': [], 'measures': [],
                     'partitions': [{'name': 'P', 'content_hash': 'bbb'}]}}
        entries = _diff_tables(old, new)
        assert any(e.category == 'partition' and e.change_type == MODIFIED for e in entries)

    def test_no_changes(self):
        t = {'T': {'name': 'T', 'columns': [{'name': 'A'}],
                   'measures': [{'name': 'M', 'expression_hash': 'x'}],
                   'partitions': [{'name': 'P', 'content_hash': 'y'}]}}
        entries = _diff_tables(t, t)
        assert entries == []


class TestDiffRelationships:
    def test_added_relationship(self):
        old = []
        new = [{'name': 'R1', 'fromTable': 'A', 'signature': 'sig1'}]
        entries = _diff_relationships(old, new)
        assert len(entries) == 1
        assert entries[0].change_type == ADDED

    def test_removed_relationship(self):
        old = [{'name': 'R1', 'fromTable': 'A', 'signature': 'sig1'}]
        new = []
        entries = _diff_relationships(old, new)
        assert len(entries) == 1
        assert entries[0].change_type == REMOVED

    def test_modified_relationship(self):
        old = [{'name': 'R1', 'signature': 'sig1'}]
        new = [{'name': 'R1', 'signature': 'sig2'}]
        entries = _diff_relationships(old, new)
        assert entries[0].change_type == MODIFIED

    def test_no_changes(self):
        r = [{'name': 'R1', 'signature': 'sig1'}]
        assert _diff_relationships(r, r) == []


class TestDiffRoles:
    def test_added_role(self):
        entries = _diff_roles([], [{'name': 'Admin', 'content_hash': 'aaa'}])
        assert len(entries) == 1
        assert entries[0].change_type == ADDED

    def test_removed_role(self):
        entries = _diff_roles([{'name': 'Admin', 'content_hash': 'aaa'}], [])
        assert len(entries) == 1
        assert entries[0].change_type == REMOVED

    def test_modified_role(self):
        entries = _diff_roles(
            [{'name': 'R', 'content_hash': 'aaa'}],
            [{'name': 'R', 'content_hash': 'bbb'}],
        )
        assert entries[0].change_type == MODIFIED


class TestDiffPages:
    def _page(self, name, vis_count=0, page_type='', visuals=None):
        return {
            'displayName': name,
            'pageType': page_type,
            'visual_count': vis_count,
            'visuals': visuals or [],
        }

    def test_added_page(self):
        entries = _diff_pages({}, {'P1': self._page('Sales')})
        assert entries[0].change_type == ADDED

    def test_removed_page(self):
        entries = _diff_pages({'P1': self._page('Sales')}, {})
        assert entries[0].change_type == REMOVED

    def test_added_visual(self):
        old = {'P1': self._page('Sales', visuals=[
            {'name': 'v1', 'visualType': 'bar', 'field_count': 2},
        ])}
        new = {'P1': self._page('Sales', visuals=[
            {'name': 'v1', 'visualType': 'bar', 'field_count': 2},
            {'name': 'v2', 'visualType': 'line', 'title': 'Trend', 'field_count': 3},
        ])}
        entries = _diff_pages(old, new)
        vis_entries = [e for e in entries if e.category == 'visual']
        assert len(vis_entries) == 1
        assert vis_entries[0].change_type == ADDED

    def test_modified_visual_type(self):
        old = {'P1': self._page('Sales', visuals=[
            {'name': 'v1', 'visualType': 'bar', 'field_count': 2},
        ])}
        new = {'P1': self._page('Sales', visuals=[
            {'name': 'v1', 'visualType': 'line', 'field_count': 2},
        ])}
        entries = _diff_pages(old, new)
        vis_entries = [e for e in entries if e.category == 'visual']
        assert vis_entries[0].change_type == MODIFIED
        assert 'bar → line' in vis_entries[0].detail


class TestDiffFilters:
    def test_added_filter(self):
        entries = _diff_filters([], [{'name': 'F1', 'type': 'cat'}])
        assert entries[0].change_type == ADDED

    def test_removed_filter(self):
        entries = _diff_filters([{'name': 'F1', 'type': 'cat'}], [])
        assert entries[0].change_type == REMOVED

    def test_modified_filter_type(self):
        entries = _diff_filters(
            [{'name': 'F1', 'type': 'categorical'}],
            [{'name': 'F1', 'type': 'range'}],
        )
        assert entries[0].change_type == MODIFIED


# ═══════════════════════════════════════════════════════════════════════
#  Full project diff tests
# ═══════════════════════════════════════════════════════════════════════

class TestDiffProjects:
    def test_identical_projects(self, tmp_dir):
        tables = {'Orders': SAMPLE_TABLE_TMDL}
        p1 = _build_project(os.path.join(tmp_dir, 'a'), tables=tables)
        p2 = _build_project(os.path.join(tmp_dir, 'b'), tables=tables)
        report = diff_projects(p1, p2)
        assert not report.has_changes

    def test_table_added(self, tmp_dir):
        p1 = _build_project(os.path.join(tmp_dir, 'a'), tables={'Orders': SAMPLE_TABLE_TMDL})
        p2 = _build_project(os.path.join(tmp_dir, 'b'), tables={
            'Orders': SAMPLE_TABLE_TMDL,
            'Products': "table 'Products'\n\n\tcolumn 'ProductID'\n\t\tdataType: int64\n",
        })
        report = diff_projects(p1, p2)
        assert report.has_changes
        assert any(e.category == 'table' and e.change_type == ADDED for e in report.entries)

    def test_column_and_measure_changes(self, tmp_dir):
        p1 = _build_project(os.path.join(tmp_dir, 'a'), tables={'Orders': SAMPLE_TABLE_TMDL})
        p2 = _build_project(os.path.join(tmp_dir, 'b'), tables={'Orders': SAMPLE_TABLE_TMDL_V2})
        report = diff_projects(p1, p2)
        assert report.has_changes
        # Should detect: OrderID isHidden removed, Amount dataCategory added,
        # Region column added, Total Sales formula changed, Order Count added
        cats = [e.category for e in report.entries]
        assert 'column' in cats
        assert 'measure' in cats

    def test_relationship_changes(self, tmp_dir):
        p1 = _build_project(os.path.join(tmp_dir, 'a'), relationships=SAMPLE_RELATIONSHIPS)
        p2 = _build_project(os.path.join(tmp_dir, 'b'), relationships='')
        report = diff_projects(p1, p2)
        assert any(e.category == 'relationship' and e.change_type == REMOVED for e in report.entries)

    def test_page_and_visual_changes(self, tmp_dir):
        page_v1 = {
            'Page1': {
                'page_json': {'displayName': 'Dashboard'},
                'visuals': {
                    'v1': {'visual': {'visualType': 'bar'}},
                },
            },
        }
        page_v2 = {
            'Page1': {
                'page_json': {'displayName': 'Dashboard'},
                'visuals': {
                    'v1': {'visual': {'visualType': 'bar'}},
                    'v2': {'visual': {'visualType': 'line'}},
                },
            },
            'Page2': {
                'page_json': {'displayName': 'Details'},
                'visuals': {},
            },
        }
        p1 = _build_project(os.path.join(tmp_dir, 'a'), pages=page_v1)
        p2 = _build_project(os.path.join(tmp_dir, 'b'), pages=page_v2)
        report = diff_projects(p1, p2)
        assert any(e.category == 'page' and e.change_type == ADDED for e in report.entries)
        assert any(e.category == 'visual' and e.change_type == ADDED for e in report.entries)

    def test_report_paths_recorded(self, tmp_dir):
        p1 = _build_project(os.path.join(tmp_dir, 'a'))
        p2 = _build_project(os.path.join(tmp_dir, 'b'))
        report = diff_projects(p1, p2)
        assert report.old_path == p1
        assert report.new_path == p2


# ═══════════════════════════════════════════════════════════════════════
#  Baseline tests
# ═══════════════════════════════════════════════════════════════════════

class TestBaseline:
    def test_save_baseline(self, tmp_dir):
        proj = _build_project(os.path.join(tmp_dir, 'src'), tables={'T': SAMPLE_TABLE_TMDL})
        baseline_dir = os.path.join(tmp_dir, 'baseline')
        result = save_baseline(proj, baseline_dir)
        assert os.path.isdir(result)
        # Manifest should exist
        manifest_path = os.path.join(result, '.artifact_baseline')
        assert os.path.isfile(manifest_path)
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
        assert 'timestamp' in manifest
        assert manifest['version'] == '1.0'

    def test_check_baseline_pass(self, tmp_dir):
        tables = {'T': SAMPLE_TABLE_TMDL}
        proj = _build_project(os.path.join(tmp_dir, 'src'), tables=tables)
        baseline_dir = os.path.join(tmp_dir, 'baseline')
        save_baseline(proj, baseline_dir)
        # Compare identical
        passed, report = check_baseline(proj, baseline_dir)
        assert passed
        assert not report.has_changes

    def test_check_baseline_fail(self, tmp_dir):
        proj_v1 = _build_project(os.path.join(tmp_dir, 'v1'), tables={'T': SAMPLE_TABLE_TMDL})
        baseline_dir = os.path.join(tmp_dir, 'baseline')
        save_baseline(proj_v1, baseline_dir)
        # Build changed project
        proj_v2 = _build_project(os.path.join(tmp_dir, 'v2'), tables={'T': SAMPLE_TABLE_TMDL_V2})
        passed, report = check_baseline(proj_v2, baseline_dir)
        assert not passed
        assert report.has_changes

    def test_check_baseline_missing(self, tmp_dir):
        proj = _build_project(os.path.join(tmp_dir, 'src'))
        passed, report = check_baseline(proj, os.path.join(tmp_dir, 'no_baseline'))
        assert not passed

    def test_save_baseline_overwrites(self, tmp_dir):
        proj = _build_project(os.path.join(tmp_dir, 'src'), tables={'T': SAMPLE_TABLE_TMDL})
        baseline_dir = os.path.join(tmp_dir, 'baseline')
        save_baseline(proj, baseline_dir)
        # Second save should overwrite
        save_baseline(proj, baseline_dir)
        assert os.path.isdir(baseline_dir)


# ═══════════════════════════════════════════════════════════════════════
#  HTML Report tests
# ═══════════════════════════════════════════════════════════════════════

class TestHTMLReport:
    def test_no_changes_report(self):
        report = DiffReport()
        html = generate_diff_report(report)
        assert 'No Differences Detected' in html

    def test_changes_report(self, tmp_dir):
        entries = [
            DiffEntry('table', ADDED, 'Products', detail='5 columns'),
            DiffEntry('column', REMOVED, 'OldCol', parent='Orders'),
            DiffEntry('measure', MODIFIED, 'Sales', parent='Orders',
                      before='SUM(x)', after='CALCULATE(SUM(x))'),
        ]
        report = DiffReport(entries=entries, old_path='/old', new_path='/new')
        html_path = os.path.join(tmp_dir, 'report.html')
        html = generate_diff_report(report, html_path)
        assert os.path.isfile(html_path)
        assert 'ADDED' in html
        assert 'REMOVED' in html
        assert 'MODIFIED' in html
        assert 'Products' in html
        assert 'Formula Changes' in html

    def test_report_has_donut_chart(self):
        entries = [
            DiffEntry('table', ADDED, 'A'),
            DiffEntry('column', REMOVED, 'B', parent='A'),
        ]
        report = DiffReport(entries=entries)
        html = generate_diff_report(report)
        assert 'Changes by Category' in html

    def test_report_writes_to_file(self, tmp_dir):
        entries = [DiffEntry('filter', ADDED, 'F1')]
        report = DiffReport(entries=entries)
        out = os.path.join(tmp_dir, 'sub', 'diff.html')
        generate_diff_report(report, out)
        assert os.path.isfile(out)


# ═══════════════════════════════════════════════════════════════════════
#  Load Project tests
# ═══════════════════════════════════════════════════════════════════════

class TestLoadProject:
    def test_load_empty_project(self, tmp_dir):
        proj = _build_project(tmp_dir)
        data = load_project(proj)
        assert data['tables'] == {}
        assert data['relationships'] == []
        assert data['path'] == proj

    def test_load_with_tables(self, tmp_dir):
        proj = _build_project(tmp_dir, tables={'Orders': SAMPLE_TABLE_TMDL})
        data = load_project(proj)
        assert 'Orders' in data['tables']
        assert len(data['tables']['Orders']['columns']) == 2

    def test_load_with_relationships(self, tmp_dir):
        proj = _build_project(tmp_dir, relationships=SAMPLE_RELATIONSHIPS)
        data = load_project(proj)
        assert len(data['relationships']) == 1

    def test_load_with_roles(self, tmp_dir):
        proj = _build_project(tmp_dir, roles=SAMPLE_ROLES)
        data = load_project(proj)
        assert len(data['roles']) == 1

    def test_load_with_pages(self, tmp_dir):
        pages = {
            'Page1': {
                'page_json': {'displayName': 'Test'},
                'visuals': {'v1': {'visual': {'visualType': 'bar'}}},
            },
        }
        proj = _build_project(tmp_dir, pages=pages)
        data = load_project(proj)
        assert 'Page1' in data['pages']
        assert data['pages']['Page1']['visual_count'] == 1


# ═══════════════════════════════════════════════════════════════════════
#  CLI tests
# ═══════════════════════════════════════════════════════════════════════

class TestCLI:
    def test_main_entry_point(self, tmp_dir, monkeypatch):
        """Test the standalone CLI entry point."""
        p1 = _build_project(os.path.join(tmp_dir, 'a'), tables={'T': SAMPLE_TABLE_TMDL})
        p2 = _build_project(os.path.join(tmp_dir, 'b'), tables={'T': SAMPLE_TABLE_TMDL_V2})
        json_out = os.path.join(tmp_dir, 'out.json')
        html_out = os.path.join(tmp_dir, 'out.html')

        monkeypatch.setattr('sys.argv', [
            'artifact_diff', p1, p2,
            '--json', json_out,
            '--output', html_out,
        ])

        from powerbi_import.artifact_diff import main
        main()

        assert os.path.isfile(json_out)
        assert os.path.isfile(html_out)
        with open(json_out, 'r', encoding='utf-8') as f:
            data = json.load(f)
        assert data['has_changes'] is True
