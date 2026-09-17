import json
import os
import tempfile
import unittest

from powerbi_import.interaction_graph import build_interaction_graph


class TestInteractionGraph(unittest.TestCase):
    def test_extracts_actions_sync_filters_and_disabled_edges(self):
        with tempfile.TemporaryDirectory() as root:
            visual_dir = os.path.join(root, "Demo.Report", "definition", "pages", "p1", "visuals", "v1")
            os.makedirs(visual_dir)
            with open(os.path.join(visual_dir, "visual.json"), "w", encoding="utf-8") as handle:
                json.dump({
                    "name": "source-v1",
                    "syncGroup": {"groupName": "Region slicers"},
                    "filterConfig": {
                        "disabled": True,
                        "filters": [{"field": "Region"}],
                    },
                    "visual": {"objects": {"action": [{"properties": {
                        "type": {"expr": {"Literal": {"Value": "'PageNavigation'"}}},
                        "destination": {"expr": {"Literal": {"Value": "'Details'"}}},
                    }}]}},
                }, handle)

            result = build_interaction_graph(
                {"actions": [{"type": "navigate"}]}, root, "Demo"
            )

        self.assertEqual(result["status"], "scanned")
        self.assertEqual(result["runtime"], "not_run")
        self.assertEqual(result["source_action_count"], 1)
        self.assertEqual(result["summary"]["action_edges"], 1)
        self.assertEqual(result["summary"]["sync_edges"], 1)
        self.assertEqual(result["summary"]["filter_edges"], 1)
        self.assertEqual(result["summary"]["disabled_edges"], 1)
        action = next(edge for edge in result["edges"] if edge["kind"] == "action")
        self.assertEqual(action["destination"], "Details")


if __name__ == "__main__":
    unittest.main()