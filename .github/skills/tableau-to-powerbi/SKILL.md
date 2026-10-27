---
name: tableau-to-powerbi
description: >-
  Migrate Tableau workbooks (.twb/.twbx) and Prep flows (.tfl/.tflx) to Power BI
  projects (.pbip, PBIR v4.0 + TMDL) or Fabric-native artifacts using this repo's
  migrate.py engine.   USE WHEN the user wants to: convert/migrate Tableau workbook(s)
  or dashboard to Power BI, assess migration readiness, batch-migrate a folder,
  build a shared semantic model from several workbooks, migrate from Tableau Server/
  Cloud, generate a parity/QA report, or deploy the result to Fabric/Power BI Service.
  Triggers: "migrate this Tableau workbook", "convert .twbx to Power BI", "assess this
  dashboard", "batch migrate", "shared model", "migrate from Tableau Server", "deploy
  to Fabric", "run the migration", "why did the DAX fail", "check parity". DO NOT USE
  FOR: authoring Power BI reports from scratch, non-Tableau BI tools, or general DAX
  questions unrelated to a Tableau source.
---

# Tableau → Power BI Migration Skill

This repository is an automated migration engine. This skill tells you how to drive
it correctly. **Prefer running the CLI over hand-editing generated artifacts.**

## Golden rules

1. **No external dependencies for core migration** — everything runs on the Python
   standard library. Use the repo venv: `.venv\Scripts\python.exe` (Windows) or
   `.venv/bin/python` (Linux/macOS). Never `pip install` for a core migration.
2. **One command does the whole pipeline.** `migrate.py` orchestrates extraction →
   generation. Don't call the internal extractors/generators directly unless
   debugging.
3. **Validate, don't assume.** Every PBIP migration runs the static openability gate
  automatically. Do not disable it except for diagnosis. Power BI Desktop is never
  launched automatically. Batch does not forward `--desktop-probe`; use separately
  confirmed local browser diagnostics or `scripts\probe_projects.py` for a native
  check. Static QA and Desktop evidence remain separate.
4. **Read before editing generated code.** If a DAX/TMDL/PBIR fix is needed, find the
   *source generator* (see "Where things live") and fix it there so the fix is
   reproducible — not in the emitted artifact.
5. **Advisory output may propose, never apply.** LLM refinement, remediation
   suggestions, conversational answers and marketplace recipes are allowed to be
   non-deterministic *because* they stay out of the artifact path: they read
   evidence and write their own report. Never write a model-changing suggestion
   into `.tmdl`/`.pbir` — emit it as a suggestion and let a deterministic gate
   (openability, DAX validator, parity) decide. `scripts/check_advisory_boundary.py`
   enforces this; the same workbook must always produce the same project.

## The 2-step pipeline

```
.twbx ──[Extraction]──► 23 JSON files ──[Generation]──► .pbip (PBIR v4.0 + TMDL)
```

- **Extraction** (`tableau_export/`): parses Tableau XML, emits 23 object types.
- **Generation** (`powerbi_import/`): produces the full `.pbip` (TMDL semantic model,
  PBIR v4.0 report, Power Query M, visuals, filters, bookmarks).

