<p align="center">
  <img src="images/logo-migration-dashboards.png" alt="Tableau to Power BI migration" width="140"/>
</p>

# Documentation

Reference documentation for the Tableau to Power BI migration engine.

## Guides

- [USER_MANUAL.md](USER_MANUAL.md) — Operator guide, workflows, validation states, and Tableau ↔ Power BI correspondence table
- [POWERBI_PROJECT_GUIDE.md](POWERBI_PROJECT_GUIDE.md) — Understanding and using `.pbip` projects
- [MAPPING_REFERENCE.md](MAPPING_REFERENCE.md) — Tableau ↔ Power BI mappings (190 visuals, formulas, interactions)
- [TABLEAU_TO_DAX_REFERENCE.md](TABLEAU_TO_DAX_REFERENCE.md) — Complete 133+-function Tableau → DAX mapping
- [TABLEAU_TO_POWERQUERY_REFERENCE.md](TABLEAU_TO_POWERQUERY_REFERENCE.md) — Complete 108-property Tableau → Power Query M mapping (25 connectors)
- [TABLEAU_PREP_TO_POWERQUERY_REFERENCE.md](TABLEAU_PREP_TO_POWERQUERY_REFERENCE.md) — Complete 165-operation Tableau Prep → Power Query M transformation mapping
- [FAQ.md](FAQ.md) — Frequently asked questions
- [ROADMAP.md](ROADMAP.md) — Release gates, verified evidence, and active semantic-validation work
- [LOCAL_WEB_UI.md](LOCAL_WEB_UI.md) — Local browser UI, launcher, and runtime requirements
- [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) — Fabric deployment and client-side Desktop opening validation
- [SERVER_MIGRATION_GUIDE.md](SERVER_MIGRATION_GUIDE.md) — Server/Cloud migration and optional official Tableau MCP evidence

## Quick Reference

### CLI Commands

```bash
python migrate.py batch ./workbooks --output-dir /tmp/out
python migrate.py assess file.twbx
python migrate.py batch dir/ --output-dir /tmp/out
python migrate.py server https://tableau.example "Sales Dashboard"
python migrate.py merge wb1.twbx wb2.twbx
python migrate.py fabric dir/ --output-dir /tmp/fabric-out
# Deploy generated projects separately with the MCP deploy tool (preview first).
python migrate.py qa ./workbooks
python migrate.py quality file.twbx
python migrate.py parity file.twbx
python migrate.py portfolio ./workbooks
python migrate.py plan https://tableau.example
python migrate.py lineage ./prep_flows
python migrate.py package ./workbooks
```

Use `python migrate.py quality file.twbx` after migrating the workbook to
inspect the combined assessment, parity, data, interface, and openability report.
`plan` requires a Tableau Server/Cloud URL. Add `--quality-policy report|enterprise|production`
to control escalation of static semantic diagnostics and unresolved lineage.
Use `python migrate.py --help` for the
concise command list. Supported flag-based automation remains available through
batch mode; direct single-file project generation has been retired.
`python migrate.py --advanced-help` lists advanced and legacy options.

Semantic-context checks for converted LOD expressions are available through
`powerbi_import.semantic_execution_validator`. They are static diagnostics only;
the optional `powerbi_import.semantic_runtime` adapter accepts an injected
executor and records bounded DAX query evidence as `not_run`, `passed`, or
`failed`. The unified quality report continues to label live semantic execution
as `not_run` until an authorized execution environment supplies evidence.

Quality-surface coverage is explicit: the file-based diagnostic CLI and MCP
`quality_report` tool run the unified report. The `qa FOLDER` shortcut follows
batch migration and does not invoke the legacy specialized QA report card; the
legacy `--qa` flag is retained but does not invoke that report card in batch mode.
Generation is batch-only; one-workbook migrations use a folder containing just
that workbook. Batch migration writes unified quality reports by default.
Notebook workflows expose their own migration/assessment operations and do not
implicitly run the unified quality report for every item.

In an MCP session, an owning agent can then call `agent_handoff` with its own
agent name to pull only its matching quality priorities. The packet is
read-only. The separate `agent_handoff_ack` tool accepts `not_applicable` with
a rationale, or `applied` only after PBIP definition files change and a fresh
quality report no longer contains the same finding. ACKs remain in the MCP
process memory; they are not durable resolution history.

Notebook sessions can run the same report explicitly after generation:
`session.quality_report()`. It writes JSON and HTML evidence beside the generated
project and keeps live semantic execution marked `not_run` until an authorized
runtime is used.

For local browser setup and launch options, see the
[Local browser UI guide](LOCAL_WEB_UI.md).

Desktop probe results are evidence gates: a successful launch can produce
`DESKTOP_SMOKE_PASS`, while a crash, timeout, or probe error downgrades the
confidence level to `UNVERIFIED` even when static validation passed. Two
successful consecutive launches produce `DESKTOP_REOPEN_PASS`; this still does
not claim that Desktop saved project changes. The batch opening service also
records `OPENED`/`CRASHED` and a separate model data status; only `OPENED` plus
`verified` qualifies as a data-bearing screenshot. Its HTML/JSON/PNG evidence
must remain outside the repository for customer reports.

The migration-level zero-touch tracker (`ZeroTouchTracker` in
`powerbi_import/feedback_loop.py`) uses the unified quality status when recording
history: blockers are `quality_blocker` and cannot count as successful
migrations. This is distinct from preceptor coaching: that review writes a
report, but does not dispatch fixes to owning agents. Batch runs use the same
zero-touch classification for each workbook.

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
| `tests/` | Unit, integration, and regression tests; see CI for current results |
| `artifacts/` | Generated .pbip projects |
| `.github/workflows/` | CI/CD pipeline (lint, test, validate, deploy) |
