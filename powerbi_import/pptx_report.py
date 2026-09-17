"""PPTX executive summary report generator.

Sprint 175: Generates Office Open XML ``.pptx`` files using only the Python
standard library (xml.etree + zipfile).  No ``python-pptx`` dependency.

Produces a 5-slide deck:
    1. Title slide (workbook name, date, overall score)
    2. Scope overview (counts of datasources, worksheets, calculations, etc.)
    3. Fidelity / readiness table (per-category pass/warn/fail)
    4. Risk matrix (top warnings and failures)
    5. Recommendations & next steps

Usage::

    from powerbi_import.pptx_report import generate_pptx_report

    generate_pptx_report(
        assessment_data=report.to_dict(),
        output_path="migration_summary.pptx",
    )
"""

from __future__ import annotations

import logging
import os
import uuid
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from xml.etree.ElementTree import Element, SubElement, tostring

logger = logging.getLogger(__name__)

# ── OPC (Open Packaging Convention) constants ─────────────────────────────

_CONTENT_TYPES = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
  <Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
  <Override PartName="/ppt/slideLayouts/slideLayout2.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
  <Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
  {slide_overrides}
</Types>"""

_RELS_ROOT = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>"""

# ── XML namespaces ────────────────────────────────────────────────────────

_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"

# Slide dimensions: 10" × 7.5" in EMU (1 inch = 914400 EMU)
_SLIDE_W = 12192000  # 13.333" widescreen → 12192000 EMU
_SLIDE_H = 6858000   # 7.5" → 6858000 EMU

# Colors (PBI theme)
_PBI_BLUE = "0078D4"
_PBI_DARK = "323130"
_WHITE = "FFFFFF"
_GREEN = "107C10"
_YELLOW = "C19C00"
_RED = "A4262C"
_LIGHT_BG = "F3F2F1"


def generate_pptx_report(
    assessment_data: dict,
    output_path: str,
    *,
    migration_stats: dict | None = None,
) -> str:
    """Generate a PPTX executive summary from assessment data.

    Args:
        assessment_data: Dict from ``AssessmentReport.to_dict()``.
        output_path: Where to save the ``.pptx`` file.
        migration_stats: Optional extra stats (fidelity_pct, visual_count, etc.).

    Returns:
        Absolute path to the generated file.
    """
    buf = BytesIO()

    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        slides = _build_slides(assessment_data, migration_stats or {})

        # [Content_Types].xml
        overrides = "\n  ".join(
            f'<Override PartName="/ppt/slides/slide{i+1}.xml" '
            f'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
            for i in range(len(slides))
        )
        zf.writestr('[Content_Types].xml',
                     _CONTENT_TYPES.replace('{slide_overrides}', overrides))

        # _rels/.rels
        zf.writestr('_rels/.rels', _RELS_ROOT)

        # Theme
        zf.writestr('ppt/theme/theme1.xml', _build_theme())

        # Slide layouts
        zf.writestr('ppt/slideLayouts/slideLayout1.xml', _build_slide_layout("Title Slide", "1"))
        zf.writestr('ppt/slideLayouts/slideLayout2.xml', _build_slide_layout("Content", "2"))
        zf.writestr('ppt/slideLayouts/_rels/slideLayout1.xml.rels', _layout_rels())
        zf.writestr('ppt/slideLayouts/_rels/slideLayout2.xml.rels', _layout_rels())

        # Slide master
        zf.writestr('ppt/slideMasters/slideMaster1.xml', _build_slide_master(2))
        zf.writestr('ppt/slideMasters/_rels/slideMaster1.xml.rels',
                     _slide_master_rels(2))

        # Presentation
        zf.writestr('ppt/presentation.xml', _build_presentation(len(slides)))
        zf.writestr('ppt/_rels/presentation.xml.rels',
                     _presentation_rels(len(slides)))

        # Slides
        for i, slide_xml in enumerate(slides):
            zf.writestr(f'ppt/slides/slide{i+1}.xml', slide_xml)
            zf.writestr(f'ppt/slides/_rels/slide{i+1}.xml.rels', _slide_rels())

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with open(output_path, 'wb') as f:
        f.write(buf.getvalue())

    logger.info("PPTX report saved to %s", output_path)
    return os.path.abspath(output_path)


# ═══════════════════════════════════════════════════════════════════════
#  Slide builders
# ═══════════════════════════════════════════════════════════════════════

