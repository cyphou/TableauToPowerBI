import json
import os
import tempfile
import unittest

from powerbi_import.page_composition import (
    build_page_blueprint,
    compare_page_composition,
)


def _dashboard():
    return {
        "name": "Sales",
        "size": {"width": 800, "height": 600},
        "zone_hierarchy": {
            "name": "root",
            "orientation": "horz",
            "children": [{"name": "Orders", "zone_type": "worksheet"}],
        },
        "objects": [{
            "type": "worksheetReference",
            "worksheetName": "Orders",
            "position": {"x": 10, "y": 20, "w": 300, "h": 200},
        }],
    }


def _write_page(root, position, z=0):
    page_dir = os.path.join(root, "Demo.Report", "definition", "pages", "p1")
    visual_dir = os.path.join(page_dir, "visuals", "v1")
    os.makedirs(visual_dir)
    with open(os.path.join(page_dir, "page.json"), "w", encoding="utf-8") as handle:
        json.dump({"displayName": "Sales", "width": 800, "height": 600}, handle)
    with open(os.path.join(visual_dir, "visual.json"), "w", encoding="utf-8") as handle:
        json.dump({"name": "target-v1", "position": {**position, "z": z}}, handle)


class TestPageComposition(unittest.TestCase):
    def test_blueprint_ids_are_deterministic(self):
        first = build_page_blueprint(_dashboard())
        second = build_page_blueprint(_dashboard())
        self.assertEqual(first["page_id"], second["page_id"])
        self.assertEqual(first["objects"][0]["stable_id"], second["objects"][0]["stable_id"])
        self.assertEqual(first["zones"][1]["stable_id"], second["zones"][1]["stable_id"])

    def test_exact_geometry_passes(self):
        with tempfile.TemporaryDirectory() as root:
            _write_page(root, {"x": 10, "y": 20, "width": 300, "height": 200})
            result = compare_page_composition(_dashboard(), os.path.join(root, "Demo.Report"))

        self.assertEqual(result["status"], "pass")
        self.assertEqual(len(result["matched"]), 1)
        self.assertEqual(result["mismatched"], [])

    def test_position_and_z_order_drift_is_reported(self):
        with tempfile.TemporaryDirectory() as root:
            _write_page(root, {"x": 50, "y": 20, "width": 300, "height": 200}, z=8)
            result = compare_page_composition(_dashboard(), os.path.join(root, "Demo.Report"))

        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["mismatched"][0]["differences"], ["x", "z"])


if __name__ == "__main__":
    unittest.main()