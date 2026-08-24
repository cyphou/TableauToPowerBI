"""Sprint 135 — Tests for approximation elimination improvements.

Covers: lollipop, butterfly, calendar heat map, waffle, slope chart, timeline.
Each test verifies that the enhanced approximation produces the correct PBI
visual config, auto-generated measures, and migration notes.
"""

import unittest

from powerbi_import.visual_generator import (
    resolve_visual_type,
    get_approximation_note,
    create_visual_container,
    _apply_visual_decorations,
    _build_visual_query_state,
    _AUTO_GENERATED_MEASURES,
    APPROXIMATION_MAP,
    VISUAL_TYPE_MAP,
    VISUAL_FALLBACK_CASCADE,
)


class TestLollipopApproximation(unittest.TestCase):
    """135.1 — Lollipop chart → clusteredBarChart with thin bars + data labels."""

    def test_resolve_type(self):
        self.assertEqual(resolve_visual_type("lollipop"), "clusteredBarChart")

    def test_approximation_note(self):
        note = get_approximation_note("lollipop")
        self.assertIsNotNone(note)
        self.assertIn("Lollipop", note)
        self.assertIn("circle markers", note)

    def test_visual_container_has_thin_bars(self):
        ws = {
            "name": "Lollipop Test",
            "visualType": "lollipop",
            "dimensions": [{"field": "Category"}],
            "measures": [{"name": "Value", "expression": "SUM(Value)"}],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="lolli-1",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Category": "T", "Value": "T"},
        )
        visual = container["visual"]
        self.assertEqual(visual["visualType"], "clusteredBarChart")
        # Must have spacing object with innerPadding for thin bars
        self.assertIn("objects", visual)
        self.assertIn("spacing", visual["objects"])
        padding_props = visual["objects"]["spacing"][0]["properties"]
        self.assertIn("innerPadding", padding_props)

    def test_visual_container_has_data_labels(self):
        ws = {
            "name": "Lollipop Test",
            "visualType": "lollipop",
            "dimensions": [{"field": "Category"}],
            "measures": [{"name": "Value", "expression": "SUM(Value)"}],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="lolli-2",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Category": "T", "Value": "T"},
        )
        visual = container["visual"]
        self.assertIn("labels", visual["objects"])
        label_props = visual["objects"]["labels"][0]["properties"]
        self.assertIn("show", label_props)

    def test_visual_container_has_migration_note(self):
        ws = {
            "name": "Lollipop Test",
            "visualType": "lollipop",
            "dimensions": [{"field": "Category"}],
            "measures": [{"name": "Value", "expression": "SUM(Value)"}],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="lolli-3",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Category": "T", "Value": "T"},
        )
        visual = container["visual"]
        # PBIR v4.0: annotations live at the container root, not in visual.
        self.assertIn("annotations", container)
        notes = [a["value"] for a in container["annotations"] if a["name"] == "MigrationNote"]
        self.assertTrue(any("Lollipop" in n for n in notes))

    def test_lollipop_in_approximation_map(self):
        self.assertIn("lollipop", APPROXIMATION_MAP)
        pbi_type, note = APPROXIMATION_MAP["lollipop"]
        self.assertEqual(pbi_type, "clusteredBarChart")


