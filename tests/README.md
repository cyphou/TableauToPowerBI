# tests — Migration Validation

[🏠 Overview](../README.md) · [📚 Documentation](../docs/README.md) · [🤝 Contributing](../CONTRIBUTING.md) · [📈 Dated evidence](../docs/ROADMAP.md)

Run commands from the repository root with Python 3.12+ and the development
dependencies from [CONTRIBUTING.md](../CONTRIBUTING.md). The suite mixes
`unittest.TestCase` and pytest-discovered tests; use pytest as CI does.
Current counts and outcomes belong to [CI](https://github.com/cyphou/TableauToPowerBI/actions),
not a hard-coded total in this guide.

## Running tests

```powershell
# Smallest regression set for CLI/local browser contracts
python -m pytest tests\test_cli_wiring.py tests\test_local_web_ui.py -q

# Focus a conversion or model change
python -m pytest tests\test_dax_converter.py tests\test_tmdl_generator.py -q

# Full suite, when the scope warrants it
python -m pytest tests\ -q

# Configured coverage scope
python -m coverage run -m pytest tests\
python -m coverage report --fail-under=80
```

## Test areas

| Area | Representative tests |
|------|----------------------|
| Extraction | `test_extraction.py`, `test_extract_coverage.py` |
| Native refresh bindings | `test_native_refresh_binding.py`, `test_csv_partition_wiring.py`, `test_datasource_extractor.py` |
| DAX and M | `test_dax_converter.py`, `test_m_query_builder.py` |
| TMDL and PBIR | `test_tmdl_generator.py`, `test_pbip_generator.py`, `test_visual_generator.py` |
| Static openability | `test_openability.py` |
| CLI and local browser backend | `test_cli_wiring.py`, `test_local_web_ui.py` |
| Upload API | `test_sprint110_api_server.py`, `test_api_v2.py`, `test_upload_source_confinement.py` |
| Prep dialect and binding | `test_prep_flow_parser.py`, `test_prep_remap_conversion.py` |
| Specialist ownership | `test_agent_ownership.py` |

The loopback browser runner, optional upload UI and REST upload API have
different contracts. Do not treat API-upload tests as local-browser acceptance,
or static/fixture checks as live Power BI/Tableau execution.

## Native refresh-binding regressions

The correction slice recorded on **2026-10-22** passed **863 targeted tests**:
771 affected-path tests plus 92 dedicated datasource-extractor tests, in
separate non-overlapping runs. The combined selector is:

```powershell
python -m pytest `
    tests\test_native_refresh_binding.py `
    tests\test_m_query_builder.py `
    tests\test_extract_tableau_data.py `
    tests\test_extract_coverage.py `
    tests\test_tmdl_generator.py `
    tests\test_csv_partition_wiring.py `
    tests\test_upload_source_confinement.py `
    tests\test_local_data_collection.py `
    tests\test_datasource_extractor.py -q
```

These synthetic regression checks cover original native bindings, no native
dataset reads/copies, full CSV width and declared-field projection, exact
physical names and captions, partial source schemas, pseudo-field exclusion,
and preserved Hyper/Fabric paths. The earlier **11,741-pass** full-suite
baseline predates this slice. The later local publication-candidate run on
**2026-10-23**, using Python **3.14.2**, passed **11,826 tests**, with **66 skipped**,
**1 xfailed** and **11,893 collected**. Its one expected warning comes from the
synthetic duplicate-ZIP-name fixture. These are dated local observations, not
live CI status. Skipped environment-dependent checks remain unexecuted.
Do not reinterpret these measurements as a Desktop refresh, save/reopen,
visual/value parity or deployment certificate.

## Documentation and agent checks

```powershell
python -m pytest tests\test_build_docs.py -q
python .github\scripts\build_docs.py docs .\build\docs
python scripts\validate_skills.py
python scripts\check_agent_ownership.py
```

The documentation-builder tests use temporary public placeholders to verify
allowlisted raster assets, passive SVG validation and exact preservation of the
nine existing SVGs, including the seven maintained illustrations and badges.
They also cover fail-closed rejection of symlinked, unsafe or unsupported assets,
bounded presentation HTML, code escaping, relative navigation and responsive
image sizing. They do not test migration runtime behavior.
The skill validator checks frontmatter, references, documented flags and secret
patterns. The ownership checker reports specialist source-file assignments.
Neither command performs a live Desktop or Fabric validation.

## Fixtures and optional runtime evidence

Use public sources or reserved placeholders only. Keep customer workbooks,
credentials, generated screenshots and reports outside the repository.
See the [golden fixture guide](golden/README.md), the
[public sample inventory](../examples/tableau_samples/README.md), and the
[fixture conventions](../CONTRIBUTING.md#committed-fixtures).
Do not regenerate baselines just to hide unexpected drift.

Native, network and credential-dependent tests may require explicit environment
configuration and may be skipped locally. Preserve `not_run`, skips and
unavailable states; a static pass must not become a fabricated runtime pass.

## Manual Validation

After generating the `.pbip`, verify in Power BI Desktop:

1. The project opens without errors
2. Tables and columns are correct
3. DAX measures compute correctly
4. Calculated columns are valid
5. Relationships are functional
6. Visuals display the correct data
