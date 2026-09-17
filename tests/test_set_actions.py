"""
Tests for Sprint 122 — Set Actions & Interactive Parity.

Covers:
- Deepened set-value action extraction (source_field, clearing_behavior, activation)
- Sheet-navigate action extraction (target_sheet, field_mappings)
- Parameter action extraction (target_parameter)
- Set action → hidden slicer + bookmarks + action button generation
- Navigation action → action button with destinationPage
- Parameter action → slicer visual generation
"""

import json
import os
import sys
import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'powerbi_import'))

from tableau_export.extract_tableau_data import TableauExtractor
from powerbi_import.pbip_generator import PowerBIProjectGenerator


def _make_extractor():
    ext = TableauExtractor.__new__(TableauExtractor)
    ext.workbook_data = {}
    return ext


def _make_generator():
    gen = PowerBIProjectGenerator.__new__(PowerBIProjectGenerator)
    gen._field_map = {}
    gen._main_table = 'Sales'
    gen._set_action_bookmarks = []
    gen._motion_chart_bookmarks = []
    return gen


# ── Extraction: Set-Value Actions ───────────────────────────────────

class TestSetValueActionExtraction(unittest.TestCase):
    """Tests for deepened set-value action extraction."""

    def _extract(self, action_xml):
        ext = _make_extractor()
        root = ET.fromstring(
            f'<workbook><actions>{action_xml}</actions></workbook>')
        ext.extract_workbook_actions(root)
        return ext.workbook_data.get('actions', [])

    def test_basic_set_value_fields(self):
        xml = '''<action type="set-value" name="Select Region" set="[Region Set]"
                         source-field="[Region]" activation="select">
                   <set name="[Region Set]" field="[Region]" behavior="assign"/>
                 </action>'''
        actions = self._extract(xml)
        self.assertEqual(len(actions), 1)
        a = actions[0]
        self.assertEqual(a['type'], 'set-value')
        self.assertEqual(a['name'], 'Select Region')
        self.assertEqual(a['target_set'], 'Region Set')
        self.assertEqual(a['target_field'], 'Region')
        self.assertEqual(a['assign_behavior'], 'assign')

    def test_source_field_from_attribute(self):
        xml = '''<action type="set-value" name="Pick" source-field="[Product]">
                   <set name="[My Set]" field="[Product]" behavior="add"/>
                 </action>'''
        actions = self._extract(xml)
        self.assertEqual(actions[0]['source_field'], 'Product')

    def test_source_field_falls_back_to_set_field(self):
        xml = '''<action type="set-value" name="Pick">
                   <set name="[My Set]" field="[Category]" behavior="remove"/>
                 </action>'''
        actions = self._extract(xml)
        self.assertEqual(actions[0]['source_field'], 'Category')

    def test_assign_behavior_add(self):
        xml = '''<action type="set-value" name="Add">
                   <set name="[S]" field="[F]" behavior="add"/>
                 </action>'''
        actions = self._extract(xml)
        self.assertEqual(actions[0]['assign_behavior'], 'add')

    def test_assign_behavior_remove(self):
        xml = '''<action type="set-value" name="Remove">
                   <set name="[S]" field="[F]" behavior="remove"/>
                 </action>'''
        actions = self._extract(xml)
        self.assertEqual(actions[0]['assign_behavior'], 'remove')

    def test_clearing_behavior_captured(self):
        xml = '''<action type="set-value" name="Clear" clearing="server-side">
                   <set name="[S]" field="[F]" behavior="assign"/>
                 </action>'''
        actions = self._extract(xml)
        self.assertEqual(actions[0]['clearing_behavior'], 'server-side')

    def test_clearing_behavior_default_keep(self):
        xml = '''<action type="set-value" name="NoClearing">
                   <set name="[S]" field="[F]" behavior="assign"/>
                 </action>'''
        actions = self._extract(xml)
        self.assertEqual(actions[0]['clearing_behavior'], 'keep')

    def test_activation_from_run_on(self):
        xml = '''<action type="set-value" name="Hover" run-on="hover">
                   <set name="[S]" field="[F]" behavior="assign"/>
                 </action>'''
        actions = self._extract(xml)
        self.assertEqual(actions[0]['activation'], 'hover')

    def test_activation_default_select(self):
        xml = '''<action type="set-value" name="Default">
                   <set name="[S]" field="[F]" behavior="assign"/>
                 </action>'''
        actions = self._extract(xml)
        self.assertEqual(actions[0]['activation'], 'select')

    def test_set_name_from_action_attribute(self):
        xml = '''<action type="set-value" name="A" set-name="[Top N Set]">
                   <set name="[Top N Set]" field="[ID]" behavior="assign"/>
                 </action>'''
        actions = self._extract(xml)
        self.assertEqual(actions[0]['set_name'], 'Top N Set')


