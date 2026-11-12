# Tableau to Power BI — VS Code Extension

Assess, preview, and migrate Tableau workbooks (`.twb`/`.twbx`) to Power BI
projects (`.pbip`, PBIR v4.0 + TMDL) without leaving VS Code. The extension is a
thin front-end over the Python migration engine that lives in the repository
root (`migrate.py`).

The migration action passes the selected workbook to the engine through a
single-entry batch configuration, without moving the source. Each run has its
own output directory. A retained partial project is diagnostic only and is
reported as a failed migration, not a validated result. Generation does not
verify Desktop opening, refresh or visual parity. See the
[extension guide](../docs/VS_CODE_EXTENSION.md).

## Features

- **Workbook tree view** — browse datasources, tables, columns, worksheets,
  dashboards, and parameters extracted from a workbook.
- **Assess migration readiness** — extract the selected workbook in a temporary
  directory and show its static assessment, verdict and recommendations. Failed
  extraction never falls back to another workbook's assessment.
- **Migration action** — generate a project for the selected workbook and
  surface retained partial diagnostics separately from successful generation.
- **DAX conversion preview** — side-by-side Tableau → DAX, marked for review
  because full model context and runtime equivalence are not verified; editable
  override fields are saved to `config.json`.
- **Syntax highlighting** — TextMate grammars for DAX and Tableau calculation
  language (LOD, table calcs, functions).
- **Status bar** — shows the current migration state and last fidelity score.

## Requirements

- Python 3.12+ on your `PATH` (or set `tableauToPowerBI.pythonPath`).
- The Tableau-to-PowerBI repository checked out locally. The extension
  auto-detects `migrate.py` by walking up from the active file; override with
  `tableauToPowerBI.engineRoot`.

## Commands

| Command | Description |
|---------|-------------|
| `Tableau: Assess Migration Readiness` | Run the readiness assessment. |
| `Tableau: Migrate to Power BI` | Migrate the selected workbook via the batch engine. |
| `Tableau: Preview DAX Conversions` | Show Tableau → DAX side by side. |
| `Tableau: Refresh Workbook Tree` | Reload the tree from extraction JSON. |

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `tableauToPowerBI.pythonPath` | `python` | Python interpreter for the engine. |
| `tableauToPowerBI.outputDirectory` | _(empty)_ | Output root; each run gets a new `migration-*` subdirectory. Empty uses `artifacts/powerbi_projects/migrated` under the engine root. |
| `tableauToPowerBI.engineRoot` | _(empty)_ | Repository root containing `migrate.py`. |

## Development

```bash
cd vscode-extension
npm install
npm run compile     # type-check + emit to out/
npm test            # run helper and Python integration tests, no Extension Host
```

The unit suite installs a lightweight `vscode` stub via a `require` hook so the
helper functions and real Python engine integrations can be tested without
launching the Extension Host. Set `TTPBI_TEST_PYTHON` to select an interpreter.
