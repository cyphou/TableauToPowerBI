# Changelog

## Unreleased

- Clarify that `openable=true` reports a static verdict, not successful Desktop
  loading or refresh. Native failures take precedence over a static pass.
  The relationship-triggered refresh failure reported in #84 remains
  unresolved without its exact reproduction; no relationship removal or
  typed-empty workaround is claimed as a causal fix.
- Type legacy Hyper metadata-only empty table literals using their declared
  column metadata, without adding rows or replacing unavailable-source errors.
  This independent schema-quality repair does not resolve bug report #84.
- Exclude disabled shadow extracts from active source routing. Bind a
  materialized extract over a native join/union only when a single logical
  object, exact extract identity and complete typed field mapping prove the
  binding. Preserve native relation/filter provenance and reject ambiguous
  bindings without partially redirecting fields.
- Separate Desktop row-count observations from refresh verification. Visible
  owned refresh errors, busy/pending state or unavailable UI inspection prevent
  refresh and screenshot verification, including errors discovered after render
  settling. Existing or partially loaded rows alone are not refresh evidence.
- Retarget combined-group field references through their owning table's exact
  rename ledger. Cross-table combined groups require an unambiguous active
  source-defined to-one path; inferred or many-to-many edges do not justify
  scalar lookup substitutions.
- Stage exact declared files from inside packaged TWBX archives before semantic
  source checks, rather than extracting them only after an unavailable-source
  partition has already been emitted. Reject ambiguous archive bindings and
  conflicting destination content; do not substitute an unrelated same-name file.
- Convert proven date/datetime plus numeric-literal day offsets to Power Query
  `Date.AddDays`, and normalize boolean literals without rewriting quoted data
  or field names. Uncertain date arithmetic stays in DAX instead of emitting M
  that fails for every row.
- Canonicalize calculation captions that repeat a bracketed Tableau identifier
  consistently across semantic-context maps and generated column/measure names.
  Preserve different explicit captions and genuine inner brackets. Literal
  `Number of Records` references can then be inlined in row calculations rather
  than producing invalid double-bracket DAX. Keep diagnostic-comment DAX in
  DAX instead of parsing its comment text as Power Query arithmetic. These
  generation fixes do not certify source availability or rendered visual parity.
- Extract worksheet and dashboard definitions from their direct workbook
  collections rather than nested metadata references. Preserve direct-root
  compatibility when no collection exists, and retain duplicate names that are
  actually present in the source. Support validated parameter-sized Tableau
  bins without replacing the parameter reference with an invented size.
  Extraction checks do not certify generation, refresh or visual parity.
- Export Power Query from final reconciled model partitions rather than stale
  pre-repair queries. Preserve private Calendar source scopes and persist their
  source-table provenance for synchronization after local CSV repairs.
  Materialize supported categorical dimensions at their verified local `FIXED`
  grain without unresolved measure dependencies. Preserve declared visual
  colors, percentage formats and continuous-date controls where supported.
  These targeted fixes do not certify workbook-wide rendered or semantic parity.
- Preserve Tableau logical-relationship aliases and complete comparison
  predicates, including composite/range conditions. These predicates are not
  emitted as ordinary Power BI model relationships. Compile a constrained
  subset of relationship-grain SUM calculations without RowID lookups or
  flattening unrelated measures. These cases require explicit datasource-scoped
  filter, unmatched-row and string-comparison policies; unsupported cases stop
  rather than inventing an equality relationship. See the logical-relationship
  policy requirements in `docs/KNOWN_LIMITATIONS.md`.
- Convert divisions in supported scalar DAX expressions, including ratios
  inside `FIXED` calculations, to `DIVIDE` without an alternate zero.
  Undefined rates remain blank rather than producing NaN or infinity;
  quoted slashes and unsupported expression syntax are left unchanged.
  Materialize supported `DIVIDE` expressions in Power Query with explicit
  null/zero-denominator handling. Conditional Tableau `COUNT` returns numeric
  zero for empty/all-null inputs without replacing undefined rates with zero.
  For eligible `null_extend` relationship-grain SUM calculations, numeric
  `IF`/`IIF` trees prove which left rows contribute zero and prune them before
  pair construction, preserving empty-versus-blank and duplicate-row behavior.
  Empty strings are not treated as nulls.
