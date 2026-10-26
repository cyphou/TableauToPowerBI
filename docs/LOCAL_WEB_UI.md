# Local Browser Interface

[🏠 Overview](../README.md) · [📚 Documentation](README.md) · [🗄️ Source strategy](ARCHITECTURE.md#automatic-storage-mode-and-hyper-export) · [✅ Review checklist](MIGRATION_CHECKLIST.md)

The browser interface starts the existing Python migration engine locally. It
is the supported guided UI and does not require a packaged desktop application,
an account, or cloud hosting. Its page and assets are served locally.

## Requirements

- A repository checkout with the migration engine.
- Python 3.12 or later.
- A current browser with JavaScript enabled.
- Local read access to the Tableau source folder and write access to the output
  folder.

The runner and frontend use the Python standard library and local HTML, CSS,
JavaScript, and SVG assets. No frontend package manager or installation of
third-party Python packages is required to start the interface.

This is the folder-path UI in `web/local_server.py`, not the separate
Streamlit/upload UI in `web/app.py` or the REST ZIP-upload API. See
[the documentation index](README.md) for those workflows.

## Start

From the repository root in PowerShell, use the convenience launcher:

```powershell
.\run_web_ui.ps1
```

The launcher prefers `.venv\Scripts\python.exe` when present, otherwise Python
on `PATH`. Its optional switches are `-Port 8766`, `-NoBrowser`, and `-DryRun`
(print the selected interpreter/server without starting it).

Or start the same server directly with Python:

```powershell
python web\local_server.py
```

By default, the runner binds exclusively to `127.0.0.1:8765` and opens the
local browser. If the default port is in use, choose another local port:

```powershell
python web\local_server.py --port 8766
```

Open the loopback address printed by the runner, for example
`http://127.0.0.1:8765/`. To start without opening a browser automatically:

```powershell
python web\local_server.py --no-browser
```

On other platforms, run the same Python commands from a terminal, using the
local platform's path separators.

To stop the runner and its managed migration process, return to the terminal
and press Ctrl+C. Closing the browser tab alone does not cancel a running
job. Do not expose the loopback runner on a network interface.

## Run a migration

1. Enter the absolute path to the source folder containing Tableau workbooks.
   Use a folder even when migrating one workbook; generation follows the
   engine's batch workflow.
2. Enter an absolute path to a local output folder.
   Source and output folders must be separate, non-nested local directories;
   network shares are not accepted.
3. Select the Power BI project or local Fabric artifact format and, if needed,
   the model culture, calendar years, quality policy, and resume setting.
   For Power BI projects, leave storage mode on **Automatique**, or select
   **Import** to override DirectQuery. Automatic mode imports Hyper snapshots
   and uses DirectQuery only for capable native sources without Hyper.
   Non-Hyper Import generates the datasource connection, schema and M without
   exporting rows. An explicitly packaged Excel workbook is copied unchanged
   into the project's `Data/` folder and bound by its absolute path; loose
   native files and packaged non-Excel files are not copied.
   Power BI loads native rows at refresh. Hyper data still export.
4. Start the migration and follow its real status and technical log.
5. Open the generated reports or project artifacts from the result list, or
   download the completed output as a ZIP.

Copied Excel workbooks contain source data, including all sheets and metadata.
Treat their output ZIPs accordingly and keep them out of repositories. Moving
the project requires updating absolute Excel source paths; copying alone does
not establish successful native refresh or parity.

The browser cannot browse or upload folders from the local computer. Enter or
paste the absolute paths directly into the two folder fields.
The backend retains an optional native folder-picker endpoint, but the current
page does not expose a Browse button; this is not the removed desktop UI.

The equivalent core-engine command is:

```powershell
python migrate.py --batch .\workbooks --output-dir .\output
```

For other commands and options, see the [CLI guide](../README.md).
The [storage-mode diagram](ARCHITECTURE.md#automatic-storage-mode-and-hyper-export)
also shows the PBIP native Hyper export rule: above 10 MiB requires Parquet,
otherwise CSV. Above-threshold SQLite-compatible inputs fail unless CSV is
explicitly selected through the CLI; the browser keeps automatic export.
Native Import does not export source rows. Explicitly packaged Excel is copied
unchanged into `Data/` and bound by absolute path; loose native files and packaged
non-Excel files are not copied. Configure their original paths and credentials
in Power Query when needed.
Fabric keeps its separate DirectLake/CSV artifact path.

## Local data and safety

The browser page and its API are served from the same loopback origin. Source
folders are accessed by the local Python process; workbook folders are not
uploaded through the browser. Jobs run locally, and generated files are written
to the chosen output directory.

The runner validates local paths, binds to the loopback address, restricts
requests to its expected host and origin, and issues a per-run session token
for state-changing requests. Keep the loopback service available only to this
computer. Do not substitute a remote-hosted page or expose the runner to your
LAN.

Ordinary generation does not launch Power BI Desktop or Tableau, refresh a
remote data source, deploy a project, or perform an online parity check.
Connection or query files in the output do not imply that a source is
reachable. Runtime checks that were not executed remain `not_run`.

Only a successfully completed migration exposes the project ZIP. The archive
is filtered generated output, not a source backup, and it may still contain
private staged data, logs or screenshots. Use a dedicated output directory
and review the contents before sharing it.

The runner retains at most 30 jobs and 2,000 log lines per job in memory;
job history and active-job tracking are not durable across restarts. There is
one active job at a time, and no per-job cancellation control in the page.

## Optional native diagnostics

The Power BI probe and Tableau/Power BI comparison are separate, optional
actions. Before running one, review the confirmation dialog. A diagnostic may
launch an installed desktop application, use its configured connections, access
networked sources, or capture local report content. Only an explicitly
confirmed diagnostic can start; it is not part of the ordinary offline
migration.

If the required application or diagnostic is unavailable, the job reports that
the action was not run. A successful file-generation job is not proof that a
Power BI project opens, that data refreshes, or that the Tableau and Power BI
results are equivalent.

## Previously recorded browser acceptance

Existing acceptance notes report a real-browser migration of the public
Superstore sample with artifacts, logs, and a download link; French UI job
polling; and no horizontal overflow at a 390px viewport. The recorded evidence
includes separate observations of 22 and 23 artifacts and 189 log lines. These
are preserved as distinct prior observations, not a reconciled count or a new
measurement. Canceling either native-diagnostic authorization dialog submitted
no job or native launch. Native runtime remains `not_run`; browser acceptance
does not complete N5 operational acceptance or prove deployment readiness.

## Troubleshooting

- **Page is unavailable:** confirm that the local runner is still open in its
  terminal and use the exact `127.0.0.1` URL and port it printed.
- **Browser cannot connect or the session expires:** choose **Réessayer la
  connexion**. This refreshes local job status; it does not resubmit a
  migration.
- **The request may have been accepted but the connection timed out:** refresh
  the task list before attempting anything again. Ambiguous requests are never
  automatically retried.
- **No folder Browse button:** the current page intentionally uses typed or
  pasted absolute local paths.
- **A job fails:** review the job error and technical log, and verify that the
  process can read the source folder and write to the chosen output directory.
  Some optional readers require an appropriate locally installed dependency.
- **A generated report does not open from its link:** make sure the runner is
  still running. The application serves permitted generated reports from the
  selected job's output directory.

## Existing alternatives

The migration CLI and the existing optional Streamlit interface remain
available for their documented workflows. The local browser interface is the
recommended interactive path.
