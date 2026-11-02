"""Side-by-side comparison report — Tableau vs. Power BI.

Generates an HTML report that shows each Tableau worksheet alongside
the corresponding Power BI visual definition, highlighting:

* Visual type mapping
* DAX formula conversions
* Filter mapping
* Data model differences (columns, measures, relationships)

Usage::

    python -m powerbi_import.comparison_report \\
        tableau_export/ artifacts/powerbi_projects/MyProject/ \\
        --output comparison.html
"""

import argparse
import glob
import html as html_mod
import json
import os

from powerbi_import.report_scoring import migration_classification_score

try:
    from powerbi_import.html_template import get_report_css, get_report_js
except ImportError:
    from html_template import get_report_css, get_report_js


# ────────────────────────────────────────────────────────
# CSS Theme extras (comparison-specific)
# ────────────────────────────────────────────────────────

_CSS_EXTRA = """
/* comparison-report extras */
.comparison { background: var(--surface); border-radius: 8px; margin-bottom: 1rem;
              box-shadow: 0 1px 3px rgba(0,0,0,0.12); overflow: hidden; }
.comparison .row-header { background: var(--pbi-light-blue); padding: 0.75rem 1rem;
                          font-weight: 600; display: flex; justify-content: space-between; }
.comparison .row-header .badge { background: var(--success); color: #fff; padding: 2px 8px;
                                  border-radius: 4px; font-size: 0.75rem; }
.comparison .row-header .badge.warn { background: #ca5010; }
.comparison .row-header .badge.fail { background: var(--fail); }
.cols { display: grid; grid-template-columns: 1fr 1fr; }
.col { padding: 1rem; border-top: 1px solid #e0e0e0; }
.col:first-child { border-right: 1px solid #e0e0e0; }
.col h4 { margin: 0 0 0.5rem; color: var(--pbi-blue); font-size: 0.8rem; text-transform: uppercase; }
pre { background: var(--pbi-bg); padding: 0.5rem; border-radius: 4px; overflow-x: auto;
      font-size: 0.82rem; margin: 0.3rem 0; white-space: pre-wrap; }
.label { font-weight: 600; color: #555; font-size: 0.8rem; }
.pass { color: var(--success); } .warn { color: #ca5010; } .fail { color: var(--fail); }
.summary { display: flex; gap: 1rem; margin-bottom: 1.5rem; flex-wrap: wrap; }
.summary .card { flex: 1; min-width: 200px; }
"""


# ────────────────────────────────────────────────────────
# Data loaders
# ────────────────────────────────────────────────────────