def _build_slides(data: dict, stats: dict) -> list[str]:
    """Build all slide XMLs."""
    slides = [
        _slide_title(data),
        _slide_scope(data),
        _slide_readiness(data),
        _slide_risks(data),
        _slide_recommendations(data),
    ]
    return slides


def _slide_title(data: dict) -> str:
    """Slide 1: Title slide."""
    wb = data.get('workbook_name', 'Workbook')
    ts = data.get('timestamp', '')
    score = data.get('overall_score', 'UNKNOWN')
    date_str = ts[:10] if ts else datetime.now(timezone.utc).strftime('%Y-%m-%d')

    score_color = {
        'GREEN': _GREEN, 'YELLOW': _YELLOW, 'RED': _RED,
    }.get(score, _PBI_DARK)

    shapes = []
    # Background rectangle (PBI blue)
    shapes.append(_rect(0, 0, _SLIDE_W, _SLIDE_H, fill=_PBI_BLUE))
    # Title
    shapes.append(_text_box(
        "Tableau → Power BI Migration",
        left=600000, top=1800000, width=10900000, height=900000,
        font_size=3200, bold=True, color=_WHITE, align='ctr',
    ))
    # Subtitle (workbook name)
    shapes.append(_text_box(
        wb,
        left=600000, top=2700000, width=10900000, height=600000,
        font_size=2000, bold=False, color=_WHITE, align='ctr',
    ))
    # Score badge
    shapes.append(_text_box(
        f"Readiness: {score}",
        left=4000000, top=3600000, width=4000000, height=500000,
        font_size=1800, bold=True, color=score_color, align='ctr',
        fill=_WHITE,
    ))
    # Date
    shapes.append(_text_box(
        f"Assessment Date: {date_str}",
        left=600000, top=5800000, width=10900000, height=400000,
        font_size=1200, bold=False, color=_WHITE, align='ctr',
    ))

    return _wrap_slide(shapes)


def _slide_scope(data: dict) -> str:
    """Slide 2: Scope overview."""
    totals = data.get('totals', {})
    summary = data.get('summary', {})
    categories = data.get('categories', [])

    shapes = []
    shapes.append(_slide_title_bar("Migration Scope Overview"))

    # Stats grid
    stats_items = [
        ("Total Checks", str(totals.get('checks', 0))),
        ("Passed", str(totals.get('pass', 0))),
        ("Warnings", str(totals.get('warn', 0))),
        ("Failures", str(totals.get('fail', 0))),
    ]

    for i, (label, value) in enumerate(stats_items):
        col = i % 4
        left = 600000 + col * 2800000
        top = 1600000
        shapes.append(_stat_box(left, top, 2500000, 900000, value, label))

    # Categories list
    y = 2900000
    for cat in categories[:8]:  # Max 8 categories per slide
        name = cat.get('name', '')
        sev = cat.get('worst_severity', 'pass')
        checks = cat.get('checks', [])
        pass_c = sum(1 for c in checks if c.get('severity') == 'pass')
        warn_c = sum(1 for c in checks if c.get('severity') == 'warn')
        fail_c = sum(1 for c in checks if c.get('severity') == 'fail')
        sev_color = {'pass': _GREEN, 'warn': _YELLOW, 'fail': _RED}.get(sev, _PBI_DARK)
        text = f"● {name}  —  {len(checks)} checks ({pass_c}✓ {warn_c}⚠ {fail_c}✗)"
        shapes.append(_text_box(
            text,
            left=600000, top=y, width=10900000, height=350000,
            font_size=1200, color=sev_color,
        ))
        y += 400000

    return _wrap_slide(shapes)


