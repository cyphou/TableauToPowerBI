"""
Unit tests for pbip_generator.py — Power BI Project generation.

Tests utility methods (_clean_field_name, _make_visual_position,
_build_visual_objects, _is_measure_field, _create_bookmarks,
_create_slicer_visual, _resolve_field_entity) and structural integrity
of generated visual JSON.
"""

import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'powerbi_import'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from pbip_generator import PowerBIProjectGenerator


# ═══════════════════════════════════════════════════════════════════════
# Helpers — create an instance without writing to disk
# ═══════════════════════════════════════════════════════════════════════

def _make_generator():
    """Create a PowerBIProjectGenerator with temp dirs."""
    return PowerBIProjectGenerator(
        output_dir=tempfile.mkdtemp()
    )


def _extract_regression_worksheet(xml, output_dir):
    """Exercise the public extraction API, including slices and pill metadata."""
    import xml.etree.ElementTree as ET
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))
    from extract_tableau_data import TableauExtractor
    extractor = TableauExtractor(
        os.path.join(output_dir, 'fixture.twb'), output_dir=output_dir)
    extractor.extract_worksheets(ET.fromstring(xml))
    worksheets = extractor.workbook_data['worksheets']
    if len(worksheets) != 1:
        raise AssertionError('Regression fixture must contain exactly one worksheet')
    return worksheets[0]


# ═══════════════════════════════════════════════════════════════════════
# _clean_field_name
# ═══════════════════════════════════════════════════════════════════════

class TestCleanFieldName(unittest.TestCase):
    """Test _clean_field_name removes Tableau derivation prefixes."""

    def setUp(self):
        self.gen = _make_generator()

    def test_none_prefix(self):
        self.assertEqual(self.gen._clean_field_name("none:Sales"), "Sales")

    def test_sum_prefix(self):
        self.assertEqual(self.gen._clean_field_name("sum:Amount"), "Amount")

    def test_avg_prefix(self):
        self.assertEqual(self.gen._clean_field_name("avg:Price"), "Price")

    def test_count_prefix(self):
        self.assertEqual(self.gen._clean_field_name("count:Rows"), "Rows")

    def test_min_max_prefix(self):
        self.assertEqual(self.gen._clean_field_name("min:Date"), "Date")
        self.assertEqual(self.gen._clean_field_name("max:Date"), "Date")

    def test_usr_prefix(self):
        self.assertEqual(self.gen._clean_field_name("usr:Custom Field"), "Custom Field")

    def test_year_prefix(self):
        self.assertEqual(self.gen._clean_field_name("yr:OrderDate"), "OrderDate")

    def test_attr_prefix(self):
        self.assertEqual(self.gen._clean_field_name("attr:Region"), "Region")

    def test_nk_suffix_removed(self):
        self.assertEqual(self.gen._clean_field_name("Sales:nk"), "Sales")

    def test_qk_suffix_removed(self):
        self.assertEqual(self.gen._clean_field_name("Date:qk"), "Date")

    def test_no_prefix_unchanged(self):
        self.assertEqual(self.gen._clean_field_name("Sales"), "Sales")

    def test_combined_prefix_and_suffix(self):
        result = self.gen._clean_field_name("sum:Revenue:nk")
        self.assertEqual(result, "Revenue")


# ═══════════════════════════════════════════════════════════════════════
# _make_visual_position
# ═══════════════════════════════════════════════════════════════════════

class TestMakeVisualPosition(unittest.TestCase):
    """Test _make_visual_position coordinate scaling."""

    def setUp(self):
        self.gen = _make_generator()

    def test_basic_scaling(self):
        pos = {"x": 100, "y": 50, "w": 400, "h": 300}
        result = self.gen._make_visual_position(pos, 2.0, 1.5, 1)
        self.assertEqual(result["x"], 200)
        self.assertEqual(result["y"], 75)
        self.assertEqual(result["width"], 800)
        self.assertEqual(result["height"], 450)

    def test_z_order(self):
        pos = {"x": 0, "y": 0, "w": 100, "h": 100}
        result = self.gen._make_visual_position(pos, 1.0, 1.0, 3)
        self.assertEqual(result["z"], 3000)
        self.assertEqual(result["tabOrder"], 3000)

    def test_missing_dimensions_defaults(self):
        pos = {}
        result = self.gen._make_visual_position(pos, 1.0, 1.0, 0)
        self.assertEqual(result["x"], 0)
        self.assertEqual(result["y"], 0)
        self.assertEqual(result["height"], 200)  # default h
        self.assertEqual(result["width"], 300)   # default w

    def test_rounding(self):
        pos = {"x": 33, "y": 17, "w": 123, "h": 89}
        result = self.gen._make_visual_position(pos, 1.33, 0.77, 1)
        # All should be rounded to int
        for key in ("x", "y", "width", "height"):
            self.assertIsInstance(result[key], int)


# ═══════════════════════════════════════════════════════════════════════
# _is_measure_field
# ═══════════════════════════════════════════════════════════════════════

class TestIsMeasureField(unittest.TestCase):
    """Test _is_measure_field detection."""

    def setUp(self):
        self.gen = _make_generator()

    def test_known_measure(self):
        self.gen._measure_names = {"Total Sales", "Profit Ratio"}
        self.assertTrue(self.gen._is_measure_field("Total Sales"))

    def test_known_measure_with_brackets(self):
        self.gen._measure_names = {"Total Sales"}
        self.assertTrue(self.gen._is_measure_field("[Total Sales]"))

    def test_unknown_field(self):
        self.gen._measure_names = {"Total Sales"}
        self.assertFalse(self.gen._is_measure_field("Region"))

    def test_no_measure_names_set(self):
        # No _measure_names attribute
        self.assertFalse(self.gen._is_measure_field("Sales"))


# ═══════════════════════════════════════════════════════════════════════
# _build_visual_objects
# ═══════════════════════════════════════════════════════════════════════

class TestBuildVisualObjects(unittest.TestCase):
    """Test _build_visual_objects — title, labels, legend, axes."""

    def setUp(self):
        self.gen = _make_generator()

    def test_title_not_in_objects(self):
        result = self.gen._build_visual_objects("My Chart", None, "clusteredBarChart")
        # Title is in visualContainerObjects, not in visual.objects
        self.assertNotIn("title", result)

    def test_no_ws_data_empty_objects(self):
        result = self.gen._build_visual_objects("Chart", None, "lineChart")
        self.assertNotIn("title", result)
        # Without ws_data, no extra objects
        self.assertNotIn("labels", result)
        self.assertNotIn("legend", result)

    def test_labels_enabled(self):
        ws_data = {
            "formatting": {"mark": {"mark-labels-show": "true"}},
            "mark_encoding": {}
        }
        result = self.gen._build_visual_objects("C", ws_data, "barChart")
        self.assertIn("labels", result)
        label_show = result["labels"][0]["properties"]["show"]["expr"]["Literal"]["Value"]
        self.assertEqual(label_show, "true")

    def test_legend_with_color_field(self):
        ws_data = {
            "formatting": {},
            "mark_encoding": {"color": {"field": "Category"}}
        }
        result = self.gen._build_visual_objects("C", ws_data, "barChart")
        self.assertIn("legend", result)

    def test_no_legend_for_measure_values(self):
        ws_data = {
            "formatting": {},
            "mark_encoding": {"color": {"field": "Multiple Values"}}
        }
        result = self.gen._build_visual_objects("C", ws_data, "barChart")
        self.assertNotIn("legend", result)

    def test_background_color(self):
        ws_data = {
            "formatting": {"background_color": "#FFFFFF"},
            "mark_encoding": {}
        }
        expected_background = [{
            "properties": {
                "color": {"solid": {
                    "color": {"expr": {"Literal": {"Value": "'#FFFFFF'"}}}
                }}
            }
        }]
        result = self.gen._build_visual_objects("C", ws_data, "clusteredBarChart")
        self.assertEqual(result["background"], expected_background)
        self.assertNotIn("visualContainerStyle", result)
        with tempfile.TemporaryDirectory() as visuals_dir:
            self.gen._create_visual_worksheet(
                visuals_dir, ws_data, {'worksheetName': 'C'},
                1.0, 1.0, 0, [], {'calculations': []})
            visual_ids = os.listdir(visuals_dir)
            self.assertEqual(len(visual_ids), 1)
            with open(os.path.join(visuals_dir, visual_ids[0], 'visual.json'),
                      encoding='utf-8') as visual_file:
                visual = json.load(visual_file)['visual']
        container_objects = visual['visualContainerObjects']
        self.assertEqual(set(container_objects), {'title', 'background'})
        self.assertEqual(container_objects['background'], expected_background)
        self.assertNotIn('background', visual['objects'])
        self.assertNotIn('visualContainerStyle', visual['objects'])

    def test_reference_lines(self):
        ws_data = {
            "formatting": {},
            "mark_encoding": {},
            "reference_lines": [
                {"value": 500, "label": "Target", "color": "#FF0000",
                 "style": "dashed"}
            ]
        }
        expected_axes = {
            "categoryAxis": [{"properties": {
                "show": {"expr": {"Literal": {"Value": "true"}}}
            }}],
            "valueAxis": [{"properties": {
                "show": {"expr": {"Literal": {"Value": "true"}}}
            }}],
        }
        with self.subTest(value=500):
            result = self.gen._build_visual_objects(
                "C", ws_data, "clusteredBarChart")
            self.assertEqual(result, {
                **expected_axes,
                "y1AxisReferenceLine": [{
                    "properties": {
                        "value": {"expr": {"Literal": {"Value": "500.0D"}}},
                        "show": {"expr": {"Literal": {"Value": "true"}}},
                        "displayName": {"expr": {"Literal": {"Value": "'Target'"}}},
                        "lineColor": {"solid": {
                            "color": {"expr": {"Literal": {"Value": "'#FF0000'"}}}
                        }},
                        "style": {"expr": {"Literal": {"Value": "'dashed'"}}},
                    },
                    "selector": {"id": "0"},
                }],
            })
        for value in ('', None):
            with self.subTest(value=value):
                empty_ws_data = {
                    **ws_data,
                    "reference_lines": [{
                        **ws_data["reference_lines"][0], "value": value
                    }],
                }
                result = self.gen._build_visual_objects(
                    "C", empty_ws_data, "clusteredBarChart")
                self.assertEqual(result, expected_axes)

    def test_conditional_formatting_gradient(self):
        ws_data = {
            "formatting": {},
            "mark_encoding": {
                "color": {
                    "field": "Sales",
                    "type": "quantitative",
                    "palette": "green-gold",
                    "palette_colors": ["#00FF00", "#FFD700"]
                }
            }
        }
        result = self.gen._build_visual_objects("C", ws_data, "barChart")
        self.assertIn("dataPoint", result)

    def test_axes_from_axes_data(self):
        ws_data = {
            "formatting": {},
            "mark_encoding": {},
            "axes": {
                "x": {"title": "Date"},
                "y": {"title": "Revenue"}
            }
        }
        result = self.gen._build_visual_objects("C", ws_data, "lineChart")
        cat_title = result["categoryAxis"][0]["properties"]["titleText"]["expr"]["Literal"]["Value"]
        val_title = result["valueAxis"][0]["properties"]["titleText"]["expr"]["Literal"]["Value"]
        self.assertIn("Date", cat_title)
        self.assertIn("Revenue", val_title)


