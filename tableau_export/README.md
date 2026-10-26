# tableau_export — Tableau Extraction

Extracts metadata from Tableau `.twb` and `.twbx` files. Normal migrations
should use the [local browser UI](../docs/LOCAL_WEB_UI.md) or the batch CLI;
the extraction entry point below is for development and diagnosis.

## Modules

### `extract_tableau_data.py`

Extraction orchestrator. Safely parses Tableau XML, reads packaged workbooks
and coordinates datasource, worksheet, dashboard and other extractors.

```bash
python tableau_export/extract_tableau_data.py your_workbook.twbx
```

### `datasource_extractor.py`

Main extractor. For each Tableau datasource:

- **Physical tables**: name, columns, data types, per-table connection
- **Calculations**: raw formula, caption, role (dimension/measure), type
- **Relationships**: joins between tables (LEFT, INNER, FULL)
- **Connections**: Excel, CSV, GeoJSON, SQL Server, PostgreSQL
- **Parameters**: names and default values

For active Hyper sources, physical declarations determine the columns, rename
maps, types and calculation routing. Historical native declarations remain
provenance only. Logical groups and table/object pseudo-fields are not physical
source columns; categorical-bin memberships are extracted as groups, not
duplicate numeric bins.

### `dax_converter.py`

Converts Tableau formulas to DAX and preserves diagnostics for approximate or
unsupported expressions. See the [DAX reference](../docs/TABLEAU_TO_DAX_REFERENCE.md).

### `m_query_builder.py`

Generates Power Query M connections and transformation steps. Its 97 dispatch
keys include aliases for 55 distinct connector generator functions; live
connectivity and refresh are separate checks.

### Prep, Hyper and Server helpers

- `prep_flow_parser.py` parses supported Prep dialects into M; standalone
  flows export M/source definitions, not empty PBIP reports.
- `prep_flow_analyzer.py` supplies per-flow schema, graph and assessment data.
- `hyper_reader.py` uses optional `tableauhyperapi`, SQLite and metadata-scan
  fallbacks with bounded archive copies and header scans. Metadata-only
  extraction may supply a bounded sample, never complete data; `.tde` is not
  read. Packaged Hyper snapshots remain Import sources; PBIP orchestration
  requires full CSV/Parquet exports and exact Import bindings rather than
  using metadata samples as successful row recovery. Row caps/sample-only
  readers or unresolved required bindings block a full PBIP migration:
  [automatic mode and export diagram](../docs/ARCHITECTURE.md#automatic-storage-mode-and-hyper-export).
- `server_client.py` handles authorized Tableau Server/Cloud REST access.

## Output

The extractor emits 23 JSON object types in its configured extraction
directory. The migration orchestrator manages per-workbook snapshots; use
`TTPBI_EXTRACT_DIR` when isolating a direct diagnostic extraction.

| File | Content |
|------|---------|
| `datasources.json` | Tables, columns, calculations, relationships, connections (**used by the pipeline**) |
| `worksheets.json` | Sheets and visuals |
| `dashboards.json` | Dashboards |
| `calculations.json` | Calculated fields |
| `parameters.json` | Parameters |
| `filters.json` | Filters |
| `stories.json` | Stories |
| `actions.json` | Filter, highlight, URL, navigation, parameter and set actions |
| `sets.json` | Set definitions |
| `groups.json` | Manual groups |
| `bins.json` | Bin definitions |
| `hierarchies.json` | Drill hierarchies |
| `sort_orders.json` | Sort metadata |
| `aliases.json` | Field value aliases |
| `custom_sql.json` | Custom SQL definitions |
| `user_filters.json` | User filters and security mappings |
| `hyper_files.json` | Hyper extraction metadata and available rows |
| `datasource_filters.json` | Extract/datasource-level filters |
| `custom_geocoding.json` | Custom geographic definitions |
| `published_datasources.json` | Published datasource references |
| `data_blending.json` | Cross-datasource blending metadata |
| `table_extensions.json` | Table-extension definitions |
| `linguistic_schema.json` | Linguistic/Q&A metadata |

These files are not merely informational: generation consumes the relevant
object types for the semantic model, pages, visuals, filters, bookmarks,
security and quality evidence. The contract is
`TableauExtractor.EXTRACTION_OUTPUT_TYPES` in
[`extract_tableau_data.py`](extract_tableau_data.py).
