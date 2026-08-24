"""Per-workbook pixel-perfect golden regression tests (Sprint 205).

Each committed fixture under ``tests/golden/<workbook>/visuals.json`` is a
deterministic snapshot of the pixel-relevant attributes (position, size, type,
encoded field count, format presence, title font) of every visual produced by
migrating the matching sample workbook in ``examples/tableau_samples/``.

The tests re-migrate the workbook into a temp directory, build a fresh
snapshot, and assert it matches the committed golden byte-for-byte (after JSON
normalisation).  A mismatch means the generator's visual layout/formatting
output drifted — regenerate fixtures with::

    python scripts/generate_pixel_fixtures.py

only after confirming the change is intentional.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.generate_pixel_fixtures import (  # noqa: E402
    GOLDEN_WORKBOOKS,
    SAMPLES_DIR,
    build_snapshot_for_workbook,
    _golden_path,
    _load_json,
)


def _make_case(name, twb):
    def _test(self):
        twb_path = os.path.join(SAMPLES_DIR, twb)
        if not os.path.isfile(twb_path):
            self.skipTest(f"sample workbook not found: {twb}")
        golden = _load_json(_golden_path(name))
        self.assertIsNotNone(golden, f"missing golden fixture for {name}")
        snapshot = build_snapshot_for_workbook(twb)
        self.assertEqual(
            snapshot.get("visual_count"), golden.get("visual_count"),
            f"{name}: visual count drifted",
        )
        self.assertEqual(
            snapshot.get("visuals"), golden.get("visuals"),
            f"{name}: pixel attributes drifted — regenerate fixtures only if "
            f"the change is intentional",
        )
    _test.__name__ = f"test_golden_{name}"
    return _test


class TestPixelGolden(unittest.TestCase):
    """Dynamically-bound per-workbook golden assertions."""


for _name, _twb in GOLDEN_WORKBOOKS.items():
    setattr(TestPixelGolden, f"test_golden_{_name}", _make_case(_name, _twb))


class TestGoldenFixturesPresent(unittest.TestCase):
    """Every curated workbook must ship a committed golden fixture."""

    def test_all_fixtures_committed(self):
        for name in GOLDEN_WORKBOOKS:
            path = _golden_path(name)
            self.assertTrue(
                os.path.isfile(path),
                f"golden fixture missing: {path} (run generate_pixel_fixtures.py)",
            )

    def test_fixtures_have_visuals(self):
        for name in GOLDEN_WORKBOOKS:
            golden = _load_json(_golden_path(name))
            self.assertIsInstance(golden, dict)
            self.assertGreater(
                golden.get("visual_count", 0), 0,
                f"{name}: golden fixture has no visuals",
            )


if __name__ == "__main__":
    unittest.main()
