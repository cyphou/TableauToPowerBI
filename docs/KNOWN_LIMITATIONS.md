# Known Limitations

[🏠 Overview](../README.md) · [📚 Documentation](README.md) · [🌐 Local browser](LOCAL_WEB_UI.md) · [✅ Review checklist](MIGRATION_CHECKLIST.md)

This document lists known limitations and approximations in the Tableau to Power BI migration tool.

---

## Local browser interface

The supported guided UI is the [local browser runner](LOCAL_WEB_UI.md), not a
packaged Windows application. It accepts separate, non-nested local source
and output paths; the current page uses manual path entry, not folder/ZIP
upload, a Server task, or a Browse button. The optional upload UI and REST API
have separate source-confinement rules.

The runner binds to loopback, keeps a bounded in-memory history, and allows
one active job. It is not a production/multiuser server, and closing a browser
tab does not cancel a job. Native Power BI/Tableau diagnostics require a
separate confirmation and can access configured network sources; ordinary
local generation is not proof of native refresh or runtime parity.

## Extraction Limitations

| Area | Limitation | Impact |
|------|-----------|--------|
| **Hyper files** | PBIP requires complete snapshot export and exact CSV/Parquet-to-Import bindings. Auto requires native Parquet when the uncompressed Hyper file is strictly > 10 MiB, otherwise CSV; Fabric auto remains CSV. Row caps, including environment caps, and metadata-only sample readers are rejected for complete PBIP migrations. Missing/incomplete/unbound required exports block success. `--hyper-rows` remains separate legacy metadata sampling, not a batch export cap. | Proprietary Hyper and native Parquet require optional `tableauhyperapi`. SQLite handles compatible inputs, but cannot provide native Parquet: above the PBIP threshold it requires an explicit CSV override. Native Parquet needs a writable, authorized ASCII transit directory; uploads cannot escape their job boundary to obtain one. Full-export/static binding checks do not prove Desktop loading, saved state or parity. See the [decision diagram](ARCHITECTURE.md#automatic-storage-mode-and-hyper-export). |
| **TDE files (legacy)** | ⚠️ `.tde` is the **pre-2018 legacy extract format** and cannot be read by any tier (not a Hyper database, not SQLite-compatible). Tables get a placeholder `#table()` partition with a `TODO` comment. | Re-save the workbook in Tableau Desktop to convert `.tde` → `.hyper`, or manually export data to CSV |
| **Tableau Server/Cloud** | ✅ `--server` CLI flag enables direct extraction from Tableau Server/Cloud via REST API (PAT or password auth). Live connections still need reconfiguration in PBI |
| **Tableau 2024.3+** | ✅ Dynamic parameters with database queries fully extracted and converted to M partition with `Value.NativeQuery()`. Dynamic zone visibility parses conditions and maps to PBI bookmark visibility toggles. Table extensions (Einstein Discovery, external API) generate M `Web.Contents()` or placeholder. Linguistic schema extraction feeds PBI Q&A synonyms. | Some newer Tableau 2024.3+ features may still need manual adjustment |
| **Custom shapes** | Shape encoding extracts the field reference only — actual image files are not migrated | Custom shape visuals will show default markers |
| **OAuth credentials** | Credential metadata is stripped by design | Data source connections need re-authentication in Power BI |
| **Nested layout containers** | ✅ IMPROVED — Grid-snapping engine handles 3-level nesting; 4+ levels may lose precision | Very deeply nested containers may need manual adjustment |
| **Rich tooltips** | HTML/custom layout tooltips are converted to run-level text (bold, color, font_size extracted) | Complex HTML tooltip layouts are not preserved |

Strict Hyper Import evidence requires a persistent exact source identity.
Loose Hyper inputs without that identity and legacy caption-staged CSV aliases
are not certified by the full-import gate; a filename or `staged_files` claim
alone cannot establish a binding. Derived groups require explicit membership
metadata: hidden Tableau clustering without recoverable declarations is not
reconstructed from the physical Hyper columns.

Hyper verification reads absolute paths stored in the generated project (the
`Migration_HyperIdentity` annotation, `Data/hyper_exports.json`, the `DataFolder`
parameter and `File.Contents` literals). None of them is resolved through the
filesystem: each is normalized as text and compared with the keys of the `Data`
folder the operator selected, so a UNC path, a device-namespace path or a
local-looking path routed through a symlink or junction cannot make
verification contact a remote host. A `Data` directory, `expressions.tmdl`,
export file or manifest that is a link is reported and never opened. The cost is
that an alias of the `Data` folder through a link or an 8.3 short name is not
accepted; regenerate the project at its real location. A drive letter mapped to
a network share looks local and is treated as local. The generic project
scanners and the MCP `quality_report(project_dir)` tool do not test for links,
so treat a project that this tool did not generate as untrusted input. Those
artifacts and the quality report also embed the source workbook's absolute
path, so share generated projects only within the trust zone that produced
them. See [Upload Security](../UPLOAD_SECURITY.md#hyper-project-metadata).

Native Hyper geography is exported as WKT text in Parquet, not as a native
spatial column. CSV geography serialization remains the SDK's bytes
representation. Other unsupported Parquet types remain explicit errors;
successful export does not establish spatial or visual parity.

## Generation Limitations

### Automatic storage mode

`--mode auto` imports Hyper snapshots and selects DirectQuery for supported
native database connectors without Hyper. Excel, CSV, native MySQL and other import-only
sources must remain Import; they cannot be made DirectQuery by changing a
partition label. `--mode import` and the local browser **Import** control let
the user override automatic DirectQuery. Mixed-source models are resolved per
table. Explicit DirectQuery requests for Hyper or import-only files fail
instead of producing an invalid file-backed DirectQuery model.

Non-Hyper Import generates only the connection, schema and Power Query M.
Migration does not export native rows. An Excel workbook explicitly packaged
in a TWBX is copied unchanged into the generated `Data/` folder and bound by its
absolute path; loose native files and packaged non-Excel files are not copied.
This exception covers trusted local per-workbook batch generation only, not
uploaded jobs or shared-model/add-to-model/remove-from-model workflows.
Loose-workbook relative paths are resolved against the
original workbook directory without dataset I/O; absolute paths are preserved.
Other packaged native data are not copied or rebound to guessed files, and
drive-relative/ambiguous paths still need explicit configuration. Source
delimiters and schemas must agree with the file. Credentials and source access
are required when Power BI loads the data at refresh.
Hyper data still export to CSV/Parquet and use Import.

The packaged-Excel copy preserves every sheet and the workbook metadata; it
contains source data, including when included in a downloadable output ZIP.
Keep it out of repositories. Moving the project requires updating its absolute
Power Query source path; service refresh requires gateway/file access.
A byte-for-byte copy does not prove sheet/column validity, successful refresh,
saved/reopened state or value/visual parity.

Native shapefiles cannot be parsed by the GeoJSON M connector. PBIP emits an
actionable `SourceBinding.Error`; supply a supported GeoJSON source separately.
It does not silently parse binary `.shp` as JSON or convert native spatial rows.
Fabric's separate spatial-staging behavior is unchanged.

Neither native Import nor DirectQuery generation establishes credentials,
gateway availability, query folding or successful Power BI refresh.
Fabric-native DirectLake generation retains its separate file-staging contract.

### Active extract behind a live connection

When a federated datasource has a live connection (for example PostgreSQL) and
an **enabled** extract, the packaged Hyper snapshot is the source: it is
exported in full and bound in Import mode, and explicit DirectQuery is rejected.
The live connection stays in `datasources.json`. The Hyper Import section of the
quality report states this choice in a **Source decision** table (datasource,
retained source, unused original connector type). Only the datasource label and
the connector type are reported, never the server, database, user or any other
connection detail, and the table adds no warning: it does not change the
PASS/WARN status or the gate. There is no option to prefer the live source,
because a Tableau extract can be filtered, aggregated or dated and is therefore
not interchangeable with its source.

To migrate such a datasource against its live source, remove the extract and its
`.hyper` in Tableau before migrating. Only disabling the extract is not enough
while the `.hyper` stays packaged in the TWBX: the model is built from the live
tables, but the unused snapshot is still exported and the Hyper Import gate then
stops the migration (`Hyper export record … is unconsumed by a final annotated
Import table`). When the gate fails for that reason, an extra `Hint:` diagnostic
names the datasources whose extract is disabled and the two ways out (re-enable
the extract, or remove the extract and its `.hyper`); it is never added to a
passing result and no live source is substituted automatically. Observed with
synthetic fixtures only, not with a workbook saved by Tableau; no live
connection was opened and no Desktop load, refresh or value parity was checked.

### Windows / Power BI Desktop path lengths

For workbook batch migrations, the orchestrator budgets the full absolute path
**before generation**, including both occurrences of the workbook name. When
necessary it chooses a deterministic `P_<hash>` project name and uses that same
name for the project folder, `.pbip`, `.Report`, `.SemanticModel`, references and
postprocessing. Table filenames are capped separately; logical table names are
unchanged. No late `R.Report` rename is performed.

If even that compact identity does not fit, generation fails explicitly: choose
a shorter `--output-dir` (for example `C:\PBI`). Direct generator/shared-model
callers must supply a name and root that fit the preflight budget; they are not
silently renamed. Optional artifacts and staged data may need additional room;
a final path check is still required. Existing projects are not relocated.

This is **not** extended Windows `\\?\` write support. Such output roots are
rejected; enabling Windows long paths or adding that prefix does not lift
Desktop's own limits (files below 260 UTF-16 code units, directories below 248).
Static path checks do not prove successful native Desktop loading or refresh.
Filesystem/generation errors, including WinError 206, fail generation rather than
reporting a partial model as successful. The `.pbip` entry point is written only
after artifact generation and the final path check; partial artifact directories
can remain after failure for diagnosis.

Batch resume uses the planned identity, including `P_<hash>`, with compatible
legacy-name markers accepted only when their actual paths fit. Thin reports use
their flat report-only layout rather than the nested full-project forecast;
their actual generated paths are checked before publishing the `.pbip`.
Single-file completion checkpoints include postprocessing and the final path
guard. Postprocessing/path failures remove the `.pbip` entry point and prevent
generation reuse. Resume rechecks actual Desktop paths even with
`--no-verify-open`; that flag skips the broader static openability suite, not
path safety. Older single-file generation checkpoints are regenerated once
(their extraction snapshot can still be reused).

Automatic generation and static validation do not establish rendered parity.
They also do not establish successful native Desktop loading or refresh.
`openable=true` is a compatibility name for a static verdict; refresh remains
`not_run` unless a separate native diagnostic is performed. Bug report #84
describes an orphaned `FromColumn` object during query/relationship
reconciliation despite consistent declared endpoints. Its no-relationship
variant reportedly passed, while null-row and typed-empty variants failed.
Without the exact reproduction, the relationship-triggered cause is unresolved;
typing empty outputs is not claimed as its remedy.
In a focused Desktop recheck, removing identical Tooltip projections already
bound in primary roles resolved a repeated-index visual error: bars rendered,
table row counts were unchanged, and no accessible visual-error elements were
found. This evidence is limited to that recheck; maps remain under investigation
and require Desktop review.

| Area | Limitation | Impact |
|------|-----------|--------|
| **Visual positioning** | ✅ IMPROVED — Grid-snapping + scaled zone mapping preserve most real-world layout/sizing. | One known caveat remains: floating legend overlays can still render side-by-side instead of overlaying chart corners in some dashboards. |
| **Independent text labels** | A Tableau Text encoding can aggregate a different measure from the chart's axis. | The independent measure is retained in Tooltips with an approximation annotation, without replacing Y. Misleading native Y labels are suppressed; exact label placement still requires review. |
| **Sparklines** | Table/matrix sparkline columns are generated as lineChart sparkline configs | Limited to basic line sparklines; area/bar sparklines not supported |
| **Bump charts** | ✅ IMPROVED — Auto-generated RANKX measure injected for ranking semantics | Maps to lineChart; ranking is approximated via `RANKX(ALL(), [measure],, ASC, Dense)` |
| **Fabric-native output** | ✅ NEW — `--output-format fabric` generates Lakehouse, Dataflow Gen2, PySpark Notebook, DirectLake Semantic Model, and Data Pipeline. Activity IDs in pipelines are placeholders requiring workspace binding. | Fabric artifacts require Fabric workspace capacity to deploy and test |
| **DAX optimizer** | ✅ NEW — `--optimize-dax` rewrites verbose DAX (IF→SWITCH, COALESCE, constant fold, SUMX simplify). `--time-intelligence auto` injects YTD/PY/YoY%. Opt-in only to preserve original semantics by default. | Optimized DAX may differ from direct Tableau→DAX conversion; original preserved as annotation |

## DAX Conversion Limitations

### Combined groups spanning tables

A combined group that concatenates fields from different tables needs one
unambiguous active, source-defined to-one relationship path to each referenced
table. Canonical column names follow the owning table's exact rename metadata.
An inferred edge, a many-to-many edge, or multiple possible row paths does not
establish scalar lookup semantics. These cases stop generation rather than
selecting an arbitrary match or creating blank placeholder columns. Resolve the
source grain and relationship metadata before regenerating.

### Composite and range logical relationships

Logical relationships with multiple comparisons or inequalities cannot be
represented by a single Power BI model equality relationship. Extraction retains
their predicates and table aliases. A constrained conversion path supports
`SUM` over pair-level calculated expressions, including supported `FIXED`
dependencies, without duplicating unrelated single-table measures.

This path requires an explicit policy keyed by the extracted datasource name,
provided through `extra_objects['logical_relationship_policies']` or the JSON
environment variable `TTPBI_LOGICAL_RELATIONSHIP_POLICIES`. Each policy declares:

- `row_scopes`: lineage-preserving DAX table expressions for `left`, `right`,
  `fixed_left` and `fixed_right`, accounting for source/context filter order.
- `unmatched_left`: `exclude` or `null_extend`, based on the source calculation.
- `contains_case_sensitive` and `comparison_case_sensitive`: source comparison
  behavior.
- `case_encoding_verified`: `true` only after verifying that the model's string
  encoding preserves the distinctions required by those comparisons.

Missing policies, unsupported independent intermediate-calculation uses and
unrepresentable predicates stop generation rather than using an arbitrary
`LOOKUPVALUE`, deduplicating matches or replacing errors with blanks. These
policies are evidence requirements, not defaults to copy between workbooks.
Generated expressions still require runtime and source-value checks.

### Functions with No DAX Equivalent

| Tableau Function | Output | Reason |
|-----------------|--------|--------|
| MAKEPOINT, MAKELINE, DISTANCE, BUFFER, AREA, INTERSECTION | `0` + comment | No spatial functions in DAX |
| HEXBINX, HEXBINY | `0` + comment | No hex-binning in DAX |
| COLLECT | `0` + comment | No spatial collection |
| SCRIPT_BOOL/INT/REAL/STR | ✅ `scriptVisual` (Python or R) + `BLANK()` DAX fallback | R/Python scripting → PBI Python/R visual containers with script text and input columns. `BLANK()` DAX measure generated for non-visual contexts. Requires Python/R runtime configured in PBI Desktop |
| **SPLIT** | ✅ IMPLEMENTED — `SPLIT(string, delim, token)` → `PATHITEM(SUBSTITUTE(string, delim, "|"), token)`. Negative index → `PATHITEMREVERSE`. 2-arg form defaults to token 1 | Requires pipe character not present in data |

### Approximated Functions

| Tableau Function | DAX Output | Accuracy |
|-----------------|------------|----------|
| REGEXP_MATCH | Smart pattern detection: `LEFT`/`RIGHT`/`CONTAINSSTRING`/`OR` | ✅ IMPROVED — Handles `^literal$` exact match, `^literal`, `literal$`, `.+`/`.*` always-true, `pat1\|pat2`, simple substrings; complex regex falls back to `CONTAINSSTRING` |
| REGEXP_REPLACE | Chained `SUBSTITUTE()` for common patterns; `CONTAINSSTRING`+`SUBSTITUTE` for character classes | No true regex groups or backreferences |
| REGEXP_EXTRACT | `MID(field, SEARCH("prefix", field) + len, LEN(field))` for fixed-prefix patterns | Falls back to `BLANK()` for complex patterns |
| REGEXP_EXTRACT_NTH | Delimiter→PATHITEM, prefix→MID/SEARCH, alternation→IF/CONTAINSSTRING | Falls back to `BLANK()` for complex patterns |
| RANK_PERCENTILE | `DIVIDE(RANKX()-1, COUNTROWS()-1)` | Edge cases with ties |
| RUNNING_SUM/AVG/COUNT | `CALCULATE(AGG, FILTER(ALLSELECTED(...)))` | Proper window semantics with partition support |
| WINDOW_SUM/AVG/MAX/MIN | `CALCULATE(inner, ALL/ALLEXCEPT)` with OFFSET-based frame boundaries | Frame start/end positions approximated via OFFSET for specific patterns |
| LTRIM/RTRIM | ✅ FIXED — `LTRIM` → MID-based left-trim (preserves trailing spaces); `RTRIM` → LEFT-based right-trim (preserves leading spaces) | Distinct from TRIM which removes both sides |
| String `+` → `&` | All expression depths | Converted at all nesting levels since v4.0 |

## Visual Mapping Approximations

| Tableau Visual | PBI Mapping | Gap |
|---------------|------------|-----|
| Sankey / Chord / Network | ✅ Custom visual GUID (`sankeyDiagram`, `chordChart`, `networkNavigator`) or `decompositionTree` fallback | Custom visuals require AppSource installation in PBI Desktop |
| Gantt Bar / Lollipop | `clusteredBarChart`; annotated `tableEx` fallback without a category | Approximation; time-axis parity is not established |
| Butterfly / Waffle | hundredPercentStackedBarChart | ✅ IMPROVED — negate-one-measure hint in approximation note |
| Calendar Heat Map | matrix | ✅ IMPROVED — auto-enables conditional formatting properties + migration note |
| Explicit Packed Bubble / Strip Plot | scatterChart | Explicit marks may use scatter; size encoding can populate Size, but required scatter roles need review |
| Shelfless inferred packed bubble | treemap | Empty Rows/Columns shelves, one Size measure + Colour dimension; approximation does not preserve bubble geometry |
| Bump Chart / Slope | lineChart | ✅ IMPROVED — Auto-generated RANKX measure for ranking semantics |
| Motion chart (animated) | Not handled | No PBI play-axis animation |
| Violin plot | ✅ `boxAndWhisker` + custom visual (`ViolinPlot1.0.0`) | Maps to Box & Whisker; AppSource custom visual GUID available |
| Parallel coordinates | ✅ `lineChart` + custom visual (`ParallelCoordinates1.0.0`) | Maps to Line Chart; AppSource custom visual GUID available |

## Power Query M Limitations

| Area | Limitation |
|------|-----------|
| **Prep graph and export validation** | Ambiguous container boundaries, dangling references, cycles and invalid collection shapes are rejected. Missing/null optional collections normalize to empty collections. Requested M conversion failures set `conversion_error`; standalone batch rejects missing outputs or M queries. Leaf-node output fallback remains supported when no explicit outputs exist. Generated M does not prove successful refresh or semantic equivalence. |
| **Prep schema and workbook coverage** | Supported operations propagate field names/types, with checked output declarations. Missing columns, unresolved output types, colliding join names, dynamic pivot schemas and unsupported manual union mappings fail explicitly. Workbook `--prep` requires at least one binding and complete coverage of published workbook tables; unused auxiliary outputs are reported, not appended. The reserved `F_<n>_<subject>` → `S_F<n>_<subject>` rule additionally requires exact published identity metadata. See the [binding contract](TABLEAU_PREP_TO_POWERQUERY_REFERENCE.md#parser-scope-and-workbook-binding-contract-reports-8183). |
| **Prep #81 incomplete clean dialect coverage** | N2's first bounded `.v2018_3_3.Remap` slice supports observed explicit JSON-encoded TEXT maps standalone and in grouped clean actions: simultaneous exact case-sensitive lookup preserves unmatched values/nulls. Missing/non-string upstream fields and invalid/ambiguous maps reject. `groupMethodProps` is provenance for the materialized map, not fuzzy execution. Static source-to-TMDL tests cover conversion and M/schema preservation; native refresh/value execution remains `not_run`. N1 is partial (one observed dialect of eight). The other seven dialects (`ValueFilter`, `RichRangeFilter`, `RangeFilter`, `RichDiscreteValueFilter`, `QuickCalcColumn`, `RichWildcardFilter`, `RichNullFilter`) remain unsupported and lack demonstrated producer payloads. M helper availability/analyzer classification is not parser support. Existing #82/#83 safeguards are preserved. See the [Prep dialect evidence](TABLEAU_PREP_TO_POWERQUERY_REFERENCE.md#clean-node-dialect-evidence-status). |
| **M identifier quoting** | ✅ FIXED — Column names with special characters (hyphens, slashes, parentheses, etc.) are now auto-quoted as `[#"Name"]` in M expressions. Previously, names like `Sub-Category` caused "Invalid identifier" errors |
| **Custom SQL params** | ✅ IMPLEMENTED — `Value.NativeQuery()` with parameter record binding and `[EnableFolding=true]` |
| **Hyper data** | Packaged extracts require complete staged CSV/Parquet sources and exact physical-identity Import bindings, not sample/empty M `#table()` substitutes. Metadata/header fallback remains diagnostic only; it cannot satisfy a full PBIP migration. File counts/hashes and bindings are checked independently of native loaded-row and value parity. See the [Hyper contract](FAQ.md#how-are-hyper-extract-files-handled). |
| **Query folding** | ✅ IMPLEMENTED — `m_transform_buffer()` + `m_transform_join(buffer_right=True)` for `Table.Buffer()` folding boundaries |
| **PDF connector** | ✅ IMPROVED — `Pdf.Tables(File.Contents(...), [StartPage=N, EndPage=M])` with page range and table index selection |
| **Salesforce connector** | ✅ IMPROVED — SOQL passthrough via `Value.NativeQuery()`, API version specification, relationship traversal via `Table.ExpandRecordColumn()` |
| **REGEX in M** | ✅ IMPLEMENTED — `m_regex_match/extract/replace()` + `convert_tableau_regex_to_m()` dispatcher using `Text.RegexMatch/Extract/Replace` with `try/otherwise` |

## Deployment Limitations

| Area | Limitation |
|------|-----------|
| **PBI Service deployment** | Batch generation does not forward `--deploy`. Use the separate MCP `deploy` tool, preview with its default `dry_run=true`, and explicitly authorize publishing with configured credentials. See the [deployment guide](DEPLOYMENT_GUIDE.md). Live integration checks are opt-in, not evidence supplied by standard CI |
| **Fabric deployment** | Fabric deployment is structurally tested but not against a real workspace |
| **Windows paths** | ✅ OneDrive file locks handled via `_rmtree_with_retry()` with exponential backoff (3 attempts). Stale TMDL files retried with 0.3s backoff |

## Plugin System Limitations

| Area | Limitation |
|------|------------|
| **Plugin API stability** | The plugin hook interface (`plugins.py`) is functional but the API is not yet frozen — custom plugins may need updates across major versions |
| **Plugin discovery** | Plugins are auto-discovered from `examples/plugins/` via `importlib` — only `.py` files with a `register(hooks)` function are loaded |

## Schema Compatibility

| Area | Limitation |
|------|------------|
| **PBIR schema versions** | Current generator targets PBIR v4.0: report 2.0.0, page 2.1.0, visualContainer 2.7.0, bookmark 2.1.0; base-theme name `CY26SU04`. These constants do not establish a minimum Desktop version. Schema/static validation is not runtime compatibility |

## Shared Semantic Model Limitations

| Area | Limitation |
|------|------------|
| **Table matching** | Tables are matched by physical fingerprint (`connection_type\|server\|database\|table_name`) — tables with the same name but from different servers are NOT merged |
| **Column type conflicts** | When the same column has different types across workbooks, the wider type is used (e.g., integer → string). Data may need type casting after migration |
| **Measure namespacing** | Conflicting measures (same name, different formula) are namespaced as `Measure (Workbook)`. Visuals referencing the original measure name may need manual update |
| **Custom SQL tables** | ✅ RESOLVED — Custom SQL tables are now fingerprinted by normalized SQL hash (`_normalize_sql()` + SHA-256). Identical queries across workbooks are merge candidates |
| **Cross-workbook RLS** | RLS roles from multiple workbooks are merged but may have overlapping rules. Review `Manage Roles` in PBI Desktop |
| **Post-merge validation** | Use `--strict-merge` to block generation on validation failures (relationship cycles, column type errors, broken DAX references). Without it, validation is advisory only |

## Workarounds

For most limitations, the recommended workflow is:

1. Run the migration to generate the .pbip project
2. Open in a Power BI Desktop installation supporting the generated PBIP/TMDL schemas
3. Review the migration metadata JSON for conversion notes
4. Manually adjust unsupported features (spatial, custom shapes, advanced formatting)
5. Re-authenticate data source connections
6. Validate measures and relationships in Model view
7. Use `--assess` flag for pre-migration readiness analysis
8. Use `--incremental` for iterative refinement without losing manual edits
9. Publish separately through the [authorized deployment workflow](DEPLOYMENT_GUIDE.md); batch generation does not deploy to Power BI Service
10. Use `--server` to extract workbooks directly from Tableau Server/Cloud
11. Use `--languages fr-FR,de-DE` to generate multi-language culture TMDL files with translated display folders
12. Use `--goals` to convert Tableau Pulse metrics to Power BI Goals/Scorecard artifacts
13. Use `--check-schema` to verify PBIR schema forward-compatibility before opening in newer PBI Desktop versions
14. Use `--shared-model wb1.twbx wb2.twbx` to merge multiple workbooks into a shared semantic model with thin reports
15. Use `--assess-merge` to preview merge feasibility before generating
16. Use `--migrate-schedules` (with `--server`) to extract Tableau refresh schedules and generate PBI refresh config JSON
17. Use `notebook_api.MigrationSession` for interactive Jupyter-based migration with DAX/visual override and notebook generation
