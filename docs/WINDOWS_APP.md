# Windows Desktop Application

![Tableau to Power BI migration logo](images/logo-migration-dashboards.png)

The distributable Windows application is the portable folder:

```text
dist\windows\TableauToPowerBI\
  TableauToPowerBI.exe
  _internal\
```

Copy the complete `TableauToPowerBI` folder. Launch `TableauToPowerBI.exe` from
that folder.

## Runtime Prerequisites

The portable application requires no:

- Python installation
- PowerShell installation or script execution policy changes
- Python virtual environment
- TableauToPowerBI repository checkout
- PyInstaller installation

The embedded engine includes the Tkinter UI, migration CLI, Tableau extractors,
Power BI generators, validators, quality policies, M-emitter coverage, Server/
Cloud workflow, and Fabric output path.

The application still requires normal access to the target environment when the
selected workflow needs it:

- Tableau Server/Cloud URL and permitted PAT for Server downloads.
- `TABLEAU_TOKEN_SECRET` supplied through the launching environment for Server
  tasks; the UI never stores the PAT secret.
- Power BI/Fabric credentials and an authorized workspace for live deployment.
- Source workbook access and write permission for the selected output folder.

## Build From Source

Builds should use Python 3.13 because it produces the supported Windows frozen
runtime. Build-time prerequisites are separate from runtime prerequisites.

```powershell
winget install --id Python.Python.3.13 --exact --scope user
py -3.13 -m pip install pyinstaller
powershell -ExecutionPolicy Bypass -File .\scripts\build_light_ui_exe.ps1 -Clean
```

The build script writes the portable application to:

```text
dist\windows\TableauToPowerBI\
```

It uses an onedir package intentionally. A one-file executable extracts Python
DLLs into a temporary directory, which can be blocked by Windows Application
Control policies. The onedir package keeps the runtime DLLs beside the EXE and
is the supported distribution layout.

## Available Workflows

The application exposes:

- **Assess** — local portfolio readiness and migration planning.
- **Migrate** — PBIP batch migration.
- **Fabric** — local Lakehouse, Dataflow, Notebook, Direct Lake Semantic Model,
  Report, and Pipeline scaffold.
- **Quality** — unified quality and openability evidence with `report`,
  `enterprise`, or `production` policies.
- **M Coverage** — offline 98-path Power Query emitter matrix with fallback
  ownership and remediation.
- **Server** — download one Tableau Server/Cloud workbook or an entire project,
  then migrate it locally.
- **Lineage** — Tableau Prep flow lineage analysis.

Fabric generation is a local scaffold result. Desktop reopen, semantic
execution, refresh, deployment, and post-deployment health remain explicit
runtime evidence states.

After a Fabric run, the Results area exposes **Fabric Evidence** when the
generated project contains `fabric_evidence.json`. The file records artifact
presence, dependency order, local validation status, and runtime states without
claiming live deployment or refresh success.

## Developer Fallback

For repository development, the Python/Tkinter path remains available:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_light_ui.ps1
```

This path requires the repository Python environment and is useful for debugging
or modifying the UI source. It is not required by the portable application.
