# 🔄 Tableau → Power BI

**Automated Migration Tool** — convert Tableau workbooks (`.twb`/`.twbx`) to Power BI projects (`.pbip`) in seconds, fully automated, zero manual rework.

| | |
|---|---|
| 🏷️ **Version** | 44.0.0 |
| ✅ **Tests** | 9,500+ passed (Python) · 38 extension unit tests |
| 🐍 **Python** | 3.12+ · zero external dependencies |
| 📜 **License** | MIT |

| 🎯 **Capabilities** | 133+ DAX conversions · 190 visual types · 87 connectors · 23 object types |

### What is new in v44.0.0

- **Copilot and MCP agent surface**: the repository-scoped skill, stdio MCP server, grounded conversational assessment, remediation routing, parity scanning, and guarded deployment tools are documented in `docs/AGENT_SURFACE.md`.
- **Openability verification and auto-healing**: PBIP output is checked by default for structure, JSON, TMDL, Power Query M, DAX, schemas, and report/model references. Deterministic DAX/M/visual healing and optional offline-first LLM correction are available through the CLI and MCP surface.
- **Safety-first automation**: deployment is dry-run by default, credentials stay in environment variables, LLM refinement is opt-in, and fixes are applied only after re-validation.
- **VS Code Extension**: assess, preview DAX, and migrate workbooks without leaving the editor — workbook tree view, assessment webview, one-click migrate, status bar, and a side-by-side DAX preview with editable overrides. Includes TextMate syntax highlighting for DAX and Tableau calculations. See `docs/VS_CODE_EXTENSION.md`.
- **Interactive Notebook API v2**: `MigrationSession` gains interactive assessment (radar SVG), a filterable DAX explorer, a Mermaid relationship diagram, and step-by-step extract/convert/generate/validate helpers for Jupyter.
- **Plugin SDK v2**: a versioned `MigrationPlugin` base class with formal hooks, manifest validation, error-isolated dispatch, and a `PluginTestRunner`. Backward compatible with the legacy hook-based plugins. See `docs/PLUGIN_SDK.md`.
- **Marketplace v2**: pattern dependency resolution, remote catalogue sync, and curated Healthcare/Finance/Retail industry packs.

### What was new in v39.0.0

- **Data Blending Engine**: cross-datasource Tableau blends are reconstructed as Power Query merge queries with primary/secondary linking fields preserved.
- **Enterprise Connector Expansion**: 8 new deep connectors with schema navigation and custom-SQL passthrough — Dremio, ClickHouse, SingleStore/MemSQL, Firebolt, Starburst/Trino, IBM Db2, Teradata, Azure Synapse.
- **Custom SQL & Native Query Depth**: a stdlib SQL analyzer detects dialect, parameters, joins and subqueries, grades query complexity, and emits parameterised `Value.NativeQuery` M. Surfaced in the migration assessment as a "Custom SQL Depth" check.
- **OAuth & Authentication Flow Migration**: Tableau auth modes map to Power BI credential types; credential template v2, Azure AD service-principal config and a PowerShell connection-test script are generated for OAuth/SP-capable connectors.

#### Pixel-perfect fidelity (4-axis coverage)

| Axis | What is preserved |
|------|-------------------|
| **Fonts** | Run-level font family, size, weight, color, and per-paragraph horizontal alignment |
| **Chrome** | Per-visual background + border from Tableau format zones |
| **Sentinel** | Tableau soft line-break sentinel runs (`Ae`/NBSP) cleaned during extraction |
| **Overlay** | Floating/overlapping zones staggered deterministically by z-order |

---

## ⚡ Quick Start

```bash
# Default: one workbook to a validated PBIP project
python migrate.py migrate your_workbook.twbx
```

> [!TIP]
> The output is a `.pbip` project (PBIR v4.0). Every PBIP migration automatically
> runs a static openability gate over project structure, JSON, TMDL, Power Query M,
> DAX, report/model references, and PBIR schemas. Use `--no-verify-open` only to
> bypass this gate deliberately. Power BI Desktop is never launched automatically;
> `--desktop-probe` remains an explicit diagnostic option.

<details>
<summary><b>📦 Installation</b></summary>

```bash
git clone https://github.com/cyphou/Tableau-To-PowerBI.git
cd Tableau-To-PowerBI
python migrate.py migrate your_workbook.twbx
```

**Requirements:** Python 3.12+ • No `pip install` needed — pure standard library.

Optional dependencies:
```bash
pip install azure-identity requests   # Fabric/PBI Service deployment
pip install tableauhyperapi           # .hyper extract file reading (v2+ format)
```
</details>

### More ways to migrate

The public CLI has **14 commands**. These cover the normal workflows:

```bash
python migrate.py migrate workbook.twbx
python migrate.py assess workbook.twbx
python migrate.py batch folder/ --output-dir ./output
python migrate.py server https://tableau.example "Sales Dashboard"
python migrate.py merge wb1.twbx wb2.twbx --model-name "Sales"
python migrate.py fabric workbook.twbx
python migrate.py deploy workbook.twbx WORKSPACE_ID
python migrate.py qa workbook.twbx
python migrate.py quality workbook.twbx
python migrate.py parity workbook.twbx
python migrate.py portfolio ./workbooks
python migrate.py plan workbook.twbx
python migrate.py lineage ./prep_flows
python migrate.py package workbook.twbx
```

Use `python migrate.py quality workbook.twbx` for one deterministic report combining
assessment, feature parity, data coverage, interface coverage, and Power BI
openability. Use `parity` for feature coverage, `portfolio` for folder assessment,
`plan` for migration waves, `lineage` for Prep flows, and `package` for a stakeholder
deliverable. Add `--quality-strict` when a CI run should fail on quality blockers.
Run `python migrate.py --help` for the concise command list. Existing flag-based
automation remains compatible; use `python migrate.py --advanced-help` only when an
advanced option is required. Secrets belong in environment variables, never in
command history.

