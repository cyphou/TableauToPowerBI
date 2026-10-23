# Tableau Version Compatibility

This document describes which Tableau Desktop versions and features are supported by the migration tool.

Support below describes parser/generator scope, not certification of every
workbook produced by a Tableau release. The source revision, exact feature
dialect, generated quality report and native validation determine the result.
See [known limitations](KNOWN_LIMITATIONS.md) and the
[Prep parser contract](TABLEAU_PREP_TO_POWERQUERY_REFERENCE.md#parser-scope-and-workbook-binding-contract-reports-8183).

---

## Supported File Formats

| Format | Extension | Support |
|--------|-----------|---------|
| Tableau Workbook | `.twb` | Parsed; unsupported features remain explicit |
| Tableau Packaged Workbook | `.twbx` | Parsed; embedded-source identity and availability are validated |
| Tableau Data Source | `.tds` | Datasource extraction; not a complete dashboard/report source |
| Tableau Packaged Data Source | `.tdsx` | Packaged datasource extraction |
| Tableau Prep Flow | `.tfl` | Bounded supported dialects; standalone output is M/source definitions, not PBIP |
| Tableau Packaged Prep Flow | `.tflx` | Same bounded Prep contract |
| Hyper Extract | `.hyper` | Rows when a reader supports the format; metadata-only fallback otherwise |

## Tableau Desktop Version Matrix

| Tableau Version | XML Format | Support Level | Notes |
|----------------|------------|---------------|-------|
| **2018.x** | Classic XML with `<column[@param-domain-type]>` | ✅ Parsed patterns | Basic parameter format |
| **2019.x** | Added `<parameters><parameter>` XML format | ✅ Parsed patterns | Both parameter formats handled |
| **2020.x** | Extended mark types (Histogram, Box Plot, etc.) | ✅ Mapping patterns | Native, approximate and custom-visual outcomes differ |
| **2021.x** | Enhanced relationship model, federated connections | ⚠️ Feature-dependent | Exact join/relationship predicates must be supported |
| **2022.x** | Object Model relationships, viz-in-tooltip | ⚠️ Feature-dependent | Tooltip pages generated; logical relationships need review |
| **2023.x** | Set actions, parameter actions | ⚠️ Feature-dependent | Extraction/mapping is not proof of equivalent interactions |
| **2024.1–2024.2** | Story point enhancements | ✅ Mapping patterns | Stories → PBI bookmarks; review rendered behavior |
| **2024.3+** | Dynamic zone visibility, database-query parameters | ⚠️ Partial | Metadata and generation patterns exist; behavior/refresh require review, not release-wide certification |

## Feature Compatibility by Tableau Version

### Parameters

| Feature | Introduced | Support |
|---------|-----------|---------|
| Basic parameters (string, int, float) | 2018.x | ✅ |
| Range parameters (min/max/step) | 2018.x | ✅ → GENERATESERIES table |
| List parameters | 2018.x | ✅ → DATATABLE table |
| `<column[@param-domain-type]>` format | 2018.x (classic) | ✅ |
| `<parameters><parameter>` format | 2019.x (modern) | ✅ |
| Parameter actions | 2023.x | ✅ Extracted |
| Database-query parameters | 2024.3+ | ⚠️ Extracted; generated native-query partitions require configured connections and refresh validation |

### Calculations & LOD

| Feature | Introduced | Support |
|---------|-----------|---------|
| Basic calculated fields | All | ✅ |
| FIXED LOD expressions | 2015.x | ✅ → CALCULATE(AGG, ALLEXCEPT) |
| INCLUDE LOD expressions | 2015.x | ✅ → CALCULATE(AGG) |
| EXCLUDE LOD expressions | 2015.x | ✅ → CALCULATE(AGG, REMOVEFILTERS) |
| Nested LOD | All | ⚠️ Approximated |
| Table calculations (RUNNING, RANK, WINDOW) | All | ⚠️ Approximated |

### Data Sources

| Feature | Introduced | Support |
|---------|-----------|---------|
| File-based (Excel, CSV) | All | ✅ |
| Database (SQL Server, PostgreSQL, MySQL, Oracle) | All | ✅ |
| Cloud (BigQuery, Snowflake, Databricks) | Various | ✅ |
| SAP (HANA, BW) | Various | ⚠️ Basic M query |
| GeoJSON, JSON, XML, PDF | Various | ✅ |
| Custom SQL | All | ✅ |
| Federated connections | 2020.x | ✅ |
| Relationships (Object Model) | 2020.x | ✅ |
| OAuth connections | All | ❌ Credentials stripped |

### Dashboard Features

| Feature | Introduced | Support |
|---------|-----------|---------|
| Worksheets, text, images | All | ✅ |
| Filter controls (quick filters) | All | ✅ → PBI slicers |
| Dashboard actions (filter, highlight, URL) | All | ✅ |
| Sheet-navigate actions | 2019.x | ✅ → PageNavigation buttons |
| Set actions | 2023.x | ✅ Extracted |
| Device layouts (phone) | 2018.x | ✅ → Mobile pages |
| Floating layout | All | ✅ |
| Tiled layout | All | ✅ |
| Viz-in-tooltip | 2019.x | ✅ → Tooltip pages |
| Layout containers | All | ✅ (shallow nesting) |
| Dynamic zone visibility | 2024.3+ | ⚠️ Conditions extracted; bookmark/visibility mapping is not proof of equivalent interaction |

### Security

| Feature | Introduced | Support |
|---------|-----------|---------|
| User filters | All | ✅ → PBI RLS roles |
| USERNAME() / FULLNAME() | All | ✅ → USERPRINCIPALNAME() |
| ISMEMBEROF() | All | ✅ → Per-group RLS roles |
| USERDOMAIN() | All | ⚠️ Returns empty string |
| Row-level security | All | ✅ |

## Tableau Prep Version Matrix

| Tableau Prep Version | Support | Notes |
|---------------------|---------|-------|
| 2019.x–2024.x | ⚠️ Dialect-dependent | Supported DAG/container, Clean/Join/Aggregate/Union/Pivot operations; do not infer complete release support |
| LoadHyper steps | ⚠️ Reader-dependent | Inspect the flow assessment, source availability and generated M; no general data-load pass is implied |
| Wildcard union | ✅ | `Folder.Files` M pattern |
| Expression functions | ✅ | Converted via dax_converter |

## Power BI Desktop Requirements

- **Required capability**: PBIP/PBIR v4.0 and TMDL support in the installed Desktop build
- **Version evidence**: No minimum Desktop build is certified by the repository's static checks alone
- **Current generator metadata**: report 2.0.0, page 2.1.0, visualContainer 2.7.0, bookmark 2.1.0; theme name `CY26SU04`, import report version `5.58`
- **Validation**: Record actual opening, refresh, save/reopen and rendered checks separately; unavailable runtime checks remain `not_run`
- **PBIR format**: Must be enabled (default in recent versions)
- **TMDL format**: Must be supported (default in Power BI Desktop)