# ── Extraction: Sheet-Navigate Actions ──────────────────────────────

class TestNavigateActionExtraction(unittest.TestCase):
    """Tests for sheet-navigate action extraction."""

    def _extract(self, action_xml):
        ext = _make_extractor()
        root = ET.fromstring(
            f'<workbook><actions>{action_xml}</actions></workbook>')
        ext.extract_workbook_actions(root)
        return ext.workbook_data.get('actions', [])

    def test_navigate_target_sheet_from_worksheets(self):
        xml = '''<action type="sheet-navigate" name="Go Detail">
                   <source worksheet="Overview"/>
                   <target worksheet="Detail"/>
                 </action>'''
        actions = self._extract(xml)
        self.assertEqual(actions[0]['type'], 'sheet-navigate')
        self.assertEqual(actions[0]['target_sheet'], 'Detail')

    def test_navigate_target_sheet_fallback(self):
        xml = '''<action type="sheet-navigate" name="Go" target-sheet="Fallback"/>'''
        actions = self._extract(xml)
        self.assertEqual(actions[0]['target_sheet'], 'Fallback')

    def test_navigate_field_mappings(self):
        xml = '''<action type="sheet-navigate" name="Drill">
                   <target worksheet="Detail"/>
                   <field-mapping source-field="[Region]" target-field="[Region]"/>
                   <field-mapping source-field="[Year]" target-field="[Year]"/>
                 </action>'''
        actions = self._extract(xml)
        fm = actions[0].get('field_mappings', [])
        self.assertEqual(len(fm), 2)
        self.assertEqual(fm[0]['source'], 'Region')
        self.assertEqual(fm[1]['target'], 'Year')

    def test_navigate_no_field_mappings(self):
        xml = '''<action type="sheet-navigate" name="Simple">
                   <target worksheet="Other"/>
                 </action>'''
        actions = self._extract(xml)
        self.assertNotIn('field_mappings', actions[0])


# ── Extraction: Parameter Actions ───────────────────────────────────

class TestParameterActionExtraction(unittest.TestCase):
    """Tests for parameter action extraction."""

    def _extract(self, action_xml):
        ext = _make_extractor()
        root = ET.fromstring(
            f'<workbook><actions>{action_xml}</actions></workbook>')
        ext.extract_workbook_actions(root)
        return ext.workbook_data.get('actions', [])

    def test_param_basic_fields(self):
        xml = '''<action type="param" name="Set Year" param="[Year Param]"
                         source-field="[Year]"/>'''
        actions = self._extract(xml)
        a = actions[0]
        self.assertEqual(a['type'], 'param')
        self.assertEqual(a['parameter'], '[Year Param]')
        self.assertEqual(a['source_field'], 'Year')

    def test_param_target_parameter_from_attribute(self):
        xml = '''<action type="param" name="Set Y" param="[Top N]"
                         source-field="[Count]"/>'''
        actions = self._extract(xml)
        self.assertEqual(actions[0]['target_parameter'], 'Top N')

    def test_param_target_parameter_from_nested_element(self):
        xml = '''<action type="param" name="Nested" param=""
                         source-field="[Val]">
                   <param name="[Deep Param]"/>
                 </action>'''
        actions = self._extract(xml)
        self.assertEqual(actions[0]['target_parameter'], 'Deep Param')