The optional Tkinter interface remains available through
`powershell -ExecutionPolicy Bypass -File .\run_light_ui.ps1`.

---

## 🎯 Key Features

<table>
<tr>
<td width="50%">

### 🔄 Complete Extraction
Parses **23 object types** from `.twb`/`.twbx`:
datasources, calculations, worksheets, dashboards, filters, parameters, stories, actions, sets, groups, bins, hierarchies, relationships, sort orders, aliases, custom SQL, custom geocoding, published datasources, data blending, hyper metadata, datasource filters, table extensions, linguistic schema

**Hyper extract data:** `.hyper` files embedded in `.twbx` are automatically converted to CSV and wired into Power Query M expressions via a 3-tier reader chain (`tableauhyperapi` → SQLite → binary scan). Small extracts are inlined directly into `#table()` M partitions; large extracts produce `Csv.Document()` references. Legacy `.tde` files require the `tableauhyperapi` package.

</td>
<td width="50%">

### 🧮 133+ DAX Conversions
Translates Tableau formulas to DAX:
LOD expressions, table calcs, IF/ELSEIF, ISNULL, CONTAINS, window functions, iterators (SUMX), cross-table RELATED/LOOKUPVALUE, RLS security, regex patterns, SPLIT, statistical functions

</td>
</tr>
<tr>
<td>

### 📊 190 Visual Types
Maps every Tableau mark to Power BI:
bar, line, pie, scatter, map, treemap, waterfall, funnel, gauge, KPI, box plot, word cloud, Sankey, Chord, combo charts, sparklines, and more

</td>
<td>

### 🔌 87 Data Connectors
Generates Power Query M for:
SQL Server, PostgreSQL, BigQuery, Snowflake, Oracle, MySQL, Databricks, SAP HANA, Excel, CSV, SharePoint, Salesforce, Web, OData, Azure Blob, Vertica, Impala, Presto, Fabric Lakehouse, MongoDB, Cosmos DB, Athena, DB2, ServiceNow, Denodo, Essbase, Splunk, and more

</td>
</tr>
<tr>
<td>

### 🧠 Smart Semantic Model
Auto-generates Calendar table, date hierarchies, calculation groups, field parameters, RLS roles, display folders, geographic categories, number formats, perspectives, multi-language cultures

</td>
<td>

### 🚀 Deploy Anywhere
One-command deploy to **Power BI Service** or **Microsoft Fabric** with Azure AD auth (Service Principal / Managed Identity). Gateway config generation included.

</td>
</tr>
<tr>
<td>

### 🏭 Fabric-Native Output
Generate **Lakehouse + Dataflow Gen2 + PySpark Notebook + DirectLake Semantic Model + Data Pipeline** with `--output-format fabric`. This is currently a validated scaffold; operational bindings and production deployment still require environment-specific configuration and verification.

</td>
<td>

### ⚡ DAX Optimizer
`--optimize-dax` rewrites verbose DAX: nested IF→SWITCH, IF(ISBLANK)→COALESCE, constant folding, SUMX simplification. `--time-intelligence auto` auto-injects YTD, PY, YoY% measures.

</td>
</tr>
<tr>
<td>

### 🔍 QA Suite & Auto-Fix
`--qa` runs the full quality assurance pipeline in one shot: validation → auto-fix (17 Tableau→DAX leak patterns) → governance → comparison report → `qa_report.json`. Validator auto-fixes `ISNULL→ISBLANK`, `ZN→IF(ISBLANK)`, `ELSEIF→nested IF`, and more.

</td>
<td>

### 🔗 Lineage Map
Every migration produces a `lineage_map.json` tracking the provenance of every object: Tableau datasource.table → PBI table, Tableau calculation → PBI measure/column, relationships, and worksheet → page mappings. Visualized in the HTML dashboard with flow diagrams, stat cards, and searchable tabbed tables.

</td>
</tr>
<tr>
<td colspan="2">

### 🔗 Shared Semantic Model
Merge multiple Tableau workbooks into **one shared semantic model** with thin reports. Fingerprint-based table matching, Jaccard column overlap scoring, measure conflict resolution, merge assessment with 0–100 scoring, and automatic `byPath` report wiring. **Global assessment** (`--global-assess`) analyzes all workbooks pairwise to find merge clusters and generates an HTML report with a score heatmap matrix. **Fabric bundle deployment** (`--deploy-bundle`) deploys the shared model + thin reports as an atomic unit.

</td>
</tr>
</table>

> [!NOTE]
> **Zero external dependencies** for core migration. The entire engine runs on Python's standard library.

---

## ⚙️ How It Works

```mermaid
flowchart LR
    A["📄 .twbx/.twb\nTableau Workbook"] --> B["🔍 EXTRACT\n23 JSON files"]
    P["📋 .tfl/.tflx\nPrep Flow"] -.-> B
    S["☁️ Tableau Server\n(optional)"] -.-> B
    B --> C["🛠️ GENERATE\n.pbip project"]
    B --> F["🏭 GENERATE\nFabric artifacts"]
    C --> D["📊 Power BI Desktop\nOpen & validate"]
    C -.-> E["🚀 DEPLOY\nPBI Service / Fabric"]
    F -.-> E

    style A fill:#E97627,color:#fff,stroke:#E97627
    style P fill:#E97627,color:#fff,stroke:#E97627
    style S fill:#E97627,color:#fff,stroke:#E97627
    style D fill:#F2C811,color:#000,stroke:#F2C811
    style E fill:#F2C811,color:#000,stroke:#F2C811
    style B fill:#4B8BBE,color:#fff,stroke:#4B8BBE
    style C fill:#4B8BBE,color:#fff,stroke:#4B8BBE
    style F fill:#0078D4,color:#fff,stroke:#0078D4
```

