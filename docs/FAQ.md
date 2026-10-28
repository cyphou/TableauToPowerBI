# FAQ — Tableau to Power BI Migration

[🏠 Overview](../README.md) · [📚 Documentation](README.md) · [🌐 Local browser](LOCAL_WEB_UI.md) · [✅ Review checklist](MIGRATION_CHECKLIST.md)

## General

### How do I open the local browser interface?

From the repository root:

```powershell
.\run_web_ui.ps1
# Or use Python directly:
python web\local_server.py
```

Open `http://127.0.0.1:8765/`, enter separate local source/output folder
paths, and keep the terminal open. Ctrl+C stops the server; closing a tab does
not stop a job. This page needs no account, cloud hosting, upload or frontend
build. The standalone Windows app/EXE has been removed; the PowerShell script
only launches the website. Native Power BI/Tableau diagnostics are separate,
explicitly confirmed actions. See the [local browser guide](LOCAL_WEB_UI.md).

### Which Tableau files are supported?

`.twb` (XML workbooks) and `.twbx` (packaged workbooks with data). `.tds`/`.tdsx` (datasources) are also supported by the extractor.

### What is a `.pbip` file?

It is the entry point of a **Power BI Project**, whose sibling folders hold the
report definition and semantic model as editable JSON and TMDL. Open it from a
supported Power BI Desktop installation; double-clicking also works when the
file association is configured. Generation and static validation do not
guarantee native loading, refresh or visual parity. See the
[project opening and setup guide](POWERBI_PROJECT_GUIDE.md).

### Do I need to install Python dependencies?

No. The core migration uses only the Python standard library (xml, json, os, uuid, re, etc.).

Optional dependencies for advanced features:
- `tableauhyperapi` — for reading `.hyper` extract files (v2+ format). Without this, some newer `.hyper` files may only get metadata (no row data).
- `azure-identity` + `requests` — for Fabric workspace deployment
- `pydantic-settings` — for typed configuration (falls back to env vars)

### How are `.hyper` extract files handled?

Source selection happens **before Hyper data reading**. If the same datasource
has a concrete original database connection with complete, unambiguous native
table/column lineage, that database is preferred and its Hyper is not read or
exported. Credentials are configured later, in Power BI. A missing endpoint or
ambiguous native binding fails explicitly instead of guessing or silently
retrying another source. Excel/CSV-backed extracts and unresolved published
`sqlproxy` references do not qualify as database bindings.

Retained packaged Hyper tables keep their exact workbook, archive member, schema and
table identity. PBIP generation exports all rows present in the snapshot and
requires each migrated table's Import partition to read its matching staged
CSV/Parquet file. It does not select a file by similar columns or filenames,
or switch sources after an export failure.

The reader chain uses optional `tableauhyperapi`, then SQLite for compatible
files, then a bounded header scan for metadata. PBIP full export requires a
complete reader; header/sample rows cannot substitute for it. Missing full
reader support, unknown/inconsistent source and export counts, incomplete
exports or unbound required tables are errors, not successful partial imports.
Archive copies and CSV export
use bounded buffers; CSV rewrites use a separate temporary file before replacing their destination,
including when the original CSV and destination have the same name.

Use these batch options for large extracts:

| Option | Purpose |
|--------|---------|
| `--max-zip-entry-mb N` | Positive per-member uncompressed limit in MiB; explicit option overrides `TTPBI_MAX_ZIP_ENTRY_MB`, then the 1024 MiB default |
| `--mode auto` | After source selection: retained Hyper means Import; selected native databases use DirectQuery when supported, otherwise Import |
| `--mode import` | Force Import for the selected source; a database selection stays on that database, without native row export. Explicitly packaged Excel is copied unchanged into `Data/` and bound by absolute path; retained Hyper still exports its data |
| `--hyper-export-format auto` | PBIP: Hyper strictly larger than 10 MiB requires native Parquet; at most 10 MiB becomes CSV. Fabric stays CSV |
| `--hyper-export-format csv` | Explicit CSV export |
| `--hyper-export-format parquet` | Require native Parquet support; unavailable prerequisites are errors, not silent CSV substitution |
| `--hyper-export-max-rows N` | Retained for limited diagnostic/Fabric staging uses; PBIP Hyper migrations reject a resolved cap, including `TTPBI_HYPER_EXPORT_MAX_ROWS` |

