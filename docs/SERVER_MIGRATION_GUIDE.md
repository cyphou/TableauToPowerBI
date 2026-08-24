# Server-to-Service Migration Guide

End-to-end guide for migrating a Tableau Server/Cloud site to Power BI Service.

## Overview

The enterprise server migration pipeline extends the workbook-level migration with
site-wide discovery, dependency-aware wave planning, permission mapping, subscription
migration, and cutover orchestration.

```
┌──────────────────────────────────────────────────────────────────────┐
│  Phase 1: Discover    →  Phase 2: Plan     →  Phase 3: Migrate     │
│  --server-discover       --plan-migration     --server-batch        │
│                                                                      │
│  Phase 4: Permissions →  Phase 5: Subs     →  Phase 6: Cutover     │
│  --map-permissions       --migrate-subs        --cutover             │
└──────────────────────────────────────────────────────────────────────┘
```

## Prerequisites

- Python 3.12+
- Tableau Server/Cloud with REST API access
- Personal Access Token (PAT) — create in Tableau Server → Settings → Personal Access Tokens

```bash
export TABLEAU_TOKEN_SECRET="your-token-secret"
```

## Phase 1: Site Topology Discovery

Discover all workbooks, datasources, users, groups, and their dependencies:

```bash
python migrate.py \
  --server https://tableau.company.com \
  --token-name my-pat \
  --server-discover \
  --output-dir artifacts/enterprise_migration
```

**Outputs:**
- `topology.json` — full site inventory
- `dependency_graph.json` — workbook ↔ datasource dependency graph
- `topology_report.html` — interactive HTML dashboard

The discovery phase identifies:
- **Published datasource dependencies** — which workbooks share datasources
- **Usage patterns** — view counts, last-accessed dates
- **Certification status** — certified vs draft content
- **Project structure** — folder hierarchy for workspace mapping

## Phase 2: Migration Planning

Generate a dependency-aware migration plan with wave assignments and effort estimates:

```bash
python migrate.py \
  --server https://tableau.company.com \
  --token-name my-pat \
  --plan-migration \
  --team-size 3 \
  --wave-max-size 8 \
  --workspace-mapping by_project \
  --output-dir artifacts/enterprise_migration
```

**Options:**
| Flag | Description | Default |
|------|-------------|---------|
| `--team-size N` | Number of migration engineers | 1 |
| `--wave-max-size N` | Max workbooks per wave | 10 |
| `--workspace-mapping` | `by_project`, `consolidated`, or `flat` | `by_project` |

**Outputs:**
- `migration_plan.json` — machine-readable plan
- `migration_plan.html` — interactive dashboard with timeline

**Wave assignment rules:**
1. Workbooks sharing a published datasource are grouped in the same wave
2. Simpler workbooks migrate first (lower complexity score)
3. Effort is estimated per workbook (visuals, measures, connectors, RLS)

## Phase 3: Batch Migration

Download and migrate all workbooks from a project:

```bash
python migrate.py \
  --server https://tableau.company.com \
  --token-name my-pat \
  --server-batch Marketing \
  --server-assets all \
  --resolve-published-ds \
  --ds-cache-dir /tmp/ds_cache \
  --output-dir artifacts/marketing_migration
```

### Published Datasource Resolution

When workbooks reference published datasources (sqlproxy connections), use
`--resolve-published-ds` to download and inline the actual connection details:

```bash
python migrate.py \
  --server https://tableau.company.com \
  --token-name my-pat \
  --server-batch Sales \
  --resolve-published-ds \
  --ds-cache-dir artifacts/ds_cache
```

**Caching options:**
| Flag | Description |
|------|-------------|
| `--ds-cache-dir DIR` | Directory to cache downloaded datasource definitions |
| `--no-ds-cache` | Skip reading cache (still writes) — forces re-download |
| `--clear-cache` | Clear the datasource cache and exit |

The cache enables **offline migration**: resolve datasources once while connected,
then migrate workbooks later without server access.

## Phase 4: Permission Mapping

Map Tableau site roles to PBI workspace roles and generate Azure AD provisioning scripts:

```bash
python migrate.py \
  --server https://tableau.company.com \
  --token-name my-pat \
  --map-permissions \
  --output-dir artifacts/enterprise_migration
```

