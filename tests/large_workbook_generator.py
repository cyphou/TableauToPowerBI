"""Sprint 132.1 — Synthetic large-workbook TWB generator.

Produces realistic Tableau XML (.twb) files with configurable scale:
    - N datasources (with M tables, K columns each)
    - N worksheets (with field references, filters, mark encodings)
    - N dashboards (with worksheet refs, text, filter controls)
    - N calculations (Tableau formulas → DAX conversion targets)
    - N parameters (range and list types)
    - N sets, groups, bins, hierarchies

Reproducible via seed.  Used by perf benchmarks and memory-ceiling tests.

Usage:
    from tests.large_workbook_generator import generate_large_twb

    path = generate_large_twb(
        output_path='tests/fixtures/large_workbooks/large_500.twb',
        num_measures=500,
        num_worksheets=100,
        num_datasources=50,
        seed=42,
    )
"""

import os
import random
import string
import xml.etree.ElementTree as ET


# ── Helpers ──────────────────────────────────────────────────────────

def _rand_name(prefix, idx, rng):
    """Generate a plausible Tableau-style name."""
    suffixes = ['Sales', 'Revenue', 'Cost', 'Margin', 'Count', 'Avg',
                'Total', 'YTD', 'MTD', 'QTD', 'Ratio', 'Rate', 'Pct',
                'Delta', 'Growth', 'Index', 'Score', 'Rank', 'Cum']
    return f'{prefix}_{rng.choice(suffixes)}_{idx}'


def _rand_formula(col_names, rng):
    """Generate a random Tableau calculation formula."""
    templates = [
        'SUM([{col}])',
        'AVG([{col}])',
        'COUNTD([{col}])',
        'IF [Type] = "A" THEN [Sales] ELSE [Cost] END',
        'DATEDIFF("month", [Order Date], TODAY())',
        'ZN([{col}]) / ZN([{col2}])',
        'RUNNING_SUM(SUM([{col}]))',
        'RANK(SUM([{col}]))',
        'ROUND([{col}] * 100, 2)',
        'IF ISNULL([{col}]) THEN 0 ELSE [{col}] END',
        'CONTAINS([Category], "Tech")',
        'LEFT([Product Name], 5)',
        'DATETRUNC("quarter", [Order Date])',
    ]
    tmpl = rng.choice(templates)
    col = rng.choice(col_names) if col_names else 'Sales'
    col2 = rng.choice(col_names) if col_names else 'Cost'
    result = tmpl.format(col=col, col2=col2)
    # Occasionally wrap in FIXED LOD (can't use .format for these)
    if rng.random() < 0.15:
        result = '{{FIXED [Region] : {0}}}'.format(result)
    elif rng.random() < 0.1:
        result = 'WINDOW_SUM({0}, -2, 0)'.format(result)
    return result


def _rand_columns(n, rng):
    """Generate column names."""
    base = ['Sales', 'Cost', 'Profit', 'Quantity', 'Discount', 'Revenue',
            'Margin', 'Tax', 'Shipping', 'Returns', 'Budget', 'Forecast',
            'Actual', 'Variance', 'Target', 'Threshold', 'Score', 'Rating',
            'Price', 'Volume', 'Weight', 'Duration', 'Distance', 'Speed']
    dims = ['Region', 'Country', 'State', 'City', 'Category', 'Sub-Category',
            'Product Name', 'Customer Name', 'Segment', 'Market',
            'Order Date', 'Ship Date', 'Year', 'Quarter', 'Month']
    names = list(base) + list(dims)
    while len(names) < n:
        names.append(f'Col_{len(names)}_{rng.choice(string.ascii_uppercase)}')
    rng.shuffle(names)
    return names[:n]


# ── XML builders ─────────────────────────────────────────────────────

