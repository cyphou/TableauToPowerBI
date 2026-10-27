# Changelog

[🏠 Overview](README.md) · [📚 Documentation](docs/README.md) · [📈 Roadmap](docs/ROADMAP.md) · [🤝 Contributing](CONTRIBUTING.md)

## Unreleased

- Name the retained source in the quality report when an enabled extract hides
  another connection: the Hyper Import section gains a bilingual **Source
  decision** table (datasource, retained packaged Hyper snapshot, unused
  original connector type) and a `source_decisions` evidence field. Only the
  datasource label and connector type are reported, never server, database,
  user or other connection details, and the table changes neither the gate nor
  the PASS/WARN status. When the Hyper gate has already failed and the workbook
  disables the extract of a datasource while a `.hyper` is still packaged, add
  one `Hint:` diagnostic that names up to three datasources and the two ways out
  (re-enable the extract, or remove the extract and its `.hyper`); it is never
  added to a passing result and no live source is substituted. Document the
  behavior under "Active extract behind a live connection". Verified with
  synthetic fixtures only; no live connection, Desktop load, refresh or value
  parity was exercised.

- Require complete Hyper snapshot exports for PBIP and exact CSV/Parquet file
  bindings to Import partitions. Reject row caps (including environment caps),
  sample-only readers, unknown/inconsistent counts and unresolved required
  bindings instead of accepting a partial model. Keep the uncompressed 10 MiB
  format threshold, native-source no-row-export behavior and separate Fabric
  DirectLake contract. Preserve explicit main/named Hyper sources and
  multi-table extract aliases through physical source identities; record those
  identities in TMDL for final read-only binding checks.
  Rebuild column/type/routing maps from active physical declarations rather
  than inactive native metadata. Keep logical groups and table/object
  pseudo-fields out of physical source columns; do not duplicate categorical
  memberships as numeric bins. Follow declared M renames, derived columns,
  literal lookup joins and recognized scalar date/time constructors when
  proving final schema coverage, while rejecting unknown dependencies.
  Route native Parquet writing through private authorized ASCII staging and
  publish via Python at the actual destination, including accented paths.
  Keep failed exports and cleanup errors visible. These export/static checks
  do not certify native refresh, saved model data or rendered parity.

- Security: stop the Hyper import check from resolving project-controlled
  paths. The identity annotation, export manifest, `DataFolder` and
  `File.Contents` values (UNC and device-namespace paths such as `\\host\share`,
  `\\?\UNC\...`, `\\.\...` and `\??\...`, and local-looking paths that run
  through a symlink or junction) are normalized and compared as text with the
  keys of the operator-selected `Data` folder, and never reach `realpath`, so a
  tampered project cannot trigger an outbound SMB connection during
  verification. A `Data` directory, `expressions.tmdl`, export file or
  sampling manifest that is a link is reported without being opened, and the
  sampling scan no longer descends into junctions. An alias of the `Data`
  folder through a link or an 8.3 short name is no longer accepted. Document
  that generated TMDL, the manifest and the quality report carry the source
  workbook's absolute path, that a drive letter mapped to a share is treated
  as local, and that the generic project scanners do not test for links.

- Refresh the two French documentation PowerPoints: 15-slide overview/business
  case and 23-slide architecture/workflow guide, with editable diagrams,
  speaker-note references, the Hyper/source policy at generation (including the
  unchanged packaged-Excel copy into `Data/`), shared models,
  Prep/Fabric routes, optional Microsoft authoring tooling and separate quality
  gates. Consolidate their source into one generator with the compatible
  technical-guide entrypoint. Replace stale counts and unsubstantiated savings
  claims with dated evidence and an explicitly illustrative, adjustable
  effort/TCO model; document optional `python-pptx` authoring and rebuild commands.

- Write PBIR story/swap bookmarks as indexed `<name>.bookmark.json` files.
  Keep legacy bookmark discovery in static openability, parity and interaction
  evidence, and diagnose missing index targets instead of silently missing them.
  Normalize Gantt approximations before role construction so a source without
  a category preserves its measures in an explicitly annotated table rather
  than an invalid clustered bar. Omit a funnel's unsupported default value-axis
  toggle without silently dropping explicit source settings. Serialize bound
  data bars as `columnFormatting.dataBars` with metadata-only selectors and
  expression-wrapped colors/booleans, using the same formatter in both visual
  producers. Only when bound bars are emitted, upgrade legacy `matrix`/`table`
  to `pivotTable`/`tableEx` and record the compatibility adjustment; visuals
  without emitted bars keep their type. Document optional
  Microsoft authoring validation, separate from core migration dependencies
  and model/Desktop/refresh/deployment evidence.