# ── Generation: Set Action Artifacts ────────────────────────────────

class TestGenerateSetActionArtifacts(unittest.TestCase):
    """Tests for _generate_set_action_artifacts."""

    def setUp(self):
        self.gen = _make_generator()
        self.tmpdir = tempfile.mkdtemp()
        self.visuals_dir = os.path.join(self.tmpdir, 'visuals')
        os.makedirs(self.visuals_dir, exist_ok=True)

    def _run(self, actions, field_map=None):
        if field_map:
            self.gen._field_map = field_map
        return self.gen._generate_set_action_artifacts(
            self.visuals_dir, actions, 0, 'Dashboard',
            {'parameters': []})

    def test_creates_slicer_and_button(self):
        actions = [{
            'type': 'set-value', 'name': 'Region Filter',
            'set_name': 'Region Set', 'source_field': 'Region',
            'target_set': 'Region Set', 'target_field': 'Region',
            'assign_behavior': 'assign'
        }]
        count, bms = self._run(actions)
        # Should create 2 visuals: hidden slicer + action button
        self.assertEqual(count, 2)
        # Should create 2 bookmarks: active + clear
        self.assertEqual(len(bms), 2)

    def test_slicer_is_hidden(self):
        actions = [{
            'type': 'set-value', 'name': 'S',
            'set_name': 'S', 'source_field': 'F',
            'target_set': 'S', 'target_field': 'F',
            'assign_behavior': 'assign'
        }]
        self._run(actions)
        # Find the slicer visual
        for d in os.listdir(self.visuals_dir):
            vpath = os.path.join(self.visuals_dir, d, 'visual.json')
            if os.path.exists(vpath):
                with open(vpath) as f:
                    data = json.load(f)
                if data.get('visual', {}).get('visualType') == 'slicer':
                    self.assertTrue(data.get('isHidden', False))
                    return
        self.fail('No slicer visual found')

    def test_button_is_bookmark_type(self):
        actions = [{
            'type': 'set-value', 'name': 'Pick',
            'set_name': 'S', 'source_field': 'F',
            'target_set': 'S', 'target_field': 'F',
            'assign_behavior': 'assign'
        }]
        self._run(actions)
        for d in os.listdir(self.visuals_dir):
            vpath = os.path.join(self.visuals_dir, d, 'visual.json')
            if os.path.exists(vpath):
                with open(vpath) as f:
                    data = json.load(f)
                if data.get('visual', {}).get('visualType') == 'actionButton':
                    action_props = data['visual']['objects']['action'][0]['properties']
                    self.assertEqual(
                        action_props['type']['expr']['Literal']['Value'],
                        "'Bookmark'")
                    return
        self.fail('No action button found')

    def test_bookmarks_have_display_names(self):
        actions = [{
            'type': 'set-value', 'name': 'My Action',
            'set_name': 'S', 'source_field': 'F',
            'target_set': 'S', 'target_field': 'F',
            'assign_behavior': 'assign'
        }]
        _, bms = self._run(actions)
        names = [bm['displayName'] for bm in bms]
        self.assertIn('My Action (Apply)', names)
        self.assertIn('My Action (Clear)', names)

    def test_skips_action_without_source_field(self):
        actions = [{
            'type': 'set-value', 'name': 'Empty',
            'set_name': 'S', 'source_field': '',
            'target_set': 'S', 'target_field': '',
            'assign_behavior': 'assign'
        }]
        count, bms = self._run(actions)
        self.assertEqual(count, 0)
        self.assertEqual(len(bms), 0)

    def test_field_map_resolves_table(self):
        actions = [{
            'type': 'set-value', 'name': 'T',
            'set_name': 'S', 'source_field': 'Category',
            'target_set': 'S', 'target_field': 'Category',
            'assign_behavior': 'assign'
        }]
        field_map = {'Category': {'table': 'Products'}}
        self._run(actions, field_map=field_map)
        # Find slicer and check Entity reference
        for d in os.listdir(self.visuals_dir):
            vpath = os.path.join(self.visuals_dir, d, 'visual.json')
            if os.path.exists(vpath):
                with open(vpath) as f:
                    data = json.load(f)
                if data.get('visual', {}).get('visualType') == 'slicer':
                    proj = data['visual']['query']['queryState']['Values']['projections'][0]
                    entity = proj['field']['Column']['Expression']['SourceRef']['Entity']
                    self.assertEqual(entity, 'Products')
                    return
        self.fail('No slicer found')

    def test_migration_note_on_slicer(self):
        actions = [{
            'type': 'set-value', 'name': 'X',
            'set_name': 'MySet', 'source_field': 'Col',
            'target_set': 'MySet', 'target_field': 'Col',
            'assign_behavior': 'assign'
        }]
        self._run(actions)
        for d in os.listdir(self.visuals_dir):
            vpath = os.path.join(self.visuals_dir, d, 'visual.json')
            if os.path.exists(vpath):
                with open(vpath) as f:
                    data = json.load(f)
                if data.get('visual', {}).get('visualType') == 'slicer':
                    notes = [a['value'] for a in data.get('annotations', [])]
                    self.assertTrue(any('MySet' in n for n in notes))
                    return
        self.fail('No slicer found')


