# User Manual — Tableau to Power BI Migration

This manual explains how to run a Tableau to Power BI migration with this repository, what files are produced, and how common Tableau concepts map to Power BI. It is written for migration operators, BI developers, and reviewers who need to understand the generated output without reading the source code.

## 1. What This Tool Does

The migration engine converts Tableau workbooks (`.twb`, `.twbx`) and Tableau Prep flows (`.tfl`, `.tflx`) into Power BI artifacts.

Default output:

- Power BI Project (`.pbip`)
- PBIR v4.0 report definition
- TMDL semantic model
- Power Query M partitions
- Validation, parity, quality, and handoff reports

Optional output:

- Fabric-native Lakehouse, Dataflow Gen2, Notebook, Direct Lake Semantic Model, Report, and Pipeline scaffold
- Shared semantic model plus thin reports
- Deployment packaging and gateway/credential templates

The pipeline is always:

```text
Tableau source -> extraction JSON -> Power BI/Fabric generation -> validation evidence
```

## 2. Before You Start

Requirements:

- Python 3.12 or later
- No Python package install is required for core migration
- Power BI Desktop Developer Mode for opening `.pbip` projects
- Optional `tableauhyperapi` for proprietary Hyper v2+ extract reading
- Optional Azure/Fabric credentials only for deployment or live validation

Recommended pre-flight command:

```bash
python migrate.py assess workbook.twbx
```

Use assessment before migration when the workbook has many data sources, custom SQL, complex calculations, security filters, or Tableau Server dependencies.

## 3. Common Workflows

### Migrate One Workbook

```bash
python migrate.py migrate workbook.twbx
```

The generated PBIP project is written to the default output folder unless you pass `--output-dir`.

### Choose an Output Folder

```bash
python migrate.py migrate workbook.twbx --output-dir ./output
```

### Run Quality Evidence

```bash
python migrate.py quality workbook.twbx
```

The quality report combines assessment, parity, lineage, static openability, and evidence status. Runtime checks remain `not_run` unless an authorized executor is supplied.

### Batch Migrate a Folder

```bash
python migrate.py batch ./workbooks --output-dir ./output
```

Batch mode isolates each workbook. A failure in one workbook does not rewrite another workbook's evidence.

### Generate Fabric Artifacts

```bash
python migrate.py fabric workbook.twbx --output-dir ./fabric-output
```

Fabric output is local scaffolding until it is bound to an authorized Fabric workspace.

### Package a Migration Handoff

```bash
python migrate.py package workbook.twbx
```

Use the package when another operator or review board needs the source inventory, target artifacts, quality evidence, known differences, and credential template in one deliverable.

## 4. Output Folder Guide

A typical PBIP migration produces:

| Output | Purpose |
|---|---|
| `<Report>.pbip` | Power BI project entry file |
| `<Report>.Report/` | PBIR report pages, visuals, bookmarks, report metadata |
| `<Report>.SemanticModel/` | TMDL tables, measures, relationships, roles, cultures, M partitions |
| `Data/` | Staged CSV files, including converted Hyper extract data when applicable |
| `openability_report.json` | Static check that the PBIP structure, JSON, TMDL, DAX, and M are valid |
| `migration_metadata.json` | Source, output, and run metadata |
| `lineage_map.json` | Source-to-target lineage evidence |
| `credentials_template.json` | Placeholder credential and gateway handoff information |
| Quality HTML/JSON reports | Human-readable and machine-readable migration evidence |

Do not edit generated files as the long-term fix. If the output is wrong, fix the extractor or generator and rerun the migration.

## 5. Understanding Validation States

| State | Meaning |
|---|---|
| `PASS` | Static evidence found no blockers |
| `WARN` | Migration completed, but review or environment decisions remain |
| `FAIL` | A blocking issue was found |
| `not_run` | A runtime check requires an authorized environment and was not executed |
| `STATIC_PASS` | Local PBIP/TMDL/PBIR/M/DAX checks passed |
| `DESKTOP_SMOKE_PASS` | Power BI Desktop process launched and survived the smoke window; it does not prove document correctness by itself |
| `OPERATIONAL_100` | Reserved for authorized Desktop, semantic execution, refresh, deployment, and post-deploy evidence |

Static validation is intentionally conservative. It must not claim Desktop or Fabric success unless those environments actually supplied evidence.

## 6. Tableau to Power BI Correspondence Table

This table summarizes the practical migration target for common Tableau objects. For exhaustive visual and formula mappings, see [MAPPING_REFERENCE.md](MAPPING_REFERENCE.md), [TABLEAU_TO_DAX_REFERENCE.md](TABLEAU_TO_DAX_REFERENCE.md), and [TABLEAU_TO_POWERQUERY_REFERENCE.md](TABLEAU_TO_POWERQUERY_REFERENCE.md).