**🔍 Step 1 — Extract:** Parses Tableau XML into 23 structured JSON files (worksheets, datasources, calculations, etc.)

**🛠️ Step 2 — Generate:** Converts JSON into a complete `.pbip` project with PBIR v4.0 report and TMDL semantic model

**🚀 Step 3 — Deploy** *(optional):* Packages and uploads to Power BI Service or Microsoft Fabric

### 🏭 Fabric-Native Output Mode

Use `--output-format fabric` to generate a **full Microsoft Fabric project** instead of a `.pbip`:

```mermaid
flowchart LR
    A["📄 .twbx/.twb\nTableau Workbook"] --> B["🔍 EXTRACT\n23 JSON files"]
    B --> C["⚙️ GENERATE\nFabric artifacts"]
    C --> LH["🗄️ Lakehouse\nDelta tables + DDL"]
    C --> DF["🔄 Dataflow Gen2\nPower Query M"]
    C --> NB["📓 PySpark Notebook\nETL pipeline"]
    C --> SM["📦 DirectLake\nSemantic Model"]
    C --> PL["⚡ Data Pipeline\n3-stage orchestration"]
    PL -.-> DF
    PL -.-> NB
    PL -.-> SM

    style A fill:#E97627,color:#fff,stroke:#E97627
    style B fill:#4B8BBE,color:#fff,stroke:#4B8BBE
    style C fill:#4B8BBE,color:#fff,stroke:#4B8BBE
    style LH fill:#0078D4,color:#fff,stroke:#0078D4
    style DF fill:#0078D4,color:#fff,stroke:#0078D4
    style NB fill:#0078D4,color:#fff,stroke:#0078D4
    style SM fill:#0078D4,color:#fff,stroke:#0078D4
    style PL fill:#0078D4,color:#fff,stroke:#0078D4
```

The pipeline generates **5 Fabric artifacts** from a single Tableau workbook:

| Artifact | Description |
|----------|-------------|
| **Lakehouse** | Delta table schemas, Spark SQL DDL scripts, table metadata |
| **Dataflow Gen2** | Power Query M ingestion queries with Lakehouse destinations |
| **PySpark Notebook** | ETL pipeline (9 connector templates) + transformation notebook |
| **Semantic Model** | DirectLake TMDL pointing to Lakehouse Delta tables |
| **Data Pipeline** | 3-stage orchestration: Dataflow → Notebook → Semantic Model refresh |

```bash
# Generate the complete five-artifact Fabric chain
python migrate.py fabric workbook.twbx

# With custom output directory
python migrate.py fabric workbook.twbx --output-dir /tmp/fabric_output
```

### 🔗 Shared Semantic Model Mode

When migrating multiple workbooks that share the same data sources, use `--shared-model` to produce **one shared semantic model** + **N thin reports**:

```mermaid
flowchart LR
    A1["📄 Workbook A"] --> E["🔍 EXTRACT\n(isolated)"]
    A2["📄 Workbook B"] --> E
    A3["📄 Workbook C"] --> E
    E --> M["🔗 MERGE\nfingerprint matching"]
    M --> SM["📦 Shared\nSemanticModel"]
    M --> R1["📊 Report A\n(thin)"]
    M --> R2["📊 Report B\n(thin)"]
    M --> R3["📊 Report C\n(thin)"]
    R1 -.->|byPath| SM
    R2 -.->|byPath| SM
    R3 -.->|byPath| SM

    style SM fill:#4B8BBE,color:#fff
    style R1 fill:#F2C811,color:#000
    style R2 fill:#F2C811,color:#000
    style R3 fill:#F2C811,color:#000
```

```bash
# Assess merge feasibility
python migrate.py merge wb1.twbx wb2.twbx wb3.twbx --assess-merge

# Generate shared model + thin reports
python migrate.py merge wb1.twbx wb2.twbx wb3.twbx --model-name "Shared Sales"

# Deploy shared model to Fabric workspace as a bundle
python migrate.py merge wb1.twbx wb2.twbx --deploy-bundle WORKSPACE_ID --bundle-refresh
```

The optional `--assess-merge` mode generates an interactive report with pairwise
merge scores, conflicts, and a merge recommendation. Portfolio-wide assessment and
deployment of an existing bundle are documented as advanced compatibility workflows
in [Enterprise Guide](docs/ENTERPRISE_GUIDE.md) and
[Deployment Guide](docs/DEPLOYMENT_GUIDE.md).

Reliability safeguards in shared-model mode:

- Shared semantic model fallback prevents an empty merged model when isolated-table filtering would otherwise remove every table.
- `--strict-merge` blocks generation on merge validation failures.
- `--strict-thin-report` blocks output when thin-report orphaned references exceed the allowed threshold.
- `--thin-report-max-orphans N` defines that threshold (default: `0`).

![Global Assessment — Cross-Workbook Merge Analysis](docs/images/share_assessment.png)
### 📋 Tableau Prep Flow Migration

Standalone `.tfl`/`.tflx` Prep flows are migrated **without generating a `.pbip` project** — instead, the tool produces **Power Query M expressions**, **source definitions**, **cross-flow lineage analysis**, and **merge recommendations**.