def _slide_readiness(data: dict) -> str:
    """Slide 3: Readiness table."""
    categories = data.get('categories', [])
    shapes = []
    shapes.append(_slide_title_bar("Category Readiness"))

    # Table header
    y = 1500000
    headers = ["Category", "Checks", "Pass", "Warn", "Fail", "Status"]
    col_widths = [4000000, 1500000, 1500000, 1500000, 1500000, 1500000]
    x = 400000
    for i, h in enumerate(headers):
        shapes.append(_text_box(
            h, left=x, top=y, width=col_widths[i], height=400000,
            font_size=1000, bold=True, color=_WHITE, fill=_PBI_BLUE, align='ctr',
        ))
        x += col_widths[i]

    # Table rows
    y += 400000
    for cat in categories[:10]:
        name = cat.get('name', '')
        checks = cat.get('checks', [])
        total = len(checks)
        pass_c = sum(1 for c in checks if c.get('severity') == 'pass')
        warn_c = sum(1 for c in checks if c.get('severity') == 'warn')
        fail_c = sum(1 for c in checks if c.get('severity') == 'fail')
        sev = cat.get('worst_severity', 'pass')
        sev_label = sev.upper()

        values = [name, str(total), str(pass_c), str(warn_c), str(fail_c), sev_label]
        x = 400000
        for i, val in enumerate(values):
            color = _PBI_DARK
            if i == 5:
                color = {'PASS': _GREEN, 'WARN': _YELLOW, 'FAIL': _RED}.get(val, _PBI_DARK)
            fill = _LIGHT_BG if (categories.index(cat) % 2 == 0) else _WHITE
            shapes.append(_text_box(
                val, left=x, top=y, width=col_widths[i], height=350000,
                font_size=900, color=color, fill=fill,
                align='ctr' if i > 0 else 'l',
            ))
            x += col_widths[i]
        y += 350000

    return _wrap_slide(shapes)


def _slide_risks(data: dict) -> str:
    """Slide 4: Top risks (warnings + failures)."""
    categories = data.get('categories', [])
    shapes = []
    shapes.append(_slide_title_bar("Top Risks & Warnings"))

    risks = []
    for cat in categories:
        for ck in cat.get('checks', []):
            if ck.get('severity') in ('warn', 'fail'):
                risks.append({
                    'category': cat.get('name', ''),
                    'name': ck.get('name', ''),
                    'severity': ck.get('severity', ''),
                    'detail': ck.get('detail', ''),
                    'recommendation': ck.get('recommendation', ''),
                })

    # Sort failures first, then warnings
    risks.sort(key=lambda r: (0 if r['severity'] == 'fail' else 1))

    y = 1500000
    for risk in risks[:8]:
        sev = risk['severity']
        icon = "✗" if sev == 'fail' else "⚠"
        color = _RED if sev == 'fail' else _YELLOW
        text = f"{icon} [{risk['category']}] {risk['name']}: {risk['detail']}"
        if len(text) > 100:
            text = text[:97] + "..."
        shapes.append(_text_box(
            text,
            left=600000, top=y, width=10900000, height=350000,
            font_size=1100, color=color,
        ))
        if risk['recommendation']:
            rec = risk['recommendation']
            if len(rec) > 110:
                rec = rec[:107] + "..."
            shapes.append(_text_box(
                f"   → {rec}",
                left=600000, top=y + 320000, width=10900000, height=300000,
                font_size=900, color=_PBI_DARK,
            ))
            y += 700000
        else:
            y += 450000

    if not risks:
        shapes.append(_text_box(
            "✓ No warnings or failures detected — ready for migration!",
            left=600000, top=2500000, width=10900000, height=600000,
            font_size=1600, bold=True, color=_GREEN, align='ctr',
        ))

    return _wrap_slide(shapes)


def _slide_recommendations(data: dict) -> str:
    """Slide 5: Recommendations."""
    score = data.get('overall_score', 'UNKNOWN')
    shapes = []
    shapes.append(_slide_title_bar("Recommendations & Next Steps"))

    recommendations = _get_recommendations(score, data)

    y = 1600000
    for i, rec in enumerate(recommendations[:7], 1):
        shapes.append(_text_box(
            f"{i}. {rec}",
            left=600000, top=y, width=10900000, height=400000,
            font_size=1200, color=_PBI_DARK,
        ))
        y += 500000

    # Footer
    shapes.append(_text_box(
        "Generated by Tableau → Power BI Migration Tool",
        left=600000, top=6200000, width=10900000, height=300000,
        font_size=900, color="A19F9D", align='ctr',
    ))

    return _wrap_slide(shapes)


def _get_recommendations(score: str, data: dict) -> list[str]:
    """Generate recommendations based on assessment score."""
    recs = []

    if score == 'GREEN':
        recs.append("Migration is ready to proceed — all checks passed.")
        recs.append("Open the generated .pbip in Power BI Desktop to validate visuals.")
        recs.append("Review relationships in the Model view for correctness.")
        recs.append("Compare Tableau visuals side-by-side with Power BI output.")
        recs.append("Test any DAX measures that use cross-table references.")
    elif score == 'YELLOW':
        recs.append("Address warnings before production migration.")
        recs.append("Review partial-support connectors and plan alternatives.")
        recs.append("Validate LOD expressions and table calculations in DAX.")
        recs.append("Test all slicers and filters for correct behavior.")
        recs.append("Consider using DirectQuery for large datasources.")
        recs.append("Schedule a UAT review with report consumers.")
    else:  # RED
        recs.append("Resolve critical failures before attempting migration.")
        recs.append("Replace unsupported connectors with supported alternatives.")
        recs.append("Refactor complex LOD expressions for DAX compatibility.")
        recs.append("Consider breaking the workbook into smaller, focused reports.")
        recs.append("Engage with the migration team for manual conversion assistance.")
        recs.append("Review the Known Limitations guide for workarounds.")
        recs.append("Plan a phased migration starting with simpler worksheets.")

    return recs


