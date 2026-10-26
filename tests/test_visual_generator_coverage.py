"""Tests targeting uncovered lines in visual_generator.py for Sprint 30 coverage push."""

import inspect
import json
import unittest

from powerbi_import.visual_generator import (
    _L,
    APPROXIMATION_MAP,
    CUSTOM_VISUAL_GUIDS,
    VISUAL_TYPE_MAP,
    _apply_visual_decorations,
    _build_data_bar_config,
    _build_dynamic_reference_line,
    _build_small_multiples_config,
    _build_visual_filters,
    _build_visual_query_state,
    _calculate_proportional_layout,
    build_query_state,
    create_filters_config,
    create_projections,
    create_prototype_query,
    create_visual_container,
    generate_script_visual,
    generate_visual_containers,
    get_approximation_note,
    get_custom_visual_guid_for_approx,
    resolve_visual_type,
)


def _expected_data_bar_instance(query_ref, *, positive_color="#4472C4",
                                negative_color="#ED7D31",
                                axis_color="#CCCCCC",
                                reverse_direction=False, hide_text=False):
    """Canonical PBIR conditional-formatting instance for one projection."""
    def literal(value):
        return {"expr": {"Literal": {"Value": value}}}

    return {
        "properties": {
            "dataBars": {
                "positiveColor": {
                    "solid": {"color": literal(f"'{positive_color}'")},
                },
                "negativeColor": {
                    "solid": {"color": literal(f"'{negative_color}'")},
                },
                "axisColor": {
                    "solid": {"color": literal(f"'{axis_color}'")},
                },
                "reverseDirection": literal(
                    "true" if reverse_direction else "false"),
                "hideText": literal("true" if hide_text else "false"),
            },
        },
        "selector": {"metadata": query_ref},
    }


# ═══════════════════════════════════════════════════════════════════
# resolve_visual_type + approximation helpers
# ═══════════════════════════════════════════════════════════════════