| Tableau concept | Power BI target | Migration behavior | Review notes |
|---|---|---|---|
| Workbook | PBIP project | Generates report and semantic model folders | Open the `.pbip` in Power BI Desktop Developer Mode |
| Dashboard | PBIR report page | Dashboard objects become page visuals, text, images, slicers, or controls | Check page size and layout after generation |
| Worksheet | PBIR visual container | Mark type and shelves map to a Power BI visual type | Some Tableau sheets are intentionally tables, cards, or treemaps based on shelf structure |
| Story | Bookmark set | Story points become Power BI bookmarks | Review navigation order and bookmark state |
| Datasource | Power Query M partition or Fabric source | Connector metadata becomes M source logic or Fabric scaffold | Credentials are never migrated; configure them in Power BI/Fabric |
| Tableau extract `.hyper` | CSV staging plus M `Csv.Document` or inline table | Hyper data is staged in `Data/` and referenced by the semantic model | Proprietary Hyper v2+ may need optional `tableauhyperapi` |
| Table | TMDL table | Columns, partitions, and relationships are generated | Review relationship direction and cardinality |
| Column | TMDL column | Data type, hidden state, data category, sort metadata, and descriptions are preserved where possible | Special characters are quoted in M and TMDL |
| Measure / calculated field | DAX measure or calculated column | Classification depends on aggregation, row context, and references | Validate complex calculations and table calculations |
| Tableau parameter | What-If table, field parameter, or selected-value measure | Range/list values become parameter tables; labels become Name columns | Use slicers to drive parameter selection |
| Filter shelf | Visual/page/report filter or slicer | Scope is inferred from source location and dashboard controls | Top-N remains intentionally absent unless a verified PBIR target exists |
| Context filter | Report-level or shared filter semantics | Represented as a normal filter with documented scope | Confirm order-sensitive Tableau behavior manually if critical |
| Data source filter | Report/data-scope filter | Generated as target filter evidence where expressible | Verify it remains a data-scope restriction, not only UI filtering |
| Filter/highlight action | Native cross-filter/cross-highlight behavior | PBIR visuals keep cross-filtering enabled (`drillFilterOtherVisuals`) | No extra button is created for native interactions |
| URL action with static URL | Action button with WebUrl | Static links become Power BI action buttons | Dynamic row links are better represented as WebUrl columns |
| URL action pointing to a field | TMDL column with `dataCategory: WebUrl` | Direct field targets become row-level Web URL columns | Complex interpolated URLs remain suppressed rather than emitted as wrong static links |
| Navigation action | Page navigation button or bookmark target | Static navigation can become an action button | Verify target page names after generation |
| Set | Boolean calculated column | Membership becomes a true/false field | Cross-table sets may use DAX fallback |
| Group | SWITCH/display column | Group labels map source values to output labels | Review group membership for null and fallback values |
| Bin | Calculated bin column | Uses numeric bucketing such as `FLOOR` | Confirm boundary behavior around negatives and nulls |
| Hierarchy | TMDL hierarchy | Levels are written into the semantic model | Validate drill order in Power BI |
| Sort order | Sort-by-column or visual sort state | Sort metadata is written where a target visual/model field exists | Unplaced worksheets do not become visuals |
| Alias for measure name | Named DAX measure | Tableau aggregate aliases become explicit measures | Original references are not renamed |
| Alias for values | Display calculated column | Value labels become `<field> (Display)` columns | Original source column remains for filtering and joins |
| Row-level security / user filter | TMDL role | Tableau user filters become Power BI RLS expressions and role files | Assign Azure AD users/groups in the target service |
| Custom SQL | Power Query `Value.NativeQuery` | Query text is preserved where possible | Review folding, gateway, and credential requirements |
| Published datasource | Shared source binding or handoff note | Source identity is preserved for deployment binding | Requires service-side decision |
| Data blending | Relationship or merge strategy | Real blends become model relationships; parameter pseudo-blends are ignored | Verify cardinality and direction |
| Reference line | PBIR analytics/reference line metadata | Constant and supported dynamic lines become `referenceLine` settings | Compare analytics pane behavior in Desktop |
| Trend line | Power BI analytics pane approximation | Regression type is mapped where possible | Statistical behavior can differ |
| Custom geocoding | Data category or map resource | Geographic roles and shape resources are passed through where possible | Manual review often required |
| Linguistic schema | Culture TMDL / Q&A metadata | Synonyms and entity metadata become semantic model culture metadata | Review for Copilot and Q&A relevance |
| Tableau Prep flow | Power Query M, source definitions, lineage report | Standalone flows export Power Query and lineage rather than PBIP | Use `python migrate.py lineage ./prep_flows` for cross-flow analysis |

## 7. Recommended Review Checklist

After migration:

1. Open the `.pbip` in Power BI Desktop Developer Mode.
2. Confirm data source credentials and gateways.
3. Check model relationships and cardinality.
4. Review DAX measures flagged by quality or parity reports.
5. Compare core dashboards against Tableau screenshots or known totals.
6. Test slicers, parameters, bookmarks, and navigation.
7. Review RLS roles before publishing.
8. Read the quality report and resolve blockers before deployment.
9. Keep runtime checks marked `not_run` until an authorized environment supplies evidence.

## 8. Troubleshooting

| Symptom | First place to check | Typical cause |
|---|---|---|
| PBIP will not open | `openability_report.json` | Invalid JSON, TMDL, DAX, M, or broken model/report reference |
| Data is missing | `Data/`, M partitions, datasource assessment | Credentials, unsupported source, or Hyper reader limitation |
| Visual is a table instead of a chart | Quality report and worksheet shelves | Tableau mark/shelf structure may only support a table/card/treemap target |
| Filter missing | Interface coverage and parity evidence | Tableau filter may be all-selected, virtual, or unsupported Top-N |
| URL action missing | Parity report | Dynamic placeholder URL was suppressed to avoid a wrong static button |
| Calculation returns blank or TODO | DAX validation and migration report | Unsupported Tableau function or ambiguous table calculation |
| Runtime evidence says `not_run` | Quality report runtime section | No authorized Desktop/Fabric/semantic executor was supplied |

## 9. Operating Rules

- Do not put secrets in commands, logs, chat, screenshots, or documentation.
- Treat generated artifacts as output, not source-of-truth code.
- Prefer `quality` and `package` when handing work to another person.
- Do not claim production readiness from static validation alone.
- Keep approximations visible; do not hide them by changing thresholds.
