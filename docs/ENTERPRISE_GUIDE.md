<p align="center">
  <img src="images/banner.svg" alt="Tableau to Power BI Migration Tool" width="960"/>
</p>

# Enterprise Deployment Guide

Step-by-step guide for migrating Tableau workbooks to Power BI at enterprise scale (100–500+ workbooks).

## Overview

```
Discovery → Assessment → Wave Planning → Pilot → Batch Migration → Validation → Deployment → Revalidation
```

## Phase 1: Discovery

1. **Inventory**: Use Tableau Server REST API to list all workbooks
   ```bash
   # Download all asset types (workbooks + flows + datasources), preserving folder structure
   python migrate.py --server https://tableau.example.com \
     --token-name pat \
     --server-batch ALL --server-assets all --server-preserve-folders \
     --output-dir /tmp/inventory

   # Or download only workbooks (default behavior)
   python migrate.py --server https://tableau.example.com \
     --token-name pat \
     --server-batch ALL --output-dir /tmp/inventory
   ```

   Set `TABLEAU_TOKEN_SECRET` in the environment using your secret-management
   mechanism before running these commands; never put the secret in command history.

2. **Global Assessment**: Analyze all workbooks for overlap and complexity
   ```bash
   python migrate.py --global-assess --batch /path/to/workbooks/
   ```
   This generates:
   - Pairwise merge score matrix (N×N heatmap)
   - Merge clusters (shared data patterns)
   - Per-workbook complexity scores

## Phase 2: Assessment

1. **Bulk Assessment** (standalone — no Tableau Server required): Full portfolio assessment on a local folder of workbooks and prep flows
   ```bash
   python migrate.py --bulk-assess /path/to/workbooks/ --output-dir /tmp/assessment
   ```
   This combines three analyses in a single command:
   - **Portfolio readiness**: per-workbook GREEN/YELLOW/RED scoring, effort estimation, wave planning
   - **Cross-workbook merge analysis**: pairwise overlap scoring, merge clusters (same as `--global-assess`)
   - **Prep flow lineage**: cross-flow dependency graph and merge recommendations (if `.tfl`/`.tflx` files are present)

   Output: HTML dashboards + JSON reports in the output directory.

2. **Server Assessment**: Score each workbook RED/YELLOW/GREEN (requires Tableau Server)
   ```bash
   python migrate.py --batch /path/to/workbooks/ --assess
   ```

3. **Strategy Advisor**: Get Import/DirectQuery/Composite recommendations per workbook
   ```bash
   python migrate.py assess workbook.twbx
   ```

4. **Governance Report**: Executive summary for leadership
   - Total workbooks, migration waves, estimated effort
   - Risk matrix, recommended sequence

## Phase 3: Wave Planning

Workbooks are automatically grouped into 3 waves:

| Wave | Criteria | Typical Effort |
|------|----------|----------------|
| Easy (quick wins) | GREEN, low complexity | 1–4 hours each |
| Medium (standard) | YELLOW, moderate complexity | 4–12 hours each |
| Complex (manual review) | RED, high complexity | 12–40+ hours each |

**Resource Allocation** (based on `plan_resource_allocation()`):
- Easy wave: All visual designers (no DAX/M expertise needed)
- Medium wave: 1 DAX expert + visual designers
- Complex wave: 1 DAX expert + 1 M expert + visual designers

## Phase 4: Pilot Migration

1. Select 3–5 representative workbooks from Easy wave
2. Migrate with full validation:
   ```bash
   python migrate.py batch /pilot/pilot1/ --output-dir artifacts/pilot/ --verbose
   ```
3. Open in Power BI Desktop, verify visuals
4. Deploy the generated PBIP project to a test workspace separately using the
   MCP `deploy` tool: preview first, then set `dry_run=false` and `confirm=true`.

## Phase 5: Batch Migration

```bash
# Sequential (safe)
python migrate.py --batch /workbooks/ --output-dir /output/ --verbose

# Parallel (faster, for 100+ workbooks)
python migrate.py batch /workbooks/ --output-dir /output/ --workers 4

# With resume (restart-safe)
python migrate.py batch /workbooks/ --output-dir /output/ --workers 4 --resume
```

