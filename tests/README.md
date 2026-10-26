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
| Complete Hyper exports and Import | `test_hyper_full_import.py`, `test_migrate_hyper.py` |
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

These synthetic regression checks predate the packaged-Excel binary-copy
exception. They cover original native bindings, no native
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

## Complete Hyper export and Import regressions

```powershell
.\.venv\Scripts\python.exe -m pytest `
    tests\test_hyper_full_import.py `
    tests\test_migrate_hyper.py `
    tests\test_datasource_extractor.py `
    tests\test_csv_partition_wiring.py `
    tests\test_security_validator.py `
    tests\test_migration_quality.py -q
```

These synthetic tests exercise complete row/schema/hash evidence, rejection of
PBIP row caps and sample-only readers, exact XML source identities and final
Import bindings, and rollback after failed publication. Native sources without
Hyper and the opaque packaged-Excel copy keep their separate contracts.

The metadata-containment cases (`TestLexicalPathKey`,
`TestHyperMetadataPathContainment`, `TestHyperMetadataLinkContainment`) feed
UNC, device-namespace and link-routed paths to the Hyper checks. They create
real symlinks or junctions where the platform allows (otherwise they skip) and
run under the guard in `link_test_helpers.py`, which records and refuses every
call that would follow a link, so no run can reach a network share.

Two cases use the optional Hyper SDK to create valid native fixtures: an export
to an accented path with spaces/apostrophes, and a genuinely generated Hyper
larger than 10 MiB that must select Parquet. They skip only when the SDK is
absent. The low-level diagnostic API may still produce explicitly labeled
samples; those do not satisfy complete PBIP migration. Passing these tests
verifies static exports/bindings, not a Desktop refresh, saved cache or rendered
comparison.

## Microsoft authoring contract regressions

PBIR regressions cover flat bookmark files and index references, legacy
bookmark discovery, per-field formatting, funnel capabilities and category-less
Gantt approximations. These tests use local synthetic fixtures; they do not
require Node.js, model-authoring MCP or a Power BI connection.

For an independent check of generated reports, follow the
[optional Microsoft PBIR validation procedure](../docs/POWERBI_PROJECT_GUIDE.md#optional-microsoft-pbir-validation).
Record the validator version, the exact generated corpus and its diagnostics
separately from pytest results and unified migration quality. Skipping remote
schemas with `--no-schema` is an offline check, not full schema validation.
Neither mode verifies semantic-model execution, refresh or rendered parity.

The **2026-10-23** authoring follow-up ran the full suite with Python **3.14.2**:
**11,850 passed, 66 skipped, 1 xfailed, 1 warning**. This later observation is
separate from the native-binding/publication baseline above and from live CI.

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