- Resolve native Import relative file bindings against the original loose
  workbook/datasource directory, without reading or copying native rows.
  Preserve absolute paths and packaged-source configuration boundaries except
  for the separately documented unchanged packaged-Excel copy.
  Do not truncate native CSV physical width to the selected model-column count.
  Select the declared raw source columns after header parsing and before type
  or caption transformations, so unmodeled physical fields cannot collide with
  a caption rename; missing declared fields still raise an error.
  Merge explicitly declared physical columns into a sole partially populated
  source table, preserving their types instead of fabricating calculated
  placeholders; exclude Tableau pseudo-fields and leave ambiguous multi-table
  ownership unresolved.
  Explain unsupported native shapefiles with an explicit source-binding error
  instead of attempting JSON parsing. Keep Hyper/Fabric staging unchanged and
  document initial refresh notices, processing checks and Desktop cache saving.

- Recognize narrowly declared inline Tableau `Parameters` dependencies during
  source preflight without exempting missing real datasources. Export native
  Hyper geography columns to Parquet as WKT text while preserving names, order
  and nulls; keep other unsupported-type failures explicit and CSV behavior
  unchanged. Neither change establishes source-value or visual parity.

- In trusted local per-workbook batch generation, copy an explicitly declared
  Excel workbook packaged in a TWBX unchanged into the generated PBIP project's
  `Data/` folder and bind its absolute path before
  generating Power Query M. Do not select a similarly named loose file or
  convert/export Excel rows. Keep missing, ambiguous, unsafe or conflicting
  sources explicit. Uploaded jobs and shared-model/add-to-model workflows are
  outside this exception. Moving the project requires updating the absolute binding;
  the copy contains source data and must not be committed.

- Keep non-Hyper PBIP Import datasource-only: generate the connection, schema
  and Power Query M without exporting native rows. Loose native files and
  packaged non-Excel dataset files are not copied. Apart from the unchanged
  packaged-Excel copy described above, retain original file bindings for a later
  Power BI refresh. Hyper still exports its data to CSV/Parquet
  using the existing 10 MiB rule; Fabric keeps its separate staging behavior.

- Simplify PBIP source selection: Hyper snapshots use Import; native sources
  without Hyper use DirectQuery when supported. Import-only files stay Import
  with a diagnostic, and the user can always force Import through `--mode import`
  or the local browser. PBIP automatic native Hyper export requires Parquet
  strictly above 10 MiB and CSV otherwise. Above-threshold SQLite-compatible
  inputs error unless CSV is explicitly selected; Fabric auto staging stays CSV.
  Add a documented decision diagram, bounded
  Hyper/SQLite/ZIP I/O, atomic CSV rewrites, configurable ZIP and row limits,
  export provenance and throttled progress in terminal/browser logs. Required
  Parquet uses the existing optional Hyper API; unavailable prerequisites and
  failed exports are explicit errors, not partial-success fallbacks. Capped
  and header-reader diagnostics remain limited data; the later full-Hyper
  contract above rejects them for PBIP generation. The central quality report
  records those limits before determining status and handoff.
  These changes do not establish native refresh parity.

- Refresh the maintained documentation for the local-browser product and
  folder-based CLI. Reconcile PBIR schema/theme constants, the 23-object
  extraction contract, 164-record visual inventory, connector aliases,
  package metadata versus roadmap tracks, and specialist ownership. Distinguish
  local paths from upload services, static checks from native runtime evidence,
  and parsed legacy flags from actual batch dispatch. Document the current
  VS Code single-file adapter limitation rather than claiming it generates
  projects. Preserve dated test/browser observations and historical release notes.
  Add a GitHub-oriented documentation hub, linked local SVG badges and refreshed
  illustrations without fixed success or release claims. Separate the
  863-test native refresh-binding slice from the earlier full-suite baseline,
  qualify Desktop opening instructions and show only active CI stages.
  Preserve images and bounded presentation HTML in the standard-library
  documentation publisher, rewrite guide/source links and keep code examples
  literal. Publish only allowlisted raster images and strictly validated passive
  SVGs; reject symlinks, Windows junctions, unsupported assets and active or
  malformed SVG content with file-specific errors. Preserve the maintained
  illustrations, accessibility metadata and local gradient/filter references.
  Add focused publisher regressions.

