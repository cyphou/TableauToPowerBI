# Documentation

Current package baseline: **v44.0.0**. Active roadmap track: **v48.0.0**, with
verified PBIP manifest-coherence, static-openability, quality-policy, and
semantic-runtime evidence hardening (see `../CHANGELOG.md` and `ROADMAP.md`).

## Guides

- [POWERBI_PROJECT_GUIDE.md](POWERBI_PROJECT_GUIDE.md) — Understanding and using `.pbip` projects
- [MAPPING_REFERENCE.md](MAPPING_REFERENCE.md) — Tableau ↔ Power BI mappings (190 visuals, formulas, interactions)
- [TABLEAU_TO_DAX_REFERENCE.md](TABLEAU_TO_DAX_REFERENCE.md) — Complete 133+-function Tableau → DAX mapping
- [TABLEAU_TO_POWERQUERY_REFERENCE.md](TABLEAU_TO_POWERQUERY_REFERENCE.md) — Complete 108-property Tableau → Power Query M mapping (25 connectors)
- [TABLEAU_PREP_TO_POWERQUERY_REFERENCE.md](TABLEAU_PREP_TO_POWERQUERY_REFERENCE.md) — Complete 165-operation Tableau Prep → Power Query M transformation mapping
- [FAQ.md](FAQ.md) — Frequently asked questions
- [ROADMAP.md](ROADMAP.md) — Release gates, verified evidence, and active semantic-validation work

## Quick Reference

### CLI Commands

```bash
python migrate.py migrate file.twbx
python migrate.py assess file.twbx
python migrate.py batch dir/ --output-dir /tmp/out
python migrate.py server https://tableau.example "Sales Dashboard"
python migrate.py merge wb1.twbx wb2.twbx
python migrate.py fabric file.twbx
python migrate.py deploy file.twbx WORKSPACE_ID
python migrate.py qa file.twbx
python migrate.py quality file.twbx
python migrate.py parity file.twbx
python migrate.py portfolio ./workbooks
python migrate.py plan file.twbx
python migrate.py lineage ./prep_flows
python migrate.py package file.twbx
```

Use `python migrate.py quality file.twbx` for the combined assessment, parity,
data, interface, and openability report. Add `--quality-policy report|enterprise|production`
to control escalation of static semantic diagnostics and unresolved lineage.
Use `python migrate.py --help` for the
concise command list; existing flag-based automation remains compatible through
`python migrate.py --advanced-help`.

Semantic-context checks for converted LOD expressions are available through
`powerbi_import.semantic_execution_validator`. They are static diagnostics only;
the optional `powerbi_import.semantic_runtime` adapter accepts an injected
executor and records bounded DAX query evidence as `not_run`, `passed`, or
`failed`. The unified quality report continues to label live semantic execution
as `not_run` until an authorized execution environment supplies evidence.

Quality-surface coverage is explicit: the concise single-workbook CLI and MCP
`quality_report` tool run the unified report; `qa` remains the specialized
real-world report card. Batch and Notebook workflows currently expose their own
migration/assessment operations and do not implicitly run the unified quality
report for every item.

Notebook sessions can run the same report explicitly after generation:
`session.quality_report()`. It writes JSON and HTML evidence beside the generated
project and keeps live semantic execution marked `not_run` until an authorized
runtime is used.

The Windows desktop application is `web/light_ui.py`, launched with
`powershell -ExecutionPolicy Bypass -File .\run_light_ui.ps1`. Its Quality task
passes the selected `report`, `enterprise`, or `production` policy to the batch
engine and exposes the generated output and HTML dashboard. Its M Coverage task
runs the offline 98-path M-emitter matrix and writes `m_emitter_matrix.json`.
The Fabric task selects `--output-format fabric` and generates the local
Lakehouse, Dataflow, Notebook, Semantic Model, Report, and Pipeline scaffold;
deployment and refresh remain authorized runtime steps.

The Server task downloads one Tableau Server/Cloud workbook or a project before
migrating it. Set `TABLEAU_TOKEN_SECRET` in the PowerShell session; the UI only
stores the Server URL, site, PAT name, and workbook/project target. Server tests
require content-download permissions and a reachable Tableau endpoint.

Build the autonomous Windows application with Python 3.13 and PyInstaller:
`py -3.13 -m pip install pyinstaller` followed by
`powershell -ExecutionPolicy Bypass -File .\scripts\build_light_ui_exe.ps1 -Clean`.
The resulting portable folder `dist\windows\TableauToPowerBI\` contains
`TableauToPowerBI.exe` and its bundled runtime. Copy the complete folder; it
does not require Python, PowerShell, a virtual environment, or the repository
at runtime.

Desktop probe results are evidence gates: a successful launch can produce
`DESKTOP_SMOKE_PASS`, while a crash, timeout, or probe error downgrades the
confidence level to `UNVERIFIED` even when static validation passed. Two
successful consecutive launches produce `DESKTOP_REOPEN_PASS`; this still does
not claim that Desktop saved project changes.

The feedback loop uses the unified quality status when recording zero-touch
history: quality blockers are classified as `quality_blocker` and cannot be
counted as successful migrations. Batch runs use the same classification for
each workbook.

Authenticated Tableau Server evidence also includes an operational risk level
and reasons, so dependency and refresh complexity can influence migration
planning without turning unavailable API data into a false pass.

The control-plane foundations are available as `evidence_manifest` for generated
Fabric metadata, `probe_desktop_reopen()` for repeated Desktop loading, and
`build_server_lineage_graph()` for normalized portfolio dependencies.

Unified quality reports also include the versioned `evidence_manifest`, keeping
CLI, batch, MCP, Notebook, and Fabric outputs on the same provenance contract.

### Project Structure

| Module | Purpose |
|--------|---------|
| `migrate.py` | CLI entry point, batch support, logging |
| `tableau_export/` | Tableau XML parsing, DAX conversion, Power Query M generation |
| `powerbi_import/` | .pbip generation, TMDL, visuals, validation, deployment |
| `tests/` | 9,500+ tests in latest full run |
| `artifacts/` | Generated .pbip projects |
| `.github/workflows/` | CI/CD pipeline (lint, test, validate, deploy) |
