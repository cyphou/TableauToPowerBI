# Tableau Prep Transformations → Power Query M Complete Reference

[🏠 Overview](../README.md) · [📚 Documentation](README.md) · [🗺️ Mappings](MAPPING_REFERENCE.md) · [🔌 Power Query M](TABLEAU_TO_POWERQUERY_REFERENCE.md)

Reference for Tableau Prep Builder transformations and their Power Query M
patterns, including Prep (.tfl/.tflx) and workbook data-source transformations.
Not every documented pattern has a supported producer JSON dialect; the parser
contract below is authoritative for automated conversion.

> **Legend**  
> ✅ Implemented — generated automatically by the migration tool  
> 🆕 New — added in v3.1 via `m_query_builder.py` transformation helpers  
> 🔧 Manual — reference provided, requires manual setup in Power Query Editor  
> ❌ No equivalent — not available in Power Query

---

## Parser scope and workbook binding contract (reports #81–83)

The tables below are an M **reference**, not proof that every Prep JSON dialect
is parsed. Report **#81 remains partially supported**, not closed. Existing
#82/#83 behavior is preserved, including recursive-container/DAG handling and
standalone export validation.

### Envelopes and shape variants (reports 85-87)

The six known `superNode` envelopes (`SuperTransform`, `SuperJoin`,
`SuperAggregate`, `SuperUnion`, `SuperUnpivotExtended`, `SuperPivot`) normalize
their real `actionNode`, outer identity/edges and supported before/action/after
operations. They are never flattened into a transform that ignores the inner
operation. Supported conversion still depends on that inner dialect and its
schema; data-dependent rows-to-columns pivot remains unsupported. A wrapper
requires an object `actionNode`; version prefixes are ignored. The outer
identity/edges remain authoritative, and conflicting identities, types, edges
or duplicated payloads reject rather than overwrite one another.

For clean operations, `beforeActionGroup`, `actionGroup` and
`afterActionGroup` accept objects with an `actions` list, in that execution
order. A direct clean action or an unambiguous `actionNode.actions` list can
provide the central action. Conflicting central forms reject. These are action
**groups**, not arbitrary annotations: only missing, null or empty-list
before/after annotations allow conversion; nonempty annotations are inventoried
and block it.

`fields` accepts lists, null/missing collections or dictionaries keyed by
column name. A missing/null entry name comes from its key; conflicting names,
invalid values and case-insensitive collisions reject. Filters accept explicit
string `keep`/`remove` directions, ignoring surrounding whitespace and case.
Unsupported/missing filter directions and unrecognized envelope payloads are
retained as structured `_prep_diagnostics`
and `FlowProfile.diagnostics` for inventory. Diagnostics marked
`blocks_conversion` prevent successful M export. Empty containers are identity
only when they contain no schema/operation semantics and have unambiguous
routing, without discarding edge metadata. No unsupported transformation is
skipped to manufacture a successful output. The observed `SimpleUnion` inside
`SuperUnion` now decodes its per-input `namespaceFieldMappings` rather than
treating it as an automatic union. Recognizing that operation does not prove
conversion of every later node in the public flow. Synthetic
normalization/conversion tests do not establish native M execution or recovery
rates on an external/private corpus.

The full public flow still rejects its `SimpleJoin` inside `SuperJoin`:
left/right identities and equality keys alone do not establish output-field
selection, duplicate-key treatment or collision/rename semantics. An alias to
the existing `Join` dialect would invent a projection. Its union inputs also
declare a string/real mismatch without an explicit cast. The mapped-union
implementation does not turn either independent limitation into a success.

### Schema propagation

The parser tracks fields through each supported operation, rather than copying
the input schema to the output. Rename, remove, duplicate, type change and
calculated-column operations update names/types in execution order. For example,
`Raw, Discard` → rename `Raw` to `Amount` → remove `Discard` → add `Twice`
produces only `Amount, Twice` in datasource JSON and the generated TMDL columns.

- Explicit output `fields` select/reorder **existing** fields and must agree with
  computed types. Unknown calculation types can be resolved by concrete declared
  output types (also applied in M), or by `ChangeColumnType`. Missing fields,
  incompatible declared types and case-insensitive name collisions are errors.
- Aggregation retains only grouping keys and named aggregate outputs; references
  must exist. The current aggregate helper requires numeric inputs for non-count
  aggregates. Automatic unions and the observed mapped `SimpleUnion` combine
  input schemas and accept integer/real widening; incompatible types and
  unproven manual mapping dialects are rejected.
- Join schemas contain the left fields plus the right non-key fields actually
  expanded by the generated M. Colliding names require an explicit upstream
  rename. The temporary `Joined` column is removed even when no fields expand.
- Delimiter splits require explicit positive `numParts` for a deterministic
  schema. Dynamic rows-to-columns pivot schemas are rejected; columns-to-rows
  pivots require compatible value types. No source columns are invented.
- Expression conversion remains limited: simple literals, field references and
  numeric arithmetic have conservative type inference. A typed schema does not
  establish equivalence of arbitrary calculations, script/ML placeholders, or a
  successful Power Query refresh.

