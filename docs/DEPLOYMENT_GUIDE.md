# Deployment Guide — Fabric REST API

This guide covers deploying generated Power BI projects to Microsoft Fabric workspaces using the tool's built-in deployment pipeline.

---

## Prerequisites

1. **Microsoft Fabric workspace** with capacity assigned
2. **Azure AD App Registration** with the following API permissions:
   - `Power BI Service` → `Dataset.ReadWrite.All`
   - `Power BI Service` → `Report.ReadWrite.All`
   - `Power BI Service` → `Workspace.ReadWrite.All`
3. **Admin consent** granted for the above permissions
4. **Client secret** created for the app registration

## Environment Variables

Create a `.env` file (or set environment variables) based on `.env.example`:

```bash
# Required
FABRIC_WORKSPACE_ID=<your-workspace-guid>
FABRIC_TENANT_ID=<your-azure-ad-tenant-guid>
FABRIC_CLIENT_ID=<your-app-registration-client-id>
FABRIC_CLIENT_SECRET=<your-client-secret>

# Optional
FABRIC_API_BASE_URL=https://api.fabric.microsoft.com/v1
FABRIC_USE_MANAGED_IDENTITY=false
FABRIC_LOG_LEVEL=INFO
FABRIC_LOG_FORMAT=text
FABRIC_DEPLOYMENT_TIMEOUT=300
FABRIC_RETRY_ATTEMPTS=3
FABRIC_RETRY_DELAY=5
```

Fabric-native deployment uses the `FABRIC_*` identity above. PBIP deployment
uses `PBI_TENANT_ID`, `PBI_CLIENT_ID`, and `PBI_CLIENT_SECRET` (or
`PBI_ACCESS_TOKEN`) instead. Do not pass client secrets as command-line
arguments.

## Authentication Methods

### Service Principal (Recommended for CI/CD)

Set `FABRIC_TENANT_ID`, `FABRIC_CLIENT_ID`, and `FABRIC_CLIENT_SECRET`. The tool uses `azure-identity`'s `ClientSecretCredential`.

```bash
pip install azure-identity
```

### Managed Identity (For Azure-hosted runners)

Set `FABRIC_USE_MANAGED_IDENTITY=true`. Uses `DefaultAzureCredential` which automatically picks up managed identity credentials.

### No Authentication (Local Dev)

Skip the deployment step and open generated `.pbip` files directly in Power BI Desktop.

## Deployment Pipeline

### Manual Deployment

```bash
# Generate the six-item Fabric-native project for each workbook in a folder
python migrate.py fabric ./workbooks \
  --output-dir ./output
```

Batch generation does not dispatch `--deploy`, `--deploy-refresh`, or
gateway-binding flags. For generated PBIP projects, use the MCP `deploy` tool:
preview with its default `dry_run=true`, then explicitly authorize deployment
with `dry_run=false` and `confirm=true`. For Fabric-native projects, the
programmatic `FabricDeployer.deploy_fabric_project()` entry point is separate
from batch generation.

Before the first remote write, Fabric-native deployment validates the local six-artifact
bundle and every Notebook `Files/...` reference. It then performs a read-only
workspace preflight using `GET /workspaces/{id}` and
`GET /workspaces/{id}/items`. A failed preflight creates no Fabric items and
uploads no OneLake files.

Running the deployed Data Pipeline is a separate operation via
`FabricDeployer.run_fabric_pipeline()`. Do not treat local generation success as
deployment or refresh success. Power BI Desktop is never opened automatically.

## Client-side opening validation

Migration and Fabric deployment do not require Power BI Desktop. Use Desktop on
a Windows validation runner when real opening evidence and screenshots are
required. Configure the executable explicitly on client machines with a custom
installation path:

```powershell
$py = ".\.venv\Scripts\python.exe"
$env:POWERBI_DESKTOP_PATH = "C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe"
$output = "C:\Client\migration_output"

& $py migrate.py --batch "C:\Client\tableau_sources" `
  --output-dir $output --verify-open --quiet
& $py scripts/probe_projects.py $output `
  --shots "$output\opening_validation\screenshots" `
  --json "$output\opening_validation\opening_report.json" `
  --html "$output\DESKTOP_OPENING_VALIDATION.html"
```