# ── Generation: Navigation Buttons ──────────────────────────────────

class TestGenerateNavigationButtons(unittest.TestCase):
    """Tests for _generate_navigation_buttons."""

    def setUp(self):
        self.gen = _make_generator()
        self.tmpdir = tempfile.mkdtemp()
        self.visuals_dir = os.path.join(self.tmpdir, 'visuals')
        os.makedirs(self.visuals_dir, exist_ok=True)

    def test_creates_button_with_destination(self):
        actions = [{
            'type': 'sheet-navigate', 'name': 'Go Detail',
            'target_sheet': 'Detail View',
            'target_worksheets': ['Detail View']
        }]
        page_map = {'Detail View': 'ReportSection_Detail'}
        count = self.gen._generate_navigation_buttons(
            self.visuals_dir, actions, 0, page_map)
        self.assertEqual(count, 1)
        # Check destinationPage property
        for d in os.listdir(self.visuals_dir):
            vpath = os.path.join(self.visuals_dir, d, 'visual.json')
            if os.path.exists(vpath):
                with open(vpath) as f:
                    data = json.load(f)
                action_props = data['visual']['objects']['action'][0]['properties']
                dest = action_props.get('destinationPage', {})
                self.assertIn('ReportSection_Detail',
                              dest.get('expr', {}).get('Literal', {}).get('Value', ''))
                return
        self.fail('No nav button found')

    def test_drillthrough_field_annotations(self):
        actions = [{
            'type': 'sheet-navigate', 'name': 'Drill',
            'target_sheet': 'Detail',
            'target_worksheets': ['Detail'],
            'field_mappings': [
                {'source': 'Region', 'target': 'Region'},
                {'source': 'Year', 'target': 'Year'}
            ]
        }]
        self.gen._generate_navigation_buttons(
            self.visuals_dir, actions, 0, {})
        for d in os.listdir(self.visuals_dir):
            vpath = os.path.join(self.visuals_dir, d, 'visual.json')
            if os.path.exists(vpath):
                with open(vpath) as f:
                    data = json.load(f)
                annos = data.get('annotations', [])
                dt_annos = [a for a in annos if a.get('name') == 'DrillThroughFields']
                self.assertEqual(len(dt_annos), 1)
                val = dt_annos[0]['value']
                self.assertTrue('Region' in val and 'Year' in val)
                return
        self.fail('No nav button found')

    def test_fallback_to_target_worksheets_list(self):
        actions = [{
            'type': 'sheet-navigate', 'name': 'Go',
            'target_worksheets': ['Sheet2']
        }]
        count = self.gen._generate_navigation_buttons(
            self.visuals_dir, actions, 0, {})
        self.assertEqual(count, 1)


