"""
Notebook-based interactive migration API for Jupyter environments.

Provides a stateful ``MigrationSession`` class for cell-by-cell migration
control, inline DAX/M editing, visual preview, and notebook generation.

Usage in a Jupyter notebook::

    from powerbi_import.notebook_api import MigrationSession

    session = MigrationSession()
    session.load('path/to/workbook.twbx')
    session.assess()
    session.preview_dax()
    session.edit_dax('Total Sales', 'SUM(Sales[Amount])')
    session.generate(output_dir='/tmp/pbi_output')
    session.validate()
"""

import copy
import json
import logging
import os
import re
import sys

logger = logging.getLogger(__name__)


class MigrationSession:
    """Stateful migration session for interactive notebook use.

    Maintains extraction results, DAX/visual overrides, and configuration
    across Jupyter cells.  Methods return plain dicts/lists so that
    ``pandas.DataFrame(result)`` works seamlessly when pandas is available.
    """

    def __init__(self):
        self._workbook_path = None
        self._extracted = None          # dict of 16 JSON object types
        self._converted_objects = None  # post-conversion model dict
        self._assessment = None
        self._dax_overrides = {}        # measure_name → new_formula
        self._visual_overrides = {}     # visual_name → new_visual_type
        self._config = {
            'calendar_start': 2020,
            'calendar_end': 2030,
            'culture': 'en-US',
            'mode': 'import',
            'languages': [],
            'goals': False,
        }
        self._generated_path = None
        self._validation_result = None

    # ── Loading ───────────────────────────────────────────────

    def load(self, workbook_path):
        """Extract a Tableau workbook into the session.

        Args:
            workbook_path: Path to ``.twb`` or ``.twbx`` file.

        Returns:
            dict: Summary of extracted objects (counts per type).
        """
        # Add extraction module to path if needed
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        extract_dir = os.path.join(base, 'tableau_export')
        if extract_dir not in sys.path:
            sys.path.insert(0, extract_dir)

        from tableau_export.extract_tableau_data import TableauExtractor

        self._workbook_path = os.path.abspath(workbook_path)
        extractor = TableauExtractor(self._workbook_path)
        self._extracted = extractor.extract_all()

        summary = {}
        for key, value in self._extracted.items():
            if isinstance(value, list):
                summary[key] = len(value)
            elif isinstance(value, dict):
                summary[key] = len(value)
            else:
                summary[key] = 1
        logger.info("Loaded %s — %d object types extracted", workbook_path,
                     len(summary))
        return summary

    # ── Assessment ────────────────────────────────────────────

    def assess(self):
        """Run pre-migration assessment on extracted data.

        Returns:
            dict: Assessment report with per-category scores.
        """
        self._require_loaded()

        from powerbi_import.assessment import run_assessment

        workbook_name = (self._config.get('workbook_name')
                         or self._extracted.get('workbook_name')
                         or 'Workbook')
        report = run_assessment(self._extracted, workbook_name=workbook_name)
        self._assessment = report.to_dict()
        return self._assessment

    # ── DAX Preview & Editing ─────────────────────────────────

    def preview_dax(self):
        """Preview DAX conversions for all calculations.

        Returns:
            list[dict]: Per-calculation Tableau→DAX mapping with accuracy.
        """
        self._require_loaded()

        from tableau_export.dax_converter import convert_tableau_formula_to_dax

        calculations = self._extracted.get('calculations', [])
        datasources = self._extracted.get('datasources', [])
        # Build column_table_map from datasources
        col_table_map = {}
        for ds in datasources:
            for tbl in ds.get('tables', []):
                tname = tbl.get('name', '')
                for col in tbl.get('columns', []):
                    cname = col.get('name', '')
                    if cname:
                        col_table_map[cname] = tname

        results = []
        for calc in calculations:
            name = calc.get('name', calc.get('caption', ''))
            formula = calc.get('formula', '')
            if not formula:
                continue
            dax = convert_tableau_formula_to_dax(
                formula, column_table_map=col_table_map
            )
            # Check for overrides
            if name in self._dax_overrides:
                dax = self._dax_overrides[name]
                status = 'overridden'
            elif any(kw in dax.lower() for kw in ('blank(', '0 /* ', 'todo', '/* no dax')):
                status = 'approximated'
            else:
                status = 'exact'

            results.append({
                'name': name,
                'tableau_formula': formula,
                'dax_formula': dax,
                'status': status,
            })
        return results

    def list_approximated(self):
        """List all calculations with approximated or placeholder DAX.

        Returns:
            list[dict]: Measures/columns needing manual review.
        """
        previews = self.preview_dax()
        return [p for p in previews if p['status'] == 'approximated']

    def edit_dax(self, measure_name, new_formula):
        """Override a DAX formula for a specific measure or calculation.

        Args:
            measure_name: Name of the measure/calculation to override.
            new_formula: New DAX expression.
        """
        self._dax_overrides[measure_name] = new_formula
        logger.info("DAX override set: %s", measure_name)

    def clear_dax_override(self, measure_name):
        """Remove a DAX override, reverting to auto-converted formula.

        Args:
            measure_name: Name of the measure/calculation.
        """
        self._dax_overrides.pop(measure_name, None)

    def get_dax_overrides(self):
        """Return all active DAX overrides.

        Returns:
            dict: measure_name → formula.
        """
        return dict(self._dax_overrides)

    # ── M Query Preview ───────────────────────────────────────

    def preview_m(self):
        """Preview Power Query M expressions for all datasources.

        Returns:
            list[dict]: Per-table M query preview.
        """
        self._require_loaded()

        from tableau_export.m_query_builder import generate_power_query_m

        datasources = self._extracted.get('datasources', [])
        results = []
        for ds in datasources:
            conn = ds.get('connection', ds.get('connection_map', {}))
            for tbl in ds.get('tables', []):
                tname = tbl.get('name', '')
                try:
                    m_expr = generate_power_query_m(conn, tbl)
                except Exception as exc:  # noqa: BLE001 — preview-only fallback
                    logger.warning("M preview generation failed for table %r: %s", tname, exc)
                    m_expr = f'// Failed to generate M for {tname}: {exc}'
                results.append({
                    'table': tname,
                    'datasource': ds.get('name', ''),
                    'connection_type': conn.get('class', conn.get('type', 'unknown')),
                    'm_expression': m_expr,
                })
        return results

    # ── Visual Preview & Overrides ────────────────────────────

    def preview_visuals(self):
        """Preview Tableau→PBI visual type mappings.

        Returns:
            list[dict]: Per-visual mapping with data role coverage.
        """
        self._require_loaded()

        from powerbi_import.visual_generator import resolve_visual_type

        worksheets = self._extracted.get('worksheets', [])
        results = []
        for ws in worksheets:
            ws_name = ws.get('name', '')
            mark = ws.get('mark_type', ws.get('type', 'automatic'))

            # Check override
            if ws_name in self._visual_overrides:
                pbi_type = self._visual_overrides[ws_name]
                override = True
            else:
                pbi_type = resolve_visual_type(mark)
                override = False

            fields = ws.get('fields', [])
            results.append({
                'worksheet': ws_name,
                'tableau_mark': mark,
                'pbi_visual_type': pbi_type,
                'field_count': len(fields),
                'overridden': override,
            })
        return results

    def override_visual_type(self, visual_name, new_type):
        """Override the PBI visual type for a specific worksheet.

        Args:
            visual_name: Tableau worksheet name.
            new_type: PBI visual type string (e.g., 'lineChart').
        """
        self._visual_overrides[visual_name] = new_type
        logger.info("Visual override set: %s → %s", visual_name, new_type)

    # ── Configuration ─────────────────────────────────────────

    def configure(self, **options):
        """Update migration configuration options.

        Args:
            **options: Any of calendar_start, calendar_end, culture,
                       mode, languages, goals.

        Returns:
            dict: Updated configuration.
        """
        for key, value in options.items():
            if key in self._config:
                self._config[key] = value
            else:
                logger.warning("Unknown config option: %s", key)
        return dict(self._config)

    def get_config(self):
        """Return current migration configuration."""
        return dict(self._config)

    # ── Generation ────────────────────────────────────────────

    def generate(self, output_dir=None):
        """Generate the .pbip project from extracted + overridden data.

        Args:
            output_dir: Output directory (default: temp dir).

        Returns:
            dict: Generation summary (path, table count, measure count).
        """
        self._require_loaded()

        from powerbi_import.import_to_powerbi import PowerBIImporter

        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        source_dir = os.path.join(base, 'tableau_export')

        importer = PowerBIImporter(source_dir=source_dir)

        # Apply DAX overrides to extracted calculations
        if self._dax_overrides:
            calcs = self._extracted.get('calculations', [])
            for calc in calcs:
                name = calc.get('name', calc.get('caption', ''))
                if name in self._dax_overrides:
                    calc['_dax_override'] = self._dax_overrides[name]

        if output_dir is None:
            import tempfile
            output_dir = os.path.join(tempfile.gettempdir(), 'pbi_notebook_output')

        result = importer.import_all(
            generate_pbip=True,
            output_dir=output_dir,
            calendar_start=self._config.get('calendar_start', 2020),
            calendar_end=self._config.get('calendar_end', 2030),
            culture=self._config.get('culture', 'en-US'),
            model_mode=self._config.get('mode', 'import'),
            languages=self._config.get('languages'),
        )
        self._generated_path = output_dir

        summary = {
            'output_dir': output_dir,
            'tables': result.get('tables', 0) if isinstance(result, dict) else 0,
            'measures': result.get('measures', 0) if isinstance(result, dict) else 0,
            'pages': result.get('pages', 0) if isinstance(result, dict) else 0,
        }
        logger.info("Generated .pbip project at %s", output_dir)
        return summary

    # ── Validation ────────────────────────────────────────────

    def validate(self):
        """Validate the generated .pbip project.

        Returns:
            dict: Validation results with error/warning counts.
        """
        if not self._generated_path:
            raise RuntimeError("No project generated yet — call generate() first")

        from powerbi_import.validator import ArtifactValidator

        validator = ArtifactValidator()
        result = validator.validate_project(self._generated_path)
        self._validation_result = result
        return result

    # ── Deployment ────────────────────────────────────────────

    def deploy(self, workspace_id, refresh=False):
        """Deploy the generated project to Power BI Service.

        Args:
            workspace_id: Target PBI workspace ID.
            refresh: Trigger dataset refresh after deploy.

        Returns:
            dict: Deployment result.
        """
        if not self._generated_path:
            raise RuntimeError("No project generated yet — call generate() first")

        from powerbi_import.deploy.pbi_deployer import PBIWorkspaceDeployer

        deployer = PBIWorkspaceDeployer(workspace_id=workspace_id)
        result = deployer.deploy_project(
            self._generated_path, refresh=refresh
        )
        return result.to_dict()

    # ── Interactive widgets (v2) ──────────────────────────────

    def assess_interactive(self):
        """Render the assessment as an inline HTML radar chart.

        Returns a rich-display object that renders an SVG radar chart of the
        9 assessment categories in Jupyter (via ``_repr_html_``) and degrades
        to a plain dict elsewhere. Each category axis is scored from its
        pass/warn/fail check ratio.

        Returns:
            _NotebookDisplay: inline widget; ``.to_dict()`` returns raw scores.
        """
        assessment = self._assessment or self.assess()
        scores = _category_scores(assessment)
        html = _render_radar_svg(scores, title='Migration Readiness')
        return _NotebookDisplay(html, {'scores': scores, 'assessment': assessment})

    def explore_dax(self, status=None):
        """Explore all DAX conversions as a filterable inline table.

        Each row carries the Tableau formula, converted DAX, a numeric
        ``confidence`` (1.0 exact / 1.0 overridden / 0.5 approximated /
        0.0 unsupported) and a ``migration_note``. Renders as an HTML table
        in Jupyter; ``.to_dict()`` / ``.to_frame()`` expose the raw rows.

        Args:
            status: optional filter — 'exact', 'approximated', 'overridden'.

        Returns:
            _DaxExplorer: inline table widget over the conversion rows.
        """
        previews = self.preview_dax()
        rows = []
        for p in previews:
            st = p['status']
            confidence = {
                'exact': 1.0, 'overridden': 1.0,
                'approximated': 0.5, 'unsupported': 0.0,
            }.get(st, 0.75)
            note = ''
            if st == 'approximated':
                note = 'Manual review recommended — placeholder/partial DAX'
            elif st == 'overridden':
                note = 'User override active'
            rows.append({
                'name': p['name'],
                'tableau_formula': p['tableau_formula'],
                'dax_formula': p['dax_formula'],
                'status': st,
                'confidence': confidence,
                'migration_note': note,
            })
        if status:
            rows = [r for r in rows if r['status'] == status]
        return _DaxExplorer(rows)

    def show_relationships(self):
        """Render the model relationships as an inline Mermaid ER diagram.

        Builds a Mermaid ``erDiagram`` from extracted datasource
        relationships, flagging cross-table cardinality. Renders inline in
        Jupyter (Mermaid-enabled front-ends) and exposes the diagram source
        via ``.to_dict()['mermaid']``.

        Returns:
            _NotebookDisplay: inline diagram widget.
        """
        self._require_loaded()
        rels = _collect_relationships(self._extracted)
        mermaid = _render_mermaid_er(rels)
        html = _render_mermaid_html(mermaid)
        return _NotebookDisplay(html, {'mermaid': mermaid, 'relationships': rels},
                                text=mermaid)

    # ── Step-by-step migration (v2) ───────────────────────────

    def step_extract(self, workbook_path=None):
        """Run only the extraction phase (idempotent).

        Args:
            workbook_path: optional path; reuses the loaded workbook if omitted.

        Returns:
            dict: per-type extraction counts.
        """
        if workbook_path:
            return self.load(workbook_path)
        if self._extracted is None:
            raise RuntimeError("No workbook loaded — pass workbook_path or call load() first")
        return {k: (len(v) if isinstance(v, (list, dict)) else 1)
                for k, v in self._extracted.items()}

    def step_convert(self):
        """Run only the DAX/M/visual conversion phase for inspection.

        Returns:
            dict: {'dax', 'm', 'visuals'} preview lists plus summary counts.
        """
        self._require_loaded()
        dax = self.preview_dax()
        try:
            m = self.preview_m()
        except Exception as exc:  # noqa: BLE001 — preview-only
            logger.warning("M preview failed during step_convert: %s", exc)
            m = []
        visuals = self.preview_visuals()
        self._converted_objects = {'dax': dax, 'm': m, 'visuals': visuals}
        return {
            'dax': dax,
            'm': m,
            'visuals': visuals,
            'dax_count': len(dax),
            'm_count': len(m),
            'visual_count': len(visuals),
            'approximated': sum(1 for d in dax if d['status'] == 'approximated'),
        }

    def step_generate(self, output_dir=None):
        """Run only the generation phase. Alias of :meth:`generate`.

        Returns:
            dict: generation summary.
        """
        return self.generate(output_dir=output_dir)

    def step_validate(self):
        """Run only the validation phase. Alias of :meth:`validate`.

        Returns:
            dict: validation results.
        """
        return self.validate()

    # ── Notebook Generation ───────────────────────────────────

    def generate_notebook(self, workbook_path, output_path=None):
        """Auto-generate a Jupyter notebook for the given workbook.

        Creates a pre-filled .ipynb with extraction results, assessment,
        and conversion previews.

        Args:
            workbook_path: Path to .twb / .twbx file.
            output_path: Output .ipynb path (default: same dir as workbook).

        Returns:
            str: Path to the generated notebook file.
        """
        wb_name = os.path.splitext(os.path.basename(workbook_path))[0]
        if output_path is None:
            output_path = os.path.join(
                os.path.dirname(os.path.abspath(workbook_path)),
                f'{wb_name}_migration.ipynb'
            )

        # Escape the workbook path for embedding in Python code
        safe_path = workbook_path.replace('\\', '\\\\').replace("'", "\\'")

        cells = [
            _make_markdown_cell(
                f"# Migration Notebook: {wb_name}\n\n"
                "This notebook guides you through migrating a Tableau workbook "
                "to Power BI using the interactive `MigrationSession` API."
            ),
            _make_code_cell(
                "from powerbi_import.notebook_api import MigrationSession\n\n"
                "session = MigrationSession()\n"
                f"summary = session.load(r'{safe_path}')\n"
                "summary"
            ),
            _make_markdown_cell("## Step 2 — Pre-Migration Assessment"),
            _make_code_cell(
                "assessment = session.assess()\n"
                "assessment"
            ),
            _make_markdown_cell(
                "## Step 3 — DAX Conversion Preview\n\n"
                "Review approximated formulas and override if needed."
            ),
            _make_code_cell(
                "dax_preview = session.preview_dax()\n"
                "# Show approximated formulas:\n"
                "approx = session.list_approximated()\n"
                "approx"
            ),
            _make_markdown_cell(
                "## Step 4 — M Query Preview\n\n"
                "Check generated Power Query M expressions."
            ),
            _make_code_cell(
                "m_preview = session.preview_m()\n"
                "m_preview"
            ),
            _make_markdown_cell(
                "## Step 5 — Visual Mapping Preview\n\n"
                "Review Tableau→PBI visual type mappings."
            ),
            _make_code_cell(
                "visuals = session.preview_visuals()\n"
                "visuals"
            ),
            _make_markdown_cell(
                "## Step 6 — Configure & Generate\n\n"
                "Adjust settings and generate the .pbip project."
            ),
            _make_code_cell(
                "session.configure(calendar_start=2020, calendar_end=2030)\n"
                "result = session.generate()\n"
                "result"
            ),
            _make_markdown_cell("## Step 7 — Validate"),
            _make_code_cell(
                "validation = session.validate()\n"
                "validation"
            ),
            _make_markdown_cell(
                "## Step 8 — Deploy (Optional)\n\n"
                "Uncomment and set your workspace ID to deploy."
            ),
            _make_code_cell(
                "# result = session.deploy(workspace_id='YOUR_WORKSPACE_ID', refresh=True)\n"
                "# result"
            ),
        ]

        notebook = {
            'nbformat': 4,
            'nbformat_minor': 5,
            'metadata': {
                'kernelspec': {
                    'display_name': 'Python 3',
                    'language': 'python',
                    'name': 'python3',
                },
                'language_info': {
                    'name': 'python',
                    'version': '3.11.0',
                },
            },
            'cells': cells,
        }

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(notebook, f, indent=1, ensure_ascii=False)

        logger.info("Generated migration notebook: %s", output_path)
        return output_path

    # ── Internals ─────────────────────────────────────────────

    def _require_loaded(self):
        """Raise if no workbook has been loaded."""
        if self._extracted is None:
            raise RuntimeError(
                "No workbook loaded — call load('path.twbx') first"
            )


