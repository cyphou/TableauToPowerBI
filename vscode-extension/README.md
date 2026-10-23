# Tableau to Power BI — VS Code Extension

Assess, preview, and migrate Tableau workbooks (`.twb`/`.twbx`) to Power BI
projects (`.pbip`, PBIR v4.0 + TMDL) without leaving VS Code. The extension is a
thin front-end over the Python migration engine that lives in the repository
root (`migrate.py`).

**Current compatibility:** the extension's migration action still targets a
single workbook file; the current engine requires a folder and rejects direct
single-file generation. Use the [local browser UI](../docs/LOCAL_WEB_UI.md) or
`python migrate.py batch FOLDER --output-dir OUTPUT` from the repository root
until the adapter is updated. Assessment/preview helpers do not establish a
successful migration or native validation. See the
[extension guide](../docs/VS_CODE_EXTENSION.md).

## Features

- **Workbook tree view** — browse datasources, tables, columns, worksheets,
  dashboards, and parameters extracted from a workbook.
- **Assess migration readiness** — run the 9-category assessment and view the
  results in a webview panel with severity badges and recommendations.
- **Migration action (legacy adapter)** — currently incompatible with the
  folder-only engine generation contract; use the browser or batch CLI.
- **DAX conversion preview** — side-by-side Tableau → DAX with confidence
  indicators and editable override fields saved to `config.json`.
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
| `Tableau: Migrate to Power BI` | Legacy single-file action; use the supported folder workflow above. |
| `Tableau: Preview DAX Conversions` | Show Tableau → DAX side by side. |
| `Tableau: Refresh Workbook Tree` | Reload the tree from extraction JSON. |

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `tableauToPowerBI.pythonPath` | `python` | Python interpreter for the engine. |
| `tableauToPowerBI.outputDirectory` | _(empty)_ | Output dir for `.pbip` projects. |
| `tableauToPowerBI.engineRoot` | _(empty)_ | Repository root containing `migrate.py`. |

## Development

```bash
cd vscode-extension
npm install
npm run compile     # type-check + emit to out/
npm test            # run the unit suite (pure functions, no Extension Host)
```

The unit suite installs a lightweight `vscode` stub via a `require` hook so the
pure helper functions (argument building, tree construction, HTML rendering,
stdout parsing, override management) can be tested without launching the
Extension Host.
