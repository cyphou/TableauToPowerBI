"""Coverage inventory tests for Tableau visual mappings."""

import unittest

from powerbi_import.visual_mapping_matrix import (
    build_visual_mapping_matrix,
    find_visual_approximations_in_use,
    summarize_visual_mapping_matrix,
)


class TestVisualMappingMatrix(unittest.TestCase):
    def test_native_approximate_and_custom_mappings_are_visible(self):
        rows = build_visual_mapping_matrix()
        summary = summarize_visual_mapping_matrix(rows)
        self.assertGreaterEqual(summary["mappings"], 150)
        self.assertGreater(summary["status_counts"].get("native", 0), 0)
        self.assertGreater(summary["status_counts"].get("approximation", 0), 0)
        self.assertGreater(summary["status_counts"].get("custom_visual", 0), 0)
        self.assertIn("barchart", summary["sources"])
        approximations = [row for row in rows if row["status"] == "approximation"]
        self.assertTrue(all(row["owner"] == "@visual" for row in approximations))
        self.assertTrue(all(row["remediation"] for row in approximations))

    def test_approximations_are_scoped_to_source_worksheets(self):
        matches = find_visual_approximations_in_use({
            "worksheets": [{"name": "Demo", "original_mark_class": "Gantt Bar"}]
        })
        self.assertTrue(matches)
        self.assertEqual(matches[0]["worksheet"], "Demo")


if __name__ == "__main__":
    unittest.main()