- Normalize nested Tableau Prep `loomContainer` graphs before conversion and
  analysis, rejecting ambiguous boundaries, cycles and dangling references.
  Share output classification and optional-null handling between parser and
  analyzer. Requested M conversion failures record `conversion_error` with a
  RED/error assessment; batch exports reject missing outputs or M queries
  instead of reporting success. Explicit `--prep` failures stop the affected
  workbook rather than silently continuing without its transformations.
- Keep calculated expressions in DAX when their M row dependencies are not
  available, instead of generating missing-field row errors. Convert M
  calculated-column values explicitly to their declared datetime type.
  Preserve year-only `DATEPARSE("yyyy", ...)` as an explicit January 1 date,
  and translate one-argument `DATE`/`DATETIME` casts using explicit `CONVERT`
  expressions. Locale-dependent `DATEVALUE` expressions stay in DAX rather
  than using different M parsing.
- Stage explicitly declared local files before semantic availability validation,
  resolving workbook-relative paths and bounded stale-root sibling paths without
  recursive basename substitution. Missing sources are counted as not found and
  left unstaged; conflicting resolved sources or ambiguous relocations raise
  errors. This does not certify Desktop refresh.
- Correct Tableau standalone `NULL` expressions to DAX `BLANK()` without
  rewriting quoted strings or field names. Convert nested `NULLIF(a, b)` calls
  to strict-equality DAX `IF` expressions, preserving blank versus zero/empty
  text distinctions.
- Correct PBIR aggregation identifiers (including distinct count), use
  datasource-scoped metadata for field resolution, and prefer physical-column/
  model type evidence over same-name foreign measures. Ambiguous provenance
  remains unbound rather than borrowed from another datasource. Discrete
  date/year fields avoid implicit sums; numeric aggregations on known
  non-numeric columns are rejected, with warnings in the projection path.
  These generator fixes do not certify Desktop rendering or source availability.
- Local Excel/CSV partitions now expose missing files or worksheets instead of
  replacing refresh failures with empty tables. Excel relation names normalize
  SQL-style quoting, and relation-specific CSV paths take precedence over shared
  connection filenames.
- Desktop diagnostics can explicitly launch a PBIP's declared report definition
  with `probe_desktop_open(..., launch_report_definition=True)`. The default PBIP
  launch is unchanged; report-definition launch is labelled separately and does
  not certify that opening the PBIP itself succeeds.

- Added an explicit folder-based Tableau / Power BI Desktop screenshot diagnostic
  (`scripts/compare_desktop_reports.py`). Fresh per-run source/output captures and
  a JSON/HTML side-by-side report expose missing pairs, unavailable applications,
  and capture failures. Only comparison-launched processes close after capture.
  Captures default to manual-review evidence, not parity scores or data-load
  verification. Optional `--screen-comparison local` adds deterministic,
  stdlib-only PNG pixel diagnostics without AI endpoints or network access in the
  comparison step; full-window metrics are not semantic parity or SSIM scores
  and do not change capture exit status. Tableau is not refreshed; the Power BI
  probe requests refresh/data verification, which may use network data sources.
  Migration does not
  launch this diagnostic automatically.
  Opt-in `--all-pages` inventories individual Tableau views and Power BI pages,
  verifies UI selection before capture, and reports inaccessible views and
  missing or ambiguous page correspondences explicitly. Initial-window capture
  remains the default. The all-pages HTML also displays accepted existing
  initial opening screenshots, expanded before page evidence, as
  page-identity-unverified evidence, excluded
  from verified page totals and per-page pixel comparisons.
  Business-review presentation now includes shorter messages, icon-enhanced
  cards, light neutral styling, and report/page navigation. Missing captures stay
  explicit; technical evidence remains collapsed by default but available, and
  opening captures stay separate and labelled page identity unverified.
  Screenshot links use byte-identical, content-addressed presentation copies
  beside the HTML to avoid browser failures on long Windows source paths;
  original captures and manifest evidence remain unchanged. This is
  presentation only: no new captures or parity claims. Generated-HTML tests and
  browser DOM checks cover language switching and navigation state, not complete
  cross-browser pointer or rendering validation.

