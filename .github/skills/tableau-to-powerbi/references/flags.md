# Flag catalogue (on-demand reference)

Deep-dive companion to the `tableau-to-powerbi` skill. Run
`python migrate.py --help` for the authoritative list; this file groups the most
useful flags by intent.

For new scripts, prefer the concise commands below and use this page only for
advanced options. Legacy flags remain supported for backward compatibility.

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
| `--output-dir DIR` | Where to write generated output (default `artifacts/`) |
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
| `--quality-strict` | Fail the run when the combined report has blockers |
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
| `--token-name / --token-secret` | PAT auth — **type the secret in the terminal**, never in chat |

## Deployment

| Flag | Purpose |
|------|---------|
| `--deploy WORKSPACE_ID` | Legacy flag; batch does not dispatch deployment. Use separate deployment |
| `--deploy-refresh` | Legacy flag; batch does not dispatch refresh |
| `--gateway-bind GATEWAY_ID` | Bind dataset to a data gateway |
| `--create-workspace NAME` | Provision the workspace first |

Safety: deployment and server credentials are secrets. Never echo them into chat
or a question tool — have the user type them directly into the terminal.
