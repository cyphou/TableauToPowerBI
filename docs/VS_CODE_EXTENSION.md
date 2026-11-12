# VS Code Extension Guide

The `vscode-extension/` directory contains a VS Code extension that wraps the
Python migration engine (`migrate.py`) so you can assess, preview, and migrate
Tableau workbooks without leaving the editor.

## Migration behavior

The **Migrate** action uses a temporary single-entry batch configuration with
the selected workbook's original path. It writes each run to a separate
`migration-*` directory under the configured output root and preserves failed
run diagnostics. A retained partial PBIP is flagged as diagnostic only and
failed, not as a successful migration. Generation does not verify Desktop
opening, refresh or visual parity; assessment and preview are separate checks.

## Installation (from source)

The extension is not yet published to the Marketplace. To run it locally:

```bash
cd vscode-extension
npm install
npm run compile
```

Then press **F5** in VS Code with the `vscode-extension/` folder open to launch
an Extension Development Host, or package it:

```bash
npx @vscode/vsce package   # produces tableau-to-powerbi-40.0.0.vsix
code --install-extension tableau-to-powerbi-40.0.0.vsix
```

## Requirements

- **Python 3.12+** on your `PATH`, or set `tableauToPowerBI.pythonPath`.
- A local checkout of this repository (the extension calls `migrate.py`). The
  engine root is auto-detected by walking up from the active file; override it
  with `tableauToPowerBI.engineRoot`.

## Commands

| Command | Palette title | What it does |
|---------|---------------|--------------|
| `tableauToPowerBI.assess` | Tableau: Assess Migration Readiness | Extracts the selected workbook and opens its static assessment; failures do not reuse older results. |
| `tableauToPowerBI.migrate` | Tableau: Migrate to Power BI | Migrates the selected workbook through batch-config and distinguishes diagnostic partial output. |
| `tableauToPowerBI.previewDax` | Tableau: Preview DAX Conversions | Shows Tableau → DAX previews with editable overrides; conversions require review with model context. |
| `tableauToPowerBI.refreshTree` | Tableau: Refresh Workbook Tree | Reloads the tree from extraction JSON. |

Right-click a `.twb`/`.twbx` file in the Explorer to access **Assess** and
**Migrate** actions.

## Views

- **Tableau Workbook** (Explorer view): a tree of datasources → tables →
  columns, plus worksheets, dashboards, and parameters extracted from the
  workbook.
- **Status bar**: shows the current migration state (idle / assessing /
  migrating / done / error) and the last fidelity score.

## DAX preview & overrides

The DAX preview panel renders each Tableau calculation next to its converted
DAX with a confidence indicator (exact / approximated / unsupported). Edit the
DAX in any field and click **Save override** — the value is written to
`config.json` under `dax_overrides`, which the engine applies on the next
migration run.

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `tableauToPowerBI.pythonPath` | `python` | Python interpreter for the engine. |
| `tableauToPowerBI.outputDirectory` | _(empty)_ | Output root; each run gets a new `migration-*` subdirectory. Empty uses `artifacts/powerbi_projects/migrated` under the engine root. |
| `tableauToPowerBI.engineRoot` | _(empty)_ | Repository root containing `migrate.py` (empty = auto-detect). |

## Architecture

The extension is intentionally thin. All migration logic stays in the Python
engine; the TypeScript layer only:

1. Locates `migrate.py` (`pythonRunner.resolveEngineRoot`).
2. Creates an isolated output directory and invokes the batch engine for the selected source.
3. Parses engine output (fidelity %, output path, assessment/DAX JSON).
4. Renders results in tree views and webviews.

Helper and Python integration tests run without launching the Extension Host
via a lightweight `vscode` stub:

```bash
cd vscode-extension
npm test   # set TTPBI_TEST_PYTHON to select the Python interpreter
```

## Development notes

- TypeScript is compiled with `strict` and `noUnusedLocals`.
- TextMate grammars live in `syntaxes/` (`dax.tmLanguage.json`,
  `tableau.tmLanguage.json`).
- The unit runner (`src/test/run-unit.ts`) installs a `vscode` stub via a
  `require` hook so pure functions can be exercised in plain Node + Mocha.
