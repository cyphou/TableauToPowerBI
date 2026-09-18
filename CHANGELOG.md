# Changelog

## Unreleased

- Parity evidence now distinguishes "we looked and found nothing" from "nothing
  looked". Both were reported as `source_only`, and only 8 of the 32 tracked
  features had a probe at all, so the headline read 36% coverage when three
  quarters of it had never been checked. Evidence is now `evidenced`,
  `not_found` or `not_checked`, unprobed features are kept out of the
  denominator, and coverage reads 77.2% over what is actually verified with the
  unchecked count stated beside it.

- A Power BI parameter is no longer evidenced by any table containing the word
  "measure". Parameters are confirmed by the table shapes the generator really
  emits for them (`GENERATESERIES`, `DATATABLE`, `NAMEOF`), and hierarchies,
  sort-by-column and native SQL queries gained probes of their own. Probes were
  added only where they identify their feature specifically: a measure proves
  some calculation converted but not which kind, so no calculation feature
  claims one.

- Pre-migration assessment warnings now reach the remediation queue as separate,
  addressable entries. A single "assessment contains warnings" line stood in for
  19 warnings spanning 6 categories and 13 distinct checks across the example
  corpus, all attributed to one owner with one action, and it discarded the
  detail and recommendation each check already carried. Warnings are now routed
  by assessment category: connector, volume and licensing warnings go to the
  deploying tenant as decisions, conversion warnings ask the owning agent to
  confirm its approximation, and the rest is recorded as context. Blocking
  failures are named rather than counted. Verdicts are unchanged.

- Semantic lineage no longer reports generated and calculated objects as
  orphans. The resolver matched every target against source tables and columns
  only, so What-If tables named after their parameter, calculated columns whose
  source is a calculation, and columns a join merged in from another table
  could never resolve — 120 records across 12 of the 26 example workbooks. Each
  now resolves against the category it comes from, taking the count to zero and
  moving four workbooks from WARN to PASS.

- The Power BI Desktop probe now states what it actually verifies. All 26
  migrated example projects launch Desktop and survive the settle window — but
  so does a project whose semantic model has been deleted, because Desktop
  reports content errors in a dialog and keeps running. The probe reports
  `verified: process_survival` and carries that limitation in its payload, so
  an `opened` verdict cannot be mistaken for proof that a project loaded.

- Parameter usage is no longer counted as data blending. Tableau exposes its
  parameter container as a pseudo-datasource, so referencing a parameter looked
  like a cross-datasource blend: all 15 blend records in the example corpus
  named it, and none was a genuine blend. The parity detector now reuses the
  distinction `blend_graph` already made, lifting the lowest corpus parity
  score from 90.9% to 96.4% and the mean to 99.7%.

- Tableau field aliases that rename an aggregated field now survive migration.
  Tableau aggregates implicitly, so an author renaming `sum:F: GDP (curr $)` to
  "GDP (US $'s)" had no named object to carry the caption and it was dropped —
  none of the 17 such aliases in the example corpus reached the model. A named
  measure is now generated for each, added rather than renamed so existing
  references stay valid. Parity for `feedback_dashboard` rises from 91.7% to
  100%.

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