Source selection precedes Hyper data reading: prefer a complete explicit original
database over Hyper in the same datasource, without exporting that Hyper.
File-backed extracts and unresolved published references do not qualify;
incomplete/ambiguous database bindings fail explicitly. Remaining Hyper consumers
still require full CSV/Parquet export and exact Import bindings. Generating native
M does not query the database or prove snapshot parity. See the
[source decision diagram](../../../docs/ARCHITECTURE.md#automatic-storage-mode-and-hyper-export).

Generation is batch-only: pass a source folder to the `batch` or `fabric`
command. A folder containing just one workbook is valid. Assessment, quality,
and parity can still analyze individual source files; `qa` and
`package` take folders and follow the batch migration path. Batch mode writes
unified quality reports and evidence ZIPs by default, but does not run the
specialized six-check QA suite or assessment report packager.

For a guided local workflow, run `.\run_web_ui.ps1` or
`python web\local_server.py` from the repository root. This loopback-only
folder-path UI submits the same batch engine; it is separate from the optional
Streamlit/upload UI and REST ZIP API. See the
[local browser guide](../../../docs/LOCAL_WEB_UI.md). Native diagnostics are
separately confirmed, never automatic migration steps.

Deployment is a separate operation: use the MCP `deploy` tool on a generated
PBIP project, preview first, then explicitly confirm the write. Batch dispatch
does not forward `--deploy` or refresh flags; they do not deploy batch outputs.

## Supported CLI workflows

```bash
.venv\Scripts\python.exe migrate.py batch .\workbooks --output-dir C:\out
.venv\Scripts\python.exe migrate.py assess workbook.twbx
.venv\Scripts\python.exe migrate.py batch folder --output-dir C:\out
.venv\Scripts\python.exe migrate.py server https://tableau.example "Sales Dashboard"
.venv\Scripts\python.exe migrate.py merge wb1.twbx wb2.twbx --model-name "Sales"
.venv\Scripts\python.exe migrate.py fabric .\workbooks
.venv\Scripts\python.exe migrate.py qa .\workbooks
.venv\Scripts\python.exe migrate.py quality workbook.twbx
.venv\Scripts\python.exe migrate.py parity workbook.twbx
.venv\Scripts\python.exe migrate.py portfolio .\workbooks
.venv\Scripts\python.exe migrate.py plan https://tableau.example
.venv\Scripts\python.exe migrate.py lineage .\prep_flows
.venv\Scripts\python.exe migrate.py package .\workbooks
```

Legacy flag-based entry points include `--batch folder` and `--assess file`.
The legacy `--qa` flag is retained, but on batch generation it does not run
the specialized single-workbook QA suite.

Use `quality` for one deterministic report combining assessment, feature parity,
data coverage, interface coverage, and Power BI openability. Use `parity` for the
feature scorecard, `portfolio` for folder assessment, `plan` for Tableau Server
migration waves, and `lineage` for Prep flows. The `qa` and `package` folder
shortcuts follow the batch path rather than running their single-workbook extras.
For a CI quality exit gate, run `quality WORKBOOK --quality-strict` after
generation; batch does not forward `--quality-strict`. Use
`--quality-policy report|enterprise|production` to control escalation of
static semantic diagnostics and unresolved lineage. The semantic runtime
adapter is offline-safe: without an explicitly authorized executor, runtime
status remains `not_run`.

Batch migration writes a unified quality JSON and HTML report beside each
successful workbook output and prints aggregate PASS/WARN/FAIL counts. Extraction
data is snapshotted per workbook before the next batch item starts, so parallel
or resumed batches do not compare a target against another workbook's source
snapshot. The batch root also contains `batch_quality_summary.json` with
per-workbook statuses and report paths for CI or downstream automation. Batch
outcomes also update `zero_touch_history.json`; a quality `FAIL` is recorded as
`quality_blocker` rather than a successful migration.

Run `.venv\Scripts\python.exe migrate.py --help` for the concise list. Supported
flag-based automation remains available through batch mode; direct single-file
project generation has been retired. `--advanced-help` exposes advanced and
legacy options. Prefer the subcommands for new workflows.
Never put credentials in chat or command arguments; use environment variables.

## What you get (output layout)

A generated `.pbip` project contains:
- `*.SemanticModel/` — TMDL model (`model.tmdl`, `tables/*.tmdl`, `relationships`,
  `roles.tmdl` for RLS, Power Query M partitions).
- `*.Report/` — PBIR v4.0 report (`definition/report.json`, `pages/*/page.json`,
  `.../visuals/*/visual.json`, bookmarks, theme in `RegisteredResources/`).
- `.platform`, `definition.pbir`, `*.pbip` manifests.

Assessment/QA/parity runs also write HTML + JSON reports to the output dir.

## Where things live (fix at the source, by domain)

| You need to change… | Owning file(s) | Agent |
|---|---|---|
| Tableau XML parsing / extraction | `tableau_export/extract_tableau_data.py`, `datasource_extractor.py` | @extractor |
| Tableau → DAX formula conversion | `tableau_export/dax_converter.py`, `powerbi_import/dax_optimizer.py` | @dax |
| Power Query M / classification | `tableau_export/m_query_builder.py`, `powerbi_import/calc_column_utils.py` | @wiring |
| TMDL model (tables, relationships, RLS, hierarchies) | `powerbi_import/tmdl_generator.py` | @semantic |
| PBIR report / visuals / slicers / themes | `powerbi_import/pbip_generator.py`, `visual_generator.py` | @visual |
| CLI / pipeline / batch | `migrate.py`, `powerbi_import/import_to_powerbi.py` | @orchestrator |
| Local browser backend / launcher | `web/local_server.py`, `run_web_ui.ps1` | @orchestrator |
| Local browser frontend | `web/index.html`, `web/app.js`, `web/style.css` | @web-designer |
| Readiness assessment | `powerbi_import/assessment.py` | @assessor |
| Quality / parity / diff evidence | `powerbi_import/migration_quality.py`, `parity_registry.py`, `qa_suite.py` | @evidence |
| Fabric-native generators | `powerbi_import/fabric_*_generator.py`, `lakehouse_generator.py` | @fabric |
| Deployment (Fabric / PBI Service) | `powerbi_import/deploy/*.py` | @deployer |
| Tests | `tests/*.py` | @tester |

Only the owning agent modifies each file; read access is universal. See
[.github/agents/](../../agents) and [docs/AGENTS.md](../../../docs/AGENTS.md).

## Reference docs (read on demand)

- Tableau→DAX: [docs/TABLEAU_TO_DAX_REFERENCE.md](../../../docs/TABLEAU_TO_DAX_REFERENCE.md)
- Tableau→Power Query: [docs/TABLEAU_TO_POWERQUERY_REFERENCE.md](../../../docs/TABLEAU_TO_POWERQUERY_REFERENCE.md)
- Visual/type mapping: [docs/MAPPING_REFERENCE.md](../../../docs/MAPPING_REFERENCE.md)
- Known limitations: [docs/KNOWN_LIMITATIONS.md](../../../docs/KNOWN_LIMITATIONS.md)
- PBI project guide: [docs/POWERBI_PROJECT_GUIDE.md](../../../docs/POWERBI_PROJECT_GUIDE.md)
- FAQ: [docs/FAQ.md](../../../docs/FAQ.md)

## Troubleshooting playbook

- **"No DAX output" / measures show TODO/BLANK** → likely a converter/validator edge
  case. Reproduce with `--verbose`, then fix in `dax_converter.py` or check
  `dax_validator.py`. Never hand-patch the emitted `.tmdl` as the fix.
- **Workbook won't parse** → run `--dry-run --verbose`; check for corrupt `.twbx`
  ZIP or unsupported Tableau version ([docs/TABLEAU_VERSION_COMPATIBILITY.md](../../../docs/TABLEAU_VERSION_COMPATIBILITY.md)).
- **Visual loads blank in Desktop** → PBIR annotations must live at the visual.json
  *container* root, never inside the inner `visual` object. See `visual_generator.py`.
- **Relationship / cardinality wrong** → cardinality is inferred in
  `tmdl_generator.py`; check join clauses and column-count ratios.
- **Batch run partially fails** → each item is isolated; read the per-item status and
  the batch summary, then re-run only the failed workbook.

## Testing

- Full suite: `.venv\Scripts\python.exe -m pytest -q`
- Targeted: `.venv\Scripts\python.exe -m pytest -k "dax or tmdl or visual" -q`
- Any test importing `tableau_export/datasource_extractor` must
  `sys.path.insert(0, .../tableau_export)` (it uses top-level `from dax_converter import`).

## Safety

- Never echo Tableau Server tokens/PATs into chat or ask for them via a question tool —
  have the user type the secret directly into the terminal.
- Generated artifacts under `artifacts/` and auto-generated dashboards
  (`docs/zero_error_dashboard.html`) are build output — do not stage them in commits.
