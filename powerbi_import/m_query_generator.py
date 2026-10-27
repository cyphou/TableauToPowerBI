"""
M query (Power Query) generator from converted Tableau datasources
Generates realistic M queries instead of empty tables
"""

import json
import re
import textwrap
from pathlib import Path


def extract_m_partitions_from_tmdl(content):
    """Return ``(partition_name, M)`` pairs from serialized table TMDL.

    This is an export reader, not an M normalizer. Only the TMDL expression
    indentation (and optional TMDL code fence) is removed. In particular,
    nested let scopes, comments, CSV quoting options, aliases and M string
    escapes must survive exactly as serialized by the semantic writer.
    """
    lines = content.splitlines()
    partitions = []
    header = re.compile(
        r"^(?P<indent>[ \t]+)partition\s+"
        r"(?P<name>'(?:[^']|'')*'|.+?)\s*=\s*m\s*$"
    )
    source_header = re.compile(r'^[ \t]+source\s*=\s*(.*)$')

    def indentation(line):
        return len(line) - len(line.lstrip(' \t'))

    i = 0
    while i < len(lines):
        match = header.match(lines[i])
        if not match:
            i += 1
            continue
        name = match['name']
        if name.startswith("'") and name.endswith("'"):
            name = name[1:-1].replace("''", "'")
        partition_indent = len(match['indent'])
        i += 1
        while i < len(lines):
            line = lines[i]
            if line.strip() and indentation(line) <= partition_indent:
                break
            source = source_header.match(line)
            i += 1
            if not source:
                continue
            source_indent = indentation(line)
            inline = source[1]
            if inline and inline != '```':
                expression = inline
            else:
                body = []
                fenced = inline == '```'
                while i < len(lines):
                    line = lines[i]
                    if fenced and line.strip() == '```':
                        i += 1
                        break
                    if line.strip() and indentation(line) <= source_indent:
                        break
                    body.append(line)
                    i += 1
                # Do not strip expression indentation beyond the common
                # serialization prefix, or alter backslashes as if this
                # were a JSON string. TMDL source blocks contain literal M.
                expression = textwrap.dedent('\n'.join(body)).strip('\r\n')
            if expression:
                partitions.append((name, expression))
            break
    return partitions


def export_final_power_query_files(project_dir, source_basename):
    """Export actual on-disk M partitions after all semantic normalization.

    Call this *after* CSV reconciliation, alias duplication, partition
    restriction and other post-generation rewrites, not from datasource
    metadata or an earlier model snapshot. Re-running overwrites the same
    exports with the final serialized M, without regenerating any M steps.

    Single-partition tables keep the established ``PowerQuery/Table.pq``
    name. Multi-partition tables export every M partition, using stable
    numbered suffixes for the additional partitions. Shared expressions
    retain the existing ``_expressions.pq`` TMDL export convention.

    Returns the number of files written. I/O errors propagate to the caller
    so an unsuccessful synchronization cannot be reported as successful.
    """
    model_dir = Path(project_dir) / f'{source_basename}.SemanticModel' / 'definition'
    tables_dir = model_dir / 'tables'
    if not tables_dir.is_dir():
        return 0

    # Read everything before writing: a failed read must not leave an export
    # assembled partly from old partitions and partly from normalized ones.
    exports = {}
    table_paths = sorted(tables_dir.glob('*.tmdl'))
    reserved = {path.stem.casefold() + '.pq' for path in table_paths}
    reserved.add('_expressions.pq')
    for path in table_paths:
        partitions = extract_m_partitions_from_tmdl(
            path.read_text(encoding='utf-8-sig'))
        for index, (_partition_name, expression) in enumerate(partitions, 1):
            filename = f'{path.stem}.pq'
            if index > 1:
                suffix = index
                filename = f'{path.stem}__partition_{suffix}.pq'
                while filename.casefold() in reserved:
                    suffix += 1
                    filename = f'{path.stem}__partition_{suffix}.pq'
            if filename.casefold() in {name.casefold() for name in exports}:
                raise ValueError(f'Power Query export filename collision: {filename}')
            reserved.add(filename.casefold())
            exports[filename] = expression

    expressions_path = model_dir / 'expressions.tmdl'
    if expressions_path.is_file():
        expressions = expressions_path.read_text(encoding='utf-8-sig').strip()
        if expressions:
            if '_expressions.pq' in exports:
                raise ValueError('Table export conflicts with _expressions.pq')
            exports['_expressions.pq'] = expressions
    if exports:
        output_dir = Path(project_dir) / 'PowerQuery'
        output_dir.mkdir(parents=True, exist_ok=True)
        for filename, expression in exports.items():
            (output_dir / filename).write_text(expression, encoding='utf-8')
    return len(exports)


