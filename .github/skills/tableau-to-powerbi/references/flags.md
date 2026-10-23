# Flag catalogue (on-demand reference)

Deep-dive companion to the `tableau-to-powerbi` skill. Run
`python migrate.py --help` for the authoritative list; this file groups the most
useful flags by intent.

For new scripts, prefer the concise commands below and use this page only for
advanced options. Legacy flags remain supported for backward compatibility.
Parser visibility is not proof that an option is forwarded by every dispatch
path. Batch/shared-model generation do not run the preceptor; unsupported
single-file postprocessing flags do not become batch features.

| Workflow | Preferred command |
|----------|-------------------|
| Generate PBIP from one or more workbooks | `python migrate.py batch FOLDER` |
| Readiness assessment | `python migrate.py assess WORKBOOK` |
| Unified post-migration validation | `python migrate.py quality WORKBOOK` |
| Feature parity scorecard | `python migrate.py parity WORKBOOK` |
| Portfolio assessment | `python migrate.py portfolio FOLDER` |
| Migration planning | `python migrate.py plan SERVER_URL` |
| Prep-flow lineage | `python migrate.py lineage FLOW_OR_FOLDER...` |
| Batch quality/evidence handoff | `python migrate.py package FOLDER` |

## Core

| Flag | Purpose |
|------|---------|
| `--output-dir DIR` | Batch output root; defaults to `<source-folder>\migrated` |
| `--verbose` | Detailed pipeline logging |
| `--dry-run` | Parse/validate only, generate nothing |
| `--output-format {pbip,fabric}` | PBIR+TMDL (default) or Fabric-native artifacts |
| `--culture fr-FR` | Semantic-model locale |
| `--calendar-start / --calendar-end` | Calendar table year range |

## Assessment & QA

| Flag | Purpose |
|------|---------|
| `--assess` | Pre-migration readiness report (no output written) |
| `--qa` | Legacy QA suite; unavailable through the supported batch-only workflow |
| `--qa-strict` | Legacy QA failure gate; unavailable through the supported batch-only workflow |
| `--quality-report` | Combined assessment, parity, data, interface, and openability report |
| `--quality-strict` | Gate the `quality WORKBOOK` diagnostic on blockers; not forwarded by batch generation |
| `--quality-policy {report,enterprise,production}` | Control escalation of static semantic diagnostics and unresolved lineage; default `report` |
| `--quality-ai` | Add an optional AI summary grounded in verified findings; never changes status |
| `--parity` | Feature parity scorecard |
| `--bulk-assess DIR` | Assess every workbook in a folder |

The `qa FOLDER` and `package FOLDER` shortcuts use batch migration. Batch mode
already writes unified quality reports and evidence ZIPs, but does not run the
specialized six-check QA suite or assessment report packager. The legacy flags
`--qa`, `--qa-strict`, and `--report-package` are not available through the
supported batch-only generation path; `--quality-report` is redundant for batch
output and is not forwarded.

## Batch & shared model

| Flag | Purpose |
|------|---------|
| `--batch DIR` | Migrate every workbook (and Prep flow) in a folder; use a one-workbook folder to migrate just one |
| `--shared-model wb1 wb2 …` | Build one shared semantic model |
| `--assess-merge` | Score whether workbooks should merge |
| `--global-assess --batch DIR` | Pairwise merge scoring across a portfolio |

## Large Hyper extracts

| Flag | Purpose |
|------|---------|
| `--max-zip-entry-mb N` | Positive per-entry uncompressed limit in MiB; explicit option > `TTPBI_MAX_ZIP_ENTRY_MB` > 1024 MiB |
| `--mode {auto,import,directquery,composite}` | Default `auto`: Hyper/import-only files use Import, capable native sources use DirectQuery; `import` overrides to Import. Non-Hyper Import migrates connection/schema/M only, not native rows; Hyper still exports data |
| `--hyper-export-format {auto,csv,parquet}` | PBIP `auto`: Hyper strictly > 10 MiB requires native Parquet, otherwise CSV; Fabric `auto` remains CSV |
| `--hyper-export-max-rows N` | Positive export cap; explicit option > `TTPBI_HYPER_EXPORT_MAX_ROWS`; omitted means unlimited |

Invalid limits are errors, not silent defaults. Metadata-only scans do not
establish full-data export. A capped export is a sample, with no guaranteed
subset/order from an unordered query, and must not be treated as full-data
parity. Hyper snapshots always use Import. Native
connection generation is not a refresh or credential test. Required Parquet
needs the optional Hyper API; missing support is an explicit prerequisite error.
SQLite-compatible inputs above the PBIP threshold fail rather than silently
choosing CSV; an explicit CSV override remains possible. Fabric Parquet is not
supported. Header-reader samples remain explicitly limited-data evidence.
Progress in redirected/browser logs uses newline records; percentage/ETA
require a known total. See the [Hyper guide](../../../../docs/FAQ.md#how-are-hyper-extract-files-handled).
The [decision diagram](../../../../docs/ARCHITECTURE.md#automatic-storage-mode-and-hyper-export)
separates storage mode from Hyper export format.
For PBIP native Import, source dataset files are not staged or copied into the
project. Keep or configure their original file bindings; Power BI retrieves the
rows at refresh. This does not disable Tableau metadata extraction or Hyper
CSV/Parquet exports. Fabric retains its separate staging contract.

## Performance baselines

The standalone scripts in `scripts/` keep performance measurement separate from
the migration CLI:

```bash
python scripts/run_perf_baseline.py --help
python scripts/compare_perf_baselines.py --current-dir current --previous-dir previous \
	--output-json reports/trend.json --output-md reports/trend.md
python scripts/compare_perf_baselines.py --current-dir current --previous-dir previous \
	--output-json reports/trend.json --output-md reports/trend.md --fail-on-regression
```

`--fail-on-regression` is opt-in and exits with status 1 when a lower-is-better
metric exceeds the configured threshold; report generation still occurs first.

## Tableau Server / Cloud

| Flag | Purpose |
|------|---------|
| `--server URL` | Tableau Server/Cloud base URL |
| `--workbook NAME` | Single workbook to download + migrate |
| `--server-batch PROJECT` | Migrate a whole project |
| `--token-name` / `TABLEAU_TOKEN_SECRET` | PAT name as an option; secret from the environment, never command history or chat |

## Deployment

| Flag | Purpose |
|------|---------|
| `--deploy WORKSPACE_ID` | Legacy flag; batch does not dispatch deployment. Use separate deployment |
| `--deploy-refresh` | Legacy flag; batch does not dispatch refresh |
| `--gateway-bind GATEWAY_ID` | Legacy option; batch does not dispatch gateway binding |
| `--create-workspace NAME` | Legacy option; batch does not dispatch workspace creation |

Safety: deployment and server credentials are secrets. Never echo them into chat
or a question tool — have the user type them directly into the terminal.