# ── Generation: Parameter Action Slicers ────────────────────────────

class TestGenerateParameterActionSlicers(unittest.TestCase):
    """Tests for _generate_parameter_action_slicers."""

    def setUp(self):
        self.gen = _make_generator()
        self.tmpdir = tempfile.mkdtemp()
        self.visuals_dir = os.path.join(self.tmpdir, 'visuals')
        os.makedirs(self.visuals_dir, exist_ok=True)

    def test_creates_slicer_for_param_action(self):
        actions = [{'type': 'param', 'name': 'Set Year',
                     'parameter': 'Year Param',
                     'target_parameter': 'Year Param',
                     'source_field': 'Year'}]
        converted = {'parameters': [{'name': 'Year Param', 'domain_type': 'list'}]}
        count = self.gen._generate_parameter_action_slicers(
            self.visuals_dir, actions, 0, converted)
        self.assertEqual(count, 1)

    def test_slicer_mode_dropdown_for_list(self):
        actions = [{'type': 'param', 'name': 'P',
                     'target_parameter': 'P', 'source_field': 'F'}]
        converted = {'parameters': [{'name': 'P', 'domain_type': 'list'}]}
        self.gen._generate_parameter_action_slicers(
            self.visuals_dir, actions, 0, converted)
        for d in os.listdir(self.visuals_dir):
            vpath = os.path.join(self.visuals_dir, d, 'visual.json')
            if os.path.exists(vpath):
                with open(vpath) as f:
                    data = json.load(f)
                mode = data['visual']['objects']['data'][0]['properties']['mode']
                self.assertEqual(mode['expr']['Literal']['Value'], "'Dropdown'")
                return
        self.fail('No slicer found')

    def test_slicer_mode_between_for_range(self):
        actions = [{'type': 'param', 'name': 'R',
                     'target_parameter': 'R', 'source_field': 'V'}]
        converted = {'parameters': [{'name': 'R', 'domain_type': 'range'}]}
        self.gen._generate_parameter_action_slicers(
            self.visuals_dir, actions, 0, converted)
        for d in os.listdir(self.visuals_dir):
            vpath = os.path.join(self.visuals_dir, d, 'visual.json')
            if os.path.exists(vpath):
                with open(vpath) as f:
                    data = json.load(f)
                mode = data['visual']['objects']['data'][0]['properties']['mode']
                self.assertEqual(mode['expr']['Literal']['Value'], "'Between'")
                return
        self.fail('No slicer found')

    def test_skips_param_without_name(self):
        actions = [{'type': 'param', 'name': 'Bad',
                     'target_parameter': '', 'parameter': '',
                     'source_field': 'F'}]
        count = self.gen._generate_parameter_action_slicers(
            self.visuals_dir, actions, 0, {'parameters': []})
        self.assertEqual(count, 0)

    def test_migration_note_on_param_slicer(self):
        actions = [{'type': 'param', 'name': 'Set Top N',
                     'target_parameter': 'Top N', 'source_field': 'N'}]
        converted = {'parameters': [{'name': 'Top N'}]}
        self.gen._generate_parameter_action_slicers(
            self.visuals_dir, actions, 0, converted)
        for d in os.listdir(self.visuals_dir):
            vpath = os.path.join(self.visuals_dir, d, 'visual.json')
            if os.path.exists(vpath):
                with open(vpath) as f:
                    data = json.load(f)
                notes = [a['value'] for a in data.get('annotations', [])]
                self.assertTrue(any('Top N' in n for n in notes))
                return
        self.fail('No slicer found')


if __name__ == '__main__':
    unittest.main()
