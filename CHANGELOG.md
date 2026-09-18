# Changelog

## Unreleased

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