### Observed mapped union

The licensed [public Prep source](../examples/real_world/SOURCES.md) contains a
`SuperUnion` whose `.v1.SimpleUnion` action declares
`namespaceFieldMappings`: each record names an input namespace and maps
bracketed physical source fields to output names. Each namespace must match an
incoming edge's `nextNamespace` exactly. Empty maps retain that input's schema;
mapped and unmapped fields are validated together.

The parser applies renames separately to each input before appending. Generated
M uses collision-free temporary names followed by final names, so swaps and
rename chains do not cascade. The output schema and final type casts agree;
only supported compatible type widening is accepted. Missing/extra or ambiguous
namespace records, unknown physical fields, malformed mappings, conflicting
targets, case-insensitive collisions and incompatible types reject rather than
discard columns or coerce arbitrary values.

The append uses [Table.Combine](https://learn.microsoft.com/en-us/powerquery-m/table-combine),
whose documented behavior fills absent columns with null. No deduplication
step is added. [Production-path tests](../tests/test_prep_union_mapping.py)
cover static schema/M contracts, escaping and standalone/workbook/TMDL
preservation. They do not execute the native M engine or prove Tableau
row/value equivalence; those checks remain `not_run`.

### Clean-node dialect evidence status

Static evidence demonstrates the producer structure for one of the eight
clean-node dialects tracked by #81: `.v2018_3_3.Remap` in the public
CC-BY-4.0 [`tableau_prep_book.tfl` example](../examples/real_world/SOURCES.md).
The [evidence fixture](../tests/prep_dialect_samples/remap_evidence.json)
pins the upstream commit, archive member, license and producer-node paths;
both local nodes were verified against that upstream export on 2026-10-21.
The evidence fixture contains an unmodified projection of the `Discounts`
node's operation fields (not the entire node) and a separate anonymized,
reduced multigroup example. For the latter, it records the original shape
summary, including group sizes, whether targets occur in their own source
lists, and `NGramFingerprint` method/threshold provenance; the generic payload
is not raw source data.

The observed `Discounts` `values` field is
`{"\"0\"": ["\"0\"", "\"None\""]}`: the fixture specifies string inputs
`["0", "None", "5%", null]` and expected outputs
`["0", "0", "5%", null]`. The generic multigroup scenario similarly specifies
the expected string mappings for its anonymized labels. These expected rows
are specifications for the scenarios only.
Evidence tests compare the recorded structure to producer node paths and check
the test-side mapping specification and malformed/ambiguous-map controls.
Separately, N2's **first bounded Remap conversion slice is delivered**:

- The parser accepts observed explicit JSON-encoded TEXT maps (`values` maps
  encoded target strings to nonempty lists of encoded source strings), both
  standalone and within `beforeActionGroup`, `actionGroup` and `afterActionGroup`.
- Generated M uses one `Table.TransformColumns` lookup with `List.PositionOf`
  and `Comparer.Ordinal`: exact, case-sensitive and simultaneous on the original
  cell, not cascading replacements. For `A → B, B → C`, input `A` maps to `B`,
  not `C`. Unmatched strings and nulls are preserved; field names/types remain
  unchanged.
- Missing/empty column names, missing or non-string upstream fields,
  empty/malformed maps or source lists, invalid JSON, decoded non-string values
  and conflicting targets for the same source reject explicitly. Repeating a
  source with the same target is accepted; it is not ambiguous.
- `groupMethodProps`, including `NGramFingerprint` metadata, is provenance for
  the already materialized map. It does not trigger fuzzy grouping, threshold
  evaluation or inferred replacements.
- [Production conversion tests](../tests/test_prep_remap_conversion.py) exercise
  both observed payloads through standalone/grouped dispatch, escaping and
  negative controls. Static source-to-parser-to-workbook/TMDL checks verify
  binding coverage, field names/types and preservation of the bound M partition.
  The tightly coupled identifier-healing fix preserves valid M conditional
  and equality syntax rather than misquoting it as a malformed step name.

These are static conversion contracts, not native Power Query execution.
Native refresh and runtime value equivalence remain `not_run`; they require an
authorized Power Query environment and source/target value comparison.
N1 is **partial** (one observed dialect of eight), not complete and not #81 closure.
The other seven clean-node dialects still have no demonstrated producer
payloads in accepted fixtures: `ValueFilter`, `RichRangeFilter`, `RangeFilter`,
`RichDiscreteValueFilter`, `QuickCalcColumn`, `RichWildcardFilter`, and
`RichNullFilter`. Their names in analyzer classification tables do not prove
convertibility. In particular, `RangeFilter` is not silently treated as the
already supported `FilterRange` dialect.

A bounded public-source search did not establish suitable additional fixtures:
customer-like exports with unclear data-publication permission were excluded,
and third-party node generators were not treated as Tableau-produced evidence.
This is a fixture-coverage gap, not a claim that those payloads do not exist.
The 2026-10-30 follow-up likewise did not add an accepted fixture: two filter
shapes appeared in a source with unresolved licensing and incomplete value/null
semantics; a third-party assumed `QuickCalcColumn` format was not producer
evidence. The other four dialects were not found in that bounded search. No
source data were copied and none of these candidates enabled a conversion.

### `--prep` with a workbook

Binding is planned and validated before applying any changes, on deep copies.
`merge_prep_with_workbook(..., return_report=True)` returns `(datasources,
coverage)` on success. Rejections raise `PrepBindingError` with a `.coverage`
report; `run_prep_flow` returns `False` and leaves the existing JSON unchanged.
Successful JSON replacement is staged in the same directory and atomic.

Coverage lists matched workbook tables (with match method), unmatched published
and local tables, unused outputs and required secondary queries. The contract is:

1. At least one Prep output must reconnect a workbook table.
2. **Every published datasource/table in that workbook must be reconnected.**
   Use a scoped workbook/flow pairing if the flow does not cover its published
   sources. Unrelated local tables may remain unchanged.
3. Auxiliary flow outputs need not be consumed. They are reported as unused,
   **not appended** to disguise an incomplete reconnection. Only secondary
   queries referenced by bound queries are included.
4. Exact output table names/captions are accepted, including the deterministic
   space-to-underscore caption form. A single-table published datasource may
   match its exact caption or published identity even if the relation is named
   `sqlproxy`. Ambiguity/collisions are rejected.
5. The only reserved prefix rule is `F_<digits>_<subject>` →
   `S_F<digits>_<subject>`. It requires a unique candidate **and** published
   identity metadata naming the target (`server_ds_name` from sqlproxy or the
   extracted repository name/id). A caption or prefix resemblance alone is not
   evidence. Missing proof fails with a recommendation to supply exact names or
   published metadata; no fuzzy match or connection is invented.

Thus outputs `F1, F2` with workbook published tables `F1, S_F2` **fail**, rather
than reporting a full merge after binding only `F1`. Existing workbook physical
columns must be available in the selected Prep output. Per-table overrides
retain workbook table identity and take precedence over a preexisting
datasource-wide override, so the TMDL partition consumes the bound M query.
Globally colliding workbook table names are rejected for bound overrides.

Standalone `.tfl/.tflx` **batch** migration still exports M expressions, source
definitions and lineage/assessment artifacts; it does **not** produce a PBIP.
Missing workbook datasource JSON in `--prep` mode is an error, not an implicit
standalone conversion. Static JSON/TMDL checks do not execute M or verify refresh.

---

## 1. Input Steps (Data Source Connections)

| # | Tableau Prep Step | Power Query M Equivalent | Status | Notes |
|---|------------------|-------------------------|--------|-------|
| 1 | Connect to Database | `Sql.Database()`, `PostgreSQL.Database()`, etc. | ✅ | 25 connector types supported |
| 2 | Connect to File (Excel) | `Excel.Workbook(File.Contents())` | ✅ | Sheet selection, headers |
| 3 | Connect to File (CSV) | `Csv.Document(File.Contents())` | ✅ | Delimiter, encoding |
| 4 | Connect to File (JSON) | `Json.Document(File.Contents())` | ✅ | List/record handling |
| 5 | Connect to File (PDF) | `Pdf.Tables(File.Contents())` | ✅ | Table extraction |
| 6 | Connect to Cloud (Snowflake) | `Snowflake.Databases()` | ✅ | Warehouse/schema navigation |
| 7 | Connect to Cloud (BigQuery) | `GoogleBigQuery.Database()` | ✅ | Project/dataset navigation |
| 8 | Connect to Published Data Source | Dataflow / Shared Dataset | 🔧 | Manual Power BI dataflow setup |
| 9 | Connect to Tableau Extract (.hyper) | Reconnect to original source | 🔧 | .hyper not readable by Power Query |
| 10 | Data Interpreter | Automatic in Excel connector | ✅ | Power BI auto-detects tables |
| 11 | Wildcard Union (file pattern) | `Folder.Files()` + filter | 🆕 | Pattern-based file loading |

---

## 2. Clean Step — Column Operations

| # | Tableau Prep Operation | Power Query M Function | Status | Example |
|---|----------------------|----------------------|--------|---------|
| 1 | Rename column | `Table.RenameColumns()` | 🆕 | `Table.RenameColumns(Source, {{"old", "new"}})` |
| 2 | Remove column | `Table.RemoveColumns()` | 🆕 | `Table.RemoveColumns(Source, {"col1", "col2"})` |
| 3 | Keep only columns | `Table.SelectColumns()` | 🆕 | `Table.SelectColumns(Source, {"col1", "col2"})` |
| 4 | Duplicate column | `Table.DuplicateColumn()` | 🆕 | `Table.DuplicateColumn(Source, "col", "col_copy")` |
| 5 | Change data type | `Table.TransformColumnTypes()` | ✅ | Already in all M queries |
| 6 | Split column (delimiter) | `Table.SplitColumn()` | 🆕 | `Table.SplitColumn(Source, "col", Splitter.SplitTextByDelimiter(","))` |
| 7 | Split column (position) | `Table.SplitColumn()` | 🆕 | `Table.SplitColumn(Source, "col", Splitter.SplitTextByPositions({0, 5}))` |
| 8 | Merge columns | `Table.CombineColumns()` | 🆕 | `Table.CombineColumns(Source, {"first", "last"}, Combiner.CombineTextByDelimiter(" "))` |
| 9 | Move column | Implicit in `Table.ReorderColumns()` | 🆕 | `Table.ReorderColumns(Source, {"col2", "col1", "col3"})` |

---

## 3. Clean Step — Value Operations

| # | Tableau Prep Operation | Power Query M Function | Status | Example |
|---|----------------------|----------------------|--------|---------|
| 1 | Replace values | `Table.ReplaceValue()` | 🆕 | `Table.ReplaceValue(Source, "old", "new", Replacer.ReplaceText, {"col"})` |
| 2 | Replace null | `Table.ReplaceValue()` | 🆕 | `Table.ReplaceValue(Source, null, 0, Replacer.ReplaceValue, {"col"})` |
| 3 | Remove nulls (filter) | `Table.SelectRows()` | 🆕 | `Table.SelectRows(Source, each [col] <> null)` |
| 4 | Trim whitespace | `Table.TransformColumns()` | 🆕 | `Table.TransformColumns(Source, {{"col", Text.Trim}})` |
| 5 | Clean (remove non-printable) | `Table.TransformColumns()` | 🆕 | `Table.TransformColumns(Source, {{"col", Text.Clean}})` |
| 6 | Uppercase | `Table.TransformColumns()` | 🆕 | `Table.TransformColumns(Source, {{"col", Text.Upper}})` |
| 7 | Lowercase | `Table.TransformColumns()` | 🆕 | `Table.TransformColumns(Source, {{"col", Text.Lower}})` |
| 8 | Proper case | `Table.TransformColumns()` | 🆕 | `Table.TransformColumns(Source, {{"col", Text.Proper}})` |
| 9 | Fill down | `Table.FillDown()` | 🆕 | `Table.FillDown(Source, {"col"})` |
| 10 | Fill up | `Table.FillUp()` | 🆕 | `Table.FillUp(Source, {"col"})` |
| 11 | Group and replace (typos) | `Table.ReplaceValue()` chain | 🆕 | Multiple replace steps |
| 12 | Extract text (before/after/between) | `Table.TransformColumns()` | 🆕 | `Text.BeforeDelimiter()`, `Text.AfterDelimiter()`, `Text.BetweenDelimiters()` |
| 13 | Pad text (leading zeros) | `Table.TransformColumns()` | 🆕 | `Text.PadStart([col], 5, "0")` |

---

## 4. Filter Step

| # | Tableau Prep Operation | Power Query M Function | Status | Example |
|---|----------------------|----------------------|--------|---------|
| 1 | Keep only rows (values) | `Table.SelectRows()` | 🆕 | `Table.SelectRows(Source, each [Status] = "Active")` |
| 2 | Exclude rows (values) | `Table.SelectRows()` | 🆕 | `Table.SelectRows(Source, each [Status] <> "Cancelled")` |
| 3 | Keep range (numeric) | `Table.SelectRows()` | 🆕 | `Table.SelectRows(Source, each [Amount] >= 100 and [Amount] <= 1000)` |
| 4 | Keep range (date) | `Table.SelectRows()` | 🆕 | `Table.SelectRows(Source, each [Date] >= #date(2024,1,1))` |
| 5 | Keep Top N | `Table.FirstN()` | 🆕 | `Table.FirstN(Table.Sort(Source, {{"Sales", Order.Descending}}), 10)` |
| 6 | Keep Bottom N | `Table.LastN()` | 🆕 | `Table.LastN(Table.Sort(Source, {{"Sales", Order.Ascending}}), 10)` |
| 7 | Wildcard match | `Table.SelectRows()` | 🆕 | `Table.SelectRows(Source, each Text.Contains([Name], "pattern"))` |
| 8 | Null filter | `Table.SelectRows()` | 🆕 | `Table.SelectRows(Source, each [col] <> null)` |
| 9 | Remove duplicates | `Table.Distinct()` | 🆕 | `Table.Distinct(Source, {"col1", "col2"})` |

---

## 5. Calculated Field Step

| # | Tableau Prep Operation | Power Query M Function | Status | Example |
|---|----------------------|----------------------|--------|---------|
| 1 | Add calculated field | `Table.AddColumn()` | 🆕 | `Table.AddColumn(Source, "NewCol", each [Price] * [Qty])` |
| 2 | String calculated field | `Table.AddColumn()` | 🆕 | `Table.AddColumn(Source, "Full Name", each [First] & " " & [Last])` |
| 3 | Date calculated field | `Table.AddColumn()` | 🆕 | `Table.AddColumn(Source, "Year", each Date.Year([OrderDate]))` |
| 4 | Conditional (IF) | `Table.AddColumn()` | 🆕 | `Table.AddColumn(Source, "Tier", each if [Sales] > 1000 then "High" else "Low")` |
| 5 | LOD Expression (FIXED) | DAX `CALCULATE(AGG, ALLEXCEPT())` | ✅ | Handled in DAX, not M |
| 6 | Row number | `Table.AddIndexColumn()` | 🆕 | `Table.AddIndexColumn(Source, "RowNum", 1, 1)` |
| 7 | RANK / RANK_PERCENTILE | `Table.AddIndexColumn()` + sort | 🆕 | Sort then index |
| 8 | Tableau functions in Prep | `Table.AddColumn()` with M functions | 🆕 | See §11 Function Mapping |

---

## 6. Aggregate Step

| # | Tableau Prep Operation | Power Query M Function | Status | Example |
|---|----------------------|----------------------|--------|---------|
| 1 | Group By + SUM | `Table.Group()` | 🆕 | `Table.Group(Source, {"Region"}, {{"Total", each List.Sum([Sales]), type number}})` |
| 2 | Group By + AVG | `Table.Group()` | 🆕 | `Table.Group(Source, {"Region"}, {{"Avg", each List.Average([Sales]), type number}})` |
| 3 | Group By + COUNT | `Table.Group()` | 🆕 | `Table.Group(Source, {"Region"}, {{"Count", each Table.RowCount(_), Int64.Type}})` |
| 4 | Group By + COUNT DISTINCT | `Table.Group()` | 🆕 | `Table.Group(Source, {"Region"}, {{"Unique", each List.Count(List.Distinct([CustID])), Int64.Type}})` |
| 5 | Group By + MIN | `Table.Group()` | 🆕 | `Table.Group(Source, {"Region"}, {{"Min", each List.Min([Sales]), type number}})` |
| 6 | Group By + MAX | `Table.Group()` | 🆕 | `Table.Group(Source, {"Region"}, {{"Max", each List.Max([Sales]), type number}})` |
| 7 | Group By + MEDIAN | `Table.Group()` | 🆕 | `Table.Group(Source, {"Region"}, {{"Median", each List.Median([Sales]), type number}})` |
| 8 | Group By + STDEV | `Table.Group()` | 🆕 | `Table.Group(Source, {"Region"}, {{"StDev", each List.StandardDeviation([Sales]), type number}})` |
| 9 | Multi-aggregation | `Table.Group()` | 🆕 | Multiple aggregation columns in one call |
| 10 | Group by all fields | `Table.Group()` | 🆕 | All columns as group-by |

---

## 7. Pivot Step

These are M expression-builder examples, not proof that the Prep parser accepts
the corresponding producer payload. A data-dependent `List.Distinct` pivot
does not establish the fixed output columns required by the semantic model;
that parser case remains rejected.

| # | Tableau Prep Operation | Power Query M Function | Status | Example |
|---|----------------------|----------------------|--------|---------|
| 1 | Pivot columns to rows (unpivot) | `Table.UnpivotOtherColumns()` | 🆕 | `Table.UnpivotOtherColumns(Source, {"ID", "Name"}, "Attribute", "Value")` |
| 2 | Pivot specific columns | `Table.Unpivot()` | 🆕 | `Table.Unpivot(Source, {"Q1", "Q2", "Q3", "Q4"}, "Quarter", "Revenue")` |
| 3 | Pivot rows to columns | `Table.Pivot()` | M helper only; dynamic parser schema unsupported | `Table.Pivot(Source, List.Distinct(Source[Category]), "Category", "Sales", List.Sum)` |

---

## 8. Join Step

These examples describe M helpers and the supported explicit join contract.
They do not establish the implicit result projection of the public
`SuperJoin`/`SimpleJoin` producer variant, which remains rejected.

| # | Tableau Prep Operation | Power Query M Function | Status | Example |
|---|----------------------|----------------------|--------|---------|
| 1 | Inner Join | `Table.NestedJoin()` + Expand | 🆕 | `Table.NestedJoin(T1, "key", T2, "key", "Joined", JoinKind.Inner)` |
| 2 | Left Join | `Table.NestedJoin()` + Expand | 🆕 | `Table.NestedJoin(T1, "key", T2, "key", "Joined", JoinKind.LeftOuter)` |
| 3 | Right Join | `Table.NestedJoin()` + Expand | 🆕 | `Table.NestedJoin(T1, "key", T2, "key", "Joined", JoinKind.RightOuter)` |
| 4 | Full Outer Join | `Table.NestedJoin()` + Expand | 🆕 | `Table.NestedJoin(T1, "key", T2, "key", "Joined", JoinKind.FullOuter)` |
| 5 | Left Only (anti-join) | `Table.NestedJoin()` + filter null | 🆕 | Join + `Table.SelectRows(each [Joined] = null)` |
| 6 | Right Only (anti-join) | `Table.NestedJoin()` reversed | 🆕 | Reverse tables + left anti |
| 7 | Not Inner (full anti) | Combination of anti-joins | 🔧 | Complex M pattern |
| 8 | Multi-key join | `Table.NestedJoin()` with lists | 🆕 | `Table.NestedJoin(T1, {"k1","k2"}, T2, {"k1","k2"}, "J", JoinKind.Inner)` |
| 9 | Join with calculation clause | `Table.AddColumn()` pre-join | 🔧 | Add computed key before joining |
| 10 | Expand joined columns | `Table.ExpandTableColumn()` | 🆕 | `Table.ExpandTableColumn(Joined, "J", {"col1", "col2"})` |

---

## 9. Union Step

The M helpers below do not, by themselves, decode manual Prep field mappings.
Parser support depends on the validated producer mapping and propagated input
schemas described in the scope contract above.

| # | Tableau Prep Operation | Power Query M Function | Status | Example |
|---|----------------------|----------------------|--------|---------|
| 1 | Union (same schema) | `Table.Combine()` | 🆕 | `Table.Combine({Table1, Table2, Table3})` |
| 2 | Union (mismatched columns) | `Table.Combine()` | 🆕 | Auto-fills nulls for missing columns |
| 3 | Wildcard union (files) | `Folder.Files()` + transform | 🆕 | `Folder.Files(path)` → filter → combine |
| 4 | Union with source tracking | `Table.AddColumn()` + `Table.Combine()` | 🆕 | Add source name column before union |

---

## 10. Reshape Steps (Advanced)

| # | Tableau Prep Operation | Power Query M Function | Status | Example |
|---|----------------------|----------------------|--------|---------|
| 1 | Transpose | `Table.Transpose()` | 🆕 | `Table.Transpose(Source)` |
| 2 | Sort rows | `Table.Sort()` | 🆕 | `Table.Sort(Source, {{"Sales", Order.Descending}})` |
| 3 | Multi-column sort | `Table.Sort()` | 🆕 | `Table.Sort(Source, {{"Region", Order.Ascending}, {"Sales", Order.Descending}})` |
| 4 | Sample rows | `Table.FirstN()` or `Table.Sample()` | 🔧 | `Table.FirstN(Source, 1000)` |
| 5 | Remove top/bottom rows | `Table.Skip()` / `Table.RemoveLastN()` | 🆕 | `Table.Skip(Source, 3)` |
| 6 | Promote first row to headers | `Table.PromoteHeaders()` | ✅ | Already in file connectors |
| 7 | Demote headers to first row | `Table.DemoteHeaders()` | 🆕 | `Table.DemoteHeaders(Source)` |
| 8 | Remove errors | `Table.RemoveRowsWithErrors()` | 🆕 | `Table.RemoveRowsWithErrors(Source)` |
| 9 | Remove blank rows | `Table.SelectRows()` | 🆕 | `Table.SelectRows(Source, each not List.IsEmpty(List.RemoveNulls(Record.FieldValues(_))))` |
| 10 | Add index column | `Table.AddIndexColumn()` | 🆕 | `Table.AddIndexColumn(Source, "Index", 1, 1)` |

---

## 11. Function Mapping (Tableau Prep → Power Query M)

### String Functions

| Tableau Prep / Calc | Power Query M | Notes |
|--------------------|--------------|-------|
| `LEFT(str, n)` | `Text.Start([col], n)` | |
| `RIGHT(str, n)` | `Text.End([col], n)` | |
| `MID(str, start, len)` | `Text.Middle([col], start-1, len)` | 0-indexed |
| `LEN(str)` | `Text.Length([col])` | |
| `UPPER(str)` | `Text.Upper([col])` | |
| `LOWER(str)` | `Text.Lower([col])` | |
| `TRIM(str)` | `Text.Trim([col])` | |
| `LTRIM(str)` | `Text.TrimStart([col])` | |
| `RTRIM(str)` | `Text.TrimEnd([col])` | |
| `REPLACE(str, old, new)` | `Text.Replace([col], old, new)` | |
| `CONTAINS(str, sub)` | `Text.Contains([col], sub)` | Returns logical |
| `STARTSWITH(str, sub)` | `Text.StartsWith([col], sub)` | |
| `ENDSWITH(str, sub)` | `Text.EndsWith([col], sub)` | |
| `SPLIT(str, delim, part)` | `Text.Split([col], delim){part-1}` | List index |
| `FIND(str, sub)` | `Text.PositionOf([col], sub)` | Returns -1 if not found |
| `SPACE(n)` | `Text.Repeat(" ", n)` | |
| `str1 + str2` | `[col1] & [col2]` | Concatenation |

### Date Functions

| Tableau Prep / Calc | Power Query M | Notes |
|--------------------|--------------|-------|
| `YEAR(date)` | `Date.Year([col])` | |
| `MONTH(date)` | `Date.Month([col])` | |
| `DAY(date)` | `Date.Day([col])` | |
| `DATEPART('quarter', date)` | `Date.QuarterOfYear([col])` | |
| `DATEPART('week', date)` | `Date.WeekOfYear([col])` | |
| `DATEPART('dayofweek', date)` | `Date.DayOfWeek([col])` | |
| `DATEADD('month', n, date)` | `Date.AddMonths([col], n)` | |
| `DATEADD('day', n, date)` | `Date.AddDays([col], n)` | |
| `DATEADD('year', n, date)` | `Date.AddYears([col], n)` | |
| `DATEDIFF('day', d1, d2)` | `Duration.Days([d2] - [d1])` | Duration arithmetic |
| `DATETRUNC('month', date)` | `Date.StartOfMonth([col])` | |
| `DATETRUNC('year', date)` | `Date.StartOfYear([col])` | |
| `DATETRUNC('quarter', date)` | `Date.StartOfQuarter([col])` | |
| `TODAY()` | `DateTime.LocalNow()` | or `Date.From(DateTime.LocalNow())` |
| `NOW()` | `DateTime.LocalNow()` | |
| `MAKEDATE(y, m, d)` | `#date(y, m, d)` | |
| `MAKEDATETIME(date, time)` | `#datetime(y, m, d, h, min, s)` | |
| `ISDATE(str)` | `try Date.FromText([col]) otherwise null` | Error handling |

### Numeric Functions

| Tableau Prep / Calc | Power Query M | Notes |
|--------------------|--------------|-------|
| `ABS(n)` | `Number.Abs([col])` | |
| `ROUND(n, d)` | `Number.Round([col], d)` | |
| `CEILING(n)` | `Number.RoundUp([col], 0)` | |
| `FLOOR(n)` | `Number.RoundDown([col], 0)` | |
| `POWER(n, e)` | `Number.Power([col], e)` | |
| `SQRT(n)` | `Number.Sqrt([col])` | |
| `LOG(n)` | `Number.Log([col])` | Natural log |
| `LOG(n, base)` | `Number.Log([col], base)` | With base |
| `EXP(n)` | `Number.Exp([col])` | |
| `MOD(n, d)` | `Number.Mod([col], d)` | |
| `SIGN(n)` | `Number.Sign([col])` | |
| `MIN(a, b)` | `List.Min({[a], [b]})` | Row-level min |
| `MAX(a, b)` | `List.Max({[a], [b]})` | Row-level max |

### Logical Functions

| Tableau Prep / Calc | Power Query M | Notes |
|--------------------|--------------|-------|
| `IF cond THEN a ELSE b END` | `if cond then a else b` | M uses lowercase |
| `IIF(cond, a, b)` | `if cond then a else b` | |
| `CASE expr WHEN v1 THEN r1 ... END` | Nested `if ... then ... else if` | |
| `ISNULL(x)` | `[x] = null` | |
| `IFNULL(x, alt)` | `if [x] = null then alt else [x]` | |
| `ZN(x)` | `if [x] = null then 0 else [x]` | |
| `AND` / `OR` / `NOT` | `and` / `or` / `not` | M logical operators |

### Conversion Functions

| Tableau Prep / Calc | Power Query M | Notes |
|--------------------|--------------|-------|
| `INT(x)` | `Number.RoundDown([col], 0)` | or `Int64.From([col])` |
| `FLOAT(x)` | `Number.From([col])` | |
| `STR(x)` | `Text.From([col])` | |
| `DATE(str)` | `Date.FromText([col])` | |
| `DATETIME(str)` | `DateTime.FromText([col])` | |

---

## 12. Script Step (R / Python)

| # | Tableau Prep Operation | Power Query M Equivalent | Status | Notes |
|---|----------------------|-------------------------|--------|-------|
| 1 | R script | `R.Execute()` | 🔧 | Requires R runtime on gateway |
| 2 | Python script | `Python.Execute()` | 🔧 | Requires Python runtime on gateway |
| 3 | TabPy custom functions | Custom M functions or `Python.Execute()` | 🔧 | Manual conversion needed |

---

## 13. Output Step

| # | Tableau Prep Operation | Power Query M Equivalent | Status | Notes |
|---|----------------------|-------------------------|--------|-------|
| 1 | Save to .hyper extract | Power BI Import mode | ✅ | Data loaded into model |
| 2 | Publish to Tableau Server | Power BI dataflow / dataset | 🔧 | Manual publish setup |
| 3 | Save to CSV | Power BI export / dataflow | 🔧 | |
| 4 | Save to Database | DirectQuery or dataflow | 🔧 | |
| 5 | Incremental refresh | Power BI incremental refresh policy | 🔧 | Manual configuration |

---

## 14. TWB-Embedded Data Transformations

These transformations can appear inside `.twb` files (Tableau Desktop Data Source pane) and are extracted during migration:

| # | TWB XML Element | Power Query M Step | Status | Notes |
|---|----------------|-------------------|--------|-------|
| 1 | `<relation type='join'>` | TMDL relationship | ✅ | Extracted to relationships.tmdl |
| 2 | `<relation type='union'>` | `Table.Combine()` | 🆕 | Detected and converted |
| 3 | `<_.fcp.ObjectModelEncapsulatePivot>` | `Table.Unpivot()` | 🆕 | Pivot in data source pane |
| 4 | `<column ... hidden='true'>` | `Table.RemoveColumns()` | 🆕 | Hidden columns removed |
| 5 | `<column ... caption='...'>` | `Table.RenameColumns()` | 🆕 | Caption used as display name |
| 6 | `<filter ... column='...' type='quantitative'>` | `Table.SelectRows()` | 🆕 | Data source filter |
| 7 | `<filter ... column='...' type='categorical'>` | `Table.SelectRows()` | 🆕 | Data source filter |
| 8 | Custom SQL (`<relation type='text'>`) | `[Query="..."]` option | ✅ | Native SQL passthrough |
| 9 | Data type override (`<metadata-record ... datatype='...'>`) | `Table.TransformColumnTypes()` | ✅ | Type changes |
| 10 | Column alias (`<aliases>`) | Preserved in TMDL description | ✅ | |

---

## 15. Summary Statistics

| Category | Total Operations | ✅ Auto | 🆕 New | 🔧 Manual | ❌ N/A |
|----------|-----------------|---------|-------|-----------|--------|
| Input Steps | 11 | 9 | 1 | 1 | 0 |
| Clean — Columns | 9 | 1 | 8 | 0 | 0 |
| Clean — Values | 13 | 0 | 13 | 0 | 0 |
| Filter | 9 | 0 | 9 | 0 | 0 |
| Calculated Fields | 8 | 1 | 7 | 0 | 0 |
| Aggregate | 10 | 0 | 10 | 0 | 0 |
| Pivot | 3 | 0 | 3 | 0 | 0 |
| Join | 10 | 0 | 8 | 2 | 0 |
| Union | 4 | 0 | 4 | 0 | 0 |
| Reshape | 10 | 1 | 8 | 1 | 0 |
| Functions (String) | 17 | 0 | 17 | 0 | 0 |
| Functions (Date) | 18 | 0 | 18 | 0 | 0 |
| Functions (Numeric) | 13 | 0 | 13 | 0 | 0 |
| Functions (Logic) | 7 | 0 | 7 | 0 | 0 |
| Functions (Conversion) | 5 | 0 | 5 | 0 | 0 |
| Script | 3 | 0 | 0 | 3 | 0 |
| Output | 5 | 1 | 0 | 4 | 0 |
| TWB Embedded | 10 | 5 | 5 | 0 | 0 |
| **TOTAL** | **165** | **18** | **156** | **11** | **0** |

**Coverage: 174/165 operations mapped (18 previously implemented + 156 newly documented)**

---

## Quick-Reference: Common Tableau Prep → Power Query Patterns

### Pattern A: Clean & Filter
```
Tableau Prep:  Input → Clean (rename, type, trim, nulls) → Filter (keep active) → Output
Power Query M:
    let
        Source = Sql.Database("server", "db"),
        Data = Source{[Schema="dbo", Item="Orders"]}[Data],
        #"Renamed" = Table.RenameColumns(Data, {{"old_name", "NewName"}}),
        #"Changed Types" = Table.TransformColumnTypes(#"Renamed", {{"Amount", type number}}),
        #"Trimmed" = Table.TransformColumns(#"Changed Types", {{"Name", Text.Trim}}),
        #"Replaced Nulls" = Table.ReplaceValue(#"Trimmed", null, 0, Replacer.ReplaceValue, {"Amount"}),
        #"Filtered" = Table.SelectRows(#"Replaced Nulls", each [Status] = "Active")
    in
        #"Filtered"
```

### Pattern B: Join & Aggregate
```
Tableau Prep:  Orders Input → Join(Customers) → Aggregate(Region, SUM Sales) → Output
Power Query M:
    let
        Orders = ...,
        Customers = ...,
        #"Joined" = Table.NestedJoin(Orders, "CustomerID", Customers, "CustomerID", "Cust", JoinKind.LeftOuter),
        #"Expanded" = Table.ExpandTableColumn(#"Joined", "Cust", {"Region", "Segment"}),
        #"Grouped" = Table.Group(#"Expanded", {"Region"}, {{"Total Sales", each List.Sum([Sales]), type number}})
    in
        #"Grouped"
```

### Pattern C: Pivot (Columns to Rows)
```
Tableau Prep:  Input → Pivot(Q1,Q2,Q3,Q4 to rows) → Output
Power Query M:
    let
        Source = ...,
        #"Unpivoted" = Table.Unpivot(Source, {"Q1", "Q2", "Q3", "Q4"}, "Quarter", "Revenue")
    in
        #"Unpivoted"
```

### Pattern D: Union Multiple Files
```
Tableau Prep:  File1 + File2 + File3 → Union → Clean → Output
Power Query M:
    let
        Files = Folder.Files("C:\Data\Monthly"),
        #"Filtered CSVs" = Table.SelectRows(Files, each Text.EndsWith([Name], ".csv")),
        #"Add Content" = Table.AddColumn(#"Filtered CSVs", "Tables", each Csv.Document([Content], [Delimiter=","])),
        #"Combined" = Table.Combine(#"Add Content"[Tables]),
        #"Headers" = Table.PromoteHeaders(#"Combined")
    in
        #"Headers"
```