# ═══════════════════════════════════════════════════════════════════════
#  XML shape builders
# ═══════════════════════════════════════════════════════════════════════

def _wrap_slide(shapes: list[str]) -> str:
    """Wrap shape XMLs into a full slide XML."""
    shapes_xml = "\n".join(shapes)
    return f"""\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="{_NS_A}" xmlns:r="{_NS_R}" xmlns:p="{_NS_P}">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr>
        <p:cNvPr id="1" name=""/>
        <p:cNvGrpSpPr/>
        <p:nvPr/>
      </p:nvGrpSpPr>
      <p:grpSpPr>
        <a:xfrm>
          <a:off x="0" y="0"/>
          <a:ext cx="0" cy="0"/>
          <a:chOff x="0" y="0"/>
          <a:chExt cx="0" cy="0"/>
        </a:xfrm>
      </p:grpSpPr>
      {shapes_xml}
    </p:spTree>
  </p:cSld>
</p:sld>"""


def _text_box(
    text: str,
    *,
    left: int,
    top: int,
    width: int,
    height: int,
    font_size: int = 1200,
    bold: bool = False,
    color: str = _PBI_DARK,
    align: str = 'l',
    fill: str = "",
) -> str:
    """Generate a text box shape."""
    sp_id = _next_id()
    bold_attr = ' b="1"' if bold else ''
    algn_map = {'l': 'l', 'ctr': 'ctr', 'r': 'r', 'c': 'ctr'}
    algn = algn_map.get(align, 'l')

    fill_xml = ""
    if fill:
        fill_xml = f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>'

    sp_fill = ""
    if fill:
        sp_fill = f"""
        <p:spPr>
          <a:xfrm>
            <a:off x="{left}" y="{top}"/>
            <a:ext cx="{width}" cy="{height}"/>
          </a:xfrm>
          <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
          <a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>
          <a:ln><a:noFill/></a:ln>
        </p:spPr>"""
    else:
        sp_fill = f"""
        <p:spPr>
          <a:xfrm>
            <a:off x="{left}" y="{top}"/>
            <a:ext cx="{width}" cy="{height}"/>
          </a:xfrm>
          <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
          <a:noFill/>
          <a:ln><a:noFill/></a:ln>
        </p:spPr>"""

    # Escape text for XML
    safe_text = _xml_esc(text)

    return f"""
      <p:sp>
        <p:nvSpPr>
          <p:cNvPr id="{sp_id}" name="TextBox{sp_id}"/>
          <p:cNvSpPr txBox="1"/>
          <p:nvPr/>
        </p:nvSpPr>
        {sp_fill}
        <p:txBody>
          <a:bodyPr wrap="square" rtlCol="0"/>
          <a:lstStyle/>
          <a:p>
            <a:pPr algn="{algn}"/>
            <a:r>
              <a:rPr lang="en-US" sz="{font_size}"{bold_attr} dirty="0">
                <a:solidFill><a:srgbClr val="{color}"/></a:solidFill>
                <a:latin typeface="Segoe UI"/>
              </a:rPr>
              <a:t>{safe_text}</a:t>
            </a:r>
          </a:p>
        </p:txBody>
      </p:sp>"""


def _rect(
    left: int,
    top: int,
    width: int,
    height: int,
    *,
    fill: str = _PBI_BLUE,
) -> str:
    """Generate a filled rectangle shape."""
    sp_id = _next_id()
    return f"""
      <p:sp>
        <p:nvSpPr>
          <p:cNvPr id="{sp_id}" name="Rect{sp_id}"/>
          <p:cNvSpPr/>
          <p:nvPr/>
        </p:nvSpPr>
        <p:spPr>
          <a:xfrm>
            <a:off x="{left}" y="{top}"/>
            <a:ext cx="{width}" cy="{height}"/>
          </a:xfrm>
          <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
          <a:solidFill><a:srgbClr val="{fill}"/></a:solidFill>
          <a:ln><a:noFill/></a:ln>
        </p:spPr>
      </p:sp>"""


