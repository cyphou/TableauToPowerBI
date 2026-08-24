# Documentation

Current release baseline: **v40.0.0** (see `../CHANGELOG.md` and `ROADMAP.md`).

## Guides

- [POWERBI_PROJECT_GUIDE.md](POWERBI_PROJECT_GUIDE.md) — Understanding and using `.pbip` projects
- [MAPPING_REFERENCE.md](MAPPING_REFERENCE.md) — Tableau ↔ Power BI mappings (190 visuals, formulas, interactions)
- [TABLEAU_TO_DAX_REFERENCE.md](TABLEAU_TO_DAX_REFERENCE.md) — Complete 133+-function Tableau → DAX mapping
- [TABLEAU_TO_POWERQUERY_REFERENCE.md](TABLEAU_TO_POWERQUERY_REFERENCE.md) — Complete 108-property Tableau → Power Query M mapping (25 connectors)
- [TABLEAU_PREP_TO_POWERQUERY_REFERENCE.md](TABLEAU_PREP_TO_POWERQUERY_REFERENCE.md) — Complete 165-operation Tableau Prep → Power Query M transformation mapping
- [FAQ.md](FAQ.md) — Frequently asked questions

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
```

Use `python migrate.py --help` for the concise interface. Existing flag-based
automation remains compatible through `python migrate.py --advanced-help`.

### Project Structure

| Module | Purpose |
|--------|---------|
| `migrate.py` | CLI entry point, batch support, logging |
| `tableau_export/` | Tableau XML parsing, DAX conversion, Power Query M generation |
| `powerbi_import/` | .pbip generation, TMDL, visuals, validation, deployment |
| `tests/` | 9,150+ tests in latest full run |
| `artifacts/` | Generated .pbip projects |
| `.github/workflows/` | CI/CD pipeline (lint, test, validate, deploy) |