For example, generate complete Hyper-backed projects with a larger archive
member limit, without setting a row cap:

```powershell
python migrate.py batch .\workbooks --output-dir .\output `
    --max-zip-entry-mb 4096 --hyper-export-format auto
```

A cap inherited from the environment is still a cap: remove it before a full
PBIP Hyper migration. Limited exports made through other diagnostic/staging
paths remain samples, with no guaranteed subset or stable order from `LIMIT`.
The PBIP gate checks file integrity and the final Import bindings as well as
row counts and schemas. Exported files alone do not prove that Power BI
loaded their data; native refresh and saved/reopened model checks are separate.

On Windows, native Parquet is written through a private ASCII transit path
then copied by Python to the requested output path, preserving accents in the
final project location. If no writable staging path satisfies the job's
security boundary, export stops with an explicit error rather than writing
outside that boundary or silently changing format.

`--hyper-rows` is a separate legacy metadata/inline-sample option. It is not
an export row cap and is not forwarded by batch generation.

The decision is deliberately source-local: **eligible original database first;
otherwise retain the Hyper snapshot**. A supported selected native database
connector uses DirectQuery. Excel, CSV and other import-only
connectors cannot use DirectQuery and remain Import with an explanation.
This also applies to the native [MySQL connector](https://learn.microsoft.com/en-us/power-query/connectors/mysql-database#supported-capabilities),
whose supported storage capability is Import, not DirectQuery.
Select **Import** in the local browser or pass `--mode import` to override
automatic DirectQuery. See the
[decision diagram](ARCHITECTURE.md#automatic-storage-mode-and-hyper-export).

The quality report's Hyper Import section lists the retained source and the
unused source in a **Source decision** table, using datasource/connector labels
only, never endpoints or credentials. A deliberately unused snapshot needs no
export proof; remaining Hyper consumers keep every existing strict check.
There is no new CLI switch: this source selection is the default. See
[Active extract behind a live connection](KNOWN_LIMITATIONS.md#active-extract-behind-a-live-connection)
for eligibility and snapshot-parity limitations.

For non-Hyper sources, **Import does not mean extracting their data during
migration**. The PBIP contains the datasource connection, schema and Power Query
M; Power BI loads the rows on refresh. One native-file copy is supported: an
Excel workbook explicitly packaged in a TWBX is copied byte-for-byte, unchanged,
into the generated `Data/` folder and bound by its absolute path. No rows are
parsed or exported during that copy. Loose native files and packaged non-Excel
files are not copied into the generated project.
For loose workbooks, relative file bindings resolve against the original
workbook directory; absolute bindings stay unchanged. Power Query must be able
to access that location. Other native files inside a TWBX are not copied or
rebound to guessed loose files: configure an accessible external source when
needed. Native CSV parsing determines the full physical width before selecting
the exact declared source columns by name, rather than truncating the input to
the model's column count. Selection precedes type and caption transformations:
an unmodeled physical field cannot collide with a caption rename, while a
still-unresolved declared source field raises an error.

For declared-schema Excel, CSV, Google Sheets, SharePoint, PDF and Azure Blob
queries that promote headers, generated M reserves exact source-header matches
first, including names with intentional leading or trailing whitespace. Only
an otherwise missing declared name may match a unique unclaimed header after
Power Query's `Text.Trim` removes its outer whitespace. Case and internal spaces
remain significant. Missing or ambiguous matches raise explicit errors instead
of inventing suffixes or substituting null columns. This is not global header
trimming: exact physical names and their caption bindings remain unchanged.
The helper does not read or rewrite source files. Wildcard/no-schema queries,
explicit promote-header transformations, database queries and Hyper bindings
retain their existing behavior.

When a header binding fails, Desktop raises `HeaderSchema.Error` or
`HeaderMatch.Error` and may display only the message, for example "Source headers
must uniquely match the declared source column names." The opt-in
`--screenshots` / `--compare-desktop` probe classifies these complete texts as
refresh errors: it stops without waiting out the refresh timeout, and the
screenshot is never labelled data verified, even if stale row counts exist.
Resolve the ambiguous or duplicate source headers, then refresh again; the
migration never edits source files.

Explicitly declared physical columns are also retained when the sole source
table has a partial nested schema; their declared types and caption mappings
are preserved instead of creating calculated placeholders. Tableau pseudo-fields
are excluded from that physical schema. Declarations without proven ownership
are not assigned to an arbitrary table in a multi-table source.
Metadata-record ownership remains authoritative. When an individual field
lacks that ownership, an explicit `[local]` to `[Table].[physical]` mapping in
the datasource connection's `<cols>` can supply its parent table. Partial
metadata does not disable this fallback for other fields. Conflicting or
malformed maps produce warnings, not a guessed join side; mappings from nested
extract or object scopes are not borrowed for this fallback. This establishes
table ownership, not a blanket guarantee of physical-column rename recovery.
Metadata extraction from the Tableau workbook still takes place. Retained Hyper
is the exception: its data are exported to CSV/Parquet and used by the Import partition.

The size test uses the uncompressed Hyper file: strictly more than
`10 * 1024 * 1024` bytes chooses Parquet; exactly that size still chooses CSV.
The existing optional Hyper API is required when Parquet is selected.
A SQLite-compatible input is not native Hyper: above the PBIP threshold it
fails explicitly rather than silently using CSV. Select
`--hyper-export-format csv` explicitly if that compatibility path is intended.
Fabric-native auto staging remains CSV; explicit Fabric Parquet is unsupported.
A generated database connection requires credentials and possibly a gateway;
generation does not contact it or prove that it refreshes.

CSV exports use `Csv.Document()` with header promotion and physical scalar
typing. Parquet exports use `Parquet.Document()` without CSV header promotion.
Native Parquet export uses the already optional Hyper API; it does not add a
core dependency on pandas, pantab or pyarrow.

Hyper geography columns are cast to WKT text for native Parquet export because
Parquet cannot store Hyper's geography type. Names, order and nulls are preserved;
other columns keep their native types. Other unsupported types still fail
explicitly, without dropping columns or falling back to CSV. CSV serialization
is unchanged and can contain the SDK's bytes representation for geography.
Exporting a geometry as text does not certify spatial rendering in Power BI.

Long CSV exports report their table, written row count and rate, with a
percentage and ETA only when a total is available. Intermediate display
updates are throttled to at most one per two seconds. Terminal output can
update in place; redirected/browser logs use flushed newline records.
Native Parquet export has truthful start/completion messages, not an invented
row-by-row percentage.

For Hyper data staged as CSV, physical columns are typed at the M `Source`
step immediately after header promotion and before derived calculations.
Physical scalar type metadata is matched by exact Hyper identity before the
physical-to-logical rename, so aliasing does not make date, logical or numeric
values fall back to text. Column types come from extraction metadata, with model
`sourceColumn`/`dataType` as a fallback, and use explicit `en-US` culture for
invariant CSV values.

Earlier Power BI evidence for World Indicators recorded numeric types,
successful `AVERAGE` queries, and a populated Business scatter without an
aggregate error. A separate pre-fix refresh loaded 2,691 rows but reported 345
row errors in generated nullable `IF` conditions. These are historical,
workbook-specific observations, not a general Desktop/all-workbook result.

Converted DAX `IF` expressions now generate M that compares the complete
condition with `true` before taking the `then` branch. A `null` condition
therefore selects the existing `else` branch; nested `IF` conditions use the
same recursive conversion. No exception-catching or type coercion is added.

Separate authorized, owned-window UI checks subsequently confirmed refreshes
for two formerly failing fixtures: each physical partition's `RefreshedTime`
advanced, the owned UI reported `Clear`, and verified row counts matched
expected values without row errors. The standard probe's refresh state remained
`waiting` because it missed the UI refresh cycle; that probe did not itself
observe these successful refreshes. This scoped evidence is not a general
Desktop, rendered-output, or parity certification.

### What about `.tde` files (legacy Tableau Data Extract)?

`.tde` is the **pre-2018 legacy extract format**. It cannot be read by `tableauhyperapi`, SQLite, or the binary scanner. Tables from `.tde`-based workbooks get a placeholder `#table()` partition with a `TODO` comment. To migrate the data, either:
- Re-save the workbook in Tableau Desktop (which converts `.tde` → `.hyper`)
- Manually export the data to CSV and update the M expression

