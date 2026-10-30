# Power BI Projects Guide (.pbip)

[🏠 Overview](../README.md) · [📚 Documentation](README.md) · [🌐 Local browser](LOCAL_WEB_UI.md) · [✅ Review checklist](MIGRATION_CHECKLIST.md)

This guide explains how to use Power BI Projects generated automatically during Tableau migration.

Generate projects with the [local browser UI](LOCAL_WEB_UI.md) or
`python migrate.py batch FOLDER --output-dir OUTPUT`. The batch default is
`<source-folder>\migrated`; use the actual reported project path. Long names
may use a deterministic `P_<hash>` identity to fit Desktop path limits.
Generation and static validation do not establish native loading or refresh.

## 🎯 What is a Power BI Project?

A Power BI Project (.pbip) is a new file format introduced by Microsoft that allows you to:
- 📁 Work with Git and version control
- 💻 Edit in VS Code with extensions
- 👥 Collaborate more easily as a team
- 🔄 Track changes in reports

## 📦 Structure of a Power BI Project

```
MyReport.pbip                      # Main file to open
MyReport.SemanticModel/            # Semantic model folder (TMDL)
├── definition.pbism
├── .platform
└── definition/
    ├── tables/                    # Tables
    │   ├── Sales.tmdl
    │   ├── Customers.tmdl
    │   └── Products.tmdl
    ├── relationships.tmdl         # Relationships
    └── model.tmdl                 # Model configuration

MyReport.Report/                   # Report folder (visuals)
├── .platform
├── definition.pbir                # Points to the sibling SemanticModel
└── definition/
    ├── version.json
    ├── report.json
    ├── pages/                     # Report pages
    │   ├── pages.json             # Page order and active page
    │   └── {pageId}/
    │       ├── page.json
    │       └── visuals/
    │           └── {visualId}/visual.json
    ├── bookmarks/                 # When generated from source stories/actions
    └── RegisteredResources/       # Generated theme and embedded assets

migration_metadata.json            # Migration metadata
Data/                              # Unchanged packaged Excel copies and/or Hyper CSV/Parquet
```

## 🚀 Opening a Power BI Project

### Method 1: From Power BI Desktop

1. Open Power BI Desktop
2. **File** → **Open** → **Browse reports**
3. Navigate to the project directory reported by the migration
4. Select the file `[ReportName].pbip`
5. Click **Open**

### Method 2: Double-click

1. Navigate to the project directory reported by the migration
2. Double-click on `[ReportName].pbip`
3. With a supported Power BI Desktop installation and file association, Windows
   launches Desktop for the project; otherwise use **File → Open** above

### Method 3: Command line

```powershell
Start-Process "C:\Migration\Output\MyReport\MyReport.pbip"

# Or with Power BI Desktop directly
& "C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe" "C:\Migration\Output\MyReport\MyReport.pbip"
```

Use an installed Desktop build supporting the generated PBIP/TMDL schemas.
The generator currently targets page 2.1.0, visualContainer 2.7.0 and
bookmark 2.1.0, with base-theme name `CY26SU04`; these constants do not certify
a minimum Desktop version. See the [generation module guide](../powerbi_import/README.md).
These are opening instructions, not proof that every generated project loads
or refreshes successfully. Complete the source and review steps below.

## ⚙️ Initial Setup

### Step 1: Configure Data Sources

After opening the project:

The packaged-Excel copy below is available for trusted local per-workbook batch
generation. Uploaded jobs and shared-model/add-to-model/remove-from-model
workflows are outside this exception.

1. **Home** → **Transform data**
2. In Power Query Editor:
   - Click on each query
   - For native Import, review the generated connection and original file paths:
     migration generates schema/M without exporting native rows
   - An Excel workbook explicitly packaged in a TWBX is copied unchanged into
     the project's `Data/` folder and bound by its absolute path, not replaced
     by a similarly named loose file
   - Update that absolute path if the project moves. The copy includes all
     sheets and workbook metadata; keep source-bearing output/ZIPs out of
     repositories. Copying alone does not establish successful refresh
   - Relative paths from loose workbooks resolve against their original folder;
     other packaged native sources still need an accessible external binding
   - When the same datasource declares Hyper and an eligible complete original
     database binding, generation keeps that database and skips the Hyper.
     Review the quality report's Source decision; configure access separately.
     This choice is not proof of parity with the Tableau snapshot
   - For retained Hyper, review the complete staged CSV/Parquet files, their recorded
     counts/identities and the absolute `DataFolder` parameter. Every required
     export must have its exact Import binding; incomplete or unbound results
     block generation. Do not switch sources to hide an export failure.
     Static checks do not prove that Desktop has loaded the data
   - Configure credentials and gateway access separately
   - Refresh native Import data in Power BI after configuring source access
   - Inspect the refresh result, then save after successful processing to retain
     Power BI's local cache; initial refresh/no-data notices are not hidden
   - Resolve explicit unsupported-source diagnostics rather than replacing every source blindly
   - Examples:
   
   **SQL Server**
   ```m
   Source = Sql.Database("server.database.windows.net", "DatabaseName")
   ```
   
   **Excel**
   ```m
   Source = Excel.Workbook(File.Contents("C:\Data\file.xlsx"), null, true)
   ```
   
   **CSV**
   ```m
   Source = Csv.Document(File.Contents("C:\Data\file.csv"), [Delimiter=",", Encoding=65001])
   ```