def sync_final_calendar_source_expressions(project_dir, source_basename):
    """Refresh persisted Calendar source copies from final serialized fact M.

    Contract with the semantic writer: each managed shared expression carries
    ``annotation Migration_CalendarSourceTable = <JSON string>``. No expression-name,
    column-name or filename heuristics are used. Other expressions and all
    metadata are preserved. Call after the final partition rewrites and before
    exporting PowerQuery files or opening the model.

    Returns the number of changed expressions (zero is an idempotent no-op).
    Missing/ambiguous sources or invalid provenance raise ValueError before
    writing anything; I/O errors propagate. This never repairs M or suppresses
    source failures.
    """
    definition = Path(project_dir) / f'{source_basename}.SemanticModel' / 'definition'
    expressions_path = definition / 'expressions.tmdl'
    if not expressions_path.is_file():
        return 0
    raw = expressions_path.read_bytes()
    bom = raw.startswith(b'\xef\xbb\xbf')
    content = raw.decode('utf-8-sig')
    newline = '\r\n' if '\r\n' in content else '\n'
    lines = content.splitlines(keepends=True)
    expression_header = re.compile(
        r"^expression\s+(?:'(?:[^']|'')*'|[^=\r\n]+?)\s*="
    )
    annotation = re.compile(
        r'^(?P<indent>[ \t]+)annotation\s+Migration_CalendarSourceTable\s*=\s*(?P<value>.*?)\s*$')
    header_by_start = {}
    for index, line in enumerate(lines):
        header_match = expression_header.match(line)
        if header_match:
            header_by_start[index] = header_match.group(0)
    starts = list(header_by_start)
    replacements = []
    tables = None
    for start, end in zip(starts, starts[1:] + [len(lines)]):
        provenance = []
        for line in lines[start + 1:end]:
            match = annotation.match(line.rstrip('\r\n'))
            if match:
                provenance.append((match['indent'], match['value']))
        if not provenance:
            continue
        if len(provenance) != 1:
            raise ValueError('Calendar source expression has duplicate provenance annotations')
        try:
            table_name = json.loads(provenance[0][1])
        except (ValueError, TypeError) as exc:
            raise ValueError('Migration_CalendarSourceTable must contain a JSON string') from exc
        if not isinstance(table_name, str) or not table_name:
            raise ValueError('Migration_CalendarSourceTable must contain a nonempty table name')

        if tables is None:
            # Resolve semantic table identity from its declaration, never by
            # guessing a sanitized filename or traversing a provenance path.
            tables = {}
            table_header = re.compile(
                r"^table\s+('(?:[^']|'')*'|[^\r\n]+?)\s*$", re.MULTILINE)
            for path in sorted((definition / 'tables').glob('*.tmdl')):
                text = path.read_text(encoding='utf-8-sig')
                header = table_header.search(text)
                if not header:
                    continue
                name = header[1]
                if name.startswith("'") and name.endswith("'"):
                    name = name[1:-1].replace("''", "'")
                tables.setdefault(name, []).append(text)
        sources = tables.get(table_name, [])
        if len(sources) != 1:
            raise ValueError(f"Calendar source table {table_name!r} is missing or ambiguous")
        source = sources[0]
        partitions = extract_m_partitions_from_tmdl(source)
        partition_count = len(re.findall(r'^[ \t]+partition\s+', source, re.MULTILINE))
        if len(partitions) != 1 or partition_count != 1:
            raise ValueError(f"Calendar source table {table_name!r} must have one M partition")
        final_m = partitions[0][1]

        # Shared expressions are root declarations; their metadata starts at
        # one indentation level. Stop there, not at nested M let/in keywords.
        metadata_start = end
        metadata = re.compile(
            '^' + re.escape(provenance[0][0]) + r'(?:annotation\s+|[A-Za-z]\w*\s*:)')
        for i in range(start + 1, end):
            if metadata.match(lines[i]):
                metadata_start = i
                break
        body_end = metadata_start
        while body_end > start + 1 and not lines[body_end - 1].strip():
            body_end -= 1
        header = header_by_start[start]
        replacement = header + newline
        # Match the semantic writer: the M body must be deeper than the
        # expression metadata, or Desktop consumes the annotation as M.
        # Existing blank separators are outside this replacement span and
        # remain untouched; a blank line alone is not an expression boundary.
        body_indent = provenance[0][0] + '\t'
        replacement += ''.join(body_indent + line + newline
                               for line in final_m.splitlines())
        old = ''.join(lines[start:body_end])
        if replacement != old:
            replacements.append((start, body_end, replacement))
    if replacements:
        for start, end, replacement in reversed(replacements):
            lines[start:end] = [replacement]
        updated = ''.join(lines).encode('utf-8')
        expressions_path.write_bytes((b'\xef\xbb\xbf' if bom else b'') + updated)
    return len(replacements)


