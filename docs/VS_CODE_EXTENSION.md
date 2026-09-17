# VS Code Extension Guide

The `vscode-extension/` directory contains a VS Code extension that wraps the
Python migration engine (`migrate.py`) so you can assess, preview, and migrate
Tableau workbooks without leaving the editor.

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
| `tableauToPowerBI.assess` | Tableau: Assess Migration Readiness | Runs the 9-category assessment and opens a results panel. |
| `tableauToPowerBI.migrate` | Tableau: Migrate to Power BI | Generates a `.pbip` project with live progress. |
| `tableauToPowerBI.previewDax` | Tableau: Preview DAX Conversions | Shows Tableau → DAX side by side with editable overrides. |
| `tableauToPowerBI.refreshTree` | Tableau: Refresh Workbook Tree | Reloads the tree from extraction JSON. |

Right-click a `.twb`/`.twbx` file in the Explorer to access **Assess** and
**Migrate** directly.

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
| `tableauToPowerBI.outputDirectory` | _(empty)_ | Output dir for `.pbip` projects (empty = engine default `artifacts/`). |
| `tableauToPowerBI.engineRoot` | _(empty)_ | Repository root containing `migrate.py` (empty = auto-detect). |

## Architecture

The extension is intentionally thin. All migration logic stays in the Python
engine; the TypeScript layer only:

1. Locates `migrate.py` (`pythonRunner.resolveEngineRoot`).
2. Spawns the engine and streams stdout (`pythonRunner.runEngine`).
3. Parses engine output (fidelity %, output path, assessment/DAX JSON).
4. Renders results in tree views and webviews.

Pure helper functions (argument building, tree construction, HTML rendering,
stdout parsing, override management) are unit-tested without launching the
Extension Host via a lightweight `vscode` stub:

```bash
cd vscode-extension
npm test   # 38 unit tests
```

## Development notes

- TypeScript is compiled with `strict` and `noUnusedLocals`.
- TextMate grammars live in `syntaxes/` (`dax.tmLanguage.json`,
  `tableau.tmLanguage.json`).
- The unit runner (`src/test/run-unit.ts`) installs a `vscode` stub via a
  `require` hook so pure functions can be exercised in plain Node + Mocha.