3. **Close & Apply**

### Step 2: Verify the Data Model

1. Go to the **Model** tab
2. Check the **relationships** between tables
3. Validate column **data types**
4. Check **DAX measures** in the Data pane

### Step 3: Review Generated Visuals

1. Go to the **Report** tab
2. Inspect the pages and visuals already generated from the Tableau source
3. For each page:
   - Check field roles, filters, interactions and formatting against Tableau
   - Review approximate/custom visual mappings and required tenant availability
   - Record native-rendering evidence separately from JSON/schema validation

For reproducible repairs, fix the extractor/generator and regenerate; manual
project edits are local adjustments and may be overwritten by the next run.

#### Optional Microsoft PBIR validation

The [Microsoft skills-for-fabric authoring workflows](https://github.com/microsoft/skills-for-fabric)
provide an additional report check with the separately installed
`powerbi-report-author` CLI. The migration engine does not install or invoke it
automatically; Python's standard library remains sufficient for core migration.
The commands below were exercised with CLI version `0.5.0`.

Run it against the generated **`.Report` directory**, not the semantic model:

```powershell
powerbi-report-author validate '.\output\Sales\Sales.Report' --out '.\pbir-validation.json'

# Offline mode: skip remote JSON-Schema checks.
powerbi-report-author validate '.\output\Sales\Sales.Report' --no-schema --out '.\pbir-validation-offline.json'
```

Default validation retrieves the referenced JSON schemas. `--no-schema` skips
that part, so an offline pass must not be reported as a schema-validation pass.
Inspect the exit code and the saved `data.result`, `errorCount`, `warningCount`
and `diagnostics` fields; diagnostic codes are keys in the `diagnostics` object.
Do not discard a failed check or replace it with the migration's quality status.

Use the CLI's `catalog describe` and `formatting describe-property` commands to
verify the exact visual roles and formatting properties before changing a
generator. A property supported by `tableEx` need not be supported by legacy
`table`; the same distinction applies to `pivotTable` and `matrix`. Keep
regression tests for the verified contract and regenerate after a repair.

This is a **read-only static PBIR check**, not TMDL/DAX engine validation,
Desktop loading, refresh, rendered parity or deployment. Those results remain
`not_run` until separately authorized and executed. Model-authoring MCP also
requires its own setup and explicit acceptance of its license terms; this
report-validation procedure neither accepts them nor connects to a model.

### Step 4: Save

1. **File** → **Save**
2. Changes are saved within the project structure
3. Compatible with Git for versioning

## 📝 TMDL Format (Tabular Model Definition Language)

`.tmdl` files use a declarative language to define the model.

`sourceColumn` is a binding string, not an apostrophe-quoted object identifier.
The generator follows TOM's conditional double quoting for names with edge
whitespace or enclosing quotes, preserving exact physical CSV/Parquet names
and caption aliases. Ordinary names remain unquoted. Offline TOM roundtrip
tests establish serialization only, not Desktop loading or successful refresh.

### Example: Table

```tmdl
table 'Sales'
    lineageTag: 12345678-1234-1234-1234-123456789abc

    column 'SaleDate'
        dataType: dateTime
        lineageTag: 12345678-1234-1234-1234-123456789abc
        summarizeBy: none
        sourceColumn: SaleDate

    column 'Amount'
        dataType: double
        lineageTag: 12345678-1234-1234-1234-123456789abc
        summarizeBy: sum
        sourceColumn: Amount

    partition 'Sales' = m
        mode: import
        source =
            let
                Source = Sql.Database("server", "database"),
                Sales = Source{[Schema="dbo",Item="Sales"]}[Data]
            in
                Sales
```

### Example: Measure

```tmdl
measure 'Total Sales' =
    SUM('Sales'[Amount])
    lineageTag: 12345678-1234-1234-1234-123456789abc
    formatString: $#,##0.00
```

### Example: Relationship

```tmdl
relationship 12345678-1234-1234-1234-123456789abc
    fromColumn: 'Sales'.CustomerID
    toColumn: 'Customers'.ID
    fromCardinality: many
    toCardinality: one
    crossFilteringBehavior: oneDirection
    isActive: true
```

## 🔧 Common Modifications

### Adding a New Table

1. Create a file in `MyReport.SemanticModel/definition/tables/`
2. Name it: `NewTable.tmdl`
3. Define the structure:

```tmdl
table 'NewTable'
    lineageTag: [generate a GUID]

    column 'ID'
        dataType: int64
        lineageTag: [generate a GUID]
        summarizeBy: none
        sourceColumn: ID

    partition 'NewTable' = m
        mode: import
        source =
            let
                Source = ...
            in
                Source
```

### Adding a Measure

1. Open `MyReport.SemanticModel/definition/tables/` (measures are defined inside table TMDL files)
2. Add:

```tmdl
measure 'New Measure' =
    CALCULATE(SUM([Amount]), FILTER(...))
    lineageTag: [generate a GUID]
    formatString: #,##0
```

### Adding a Relationship

1. Open `MyReport.SemanticModel/definition/relationships.tmdl`
2. Add:

```tmdl
relationship 87654321-4321-4321-4321-cba987654321
    fromColumn: 'Table2'.ForeignKey
    toColumn: 'Table1'.ID
    fromCardinality: many
    toCardinality: one
    crossFilteringBehavior: oneDirection
    isActive: true
```

## 🔄 Git Workflow

### Initialize Git

```bash
cd C:\Migration\Output\MyReport
git init
git add .
git commit -m "Initial commit - Tableau migration"
```

### Create a Branch for Changes

```bash
git checkout -b feature/new-measure
# Make changes in Power BI Desktop
git add .
git commit -m "Add new sales measure"
git push origin feature/new-measure
```

### Compare Versions

```bash
# View differences
git diff main feature/new-measure

# View history
git log --oneline --graph
```

### Merge Changes

```bash
git checkout main
git merge feature/new-measure
```

## 🎨 Editing in VS Code

### Install Extensions

1. **TMDL Extension** for Power BI
2. **Power BI Project Extension**

### Open the Project

```bash
code artifacts/powerbi_projects/MyReport
```

### Benefits

- ✅ IntelliSense for DAX
- ✅ Syntax validation
- ✅ Search across all files
- ✅ Easier refactoring
- ✅ Native Git integration

## ⚡ Best Practices

### File Organization

```
powerbi_projects/
├── SalesReport/
│   ├── SalesReport.pbip
│   ├── SalesReport.SemanticModel/
│   └── SalesReport.Report/
├── MarketingReport/
│   ├── MarketingReport.pbip
│   ├── MarketingReport.SemanticModel/
│   └── MarketingReport.Report/
└── README.md
```

### Naming Conventions

- **Tables**: PascalCase (e.g., `CustomerOrders`)
- **Columns**: PascalCase (e.g., `OrderDate`)
- **Measures**: Spaces allowed (e.g., `Total Sales YTD`)
- **TMDL files**: Same name as the object (e.g., `Sales.tmdl`)

### Comments

```tmdl
/// This is a comment
/// Use triple slash for documentation

measure 'Complex Calculation' =
    // This measure calculates...
    VAR CurrentYear = YEAR(TODAY())
    RETURN
        CALCULATE(
            SUM([Amount]),
            YEAR([Date]) = CurrentYear
        )
```

### Indentation

- Use **tabs** for indentation
- Align properties vertically
- Separate objects with blank lines

## 🐛 Troubleshooting

### Error: "Cannot open .pbip file"

**Cause**: Power BI Desktop version is too old

**Solution**: Update to the latest version of Power BI Desktop

### Error: "Source not found"

**Cause**: Data connections are not configured

**Solution**:
1. Open Power Query Editor
2. Configure each data source
3. Replace `null` values with actual connections

### Error: "Invalid TMDL syntax"

**Cause**: Syntax error in a .tmdl file

**Solution**:
1. Check braces and parentheses
2. Check indentation
3. Validate GUIDs (format: 12345678-1234-1234-1234-123456789abc)

### Visuals Are Not Displaying

**Cause**: Missing or invalid page files

**Solution**:
1. Check `MyReport.Report/definition/pages/`
2. Recreate visuals manually if necessary
3. Use the JSON definitions as a guide

## 📚 Resources

- [Power BI Projects Documentation](https://learn.microsoft.com/power-bi/developer/projects/projects-overview)
- [TMDL Format](https://learn.microsoft.com/analysis-services/tmdl/tmdl-overview)
- [Git with Power BI](https://learn.microsoft.com/power-bi/developer/projects/projects-git)
- [VS Code Extensions](https://marketplace.visualstudio.com/search?term=power%20bi&target=VSCode)

## 🎓 Complete Example

See the example project in:
```
artifacts/powerbi_projects/ExampleReport/
```

Contains:
- ✅ Complete data source configuration
- ✅ Model with relationships
- ✅ Documented DAX measures
- ✅ Report pages with visuals
- ✅ README with instructions

---

**Note**: Power BI Projects are generated automatically during migration. You can modify them directly in Power BI Desktop or in VS Code.