- Add the bounded source-checkout local browser UI and runner/launcher slice:
  `python web/local_server.py` or `run_web_ui.ps1`, standard-library serving
  on `127.0.0.1:8765` with default browser opening, opt-out and Ctrl+C shutdown.
  The backend accepts local source/output folder paths, bulk migration
  (including one-workbook folders), PBIP/local Fabric, culture/calendar,
  quality policy and resume; standalone Prep keeps its M/source-export route.
  Expose generated artifact reports and a filtered output ZIP. Native Power BI
  probing and Tableau/Power BI comparison are separately confirmed actions
  that may launch apps, refresh/access sources and save private screenshots;
  neither runs automatically. Reported browser acceptance verified public
  Superstore migration, artifact/log display and a download link, French UI
  polling, a 390px layout and both authorization dialogs with cancel submitting
  no job. Native runtime remains `not_run`. No folder/ZIP upload or Server
  workflow is claimed. The separate Streamlit workflow remains unchanged; this
  is not N5 operational acceptance.
- Removed the standalone Tkinter desktop UI and its Windows executable build;
  the local browser UI and PowerShell launcher remain supported.

- Fix stale Prep schemas through supported clean operations, aggregates, joins,
  automatic unions and unpivot. Validate declared output fields, reject missing
  columns/type conflicts and require deterministic split/pivot schemas rather
  than inventing columns. Plan workbook `--prep` binding without mutating inputs,
  report complete published-source coverage, and reject unmatched/partial or
  ambiguous bindings before atomically replacing datasource JSON. Unused
  auxiliary outputs no longer masquerade as reconnected workbook sources.
  Permit only the reserved `F_<n>_<subject>` → `S_F<n>_<subject>` convention with
  unique, exact published identity evidence, preserved from datasource metadata.
  Keep bound M overrides effective through TMDL partition collection.
  Convert observed `.v2018_3_3.Remap` explicit JSON-encoded TEXT maps in
  standalone nodes and grouped clean actions. Use simultaneous, exact
  case-sensitive lookup, preserving unmatched values and nulls; reject missing
  or non-string upstream fields and invalid/ambiguous maps. `groupMethodProps`
  is provenance for the materialized map, not fuzzy execution. Preserve this
  valid M through identifier healing and workbook binding into TMDL, covered by
  static source-to-TMDL tests, not native refresh/value execution. N1 remains
  partial (one observed dialect of eight); N2's first bounded Remap slice is
  delivered. The other seven clean dialects remain unsupported. **#81 is not complete.**
  Preserve #82/#83 container/DAG and standalone batch M+sources behavior (no
  standalone batch PBIP); generated M/TMDL is not refresh verification.

- Preserve authored `m_query_override` partitions through local-source auditing:
  missing inputs/stale connection metadata remain diagnostics, without replacing
  the query or claiming refresh success. Generated missing sources still fail
  explicitly. MCP and CLI quality inspection resolve the generated identity from
  project artifacts/metadata (including `P_<hash>`), rejecting ambiguity instead
  of reporting false missing artifacts under the source filename.

- Fix batch `--resume` to look up the same planned `P_<hash>` identity used by
  generation, retaining safe legacy-name markers and rechecking Desktop paths.
  Separate generator root validation from full-project validation so flat thin
  reports (shared models and Fabric) are not rejected for a layout they do not
  create; publish their `.pbip` only after content and actual path checks.
  Single-file checkpoints now complete only after postprocessing and the final
  path guard. Failed postprocessing/path checks invalidate the entry point and
  generation checkpoint. Resumed outputs are rechecked even with
  `--no-verify-open`; older single-file generation checkpoints are regenerated
  once because they do not prove postprocessing completed.