# ── Notebook cell helpers ─────────────────────────────────────

def _make_markdown_cell(source):
    """Create a Jupyter markdown cell dict."""
    return {
        'cell_type': 'markdown',
        'metadata': {},
        'source': [source],
    }


def _make_code_cell(source):
    """Create a Jupyter code cell dict."""
    return {
        'cell_type': 'code',
        'metadata': {},
        'source': [source],
        'outputs': [],
        'execution_count': None,
    }


# ── Rich display objects (v2) ─────────────────────────────────

class _NotebookDisplay:
    """Rich-display wrapper that renders HTML in Jupyter, dict elsewhere."""

    def __init__(self, html, data, text=None):
        self._html = html
        self._data = data
        self._text = text or ''

    def _repr_html_(self):  # noqa: N802 — Jupyter protocol
        return self._html

    def __repr__(self):
        return self._text or repr(self._data)

    def to_dict(self):
        return self._data


class _DaxExplorer:
    """Filterable table of DAX conversion rows with inline HTML rendering."""

    def __init__(self, rows):
        self._rows = list(rows)

    @property
    def rows(self):
        return list(self._rows)

    def filter(self, status):
        """Return a new explorer filtered by conversion status."""
        return _DaxExplorer([r for r in self._rows if r['status'] == status])

    def to_dict(self):
        return list(self._rows)

    def to_frame(self):
        """Return a pandas DataFrame if pandas is installed, else the rows."""
        try:
            import pandas as pd  # noqa: WPS433 — optional dependency
            return pd.DataFrame(self._rows)
        except Exception:  # noqa: BLE001 — pandas optional
            return list(self._rows)

    def __len__(self):
        return len(self._rows)

    def __iter__(self):
        return iter(self._rows)

    def _repr_html_(self):  # noqa: N802 — Jupyter protocol
        if not self._rows:
            return '<p><em>No DAX conversions.</em></p>'
        head = ('<tr>'
                '<th>Name</th><th>Tableau</th><th>DAX</th>'
                '<th>Status</th><th>Confidence</th><th>Note</th></tr>')
        body = []
        colors = {'exact': '#107c10', 'overridden': '#0078d4',
                  'approximated': '#d29200', 'unsupported': '#a4262c'}
        for r in self._rows:
            c = colors.get(r['status'], '#605e5c')
            body.append(
                '<tr>'
                f'<td><b>{_html_escape(r["name"])}</b></td>'
                f'<td><code>{_html_escape(r["tableau_formula"])}</code></td>'
                f'<td><code>{_html_escape(r["dax_formula"])}</code></td>'
                f'<td style="color:{c};font-weight:600">{r["status"]}</td>'
                f'<td>{r["confidence"]:.2f}</td>'
                f'<td>{_html_escape(r["migration_note"])}</td>'
                '</tr>'
            )
        return (
            '<table style="border-collapse:collapse;font-family:Segoe UI,sans-serif;'
            'font-size:12px" border="1" cellpadding="4">'
            f'{head}{"".join(body)}</table>'
        )

    def __repr__(self):
        return f'<_DaxExplorer rows={len(self._rows)}>'


