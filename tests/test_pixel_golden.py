"""Per-workbook pixel-perfect golden regression tests (Sprint 205).

Each committed fixture under ``tests/golden/<fixture_id>/visuals.json`` is a
deterministic snapshot of the pixel-relevant attributes (position, size, type,
encoded field count, format presence, title font) of every visual produced by
migrating the matching sample workbook in ``examples/tableau_samples/``.

Fixtures are addressed by the derived id from ``tests/fixture_corpus`` because
workbook names are only legal under ``examples/``; failure messages resolve the
id back to a filename so a drift report still points at a real file.

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

from tests.fixture_corpus import (  # noqa: E402
    describe,
    golden_path,
    golden_workbooks,
)
from scripts.generate_pixel_fixtures import (  # noqa: E402
    build_snapshot_for_workbook,
    _load_json,
)


def _make_case(fid, twb_path):
    def _test(self):
        twb = os.path.basename(twb_path)
        if not os.path.isfile(twb_path):
            self.skipTest(f"sample workbook not found: {twb}")
        golden = _load_json(golden_path(fid))
        self.assertIsNotNone(golden, f"missing golden fixture for {twb}")
        snapshot = build_snapshot_for_workbook(twb)
        self.assertEqual(
            snapshot.get("visual_count"), golden.get("visual_count"),
            f"{twb} ({fid}): visual count drifted",
        )
        self.assertEqual(
            snapshot.get("visuals"), golden.get("visuals"),
            f"{twb} ({fid}): pixel attributes drifted — regenerate fixtures "
            f"only if the change is intentional",
        )
    _test.__name__ = f"test_golden_{fid}"
    return _test


class TestPixelGolden(unittest.TestCase):
    """Dynamically-bound per-workbook golden assertions."""


for _fid, _path in golden_workbooks():
    setattr(TestPixelGolden, f"test_golden_{_fid}", _make_case(_fid, _path))


class TestGoldenFixturesPresent(unittest.TestCase):
    """Every curated workbook must ship a committed golden fixture."""

    def test_all_fixtures_committed(self):
        for fid, _path in golden_workbooks():
            path = golden_path(fid)
            self.assertTrue(
                os.path.isfile(path),
                f"golden fixture missing for {describe(fid)}: {path} "
                f"(run generate_pixel_fixtures.py)",
            )

    def test_fixtures_have_visuals(self):
        for fid, _path in golden_workbooks():
            golden = _load_json(golden_path(fid))
            self.assertIsInstance(golden, dict)
            self.assertGreater(
                golden.get("visual_count", 0), 0,
                f"{describe(fid)}: golden fixture has no visuals",
            )


if __name__ == "__main__":
    unittest.main()