class TestButterflyApproximation(unittest.TestCase):
    """135.2 — Butterfly chart → hundredPercentStackedBarChart with NEGATE measure."""

    def setUp(self):
        # Clear auto-generated measures before each test
        _AUTO_GENERATED_MEASURES.clear()

    def test_resolve_type(self):
        self.assertEqual(resolve_visual_type("butterfly"), "hundredPercentStackedBarChart")

    def test_approximation_note(self):
        note = get_approximation_note("butterfly")
        self.assertIsNotNone(note)
        self.assertIn("NEGATE", note)

    def test_negate_measure_auto_generated(self):
        ws = {
            "name": "Butterfly Test",
            "visualType": "butterfly",
            "dimensions": [{"field": "Category"}],
            "measures": [
                {"name": "Male", "expression": "SUM(Male)"},
                {"name": "Female", "expression": "SUM(Female)"},
            ],
        }
        ctm = {"Category": "T", "Male": "T", "Female": "T"}
        container = create_visual_container(
            worksheet=ws, visual_id="bfly-1",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map=ctm,
        )
        visual = container["visual"]
        self.assertEqual(visual["visualType"], "hundredPercentStackedBarChart")
        # A NEGATE measure should have been registered
        neg_measures = [m for m in _AUTO_GENERATED_MEASURES if m['name'].startswith('_neg_')]
        self.assertGreaterEqual(len(neg_measures), 1)
        neg = neg_measures[0]
        self.assertIn("-[", neg['expression'])

    def test_butterfly_legend_shown(self):
        ws = {
            "name": "Butterfly Test",
            "visualType": "butterfly",
            "dimensions": [{"field": "Category"}],
            "measures": [
                {"name": "Male", "expression": "SUM(Male)"},
                {"name": "Female", "expression": "SUM(Female)"},
            ],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="bfly-2",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Category": "T", "Male": "T", "Female": "T"},
        )
        visual = container["visual"]
        self.assertIn("objects", visual)
        self.assertIn("legend", visual["objects"])

    def test_butterfly_axis_title_hidden(self):
        ws = {
            "name": "Butterfly Test",
            "visualType": "butterfly",
            "dimensions": [{"field": "Category"}],
            "measures": [
                {"name": "Male", "expression": "SUM(Male)"},
                {"name": "Female", "expression": "SUM(Female)"},
            ],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="bfly-3",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Category": "T", "Male": "T", "Female": "T"},
        )
        visual = container["visual"]
        va = visual["objects"].get("valueAxis", [{}])
        if va:
            props = va[0].get("properties", {})
            self.assertIn("showAxisTitle", props)

    def test_butterfly_single_measure_no_negate(self):
        """With only 1 measure, no NEGATE should be generated."""
        _AUTO_GENERATED_MEASURES.clear()
        ws = {
            "name": "Butterfly Test",
            "visualType": "butterfly",
            "dimensions": [{"field": "Category"}],
            "measures": [{"name": "Value", "expression": "SUM(Value)"}],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="bfly-4",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Category": "T", "Value": "T"},
        )
        neg_measures = [m for m in _AUTO_GENERATED_MEASURES if m['name'].startswith('_neg_')]
        self.assertEqual(len(neg_measures), 0)


class TestCalendarHeatMapApproximation(unittest.TestCase):
    """135.3 — Calendar heat map → matrix with gradient conditional formatting."""

    def test_resolve_type(self):
        self.assertEqual(resolve_visual_type("calendarheatmap"), "matrix")

    def test_approximation_note_updated(self):
        note = get_approximation_note("calendarheatmap")
        self.assertIsNotNone(note)
        self.assertIn("auto-configured", note)

    def test_gradient_from_palette(self):
        ws = {
            "name": "Calendar Heat Map",
            "visualType": "calendarheatmap",
            "dimensions": [{"field": "Date"}],
            "measures": [{"name": "Value", "expression": "SUM(Value)"}],
            "mark_encoding": {
                "color": {
                    "type": "quantitative",
                    "palette_colors": ["#FFFFFF", "#FFD700", "#FF4500"],
                }
            },
        }
        container = create_visual_container(
            worksheet=ws, visual_id="cal-1",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Date": "T", "Value": "T"},
        )
        visual = container["visual"]
        self.assertEqual(visual["visualType"], "matrix")
        values_obj = visual["objects"].get("values", [{}])
        props = values_obj[0].get("properties", {})
        self.assertIn("backColorConditionalFormatting", props)
        # Should have a fillRule with gradient
        self.assertIn("fillRule", props)
        fill_rule = props["fillRule"]
        # 3-stop gradient for 3 colors
        self.assertIn("linearGradient3", fill_rule)

    def test_gradient_2_colors(self):
        ws = {
            "name": "Heat Map 2-Color",
            "visualType": "calendarheatmap",
            "dimensions": [{"field": "Date"}],
            "measures": [{"name": "Value", "expression": "SUM(Value)"}],
            "mark_encoding": {
                "color": {
                    "type": "quantitative",
                    "palette_colors": ["#FFFFFF", "#FF0000"],
                }
            },
        }
        container = create_visual_container(
            worksheet=ws, visual_id="cal-2",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Date": "T", "Value": "T"},
        )
        visual = container["visual"]
        props = visual["objects"]["values"][0]["properties"]
        self.assertIn("fillRule", props)
        self.assertIn("linearGradient2", props["fillRule"])

    def test_no_palette_falls_back(self):
        ws = {
            "name": "Heat Map No Color",
            "visualType": "calendarheatmap",
            "dimensions": [{"field": "Date"}],
            "measures": [{"name": "Value", "expression": "SUM(Value)"}],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="cal-3",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Date": "T", "Value": "T"},
        )
        visual = container["visual"]
        props = visual["objects"]["values"][0]["properties"]
        self.assertIn("backColorConditionalFormatting", props)
        # No fillRule if no palette
        self.assertNotIn("fillRule", props)

    def test_migration_note_mentions_day_week(self):
        ws = {
            "name": "Calendar Heat Map",
            "visualType": "calendarheatmap",
            "dimensions": [{"field": "Date"}],
            "measures": [{"name": "Value", "expression": "SUM(Value)"}],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="cal-4",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Date": "T", "Value": "T"},
        )
        visual = container["visual"]
        # PBIR v4.0: annotations live at the container root, not in visual.
        notes = [a["value"] for a in container.get("annotations", []) if a["name"] == "MigrationNote"]
        self.assertTrue(any("DayOfWeek" in n for n in notes))