## Migration

### How is an Excel workbook inside a TWBX bound for refresh?

For trusted local per-workbook batch generation, regenerate older output to
receive the packaged-Excel correction. The exact
declared Excel resource is copied unchanged into the project's `Data/` folder,
and Power Query references that copy's absolute path. A similarly named file
beside the TWBX is not substituted, because it can contain different data.
This copies the original workbook binary, not a CSV/Parquet export or row sample.

Moving or renaming the project requires updating the absolute source path.
Service refresh needs a gateway or other supported access to that file.
The copy includes every sheet and the workbook metadata; treat output and ZIP
downloads as containing source data and do not commit them. Copying does not
prove that sheets/columns exist or that Desktop refresh succeeds.

Packaged CSV and other non-Excel native files still require accessible external
bindings. Hyper data are exported separately as CSV or Parquet.
Uploaded jobs and shared-model/add-to-model/remove-from-model workflows do not
use this Excel-copy path; Fabric keeps its separate staging behavior.

### Why did a date-based calculated column fail on every row in Power Query?

DAX permits numeric day offsets such as `NOW() - 45`; Power Query does not
subtract a number directly from a datetime. Supported, proven date expressions
with numeric literal offsets now use `Date.AddDays`. Bare boolean literals also
become M `true` and `false`, without changing strings or field names. Uncertain
date arithmetic remains in DAX instead of being pushed into invalid M. These
conversion checks do not replace a native Desktop refresh.

