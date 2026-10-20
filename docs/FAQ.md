# FAQ — Tableau to Power BI Migration

## General

### Which Tableau files are supported?

`.twb` (XML workbooks) and `.twbx` (packaged workbooks with data). `.tds`/`.tdsx` (datasources) are also supported by the extractor.

### What is a `.pbip` file?

It is a **Power BI Project** — a text-based file format (JSON + TMDL) that represents a complete Power BI report. It opens directly in Power BI Desktop by double-clicking.

### Do I need to install Python dependencies?

No. The core migration uses only the Python standard library (xml, json, os, uuid, re, etc.).

Optional dependencies for advanced features:
- `tableauhyperapi` — for reading `.hyper` extract files (v2+ format). Without this, some newer `.hyper` files may only get metadata (no row data).
- `azure-identity` + `requests` — for Fabric workspace deployment
- `pydantic-settings` — for typed configuration (falls back to env vars)

### How are `.hyper` extract files handled?

When a `.twbx` contains embedded `.hyper` files, the migration automatically:

1. **Extracts** the `.hyper` file from the TWBX archive
2. **Reads** column metadata and row data using a 3-tier reader chain:
   - **Tier 1:** `tableauhyperapi` (pip package) — handles 100% of `.hyper` formats including v2+
   - **Tier 2:** SQLite fallback — works for older `.hyper` files that are SQLite-compatible
   - **Tier 3:** Binary header scan — extracts CREATE TABLE/INSERT patterns for metadata-only
3. **Inlines** small tables (≤500 rows by default) into M `#table()` expressions
4. **Converts** large tables to CSV files with `Csv.Document()` M expressions
5. **Patches** TMDL partition expressions to reference the generated CSV files

Use `--hyper-rows N` to control the inline/CSV threshold.

For Hyper data staged as CSV, physical columns are typed at the M `Source`
step immediately after header promotion and before derived calculations.
Column types come from extraction metadata, with model `sourceColumn`/`dataType`
as a fallback, and use explicit `en-US` culture for invariant CSV values. This
was verified in Power BI on World Indicators: schema and data refresh confirmed
numeric types and successful `AVERAGE` queries, and the Business scatter was
populated without an aggregate error. This evidence applies to that workbook;
it does not establish a general Desktop or all-workbook pass.

### What about `.tde` files (legacy Tableau Data Extract)?

`.tde` is the **pre-2018 legacy extract format**. It cannot be read by `tableauhyperapi`, SQLite, or the binary scanner. Tables from `.tde`-based workbooks get a placeholder `#table()` partition with a `TODO` comment. To migrate the data, either:
- Re-save the workbook in Tableau Desktop (which converts `.tde` → `.hyper`)
- Manually export the data to CSV and update the M expression

## Migration

### Why was an Excel source reported missing even though it was inside the TWBX?

Packaged files must be available before semantic source validation. Migration
now stages exact declared archive members into the project's `Data` folder
before generation, using the same content-conflict checks as loose sources.
It does not search by basename or replace a missing member with an unrelated
file. Regenerate an affected project; merely copying the file into an older
output does not replace an already-generated unavailable-source partition.

### Why did a date-based calculated column fail on every row in Power Query?

DAX permits numeric day offsets such as `NOW() - 45`; Power Query does not
subtract a number directly from a datetime. Supported, proven date expressions
with numeric literal offsets now use `Date.AddDays`. Bare boolean literals also
become M `true` and `false`, without changing strings or field names. Uncertain
date arithmetic remains in DAX instead of being pushed into invalid M. These
conversion checks do not replace a native Desktop refresh.

### Why did a calculation referencing `Number of Records` produce invalid DAX?

A calculation without an explicit caption may repeat its bracketed Tableau
identifier as its extracted caption. Semantic-context collection now removes
only that fallback identifier's outer bracket pair, consistently across
constant values, measure names and calculation references. This allows a
literal `Number of Records = 1` calculation to be inlined in row expressions.
Different explicit captions, including accents and bracket characters, remain
unchanged. DAX containing diagnostic comments is not pushed into Power Query;
the diagnostic remains visible rather than becoming invalid M arithmetic.
Regenerate affected projects to apply these generator changes. Successful
generation still does not establish source availability or Desktop parity.

### Does successful extraction mean the migrated report is verified?