```mermaid
flowchart LR
    subgraph "Prep Flows"
        F1["📋 flow_1.tfl"]
        F2["📋 flow_2.tfl"]
        F3["📋 flow_N.tfl"]
    end

    subgraph "Per-Flow Analysis"
        AN["🔍 ANALYZE\nFlow profile\n+ assessment"]
    end

    subgraph "Per-Flow Export"
        PQ["⚡ Power Query M\n.pq files"]
        SR["📁 Sources\nConnection metadata"]
        AS["📊 Assessment\nGrade + stats"]
    end

    subgraph "Cross-Flow Lineage"
        LG["🔗 Lineage Graph\nInput→Output matching"]
        MR["🔀 Merge\nRecommendations"]
        HR["📄 HTML Report\nInteractive diagram"]
    end

    F1 --> AN
    F2 --> AN
    F3 --> AN
    AN --> PQ
    AN --> SR
    AN --> AS
    AN --> LG
    LG --> MR
    LG --> HR

    style F1 fill:#E97627,color:#fff,stroke:#E97627
    style F2 fill:#E97627,color:#fff,stroke:#E97627
    style F3 fill:#E97627,color:#fff,stroke:#E97627
    style AN fill:#4B8BBE,color:#fff,stroke:#4B8BBE
    style PQ fill:#22c55e,color:#fff
    style SR fill:#22c55e,color:#fff
    style AS fill:#22c55e,color:#fff
    style LG fill:#0078D4,color:#fff,stroke:#0078D4
    style MR fill:#0078D4,color:#fff,stroke:#0078D4
    style HR fill:#0078D4,color:#fff,stroke:#0078D4
```

```bash
# Batch — analyze & export all .tfl files in a folder
python migrate.py batch examples/prep_portfolio/ --output-dir /tmp/prep_output

# Pair a prep flow with a workbook (merge M expressions into .pbip)
python migrate.py migrate workbook.twbx --prep flow.tflx
```

The lineage report shows cross-flow dependencies, merge candidates, and data provenance across your entire Prep portfolio:

![Prep Flow Lineage Diagram — Cross-flow dependencies and output mapping](docs/images/prep_lineage_diagram.png)

<details>
<summary><b>📂 Prep flow batch output</b> (click to expand)</summary>

When running `--batch` on a folder of `.tfl` files, each flow produces:

```
prep_output/
├── 01_Raw_Orders_Clean/
│   ├── PowerQuery/
│   │   └── Orders_Clean.pq              ← Power Query M expression
│   ├── Sources/
│   │   └── Orders_2024.csv.json          ← Source connection metadata
│   └── assessment.json                   ← Grade, inputs, outputs, stats
├── 04_Customer_Enrichment/
│   ├── PowerQuery/
│   │   ├── Customer_360.pq
│   │   └── Demographics.pq
│   ├── Sources/
│   │   ├── CRM Customers.json
│   │   └── Demographics.csv.json
│   └── assessment.json
├── 14_Healthcare_Patient_Flow/
│   ├── PowerQuery/
│   │   ├── Department_KPI_Summary.pq
│   │   ├── Patient_Flow_Detail.pq
│   │   └── Physician_Performance.pq
│   ├── Sources/
│   │   ├── admissions.json
│   │   ├── ICD10_Codes.csv.json
│   │   ├── Procedures.json
│   │   └── Staff_Schedule.xlsx.json
│   └── assessment.json
└── prep_lineage/                         ← Cross-flow lineage (auto-generated)
    ├── prep_lineage_report.html          ← Interactive HTML with Mermaid diagram
    └── prep_lineage.json                 ← Machine-readable lineage graph
```

**Batch summary for prep flows:**

```
  Prep Flow                      Status    Grade   M Queries   Sources
  01_Raw_Orders_Clean                OK    GREEN           1         1
  04_Customer_Enrichment             OK    GREEN           2         2
  09_HR_Attrition_Analysis           OK    GREEN           4         3
  14_Healthcare_Patient_Flow         OK    GREEN           5         4
```

**Mixed directories** (`.twb` + `.tfl`) produce separate summary tables — workbooks get `.pbip` projects with fidelity scores, prep flows get Power Query M + sources + lineage.

</details>
### �📂 Generated Output

```
YourReport/
├── YourReport.pbip                     ← Double-click to open in PBI Desktop
├── migration_metadata.json             ← Stats, fidelity scores, warnings
├── lineage_map.json                    ← Source→target traceability
├── credentials_template.json           ← Datasource credential placeholders
├── YourReport.SemanticModel/
│   └── definition/
│       ├── model.tmdl                  ← Tables, measures, relationships
│       ├── expressions.tmdl            ← Power Query M queries
│       ├── roles.tmdl                  ← Row-Level Security
│       └── tables/
│           ├── Orders.tmdl             ← Columns + DAX measures
│           └── Calendar.tmdl           ← Auto-generated date table
└── YourReport.Report/
    └── definition/
        ├── report.json                 ← Report config + theme
        └── pages/
            └── ReportSection/
                ├── page.json           ← Layout + filters
                └── visuals/
                    └── [id]/visual.json ← Each visual
```

<details>
<summary><b>📂 Shared Semantic Model output</b> (click to expand)</summary>

When using `--shared-model`, the output is a single directory with one shared model and N thin reports:

```
SharedSales/
├── SharedSales.SemanticModel/            ← ONE shared semantic model
│   ├── .platform
│   ├── definition.pbism
│   └── definition/
│       ├── model.tmdl                    ← Merged tables, measures, relationships
│       ├── expressions.tmdl
│       ├── relationships.tmdl
│       └── tables/
│           ├── Orders.tmdl               ← Deduplicated across workbooks
│           ├── Customers.tmdl
│           └── Calendar.tmdl
├── WorkbookA.pbip                        ← Thin report A
├── WorkbookA.Report/
│   ├── definition.pbir                   ← byPath → ../SharedSales.SemanticModel
│   └── definition/
│       └── pages/
├── WorkbookB.pbip                        ← Thin report B
├── WorkbookB.Report/
│   ├── definition.pbir                   ← byPath → ../SharedSales.SemanticModel
│   └── definition/
│       └── pages/
└── merge_assessment.json                 ← Merge score, conflicts, recommendations
```

