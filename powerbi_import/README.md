# powerbi_import — Power BI Generation

Generates complete Power BI projects (`.pbip`) from extracted Tableau data.

For normal use, run the [local browser UI](../docs/LOCAL_WEB_UI.md) or the
batch CLI from the repository root:

```powershell
python migrate.py batch .\workbooks --output-dir .\output
```

Even one workbook uses a source folder. The internal importer below is a
developer entry point for already-extracted data, not an alternative public
single-file migration command.

## Core Modules

### `import_to_powerbi.py`

Main orchestrator. Reads extracted JSON and generates the `.pbip` project.

```bash
python powerbi_import/import_to_powerbi.py
```

### `pbip_generator.py`

Generates the complete `.pbip` file structure:

- `.pbip` file (entry point)
- SemanticModel (TMDL via `tmdl_generator.py`)
- Report (PBIR v4.0: `report.json`, `pages.json`, `visual.json`)
- `.platform` files and metadata

### `tmdl_generator.py`

Converts extracted data into TMDL (Tabular Model Definition Language) files:

- Automatic per-table storage: Hyper snapshots and import-only connectors use
  Import; supported native sources without Hyper use DirectQuery. Users can
  always force Import with `--mode import`. See the
  [mode/export decision diagram](../docs/ARCHITECTURE.md#automatic-storage-mode-and-hyper-export).
- Non-Hyper Import generates the connection, schema and M without native row
  export. Orchestration copies an explicitly packaged Excel workbook unchanged
  into `Data/` and supplies its absolute path before generation; loose native
  files and packaged non-Excel files retain original bindings without copying.
  This exception is in trusted local per-workbook batch orchestration, not
  uploaded jobs or shared-model/add-to-model/remove-from-model workflows.
  Power BI loads native rows at refresh. Hyper requires complete CSV/Parquet
  exports and exact Import bindings; capped/sample-only exports and unbound
  required files block PBIP success. Static export/binding checks do not prove
  native loading or saved state. Fabric retains its separate staging contract.
- `database.tmdl` — compatibility
- `model.tmdl` — culture, data source version
- `relationships.tmdl` — relationships between tables
- `tables/{Table}.tmdl` — physical columns, calculated columns, measures, M partitions

### `visual_generator.py`

Generates JSON visual definitions for the report. The runtime mapping inventory
has 164 records: 129 native, 16 approximations and 19 custom-visual mappings.
See [the mapping reference](../docs/MAPPING_REFERENCE.md); custom mappings
require target-tenant availability, and mapping counts do not establish parity.

### `m_query_generator.py`

Generates Power Query M queries for the different data source types.

### `validator.py`

Validates `.pbip` projects (JSON, TMDL, report structure) before opening in PBI Desktop.

### `migration_report.py`

Per-item fidelity tracking and migration status reporting.

### `api_server.py`

REST upload API server (stdlib `http.server`). `POST /migrate/batch` accepts a
ZIP containing one or more Tableau files; `POST /migrate` is retired (HTTP 410).
Status/download routes include `GET /batch/{id}`, `GET /status/{id}`,
`GET /download/{id}`, `GET /health` and `GET /jobs`.
This is distinct from the loopback-only [local browser runner](../web/local_server.py),
which accepts trusted local folder paths, not uploads. See the
[deployment guide](../docs/DEPLOYMENT_GUIDE.md) and
[upload security boundary](../UPLOAD_SECURITY.md).

### `schema_drift.py`

Schema drift detection — compares extraction snapshots. Detects added/removed/changed tables, columns, calculations, worksheets, relationships, parameters, filters.

## Deployment Subpackage (`deploy/`)

Fabric deployment is in the `deploy/` subpackage:

| Module | Responsibility |
|--------|---------------|
| `auth.py` | Azure AD authentication (Service Principal + Managed Identity) |
| `client.py` | Fabric REST API client with retry logic |
| `deployer.py` | Fabric deployment orchestrator |
| `utils.py` | DeploymentReport, ArtifactCache |
| `config/settings.py` | Centralized config via env vars |
| `config/environments.py` | Per-environment configs (dev/staging/production) |

## Output Format

**PBIR v4.0** with TMDL. Schema and theme constants are defined in
[`pbip_generator.py`](pbip_generator.py). The current base-theme name is
`CY26SU04`; report version at import is `5.58`. These metadata values do not
certify a minimum Power BI Desktop version or successful loading. Validate
generated projects separately with a compatible Desktop installation.

| Artifact | Schema | Version |
|----------|--------|--------|
| report.json | `report/2.0.0` | 2.0.0 |
| page.json | `page/2.1.0` | 2.1.0 |
| visual.json | `visualContainer/2.7.0` | 2.7.0 |
| bookmark.json | `bookmark/2.1.0` | 2.1.0 |
| pages.json | `pagesMetadata/1.0.0` | 1.0.0 |
| definition.pbir | `definitionProperties/2.0.0` | PBIR v4.0 |
| .platform | `platformProperties/2.0.0` | 2.0.0 |
| .pbip | `pbipProperties/1.0.0` | 1.0.0 |