# ── Rendering helpers (v2) ────────────────────────────────────

def _html_escape(text):
    """Minimal HTML escaping for inline rendering."""
    return (str(text).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def _category_scores(assessment):
    """Reduce an assessment dict to {category: 0.0-1.0} readiness scores."""
    sev_score = {
        'pass': 1.0, 'info': 0.85, 'warn': 0.5, 'fail': 0.0,
        'PASS': 1.0, 'INFO': 0.85, 'WARN': 0.5, 'FAIL': 0.0,
    }
    scores = {}
    categories = assessment.get('categories', assessment.get('checks', []))
    if isinstance(categories, dict):
        categories = list(categories.values())
    for cat in categories or []:
        name = cat.get('name', cat.get('category', 'Category'))
        checks = cat.get('checks', cat.get('items', []))
        if not checks:
            # fall back to a category-level severity/status
            sev = str(cat.get('worst_severity', cat.get('status', 'pass')))
            scores[name] = sev_score.get(sev, sev_score.get(sev.upper(), 0.5))
            continue
        total = 0.0
        for chk in checks:
            sev = str(chk.get('severity', chk.get('status', 'pass')))
            total += sev_score.get(sev, sev_score.get(sev.upper(), 0.5))
        scores[name] = round(total / len(checks), 3) if checks else 1.0
    return scores


def _render_radar_svg(scores, title='Readiness', size=320):
    """Render a simple SVG radar chart from {label: 0.0-1.0} scores."""
    import math
    labels = list(scores.keys())
    n = len(labels)
    if n == 0:
        return '<p><em>No assessment categories.</em></p>'
    cx = cy = size / 2
    radius = size / 2 - 60
    # Grid rings
    rings = []
    for frac in (0.25, 0.5, 0.75, 1.0):
        pts = []
        for i in range(n):
            ang = (2 * math.pi * i / n) - math.pi / 2
            x = cx + radius * frac * math.cos(ang)
            y = cy + radius * frac * math.sin(ang)
            pts.append(f'{x:.1f},{y:.1f}')
        rings.append(f'<polygon points="{" ".join(pts)}" fill="none" '
                     f'stroke="#e1dfdd" stroke-width="1"/>')
    # Data polygon
    data_pts, label_tags = [], []
    for i, lbl in enumerate(labels):
        ang = (2 * math.pi * i / n) - math.pi / 2
        val = max(0.0, min(1.0, scores[lbl]))
        x = cx + radius * val * math.cos(ang)
        y = cy + radius * val * math.sin(ang)
        data_pts.append(f'{x:.1f},{y:.1f}')
        lx = cx + (radius + 24) * math.cos(ang)
        ly = cy + (radius + 24) * math.sin(ang)
        anchor = 'middle'
        if math.cos(ang) > 0.3:
            anchor = 'start'
        elif math.cos(ang) < -0.3:
            anchor = 'end'
        label_tags.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="10" '
            f'text-anchor="{anchor}" fill="#323130">{_html_escape(lbl)}</text>'
        )
    poly = (f'<polygon points="{" ".join(data_pts)}" '
            'fill="rgba(0,120,212,0.25)" stroke="#0078d4" stroke-width="2"/>')
    return (
        f'<svg width="{size}" height="{size}" '
        f'xmlns="http://www.w3.org/2000/svg" '
        'style="font-family:Segoe UI,sans-serif">'
        f'<text x="{cx}" y="16" text-anchor="middle" font-size="13" '
        f'font-weight="600" fill="#201f1e">{_html_escape(title)}</text>'
        f'{"".join(rings)}{poly}{"".join(label_tags)}</svg>'
    )


