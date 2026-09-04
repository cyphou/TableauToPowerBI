# Documentation

Current release baseline: **v44.0.0** with the verified PBIP manifest-coherence and static-openability hardening from 2026-09-03 (see `../CHANGELOG.md` and `ROADMAP.md`).

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
data, interface, and openability report. Use `python migrate.py --help` for the
concise command list; existing flag-based automation remains compatible through
`python migrate.py --advanced-help`.

Semantic-context checks for converted LOD expressions are available through
`powerbi_import.semantic_execution_validator`. They are static diagnostics only;
the unified quality report continues to label live semantic execution as
`not_run` until an authorized execution environment supplies evidence.

Quality-surface coverage is explicit: the concise single-workbook CLI and MCP
`quality_report` tool run the unified report; `qa` remains the specialized
real-world report card. Batch and Notebook workflows currently expose their own
migration/assessment operations and do not implicitly run the unified quality
report for every item.

Notebook sessions can run the same report explicitly after generation:
`session.quality_report()`. It writes JSON and HTML evidence beside the generated
project and keeps live semantic execution marked `not_run` until an authorized
runtime is used.

Desktop probe results are evidence gates: a successful launch can produce
`DESKTOP_SMOKE_PASS`, while a crash, timeout, or probe error downgrades the
confidence level to `UNVERIFIED` even when static validation passed.

The feedback loop uses the unified quality status when recording zero-touch
history: quality blockers are classified as `quality_blocker` and cannot be
counted as successful migrations. Batch runs use the same classification for
each workbook.

Authenticated Tableau Server evidence also includes an operational risk level
and reasons, so dependency and refresh complexity can influence migration
planning without turning unavailable API data into a false pass.

### Project Structure

| Module | Purpose |
|--------|---------|
| `migrate.py` | CLI entry point, batch support, logging |
| `tableau_export/` | Tableau XML parsing, DAX conversion, Power Query M generation |
| `powerbi_import/` | .pbip generation, TMDL, visuals, validation, deployment |
| `tests/` | 9,500+ tests in latest full run |
| `artifacts/` | Generated .pbip projects |
| `.github/workflows/` | CI/CD pipeline (lint, test, validate, deploy) |
