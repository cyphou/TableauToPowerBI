---
name: tableau-to-powerbi
description: >-
  Migrate Tableau workbooks (.twb/.twbx) and Prep flows (.tfl/.tflx) to Power BI
  projects (.pbip, PBIR v4.0 + TMDL) or Fabric-native artifacts using this repo's
  migrate.py engine. USE WHEN the user wants to: convert/migrate a Tableau workbook
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
  launched automatically; use the explicit `--desktop-probe` diagnostic when a real
  Desktop smoke test is required, or run `--qa` / `scripts/autoplay.py` for deeper QA.
4. **Read before editing generated code.** If a DAX/TMDL/PBIR fix is needed, find the
   *source generator* (see "Where things live") and fix it there so the fix is
   reproducible — not in the emitted artifact.

## The 2-step pipeline

```
.twbx ──[Extraction]──► 23 JSON files ──[Generation]──► .pbip (PBIR v4.0 + TMDL)
```

- **Extraction** (`tableau_export/`): parses Tableau XML, emits 23 object types.
- **Generation** (`powerbi_import/`): produces the full `.pbip` (TMDL semantic model,
  PBIR v4.0 report, Power Query M, visuals, filters, bookmarks).

## Public CLI: 8 commands

```bash
.venv\Scripts\python.exe migrate.py migrate workbook.twbx
.venv\Scripts\python.exe migrate.py assess workbook.twbx
.venv\Scripts\python.exe migrate.py batch folder --output-dir C:\out
.venv\Scripts\python.exe migrate.py server https://tableau.example "Sales Dashboard"
.venv\Scripts\python.exe migrate.py merge wb1.twbx wb2.twbx --model-name "Sales"
.venv\Scripts\python.exe migrate.py fabric workbook.twbx
.venv\Scripts\python.exe migrate.py deploy workbook.twbx WORKSPACE_ID
.venv\Scripts\python.exe migrate.py qa workbook.twbx
```

Run `.venv\Scripts\python.exe migrate.py --help` for the concise list. Existing
flag-based automation remains compatible; `--advanced-help` exposes legacy options.
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
| Readiness / parity / diff reports | `powerbi_import/assessment.py`, `parity_registry.py`, `qa_suite.py` | @assessor |
| Fabric-native generators | `powerbi_import/fabric_*_generator.py`, `lakehouse_generator.py` | @generator |
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