Date arithmetic is converted by operand type, from the declared types of the
table's columns and of earlier calculated columns. Subtracting two dates, such
as `[Ship Date] - [Order Date]`, gives a `duration` in Power Query, which a
numeric column cannot store, so the table could fail to load. A difference of
two dates is now `Duration.Days(...)`; a difference involving a datetime is
`Duration.TotalDays(...)`, which keeps the fraction, with a date operand
promoted by `DateTime.From`. The same typing applies when the difference sits
inside a larger expression (`([End] - [Start]) * 24`), a comparison
(`[End] - [Start] > 30`), an `IF` condition, or `YEAR`, `ABS` and `ROUND`. A
date or datetime plus or minus a whole-number literal (`[Start] + 7`) uses
`Date.AddDays`, `TODAY()` and `NOW()` follow the same rules, and a date
compared with a datetime is promoted to a datetime first because M refuses the
mixed comparison. `DATEDIFF` with a day, month, quarter or year unit stays in
M; hour, minute, second and week units stay in DAX, because DAX counts interval
boundaries while an M duration measures elapsed time.

Whatever the declared types do not prove stays in DAX rather than becoming
invalid M: a fractional offset on a date column, an offset that is not a
literal, `*`, `/` or `&` on a date, a date plus a date, and `INT(...)` of a
difference. A column name is never taken as proof of a date. The conversion
assumes that a column's Power Query type equals its declared Tableau type, so a
column that the source returns with another type is not covered. Generated-M
checks do not replace a native Desktop refresh. Regenerate affected projects to
apply this change.

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
banner alone is neither a refresh error nor evidence of success. A newly
generated metadata-only PBIP may show both incomplete/no-data and calculated
objects needing refresh. Configure source access, run **Home > Refresh >
Schema and data**, inspect any errors, then save the project after successful
processing so Power BI can retain its local cache. The migration itself does
not preload native rows or hide these notices.

A native `.shp` is not JSON. PBIP generation leaves an explicit
`SourceBinding.Error` requesting a supported GeoJSON binding instead of sending
the binary file to `Json.Document` and producing a misleading buffer error.
It does not convert or copy native spatial data.

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

For batch generation, `--output-dir` is the output root; without it the root
is `<source-folder>\migrated`. Each workbook has its own project directory,
and a path-budgeted `P_<hash>` identity may replace a long workbook name.
Use the reported output path rather than assuming a fixed filename. Opening
the `.pbip` in Power BI Desktop is a separate validation step.

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