- For batch resume, use legacy marker-only fallback only when the checkpoint
  file is absent; an empty, malformed, non-object, or invalid-UTF-8 checkpoint
  forces regeneration with a warning, as do read failures. Failed or
  interrupted batch postprocessing retracts the PBIP entry marker using the
  planned identity, so retries regenerate rather than skip incomplete output.
  Batch checkpoint signatures now hash each workbook's effective configuration:
  manifest-resolved culture and calendar, requested Prep path and file-content
  signature, quality policy, `verify_open`, output format, model mode, and
  extraction mode. Changing an effective setting or Prep contents invalidates
  that workbook; unchanged effective settings and unrelated manifest entries do
  not. A requested but missing/unreadable Prep file cannot match a successful
  checkpoint. Older global-config signatures regenerate once under the new
  signature version. This is separate from the legacy marker-only fallback,
  which remains available only when the checkpoint file itself is absent.
  Roll back the in-memory checkpoint entry under the lock on any save
  `BaseException`, then propagate it: another worker can no longer persist an
  interrupted worker's uncommitted success. Five checkpoint-write fault
  scenarios cover partial writes, persistent/transient replacement locks,
  interrupted saves and two concurrent workers. Failed saves preserve the prior
  on-disk snapshot and clean temporary files; retries regenerate incomplete
  output, while a transient lock can recover without regeneration.
  Ten focused regressions cover effective manifest culture/calendar changes,
  unchanged overrides despite changed globals, unrelated manifest entries,
  Prep content edits and missing Prep, quality policy and `verify_open`
  changes, and one-time regeneration of old signatures.
  Successful workbook checkpoints now also record a bounded SHA-256 inventory
  of generated structural files. On resume, added, deleted, or content-edited
  inventoried files force regeneration; unchanged inventory can be skipped only
  with matching source/configuration signatures and a completed output marker.
  This is not full schema validation or an all-artifact audit: customer files
  under the project-root `Data` directory and Desktop-local `.pbi` state are
  excluded, and the inventory does not prove native refresh. Older successful
  checkpoints without an inventory regenerate once to establish a baseline.
  The legacy marker-only fallback remains limited to an absent checkpoint file;
  malformed/unreadable checkpoints force regeneration. Standalone Prep flows
  do not qualify for PBIP/Fabric completion-marker skipping.
  Validate each workbook's checkpoint entry, result, and optional `report_name`
  identity before reuse. Invalid entries log only the workbook key and reason,
  not checkpoint payload; only that workbook is regenerated while healthy
  entries remain reusable. A supplied identity is limited to the planned
  project name or source basename, and an omitted optional result/identity
  remains compatible with existing checkpoints.
  Capture workbook and requested Prep content signatures when each batch task
  starts, refreshing a queued task's Prep signature at that boundary; recheck
  the signatures before recording success. A workbook or Prep input that
  changes or becomes deleted/unreadable after capture fails explicitly,
  retracts the PBIP marker, and remains retryable. A requested Prep flow already
  missing at task start fails before the worker can report success. The
  checkpoint records the captured input digest, not a new digest taken after
  output generation. Eleven focused source/entry-stability cases and the
  missing-Prep preflight contract cover these boundaries. This compares
  before/after digests only; it does not provide input snapshots, atomic-input
  or continuous-monitoring guarantees, or protection against ABA changes.
  Fault-injection regressions also verify that failed Prep JSON dumping or
  replacement leaves existing datasource JSON intact and cleans the staged
  file; this verifies existing atomic-write behavior and is not a new Prep
  production fix. N3 remains partial; these checks do not claim full operational
  closure, Power BI Desktop or runtime validation.

- Preflight every discovered batch candidate's planned output destination
  before resume skipping, dry-run return, or output/checkpoint/log writes.
  Compare normalized Windows path identities case-insensitively, including
  path-budget-shortened workbook names and Prep-flow targets. Same-stem inputs
  with different extensions, a shortened project colliding with a Prep target,
  and case-only path aliases now fail explicitly with `VALIDATION_FAILED`
  (exit code 5) without generating outputs or overwriting existing checkpoint,
  PBIP, or JSONL log files. Rename conflicting inputs or place them in distinct
  noncolliding source subdirectories; names are not auto-renamed. Distinct
  subdirectories remain valid, and a single candidate's path-budget failure
  remains isolated from healthy siblings. Focused regressions:
  `python -m pytest tests/test_batch_output_collisions.py -q` — **7 passed**.
  This bounded result does not close N3 or claim Desktop/runtime validation.

- Stop injecting Power Query `Source = try ... otherwise #table(...)` guards
  during semantic-model generation and self-healing, for all connectors.
  Connection and navigation failures now propagate instead of reporting a
  successful empty refresh. Keep `wrap_source_with_try_otherwise()` as a
  documented compatibility no-op; preserve authored M and targeted value-level
  `try` handling. Spatial-file routing recognizes unwrapped generated sources
  without relying on fallback-table metadata. Existing generated projects are
  not rewritten by this change.

- Updated the CLI command reference for folder-based bulk generation and
  separate diagnostics. Corrected the `deploy` help text: the batch alias
  generates Fabric artifacts but does not deploy or execute a pipeline.

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

The [package metadata](pyproject.toml) declares `44.0.0`; publication status is
unverified here. Detailed historical changelog entries have been intentionally
cleared.