**Outputs:**
- `provision_azure_ad_groups.ps1` — PowerShell script to create Azure AD security groups
- `permission_report.html` — HTML report with role assignments and unresolved principals

**Role mapping:**
| Tableau Role | PBI Workspace Role |
|-------------|-------------------|
| Creator | Admin |
| Explorer | Member |
| ExplorerCanPublish | Contributor |
| Viewer | Viewer |
| ServerAdministrator | Admin |
| SiteAdministratorCreator | Admin |

## Phase 5: Subscription & Alert Migration

Migrate Tableau Server subscriptions and data-driven alerts to PBI equivalents:

```bash
python migrate.py \
  --server https://tableau.company.com \
  --token-name my-pat \
  --migrate-subscriptions \
  --output-dir artifacts/enterprise_migration
```

**Outputs:**
- `pbi_subscriptions.json` — PBI subscription configurations
- `power_automate_flows.json` — Power Automate flow definitions for complex scenarios
- `subscription_report.html` — migration report with conflict detection

**What gets migrated:**
- Email subscriptions → PBI email subscriptions
- Scheduled deliveries → PBI subscription schedules
- Data-driven alerts → PBI alert rules
- Custom notifications → Power Automate flow templates

## Phase 6: Cutover

Execute a controlled cutover from Tableau to Power BI:

```bash
# Generate cutover plan only (review before executing)
python migrate.py \
  --server https://tableau.company.com \
  --token-name my-pat \
  --cutover-plan-only \
  --output-dir artifacts/enterprise_migration

# Execute cutover
python migrate.py \
  --server https://tableau.company.com \
  --token-name my-pat \
  --cutover \
  --output-dir artifacts/enterprise_migration

# Roll back if needed
python migrate.py --cutover-rollback artifacts/enterprise_migration/snapshots/20250101_120000
```

**Cutover process:**
1. **Snapshot** — capture current Tableau state (workbooks, datasources, configs)
2. **Deploy** — push PBI artifacts to Power BI Service
3. **Validate** — compare Tableau vs PBI outputs
4. **Switch** — redirect users (manual step)

**Parallel run** — validate PBI outputs against Tableau before switching:

```bash
python migrate.py \
  --server https://tableau.company.com \
  --token-name my-pat \
  --parallel-run \
  --output-dir artifacts/enterprise_migration
```

## Full Pipeline Example

Complete enterprise migration in one script:

```bash
#!/bin/bash
SERVER="https://tableau.company.com"
TOKEN="my-pat"
OUT="artifacts/enterprise"

# 1. Discover
python migrate.py --server $SERVER --token-name $TOKEN \
  --server-discover --output-dir $OUT

# 2. Plan
python migrate.py --server $SERVER --token-name $TOKEN \
  --plan-migration --team-size 3 --wave-max-size 8 --output-dir $OUT

# 3. Migrate (batch)
python migrate.py --server $SERVER --token-name $TOKEN \
  --server-batch All --server-assets all \
  --resolve-published-ds --ds-cache-dir $OUT/ds_cache \
  --output-dir $OUT

# 4. Permissions
python migrate.py --server $SERVER --token-name $TOKEN \
  --map-permissions --output-dir $OUT

# 5. Subscriptions
python migrate.py --server $SERVER --token-name $TOKEN \
  --migrate-subscriptions --output-dir $OUT

# 6. Cutover (plan first, then execute)
python migrate.py --server $SERVER --token-name $TOKEN \
  --cutover-plan-only --output-dir $OUT

# Review the plan, then:
python migrate.py --server $SERVER --token-name $TOKEN \
  --cutover --output-dir $OUT
```

## Troubleshooting

### Published datasource not found
Ensure the PAT has `Explorer (can publish)` or `Creator` site role to access
published datasources. Use `--ds-cache-dir` to cache resolved datasources for
offline re-runs.

### Permission mapping shows unresolved users
Tableau users without email addresses cannot be automatically mapped to Azure AD.
Review the `permission_report.html` and manually add UPNs.

### Cutover rollback
Snapshots are stored in `{output_dir}/snapshots/` with timestamps.
Use `--cutover-rollback {snapshot_path}` to restore the previous state.

### Rate limiting
For large sites (1000+ workbooks), the tool respects Tableau Server rate limits.
If you encounter 429 errors, add `--parallel 1` to serialize API calls.