- Screenshot comparison HTML (main and per-page) and unified migration-quality
  HTML (individual and consolidated) now offer a French/English selector, with
  a locally remembered preference when browser storage permits. Authored
  translations are embedded: no translation endpoint or external script is
  required. Titles, accessibility labels and dynamic navigation text switch;
  names, paths, raw diagnostics, JSON and screenshots remain unchanged. Shared
  report controls are bilingual. This translates authored interface text, not
  every report value or diagnostic; embedded source and technical content stays
  in its original language.

- Extend authored French/English interface text to eleven shared-template report
  types: artifact differences, cutover, dependencies, permissions, QA, recovery,
  subscriptions, global assessment, merge assessment, Prep lineage and server
  assessment. They default to English without a saved browser choice and reuse
  the shared language preference where browser storage permits. Names, paths,
  raw diagnostics, evidence, JSON and canonical status values remain unchanged.
  Standalone parity-registry, remediation, migration-planner and merge-report
  HTML, plus notebook/PDF dashboards, remain outside this extension. Localization
  needs no translation endpoint; existing network dependencies remain, including
  the Prep-lineage Mermaid CDN. No new migrations or Desktop captures are implied.

- Scatter generation now preserves Tableau Detail grouping separately from
  color/Series and keeps numeric measure and column bindings on their X/Y
  roles. Tooltip-only fields do not fill a missing axis or become Size for
  single-axis Circle/Shape plots; those plots become annotated tables that
  preserve their source fields rather than receiving a fabricated second
  scatter axis. The live Power BI Desktop outcome for
  `ScatterXIncorrectAggregate` remains `not_run` generally. The separate
  World Indicators typed-CSV root-fix verification below did show that
  workbook's Business scatter populated without an aggregate error.

- Hyper-to-CSV partitions now type physical CSV columns at the M `Source` step,
  immediately after headers and before derived calculations. Types use
  extraction metadata, with model `sourceColumn`/`dataType` fallback, and
  explicit `en-US` culture for invariant Hyper CSV values. In a live Power BI
  check of World Indicators, schema and data refresh verified numeric column
  types and successful `AVERAGE` queries; its Business scatter was populated
  without an aggregate error. This is workbook-specific evidence, not a
  general Desktop or all-workbook claim.

- Bind supported enabled single-object object-graph native/extract pairs to
  the exact packaged Hyper member, schema, and table. Preserve the logical
  table and physical-column mapping, and remove only a proven duplicate
  Extract table entry. Hyper API readers and CSV exports now read schema
  names through `SchemaName.name.unescaped`, matching the supported API.
  Exact-identity CSV staging preserves downstream calculated columns, groups,
  and bins instead of overwriting their M transformation steps.
  Tableau `class="bin"` calculations now retain their declared interval size
  and origin instead of becoming integer-typed copies of fractional source
  values; nested arithmetic preserves that interval formula in Power Query M.

- Tableau-to-Power BI generation is now batch-only across CLI, wizard, REST API,
  MCP, and web interfaces. To migrate one workbook, place it in a folder by
  itself. The legacy `POST /migrate` endpoint returns HTTP 410; submit a ZIP to
  `POST /migrate/batch` instead. File-based assessment and diagnostic commands
  remain supported.
- Fixed page routing for set-value and parameter actions to use source dashboards
  and worksheets consistently with URL/navigation actions. Removed unreachable
  DAX optimizer code containing undefined references flagged by F821.

- Added MCP owner-pull handoff and `agent_handoff_ack`. Owners can retrieve only
  their findings; `applied` ACKs require changed PBIP definition artifacts and
  a fresh quality report where the finding is gone. ACKs are in-memory only and
  do not dispatch agents or start review cycles. Durable status and automatic
  orchestration remain open in the roadmap.

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
  remediation queue as a repair finding tagged for the owning agent, together
  with the fix text and the file the finding was raised against — the report
  previously said what was wrong but never how to correct it. This is a report
  handoff, not automatic agent dispatch or repair. The CLI review is on by
  default and can be skipped with `--no-preceptor`; review evidence remains
  optional, so a missing report is recorded as `not_run`.
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