def _build_datasource(ds_name, tables, col_names, rng):
    """Build a <datasource> XML element."""
    ds = ET.Element('datasource', {
        'caption': ds_name,
        'inline': 'true',
        'name': ds_name.lower().replace(' ', '_'),
        'version': '18.1',
    })
    conn = ET.SubElement(ds, 'connection', {
        'class': 'sqlserver',
        'server': 'localhost',
        'dbname': 'SampleDB',
        'username': '',
    })
    for tbl_name, tbl_cols in tables:
        rel = ET.SubElement(conn, 'relation', {
            'name': tbl_name,
            'table': f'[dbo].[{tbl_name}]',
            'type': 'table',
        })
        cols_el = ET.SubElement(rel, 'columns')
        for col in tbl_cols:
            role = 'dimension' if any(d in col for d in ['Region', 'Category', 'Name', 'Date', 'Segment']) else 'measure'
            dtype = 'string' if role == 'dimension' else 'real'
            ET.SubElement(cols_el, 'column', {
                'caption': col,
                'datatype': dtype,
                'name': f'[{col}]',
                'role': role,
                'type': 'quantitative' if role == 'measure' else 'nominal',
            })
    return ds


def _build_worksheet(ws_name, ds_name, col_names, rng):
    """Build a <worksheet> XML element."""
    ws = ET.Element('worksheet', {'name': ws_name})
    table = ET.SubElement(ws, 'table')
    view = ET.SubElement(table, 'view')
    datasources = ET.SubElement(view, 'datasources')
    ET.SubElement(datasources, 'datasource', {
        'caption': ds_name,
        'name': ds_name.lower().replace(' ', '_'),
    })
    # Add field references (rows/columns/marks)
    used_cols = rng.sample(col_names, min(8, len(col_names)))
    rows = ET.SubElement(table, 'rows')
    rows.text = ' '.join(f'[{ds_name.lower().replace(" ", "_")}].[{c}]'
                         for c in used_cols[:2])
    cols = ET.SubElement(table, 'cols')
    cols.text = ' '.join(f'[{ds_name.lower().replace(" ", "_")}].[{c}]'
                         for c in used_cols[2:4])

    # Pane with mark encoding
    panes = ET.SubElement(view, 'panes')
    pane = ET.SubElement(panes, 'pane')
    mark = ET.SubElement(pane, 'mark', {'class': rng.choice([
        'Automatic', 'Bar', 'Line', 'Area', 'Circle', 'Square',
        'Text', 'Map', 'Pie',
    ])})
    for enc_type in ['color', 'size', 'label']:
        enc = ET.SubElement(pane, 'encodings')
        enc_el = ET.SubElement(enc, enc_type)
        if used_cols:
            enc_el.set('column',
                       f'[{ds_name.lower().replace(" ", "_")}].[{rng.choice(used_cols)}]')

    return ws


def _build_dashboard(db_name, ws_names, rng):
    """Build a <dashboard> XML element."""
    db = ET.Element('dashboard', {'name': db_name})
    size = ET.SubElement(db, 'size', {
        'maxheight': '1200',
        'maxwidth': '1600',
        'minheight': '800',
        'minwidth': '1200',
    })
    zones = ET.SubElement(db, 'zones')
    y = 0
    for ws in ws_names[:12]:  # max 12 worksheets per dashboard
        zone = ET.SubElement(zones, 'zone', {
            'h': '300',
            'w': '600',
            'x': str(rng.randint(0, 800)),
            'y': str(y),
            'id': str(rng.randint(1000, 9999)),
            'name': ws,
            'type-v2': 'viz',
        })
        y += 320
    # Add a text zone
    text_zone = ET.SubElement(zones, 'zone', {
        'type-v2': 'text',
        'h': '50', 'w': '600', 'x': '0', 'y': str(y),
    })
    text_run = ET.SubElement(text_zone, 'formatted-text')
    run = ET.SubElement(text_run, 'run')
    run.text = f'Dashboard: {db_name}'
    return db