</details>

---

## 🧮 DAX Conversions (180+ functions)

> **Full reference:** [docs/TABLEAU_TO_DAX_REFERENCE.md](docs/TABLEAU_TO_DAX_REFERENCE.md)

<details>
<summary><b>📋 Complete conversion table</b> (click to expand)</summary>

| Category | Tableau | DAX |
|----------|---------|-----|
| Logic | `IF cond THEN val ELSE val2 END` | `IF(cond, val, val2)` |
| Logic | `IF ... ELSEIF ... END` | `IF(..., ..., IF(...))` |
| Null | `ISNULL([col])` | `ISBLANK([col])` |
| Null | `ZN([col])`, `IFNULL([col], 0)` | `IF(ISBLANK([col]), 0, [col])` |
| Text | `CONTAINS([col], "text")` | `CONTAINSSTRING([col], "text")` |
| Text | `ASCII`, `LEN`, `LEFT`, `RIGHT`, `MID` | `UNICODE`, `LEN`, `LEFT`, `RIGHT`, `MID` |
| Text | `UPPER`, `LOWER`, `REPLACE`, `TRIM` | `UPPER`, `LOWER`, `SUBSTITUTE`, `TRIM` |
| Agg | `COUNTD([col])` | `DISTINCTCOUNT([col])` |
| Agg | `AVG([col])` | `AVERAGE([col])` |
| Date | `DATETRUNC`, `DATEPART`, `DATEDIFF` | `STARTOF*`, `YEAR/MONTH/DAY/etc`, `DATEDIFF` |
| Date | `DATEADD`, `TODAY`, `NOW` | `DATEADD`, `TODAY`, `NOW` |
| Math | `ABS`, `CEILING`, `FLOOR`, `ROUND` | Identical or mapped |
| Stats | `MEDIAN`, `STDEV`, `STDEVP` | `MEDIAN`, `STDEV.S`, `STDEV.P` |
| Stats | `VAR`, `VARP`, `PERCENTILE`, `CORR` | `VAR.S`, `VAR.P`, `PERCENTILE.INC`, `CORREL` |
| Conversion | `INT`, `FLOAT`, `STR`, `DATE` | `INT`, `CONVERT`, `FORMAT`, `DATE` |
| Syntax | `==` | `=` |
| Syntax | `or` / `and` | `\|\|` / `&&` |
| Syntax | `+` (strings) | `&` |
| LOD | `{FIXED [dim] : AGG}` | `CALCULATE(AGG, ALLEXCEPT)` |
| LOD | `{INCLUDE [dim] : AGG}` | `CALCULATE(AGG)` |
| LOD | `{EXCLUDE [dim] : AGG}` | `CALCULATE(AGG, REMOVEFILTERS)` |
| Table Calc | `RUNNING_SUM / AVG / COUNT` | `CALCULATE(SUM/AVERAGE/COUNT)` |
| Table Calc | `RANK`, `RANK_UNIQUE`, `RANK_DENSE` | `RANKX(ALL())` |
| Table Calc | `WINDOW_SUM / AVG / MAX / MIN` | `CALCULATE()` |
| Iterator | `SUM(IF(...))` | `SUMX('table', IF(...))` |
| Iterator | `AVG(IF(...))` / `COUNT(IF(...))` | `AVERAGEX(...)` / `COUNTX(...)` |
| Cross-table | `[col]` other table (manyToOne) | `RELATED('Table'[col])` |
| Cross-table | `[col]` other table (manyToMany) | `LOOKUPVALUE(...)` |
| Security | `USERNAME()` | `USERPRINCIPALNAME()` |
| Security | `FULLNAME()` | `USERPRINCIPALNAME()` |
| Security | `ISMEMBEROF("group")` | `TRUE()` + RLS role per group |

</details>

### Highlights

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Tableau LOD                    →  Power BI DAX                        │
├─────────────────────────────────────────────────────────────────────────┤
│  {FIXED [customer] : SUM([qty] * [price])}                             │
│  → CALCULATE(SUM('T'[qty] * 'T'[price]), ALLEXCEPT('T', 'T'[customer]))│
│                                                                         │
│  {EXCLUDE [channel] : SUM([revenue])}                                   │
│  → CALCULATE(SUM([revenue]), REMOVEFILTERS('T'[channel]))               │
│                                                                         │
│  SUM(IF [status] != "X" THEN [qty] * [price] ELSE 0 END)               │
│  → SUMX('Orders', IF('Orders'[status] != "X", [qty] * [price], 0))     │
│                                                                         │
│  RANK(SUM([revenue]))                                                   │
│  → RANKX(ALL(SUM('Table'[revenue])))                                    │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 📊 Visual Type Mapping (128+)

<details>
<summary><b>🎨 Full visual mapping table</b> (click to expand)</summary>

