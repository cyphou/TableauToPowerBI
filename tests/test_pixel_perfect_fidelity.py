"""
Tests for pixel-perfect visual fidelity between Tableau source and PBIR output.

Covers:
- Position scale never UPSCALES (clamped to ≤ 1.0) — preserves Tableau pixel
  coordinates as-is when objects fit within dashboard size.
- Title fontFamily extracted from <run fontname="..."> and applied to PBIR
  visualContainerObjects.title.
- Label fontFamily extracted from <label font-family="..."> and applied to
  PBIR objects.labels.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'powerbi_import'))

from tableau_export.extract_tableau_data import TableauExtractor
from powerbi_import.pbip_generator import PowerBIProjectGenerator


def _make_extractor():
    return TableauExtractor.__new__(TableauExtractor)


def _make_generator():
    gen = PowerBIProjectGenerator.__new__(PowerBIProjectGenerator)
    return gen


# ── Position fidelity ──────────────────────────────────────────────

class TestPositionFidelity(unittest.TestCase):
    """Position scale is clamped to ≤ 1.0 — never upscales objects."""

    def _run_dashboard(self, db, ws_name='Sheet1'):
        gen = _make_generator()
        gen._field_map = {}
        gen._find_worksheet = lambda worksheets, name: {  # type: ignore[assignment]
            'name': name, 'fields': [], 'filters': [], 'mark_encoding': {},
        }
        tmpdir = tempfile.mkdtemp()
        try:
            pages_dir = os.path.join(tmpdir, 'pages')
            os.makedirs(pages_dir, exist_ok=True)
            gen._create_dashboard_pages(
                pages_dir, [db],
                [{'name': ws_name, 'fields': [], 'filters': [], 'mark_encoding': {}}],
                {'calculations': [], 'actions': []},
                {},
            )
            visuals_dir = os.path.join(pages_dir, 'ReportSection', 'visuals')
            visual_dirs = sorted(os.listdir(visuals_dir))
            positions = []
            for vd in visual_dirs:
                with open(os.path.join(visuals_dir, vd, 'visual.json'), 'r') as f:
                    positions.append(json.load(f).get('position', {}))
            return positions
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_no_upscale_when_objects_fit_within_dashboard(self):
        """Object at (100, 50) size 200×100 in a 1280×720 dashboard
        must keep exact pixel coords — no rescaling to fill the canvas."""
        db = {
            'name': 'Test',
            'size': {'width': 1280, 'height': 720},
            'filters': [],
            'objects': [
                {'type': 'worksheetReference', 'worksheetName': 'Sheet1',
                 'position': {'x': 100, 'y': 50, 'w': 200, 'h': 100}},
            ],
        }
        positions = self._run_dashboard(db)
        self.assertEqual(len(positions), 1)
        p = positions[0]
        # Pixel-perfect: exact Tableau coordinates preserved.
        self.assertEqual(p['x'], 100)
        self.assertEqual(p['y'], 50)
        self.assertEqual(p['width'], 200)
        self.assertEqual(p['height'], 100)

    def test_downscale_still_applies_when_object_overflows(self):
        """When an object overflows past the dashboard size, scale must
        downscale proportionally to fit (pre-existing behaviour)."""
        db = {
            'name': 'Test',
            'size': {'width': 1000, 'height': 500},
            'filters': [],
            'objects': [
                {'type': 'worksheetReference', 'worksheetName': 'Sheet1',
                 'position': {'x': 0, 'y': 0, 'w': 2000, 'h': 1000}},
            ],
        }
        positions = self._run_dashboard(db)
        self.assertEqual(len(positions), 1)
        p = positions[0]
        # 2000 wide overflow → scaled to fit 1000-wide page.
        self.assertLessEqual(p['width'], 1000)
        self.assertLessEqual(p['height'], 500)

    def test_partial_fill_does_not_anisotropic_stretch(self):
        """When objects fill ~75% of canvas, NO upscaling must occur
        (previous behaviour distorted positions to fill canvas)."""
        db = {
            'name': 'Test',
            'size': {'width': 1000, 'height': 600},
            'filters': [],
            'objects': [
                {'type': 'worksheetReference', 'worksheetName': 'Sheet1',
                 'position': {'x': 0, 'y': 0, 'w': 750, 'h': 450}},
            ],
        }
        positions = self._run_dashboard(db)
        p = positions[0]
        # Exact pixels preserved — no stretching to fill 1000×600.
        self.assertEqual(p['x'], 0)
        self.assertEqual(p['y'], 0)
        self.assertEqual(p['width'], 750)
        self.assertEqual(p['height'], 450)


# ── Title font fidelity ────────────────────────────────────────────

class TestTitleFontFidelity(unittest.TestCase):
    """Title fontFamily extracted from <run fontname=...> and emitted to PBIR."""

    def test_extract_title_fontname(self):
        ext = _make_extractor()
        ws_xml = '''<worksheet name="Sheet1">
            <title>
                <formatted-text>
                    <run fontname="Helvetica" fontsize="14" fontcolor="#222222">Sales</run>
                </formatted-text>
            </title>
        </worksheet>'''
        ws = ET.fromstring(ws_xml)
        fmt = ext._extract_title_format(ws)
        self.assertEqual(fmt.get('font_family'), 'Helvetica')
        self.assertEqual(fmt.get('font_size'), '14')
        self.assertEqual(fmt.get('font_color'), '#222222')

    def test_extract_title_no_fontname(self):
        ext = _make_extractor()
        ws_xml = '''<worksheet name="Sheet1">
            <title><formatted-text><run fontsize="12">Sales</run></formatted-text></title>
        </worksheet>'''
        fmt = ext._extract_title_format(ET.fromstring(ws_xml))
        self.assertNotIn('font_family', fmt)
        self.assertEqual(fmt.get('font_size'), '12')

    def test_apply_title_fontfamily_to_visual(self):
        """The PBIR visual.json must include fontFamily on title properties."""
        gen = _make_generator()
        gen._field_map = {}
        gen._main_table = 'Table'
        gen._find_worksheet = lambda worksheets, name: {  # type: ignore[assignment]
            'name': name, 'fields': [], 'filters': [], 'mark_encoding': {},
            'title_format': {'font_family': 'Arial', 'font_size': '16'},
        }
        db = {
            'name': 'Test',
            'size': {'width': 1280, 'height': 720},
            'filters': [],
            'objects': [
                {'type': 'worksheetReference', 'worksheetName': 'Sheet1',
                 'position': {'x': 0, 'y': 0, 'w': 400, 'h': 300}},
            ],
        }
        ws_data = [{
            'name': 'Sheet1', 'fields': [], 'filters': [], 'mark_encoding': {},
            'title_format': {'font_family': 'Arial', 'font_size': '16'},
        }]

        tmpdir = tempfile.mkdtemp()
        try:
            pages_dir = os.path.join(tmpdir, 'pages')
            os.makedirs(pages_dir, exist_ok=True)
            gen._create_dashboard_pages(
                pages_dir, [db], ws_data,
                {'calculations': [], 'actions': []}, {},
            )
            visuals_dir = os.path.join(pages_dir, 'ReportSection', 'visuals')
            visual_dir = os.listdir(visuals_dir)[0]
            with open(os.path.join(visuals_dir, visual_dir, 'visual.json'), 'r') as f:
                visual_json = json.load(f)
            title_props = (visual_json['visual']['visualContainerObjects']
                           ['title'][0]['properties'])
            self.assertIn('fontFamily', title_props)
            # Property is wrapped in literal expression: {"expr": {"Literal": {"Value": "'Arial'"}}}
            ff = title_props['fontFamily']
            self.assertEqual(ff['expr']['Literal']['Value'], "'Arial'")
            self.assertEqual(title_props['fontSize']['expr']['Literal']['Value'], '16D')
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── Label font fidelity ────────────────────────────────────────────

class TestLabelFontFidelity(unittest.TestCase):
    """Label fontFamily extracted from encoding and applied to PBIR labels."""

    def test_extract_label_font_family(self):
        ext = _make_extractor()
        ws_xml = '''<worksheet name="Sheet1">
            <table>
                <panes><pane>
                    <encodings>
                        <label column="[fed].[Sales]" show-label="true"
                               font-size="11" font-family="Verdana"
                               font-color="#333333" />
                    </encodings>
                </pane></panes>
            </table>
        </worksheet>'''
        ws = ET.fromstring(ws_xml)
        encoding = ext.extract_mark_encoding(ws)
        self.assertIn('label', encoding)
        self.assertEqual(encoding['label'].get('font_family'), 'Verdana')
        self.assertEqual(encoding['label'].get('font_size'), '11')
        self.assertEqual(encoding['label'].get('font_color'), '#333333')
        self.assertTrue(encoding['label'].get('show'))

    def test_apply_label_font_family_in_labels_objects(self):
        """_build_label_objects must include fontFamily when label_info has it."""
        gen = _make_generator()
        objects = {}
        mark_encoding = {
            'label': {
                'show': True,
                'font_size': '11',
                'font_family': 'Verdana',
                'font_color': '#333333',
            }
        }
        gen._build_label_objects(objects, {}, mark_encoding)
        self.assertIn('labels', objects)
        props = objects['labels'][0]['properties']
        self.assertIn('fontFamily', props)
        self.assertEqual(props['fontFamily']['expr']['Literal']['Value'], "'Verdana'")
        self.assertEqual(props['fontSize']['expr']['Literal']['Value'], '11D')


# ── Annotation font fidelity ───────────────────────────────────────

class TestAnnotationFontFidelity(unittest.TestCase):
    """Annotation fontname/font-family extracted and applied to PBI textbox overlays."""

    def test_extract_annotation_font_family(self):
        ext = _make_extractor()
        ws_xml = '''<worksheet name="Sheet1">
            <annotations>
                <annotation type="point">
                    <formatted-text>
                        <run fontname="Calibri" fontsize="12" fontcolor="#222222"
                             bold="true">Important point</run>
                    </formatted-text>
                    <point x="100" y="200"/>
                </annotation>
            </annotations>
        </worksheet>'''
        ws = ET.fromstring(ws_xml)
        anns = ext.extract_annotations(ws)
        self.assertEqual(len(anns), 1)
        fmt = anns[0].get('formatting', {})
        self.assertEqual(fmt.get('font_family'), 'Calibri')
        self.assertEqual(fmt.get('font_size'), '12')
        self.assertEqual(fmt.get('font_color'), '#222222')
        self.assertTrue(fmt.get('bold'))

    def test_annotation_fontfamily_applied_to_textbox(self):
        gen = _make_generator()
        gen._make_visual_position = lambda pos, sx, sy, vc: {
            "x": float(pos.get('x', 0)), "y": float(pos.get('y', 0)),
            "z": vc * 1000, "height": float(pos.get('h', 40)),
            "width": float(pos.get('w', 200)), "tabOrder": vc * 1000,
        }
        annotation = {
            'text': 'Important note',
            'position': {'x': 50, 'y': 60, 'w': 200, 'h': 40},
            'formatting': {
                'font_size': '12',
                'font_family': 'Calibri',
                'font_color': '#222222',
                'bold': True,
            },
        }
        tmpdir = tempfile.mkdtemp()
        try:
            visuals_dir = os.path.join(tmpdir, 'visuals')
            os.makedirs(visuals_dir, exist_ok=True)
            gen._create_annotation_overlay(visuals_dir, annotation, {}, 1.0, 1.0, 0)
            # Read back the visual.json
            visual_dirs = [d for d in os.listdir(visuals_dir)
                           if os.path.isdir(os.path.join(visuals_dir, d))]
            self.assertEqual(len(visual_dirs), 1)
            with open(os.path.join(visuals_dir, visual_dirs[0], 'visual.json'),
                      encoding='utf-8') as f:
                vj = json.load(f)
            paragraphs = vj['visual']['objects']['general'][0]['properties']['paragraphs']
            style = paragraphs[0]['textRuns'][0]['textStyle']
            self.assertEqual(style.get('fontFamily'), 'Calibri')
            self.assertEqual(style.get('fontSize'), '12.0pt')
            self.assertEqual(style.get('color'), '#222222')
            self.assertEqual(style.get('fontWeight'), 'bold')
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── Tableau worksheet formatting → PBI override ────────────────────

class TestTableauFontOverrides(unittest.TestCase):
    """_apply_tableau_font_overrides propagates worksheet font-size/family to objects."""

    def _import_overrides(self):
        from powerbi_import.visual_generator import _apply_tableau_font_overrides
        return _apply_tableau_font_overrides

    def test_no_op_without_formatting(self):
        apply = self._import_overrides()
        visual_obj = {"objects": {"labels": [{"properties": {"show": {}}}]}}
        apply({}, visual_obj)
        # Should not have injected fontSize/fontFamily
        props = visual_obj["objects"]["labels"][0]["properties"]
        self.assertNotIn('fontSize', props)
        self.assertNotIn('fontFamily', props)

    def test_overrides_propagate_to_targets(self):
        apply = self._import_overrides()
        worksheet = {
            'formatting': {
                'worksheet_style': {
                    'font-size': '14',
                    'font-family': 'Tahoma',
                }
            }
        }
        visual_obj = {
            "objects": {
                "labels": [{"properties": {"show": {}}}],
                "categoryAxis": [{"properties": {"show": {}}}],
                "valueAxis": [{"properties": {"show": {}}}],
                "legend": [{"properties": {"show": {}}}],
            }
        }
        apply(worksheet, visual_obj)
        for tgt in ("labels", "categoryAxis", "valueAxis", "legend"):
            props = visual_obj["objects"][tgt][0]["properties"]
            self.assertIn('fontSize', props, f"{tgt} missing fontSize")
            self.assertIn('fontFamily', props, f"{tgt} missing fontFamily")
            self.assertEqual(props['fontSize']['expr']['Literal']['Value'], '14.0D')
            self.assertEqual(props['fontFamily']['expr']['Literal']['Value'], "'Tahoma'")

    def test_override_does_not_overwrite_existing(self):
        apply = self._import_overrides()
        worksheet = {
            'formatting': {
                'worksheet_style': {'font-size': '14', 'font-family': 'Tahoma'}
            }
        }
        # Pre-populate fontSize on labels — must not be overwritten
        existing_size = {"expr": {"Literal": {"Value": "20D"}}}
        visual_obj = {
            "objects": {
                "labels": [{"properties": {"fontSize": existing_size}}],
                "valueAxis": [{"properties": {"show": {}}}],
            }
        }
        apply(worksheet, visual_obj)
        labels_props = visual_obj["objects"]["labels"][0]["properties"]
        self.assertEqual(labels_props['fontSize']['expr']['Literal']['Value'], '20D')
        # But fontFamily was missing → applied
        self.assertEqual(labels_props['fontFamily']['expr']['Literal']['Value'], "'Tahoma'")
        # valueAxis had nothing → both applied
        va_props = visual_obj["objects"]["valueAxis"][0]["properties"]
        self.assertEqual(va_props['fontSize']['expr']['Literal']['Value'], '14.0D')

    def test_only_font_family_no_size(self):
        apply = self._import_overrides()
        worksheet = {
            'formatting': {'worksheet_style': {'font-family': 'Arial'}}
        }
        visual_obj = {"objects": {"labels": [{"properties": {}}]}}
        apply(worksheet, visual_obj)
        props = visual_obj["objects"]["labels"][0]["properties"]
        self.assertEqual(props['fontFamily']['expr']['Literal']['Value'], "'Arial'")
        self.assertNotIn('fontSize', props)

    def test_invalid_font_size_ignored(self):
        apply = self._import_overrides()
        worksheet = {
            'formatting': {'worksheet_style': {'font-size': 'not-a-number'}}
        }
        visual_obj = {"objects": {"labels": [{"properties": {}}]}}
        apply(worksheet, visual_obj)
        props = visual_obj["objects"]["labels"][0]["properties"]
        self.assertNotIn('fontSize', props)


# ── Tableau background / border → PBI override ─────────────────────

class TestTableauBackgroundBorder(unittest.TestCase):
    """_apply_tableau_background_border propagates background_color and border-*."""

    def _import_apply(self):
        from powerbi_import.visual_generator import _apply_tableau_background_border
        return _apply_tableau_background_border

    def test_no_op_without_formatting(self):
        apply = self._import_apply()
        visual_obj = {"objects": {}}
        apply({}, visual_obj)
        self.assertNotIn('background', visual_obj.get('objects', {}))
        self.assertNotIn('border', visual_obj.get('objects', {}))

    def test_background_color_applied(self):
        apply = self._import_apply()
        worksheet = {'formatting': {'background_color': '#F5F5F5'}}
        visual_obj = {"objects": {}}
        apply(worksheet, visual_obj)
        self.assertIn('background', visual_obj['objects'])
        bg_props = visual_obj['objects']['background'][0]['properties']
        self.assertEqual(bg_props['show']['expr']['Literal']['Value'], 'true')
        self.assertEqual(
            bg_props['color']['solid']['color']['expr']['Literal']['Value'],
            "'#F5F5F5'",
        )

    def test_border_applied_with_color_and_width(self):
        apply = self._import_apply()
        worksheet = {
            'formatting': {
                'worksheet_style': {
                    'border-style': 'solid',
                    'border-color': '#333333',
                    'border-width': '2pt',
                }
            }
        }
        visual_obj = {"objects": {}}
        apply(worksheet, visual_obj)
        self.assertIn('border', visual_obj['objects'])
        bp = visual_obj['objects']['border'][0]['properties']
        self.assertEqual(bp['show']['expr']['Literal']['Value'], 'true')
        self.assertEqual(
            bp['color']['solid']['color']['expr']['Literal']['Value'],
            "'#333333'",
        )
        self.assertEqual(bp['radius']['expr']['Literal']['Value'], '2.0D')

    def test_border_skipped_when_style_none(self):
        apply = self._import_apply()
        worksheet = {
            'formatting': {'worksheet_style': {'border-style': 'none'}}
        }
        visual_obj = {"objects": {}}
        apply(worksheet, visual_obj)
        self.assertNotIn('border', visual_obj['objects'])

    def test_does_not_overwrite_existing_background(self):
        apply = self._import_apply()
        worksheet = {'formatting': {'background_color': '#FFFFFF'}}
        existing = [{"properties": {"show": {"expr": {"Literal": {"Value": "false"}}}}}]
        visual_obj = {"objects": {"background": existing}}
        apply(worksheet, visual_obj)
        # Existing background preserved, not overwritten
        self.assertIs(visual_obj['objects']['background'], existing)

    def test_invalid_color_ignored(self):
        apply = self._import_apply()
        worksheet = {'formatting': {'background_color': 'red'}}  # no #
        visual_obj = {"objects": {}}
        apply(worksheet, visual_obj)
        self.assertNotIn('background', visual_obj['objects'])


# ── Tableau Æ line-break sentinel artifact ─────────────────────────

class TestTableauLineBreakSentinel(unittest.TestCase):
    """Tableau Desktop emits unstyled <run>Æ&#10;</run> elements as soft
    line-break sentinels inside <formatted-text>. The literal U+00C6 is
    invisible in Tableau but renders as "Æ" in PBI, browsers, and plain
    text. _clean_tableau_run_text strips the sentinel from unstyled runs
    while preserving styled content and surrounding newlines.
    """

    def _clean(self):
        from tableau_export.extract_tableau_data import _clean_tableau_run_text
        return _clean_tableau_run_text

    @staticmethod
    def _run(text, **attrs):
        el = ET.Element('run', attrs)
        el.text = text
        return el

    def test_unstyled_AE_dropped(self):
        clean = self._clean()
        # Æ followed by newline, no attributes
        out = clean(self._run('\u00c6\n'))
        self.assertEqual(out, '\n')

    def test_unstyled_AE_with_fontalignment_dropped(self):
        # fontalignment alone is not a styling attr — sentinel still stripped
        clean = self._clean()
        out = clean(self._run('\u00c6\n', fontalignment='1'))
        self.assertEqual(out, '\n')

    def test_styled_AE_preserved(self):
        # Æ inside a run with explicit font/color/size is real content
        clean = self._clean()
        out = clean(self._run('\u00c6', fontname='Arial', fontsize='12'))
        self.assertEqual(out, '\u00c6')

    def test_AE_with_other_text_preserved(self):
        clean = self._clean()
        out = clean(self._run('Caf\u00e9 \u00c6 bar'))  # Café Æ bar — real text
        self.assertEqual(out, 'Caf\u00e9 \u00c6 bar')

    def test_nbsp_sentinel_dropped(self):
        clean = self._clean()
        out = clean(self._run('\u00a0'))
        self.assertEqual(out, '')

    def test_normal_text_unchanged(self):
        clean = self._clean()
        out = clean(self._run('Tableau de bord'))
        self.assertEqual(out, 'Tableau de bord')

    def test_dashboard_textbox_strips_sentinel_run(self):
        """End-to-end: <formatted-text> with Æ sentinel run produces clean
        text_runs in the extractor output."""
        from tableau_export.extract_tableau_data import _clean_tableau_run_text
        # Simulate the UC80 pattern:
        # <run fontcolor='#fff'>Tableau de bord</run>
        # <run>Æ&#10;</run>
        # <run bold='true'>ARGOS</run>
        runs = [
            self._run('Tableau de bord', fontcolor='#ffffff', fontsize='10'),
            self._run('\u00c6\n'),
            self._run('ARGOS', bold='true', fontcolor='#ffffff', fontsize='14'),
        ]
        cleaned = [_clean_tableau_run_text(r) for r in runs]
        self.assertEqual(cleaned[0], 'Tableau de bord')
        self.assertEqual(cleaned[1], '\n')  # newline survives, Æ dropped
        self.assertEqual(cleaned[2], 'ARGOS')


# ── Text alignment fidelity (Sprint 206) ───────────────────────────

class TestTextAlignment(unittest.TestCase):
    """Tableau text zones carry per-run horizontal alignment
    (fontalignment) and zone-level vertical anchoring (vertical-align).
    Sprint 206 maps these onto PBI textbox paragraphs
    (horizontalTextAlignment) and the general verticalAlignment property.
    """

    # ── Extractor-side mapping helpers ──

    def _map_h(self):
        from tableau_export.extract_tableau_data import _map_text_alignment
        return _map_text_alignment

    def _map_v(self):
        from tableau_export.extract_tableau_data import _map_vertical_alignment
        return _map_vertical_alignment

    def test_horizontal_numeric_left(self):
        self.assertEqual(self._map_h()('1'), 'left')

    def test_horizontal_numeric_center(self):
        self.assertEqual(self._map_h()('2'), 'center')

    def test_horizontal_numeric_right(self):
        self.assertEqual(self._map_h()('3'), 'right')

    def test_horizontal_numeric_justify(self):
        self.assertEqual(self._map_h()('4'), 'justify')

    def test_horizontal_named_center(self):
        self.assertEqual(self._map_h()('center'), 'center')

    def test_horizontal_centre_british_spelling(self):
        self.assertEqual(self._map_h()('centre'), 'center')

    def test_horizontal_unknown_returns_empty(self):
        self.assertEqual(self._map_h()('diagonal'), '')

    def test_horizontal_empty_returns_empty(self):
        self.assertEqual(self._map_h()(''), '')
        self.assertEqual(self._map_h()(None), '')

    def test_vertical_named_middle(self):
        self.assertEqual(self._map_v()('center'), 'middle')

    def test_vertical_named_bottom(self):
        self.assertEqual(self._map_v()('bottom'), 'bottom')

    def test_vertical_numeric_top(self):
        self.assertEqual(self._map_v()('1'), 'top')

    def test_vertical_unknown_returns_empty(self):
        self.assertEqual(self._map_v()('sideways'), '')

    # ── Generator-side paragraph emission ──

    def test_paragraph_emits_horizontal_alignment_from_run(self):
        gen = _make_generator()
        obj = {
            'content': 'Hello',
            'text_runs': [{'text': 'Hello', 'alignment': 'center'}],
        }
        paras = gen._parse_rich_text_runs(obj)
        self.assertEqual(len(paras), 1)
        self.assertEqual(paras[0].get('horizontalTextAlignment'), 'center')

    def test_paragraph_falls_back_to_zone_alignment(self):
        gen = _make_generator()
        obj = {
            'content': 'Hi',
            'text_runs': [{'text': 'Hi'}],
            'text_align': 'right',
        }
        paras = gen._parse_rich_text_runs(obj)
        self.assertEqual(paras[0].get('horizontalTextAlignment'), 'right')

    def test_no_alignment_omits_property(self):
        gen = _make_generator()
        obj = {'content': 'Plain', 'text_runs': [{'text': 'Plain'}]}
        paras = gen._parse_rich_text_runs(obj)
        self.assertNotIn('horizontalTextAlignment', paras[0])

    def test_multiple_paragraphs_each_carry_alignment(self):
        gen = _make_generator()
        obj = {
            'content': 'A\nB',
            'text_runs': [{'text': 'Line A\nLine B', 'alignment': 'center'}],
        }
        paras = gen._parse_rich_text_runs(obj)
        self.assertEqual(len(paras), 2)
        for p in paras:
            self.assertEqual(p.get('horizontalTextAlignment'), 'center')

    def test_fallback_plain_content_uses_zone_alignment(self):
        gen = _make_generator()
        obj = {'content': 'Just text', 'text_align': 'center'}
        paras = gen._parse_rich_text_runs(obj)
        self.assertEqual(len(paras), 1)
        self.assertEqual(paras[0].get('horizontalTextAlignment'), 'center')

    def test_textbox_emits_vertical_alignment(self):
        gen = _make_generator()
        gen._make_visual_position = lambda *a, **k: {  # type: ignore[assignment]
            'x': 0, 'y': 0, 'width': 100, 'height': 50, 'z': 0,
        }
        obj = {
            'content': 'Centered',
            'text_runs': [{'text': 'Centered', 'alignment': 'center'}],
            'vertical_align': 'middle',
        }
        tmpdir = tempfile.mkdtemp()
        try:
            gen._create_visual_textbox(tmpdir, obj, 1.0, 1.0, 0)
            vid = os.listdir(tmpdir)[0]
            with open(os.path.join(tmpdir, vid, 'visual.json'), 'r', encoding='utf-8') as f:
                data = json.load(f)
            props = data['visual']['objects']['general'][0]['properties']
            self.assertEqual(props.get('verticalAlignment'), 'middle')
            self.assertEqual(props['paragraphs'][0].get('horizontalTextAlignment'), 'center')
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_textbox_omits_vertical_alignment_when_absent(self):
        gen = _make_generator()
        gen._make_visual_position = lambda *a, **k: {  # type: ignore[assignment]
            'x': 0, 'y': 0, 'width': 100, 'height': 50, 'z': 0,
        }
        obj = {'content': 'Plain', 'text_runs': [{'text': 'Plain'}]}
        tmpdir = tempfile.mkdtemp()
        try:
            gen._create_visual_textbox(tmpdir, obj, 1.0, 1.0, 0)
            vid = os.listdir(tmpdir)[0]
            with open(os.path.join(tmpdir, vid, 'visual.json'), 'r', encoding='utf-8') as f:
                data = json.load(f)
            props = data['visual']['objects']['general'][0]['properties']
            self.assertNotIn('verticalAlignment', props)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestFloatingOverlap(unittest.TestCase):
    """Sprint 204 — floating zone overlay fidelity.

    When two dashboard zones occupy the same rectangle (e.g. a textbox
    backdrop behind a worksheet tableEx, as in Enterprise_Sales), the
    report-side healer ``_heal_visual_overlap_full`` staggers one of them
    by +32 px so PBI Desktop does not hide the duplicate. The choice of
    WHICH visual moves must be deterministic — keyed by z-order, not by
    the random UUID directory names that ``os.listdir`` returns.
    """

    @staticmethod
    def _visual(name, x, y, w, h, z, vtype='table'):
        return {
            'dir': f'/fake/visuals/{name}',
            'name': name,
            'json': {
                'visual': {'visualType': vtype},
                'position': {'x': x, 'y': y, 'width': w, 'height': h,
                             'z': z, 'tabOrder': z},
            },
        }

    def _state(self, visuals):
        return {'pages': [{'visuals': visuals}], '_dirty_files': set()}

    def _heal(self):
        from powerbi_import.self_healing_report import _heal_visual_overlap_full
        return _heal_visual_overlap_full

    def test_backdrop_stays_foreground_staggers(self):
        # Backdrop textbox z=0, foreground worksheet z=1000, same rect.
        backdrop = self._visual('aaa', 0, 108, 960, 540, 0, 'textbox')
        worksheet = self._visual('zzz', 0, 108, 960, 540, 1000, 'tableEx')
        state = self._state([backdrop, worksheet])
        repairs = self._heal()(state)
        self.assertEqual(repairs, 1)
        self.assertEqual(backdrop['json']['position']['x'], 0)
        self.assertEqual(backdrop['json']['position']['y'], 108)
        self.assertEqual(worksheet['json']['position']['x'], 32)
        self.assertEqual(worksheet['json']['position']['y'], 140)

    def test_deterministic_regardless_of_list_order(self):
        # Same visuals, reversed input order → identical outcome.
        b1 = self._visual('aaa', 0, 108, 960, 540, 0, 'textbox')
        w1 = self._visual('zzz', 0, 108, 960, 540, 1000, 'tableEx')
        self._heal()(self._state([b1, w1]))

        b2 = self._visual('aaa', 0, 108, 960, 540, 0, 'textbox')
        w2 = self._visual('zzz', 0, 108, 960, 540, 1000, 'tableEx')
        self._heal()(self._state([w2, b2]))  # reversed order

        self.assertEqual(b1['json']['position'], b2['json']['position'])
        self.assertEqual(w1['json']['position'], w2['json']['position'])

    def test_lowest_z_is_always_anchored(self):
        # The visual with the lowest z stays put; higher z moves.
        low = self._visual('mmm', 10, 20, 100, 100, 5)
        high = self._visual('nnn', 10, 20, 100, 100, 99)
        self._heal()(self._state([high, low]))  # higher z listed first
        self.assertEqual((low['json']['position']['x'], low['json']['position']['y']),
                         (10, 20))
        self.assertEqual((high['json']['position']['x'], high['json']['position']['y']),
                         (42, 52))

    def test_no_overlap_no_repair(self):
        a = self._visual('aaa', 0, 0, 100, 100, 0)
        b = self._visual('bbb', 200, 200, 100, 100, 1000)
        state = self._state([a, b])
        repairs = self._heal()(state)
        self.assertEqual(repairs, 0)
        self.assertEqual(a['json']['position']['x'], 0)
        self.assertEqual(b['json']['position']['x'], 200)

    def test_three_way_overlap_staggers_two(self):
        a = self._visual('a', 0, 0, 100, 100, 0)
        b = self._visual('b', 0, 0, 100, 100, 100)
        c = self._visual('c', 0, 0, 100, 100, 200)
        state = self._state([c, a, b])
        repairs = self._heal()(state)
        self.assertEqual(repairs, 2)
        # Lowest z anchored; both higher staggered by +32.
        self.assertEqual((a['json']['position']['x'], a['json']['position']['y']),
                         (0, 0))
        self.assertEqual((b['json']['position']['x'], b['json']['position']['y']),
                         (32, 32))
        self.assertEqual((c['json']['position']['x'], c['json']['position']['y']),
                         (32, 32))

    def test_visual_marked_dirty_when_staggered(self):
        a = self._visual('a', 5, 5, 50, 50, 0)
        b = self._visual('b', 5, 5, 50, 50, 1000)
        state = self._state([a, b])
        self._heal()(state)
        self.assertIn(os.path.join(b['dir'], 'visual.json'), state['_dirty_files'])
        self.assertNotIn(os.path.join(a['dir'], 'visual.json'), state['_dirty_files'])

    def test_tie_on_z_breaks_on_name(self):
        # Equal z → name decides which is anchored (deterministic).
        a = self._visual('alpha', 0, 0, 100, 100, 0)
        b = self._visual('beta', 0, 0, 100, 100, 0)
        self._heal()(self._state([b, a]))  # reversed
        # 'alpha' < 'beta' → alpha anchored, beta staggered.
        self.assertEqual((a['json']['position']['x'], a['json']['position']['y']),
                         (0, 0))
        self.assertEqual((b['json']['position']['x'], b['json']['position']['y']),
                         (32, 32))

    def test_missing_position_skipped(self):
        a = self._visual('a', 0, 0, 100, 100, 0)
        b = {'dir': '/fake/visuals/b', 'name': 'b',
             'json': {'visual': {'visualType': 'table'}}}  # no position
        state = self._state([a, b])
        repairs = self._heal()(state)
        self.assertEqual(repairs, 0)

    def test_sort_key_orders_by_z(self):
        from powerbi_import.self_healing_report import _overlap_sort_key
        low = self._visual('z', 0, 0, 1, 1, 0)
        high = self._visual('a', 0, 0, 1, 1, 500)
        self.assertLess(_overlap_sort_key(low), _overlap_sort_key(high))

    def test_sort_key_handles_missing_position(self):
        from powerbi_import.self_healing_report import _overlap_sort_key
        v = {'name': 'x', 'json': {}}
        # Should not raise; returns a tuple with default z=0.
        key = _overlap_sort_key(v)
        self.assertEqual(key[0], 0)


if __name__ == '__main__':
    unittest.main()