def generate_m_query_from_datasource(datasource, table_name):
    """
    Generate an M Power Query from a Tableau datasource

    Args:
        datasource: Datasource converted from Tableau
        table_name: Name of the table to generate

    Returns:
        String: Complete M query
    """

    connection = datasource.get('dataSource', {})
    conn_type = connection.get('connectionType', 'Unknown').lower()

    # Generate query based on connection type
    if 'sql' in conn_type or 'postgres' in conn_type or 'mysql' in conn_type or 'oracle' in conn_type:
        return generate_sql_query(connection, table_name)
    elif 'excel' in conn_type:
        return generate_excel_query(connection, table_name)
    elif 'csv' in conn_type or 'text' in conn_type:
        return generate_csv_query(connection, table_name)
    elif 'web' in conn_type:
        return generate_web_query(connection, table_name)
    elif 'json' in conn_type:
        return generate_json_query(connection, table_name)
    else:
        # Fallback: sample table with test data
        return generate_sample_data_query(table_name, datasource)


def generate_sql_query(connection, table_name):
    """Generate an M query for a SQL connection"""

    server = connection.get('connectionDetails', {}).get('server', 'YourServer')
    database = connection.get('connectionDetails', {}).get('database', 'YourDatabase')

    query = f"""let
    // ============================================================
    // CONFIGURATION: Update these values with your information
    // ============================================================
    Server = "{server}",
    Database = "{database}",
    TableName = "{table_name}",

    // ============================================================
    // DATABASE CONNECTION
    // ============================================================
    Source = Sql.Database(Server, Database),

    // ============================================================
    // TABLE SELECTION
    // ============================================================
    Table = Source{{[Schema="dbo", Item=TableName]}}[Data],

    // ============================================================
    // COLUMN TYPING (optional - adjust as needed)
    // ============================================================
    // ChangedType = Table.TransformColumnTypes(Table,{{
    //     {{"Column1", type text}},
    //     {{"Column2", type number}}
    // }}),

    Result = Table
in
    Result"""

    return query


def generate_excel_query(connection, table_name):
    """Generate an M query for an Excel file"""

    query = f"""let
    // ============================================================
    // CONFIGURATION: Update the Excel file path
    // ============================================================
    FilePath = "C:\\\\Data\\\\YourFile.xlsx",
    SheetName = "{table_name}",

    // ============================================================
    // LOADING THE EXCEL FILE
    // ============================================================
    Source = Excel.Workbook(File.Contents(FilePath), null, true),

    // ============================================================
    // SHEET SELECTION
    // ============================================================
    Sheet = Source{{[Item=SheetName,Kind="Sheet"]}}[Data],

    // ============================================================
    // HEADER PROMOTION
    // ============================================================
    PromotedHeaders = Table.PromoteHeaders(Sheet, [PromoteAllScalars=true]),

    // ============================================================
    // AUTOMATIC COLUMN TYPING
    // ============================================================
    ChangedType = Table.TransformColumnTypes(PromotedHeaders, List.Transform(Table.ColumnNames(PromotedHeaders), each {{_, type text}})),

    Result = ChangedType
in
    Result"""

    return query


def generate_csv_query(connection, table_name):
    """Generate an M query for a CSV file"""

    query = f"""let
    // ============================================================
    // CONFIGURATION: Update the CSV file path
    // ============================================================
    FilePath = "C:\\\\Data\\\\{table_name}.csv",

    // ============================================================
    // LOADING THE CSV FILE
    // ============================================================
    Source = Csv.Document(File.Contents(FilePath), [
        Delimiter=",",
        Columns=null,
        Encoding=65001,  // UTF-8
        QuoteStyle=QuoteStyle.Csv
    ]),

    // ============================================================
    // HEADER PROMOTION
    // ============================================================
    PromotedHeaders = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),

    // ============================================================
    // AUTOMATIC COLUMN TYPING
    // ============================================================
    ChangedType = Table.TransformColumnTypes(PromotedHeaders, List.Transform(Table.ColumnNames(PromotedHeaders), each {{_, type text}})),

    Result = ChangedType
in
    Result"""

    return query