def _collect_relationships(extracted):
    """Collect relationship descriptors from extracted datasources."""
    rels = []
    seen = set()
    for ds in extracted.get('datasources', []) or []:
        for rel in ds.get('relationships', []) or []:
            frm = rel.get('from_table', rel.get('from', ''))
            to = rel.get('to_table', rel.get('to', ''))
            if not frm or not to:
                continue
            key = (frm, to)
            if key in seen:
                continue
            seen.add(key)
            rels.append({
                'from_table': frm,
                'to_table': to,
                'cardinality': rel.get('cardinality', 'manyToOne'),
                'from_column': rel.get('from_column', rel.get('left_column', '')),
                'to_column': rel.get('to_column', rel.get('right_column', '')),
            })
    return rels


def _mermaid_id(name):
    """Sanitise a table name into a Mermaid identifier."""
    ident = re.sub(r'\W+', '_', str(name)).strip('_')
    return ident or 'T'


def _render_mermaid_er(relationships):
    """Render a Mermaid erDiagram source from relationship descriptors."""
    if not relationships:
        return 'erDiagram\n    %% No relationships detected'
    card_map = {
        'manyToOne': '}o--||',
        'oneToMany': '||--o{',
        'oneToOne': '||--||',
        'manyToMany': '}o--o{',
    }
    lines = ['erDiagram']
    for rel in relationships:
        frm = _mermaid_id(rel['from_table'])
        to = _mermaid_id(rel['to_table'])
        conn = card_map.get(rel.get('cardinality', 'manyToOne'), '}o--||')
        col = rel.get('from_column') or rel.get('to_column') or 'key'
        label = re.sub(r'\W+', '_', str(col)).strip('_') or 'key'
        lines.append(f'    {frm} {conn} {to} : {label}')
    return '\n'.join(lines)


def _render_mermaid_html(mermaid_src):
    """Wrap a Mermaid source in an HTML block for Jupyter front-ends."""
    return (
        '<div class="mermaid" '
        'style="font-family:Segoe UI,sans-serif">'
        f'{_html_escape(mermaid_src)}</div>'
        '<pre style="display:none">' + _html_escape(mermaid_src) + '</pre>'
    )