def _build_calculation(calc_name, formula, ds_name):
    """Build a calculation dict (will be wrapped in XML)."""
    return {
        'name': calc_name,
        'formula': formula,
        'datasource': ds_name,
    }


# ── Main generator ──────────────────────────────────────────────────

def generate_large_twb(
    output_path,
    num_measures=500,
    num_worksheets=100,
    num_datasources=50,
    num_dashboards=20,
    num_parameters=30,
    num_sets=20,
    num_groups=15,
    num_bins=10,
    num_hierarchies=15,
    columns_per_table=25,
    tables_per_datasource=3,
    seed=42,
):
    """Generate a synthetic Tableau .twb file.

    Args:
        output_path: Where to write the .twb XML
        num_measures: Number of calculated fields (measures + calc columns)
        num_worksheets: Number of worksheet elements
        num_datasources: Number of datasource elements
        num_dashboards: Number of dashboard elements
        num_parameters: Number of parameter elements
        num_sets: Number of set elements
        num_groups: Number of group elements
        num_bins: Number of bin elements
        num_hierarchies: Number of hierarchy elements
        columns_per_table: Columns per relation table
        tables_per_datasource: Tables per datasource
        seed: Random seed for reproducibility

    Returns:
        str: path to the generated .twb file
    """
    rng = random.Random(seed)
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)

    root = ET.Element('workbook', {
        'source-build': '2024.3.0 (20243.24.0328.1512)',
        'source-platform': 'win',
        'version': '18.1',
        'xmlns:user': 'http://www.tableausoftware.com/xml/user',
    })

    # Datasources
    datasources_el = ET.SubElement(root, 'datasources')
    all_col_names = _rand_columns(columns_per_table * 3, rng)
    ds_infos = []  # (ds_name, [col_names])
    for i in range(num_datasources):
        ds_name = f'DataSource_{i}'
        tables = []
        ds_cols = []
        for t in range(tables_per_datasource):
            tbl_name = f'Table_{i}_{t}'
            cols = _rand_columns(columns_per_table, rng)
            tables.append((tbl_name, cols))
            ds_cols.extend(cols)
        ds_el = _build_datasource(ds_name, tables, ds_cols, rng)

        # Inject calculations into datasource
        num_calcs_here = num_measures // num_datasources
        for c in range(num_calcs_here):
            calc_idx = i * num_calcs_here + c
            calc_name = _rand_name('Calc', calc_idx, rng)
            formula = _rand_formula(ds_cols, rng)
            col_el = ET.SubElement(ds_el, 'column', {
                'caption': calc_name,
                'datatype': 'real',
                'name': f'[Calculation_{calc_idx}]',
                'role': 'measure',
                'type': 'quantitative',
            })
            calc_el = ET.SubElement(col_el, 'calculation', {
                'class': 'tableau',
                'formula': formula,
            })

        datasources_el.append(ds_el)
        ds_infos.append((ds_name, ds_cols))

    # Parameters (as a special datasource)
    if num_parameters > 0:
        params_ds = ET.SubElement(datasources_el, 'datasource', {
            'name': 'Parameters',
            'hasconnection': 'false',
            'inline': 'true',
        })
        for p in range(num_parameters):
            ptype = rng.choice(['integer', 'real', 'string'])
            col = ET.SubElement(params_ds, 'column', {
                'caption': f'Parameter_{p}',
                'datatype': ptype,
                'name': f'[Parameter {p}]',
                'param-domain-type': 'range' if ptype != 'string' else 'list',
                'role': 'measure',
                'type': 'quantitative',
                'value': str(rng.randint(1, 100)) if ptype != 'string' else '"Default"',
            })
            if ptype != 'string':
                rng_el = ET.SubElement(col, 'range', {
                    'granularity': '1',
                    'max': '1000',
                    'min': '0',
                })
            else:
                members = ET.SubElement(col, 'members')
                for m in range(5):
                    ET.SubElement(members, 'member', {'value': f'"Option_{m}"'})

    # Worksheets
    worksheets_el = ET.SubElement(root, 'worksheets')
    ws_names = []
    for i in range(num_worksheets):
        ws_name = f'Sheet_{i}'
        ws_names.append(ws_name)
        ds_name, ds_cols = rng.choice(ds_infos)
        ws_el = _build_worksheet(ws_name, ds_name, ds_cols, rng)
        worksheets_el.append(ws_el)

    # Dashboards
    dashboards_el = ET.SubElement(root, 'dashboards')
    for i in range(num_dashboards):
        db_name = f'Dashboard_{i}'
        db_ws = rng.sample(ws_names, min(12, len(ws_names)))
        db_el = _build_dashboard(db_name, db_ws, rng)
        dashboards_el.append(db_el)

    # Sets
    for ds_name, ds_cols in ds_infos[:num_sets]:
        for s_idx in range(min(2, num_sets)):
            set_el = ET.SubElement(datasources_el, 'set', {
                'name': f'Set_{ds_name}_{s_idx}',
                'datasource': ds_name.lower().replace(' ', '_'),
            })
            cond = ET.SubElement(set_el, 'condition')
            cond.text = f'[{rng.choice(ds_cols)}] > {rng.randint(50, 200)}'

    # Groups
    for g in range(num_groups):
        ds_name, ds_cols = rng.choice(ds_infos)
        group_el = ET.SubElement(datasources_el, 'group', {
            'name': f'Group_{g}',
            'name-style': 'unqualified',
            'user:auto-column': 'num_bin',
        })
        for gm in range(rng.randint(2, 5)):
            member = ET.SubElement(group_el, 'groupfilter', {
                'function': 'member',
                'level': f'[{rng.choice(ds_cols)}]',
                'member': f'Value_{gm}',
            })

    # Bins
    for b in range(num_bins):
        ds_name, ds_cols = rng.choice(ds_infos)
        bin_el = ET.SubElement(datasources_el, 'column', {
            'caption': f'Bin_{b}',
            'datatype': 'real',
            'name': f'[Bin_{b}]',
            'role': 'dimension',
            'type': 'ordinal',
            'user:auto-column': 'num_bin',
        })
        bin_calc = ET.SubElement(bin_el, 'calculation', {
            'class': 'bin',
            'formula': '',
            'peg': '0',
            'size': str(rng.choice([5, 10, 25, 50, 100])),
            'size-type': 'automatic',
        })

    # Hierarchies
    for h in range(num_hierarchies):
        ds_name, ds_cols = rng.choice(ds_infos)
        drill_path = ET.SubElement(datasources_el, 'drill-path', {
            'name': f'Hierarchy_{h}',
        })
        depth = rng.randint(2, 4)
        for level in range(depth):
            field = ET.SubElement(drill_path, 'field')
            field.text = f'[{rng.choice(ds_cols)}]'

    # Write
    tree = ET.ElementTree(root)
    ET.indent(tree, space='  ')
    tree.write(output_path, encoding='unicode', xml_declaration=True)

    return output_path


def get_twb_stats(twb_path):
    """Return basic stats about a generated TWB file."""
    tree = ET.parse(twb_path)
    root = tree.getroot()
    return {
        'file_size_bytes': os.path.getsize(twb_path),
        'datasources': len(root.findall('.//datasource')),
        'worksheets': len(root.findall('.//worksheet')),
        'dashboards': len(root.findall('.//dashboard')),
        'calculations': len(root.findall('.//calculation[@formula]')),
        'columns': len(root.findall('.//column')),
        'parameters': len(root.findall('.//column[@param-domain-type]')),
    }


if __name__ == '__main__':
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else 'tests/fixtures/large_workbooks/large_500.twb'
    path = generate_large_twb(out)
    stats = get_twb_stats(path)
    print(f'Generated: {path}')
    for k, v in stats.items():
        print(f'  {k}: {v}')