The probe presses **Home > Refresh > Schema and data** for each report, waits
for rows in the local model, and labels each screenshot *data verified* or
*data not verified*. Data files embedded in a `.twbx` are exported to each
project's `Data/` folder. Files the workbook links instead are searched for, by
file name, in this order: the workbook's folder, its subfolders, its parent
folder, then the batch root. Matches are copied into `Data/` and `DataFolder`
points there. When several copies share a name but differ in content, none is
copied. Files that cannot be found must be placed in `Data/` before the refresh
can load them.

`POWERBI_DESKTOP_PATH` must point to an existing `PBIDesktop.exe`; an invalid
explicit path fails closed. Reports and screenshots remain beside the external
output directory. A screenshot made with `--skip-data-check` is diagnostic only
and is not evidence that the model contains rows.

### Paired Tableau / Power BI Desktop screenshots

> **Bulk/batch workflow:** generate from a source folder first, even for one
> workbook. A simple single-file load is not a generation mode; this screenshot
> diagnostic also accepts folders, not a single workbook or `.pbip` argument.

After batch generation, run the separate diagnostic
[`compare_desktop_reports.py`](../scripts/compare_desktop_reports.py) explicitly:

```powershell
$py = ".\.venv\Scripts\python.exe"
$src = "C:\Client\tableau_sources"
$output = "C:\Client\migration_output"
$env:TABLEAU_DESKTOP_PATH = "C:\Program Files\Tableau\Tableau 2026.2\bin\tableau.exe"
$env:POWERBI_DESKTOP_PATH = "C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe"

& $py scripts\compare_desktop_reports.py $src $output --timeout 90

# Opt in to individual view/page capture.
& $py scripts\compare_desktop_reports.py $src $output --all-pages --timeout 90 --page-timeout 30

# Optional local pixel metrics, without an AI endpoint (also works with --all-pages).
& $py scripts\compare_desktop_reports.py $src $output --screen-comparison local
```

Use the Tableau version installed on the validation runner. Both applications
must be installed in an interactive Windows session. Without an override, Tableau
discovery checks PATH and Program Files and selects the newest numeric version.
The options `--tableau-desktop-path` and `--powerbi-desktop-path` provide explicit
executable overrides; an invalid override fails rather than silently falling back.

The source argument is a folder, even for one workbook. The diagnostic recursively
pairs `.twb`/`.twbx` sources with generated PBIP projects using batch layout and
available quality-summary mappings. Missing or ambiguous pairs are reported, not
guessed. By default it opens each pair and captures a responsive document-specific
window without navigating pages. With `--all-pages`, it inventories individual
source views and target pages and attempts to select each through Windows UI
Automation. A page screenshot requires verified selection; an inaccessible tab,
ambiguous correspondence, or failed capture is reported explicitly rather than
counted as complete coverage. Tableau page navigation requires a uniquely named
selectable tab; unnamed tabs remain inaccessible and block per-page capture.
An inventory or successful run does not prove that every Tableau page was
captured. Neither mode refreshes Tableau or changes filters.
The Tableau probe waits, up to `--timeout`, for transient owned progress windows
whose title exactly matches English `Opening workbook '<name>'...`; it does not
dismiss other dialogs. A non-progress owned dialog is captured separately as
`<workbook-stem>-dialog.png` (`dialog_screenshot`) and leaves the Tableau result as
`error`. Power BI refreshes and verifies data. After capture, the comparison
closes only application processes it launched, not unrelated or existing instances.
Tableau forwarding to an existing
instance is rejected conservatively when the document cannot be attributed to
the launched process; close existing Tableau instances manually before retrying.

All-page scope:

- Tableau: dashboard tabs, standalone worksheet tabs, and story tabs. Worksheets
  embedded in dashboards are not captured separately. Individual story points
  are not iterated.
- Power BI: all PBIR pages, including hidden, tooltip, and unordered pages.
  Hidden pages are not unhidden; pages unavailable through the application's
  accessible tabs remain explicit capture failures.
- Correspondence uses exact names or explicit `lineage_map.json` page lineage
  beside the PBIP project. Duplicate or conflicting names remain ambiguous;
  unmatched views can be captured individually without being asserted equivalent.