No. Extraction, project generation, source availability, Desktop refresh and
rendered-page comparison are separate checks. An extraction snapshot can be
valid while migration is blocked by an unresolved source reference or an
unsupported logical relationship.

Desktop row counts are observations, not proof of an error-free refresh. The
probe checks visible owned refresh state before and after observing rows, and
again after render settling. Errors, unfinished refresh/pending changes or
unavailable UI inspection prevent refresh/screenshot verification; loaded row
counts remain available separately for diagnosis. A benign incomplete/no-data
banner alone is not classified as a refresh error.

Worksheet and dashboard extraction uses direct workbook collection members,
not similarly named nested metadata references. Minimal XML without those
collections may use direct workbook children. Duplicate names genuinely present
in the source remain present; they are not silently deduplicated.

Tableau bins may contain a literal size or a validated size-parameter reference.
The extracted calculation can therefore differ from a raw field-reference
formula while preserving the explicit bin definition. Logical relationship
metadata and the active physical join tree must also be inspected separately;
the legacy equality-only relationship list is not a complete relationship audit.

Compare a fresh snapshot against its exact source revision. Object counts alone
do not validate formulas, predicates, formats, values or rendered behavior.

### How do I run a migration?

```bash
python migrate.py batch ./workbooks --output-dir ./output
```

Migration generation is batch-only. To migrate one workbook, place it in a
folder by itself. Assessment and diagnostic commands can still target a
workbook file. For example:

```bash
# Batch migration with verbose logging
python migrate.py batch ./workbooks --output-dir /tmp/output --verbose

# Pair a Prep flow with a workbook in a one-workbook source folder
python migrate.py batch ./one_workbook --prep ./flows/flow.tfl --output-dir /tmp/output

# Standalone Prep flow batch — export Power Query M, sources & lineage
python migrate.py batch examples/prep_portfolio/ --output-dir /tmp/prep_output

# Prep flow lineage analysis
python migrate.py lineage examples/prep_portfolio/
```

### Where is the output?

In `artifacts/powerbi_projects/[ReportName]/[ReportName].pbip`. Double-click to open in Power BI Desktop.

### How are standalone Tableau Prep flows (.tfl/.tflx) handled?

Standalone prep flows are **not** converted to `.pbip` projects (which would be empty with no visuals). Instead, `--batch` on prep flow directories produces:

- **Power Query M** — `.pq` files for each flow output table
- **Source definitions** — JSON files with connection metadata and column schema
- **Assessment** — per-flow grade (GREEN/YELLOW/RED) and stats
- **Cross-flow lineage** — automatic HTML + JSON report when ≥2 flows succeed, showing dependencies and merge recommendations

To pair a Prep flow with a workbook (merge M transforms into a `.pbip`), put
the workbook in a source folder (the folder may contain only that workbook):
```bash
python migrate.py batch ./one_workbook --prep flow.tfl --output-dir ./output
```

### Why do some formulas not work?

Some Tableau functions have no direct DAX equivalent:

- `MAKEPOINT()` — no DAX equivalent; use lat/lon columns in a map visual
- `PREVIOUS_VALUE()` — automatically converted to OFFSET-based DAX pattern (may need manual adjustment for complex seed logic)
- `LOOKUP()` — automatically converted to OFFSET-based DAX pattern
- Table functions (`SIZE()` → `COUNTROWS(ALLSELECTED())`, `INDEX()` → `RANKX()`) — approximated, may need adjustment

Most complex patterns **are** handled automatically:
- LOD expressions (`{ FIXED ... }`) → `CALCULATE` + `ALLEXCEPT` / `REMOVEFILTERS` / `ALL`
- `SUM(IF ...)` → `SUMX('table', IF(...))` iterator conversion
- Nested `IF/ELSEIF/ELSE/END` → nested `IF()` calls
- Window functions (`WINDOW_AVG`, `WINDOW_SUM`) → `CALCULATE(..., ALL('table'))`
- Table calculations (`RUNNING_SUM`, `RANK`) → `CALCULATE(SUM(...))`, `RANKX(ALL(...))`

### How are LOD expressions converted?

LOD (Level of Detail) expressions are one of the most complex Tableau features.
The tool converts supported patterns automatically; source filter order,
relationship grain and undefined ratios still require source-value and runtime checks:

```
{FIXED [Region] : SUM([Sales])}
→ CALCULATE(SUM('Table'[Sales]), ALLEXCEPT('Table', 'Table'[Region]))

{FIXED [Region], [Category] : SUM([Sales])}
→ CALCULATE(SUM(...), ALLEXCEPT('Table', 'Table'[Region], 'Table'[Category]))

{EXCLUDE [Category] : SUM([Sales])}
→ CALCULATE(SUM(...), REMOVEFILTERS('Table'[Category]))

{FIXED : SUM(IF YEAR([Date]) = YEAR(TODAY()) THEN [Amount] ELSE 0 END)}
→ CALCULATE(SUMX('Table', IF(YEAR(...) = YEAR(TODAY()), ...)), ALL('Table'))
```

### How does the SUM(IF) → SUMX conversion work?

In Tableau, `SUM(IF condition THEN value ELSE 0 END)` is common. DAX's `SUM()` only accepts a single column, so the tool converts to iterator functions:

```
Tableau: SUM(IF [type] = "Revenue" THEN [amount] ELSE 0 END)
DAX:     SUMX('transactions', IF('transactions'[type] = "Revenue", 'transactions'[amount], 0))
```

This applies to all aggregate+condition patterns:
- `SUM(IF ...)` → `SUMX`
- `AVG(IF ...)` → `AVERAGEX`
- `MIN(IF ...)` → `MINX`
- `MAX(IF ...)` → `MAXX`
- `COUNT(IF ...)` → `COUNTX`

### How is Row-Level Security (RLS) migrated?

Tableau has multiple security mechanisms, all converted to Power BI RLS roles:

1. **User filters** (`<user-filter>` with user→value mappings):
   ```tmdl
   role 'Region Access'
       tablePermission Orders
           filterExpression = (USERPRINCIPALNAME() = "alice@example.com" && [Region] IN {"East", "West"}) || ...
   ```

2. **USERNAME() / FULLNAME() calculations**:
   ```tmdl
   role 'Is Current User'
       tablePermission Orders
           filterExpression = 'Orders'[Email] = USERPRINCIPALNAME()
   ```

3. **ISMEMBEROF("group")** — creates a separate role per group:
   ```tmdl
   role Managers
       tablePermission Orders
           filterExpression = TRUE()  /* Assign Azure AD group members to this role */
   ```

### What about parameters?

Tableau parameters are converted to Power BI What-If parameter tables:

- **Integer/real range** → `GENERATESERIES(min, max, step)` table + `SELECTEDVALUE` measure
- **String list** → `DATATABLE("Value", STRING, {{"val1"}, {"val2"}})` table + `SELECTEDVALUE` measure
- **Date parameters** → static measure with default value

