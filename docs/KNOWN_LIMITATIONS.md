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
| **Hyper files** | A complete explicit original database takes precedence over Hyper in the same datasource, without Hyper data reading/export. Otherwise PBIP requires complete retained-snapshot export and exact CSV/Parquet-to-Import bindings. Auto requires native Parquet when the uncompressed Hyper file is strictly > 10 MiB, otherwise CSV; Fabric auto remains CSV. Row caps, including environment caps, and metadata-only sample readers are rejected for retained-Hyper PBIP migrations. Missing/incomplete/unbound required exports block success. `--hyper-rows` remains separate legacy metadata sampling, not a batch export cap. | Proprietary Hyper and native Parquet require optional `tableauhyperapi` only when Hyper is retained. SQLite handles compatible inputs, but cannot provide native Parquet: above the PBIP threshold it requires an explicit CSV override. Native Parquet needs a writable, authorized ASCII transit directory; uploads cannot escape their job boundary to obtain one. Full-export/static binding checks and database source selection do not prove Desktop loading, saved state or parity. See the [decision diagram](ARCHITECTURE.md#automatic-storage-mode-and-hyper-export). |
| **TDE files (legacy)** | `.tde` is the pre-2018 legacy extract format, not a Hyper database or SQLite-compatible file. Required legacy-only extracts block PBIP batch migration; empty/sample placeholders cannot satisfy the full-data gate. | Upgrade/package the extract as `.hyper` using an authorized compatible Tableau Desktop version (2024.2 is the last that supports this), or repair the workbook binding to a complete supported export/original database. See the [recovery steps](FAQ.md#what-about-tde-files-legacy-tableau-data-extract). Missing rows cannot be reconstructed. |
| **Tableau Server/Cloud** | ✅ `--server` CLI flag enables direct extraction from Tableau Server/Cloud via REST API (PAT or password auth). Live connections still need reconfiguration in PBI |
| **Tableau 2024.3+** | ✅ Dynamic parameters with database queries fully extracted and converted to M partition with `Value.NativeQuery()`. Dynamic zone visibility parses conditions and maps to PBI bookmark visibility toggles. Table extensions (Einstein Discovery, external API) generate M `Web.Contents()` or placeholder. Linguistic schema extraction feeds PBI Q&A synonyms. | Some newer Tableau 2024.3+ features may still need manual adjustment |
| **Custom shapes** | Shape encoding extracts the field reference only — actual image files are not migrated | Custom shape visuals will show default markers |
| **OAuth credentials** | Credential metadata is stripped by design | Data source connections need re-authentication in Power BI |
| **Nested layout containers** | ✅ IMPROVED — Grid-snapping engine handles 3-level nesting; 4+ levels may lose precision | Very deeply nested containers may need manual adjustment |
| **Rich tooltips** | HTML/custom layout tooltips are converted to run-level text (bold, color, font_size extracted) | Complex HTML tooltip layouts are not preserved |
| **Relevant-value filters** | Inclusive member lists do not reliably distinguish a partial selection from dynamic **All**. Explicit all-members markers are handled separately. | Ambiguous lists are retained as snapshots with a warning; new members may remain excluded. |

Generated filter controls use Power BI's default slicer interactions, which may
affect other page visuals and intersect saved worksheet-level filters. Their
scope and interactive selection behavior require manual comparison with Tableau.
Unknown or ambiguous datasource bindings are warned and omitted rather than
attached to an arbitrary field.

Resolved `COUNTD` columns exclude blank values. An unresolved ordinary visual
binding can fall back to a distinct count that includes blank; unresolved quick
calculations are omitted. A `RUNNING_*` formula over `COUNTD` is emitted as
a warned `TODO`/`BLANK`, not a cumulative distinct-count approximation.
Percent-of-total and other quick calculations are not guaranteed to preserve
Tableau's mark partitions or distinct-count semantics. Review warnings and
validate values in the source and target.

Strict Hyper Import evidence requires a persistent exact source identity.
Loose Hyper inputs without that identity and legacy caption-staged CSV aliases
are not certified by the full-import gate; a filename or `staged_files` claim
alone cannot establish a binding. Derived groups require explicit membership
metadata: hidden Tableau clustering without recoverable declarations is not
reconstructed from the physical Hyper columns.

Multi-object object-graph extracts are bound only when the federated root has
exactly one native `collection` and the enabled Hyper extract has exactly one
Hyper `collection`. Each collection's leaves must pair one-to-one with the
matching object-context relations: native leaves with `context=""` relations,
Hyper leaves with `context="extract"` relations. Physical identity comes from
each extract relation, not the connection's `Extract` marker. Missing, extra,
duplicate, or mismatched collections/leaves stay unbound; this bounded path is
not a general fallback for arbitrary relation graphs.

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

### Active physical joins

Only a complete, resolved single-field equality predicate can be represented
as a Power BI relationship. Missing, opaque, non-equality and multi-condition
predicates are not reduced to a guessed or partial edge. In ordinary non-strict
PBIP batch generation, tables and the report are still emitted, every model
relationship between the affected source-table pair is omitted, and the reason
is written to `generation_diagnostics.json` and the PBIR Migration Summary
page. Dependent measures and visuals may therefore be blank or incorrect; the
PBIP is a repair starting point, not a semantically complete model. Static
openability is checked separately. Strict generation, TMDL-only, Fabric, and
shared-model paths continue to reject the unrepresentable join.

An exact Hyper identity or complete export of an underlying table reused by
multiple aliases does not establish that the active join result was
materialized; binding that same table to every alias can change row
multiplicity and filter behavior. Materialize the full joined result as one
source table with explicit mappings, or simplify the source join to supported
predicates. Static export and binding checks do not establish runtime or value
parity.

## Generation Limitations

### Diagnostic salvage (default recovery)

Partial recovery is the default for supported fresh ordinary PBIP workbook
folder batches and `--batch-config`. `--salvage-partial` remains an explicit
compatibility alias; explicit `--config` accepts a boolean flat `salvage_partial`
or nested `migration.salvage_partial`. `--strict-generation` disables salvage,
stops supported recoverable extraction refusals and keeps strict semantic
generation without a repair PBIP. Explicit CLI strict overrides a salvage
config default; explicitly passing both CLI flags is a configuration error.
This is diagnostic retention, not a readiness mode or permission to bypass
integrity guards.

After safe XML parsing, known object-level preflight refusals and refused
datasource normalization/extraction may be isolated. Incremental stage JSON and
raw refused declarations/formulas are retained as diagnostics, with credential
redaction. Security blockers remain fatal. Complete, safe extraction continues
to normal generation; supported incomplete extractions attempt a diagnostic PBIP
with page outlines and no source data, source model, or security roles. Semantic guard
failures can retain independent `.dax`, `.pq`, per-object source JSON and
`model_metadata.json` without depending on TMDL generation. Recovered text is
unvalidated: fragment recovery does not guess Hyper bindings or relationships,
read source data or execute source queries. A partial project that retains model
bindings is staged and checked separately; an incomplete Hyper binding withholds
its PBIP marker. Ambiguous visual references can produce a data-free repair PBIP.
If source checks pass and the only static blockers are unsupported DAX
placeholders, the model and report are retained as a diagnostic PBIP. Affected
calculations may return blank and dependent results may be incorrect. The
project remains `FAIL`, unvalidated and unsuitable for production; other
blocking static issues or a failed Hyper check do not qualify for this recovery.

Source metadata retention is not a promise of conversion: unresolved field
ownership, cross-table context and incomplete connections can block fragments;
Hyper/published bindings and connectors without a diagnostic-safe contract may
be `not_attempted`. Credential scrubbing does not make business formulas or
metadata public. Review recovery files before sharing. See the
[artifact triage procedure](FAQ.md#where-are-partial-artifacts-when-a-batch-fails).

`<Project>\Salvage\inventory.json` records recovered, blocked and not-attempted
items; `salvage_manifest.json` marks diagnostic-only, incomplete output with
`validation_status: FAIL`. A supported extraction refusal, semantic guard or DAX-placeholder check can retain a repair `.pbip`
with `completion_marker: diagnostic_only` and `partial_generation.json`; otherwise
the marker is `withheld`. A partial failure remains nonzero and is retried on
resume, not treated as successful completion. Prior project, extraction
and current quality/evidence outputs are preserved under `.migration_previous`
before the recovery attempt, never reused as current evidence.

Automatic recovery requires fresh ordinary PBIP workbook extraction.
Desktop diagnostics, `--skip-extraction`, Fabric, shared-model, standalone Prep
and inspection modes retain their legacy defaults, including the existing
`PartialGenerationError` repair fallback where applicable. These valid routes
do not become errors merely because ordinary batch recovery is automatic;
explicitly requesting incompatible salvage is an error. Shared-model and
standalone Prep salvage are not implemented. `--dry-run` makes no writes.
No Desktop opening, native refresh or runtime validation was performed for this
documentation update; recovery itself establishes none of them.

### Automatic storage mode

Source selection precedes storage mode: an eligible explicit original database
takes precedence over its Hyper extract. `--mode auto` imports retained Hyper
snapshots and selects DirectQuery for supported selected native database
connectors. Excel, CSV, native MySQL and other import-only
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
Retained Hyper data still export to CSV/Parquet and use Import.

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

When the same datasource contains Hyper and a concrete original database
connection (for example PostgreSQL), the database is preferred **before any
Hyper data read or row export**. Native connections, physical table ownership
and column lineage must be complete and unambiguous. Missing database endpoint
metadata or unsupported native relation bindings are explicit errors, not a
fallback to another source. Generation does not query the database.

This rule is per datasource, not per workbook. It does not turn an Excel/CSV
connection or an unresolved published `sqlproxy` reference into a database.
An extract shared with a remaining Hyper consumer is still required. Unknown
or unreferenced packaged Hyper files are not silently exempted.
Remaining snapshots retain full CSV/Parquet export and exact Import gates.
Custom SQL and other native non-table relation leaves are currently rejected
for this database-preference path; a physical table is not guessed from a
relation caption. Existing native-only Custom SQL handling is unchanged.

The Hyper Import section of the quality report records the **Source decision**
(database retained / Hyper not used, or snapshot retained / original connection
not used). Only datasource and connector labels are reported, never server,
database, user or other connection details. A database-only choice is
`not_applicable` for Hyper Import, not `static_verified`. Stale Hyper files,
manifests or unresolved Hyper partitions in an existing output still fail
their checks; use a fresh output directory instead of treating old exports as
evidence for the newly selected source.

This is an intentional source change, not snapshot parity: Tableau extracts
can be filtered, aggregated or dated. Credentials, gateway access, native
refresh, save/reopen, values and rendered comparison require separate
validation. Disabling an extract alone does not establish database eligibility.
For noneligible disabled extracts with an unused packaged Hyper, the existing
unconsumed-export diagnostic and conditional remediation hint still apply.

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

Failed PBIP workbook batches also write `migration_failure.json` with the
original stage and rejection details, synchronize openability/quality/evidence
to the final failed state, and retain `.migration_extract` snapshots for
diagnosis. A prior `openable=true` snapshot is not a completion signal.
Integrity guards remain blocking regardless of reporting policy.

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
selecting an arbitrary match or creating blank placeholder columns. When all
fields belong to one table, the group is materialized on that table rather than
on the widest table. A declared but missing/ambiguous datasource, absent
source fields, or a caption/raw-identifier collision with a column, measure or
source binding in the same datasource blocks generation. Without a declared
datasource, the collision check covers all model tables.
References in calculations and visuals are resolved to the generated group;
the same generated group can be processed again without creating a duplicate.
New extractions retain component derivation references alongside normalized
field names. Year-, quarter- and month-truncated components use calendar-period
starts in M and DAX; other unsupported date truncations, aggregations and table
calculations stop generation. These mappings do not establish fiscal-calendar
or display-label equivalence. Older extraction JSON without raw references
retains a caption-based date-function fallback; re-extract the workbook to
preserve the source derivations.

The resulting value is **approximate**, not a proven Tableau tuple: it joins
text values with ` | `. For example, `("A | B", "C")` and `("A", "B | C")`
can produce the same label. Null handling, culture-dependent text conversion
and Tableau display labels may also differ, including between the M and DAX
paths. Validate these semantics and values against each source before use.
Generated columns carry an approximation annotation, and parity and migration
reports classify combined groups separately from value groups. Unified quality
reports warn under `report` and block under `enterprise`/`production`; this is
a quality finding with no automatic evidence-based waiver, not a native
validation or an automatic repair.

### Composite and range logical relationships

Logical relationships with multiple comparisons or inequalities cannot be
represented by a single Power BI model equality relationship. Extraction retains
their predicates and table aliases. An opt-in Import path now supports a
conjunctive composite equality between two source aliases by encoding every
typed key component with an unambiguous length prefix. It creates a hidden key
on each table, keeps the original row tables intact, and checks uniqueness on
the declared one-side during each Power Query refresh.

Enable the composite-equality path through
`extra_objects['composite_relationship_policies']` or the JSON environment
variable `TTPBI_COMPOSITE_RELATIONSHIP_POLICIES`. The outer object is keyed by
the exact extracted datasource name; each inner key is the zero-based index of
that datasource's extracted logical relationship. A policy must contain exactly
these fields:

- `one_side`: `left` or `right`; that endpoint's complete tuple must be unique.
- `comparison_case_sensitive` and `comparison_exact`: both must be `true`;
  source case, accent and trailing-space comparison rules are not inferred or
  normalized.
- `nulls_match`: explicit boolean. When false, tuples containing null never
  match; when true, null components are encoded individually while the other
  components remain part of the key. Both cases are covered by the one-side
  uniqueness check.
- `filter_direction`: currently only `oneDirection`.

Example policy value:

```json
{
  "Synthetic": {
    "0": {
      "one_side": "right",
      "comparison_case_sensitive": true,
      "comparison_exact": true,
      "nulls_match": false,
      "filter_direction": "oneDirection"
    }
  }
}
```

Only matching string, integer, and boolean tuple components bound to M source
columns are supported; DAX-only columns are rejected. Literal M escape
sequences in source headers are preserved as header text.
Ranges, OR predicates, opaque operands, self-relationships, DirectQuery
partitions, repeated composite relationships between the same model tables and
unverified cardinality/collation remain blocked. Each participating table must
have one Import M partition. Multiple distinct composite pairs may share a
table. Environment policies for other datasources are ignored for the current
batch item, but a matching datasource name and index applies in every workbook:
verify that the evidence is valid for each one. The uniqueness assertion runs
on refresh, not during static generation; its distinct-key scan can add refresh
cost and may affect query folding. A failed uniqueness assertion fails the
refresh rather than silently demoting the relationship to many-to-many.

A separate constrained DAX path supports `SUM` over pair-level calculated
expressions for some equality/range predicates, including supported `FIXED`
dependencies, without duplicating unrelated single-table measures. It does not
create a general model relationship or materialize arbitrary interval matches.

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

A separate opt-in path handles only a single-table `SUM` of a `FIXED`
`MIN(physical_column)`. Supply `extra_objects['verified_fixed_scopes']` or the
JSON environment variable `TTPBI_VERIFIED_FIXED_SCOPES`, keyed by exact
extracted datasource name and the calculation's normalized raw identity (the
outer Tableau brackets are removed). Each scope must provide
`table`, `fixed_dimensions`, `value_column`, `physical_fields`, explicit
`source_filters` and `target_filters`, `filter_policy`, all `view_grains`
(including `[]`), `total_grain`, `total_aggregation`, `shelf_aggregation`,
`value_type`, and the compiler's eight `*_verified: true` assertions.
`source_filters` or `target_filters` may be empty only when the caller has
verified there are none. These fields are proof assertions, not evidence the
generator can discover: do not copy them between workbooks or set them merely
to bypass a placeholder. Missing/invalid proof keeps the DAX blocked. This
path does not cover multi-table LOD ratios, arbitrary `FIXED` bodies, or
automatic shelf-grain discovery.

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

`equivalence_report_v2.json` reports `visual_coverage` as `not_run`: it does
not have verified worksheet-to-PBIR projection provenance. The separate
`model_name_presence` check searches unqualified names across the whole model,
not the target visual or a proven datasource binding. Unambiguous calculation
IDs are resolved to captions; ambiguous IDs are not scored. The historical
`fidelity_percent` field covers these offline name checks only and does not
establish value, rendering, or visual parity.

The separate visual-diff report selects candidate visuals heuristically by
type, title and unqualified field names. Its historical `exact` and
`field_coverage` fields describe candidate type and name overlap, not verified
worksheet-to-visual provenance or rendered parity. With dashboards, that report
compares dashboard-placed worksheets and lists the others as excluded. The
source-fidelity audit keeps such results in `needs_review` even when Desktop
data loading was verified.

## Power Query M Limitations

| Area | Limitation |
|------|-----------|
| **Prep graph and export validation** | Ambiguous container boundaries, dangling references, cycles and invalid collection shapes are rejected. Missing/null optional collections normalize to empty collections. Requested M conversion failures set `conversion_error`; standalone batch rejects missing outputs or M queries. Leaf-node output fallback remains supported when no explicit outputs exist. Generated M does not prove successful refresh or semantic equivalence. |
| **Prep envelope/shape variants** | Known `superNode` envelopes normalize their `actionNode`, graph identity and ordered actions; this is not support for arbitrary inner operations. Column-keyed `fields` maps normalize with conflict checks. Unsupported filter directions/payloads and nonempty action annotations stay visible in inventory diagnostics and block conversion, rather than silently defaulting to `keep`. Only semantically empty, unambiguously routed containers collapse to identity. Data-dependent rows-to-columns pivot remains unsupported. See the [normalization contract](TABLEAU_PREP_TO_POWERQUERY_REFERENCE.md#envelopes-and-shape-variants-reports-85-87). |
| **Prep schema and workbook coverage** | Supported operations propagate field names/types, with checked output declarations. The observed `SimpleUnion.namespaceFieldMappings` form renames physical fields per exact incoming namespace before append; collisions, unknown mappings and incompatible types still reject. This does not support arbitrary manual mapping dialects. Missing columns, unresolved output types, colliding join names and dynamic pivot schemas fail explicitly. Workbook `--prep` requires at least one binding and complete coverage of published workbook tables; unused auxiliary outputs are reported, not appended. The reserved `F_<n>_<subject>` → `S_F<n>_<subject>` rule additionally requires exact published identity metadata. See the [binding contract](TABLEAU_PREP_TO_POWERQUERY_REFERENCE.md#parser-scope-and-workbook-binding-contract-reports-8183) and [mapped union scope](TABLEAU_PREP_TO_POWERQUERY_REFERENCE.md#observed-mapped-union). |
| **Prep SimpleJoin and union types** | Conversion requires explicit input namespaces, equality keys, and a per-node comparison/null/duplicate evidence policy. A nonempty declared output projection is honored; when absent, the parser derives all left fields plus right-side non-key fields after schema checks. The pinned Apache-2.0 Datus flow verifies an inner `[Region] == [Region]` join with `Left`/`Right` edges and no declared projection; it remains blocked without semantic policy. An environment policy is keyed by node ID, not flow: reused IDs share the attestation and produce a warning, so verify every matching flow. The Loth flow records `Discount` as `real` on two inputs and `string` on the third; its referenced CSVs are unavailable and it declares no final union type. Tableau documents automatic type compatibility for unions but warns the chosen type can be wrong, so no cast is inferred. Native M refresh and value parity have not been established. `notInner` combines both unmatched sides only on this guarded path; ambiguous projections and unproven semantics remain blocked. See the [SimpleJoin and union contract](TABLEAU_PREP_TO_POWERQUERY_REFERENCE.md#envelopes-and-shape-variants-reports-85-87). |
| **Prep #81 incomplete clean dialect coverage** | Bounded producer-backed paths cover `Remap`, one string-exclusion `ValueFilter`, one numeric/null-exclusion `RangeFilter`, and exact `QuickCalcColumn` `TrimSpaces`/`RemoveLetters` forms. `RemoveLetters` uses pinned Unicode 16.0 ranges and fails closed on other Unicode versions. `CleanOperation removeletters` reuses that filter, but its payload shape lacks an accepted producer fixture; `removenumbers` and `removepunctuation` are rejected rather than approximated with `Text.Clean`. N1 remains partial; unsupported `Rich*Filter` and other unobserved payloads remain blocked. Static tests validate accepted shapes; native refresh/value parity remains `not_run`. `groupMethodProps` is provenance, not fuzzy execution. Existing #82/#83 safeguards are preserved. See the [Prep dialect evidence](TABLEAU_PREP_TO_POWERQUERY_REFERENCE.md#clean-node-dialect-evidence-status). |
| **M identifier quoting** | ✅ FIXED — Column names with special characters (hyphens, slashes, parentheses, etc.) are now auto-quoted as `[#"Name"]` in M expressions. Previously, names like `Sub-Category` caused "Invalid identifier" errors |
| **Custom SQL params** | ✅ IMPLEMENTED — `Value.NativeQuery()` with parameter record binding and `[EnableFolding=true]` |
| **Hyper data** | Retained packaged extracts require complete staged CSV/Parquet sources and exact physical-identity Import bindings, not sample/empty M `#table()` substitutes. An eligible explicit native database is selected before Hyper reading/export; remaining Hyper consumers keep all strict checks. Metadata/header fallback remains diagnostic only; it cannot satisfy a full PBIP migration. File counts/hashes and bindings are checked independently of native loaded-row and value parity. See the [Hyper contract](FAQ.md#how-are-hyper-extract-files-handled). |
| **Calendar bounds** | Default source-derived bounds may reread independent source M copies, so source errors propagate. `--calendar-mode fixed` avoids those Calendar reads with explicit bounds or 2020–2030 defaults; it does not repair other source queries. Fixed-range coverage, including future dates, requires separate verification and is disclosed in annotations/static diagnostics. |
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