### Shared Semantic Model

For workbooks sharing data, consolidate into a shared model:
```bash
python migrate.py --shared-model wb1.twbx wb2.twbx --model-name "Shared Sales"
```

### Composite Model

For large tables, use composite mode:
```bash
python migrate.py batch /one_workbook/ --mode composite --composite-threshold 10 --agg-tables auto
```

### Tableau Prep Flow Migration

If your organization has standalone Prep flows (.tfl/.tflx), migrate them separately — they produce Power Query M expressions, source definitions, and cross-flow lineage (not `.pbip` projects):

```bash
# Batch export all Prep flows — produces M queries, sources & lineage
python migrate.py batch /prep_flows/ --output-dir /output/prep/

# Cross-flow lineage analysis
python migrate.py lineage /prep_flows/

# Pair a prep flow with a workbook for merged .pbip output
python migrate.py batch /one_workbook/ --prep flow.tfl
```

The cross-flow lineage report identifies chain dependencies, merge candidates, and data provenance across all flows in your portfolio.

## Phase 6: Validation

```bash
# Validate all generated projects
python migrate.py --batch /output/ --check-schema

# Side-by-side comparison report (Tableau vs Power BI) — on by default
python migrate.py batch /one_workbook/ --check-schema
```

## Phase 7: Deployment

### Deploy Generated Projects

Batch generation does not forward deployment, refresh, or gateway-binding
flags. Use the MCP `deploy` tool for generated PBIP projects (preview first;
actual writes require `dry_run=false` and `confirm=true`). See
[DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) for Fabric-native deployment.

### Bundle Deployment (Shared Model + Thin Reports)
```bash
python migrate.py --shared-model wb1.twbx wb2.twbx \
  --deploy-bundle WORKSPACE_ID --bundle-refresh
```

### Multi-Tenant
```bash
python migrate.py --shared-model wb1.twbx wb2.twbx \
  --multi-tenant tenants.json
```

### REST API (Docker)

For headless/programmatic migrations, use the REST API server:
```bash
docker build -t tableau-to-pbi .
# POST a ZIP of one or more Tableau files via /migrate/batch
```

The Docker server binds beyond loopback and requires an API key. Configure
startup authentication and bearer headers as described in
[DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md).

`POST /migrate/batch` accepts multipart field `file` containing a ZIP with one
or more `.twb`, `.twbx`, `.tds`, or `.tdsx` files. `GET /batch/{id}` reports
aggregate progress; `GET /status/{id}` and `/download/{id}` address individual
jobs. `POST /migrate` returns HTTP 410.

## Phase 8: Revalidation

Regenerate batch output after updating the source workbooks:
```bash
python migrate.py batch /workbooks/ --output-dir /output/
```

`--sync` is retired. Batch mode does not detect Server `updatedAt` changes,
perform incremental synchronization, or auto-deploy. Review the new quality
reports and explicitly authorize any subsequent deployment.

### Schema Drift Detection

Compare a workbook against a previously saved extraction baseline:
```bash
python migrate.py workbook.twbx --check-drift /path/to/snapshot_dir
```

Detects added/removed/changed tables, columns, calculations, worksheets, relationships, parameters, and filters. Saves JSON drift report and updated baseline.

## Performance Guidelines

| Scale | Workers | Expected Time | RAM |
|-------|---------|--------------|-----|
| 10 workbooks | 1 | ~2 min | <200MB |
| 50 workbooks | 2 | ~5 min | <300MB |
| 100 workbooks | 4 | ~8 min | <500MB |
| 500 workbooks | 4–8 | ~30 min | <1GB |

## Monitoring

Enable telemetry for migration observability:
```bash
export TTPBI_TELEMETRY=1
python migrate.py --batch /workbooks/ --output-dir /output/
```

View the telemetry dashboard:
```python
from powerbi_import.telemetry_dashboard import generate_dashboard
generate_dashboard(log_path='~/.ttpbi_telemetry.json', output='dashboard.html')
```
