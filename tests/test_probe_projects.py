"""Tests for the scripts/probe_projects.py Desktop verification gate."""

import json
import os
import tempfile
import unittest
from unittest import mock

from scripts import probe_projects


def _report(*, window_loaded, data_status, screenshot="shot.png", **totals):
    data_load = {
        "status": data_status,
        "tables_checked": totals.get("tables_checked", 0),
        "tables_nonempty": totals.get("tables_nonempty", 0),
        "tables_empty": totals.get("tables_empty", 0),
        "tables_failed": totals.get("tables_failed", 0),
        "total_rows": totals.get("total_rows", 0),
    }
    return mock.Mock(to_dict=mock.Mock(return_value={
        "window_loaded": window_loaded,
        "duration_s": 0.1,
        "screenshot": screenshot,
        "data_load": data_load,
    }))


class TestProbeProjects(unittest.TestCase):
    def _run(self, reports, *arguments):
        json_path = os.path.join(tempfile.gettempdir(), "probe-projects-test.json")
        with mock.patch.object(probe_projects, "find_pbi_desktop", return_value=True), \
             mock.patch.object(probe_projects, "desktop_pids", return_value=[]), \
             mock.patch.object(probe_projects.glob, "glob",
                               return_value=[f"C:/projects/{i}.pbip"
                                             for i in range(len(reports))]), \
             mock.patch.object(probe_projects.os, "makedirs"), \
             mock.patch.object(probe_projects, "probe_desktop_open",
                               side_effect=reports) as probe:
            result = probe_projects.main([
                "C:/projects", "--shots", "C:/shots", "--json", json_path,
                *arguments,
            ])
        with open(json_path, encoding="utf-8") as handle:
            written = json.load(handle)
        os.remove(json_path)
        return result, probe, written

    def test_default_requires_window_and_verified_data_and_keeps_totals(self):
        reports = [
            _report(window_loaded=True, data_status="verified",
                    tables_checked=2, tables_nonempty=2, total_rows=17),
            _report(window_loaded=True, data_status="empty"),
            _report(window_loaded=False, data_status="verified"),
        ]

        result, probe, written = self._run(reports)

        self.assertEqual(result, 1)
        self.assertEqual([call.kwargs["verify_data"] for call in probe.call_args_list],
                         [True, True, True])
        self.assertEqual(written[0]["data_load"]["status"], "verified")
        self.assertEqual(written[0]["data_load"]["tables_checked"], 2)
        self.assertEqual(written[0]["data_load"]["tables_nonempty"], 2)
        self.assertEqual(written[0]["data_load"]["total_rows"], 17)

    def test_skip_data_check_allows_window_only_diagnostic_screenshot(self):
        report = _report(window_loaded=True, data_status="unavailable",
                         screenshot="diagnostic.png")

        result, probe, written = self._run([report], "--skip-data-check")

        self.assertEqual(result, 0)
        probe.assert_called_once()
        self.assertFalse(probe.call_args.kwargs["verify_data"])
        self.assertEqual(written[0]["screenshot"], "diagnostic.png")
        self.assertEqual(written[0]["data_load"]["status"], "unavailable")

    def test_refresh_is_on_by_default_and_can_be_disabled(self):
        _, probe, _ = self._run([_report(window_loaded=True, data_status="verified")])
        self.assertTrue(probe.call_args.kwargs["refresh"])
        self.assertTrue(probe.call_args.kwargs["capture_unverified"])
        _, probe, _ = self._run([_report(window_loaded=True, data_status="verified")],
                                "--no-refresh")
        self.assertFalse(probe.call_args.kwargs["refresh"])

    def test_html_report_has_status_cards_and_relative_screenshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            shot = os.path.join(tmp, "shots", "A.png")
            os.makedirs(os.path.dirname(shot))
            open(shot, "wb").close()
            html_path = os.path.join(tmp, "DESKTOP_OPENING_VALIDATION.html")
            text = probe_projects._html_report([
                {"pbip_path": "C:/p/A.pbip", "window_loaded": True, "screenshot": shot,
                 "screenshot_data_verified": True,
                 "refresh": {"mode": "schema_and_data", "status": "verified"},
                 "data_load": {"status": "verified", "tables_checked": 2,
                               "tables_nonempty": 2, "total_rows": 9}},
                {"pbip_path": "C:/p/B.pbip", "window_loaded": False},
            ], html_path)
        self.assertIn('class="card ok">OPENED', text)
        self.assertIn('class="card ko">CRASHED', text)
        self.assertIn('src="shots/A.png"', text)
        self.assertIn("schema_and_data", text)
        self.assertNotIn(tmp, text)


if __name__ == "__main__":
    unittest.main()