# ═══════════════════════════════════════════════════════════════════════
# _create_bookmarks
# ═══════════════════════════════════════════════════════════════════════

class TestCreateBookmarks(unittest.TestCase):
    """Test _create_bookmarks from Tableau stories."""

    def setUp(self):
        self.gen = _make_generator()

    def test_empty_stories(self):
        result = self.gen._create_bookmarks([])
        self.assertEqual(result, [])

    def test_single_story_point(self):
        stories = [{
            "name": "MyStory",
            "story_points": [{"caption": "Overview"}]
        }]
        result = self.gen._create_bookmarks(stories)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["displayName"], "Overview")
        self.assertTrue(result[0]["name"].startswith("Bookmark_"))

    def test_multiple_story_points(self):
        stories = [{
            "name": "Sales Story",
            "story_points": [
                {"caption": "Point 1"},
                {"caption": "Point 2"},
                {"caption": "Point 3"},
            ]
        }]
        result = self.gen._create_bookmarks(stories)
        self.assertEqual(len(result), 3)

    def test_default_caption(self):
        stories = [{
            "name": "MyStory",
            "story_points": [{}]  # No caption
        }]
        result = self.gen._create_bookmarks(stories)
        self.assertIn("MyStory", result[0]["displayName"])

    def test_exploration_state_structure(self):
        stories = [{"name": "S", "story_points": [{"caption": "C"}]}]
        result = self.gen._create_bookmarks(stories)
        self.assertIn("explorationState", result[0])
        self.assertEqual(result[0]["explorationState"]["version"], "1.0")


class TestDashboardPageNames(unittest.TestCase):
    def setUp(self):
        self.gen = _make_generator()

    def test_blank_dashboard_name_uses_page_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            pages_dir = os.path.join(td, "pages")
            self.gen._create_dashboard_pages(
                pages_dir, [{"name": "  ", "objects": []}], [], {}, {})
            page_path = os.path.join(
                pages_dir, "ReportSection", "page.json")
            with open(page_path, encoding="utf-8") as stream:
                page = json.load(stream)
        self.assertEqual(page["displayName"], "Page 1")

    def test_blank_worksheet_name_uses_page_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            pages_dir = os.path.join(td, "pages")
            self.gen._create_fallback_page(
                pages_dir, [{"name": ""}], {})
            page_path = os.path.join(
                pages_dir, "ReportSection", "page.json")
            with open(page_path, encoding="utf-8") as stream:
                page = json.load(stream)
        self.assertEqual(page["displayName"], "Sheet 1")


# ═══════════════════════════════════════════════════════════════════════
# _create_slicer_visual
# ═══════════════════════════════════════════════════════════════════════

class TestCreateSlicerVisual(unittest.TestCase):
    """Test _create_slicer_visual."""

    def setUp(self):
        self.gen = _make_generator()

    def test_slicer_structure(self):
        result = self.gen._create_slicer_visual(
            "abc123", 10, 20, 200, 50, "Region", "Orders", 1
        )
        self.assertEqual(result["name"], "abc123")
        self.assertEqual(result["visual"]["visualType"], "slicer")
        self.assertEqual(result["position"]["x"], 10)
        self.assertEqual(result["position"]["y"], 20)
        self.assertEqual(result["position"]["width"], 200)
        self.assertEqual(result["position"]["height"], 50)

    def test_slicer_has_query_binding(self):
        result = self.gen._create_slicer_visual(
            "xyz", 0, 0, 100, 30, "Category", "Products", 0
        )
        query = result["visual"].get("query")
        self.assertIsNotNone(query)
        qs = query["queryState"]["Values"]["projections"][0]
        self.assertEqual(qs["field"]["Column"]["Property"], "Category")

    def test_slicer_dropdown_mode(self):
        result = self.gen._create_slicer_visual(
            "s1", 0, 0, 100, 30, "Status", "T", 0
        )
        data_obj = result["visual"]["objects"]["data"][0]
        mode = data_obj["properties"]["mode"]["expr"]["Literal"]["Value"]
        self.assertIn("Dropdown", mode)

    def test_slicer_schema(self):
        result = self.gen._create_slicer_visual(
            "s1", 0, 0, 100, 30, "X", "T", 0
        )
        self.assertIn("visualContainer", result["$schema"])


# ═══════════════════════════════════════════════════════════════════════
# _resolve_field_entity
# ═══════════════════════════════════════════════════════════════════════

class TestResolveFieldEntity(unittest.TestCase):
    """Test _resolve_field_entity field → (table, column) resolution."""

    def setUp(self):
        self.gen = _make_generator()

    def test_direct_match(self):
        self.gen._field_map = {"Sales": ("Orders", "Sales")}
        entity, prop = self.gen._resolve_field_entity("Sales")
        self.assertEqual(entity, "Orders")
        self.assertEqual(prop, "Sales")

    def test_bracket_stripped(self):
        self.gen._field_map = {"Sales": ("Orders", "Sales")}
        entity, prop = self.gen._resolve_field_entity("[Sales]")
        self.assertEqual(entity, "Orders")

    def test_fallback_no_map(self):
        self.gen._main_table = "DefaultTable"
        entity, prop = self.gen._resolve_field_entity("Unknown")
        self.assertEqual(entity, "DefaultTable")
        self.assertEqual(prop, "Unknown")


# ═══════════════════════════════════════════════════════════════════════
# Full project generation (integration — writes to temp dir)
# ═══════════════════════════════════════════════════════════════════════

