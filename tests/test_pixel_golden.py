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
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.generate_pixel_fixtures import (  # noqa: E402
    _load_json,
    build_snapshot_for_workbook,
)
from tests.fixture_corpus import (  # noqa: E402
    describe,
    golden_path,
    golden_workbooks,
)

_STANDALONE_GOLDEN_IDS = frozenset({'wb_501a11ab', 'wb_7af02205'})


def _assert_standalone_inventory(test, twb_path, snapshot):
    """Pin these public samples' append shape independently of the golden."""
    root = ET.parse(twb_path).getroot()
    worksheets = root.findall('./worksheets/worksheet')
    dashboards = root.findall('./dashboards/dashboard')
    references = {
        zone.attrib['name'] for dashboard in dashboards
        for zone in dashboard.findall('.//zone')
        if zone.get('type') in {'worksheet-ref', 'worksheet'}
    }
    standalone = [
        worksheet.attrib['name'] for worksheet in worksheets
        if worksheet.attrib['name'] not in references
        and not any(worksheet.get(key, '').strip().lower() == 'true'
                    for key in ('hidden', 'is_hidden'))
    ]
    test.assertEqual(len(worksheets), 5)
    test.assertEqual(len(dashboards), 1)
    test.assertEqual(len(references), 4)
    test.assertEqual(len(standalone), 1)
    # Regenerating a snapshot must not hide lost standalone pages or turn
    # already-embedded sheets into additional report pages.
    test.assertEqual(
        {visual['page'] for visual in snapshot['visuals']},
        {dashboard.attrib['name'] for dashboard in dashboards} | set(standalone))
    added = [visual for visual in snapshot['visuals'] if visual['page'] == standalone[0]]
    test.assertEqual(len(added), 1)
    test.assertGreater(added[0]['fields'], 0)


def _make_case(fid, twb_path):
    def _test(self):
        twb = os.path.basename(twb_path)
        if not os.path.isfile(twb_path):
            self.skipTest(f"sample workbook not found: {twb}")
        golden = _load_json(golden_path(fid))
        self.assertIsNotNone(golden, f"missing golden fixture for {twb}")
        snapshot = build_snapshot_for_workbook(twb)
        if fid in _STANDALONE_GOLDEN_IDS:
            _assert_standalone_inventory(self, twb_path, snapshot)
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
