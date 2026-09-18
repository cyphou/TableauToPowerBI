# Changelog

## Unreleased

- Tableau colour encodings are no longer dropped. A colour encoding is stored
  in two places — the worksheet names the field and the palette, the datasource
  holds the per-value colours — and only the first was read, so workbooks lost
  their colours. The halves are now joined and custom palettes resolve against
  their document-level definitions; across the example corpus 46 visuals gained
  colour that previously vanished. The readiness assessment counts those colour
  rules rather than a `conditionalFormatting` key no extractor produces.

- The consolidated quality report now carries the preceptorship review. When a
  `preceptor_report.json` sits beside the project, its coaching enters the
  remediation queue as a repair owned by the agent that owns the artifact,
  together with the fix text and the file the finding was raised against — the
  report previously said what was wrong but never how to correct it. The review
  remains opt-in; its absence is reported as `not_run`.
- Preceptor review now reads the shapes the generator actually writes.
  Annotations recording the original Tableau formula are no longer scored as
  leaked DAX; `definition.pbir`, report-level filters (`filterConfig`), date
  dimensions and dashboard-placed worksheets are each resolved through the
  producer's own vocabulary. The M dimension previously inspected fenced
  blocks, which hold calculated-table DAX rather than M, so it scored every
  workbook 5/5 without reading a single M partition; it now parses the bare
  `source =` partitions and catches real `if`/`else` and quoting defects.
- Internal hardening and confidentiality cleanup. Prior release history was intentionally removed.

## v44.0.0

Current released version. Detailed historical changelog entries have been intentionally cleared.