class TestWaffleApproximation(unittest.TestCase):
    """135.4 — Waffle chart → multiRowCard."""

    def test_resolve_type(self):
        self.assertEqual(resolve_visual_type("waffle"), "multiRowCard")

    def test_visual_type_map_entry(self):
        self.assertEqual(VISUAL_TYPE_MAP["waffle"], "multiRowCard")

    def test_approximation_note(self):
        note = get_approximation_note("waffle")
        self.assertIsNotNone(note)
        self.assertIn("Multi-Row Card", note)

    def test_visual_container_creates_multirowcard(self):
        ws = {
            "name": "Waffle Test",
            "visualType": "waffle",
            "dimensions": [{"field": "Category"}],
            "measures": [{"name": "Pct", "expression": "SUM(Pct)"}],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="waffle-1",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Category": "T", "Pct": "T"},
        )
        visual = container["visual"]
        self.assertEqual(visual["visualType"], "multiRowCard")

    def test_fallback_cascade(self):
        self.assertEqual(VISUAL_FALLBACK_CASCADE.get("multiRowCard"), "card")


class TestSlopeChartApproximation(unittest.TestCase):
    """135.5 — Slope chart → lineChart with markers + data labels."""

    def test_resolve_type(self):
        self.assertEqual(resolve_visual_type("slopechart"), "lineChart")

    def test_approximation_note(self):
        note = get_approximation_note("slopechart")
        self.assertIsNotNone(note)
        self.assertIn("markers", note)

    def test_visual_container_has_markers(self):
        ws = {
            "name": "Slope Chart",
            "visualType": "slopechart",
            "dimensions": [{"field": "Period"}],
            "measures": [{"name": "Value", "expression": "SUM(Value)"}],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="slope-1",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Period": "T", "Value": "T"},
        )
        visual = container["visual"]
        self.assertEqual(visual["visualType"], "lineChart")
        self.assertIn("objects", visual)
        dp = visual["objects"].get("dataPoint", [{}])
        props = dp[0].get("properties", {})
        self.assertIn("showMarkers", props)

    def test_visual_container_has_labels(self):
        ws = {
            "name": "Slope Chart",
            "visualType": "slopechart",
            "dimensions": [{"field": "Period"}],
            "measures": [{"name": "Value", "expression": "SUM(Value)"}],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="slope-2",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Period": "T", "Value": "T"},
        )
        visual = container["visual"]
        self.assertIn("labels", visual["objects"])
        label_props = visual["objects"]["labels"][0]["properties"]
        self.assertIn("show", label_props)

    def test_marker_size_set(self):
        ws = {
            "name": "Slope Chart",
            "visualType": "slopechart",
            "dimensions": [{"field": "Period"}],
            "measures": [{"name": "Value", "expression": "SUM(Value)"}],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="slope-3",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Period": "T", "Value": "T"},
        )
        visual = container["visual"]
        dp = visual["objects"]["dataPoint"][0]["properties"]
        self.assertIn("markerSize", dp)


class TestTimelineApproximation(unittest.TestCase):
    """135.6 — Timeline → lineChart with diamond shape markers."""

    def test_resolve_type(self):
        self.assertEqual(resolve_visual_type("timeline"), "lineChart")

    def test_approximation_note(self):
        note = get_approximation_note("timeline")
        self.assertIsNotNone(note)
        self.assertIn("shape markers", note)

    def test_visual_container_has_markers(self):
        ws = {
            "name": "Timeline",
            "visualType": "timeline",
            "dimensions": [{"field": "Date"}],
            "measures": [{"name": "Events", "expression": "COUNT(Events)"}],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="tl-1",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Date": "T", "Events": "T"},
        )
        visual = container["visual"]
        self.assertEqual(visual["visualType"], "lineChart")
        self.assertIn("objects", visual)
        dp = visual["objects"].get("dataPoint", [{}])
        props = dp[0].get("properties", {})
        self.assertIn("showMarkers", props)

    def test_diamond_marker_shape(self):
        ws = {
            "name": "Timeline",
            "visualType": "timeline",
            "dimensions": [{"field": "Date"}],
            "measures": [{"name": "Events", "expression": "COUNT(Events)"}],
        }
        container = create_visual_container(
            worksheet=ws, visual_id="tl-2",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map={"Date": "T", "Events": "T"},
        )
        visual = container["visual"]
        dp = visual["objects"]["dataPoint"][0]["properties"]
        self.assertIn("markerShape", dp)


