# Changelog

## Unreleased

- Direct Tableau URL actions now preserve their row-level target when the action
  points exactly to a field (`<[datasource].[field]>`). The generated semantic
  model marks that column as `dataCategory: WebUrl`, including calculated Tableau
  fields resolved through their generated captions. Static URLs continue to use
  action buttons. Composed or otherwise unsafe interpolated URLs remain omitted
  rather than becoming incorrect static links; their report-level presentation is
  still an open limitation.

- Tableau per-value aliases now migrate. A field whose values were renamed in
  Tableau ("North" shown as "North (N)") gets a generated
  `'<field> (Display)'` calculated column that maps each value to its label with
  a `SWITCH`, while the original column stays for filtering and joins. Measured
  first: of the three corpus workbooks that appeared to use value aliases, two
  were parameter value labels already carried by the parameter table's Name
  column, so the `alias_value` parity detector — which had been counting those
  parameter cases, the same over-count as the old data-blending floor — now
  excludes parameter names. The feature's parity status moves from
  `approximated` to `healed`, backed by an evidence probe on the generated
  `displayFolder: Aliases` column; SampleWB scores 100% parity with the alias
  evidenced.

- An action button is placed on the page its action came from. The call site
  filtered on `source_worksheet` — singular, a key the extractor never emits —
  so every action passed on every page and one action became one button per
  page: three Salesforce actions produced twenty-four buttons. Tableau names
  the endpoint as `dashboard` on `<source>` and `<target>`, and only sometimes
  as `worksheet`; both are now captured. All six `<target>` elements in the
  example corpus name a dashboard and no worksheet, which is why
  `target_worksheets` was empty everywhere and no drill-through page was ever
  built from an action.

- Tableau actions are read again. A real `<action>` carries no `type`
  attribute: the kind is a child — `<command command="tsc:brush">` for
  highlighting, `tsc:tsl-filter` for filtering, `<link>` for a URL. The reader
  looked only at `@type`, which is the dialect the hand-written sample files
  use, so **29 of the 45 actions in the example corpus — every action in every
  genuine workbook — were dropped**. All 45 are now typed.

- A URL action's target is read from where Tableau puts it: the `<url>`
  element's text, or `link/@expression`. Reading the `@url` attribute returned
  nothing, so URL buttons shipped pointing at an empty string. A button is now
  created only when the target is a static link; Tableau usually interpolates a
  field (`https://crm/customer/<customer_id>`, `<[ds].[Link]>`), which is a
  per-row value a button cannot hold. Those report as unmigrated rather than as
  a button that goes nowhere — there were 24 such buttons on one workbook.

- The generation path's dependency on `visual_generator` is now pinned. The
  module reads as the visual builder, but production imports ten names from it —
  three lookup maps and seven helpers — and builds its visuals in
  `pbip_generator`. Its other 48 top-level functions are referenced by no
  production code, and 38 of those are named by a test, so the module read as
  covered. A test now records the real surface, so wiring a builder in is a
  decision rather than an accident.

- `pbip_generator.py` no longer carries a UTF-8 BOM. It was the only file in the
  repository with one, and it makes `ast.parse` reject an otherwise valid file —
  quietly excluding the largest generator from any static scan. A test now
  refuses a BOM anywhere in the source tree.

- Tableau packed-bubble sheets become treemaps instead of tables. With Rows and
  Columns empty, a measure on Size with a dimension on Colour is one shape per
  category sized by the measure — which is what a Power BI treemap draws. A
  scatter chart was the nominal mapping but needs an X and a Y these sheets do
  not have, so it would have degraded straight back to a table. Four more
  example sheets now carry a real visual.

- Filters now carry the values they filter to. The workbook-level reader looked
  for `<value>` children, a shape Tableau does not write — members are the
  `member` attribute of a `<groupfilter>` — so all 167 filters in the example
  corpus reported no values, and the generator correctly refused to emit a
  categorical filter with an empty condition. Three workbooks therefore shipped
  reports with no filter at any level. The worksheet-level reader already read
  the real shape, so both now share one function and cannot disagree about the
  same XML. 67 filters recovered their values; 19 report-level filters are now
  generated where there were none.

- A Tableau top-N filter is no longer migrated as a bound on the measure.
  `class="topn" direction="top" max="10"` means "the ten largest", but its
  cut-off was read as a range and emitted as `amount <= 10`. It is now
  classified as top-N; expressing it as a Power BI TopN filter remains
  outstanding, so the filter is absent rather than wrong.

- Tableau KPI cards no longer arrive as data tables. A sheet that leaves Rows
  and Columns empty and puts its measure on the Text shelf is Tableau's
  "big number" card, but both the `Text` and `Automatic` marks fell through to
  a grid — 28 of the 181 example worksheets became a table, 16 of them named
  "KPI Card", "Ranking" or "Card". Such a sheet now becomes a Power BI `card`,
  or a `multiRowCard` when several measures share the Text shelf. Sheets with a
  real field on Rows or Columns are untouched, and a Size encoding still marks a
  packed-bubble plot rather than a card. Corpus: 28 table-like worksheets → 12.

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