When a calculated column references a parameter, the value is **inlined** (since calc columns can't reference measures in DAX).

### How do I generate Fabric-native output?

Use `--output-format fabric` to generate Lakehouse, Dataflow Gen2, PySpark Notebook, DirectLake Semantic Model, and Data Pipeline artifacts instead of a .pbip project:

```bash
python migrate.py fabric ./one_workbook --output-dir /tmp/fabric_output
```

### How do I optimize the generated DAX formulas?

Use `--optimize-dax` to run an AST-based DAX optimizer pass after conversion. This rewrites verbose formulas for readability and performance:

```bash
python migrate.py batch ./one_workbook --optimize-dax --time-intelligence auto
```

The optimizer applies: nested IF→SWITCH, IF(ISBLANK)→COALESCE, redundant CALCULATE collapse, constant folding, SUMX simplification, and optional Time Intelligence auto-injection (YTD, PY, YoY%).

### How do I reconfigure the data sources?

1. Open the `.pbip` in Power BI Desktop
2. Go to Power Query Editor (Transform Data)
3. Edit the source parameters in each query
4. Close and Apply

### Why did I sometimes see a stray `Ae`/odd glyph in textbox content after migration?

Tableau can emit internal line-break sentinel runs in rich text XML. In older builds those sentinel runs could appear as literal glyph artifacts in generated Power BI textboxes. This is fixed by cleaning sentinel-only runs during extraction while preserving actual paragraph breaks.

If you still see this behavior, regenerate with the latest version and attach a minimal `.twb` snippet in your issue.

### Is visual positioning pixel-perfect?

Direct zone-to-visual sizing and deterministic layout fixtures provide bounded
positioning checks, not a guarantee of rendered Tableau equivalence.
Floating/overlapping zones are resolved deterministically: the report-side
overlap healer sorts overlapping visuals by z-order, keeps the lowest-z backdrop
anchored, and staggers higher-z foreground zones by +32 px. The fixture drift
gate (`scripts/generate_pixel_fixtures.py --check`) checks its recorded cases;
it does not certify every workbook, visual type, color or interaction.

The fixtures cover four presentation surfaces within their recorded cases:

| Axis | What is preserved |
|------|-------------------|
| **Fonts** | Run-level font family, size, weight, color, per-paragraph horizontal alignment |
| **Chrome** | Per-visual background + border from Tableau format zones |
| **Sentinel** | Tableau soft line-break sentinel runs (`Ae`/NBSP) cleaned during extraction |
| **Overlay** | Floating/overlapping zones staggered deterministically by z-order |

### What quality reports does batch migration produce?

```bash
python migrate.py qa ./one_workbook
```

`one_workbook` is a source folder (it can contain just one workbook). The `qa`
shortcut follows the batch migration path. Batch mode writes a unified quality
JSON/HTML report and an evidence ZIP for each successful workbook by default;
it does not run the separate six-check QA suite. The legacy `--qa` and
`--qa-strict` flags are not available through the supported batch-only workflow.

## Technical

### Why do I get "Invalid identifier" errors in Power Query M?

Column names with special characters (hyphens, slashes, parentheses) must be quoted as `[#"Name"]` in Power Query M. For example, `[Sub-Category]` is invalid — M interprets the hyphen as a minus operator. The correct syntax is `[#"Sub-Category"]`.

The migration tool auto-detects and quotes these identifiers. Characters that trigger quoting: `/ ( ) ' " + @ # $ % ^ & * ! ~ \` < > ? ; : { } | \\ , -`.

If you see this error after migration, re-run with the latest version.

### What is the difference between a measure and a calculated column?

- **Measure**: computed at aggregation time (e.g., `SUM`, `AVERAGE`). Adapts to the filter context.
- **Calculated column**: computed row by row, stored in the model. Like an added column.

The tool classifies automatically:
- Tableau `role=measure` → DAX measure
- Tableau `role=dimension` → calculated column

### Why does `RELATED()` appear in some formulas?

In calculated columns, to access a column from another related table, DAX requires `RELATED('OtherTable'[column])`. The tool adds `RELATED()` automatically when the column belongs to a different table.

### What is the TMDL format?

**Tabular Model Definition Language** — a text format for describing Power BI semantic models. It is the successor to the `model.bim` JSON, used in `.pbip` projects.

### How do relationships work?

Supported single-key equality relationships can become Power BI relationships:
- `LEFT JOIN` → `toColumn` with `crossFilteringBehavior: oneDirection`
- `FULL OUTER JOIN` → `crossFilteringBehavior: bothDirections`
- Join columns become the relationship keys

Composite or range logical predicates cannot be replaced by a single equality
key. A constrained calculation path preserves their pair grain with explicit
datasource-scoped policies; unsupported cases stop generation. See
[logical-relationship policy requirements](KNOWN_LIMITATIONS.md#composite-and-range-logical-relationships).

## Validation & Deployment

### Why does my project fail the openability gate?

Every PBIP migration runs a static openability preflight by default. It checks project
structure, JSON parsing, TMDL presence, TMDL partitions, Power Query (M) validity, DAX,
report/model references, report structure, and PBIR schema — without launching Power BI
Desktop. If it reports `openable=false`, read the listed blocking issues; the most common
cause is an invalid generated M partition. Fix the source generator, not the emitted
`.tmdl`. Re-run with `--no-verify-open` only to bypass the gate deliberately for diagnosis.

To attempt automatic recovery before opening, run `--autoheal` (deterministic DAX/M/visual
healing). Add `--llm-autofix` to allow opt-in LLM correction, which is applied only when the
result re-validates cleanly. For a best-effort real Desktop smoke test, use the explicit
`--desktop-probe`. The `--verify-open` verdict applies only to its static checks;
it does not override a Desktop load or refresh failure.

### Can `openable=true` still fail when data is refreshed?

Yes. `openable=true` means the static checks found no blocking issue, not that
Desktop loaded or refreshed the model successfully. The serialized report
identifies its static scope and leaves refresh verification as `not_run`.
Inspect native refresh evidence separately from table row-count observations.

In bug report #84, removing relationships allowed the supplied test variant to
refresh, while adding null rows or typing empty tables did not prevent the
original `FromColumn` deleted-object error. These are reported observations,
not an independently reproduced cause. A static endpoint/schema check cannot
prove that Desktop will preserve column identities while reconciling queries.
Do not remove relationships, add fake rows, or change cardinality merely to
obtain a passing verdict. The exact reproduction project and native relation
bissection are needed to establish a causal fix.

### How do I validate a client report opening and its screenshot?

Run the batch probe against the generated output directory:

```powershell
python scripts/probe_projects.py C:\Client\migration_output `
   --shots C:\Client\migration_output\opening_validation\screenshots `
   --json C:\Client\migration_output\opening_validation\opening_report.json `
   --html C:\Client\migration_output\DESKTOP_OPENING_VALIDATION.html
```

The default probe waits for the report window, presses **Home > Refresh >
Schema and data**, checks aggregate row counts in Desktop's local model, and
labels each screenshot *data verified* or *data not verified*. Read `DESKTOP_OPENING_VALIDATION.html` for the green `OPENED` and red
`CRASHED` cards. `empty` means Desktop opened but the model returned zero rows;
`unavailable` or `query_failed` means the data check could not complete. Use
`--skip-data-check` only for diagnostic captures, never as a successful data
validation. Store all client screenshots and reports outside this repository.

### Why did strict thin-report validation block my shared model?

`--strict-thin-report` fails shared-model generation when orphaned thin-report field
references exceed `--thin-report-max-orphans` (default `0`). Orphaned fields are report
fields that do not resolve to a column or measure in the merged semantic model. Either map
them via the field mapping, raise the threshold, or remove `--strict-thin-report` to proceed
with warnings instead of a hard failure.

### How do I validate generated projects?

Use the built-in `ArtifactValidator` to check project integrity before opening in Power BI Desktop:

```python
from powerbi_import.validator import ArtifactValidator

result = ArtifactValidator.validate_project("artifacts/powerbi_projects/MyReport")
print(result)  # {"valid": True, "files_checked": 15, "errors": []}

# Batch validate all projects
results = ArtifactValidator.validate_directory("artifacts/powerbi_projects/")
```

The validator checks: `.pbip` JSON, Report directory (report.json, pages, visuals), SemanticModel directory (model.tmdl starts with `model Model`, table TMDLs).

### How do I deploy to Microsoft Fabric?

1. Install optional dependencies: `pip install azure-identity requests`
2. Set environment variables:
   ```bash
   export FABRIC_WORKSPACE_ID="your-workspace-guid"
   export FABRIC_TENANT_ID="your-tenant-guid"
   export FABRIC_CLIENT_ID="your-app-client-id"
   export FABRIC_CLIENT_SECRET="your-app-secret"
   ```
3. Deploy:
   ```python
   from powerbi_import.deployer import FabricDeployer
   deployer = FabricDeployer(workspace_id='your-workspace-guid')
   deployer.deploy_artifacts_batch('artifacts/powerbi_projects/')
   ```

Service Principal and Managed Identity authentication are both supported.

### How do I batch-migrate multiple workbooks?

```bash
python migrate.py --batch /path/to/tableau/files/ --output-dir /tmp/output
```

This processes all `.twb` and `.twbx` files in the directory and generates separate .pbip projects for each.

### How do I run a full portfolio assessment without migrating?

Use `--bulk-assess` to get readiness scoring, merge analysis, and prep flow lineage in one command — no Tableau Server connection required:

```bash
python migrate.py --bulk-assess /path/to/workbooks/ --output-dir /tmp/assessment
```

This recursively discovers `.twb`/`.twbx` workbooks and `.tfl`/`.tflx` prep flows, then generates:
- **Portfolio readiness report** — per-workbook GREEN/YELLOW/RED grading, effort estimation, migration wave plan
- **Cross-workbook merge analysis** — pairwise overlap heatmap, merge clusters (equivalent to `--global-assess`)
- **Prep flow lineage** — cross-flow dependency graph and merge recommendations (when ≥2 flows are found)

All reports are saved as HTML dashboards + JSON to the output directory (defaults to `artifacts/bulk_assess/`).

### How do I merge multiple workbooks into a shared semantic model?

Use `--shared-model` to merge workbooks that share the same data sources into one Power BI semantic model with thin reports:

```bash
# Pre-assessment (see merge score before generating)
python migrate.py --shared-model wb1.twbx wb2.twbx --assess-merge

# Generate shared model + thin reports
python migrate.py --shared-model wb1.twbx wb2.twbx --model-name "Sales Model"

# Batch all workbooks in a directory
python migrate.py --batch /path/to/workbooks/ --shared-model

# Force merge even with low overlap score
python migrate.py --shared-model wb1.twbx wb2.twbx --force-merge
```

The merge engine uses fingerprint-based table matching — tables are matched by `connection_type|server|database|table_name`, so workbooks connecting to the same physical database tables are automatically detected as merge candidates.

**Merge scores:**
- **≥60** — MERGE RECOMMENDED: high table overlap, few conflicts
- **30–59** — PARTIAL: some overlap but significant differences
- **<30** — KEEP SEPARATE: different data sources, use `--force-merge` to override

### What is a thin report?

A thin report is a Power BI report (`.pbip`) that doesn’t contain its own semantic model. Instead, it references a shared semantic model via a `byPath` pointer in `definition.pbir`:

```json
{
  "datasetReference": {
    "byPath": {
      "path": "../SharedModel.SemanticModel"
    }
  }
}
```

This allows multiple reports to share one data model — changes to the model (tables, measures, relationships) are reflected in all thin reports automatically.

### How are measure conflicts resolved during merge?

- **Identical measures** (same name + same DAX formula) → deduplicated (kept once)
- **Conflicting measures** (same name, different formula) → namespaced as `MeasureName (WorkbookName)` in the shared model
- **Unique measures** (name only in one workbook) → kept as-is

The `merge_assessment.json` file lists all conflicts detected.

### How do I run the tests?

```bash
python -m pytest tests/ -v
```

The historical full-suite baseline completed with **10,798 passed, 66 skipped, and 1 xfailed** from 10,865 collected tests across 316 files; see CI for current results. Coverage includes DAX conversion, Power Query M generation, TMDL model building, visual generation, project structure, artifact validation, deployment utilities, Fabric-native generation, DAX optimization, cross-platform equivalence testing, and end-to-end non-regression migration.

### Does the preceptor automatically send fixes to agents?

Not automatically. The CLI runs preceptor scoring by default and writes
`preceptor_report.json`; it does not invoke Copilot agents or apply coaching.
With MCP configured, an owner can call `quality_report` and then
`agent_handoff` with its own name in the same server session to pull only its
findings. The packet is read-only. After a real artifact change, rerun
`quality_report` and call `agent_handoff_ack` with `outcome="applied"`; the ACK
is accepted only if PBIP definition files changed and the finding disappeared.
An owner can instead acknowledge `not_applicable` with a rationale. Accepted
ACKs live only in the MCP process (`persisted: false`); they do not trigger an
agent or persist a resolution history. `--no-preceptor` skips scoring;
`--preceptor-block` opts into a hard failure after escalation. The legacy
specialized QA suite is not available through the supported batch-only CLI.

### How do I run the migration as a REST API?

Use the built-in API server:

```bash
python -m powerbi_import.api_server --port 8000
# Or via Docker:
docker build -t tableau-to-pbi .
docker run -p 8000:8000 tableau-to-pbi
```

Endpoints: `POST /migrate/batch` (upload a ZIP containing Tableau files),
`GET /batch/{id}`, `GET /status/{id}`, `GET /download/{id}`, `GET /health`,
and `GET /jobs`. The retired `POST /migrate` endpoint returns HTTP 410.

### How do I detect schema drift between migrations?

Use `--check-drift` to compare the current extraction against a saved snapshot:

```bash
# First migration creates a baseline snapshot
python migrate.py workbook.twbx --check-drift /path/to/snapshot_dir
# ... time passes, Tableau workbook changes ...
# Re-run to detect drift
python migrate.py workbook.twbx --check-drift /path/to/snapshot_dir
```

The report lists added/removed/modified tables, columns, calculations, worksheets, relationships, parameters, and filters.