def _stat_box(
    left: int,
    top: int,
    width: int,
    height: int,
    value: str,
    label: str,
) -> str:
    """Generate a stat card shape (value + label)."""
    sp_id = _next_id()
    return f"""
      <p:sp>
        <p:nvSpPr>
          <p:cNvPr id="{sp_id}" name="Stat{sp_id}"/>
          <p:cNvSpPr txBox="1"/>
          <p:nvPr/>
        </p:nvSpPr>
        <p:spPr>
          <a:xfrm>
            <a:off x="{left}" y="{top}"/>
            <a:ext cx="{width}" cy="{height}"/>
          </a:xfrm>
          <a:prstGeom prst="roundRect"><a:avLst/></a:prstGeom>
          <a:solidFill><a:srgbClr val="{_LIGHT_BG}"/></a:solidFill>
          <a:ln w="12700"><a:solidFill><a:srgbClr val="{_PBI_BLUE}"/></a:solidFill></a:ln>
        </p:spPr>
        <p:txBody>
          <a:bodyPr wrap="square" rtlCol="0" anchor="ctr"/>
          <a:lstStyle/>
          <a:p>
            <a:pPr algn="ctr"/>
            <a:r>
              <a:rPr lang="en-US" sz="2400" b="1" dirty="0">
                <a:solidFill><a:srgbClr val="{_PBI_BLUE}"/></a:solidFill>
                <a:latin typeface="Segoe UI"/>
              </a:rPr>
              <a:t>{_xml_esc(value)}</a:t>
            </a:r>
          </a:p>
          <a:p>
            <a:pPr algn="ctr"/>
            <a:r>
              <a:rPr lang="en-US" sz="1000" dirty="0">
                <a:solidFill><a:srgbClr val="{_PBI_DARK}"/></a:solidFill>
                <a:latin typeface="Segoe UI"/>
              </a:rPr>
              <a:t>{_xml_esc(label)}</a:t>
            </a:r>
          </a:p>
        </p:txBody>
      </p:sp>"""


def _slide_title_bar(title: str) -> str:
    """Generate a title bar across the top of a content slide."""
    shapes = _rect(0, 0, _SLIDE_W, 1200000, fill=_PBI_BLUE)
    shapes += _text_box(
        title,
        left=400000, top=250000, width=11000000, height=600000,
        font_size=2400, bold=True, color=_WHITE,
    )
    return shapes


# ═══════════════════════════════════════════════════════════════════════
#  OPC relationship and structure files
# ═══════════════════════════════════════════════════════════════════════

def _build_presentation(num_slides: int) -> str:
    """Build presentation.xml."""
    slide_refs = "\n    ".join(
        f'<p:sldId id="{256 + i}" r:id="rId{10 + i}"/>'
        for i in range(num_slides)
    )
    return f"""\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="{_NS_A}" xmlns:r="{_NS_R}" xmlns:p="{_NS_P}"
    saveSubsetFonts="1">
  <p:sldMasterIdLst>
    <p:sldMasterId id="2147483648" r:id="rId1"/>
  </p:sldMasterIdLst>
  <p:sldIdLst>
    {slide_refs}
  </p:sldIdLst>
  <p:sldSz cx="{_SLIDE_W}" cy="{_SLIDE_H}"/>
  <p:notesSz cx="{_SLIDE_H}" cy="{_SLIDE_W}"/>
</p:presentation>"""


def _presentation_rels(num_slides: int) -> str:
    """Build ppt/_rels/presentation.xml.rels."""
    rels = [
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" '
        'Target="slideMasters/slideMaster1.xml"/>',
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" '
        'Target="theme/theme1.xml"/>',
    ]
    for i in range(num_slides):
        rels.append(
            f'<Relationship Id="rId{10 + i}" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
            f'Target="slides/slide{i + 1}.xml"/>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n  '
        + "\n  ".join(rels)
        + "\n</Relationships>"
    )