class TestGenerateProject(unittest.TestCase):
    """Integration test — generate a full .pbip project from mock data."""

    def setUp(self):
        self.output_dir = tempfile.mkdtemp()
        self.gen = PowerBIProjectGenerator(
            output_dir=self.output_dir
        )
        self.objects = {
            "worksheets": [
                {"name": "Sheet1", "fields": [
                    {"name": "Category", "shelf": "rows"},
                    {"name": "Sales", "shelf": "columns"}
                ], "chart_type": "clusteredBarChart", "formatting": {},
                 "mark_encoding": {}, "filters": []}
            ],
            "dashboards": [
                {"name": "Dashboard1", "width": 1000, "height": 800,
                 "objects": [
                     {"type": "worksheet", "worksheetName": "Sheet1",
                      "position": {"x": 0, "y": 0, "w": 500, "h": 400}}
                 ], "colors": []}
            ],
            "datasources": [
                {
                    "name": "Sample",
                    "connection": {"type": "CSV", "details": {"filename": "data.csv"}},
                    "tables": [{"name": "Data", "columns": [
                        {"name": "Category", "datatype": "string"},
                        {"name": "Sales", "datatype": "real"},
                    ]}],
                    "calculations": [],
                    "relationships": []
                }
            ],
            "calculations": [],
            "parameters": [],
            "filters": [],
            "stories": [],
            "sets": [],
            "groups": [],
            "bins": [],
            "hierarchies": [],
            "user_filters": [],
            "actions": [],
            "custom_sql": [],
            "sort_orders": [],
            "aliases": [],
        }

    def tearDown(self):
        shutil.rmtree(self.output_dir, ignore_errors=True)

    def _generate(self):
        """Generate project, suppressing emoji-laden stdout."""
        old_stdout = sys.stdout
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
        try:
            self.gen.generate_project("Test", self.objects)
        finally:
            sys.stdout = old_stdout

    def test_project_creates_pbip_file(self):
        self._generate()
        pbip_path = os.path.join(self.output_dir, "Test", "Test.pbip")
        self.assertTrue(os.path.exists(pbip_path))

    def test_project_creates_report_dir(self):
        self._generate()
        report_dir = os.path.join(self.output_dir, "Test", "Test.Report")
        self.assertTrue(os.path.isdir(report_dir))

    def test_project_creates_semantic_model(self):
        self._generate()
        sm_dir = os.path.join(self.output_dir, "Test", "Test.SemanticModel")
        self.assertTrue(os.path.isdir(sm_dir))

    def test_pbip_contains_valid_json(self):
        self._generate()
        pbip_path = os.path.join(self.output_dir, "Test", "Test.pbip")
        with open(pbip_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertIn("version", data)

    def test_metadata_created(self):
        self._generate()
        meta_path = os.path.join(self.output_dir, "Test", "migration_metadata.json")
        self.assertTrue(os.path.exists(meta_path))
        with open(meta_path, 'r', encoding='utf-8') as f:
            meta = json.load(f)
        self.assertIn("objects_converted", meta)
        self.assertEqual(meta["objects_converted"]["worksheets"], 1)
        self.assertEqual(meta["objects_converted"]["dashboards"], 1)


class TestAutomaticModelModeEntrypoints(unittest.TestCase):
    """Primary generator APIs must not silently reinstate the old Import default."""

    def _objects(self):
        return {
            "datasources": [{
                "name": "SyntheticSQL",
                "connection": {
                    "type": "SQL Server",
                    "details": {"server": "synthetic.invalid", "database": "SyntheticDB"},
                },
                "tables": [{"name": "Facts", "columns": [
                    {"name": "ID", "datatype": "integer"},
                    {"name": "Amount", "datatype": "real"},
                ]}],
                "calculations": [], "relationships": [],
            }],
            "worksheets": [], "dashboards": [], "calculations": [],
        }

    def _assert_sql_mode(self, semantic_dir, expected):
        path = os.path.join(semantic_dir, "definition", "tables", "Facts.tmdl")
        self.assertTrue(os.path.isfile(path), path)
        with open(path, encoding="utf-8") as stream:
            content = stream.read()
        self.assertEqual(re.findall(r"^\s*mode:\s+(\w+)\s*$", content, re.M), [expected])
        self.assertIn("Sql.Database(", content)
        self.assertNotIn("Csv.Document", content)
        self.assertNotIn("Parquet.Document", content)

    def test_project_default_auto_and_explicit_import_emit_effective_sql_modes(self):
        cases = (
            ("Default", {}, "directQuery"),
            ("Auto", {"model_mode": "auto"}, "directQuery"),
            ("None", {"model_mode": None}, "directQuery"),
            ("Import", {"model_mode": "import"}, "import"),
        )
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory(prefix="project_mode_") as root:
            for name, options, expected in cases:
                with self.subTest(name=name), redirect_stdout(io.StringIO()):
                    generator = PowerBIProjectGenerator(output_dir=root)
                    generator.generate_project(name, self._objects(), **options)
                    self._assert_sql_mode(
                        os.path.join(root, name, name + ".SemanticModel"), expected,
                    )

    def test_direct_semantic_structure_default_without_project_options_is_auto(self):
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory(prefix="semantic_mode_") as root, \
                redirect_stdout(io.StringIO()):
            generator = PowerBIProjectGenerator(output_dir=root)
            self.assertFalse(hasattr(generator, "_model_mode"))
            semantic_dir = generator.create_semantic_model_structure(
                root, "Standalone", self._objects(),
            )
            self._assert_sql_mode(semantic_dir, "directQuery")


class TestAutomaticShelfRegressions(unittest.TestCase):
    """Automatic mark repair must depend on shelves, never worksheet titles."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gen = PowerBIProjectGenerator(output_dir=self.temp.name)
        self.gen._main_table = 'Facts'
        types = {'A': 'double', 'B': 'double', 'C': 'string',
                 'D': 'string', 'E': 'string', 'F': 'string'}
        self.gen._field_map = {name: ('Facts', name) for name in types}
        self.gen._actual_bim_symbols = set(self.gen._field_map.values())
        self.gen._actual_bim_column_types = {
            ('Facts', name): datatype for name, datatype in types.items()
        }
        self.gen._measure_names = {'A', 'B'}
        self.gen._bim_measure_names = set()

    def _bar(self, reverse=False):
        return {
            'name': 'Untitled 731',
            'chart_type': 'clusteredBarChart',
            'original_mark_class': 'Automatic',
            'fields': [
                {'name': 'C', 'shelf': 'columns' if reverse else 'rows'},
                {'name': 'sum:A', 'aggregation': 'sum',
                 'shelf': 'rows' if reverse else 'columns'},
                {'name': 'D', 'shelf': 'color'},
            ],
        }

    def _scatter(self):
        return {
            'name': 'Untitled 942',
            'chart_type': 'lineClusteredColumnComboChart',
            'original_mark_class': 'Automatic',
            'fields': [
                {'name': 'avg:B', 'shelf': 'rows', 'aggregation': 'avg'},
                {'name': 'sum:A', 'shelf': 'columns', 'aggregation': 'sum'},
                {'name': 'E', 'shelf': 'detail'},
                {'name': 'F', 'shelf': 'detail'},
            ],
        }

    def _column(self, name):
        return {'Column': {
            'Expression': {'SourceRef': {'Entity': 'Facts'}},
            'Property': name,
        }}

    def _assert_aggregate(self, state, role, name, function):
        projections = state[role]['projections']
        self.assertEqual(len(projections), 1)
        self.assertEqual(projections[0]['field'], {
            'Aggregation': {'Expression': self._column(name),
                            'Function': function},
        })
        self.assertEqual(projections[0]['queryRef'], f'Facts.{name}')

    def _emitted_visual(self, worksheet):
        with tempfile.TemporaryDirectory(dir=self.temp.name) as directory:
            self.gen._create_visual_worksheet(
                directory, worksheet,
                {'worksheetName': worksheet['name'], 'position': {}},
                1, 1, 0, [worksheet], {},
            )
            paths = [
                os.path.join(root, filename)
                for root, _, files in os.walk(directory)
                for filename in files if filename == 'visual.json'
            ]
            self.assertEqual(len(paths), 1)
            with open(paths[0], encoding='utf-8') as stream:
                return json.load(stream)['visual']

    def test_automatic_categorical_color_stacks_in_shelf_orientation(self):
        for reverse, expected in ((False, 'barChart'),
                                  (True, 'columnChart')):
            for title in ('Untitled 731', 'Scatter trend by month', 'Q'):
                with self.subTest(reverse=reverse, title=title):
                    worksheet = self._bar(reverse)
                    worksheet['name'] = title
                    self.assertEqual(
                        self.gen._resolve_worksheet_visual_type(worksheet), expected)
                    visual = self._emitted_visual(worksheet)
                    self.assertEqual(visual['visualType'], expected)
                    state = visual['query']['queryState']
                    self.assertEqual(set(state), {'Category', 'Series', 'Y'})
                    self.assertEqual(state['Category']['projections'][0]['field'],
                                     self._column('C'))
                    self.assertEqual(state['Series']['projections'][0]['field'],
                                     self._column('D'))
                    self._assert_aggregate(state, 'Y', 'A', 0)
                    self.assertNotIn('_override_visual_type', worksheet)

    def test_explicit_marks_are_not_repaired(self):
        for mark in ('Bar', 'Line', 'Circle', 'Shape'):
            for factory in (self._bar, self._scatter):
                with self.subTest(mark=mark, chart=factory.__name__):
                    worksheet = factory()
                    worksheet['original_mark_class'] = mark
                    self.assertEqual(
                        self.gen._resolve_worksheet_visual_type(worksheet),
                        worksheet['chart_type'])

    def test_dual_axis_metadata_prevents_automatic_repair(self):
        for metadata in ({'dual_axis': {'enabled': True}},
                         {'axes': {'dual_axis': True}}):
            for factory in (self._bar, self._scatter):
                with self.subTest(metadata=metadata, chart=factory.__name__):
                    worksheet = factory()
                    worksheet.update(metadata)
                    self.assertEqual(
                        self.gen._resolve_worksheet_visual_type(worksheet),
                        worksheet['chart_type'])

    def test_stack_mode_off_preserves_clustered_chart(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                worksheet = self._bar(reverse)
                worksheet['chart_type'] = (
                    'clusteredColumnChart' if reverse else 'clusteredBarChart')
                worksheet['formatting'] = {'pane': {'stack-mode': 'off'}}
                visual = self._emitted_visual(worksheet)
                self.assertEqual(visual['visualType'], worksheet['chart_type'])
                self.assertNotIn('_override_visual_type', worksheet)

    def test_no_categorical_color_does_not_stack(self):
        for color in (None, {'name': 'B', 'shelf': 'color',
                             'aggregation': 'sum'}):
            with self.subTest(color=color):
                worksheet = self._bar()
                worksheet['fields'] = worksheet['fields'][:2]
                if color:
                    worksheet['fields'].append(color)
                visual = self._emitted_visual(worksheet)
                self.assertEqual(visual['visualType'], 'clusteredBarChart')
                self.assertNotIn('Series', visual['query']['queryState'])

    def test_nested_opposing_dimension_does_not_stack(self):
        worksheet = self._bar()
        worksheet['fields'].append({'name': 'E', 'shelf': 'columns'})
        self.assertEqual(self.gen._resolve_worksheet_visual_type(worksheet),
                         'clusteredBarChart')

    def test_expanded_measure_values_do_not_trigger_repair(self):
        for factory in (self._bar, self._scatter):
            with self.subTest(chart=factory.__name__):
                worksheet = factory()
                worksheet['fields'].append(
                    {'name': 'B', 'shelf': 'measure_value', 'aggregation': 'sum'})
                self.assertEqual(self.gen._resolve_worksheet_visual_type(worksheet),
                                 worksheet['chart_type'])

    def test_stale_combo_becomes_scatter_with_valid_xy_and_all_detail(self):
        worksheet = self._scatter()
        self.assertEqual(self.gen._resolve_worksheet_visual_type(worksheet),
                         'scatterChart')
        visual = self._emitted_visual(worksheet)
        self.assertEqual(visual['visualType'], 'scatterChart')
        state = visual['query']['queryState']
        self.assertEqual(set(state), {'Category', 'X', 'Y'})
        self._assert_aggregate(state, 'X', 'A', 0)
        self._assert_aggregate(state, 'Y', 'B', 1)
        self.assertEqual(
            [p['field'] for p in state['Category']['projections']],
            [self._column('E'), self._column('F')])
        self.assertNotIn('_override_visual_type', worksheet)
        self.assertNotIn('_visual_fallback_note', worksheet)

    def test_scatter_excludes_unresolved_detail_without_table_override(self):
        worksheet = self._scatter()
        worksheet['fields'].insert(2, {'name': 'Missing', 'shelf': 'detail'})
        state = self.gen._build_visual_query(worksheet)['queryState']
        self.assertEqual(set(state), {'Category', 'X', 'Y'})
        self.assertEqual(
            [p['queryRef'] for p in state['Category']['projections']],
            ['Facts.E', 'Facts.F'])
        self._assert_aggregate(state, 'X', 'A', 0)
        self._assert_aggregate(state, 'Y', 'B', 1)
        self.assertNotIn('Missing', json.dumps(state))
        self.assertNotIn('_override_visual_type', worksheet)

    def test_scatter_shelf_order_not_field_list_order_controls_axes(self):
        worksheet = self._scatter()
        worksheet['fields'].reverse()
        state = self.gen._build_visual_query(worksheet)['queryState']
        self._assert_aggregate(state, 'X', 'A', 0)
        self._assert_aggregate(state, 'Y', 'B', 1)
        self.assertEqual(
            [p['queryRef'] for p in state['Category']['projections']],
            ['Facts.F', 'Facts.E'])
        self.assertNotIn('_override_visual_type', worksheet)

    def test_extracted_scatter_groups_by_customer_not_filter_slices(self):
        for name, datatype in (('Customer Name', 'string'), ('Region', 'string')):
            self.gen._field_map[name] = ('Facts', name)
            self.gen._actual_bim_symbols.add(('Facts', name))
            self.gen._actual_bim_column_types[('Facts', name)] = datatype
        worksheet = _extract_regression_worksheet("""
            <workbook><worksheet name="Q 63"><table>
                <cols>[ds].[sum:A:qk]</cols><rows>[ds].[avg:B:qk]</rows>
                <view><slices><column>[ds].[none:Region:nk]</column></slices></view>
                <panes><pane><mark class="Automatic"/><encodings>
                    <lod column="[ds].[none:Customer Name:nk]"/>
                </encodings></pane></panes>
            </table></worksheet></workbook>
        """, self.temp.name)
        worksheet['chart_type'] = 'lineClusteredColumnComboChart'
        self.assertEqual(worksheet['slices'], [
            {'name': 'Region', 'datasource': 'ds',
             'shelf': 'filter', 'source': 'slices'}])
        visual = self._emitted_visual(worksheet)
        self.assertEqual(visual['visualType'], 'scatterChart')
        state = visual['query']['queryState']
        self.assertEqual(set(state), {'Category', 'X', 'Y'})
        self.assertEqual([p['field'] for p in state['Category']['projections']],
                         [self._column('Customer Name')])
        self._assert_aggregate(state, 'X', 'A', 0)
        self._assert_aggregate(state, 'Y', 'B', 1)
        self.assertNotIn('Region', json.dumps(state))
        self.assertNotIn('_override_visual_type', worksheet)


class TestScopedAggregationRegressions(unittest.TestCase):
    """Synthetic datasource provenance and semantic-query aggregate regressions."""

    def setUp(self):
        self.gen = _make_generator()
        self.addCleanup(shutil.rmtree, self.gen.output_dir, True)
        self.gen._main_table = 'Foreign'
        self.gen._field_map = {
            'Value': ('Foreign', 'Value'), 'Period': ('Facts', 'Period'),
            'Label': ('Facts', 'Label'),
        }
        self.gen._measure_names = {'Value', 'Period'}
        self.gen._bim_measure_names = {'Value', 'Period'}
        self.gen._actual_bim_column_types = {
            ('Facts', 'Value'): 'double', ('Facts', 'Period'): 'dateTime',
            ('Facts', 'Label'): 'string',
        }
        self.gen._actual_bim_measure_types = {('Foreign', 'Value'): 'double'}
        self.gen._actual_bim_symbols = {
            *self.gen._actual_bim_column_types,
            *self.gen._actual_bim_measure_types,
        }

    def test_projection_and_scatter_use_semantic_query_aggregation_ids(self):
        for aggregation, function in (
            ('ctd', 2), ('countd', 2), ('cnt', 5), ('count', 5),
            ('median', 6), ('avg', 1),
        ):
            for method in (
                self.gen._make_projection_entry, self.gen._make_scatter_axis_entry,
            ):
                with self.subTest(aggregation=aggregation, method=method.__name__):
                    entry = method({
                        'name': 'Value', '_resolved_pair': ('Facts', 'Value'),
                        'aggregation': aggregation,
                    })
                    self.assertEqual(entry['field'], {'Aggregation': {
                        'Expression': {'Column': {
                            'Expression': {'SourceRef': {'Entity': 'Facts'}},
                            'Property': 'Value',
                        }},
                        'Function': function,
                    }})

    def test_scoped_duplicate_column_uses_renamed_table_and_property(self):
        self.gen._datasources_ref = [
            {'name': 'source-a', 'tables': [{
                'name': 'Raw', 'columns': [{'name': 'Value'}],
            }]},
            {'name': 'source-b', 'tables': [{
                'name': 'Raw', 'columns': [{'name': 'Value'}],
            }]},
        ]
        self.gen._table_rename_map = {
            ('source-a', 'Raw'): 'Facts',
            ('source-b', 'Raw'): 'Foreign',
        }
        self.gen._column_rename_map = {('Facts', 'Value'): 'Valeur (%)'}
        self.gen._actual_bim_symbols.add(('Facts', 'Valeur (%)'))
        self.gen._actual_bim_column_types[('Facts', 'Valeur (%)')] = 'double'
        self.assertEqual(
            self.gen._resolve_field_entity('Value', datasource='source-a'),
            ('Facts', 'Valeur (%)'),
        )
        self.assertEqual(
            self.gen._resolve_field_entity('Value', datasource='source-b'),
            ('Foreign', 'Value'),
        )
        entry = self.gen._make_projection_entry({
            'name': 'Value', 'datasource': 'source-a', 'aggregation': 'avg',
        })
        self.assertEqual(entry['field']['Aggregation'], {
            'Expression': {'Column': {
                'Expression': {'SourceRef': {'Entity': 'Facts'}},
                'Property': 'Valeur (%)',
            }},
            'Function': 1,
        })

    def test_connected_copy_wins_independent_of_table_order(self):
        for connection in (
            {'connection': {'type': 'sql'}},
            {'connection_details': {'type': 'sql'}},
        ):
            for reverse in (False, True):
                with self.subTest(connection=connection, reverse=reverse):
                    tables = [
                        {'name': 'ExtractCopy', 'columns': [{'name': 'Value'}]},
                        {'name': 'Facts', 'columns': [{'name': 'Value'}],
                         **connection},
                    ]
                    self.gen._actual_bim_symbols.add(('ExtractCopy', 'Value'))
                    self.gen._datasources_ref = [{
                        'name': 'source-a',
                        'tables': list(reversed(tables)) if reverse else tables,
                    }]
                    self.assertEqual(
                        self.gen._resolve_field_entity('Value', 'source-a'),
                        ('Facts', 'Value'),
                    )

    def test_ambiguous_provenance_does_not_borrow_foreign_column(self):
        for connected in (False, True):
            with self.subTest(connected=connected):
                self.gen._datasources_ref = [{
                    'name': 'source-a', 'tables': [
                        {'name': name, 'columns': [{'name': 'Value'}],
                         'connection': {'type': 'sql'} if connected else {}}
                        for name in ('Facts', 'OtherFacts')
                    ],
                }]
                self.gen._actual_bim_symbols.add(('OtherFacts', 'Value'))
                self.assertEqual(
                    self.gen._resolve_field_entity('Value', 'source-a'),
                    ('source-a', 'Value'),
                )
                entry = self.gen._make_projection_entry({
                    'name': 'Value', 'datasource': 'source-a',
                })
                self.assertNotIn('Foreign', repr(entry))

    def test_actual_measure_remains_measure_but_physical_pair_is_aggregated(self):
        self.assertIn('Measure', self.gen._make_projection_entry({
            'name': 'Value', '_resolved_pair': ('Foreign', 'Value'),
        })['field'])
        self.assertIn('Aggregation', self.gen._make_projection_entry({
            'name': 'Value', '_resolved_pair': ('Facts', 'Value'),
            'aggregation': 'avg',
        })['field'])

    def test_discrete_calculated_date_and_year_are_axis_columns_not_sum(self):
        for field in (
            {'name': 'Period', 'shelf': 'columns', 'is_discrete': True},
            {'name': 'yr:Period:ok', 'shelf': 'columns', 'is_discrete': True},
        ):
            with self.subTest(field=field):
                worksheet = {
                    'name': 'Synthetic periods', 'chart_type': 'lineChart',
                    'fields': [
                        field,
                        {'name': 'Value', '_resolved_pair': ('Facts', 'Value'),
                         'shelf': 'rows', 'aggregation': 'avg'},
                    ],
                }
                self.gen._field_map['Value'] = ('Facts', 'Value')
                state = self.gen._build_visual_query(worksheet)['queryState']
                self.assertEqual(state['Category']['projections'][0]['field'], {
                    'Column': {
                        'Expression': {'SourceRef': {'Entity': 'Facts'}},
                        'Property': 'Period',
                    },
                })
                self.assertEqual(
                    state['Y']['projections'][0]['field']['Aggregation']['Function'], 1
                )
                self.assertEqual(
                    state['Y']['projections'][0]['field']['Aggregation']
                    ['Expression']['Column']['Property'], 'Value',
                )

    def test_invalid_numeric_aggregation_rejected_on_text_date_and_boolean(self):
        for datatype in ('string', 'dateTime', 'boolean'):
            self.gen._actual_bim_column_types[('Facts', 'Label')] = datatype
            for aggregation in ('sum', 'avg', 'median'):
                for method in (
                    self.gen._make_projection_entry,
                    self.gen._make_scatter_axis_entry,
                ):
                    with self.subTest(datatype=datatype, aggregation=aggregation,
                                      method=method.__name__):
                        self.assertIsNone(method({
                            'name': 'Label', '_resolved_pair': ('Facts', 'Label'),
                            'aggregation': aggregation,
                        }))

    def test_invalid_optional_scatter_size_is_omitted_without_crash(self):
        self.gen._field_map['Value'] = ('Facts', 'Value')
        self.gen._field_map['Quantity'] = ('Facts', 'Quantity')
        self.gen._actual_bim_symbols.add(('Facts', 'Quantity'))
        self.gen._actual_bim_column_types[('Facts', 'Quantity')] = 'int64'
        for aggregation in ('sum', 'avg', 'median'):
            with self.subTest(aggregation=aggregation):
                worksheet = {
                    'name': 'Synthetic scatter', 'chart_type': 'scatterChart',
                    'fields': [
                        {'name': 'Value', 'shelf': 'columns', 'aggregation': 'avg'},
                        {'name': 'Quantity', 'shelf': 'rows', 'aggregation': 'countd'},
                        {'name': 'Label', 'shelf': 'size',
                         'aggregation': aggregation},
                    ],
                }
                state = self.gen._build_visual_query(worksheet)['queryState']
                self.assertEqual(set(state), {'X', 'Y'})
                self.assertEqual(
                    state['X']['projections'][0]['field']['Aggregation']['Function'], 1
                )
                self.assertEqual(
                    state['Y']['projections'][0]['field']['Aggregation']['Function'], 2
                )


class TestScatterAxisBindingRegressions(unittest.TestCase):
    """Scatter axis values must retain their source semantics and shelf roles."""

    def setUp(self):
        self.gen = _make_generator()
        self.gen._main_table = 'Facts'
        self.gen._field_map = {
            name: ('Facts', name)
            for name in ('XValue', 'YValue', 'TipMeasure', 'Customer', 'Region',
                         'Shared', 'Label', 'TextMeasure')
        }
        self.gen._actual_bim_symbols = set(self.gen._field_map.values())
        self.gen._actual_bim_column_types = {
            ('Facts', 'XValue'): 'double',
            ('Facts', 'YValue'): 'double',
            ('Facts', 'Customer'): 'string',
            ('Facts', 'Region'): 'string',
            ('Facts', 'Shared'): 'double',
            ('Facts', 'Label'): 'string',
        }
        self.gen._actual_bim_measure_types = {
            ('Facts', 'TipMeasure'): 'double',
            ('Facts', 'Shared'): 'double',
            ('Facts', 'Label'): 'string',
            ('Facts', 'TextMeasure'): 'string',
        }
        self.gen._measure_names = set()
        self.gen._bim_measure_names = {
            'TipMeasure', 'Shared', 'Label', 'TextMeasure'}
        self.gen._actual_bim_measure_names = {
            'TipMeasure', 'Shared', 'Label', 'TextMeasure'}

    @staticmethod
    def _column(entity, name):
        return {'Column': {
            'Expression': {'SourceRef': {'Entity': entity}},
            'Property': name,
        }}

    def _assert_aggregate(self, projection, entity, name, function):
        self.assertEqual(projection['field'], {
            'Aggregation': {
                'Expression': self._column(entity, name),
                'Function': function,
            },
        })

    def test_scatter_keeps_detail_when_color_is_present(self):
        worksheet = {
            'name': 'Detail and colour',
            'chart_type': 'scatterChart',
            'fields': [
                {'name': 'sum:XValue', 'aggregation': 'sum', 'shelf': 'columns'},
                {'name': 'avg:YValue', 'aggregation': 'avg', 'shelf': 'rows'},
                {'name': 'Customer', 'shelf': 'detail'},
                {'name': 'Region', 'shelf': 'color'},
            ],
        }

        state = self.gen._build_visual_query(worksheet)['queryState']
        self.assertEqual(set(state), {'Category', 'Series', 'X', 'Y'})
        self.assertEqual(
            [p['field'] for p in state['Category']['projections']],
            [self._column('Facts', 'Customer')],
        )
        self.assertEqual(
            state['Series']['projections'][0]['field'],
            self._column('Facts', 'Region'),
        )
        self._assert_aggregate(state['X']['projections'][0], 'Facts', 'XValue', 0)
        self._assert_aggregate(state['Y']['projections'][0], 'Facts', 'YValue', 1)

    def test_numeric_dax_measure_stays_a_measure_on_scatter_axis(self):
        entry = self.gen._make_scatter_axis_entry(
            {'name': 'TipMeasure', 'aggregation': 'avg'})

        self.assertEqual(entry['field'], {
            'Measure': {
                'Expression': {'SourceRef': {'Entity': 'Facts'}},
                'Property': 'TipMeasure',
            },
        })
        self.assertEqual(entry['queryRef'], 'Facts.TipMeasure')

    def test_physical_numeric_axis_uses_tableau_source_aggregation(self):
        entry = self.gen._make_scatter_axis_entry(
            {'name': 'avg:XValue', 'aggregation': 'avg'})

        self._assert_aggregate(entry, 'Facts', 'XValue', 1)

    def test_nonnumeric_axis_rejected_except_count_aggregations(self):
        self.assertIsNone(self.gen._make_scatter_axis_entry(
            {'name': 'TextMeasure'}))
        self.assertIsNone(self.gen._make_scatter_axis_entry(
            {'name': 'Label', 'aggregation': 'sum'}))
        self.assertIsNone(self.gen._make_scatter_axis_entry(
            {'name': 'Label', 'aggregation': 'avg'}))

        count = self.gen._make_scatter_axis_entry(
            {'name': 'Label', 'aggregation': 'count'})
        distinct_count = self.gen._make_scatter_axis_entry(
            {'name': 'Label', 'aggregation': 'countd'})
        self._assert_aggregate(count, 'Facts', 'Label', 5)
        self._assert_aggregate(distinct_count, 'Facts', 'Label', 2)

    def test_physical_column_wins_same_name_measure_collision(self):
        # The name is globally registered as a DAX measure, but this resolved
        # source pair is a physical numeric column and must be aggregated.
        entry = self.gen._make_scatter_axis_entry(
            {'name': 'Shared', '_resolved_pair': ('Facts', 'Shared'),
             'aggregation': 'max'})

        self._assert_aggregate(entry, 'Facts', 'Shared', 4)

    def test_duplicate_detail_and_aggregate_binding_is_extraction_order_independent(self):
        for duplicated_fields in (
            [
                {'name': 'avg:XValue', 'aggregation': 'avg', 'shelf': 'columns'},
                {'name': 'XValue', 'shelf': 'detail'},
            ],
            [
                {'name': 'XValue', 'shelf': 'detail'},
                {'name': 'avg:XValue', 'aggregation': 'avg', 'shelf': 'columns'},
            ],
        ):
            with self.subTest(order=[f['shelf'] for f in duplicated_fields]):
                worksheet = {
                    'name': 'Shared detail and axis',
                    'chart_type': 'scatterChart',
                    'fields': duplicated_fields + [
                        {'name': 'sum:YValue', 'aggregation': 'sum',
                         'shelf': 'rows'},
                    ],
                }
                state = self.gen._build_visual_query(worksheet)['queryState']
                self.assertEqual(
                    [p['field'] for p in state['Category']['projections']],
                    [self._column('Facts', 'XValue')],
                )
                self._assert_aggregate(
                    state['X']['projections'][0], 'Facts', 'XValue', 1)
                self._assert_aggregate(
                    state['Y']['projections'][0], 'Facts', 'YValue', 0)
                self.assertNotIn('_override_visual_type', worksheet)

    def test_tooltip_measure_does_not_become_scatter_size(self):
        worksheet = {
            'name': 'Two axes and a tooltip',
            'chart_type': 'scatterChart',
            'fields': [
                {'name': 'sum:XValue', 'aggregation': 'sum', 'shelf': 'columns'},
                {'name': 'avg:YValue', 'aggregation': 'avg', 'shelf': 'rows'},
                {'name': 'TipMeasure', 'shelf': 'tooltip'},
            ],
        }

        state = self.gen._build_visual_query(worksheet)['queryState']
        self.assertEqual(set(state), {'X', 'Y', 'Tooltips'})
        self._assert_aggregate(state['X']['projections'][0], 'Facts', 'XValue', 0)
        self._assert_aggregate(state['Y']['projections'][0], 'Facts', 'YValue', 1)
        self.assertEqual(
            state['Tooltips']['projections'][0]['field']['Measure']['Property'],
            'TipMeasure',
        )

    def test_one_axis_circle_tooltip_does_not_invent_scatter_axis_or_size(self):
        worksheet = {
            'name': 'Single axis circle',
            'chart_type': 'scatterChart',
            'original_mark_class': 'Circle',
            'fields': [
                {'name': 'sum:XValue', 'aggregation': 'sum', 'shelf': 'columns'},
                {'name': 'TipMeasure', 'shelf': 'tooltip'},
            ],
        }

        query = self.gen._build_visual_query(worksheet)
        self.assertEqual(set(query['queryState']), {'Values'})
        self.assertEqual(worksheet['_override_visual_type'], 'tableEx')
        self.assertNotIn('X', query['queryState'])
        self.assertNotIn('Y', query['queryState'])
        self.assertNotIn('Size', query['queryState'])
        self.assertEqual(
            {p['field'].get('Measure', {}).get('Property')
             or p['field'].get('Aggregation', {}).get('Expression', {})
             .get('Column', {}).get('Property')
             for p in query['queryState']['Values']['projections']},
            {'XValue', 'TipMeasure'},
        )


class TestDiscreteMonthRegressions(unittest.TestCase):
    """Month grouping requires explicit extraction metadata and model evidence."""

    CALENDAR = (
        "table 'Date Lookup'\n"
        "\tcolumn Day\n\t\tdataType: dateTime\n\t\tsourceColumn: Day\n"
        "\tcolumn 'Period Label'\n\t\tdataType: string\n"
        "\t\tsourceColumn: MonthText\n\t\tsortByColumn: 'Period Index'\n"
        "\tcolumn 'Period Index'\n\t\tdataType: int64\n\t\tsourceColumn: MonthIndex\n"
        "\tpartition 'Date Lookup' = m\n\t\tmode: import\n\t\tsource =\n"
        "\t\t\tlet\n"
        '\t\t\t\tNumbers = Table.AddColumn(Source, "MonthIndex", each Date.Month([Day]), Int64.Type),\n'
        '\t\t\t\tNames = Table.AddColumn(Numbers, "MonthText", each Date.MonthName([Day], "en-US"), type text)\n'
        "\t\t\tin Names\n"
    )
    RELATIONSHIP = (
        "relationship DateLink\n"
        "\tfromColumn: Facts.'Event Date'\n"
        "\ttoColumn: 'Date Lookup'.Day\n"
    )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gen = PowerBIProjectGenerator(output_dir=self.temp.name)
        self.gen._main_table = 'Facts'
        self.gen._field_map = {
            'Event Date': ('Facts', 'Event Date'),
            'Other Date': ('Facts', 'Other Date'),
            'A': ('Facts', 'A'),
        }
        self.gen._actual_bim_symbols = set(self.gen._field_map.values()) | {
            ('Date Lookup', 'Day'), ('Date Lookup', 'Period Label'),
            ('Date Lookup', 'Period Index'),
        }
        self.gen._actual_bim_column_types = {
            ('Facts', 'Event Date'): 'datetime',
            ('Facts', 'Other Date'): 'datetime',
            ('Facts', 'A'): 'double',
        }
        self.gen._measure_names = {'A'}
        self.gen._bim_measure_names = set()

    def _index(self, calendar=None, relationship=None):
        definition = os.path.join(self.temp.name, 'definition')
        tables = os.path.join(definition, 'tables')
        os.makedirs(tables, exist_ok=True)
        with open(os.path.join(tables, 'Date Lookup.tmdl'), 'w', encoding='utf-8') as stream:
            stream.write(self.CALENDAR if calendar is None else calendar)
        with open(os.path.join(definition, 'relationships.tmdl'), 'w', encoding='utf-8') as stream:
            stream.write(self.RELATIONSHIP if relationship is None else relationship)
        return self.gen._read_related_month_bindings(self.temp.name)

    def _worksheet(self, **metadata):
        return {
            'name': 'Q 18', 'chart_type': 'lineChart',
            'original_mark_class': 'Line',
            'fields': [
                {'name': 'mn:Event Date:nk', 'shelf': 'columns',
                 'date_part': 'month', 'is_discrete': True, **metadata},
                {'name': 'sum:A', 'shelf': 'rows', 'aggregation': 'sum'},
            ],
        }

    def _category(self, query):
        projections = query['queryState']['Category']['projections']
        self.assertEqual(len(projections), 1)
        return projections[0]['field']

    def test_validated_calendar_uses_month_name_and_category_sort(self):
        self.gen._related_month_bindings = self._index()
        self.assertEqual(self.gen._related_month_bindings, {
            ('Facts', 'Event Date'): ('Date Lookup', 'Period Label', 'Period Index'),
        })
        for metadata in ({'date_part': 'month', 'is_discrete': True},
                         {'date_part': 'mn', 'is_discrete': None,
                          'is_continuous': False}):
            with self.subTest(metadata=metadata):
                worksheet = self._worksheet(**metadata)
                query = self.gen._build_visual_query(worksheet)
                expected = {'Column': {
                    'Expression': {'SourceRef': {'Entity': 'Date Lookup'}},
                    'Property': 'Period Label',
                }}
                self.assertEqual(self._category(query), expected)
                self.assertEqual(query['sortDefinition'], {
                    'sort': [{'field': expected, 'direction': 'Ascending'}],
                    'isDefaultSort': True,
                })
                self.assertEqual(worksheet['_discrete_month_binding'],
                                 ('Date Lookup', 'Period Label', 'Period Index'))
                self.assertNotIn('_override_visual_type', worksheet)
                self.assertNotIn('_visual_mapping_note', worksheet)

    def test_continuous_or_missing_datepart_metadata_does_not_rebind(self):
        self.gen._related_month_bindings = self._index()
        for metadata in (
                {'is_discrete': False, 'is_continuous': True},
                {'is_discrete': None},
                {'date_part': None}, {'date_part': 'year'}):
            with self.subTest(metadata=metadata):
                worksheet = self._worksheet(**metadata)
                query = self.gen._build_visual_query(worksheet)
                self.assertEqual(self._category(query), {'Column': {
                    'Expression': {'SourceRef': {'Entity': 'Facts'}},
                    'Property': 'Event Date',
                }})
                self.assertNotIn('_discrete_month_binding', worksheet)
                self.assertNotIn('sortDefinition', query)

    def test_other_source_date_is_not_rebound_by_name_similarity(self):
        self.gen._related_month_bindings = self._index()
        worksheet = self._worksheet(name='mn:Other Date:nk')
        query = self.gen._build_visual_query(worksheet)
        self.assertEqual(self._category(query)['Column']['Property'], 'Other Date')
        self.assertNotIn('_discrete_month_binding', worksheet)
        self.assertIn('raw date retained', worksheet['_visual_mapping_note'])

    def test_missing_calendar_retains_raw_date_with_review_note(self):
        worksheet = self._worksheet()
        query = self.gen._build_visual_query(worksheet)
        self.assertEqual(self._category(query)['Column']['Property'], 'Event Date')
        self.assertNotIn('_discrete_month_binding', worksheet)
        self.assertNotIn('sortDefinition', query)
        self.assertNotIn('_override_visual_type', worksheet)
        self.assertIn('no unique validated active calendar relationship',
                      worksheet['_visual_mapping_note'])

    def test_emitted_month_sort_is_not_overwritten_by_stale_value_sort(self):
        self.gen._related_month_bindings = self._index()
        worksheet = self._worksheet()
        worksheet['sort_orders'] = [
            {'field': 'Event Date', 'direction': 'DESC', 'sort_by': 'sum:A'},
        ]
        visuals = os.path.join(self.temp.name, 'visuals')
        self.gen._create_visual_worksheet(
            visuals, worksheet,
            {'worksheetName': worksheet['name'], 'position': {}},
            1, 1, 0, [worksheet], {},
        )
        paths = [os.path.join(root, filename)
                 for root, _, files in os.walk(visuals)
                 for filename in files if filename == 'visual.json']
        self.assertEqual(len(paths), 1)
        with open(paths[0], encoding='utf-8') as stream:
            visual = json.load(stream)['visual']
        self.assertEqual(visual['visualType'], 'lineChart')
        category = self._category(visual['query'])
        self.assertEqual(category['Column']['Expression'],
                         {'SourceRef': {'Entity': 'Date Lookup'}})
        self.assertEqual(category['Column']['Property'], 'Period Label')
        self.assertEqual(visual['query']['sortDefinition'], {
            'sort': [{'field': category, 'direction': 'Ascending'}],
            'isDefaultSort': True,
        })

    def test_inactive_many_side_or_wrong_date_relationship_is_rejected(self):
        for relationship in (
                self.RELATIONSHIP + '\tisActive: false\n',
                self.RELATIONSHIP + '\ttoCardinality: many\n',
                self.RELATIONSHIP.replace("'Date Lookup'.Day", "'Date Lookup'.Missing"),
                self.RELATIONSHIP.replace("Facts.'Event Date'", "Facts.Missing")):
            with self.subTest(relationship=relationship):
                self.assertEqual(self._index(relationship=relationship), {})

    def test_month_names_alone_or_incorrect_sort_are_not_evidence(self):
        for calendar in (
                self.CALENDAR.replace('Date.MonthName([Day]', 'Date.MonthName([OtherDay]'),
                self.CALENDAR.replace('Date.Month([Day])', 'Date.Month([OtherDay])'),
                self.CALENDAR.replace("\t\tsortByColumn: 'Period Index'\n", ''),
                self.CALENDAR.replace('Date.MonthName', 'Text.From'),
                self.CALENDAR.replace('\t\tdataType: int64', '\t\tdataType: string')):
            with self.subTest(calendar=calendar):
                self.assertEqual(self._index(calendar=calendar), {})

    def test_ambiguous_calendar_bindings_are_rejected(self):
        second = self.CALENDAR.replace("'Date Lookup'", "'Alternate Calendar'")
        tables = os.path.join(self.temp.name, 'definition', 'tables')
        self._index()
        with open(os.path.join(tables, 'Alternate Calendar.tmdl'), 'w', encoding='utf-8') as stream:
            stream.write(second)
        self.gen._actual_bim_symbols.update({
            ('Alternate Calendar', name) for name in ('Day', 'Period Label', 'Period Index')
        })
        relationship = self.RELATIONSHIP + self.RELATIONSHIP.replace(
            'DateLink', 'SecondLink').replace("'Date Lookup'", "'Alternate Calendar'")
        self.assertEqual(self._index(relationship=relationship), {})

    def test_calendar_columns_must_exist_in_emitted_model(self):
        for missing in ('Day', 'Period Label', 'Period Index'):
            with self.subTest(missing=missing):
                pair = ('Date Lookup', missing)
                self.gen._actual_bim_symbols.remove(pair)
                try:
                    self.assertEqual(self._index(), {})
                finally:
                    self.gen._actual_bim_symbols.add(pair)

    def test_extracted_month_quantity_and_sales_text_emit_faithful_roles(self):
        self.gen._field_map = {
            name: ('Orders', name)
            for name in ('Order Date', 'Order Quantity', 'Sales')
        }
        self.gen._actual_bim_symbols = set(self.gen._field_map.values()) | {
            ('Calendar', name) for name in ('Date', 'MonthName', 'Month')
        }
        self.gen._actual_bim_column_types = {
            ('Orders', 'Order Date'): 'datetime',
            ('Orders', 'Order Quantity'): 'int64', ('Orders', 'Sales'): 'double',
        }
        self.gen._measure_names = {'Order Quantity', 'Sales'}
        calendar = self.CALENDAR.replace("'Date Lookup'", 'Calendar').replace(
            'Day', 'Date').replace("'Period Label'", 'MonthName').replace(
            "'Period Index'", 'Month')
        relationship = (
            "relationship DateLink\n"
            "\tfromColumn: Orders.'Order Date'\n\ttoColumn: Calendar.Date\n")
        self.gen._related_month_bindings = self._index(calendar, relationship)
        self.assertEqual(self.gen._related_month_bindings, {
            ('Orders', 'Order Date'): ('Calendar', 'MonthName', 'Month')})
        worksheet = _extract_regression_worksheet("""
            <workbook><worksheet name="Q 81"><table>
                <cols>[ds].[mn:Order Date:ok]</cols>
                <rows>[ds].[sum:Order Quantity:qk]</rows>
                <view><datasource-dependencies datasource="ds">
                    <column-instance name="[mn:Order Date:ok]" column="[Order Date]"
                                     derivation="Month" type="ordinal"/>
                </datasource-dependencies></view>
                <panes><pane><mark class="Line"/><encodings>
                    <text column="[ds].[avg:Sales:qk]"/>
                </encodings></pane></panes>
            </table></worksheet></workbook>
        """, self.temp.name)
        self.assertEqual(worksheet['fields'][0], {
            'name': 'Order Date', 'shelf': 'columns', 'datasource': 'ds',
            'derivation': 'Month', 'date_part': 'mn',
            'is_discrete': True, 'is_continuous': False})
        visuals = os.path.join(self.temp.name, 'quantity-visual')
        self.gen._create_visual_worksheet(
            visuals, worksheet, {'worksheetName': 'Q 81', 'position': {}},
            1, 1, 0, [worksheet], {})
        paths = [os.path.join(root, filename)
                 for root, _, files in os.walk(visuals)
                 for filename in files if filename == 'visual.json']
        self.assertEqual(len(paths), 1)
        with open(paths[0], encoding='utf-8') as stream:
            payload = json.load(stream)
        visual = payload['visual']
        self.assertEqual(visual['visualType'], 'lineChart')
        query = visual['query']
        category = {'Column': {
            'Expression': {'SourceRef': {'Entity': 'Calendar'}},
            'Property': 'MonthName'}}
        self.assertEqual(self._category(query), category)
        self.assertEqual(query['sortDefinition'], {
            'sort': [{'field': category, 'direction': 'Ascending'}],
            'isDefaultSort': True})
        state = query['queryState']
        self.assertEqual(set(state), {'Category', 'Y', 'Tooltips'})
        for role, name, function in (
                ('Y', 'Order Quantity', 0), ('Tooltips', 'Sales', 1)):
            self.assertEqual(len(state[role]['projections']), 1)
            self.assertEqual(state[role]['projections'][0]['field'], {
                'Aggregation': {'Expression': {'Column': {
                    'Expression': {'SourceRef': {'Entity': 'Orders'}},
                    'Property': name}}, 'Function': function}})
        self.assertEqual(
            visual['objects']['categoryAxis'][0]['properties']['axisType'],
            {'expr': {'Literal': {'Value': "'Categorical'"}}})
        self.assertEqual(visual['objects']['labels'][0]['properties']['show'],
                         {'expr': {'Literal': {'Value': 'false'}}})
        self.assertTrue(any(
            annotation.get('name') == 'MigrationNote'
            and 'Separate Tableau Text measure labels' in annotation.get('value', '')
            and 'Tooltips' in annotation['value']
            for annotation in payload.get('annotations', [])))


class TestPublicSuperstorePipeline(unittest.TestCase):
    """Original TWBX -> public extraction -> JSON importer -> emitted PBIR."""

    @classmethod
    def setUpClass(cls):
        from contextlib import redirect_stdout
        from pathlib import Path
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))
        from extract_tableau_data import TableauExtractor
        from import_to_powerbi import PowerBIImporter
        fixture = Path(__file__).resolve().parents[1] / (
            'examples/real_world/superstore_sales_dashboard.twbx')
        if not fixture.is_file():
            raise unittest.SkipTest('Original public Superstore TWBX not available')
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        extract_dir = os.path.join(cls.temp.name, 'extracted')
        output_dir = os.path.join(cls.temp.name, 'output')
        extractor = TableauExtractor(str(fixture), output_dir=extract_dir)
        with redirect_stdout(io.StringIO()):
            if extractor.extract_all() is not True:
                raise AssertionError('Original Superstore extraction failed')
            importer = PowerBIImporter(source_dir=extract_dir)
            importer.import_all(
                report_name='PublicSuperstore', output_dir=output_dir)
        cls.worksheets = {
            worksheet['name']: worksheet
            for worksheet in extractor.workbook_data['worksheets']
        }
        cls.payloads = []
        for path in Path(output_dir).rglob('visual.json'):
            with path.open(encoding='utf-8') as stream:
                cls.payloads.append(json.load(stream))
        cls.model_dir = Path(output_dir) / 'PublicSuperstore' / (
            'PublicSuperstore.SemanticModel/definition')

    def _payload(self, title):
        matches = []
        for payload in self.payloads:
            objects = payload.get('visual', {}).get('visualContainerObjects', {})
            for entry in objects.get('title', []):
                value = entry.get('properties', {}).get('text', {}).get(
                    'expr', {}).get('Literal', {}).get('Value')
                if value == f"'{title}'":
                    matches.append(payload)
        self.assertEqual(len(matches), 1, f'Expected one emitted visual titled {title}')
        return matches[0]

    def _column(self, table, property_name):
        return {'Column': {
            'Expression': {'SourceRef': {'Entity': table}},
            'Property': property_name}}

    def _assert_role(self, state, role, field):
        self.assertEqual(len(state[role]['projections']), 1)
        self.assertEqual(state[role]['projections'][0]['field'], field)

    def _sum(self, property_name):
        return {'Aggregation': {
            'Expression': self._column('Orders', property_name), 'Function': 0}}

    def test_original_bar_is_horizontal_stacked_by_customer_segment(self):
        worksheet = self.worksheets['Bar Chart']
        self.assertEqual(worksheet['original_mark_class'], 'Automatic')
        self.assertEqual(
            [(f['name'], f['shelf']) for f in worksheet['fields']
             if f['shelf'] in ('rows', 'columns', 'color')],
            [('Sales', 'columns'), ('Region', 'rows'),
             ('Customer Segment', 'color')])
        visual = self._payload('Profit By Region & Customer Segment')['visual']
        self.assertEqual(visual['visualType'], 'barChart')
        state = visual['query']['queryState']
        self._assert_role(state, 'Category', self._column('Orders', 'Region'))
        self._assert_role(state, 'Series', self._column('Orders', 'Customer Segment'))
        self._assert_role(state, 'Y', self._sum('Sales'))
        self.assertNotIn('Y2', state)

    def test_original_pie_keeps_region_and_sales_not_totals_table(self):
        worksheet = self.worksheets['Pie chart']
        self.assertEqual(worksheet['original_mark_class'], 'Pie')
        visual = self._payload('%Sales Contribution & Sales by each Region')['visual']
        self.assertEqual(visual['visualType'], 'pieChart')
        state = visual['query']['queryState']
        self._assert_role(state, 'Category', self._column('Orders', 'Region'))
        self._assert_role(state, 'Y', self._sum('Sales'))
        self.assertNotIn('Values', state)

    def test_original_line_uses_related_calendar_month_quantity_and_sales_text(self):
        worksheet = self.worksheets['Line chart']
        date_fields = [f for f in worksheet['fields']
                       if f['name'] == 'Order Date' and f['shelf'] == 'columns']
        self.assertEqual(len(date_fields), 1)
        self.assertEqual(date_fields[0]['date_part'], 'mn')
        self.assertEqual(date_fields[0]['derivation'], 'Month')
        self.assertIs(date_fields[0]['is_discrete'], True)
        self.assertIs(date_fields[0]['is_continuous'], False)
        payload = self._payload('Order Quality by Month')
        visual = payload['visual']
        self.assertEqual(visual['visualType'], 'lineChart')
        query = visual['query']
        state = query['queryState']
        month = self._column('Calendar', 'MonthName')
        self._assert_role(state, 'Category', month)
        self._assert_role(state, 'Y', self._sum('Order Quantity'))
        self.assertTrue(any(
            projection['field'] == self._sum('Sales')
            for projection in state['Tooltips']['projections']))
        self.assertEqual(query['sortDefinition'], {
            'sort': [{'field': month, 'direction': 'Ascending'}],
            'isDefaultSort': True})
        self.assertEqual(
            visual['objects']['categoryAxis'][0]['properties']['axisType'],
            {'expr': {'Literal': {'Value': "'Categorical'"}}})
        self.assertEqual(visual['objects']['labels'][0]['properties']['show'],
                         {'expr': {'Literal': {'Value': 'false'}}})
        self.assertTrue(any(
            annotation.get('name') == 'MigrationNote'
            and 'Separate Tableau Text measure labels' in annotation.get('value', '')
            for annotation in payload.get('annotations', [])))
        relationships = (self.model_dir / 'relationships.tmdl').read_text(encoding='utf-8')
        self.assertRegex(relationships, r"fromColumn:\s*Orders\.'Order Date'")
        self.assertRegex(relationships, r'toColumn:\s*Calendar\.Date')
        calendar = (self.model_dir / 'tables' / 'Calendar.tmdl').read_text(encoding='utf-8')
        self.assertRegex(calendar, r'column MonthName[\s\S]*?sortByColumn:\s*Month')

    def test_original_month_axis_has_active_exact_source_date_relationship(self):
        relationships = (self.model_dir / 'relationships.tmdl').read_text(encoding='utf-8')
        blocks = re.split(r'^relationship\s+.+$', relationships, flags=re.MULTILINE)[1:]
        matching = [
            block for block in blocks
            if re.search(r"fromColumn:\s*Orders\.'Order Date'\s*$", block, re.MULTILINE)
            and re.search(r'toColumn:\s*Calendar\.Date\s*$', block, re.MULTILINE)
            and not re.search(r'isActive:\s*false', block)
        ]
        self.assertEqual(len(matching), 1,
                         'Discrete Order Date month requires an active exact '
                         'Orders.Order Date -> Calendar.Date relationship; '
                         'Ship Date is not an equivalent source')

    def test_month_relationship_propagates_calendar_filter_to_orders(self):
        relationships = (self.model_dir / 'relationships.tmdl').read_text(encoding='utf-8')
        blocks = re.split(r'^relationship\s+.+$', relationships, flags=re.MULTILINE)[1:]
        matching = [
            block for block in blocks
            if re.search(r"fromColumn:\s*Orders\.'Order Date'\s*$", block, re.MULTILINE)
            and re.search(r'toColumn:\s*Calendar\.Date\s*$', block, re.MULTILINE)]
        self.assertEqual(len(matching), 1)
        props = dict(re.findall(r'^\s+(\w+):\s*(.+)$', matching[0], re.MULTILINE))
        self.assertEqual(props.get('isActive', 'true').lower(), 'true')
        self.assertEqual(props.get('fromCardinality', 'many').lower(), 'many')
        self.assertEqual(props.get('toCardinality', 'one').lower(), 'one')
        # TMDL oneDirection on a many->one relationship propagates the
        # Calendar (one) filter to Orders (many); bothDirections also permits it.
        self.assertIn(props.get('crossFilteringBehavior', 'oneDirection'),
                      ('oneDirection', 'bothDirections'))
        visual = self._payload('Order Quality by Month')['visual']
        self._assert_role(visual['query']['queryState'], 'Y',
                          self._sum('Order Quantity'))

    def test_original_scatter_groups_by_customer_only_not_filter_slices(self):
        worksheet = self.worksheets['Scatter Plot']
        self.assertEqual(
            [f['name'] for f in worksheet['fields'] if f['shelf'] == 'detail'],
            ['Customer Name'])
        self.assertTrue(worksheet['slices'])
        self.assertTrue(all(
            f['shelf'] == 'filter' and f['source'] == 'slices'
            for f in worksheet['slices']))
        visual = self._payload('Profit V/s Sales')['visual']
        self.assertEqual(visual['visualType'], 'scatterChart')
        state = visual['query']['queryState']
        self.assertEqual(set(state), {'Category', 'X', 'Y'})
        self._assert_role(state, 'Category', self._column('Orders', 'Customer Name'))
        self._assert_role(state, 'X', self._sum('Sales'))
        self._assert_role(state, 'Y', self._sum('Profit'))
        self.assertNotIn('Action (Region)', json.dumps(state))
        self.assertNotIn('Calculation_310466947315806208', json.dumps(state))


class TestPublicSuperstoreAfterAutoheal(TestPublicSuperstorePipeline):
    """The CLI autoheal stage must preserve the same emitted visual contracts."""

    @classmethod
    def setUpClass(cls):
        from contextlib import redirect_stdout
        from pathlib import Path
        from powerbi_import.healing import AutoHealer
        super().setUpClass()
        project_dir = cls.model_dir.parent.parent
        with redirect_stdout(io.StringIO()):
            cls.heal_report = AutoHealer().heal_project(str(project_dir))
        # Reload disk output after healing: assertions must not use pre-heal JSON.
        cls.payloads = []
        for path in Path(project_dir).rglob('visual.json'):
            with path.open(encoding='utf-8') as stream:
                cls.payloads.append(json.load(stream))


if __name__ == '__main__':
    unittest.main(verbosity=2)