class TestApproximationMapCompleteness(unittest.TestCase):
    """Verify APPROXIMATION_MAP structure and coverage."""

    def test_all_entries_have_two_tuple(self):
        for key, value in APPROXIMATION_MAP.items():
            self.assertIsInstance(value, tuple, f"{key} should be a tuple")
            self.assertEqual(len(value), 2, f"{key} should have (type, note)")

    def test_all_approx_types_are_in_visual_type_map_or_standalone(self):
        """Every APPROXIMATION_MAP key should also be in VISUAL_TYPE_MAP."""
        for key in APPROXIMATION_MAP:
            self.assertIn(key, VISUAL_TYPE_MAP,
                          f"{key} in APPROXIMATION_MAP but not in VISUAL_TYPE_MAP")

    def test_pbi_types_are_valid(self):
        """The PBI type in each APPROXIMATION_MAP entry should be a known PBI visual type."""
        known_types = set(VISUAL_TYPE_MAP.values())
        for key, (pbi_type, _) in APPROXIMATION_MAP.items():
            self.assertIn(pbi_type, known_types,
                          f"{key} maps to unknown PBI type {pbi_type}")

    def test_notes_are_non_empty(self):
        for key, (_, note) in APPROXIMATION_MAP.items():
            self.assertTrue(len(note) > 10, f"{key} has a too-short migration note")

    def test_lollipop_entry_exists(self):
        self.assertIn("lollipop", APPROXIMATION_MAP)

    def test_waffle_entry_exists(self):
        self.assertIn("waffle", APPROXIMATION_MAP)

    def test_calendar_heatmap_entry_exists(self):
        self.assertIn("calendarheatmap", APPROXIMATION_MAP)

    def test_slopechart_entry_exists(self):
        self.assertIn("slopechart", APPROXIMATION_MAP)

    def test_timeline_entry_exists(self):
        self.assertIn("timeline", APPROXIMATION_MAP)

    def test_butterfly_entry_exists(self):
        self.assertIn("butterfly", APPROXIMATION_MAP)


class TestRoundTripApproximations(unittest.TestCase):
    """End-to-end: extract → generate → verify for each improved approximation type."""

    def _roundtrip(self, visual_type, dims, measures, ctm):
        ws = {
            "name": f"{visual_type} RT",
            "visualType": visual_type,
            "dimensions": dims,
            "measures": measures,
        }
        container = create_visual_container(
            worksheet=ws, visual_id=f"rt-{visual_type}",
            x=0, y=0, width=400, height=300, z_index=0,
            col_table_map=ctm,
        )
        return container

    def test_roundtrip_lollipop(self):
        c = self._roundtrip("lollipop",
                            [{"field": "Category"}],
                            [{"name": "Sales", "expression": "SUM(Sales)"}],
                            {"Category": "T", "Sales": "T"})
        self.assertEqual(c["visual"]["visualType"], "clusteredBarChart")
        self.assertIn("spacing", c["visual"]["objects"])

    def test_roundtrip_butterfly(self):
        _AUTO_GENERATED_MEASURES.clear()
        c = self._roundtrip("butterfly",
                            [{"field": "Age"}],
                            [{"name": "Male", "expression": "SUM(Male)"},
                             {"name": "Female", "expression": "SUM(Female)"}],
                            {"Age": "T", "Male": "T", "Female": "T"})
        self.assertEqual(c["visual"]["visualType"], "hundredPercentStackedBarChart")

    def test_roundtrip_calendarheatmap(self):
        c = self._roundtrip("calendarheatmap",
                            [{"field": "Date"}],
                            [{"name": "Temp", "expression": "AVG(Temp)"}],
                            {"Date": "T", "Temp": "T"})
        self.assertEqual(c["visual"]["visualType"], "matrix")
        self.assertIn("values", c["visual"]["objects"])

    def test_roundtrip_waffle(self):
        c = self._roundtrip("waffle",
                            [{"field": "Status"}],
                            [{"name": "Pct", "expression": "SUM(Pct)"}],
                            {"Status": "T", "Pct": "T"})
        self.assertEqual(c["visual"]["visualType"], "multiRowCard")

    def test_roundtrip_slopechart(self):
        c = self._roundtrip("slopechart",
                            [{"field": "Year"}],
                            [{"name": "Revenue", "expression": "SUM(Revenue)"}],
                            {"Year": "T", "Revenue": "T"})
        self.assertEqual(c["visual"]["visualType"], "lineChart")
        self.assertIn("dataPoint", c["visual"]["objects"])

    def test_roundtrip_timeline(self):
        c = self._roundtrip("timeline",
                            [{"field": "Date"}],
                            [{"name": "Events", "expression": "COUNT(Events)"}],
                            {"Date": "T", "Events": "T"})
        self.assertEqual(c["visual"]["visualType"], "lineChart")
        self.assertIn("dataPoint", c["visual"]["objects"])


if __name__ == "__main__":
    unittest.main()