- `--timeout` controls initial opening (90 seconds by default).
  `--page-timeout` bounds selection, capture, and selection verification per
  page (30 seconds by default). Selection is checked before and after capture.
  Both timeouts must be finite and positive.

Each run creates fresh evidence:

- `<project>\screenshots\runs\<run_id>\source\<workbook-stem>.png`
- `<project>\screenshots\runs\<run_id>\output\<workbook-stem>.png`
- `<output>\desktop_comparisons\<run_id>\manifest.json`
- `<output>\desktop_comparisons\<run_id>\comparison.html`

After a comparison run, existing migration quality HTML (including
`MIGRATION_QUALITY_CONSOLIDATED.html`) is refreshed with relative links to
available comparison reports. Regenerating quality HTML discovers those reports
again. The links show recorded capture evidence, not a parity verdict: failed
captures remain failures, and successful captures still require manual review.
No comparison links are emitted when no comparison HTML exists.

With `--all-pages`, the screenshot folders also contain indexed, sanitized
per-page PNG names with unique suffixes. The comparison directory contains a
`page-00001.html` review file for each correspondence row. The manifest includes
source/target inventories, per-page capture statuses, correspondence statuses,
and coverage totals. The all-pages `comparison.html` also displays any accepted,
existing initial Tableau and Power BI opening screenshots in a separately
labelled section as **page identity unverified**. These screenshots are opening
evidence only: they do not identify a page, are excluded from verified page
totals, and are never used for per-page pixel comparisons.

**Business-review presentation:** report and page selectors, Previous/Next
navigation, shorter messages, icon-enhanced cards, and light neutral styling.
Missing or failed captures stay explicit on the affected side; they are not
replaced by an opening screenshot or presented as a successful pair. Technical
diagnostics and inventories are collapsed by default, with full evidence
retained in the report and JSON manifest. Initial opening captures belong in a separate,
clearly labelled **page identity unverified** section, not in the page-pair
viewer.

This is a presentation change over recorded evidence, not a new capture run,
increased verified page coverage, or a parity claim. Generated-HTML tests and
browser DOM checks verify language switching and navigation state; they do not
establish pointer usability or visual rendering on every browser. Desktop
capture or runtime parity claims require separate authorized runtime evidence.

#### Report language: French / English

Screenshot comparisons (main and individual pages) and unified migration-quality
reports (individual and consolidated) include a **Français / English** selector.
The interface, document title, accessibility labels, authored status text and dynamic page
counter switch locally. The browser remembers the choice when local storage is
available; switching still works if storage is unavailable. Comparison reports
default to French, and quality reports default to English without a saved choice.

Translation uses authored strings embedded in the HTML, with no translation API
or external script. Workbook/page/field names, paths, code, raw diagnostics,
JSON evidence and screenshots remain unchanged. Shared report controls are
bilingual. Specialized shared-template reports also translate authored interface
text: artifact differences, cutover, dependencies, permissions, QA, recovery,
subscriptions, global assessment, merge assessment, Prep lineage and server
assessment. These specialized reports default to English when no saved browser
choice is available.

The selector translates authored interface text, not every report value or
diagnostic: embedded source and technical content retains its original language,
including in comparison and quality reports. Canonical machine-readable status
values remain unchanged. Standalone parity-registry, remediation,
migration-planner and merge-report HTML, plus notebook/PDF dashboards, are not
covered by this extension. Embedded localization does not remove existing
network dependencies: Prep-lineage diagrams still use the Mermaid CDN.
It also does not imply that Desktop refresh or deployment is offline.

Open `comparison.html` for side-by-side review. These are full application-window
captures of the initially opened sheet/page by default, or individually selected
views/pages with `--all-pages`, including different application chrome. They are
**not** matched dashboard crops, SSIM fidelity scores, or proof
that data loaded or all visuals rendered. The manifest's parity score stays
unset. For a meaningful comparison, prepare corresponding Tableau sheets and
Power BI pages, filters, and data manually; use the separate opening-validation
probe above when refresh and model-row evidence are needed.

#### Local screenshot comparison (no AI endpoint)