def _slide_rels() -> str:
    """Build per-slide .rels pointing to slideLayout2 (content layout)."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" '
        'Target="../slideLayouts/slideLayout2.xml"/>\n'
        '</Relationships>'
    )


def _build_slide_master(num_layouts: int) -> str:
    """Build slideMaster1.xml."""
    layout_refs = "\n    ".join(
        f'<p:sldLayoutId id="{2147483649 + i}" r:id="rId{i + 1}"/>'
        for i in range(num_layouts)
    )
    return f"""\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="{_NS_A}" xmlns:r="{_NS_R}" xmlns:p="{_NS_P}">
  <p:cSld>
    <p:bg><p:bgPr><a:solidFill><a:srgbClr val="{_WHITE}"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr>
        <a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm>
      </p:grpSpPr>
    </p:spTree>
  </p:cSld>
  <p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2"
    accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
  <p:sldLayoutIdLst>
    {layout_refs}
  </p:sldLayoutIdLst>
</p:sldMaster>"""


def _slide_master_rels(num_layouts: int) -> str:
    """Build slideMasters/_rels/slideMaster1.xml.rels."""
    rels = []
    for i in range(num_layouts):
        rels.append(
            f'<Relationship Id="rId{i + 1}" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" '
            f'Target="../slideLayouts/slideLayout{i + 1}.xml"/>'
        )
    rels.append(
        f'<Relationship Id="rId{num_layouts + 1}" '
        f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" '
        f'Target="../theme/theme1.xml"/>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n  '
        + "\n  ".join(rels)
        + "\n</Relationships>"
    )


def _build_slide_layout(name: str, idx: str) -> str:
    """Build a minimal slide layout."""
    return f"""\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="{_NS_A}" xmlns:r="{_NS_R}" xmlns:p="{_NS_P}"
    type="blank" preserve="1">
  <p:cSld name="{_xml_esc(name)}">
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr>
        <a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm>
      </p:grpSpPr>
    </p:spTree>
  </p:cSld>
</p:sldLayout>"""


def _layout_rels() -> str:
    """Build slideLayouts/_rels/slideLayoutN.xml.rels."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" '
        'Target="../slideMasters/slideMaster1.xml"/>\n'
        '</Relationships>'
    )


def _build_theme() -> str:
    """Build a minimal PBI-themed theme1.xml."""
    return f"""\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="{_NS_A}" name="PBI Migration">
  <a:themeElements>
    <a:clrScheme name="PBI">
      <a:dk1><a:srgbClr val="{_PBI_DARK}"/></a:dk1>
      <a:lt1><a:srgbClr val="{_WHITE}"/></a:lt1>
      <a:dk2><a:srgbClr val="{_PBI_DARK}"/></a:dk2>
      <a:lt2><a:srgbClr val="{_LIGHT_BG}"/></a:lt2>
      <a:accent1><a:srgbClr val="{_PBI_BLUE}"/></a:accent1>
      <a:accent2><a:srgbClr val="{_GREEN}"/></a:accent2>
      <a:accent3><a:srgbClr val="{_YELLOW}"/></a:accent3>
      <a:accent4><a:srgbClr val="{_RED}"/></a:accent4>
      <a:accent5><a:srgbClr val="5C2D91"/></a:accent5>
      <a:accent6><a:srgbClr val="005B5E"/></a:accent6>
      <a:hlink><a:srgbClr val="{_PBI_BLUE}"/></a:hlink>
      <a:folHlink><a:srgbClr val="5C2D91"/></a:folHlink>
    </a:clrScheme>
    <a:fontScheme name="PBI">
      <a:majorFont><a:latin typeface="Segoe UI Semibold"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>
      <a:minorFont><a:latin typeface="Segoe UI"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont>
    </a:fontScheme>
    <a:fmtScheme name="PBI">
      <a:fillStyleLst>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
      </a:fillStyleLst>
      <a:lnStyleLst>
        <a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>
        <a:ln w="25400"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>
        <a:ln w="38100"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>
      </a:lnStyleLst>
      <a:effectStyleLst>
        <a:effectStyle><a:effectLst/></a:effectStyle>
        <a:effectStyle><a:effectLst/></a:effectStyle>
        <a:effectStyle><a:effectLst/></a:effectStyle>
      </a:effectStyleLst>
      <a:bgFillStyleLst>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
      </a:bgFillStyleLst>
    </a:fmtScheme>
  </a:themeElements>
</a:theme>"""


# ═══════════════════════════════════════════════════════════════════════
#  Utilities
# ═══════════════════════════════════════════════════════════════════════

_SHAPE_ID_COUNTER = 100


def _next_id() -> int:
    global _SHAPE_ID_COUNTER
    _SHAPE_ID_COUNTER += 1
    return _SHAPE_ID_COUNTER


def _xml_esc(text: str) -> str:
    """XML-escape a string."""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))