def generate_web_query(connection, table_name):
    """Generate an M query for a Web source"""

    query = """let
    // ============================================================
    // CONFIGURATION: Update the URL
    // ============================================================
    Url = "https://api.example.com/data",

    // ============================================================
    // WEB SOURCE CONNECTION
    // ============================================================
    Source = Json.Document(Web.Contents(Url)),

    // ============================================================
    // CONVERT TO TABLE
    // ============================================================
    Table = Table.FromRecords(Source),

    Result = Table
in
    Result"""

    return query


def generate_json_query(connection, table_name):
    """Generate an M query for a JSON file"""

    query = f"""let
    // ============================================================
    // CONFIGURATION: Update the JSON file path
    // ============================================================
    FilePath = "C:\\\\Data\\\\{table_name}.json",

    // ============================================================
    // LOADING THE JSON FILE
    // ============================================================
    Source = Json.Document(File.Contents(FilePath)),

    // ============================================================
    // CONVERT TO TABLE
    // ============================================================
    Table = if Value.Is(Source, type list)
            then Table.FromRecords(Source)
            else Table.FromRecords({{Source}}),

    Result = Table
in
    Result"""

    return query


def generate_sample_data_query(table_name, datasource):
    """Generate an M query with sample data"""

    # Extract columns from the datasource
    tables = datasource.get('tables', [])
    columns = []

    for table in tables:
        if table.get('name') == table_name:
            columns = [col.get('name', f'Column{i}') for i, col in enumerate(table.get('columns', []), 1)]
            break

    # If no columns found, use default columns
    if not columns:
        columns = ['ID', 'Name', 'Category', 'Value', 'Date']

    column_list = ', '.join([f'"{col}"' for col in columns])

    # Generate sample data
    sample_rows = []
    for i in range(1, 6):  # 5 sample rows
        row_values = []
        for col in columns:
            col_lower = col.lower()
            if 'id' in col_lower:
                row_values.append(f'{i}')
            elif 'name' in col_lower:
                row_values.append(f'"Item {i}"')
            elif 'category' in col_lower or 'type' in col_lower:
                row_values.append(f'"Category {(i % 3) + 1}"')
            elif 'value' in col_lower or 'amount' in col_lower or 'price' in col_lower:
                row_values.append(f'{100 * i}')
            elif 'date' in col_lower:
                row_values.append(f'#date(2024, {i}, 15)')
            else:
                row_values.append(f'"Value {i}"')

        sample_rows.append('{' + ', '.join(row_values) + '}')

    rows_data = ',\n        '.join(sample_rows)

    query = f"""let
    // ============================================================
    // SAMPLE DATA FOR "{table_name}"
    // ============================================================
    // TODO: Replace with your actual data source
    // ============================================================

    Source = #table(
        // Column definition
        {{{column_list}}},

        // Sample data (5 rows)
        {{
        {rows_data}
        }}
    ),

    // ============================================================
    // To connect an actual data source, replace with:
    // ============================================================
    // SQL Server:
    //   Source = Sql.Database("ServerName", "DatabaseName"){{[Schema="dbo", Item="{table_name}"]}}[Data]
    //
    // Excel:
    //   Source = Excel.Workbook(File.Contents("C:\\Path\\File.xlsx"), null, true){{[Item="{table_name}"]}}[Data]
    //
    // CSV:
    //   Source = Csv.Document(File.Contents("C:\\Path\\{table_name}.csv"), [Delimiter=",", Encoding=65001])
    // ============================================================

    Result = Source
in
    Result"""

    return query


def get_column_type_mapping():
    """Return the Tableau → M column type mapping"""
    return {
        'string': 'type text',
        'integer': 'Int64.Type',
        'real': 'type number',
        'decimal': 'type number',
        'boolean': 'type logical',
        'date': 'type date',
        'datetime': 'type datetime',
        'time': 'type time',
        'spatial': 'type text'  # Geography stored as text
    }