`--screen-comparison {manual,local}` defaults to `manual`. The Python API accepts
`compare_desktop_reports(..., screen_comparison="local")`. Local mode adds
`pixel_comparison` diagnostics to each initial workbook pair; with `--all-pages`,
diagnostics instead belong to individual `page_mappings` with unambiguous
correspondence and verified captures. Initial launch screenshots are not included
in all-page metrics.

The stdlib-only comparator decodes non-interlaced 8-bit RGB/RGBA PNGs and compares
literal RGBA channels (RGB receives alpha 255). It reports differing-pixel count
and fraction, absolute channel-difference sum, mean absolute channel error, and
normalized mean absolute channel error. It does not resize, crop, align, composite,
apply a tolerance, or produce an SSIM score or parity verdict. Both images must
have identical dimensions. Application chrome, DPI, fonts, animation, and selection
state can produce differences unrelated to report semantics.

Missing, unverified, unreadable, corrupt, unsupported, and dimension-mismatched
images have explicit reasons and no numeric metrics. PNG integrity is checked;
files and decoded scanlines are each limited to 64 MiB. Local results
(`compared`, `not_comparable`, or `error`) appear in the JSON manifest, main HTML,
and individual page-review HTML. They never change capture status or the CLI
exit code; inspect these diagnostics separately from capture completeness.

The comparison step uses no AI endpoint or network access. Desktop applications
are still needed for new captures: Tableau is not refreshed, while the Power BI
probe requests refresh and data verification and may contact configured data
sources. A captured screenshot alone does not prove successful refresh or
loaded-data correctness.

Exit code `0` means all requested captures are available for manual review, `1`
means a pairing/capture failure or unavailable application, and `2` means invalid
CLI arguments. In all-page mode, success also requires nonempty inventories,
unambiguous correspondence for every entry, and successful captures of every
inventoried view/page; it does not establish that every Tableau page was
discoverable or navigable. Missing/blank captures and timeouts remain explicit
in the report; older screenshots never satisfy a new run. Neither migration nor
deployment invokes this diagnostic automatically. Keep source workbooks and
evidence outside the repository.

### CI/CD Deployment (GitHub Actions)

The workflow in `.github/workflows/ci.yml` runs linting, pytest on Python
3.12, 3.13, and 3.14 across Ubuntu, Windows, and macOS, and artifact validation.
Staging and production deployment jobs are disabled; CI does not automatically
deploy batch output.

#### GitHub Secrets Required

| Secret | Description |
|--------|------------|
| `FABRIC_WORKSPACE_ID` | Target Fabric workspace GUID |
| `FABRIC_TENANT_ID` | Azure AD tenant GUID |
| `FABRIC_CLIENT_ID` | App registration client ID |
| `FABRIC_CLIENT_SECRET` | App registration client secret |
| `STAGING_WORKSPACE_ID` | Staging workspace GUID (for staging deploy) |

#### Staging vs Production

Configure separate deployment jobs, secrets, and environment approval before
enabling staging or production deployment. The checked-in workflow does not
provide active `deploy-staging` or `deploy-production` jobs.

## Environment Configurations

Three pre-configured environments in `powerbi_import/config/environments.py`:

| Setting | Development | Staging | Production |
|---------|------------|---------|------------|
| Log level | DEBUG | INFO | WARNING |
| Timeout (s) | 120 | 300 | 600 |
| Retry attempts | 1 | 3 | 5 |
| Retry delay (s) | 1 | 5 | 10 |
| Approval required | No | No | Yes |

## Retry & Error Handling

The `FabricClient` includes built-in retry logic:

- **HTTP 429** (Rate Limited): Respects `Retry-After` header, waits and retries
- **HTTP 5xx** (Server Error): Retries up to `RETRY_ATTEMPTS` times with `RETRY_DELAY` between attempts
- **Timeout**: Operations time out after `DEPLOYMENT_TIMEOUT` seconds

## REST API Server (Docker)

The migration tool can run as a REST API server for programmatic/headless migration.

### Docker

```bash
docker build -t tableau-to-pbi .
```

The image binds to `0.0.0.0` and requires an API key. Supply `--api-key` through
your deployment's secret-management mechanism when starting the server; the
default image command without a key is rejected. Do not paste keys into chat
or commit them.

Or run directly:

```bash
python -m powerbi_import.api_server --port 8000
```

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/migrate/batch` | POST | Submit a ZIP (multipart field `file`) containing one or more `.twb`, `.twbx`, `.tds`, or `.tdsx` files |
| `/batch/{id}` | GET | Check aggregate batch progress |
| `/status/{id}` | GET | Check an individual migration job status |
| `/download/{id}` | GET | Download completed .pbip project as ZIP |
| `/health` | GET | Health check |
| `/jobs` | GET | List all migration jobs |
| `/openapi.json` | GET | OpenAPI specification |
| `/metrics` | GET | Server metrics |

`POST /migrate` is retired and returns HTTP 410. A ZIP containing only one
workbook is valid.

For a key-protected server, include `Authorization: Bearer <API key>` on
protected upload, batch-status, job-status, and download requests. The examples
below target the directly started loopback server without API authentication,
not the externally bound Docker server.

### Usage Example

```bash
# Upload a ZIP containing one or more Tableau files for batch migration
curl -X POST -F "file=@workbooks.zip" http://localhost:8000/migrate/batch
# The response identifies the batch; poll its aggregate status

# Check an individual job
curl http://localhost:8000/status/abc123
# Returns: {"job_id": "abc123", "status": "completed"}

# Download result
curl -o output.zip http://localhost:8000/download/abc123
```

## Validation Before Deployment

Always validate generated artifacts before deploying:

```bash
python -c "
from powerbi_import.validator import ArtifactValidator
results = ArtifactValidator.validate_directory('./output')
for name, result in results.items():
    status = 'OK' if result['valid'] else 'FAIL'
    print(f'{status}: {name} ({result[\"files_checked\"]} files, {len(result[\"warnings\"])} warnings)')
"
```

## Troubleshooting

| Issue | Solution |
|-------|---------|
| `401 Unauthorized` | Check tenant ID, client ID, and client secret. Ensure admin consent is granted. |
| `403 Forbidden` | Verify the service principal has workspace access (Admin/Member role). |
| `429 Too Many Requests` | Retry logic handles this automatically. Reduce batch size if persistent. |
| `ImportError: azure-identity` | Install with `pip install azure-identity`. Required for authentication. |
| `ImportError: requests` | Install with `pip install requests`. Falls back to `urllib` if not available. |
| Stale files on Windows | OneDrive may lock generated files. Close OneDrive sync or use `--output-dir` outside synced folders. |

---

## Multi-Tenant Deployment

Deploy a shared semantic model to multiple Fabric workspaces with per-tenant connection string overrides.

### Configuration File

Create a `tenants.json`:

```json
{
  "tenants": [
    {
      "name": "Contoso",
      "workspace_id": "aaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      "connection_overrides": {
        "${TENANT_SERVER}": "contoso-sql.database.windows.net",
        "${TENANT_DATABASE}": "contoso_sales"
      },
      "rls_mappings": {
        "RegionManager": ["user1@contoso.com"]
      }
    },
    {
      "name": "Fabrikam",
      "workspace_id": "1111-2222-3333-4444-555555555555",
      "connection_overrides": {
        "${TENANT_SERVER}": "fabrikam-sql.database.windows.net",
        "${TENANT_DATABASE}": "fabrikam_sales"
      }
    }
  ]
}
```

### Usage

```bash
python migrate.py --shared-model wb1.twbx wb2.twbx --multi-tenant tenants.json
```

The pipeline copies the model for each tenant, substitutes `${TENANT_SERVER}` / `${TENANT_DATABASE}` placeholders in all `.tmdl`, `.m`, `.json`, and `.pbir` files, then deploys each copy to the tenant's workspace.

---

## Live Connection (byConnection) Mode

Wire thin reports to reference a published semantic model via Power BI connection string instead of local `byPath`.

### Usage

```bash
python migrate.py --shared-model wb1.twbx wb2.twbx --live-connection WORKSPACE_ID/ModelName
```

This generates thin reports with `definition.pbir` containing a `byConnection` reference:

```
Data Source=powerbi://api.powerbi.com/v1.0/myorg/{workspace_id};Initial Catalog={model_name}
```

Use this when the semantic model is already published to a Fabric workspace and thin reports should connect remotely.
