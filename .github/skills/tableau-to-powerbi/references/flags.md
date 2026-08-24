# Flag catalogue (on-demand reference)

Deep-dive companion to the `tableau-to-powerbi` skill. Run
`python migrate.py --help` for the authoritative list; this file groups the most
useful flags by intent.

## Core

| Flag | Purpose |
|------|---------|
| `--output-dir DIR` | Where to write the `.pbip` (default `artifacts/`) |
| `--verbose` | Detailed pipeline logging |
| `--dry-run` | Parse/validate only, generate nothing |
| `--output-format {pbip,fabric}` | PBIR+TMDL (default) or Fabric-native artifacts |
| `--culture fr-FR` | Semantic-model locale |
| `--calendar-start / --calendar-end` | Calendar table year range |

## Assessment & QA

| Flag | Purpose |
|------|---------|
| `--assess` | Pre-migration readiness report (no output written) |
| `--qa` | Post-migration real-world QA report card |
| `--qa-strict` | Fail the run on any error-severity QA check |
| `--bulk-assess DIR` | Assess every workbook in a folder |

## Batch & shared model

| Flag | Purpose |
|------|---------|
| `--batch DIR` | Migrate every workbook (and Prep flow) in a folder |
| `--shared-model wb1 wb2 …` | Build one shared semantic model |
| `--assess-merge` | Score whether workbooks should merge |
| `--global-assess --batch DIR` | Pairwise merge scoring across a portfolio |

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
| `--deploy WORKSPACE_ID` | Deploy to Power BI / Fabric workspace |
| `--deploy-refresh` | Also configure scheduled refresh |
| `--gateway-bind GATEWAY_ID` | Bind dataset to a data gateway |
| `--create-workspace NAME` | Provision the workspace first |

Safety: deployment and server credentials are secrets. Never echo them into chat
or a question tool — have the user type them directly into the terminal.