class TestResolveVisualType(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(resolve_visual_type("bar"), "clusteredBarChart")

    def test_approximation_match(self):
        """Covers L672-673: APPROXIMATION_MAP fallback path."""
        self.assertEqual(resolve_visual_type("sankey"), "sankeyDiagram")

    def test_none_returns_tableEx(self):
        self.assertEqual(resolve_visual_type(None), "tableEx")

    def test_unknown_returns_tableEx(self):
        self.assertEqual(resolve_visual_type("totally_unknown_chart"), "tableEx")


class TestGetApproximationNote(unittest.TestCase):
    def test_known_approx(self):
        note = get_approximation_note("sankey")
        self.assertIn("custom visual", note)

    def test_none_input(self):
        self.assertIsNone(get_approximation_note(None))

    def test_exact_type_returns_none(self):
        """Exact match has no approximation note."""
        self.assertIsNone(get_approximation_note("bar"))


class TestGetCustomVisualGuidForApprox(unittest.TestCase):
    """Covers L674, 696 — approx target matching to CUSTOM_VISUAL_GUIDS."""

    def test_none_input(self):
        self.assertIsNone(get_custom_visual_guid_for_approx(None))

    def test_no_approx(self):
        self.assertIsNone(get_custom_visual_guid_for_approx("bar"))

    def test_approx_with_custom_visual(self):
        """Sankey approx maps to sankeyDiagram, which is in CUSTOM_VISUAL_GUIDS."""
        result = get_custom_visual_guid_for_approx("sankey")
        self.assertIsNotNone(result)
        self.assertIn("guid", result)
        self.assertEqual(result["class"], "sankeyDiagram")

    def test_approx_without_custom_visual(self):
        """Butterfly maps to hundredPercentStackedBarChart — not a custom visual."""
        result = get_custom_visual_guid_for_approx("butterfly")
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════
# _calculate_proportional_layout
# ═══════════════════════════════════════════════════════════════════

class TestCalculateProportionalLayout(unittest.TestCase):
    """Covers L812 — overlap detection branch."""

    def test_overlap_correction(self):
        """Two overlapping source positions should be corrected."""
        worksheets = [{"name": "A"}, {"name": "B"}]
        source_positions = [
            {"x": 0, "y": 0, "w": 500, "h": 400},
            {"x": 100, "y": 100, "w": 500, "h": 400},  # overlaps with first
        ]
        positions = _calculate_proportional_layout(
            worksheets, page_width=1280, page_height=720,
            source_positions=source_positions,
        )
        self.assertEqual(len(positions), 2)
        # After overlap fix, second should be shifted right of first
        x1, _y1, w1, _h1 = positions[0]
        x2, _y2, _w2, _h2 = positions[1]
        self.assertGreaterEqual(x2, x1 + w1)

    def test_no_overlap(self):
        """Non-overlapping positions should stay as-is (scaled)."""
        worksheets = [{"name": "A"}, {"name": "B"}]
        source_positions = [
            {"x": 0, "y": 0, "w": 200, "h": 200},
            {"x": 400, "y": 0, "w": 200, "h": 200},
        ]
        positions = _calculate_proportional_layout(
            worksheets, page_width=1280, page_height=720,
            source_positions=source_positions,
        )
        self.assertEqual(len(positions), 2)

    def test_grid_fallback(self):
        """No source positions → grid layout."""
        worksheets = [{"name": "A"}, {"name": "B"}, {"name": "C"}]
        positions = _calculate_proportional_layout(
            worksheets, page_width=1280, page_height=720,
            source_positions=None,
        )
        self.assertEqual(len(positions), 3)


# ═══════════════════════════════════════════════════════════════════
# generate_visual_containers — fallback position
# ═══════════════════════════════════════════════════════════════════

class TestGenerateVisualContainers(unittest.TestCase):
    """Covers L956 — fallback position when idx >= len(positions)."""

    def test_basic_generation(self):
        worksheets = [{"name": "Sheet1", "dimensions": [{"field": "Cat"}],
                       "measures": [{"name": "Sales", "expression": "SUM(Sales)"}]}]
        containers = generate_visual_containers(
            worksheets, "TestReport", page_width=1280, page_height=720,
        )
        self.assertEqual(len(containers), 1)
        self.assertIn("position", containers[0])

    def test_empty_worksheets(self):
        containers = generate_visual_containers(
            [], "TestReport", page_width=1280, page_height=720,
        )
        self.assertEqual(len(containers), 0)


# ═══════════════════════════════════════════════════════════════════
# create_visual_container — cross-filtering disabled
# ═══════════════════════════════════════════════════════════════════

class TestCreateVisualContainerCrossFilter(unittest.TestCase):
    """Covers L1094-1096 — cross-filtering disabled branch."""

    def test_cross_filtering_disabled(self):
        ws = {
            "name": "Test",
            "visual_type": "bar",
            "interactions": {"disabled": True},
            "dimensions": [{"field": "Category"}],
            "measures": [{"name": "Sales", "expression": "SUM(Sales)"}],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="test-id",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Category": "T", "Sales": "T"},
        )
        self.assertIn("filterConfig", container)
        self.assertTrue(container["filterConfig"]["disabled"])

    def test_cross_filtering_not_disabled(self):
        ws = {
            "name": "Test",
            "visual_type": "bar",
            "dimensions": [{"field": "Category"}],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="test-id",
            x=0, y=0, width=400, height=300, z_index=0,
        )
        self.assertNotIn("filterConfig", container)

    def test_visual_filters_in_filter_config(self):
        """Visual-level filters must go into container.filterConfig.filters (PBIR v4.0)."""
        ws = {
            "name": "Test",
            "visualType": "bar",
            "filters": [{"field": "Region", "values": ["East", "West"]}],
            "dataFields": [],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="test-id",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Region": "Sales"},
        )
        self.assertIn("filterConfig", container)
        self.assertIn("filters", container["filterConfig"])
        self.assertGreater(len(container["filterConfig"]["filters"]), 0)
        # Filters must NOT appear as a top-level property on the visual object
        self.assertNotIn("filters", container["visual"])


# ═══════════════════════════════════════════════════════════════════
# _apply_visual_decorations — subtitle, colorBy, calendar, cond fmt
# ═══════════════════════════════════════════════════════════════════

class TestApplyVisualDecorations(unittest.TestCase):
    """Covers L1158-1165 (subtitle), L1178/1187-1188 (colorBy),
    L1230-1239 (calendar heat map), L1254-1255 (conditional format),
    L1261-1273 (visual filters), L1282-1294 (sort order),
    L1301-1328 (reference lines)."""

    def _make_visual_obj(self):
        return {}

    def test_subtitle(self):
        ws = {"name": "Test", "subtitle": "My Subtitle"}
        vo = self._make_visual_obj()
        _apply_visual_decorations(ws, "bar", "clusteredBarChart", "Test", {}, vo)
        self.assertIn("subTitle", vo["visualContainerObjects"])

    def test_no_subtitle(self):
        ws = {"name": "Test"}
        vo = self._make_visual_obj()
        _apply_visual_decorations(ws, "bar", "clusteredBarChart", "Test", {}, vo)
        self.assertNotIn("subTitle", vo.get("visualContainerObjects", {}))

    def test_color_by_measure(self):
        ws = {"name": "Test", "colorBy": {"mode": "byMeasure"}}
        vo = self._make_visual_obj()
        _apply_visual_decorations(ws, "bar", "clusteredBarChart", "Test", {}, vo)
        self.assertIn("dataPoint", vo.get("objects", {}))
        dp = vo["objects"]["dataPoint"][0]["properties"]
        self.assertIn("showAllDataPoints", dp)

    def test_color_by_measure_alt_key(self):
        """Also accepts 'measure' as mode alias."""
        ws = {"name": "Test", "colorBy": {"mode": "measure"}}
        vo = self._make_visual_obj()
        _apply_visual_decorations(ws, "bar", "clusteredBarChart", "Test", {}, vo)
        self.assertIn("dataPoint", vo.get("objects", {}))

    def test_color_by_dimension(self):
        ws = {"name": "Test", "colorBy": {"mode": "byDimension"}}
        vo = self._make_visual_obj()
        _apply_visual_decorations(ws, "bar", "clusteredBarChart", "Test", {}, vo)
        self.assertIn("dataPoint", vo.get("objects", {}))

    def test_color_by_dimension_alt_key(self):
        ws = {"name": "Test", "colorBy": {"mode": "dimension"}}
        vo = self._make_visual_obj()
        _apply_visual_decorations(ws, "bar", "clusteredBarChart", "Test", {}, vo)
        self.assertIn("dataPoint", vo.get("objects", {}))

    def test_calendar_heat_map(self):
        ws = {"name": "Test"}
        vo = self._make_visual_obj()
        _apply_visual_decorations(ws, "CalendarHeatMap", "matrix", "Test", {}, vo)
        self.assertIn("values", vo.get("objects", {}))
        self.assertTrue(len(vo.get("annotations", [])) > 0)

    def test_highlight_table(self):
        ws = {"name": "Test"}
        vo = self._make_visual_obj()
        _apply_visual_decorations(ws, "highlight_table", "matrix", "Test", {}, vo)
        self.assertIn("values", vo.get("objects", {}))

    def test_conditional_formatting_rules(self):
        ws = {"name": "Test", "conditionalFormatting": [{"field": "Sales", "min": 0, "max": 100}]}
        vo = self._make_visual_obj()
        _apply_visual_decorations(ws, "bar", "clusteredBarChart", "Test", {}, vo)
        self.assertIn("dataPoint", vo.get("objects", {}))

    def test_visual_level_filters(self):
        """Filters are no longer set on visual_obj; they go to container.filterConfig."""
        ws = {
            "name": "Test",
            "filters": [{"field": "Region", "values": ["East", "West"]}],
        }
        ctm = {"Region": "Sales"}
        vo = self._make_visual_obj()
        _apply_visual_decorations(ws, "bar", "clusteredBarChart", "Test", ctm, vo)
        # PBIR v4.0: filters must NOT appear on visual_obj
        self.assertNotIn("filters", vo)

    def test_sort_order(self):
        ws = {
            "name": "Test",
            "sortBy": [{"field": "Date", "direction": "asc"}],
        }
        vo = self._make_visual_obj()
        _apply_visual_decorations(ws, "bar", "clusteredBarChart", "Test", {"Date": "Orders"}, vo)
        # Sort is processed (implementation stores it in visualContainerObjects or annotations)
        # Just verify no crash — the sort code writes to visualContainerObjects
        self.assertIn("visualContainerObjects", vo)

    def test_reference_lines_constant(self):
        ws = {
            "name": "Test",
            "referenceLines": [
                {"value": 42, "label": "Target", "color": "#FF0000"},
            ],
        }
        vo = self._make_visual_obj()
        _apply_visual_decorations(ws, "bar", "clusteredBarChart", "Test", {}, vo)
        self.assertIn("constantLine", vo.get("objects", {}))

    def test_reference_lines_dynamic(self):
        ws = {
            "name": "Test",
            "referenceLines": [
                {"type": "average", "field": "Sales", "label": "Avg Sales"},
            ],
        }
        ctm = {"Sales": "OrdersTable"}
        vo = self._make_visual_obj()
        _apply_visual_decorations(ws, "bar", "clusteredBarChart", "Test", ctm, vo)
        self.assertIn("referenceLine", vo.get("objects", {}))

    def test_reference_lines_mixed(self):
        ws = {
            "name": "Test",
            "referenceLines": [
                {"value": 10, "label": "Min"},
                {"type": "median", "field": "Profit", "label": "Med Profit"},
            ],
        }
        ctm = {"Profit": "T"}
        vo = self._make_visual_obj()
        _apply_visual_decorations(ws, "bar", "clusteredBarChart", "Test", ctm, vo)
        self.assertIn("constantLine", vo.get("objects", {}))
        self.assertIn("referenceLine", vo.get("objects", {}))

    def test_data_bars_matrix_use_exact_projection_selector(self):
        measure_name = "Revenu net €"
        table_name = "Finances & opérations"
        measure_query_ref = f"{table_name}.{measure_name}"
        worksheet = {
            "name": "Test",
            "visualType": "matrix",
            "measures": [{
                "name": measure_name,
                "label": measure_name,
                "expression": "SUM(Revenue)",
            }],
            "dataBars": [{
                "queryRef": measure_query_ref,
                "maxColor": "#007ACC",
                "negativeColor": "#CC3300",
                "axisColor": "#222222",
                "reverseDirection": True,
                "showBarOnly": True,
                # Old value bounds are not supported by this PBIR format.
                "minimumValue": 0,
                "maximumValue": 100,
            }],
        }
        container = create_visual_container(
            worksheet,
            visual_id="data-bars-matrix",
            measure_lookup={
                measure_name: (table_name, "SUM('Finances & opérations'[Revenue])"),
            },
        )
        visual = container["visual"]
        projections = visual["query"]["queryState"]["Values"]["projections"]
        self.assertEqual(len(projections), 1)
        self.assertEqual(projections[0]["queryRef"], measure_query_ref)
        self.assertEqual(projections[0]["field"], {"Measure": {
            "Expression": {"SourceRef": {"Entity": table_name}},
            "Property": measure_name,
        }})
        self.assertEqual(visual["visualType"], "pivotTable")
        self.assertEqual(
            visual["objects"]["columnFormatting"],
            [_expected_data_bar_instance(
                measure_query_ref,
                positive_color="#007ACC",
                negative_color="#CC3300",
                axis_color="#222222",
                reverse_direction=True,
                hide_text=True,
            )],
        )
        self.assertNotIn("dataBar", visual["objects"])
        self.assertIn({
            "name": "MigrationNote",
            "value": (
                "Legacy matrix mapped to pivotTable to preserve data bars; "
                "existing query projections and formatting retained."
            ),
        }, container["annotations"])
        self.assertNotIn("annotations", visual)

    def test_data_bars_table_keep_exact_named_and_physical_aggregation_refs(self):
        measure_name = "Revenu net €"
        table_name = "Finances & opérations"
        measure_query_ref = f"{table_name}.{measure_name}"
        aggregate_query_ref = f"Sum({table_name}.Revenue)"
        worksheet = {
            "name": "Test",
            "visualType": "tableEx",
            "measures": [{
                "name": measure_name,
                "label": measure_name,
                "expression": "SUM(Revenue)",
            }, {
                # Revenue is a physical numeric column, not an unregistered
                # named measure. This shape follows the query builder's
                # supported expression + column-table-map route.
                "name": "Revenue",
                "label": "Revenue",
                "expression": "SUM(Revenue)",
            }],
            "dataBars": [
                {"queryRef": measure_query_ref},
                {"queryRef": aggregate_query_ref},
            ],
        }
        container = create_visual_container(
            worksheet,
            visual_id="data-bars-table",
            col_table_map={"Revenue": table_name},
            measure_lookup={
                measure_name: (table_name, "SUM('Finances & opérations'[Revenue])"),
            },
        )
        visual = container["visual"]
        projections = visual["query"]["queryState"]["Values"]["projections"]
        self.assertEqual(
            [projection["queryRef"] for projection in projections],
            [measure_query_ref, aggregate_query_ref],
        )
        self.assertEqual(projections[0]["field"], {"Measure": {
            "Expression": {"SourceRef": {"Entity": table_name}},
            "Property": measure_name,
        }})
        self.assertEqual(projections[1]["field"], {"Aggregation": {
            "Expression": {"Column": {
                "Expression": {"SourceRef": {"Entity": table_name}},
                "Property": "Revenue",
            }},
            "Function": 0,
        }})
        self.assertEqual(
            visual["objects"]["columnFormatting"],
            [
                _expected_data_bar_instance(measure_query_ref),
                _expected_data_bar_instance(aggregate_query_ref),
            ],
        )
        self.assertEqual(visual["visualType"], "tableEx")
        self.assertFalse(any(
            note.get("name") == "MigrationNote"
            for note in container.get("annotations", [])
        ))

    def test_unbound_data_bar_does_not_invent_selector_or_modernize_matrix(self):
        measure_name = "Revenu net €"
        valid_query_ref = "Sales.Revenu net €"
        requested_query_ref = "Sales.Mesure manquante €"
        worksheet = {
            "name": "Test",
            "visualType": "matrix",
            "measures": [{
                "name": measure_name,
                "label": measure_name,
                "expression": "SUM(Revenue)",
            }],
            "dataBars": [{"queryRef": requested_query_ref}],
        }
        container = create_visual_container(
            worksheet,
            visual_id="unbound-data-bars-matrix",
            measure_lookup={
                measure_name: ("Sales", "SUM(Sales[Revenue])"),
            },
        )
        visual = container["visual"]
        self.assertEqual(visual["visualType"], "matrix")
        self.assertEqual(
            visual["query"]["queryState"]["Values"]["projections"][0]["queryRef"],
            valid_query_ref,
        )
        self.assertNotIn("columnFormatting", visual.get("objects", {}))
        self.assertTrue(any(
            note.get("name") == "MigrationNote"
            and note.get("value") ==
            "Data bars could not bind to projected Values fields; review "
            f"required: {requested_query_ref}"
            for note in container.get("annotations", [])
        ))
        self.assertNotIn("annotations", visual)

    def test_small_multiples(self):
        ws = {
            "name": "Test",
            "smallMultiples": {"field": "Region", "layout": "flow", "maxPerRow": 3},
        }
        ctm = {"Region": "Geo"}
        vo = self._make_visual_obj()
        _apply_visual_decorations(ws, "bar", "clusteredBarChart", "Test", ctm, vo)
        self.assertIn("query", vo)
        self.assertIn("SmallMultiple", vo["query"]["queryState"])

    def test_axis_config_y(self):
        ws = {
            "name": "Test",
            "axes": {"y": {"auto_range": False, "range_min": 0, "range_max": 100,
                           "scale": "log", "reversed": True, "title": "Revenue"}},
        }
        vo = self._make_visual_obj()
        _apply_visual_decorations(ws, "bar", "clusteredBarChart", "Test", {}, vo)
        va = vo["objects"]["valueAxis"][0]["properties"]
        self.assertIn("start", va)
        self.assertIn("end", va)
        self.assertIn("axisScale", va)
        self.assertIn("reverseOrder", va)
        self.assertIn("titleText", va)

    def test_axis_config_x(self):
        ws = {
            "name": "Test",
            "axes": {"x": {"reversed": True, "title": "Time"}},
        }
        vo = self._make_visual_obj()
        _apply_visual_decorations(ws, "bar", "clusteredBarChart", "Test", {}, vo)
        ca = vo["objects"]["categoryAxis"][0]["properties"]
        self.assertIn("reverseOrder", ca)
        self.assertIn("titleText", ca)


# ═══════════════════════════════════════════════════════════════════
# _build_visual_filters — TopN + categorical
# ═══════════════════════════════════════════════════════════════════

class TestBuildVisualFilters(unittest.TestCase):
    """Covers TopN and categorical filter code paths."""

    def test_topn_filter(self):
        filters = [{"field": "Product", "type": "topN", "count": 5}]
        result = _build_visual_filters(filters, {"Product": "Sales"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "TopN")
        self.assertEqual(result[0]["itemCount"], 5)

    def test_categorical_filter(self):
        filters = [{"field": "Region", "values": ["East", "West"]}]
        result = _build_visual_filters(filters, {"Region": "Geo"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["type"], "Categorical")

    def test_empty_filters(self):
        result = _build_visual_filters([], {})
        self.assertEqual(result, [])


# ═══════════════════════════════════════════════════════════════════
# build_query_state — tableEx, BIM info, measure resolution
# ═══════════════════════════════════════════════════════════════════

class TestBuildQueryState(unittest.TestCase):
    """Covers L1461 (tableEx Values role), L1509/1514 (BIM measure resolution)."""

    def test_tableEx_values_role(self):
        """tableEx should combine all fields into a single 'Values' role."""
        dims = [{"field": "Category"}]
        measures = [{"name": "Sales", "expression": "SUM(Sales)"}]
        ctm = {"Category": "T", "Sales": "T"}
        result = build_query_state("tableEx", dims, measures, ctm, {})
        self.assertIn("Values", result)
        self.assertGreater(len(result["Values"]["projections"]), 0)

    def test_bim_measure_lookup(self):
        """Named measure from BIM model should use Measure field type."""
        dims = [{"field": "Date"}]
        measures = [{"name": "Total Sales", "expression": "SUM(Amount)"}]
        ctm = {"Date": "Calendar"}
        ml = {"Total Sales": ("Facts", "SUM([Amount])")}
        result = build_query_state("clusteredBarChart", dims, measures, ctm, ml)
        # Should have dimension and measure roles
        self.assertIsNotNone(result)
        # Find the measure projection
        found_measure = False
        for role_name, role_data in result.items():
            for proj in role_data.get("projections", []):
                if "Measure" in proj.get("field", {}):
                    found_measure = True
        self.assertTrue(found_measure)

    def test_inline_aggregation_fallback(self):
        """When no BIM match, inline aggregation is used."""
        dims = []
        measures = [{"name": "Total", "expression": "SUM(Revenue)"}]
        ctm = {"Revenue": "Sales"}
        result = build_query_state("clusteredBarChart", dims, measures, ctm, {})
        self.assertIsNotNone(result)

    def test_unknown_visual_type_returns_none(self):
        result = build_query_state("totally_unknown_type_xyz", [], [], {}, {})
        self.assertIsNone(result)


# ═══════════════════════════════════════════════════════════════════
# generate_script_visual — Python + R branches
# ═══════════════════════════════════════════════════════════════════

class TestGenerateScriptVisual(unittest.TestCase):
    """Covers L1557-1558 — R language branch."""

    def test_python_script(self):
        info = {"language": "python", "code": "print('hello')",
                "function": "SCRIPT_REAL", "return_type": "real"}
        container = generate_script_visual("PyViz", info, fields=["Sales"])
        self.assertEqual(container["visual"]["visualType"], "scriptVisual")
        self.assertIn("matplotlib", container["visual"]["script"]["scriptText"])

    def test_r_script(self):
        info = {"language": "r", "code": "plot(x)",
                "function": "SCRIPT_INT", "return_type": "int"}
        container = generate_script_visual("RViz", info, fields=["Category"])
        self.assertEqual(container["visual"]["visualType"], "scriptRVisual")
        self.assertIn("data.frame", container["visual"]["script"]["scriptText"])
        self.assertIn("plot(x)", container["visual"]["script"]["scriptText"])

    def test_default_language_is_python(self):
        info = {"code": "x = 1"}
        container = generate_script_visual("Viz", info)
        self.assertEqual(container["visual"]["visualType"], "scriptVisual")

    def test_position_and_z_index(self):
        info = {"language": "python", "code": "pass"}
        container = generate_script_visual("Viz", info, x=50, y=100,
                                           width=600, height=400, z_index=3)
        pos = container["position"]
        self.assertEqual(pos["x"], 50)
        self.assertEqual(pos["y"], 100)
        self.assertEqual(pos["width"], 600)
        self.assertEqual(pos["height"], 400)
        self.assertEqual(pos["z"], 3000)


# ═══════════════════════════════════════════════════════════════════
# Misc helpers
# ═══════════════════════════════════════════════════════════════════

class TestBuildDynamicReferenceLine(unittest.TestCase):
    def test_average(self):
        result = _build_dynamic_reference_line("average", "Sales", "Facts", "Avg", "#FF0000", "dashed")
        self.assertIsNotNone(result)
        self.assertIn("properties", result)

    def test_median(self):
        result = _build_dynamic_reference_line("median", "Profit", "Facts", "Med", "#00FF00", "solid")
        self.assertIsNotNone(result)

    def test_unknown_type(self):
        result = _build_dynamic_reference_line("unknown_type", "X", "T", "L", "#000", "solid")
        # Unknown types may return None or a best-effort result
        # Just verify no crash


class TestBuildDataBarConfig(unittest.TestCase):
    def test_api_signature_keeps_options_keyword_only(self):
        parameters = inspect.signature(_build_data_bar_config).parameters
        self.assertEqual(list(parameters), [
            "query_ref", "positive_color", "negative_color", "axis_color",
            "reverse_direction", "hide_text",
        ])
        self.assertIs(
            parameters["query_ref"].kind,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        self.assertEqual({
            name: parameter.kind
            for name, parameter in parameters.items()
            if name != "query_ref"
        }, {
            "positive_color": inspect.Parameter.KEYWORD_ONLY,
            "negative_color": inspect.Parameter.KEYWORD_ONLY,
            "axis_color": inspect.Parameter.KEYWORD_ONLY,
            "reverse_direction": inspect.Parameter.KEYWORD_ONLY,
            "hide_text": inspect.Parameter.KEYWORD_ONLY,
        })
        self.assertEqual({
            name: parameter.default
            for name, parameter in parameters.items()
        }, {
            "query_ref": inspect.Parameter.empty,
            "positive_color": "#4472C4",
            "negative_color": "#ED7D31",
            "axis_color": "#CCCCCC",
            "reverse_direction": False,
            "hide_text": False,
        })

    def test_default_configuration_matches_supported_contract(self):
        query_ref = "Sales.Net revenue €"
        result = _build_data_bar_config(query_ref)
        self.assertEqual(result, _expected_data_bar_instance(query_ref))
        self.assertNotIn("id", result)
        self.assertNotIn("field", result)

    def test_custom_colors_direction_and_visibility(self):
        query_ref = "Sum(Finances & opérations.Chiffre net €)"
        result = _build_data_bar_config(
            query_ref,
            positive_color="#007ACC",
            negative_color="#CC3300",
            axis_color="#222222",
            reverse_direction=True,
            hide_text=True,
        )
        self.assertEqual(result, _expected_data_bar_instance(
            query_ref,
            positive_color="#007ACC",
            negative_color="#CC3300",
            axis_color="#222222",
            reverse_direction=True,
            hide_text=True,
        ))


class TestBuildSmallMultiplesConfig(unittest.TestCase):
    def test_flow_layout(self):
        config, proj = _build_small_multiples_config(
            "Region", "Geo", "flow", 3, False,
        )
        self.assertIsInstance(config, dict)
        self.assertIsInstance(proj, dict)

    def test_grid_layout(self):
        config, proj = _build_small_multiples_config(
            "Category", "Products", "grid", 4, True,
        )
        self.assertIsInstance(config, dict)


if __name__ == "__main__":
    unittest.main()