For separate deterministic DAX/M/visual recovery, use the MCP `autoheal` tool
and review the resulting repair evidence. The parser's legacy `--autoheal` and
`--llm-autofix` switches are not forwarded by batch generation. Native diagnostics
can be requested in the same migration command with `--screenshots` (also
`--desktop-probe`) or `--compare-desktop`; without these options, no apps open.
The local browser's separately confirmed diagnostics and
`python scripts\probe_projects.py PROJECTS_DIR` remain alternatives.
The `--verify-open` verdict applies only to its static checks; it does not
override a Desktop load or refresh failure.

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

Use one ordinary migration command, with the desired optional diagnostic:

```powershell
# Migration + Power BI opening, verified refresh and screenshots.
python migrate.py batch C:\Client\Tableau --output-dir C:\Client\migration_output --screenshots

# Migration + Tableau / Power BI captures, with selected-page verification.
python migrate.py batch C:\Client\Tableau --output-dir C:\Client\migration_output `
   --compare-desktop --all-pages --page-timeout 90
```

The two desktop modes are mutually exclusive. They require an interactive
Windows session and installed apps. Default captures show opening windows;
`--all-pages` attempts individual pages and requires selection checks before
and after capture. Failed/inaccessible pages remain explicit, with no parity
claim. Only current successful/resumed workbook outputs are captured, and only
applications launched by the diagnostic are closed. There is no automatic
Desktop save, credential/license acceptance or deployment.
See the [one-command guide](DEPLOYMENT_GUIDE.md#migration-and-native-diagnostics-in-one-command)
for timeouts, executable overrides and evidence locations.

For already generated output, the standalone batch probe avoids rerunning migration:

```powershell
python scripts/probe_projects.py C:\Client\migration_output `
   --shots C:\Client\migration_output\opening_validation\screenshots `
   --json C:\Client\migration_output\opening_validation\opening_report.json `
   --html C:\Client\migration_output\DESKTOP_OPENING_VALIDATION.html
```

The default probe waits for the report window, presses **Home > Refresh >
Schema and data**, verifies completion separately from aggregate model rows, and
labels each screenshot *data verified* or *data not verified*. Completion needs
an observed refresh lifecycle or strictly advanced expected M-partition timestamps
with ready/error-free partitions, plus a clear owned UI and complete table checks.
Rows alone do not prove refresh. Read `DESKTOP_OPENING_VALIDATION.html` for the green `OPENED` and red
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

See [CI](https://github.com/cyphou/TableauToPowerBI/actions) for current outcomes,
the [test guide](../tests/README.md) for focused commands, and the
[roadmap](ROADMAP.md) for dated full-suite measurements. Historical totals are
not a fresh test run or live Desktop/Fabric evidence.

### Does the preceptor automatically send fixes to agents?

No. Supported batch and shared-model CLI generation do not invoke the Python
preceptor or write `preceptor_report.json`; their dispatches do not forward
the parser's preceptor switches. A separately invoked preceptor scores
artifacts but does not invoke Copilot agents or apply coaching.
With MCP configured, an owner can call `quality_report` and then
`agent_handoff` with its own name in the same server session to pull only its
findings. The packet is read-only. After a real artifact change, rerun
`quality_report` and call `agent_handoff_ack` with `outcome="applied"`; the ACK
is accepted only if PBIP definition files changed and the finding disappeared.
An owner can instead acknowledge `not_applicable` with a rationale. Accepted
ACKs live only in the MCP process (`persisted: false`); they do not trigger an
agent or persist a resolution history. `--no-preceptor` and
`--preceptor-block` do not establish a preceptor gate for batch/shared-model
generation. The legacy
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
This upload service is not the loopback folder-path browser runner. Review
the [upload security boundary](../UPLOAD_SECURITY.md) before hosting uploads;
application-level confinement is not an OS sandbox.

### How do I detect schema drift between migrations?

Use the schema-drift Python API to compare saved extraction snapshots.
The legacy `--check-drift` postprocessing option is not forwarded by supported
batch generation; a parsed flag does not prove a drift check ran:

```python
from powerbi_import.schema_drift import detect_schema_drift, load_snapshot

# Each directory contains the extracted JSON for one source revision.
previous = load_snapshot(r"snapshots\before")
current = load_snapshot(r"snapshots\after")
report = detect_schema_drift(current, previous)
print(report.summary())
```

The report lists added/removed/modified tables, columns, calculations, worksheets, relationships, parameters, and filters.