def _load_json(path):
    """Load a JSON file, returning empty dict/list on failure."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _load_extracted(extract_dir):
    """Load all 16 extracted JSON files from the extraction directory."""
    data = {}
    names = [
        'worksheets', 'dashboards', 'datasources', 'calculations',
        'parameters', 'filters', 'stories', 'actions', 'sets', 'groups',
        'bins', 'hierarchies', 'sort_orders', 'aliases', 'custom_sql',
        'user_filters',
    ]
    for name in names:
        path = os.path.join(extract_dir, f'{name}.json')
        data[name] = _load_json(path)
    return data


def _load_pbip(pbip_dir):
    """Load Power BI project artifacts from a .pbip directory."""
    result = {'pages': [], 'model': {}, 'report': {}}
    # Find report.json
    for root, dirs, files in os.walk(pbip_dir):
        for f in files:
            full = os.path.join(root, f)
            if f == 'visual.json':
                result.setdefault('visuals', []).append(
                    {'path': full, 'data': _load_json(full)}
                )
            elif f == 'report.json':
                result['report'] = _load_json(full)
            elif f.endswith('.json') and 'page' in root.lower():
                result['pages'].append({'path': full, 'data': _load_json(full)})
    # Load migration report if present
    reports = glob.glob(os.path.join(pbip_dir, '..', 'migration_report_*.json'))
    if not reports:
        # Check new layout: reports/ sibling directory of migrated/
        reports = glob.glob(os.path.join(pbip_dir, '..', '..', 'reports', 'migration_report_*.json'))
    if reports:
        result['migration_report'] = _load_json(sorted(reports)[-1])
    return result


def _visual_payload(record):
    """Unwrap PBIR visual.json records while accepting older flat fixtures."""
    data = record.get('data', record) if isinstance(record, dict) else {}
    visual = data.get('visual', data) if isinstance(data, dict) else {}
    return visual if isinstance(visual, dict) else {}


def _visual_title(record):
    visual = _visual_payload(record)
    title = visual.get('title', '')
    if isinstance(title, dict):
        title = title.get('text', '')
    if title:
        return str(title).strip()

    objects = visual.get('visualContainerObjects', visual.get('vcObjects', {}))
    for item in (objects.get('title', []) if isinstance(objects, dict) else []):
        properties = item.get('properties', {}) if isinstance(item, dict) else {}
        text = properties.get('text', {}) if isinstance(properties, dict) else {}
        if isinstance(text, dict):
            literal = text.get('expr', {}).get('Literal', {})
            text = literal.get('Value', '') if isinstance(literal, dict) else ''
        if text:
            return str(text).strip("'\"")
    return ''


def _visual_type(record):
    return _visual_payload(record).get('visualType', 'N/A')


def _projection_field_name(field):
    """Extract a projected property name from common PBIR query expressions."""
    if not isinstance(field, dict):
        return ''
    for expression_name in ('Aggregation', 'Column', 'Measure'):
        expression = field.get(expression_name)
        if not isinstance(expression, dict):
            continue
        nested = expression.get('Expression', {})
        if isinstance(nested, dict):
            expression = (
                nested if nested.get('Property') else
                nested.get('Column', nested.get('Measure', expression))
            )
        if isinstance(expression, dict) and expression.get('Property'):
            return str(expression['Property'])
    return ''


# ────────────────────────────────────────────────────────
# Comparison logic
# ────────────────────────────────────────────────────────

def _compare_worksheets(extracted, pbip_data):
    """Compare Tableau worksheets to PBI pages/visuals."""
    comparisons = []
    worksheets = extracted.get('worksheets', [])
    if isinstance(worksheets, dict):
        worksheets = worksheets.get('worksheets', [])

    pbi_visuals = pbip_data.get('visuals', [])
    used_indices = set()

    for ws in worksheets:
        name = ws.get('name', 'Unknown')
        # `chart_type` is what the extractor writes; `mark_type` never appears in
        # a real extraction and stayed here as the primary, so every worksheet
        # fell through to the mark-encoding type.
        tab_type = (ws.get('chart_type')
                    or ws.get('mark_type')
                    or ws.get('mark_encoding', {}).get('type', 'auto'))
        tab_fields = ws.get('fields', [])
        tab_filters = ws.get('filters', [])

        # A title is only a heuristic candidate, never proof of equivalence.
        candidates = [
            i for i, visual in enumerate(pbi_visuals)
            if i not in used_indices and _visual_title(visual)
            and (name.casefold() in _visual_title(visual).casefold()
                 or _visual_title(visual).casefold() in name.casefold())
        ]
        pbi_match = pbi_visuals[candidates[0]] if len(candidates) == 1 else None
        if pbi_match:
            used_indices.add(candidates[0])

        comparisons.append({
            'name': name,
            'tableau': {
                'mark_type': tab_type,
                'field_count': len(tab_fields),
                'fields': tab_fields[:10],
                'filter_count': len(tab_filters),
            },
            'powerbi': {
                'visual_type': _visual_type(pbi_match) if pbi_match else 'N/A',
                'matched': pbi_match is not None,
                'artifact_path': pbi_match.get('path') if pbi_match else None,
            },
            'status': 'candidate' if pbi_match else 'not_run',
        })
    return comparisons


def _compare_calculations(extracted, pbip_data):
    """Compare Tableau calculations to PBI DAX measures/columns."""
    calcs = extracted.get('calculations', [])
    if isinstance(calcs, dict):
        calcs = calcs.get('calculations', [])

    results = []
    for calc in calcs[:50]:  # Limit for report size
        name = calc.get('name', calc.get('caption', ''))
        formula = calc.get('formula', '')
        role = calc.get('role', '')
        results.append({
            'name': name,
            'tableau_formula': formula,
            'role': role,
        })
    return results


def _compare_field_bindings(extracted, pbip_dir):
    """Compare Tableau worksheet field bindings against PBI visual fields.

    Returns a list of per-worksheet results with match status and field details.
    """
    worksheets = extracted.get('worksheets', [])
    if isinstance(worksheets, dict):
        worksheets = worksheets.get('worksheets', [])

    # ── Load PBI visuals from disk ──
    pbi_visuals = []
    vis_pattern = os.path.join(pbip_dir, '**', 'visual.json')
    for filepath in sorted(glob.glob(vis_pattern, recursive=True)):
        if os.path.basename(filepath) != 'visual.json':
            continue
        data = _load_json(filepath)
        vis = _visual_payload({'data': data})
        vtype = vis.get('visualType', 'unknown')
        title = _visual_title({'data': data})
        # Extract fields from queryState
        qs = vis.get('query', {}).get('queryState', {})
        fields = []
        for role_data in qs.values():
            if not isinstance(role_data, dict):
                continue
            for proj in role_data.get('projections', []):
                fi = proj.get('field', {}) if isinstance(proj, dict) else {}
                prop = _projection_field_name(fi)
                if prop:
                    fields.append(prop)
        pbi_visuals.append({
            'type': vtype,
            'title': title,
            'fields': fields,
            'is_empty': len(fields) == 0,
            'path': filepath,
        })

    # ── Generated fields to ignore ──
    _GENERATED = {'Latitude (generated)', 'Longitude (generated)',
                  'Number of Records', 'Multiple Values'}

    results = []
    used_indices = set()

    for ws in worksheets:
        ws_name = ws.get('name', '')
        ws_raw_fields = ws.get('fields', [])
        ws_field_names = set()
        for f in ws_raw_fields:
            name = f.get('name', '') if isinstance(f, dict) else str(f)
            if name and name not in _GENERATED:
                ws_field_names.add(name)
        ws_is_empty = len(ws_field_names) == 0

        # Prefer a unique title candidate. Otherwise field overlap is only a
        # heuristic, and is never promoted to a parity verdict.
        best_idx, best_score = None, -1
        title_candidates = [
            i for i, visual in enumerate(pbi_visuals)
            if i not in used_indices and visual['title']
            and visual['title'].casefold() == ws_name.casefold()
        ]
        if len(title_candidates) == 1:
            best_idx = title_candidates[0]
        else:
            tied = False
            best_score = 0
            for i, v in enumerate(pbi_visuals):
                if i in used_indices:
                    continue
                pbi_set = set(v['fields'])
                overlap = len(ws_field_names & pbi_set)
                union = len(ws_field_names | pbi_set)
                score = overlap / union if union else 0
                if score > best_score:
                    best_score, best_idx, tied = score, i, False
                elif score == best_score and score > 0:
                    tied = True
            if tied:
                best_idx = None

        pbi = pbi_visuals[best_idx] if best_idx is not None else None
        if pbi:
            used_indices.add(best_idx)

        pbi_field_names = set(pbi['fields']) if pbi else set()
        matched = ws_field_names & pbi_field_names
        missing = ws_field_names - pbi_field_names
        extra = pbi_field_names - ws_field_names

        if not pbi and not pbi_visuals:
            status = 'NO_VISUAL'
        elif not pbi:
            status = 'UNVERIFIED'
        elif ws_is_empty:
            status = 'UNVERIFIED'
        elif missing and not extra:
            status = 'MISSING'
        elif missing and extra:
            status = 'PARTIAL'
        elif extra and not missing:
            status = 'EXTRA'
        else:
            status = 'OK'

        results.append({
            'worksheet': ws_name,
            'visual_type': pbi['type'] if pbi else None,
            'status': status,
            'matched': sorted(matched),
            'missing': sorted(missing),
            'extra': sorted(extra),
            'tab_count': len(ws_field_names),
            'pbi_count': len(pbi_field_names),
            'match_method': (
                'title' if pbi and pbi['title'].casefold() == ws_name.casefold()
                else 'field_overlap' if pbi else None
            ),
            'artifact_path': pbi.get('path') if pbi else None,
            'generated_visual_artifact_count': len(pbi_visuals),
        })

    return results


def _compare_datasources(extracted):
    """Summarize datasource comparison."""
    ds = extracted.get('datasources', [])
    if isinstance(ds, dict):
        ds = ds.get('datasources', [])
    summary = []
    for d in ds:
        name = d.get('name', d.get('caption', ''))
        conn = d.get('connection', {})
        tables = d.get('tables', [])
        summary.append({
            'name': name,
            'type': conn.get('class', conn.get('type', 'unknown')),
            'table_count': len(tables),
            'column_count': sum(len(t.get('columns', [])) for t in tables),
        })
    return summary


# ────────────────────────────────────────────────────────
# HTML generation
# ────────────────────────────────────────────────────────

def _esc(text):
    """HTML-escape text."""
    return html_mod.escape(str(text)) if text else ''


def generate_comparison_report(extract_dir, pbip_dir, output_path=None):
    """Generate an HTML comparison report.

    Args:
        extract_dir: Path to the tableau_export/ directory with JSON files.
        pbip_dir: Path to the generated .pbip project directory.
        output_path: Output HTML file path (default: comparison_report.html
                     in the pbip directory's parent).

    Returns:
        str: Path to the generated HTML file.
    """
    extracted = _load_extracted(extract_dir)
    pbip_data = _load_pbip(pbip_dir)

    ws_compare = _compare_worksheets(extracted, pbip_data)
    calc_compare = _compare_calculations(extracted, pbip_data)
    ds_compare = _compare_datasources(extracted)
    field_compare = _compare_field_bindings(extracted, pbip_dir)
    migration_report = pbip_data.get('migration_report', {})

    # Counts
    ws_total = len(ws_compare)
    ws_matched = sum(1 for w in ws_compare if w['status'] == 'candidate')
    calc_total = len(calc_compare)
    ds_total = len(ds_compare)
    field_name_matches = sum(1 for f in field_compare if f['status'] == 'OK')
    visual_artifact_count = len(pbip_data.get('visuals', []))
    visual_artifact_count_delta = visual_artifact_count - ws_total
    migration_items = migration_report.get('items', [])
    has_visual_migration_items = any(
        str(item.get('category') or item.get('type') or '').casefold() == 'visual'
        for item in migration_items if isinstance(item, dict)
    )

    if output_path is None:
        output_path = os.path.join(os.path.dirname(pbip_dir), 'comparison_report.html')

    parts = [f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Migration Comparison Report</title>
<style>{get_report_css()}{_CSS_EXTRA}</style>
</head>
<body>
<div class="report-header">
<h1>Tableau &rarr; Power BI &mdash; Side-by-Side Comparison</h1>
<p>Extract: {_esc(extract_dir)} | Project: {_esc(pbip_dir)}</p>
</div>
<div class="container">
<p><strong>Limitations:</strong> title and field-name matching select heuristic
candidates. They do not verify worksheet provenance, target bindings, data
values, or rendered visual parity.</p>
<p><strong>Artifact reconciliation:</strong> {ws_total} source worksheet(s)
versus {visual_artifact_count} generated PBIR <code>visual.json</code>
artifact(s), count difference {visual_artifact_count_delta:+d}. Counts are
shown independently because a one-to-one worksheet binding has not been
verified.</p>
"""]

    # Summary cards
    score = migration_classification_score(migration_report)
    fidelity = f'{score:g}%' if score is not None else 'not_run'
    if ws_total or has_visual_migration_items:
        # A legacy aggregate score cannot establish the visual component when
        # worksheet-to-PBIR provenance and rendered parity are unverified.
        fidelity = 'not_run (visual parity unverified)'
    parts.append(f"""
<div class="summary">
  <div class="card"><h3>Worksheets</h3><div class="val">{ws_total}</div></div>
  <div class="card"><h3>Generated PBIR Visual Artifacts</h3><div class="val">{visual_artifact_count}</div></div>
  <div class="card"><h3>Title-matched Candidates</h3><div class="val warn">{ws_matched}</div></div>
  <div class="card"><h3>Equal-name Candidates</h3><div class="val warn">{field_name_matches}</div></div>
  <div class="card"><h3>Calculations</h3><div class="val">{calc_total}</div></div>
  <div class="card"><h3>Datasources</h3><div class="val">{ds_total}</div></div>
  <div class="card"><h3>Reported classification score</h3><div class="val">{_esc(str(fidelity))}</div></div>
</div>
""")

    # ── Worksheet comparison ──
    parts.append('<h2>Worksheet → Visual Mapping</h2>')
    for ws in ws_compare:
        badge_cls = 'warn'
        badge_txt = 'Title candidate' if ws['status'] == 'candidate' else 'No verified candidate'
        parts.append(f"""
<div class="comparison">
  <div class="row-header">
    <span>{_esc(ws['name'])}</span>
    <span class="badge {badge_cls}">{badge_txt}</span>
  </div>
  <div class="cols">
    <div class="col">
      <h4>Tableau</h4>
      <p><span class="label">Mark type:</span> {_esc(ws['tableau']['mark_type'])}</p>
      <p><span class="label">Fields:</span> {ws['tableau']['field_count']}</p>
      <p><span class="label">Filters:</span> {ws['tableau']['filter_count']}</p>
    </div>
    <div class="col">
      <h4>Power BI</h4>
      <p><span class="label">Visual type:</span> {_esc(ws['powerbi']['visual_type'])}</p>
    </div>
  </div>
</div>""")

    # ── Candidate field-name comparison ──
    if field_compare:
        parts.append('<h2>Heuristic Candidate Field-Name Comparison</h2>')
        parts.append('<table><tr>'
                     '<th>Worksheet</th><th>Visual Type</th><th>Status</th>'
                     '<th>Tableau</th><th>PBI</th><th>Matched</th>'
                     '<th>Missing in PBI</th><th>Extra in PBI</th></tr>')
        _STATUS_BADGE = {
            'OK': ('warn', 'Names match'), 'EMPTY': ('', 'No source names'),
            'EXTRA': ('warn', 'Extra candidate names'), 'MISSING': ('fail', 'Missing'),
            'PARTIAL': ('warn', 'Partial'), 'NO_VISUAL': ('fail', 'No Visual'),
            'UNVERIFIED': ('warn', 'Unverified'),
        }
        for fc in field_compare:
            cls, label = _STATUS_BADGE.get(fc['status'], ('', fc['status']))
            missing_str = ', '.join(fc['missing']) if fc['missing'] else '—'
            extra_str = ', '.join(fc['extra']) if fc['extra'] else '—'
            parts.append(
                f"<tr><td>{_esc(fc['worksheet'])}</td>"
                f"<td>{_esc(fc['visual_type'] or 'N/A')}</td>"
                f"<td class=\"{cls}\">{_esc(label)}</td>"
                f"<td>{fc['tab_count']}</td><td>{fc['pbi_count']}</td>"
                f"<td>{len(fc['matched'])}</td>"
                f"<td>{_esc(missing_str)}</td>"
                f"<td>{_esc(extra_str)}</td></tr>"
            )
        parts.append('</table>')

    # ── Calculation comparison ──
    if calc_compare:
        parts.append('<h2>Calculation Conversions</h2>')
        parts.append('<table><tr><th>Name</th><th>Tableau Formula</th><th>Role</th></tr>')
        for c in calc_compare:
            parts.append(
                f"<tr><td>{_esc(c['name'])}</td>"
                f"<td><pre>{_esc(c['tableau_formula'])}</pre></td>"
                f"<td>{_esc(c['role'])}</td></tr>"
            )
        parts.append('</table>')

    # ── Datasource comparison ──
    if ds_compare:
        parts.append('<h2>Datasource Summary</h2>')
        parts.append('<table><tr><th>Name</th><th>Type</th><th>Tables</th><th>Columns</th></tr>')
        for d in ds_compare:
            parts.append(
                f"<tr><td>{_esc(d['name'])}</td><td>{_esc(d['type'])}</td>"
                f"<td>{d['table_count']}</td><td>{d['column_count']}</td></tr>"
            )
        parts.append('</table>')

    # ── Migration report items ──
    items = migration_items
    if items:
        parts.append('<h2>Migration Item Status</h2>')
        parts.append('<table><tr><th>Item</th><th>Type</th><th>Status</th><th>Notes</th></tr>')
        for item in items[:100]:
            status = item.get('status', '')
            item_category = item.get('type') or item.get('category', '')
            note = item.get('notes') or item.get('note', '')
            if str(item_category).casefold() == 'visual' and status == 'exact':
                status = 'unverified'
                note = '; '.join(filter(None, (
                    note, 'PBIR visual projection and rendered parity not verified'
                )))
            cls = ('pass' if status in ('migrated', 'converted', 'exact') else
                   'warn' if status in ('partial', 'approximate', 'unverified', 'skipped', 'placeholder')
                   else 'fail')
            parts.append(
                f"<tr><td>{_esc(item.get('name', ''))}</td>"
                f"<td>{_esc(item_category)}</td>"
                f"<td class=\"{cls}\">{_esc(status)}</td>"
                f"<td>{_esc(note)}</td></tr>"
            )
        parts.append('</table>')

    parts.append(f'</div><script>{get_report_js()}</script></body></html>')

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(parts))
    print(f"  ✓ Comparison report: {output_path}")
    return output_path


# ────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Generate a side-by-side Tableau vs Power BI comparison report'
    )
    parser.add_argument('extract_dir', help='Path to tableau_export/ directory')
    parser.add_argument('pbip_dir', help='Path to generated .pbip project directory')
    parser.add_argument(
        '--output', '-o', default=None,
        help='Output HTML file path (default: comparison_report.html in pbip parent)'
    )
    args = parser.parse_args()
    generate_comparison_report(args.extract_dir, args.pbip_dir, args.output)


if __name__ == '__main__':
    main()
