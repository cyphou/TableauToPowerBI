"""Tests for the best-effort Power BI Desktop open self-check (desktop_probe.py).

All Desktop launches are mocked — no real GUI is spawned.
"""

import os
import tempfile
import unittest
from unittest import mock

from powerbi_import.desktop_probe import (
    probe_desktop_open, find_pbi_desktop, DesktopProbeReport,
)


def _pbip(root):
    p = os.path.join(root, "P.pbip")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"version": "1.0"}')
    return p


class _FakeProc:
    """Minimal subprocess.Popen stand-in."""
    def __init__(self, pid=4242, poll_seq=None):
        self.pid = pid
        self._seq = list(poll_seq or [None])
        self._i = 0
        self.terminated = False
        self.killed = False

    def poll(self):
        v = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return v

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class TestFind(unittest.TestCase):
    def test_returns_none_when_absent(self):
        with mock.patch("powerbi_import.desktop_probe.shutil.which", return_value=None), \
             mock.patch.dict(os.environ, {"ProgramFiles": "", "ProgramW6432": "",
                                          "ProgramFiles(x86)": ""}, clear=False):
            self.assertIsNone(find_pbi_desktop())

    def test_returns_which_hit(self):
        with mock.patch("powerbi_import.desktop_probe.shutil.which",
                        return_value=r"C:\pbi\PBIDesktop.exe"):
            self.assertEqual(find_pbi_desktop(), r"C:\pbi\PBIDesktop.exe")


class TestProbe(unittest.TestCase):
    def test_missing_pbip_errors(self):
        r = probe_desktop_open(os.path.join(tempfile.gettempdir(), "nope.pbip"))
        self.assertEqual(r.status, "error")
        self.assertFalse(r.opened)

    def test_desktop_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("powerbi_import.desktop_probe.find_pbi_desktop",
                            return_value=None):
                r = probe_desktop_open(_pbip(d))
        self.assertEqual(r.status, "unavailable")
        self.assertFalse(r.opened)
        self.assertIn("authoritative", r.note)

    def test_opened_when_process_stays_alive(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("powerbi_import.desktop_probe.find_pbi_desktop",
                            return_value="pbi.exe"), \
                 mock.patch("powerbi_import.desktop_probe.subprocess.Popen",
                            return_value=_FakeProc(poll_seq=[None])), \
                 mock.patch("powerbi_import.desktop_probe._recent_matches",
                            return_value=[]), \
                 mock.patch("powerbi_import.desktop_probe._scan_trace_errors",
                            return_value=[]), \
                 mock.patch("powerbi_import.desktop_probe.time.sleep"):
                r = probe_desktop_open(_pbip(d), settle=0, timeout=5)
        self.assertEqual(r.status, "opened")
        self.assertTrue(r.opened)
        self.assertEqual(r.pid, 4242)

    def test_crashed_on_early_exit(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("powerbi_import.desktop_probe.find_pbi_desktop",
                            return_value="pbi.exe"), \
                 mock.patch("powerbi_import.desktop_probe.subprocess.Popen",
                            return_value=_FakeProc(poll_seq=[1])), \
                 mock.patch("powerbi_import.desktop_probe._recent_matches",
                            return_value=[]), \
                 mock.patch("powerbi_import.desktop_probe._scan_trace_errors",
                            return_value=[]), \
                 mock.patch("powerbi_import.desktop_probe.time.sleep"):
                r = probe_desktop_open(_pbip(d), settle=30, timeout=60)
        self.assertEqual(r.status, "crashed")
        self.assertFalse(r.opened)
        self.assertTrue(any("exited early" in s for s in r.signals))

    def test_crashed_on_frowndump(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("powerbi_import.desktop_probe.find_pbi_desktop",
                            return_value="pbi.exe"), \
                 mock.patch("powerbi_import.desktop_probe.subprocess.Popen",
                            return_value=_FakeProc(poll_seq=[None, None])), \
                 mock.patch("powerbi_import.desktop_probe._recent_matches",
                            return_value=[r"C:\FrownDump1"]), \
                 mock.patch("powerbi_import.desktop_probe._scan_trace_errors",
                            return_value=[]), \
                 mock.patch("powerbi_import.desktop_probe.time.sleep"):
                r = probe_desktop_open(_pbip(d), settle=30, timeout=60)
        self.assertEqual(r.status, "crashed")
        self.assertTrue(any("FrownDump" in s for s in r.signals))

    def test_launch_failure_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("powerbi_import.desktop_probe.find_pbi_desktop",
                            return_value="pbi.exe"), \
                 mock.patch("powerbi_import.desktop_probe.subprocess.Popen",
                            side_effect=OSError("boom")):
                r = probe_desktop_open(_pbip(d))
        self.assertEqual(r.status, "error")

    def test_to_dict_includes_opened(self):
        r = DesktopProbeReport(pbip_path="x", status="opened")
        d = r.to_dict()
        self.assertTrue(d["opened"])
        self.assertEqual(d["status"], "opened")


if __name__ == "__main__":
    unittest.main()
