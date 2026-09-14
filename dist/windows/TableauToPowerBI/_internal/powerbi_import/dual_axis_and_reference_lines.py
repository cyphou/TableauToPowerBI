"""Enhanced visual support for dual-axis charts and reference lines.

This module provides lightweight helpers used by the PBIR generator.
It does not write PBIR directly; it normalizes worksheet metadata so the
existing visual pipeline can produce better output.
"""

from typing import Any, Dict, List


class DualAxisBuilder:
    """Build and detect dual-axis chart metadata."""

    @staticmethod
    def _measure_fields(worksheet: Dict[str, Any]) -> List[Dict[str, Any]]:
        fields = worksheet.get("fields", []) or []
        measures = []
        for field in fields:
            role = str(field.get("type", "")).lower()
            shelf = str(field.get("shelf", "")).lower()
            has_agg = bool(field.get("aggregation"))
            if role == "measure" or shelf == "measure_value" or has_agg:
                measures.append(field)
        return measures

    @staticmethod
    def detect_dual_axis_worksheet(worksheet: Dict[str, Any]) -> bool:
        """Detect whether a worksheet likely requires a dual-axis combo chart."""
        axes = worksheet.get("axes", {})
        if isinstance(axes, dict):
            if bool(axes.get("dual_axis")) or bool(axes.get("dual_axis_sync")):
                return True
            dual_axis_meta = axes.get("dual_axis")
            if isinstance(dual_axis_meta, dict) and dual_axis_meta:
                return True
        elif isinstance(axes, list) and len(axes) > 1:
            return True

        return len(DualAxisBuilder._measure_fields(worksheet)) >= 2

    @staticmethod
    def extract_axis_config(worksheet: Dict[str, Any], axis_idx: int = 0) -> Dict[str, Any]:
        """Extract axis formatting info, supporting both dict and list axis payloads."""
        axes = worksheet.get("axes", {})
        if isinstance(axes, list):
            if axis_idx >= len(axes):
                return {}
            axis = axes[axis_idx] or {}
            return {
                "title": axis.get("title", ""),
                "format": axis.get("format", ""),
                "scale_type": axis.get("scaleType", "linear"),
                "reversed": bool(axis.get("reversed", False)),
            }

        if isinstance(axes, dict):
            key = "primary" if axis_idx == 0 else "secondary"
            axis = axes.get(key, {})
            if not isinstance(axis, dict):
                axis = {}
            return {
                "title": axis.get("title", ""),
                "format": axis.get("format", ""),
                "scale_type": axis.get("scaleType", "linear"),
                "reversed": bool(axis.get("reversed", False)),
            }

        return {}

    @staticmethod
    def build_combo_chart_config(tableau_ws: Dict[str, Any]) -> Dict[str, Any]:
        """Build normalized combo configuration metadata."""
        measure_fields = DualAxisBuilder._measure_fields(tableau_ws)
        primary = measure_fields[0] if len(measure_fields) >= 1 else {}
        secondary = measure_fields[1] if len(measure_fields) >= 2 else {}

        return {
            "type": "lineClusteredColumnComboChart",
            "lineSeriesName": primary.get("name", ""),
            "columnSeriesName": secondary.get("name", ""),
            "primaryYAxisTitle": DualAxisBuilder.extract_axis_config(tableau_ws, 0).get("title", ""),
            "secondaryYAxisTitle": DualAxisBuilder.extract_axis_config(tableau_ws, 1).get("title", ""),
        }

    @staticmethod
    def inject_combo_axis_formatting(
        config: Dict[str, Any],
        primary_axis: Dict[str, Any],
        secondary_axis: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Add axis formatting hints to combo config metadata."""
        if primary_axis.get("format"):
            config["primaryYAxisNumberFormat"] = primary_axis["format"]
        if secondary_axis.get("format"):
            config["secondaryYAxisNumberFormat"] = secondary_axis["format"]
        if primary_axis.get("reversed"):
            config["primaryYAxisReversed"] = True
        if secondary_axis.get("reversed"):
            config["secondaryYAxisReversed"] = True
        if primary_axis.get("scale_type") == "logarithmic":
            config["primaryYAxisLogarithmic"] = True
        if secondary_axis.get("scale_type") == "logarithmic":
            config["secondaryYAxisLogarithmic"] = True
        return config


class ReferenceLineBuilder:
    """Normalize Tableau reference lines for PBIR generation."""

    @staticmethod
    def extract_reference_lines(worksheet: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract normalized reference line records from worksheet metadata."""
        raw_lines = worksheet.get("reference_lines", []) or []
        lines = []
        for ref_line in raw_lines:
            if not isinstance(ref_line, dict):
                continue
            lines.append(
                {
                    "name": ref_line.get("name", "Reference Line"),
                    "type": ref_line.get("type", "constant"),
                    "value": ref_line.get("value"),
                    "field": ref_line.get("field"),
                    "color": ref_line.get("color", "#808080"),
                    "line_style": ref_line.get("lineStyle", "solid"),
                    "thickness": ref_line.get("thickness", 1),
                    "label": ref_line.get("label", ""),
                    "axis": ref_line.get("axis", "primary"),
                    "tooltip": bool(ref_line.get("tooltip", True)),
                    "format": ref_line.get("format", ""),
                }
            )
        return lines

    @staticmethod
    def build_pbi_reference_line_config(ref_line_def: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a normalized reference line to PBIR-friendly metadata."""
        line_style_map = {
            "solid": "solid",
            "dashed": "dashed",
            "dotted": "dotted",
            "double": "solid",
        }

        return {
            "name": ref_line_def.get("label") or ref_line_def.get("name", "Reference Line"),
            "value": ref_line_def.get("value"),
            "color": ref_line_def.get("color", "#808080"),
            "lineStyle": line_style_map.get(ref_line_def.get("line_style", "solid"), "solid"),
            "thickness": ref_line_def.get("thickness", 1),
            "showLabel": bool(ref_line_def.get("label")),
            "axis": ref_line_def.get("axis", "primary"),
            "tooltip": bool(ref_line_def.get("tooltip", True)),
        }

    @staticmethod
    def generate_reference_line_dax(ref_line_def: Dict[str, Any], measure_name: str = "Value") -> str:
        """Generate a DAX expression for computed reference line types."""
        ref_type = str(ref_line_def.get("type", "constant")).lower()
        if ref_type == "constant":
            value = ref_line_def.get("value", 0)
            return f"VAR constant_value = {value} RETURN constant_value"
        if ref_type == "average":
            return f"AVERAGE([{measure_name}])"
        if ref_type == "median":
            return f"MEDIAN([{measure_name}])"
        if ref_type == "percentile":
            pct = ref_line_def.get("value", 50)
            return f"PERCENTILE.INC([{measure_name}], {float(pct) / 100.0})"
        return f"AVERAGE([{measure_name}])"
