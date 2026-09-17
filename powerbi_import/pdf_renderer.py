"""PDF-ready report renderer.

Sprint 175: Generates print-optimized HTML suitable for browser Print-to-PDF
or ``weasyprint`` conversion.  Pure stdlib — zero external dependencies.

The output is a self-contained ``.pdf.html`` file with:

* ``@media print`` CSS for page breaks, expanded sections, hidden interactive
  elements, A4 page size, and margin control.
* All collapsed sections auto-expanded.
* Interactive elements (search bars, theme toggle, tab bars) hidden.
* Page breaks before each major section.

Usage::

    from powerbi_import.pdf_renderer import render_print_html, save_print_html

    html = generate_server_html_report(...)  # any HTML report string
    print_html = render_print_html(html, title="Portfolio Assessment")
    save_print_html(print_html, "report.pdf.html")
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# ── Print-optimized CSS injected before </style> ──────────────────────────

_PRINT_CSS = """
/* ═══ PDF / Print Optimizations (Sprint 175) ═══ */
@media print {
    /* Page setup */
    @page {
        size: A4 portrait;
        margin: 18mm 15mm 18mm 15mm;
    }

    /* Force color printing */
    * {
        -webkit-print-color-adjust: exact !important;
        print-color-adjust: exact !important;
        color-adjust: exact !important;
    }

    body {
        background: #fff !important;
        font-size: 10pt;
        line-height: 1.45;
    }

    /* Expand all collapsed sections */
    .section-body {
        max-height: none !important;
        opacity: 1 !important;
        overflow: visible !important;
    }
    .section-body.collapsed {
        max-height: none !important;
        opacity: 1 !important;
    }

    /* Show all tab contents */
    .tab-content { display: block !important; }
    .tab-content::before {
        content: attr(id);
        display: block;
        font-weight: 600;
        font-size: 0.9em;
        color: #605e5c;
        margin-bottom: 6px;
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }

    /* Hide interactive elements */
    .theme-toggle { display: none !important; }
    .table-search { display: none !important; }
    .tab-bar { display: none !important; }
    .toggle-arrow { display: none !important; }
    .section-header { cursor: default !important; pointer-events: none; }

    /* Page breaks */
    .section-header { page-break-before: auto; }
    .section-header + .section-body { page-break-before: avoid; }
    .stat-card { break-inside: avoid; }
    .card { break-inside: avoid; }
    .table-container { break-inside: avoid; }
    .chart-card { break-inside: avoid; }
    .bar-chart { break-inside: avoid; }
    .donut-container { break-inside: avoid; }

    /* Ensure header prints with background */
    .report-header {
        background: #0078d4 !important;
        -webkit-print-color-adjust: exact !important;
        print-color-adjust: exact !important;
    }

    /* Reduce padding */
    .container { padding: 0 8px 20px; }
    .report-header { padding: 20px 24px 16px; margin-bottom: 12px; }

    /* Table readability */
    thead th { background: #0078d4 !important; color: #fff !important; font-size: 0.78em; }
    tbody td { font-size: 0.8em; padding: 5px 8px; }

    /* Footer */
    .report-footer { page-break-before: avoid; margin-top: 20px; }

    /* Badges - ensure visibility */
    .badge { border: 1px solid currentColor; }
}

/* Print-specific class: added by render_print_html to force-expand */
.print-mode .section-body {
    max-height: none !important;
    opacity: 1 !important;
    overflow: visible !important;
}
.print-mode .section-body.collapsed {
    max-height: none !important;
    opacity: 1 !important;
}
.print-mode .tab-content { display: block !important; }
.print-mode .theme-toggle { display: none !important; }
.print-mode .table-search { display: none !important; }
.print-mode .tab-bar { display: none !important; }
.print-mode .toggle-arrow { display: none !important; }
"""

# ── Print instructions banner ─────────────────────────────────────────────

_PRINT_BANNER = """
<div id="print-banner" style="
    background: #fff4ce; border: 1px solid #c19c00; border-radius: 8px;
    padding: 14px 20px; margin: 0 24px 16px; font-size: 0.9em;
    color: #856404; display: flex; align-items: center; gap: 10px;
">
    <span style="font-size:1.3em">🖨️</span>
    <span>
        <strong>Print-Ready Report</strong> —
        Use <kbd>Ctrl+P</kbd> (or <kbd>⌘P</kbd> on Mac) to save as PDF.
        All sections are expanded and interactive elements are hidden for clean output.
    </span>
    <button onclick="window.print()" style="
        background: #0078d4; color: #fff; border: none; border-radius: 4px;
        padding: 6px 16px; cursor: pointer; font-weight: 600; font-size: 0.9em;
        white-space: nowrap; margin-left: auto;
    ">Save as PDF</button>
</div>
<style>#print-banner { display: flex; } @media print { #print-banner { display: none !important; } }</style>
"""


def render_print_html(
    html: str,
    *,
    title: str = "",
) -> str:
    """Transform an interactive HTML report into a print-optimized version.

    Args:
        html: The source HTML report string (from any ``generate_*_html_report``).
        title: Optional override for the ``<title>`` tag.

    Returns:
        A modified HTML string with print-optimized CSS, expanded sections,
        and hidden interactive elements.
    """
    output = html

    # 1. Inject print CSS before </style> (or in <head> if no <style>)
    if '</style>' in output:
        output = output.replace('</style>', _PRINT_CSS + '\n</style>', 1)
    elif '</head>' in output:
        output = output.replace('</head>', '<style>' + _PRINT_CSS + '</style>\n</head>', 1)
    else:
        # No <head> — inject CSS as <style> block before <body> or at start
        css_block = '<style>' + _PRINT_CSS + '</style>\n'
        if '<body>' in output:
            output = output.replace('<body>', css_block + '<body>', 1)
        else:
            output = css_block + output

    # 2. Add print-mode class to <html> to force-expand without @media print
    if '<html lang="en">' in output:
        output = output.replace('<html lang="en">', '<html lang="en" class="print-mode">', 1)
    elif '<html>' in output:
        output = output.replace('<html>', '<html class="print-mode">', 1)

    # 3. Remove collapsed classes from section bodies via regex
    output = re.sub(
        r'class="section-body\s+collapsed"',
        'class="section-body"',
        output,
    )
    output = re.sub(
        r'class="toggle-arrow\s+collapsed"',
        'class="toggle-arrow"',
        output,
    )

    # 4. Make all tab-contents visible
    output = re.sub(
        r'class="tab-content"',
        'class="tab-content active"',
        output,
    )

    # 5. Inject print banner after <div class="container"> or after <body>
    if '<div class="container">' in output:
        output = output.replace(
            '<div class="container">',
            '<div class="container">\n' + _PRINT_BANNER,
            1,
        )
    elif '<body>' in output:
        output = output.replace(
            '<body>',
            '<body>\n' + _PRINT_BANNER,
            1,
        )
    elif '<body' in output:
        # <body ...> with attributes
        output = re.sub(
            r'(<body[^>]*>)',
            r'\1\n' + _PRINT_BANNER,
            output,
            count=1,
        )

    # 6. Override title if provided
    if title:
        output = re.sub(
            r'<title>[^<]*</title>',
            f'<title>{_esc(title)} (Print)</title>',
            output,
            count=1,
        )

    return output


def save_print_html(
    html: str,
    output_path: str,
) -> str:
    """Save print-optimized HTML to a file.

    Args:
        html: The print-optimized HTML string.
        output_path: Path for the output file (e.g. ``report.pdf.html``).

    Returns:
        The absolute path to the saved file.
    """
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    logger.info("Print-ready report saved to %s", output_path)
    return os.path.abspath(output_path)


def _esc(text: str) -> str:
    """HTML-escape a string."""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))
