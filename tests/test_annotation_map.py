"""
Tests for Sprint 121 — Annotation & Map Migration enhancements.

Covers:
- Enhanced annotation extraction (font formatting, target marks, area annotations)
- Map options extraction (zoom level, center coordinates, layer types)
- Annotation textbox overlay generation in PBIPGenerator
- Map config builders in visual_generator (build_map_config, build_map_layer_config)
- MAP_BASE_STYLE_MAP
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tableau_export.extract_tableau_data import TableauExtractor
from powerbi_import.visual_generator import (
    MAP_BASE_STYLE_MAP,
    build_map_config,
    build_map_layer_config,
    _build_map_config,
)


def _make_extractor():
    return TableauExtractor.__new__(TableauExtractor)


# ═══════════════════════════════════════════════════════════════════
# Annotation Extraction Tests
# ═══════════════════════════════════════════════════════════════════

class TestExtractAnnotationsEnhanced(unittest.TestCase):
    """Tests for enhanced annotation extraction with formatting and targets."""

    def setUp(self):
        self.ext = _make_extractor()

    def test_basic_annotation(self):
        ws = ET.fromstring('''
        <worksheet>
            <annotation type="point">
                <formatted-text><run>Hello World</run></formatted-text>
                <point x="100" y="200"/>
            </annotation>
        </worksheet>''')
        anns = self.ext.extract_annotations(ws)
        self.assertEqual(len(anns), 1)
        self.assertEqual(anns[0]['text'], 'Hello World')
        self.assertEqual(anns[0]['type'], 'point')
        self.assertEqual(anns[0]['position']['x'], '100')
        self.assertEqual(anns[0]['position']['y'], '200')

    def test_annotation_font_formatting(self):
        ws = ET.fromstring('''
        <worksheet>
            <annotation type="point">
                <formatted-text>
                    <run fontsize="14" fontcolor="#FF0000" bold="true">Bold Red</run>
                </formatted-text>
                <point x="10" y="20"/>
            </annotation>
        </worksheet>''')
        anns = self.ext.extract_annotations(ws)
        self.assertEqual(len(anns), 1)
        fmt = anns[0]['formatting']
        self.assertEqual(fmt['font_size'], '14')
        self.assertEqual(fmt['font_color'], '#FF0000')
        self.assertTrue(fmt['bold'])

    def test_annotation_italic_formatting(self):
        ws = ET.fromstring('''
        <worksheet>
            <annotation type="point">
                <formatted-text>
                    <run italic="true">Italic text</run>
                </formatted-text>
                <point x="0" y="0"/>
            </annotation>
        </worksheet>''')
        anns = self.ext.extract_annotations(ws)
        self.assertTrue(anns[0]['formatting']['italic'])

    def test_annotation_no_formatting(self):
        ws = ET.fromstring('''
        <worksheet>
            <annotation type="point">
                <formatted-text><run>Plain text</run></formatted-text>
                <point x="0" y="0"/>
            </annotation>
        </worksheet>''')
        anns = self.ext.extract_annotations(ws)
        self.assertNotIn('formatting', anns[0])

    def test_area_annotation(self):
        ws = ET.fromstring('''
        <worksheet>
            <area-annotation>
                <formatted-text><run>Area note</run></formatted-text>
                <rect x="50" y="60" w="200" h="100"/>
            </area-annotation>
        </worksheet>''')
        anns = self.ext.extract_annotations(ws)
        self.assertEqual(len(anns), 1)
        self.assertEqual(anns[0]['type'], 'area')
        self.assertEqual(anns[0]['position']['w'], '200')
        self.assertEqual(anns[0]['position']['h'], '100')

    def test_point_annotation_tag(self):
        ws = ET.fromstring('''
        <worksheet>
            <point-annotation>
                <formatted-text><run>Point note</run></formatted-text>
                <point x="10" y="20"/>
            </point-annotation>
        </worksheet>''')
        anns = self.ext.extract_annotations(ws)
        self.assertEqual(anns[0]['type'], 'point')

    def test_annotation_target_mark(self):
        ws = ET.fromstring('''
        <worksheet>
            <annotation type="mark">
                <formatted-text><run>Target annotation</run></formatted-text>
                <point x="0" y="0"/>
                <target field="[Sales]" value="1000"/>
            </annotation>
        </worksheet>''')
        anns = self.ext.extract_annotations(ws)
        self.assertIn('target_mark', anns[0])
        self.assertEqual(anns[0]['target_mark']['field'], 'Sales')
        self.assertEqual(anns[0]['target_mark']['value'], '1000')

    def test_annotation_no_target(self):
        ws = ET.fromstring('''
        <worksheet>
            <annotation type="point">
                <formatted-text><run>No target</run></formatted-text>
                <point x="0" y="0"/>
            </annotation>
        </worksheet>''')
        anns = self.ext.extract_annotations(ws)
        self.assertNotIn('target_mark', anns[0])

    def test_annotation_empty_text_skipped(self):
        ws = ET.fromstring('''
        <worksheet>
            <annotation type="point">
                <formatted-text><run></run></formatted-text>
                <point x="0" y="0"/>
            </annotation>
        </worksheet>''')
        anns = self.ext.extract_annotations(ws)
        self.assertEqual(len(anns), 0)

    def test_multiple_annotations(self):
        ws = ET.fromstring('''
        <worksheet>
            <annotation type="point">
                <formatted-text><run>First</run></formatted-text>
                <point x="10" y="20"/>
            </annotation>
            <annotation type="area">
                <formatted-text><run>Second</run></formatted-text>
                <point x="30" y="40"/>
            </annotation>
        </worksheet>''')
        anns = self.ext.extract_annotations(ws)
        self.assertEqual(len(anns), 2)
        self.assertEqual(anns[0]['text'], 'First')
        self.assertEqual(anns[1]['text'], 'Second')

    def test_annotation_multirun_text(self):
        ws = ET.fromstring('''
        <worksheet>
            <annotation type="point">
                <formatted-text>
                    <run fontsize="12">Hello </run>
                    <run bold="true">World</run>
                </formatted-text>
                <point x="0" y="0"/>
            </annotation>
        </worksheet>''')
        anns = self.ext.extract_annotations(ws)
        self.assertEqual(anns[0]['text'], 'Hello World')
        # Formatting comes from first run
        self.assertEqual(anns[0]['formatting']['font_size'], '12')

    def test_annotation_direct_position_attrs(self):
        """Annotation with x/y/w/h directly on the element."""
        ws = ET.fromstring('''
        <worksheet>
            <annotation type="area" x="5" y="10" w="200" h="80">
                <formatted-text><run>Direct pos</run></formatted-text>
            </annotation>
        </worksheet>''')
        anns = self.ext.extract_annotations(ws)
        self.assertEqual(anns[0]['position']['x'], '5')
        self.assertEqual(anns[0]['position']['w'], '200')

    def test_no_annotations(self):
        ws = ET.fromstring('<worksheet/>')
        self.assertEqual(self.ext.extract_annotations(ws), [])


# ═══════════════════════════════════════════════════════════════════
# Map Options Extraction Tests
# ═══════════════════════════════════════════════════════════════════

class TestExtractMapOptionsEnhanced(unittest.TestCase):
    """Tests for enhanced map extraction with zoom/center/layer types."""

    def setUp(self):
        self.ext = _make_extractor()

    def test_zoom_level(self):
        ws = ET.fromstring('''
        <worksheet>
            <map-options washout="0.0" style="normal" zoom-level="8"/>
        </worksheet>''')
        mo = self.ext.extract_map_options(ws)
        self.assertEqual(mo['zoom_level'], 8)

    def test_zoom_level_float(self):
        ws = ET.fromstring('''
        <worksheet>
            <map-options style="normal" zoom-level="5.7"/>
        </worksheet>''')
        mo = self.ext.extract_map_options(ws)
        self.assertEqual(mo['zoom_level'], 5)

    def test_center_coordinates(self):
        ws = ET.fromstring('''
        <worksheet>
            <map-options style="normal"
                         center-latitude="48.8566"
                         center-longitude="2.3522"/>
        </worksheet>''')
        mo = self.ext.extract_map_options(ws)
        self.assertAlmostEqual(mo['center_lat'], 48.8566)
        self.assertAlmostEqual(mo['center_lon'], 2.3522)

    def test_center_alt_attrs(self):
        ws = ET.fromstring('''
        <worksheet>
            <map-options style="normal"
                         center-lat="40.7128"
                         center-lon="-74.0060"/>
        </worksheet>''')
        mo = self.ext.extract_map_options(ws)
        self.assertAlmostEqual(mo['center_lat'], 40.7128)
        self.assertAlmostEqual(mo['center_lon'], -74.0060)

    def test_no_zoom_or_center(self):
        ws = ET.fromstring('''
        <worksheet>
            <map-options style="normal"/>
        </worksheet>''')
        mo = self.ext.extract_map_options(ws)
        self.assertNotIn('zoom_level', mo)
        self.assertNotIn('center_lat', mo)

    def test_layer_type_and_opacity(self):
        ws = ET.fromstring('''
        <worksheet>
            <map-options style="normal">
                <map-layer name="Heatmap" enabled="true" type="heat" opacity="0.8"/>
                <map-layer name="Points" enabled="true" type="bubble"/>
            </map-options>
        </worksheet>''')
        mo = self.ext.extract_map_options(ws)
        self.assertEqual(len(mo['layers']), 2)
        self.assertEqual(mo['layers'][0]['type'], 'heat')
        self.assertAlmostEqual(mo['layers'][0]['opacity'], 0.8)
        self.assertEqual(mo['layers'][1]['type'], 'bubble')
        self.assertNotIn('opacity', mo['layers'][1])

    def test_no_map_options(self):
        ws = ET.fromstring('<worksheet/>')
        self.assertEqual(self.ext.extract_map_options(ws), {})

    def test_full_map_options(self):
        ws = ET.fromstring('''
        <worksheet>
            <map-options washout="0.3" style="satellite" pan-zoom="true"
                         unit="km" zoom-level="12"
                         center-latitude="51.5074" center-longitude="-0.1278">
                <map-layer name="Base" enabled="true" type="polygon" opacity="0.6"/>
            </map-options>
            <mapsources>
                <mapsource provider="mapbox"/>
            </mapsources>
        </worksheet>''')
        mo = self.ext.extract_map_options(ws)
        self.assertEqual(mo['washout'], '0.3')
        self.assertEqual(mo['style'], 'satellite')
        self.assertEqual(mo['zoom_level'], 12)
        self.assertAlmostEqual(mo['center_lat'], 51.5074)
        self.assertAlmostEqual(mo['center_lon'], -0.1278)
        self.assertEqual(mo['provider'], 'mapbox')
        self.assertEqual(len(mo['layers']), 1)
        self.assertEqual(mo['layers'][0]['type'], 'polygon')
        self.assertAlmostEqual(mo['layers'][0]['opacity'], 0.6)


# ═══════════════════════════════════════════════════════════════════
# Visual Generator — Map Config Builder Tests
# ═══════════════════════════════════════════════════════════════════

class TestMapBaseStyleMap(unittest.TestCase):
    """Tests for MAP_BASE_STYLE_MAP constant."""

    def test_known_styles(self):
        self.assertEqual(MAP_BASE_STYLE_MAP['normal'], 'road')
        self.assertEqual(MAP_BASE_STYLE_MAP['dark'], 'road_dark')
        self.assertEqual(MAP_BASE_STYLE_MAP['satellite'], 'aerial')
        self.assertEqual(MAP_BASE_STYLE_MAP['light'], 'grayscale_light')

    def test_streets_alias(self):
        self.assertEqual(MAP_BASE_STYLE_MAP['streets'], 'road')


class TestBuildMapConfig(unittest.TestCase):
    """Tests for build_map_config(worksheet)."""

    def test_empty_worksheet(self):
        self.assertEqual(build_map_config({}), {})

    def test_none_worksheet(self):
        self.assertEqual(build_map_config(None), {})

    def test_basic_style(self):
        ws = {'map_options': {'style': 'dark'}}
        cfg = build_map_config(ws)
        self.assertIn('objects', cfg)
        props = cfg['objects']['mapControls'][0]['properties']
        self.assertIn('road_dark', str(props['mapStyle']))

    def test_zoom_level(self):
        ws = {'map_options': {'style': 'normal', 'zoom_level': 10}}
        cfg = build_map_config(ws)
        props = cfg['objects']['mapControls'][0]['properties']
        self.assertIn('10', str(props['zoomLevel']))
        self.assertIn('false', str(props['autoZoom']))

    def test_center_coordinates(self):
        ws = {'map_options': {'style': 'normal', 'center_lat': 48.85, 'center_lon': 2.35}}
        cfg = build_map_config(ws)
        props = cfg['objects']['mapControls'][0]['properties']
        self.assertIn('48.85', str(props['latitude']))
        self.assertIn('2.35', str(props['longitude']))

    def test_auto_zoom_when_no_level(self):
        ws = {'map_options': {'style': 'normal'}}
        cfg = build_map_config(ws)
        props = cfg['objects']['mapControls'][0]['properties']
        self.assertIn('true', str(props['autoZoom']))

    def test_unknown_style_defaults_to_road(self):
        ws = {'map_options': {'style': 'unknown_style'}}
        cfg = build_map_config(ws)
        props = cfg['objects']['mapControls'][0]['properties']
        self.assertIn('road', str(props['mapStyle']))


class TestBuildMapLayerConfig(unittest.TestCase):
    """Tests for build_map_layer_config(worksheet)."""

    def test_empty_layers(self):
        self.assertEqual(build_map_layer_config({}), {})
        self.assertEqual(build_map_layer_config(None), {})

    def test_heat_layer(self):
        ws = {'map_options': {'layers': [
            {'name': 'heatmap', 'enabled': True, 'type': 'heat', 'opacity': 0.7}
        ]}}
        cfg = build_map_layer_config(ws)
        self.assertIn('heatmap', cfg)
        self.assertIn('true', str(cfg['heatmap'][0]['properties']['show']))

    def test_bubble_layer(self):
        ws = {'map_options': {'layers': [
            {'name': 'Points', 'enabled': True, 'type': 'bubble', 'opacity': 0.5}
        ]}}
        cfg = build_map_layer_config(ws)
        self.assertIn('bubbles', cfg)

    def test_polygon_layer(self):
        ws = {'map_options': {'layers': [
            {'name': 'Regions', 'enabled': True, 'type': 'polygon'}
        ]}}
        cfg = build_map_layer_config(ws)
        self.assertIn('shape', cfg)

    def test_disabled_layer_skipped(self):
        ws = {'map_options': {'layers': [
            {'name': 'Off', 'enabled': False, 'type': 'heat'}
        ]}}
        cfg = build_map_layer_config(ws)
        self.assertEqual(cfg, {})

    def test_opacity_to_transparency(self):
        ws = {'map_options': {'layers': [
            {'name': 'Bubbles', 'enabled': True, 'type': 'circle', 'opacity': 0.8}
        ]}}
        cfg = build_map_layer_config(ws)
        # opacity 0.8 → transparency 20%
        self.assertIn('20', str(cfg['bubbles'][0]['properties']['transparency']))

    def test_no_layers_key(self):
        ws = {'map_options': {'style': 'normal'}}
        self.assertEqual(build_map_layer_config(ws), {})


# ═══════════════════════════════════════════════════════════════════
# PBIPGenerator — Annotation Overlay Tests
# ═══════════════════════════════════════════════════════════════════

class TestAnnotationOverlay(unittest.TestCase):
    """Tests for _create_annotation_overlay in PBIPGenerator."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from powerbi_import.pbip_generator import PowerBIProjectGenerator
        self.gen = PowerBIProjectGenerator.__new__(PowerBIProjectGenerator)
        self.gen.MIN_VISUAL_WIDTH = 50
        self.gen.MIN_VISUAL_HEIGHT = 30

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_creates_textbox_visual(self):
        ann = {'text': 'Test note', 'position': {'x': '10', 'y': '20', 'w': '100', 'h': '30'}}
        parent = {'position': {'x': 0, 'y': 0, 'w': 400, 'h': 300}}
        self.gen._create_annotation_overlay(self.tmpdir, ann, parent, 1.0, 1.0, 5)
        # Should have created a visual directory
        dirs = os.listdir(self.tmpdir)
        self.assertEqual(len(dirs), 1)
        vj = json.load(open(os.path.join(self.tmpdir, dirs[0], 'visual.json')))
        self.assertEqual(vj['visual']['visualType'], 'textbox')

    def test_migration_note_annotation(self):
        ann = {'text': 'Note', 'position': {'x': '0', 'y': '0'}}
        parent = {'position': {'x': 0, 'y': 0}}
        self.gen._create_annotation_overlay(self.tmpdir, ann, parent, 1.0, 1.0, 0)
        dirs = os.listdir(self.tmpdir)
        vj = json.load(open(os.path.join(self.tmpdir, dirs[0], 'visual.json')))
        self.assertIn('annotations', vj)
        self.assertEqual(vj['annotations'][0]['name'], 'MigrationNote')

    def test_formatting_applied(self):
        ann = {
            'text': 'Bold Red',
            'position': {'x': '0', 'y': '0'},
            'formatting': {'font_size': '16', 'font_color': '#FF0000', 'bold': True}
        }
        parent = {'position': {'x': 0, 'y': 0}}
        self.gen._create_annotation_overlay(self.tmpdir, ann, parent, 1.0, 1.0, 0)
        dirs = os.listdir(self.tmpdir)
        vj = json.load(open(os.path.join(self.tmpdir, dirs[0], 'visual.json')))
        runs = vj['visual']['objects']['general'][0]['properties']['paragraphs'][0]['textRuns']
        self.assertEqual(runs[0]['value'], 'Bold Red')
        self.assertEqual(runs[0]['textStyle']['fontWeight'], 'bold')
        self.assertEqual(runs[0]['textStyle']['color'], '#FF0000')
        self.assertIn('16', runs[0]['textStyle']['fontSize'])

    def test_empty_text_no_output(self):
        ann = {'text': '', 'position': {'x': '0', 'y': '0'}}
        parent = {'position': {'x': 0, 'y': 0}}
        self.gen._create_annotation_overlay(self.tmpdir, ann, parent, 1.0, 1.0, 0)
        self.assertEqual(os.listdir(self.tmpdir), [])

    def test_position_offset_from_parent(self):
        ann = {'text': 'Offset', 'position': {'x': '50', 'y': '30', 'w': '100', 'h': '40'}}
        parent = {'position': {'x': 100, 'y': 200, 'w': 400, 'h': 300}}
        self.gen._create_annotation_overlay(self.tmpdir, ann, parent, 1.0, 1.0, 0)
        dirs = os.listdir(self.tmpdir)
        vj = json.load(open(os.path.join(self.tmpdir, dirs[0], 'visual.json')))
        pos = vj['position']
        # x should be parent_x + ann_x = 100 + 50 = 150
        self.assertEqual(pos['x'], 150)
        # y should be parent_y + ann_y = 200 + 30 = 230
        self.assertEqual(pos['y'], 230)

    def test_aarrggbb_color_conversion(self):
        ann = {
            'text': 'Alpha color',
            'position': {'x': '0', 'y': '0'},
            'formatting': {'font_color': '#FF00FF00'}
        }
        parent = {'position': {'x': 0, 'y': 0}}
        self.gen._create_annotation_overlay(self.tmpdir, ann, parent, 1.0, 1.0, 0)
        dirs = os.listdir(self.tmpdir)
        vj = json.load(open(os.path.join(self.tmpdir, dirs[0], 'visual.json')))
        runs = vj['visual']['objects']['general'][0]['properties']['paragraphs'][0]['textRuns']
        # #FF00FF00 → #00FF00
        self.assertEqual(runs[0]['textStyle']['color'], '#00FF00')