| Tableau Mark | Power BI visualType | Notes |
|-------------|-------------------|-------|
| Bar | `clusteredBarChart` | Standard bar |
| Stacked Bar | `stackedBarChart` | |
| Line | `lineChart` | With markers |
| Area | `areaChart` | |
| Pie | `pieChart` | |
| SemiCircle / Donut / Ring | `donutChart` | |
| Circle / Shape / Dot Plot | `scatterChart` | |
| Square / Hex / Treemap | `treemap` | |
| Text | `tableEx` | Table with text |
| Automatic | `table` | Default table |
| Map / Density | `map` | |
| Polygon / Multipolygon | `filledMap` | Choropleth |
| Gantt Bar | `ganttChart` | Custom visual |
| Histogram | `clusteredColumnChart` | |
| Box Plot | `boxAndWhisker` | |
| Waterfall | `waterfallChart` | |
| Funnel | `funnel` | |
| Bullet / Radial / Gauge | `gauge` | |
| Heat Map / Highlight Table | `matrix` | Conditional formatting |
| Packed Bubble / Strip Plot | `scatterChart` | Bubble variant |
| Word Cloud | `wordCloud` | |
| Dual Axis / Combo / Pareto | `lineClusteredColumnComboChart` | |
| Sankey | `sankeyDiagram` | Custom visual GUID |
| Chord | `chordChart` | Custom visual GUID |
| Network | `networkNavigator` | Custom visual GUID |
| KPI | `card` | |
| Image | `image` | |
| 100% Stacked Area | `hundredPercentStackedAreaChart` | |
| Sunburst | `sunburst` | |
| Decomposition Tree | `decompositionTree` | |
| Shape Map | `shapeMap` | |

</details>

---

## 🏗️ Architecture

<details>
<summary><b>📁 Project structure</b> (click to expand)</summary>

```
TableauToPowerBI/
├── migrate.py                                 # CLI entry point (30+ flags)
├── tableau_export/                            # Tableau extraction
│   ├── extract_tableau_data.py                #   TWB/TWBX parser (23 object types)
│   ├── datasource_extractor.py                #   Connection/table/calc extractor
│   ├── dax_converter.py                       #   133+ DAX formula conversions
│   ├── m_query_builder.py                     #   49 connectors + 43 transforms
│   ├── prep_flow_parser.py                    #   Tableau Prep flow parser
│   ├── prep_flow_analyzer.py                  #   Prep flow profiler & assessment
│   ├── hyper_reader.py                        #   .hyper file data loader
│   ├── pulse_extractor.py                     #   Tableau Pulse metric extractor
│   └── server_client.py                       #   Tableau Server REST API client
├── powerbi_import/                            # Power BI generation
│   ├── import_to_powerbi.py                   #   Orchestrator
│   ├── pbip_generator.py                      #   .pbip project + visuals + filters
│   ├── visual_generator.py                    #   190 visual types, PBIR configs
│   ├── tmdl_generator.py                      #   Semantic model → TMDL
│   ├── dax_optimizer.py                       #   DAX AST optimizer (v25)
│   ├── assessment.py                          #   Pre-migration assessment
│   ├── strategy_advisor.py                    #   Import/DQ/Composite advisor
│   ├── validator.py                           #   Artifact validation
│   ├── equivalence_tester.py                  #   Cross-platform validation (v25)
│   ├── regression_suite.py                    #   Regression snapshot testing (v25)
│   ├── html_template.py                       #   Shared HTML report template (CSS/JS)
│   ├── migration_report.py                    #   Per-item fidelity tracking
│   ├── goals_generator.py                     #   Tableau Pulse → PBI Goals
│   ├── shared_model.py                        #   Multi-workbook merge engine
│   ├── merge_assessment.py                    #   Merge assessment reporter
│   ├── thin_report_generator.py               #   Thin report (byPath) generator
│   ├── prep_lineage.py                        #   Cross-flow lineage graph engine
│   ├── prep_lineage_report.py                 #   Lineage HTML report & merge advisor
│   ├── plugins.py                             #   Plugin system
│   ├── fabric_project_generator.py            #   Fabric-native output (v25)
│   ├── api_server.py                          #   REST API server (v28)
│   ├── schema_drift.py                        #   Schema drift detection (v28)
│   └── deploy/                                #   Deploy to PBI Service / Fabric
├── Dockerfile                                 # Docker image for API server
├── tests/                                     # 9,500+ tests in latest full run
├── docs/                                      # 18 documentation files
└── examples/                                  # Sample Tableau workbooks
```

</details>

---

## 📝 CLI Reference

