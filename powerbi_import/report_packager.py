"""Report packaging — bundles HTML, print-ready PDF, PPTX, JSON, and CSV.

Sprint 175: Produces a ZIP archive containing all report formats for a
single assessment or migration run.

Contents of the ZIP:
    assessment_report.html     — Interactive HTML report
    assessment_report.pdf.html — Print-optimized HTML (for browser PDF export)
    executive_summary.pptx     — 5-slide PPTX executive summary
    assessment_data.json       — Machine-readable assessment data
    fidelity_checks.csv        — CSV of all check items
    README.txt                 — Instructions for using the package

Usage::

    from powerbi_import.report_packager import generate_report_package

    generate_report_package(
        assessment_report=report,
        html_content=html_string,
        output_path="assessment_package.zip",
    )
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import zipfile
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def generate_report_package(
    assessment_report,
    html_content: str,
    output_path: str,
    *,
    migration_stats: dict | None = None,
) -> str:
    """Generate a ZIP package with all report formats.

    Args:
        assessment_report: An ``AssessmentReport`` instance (has ``to_dict()``).
        html_content: The interactive HTML report string.
        output_path: Path for the output ``.zip`` file.
        migration_stats: Optional extra stats for the PPTX.

    Returns:
        Absolute path to the generated ZIP file.
    """
    from powerbi_import.pdf_renderer import render_print_html
    from powerbi_import.pptx_report import generate_pptx_report

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)

    data = assessment_report.to_dict()
    wb_name = data.get('workbook_name', 'Workbook')

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 1. Interactive HTML
        zf.writestr('assessment_report.html', html_content)

        # 2. Print-optimized HTML
        print_html = render_print_html(
            html_content,
            title=f"{wb_name} — Assessment Report",
        )
        zf.writestr('assessment_report.pdf.html', print_html)

        # 3. PPTX executive summary
        pptx_buf = io.BytesIO()
        # Generate PPTX to a temp buffer
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.pptx', delete=False) as tmp:
            tmp_path = tmp.name
        try:
            generate_pptx_report(
                data,
                tmp_path,
                migration_stats=migration_stats or {},
            )
            with open(tmp_path, 'rb') as f:
                zf.writestr('executive_summary.pptx', f.read())
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        # 4. JSON data
        zf.writestr(
            'assessment_data.json',
            json.dumps(data, indent=2, ensure_ascii=False),
        )

        # 5. CSV of all checks
        csv_content = _build_checks_csv(data)
        zf.writestr('fidelity_checks.csv', csv_content)

        # 6. README
        readme = _build_readme(wb_name, data)
        zf.writestr('README.txt', readme)

    logger.info("Report package saved to %s", output_path)
    return os.path.abspath(output_path)


def _build_checks_csv(data: dict) -> str:
    """Build a CSV string of all assessment check items."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        'Category', 'Check Name', 'Severity', 'Detail', 'Recommendation',
    ])
    for cat in data.get('categories', []):
        cat_name = cat.get('name', '')
        for ck in cat.get('checks', []):
            writer.writerow([
                cat_name,
                ck.get('name', ''),
                ck.get('severity', ''),
                ck.get('detail', ''),
                ck.get('recommendation', ''),
            ])
    return output.getvalue()


def _build_readme(wb_name: str, data: dict) -> str:
    """Build README.txt for the report package."""
    score = data.get('overall_score', 'UNKNOWN')
    totals = data.get('totals', {})
    ts = data.get('timestamp', datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))

    return f"""\
Tableau to Power BI — Assessment Report Package
================================================

Workbook:    {wb_name}
Score:       {score}
Date:        {ts}
Checks:      {totals.get('checks', 0)} total ({totals.get('pass', 0)} pass, \
{totals.get('warn', 0)} warn, {totals.get('fail', 0)} fail)

Package Contents
----------------

assessment_report.html
    Interactive HTML report with sortable tables, charts, and dark mode.
    Open in any web browser.

assessment_report.pdf.html
    Print-optimized version with all sections expanded and interactive
    elements hidden. Open in a browser and use Ctrl+P / Cmd+P to save
    as PDF. Also compatible with weasyprint for command-line conversion.

executive_summary.pptx
    5-slide PowerPoint executive summary:
    1. Title (workbook, score, date)
    2. Scope overview (check counts, categories)
    3. Readiness table (per-category breakdown)
    4. Top risks and warnings
    5. Recommendations and next steps

assessment_data.json
    Machine-readable JSON with all assessment data. Suitable for
    programmatic analysis, dashboarding, or API consumption.

fidelity_checks.csv
    Flat CSV of every check item with category, severity, detail,
    and recommendation. Import into Excel or any BI tool for custom
    analysis.

Usage
-----

1. Review assessment_report.html for full interactive analysis
2. Share executive_summary.pptx with stakeholders
3. Use assessment_report.pdf.html for print/archive copies
4. Import fidelity_checks.csv into Excel for custom filtering

Generated by Tableau -> Power BI Migration Tool
"""