# ═══════════════════════════════════════════════════════════════════
# Map Zoom/Center in visual config objects
# ═══════════════════════════════════════════════════════════════════

class TestMapZoomCenterInVisualConfig(unittest.TestCase):
    """Tests for zoom_level and center in map visual config objects."""

    def test_zoom_level_in_map_objects(self):
        """Verify map options with zoom_level set autoZoom=false and zoomLevel."""
        ws = {'map_options': {'style': 'normal', 'zoom_level': 8}}
        cfg = build_map_config(ws)
        self.assertIn('mapControls', cfg['objects'])
        props = cfg['objects']['mapControls'][0]['properties']
        self.assertIn('zoomLevel', props)
        self.assertIn('8', str(props['zoomLevel']))
        self.assertIn('false', str(props['autoZoom']))

    def test_center_in_map_objects(self):
        ws = {'map_options': {'style': 'dark', 'center_lat': 40.0, 'center_lon': -74.0}}
        cfg = build_map_config(ws)
        props = cfg['objects']['mapControls'][0]['properties']
        self.assertIn('latitude', props)
        self.assertIn('longitude', props)
        self.assertIn('40.0', str(props['latitude']))
        self.assertIn('-74.0', str(props['longitude']))


if __name__ == '__main__':
    unittest.main()