| Flag | Description |
|------|-------------|
| **Input & Output** | |
| `workbook.twbx` | Positional argument — path to Tableau workbook |
| `--prep FILE` | Tableau Prep flow (.tfl/.tflx) to merge with a workbook |
| `--output-dir DIR` | Custom output directory (default: `artifacts/powerbi_projects/`) |
| `--output-format FORMAT` | Output format: `pbip` (default) or `fabric` |
| `--dry-run` | Preview migration without writing files |
| `--skip-extraction` | Skip extraction, re-use existing datasources.json |
| `--skip-conversion` | Skip DAX/M conversion, re-use existing JSON files |
| `--rollback` | Backup existing .pbip project before overwriting |
| **Batch** | |
| `--batch DIR` | Batch-migrate all .twb/.twbx files in a directory |
| `--batch-config FILE` | JSON batch config with per-workbook overrides |
| `--workers N` | Parallel batch processing with N workers |
| **Tableau Server / Cloud** | |
| `--server URL` | Tableau Server/Cloud URL |
| `--site SITE_ID` | Tableau site content URL |
| `--workbook NAME` | Workbook name or LUID to download |
| `--token-name NAME` | PAT name for Tableau Server auth |
| `--token-secret SECRET` | PAT secret for Tableau Server auth |
| `--server-batch PROJECT` | Download all workbooks from a server project (or `all`) |
| `--server-assets TYPE [...]` | Asset types: `workbooks`, `flows`, `datasources`, `all` |
| `--server-preserve-folders` | Mirror Tableau Server project folder structure locally |
| `--migrate-schedules` | Extract Tableau refresh schedules → PBI refresh config JSON |
| `--server-discover` | Discover site topology, dependency graph, and topology report |
| `--server-assess` | Server-level portfolio readiness report (GREEN/YELLOW/RED) |
| `--plan-migration` | Generate migration plan with wave assignments and effort estimates |
| `--team-size N` | Number of migration engineers for timeline calculation (default: 1) |
| **Shared Semantic Model** | |
| `--shared-model WB [WB ...]` | Merge multiple workbooks into one shared semantic model |
| `--model-name NAME` | Name for the shared semantic model (default: `SharedModel`) |
| `--assess-merge` | Only assess merge feasibility |
| `--force-merge` | Force merge even if score is below threshold |
| `--strict-merge` | Block generation on merge validation failures |
| `--strict-thin-report` | Block generation when thin-report orphaned references exceed the allowed threshold |
| `--thin-report-max-orphans N` | Maximum orphaned thin-report field references allowed when strict thin-report is enabled (default: `0`) |
| `--merge-preview` | Preview merge results without generating output |
| `--global-assess` | Cross-workbook pairwise merge scoring and clustering |
| **Deploy** | |
| `--deploy WORKSPACE_ID` | Deploy to Power BI Service workspace |
| `--deploy-refresh` | Trigger dataset refresh after deploy |
| `--deploy-bundle WS_ID` | Deploy shared model + thin reports as atomic Fabric bundle |
| `--bundle-refresh` | Trigger dataset refresh after bundle deployment |
| `--sync` | Auto-deploy after incremental change detection |
| **Semantic Model** | |
| `--calendar-start YEAR` | Calendar table start year (default: 2020) |
| `--calendar-end YEAR` | Calendar table end year (default: 2030) |
| `--culture LOCALE` | Culture/locale for linguistic metadata (e.g., `fr-FR`) |
| `--languages LOCALES` | Multi-language culture TMDL files (e.g., `fr-FR,de-DE`) |
| `--mode MODE` | Semantic model mode: `import`, `directquery`, or `composite` |
| `--composite-threshold COLS` | Per-table StorageMode threshold for Import vs DirectQuery |
| `--agg-tables MODE` | Auto-generate aggregation tables: `auto` or `none` |
| `--goals` | Convert Tableau Pulse metrics to PBI Goals |
| **Quality & Optimization** | |
| `--assess` | Run pre-migration assessment and strategy analysis |
| `--bulk-assess DIR` | Full portfolio assessment on a local folder (readiness + merge + prep lineage) |
| `--qa` | Full QA suite: validate → auto-fix → governance → compare |
| `--optimize-dax` | Run DAX optimizer (IF→SWITCH, COALESCE, constant folding) |
| `--no-optimize-dax` | Disable DAX optimizer |
| `--time-intelligence MODE` | Auto-inject Time Intelligence measures: `auto` or `none` |
| `--validate-data` | Post-migration data validation (query equivalence) |
| `--compare` | Generate comparison report (HTML) |
| `--no-compare` | Disable comparison report generation |
| `--check-drift DIR` | Compare extraction against saved snapshot for schema drift |
| `--autoplay` | Post-migration validation checks |
| **Prep Flows** | |
| `--prep-lineage PATHS` | Cross-flow lineage analysis for .tfl/.tflx files |
| **Other** | |
| `--verbose` / `-v` | Enable verbose (DEBUG) console logging |
| `--quiet` / `-q` | Suppress all output except errors |
| `--log-file FILE` | Write logs to a file |
| `--wizard` | Launch interactive migration wizard |
| `--paginated` | Generate paginated report layout |
| `--config FILE` | Load settings from a JSON configuration file |
| `--telemetry` | Enable anonymous usage telemetry (opt-in) |
| `--dashboard` | Generate telemetry dashboard |
| `--incremental DIR` | Merge changes into existing .pbip |

---

## 🚀 Deployment

<details>
<summary><b>Fabric end-to-end deployment</b></summary>

```bash
# Set FABRIC_TENANT_ID, FABRIC_CLIENT_ID, and FABRIC_CLIENT_SECRET first.
python migrate.py deploy your_workbook.twbx WORKSPACE_ID
```

The command generates and deploys Lakehouse, Dataflow Gen2, Notebook, Direct Lake
Semantic Model, Power BI report, and Pipeline artifacts. It then runs the Pipeline
and returns a nonzero exit code unless the run completes successfully. See the
[Deployment Guide](docs/DEPLOYMENT_GUIDE.md) for identity setup, read-only preflight,
and the legacy Power BI Service deployment workflow.

Or programmatically:

```python
from powerbi_import.deploy.pbi_deployer import PBIWorkspaceDeployer

deployer = PBIWorkspaceDeployer(workspace_id="your-workspace-guid")
result = deployer.deploy("artifacts/powerbi_projects/MyReport", refresh=True)
```

</details>

<details>
<summary><b>Microsoft Fabric</b></summary>

```bash
export FABRIC_WORKSPACE_ID="your-workspace-guid"
export FABRIC_TENANT_ID="your-tenant-guid"
export FABRIC_CLIENT_ID="your-app-client-id"
export FABRIC_CLIENT_SECRET="your-app-secret"

python -c "
from powerbi_import.deploy.deployer import FabricDeployer
deployer = FabricDeployer(workspace_id='your-workspace-guid')
deployer.deploy_artifacts_batch('artifacts/powerbi_projects/')
"
```

</details>

<details>
<summary><b>Environment configurations</b></summary>

| Environment | Log Level | Retry | Validate | Approval |
|-------------|-----------|-------|----------|----------|
| development | DEBUG | 3 | No | No |
| staging | INFO | 3 | Yes | No |
| production | WARNING | 5 | Yes | Yes |

</details>

---

## ✅ Validation

```python
from powerbi_import.validator import ArtifactValidator

result = ArtifactValidator.validate_project("artifacts/powerbi_projects/MyReport")
# {"valid": True, "files_checked": 15, "errors": []}
```

The validator checks `.pbip` JSON, `report.json`, `model.tmdl`, page/visual structure, and `sortByColumn` cross-references.

---

## 🧪 Testing

