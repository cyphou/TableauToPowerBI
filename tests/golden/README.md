# Pixel-Perfect Golden Fixtures (Sprint 205)

This directory holds **deterministic visual snapshots** used as regression
baselines for the Tableau → Power BI generator. Each subfolder corresponds to a
committed sample workbook in `examples/tableau_samples/` and contains a single
`visuals.json` capturing the pixel-relevant attributes of every generated
visual.

## Snapshot schema

`visuals.json`:

```jsonc
{
  "visual_count": 4,
  "visuals": [
    {
      "page": "Sales Dashboard",   // page displayName (stable; folders are UUIDs)
      "type": "clusteredBarChart", // PBIR visualType
      "x": 640, "y": 0,            // position (Tableau pixel coords preserved)
      "width": 640, "height": 360,
      "z": 1000,                    // z-order
      "fields": 3,                  // encoded field count across query roles
      "has_format": true,           // visual.objects present
      "title_font": "",            // title fontFamily (if any)
      "title_size": ""             // title fontSize (if any)
    }
  ]
}
```

Records are **order-normalised** (sorted by page → type → x → y → size → z →
fields → title) so snapshots never depend on visual UUID filenames or
filesystem ordering.

## Regenerating fixtures

Only regenerate after an **intentional, reviewed** change to layout or
formatting output:

```bash
python scripts/generate_pixel_fixtures.py          # rewrite all fixtures
python scripts/generate_pixel_fixtures.py --check   # diff only, exit 1 on drift
```

## Tests

`tests/test_pixel_golden.py` re-migrates each workbook into a temp directory,
rebuilds the snapshot, and asserts it matches the committed golden. A failure
means the generator output drifted.

## Excluded workbooks

_None._ As of **Sprint 204 (floating zone overlay fidelity)**, the previously
excluded **Enterprise_Sales** workbook is part of the golden set. It contains
two heavily overlapping zones (a textbox backdrop behind a `tableEx`); the
report-side overlap healer now staggers the duplicate deterministically by
z-order (lowest z stays anchored, higher z moves +32 px) instead of relying on
random UUID directory ordering, so its snapshot is stable across runs.