```bash
python -m pytest tests/ -v                          # Run all tests
python -m pytest tests/test_dax_converter.py -v      # Run specific file
python -m pytest tests/ --cov --cov-report=html      # Coverage report
RUN_OPENABILITY_SUITE=1 python -m pytest tests/test_desktop_openability_suite.py -v  # Fixture-based Desktop openability suite
python -m pytest tests/test_openability.py -v         # Static PBIP openability checks
```

<details>
<summary><b>📋 Test suite breakdown</b> (click to expand)</summary>

| Test File | Tests | Coverage |
|-----------|-------|----------|
| `test_dax_coverage.py` | 168 | Edge cases across all DAX categories |
| `test_generation_coverage.py` | 145 | TMDL/PBIR generation edge cases |
| `test_m_query_builder.py` | 102 | Power Query M, 40+ transforms |
| `test_tmdl_generator.py` | 92 | Semantic model, Calendar, TMDL |
| `test_dax_converter.py` | 86 | DAX formulas, LOD, table calcs |
| `test_error_paths.py` | 78 | Error handling, graceful degradation |
| `test_sprint_features.py` | 78 | Multi-DS, inference, metadata |
| `test_extract_coverage.py` | 75 | Stories, actions, sets, bins, hierarchies |
| `test_new_features.py` | 74 | Calc groups, field params, M columns |
| `test_v5_features.py` | 72 | v5.x features |
| `test_visual_generator.py` | 65 | 190 visual types, sync, buttons |
| `test_non_regression.py` | 63 | End-to-end sample workbook migrations |
| `test_prep_flow_parser.py` | 58 | Prep parsing, DAG, step conversion |
| `test_assessment.py` | 55 | Pre-migration (8 categories) |
| + 114 more files | — | Sprint, coverage, layout, E2E, wizard, telemetry… |

</details>

### CI/CD Pipeline

```mermaid
flowchart LR
    L["🔍 Lint\nflake8 + ruff"] --> T["🧪 Test\n9,500+ tests\nPy 3.12–3.14"]
    T --> V["✅ Validate\nStrict .twbx\nmigrations"]
    V --> S["📦 Staging\nFabric deploy"]
    S --> P["🚀 Production\nManual approval"]
    
    style L fill:#6366f1,color:#fff
    style T fill:#22c55e,color:#fff
    style V fill:#3b82f6,color:#fff
    style S fill:#f59e0b,color:#000
    style P fill:#ef4444,color:#fff
```

### 📊 Migration Report

After batch migration, run `python generate_report.py` to produce an HTML Migration & Assessment Report with per-workbook fidelity scores:

![Migration Results](docs/images/migration_results.png)

The report shows for each migrated workbook:
- **Fidelity** — percentage of items migrated successfully (100% = everything converted)
- **Total Items / Exact / Approximate / Unsupported** — breakdown of migration quality per item
- **Tables / Measures / Visuals** — counts of generated artifacts in the output .pbip project

---

## 📚 Documentation

| Document | Description |
|----------|-------------|
| 📖 [Migration Checklist](docs/MIGRATION_CHECKLIST.md) | Step-by-step migration guide |
| 🗺️ [Mapping Reference](docs/MAPPING_REFERENCE.md) | Tableau → Power BI mappings |
| 🔢 [133+ DAX Functions](docs/TABLEAU_TO_DAX_REFERENCE.md) | Complete formula reference |
| ⚡ [108 Power Query M](docs/TABLEAU_TO_POWERQUERY_REFERENCE.md) | Property reference |
| 🔄 [165 Prep → M](docs/TABLEAU_PREP_TO_POWERQUERY_REFERENCE.md) | Prep transformation reference |
| 📋 Prep Flow Lineage | Cross-flow lineage, Power Query M export, merge recommendations (`--batch` / `--prep-lineage`) |
| 🏗️ [Architecture](docs/ARCHITECTURE.md) | System design overview |
| 📊 [.pbip Guide](docs/POWERBI_PROJECT_GUIDE.md) | Output format explained |
| 🚀 [Deployment Guide](docs/DEPLOYMENT_GUIDE.md) | PBI Service & Fabric deploy |
| ⚠️ [Known Limitations](docs/KNOWN_LIMITATIONS.md) | Current limitations |
| 🔧 [Tableau Versions](docs/TABLEAU_VERSION_COMPATIBILITY.md) | Version compatibility |
| ❓ [FAQ](docs/FAQ.md) | Frequently asked questions |
| 🤝 [Contributing](CONTRIBUTING.md) | How to contribute |
| 📝 [Changelog](CHANGELOG.md) | Release history |
| � [Enterprise Guide](docs/ENTERPRISE_GUIDE.md) | 8-phase enterprise migration guide |
| 📈 [Roadmap](docs/ROADMAP.md) | Development roadmap |
| 🤖 [Agents](docs/AGENTS.md) | 15-agent specialization model |
| �🌐 Global Assessment | Cross-workbook merge analysis with HTML heatmap (`--global-assess`) |
| 🚀 Bundle Deployment | Deploy shared model + reports to Fabric (`--deploy-bundle`) |

---

## ⚠️ Known Limitations

- `MAKEPOINT()` (spatial) has no DAX equivalent — skipped
- `PREVIOUS_VALUE()` / `LOOKUP()` use OFFSET-based DAX — may need manual tuning
- Data source connection strings must be reconfigured in Power Query after migration
- Some table calculations (`INDEX()`, `SIZE()`) are approximated
- See [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) for the full list

---

## 🤝 Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

```bash
git clone https://github.com/cyphou/Tableau-To-PowerBI.git
cd Tableau-To-PowerBI
python -m pytest tests/ -q  # Make sure tests pass
```

---

## 📜 License

MIT
